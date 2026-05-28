"""
update_fundamentals_s3.py
==========================
Fetch fundamental financial health metrics via yfinance for all tickers.
Writes a flat Parquet to S3: raw/fundamentals/date=YYYY-MM-DD/fundamentals.parquet

Raw metrics stored (gate logic lives in mart_fundamental_health.sql):
  - debt_equity       : Total Debt / Stockholders Equity (most recent annual)
  - current_ratio     : Current Assets / Current Liabilities
  - interest_coverage : EBIT / |Interest Expense|  (null if no interest expense)
  - fcf_yr0/1/2       : Free Cash Flow for most recent 3 annual periods
  - ni_yr0/1          : Net Income for most recent 2 annual periods
  - fetch_error       : Non-null if yfinance raised an exception

Watermark: skips today's run if today's file already exists in S3.
           Use --force to override.

Usage:
    python update_fundamentals_s3.py
    python update_fundamentals_s3.py --dry-run   # validate 3 tickers, no writes
    python update_fundamentals_s3.py --force      # re-fetch even if file exists today
"""

import os
import sys
import time
import warnings
import argparse
import boto3
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
MAX_WORKERS = 5   # yfinance financial fetches are heavier than OHLC; keep low


# ============================================================================
# YFINANCE HELPERS
# ============================================================================

def _get_row(df: pd.DataFrame, keys: list):
    """Try multiple row label variants; return the first found Series or None."""
    if df is None or df.empty:
        return None
    for k in keys:
        if k in df.index:
            return df.loc[k]
    return None


def _val(series: pd.Series, idx: int = 0):
    """Safely extract the idx-th value (by column position) from a row Series."""
    if series is None:
        return None
    try:
        if idx >= len(series):
            return None
        val = series.iloc[idx]
        if pd.isna(val):
            return None
        return float(val)
    except Exception:
        return None


def fetch_fundamentals(ticker: str) -> dict:
    """
    Pull annual financial statements from yfinance and return raw ratio columns.
    All computation of pass/fail gates happens later in mart_fundamental_health.sql.
    """
    result = {
        "ticker"       : ticker,
        "as_of_date"   : date.today().isoformat(),
        "debt_equity"  : None,
        "current_ratio": None,
        "interest_coverage": None,
        "fcf_yr0"      : None,
        "fcf_yr1"      : None,
        "fcf_yr2"      : None,
        "ni_yr0"       : None,
        "ni_yr1"       : None,
        "fetch_error"  : None,
    }

    try:
        t  = yf.Ticker(ticker)
        bs = t.balance_sheet   # index=items, columns=dates newest-first
        fi = t.financials      # income statement
        cf = t.cashflow        # cash flow statement

        # --- Debt / Equity ---
        debt_row   = _get_row(bs, [
            "Total Debt", "Long Term Debt", "LongTermDebt",
            "Net Debt",
        ])
        equity_row = _get_row(bs, [
            "Stockholders Equity", "Total Stockholders Equity",
            "Common Stock Equity", "Total Equity Gross Minority Interest",
        ])
        debt   = _val(debt_row,   0)
        equity = _val(equity_row, 0)
        if debt is not None and equity is not None and equity != 0:
            result["debt_equity"] = round(abs(debt) / abs(equity), 4)

        # --- Current Ratio ---
        ca_row = _get_row(bs, ["Current Assets",       "Total Current Assets"])
        cl_row = _get_row(bs, ["Current Liabilities",  "Total Current Liabilities"])
        ca = _val(ca_row, 0)
        cl = _val(cl_row, 0)
        if ca is not None and cl is not None and cl != 0:
            result["current_ratio"] = round(ca / cl, 4)

        # --- Interest Coverage ---
        ebit_row = _get_row(fi, [
            "EBIT", "Operating Income",
            "Normalized EBITDA", "Operating Income Or Loss",
        ])
        int_row  = _get_row(fi, [
            "Interest Expense", "Interest Expense Non Operating",
            "Net Interest Income", "Interest And Debt Expense",
        ])
        ebit     = _val(ebit_row, 0)
        interest = _val(int_row,  0)
        # yfinance reports interest expense as a negative number (it's a cost).
        # Guard: only compute if interest is non-trivially non-zero.
        if ebit is not None and interest is not None and abs(interest) > 1000:
            result["interest_coverage"] = round(ebit / abs(interest), 4)

        # --- Free Cash Flow (3 most recent annual periods) ---
        fcf_row = _get_row(cf, ["Free Cash Flow", "FreeCashFlow"])
        if fcf_row is not None:
            result["fcf_yr0"] = _val(fcf_row, 0)
            result["fcf_yr1"] = _val(fcf_row, 1)
            result["fcf_yr2"] = _val(fcf_row, 2)

        # --- Net Income (2 most recent annual periods) ---
        ni_row = _get_row(fi, [
            "Net Income", "Net Income Common Stockholders",
            "Net Income From Continuing Operation Net Minority Interest",
            "Net Income Applicable To Common Shares",
        ])
        if ni_row is not None:
            result["ni_yr0"] = _val(ni_row, 0)
            result["ni_yr1"] = _val(ni_row, 1)

    except Exception as e:
        result["fetch_error"] = str(e)[:300]

    return result


