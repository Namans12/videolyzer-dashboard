import { Fragment, useEffect, useId, useRef, useState } from "react";
import { EvidenceLegend } from "../EvidenceLegend";
import { JobMessage } from "../JobMessage";
import { VerdictBadge } from "../VerdictBadge";
import { checkPaths } from "../paths";
import { KIT_NOT_ACTIVE_TEXT, playerName, RECOVERY_REQUIRED_TEXT, ruleSummary } from "./messages";
import type { ApplyDocument, ReportSide } from "./types";
import type { useCalibration } from "./useCalibration";

type Calibration = Pick<ReturnType<typeof useCalibration>, "status" | "loading" | "mutationBusy" | "pendingAnswers" | "error" |
  "preview" | "applyResult" | "loadPreview" | "apply" | "undo" | "report" | "reportJob" | "reportDocument">;

export function ApplyPanel({ calibration, disabledReason = null }: { calibration: Calibration; disabledReason?: string | null }) {
  return <ApplyForm key={calibration.status?.kit?.id ?? "no-kit"} calibration={calibration} disabledReason={disabledReason} />;
}

function ApplyForm({ calibration, disabledReason }: { calibration: Calibration; disabledReason: string | null }) {
  const id = useId();
  const [paths, setPaths] = useState("");
  const [problems, setProblems] = useState<string[]>([]);
  const [starting, setStarting] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [resultKind, setResultKind] = useState<"apply" | "undo" | null>(null);
  const { preview, applyResult, reportJob, reportDocument } = calibration;
  // Confirmations belong to this exact preview, including a reload of the same revision.
  const [vetoes, setVetoes] = useState<{ document: ApplyDocument | null; checked: string[]; confirmed: string[] }>({ document: preview, checked: [], confirmed: [] });
  if (vetoes.document !== preview) setVetoes({ document: preview, checked: [], confirmed: [] });
  const checked = vetoes.document === preview ? vetoes.checked : [];
  const confirmed = vetoes.document === preview ? vetoes.confirmed : [];
  const inFlight = useRef(false);
  const mounted = useRef(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const busy = starting || calibration.loading || calibration.mutationBusy || calibration.pendingAnswers.length > 0;
  const undoBlocked = busy || !calibration.status?.kit;
  const blocked = undoBlocked || disabledReason !== null;
  const recovery = !!(preview?.recovery_required || applyResult?.recovery_required || calibration.status?.recovery_required);
  const canApply = !blocked && !!preview && !preview.recovery_required;
  const run = async (kind: "preview" | "apply" | "undo" | "report") => {
    if (inFlight.current || (kind === "undo" ? undoBlocked : blocked) || (kind === "apply" && !canApply) || (kind === "report" && reportJob.status === "running")) return;
    const files = kind === "report" ? checkPaths(paths) : null;
    setProblems(files?.problems ?? []);
    if (files?.problems.length) return;
    inFlight.current = true; setStarting(true); setNotice(null);
    if (kind === "apply" || kind === "undo") setResultKind(kind);
    try {
      if (kind === "preview") await calibration.loadPreview();
      if (kind === "undo") await calibration.undo(calibration.status!.kit!.id);
      if (kind === "report") await calibration.report({ paths: [...files!.paths] });
      if (kind === "apply") {
        // Only ids explicitly confirmed on this preview travel to the server.
        const ids = preview!.changes.filter(c => c.veto && confirmed.includes(c.id)).map(c => c.id);
        const ok = await calibration.apply([...new Set(ids)]);
        if (!ok && mounted.current) {
          setNotice("Apply was refused. The preview may be out of date; review the refreshed preview and confirm each veto again before applying.");
          // The hook exposes failures as plain sentences. Refresh after any refusal,
          // covering stale revisions without depending on server error wording.
          await calibration.loadPreview();
        }
      }
    } finally { inFlight.current = false; if (mounted.current) setStarting(false); }
  };
  const toggle = (changeId: string, value: boolean) => setVetoes({ document: preview,
    checked: value ? [...checked, changeId] : checked.filter(v => v !== changeId),
    confirmed: confirmed.filter(v => v !== changeId) });
  return <section className="suite-section" aria-label="Apply calibration">
    <h3>Apply calibration</h3>
    <p>Preview the recorded answers before changing owner settings. Unsure, informational and skipped answers do not become owner evidence.</p>
    <p>A veto means silent or unsupported playback. Check and confirm each veto separately; unconfirmed vetoes are skipped.</p>
    {disabledReason && <p>{disabledReason}</p>}
    {recovery && <p>{RECOVERY_REQUIRED_TEXT}</p>}
    <button type="button" disabled={blocked} onClick={() => void run("preview")}>Preview changes</button>
    <button type="button" disabled={!canApply} onClick={() => void run("apply")}>Apply</button>
    <button type="button" disabled={undoBlocked} onClick={() => void run("undo")}>Undo this kit</button>
    {!preview && <p>Load a fresh preview to enable Apply.</p>}
    {starting && <p role="status">Working…</p>}
    {notice && <p role="status">{notice}</p>}
    {calibration.error && <p role="alert">{calibration.error}</p>}
    {preview && <>
      <p>Preview revision: {preview.revision}</p>
      <ChangesTable document={preview} label="Apply preview" />
      {preview.changes.filter(c => c.veto).map(c => <div key={c.id}>
        <label><input type="checkbox" checked={checked.includes(c.id)} disabled={blocked || preview.recovery_required}
          onChange={e => toggle(c.id, e.target.checked)} />I checked the veto for {c.test} ({playerName(c.player)})</label>
        <button type="button" disabled={blocked || preview.recovery_required || !checked.includes(c.id) || confirmed.includes(c.id)}
          onClick={() => setVetoes({ document: preview, checked, confirmed: [...confirmed, c.id] })}>Confirm veto for {c.test} ({playerName(c.player)})</button>
        {confirmed.includes(c.id) && <p>Veto confirmed for {c.test} ({playerName(c.player)}) on this preview.</p>}
      </div>)}
    </>}
    {applyResult && <>
      <p>{applyResult.recovery_required ? "The operation needs recovery." : applyResult.applied ? resultKind === "undo" ? "Kit changes removed." : "Changes applied." : "Operation result; review the entries below."}</p>
      <ChangesTable document={applyResult} label="Apply result" />
    </>}
    <h4>What changed</h4>
    <p>playback only, not a ranking</p>
    <p className="suite-muted">Compare playback before and after this kit. Paste full local video paths, one per line, up to 50.</p>
    <label htmlFor={`${id}-paths`}>Files for playback report</label>
    <textarea id={`${id}-paths`} rows={5} spellCheck={false} value={paths} disabled={blocked || reportJob.status === "running"}
      onChange={e => { setPaths(e.target.value); setProblems([]); }} />
    {problems.length > 0 && <ul className="suite-problems">{problems.map(p => <li key={p}>{p}</li>)}</ul>}
    <button type="button" disabled={blocked || reportJob.status === "running"} onClick={() => void run("report")}>What changed</button>
    <JobMessage label="Playback report" job={reportJob} />
    {reportJob.status === "running" && <button type="button" disabled={reportJob.cancelRequested} onClick={() => void reportJob.cancel()}>Cancel report</button>}
    {reportDocument?.recovery_required && <p>{RECOVERY_REQUIRED_TEXT}</p>}
    {reportDocument && !reportDocument.kit_active && <p>This kit is no longer active. {KIT_NOT_ACTIVE_TEXT}</p>}
    {reportDocument && reportDocument.kit_active && !reportDocument.recovery_required && <>
      {reportDocument.files.map((file, index) => <article className="suite-record" key={index}>
        <h5>{file.path} — {playerName(file.player.split("/").at(-1)!)}</h5>
        <p>{file.changed ? "Playback changed" : "Playback unchanged"}</p>
        <table className="suite-table" aria-label={`Playback before and after: ${file.path} (${playerName(file.player.split("/").at(-1)!)})`}>
          <thead><tr><th scope="col">Before</th><th scope="col">After</th></tr></thead>
          <tbody><tr><td><PlaybackSide side={file.before} /></td><td><PlaybackSide side={file.after} /></td></tr></tbody>
        </table>
      </article>)}
      {reportDocument.files.length === 0 && <p>No playback files in this report.</p>}
      {reportDocument.best_player_changes.map((file, index) => <div className="suite-best-player" key={index}>
        <p>{file.path} ({file.device})</p>
        <p>Best player before: {file.before.label} ({playerName(file.before.player)}) — {file.before.reason}{" "}
          <VerdictBadge verdict={file.before.verdict}
            evidence={reportDocument.files.find(row => row.path === file.path && row.player === `${file.device}/${file.before.player}`)?.before.evidence} /></p>
        <p>Best player after: {file.after.label} ({playerName(file.after.player)}) — {file.after.reason}{" "}
          <VerdictBadge verdict={file.after.verdict}
            evidence={reportDocument.files.find(row => row.path === file.path && row.player === `${file.device}/${file.after.player}`)?.after.evidence} /></p>
      </div>)}
      <EvidenceLegend />
    </>}
  </section>;
}

function PlaybackSide({ side }: { side: ReportSide }) {
  return <><VerdictBadge verdict={side.verdict} evidence={side.evidence} /><p>Playback score: {side.score}</p>
    <p>Audio: {side.effective_audio ?? "unknown"}</p><p>HDR: {side.effective_hdr ?? "unknown"}</p></>;
}

function ChangesTable({ document, label }: { document: ApplyDocument; label: string }) {
  // The companion skipped array repeats skips already represented by change rows.
  const skipped = document.skipped.filter(s => !document.changes.some(c => c.test === s.test && c.player === s.player));
  return <>
    <table className="suite-table" aria-label={label}>
      <thead><tr><th scope="col">Test / player / rule</th><th scope="col">Action</th><th scope="col">Reason</th><th scope="col">Previous</th><th scope="col">Recorded value</th></tr></thead>
      <tbody>{document.changes.map((c, i) => <Fragment key={i}>
        <tr>
          <td>{c.test} / {playerName(c.player)} / {c.rule ?? "no rule"}</td><td>{c.action}</td><td>{c.reason}</td>
          <td>{ruleSummary(c.rule, c.previous)}</td><td>{ruleSummary(c.rule, c.value)}</td>
        </tr>
        <tr><td colSpan={5}><details><summary>Full rule details for {c.test} ({playerName(c.player)})</summary>
          <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{`Previous:\n${JSON.stringify(c.previous, null, 2)}\nRecorded value:\n${JSON.stringify(c.value, null, 2)}`}</pre>
        </details></td></tr>
      </Fragment>)}</tbody>
    </table>
    {document.changes.length === 0 && <p>No settings changes listed.</p>}
    {skipped.length > 0 && <ul>{skipped.map((s, i) => <li key={i}>{s.test} ({playerName(s.player)}): skip — {s.reason}</li>)}</ul>}
    {document.conflicts.map((c, i) => <p key={i}>Conflicting answers: {c.tests.join(", ")} ({playerName(c.player)}, {c.rule}) contradict each other. Neither applies; record one as skipped to resolve this.</p>)}
    {document.changes.some(c => c.action === "skip") || skipped.length > 0 ? <p className="suite-muted">Skipped answers do not change settings. A hand-written entry is kept. A duplicate answer is counted once; it adds no second change. Each row explains its reason.</p> : null}
  </>;
}
