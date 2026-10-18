import { act, StrictMode } from "react";
import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useCalibration } from "./useCalibration";
import { API } from "../config";
import { bodyOf, callsTo, installFetch, jsonResponse, unexpectedFetch } from "../testing/fetchMock";
import { jobOf } from "../testing/fixtures";

const envelope = (sub: string) => ({ schema: `videosuite-calibrate-${sub}/1`, tool_version: "0.5.0" });
function descriptor(test = "T01") {
  return { test, player: "builtin_usb", title: "Banner", class: "decisive", rule: "hdr_map.hdr10", instruction: "Play", question: "Which banner?",
    outcomes: [{ id: "yes", text: "HDR", value: "hdr10" }, { id: "no", text: "SDR", value: "unsupported" }], conditions: ["audio_output"], manual: false,
    file: "clip.mkv", source: { kind: "invented", file: null, verification: { status: "verified" } }, prediction: { outcome: "yes", text: "HDR", evidence: "inferred" } };
}
function answer(test = "T01", outcome = "yes", kit = "A") {
  return { kit_id: kit, test, player: "builtin_usb", outcome, note: "", conditions: { tv_software: "v1", audio_output: "tv_speakers", usb_filesystem: "exfat" },
    at: "2026-10-17T01:00:00Z", older_tv_software: false, match: outcome === "yes" ? "matches" : "differs" };
}
function statusDoc(kit = "A") {
  return { ...envelope("status"), recovery_required: false,
    kit: { id: kit, catalogue_version: 1, built_at: "2026-10-17T00:00:00Z", out: "D:\\Kit", descriptors: [descriptor(), descriptor("T02")] },
    session: { tv_software: "v1", audio_output: "tv_speakers", usb_filesystem: "exfat", confirmed_at: "2026-10-17T00:00:00Z" },
    answers: [answer("T01", "yes", kit)], summary: { done: 1, total: 2, matches: 1, differs: 0 } };
}
const recordDoc = (test = "T01", outcome = "no", kit = "A") => ({ ...envelope("record"), answer: answer(test, outcome, kit), summary: { done: 1, total: 2, matches: 0, differs: 1 } });
const applyDoc = () => ({ ...envelope("apply"), revision: "rev1", applied: false, recovery_required: false, changes: [], conflicts: [], skipped: [] });
const planDoc = () => ({ ...envelope("plan"), catalogue_version: 1, players: [], tests: [] });
const buildDoc = () => ({ ...envelope("build"), kit_id: "A", out: "D:\\Kit", descriptor_path: "D:\\Kit\\kit.json", total_bytes: 10, descriptors: [descriptor()], unavailable: [] });
const reportDoc = () => ({ ...envelope("report"), note: "playback only, not a quality ranking", kit_active: false, recovery_required: false, files: [], best_player_changes: [] });

function deferred() { let resolve!: (r: Response) => void; const promise = new Promise<Response>(r => { resolve = r; }); return { promise, resolve }; }
const recordPath = "/suite/calibration/record";
async function loaded(result: { current: ReturnType<typeof useCalibration> }) { await waitFor(() => expect(result.current.status?.kit).toBeTruthy()); }
beforeEach(() => { vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true); });

it("explains settings_changed without implying apply succeeded", async () => {
  installFetch(url => url === API + "/suite/calibration/status?kit=A" ? jsonResponse(200, statusDoc())
    : url === API + "/suite/calibration/apply" ? jsonResponse(409, { code: "settings_changed", detail: "The settings file changed while applying. Preview and apply again." })
    : unexpectedFetch(url));
  const { result } = renderHook(() => useCalibration("A")); await loaded(result);
  await act(() => result.current.loadPreview());
  expect(result.current.error).toBe("The settings file changed while applying. Preview and apply again.");
  expect(result.current.preview).toBeNull();
});

