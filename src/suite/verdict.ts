// src/suite/verdict.ts — how a suite result is put into words. Pure; no React.
import { EVIDENCE_WORDS } from "./types";
import type { BestPlayer, DevicesDocument, EvidenceWord, FormatCreditRemoved, Issue, PlaybackResult, ScoreRecord } from "./types";

export const VERDICT_LABEL: Record<string, string> = {
  PLAYS_WELL: "Plays well", PLAYS_DEGRADED: "Plays with a loss", RISKY: "May not play", WONT_PLAY: "Will not play",
};
export function verdictLabel(v: string): string { return Object.hasOwn(VERDICT_LABEL, v) ? VERDICT_LABEL[v] : v; }

export const EVIDENCE_MEANING: Record<EvidenceWord, string> = {
  official: "the maker's documentation", owner: "tested by you", reviewed: "a professional review",
  forum: "user reports", inferred: "our deduction", unknown: "no information",
};
/** Verbatim VIDEOSUITE.md line 70 (= videosuite/cli.py EVIDENCE_LEGEND). */
export const EVIDENCE_LEGEND_TEXT =
  "Evidence (weakest rule used): official = the maker's documentation; owner = tested by you; reviewed = a professional review; forum = user reports; inferred = our deduction; unknown = no information. A score range (for example 69-99) means the result is uncertain.";

export function evidenceWord(e: unknown): EvidenceWord {
  return typeof e === "string" && (EVIDENCE_WORDS as readonly string[]).includes(e) ? (e as EvidenceWord) : "unknown";
}
export function evidenceLabel(e: unknown): string { return `evidence: ${evidenceWord(e)}`; }

const NOTICE: Partial<Record<EvidenceWord, string>> = {
  forum: "Unverified rules: this result rests on user reports (forum evidence). Confirm it with your own playback test.",
  inferred: "Unverified rules: this result rests on our deduction (inferred evidence). Confirm it with your own playback test.",
  unknown: "Unverified rules: at least one rule used has no information behind it (unknown evidence). Confirm it with your own playback test.",
};
export function unverifiedNotice(e: unknown): string | null {
  const word = evidenceWord(e);
  return Object.hasOwn(NOTICE, word) ? NOTICE[word] ?? null : null;
}

export const SEVERITY_ORDER = ["veto", "warn", "risk", "info"] as const;
function sevRank(s: string): number {
  const i = (SEVERITY_ORDER as readonly string[]).indexOf(s);
  return i === -1 ? SEVERITY_ORDER.length : i;
}
/** Fusion's order (veto, warn, risk, info), stable within a severity; unknown severities last. */
export function sortIssues(issues: Issue[]): Issue[] {
  return (Array.isArray(issues) ? issues : [])
    .map((issue, i) => [issue, i] as const)
    .sort((a, b) => sevRank(a[0].severity) - sevRank(b[0].severity) || a[1] - b[1])
    .map(([issue]) => issue);
}

export const HDR_NOT_VERIFIED = "HDR path not verified";
/** Sorted issues without the INFO-level hdr_uncertain lines; those become ONE note naming the weakest confidence.
 *  A higher-severity hdr_uncertain (none exists today, playback.py:284) stays in the list (R28). */
export function foldIssues(issues: Issue[]): { issues: Issue[]; hdrNote: string | null } {
  const sorted = sortIssues(issues);
  const isFoldable = (i: Issue) => i.code === "hdr_uncertain" && i.severity === "info";
  const folded = sorted.filter(isFoldable);
  const rest = sorted.filter((i) => !isFoldable(i));
  if (folded.length === 0) return { issues: rest, hdrNote: null };
  const weakest = folded
    .map((i) => evidenceWord(i.confidence))
    .sort((a, b) => EVIDENCE_WORDS.indexOf(b) - EVIDENCE_WORDS.indexOf(a))[0];
  return { issues: rest, hdrNote: `${HDR_NOT_VERIFIED} (${weakest})` };
}
/** The CLI's own issue line format: "[severity/confidence] message". */
export function issueLine(i: Issue): string { return `[${i.severity}/${i.confidence}] ${i.message}`; }

export function scoreText(score: number, range: number[] | undefined): string {
  const s = Math.round(score);
  if (!Array.isArray(range) || range.length !== 2 || typeof range[0] !== "number" || typeof range[1] !== "number") return `${s}`;
  const lo = Math.round(range[0]);
  const hi = Math.round(range[1]);
  return lo === hi ? `${s}` : `${s} (range ${lo}-${hi})`;
}

export function bestPlayerHeadline(bp: BestPlayer): string {
  if (bp.verdict === "WONT_PLAY") return "No player on this device will play it";
  return bp.switch ? `Better player: ${bp.label}` : `Keep the default player: ${bp.label}`;
}

