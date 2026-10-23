import { useState } from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { CalibrationScreen } from "./CalibrationScreen";
import { CALIBRATION_UPDATE_TEXT } from "./messages";
import type { Descriptor, StatusDocument } from "./types";
import { API } from "../config";
import { bodyOf, callsTo, installFetch, jsonResponse, unexpectedFetch } from "../testing/fetchMock";
import type { FetchHandler } from "../testing/fetchMock";
import { devicesDoc, jobOf, suiteSettings, versionInfo } from "../testing/fixtures";

const KIT_KEY = "calibration_kit";
const conditions = { tv_software: "v1", audio_output: "avr", usb_filesystem: "exfat" } as const;
const descriptor = (test: string): Descriptor => ({ test, player: "builtin_usb", title: `Title ${test}`, class: "decisive", rule: "hdr_map.hdr10",
  instruction: `Instruction ${test}`, question: `Question ${test}?`, conditions: [], manual: false, file: `${test}.mkv`,
  outcomes: [{ id: "hdr", text: `HDR banner ${test}`, value: "hdr10" }, { id: "unsure", text: "unsure", value: null }, { id: "skipped", text: "skip", value: null }],
  source: { kind: "invented", file: null, verification: null }, prediction: { outcome: "hdr", text: "HDR predicted", evidence: "inferred" } });
const statusDoc = (id = "K1"): StatusDocument => ({ schema: "videosuite-calibrate-status/1", tool_version: "0.5.0", recovery_required: false,
  kit: { id, out: "D:\\Kit", built_at: "saved time", catalogue_version: 1, descriptors: [descriptor("T01"), descriptor("T02")] },
  session: { ...conditions, confirmed_at: "saved time" }, answers: [], summary: { done: 0, total: 2, matches: 0, differs: 0 } });
const emptyStatus = (): StatusDocument => ({ ...statusDoc(), kit: null, session: null, summary: { done: 0, total: 0, matches: 0, differs: 0 } });
function settings() { const s = suiteSettings(); s.devices.sony_bravia_8_ii = { label: "BRAVIA", audio_output: "avr", usb_filesystem: "exfat", overrides: [] }; return s; }
const capable = () => versionInfo({ tool_version: "0.5.0", capabilities: [...versionInfo().capabilities, "calibration"] });
function setup(extra: FetchHandler = unexpectedFetch, version = capable(), view = settings()) {
  return installFetch((url, init) => {
    if (url === API + "/suite/version" || url === API + "/suite/version?refresh=true") return jsonResponse(200, version);
    if (url === API + "/suite/devices") return jsonResponse(200, devicesDoc());
    if (url === API + "/suite/settings" || url === API + "/suite/settings?refresh=true") return jsonResponse(200, view);
    return extra(url, init);
  });
}
beforeEach(() => { vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true); localStorage.clear(); });
afterEach(() => { vi.restoreAllMocks(); localStorage.clear(); });

it("shows only the update message when the installed suite lacks the calibration capability", async () => {
  const fetch = setup(unexpectedFetch, versionInfo());
  render(<CalibrationScreen />);
  expect(await screen.findByText(CALIBRATION_UPDATE_TEXT)).toBeVisible();
  expect(CALIBRATION_UPDATE_TEXT).toContain("update VideoSuite to 0.5.0");
  expect(screen.queryByRole("region", { name: "Prepare calibration kit" })).toBeNull();
  expect(fetch.mock.calls.some(c => String(c[0]).includes("/suite/calibration/"))).toBe(false);
});

