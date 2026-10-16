import { describe, expect, it } from "vitest";
import type { Issue } from "./types";
import { devicesDoc, scorePartialDoc, scoreRecord } from "./testing/fixtures";
import {
  AUDIO_NAMES, EVIDENCE_LEGEND_TEXT, EVIDENCE_MEANING, FS_NAMES, HDR_NOT_VERIFIED, PARTIAL_TEXT,
  basename, bestPlayerHeadline, deviceName, evidenceLabel, evidenceWord, foldIssues,
  formatCreditText, groupPlayback, issueLine, recordName, scoreText, sortIssues,
  unverifiedNotice, usbLine, verdictLabel,
} from "./verdict";

const issue = (code: string, severity: string, confidence = "official"): Issue => ({
  code, severity, confidence, message: code,
});

describe.each(["constructor", "toString", "__proto__", "hasOwnProperty"])("prototype-property input %s", (value) => {
  it("preserves an unknown verdict", () => {
    expect(verdictLabel(value)).toBe(value);
  });

  it("preserves an unknown device name", () => {
    expect(deviceName(value)).toBe(value);
  });

  it("preserves an unknown shown-as format", () => {
    expect(formatCreditText({ a: "A", b: "B", rule: "DYNAMIC_RANGE", shown_as: value,
      measured: [null, null], winner: null })).toBe(
      `Format credit not counted on this player: A vs B (DYNAMIC_RANGE; the winning format is shown as ${value}; no measured scores, so a tie)`,
    );
  });

  it("preserves an unknown USB file system", () => {
    const row = scoreRecord(0).playback["sony_bravia_8_ii/builtin_usb"];
    expect(usbLine({ ...row, usb_filesystem: value, usb_filesystem_source: null }))
      .toBe(`USB file system: ${value}`);
  });

  it("normalizes evidence before looking up the notice", () => {
    expect(evidenceWord(value)).toBe("unknown");
    expect(evidenceLabel(value)).toBe("evidence: unknown");
    expect(unverifiedNotice(value)).toBe("Unverified rules: at least one rule used has no information behind it (unknown evidence). Confirm it with your own playback test.");
  });

  it("does not inherit a best player for an unknown device", () => {
    const row = scoreRecord(0).playback["sony_bravia_8_ii/builtin_usb"];
    const record = { ...scoreRecord(0), playback: { [`${value}/player`]: row }, best_player: {} };
    const groups = groupPlayback(record, null);
    expect(groups[0].best).toBeNull();
    expect(groups[0].deviceLabel).toBe(value);
    expect(groups[0].players[0].playerLabel).toBe("player");
  });
});

describe("issue ordering and HDR folding", () => {
  it("uses fusion severity order, preserves ties and leaves the input untouched", () => {
    const input = [issue("a", "info"), issue("b", "odd"), issue("c", "veto"),
      issue("d", "risk"), issue("e", "warn"), issue("f", "info"), issue("g", "odd")];
    const before = [...input];
    expect(sortIssues(input).map((i) => i.code)).toEqual(["c", "e", "d", "a", "f", "b", "g"]);
    expect(input).toEqual(before);
    expect(sortIssues([])).toEqual([]);
  });

  it("folds the real Kodi HDR notes while retaining the other sorted issues", () => {
    const input = scoreRecord(0).playback["sony_bravia_8_ii/kodi"].issues;
    const before = structuredClone(input);
    const folded = foldIssues(input);
    expect(folded.hdrNote).toBe("HDR path not verified (inferred)");
    expect(folded.issues.map((i) => `${i.severity}:${i.code}`)).toEqual([
      "warn:no_audio_tracks", "info:video_codec_note", "info:container_note", "info:unverified_path",
    ]);
    expect(input).toEqual(before);
    expect(foldIssues(scoreRecord(0).playback["sony_bravia_8_ii/builtin_usb"].issues).hdrNote).toBeNull();
  });

  it("uses the weakest folded confidence and treats n/a as unknown", () => {
    expect(foldIssues([issue("hdr_uncertain", "info", "forum"),
      issue("hdr_uncertain", "info", "n/a")]).hdrNote).toBe("HDR path not verified (unknown)");
    expect(HDR_NOT_VERIFIED).toBe("HDR path not verified");
    expect(foldIssues([])).toEqual({ issues: [], hdrNote: null });
  });

  it("keeps higher-severity HDR issues and folds only info", () => {
    const warn = issue("hdr_uncertain", "warn", "forum");
    expect(foldIssues([warn, issue("hdr_uncertain", "info", "inferred")])).toEqual({
      issues: [warn], hdrNote: "HDR path not verified (inferred)",
    });
    expect(foldIssues([warn])).toEqual({ issues: [warn], hdrNote: null });
  });

  it("keeps n/a confidence in the CLI issue line", () => {
    expect(issueLine({ severity: "warn", confidence: "n/a", message: "the file has no audio track", code: "x" }))
      .toBe("[warn/n/a] the file has no audio track");
  });
});

