"use client";
import { useState } from "react";

interface PriceTarget {
  Ticker: string;
  current_price: number;
  target_price_3m: number;
  target_price_6m: number;
  expected_return_3m_pct: number;
}
interface TopPick {
  ticker: string;
  price: number;
  target3m: number;
  upside: number;
  strike: number;
  expiry: string;
  dte: number;
  ask: number;
  fair_value: number;
  roi: number;
}

const API = process.env.NEXT_PUBLIC_API_URL ?? "";

export default function TopPicksPage() {
  const [results, setResults]   = useState<TopPick[]>([]);
  const [scanning, setScanning] = useState(false);
  const [minRoi, setMinRoi]     = useState(50);
  const [error, setError]       = useState<string | null>(null);
  const [scanned, setScanned]   = useState(false);

  async function runScan() {
    setScanning(true);
    setResults([]);
    setError(null);
    try {
      const r = await fetch(`${API}/scan-options`);
      if (!r.ok) { setError(`HTTP ${r.status}`); return; }
      const picks: TopPick[] = await r.json();
      setResults(picks);
      setScanned(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setScanning(false);
    }
  }

  const filtered = results.filter(r => r.roi >= minRoi);

  return (
    <div>
      <div className="flex items-baseline justify-between mb-6">
        <h1 className="text-xl font-bold text-zinc-100">Top Picks — Calls</h1>
        <span className="text-sm text-zinc-500">Oracle-filtered · underpriced calls only</span>
      </div>

      <div className="flex items-center gap-4 mb-6">
        <button
          type="button"
          onClick={runScan}
          disabled={scanning}
          className="px-4 py-2 rounded bg-green-700 hover:bg-green-600 disabled:bg-zinc-700 disabled:text-zinc-500 text-sm font-semibold transition-colors"
        >
          {scanning ? "Scanning..." : "Scan for Top Picks"}
        </button>
        <div className="flex items-center gap-2 text-sm text-zinc-400">
          <span>Min ROI</span>
          <input
            type="number"
            value={minRoi}
            onChange={e => setMinRoi(Number(e.target.value))}
            className="w-16 bg-zinc-900 border border-zinc-700 rounded px-2 py-1 text-zinc-100 text-sm"
          />
          <span>%</span>
        </div>
      </div>

      {scanning && (
        <div className="mb-6 text-sm text-zinc-400">
          Scanning all tickers server-side — this takes ~30–60s...
        </div>
      )}

      {!scanning && results.length > 0 && filtered.length === 0 && (
        <div className="text-sm text-zinc-500">
          Scan complete — {results.length} pick{results.length !== 1 ? "s" : ""} found, but none meet the {minRoi}% ROI threshold. Lower the slider to see them.
        </div>
      )}

      {filtered.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-zinc-800">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-zinc-800 text-zinc-500">
                {["Ticker", "Price", "3M Target", "Upside", "Strike", "Expiry", "DTE", "Ask", "Fair Value", "ROI"].map(h => (
                  <th key={h} className="px-3 py-2 text-left font-medium">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {filtered.map((r, i) => (
                <tr key={i} className="border-b border-zinc-100 dark:border-zinc-800/50 hover:bg-zinc-50 dark:hover:bg-zinc-800/30 transition-colors">
                  <td className="px-3 py-2 font-bold text-zinc-900 dark:text-zinc-100">{r.ticker}</td>
                  <td className="px-3 py-2 text-zinc-700 dark:text-zinc-300">${r.price?.toFixed(2)}</td>
                  <td className="px-3 py-2 text-zinc-700 dark:text-zinc-300">${r.target3m?.toFixed(2)}</td>
                  <td className="px-3 py-2 text-green-500 dark:text-green-400 font-semibold">+{r.upside?.toFixed(1)}%</td>
                  <td className="px-3 py-2 text-zinc-700 dark:text-zinc-300">${r.strike}</td>
                  <td className="px-3 py-2 text-zinc-500 dark:text-zinc-400">{r.expiry}</td>
                  <td className="px-3 py-2 text-zinc-500">{r.dte}d</td>
                  <td className="px-3 py-2 text-zinc-700 dark:text-zinc-300">${r.ask?.toFixed(2)}</td>
                  <td className="px-3 py-2 text-zinc-500 dark:text-zinc-400">${r.fair_value?.toFixed(4)}</td>
                  <td className={`px-3 py-2 font-bold ${r.roi >= 300 ? "text-green-400" : r.roi >= 100 ? "text-green-500" : "text-zinc-700 dark:text-zinc-300"}`}>
                    {r.roi?.toFixed(0)}%
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {error && (
        <div className="text-sm text-red-400 bg-red-950/30 border border-red-800 rounded px-3 py-2">
          Error: {error}
        </div>
      )}

      {!scanning && !scanned && !error && (
        <div className="text-sm text-zinc-500">Click scan to find underpriced calls across all Oracle-qualified tickers. Results load in ~30–60s.</div>
      )}

      {!scanning && scanned && results.length === 0 && (
        <div className="text-sm text-zinc-500">
          Scan complete — no underpriced calls found today. Markets may be fairly priced or options are expensive.
        </div>
      )}
    </div>
  );
}
