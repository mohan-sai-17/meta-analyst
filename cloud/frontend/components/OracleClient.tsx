"use client";
import { useState, useCallback, useMemo, useEffect } from "react";
import { useSearchParams } from "next/navigation";
import dynamic from "next/dynamic";

const OHLCChart = dynamic(() => import("@/components/OHLCChart"), { ssr: false });

interface Ticker     { ticker: string; stock_name: string }
interface PriceTarget {
  Ticker: string;
  current_price: number;
  top_tier_firm_count: number;
  expected_return_3m_pct: number;
  expected_return_6m_pct: number;
  avg_win_rate_3m: number;
  avg_win_rate_6m: number;
  target_price_3m: number;
  target_price_6m: number;
}
interface ScorecardRow {
  Firm: string;
  signal_count: number;
  win_rate_3m: number;
  win_rate_6m: number;
  avg_return_3m: number;
  avg_return_6m: number;
}
interface OHLCRow    { Date: string; Open: number; High: number; Low: number; Close: number }
interface RatingRow  { Ticker: string; GradeDate: string; Firm: string; Action: string; ToGrade: string; FromGrade: string }
interface MonthRow   { month: number; win_rate: number; avg_return: number; years_observed?: number }
interface WeekRow    { week_num: number; win_rate: number; avg_return: number; count: number }

const MONTH_NAMES = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];

function cellBg(v: number): string {
  if (v >= 5)  return "bg-green-600/90 dark:bg-green-500/80";
  if (v >= 3)  return "bg-green-500/70 dark:bg-green-600/60";
  if (v >= 1)  return "bg-green-400/50 dark:bg-green-700/50";
  if (v > 0)   return "bg-green-300/30 dark:bg-green-800/40";
  if (v >= -1) return "bg-red-300/30 dark:bg-red-800/40";
  if (v >= -3) return "bg-red-400/50 dark:bg-red-700/50";
  if (v >= -5) return "bg-red-500/70 dark:bg-red-600/60";
  return "bg-red-600/90 dark:bg-red-500/80";
}
function cellText(v: number): string {
  return Math.abs(v) >= 3 ? "text-white" : "text-zinc-900 dark:text-zinc-100";
}

const API = process.env.NEXT_PUBLIC_API_URL!;

async function lambdaGet<T>(path: string, params?: Record<string, string>): Promise<T> {
  const url = new URL(`${API}${path}`);
  if (params) Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, v));
  const r = await fetch(url.toString());
  if (!r.ok) throw new Error(`${r.status}`);
  return r.json();
}