describe("verdict and evidence wording", () => {
  it.each([
    ["PLAYS_WELL", "Plays well"], ["PLAYS_DEGRADED", "Plays with a loss"],
    ["RISKY", "May not play"], ["WONT_PLAY", "Will not play"], ["ODD", "ODD"],
  ])("labels verdict %s", (value, label) => expect(verdictLabel(value)).toBe(label));

  it.each(["official", "owner", "reviewed", "forum", "inferred", "unknown"])("preserves evidence %s", (word) => {
    expect(evidenceWord(word)).toBe(word);
    expect(evidenceLabel(word)).toBe(`evidence: ${word}`);
  });

  it.each([null, undefined, "rumour", 42, {}])("normalizes unknown evidence %j", (value) => {
    expect(evidenceWord(value)).toBe("unknown");
    expect(evidenceLabel(value)).toBe("evidence: unknown");
    expect(unverifiedNotice(value)).toBe("Unverified rules: at least one rule used has no information behind it (unknown evidence). Confirm it with your own playback test.");
  });

  it.each(["official", "owner", "reviewed"])("has no unverified notice for %s", (word) => {
    expect(unverifiedNotice(word)).toBeNull();
  });

  it.each([
    ["forum", "Unverified rules: this result rests on user reports (forum evidence). Confirm it with your own playback test."],
    ["inferred", "Unverified rules: this result rests on our deduction (inferred evidence). Confirm it with your own playback test."],
    ["unknown", "Unverified rules: at least one rule used has no information behind it (unknown evidence). Confirm it with your own playback test."],
  ])("shows the exact unverified notice for %s", (word, notice) => expect(unverifiedNotice(word)).toBe(notice));

  it("exports the verbatim evidence legend and meanings", () => {
    expect(EVIDENCE_LEGEND_TEXT).toBe("Evidence (weakest rule used): official = the maker's documentation; owner = tested by you; reviewed = a professional review; forum = user reports; inferred = our deduction; unknown = no information. A score range (for example 69-99) means the result is uncertain.");
    expect(EVIDENCE_MEANING).toEqual({ official: "the maker's documentation", owner: "tested by you",
      reviewed: "a professional review", forum: "user reports", inferred: "our deduction", unknown: "no information" });
  });
});

describe("score and format credit wording", () => {
  it.each([
    [58.0, [58, 58], "58"], [84.4, [69.2, 99], "84 (range 69-99)"],
    [70, undefined, "70"], [58.3, [58.1, 58.4], "58"], [70, [], "70"], [70, [1], "70"],
  ])("formats score %s with range %j", (score, range, text) => expect(scoreText(score, range)).toBe(text));

  it.each([
    [{ a: "A.mkv", b: "B.mkv", rule: "DYNAMIC_RANGE", shown_as: "hdr10", measured: [60, 70], winner: "B.mkv" },
      "Format credit not counted on this player: A.mkv vs B.mkv (DYNAMIC_RANGE; the winning format is shown as HDR10; measured picture 60.0 vs 70.0, B.mkv is ahead)"],
    [{ a: "A.mkv", b: "B.mkv", rule: "DYNAMIC_RANGE", shown_as: "hdr10", measured: [60, 60.6], winner: null },
      "Format credit not counted on this player: A.mkv vs B.mkv (DYNAMIC_RANGE; the winning format is shown as HDR10; measured picture 60.0 vs 60.6, a tie)"],
    [{ a: "A.mkv", b: "B.mkv", rule: "DV_QUALITY", shown_as: "sdr", measured: [null, 61], winner: null },
      "Format credit not counted on this player: A.mkv vs B.mkv (DV_QUALITY; the winning format is shown as SDR; no measured scores, so a tie)"],
  ])("explains removed format credit %j", (detail, text) => expect(formatCreditText(detail)).toBe(text));
});

