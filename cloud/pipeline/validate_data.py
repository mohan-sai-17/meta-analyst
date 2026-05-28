"""
validate_data.py
================
Data quality gate — runs AFTER compute_marts.py, BEFORE send_alerts.py.

Checks:
  1. All expected mart files exist in S3
  2. All mart files are non-empty (row count > 0)
  3. OHLC freshness — mart_daily_candidates must have prices no more than 3
     trading days stale
  4. No null/zero current_price in mart_daily_candidates

Exits with sys.exit(1) if any critical check (mart missing or 0-row) fails,
blocking the downstream send_alerts step in GitHub Actions.

Publishes DataQualityScore (0-100) to CloudWatch.

Usage:
    python aws/pipeline/validate_data.py
"""

import sys
import boto3
import pandas as pd
from io import BytesIO
from datetime import date, timedelta


# ============================================================================
# CONFIGURATION
# ============================================================================

S3_BUCKET  = "meta-analyst-data-lake-YOUR_AWS_ACCOUNT_ID"
AWS_REGION = "us-east-1"

EXPECTED_MARTS = [
    "mart/mart_firm_scorecard.parquet",
    "mart/mart_top_tier.parquet",
    "mart/mart_price_targets.parquet",
    "mart/mart_seasonality.parquet",
    "mart/mart_recent_signals.parquet",
    "mart/mart_daily_candidates.parquet",
    "mart/mart_sector_candidates.parquet",
]

# mart_daily_candidates is legitimately empty on weekends / quiet trading days
# (no Top Tier analyst upgrades in the last 3 days). Treat as WARNING, not CRITICAL.
OPTIONAL_EMPTY_MARTS = {"mart/mart_daily_candidates.parquet"}


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

def get_last_market_day():
    today = date.today()
    wd    = today.weekday()
    if wd == 5:   return today - timedelta(days=1)   # Saturday -> Friday
    elif wd == 6: return today - timedelta(days=2)   # Sunday   -> Friday
    else:         return today - timedelta(days=1)   # Weekday  -> yesterday


def load_parquet_from_s3(s3, key):
    buf = BytesIO()
    s3.download_fileobj(S3_BUCKET, key, buf)
    buf.seek(0)
    return pd.read_parquet(buf)


# ============================================================================
# CHECKS
# ============================================================================

def check_mart_exists(s3):
    """Check 1: All expected mart files are present in S3."""
    print("\n  [CHECK 1] Mart file existence in S3...")
    failures = []
    for key in EXPECTED_MARTS:
        try:
            s3.head_object(Bucket=S3_BUCKET, Key=key)
            print(f"    OK  : s3://{S3_BUCKET}/{key}")
        except Exception:
            print(f"    FAIL: s3://{S3_BUCKET}/{key} — NOT FOUND")
            failures.append(key)
    return failures


def check_mart_nonempty(s3):
    """Check 2: All mart files have at least 1 row.
    Optional-empty marts (e.g. mart_daily_candidates on weekends) produce
    a warning but do NOT count as critical failures.
    """
    print("\n  [CHECK 2] Mart file row counts...")
    failures = []
    for key in EXPECTED_MARTS:
        try:
            df = load_parquet_from_s3(s3, key)
            if len(df) == 0:
                if key in OPTIONAL_EMPTY_MARTS:
                    print(f"    WARN: {key} — 0 rows (expected on weekends / quiet days)")
                else:
                    print(f"    FAIL: {key} — 0 rows")
                    failures.append(key)
            else:
                print(f"    OK  : {key} — {len(df):,} rows")
        except Exception as e:
            print(f"    FAIL: {key} — could not load: {e}")
            failures.append(key)
    return failures


def check_ohlc_freshness(s3):
    """Check 3: mart_daily_candidates prices are no more than 3 trading days stale."""
    print("\n  [CHECK 3] OHLC freshness...")
    try:
        df = load_parquet_from_s3(s3, "mart/mart_daily_candidates.parquet")
    except Exception as e:
        print(f"    SKIP: Could not load mart_daily_candidates: {e}")
        return []

    if df.empty or "GradeDate" not in df.columns:
        print("    SKIP: mart_daily_candidates empty or missing GradeDate.")
        return []

    # Use GradeDate as a proxy for data recency
    try:
        max_date = pd.to_datetime(df["GradeDate"]).dt.date.max()
    except Exception:
        print("    SKIP: Could not parse GradeDate column.")
        return []

    last_market_day = get_last_market_day()
    delta = (last_market_day - max_date).days

    if delta > 3:
        print(f"    WARN: Most recent GradeDate is {max_date} — {delta} days behind {last_market_day}")
        return [f"GradeDate stale by {delta} days"]
    else:
        print(f"    OK  : Most recent GradeDate is {max_date} ({delta} days behind {last_market_day})")
        return []


def check_no_null_prices(s3):
    """Check 4: mart_daily_candidates has no null or zero current_price."""
    print("\n  [CHECK 4] Null/zero current_price check...")
    try:
        df = load_parquet_from_s3(s3, "mart/mart_daily_candidates.parquet")
    except Exception as e:
        print(f"    SKIP: Could not load mart_daily_candidates: {e}")
        return []

    if df.empty or "current_price" not in df.columns:
        print("    SKIP: mart_daily_candidates empty or missing current_price.")
        return []

    bad = df[df["current_price"].isna() | (df["current_price"] == 0)]
    if len(bad) > 0:
        print(f"    WARN: {len(bad)} row(s) with null/zero current_price")
        return [f"{len(bad)} null/zero current_price rows"]
    else:
        print(f"    OK  : All {len(df)} rows have valid current_price")
        return []


# ============================================================================
# MAIN
# ============================================================================

def main():
    print()
    print("=" * 68)
    print("  VALIDATE_DATA — Data Quality Gate")
    print(f"  Bucket : s3://{S3_BUCKET}")
    print("=" * 68)

    s3 = boto3.client("s3", region_name=AWS_REGION)

    # Run all checks
    fail_exists    = check_mart_exists(s3)
    fail_nonempty  = check_mart_nonempty(s3)
    warn_freshness = check_ohlc_freshness(s3)
    warn_nulls     = check_no_null_prices(s3)

    total_checks  = 4
    passing       = sum([
        len(fail_exists)   == 0,
        len(fail_nonempty) == 0,
        len(warn_freshness) == 0,
        len(warn_nulls)    == 0,
    ])
    score = int(passing / total_checks * 100)

    # Summary
    critical_failed = bool(fail_exists or fail_nonempty)

    print()
    print("=" * 68)
    print("  DATA QUALITY SUMMARY")
    print("=" * 68)
    print(f"  Checks passed   : {passing}/{total_checks}")
    print(f"  Quality score   : {score}/100")
    if fail_exists:
        print(f"  CRITICAL FAIL   : {len(fail_exists)} mart file(s) missing")
    if fail_nonempty:
        print(f"  CRITICAL FAIL   : {len(fail_nonempty)} mart file(s) empty")
    if warn_freshness:
        print(f"  WARNING         : OHLC freshness — {warn_freshness[0]}")
    if warn_nulls:
        print(f"  WARNING         : Price nulls — {warn_nulls[0]}")
    if not critical_failed and not warn_freshness and not warn_nulls:
        print("  All checks PASSED.")
    print("=" * 68)
    print()

    _publish({"DataQualityScore": score})

    if critical_failed:
        print("[VALIDATE] CRITICAL failures detected — blocking pipeline.")
        sys.exit(1)

    print("[VALIDATE] Data quality OK — pipeline may continue.")


if __name__ == "__main__":
    main()
