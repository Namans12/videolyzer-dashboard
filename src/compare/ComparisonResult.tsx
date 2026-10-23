import type { ReactNode } from "react";
import { EvidenceLegend } from "../suite/EvidenceLegend";
import { RankResult } from "../suite/RankResult";
import { VerdictBadge } from "../suite/VerdictBadge";
import { estimateText } from "../suite/messages";
import { foldIssues, issueLine, recordName, scoreText } from "../suite/verdict";
import type { DevicesDocument, Issue, ScoreRecord } from "../suite/types";
import { FactsTable } from "./FactsTable";
import { ReportPanel } from "./ReportPanel";
import { deviceLabel, playerLabel } from "./types";
import type { QuickDocument, QuickGroup, QuickOrder, RankAnswer, RunResult } from "./types";

type Obj = Record<string, unknown>;
const isObj = (v: unknown): v is Obj => v !== null && typeof v === "object" && !Array.isArray(v);
/** Prototype-safe lookup: backend keys never reach Object.prototype. */
const own = (v: unknown, k: string): unknown => (isObj(v) && Object.prototype.hasOwnProperty.call(v, k) ? v[k] : undefined);
const str = (v: unknown): string | null => (typeof v === "string" ? v : null);
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

const QUICK_OVERALL_LABEL = "Metadata estimate — picture not measured";
const STAGE_NAMES: Record<string, string> = {
  planning: "planning", selection: "file choice", facts: "reading facts", quick: "quick estimate", pixels: "picture comparison",
  rank: "ranking", report: "reports",
};
const STATUS_TEXT: Record<QuickGroup["status"], string> = { estimated: "estimate", tie: "tie", insufficient: "not enough information" };

function groupOf(order: QuickOrder, id: string): QuickGroup | null {
  return order.groups.find((g) => g.ids.includes(id)) ?? null;
}

function OverallFacts({ basis }: { basis: Obj | undefined }) {
  const known = own(basis, "known");
  const observed = own(basis, "observed");
  const k = (name: string) => own(known, name) === true;
  const width = num(own(observed, "width"));
  const bitrate = num(own(observed, "bitrate"));
  const hdr = own(observed, "hdr");
  const lossless = own(observed, "lossless_audio");
  const parts = [
    k("width") && width !== null ? `${width} px wide` : "width unknown",
    k("hdr") ? (Array.isArray(hdr) && hdr.length ? `HDR: ${hdr.map(String).join(", ")}` : "SDR") : "HDR unknown",
    k("bitrate") && bitrate !== null ? `${Math.round(bitrate)} kb/s` : "bitrate unknown",
    k("lossless_audio") ? (Array.isArray(lossless) && lossless.length ? `lossless audio: ${lossless.map(String).join(", ")}`
      : "no lossless audio") : "lossless audio unknown",
  ];
  return <>{parts.join("; ")}</>;
}

function PlaybackCell({ playback }: { playback: unknown }) {
  const verdict = str(own(playback, "verdict")) ?? "unknown";
  const issues = own(playback, "issues");
  const { issues: list, hdrNote } = foldIssues(Array.isArray(issues) ? issues as Issue[] : []);
  const score = num(own(playback, "score"));
  const range = own(playback, "score_range");
  return <>
    <VerdictBadge verdict={verdict} evidence={str(own(playback, "evidence"))} />
    <p>You will see: {str(own(playback, "effective_hdr")) ?? "unknown"}; audio: {str(own(playback, "effective_audio")) ?? "none"}</p>
    {score !== null && <p>Playback rule score: {scoreText(score, Array.isArray(range) ? range as number[] : undefined)}</p>}
    {(list.length > 0 || hdrNote) && <ul className="suite-issues">
      {list.map((issue, i) => <li key={`${i}-${issue.code}`} data-severity={issue.severity}>{issueLine(issue)}</li>)}
      {hdrNote && <li data-severity="info">{hdrNote}</li>}
    </ul>}
  </>;
}

