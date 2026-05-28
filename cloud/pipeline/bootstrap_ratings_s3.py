"""
bootstrap_ratings_s3.py
========================
Cloud equivalent of sql_server/setup/setup_analyst_ratings.py

One-time script to load full analyst upgrade/downgrade history from
Yahoo Finance for all S&P 500 stocks and upload directly to S3.
Zero dependency on SQL Server or any local database.

Flow:
    1. Get ticker list — reads from S3 stocks_list (if bootstrap already run)
       or falls back to scraping Wikipedia directly
    2. Fetch analyst ratings from yfinance.upgrades_downgrades for each ticker
       (10 parallel workers with 0.5s sleep per worker to avoid rate limiting)
    3. Upload combined Parquet to:
           s3://<bucket>/raw/ratings/bootstrap/ratings.parquet

The Lambda reads raw/ratings/*/*.parquet for the /ratings endpoint, so
this bootstrap partition is picked up automatically alongside daily updates.

Note on Action values: yfinance returns abbreviations directly —
    'up'   = Upgrade
    'down' = Downgrade
    'init' = Initiates Coverage
    'main' = Maintains
    'reit' = Reiterates

Usage:
    python bootstrap_ratings_s3.py                  # full S&P 500
    python bootstrap_ratings_s3.py --dry-run        # preview, no S3 write
    python bootstrap_ratings_s3.py --tickers AAPL MSFT  # specific tickers only
    python bootstrap_ratings_s3.py --no-s3-tickers  # always scrape Wikipedia
"""

import io
import sys
import time
import warnings
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import boto3
import duckdb
import pandas as pd
import requests
import yfinance as yf

warnings.filterwarnings("ignore")


# ============================================================================
# CONFIGURATION
# ============================================================================

S3_BUCKET      = "meta-analyst-data-lake-YOUR_AWS_ACCOUNT_ID"
AWS_REGION     = "us-east-1"
S3_KEY         = "raw/ratings/bootstrap/ratings.parquet"
S3_STOCKS_PATH = f"s3://{S3_BUCKET}/raw/stocks_list/bootstrap/stocks_list.parquet"

MAX_WORKERS    = 10       # conservative — Yahoo Finance rate limits parallel requests
SLEEP_PER_REQ  = 0.5     # seconds sleep per worker after each fetch

WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
WIKIPEDIA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/91.0.4472.124 Safari/537.36"
    )
}


# ============================================================================
# STEP 1: TICKER LIST
# ============================================================================

def get_tickers_from_s3():
    """
    Read ticker list from the S3 stocks_list bootstrap Parquet via DuckDB.
    Returns list of tickers, or None if the file doesn't exist yet.
    """
    try:
        conn = duckdb.connect()
        conn.execute("INSTALL httpfs; LOAD httpfs;")
        conn.execute(f"""
            CREATE SECRET (
                TYPE S3,
                PROVIDER CREDENTIAL_CHAIN,
                REGION '{AWS_REGION}'
            )
        """)
        df = conn.execute(
            f"SELECT Ticker FROM read_parquet('{S3_STOCKS_PATH}') ORDER BY Ticker"
        ).fetchdf()
        conn.close()
        tickers = df["Ticker"].tolist()
        print(f"  [INFO] {len(tickers)} tickers read from S3 stocks_list.")
        return tickers
    except Exception as e:
        print(f"  [WARN] Could not read S3 stocks_list ({e}). Falling back to Wikipedia.")
        return None


def get_tickers_from_wikipedia():
    """Scrape S&P 500 ticker list from Wikipedia."""
    print("  [INFO] Fetching tickers from Wikipedia...")
    resp = requests.get(WIKIPEDIA_URL, headers=WIKIPEDIA_HEADERS, timeout=15)
    resp.raise_for_status()
    tables  = pd.read_html(io.StringIO(resp.text))
    tickers = tables[0]["Symbol"].str.strip().tolist()
    print(f"  [INFO] {len(tickers)} tickers from Wikipedia.")
    return tickers


def get_tickers(prefer_s3=True):
    """Return ticker list from S3 if available, otherwise Wikipedia."""
    if prefer_s3:
        tickers = get_tickers_from_s3()
        if tickers:
            return tickers
    return get_tickers_from_wikipedia()


# ============================================================================
# STEP 2: FETCH RATINGS
# ============================================================================

def fetch_ratings(ticker):
    """
    Fetch full analyst upgrade/downgrade history for one ticker via yfinance.

    Returns (ticker, DataFrame) on success, (ticker, None) on skip/error.
    """
    try:
        time.sleep(SLEEP_PER_REQ)
        ratings = yf.Ticker(ticker).upgrades_downgrades

        if ratings is None or ratings.empty:
            return ticker, None

        ratings = ratings.reset_index()          # GradeDate becomes a column
        ratings.insert(0, "Ticker", ticker)
        return ticker, ratings

    except Exception:
        return ticker, None


