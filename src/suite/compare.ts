import type { CompareDocument, DeviceOptions, RankDocument } from "./types";
export const MAX_EVENTS_SHOWN = 50;
export function isCompareDocument(v: unknown): v is CompareDocument {
  return v !== null && typeof v === "object" && Array.isArray((v as CompareDocument).pairs) && typeof (v as CompareDocument).failed === "number";
}
export function isRankDocument(v: unknown): v is RankDocument {
  return v !== null && typeof v === "object" && Array.isArray((v as RankDocument).quality) && (v as RankDocument).devices !== null && typeof (v as RankDocument).devices === "object" && !Array.isArray((v as RankDocument).devices);
}
export function shouldRank(view: { status: string; errorKind: string | null; results: unknown }): boolean {
  return isCompareDocument(view.results) && (view.status === "done" || (view.status === "error" && view.errorKind === "failed_pairs"));
}
export interface LastRun { paths: string[]; outDir: string; mode: "fast" | "accurate"; top: number; options: DeviceOptions; }
