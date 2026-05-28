"""
bootstrap_stocks_s3.py
=======================
Cloud equivalent of sql_server/setup/setup_master_list.py

One-time script to build the stocks_list (S&P 500 + underwriter data)
directly from public internet sources and upload to S3.
Zero dependency on SQL Server or any local database.

Flow:
    1. Scrape S&P 500 ticker list from Wikipedia
    2. Download SEC S-1 filings and extract underwriter names in parallel
    3. Upload stocks_list as Parquet to:
           s3://<bucket>/raw/stocks_list/bootstrap/stocks_list.parquet

The Lambda reads raw/stocks_list/*/*.parquet for the /tickers and
/friends endpoints, so this bootstrap partition is picked up automatically.

Usage:
    python bootstrap_stocks_s3.py                  # full S&P 500
    python bootstrap_stocks_s3.py --dry-run        # preview, no S3 write
    python bootstrap_stocks_s3.py --tickers AAPL MSFT  # specific tickers only
"""

import io
import shutil
import warnings
import argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import boto3
import pandas as pd
import requests
from bs4 import BeautifulSoup
from sec_edgar_downloader import Downloader

warnings.filterwarnings("ignore")


# ============================================================================
# CONFIGURATION
# ============================================================================

S3_BUCKET  = "meta-analyst-data-lake-YOUR_AWS_ACCOUNT_ID"
AWS_REGION = "us-east-1"
S3_KEY     = "raw/stocks_list/bootstrap/stocks_list.parquet"

WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
WIKIPEDIA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/91.0.4472.124 Safari/537.36"
    )
}

# Investment banks to look for in S-1 filings
KNOWN_BANKS = [
    "Goldman Sachs", "Morgan Stanley", "J.P. Morgan", "JPMorgan",
    "Bank of America", "Citigroup", "Citi", "Wells Fargo",
    "Credit Suisse", "Deutsche Bank", "Barclays", "UBS",
    "BNP Paribas", "HSBC", "RBC Capital Markets", "Jefferies",
    "Merrill Lynch", "Lazard", "Evercore", "Piper Sandler",
    "William Blair", "Stifel", "Cowen", "Guggenheim",
    "Canaccord Genuity", "Oppenheimer", "Raymond James",
]

SEC_BATCH_SIZE = 100
SEC_WORKERS    = 150


# ============================================================================
# STEP 1: S&P 500 TICKER LIST
# ============================================================================

def get_sp500_tickers():
    """Scrape S&P 500 ticker list and company names from Wikipedia."""
    print("  [INFO] Fetching S&P 500 tickers from Wikipedia...")
    resp = requests.get(WIKIPEDIA_URL, headers=WIKIPEDIA_HEADERS, timeout=15)
    resp.raise_for_status()

    tables    = pd.read_html(io.StringIO(resp.text))
    sp500     = tables[0]

    df = pd.DataFrame({
        "Ticker"    : sp500["Symbol"].str.strip(),
        "Stock_Name": sp500["Security"].str.strip(),
    })

    print(f"  [INFO] {len(df)} S&P 500 companies retrieved.")
    return df


# ============================================================================
# STEP 2: UNDERWRITER EXTRACTION (SEC S-1 FILINGS)
# ============================================================================

