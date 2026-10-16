import { EVIDENCE_MEANING, evidenceLabel, evidenceWord, unverifiedNotice, verdictLabel } from "./verdict";

export function VerdictBadge({ verdict, evidence }: { verdict: string; evidence: string | null | undefined }) {
  const notice = unverifiedNotice(evidence);
  return <span className="suite-verdict-badge"><span className="suite-verdict" data-verdict={verdict}>{verdictLabel(verdict)}</span>{" "}
    <span className="suite-evidence" title={EVIDENCE_MEANING[evidenceWord(evidence)]}>{evidenceLabel(evidence)}</span>
    {notice !== null && <span className="suite-notice" role="note">{notice}</span>}
  </span>;
}
