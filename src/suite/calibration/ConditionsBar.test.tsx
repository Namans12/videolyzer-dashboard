import { act } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { ConditionsBar } from "./ConditionsBar";
import { useCalibration } from "./useCalibration";
import type { CalibrationSession, StatusDocument } from "./types";
import type { SuiteSettings } from "../types";
import { suiteSettings } from "../testing/fixtures";
import { API } from "../config";
import { bodyOf, callsTo, installFetch, jsonResponse, unexpectedFetch } from "../testing/fetchMock";

function settings(): SuiteSettings { const s = suiteSettings(); s.devices.sony_bravia_8_ii = { label: "BRAVIA", audio_output: "avr", usb_filesystem: "exfat", overrides: [] }; return s; }
const session: CalibrationSession = { tv_software: "v1", audio_output: "avr", usb_filesystem: "exfat", confirmed_at: "saved time" };
function statusDoc(): StatusDocument { return { schema: "videosuite-calibrate-status/1", tool_version: "0.5.0", recovery_required: false,
  kit: { id: "A", out: "D:\\Kit", built_at: "saved time", catalogue_version: 1, descriptors: [] }, session: null, answers: [], summary: { done: 0, total: 0, matches: 0, differs: 0 } }; }
function Harness({ value = settings(), onReady = vi.fn() }: { value?: SuiteSettings | null; onReady?: (ready: boolean) => void }) {
  const calibration = useCalibration("A");
  return <ConditionsBar calibration={calibration} settings={value} onReadyChange={onReady} />;
}
it.each(["missing", "audio", "filesystem", "no path"])("explains how to fix %s settings with a copyable text example", async kind => {
  const value = settings(); value.path = "D:\\Owner <img src=x>\\settings.json";
  if (kind === "missing") value.loaded = false;
  if (kind === "audio") value.devices.sony_bravia_8_ii.audio_output = null;
  if (kind === "filesystem") value.devices.sony_bravia_8_ii.usb_filesystem = null;
  if (kind === "no path") { value.path = null; value.devices.sony_bravia_8_ii.audio_output = null; }
  installFetch(url => url === `${API}/suite/calibration/status?kit=A` ? jsonResponse(200, statusDoc()) : unexpectedFetch(url));
  const { container } = render(<Harness value={value} />);
  await screen.findByRole("textbox", { name: "TV software" });
  expect(screen.getByText(kind === "no path" ? /%LOCALAPPDATA%\\VideoComparator\\suite\\settings.json/ : /D:\\Owner <img src=x>\\settings.json/)).toBeVisible();
  expect(container.querySelector("pre")?.textContent).toBe(JSON.stringify({ schema: "videosuite-settings/1", devices: { sony_bravia_8_ii: { usb_filesystem: "exfat", audio_output: "tv_speakers" } } }, null, 2));
  expect(screen.getByText(/audio output: tv_speakers \| soundbar \| avr/i)).toHaveTextContent("USB file system: exfat | fat32 | ntfs");
  expect(screen.getByText(/save the file, then press Check settings again/)).toBeVisible();
  expect(container.querySelector("img")).toBeNull();
});
it("shows settings and posts one explicitly confirmed session with TV software capped to 100", async () => {
  const doc = statusDoc(); let finish!: (r: Response) => void;
  const pending = new Promise<Response>(r => { finish = r; }); const ready = vi.fn();
  const fetch = installFetch((url, init) => {
    if (url === `${API}/suite/calibration/status?kit=A`) return jsonResponse(200, doc);
    if (url === `${API}/suite/calibration/session`) { doc.session = { ...JSON.parse(String(init?.body)), confirmed_at: "saved time" }; return pending; }
    return unexpectedFetch(url);
  });
  render(<Harness onReady={ready} />);
  await waitFor(() => expect(screen.getByRole("textbox", { name: "TV software" })).toBeEnabled());
  const input = screen.getByRole("textbox", { name: "TV software" });
  expect(screen.getByText(/Audio output: AV receiver/)).toBeVisible();
  expect(screen.getByText(/USB file system: exFAT/)).toBeVisible();
  expect(input).toHaveAttribute("maxlength", "100");
  fireEvent.change(input, { target: { value: "v".repeat(120) } });
  expect(screen.getByText("100 / 100 characters")).toBeVisible();
  expect(screen.getByRole("button", { name: "Confirm conditions" })).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox"));
  const button = screen.getByRole("button", { name: "Confirm conditions" }); button.focus(); expect(button).toHaveFocus();
  fireEvent.click(button, { detail: 0 }); fireEvent.click(button);
  expect(callsTo(fetch, "/suite/calibration/session")).toHaveLength(1);
  expect(bodyOf(fetch, "/suite/calibration/session")).toEqual({ out: "D:\\Kit", tv_software: "v".repeat(100), audio_output: "avr", usb_filesystem: "exfat" });
  await act(async () => { finish(jsonResponse(200, { schema: "videosuite-calibrate-session/1", tool_version: "0.5.0", kit_id: "A", session: doc.session })); });
  await waitFor(() => expect(ready).toHaveBeenLastCalledWith(true));
});
it("restores confirmed conditions, invalidates readiness on edits/settings changes, and requires the new-session sentence", async () => {
  const doc = statusDoc(); doc.session = session; const ready = vi.fn();
  const fetch = installFetch((url, init) => {
    if (url === `${API}/suite/calibration/status?kit=A`) return jsonResponse(200, doc);
    if (url === `${API}/suite/calibration/session`) { doc.session = { ...JSON.parse(String(init?.body)), confirmed_at: "new saved time" }; return jsonResponse(200, { schema: "videosuite-calibrate-session/1", tool_version: "0.5.0", kit_id: "A", session: doc.session }); }
    return unexpectedFetch(url);
  });
  const view = render(<Harness onReady={ready} />);
  await waitFor(() => expect(ready).toHaveBeenLastCalledWith(true));
  expect(screen.getByRole("textbox", { name: "TV software" })).toHaveValue("v1");
  fireEvent.change(screen.getByRole("textbox", { name: "TV software" }), { target: { value: "v2" } });
  await waitFor(() => expect(ready).toHaveBeenLastCalledWith(false));
  expect(screen.getByText(/Changing conditions starts a new session/)).toBeVisible();
  expect(screen.getByRole("button", { name: "Start new session" })).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox")); fireEvent.click(screen.getByRole("button", { name: "Start new session" }));
  await waitFor(() => expect(ready).toHaveBeenLastCalledWith(true));
  const changed = settings(); changed.devices.sony_bravia_8_ii.audio_output = "soundbar";
  view.rerender(<Harness onReady={ready} value={changed} />);
  await waitFor(() => expect(ready).toHaveBeenLastCalledWith(false));
  expect(screen.getByRole("button", { name: "Start new session" })).toBeDisabled();
  expect(callsTo(fetch, "/suite/calibration/session")).toHaveLength(1);
  view.unmount(); render(<Harness onReady={ready} />);
  await waitFor(() => expect(ready).toHaveBeenLastCalledWith(true));
  expect(screen.getByRole("textbox", { name: "TV software" })).toHaveValue("v2");
});
it("blocks missing/unsafe settings and exposes session failure without marking conditions ready", async () => {
  const doc = statusDoc(); const ready = vi.fn();
  const fetch = installFetch(url => url === `${API}/suite/calibration/status?kit=A` ? jsonResponse(200, doc)
    : url === `${API}/suite/calibration/session` ? jsonResponse(500, { detail: "session failed" }) : unexpectedFetch(url));
  const view = render(<Harness value={null} onReady={ready} />);
  await screen.findByRole("textbox", { name: "TV software" });
  expect(screen.getByRole("button", { name: "Confirm conditions" })).toBeDisabled();
  const unsafe = settings(); unsafe.devices = Object.create({ sony_bravia_8_ii: unsafe.devices.sony_bravia_8_ii });
  view.rerender(<Harness value={unsafe} onReady={ready} />);
  expect(screen.getByRole("button", { name: "Confirm conditions" })).toBeDisabled();
  view.rerender(<Harness onReady={ready} />);
  fireEvent.change(screen.getByRole("textbox", { name: "TV software" }), { target: { value: "v1" } });
  fireEvent.click(screen.getByRole("checkbox")); fireEvent.click(screen.getByRole("button", { name: "Confirm conditions" }));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("session failed"));
  expect(ready).toHaveBeenLastCalledWith(false);
  expect(callsTo(fetch, "/suite/calibration/session")).toHaveLength(1);
});
