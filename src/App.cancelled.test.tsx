import { act } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { JOB_GONE_TEXT } from "./jobStatus";
import { callsTo, installFetch, jsonResponse, unexpectedFetch } from "./suite/testing/fetchMock";
import { legacyVideo } from "./suite/testing/fixtures";

const API = "";
const path = "D:\\Media\\clip.mkv";

async function advance(ms: number) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms); });
}

function startPath() {
  render(<App />);
  fireEvent.change(document.getElementById("video-path")!, { target: { value: path } });
  fireEvent.click(screen.getByRole("button", { name: "Analyze Path" }));
}

function startMagnet() {
  render(<App />);
  fireEvent.change(document.getElementById("magnet-uri")!, {
    target: { value: "magnet:?xt=urn:btih:abc" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Analyze Magnet" }));
}

function startCompare() {
  render(<App />);
  fireEvent.change(document.getElementById("magnet-chip-input")!, {
    target: { value: "magnet:?xt=urn:btih:a magnet:?xt=urn:btih:b" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Compare 2 Magnets" }));
}

describe("legacy polling terminal outcomes", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    // Vitest globals are disabled, so RTL cannot register its act-environment hook.
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  });

  it.each([
    { flow: "analyze path", start: startPath, startUrl: `/analyze-path/?path=${encodeURIComponent(path)}`, jobId: "j1", message: "Analysis failed." },
    { flow: "magnet", start: startMagnet, startUrl: "/magnet/?fast=true&hdr10plus=false", jobId: "m1", message: "Magnet job failed." },
    { flow: "compare magnets", start: startCompare, startUrl: "/compare-magnets/?fast=true&hdr10plus=false", jobId: "c1", message: "compare-magnets failed." },
  ])("$flow: stops after one unparseable 2xx poll with $message", async ({ start, startUrl, jobId, message }) => {
    const fetch = installFetch((url) => {
      if (url.startsWith(`${API}${startUrl}`)) return jsonResponse(200, { job_id: jobId });
      if (url === `${API}/job/${jobId}`) return jsonResponse(200, "<html>");
      return unexpectedFetch(url);
    });
    start();
    await advance(0);
    expect(callsTo(fetch, `/job/${jobId}`)).toHaveLength(1);
    expect.soft(screen.queryByText(message)).toHaveClass("error-box");
    await advance(5000);
    expect(callsTo(fetch, `/job/${jobId}`)).toHaveLength(1);
    expect(screen.getByText(message)).toHaveClass("error-box");
  });

  it("analyze path: stops when the job is cancelled elsewhere", async () => {
    const fetch = installFetch((url) => {
      if (url.startsWith(`${API}/analyze-path/?path=`)) return jsonResponse(200, { job_id: "j1" });
      if (url === `${API}/job/j1`) return jsonResponse(200, {
        status: "cancelled", progress: "0/1", current: "", results: [], error: "cancelled",
        error_kind: null, events: [], kind: "path",
      });
      return unexpectedFetch(url);
    });
    startPath();
    await advance(0);
    await advance(5000);
    expect.soft(callsTo(fetch, "/job/j1")).toHaveLength(1);
    expect(screen.getByText("Cancelled")).toHaveClass("error-box");
  });

  it("analyze path: stops when the job was purged", async () => {
    const fetch = installFetch((url) => {
      if (url.startsWith(`${API}/analyze-path/?path=`)) return jsonResponse(200, { job_id: "j1" });
      if (url === `${API}/job/j1`) return jsonResponse(404, { detail: "Job not found." });
      return unexpectedFetch(url);
    });
    startPath();
    await advance(0);
    await advance(5000);
    expect.soft(callsTo(fetch, "/job/j1")).toHaveLength(1);
    expect(screen.getByText(JOB_GONE_TEXT)).toHaveClass("error-box");
  });

  it("analyze path: still renders Analysis Result after running then done", async () => {
    let polls = 0;
    const fetch = installFetch((url) => {
      if (url.startsWith(`${API}/analyze-path/?path=`)) return jsonResponse(200, { job_id: "j1" });
      if (url === `${API}/job/j1`) return jsonResponse(200, ++polls === 1 ? {
        status: "running", progress: "0/1", current: "clip.mkv", results: [], error: null, events: [],
      } : {
        status: "done", progress: "1/1", current: "", results: [legacyVideo(path)], error: null, events: [],
      });
      return unexpectedFetch(url);
    });
    startPath();
    await advance(0);
    await advance(600);
    await advance(600);
    expect(screen.getByRole("heading", { name: "Analysis Result" })).toBeInTheDocument();
    expect(callsTo(fetch, "/job/j1")).toHaveLength(2);
    await advance(5000);
    expect(callsTo(fetch, "/job/j1")).toHaveLength(2);
  });

  it("analyze path: stops with Analysis failed. on an HTTP 500 poll", async () => {
    const fetch = installFetch((url) => {
      if (url.startsWith(`${API}/analyze-path/?path=`)) return jsonResponse(200, { job_id: "j1" });
      if (url === `${API}/job/j1`) return jsonResponse(500, { detail: "boom" });
      return unexpectedFetch(url);
    });
    startPath();
    await advance(0);
    await advance(5000);
    expect.soft(callsTo(fetch, "/job/j1")).toHaveLength(1);
    expect(screen.getByText("Analysis failed.")).toHaveClass("error-box");
  });

  it("magnet: stops when the job is cancelled elsewhere", async () => {
    const fetch = installFetch((url) => {
      if (url === `${API}/magnet/?fast=true&hdr10plus=false`) return jsonResponse(200, { job_id: "m1" });
      if (url === `${API}/job/m1`) return jsonResponse(200, {
        status: "cancelled", progress: "0/1", current: "", results: [], error: "cancelled",
        error_kind: null, events: [{ msg: "Cancelled.", ts: 1 }],
        magnet_files: [], magnet_torrent: null, kind: "magnet",
      });
      return unexpectedFetch(url);
    });
    startMagnet();
    await advance(0);
    await advance(5000);
    expect.soft(callsTo(fetch, "/job/m1")).toHaveLength(1);
    expect(screen.getByText("Cancelled")).toHaveClass("error-box");
  });

  it("magnet: stops with the gone message when the job was purged", async () => {
    const fetch = installFetch((url) => {
      if (url === `${API}/magnet/?fast=true&hdr10plus=false`) return jsonResponse(200, { job_id: "m1" });
      if (url === `${API}/job/m1`) return jsonResponse(404, { detail: "Job not found." });
      return unexpectedFetch(url);
    });
    startMagnet();
    await advance(0);
    await advance(5000);
    expect.soft(callsTo(fetch, "/job/m1")).toHaveLength(1);
    expect(screen.getByText(JOB_GONE_TEXT)).toHaveClass("error-box");
  });

  it("magnet: user Cancel sends one cancel POST and stops polling", async () => {
    const fetch = installFetch((url, init) => {
      if (url === `${API}/magnet/?fast=true&hdr10plus=false`) return jsonResponse(200, { job_id: "m1" });
      if (url === `${API}/job/m1`) return jsonResponse(200, {
        status: "running", progress: "0/1", current: "", results: [], error: null, events: [],
        magnet_files: [], magnet_torrent: null, kind: "magnet",
      });
      if (url === `${API}/magnet/m1/cancel` && init?.method === "POST") return jsonResponse(200, {});
      return unexpectedFetch(url);
    });
    startMagnet();
    await advance(0);
    expect(callsTo(fetch, "/job/m1")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await advance(800);
    expect(callsTo(fetch, "/magnet/m1/cancel")).toHaveLength(1);
    await advance(5000);
    expect.soft(callsTo(fetch, "/magnet/m1/cancel")).toHaveLength(1);
    expect.soft(callsTo(fetch, "/job/m1")).toHaveLength(1);
    expect(screen.getByText("Cancelled")).toHaveClass("error-box");
  });

  it("compare magnets: stops when the job is cancelled elsewhere", async () => {
    const fetch = installFetch((url) => {
      if (url === `${API}/compare-magnets/?fast=true&hdr10plus=false`) {
        return jsonResponse(200, { job_id: "c1", total: 2 });
      }
      if (url === `${API}/job/c1`) return jsonResponse(200, {
        status: "cancelled", progress: "0/2", error: "cancelled", per_magnet: [], comparison: null, kind: "compare",
      });
      return unexpectedFetch(url);
    });
    startCompare();
    await advance(0);
    await advance(5000);
    expect.soft(callsTo(fetch, "/job/c1")).toHaveLength(1);
    expect(screen.getByText("Cancelled")).toHaveClass("error-box");
  });

  it("compare magnets: user Cancel handles an unreachable cancel route without an unhandled rejection", async () => {
    const fetch = installFetch((url, init) => {
      if (url === `${API}/compare-magnets/?fast=true&hdr10plus=false`) {
        return jsonResponse(200, { job_id: "c1", total: 2 });
      }
      if (url === `${API}/job/c1`) return jsonResponse(200, {
        status: "running", progress: "0/2", error: null, per_magnet: [], comparison: null, kind: "compare",
      });
      if (url === `${API}/compare-magnets/c1/cancel` && init?.method === "POST") {
        throw new TypeError("Failed to fetch");
      }
      return unexpectedFetch(url);
    });
    startCompare();
    await advance(0);
    expect(callsTo(fetch, "/job/c1")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await advance(1000);
    expect(callsTo(fetch, "/compare-magnets/c1/cancel")).toHaveLength(1);
    await advance(5000);
    expect.soft(callsTo(fetch, "/compare-magnets/c1/cancel")).toHaveLength(1);
    expect.soft(callsTo(fetch, "/job/c1")).toHaveLength(1);
    expect(screen.getByText("Cancelled")).toHaveClass("error-box");
  });
});
