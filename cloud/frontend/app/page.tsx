import Link from "next/link";
import { apiFetch } from "@/lib/api";
import SignalCard from "@/components/SignalCard";
import type { Candidate } from "@/components/StockModal";

export const revalidate = 300;

export default async function SignalsPage() {
  let candidates: Candidate[] = [];
  let error: string | null = null;

  try {
    candidates = await apiFetch<Candidate[]>("/candidates");
  } catch (e) {
    error = String(e);
  }

  const highConf = candidates.filter(
    (c) => !c.is_biased && c.season_win_rate >= 50 && c.season_avg_return > 0 && !c.near_earnings
        && c.passes_health_screen !== false   // null = data pending = don't penalize
  );

  return (
    <div>
      <div className="flex items-baseline justify-between mb-6">
        <h1 className="text-xl font-bold text-zinc-100">Today&apos;s Buy Signals</h1>
        <span className="text-sm text-zinc-500">
          {candidates.length} signal{candidates.length !== 1 ? "s" : ""} ·{" "}
          <span className="text-green-400">{highConf.length} high-confidence</span>
        </span>
      </div>

      {error && (
        <div className="rounded border border-red-800 bg-red-950/40 px-4 py-3 text-sm text-red-400 mb-6">
          {error}
        </div>
      )}

      {candidates.length === 0 && !error && (
        <div className="flex flex-col items-center justify-center text-center py-16 gap-4">
          <h2 className="text-lg font-bold text-zinc-900 dark:text-zinc-100">No new signals today</h2>
          <p className="text-sm text-zinc-500 max-w-sm">
            The pipeline runs daily at 2pm UTC on weekdays. Check back tomorrow or browse recent activity in the Scanner tab.
          </p>
          <Link
            href="/scanner"
            className="mt-2 px-5 py-2.5 rounded bg-zinc-900 dark:bg-zinc-100 text-white dark:text-zinc-900 text-sm font-semibold hover:bg-zinc-700 dark:hover:bg-zinc-300 transition-colors"
          >
            View Recent Signals &rarr;
          </Link>
        </div>
      )}

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
        {candidates.map((c) => (
          <SignalCard key={`${c.Ticker}-${c.Firm}-${c.GradeDate}`} candidate={c} />
        ))}
      </div>
    </div>
  );
}
