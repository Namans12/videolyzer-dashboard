import { act } from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ReportPanel } from "./ReportPanel";
import { asRunDetail } from "./types";
import type { ReportRef } from "./types";
import { localFullBoth } from "./testing/fixtures";
import { API } from "../suite/config";
import { callsTo, installFetch, jsonResponse, unexpectedFetch } from "../suite/testing/fetchMock";

const reportsOf = () => asRunDetail(localFullBoth()).result!.reports;
async function sha256(text: string): Promise<string> {
  const { createHash } = await vi.importActual<{ createHash: (a: string) => { update: (t: string) => { digest: (e: string) => string } } }>("node:crypto");
  return createHash("sha256").update(text).digest("hex");
}
const flush = () => act(async () => { for (let i = 0; i < 5; i += 1) await Promise.resolve(); await new Promise((r) => setTimeout(r, 0)); });

describe("ReportPanel", () => {
  it("lists only the run's registry reports with relative view and export links", () => {
    installFetch(unexpectedFetch);
    render(<ReportPanel reports={reportsOf()} />);
    const list = screen.getByRole("list", { name: "Reports" });
    const items = within(list).getAllByRole("listitem");
    expect(items.map((i) => i.querySelector(".compare-path")?.textContent)).toEqual(reportsOf().map((r) => r.name));
    expect(items).toHaveLength(3);
    for (const link of within(list).getAllByRole("link", { name: /Export/ })) {
      expect(link.getAttribute("href")).toMatch(/^\/comparison\/runs\/run-0002\/reports\/[0-9a-f]+\?download=1$/);
      expect(link).toHaveAttribute("download");
    }
    expect(document.body.innerHTML).not.toMatch(/file:|[A-Za-z]:\\/);
  });

  it("shows a JSON report as text after checking its hash, and refuses changed bytes", async () => {
    const [first, second] = reportsOf();
    const body = JSON.stringify({ verdict: "<script>alert(1)</script>", winner: "A.mkv" });
    const good: ReportRef = { ...first, sha256: await sha256(body) };      // test-only: hash of the bytes served below
    const fetch = installFetch((url) => {
      if (url === `${API}${good.url}` || url === `${API}${second.url}`) return jsonResponse(200, body);
      return unexpectedFetch(url);
    });
    render(<ReportPanel reports={[good, second]} />);
    fireEvent.click(screen.getByRole("button", { name: `View ${good.name}` }));
    await flush();
    const viewer = screen.getByRole("region", { name: `Report ${good.name}` });
    expect(within(viewer).getByText(/"verdict": "<script>alert\(1\)<\/script>"/)).toBeInTheDocument();
    expect(viewer.querySelector("script")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: `View ${second.name}` }));
    await flush();
    const refused = screen.getByRole("region", { name: `Report ${second.name}` });
    expect(refused).toHaveTextContent("This report changed after the run, so it is not shown.");
    expect(refused).not.toHaveTextContent("A.mkv");
    expect(callsTo(fetch, good.url)).toHaveLength(1);
  });

  it("opens an HTML report only in a sandboxed frame without scripts", () => {
    installFetch(unexpectedFetch);
    const html: ReportRef = { ...reportsOf()[0], kind: "html", name: "A_vs_B_accurate_f5fe396524.html" };
    render(<ReportPanel reports={[html]} />);
    fireEvent.click(screen.getByRole("button", { name: `View ${html.name}` }));
    const frame = screen.getByTitle(`Report ${html.name}`);
    expect(frame.tagName).toBe("IFRAME");
    expect(frame.getAttribute("sandbox")).toBe("");
    expect(frame.getAttribute("src")).toBe(`${API}${html.url}?download=0`);
    expect(frame).not.toHaveAttribute("srcdoc");
  });

  it("says plainly when a run has no reports", () => {
    render(<ReportPanel reports={[]} />);
    expect(screen.getByText("This run produced no report files.")).toBeInTheDocument();
  });
});