describe("useCalibration", () => {
  it("clears pending choices when revisiting a kit before the other kit has loaded", async () => {
    const oldSave = deferred(), oldStatus = deferred(), newStatus = deferred(); let aGets = 0;
    installFetch(url => {
      if (url === `${API}/suite/calibration/status?kit=A`) return ++aGets === 1 ? jsonResponse(200, statusDoc()) : newStatus.promise;
      if (url === `${API}/suite/calibration/status?kit=B`) return oldStatus.promise;
      if (url === API + recordPath) return oldSave.promise;
      return unexpectedFetch(url);
    });
    const { result, rerender } = renderHook(({ kit }) => useCalibration(kit), { initialProps: { kit: "A" } }); await loaded(result);
    let saving!: Promise<boolean>; act(() => { saving = result.current.answer("T01", "builtin_usb", "no", ""); });
    rerender({ kit: "B" }); expect(result.current.status).toBeNull();
    rerender({ kit: "A" }); expect(result.current.status).toBeNull(); expect(result.current.pendingAnswers).toEqual([]);
    await act(async () => { oldSave.resolve(jsonResponse(200, recordDoc())); oldStatus.resolve(jsonResponse(200, statusDoc("B"))); newStatus.resolve(jsonResponse(200, statusDoc())); await saving; });
    expect(result.current.answers[0].outcome).toBe("yes"); expect(result.current.pendingAnswers).toEqual([]);
  });
  it("uses the latest status in StrictMode and aborts on disposal", async () => {
    const fetch = installFetch(url => url === `${API}/suite/calibration/status` ? jsonResponse(200, statusDoc()) : unexpectedFetch(url));
    const { result, unmount } = renderHook(() => useCalibration(null), { wrapper: StrictMode }); await loaded(result);
    expect(result.current.answers[0].conditions.tv_software).toBe("v1");
    unmount(); expect(fetch.mock.calls.every(([, init]) => init?.signal?.aborted)).toBe(true);
  });
  it("requires a confirmed session and refuses outcomes outside the stored descriptor", async () => {
    installFetch(url => url === `${API}/suite/calibration/status?kit=A` ? jsonResponse(200, { ...statusDoc(), session: null }) : unexpectedFetch(url));
    const { result } = renderHook(() => useCalibration("A")); await loaded(result);
    await act(() => result.current.answer("T01", "builtin_usb", "no", ""));
    expect(result.current.error).toContain("Confirm the TV software");
    expect(result.current.pendingAnswers).toEqual([]);
  });
  it("does not fetch a session's old kit after a kit switch", async () => {
    const pending = deferred();
    installFetch(url => {
      if (url === `${API}/suite/calibration/status?kit=A`) return jsonResponse(200, statusDoc());
      if (url === `${API}/suite/calibration/status?kit=B`) return jsonResponse(200, statusDoc("B"));
      if (url === `${API}/suite/calibration/session`) return pending.promise;
      return unexpectedFetch(url);
    });
    const { result, rerender } = renderHook(({ kit }) => useCalibration(kit), { initialProps: { kit: "A" } }); await loaded(result);
    let confirming!: Promise<boolean>;
    act(() => { confirming = result.current.confirmSession({ out: "D:\\Kit", tv_software: "v2", audio_output: "avr", usb_filesystem: "ntfs" }); });
    rerender({ kit: "B" }); await waitFor(() => expect(result.current.status?.kit?.id).toBe("B"));
    await act(async () => { pending.resolve(jsonResponse(200, { ...envelope("session"), kit_id: "oldkit", session: statusDoc().session })); await confirming; });
    expect(result.current.status?.kit?.id).toBe("B");
  });
  it("keeps a successful different-test save when another save fails", async () => {
    const pending = deferred();
    installFetch((url, init) => {
      if (url === `${API}/suite/calibration/status?kit=A`) return jsonResponse(200, statusDoc());
      if (url === API + recordPath) return JSON.parse(String(init?.body)).test === "T01" ? pending.promise : jsonResponse(200, recordDoc("T02", "yes"));
      return unexpectedFetch(url);
    });
    const { result } = renderHook(() => useCalibration("A")); await loaded(result);
    let saving!: Promise<boolean>; act(() => { saving = result.current.answer("T01", "builtin_usb", "no", ""); });
    await act(() => result.current.answer("T02", "builtin_usb", "yes", ""));
    await act(async () => { pending.resolve(jsonResponse(500, { detail: "failed" })); await saving; });
    expect(result.current.answers.map(a => a.outcome)).toEqual(["yes", "yes"]);
    expect(result.current.status?.summary).toEqual({ done: 2, total: 2, matches: 2, differs: 0 });
    expect(result.current.error).toContain("failed");
  });
  it("does not let the initial status overwrite a newly confirmed session", async () => {
    const old = deferred(); let gets = 0;
    installFetch(url => {
      if (url === `${API}/suite/calibration/status?kit=A`) return ++gets === 1 ? old.promise : jsonResponse(200, { ...statusDoc(), session: { ...statusDoc().session, tv_software: "v2" } });
      if (url === `${API}/suite/calibration/session`) return jsonResponse(200, { ...envelope("session"), kit_id: "A", session: { ...statusDoc().session, tv_software: "v2" } });
      return unexpectedFetch(url);
    });
    const { result } = renderHook(() => useCalibration("A"));
    await act(() => result.current.confirmSession({ out: "D:\\Kit", tv_software: "v2", audio_output: "tv_speakers", usb_filesystem: "exfat" }));
    expect(result.current.status?.session?.tv_software).toBe("v2");
    await act(async () => { old.resolve(jsonResponse(200, statusDoc())); });
    expect(result.current.status?.session?.tv_software).toBe("v2");
  });
  it("retains only the newest plan/build generation and copies request arrays", async () => {
    const oldPlan = deferred(), oldBuild = deferred(); let plans = 0, builds = 0;
    const fetch = installFetch(url => {
      if (url === `${API}/suite/calibration/status?kit=A`) return jsonResponse(200, statusDoc());
      if (url === `${API}/suite/calibration/plan`) return ++plans === 1 ? oldPlan.promise : jsonResponse(200, { ...planDoc(), catalogue_version: 2 });
      if (url === `${API}/suite/calibration/build`) return ++builds === 1 ? oldBuild.promise : jsonResponse(200, { job_id: "new" });
      if (url === `${API}/job/new`) return jsonResponse(200, jobOf({ status: "done", results: buildDoc() }));
      return unexpectedFetch(url);
    });
    const { result } = renderHook(() => useCalibration("A")); await loaded(result);
    const request = { paths: ["D:\\first.mkv"] }; let first!: Promise<boolean>, build!: Promise<boolean>;
    act(() => { first = result.current.plan(request); build = result.current.build({ ...request, out: "D:\\Kit" }); });
    request.paths[0] = "D:\\second.mkv";
    await act(() => result.current.plan(request)); await act(() => result.current.build({ ...request, out: "D:\\Kit2" }));
    await act(async () => { oldPlan.resolve(jsonResponse(500, { detail: "old" })); oldBuild.resolve(jsonResponse(200, { job_id: "old" })); await Promise.all([first, build]); });
    expect(result.current.planDocument?.catalogue_version).toBe(2); expect(result.current.error).toBeNull();
    expect(bodyOf(fetch, "/suite/calibration/plan", 0)).toEqual({ paths: ["D:\\first.mkv"] });
    expect(bodyOf(fetch, "/suite/calibration/build", 0)).toMatchObject({ paths: ["D:\\first.mkv"], out: "D:\\Kit" });
    expect(result.current.buildId).toBe("new");
  });
  it("applies only the loaded revision, freezes veto ids, deduplicates apply, and exposes undo", async () => {
    const pending = deferred(); let commits = 0;
    const fetch = installFetch((url, init) => {
      if (url === `${API}/suite/calibration/status?kit=A`) return jsonResponse(200, statusDoc());
      if (url === `${API}/suite/calibration/apply`) {
        if (JSON.parse(String(init?.body)).preview) return jsonResponse(200, applyDoc());
        commits++; return pending.promise;
      }
      if (url === `${API}/suite/calibration/undo`) return jsonResponse(200, { ...applyDoc(), applied: true });
      return unexpectedFetch(url);
    });
    const { result } = renderHook(() => useCalibration("A")); await loaded(result);
    await act(() => result.current.apply()); expect(commits).toBe(0);
    await act(() => result.current.loadPreview());
    const ids = ["T01:builtin_usb:hdr_map.hdr10"]; let applying!: Promise<boolean>;
    act(() => { applying = result.current.apply(ids); void result.current.apply(ids); }); ids.push("later");
    expect(commits).toBe(1); expect(bodyOf(fetch, "/suite/calibration/apply", 1)).toEqual({ kit: "A", preview: false, revision: "rev1", confirm_veto: [ids[0]] });
    await act(async () => { pending.resolve(jsonResponse(200, { ...applyDoc(), applied: true })); await applying; });
    expect(result.current.applyResult?.applied).toBe(true); expect(result.current.preview).toBeNull();
    await act(() => result.current.undo()); expect(bodyOf(fetch, "/suite/calibration/undo")).toEqual({ kit: "A" });
  });
  it("ignores an old report start after answers or settings invalidate it", async () => {
    const pending = deferred();
    installFetch(url => url === `${API}/suite/calibration/status?kit=A` ? jsonResponse(200, statusDoc()) : url === `${API}/suite/calibration/report` ? pending.promise : unexpectedFetch(url));
    const { result } = renderHook(() => useCalibration("A")); await loaded(result);
    let reporting!: Promise<boolean>; act(() => { reporting = result.current.report({ paths: ["D:\\a.mkv"] }); });
    act(() => result.current.settingsChanged());
    await act(async () => { pending.resolve(jsonResponse(200, { job_id: "old" })); await reporting; });
    expect(result.current.reportId).toBeNull(); expect(result.current.reportDocument).toBeNull();
  });
  it("deduplicates a double click, shows a pending choice, and rolls back failed POST", async () => {
    const pending = deferred();
    const fetch = installFetch(url => url === `${API}/suite/calibration/status?kit=A` ? jsonResponse(200, statusDoc()) : url === API + recordPath ? pending.promise : unexpectedFetch(url));
    const { result } = renderHook(() => useCalibration("A")); await loaded(result);
    let saving!: Promise<boolean>;
    act(() => { saving = result.current.answer("T01", "builtin_usb", "no", "note"); void result.current.answer("T01", "builtin_usb", "no", "note"); });
    expect(callsTo(fetch, recordPath)).toHaveLength(1);
    expect(result.current.pendingAnswers).toHaveLength(1);
    await act(async () => { pending.resolve(jsonResponse(409, { detail: "store busy" })); await saving; });
    expect(result.current.answers[0].outcome).toBe("yes"); expect(result.current.pendingAnswers).toEqual([]);
    expect(result.current.error).toBe("Busy: store busy");
  });
  it("restores server answers on reload even when browser storage throws", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw Error("disabled"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw Error("disabled"); });
    installFetch(url => url === `${API}/suite/calibration/status?kit=A` ? jsonResponse(200, statusDoc()) : unexpectedFetch(url));
    const first = renderHook(() => useCalibration("A")); await loaded(first.result); first.unmount();
    const second = renderHook(() => useCalibration("A")); await loaded(second.result);
    expect(second.result.current.answers).toEqual([answer()]);
    expect(second.result.current.status?.kit?.descriptors[0].prediction.evidence).toBe("inferred");
  });
  it("rapid Next saves independent immutable test snapshots and merges reverse completions", async () => {
    const one = deferred(), two = deferred();
    const fetch = installFetch((url, init) => {
      if (url === `${API}/suite/calibration/status?kit=A`) return jsonResponse(200, statusDoc());
      if (url === API + recordPath) return JSON.parse(String(init?.body)).test === "T01" ? one.promise : two.promise;
      return unexpectedFetch(url);
    });
    const { result } = renderHook(() => useCalibration("A")); await loaded(result);
    let a!: Promise<boolean>, b!: Promise<boolean>;
    act(() => { a = result.current.answer("T01", "builtin_usb", "no", "first"); b = result.current.answer("T02", "builtin_usb", "yes", "next"); });
    expect(result.current.pendingAnswers).toHaveLength(2);
    expect(bodyOf(fetch, recordPath, 0)).toMatchObject({ test: "T01", note: "first" });
    expect(bodyOf(fetch, recordPath, 1)).toMatchObject({ test: "T02", note: "next" });
    await act(async () => { two.resolve(jsonResponse(200, recordDoc("T02", "yes"))); await b; });
    await act(async () => { one.resolve(jsonResponse(200, recordDoc())); await a; });
    expect(result.current.answers.map(a => [a.test, a.outcome])).toEqual([["T01", "no"], ["T02", "yes"]]);
    expect(result.current.status?.summary).toEqual({ done: 2, total: 2, matches: 1, differs: 1 });
  });
  it("ignores an old-kit save failure after a kit switch", async () => {
    const pending = deferred();
    installFetch(url => url === `${API}/suite/calibration/status?kit=A` ? jsonResponse(200, statusDoc()) : url === `${API}/suite/calibration/status?kit=B` ? jsonResponse(200, statusDoc("B")) : url === API + recordPath ? pending.promise : unexpectedFetch(url));
    const { result, rerender } = renderHook(({ kit }) => useCalibration(kit), { initialProps: { kit: "A" } }); await loaded(result);
    let saving!: Promise<boolean>; act(() => { saving = result.current.answer("T01", "builtin_usb", "no", ""); });
    rerender({ kit: "B" }); await waitFor(() => expect(result.current.status?.kit?.id).toBe("B"));
    await act(async () => { pending.resolve(jsonResponse(500, { detail: "old" })); await saving; });
    expect(result.current.error).toBeNull(); expect(result.current.pendingAnswers).toEqual([]);
    expect(result.current.answers[0].kit_id).toBe("B");
  });
  it("invalidates previews on answers and settings changes, and ignores a late preview", async () => {
    let late = false; const pending = deferred();
    installFetch(url => {
      if (url === `${API}/suite/calibration/status?kit=A`) return jsonResponse(200, statusDoc());
      if (url === `${API}/suite/calibration/apply`) return late ? pending.promise : jsonResponse(200, applyDoc());
      if (url === API + recordPath) return jsonResponse(200, recordDoc());
      return unexpectedFetch(url);
    });
    const { result } = renderHook(() => useCalibration("A")); await loaded(result);
    await act(() => result.current.loadPreview()); expect(result.current.preview?.revision).toBe("rev1");
    await act(() => result.current.answer("T01", "builtin_usb", "no", "")); expect(result.current.preview).toBeNull();
    await act(() => result.current.loadPreview()); act(() => result.current.settingsChanged()); expect(result.current.preview).toBeNull();
    late = true; let load!: Promise<boolean>; act(() => { load = result.current.loadPreview(); });
    act(() => result.current.settingsChanged());
    await act(async () => { pending.resolve(jsonResponse(200, applyDoc())); await load; });
    expect(result.current.preview).toBeNull();
  });
  it("polls build/report/deep plan through useJob and narrows their results", async () => {
    installFetch(url => {
      if (url === `${API}/suite/calibration/status?kit=A`) return jsonResponse(200, statusDoc());
      for (const name of ["plan", "build", "report"]) {
        if (url === `${API}/suite/calibration/${name}`) return jsonResponse(200, { job_id: name });
        if (url === `${API}/job/${name}`) return jsonResponse(200, jobOf({ status: "done", results: name === "plan" ? planDoc() : name === "build" ? buildDoc() : reportDoc() }));
      }
      return unexpectedFetch(url);
    });
    const { result } = renderHook(() => useCalibration("A")); await loaded(result);
    await act(() => result.current.plan({ paths: [], deep_dv: true }));
    await waitFor(() => expect(result.current.planDocument?.catalogue_version).toBe(1));
    await act(() => result.current.build({ paths: [], out: "D:\\Kit" }));
    await waitFor(() => expect(result.current.buildDocument?.kit_id).toBe("A"));
    await act(() => result.current.report({ paths: ["D:\\a.mkv"] }));
    await waitFor(() => expect(result.current.reportDocument?.kit_active).toBe(false));
    expect(result.current.reportDocument?.note).toBe("playback only, not a quality ranking");
  });
});
