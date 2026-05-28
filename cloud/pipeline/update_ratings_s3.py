"""
update_ratings_s3.py
=====================
Daily Incremental Ratings Update — yfinance -> S3.

1. Reads watermarks (MAX GradeDate per ticker) from all existing S3 ratings
   Parquet files using DuckDB.
2. Fetches full upgrade/downgrade history from yfinance for each ticker
   (yfinance has no date-range filter for ratings — full history only).
3. Keeps only rows newer than the watermark (new rows since last run).
4. Writes new rows as a daily partition to S3.

No SQL Server. No BigQuery. Just S3 + DuckDB + yfinance.

Usage:
    python update_ratings_s3.py           # update all tickers
    python update_ratings_s3.py --dry-run # show what would be written, no uploads
"""

import sys
import time
import warnings
import argparse
import boto3
import duckdb
import pandas as pd
import yfinance as yf
from io import BytesIO
from datetime import date, datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

warnings.filterwarnings("ignore")


# ============================================================================
# CONFIGURATION
# ============================================================================

S3_BUCKET   = "meta-analyst-data-lake-YOUR_AWS_ACCOUNT_ID"
AWS_REGION  = "us-east-1"
MAX_WORKERS = 10      # conservative — Yahoo Finance rate-limits parallel requests
SLEEP_PER   = 0.3    # seconds sleep per worker after each fetch


# ============================================================================
# CLOUDWATCH METRICS
# ============================================================================

def _publish(metrics: dict):
    """Non-fatal CloudWatch metric push. metrics = {name: value}."""
    try:
        cw = boto3.client("cloudwatch", region_name=AWS_REGION)
        cw.put_metric_data(
            Namespace="MetaAnalyst/Pipeline",
            MetricData=[{"MetricName": k, "Value": float(v), "Unit": "Count"}
                        for k, v in metrics.items()]
        )
        print(f"  [METRICS] Published: {metrics}")
    except Exception as e:
        print(f"  [METRICS] Skipped (non-fatal): {e}")


# ============================================================================
# HELPERS
# ============================================================================

def get_duckdb_conn():
    conn = duckdb.connect()
    conn.execute("INSTALL httpfs; LOAD httpfs;")
    conn.execute(f"""
        CREATE SECRET (
            TYPE S3,
            PROVIDER CREDENTIAL_CHAIN,
            REGION '{AWS_REGION}'
        )
    """)
    return conn


def get_watermarks(conn):
    """Read MAX(GradeDate) per ticker from ALL ratings S3 partitions via DuckDB."""
    try:
        df = conn.execute(f"""
            SELECT Ticker, MAX(GradeDate)::DATE AS last_date
            FROM read_parquet('s3://{S3_BUCKET}/raw/ratings/*/*.parquet')
            GROUP BY Ticker
        """).fetchdf()
        df["last_date"] = pd.to_datetime(df["last_date"]).dt.date
        return dict(zip(df["Ticker"], df["last_date"]))
    except Exception as e:
        print(f"  [WARN] Could not read watermarks: {e}")
        return {}


def fetch_ratings(ticker, watermark):
    """
    Fetch full ratings history for ticker from yfinance.
    Returns only rows with GradeDate > watermark.
    Returns (ticker, DataFrame) or (ticker, None) on error.
    """
    try:
        time.sleep(SLEEP_PER)
        raw = yf.Ticker(ticker).upgrades_downgrades

        if raw is None or raw.empty:
            return ticker, pd.DataFrame()

        df = raw.reset_index()   # GradeDate becomes a column
        df.insert(0, "Ticker", ticker)

        if watermark is not None:
            df["GradeDate"] = pd.to_datetime(df["GradeDate"]).dt.tz_localize(None).dt.date
            df = df[df["GradeDate"] > watermark]

        return ticker, df

    except Exception:
        return ticker, None


def upload_partition(df, export_date):
    """Write new ratings rows as a date-partitioned Parquet file in S3."""
    key = f"raw/ratings/date={export_date}/ratings_update.parquet"
    buf = BytesIO()
    df.to_parquet(buf, index=False, engine="pyarrow", compression="snappy")
    buf.seek(0)
    s3 = boto3.client("s3", region_name=AWS_REGION)
    s3.put_object(Bucket=S3_BUCKET, Key=key, Body=buf.read())
    return key


