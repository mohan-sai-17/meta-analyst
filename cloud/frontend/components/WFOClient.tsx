"use client";

import { useMemo } from "react";

interface WFORow {
  year: number;
  best_take_profit_pct: number;
  best_hold_days: number;
  train_score_pct: number;
  test_return_pct: number;
  fixed_test_return_pct: number;
  test_trades: number;
  test_win_rate_pct: number;
  fixed_trades: number;
  fixed_win_rate_pct: number;
  spy_return_pct: number | null;
}

interface Props {
  data: WFORow[];
}

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
  wfo, fixed, spy, maxAbs,
}: {
  wfo: number; fixed: number; spy: number | null; maxAbs: number;
}) {
  const scale = (v: number) => `${Math.min(Math.abs(v) / maxAbs * 100, 100)}%`;
  const color = (v: number) => v >= 0 ? "bg-emerald-500" : "bg-red-500";
  return (
    <div className="space-y-1">
      {[
        { label: "WFO", val: wfo, cls: "bg-violet-500" },
        { label: "Fixed", val: fixed, cls: color(fixed) },
        { label: "SPY", val: spy ?? 0, cls: "bg-zinc-500" },
      ].map(({ label, val, cls }) => (
        <div key={label} className="flex items-center gap-2">
          <span className="text-xs text-zinc-500 w-9 shrink-0">{label}</span>
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

export default function WFOClient({ data }: Props) {
  const rows = useMemo(() => [...data].sort((a, b) => a.year - b.year), [data]);

  const stats = useMemo(() => {
    const n = rows.length;
    if (n === 0) return null;
    const wfoAvg   = rows.reduce((s, r) => s + r.test_return_pct, 0) / n;
    const fixedAvg = rows.reduce((s, r) => s + r.fixed_test_return_pct, 0) / n;
    const wfoBetter = rows.filter(r => r.test_return_pct > r.fixed_test_return_pct).length;

    // Compound WFO: each year multiplied
    const CAPITAL = 15_000;
    let wfoPV = CAPITAL, fixedPV = CAPITAL, spyPV = CAPITAL;
    for (const r of rows) {
      wfoPV   *= (1 + r.test_return_pct / 100);
      fixedPV *= (1 + r.fixed_test_return_pct / 100);
      if (r.spy_return_pct != null) spyPV *= (1 + r.spy_return_pct / 100);
    }

    return {
      wfoAvg, fixedAvg, wfoBetter, n,
      wfoTotal  : (wfoPV   / CAPITAL - 1) * 100,
      fixedTotal: (fixedPV / CAPITAL - 1) * 100,
      spyTotal  : (spyPV   / CAPITAL - 1) * 100,
    };
  }, [rows]);

  const maxAbs = useMemo(() => {
    let m = 5;
    for (const r of rows) {
      m = Math.max(m, Math.abs(r.test_return_pct), Math.abs(r.fixed_test_return_pct),
                   Math.abs(r.spy_return_pct ?? 0));
    }
    return m;
  }, [rows]);

  // Params evolution
  const paramCounts = useMemo(() => {
    const tpMap: Record<number, number> = {};
    const hdMap: Record<number, number> = {};
    for (const r of rows) {
      tpMap[r.best_take_profit_pct] = (tpMap[r.best_take_profit_pct] ?? 0) + 1;
      hdMap[r.best_hold_days]       = (hdMap[r.best_hold_days]       ?? 0) + 1;
    }
    return { tpMap, hdMap };
  }, [rows]);

  if (!stats) return null;

  return (
    <div className="space-y-8">

      {/* Summary cards */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <StatCard
          label={`WFO ${rows[0]?.year}–${rows[rows.length-1]?.year} Total`}
          value={`${stats.wfoTotal >= 0 ? "+" : ""}${stats.wfoTotal.toFixed(1)}%`}
          sub="compounded"
        />
        <StatCard
          label="Fixed 5%/90d Total"
          value={`${stats.fixedTotal >= 0 ? "+" : ""}${stats.fixedTotal.toFixed(1)}%`}
          sub="compounded"
        />
        <StatCard
          label="SPY Total"
          value={`${stats.spyTotal >= 0 ? "+" : ""}${stats.spyTotal.toFixed(1)}%`}
          sub="compounded"
        />
        <StatCard
          label="WFO Outperformed Fixed"
          value={`${stats.wfoBetter} / ${stats.n} years`}
          sub={`avg WFO ${stats.wfoAvg >= 0 ? "+" : ""}${stats.wfoAvg.toFixed(1)}% vs fixed ${stats.fixedAvg >= 0 ? "+" : ""}${stats.fixedAvg.toFixed(1)}%`}
        />
      </div>

      {/* Year-by-year bars */}
      <div>
        <h2 className="text-sm font-semibold text-zinc-300 mb-4">
          Annual Returns — WFO vs Fixed 5%/90d vs SPY
        </h2>
        <div className="space-y-4">
          {rows.map((r) => (
            <div key={r.year}>
              <div className="flex items-center justify-between mb-1">
                <span className="text-sm font-medium text-zinc-300">{r.year}</span>
                <span className="text-xs text-zinc-500">
                  best: {r.best_take_profit_pct}% / {r.best_hold_days}d
                  {r.test_return_pct > r.fixed_test_return_pct
                    ? <span className="ml-2 text-violet-400">▲ WFO wins</span>
                    : <span className="ml-2 text-zinc-600">fixed wins</span>}
                </span>
              </div>
              <ReturnBar
                wfo={r.test_return_pct}
                fixed={r.fixed_test_return_pct}
                spy={r.spy_return_pct}
                maxAbs={maxAbs}
              />
            </div>
          ))}
        </div>
      </div>

      {/* Params evolution */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-6">
        <div>
          <h2 className="text-sm font-semibold text-zinc-300 mb-3">
            Take-Profit Selected by Year
          </h2>
          <div className="space-y-1">
            {rows.map((r) => (
              <div key={r.year} className="flex items-center gap-2">
                <span className="text-xs text-zinc-500 w-10">{r.year}</span>
                <div className={`px-2 py-0.5 rounded text-xs font-mono font-bold ${
                  r.best_take_profit_pct === 3  ? "bg-blue-900/60 text-blue-300"   :
                  r.best_take_profit_pct === 5  ? "bg-emerald-900/60 text-emerald-300" :
                  r.best_take_profit_pct === 7  ? "bg-amber-900/60 text-amber-300" :
                  "bg-red-900/60 text-red-300"
                }`}>
                  +{r.best_take_profit_pct}%
                </div>
                <span className="text-xs text-zinc-600">
                  (train score {r.train_score_pct >= 0 ? "+" : ""}{r.train_score_pct.toFixed(2)}%)
                </span>
              </div>
            ))}
          </div>
          <div className="mt-3 flex gap-2 flex-wrap">
            {Object.entries(paramCounts.tpMap).sort((a, b) => +a[0] - +b[0]).map(([tp, cnt]) => (
              <span key={tp} className="text-xs text-zinc-400">
                +{tp}% chosen {cnt}×
              </span>
            ))}
          </div>
        </div>

        <div>
          <h2 className="text-sm font-semibold text-zinc-300 mb-3">
            Hold Days Selected by Year
          </h2>
          <div className="space-y-1">
            {rows.map((r) => (
              <div key={r.year} className="flex items-center gap-2">
                <span className="text-xs text-zinc-500 w-10">{r.year}</span>
                <div className={`px-2 py-0.5 rounded text-xs font-mono font-bold ${
                  r.best_hold_days === 30  ? "bg-sky-900/60 text-sky-300"     :
                  r.best_hold_days === 60  ? "bg-teal-900/60 text-teal-300"   :
                  r.best_hold_days === 90  ? "bg-indigo-900/60 text-indigo-300" :
                  "bg-purple-900/60 text-purple-300"
                }`}>
                  {r.best_hold_days}d
                </div>
                <span className="text-xs text-zinc-600">
                  {r.test_trades} trades  {r.test_win_rate_pct.toFixed(0)}% win
                </span>
              </div>
            ))}
          </div>
          <div className="mt-3 flex gap-2 flex-wrap">
            {Object.entries(paramCounts.hdMap).sort((a, b) => +a[0] - +b[0]).map(([hd, cnt]) => (
              <span key={hd} className="text-xs text-zinc-400">
                {hd}d chosen {cnt}×
              </span>
            ))}
          </div>
        </div>
      </div>

      {/* Full table */}
      <div>
        <h2 className="text-sm font-semibold text-zinc-300 mb-3">Full Year Results</h2>
        <div className="overflow-x-auto">
          <table className="w-full text-xs text-left">
            <thead>
              <tr className="text-zinc-500 border-b border-zinc-800">
                <th className="py-2 pr-4">Year</th>
                <th className="py-2 pr-4">Best TP%</th>
                <th className="py-2 pr-4">Best HD</th>
                <th className="py-2 pr-4">Train Score</th>
                <th className="py-2 pr-4 text-violet-400">WFO Return</th>
                <th className="py-2 pr-4">Fixed Return</th>
                <th className="py-2 pr-4">SPY</th>
                <th className="py-2 pr-4">WFO Trades</th>
                <th className="py-2 pr-4">WFO Win%</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const wfoWins = r.test_return_pct > r.fixed_test_return_pct;
                return (
                  <tr key={r.year} className="border-b border-zinc-800/50 hover:bg-zinc-800/30">
                    <td className="py-2 pr-4 font-medium text-zinc-200">{r.year}</td>
                    <td className="py-2 pr-4 font-mono text-amber-400">+{r.best_take_profit_pct}%</td>
                    <td className="py-2 pr-4 font-mono text-sky-400">{r.best_hold_days}d</td>
                    <td className="py-2 pr-4 text-zinc-400">
                      {r.train_score_pct >= 0 ? "+" : ""}{r.train_score_pct.toFixed(2)}%
                    </td>
                    <td className={`py-2 pr-4 font-semibold ${wfoWins ? "text-violet-400" : "text-zinc-300"}`}>
                      {r.test_return_pct >= 0 ? "+" : ""}{r.test_return_pct.toFixed(1)}%
                    </td>
                    <td className={`py-2 pr-4 ${r.fixed_test_return_pct >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                      {r.fixed_test_return_pct >= 0 ? "+" : ""}{r.fixed_test_return_pct.toFixed(1)}%
                    </td>
                    <td className="py-2 pr-4 text-zinc-400">
                      {r.spy_return_pct != null
                        ? `${r.spy_return_pct >= 0 ? "+" : ""}${r.spy_return_pct.toFixed(1)}%`
                        : "—"}
                    </td>
                    <td className="py-2 pr-4 text-zinc-400">{r.test_trades}</td>
                    <td className="py-2 pr-4 text-zinc-400">{r.test_win_rate_pct.toFixed(0)}%</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

    </div>
  );
}
