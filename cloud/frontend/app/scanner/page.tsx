import { apiFetch } from "@/lib/api";
import ScannerClient, { type Signal } from "@/components/ScannerClient";

export const revalidate = 300;

export default async function ScannerPage() {
  const [signals, tickers] = await Promise.all([
    apiFetch<Omit<Signal, "Stock_Name">[]>("/signals").catch(() => [] as Omit<Signal, "Stock_Name">[]),
    apiFetch<{ ticker: string; stock_name: string }[]>("/tickers").catch(() => []),
  ]);

  const nameMap = Object.fromEntries(tickers.map(t => [t.ticker, t.stock_name]));
  const enriched: Signal[] = signals.map(s => ({ ...s, Stock_Name: nameMap[s.Ticker] ?? "" }));

  return <ScannerClient signals={enriched} />;
}