it("lays out kit, conditions, checklist with guided card in two columns, then apply, and wires readiness to answers", async () => {
  const fetch = setup(url => url === API + "/suite/calibration/status" ? jsonResponse(200, statusDoc()) : unexpectedFetch(url));
  const { container } = render(<CalibrationScreen />);
  const answer = await screen.findByRole("button", { name: "HDR banner T01" });
  await waitFor(() => expect(answer).toBeEnabled());
  const order = ["Prepare calibration kit", "Calibration conditions", "Calibration checklist", "Guided calibration", "Apply calibration"]
    .map(name => screen.getByRole("region", { name }));
  for (let i = 1; i < order.length; i++) expect(order[i - 1].compareDocumentPosition(order[i]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  const columns = container.querySelector(".calibration-columns")!;
  expect(columns).toContainElement(order[2]); expect(columns).toContainElement(order[3]);
  expect(columns).not.toContainElement(order[1]); expect(columns).not.toContainElement(order[4]);
  const { readFileSync } = await vi.importActual<{ readFileSync: (path: string, encoding: "utf8") => string }>("node:fs");
  const css = readFileSync("src/suite/calibration/calibration.css", "utf8");
  expect(css).toMatch(/@media \(min-width: 900px\)[^}]*\.calibration-columns[^}]*grid-template-columns: minmax\(0, 3fr\) minmax\(0, 2fr\)/);
  expect(css).toMatch(/^\.calibration-columns \{[^}]*grid-template-columns: minmax\(0, 1fr\)/m);
  // Only theme tokens: no literal colours, so the checked light/dark contrast pairs stay the only ones.
  expect(css).not.toMatch(/#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(/);
  // The first unanswered test is selected; the checklist moves the guided card.
  expect(screen.getByRole("article", { name: "T01 Built-in USB player" })).toBeVisible();
  fireEvent.click(within(order[2]).getByRole("button", { name: "T02" }));
  expect(screen.getByRole("article", { name: "T02 Built-in USB player" })).toBeVisible();
  expect(screen.getByRole("button", { name: "HDR banner T02" })).toBeEnabled();
  // Editing the conditions blocks answers until they are confirmed again.
  fireEvent.change(screen.getByRole("textbox", { name: "TV software" }), { target: { value: "v2" } });
  expect(screen.getByRole("button", { name: "HDR banner T02" })).toBeDisabled();
  expect(callsTo(fetch, "/suite/calibration/status")).toHaveLength(1);
});

it("blocks answers while the saved session differs from the current owner settings", async () => {
  const view = settings(); view.devices.sony_bravia_8_ii.audio_output = "soundbar";
  setup(url => url === API + "/suite/calibration/status" ? jsonResponse(200, statusDoc()) : unexpectedFetch(url), capable(), view);
  render(<CalibrationScreen />);
  const answer = await screen.findByRole("button", { name: "HDR banner T01" });
  await screen.findByText(/Changing conditions starts a new session/);
  expect(answer).toBeDisabled();
});

it.each(["errors", "unknown", "no session"])("keeps undo available with %s", async kind => {
  const view = settings(); const doc = statusDoc();
  if (kind === "errors") view.errors = ["invalid owner entry"];
  if (kind === "unknown") view.supported = false;
  if (kind === "no session") doc.session = null;
  const fetch = setup(url => url === API + "/suite/calibration/status" ? jsonResponse(200, doc)
    : url === API + "/suite/calibration/undo" ? jsonResponse(200, { ...previewDoc(), applied: true }) : unexpectedFetch(url), capable(), view);
  render(<CalibrationScreen />);
  await waitFor(() => expect(button("Undo this kit")).toBeEnabled());
  if (kind === "errors") {
    expect(screen.getByText(/Owner settings have errors/)).toBeVisible();
    expect(screen.queryByText(/Owner settings are unknown/)).toBeNull();
    expect(button("Preview changes")).toBeDisabled();
  }
  fireEvent.click(button("Undo this kit"));
  await screen.findByText("Kit changes removed.");
  expect(bodyOf(fetch, "/suite/calibration/undo")).toEqual({ kit: "K1" });
});

it("remembers the kit id and reloads that kit; tolerates throwing storage", async () => {
  const fetch = setup(url => url === API + "/suite/calibration/status" ? jsonResponse(200, statusDoc("K1"))
    : url === API + "/suite/calibration/status?kit=K1" ? jsonResponse(200, statusDoc("K1")) : unexpectedFetch(url));
  const first = render(<CalibrationScreen />);
  await screen.findByRole("button", { name: "HDR banner T01" });
  await waitFor(() => expect(localStorage.getItem(KIT_KEY)).toBe("K1"));
  first.unmount();
  render(<CalibrationScreen />);
  await screen.findByRole("button", { name: "HDR banner T01" });
  expect(callsTo(fetch, "/suite/calibration/status?kit=K1")).toHaveLength(1);
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("denied"); });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new DOMException("full", "QuotaExceededError"); });
  const third = render(<CalibrationScreen />);
  await waitFor(() => expect(callsTo(fetch, "/suite/calibration/status")).toHaveLength(2));
  expect(await within(third.container).findByRole("button", { name: "HDR banner T01" })).toBeVisible();
});

