import { act } from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CompareScreen } from "./CompareScreen";
import { API } from "../suite/config";
import { bodyOf, callsTo, installFetch, jsonResponse, unexpectedFetch } from "../suite/testing/fetchMock";
import type { FetchHandler } from "../suite/testing/fetchMock";
import { devicesDoc, versionInfo, versionInfo07 } from "../suite/testing/fixtures";
import { BAD_JOB_RESPONSE_TEXT } from "../suite/messages";
import {
  changedSettings, localFullBoth, localJustPlayerAlternatives, magnetQuickOverall, magnetSelectionRequired, publicSnapshotsHttp,
} from "./testing/fixtures";
import type { Json } from "./testing/fixtures";

const tick = (ms = 0) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });
beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
});

/** The screen's own reads (version, devices, runs list) plus a test-specific handler for everything else. */
function backend(runs: unknown[], extra: FetchHandler = (url) => unexpectedFetch(url), version = versionInfo07()) {
  return installFetch((url, init) => {
    if (url === `${API}/suite/version`) return jsonResponse(200, version);
    if (url === `${API}/suite/devices`) return jsonResponse(200, devicesDoc());
    if (url === `${API}/comparison/runs` && init?.method !== "POST") return jsonResponse(200, { runs });
    return extra(url, init);
  });
}
const checked = (name: RegExp | string) => screen.getByRole("radio", { name });
const THREE = "D:\\Movies\\A.mkv\nD:\\Movies\\B.mkv\nD:\\Movies\\C.mkv";
const pathsBox = () => screen.getByRole("textbox", { name: /Video files on this computer/ });
const announcer = () => screen.getByRole("status", { name: "Comparison status" });

const OPENSSL_TEXT = "Magnet links are unavailable on this PC (OpenSSL 1.1 files not found); file comparison still works";
/** A failed planning job from the real magnet run snapshot and the real plan job: the run ends `error` with no
 *  RunResult and the job carries the backend's own message (comparison_runs.plan_run). */
function failedPlanning(): { failedRun: Json; failedJob: Json; jobId: string } {
  const run = magnetQuickOverall().run as Json;
  const jobId = "00000000000000000000000000000009";
  const failedRun: Json = { ...run, stage: "error", job_id: jobId };
  const planJob = publicSnapshotsHttp().plan_job as Json;
  const failedJob: Json = {
    ...planJob, status: "error", error: OPENSSL_TEXT, error_kind: "magnet_unavailable",
    events: [{ msg: `Error: ${OPENSSL_TEXT}`, ts: 1700000000 }], run: failedRun,
  };
  return { failedRun, failedJob, jobId };
}

