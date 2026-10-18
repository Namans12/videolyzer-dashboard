// Frozen calibration documents. HTTP and job results enter as unknown, then pass these guards.
export type AudioOutput = "tv_speakers" | "soundbar" | "avr";
export type UsbFilesystem = "fat32" | "exfat" | "ntfs";
export interface Conditions { tv_software: string | null; audio_output: AudioOutput | null; usb_filesystem: UsbFilesystem | null; }
export interface CalibrationSession extends Conditions { tv_software: string; confirmed_at: string; }
export interface Prediction { outcome: string | null; text: string; evidence: string; }
export interface Outcome { id: string; text: string; value: unknown; }
export type TestClass = "decisive" | "informational";
export type SourceKind = "invented" | "excerpt" | "manual";
export interface Descriptor {
  test: string; player: string; title: string; class: TestClass; rule: string | null;
  instruction: string; question: string; outcomes: Outcome[]; conditions: string[];
  manual: boolean; file: string | null;
  source: { kind: SourceKind; file: string | null; verification: Record<string, unknown> | null };
  prediction: Prediction;
}
export interface Envelope { schema: string; tool_version: string; }
export interface PlanTest {
  test: string; title: string; class: TestClass; players: string[]; manual: boolean; instruction: string; question: string;
  outcomes: { id: string; text: string }[]; prediction: Record<string, Prediction>;
  source: { kind: SourceKind; status: "ready" | "needs_real_file" | "unavailable"; file: string | null; est_bytes: number | null; reason: string | null };
}
export interface PlanDocument extends Envelope { catalogue_version: number; players: string[]; tests: PlanTest[]; }
export interface BuildDocument extends Envelope {
  kit_id: string; out: string; descriptor_path: string; total_bytes: number; descriptors: Descriptor[];
  unavailable: { test: string; reason: string }[];
}
export interface SessionDocument extends Envelope { kit_id: string; session: CalibrationSession; }
export type AnswerMatch = "matches" | "differs" | "unsure" | "none";
export interface CalibrationAnswer {
  kit_id: string; test: string; player: string; outcome: string; note: string; conditions: Conditions;
  at: string; older_tv_software: boolean; match: AnswerMatch;
}
export interface Summary { done: number; total: number; matches: number; differs: number; }
// record has no required recovery flag; unknown additive fields are ignored.
export interface RecordDocument extends Envelope { answer: CalibrationAnswer; summary: Summary; }
export interface Kit { id: string; catalogue_version: number; built_at: string; out: string; descriptors: Descriptor[]; }
export interface StatusDocument extends Envelope {
  recovery_required: boolean; kit: Kit | null; session: CalibrationSession | null; answers: CalibrationAnswer[]; summary: Summary;
}
export interface ApplyChange {
  id: string; test: string; player: string; rule: string | null;
  action: "confirm" | "change" | "veto" | "skip" | "undo" | "left";
  reason: string; previous: unknown; value: unknown; veto: boolean;
}
export interface ApplyDocument extends Envelope {
  revision: string; applied: boolean; recovery_required: boolean; changes: ApplyChange[];
  conflicts: { rule: string; player: string; tests: string[] }[];
  skipped: { test: string; player: string; reason: string }[];
}
export interface ReportSide { verdict: string; score: number; evidence: string; effective_audio: string | null; effective_hdr: string | null; }
export interface ReportBestPlayer {
  player: string; label: string; verdict: string; score: number; default_player: string; switch: boolean; reason: string;
}
export interface ReportDocument extends Envelope {
  note: "playback only, not a quality ranking"; kit_active: boolean; recovery_required: boolean;
  files: { path: string; player: string; before: ReportSide; after: ReportSide; changed: boolean }[];
  best_player_changes: { path: string; device: string; before: ReportBestPlayer; after: ReportBestPlayer }[];
}
export interface PlanRequest { paths: string[]; players?: string[]; deep_dv?: boolean; }
export interface BuildRequest extends PlanRequest { out: string; tests?: string[]; }
export interface SessionRequest { out: string; tv_software: string; audio_output: AudioOutput | null; usb_filesystem: UsbFilesystem | null; }
export interface RecordRequest { kit: string; test: string; player: string; outcome: string; note: string; }
export interface ApplyRequest { kit: string; preview: boolean; revision: string | null; confirm_veto: string[]; }
export interface UndoRequest { kit: string; }
export interface ReportRequest { kit: string; paths: string[]; }

type Obj = Record<string, unknown>;
type Guard<T> = (v: unknown) => v is T;
export const isObject = (v: unknown): v is Obj => v !== null && typeof v === "object" && !Array.isArray(v);
/** Use this for maps keyed by backend strings (players, outcomes, conditions). */
export function own(v: unknown, key: string): unknown { return isObject(v) && Object.hasOwn(v, key) ? v[key] : undefined; }
const str: Guard<string> = (v): v is string => typeof v === "string";
const bool: Guard<boolean> = (v): v is boolean => typeof v === "boolean";
const num: Guard<number> = (v): v is number => typeof v === "number" && Number.isFinite(v);
const count: Guard<number> = (v): v is number => num(v) && Number.isInteger(v) && v >= 0;
const nullable = <T>(guard: Guard<T>): Guard<T | null> => (v): v is T | null => v === null || guard(v);
const list = <T>(guard: Guard<T>): Guard<T[]> => (v): v is T[] => Array.isArray(v) && v.every(guard);
const strings = list(str);
const oneOf = <T extends string>(...values: T[]): Guard<T> => (v): v is T => str(v) && values.some(x => x === v);
const fields = (v: unknown, shape: Record<string, (x: unknown) => boolean>): v is Obj =>
  isObject(v) && Object.entries(shape).every(([key, check]) => Object.hasOwn(v, key) && check(v[key]));
