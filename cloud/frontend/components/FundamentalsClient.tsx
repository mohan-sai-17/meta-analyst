"use client";
import { useState, useMemo } from "react";
import Link from "next/link";

export interface FundRow {
  ticker: string;
  as_of_date: string;
  debt_equity: number | null;
  current_ratio: number | null;
  interest_coverage: number | null;
  fcf_positive_years: number | null;
  passes_health_screen: boolean | null;
}

type SortKey = keyof FundRow;
type Filter  = "all" | "pass" | "fail" | "unknown";

function GateChip({ v }: { v: boolean | null }) {
  if (v === true)  return <span className="text-[10px] font-bold bg-green-900 text-green-300 px-1.5 py-0.5 rounded">PASS</span>;
  if (v === false) return <span className="text-[10px] font-bold bg-red-900   text-red-300   px-1.5 py-0.5 rounded">FAIL</span>;
  return                  <span className="text-[10px] font-bold bg-zinc-800  text-zinc-500  px-1.5 py-0.5 rounded">-</span>;
}

function Metric({
  v, low, high, fmt = (n: number) => n.toFixed(2), invert = false,
}: {
  v: number | null;
  low: number;
  high: number;
  fmt?: (n: number) => string;
  invert?: boolean;   // true = lower is better (e.g. debt/equity)
}) {
  if (v == null) return <span className="text-zinc-600">-</span>;
  const passes = invert ? v <= high : v >= low;
  const warns  = invert ? v > low && v <= high : v >= low && v < high;
  const color  = !passes ? "text-red-400" : warns ? "text-amber-400" : "text-green-400";
  return <span className={`font-semibold ${color}`}>{fmt(v)}</span>;
}

const HEADERS: { label: string; key: SortKey; align?: string }[] = [
  { label: "Ticker",        key: "ticker"              },
  { label: "Health Gate",   key: "passes_health_screen"},
  { label: "Debt / Equity", key: "debt_equity"         },
  { label: "Current Ratio", key: "current_ratio"       },
  { label: "Interest Cov.", key: "interest_coverage"   },
  { label: "FCF Positive",  key: "fcf_positive_years"  },
  { label: "Data Date",     key: "as_of_date"          },
];

