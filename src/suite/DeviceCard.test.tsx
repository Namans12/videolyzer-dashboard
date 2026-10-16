import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ScoreResults } from "./ScoreResults";
import { devicesDoc, scoreDoc, scorePartialDoc, scoreRecord, withPlayback } from "./testing/fixtures";
import { PARTIAL_TEXT, unverifiedNotice } from "./verdict";
import { NO_RECORDS_TEXT } from "./messages";

describe("device score cards", () => {
  it("renders fixture records and every device/player with evidence", () => {
    render(<ScoreResults records={scoreDoc().records} devices={devicesDoc()} />);
    expect(screen.getAllByRole("heading", { level: 3 }).map(el => el.textContent)).toEqual(["clip_a_crf18.mkv", "clip_b_crf34.mkv", "clip_c_crf26.mp4"]);
    expect(screen.getAllByRole("article")).toHaveLength(9);
    const sony = screen.getAllByRole("article", { name: devicesDoc().devices.find(d => d.id === "sony_bravia_8_ii")!.label })[0];
    expect(sony.querySelectorAll(".suite-evidence")).toHaveLength(4);
    expect(sony.textContent?.match(/\(default player\)/g)).toHaveLength(1);
    const rows = sony.querySelectorAll(".suite-player");
    expect(rows).toHaveLength(3);
    expect(within(rows[0] as HTMLElement).queryByRole("note")).toBeNull();
    const kodi = rows[1] as HTMLElement;
    expect(within(kodi).getByRole("note")).toHaveTextContent(unverifiedNotice("inferred")!);
    expect(within(kodi).getByRole("note")).toBeVisible();
    expect(kodi.querySelector("details")).toBeNull();
    expect(Array.from(kodi.querySelectorAll(".suite-issues li"), el => el.textContent)).toEqual([
      "[warn/n/a] the file has no audio track", "[info/inferred] video codec h264: not tested on this TV",
      "[info/forum] container matroska: MKV plays in Kodi on a BRAVIA 9 (same platform family)",
      "[info/inferred] the least certain rule used is video_codecs.h264 (inferred), so the score is a range", "HDR path not verified (inferred)",
    ]);
    expect(kodi).not.toHaveTextContent("the picture path rests on inferred evidence only: not tested on this TV");
    expect(kodi).toHaveTextContent("Score: 58 (range 43-73)"); expect(rows[0]).toHaveTextContent("Score: 58");
  });
  it("shows rounded range, veto first, settings and literal issue text", () => {
    const rec = withPlayback(scoreRecord(), "sony_bravia_8_ii/builtin_usb", { score: 84.4, score_range: [69.2, 99], verdict: "WONT_PLAY", evidence: "official", usb_filesystem: "exfat", usb_filesystem_source: "settings", issues: [
      { code: "info", severity: "info", message: "<img src=x onerror=alert(1)>", confidence: "official" },
      { code: "audio_codec_unsupported", severity: "veto", message: "TrueHD audio is silent on this player", confidence: "official" },
    ] });
    const { container } = render(<ScoreResults records={[rec]} devices={devicesDoc()} />);
    const row = screen.getByRole("article", { name: devicesDoc().devices.find(d => d.id === "sony_bravia_8_ii")!.label }).querySelector(".suite-player")!;
    expect(row).toHaveTextContent("Will not play evidence: official"); expect(row).toHaveTextContent("Score: 84 (range 69-99)");
    expect(row.querySelector("li")).toHaveTextContent("[veto/official] TrueHD audio is silent on this player");
    expect(screen.getByText("USB file system: exFAT (from your settings)")).toBeVisible();
    expect(container.querySelector("img")).toBeNull(); expect(screen.getByText("[info/official] <img src=x onerror=alert(1)>")).toBeVisible();
  });
  it.each([false, true])("shows no-player and switch advice (%s)", switching => {
    const rec = scoreRecord(); const best = rec.best_player!.sony_bravia_8_ii;
    rec.best_player!.sony_bravia_8_ii = { ...best, player: switching ? "kodi" : "builtin_usb", label: switching ? "Kodi on the TV (Dolby Vision compatibility mode on)" : best.label, verdict: switching ? "PLAYS_DEGRADED" : "WONT_PLAY", switch: switching, reason: "specific reason" };
    render(<ScoreResults records={[rec]} devices={devicesDoc()} />);
    expect(screen.getByText(switching ? "Better player: Kodi on the TV (Dolby Vision compatibility mode on)" : "No player on this device will play it")).toBeVisible();
    const hint = screen.getByText("specific reason").closest(".suite-best-player")!;
    expect(hint).toHaveTextContent(switching ? "evidence: inferred" : "evidence: official");
  });
  it.each([false, true])("supports missing or empty best_player (%s)", missing => {
    const rec = scoreRecord(); if (missing) delete rec.best_player; else rec.best_player = {};
    const { container } = render(<ScoreResults records={[rec]} devices={null} />);
    expect(screen.getAllByRole("article")).toHaveLength(3); expect(container.querySelector(".suite-best-player")).toBeNull();
  });
  it("labels partial records and caveats", () => {
    const records = scorePartialDoc().records;
    render(<ScoreResults records={records} devices={devicesDoc()} />);
    expect(screen.getByRole("heading", { name: "Movie.2160p.WEB-DL.DDP5.1.mkv" })).toBeVisible();
    expect(screen.getByText(PARTIAL_TEXT)).toBeVisible();
    for (const caveat of records[0].source.caveats ?? []) expect(screen.getByText(caveat)).toBeVisible();
  });
  it("shows full long non-ASCII names", () => {
    const rec = scoreRecord(); rec.source.path = "D:\\" + "x".repeat(300) + " & 100% Спайдер.mkv"; delete rec.source.real_name;
    render(<ScoreResults records={[rec]} devices={null} />);
    expect(screen.getByText(rec.source.path)).toBeVisible(); expect(screen.getByRole("heading", { name: rec.source.path.slice(3) })).toBeVisible();
  });
  it("shows empty state", () => { render(<ScoreResults records={[]} devices={null} />); expect(screen.getByText(NO_RECORDS_TEXT)).toBeVisible(); });
});
