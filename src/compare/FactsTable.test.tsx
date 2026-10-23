import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { FactsTable } from "./FactsTable";
import { asRunDetail } from "./types";
import { localFullBoth, magnetQuickOverall } from "./testing/fixtures";
import type { ScoreRecord } from "../suite/types";

const factsOf = (detail: unknown) => asRunDetail(detail).result!.facts as unknown as ScoreRecord[];

describe("FactsTable", () => {
  it("puts the frozen facts of every version side by side", () => {
    render(<FactsTable records={factsOf(localFullBoth())} />);
    const table = screen.getByRole("table", { name: "File facts side by side" });
    const headers = within(table).getAllByRole("columnheader").map((h) => h.textContent);
    expect(headers).toEqual(["Fact", "A.mkv", "B.mkv", "C.mkv"]);
    const row = (name: string) => within(table).getByRole("rowheader", { name }).closest("tr")!;
    expect(row("Resolution")).toHaveTextContent("1920 × 1080");
    expect(row("Video")).toHaveTextContent("h264");
    expect(row("Video bitrate")).toHaveTextContent("10000 kb/s");
    expect(row("HDR")).toHaveTextContent("SDR");
    expect(row("Audio tracks")).toHaveTextContent("none found");
    expect(row("File size")).toHaveTextContent("4096 bytes");
    expect(row("How much was read")).toHaveTextContent("whole file");
    for (const header of within(table).getAllByRole("columnheader")) expect(header.textContent).not.toMatch(/quality|score/i);
  });

  it("marks partial magnet facts and unknown values instead of guessing", () => {
    render(<FactsTable records={factsOf(magnetQuickOverall())} />);
    const table = screen.getByRole("table", { name: "File facts side by side" });
    const read = within(table).getByRole("rowheader", { name: "How much was read" }).closest("tr")!;
    expect(read).toHaveTextContent("only part of the file (head)");
    expect(screen.getAllByText("only the start of the file was downloaded and probed").length).toBeGreaterThan(0);
    expect(within(table).getByRole("rowheader", { name: "HDR" }).closest("tr")).toHaveTextContent("unknown");
  });

  it("shows a plain sentence when there are no facts", () => {
    render(<FactsTable records={[]} />);
    expect(screen.getByText("No file facts were recorded for this run.")).toBeInTheDocument();
  });
});
