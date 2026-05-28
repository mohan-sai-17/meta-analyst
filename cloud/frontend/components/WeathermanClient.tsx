"use client";
import { useState, useCallback } from "react";

const API = process.env.NEXT_PUBLIC_API_URL!;

interface Ticker { ticker: string; stock_name: string }
interface SeasonRow {
  month: number;
  win_rate: number;
  avg_return: number;
  count: number;
}

const MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function monthBg(avgReturn: number): string {
  if (avgReturn >= 5)  return "bg-green-600/90 dark:bg-green-500/80";
  if (avgReturn >= 3)  return "bg-green-500/70 dark:bg-green-600/60";
  if (avgReturn >= 1)  return "bg-green-400/50 dark:bg-green-700/50";
  if (avgReturn > 0)   return "bg-green-300/30 dark:bg-green-800/40";
  if (avgReturn >= -1) return "bg-red-300/30 dark:bg-red-800/40";
  if (avgReturn >= -3) return "bg-red-400/50 dark:bg-red-700/50";
  if (avgReturn >= -5) return "bg-red-500/70 dark:bg-red-600/60";
  return "bg-red-600/90 dark:bg-red-500/80";
}

function monthTextColor(avgReturn: number): string {
  if (Math.abs(avgReturn) >= 3) return "text-white";
  return "text-zinc-900 dark:text-zinc-100";
}

