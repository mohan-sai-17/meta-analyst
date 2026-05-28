"""
update_ohlc_s3.py
==================
Daily Incremental OHLC Update — yfinance → S3.

1. Reads watermarks (MAX Date per ticker) from existing S3 Parquet using DuckDB.
2. Fetches only missing candles from yfinance (20 parallel threads).
3. Writes new candles as a new daily partition to S3.

No SQL Server. No BigQuery. No Athena. Just S3 + DuckDB + yfinance.

Usage:
    python update_ohlc_s3.py           # update all tickers
    python update_ohlc_s3.py --dry-run # show what would be fetched, no writes
"""

import sys
import warnings
import argparse
import boto3
import duckdb
import pandas as pd
import yfinance as yf
from io import BytesIO
from datetime import date, timedelta, datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

warnings.filterwarnings("ignore")


# ============================================================================
# CONFIGURATION
# ============================================================================

S3_BUCKET   = "meta-analyst-data-lake-YOUR_AWS_ACCOUNT_ID"
AWS_REGION  = "us-east-1"
MAX_WORKERS = 20


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
        print(f"[METRICS] Published: {metrics}")
    except Exception as e:
        print(f"[METRICS] CloudWatch publish skipped: {e}")


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
    """Read MAX(Date) per ticker from all ohlc S3 partitions via DuckDB."""
    df = conn.execute(f"""
        SELECT Ticker, MAX(Date)::DATE AS last_date
        FROM read_parquet('s3://{S3_BUCKET}/raw/ohlc/*/*.parquet')
        GROUP BY Ticker
    """).fetchdf()
    df["last_date"] = pd.to_datetime(df["last_date"]).dt.date
    return dict(zip(df["Ticker"], df["last_date"]))


def get_last_market_day():
    """Most recent completed trading day — always yesterday or earlier."""
    today   = date.today()
    weekday = today.weekday()
    if weekday == 5:    return today - timedelta(days=1)   # Saturday → Friday
    elif weekday == 6:  return today - timedelta(days=2)   # Sunday   → Friday
    else:               return today - timedelta(days=1)   # Weekday  → yesterday


def fetch_new_candles(ticker, last_date):
    """Fetch candles for `ticker` from day after last_date onward."""
    try:
        start = (last_date + timedelta(days=1)).strftime("%Y-%m-%d")
        hist  = yf.Ticker(ticker).history(start=start, auto_adjust=True)
        if hist.empty:
            return ticker, pd.DataFrame()
        hist = hist.reset_index()[["Date", "Open", "High", "Low", "Close", "Volume"]]
        hist["Date"] = pd.to_datetime(hist["Date"]).dt.tz_localize(None).dt.date
        hist.insert(0, "Ticker", ticker)
        return ticker, hist
    except Exception:
        return ticker, None


def upload_partition(df, export_date):
    """Write new candles as a date-partitioned Parquet file in S3."""
    key = f"raw/ohlc/date={export_date}/ohlc_update.parquet"
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
    print(f"  UPDATE_OHLC_S3 — Daily Incremental Update{mode_label}")
    print(f"  Started  : {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Target   : s3://{S3_BUCKET}/raw/ohlc/date={export_date}/")
    print("=" * 68)

    # ------------------------------------------------------------------
    # Step 1: Watermarks from S3 via DuckDB
    # ------------------------------------------------------------------
    print(f"\n  [INFO] Reading watermarks from S3 via DuckDB...")
    conn = get_duckdb_conn()
    watermark_map = get_watermarks(conn)
    conn.close()
    print(f"  [INFO] {len(watermark_map)} tickers found.")

    # ------------------------------------------------------------------
    # Step 2: Identify stale tickers
    # ------------------------------------------------------------------
    last_market_day = get_last_market_day()
    print(f"  [INFO] Last completed market day : {last_market_day}")

    stale = {t: d for t, d in watermark_map.items() if d < last_market_day}

    if not stale:
        print(f"\n  All {len(watermark_map)} tickers are already up to date.")
        print("  Nothing to do. Exiting cleanly.\n")
        return

    print(f"  [INFO] {len(stale)} tickers need updating.")
    print(f"  [INFO] {len(watermark_map) - len(stale)} tickers already current.\n")

    if dry_run:
        print("  Tickers that would be updated:")
        for t, d in sorted(stale.items()):
            print(f"    {t:<8} last_date={d}  missing since {d + timedelta(days=1)}")
        print("\n  Dry run complete. No writes performed.")
        return

    # ------------------------------------------------------------------
    # Step 3: Parallel fetch from yfinance
    # ------------------------------------------------------------------
    print(f"  [FETCH] Fetching new candles ({MAX_WORKERS} workers)...")

    fetch_results = {}
    fetch_errors  = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(fetch_new_candles, t, d): t
            for t, d in stale.items()
        }
        for future in as_completed(futures):
            ticker, df = future.result()
            if df is None:
                fetch_errors.append(ticker)
                print(f"  [ERROR] {ticker:<8} — yfinance fetch failed.")
            else:
                fetch_results[ticker] = df

    all_new = [df for df in fetch_results.values() if not df.empty]
    tickers_no_data = sum(1 for df in fetch_results.values() if df.empty)

    print(f"  [FETCH] Done. {len(fetch_errors)} errors, {tickers_no_data} returned no data.")

    if not all_new:
        print("\n  No new data to write (holiday or all tickers errored).")
        return

    # ------------------------------------------------------------------
    # Step 4: Upload combined new candles to S3
    # ------------------------------------------------------------------
    combined = pd.concat(all_new, ignore_index=True)
    print(f"\n  [WRITE] Uploading {len(combined):,} new rows to S3...")
    key = upload_partition(combined, export_date)
    print(f"  [WRITE] Done → s3://{S3_BUCKET}/{key}")

    # ------------------------------------------------------------------
    # Step 5: Summary
    # ------------------------------------------------------------------
    elapsed = (datetime.now() - start_time).seconds
    print()
    print("=" * 68)
    print("  UPDATE SUMMARY")
    print("=" * 68)
    print(f"  Tickers in S3              : {len(watermark_map)}")
    print(f"  Already up to date         : {len(watermark_map) - len(stale)}")
    print(f"  Needed update              : {len(stale)}")
    print(f"  Tickers with new data      : {len(all_new)}")
    print(f"  No new data (holiday/gap)  : {tickers_no_data}")
    print(f"  Fetch errors               : {len(fetch_errors)}")
    print(f"  New rows written           : {len(combined):,}")
    print(f"  Elapsed time               : {elapsed}s")
    print("=" * 68)

    if fetch_errors:
        print(f"\n  Tickers with errors:")
        for t in fetch_errors:
            print(f"    - {t}")

    print("\n  DONE.\n")

    _publish({
        "OHLCRowsWritten"    : len(combined),
        "OHLCTickersUpdated" : len(all_new),
    })


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Incremental OHLC updater — yfinance to S3, no SQL Server."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be fetched without writing.")
    args = parser.parse_args()
    run_update(dry_run=args.dry_run)
