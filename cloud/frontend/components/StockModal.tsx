"use client";
import { useEffect, useState } from "react";
import dynamic from "next/dynamic";
import { X } from "lucide-react";

const OHLCChart = dynamic(() => import("@/components/OHLCChart"), { ssr: false });

interface OHLCRow { Date: string; Open: number; High: number; Low: number; Close: number }

export interface Candidate {
  Ticker: string;
  Firm: string;
  Action: string;
  ToGrade: string;
  GradeDate: string;
  current_price: number;
  target_3m: number;
  target_6m: number;
  season_win_rate: number;
  season_avg_return: number;
  hist_vol: number;
  is_biased: boolean;
  near_earnings: boolean;
  days_to_earnings: number | null;
  // Fundamental Value Engine (Phase 1 + Phase 3 — null until pipeline runs)
  passes_health_screen?: boolean | null;
  debt_equity?: number | null;
  current_ratio?: number | null;
  interest_coverage?: number | null;
  insider_buy_flag?: boolean | null;
  insider_strength?: string | null;
  net_insider_buy_90d?: number | null;
}

interface FundDetail {
  debt_equity: number | null;
  current_ratio: number | null;
  interest_coverage: number | null;
  fcf_yr0: number | null;
  fcf_yr1: number | null;
  fcf_yr2: number | null;
  fcf_positive_years: number | null;
  ni_yr0: number | null;
  ni_yr1: number | null;
  passes_health_screen: boolean | null;
}

const ACTION_LABEL: Record<string, string> = {
  up: "Upgrade",
  init: "Initiate",
  down: "Downgrade",
  main: "Maintain",
  reit: "Reiterate",
};

const API = process.env.NEXT_PUBLIC_API_URL!;

function fmt$(n: number | null | undefined) {
  if (n == null) return "-";
  const abs  = Math.abs(n);
  const sign = n < 0 ? "-" : "+";
  if (abs >= 1e9) return `${sign}$${(abs / 1e9).toFixed(1)}B`;
  if (abs >= 1e6) return `${sign}$${(abs / 1e6).toFixed(1)}M`;
  if (abs >= 1e3) return `${sign}$${(abs / 1e3).toFixed(0)}K`;
  return `${sign}$${abs.toFixed(0)}`;
}

function MetricPill({
  label, value, pass, warn,
}: { label: string; value: string; pass: boolean | null; warn?: boolean }) {
  const color =
    pass === null  ? "text-zinc-400 bg-zinc-800/60" :
    warn           ? "text-amber-400 bg-amber-950/40" :
    pass           ? "text-green-400 bg-green-950/40" :
                     "text-red-400   bg-red-950/40";
  return (
    <div className={`rounded px-2 py-1.5 text-center ${color}`}>
      <div className="text-[10px] text-zinc-500 mb-0.5">{label}</div>
      <div className="text-xs font-semibold">{value}</div>
    </div>
  );
}

