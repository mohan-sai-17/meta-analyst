import { apiFetch } from "@/lib/api";
import MLClient from "@/components/MLClient";

export const revalidate = 3600;

export default async function MLSignalsPage() {
  let summary: object[] = [];
  let live: object[]    = [];
  let error: string | null = null;

  try {
    [summary, live] = await Promise.all([
      apiFetch<object[]>("/ml-signals"),
      apiFetch<object[]>("/ml-live"),
    ]);
  } catch (e) {
    error = String(e);
  }

  return (
    <div>
      <div className="mb-6">
        <h1 className="text-xl font-bold text-zinc-100">ML Signal Filter</h1>
        <p className="text-sm text-zinc-500 mt-1">
          A GradientBoosting classifier trained walk-forward on signal outcomes predicts
          which analyst upgrades will hit +5% within 90 days. Features: firm score,
          stock momentum, market regime, sector, seasonality.
          Only signals above 55% confidence are taken.
        </p>
      </div>

      {error && (
        <div className="rounded border border-red-800 bg-red-950/40 px-4 py-3 text-sm text-red-400 mb-6">
          {error}
        </div>
      )}

      {summary.length === 0 && !error ? (
        <div className="text-sm text-zinc-500 py-8 text-center">
          No data yet — run <code className="text-zinc-400">backtest_ml.py</code> to populate.
        </div>
      ) : (
        <MLClient
          summary={summary as Parameters<typeof MLClient>[0]["summary"]}
          live={live    as Parameters<typeof MLClient>[0]["live"]}
        />
      )}
    </div>
  );
}