def fetch_all_ratings(tickers):
    """
    Fetch ratings for all tickers in parallel.
    Returns (list of DataFrames, list of skipped tickers).
    """
    print(f"\n  [FETCH] Fetching ratings for {len(tickers)} tickers ({MAX_WORKERS} workers)...")
    print(f"          Sleep per request: {SLEEP_PER_REQ}s\n")

    results = []
    skipped = []
    done    = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(fetch_ratings, t): t for t in tickers}
        for future in as_completed(futures):
            ticker, df = future.result()
            done += 1

            if df is not None:
                results.append(df)
                print(f"  [OK]   {ticker:<8} {len(df):>5} ratings")
            else:
                skipped.append(ticker)
                print(f"  [SKIP] {ticker:<8} no data")

            if done % 50 == 0:
                print(f"\n  [PROGRESS] {done}/{len(tickers)}  collected={len(results)}  skipped={len(skipped)}\n")

    return results, skipped


# ============================================================================
# STEP 3: UPLOAD TO S3
# ============================================================================

def upload_to_s3(df, dry_run=False):
    """Upload combined ratings DataFrame as Parquet to S3."""
    rows = len(df)
    print(f"\n  [WRITE] {rows:,} rows across {df['Ticker'].nunique()} tickers")
    print(f"          -> s3://{S3_BUCKET}/{S3_KEY}")

    if dry_run:
        print("  [DRY RUN] No data written.")
        print(df.head(10).to_string(index=False))
        return

    buf = io.BytesIO()
    df.to_parquet(buf, index=False, engine="pyarrow", compression="snappy")
    buf.seek(0)

    s3 = boto3.client("s3", region_name=AWS_REGION)
    s3.put_object(Bucket=S3_BUCKET, Key=S3_KEY, Body=buf.read())
    print(f"  [WRITE] Done. s3://{S3_BUCKET}/{S3_KEY}")


# ============================================================================
# MAIN
# ============================================================================

def run(tickers_filter=None, prefer_s3=True, dry_run=False):
    start_time = datetime.now()
    mode = " [DRY RUN]" if dry_run else ""

    print()
    print("=" * 68)
    print(f"  BOOTSTRAP_RATINGS_S3 — Analyst Ratings Historical Load{mode}")
    print(f"  Started : {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Target  : s3://{S3_BUCKET}/{S3_KEY}")
    print("=" * 68)

    # Step 1: Ticker list
    if tickers_filter:
        tickers = tickers_filter
        print(f"\n  [INFO] Using {len(tickers)} user-supplied tickers.")
    else:
        tickers = get_tickers(prefer_s3=prefer_s3)

    # Step 2: Fetch ratings
    results, skipped = fetch_all_ratings(tickers)

    if not results:
        print("\n  [ERROR] No ratings data fetched. Nothing to write.")
        sys.exit(1)

    combined = pd.concat(results, ignore_index=True)

    # Step 3: Upload
    upload_to_s3(combined, dry_run=dry_run)

    # Summary
    elapsed = (datetime.now() - start_time).seconds

    print()
    print("=" * 68)
    print("  BOOTSTRAP SUMMARY")
    print("=" * 68)
    print(f"  Tickers requested        : {len(tickers)}")
    print(f"  Tickers with ratings     : {len(results)}")
    print(f"  Skipped (no data)        : {len(skipped)}")
    print(f"  Total ratings rows       : {len(combined):,}")

    if len(combined) > 0:
        action_counts = combined["Action"].value_counts().to_dict()
        print(f"  Action breakdown         : {action_counts}")

    print(f"  Elapsed                  : {elapsed}s")
    print("=" * 68)

    if skipped:
        print(f"\n  Tickers with no ratings:")
        for t in sorted(skipped):
            print(f"    - {t}")

    print("\n  DONE.\n")


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Bootstrap analyst ratings from yfinance to S3. No SQL Server."
    )
    parser.add_argument(
        "--tickers", nargs="+", metavar="TICKER",
        help="Specific tickers to fetch (default: read from S3 or Wikipedia)."
    )
    parser.add_argument(
        "--no-s3-tickers", action="store_true",
        help="Skip S3 stocks_list lookup and always scrape Wikipedia for tickers."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Fetch data but skip S3 upload. Prints sample output."
    )
    args = parser.parse_args()

    run(
        tickers_filter = args.tickers,
        prefer_s3      = not args.no_s3_tickers,
        dry_run        = args.dry_run,
    )
