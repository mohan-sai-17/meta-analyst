"use client";
import { useState, useMemo } from "react";
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid,
  Tooltip, Legend, ResponsiveContainer,
} from "recharts";

interface TradeRow {
  year: number;
  entry_date: string;
  exit_date: string;
  ticker: string;
  signal_type: "analyst" | "seasonal";
  entry_price: number;
  exit_price: number;
  pct_return: number;
  exit_reason: "profit" | "breakeven" | "time_stop" | "year_end";
  expected_return_pct: number;
  profit_target_pct: number | null;
  hold_days: number;
  portfolio_value_after: number;
  spy_return_yr: number | null;
  strategy: string;
}

const STRATEGIES: { key: string; label: string; desc: string }[] = [
  { key: "base",                      label: "Base",                        desc: "Fixed +5% target, dynamic sizing, breakeven exit" },
  { key: "season_confirmed",          label: "Season Confirmed",            desc: "Same as Base + analyst signals only when month avg_return>0% & win_rate≥60%" },
  { key: "analyst_target",            label: "Analyst Target",              desc: "Profit target = firm's avg_ret_3m (5–25%), max 25% per position" },
  { key: "analyst_target_season",     label: "Analyst Target + Season",     desc: "Analyst Target with season confirmation" },
  { key: "sector_h2",                 label: "Sector H2 Aware",             desc: "Fixed +5% target — skips signals in Jul–Dec from sectors that historically underperform in H2" },
  { key: "sector_h2_analyst_target",  label: "Sector H2 + Analyst Target",  desc: "Analyst Target (5–25%) + skip bad H2 sectors in Jul–Dec" },
  { key: "rebalance_20",              label: "Rebalance (max 20)",           desc: "New signal arrives → trim all existing positions to equal weight to fund it. Max 20 slots. Losing positions trimmed at 50%." },
  { key: "rebalance_unlimited",       label: "Rebalance (unlimited)",        desc: "Same rebalance logic but no slot cap — every signal gets added, portfolio always equal-weight." },
  { key: "base_10pct",                label: "Base 10%",                     desc: "Fixed +10% profit target, dynamic sizing, breakeven exit after 90 days — higher bar than Base 5%" },
  { key: "season_confirmed_10pct",    label: "Season Confirmed 10%",         desc: "Fixed +10% target + analyst signals only when month win_rate ≥ 60% confirms" },
];

function fmt(v: number | null, dec = 1, plus = true) {
  if (v == null) return "N/A";
  return `${plus && v > 0 ? "+" : ""}${v.toFixed(dec)}%`;
}

