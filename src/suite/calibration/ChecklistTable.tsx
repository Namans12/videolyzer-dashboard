import { EVIDENCE_MEANING, evidenceLabel, evidenceWord } from "../verdict";
import type { CalibrationAnswer, Descriptor } from "./types";
import { playerName } from "./messages";

export function ChecklistTable({ descriptors, answers, selected, onSelect }: {
  descriptors: Descriptor[];
  answers: CalibrationAnswer[];
  selected?: { test: string; player: string } | null;
  onSelect: (descriptor: Descriptor) => void;
}) {
  const players = [...new Set(descriptors.map(d => d.player))].sort((a, b) =>
    Number(b === "builtin_usb") - Number(a === "builtin_usb"));
  const answerFor = (d: Descriptor) => answers.find(a => a.test === d.test && a.player === d.player);
  const done = descriptors.filter(d => answerFor(d)).length;
  return <section className="suite-section" aria-label="Calibration checklist">
    <h3>Calibration checklist</h3>
    <p>{done} of {descriptors.length} answered</p>
    <progress aria-label="Calibration progress" value={done} max={Math.max(1, descriptors.length)} />
    {players.map(player => <section className="suite-player" key={player}>
      <h4>{playerName(player)}</h4>
      <table className="suite-table" aria-label={`${playerName(player)} checklist`}>
        <thead><tr><th scope="col">Test</th><th scope="col">Title</th><th scope="col">Frozen prediction</th><th scope="col">Status</th></tr></thead>
        <tbody>{descriptors.filter(d => d.player === player).map(d => {
          const answer = answerFor(d);
          const status = !answer ? "not done" : answer.older_tv_software ? "older TV software"
            : answer.outcome === "unsure" ? "unsure" : answer.outcome === "skipped" ? "skipped"
            : answer.match === "none" || answer.match === "unsure" ? "answered" : answer.match;
          const active = selected?.test === d.test && selected.player === player;
          return <tr key={d.test} aria-selected={active} onClick={() => onSelect(d)}>
            <td><button type="button" aria-pressed={active} onClick={e => { e.stopPropagation(); onSelect(d); }}>{d.test}</button></td>
            <td>{d.title}{d.manual && <> <span className="suite-muted">manual</span></>}</td>
            <td><span>{d.prediction.text}</span>{" "}<span className="suite-evidence" title={EVIDENCE_MEANING[evidenceWord(d.prediction.evidence)]}>{evidenceLabel(d.prediction.evidence)}</span></td>
            <td>{status}</td>
          </tr>;
        })}</tbody>
      </table>
    </section>)}
    {descriptors.length === 0 && <p>No tests in this kit.</p>}
  </section>;
}
