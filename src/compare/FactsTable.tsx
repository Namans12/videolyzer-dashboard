import type { ScoreRecord } from "../suite/types";
import { recordName } from "../suite/verdict";

type Obj = Record<string, unknown>;
const isObj = (v: unknown): v is Obj => v !== null && typeof v === "object" && !Array.isArray(v);
/** Prototype-safe field lookup on backend data. */
const own = (v: unknown, k: string): unknown => (isObj(v) && Object.prototype.hasOwnProperty.call(v, k) ? v[k] : undefined);
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const str = (v: unknown): string | null => (typeof v === "string" && v !== "" ? v : null);
const UNKNOWN = "unknown";

function partial(record: unknown): boolean {
  const coverage = own(own(record, "source"), "probe_coverage");
  return own(own(record, "source"), "partial") === true || (typeof coverage === "string" && coverage !== "full");
}

function coverageText(record: unknown): string {
  const coverage = str(own(own(record, "source"), "probe_coverage"));
  if (coverage === "full") return "whole file";
  if (coverage === null) return UNKNOWN;
  return `only part of the file (${coverage})`;
}

function resolution(record: unknown): string {
  const video = own(record, "video");
  const w = num(own(video, "width")), h = num(own(video, "height"));
  return w !== null && h !== null ? `${w} × ${h}` : UNKNOWN;
}

function videoText(record: unknown): string {
  const video = own(record, "video");
  const codec = str(own(video, "codec"));
  if (codec === null) return UNKNOWN;
  const depth = num(own(video, "bit_depth"));
  return depth !== null ? `${codec}, ${depth}-bit` : codec;
}

function bitrateText(record: unknown): string {
  const kbps = num(own(own(record, "video"), "bitrate_kbps"));
  return kbps !== null && kbps > 0 ? `${Math.round(kbps)} kb/s` : UNKNOWN;
}

/** Observed HDR: formats when found; SDR only when the whole file was read and nothing undecided remains. */
function hdrText(record: unknown): string {
  const hdr = own(record, "hdr");
  const formats = own(hdr, "formats");
  if (Array.isArray(formats) && formats.length > 0) return formats.map(String).join(", ");
  if (partial(record) || !Array.isArray(formats)) return UNKNOWN;
  const codec = str(own(own(record, "video"), "codec"));
  const dv = str(own(own(hdr, "dolby_vision"), "status"));
  if ((codec === "hevc" || codec === "h265") && (dv === null || dv === "unknown")) return "no HDR found (Dolby Vision not checked)";
  return "SDR";
}

function audioText(record: unknown): string {
  const tracks = own(record, "audio");
  if (!Array.isArray(tracks)) return UNKNOWN;
  if (tracks.length === 0) return partial(record) ? "none found in the part read" : "none found";
  return tracks.map((t) => [str(own(t, "codec")), str(own(t, "channels_label")) ?? (num(own(t, "channels")) !== null ? `${num(own(t, "channels"))} ch` : null),
    str(own(t, "language"))].filter((x) => x !== null).join(" ")).join("; ");
}

function sizeText(record: unknown): string {
  const size = num(own(own(record, "container"), "size_bytes"));
  return size !== null ? `${size} bytes` : UNKNOWN;
}

function durationText(record: unknown): string {
  const seconds = num(own(own(record, "container"), "duration_s"));
  if (seconds === null || seconds <= 0) return UNKNOWN;
  const minutes = Math.round(seconds / 6) / 10;
  return `${minutes} min${partial(record) ? " (from the header, not verified)" : ""}`;
}

function checksText(record: unknown): string {
  const verification = own(record, "verification");
  const vetoes = own(verification, "vetoes"), flags = own(verification, "flags");
  const parts: string[] = [];
  if (Array.isArray(vetoes) && vetoes.length) parts.push(`${vetoes.length} problem(s) that block playback`);
  if (Array.isArray(flags) && flags.length) parts.push(`${flags.length} warning(s)`);
  return parts.length ? parts.join(", ") : Array.isArray(vetoes) ? "no problems found" : UNKNOWN;
}

const ROWS: Array<[string, (r: unknown) => string]> = [
  ["Resolution", resolution], ["Video", videoText], ["Video bitrate", bitrateText], ["HDR", hdrText],
  ["Audio tracks", audioText], ["Duration", durationText], ["File size", sizeText], ["How much was read", coverageText],
  ["File checks", checksText],
];

/** The run's frozen facts, side by side. Missing observations stay "unknown"; partial magnet reads are marked. */
export function FactsTable({ records }: { records: ScoreRecord[] }) {
  if (!Array.isArray(records) || records.length === 0) return <p className="compare-muted">No file facts were recorded for this run.</p>;
  const caveats = records.map((r) => {
    const list = own(own(r, "source"), "caveats");
    return Array.isArray(list) ? list.filter((c): c is string => typeof c === "string") : [];
  });
  return <div className="compare-table-wrap">
    <table className="compare-table" aria-label="File facts side by side">
      <thead>
        <tr><th scope="col">Fact</th>{records.map((r) => <th scope="col" key={r.id} className="compare-path">{recordName(r)}</th>)}</tr>
      </thead>
      <tbody>
        {ROWS.map(([label, read]) => <tr key={label}>
          <th scope="row">{label}</th>
          {records.map((r) => <td key={r.id}>{read(r)}</td>)}
        </tr>)}
        {caveats.some((c) => c.length > 0) && <tr>
          <th scope="row">Limits of a partial read</th>
          {caveats.map((list, i) => <td key={records[i].id}>
            {list.length ? <ul className="compare-plain-list">{list.map((c, j) => <li key={j}>{c}</li>)}</ul> : "none"}
          </td>)}
        </tr>}
      </tbody>
    </table>
  </div>;
}
