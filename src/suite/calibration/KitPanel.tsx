import { useEffect, useId, useRef, useState } from "react";
import { JobMessage } from "../JobMessage";
import { checkFolder, checkPaths } from "../paths";
import { EVIDENCE_MEANING, evidenceLabel, evidenceWord } from "../verdict";
import { kitSize, OUTPUT_FOLDER_TEXT, playerName } from "./messages";
import { isPrediction, own } from "./types";
import type { useCalibration } from "./useCalibration";

export function KitPanel({ calibration, disabledReason = null }: {
  calibration: Pick<ReturnType<typeof useCalibration>, "plan" | "build" | "planDocument" | "buildDocument" | "planJob" | "buildJob" | "error">;
  disabledReason?: string | null;
}) {
  const id = useId();
  const [text, setText] = useState("");
  const [out, setOut] = useState("");
  const [deep, setDeep] = useState(false);
  const [starting, setStarting] = useState(false);
  const [problems, setProblems] = useState<string[]>([]);
  const [plannedInput, setPlannedInput] = useState<string | null>(null);
  const startingRef = useRef(false);
  const mounted = useRef(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const { planJob, buildJob, buildDocument } = calibration;
  const busy = starting || planJob.status === "running" || buildJob.status === "running";
  const inputKey = JSON.stringify([text, deep]);
  const plan = plannedInput === inputKey ? calibration.planDocument : null;
  const run = async (kind: "plan" | "build") => {
    if (startingRef.current || busy || disabledReason !== null || (kind === "build" && !plan)) return;
    // An empty list is valid: the kit can consist entirely of invented/manual tests.
    const files = text.trim() ? checkPaths(text) : { paths: [], problems: [] };
    const folder = checkFolder(out);
    const invalid = [...files.problems, ...(kind === "build" && folder.problem ? [folder.problem] : [])];
    setProblems(invalid);
    if (invalid.length || (kind === "build" && folder.folder === null)) return;
    startingRef.current = true;
    setStarting(true);
    if (kind === "plan") setPlannedInput(inputKey);
    try {
      const request = { paths: [...files.paths], deep_dv: deep };
      if (kind === "plan") await calibration.plan(request);
      else await calibration.build({ ...request, out: folder.folder! });
    } finally {
      startingRef.current = false;
      if (mounted.current) setStarting(false);
    }
  };
  const estimated = plan?.tests.reduce((sum, t) => sum + (t.source.est_bytes ?? 0), 0) ?? 0;
  const unknownSize = plan?.tests.some(t => !t.manual && t.source.status === "ready" && t.source.est_bytes === null);
  return <section className="suite-section" aria-label="Prepare calibration kit">
    <h3>Prepare calibration kit</h3>
    <p className="suite-muted">The browser cannot read files from your disk here: paste full paths (Explorer: Shift + right-click, Copy as path). Leave the list empty to use tests that need no source file.</p>
    <label htmlFor={`${id}-paths`}>Video files on this computer (one full path per line, up to 50)</label>
    <textarea id={`${id}-paths`} rows={8} spellCheck={false} value={text} disabled={busy || disabledReason !== null} onChange={e => { setText(e.target.value); setProblems([]); }} />
    <label htmlFor={`${id}-out`}>Kit output folder</label>
    <input id={`${id}-out`} value={out} disabled={busy || disabledReason !== null} onChange={e => { setOut(e.target.value); setProblems([]); }} />
    <p className="suite-muted">{OUTPUT_FOLDER_TEXT} VideoSuite checks that it is separate from source folders and repository folders, has no symlink or junction ancestors, and will not replace existing kit files.</p>
    <label htmlFor={`${id}-deep`}><input id={`${id}-deep`} type="checkbox" checked={deep} disabled={busy || disabledReason !== null} onChange={e => setDeep(e.target.checked)} />Deep Dolby Vision scan (verifies FEL/MEL for this run)</label>
    {disabledReason && <p>{disabledReason}</p>}
    {problems.length > 0 && <ul className="suite-problems">{problems.map(p => <li key={p}>{p}</li>)}</ul>}
    <button type="button" disabled={busy || disabledReason !== null} onClick={() => void run("plan")}>Plan kit</button>
    <button type="button" disabled={busy || disabledReason !== null || !plan} onClick={() => void run("build")}>Prepare kit</button>
    {starting && <p>Starting…</p>}
    {calibration.error && <p role="alert">{calibration.error}{/^Not found(?::|\.)/.test(calibration.error) && " — connect the drive that holds these files, then try again."}</p>}
    <JobMessage label="Kit plan" job={planJob} />
    {planJob.status === "running" && <button type="button" disabled={planJob.cancelRequested} onClick={() => void planJob.cancel()}>Cancel plan</button>}
    {plan && <>
      <p>Estimated total: {kitSize(estimated)}{unknownSize ? " plus files of unknown size" : ""}. VideoSuite plans again when building; the final size may differ.</p>
      <table className="suite-table" aria-label="Kit plan"><thead><tr><th scope="col">Test</th><th scope="col">Title</th><th scope="col">Source</th><th scope="col">Prediction</th></tr></thead>
        <tbody>{plan.tests.map(t => <tr key={t.test}>
          <td>{t.test}</td><td>{t.title}</td><td>{t.manual ? "manual" : t.source.status === "needs_real_file" ? "needs a real file" : t.source.status}{t.source.file && <p>{t.source.file}</p>}{t.source.reason && <p>{t.source.reason}</p>}</td>
          <td>{t.players.map(player => {
            const prediction = own(t.prediction, player);
            return <p key={player}>{playerName(player)}: {isPrediction(prediction) ? <><span>{prediction.text}</span>{" "}<span className="suite-evidence" title={EVIDENCE_MEANING[evidenceWord(prediction.evidence)]}>{evidenceLabel(prediction.evidence)}</span></> : <><span>Unknown</span>{" "}<span className="suite-evidence">{evidenceLabel("unknown")}</span></>}</p>;
          })}</td>
        </tr>)}</tbody>
      </table>
    </>}
    <JobMessage label="Kit build" job={buildJob} />
    {buildJob.status === "running" && <button type="button" disabled={buildJob.cancelRequested} onClick={() => void buildJob.cancel()}>Cancel build</button>}
    {(buildJob.cancelRequested || buildJob.status === "cancelled") && <p>On Windows, cancelling stops the process tree. The staging folder and any already-published files stay until the next build into this output folder cleans them.</p>}
    {[{ name: "Plan", job: planJob }, { name: "Build", job: buildJob }].map(({ name, job }) => job.events.length > 0 && <details className="suite-log" key={name}><summary>{name}: last 50 retained CLI lines</summary><pre>{job.events.slice(-50).map(e => e.msg).join("\n")}</pre></details>)}
    {buildDocument && <>
      <p>Final kit size: {kitSize(buildDocument.total_bytes)}. Kit: {buildDocument.kit_id}</p>
      <p>Kit descriptor: {buildDocument.descriptor_path}</p>
      {buildDocument.unavailable.length > 0 && <ul>{buildDocument.unavailable.map((t, i) => <li key={i}>{t.test}: unavailable — {t.reason}</li>)}</ul>}
    </>}
  </section>;
}