export default function StockModal({
  candidate: c,
  onClose,
}: {
  candidate: Candidate;
  onClose: () => void;
}) {
  const [ohlc,    setOhlc]    = useState<OHLCRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [fund,    setFund]    = useState<FundDetail | null>(null);

  useEffect(() => {
    setLoading(true);
    fetch(`${API}/ohlc/${c.Ticker}?days=365`)
      .then((r) => r.json())
      .then(setOhlc)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [c.Ticker]);

  useEffect(() => {
    fetch(`${API}/fundamentals/${c.Ticker}`)
      .then((r) => (r.ok ? r.json() : null))
      .then(setFund)
      .catch(() => {});
  }, [c.Ticker]);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  const up3m = ((c.target_3m / c.current_price - 1) * 100).toFixed(1);
  const up6m = ((c.target_6m / c.current_price - 1) * 100).toFixed(1);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/70 backdrop-blur-sm" onClick={onClose} />

      {/* Panel */}
      <div className="relative z-10 w-full max-w-2xl bg-zinc-950 border border-zinc-800 rounded-xl shadow-2xl overflow-hidden">

        {/* Header */}
        <div className="flex items-start justify-between px-5 py-4 border-b border-zinc-800">
          <div>
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-2xl font-bold text-zinc-100">{c.Ticker}</span>
              <span className="text-xs px-2 py-0.5 rounded bg-zinc-800 text-zinc-400">
                {ACTION_LABEL[c.Action] ?? c.Action} → {c.ToGrade}
              </span>
              {c.is_biased && (
                <span className="text-[10px] font-bold bg-red-900 text-red-300 px-1.5 py-0.5 rounded">
                  BIASED
                </span>
              )}
              {c.near_earnings && (
                <span className="text-[10px] font-bold bg-amber-900 text-amber-300 px-1.5 py-0.5 rounded">
                  EARNINGS{c.days_to_earnings != null ? ` ${c.days_to_earnings}d` : ""}
                </span>
              )}
            </div>
            <div className="text-sm text-zinc-500 mt-1">
              {c.Firm} · {String(c.GradeDate).slice(0, 10)}
            </div>
          </div>
          <button
            onClick={onClose}
            className="text-zinc-500 hover:text-zinc-200 transition-colors p-1 rounded hover:bg-zinc-800 ml-4 shrink-0"
            aria-label="Close"
          >
            <X size={18} />
          </button>
        </div>

        {/* Stats row */}
        <div className="grid grid-cols-4 divide-x divide-zinc-800 border-b border-zinc-800 text-center">
          {[
            { label: "Price",     value: `$${c.current_price.toFixed(2)}`,  sub: null },
            { label: "3M Target", value: `$${c.target_3m.toFixed(2)}`,      sub: `+${up3m}%` },
            { label: "6M Target", value: `$${c.target_6m.toFixed(2)}`,      sub: `+${up6m}%` },
            { label: "Win Rate",  value: `${c.season_win_rate.toFixed(0)}%`, sub: `${c.season_avg_return.toFixed(1)}% avg` },
          ].map(({ label, value, sub }) => (
            <div key={label} className="px-3 py-3">
              <div className="text-[11px] text-zinc-500 mb-0.5">{label}</div>
              <div className="text-sm font-semibold text-zinc-100">{value}</div>
              {sub && <div className="text-[11px] text-green-400">{sub}</div>}
            </div>
          ))}
        </div>

        {/* Chart */}
        <div className="p-4">
          {loading ? (
            <div className="h-[320px] flex items-center justify-center text-sm text-zinc-500">
              Loading chart…
            </div>
          ) : ohlc.length > 0 ? (
            <>
              <div className="flex items-center gap-4 text-[11px] text-zinc-500 mb-2">
                <span>{c.Ticker} · 1Y · {ohlc.length} sessions</span>
                <span className="flex items-center gap-1">
                  <svg width="18" height="6"><line x1="0" y1="3" x2="18" y2="3" stroke="#86efac" strokeWidth="1.5" strokeDasharray="3 2"/></svg>
                  3M target
                </span>
                <span className="flex items-center gap-1">
                  <svg width="18" height="6"><line x1="0" y1="3" x2="18" y2="3" stroke="#4ade80" strokeWidth="1.5" strokeDasharray="3 2"/></svg>
                  6M target
                </span>
              </div>
              <OHLCChart data={ohlc} target3m={c.target_3m} target6m={c.target_6m} />
            </>
          ) : (
            <div className="h-[320px] flex items-center justify-center text-sm text-zinc-500">
              No price history available for {c.Ticker}
            </div>
          )}
        </div>

        {/* Fundamental Health + Insider */}
        <div className="px-4 pb-4 border-t border-zinc-800 pt-3 flex flex-col gap-3">
          {/* Health Gate header */}
          <div className="flex items-center gap-2">
            <span className="text-[11px] font-semibold text-zinc-400 uppercase tracking-wide">
              Fundamental Health
            </span>
            {fund ? (
              fund.passes_health_screen === true  ? <span className="text-[10px] font-bold bg-green-900 text-green-300 px-1.5 py-0.5 rounded">PASS</span>  :
              fund.passes_health_screen === false ? <span className="text-[10px] font-bold bg-red-900   text-red-300   px-1.5 py-0.5 rounded">FAIL</span>  :
                                                    <span className="text-[10px] font-bold bg-zinc-800  text-zinc-400  px-1.5 py-0.5 rounded">NO DATA</span>
            ) : (
              <span className="text-[10px] text-zinc-600">loading…</span>
            )}
          </div>

          {/* Ratio pills */}
          {fund && (
            <div className="grid grid-cols-4 gap-2">
              <MetricPill
                label="Debt / Equity"
                value={fund.debt_equity != null ? fund.debt_equity.toFixed(2) : "-"}
                pass={fund.debt_equity == null ? null : fund.debt_equity <= 2.0}
                warn={fund.debt_equity != null && fund.debt_equity > 1.0 && fund.debt_equity <= 2.0}
              />
              <MetricPill
                label="Current Ratio"
                value={fund.current_ratio != null ? fund.current_ratio.toFixed(2) : "-"}
                pass={fund.current_ratio == null ? null : fund.current_ratio >= 1.2}
                warn={fund.current_ratio != null && fund.current_ratio >= 1.2 && fund.current_ratio < 1.5}
              />
              <MetricPill
                label="Interest Cov."
                value={fund.interest_coverage != null ? `${fund.interest_coverage.toFixed(1)}x` : "N/A"}
                pass={fund.interest_coverage == null ? null : fund.interest_coverage >= 3.0}
                warn={fund.interest_coverage != null && fund.interest_coverage >= 3.0 && fund.interest_coverage < 5.0}
              />
              <MetricPill
                label="FCF Positive"
                value={fund.fcf_positive_years != null ? `${fund.fcf_positive_years}/3 yr` : "-"}
                pass={fund.fcf_positive_years == null ? null : fund.fcf_positive_years >= 2}
                warn={fund.fcf_positive_years === 2}
              />
            </div>
          )}

          {/* Insider Activity */}
          {(c.insider_strength || c.net_insider_buy_90d != null) && (
            <div className="flex items-center gap-3 text-xs">
              <span className="text-[11px] font-semibold text-zinc-400 uppercase tracking-wide">
                Insider (90d)
              </span>
              {c.insider_strength && c.insider_strength !== "no_data" && (
                <span className={`font-semibold ${
                  c.insider_strength === "strong_buy" || c.insider_strength === "mild_buy"
                    ? "text-blue-400"
                    : c.insider_strength === "strong_sell" || c.insider_strength === "mild_sell"
                    ? "text-red-400"
                    : "text-zinc-400"
                }`}>
                  {c.insider_strength.replace("_", " ").toUpperCase()}
                </span>
              )}
              {c.net_insider_buy_90d != null && (
                <span className={c.net_insider_buy_90d >= 0 ? "text-blue-300" : "text-red-300"}>
                  {fmt$(c.net_insider_buy_90d)} net
                </span>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
