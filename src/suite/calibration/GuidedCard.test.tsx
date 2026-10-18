import { act, useState } from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { GuidedCard } from "./GuidedCard";
import { ConditionsBar } from "./ConditionsBar";
import { useCalibration } from "./useCalibration";
import type { CalibrationAnswer, Descriptor, StatusDocument } from "./types";
import { API } from "../config";
import { bodyOf, callsTo, installFetch, jsonResponse, unexpectedFetch } from "../testing/fetchMock";
import { suiteSettings } from "../testing/fixtures";

const descriptor = (test = "T01"): Descriptor => ({ test, player: "builtin_usb", title: "<img src=x> HDR banner", class: "decisive", rule: "hdr_map.hdr10",
  instruction: "Open the TV banner.", question: "Which banner appears?", conditions: [], manual: false, file: "Клип & 50%.mkv",
  outcomes: [{ id: "hdr", text: "HDR banner", value: "hdr10" }, { id: "sdr", text: "SDR banner", value: "sdr" },
    { id: "unsure", text: "Catalogue unsure", value: null }, { id: "skipped", text: "Catalogue skip", value: null }],
  source: { kind: "invented", file: null, verification: null }, prediction: { outcome: "hdr", text: "HDR predicted", evidence: "inferred" } });
const conditions = { tv_software: "v1", audio_output: "tv_speakers", usb_filesystem: "exfat" } as const;
const statusDoc = (): StatusDocument => ({ schema: "videosuite-calibrate-status/1", tool_version: "0.5.0", recovery_required: false,
  kit: { id: "A", out: "D:\\Kit", built_at: "saved time", catalogue_version: 1, descriptors: [descriptor(), descriptor("T02")] },
  session: { ...conditions, confirmed_at: "saved time" }, answers: [], summary: { done: 0, total: 2, matches: 0, differs: 0 } });
it.each(["T14", "T15", "T16", "error", "sound_default_ok", "older"])("does not promote stored %s outcomes to owner evidence", async kind => {
  const doc = statusDoc();
  const d = doc.kit!.descriptors[0];
  const informational = kind.startsWith("T");
  if (informational) d.class = "informational";
  const outcome = kind === "error" || kind === "sound_default_ok" ? kind : "hdr";
  if (outcome !== "hdr") d.outcomes.push({ id: outcome, text: outcome, value: null });
  doc.answers = [{ kit_id: "A", test: d.test, player: d.player, outcome, note: "", conditions,
    at: "saved", older_tv_software: kind === "older", match: "none" }];
  installFetch(url => url === `${API}/suite/calibration/status?kit=A` ? jsonResponse(200, doc) : unexpectedFetch(url));
  render(<Harness />);
  await screen.findByText(/^Saved outcome:/);
  expect(screen.queryByText("evidence: owner")).not.toBeInTheDocument();
  if (kind !== "older") expect(screen.getByRole("status")).toHaveTextContent("stored only, not used for scores");
});

