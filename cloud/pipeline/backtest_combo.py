"""
backtest_combo.py
=================
Combination Memory — learns which (firm × sector × month) patterns are
historically reliable and only takes signals where that exact combo (or a
coarser fallback) has proven itself in prior years.

Learning logic:
  If Goldman upgraded Technology stocks in March and it worked historically
  → next year Goldman + Tech + March signals are trusted.
  If it didn't → that combo is skipped.

Fallback chain (when sparse):
  Level 0 : (firm, sector, month)  — need n >= MIN_N and hit_rate >= THRESHOLD
  Level 1 : (firm, sector)         — same thresholds
  Level 2 : (firm) alone           — same thresholds
  No data → skip signal

Walk-forward: combo stats are computed from ALL prior years only.
  Test year data is never used to build the lookup.

Outputs:
  mart/mart_combo_perf.parquet    — learned memory across all history
  mart/mart_combo_summary.parquet — walk-forward year-by-year comparison
  mart/mart_combo_live.parquet    — live signals ranked by combo confidence

Usage:
    python aws/pipeline/backtest_combo.py
"""

import os
import bisect
import datetime
import warnings
import tempfile
from io import BytesIO
from collections import defaultdict

import boto3
import duckdb
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")


# ============================================================================
# CONFIGURATION
# ============================================================================

S3_BUCKET      = "meta-analyst-data-lake-YOUR_AWS_ACCOUNT_ID"
AWS_REGION     = "us-east-1"
PROFIT_TARGET  = 0.05     # label: hit +5%?
HOLD_DAYS      = 90       # within 90 calendar days
MIN_N          = 5        # minimum signals to trust a combo
THRESHOLD      = 0.55     # minimum hit rate to take the signal
MIN_EXP_RETURN = 1.5      # minimum firm expected return to be in top-tier

FIRST_TEST_YEAR = 2017    # need 2015+2016 as minimum training window
LAST_TEST_YEAR  = 2025


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
# TOP-TIER FIRMS  (same logic as backtest_reinvest.py)
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
# SIGNAL DATASET  (firm, sector, month, outcome)
# ============================================================================

def build_signal_rows(conn, year_start, year_end, top_tier_firms,
                      ticker_data, sector_map):
    """
    For every top-tier analyst signal in [year_start, year_end), compute:
      firm, sector, month, hit_target, actual_return, entry_price, entry_day
    Returns a list of dicts.
    """
    if not top_tier_firms:
        return []

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
        return []

    df["signal_date"]     = pd.to_datetime(df["signal_date"]).dt.date
    df["expected_return"] = df["firm"].map(top_tier_firms)
    df = df[df["expected_return"] >= MIN_EXP_RETURN].copy()
    df = (df.sort_values("expected_return", ascending=False)
            .drop_duplicates(subset=["ticker", "signal_date"]))

    rows = []
    for _, sig in df.iterrows():
        ticker   = sig["ticker"]
        sig_date = sig["signal_date"]
        firm     = sig["firm"]
        sector   = sector_map.get(ticker, "Unknown")

        td = ticker_data.get(ticker)
        if td is None:
            continue

        dates  = td["dates"]
        closes = td["closes"]

        # Entry = next trading day after signal date
        idx = bisect.bisect_right(dates, sig_date)
        if idx >= len(dates):
            continue
        entry_price = closes[idx]
        entry_day   = dates[idx]
        if entry_price <= 0:
            continue

        # Outcome: did it hit +5% within 90 calendar days?
        end_dt = entry_day + datetime.timedelta(days=HOLD_DAYS)
        lo     = bisect.bisect_right(dates, entry_day)
        hi     = bisect.bisect_right(dates, end_dt)
        window = closes[lo:hi]

        if len(window) == 0:
            continue

        hit = bool(np.any(window >= entry_price * (1.0 + PROFIT_TARGET)))
        ret = float(PROFIT_TARGET * 100) if hit else \
              float((window[-1] - entry_price) / entry_price * 100)

        rows.append({
            "ticker"     : ticker,
            "signal_date": sig_date,
            "year"       : sig_date.year,
            "firm"       : firm,
            "sector"     : sector,
            "month"      : sig_date.month,
            "hit_target" : int(hit),
            "ret_pct"    : round(ret, 3),
            "entry_price": round(entry_price, 4),
            "entry_day"  : entry_day,
        })

    return rows


# ============================================================================
# COMBO LOOKUP TABLE
# ============================================================================

