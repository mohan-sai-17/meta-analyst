"""
Block 2: The Seasonality Filter ("The Weatherman")  [Cloud Track]
=================================================================
Analyzes historical monthly seasonality for a given stock via the Lambda API.
Data source: mart_seasonality (pre-computed from S3 OHLC Parquet).

Usage:
    analyze_seasonality(ticker, target_month)  ->  dict
"""

import os
import warnings
import requests

warnings.filterwarnings('ignore')

# ============================================================================
# CONFIGURATION
# ============================================================================

API_BASE_URL = os.environ.get(
    "API_BASE_URL",
    "https://YOUR_LAMBDA_URL.lambda-url.us-east-1.on.aws"
)

CALENDAR = {
    1: "January",  2: "February", 3: "March",    4: "April",
    5: "May",      6: "June",     7: "July",      8: "August",
    9: "September",10: "October", 11: "November", 12: "December"
}


# ============================================================================
# CORE FUNCTION
# ============================================================================

def analyze_seasonality(ticker, target_month):
    """
    Analyse the historical seasonality of a stock for a given month.

    Parameters
    ----------
    ticker       : str   — e.g. 'AAPL'
    target_month : int   — 1 (January) ... 12 (December)

    Returns
    -------
    dict with keys: Month, Average_Return, Win_Rate
    """
    resp = requests.get(
        f"{API_BASE_URL}/seasonality/{ticker.upper()}",
        timeout=10
    )
    if resp.status_code == 404:
        raise ValueError(f"No seasonality data found for '{ticker}'.")
    resp.raise_for_status()

    rows = resp.json()
    match = next((r for r in rows if r["month"] == target_month), None)

    if match is None:
        raise ValueError(
            f"No data for month {target_month} in seasonality results for '{ticker}'."
        )

    print(f"[INFO] Ticker      : {ticker.upper()}")
    print(f"[INFO] Target Month: {CALENDAR[target_month]} ({target_month})")

    return {
        "Month"          : match["month"],
        "Average_Return" : round(float(match["avg_return"]), 6),
        "Win_Rate"       : round(float(match["win_rate"]), 6),
    }


# ============================================================================
# TEST BLOCK
# ============================================================================

if __name__ == "__main__":

    TICKER       = 'AAPL'
    TARGET_MONTH = 9          # September

    print("=" * 55)
    print("  BLOCK 2: THE SEASONALITY FILTER (The Weatherman)")
    print("=" * 55 + "\n")

    result = analyze_seasonality(TICKER, TARGET_MONTH)

    print("-" * 55)
    print(f"  SEASONALITY REPORT")
    print("-" * 55)
    print(f"  Ticker         : {TICKER}")
    print(f"  Month          : {CALENDAR[result['Month']]} ({result['Month']})")
    print(f"  Average Return : {result['Average_Return'] * 100:.2f}%")
    print(f"  Win Rate       : {result['Win_Rate'] * 100:.1f}%")
    print("-" * 55)

    avg = result['Average_Return']
    win = result['Win_Rate']

    if avg > 0 and win >= 0.6:
        verdict = "BULLISH  - Historically a strong month. Seasonality favours a long."
    elif avg < 0 and win <= 0.4:
        verdict = "BEARISH  - Historically a weak month. Seasonality warns against a long."
    else:
        verdict = "NEUTRAL  - Mixed historical signals. Seasonality is inconclusive."

    print(f"\n  Verdict: {verdict}")
    print("=" * 55)
