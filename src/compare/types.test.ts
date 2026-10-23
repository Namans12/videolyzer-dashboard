import { describe, expect, it } from "vitest";
import {
  asPublicSnapshot, asQuickDocument, asRankDocument, asRunDetail, asRunJob, asRunList, asRunResult, buildRunRequest,
  chooseDevice, ContractViolation, defaultForm, supportedDevices,
} from "./types";
import type { CompareForm } from "./types";
import {
  allDetails, changedSettings, failedPairs, localFullBoth, localJustPlayerAlternatives, localQuickBoth,
  magnetPartialFailure, magnetQuickDevice, magnetSelectionRequired, publicSnapshotsHttp,
} from "./testing/fixtures";
import type { Json } from "./testing/fixtures";
import { devicesDoc, quickBothDoc, quickJustPlayerAlternativesDoc, rankDeviceDoc, rankOverallDoc } from "../suite/testing/fixtures";
import supported from "./supported-devices.json";

const INTERNAL = "C:\\Users\\x\\AppData\\Local\\VideoComparator\\app\\runs\\run-0001\\reports";
const obj = (v: unknown) => v as Json;

describe("unified run contract guards", () => {
  it("accepts real public HTTP snapshots and refuses leaked internal reports_dir", () => {
    const http = publicSnapshotsHttp();
    const list = asRunList(http.list);
    expect(list.runs.map((r) => r.run_id)).toEqual(["run-0001"]);
    const detail = asRunDetail(http.detail);
    expect(detail.result?.snapshot.run_id).toBe(detail.run.run_id);
    expect(asRunResult(obj(http.detail).result).snapshot).toEqual(detail.run);
    const planJob = asRunJob(http.plan_job);
    const runJob = asRunJob(http.run_job);
    expect(planJob.run?.stage).toBe("awaiting_confirmation");
    expect(runJob.results?.schema).toBe("videosuite-comparison-run/1");
    for (const snap of [list.runs[0], detail.run, detail.result!.snapshot, planJob.run!, runJob.run!]) {
      expect(Object.keys(snap)).not.toContain("reports_dir");
    }

    // The internal-only field fails wherever it appears, even with an otherwise valid absolute value.
    const leaks: Array<[string, () => unknown]> = [
      ["list", () => { const h = publicSnapshotsHttp(); ((obj(h.list).runs as Json[])[0]).reports_dir = INTERNAL; return asRunList(h.list); }],
      ["detail.run", () => { const h = publicSnapshotsHttp(); obj(obj(h.detail).run).reports_dir = INTERNAL; return asRunDetail(h.detail); }],
      ["result.snapshot", () => { const h = publicSnapshotsHttp(); obj(obj(obj(h.detail).result).snapshot).reports_dir = INTERNAL; return asRunDetail(h.detail); }],
      ["plan_job.run", () => { const h = publicSnapshotsHttp(); obj(obj(h.plan_job).run).reports_dir = INTERNAL; return asRunJob(h.plan_job); }],
      ["run_job.results.snapshot", () => { const h = publicSnapshotsHttp(); obj(obj(obj(h.run_job).results).snapshot).reports_dir = INTERNAL; return asRunJob(h.run_job); }],
      ["snapshot", () => { const h = publicSnapshotsHttp(); const run = obj(obj(h.detail).run); run.reports_dir = INTERNAL; return asPublicSnapshot(run); }],
    ];
    for (const [where, leak] of leaks) expect(leak, where).toThrow(ContractViolation);
  });

  it("ingests every actual U5/U6 envelope and never casts Quick as a rank document", () => {
    for (const [name, raw] of allDetails()) {
      const detail = asRunDetail(raw);
      expect(detail.run.run_id, name).toMatch(/^run-/);
      const result = detail.result;
      if (result === null) { expect(detail.run.stage, name).toBe("selection_required"); continue; }
      if (result.measurement === "metadata") {
        if (result.device !== null) {
          expect(result.device.schema, name).toBe("videosuite-quick/1");
          expect(() => asRankDocument(result.device), name).toThrow(ContractViolation);
        }
      } else {
        for (const doc of [result.overall, result.device]) if (doc !== null) expect(doc.schema, name).toBe("videosuite-rank/1");
      }
    }
    const failed = asRunDetail(failedPairs().run);
    expect(failed.run.stage).toBe("error");
    expect(failed.result?.coverage).toMatchObject({ complete: false, missing_pairs: 1 });
    const changed = asRunJob(changedSettings().job);
    expect(changed.error_kind).toBe("settings_changed");
    expect(changed.results?.overall).toMatchObject({ schema: "videosuite-rank/1" });
    expect(changed.results?.device).toBeNull();
    expect(changed.results?.coverage.complete).toBe(true);
    expect(changed.results?.failures.map((f) => f.kind)).toEqual(["settings_changed"]);
    const partial = asRunDetail(magnetPartialFailure());
    expect(partial.result?.coverage.complete).toBe(false);
    expect(partial.result?.failures.every((f) => f.candidate_id === "c1")).toBe(true);
    expect(asRunDetail(magnetSelectionRequired()).run.candidates[0].entries?.map((e) => e.index)).toEqual([0, 2]);
  });

  it("accepts the real U4 suite documents and their additive keys", () => {
    expect(asQuickDocument(quickJustPlayerAlternativesDoc()).selected.player).toBe("just_player");
    expect(asQuickDocument({ ...quickBothDoc(), future_key: 1 }).results).toBe("both");
    expect(asRankDocument(rankOverallDoc()).devices).toEqual({});
    expect(Object.keys(asRankDocument(rankDeviceDoc()).devices)).toHaveLength(3);
    const measured = quickBothDoc() as unknown as Json;
    obj((obj(measured.overall).groups as Json[])[0]).quality_score = 80;
    expect(() => asQuickDocument(measured)).toThrow(/measured score/);
  });

  it.each([
    ["missing stage", (d: Json) => { delete obj(d.run).stage; }],
    ["unknown stage", (d: Json) => { obj(d.run).stage = "quick"; }],
    ["non-string run id", (d: Json) => { obj(d.run).run_id = 7; }],
    ["coverage claims complete with missing pairs", (d: Json) => { Object.assign(obj(obj(d.result).coverage), { complete: true, missing_pairs: 1, completed_pairs: 2 }); }],
    ["metadata answer labelled as pixels", (d: Json) => { obj(d.result).measurement = "pixels"; }],
    ["absolute report url", (d: Json) => { ((obj(d.result).reports as Json[])[0]).url = "file:///C:/reports/a.json"; }],
    ["report from another run", (d: Json) => { ((obj(d.result).reports as Json[])[0]).url = "/comparison/runs/run-9999/reports/4986a3f0e83439af"; }],
    ["evidence word outside the vocabulary", (d: Json) => {
      const device = obj(obj(d.result).device);
      const key = Object.keys(obj(device.devices))[0];
      const group = (obj(obj(device.devices)[key]).groups as Json[])[0];
      obj(obj(Object.values(obj(group.basis))[0]).playback).evidence = "probably";
    }],
  ])("rejects a mutated required field: %s", (name, mutate) => {
    const source = name === "metadata answer labelled as pixels" || name === "evidence word outside the vocabulary"
      ? localJustPlayerAlternatives() : localFullBoth();
    expect(() => asRunDetail(source)).not.toThrow();
    mutate(source);
    expect(() => asRunDetail(source)).toThrow(ContractViolation);
  });

  // A required nested value of the wrong type must be refused here, not reach the renderer (R-C Important 3).
  const rankDevice = (d: Json) => {
    const device = obj(obj(d.result).device);
    const key = Object.keys(obj(device.devices))[0];
    return obj(obj(device.devices)[key]);
  };
  it.each([
    ["device_score is text", (d: Json) => { ((rankDevice(d).ranking as Json[])[0]).device_score = "invalid"; }],
    ["device_rank is text", (d: Json) => { ((rankDevice(d).ranking as Json[])[0]).device_rank = "1"; }],
    ["playback_score missing", (d: Json) => { delete ((rankDevice(d).ranking as Json[])[0]).playback_score; }],
    ["playback_score is not finite", (d: Json) => { ((rankDevice(d).ranking as Json[])[0]).playback_score = "70"; }],
    ["verdict missing", (d: Json) => { delete ((rankDevice(d).ranking as Json[])[0]).verdict; }],
    ["quality_score is text", (d: Json) => { ((rankDevice(d).ranking as Json[])[0]).quality_score = "88.1"; }],
    ["score_range holds text", (d: Json) => { ((rankDevice(d).ranking as Json[])[0]).score_range = ["a", "b"]; }],
    ["reasons is not a list", (d: Json) => { ((rankDevice(d).ranking as Json[])[0]).reasons = "none"; }],
    ["why is a number", (d: Json) => { ((rankDevice(d).ranking as Json[])[0]).why = 3; }],
    ["crossing_note is a number", (d: Json) => { rankDevice(d).crossing_note = 5; }],
    ["format credit measured holds text", (d: Json) => {
      rankDevice(d).format_credit_removed = [{ a: "A", b: "B", rule: "r", shown_as: "sdr", measured: ["x", 1], winner: null }];
    }],
    ["overall quality games is text", (d: Json) => { ((obj(obj(d.result).overall).quality as Json[])[0]).games = "2"; }],
    ["overall tool_version missing", (d: Json) => { delete obj(obj(d.result).overall).tool_version; }],
    ["stale report without names", (d: Json) => { obj(obj(d.result).overall).stale_reports = [{ reason: "x" }]; }],
  ])("refuses an invalid required rank field from the real Full result: %s", (_name, mutate) => {
    const source = localFullBoth();
    expect(() => asRunDetail(source)).not.toThrow();
    mutate(source);
    expect(() => asRunDetail(source)).toThrow(ContractViolation);
  });

  it.each([
    ["tool_version", (q: Json) => { delete q.tool_version; }],
    ["failures", (q: Json) => { delete q.failures; }],
    ["settings", (q: Json) => { delete q.settings; }],
    ["failure without reason", (q: Json) => { q.failures = [{ path: "D:\\A.mkv" }]; }],
    ["records holding text", (q: Json) => { q.records = ["x"]; }],
  ])("refuses a real Quick document missing required %s", (_name, mutate) => {
    const source = localQuickBoth();
    expect(() => asRunDetail(source)).not.toThrow();
    mutate(obj(obj(source.result).device));
    expect(() => asRunDetail(source)).toThrow(ContractViolation);
  });

  it("keeps the request/snapshot pairing honest", () => {
    const d = localQuickBoth();
    obj(obj(obj(d.result).snapshot).request).primary_player = "kodi";
    expect(() => asRunDetail(d)).toThrow(ContractViolation);
    const m = magnetQuickDevice();
    obj(obj(m.run).conditions).primary_player = "kodi";
    expect(() => asRunDetail(m)).toThrow(ContractViolation);
  });
});

