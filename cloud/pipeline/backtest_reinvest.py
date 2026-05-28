"""
backtest_reinvest.py
====================
Sequential compounding backtest — 5% position sizing, dual exit rules.

Strategy:
  - Start with $15,000; always redeploy proceeds immediately.
  - Each qualifying signal: invest 5% of current portfolio value.
  - Up to 20 concurrent positions (5% × 20 = 100% deployed).
  - Exit when: close >= entry × 1.05 (profit) OR 90 calendar days elapsed (time stop).
  - If no signals: stay in cash. Only enter if model's expected return >= 1.5%.
  - Signal sources: Top Tier analyst upgrades + historical seasonal patterns.

Walk-forward (no lookahead):
  Train: compute Top Tier firms and seasonal patterns from data BEFORE test_year.
  Test : simulate on test_year OHLC and signals only.

Output: mart/mart_backtest_reinvest.parquet
  One row per closed trade, 2015–2024.
  Includes portfolio_value_after for equity-curve reconstruction.

Usage:
    python aws/pipeline/backtest_reinvest.py
"""

import os
import bisect
import datetime
import warnings
import tempfile
from io import BytesIO

import boto3
import duckdb
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")


# ============================================================================
# CONFIGURATION
# ============================================================================

S3_BUCKET      = "meta-analyst-data-lake-YOUR_AWS_ACCOUNT_ID"
AWS_REGION     = "us-east-1"
TEST_YEARS     = list(range(2015, 2027))   # 2015–2026 inclusive (2026 = live YTD)

CAPITAL        = 15_000
MAX_SLOTS      = 20      # max concurrent positions
# No fixed position size — available cash is split equally across all
# signals available on a given day (up to remaining slots).
# This keeps the portfolio fully deployed whenever signals exist.

PROFIT_TARGET_FIXED = 0.05   # base strategies: fixed +5% target
PROFIT_TARGET_MIN   = 0.05   # analyst_target strategies: floor (never sell for less than +5%)
PROFIT_TARGET_MAX   = 0.25   # analyst_target strategies: cap  (never hold for more than +25%)
BREAKEVEN_DAYS      = 90     # after N days, lower target to breakeven
MIN_EXP_RETURN      = 1.5    # min model-predicted expected return % to enter
MAX_POSITION_PCT    = 0.25   # analyst_target strategies: max 25% of portfolio per position


# ============================================================================
# S3 DOWNLOAD
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
        ("ohlc",    "raw/ohlc/",           ["Ticker", "Date"]),
        ("ratings", "raw/ratings/",        None),
        ("stocks",  "raw/stocks_list/",    None),
    ]:
        local = os.path.join(tmp_dir, f"{table}.parquet")
        print(f"  [{table.upper()}] downloading...")
        _download_merged(s3, prefix, local, dedup_cols=dedup)
        paths[table] = local
    return paths


# ============================================================================
# TRAINING — Top Tier firms (identical logic to backtest_v2)
# Returns dict: {firm_name -> avg_ret_3m}
# ============================================================================

def compute_top_tier(conn, test_year):
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

    top_tier = df[
        (df["win_rate_6m"] > 50) &
        (df["avg_ret_6m"]  > 0)  &
        (df["avg_ret_3m"]  > 0)
    ][["Firm", "avg_ret_3m"]].copy()

    return dict(zip(top_tier["Firm"], top_tier["avg_ret_3m"]))


# ============================================================================
# TRAINING — Seasonal patterns (OHLC data before test_year only)
# Returns dict: {(ticker, month) -> {"avg_return": x, "win_rate": y}}
# Only includes entries qualifying for base seasonal signals
# (avg_return >= MIN_EXP_RETURN, win_rate >= 55%).
# The richer dict lets strategies apply stricter filters at signal time.
# ============================================================================

