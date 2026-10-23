import { describe, expect, it } from "vitest";
import { API } from "../config";
import { compareDoc, devicesDoc, legacyVideo, rankDoc, scoreDoc, scorePartialDoc, scoreRecord, suiteSettings } from "./fixtures";
import {
  asQuickDocument, compareAllDoc, comparePlanDoc, quickBothDoc, quickDeviceDoc, quickJustPlayerAlternativesDoc,
  quickOverallDoc, quickPartialDoc, rankDeviceDoc, rankOverallDoc, version4bDoc, versionInfo07,
} from "./fixtures";
import { bodyOf, callsTo, installFetch, jsonResponse, takeUnexpectedFetches, unexpectedFetch } from "./fetchMock";
import { EVIDENCE_WORDS, VERDICTS } from "../types";
import { asQuickDocument as appQuickGuard } from "../../compare/types";

describe("real VideoSuite fixtures", () => {
  it("contains the score fields consumed by the UI", () => {
    const score = scoreDoc();
    expect(score.tool_version).toBe("0.4.0");
    expect(score.records).toHaveLength(3);

    const deviceIds = ["galaxy_tab_s9_ultra", "sony_bravia_8_ii", "vivobook_pro_16x"];
    for (const record of score.records) {
      expect(typeof record.id).toBe("string");
      expect(typeof record.source.path).toBe("string");
      for (const row of Object.values(record.playback)) {
        expect(VERDICTS).toContain(row.verdict);
        expect(typeof row.score).toBe("number");
        expect(row.score_range).toHaveLength(2);
        for (const point of row.score_range) expect(typeof point).toBe("number");
        expect(typeof row.evidence).toBe("string");
        expect(EVIDENCE_WORDS).toContain(row.evidence);
        expect(typeof row.effective_hdr).toBe("string");
        expect(row.effective_audio === null || typeof row.effective_audio === "string").toBe(true);
        expect(Array.isArray(row.issues)).toBe(true);
        for (const issue of row.issues) {
          expect(typeof issue.code).toBe("string");
          expect(typeof issue.severity).toBe("string");
          expect(typeof issue.message).toBe("string");
          expect(typeof issue.confidence).toBe("string");
        }
        expect(Array.isArray(row.hints)).toBe(true);
      }

      expect(record.best_player).toBeDefined();
      if (!record.best_player) throw new Error("score fixture is missing best_player");
      expect(Object.keys(record.best_player)).toEqual(deviceIds);
      for (const id of deviceIds) {
        const best = record.best_player[id];
        expect(best).toBeDefined();
        if (!best) throw new Error(`score fixture is missing best_player for ${id}`);
        expect(typeof best.player).toBe("string");
        expect(typeof best.label).toBe("string");
        expect(typeof best.reason).toBe("string");
        expect(typeof best.switch).toBe("boolean");
      }
    }
  });

  it("contains the partial score source details", () => {
    const source = scorePartialDoc().records[0].source;
    expect(source.partial).toBe(true);
    expect(source.real_name).toBe("Movie.2160p.WEB-DL.DDP5.1.mkv");
    expect(source.caveats).toHaveLength(3);
  });

  it("contains the rank document fields consumed by the UI", () => {
    const rank = rankDoc();
    expect(rank.quality.map((row) => row.label)).toEqual([
      "clip_a_crf18.mkv",
      "clip_c_crf26.mp4",
      "clip_b_crf34.mkv",
    ]);
    expect(Object.keys(rank.devices)).toHaveLength(7);
    for (const device of Object.values(rank.devices)) {
      for (const row of device.ranking) {
        expect(typeof row.playback_score).toBe("number");
        expect(typeof row.verdict).toBe("string");
        expect(row.why === null || typeof row.why === "string").toBe(true);
      }
    }
    expect(rank.devices["sony_bravia_8_ii/builtin_usb"].quality_basis).toBe("neutral");
  });

  it("contains the compare estimate and pairs", () => {
    const compare = compareDoc();
    expect(compare.estimate).toEqual({ pairs: 3, minutes: 12 });
    expect(compare.pairs).toHaveLength(3);
    expect(compare.failed).toBe(0);
  });

  it("contains the ordered device and player fixtures", () => {
    const devices = devicesDoc();
    expect(devices.devices.map((device) => device.id)).toEqual([
      "galaxy_tab_s9_ultra",
      "sony_bravia_8_ii",
      "vivobook_pro_16x",
    ]);
    const sony = devices.devices.find((device) => device.id === "sony_bravia_8_ii");
    expect(sony).toBeDefined();
    if (!sony) throw new Error("devices fixture is missing sony_bravia_8_ii");
    expect(sony.players.map((player) => player.id)).toEqual(["builtin_usb", "kodi", "just_player"]);
    expect(sony.players[0].default).toBe(true);
  });

  it("derives the settings view used by the backend contract", () => {
    expect(suiteSettings()).toEqual({
      tool_version: "0.4.0",
      supported: true,
      path: "C:\\fixtures\\suite\\no_settings.json",
      loaded: false,
      errors: [],
      applied_count: 0,
      devices: {
        sony_bravia_8_ii: {
          label: 'Sony BRAVIA 8 II 65" (K-65XR80M2 / K-65XR8M2), USB drive',
          usb_filesystem: null,
          audio_output: null,
          overrides: [],
        },
        vivobook_pro_16x: {
          label: "ASUS Vivobook Pro 16X OLED M7600QE (Windows 11)",
          usb_filesystem: null,
          audio_output: null,
          overrides: [],
        },
        galaxy_tab_s9_ultra: {
          label: "Samsung Galaxy Tab S9 Ultra (SM-X910 / SM-X916B)",
          usb_filesystem: null,
          audio_output: null,
          overrides: [],
        },
      },
    });
  });

  it("returns independent fixture clones", () => {
    const first = scoreRecord();
    const originalId = scoreRecord().id;
    first.id = "changed";
    expect(scoreRecord().id).toBe(originalId);
  });
});

