"""
api/main.py
============
Meta-Analyst REST API — Lambda + DuckDB + S3.

Reads pre-built mart Parquet files from S3 via DuckDB.
Mart tables are cached in DuckDB memory on Lambda cold start.
Raw ohlc/ratings are queried on-demand (views, not loaded into memory).

Endpoints:
  GET /health
  GET /tickers
  GET /ohlc/{ticker}?days=730
  GET /ratings/{ticker}
  GET /scorecard/{ticker}
  GET /top-tier/{ticker}
  GET /price-targets/{ticker}
  GET /seasonality/{ticker}
  GET /friends/{ticker}
  GET /signals
  GET /sector-candidates
  GET /candidates
  GET /backtest

Deployed on AWS Lambda via container image. Uses Mangum as the
ASGI adapter. Lambda execution role provides S3 read access —
no key files needed at runtime.
"""

import os
import json
import math
import logging
import duckdb
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware
from mangum import Mangum

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

S3_BUCKET  = os.environ.get("S3_BUCKET", "meta-analyst-data-lake-YOUR_AWS_ACCOUNT_ID")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# DuckDB extension directory — must be writable on Lambda.
# Lambda's /tmp is the only writable path at runtime.
# We pre-install httpfs into /tmp/duckdb_ext before the first connection.
os.environ.setdefault("DUCKDB_EXTENSION_DIRECTORY", "/tmp/duckdb_ext")
os.environ.setdefault("HOME", "/tmp")   # DuckDB extension system needs HOME on Lambda

app = FastAPI(
    title="Meta-Analyst API",
    version="2.0.0",
    description="Serves DuckDB/S3 mart data for the Meta-Analyst trading dashboard.",
)
app.add_middleware(GZipMiddleware, minimum_size=1000)

# Lambda handler — wraps FastAPI for AWS Lambda + Function URL
# CORS is handled by the Lambda Function URL (Terraform cors{} block) —
# no need for FastAPI CORSMiddleware, which would add a duplicate
# access-control-allow-origin header and cause browser fetch failures.
handler = Mangum(app, lifespan="off")


# ============================================================================
# DUCKDB — module-level cache (persists across warm Lambda invocations)
# ============================================================================

_conn = None

MART_TABLES = [
    "mart_firm_scorecard",
    "mart_top_tier",
    "mart_price_targets",
    "mart_sector_candidates",
    "mart_seasonality",
    "mart_recent_signals",
    "mart_daily_candidates",
    "mart_backtest_wf",
    "mart_backtest_stock",
    "mart_backtest_v2",
    "mart_fundamental_health",   # Phase 1 — financial health gate
    "mart_insider_summary",      # Phase 3 — EDGAR insider signals
    "mart_backtest_reinvest",    # Compounder — sequential 5% position sizing
    "mart_wfo",                  # Walk-Forward Optimizer results
    "mart_ml_summary",           # ML signal filter — per-year summary
    "mart_ml_live",              # ML signal filter — live ranked signals
    "mart_combo_perf",           # Combo memory — (firm x sector x month) leaderboard
    "mart_combo_summary",        # Combo memory — walk-forward summary
    "mart_combo_live",           # Combo memory — live signals scored
]


def _load_extension(conn: duckdb.DuckDBPyConnection, name: str) -> None:
    """
    Load a DuckDB extension, installing it first if needed.

    Lambda's filesystem is read-only except for /tmp.  Extensions are
    pre-baked into the Docker image or installed to /tmp/duckdb_ext
    at first cold start.
    """
    try:
        conn.execute(f"LOAD {name};")
        logger.info(f"{name} loaded.")
    except Exception:
        logger.info(f"{name} not found locally — installing ...")
        os.makedirs("/tmp/duckdb_ext", exist_ok=True)
        conn.execute(f"INSTALL {name};")
        conn.execute(f"LOAD {name};")
        logger.info(f"{name} installed and loaded.")


