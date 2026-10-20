// @ts-expect-error Node APIs are used only by the test emitter, outside browser types.
import { mkdtempSync } from "node:fs";
// @ts-expect-error Node APIs are used only by the test emitter, outside browser types.
import { resolve } from "node:path";
// @ts-expect-error Node APIs are used only by the test emitter, outside browser types.
import { execFileSync } from "node:child_process";
// @ts-expect-error Node APIs are used only by the test emitter, outside browser types.
import process from "node:process";
import { act } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { AppControls } from "./AppControls";
import { parseAppStatus } from "./api";

const scratch = mkdtempSync(resolve(process.env.VIDEOLYZER_DATA_ROOT!, "../status-emitter-"));
const env = { ...process.env, VIDEOSUITE_SETTINGS: resolve(scratch, "settings.json"),
  VIDEOSUITE_CALIBRATION: resolve(scratch, "calibration.json"), VC_CACHE_DIR: resolve(scratch, "cache"),
  VIDEOLYZER_DATA_ROOT: resolve(scratch, "app"), TORCH_HOME: resolve(scratch, "torch"),
  HF_HOME: resolve(scratch, "hf"), CLIP_CACHE_DIR: resolve(scratch, "clip") };
const emitted = execFileSync(resolve(".venv/Scripts/python.exe"), ["-B", "tests/test_app_routes.py"], { env, encoding: "utf8", windowsHide: true });
function actualStatus() { return parseAppStatus(JSON.parse(emitted)); }
beforeEach(() => { vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true); });
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

it("shows the actual legacy census and exact close-tab sentence", () => {
  render(<AppControls status={actualStatus()} onReconnect={vi.fn()} />);
  for (const kind of ["suite", "path", "magnet", "calibrate-build"]) expect(screen.getByText(new RegExp(`${kind}:`))).toBeVisible();
  expect(screen.getByText("You can close this tab. Reopen the shortcut to return. Use Quit to stop the app.")).toBeVisible();
  for (const name of ["Keep running", "Quit when finished", "Stop jobs and quit"]) expect(screen.getByRole("button", { name })).toBeVisible();
});

it.each(["done", "error", "cancelled", "gone"])("reconnects active IDs and stops polling at %s", async outcome => {
  vi.useFakeTimers();
  const status = actualStatus();
  const fetcher = vi.fn(async () => ({ ok: outcome !== "gone", status: outcome === "gone" ? 404 : 200,
    json: async () => ({ status: outcome }) }));
  vi.stubGlobal("fetch", fetcher);
  const reconnect = vi.fn();
  render(<AppControls status={status} onReconnect={reconnect} />);
  fireEvent.click(screen.getByRole("button", { name: "Reconnect" }));
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(reconnect).toHaveBeenCalled();
  expect(fetcher.mock.calls.length).toBe(status.active_jobs.length);
  await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
  expect(fetcher.mock.calls.length).toBe(status.active_jobs.length);
  expect(screen.getAllByText(new RegExp(`— ${outcome}`))).toHaveLength(status.active_jobs.length);
});

it("unmount and browser unload never request cancellation or Quit", async () => {
  vi.useFakeTimers();
  const fetcher = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ status: "running" }) }));
  vi.stubGlobal("fetch", fetcher);
  const { unmount } = render(<AppControls status={actualStatus()} onReconnect={vi.fn()} />);
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  unmount();
  window.dispatchEvent(new Event("beforeunload"));
  window.dispatchEvent(new Event("unload"));
  await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
  expect(fetcher.mock.calls.length).toBe(actualStatus().active_jobs.length);
});

it("keeps the launch token in memory and sends JSON only on explicit controls", async () => {
  const status = actualStatus();
  const calls: [string, RequestInit | undefined][] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push([url, init]);
    return { ok: true, status: 200, json: async () => url.endsWith("bootstrap")
      ? { token: "private-token", session_id: status.session_id }
      : { accepted: true, quit_pending: false, active_jobs: status.active_jobs } };
  }));
  render(<AppControls status={status} onReconnect={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "Keep running" }));
  await act(async () => {});
  const controls = calls.filter(c => c[0].startsWith("/app/"));
  expect(controls.map(c => c[0])).toEqual(["/app/bootstrap", "/app/quit"]);
  expect(controls[1][1]?.body).toBe(JSON.stringify({ mode: "keep" }));
  expect(controls[1][1]?.headers).toMatchObject({ "Content-Type": "application/json", "X-VideoSuite-Token": "private-token" });
  expect(JSON.stringify(localStorage)).not.toContain("private-token");
  expect(JSON.stringify(sessionStorage)).not.toContain("private-token");
});