def compute_seasonal_patterns(conn, test_year):
    train_cut = f"{test_year}-01-01"

    df = conn.execute(f"""
        WITH monthly_bounds AS (
            SELECT
                Ticker,
                EXTRACT('year'  FROM Date)::INTEGER AS yr,
                EXTRACT('month' FROM Date)::INTEGER AS mo,
                MIN(Date) AS first_date,
                MAX(Date) AS last_date
            FROM stg_ohlc
            WHERE Date < '{train_cut}'
            GROUP BY Ticker,
                     EXTRACT('year'  FROM Date),
                     EXTRACT('month' FROM Date)
        ),
        with_prices AS (
            SELECT mb.Ticker, mb.yr, mb.mo,
                   o_open.Close  AS month_open,
                   o_close.Close AS month_close
            FROM monthly_bounds mb
            JOIN stg_ohlc o_open  ON mb.Ticker = o_open.Ticker  AND mb.first_date = o_open.Date
            JOIN stg_ohlc o_close ON mb.Ticker = o_close.Ticker AND mb.last_date  = o_close.Date
        ),
        monthly_returns AS (
            SELECT Ticker, mo,
                   (month_close - month_open) / NULLIF(month_open, 0) * 100 AS monthly_return
            FROM with_prices
            WHERE month_open > 0
        )
        SELECT
            Ticker,
            mo                                                                       AS month,
            COUNT(*)                                                                 AS years_observed,
            ROUND(AVG(monthly_return), 3)                                            AS avg_return,
            ROUND(COUNT(*) FILTER (WHERE monthly_return > 0)::DOUBLE
                  / COUNT(*) * 100, 1)                                               AS win_rate
        FROM monthly_returns
        GROUP BY Ticker, mo
        HAVING COUNT(*) >= 3
    """).fetchdf()

    qualified = df[
        (df["avg_return"] >= MIN_EXP_RETURN) &
        (df["win_rate"]   >= 55.0)
    ]

    # Return avg_return AND win_rate so strategies can apply their own thresholds
    return {
        (row["Ticker"], int(row["month"])): {
            "avg_return": float(row["avg_return"]),
            "win_rate"  : float(row["win_rate"]),
        }
        for _, row in qualified.iterrows()
    }


# ============================================================================
# TRAINING — Sector H2 underperformers (months 7–12 before test_year)
# Returns set of sector names that historically underperform in H2.
# A sector is "bad H2" if its avg monthly return in months 7-12 < 0
# OR its H2 win rate < 50% (min 18 observations = 3 years × 6 months).
# ============================================================================

SECTOR_H2_MIN_OBS       = 18    # min ticker-month observations per sector
SECTOR_H2_BAD_WINRATE   = 50.0  # threshold: below this = bad H2 sector
SECTOR_H2_BAD_AVG_RET   = 0.0   # threshold: below this = bad H2 sector


def compute_sector_h2_scores(conn, test_year):
    """
    Computes H2 (July–December) performance by sector from pre-test_year data.
    Returns a set of sector names that underperform in H2 and should be avoided
    when placing trades in months 7–12 of test_year.
    """
    train_cut = f"{test_year}-01-01"

    df = conn.execute(f"""
        WITH monthly_bounds AS (
            SELECT
                o.Ticker,
                s.Sector,
                EXTRACT('year'  FROM o.Date)::INTEGER AS yr,
                EXTRACT('month' FROM o.Date)::INTEGER AS mo,
                MIN(o.Date) AS first_date,
                MAX(o.Date) AS last_date
            FROM stg_ohlc o
            JOIN stg_stocks s ON s.Ticker = o.Ticker
            WHERE o.Date < '{train_cut}'
              AND EXTRACT('month' FROM o.Date)::INTEGER >= 7
              AND s.Sector IS NOT NULL
              AND s.Sector NOT IN ('Unknown', '')
            GROUP BY o.Ticker, s.Sector,
                     EXTRACT('year'  FROM o.Date),
                     EXTRACT('month' FROM o.Date)
        ),
        with_prices AS (
            SELECT mb.Sector, mb.yr, mb.mo,
                   (o_close.Close - o_open.Close) / NULLIF(o_open.Close, 0) * 100
                       AS monthly_return
            FROM monthly_bounds mb
            JOIN stg_ohlc o_open  ON mb.Ticker = o_open.Ticker  AND mb.first_date = o_open.Date
            JOIN stg_ohlc o_close ON mb.Ticker = o_close.Ticker AND mb.last_date  = o_close.Date
            WHERE o_open.Close > 0
        )
        SELECT
            Sector,
            COUNT(*)                                                          AS obs,
            ROUND(AVG(monthly_return), 3)                                    AS h2_avg_return,
            ROUND(COUNT(*) FILTER (WHERE monthly_return > 0)::DOUBLE
                  / COUNT(*) * 100, 1)                                       AS h2_win_rate
        FROM with_prices
        GROUP BY Sector
        HAVING COUNT(*) >= {SECTOR_H2_MIN_OBS}
    """).fetchdf()

    if df.empty:
        return set()

    bad = df[
        (df["h2_avg_return"] < SECTOR_H2_BAD_AVG_RET) |
        (df["h2_win_rate"]   < SECTOR_H2_BAD_WINRATE)
    ]
    return set(bad["Sector"].tolist())


# ============================================================================
# SPY BENCHMARK
# ============================================================================

def get_spy_return(test_year):
    try:
        hist = yf.Ticker("SPY").history(
            start=f"{test_year}-01-01",
            end=f"{test_year + 1}-01-01",
            auto_adjust=True,
        )
        if not hist.empty:
            return (
                float(hist["Close"].iloc[-1]) - float(hist["Close"].iloc[0])
            ) / float(hist["Close"].iloc[0]) * 100
    except Exception:
        pass
    return None


