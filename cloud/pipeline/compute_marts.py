"""
compute_marts.py
=================
Builds mart Parquet files using DuckDB SQL transforms.

Strategy (reliable, avoids S3-httpfs multi-file schema issues):
  1.  Download the latest raw Parquet files from S3 to a local temp dir.
  2.  Run DuckDB against LOCAL files (no httpfs) — deterministic, fast.
  3.  Upload mart result Parquet files back to S3.

Execution order (dependency chain in sql/):
  stg_ohlc, stg_ratings, stg_stocks (views -> local Parquet files)
    └─ mart_firm_scorecard
         └─ mart_top_tier
              ├─ mart_price_targets    (also needs stg_ohlc)
              ├─ mart_seasonality      (needs stg_ohlc only)
              ├─ mart_recent_signals   (needs stg_ratings + stg_ohlc)
              └─ mart_daily_candidates (needs all stg_* + mart_price_targets + mart_seasonality)

Usage:
    python compute_marts.py
    python compute_marts.py --dry-run   # validate SQL, skip S3 upload
"""

import os
import sys
import argparse
import tempfile
import warnings
import boto3
import duckdb
import pandas as pd
import yfinance as yf
from io import BytesIO
from datetime import datetime, date

_SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
# Lambda bundles SQL files at /var/task/sql/; local dev uses aws/sql/
# SQL_ROOT env var overrides both so the path always resolves correctly.
_SQL_ROOT = os.environ.get("SQL_ROOT", _PROJECT_ROOT)
warnings.filterwarnings("ignore")


# ============================================================================
# CONFIGURATION
# ============================================================================

S3_BUCKET  = "meta-analyst-data-lake-YOUR_AWS_ACCOUNT_ID"
AWS_REGION = "us-east-1"

# Execution order matters — each mart may depend on the previous
MART_ORDER = [
    "mart_firm_scorecard",
    "mart_top_tier",
    "mart_price_targets",
    "mart_sector_candidates",    # needs price_targets + stg_stocks.Sector
    "mart_seasonality",
    "mart_recent_signals",
    "mart_fundamental_health",   # Phase 1 — financial health gate (yfinance)
    "mart_insider_summary",      # Phase 3 — EDGAR Form 4 insider signals
    "mart_daily_candidates",     # needs all of the above
]

# Raw tables to download: {local_name: S3_key_prefix}
# All Parquet files under each prefix are downloaded and merged.
RAW_TABLES = {
    "ohlc"        : "raw/ohlc/",
    "ratings"     : "raw/ratings/",
    "stocks_list" : "raw/stocks_list/",
}

# Optional tables: gracefully skipped if not yet present in S3.
# Will be missing on first pipeline run before update_fundamentals_s3.py
# and update_insider_s3.py have been executed.
OPTIONAL_RAW_TABLES = {
    "fundamentals": "raw/fundamentals/",
    "insider"     : "raw/insider/",
}


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
# EARNINGS FLAG POST-PROCESSING
# ============================================================================

def add_earnings_flag(conn, dry_run):
    """Fetch upcoming earnings dates for candidates, add near_earnings column."""
    print("\n  [EARNINGS] Fetching earnings calendar for candidates...")
    try:
        df = conn.execute("SELECT * FROM mart_daily_candidates").fetchdf()
    except Exception as e:
        print(f"  [EARNINGS] Could not load mart_daily_candidates: {e}")
        return

    if df.empty:
        print("  [EARNINGS] mart_daily_candidates is empty — skipping.")
        return

    tickers  = df["Ticker"].unique().tolist()
    today    = date.today()
    days_map = {}

    for ticker in tickers:
        try:
            cal = yf.Ticker(ticker).calendar
            if cal is not None and not cal.empty and "Earnings Date" in cal.index:
                e_date = pd.to_datetime(cal.loc["Earnings Date"].iloc[0]).date()
                days_map[ticker] = (e_date - today).days
        except Exception:
            pass
        days_map.setdefault(ticker, None)

    df["days_to_earnings"] = df["Ticker"].map(days_map)
    df["near_earnings"]    = df["days_to_earnings"].apply(
        lambda x: abs(x) <= 5 if x is not None else False
    )

    near_count = int(df["near_earnings"].sum())
    print(f"  [EARNINGS] {near_count} candidate(s) flagged as near_earnings (within 5 days).")

    # Re-register as DuckDB table
    conn.execute("DROP TABLE mart_daily_candidates")
    conn.register("df", df)
    conn.execute("CREATE TABLE mart_daily_candidates AS SELECT * FROM df")
    conn.unregister("df")

    if not dry_run:
        buf = BytesIO()
        df.to_parquet(buf, index=False, engine="pyarrow", compression="snappy")
        buf.seek(0)
        s3 = boto3.client("s3", region_name=AWS_REGION)
        s3.put_object(Bucket=S3_BUCKET, Key="mart/mart_daily_candidates.parquet", Body=buf.read())
        print(f"  [EARNINGS] Re-uploaded mart_daily_candidates with near_earnings column.")
    else:
        print("  [EARNINGS] [DRY RUN] Skipping S3 re-upload.")