it("ignores an invalid stored kit id and offers the latest kit when the stored one is gone", async () => {
  localStorage.setItem(KIT_KEY, "../bad id");
  const fetch = setup(url => {
    if (url === API + "/suite/calibration/status") return jsonResponse(200, statusDoc("K2"));
    if (url === API + "/suite/calibration/status?kit=OLD") return jsonResponse(404, { detail: "unknown kit" });
    return unexpectedFetch(url);
  });
  const first = render(<CalibrationScreen />);
  await screen.findByRole("button", { name: "HDR banner T01" });
  expect(callsTo(fetch, "/suite/calibration/status")).toHaveLength(1);
  first.unmount(); localStorage.setItem(KIT_KEY, "OLD");
  render(<CalibrationScreen />);
  fireEvent.click(await screen.findByRole("button", { name: "Show the latest kit" }));
  await screen.findByRole("button", { name: "HDR banner T01" });
  expect(callsTo(fetch, "/suite/calibration/status")).toHaveLength(2);
  await waitFor(() => expect(localStorage.getItem(KIT_KEY)).toBe("K2"));
});

it("shows a newly built kit and starts its first session in the kit's output folder", async () => {
  const built = { schema: "videosuite-calibrate-build/1", tool_version: "0.5.0", kit_id: "NEW", out: "D:\\New Kit & Тест", descriptor_path: "D:\\New Kit & Тест\\kit.json",
    total_bytes: 10, descriptors: [descriptor("T05")], unavailable: [] };
  let newStatus: StatusDocument | null = null;
  const fetch = setup((url, init) => {
    if (url === API + "/suite/calibration/status") return jsonResponse(200, statusDoc("K1"));
    if (url === API + "/suite/calibration/plan") return jsonResponse(200, { schema: "videosuite-calibrate-plan/1", tool_version: "0.5.0", catalogue_version: 1, players: [], tests: [] });
    if (url === API + "/suite/calibration/build") return jsonResponse(200, { job_id: "b1" });
    if (url === API + "/job/b1") return jsonResponse(200, jobOf({ status: "done", results: built }));
    if (url === API + "/suite/calibration/session") {
      const body = JSON.parse(String(init?.body));
      newStatus = { ...statusDoc("NEW"), kit: { id: "NEW", out: built.out, built_at: "saved time", catalogue_version: 1, descriptors: built.descriptors },
        session: { tv_software: body.tv_software, audio_output: body.audio_output, usb_filesystem: body.usb_filesystem, confirmed_at: "saved" } };
      return jsonResponse(200, { schema: "videosuite-calibrate-session/1", tool_version: "0.5.0", kit_id: "NEW", session: newStatus.session });
    }
    if (url === API + "/suite/calibration/status?kit=NEW" && newStatus) return jsonResponse(200, newStatus);
    return unexpectedFetch(url);
  });
  render(<CalibrationScreen />);
  await screen.findByRole("button", { name: "HDR banner T01" });
  fireEvent.change(screen.getByLabelText("Kit output folder"), { target: { value: "D:\\Other Kit" } });
  fireEvent.click(screen.getByRole("button", { name: "Plan kit" }));
  await screen.findByText(/Estimated total:/);
  fireEvent.click(screen.getByRole("button", { name: "Prepare kit" }));
  expect(await screen.findByRole("button", { name: "HDR banner T05" })).toBeDisabled();
  expect(screen.getByText(/New kit NEW is ready/)).toBeVisible();
  expect(screen.queryByRole("button", { name: "HDR banner T01" })).toBeNull();
  expect(screen.getByRole("button", { name: "Preview changes" })).toBeDisabled();
  fireEvent.change(screen.getByRole("textbox", { name: "TV software" }), { target: { value: "v9" } });
  fireEvent.click(screen.getByRole("checkbox", { name: /I confirm/ }));
  fireEvent.click(screen.getByRole("button", { name: "Confirm conditions" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "HDR banner T05" })).toBeEnabled());
  expect(bodyOf(fetch, "/suite/calibration/session")).toEqual({ out: built.out, tv_software: "v9", audio_output: "avr", usb_filesystem: "exfat" });
  expect(screen.queryByText(/New kit NEW is ready/)).toBeNull();
  await waitFor(() => expect(localStorage.getItem(KIT_KEY)).toBe("NEW"));
});

