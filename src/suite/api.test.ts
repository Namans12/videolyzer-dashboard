import { describe, expect, it } from "vitest";
import {
  apiErrorKind, cancelJob, errorSentence, getDevices, getJob, getSettings, getVersion,
  postCompare, postRank, postScore, SuiteApiError,
} from "./api";
import { API } from "./config";
import { bodyOf, callsTo, installFetch, jsonResponse, unexpectedFetch } from "./testing/fetchMock";
import { devicesDoc, jobOf, suiteSettings, versionInfo } from "./testing/fixtures";

const paths = [
  'D:\\Médias\\Tom & Jerry (2021) 100%\\Спайдер-Мен "x".mkv',
  "D:\\" + "n".repeat(300) + ".mkv",
  "D:\\Media\\special ^ (1).mkv",
];
const networkSentence = "The Dashboard backend is not answering (is it running on http://127.0.0.1:8000?).";

describe("suite HTTP client", () => {
  it("preserves an abort while consuming the response body", async () => {
    const ctrl = new AbortController(); const aborted = new DOMException("aborted", "AbortError");
    installFetch(url => {
      if (url !== `${API}/job/j1`) return unexpectedFetch(url);
      return { ...jsonResponse(200, {}), text: () => { ctrl.abort(); return Promise.reject(aborted); } } as Response;
    });
    await expect(getJob("j1", ctrl.signal)).rejects.toBe(aborted);
  });
  it("gets version and explicit refresh with no request body", async () => {
    const info = versionInfo();
    const fetch = installFetch((url) => {
      if (url === `${API}/suite/version` || url === `${API}/suite/version?refresh=true`) {
        return jsonResponse(200, info);
      }
      return unexpectedFetch(url);
    });
    expect(await getVersion()).toEqual(info);
    expect(await getVersion(true)).toEqual(info);
    expect(fetch).toHaveBeenCalledTimes(2);
    for (const path of ["/suite/version", "/suite/version?refresh=true"]) {
      const [[url, init]] = callsTo(fetch, path);
      expect(url).toBe(API + path);
      expect(init?.method).toBe("GET");
      expect(init?.body).toBeUndefined();
    }
  });

  it("gets devices, settings and a job with the supplied signal", async () => {
    const devices = devicesDoc();
    const settings = suiteSettings();
    const job = jobOf();
    const fetch = installFetch((url) => {
      if (url === `${API}/suite/devices`) return jsonResponse(200, devices);
      if (url === `${API}/suite/settings`) return jsonResponse(200, settings);
      if (url === `${API}/job/a%2Fb%20c`) return jsonResponse(200, job);
      return unexpectedFetch(url);
    });
    const controller = new AbortController();
    expect(await getDevices(controller.signal)).toEqual(devices);
    expect(await getSettings(controller.signal)).toEqual(settings);
    expect(await getJob("a/b c", controller.signal)).toEqual(job);
    expect(fetch).toHaveBeenCalledTimes(3);
    for (const [, init] of fetch.mock.calls) {
      expect(init?.method).toBe("GET");
      expect(init?.body).toBeUndefined();
      expect(init?.signal).toBe(controller.signal);
    }
  });

  it("posts exact score, rank and compare JSON bodies without paths in URLs", async () => {
    const fetch = installFetch((url) => {
      if (["/suite/score", "/suite/rank", "/suite/compare"].some((path) => url === API + path)) {
        return jsonResponse(200, { job_id: "abc" });
      }
      return unexpectedFetch(url);
    });
    const signal = new AbortController().signal;
    const score = { paths, lang: "eng" };
    const rank = { paths, lang: "eng", reports_dir: 'D:\\Médias\\reports & 100% ^ (1) "я"' };
    const compare = { paths, out_dir: rank.reports_dir, mode: "accurate" as const, top: 4 };
    expect(await postScore(score, signal)).toEqual({ job_id: "abc" });
    expect(await postRank(rank, signal)).toEqual({ job_id: "abc" });
    expect(await postCompare(compare, signal)).toEqual({ job_id: "abc" });
    expect(fetch).toHaveBeenCalledTimes(3);
    for (const [path, body] of [["/suite/score", score], ["/suite/rank", rank], ["/suite/compare", compare]] as const) {
      const [[url, init]] = callsTo(fetch, path);
      expect(url).toBe(API + path);
      expect(init?.method).toBe("POST");
      expect(init?.headers).toEqual({ "Content-Type": "application/json" });
      expect(init?.signal).toBe(signal);
      expect(bodyOf(fetch, path)).toEqual(body);
    }
  });

  it.each([
    [400, "bad_request"], [403, "forbidden"], [404, "not_found"], [409, "conflict"],
    [415, "unsupported_media"], [503, "unavailable"], [502, "server"],
  ] as const)("maps HTTP %s to a typed error", async (status, kind) => {
    installFetch((url) => url === `${API}/suite/score`
      ? jsonResponse(status, { detail: "not a supported video file" }) : unexpectedFetch(url));
    const err = await postScore({ paths, lang: "eng" }).catch((error: unknown) => error);
    expect(err).toBeInstanceOf(SuiteApiError);
    expect(err).toMatchObject({ name: "SuiteApiError", status, detail: "not a supported video file", kind });
    expect(apiErrorKind(status)).toBe(kind);
  });

  it("sanitizes the real 422 too_long shape without retaining its input", async () => {
    installFetch((url) => url === `${API}/suite/score` ? jsonResponse(422, {
      detail: [{ type: "too_long", loc: ["body", "paths"],
        msg: "List should have at most 50 items after validation, not 51",
        input: Array.from({ length: 51 }, () => "C:\\secret\\a.mkv"),
        ctx: { field_type: "List", max_length: 50, actual_length: 51 } }],
    }) : unexpectedFetch(url));
    const err = await postScore({ paths }).catch((error: unknown) => error);
    expect(err).toBeInstanceOf(SuiteApiError);
    expect(err).toMatchObject({ status: 422, kind: "validation",
      detail: "paths: List should have at most 50 items after validation, not 51" });
    if (!(err instanceof SuiteApiError)) throw new Error("expected SuiteApiError");
    for (const text of [JSON.stringify(err), err.message, err.detail]) expect(text).not.toContain("secret");
  });

  it("maps a network failure and unknown errors to the backend sentence", async () => {
    installFetch((url) => {
      if (url !== `${API}/suite/version`) return unexpectedFetch(url);
      throw new TypeError("Failed to fetch");
    });
    const err = await getVersion().catch((error: unknown) => error);
    expect(err).toBeInstanceOf(SuiteApiError);
    expect(err).toMatchObject({ status: 0, kind: "network", detail: "" });
    expect(apiErrorKind(0)).toBe("network");
    expect(errorSentence(err)).toBe(networkSentence);
    expect(errorSentence(new Error("x"))).toBe(networkSentence);
  });

  it("rethrows the original AbortError when the caller aborted", async () => {
    const controller = new AbortController();
    controller.abort();
    const aborted = new DOMException("aborted", "AbortError");
    const fetch = installFetch((url) => {
      if (url !== `${API}/suite/version`) return unexpectedFetch(url);
      throw aborted;
    });
    await expect(getVersion(false, controller.signal)).rejects.toBe(aborted);
    expect(aborted).not.toBeInstanceOf(SuiteApiError);
    expect(fetch.mock.calls[0][1]?.signal).toBe(controller.signal);
  });

  it.each(["<html>", "null", "5", '"text"', ""])("rejects unusable successful JSON %s", async (body) => {
    installFetch((url) => url === `${API}/suite/version` ? jsonResponse(200, body) : unexpectedFetch(url));
    await expect(getVersion()).rejects.toMatchObject({ status: 200, kind: "bad_response" });
  });

  it.each([{}, { job_id: 5 }, { job_id: "" }])("rejects an unusable job start %j", async (body) => {
    installFetch((url) => url === `${API}/suite/score` ? jsonResponse(200, body) : unexpectedFetch(url));
    await expect(postScore({ paths })).rejects.toMatchObject({ status: 200, kind: "bad_response" });
  });

  it("encodes job ids and exposes a purged job as 404", async () => {
    const fetch = installFetch((url) => url === `${API}/job/a%2Fb%20c`
      ? jsonResponse(404, { detail: "job not found" }) : unexpectedFetch(url));
    await expect(getJob("a/b c")).rejects.toMatchObject({ status: 404, kind: "not_found" });
    expect(callsTo(fetch, "/job/a%2Fb%20c")).toHaveLength(1);
  });

  it.each([200, 404])("cancels with no JSON header or body (HTTP %s)", async (status) => {
    const signal = new AbortController().signal;
    const fetch = installFetch((url) => url === `${API}/job/j1/cancel`
      ? jsonResponse(status, status === 200 ? { ok: true } : { detail: "job not found" }) : unexpectedFetch(url));
    const result = cancelJob("j1", signal);
    if (status === 200) await expect(result).resolves.toBeUndefined();
    else await expect(result).rejects.toMatchObject({ status: 404, kind: "not_found" });
    const [[, init]] = callsTo(fetch, "/job/j1/cancel");
    expect(init?.method).toBe("POST");
    expect(init?.headers).toBeUndefined();
    expect(init?.body).toBeUndefined();
    expect(init?.signal).toBe(signal);
  });

  it("formats a conflict sentence", () => {
    expect(errorSentence(new SuiteApiError(409, "a comparison is already running")))
      .toBe("Busy: a comparison is already running");
  });
});