# ============================================================================
# STEP 1: DOWNLOAD LATEST RAW PARQUET FROM S3
# ============================================================================

def download_merged_parquet(s3_client, prefix, local_path):
    """
    Download ALL Parquet files under prefix, merge into a single local file.

    Replaces the old 'pick largest file' approach — after bootstrap + daily
    incremental updates, multiple partition files exist and all must be merged.
    Uses pandas concat so stg_ohlc/stg_ratings deduplication handles overlaps.
    """
    resp = s3_client.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix)
    objects = sorted(
        [o for o in resp.get("Contents", []) if o["Key"].endswith(".parquet")],
        key=lambda o: o["Key"]
    )
    if not objects:
        raise RuntimeError(f"No Parquet files found under s3://{S3_BUCKET}/{prefix}")

    frames      = []
    total_bytes = 0
    for obj in objects:
        buf = BytesIO()
        s3_client.download_fileobj(S3_BUCKET, obj["Key"], buf)
        buf.seek(0)
        frames.append(pd.read_parquet(buf))
        total_bytes += obj["Size"]

    merged = pd.concat(frames, ignore_index=True)

    # Normalize GradeDate — bootstrap stores datetime64, daily updates store datetime.date;
    # mixed types cause pyarrow schema errors. Coerce everything to datetime64[ns].
    if "GradeDate" in merged.columns:
        merged["GradeDate"] = pd.to_datetime(merged["GradeDate"], errors="coerce")

    merged.to_parquet(local_path, index=False, engine="pyarrow", compression="snappy")
    print(f"    {len(objects)} file(s), {total_bytes/1e6:.1f} MB merged -> {local_path}")
    return local_path


def download_raw_parquet(tmp_dir):
    """Download and merge all raw Parquet partitions for each table to tmp_dir."""
    s3 = boto3.client("s3", region_name=AWS_REGION)
    paths = {}

    # Required tables — raise on failure
    for table, prefix in RAW_TABLES.items():
        local_path = os.path.join(tmp_dir, f"{table}.parquet")
        print(f"  [{table.upper()}] s3://{S3_BUCKET}/{prefix}*")
        download_merged_parquet(s3, prefix, local_path)
        paths[table] = local_path

    # Optional tables — skip gracefully if not yet in S3 (first pipeline run)
    for table, prefix in OPTIONAL_RAW_TABLES.items():
        local_path = os.path.join(tmp_dir, f"{table}.parquet")
        print(f"  [{table.upper()}] s3://{S3_BUCKET}/{prefix}*  (optional)")
        try:
            download_merged_parquet(s3, prefix, local_path)
            paths[table] = local_path
        except RuntimeError as e:
            print(f"  [{table.upper()}] Skipping — not yet in S3: {e}")

    return paths


# ============================================================================
# STEP 2: CREATE DuckDB VIEWS AGAINST LOCAL FILES
# ============================================================================