it("explains an empty store and keeps apply blocked", async () => {
  setup(url => url === API + "/suite/calibration/status" ? jsonResponse(200, emptyStatus()) : unexpectedFetch(url));
  render(<CalibrationScreen />);
  expect(await screen.findByText(/No calibration kit yet/)).toBeVisible();
  expect(screen.getByText("No tests in this kit.")).toBeVisible();
  expect(screen.getByRole("button", { name: "Preview changes" })).toBeDisabled();
  expect(localStorage.getItem(KIT_KEY)).toBeNull();
});

it("re-checks owner settings on request", async () => {
  const fetch = setup(url => url === API + "/suite/calibration/status" ? jsonResponse(200, statusDoc()) : unexpectedFetch(url));
  render(<CalibrationScreen />);
  await screen.findByRole("button", { name: "HDR banner T01" });
  fireEvent.click(screen.getByRole("button", { name: "Check settings again" }));
  await waitFor(() => expect(callsTo(fetch, "/suite/settings?refresh=true")).toHaveLength(1));
  expect(callsTo(fetch, "/suite/settings")).toHaveLength(1);
  expect(callsTo(fetch, "/suite/version?refresh=true")).toHaveLength(1);
});

// ── Fix round 1 (B5 I3): both recheck paths invalidate the preview and block actions while settings load ──
const previewDoc = () => ({ schema: "videosuite-calibrate-apply/1", tool_version: "0.5.0", revision: "r1", applied: false, recovery_required: false,
  changes: [{ id: "T01:builtin_usb:hdr_map.hdr10", test: "T01", player: "builtin_usb", rule: "hdr_map.hdr10", action: "confirm", reason: "matches the rule", previous: null, value: null, veto: false }],
  conflicts: [], skipped: [] });
function recheckSetup() {
  const state = { audio: "avr" as "avr" | "soundbar", fail: false, hold: null as null | (() => void), deferNext: false };
  const fetch = installFetch(url => {
    if (url === API + "/suite/version" || url === API + "/suite/version?refresh=true") return jsonResponse(200, capable());
    if (url === API + "/suite/settings" || url === API + "/suite/settings?refresh=true") {
      const view = settings(); view.devices.sony_bravia_8_ii.audio_output = state.audio;
      const response = () => state.fail ? jsonResponse(500, { detail: "settings unreadable" }) : jsonResponse(200, view);
      if (!state.deferNext) return response();
      state.deferNext = false;
      return new Promise<Response>(resolve => { state.hold = () => resolve(response()); });
    }
    if (url === API + "/suite/calibration/status") return jsonResponse(200, statusDoc());
    if (url === API + "/suite/calibration/apply") return jsonResponse(200, previewDoc());
    return unexpectedFetch(url);
  });
  return { fetch, state };
}
const button = (name: string) => screen.getByRole("button", { name });
async function readyWithPreview() {
  render(<CalibrationScreen />);
  await waitFor(() => expect(button("HDR banner T01")).toBeEnabled());
  fireEvent.click(button("Preview changes"));
  expect(await screen.findByText("Preview revision: r1")).toBeVisible();
  await waitFor(() => expect(button("Apply")).toBeEnabled());
}
function expectAllBlocked(undoBlocked = true) {
  for (const name of ["HDR banner T01", "Not sure", "Skip", "Preview changes", "Apply", "What changed"]) expect(button(name)).toBeDisabled();
  if (undoBlocked) expect(button("Undo this kit")).toBeDisabled();
  else expect(button("Undo this kit")).toBeEnabled();
}