def get_conn() -> duckdb.DuckDBPyConnection:
    """
    Return the cached DuckDB connection.
    On first call (cold start): configure S3 credentials and create views.
    Mart tables are loaded lazily — if S3 files are missing, endpoints that
    need them will return 503; /health always works regardless.
    """
    global _conn
    if _conn is not None:
        return _conn

    logger.info("Cold start — initialising DuckDB connection...")
    conn = duckdb.connect()
    conn.execute("SET home_directory='/tmp';")
    _load_extension(conn, "httpfs")
    _load_extension(conn, "aws")

    # Prefer CREDENTIAL_CHAIN (uses IAM role via AWS SDK).
    # Fallback to CONFIG with Lambda's auto-injected env vars.
    try:
        conn.execute(f"""
            CREATE SECRET (
                TYPE S3,
                PROVIDER CREDENTIAL_CHAIN,
                REGION '{AWS_REGION}'
            )
        """)
        logger.info("S3 secret created via CREDENTIAL_CHAIN.")
    except Exception as exc:
        logger.warning(f"CREDENTIAL_CHAIN failed ({exc}), falling back to CONFIG.")
        conn.execute(f"""
            CREATE SECRET (
                TYPE S3,
                PROVIDER CONFIG,
                KEY_ID '{os.environ.get("AWS_ACCESS_KEY_ID", "")}',
                SECRET '{os.environ.get("AWS_SECRET_ACCESS_KEY", "")}',
                SESSION_TOKEN '{os.environ.get("AWS_SESSION_TOKEN", "")}',
                REGION '{AWS_REGION}'
            )
        """)
        logger.info("S3 secret created via CONFIG (env vars).")

    # Load mart tables from S3 — skip gracefully if files not yet available
    # (pipeline hasn't run yet).  Affected endpoints return 503.
    for mart in MART_TABLES:
        try:
            conn.execute(f"""
                CREATE TABLE {mart} AS
                SELECT * FROM read_parquet('s3://{S3_BUCKET}/mart/{mart}.parquet')
            """)
            logger.info(f"Loaded {mart}")
        except Exception as exc:
            logger.warning(f"Skipping {mart} — not available yet: {exc}")
            # Create empty placeholder so queries don't crash with NameError
            conn.execute(f"CREATE TABLE IF NOT EXISTS {mart} AS SELECT 1 WHERE 1=0")

    # Raw tables as views — scanned on-demand per request (not loaded into memory).
    # Each view is created independently so one failure doesn't block others.
    raw_views = {
        "raw_ohlc": f"""
            CREATE VIEW raw_ohlc AS
            SELECT Ticker, Date::DATE AS Date, Open, High, Low, Close, Volume
            FROM read_parquet('s3://{S3_BUCKET}/raw/ohlc/*/*.parquet', hive_partitioning=false)
            WHERE Ticker IS NOT NULL AND Close IS NOT NULL AND Close > 0
            QUALIFY ROW_NUMBER() OVER (PARTITION BY Ticker, Date::DATE ORDER BY Date::DATE) = 1
        """,
        "raw_ratings": f"""
            CREATE VIEW raw_ratings AS
            SELECT Ticker, GradeDate::DATE AS GradeDate, Firm, Action, ToGrade, FromGrade
            FROM read_parquet('s3://{S3_BUCKET}/raw/ratings/*/*.parquet')
            WHERE Action IN ('up', 'init') AND Ticker IS NOT NULL
        """,
        "raw_stocks": f"""
            CREATE VIEW raw_stocks AS
            SELECT Ticker, Stock_Name, Friends
            FROM read_parquet('s3://{S3_BUCKET}/raw/stocks_list/*/*.parquet')
        """,
    }
    for view_name, view_sql in raw_views.items():
        try:
            conn.execute(view_sql)
            logger.info(f"Created view {view_name}")
        except Exception as exc:
            logger.warning(f"View {view_name} failed — {exc}")
            conn.execute(f"CREATE TABLE IF NOT EXISTS {view_name} AS SELECT 1 WHERE 1=0")

    _conn = conn
    logger.info("DuckDB ready.")
    return _conn


# ============================================================================
# HELPERS
# ============================================================================

