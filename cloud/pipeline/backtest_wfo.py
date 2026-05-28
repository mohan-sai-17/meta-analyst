"""
backtest_wfo.py
===============
Walk-Forward Optimizer — finds the best (take_profit %, hold_days) parameters
for each test year using ONLY prior-year data, then tests out-of-sample.

Grid:
  take_profit : 3%, 5%, 7%, 10%
  hold_days   : 30, 60, 90, 120
  (16 combinations)

Training metric: mean expected return per signal across all training years.
  Computed vectorized from OHLC — no day-by-day simulation during training.

Test: full day-by-day portfolio simulation with the winning params,
  PLUS a fixed 5%/90d baseline for direct comparison.

Output: mart/mart_wfo.parquet
  One row per test year (2017–2025):
    year, best_take_profit_pct, best_hold_days,
    train_score, test_return_pct, fixed_test_return_pct,
    test_trades, test_win_rate_pct, spy_return_pct

Usage:
    python aws/pipeline/backtest_wfo.py
"""

import os
import bisect
import datetime
import warnings
import tempfile
from io import BytesIO

import boto3
import duckdb
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")


# ============================================================================
# CONFIGURATION
# ============================================================================

S3_BUCKET    = "meta-analyst-data-lake-YOUR_AWS_ACCOUNT_ID"
AWS_REGION   = "us-east-1"
CAPITAL      = 15_000
MAX_SLOTS    = 20
MIN_EXP_RETURN = 1.5      # min expected return % to enter a signal

TAKE_PROFITS = [0.03, 0.05, 0.07, 0.10]
HOLD_DAYS    = [30,   60,   90,   120 ]

FIXED_TP  = 0.05
FIXED_HD  = 90

# First year we have enough training data to optimize (need ≥2 prior years)
FIRST_TEST_YEAR = 2017
LAST_TEST_YEAR  = 2025
ALL_YEARS       = list(range(2015, LAST_TEST_YEAR + 1))


# ============================================================================
# S3 DOWNLOAD  (same as backtest_reinvest.py)
# ============================================================================

