"""
Block 4: The Master Loop and Risk Manager  [Cloud Track]
=========================================================
Orchestrates all three analytical blocks into a single trade evaluation
pipeline. Produces a formatted Trade Ticket with final action.

All data sourced from the Lambda API — no local SQL Server dependency.

Pipeline:
    Block 1 (Detective)  -> check_analyst_bias()     via /friends/{ticker}
    Block 2 (Weatherman) -> analyze_seasonality()    via /seasonality/{ticker}
    Block 3 (Accountant) -> find_cheap_calls()       via /ohlc/{ticker} + yfinance
    Block 4 (Bodyguard)  -> calculate_position_size()
"""

import io
import os
import sys
import warnings
import requests
import yfinance as yf
from math import floor
from datetime import datetime

# Allow imports from sibling modules in analysis/
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from block2_weatherman import analyze_seasonality, CALENDAR
from block3_accountant  import find_cheap_calls

warnings.filterwarnings('ignore')


# ============================================================================
# CONFIGURATION
# ============================================================================

API_BASE_URL = os.environ.get(
    "API_BASE_URL",
    "https://YOUR_LAMBDA_URL.lambda-url.us-east-1.on.aws"
)


# ============================================================================
# FUNCTION 1: The Detective (Block 1 — Analyst Bias Check)
# ============================================================================

def check_analyst_bias(ticker, analyst_firm):
    """
    Check whether the analyst firm has an underwriting relationship with
    the company (i.e. is listed as a 'Friend' in stocks_list).

    Returns True  -> Biased (conflict of interest flagged)
    Returns False -> Clean  (no known relationship)
    """
    try:
        resp = requests.get(
            f"{API_BASE_URL}/friends/{ticker.upper()}",
            timeout=10
        )
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        friends = resp.json().get("friends") or ""
        return analyst_firm.lower() in friends.lower()
    except Exception:
        return False


# ============================================================================
# FUNCTION 2: The Bodyguard (Position Sizing)
# ============================================================================

def calculate_position_size(account_balance, option_market_price, risk_pct=0.02):
    """
    Calculate the maximum number of option contracts to buy based on a
    fixed-percentage risk model.

    Rules:
        Max Loss          = account_balance * risk_pct
        Cost per contract = option_market_price * 100  (1 contract = 100 shares)
        Max contracts     = floor(Max Loss / Cost per contract)

    Returns 0 if even 1 contract exceeds the max loss threshold.
    """
    max_loss          = account_balance * risk_pct
    cost_per_contract = option_market_price * 100

    if cost_per_contract > max_loss:
        return 0

    return floor(max_loss / cost_per_contract)


# ============================================================================
# MASTER LOOP: evaluate_trade_signal
# ============================================================================

