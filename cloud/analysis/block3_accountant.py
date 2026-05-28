"""
Block 3: The Accountant (Options Math)  [Cloud Track]
======================================================
Implements Black-Scholes call pricing and compares theoretical fair value
against live market ask prices to identify potentially mispriced options.

Historical volatility is sourced from the Lambda API (/ohlc/{ticker}).
Live options chain is fetched directly from yfinance (external market data).

Functions:
    get_historical_volatility(ticker, days=252)  ->  float
    black_scholes_call(S, K, T, r, sigma)        ->  float
    find_cheap_calls(ticker, target_strike)      ->  dict
"""

import os
import warnings
import numpy as np
import pandas as pd
import yfinance as yf
import requests
from scipy.stats import norm
from datetime import datetime, date

warnings.filterwarnings('ignore')


# ============================================================================
# CONFIGURATION
# ============================================================================

API_BASE_URL   = os.environ.get(
    "API_BASE_URL",
    "https://YOUR_LAMBDA_URL.lambda-url.us-east-1.on.aws"
)
RISK_FREE_RATE = 0.043          # 4.3% — approximate current risk-free rate


# ============================================================================
# FUNCTION 1: Historical Volatility
# ============================================================================

def get_historical_volatility(ticker, days=252):
    """
    Calculate annualised historical volatility from OHLC data via the Lambda API.

    Parameters
    ----------
    ticker : str   — e.g. 'AAPL'
    days   : int   — lookback window (default 252 = 1 trading year)

    Returns
    -------
    float — annualised volatility (e.g. 0.28 = 28%)
    """
    resp = requests.get(
        f"{API_BASE_URL}/ohlc/{ticker.upper()}",
        params={"days": days},
        timeout=15
    )
    if resp.status_code == 404:
        raise ValueError(f"No OHLC data found for '{ticker}'.")
    resp.raise_for_status()

    rows = resp.json()
    if len(rows) < 2:
        raise ValueError(f"Insufficient OHLC data for '{ticker}' to compute volatility.")

    closes = pd.Series([r["Close"] for r in rows], dtype=float)
    daily_returns  = closes.pct_change().dropna()
    daily_std      = daily_returns.std()
    annualised_vol = daily_std * np.sqrt(252)

    return float(annualised_vol)


# ============================================================================
# FUNCTION 2: Black-Scholes Call Pricing
# ============================================================================

def black_scholes_call(S, K, T, r, sigma):
    """
    Calculate theoretical call option price using the Black-Scholes model.

    Parameters
    ----------
    S     : float — current stock price
    K     : float — option strike price
    T     : float — time to expiry in years (e.g. 30 days = 30/365)
    r     : float — risk-free rate (e.g. 0.043 for 4.3%)
    sigma : float — annualised volatility (e.g. 0.28 for 28%)

    Returns
    -------
    float — theoretical call option price
    """
    if T <= 0:
        return max(0.0, S - K)

    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)

    call_price = (S * norm.cdf(d1)) - (K * np.exp(-r * T) * norm.cdf(d2))

    return float(call_price)


# ============================================================================
# FUNCTION 3: The Bargain Hunter
# ============================================================================

def find_cheap_calls(ticker, target_strike):
    """
    Compare the Black-Scholes theoretical value of a call option against its
    live market ask price. Flags the option as cheap if fair value > ask.

    Parameters
    ----------
    ticker        : str   — e.g. 'AMD'
    target_strike : float — desired strike price

    Returns
    -------
    dict with keys:
        Ticker, Strike, Expiry, Market_Ask, Theoretical_Value, Is_Cheap
    """
    stock = yf.Ticker(ticker)

    # Current stock price
    hist = stock.history(period='2d')
    if hist.empty:
        raise ValueError(f"Could not fetch current price for '{ticker}'.")
    S = float(hist['Close'].iloc[-1])

    # Historical volatility from Lambda API
    sigma = get_historical_volatility(ticker)

    # Pick expiration date 30-45 days out (closest match)
    expirations = stock.options
    if not expirations:
        raise ValueError(f"No options data available for '{ticker}'.")

    today       = date.today()
    target_days = 37

    best_expiry = min(
        expirations,
        key=lambda d: abs((datetime.strptime(d, '%Y-%m-%d').date() - today).days - target_days)
    )

    expiry_date = datetime.strptime(best_expiry, '%Y-%m-%d').date()
    T = (expiry_date - today).days / 365.0

    # Fetch the call option chain for that expiry
    calls = stock.option_chain(best_expiry).calls

    if calls.empty:
        raise ValueError(f"No call options found for '{ticker}' expiring {best_expiry}.")

    calls = calls.copy()
    calls['strike_diff'] = (calls['strike'] - target_strike).abs()
    closest_row = calls.loc[calls['strike_diff'].idxmin()]

    matched_strike = float(closest_row['strike'])
    ask_price      = float(closest_row['ask'])

    fair_value = black_scholes_call(S, matched_strike, T, RISK_FREE_RATE, sigma)

    return {
        'Ticker'            : ticker,
        'Strike'            : matched_strike,
        'Expiry'            : str(expiry_date),
        'Days_To_Expiry'    : (expiry_date - today).days,
        'Current_Price'     : round(S, 2),
        'Market_Ask'        : round(ask_price, 2),
        'Theoretical_Value' : round(fair_value, 2),
        'Historical_Vol'    : f"{sigma * 100:.1f}%",
        'Is_Cheap'          : fair_value > ask_price
    }


# ============================================================================
# TEST BLOCK
# ============================================================================

if __name__ == "__main__":

    TICKER = 'AMD'

    print("=" * 60)
    print("  BLOCK 3: THE ACCOUNTANT (Options Math)")
    print("=" * 60)

    _hist = yf.Ticker(TICKER).history(period='2d')
    current_price = float(_hist['Close'].iloc[-1])
    target_strike = round(current_price * 1.05)

    print(f"\n  Ticker         : {TICKER}")
    print(f"  Current Price  : ${current_price:.2f}")
    print(f"  Target Strike  : ${target_strike:.2f} (~5% OTM)")
    print(f"  Risk-Free Rate : {RISK_FREE_RATE * 100:.1f}%\n")

    result = find_cheap_calls(TICKER, target_strike)

    print("-" * 60)
    print("  OPTIONS ANALYSIS REPORT")
    print("-" * 60)
    print(f"  Ticker              : {result['Ticker']}")
    print(f"  Current Price       : ${result['Current_Price']}")
    print(f"  Strike              : ${result['Strike']}")
    print(f"  Expiry              : {result['Expiry']}  ({result['Days_To_Expiry']} days)")
    print(f"  Historical Vol      : {result['Historical_Vol']}")
    print(f"  Market Ask          : ${result['Market_Ask']}")
    print(f"  Theoretical Value   : ${result['Theoretical_Value']}")
    print("-" * 60)

    if result['Is_Cheap']:
        verdict = "CHEAP  - Market ask is BELOW theoretical value. Potential buy."
    else:
        verdict = "RICH   - Market ask is ABOVE theoretical value. Overpriced."

    premium_diff = result['Theoretical_Value'] - result['Market_Ask']
    print(f"  Is Cheap            : {result['Is_Cheap']}")
    print(f"  Price Difference    : ${premium_diff:+.2f}")
    print(f"\n  Verdict: {verdict}")
    print("=" * 60)
