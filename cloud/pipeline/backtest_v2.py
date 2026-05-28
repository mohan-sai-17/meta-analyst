"""
backtest_v2.py
==============
Unified walk-forward backtest — all hold periods × all leverages in one run.

For each test_year in 2015-2024:
  Train  : compute firm scorecard using GradeDate < {test_year}-01-01
  Derive : Top Tier firms (win_rate_6m > 50%, avg_ret_6m > 0, avg_ret_3m > 0)
  Test   : ONE DuckDB SQL query fetches entry + ALL 5 exit prices simultaneously
           -> Python then computes 5 hold periods × 5 leverages = 25 combos instantly

Speed trick: the slow part was a pandas row-by-row loop (O(trades) per year).
  Now DuckDB does the OHLC lookups in vectorised SQL, pandas just does arithmetic.

PnL accounting (losses fully deducted):
  dollar_pnl  = risk_usd * leverage * pct_return   (negative when stock falls)
  total_pnl   = SUM(dollar_pnl)                    (losses subtract from profits)
  strategy_ret = total_pnl / capital * 100

Output: mart/mart_backtest_v2.parquet
  columns: year, hold_days, leverage, top_tier_firms, trades,
           win_rate, avg_pct_return, strategy_return, spy_return, alpha

Usage:
    python aws/pipeline/backtest_v2.py
"""

import os
import warnings
import boto3
import duckdb
import pandas as pd
import yfinance as yf
from io import BytesIO

warnings.filterwarnings("ignore")


# ============================================================================
# CONFIGURATION
# ============================================================================

S3_BUCKET    = "meta-analyst-data-lake-YOUR_AWS_ACCOUNT_ID"
AWS_REGION   = "us-east-1"
TEST_YEARS   = list(range(2015, 2026))    # 2015–2025 inclusive
HOLD_PERIODS = [30, 60, 90, 120, 150]    # days to hold before selling
LEVERAGES    = [1, 2, 3, 4, 5]           # leverage multipliers
RISK_PCT     = 0.02                       # 2% of capital risked per trade
CAPITAL      = 15_000


# ============================================================================
# S3 DOWNLOAD
# ============================================================================

def _download_merged(s3, prefix, local_path, dedup_cols=None):
    resp    = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix)
    objects = sorted(
        [o for o in resp.get("Contents", []) if o["Key"].endswith(".parquet")],
        key=lambda o: o["Key"]
    )
    if not objects:
        raise RuntimeError(f"No Parquet files under s3://{S3_BUCKET}/{prefix}")
    frames = []
    for obj in objects:
        buf = BytesIO()
        s3.download_fileobj(S3_BUCKET, obj["Key"], buf)
        buf.seek(0)
        frames.append(pd.read_parquet(buf))
    merged = pd.concat(frames, ignore_index=True)
    if "GradeDate" in merged.columns:
        merged["GradeDate"] = pd.to_datetime(merged["GradeDate"], errors="coerce")
    # Deduplicate if requested — keep last (files are sorted oldest→newest,
    # so daily partitions override bootstrap with the most recently adjusted prices).
    if dedup_cols:
        merged = merged.drop_duplicates(subset=dedup_cols, keep="last")
    merged.to_parquet(local_path, index=False, engine="pyarrow", compression="snappy")
    return local_path


def download_raw(tmp_dir):
    s3    = boto3.client("s3", region_name=AWS_REGION)
    paths = {}
    for table, prefix, dedup in [
        ("ohlc",    "raw/ohlc/",     ["Ticker", "Date"]),
        ("ratings", "raw/ratings/",  None),
    ]:
        local = os.path.join(tmp_dir, f"{table}.parquet")
        print(f"  [{table.upper()}] downloading...")
        _download_merged(s3, prefix, local, dedup_cols=dedup)
        paths[table] = local
    return paths


# ============================================================================
# TRAINING PHASE — identify Top Tier firms before train_cut
# ============================================================================

