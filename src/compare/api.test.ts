import { describe, expect, it } from "vitest";
import { cancelRun, chooseFiles, confirmRun, getRun, getRunJob, listRuns, planRun } from "./api";
import { SuiteApiError } from "../suite/api";
import { API } from "../suite/config";
import { bodyOf, callsTo, installFetch, jsonResponse, unexpectedFetch } from "../suite/testing/fetchMock";
import { changedSettings, localFullBoth, localJustPlayerAlternatives, localQuickBoth, publicSnapshotsHttp } from "./testing/fixtures";
import type { Json } from "./testing/fixtures";
import type { RunRequest } from "./types";

const REQUEST: RunRequest = {
  input_kind: "files", results: "both", depth: "full",
  paths: ['D:\\Médias\\Tom & Jerry (2021) 100%\\Спайдер-Мен "x".mkv', "D:\\Media\\B.mkv"],
  device: "sony_bravia_8_ii", primary_player: "just_player", player: "all", include_alternatives: true,
  fs: null, lang: "eng", audio_output: null,
};

describe("unified run HTTP client", () => {
  it("plans and confirms with exact JSON bodies and never puts paths in URLs", async () => {
    const fetch = installFetch((url) => {
      if (url === `${API}/comparison/plans`) return jsonResponse(200, { job_id: "j-plan", run_id: "run-0001" });
      if (url === `${API}/comparison/runs`) return jsonResponse(200, { job_id: "j-run", run_id: "run-0001" });
      return unexpectedFetch(url);
    });
    await expect(planRun(REQUEST)).resolves.toEqual({ job_id: "j-plan", run_id: "run-0001" });
    await expect(confirmRun("run-0001", "rev-302aa61015d20d93")).resolves.toEqual({ job_id: "j-run", run_id: "run-0001" });
    expect(bodyOf(fetch, "/comparison/plans")).toEqual(REQUEST);
    expect(bodyOf(fetch, "/comparison/runs")).toEqual({ run_id: "run-0001", plan_revision: "rev-302aa61015d20d93" });
    for (const [url, init] of fetch.mock.calls) {
      expect(String(url)).not.toContain("Movies");
      expect(init?.method).toBe("POST");
      expect(init?.headers).toEqual({ "Content-Type": "application/json" });
    }
  });

  it("feeds the actual public HTTP response bytes through the guards", async () => {
    const http = publicSnapshotsHttp();
    const fetch = installFetch((url) => {
      if (url === `${API}/comparison/runs`) return jsonResponse(200, http.list);
      if (url === `${API}/comparison/runs/run-0001`) return jsonResponse(200, http.detail);
      if (url === `${API}/job/plan-job`) return jsonResponse(200, http.plan_job);
      if (url === `${API}/job/run-job`) return jsonResponse(200, http.run_job);
      return unexpectedFetch(url);
    });
    expect((await listRuns()).runs[0].stage).toBe("done");
    const detail = await getRun("run-0001");
    expect(detail.result?.snapshot).toEqual(detail.run);
    expect((await getRunJob("plan-job")).run?.stage).toBe("awaiting_confirmation");
    expect((await getRunJob("run-job")).results?.run_id).toBe("run-0001");
    for (const [, init] of fetch.mock.calls) expect(init?.method).toBe("GET");
  });

  it("turns a leaked internal reports_dir or a broken document into an unusable-response error", async () => {
    const detail = localJustPlayerAlternatives();
    (detail.run as Json).reports_dir = "C:\\data\\runs\\run-0001\\reports";
    const broken = localJustPlayerAlternatives();
    delete (broken.result as Json).coverage;
    installFetch((url) => {
      if (url === `${API}/comparison/runs/run-0001`) return jsonResponse(200, detail);
      if (url === `${API}/comparison/runs/run-0002`) return jsonResponse(200, broken);
      return unexpectedFetch(url);
    });
    for (const id of ["run-0001", "run-0002"]) {
      const err = await getRun(id).catch((e: unknown) => e);
      expect(err).toBeInstanceOf(SuiteApiError);
      expect(err).toMatchObject({ status: 200, kind: "bad_response" });
      expect(String((err as Error).message)).not.toContain("C:\\data");
    }
  });

  it("turns an invalid nested score in the real Full result into a clean unusable-response error", async () => {
    const full = localFullBoth();
    const device = (full.result as Json).device as Json;
    const devices = device.devices as Json;
    const rows = (devices[Object.keys(devices)[0]] as Json).ranking as Json[];
    rows[0].device_score = "invalid";
    const quick = localQuickBoth();
    delete ((quick.result as Json).device as Json).tool_version;
    installFetch((url) => {
      if (url === `${API}/comparison/runs/run-0001`) return jsonResponse(200, full);
      if (url === `${API}/comparison/runs/run-0002`) return jsonResponse(200, quick);
      return unexpectedFetch(url);
    });
    for (const id of ["run-0001", "run-0002"]) {
      await expect(getRun(id)).rejects.toMatchObject({ status: 200, kind: "bad_response" });
    }
  });

  it("maps the real 422 refusal, a missing run and a settings_changed job honestly", async () => {
    const changed = changedSettings();
    installFetch((url) => {
      if (url === `${API}/comparison/plans`) {
        return jsonResponse(422, { detail: "invalid run request: body: Value error, player must be all with alternatives, otherwise the primary player" });
      }
      if (url === `${API}/comparison/runs/run-gone`) return jsonResponse(404, { detail: "Run not found." });
      if (url === `${API}/job/j-changed`) return jsonResponse(200, changed.job);
      return unexpectedFetch(url);
    });
    await expect(planRun(REQUEST)).rejects.toMatchObject({ status: 422, kind: "validation" });
    await expect(getRun("run-gone")).rejects.toMatchObject({ status: 404, kind: "not_found" });
    const job = await getRunJob("j-changed");
    expect(job).toMatchObject({ status: "error", error_kind: "settings_changed" });
    expect(job.results?.coverage.complete).toBe(true);
    expect(job.results?.overall).toMatchObject({ schema: "videosuite-rank/1" });
    expect(job.results?.device).toBeNull();
  });

  it("rejects an unusable start answer", async () => {
    installFetch((url) => url === `${API}/comparison/plans` ? jsonResponse(200, { job_id: "j" }) : unexpectedFetch(url));
    await expect(planRun(REQUEST)).rejects.toMatchObject({ kind: "bad_response" });
  });

  it("cancels a pending plan with an empty JSON object", async () => {
    const fetch = installFetch((url) => url === `${API}/comparison/runs/run-0001/cancel`
      ? jsonResponse(200, { run_id: "run-0001", stage: "cancelled" }) : unexpectedFetch(url));
    await expect(cancelRun("run-0001")).resolves.toEqual({ run_id: "run-0001", stage: "cancelled" });
    expect(bodyOf(fetch, "/comparison/runs/run-0001/cancel")).toEqual({});
  });

  it("reports an unavailable chooser so the user can paste paths", async () => {
    installFetch((url) => {
      if (url === `${API}/app/bootstrap`) return jsonResponse(503, { detail: "App controls require the packaged launcher." });
      return unexpectedFetch(url);
    });
    await expect(chooseFiles()).rejects.toMatchObject({ status: 503 });
  });

  it("chooses files through the privileged chooser with an in-memory token only", async () => {
    let bootstraps = 0;
    const fetch = installFetch((url, init) => {
      if (url === `${API}/app/bootstrap`) { bootstraps += 1; return jsonResponse(200, { token: `secret-${bootstraps}`, session_id: "s1" }); }
      if (url === `${API}/app/choose-files`) {
        const token = (init?.headers as Record<string, string>)["X-VideoSuite-Token"];
        if (token === "secret-1" && bootstraps === 1 && callsTo(fetch, "/app/choose-files").length === 1) {
          return jsonResponse(403, { detail: "App control authorization required." });
        }
        return jsonResponse(200, { paths: ["D:\\Movies\\A.mkv", "D:\\Movies\\B.mkv"], cancelled: false });
      }
      return unexpectedFetch(url);
    });
    await expect(chooseFiles()).resolves.toEqual({ paths: ["D:\\Movies\\A.mkv", "D:\\Movies\\B.mkv"], cancelled: false });
    expect(bootstraps).toBe(2);                     // a refused token is dropped and re-bootstrapped once
    await chooseFiles();
    expect(bootstraps).toBe(2);                     // reused from module memory
    for (const [url, init] of fetch.mock.calls) {
      expect(String(url)).not.toContain("secret");
      expect(init?.body).toBe("{}");
    }
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  });
});