it.each([
  ["Check again", "soundbar"], ["Check settings again", "soundbar"], ["Check again", "avr"], ["Check settings again", "avr"],
] as const)("%s with audio output %s: blocks while loading, clears the preview, then follows the fetched settings", async (control, audio) => {
  const { fetch, state } = recheckSetup();
  await readyWithPreview();
  state.audio = audio; state.deferNext = true;
  fireEvent.click(button(control));
  await waitFor(() => expect(state.hold).not.toBeNull());
  expect(callsTo(fetch, "/suite/settings")).toHaveLength(1);
  expect(callsTo(fetch, "/suite/settings?refresh=true")).toHaveLength(1);
  expectAllBlocked();
  fireEvent.click(button("HDR banner T01")); fireEvent.click(button("Apply"));
  expect(screen.getByText(/Checking owner settings/)).toBeVisible();
  state.hold!();
  await waitFor(() => expect(button("Check settings again")).toBeEnabled());
  expect(screen.queryByText("Preview revision: r1")).toBeNull();
  expect(button("Apply")).toBeDisabled();
  if (audio === "soundbar") {
    expect(screen.getByText(/Changing conditions starts a new session/)).toBeVisible();
    expect(screen.getByText(/differ from the confirmed session/)).toBeVisible();
    expectAllBlocked(false);
  } else {
    await waitFor(() => expect(button("HDR banner T01")).toBeEnabled());
    expect(button("Preview changes")).toBeEnabled();
  }
  expect(callsTo(fetch, "/suite/calibration/record")).toHaveLength(0);
  expect(callsTo(fetch, "/suite/calibration/apply")).toHaveLength(1);
});

it("treats a failed settings check as unknown and keeps actions blocked", async () => {
  const { fetch, state } = recheckSetup();
  await readyWithPreview();
  state.fail = true;
  fireEvent.click(button("Check again"));
  expect(await screen.findByText(/Owner settings could not be loaded/)).toBeVisible();
  await waitFor(() => expect(button("Check settings again")).toBeEnabled());
  expect(screen.queryByText("Preview revision: r1")).toBeNull();
  expectAllBlocked(false);
  expect(screen.getByText(/Owner settings are unknown/)).toBeVisible();
  expect(callsTo(fetch, "/suite/calibration/apply")).toHaveLength(1);
});

