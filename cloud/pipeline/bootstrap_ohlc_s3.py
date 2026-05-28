"""
bootstrap_ohlc_s3.py
=====================
Cloud equivalent of sql_server/setup/populate_price_data.py

One-time script to load 10 years of historical OHLCV data directly from
yfinance into S3 — zero dependency on SQL Server or any local database.

Run this once to seed the S3 data lake before the daily update pipeline
(update_ohlc_s3.py) takes over.

Flow:
    1. Scrape S&P 500 ticker list from Wikipedia
    2. Download 10yr OHLCV in parallel (20 workers) via yfinance
    3. Upload combined Parquet to:
           s3://<bucket>/raw/ohlc/bootstrap/ohlc_full.parquet

The DuckDB view in the Lambda reads raw/ohlc/*/*.parquet, so the
bootstrap partition is automatically included alongside daily updates.

Usage:
    python bootstrap_ohlc_s3.py                    # full S&P 500
    python bootstrap_ohlc_s3.py --dry-run          # preview only, no writes
    python bootstrap_ohlc_s3.py --tickers AAPL MSFT NVDA  # specific tickers
    python bootstrap_ohlc_s3.py --years 5          # 5yr history instead of 10
"""

import io
import sys
import argparse
import warnings
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
import pandas as pd
import requests
import yfinance as yf

warnings.filterwarnings("ignore")


# ============================================================================
# CONFIGURATION
# ============================================================================

S3_BUCKET    = "meta-analyst-data-lake-YOUR_AWS_ACCOUNT_ID"
AWS_REGION   = "us-east-1"
S3_KEY       = "raw/ohlc/bootstrap/ohlc_full.parquet"
MAX_WORKERS  = 20
YEARS_OF_DATA = 10

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

def get_sp500_tickers():
    """Scrape S&P 500 ticker list from Wikipedia. No SQL Server needed."""
    print("  [INFO] Fetching S&P 500 tickers from Wikipedia...")
    resp = requests.get(WIKIPEDIA_URL, headers=WIKIPEDIA_HEADERS, timeout=15)
    resp.raise_for_status()

    tables = pd.read_html(io.StringIO(resp.text))
    sp500  = tables[0]

    tickers = sp500["Symbol"].str.strip().tolist()
    # yfinance uses '-' for BRK.B / BF.B — these reliably return no data; skip
    tickers = [t.replace(".", "-") for t in tickers]

    print(f"  [INFO] {len(tickers)} tickers found.")
    return tickers


# ============================================================================
# STEP 2: PARALLEL DOWNLOAD
# ============================================================================

def download_ticker(ticker, start_date, end_date):
    """
    Fetch OHLCV history for one ticker from yfinance.

    Returns (ticker, DataFrame) on success, (ticker, None) on failure.
    """
    try:
        hist = yf.Ticker(ticker).history(
            start=start_date.strftime("%Y-%m-%d"),
            end=end_date.strftime("%Y-%m-%d"),
            auto_adjust=True
        )
        if hist.empty:
            return ticker, None

        hist = hist.reset_index()[["Date", "Open", "High", "Low", "Close", "Volume"]]
        hist["Date"]   = pd.to_datetime(hist["Date"]).dt.tz_localize(None)
        hist.insert(0, "Ticker", ticker)
        return ticker, hist

    except Exception as e:
        return ticker, None


def parallel_download(tickers, start_date, end_date):
    """Download all tickers in parallel. Returns dict of ticker -> DataFrame."""
    print(f"\n  [FETCH] Downloading {len(tickers)} tickers ({MAX_WORKERS} workers)...")
    print(f"          Date range: {start_date.strftime('%Y-%m-%d')} -> {end_date.strftime('%Y-%m-%d')}\n")

    results  = {}
    errors   = []
    no_data  = []
    done     = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(download_ticker, t, start_date, end_date): t
            for t in tickers
        }
        for future in as_completed(futures):
            ticker, df = future.result()
            done += 1

            if df is None:
                no_data.append(ticker)
            else:
                results[ticker] = df

            if done % 50 == 0 or done == len(tickers):
                ok  = len(results)
                bad = len(no_data) + len(errors)
                print(f"  [PROGRESS] {done}/{len(tickers)}  ok={ok}  no_data/error={bad}")

    return results, no_data