describe("fetch test helpers", () => {
  it("builds a minimal JSON response and round-trips a request body", async () => {
    const missing = jsonResponse(404, { detail: "Job not found." });
    expect(missing.ok).toBe(false);
    expect(missing.status).toBe(404);
    expect(await missing.text()).toBe('{"detail":"Job not found."}');
    expect(await missing.json()).toEqual({ detail: "Job not found." });

    const fetchMock = installFetch((url) => {
      if (url === API + "/suite/score") return jsonResponse(201, { job_id: "job-1" });
      return unexpectedFetch(url);
    });
    const request = { paths: ["D:\\a & b.mkv"] };
    await globalThis.fetch(API + "/suite/score", { method: "POST", body: JSON.stringify(request) });
    expect(callsTo(fetchMock, "/suite/score")).toHaveLength(1);
    expect(bodyOf(fetchMock, "/suite/score")).toEqual(request);
  });

  it("records swallowed unexpected requests but not deliberate network failures", async () => {
    installFetch((url) => unexpectedFetch(url));
    try {
      await globalThis.fetch(API + "/nope");
    } catch {
      // Simulate application code swallowing the rejected fetch.
    }
    expect(takeUnexpectedFetches()).toEqual(["unexpected fetch /nope"]);

    installFetch(() => {
      throw new TypeError("Failed to fetch");
    });
    try {
      await globalThis.fetch(API + "/offline");
    } catch {
      // Deliberate network failures are allowed by the harness.
    }
    expect(takeUnexpectedFetches()).toEqual([]);
  });
});

describe("legacy fixture helper", () => {
  it("returns the old dashboard video shape", () => {
    expect(legacyVideo().path).toBe("D:\\Media\\clip.mkv");
  });
});

