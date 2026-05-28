import { apiFetch } from "@/lib/api";
import WFOClient from "@/components/WFOClient";

export const revalidate = 3600;

export default async function WFOPage() {
  let data: object[] = [];
  let error: string | null = null;

  try {
    data = await apiFetch<object[]>("/wfo");
  } catch (e) {
    error = String(e);
  }

  return (
    <div>
      <div className="mb-6">
        <h1 className="text-xl font-bold text-zinc-100">Walk-Forward Optimizer</h1>
        <p className="text-sm text-zinc-500 mt-1">
          Each year, the best (take_profit %, hold_days) are chosen from a 4×4 grid using
          only prior-year data. Out-of-sample test results are shown alongside the fixed
          5% / 90d baseline. Does adapting parameters beat the fixed strategy?
        </p>
      </div>

      {error && (
        <div className="rounded border border-red-800 bg-red-950/40 px-4 py-3 text-sm text-red-400 mb-6">
          {error}
        </div>
      )}

      {data.length === 0 && !error ? (
        <div className="text-sm text-zinc-500 py-8 text-center">
          No data yet — run <code className="text-zinc-400">backtest_wfo.py</code> to populate.
        </div>
      ) : (
        <WFOClient data={data as Parameters<typeof WFOClient>[0]["data"]} />
      )}
    </div>
  );
}