def _safe(val):
    """Convert a DuckDB value to a JSON-safe Python type."""
    if val is None:
        return None
    if isinstance(val, float) and math.isnan(val):
        return None
    if hasattr(val, "isoformat"):   # date / datetime
        return val.isoformat()
    return val


def query(sql: str, params: list = None) -> list:
    """Execute DuckDB SQL and return JSON-safe list of dicts."""
    conn = get_conn()
    rel = conn.execute(sql, params or [])
    columns = [desc[0] for desc in rel.description]
    return [
        {col: _safe(val) for col, val in zip(columns, row)}
        for row in rel.fetchall()
    ]


# ============================================================================
# ROUTES
# ============================================================================

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/tickers")
def get_tickers():
    """All tickers and company names."""
    return query("SELECT Ticker AS ticker, Stock_Name AS stock_name FROM raw_stocks ORDER BY Ticker")


@app.get("/ohlc/{ticker}")
def get_ohlc(
    ticker: str,
    days: int = Query(730, ge=30, le=3650),
):
    """OHLC history for one ticker (default 2 years)."""
    result = query("""
        SELECT
            Ticker,
            Date::VARCHAR AS Date,
            Open, High, Low, Close, Volume
        FROM raw_ohlc
        WHERE Ticker = ?
          AND Date >= CURRENT_DATE - INTERVAL (?) DAY
        ORDER BY Date
    """, [ticker.upper(), days])
    if not result:
        raise HTTPException(404, f"No OHLC data for '{ticker}'.")
    return result


@app.get("/ratings/{ticker}")
def get_ratings(ticker: str):
    """All analyst upgrade/initiation signals for one ticker."""
    return query("""
        SELECT
            Ticker,
            GradeDate::VARCHAR AS GradeDate,
            Firm, Action, ToGrade, FromGrade
        FROM raw_ratings
        WHERE Ticker = ?
        ORDER BY GradeDate DESC
    """, [ticker.upper()])


@app.get("/scorecard/{ticker}")
def get_scorecard(ticker: str):
    """Firm-level scorecard for one ticker — avg returns and win rates per firm."""
    return query("""
        SELECT Ticker, Firm, signal_count,
               avg_return_3m, avg_return_6m, win_rate_3m, win_rate_6m
        FROM mart_firm_scorecard
        WHERE Ticker = ?
        ORDER BY avg_return_6m DESC
    """, [ticker.upper()])


@app.get("/top-tier/{ticker}")
def get_top_tier(ticker: str):
    """Top Tier firms for one ticker (win_rate > 50%, positive returns, >= 2 signals)."""
    return query("""
        SELECT Ticker, Firm, signal_count,
               avg_return_3m, avg_return_6m, win_rate_3m, win_rate_6m
        FROM mart_top_tier
        WHERE Ticker = ?
        ORDER BY avg_return_6m DESC
    """, [ticker.upper()])


@app.get("/price-targets")
def get_all_price_targets():
    """All tickers with Oracle price targets — used for bulk prefilter in Top Picks scan."""
    return query("""
        SELECT
            Ticker,
            current_price,
            price_date::VARCHAR AS price_date,
            top_tier_firm_count,
            expected_return_3m_pct,
            expected_return_6m_pct,
            avg_win_rate_3m,
            avg_win_rate_6m,
            target_price_3m,
            target_price_6m
        FROM mart_price_targets
        ORDER BY expected_return_3m_pct DESC
    """)


@app.get("/price-targets/{ticker}")
def get_price_targets(ticker: str):
    """Oracle price targets for one ticker — 3M and 6M from Top Tier averages."""
    result = query("""
        SELECT
            Ticker,
            current_price,
            price_date::VARCHAR AS price_date,
            top_tier_firm_count,
            expected_return_3m_pct,
            expected_return_6m_pct,
            avg_win_rate_3m,
            avg_win_rate_6m,
            target_price_3m,
            target_price_6m
        FROM mart_price_targets
        WHERE Ticker = ?
    """, [ticker.upper()])
    if not result:
        raise HTTPException(404, f"No Top Tier coverage for '{ticker}'.")
    return result[0]