describe("real Plan 4B VideoSuite fixtures", () => {
  it("are VideoSuite 0.7.0 documents with the unified capabilities", () => {
    expect(version4bDoc().tool_version).toBe("0.7.0");
    const info = versionInfo07();
    for (const capability of ["compare_all", "compare_plan", "neutral_only", "record_snapshot", "quick_recommendations",
      "calibration", "offline_payload"]) {
      expect(info.capabilities).toContain(capability);
    }
    expect(comparePlanDoc().schema).toBe("videosuite-compare-plan/1");
    expect(compareAllDoc().coverage).toEqual({ expected_pairs: 3, scheduled_pairs: 3, completed_pairs: 3, missing_pairs: 0 });
    expect(rankOverallDoc().devices).toEqual({});
    expect(Object.keys(rankDeviceDoc().devices)).toEqual([
      "sony_bravia_8_ii/builtin_usb", "sony_bravia_8_ii/kodi", "sony_bravia_8_ii/just_player",
    ]);
  });

  it("keeps the Just Player primary and the alternatives partition through the fixture guard", () => {
    const doc = quickJustPlayerAlternativesDoc();
    expect(doc.selected).toEqual({ device: "sony_bravia_8_ii", player: "just_player" });
    expect(Object.keys(doc.devices)).toEqual(["sony_bravia_8_ii/builtin_usb", "sony_bravia_8_ii/kodi", "sony_bravia_8_ii/just_player"]);
    expect(doc.alternatives.map((a) => [a.device, a.player])).toEqual([
      ["sony_bravia_8_ii", "builtin_usb"], ["sony_bravia_8_ii", "kodi"],
    ]);
    for (const alternative of doc.alternatives) {
      expect(alternative.order).toEqual(doc.devices[`${alternative.device}/${alternative.player}`]);
      for (const group of alternative.order.groups) {
        for (const row of Object.values(group.basis)) {
          expect(EVIDENCE_WORDS).toContain((row.playback as { evidence: string }).evidence);
        }
      }
    }
  });

  it("labels every Quick document as metadata and never carries measured scores", () => {
    for (const doc of [quickOverallDoc(), quickDeviceDoc(), quickBothDoc(), quickPartialDoc()]) {
      expect(doc.measurement).toBe("metadata");
      expect(JSON.stringify([doc.overall, doc.devices, doc.alternatives])).not.toMatch(/quality_score|device_score/);
    }
    expect(quickOverallDoc().devices).toEqual({});
    expect(quickDeviceDoc().overall).toBeNull();
    expect(quickPartialDoc().overall?.winner_ids).toEqual([]);
    const tampered = JSON.parse(JSON.stringify(quickBothDoc())) as Record<string, unknown>;
    ((tampered.overall as { groups: Array<Record<string, unknown>> }).groups[0]).quality_score = 80;
    expect(() => asQuickDocument(tampered)).toThrow(/measured score/);
    const extra = { ...quickBothDoc(), future_suite_key: { kept: true } };
    expect(asQuickDocument(extra)).toBe(extra);
  });

  it("returns independent Plan 4B clones", () => {
    const first = quickBothDoc();
    first.selected.player = "kodi";
    expect(quickBothDoc().selected.player).toBe("just_player");
  });

  it("narrows Quick fixtures with the same guard the Compare screen uses", () => {
    expect(asQuickDocument).toBe(appQuickGuard);
    const doc = quickJustPlayerAlternativesDoc();
    (doc.alternatives[0] as { player: unknown }).player = 5;
    expect(() => asQuickDocument(doc)).toThrow(/device\/player identity/);
  });

  it("preserves the emitted single-player Both selection", () => {
    const doc = quickBothDoc();
    expect(Object.keys(doc.devices)).toEqual(["sony_bravia_8_ii/just_player"]);
    expect(doc.alternatives).toEqual([]);
    expect(doc.records.every((r) => (r.hdr as { formats: string[] }).formats.length === 0)).toBe(true);
  });
});
