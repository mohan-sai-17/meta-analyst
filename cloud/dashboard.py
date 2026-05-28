"""
Meta-Analyst Terminal — Streamlit Dashboard
=============================================
Interactive web dashboard for the quantitative trading model.
Displays analyst ratings, seasonality analysis, and scanner logs.

Run with:
    streamlit run dashboard.py
"""

import re
import os
import sys
import warnings
import numpy as np
import pandas as pd
import yfinance as yf
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

# Allow imports from analysis/ directory
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'analysis'))

from block3_accountant import black_scholes_call, RISK_FREE_RATE

warnings.filterwarnings('ignore')

# ============================================================================
# PAGE CONFIG
# ============================================================================

st.set_page_config(
    page_title = "Meta-Analyst Terminal",
    page_icon  = "📈",
    layout     = "wide"
)

# ============================================================================
# CONFIGURATION
# ============================================================================

API_BASE_URL   = os.environ.get("API_BASE_URL", "https://YOUR_LAMBDA_URL.lambda-url.us-east-1.on.aws")
LOG_FILE       = r"C:\path\to\Daily_Trade_Tickets.txt"

CALENDAR = {
    1: "Jan", 2: "Feb",  3: "Mar", 4: "Apr",
    5: "May", 6: "Jun",  7: "Jul", 8: "Aug",
    9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec"
}


# ============================================================================
# API CLIENT
# ============================================================================

