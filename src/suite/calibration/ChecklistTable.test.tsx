import { fireEvent, render, screen, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { ChecklistTable } from "./ChecklistTable";
import type { CalibrationAnswer, Descriptor } from "./types";

function descriptor(player = "builtin_usb", test = "T01"): Descriptor {
  return { test, player, title: "<img src=x> Banner", class: "decisive", rule: "hdr_map.hdr10", instruction: "Play", question: "Banner?",
    outcomes: [], conditions: [], manual: test === "T16", file: null, source: { kind: "manual", file: null, verification: null },
    prediction: { outcome: "yes", text: "HDR predicted", evidence: "inferred" } };
}
it("groups built-in first, selects a descriptor, and pairs every frozen prediction with evidence", () => {
  const select = vi.fn();
  const descriptors = [descriptor("kodi"), descriptor(), descriptor("builtin_usb", "T16"), descriptor("__proto__"), descriptor("just_player"), descriptor("constructor")];
  const { container } = render(<ChecklistTable descriptors={descriptors} answers={[]} onSelect={select} selected={{ test: "T01", player: "builtin_usb" }} />);
  expect(screen.getAllByRole("heading", { level: 4 }).map(h => h.textContent)).toEqual(["Built-in USB player", "Kodi", "__proto__", "Just Player", "constructor"]);
  screen.getAllByText("HDR predicted").forEach(p => expect(within(p.closest("td")!).getByText("evidence: inferred")).toBeVisible());
  fireEvent.click(screen.getAllByRole("button", { name: /T01/ })[0]);
  expect(select).toHaveBeenCalledWith(descriptors[1]);
  expect(screen.getByText("manual")).toBeVisible();
  expect(container.querySelector("img")).toBeNull();
  expect(screen.getByRole("progressbar")).toHaveAttribute("value", "0");
});
it("shows saved answer states including older software without changing descriptor predictions", () => {
  const descriptors = ["T01", "T02", "T03", "T04", "T05"].map(t => descriptor("builtin_usb", t));
  const answers: CalibrationAnswer[] = ["matches", "differs", "unsure", "none"].map((match, i) => ({
    kit_id: "kit", test: descriptors[i].test, player: "builtin_usb", outcome: i === 2 ? "unsure" : "yes", note: "", at: "saved time",
    conditions: { audio_output: null, usb_filesystem: null, tv_software: "v1" }, older_tv_software: i === 3, match: match as CalibrationAnswer["match"],
  }));
  render(<ChecklistTable descriptors={descriptors} answers={answers} onSelect={vi.fn()} />);
  for (const text of ["matches", "differs", "unsure", "older TV software", "not done"]) expect(screen.getByText(text)).toBeVisible();
  expect(screen.getByRole("progressbar")).toHaveAttribute("value", "4");
  expect(screen.getByText("4 of 5 answered")).toBeVisible();
});

it.each([["yes", "answered"], ["skipped", "skipped"]])("labels %s against an unknown prediction as %s", (outcome, status) => {
  const d = descriptor(); d.prediction = { outcome: null, text: "Unknown", evidence: "unknown" };
  const answer: CalibrationAnswer = { kit_id: "kit", test: d.test, player: d.player, outcome, note: "", at: "saved",
    conditions: { audio_output: null, usb_filesystem: null, tv_software: "v1" }, older_tv_software: false, match: "none" };
  render(<ChecklistTable descriptors={[d]} answers={[answer]} onSelect={vi.fn()} />);
  expect(screen.getByText(status)).toBeVisible();
  expect(screen.queryByText("unsure")).not.toBeInTheDocument();
});