def compute_top_tier(conn, test_year):
    """
    Run DuckDB SQL to score each firm on pre-test_year data.
    Returns a set of firm names qualifying as Top Tier.
    Criteria: win_rate_6m > 50%, avg_ret_6m > 0, avg_ret_3m > 0, signal_count >= 2.
    """
    train_cut = f"{test_year}-01-01"

    df = conn.execute(f"""
        WITH entry AS (
            SELECT r.Ticker, r.Firm, r.GradeDate, o.Close AS entry_price
            FROM stg_ratings r
            JOIN stg_ohlc o ON o.Ticker = r.Ticker AND o.Date >= r.GradeDate
            WHERE r.GradeDate < '{train_cut}'
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY r.Ticker, r.Firm, r.GradeDate ORDER BY o.Date
            ) = 1
        ),
        close_3m AS (
            SELECT r.Ticker, r.Firm, r.GradeDate, o.Close AS close_3m
            FROM stg_ratings r
            JOIN stg_ohlc o ON o.Ticker = r.Ticker
                AND o.Date >= r.GradeDate + INTERVAL 90 DAYS
            WHERE r.GradeDate < '{train_cut}'
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY r.Ticker, r.Firm, r.GradeDate ORDER BY o.Date
            ) = 1
        ),
        close_6m AS (
            SELECT r.Ticker, r.Firm, r.GradeDate, o.Close AS close_6m
            FROM stg_ratings r
            JOIN stg_ohlc o ON o.Ticker = r.Ticker
                AND o.Date >= r.GradeDate + INTERVAL 180 DAYS
            WHERE r.GradeDate < '{train_cut}'
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY r.Ticker, r.Firm, r.GradeDate ORDER BY o.Date
            ) = 1
        ),
        signals AS (
            SELECT e.Ticker, e.Firm, e.GradeDate, e.entry_price,
                   m3.close_3m, m6.close_6m
            FROM entry e
            LEFT JOIN close_3m m3 USING (Ticker, Firm, GradeDate)
            LEFT JOIN close_6m m6 USING (Ticker, Firm, GradeDate)
        )
        SELECT
            Firm,
            COUNT(*) AS signal_count,
            AVG(CASE WHEN close_6m IS NOT NULL
                     THEN (close_6m - entry_price) / entry_price * 100 END) AS avg_ret_6m,
            AVG(CASE WHEN close_3m IS NOT NULL
                     THEN (close_3m - entry_price) / entry_price * 100 END) AS avg_ret_3m,
            SUM(CASE WHEN close_6m > entry_price THEN 1 ELSE 0 END)::DOUBLE /
                NULLIF(COUNT(CASE WHEN close_6m IS NOT NULL THEN 1 END), 0) * 100
                AS win_rate_6m
        FROM signals
        GROUP BY Firm
        HAVING COUNT(*) >= 2
    """).fetchdf()

    top_tier = set(
        df[
            (df["win_rate_6m"] > 50) &
            (df["avg_ret_6m"]  > 0)  &
            (df["avg_ret_3m"]  > 0)
        ]["Firm"].tolist()
    )
    return top_tier


# ============================================================================
# SPY BENCHMARK — annual return for each test year
# ============================================================================

def get_spy_return(test_year):
    try:
        hist = yf.Ticker("SPY").history(
            start=f"{test_year}-01-01",
            end=f"{test_year + 1}-01-01",
            auto_adjust=True
        )
        if not hist.empty:
            return (
                float(hist["Close"].iloc[-1]) - float(hist["Close"].iloc[0])
            ) / float(hist["Close"].iloc[0]) * 100
    except Exception:
        pass
    return None


# ============================================================================
# TEST PHASE — one DuckDB query gets all 5 exit prices, Python does the math
# ============================================================================

