import { errorKindSentence } from "./messages";
import { deviceName, PARTIAL_TEXT } from "./verdict";
import { VerdictBadge } from "./VerdictBadge";
import type { BestPlayer } from "./types";

function object(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}
function text(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}
function bestPlayer(value: unknown): value is BestPlayer {
  const v = object(value);
  return v !== null && [v.player, v.label, v.verdict, v.reason].every(s => typeof s === "string");
}
export function MagnetSuiteBlock({ suite }: { suite: unknown }) {
  const s = object(suite);
  const status = text(s?.status);
  if (!s || !["ok", "unavailable", "error", "skipped"].includes(status ?? "")) return <div
    className="suite-magnet"
    aria-label="VideoSuite result"
  >
    <p className="suite-magnet-title">VideoSuite</p>
    <p>VideoSuite result not readable.</p>
  </div>;
  const record = object(s.record);
  const source = object(record?.source);
  const playback = object(record?.playback);
  const best = Object.entries(object(s.best_player) ?? {}).filter(
    (entry): entry is [string, BestPlayer] => bestPlayer(entry[1])
  );
  return <div className="suite-magnet" aria-label="VideoSuite result">
    <p className="suite-magnet-title">VideoSuite</p>

    {status === "unavailable"
      && <p>VideoSuite not available: {text(s.message) ?? "VideoSuite is not available."}</p>}

    {status === "error"
      && <p>VideoSuite could not score this file: {text(s.message) ?? errorKindSentence(text(s.error_kind))}
      </p>}

    {status === "skipped"
      && <p>VideoSuite skipped this file: {text(s.message) ?? "no reason given"}</p>}

    {status === "ok"
      && <>
        {s.partial === true
          && <>
            <p className="suite-partial">{PARTIAL_TEXT}</p>
            {Array.isArray(source?.caveats)
              && <ul>
                {source.caveats.filter((v): v is string => typeof v === "string").map((v, i) => <li key={i}>
                  {v}
                </li>)}
              </ul>}
          </>}

        {best.length
          ? best.map(([id, bp]) => {
            const key = `${id}/${bp.player}`;
            const row = playback && Object.hasOwn(playback, key) ? object(playback[key]) : null;
            return <div key={id}>
              <p>
                {deviceName(id)}: {bp.label}
                {" "}
                <VerdictBadge verdict={bp.verdict} evidence={text(row?.evidence)} /></p>
              <p className="suite-muted">{bp.reason}</p>
            </div>;
          })
          : <p>No player recommendation in this result.</p>}

        {text(s.tool_version) && <p className="suite-muted">VideoSuite {text(s.tool_version)}</p>}
      </>}
  </div>;
}
