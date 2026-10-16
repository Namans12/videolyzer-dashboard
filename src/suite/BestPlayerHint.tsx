import type { BestPlayer } from "./types";
import { bestPlayerHeadline } from "./verdict";
import { VerdictBadge } from "./VerdictBadge";

export function BestPlayerHint({ best, evidence }: { best: BestPlayer; evidence: string | null }) {
  return <div className="suite-best-player"><p><strong>{bestPlayerHeadline(best)}</strong>{" "}<VerdictBadge verdict={best.verdict} evidence={evidence} /></p><p className="suite-muted">{best.reason}</p></div>;
}
