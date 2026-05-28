"""
pages/top_picks_calls.py
=========================
Top Picks — Calls  (Cloud / Lambda API version)

Two-phase scan for speed:
  Phase 1 — Parallel API calls to find tickers with Oracle price targets (fast, cached).
  Phase 2 — yfinance options lookup ONLY for qualifying tickers (much smaller set).

This means instead of hitting yfinance 500 times we hit it ~20-50 times.
"""

import os
import sys
import warnings
import numpy as np
import pandas as pd
import streamlit as st
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "analysis"))
from block3_accountant import black_scholes_call, RISK_FREE_RATE

warnings.filterwarnings("ignore")

API_BASE_URL   = os.environ.get("API_BASE_URL", "https://YOUR_LAMBDA_URL.lambda-url.us-east-1.on.aws")

st.set_page_config(page_title="Top Picks — Calls", page_icon="🎯", layout="wide")


# ── Helpers ────────────────────────────────────────────────────────────────────

def api_get(path, params=None):
    resp = requests.get(f"{API_BASE_URL}{path}", params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


@st.cache_data(ttl=300)
def load_stocks_list():
    data = api_get("/tickers")
    df   = pd.DataFrame(data).rename(columns={"ticker": "Ticker", "stock_name": "Stock_Name"})
    return df[["Ticker", "Stock_Name"]].sort_values("Ticker").reset_index(drop=True)


@st.cache_data(ttl=600)
def load_all_price_targets():
    """One bulk call — returns all tickers with Oracle coverage (~457 rows)."""
    try:
        return api_get("/price-targets")
    except Exception:
        return []


@st.cache_data(ttl=600)
def fetch_ohlc_sigma(ticker):
    """Cached sigma (volatility) computation from OHLC."""
    try:
        data = api_get(f"/ohlc/{ticker}", params={"days": 756})   # 3 yrs
        if not data:
            return None
        df = pd.DataFrame(data)
        df["Date"] = pd.to_datetime(df["Date"])
        return float(df["Close"].pct_change().dropna().tail(252).std() * np.sqrt(252))
    except Exception:
        return None


def find_best_call(ticker, target_price, current_price, sigma,
                   horizon_days=90, risk_free=RISK_FREE_RATE):
    """Hit yfinance and return the best underpriced call, or None."""
    import yfinance as yf
    today   = pd.Timestamp.today().normalize()
    min_dte = horizon_days - 14
    max_dte = horizon_days + 45
    try:
        obj         = yf.Ticker(ticker)
        expirations = obj.options
        if not expirations:
            return None
        valid = [e for e in expirations
                 if min_dte <= (pd.Timestamp(e) - today).days <= max_dte]
        if not valid:
            return None
        best = None
        for exp in valid[:3]:
            dte   = max((pd.Timestamp(exp) - today).days, 1)
            T     = dte / 365.0
            chain = obj.option_chain(exp).calls
            chain = chain[
                (chain["strike"] >= current_price * 0.90) &
                (chain["strike"] <= target_price  * 1.05) &
                (chain["ask"]    > 0)
            ]
            for _, row in chain.iterrows():
                fv  = black_scholes_call(current_price, row["strike"], T, risk_free, sigma)
                ask = row["ask"]
                if fv <= ask * 1.05:
                    continue
                roi = (target_price - row["strike"] - ask) / ask * 100
                if best is None or roi > best["ROI"]:
                    best = {
                        "Strike"    : row["strike"],
                        "Expiry"    : exp,
                        "DTE"       : dte,
                        "Ask"       : round(ask, 2),
                        "Fair_Value": round(fv, 4),
                        "ROI"       : round(roi, 1),
                        "ROI_Exit"  : round((fv - ask) / ask * 100, 1),
                    }
    except Exception:
        return None
    return best


# ── Page ──────────────────────────────────────────────────────────────────────

st.title("🎯 Top Picks — Calls")
st.markdown(
    "Finds stocks where **Top Tier analysts** have a bullish consensus "
    "**AND** a cheap mispriced call option is available. Ranked by estimated ROI.\n\n"
    "**⚡ Fast two-phase scan:** Oracle prefilter first, then options only for qualifying tickers."
)
st.markdown("---")

c1, c2 = st.columns([3, 1])
with c1:
    min_roi = st.slider("Minimum Est. ROI %", 0, 500, 50, 10, key="board_min_roi")
with c2:
    st.markdown("<br>", unsafe_allow_html=True)
    run_board = st.button("🔍 Scan All Stocks", type="primary", use_container_width=True)

if run_board:
    # ── PHASE 1: Single bulk call for all Oracle price targets ───────────────
    st.markdown("**Phase 1 of 2** — Loading Oracle price targets (single API call)...")
    p1 = st.progress(0.0)
    s1 = st.empty()

    all_pt = load_all_price_targets()   # ~457 rows, one request
    p1.progress(1.0)

    # Fetch sigma in parallel for the qualifying tickers only
    oracle_hits = []
    total = len(all_pt)
    done  = 0

    def _fetch_sigma(pt):
        sigma = fetch_ohlc_sigma(pt["Ticker"])
        if sigma is None:
            return None
        return {"Ticker": pt["Ticker"], "Name": pt["Ticker"], "pt": pt, "sigma": sigma}

    with ThreadPoolExecutor(max_workers=20) as ex:
        futs = {ex.submit(_fetch_sigma, pt): pt for pt in all_pt}
        for f in as_completed(futs):
            done += 1
            res = f.result()
            if res:
                oracle_hits.append(res)
            p1.progress(done / max(total, 1))
            s1.markdown(
                f"Phase 1: **{done}/{total}** price targets loaded — "
                f"**{len(oracle_hits)}** with volatility data"
            )

    p1.progress(1.0)
    s1.success(f"Phase 1 done — {len(oracle_hits)} tickers qualify. Now checking options...")

    # ── PHASE 2: Options scan on oracle hits only ────────────────────────────
    st.markdown(f"**Phase 2 of 2** — Scanning options for {len(oracle_hits)} qualifying tickers...")
    p2 = st.progress(0.0)
    s2 = st.empty()

    results = []
    done2   = 0

    def _scan_options(hit):
        t        = hit["Ticker"]
        pt       = hit["pt"]
        sigma    = hit["sigma"]
        cp       = float(pt["current_price"])
        target3m = float(pt["target_price_3m"])
        target6m = float(pt["target_price_6m"])
        upside   = (target3m / cp - 1) * 100
        best     = find_best_call(t, target3m, cp, sigma)
        if best is None:
            return None
        return {
            "Ticker"            : t,
            "Name"              : hit["Name"],
            "Price"             : cp,
            "3M Target"         : target3m,
            "6M Target"         : target6m,
            "Upside"            : upside,
            "Strike"            : best["Strike"],
            "Expiry"            : best["Expiry"],
            "DTE"               : best["DTE"],
            "Ask"               : best["Ask"],
            "Fair Value"        : best["Fair_Value"],
            "ROI"               : best["ROI"],
            "ROI_Exit"          : best["ROI_Exit"],
        }

    with ThreadPoolExecutor(max_workers=10) as ex:
        futs2 = {ex.submit(_scan_options, h): h for h in oracle_hits}
        for f in as_completed(futs2):
            done2 += 1
            res = f.result()
            if res:
                results.append(res)
            p2.progress(done2 / max(len(oracle_hits), 1))
            s2.markdown(
                f"Phase 2: **{done2}/{len(oracle_hits)}** options checked — "
                f"**{len(results)}** top picks found"
            )

    p2.progress(1.0)
    s2.success(f"✅ Scan complete — {len(results)} top picks found.")
    st.session_state["top_picks_results"] = results


# ── Display ────────────────────────────────────────────────────────────────────

if st.session_state.get("top_picks_results"):
    raw      = st.session_state["top_picks_results"]
    filtered = sorted([r for r in raw if r["ROI"] >= min_roi],
                      key=lambda x: x["ROI"], reverse=True)

    if not filtered:
        st.warning(f"No results with Est. ROI ≥ {min_roi}%. Lower the slider.")
    else:
        st.markdown(f"#### {len(filtered)} Top Picks — sorted by Est. ROI (≥ {min_roi}%)")

        df = pd.DataFrame(filtered)
        display_df = pd.DataFrame({
            "Ticker"            : df["Ticker"],
            "Name"              : df["Name"],
            "Price"             : df["Price"].apply(lambda x: f"${x:.2f}"),
            "3M Target"         : df["3M Target"].apply(lambda x: f"${x:.2f}"),
            "6M Target"         : df["6M Target"].apply(lambda x: f"${x:.2f}"),
            "Upside"            : df["Upside"].apply(lambda x: f"+{x:.1f}%"),
            "Best Call"         : df["Strike"].apply(lambda x: f"${x:.0f} Call"),
            "Expiry"            : df["Expiry"],
            "DTE"               : df["DTE"].apply(lambda x: f"{x}d"),
            "Ask"               : df["Ask"].apply(lambda x: f"${x:.2f}"),
            "Fair Value"        : df["Fair Value"].apply(lambda x: f"${x:.4f}"),
            "ROI (at Expiry)"   : df["ROI"].apply(lambda x: f"{x:.0f}%"),
            "ROI (Sell at Hit)" : df["ROI_Exit"].apply(lambda x: f"{x:.0f}%"),
        })

        def color_roi(val):
            try:
                n = float(str(val).replace("%", ""))
                if n >= 300: return "color: #4caf50; font-weight: bold"
                if n >= 150: return "color: #8bc34a; font-weight: bold"
                if n >=  50: return "color: #cddc39"
                return "color: #ff9800"
            except Exception:
                return ""

        def color_upside(val):
            try:
                n = float(str(val).replace("+", "").replace("%", ""))
                if n >= 10: return "color: #4caf50"
                if n >=  5: return "color: #8bc34a"
                return "color: white"
            except Exception:
                return ""

        styled = (
            display_df.style
            .applymap(color_roi,    subset=["ROI (at Expiry)", "ROI (Sell at Hit)"])
            .applymap(color_upside, subset=["Upside"])
        )
        st.dataframe(styled, use_container_width=True, hide_index=True)

elif not run_board:
    st.info(
        "Click **Scan All Stocks** to find top picks. "
        "The scan is split into two fast phases — oracle prefilter (all tickers), "
        "then options lookup only for the qualifying ones."
    )