# ============================================================================
# BUILD SIGNAL FEED
# ============================================================================

def build_analyst_signals(conn, test_year, top_tier_firms):
    """
    All analyst upgrade signals from Top Tier firms during test_year.
    Expected return = the firm's historical avg_ret_3m from training.
    If multiple firms upgrade the same ticker on the same day, keep the one
    with the highest expected return.
    Returns DataFrame: ticker, signal_date, expected_return, signal_type.
    """
    if not top_tier_firms:
        return pd.DataFrame(columns=["ticker", "signal_date", "expected_return", "signal_type"])

    train_cut    = f"{test_year}-01-01"
    test_end     = f"{test_year + 1}-01-01"
    placeholders = ", ".join(
        f"'{f.replace(chr(39), chr(39)+chr(39))}'" for f in top_tier_firms
    )

    df = conn.execute(f"""
        SELECT DISTINCT
            r.Ticker     AS ticker,
            r.GradeDate  AS signal_date,
            r.Firm       AS firm
        FROM stg_ratings r
        WHERE r.GradeDate >= '{train_cut}'
          AND r.GradeDate <  '{test_end}'
          AND r.Firm IN ({placeholders})
        ORDER BY r.GradeDate, r.Ticker
    """).fetchdf()

    if df.empty:
        return pd.DataFrame(columns=["ticker", "signal_date", "expected_return", "signal_type"])

    df["expected_return"] = df["firm"].map(top_tier_firms)
    df["signal_type"]     = "analyst"
    df["signal_date"]     = pd.to_datetime(df["signal_date"]).dt.date

    # Keep best firm per (ticker, date)
    df = (
        df.sort_values("expected_return", ascending=False)
          .drop_duplicates(subset=["ticker", "signal_date"])
    )

    return df[["ticker", "signal_date", "expected_return", "signal_type"]].copy()


def build_seasonal_signals(test_year, seasonal_patterns, ohlc_df):
    """
    Generate one seasonal signal per (ticker, month) on the first trading day
    of that month in test_year, for all qualifying seasonal patterns.
    Returns DataFrame: ticker, signal_date, expected_return, signal_type.
    """
    # Pre-build: for each ticker, its trading dates in the year
    ticker_dates = (
        ohlc_df.groupby("Ticker")["Date"]
               .apply(lambda s: sorted(s.tolist()))
               .to_dict()
    )

    rows = []
    for month in range(1, 13):
        month_start = datetime.date(test_year, month, 1)
        if month == 12:
            month_end = datetime.date(test_year, 12, 31)
        else:
            month_end = datetime.date(test_year, month + 1, 1) - datetime.timedelta(days=1)

        qualifying = [
            (ticker, data["avg_return"])
            for (ticker, mo), data in seasonal_patterns.items()
            if mo == month
        ]
        if not qualifying:
            continue

        for ticker, avg_ret in qualifying:
            dates = ticker_dates.get(ticker, [])
            first_day = next(
                (d for d in dates if month_start <= d <= month_end),
                None
            )
            if first_day is None:
                continue
            rows.append({
                "ticker"         : ticker,
                "signal_date"    : first_day,
                "expected_return": avg_ret,
                "signal_type"    : "seasonal",
            })

    return pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["ticker", "signal_date", "expected_return", "signal_type"]
    )


# ============================================================================
# DAY-BY-DAY SIMULATION
# ============================================================================

SEASON_CONFIRM_MIN_WINRATE  = 60.0   # min win_rate % for seasonality confirmation
SEASON_CONFIRM_MIN_AVG_RET  = 0.0    # min avg_return % for seasonality confirmation


