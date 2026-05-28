"use client";
import { useState, useMemo } from "react";

interface BacktestRow {
  year: number;
  hold_days: number;
  leverage: number;
  top_tier_firms: number;
  trades: number;
  win_rate: number | null;
  avg_pct_return: number | null;
  strategy_return: number | null;
  spy_return: number | null;
  alpha: number | null;
}

function fmt(v: number | null, decimals = 1, plus = true): string {
  if (v == null) return "N/A";
  return `${plus && v > 0 ? "+" : ""}${v.toFixed(decimals)}%`;
}

const HOLD_OPTIONS = [30, 60, 90, 120, 150];
const LEV_OPTIONS  = [1, 2, 3, 4, 5];

export default function BacktestClient({ data }: { data: BacktestRow[] }) {
  const [holdDays, setHoldDays] = useState(30);
  const [leverage, setLeverage] = useState(1);

  const rows = useMemo(
    () => data.filter(r => r.hold_days === holdDays && r.leverage === leverage),
    [data, holdDays, leverage]
  );

  const validRows = rows.filter(r => r.trades > 0);

  const totalTrades = validRows.reduce((s, r) => s + r.trades, 0);
  const totalWins   = validRows.reduce(
    (s, r) => s + Math.round(((r.win_rate ?? 0) / 100) * r.trades), 0
  );
  const overallWR  = totalTrades > 0 ? (totalWins / totalTrades) * 100 : null;

  // Avg stock return per trade — weighted by trade count across years
  const weightedAvgPct = totalTrades > 0
    ? validRows.reduce((s, r) => s + (r.avg_pct_return ?? 0) * r.trades, 0) / totalTrades
    : null;

  // Avg leveraged return per trade = avg_pct × leverage
  // This is the honest per-trade expectancy: what you make/lose on average per position
  const avgLevRet = weightedAvgPct != null ? weightedAvgPct * leverage : null;

  // SPY annual return averaged across active years (for context)
  const avgSpy = validRows.length > 0
    ? validRows.reduce((s, r) => s + (r.spy_return ?? 0), 0) / validRows.length
    : null;

  const selectCls = "bg-zinc-900 border border-zinc-700 text-zinc-100 text-sm rounded px-3 py-1.5 focus:outline-none focus:border-zinc-500";

  return (
    <div>
      {/* Controls */}
      <div className="flex flex-wrap gap-4 mb-6 items-center">
        <div className="flex items-center gap-2">
          <span className="text-xs text-zinc-400 whitespace-nowrap">Hold Period</span>
          <select className={selectCls} value={holdDays} onChange={e => setHoldDays(Number(e.target.value))}>
            {HOLD_OPTIONS.map(d => <option key={d} value={d}>{d} days</option>)}
          </select>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs text-zinc-400">Leverage</span>
          <select className={selectCls} value={leverage} onChange={e => setLeverage(Number(e.target.value))}>
            {LEV_OPTIONS.map(l => (
              <option key={l} value={l}>{l}x{l === 1 ? " (stock, no leverage)" : ""}</option>
            ))}
          </select>
        </div>
        <span className="text-xs text-zinc-600">
          $300 at risk per trade × {leverage}x = ${(300 * leverage).toLocaleString()} position size
        </span>
      </div>

      {/* Summary cards — only honest, per-trade metrics */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-6">
        <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 px-4 py-3">
          <div className="text-xs text-zinc-500 mb-1">Total Trades (2017–2025)</div>
          <div className="text-lg font-bold text-zinc-100">{totalTrades.toLocaleString() || "—"}</div>
        </div>
        <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 px-4 py-3">
          <div className="text-xs text-zinc-500 mb-1">Overall Win Rate</div>
          <div className={`text-lg font-bold ${overallWR != null && overallWR >= 50 ? "text-green-400" : "text-red-400"}`}>
            {overallWR != null ? `${overallWR.toFixed(1)}%` : "—"}
          </div>
        </div>
        <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 px-4 py-3">
          <div className="text-xs text-zinc-500 mb-1">Avg Stock Return / Trade</div>
          <div className={`text-lg font-bold ${weightedAvgPct != null && weightedAvgPct >= 0 ? "text-green-400" : "text-red-400"}`}>
            {weightedAvgPct != null ? fmt(weightedAvgPct) : "—"}
          </div>
          <div className="text-xs text-zinc-600 mt-0.5">raw signal quality</div>
        </div>
        <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 px-4 py-3">
          <div className="text-xs text-zinc-500 mb-1">Avg Leveraged Return / Trade</div>
          <div className={`text-lg font-bold ${avgLevRet != null && avgLevRet >= 0 ? "text-green-400" : "text-red-400"}`}>
            {avgLevRet != null ? fmt(avgLevRet) : "—"}
          </div>
          <div className="text-xs text-zinc-600 mt-0.5">avg stock ret × {leverage}x</div>
        </div>
      </div>

      {/* Year table */}
      {rows.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-zinc-800">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-zinc-800 text-zinc-400 text-left">
                <th className="px-4 py-3 font-medium">Year</th>
                <th className="px-4 py-3 font-medium">TT Firms</th>
                <th className="px-4 py-3 font-medium">Trades</th>
                <th className="px-4 py-3 font-medium">Win Rate</th>
                <th className="px-4 py-3 font-medium">
                  Avg Stock Ret
                  <span className="block text-xs font-normal text-zinc-600">per trade (raw)</span>
                </th>
                <th className="px-4 py-3 font-medium">
                  Avg Lev. Ret
                  <span className="block text-xs font-normal text-zinc-600">stock ret × {leverage}x</span>
                </th>
                <th className="px-4 py-3 font-medium">
                  SPY (annual)
                  <span className="block text-xs font-normal text-zinc-600">buy-and-hold</span>
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-800">
              {rows.map(r => {
                const levRet = r.avg_pct_return != null ? r.avg_pct_return * leverage : null;
                return (
                  <tr key={r.year} className="hover:bg-zinc-800/30 transition-colors">
                    <td className="px-4 py-3 font-semibold text-zinc-100">{r.year}</td>
                    <td className="px-4 py-3 text-zinc-400">{r.top_tier_firms || "—"}</td>
                    <td className="px-4 py-3 text-zinc-300">{r.trades || "—"}</td>
                    <td className={`px-4 py-3 font-medium ${
                      r.win_rate == null ? "text-zinc-500"
                      : r.win_rate >= 50  ? "text-green-400"
                      : "text-red-400"
                    }`}>
                      {r.win_rate != null ? `${r.win_rate.toFixed(1)}%` : "N/A"}
                    </td>
                    <td className={`px-4 py-3 ${
                      r.avg_pct_return == null ? "text-zinc-500"
                      : r.avg_pct_return >= 0  ? "text-green-400"
                      : "text-red-400"
                    }`}>
                      {fmt(r.avg_pct_return)}
                    </td>
                    <td className={`px-4 py-3 font-medium ${
                      levRet == null ? "text-zinc-500"
                      : levRet >= 0  ? "text-green-400"
                      : "text-red-400"
                    }`}>
                      {fmt(levRet)}
                    </td>
                    <td className="px-4 py-3 text-zinc-300">{fmt(r.spy_return)}</td>
                  </tr>
                );
              })}
            </tbody>
            {totalTrades > 0 && (
              <tfoot>
                <tr className="border-t-2 border-zinc-700 bg-zinc-800/40">
                  <td className="px-4 py-3 font-semibold text-zinc-100" colSpan={2}>Weighted Avg</td>
                  <td className="px-4 py-3 font-semibold text-zinc-100">{totalTrades.toLocaleString()}</td>
                  <td className="px-4 py-3 font-semibold text-zinc-100">
                    {overallWR != null ? `${overallWR.toFixed(1)}%` : "N/A"}
                  </td>
                  <td className={`px-4 py-3 font-semibold ${weightedAvgPct != null && weightedAvgPct >= 0 ? "text-green-400" : "text-red-400"}`}>
                    {fmt(weightedAvgPct)}
                  </td>
                  <td className={`px-4 py-3 font-semibold ${avgLevRet != null && avgLevRet >= 0 ? "text-green-400" : "text-red-400"}`}>
                    {fmt(avgLevRet)}
                  </td>
                  <td className="px-4 py-3 text-zinc-400">{fmt(avgSpy)}</td>
                </tr>
              </tfoot>
            )}
          </table>
        </div>
      )}

      <div className="mt-4 rounded border border-zinc-800 bg-zinc-900/40 px-4 py-3 text-xs text-zinc-500 space-y-1">
        <p><span className="text-zinc-400 font-medium">Avg Stock Ret / Trade</span> — the raw signal: when a Top Tier firm upgrades a stock, how much does it gain on average over the hold period? This is independent of leverage.</p>
        <p><span className="text-zinc-400 font-medium">Avg Lev. Ret / Trade</span> — avg stock return × leverage. If you apply {leverage}x (e.g. via options), this is your average return per position. Losses included.</p>
        <p><span className="text-zinc-400 font-medium">Why no "cumulative return"?</span> — With 1,300 trades/year at concurrent 30-day holds, total capital deployed far exceeds the $15k base. A cumulative % from trade stacking would be misleading. Per-trade expectancy is the honest metric.</p>
      </div>
    </div>
  );
}