def create_source_views(conn, paths):
    """
    Create stg_ohlc and stg_ratings views pointing to local Parquet files.
    Reading from local files avoids S3-httpfs multi-partition schema issues.
    """
    ohlc_path    = paths["ohlc"].replace("\\", "/")
    ratings_path = paths["ratings"].replace("\\", "/")

    conn.execute(f"""
        CREATE VIEW stg_ohlc AS
        SELECT Ticker, Date, Open, High, Low, Close, Volume
        FROM (
            SELECT
                Ticker::VARCHAR  AS Ticker,
                Date::DATE       AS Date,
                Open::DOUBLE     AS Open,
                High::DOUBLE     AS High,
                Low::DOUBLE      AS Low,
                Close::DOUBLE    AS Close,
                Volume::BIGINT   AS Volume,
                ROW_NUMBER() OVER (
                    PARTITION BY Ticker::VARCHAR, Date::DATE
                    ORDER BY Close::DOUBLE
                ) AS _rn
            FROM read_parquet('{ohlc_path}')
            WHERE Ticker IS NOT NULL
              AND Date   IS NOT NULL
              AND Close  IS NOT NULL
              AND Close  > 0
        ) sub
        WHERE _rn = 1
    """)

    conn.execute(f"""
        CREATE VIEW stg_ratings AS
        SELECT DISTINCT
            Ticker::VARCHAR             AS Ticker,
            TRY_CAST(GradeDate AS DATE) AS GradeDate,
            Firm::VARCHAR               AS Firm,
            Action::VARCHAR             AS Action,
            ToGrade::VARCHAR            AS ToGrade,
            FromGrade::VARCHAR          AS FromGrade
        FROM read_parquet('{ratings_path}')
        WHERE Action IN ('up', 'init')
          AND Ticker    IS NOT NULL
          AND GradeDate IS NOT NULL
          AND Firm      IS NOT NULL
    """)

    stocks_path = paths["stocks_list"].replace("\\", "/")
    conn.execute(f"""
        CREATE VIEW stg_stocks AS
        SELECT
            Ticker::VARCHAR                              AS Ticker,
            Stock_Name::VARCHAR                         AS Stock_Name,
            Friends::VARCHAR                            AS Friends,
            COALESCE(TRY_CAST(Sector AS VARCHAR), 'Unknown') AS Sector
        FROM read_parquet('{stocks_path}')
        WHERE Ticker IS NOT NULL
    """)

    # Validate — exit early if empty
    n_ohlc    = conn.execute("SELECT count(*) FROM stg_ohlc").fetchone()[0]
    n_ratings = conn.execute("SELECT count(*) FROM stg_ratings").fetchone()[0]
    print(f"  [INFO] stg_ohlc rows    : {n_ohlc:,}")
    print(f"  [INFO] stg_ratings rows : {n_ratings:,}")
    if n_ohlc == 0 or n_ratings == 0:
        raise RuntimeError("Source view(s) returned 0 rows — check S3 data.")
    print("  [INFO] Source views OK.")

    # --- stg_fundamentals (optional) ---
    if "fundamentals" in paths:
        fund_path = paths["fundamentals"].replace("\\", "/")
        conn.execute(f"""
            CREATE VIEW stg_fundamentals AS
            SELECT * EXCLUDE (_rn)
            FROM (
                SELECT *,
                    ROW_NUMBER() OVER (
                        PARTITION BY ticker
                        ORDER BY as_of_date DESC
                    ) AS _rn
                FROM read_parquet('{fund_path}')
                WHERE ticker IS NOT NULL
            ) sub
            WHERE _rn = 1
        """)
        n = conn.execute("SELECT count(*) FROM stg_fundamentals").fetchone()[0]
        print(f"  [INFO] stg_fundamentals rows : {n:,}")
    else:
        conn.execute("""
            CREATE VIEW stg_fundamentals AS
            SELECT
                NULL::VARCHAR AS ticker,
                NULL::DATE    AS as_of_date,
                NULL::DOUBLE  AS debt_equity,
                NULL::DOUBLE  AS current_ratio,
                NULL::DOUBLE  AS interest_coverage,
                NULL::DOUBLE  AS fcf_yr0,
                NULL::DOUBLE  AS fcf_yr1,
                NULL::DOUBLE  AS fcf_yr2,
                NULL::DOUBLE  AS ni_yr0,
                NULL::DOUBLE  AS ni_yr1,
                NULL::VARCHAR AS fetch_error
            WHERE 1=0
        """)
        print("  [INFO] stg_fundamentals : skipped (raw/fundamentals/ not yet in S3)")

    # --- stg_insider (optional) ---
    if "insider" in paths:
        insider_path = paths["insider"].replace("\\", "/")
        conn.execute(f"""
            CREATE VIEW stg_insider AS
            SELECT * EXCLUDE (_rn)
            FROM (
                SELECT *,
                    ROW_NUMBER() OVER (
                        PARTITION BY ticker
                        ORDER BY as_of_date DESC
                    ) AS _rn
                FROM read_parquet('{insider_path}')
                WHERE ticker IS NOT NULL
            ) sub
            WHERE _rn = 1
        """)
        n = conn.execute("SELECT count(*) FROM stg_insider").fetchone()[0]
        print(f"  [INFO] stg_insider rows      : {n:,}")
    else:
        conn.execute("""
            CREATE VIEW stg_insider AS
            SELECT
                NULL::VARCHAR  AS ticker,
                NULL::DATE     AS as_of_date,
                NULL::DOUBLE   AS net_insider_buy_90d,
                NULL::INTEGER  AS insider_buy_count_90d,
                NULL::INTEGER  AS insider_sell_count_90d,
                NULL::VARCHAR  AS last_transaction_date,
                NULL::BOOLEAN  AS insider_buy_flag,
                NULL::VARCHAR  AS fetch_error
            WHERE 1=0
        """)
        print("  [INFO] stg_insider           : skipped (raw/insider/ not yet in S3)")


