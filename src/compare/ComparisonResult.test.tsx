import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ComparisonResult } from "./ComparisonResult";
import { asRunDetail } from "./types";
import type { RunResult } from "./types";
import {
  changedSettings, failedPairs, localFullBoth, localJustPlayerAlternatives, localQuickBoth, magnetPartialFailure,
  magnetQuickDevice, magnetQuickOverall,
} from "./testing/fixtures";
import type { Json } from "./testing/fixtures";
import { devicesDoc } from "../suite/testing/fixtures";
import { EVIDENCE_WORDS } from "../suite/types";
import { unverifiedNotice } from "../suite/verdict";

const resultOf = (detail: unknown): RunResult => asRunDetail(detail).result!;
const obj = (v: unknown) => v as Json;
const JP = "Just Player on the TV (ExoPlayer based)";
const TV = "Sony Bravia 8 II (TV)";

function renderResult(detail: unknown) {
  return render(<ComparisonResult result={resultOf(detail)} devices={devicesDoc()} />);
}
const region = (name: string | RegExp) => screen.getByRole("region", { name });

describe("ComparisonResult", () => {
  it("labels Quick estimates and never renders measured scores", () => {
    const { container } = renderResult(localQuickBoth());
    const overall = region("Best overall");
    expect(overall).toHaveTextContent("Metadata estimate — picture not measured");
    const device = region("Best for my device");
    expect(device).toHaveTextContent(`Best playback estimate for ${TV} / ${JP} — picture not measured`);
    const answer = region("Answer");
    expect(answer).toHaveTextContent("Best overall (metadata estimate — picture not measured): A.mkv");
    expect(answer).toHaveTextContent(`Best playback estimate for ${TV} / ${JP} — picture not measured: A.mkv`);
    for (const header of screen.getAllByRole("columnheader")) expect(header.textContent).not.toMatch(/quality/i);
    expect(container.textContent).not.toMatch(/quality_score|device_score|measured picture/);
    // the nested suite settings path is never shown (it is a backend run folder)
    expect(container.textContent).not.toContain("settings.json");
    expect(container.textContent).not.toContain("<SCRATCH>");
    // ties are ties, never an alphabetical win
    expect(overall).toHaveTextContent("tie");
  });

  it("renders only requested Full answers without hiding missing coverage", () => {
    const deviceOnly = renderResult(magnetQuickDevice());
    expect(screen.queryByRole("region", { name: "Best overall" })).toBeNull();
    expect(region("Best for my device")).toBeInTheDocument();
    expect(region("Answer")).toHaveTextContent("No whole-set winner");
    deviceOnly.unmount();

    const overallOnly = renderResult(magnetQuickOverall());
    expect(screen.queryByRole("region", { name: "Best for my device" })).toBeNull();
    expect(region("Best overall")).toBeInTheDocument();
    overallOnly.unmount();

    const full = renderResult(localFullBoth());
    expect(region("Best overall")).toHaveTextContent("Picture quality (device-neutral)");
    expect(region("Answer")).toHaveTextContent("Best overall (measured picture): A.mkv");
    expect(region("Answer")).toHaveTextContent(`Best on ${TV} with ${JP} (measured picture and playback): A.mkv`);
    // the device panel carries no second neutral table: Overall is its own panel
    expect(within(region("Best for my device")).queryByText("Picture quality (device-neutral)")).toBeNull();
    full.unmount();

    // Full Device-only, derived from the real Full Both envelope by changing only the requested results choice
    const derived = localFullBoth();
    for (const r of [obj(derived.run).request, obj(obj(derived.result).request), obj(obj(obj(derived.result).snapshot).request)]) {
      obj(r).results = "device";
    }
    obj(derived.result).overall = null;
    const deviceFull = renderResult(derived);
    expect(screen.queryByRole("region", { name: "Best overall" })).toBeNull();
    expect(screen.queryByText("Picture quality (device-neutral)")).toBeNull();
    deviceFull.unmount();

    const failed = renderResult(failedPairs().run);
    const answer = region("Answer");
    expect(answer).toHaveTextContent("No whole-set winner");
    expect(answer).not.toHaveTextContent("Best overall (measured picture)");
    expect(region("Coverage")).toHaveTextContent("2 of 3 pairs were compared");
    expect(region("Coverage")).toHaveTextContent("1 comparison pair(s) failed");
    failed.unmount();

    const partial = renderResult(magnetPartialFailure());
    expect(region("Coverage")).toHaveTextContent("1 of 2 versions could be read");
    expect(region("Coverage")).toHaveTextContent("Could not read partial facts");
    expect(region("Answer")).toHaveTextContent("No whole-set winner");
    partial.unmount();

    renderResult(changedSettings().run);
    expect(region("Coverage")).toHaveTextContent("settings or the device rules changed during the run");
    expect(region("Answer")).toHaveTextContent("Run it again");
  });

  it("keeps selected player and every device recommendation evidence word after reconnect", () => {
    const { container } = renderResult(localJustPlayerAlternatives());
    const device = region("Best for my device");
    const sections = within(device).getAllByRole("region");
    expect(sections[0]).toHaveAccessibleName(`Your player: ${JP}`);
    expect(sections.slice(1).map((s) => s.getAttribute("aria-label"))).toEqual([
      "Alternative player: Sony USB media player (built-in)", "Alternative player: Kodi on the TV (Dolby Vision compatibility mode on)",
    ]);
    const verdicts = container.querySelectorAll(".suite-verdict");
    expect(verdicts.length).toBeGreaterThan(0);
    expect(container.querySelectorAll(".suite-evidence")).toHaveLength(verdicts.length);
    for (const e of container.querySelectorAll(".suite-evidence")) {
      expect(EVIDENCE_WORDS).toContain(String(e.textContent).replace("evidence: ", ""));
    }
    expect(region("Answer").querySelector(".suite-evidence")).not.toBeNull();

    // a missing evidence word is shown as unknown with the unverified notice, never dropped
    const detail = localJustPlayerAlternatives();
    const doc = obj(obj(detail.result).device);
    const order = obj(obj(doc.devices)["sony_bravia_8_ii/just_player"]);
    delete obj(obj(Object.values(obj((order.groups as Json[])[0].basis))[0]).playback).evidence;
    obj(detail.result).device = doc;
    const r = (detail.result as unknown) as RunResult;   // the guard would refuse it; the view must still be honest
    const view = render(<ComparisonResult result={r} devices={devicesDoc()} />);
    expect(within(view.container).getAllByText("evidence: unknown").length).toBeGreaterThan(0);
    expect(within(view.container).getAllByText(unverifiedNotice("unknown")!).length).toBeGreaterThan(0);
  });

  it("keeps the Full primary player first and labels the measured alternatives", () => {
    renderResult(localFullBoth());
    const device = region("Best for my device");
    const sections = within(device).getAllByRole("region");
    expect(sections[0]).toHaveAccessibleName(new RegExp(`^Your player: .*${JP.replace(/[()]/g, ".")}`));
    expect(sections.slice(1).every((s) => /^Alternative player: /.test(s.getAttribute("aria-label") ?? ""))).toBe(true);
  });

  it("retains same-picture conflicts stale legacy reports device basis removed credit and ranges", () => {
    const detail = localFullBoth();
    const overall = obj(obj(detail.result).overall);
    overall.same_picture_groups = [["A.mkv", "B.mkv"], ["C.mkv"]];
    overall.head_to_head_conflicts = [{ winner: "C.mkv", ranked_above: "B.mkv" }];
    overall.stale_reports = [{ name1: "A.mkv", name2: "C.mkv", reason: "engine changed" }];
    overall.legacy_reports = [{ name1: "B.mkv", name2: "C.mkv" }];
    const device = obj(obj(detail.result).device);
    const jp = obj(obj(device.devices)["sony_bravia_8_ii/just_player"]);
    jp.quality_basis = "device";
    jp.format_credit_removed = [{ a: "A.mkv", b: "B.mkv", rule: "DYNAMIC_RANGE", shown_as: "hdr10", measured: [60, 70], winner: "B.mkv" }];
    renderResult(detail);
    const o = region("Best overall");
    expect(o).toHaveTextContent("Same picture: A.mkv = B.mkv");
    expect(o).toHaveTextContent("Note: C.mkv beat B.mkv head to head but ranks below it overall");
    expect(o).toHaveTextContent("Ignored 1 stale report(s)");
    expect(o).toHaveTextContent("A.mkv vs C.mkv (engine changed)");
    expect(o).toHaveTextContent("1 older report(s) could not be checked against the current files: B.mkv vs C.mkv");
    expect(region("Answer")).toHaveTextContent("Same picture: A.mkv = B.mkv");
    const d = region("Best for my device");
    expect(d).toHaveTextContent("Quality basis: adjusted for this player (format credit it cannot show was removed).");
    expect(d).toHaveTextContent("Format credit not counted on this player: A.mkv vs B.mkv");
    expect(d).toHaveTextContent("58 (range 43-73)");
    expect(d).toHaveTextContent("Crossing point:");
  });

  it("shows provenance without paths and an honest missing build identity", () => {
    renderResult(localFullBoth());
    const details = screen.getByText("How this result was made").closest("details")!;
    expect(details).toHaveTextContent("VideoSuite 0.7.0");
    expect(details).toHaveTextContent("562ab353");
    expect(details).toHaveTextContent("sony_bravia_8_ii: 3");
    expect(details).toHaveTextContent("App build: not recorded");
    expect(details.textContent).not.toMatch(/[A-Za-z]:\\/);
  });

  it("renders hostile backend text literally", () => {
    const detail = localQuickBoth();
    const facts = obj(detail.result).facts as Json[];
    obj(facts[0].source).path = "C:\\plan4b\\<img src=x onerror=alert(1)>.mkv";
    const failures = obj(detail.result).failures as Json[];
    failures.push({ candidate_id: null, stage: "report", kind: "x", message: "<script>alert(1)</script>" });
    const { container } = render(<ComparisonResult result={(detail.result as unknown) as RunResult} devices={devicesDoc()} />);
    expect(container.querySelector("img, script")).toBeNull();
    expect(container.textContent).toContain("<img src=x onerror=alert(1)>.mkv");
    expect(container.textContent).toContain("<script>alert(1)</script>");
  });
});