function Harness({ selected = "T01", ready = true }: { selected?: string; ready?: boolean }) {
  const calibration = useCalibration("A");
  return <GuidedCard calibration={calibration} selected={{ test: selected, player: "builtin_usb" }} onSelect={vi.fn()} conditionsConfirmed={ready} />;
}
it.each([["builtin_usb", "Built-in USB player"], ["kodi", "Kodi"], ["just_player", "Just Player"], ["__proto__", "__proto__"]])(
  "shows the friendly player name for %s", async (player, name) => {
    const doc = statusDoc(); doc.kit!.descriptors[0].player = player;
    installFetch(url => url === `${API}/suite/calibration/status?kit=A` ? jsonResponse(200, doc) : unexpectedFetch(url));
    function Card() { const calibration = useCalibration("A"); return <GuidedCard calibration={calibration} selected={{ test: "T01", player }} onSelect={vi.fn()} conditionsConfirmed />; }
    render(<Card />);
    expect(await screen.findByText(`Player: ${name}`)).toBeVisible();
    expect(screen.getByRole("article", { name: `T01 ${name}` })).toBeVisible();
  },
);
it.each([
  ["hdr", "matches", "HDR banner"],
  ["sdr", "differs", "SDR banner"],
] as const)("labels the recorded %s observation with owner evidence beside the outcome", async (outcome, match, text) => {
  const doc = statusDoc();
  doc.answers = [{ kit_id: "A", test: "T01", player: "builtin_usb", outcome, note: "", conditions,
    at: "saved time", older_tv_software: false, match }];
  installFetch(url => url === `${API}/suite/calibration/status?kit=A` ? jsonResponse(200, doc) : unexpectedFetch(url));
  render(<Harness />);
  const saved = await screen.findByText(`Saved outcome: ${text}`);
  expect(within(saved).getByText("evidence: owner")).toHaveAttribute("title", "tested by you");
  expect(screen.getByRole("status")).toHaveTextContent("Recorded.");
  expect(saved).toHaveTextContent("evidence: owner (recorded)");
  expect(screen.getByRole("status")).not.toHaveTextContent(/changes applied|not applied yet/i);
  expect(screen.getByText(/Frozen prediction:/)).toHaveTextContent("evidence: inferred");
});
it.each([["unsure", "Not sure", "unsure"], ["skipped", "Skipped", "none"]] as const)(
  "does not claim owner evidence for a saved %s answer", async (outcome, text, match) => {
    const doc = statusDoc();
    doc.answers = [{ kit_id: "A", test: "T01", player: "builtin_usb", outcome, note: "", conditions,
      at: "saved time", older_tv_software: false, match }];
    installFetch(url => url === `${API}/suite/calibration/status?kit=A` ? jsonResponse(200, doc) : unexpectedFetch(url));
    render(<Harness />);
    expect(await screen.findByText(`Saved outcome: ${text}`)).toBeVisible();
    expect(screen.queryByText("evidence: owner")).not.toBeInTheDocument();
  },
);
it("renders descriptor text safely, accessible unselected buttons, progress, and navigation", async () => {
  const doc = statusDoc(); const select = vi.fn();
  installFetch(url => url === `${API}/suite/calibration/status?kit=A` ? jsonResponse(200, doc) : unexpectedFetch(url));
  function Card() { const calibration = useCalibration("A"); return <GuidedCard calibration={calibration} selected={{ test: "T01", player: "builtin_usb" }} onSelect={select} conditionsConfirmed />; }
  const { container } = render(<Card />);
  expect(await screen.findByText("Play: Клип & 50%.mkv")).toBeVisible();
  expect(screen.getByText("evidence: inferred")).toBeVisible();
  expect(screen.getByText("Open the TV banner.")).toBeVisible();
  expect(screen.getByText("Which banner appears?")).toBeVisible();
  expect(container.querySelector("img")).toBeNull();
  expect(screen.queryByText("Catalogue unsure")).toBeNull();
  expect(screen.getAllByRole("button", { name: "Not sure" })).toHaveLength(1);
  expect(screen.getAllByRole("button", { name: "Skip" })).toHaveLength(1);
  const outcome = screen.getByRole("button", { name: "HDR banner" });
  expect(outcome.tagName).toBe("BUTTON"); expect(outcome).not.toHaveAttribute("aria-pressed", "true");
  outcome.focus(); expect(outcome).toHaveFocus();
  expect(screen.getByRole("progressbar")).toHaveAttribute("value", "0");
  expect(screen.getByRole("button", { name: "Back" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Next" })); expect(select).toHaveBeenCalledWith(doc.kit!.descriptors[1]);
});
it("requires a session and current conditions confirmation before any answer", async () => {
  const doc = statusDoc(); doc.session = null;
  const fetch = installFetch(url => url === `${API}/suite/calibration/status?kit=A` ? jsonResponse(200, doc) : unexpectedFetch(url));
  const view = render(<Harness />);
  expect(await screen.findByRole("button", { name: "HDR banner" })).toBeDisabled();
  view.unmount(); doc.session = { ...conditions, confirmed_at: "saved" };
  render(<Harness ready={false} />);
  expect(await screen.findByRole("button", { name: "HDR banner" })).toBeDisabled();
  expect(callsTo(fetch, "/suite/calibration/record")).toHaveLength(0);
});
it("caps a single-line note, deduplicates clicks, and restores saved answers with immutable conditions on switch and reload", async () => {
  const doc = statusDoc(); let finish!: (r: Response) => void;
  const pending = new Promise<Response>(r => { finish = r; });
  const fetch = installFetch((url, init) => {
    if (url === `${API}/suite/calibration/status?kit=A`) return jsonResponse(200, doc);
    if (url === `${API}/suite/calibration/record`) {
      const body = JSON.parse(String(init?.body));
      const answer: CalibrationAnswer = { ...body, kit_id: "A", conditions, at: "saved time", older_tv_software: false, match: "matches" };
      doc.answers = [answer]; doc.summary = { done: 1, total: 2, matches: 1, differs: 0 };
      return pending;
    }
    return unexpectedFetch(url);
  });
  const view = render(<Harness />); const input = await screen.findByRole("textbox", { name: "Optional note" });
  expect(input).toHaveAttribute("maxlength", "500");
  fireEvent.change(input, { target: { value: "a\u2028" + "b".repeat(600) } });
  expect(screen.getByText("500 / 500 characters")).toBeVisible();
  const button = screen.getByRole("button", { name: "HDR banner" });
  fireEvent.click(button, { detail: 0 }); fireEvent.click(button);
  expect(callsTo(fetch, "/suite/calibration/record")).toHaveLength(1);
  expect(bodyOf(fetch, "/suite/calibration/record")).toMatchObject({ note: "a " + "b".repeat(498) });
  expect(screen.getByText("Saving answer…")).toBeVisible();
  await act(async () => { finish(jsonResponse(200, { schema: "videosuite-calibrate-record/1", tool_version: "0.5.0", answer: doc.answers[0], summary: doc.summary })); });
  expect(screen.getByText("Saved outcome: HDR banner")).toBeVisible();
  expect(screen.getByText("Matches the prediction.")).toBeVisible();
  view.rerender(<Harness selected="T02" />); expect(screen.getByRole("textbox", { name: "Optional note" })).toHaveValue("");
  view.rerender(<Harness />); expect(screen.getByRole("textbox", { name: "Optional note" })).toHaveValue(doc.answers[0].note);
  view.unmount(); render(<Harness />);
  expect(await screen.findByText("Saved outcome: HDR banner")).toBeVisible();
  expect(screen.getByText(/Recorded conditions:.*v1.*tv_speakers.*exfat/)).toBeVisible();
  expect(screen.getByRole("textbox", { name: "Optional note" })).toHaveValue(doc.answers[0].note);
});
it("uses manual instruction, supports fixed outcomes, and keeps the saved answer on failure", async () => {
  const doc = statusDoc(); doc.kit!.descriptors[0] = { ...descriptor(), manual: true, file: null, instruction: "Play your own file over 4 GB." };
  doc.answers = [{ kit_id: "A", test: "T01", player: "builtin_usb", outcome: "hdr", note: "previous note", conditions,
    at: "saved time", older_tv_software: true, match: "matches" }];
  doc.summary = { done: 1, total: 2, matches: 1, differs: 0 };
  const fetch = installFetch(url => url === `${API}/suite/calibration/status?kit=A` ? jsonResponse(200, doc)
    : url === `${API}/suite/calibration/record` ? jsonResponse(500, { detail: "save failed" }) : unexpectedFetch(url));
  render(<Harness />);
  expect(await screen.findByText("Play your own file over 4 GB.")).toBeVisible();
  expect(screen.queryByText(/^Play:/)).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Not sure" }));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("save failed"));
  expect(bodyOf(fetch, "/suite/calibration/record")).toMatchObject({ outcome: "unsure" });
  fireEvent.click(screen.getByRole("button", { name: "Skip" }));
  await waitFor(() => expect(callsTo(fetch, "/suite/calibration/record")).toHaveLength(2));
  expect(bodyOf(fetch, "/suite/calibration/record", 1)).toMatchObject({ outcome: "skipped" });
  expect(screen.getByText("Saved outcome: HDR banner")).toBeVisible();
  expect(screen.getByText(/Older TV software:/)).toBeVisible();
  expect(screen.getByRole("textbox", { name: "Optional note" })).toHaveValue("previous note");
});
it("connects conditions readiness to the card and keeps earlier answers' conditions after a new session", async () => {
  const doc = statusDoc(); doc.session = null;
  const settings = suiteSettings(); settings.devices.sony_bravia_8_ii = { label: "BRAVIA", ...conditions, overrides: [] };
  const fetch = installFetch((url, init) => {
    if (url === `${API}/suite/calibration/status?kit=A`) return jsonResponse(200, doc);
    if (url !== `${API}/suite/calibration/session` && url !== `${API}/suite/calibration/record`) return unexpectedFetch(url);
    const body = JSON.parse(String(init?.body));
    if (url === `${API}/suite/calibration/session`) {
      doc.session = { tv_software: body.tv_software, audio_output: body.audio_output, usb_filesystem: body.usb_filesystem, confirmed_at: `saved ${body.tv_software}` };
      return jsonResponse(200, { schema: "videosuite-calibrate-session/1", tool_version: "0.5.0", kit_id: "A", session: doc.session });
    }
    if (url === `${API}/suite/calibration/record`) {
      doc.answers = [{ ...body, kit_id: "A", conditions: { ...doc.session! }, at: "saved time", older_tv_software: false, match: "differs" }];
      doc.summary = { done: 1, total: 2, matches: 0, differs: 1 };
      return jsonResponse(200, { schema: "videosuite-calibrate-record/1", tool_version: "0.5.0", answer: doc.answers[0], summary: doc.summary });
    }
    return unexpectedFetch(url);
  });
  function Screen() {
    const calibration = useCalibration("A"); const [ready, setReady] = useState(false);
    return <><ConditionsBar calibration={calibration} settings={settings} onReadyChange={setReady} />
      <GuidedCard calibration={calibration} selected={{ test: "T01", player: "builtin_usb" }} onSelect={vi.fn()} conditionsConfirmed={ready} /></>;
  }
  render(<Screen />);
  expect(await screen.findByRole("button", { name: "SDR banner" })).toBeDisabled();
  fireEvent.change(screen.getByRole("textbox", { name: "TV software" }), { target: { value: "v1" } });
  fireEvent.click(screen.getByRole("checkbox")); fireEvent.click(screen.getByRole("button", { name: "Confirm conditions" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "SDR banner" })).toBeEnabled());
  fireEvent.change(screen.getByRole("textbox", { name: "Optional note" }), { target: { value: "observed SDR" } });
  fireEvent.click(screen.getByRole("button", { name: "SDR banner" }));
  expect(await screen.findByText("Saved outcome: SDR banner")).toBeVisible();
  expect(screen.getByText("Differs from the prediction.")).toBeVisible();
  fireEvent.change(screen.getByRole("textbox", { name: "TV software" }), { target: { value: "v2" } });
  expect(screen.getByRole("button", { name: "SDR banner" })).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox")); fireEvent.click(screen.getByRole("button", { name: "Start new session" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "SDR banner" })).toBeEnabled());
  expect(screen.getByText(/Recorded conditions:.*v1.*tv_speakers.*exfat/)).toBeVisible();
  expect(screen.getByRole("textbox", { name: "Optional note" })).toHaveValue("observed SDR");
  expect(callsTo(fetch, "/suite/calibration/session")).toHaveLength(2);
  expect(callsTo(fetch, "/suite/calibration/record")).toHaveLength(1);
  expect(screen.getByText("HDR predicted", { exact: false })).toHaveTextContent("evidence: inferred");
});
