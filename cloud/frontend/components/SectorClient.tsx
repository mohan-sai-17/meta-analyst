"use client";
import { useState, useMemo } from "react";
import Link from "next/link";
import { ChevronUp, ChevronDown, ChevronsUpDown } from "lucide-react";

export interface Candidate {
  Ticker: string;
  Stock_Name: string;
  Sector: string;
  current_price: number;
  target_3m: number;
  target_6m: number;
  top_tier_firm_count: number;
  expected_return_3m_pct: number;
  expected_return_6m_pct: number;
  avg_win_rate_3m: number;
  avg_win_rate_6m: number;
  hist_vol: number;
}

type SortDir = "asc" | "desc" | null;

const COLS: { key: keyof Candidate; label: string }[] = [
  { key: "Ticker",               label: "Ticker"    },
  { key: "Stock_Name",           label: "Company"   },
  { key: "current_price",        label: "Price"     },
  { key: "target_3m",            label: "3M Target" },
  { key: "target_6m",            label: "6M Target" },
  { key: "expected_return_3m_pct", label: "Upside 3M" },
  { key: "expected_return_6m_pct", label: "Upside 6M" },
  { key: "avg_win_rate_3m",      label: "Win% 3M"   },
  { key: "avg_win_rate_6m",      label: "Win% 6M"   },
  { key: "top_tier_firm_count",  label: "Firms"     },
  { key: "hist_vol",             label: "Vol"       },
];

function SortIcon({ col, sortCol, sortDir }: { col: string; sortCol: string | null; sortDir: SortDir }) {
  if (col !== sortCol || !sortDir) return <ChevronsUpDown size={11} className="text-zinc-600" />;
  return sortDir === "asc"
    ? <ChevronUp size={11} className="text-zinc-300" />
    : <ChevronDown size={11} className="text-zinc-300" />;
}

export default function SectorClient({ candidates }: { candidates: Candidate[] }) {
  const sectors = useMemo(
    () => [...new Set(candidates.map(c => c.Sector))].sort(),
    [candidates]
  );

  const [activeSector, setActiveSector] = useState<string>(sectors[0] ?? "");
  const [sortCol, setSortCol]           = useState<string | null>(null);
  const [sortDir, setSortDir]           = useState<SortDir>(null);

  function handleSort(col: string) {
    if (sortCol !== col) {
      setSortCol(col);
      setSortDir("asc");
    } else if (sortDir === "asc") {
      setSortDir("desc");
    } else if (sortDir === "desc") {
      setSortCol(null);
      setSortDir(null);
    }
  }

  const rows = useMemo(() => {
    const base = candidates.filter(c => c.Sector === activeSector);
    if (!sortCol || !sortDir) return [...base].sort((a, b) => a.Ticker.localeCompare(b.Ticker));
    return [...base].sort((a, b) => {
      const av = a[sortCol as keyof Candidate];
      const bv = b[sortCol as keyof Candidate];
      if (typeof av === "number" && typeof bv === "number")
        return sortDir === "asc" ? av - bv : bv - av;
      return sortDir === "asc"
        ? String(av).localeCompare(String(bv))
        : String(bv).localeCompare(String(av));
    });
  }, [candidates, activeSector, sortCol, sortDir]);

  return (
    <div>
      {/* Page header */}
      <div className="flex items-baseline justify-between mb-5">
        <h1 className="text-xl font-bold text-zinc-900 dark:text-zinc-100">Sector Rotation</h1>
        <span className="text-sm text-zinc-500">{rows.length} stocks · {sectors.length} sectors</span>
      </div>

      {/* Sector pills */}
      <div className="flex flex-wrap gap-2 mb-6">
        {sectors.map(sector => (
          <button
            key={sector}
            onClick={() => { setActiveSector(sector); setSortCol(null); setSortDir(null); }}
            className={`px-3 py-1.5 rounded-full text-xs font-medium transition-colors whitespace-nowrap ${
              activeSector === sector
                ? "bg-zinc-900 dark:bg-zinc-100 text-white dark:text-zinc-900"
                : "bg-zinc-100 dark:bg-zinc-800 text-zinc-600 dark:text-zinc-400 hover:bg-zinc-200 dark:hover:bg-zinc-700"
            }`}
          >
            {sector}
            <span className={`ml-1.5 ${activeSector === sector ? "text-zinc-400 dark:text-zinc-500" : "text-zinc-400"}`}>
              {candidates.filter(c => c.Sector === sector).length}
            </span>
          </button>
        ))}
      </div>

      {/* Table */}
      {rows.length === 0 ? (
        <div className="text-sm text-zinc-500">No candidates in this sector.</div>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-zinc-200 dark:border-zinc-800">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-900/60">
                {COLS.map(({ key, label }) => (
                  <th
                    key={key}
                    onClick={() => handleSort(key)}
                    className="px-3 py-2.5 text-left font-medium text-zinc-500 cursor-pointer select-none hover:text-zinc-900 dark:hover:text-zinc-100 whitespace-nowrap transition-colors"
                  >
                    <span className="inline-flex items-center gap-1">
                      {label}
                      <SortIcon col={key} sortCol={sortCol} sortDir={sortDir} />
                    </span>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map(c => (
                <tr
                  key={c.Ticker}
                  className="border-b border-zinc-100 dark:border-zinc-800/50 hover:bg-zinc-50 dark:hover:bg-zinc-800/30 transition-colors"
                >
                  <td className="px-3 py-2 font-bold">
                    <Link href={`/oracle?ticker=${c.Ticker}`} className="text-zinc-900 dark:text-zinc-100 hover:text-green-500 dark:hover:text-green-400 transition-colors">
                      {c.Ticker}
                    </Link>
                  </td>
                  <td className="px-3 py-2 text-zinc-500 max-w-[180px] truncate">{c.Stock_Name}</td>
                  <td className="px-3 py-2 text-zinc-700 dark:text-zinc-300">${c.current_price?.toFixed(2)}</td>
                  <td className="px-3 py-2 text-zinc-700 dark:text-zinc-300">${c.target_3m?.toFixed(2)}</td>
                  <td className="px-3 py-2 text-zinc-700 dark:text-zinc-300">${c.target_6m?.toFixed(2)}</td>
                  <td className="px-3 py-2 text-green-500 dark:text-green-400 font-semibold">+{c.expected_return_3m_pct?.toFixed(1)}%</td>
                  <td className="px-3 py-2 text-green-500 dark:text-green-400 font-semibold">+{c.expected_return_6m_pct?.toFixed(1)}%</td>
                  <td className="px-3 py-2 text-zinc-600 dark:text-zinc-300">{c.avg_win_rate_3m?.toFixed(0)}%</td>
                  <td className="px-3 py-2 text-zinc-600 dark:text-zinc-300">{c.avg_win_rate_6m?.toFixed(0)}%</td>
                  <td className="px-3 py-2 text-zinc-500">{c.top_tier_firm_count}</td>
                  <td className="px-3 py-2 text-zinc-500">{(c.hist_vol * 100)?.toFixed(0)}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
