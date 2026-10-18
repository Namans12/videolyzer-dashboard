import { describe, expect, it } from "vitest";
import { isApplyDocument, isBuildDocument, isPlanDocument, isRecordDocument, isReportDocument, isSessionDocument, isStatusDocument, own } from "./types";

// Small contract fixtures; each call returns fresh data, including offline descriptors.
export function envelope(sub: string) { return { schema: `videosuite-calibrate-${sub}/1`, tool_version: "0.5.0" }; }
export function descriptor(test = "T01") {
  return { test, player: "builtin_usb", title: "Banner", class: "decisive", rule: "hdr_map.hdr10",
    instruction: "Play the clip", question: "Which banner?", outcomes: [
      { id: "yes", text: "HDR", value: { out: "hdr10", risk: false, qoe: "KEEP" } },
      { id: "no", text: "SDR", value: "unsupported" }, { id: "unsure", text: "Unsure", value: null },
      { id: "skipped", text: "Skipped", value: null }], conditions: ["audio_output"], manual: false,
    file: "clip.mkv", source: { kind: "invented", file: null, verification: { status: "verified", sha256: "abc", bytes: 10 } },
    prediction: { outcome: "yes", text: "HDR", evidence: "inferred" } };
}
export function session() { return { tv_software: "v1", audio_output: "tv_speakers", usb_filesystem: "exfat", confirmed_at: "2026-10-17T00:00:00Z" }; }
export function answer(test = "T01", outcome = "yes", kit = "A") {
  return { kit_id: kit, test, player: "builtin_usb", outcome, note: "", conditions: { tv_software: "v1", audio_output: "tv_speakers", usb_filesystem: "exfat" },
    at: "2026-10-17T01:00:00Z", older_tv_software: false, match: outcome === "yes" ? "matches" : "differs" };
}
export function statusDoc(kit = "A") {
  return { ...envelope("status"), recovery_required: false,
    kit: { id: kit, catalogue_version: 1, built_at: "2026-10-17T00:00:00Z", out: "D:\\Kit", descriptors: [descriptor(), descriptor("T02")] },
    session: session(), answers: [answer("T01", "yes", kit)], summary: { done: 1, total: 2, matches: 1, differs: 0 } };
}
export function recordDoc(test = "T01", outcome = "no", kit = "A") {
  return { ...envelope("record"), answer: answer(test, outcome, kit), summary: { done: 1, total: 2, matches: 0, differs: 1 } };
}
export function applyDoc() { return { ...envelope("apply"), revision: "rev1", applied: false, recovery_required: false, changes: [], conflicts: [], skipped: [] }; }
export function planDoc() {
  const d = descriptor();
  return { ...envelope("plan"), catalogue_version: 1, players: [d.player], tests: [{ ...d, players: [d.player],
    prediction: { [d.player]: d.prediction }, source: { kind: "invented", status: "ready", file: null, est_bytes: 100, reason: null } }] };
}
export function buildDoc() { return { ...envelope("build"), kit_id: "A", out: "D:\\Kit", descriptor_path: "D:\\Kit\\kit.json", total_bytes: 10, descriptors: [descriptor()], unavailable: [] }; }
export function reportDoc() { return { ...envelope("report"), note: "playback only, not a quality ranking", kit_active: false, recovery_required: false, files: [], best_player_changes: [] }; }