# ============================================================================
# STEP 3: RUN MART SQL + UPLOAD
# ============================================================================

def run_mart(conn, name, dry_run=False):
    """
    Execute sql/{name}.sql, materialise as a DuckDB table,
    then upload as mart/{name}.parquet to S3.
    """
    sql_path = os.path.join(_SQL_ROOT, "sql", f"{name}.sql")
    sql      = open(sql_path).read()

    print(f"\n  [{name.upper()}]")
    print(f"  Running  : {sql_path}")

    try:
        conn.execute(f"CREATE TABLE {name} AS\n{sql}")
    except Exception as e:
        print(f"  ERROR    : SQL execution failed — {e}")
        sys.exit(1)

    row_count = conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
    print(f"  Rows     : {row_count:,}")

    if dry_run:
        print("  [DRY RUN] Skipping S3 upload.")
        return

    df  = conn.execute(f"SELECT * FROM {name}").fetchdf()
    key = f"mart/{name}.parquet"

    buf = BytesIO()
    df.to_parquet(buf, index=False, engine="pyarrow", compression="snappy")
    buf.seek(0)

    s3 = boto3.client("s3", region_name=AWS_REGION)
    s3.put_object(Bucket=S3_BUCKET, Key=key, Body=buf.read())
    print(f"  Written  : s3://{S3_BUCKET}/{key}")


# ============================================================================
# MAIN
# ============================================================================

def run_compute(dry_run=False):
    start_time = datetime.now()
    mode_label = " [DRY RUN]" if dry_run else ""

    print()
    print("=" * 68)
    print(f"  COMPUTE_MARTS — DuckDB SQL Transforms{mode_label}")
    print(f"  Started  : {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Source   : s3://{S3_BUCKET}/raw/  (downloaded locally)")
    print(f"  Output   : s3://{S3_BUCKET}/mart/")
    print("=" * 68)

    with tempfile.TemporaryDirectory() as tmp_dir:
        print(f"\n  [DOWNLOAD] Fetching raw Parquet files to {tmp_dir}...")
        paths = download_raw_parquet(tmp_dir)

        conn = duckdb.connect()
        print("\n  [VIEWS] Creating source views from local files...")
        create_source_views(conn, paths)

        for mart_name in MART_ORDER:
            run_mart(conn, mart_name, dry_run=dry_run)

        # Post-processing: add earnings proximity flag to mart_daily_candidates
        add_earnings_flag(conn, dry_run)

        # Row count for metrics
        try:
            candidates_built = conn.execute(
                "SELECT count(*) FROM mart_daily_candidates"
            ).fetchone()[0]
        except Exception:
            candidates_built = 0

        conn.close()
    # tmp_dir auto-cleaned here

    elapsed = (datetime.now() - start_time).seconds
    print()
    print("=" * 68)
    print(f"  DONE. {len(MART_ORDER)} mart tables built in {elapsed}s.")
    print("=" * 68)
    print()

    _publish({
        "MartDurationSeconds" : elapsed,
        "DailyCandidatesBuilt": candidates_built,
    })


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Build mart Parquet files from S3 raw data using DuckDB."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Run SQL and validate row counts, skip S3 upload.")
    args = parser.parse_args()
    run_compute(dry_run=args.dry_run)