def run_year(conn, test_year, top_tier_firms, seasonal_patterns, spy_return,
             starting_capital=None,
             season_filter=False,
             dynamic_target=False,
             max_position_pct=1.0,
             bad_h2_sectors=None,
             ohlc_df=None,
             rebalance=False,
             slot_cap=MAX_SLOTS,
             strategy="base",
             profit_target_fixed=PROFIT_TARGET_FIXED):
    """
    Simulate the compounding strategy for one calendar year.

    starting_capital : portfolio value carried in from prior year (compounding).
                       Defaults to CAPITAL on first year.
    season_filter    : analyst signals only taken when current month confirms
                       (avg_return > 0, win_rate >= 60%).
    dynamic_target   : if True, each position's profit target = firm's expected
                       return (floored at PROFIT_TARGET_MIN, capped at PROFIT_TARGET_MAX).
                       If False, all positions use fixed PROFIT_TARGET_FIXED.
    max_position_pct : max fraction of portfolio in any single position
                       (e.g. 0.25 = 25% cap; 1.0 = no cap).
    bad_h2_sectors   : set of sector names to skip in H2 (July–Dec).
                       If None or empty, no sector filtering is applied.
    ohlc_df          : pre-loaded OHLC DataFrame for test_year (avoids re-querying).
                       If None, loaded from conn.
    rebalance        : if True, when new signals arrive trim existing positions to
                       equal weight and buy new signals with freed cash.
                       Negative positions trimmed at 50% to protect them.
    slot_cap         : max concurrent positions (default MAX_SLOTS=20).
                       Set to a large number for unlimited.
    strategy         : label written into every trade row.

    Returns list of dicts, one per closed trade.
    """
    train_cut = f"{test_year}-01-01"
    test_end  = f"{test_year + 1}-01-01"

    # Load test-year OHLC — use pre-loaded df if provided (avoids repeated SQL round-trips)
    if ohlc_df is None:
        ohlc_df = conn.execute(f"""
            SELECT Ticker, Date, Open, Close
            FROM stg_ohlc
            WHERE Date >= '{train_cut}' AND Date < '{test_end}'
            ORDER BY Ticker, Date
        """).fetchdf()
        ohlc_df["Date"] = pd.to_datetime(ohlc_df["Date"]).dt.date

    if ohlc_df.empty:
        return []

    # Build ticker -> sector lookup (used by sector H2 filter)
    ticker_sector: dict = {}
    if bad_h2_sectors:
        try:
            sec_df = conn.execute("""
                SELECT DISTINCT Ticker, Sector FROM stg_stocks
                WHERE Sector IS NOT NULL AND Sector NOT IN ('Unknown', '')
            """).fetchdf()
            ticker_sector = dict(zip(sec_df["Ticker"], sec_df["Sector"]))
        except Exception:
            ticker_sector = {}

    # O(1) price lookup: date -> ticker -> {open, close}
    price_by_date: dict = {}
    for row in ohlc_df.itertuples(index=False):
        d = row.Date
        if d not in price_by_date:
            price_by_date[d] = {}
        price_by_date[d][row.Ticker] = {"open": float(row.Open), "close": float(row.Close)}

    trading_days = sorted(price_by_date.keys())

    # Build unified signal feed
    analyst_sigs  = build_analyst_signals(conn, test_year, top_tier_firms)
    seasonal_sigs = build_seasonal_signals(test_year, seasonal_patterns, ohlc_df)

    # Shift analyst signal dates to next trading day.
    # Analyst upgrades are published pre-market but Wall Street algo desks
    # react instantly — by the time we run the daily pipeline the move is
    # already priced in.  Entering at the NEXT day's open is realistic.
    if not analyst_sigs.empty:
        def _next_trading_day(d):
            target = d + datetime.timedelta(days=1)
            idx = bisect.bisect_left(trading_days, target)
            return trading_days[idx] if idx < len(trading_days) else None
        analyst_sigs = analyst_sigs.copy()
        analyst_sigs["signal_date"] = analyst_sigs["signal_date"].apply(_next_trading_day)
        analyst_sigs = analyst_sigs.dropna(subset=["signal_date"]).copy()

    all_sigs = pd.concat([analyst_sigs, seasonal_sigs], ignore_index=True)
    all_sigs = all_sigs[all_sigs["expected_return"] >= MIN_EXP_RETURN].copy()

    # Season-confirmed filter: drop analyst signals where the current month's
    # seasonality does not agree (avg_return > threshold AND win_rate >= 60%).
    if season_filter and not all_sigs.empty:
        def _season_confirms(row):
            if row["signal_type"] != "analyst":
                return True   # seasonal signals are always kept
            month = pd.to_datetime(row["signal_date"]).month
            data  = seasonal_patterns.get((row["ticker"], month))
            if data is None:
                return False  # no seasonal history → skip
            return (
                data["avg_return"] > SEASON_CONFIRM_MIN_AVG_RET and
                data["win_rate"]   >= SEASON_CONFIRM_MIN_WINRATE
            )
        all_sigs = all_sigs[all_sigs.apply(_season_confirms, axis=1)].copy()

    # Sector H2 filter: drop signals in months 7-12 where the ticker's sector
    # historically underperforms in H2.
    if bad_h2_sectors and ticker_sector and not all_sigs.empty:
        def _sector_ok(row):
            month = pd.to_datetime(row["signal_date"]).month
            if month < 7:
                return True   # H1 — no sector filter applied
            sector = ticker_sector.get(row["ticker"])
            if sector is None:
                return True   # unknown sector — allow through
            return sector not in bad_h2_sectors
        all_sigs = all_sigs[all_sigs.apply(_sector_ok, axis=1)].copy()

    # Dedup: if same ticker on same day from both sources, keep highest expected return
    all_sigs = (
        all_sigs.sort_values("expected_return", ascending=False)
                .drop_duplicates(subset=["ticker", "signal_date"])
    )

    # Index by date for fast lookup
    signals_by_date: dict = {}
    for row in all_sigs.itertuples(index=False):
        d = row.signal_date
        if d not in signals_by_date:
            signals_by_date[d] = []
        signals_by_date[d].append({
            "ticker"         : row.ticker,
            "expected_return": float(row.expected_return),
            "signal_type"    : row.signal_type,
        })
    for d in signals_by_date:
        signals_by_date[d].sort(key=lambda x: x["expected_return"], reverse=True)

    # ── SIMULATION ──────────────────────────────────────────────────────────
    start           = float(starting_capital) if starting_capital is not None else float(CAPITAL)
    portfolio_value = start
    cash            = start
    open_positions  = []   # active trades
    trades          = []   # closed trades (returned at end)

    for today in trading_days:
        prices_today = price_by_date.get(today, {})

        # 1. CHECK EXITS ─────────────────────────────────────────────────────
        remaining   = []
        closed_today = []

        for pos in open_positions:
            cp = prices_today.get(pos["ticker"], {}).get("close")
            if cp is None:
                remaining.append(pos)
                continue

            days_held      = (today - pos["entry_date"]).days
            profit_hit     = cp >= pos["entry_price"] * (1.0 + pos["profit_target"])
            # After BREAKEVEN_DAYS, lower exit target to breakeven (0%)
            breakeven_hit  = days_held >= BREAKEVEN_DAYS and cp >= pos["entry_price"]

            if profit_hit or breakeven_hit:
                pct_ret  = (cp - pos["entry_price"]) / pos["entry_price"]
                proceeds = pos["shares"] * cp
                cash    += proceeds
                closed_today.append({
                    "year"                : test_year,
                    "entry_date"          : str(pos["entry_date"]),
                    "exit_date"           : str(today),
                    "ticker"              : pos["ticker"],
                    "signal_type"         : pos["signal_type"],
                    "entry_price"         : round(pos["entry_price"], 4),
                    "exit_price"          : round(cp, 4),
                    "pct_return"          : round(pct_ret * 100, 3),
                    "exit_reason"         : "profit" if profit_hit else "breakeven",
                    "expected_return_pct" : round(pos["expected_return"], 3),
                    "profit_target_pct"   : round(pos["profit_target"] * 100, 1),
                    "hold_days"           : days_held,
                    "portfolio_value_after": None,   # filled below
                    "spy_return_yr"       : spy_return,
                    "strategy"            : strategy,
                })
            else:
                remaining.append(pos)

        open_positions = remaining

        # 2. MARK-TO-MARKET ──────────────────────────────────────────────────
        open_value = sum(
            pos["shares"] * prices_today.get(pos["ticker"], {}).get("close", pos["entry_price"])
            for pos in open_positions
        )
        portfolio_value = cash + open_value

        # Fill portfolio_value_after for all trades closed today
        for t in closed_today:
            t["portfolio_value_after"] = round(portfolio_value, 2)
        trades.extend(closed_today)

        # 3. DEPLOY CAPITAL ──────────────────────────────────────────────────
        if today in signals_by_date:
            open_tickers = {p["ticker"] for p in open_positions}

            # All eligible new signals for today
            eligible_all = [
                sig for sig in signals_by_date[today]
                if sig["ticker"] not in open_tickers
                and prices_today.get(sig["ticker"], {}).get("open", 0) > 0
            ]

            if rebalance and eligible_all:
                # ── REBALANCE MODE ──────────────────────────────────────────
                # How many new signals can we add within slot_cap?
                available_slots = slot_cap - len(open_positions)
                new_sigs = eligible_all[:available_slots] if available_slots > 0 else []

                if new_sigs:
                    n_total      = len(open_positions) + len(new_sigs)
                    target_size  = portfolio_value / n_total

                    # Trim existing positions to target_size to free cash.
                    # Negative positions: sell only 50% of excess (protect losers,
                    # let them recover to breakeven rather than locking in losses).
                    for pos in open_positions:
                        cp          = prices_today.get(pos["ticker"], {}).get("close", pos["entry_price"])
                        current_val = pos["shares"] * cp
                        excess      = current_val - target_size
                        if excess <= 0:
                            continue
                        is_negative  = cp < pos["entry_price"]
                        sell_amount  = excess * (0.5 if is_negative else 1.0)
                        shares_sold  = sell_amount / cp
                        pos["shares"] -= shares_sold
                        cash          += sell_amount

                    # Buy each new signal at target_size (or whatever cash remains)
                    for sig in new_sigs:
                        open_price  = prices_today[sig["ticker"]]["open"]
                        actual_size = min(target_size, cash)
                        if actual_size < 1.0:
                            break
                        pt = max(PROFIT_TARGET_MIN,
                                 min(PROFIT_TARGET_MAX, sig["expected_return"] / 100.0)) \
                             if dynamic_target else profit_target_fixed
                        cash -= actual_size
                        open_positions.append({
                            "ticker"         : sig["ticker"],
                            "signal_type"    : sig["signal_type"],
                            "entry_date"     : today,
                            "entry_price"    : open_price,
                            "shares"         : actual_size / open_price,
                            "expected_return": sig["expected_return"],
                            "profit_target"  : pt,
                        })
                        open_tickers.add(sig["ticker"])

            elif not rebalance and cash >= 1.0:
                # ── STANDARD MODE (original logic) ──────────────────────────
                available_slots = slot_cap - len(open_positions)
                eligible = eligible_all[:available_slots]

                if eligible:
                    raw_size = cash / len(eligible)
                    pos_size = min(raw_size, portfolio_value * max_position_pct)

                    for sig in eligible:
                        open_price  = prices_today[sig["ticker"]]["open"]
                        actual_size = min(pos_size, cash)
                        if actual_size < 1.0:
                            break
                        pt = max(PROFIT_TARGET_MIN,
                                 min(PROFIT_TARGET_MAX, sig["expected_return"] / 100.0)) \
                             if dynamic_target else profit_target_fixed
                        shares = actual_size / open_price
                        cash  -= actual_size
                        open_positions.append({
                            "ticker"         : sig["ticker"],
                            "signal_type"    : sig["signal_type"],
                            "entry_date"     : today,
                            "entry_price"    : open_price,
                            "shares"         : shares,
                            "expected_return": sig["expected_return"],
                            "profit_target"  : pt,
                        })
                        open_tickers.add(sig["ticker"])

    # Force-close positions still open at year end
    last_day    = trading_days[-1]
    prices_last = price_by_date.get(last_day, {})

    for pos in open_positions:
        cp        = prices_last.get(pos["ticker"], {}).get("close", pos["entry_price"])
        pct_ret   = (cp - pos["entry_price"]) / pos["entry_price"]
        proceeds  = pos["shares"] * cp
        cash     += proceeds
        days_held = (last_day - pos["entry_date"]).days
        trades.append({
            "year"                : test_year,
            "entry_date"          : str(pos["entry_date"]),
            "exit_date"           : str(last_day),
            "ticker"              : pos["ticker"],
            "signal_type"         : pos["signal_type"],
            "entry_price"         : round(pos["entry_price"], 4),
            "exit_price"          : round(cp, 4),
            "pct_return"          : round(pct_ret * 100, 3),
            "exit_reason"         : "year_end",
            "expected_return_pct" : round(pos["expected_return"], 3),
            "profit_target_pct"   : round(pos["profit_target"] * 100, 1),
            "hold_days"           : days_held,
            "portfolio_value_after": round(cash, 2),
            "spy_return_yr"       : spy_return,
            "strategy"            : strategy,
        })

    open_positions = []
    portfolio_value = cash

    # Patch year_end rows that were added before final cash tally
    for t in trades:
        if t["portfolio_value_after"] is None:
            t["portfolio_value_after"] = round(portfolio_value, 2)

    return trades