export const DEVICE_NAMES: Record<string, string> = {
  sony_bravia_8_ii: "Sony BRAVIA 8 II", galaxy_tab_s9_ultra: "Galaxy Tab S9 Ultra", vivobook_pro_16x: "Vivobook Pro 16X",
};
export function deviceName(id: string): string { return Object.hasOwn(DEVICE_NAMES, id) ? DEVICE_NAMES[id] : id; }

export const SHOWN_AS: Record<string, string> = {
  dolby_vision: "Dolby Vision", hdr10plus: "HDR10+", hdr10: "HDR10", hlg: "HLG", sdr: "SDR", broken: "a broken picture",
};
export function formatCreditText(d: FormatCreditRemoved): string {
  const shown = Object.hasOwn(SHOWN_AS, d.shown_as) ? SHOWN_AS[d.shown_as] : String(d.shown_as);
  const [m1, m2] = Array.isArray(d.measured) ? d.measured : [null, null];
  const tail = typeof m1 !== "number" || typeof m2 !== "number"
    ? "no measured scores, so a tie"
    : d.winner == null
      ? `measured picture ${m1.toFixed(1)} vs ${m2.toFixed(1)}, a tie`
      : `measured picture ${m1.toFixed(1)} vs ${m2.toFixed(1)}, ${d.winner} is ahead`;
  return `Format credit not counted on this player: ${d.a} vs ${d.b} (${d.rule}; the winning format is shown as ${shown}; ${tail})`;
}

export const FS_NAMES: Record<string, string> = { fat32: "FAT32", exfat: "exFAT", ntfs: "NTFS" };
export const AUDIO_NAMES: Record<string, string> = { tv_speakers: "TV speakers", soundbar: "Soundbar", avr: "AV receiver" };

export function usbLine(r: PlaybackResult): string | null {
  if (!r.usb_filesystem) return null;
  const name = Object.hasOwn(FS_NAMES, r.usb_filesystem) ? FS_NAMES[r.usb_filesystem] : r.usb_filesystem;
  const from = r.usb_filesystem_source === "settings" ? " (from your settings)"
    : r.usb_filesystem_source === "cli" ? " (chosen for this run)" : "";
  return `USB file system: ${name}${from}`;
}

export const PARTIAL_TEXT = "Partial download: only part of the file was probed, so some facts are not verified.";

export function basename(path: string): string {
  const parts = String(path).split(/[\\/]/);
  return parts[parts.length - 1] || String(path);
}
export function recordName(rec: ScoreRecord): string {
  return rec.source?.real_name || (rec.source?.path ? basename(rec.source.path) : "") || rec.id;
}

export interface PlayerRow { key: string; playerId: string; playerLabel: string; isDefault: boolean; result: PlaybackResult; }
export interface DeviceGroup { deviceId: string; deviceLabel: string; players: PlayerRow[]; best: BestPlayer | null; }

/** Playback rows grouped per device: devices-document order (unknown devices/players after, in key order). */
export function groupPlayback(record: ScoreRecord, devices: DevicesDocument | null): DeviceGroup[] {
  const groups = new Map<string, DeviceGroup>();
  const order = (devices?.devices ?? []).map((d) => d.id);
  for (const [key, result] of Object.entries(record.playback ?? {})) {
    const cut = key.indexOf("/");
    const deviceId = cut === -1 ? key : key.slice(0, cut);
    const playerId = cut === -1 ? "" : key.slice(cut + 1);
    const dev = devices?.devices.find((d) => d.id === deviceId);
    const pl = dev?.players.find((p) => p.id === playerId);
    if (!groups.has(deviceId)) {
      groups.set(deviceId, { deviceId, deviceLabel: dev?.label ?? deviceName(deviceId), players: [],
                             best: record.best_player && Object.hasOwn(record.best_player, deviceId)
                               ? record.best_player[deviceId] ?? null : null });
    }
    groups.get(deviceId)!.players.push({ key, playerId, playerLabel: pl?.label ?? playerId, isDefault: pl?.default ?? false, result });
  }
  const rank = (id: string) => { const i = order.indexOf(id); return i === -1 ? order.length : i; };
  const out = [...groups.values()].sort((a, b) => rank(a.deviceId) - rank(b.deviceId));
  for (const g of out) {
    const pOrder = devices?.devices.find((d) => d.id === g.deviceId)?.players.map((p) => p.id) ?? [];
    const pr = (id: string) => { const i = pOrder.indexOf(id); return i === -1 ? pOrder.length : i; };
    g.players.sort((a, b) => pr(a.playerId) - pr(b.playerId));
  }
  return out;
}