describe("run request construction", () => {
  const devices = devicesDoc();
  const filesForm = (patch: Partial<CompareForm> = {}): CompareForm => ({
    ...defaultForm("files", devices), pathsText: "D:\\Movies\\A.mkv\nD:\\Movies\\B.mkv", ...patch,
  });

  it("derives the evaluation player from the alternatives checkbox and keeps the primary separate", () => {
    const withAlternatives = buildRunRequest(filesForm({ primaryPlayer: "just_player", includeAlternatives: true }));
    expect(withAlternatives).toMatchObject({ device: "sony_bravia_8_ii", primary_player: "just_player", player: "all", include_alternatives: true });
    const without = buildRunRequest(filesForm({ primaryPlayer: "just_player", includeAlternatives: false }));
    expect(without).toMatchObject({ primary_player: "just_player", player: "just_player", include_alternatives: false });
    const overall = buildRunRequest(filesForm({ results: "overall", primaryPlayer: "just_player", fs: "ntfs", audioOutput: "avr" }));
    expect(overall).toMatchObject({ results: "overall", device: null, primary_player: null, player: null, fs: null, audio_output: null });
    expect(Object.keys(overall).sort()).toEqual([
      "audio_output", "depth", "device", "fs", "include_alternatives", "input_kind", "lang", "paths", "player", "primary_player", "results",
    ]);
  });

  it("builds magnet requests with the explicitly chosen file entries", () => {
    const aa = `magnet:?xt=urn:btih:${"a".repeat(40)}`;
    const bb = `magnet:?xt=urn:btih:${"B".repeat(32)}`;          // base32 form
    const form: CompareForm = { ...defaultForm("magnets", devices), magnetsText: `${aa}\n${bb}`, fileChoices: { [bb]: 2 } };
    expect(buildRunRequest(form)).toMatchObject({
      input_kind: "magnets", depth: "quick",
      magnets: [{ uri: aa, file_index: null }, { uri: bb, file_index: 2 }],
    });
    expect(buildRunRequest(form)).not.toHaveProperty("paths");
  });

  it("takes device conditions from the chosen device only", () => {
    const tv = filesForm({ fs: "fat32", audioOutput: "soundbar" });
    const laptop = chooseDevice(tv, "vivobook_pro_16x", devices);
    expect(laptop).toMatchObject({ device: "vivobook_pro_16x", fs: null, audioOutput: null });
    expect(buildRunRequest(laptop)).toMatchObject({ device: "vivobook_pro_16x", fs: null, audio_output: null });
    expect(chooseDevice(tv, "sony_bravia_8_ii", devices)).toMatchObject({ fs: "fat32", audioOutput: "soundbar" });  // same device kept
  });

  it("offers exactly the fixed supported devices that the real catalogue knows", () => {
    expect(supported.devices.map((d) => d.id)).toEqual(["sony_bravia_8_ii", "vivobook_pro_16x", "galaxy_tab_s9_ultra"]);
    const doc = devicesDoc();
    doc.devices.push({ ...doc.devices[0], id: "unknown_projector", label: "Unknown projector" });
    const options = supportedDevices(doc);
    expect(options.map((o) => o.id)).toEqual(["sony_bravia_8_ii", "vivobook_pro_16x", "galaxy_tab_s9_ultra"]);
    expect(options[0].defaultPlayer).toBe("builtin_usb");
    expect(options[0].players.map((p) => p.id)).toEqual(["builtin_usb", "kodi", "just_player"]);
    const missing = devicesDoc();
    missing.devices = missing.devices.filter((d) => d.id !== "vivobook_pro_16x");
    expect(supportedDevices(missing).map((o) => o.id)).toEqual(["sony_bravia_8_ii", "galaxy_tab_s9_ultra"]);
    expect(supportedDevices(null)).toEqual([]);
  });
});