def build_combo_lookup(signal_rows):
    """
    Given a list of signal dicts (from training years), build three lookup dicts:
      level0: {(firm, sector, month) -> {n, hit_rate, avg_ret}}
      level1: {(firm, sector)        -> {n, hit_rate, avg_ret}}
      level2: {firm                  -> {n, hit_rate, avg_ret}}
    """
    # Accumulate
    acc = defaultdict(lambda: {"hits": 0, "n": 0, "ret_sum": 0.0})

    for r in signal_rows:
        firm, sector, month = r["firm"], r["sector"], r["month"]
        hit, ret = r["hit_target"], r["ret_pct"]

        for key in [
            ("l0", firm, sector, month),
            ("l1", firm, sector),
            ("l2", firm),
        ]:
            acc[key]["n"]       += 1
            acc[key]["hits"]    += hit
            acc[key]["ret_sum"] += ret

    def _stats(entry):
        n = entry["n"]
        return {
            "n"       : n,
            "hit_rate": entry["hits"] / n,
            "avg_ret" : entry["ret_sum"] / n,
        }

    level0, level1, level2 = {}, {}, {}
    for key, entry in acc.items():
        if key[0] == "l0":
            level0[(key[1], key[2], key[3])] = _stats(entry)
        elif key[0] == "l1":
            level1[(key[1], key[2])]         = _stats(entry)
        else:
            level2[key[1]]                   = _stats(entry)

    return level0, level1, level2


def score_signal(firm, sector, month, level0, level1, level2):
    """
    Returns (hit_rate, n, avg_ret, fallback_level, take) for a signal.
    Tries level0 first, falls back to level1, then level2.
    Returns None if no sufficient data at any level.
    """
    for lvl, key in [
        (0, (firm, sector, month)),
        (1, (firm, sector)),
        (2, firm),
    ]:
        lookup = [level0, level1, level2][lvl]
        stats  = lookup.get(key)
        if stats and stats["n"] >= MIN_N:
            take = stats["hit_rate"] >= THRESHOLD
            return stats["hit_rate"], stats["n"], stats["avg_ret"], lvl, take

    return None   # no data


# ============================================================================
# RETURN SIMULATION  (equal-weight average across signals)
# ============================================================================

def simulate_return(signal_rows):
    """Mean return across a list of signal dicts (each has ret_pct)."""
    if not signal_rows:
        return 0.0, 0, 0
    rets    = [r["ret_pct"] for r in signal_rows]
    n_wins  = sum(1 for r in rets if r > 0)
    return float(np.mean(rets)), n_wins, len(rets)


# ============================================================================
# MAIN
# ============================================================================

