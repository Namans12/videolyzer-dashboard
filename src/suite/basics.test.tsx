import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SuiteStatusBanner } from "./SuiteStatusBanner";
import { EvidenceLegend } from "./EvidenceLegend";
import { VerdictBadge } from "./VerdictBadge";
import { BestPlayerHint } from "./BestPlayerHint";
import { JobMessage } from "./JobMessage";
import { CANCELLING_TEXT, CANCEL_FAILED_TEXT, UNREACHABLE_TEXT, SUITE_DISABLED_TEXT, versionBanner, errorKindSentence } from "./messages";
import type { VersionState } from "./messages";
import { EVIDENCE_LEGEND_TEXT, unverifiedNotice } from "./verdict";
import { CANCELLED_TEXT, JOB_GONE_TEXT } from "../jobStatus";
import { scoreRecord, versionInfo } from "./testing/fixtures";
import type { UseJobResult } from "./useJob";

const base = { status: "running", current: "Scoring 2 file(s)", error: null, errorKind: null, errorDetail: null, unreachable: false, cancelRequested: false, cancelFailed: false } satisfies Pick<UseJobResult, "status" | "current" | "error" | "errorKind" | "errorDetail" | "unreachable" | "cancelRequested" | "cancelFailed">;
describe("suite basics", () => {
  it.each(["constructor", "toString", "__proto__", "hasOwnProperty"])("safely handles error kind %s", errorKind => {
    render(<JobMessage label="Score" job={{ ...base, status: "error", errorKind }} />);
    expect(screen.getByRole("alert")).toHaveTextContent("The job failed.");
  });
  it("keeps the real old-suite reinstall detail", () => {
    const message = "The installed VideoSuite is 0.2.9; the Dashboard needs 0.3.0 or later. Reinstall the Comparizer (run the latest Video Comparator installer), then restart the Dashboard.";
    render(<SuiteStatusBanner state={{ kind: "loaded", info: versionInfo({ status: "too_old", message }) }} checking={false} onRecheck={() => {}} />);
    expect(screen.getByText(message)).toBeVisible(); expect(screen.getByText(SUITE_DISABLED_TEXT)).toBeVisible();
  });
  const states: VersionState[] = [{ kind: "loading" }, { kind: "failed", status: 403, detail: "refused" }, ...["ok", "too_old", "unavailable", "error", "unchecked"].map(status => ({ kind: "loaded" as const, info: { ...versionInfo(), status, message: "Reinstall the Comparizer." } }))];
  it.each(states)("shows banner $kind", state => {
    const recheck = vi.fn();
    const { rerender } = render(<SuiteStatusBanner state={state} checking={false} onRecheck={recheck} />);
    expect(screen.getByText(versionBanner(state).text)).toBeVisible();
    const disabled = state.kind !== "loading" && !(state.kind === "loaded" && state.info.status === "ok");
    expect(screen.queryByText(SUITE_DISABLED_TEXT) !== null).toBe(disabled);
    if (versionBanner(state).detail) expect(screen.getByText(versionBanner(state).detail!)).toBeVisible();
    fireEvent.click(screen.getByText("Check again")); expect(recheck).toHaveBeenCalledTimes(1);
    rerender(<SuiteStatusBanner state={state} checking onRecheck={recheck} />);
    expect(screen.getByRole("button")).toBeDisabled();
  });
  it("shows the exact legend in a named region", () => {
    render(<EvidenceLegend />);
    expect(screen.getByRole("region", { name: "Evidence legend" })).toHaveTextContent(EVIDENCE_LEGEND_TEXT);
  });
  it.each(["official", "owner", "reviewed", "forum", "inferred", "unknown", "rumour", "constructor", null, undefined])("labels evidence %s honestly", evidence => {
    const { container } = render(<VerdictBadge verdict="PLAYS_DEGRADED" evidence={evidence} />);
    expect(screen.getByText("Plays with a loss")).toBeVisible();
    expect(container.querySelector(".suite-evidence")).toHaveTextContent(`evidence: ${["official", "owner", "reviewed", "forum", "inferred", "unknown"].includes(evidence ?? "") ? evidence : "unknown"}`);
    const notice = unverifiedNotice(evidence);
    if (notice) { expect(screen.getByRole("note")).toHaveTextContent(notice); expect(screen.getByRole("note")).toBeVisible(); expect(screen.getByRole("note").closest("details")).toBeNull(); }
    else expect(screen.queryByRole("note")).toBeNull();
    if (evidence === "inferred") expect(container.querySelector(".suite-evidence")).toHaveAttribute("title", "our deduction");
  });
  it.each(["ODD", "constructor", "__proto__", "toString"])("renders unknown verdict %s", verdict => {
    render(<VerdictBadge verdict={verdict} evidence={null} />);
    expect(screen.getByText(verdict)).toBeVisible(); expect(screen.getByText("evidence: unknown")).toBeVisible();
  });
  it.each(["official", "inferred"])("shows best player with evidence %s and literal reason", evidence => {
    const best = scoreRecord().best_player!.sony_bravia_8_ii;
    const { container } = render(<BestPlayerHint best={{ ...best, reason: "<img src=x onerror=alert(1)>" }} evidence={evidence} />);
    expect(screen.getByText("Keep the default player: Sony USB media player (built-in)")).toBeVisible();
    expect(screen.getByText("<img src=x onerror=alert(1)>")).toBeVisible(); expect(container.querySelector("img")).toBeNull();
    expect(screen.queryByRole("note") !== null).toBe(evidence === "inferred");
  });
  it("shows running job and notices", () => {
    render(<JobMessage label="Score" job={{ ...base, cancelRequested: true, cancelFailed: true, unreachable: true }} />);
    for (const text of ["Score: running…", base.current, CANCELLING_TEXT, CANCEL_FAILED_TEXT, UNREACHABLE_TEXT]) expect(screen.getByText(text)).toBeVisible();
  });
  it.each([["cancelled", CANCELLED_TEXT], ["gone", JOB_GONE_TEXT]] as const)("shows %s", (status, text) => {
    render(<JobMessage label="Score" job={{ ...base, status }} />); expect(screen.getByText(text)).toBeVisible();
  });
  it("keeps raw errors in closed details as text", () => {
    const { container } = render(<JobMessage label="Score" job={{ ...base, status: "error", errorKind: "no_files", error: "failed", errorDetail: "<b>x</b>" }} />);
    expect(screen.getByText(errorKindSentence("no_files"))).toBeVisible(); expect(screen.getByText("failed")).toBeVisible();
    expect(container.querySelector("details")?.open).toBe(false); expect(container.querySelector("pre")).toHaveTextContent("<b>x</b>"); expect(container.querySelector("b")).toBeNull();
  });
  it.each(["idle", "done"] as const)("renders nothing for %s", status => {
    const { container } = render(<JobMessage label="Score" job={{ ...base, status }} />); expect(container).toBeEmptyDOMElement();
  });
});
