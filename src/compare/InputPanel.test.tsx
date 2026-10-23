import { useState } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { InputPanel } from "./InputPanel";
import { checkCandidates, defaultForm, ONE_TITLE_TEXT, TRANSFER_NOTICE } from "./types";
import type { CompareForm } from "./types";
import { devicesDoc } from "../suite/testing/fixtures";
import { installFetch, unexpectedFetch } from "../suite/testing/fetchMock";

function Harness({ kind = "files" as "files" | "magnets", onForm }: { kind?: "files" | "magnets"; onForm?: (f: CompareForm) => void }) {
  const [form, setForm] = useState<CompareForm>(() => defaultForm(kind, devicesDoc()));
  return <InputPanel form={form} disabled={false} onChange={(next) => { setForm(next); onForm?.(next); }} />;
}
const lines = (...xs: string[]) => xs.join("\n");

describe("InputPanel", () => {
  it("rejects repeated paths duplicate basenames and wrong candidate counts before work", () => {
    expect(checkCandidates("files", "D:\\Movies\\A.mkv").problems).toContain("Add at least 2 versions to compare (you have 1).");
    expect(checkCandidates("files", lines("D:\\Movies\\A.mkv", "d:\\movies\\a.mkv")).problems)
      .toContain("Line 2 repeats a file already listed. Remove the repeated line.");
    expect(checkCandidates("files", lines("D:\\Movies\\A.mkv", "E:\\Other\\a.MKV")).problems)
      .toContain("Line 2 has the same file name as line 1 (a.MKV). Comparison reports are matched by file name, so give each version a different name in Explorer, or remove one.");
    const fifty = Array.from({ length: 51 }, (_, i) => `D:\\Movies\\v${i}.mkv`);
    expect(checkCandidates("files", lines(...fifty)).problems).toContain("Too many versions: 51. Compare at most 50 files at a time.");
    expect(checkCandidates("files", lines(...fifty.slice(0, 50))).problems).toEqual([]);
    expect(checkCandidates("files", lines("D:\\Movies\\A.txt", "Movies\\B.mkv")).problems).toEqual([
      "Line 1 is not a supported video file (.mkv .mp4 .m4v .ts .m2ts .hevc .h265).",
      "Line 2: use a full local path that starts with a drive letter, such as D:\\Movies\\film.mkv.",
    ]);
    const magnets = Array.from({ length: 9 }, (_, i) => `magnet:?xt=urn:btih:${String(i).repeat(40)}`);   // 0-8: valid hex
    expect(checkCandidates("magnets", lines(...magnets)).problems).toContain("Too many links: 9. Check at most 8 magnet links at a time.");
    expect(checkCandidates("magnets", lines(magnets[0], magnets[0])).problems).toContain("Line 2 repeats a link already listed. Remove the repeated line.");
    expect(checkCandidates("magnets", lines(magnets[0], "https://example.invalid/x")).problems).toContain("Line 2 is not a magnet link (it must start with magnet:?).");
    expect(checkCandidates("magnets", lines(magnets[0])).problems).toContain("Add at least 2 magnet links to compare (you have 1).");
    expect(checkCandidates("magnets", lines(...magnets.slice(0, 8))).problems).toEqual([]);

    installFetch(unexpectedFetch);
    render(<Harness />);
    fireEvent.change(screen.getByLabelText(/Video files on this computer/), { target: { value: lines("D:\\A.mkv", "D:\\A.mkv") } });
    expect(screen.getByRole("list", { name: "Problems with the list" })).toHaveTextContent("Line 2 repeats a file already listed.");
    expect(screen.getByRole("textbox", { name: /Video files on this computer/ })).toHaveAttribute("aria-invalid", "true");
  });

  it("refuses a magnet link whose info-hash is not usable, inline", () => {
    const good = `magnet:?xt=urn:btih:${"0123456789abcdef".repeat(2)}01234567&dn=Film`;
    const base32 = `magnet:?dn=Film&xt=urn:btih:${"ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"}`;
    expect(checkCandidates("magnets", lines(good, base32)).problems).toEqual([]);
    const bad = [
      "magnet:?xt=urn:btih:zzzz", `magnet:?xt=urn:btih:${"g".repeat(40)}`, "magnet:?dn=No.Hash", `magnet:?xt=urn:btih:${"a".repeat(41)}`,
    ];
    for (const link of bad) {
      expect(checkCandidates("magnets", lines(good, link)).problems, link).toEqual([
        "Line 2 is not a usable magnet link: the part after urn:btih: must be the 40-character torrent hash (0-9 and A-F) "
          + "or its 32-character form (A-Z and 2-7). Copy the whole link again.",
      ]);
    }
    installFetch(unexpectedFetch);
    render(<Harness kind="magnets" />);
    fireEvent.change(screen.getByRole("textbox", { name: /Magnet links/ }), { target: { value: lines(good, bad[0]) } });
    expect(screen.getByRole("list", { name: "Problems with the list" })).toHaveTextContent("Line 2 is not a usable magnet link");
    expect(screen.getByRole("textbox", { name: /Magnet links/ })).toHaveAttribute("aria-invalid", "true");
  });

  it("shows one-title and limited-transfer warnings without grouping", () => {
    installFetch(unexpectedFetch);
    render(<Harness />);
    expect(screen.getByText(ONE_TITLE_TEXT)).toBeInTheDocument();
    expect(ONE_TITLE_TEXT).toBe("Add versions of one movie/title only. If you mixed titles or episodes, remove them and start a separate run for each title.");
    expect(screen.queryByText(TRANSFER_NOTICE)).toBeNull();
    fireEvent.click(screen.getByRole("radio", { name: "Magnet links" }));
    expect(screen.getByText(ONE_TITLE_TEXT)).toBeInTheDocument();
    expect(screen.getByText(TRANSFER_NOTICE)).toBeInTheDocument();
    // mixed episodes stay exactly as typed: no grouping, splitting, detection or sorting
    const mixed = lines("magnet:?xt=urn:btih:" + "1".repeat(40) + "&dn=Show.S01E01", "magnet:?xt=urn:btih:" + "2".repeat(40) + "&dn=Show.S01E02");
    expect(checkCandidates("magnets", mixed)).toEqual({ items: mixed.split("\n"), problems: [] });
    for (const role of ["button", "checkbox", "combobox"] as const) {
      expect(screen.queryByRole(role, { name: /episode|season|group|split|detect/i })).toBeNull();
    }
  });

  it("is keyboard operable with labelled radios and resets the file-entry choices on switch", () => {
    installFetch(unexpectedFetch);
    let last: CompareForm | null = null;
    render(<Harness kind="magnets" onForm={(f) => { last = f; }} />);
    const group = screen.getByRole("radiogroup", { name: "What are you comparing?" });
    expect(group).toBeInTheDocument();
    const files = screen.getByRole("radio", { name: "Downloaded files" });
    files.focus();
    fireEvent.click(files);
    expect(files).toBeChecked();
    expect(last).toMatchObject({ inputKind: "files", depth: "full", fileChoices: {} });
    expect(screen.getByRole("button", { name: "Choose files…" })).toBeEnabled();
  });
});
