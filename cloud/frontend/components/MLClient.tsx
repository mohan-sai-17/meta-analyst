"use client";

import { useMemo, useState } from "react";

interface MLSummaryRow {
  year: number;
  n_all_signals: number;
  n_filtered_signals: number;
  all_signals_return_pct: number;
  filtered_return_pct: number;
  precision: number;
  recall: number;
  feature_1: string;
  feature_1_importance: number;
  feature_2: string;
  feature_2_importance: number;
  feature_3: string;
  feature_3_importance: number;
  spy_return_pct: number | null;
}

interface MLLiveRow {
  ticker: string;
  signal_date: string;
  firm: string;
  sector: string;
  confidence_pct: number;
  predicted_hit: number;
  firm_score: number;
  stock_momentum_5d: number;
  stock_momentum_20d: number;
  market_momentum_20d: number;
}

interface Props {
  summary: MLSummaryRow[];
  live: MLLiveRow[];
}

const FEATURE_LABELS: Record<string, string> = {
  month               : "Month",
  firm_score          : "Firm Score",
  stock_momentum_5d   : "Stock Mom 5d",
  stock_momentum_20d  : "Stock Mom 20d",
  market_momentum_20d : "Market Mom 20d",
  sector_code         : "Sector",
};

function StatCard({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="rounded border border-zinc-800 bg-zinc-900 px-4 py-3">
      <div className="text-xs text-zinc-500 mb-1">{label}</div>
      <div className="text-lg font-bold text-zinc-100">{value}</div>
      {sub && <div className="text-xs text-zinc-500 mt-0.5">{sub}</div>}
    </div>
  );
}

function Bar({ value, max, color }: { value: number; max: number; color: string }) {
  return (
    <div className="flex-1 h-3 bg-zinc-800 rounded overflow-hidden">
      <div
        className={`h-3 rounded ${color}`}
        style={{ width: `${Math.min(Math.abs(value) / max * 100, 100)}%` }}
      />
    </div>
  );
}

