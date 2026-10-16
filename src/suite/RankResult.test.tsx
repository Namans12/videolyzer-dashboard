import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { RankResult } from "./RankResult";
import { devicesDoc, rankDoc } from "./testing/fixtures";
import { formatCreditText, unverifiedNotice } from "./verdict";

describe("rank result", () => {
  it.each([undefined, null, "unexpected"])("handles non-array will-not-play reasons: %s", reasons => {
    const doc = rankDoc();
    const d = doc.devices["sony_bravia_8_ii/builtin_usb"];
    d.will_not_play = [{ ...d.ranking[0], id: "missing-reasons", label: "Missing.mkv", verdict: "WONT_PLAY", reasons: reasons as unknown as string[] }];
    render(<RankResult doc={doc} devices={null} />);
    expect(screen.getByText(/Will not play: Missing.mkv:/)).toHaveTextContent("Will not play: Missing.mkv: see the issues list");
  });
  it("shows fixture quality order, seven device sections and evidence for every verdict", () => {
    const doc = rankDoc(); const { container } = render(<RankResult doc={doc} devices={devicesDoc()} />);
    const tables = screen.getAllByRole("table");
    expect(within(tables[0]).getAllByRole("row").slice(1).map(r => r.children[1].textContent)).toEqual(["clip_a_crf18.mkv", "clip_c_crf26.mp4", "clip_b_crf34.mkv"]);
    expect(container.querySelectorAll(".suite-rank")).toHaveLength(7);
    const name = devicesDoc().devices.find(d => d.id === "sony_bravia_8_ii")!.label + " - Sony USB media player (built-in)";
    const sony = screen.getByRole("region", { name });
    expect(sony).toHaveTextContent("Quality basis: device-neutral.");
    expect(sony).toHaveTextContent(`Crossing point: ${doc.devices["sony_bravia_8_ii/builtin_usb"].crossing_note}`);
    for (const section of container.querySelectorAll(".suite-rank")) expect(section.querySelectorAll(".suite-verdict").length).toBe(section.querySelectorAll(".suite-evidence").length);
    const kodi = Array.from(container.querySelectorAll(".suite-rank")).find(s => s.textContent?.includes("Dolby Vision compatibility mode on"))!;
    expect(kodi.querySelectorAll('[role="note"]')).toHaveLength(doc.devices["sony_bravia_8_ii/kodi"].ranking.length);
    expect(kodi.querySelector('[role="note"]')).toHaveTextContent(unverifiedNotice("inferred")!);
    expect(kodi.querySelector("details")).toBeNull();
  });
  it("renders adjustments, missing/stale reports, unique why, null scores, and unknown evidence", () => {
    const doc = rankDoc(); const d = doc.devices["sony_bravia_8_ii/builtin_usb"];
    d.ranking[0].why = "same reason"; d.ranking[1].why = "same reason";
    d.will_not_play = [{ ...d.ranking[0], id: "x", label: "X.mkv", verdict: "WONT_PLAY", evidence: null, reasons: ["silent"] }];
    d.not_compared = [{ ...d.ranking[0], id: "nc", label: "No data.mkv", evidence: "inferred" }];
    d.quality_basis = "device"; d.format_credit_removed = [{ a: "A.mkv", b: "B.mkv", rule: "DYNAMIC_RANGE", shown_as: "hdr10", measured: [60, 70], winner: "B.mkv" }];
    doc.head_to_head_conflicts = [{ winner: "A.mkv", ranked_above: "B.mkv" }];
    doc.stale_reports = [{ name1: "A.mkv", name2: "B.mkv", reason: "engine changed" }]; doc.missing_pairs = [["A", "C"]];
    doc.quality[0].quality_score = null; doc.same_picture_groups = [["A", "B"]];
    render(<RankResult doc={doc} devices={null} />);
    expect(screen.getAllByText("Why: same reason")).toHaveLength(1);
    expect(screen.getByText(formatCreditText(d.format_credit_removed[0]))).toBeVisible();
    expect(screen.getByText("Quality basis: adjusted for this player (format credit it cannot show was removed).")).toBeVisible();
    expect(screen.getByText("Note: A.mkv beat B.mkv head to head but ranks below it overall (quality scores are averages over all pairs of this set).")).toBeVisible();
    expect(screen.getByText("Ignored 1 stale report(s) (made from files that have changed since, or by an older comparison engine): A.mkv vs B.mkv (engine changed)")).toBeVisible();
    expect(screen.getByText("1 pair(s) have no comparison report yet.")).toBeVisible(); expect(screen.getByText("Same picture: A = B")).toBeVisible();
    const block = screen.getByRole("region", { name: "sony_bravia_8_ii/builtin_usb" });
    expect(block).toHaveTextContent("Will not play: X.mkv: silent"); expect(within(block).getByText("evidence: unknown")).toBeVisible();
    expect(within(block).getByText(unverifiedNotice(null)!)).toBeVisible(); expect(block).toHaveTextContent("Not compared (no picture data): No data.mkv");
    expect(screen.getByText("not compared")).toBeVisible();
  });
  it("renders hostile labels literally and unknown device keys", () => {
    const doc = rankDoc(); doc.quality[0].label = "<img src=x onerror=alert(1)>.mkv";
    const { container } = render(<RankResult doc={doc} devices={null} />);
    expect(screen.getByText(doc.quality[0].label)).toBeVisible(); expect(container.querySelector("img")).toBeNull();
    for (const key of Object.keys(doc.devices)) expect(screen.getByRole("region", { name: key })).toBeVisible();
  });
});
