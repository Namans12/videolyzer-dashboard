import { act } from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ApplyPanel } from "./ApplyPanel";
import { useCalibration } from "./useCalibration";
import type { ApplyChange, ApplyDocument, ReportDocument } from "./types";
import { API } from "../config";
import { bodyOf, callsTo, installFetch, jsonResponse, unexpectedFetch } from "../testing/fetchMock";
import { jobOf } from "../testing/fixtures";
import { POLL_MS } from "../useJob";

const base = "/suite/calibration";
const envelope = (sub: string) => ({ schema: `videosuite-calibrate-${sub}/1`, tool_version: "0.5.0" });
function statusDoc() {
  return { ...envelope("status"), recovery_required: false, kit: { id: "kit", catalogue_version: 1, built_at: "saved", out: "D:\\Kit", descriptors: [] },
    session: null, answers: [], summary: { done: 0, total: 0, matches: 0, differs: 0 } };
}
function change(test: string, action: ApplyChange["action"], reason = "Recorded answer"): ApplyChange {
  return { id: `${test}:builtin_usb:audio_map.dts`, test, player: "builtin_usb", rule: "audio_map.dts", action, reason,
    previous: "decode", value: action === "veto" ? "silent" : "decode", veto: action === "veto" };
}
function preview(revision = "rev1"): ApplyDocument {
  return { ...envelope("apply"), revision, applied: false, recovery_required: false,
    changes: [change("T01", "veto"), change("T02", "veto"), change("T03", "confirm"), change("T04", "change"), change("T05", "skip", "manual entry present")],
    conflicts: [{ rule: "audio_map.dts", player: "builtin_usb", tests: ["T06", "T07"] }],
    skipped: [{ test: "T05", player: "builtin_usb", reason: "manual entry present" }, { test: "T08", player: "builtin_usb", reason: "duplicate answer" }] };
}
function reportDoc(): ReportDocument {
  const before = { verdict: "PLAYS_WELL", score: 90, evidence: "inferred", effective_audio: "DTS", effective_hdr: "HDR10" };
  const after = { verdict: "WONT_PLAY", score: 30, evidence: "owner", effective_audio: "silent", effective_hdr: "HDR10" };
  const best = { player: "builtin_usb", label: "Built-in USB", verdict: "PLAYS_WELL", score: 90, default_player: "builtin_usb", switch: false, reason: "default" };
  return { ...envelope("report"), note: "playback only, not a quality ranking", kit_active: true, recovery_required: false,
    files: [{ path: "D:\\Fixtures\\Тест & 100%.mkv", player: "sony_bravia_8_ii/builtin_usb", before, after, changed: true }],
    best_player_changes: [{ path: "D:\\Fixtures\\Тест & 100%.mkv", device: "sony_bravia_8_ii", before: best, after: { ...best, player: "kodi", label: "Kodi", switch: true, reason: "Audio plays" } }] };
}
function Harness() {
  const calibration = useCalibration(null);
  return <><ApplyPanel calibration={calibration} /><button onClick={calibration.settingsChanged}>Invalidate settings</button></>;
}
it("can undo despite a conditions refusal, but waits for loading, mutations and answers", async () => {
  const fetch = installFetch(url => url === API + base + "/status" ? jsonResponse(200, statusDoc())
    : url === API + base + "/undo" ? jsonResponse(200, { ...preview(), applied: true }) : unexpectedFetch(url));
  function Card() {
    const calibration = useCalibration(null);
    return <ApplyPanel calibration={calibration} disabledReason="Owner settings have errors." />;
  }
  render(<Card />);
  await waitFor(() => expect(screen.getByRole("button", { name: "Undo this kit" })).toBeEnabled());
  expect(screen.getByRole("button", { name: "Preview changes" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Apply" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Undo this kit" }));
  await screen.findByText("Kit changes removed.");
  expect(bodyOf(fetch, base + "/undo")).toEqual({ kit: "kit" });
});

