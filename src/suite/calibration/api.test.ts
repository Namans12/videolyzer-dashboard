import { describe, expect, it } from "vitest";
import { postPlan, getStatus, postSession, postRecord, postApply, postUndo, startBuild, startReport } from "./api";
import { API } from "../config";
import { bodyOf, installFetch, jsonResponse, unexpectedFetch } from "../testing/fetchMock";

const envelope = (sub: string) => ({ schema: `videosuite-calibrate-${sub}/1`, tool_version: "0.5.0" });
const session = () => ({ tv_software: "v1", audio_output: "tv_speakers", usb_filesystem: "exfat", confirmed_at: "2026-10-17T00:00:00Z" });
const statusDoc = () => ({ ...envelope("status"), recovery_required: false, kit: null, session: null, answers: [], summary: { done: 0, total: 0, matches: 0, differs: 0 } });
const planDoc = () => ({ ...envelope("plan"), catalogue_version: 1, players: [], tests: [] });
const applyDoc = () => ({ ...envelope("apply"), revision: "r1", applied: false, recovery_required: false, changes: [], conflicts: [], skipped: [] });
const recordDoc = () => ({ ...envelope("record"), answer: { kit_id: "A", test: "T01", player: "builtin_usb", outcome: "no", note: "", conditions: { tv_software: "v1", audio_output: "tv_speakers", usb_filesystem: "exfat" }, at: "2026-10-17T01:00:00Z", older_tv_software: false, match: "differs" }, summary: { done: 1, total: 1, matches: 0, differs: 1 } });

describe("calibration HTTP client", () => {
  it("preserves JSON paths, sends exact request keys, strips every note line separator", async () => {
    const path = 'D:\\Movies & 100%\\Кино "a".mkv';
    const cases = [
      ["plan", () => postPlan({ paths: [path] }), { paths: [path] }, planDoc()],
      ["session", () => postSession({ out: "D:\\USB kit", tv_software: "v1", audio_output: "tv_speakers", usb_filesystem: "exfat" }), { out: "D:\\USB kit", tv_software: "v1", audio_output: "tv_speakers", usb_filesystem: "exfat" }, { ...envelope("session"), kit_id: "A", session: session() }],
      ["record", () => postRecord({ kit: "A", test: "T01", player: "builtin_usb", outcome: "no", note: "a\r\nb\nc\u0085d\u2028e\u2029f" }), { kit: "A", test: "T01", player: "builtin_usb", outcome: "no", note: "a b c d e f" }, recordDoc()],
      ["apply", () => postApply({ kit: "A", preview: true, revision: null, confirm_veto: [] }), { kit: "A", preview: true, revision: null, confirm_veto: [] }, applyDoc()],
      ["undo", () => postUndo({ kit: "A" }), { kit: "A" }, applyDoc()],
      ["build", () => startBuild({ paths: [path], out: "D:\\Kit" }), { paths: [path], out: "D:\\Kit" }, { job_id: "build" }],
      ["report", () => startReport({ kit: "A", paths: [path] }), { kit: "A", paths: [path] }, { job_id: "report" }],
    ] as const;
    for (const [name, call, body, response] of cases) {
      const route = `/suite/calibration/${name}`;
      const fetch = installFetch(url => url === API + route ? jsonResponse(200, response) : unexpectedFetch(url));
      await call(); expect(bodyOf(fetch, route)).toEqual(body);
      expect(fetch.mock.calls[0][1]?.method).toBe("POST");
    }
  });
  it("encodes status kit and forwards abort signals", async () => {
    const signal = new AbortController().signal;
    const fetch = installFetch(url => url === `${API}/suite/calibration/status?kit=a%2Fb` ? jsonResponse(200, statusDoc()) : unexpectedFetch(url));
    await getStatus("a/b", signal); expect(fetch.mock.calls[0][1]?.signal).toBe(signal);
  });
  it("allows the latest offline status without a kit query", async () => {
    installFetch(url => url === `${API}/suite/calibration/status` ? jsonResponse(200, statusDoc()) : unexpectedFetch(url));
    await expect(getStatus()).resolves.toEqual(statusDoc());
  });
  it("supports cancellable deep-DV planning", async () => {
    installFetch(url => url === `${API}/suite/calibration/plan` ? jsonResponse(200, { job_id: "plan" }) : unexpectedFetch(url));
    await expect(postPlan({ paths: [], deep_dv: true })).resolves.toEqual({ job_id: "plan" });
  });
  it("rejects malformed success and propagates safe backend error detail", async () => {
    installFetch(url => url === `${API}/suite/calibration/status?kit=A` ? jsonResponse(200, {}) : unexpectedFetch(url));
    await expect(getStatus("A")).rejects.toMatchObject({ kind: "bad_response" });
    installFetch(url => url === `${API}/suite/calibration/apply` ? jsonResponse(409, { code: "stale_revision", detail: "Preview again" }) : unexpectedFetch(url));
    await expect(postApply({ kit: "A", preview: false, revision: "old", confirm_veto: [] })).rejects.toMatchObject({ status: 409, detail: "Preview again" });
  });
});