# ============================================================================
# MAIN
# ============================================================================

def main():
    print()
    print("=" * 72)
    print("  COMPOUNDER — Sequential 5% Position Sizing, Dual Exit Rules")
    print(f"  Years        : {TEST_YEARS[0]}–{TEST_YEARS[-1]}")
    print(f"  Capital      : ${CAPITAL:,}")
    print(f"  Position size: cash / signals (dynamic)  |  Max slots: {MAX_SLOTS}")
    print(f"  Profit target: +{PROFIT_TARGET_FIXED*100:.0f}%  |  Breakeven after: {BREAKEVEN_DAYS} days")
    print(f"  Min exp. ret : {MIN_EXP_RETURN}%  (model-predicted)")
    print("=" * 72)

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
                Ticker::VARCHAR             AS Ticker,
                TRY_CAST(GradeDate AS DATE) AS GradeDate,
                Firm::VARCHAR               AS Firm,
                Action::VARCHAR             AS Action
            FROM read_parquet('{ratings_path}')
            WHERE Action IN ('up', 'init')
              AND Ticker IS NOT NULL AND GradeDate IS NOT NULL AND Firm IS NOT NULL
        """)

        conn.execute(f"""
            CREATE VIEW stg_stocks AS
            SELECT DISTINCT
                Ticker::VARCHAR AS Ticker,
                COALESCE(TRY_CAST(Sector AS VARCHAR), 'Unknown') AS Sector
            FROM read_parquet('{stocks_path}')
            WHERE Ticker IS NOT NULL
        """)

        n_ohlc    = conn.execute("SELECT COUNT(*) FROM stg_ohlc").fetchone()[0]
        n_ratings = conn.execute("SELECT COUNT(*) FROM stg_ratings").fetchone()[0]
        print(f"  stg_ohlc rows    : {n_ohlc:,}")
        print(f"  stg_ratings rows : {n_ratings:,}")

        all_trades = []

        STRATEGIES = [
            {
                "key": "base",
                "season_filter": False, "dynamic_target": False, "max_position_pct": 1.0,
                "sector_h2_filter": False, "rebalance": False, "slot_cap": MAX_SLOTS,
                "label": "Base — fixed +5% target, dynamic sizing",
                "profit_target_fixed": PROFIT_TARGET_FIXED,
            },
            {
                "key": "season_confirmed",
                "season_filter": True,  "dynamic_target": False, "max_position_pct": 1.0,
                "sector_h2_filter": False, "rebalance": False, "slot_cap": MAX_SLOTS,
                "label": f"Season Confirmed — fixed +5%, season filter (win_rate>={SEASON_CONFIRM_MIN_WINRATE:.0f}%)",
                "profit_target_fixed": PROFIT_TARGET_FIXED,
            },
            {
                "key": "analyst_target",
                "season_filter": False, "dynamic_target": True,  "max_position_pct": MAX_POSITION_PCT,
                "sector_h2_filter": False, "rebalance": False, "slot_cap": MAX_SLOTS,
                "label": f"Analyst Target — firm's avg_ret_3m as target ({PROFIT_TARGET_MIN*100:.0f}%-{PROFIT_TARGET_MAX*100:.0f}%), max {MAX_POSITION_PCT*100:.0f}%/position",
                "profit_target_fixed": PROFIT_TARGET_FIXED,
            },
            {
                "key": "analyst_target_season",
                "season_filter": True,  "dynamic_target": True,  "max_position_pct": MAX_POSITION_PCT,
                "sector_h2_filter": False, "rebalance": False, "slot_cap": MAX_SLOTS,
                "label": f"Analyst Target + Season — dynamic target + season confirmation",
                "profit_target_fixed": PROFIT_TARGET_FIXED,
            },
            {
                "key": "sector_h2",
                "season_filter": False, "dynamic_target": False, "max_position_pct": 1.0,
                "sector_h2_filter": True,
                "rebalance": False, "slot_cap": MAX_SLOTS,
                "label": "Sector H2 Aware — skip bad H2 sectors in Jul–Dec",
                "profit_target_fixed": PROFIT_TARGET_FIXED,
            },
            {
                "key": "sector_h2_analyst_target",
                "season_filter": False, "dynamic_target": True,  "max_position_pct": MAX_POSITION_PCT,
                "sector_h2_filter": True,
                "rebalance": False, "slot_cap": MAX_SLOTS,
                "label": f"Sector H2 + Analyst Target — dynamic target, skip bad H2 sectors",
                "profit_target_fixed": PROFIT_TARGET_FIXED,
            },
            {
                "key": "rebalance_20",
                "season_filter": False, "dynamic_target": False, "max_position_pct": 1.0,
                "sector_h2_filter": False,
                "rebalance": True, "slot_cap": MAX_SLOTS,
                "label": f"Rebalance (max {MAX_SLOTS}) — trim existing positions equally to fund new signals",
                "profit_target_fixed": PROFIT_TARGET_FIXED,
            },
            {
                "key": "rebalance_unlimited",
                "season_filter": False, "dynamic_target": False, "max_position_pct": 1.0,
                "sector_h2_filter": False,
                "rebalance": True, "slot_cap": 9999,
                "label": "Rebalance (unlimited) — equal-weight rebalance on every new signal, no slot cap",
                "profit_target_fixed": PROFIT_TARGET_FIXED,
            },
            {
                "key": "base_10pct",
                "season_filter": False, "dynamic_target": False, "max_position_pct": 1.0,
                "sector_h2_filter": False, "rebalance": False, "slot_cap": MAX_SLOTS,
                "label": "Base 10% — fixed +10% target, dynamic sizing, breakeven exit after 90 days",
                "profit_target_fixed": 0.10,
            },
            {
                "key": "season_confirmed_10pct",
                "season_filter": True, "dynamic_target": False, "max_position_pct": 1.0,
                "sector_h2_filter": False, "rebalance": False, "slot_cap": MAX_SLOTS,
                "label": f"Season Confirmed 10% — fixed +10%, season filter (win_rate>={SEASON_CONFIRM_MIN_WINRATE:.0f}%)",
                "profit_target_fixed": 0.10,
            },
        ]

        # ── Precompute per-year training data (computed ONCE, reused across all strategies) ──
        print("\n[TRAINING] Precomputing models for each year...")
        year_cache = {}
        for year in TEST_YEARS:
            top_tier = compute_top_tier(conn, year)
            seasonal = compute_seasonal_patterns(conn, year)
            spy      = get_spy_return(year)
            bad_h2   = compute_sector_h2_scores(conn, year)

            # Pre-load OHLC for the test year once
            train_cut = f"{year}-01-01"
            test_end  = f"{year + 1}-01-01"
            ohlc_df = conn.execute(f"""
                SELECT Ticker, Date, Open, Close
                FROM stg_ohlc
                WHERE Date >= '{train_cut}' AND Date < '{test_end}'
                ORDER BY Ticker, Date
            """).fetchdf()
            ohlc_df["Date"] = pd.to_datetime(ohlc_df["Date"]).dt.date

            year_cache[year] = dict(
                top_tier = top_tier,
                seasonal = seasonal,
                spy      = spy,
                bad_h2   = bad_h2,
                ohlc_df  = ohlc_df,
            )
            print(f"  {year}: {len(top_tier)} TT firms | {len(seasonal)} seasonal patterns | "
                  f"{len(bad_h2)} bad-H2 sectors | {len(ohlc_df):,} OHLC rows")

        for strat in STRATEGIES:
            rolling_pv = CAPITAL   # reset to $15k for each strategy (each is independent)
            print()
            print(f"  === {strat['label']} ===")
            print(f"  {'Year':<6} {'Start $':<12} {'Trades':<8} {'Profit%':<10} {'End $':<12} {'Ret%':<8} {'SPY':<8}")
            print("  " + "-" * 72)

            for year in TEST_YEARS:
                cache    = year_cache[year]
                top_tier = cache["top_tier"]
                seasonal = cache["seasonal"]
                spy      = cache["spy"]
                spy_str  = f"{spy:+.1f}%" if spy is not None else "  N/A"
                year_start = rolling_pv

                bad_h2 = cache["bad_h2"] if strat.get("sector_h2_filter") else None

                trades = run_year(
                    conn, year, top_tier, seasonal, spy,
                    starting_capital     = year_start,
                    season_filter        = strat["season_filter"],
                    dynamic_target       = strat["dynamic_target"],
                    max_position_pct     = strat["max_position_pct"],
                    bad_h2_sectors       = bad_h2,
                    ohlc_df              = cache["ohlc_df"],
                    rebalance            = strat["rebalance"],
                    slot_cap             = strat["slot_cap"],
                    strategy             = strat["key"],
                    profit_target_fixed  = strat["profit_target_fixed"],
                )
                all_trades.extend(trades)

                real_trades  = [t for t in trades if t["exit_reason"] != "year_end"]
                profit_exits = [t for t in real_trades if t["exit_reason"] == "profit"]
                final_pv     = trades[-1]["portfolio_value_after"] if trades else rolling_pv
                year_ret     = (final_pv / year_start - 1) * 100 if year_start > 0 else 0
                profit_pct   = len(profit_exits) / len(real_trades) * 100 if real_trades else 0

                print(
                    f"  {year:<6} ${year_start:>9,.0f}  "
                    f"{len(real_trades):<8} {profit_pct:5.1f}%{'':<4} "
                    f"${final_pv:>9,.0f}  {year_ret:+.1f}%{'':<2} {spy_str}"
                )
                rolling_pv = final_pv   # compound into next year

            print("  " + "-" * 72)
            final_total_ret = (rolling_pv / CAPITAL - 1) * 100
            print(f"  10-year: ${CAPITAL:,} -> ${rolling_pv:,.0f}  ({final_total_ret:+.1f}% total)")

        conn.close()

    results_df = pd.DataFrame(all_trades)
    buf = BytesIO()
    results_df.to_parquet(buf, index=False, engine="pyarrow", compression="snappy")
    buf.seek(0)

    try:
        boto3.client("s3", region_name=AWS_REGION).put_object(
            Bucket=S3_BUCKET,
            Key="mart/mart_backtest_reinvest.parquet",
            Body=buf.read(),
        )
        print(f"\n[SAVED] s3://{S3_BUCKET}/mart/mart_backtest_reinvest.parquet  ({len(results_df)} rows)")
    except Exception as e:
        print(f"\n[ERROR] S3 save failed: {e}")


if __name__ == "__main__":
    main()
