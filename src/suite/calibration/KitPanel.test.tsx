import { act } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { KitPanel } from "./KitPanel";
import { ChecklistTable } from "./ChecklistTable";
import { useCalibration } from "./useCalibration";
import { API } from "../config";
import { bodyOf, callsTo, installFetch, jsonResponse, unexpectedFetch } from "../testing/fetchMock";
import { jobOf } from "../testing/fixtures";

const envelope = (sub: string) => ({ schema: `videosuite-calibrate-${sub}/1`, tool_version: "0.5.0" });
const emptyStatus = () => ({ ...envelope("status"), recovery_required: false, kit: null, session: null, answers: [], summary: { done: 0, total: 0, matches: 0, differs: 0 } });
function planDoc() {
  return { ...envelope("plan"), catalogue_version: 1, players: ["builtin_usb"], tests: ["ready", "needs_real_file", "unavailable", "manual"].map((status, i) => ({
    test: `T0${i + 1}`, title: status, class: "informational", players: ["builtin_usb"], manual: status === "manual", instruction: "Play", question: "Banner?", outcomes: [],
    prediction: { builtin_usb: { outcome: null, text: "Unknown banner", evidence: "unknown" } },
    source: { kind: status === "manual" ? "manual" : "excerpt", status: status === "manual" ? "ready" : status, file: null, est_bytes: i === 0 ? 288000000 : null, reason: null },
  })) };
}
function Harness() {
  const calibration = useCalibration(null);
  return <><KitPanel calibration={calibration} />{calibration.status?.kit && <ChecklistTable descriptors={calibration.status.kit.descriptors} answers={calibration.answers} onSelect={() => {}} />}</>;
}
beforeEach(() => { vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true); });
function output(value = "D:\\Calibration Kit") { fireEvent.change(screen.getByLabelText("Kit output folder"), { target: { value } }); }
it("plans with unchanged deduplicated paths, evidence and estimated size; builds once and cancels with cleanup guidance", async () => {
  let cancelled = false;
  const fetch = installFetch(url => {
    if (url === API + "/suite/calibration/status") return jsonResponse(200, emptyStatus());
    if (url === API + "/suite/calibration/plan") return jsonResponse(200, planDoc());
    if (url === API + "/suite/calibration/build") return jsonResponse(200, { job_id: "build" });
    if (url === API + "/job/build") return jsonResponse(200, jobOf({ status: cancelled ? "cancelled" : "running", current: "<img src=x>", events: [{ msg: "CLI emitted line", ts: 0 }] }));
    if (url === API + "/job/build/cancel") { cancelled = true; return jsonResponse(200, {}); }
    return unexpectedFetch(url);
  });
  const { container } = render(<Harness />); output();
  const path = "D:\\Fixtures\\Тест & 100%.mkv";
  fireEvent.change(screen.getByLabelText(/Video files/), { target: { value: `"${path}"\n${path}` } });
  fireEvent.click(screen.getByRole("button", { name: "Plan kit" }));
  expect(await screen.findByText(/Estimated total: about 288.0 MB/)).toBeVisible();
  expect(screen.getAllByText(/Built-in USB player:/)).toHaveLength(4);
  expect(bodyOf(fetch, "/suite/calibration/plan")).toEqual({ paths: [path], deep_dv: false });
  expect(screen.getByText("needs a real file")).toBeVisible(); expect(screen.getAllByText("manual").length).toBeGreaterThan(0);
  screen.getAllByText("Unknown banner").forEach(p => expect(p.parentElement).toHaveTextContent("evidence: unknown"));
  const build = screen.getByRole("button", { name: "Prepare kit" }); fireEvent.click(build); fireEvent.click(build);
  expect(await screen.findByText("<img src=x>")).toBeVisible(); expect(container.querySelector("img")).toBeNull();
  expect(screen.getByText("CLI emitted line")).toBeInTheDocument();
  expect(callsTo(fetch, "/suite/calibration/build")).toHaveLength(1);
  expect(bodyOf(fetch, "/suite/calibration/build")).toEqual({ paths: [path], deep_dv: false, out: "D:\\Calibration Kit" });
  fireEvent.click(screen.getByRole("button", { name: "Cancel build" }));
  await waitFor(() => expect(callsTo(fetch, "/job/build/cancel")).toHaveLength(1));
  expect(screen.getByText(/staging folder.*next build/i)).toBeVisible();
});
it("requires a displayed plan for the current paths and deep-scan input before building", async () => {
  installFetch(url => url === API + "/suite/calibration/status" ? jsonResponse(200, emptyStatus())
    : url === API + "/suite/calibration/plan" ? jsonResponse(200, planDoc()) : unexpectedFetch(url));
  render(<Harness />); output();
  expect(screen.getByRole("button", { name: "Prepare kit" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Plan kit" }));
  await screen.findByText(/Estimated total:/);
  expect(screen.getByRole("button", { name: "Prepare kit" })).toBeEnabled();
  fireEvent.change(screen.getByLabelText(/Video files/), { target: { value: "D:\\new.mkv" } });
  expect(screen.getByRole("button", { name: "Prepare kit" })).toBeDisabled();
  expect(screen.queryByText(/Estimated total:/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Plan kit" }));
  await screen.findByText(/Estimated total:/);
  expect(screen.getByRole("button", { name: "Prepare kit" })).toBeEnabled();
  fireEvent.click(screen.getByLabelText(/Deep Dolby Vision scan/));
  expect(screen.getByRole("button", { name: "Prepare kit" })).toBeDisabled();
});

it("validates output and file-list policies without sending requests", async () => {
  const fetch = installFetch(url => url === API + "/suite/calibration/status" ? jsonResponse(200, emptyStatus())
    : url === API + "/suite/calibration/plan" ? jsonResponse(200, planDoc()) : unexpectedFetch(url));
  render(<Harness />); output("D:\\bad&folder");
  fireEvent.click(screen.getByRole("button", { name: "Plan kit" }));
  await screen.findByText(/Estimated total:/);
  fireEvent.click(screen.getByRole("button", { name: "Prepare kit" }));
  expect(screen.getByText(/cannot contain any/)).toBeVisible();
  output(); fireEvent.change(screen.getByLabelText(/Video files/), { target: { value: "relative.mkv" } });
  fireEvent.click(screen.getByRole("button", { name: "Plan kit" })); expect(screen.getByText(/Line 1: use a full local path/)).toBeVisible();
  await act(async () => {}); expect(fetch).toHaveBeenCalledTimes(2);
});
it("runs deep discovery as a cancellable job and shows final build size separately", async () => {
  const fetch = installFetch(url => {
    if (url === API + "/suite/calibration/status") return jsonResponse(200, emptyStatus());
    if (url === API + "/suite/calibration/plan") return jsonResponse(200, { job_id: "deep" });
    if (url === API + "/job/deep") return jsonResponse(200, jobOf({ status: "done", results: planDoc() }));
    if (url === API + "/suite/calibration/build") return jsonResponse(200, { job_id: "built" });
    if (url === API + "/job/built") return jsonResponse(200, jobOf({ status: "done", results: { ...envelope("build"), kit_id: "new", out: "D:\\Calibration Kit", descriptor_path: "D:\\Calibration Kit\\kit.json", total_bytes: 1500000000, descriptors: [], unavailable: [{ test: "T03", reason: "<b>missing</b>" }] } }));
    return unexpectedFetch(url);
  });
  const { container } = render(<Harness />); output(); fireEvent.click(screen.getByLabelText(/Deep Dolby Vision scan/));
  fireEvent.click(screen.getByRole("button", { name: "Plan kit" }));
  expect(await screen.findByText(/Estimated total: about 288.0 MB/)).toBeVisible(); expect(bodyOf(fetch, "/suite/calibration/plan")).toEqual({ paths: [], deep_dv: true });
  fireEvent.click(screen.getByRole("button", { name: "Prepare kit" }));
  expect(await screen.findByText(/Final kit size: about 1.5 GB/)).toBeVisible(); expect(screen.getByText(/<b>missing<\/b>/)).toBeVisible(); expect(container.querySelector("b")).toBeNull();
});
it("explains disconnected drives and a concurrent folder build", async () => {
  let missing = true;
  installFetch(url => {
    if (url === API + "/suite/calibration/status") return jsonResponse(200, emptyStatus());
    if (url === API + "/suite/calibration/plan") return missing ? jsonResponse(404, { detail: "source missing" }) : jsonResponse(200, planDoc());
    if (url === API + "/suite/calibration/build") return jsonResponse(409, { detail: "build already running" });
    return unexpectedFetch(url);
  });
  render(<Harness />); output(); fireEvent.click(screen.getByRole("button", { name: "Plan kit" }));
  expect(await screen.findByText(/connect the drive that holds these files/)).toBeVisible();
  expect(screen.getByRole("button", { name: "Prepare kit" })).toBeDisabled();
  missing = false;
  fireEvent.click(screen.getByRole("button", { name: "Plan kit" }));
  await screen.findByText(/Estimated total:/);
  fireEvent.click(screen.getByRole("button", { name: "Prepare kit" })); expect(await screen.findByText(/build already running/)).toBeVisible();
});
it("reloads the saved offline descriptor without planning, probing or building", async () => {
  const descriptor = { test: "T16", player: "builtin_usb", title: "Saved offline test", class: "informational", rule: null, instruction: "Play", question: "Played?", outcomes: [], conditions: [], manual: true, file: null,
    source: { kind: "manual", file: null, verification: null }, prediction: { outcome: null, text: "Frozen offline prediction", evidence: "forum" } };
  const fetch = installFetch(url => url === API + "/suite/calibration/status" ? jsonResponse(200, { ...emptyStatus(), kit: { id: "saved", catalogue_version: 1, built_at: "saved time", out: "D:\\Offline Kit", descriptors: [descriptor] } }) : unexpectedFetch(url));
  const first = render(<Harness />); expect(await screen.findByText("Frozen offline prediction")).toBeVisible(); first.unmount();
  render(<Harness />); expect(await screen.findByText("Frozen offline prediction")).toBeVisible(); expect(screen.getByText("evidence: forum")).toBeVisible(); expect(fetch).toHaveBeenCalledTimes(2);
});
