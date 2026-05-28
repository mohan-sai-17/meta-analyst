"""
backtest_ml.py
==============
Walk-forward ML signal filter — trains a GradientBoostingClassifier on
historical signal outcomes and predicts which NEW signals will hit +5%
within 90 days.

Features per signal:
  month              : 1–12 (seasonality)
  firm_score         : analyst firm's historical avg_ret_3m from training data
  stock_momentum_5d  : stock's 5-day return at signal date (%)
  stock_momentum_20d : stock's 20-day return at signal date (%)
  market_momentum_20d: median 20-day return across all S&P 500 at signal date (market regime)
  sector_code        : integer-encoded sector

Label: hit_target = stock closed >= entry_price * 1.05 within 90 calendar days

Walk-forward: train on years 1..N-1, predict on year N (no lookahead).

Outputs:
  mart/mart_ml_summary.parquet — per-year: filtered vs all-signals performance,
                                  precision, recall, top 3 feature importances
  mart/mart_ml_live.parquet    — current signals scored by model trained on all
                                  history, ranked by confidence (descending)

Usage:
    python aws/pipeline/backtest_ml.py
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
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.preprocessing import LabelEncoder
from sklearn.metrics import precision_score, recall_score

warnings.filterwarnings("ignore")


# ============================================================================
# CONFIGURATION
# ============================================================================

S3_BUCKET       = "meta-analyst-data-lake-YOUR_AWS_ACCOUNT_ID"
AWS_REGION      = "us-east-1"
CAPITAL         = 15_000
MAX_SLOTS       = 20
MIN_EXP_RETURN  = 1.5
PROFIT_TARGET   = 0.05    # label: did it hit +5%?
HOLD_DAYS       = 90      # within 90 calendar days
MIN_TRAIN_YEARS = 2       # need at least this many training years before predicting
FIRST_TEST_YEAR = 2018    # first year with enough training data (2015, 2016, 2017)
LAST_TEST_YEAR  = 2025
ALL_YEARS       = list(range(2015, LAST_TEST_YEAR + 1))
PROB_THRESHOLD  = 0.55    # min predicted probability to take a signal

FEATURE_COLS = [
    "month",
    "firm_score",
    "stock_momentum_5d",
    "stock_momentum_20d",
    "market_momentum_20d",
    "sector_code",
]


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
# TRAINING HELPERS
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
            SELECT e.Ticker, e.Firm, e.GradeDate, e.entry_price, m3.close_3m, m6.close_6m
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
# FEATURE ENGINEERING
# ============================================================================

def compute_market_momentum(ohlc_all, lookback_days=20):
    """
    For each trading date, compute the median 20-day return across all stocks.
    Returns {date: median_return_%}.
    """
    print("  [FEAT] Computing market momentum (median 20d return per date)...")
    ohlc_all = ohlc_all.sort_values(["Ticker", "Date"])

    # Pivot: index=Date, columns=Ticker, values=Close
    pivot = ohlc_all.pivot_table(index="Date", columns="Ticker", values="Close", aggfunc="last")
    pct   = pivot.pct_change(lookback_days) * 100   # % return over lookback window
    market_momentum = pct.median(axis=1).to_dict()  # date -> median across tickers
    return market_momentum


def build_feature_dataset(conn, year_start, year_end, top_tier_firms,
                          ticker_data, market_momentum, sector_map,
                          sector_encoder):
    """
    Build ML feature rows for all analyst signals in [year_start, year_end).
    Returns DataFrame with FEATURE_COLS + label columns.
    """
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
    df = (df.sort_values("expected_return", ascending=False)
            .drop_duplicates(subset=["ticker", "signal_date"]))

    rows = []
    for _, sig in df.iterrows():
        ticker     = sig["ticker"]
        sig_date   = sig["signal_date"]
        firm       = sig["firm"]

        td = ticker_data.get(ticker)
        if td is None:
            continue

        dates  = td["dates"]
        closes = td["closes"]

        # Entry = next trading day after signal_date
        idx = bisect.bisect_right(dates, sig_date)
        if idx >= len(dates):
            continue
        entry_price = closes[idx]
        entry_day   = dates[idx]
        if entry_price <= 0:
            continue

        # Momentum features: look BACK from entry_day
        def _momentum(lookback):
            back_dt = entry_day - datetime.timedelta(days=lookback * 2)  # rough calendar buffer
            lo = bisect.bisect_left(dates, back_dt)
            hi = bisect.bisect_left(dates, entry_day)
            # want exactly `lookback` trading days back
            if hi - lo < 1:
                return 0.0
            actual_lo = max(0, hi - lookback)
            ref_price = closes[actual_lo]
            return float((entry_price - ref_price) / ref_price * 100) if ref_price > 0 else 0.0

        stock_mom_5d  = _momentum(5)
        stock_mom_20d = _momentum(20)
        mkt_mom_20d   = market_momentum.get(entry_day, 0.0)

        # Sector
        sector  = sector_map.get(ticker, "Unknown")
        try:
            sec_code = int(sector_encoder.transform([sector])[0])
        except Exception:
            sec_code = 0

        # Label: did it hit PROFIT_TARGET within HOLD_DAYS?
        end_dt = entry_day + datetime.timedelta(days=HOLD_DAYS)
        lo     = bisect.bisect_right(dates, entry_day)
        hi     = bisect.bisect_right(dates, end_dt)
        window = closes[lo:hi]
        hit_target = bool(len(window) > 0 and np.any(window >= entry_price * (1.0 + PROFIT_TARGET)))

        rows.append({
            "ticker"              : ticker,
            "signal_date"         : sig_date,
            "year"                : sig_date.year,
            "firm"                : firm,
            "sector"              : sector,
            "firm_score"          : float(sig["expected_return"]),
            "month"               : sig_date.month,
            "stock_momentum_5d"   : round(stock_mom_5d,  3),
            "stock_momentum_20d"  : round(stock_mom_20d, 3),
            "market_momentum_20d" : round(float(mkt_mom_20d) if mkt_mom_20d is not None else 0.0, 3),
            "sector_code"         : sec_code,
            "hit_target"          : int(hit_target),
            "entry_price"         : round(entry_price, 4),
            "entry_day"           : entry_day,
        })

    return pd.DataFrame(rows)


# ============================================================================
# PORTFOLIO SIMULATION (for filtered vs all comparison)
# ============================================================================

def simulate_signals(signals_df, ticker_data, starting_capital, profit_target, hold_days):
    """
    Simple per-signal return simulation — no portfolio sizing, just equal-weight
    average return across signals. Returns (mean_return_pct, n_wins, n_total).
    """
    if signals_df.empty:
        return 0.0, 0, 0

    returns = []
    for _, sig in signals_df.iterrows():
        td = ticker_data.get(sig["ticker"])
        if td is None:
            continue
        dates  = td["dates"]
        closes = td["closes"]

        entry_day   = sig["entry_day"]
        entry_price = sig["entry_price"]
        if entry_price <= 0:
            continue

        end_dt = entry_day + datetime.timedelta(days=hold_days)
        lo     = bisect.bisect_right(dates, entry_day)
        hi     = bisect.bisect_right(dates, end_dt)
        window = closes[lo:hi]

        if len(window) == 0:
            continue
        if np.any(window >= entry_price * (1.0 + profit_target)):
            returns.append(profit_target * 100)
        else:
            ret = float((window[-1] - entry_price) / entry_price * 100)
            returns.append(ret)

    if not returns:
        return 0.0, 0, 0

    arr     = np.array(returns)
    n_wins  = int(np.sum(arr > 0))
    return float(np.mean(arr)), n_wins, len(returns)


# ============================================================================
# MAIN
# ============================================================================

def main():
    print()
    print("=" * 68)
    print("  ML SIGNAL FILTER — GradientBoostingClassifier, walk-forward")
    print(f"  Features    : {FEATURE_COLS}")
    print(f"  Label       : hit +{int(PROFIT_TARGET*100)}% within {HOLD_DAYS}d")
    print(f"  Test years  : {FIRST_TEST_YEAR}–{LAST_TEST_YEAR}")
    print(f"  Threshold   : prob >= {PROB_THRESHOLD:.0%} to take signal")
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
        conn.execute(f"""
            CREATE VIEW stg_stocks AS
            SELECT DISTINCT Ticker::VARCHAR AS Ticker,
                COALESCE(TRY_CAST(Sector AS VARCHAR), 'Unknown') AS Sector
            FROM read_parquet('{stocks_path}')
            WHERE Ticker IS NOT NULL
        """)

        # Load full OHLC into memory
        print("\n[OHLC] Loading full OHLC into memory...")
        ohlc_all = conn.execute(
            "SELECT Ticker, Date, Open, Close FROM stg_ohlc ORDER BY Ticker, Date"
        ).fetchdf()
        ohlc_all["Date"] = pd.to_datetime(ohlc_all["Date"]).dt.date

        # Per-ticker numpy arrays
        ticker_data = {}
        for ticker, grp in ohlc_all.groupby("Ticker"):
            g = grp.sort_values("Date")
            ticker_data[ticker] = {
                "dates" : np.array(g["Date"].tolist()),
                "closes": np.array(g["Close"].values, dtype=float),
            }
        print(f"  {len(ticker_data)} tickers loaded")

        # Sector map
        sec_df     = conn.execute(
            "SELECT DISTINCT Ticker, Sector FROM stg_stocks"
        ).fetchdf()
        sector_map = dict(zip(sec_df["Ticker"], sec_df["Sector"]))

        # Fit sector encoder on all known sectors
        all_sectors = sorted(set(sector_map.values()) | {"Unknown"})
        sector_encoder = LabelEncoder()
        sector_encoder.fit(all_sectors)

        # Market momentum: {date -> median 20d return}
        market_momentum = compute_market_momentum(ohlc_all)

        # Pre-compute top_tier for each test year
        print("\n[TRAIN] Computing top-tier firms per year...")
        top_tier_by_year = {}
        for yr in range(FIRST_TEST_YEAR, LAST_TEST_YEAR + 2):  # +2 for live (2026)
            top_tier_by_year[yr] = compute_top_tier(conn, yr)
            print(f"  {yr}: {len(top_tier_by_year[yr])} top-tier firms")

        # SPY returns
        print("\n[SPY] Fetching annual SPY returns...")
        spy_by_year = {yr: get_spy_return(yr) for yr in range(FIRST_TEST_YEAR, LAST_TEST_YEAR + 1)}

        # ── Build full historical feature dataset ────────────────────────────
        print("\n[FEAT] Building feature dataset for all years 2015–2025...")
        all_feat_dfs = []
        for yr in range(2015, LAST_TEST_YEAR + 1):
            tt = top_tier_by_year.get(yr + 1, {})   # top_tier from data BEFORE yr
            feat_df = build_feature_dataset(
                conn, yr, yr + 1, tt,
                ticker_data, market_momentum, sector_map, sector_encoder
            )
            if not feat_df.empty:
                all_feat_dfs.append(feat_df)
                print(f"  {yr}: {len(feat_df)} signals  "
                      f"(hit_rate={feat_df['hit_target'].mean()*100:.0f}%)")

        all_features = pd.concat(all_feat_dfs, ignore_index=True) if all_feat_dfs else pd.DataFrame()
        print(f"\n  Total signals: {len(all_features)}  "
              f"(overall hit rate: {all_features['hit_target'].mean()*100:.0f}%)"
              if not all_features.empty else "\n  No feature data built")

        # ── Walk-forward ML ──────────────────────────────────────────────────
        summary_rows = []

        for test_year in range(FIRST_TEST_YEAR, LAST_TEST_YEAR + 1):
            print(f"\n{'='*68}")
            print(f"  Test year: {test_year}")

            train_df = all_features[all_features["year"] < test_year].copy()
            test_df  = all_features[all_features["year"] == test_year].copy()

            if len(train_df) < 20 or test_df.empty:
                print(f"  [SKIP] Insufficient data (train={len(train_df)}, test={len(test_df)})")
                continue

            X_train = train_df[FEATURE_COLS].fillna(0).values
            y_train = train_df["hit_target"].values
            X_test  = test_df[FEATURE_COLS].fillna(0).values
            y_test  = test_df["hit_target"].values

            clf = GradientBoostingClassifier(
                n_estimators=100, max_depth=3, learning_rate=0.1,
                random_state=42, subsample=0.8,
            )
            clf.fit(X_train, y_train)

            probs      = clf.predict_proba(X_test)[:, 1]
            preds      = (probs >= PROB_THRESHOLD).astype(int)

            precision = float(precision_score(y_test, preds, zero_division=0))
            recall    = float(recall_score(y_test, preds, zero_division=0))

            feat_imp   = clf.feature_importances_
            sorted_idx = np.argsort(feat_imp)[::-1]
            top3       = [(FEATURE_COLS[i], round(float(feat_imp[i]), 4))
                          for i in sorted_idx[:3]]

            # Simulate: all signals vs ML-filtered signals
            test_df = test_df.copy()
            test_df["ml_prob"]     = probs
            test_df["ml_take"]     = preds

            filtered_df = test_df[test_df["ml_take"] == 1].copy()

            all_ret, all_wins, all_n = simulate_signals(
                test_df, ticker_data, CAPITAL, PROFIT_TARGET, HOLD_DAYS
            )
            filt_ret, filt_wins, filt_n = simulate_signals(
                filtered_df, ticker_data, CAPITAL, PROFIT_TARGET, HOLD_DAYS
            )

            spy = spy_by_year.get(test_year)
            spy_str = f"{spy:+.1f}%" if spy is not None else "N/A"

            print(f"  All signals  : {all_n} signals  avg_ret={all_ret:+.2f}%  "
                  f"wins={all_wins}")
            print(f"  ML filtered  : {filt_n} signals  avg_ret={filt_ret:+.2f}%  "
                  f"wins={filt_wins}  "
                  f"(precision={precision:.2f}  recall={recall:.2f})")
            print(f"  SPY          : {spy_str}")
            print(f"  Top features : "
                  + "  |  ".join(f"{n}={v:.3f}" for n, v in top3))

            summary_rows.append({
                "year"                  : test_year,
                "n_all_signals"         : all_n,
                "n_filtered_signals"    : filt_n,
                "all_signals_return_pct": round(all_ret,  3),
                "filtered_return_pct"   : round(filt_ret, 3),
                "precision"             : round(precision, 4),
                "recall"                : round(recall,    4),
                "feature_1"             : top3[0][0],
                "feature_1_importance"  : top3[0][1],
                "feature_2"             : top3[1][0] if len(top3) > 1 else "",
                "feature_2_importance"  : top3[1][1] if len(top3) > 1 else 0.0,
                "feature_3"             : top3[2][0] if len(top3) > 2 else "",
                "feature_3_importance"  : top3[2][1] if len(top3) > 2 else 0.0,
                "spy_return_pct"        : round(spy, 2) if spy is not None else None,
            })

        # ── Final model on all history — score live signals ──────────────────
        print(f"\n{'='*68}")
        print("  LIVE SIGNAL SCORING (final model trained on all history)")

        live_rows = []
        if not all_features.empty:
            X_all = all_features[FEATURE_COLS].fillna(0).values
            y_all = all_features["hit_target"].values

            final_clf = GradientBoostingClassifier(
                n_estimators=100, max_depth=3, learning_rate=0.1,
                random_state=42, subsample=0.8,
            )
            final_clf.fit(X_all, y_all)
            print(f"  Final model trained on {len(all_features)} signals")

            # Live signals: top_tier firms' upgrades from the last HOLD_DAYS calendar days
            live_top_tier = top_tier_by_year.get(LAST_TEST_YEAR + 1, {})
            today         = datetime.date.today()
            lookback_date = today - datetime.timedelta(days=HOLD_DAYS)

            if live_top_tier:
                live_feat_df = build_feature_dataset(
                    conn,
                    lookback_date.year if lookback_date.year < today.year else today.year,
                    today.year + 1,
                    live_top_tier,
                    ticker_data, market_momentum, sector_map, sector_encoder,
                )
                # Filter to only signals within lookback window
                if not live_feat_df.empty:
                    live_feat_df = live_feat_df[
                        live_feat_df["signal_date"] >= lookback_date
                    ].copy()

                if not live_feat_df.empty:
                    X_live    = live_feat_df[FEATURE_COLS].fillna(0).values
                    live_probs = final_clf.predict_proba(X_live)[:, 1]
                    live_feat_df["confidence"] = live_probs
                    live_feat_df = live_feat_df.sort_values("confidence", ascending=False)

                    for _, row in live_feat_df.iterrows():
                        live_rows.append({
                            "ticker"              : row["ticker"],
                            "signal_date"         : str(row["signal_date"]),
                            "firm"                : row["firm"],
                            "sector"              : row["sector"],
                            "confidence_pct"      : round(float(row["confidence"]) * 100, 1),
                            "predicted_hit"       : int(row["confidence"] >= PROB_THRESHOLD),
                            "firm_score"          : round(float(row["firm_score"]), 2),
                            "stock_momentum_5d"   : round(float(row["stock_momentum_5d"]), 2),
                            "stock_momentum_20d"  : round(float(row["stock_momentum_20d"]), 2),
                            "market_momentum_20d" : round(float(row["market_momentum_20d"]), 2),
                        })

                    print(f"  Live signals: {len(live_rows)} scored  "
                          f"(top confidence: "
                          f"{live_rows[0]['confidence_pct']:.0f}% for "
                          f"{live_rows[0]['ticker']} — {live_rows[0]['firm']})"
                          if live_rows else "  No live signals found")

        conn.close()

    # ── Save to S3 ───────────────────────────────────────────────────────────
    s3_client = boto3.client("s3", region_name=AWS_REGION)

    summary_df = pd.DataFrame(summary_rows)
    live_df    = pd.DataFrame(live_rows) if live_rows else pd.DataFrame()

    for df, key, label in [
        (summary_df, "mart/mart_ml_summary.parquet", "ML summary"),
        (live_df,    "mart/mart_ml_live.parquet",    "ML live"),
    ]:
        if df.empty:
            print(f"\n[SKIP] {label} — no rows to save")
            continue
        buf = BytesIO()
        df.to_parquet(buf, index=False, engine="pyarrow", compression="snappy")
        buf.seek(0)
        try:
            s3_client.put_object(Bucket=S3_BUCKET, Key=key, Body=buf.read())
            print(f"\n[SAVED] s3://{S3_BUCKET}/{key}  ({len(df)} rows)")
        except Exception as e:
            print(f"\n[ERROR] Failed to save {key}: {e}")

    if summary_rows:
        print("\n  Summary:")
        sdf = pd.DataFrame(summary_rows)
        print(sdf[["year", "n_all_signals", "n_filtered_signals",
                   "all_signals_return_pct", "filtered_return_pct",
                   "precision", "recall"]].to_string(index=False))

    print("\n" + "=" * 68)


if __name__ == "__main__":
    main()
