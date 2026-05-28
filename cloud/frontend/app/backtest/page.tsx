import { apiFetch } from "@/lib/api";
import BacktestClient from "@/components/BacktestClient";

export const revalidate = 86400;

export default async function BacktestPage() {
  let data: object[] = [];
  let error: string | null = null;

  try {
    data = await apiFetch<object[]>("/backtest-v2");
  } catch (e) {
    error = String(e);
  }

  return (
    <div>
      <div className="mb-6">
        <h1 className="text-xl font-bold text-zinc-100">Backtest</h1>
        <p className="text-sm text-zinc-500 mt-1">
          Walk-forward 2015–2025. Train on pre-year data, test on Top Tier firm signals only.
          Select hold period and leverage to explore all combinations.
        </p>
      </div>

      {error && (
        <div className="rounded border border-red-800 bg-red-950/40 px-4 py-3 text-sm text-red-400 mb-6">
          {error}
        </div>
      )}

      {data.length === 0 && !error ? (
        <div className="text-sm text-zinc-500 py-8 text-center">
          No backtest data yet — pipeline will populate this on next run.
        </div>
      ) : (
        <BacktestClient data={data as Parameters<typeof BacktestClient>[0]["data"]} />
      )}
    </div>
  );
}
