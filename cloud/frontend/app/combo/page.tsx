import { apiFetch } from "@/lib/api";
import ComboClient from "@/components/ComboClient";

export const revalidate = 3600;

export default async function ComboPage() {
  let summary: object[] = [];
  let perf: object[]    = [];
  let live: object[]    = [];
  let error: string | null = null;

  try {
    [summary, perf, live] = await Promise.all([
      apiFetch<object[]>("/combo-summary"),
      apiFetch<object[]>("/combo-perf"),
      apiFetch<object[]>("/combo-live"),
    ]);
  } catch (e) {
    error = String(e);
  }

  return (
    <div>
      <div className="mb-6">
        <h1 className="text-xl font-bold text-zinc-100">Combination Memory</h1>
        <p className="text-sm text-zinc-500 mt-1">
          Learns which <span className="text-zinc-300">(analyst firm × sector × month)</span> patterns
          are historically reliable. If Goldman upgraded Tech in March and it worked —
          next year that exact combo is trusted. Fallback chain: exact combo → firm+sector → firm alone.
          Minimum 5 historical signals and 55% hit rate to take a signal.
        </p>
      </div>

      {error && (
        <div className="rounded border border-red-800 bg-red-950/40 px-4 py-3 text-sm text-red-400 mb-6">
          {error}
        </div>
      )}

      {summary.length === 0 && !error ? (
        <div className="text-sm text-zinc-500 py-8 text-center">
          No data yet — run <code className="text-zinc-400">backtest_combo.py</code> to populate.
        </div>
      ) : (
        <ComboClient
          summary={summary as Parameters<typeof ComboClient>[0]["summary"]}
          perf={perf       as Parameters<typeof ComboClient>[0]["perf"]}
          live={live       as Parameters<typeof ComboClient>[0]["live"]}
        />
      )}
    </div>
  );
}