def get_underwriters(ticker):
    """
    Download the most recent S-1 filing for a ticker and extract
    underwriter (investment bank) names from it.

    Returns a comma-separated string of bank names, or 'Unknown'.
    """
    temp_dir = Path(f"temp_sec_{ticker}")
    try:
        dl = Downloader("MetaAnalyst", "your-email@example.com", str(temp_dir))
        dl.get("S-1", ticker, limit=1, download_details=True)

        ticker_folder = temp_dir / "sec-edgar-filings" / ticker / "S-1"
        if not ticker_folder.exists():
            return ticker, "Unknown"

        filing_dirs = sorted(ticker_folder.iterdir(), reverse=True)
        if not filing_dirs:
            return ticker, "Unknown"

        html_file = filing_dirs[0] / "full-submission.txt"
        if not html_file.exists():
            html_file = filing_dirs[0] / "primary-document.html"
        if not html_file.exists():
            return ticker, "Unknown"

        with open(html_file, "r", encoding="utf-8", errors="ignore") as f:
            soup = BeautifulSoup(f.read(), "html.parser")

        text_content = soup.get_text()
        if "underwriter" not in text_content.lower() and "underwriting" not in text_content.lower():
            return ticker, "Unknown"

        found = list(dict.fromkeys(b for b in KNOWN_BANKS if b in text_content))
        return ticker, ", ".join(found) if found else "Unknown"

    except Exception:
        return ticker, "Unknown"

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def extract_all_underwriters(tickers):
    """Run SEC S-1 extraction in parallel batches. Returns dict ticker -> friends."""
    total      = len(tickers)
    num_batches = (total + SEC_BATCH_SIZE - 1) // SEC_BATCH_SIZE
    all_friends = {}

    print(f"\n  [SEC] Extracting underwriters for {total} tickers")
    print(f"        {SEC_WORKERS} workers, batches of {SEC_BATCH_SIZE}")

    try:
        for batch_num in range(num_batches):
            batch = tickers[batch_num * SEC_BATCH_SIZE:(batch_num + 1) * SEC_BATCH_SIZE]
            print(f"\n  [BATCH {batch_num+1}/{num_batches}] {len(batch)} tickers...")

            with ThreadPoolExecutor(max_workers=SEC_WORKERS) as executor:
                futures = {executor.submit(get_underwriters, t): t for t in batch}
                for future in as_completed(futures):
                    ticker, friends = future.result()
                    all_friends[ticker] = friends

            done = len(all_friends)
            print(f"  [BATCH {batch_num+1}/{num_batches}] Done — {done}/{total} total processed")

    except KeyboardInterrupt:
        print(f"\n  [WARN] Interrupted — saving {len(all_friends)} processed so far.")

    return all_friends


# ============================================================================
# STEP 3: UPLOAD TO S3
# ============================================================================

def upload_to_s3(df, dry_run=False):
    """Upload stocks_list DataFrame as Parquet to S3."""
    print(f"\n  [WRITE] {len(df)} rows -> s3://{S3_BUCKET}/{S3_KEY}")

    if dry_run:
        print("  [DRY RUN] No data written.")
        print(df[["Ticker", "Stock_Name", "Friends"]].head(10).to_string(index=False))
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

def run(tickers_filter=None, dry_run=False):
    start_time = datetime.now()
    mode = " [DRY RUN]" if dry_run else ""

    print()
    print("=" * 68)
    print(f"  BOOTSTRAP_STOCKS_S3 — S&P 500 + Underwriter Data{mode}")
    print(f"  Started : {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Target  : s3://{S3_BUCKET}/{S3_KEY}")
    print("=" * 68)

    # Step 1: Ticker + company name list
    sp500_df = get_sp500_tickers()

    if tickers_filter:
        sp500_df = sp500_df[sp500_df["Ticker"].isin(tickers_filter)].reset_index(drop=True)
        print(f"  [INFO] Filtered to {len(sp500_df)} user-supplied tickers.")

    tickers = sp500_df["Ticker"].tolist()

    # Step 2: Underwriter extraction
    friends_map = extract_all_underwriters(tickers)

    sp500_df["Friends"] = sp500_df["Ticker"].map(friends_map).fillna("Unknown")

    # Step 3: Upload
    upload_to_s3(sp500_df, dry_run=dry_run)

    # Summary
    elapsed  = (datetime.now() - start_time).seconds
    known    = (sp500_df["Friends"] != "Unknown").sum()
    unknown  = (sp500_df["Friends"] == "Unknown").sum()

    print()
    print("=" * 68)
    print("  BOOTSTRAP SUMMARY")
    print("=" * 68)
    print(f"  Tickers processed        : {len(sp500_df)}")
    print(f"  With underwriter data    : {known}")
    print(f"  Unknown / no S-1 found   : {unknown}")
    print(f"  Elapsed                  : {elapsed}s")
    print("=" * 68)
    print("\n  DONE.\n")


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Bootstrap stocks_list from Wikipedia + SEC EDGAR to S3. No SQL Server."
    )
    parser.add_argument(
        "--tickers", nargs="+", metavar="TICKER",
        help="Specific tickers to process (default: full S&P 500)."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run extraction but skip S3 upload. Prints sample output."
    )
    args = parser.parse_args()

    run(tickers_filter=args.tickers, dry_run=args.dry_run)