// ── Fix round 1 (B5 M1): a prepared kit survives a reload before its first session ──
const PENDING_KEY = "calibration_pending_kit";
it("remembers a built kit, restores it after a reload, and forgets it once its session exists", async () => {
  // Output folders follow the folder policy (no & or %), so the remembered value passes the same check.
  const built = { schema: "videosuite-calibrate-build/1", tool_version: "0.5.0", kit_id: "NEW", out: "D:\\New Kit Тест", descriptor_path: "D:\\New Kit Тест\\kit.json",
    total_bytes: 10, descriptors: [descriptor("T05")], unavailable: [] };
  let newStatus: StatusDocument | null = null;
  const fetch = setup((url, init) => {
    if (url === API + "/suite/calibration/status") return jsonResponse(200, statusDoc("K1"));
    if (url === API + "/suite/calibration/status?kit=K1") return jsonResponse(200, statusDoc("K1"));
    // A prepared kit is not in the store before its first session: refuse that kit, never undo K1.
    if (url === API + "/suite/calibration/undo") return jsonResponse(400, { code: "refused", detail: "VideoSuite refused the request." });
    if (url === API + "/suite/calibration/plan") return jsonResponse(200, { schema: "videosuite-calibrate-plan/1", tool_version: "0.5.0", catalogue_version: 1, players: [], tests: [] });
    if (url === API + "/suite/calibration/build") return jsonResponse(200, { job_id: "b1" });
    if (url === API + "/job/b1") return jsonResponse(200, jobOf({ status: "done", results: built }));
    if (url === API + "/suite/calibration/session") {
      const body = JSON.parse(String(init?.body));
      newStatus = { ...statusDoc("NEW"), kit: { id: "NEW", out: built.out, built_at: "saved time", catalogue_version: 1, descriptors: built.descriptors },
        session: { tv_software: body.tv_software, audio_output: body.audio_output, usb_filesystem: body.usb_filesystem, confirmed_at: "saved" } };
      return jsonResponse(200, { schema: "videosuite-calibrate-session/1", tool_version: "0.5.0", kit_id: "NEW", session: newStatus.session });
    }
    if (url === API + "/suite/calibration/status?kit=NEW" && newStatus) return jsonResponse(200, newStatus);
    return unexpectedFetch(url);
  });
  const first = render(<CalibrationScreen />);
  await screen.findByRole("button", { name: "HDR banner T01" });
  fireEvent.change(screen.getByLabelText("Kit output folder"), { target: { value: "D:\\Other Kit" } });
  fireEvent.click(button("Plan kit"));
  await screen.findByText(/Estimated total:/);
  fireEvent.click(button("Prepare kit"));
  await screen.findByText(/New kit NEW is ready/);
  await waitFor(() => expect(JSON.parse(localStorage.getItem(PENDING_KEY) ?? "null")).toEqual({ id: "NEW", out: built.out }));
  first.unmount();
  render(<CalibrationScreen />);
  expect(await screen.findByText(/New kit NEW is ready/)).toBeVisible();
  expect(screen.queryByRole("button", { name: "HDR banner T01" })).toBeNull();
  for (const name of ["Preview changes", "What changed"]) expect(button(name)).toBeDisabled();
  expect(button("Undo this kit")).toBeEnabled();
  fireEvent.click(button("Undo this kit"));
  await waitFor(() => expect(screen.getAllByRole("alert").some(a => a.textContent?.includes("VideoSuite refused the request."))).toBe(true));
  expect(screen.queryByText("Kit changes removed.")).toBeNull();
  expect(bodyOf(fetch, "/suite/calibration/undo")).toEqual({ kit: "NEW" });
  fireEvent.change(screen.getByRole("textbox", { name: "TV software" }), { target: { value: "v9" } });
  fireEvent.click(screen.getByRole("checkbox", { name: /I confirm/ }));
  fireEvent.click(button("Confirm conditions"));
  await waitFor(() => expect(button("HDR banner T05")).toBeEnabled());
  expect(bodyOf(fetch, "/suite/calibration/session")).toEqual({ out: built.out, tv_software: "v9", audio_output: "avr", usb_filesystem: "exfat" });
  expect(screen.queryByText(/New kit NEW is ready/)).toBeNull();
  await waitFor(() => expect(localStorage.getItem(PENDING_KEY)).toBeNull());
  expect(localStorage.getItem(KIT_KEY)).toBe("NEW");
});

it("forgets a remembered pending kit that the store already reports", async () => {
  localStorage.setItem(PENDING_KEY, JSON.stringify({ id: "K1", out: "D:\\Kit" }));
  setup(url => url === API + "/suite/calibration/status" ? jsonResponse(200, statusDoc("K1")) : unexpectedFetch(url));
  render(<CalibrationScreen />);
  await waitFor(() => expect(button("HDR banner T01")).toBeEnabled());
  expect(screen.queryByText(/New kit/)).toBeNull();
  await waitFor(() => expect(localStorage.getItem(PENDING_KEY)).toBeNull());
});

