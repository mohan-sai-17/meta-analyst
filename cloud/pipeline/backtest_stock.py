"""
backtest_stock.py
=================
Stock-only walk-forward backtest — no options leverage.

For each test_year in 2015-2024:
  Train  : compute firm scorecard using GradeDate < {test_year}-01-01
  Derive : Top Tier firms from train result
  Test   : replay signals in {test_year} from those firms, buy stock at Open,
           sell at Close 30 days later — NO leverage multiplier
  SPY    : buy-and-hold benchmark for the test period

Trade sizing: 2% of capital risked per trade, 1x leverage.
  => dollar_pnl = ($15,000 * 2%) * pct_return = $300 * pct_return

Saves results to s3://<bucket>/mart/mart_backtest_stock.parquet

Usage:
    python aws/pipeline/backtest_stock.py
"""

import os
import warnings
import boto3
import duckdb
import pandas as pd
import yfinance as yf
from io import BytesIO
from datetime import date

warnings.filterwarnings("ignore")


# ============================================================================
# CONFIGURATION
# ============================================================================

S3_BUCKET  = "meta-analyst-data-lake-YOUR_AWS_ACCOUNT_ID"
AWS_REGION = "us-east-1"
TEST_YEARS = list(range(2015, 2025))   # 2015 through 2024 inclusive
LEVERAGE   = 1                          # no leverage — raw stock return
RISK_PCT   = 0.02
CAPITAL    = 15_000
HOLD_DAYS  = 30


# ============================================================================
# DATA DOWNLOAD
# ============================================================================

def _download_merged(s3_client, prefix, local_path):
    resp    = s3_client.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix)
    objects = sorted(
        [o for o in resp.get("Contents", []) if o["Key"].endswith(".parquet")],
        key=lambda o: o["Key"]
    )
    if not objects:
        raise RuntimeError(f"No Parquet files under s3://{S3_BUCKET}/{prefix}")

    frames = []
    for obj in objects:
        buf = BytesIO()
        s3_client.download_fileobj(S3_BUCKET, obj["Key"], buf)
        buf.seek(0)
        frames.append(pd.read_parquet(buf))

    merged = pd.concat(frames, ignore_index=True)
    if "GradeDate" in merged.columns:
        merged["GradeDate"] = pd.to_datetime(merged["GradeDate"], errors="coerce")
    merged.to_parquet(local_path, index=False, engine="pyarrow", compression="snappy")
    return local_path


def download_raw(tmp_dir):
    s3    = boto3.client("s3", region_name=AWS_REGION)
    paths = {}
    for table, prefix in [("ohlc", "raw/ohlc/"), ("ratings", "raw/ratings/")]:
        local = os.path.join(tmp_dir, f"{table}.parquet")
        print(f"  [{table.upper()}] downloading...")
        _download_merged(s3, prefix, local)
        paths[table] = local
    return paths


# ============================================================================
# WALK-FORWARD ENGINE
# ============================================================================