def api_get(path, params=None):
    """Call the Lambda API. Returns parsed JSON or raises on error."""
    resp = requests.get(f"{API_BASE_URL}{path}", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ============================================================================
# DATA LOADERS  (API-backed, replacing SQL Server)
# ============================================================================

@st.cache_data(ttl=300)
def load_stocks_list():
    """Fetch all tickers from the API. Cached for 5 minutes."""
    data = api_get("/tickers")
    df   = pd.DataFrame(data)
    df   = df.rename(columns={"ticker": "Ticker", "stock_name": "Stock_Name"})
    df["Friends"] = None   # not available in API; kept for column-compat
    return df[["Ticker", "Stock_Name", "Friends"]].sort_values("Ticker").reset_index(drop=True)


@st.cache_data(ttl=300)
def load_analyst_ratings(ticker):
    """Fetch analyst ratings for one ticker from the API. Cached 5 minutes."""
    data = api_get(f"/ratings/{ticker}")
    if not data:
        return pd.DataFrame()
    return pd.DataFrame(data)


@st.cache_data(ttl=300)
def load_ohlc(ticker):
    """Fetch OHLC history for one ticker from the API (up to 10 years). Cached 5 minutes."""
    data = api_get(f"/ohlc/{ticker}", params={"days": 3650})
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    df["Date"] = pd.to_datetime(df["Date"])
    return df


@st.cache_data(ttl=300)
def load_oracle_data(ticker):
    """
    All Oracle precomputation for one ticker — OHLC, scorecard, top tier,
    price targets, sigma. Cached 5 minutes so tab switches are instant.
    Returns a dict with all precomputed vars.
    """
    result = {
        'ohlc_df'      : pd.DataFrame(),
        'current_price': None,
        'sigma'        : None,
        'scorecard'    : pd.DataFrame(),
        'top_tier'     : pd.DataFrame(),
        'target_3m'    : None,
        'target_6m'    : None,
        'has_targets'  : False,
        'error'        : None,
    }
    try:
        ohlc = load_ohlc(ticker)
        result['ohlc_df'] = ohlc
        if not ohlc.empty:
            result['current_price'] = float(ohlc['Close'].iloc[-1])
            result['sigma'] = float(
                ohlc['Close'].pct_change().dropna().tail(252).std() * np.sqrt(252)
            )

        sc_data = api_get(f"/scorecard/{ticker}")
        result['scorecard'] = _scorecard_cols(pd.DataFrame(sc_data)) if sc_data else pd.DataFrame()

        tt_data = api_get(f"/top-tier/{ticker}")
        result['top_tier'] = _scorecard_cols(pd.DataFrame(tt_data)) if tt_data else pd.DataFrame()

        pt_data = api_get(f"/price-targets/{ticker}")
        if pt_data and isinstance(pt_data, dict):
            result['current_price'] = pt_data.get('current_price') or result['current_price']
            result['target_3m']     = pt_data.get('target_price_3m')
            result['target_6m']     = pt_data.get('target_price_6m')
            result['has_targets']   = (
                result['target_3m'] is not None and result['target_6m'] is not None
            )
    except Exception as e:
        result['error'] = str(e)
    return result


@st.cache_data(ttl=300)
def _cached_banner_option(ticker, target_3m, current_price, sigma):
    """Cached wrapper for the banner option scan. Avoids yfinance call on every rerun."""
    return find_optimal_option(ticker, float(target_3m), 90, float(current_price), float(sigma))


def _scorecard_cols(df):
    """Normalise API scorecard column names to match dashboard expectations."""
    return df.rename(columns={
        "signal_count" : "Calls",
        "avg_return_3m": "Avg_Return_3M",
        "avg_return_6m": "Avg_Return_6M",
        "win_rate_3m"  : "Win_Rate_3M",
        "win_rate_6m"  : "Win_Rate_6M",
    })


# ============================================================================
# BEST TRADE FINDER — highest-ROI option for a given price target
# ============================================================================

def find_optimal_option(ticker, target_price, target_days, current_price, sigma):
    """
    Scan the option chain for the expiry closest to target_days and return
    the single call with the highest estimated ROI if the price target is hit.

    Filters applied:
        - Strike >= current_price and < target_price  (ITM at target, not yet ITM)
        - ask > 0  (liquid)
        - ask < Black-Scholes fair value  (not overpriced)

    Returns a dict or None if no qualifying option is found.
    """
    try:
        stock       = yf.Ticker(ticker)
        expirations = stock.options
        if not expirations:
            return None

        today_d = pd.Timestamp.today().normalize().date()

        best_expiry = min(
            expirations,
            key=lambda d: abs((pd.Timestamp(d).date() - today_d).days - target_days)
        )

        dte = (pd.Timestamp(best_expiry).date() - today_d).days
        if dte <= 0:
            return None
        T = dte / 365.0

        calls = stock.option_chain(best_expiry).calls.copy()

        calls = calls[
            (calls['strike'] >= current_price) &
            (calls['strike'] <  target_price)  &
            (calls['ask']    >  0)             &
            (calls['bid']    >  0)             &
            # Skip illiquid/stale quotes: spread > 15% of ask means last price is unreliable
            ((calls['ask'] - calls['bid']) / calls['ask'] <= 0.15)
        ].reset_index(drop=True)

        if calls.empty:
            return None

        best     = None
        best_roi = -np.inf

        for _, row in calls.iterrows():
            K   = float(row['strike'])
            ask = float(row['ask'])

            fair_value = black_scholes_call(current_price, K, T, RISK_FREE_RATE, sigma)

            if ask >= fair_value:
                continue

            intrinsic = target_price - K
            if intrinsic <= 0:
                continue

            # Conservative ROI: hold to expiry, full theta decay, pure intrinsic
            roi_expiry = ((intrinsic - ask) / ask) * 100

            # Realistic ROI: sell when stock hits target, BS-price with remaining DTE
            days_remaining  = max(dte - target_days, 0)
            T_remaining     = days_remaining / 365.0
            if T_remaining > 0:
                bs_at_exit = black_scholes_call(target_price, K, T_remaining, RISK_FREE_RATE, sigma)
            else:
                bs_at_exit = intrinsic  # expiry already passed target date
            roi_exit = ((bs_at_exit - ask) / ask) * 100

            if roi_expiry > best_roi:
                best_roi = roi_expiry
                best = {
                    'Expiry'              : best_expiry,
                    'DTE'                 : dte,
                    'Strike'              : K,
                    'Ask'                 : round(ask, 2),
                    'Fair_Value'          : round(fair_value, 4),
                    'Intrinsic_At_Target' : round(intrinsic, 2),
                    'ROI'                 : round(roi_expiry, 1),
                    'ROI_Exit'            : round(roi_exit, 1),
                }

        return best if best_roi > 0 else None

    except Exception:
        return None


# ============================================================================
# BUY SIGNALS PARSER — reads the last scan block from the log file
# ============================================================================

def parse_last_scan(log_file):
    """
    Parse the most recent scanner run from the log file.
    Returns (scan_time: str, signals: list[dict]).
    """
    if not os.path.exists(log_file):
        return None, []

    with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()

    # Locate the last scan block
    scan_positions = [m.start() for m in re.finditer(r'META-ANALYST SCAN', content)]
    if not scan_positions:
        return None, []

    last_block = content[scan_positions[-1]:]

    ts_match  = re.search(r'META-ANALYST SCAN\s*\|?\s*([^\n\r]+)', last_block)
    scan_time = ts_match.group(1).strip() if ts_match else "Unknown"

    signal_positions = [m.start() for m in re.finditer(r'SIGNAL DETECTED:', last_block)]
    if not signal_positions:
        return scan_time, []

    signals = []
    for idx, start in enumerate(signal_positions):
        end   = signal_positions[idx + 1] if idx + 1 < len(signal_positions) else len(last_block)
        chunk = last_block[start:end]

        sig_m = re.search(
            r"SIGNAL DETECTED:\s*(.+?)\s*->\s*(\S+)\s+(\S+)\s+to\s+['\"]([^'\"]+)['\"]",
            chunk
        )
        if not sig_m:
            continue

        def find(pattern, text=chunk):
            m = re.search(pattern, text)
            return m.group(1).strip() if m else None

        firm   = sig_m.group(1).strip()
        action = sig_m.group(2).strip()
        ticker = sig_m.group(3).strip()
        grade  = sig_m.group(4).strip()

        final      = find(r'FINAL ACTION:\s*(.+)')
        contracts  = find(r'BUY (\d+) CONTRACT')
        instrument = find(r'Instrument\s*:\s*(.+)')
        expiry_str = find(r'Expiry\s*:\s*([\w\-]+)')
        dte        = find(r'Expiry\s*:.+?\((\d+) DTE\)')
        ask        = find(r'Entry Ask\s*:\s*\$([0-9.]+)')
        max_loss   = find(r'Max Loss\s*:\s*\$([0-9.,]+)')
        confidence = find(r'Confidence\s*:\s*(\d+)/100')
        bias       = find(r'Bias Check\s*:\s*(\w+)')
        pricing    = find(r'Pricing\s*:\s*(CHEAP|RICH)')
        season_wr  = find(r'Seasonality:\s*(\d+)%')
        season_mon = find(r'Seasonality:\s*\d+% win rate in (\w+)')
        season_dir = find(r'Seasonality:.+\[(Tailwind|Headwind)\]')
        price      = find(r'Current Price:\s*\$([0-9.]+)')

        is_buy = final is not None and 'BUY' in final and 'REJECTED' not in final

        signals.append({
            'Ticker'    : ticker,
            'Firm'      : firm,
            'Action'    : action,
            'Grade'     : grade,
            'Price'     : price,
            'Final'     : final or 'Unknown',
            'Is_Buy'    : is_buy,
            'Contracts' : contracts,
            'Instrument': instrument,
            'Expiry'    : f"{expiry_str} ({dte} DTE)" if expiry_str and dte else expiry_str,
            'Ask'       : ask,
            'Max_Loss'  : max_loss,
            'Confidence': confidence,
            'Bias'      : bias,
            'Pricing'   : pricing,
            'Season_WR' : season_wr,
            'Season_Mon': season_mon,
            'Season_Dir': season_dir,
        })

    return scan_time, signals



# ============================================================================
# SIDEBAR
# ============================================================================

st.sidebar.title("Meta-Analyst Terminal")
st.sidebar.markdown("---")

stocks_df = load_stocks_list()
ticker_list = stocks_df['Ticker'].tolist()

selected_ticker = st.sidebar.selectbox(
    label   = "Select Ticker",
    options = ticker_list,
    index   = ticker_list.index('AAPL') if 'AAPL' in ticker_list else 0
)

# Look up the selected stock's details
stock_row  = stocks_df[stocks_df['Ticker'] == selected_ticker].iloc[0]
stock_name = stock_row['Stock_Name']
friends    = stock_row['Friends'] if pd.notna(stock_row['Friends']) else "Unknown"

st.sidebar.markdown("---")
st.sidebar.markdown(f"**Company:** {stock_name}")
st.sidebar.markdown(f"**Ticker:** `{selected_ticker}`")
st.sidebar.markdown("---")
st.sidebar.caption("Data sourced from AWS S3 · Yahoo Finance · SEC Edgar")


# ============================================================================
# ORACLE PRECOMPUTATION — cached per ticker, instant on tab switches
# ============================================================================

_oracle       = load_oracle_data(selected_ticker)
_oracle_error = _oracle['error']
ohlc_df       = _oracle['ohlc_df']
current_price = _oracle['current_price']
sigma         = _oracle['sigma']
scorecard     = _oracle['scorecard']
top_tier      = _oracle['top_tier']
target_3m     = _oracle['target_3m']
target_6m     = _oracle['target_6m']
has_targets   = _oracle['has_targets']
today         = pd.Timestamp.today().normalize()
date_3m       = today + pd.Timedelta(days=90)
date_6m       = today + pd.Timedelta(days=180)


# ============================================================================
# MAIN AREA — PAGE TITLE
# ============================================================================

st.title(f"📈 Meta-Analyst Terminal")
st.subheader(f"{selected_ticker} — {stock_name}")
st.markdown("---")

# ============================================================================
# TOP BANNER — ORACLE MASTER RECOMMENDATION
# ============================================================================

if _oracle_error:
    st.warning(f"Oracle precomputation warning for {selected_ticker}: {_oracle_error}")

elif has_targets and current_price is not None and sigma is not None:
    with st.spinner("Scanning option chain for best trade..."):
        best_3m = _cached_banner_option(
            selected_ticker, target_3m, current_price, sigma
        )

    if best_3m:
        st.success(
            f"🔥 **ORACLE TOP PICK — {selected_ticker}:** "
            f"Buy the **${best_3m['Strike']:.2f} Call** expiring **{best_3m['Expiry']}** "
            f"({best_3m['DTE']} DTE).  "
            f"Market Ask: **${best_3m['Ask']:.2f}** "
            f"(Fair Value: ${best_3m['Fair_Value']:.2f}).  "
            f"ROI at expiry: **{best_3m['ROI']:.0f}%** | "
            f"ROI if sold at target hit: **{best_3m['ROI_Exit']:.0f}%**"
        )
    else:
        st.info(
            f"Oracle projects a **${target_3m:.2f}** 3-month target for {selected_ticker}, "
            f"but no appropriately priced CHEAP calls are currently available."
        )

st.markdown("---")

# ============================================================================
# TABS
# ============================================================================

tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
    "🕵️ The Detective (Ratings & Bias)",
    "🌤️ The Weatherman (Seasonality)",
    "🔮 The Oracle (Price Projections)",
    "🚦 Today's Buy Signals",
    "📊 The Options Desk (Fair Value)",
    "🌍 Sector Rotation Scanner",
])


