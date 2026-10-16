import { CANCELLED_TEXT, JOB_GONE_TEXT } from "../jobStatus";
import { CANCELLING_TEXT, CANCEL_FAILED_TEXT, UNREACHABLE_TEXT, errorKindSentence } from "./messages";
import type { UseJobResult } from "./useJob";

export function JobMessage({ label, job }: { label: string; job: Pick<UseJobResult, "status" | "current" | "error" | "errorKind" | "errorDetail" | "unreachable" | "cancelRequested" | "cancelFailed"> }) {
  if (job.status === "running") return <div><p>{label}: running…</p>{job.current && <p className="suite-muted">{job.current}</p>}
    {job.cancelRequested && <p>{CANCELLING_TEXT}</p>}{job.cancelFailed && <p role="alert">{CANCEL_FAILED_TEXT}</p>}{job.unreachable && <p role="alert">{UNREACHABLE_TEXT}</p>}</div>;
  if (job.status === "cancelled") return <p>{CANCELLED_TEXT}</p>;
  if (job.status === "gone") return <p role="alert">{JOB_GONE_TEXT}</p>;
  if (job.status === "error") return <div className="suite-error" role="alert"><p>{errorKindSentence(job.errorKind)}</p>{job.error && <p className="suite-muted">{job.error}</p>}
    {job.errorDetail && <details className="suite-details"><summary>Details</summary><pre>{job.errorDetail}</pre></details>}</div>;
  return null;
}