@app.get("/seasonality/{ticker}")
def get_seasonality(ticker: str, period: str = Query("monthly")):
    """
    Seasonality for one ticker.
    period=monthly  — win rate + avg return by calendar month (from mart_seasonality).
    period=weekly   — win rate + avg return by day of week Mon-Fri (from raw_ohlc).
    """
    if period == "weekly":
        result = query("""
            WITH returns AS (
                SELECT
                    WEEKOFYEAR(Date) AS week_num,
                    (Close / LAG(Close) OVER (PARTITION BY Ticker ORDER BY Date) - 1) * 100 AS daily_return
                FROM raw_ohlc
                WHERE Ticker = ?
            )
            SELECT
                week_num,
                COUNT(*)                                                                    AS count,
                ROUND(AVG(daily_return), 3)                                                 AS avg_return,
                ROUND(SUM(CASE WHEN daily_return > 0 THEN 1.0 ELSE 0.0 END)
                      * 100.0 / COUNT(*), 1)                                                AS win_rate
            FROM returns
            WHERE daily_return IS NOT NULL
              AND week_num BETWEEN 1 AND 52
            GROUP BY week_num
            ORDER BY week_num
        """, [ticker.upper()])
        if not result:
            raise HTTPException(404, f"No OHLC data for '{ticker}'.")
        return result
    else:
        result = query("""
            SELECT Ticker, month, years_observed, win_rate, avg_return
            FROM mart_seasonality
            WHERE Ticker = ?
            ORDER BY month
        """, [ticker.upper()])
        if not result:
            raise HTTPException(404, f"No seasonality data for '{ticker}'.")
        return result


@app.get("/friends/{ticker}")
def get_friends(ticker: str):
    """Underwriter relationships for one ticker (from stocks_list.Friends)."""
    result = query("""
        SELECT Ticker, Friends
        FROM raw_stocks
        WHERE Ticker = ?
    """, [ticker.upper()])
    if not result:
        raise HTTPException(404, f"Ticker '{ticker}' not found.")
    return {"ticker": ticker.upper(), "friends": result[0].get("Friends") or ""}


@app.get("/signals")
def get_signals():
    """Top Tier signals from the last 90 days with current price and targets."""
    return query("""
        SELECT
            Ticker, Firm,
            GradeDate::VARCHAR AS GradeDate,
            Action, ToGrade,
            current_price,
            price_date::VARCHAR AS price_date,
            avg_return_3m, avg_return_6m, win_rate_6m, est_target_3m
        FROM mart_recent_signals
        ORDER BY GradeDate DESC
    """)


@app.get("/sector-candidates")
def get_sector_candidates():
    """
    Pre-screened non-tech Oracle-green tickers: Top Tier coverage + positive 3M target.
    Sector comes from stocks_list (run bootstrap_sectors_s3.py once to populate).
    Dashboard fetches live option chains only for these pre-filtered tickers.
    """
    return query("""
        SELECT
            Ticker,
            Stock_Name,
            Sector,
            current_price,
            target_3m,
            target_6m,
            top_tier_firm_count,
            expected_return_3m_pct,
            expected_return_6m_pct,
            avg_win_rate_3m,
            avg_win_rate_6m,
            hist_vol
        FROM mart_sector_candidates
        ORDER BY Sector, expected_return_3m_pct DESC
    """)


