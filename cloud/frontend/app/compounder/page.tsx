import { apiFetch } from "@/lib/api";
import CompounderClient from "@/components/CompounderClient";

export const revalidate = 3600;

export default async function CompounderPage() {
  let data: object[] = [];
  let error: string | null = null;

  try {
    data = await apiFetch<object[]>("/backtest-reinvest");
  } catch (e) {
    error = String(e);
  }

  return (
    <div>
      <div className="mb-6">
        <h1 className="text-xl font-bold text-zinc-100">Compounder</h1>
        <p className="text-sm text-zinc-500 mt-1">
          Walk-forward 2015–2025 (backtest) + 2026 YTD live. 5% of portfolio per signal, up to 20 concurrent positions.
          Sell at +5% profit or after 90 days. Signals: Top Tier analyst upgrades + seasonal patterns.
        </p>
      </div>

      {error && (
        <div className="rounded border border-red-800 bg-red-950/40 px-4 py-3 text-sm text-red-400 mb-6">
          {error}
        </div>
      )}

      {data.length === 0 && !error ? (
        <div className="text-sm text-zinc-500 py-8 text-center">
          No data yet — run <code className="text-zinc-400">backtest_reinvest.py</code> to populate.
        </div>
      ) : (
        <CompounderClient data={data as Parameters<typeof CompounderClient>[0]["data"]} />
      )}
    </div>
  );
}
