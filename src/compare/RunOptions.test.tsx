import { useState } from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { RunOptions } from "./RunOptions";
import { defaultForm, FULL_NEEDS_FILES_TEXT } from "./types";
import type { CompareForm } from "./types";
import { devicesDoc } from "../suite/testing/fixtures";
import type { DevicesDocument } from "../suite/types";

function Harness({ kind = "files" as "files" | "magnets", devices = devicesDoc() as DevicesDocument | null,
                   onForm }: { kind?: "files" | "magnets"; devices?: DevicesDocument | null; onForm?: (f: CompareForm) => void }) {
  const [form, setForm] = useState<CompareForm>(() => defaultForm(kind, devices));
  return <RunOptions form={form} devices={devices} disabled={false} onChange={(next) => { setForm(next); onForm?.(next); }} />;
}

describe("RunOptions", () => {
  it("offers only TV laptop tablet in fixed order without management", () => {
    const doc = devicesDoc();          // the real catalogue is alphabetical: galaxy, sony, vivobook
    doc.devices.push({ ...doc.devices[1], id: "unknown_projector", label: "Unknown projector" });
    render(<Harness devices={doc} />);
    const group = screen.getByRole("radiogroup", { name: "Device" });
    const radios = within(group).getAllByRole("radio");
    expect(radios.map((r) => (r as HTMLInputElement).value)).toEqual(["sony_bravia_8_ii", "vivobook_pro_16x", "galaxy_tab_s9_ultra"]);
    expect(radios.map((r) => r.getAttribute("aria-label") ?? r.closest("label")?.textContent)).toEqual([
      expect.stringContaining("Sony Bravia 8 II (TV)"), expect.stringContaining("ASUS Vivobook Pro 16X (laptop)"),
      expect.stringContaining("Galaxy Tab S9 Ultra (tablet)"),
    ]);
    expect(screen.queryByText(/Unknown projector/)).toBeNull();
    // the catalogue's own label and players come from the real devices response
    expect(group).toHaveTextContent(doc.devices.find((d) => d.id === "sony_bravia_8_ii")!.label);
    const player = screen.getByRole("combobox", { name: "Player" });
    expect(within(player).getAllByRole("option").map((o) => (o as HTMLOptionElement).value)).toEqual(["builtin_usb", "kodi", "just_player"]);
    expect(player).toHaveValue("builtin_usb");
    for (const name of [/add/i, /remove/i, /restore/i, /reorder/i, /move up/i, /move down/i, /manage/i, /my devices/i]) {
      expect(screen.queryByRole("button", { name })).toBeNull();
    }
    fireEvent.click(screen.getByRole("radio", { name: /Galaxy Tab S9 Ultra/ }));
    const tabPlayers = doc.devices.find((d) => d.id === "galaxy_tab_s9_ultra")!.players;
    expect(screen.getByRole("combobox", { name: "Player" }))
      .toHaveValue(tabPlayers.find((p) => p.default)?.id ?? tabPlayers[0].id);
  });

  it("keeps Full disabled for magnets and states download-files-first", () => {
    render(<Harness kind="magnets" />);
    expect(screen.getByRole("radio", { name: /Quick/ })).toBeChecked();
    expect(screen.getByRole("radio", { name: /Full/ })).toBeDisabled();
    expect(screen.getByText(FULL_NEEDS_FILES_TEXT)).toBeInTheDocument();
    expect(FULL_NEEDS_FILES_TEXT).toBe("Download the files first, then choose Downloaded files.");
  });

  it("disables device, player and conditions for Overall and restores them for Device", () => {
    let last: CompareForm | null = null;
    render(<Harness onForm={(f) => { last = f; }} />);
    expect(screen.getByRole("radio", { name: /^Both/ })).toBeChecked();
    fireEvent.click(screen.getByRole("radio", { name: /^Best overall/ }));
    for (const radio of within(screen.getByRole("radiogroup", { name: "Device" })).getAllByRole("radio")) expect(radio).toBeDisabled();
    expect(screen.getByRole("combobox", { name: "Player" })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: /alternatives/i })).toBeDisabled();
    expect(screen.getByRole("combobox", { name: "USB file system" })).toBeDisabled();
    expect(screen.getByRole("combobox", { name: "Audio output" })).toBeDisabled();
    fireEvent.click(screen.getByRole("radio", { name: /^Best for my device/ }));
    expect(screen.getByRole("combobox", { name: "Player" })).toBeEnabled();
    fireEvent.change(screen.getByRole("combobox", { name: "Player" }), { target: { value: "just_player" } });
    fireEvent.click(screen.getByRole("checkbox", { name: /alternatives/i }));
    expect(last).toMatchObject({ results: "device", primaryPlayer: "just_player", includeAlternatives: false });
    expect(screen.getByRole("combobox", { name: "Audio output" })).toHaveValue("");
    expect(screen.getByRole("combobox", { name: "USB file system" })).toHaveValue("");
  });

  it("explains that Quick estimates from metadata while Full measures the picture", () => {
    render(<Harness />);
    expect(screen.getByRole("radiogroup", { name: "Depth" })).toHaveTextContent("picture not measured");
    expect(screen.getByRole("radiogroup", { name: "Depth" })).toHaveTextContent("every pair");
  });
});