# ============================================================================
# S3 HELPERS
# ============================================================================

def get_tickers_from_s3() -> list:
    """Read ticker list from the stocks_list raw Parquet in S3."""
    s3   = boto3.client("s3", region_name=AWS_REGION)
    resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix="raw/stocks_list/")
    objects = sorted(
        [o for o in resp.get("Contents", []) if o["Key"].endswith(".parquet")],
        key=lambda o: o["Key"],
    )
    if not objects:
        raise RuntimeError("No stocks_list Parquet found in S3.")
    frames = []
    for obj in objects:
        buf = BytesIO()
        s3.download_fileobj(S3_BUCKET, obj["Key"], buf)
        buf.seek(0)
        frames.append(pd.read_parquet(buf))
    df = pd.concat(frames, ignore_index=True)
    return df["Ticker"].dropna().unique().tolist()


def watermark_exists(export_date: str) -> bool:
    """Return True if today's fundamentals file is already in S3."""
    s3  = boto3.client("s3", region_name=AWS_REGION)
    key = f"raw/fundamentals/date={export_date}/fundamentals.parquet"
    try:
        s3.head_object(Bucket=S3_BUCKET, Key=key)
        return True
    except Exception:
        return False


def upload_parquet(df: pd.DataFrame, export_date: str) -> str:
    key = f"raw/fundamentals/date={export_date}/fundamentals.parquet"
    buf = BytesIO()
    df.to_parquet(buf, index=False, engine="pyarrow", compression="snappy")
    buf.seek(0)
    s3 = boto3.client("s3", region_name=AWS_REGION)
    s3.put_object(Bucket=S3_BUCKET, Key=key, Body=buf.read())
    return key


# ============================================================================
# MAIN
# ============================================================================

def run_update(dry_run=False, force=False):
    start_time  = datetime.now()
    export_date = date.today().isoformat()
    mode_label  = " [DRY RUN]" if dry_run else ""

    print()
    print("=" * 68)
    print(f"  UPDATE_FUNDAMENTALS_S3 — Financial Health Metrics{mode_label}")
    print(f"  Started : {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Output  : s3://{S3_BUCKET}/raw/fundamentals/date={export_date}/")
    print("=" * 68)

    if not force and not dry_run and watermark_exists(export_date):
        print(f"\n  Watermark exists for {export_date}. Use --force to re-fetch.")
        print("  Nothing to do. Exiting cleanly.\n")
        return

    print(f"\n  [INFO] Loading ticker list from S3...")
    tickers = get_tickers_from_s3()
    print(f"  [INFO] {len(tickers)} tickers to process.")

    if dry_run:
        print(f"\n  [DRY RUN] Would fetch fundamentals for {len(tickers)} tickers.")
        print("  Sampling 3 tickers to validate connectivity...")
        for t in tickers[:3]:
            r = fetch_fundamentals(t)
            print(f"    {t:<8} DE={r['debt_equity']}  CR={r['current_ratio']}  "
                  f"IC={r['interest_coverage']}  FCF={r['fcf_yr0']}  "
                  f"NI={r['ni_yr0']}  err={r['fetch_error']}")
        print("\n  Dry run complete. No S3 writes.\n")
        return

    print(f"\n  [FETCH] Fetching financials ({MAX_WORKERS} workers)...")
    results = []
    errors  = []
    done    = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(fetch_fundamentals, t): t for t in tickers}
        for future in as_completed(futures):
            rec = future.result()
            if rec["fetch_error"]:
                errors.append(rec["ticker"])
            results.append(rec)
            done += 1
            if done % 50 == 0:
                print(f"    {done}/{len(tickers)} processed | {len(errors)} errors so far")

    df = pd.DataFrame(results)

    # Summary stats
    has_de  = df["debt_equity"].notna().sum()
    has_cr  = df["current_ratio"].notna().sum()
    has_fcf = df["fcf_yr0"].notna().sum()

    print(f"\n  [STATS]")
    print(f"    Total tickers        : {len(df)}")
    print(f"    With Debt/Equity     : {has_de}")
    print(f"    With Current Ratio   : {has_cr}")
    print(f"    With FCF data        : {has_fcf}")
    print(f"    Fetch errors         : {len(errors)}")
    if errors:
        print(f"    Error tickers        : {errors[:10]}{'...' if len(errors)>10 else ''}")

    print(f"\n  [WRITE] Uploading to S3...")
    key = upload_parquet(df, export_date)
    print(f"  [WRITE] Done -> s3://{S3_BUCKET}/{key}")

    elapsed = (datetime.now() - start_time).seconds
    print(f"\n  Elapsed : {elapsed}s")
    print("=" * 68)
    print()


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Fetch yfinance financial health metrics -> S3."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate connectivity for 3 tickers, no S3 writes.")
    parser.add_argument("--force", action="store_true",
                        help="Re-fetch even if today's file already exists.")
    args = parser.parse_args()
    run_update(dry_run=args.dry_run, force=args.force)
