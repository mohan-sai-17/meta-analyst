"use client";
import { useState, useMemo } from "react";
import Link from "next/link";

export interface Signal {
  Ticker: string;
  Stock_Name: string;
  Firm: string;
  GradeDate: string;
  Action: string;
  ToGrade: string;
  current_price: number;
  avg_return_3m: number;
  avg_return_6m: number;
  win_rate_6m: number;
  est_target_3m: number;
}

const ACTION_LABEL: Record<string, string> = {
  up: "Upgrade", init: "Initiate", down: "Downgrade", main: "Maintain", reit: "Reiterate",
};

function Ret({ v }: { v: number }) {
  return (
    <span className={`font-semibold ${v >= 0 ? "text-green-500 dark:text-green-400" : "text-red-500 dark:text-red-400"}`}>
      {v >= 0 ? "+" : ""}{v?.toFixed(1)}%
    </span>
  );
}

export default function ScannerClient({ signals }: { signals: Signal[] }) {
  const [view, setView]           = useState<"stock" | "firm">("stock");
  const [stockSearch, setStockSearch] = useState("");
  const [firmSearch, setFirmSearch]   = useState("");
  const [selectedFirm, setSelectedFirm] = useState<string | null>(null);

  // Unique sorted firms
  const firms = useMemo(() =>
    [...new Set(signals.map(s => s.Firm))].sort(),
  [signals]);

  // Stock view: filter by ticker or company name
  const stockRows = useMemo(() => {
    const q = stockSearch.toLowerCase();
    return q
      ? signals.filter(s =>
          s.Ticker.toLowerCase().includes(q) ||
          s.Stock_Name.toLowerCase().includes(q)
        )
      : signals;
  }, [signals, stockSearch]);

  // Firm view: filter firm list by search
  const filteredFirms = useMemo(() => {
    const q = firmSearch.toLowerCase();
    return q ? firms.filter(f => f.toLowerCase().includes(q)) : firms;
  }, [firms, firmSearch]);

  // Firm view: signals for selected firm + firm stats
  const firmSignals = useMemo(() =>
    selectedFirm ? signals.filter(s => s.Firm === selectedFirm) : [],
  [signals, selectedFirm]);

  const firmStats = useMemo(() => {
    if (!firmSignals.length) return null;
    const calls     = firmSignals.length;
    const avgRet3m  = firmSignals.reduce((a, s) => a + (s.avg_return_3m ?? 0), 0) / calls;
    const avgRet6m  = firmSignals.reduce((a, s) => a + (s.avg_return_6m ?? 0), 0) / calls;
    const winRate6m = firmSignals.reduce((a, s) => a + (s.win_rate_6m  ?? 0), 0) / calls;
    return { calls, avgRet3m, avgRet6m, winRate6m };
  }, [firmSignals]);

  const SignalTable = ({ rows }: { rows: Signal[] }) => (
    <div className="overflow-x-auto rounded-lg border border-zinc-200 dark:border-zinc-800">
      <table className="w-full text-xs">
        <thead>
          <tr className="border-b border-zinc-200 dark:border-zinc-800 text-zinc-500">
            {["Date","Ticker","Company","Firm","Action","Grade","Price","3M Target","3M Ret","6M Ret","Win%"].map(h => (
              <th key={h} className="px-3 py-2 text-left font-medium whitespace-nowrap">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((s, i) => (
            <tr key={i} className="border-b border-zinc-100 dark:border-zinc-800/50 hover:bg-zinc-50 dark:hover:bg-zinc-800/30 transition-colors">
              <td className="px-3 py-2 text-zinc-400">{String(s.GradeDate).slice(0,10)}</td>
              <td className="px-3 py-2 font-bold">
                <Link href={`/oracle?ticker=${s.Ticker}`} className="text-zinc-900 dark:text-zinc-100 hover:text-green-500 dark:hover:text-green-400 transition-colors">
                  {s.Ticker}
                </Link>
              </td>
              <td className="px-3 py-2 text-zinc-500 max-w-[160px] truncate">{s.Stock_Name}</td>
              <td className="px-3 py-2 text-zinc-500 max-w-[140px] truncate">{s.Firm}</td>
              <td className="px-3 py-2 text-zinc-600 dark:text-zinc-300">{ACTION_LABEL[s.Action] ?? s.Action}</td>
              <td className="px-3 py-2 text-green-500 dark:text-green-400 font-semibold">{s.ToGrade}</td>
              <td className="px-3 py-2 text-zinc-700 dark:text-zinc-300">${s.current_price?.toFixed(2)}</td>
              <td className="px-3 py-2 text-zinc-700 dark:text-zinc-300">${s.est_target_3m?.toFixed(2)}</td>
              <td className="px-3 py-2"><Ret v={s.avg_return_3m} /></td>
              <td className="px-3 py-2"><Ret v={s.avg_return_6m} /></td>
              <td className="px-3 py-2 text-zinc-600 dark:text-zinc-300">{s.win_rate_6m?.toFixed(0)}%</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );

  return (
    <div>
      {/* Header + view toggle */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-xl font-bold text-zinc-900 dark:text-zinc-100">Scanner</h1>
          <p className="text-sm text-zinc-500 mt-0.5">Top Tier ratings · last 90 days · {signals.length} rows</p>
        </div>
        <div className="flex rounded-lg border border-zinc-200 dark:border-zinc-700 overflow-hidden text-sm">
          <button
            onClick={() => setView("stock")}
            className={`px-4 py-1.5 transition-colors ${view === "stock" ? "bg-zinc-900 dark:bg-zinc-100 text-white dark:text-zinc-900 font-semibold" : "text-zinc-500 hover:text-zinc-900 dark:hover:text-zinc-100"}`}
          >
            By Stock
          </button>
          <button
            onClick={() => setView("firm")}
            className={`px-4 py-1.5 transition-colors ${view === "firm" ? "bg-zinc-900 dark:bg-zinc-100 text-white dark:text-zinc-900 font-semibold" : "text-zinc-500 hover:text-zinc-900 dark:hover:text-zinc-100"}`}
          >
            By Firm
          </button>
        </div>
      </div>

      {/* ── BY STOCK VIEW ────────────────────────────────────────── */}
      {view === "stock" && (
        <div className="flex flex-col gap-4">
          <input
            value={stockSearch}
            onChange={e => setStockSearch(e.target.value)}
            placeholder="Search ticker or company name..."
            className="w-80 bg-zinc-50 dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-700 rounded px-3 py-2 text-sm text-zinc-900 dark:text-zinc-100 placeholder:text-zinc-400 focus:outline-none focus:border-zinc-400 dark:focus:border-zinc-500"
          />
          <SignalTable rows={stockRows} />
        </div>
      )}

      {/* ── BY FIRM VIEW ─────────────────────────────────────────── */}
      {view === "firm" && (
        <div className="flex gap-4">
          {/* Firm sidebar */}
          <div className="w-64 shrink-0 flex flex-col gap-2">
            <input
              value={firmSearch}
              onChange={e => setFirmSearch(e.target.value)}
              placeholder="Search firm..."
              className="w-full bg-zinc-50 dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-700 rounded px-3 py-2 text-sm text-zinc-900 dark:text-zinc-100 placeholder:text-zinc-400 focus:outline-none focus:border-zinc-400 dark:focus:border-zinc-500"
            />
            <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 overflow-hidden max-h-[70vh] overflow-y-auto">
              {filteredFirms.map(firm => {
                const count   = signals.filter(s => s.Firm === firm).length;
                const avgWin  = signals.filter(s => s.Firm === firm)
                  .reduce((a, s) => a + (s.win_rate_6m ?? 0), 0) / count;
                return (
                  <button
                    key={firm}
                    onClick={() => setSelectedFirm(firm)}
                    className={`w-full text-left px-3 py-2.5 border-b border-zinc-100 dark:border-zinc-800/50 last:border-0 transition-colors ${
                      selectedFirm === firm
                        ? "bg-zinc-900 dark:bg-zinc-100 text-white dark:text-zinc-900"
                        : "hover:bg-zinc-50 dark:hover:bg-zinc-800/40 text-zinc-700 dark:text-zinc-300"
                    }`}
                  >
                    <div className="text-xs font-semibold truncate">{firm}</div>
                    <div className={`text-[10px] mt-0.5 ${selectedFirm === firm ? "text-zinc-300 dark:text-zinc-600" : "text-zinc-400"}`}>
                      {count} call{count !== 1 ? "s" : ""} · {avgWin.toFixed(0)}% win 6M
                    </div>
                  </button>
                );
              })}
            </div>
          </div>

          {/* Firm detail panel */}
          <div className="flex-1 min-w-0 flex flex-col gap-4">
            {selectedFirm && firmStats ? (
              <>
                {/* Stats banner */}
                <div className="grid grid-cols-4 gap-3">
                  {[
                    { label: "Calls (90d)",   value: String(firmStats.calls) },
                    { label: "Avg Ret 3M",    value: `${firmStats.avgRet3m >= 0 ? "+" : ""}${firmStats.avgRet3m.toFixed(1)}%`, green: firmStats.avgRet3m >= 0 },
                    { label: "Avg Ret 6M",    value: `${firmStats.avgRet6m >= 0 ? "+" : ""}${firmStats.avgRet6m.toFixed(1)}%`, green: firmStats.avgRet6m >= 0 },
                    { label: "Win Rate 6M",   value: `${firmStats.winRate6m.toFixed(0)}%` },
                  ].map(({ label, value, green }) => (
                    <div key={label} className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-900 px-3 py-2.5">
                      <div className="text-[10px] text-zinc-500 mb-1">{label}</div>
                      <div className={`text-base font-bold ${green === false ? "text-red-500 dark:text-red-400" : green ? "text-green-500 dark:text-green-400" : "text-zinc-900 dark:text-zinc-100"}`}>
                        {value}
                      </div>
                    </div>
                  ))}
                </div>
                <div className="text-sm font-semibold text-zinc-700 dark:text-zinc-300">{selectedFirm}</div>
                <SignalTable rows={firmSignals} />
              </>
            ) : (
              <div className="flex items-center justify-center h-48 text-sm text-zinc-400">
                Select a firm from the list to see their calls
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
