"use client";
import { useState } from "react";
import dynamic from "next/dynamic";
import type { Candidate } from "@/components/StockModal";

const StockModal = dynamic(() => import("@/components/StockModal"), { ssr: false });

function upside(target: number, current: number) {
  return ((target / current - 1) * 100).toFixed(1);
}

const ACTION_LABEL: Record<string, string> = {
  up:   "Upgrade",
  init: "Initiate",
  down: "Downgrade",
  main: "Maintain",
  reit: "Reiterate",
};

export default function SignalCard({ candidate: c }: { candidate: Candidate }) {
  const [open, setOpen] = useState(false);
  const highConf = !c.is_biased && c.season_win_rate >= 50 && c.season_avg_return > 0 && !c.near_earnings && c.passes_health_screen !== false;
  const up3m = upside(c.target_3m, c.current_price);
  const up6m = upside(c.target_6m, c.current_price);

  return (
    <>
      <div
        onClick={() => setOpen(true)}
        className={`rounded-lg border p-4 flex flex-col gap-3 cursor-pointer transition-all hover:scale-[1.02] hover:shadow-lg ${
          highConf
            ? "border-green-700 bg-green-950/20 hover:border-green-500"
            : "border-zinc-800 bg-zinc-900/40 hover:border-zinc-600"
        }`}
      >
        {/* Header */}
        <div className="flex items-start justify-between">
          <div>
            <span className="text-lg font-bold text-zinc-100">{c.Ticker}</span>
            <span className="ml-2 text-xs text-zinc-500">{c.Firm}</span>
          </div>
          <div className="flex flex-col items-end gap-1">
            {highConf && (
              <span className="text-[10px] font-bold bg-green-700 text-green-100 px-1.5 py-0.5 rounded">
                HIGH CONF
              </span>
            )}
            {c.is_biased && (
              <span className="text-[10px] font-bold bg-red-900 text-red-300 px-1.5 py-0.5 rounded">
                BIASED
              </span>
            )}
            {c.near_earnings && (
              <span className="text-[10px] font-bold bg-amber-900 text-amber-300 px-1.5 py-0.5 rounded">
                EARNINGS{c.days_to_earnings != null ? ` ${c.days_to_earnings}d` : ""}
              </span>
            )}
            {c.passes_health_screen === false && (
              <span className="text-[10px] font-bold bg-red-950 text-red-400 border border-red-800 px-1.5 py-0.5 rounded">
                HEALTH FAIL
              </span>
            )}
            {(c.insider_strength === "strong_buy" || c.insider_strength === "mild_buy") && (
              <span className="text-[10px] font-bold bg-blue-950 text-blue-300 border border-blue-800 px-1.5 py-0.5 rounded">
                INSIDER BUY
              </span>
            )}
          </div>
        </div>

        {/* Action */}
        <div className="text-xs text-zinc-400">
          <span className="text-zinc-300">{ACTION_LABEL[c.Action] ?? c.Action}</span>
          {" → "}
          <span className="text-green-400 font-semibold">{c.ToGrade}</span>
          <span className="ml-2 text-zinc-600">{String(c.GradeDate).slice(0, 10)}</span>
        </div>

        {/* Price targets */}
        <div className="grid grid-cols-2 gap-2 text-xs">
          <div className="bg-zinc-800/60 rounded px-2 py-1.5">
            <div className="text-zinc-500 mb-0.5">3M Target</div>
            <div className="text-zinc-100 font-semibold">${c.target_3m.toFixed(2)}</div>
            <div className="text-green-400">+{up3m}%</div>
          </div>
          <div className="bg-zinc-800/60 rounded px-2 py-1.5">
            <div className="text-zinc-500 mb-0.5">6M Target</div>
            <div className="text-zinc-100 font-semibold">${c.target_6m.toFixed(2)}</div>
            <div className="text-green-400">+{up6m}%</div>
          </div>
        </div>

        {/* Seasonality + hint */}
        <div className="flex items-center justify-between text-xs text-zinc-500 border-t border-zinc-800 pt-2">
          <span>Win rate <span className="text-zinc-300">{c.season_win_rate.toFixed(0)}%</span></span>
          <span>Avg ret <span className="text-zinc-300">{c.season_avg_return.toFixed(1)}%</span></span>
          <span>Vol <span className="text-zinc-300">{(c.hist_vol * 100).toFixed(0)}%</span></span>
        </div>
      </div>

      {open && <StockModal candidate={c} onClose={() => setOpen(false)} />}
    </>
  );
}