function QuickTable({ order, name, device, caption }: {
  order: QuickOrder; name: (id: string) => string; device: boolean; caption: string;
}) {
  const limitations = [...new Set(order.groups.flatMap((g) => g.limitations))];
  return <>
    <div className="compare-table-wrap">
      <table className="compare-table" aria-label={caption}>
        <thead>
          <tr>
            <th scope="col">#</th><th scope="col">Version</th><th scope="col">Order</th>
            <th scope="col">{device ? "Playback on this player" : "Facts behind the order"}</th>
          </tr>
        </thead>
        <tbody>
          {order.groups.flatMap((g, gi) => g.ids.map((id) => <tr key={`${gi}-${id}`}>
            <td>{g.rank ?? "—"}</td>
            <td className="compare-path">{name(id)}</td>
            <td>{STATUS_TEXT[g.status] ?? g.status}{g.status === "tie" ? ` (${g.ids.length} versions)` : ""}</td>
            <td>{device ? <PlaybackCell playback={own(own(g.basis, id), "playback")} />
              : <OverallFacts basis={own(g.basis, id) as Obj | undefined} />}</td>
          </tr>))}
        </tbody>
      </table>
    </div>
    {limitations.length > 0 && <ul className="compare-limits" aria-label={`Limits of ${caption}`}>
      {limitations.map((l, i) => <li key={i}>{l}</li>)}
    </ul>}
  </>;
}

function SettingsLine({ doc }: { doc: unknown }) {
  // Only whether owner settings were used; the nested settings file path is never shown.
  const settings = own(doc, "settings");
  if (!isObj(settings)) return null;
  const applied = num(settings.applied_count) ?? 0;
  return <p className="compare-muted">{settings.loaded === true
    ? `Your owner settings were used for this run${applied ? ` (${applied} rule change(s) applied)` : ""}.`
    : "No owner settings file was loaded, so the device's standard rules were used."}</p>;
}

/** The answer-first view of one backend RunResult. Quick is never cast as a rank document: the discriminator is
 *  the result's measurement. Only the requested answers appear, and incomplete coverage never names a winner. */