@app.get("/options/{ticker}")
def scan_options(ticker: str, target: float = Query(...), price: float = Query(...)):
    """
    Scan for the best underpriced call option for a ticker.
    Uses yfinance for live chain data + Black-Scholes fair value.
    Returns the best call by ROI, or null if none qualifies.
    """
    import math
    import yfinance as yf
    import pandas as pd

    RISK_FREE   = 0.043
    HORIZON     = 90      # target DTE
    MIN_DTE     = 60
    MAX_DTE     = 135

    def norm_cdf(x: float) -> float:
        a1, a2, a3, a4, a5, p = 0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429, 0.3275911
        sign = 1 if x >= 0 else -1
        x = abs(x)
        t = 1.0 / (1.0 + p * x)
        y = 1.0 - (((((a5 * t + a4) * t + a3) * t + a2) * t + a1) * t) * math.exp(-x * x)
        return 0.5 * (1.0 + sign * y)

    def bs_call(S: float, K: float, T: float, r: float, sigma: float) -> float:
        if T <= 0 or sigma <= 0:
            return max(S - K, 0)
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)

    try:
        ticker = ticker.upper()
        obj    = yf.Ticker(ticker)

        # Estimate sigma from 1Y OHLC
        hist = obj.history(period="1y")
        if hist.empty or len(hist) < 20:
            return None
        sigma = float(hist["Close"].pct_change().dropna().tail(252).std() * math.sqrt(252))

        expirations = obj.options
        if not expirations:
            return None

        today = pd.Timestamp.today().normalize()
        valid = [e for e in expirations
                 if MIN_DTE <= (pd.Timestamp(e) - today).days <= MAX_DTE]
        if not valid:
            return None

        best = None
        for exp in valid[:3]:
            dte   = max((pd.Timestamp(exp) - today).days, 1)
            T     = dte / 365.0
            chain = obj.option_chain(exp).calls
            chain = chain[
                (chain["strike"] >= price * 0.90) &
                (chain["strike"] <= target * 1.05) &
                (chain["ask"]    >  0)
            ]
            for _, row in chain.iterrows():
                fv  = bs_call(price, float(row["strike"]), T, RISK_FREE, sigma)
                ask = float(row["ask"])
                if fv <= ask * 1.05:
                    continue
                roi = (target - float(row["strike"]) - ask) / ask * 100
                if best is None or roi > best["roi"]:
                    best = {
                        "ticker"    : ticker,
                        "price"     : round(price,  2),
                        "target3m"  : round(target, 2),
                        "upside"    : round((target / price - 1) * 100, 1),
                        "strike"    : float(row["strike"]),
                        "expiry"    : exp,
                        "dte"       : dte,
                        "ask"       : round(ask, 2),
                        "fair_value": round(fv, 4),
                        "roi"       : round(roi, 1),
                    }

        return best  # None if no qualifying call found

    except Exception as e:
        logger.warning(f"options scan failed for {ticker}: {e}")
        return None