def evaluate_trade_signal(account_balance, ticker, analyst_firm, target_strike):
    """
    Run the full four-block evaluation pipeline for a single trade signal.

    Parameters
    ----------
    account_balance : float — total trading account size
    ticker          : str   — stock ticker (e.g. 'TSLA')
    analyst_firm    : str   — analyst firm issuing the signal (e.g. 'Morgan Stanley')
    target_strike   : float — target call option strike price

    Prints a formatted Trade Ticket to the console.
    """

    confidence    = 100
    warnings_list = []
    current_month = datetime.now().month

    divider     = "=" * 62
    sub_divider = "-" * 62

    print(divider)
    print("  META-ANALYST MODEL  |  TRADE EVALUATION PIPELINE")
    print(divider)
    print(f"  Account Balance : ${account_balance:,.2f}")
    print(f"  Ticker          : {ticker}")
    print(f"  Analyst Firm    : {analyst_firm}")
    print(f"  Target Strike   : ${target_strike:.2f}")
    print(f"  Evaluated On    : {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(sub_divider)

    # ------------------------------------------------------------------
    # STEP 1 — Block 1: Analyst Bias Check
    # ------------------------------------------------------------------
    print("\n  [STEP 1]  CONFLICT OF INTEREST CHECK")

    is_biased = check_analyst_bias(ticker, analyst_firm)

    if is_biased:
        confidence -= 25
        warnings_list.append("Analyst bias detected — underwriting relationship found.")
        bias_status = "BIASED"
        bias_icon   = "[!]"
    else:
        bias_status = "CLEAN"
        bias_icon   = "[OK]"

    print(f"  {bias_icon} {analyst_firm} vs {ticker}: {bias_status}", end="")
    print(f"  (Confidence: {confidence}%)" if is_biased else "")

    # ------------------------------------------------------------------
    # STEP 2 — Block 2: Seasonality Filter
    # ------------------------------------------------------------------
    print(f"\n  [STEP 2]  SEASONALITY CHECK  ({CALENDAR[current_month]})")

    _old = sys.stdout; sys.stdout = io.StringIO()
    season = analyze_seasonality(ticker, current_month)
    sys.stdout = _old

    avg_return = season['Average_Return']
    win_rate   = season['Win_Rate']

    if win_rate < 0.50:
        confidence -= 15
        warnings_list.append(f"Weak seasonality - {CALENDAR[current_month]} win rate is {win_rate*100:.0f}%.")
        season_icon = "[!]"
    else:
        season_icon = "[OK]"

    print(f"  {season_icon} {CALENDAR[current_month]} Historical Avg Return : {avg_return*100:+.2f}%")
    print(f"  {season_icon} {CALENDAR[current_month]} Win Rate              : {win_rate*100:.0f}%", end="")
    print(f"  (Confidence: {confidence}%)" if win_rate < 0.50 else "")

    # ------------------------------------------------------------------
    # STEP 3 — Block 3: Options Pricing (Go / No-Go gate)
    # ------------------------------------------------------------------
    print(f"\n  [STEP 3]  OPTIONS PRICING CHECK")

    _old = sys.stdout; sys.stdout = io.StringIO()
    options_data = find_cheap_calls(ticker, target_strike)
    sys.stdout = _old

    market_ask     = options_data['Market_Ask']
    fair_value     = options_data['Theoretical_Value']
    is_cheap       = options_data['Is_Cheap']
    expiry         = options_data['Expiry']
    dte            = options_data['Days_To_Expiry']
    hist_vol       = options_data['Historical_Vol']
    matched_strike = options_data['Strike']
    price_diff     = fair_value - market_ask

    print(f"  {'[OK]' if is_cheap else '[X]'} Strike        : ${matched_strike:.2f}  |  Expiry: {expiry}  ({dte} DTE)")
    print(f"  {'[OK]' if is_cheap else '[X]'} Market Ask    : ${market_ask:.2f}")
    print(f"  {'[OK]' if is_cheap else '[X]'} Fair Value    : ${fair_value:.2f}  |  Hist Vol: {hist_vol}")
    print(f"  {'[OK]' if is_cheap else '[X]'} Edge          : ${price_diff:+.2f}  ->  {'CHEAP' if is_cheap else 'RICH'}")

    if not is_cheap:
        print(f"\n  [REJECTED] Options are overpriced. No edge exists.")
        print(sub_divider)
        print("\n  TRADE TICKET")
        print(sub_divider)
        print(f"  Signal     : {analyst_firm} on {ticker}")
        print(f"  Bias       : {bias_status}")
        print(f"  Seasonality: {win_rate*100:.0f}% win rate in {CALENDAR[current_month]}")
        print(f"  Pricing    : RICH  (Ask ${market_ask:.2f} > Fair ${fair_value:.2f})")
        print(f"\n  FINAL ACTION: TRADE REJECTED - Options too expensive.")
        print(divider)
        return

    # ------------------------------------------------------------------
    # STEP 4 — Block 4: Position Sizing
    # ------------------------------------------------------------------
    print(f"\n  [STEP 4]  POSITION SIZING  (Risk: 2% of ${account_balance:,.0f})")

    contracts  = calculate_position_size(account_balance, market_ask)
    max_loss   = account_balance * 0.02
    total_cost = contracts * market_ask * 100

    print(f"  [OK] Max Risk Allowed  : ${max_loss:,.2f}")
    print(f"  [OK] Cost per Contract : ${market_ask * 100:,.2f}")
    print(f"  [OK] Contracts to Buy  : {contracts}")
    print(f"  [OK] Total Outlay      : ${total_cost:,.2f}")

    # ------------------------------------------------------------------
    # TRADE TICKET
    # ------------------------------------------------------------------
    print(f"\n{sub_divider}")
    print("  *** TRADE TICKET ***")
    print(sub_divider)
    print(f"  Signal     : {analyst_firm} on {ticker}")
    print(f"  Bias Check : {bias_status}", end="")
    print(f"  (-25 pts confidence)" if is_biased else "")

    season_label = "Tailwind" if win_rate >= 0.5 else "Headwind"
    print(f"  Seasonality: {win_rate*100:.0f}% win rate in {CALENDAR[current_month]} [{season_label}]", end="")
    print(f"  (-15 pts confidence)" if win_rate < 0.5 else "")

    print(f"  Pricing    : CHEAP  (Edge ${price_diff:+.2f} per share / ${price_diff*100:+.2f} per contract)")
    print(f"  Confidence : {confidence}/100")
    print(sub_divider)

    if warnings_list:
        print("  WARNINGS:")
        for w in warnings_list:
            print(f"    [!] {w}")
        print(sub_divider)

    if contracts == 0:
        print(f"\n  FINAL ACTION: TRADE REJECTED - Insufficient capital.")
        print(f"                1 contract costs ${market_ask*100:,.2f}, exceeds max risk of ${max_loss:,.2f}.")
    else:
        print(f"\n  FINAL ACTION: BUY {contracts} CONTRACT{'S' if contracts > 1 else ''}")
        print(f"    Instrument : {ticker} ${matched_strike:.2f} CALL")
        print(f"    Expiry     : {expiry}  ({dte} DTE)")
        print(f"    Entry Ask  : ${market_ask:.2f} per share  (${market_ask*100:.2f} per contract)")
        print(f"    Max Loss   : ${total_cost:,.2f}  ({(total_cost/account_balance)*100:.1f}% of account)")

    print(divider)


# ============================================================================
# TEST BLOCK
# ============================================================================

if __name__ == "__main__":

    _hist          = yf.Ticker('TSLA').history(period='2d')
    _price         = float(_hist['Close'].iloc[-1])
    _target_strike = round(_price * 1.05)

    evaluate_trade_signal(
        account_balance = 15000,
        ticker          = 'TSLA',
        analyst_firm    = 'Morgan Stanley',
        target_strike   = _target_strike
    )
