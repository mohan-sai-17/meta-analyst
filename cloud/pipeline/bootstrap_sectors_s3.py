"""
bootstrap_sectors_s3.py
========================
One-time script to add the Sector column to the stocks_list Parquet in S3.

Flow:
    1. Download current stocks_list.parquet from S3.
    2. Fetch yfinance sector for each ticker (20 parallel workers).
    3. Add Sector column to DataFrame.
    4. Re-upload to S3 (overwrites bootstrap file in-place).

Run once after bootstrap_stocks_s3.py has completed.
Does not need to run daily — sector assignments change very rarely.

Usage:
    python bootstrap_sectors_s3.py
    python bootstrap_sectors_s3.py --dry-run
"""

import io
import sys
import time
import warnings
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import boto3
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")


# ============================================================================
# CONFIGURATION
# ============================================================================

S3_BUCKET   = "meta-analyst-data-lake-YOUR_AWS_ACCOUNT_ID"
AWS_REGION  = "us-east-1"
S3_PREFIX   = "raw/stocks_list/"
S3_OUT_KEY  = "raw/stocks_list/bootstrap/stocks_list.parquet"
MAX_WORKERS = 20
SLEEP_PER   = 0.2


# ============================================================================
# FETCH SECTOR
# ============================================================================

def fetch_sector(ticker):
    """Fetch sector string from yfinance for one ticker."""
    try:
        time.sleep(SLEEP_PER)
        info   = yf.Ticker(ticker).info
        sector = info.get("sector") or "Unknown"
        return ticker, sector
    except Exception:
        return ticker, "Unknown"


# ============================================================================
# MAIN
# ============================================================================

def run(dry_run=False):
    start_time = datetime.now()
    mode       = " [DRY RUN]" if dry_run else ""

    print()
    print("=" * 68)
    print(f"  BOOTSTRAP_SECTORS_S3 — Add Sector column to stocks_list{mode}")
    print(f"  Started : {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Target  : s3://{S3_BUCKET}/{S3_OUT_KEY}")
    print("=" * 68)

    # Fast idempotency check — if the bootstrap output already exists, skip entirely.
    # This runs daily in the pipeline; head_object is a single cheap API call.
    if not dry_run:
        s3_check = boto3.client("s3", region_name=AWS_REGION)
        try:
            s3_check.head_object(Bucket=S3_BUCKET, Key=S3_OUT_KEY)
            print("  [SKIP] Bootstrap file already exists — Sector column populated. Nothing to do.")
            return
        except s3_check.exceptions.ClientError:
            pass  # File not found — proceed with full bootstrap

    # Step 1: Merge all stocks_list Parquet files under the prefix (same approach as compute_marts)
    print(f"\n  [DOWNLOAD] Listing s3://{S3_BUCKET}/{S3_PREFIX}* ...")
    s3 = boto3.client("s3", region_name=AWS_REGION)

    resp    = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=S3_PREFIX)
    objects = [o for o in resp.get("Contents", []) if o["Key"].endswith(".parquet")]
    if not objects:
        print("  [ERROR] No stocks_list Parquet files found in S3. Run bootstrap_stocks_s3.py first.")
        sys.exit(1)

    print(f"  [INFO] Found {len(objects)} file(s). Merging...")
    frames = []
    for obj in objects:
        buf = io.BytesIO()
        s3.download_fileobj(S3_BUCKET, obj["Key"], buf)
        buf.seek(0)
        frames.append(pd.read_parquet(buf))
    stocks = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["Ticker"])
    print(f"  [INFO] {len(stocks)} tickers loaded.")

    if "Sector" in stocks.columns:
        existing = stocks["Sector"].notna().sum()
        print(f"  [INFO] Sector column already exists ({existing} non-null values).")
        if existing == len(stocks) and not dry_run:
            print("  All sectors already populated. Nothing to do.\n  DONE.\n")
            return

    tickers = stocks["Ticker"].tolist()

    # Step 2: Fetch sectors in parallel
    print(f"\n  [FETCH] Fetching sectors for {len(tickers)} tickers ({MAX_WORKERS} workers)...")
    sector_map = {}
    done       = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(fetch_sector, t): t for t in tickers}
        for future in as_completed(futures):
            ticker, sector = future.result()
            sector_map[ticker] = sector
            done += 1
            if done % 100 == 0:
                print(f"    {done}/{len(tickers)} done")

    print(f"  [FETCH] Complete. {len(sector_map)} tickers.")

    # Step 3: Add / update Sector column
    stocks["Sector"] = stocks["Ticker"].map(sector_map).fillna("Unknown")

    sector_counts = stocks["Sector"].value_counts()
    print(f"\n  Sector breakdown:")
    for sec, cnt in sector_counts.items():
        print(f"    {sec:<35} {cnt:>4}")

    if dry_run:
        print("\n  [DRY RUN] No S3 upload.")
        return

    # Step 4: Re-upload to canonical bootstrap key (compute_marts picks it up via prefix scan)
    print(f"\n  [UPLOAD] Writing s3://{S3_BUCKET}/{S3_OUT_KEY} ...")
    out_buf = io.BytesIO()
    stocks.to_parquet(out_buf, index=False, engine="pyarrow", compression="snappy")
    out_buf.seek(0)
    s3.put_object(Bucket=S3_BUCKET, Key=S3_OUT_KEY, Body=out_buf.read())
    print(f"  [UPLOAD] Done.")

    elapsed = (datetime.now() - start_time).seconds
    print()
    print("=" * 68)
    print(f"  DONE. Sector column added to {len(stocks)} tickers in {elapsed}s.")
    print("=" * 68)
    print()


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Add Sector column to S3 stocks_list. Run once after bootstrap."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch sectors but skip S3 upload.")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