export default function OracleClient({
  tickers,
  allTargets,
}: {
  tickers: Ticker[];
  allTargets: PriceTarget[];
}) {
  const searchParams   = useSearchParams();
  const initialTicker  = searchParams.get("ticker") ?? undefined;
  const [query,      setQuery]     = useState("");
  const [selected,   setSelected]  = useState<string | null>(null);
  const [pt,         setPt]        = useState<PriceTarget | null>(null);
  const [sc,         setSc]        = useState<ScorecardRow[]>([]);
  const [ohlc,       setOhlc]      = useState<OHLCRow[]>([]);
  const [ratings,    setRatings]   = useState<RatingRow[]>([]);
  const [seasonM,    setSeasonM]   = useState<MonthRow[]>([]);
  const [seasonW,    setSeasonW]   = useState<WeekRow[]>([]);
  const [seasonTab,  setSeasonTab] = useState<"monthly" | "weekly">("monthly");
  const [loading,    setLoading]   = useState(false);
  const [error,      setError]     = useState<string | null>(null);

  const currentMonth = new Date().getMonth() + 1;

  // ticker → company name lookup
  const nameMap = useMemo(
    () => Object.fromEntries(tickers.map(t => [t.ticker, t.stock_name])),
    [tickers]
  );

  // Dropdown suggestions (all tickers, only in default state)
  const suggestions = useMemo(() => {
    if (!query || selected) return [];
    const q = query.toLowerCase();
    return tickers
      .filter(t => t.ticker.toLowerCase().includes(q) || t.stock_name.toLowerCase().includes(q))
      .slice(0, 8);
  }, [query, selected, tickers]);

  // Oracle table rows filtered by search query
  const filteredTargets = useMemo(() => {
    if (!query) return allTargets;
    const q = query.toLowerCase();
    return allTargets.filter(t =>
      t.Ticker.toLowerCase().includes(q) ||
      (nameMap[t.Ticker] || "").toLowerCase().includes(q)
    );
  }, [query, allTargets, nameMap]);

  const load = useCallback(async (ticker: string) => {
    setSelected(ticker);
    setQuery(ticker);
    setLoading(true);
    setError(null);
    try {
      const [ptData, scData, ohlcData, ratingsData, seasonMData, seasonWData] = await Promise.all([
        lambdaGet<PriceTarget>(`/price-targets/${ticker}`).catch(() => null),
        lambdaGet<ScorecardRow[]>(`/scorecard/${ticker}`).catch(() => []),
        lambdaGet<OHLCRow[]>(`/ohlc/${ticker}`, { days: "3650" }).catch(() => []),
        lambdaGet<RatingRow[]>(`/ratings/${ticker}`).catch(() => []),
        lambdaGet<MonthRow[]>(`/seasonality/${encodeURIComponent(ticker)}`, { period: "monthly" }).catch(() => []),
        lambdaGet<WeekRow[]>(`/seasonality/${encodeURIComponent(ticker)}`, { period: "weekly" }).catch(() => []),
      ]);
      setPt(ptData);
      setSc(scData);
      setOhlc(ohlcData);
      setRatings(ratingsData);
      setSeasonM(seasonMData);
      setSeasonW(seasonWData);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  // Auto-load when navigated from another tab via ?ticker=
  useEffect(() => {
    if (initialTicker) load(initialTicker);
  }, [initialTicker, load]);

  const back = () => {
    setSelected(null);
    setQuery("");
    setPt(null);
    setSc([]);
    setOhlc([]);
    setRatings([]);
    setSeasonM([]);
    setSeasonW([]);
    setError(null);
  };

  return (
    <div>
      {/* Page header */}
      <div className="flex items-baseline justify-between mb-6">
        <h1 className="text-xl font-bold text-zinc-900 dark:text-zinc-100">Oracle</h1>
        <span className="text-sm text-zinc-500">Price targets · Analyst scorecard · OHLC</span>
      </div>

      {/* Back button OR search box */}
      {selected ? (
        <button
          onClick={back}
          className="flex items-center gap-1 text-sm text-zinc-500 hover:text-zinc-900 dark:hover:text-zinc-100 mb-6 transition-colors"
        >
          ← All Coverage
        </button>
      ) : (
        <div className="relative w-full max-w-sm mb-6">
          <input
            value={query}
            onChange={e => { setQuery(e.target.value); setSelected(null); }}
            placeholder="Filter by ticker or company..."
            className="w-full bg-white dark:bg-zinc-900 border border-zinc-300 dark:border-zinc-700 rounded px-3 py-2 text-sm text-zinc-900 dark:text-zinc-100 placeholder:text-zinc-400 dark:placeholder:text-zinc-600 focus:outline-none focus:border-zinc-500"
          />
          {suggestions.length > 0 && (
            <ul className="absolute z-50 w-full mt-1 bg-white dark:bg-zinc-900 border border-zinc-300 dark:border-zinc-700 rounded shadow-xl max-h-60 overflow-y-auto">
              {suggestions.map(t => (
                <li
                  key={t.ticker}
                  onClick={() => load(t.ticker)}
                  className="px-3 py-2 text-sm cursor-pointer hover:bg-zinc-100 dark:hover:bg-zinc-800 flex items-center gap-2"
                >
                  <span className="font-bold w-14 shrink-0 text-zinc-900 dark:text-zinc-100">{t.ticker}</span>
                  <span className="text-zinc-500 truncate">{t.stock_name}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {error && (
        <div className="rounded border border-red-800 bg-red-950/40 px-4 py-3 text-sm text-red-400 mb-4">{error}</div>
      )}

      {loading && (
        <div className="text-sm text-zinc-500">Loading {selected}...</div>
      )}

      {/* ── DEFAULT: Oracle coverage table ── */}
      {!selected && !loading && (
        <div>
          <div className="text-xs text-zinc-500 uppercase tracking-wider mb-3">
            Oracle Coverage — {filteredTargets.length} tickers
          </div>
          <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 overflow-hidden">
            <div className="overflow-x-auto">
              <div className="overflow-y-auto max-h-[600px]">
                <table className="w-full text-xs">
                  <thead className="sticky top-0 bg-zinc-50 dark:bg-zinc-900 z-10 border-b border-zinc-200 dark:border-zinc-800">
                    <tr className="text-zinc-500">
                      <th className="px-3 py-2 text-left font-medium">Ticker</th>
                      <th className="px-3 py-2 text-left font-medium">Company</th>
                      <th className="px-3 py-2 text-right font-medium">Price</th>
                      <th className="px-3 py-2 text-right font-medium">3M Target</th>
                      <th className="px-3 py-2 text-right font-medium">3M Upside</th>
                      <th className="px-3 py-2 text-right font-medium">Win Rate</th>
                      <th className="px-3 py-2 text-right font-medium">Firms</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredTargets.map(t => (
                      <tr
                        key={t.Ticker}
                        onClick={() => load(t.Ticker)}
                        className="border-b border-zinc-100 dark:border-zinc-800/50 hover:bg-zinc-50 dark:hover:bg-zinc-800/30 transition-colors cursor-pointer"
                      >
                        <td className="px-3 py-2 font-bold text-zinc-900 dark:text-zinc-100">{t.Ticker}</td>
                        <td className="px-3 py-2 text-zinc-500 max-w-[200px] truncate">{nameMap[t.Ticker] || "—"}</td>
                        <td className="px-3 py-2 text-right text-zinc-700 dark:text-zinc-300">${t.current_price?.toFixed(2)}</td>
                        <td className="px-3 py-2 text-right text-zinc-700 dark:text-zinc-300">${t.target_price_3m?.toFixed(2)}</td>
                        <td className={`px-3 py-2 text-right font-semibold ${(t.expected_return_3m_pct ?? 0) >= 0 ? "text-green-500 dark:text-green-400" : "text-red-500 dark:text-red-400"}`}>
                          {(t.expected_return_3m_pct ?? 0) >= 0 ? "+" : ""}{t.expected_return_3m_pct?.toFixed(1)}%
                        </td>
                        <td className="px-3 py-2 text-right text-zinc-500">{t.avg_win_rate_3m?.toFixed(0)}%</td>
                        <td className="px-3 py-2 text-right text-zinc-500">{t.top_tier_firm_count}</td>
                      </tr>
                    ))}
                    {filteredTargets.length === 0 && (
                      <tr>
                        <td colSpan={7} className="px-3 py-8 text-center text-zinc-400">
                          No Oracle coverage matches your search.
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* ── SELECTED: Stock detail view ── */}
      {selected && !loading && (
        <div className="flex flex-col gap-6">

          {/* Stock header */}
          <div>
            <div className="text-2xl font-bold text-zinc-900 dark:text-zinc-100">{selected}</div>
            {nameMap[selected] && (
              <div className="text-sm text-zinc-500 mt-0.5">{nameMap[selected]}</div>
            )}
          </div>

          {/* Price target cards */}
          {pt ? (
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
              {[
                { label: "Current Price",  value: `$${pt.current_price?.toFixed(2)}`,        sub: null },
                { label: "3M Target",      value: `$${pt.target_price_3m?.toFixed(2)}`,      sub: `+${pt.expected_return_3m_pct?.toFixed(1)}% upside` },
                { label: "6M Target",      value: `$${pt.target_price_6m?.toFixed(2)}`,      sub: `+${pt.expected_return_6m_pct?.toFixed(1)}% upside` },
                { label: "Top Tier Firms", value: String(pt.top_tier_firm_count),             sub: `${pt.avg_win_rate_6m?.toFixed(0)}% win rate 6M` },
              ].map(({ label, value, sub }) => (
                <div key={label} className="bg-zinc-50 dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 rounded-lg px-4 py-3">
                  <div className="text-xs text-zinc-500 mb-1">{label}</div>
                  <div className="text-lg font-bold text-zinc-900 dark:text-zinc-100">{value}</div>
                  {sub && <div className="text-xs text-green-500 dark:text-green-400">{sub}</div>}
                </div>
              ))}
            </div>
          ) : (
            <div className="text-sm text-zinc-500">No Top Tier coverage for {selected}.</div>
          )}

          {/* OHLC chart */}
          {ohlc.length > 0 && (
            <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-900 p-4">
              <div className="text-xs text-zinc-500 mb-3">{selected} · Price History ({ohlc.length} sessions)</div>
              <OHLCChart data={ohlc} target3m={pt?.target_price_3m} target6m={pt?.target_price_6m} />
            </div>
          )}

          {/* Analyst ratings */}
          {ratings.length > 0 && (
            <div>
              <div className="text-xs text-zinc-500 uppercase tracking-wider mb-3">
                Analyst Ratings — {ratings.length} total
              </div>
              <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 overflow-hidden">
                <div className="overflow-x-auto">
                  <div className="overflow-y-auto max-h-72">
                    <table className="w-full text-xs">
                      <thead className="sticky top-0 bg-zinc-50 dark:bg-zinc-900 border-b border-zinc-200 dark:border-zinc-800">
                        <tr className="text-zinc-500">
                          {["Date", "Firm", "Action", "To Grade", "From Grade"].map(h => (
                            <th key={h} className="px-3 py-2 text-left font-medium">{h}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {ratings.map((r, i) => (
                          <tr key={i} className="border-b border-zinc-100 dark:border-zinc-800/50 hover:bg-zinc-50 dark:hover:bg-zinc-800/30 transition-colors">
                            <td className="px-3 py-2 text-zinc-500 whitespace-nowrap">{r.GradeDate}</td>
                            <td className="px-3 py-2 text-zinc-700 dark:text-zinc-300 font-medium">{r.Firm}</td>
                            <td className="px-3 py-2">
                              <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${
                                r.Action === "up"
                                  ? "bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-400"
                                  : "bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-400"
                              }`}>
                                {r.Action === "up" ? "UPGRADE" : "INITIATE"}
                              </span>
                            </td>
                            <td className="px-3 py-2 text-zinc-700 dark:text-zinc-300">{r.ToGrade}</td>
                            <td className="px-3 py-2 text-zinc-500">{r.FromGrade || "—"}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              </div>
            </div>
          )}

          {/* Analyst scorecard */}
          {sc.length > 0 && (
            <div>
              <div className="text-xs text-zinc-500 uppercase tracking-wider mb-3">Analyst Scorecard</div>
              <div className="overflow-x-auto rounded-lg border border-zinc-200 dark:border-zinc-800">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b border-zinc-200 dark:border-zinc-800 text-zinc-500">
                      {["Firm", "Signals", "Win% 3M", "Win% 6M", "Avg Ret 3M", "Avg Ret 6M"].map(h => (
                        <th key={h} className="px-3 py-2 text-left font-medium">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {sc.map((row, i) => (
                      <tr key={i} className="border-b border-zinc-100 dark:border-zinc-800/50 hover:bg-zinc-50 dark:hover:bg-zinc-800/30 transition-colors">
                        <td className="px-3 py-2 text-zinc-700 dark:text-zinc-300 font-medium">{row.Firm}</td>
                        <td className="px-3 py-2 text-zinc-500">{row.signal_count}</td>
                        <td className="px-3 py-2 text-zinc-700 dark:text-zinc-300">{row.win_rate_3m?.toFixed(0)}%</td>
                        <td className="px-3 py-2 text-zinc-700 dark:text-zinc-300">{row.win_rate_6m?.toFixed(0)}%</td>
                        <td className={`px-3 py-2 font-semibold ${row.avg_return_3m >= 0 ? "text-green-500 dark:text-green-400" : "text-red-500 dark:text-red-400"}`}>
                          {row.avg_return_3m >= 0 ? "+" : ""}{row.avg_return_3m?.toFixed(1)}%
                        </td>
                        <td className={`px-3 py-2 font-semibold ${row.avg_return_6m >= 0 ? "text-green-500 dark:text-green-400" : "text-red-500 dark:text-red-400"}`}>
                          {row.avg_return_6m >= 0 ? "+" : ""}{row.avg_return_6m?.toFixed(1)}%
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* Seasonality — monthly + weekly toggle */}
          {(seasonM.length > 0 || seasonW.length > 0) && (
            <div>
              <div className="flex items-center justify-between mb-3">
                <div className="text-xs text-zinc-500 uppercase tracking-wider">Seasonality</div>
                <div className="flex rounded overflow-hidden border border-zinc-200 dark:border-zinc-700">
                  {(["monthly", "weekly"] as const).map(tab => (
                    <button
                      key={tab}
                      onClick={() => setSeasonTab(tab)}
                      className={`px-3 py-1 text-xs font-medium transition-colors ${
                        seasonTab === tab
                          ? "bg-zinc-900 dark:bg-zinc-100 text-white dark:text-zinc-900"
                          : "text-zinc-500 hover:text-zinc-900 dark:hover:text-zinc-100 hover:bg-zinc-50 dark:hover:bg-zinc-900"
                      }`}
                    >
                      {tab === "monthly" ? "Monthly" : "Weekly"}
                    </button>
                  ))}
                </div>
              </div>

              {/* Monthly heatmap */}
              {seasonTab === "monthly" && seasonM.length > 0 && (
                <div className="grid grid-cols-3 sm:grid-cols-6 gap-2">
                  {Array.from({ length: 12 }, (_, i) => i + 1).map(m => {
                    const row = seasonM.find(r => r.month === m);
                    const isCurrent = m === currentMonth;
                    const bg = row ? cellBg(row.avg_return) : "bg-zinc-100 dark:bg-zinc-800/50";
                    const tc = row ? cellText(row.avg_return) : "text-zinc-500";
                    return (
                      <div
                        key={m}
                        className={`relative rounded-lg p-3 ${bg} ${
                          isCurrent
                            ? "ring-2 ring-offset-2 ring-zinc-900 dark:ring-zinc-100 ring-offset-white dark:ring-offset-zinc-950"
                            : ""
                        }`}
                      >
                        <div className={`text-xs font-semibold mb-1 ${tc}`}>
                          {MONTH_NAMES[m - 1]}
                          {isCurrent && <span className="ml-1 text-[10px] opacity-70">(now)</span>}
                        </div>
                        {row ? (
                          <>
                            <div className={`text-lg font-bold leading-tight ${tc}`}>{row.win_rate.toFixed(0)}%</div>
                            <div className={`text-[11px] mt-0.5 ${tc} opacity-80`}>
                              {row.avg_return >= 0 ? "+" : ""}{row.avg_return.toFixed(1)}% avg
                            </div>
                            <div className={`text-[10px] mt-0.5 ${tc} opacity-60`}>
                              n={row.years_observed ?? "—"}
                            </div>
                          </>
                        ) : (
                          <div className="text-xs text-zinc-400">No data</div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}

              {/* Weekly heatmap — weeks 1–52 */}
              {seasonTab === "weekly" && seasonW.length > 0 && (
                <div
                  className="grid gap-1"
                  style={{ gridTemplateColumns: "repeat(auto-fill, minmax(52px, 1fr))" }}
                >
                  {Array.from({ length: 52 }, (_, i) => i + 1).map(w => {
                    const row = seasonW.find(r => r.week_num === w);
                    const bg = row ? cellBg(row.avg_return) : "bg-zinc-100 dark:bg-zinc-800/50";
                    const tc = row ? cellText(row.avg_return) : "text-zinc-500";
                    return (
                      <div key={w} className={`rounded p-1.5 ${bg}`}>
                        <div className={`text-[10px] font-semibold mb-0.5 ${tc} opacity-70`}>W{w}</div>
                        {row ? (
                          <>
                            <div className={`text-sm font-bold leading-tight ${tc}`}>{row.win_rate.toFixed(0)}%</div>
                            <div className={`text-[10px] ${tc} opacity-80`}>
                              {row.avg_return >= 0 ? "+" : ""}{row.avg_return.toFixed(1)}%
                            </div>
                          </>
                        ) : (
                          <div className="text-[10px] text-zinc-400">–</div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}

              {seasonTab === "monthly" && seasonM.length === 0 && (
                <div className="text-sm text-zinc-500">No monthly seasonality data for {selected}.</div>
              )}
              {seasonTab === "weekly" && seasonW.length === 0 && (
                <div className="text-sm text-zinc-500">No weekly seasonality data for {selected}.</div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