@app.get("/scan-options")
def scan_all_options():
    """
    Server-side bulk scan: pre-computes sigma from S3 OHLC (one DuckDB query),
    then fans out option-chain fetches in parallel. Scans top 100 tickers by
    expected 3-month return. Returns all underpriced calls sorted by ROI.
    """
    import math
    import yfinance as yf
    import pandas as pd
    from concurrent.futures import ThreadPoolExecutor, as_completed

    RISK_FREE       = 0.043
    MIN_DTE         = 60
    MAX_DTE         = 135
    WORKERS         = 15
    TOP_N           = 40    # keep under API Gateway's 29s hard timeout
    TICKER_TIMEOUT  = 8     # seconds per ticker before giving up

    def norm_cdf(x: float) -> float:
        a1, a2, a3, a4, a5, p = 0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429, 0.3275911
        sign = 1 if x >= 0 else -1
        x = abs(x)
        t = 1.0 / (1.0 + p * x)
        y = 1.0 - (((((a5*t+a4)*t+a3)*t+a2)*t+a1)*t) * math.exp(-x*x)
        return 0.5 * (1.0 + sign * y)

    def bs_call(S: float, K: float, T: float, r: float, sigma: float) -> float:
        if T <= 0 or sigma <= 0:
            return max(S - K, 0)
        d1 = (math.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*math.sqrt(T))
        d2 = d1 - sigma*math.sqrt(T)
        return S*norm_cdf(d1) - K*math.exp(-r*T)*norm_cdf(d2)

    # Step 1: get top-N targets from mart (fast, already in DuckDB memory)
    try:
        rows = get_conn().execute(f"""
            SELECT Ticker, current_price, target_price_3m
            FROM mart_price_targets
            WHERE target_price_3m > current_price
            ORDER BY expected_return_3m_pct DESC
            LIMIT {TOP_N}
        """).fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    tickers     = [r[0] for r in rows]
    price_map   = {r[0]: float(r[1]) for r in rows}
    target_map  = {r[0]: float(r[2]) for r in rows}

    # Step 2: batch-download 6-month history for all tickers in ONE yfinance call
    try:
        hist_all = yf.download(tickers, period="6mo", auto_adjust=True, progress=False)
        closes   = hist_all["Close"] if len(tickers) > 1 else hist_all[["Close"]]
        sigmas   = {}
        for t in tickers:
            try:
                s = closes[t].dropna().pct_change().dropna()
                if len(s) >= 20:
                    sigmas[t] = float(s.std() * math.sqrt(252))
            except Exception:
                pass
    except Exception:
        sigmas = {}   # fall back to default sigma per ticker if batch fails

    def scan_ticker(ticker: str):
        price  = price_map[ticker]
        target = target_map[ticker]
        sigma  = sigmas.get(ticker, 0.30)   # 30% default vol if history unavailable
        try:
            obj = yf.Ticker(ticker)
            expirations = obj.options
            if not expirations:
                return None
            today = pd.Timestamp.today().normalize()
            valid = [e for e in expirations
                     if MIN_DTE <= (pd.Timestamp(e) - today).days <= MAX_DTE]
            if not valid:
                return None
            best = None
            for exp in valid[:3]:
                dte   = max((pd.Timestamp(exp) - today).days, 1)
                T     = dte / 365.0
                chain = obj.option_chain(exp).calls
                chain = chain[
                    (chain["strike"] >= price * 0.90) &
                    (chain["strike"] <= target * 1.05) &
                    (chain["ask"]    >  0)
                ]
                for _, row in chain.iterrows():
                    fv  = bs_call(price, float(row["strike"]), T, RISK_FREE, sigma)
                    ask = float(row["ask"])
                    if fv <= ask * 1.05:
                        continue
                    roi = (target - float(row["strike"]) - ask) / ask * 100
                    if best is None or roi > best["roi"]:
                        best = {
                            "ticker"    : ticker,
                            "price"     : round(price, 2),
                            "target3m"  : round(target, 2),
                            "upside"    : round((target/price - 1)*100, 1),
                            "strike"    : float(row["strike"]),
                            "expiry"    : exp,
                            "dte"       : dte,
                            "ask"       : round(ask, 2),
                            "fair_value": round(fv, 4),
                            "roi"       : round(roi, 1),
                        }
        except Exception as e:
            logger.debug(f"scan_ticker {ticker}: {e}")
        return best

    # Step 3: parallel option-chain fetches only
    results = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(scan_ticker, t): t for t in tickers}
        for fut in as_completed(futures, timeout=TICKER_TIMEOUT * len(tickers)):
            try:
                pick = fut.result(timeout=TICKER_TIMEOUT)
                if pick and pick["roi"] > 0:
                    results.append(pick)
            except Exception:
                pass

    return sorted(results, key=lambda x: x["roi"], reverse=True)


@app.get("/fundamentals")
def get_all_fundamentals():
    """
    Financial health snapshot for all tickers.
    Returns debt_equity, current_ratio, interest_coverage, and passes_health_screen.
    Data is populated daily by update_fundamentals_s3.py.
    """
    try:
        return query("""
            SELECT
                ticker, as_of_date::VARCHAR AS as_of_date,
                debt_equity, current_ratio, interest_coverage,
                fcf_positive_years, passes_health_screen
            FROM mart_fundamental_health
            ORDER BY ticker
        """)
    except Exception:
        return []


