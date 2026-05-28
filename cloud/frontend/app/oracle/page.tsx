import { Suspense } from "react";
import { apiFetch } from "@/lib/api";
import OracleClient from "@/components/OracleClient";

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

export default async function OraclePage() {
  const [tickers, allTargets] = await Promise.all([
    apiFetch<{ ticker: string; stock_name: string }[]>("/tickers"),
    apiFetch<PriceTarget[]>("/price-targets").catch(() => [] as PriceTarget[]),
  ]);

  return (
    <Suspense>
      <OracleClient
        tickers={tickers}
        allTargets={allTargets}
      />
    </Suspense>
  );
}
