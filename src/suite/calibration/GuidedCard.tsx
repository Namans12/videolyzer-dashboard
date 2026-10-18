import { useEffect, useId, useRef, useState } from "react";
import { EVIDENCE_MEANING, evidenceLabel, evidenceWord } from "../verdict";
import { singleLineNote } from "./api";
import { matchText, playerName, SAVING_TEXT, SESSION_REQUIRED_TEXT } from "./messages";
import type { CalibrationAnswer, Descriptor } from "./types";
import type { useCalibration } from "./useCalibration";

type Calibration = Pick<ReturnType<typeof useCalibration>, "status" | "answers" | "pendingAnswers" | "loading" | "mutationBusy" | "error" | "answer">;
interface Props {
  calibration: Calibration;
  selected: { test: string; player: string } | null;
  onSelect: (descriptor: Descriptor) => void;
  /** Connect to ConditionsBar.onReadyChange; false while conditions are being edited. */
  conditionsConfirmed: boolean;
}

export function GuidedCard({ calibration, selected, onSelect, conditionsConfirmed }: Props) {
  const descriptors = calibration.status?.kit?.descriptors ?? [];
  const index = descriptors.findIndex(d => d.test === selected?.test && d.player === selected.player);
  const descriptor = descriptors[index];
  const answer = calibration.answers.find(a => a.test === descriptor?.test && a.player === descriptor.player);
  const done = descriptors.filter(d => calibration.answers.some(a => a.test === d.test && a.player === d.player)).length;
  return <section className="suite-section" aria-label="Guided calibration">
    <h3>Guided calibration</h3>
    <p>{done} of {descriptors.length} answered</p>
    <progress aria-label="Guided calibration progress" value={done} max={Math.max(1, descriptors.length)} />
    {descriptor ? <GuidedAnswer key={JSON.stringify([calibration.status?.kit?.id, descriptor.test, descriptor.player, answer?.note, answer?.at])}
      calibration={calibration} descriptor={descriptor} saved={answer} conditionsConfirmed={conditionsConfirmed} />
      : <p>{calibration.loading ? "Loading calibration…" : "Select a test from the checklist."}</p>}
    {calibration.error && <p role="alert">{calibration.error}</p>}
    <nav aria-label="Calibration tests">
      <button type="button" disabled={index <= 0} onClick={() => onSelect(descriptors[index - 1])}>Back</button>
      <button type="button" disabled={index < 0 || index >= descriptors.length - 1} onClick={() => onSelect(descriptors[index + 1])}>Next</button>
    </nav>
  </section>;
}

function GuidedAnswer({ calibration, descriptor: d, saved, conditionsConfirmed }: {
  calibration: Calibration; descriptor: Descriptor; saved?: CalibrationAnswer; conditionsConfirmed: boolean;
}) {
  const id = useId();
  const [note, setNote] = useState(() => singleLineNote(saved?.note ?? "").slice(0, 500));
  const [saving, setSaving] = useState(false);
  const inFlight = useRef(false);
  const mounted = useRef(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const pending = saving || calibration.pendingAnswers.some(a => a.test === d.test && a.player === d.player);
  const sessionReady = conditionsConfirmed && !!calibration.status?.session;
  const disabled = !sessionReady || pending || calibration.loading || calibration.mutationBusy;
  const save = async (outcome: string) => {
    if (disabled || inFlight.current) return;
    inFlight.current = true; setSaving(true);
    try { await calibration.answer(d.test, d.player, outcome, singleLineNote(note).slice(0, 500)); }
    finally { inFlight.current = false; if (mounted.current) setSaving(false); }
  };
  const outcomes = d.outcomes.filter(o => o.id !== "unsure" && o.id !== "skipped");
  const savedText = saved && (saved.outcome === "unsure" ? "Not sure" : saved.outcome === "skipped" ? "Skipped"
    : d.outcomes.find(o => o.id === saved.outcome)?.text ?? saved.outcome);
  const savedOutcome = d.outcomes.find(o => o.id === saved?.outcome);
  const storedOnly = d.class !== "decisive" || savedOutcome?.value == null;
  const ownerObservation = saved && !saved.older_tv_software && saved.outcome !== "unsure"
    && saved.outcome !== "skipped" && !storedOnly;
  return <article aria-label={`${d.test} ${playerName(d.player)}`}>
    <h4>{d.test}: {d.title}</h4>
    <p>Player: {playerName(d.player)}</p>
    {!d.manual && d.file !== null && <p>Play: {d.file}</p>}
    <p>{d.instruction}</p>
    <p>Frozen prediction: {d.prediction.text}{" "}<span className="suite-evidence" title={EVIDENCE_MEANING[evidenceWord(d.prediction.evidence)]}>{evidenceLabel(d.prediction.evidence)}</span></p>
    <p>{d.question}</p>
    {!sessionReady && <p>{SESSION_REQUIRED_TEXT}</p>}
    <label htmlFor={`${id}-note`}>Optional note</label>
    <input id={`${id}-note`} value={note} maxLength={500} aria-describedby={`${id}-count`} disabled={pending || calibration.mutationBusy}
      onChange={e => setNote(singleLineNote(e.target.value).slice(0, 500))} />
    <p id={`${id}-count`} className="suite-muted">{note.length} / 500 characters</p>
    <div role="group" aria-label="Observed outcome">
      {outcomes.map(o => <button type="button" key={o.id} disabled={disabled} style={{ display: "block", width: "100%", minHeight: 48 }} onClick={() => void save(o.id)}>{o.text}</button>)}
      {d.outcomes.some(o => o.id === "unsure") && <button type="button" disabled={disabled} onClick={() => void save("unsure")}>Not sure</button>}
      {d.outcomes.some(o => o.id === "skipped") && <button type="button" disabled={disabled} onClick={() => void save("skipped")}>Skip</button>}
    </div>
    <div role="status" aria-live="polite">
      {pending && <p>{SAVING_TEXT}</p>}
      {saved && <>
        <p>Saved outcome: {savedText}{ownerObservation && <>{" "}
          <span className="suite-evidence" title={EVIDENCE_MEANING.owner}>{evidenceLabel("owner")}</span>{" "}<span>(recorded)</span>
        </>}</p>
        <p>{storedOnly ? "Recorded — stored only, not used for scores." : "Recorded."}</p><p>{matchText(saved.match)}</p>
        {saved.older_tv_software && <p>Older TV software: record a fresh answer before applying.</p>}
        <p>Recorded conditions: {saved.conditions.tv_software ?? "unknown"}; {saved.conditions.audio_output ?? "unknown"}; {saved.conditions.usb_filesystem ?? "unknown"}</p>
      </>}
    </div>
  </article>;
}