export default function MLClient({ summary, live }: Props) {
  const [activeTab, setActiveTab] = useState<"performance" | "features" | "live">("performance");

  const rows = useMemo(() => [...summary].sort((a, b) => a.year - b.year), [summary]);

  const stats = useMemo(() => {
    if (!rows.length) return null;
    const allAvg  = rows.reduce((s, r) => s + r.all_signals_return_pct, 0) / rows.length;
    const filtAvg = rows.reduce((s, r) => s + r.filtered_return_pct, 0)    / rows.length;
    const filtBetter = rows.filter(r => r.filtered_return_pct > r.all_signals_return_pct).length;
    const avgPrec = rows.reduce((s, r) => s + r.precision, 0) / rows.length;
    const avgRecall = rows.reduce((s, r) => s + r.recall, 0) / rows.length;
    const filterRate = rows.reduce((s, r) =>
      s + (r.n_all_signals > 0 ? r.n_filtered_signals / r.n_all_signals : 0), 0
    ) / rows.length;
    return { allAvg, filtAvg, filtBetter, n: rows.length, avgPrec, avgRecall, filterRate };
  }, [rows]);

  // Aggregate feature importances across all years
  const featureImportances = useMemo(() => {
    const acc: Record<string, number[]> = {};
    for (const r of rows) {
      for (const [f, imp] of [
        [r.feature_1, r.feature_1_importance],
        [r.feature_2, r.feature_2_importance],
        [r.feature_3, r.feature_3_importance],
      ] as [string, number][]) {
        if (f) {
          if (!acc[f]) acc[f] = [];
          acc[f].push(imp);
        }
      }
    }
    return Object.entries(acc)
      .map(([feat, vals]) => ({
        feat,
        label: FEATURE_LABELS[feat] ?? feat,
        avg  : vals.reduce((s, v) => s + v, 0) / vals.length,
      }))
      .sort((a, b) => b.avg - a.avg);
  }, [rows]);

  const maxRet = useMemo(() => {
    let m = 5;
    for (const r of rows) {
      m = Math.max(m, Math.abs(r.all_signals_return_pct), Math.abs(r.filtered_return_pct),
                   Math.abs(r.spy_return_pct ?? 0));
    }
    return m;
  }, [rows]);

  if (!stats) return null;

  const tabs = [
    { id: "performance" as const, label: "Performance" },
    { id: "features"    as const, label: "Feature Importances" },
    { id: "live"        as const, label: `Live Signals${live.length ? ` (${live.length})` : ""}` },
  ];

  return (
    <div className="space-y-6">

      {/* Summary cards */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <StatCard
          label="Filtered Avg Return"
          value={`${stats.filtAvg >= 0 ? "+" : ""}${stats.filtAvg.toFixed(2)}%`}
          sub="per signal, avg over test years"
        />
        <StatCard
          label="All Signals Avg Return"
          value={`${stats.allAvg >= 0 ? "+" : ""}${stats.allAvg.toFixed(2)}%`}
          sub="unfiltered baseline"
        />
        <StatCard
          label="Filter Outperforms"
          value={`${stats.filtBetter} / ${stats.n} years`}
          sub={`avg precision ${(stats.avgPrec * 100).toFixed(0)}%  recall ${(stats.avgRecall * 100).toFixed(0)}%`}
        />
        <StatCard
          label="Avg Signals Taken"
          value={`${(stats.filterRate * 100).toFixed(0)}%`}
          sub="of all signals pass threshold"
        />
      </div>

      {/* Tabs */}
      <div className="flex gap-1 border-b border-zinc-800">
        {tabs.map(t => (
          <button
            key={t.id}
            onClick={() => setActiveTab(t.id)}
            className={`px-4 py-2 text-sm rounded-t transition-colors ${
              activeTab === t.id
                ? "bg-zinc-800 text-zinc-100"
                : "text-zinc-500 hover:text-zinc-300"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* Performance tab */}
      {activeTab === "performance" && (
        <div className="space-y-4">
          <p className="text-xs text-zinc-500">
            ML-filtered signals (≥55% confidence) vs taking all signals vs SPY — per-signal average return each year.
          </p>
          {rows.map((r) => (
            <div key={r.year}>
              <div className="flex items-center justify-between mb-1">
                <span className="text-sm font-medium text-zinc-300">{r.year}</span>
                <span className="text-xs text-zinc-500">
                  {r.n_filtered_signals}/{r.n_all_signals} signals taken
                  &nbsp;·&nbsp;P={( r.precision * 100).toFixed(0)}%
                  &nbsp;R={( r.recall    * 100).toFixed(0)}%
                </span>
              </div>
              <div className="space-y-1">
                {[
                  { label: "Filter", val: r.filtered_return_pct,  cls: "bg-violet-500" },
                  { label: "All",    val: r.all_signals_return_pct, cls: r.all_signals_return_pct >= 0 ? "bg-emerald-500" : "bg-red-500" },
                  { label: "SPY",    val: r.spy_return_pct ?? 0,   cls: "bg-zinc-500" },
                ].map(({ label, val, cls }) => (
                  <div key={label} className="flex items-center gap-2">
                    <span className="text-xs text-zinc-500 w-10 shrink-0">{label}</span>
                    <Bar value={val} max={maxRet} color={cls} />
                    <span className={`text-xs w-14 text-right ${val >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                      {val >= 0 ? "+" : ""}{val.toFixed(2)}%
                    </span>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Feature importances tab */}
      {activeTab === "features" && (
        <div className="space-y-6">
          <div>
            <h2 className="text-sm font-semibold text-zinc-300 mb-3">
              Average Feature Importance (across all test years)
            </h2>
            <div className="space-y-2">
              {featureImportances.map(({ feat, label, avg }) => (
                <div key={feat} className="flex items-center gap-3">
                  <span className="text-xs text-zinc-400 w-36 shrink-0">{label}</span>
                  <div className="flex-1 h-4 bg-zinc-800 rounded overflow-hidden">
                    <div
                      className="h-4 rounded bg-violet-500"
                      style={{ width: `${Math.min(avg / (featureImportances[0]?.avg ?? 1) * 100, 100)}%` }}
                    />
                  </div>
                  <span className="text-xs text-zinc-400 w-12 text-right font-mono">
                    {(avg * 100).toFixed(1)}%
                  </span>
                </div>
              ))}
            </div>
          </div>

          {/* Year-by-year top feature */}
          <div>
            <h2 className="text-sm font-semibold text-zinc-300 mb-3">Top Feature by Year</h2>
            <div className="overflow-x-auto">
              <table className="w-full text-xs text-left">
                <thead>
                  <tr className="text-zinc-500 border-b border-zinc-800">
                    <th className="py-2 pr-4">Year</th>
                    <th className="py-2 pr-4">#1 Feature</th>
                    <th className="py-2 pr-4">Imp.</th>
                    <th className="py-2 pr-4">#2 Feature</th>
                    <th className="py-2 pr-4">Imp.</th>
                    <th className="py-2 pr-4">#3 Feature</th>
                    <th className="py-2 pr-4">Imp.</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.year} className="border-b border-zinc-800/50 hover:bg-zinc-800/30">
                      <td className="py-2 pr-4 font-medium text-zinc-200">{r.year}</td>
                      <td className="py-2 pr-4 text-violet-300">{FEATURE_LABELS[r.feature_1] ?? r.feature_1}</td>
                      <td className="py-2 pr-4 font-mono text-zinc-400">{(r.feature_1_importance * 100).toFixed(1)}%</td>
                      <td className="py-2 pr-4 text-violet-300/70">{FEATURE_LABELS[r.feature_2] ?? r.feature_2}</td>
                      <td className="py-2 pr-4 font-mono text-zinc-400">{(r.feature_2_importance * 100).toFixed(1)}%</td>
                      <td className="py-2 pr-4 text-violet-300/50">{FEATURE_LABELS[r.feature_3] ?? r.feature_3}</td>
                      <td className="py-2 pr-4 font-mono text-zinc-400">{(r.feature_3_importance * 100).toFixed(1)}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {/* Live signals tab */}
      {activeTab === "live" && (
        <div>
          {live.length === 0 ? (
            <p className="text-sm text-zinc-500 py-6 text-center">
              No live signals found in the last 90 days.
            </p>
          ) : (
            <>
              <p className="text-xs text-zinc-500 mb-4">
                Analyst upgrades from the last 90 days scored by the model trained on all
                historical data. Sorted by confidence. ≥55% = model predicts +5% hit.
              </p>
              <div className="overflow-x-auto">
                <table className="w-full text-xs text-left">
                  <thead>
                    <tr className="text-zinc-500 border-b border-zinc-800">
                      <th className="py-2 pr-4">Ticker</th>
                      <th className="py-2 pr-4">Confidence</th>
                      <th className="py-2 pr-4">Signal Date</th>
                      <th className="py-2 pr-4">Firm</th>
                      <th className="py-2 pr-4">Sector</th>
                      <th className="py-2 pr-4">Firm Score</th>
                      <th className="py-2 pr-4">Mom 5d</th>
                      <th className="py-2 pr-4">Mom 20d</th>
                      <th className="py-2 pr-4">Mkt Mom</th>
                    </tr>
                  </thead>
                  <tbody>
                    {live.map((r, i) => {
                      const highConf = r.confidence_pct >= 55;
                      return (
                        <tr
                          key={i}
                          className={`border-b border-zinc-800/50 hover:bg-zinc-800/30 ${
                            highConf ? "" : "opacity-50"
                          }`}
                        >
                          <td className="py-2 pr-4 font-bold text-zinc-100">{r.ticker}</td>
                          <td className="py-2 pr-4">
                            <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-bold ${
                              r.confidence_pct >= 65 ? "bg-violet-500/20 text-violet-300" :
                              r.confidence_pct >= 55 ? "bg-emerald-500/20 text-emerald-300" :
                              "bg-zinc-700/40 text-zinc-400"
                            }`}>
                              {r.confidence_pct.toFixed(0)}%
                              {highConf && " ✓"}
                            </span>
                          </td>
                          <td className="py-2 pr-4 text-zinc-400 font-mono">{r.signal_date}</td>
                          <td className="py-2 pr-4 text-zinc-300">{r.firm}</td>
                          <td className="py-2 pr-4 text-zinc-400">{r.sector}</td>
                          <td className="py-2 pr-4 font-mono text-zinc-400">
                            {r.firm_score >= 0 ? "+" : ""}{r.firm_score.toFixed(1)}%
                          </td>
                          <td className={`py-2 pr-4 font-mono ${r.stock_momentum_5d >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                            {r.stock_momentum_5d >= 0 ? "+" : ""}{r.stock_momentum_5d.toFixed(1)}%
                          </td>
                          <td className={`py-2 pr-4 font-mono ${r.stock_momentum_20d >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                            {r.stock_momentum_20d >= 0 ? "+" : ""}{r.stock_momentum_20d.toFixed(1)}%
                          </td>
                          <td className={`py-2 pr-4 font-mono ${r.market_momentum_20d >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                            {r.market_momentum_20d >= 0 ? "+" : ""}{r.market_momentum_20d.toFixed(1)}%
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </div>
      )}

      {/* Full summary table */}
      {activeTab === "performance" && (
        <div>
          <h2 className="text-sm font-semibold text-zinc-300 mb-3">Full Year Statistics</h2>
          <div className="overflow-x-auto">
            <table className="w-full text-xs text-left">
              <thead>
                <tr className="text-zinc-500 border-b border-zinc-800">
                  <th className="py-2 pr-4">Year</th>
                  <th className="py-2 pr-4">All Signals</th>
                  <th className="py-2 pr-4">Filtered</th>
                  <th className="py-2 pr-4 text-violet-400">Filtered Ret</th>
                  <th className="py-2 pr-4">All Ret</th>
                  <th className="py-2 pr-4">SPY</th>
                  <th className="py-2 pr-4">Precision</th>
                  <th className="py-2 pr-4">Recall</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => {
                  const filtWins = r.filtered_return_pct > r.all_signals_return_pct;
                  return (
                    <tr key={r.year} className="border-b border-zinc-800/50 hover:bg-zinc-800/30">
                      <td className="py-2 pr-4 font-medium text-zinc-200">{r.year}</td>
                      <td className="py-2 pr-4 text-zinc-400">{r.n_all_signals}</td>
                      <td className="py-2 pr-4 text-zinc-400">
                        {r.n_filtered_signals}
                        <span className="text-zinc-600 ml-1">
                          ({r.n_all_signals > 0
                            ? Math.round(r.n_filtered_signals / r.n_all_signals * 100)
                            : 0}%)
                        </span>
                      </td>
                      <td className={`py-2 pr-4 font-semibold ${filtWins ? "text-violet-400" : "text-zinc-300"}`}>
                        {r.filtered_return_pct >= 0 ? "+" : ""}{r.filtered_return_pct.toFixed(2)}%
                      </td>
                      <td className={`py-2 pr-4 ${r.all_signals_return_pct >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                        {r.all_signals_return_pct >= 0 ? "+" : ""}{r.all_signals_return_pct.toFixed(2)}%
                      </td>
                      <td className="py-2 pr-4 text-zinc-400">
                        {r.spy_return_pct != null
                          ? `${r.spy_return_pct >= 0 ? "+" : ""}${r.spy_return_pct.toFixed(1)}%`
                          : "—"}
                      </td>
                      <td className="py-2 pr-4 text-zinc-400">{(r.precision * 100).toFixed(0)}%</td>
                      <td className="py-2 pr-4 text-zinc-400">{(r.recall    * 100).toFixed(0)}%</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

    </div>
  );
}
