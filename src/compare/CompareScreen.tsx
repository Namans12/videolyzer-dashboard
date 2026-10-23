import { useEffect, useRef, useState } from "react";
import { errorSentence, getDevices, getVersion } from "../suite/api";
import { estimateText } from "../suite/messages";
import type { DevicesDocument, VersionInfo } from "../suite/types";
import { InputPanel } from "./InputPanel";
import { RunOptions } from "./RunOptions";
import { ComparisonResult } from "./ComparisonResult";
import { useComparison } from "./useComparison";
import {
  ACTIVE_STAGES, AUDIO_LABELS, buildRunRequest, clearRunConditions, CLOSE_TAB_TEXT, defaultForm, defaultPlayerFor, DEPTH_LABELS, deviceLabel, formProblem,
  FS_LABELS, playerLabel, RESULTS_LABELS, STAGE_TEXT, unifiedSupport,
} from "./types";
import type { CompareForm, RunRequest, RunSnapshot } from "./types";
import "./compare.css";

function FrozenChoices({ run, devices }: { run: RunSnapshot; devices: DevicesDocument | null }) {
  const r = run.request;
  const count = r.input_kind === "files" ? r.paths?.length ?? 0 : r.magnets?.length ?? 0;
  return <section className="compare-section compare-frozen" aria-labelledby="compare-frozen-title">
    <h2 id="compare-frozen-title">Choices used for this run</h2>
    <dl className="compare-facts">
      <dt>Compared</dt><dd>{count} {r.input_kind === "files" ? "downloaded file(s)" : "magnet link(s)"}</dd>
      <dt>Results</dt><dd>{RESULTS_LABELS[r.results]}</dd>
      <dt>Depth</dt><dd>{DEPTH_LABELS[r.depth]}</dd>
      {r.results === "overall"
        ? <><dt>Device</dt><dd>None (Best overall uses no device rules)</dd></>
        : <>
          <dt>Device</dt><dd>{deviceLabel(r.device)}</dd>
          <dt>Player</dt>
          <dd>{playerLabel(devices, r.device, r.primary_player)}{r.include_alternatives
            ? ", with the other players as alternatives" : ", this player only"}</dd>
          <dt>USB file system</dt><dd>{r.fs ? FS_LABELS[r.fs] : "your saved setting (unknown if not set)"}</dd>
          <dt>Audio output</dt><dd>{r.audio_output ? AUDIO_LABELS[r.audio_output] : "your saved setting (unknown if not set)"}</dd>
        </>}
      <dt>Language</dt><dd>{r.lang}</dd>
    </dl>
  </section>;
}

function candidateName(c: RunSnapshot["candidates"][number]): string {
  return c.path ?? c.uri ?? c.candidate_id;
}

type View = "input" | "checking" | "problem" | "plan" | "progress" | "result" | "outcome";
/** Where keyboard focus lands for each step (first element that exists). */
const FOCUS: Record<View, string[]> = {
  input: ["compare-input-title"], checking: ["compare-progress-title", "compare-go-title"], problem: ["compare-go-title"],
  plan: ["compare-plan-title"], progress: ["compare-progress-title"], result: ["compare-answer-title"],
  outcome: ["compare-outcome-title"],
};

