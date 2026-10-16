import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MagnetSuiteBlock } from "./MagnetSuiteBlock";
import { scorePartialDoc } from "./testing/fixtures";
import { PARTIAL_TEXT, unverifiedNotice } from "./verdict";

describe("magnet suite result", () => {
  it("shows partial caveats and evidence per recommendation", () => {
    const record = scorePartialDoc().records[0];
    const { container } = render(<MagnetSuiteBlock suite={{ status: "ok", tool_version: "0.4.0", partial: true, record, best_player: record.best_player }} />);
    expect(screen.getByText(PARTIAL_TEXT)).toBeVisible(); for (const caveat of record.source.caveats!) expect(screen.getByText(caveat)).toBeVisible();
    expect(container.querySelectorAll(".suite-verdict")).toHaveLength(3);
    const sony = screen.getByText(/Sony BRAVIA 8 II:/); expect(sony).toHaveTextContent("Sony USB media player (built-in)"); expect(sony).toHaveTextContent("Plays with a loss evidence: official"); expect(sony.querySelector('[role="note"]')).toBeNull();
    const laptop = screen.getByText(/Vivobook Pro 16X:/); const notice = laptop.querySelector('[role="note"]')!;
    expect(notice).toHaveTextContent(unverifiedNotice("inferred")!); expect(notice).toBeVisible(); expect(notice.closest("details")).toBeNull();
    expect(screen.getByText("VideoSuite 0.4.0")).toBeVisible();
  });
  it("labels missing evidence and missing recommendations", () => {
    const best_player = scorePartialDoc().records[0].best_player;
    const { rerender } = render(<MagnetSuiteBlock suite={{ status: "ok", best_player }} />);
    expect(screen.getAllByText("evidence: unknown")).toHaveLength(3); expect(screen.getAllByText(unverifiedNotice(null)!)).toHaveLength(3);
    rerender(<MagnetSuiteBlock suite={{ status: "ok", best_player: {} }} />); expect(screen.getByText("No player recommendation in this result.")).toBeVisible();
  });
  it.each([
    [{ status: "unavailable", message: "needs 0.4.0" }, "VideoSuite not available: needs 0.4.0"],
    [{ status: "unavailable" }, "VideoSuite not available: VideoSuite is not available."],
    [{ status: "error", message: "VideoSuite scoring failed" }, "VideoSuite could not score this file: VideoSuite scoring failed"],
    [{ status: "error", error_kind: "timeout" }, "VideoSuite could not score this file: VideoSuite took too long and was stopped."],
    [{ status: "error", error_kind: "__proto__" }, "VideoSuite could not score this file: The job failed."],
    [{ status: "skipped", message: "VideoSuite time budget (300 s) used up" }, "VideoSuite skipped this file: VideoSuite time budget (300 s) used up"],
    [{ status: "skipped" }, "VideoSuite skipped this file: no reason given"],
    [null, "VideoSuite result not readable."], ["x", "VideoSuite result not readable."], [{ status: "weird" }, "VideoSuite result not readable."],
  ])("renders status safely %j", (suite, text) => { render(<MagnetSuiteBlock suite={suite} />); expect(screen.getByText(text as string)).toBeVisible(); });
  it("renders hostile text and prototype-named devices literally", () => {
    const best = scorePartialDoc().records[0].best_player!.sony_bravia_8_ii;
    const { container } = render(<MagnetSuiteBlock suite={{ status: "ok", best_player: { constructor: { ...best, reason: "<img src=x>" } } }} />);
    expect(screen.getByText(/constructor:/)).toBeVisible(); expect(screen.getByText("<img src=x>")).toBeVisible(); expect(container.querySelector("img")).toBeNull();
  });
});
