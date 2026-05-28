"use client";

import { useMemo, useState } from "react";

interface SummaryRow {
  year: number;
  n_all_signals: number;
  n_filtered_signals: number;
  all_return_pct: number;
  filtered_return_pct: number;
  n_exact_match: number;
  n_firm_sector: number;
  n_firm_only: number;
  n_no_data: number;
  spy_return_pct: number | null;
}

interface PerfRow {
  firm: string;
  sector: string;
  month: number;
  n_signals: number;
  hit_rate_pct: number;
  avg_return_pct: number;
  last_seen_year: number;
  trusted: number;
}

interface LiveRow {
  ticker: string;
  signal_date: string;
  firm: string;
  sector: string;
  month: number;
  hit_rate_pct: number | null;
  n_signals: number | null;
  avg_return_pct: number | null;
  fallback_level: number;
  take_signal: number;
}

interface Props {
  summary: SummaryRow[];
  perf: PerfRow[];
  live: LiveRow[];
}

const MONTHS = [
  "", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

function StatCard({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="rounded border border-zinc-800 bg-zinc-900 px-4 py-3">
      <div className="text-xs text-zinc-500 mb-1">{label}</div>
      <div className="text-lg font-bold text-zinc-100">{value}</div>
      {sub && <div className="text-xs text-zinc-500 mt-0.5">{sub}</div>}
    </div>
  );
}

function ReturnBar({
  filtered, all, spy, maxAbs,
}: {
  filtered: number; all: number; spy: number | null; maxAbs: number;
}) {
  const scale = (v: number) => `${Math.min(Math.abs(v) / maxAbs * 100, 100)}%`;
  const color = (v: number) => v >= 0 ? "bg-emerald-500" : "bg-red-500";
  return (
    <div className="space-y-1">
      {[
        { label: "Combo", val: filtered, cls: "bg-violet-500" },
        { label: "All",   val: all,      cls: color(all) },
        { label: "SPY",   val: spy ?? 0, cls: "bg-zinc-500" },
      ].map(({ label, val, cls }) => (
        <div key={label} className="flex items-center gap-2">
          <span className="text-xs text-zinc-500 w-10 shrink-0">{label}</span>
          <div className="flex-1 h-3 bg-zinc-800 rounded overflow-hidden">
            <div className={`h-3 rounded ${cls}`} style={{ width: scale(val) }} />
          </div>
          <span className={`text-xs w-12 text-right ${val >= 0 ? "text-emerald-400" : "text-red-400"}`}>
            {val >= 0 ? "+" : ""}{val.toFixed(1)}%
          </span>
        </div>
      ))}
    </div>
  );
}

function FallbackBadge({ level }: { level: number }) {
  if (level === 0) return (
    <span className="px-1.5 py-0.5 rounded text-xs bg-violet-900/60 text-violet-300 font-mono">exact</span>
  );
  if (level === 1) return (
    <span className="px-1.5 py-0.5 rounded text-xs bg-sky-900/60 text-sky-300 font-mono">firm+sector</span>
  );
  if (level === 2) return (
    <span className="px-1.5 py-0.5 rounded text-xs bg-amber-900/60 text-amber-300 font-mono">firm only</span>
  );
  return (
    <span className="px-1.5 py-0.5 rounded text-xs bg-zinc-800 text-zinc-500 font-mono">no data</span>
  );
}

export default function ComboClient({ summary, perf, live }: Props) {
  const [tab, setTab] = useState<"wfo" | "leaderboard" | "live">("wfo");
  const [leaderFilter, setLeaderFilter] = useState<"trusted" | "all">("trusted");
  const [liveFilter, setLiveFilter]     = useState<"take" | "all">("take");

  const rows = useMemo(() => [...summary].sort((a, b) => a.year - b.year), [summary]);

  const stats = useMemo(() => {
    const n = rows.length;
    if (n === 0) return null;
    const comboAvg = rows.reduce((s, r) => s + r.filtered_return_pct, 0) / n;
    const allAvg   = rows.reduce((s, r) => s + r.all_return_pct,      0) / n;
    const comboBetter = rows.filter(r => r.filtered_return_pct > r.all_return_pct).length;

    const CAPITAL = 15_000;
    let comboPV = CAPITAL, allPV = CAPITAL, spyPV = CAPITAL;
    for (const r of rows) {
      comboPV *= (1 + r.filtered_return_pct / 100);
      allPV   *= (1 + r.all_return_pct      / 100);
      if (r.spy_return_pct != null) spyPV *= (1 + r.spy_return_pct / 100);
    }

    return {
      comboAvg, allAvg, comboBetter, n,
      comboTotal: (comboPV / CAPITAL - 1) * 100,
      allTotal  : (allPV   / CAPITAL - 1) * 100,
      spyTotal  : (spyPV   / CAPITAL - 1) * 100,
    };
  }, [rows]);

  const maxAbs = useMemo(() => {
    let m = 5;
    for (const r of rows) {
      m = Math.max(m, Math.abs(r.filtered_return_pct), Math.abs(r.all_return_pct),
                   Math.abs(r.spy_return_pct ?? 0));
    }
    return m;
  }, [rows]);

  const leaderRows = useMemo(() => {
    const data = leaderFilter === "trusted"
      ? perf.filter(r => r.trusted === 1)
      : [...perf];
    return data.sort((a, b) => b.hit_rate_pct - a.hit_rate_pct || b.n_signals - a.n_signals);
  }, [perf, leaderFilter]);

  const liveRows = useMemo(() => {
    const data = liveFilter === "take"
      ? live.filter(r => r.take_signal === 1)
      : [...live];
    return data.sort((a, b) => (b.hit_rate_pct ?? 0) - (a.hit_rate_pct ?? 0));
  }, [live, liveFilter]);

  const tabs = [
    { id: "wfo",         label: "Walk-Forward" },
    { id: "leaderboard", label: "Combo Leaderboard" },
    { id: "live",        label: "Live Signals" },
  ] as const;

  return (
    <div className="space-y-6">
      {/* Tabs */}
      <div className="flex gap-1 border-b border-zinc-800">
        {tabs.map(t => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`px-4 py-2 text-sm rounded-t transition-colors ${
              tab === t.id
                ? "bg-zinc-800 text-zinc-100"
                : "text-zinc-500 hover:text-zinc-300"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* ── Walk-Forward tab ── */}
      {tab === "wfo" && stats && (
        <div className="space-y-8">
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            <StatCard
              label={`Combo ${rows[0]?.year}–${rows[rows.length - 1]?.year} Total`}
              value={`${stats.comboTotal >= 0 ? "+" : ""}${stats.comboTotal.toFixed(1)}%`}
              sub="compounded"
            />
            <StatCard
              label="All Signals Total"
              value={`${stats.allTotal >= 0 ? "+" : ""}${stats.allTotal.toFixed(1)}%`}
              sub="compounded"
            />
            <StatCard
              label="SPY Total"
              value={`${stats.spyTotal >= 0 ? "+" : ""}${stats.spyTotal.toFixed(1)}%`}
              sub="compounded"
            />
            <StatCard
              label="Combo Beat All Signals"
              value={`${stats.comboBetter} / ${stats.n} years`}
              sub={`avg combo ${stats.comboAvg >= 0 ? "+" : ""}${stats.comboAvg.toFixed(1)}% vs all ${stats.allAvg >= 0 ? "+" : ""}${stats.allAvg.toFixed(1)}%`}
            />
          </div>

          <div>
            <h2 className="text-sm font-semibold text-zinc-300 mb-4">
              Annual Returns — Combo Filter vs All Signals vs SPY
            </h2>
            <div className="space-y-4">
              {rows.map(r => (
                <div key={r.year}>
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-sm font-medium text-zinc-300">{r.year}</span>
                    <span className="text-xs text-zinc-500">
                      {r.n_filtered_signals}/{r.n_all_signals} signals kept
                      {r.filtered_return_pct > r.all_return_pct
                        ? <span className="ml-2 text-violet-400">▲ combo wins</span>
                        : <span className="ml-2 text-zinc-600">all wins</span>}
                    </span>
                  </div>
                  <ReturnBar
                    filtered={r.filtered_return_pct}
                    all={r.all_return_pct}
                    spy={r.spy_return_pct}
                    maxAbs={maxAbs}
                  />
                  <div className="flex gap-3 mt-1 text-xs text-zinc-600">
                    <span>exact={r.n_exact_match}</span>
                    <span>firm+sector={r.n_firm_sector}</span>
                    <span>firm={r.n_firm_only}</span>
                    <span>no data={r.n_no_data}</span>
                  </div>
                </div>
              ))}
            </div>
          </div>

          <div>
            <h2 className="text-sm font-semibold text-zinc-300 mb-3">Full Year Results</h2>
            <div className="overflow-x-auto">
              <table className="w-full text-xs text-left">
                <thead>
                  <tr className="text-zinc-500 border-b border-zinc-800">
                    <th className="py-2 pr-4">Year</th>
                    <th className="py-2 pr-4 text-violet-400">Combo Return</th>
                    <th className="py-2 pr-4">All Return</th>
                    <th className="py-2 pr-4">SPY</th>
                    <th className="py-2 pr-4">Kept</th>
                    <th className="py-2 pr-4">Total</th>
                    <th className="py-2 pr-4">Exact</th>
                    <th className="py-2 pr-4">Firm+Sec</th>
                    <th className="py-2 pr-4">Firm</th>
                    <th className="py-2 pr-4">No Data</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map(r => {
                    const wins = r.filtered_return_pct > r.all_return_pct;
                    return (
                      <tr key={r.year} className="border-b border-zinc-800/50 hover:bg-zinc-800/30">
                        <td className="py-2 pr-4 font-medium text-zinc-200">{r.year}</td>
                        <td className={`py-2 pr-4 font-semibold ${wins ? "text-violet-400" : "text-zinc-300"}`}>
                          {r.filtered_return_pct >= 0 ? "+" : ""}{r.filtered_return_pct.toFixed(1)}%
                        </td>
                        <td className={`py-2 pr-4 ${r.all_return_pct >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                          {r.all_return_pct >= 0 ? "+" : ""}{r.all_return_pct.toFixed(1)}%
                        </td>
                        <td className="py-2 pr-4 text-zinc-400">
                          {r.spy_return_pct != null
                            ? `${r.spy_return_pct >= 0 ? "+" : ""}${r.spy_return_pct.toFixed(1)}%`
                            : "—"}
                        </td>
                        <td className="py-2 pr-4 text-zinc-400">{r.n_filtered_signals}</td>
                        <td className="py-2 pr-4 text-zinc-400">{r.n_all_signals}</td>
                        <td className="py-2 pr-4 text-zinc-400">{r.n_exact_match}</td>
                        <td className="py-2 pr-4 text-zinc-400">{r.n_firm_sector}</td>
                        <td className="py-2 pr-4 text-zinc-400">{r.n_firm_only}</td>
                        <td className="py-2 pr-4 text-zinc-400">{r.n_no_data}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {/* ── Leaderboard tab ── */}
      {tab === "leaderboard" && (
        <div className="space-y-4">
          <div className="flex items-center gap-2">
            <span className="text-xs text-zinc-500">Show:</span>
            {(["trusted", "all"] as const).map(f => (
              <button
                key={f}
                onClick={() => setLeaderFilter(f)}
                className={`px-3 py-1 rounded text-xs transition-colors ${
                  leaderFilter === f
                    ? "bg-zinc-700 text-zinc-100"
                    : "text-zinc-500 hover:text-zinc-300"
                }`}
              >
                {f === "trusted" ? `Trusted ≥55%` : "All combos"}
              </button>
            ))}
            <span className="text-xs text-zinc-600 ml-2">
              {leaderRows.length} combos
            </span>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-xs text-left">
              <thead>
                <tr className="text-zinc-500 border-b border-zinc-800">
                  <th className="py-2 pr-4">Firm</th>
                  <th className="py-2 pr-4">Sector</th>
                  <th className="py-2 pr-4">Month</th>
                  <th className="py-2 pr-4">Signals</th>
                  <th className="py-2 pr-4 text-violet-400">Hit Rate</th>
                  <th className="py-2 pr-4">Avg Return</th>
                  <th className="py-2 pr-4">Last Seen</th>
                  <th className="py-2 pr-4">Status</th>
                </tr>
              </thead>
              <tbody>
                {leaderRows.map((r, i) => (
                  <tr key={i} className="border-b border-zinc-800/50 hover:bg-zinc-800/30">
                    <td className="py-2 pr-4 font-medium text-zinc-200 max-w-[160px] truncate">{r.firm}</td>
                    <td className="py-2 pr-4 text-zinc-400">{r.sector}</td>
                    <td className="py-2 pr-4 text-sky-400 font-mono">{MONTHS[r.month]}</td>
                    <td className="py-2 pr-4 text-zinc-400">{r.n_signals}</td>
                    <td className={`py-2 pr-4 font-semibold ${r.trusted ? "text-violet-400" : "text-zinc-400"}`}>
                      {r.hit_rate_pct.toFixed(0)}%
                    </td>
                    <td className={`py-2 pr-4 ${r.avg_return_pct >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                      {r.avg_return_pct >= 0 ? "+" : ""}{r.avg_return_pct.toFixed(1)}%
                    </td>
                    <td className="py-2 pr-4 text-zinc-500">{r.last_seen_year}</td>
                    <td className="py-2 pr-4">
                      {r.trusted === 1
                        ? <span className="px-1.5 py-0.5 rounded text-xs bg-violet-900/50 text-violet-300">trusted</span>
                        : <span className="px-1.5 py-0.5 rounded text-xs bg-zinc-800 text-zinc-500">low rate</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* ── Live Signals tab ── */}
      {tab === "live" && (
        <div className="space-y-4">
          <div className="flex items-center gap-2">
            <span className="text-xs text-zinc-500">Show:</span>
            {(["take", "all"] as const).map(f => (
              <button
                key={f}
                onClick={() => setLiveFilter(f)}
                className={`px-3 py-1 rounded text-xs transition-colors ${
                  liveFilter === f
                    ? "bg-zinc-700 text-zinc-100"
                    : "text-zinc-500 hover:text-zinc-300"
                }`}
              >
                {f === "take" ? "Pass filter" : "All signals"}
              </button>
            ))}
            <span className="text-xs text-zinc-600 ml-2">
              {liveRows.length} signals
            </span>
          </div>

          {liveRows.length === 0 ? (
            <div className="text-sm text-zinc-500 py-8 text-center">
              No signals in the last 90 days pass the combo filter.
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-xs text-left">
                <thead>
                  <tr className="text-zinc-500 border-b border-zinc-800">
                    <th className="py-2 pr-4">Ticker</th>
                    <th className="py-2 pr-4">Date</th>
                    <th className="py-2 pr-4">Firm</th>
                    <th className="py-2 pr-4">Sector</th>
                    <th className="py-2 pr-4">Month</th>
                    <th className="py-2 pr-4 text-violet-400">Hit Rate</th>
                    <th className="py-2 pr-4">Signals</th>
                    <th className="py-2 pr-4">Avg Ret</th>
                    <th className="py-2 pr-4">Match</th>
                    <th className="py-2 pr-4">Action</th>
                  </tr>
                </thead>
                <tbody>
                  {liveRows.map((r, i) => (
                    <tr key={i} className={`border-b border-zinc-800/50 hover:bg-zinc-800/30 ${
                      r.take_signal ? "" : "opacity-50"
                    }`}>
                      <td className="py-2 pr-4 font-bold text-zinc-100">{r.ticker}</td>
                      <td className="py-2 pr-4 text-zinc-400 font-mono">{r.signal_date}</td>
                      <td className="py-2 pr-4 text-zinc-300 max-w-[140px] truncate">{r.firm}</td>
                      <td className="py-2 pr-4 text-zinc-400">{r.sector}</td>
                      <td className="py-2 pr-4 text-sky-400 font-mono">{MONTHS[r.month]}</td>
                      <td className={`py-2 pr-4 font-semibold ${
                        r.hit_rate_pct != null && r.hit_rate_pct >= 65 ? "text-violet-400" :
                        r.hit_rate_pct != null && r.hit_rate_pct >= 55 ? "text-emerald-400" :
                        "text-zinc-500"
                      }`}>
                        {r.hit_rate_pct != null ? `${r.hit_rate_pct.toFixed(0)}%` : "—"}
                      </td>
                      <td className="py-2 pr-4 text-zinc-400">{r.n_signals ?? "—"}</td>
                      <td className={`py-2 pr-4 ${
                        r.avg_return_pct != null && r.avg_return_pct >= 0
                          ? "text-emerald-400" : "text-red-400"
                      }`}>
                        {r.avg_return_pct != null
                          ? `${r.avg_return_pct >= 0 ? "+" : ""}${r.avg_return_pct.toFixed(1)}%`
                          : "—"}
                      </td>
                      <td className="py-2 pr-4">
                        <FallbackBadge level={r.fallback_level} />
                      </td>
                      <td className="py-2 pr-4">
                        {r.take_signal
                          ? <span className="px-1.5 py-0.5 rounded text-xs bg-emerald-900/50 text-emerald-300 font-semibold">TAKE</span>
                          : <span className="px-1.5 py-0.5 rounded text-xs bg-zinc-800 text-zinc-500">skip</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