# ─────────────────────────────────────────────────────────────────────────────
# TAB 1: THE DETECTIVE
# ─────────────────────────────────────────────────────────────────────────────

with tab1:

    st.markdown("### Analyst Intelligence Report")

    # Company info row
    col1, col2 = st.columns(2)
    with col1:
        st.metric(label="Company", value=stock_name)
    with col2:
        st.metric(label="Ticker", value=selected_ticker)

    # Underwriter (Friends) bias section
    st.markdown("#### Underwriter Relationships (The 'Friends')")
    if friends == "Unknown" or not friends:
        st.info("No underwriting relationships found in SEC filings. Signal considered **CLEAN**.")
    else:
        st.warning(
            f"**Conflict of Interest Flag:** This company has known underwriting "
            f"relationships with the following banks. Treat analyst upgrades from "
            f"these firms with caution:\n\n**{friends}**"
        )
        # Show individual friend chips
        friend_list = [f.strip() for f in friends.split(',')]
        cols = st.columns(min(len(friend_list), 4))
        for i, firm in enumerate(friend_list):
            cols[i % 4].error(f"⚠️ {firm}")

    st.markdown("---")

    # Analyst ratings table
    st.markdown("#### Recent Analyst Ratings (Last 50)")
    try:
        ratings_df = load_analyst_ratings(selected_ticker)

        if ratings_df.empty:
            st.info(f"No analyst ratings found for {selected_ticker}.")
        else:
            # Colour-code the Action column
            def highlight_action(val):
                val_lower = str(val).lower()
                if 'upgrade' in val_lower or 'initiated' in val_lower:
                    return 'background-color: #1a3a1a; color: #4caf50'
                elif 'downgrade' in val_lower:
                    return 'background-color: #3a1a1a; color: #f44336'
                return ''

            styled = ratings_df.style.applymap(highlight_action, subset=['Action'])
            st.dataframe(styled, use_container_width=True, height=450)
            st.caption(f"Showing {len(ratings_df)} most recent ratings · sorted newest first")

    except Exception as e:
        st.error(f"Could not load analyst ratings for {selected_ticker}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# TAB 2: THE WEATHERMAN
# ─────────────────────────────────────────────────────────────────────────────

with tab2:

    st.markdown("### Historical Seasonality Analysis")
    st.markdown(
        "Based on 10 years of daily OHLCV data. "
        "**Win Rate** = % of years that month finished positive. "
        "Bar colour = average return direction."
    )

    try:
        seas_data  = api_get(f"/seasonality/{selected_ticker}")
        season_df  = pd.DataFrame(seas_data)
        season_df["Month"]          = season_df["month"].map(CALENDAR)
        season_df["Win_Rate_Pct"]   = season_df["win_rate"].astype(float)
        season_df["Avg_Return_Pct"] = season_df["avg_return"].astype(float)
        season_df["Signal"]         = season_df["Avg_Return_Pct"].apply(
            lambda x: "Bullish" if x > 0 else "Bearish"
        )

        if season_df.empty:
            st.warning(f"No seasonality data available for {selected_ticker}.")
        else:

            # ── Bar chart ──────────────────────────────────────────────────
            fig = px.bar(
                season_df,
                x             = 'Month',
                y             = 'Win_Rate_Pct',
                color         = 'Signal',
                color_discrete_map = {
                    'Bullish': '#4caf50',   # green
                    'Bearish': '#f44336'    # red
                },
                text          = season_df['Win_Rate_Pct'].apply(lambda x: f"{x:.0f}%"),
                hover_data    = {
                    'Avg_Return_Pct': ':.2f',
                    'Win_Rate_Pct'  : ':.1f',
                    'Signal'        : True
                },
                labels        = {
                    'Win_Rate_Pct'  : 'Win Rate (%)',
                    'Month'         : 'Month',
                    'Avg_Return_Pct': 'Avg Return (%)'
                },
                title         = f"{selected_ticker} — Monthly Win Rate (10-Year History)",
                category_orders = {'Month': list(CALENDAR.values())}
            )

            fig.update_traces(textposition='outside')
            fig.add_hline(
                y          = 50,
                line_dash  = "dash",
                line_color = "gray",
                annotation_text = "50% breakeven",
                annotation_position = "bottom right"
            )
            fig.update_layout(
                plot_bgcolor  = 'rgba(0,0,0,0)',
                paper_bgcolor = 'rgba(0,0,0,0)',
                font_color    = 'white',
                showlegend    = True,
                legend_title  = "Signal",
                yaxis         = dict(range=[0, 110]),
                height        = 480
            )

            st.plotly_chart(fig, use_container_width=True)

            # ── Summary table ──────────────────────────────────────────────
            st.markdown("#### Monthly Breakdown")
            display_df = season_df[['Month', 'Avg_Return_Pct', 'Win_Rate_Pct', 'Signal']].copy()
            display_df.columns = ['Month', 'Avg Return (%)', 'Win Rate (%)', 'Signal']

            def color_signal(val):
                if val == 'Bullish':
                    return 'color: #4caf50; font-weight: bold'
                return 'color: #f44336; font-weight: bold'

            def color_return(val):
                return 'color: #4caf50' if val > 0 else 'color: #f44336'

            styled_season = (
                display_df.style
                .applymap(color_signal, subset=['Signal'])
                .applymap(color_return, subset=['Avg Return (%)'])
                .format({'Avg Return (%)': '{:+.2f}%', 'Win Rate (%)': '{:.1f}%'})
            )

            st.dataframe(styled_season, use_container_width=True, hide_index=True)

            # ── Best / Worst callouts ──────────────────────────────────────
            best  = season_df.loc[season_df['Win_Rate_Pct'].idxmax()]
            worst = season_df.loc[season_df['Win_Rate_Pct'].idxmin()]

            col1, col2 = st.columns(2)
            with col1:
                st.success(
                    f"**Best Month:** {best['Month']}  \n"
                    f"Win Rate: {best['Win_Rate_Pct']:.0f}%  |  "
                    f"Avg Return: {best['Avg_Return_Pct']:+.2f}%"
                )
            with col2:
                st.error(
                    f"**Worst Month:** {worst['Month']}  \n"
                    f"Win Rate: {worst['Win_Rate_Pct']:.0f}%  |  "
                    f"Avg Return: {worst['Avg_Return_Pct']:+.2f}%"
                )

    except Exception as e:
        st.error(f"Could not load seasonality data for {selected_ticker}: `{e}`")


# ─────────────────────────────────────────────────────────────────────────────
# TAB 3: THE ORACLE
# ─────────────────────────────────────────────────────────────────────────────

with tab3:

    st.markdown("### The Oracle — Analyst Scorecard & Price Projections")
    st.markdown(
        "Grades every analyst firm by the **actual 3-month and 6-month price "
        "performance** that followed each of their upgrades. Only 'Top Tier' "
        "firms (6M Win Rate > 50% **and** positive avg returns) feed the "
        "Smart Consensus price targets."
    )

    if _oracle_error:
        st.error(f"Oracle computation failed: {_oracle_error}")
    elif ohlc_df.empty:
        st.warning(f"No OHLC data found for `{selected_ticker}`.")
    elif scorecard.empty:
        st.warning("Not enough historical data to build the scorecard yet.")
    else:
        # ── Price target metrics ───────────────────────────────────────────
        st.markdown("#### Smart Consensus Price Targets")
        st.caption(
            f"Based on {len(top_tier)} Top Tier firm(s) out of "
            f"{len(scorecard)} total firms with sufficient history."
        )

        col1, col2, col3 = st.columns(3)
        col1.metric(
            label = "Current Price",
            value = f"${current_price:.2f}"
        )

        if has_targets:
            delta_3m = target_3m - current_price
            delta_6m = target_6m - current_price
            col2.metric(
                label = "3-Month Target",
                value = f"${target_3m:.2f}",
                delta = f"{delta_3m:+.2f} ({delta_3m/current_price*100:+.1f}%)"
            )
            col3.metric(
                label = "6-Month Target",
                value = f"${target_6m:.2f}",
                delta = f"{delta_6m:+.2f} ({delta_6m/current_price*100:+.1f}%)"
            )
        else:
            col2.metric(label="3-Month Target", value="N/A")
            col3.metric(label="6-Month Target", value="N/A")
            st.warning("No Top Tier firms qualify yet. Targets require Win Rate > 50% and positive avg returns.")

        st.markdown("---")

        # ── Price chart ────────────────────────────────────────────────────
        st.markdown("#### Price History + Projected Targets (Last 12 Months)")

        cutoff      = today - pd.Timedelta(days=365)
        ohlc_1yr    = ohlc_df[ohlc_df['Date'] >= cutoff].copy()

        fig = go.Figure()

        # Historical price line
        fig.add_trace(go.Scatter(
            x    = ohlc_1yr['Date'],
            y    = ohlc_1yr['Close'],
            mode = 'lines',
            name = 'Close Price',
            line = dict(color='#2196F3', width=2)
        ))

        if has_targets:
            # Dotted projection lines from today to targets
            fig.add_trace(go.Scatter(
                x    = [today, date_3m],
                y    = [current_price, target_3m],
                mode = 'lines',
                name = '3M Projection',
                line = dict(color='#4caf50', dash='dot', width=2)
            ))
            fig.add_trace(go.Scatter(
                x    = [today, date_6m],
                y    = [current_price, target_6m],
                mode = 'lines',
                name = '6M Projection',
                line = dict(color='#ff9800', dash='dot', width=2)
            ))

            # Target star markers
            fig.add_trace(go.Scatter(
                x          = [date_3m],
                y          = [target_3m],
                mode       = 'markers+text',
                name       = f'3M Target ${target_3m:.2f}',
                marker     = dict(symbol='star', size=16, color='#4caf50'),
                text       = [f'  3M: ${target_3m:.2f}'],
                textposition = 'middle right',
                textfont   = dict(color='#4caf50', size=12)
            ))
            fig.add_trace(go.Scatter(
                x          = [date_6m],
                y          = [target_6m],
                mode       = 'markers+text',
                name       = f'6M Target ${target_6m:.2f}',
                marker     = dict(symbol='star', size=16, color='#ff9800'),
                text       = [f'  6M: ${target_6m:.2f}'],
                textposition = 'middle right',
                textfont   = dict(color='#ff9800', size=12)
            ))

            # Vertical "Today" reference line
            fig.add_vline(
                x              = today.timestamp() * 1000,
                line_dash      = "dash",
                line_color     = "gray",
                annotation_text = "Today",
                annotation_position = "top"
            )

        fig.update_layout(
            plot_bgcolor  = 'rgba(0,0,0,0)',
            paper_bgcolor = 'rgba(0,0,0,0)',
            font_color    = 'white',
            height        = 460,
            legend        = dict(orientation='h', yanchor='bottom', y=1.02),
            xaxis_title   = "Date",
            yaxis_title   = "Price (USD)",
            title         = f"{selected_ticker} — 12-Month History + Smart Consensus Targets",
            hovermode     = 'x unified'
        )

        st.plotly_chart(fig, use_container_width=True)

        st.markdown("---")

        # ── Analyst Scorecard table ────────────────────────────────────────
        st.markdown("#### Full Analyst Scorecard")
        st.caption("Sorted by Average 6M Return descending. Top Tier firms highlighted.")

        display_sc = scorecard[['Firm', 'Calls', 'Avg_Return_3M', 'Avg_Return_6M', 'Win_Rate_3M', 'Win_Rate_6M']].copy()
        display_sc.columns = [
            'Firm', 'Calls',
            'Avg 3M Return (%)', 'Avg 6M Return (%)',
            'Win Rate 3M (%)', 'Win Rate 6M (%)'
        ]

        top_tier_firms = set(top_tier['Firm'].tolist())

        def style_scorecard_row(row):
            is_top = row['Firm'] in top_tier_firms
            base   = 'background-color: #1a2e1a; ' if is_top else ''
            return [base] * len(row)

        def color_ret(val):
            try:
                return 'color: #4caf50' if float(val) > 0 else 'color: #f44336'
            except Exception:
                return ''

        styled_sc = (
            display_sc.style
            .apply(style_scorecard_row, axis=1)
            .applymap(color_ret, subset=['Avg 3M Return (%)', 'Avg 6M Return (%)'])
            .format({
                'Avg 3M Return (%)' : '{:+.1f}%',
                'Avg 6M Return (%)' : '{:+.1f}%',
                'Win Rate 3M (%)' : '{:.1f}%',
                'Win Rate 6M (%)' : '{:.1f}%',
            })
        )

        st.dataframe(styled_sc, use_container_width=True, hide_index=True, height=400)

        if top_tier_firms:
            st.success(
                f"**Top Tier Firms** (green rows): "
                + ", ".join(sorted(top_tier_firms))
            )



# ─────────────────────────────────────────────────────────────────────────────
# TAB 4: TODAY'S BUY SIGNALS
# ─────────────────────────────────────────────────────────────────────────────

with tab4:

    st.markdown("### Today's Buy Signals")
    st.caption(
        "Pre-screened by cloud pipeline — Top Tier analyst upgrades from the last 3 days. "
        "Option chains fetched live only for qualifying tickers."
    )

    try:
        candidates = api_get("/candidates")

        if not candidates:
            st.info(
                "No Top Tier analyst upgrades detected in the last 3 days.  \n"
                "Check back after the next pipeline run (weekdays, 2pm UTC)."
            )
        else:
            clean  = [c for c in candidates if not c.get("is_biased")]
            biased = [c for c in candidates if c.get("is_biased")]

            c1, c2, c3 = st.columns(3)
            c1.metric("Top Tier Upgrades (3d)", len(candidates))
            c2.metric("Clean (no conflict)",    len(clean))
            c3.metric("Biased (underwriter)",   len(biased))
            st.markdown("---")

            if clean:
                # ── Live option chain scan — only for clean candidates ─────
                def scan_candidate(c):
                    try:
                        price  = float(c.get("current_price") or 0)
                        target = float(c.get("target_3m")     or 0)
                        sigma  = float(c.get("hist_vol")       or 0.25)
                        if target <= price or price <= 0 or sigma <= 0:
                            return c["Ticker"], None
                        opt = find_optimal_option(c["Ticker"], target, 90, price, sigma)
                        return c["Ticker"], opt
                    except Exception:
                        return c["Ticker"], None

                with st.spinner(f"Scanning option chains for {len(clean)} clean candidates..."):
                    opt_results = {}
                    with ThreadPoolExecutor(max_workers=10) as executor:
                        futs = {executor.submit(scan_candidate, c): c["Ticker"] for c in clean}
                        for fut in as_completed(futs):
                            ticker, opt = fut.result()
                            opt_results[ticker] = opt

                approved  = [(c, opt_results[c["Ticker"]]) for c in clean if opt_results.get(c["Ticker"])]
                no_option = [c for c in clean if not opt_results.get(c["Ticker"])]

                a1, a2 = st.columns(2)
                a1.metric("Approved — cheap options found", len(approved))
                a2.metric("No cheap option available",      len(no_option))
                st.markdown("---")

                # ── Approved BUY signals ────────────────────────────────────
                if approved:
                    st.markdown("#### Approved — BUY Signals")
                    for c, opt in approved:
                        with st.container(border=True):
                            st.markdown(
                                f"### 🟢 BUY — **{c['Ticker']}**  "
                                f"&nbsp;&nbsp; `{c['Firm']}` &nbsp;·&nbsp; "
                                f"{c['Action']} to **{c['ToGrade']}**  "
                                f"&nbsp;·&nbsp; {c['GradeDate']}"
                            )

                            col1, col2, col3, col4 = st.columns(4)
                            price  = c.get("current_price")
                            target = c.get("target_3m")
                            col1.metric("Current Price", f"${price:.2f}"  if price  else "N/A")
                            col2.metric("3M Target",     f"${target:.2f}" if target else "N/A")
                            col3.metric("Entry Ask",     f"${opt['Ask']:.2f}")
                            col4.metric("ROI at Expiry", f"{opt['ROI']:.0f}%")

                            st.markdown("---")

                            d1, d2, d3 = st.columns(3)
                            with d1:
                                st.markdown("**Bias Check:** ✅ CLEAN")
                            with d2:
                                wr  = c.get("season_win_rate",   0)
                                ar  = c.get("season_avg_return", 0)
                                ico = "✅" if ar > 0 else "⚠️"
                                dir_lbl = "Tailwind" if ar > 0 else "Headwind"
                                st.markdown(f"**Seasonality:** {ico} {wr:.0f}% WR [{dir_lbl}]")
                            with d3:
                                st.markdown("**Options Pricing:** ✅ CHEAP")

                            st.markdown(
                                f"**Instrument:** `{c['Ticker']} ${opt['Strike']:.2f} Call`  "
                                f"&nbsp;&nbsp;  **Expiry:** `{opt['Expiry']} ({opt['DTE']} DTE)`"
                            )
                            st.success(
                                f"**FINAL ACTION: BUY {c['Ticker']} ${opt['Strike']:.2f} CALL — "
                                f"ROI at expiry: {opt['ROI']:.0f}%** "
                                f"(ROI if sold at target: {opt['ROI_Exit']:.0f}%)"
                            )

                    st.markdown("---")

                # ── Clean signals with no cheap option ─────────────────────
                if no_option:
                    st.markdown("#### Clean Signals — No Cheap Options Found")
                    for c in no_option:
                        with st.container(border=True):
                            price  = c.get("current_price")
                            target = c.get("target_3m")
                            st.markdown(
                                f"**{c['Ticker']}** — `{c['Firm']}` "
                                f"→ **{c['ToGrade']}** on {c['GradeDate']}"
                            )
                            st.info(
                                f"Oracle 3M target: ${target:.2f} | "
                                f"Current: ${price:.2f} | "
                                "No underpriced call options available at this time."
                            )

            # ── Biased signals (collapsed) ──────────────────────────────────
            if biased:
                with st.expander(f"Biased signals — underwriter conflict ({len(biased)} tickers)"):
                    for c in biased:
                        st.warning(
                            f"**{c['Ticker']}** — `{c['Firm']}` "
                            f"→ **{c['ToGrade']}** on {c['GradeDate']}  \n"
                            f"Firm appears in underwriter list: _{c.get('friends', '')}_"
                        )

    except Exception as e:
        st.error(f"Could not load today's candidates: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# TAB 5: THE OPTIONS DESK
# ─────────────────────────────────────────────────────────────────────────────

with tab5:

    st.markdown("### The Options Desk — Black-Scholes Fair Value Analysis")
    st.markdown(
        "Fetches the live call option chain for the selected ticker, calculates "
        "the **Black-Scholes theoretical fair value** for every strike, and flags "
        "where the market is **underpricing (CHEAP)** or **overpricing (RICH)** options."
    )

    try:
        # ── Use pre-computed OHLC and volatility ───────────────────────────
        if ohlc_df.empty:
            st.warning(f"No OHLC data found for `{selected_ticker}`. Cannot compute volatility.")
            st.stop()

        if sigma is None:
            st.warning(f"Could not compute historical volatility for `{selected_ticker}`.")
            st.stop()

        if current_price is None:
            st.warning(f"Could not fetch current price for `{selected_ticker}`.")
            st.stop()

        S = current_price

        # ── Expiration date selector ───────────────────────────────────────
        ticker_obj  = yf.Ticker(selected_ticker)
        expirations = ticker_obj.options

        if not expirations:
            st.warning(f"No options data available for `{selected_ticker}` on Yahoo Finance.")
            st.stop()

        two_yrs_out = today + pd.Timedelta(days=730)
        expirations = [e for e in expirations if pd.Timestamp(e) <= two_yrs_out]

        selected_date = st.selectbox(
            "Select Expiration Date",
            options  = expirations,
            help     = "Options up to 2 years out. Shorter expiries are more liquid."
        )

        # ── Live call chain ────────────────────────────────────────────────
        with st.spinner(f"Fetching option chain for {selected_ticker} expiring {selected_date}..."):
            chain = ticker_obj.option_chain(selected_date).calls.copy()

        # ── Time to expiry ─────────────────────────────────────────────────
        expiry_dt = pd.Timestamp(selected_date)
        dte       = max((expiry_dt - today).days, 1)
        T         = dte / 365.0

        # ── Metric cards ───────────────────────────────────────────────────
        m1, m2, m3 = st.columns(3)
        m1.metric("Current Stock Price",           f"${S:,.2f}")
        m2.metric("Historical Volatility (252d)",   f"{sigma * 100:.1f}%")
        m3.metric("Days to Expiry",                f"{dte} days")
        st.markdown("---")

        # ── Filter: liquid strikes within ±30% of spot ────────────────────
        chain = chain[
            (chain['strike'] >= S * 0.70) &
            (chain['strike'] <= S * 1.30) &
            (chain['ask']    >  0)
        ].reset_index(drop=True)

        if chain.empty:
            st.warning("No liquid strikes found within ±30% of the current price.")
            st.stop()

        # ── Black-Scholes loop ─────────────────────────────────────────────
        chain['Theoretical_Value'] = [
            round(black_scholes_call(S, float(k), T, RISK_FREE_RATE, sigma), 4)
            for k in chain['strike']
        ]
        chain['Mispricing_Edge'] = (chain['Theoretical_Value'] - chain['ask']).round(4)

        # ── Plotly chart ───────────────────────────────────────────────────
        fig = go.Figure()

        fig.add_trace(go.Scatter(
            x    = chain['strike'],
            y    = chain['Theoretical_Value'],
            mode = 'lines',
            name = 'BS Fair Value',
            line = dict(color='#2196F3', width=2.5),
            hovertemplate = 'Strike: $%{x:.2f}<br>Fair Value: $%{y:.4f}<extra></extra>'
        ))

        fig.add_trace(go.Scatter(
            x          = chain['strike'],
            y          = chain['ask'],
            mode       = 'markers',
            name       = 'Market Ask',
            marker     = dict(color='#f44336', size=8, symbol='circle'),
            hovertemplate = 'Strike: $%{x:.2f}<br>Market Ask: $%{y:.4f}<extra></extra>'
        ))

        fig.add_vline(
            x                   = S,
            line_dash           = "dash",
            line_color          = "rgba(255,255,255,0.4)",
            annotation_text     = f"  ${S:,.2f}",
            annotation_position = "top"
        )

        fig.update_layout(
            plot_bgcolor  = 'rgba(0,0,0,0)',
            paper_bgcolor = 'rgba(0,0,0,0)',
            font_color    = 'white',
            height        = 460,
            title         = (
                f"{selected_ticker} — Call Fair Value vs Market Ask  |  "
                f"Expiry: {selected_date}  |  Vol: {sigma*100:.1f}%"
            ),
            xaxis_title   = "Strike Price ($)",
            yaxis_title   = "Option Price ($)",
            legend        = dict(orientation='h', yanchor='bottom', y=1.02),
            hovermode     = 'x unified'
        )

        st.plotly_chart(fig, use_container_width=True)
        st.markdown("---")

        # ── Data table ─────────────────────────────────────────────────────
        st.markdown("#### Option Chain — Strike-by-Strike Breakdown")
        st.caption(
            "Green rows = CHEAP (BS Fair Value > Market Ask). "
            "Red rows = RICH (Market Ask significantly above Fair Value). "
            "Positive Mispricing Edge = potential buying opportunity."
        )

        display_cols = ['strike', 'ask', 'Theoretical_Value', 'Mispricing_Edge', 'impliedVolatility']
        display_df   = chain[display_cols].rename(columns={
            'strike'            : 'Strike',
            'ask'               : 'Market Ask',
            'Theoretical_Value' : 'Fair Value (BS)',
            'Mispricing_Edge'   : 'Mispricing Edge',
            'impliedVolatility' : 'Impl. Vol'
        })

        def style_row(row):
            edge = row['Mispricing Edge']
            if edge > 0:
                return ['background-color: #1a3a1a'] * len(row)
            elif edge < -0.50:
                return ['background-color: #3a1a1a'] * len(row)
            return [''] * len(row)

        styled_chain = (
            display_df.style
            .apply(style_row, axis=1)
            .format({
                'Strike'         : '${:.2f}',
                'Market Ask'     : '${:.2f}',
                'Fair Value (BS)': '${:.4f}',
                'Mispricing Edge': '${:+.4f}',
                'Impl. Vol'      : '{:.1%}'
            })
        )

        st.dataframe(styled_chain, use_container_width=True, hide_index=True, height=420)

        # ── Summary callouts ───────────────────────────────────────────────
        cheap_count = int((chain['Mispricing_Edge'] >     0).sum())
        rich_count  = int((chain['Mispricing_Edge'] < -0.50).sum())
        best_edge   = chain.loc[chain['Mispricing_Edge'].idxmax()]

        st.markdown("---")
        s1, s2, s3 = st.columns(3)
        s1.success(f"**Cheap strikes:** {cheap_count} — Market underpricing vs BS Fair Value")
        s2.error(  f"**Rich strikes:** {rich_count} — Market overpricing by >$0.50")
        s3.info(
            f"**Best edge:** ${best_edge['strike']:.2f} strike  \n"
            f"Edge: ${best_edge['Mispricing_Edge']:+.4f} per share "
            f"(${best_edge['Mispricing_Edge']*100:+.2f} per contract)"
        )

    except Exception as e:
        st.error(f"Options Desk failed for {selected_ticker}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# TAB 6: SECTOR ROTATION SCANNER
# ─────────────────────────────────────────────────────────────────────────────

with tab6:

    st.markdown("### Sector Rotation Scanner — Non-Tech Oracle Green Signals")
    st.markdown(
        "Loads pre-screened non-tech tickers (Oracle green — Top Tier coverage + positive 3M target) "
        "from the daily pipeline. Click **Scan for Options** to run live Black-Scholes "
        "screening only on those candidates (~20-50 tickers instead of 500)."
    )
    st.markdown("---")

    # ── Load pre-filtered candidates from API (instant) ────────────────────────
    try:
        sector_candidates = api_get("/sector-candidates")
    except Exception as _e:
        sector_candidates = []
        st.warning(f"Could not load sector candidates from API: {_e}")

    if sector_candidates:
        cand_df = pd.DataFrame(sector_candidates)
        m1, m2, m3 = st.columns(3)
        m1.metric("Pre-screened Candidates", len(cand_df))
        m2.metric("Sectors Covered",         cand_df['Sector'].nunique() if 'Sector' in cand_df else '—')
        m3.metric("Avg Expected Return 3M",  f"{cand_df['expected_return_3m_pct'].mean():.1f}%" if 'expected_return_3m_pct' in cand_df else '—')
        st.markdown("---")

    # ── Scan for cheap options ─────────────────────────────────────────────────
    run_scan = st.button("Scan for Options", type="primary", use_container_width=False)

    if run_scan:
        if not sector_candidates:
            st.error("No pre-screened candidates available. Run the pipeline first.")
        else:
            st.session_state['sector_results'] = []
            st.session_state['sector_summary'] = {}

            status_line = st.empty()
            progress_bar = st.progress(0.0)
            live_table   = st.empty()

            results = []
            total   = len(sector_candidates)

            def _scan_sector_candidate(c):
                try:
                    t_ticker = c['Ticker']
                    t_price  = float(c['current_price'])
                    t_target = float(c['target_3m'])
                    t_sigma  = float(c.get('hist_vol') or 0.25)
                    best = find_optimal_option(t_ticker, t_target, 90, t_price, t_sigma)
                    return c, best
                except Exception:
                    return c, None

            with ThreadPoolExecutor(max_workers=10) as executor:
                futs = {executor.submit(_scan_sector_candidate, c): c['Ticker']
                        for c in sector_candidates}
                done = 0
                for fut in as_completed(futs):
                    done += 1
                    progress_bar.progress(done / total)
                    c, best = fut.result()
                    if best:
                        pct = (float(c['target_3m']) / float(c['current_price']) - 1) * 100
                        results.append({
                            'Ticker'    : c['Ticker'],
                            'Sector'    : c.get('Sector', ''),
                            'Price'     : f"${float(c['current_price']):.2f}",
                            '3M Target' : f"${float(c['target_3m']):.2f} (+{pct:.1f}%)",
                            'Best Call' : f"${best['Strike']:.0f} Call {best['Expiry']}",
                            'Ask'       : f"${best['Ask']:.2f}",
                            'Fair Value': f"${best['Fair_Value']:.4f}",
                            'Est. ROI'  : f"{best['ROI']:.0f}%",
                        })
                    status_line.markdown(
                        f"Scanning {done}/{total} candidates — **{len(results)} GREEN** found"
                    )

            progress_bar.progress(1.0)
            status_line.success(
                f"Scan complete — {total} candidates checked, "
                f"{len(results)} GREEN signals found."
            )

            st.session_state['sector_results'] = results
            st.session_state['sector_summary'] = {
                'scanned' : total,
                'green'   : len(results),
            }

    # ── Display stored results ─────────────────────────────────────────────────
    if st.session_state.get('sector_results'):
        results = st.session_state['sector_results']
        summary = st.session_state.get('sector_summary', {})

        if not run_scan:
            st.markdown("---")
            m1, m2 = st.columns(2)
            m1.metric("Candidates Scanned",  summary.get('scanned', '—'))
            m2.metric("GREEN Signals Found",  summary.get('green',   '—'))

        if results:
            st.markdown("#### Results — Non-Tech Green Signals")
            df = pd.DataFrame(results)

            def color_roi_cell(val):
                try:
                    n = float(str(val).replace('%', ''))
                    if n >= 200: return 'color: #4caf50; font-weight: bold'
                    if n >= 100: return 'color: #8bc34a; font-weight: bold'
                    if n >=  50: return 'color: #cddc39'
                    return 'color: #ff9800'
                except Exception:
                    return ''

            def highlight_sector(val):
                palette = {
                    'Healthcare'        : 'background-color: #1a2a3a',
                    'Financials'        : 'background-color: #1a3a2a',
                    'Financial Services': 'background-color: #1a3a2a',
                    'Energy'            : 'background-color: #3a2a1a',
                    'Utilities'         : 'background-color: #2a1a3a',
                    'Real Estate'       : 'background-color: #3a1a2a',
                    'Industrials'       : 'background-color: #2a2a1a',
                    'Consumer Staples'  : 'background-color: #1a3a3a',
                    'Consumer Cyclical' : 'background-color: #2a3a1a',
                    'Basic Materials'   : 'background-color: #3a3a1a',
                }
                return palette.get(val, '')

            styled = (
                df.style
                .applymap(color_roi_cell,   subset=['Est. ROI'])
                .applymap(highlight_sector, subset=['Sector'])
            )
            st.dataframe(styled, use_container_width=True, hide_index=True)

            # Sector breakdown
            st.markdown("---")
            st.markdown("#### Sector Breakdown")
            sector_counts = df['Sector'].value_counts().reset_index()
            sector_counts.columns = ['Sector', 'GREEN Signals']
            fig_s = px.bar(
                sector_counts,
                x     = 'Sector',
                y     = 'GREEN Signals',
                color = 'Sector',
                title = "GREEN Signals by Sector",
                text  = 'GREEN Signals'
            )
            fig_s.update_layout(
                plot_bgcolor  = 'rgba(0,0,0,0)',
                paper_bgcolor = 'rgba(0,0,0,0)',
                font_color    = 'white',
                showlegend    = False,
                height        = 340
            )
            st.plotly_chart(fig_s, use_container_width=True)
        else:
            st.info("No cheap options found in this batch. Try again after market open.")

    elif not run_scan:
        if sector_candidates:
            st.info(
                f"{len(sector_candidates)} pre-screened candidates ready. "
                "Click **Scan for Options** to find the best mispriced calls."
            )
        else:
            st.info("No pre-screened candidates available. Run the daily pipeline first.")