async function loadPreview() {
  await waitFor(() => expect(screen.getByRole("button", { name: "Preview changes" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Preview changes" }));
  await screen.findByRole("table", { name: "Apply preview" });
}
function confirm(test: string) {
  fireEvent.click(screen.getByRole("checkbox", { name: `I checked the veto for ${test} (Built-in USB player)` }));
  fireEvent.click(screen.getByRole("button", { name: `Confirm veto for ${test} (Built-in USB player)` }));
}
beforeEach(() => { vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true); });
afterEach(() => { vi.useRealTimers(); });

it("summarizes rule arrays with evidence and HDR quality, retaining raw rules in closed details", async () => {
  const doc = preview();
  doc.changes = [
    { ...change("T01", "change"), player: "kodi", previous: ["silent", "official", "<img src=x>", "spec"], value: ["decode", "owner", "Heard sound", "owner_test"] },
    { ...change("T02", "confirm"), player: "just_player", rule: "hdr_map.dv_p8_1", previous: ["dolby_vision", 0, 92, "inferred", "", "profile"], value: ["dolby_vision", 0, 92, "owner", "Banner", "owner_test"] },
    { ...change("T03", "skip"), player: "constructor", previous: null, value: null },
  ];
  installFetch(url => url === API + base + "/status" ? jsonResponse(200, statusDoc())
    : url === API + base + "/apply" ? jsonResponse(200, doc) : unexpectedFetch(url));
  const { container } = render(<Harness />); await loadPreview();
  const table = screen.getByRole("table", { name: "Apply preview" });
  for (const text of ["silent (official)", "decode (owner)", "Dolby Vision picture, quality 92 (inferred)", "Dolby Vision picture, quality 92 (owner)"])
    expect(within(table).getByText(text)).toBeVisible();
  expect(table).toHaveTextContent("T01 / Kodi /"); expect(table).toHaveTextContent("T02 / Just Player /");
  expect(table).toHaveTextContent("T03 / constructor /");
  const details = container.querySelectorAll("details"); expect(details).toHaveLength(3);
  for (const detail of details) expect(detail).not.toHaveAttribute("open");
  expect(details[0].querySelector("pre")?.textContent).toContain(JSON.stringify(doc.changes[0].previous, null, 2));
  expect(container.querySelector("img")).toBeNull();
});

it("shows every action and reason once, explains conflicts/manual entries/duplicates and sends only individually confirmed veto ids with the revision", async () => {
  const fetch = installFetch((url, init) => {
    if (url === API + base + "/status") return jsonResponse(200, statusDoc());
    if (url === API + base + "/apply") return jsonResponse(200, JSON.parse(String(init?.body)).preview ? preview() : { ...preview(), applied: true });
    return unexpectedFetch(url);
  });
  render(<Harness />);
  expect(screen.getByRole("button", { name: "Apply" })).toBeDisabled();
  await loadPreview();
  const table = screen.getByRole("table", { name: "Apply preview" });
  for (const action of ["confirm", "change", "veto", "skip"]) expect(within(table).getAllByText(action).length).toBeGreaterThan(0);
  expect(within(table).getAllByText("manual entry present")).toHaveLength(1);
  expect(screen.getByText(/hand-written.*kept/i)).toBeVisible();
  expect(screen.getByText(/contradict.*skipped/i)).toHaveTextContent("T06, T07");
  expect(screen.getByText(/T08.*duplicate answer/)).toBeVisible();
  expect(screen.getByRole("button", { name: "Confirm veto for T01 (Built-in USB player)" })).toBeDisabled();
  confirm("T01");
  // Checking alone is not confirmation.
  fireEvent.click(screen.getByRole("checkbox", { name: "I checked the veto for T02 (Built-in USB player)" }));
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  expect(await screen.findByText("Changes applied.")).toBeVisible();
  expect(bodyOf(fetch, base + "/apply", 1)).toEqual({ kit: "kit", preview: false, revision: "rev1", confirm_veto: ["T01:builtin_usb:audio_map.dts"] });
  expect(screen.getByRole("button", { name: "Apply" })).toBeDisabled();
});

it("re-fetches a refused stale preview, tells the owner and clears revision-bound confirmations", async () => {
  let previews = 0;
  const fetch = installFetch((url, init) => {
    if (url === API + base + "/status") return jsonResponse(200, statusDoc());
    if (url === API + base + "/apply") return JSON.parse(String(init?.body)).preview
      ? jsonResponse(200, preview(`rev${++previews}`)) : jsonResponse(409, { detail: "stale revision; preview again" });
    return unexpectedFetch(url);
  });
  render(<Harness />); await loadPreview(); confirm("T01");
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  await waitFor(() => expect(previews).toBe(2));
  expect(await screen.findByText(/Apply was refused.*preview.*review/i)).toBeVisible();
  expect(screen.getByRole("checkbox", { name: "I checked the veto for T01 (Built-in USB player)" })).not.toBeChecked();
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  await waitFor(() => expect(callsTo(fetch, base + "/apply").length).toBeGreaterThanOrEqual(4));
  expect(bodyOf(fetch, base + "/apply", 3)).toEqual({ kit: "kit", preview: false, revision: "rev2", confirm_veto: [] });
});

it("invalidates confirmations when settings or a new preview change, and undo double-click sends one request", async () => {
  let resolve!: (r: Response) => void;
  const fetch = installFetch(url => {
    if (url === API + base + "/status") return jsonResponse(200, statusDoc());
    if (url === API + base + "/apply") return jsonResponse(200, preview());
    if (url === API + base + "/undo") return new Promise<Response>(r => { resolve = r; });
    return unexpectedFetch(url);
  });
  render(<Harness />); await loadPreview(); confirm("T01");
  fireEvent.click(screen.getByRole("button", { name: "Invalidate settings" }));
  expect(screen.getByRole("button", { name: "Apply" })).toBeDisabled();
  await loadPreview();
  expect(screen.getByRole("checkbox", { name: "I checked the veto for T01 (Built-in USB player)" })).not.toBeChecked();
  const undo = screen.getByRole("button", { name: "Undo this kit" }); fireEvent.click(undo); fireEvent.click(undo);
  expect(callsTo(fetch, base + "/undo")).toHaveLength(1); expect(bodyOf(fetch, base + "/undo")).toEqual({ kit: "kit" });
  await act(async () => resolve(jsonResponse(200, { ...preview(), applied: true, changes: [change("T01", "left", "Entry was changed later"), change("T02", "undo")] })));
  expect(await screen.findByRole("table", { name: "Apply result" })).toHaveTextContent("Entry was changed later");
  expect(screen.getByText("Kit changes removed.")).toBeVisible();
  expect(screen.queryByText("Changes applied.")).not.toBeInTheDocument();
});

it("shows recovery as plain text and prevents applying a recovery preview", async () => {
  installFetch(url => {
    if (url === API + base + "/status") return jsonResponse(200, statusDoc());
    if (url === API + base + "/apply") return jsonResponse(200, { ...preview(), recovery_required: true });
    return unexpectedFetch(url);
  });
  render(<Harness />); await loadPreview();
  expect(screen.getByText(/earlier apply or undo needs recovery/i)).toBeVisible();
  expect(screen.getByRole("button", { name: "Apply" })).toBeDisabled();
});

it("runs What changed with unchanged paths, renders old/new evidence, audio, HDR and best player as text", async () => {
  const doc = reportDoc(); doc.files[0].after.effective_audio = "<img src=x>";
  const fetch = installFetch(url => {
    if (url === API + base + "/status") return jsonResponse(200, statusDoc());
    if (url === API + base + "/report") return jsonResponse(200, { job_id: "report" });
    if (url === API + "/job/report") return jsonResponse(200, jobOf({ status: "done", results: doc }));
    return unexpectedFetch(url);
  });
  const { container } = render(<Harness />);
  await waitFor(() => expect(screen.getByRole("button", { name: "What changed" })).toBeEnabled());
  fireEvent.change(screen.getByLabelText(/Files for playback report/), { target: { value: doc.files[0].path } });
  fireEvent.click(screen.getByRole("button", { name: "What changed" }));
  const playback = await screen.findByRole("table", { name: /Playback before and after:/ });
  expect(playback).toHaveAccessibleName(`Playback before and after: ${doc.files[0].path} (Built-in USB player)`);
  expect(screen.getByRole("heading", { level: 5 })).toHaveTextContent(`${doc.files[0].path} — Built-in USB player`);
  expect(within(playback).getByText("evidence: owner")).toBeVisible();
  expect(within(playback).getByText("evidence: inferred")).toBeVisible();
  expect(screen.getByText("playback only, not a ranking")).toBeVisible();
  expect(screen.getByText(/Audio: <img src=x>/)).toBeVisible(); expect(container.querySelector("img")).toBeNull();
  expect(screen.getAllByText(/HDR: HDR10/)).toHaveLength(2);
  expect(screen.getByText(/Kodi/)).toBeVisible();
  expect(bodyOf(fetch, base + "/report")).toEqual({ kit: "kit", paths: [doc.files[0].path] });
});

it.each(["derived", "missing", "constructor", "__proto__"])("labels both best-player recommendations with %s evidence lookup", async kind => {
  const doc = reportDoc();
  const recommendation = doc.best_player_changes[0];
  // Decoys ensure evidence is matched by both exact path and recommended player.
  doc.files.push({ ...doc.files[0], path: "another path", player: "sony_bravia_8_ii/kodi",
    before: { ...doc.files[0].before, evidence: "official" }, after: { ...doc.files[0].after, evidence: "official" } });
  if (kind === "constructor" || kind === "__proto__") {
    recommendation.path = kind;
    recommendation.before.player = kind;
    recommendation.after.player = kind === "constructor" ? "__proto__" : "constructor";
  }
  doc.files[0].path = recommendation.path;
  doc.files[0].player = `${recommendation.device}/${recommendation.before.player}`;
  doc.files.unshift({ ...doc.files[0], player: `other_device/${recommendation.before.player}`,
    before: { ...doc.files[0].before, evidence: "official" }, after: { ...doc.files[0].after, evidence: "official" } });
  doc.files.push({ ...doc.files[1], player: `${recommendation.device}/${recommendation.after.player}`,
    before: { ...doc.files[1].before, evidence: "forum" }, after: { ...doc.files[1].after, evidence: "reviewed" } });
  if (kind === "missing") doc.files = [];
  installFetch(url => {
    if (url === API + base + "/status") return jsonResponse(200, statusDoc());
    if (url === API + base + "/report") return jsonResponse(200, { job_id: "report" });
    if (url === API + "/job/report") return jsonResponse(200, jobOf({ status: "done", results: doc }));
    return unexpectedFetch(url);
  });
  render(<Harness />);
  await waitFor(() => expect(screen.getByRole("button", { name: "What changed" })).toBeEnabled());
  fireEvent.change(screen.getByLabelText(/Files for playback report/), { target: { value: "D:\\Fixtures\\clip.mkv" } });
  fireEvent.click(screen.getByRole("button", { name: "What changed" }));
  const before = await screen.findByText(/^Best player before:/);
  const after = screen.getByText(/^Best player after:/);
  for (const [element, side, evidence] of [[before, recommendation.before, "inferred"], [after, recommendation.after, "reviewed"]] as const) {
    expect(within(element).getByText("Plays well")).toBeVisible();
    expect(element).toHaveTextContent(side.label);
    expect(within(element).getByText(`evidence: ${kind === "missing" ? "unknown" : evidence}`)).toBeVisible();
    if (kind === "missing") expect(within(element).getByRole("note")).toHaveTextContent(/Unverified rules:.*unknown evidence/);
  }
});

it.each(["done", "error", "cancelled", "gone"] as const)("stops report polling at %s", async terminal => {
  vi.useFakeTimers();
  const fetch = installFetch(url => {
    if (url === API + base + "/status") return jsonResponse(200, statusDoc());
    if (url === API + base + "/report") return jsonResponse(200, { job_id: "terminal" });
    if (url === API + "/job/terminal") return terminal === "gone" ? jsonResponse(404, {}) : jsonResponse(200, jobOf({ status: terminal, results: reportDoc() }));
    return unexpectedFetch(url);
  });
  render(<Harness />); await act(async () => {});
  fireEvent.change(screen.getByLabelText(/Files for playback report/), { target: { value: "D:\\Fixtures\\clip.mkv" } });
  fireEvent.click(screen.getByRole("button", { name: "What changed" }));
  await act(async () => {}); await act(async () => vi.advanceTimersByTimeAsync(1));
  expect(callsTo(fetch, "/job/terminal")).toHaveLength(1);
  await act(async () => vi.advanceTimersByTimeAsync(POLL_MS * 10));
  expect(callsTo(fetch, "/job/terminal")).toHaveLength(1);
});

it.each(["inactive", "recovery"])("explains %s reports without attributing file changes", async kind => {
  const doc = reportDoc(); doc.kit_active = kind !== "inactive"; doc.recovery_required = kind === "recovery";
  installFetch(url => {
    if (url === API + base + "/status") return jsonResponse(200, statusDoc());
    if (url === API + base + "/report") return jsonResponse(200, { job_id: "report" });
    if (url === API + "/job/report") return jsonResponse(200, jobOf({ status: "done", results: doc }));
    return unexpectedFetch(url);
  });
  render(<Harness />); await waitFor(() => expect(screen.getByRole("button", { name: "What changed" })).toBeEnabled());
  fireEvent.change(screen.getByLabelText(/Files for playback report/), { target: { value: "D:\\Fixtures\\clip.mkv" } });
  fireEvent.click(screen.getByRole("button", { name: "What changed" }));
  expect(await screen.findByText(kind === "inactive" ? /this kit is no longer active/i : /earlier apply or undo needs recovery/i)).toBeVisible();
  expect(screen.queryByText("evidence: owner")).not.toBeInTheDocument();
});

it("validates report paths without starting a job", async () => {
  const fetch = installFetch(url => url === API + base + "/status" ? jsonResponse(200, statusDoc()) : unexpectedFetch(url));
  render(<Harness />); await waitFor(() => expect(screen.getByRole("button", { name: "What changed" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "What changed" }));
  expect(screen.getByText(/Enter at least one video file path/)).toBeVisible(); expect(callsTo(fetch, base + "/report")).toHaveLength(0);
});

it("requires individual veto confirmation even when confirming an existing silent rule", async () => {
  const doc = preview(); doc.changes = [{ ...change("T01", "confirm"), previous: "silent", value: "silent", veto: true }];
  const fetch = installFetch((url, init) => {
    if (url === API + base + "/status") return jsonResponse(200, statusDoc());
    if (url === API + base + "/apply") return jsonResponse(200, JSON.parse(String(init?.body)).preview ? doc : { ...doc, applied: true });
    return unexpectedFetch(url);
  });
  render(<Harness />); await loadPreview(); confirm("T01");
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  await screen.findByText("Changes applied.");
  expect(bodyOf(fetch, base + "/apply", 1)).toEqual({ kit: "kit", preview: false, revision: "rev1", confirm_veto: [doc.changes[0].id] });
});

it("keeps Apply disabled while the fresh preview is outstanding and blocks duplicate preview requests", async () => {
  let resolve!: (r: Response) => void;
  const fetch = installFetch(url => {
    if (url === API + base + "/status") return jsonResponse(200, statusDoc());
    if (url === API + base + "/apply") return new Promise<Response>(r => { resolve = r; });
    return unexpectedFetch(url);
  });
  render(<Harness />); await waitFor(() => expect(screen.getByRole("button", { name: "Preview changes" })).toBeEnabled());
  const button = screen.getByRole("button", { name: "Preview changes" }); fireEvent.click(button); fireEvent.click(button);
  expect(callsTo(fetch, base + "/apply")).toHaveLength(1);
  expect(screen.getByRole("button", { name: "Apply" })).toBeDisabled();
  await act(async () => resolve(jsonResponse(200, preview())));
  expect(screen.getByRole("button", { name: "Apply" })).toBeEnabled();
});

it("leaves Apply disabled and shows the error if automatic preview refresh fails", async () => {
  let previews = 0;
  installFetch((url, init) => {
    if (url === API + base + "/status") return jsonResponse(200, statusDoc());
    if (url === API + base + "/apply") {
      if (!JSON.parse(String(init?.body)).preview) return jsonResponse(400, { detail: "stale revision" });
      return ++previews === 1 ? jsonResponse(200, preview()) : jsonResponse(503, { detail: "unavailable" });
    }
    return unexpectedFetch(url);
  });
  render(<Harness />); await loadPreview(); fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/unavailable/i);
  expect(screen.getByRole("button", { name: "Apply" })).toBeDisabled();
  expect(screen.queryByRole("table", { name: "Apply preview" })).not.toBeInTheDocument();
});
