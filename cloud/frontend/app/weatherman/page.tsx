import { apiFetch } from "@/lib/api";
import WeathermanClient from "@/components/WeathermanClient";

export const revalidate = 3600;

export default async function WeathermanPage() {
  const tickers = await apiFetch<{ ticker: string; stock_name: string }[]>("/tickers");
  return <WeathermanClient tickers={tickers} />;
}
