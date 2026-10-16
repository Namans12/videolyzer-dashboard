import type { DeviceGroup } from "./verdict";
import { foldIssues, issueLine, scoreText, usbLine } from "./verdict";
import { BestPlayerHint } from "./BestPlayerHint";
import { VerdictBadge } from "./VerdictBadge";

export function DeviceCard({ group }: { group: DeviceGroup }) {
  return <article className="suite-device-card" aria-label={group.deviceLabel}>
    <h4>{group.deviceLabel}</h4>

    {group.best
      && <BestPlayerHint
        best={group.best}
        evidence={group.players.find(row => row.playerId === group.best?.player)?.result.evidence ?? null}
      />}

    {group.players.map(row => {
      const { result } = row;
      const { issues, hdrNote } = foldIssues(result.issues);
      const usb = usbLine(result);
      return <section className="suite-player" key={row.key}>
        <h5>
          {row.playerLabel}
          {row.isDefault ? " (default player)" : ""}
        </h5>

        <p>
          <VerdictBadge verdict={result.verdict} evidence={result.evidence} /> Score: {
            scoreText(result.score, result.score_range)
          }</p>

        <p>You will see: {result.effective_hdr}; audio: {result.effective_audio ?? "none"}</p>
        {usb && <p>{usb}</p>}

        {issues.length || hdrNote
          ? <ul className="suite-issues">
            {issues.map((issue, index) => <li
              key={`${index}-${issue.code}`}
              data-severity={issue.severity}
            >
              {issueLine(issue)}
            </li>)}
            {hdrNote && <li data-severity="info">{hdrNote}</li>}
          </ul>
          : <p className="suite-muted">No issues reported.</p>}

        {result.hints.length > 0
          && <ul>
            {result.hints.map((hint, index) => <li key={index}>Hint: {hint}</li>)}
          </ul>}
      </section>;
    })}
  </article>;
}