# ============================================================================
# MAIN
# ============================================================================

def run_update(dry_run=False):
    start_time  = datetime.now()
    export_date = date.today().isoformat()
    mode_label  = " [DRY RUN]" if dry_run else ""

    print()
    print("=" * 68)
    print(f"  UPDATE_RATINGS_S3 — Daily Incremental Update{mode_label}")
    print(f"  Started  : {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Target   : s3://{S3_BUCKET}/raw/ratings/date={export_date}/")
    print("=" * 68)

    # ------------------------------------------------------------------
    # Step 1: Watermarks from S3 via DuckDB
    # ------------------------------------------------------------------
    print(f"\n  [INFO] Reading watermarks from S3 via DuckDB...")
    conn = get_duckdb_conn()
    watermark_map = get_watermarks(conn)
    conn.close()
    print(f"  [INFO] {len(watermark_map)} tickers with existing ratings.")

    if not watermark_map:
        print(
            "\n  No existing ratings found in S3. Run bootstrap_ratings_s3.py first.\n"
            "  Exiting."
        )
        return

    # ------------------------------------------------------------------
    # Step 2: Parallel fetch from yfinance
    # ------------------------------------------------------------------
    print(f"\n  [FETCH] Fetching ratings ({MAX_WORKERS} workers)...")

    fetch_results = {}
    fetch_errors  = []
    tickers = list(watermark_map.keys())

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(fetch_ratings, t, watermark_map.get(t)): t
            for t in tickers
        }
        done = 0
        for future in as_completed(futures):
            ticker, df = future.result()
            done += 1
            if df is None:
                fetch_errors.append(ticker)
            else:
                fetch_results[ticker] = df

            if done % 100 == 0:
                new_rows = sum(len(v) for v in fetch_results.values() if not v.empty)
                print(f"  [PROGRESS] {done}/{len(tickers)}  new rows so far: {new_rows:,}")

    all_new = [df for df in fetch_results.values() if not df.empty]
    tickers_no_new = sum(1 for df in fetch_results.values() if df.empty)

    print(f"  [FETCH] Done. {len(fetch_errors)} errors, {tickers_no_new} tickers with no new ratings.")

    if not all_new:
        print("\n  No new ratings data to write.")
        elapsed = (datetime.now() - start_time).seconds
        print(f"  Elapsed: {elapsed}s\n  DONE.\n")
        return

    # ------------------------------------------------------------------
    # Step 3: Upload combined new rows to S3
    # ------------------------------------------------------------------
    combined = pd.concat(all_new, ignore_index=True)
    print(f"\n  [WRITE] {len(combined):,} new ratings rows across {combined['Ticker'].nunique()} tickers.")

    if dry_run:
        print("  [DRY RUN] Skipping S3 upload. Sample:")
        print(combined.head(10).to_string(index=False))
        return

    key = upload_partition(combined, export_date)
    print(f"  [WRITE] Done -> s3://{S3_BUCKET}/{key}")

    _publish({
        "RatingsRowsWritten"    : len(combined),
        "RatingsTickersUpdated" : len(all_new),
    })

    # ------------------------------------------------------------------
    # Step 4: Summary
    # ------------------------------------------------------------------
    elapsed = (datetime.now() - start_time).seconds
    print()
    print("=" * 68)
    print("  UPDATE SUMMARY")
    print("=" * 68)
    print(f"  Tickers in S3              : {len(tickers)}")
    print(f"  Tickers with new ratings   : {len(all_new)}")
    print(f"  Tickers with no new data   : {tickers_no_new}")
    print(f"  Fetch errors               : {len(fetch_errors)}")
    print(f"  New rows written           : {len(combined):,}")
    print(f"  Elapsed time               : {elapsed}s")
    print("=" * 68)

    if fetch_errors:
        print(f"\n  Tickers with errors:")
        for t in fetch_errors[:20]:
            print(f"    - {t}")

    print("\n  DONE.\n")


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Incremental ratings updater — yfinance to S3, no SQL Server."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be written without uploading.")
    args = parser.parse_args()
    run_update(dry_run=args.dry_run)