export default function WeathermanClient({ tickers }: { tickers: Ticker[] }) {
  const [query, setQuery]       = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [data, setData]         = useState<SeasonRow[]>([]);
  const [loading, setLoading]   = useState(false);
  const [error, setError]       = useState<string | null>(null);

  const currentMonth = new Date().getMonth() + 1; // 1-12

  const filtered = query.length < 1 ? [] : tickers
    .filter(t =>
      t.ticker.toLowerCase().includes(query.toLowerCase()) ||
      t.stock_name.toLowerCase().includes(query.toLowerCase())
    )
    .slice(0, 8);

  const load = useCallback(async (ticker: string) => {
    setSelected(ticker);
    setQuery(ticker);
    setLoading(true);
    setError(null);
    setData([]);
    try {
      const r = await fetch(`${API}/seasonality/${encodeURIComponent(ticker)}`);
      if (!r.ok) throw new Error(`${r.status}`);
      const rows: SeasonRow[] = await r.json();
      setData(rows);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  // Summary stats
  const best  = data.length > 0 ? data.reduce((a, b) => b.avg_return > a.avg_return ? b : a) : null;
  const worst = data.length > 0 ? data.reduce((a, b) => b.avg_return < a.avg_return ? b : a) : null;
  const overallWinRate = data.length > 0
    ? data.reduce((sum, r) => sum + r.win_rate, 0) / data.length
    : null;

  return (
    <div>
      <div className="flex items-baseline justify-between mb-6">
        <h1 className="text-xl font-bold text-zinc-900 dark:text-zinc-100">Weatherman</h1>
        <span className="text-sm text-zinc-500">Seasonality · Monthly win rates</span>
      </div>

      {/* Ticker search */}
      <div className="relative w-72 mb-6">
        <input
          value={query}
          onChange={e => { setQuery(e.target.value); setSelected(null); setData([]); }}
          placeholder="Search ticker or name..."
          className="w-full bg-white dark:bg-zinc-900 border border-zinc-300 dark:border-zinc-700 rounded px-3 py-2 text-sm text-zinc-900 dark:text-zinc-100 placeholder:text-zinc-400 dark:placeholder:text-zinc-600 focus:outline-none focus:border-zinc-500"
        />
        {filtered.length > 0 && !selected && (
          <ul className="absolute z-50 w-full mt-1 bg-white dark:bg-zinc-900 border border-zinc-300 dark:border-zinc-700 rounded shadow-xl max-h-60 overflow-y-auto">
            {filtered.map(t => (
              <li
                key={t.ticker}
                onClick={() => load(t.ticker)}
                className="px-3 py-2 text-sm cursor-pointer hover:bg-zinc-100 dark:hover:bg-zinc-800 flex items-center gap-2"
              >
                <span className="text-zinc-900 dark:text-zinc-100 font-bold w-14 shrink-0">{t.ticker}</span>
                <span className="text-zinc-500 truncate">{t.stock_name}</span>
              </li>
            ))}
          </ul>
        )}
      </div>

      {error && (
        <div className="rounded border border-red-800 bg-red-950/40 px-4 py-3 text-sm text-red-400 mb-4">{error}</div>
      )}

      {loading && (
        <div className="text-sm text-zinc-500">Loading seasonality for {selected}...</div>
      )}

      {!selected && !loading && (
        <div className="text-sm text-zinc-500">Search for a ticker above to view its monthly seasonality.</div>
      )}

      {selected && !loading && data.length > 0 && (
        <div className="flex flex-col gap-6">
          {/* Summary row */}
          <div className="grid grid-cols-3 gap-3">
            <div className="bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 rounded-lg px-4 py-3">
              <div className="text-xs text-zinc-500 mb-1">Best Month</div>
              <div className="text-base font-bold text-zinc-900 dark:text-zinc-100">
                {best ? MONTH_NAMES[best.month - 1] : "-"}
              </div>
              {best && (
                <div className="text-xs text-green-500 dark:text-green-400">
                  +{best.avg_return.toFixed(1)}% avg · {best.win_rate.toFixed(0)}% win
                </div>
              )}
            </div>
            <div className="bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 rounded-lg px-4 py-3">
              <div className="text-xs text-zinc-500 mb-1">Worst Month</div>
              <div className="text-base font-bold text-zinc-900 dark:text-zinc-100">
                {worst ? MONTH_NAMES[worst.month - 1] : "-"}
              </div>
              {worst && (
                <div className="text-xs text-red-500 dark:text-red-400">
                  {worst.avg_return.toFixed(1)}% avg · {worst.win_rate.toFixed(0)}% win
                </div>
              )}
            </div>
            <div className="bg-white dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 rounded-lg px-4 py-3">
              <div className="text-xs text-zinc-500 mb-1">Overall Win Rate</div>
              <div className="text-base font-bold text-zinc-900 dark:text-zinc-100">
                {overallWinRate !== null ? `${overallWinRate.toFixed(1)}%` : "-"}
              </div>
              <div className="text-xs text-zinc-500">across all months</div>
            </div>
          </div>

          {/* 12-month grid */}
          <div className="grid grid-cols-3 sm:grid-cols-6 gap-2">
            {Array.from({ length: 12 }, (_, i) => i + 1).map(m => {
              const row = data.find(r => r.month === m);
              const isCurrent = m === currentMonth;
              const bg = row ? monthBg(row.avg_return) : "bg-zinc-100 dark:bg-zinc-800/50";
              const textColor = row ? monthTextColor(row.avg_return) : "text-zinc-500";

              return (
                <div
                  key={m}
                  className={`relative rounded-lg p-3 ${bg} ${
                    isCurrent
                      ? "ring-2 ring-offset-2 ring-zinc-900 dark:ring-zinc-100 ring-offset-white dark:ring-offset-zinc-950"
                      : ""
                  }`}
                >
                  <div className={`text-xs font-semibold mb-1 ${textColor}`}>
                    {MONTH_NAMES[m - 1]}
                    {isCurrent && (
                      <span className="ml-1 text-[10px] opacity-70">(now)</span>
                    )}
                  </div>
                  {row ? (
                    <>
                      <div className={`text-lg font-bold leading-tight ${textColor}`}>
                        {row.win_rate.toFixed(0)}%
                      </div>
                      <div className={`text-[11px] mt-0.5 ${textColor} opacity-80`}>
                        {row.avg_return >= 0 ? "+" : ""}{row.avg_return.toFixed(1)}% avg
                      </div>
                      <div className={`text-[10px] mt-0.5 ${textColor} opacity-60`}>
                        n={row.count}
                      </div>
                    </>
                  ) : (
                    <div className="text-xs text-zinc-400">No data</div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}

      {selected && !loading && data.length === 0 && !error && (
        <div className="text-sm text-zinc-500">No seasonality data found for {selected}.</div>
      )}
    </div>
  );
}