@app.get("/fundamentals/{ticker}")
def get_fundamentals(ticker: str):
    """Full financial health detail for one ticker."""
    result = query("""
        SELECT
            ticker,
            as_of_date::VARCHAR AS as_of_date,
            debt_equity,
            current_ratio,
            interest_coverage,
            fcf_yr0, fcf_yr1, fcf_yr2,
            fcf_positive_years,
            ni_yr0, ni_yr1,
            passes_health_screen
        FROM mart_fundamental_health
        WHERE ticker = ?
    """, [ticker.upper()])
    if not result:
        raise HTTPException(404, f"No fundamental data for '{ticker}'.")
    return result[0]


@app.get("/insider")
def get_all_insider():
    """
    Insider transaction summary for all tickers (last 90 days).
    Data is populated daily by update_insider_s3.py from SEC EDGAR Form 4.
    """
    try:
        return query("""
            SELECT
                ticker,
                as_of_date::VARCHAR AS as_of_date,
                net_insider_buy_90d,
                insider_buy_count_90d,
                insider_sell_count_90d,
                last_transaction_date,
                insider_buy_flag,
                insider_strength
            FROM mart_insider_summary
            WHERE insider_strength NOT IN ('no_data')
            ORDER BY net_insider_buy_90d DESC NULLS LAST
        """)
    except Exception:
        return []


@app.get("/insider/{ticker}")
def get_insider(ticker: str):
    """Insider transaction detail for one ticker."""
    result = query("""
        SELECT
            ticker,
            as_of_date::VARCHAR AS as_of_date,
            net_insider_buy_90d,
            insider_buy_count_90d,
            insider_sell_count_90d,
            last_transaction_date,
            insider_buy_flag,
            insider_strength
        FROM mart_insider_summary
        WHERE ticker = ?
    """, [ticker.upper()])
    if not result:
        raise HTTPException(404, f"No insider data for '{ticker}'.")
    return result[0]


@app.get("/backtest")
def get_backtest():
    """Walk-forward backtest results by year (5x leveraged options simulation)."""
    try:
        return query("SELECT * FROM mart_backtest_wf ORDER BY year")
    except Exception:
        return []  # mart not yet populated — backtest_wf.py hasn't run


@app.get("/backtest2")
def get_backtest2():
    """Stock-only walk-forward backtest results (no leverage, 10 years)."""
    try:
        return query("SELECT * FROM mart_backtest_stock ORDER BY year")
    except Exception:
        return []  # mart not yet populated — backtest_stock.py hasn't run


@app.get("/backtest-v2")
def get_backtest_v2():
    """Unified backtest: all hold periods x all leverages x 10 years."""
    try:
        return query("""
            SELECT year, hold_days, leverage, top_tier_firms, trades,
                   win_rate, avg_pct_return, strategy_return, spy_return, alpha
            FROM mart_backtest_v2
            ORDER BY year, hold_days, leverage
        """)
    except Exception:
        return []


@app.get("/backtest-reinvest")
def get_backtest_reinvest():
    """
    Compounder backtest: sequential 5% position sizing, 2015–2024.
    One row per closed trade; portfolio_value_after enables equity-curve reconstruction.
    """
    try:
        return query("""
            SELECT
                year, entry_date, exit_date, ticker, signal_type,
                entry_price, exit_price, pct_return, exit_reason,
                expected_return_pct, profit_target_pct, hold_days,
                portfolio_value_after, spy_return_yr, strategy
            FROM mart_backtest_reinvest
            ORDER BY exit_date, entry_date
        """)
    except Exception:
        return []


@app.get("/wfo")
def get_wfo():
    """Walk-Forward Optimizer: best (take_profit, hold_days) per year vs fixed baseline."""
    try:
        return query("""
            SELECT year, best_take_profit_pct, best_hold_days, train_score_pct,
                   test_return_pct, fixed_test_return_pct,
                   test_trades, test_win_rate_pct,
                   fixed_trades, fixed_win_rate_pct, spy_return_pct
            FROM mart_wfo
            ORDER BY year
        """)
    except Exception:
        return []