def run_year(conn, test_year, ohlc_df_full):
    """Run one walk-forward year. Returns dict with metrics."""
    train_cut = f"{test_year}-01-01"
    test_end  = f"{test_year + 1}-01-01"

    # Training: firm scorecard on pre-train_cut data
    train_scorecard = conn.execute(f"""
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

    top_tier_firms = set(
        train_scorecard[
            (train_scorecard["win_rate_6m"] > 50) &
            (train_scorecard["avg_ret_6m"]  > 0)  &
            (train_scorecard["avg_ret_3m"]  > 0)
        ]["Firm"].tolist()
    )

    if not top_tier_firms:
        return {"year": test_year, "trades": 0, "win_rate": None,
                "strategy_return": None, "spy_return": None, "alpha": None,
                "top_tier_firms": 0, "avg_pct_return": None}

    # Test: signals in test_year from Top Tier firms only
    placeholders = ", ".join(f"'{f.replace(chr(39), chr(39)+chr(39))}'" for f in top_tier_firms)
    test_ratings = conn.execute(f"""
        SELECT r.Ticker, r.Firm, r.GradeDate, r.Action
        FROM stg_ratings r
        WHERE r.GradeDate >= '{train_cut}'
          AND r.GradeDate <  '{test_end}'
          AND r.Firm IN ({placeholders})
        ORDER BY r.GradeDate
    """).fetchdf()

    if test_ratings.empty:
        return {"year": test_year, "trades": 0, "win_rate": None,
                "strategy_return": None, "spy_return": None, "alpha": None,
                "top_tier_firms": len(top_tier_firms), "avg_pct_return": None}

    risk_usd  = CAPITAL * RISK_PCT
    trade_log = []

    for _, row in test_ratings.iterrows():
        ticker     = row["Ticker"]
        grade_date = pd.to_datetime(row["GradeDate"])

        ticker_ohlc = ohlc_df_full[ohlc_df_full["Ticker"] == ticker].copy()
        if ticker_ohlc.empty:
            continue
        ticker_ohlc = ticker_ohlc.sort_values("Date").set_index("Date")

        future_entry = ticker_ohlc[ticker_ohlc.index >= grade_date]
        if future_entry.empty:
            continue
        entry_date  = future_entry.index[0]
        entry_price = float(future_entry["Open"].iloc[0])

        exit_target = entry_date + pd.Timedelta(days=HOLD_DAYS)
        future_exit = ticker_ohlc[ticker_ohlc.index >= exit_target]
        if future_exit.empty:
            continue
        exit_price = float(future_exit["Close"].iloc[0])

        pct_return = (exit_price - entry_price) / entry_price
        # No leverage — multiply by 1, not 5
        dollar_pnl = risk_usd * LEVERAGE * pct_return

        trade_log.append({
            "Ticker"    : ticker,
            "Entry_Date": entry_date,
            "Pct_Return": pct_return * 100,
            "Dollar_PnL": dollar_pnl,
            "Win"       : dollar_pnl > 0
        })

    if not trade_log:
        return {"year": test_year, "trades": 0, "win_rate": None,
                "strategy_return": None, "spy_return": None, "alpha": None,
                "top_tier_firms": len(top_tier_firms), "avg_pct_return": None}

    trade_df      = pd.DataFrame(trade_log)
    total_trades  = len(trade_df)
    wins          = trade_df["Win"].sum()
    win_rate      = wins / total_trades * 100
    avg_pct       = trade_df["Pct_Return"].mean()
    total_pnl     = trade_df["Dollar_PnL"].sum()
    strategy_ret  = total_pnl / CAPITAL * 100

    # SPY benchmark
    spy_return = None
    try:
        spy_hist = yf.Ticker("SPY").history(
            start=train_cut, end=test_end, auto_adjust=True
        )
        if not spy_hist.empty:
            spy_return = (
                float(spy_hist["Close"].iloc[-1]) - float(spy_hist["Close"].iloc[0])
            ) / float(spy_hist["Close"].iloc[0]) * 100
    except Exception:
        pass

    alpha = (strategy_ret - spy_return) if spy_return is not None else None

    return {
        "year"            : test_year,
        "trades"          : total_trades,
        "win_rate"        : round(win_rate, 1),
        "avg_pct_return"  : round(avg_pct, 2),
        "strategy_return" : round(strategy_ret, 2),
        "spy_return"      : round(spy_return, 2) if spy_return is not None else None,
        "alpha"           : round(alpha, 2) if alpha is not None else None,
        "top_tier_firms"  : len(top_tier_firms),
    }


# ============================================================================
# MAIN
# ============================================================================

def main():
    import tempfile

    print()
    print("=" * 72)
    print("  STOCK-ONLY WALK-FORWARD BACKTEST — Meta-Analyst (no leverage)")
    print(f"  Train:  data before each test year")
    print(f"  Test:   {TEST_YEARS[0]}-{TEST_YEARS[-1]}  ({len(TEST_YEARS)} years)")
    print(f"  Capital: ${CAPITAL:,}  |  Risk: {RISK_PCT*100:.0f}%  |  Leverage: {LEVERAGE}x  |  Hold: {HOLD_DAYS}d")
    print("=" * 72)

    with tempfile.TemporaryDirectory() as tmp_dir:
        print("\n[DOWNLOAD] Fetching raw Parquet from S3...")
        paths = download_raw(tmp_dir)

        ohlc_path    = paths["ohlc"].replace("\\", "/")
        ratings_path = paths["ratings"].replace("\\", "/")

        conn = duckdb.connect()

        conn.execute(f"""
            CREATE VIEW stg_ohlc AS
            SELECT
                Ticker::VARCHAR AS Ticker,
                Date::DATE      AS Date,
                Open::DOUBLE    AS Open,
                High::DOUBLE    AS High,
                Low::DOUBLE     AS Low,
                Close::DOUBLE   AS Close,
                Volume::BIGINT  AS Volume
            FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY Ticker::VARCHAR, Date::DATE ORDER BY Close::DOUBLE
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

        n_ohlc    = conn.execute("SELECT count(*) FROM stg_ohlc").fetchone()[0]
        n_ratings = conn.execute("SELECT count(*) FROM stg_ratings").fetchone()[0]
        print(f"  stg_ohlc rows    : {n_ohlc:,}")
        print(f"  stg_ratings rows : {n_ratings:,}")

        print("\n[LOAD] Loading OHLC into pandas for trade simulation...")
        ohlc_df = conn.execute(
            "SELECT Ticker, Date, Open, Close FROM stg_ohlc"
        ).fetchdf()
        ohlc_df["Date"] = pd.to_datetime(ohlc_df["Date"])

        print("\n[RUN] Walk-forward simulation (10 years)...\n")
        results = []
        for year in TEST_YEARS:
            print(f"  Year {year}...", end=" ", flush=True)
            r = run_year(conn, year, ohlc_df)
            results.append(r)
            if r["trades"] == 0:
                print(f"0 trades (top_tier_firms={r['top_tier_firms']})")
            else:
                spy_str   = f"{r['spy_return']:+.1f}%" if r["spy_return"] is not None else "N/A"
                alpha_str = f"{r['alpha']:+.1f}%"      if r["alpha"]      is not None else "N/A"
                print(f"done — {r['trades']} trades, WR={r['win_rate']}%, "
                      f"AvgRet={r['avg_pct_return']:+.1f}%, Ret={r['strategy_return']:+.1f}%, "
                      f"SPY={spy_str}, Alpha={alpha_str}")

        conn.close()

    # Print results table
    print()
    print("=" * 80)
    print("  STOCK-ONLY WALK-FORWARD RESULTS (no leverage)")
    print("=" * 80)
    header = f"  {'Year':<6} {'TT Firms':<10} {'Trades':<8} {'Win Rate':<10} {'Avg Ret%':<10} {'Strategy':<12} {'SPY':<10} {'Alpha':<8}"
    print(header)
    print("  " + "-" * 76)
    for r in results:
        if r["trades"] == 0:
            print(f"  {r['year']:<6} {r['top_tier_firms']:<10} {'0':<8} {'N/A':<10} {'N/A':<10} {'N/A':<12} {'N/A':<10} {'N/A':<8}")
        else:
            wr_str  = f"{r['win_rate']:.1f}%"
            avg_str = f"{r['avg_pct_return']:+.2f}%"
            ret_str = f"{r['strategy_return']:+.2f}%"
            spy_str = f"{r['spy_return']:+.2f}%" if r["spy_return"] is not None else "N/A"
            alp_str = f"{r['alpha']:+.2f}%"      if r["alpha"]      is not None else "N/A"
            print(f"  {r['year']:<6} {r['top_tier_firms']:<10} {r['trades']:<8} {wr_str:<10} {avg_str:<10} {ret_str:<12} {spy_str:<10} {alp_str:<8}")
    print("=" * 80)
    print()

    # Save to S3
    try:
        results_df = pd.DataFrame(results)
        buf = BytesIO()
        results_df.to_parquet(buf, index=False, engine="pyarrow", compression="snappy")
        buf.seek(0)
        boto3.client("s3", region_name=AWS_REGION).put_object(
            Bucket=S3_BUCKET,
            Key="mart/mart_backtest_stock.parquet",
            Body=buf.read()
        )
        print(f"[BACKTEST] Saved -> s3://{S3_BUCKET}/mart/mart_backtest_stock.parquet")
    except Exception as e:
        print(f"[BACKTEST] S3 save failed (non-fatal): {e}")


if __name__ == "__main__":
    main()