describe("calibration document guards", () => {
  it.each([
    [isPlanDocument, planDoc], [isBuildDocument, buildDoc], [isStatusDocument, statusDoc],
    [isRecordDocument, recordDoc], [isApplyDocument, applyDoc], [isReportDocument, reportDoc],
    [isSessionDocument, () => ({ ...envelope("session"), kit_id: "A", session: session() })],
  ])("accepts the frozen shape and rejects missing/inherited envelope fields", (guard, fixture) => {
    const doc = fixture(); expect(guard(doc)).toBe(true);
    expect(guard({ ...doc, extra: "additive" })).toBe(true);
    expect(guard(null)).toBe(false); expect(guard([])).toBe(false);
    expect(guard({ ...doc, schema: "wrong" })).toBe(false);
    expect(guard(Object.create(doc))).toBe(false);
    expect(guard({ ...doc, tool_version: undefined })).toBe(false);
  });
  it("accepts record without recovery_required and tolerates its presence", () => {
    expect(isRecordDocument(recordDoc())).toBe(true);
    expect(isRecordDocument({ ...recordDoc(), recovery_required: false })).toBe(true);
  });
  it("checks nested descriptors, conditions, prediction evidence and answers", () => {
    const doc = statusDoc();
    expect(isStatusDocument({ ...doc, kit: { ...doc.kit, descriptors: [{ ...descriptor(), prediction: { outcome: "yes", text: "HDR" } }] } })).toBe(false);
    expect(isStatusDocument({ ...doc, answers: [{ ...answer(), conditions: {} }] })).toBe(false);
    expect(isStatusDocument({ ...doc, summary: { ...doc.summary, done: "1" } })).toBe(false);
    expect(isStatusDocument({ ...doc, kit: null, session: null, answers: [] })).toBe(true);
  });
  it("never resolves inherited backend string keys", () => {
    expect(own({}, "constructor")).toBeUndefined(); expect(own({}, "__proto__")).toBeUndefined();
    expect(own(JSON.parse('{"__proto__":{"text":"literal"}}'), "__proto__")).toEqual({ text: "literal" });
    expect(isPlanDocument({ ...planDoc(), tests: [{ ...planDoc().tests[0], prediction: { constructor: "bad" } }] })).toBe(false);
  });
  it("validates report sides and apply changes rather than trusting arrays", () => {
    expect(isReportDocument({ ...reportDoc(), files: [{}] })).toBe(false);
    expect(isApplyDocument({ ...applyDoc(), changes: [{}] })).toBe(false);
    expect(isBuildDocument({ ...buildDoc(), unavailable: [{}] })).toBe(false);
  });
  it("retains recovery, manual/offline descriptors, nullable predictions, and older software flags", () => {
    const d = { ...descriptor(), class: "informational", manual: true, rule: null, file: null,
      source: { kind: "manual", file: null, verification: null }, prediction: { outcome: null, text: "Unknown", evidence: "unknown" } };
    const status = { ...statusDoc(), recovery_required: true, answers: [{ ...answer(), older_tv_software: true }], kit: { ...statusDoc().kit, descriptors: [d] } };
    expect(isStatusDocument(status)).toBe(true);
    expect(isStatusDocument({ ...status, recovery_required: undefined })).toBe(false);
    expect(isApplyDocument({ ...applyDoc(), recovery_required: true })).toBe(true);
    expect(isReportDocument({ ...reportDoc(), recovery_required: true })).toBe(true);
  });
  it("accepts complete playback report rows and veto/conflict/skip changes", () => {
    const side = { verdict: "PLAYS_DEGRADED", score: 30, evidence: "owner", effective_audio: null, effective_hdr: "HDR10" };
    const best = { player: "kodi", label: "Kodi", verdict: "PLAYS_WELL", score: 95, default_player: "builtin_usb", switch: true, reason: "Audio decodes" };
    expect(isReportDocument({ ...reportDoc(), kit_active: true, files: [{ path: "D:\\a.mkv", player: "builtin_usb", before: side, after: side, changed: false }],
      best_player_changes: [{ path: "D:\\a.mkv", device: "bravia", before: best, after: best }] })).toBe(true);
    expect(isApplyDocument({ ...applyDoc(), changes: [{ id: "T01:builtin_usb:audio.test", test: "T01", player: "builtin_usb", rule: "audio.test", action: "veto", reason: "Observed silence", previous: ["decode"], value: ["silent"], veto: true }],
      conflicts: [{ rule: "audio.test", player: "builtin_usb", tests: ["T01", "T02"] }], skipped: [{ test: "T02", player: "builtin_usb", reason: "conflict" }] })).toBe(true);
  });
});