@app.get("/ml-signals")
def get_ml_signals():
    """ML signal filter: per-year filtered vs all-signals performance + feature importances."""
    try:
        return query("""
            SELECT year, n_all_signals, n_filtered_signals,
                   all_signals_return_pct, filtered_return_pct,
                   precision, recall,
                   feature_1, feature_1_importance,
                   feature_2, feature_2_importance,
                   feature_3, feature_3_importance,
                   spy_return_pct
            FROM mart_ml_summary
            ORDER BY year
        """)
    except Exception:
        return []


@app.get("/ml-live")
def get_ml_live():
    """ML signal filter: current live signals ranked by model confidence."""
    try:
        return query("""
            SELECT ticker, signal_date, firm, sector, confidence_pct, predicted_hit,
                   firm_score, stock_momentum_5d, stock_momentum_20d, market_momentum_20d
            FROM mart_ml_live
            ORDER BY confidence_pct DESC
        """)
    except Exception:
        return []


@app.get("/combo-perf")
def get_combo_perf():
    """Combo memory: (firm x sector x month) leaderboard sorted by hit rate."""
    try:
        return query("""
            SELECT firm, sector, month, n_signals, hit_rate_pct,
                   avg_return_pct, last_seen_year, trusted
            FROM mart_combo_perf
            ORDER BY hit_rate_pct DESC, n_signals DESC
        """)
    except Exception:
        return []


@app.get("/combo-summary")
def get_combo_summary():
    """Combo memory: walk-forward year-by-year filtered vs all-signals performance."""
    try:
        return query("""
            SELECT year, n_all_signals, n_filtered_signals,
                   all_return_pct, filtered_return_pct,
                   n_exact_match, n_firm_sector, n_firm_only, n_no_data,
                   spy_return_pct
            FROM mart_combo_summary
            ORDER BY year
        """)
    except Exception:
        return []


@app.get("/combo-live")
def get_combo_live():
    """Combo memory: live signals ranked by combo confidence."""
    try:
        return query("""
            SELECT ticker, signal_date, firm, sector, month,
                   hit_rate_pct, n_signals, avg_return_pct,
                   fallback_level, take_signal
            FROM mart_combo_live
            ORDER BY take_signal DESC, hit_rate_pct DESC
        """)
    except Exception:
        return []


@app.get("/candidates")
def get_candidates():
    """
    Pre-screened buy candidates: Top Tier upgrades from the last 3 days.
    Includes price targets, seasonality, historical vol, and bias flag.
    Falls back gracefully if mart was built before the near_earnings columns were added.
    """
    try:
        return query("""
            SELECT
                Ticker,
                Firm,
                GradeDate::VARCHAR AS GradeDate,
                Action,
                ToGrade,
                current_price,
                target_3m,
                target_6m,
                season_win_rate,
                season_avg_return,
                hist_vol,
                friends,
                is_biased,
                near_earnings,
                days_to_earnings,
                passes_health_screen,
                debt_equity,
                current_ratio,
                interest_coverage,
                insider_buy_flag,
                insider_strength,
                net_insider_buy_90d
            FROM mart_daily_candidates
            QUALIFY ROW_NUMBER() OVER (PARTITION BY Ticker ORDER BY GradeDate DESC) = 1
            ORDER BY GradeDate DESC, Ticker
        """)
    except Exception:
        # Mart predates fundamental columns — return with safe defaults until pipeline rebuilds it
        return query("""
            SELECT
                Ticker,
                Firm,
                GradeDate::VARCHAR AS GradeDate,
                Action,
                ToGrade,
                current_price,
                target_3m,
                target_6m,
                season_win_rate,
                season_avg_return,
                hist_vol,
                friends,
                is_biased,
                false AS near_earnings,
                NULL  AS days_to_earnings,
                NULL  AS passes_health_screen,
                NULL  AS debt_equity,
                NULL  AS current_ratio,
                NULL  AS interest_coverage,
                NULL  AS insider_buy_flag,
                NULL  AS insider_strength,
                NULL  AS net_insider_buy_90d
            FROM mart_daily_candidates
            QUALIFY ROW_NUMBER() OVER (PARTITION BY Ticker ORDER BY GradeDate DESC) = 1
            ORDER BY GradeDate DESC, Ticker
        """)