export default function FundamentalsClient({ rows }: { rows: FundRow[] }) {
  const [filter,    setFilter]    = useState<Filter>("all");
  const [search,    setSearch]    = useState("");
  const [sortKey,   setSortKey]   = useState<SortKey>("passes_health_screen");
  const [sortAsc,   setSortAsc]   = useState(true);

  const filtered = useMemo(() => {
    let r = rows;
    if (search) {
      const q = search.toLowerCase();
      r = r.filter(row => row.ticker.toLowerCase().includes(q));
    }
    if (filter === "pass")    r = r.filter(row => row.passes_health_screen === true);
    if (filter === "fail")    r = r.filter(row => row.passes_health_screen === false);
    if (filter === "unknown") r = r.filter(row => row.passes_health_screen == null);
    return [...r].sort((a, b) => {
      const av = a[sortKey], bv = b[sortKey];
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      const cmp = av < bv ? -1 : av > bv ? 1 : 0;
      return sortAsc ? cmp : -cmp;
    });
  }, [rows, filter, search, sortKey, sortAsc]);

  function toggleSort(key: SortKey) {
    if (sortKey === key) setSortAsc(a => !a);
    else { setSortKey(key); setSortAsc(true); }
  }

  const passCount    = rows.filter(r => r.passes_health_screen === true).length;
  const failCount    = rows.filter(r => r.passes_health_screen === false).length;
  const unknownCount = rows.filter(r => r.passes_health_screen == null).length;

  const FILTERS: { label: string; value: Filter; color: string }[] = [
    { label: `All (${rows.length})`,       value: "all",     color: "zinc" },
    { label: `Pass (${passCount})`,         value: "pass",    color: "green" },
    { label: `Fail (${failCount})`,         value: "fail",    color: "red" },
    { label: `Unknown (${unknownCount})`,   value: "unknown", color: "zinc" },
  ];

  return (
    <div>
      {/* Header */}
      <div className="flex items-baseline justify-between mb-4">
        <div>
          <h1 className="text-xl font-bold text-zinc-900 dark:text-zinc-100">Fundamental Health</h1>
          <p className="text-sm text-zinc-500 mt-0.5">
            Lynch financial health gate · {rows.length} tickers · updated daily
          </p>
        </div>
      </div>

      {/* Thresholds legend */}
      <div className="flex flex-wrap gap-4 text-[11px] text-zinc-500 mb-4 border border-zinc-800 rounded-lg px-3 py-2">
        <span><span className="text-red-400 font-semibold">Fail thresholds:</span></span>
        <span>D/E <span className="text-zinc-300">&gt; 2.0</span></span>
        <span>Current Ratio <span className="text-zinc-300">&lt; 1.2</span></span>
        <span>Interest Coverage <span className="text-zinc-300">&lt; 3.0x</span></span>
        <span>FCF Positive <span className="text-zinc-300">&lt; 2 of 3 years</span></span>
      </div>

      {/* Controls */}
      <div className="flex flex-wrap items-center gap-3 mb-4">
        <input
          value={search}
          onChange={e => setSearch(e.target.value)}
          placeholder="Search ticker..."
          className="w-48 bg-zinc-50 dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-700 rounded px-3 py-1.5 text-sm text-zinc-900 dark:text-zinc-100 placeholder:text-zinc-400 focus:outline-none focus:border-zinc-400 dark:focus:border-zinc-500"
        />
        <div className="flex rounded-lg border border-zinc-200 dark:border-zinc-700 overflow-hidden text-xs">
          {FILTERS.map(({ label, value }) => (
            <button
              key={value}
              onClick={() => setFilter(value)}
              className={`px-3 py-1.5 transition-colors ${
                filter === value
                  ? "bg-zinc-900 dark:bg-zinc-100 text-white dark:text-zinc-900 font-semibold"
                  : "text-zinc-500 hover:text-zinc-900 dark:hover:text-zinc-100"
              }`}
            >
              {label}
            </button>
          ))}
        </div>
        <span className="text-xs text-zinc-500">{filtered.length} shown</span>
      </div>

      {/* Table */}
      <div className="overflow-x-auto rounded-lg border border-zinc-200 dark:border-zinc-800">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-zinc-200 dark:border-zinc-800 text-zinc-500">
              {HEADERS.map(({ label, key }) => (
                <th
                  key={key}
                  onClick={() => toggleSort(key)}
                  className="px-3 py-2 text-left font-medium whitespace-nowrap cursor-pointer hover:text-zinc-300 select-none"
                >
                  {label}
                  {sortKey === key && (
                    <span className="ml-1 text-zinc-400">{sortAsc ? "▲" : "▼"}</span>
                  )}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {filtered.length === 0 ? (
              <tr>
                <td colSpan={HEADERS.length} className="px-3 py-8 text-center text-zinc-500">
                  {rows.length === 0
                    ? "No fundamental data yet — run update_fundamentals_s3.py to populate."
                    : "No results for current filter."}
                </td>
              </tr>
            ) : (
              filtered.map(row => (
                <tr
                  key={row.ticker}
                  className="border-b border-zinc-100 dark:border-zinc-800/50 hover:bg-zinc-50 dark:hover:bg-zinc-800/30 transition-colors"
                >
                  <td className="px-3 py-2 font-bold">
                    <Link
                      href={`/oracle?ticker=${row.ticker}`}
                      className="text-zinc-900 dark:text-zinc-100 hover:text-green-500 dark:hover:text-green-400 transition-colors"
                    >
                      {row.ticker}
                    </Link>
                  </td>
                  <td className="px-3 py-2">
                    <GateChip v={row.passes_health_screen} />
                  </td>
                  <td className="px-3 py-2">
                    <Metric
                      v={row.debt_equity}
                      low={1.0} high={2.0}
                      invert={true}
                      fmt={n => n.toFixed(2)}
                    />
                  </td>
                  <td className="px-3 py-2">
                    <Metric
                      v={row.current_ratio}
                      low={1.2} high={1.5}
                      fmt={n => n.toFixed(2)}
                    />
                  </td>
                  <td className="px-3 py-2">
                    {row.interest_coverage == null
                      ? <span className="text-zinc-600">N/A</span>
                      : <Metric
                          v={row.interest_coverage}
                          low={3.0} high={5.0}
                          fmt={n => `${n.toFixed(1)}x`}
                        />
                    }
                  </td>
                  <td className="px-3 py-2">
                    <Metric
                      v={row.fcf_positive_years}
                      low={2} high={3}
                      fmt={n => `${n}/3`}
                    />
                  </td>
                  <td className="px-3 py-2 text-zinc-500">
                    {row.as_of_date ? String(row.as_of_date).slice(0, 10) : "-"}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