const any = (): boolean => true;
const sourceKind = oneOf("invented", "excerpt", "manual");
const testClass = oneOf("decisive", "informational");
const conditions = (v: unknown): v is Conditions => fields(v, {
  tv_software: nullable(str), audio_output: nullable(oneOf("tv_speakers", "soundbar", "avr")), usb_filesystem: nullable(oneOf("fat32", "exfat", "ntfs")),
});
export const isSession = (v: unknown): v is CalibrationSession => conditions(v) && fields(v, { tv_software: str, confirmed_at: str });
export const isPrediction = (v: unknown): v is Prediction => fields(v, { outcome: nullable(str), text: str, evidence: str });
const outcome = (v: unknown): v is Outcome => fields(v, { id: str, text: str, value: any });
export const isDescriptor = (v: unknown): v is Descriptor => fields(v, {
  test: str, player: str, title: str, class: testClass, rule: nullable(str), instruction: str, question: str,
  outcomes: list(outcome), conditions: strings, manual: bool, file: nullable(str), prediction: isPrediction,
  source: x => fields(x, { kind: sourceKind, file: nullable(str), verification: nullable(isObject) }),
});
const summary = (v: unknown): v is Summary => fields(v, { done: count, total: count, matches: count, differs: count });
export const isAnswer = (v: unknown): v is CalibrationAnswer => fields(v, {
  kit_id: str, test: str, player: str, outcome: str, note: str, conditions, at: str, older_tv_software: bool,
  match: oneOf("matches", "differs", "unsure", "none"),
});
const document = (v: unknown, sub: string): boolean => fields(v, { schema: x => x === `videosuite-calibrate-${sub}/1`, tool_version: str });
export const isPlanDocument = (v: unknown): v is PlanDocument => document(v, "plan") && fields(v, {
  catalogue_version: count, players: strings,
  tests: list((x: unknown): x is PlanTest => fields(x, {
    test: str, title: str, class: testClass, players: strings, manual: bool, instruction: str, question: str,
    outcomes: list((o: unknown): o is { id: string; text: string } => fields(o, { id: str, text: str })),
    prediction: p => isObject(p) && Object.values(p).every(isPrediction),
    source: s => fields(s, { kind: sourceKind, status: oneOf("ready", "needs_real_file", "unavailable"), file: nullable(str), est_bytes: nullable(count), reason: nullable(str) }),
  })),
});
export const isBuildDocument = (v: unknown): v is BuildDocument => document(v, "build") && fields(v, {
  kit_id: str, out: str, descriptor_path: str, total_bytes: count, descriptors: list(isDescriptor),
  unavailable: list((x: unknown): x is { test: string; reason: string } => fields(x, { test: str, reason: str })),
});
export const isSessionDocument = (v: unknown): v is SessionDocument => document(v, "session") && fields(v, { kit_id: str, session: isSession });
export const isRecordDocument = (v: unknown): v is RecordDocument => document(v, "record") && fields(v, { answer: isAnswer, summary });
export const isStatusDocument = (v: unknown): v is StatusDocument => document(v, "status") && fields(v, {
  recovery_required: bool, kit: nullable((x: unknown): x is Kit => fields(x, { id: str, catalogue_version: count, built_at: str, out: str, descriptors: list(isDescriptor) })),
  session: nullable(isSession), answers: list(isAnswer), summary,
});
export const isApplyDocument = (v: unknown): v is ApplyDocument => document(v, "apply") && fields(v, {
  revision: str, applied: bool, recovery_required: bool,
  changes: list((x: unknown): x is ApplyChange => fields(x, { id: str, test: str, player: str, rule: nullable(str), action: oneOf("confirm", "change", "veto", "skip", "undo", "left"), reason: str, previous: any, value: any, veto: bool })),
  conflicts: list((x: unknown): x is ApplyDocument["conflicts"][number] => fields(x, { rule: str, player: str, tests: strings })),
  skipped: list((x: unknown): x is ApplyDocument["skipped"][number] => fields(x, { test: str, player: str, reason: str })),
});
const reportSide = (v: unknown): v is ReportSide => fields(v, { verdict: str, score: num, evidence: str, effective_audio: nullable(str), effective_hdr: nullable(str) });
const reportBest = (v: unknown): v is ReportBestPlayer => fields(v, { player: str, label: str, verdict: str, score: num, default_player: str, switch: bool, reason: str });
export const isReportDocument = (v: unknown): v is ReportDocument => document(v, "report") && fields(v, {
  note: x => x === "playback only, not a quality ranking", kit_active: bool, recovery_required: bool,
  files: list((x: unknown): x is ReportDocument["files"][number] => fields(x, { path: str, player: str, before: reportSide, after: reportSide, changed: bool })),
  best_player_changes: list((x: unknown): x is ReportDocument["best_player_changes"][number] => fields(x, { path: str, device: str, before: reportBest, after: reportBest })),
});