def run_year(conn, test_year, top_tier_firms, spy_return):
    """
    For one test year:
      1. Materialise entry prices into a temp table (one row per signal).
      2. One SQL query with 5 LEFT JOINs retrieves all exit closes simultaneously.
      3. Python computes 5 hold_days x 5 leverages = 25 metric rows.
    Losses are fully deducted: total_pnl = SUM(dollar_pnl) across wins AND losses.
    """
    train_cut = f"{test_year}-01-01"
    test_end  = f"{test_year + 1}-01-01"
    n_tt      = len(top_tier_firms)

    # Zero rows if no Top Tier firms identified in training
    if not top_tier_firms:
        return [
            {
                "year": test_year, "hold_days": h, "leverage": lv,
                "top_tier_firms": 0, "trades": 0, "win_rate": None,
                "avg_pct_return": None, "strategy_return": None,
                "spy_return": spy_return, "alpha": None,
            }
            for h in HOLD_PERIODS for lv in LEVERAGES
        ]

    placeholders = ", ".join(
        f"'{f.replace(chr(39), chr(39)+chr(39))}'" for f in top_tier_firms
    )

    # Step 1: materialise test-window entry prices once
    conn.execute("DROP TABLE IF EXISTS _test_entry")
    conn.execute(f"""
        CREATE TEMP TABLE _test_entry AS
        SELECT r.Ticker, r.Firm, r.GradeDate,
               o.Date  AS entry_date,
               o.Open  AS entry_price
        FROM stg_ratings r
        JOIN stg_ohlc o ON o.Ticker = r.Ticker AND o.Date >= r.GradeDate
        WHERE r.GradeDate >= '{train_cut}'
          AND r.GradeDate <  '{test_end}'
          AND r.Firm IN ({placeholders})
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY r.Ticker, r.Firm, r.GradeDate ORDER BY o.Date
        ) = 1
    """)

    n_signals = conn.execute("SELECT COUNT(*) FROM _test_entry").fetchone()[0]
    if n_signals == 0:
        conn.execute("DROP TABLE IF EXISTS _test_entry")
        return [
            {
                "year": test_year, "hold_days": h, "leverage": lv,
                "top_tier_firms": n_tt, "trades": 0, "win_rate": None,
                "avg_pct_return": None, "strategy_return": None,
                "spy_return": spy_return, "alpha": None,
            }
            for h in HOLD_PERIODS for lv in LEVERAGES
        ]

    # Step 2: one query fetches all 5 exit closes in parallel LEFT JOINs
    test_df = conn.execute("""
        WITH
        x30 AS (
            SELECT e.Ticker, e.Firm, e.GradeDate, o.Close AS close_30
            FROM _test_entry e
            JOIN stg_ohlc o ON o.Ticker = e.Ticker
                AND o.Date >= e.entry_date + INTERVAL 30 DAYS
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY e.Ticker, e.Firm, e.GradeDate ORDER BY o.Date
            ) = 1
        ),
        x60 AS (
            SELECT e.Ticker, e.Firm, e.GradeDate, o.Close AS close_60
            FROM _test_entry e
            JOIN stg_ohlc o ON o.Ticker = e.Ticker
                AND o.Date >= e.entry_date + INTERVAL 60 DAYS
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY e.Ticker, e.Firm, e.GradeDate ORDER BY o.Date
            ) = 1
        ),
        x90 AS (
            SELECT e.Ticker, e.Firm, e.GradeDate, o.Close AS close_90
            FROM _test_entry e
            JOIN stg_ohlc o ON o.Ticker = e.Ticker
                AND o.Date >= e.entry_date + INTERVAL 90 DAYS
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY e.Ticker, e.Firm, e.GradeDate ORDER BY o.Date
            ) = 1
        ),
        x120 AS (
            SELECT e.Ticker, e.Firm, e.GradeDate, o.Close AS close_120
            FROM _test_entry e
            JOIN stg_ohlc o ON o.Ticker = e.Ticker
                AND o.Date >= e.entry_date + INTERVAL 120 DAYS
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY e.Ticker, e.Firm, e.GradeDate ORDER BY o.Date
            ) = 1
        ),
        x150 AS (
            SELECT e.Ticker, e.Firm, e.GradeDate, o.Close AS close_150
            FROM _test_entry e
            JOIN stg_ohlc o ON o.Ticker = e.Ticker
                AND o.Date >= e.entry_date + INTERVAL 150 DAYS
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY e.Ticker, e.Firm, e.GradeDate ORDER BY o.Date
            ) = 1
        )
        SELECT e.Ticker, e.Firm, e.GradeDate, e.entry_date, e.entry_price,
               x30.close_30, x60.close_60, x90.close_90, x120.close_120, x150.close_150
        FROM _test_entry e
        LEFT JOIN x30  USING (Ticker, Firm, GradeDate)
        LEFT JOIN x60  USING (Ticker, Firm, GradeDate)
        LEFT JOIN x90  USING (Ticker, Firm, GradeDate)
        LEFT JOIN x120 USING (Ticker, Firm, GradeDate)
        LEFT JOIN x150 USING (Ticker, Firm, GradeDate)
    """).fetchdf()

    conn.execute("DROP TABLE IF EXISTS _test_entry")

    # Step 3: pure Python/numpy — compute all 25 combinations
    risk_usd = CAPITAL * RISK_PCT
    results  = []

    for hold_days in HOLD_PERIODS:
        col = f"close_{hold_days}"
        sub = test_df[test_df[col].notna()].copy()

        if sub.empty:
            for leverage in LEVERAGES:
                results.append({
                    "year": test_year, "hold_days": hold_days, "leverage": leverage,
                    "top_tier_firms": n_tt, "trades": 0, "win_rate": None,
                    "avg_pct_return": None, "strategy_return": None,
                    "spy_return": spy_return, "alpha": None,
                })
            continue

        # pct_return is the same regardless of leverage
        sub["pct"] = (sub[col] - sub["entry_price"]) / sub["entry_price"]
        trades      = len(sub)
        avg_pct     = float(sub["pct"].mean() * 100)

        for leverage in LEVERAGES:
            # dollar_pnl is negative on losing trades — fully deducted from total_pnl
            dollar_pnls  = risk_usd * leverage * sub["pct"]
            wins         = int((dollar_pnls > 0).sum())
            total_pnl    = float(dollar_pnls.sum())
            win_rate     = wins / trades * 100
            strategy_ret = total_pnl / CAPITAL * 100
            alpha        = (strategy_ret - spy_return) if spy_return is not None else None

            results.append({
                "year"           : test_year,
                "hold_days"      : hold_days,
                "leverage"       : leverage,
                "top_tier_firms" : n_tt,
                "trades"         : trades,
                "win_rate"       : round(win_rate, 1),
                "avg_pct_return" : round(avg_pct, 2),
                "strategy_return": round(strategy_ret, 2),
                "spy_return"     : round(spy_return, 2) if spy_return is not None else None,
                "alpha"          : round(alpha, 2) if alpha is not None else None,
            })

    return results


