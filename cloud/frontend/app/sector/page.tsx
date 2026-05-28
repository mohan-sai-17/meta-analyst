import { apiFetch } from "@/lib/api";
import SectorClient, { type Candidate } from "@/components/SectorClient";

export const revalidate = 300;

export default async function SectorPage() {
  const candidates = await apiFetch<Candidate[]>("/sector-candidates").catch(() => [] as Candidate[]);
  return <SectorClient candidates={candidates} />;
}