describe("CompareScreen", () => {
  it("defaults Both TV and Quick magnets or Full files without hidden prior choices", async () => {
    backend([]);
    const first = render(<CompareScreen />);
    await tick();
    expect(checked("Downloaded files")).toBeChecked();
    expect(checked(/^Both/)).toBeChecked();
    expect(checked(/^Full/)).toBeChecked();
    expect(checked(/Sony Bravia 8 II \(TV\)/)).toBeChecked();
    expect(screen.getByRole("combobox", { name: "Player" })).toHaveValue("builtin_usb");
    fireEvent.click(checked("Magnet links"));
    expect(checked(/^Quick/)).toBeChecked();
    expect(checked(/^Full/)).toBeDisabled();
    fireEvent.click(checked(/ASUS Vivobook/));
    fireEvent.click(checked(/^Best overall/));
    fireEvent.click(checked("Downloaded files"));
    expect(checked(/^Full/)).toBeChecked();
    first.unmount();

    // a fresh screen never inherits the previous choices (nothing is stored in the browser)
    backend([]);
    render(<CompareScreen />);
    await tick();
    expect(checked(/^Both/)).toBeChecked();
    expect(checked(/Sony Bravia 8 II \(TV\)/)).toBeChecked();
    expect(localStorage.length).toBe(0);
  });

  it("keeps a completed run's frozen choices while the form shows fresh defaults", async () => {
    const detail = localJustPlayerAlternatives();
    backend([detail.run], (url) => url === `${API}/comparison/runs/run-0001` ? jsonResponse(200, detail) : unexpectedFetch(url));
    render(<CompareScreen />);
    await tick();
    const frozen = screen.getByRole("region", { name: "Choices used for this run" });
    expect(frozen).toHaveTextContent("Both");
    expect(frozen).toHaveTextContent("Quick (metadata)");
    expect(frozen).toHaveTextContent("Sony Bravia 8 II (TV)");
    expect(frozen).toHaveTextContent("Just Player");
    expect(frozen).toHaveTextContent("with the other players as alternatives");
    expect(checked(/^Full/)).toBeChecked();                 // the form keeps its own defaults
    fireEvent.click(checked(/^Best overall/));
    expect(frozen).toHaveTextContent("Both");
  });

  it("shows the honest result of a reconnected run with the chosen player first", async () => {
    const detail = localJustPlayerAlternatives();
    backend([detail.run], (url) => url === `${API}/comparison/runs/run-0001` ? jsonResponse(200, detail) : unexpectedFetch(url));
    render(<CompareScreen />);
    for (let i = 0; i < 3; i += 1) await tick(10);
    expect(screen.getByRole("region", { name: "Answer" })).toHaveTextContent("picture not measured");
    const device = screen.getByRole("region", { name: "Best for my device" });
    expect(within(device).getAllByRole("region")[0]).toHaveAccessibleName("Your player: Just Player on the TV (ExoPlayer based)");
    expect(screen.getByRole("table", { name: "File facts side by side" })).toBeInTheDocument();
    expect(document.activeElement).toBe(document.body);
  });

  it("never confirms stale plan or sends a second start on double click", async () => {
    const http = publicSnapshotsHttp();
    const pending = { run: (http.plan_job as Json).run, result: null };
    let confirmed = false;
    const fetch = backend([], (url, init) => {
      if (url === `${API}/comparison/plans`) return jsonResponse(200, { job_id: "plan-job", run_id: "run-0001" });
      if (url === `${API}/job/plan-job`) return jsonResponse(200, http.plan_job);
      if (url === `${API}/comparison/runs/run-0001`) return jsonResponse(200, confirmed ? http.detail : pending);
      if (url === `${API}/comparison/runs` && init?.method === "POST") {
        confirmed = true;
        return jsonResponse(200, { job_id: "run-job", run_id: "run-0001" });
      }
      if (url === `${API}/job/run-job`) return jsonResponse(200, http.run_job);
      if (url === `${API}/comparison/runs/run-0001/cancel`) return jsonResponse(200, { run_id: "run-0001", stage: "cancelled" });
      return unexpectedFetch(url);
    });
    render(<CompareScreen />);
    await tick();
    const paths = screen.getByRole("textbox", { name: /Video files on this computer/ });
    fireEvent.change(paths, { target: { value: "D:\\Movies\\A.mkv\nD:\\Movies\\B.mkv\nD:\\Movies\\C.mkv" } });
    fireEvent.click(screen.getByRole("button", { name: "Check files and estimate" }));
    await tick();
    expect(bodyOf(fetch, "/comparison/plans")).toMatchObject({
      input_kind: "files", results: "both", depth: "full", device: "sony_bravia_8_ii", primary_player: "builtin_usb", player: "all",
    });
    const plan = screen.getByRole("region", { name: "Plan" });
    expect(plan).toHaveTextContent("VideoSuite's own rough estimate: about 45 minutes for 3 pair(s)");
    expect(plan).toHaveTextContent("3 of 3 versions are readable");
    expect(plan).not.toHaveTextContent("%");

    // editing the inputs makes the shown plan stale: it can no longer be started
    fireEvent.change(paths, { target: { value: "D:\\Movies\\A.mkv\nD:\\Movies\\B.mkv" } });
    expect(screen.getByRole("button", { name: /^Start/ })).toBeDisabled();
    expect(screen.getByText("Your choices changed after this check. Check again before starting.")).toBeInTheDocument();
    fireEvent.change(paths, { target: { value: "D:\\Movies\\A.mkv\nD:\\Movies\\B.mkv\nD:\\Movies\\C.mkv" } });
    expect(screen.getByRole("button", { name: /^Start/ })).toBeDisabled();       // an edit back is still a new plan
    fireEvent.click(screen.getByRole("button", { name: "Check files and estimate" }));
    await tick();
    expect(callsTo(fetch, "/comparison/plans")).toHaveLength(2);

    const start = screen.getByRole("button", { name: /^Start/ });
    expect(start).toBeEnabled();
    fireEvent.click(start);
    fireEvent.click(start);
    await tick();
    await tick();
    expect(callsTo(fetch, "/comparison/runs").filter(([, init]) => init?.method === "POST")).toHaveLength(1);
    expect(screen.getByRole("region", { name: "Choices used for this run" })).toHaveTextContent("Full (picture comparison)");
  });

  it("asks for one explicit video file per multi-video magnet link and plans again with it", async () => {
    const selection = magnetSelectionRequired();
    const fetch = backend([selection.run], (url) => {
      if (url === `${API}/comparison/runs/run-0001`) return jsonResponse(200, selection);
      if (url === `${API}/comparison/plans`) return jsonResponse(200, { job_id: "plan-2", run_id: "run-0002" });
      if (url === `${API}/job/plan-2`) return jsonResponse(404, { detail: "Job not found." });
      if (url === `${API}/comparison/runs/run-0002`) return jsonResponse(404, { detail: "Run not found." });
      if (url === `${API}/comparison/runs/run-0001/cancel`) return jsonResponse(200, { run_id: "run-0001", stage: "cancelled" });
      return unexpectedFetch(url);
    });
    render(<CompareScreen />);
    for (let i = 0; i < 3; i += 1) await tick(10);
    const plan = screen.getByRole("region", { name: "Plan" });
    const again = within(plan).getByRole("button", { name: "Check again with these files" });
    expect(again).toBeDisabled();                       // nothing is picked automatically
    const group = within(plan).getByRole("radiogroup", { name: "Video file for link 1" });
    expect(within(group).getAllByRole("radio").map((r) => r.closest("label")?.textContent)).toEqual([
      expect.stringContaining("A.mkv"), expect.stringContaining("C.mkv"),
    ]);
    fireEvent.click(within(group).getByRole("radio", { name: /C\.mkv/ }));
    // any further link that also needs a choice (the backend fixture may list two links) gets its last entry
    const run = selection.run as { candidates: Array<{ uri: string; status: string; entries?: Array<{ index: number }> }>;
                                   request: { magnets: Json[] } };
    const picks = new Map<string, number>([["magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567", 2]]);
    run.candidates.filter((c) => c.status === "selection_required").forEach((c, i) => {
      if (i === 0) return;
      const radios = within(within(plan).getByRole("radiogroup", { name: `Video file for link ${i + 1}` })).getAllByRole("radio");
      fireEvent.click(radios[radios.length - 1]);
      picks.set(c.uri, c.entries![c.entries!.length - 1].index);
    });
    fireEvent.click(again);
    await tick(10);
    const body = bodyOf(fetch, "/comparison/plans") as Json;
    expect(body.magnets).toEqual(run.request.magnets.map((m) => (picks.has(m.uri as string)
      ? { ...m, file_index: picks.get(m.uri as string) } : m)));
    expect(callsTo(fetch, "/comparison/runs/run-0001/cancel")).toHaveLength(1);   // its own old plan, before re-planning
    expect(body).toMatchObject({ input_kind: "magnets", depth: "quick", results: "overall" });
  });

  it("explains a missing or too-old suite with the compatible-update hint and blocks planning", async () => {
    backend([], (url) => unexpectedFetch(url), versionInfo({ status: "ok", tool_version: "0.6.0" }));
    render(<CompareScreen />);
    await tick();
    expect(screen.getByRole("status", { name: "VideoSuite" })).toHaveTextContent("needs VideoSuite 0.7.0 or later");
    expect(screen.getByRole("button", { name: "Check files and estimate" })).toBeDisabled();
  });

  it("shows a failed check in plain words with the server's own message, also after a reload", async () => {
    const { failedRun, failedJob, jobId } = failedPlanning();
    const runs: unknown[] = [];
    const extra: FetchHandler = (url) => {
      if (url === `${API}/comparison/plans`) return jsonResponse(200, { job_id: jobId, run_id: failedRun.run_id });
      if (url === `${API}/job/${jobId}`) return jsonResponse(200, failedJob);
      if (url === `${API}/comparison/runs/${String(failedRun.run_id)}`) return jsonResponse(200, { run: failedRun, result: null });
      return unexpectedFetch(url);
    };
    backend(runs, extra);
    const first = render(<CompareScreen />);
    await tick();
    fireEvent.click(checked("Magnet links"));
    fireEvent.change(screen.getByRole("textbox", { name: /Magnet links/ }), { target: {
      value: `magnet:?xt=urn:btih:${"1".repeat(40)}\nmagnet:?xt=urn:btih:${"2".repeat(40)}` } });
    const check = screen.getByRole("button", { name: "Check links and estimate" });
    check.focus();
    fireEvent.click(check);
    for (let i = 0; i < 3; i += 1) await tick(10);
    expect(screen.getByRole("alert")).toHaveTextContent(OPENSSL_TEXT);
    expect(screen.getByRole("heading", { name: "Stopped with a problem" })).toHaveFocus();
    expect(announcer()).toHaveTextContent("Stopped with a problem");
    fireEvent.click(screen.getByRole("button", { name: "New comparison" }));
    await tick();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("heading", { name: "1. What are you comparing?" })).toHaveFocus();
    first.unmount();

    // after a reload the failed check is found again and its message is read from its job
    runs.push(failedRun);
    backend(runs, extra);
    render(<CompareScreen />);
    for (let i = 0; i < 3; i += 1) await tick(10);
    expect(screen.getByRole("alert")).toHaveTextContent(OPENSSL_TEXT);
    expect(screen.getByRole("button", { name: "New comparison" })).toBeEnabled();
    expect(document.activeElement).toBe(document.body);         // a reload never moves focus by itself
  });

  it("refuses a malformed result with a clear message instead of crashing", async () => {
    const full = localFullBoth();
    const devices = ((full.result as Json).device as Json).devices as Json;
    (((devices[Object.keys(devices)[0]] as Json).ranking as Json[])[0]).device_score = "invalid";
    const runId = String((full.run as Json).run_id);
    backend([full.run], (url) => url === `${API}/comparison/runs/${runId}` ? jsonResponse(200, full) : unexpectedFetch(url));
    render(<CompareScreen />);
    for (let i = 0; i < 3; i += 1) await tick(10);
    expect(screen.getByRole("alert")).toHaveTextContent(BAD_JOB_RESPONSE_TEXT);
    expect(screen.queryByRole("region", { name: "Answer" })).toBeNull();
  });

  it("states the pairs that will run and the submitted set without contradicting itself", async () => {
    const http = publicSnapshotsHttp();
    const planJob = http.plan_job as Json;
    const run = planJob.run as Json;
    Object.assign((run.candidates as Json[])[2], { status: "unreadable", record_id: null, reason: "could not be probed" });
    run.estimate = { pairs: 1, minutes: 15 };
    backend([], (url) => {
      if (url === `${API}/comparison/plans`) return jsonResponse(200, { job_id: "plan-job", run_id: "run-0001" });
      if (url === `${API}/job/plan-job`) return jsonResponse(200, planJob);
      if (url === `${API}/comparison/runs/run-0001`) return jsonResponse(200, { run, result: null });
      return unexpectedFetch(url);
    });
    render(<CompareScreen />);
    await tick();
    fireEvent.change(pathsBox(), { target: { value: THREE } });
    fireEvent.click(screen.getByRole("button", { name: "Check files and estimate" }));
    await tick();
    const plan = screen.getByRole("region", { name: "Plan" });
    expect(plan).toHaveTextContent("2 of 3 versions are readable");
    expect(plan).toHaveTextContent("about 15 minutes for 1 pair(s)");
    expect(plan).toHaveTextContent("Comparisons that will run: 1 (every pair of the 2 readable versions)");
    expect(plan).toHaveTextContent("Versions submitted: 3, which would make 3 possible pairs");
    expect(plan).not.toHaveTextContent("pair(s) for all");
  });

  it("clears an earlier error as soon as the inputs change", async () => {
    backend([], (url) => url === `${API}/comparison/plans` ? jsonResponse(404, { detail: "path not found" }) : unexpectedFetch(url));
    render(<CompareScreen />);
    await tick();
    fireEvent.change(pathsBox(), { target: { value: THREE } });
    fireEvent.click(screen.getByRole("button", { name: "Check files and estimate" }));
    await tick();
    expect(screen.getByRole("alert")).toHaveTextContent("Not found");
    fireEvent.change(pathsBox(), { target: { value: "D:\\Movies\\A.mkv\nD:\\Movies\\B.mkv" } });
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("does not carry a run's USB file system or audio output into the next comparison", async () => {
    const detail = localJustPlayerAlternatives();
    backend([detail.run], (url) => url === `${API}/comparison/runs/run-0001` ? jsonResponse(200, detail) : unexpectedFetch(url));
    render(<CompareScreen />);
    for (let i = 0; i < 3; i += 1) await tick(10);
    fireEvent.change(screen.getByRole("combobox", { name: "USB file system" }), { target: { value: "fat32" } });
    fireEvent.change(screen.getByRole("combobox", { name: "Audio output" }), { target: { value: "soundbar" } });
    fireEvent.click(checked(/ASUS Vivobook/));
    expect(screen.getByRole("combobox", { name: "USB file system" })).toHaveValue("");
    expect(screen.getByRole("combobox", { name: "Audio output" })).toHaveValue("");
    fireEvent.click(checked(/Sony Bravia 8 II \(TV\)/));
    fireEvent.change(screen.getByRole("combobox", { name: "Audio output" }), { target: { value: "soundbar" } });
    fireEvent.click(screen.getByRole("button", { name: "New comparison" }));
    await tick();
    expect(screen.getByRole("combobox", { name: "Audio output" })).toHaveValue("");
  });

  it("offers to discard an older never-started plan while showing the newest finished run", async () => {
    const done = localJustPlayerAlternatives();
    const oldPlan = { ...((publicSnapshotsHttp().plan_job as Json).run as Json), run_id: "run-0000" };
    const fetch = backend([done.run, oldPlan], (url) => {
      if (url === `${API}/comparison/runs/run-0001`) return jsonResponse(200, done);
      if (url === `${API}/comparison/runs/run-0000/cancel`) return jsonResponse(200, { run_id: "run-0000", stage: "cancelled" });
      return unexpectedFetch(url);
    });
    render(<CompareScreen />);
    for (let i = 0; i < 3; i += 1) await tick(10);
    expect(screen.getByRole("region", { name: "Answer" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Plan" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Discard the earlier unstarted check" }));
    await tick();
    expect(callsTo(fetch, "/comparison/runs/run-0000/cancel")).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Discard the earlier unstarted check" })).toBeNull();
  });

  it("announces each step politely and moves focus to the new step after Check, Start and Cancel", async () => {
    const http = publicSnapshotsHttp();
    const planJob = http.plan_job as Json;
    const runJob = http.run_job as Json;
    const jobId = String((runJob.run as Json).job_id);
    const line = "[1/3] A.mkv vs B.mkv: started (accurate)";
    const runningRun = { ...(runJob.run as Json), stage: "pixels" };
    const runningJob = { ...runJob, status: "running", results: null, current: line, run: runningRun,
                         events: [{ msg: line, ts: 1700000000 }] };
    const cancelledRun = { ...(runJob.run as Json), stage: "cancelled" };
    const cancelledJob = { ...runJob, status: "cancelled", results: null, error: null, error_kind: null, run: cancelledRun };
    let phase: "pending" | "running" | "cancelled" = "pending";
    backend([], (url, init) => {
      if (url === `${API}/comparison/plans`) return jsonResponse(200, { job_id: "plan-job", run_id: "run-0001" });
      if (url === `${API}/job/plan-job`) return jsonResponse(200, planJob);
      if (url === `${API}/comparison/runs/run-0001`) {
        return jsonResponse(200, { run: phase === "pending" ? planJob.run : phase === "running" ? runningRun : cancelledRun, result: null });
      }
      if (url === `${API}/comparison/runs` && init?.method === "POST") { phase = "running"; return jsonResponse(200, { job_id: jobId, run_id: "run-0001" }); }
      if (url === `${API}/job/${jobId}`) return jsonResponse(200, phase === "running" ? runningJob : cancelledJob);
      if (url === `${API}/job/${jobId}/cancel`) { phase = "cancelled"; return jsonResponse(200, { ok: true }); }
      return unexpectedFetch(url);
    });
    render(<CompareScreen />);
    await tick();
    expect(announcer()).toHaveTextContent("");
    fireEvent.change(pathsBox(), { target: { value: THREE } });
    const check = screen.getByRole("button", { name: "Check files and estimate" });
    check.focus();
    fireEvent.click(check);
    for (let i = 0; i < 2; i += 1) await tick(10);
    expect(screen.getByRole("heading", { name: "Plan" })).toHaveFocus();
    expect(announcer()).toHaveTextContent("Plan ready: 3 of 3 versions are readable. Press Start to begin.");

    fireEvent.click(screen.getByRole("button", { name: /^Start/ }));
    for (let i = 0; i < 3; i += 1) await tick(10);
    expect(screen.getByRole("heading", { name: "Progress" })).toHaveFocus();
    expect(announcer()).toHaveTextContent("Comparing the pictures pair by pair");
    const log = screen.getByRole("log", { name: "Progress messages" });
    expect(log).toHaveTextContent(line);
    expect(log.closest("details")).toBeNull();                   // reachable without opening anything

    fireEvent.click(screen.getByRole("button", { name: "Cancel this run" }));
    for (let i = 0; i < 4; i += 1) await tick(3000);
    expect(screen.queryByRole("region", { name: "Progress" })).toBeNull();
    expect(screen.getByRole("heading", { name: "Cancelled" })).toHaveFocus();
    expect(announcer()).toHaveTextContent("Cancelled");
    expect(document.activeElement).not.toBe(document.body);
  });

  it.each(["error", "done"])("keeps the complete Best overall answer when changed settings withheld only the device answer (stage %s)", async (stage) => {
    // Read the regenerated HTTP envelope unchanged: an error stage must not hide the independent Overall answer.
    const detail = changedSettings().run as Json;
    const result = detail.result as Json;
    expect((detail.run as Json).stage).toBe("error");
    expect((result.request as Json).results).toBe("both");
    expect(result.device).toBeNull();
    expect((result.coverage as Json).complete).toBe(true);
    const overall = result.overall as Json;
    // The error case uses the real file unchanged; done is derived only to prove the answer does not require success.
    if (stage === "done") {
      (detail.run as Json).stage = stage;
      (result.snapshot as Json).stage = stage;
    }
    const runId = String((detail.run as Json).run_id);
    backend([detail.run], (url) => url === `${API}/comparison/runs/${runId}` ? jsonResponse(200, detail) : unexpectedFetch(url));
    render(<CompareScreen />);
    for (let i = 0; i < 3; i += 1) await tick(10);
    const top = (overall.quality as Json[]).find((q) => q.rank === 1)!;
    const answer = screen.getByRole("region", { name: "Answer" });
    expect(answer).toHaveTextContent(`Best overall (measured picture): ${String(top.label)}`);
    expect(answer).toHaveTextContent("the device answer was withheld because your settings or the device rules changed");
    expect(answer).toHaveTextContent("Run it again to get the device answer with the new rules");
    expect(screen.getByRole("region", { name: "Best overall" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Best for my device" })).toBeNull();
    const coverage = screen.getByRole("region", { name: "Coverage" });
    if (stage === "error") expect(coverage).toHaveTextContent("The Best overall answer is complete; only the device answer was withheld.");
    expect(coverage).not.toHaveTextContent("anything shown is partial");
  });

  it("names the Check button for what is being checked", async () => {
    backend([]);
    render(<CompareScreen />);
    await tick();
    expect(screen.getByRole("button", { name: "Check files and estimate" })).toBeInTheDocument();
    fireEvent.click(checked("Magnet links"));
    expect(screen.getByRole("button", { name: "Check links and estimate" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Check files and estimate" })).toBeNull();
  });

  it("shows running progress text from the backend without inventing percentages or times", async () => {
    const http = publicSnapshotsHttp();
    const runJob = http.run_job as Json;
    const running = { ...runJob, status: "running", results: null, current: "[1/3] A.mkv vs B.mkv: started (accurate)",
                      run: { ...(runJob.run as Json), stage: "pixels" } };
    const fetch = backend([running.run], (url) => {
      if (url === `${API}/comparison/runs/run-0001`) return jsonResponse(200, { run: running.run, result: null });
      if (url === `${API}/job/${String(runJob.run && (runJob.run as Json).job_id)}`) return jsonResponse(200, running);
      return unexpectedFetch(url);
    });
    render(<CompareScreen />);
    for (let i = 0; i < 4; i += 1) await tick(10);       // timers set during a tick run 1 ms later
    const progress = screen.getByRole("region", { name: "Progress" });
    expect(progress).toHaveTextContent("[1/3] A.mkv vs B.mkv: started (accurate)");
    expect(progress).toHaveTextContent("You can close this tab. Reopen the shortcut to return. Use Quit to stop the app.");
    expect(within(progress).queryByRole("progressbar")).toBeNull();
    expect(document.activeElement).toBe(document.body);
    expect(callsTo(fetch, `/job/${String((runJob.run as Json).job_id)}`).length).toBeGreaterThan(0);
  });
});