describe("player, file and USB wording", () => {
  it("describes keeping, switching and having no playable player", () => {
    const best = scoreRecord(0).best_player!.sony_bravia_8_ii;
    expect(bestPlayerHeadline(best)).toBe("Keep the default player: Sony USB media player (built-in)");
    expect(bestPlayerHeadline({ ...best, switch: true, label: "Kodi on the TV (Dolby Vision compatibility mode on)" }))
      .toBe("Better player: Kodi on the TV (Dolby Vision compatibility mode on)");
    expect(bestPlayerHeadline({ ...best, verdict: "WONT_PLAY", switch: true })).toBe("No player on this device will play it");
    expect(deviceName("sony_bravia_8_ii")).toBe("Sony BRAVIA 8 II");
    expect(deviceName("x")).toBe("x");
  });

  it("describes the USB filesystem and its source", () => {
    const row = scoreRecord(0).playback["sony_bravia_8_ii/builtin_usb"];
    expect(usbLine({ ...row, usb_filesystem: null })).toBeNull();
    expect(usbLine({ ...row, usb_filesystem: "exfat", usb_filesystem_source: "settings" })).toBe("USB file system: exFAT (from your settings)");
    expect(usbLine({ ...row, usb_filesystem: "ntfs", usb_filesystem_source: "cli" })).toBe("USB file system: NTFS (chosen for this run)");
    expect(usbLine({ ...row, usb_filesystem: "odd", usb_filesystem_source: null })).toBe("USB file system: odd");
    expect(FS_NAMES).toEqual({ fat32: "FAT32", exfat: "exFAT", ntfs: "NTFS" });
    expect(AUDIO_NAMES).toEqual({ tv_speakers: "TV speakers", soundbar: "Soundbar", avr: "AV receiver" });
  });

  it("uses real names, Windows or POSIX basenames, and finally the record id", () => {
    expect(recordName(scoreRecord(0))).toBe("clip_a_crf18.mkv");
    expect(recordName(scorePartialDoc().records[0])).toBe("Movie.2160p.WEB-DL.DDP5.1.mkv");
    expect(basename("D:/a/b.mkv")).toBe("b.mkv");
    expect(basename("D:\\a\\b.mkv")).toBe("b.mkv");
    expect(recordName({ ...scoreRecord(0), source: { path: "" } })).toBe(scoreRecord(0).id);
    expect(PARTIAL_TEXT).toBe("Partial download: only part of the file was probed, so some facts are not verified.");
  });
});

describe("playback grouping", () => {
  const ids = ["galaxy_tab_s9_ultra", "sony_bravia_8_ii", "vivobook_pro_16x"];

  it("uses devices-document order, labels, defaults and the best player", () => {
    const devices = devicesDoc();
    const groups = groupPlayback(scoreRecord(0), devices);
    expect(groups.map((g) => g.deviceId)).toEqual(ids);
    const sony = groups.find((g) => g.deviceId === "sony_bravia_8_ii")!;
    expect(sony.players.map((p) => p.playerId)).toEqual(["builtin_usb", "kodi", "just_player"]);
    expect(sony.players.map((p) => p.playerLabel)).toEqual(devices.devices.find((d) => d.id === sony.deviceId)!.players.map((p) => p.label));
    expect(sony.players.map((p) => p.isDefault)).toEqual([true, false, false]);
    expect(sony.best?.player).toBe("builtin_usb");
    expect(sony.players.map((p) => p.key)).toEqual(["sony_bravia_8_ii/builtin_usb", "sony_bravia_8_ii/kodi", "sony_bravia_8_ii/just_player"]);
  });

  it("uses key order and static device names without a devices document", () => {
    const groups = groupPlayback(scoreRecord(0), null);
    expect(groups.map((g) => g.deviceId)).toEqual(ids);
    const sony = groups.find((g) => g.deviceId === "sony_bravia_8_ii")!;
    expect(sony.deviceLabel).toBe("Sony BRAVIA 8 II");
    expect(sony.players[0].playerLabel).toBe("builtin_usb");
    expect(sony.players.every((p) => !p.isDefault)).toBe(true);
  });

  it("accepts empty or missing best_player", () => {
    const record = scoreRecord(0);
    record.best_player = {};
    expect(groupPlayback(record, devicesDoc()).every((g) => g.best === null)).toBe(true);
    delete record.best_player;
    expect(groupPlayback(record, devicesDoc()).every((g) => g.best === null)).toBe(true);
  });
});