function fmtDollar(v: number) {
  return `$${v.toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
}

// ── Equity-curve chart tooltip ─────────────────────────────────────────────
function ChartTooltip({ active, payload }: { active?: boolean; payload?: { name: string; value: number; color: string }[] }) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded border border-zinc-700 bg-zinc-900 px-3 py-2 text-xs space-y-1">
      {payload.map((p) => (
        <div key={p.name} style={{ color: p.color }}>
          {p.name}: {fmtDollar(p.value)}
        </div>
      ))}
    </div>
  );
}

// ── Year filter pill ───────────────────────────────────────────────────────
const PILL = "px-2.5 py-1 rounded text-xs font-medium transition-colors cursor-pointer";
const PILL_ON  = "bg-zinc-700 text-zinc-100";
const PILL_OFF = "bg-zinc-900 border border-zinc-700 text-zinc-400 hover:text-zinc-200";

export default function CompounderClient({ data }: { data: TradeRow[] }) {
  const [strategy, setStrategy]             = useState("base");
  const [yearFilter, setYearFilter]         = useState<number | null>(null);
  const [signalFilter, setSignalFilter]     = useState<"all" | "analyst" | "seasonal">("all");
  const [exitFilter, setExitFilter]         = useState<"all" | "profit" | "breakeven">("all");
  const [logPage, setLogPage]               = useState(0);
  const PAGE_SIZE = 50;

  // Filter to selected strategy first
  const stratData = useMemo(
    () => data.filter((r) => r.strategy === strategy),
    [data, strategy]
  );

  const years = useMemo(
    () => Array.from(new Set(stratData.map((r) => r.year))).sort(),
    [stratData]
  );

  // Real trades only (exclude year_end forced closures from performance metrics)
  const realTrades = useMemo(
    () => stratData.filter((r) => r.exit_reason !== "year_end"),
    [stratData]
  );

  // ── Year-by-year summary ─────────────────────────────────────────────────
  const yearSummary = useMemo(() => {
    const CAPITAL = 15_000;
    let prevValue = CAPITAL;

    return years.map((yr) => {
      const yrTrades = stratData.filter((r) => r.year === yr);
      const yrReal   = yrTrades.filter((r) => r.exit_reason !== "year_end");

      // Final portfolio value = last trade's portfolio_value_after in this year
      const sorted    = [...yrTrades].sort((a, b) => a.exit_date.localeCompare(b.exit_date));
      const finalPV   = sorted.at(-1)?.portfolio_value_after ?? prevValue;
      const annualRet = prevValue > 0 ? (finalPV / prevValue - 1) * 100 : null;

      // SPY return for this year (same for all trades)
      const spyRet = yrTrades[0]?.spy_return_yr ?? null;

      const profits    = yrReal.filter((r) => r.exit_reason === "profit").length;
      const breakevenExits = yrReal.filter((r) => r.exit_reason === "breakeven").length;
      const winRate    = yrReal.length > 0 ? (profits / yrReal.length) * 100 : null;
      const avgHold    = yrReal.length > 0
        ? yrReal.reduce((s, r) => s + r.hold_days, 0) / yrReal.length
        : null;
      const analystCt  = yrReal.filter((r) => r.signal_type === "analyst").length;
      const seasonalCt = yrReal.filter((r) => r.signal_type === "seasonal").length;

      const row = {
        year: yr,
        trades: yrReal.length,
        analystCt,
        seasonalCt,
        profits,
        breakevenExits,
        winRate,
        avgHold,
        portfolioStart: prevValue,
        portfolioEnd: finalPV,
        annualRet,
        spyRet,
        alpha: annualRet != null && spyRet != null ? annualRet - spyRet : null,
      };

      prevValue = finalPV;
      return row;
    });
  }, [data, years]);

  // ── Equity curve data ─────────────────────────────────────────────────────
  const equityCurveData = useMemo(() => {
    const CAPITAL = 15_000;
    // Points: one per closed trade (sorted by exit_date)
    const sorted = [...stratData].sort((a, b) => a.exit_date.localeCompare(b.exit_date));

    // Build SPY compounded values per year-end
    let spyValue = CAPITAL;
    const spyByYear: Record<number, number> = {};
    for (const yr of years) {
      const yrFirst = data.find((r) => r.year === yr);
      const spyRet  = yrFirst?.spy_return_yr ?? 0;
      spyValue      = spyValue * (1 + (spyRet ?? 0) / 100);
      spyByYear[yr] = spyValue;
    }

    // Strategy curve: one point per trade
    const points = sorted.map((t) => ({
      date     : t.exit_date,
      strategy : t.portfolio_value_after,
    }));

    // Merge in SPY year-end points — find last trade of each year to anchor SPY
    const merged: { date: string; strategy?: number; spy?: number }[] = [];
    let lastYearSeen = -1;

    for (const p of points) {
      const yr = parseInt(p.date.slice(0, 4));
      if (yr !== lastYearSeen && yr in spyByYear) {
        // Add SPY year-end anchor just before this year flips
        if (lastYearSeen > 0) {
          merged.push({ date: `${lastYearSeen}-12-31`, spy: spyByYear[lastYearSeen] });
        }
        lastYearSeen = yr;
      }
      merged.push({ date: p.date, strategy: p.strategy });
    }
    // Final SPY endpoint
    if (lastYearSeen > 0 && spyByYear[lastYearSeen]) {
      merged.push({ date: `${lastYearSeen}-12-31`, spy: spyByYear[lastYearSeen] });
    }

    return merged;
  }, [data, years]);

  // ── Overall summary stats ────────────────────────────────────────────────
  const overallStats = useMemo(() => {
    const CAPITAL = 15_000;
    if (!yearSummary.length) return null;
    const last      = yearSummary.at(-1)!;
    const totalRet  = (last.portfolioEnd / CAPITAL - 1) * 100;
    const profits   = realTrades.filter((r) => r.exit_reason === "profit").length;
    const winRate   = realTrades.length > 0 ? (profits / realTrades.length) * 100 : null;
    const avgHold   = realTrades.length > 0
      ? realTrades.reduce((s, r) => s + r.hold_days, 0) / realTrades.length
      : null;
    const analystCt  = realTrades.filter((r) => r.signal_type === "analyst").length;
    const seasonalCt = realTrades.filter((r) => r.signal_type === "seasonal").length;
    return { totalRet, finalPV: last.portfolioEnd, winRate, avgHold, analystCt, seasonalCt };
  }, [yearSummary, realTrades]);

  // ── 2026 YTD live stats (for selected strategy) ──────────────────────────
  const ytd2026 = useMemo(() => {
    const yr2026 = stratData.filter((r) => r.year === 2026);
    if (!yr2026.length) return null;

    // Closed trades (real exits) vs still-open positions (year_end = force-closed at last available price)
    const closed = yr2026.filter((r) => r.exit_reason !== "year_end");
    const open   = yr2026.filter((r) => r.exit_reason === "year_end");

    // Starting value = last portfolio value before 2026 (end of 2025)
    const pre2026 = stratData.filter((r) => r.year < 2026).sort((a, b) => a.exit_date.localeCompare(b.exit_date));
    const startValue = pre2026.at(-1)?.portfolio_value_after ?? 15_000;

    // Current value = last portfolio_value_after across all 2026 trades
    const sorted2026 = [...yr2026].sort((a, b) => a.exit_date.localeCompare(b.exit_date));
    const currentValue = sorted2026.at(-1)?.portfolio_value_after ?? startValue;
    const ytdReturn = (currentValue / startValue - 1) * 100;

    const profits  = closed.filter((r) => r.exit_reason === "profit").length;
    const winRate  = closed.length > 0 ? (profits / closed.length) * 100 : null;
    const lastDate = sorted2026.at(-1)?.exit_date ?? "—";
    const spyYtd   = yr2026[0]?.spy_return_yr ?? null;

    return { startValue, currentValue, ytdReturn, closed, open, winRate, lastDate, spyYtd };
  }, [stratData]);

  // ── Strategy comparison (all strategies, side-by-side) ───────────────────
  const stratComparison = useMemo(() => {
    const CAPITAL = 15_000;

    // SPY compounded over all years (same value for every strategy)
    const allYears = Array.from(new Set(data.map((r) => r.year))).sort();
    let spyFinal = CAPITAL;
    for (const yr of allYears) {
      const spyRet = data.find((r) => r.year === yr)?.spy_return_yr ?? 0;
      spyFinal = spyFinal * (1 + (spyRet ?? 0) / 100);
    }
    const spyTotalRet = (spyFinal / CAPITAL - 1) * 100;

    return STRATEGIES.map(({ key, label }) => {
      const sData = data.filter((r) => r.strategy === key);
      if (!sData.length) return { key, label, finalPV: null, totalRet: null, trades: 0, winRate: null, avgHold: null, alpha: null };

      const sorted  = [...sData].sort((a, b) => a.exit_date.localeCompare(b.exit_date));
      const finalPV = sorted.at(-1)?.portfolio_value_after ?? CAPITAL;
      const totalRet = (finalPV / CAPITAL - 1) * 100;

      const real    = sData.filter((r) => r.exit_reason !== "year_end");
      const profits = real.filter((r) => r.exit_reason === "profit").length;
      const winRate = real.length > 0 ? (profits / real.length) * 100 : null;
      const avgHold = real.length > 0 ? real.reduce((s, r) => s + r.hold_days, 0) / real.length : null;

      return { key, label, finalPV, totalRet, trades: real.length, winRate, avgHold, alpha: totalRet - spyTotalRet };
    });
  }, [data]);

  // ── Filtered trade log ────────────────────────────────────────────────────
  const logRows = useMemo(() => {
    return realTrades
      .filter((r) => yearFilter   == null || r.year === yearFilter)
      .filter((r) => signalFilter === "all" || r.signal_type === signalFilter)
      .filter((r) => exitFilter   === "all" || r.exit_reason === exitFilter)
      .sort((a, b) => b.exit_date.localeCompare(a.exit_date));
  }, [realTrades, yearFilter, signalFilter, exitFilter]);

  const logPage_rows = logRows.slice(logPage * PAGE_SIZE, (logPage + 1) * PAGE_SIZE);
  const totalPages   = Math.ceil(logRows.length / PAGE_SIZE);

  const selectCls = "bg-zinc-900 border border-zinc-700 text-zinc-100 text-xs rounded px-2 py-1.5 focus:outline-none focus:border-zinc-500";

  return (
    <div className="space-y-8">

      {/* ── Strategy selector ─────────────────────────────────────────── */}
      <div className="flex flex-wrap gap-2 items-center">
        <span className="text-xs text-zinc-400 mr-1">Strategy</span>
        {STRATEGIES.map((s) => (
          <span
            key={s.key}
            onClick={() => { setStrategy(s.key); setYearFilter(null); setLogPage(0); }}
            className={`${PILL} ${strategy === s.key ? PILL_ON : PILL_OFF}`}
          >
            {s.label}
          </span>
        ))}
        <span className="text-xs text-zinc-500 ml-1">
          {STRATEGIES.find(s => s.key === strategy)?.desc}
        </span>
      </div>

      {/* ── 2026 YTD live banner ──────────────────────────────────────── */}
      {ytd2026 && (
        <div className="rounded-lg border border-zinc-700 bg-zinc-900/60 px-5 py-4">
          <div className="flex items-center gap-2 mb-4">
            <span className="text-sm font-semibold text-zinc-100">2026 YTD</span>
            <span className="text-[10px] font-bold px-1.5 py-0.5 rounded bg-green-500/20 text-green-400 border border-green-500/30 tracking-wide">LIVE</span>
            <span className="text-xs text-zinc-500 ml-auto">as of {ytd2026.lastDate}</span>
          </div>

          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-4">
            <div>
              <div className="text-xs text-zinc-500 mb-0.5">Starting Value</div>
              <div className="text-base font-bold text-zinc-300">{fmtDollar(ytd2026.startValue)}</div>
              <div className="text-xs text-zinc-600">end of 2025</div>
            </div>
            <div>
              <div className="text-xs text-zinc-500 mb-0.5">Current Value</div>
              <div className="text-base font-bold text-zinc-100">{fmtDollar(ytd2026.currentValue)}</div>
            </div>
            <div>
              <div className="text-xs text-zinc-500 mb-0.5">YTD Return</div>
              <div className={`text-base font-bold ${ytd2026.ytdReturn >= 0 ? "text-green-400" : "text-red-400"}`}>
                {fmt(ytd2026.ytdReturn)}
              </div>
            </div>
            <div>
              <div className="text-xs text-zinc-500 mb-0.5">vs SPY YTD</div>
              <div className={`text-base font-bold ${ytd2026.spyYtd == null ? "text-zinc-500" : (ytd2026.ytdReturn - ytd2026.spyYtd) >= 0 ? "text-green-400" : "text-red-400"}`}>
                {ytd2026.spyYtd != null ? fmt(ytd2026.ytdReturn - ytd2026.spyYtd) : "—"}
              </div>
              {ytd2026.spyYtd != null && (
                <div className="text-xs text-zinc-600">SPY {fmt(ytd2026.spyYtd)}</div>
              )}
            </div>
          </div>

          <div className="flex flex-wrap gap-4 text-xs text-zinc-400 mb-3">
            <span>Closed trades: <span className="text-zinc-200 font-medium">{ytd2026.closed.length}</span></span>
            <span>Win rate: <span className={`font-medium ${ytd2026.winRate == null ? "text-zinc-500" : ytd2026.winRate >= 50 ? "text-green-400" : "text-red-400"}`}>
              {ytd2026.winRate != null ? `${ytd2026.winRate.toFixed(0)}%` : "—"}
            </span></span>
            <span>Open positions: <span className="text-zinc-200 font-medium">{ytd2026.open.length}</span></span>
          </div>

          {ytd2026.open.length > 0 && (
            <div className="overflow-x-auto rounded border border-zinc-800">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-zinc-800 text-zinc-500">
                    <th className="px-3 py-2 text-left font-medium">Ticker</th>
                    <th className="px-3 py-2 text-left font-medium">Signal</th>
                    <th className="px-3 py-2 text-left font-medium">Entry Date</th>
                    <th className="px-3 py-2 text-right font-medium">Entry $</th>
                    <th className="px-3 py-2 text-right font-medium">Last $</th>
                    <th className="px-3 py-2 text-right font-medium">Return</th>
                    <th className="px-3 py-2 text-right font-medium">Days</th>
                    <th className="px-3 py-2 text-right font-medium">Target</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-zinc-800">
                  {ytd2026.open.sort((a, b) => b.pct_return - a.pct_return).map((r, i) => (
                    <tr key={i} className="hover:bg-zinc-800/30 transition-colors">
                      <td className="px-3 py-2 font-semibold text-zinc-100">{r.ticker}</td>
                      <td className="px-3 py-2">
                        <span className={`px-1.5 py-0.5 rounded font-medium ${r.signal_type === "analyst" ? "bg-blue-950 text-blue-300" : "bg-amber-950 text-amber-300"}`}>
                          {r.signal_type}
                        </span>
                      </td>
                      <td className="px-3 py-2 text-zinc-400">{r.entry_date}</td>
                      <td className="px-3 py-2 text-right text-zinc-300">${r.entry_price.toFixed(2)}</td>
                      <td className="px-3 py-2 text-right text-zinc-300">${r.exit_price.toFixed(2)}</td>
                      <td className={`px-3 py-2 text-right font-medium ${r.pct_return >= 0 ? "text-green-400" : "text-red-400"}`}>
                        {fmt(r.pct_return, 2)}
                      </td>
                      <td className="px-3 py-2 text-right text-zinc-400">{r.hold_days}d</td>
                      <td className="px-3 py-2 text-right text-zinc-500">
                        {r.profit_target_pct != null ? `+${r.profit_target_pct.toFixed(0)}%` : "+5%"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* ── Strategy comparison table ─────────────────────────────────── */}
      <div className="overflow-x-auto rounded-lg border border-zinc-800">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-zinc-800 text-zinc-400 text-left">
              <th className="px-3 py-2.5 font-medium">Strategy</th>
              <th className="px-3 py-2.5 font-medium text-right">Final Value</th>
              <th className="px-3 py-2.5 font-medium text-right">Total Return</th>
              <th className="px-3 py-2.5 font-medium text-right">vs SPY</th>
              <th className="px-3 py-2.5 font-medium text-right">Trades</th>
              <th className="px-3 py-2.5 font-medium text-right">Win Rate</th>
              <th className="px-3 py-2.5 font-medium text-right">Avg Hold</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-800">
            {stratComparison.map((s) => {
              const active = s.key === strategy;
              return (
                <tr
                  key={s.key}
                  onClick={() => { setStrategy(s.key); setYearFilter(null); setLogPage(0); }}
                  className={`cursor-pointer transition-colors ${active ? "bg-zinc-800/60" : "hover:bg-zinc-800/30"}`}
                >
                  <td className={`px-3 py-2 font-medium ${active ? "text-zinc-100" : "text-zinc-400"}`}>
                    {active && <span className="mr-1.5 text-green-400">▶</span>}{s.label}
                  </td>
                  <td className={`px-3 py-2 text-right font-semibold ${active ? "text-zinc-100" : "text-zinc-400"}`}>
                    {s.finalPV != null ? fmtDollar(s.finalPV) : "—"}
                  </td>
                  <td className={`px-3 py-2 text-right font-semibold ${s.totalRet == null ? "text-zinc-500" : s.totalRet >= 0 ? "text-green-400" : "text-red-400"}`}>
                    {s.totalRet != null ? fmt(s.totalRet) : "—"}
                  </td>
                  <td className={`px-3 py-2 text-right font-semibold ${s.alpha == null ? "text-zinc-500" : s.alpha >= 0 ? "text-green-400" : "text-red-400"}`}>
                    {s.alpha != null ? fmt(s.alpha) : "—"}
                  </td>
                  <td className="px-3 py-2 text-right text-zinc-400">{s.trades.toLocaleString()}</td>
                  <td className={`px-3 py-2 text-right ${s.winRate == null ? "text-zinc-500" : s.winRate >= 50 ? "text-green-400" : "text-red-400"}`}>
                    {s.winRate != null ? `${s.winRate.toFixed(1)}%` : "—"}
                  </td>
                  <td className="px-3 py-2 text-right text-zinc-400">
                    {s.avgHold != null ? `${s.avgHold.toFixed(0)}d` : "—"}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* ── Summary cards ─────────────────────────────────────────────── */}
      {overallStats && (
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 px-4 py-3">
            <div className="text-xs text-zinc-500 mb-1">Final Portfolio</div>
            <div className="text-lg font-bold text-green-400">{fmtDollar(overallStats.finalPV)}</div>
            <div className="text-xs text-zinc-600 mt-0.5">started $15,000</div>
          </div>
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 px-4 py-3">
            <div className="text-xs text-zinc-500 mb-1">{years.length}-Year Total Return</div>
            <div className={`text-lg font-bold ${overallStats.totalRet >= 0 ? "text-green-400" : "text-red-400"}`}>
              {fmt(overallStats.totalRet, 1)}
            </div>
          </div>
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 px-4 py-3">
            <div className="text-xs text-zinc-500 mb-1">Total Trades</div>
            <div className="text-lg font-bold text-zinc-100">{realTrades.length.toLocaleString()}</div>
            <div className="text-xs text-zinc-600 mt-0.5">{overallStats.analystCt} analyst · {overallStats.seasonalCt} seasonal</div>
          </div>
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 px-4 py-3">
            <div className="text-xs text-zinc-500 mb-1">Profit Exit Rate</div>
            <div className={`text-lg font-bold ${(overallStats.winRate ?? 0) >= 50 ? "text-green-400" : "text-red-400"}`}>
              {overallStats.winRate != null ? `${overallStats.winRate.toFixed(1)}%` : "—"}
            </div>
            <div className="text-xs text-zinc-600 mt-0.5">hit +5% before day 90</div>
          </div>
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 px-4 py-3">
            <div className="text-xs text-zinc-500 mb-1">Avg Hold</div>
            <div className="text-lg font-bold text-zinc-100">
              {overallStats.avgHold != null ? `${overallStats.avgHold.toFixed(0)}d` : "—"}
            </div>
          </div>
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 px-4 py-3">
            <div className="text-xs text-zinc-500 mb-1">Position Size</div>
            <div className="text-lg font-bold text-zinc-100">Dynamic</div>
            <div className="text-xs text-zinc-600 mt-0.5">cash ÷ signals available</div>
          </div>
        </div>
      )}

      {/* ── Equity curve ──────────────────────────────────────────────── */}
      <div className="rounded-lg border border-zinc-800 bg-zinc-900/30 px-4 py-5">
        <div className="text-sm font-medium text-zinc-300 mb-4">Portfolio Growth — Strategy vs SPY $15k Buy-and-Hold</div>
        <ResponsiveContainer width="100%" height={280}>
          <LineChart data={equityCurveData} margin={{ top: 4, right: 16, left: 0, bottom: 4 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#27272a" />
            <XAxis
              dataKey="date"
              tick={{ fill: "#71717a", fontSize: 11 }}
              tickFormatter={(v: string) => v.slice(0, 4)}
              interval="preserveStartEnd"
            />
            <YAxis
              tick={{ fill: "#71717a", fontSize: 11 }}
              tickFormatter={(v: number) => `$${(v / 1000).toFixed(0)}k`}
              width={52}
            />
            <Tooltip content={<ChartTooltip />} />
            <Legend
              wrapperStyle={{ fontSize: 12, color: "#a1a1aa" }}
              formatter={(value) => value === "strategy" ? "Strategy" : "SPY $15k"}
            />
            <Line
              type="monotone"
              dataKey="strategy"
              stroke="#4ade80"
              dot={false}
              strokeWidth={1.5}
              connectNulls
            />
            <Line
              type="monotone"
              dataKey="spy"
              stroke="#60a5fa"
              dot={{ r: 3, fill: "#60a5fa" }}
              strokeWidth={1.5}
              strokeDasharray="6 3"
              connectNulls
            />
          </LineChart>
        </ResponsiveContainer>
      </div>

      {/* ── Year-by-year summary table ────────────────────────────────── */}
      <div className="overflow-x-auto rounded-lg border border-zinc-800">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-zinc-800 text-zinc-400 text-left">
              <th className="px-4 py-3 font-medium">Year</th>
              <th className="px-4 py-3 font-medium">Trades</th>
              <th className="px-4 py-3 font-medium">Analyst / Seasonal</th>
              <th className="px-4 py-3 font-medium">Profit Exits</th>
              <th className="px-4 py-3 font-medium">Avg Hold</th>
              <th className="px-4 py-3 font-medium">Year-End Value</th>
              <th className="px-4 py-3 font-medium">Annual Ret</th>
              <th className="px-4 py-3 font-medium">SPY</th>
              <th className="px-4 py-3 font-medium">Alpha</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-800">
            {yearSummary.map((r) => (
              <tr
                key={r.year}
                className="hover:bg-zinc-800/30 transition-colors cursor-pointer"
                onClick={() => setYearFilter(yearFilter === r.year ? null : r.year)}
              >
                <td className={`px-4 py-3 font-semibold ${yearFilter === r.year ? "text-green-400" : "text-zinc-100"}`}>{r.year}</td>
                <td className="px-4 py-3 text-zinc-300">{r.trades}</td>
                <td className="px-4 py-3 text-zinc-400">{r.analystCt} / {r.seasonalCt}</td>
                <td className={`px-4 py-3 font-medium ${(r.winRate ?? 0) >= 50 ? "text-green-400" : "text-red-400"}`}>
                  {r.profits} / {r.trades}
                  {r.winRate != null && <span className="text-xs ml-1 opacity-70">({r.winRate.toFixed(0)}%)</span>}
                </td>
                <td className="px-4 py-3 text-zinc-400">
                  {r.avgHold != null ? `${r.avgHold.toFixed(0)}d` : "—"}
                </td>
                <td className="px-4 py-3 text-zinc-100 font-medium">{fmtDollar(r.portfolioEnd)}</td>
                <td className={`px-4 py-3 font-medium ${r.annualRet == null ? "text-zinc-500" : r.annualRet >= 0 ? "text-green-400" : "text-red-400"}`}>
                  {fmt(r.annualRet)}
                </td>
                <td className="px-4 py-3 text-zinc-400">{fmt(r.spyRet)}</td>
                <td className={`px-4 py-3 font-medium ${r.alpha == null ? "text-zinc-500" : r.alpha >= 0 ? "text-green-400" : "text-red-400"}`}>
                  {fmt(r.alpha)}
                </td>
              </tr>
            ))}
          </tbody>
          {yearSummary.length > 0 && (() => {
            const totTrades = yearSummary.reduce((s, r) => s + r.trades, 0);
            const totProfit = yearSummary.reduce((s, r) => s + r.profits, 0);
            const totWR     = totTrades > 0 ? (totProfit / totTrades) * 100 : null;
            const last      = yearSummary.at(-1)!;
            const totalRet  = (last.portfolioEnd / 15_000 - 1) * 100;
            const avgSpy    = yearSummary.filter((r) => r.spyRet != null).reduce((s, r) => s + (r.spyRet ?? 0), 0)
                              / yearSummary.filter((r) => r.spyRet != null).length;
            return (
              <tfoot>
                <tr className="border-t-2 border-zinc-700 bg-zinc-800/40">
                  <td className="px-4 py-3 font-semibold text-zinc-100" colSpan={2}>{years.length}-Year Total</td>
                  <td className="px-4 py-3 text-zinc-400">
                    {yearSummary.reduce((s, r) => s + r.analystCt, 0)} / {yearSummary.reduce((s, r) => s + r.seasonalCt, 0)}
                  </td>
                  <td className={`px-4 py-3 font-semibold ${(totWR ?? 0) >= 50 ? "text-green-400" : "text-red-400"}`}>
                    {totProfit} / {totTrades}
                    {totWR != null && <span className="text-xs ml-1 opacity-70">({totWR.toFixed(0)}%)</span>}
                  </td>
                  <td className="px-4 py-3 text-zinc-400">—</td>
                  <td className="px-4 py-3 font-bold text-zinc-100">{fmtDollar(last.portfolioEnd)}</td>
                  <td className={`px-4 py-3 font-bold ${totalRet >= 0 ? "text-green-400" : "text-red-400"}`}>{fmt(totalRet)}</td>
                  <td className="px-4 py-3 text-zinc-400">{fmt(avgSpy)}</td>
                  <td className="px-4 py-3 text-zinc-400">—</td>
                </tr>
              </tfoot>
            );
          })()}
        </table>
      </div>

      {/* ── Trade log ─────────────────────────────────────────────────── */}
      <div>
        <div className="flex flex-wrap items-center gap-3 mb-3">
          <span className="text-sm font-medium text-zinc-300">Trade Log</span>

          {/* Year filter pills */}
          <div className="flex flex-wrap gap-1">
            <span
              className={`${PILL} ${yearFilter == null ? PILL_ON : PILL_OFF}`}
              onClick={() => { setYearFilter(null); setLogPage(0); }}
            >All</span>
            {years.map((y) => (
              <span
                key={y}
                className={`${PILL} ${yearFilter === y ? PILL_ON : PILL_OFF}`}
                onClick={() => { setYearFilter(yearFilter === y ? null : y); setLogPage(0); }}
              >{y}</span>
            ))}
          </div>

          {/* Signal / exit dropdowns */}
          <select className={selectCls} value={signalFilter} onChange={(e) => { setSignalFilter(e.target.value as typeof signalFilter); setLogPage(0); }}>
            <option value="all">All signals</option>
            <option value="analyst">Analyst only</option>
            <option value="seasonal">Seasonal only</option>
          </select>

          <select className={selectCls} value={exitFilter} onChange={(e) => { setExitFilter(e.target.value as typeof exitFilter); setLogPage(0); }}>
            <option value="all">All exits</option>
            <option value="profit">Profit exits</option>
            <option value="breakeven">Breakeven exits</option>
          </select>

          <span className="text-xs text-zinc-600">{logRows.length} trades</span>
        </div>

        <div className="overflow-x-auto rounded-lg border border-zinc-800">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-zinc-800 text-zinc-400 text-left">
                <th className="px-3 py-2.5 font-medium">Ticker</th>
                <th className="px-3 py-2.5 font-medium">Signal</th>
                <th className="px-3 py-2.5 font-medium">Entry</th>
                <th className="px-3 py-2.5 font-medium">Exit</th>
                <th className="px-3 py-2.5 font-medium">Hold</th>
                <th className="px-3 py-2.5 font-medium">Entry $</th>
                <th className="px-3 py-2.5 font-medium">Exit $</th>
                <th className="px-3 py-2.5 font-medium">Target</th>
                <th className="px-3 py-2.5 font-medium">Return</th>
                <th className="px-3 py-2.5 font-medium">Exit Type</th>
                <th className="px-3 py-2.5 font-medium">Portfolio After</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-800">
              {logPage_rows.map((r, i) => (
                <tr key={i} className="hover:bg-zinc-800/30 transition-colors">
                  <td className="px-3 py-2 font-semibold text-zinc-100">{r.ticker}</td>
                  <td className="px-3 py-2">
                    <span className={`text-xs px-1.5 py-0.5 rounded font-medium ${r.signal_type === "analyst" ? "bg-blue-950 text-blue-300" : "bg-amber-950 text-amber-300"}`}>
                      {r.signal_type}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-zinc-400 text-xs">{r.entry_date}</td>
                  <td className="px-3 py-2 text-zinc-400 text-xs">{r.exit_date}</td>
                  <td className="px-3 py-2 text-zinc-400">{r.hold_days}d</td>
                  <td className="px-3 py-2 text-zinc-300">${r.entry_price.toFixed(2)}</td>
                  <td className="px-3 py-2 text-zinc-300">${r.exit_price.toFixed(2)}</td>
                  <td className="px-3 py-2 text-zinc-400 text-xs">
                    {r.profit_target_pct != null ? `+${r.profit_target_pct.toFixed(0)}%` : "+5%"}
                  </td>
                  <td className={`px-3 py-2 font-medium ${r.pct_return >= 0 ? "text-green-400" : "text-red-400"}`}>
                    {fmt(r.pct_return, 2)}
                  </td>
                  <td className="px-3 py-2">
                    <span className={`text-xs px-1.5 py-0.5 rounded font-medium ${
                      r.exit_reason === "profit"    ? "bg-green-950 text-green-300" :
                      r.exit_reason === "breakeven" ? "bg-yellow-950 text-yellow-300" :
                                                      "bg-zinc-800 text-zinc-400"
                    }`}>
                      {r.exit_reason === "profit" ? "profit" : r.exit_reason === "breakeven" ? "breakeven" : "time stop"}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-zinc-300 font-medium">{fmtDollar(r.portfolio_value_after)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {totalPages > 1 && (
          <div className="flex items-center gap-2 mt-3 text-xs text-zinc-400">
            <button
              onClick={() => setLogPage((p) => Math.max(0, p - 1))}
              disabled={logPage === 0}
              className="px-2 py-1 rounded bg-zinc-800 hover:bg-zinc-700 disabled:opacity-30"
            >← Prev</button>
            <span>Page {logPage + 1} of {totalPages}</span>
            <button
              onClick={() => setLogPage((p) => Math.min(totalPages - 1, p + 1))}
              disabled={logPage === totalPages - 1}
              className="px-2 py-1 rounded bg-zinc-800 hover:bg-zinc-700 disabled:opacity-30"
            >Next →</button>
          </div>
        )}
      </div>

      {/* ── Notes ─────────────────────────────────────────────────────── */}
      <div className="rounded border border-zinc-800 bg-zinc-900/40 px-4 py-3 text-xs text-zinc-500 space-y-1">
        <p><span className="text-zinc-400 font-medium">Position sizing</span> — Available cash is split equally across all qualifying signals on a given day (up to 20 concurrent positions). No fixed % — cash is always fully deployed when signals exist.</p>
        <p><span className="text-zinc-400 font-medium">Exit rules</span> — Sell when daily close hits +5% from entry (profit exit). After 90 calendar days with no +5%, lower the target to breakeven: hold until price returns to entry, then sell (breakeven exit). Year-end forced closures excluded from win-rate metric.</p>
        <p><span className="text-zinc-400 font-medium">Signal sources</span> — Analyst: Top Tier firm upgrades (win_rate &gt; 50%, avg_ret_6m &gt; 0%, ≥2 signals on pre-year data). Seasonal: ticker-month combos with historical avg_return ≥ 1.5% and win_rate ≥ 55% (≥3 years observed). Both filtered to expected return ≥ 1.5%.</p>
        <p><span className="text-zinc-400 font-medium">Walk-forward</span> — Training always uses data strictly before the test year. No lookahead bias.</p>
        <p><span className="text-zinc-400 font-medium">SPY comparison</span> — Hypothetical $15,000 invested in SPY at start of 2015, compounded annually at each year&apos;s SPY return.</p>
      </div>

    </div>
  );
}