def _download_merged(s3, prefix, local_path, dedup_cols=None):
    resp    = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix)
    objects = sorted(
        [o for o in resp.get("Contents", []) if o["Key"].endswith(".parquet")],
        key=lambda o: o["Key"],
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
    if dedup_cols:
        merged = merged.drop_duplicates(subset=dedup_cols, keep="last")
    merged.to_parquet(local_path, index=False, engine="pyarrow", compression="snappy")
    return local_path


def download_raw(tmp_dir):
    s3    = boto3.client("s3", region_name=AWS_REGION)
    paths = {}
    for table, prefix, dedup in [
        ("ohlc",    "raw/ohlc/",        ["Ticker", "Date"]),
        ("ratings", "raw/ratings/",     None),
        ("stocks",  "raw/stocks_list/", None),
    ]:
        local = os.path.join(tmp_dir, f"{table}.parquet")
        print(f"  [{table.upper()}] downloading...")
        _download_merged(s3, prefix, local, dedup_cols=dedup)
        paths[table] = local
    return paths


# ============================================================================
# TRAINING DATA
# ============================================================================

def compute_top_tier(conn, test_year):
    """Returns {firm: avg_ret_3m} using data strictly before test_year."""
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
        SELECT Firm,
            AVG(CASE WHEN close_3m IS NOT NULL
                     THEN (close_3m - entry_price) / entry_price * 100 END) AS avg_ret_3m,
            AVG(CASE WHEN close_6m IS NOT NULL
                     THEN (close_6m - entry_price) / entry_price * 100 END) AS avg_ret_6m,
            SUM(CASE WHEN close_6m > entry_price THEN 1 ELSE 0 END)::DOUBLE /
                NULLIF(COUNT(CASE WHEN close_6m IS NOT NULL THEN 1 END), 0) * 100
                AS win_rate_6m,
            COUNT(*) AS signal_count
        FROM signals
        GROUP BY Firm
        HAVING COUNT(*) >= 2
    """).fetchdf()

    top = df[
        (df["win_rate_6m"] > 50) &
        (df["avg_ret_6m"]  > 0)  &
        (df["avg_ret_3m"]  > 0)
    ]
    return dict(zip(top["Firm"], top["avg_ret_3m"]))


def get_spy_return(test_year):
    try:
        hist = yf.Ticker("SPY").history(
            start=f"{test_year}-01-01",
            end=f"{test_year + 1}-01-01",
            auto_adjust=True,
        )
        if not hist.empty:
            return (float(hist["Close"].iloc[-1]) - float(hist["Close"].iloc[0])) \
                   / float(hist["Close"].iloc[0]) * 100
    except Exception:
        pass
    return None


# ============================================================================
# VECTORIZED SIGNAL OUTCOME COMPUTATION
# ============================================================================

def build_analyst_signals_df(conn, year_start, year_end, top_tier_firms):
    """Pull analyst signals from [year_start, year_end) that are from top tier firms."""
    if not top_tier_firms:
        return pd.DataFrame()
    placeholders = ", ".join(
        f"'{f.replace(chr(39), chr(39)+chr(39))}'" for f in top_tier_firms
    )
    df = conn.execute(f"""
        SELECT DISTINCT r.Ticker AS ticker, r.GradeDate AS signal_date, r.Firm AS firm
        FROM stg_ratings r
        WHERE r.GradeDate >= '{year_start}-01-01'
          AND r.GradeDate <  '{year_end}-01-01'
          AND r.Firm IN ({placeholders})
        ORDER BY r.GradeDate, r.Ticker
    """).fetchdf()
    if df.empty:
        return pd.DataFrame()
    df["signal_date"] = pd.to_datetime(df["signal_date"]).dt.date
    df["expected_return"] = df["firm"].map(top_tier_firms)
    df = df[df["expected_return"] >= MIN_EXP_RETURN].copy()
    # Keep best firm per (ticker, date)
    df = (df.sort_values("expected_return", ascending=False)
            .drop_duplicates(subset=["ticker", "signal_date"]))
    return df[["ticker", "signal_date", "expected_return"]].copy()


def precompute_outcomes(signals_df, ticker_data):
    """
    For each signal, compute the outcome return under every (tp, hd) combo.
    Uses numpy arrays per ticker for speed — no SQL needed.

    ticker_data: {ticker: {"dates": np.array[date], "closes": np.array[float]}}

    Returns a DataFrame with columns:
      ticker, signal_date, year, entry_price,
      ret_{tp}p_{hd}d  (one column per combo, e.g. ret_5p_90d)
    """
    rows = []
    for _, sig in signals_df.iterrows():
        ticker     = sig["ticker"]
        sig_date   = sig["signal_date"]

        td = ticker_data.get(ticker)
        if td is None:
            continue
        dates  = td["dates"]   # sorted numpy array of date objects
        closes = td["closes"]

        # Entry = next trading day after signal_date
        idx = bisect.bisect_right(dates, sig_date)
        if idx >= len(dates):
            continue
        entry_price = closes[idx]
        entry_day   = dates[idx]
        if entry_price <= 0:
            continue

        row = {
            "ticker"      : ticker,
            "signal_date" : sig_date,
            "year"        : sig_date.year,
            "entry_price" : entry_price,
        }

        for tp in TAKE_PROFITS:
            for hd in HOLD_DAYS:
                end_dt  = entry_day + datetime.timedelta(days=hd)
                # Indices of dates in the hold window
                lo = bisect.bisect_right(dates, entry_day)
                hi = bisect.bisect_right(dates, end_dt)
                window = closes[lo:hi]

                if len(window) == 0:
                    ret = 0.0
                elif np.any(window >= entry_price * (1.0 + tp)):
                    ret = float(tp)
                else:
                    ret = float((window[-1] - entry_price) / entry_price)

                row[f"ret_{int(tp*100)}p_{hd}d"] = ret

        rows.append(row)

    return pd.DataFrame(rows) if rows else pd.DataFrame()


# ============================================================================
# FULL PORTFOLIO SIMULATION  (base strategy, parameterized)
# ============================================================================

def run_year_simulation(conn, test_year, top_tier_firms, spy_return,
                        starting_capital, profit_target, hold_days,
                        ohlc_df):
    """
    Full day-by-day portfolio simulation for one year with given exit params.
    Base strategy: analyst signals only, no season/sector filters.
    Returns (list_of_trades, final_portfolio_value).
    """
    train_cut = f"{test_year}-01-01"
    test_end  = f"{test_year + 1}-01-01"

    year_ohlc = ohlc_df[
        (ohlc_df["Date"] >= datetime.date(test_year, 1, 1)) &
        (ohlc_df["Date"] <  datetime.date(test_year + 1, 1, 1))
    ].copy()

    if year_ohlc.empty:
        return [], float(starting_capital)

    # O(1) price lookup
    price_by_date = {}
    for row in year_ohlc.itertuples(index=False):
        d = row.Date
        if d not in price_by_date:
            price_by_date[d] = {}
        price_by_date[d][row.Ticker] = {"open": float(row.Open), "close": float(row.Close)}

    trading_days = sorted(price_by_date.keys())

    # Build signals, shift to next trading day
    signals_df = build_analyst_signals_df(conn, test_year, test_year + 1, top_tier_firms)

    def _next_td(d):
        target = d + datetime.timedelta(days=1)
        i = bisect.bisect_left(trading_days, target)
        return trading_days[i] if i < len(trading_days) else None

    if not signals_df.empty:
        signals_df["signal_date"] = signals_df["signal_date"].apply(_next_td)
        signals_df = signals_df.dropna(subset=["signal_date"]).copy()

    signals_by_date = {}
    for row in signals_df.itertuples(index=False):
        d = row.signal_date
        if d not in signals_by_date:
            signals_by_date[d] = []
        signals_by_date[d].append({
            "ticker"         : row.ticker,
            "expected_return": float(row.expected_return),
        })
    for d in signals_by_date:
        signals_by_date[d].sort(key=lambda x: x["expected_return"], reverse=True)

    portfolio_value = float(starting_capital)
    cash            = portfolio_value
    open_positions  = []
    trades          = []

    for today in trading_days:
        prices_today = price_by_date.get(today, {})

        # Exits
        remaining    = []
        closed_today = []
        for pos in open_positions:
            cp = prices_today.get(pos["ticker"], {}).get("close")
            if cp is None:
                remaining.append(pos)
                continue
            days_held  = (today - pos["entry_date"]).days
            profit_hit = cp >= pos["entry_price"] * (1.0 + profit_target)
            time_stop  = days_held >= hold_days and cp >= pos["entry_price"]
            if profit_hit or time_stop:
                pct_ret = (cp - pos["entry_price"]) / pos["entry_price"]
                cash   += pos["shares"] * cp
                closed_today.append({
                    "year"                 : test_year,
                    "ticker"               : pos["ticker"],
                    "entry_price"          : round(pos["entry_price"], 4),
                    "exit_price"           : round(cp, 4),
                    "pct_return"           : round(pct_ret * 100, 3),
                    "exit_reason"          : "profit" if profit_hit else "time_stop",
                    "hold_days_actual"     : days_held,
                    "portfolio_value_after": None,
                    "spy_return_yr"        : spy_return,
                    "param_tp_pct"         : round(profit_target * 100, 0),
                    "param_hd"             : hold_days,
                })
            else:
                remaining.append(pos)
        open_positions = remaining

        open_value      = sum(
            pos["shares"] * prices_today.get(pos["ticker"], {}).get("close", pos["entry_price"])
            for pos in open_positions
        )
        portfolio_value = cash + open_value

        for t in closed_today:
            t["portfolio_value_after"] = round(portfolio_value, 2)
        trades.extend(closed_today)

        # Deploy
        if today in signals_by_date and cash >= 1.0:
            open_tickers    = {p["ticker"] for p in open_positions}
            available_slots = MAX_SLOTS - len(open_positions)
            eligible = [
                sig for sig in signals_by_date[today]
                if sig["ticker"] not in open_tickers
                and prices_today.get(sig["ticker"], {}).get("open", 0) > 0
            ][:available_slots]

            if eligible:
                pos_size = cash / len(eligible)
                for sig in eligible:
                    actual_size = min(pos_size, cash)
                    if actual_size < 1.0:
                        break
                    open_price = prices_today[sig["ticker"]]["open"]
                    cash      -= actual_size
                    open_positions.append({
                        "ticker"    : sig["ticker"],
                        "entry_date": today,
                        "entry_price": open_price,
                        "shares"    : actual_size / open_price,
                    })

    # Force-close year end
    last_day    = trading_days[-1]
    prices_last = price_by_date.get(last_day, {})
    for pos in open_positions:
        cp       = prices_last.get(pos["ticker"], {}).get("close", pos["entry_price"])
        pct_ret  = (cp - pos["entry_price"]) / pos["entry_price"]
        cash    += pos["shares"] * cp
        trades.append({
            "year"                 : test_year,
            "ticker"               : pos["ticker"],
            "entry_price"          : round(pos["entry_price"], 4),
            "exit_price"           : round(cp, 4),
            "pct_return"           : round(pct_ret * 100, 3),
            "exit_reason"          : "year_end",
            "hold_days_actual"     : (last_day - pos["entry_date"]).days,
            "portfolio_value_after": round(cash, 2),
            "spy_return_yr"        : spy_return,
            "param_tp_pct"         : round(profit_target * 100, 0),
            "param_hd"             : hold_days,
        })
    portfolio_value = cash

    for t in trades:
        if t["portfolio_value_after"] is None:
            t["portfolio_value_after"] = round(portfolio_value, 2)

    final_pv = trades[-1]["portfolio_value_after"] if trades else float(starting_capital)
    return trades, final_pv


# ============================================================================
# MAIN
# ============================================================================

def main():
    print()
    print("=" * 68)
    print("  WALK-FORWARD OPTIMIZER — take_profit × hold_days grid search")
    print(f"  Grid     : {len(TAKE_PROFITS)} take_profit × {len(HOLD_DAYS)} hold_days = "
          f"{len(TAKE_PROFITS)*len(HOLD_DAYS)} combos")
    print(f"  Test yrs : {FIRST_TEST_YEAR}–{LAST_TEST_YEAR}")
    print(f"  Baseline : fixed {int(FIXED_TP*100)}% / {FIXED_HD}d")
    print("=" * 68)

    with tempfile.TemporaryDirectory() as tmp_dir:
        print("\n[DOWNLOAD] Fetching raw Parquet from S3...")
        paths = download_raw(tmp_dir)

        ohlc_path    = paths["ohlc"].replace("\\", "/")
        ratings_path = paths["ratings"].replace("\\", "/")
        stocks_path  = paths["stocks"].replace("\\", "/")

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
                Ticker::VARCHAR AS Ticker,
                TRY_CAST(GradeDate AS DATE) AS GradeDate,
                Firm::VARCHAR AS Firm, Action::VARCHAR AS Action
            FROM read_parquet('{ratings_path}')
            WHERE Action IN ('up', 'init')
              AND Ticker IS NOT NULL AND GradeDate IS NOT NULL AND Firm IS NOT NULL
        """)

        # Pre-load ALL OHLC into memory for vectorized outcome computation
        print("\n[OHLC] Loading full OHLC into memory for vectorized outcomes...")
        ohlc_all = conn.execute(
            "SELECT Ticker, Date, Open, Close FROM stg_ohlc ORDER BY Ticker, Date"
        ).fetchdf()
        ohlc_all["Date"] = pd.to_datetime(ohlc_all["Date"]).dt.date

        # Build per-ticker arrays
        ticker_data = {}
        for ticker, grp in ohlc_all.groupby("Ticker"):
            g = grp.sort_values("Date")
            ticker_data[ticker] = {
                "dates" : np.array(g["Date"].tolist()),
                "closes": np.array(g["Close"].values, dtype=float),
                "opens" : np.array(g["Open"].values,  dtype=float),
            }
        print(f"  {len(ticker_data)} tickers loaded")

        # Pre-compute top_tier for each test year boundary
        print("\n[TRAIN] Computing top-tier firms for each test year...")
        top_tier_by_year = {}
        for yr in range(FIRST_TEST_YEAR, LAST_TEST_YEAR + 1):
            top_tier_by_year[yr] = compute_top_tier(conn, yr)
            print(f"  {yr}: {len(top_tier_by_year[yr])} top-tier firms")

        # Pre-fetch SPY returns
        print("\n[SPY] Fetching annual SPY returns...")
        spy_by_year = {}
        for yr in range(FIRST_TEST_YEAR, LAST_TEST_YEAR + 1):
            spy_by_year[yr] = get_spy_return(yr)

        # ── Walk-forward loop ────────────────────────────────────────────────
        summary_rows = []
        all_trades   = []

        for test_year in range(FIRST_TEST_YEAR, LAST_TEST_YEAR + 1):
            print(f"\n{'='*68}")
            print(f"  Test year: {test_year}")
            train_years = list(range(2015, test_year))
            top_tier    = top_tier_by_year[test_year]
            spy         = spy_by_year[test_year]

            # ── Build training signals (all years before test_year) ──────────
            train_sigs = build_analyst_signals_df(
                conn, 2015, test_year, top_tier
            )
            print(f"  Training signals ({2015}–{test_year-1}): {len(train_sigs)}")

            if train_sigs.empty:
                print("  [SKIP] No training signals — skipping this year")
                continue

            # ── Precompute vectorized outcomes for training signals ───────────
            outcomes = precompute_outcomes(train_sigs, ticker_data)
            if outcomes.empty:
                print("  [SKIP] Could not compute outcomes — skipping")
                continue

            # ── Grid search: pick combo with highest mean expected return ─────
            best_score = -999.0
            best_tp    = FIXED_TP
            best_hd    = FIXED_HD
            grid_rows  = []

            print(f"\n  Grid search ({len(TAKE_PROFITS)*len(HOLD_DAYS)} combos):")
            for tp in TAKE_PROFITS:
                for hd in HOLD_DAYS:
                    col   = f"ret_{int(tp*100)}p_{hd}d"
                    if col not in outcomes.columns:
                        continue
                    score = float(outcomes[col].mean())
                    grid_rows.append({
                        "year"           : test_year,
                        "take_profit_pct": round(tp * 100, 0),
                        "hold_days"      : hd,
                        "train_score"    : round(score * 100, 3),
                    })
                    if score > best_score:
                        best_score = score
                        best_tp    = tp
                        best_hd    = hd

            print(f"  Best combo: tp={int(best_tp*100)}%  hd={best_hd}d  "
                  f"train_score={best_score*100:.2f}%")

            # ── Full simulation: best params ─────────────────────────────────
            trades_best, final_pv_best = run_year_simulation(
                conn, test_year, top_tier, spy,
                starting_capital = CAPITAL,
                profit_target    = best_tp,
                hold_days        = best_hd,
                ohlc_df          = ohlc_all,
            )

            # ── Full simulation: fixed baseline ──────────────────────────────
            trades_fixed, final_pv_fixed = run_year_simulation(
                conn, test_year, top_tier, spy,
                starting_capital = CAPITAL,
                profit_target    = FIXED_TP,
                hold_days        = FIXED_HD,
                ohlc_df          = ohlc_all,
            )

            real_best  = [t for t in trades_best  if t["exit_reason"] != "year_end"]
            real_fixed = [t for t in trades_fixed if t["exit_reason"] != "year_end"]
            wins_best  = [t for t in real_best  if t["exit_reason"] == "profit"]
            wins_fixed = [t for t in real_fixed if t["exit_reason"] == "profit"]

            test_ret_best  = (final_pv_best  / CAPITAL - 1) * 100
            test_ret_fixed = (final_pv_fixed / CAPITAL - 1) * 100
            win_rate_best  = (len(wins_best)  / len(real_best)  * 100) if real_best  else 0
            win_rate_fixed = (len(wins_fixed) / len(real_fixed) * 100) if real_fixed else 0

            spy_str = f"{spy:+.1f}%" if spy is not None else "N/A"
            print(f"\n  Results:")
            print(f"    WFO  ({int(best_tp*100)}% / {best_hd:3d}d): "
                  f"{test_ret_best:+.1f}%  ({len(real_best)} trades, "
                  f"{win_rate_best:.0f}% win rate)")
            print(f"    Fixed ({int(FIXED_TP*100)}% / {FIXED_HD:3d}d): "
                  f"{test_ret_fixed:+.1f}%  ({len(real_fixed)} trades, "
                  f"{win_rate_fixed:.0f}% win rate)")
            print(f"    SPY  : {spy_str}")

            summary_rows.append({
                "year"                 : test_year,
                "best_take_profit_pct" : int(best_tp * 100),
                "best_hold_days"       : best_hd,
                "train_score_pct"      : round(best_score * 100, 3),
                "test_return_pct"      : round(test_ret_best,  2),
                "fixed_test_return_pct": round(test_ret_fixed, 2),
                "test_trades"          : len(real_best),
                "test_win_rate_pct"    : round(win_rate_best, 1),
                "fixed_trades"         : len(real_fixed),
                "fixed_win_rate_pct"   : round(win_rate_fixed, 1),
                "spy_return_pct"       : round(spy, 2) if spy is not None else None,
            })
            summary_rows[-1].update({k: v for row in grid_rows
                                     for k, v in [("_grid", True)]})

            # Tag trades
            for t in trades_best:
                t["row_type"] = "wfo"
            for t in trades_fixed:
                t["row_type"] = "fixed"
            all_trades.extend(trades_best)
            all_trades.extend(trades_fixed)

        conn.close()

    # ── Save to S3 ───────────────────────────────────────────────────────────
    summary_df = pd.DataFrame(summary_rows).drop(columns=["_grid"], errors="ignore")
    print(f"\n[SAVE] {len(summary_df)} summary rows")
    print(summary_df[["year", "best_take_profit_pct", "best_hold_days",
                       "test_return_pct", "fixed_test_return_pct",
                       "spy_return_pct"]].to_string(index=False))

    buf = BytesIO()
    summary_df.to_parquet(buf, index=False, engine="pyarrow", compression="snappy")
    buf.seek(0)
    try:
        boto3.client("s3", region_name=AWS_REGION).put_object(
            Bucket=S3_BUCKET,
            Key="mart/mart_wfo.parquet",
            Body=buf.read(),
        )
        print(f"\n[SAVED] s3://{S3_BUCKET}/mart/mart_wfo.parquet  "
              f"({len(summary_df)} rows)")
    except Exception as e:
        print(f"\n[ERROR] S3 save failed: {e}")

    print("=" * 68)


if __name__ == "__main__":
    main()
