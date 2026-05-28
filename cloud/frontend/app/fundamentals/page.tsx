import { apiFetch } from "@/lib/api";
import FundamentalsClient, { type FundRow } from "@/components/FundamentalsClient";

export const revalidate = 300;

export default async function FundamentalsPage() {
  const rows = await apiFetch<FundRow[]>("/fundamentals").catch(() => [] as FundRow[]);
  return <FundamentalsClient rows={rows} />;
}