export function ComparisonResult({ result, devices }: { result: RunResult; devices: DevicesDocument | null }) {
  const r = result.request;
  const quick = result.measurement === "metadata";
  const names = new Map(result.facts.map((f) => [f.id, recordName(f as unknown as ScoreRecord)]));
  const candidates = result.snapshot.candidates;
  const name = (id: string) => names.get(id) ?? id;
  const whole = result.coverage.complete && result.snapshot.stage === "done";
  // Changed settings only withhold the device answer: the neutral Overall answer does not use settings, so with
  // complete coverage it stands even though the run ended with that problem.
  const settingsChanged = result.failures.some((f) => f.kind === "settings_changed");
  const onlySettingsChanged = result.snapshot.stage === "error" && result.failures.length > 0
    && result.failures.every((f) => f.kind === "settings_changed");
  const overallWhole = result.coverage.complete && (result.snapshot.stage === "done" || onlySettingsChanged);
  const tv = deviceLabel(r.device);
  const player = playerLabel(devices, r.device, r.primary_player);
  const key = `${r.device}/${r.primary_player}`;
  const deviceTitle = `${tv} / ${player}`;
  const wantsOverall = r.results !== "device";
  const wantsDevice = r.results !== "overall";
  const quickDoc = quick && result.device !== null ? result.device as QuickDocument : null;
  const rankDevice = !quick && result.device !== null ? result.device as RankAnswer : null;
  const quickPrimary = quickDoc !== null ? own(quickDoc.devices, key) as QuickOrder | undefined : undefined;

  const answers: ReactNode[] = [];
  if (wantsOverall) {
    if (result.overall === null) {
      answers.push("No Best overall answer: the run stopped before it was ready. Run it again to get one.");
    } else if (quick) {
      const order = result.overall as QuickOrder;
      answers.push(overallWhole && order.coverage_complete && order.winner_ids.length === 1
        ? `Best overall (${QUICK_OVERALL_LABEL.toLowerCase()}): ${name(order.winner_ids[0])}`
        : overallWhole && order.winner_ids.length > 1
          ? `Tie for Best overall (${QUICK_OVERALL_LABEL.toLowerCase()}): ${order.winner_ids.map(name).join(", ")}`
          : `No whole-set winner for Best overall: the facts are not enough to put every version in order (${QUICK_OVERALL_LABEL.toLowerCase()}).`);
    } else {
      const doc = result.overall as RankAnswer;
      const top = doc.quality.filter((q) => q.rank === 1 && q.quality_score !== null);
      const same = top.length ? doc.same_picture_groups.find((g) => g.includes(top[0].label) && g.length > 1) : undefined;
      answers.push(!overallWhole
        ? `No whole-set winner for Best overall: ${result.coverage.completed_pairs} of ${result.coverage.expected_pairs} pairs were compared.`
        : same ? `Same picture: ${same.join(" = ")} (measured picture, no single winner).`
          : top.length === 1 ? `Best overall (measured picture): ${top[0].label}`
            : "No single Best overall winner: the measured order has no clear first place.");
    }
  }
  if (wantsDevice) {
    if (result.device === null) {
      answers.push(settingsChanged
        ? `No answer for ${deviceTitle}: the device answer was withheld because your settings or the device rules changed `
          + "during the run. Run it again to get the device answer with the new rules."
        : `No answer for ${deviceTitle}: the run stopped before ranking. Run it again to get one.`);
    } else if (quickDoc !== null) {
      const order = quickPrimary;
      const winner = order && whole && order.coverage_complete && order.winner_ids.length === 1 ? order.winner_ids[0] : null;
      answers.push(winner !== null
        ? <>Best playback estimate for {deviceTitle} — picture not measured: {name(winner)}{" "}
          <VerdictBadge verdict={str(own(own(own(groupOf(order!, winner)?.basis, winner), "playback"), "verdict")) ?? "unknown"}
                        evidence={str(own(own(own(groupOf(order!, winner)?.basis, winner), "playback"), "evidence"))} /></>
        : `No whole-set winner for ${deviceTitle}: the facts are not enough to put every version in order (picture not measured).`);
    } else if (rankDevice !== null) {
      const d = own(rankDevice.devices, key) as RankAnswer["devices"][string] | undefined;
      const row = d?.ranking.find((x) => x.device_rank === 1) ?? d?.ranking[0];
      answers.push(whole && row
        ? <>Best on {tv} with {player} (measured picture and playback): {row.label}{" "}
          <VerdictBadge verdict={row.verdict} evidence={row.evidence} /></>
        : `No whole-set winner for ${deviceTitle}: ${result.coverage.completed_pairs} of ${result.coverage.expected_pairs} pairs were compared.`);
    }
  }

  const failures = result.failures;
  const showCoverage = !result.coverage.complete || failures.length > 0 || result.snapshot.stage !== "done";
  const candidateName = (id: string | null) => {
    const c = id === null ? undefined : candidates.find((x) => x.candidate_id === id);
    if (c === undefined) return "The run";
    const record = c.record_id !== null ? names.get(c.record_id) : undefined;
    return record ?? (c.path ?? c.uri ?? c.candidate_id);
  };
  const p = result.provenance;
  const rules = Object.entries(p.rules_versions);

  return <div className="compare-result">
    <section className="compare-section compare-answer" aria-label="Answer">
      <h2 id="compare-answer-title" tabIndex={-1}>Answer</h2>
      <ul className="compare-plain-list">{answers.map((a, i) => <li key={i}>{a}</li>)}</ul>
      {quick && <p className="compare-muted">Quick reads file facts only. It never measures the picture; run Full on
        downloaded files for a measured answer.</p>}
    </section>

    {showCoverage && <section className="compare-section" aria-label="Coverage">
      <h3>What is missing</h3>
      <p>{result.coverage.readable} of {result.coverage.submitted} versions could be read.</p>
      {result.coverage.expected_pairs > 0 && <p>
        {result.coverage.completed_pairs} of {result.coverage.expected_pairs} pairs were compared.</p>}
      {!result.coverage.complete && <p className="compare-notice">The evidence is incomplete, so no whole-set winner is
        named. Every submitted version stays listed.</p>}
      {result.snapshot.stage === "cancelled" && <p>This run was cancelled; anything shown is partial.</p>}
      {result.snapshot.stage === "error" && (onlySettingsChanged && overallWhole && result.overall !== null
        ? <p>The Best overall answer is complete; only the device answer was withheld.</p>
        : <p>This run stopped with a problem; anything shown is partial.</p>)}
      <ul className="compare-problems" aria-label="Problems in this run">
        {failures.map((f, i) => <li key={i}>
          {f.kind === "settings_changed"
            ? "Your settings or the device rules changed during the run, so no device ranking was made. The finished "
              + "picture comparisons were kept. Run it again to use the new rules."
            : <><span className="compare-path">{candidateName(f.candidate_id)}</span> ({STAGE_NAMES[f.stage] ?? f.stage}): {f.message}</>}
        </li>)}
      </ul>
    </section>}

    {result.overall !== null && <section className="compare-section" aria-label="Best overall">
      <h3>Best overall</h3>
      {quick
        ? <>
          <p className="compare-label">{QUICK_OVERALL_LABEL}</p>
          <QuickTable order={result.overall as QuickOrder} name={name} device={false} caption="Best overall estimate" />
        </>
        : <>
          <p className="compare-label">Measured picture comparison (device-neutral).</p>
          <RankResult doc={result.overall as RankAnswer} devices={devices} />
        </>}
    </section>}

    {result.device !== null && <section className="compare-section" aria-label="Best for my device">
      <h3>Best for my device</h3>
      {quickDoc !== null ? <>
        <p className="compare-label">Best playback estimate for {deviceTitle} — picture not measured</p>
        <SettingsLine doc={quickDoc} />
        {quickPrimary !== undefined && <section aria-label={`Your player: ${player}`} className="compare-player">
          <h4>Your player: {player}</h4>
          <QuickTable order={quickPrimary} name={name} device caption={`Playback estimate for ${player}`} />
        </section>}
        {quickDoc.alternatives.map((alt) => {
          const label = playerLabel(devices, alt.device, alt.player);
          return <section key={`${alt.device}/${alt.player}`} aria-label={`Alternative player: ${label}`} className="compare-player">
            <h4>Alternative player: {label}</h4>
            <QuickTable order={alt.order} name={name} device caption={`Playback estimate for ${label}`} />
          </section>;
        })}
      </> : rankDevice !== null && <>
        <p className="compare-label">Measured picture and playback for {deviceTitle}.</p>
        <SettingsLine doc={rankDevice} />
        <RankResult doc={rankDevice} devices={devices} showOverall={false} selectedPlayer={key} />
      </>}
    </section>}

    <section className="compare-section" aria-label="File facts">
      <h3>File facts</h3>
      <FactsTable records={result.facts as unknown as ScoreRecord[]} />
    </section>

    <section className="compare-section" aria-label="Reports">
      <h3>Reports</h3>
      <ReportPanel reports={result.reports} />
    </section>

    <details className="compare-section">
      <summary>How this result was made</summary>
      <ul className="compare-plain-list">
        <li>{quick ? "File facts only (the picture was not measured)." : "Accurate picture comparison of every pair of the readable versions."}</li>
        <li>Stages finished: {result.completed_stages.join(", ") || "none"}</li>
        <li>Made with VideoSuite {p.tool_version ?? "unknown"} (picture engine {p.engine_tag ?? "unknown"}, facts version {p.facts_version ?? "unknown"})</li>
        <li>Device rules: {rules.length ? rules.map(([k, v]) => `${k}: ${v}`).join(", ") : "none used"}</li>
        {result.estimate !== null && <li>{estimateText(result.estimate)}</li>}
        <li>Facts digest: {p.records_digest ?? "none"}</li>
        <li>Settings digest: {p.settings_digest ?? "none (no owner settings file)"}</li>
        <li>App build: {p.build_id ?? "not recorded"}</li>
      </ul>
    </details>

    <EvidenceLegend />
  </div>;
}