def main():
    print()
    print("=" * 68)
    print("  COMBINATION MEMORY — (firm × sector × month) walk-forward filter")
    print(f"  Target     : +{int(PROFIT_TARGET*100)}% within {HOLD_DAYS}d")
    print(f"  Threshold  : hit_rate >= {int(THRESHOLD*100)}%  |  min n = {MIN_N}")
    print(f"  Fallback   : (firm,sector,month) -> (firm,sector) -> (firm)")
    print(f"  Test years : {FIRST_TEST_YEAR}–{LAST_TEST_YEAR}")
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

        ticker_data = {}
        for ticker, grp in ohlc_all.groupby("Ticker"):
            g = grp.sort_values("Date")
            ticker_data[ticker] = {
                "dates" : np.array(g["Date"].tolist()),
                "closes": np.array(g["Close"].values, dtype=float),
            }
        print(f"  {len(ticker_data)} tickers loaded")

        # Sector map
        sec_df     = conn.execute("SELECT DISTINCT Ticker, Sector FROM stg_stocks").fetchdf()
        sector_map = dict(zip(sec_df["Ticker"], sec_df["Sector"]))

        # Pre-compute top-tier firms for each test year
        print("\n[TRAIN] Computing top-tier firms per year...")
        top_tier_by_year = {}
        for yr in range(FIRST_TEST_YEAR, LAST_TEST_YEAR + 2):  # +2 includes live (2026)
            top_tier_by_year[yr] = compute_top_tier(conn, yr)
            print(f"  {yr}: {len(top_tier_by_year[yr])} top-tier firms")

        # SPY returns
        print("\n[SPY] Fetching annual SPY returns...")
        spy_by_year = {yr: get_spy_return(yr)
                       for yr in range(FIRST_TEST_YEAR, LAST_TEST_YEAR + 1)}

        # Build ALL historical signal rows (all years) — we slice by year in the loop
        print("\n[SIGNALS] Building full signal dataset (2015–2025)...")
        all_signal_rows = []
        for yr in range(2015, LAST_TEST_YEAR + 1):
            tt   = top_tier_by_year.get(yr + 1, {})   # top_tier from data BEFORE yr
            rows = build_signal_rows(conn, yr, yr + 1, tt, ticker_data, sector_map)
            all_signal_rows.extend(rows)
            hits = sum(r["hit_target"] for r in rows)
            print(f"  {yr}: {len(rows)} signals  "
                  f"hit_rate={hits/len(rows)*100:.0f}%" if rows else f"  {yr}: 0 signals")

        print(f"\n  Total: {len(all_signal_rows)} signals across all years")

        # ── Walk-forward ─────────────────────────────────────────────────────
        summary_rows = []

        for test_year in range(FIRST_TEST_YEAR, LAST_TEST_YEAR + 1):
            print(f"\n{'='*68}")
            print(f"  Test year: {test_year}")

            # Training = all signals strictly before test_year
            train_rows = [r for r in all_signal_rows if r["year"] < test_year]
            test_rows  = [r for r in all_signal_rows if r["year"] == test_year]

            if not train_rows:
                print("  [SKIP] No training data")
                continue
            if not test_rows:
                print("  [SKIP] No test signals")
                continue

            # Build combo lookup from training data
            level0, level1, level2 = build_combo_lookup(train_rows)

            # Score test signals
            filtered, skipped = [], []
            level_counts = {0: 0, 1: 0, 2: 0, -1: 0}  # -1 = no data

            for r in test_rows:
                result = score_signal(r["firm"], r["sector"], r["month"],
                                      level0, level1, level2)
                if result is None:
                    level_counts[-1] += 1
                    skipped.append(r)
                else:
                    hr, n, avg_ret, lvl, take = result
                    level_counts[lvl] += 1
                    if take:
                        filtered.append({**r, "combo_hit_rate": hr,
                                         "combo_n": n, "fallback_level": lvl})
                    else:
                        skipped.append(r)

            all_ret, all_wins, all_n     = simulate_return(test_rows)
            filt_ret, filt_wins, filt_n  = simulate_return(filtered)
            spy = spy_by_year.get(test_year)
            spy_str = f"{spy:+.1f}%" if spy is not None else "N/A"

            print(f"  All signals  : {all_n}  avg_ret={all_ret:+.2f}%  wins={all_wins}")
            print(f"  Combo filter : {filt_n}  avg_ret={filt_ret:+.2f}%  wins={filt_wins}")
            print(f"  SPY          : {spy_str}")
            print(f"  Level counts : exact={level_counts[0]}  "
                  f"firm+sector={level_counts[1]}  firm={level_counts[2]}  "
                  f"no_data={level_counts[-1]}")

            summary_rows.append({
                "year"              : test_year,
                "n_all_signals"     : all_n,
                "n_filtered_signals": filt_n,
                "all_return_pct"    : round(all_ret,  3),
                "filtered_return_pct": round(filt_ret, 3),
                "n_exact_match"     : level_counts[0],
                "n_firm_sector"     : level_counts[1],
                "n_firm_only"       : level_counts[2],
                "n_no_data"         : level_counts[-1],
                "spy_return_pct"    : round(spy, 2) if spy is not None else None,
            })

        # ── Build final combo leaderboard from ALL history ───────────────────
        print(f"\n{'='*68}")
        print("  Building combo leaderboard from all historical data...")

        all_level0, all_level1, all_level2 = build_combo_lookup(all_signal_rows)

        # Track last_seen_year for each combo
        last_seen: dict = defaultdict(int)
        for r in all_signal_rows:
            last_seen[(r["firm"], r["sector"], r["month"])] = max(
                last_seen[(r["firm"], r["sector"], r["month"])], r["year"]
            )

        perf_rows = []
        for (firm, sector, month), stats in all_level0.items():
            if stats["n"] < MIN_N:
                continue
            perf_rows.append({
                "firm"          : firm,
                "sector"        : sector,
                "month"         : month,
                "n_signals"     : stats["n"],
                "hit_rate_pct"  : round(stats["hit_rate"] * 100, 1),
                "avg_return_pct": round(stats["avg_ret"],   2),
                "last_seen_year": last_seen.get((firm, sector, month), 0),
                "trusted"       : int(stats["hit_rate"] >= THRESHOLD),
            })

        perf_df = pd.DataFrame(perf_rows).sort_values(
            ["hit_rate_pct", "n_signals"], ascending=[False, False]
        ) if perf_rows else pd.DataFrame()

        print(f"  {len(perf_df)} combos with n >= {MIN_N}")
        if not perf_df.empty:
            trusted = perf_df[perf_df["trusted"] == 1]
            print(f"  {len(trusted)} trusted (hit_rate >= {int(THRESHOLD*100)}%)")
            print(f"\n  Top 10 combos:")
            cols = ["firm", "sector", "month", "n_signals", "hit_rate_pct", "avg_return_pct"]
            print(trusted[cols].head(10).to_string(index=False))

        # ── Score live signals ───────────────────────────────────────────────
        print(f"\n{'='*68}")
        print("  Scoring live signals (last 90 days)...")

        live_top_tier = top_tier_by_year.get(LAST_TEST_YEAR + 1, {})
        today         = datetime.date.today()
        lookback_date = today - datetime.timedelta(days=HOLD_DAYS)

        live_signal_rows = build_signal_rows(
            conn,
            lookback_date.year if lookback_date.year < today.year else today.year,
            today.year + 1,
            live_top_tier,
            ticker_data,
            sector_map,
        )
        # Filter to within lookback window
        live_signal_rows = [r for r in live_signal_rows
                            if r["signal_date"] >= lookback_date]

        # Use the full-history lookup
        live_rows = []
        for r in live_signal_rows:
            result = score_signal(r["firm"], r["sector"], r["month"],
                                  all_level0, all_level1, all_level2)
            hr  = result[0] if result else None
            n   = result[1] if result else None
            avg = result[2] if result else None
            lvl = result[3] if result else -1
            take = result[4] if result else False

            live_rows.append({
                "ticker"          : r["ticker"],
                "signal_date"     : str(r["signal_date"]),
                "firm"            : r["firm"],
                "sector"          : r["sector"],
                "month"           : r["month"],
                "hit_rate_pct"    : round(hr * 100, 1)   if hr  is not None else None,
                "n_signals"       : n                     if n   is not None else None,
                "avg_return_pct"  : round(avg, 2)         if avg is not None else None,
                "fallback_level"  : lvl,
                "take_signal"     : int(take),
            })

        live_rows.sort(key=lambda x: (x["take_signal"], x["hit_rate_pct"] or 0), reverse=True)
        live_df = pd.DataFrame(live_rows) if live_rows else pd.DataFrame()

        if not live_df.empty:
            take_count = int(live_df["take_signal"].sum())
            print(f"  {len(live_df)} live signals found  |  {take_count} pass combo filter")
            if take_count:
                top = live_df[live_df["take_signal"] == 1].head(5)
                for _, row in top.iterrows():
                    lvl_label = ["exact", "firm+sector", "firm"][int(row["fallback_level"])] \
                                if row["fallback_level"] >= 0 else "no data"
                    print(f"    {row['ticker']:<6}  {row['firm']:<30}  "
                          f"{row['sector']:<20}  month={row['month']}  "
                          f"hit_rate={row['hit_rate_pct']}%  n={row['n_signals']}  "
                          f"[{lvl_label}]")

        conn.close()

    # ── Save to S3 ───────────────────────────────────────────────────────────
    s3_client    = boto3.client("s3", region_name=AWS_REGION)
    summary_df   = pd.DataFrame(summary_rows)

    for df, key, label in [
        (perf_df,    "mart/mart_combo_perf.parquet",    "combo leaderboard"),
        (summary_df, "mart/mart_combo_summary.parquet", "combo walk-forward summary"),
        (live_df,    "mart/mart_combo_live.parquet",    "combo live signals"),
    ]:
        if df is None or df.empty:
            print(f"\n[SKIP] {label} — no rows")
            continue
        buf = BytesIO()
        df.to_parquet(buf, index=False, engine="pyarrow", compression="snappy")
        buf.seek(0)
        try:
            s3_client.put_object(Bucket=S3_BUCKET, Key=key, Body=buf.read())
            print(f"\n[SAVED] s3://{S3_BUCKET}/{key}  ({len(df)} rows)")
        except Exception as e:
            print(f"\n[ERROR] Failed to save {key}: {e}")

    if not summary_df.empty:
        print("\n  Walk-forward summary:")
        print(summary_df[["year", "n_all_signals", "n_filtered_signals",
                           "all_return_pct", "filtered_return_pct",
                           "spy_return_pct"]].to_string(index=False))

    print("\n" + "=" * 68)


if __name__ == "__main__":
    main()