export function CompareScreen() {
  const [version, setVersion] = useState<VersionInfo | null>(null);
  const [versionError, setVersionError] = useState<string | null>(null);
  const [devices, setDevices] = useState<DevicesDocument | null>(null);
  const [form, setForm] = useState<CompareForm>(() => defaultForm("files", null));
  const [revision, setRevision] = useState(0);
  const [plannedAt, setPlannedAt] = useState(0);
  const [entryChoices, setEntryChoices] = useState<Record<string, number>>({});
  const comparison = useComparison();
  const { run, result, job } = comparison;
  // Focus moves only after the owner pressed one of this screen's buttons (never on a reload or a background update),
  // and then to the heading of the step that replaced the pressed control.
  const engaged = useRef(false);
  const pendingFocus = useRef(false);
  const acted = <A extends unknown[]>(fn: (...args: A) => void) => (...args: A) => {
    engaged.current = true;
    pendingFocus.current = true;
    fn(...args);
  };

  useEffect(() => {
    const ctrl = new AbortController();
    const t = setTimeout(() => {
      getVersion(false, ctrl.signal).then((info) => {
        if (ctrl.signal.aborted) return;
        setVersion(info);
        if (info.status !== "ok") return;
        getDevices(ctrl.signal).then((d) => { if (!ctrl.signal.aborted) setDevices(d); })
          .catch(() => { if (!ctrl.signal.aborted) setDevices(null); });
      }).catch((err: unknown) => { if (!ctrl.signal.aborted) setVersionError(errorSentence(err)); });
    }, 0);
    return () => { clearTimeout(t); ctrl.abort(); };
  }, []);

  const effective: CompareForm = form.primaryPlayer ? form : { ...form, primaryPlayer: defaultPlayerFor(devices, form.device) };
  const support = versionError !== null ? { ok: false, text: versionError } : unifiedSupport(version);
  const active = run !== null && ACTIVE_STAGES.includes(run.stage);
  const working = comparison.busy || active;
  const problem = formProblem(effective, devices);
  const stale = run !== null && run.stage === "awaiting_confirmation" && plannedAt !== revision;

  const update = (next: CompareForm) => {
    setForm(next);
    setRevision((n) => n + 1);
    comparison.dismissError();                       // an error about the earlier inputs no longer applies
  };
  const plan = acted((request: RunRequest = buildRunRequest(effective)) => {
    setPlannedAt(revision);
    setEntryChoices({});
    comparison.plan(request);
  });
  const startNew = acted(() => {
    comparison.reset();
    update(clearRunConditions(form));
  });
  const replanWithEntries = () => {
    if (run === null || run.request.magnets === undefined) return;
    const magnets = run.request.magnets.map((m) => ({
      uri: m.uri, file_index: Object.hasOwn(entryChoices, m.uri) ? entryChoices[m.uri] : m.file_index,
    }));
    plan({ ...run.request, magnets });
  };
  const needsChoice = run?.stage === "selection_required"
    ? run.candidates.filter((c) => c.status === "selection_required") : [];
  const allChosen = needsChoice.every((c) => c.uri !== null && (Object.hasOwn(entryChoices, c.uri) || c.file_index !== null));
  const readable = run ? run.candidates.filter((c) => c.status === "ready").length : 0;
  const unreadable = run ? run.candidates.filter((c) => c.status === "unreadable" || c.status === "failed") : [];
  const submitted = run?.candidates.length ?? 0;
  const pending = run !== null && (run.stage === "awaiting_confirmation" || run.stage === "selection_required");
  const view: View = run === null ? (comparison.busy ? "checking" : comparison.error !== null || comparison.gone ? "problem" : "input")
    : active ? (run.stage === "planning" ? "checking" : "progress")
      : pending ? "plan" : result !== null ? "result" : "outcome";

  useEffect(() => {
    if (!engaged.current) return;
    const now = document.activeElement;
    const lost = now === null || now === document.body || !now.isConnected;
    if (!pendingFocus.current && !lost) return;
    const target = FOCUS[view].map((id) => document.getElementById(id)).find((el) => el !== null);
    if (!target) return;
    target.focus();
    if (view !== "checking") pendingFocus.current = false;   // keep going until the check lands on its plan
  }, [view]);

  const announcement = comparison.gone ? "This run is no longer known to the app."
    : run === null ? (comparison.busy ? STAGE_TEXT.planning : "")
      : run.stage === "awaiting_confirmation" ? `Plan ready: ${readable} of ${submitted} versions are readable. Press Start to begin.`
        : run.stage === "done" ? (result !== null ? "Finished. The answer is shown below." : STAGE_TEXT.done)
          : run.stage === "error" ? `${STAGE_TEXT.error} The details are shown below.`
            : STAGE_TEXT[run.stage];
  const scheduled = run?.estimate?.pairs ?? readable * (readable - 1) / 2;
  const leftovers = comparison.leftoverPlans.length;
  const failedText = job?.status === "error" && job.error ? job.error
    : "The app no longer has the details of this problem. Check the files again to see it.";

  return <div className="compare-screen">
    <div role="status" aria-label="VideoSuite" className={`compare-banner ${support.ok ? "" : "compare-banner-warn"}`}>
      {support.text}
    </div>
    <p role="status" aria-label="Comparison status" className="compare-sr-only">{announcement}</p>

    <InputPanel form={effective} onChange={update} disabled={false} />
    <RunOptions form={effective} onChange={update} devices={devices} disabled={false} />

    <section className="compare-section" aria-labelledby="compare-go-title">
      <h2 id="compare-go-title" tabIndex={-1}>3. Check and start</h2>
      <p className="compare-muted">
        Checking reads the {form.inputKind === "files" ? "files" : "links"} once and shows VideoSuite's own plan. Nothing
        expensive starts until you press Start.
      </p>
      <button type="button" onClick={() => plan()} disabled={!support.ok || problem !== null || working}>
        {form.inputKind === "files" ? "Check files and estimate" : "Check links and estimate"}
      </button>
      {problem !== null && <p className="compare-muted">{problem}</p>}
      {comparison.error !== null && <p role="alert" className="compare-error">{comparison.error}</p>}
      {comparison.gone && <p role="alert" className="compare-error">
        This run is no longer known to the app (it was cleaned up or the app restarted). Check the files again.</p>}
    </section>

    {leftovers > 0 && <section className="compare-section" aria-label="Earlier unstarted check">
      <p className="compare-notice">{leftovers === 1 ? "An earlier check was never started and is still waiting."
        : `${leftovers} earlier checks were never started and are still waiting.`} It does not affect the run shown here.</p>
      <button type="button" onClick={comparison.discardLeftoverPlans}>
        {leftovers === 1 ? "Discard the earlier unstarted check" : "Discard the earlier unstarted checks"}
      </button>
    </section>}

    {run !== null && <FrozenChoices run={run} devices={devices} />}

    {pending && <section className="compare-section" aria-labelledby="compare-plan-title">
      <h2 id="compare-plan-title" tabIndex={-1}>Plan</h2>
      {run.stage === "selection_required" ? <>
        <p>{STAGE_TEXT.selection_required} The app does not guess which file is the movie.</p>
        {needsChoice.map((c, i) => <div key={c.candidate_id} role="radiogroup" aria-label={`Video file for link ${i + 1}`}
                                          className="compare-choices compare-choices-column">
          <p className="compare-path">{c.uri}</p>
          {(c.entries ?? []).map((e) => <label key={e.index} className="compare-choice">
            <input type="radio" name={`compare-entry-${c.candidate_id}`} value={e.index}
                   checked={c.uri !== null && entryChoices[c.uri] === e.index}
                   onChange={() => { if (c.uri !== null) setEntryChoices((x) => ({ ...x, [c.uri as string]: e.index })); }} />
            <span className="compare-path">{e.name}</span>
            {e.size !== null && <span className="compare-muted"> ({e.size} bytes)</span>}
          </label>)}
        </div>)}
        <button type="button" onClick={replanWithEntries} disabled={!allChosen || working}>Check again with these files</button>
        <button type="button" onClick={acted(comparison.cancel)} disabled={working || comparison.cancelRequested}>
          Discard this plan</button>
      </> : <>
        <p>{readable} of {submitted} versions are readable.</p>
        {unreadable.length > 0 && <>
          <ul className="compare-problems" aria-label="Versions that could not be read">
            {unreadable.map((c) => <li key={c.candidate_id}><span className="compare-path">{candidateName(c)}</span>
              {c.reason ? `: ${c.reason}` : ""}</li>)}
          </ul>
          <p className="compare-notice">Because some versions could not be read, no whole-set winner can be named. They stay
            listed in the result.</p>
        </>}
        {run.request.depth === "full" ? <>
          {run.estimate !== null && <p>{estimateText(run.estimate)}</p>}
          <p>Comparisons that will run: {scheduled} (every pair of the {readable} readable versions). Each one is an accurate
            picture comparison.</p>
          <p>Versions submitted: {submitted}, which would make {submitted * (submitted - 1) / 2} possible pairs
            {readable < submitted ? "; the versions that could not be read cannot be compared" : ""}.</p>
          <p className="compare-muted">This can take a long time. To shorten it, remove some versions and check again, or
            choose Quick. Nothing is left out automatically.</p>
        </> : <p>Quick reads the file facts only: a metadata estimate, the picture is not measured.</p>}
        {stale && <p className="compare-notice">Your choices changed after this check. Check again before starting.</p>}
        <button type="button" onClick={acted(comparison.start)} disabled={stale || working || !support.ok}>
          {run.request.depth === "full" ? "Start the full comparison" : "Start the quick estimate"}
        </button>
        <button type="button" onClick={acted(comparison.cancel)} disabled={working || comparison.cancelRequested}>
          Discard this plan</button>
      </>}
    </section>}

    {run !== null && (active || job?.status === "running") && <section className="compare-section"
                                                                         aria-labelledby="compare-progress-title">
      <h2 id="compare-progress-title" tabIndex={-1}>Progress</h2>
      <p>{STAGE_TEXT[run.stage]}</p>
      {job !== null && job.current !== "" && <p className="compare-path">{job.current}</p>}
      {job?.unreachable && <p className="compare-notice">Lost contact with the app; retrying…</p>}
      <p className="compare-muted">{CLOSE_TAB_TEXT}</p>
      <button type="button" onClick={acted(comparison.cancel)} disabled={comparison.cancelRequested}>
        {comparison.cancelRequested ? "Cancelling…" : "Cancel this run"}
      </button>
      {job !== null && job.events.length > 0 && <>
        <h3>Progress messages</h3>
        <ol role="log" aria-label="Progress messages" className="compare-log">
          {job.events.map((e, i) => <li key={i}>{e.msg}</li>)}
        </ol>
      </>}
    </section>}

    {view === "outcome" && run !== null && <section className="compare-section" aria-labelledby="compare-outcome-title">
      <h2 id="compare-outcome-title" tabIndex={-1}>
        {run.stage === "error" ? "Stopped with a problem" : run.stage === "cancelled" ? "Cancelled" : "Finished"}</h2>
      {run.stage === "error" && <p role="alert" className="compare-error">{failedText}</p>}
      {run.stage === "cancelled" && <p>Nothing more will be compared. Press New comparison to start again.</p>}
      {run.stage === "done" && <p>The result is no longer available. Check the files again to see it.</p>}
    </section>}

    {run !== null && result !== null && !active && <ComparisonResult result={result} devices={devices} />}

    {run !== null && !working && <button type="button" onClick={startNew}>New comparison</button>}
  </div>;
}
