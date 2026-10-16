import { useState } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { FileChooser } from "./FileChooser";
import { DEFAULT_CHOOSER } from "./paths";
import { devicesDoc, suiteSettings } from "./testing/fixtures";
import type { SuiteSettings } from "./types";

function Harness({ disabled = false, settings = suiteSettings() }: { disabled?: boolean; settings?: SuiteSettings }) {
  const [value, onChange] = useState(DEFAULT_CHOOSER);
  return <FileChooser value={value} onChange={onChange} devices={devicesDoc()} settings={settings} disabled={disabled} />;
}
describe("file chooser", () => {
  it("validates paths and language on input", () => {
    const { container } = render(<Harness />);
    expect(container.querySelector(".suite-problems")).toBeNull();
    fireEvent.change(screen.getByRole("textbox", { name: /Video files/ }), { target: { value: "D:\\a.mkv\nD:\\b.mp4" } });
    expect(screen.getByText("2 file(s) ready.")).toBeVisible();
    fireEvent.change(screen.getByRole("textbox", { name: /Video files/ }), { target: { value: "D:\\a.txt" } });
    expect(screen.getByText("Line 1: not a supported video file (.mkv .mp4 .m4v .ts .m2ts .hevc .h265).")).toBeVisible();
    fireEvent.change(screen.getByRole("textbox", { name: /Video files/ }), { target: { value: Array.from({ length: 51 }, (_, i) => `D:\\${i}.mkv`).join("\n") } });
    expect(screen.getByText("Too many files: 51. VideoSuite takes at most 50 at a time.")).toBeVisible();
    fireEvent.change(screen.getByLabelText("Audio and subtitle language"), { target: { value: "english" } });
    expect(screen.getByText("Language must be a code such as eng or pt-BR.")).toBeVisible();
  });
  it("lists fixture devices and resets player after device changes", () => {
    render(<Harness />); const device = screen.getByLabelText("Device"); const player = screen.getByLabelText("Player");
    for (const d of devicesDoc().devices) expect(screen.getByRole("option", { name: d.label })).toBeInTheDocument();
    expect(player).toBeDisabled();
    fireEvent.change(device, { target: { value: "sony_bravia_8_ii" } }); expect(player).not.toBeDisabled();
    expect(screen.getByRole("option", { name: "Sony USB media player (built-in) (default)" })).toBeInTheDocument();
    fireEvent.change(player, { target: { value: "kodi" } }); expect(player).toHaveValue("kodi");
    fireEvent.change(device, { target: { value: "all" } }); expect(player).toHaveValue("all"); expect(player).toBeDisabled();
  });
  it("shows no-filesystem and explicit settings hints", () => {
    const settings = suiteSettings(); const { rerender } = render(<Harness settings={settings} />);
    expect(screen.getByText("Your settings file sets no USB file system; VideoSuite assumes none.")).toBeVisible();
    const changed = suiteSettings(); changed.devices.sony_bravia_8_ii.usb_filesystem = "exfat";
    rerender(<Harness settings={changed} />);
    expect(screen.getByText('From your settings: Sony BRAVIA 8 II 65" (K-65XR80M2 / K-65XR8M2), USB drive: exFAT')).toBeVisible();
    const unknown = suiteSettings(); unknown.devices.sony_bravia_8_ii.usb_filesystem = "constructor"; rerender(<Harness settings={unknown} />);
    expect(screen.getByText(/USB drive: constructor/)).toBeVisible();
  });
  it("disables all five controls", () => {
    render(<Harness disabled />);
    for (const el of [...screen.getAllByRole("textbox"), ...screen.getAllByRole("combobox")]) expect(el).toBeDisabled();
    expect(screen.getAllByRole("textbox")).toHaveLength(2); expect(screen.getAllByRole("combobox")).toHaveLength(3);
  });
});