it.each([
  ["broken JSON", "{\"id\":"], ["oversized", JSON.stringify({ id: "NEW", out: "D:\\" + "x".repeat(5000) })], ["bad id", JSON.stringify({ id: "../x", out: "D:\\Kit" })],
  ["bad folder", JSON.stringify({ id: "NEW", out: "relative\\kit" })], ["forbidden folder character", JSON.stringify({ id: "NEW", out: "D:\\Kit & Co" })], ["not an object", JSON.stringify(["NEW", "D:\\Kit"])], ["huge value", "x".repeat(100000)],
])("ignores a corrupt remembered pending kit (%s)", async (_label, value) => {
  localStorage.setItem(PENDING_KEY, value);
  setup(url => url === API + "/suite/calibration/status" ? jsonResponse(200, statusDoc("K1")) : unexpectedFetch(url));
  render(<CalibrationScreen />);
  await waitFor(() => expect(button("HDR banner T01")).toBeEnabled());
  expect(screen.queryByText(/New kit/)).toBeNull();
});

// ── Plan 4B U9: Calibration sits beside Compare; its state survives navigation (mounted while hidden) ──
const vetoPreview = () => ({ ...previewDoc(), changes: [{ ...previewDoc().changes[0], action: "veto", veto: true }] });
function Navigable() {
  const [hidden, setHidden] = useState(false);
  return <><button type="button" onClick={() => setHidden(h => !h)}>Switch screens</button>
    <div hidden={hidden}><CalibrationScreen /></div></>;
}
it("preserves Calibration state preview invalidation veto confirmation report and undo behind navigation", async () => {
  const fetch = installFetch(url => {
    if (url === API + "/suite/version" || url === API + "/suite/version?refresh=true") return jsonResponse(200, capable());
    if (url === API + "/suite/settings" || url === API + "/suite/settings?refresh=true") return jsonResponse(200, settings());
    if (url === API + "/suite/calibration/status") return jsonResponse(200, statusDoc());
    if (url === API + "/suite/calibration/apply") return jsonResponse(200, vetoPreview());
    return unexpectedFetch(url);
  });
  render(<Navigable />);
  await waitFor(() => expect(button("HDR banner T01")).toBeEnabled());
  fireEvent.click(button("Preview changes"));
  expect(await screen.findByText("Preview revision: r1")).toBeVisible();
  fireEvent.click(screen.getByRole("checkbox", { name: /I checked the veto for T01/ }));
  fireEvent.click(screen.getByRole("button", { name: /Confirm veto for T01/ }));
  expect(screen.getByText(/Veto confirmed for T01 .* on this preview\./)).toBeVisible();
  expect(screen.getByText(/new Compare runs use the updated device rules/)).toBeVisible();
  const calls = fetch.mock.calls.length;
  for (let i = 0; i < 3; i += 1) {
    fireEvent.click(button("Switch screens"));
    expect(screen.queryByRole("button", { name: "Preview changes" })).toBeNull();     // hidden, still mounted
    fireEvent.click(button("Switch screens"));
  }
  expect(screen.getByText("Preview revision: r1")).toBeVisible();
  expect(screen.getByText(/Veto confirmed for T01 .* on this preview\./)).toBeVisible();
  expect(button("Apply")).toBeEnabled();
  expect(button("What changed")).toBeEnabled();
  expect(button("Undo this kit")).toBeInTheDocument();
  expect(fetch.mock.calls.length).toBe(calls);                                          // nothing refetched or reset
  // a settings re-check still invalidates the preview made under the old settings
  fireEvent.click(button("Check settings again"));
  await waitFor(() => expect(screen.queryByText("Preview revision: r1")).toBeNull());
});

it("tolerates storage that throws for the pending kit", async () => {
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("denied"); });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new DOMException("full", "QuotaExceededError"); });
  vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => { throw new Error("denied"); });
  setup(url => url === API + "/suite/calibration/status" ? jsonResponse(200, statusDoc("K1")) : unexpectedFetch(url));
  render(<CalibrationScreen />);
  await waitFor(() => expect(button("HDR banner T01")).toBeEnabled());
});