# ============================================================================
# STEP 3: UPLOAD TO S3
# ============================================================================

def upload_to_s3(combined_df, dry_run=False):
    """Concatenate all data and upload as a single Parquet to S3."""
    rows = len(combined_df)
    size_mb = combined_df.memory_usage(deep=True).sum() / 1024 / 1024

    print(f"\n  [WRITE] {rows:,} rows across {combined_df['Ticker'].nunique()} tickers ({size_mb:.1f} MB in memory)")
    print(f"          Target: s3://{S3_BUCKET}/{S3_KEY}")

    if dry_run:
        print("  [DRY RUN] No data written.")
        return

    buf = io.BytesIO()
    combined_df.to_parquet(buf, index=False, engine="pyarrow", compression="snappy")
    buf.seek(0)

    s3 = boto3.client("s3", region_name=AWS_REGION)
    s3.put_object(Bucket=S3_BUCKET, Key=S3_KEY, Body=buf.read())

    print(f"  [WRITE] Done. s3://{S3_BUCKET}/{S3_KEY}")


# ============================================================================
# MAIN
# ============================================================================

def run(tickers=None, years=YEARS_OF_DATA, dry_run=False):
    start_time = datetime.now()
    end_date   = datetime.now()
    start_date = end_date - timedelta(days=years * 365)

    mode = " [DRY RUN]" if dry_run else ""
    print()
    print("=" * 68)
    print(f"  BOOTSTRAP_OHLC_S3 — Historical Load{mode}")
    print(f"  Started : {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Years   : {years}  ({start_date.strftime('%Y-%m-%d')} onward)")
    print(f"  Target  : s3://{S3_BUCKET}/{S3_KEY}")
    print("=" * 68)

    # Step 1: Ticker list
    if tickers:
        print(f"\n  [INFO] Using {len(tickers)} user-supplied tickers.")
    else:
        tickers = get_sp500_tickers()

    # Step 2: Download
    results, no_data = parallel_download(tickers, start_date, end_date)

    if not results:
        print("\n  [ERROR] No data downloaded. Nothing to write.")
        sys.exit(1)

    # Step 3: Combine and upload
    combined = pd.concat(results.values(), ignore_index=True)
    upload_to_s3(combined, dry_run=dry_run)

    # Summary
    elapsed = (datetime.now() - start_time).seconds
    print()
    print("=" * 68)
    print("  BOOTSTRAP SUMMARY")
    print("=" * 68)
    print(f"  Tickers requested : {len(tickers)}")
    print(f"  Tickers with data : {len(results)}")
    print(f"  No data / failed  : {len(no_data)}")
    print(f"  Total rows        : {len(combined):,}")
    print(f"  Elapsed           : {elapsed}s")
    print("=" * 68)

    if no_data:
        print(f"\n  Tickers with no data (likely delisted or no options):")
        for t in sorted(no_data):
            print(f"    - {t}")

    print("\n  DONE.\n")


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Bootstrap 10yr OHLC history from yfinance to S3. No SQL Server."
    )
    parser.add_argument(
        "--tickers", nargs="+", metavar="TICKER",
        help="Specific tickers to load (default: full S&P 500 from Wikipedia)."
    )
    parser.add_argument(
        "--years", type=int, default=YEARS_OF_DATA,
        help=f"Years of history to fetch (default: {YEARS_OF_DATA})."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Download data but skip S3 upload. Shows what would be written."
    )
    args = parser.parse_args()

    run(
        tickers = args.tickers,
        years   = args.years,
        dry_run = args.dry_run,
    )