# ============================================================================
# MAIN
# ============================================================================

def main():
    import tempfile

    print()
    print("=" * 72)
    print("  BACKTEST V2 — Walk-Forward, All Hold Periods x All Leverages")
    print(f"  Years    : {TEST_YEARS[0]}–{TEST_YEARS[-1]}")
    print(f"  Hold days: {HOLD_PERIODS}")
    print(f"  Leverages: {LEVERAGES}")
    print(f"  Capital  : ${CAPITAL:,}  |  Risk/trade: {RISK_PCT*100:.0f}%  (${int(CAPITAL*RISK_PCT)})")
    print("=" * 72)

    with tempfile.TemporaryDirectory() as tmp_dir:
        print("\n[DOWNLOAD] Fetching raw Parquet from S3...")
        paths = download_raw(tmp_dir)

        ohlc_path    = paths["ohlc"].replace("\\", "/")
        ratings_path = paths["ratings"].replace("\\", "/")

        conn = duckdb.connect()

        conn.execute(f"""
            CREATE VIEW stg_ohlc AS
            SELECT Ticker::VARCHAR AS Ticker, Date::DATE AS Date,
                   Open::DOUBLE AS Open, Close::DOUBLE AS Close
            FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY Ticker::VARCHAR, Date::DATE ORDER BY Close::DOUBLE DESC
                ) AS _rn
                FROM read_parquet('{ohlc_path}')
                WHERE Ticker IS NOT NULL AND Date IS NOT NULL
                  AND Close IS NOT NULL AND Close > 0
            ) sub WHERE _rn = 1
        """)

        conn.execute(f"""
            CREATE VIEW stg_ratings AS
            SELECT DISTINCT
                Ticker::VARCHAR             AS Ticker,
                TRY_CAST(GradeDate AS DATE) AS GradeDate,
                Firm::VARCHAR               AS Firm,
                Action::VARCHAR             AS Action
            FROM read_parquet('{ratings_path}')
            WHERE Action IN ('up', 'init')
              AND Ticker IS NOT NULL AND GradeDate IS NOT NULL AND Firm IS NOT NULL
        """)

        n_ohlc    = conn.execute("SELECT COUNT(*) FROM stg_ohlc").fetchone()[0]
        n_ratings = conn.execute("SELECT COUNT(*) FROM stg_ratings").fetchone()[0]
        print(f"  stg_ohlc rows    : {n_ohlc:,}")
        print(f"  stg_ratings rows : {n_ratings:,}")

        all_results = []

        for year in TEST_YEARS:
            print(f"\n  [{year}] Training...", end=" ", flush=True)
            top_tier = compute_top_tier(conn, year)
            print(f"{len(top_tier)} Top Tier firms  |  SPY...", end=" ", flush=True)
            spy = get_spy_return(year)
            spy_str = f"{spy:+.1f}%" if spy is not None else "N/A"
            print(f"SPY={spy_str}  |  Testing...", end=" ", flush=True)

            rows = run_year(conn, year, top_tier, spy)
            all_results.extend(rows)

            # Quick summary for 30d/5x
            sample = next((r for r in rows if r["hold_days"] == 30 and r["leverage"] == 5), None)
            if sample and sample["trades"] > 0:
                print(f"done — {sample['trades']} trades (30d/5x: WR={sample['win_rate']}%, ret={sample['strategy_return']:+.1f}%)")
            else:
                print("done — 0 trades")

        conn.close()

    # Print results table (30d/5x slice for readability)
    print()
    print("=" * 80)
    print("  BACKTEST V2 RESULTS — 30-day hold, 5x leverage (sample slice)")
    print("=" * 80)
    print(f"  {'Year':<6} {'TT Firms':<10} {'Trades':<8} {'Win Rate':<10} {'Avg Ret%':<10} {'Strategy':<12} {'SPY':<10} {'Alpha':<8}")
    print("  " + "-" * 76)
    for r in all_results:
        if r["hold_days"] == 30 and r["leverage"] == 5:
            if r["trades"] == 0:
                print(f"  {r['year']:<6} {r['top_tier_firms']:<10} {'0':<8} {'N/A':<10} {'N/A':<10} {'N/A':<12} {'N/A':<10} {'N/A':<8}")
            else:
                print(
                    f"  {r['year']:<6} {r['top_tier_firms']:<10} {r['trades']:<8} "
                    f"{r['win_rate']:.1f}%{'':<5} {r['avg_pct_return']:+.2f}%{'':<3} "
                    f"{r['strategy_return']:+.2f}%{'':<5} "
                    f"{r['spy_return']:+.2f}%{'':<3} {r['alpha']:+.2f}%"
                )
    print("=" * 80)

    # Save to S3
    results_df = pd.DataFrame(all_results)
    buf = BytesIO()
    results_df.to_parquet(buf, index=False, engine="pyarrow", compression="snappy")
    buf.seek(0)
    try:
        boto3.client("s3", region_name=AWS_REGION).put_object(
            Bucket=S3_BUCKET,
            Key="mart/mart_backtest_v2.parquet",
            Body=buf.read()
        )
        print(f"\n[SAVED] s3://{S3_BUCKET}/mart/mart_backtest_v2.parquet  ({len(results_df)} rows)")
    except Exception as e:
        print(f"\n[ERROR] S3 save failed: {e}")


if __name__ == "__main__":
    main()
