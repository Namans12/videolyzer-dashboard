import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SettingsPanel } from "./SettingsPanel";
import { suiteSettings } from "./testing/fixtures";

describe("owner settings", () => {
  it.each([undefined, null, "unexpected"])("handles a run settings block with non-array errors: %s", errors => {
    render(<SettingsPanel settings={suiteSettings()} block={{ path: "missing.json", loaded: false, applied_count: 0, errors: errors as unknown as string[] }} loadError={null} />);
    expect(screen.getByText("Settings file: missing.json (not found; defaults in use)")).toBeVisible();
  });
  it("shows the route snapshot and read-only facts", () => {
    render(<SettingsPanel settings={suiteSettings()} block={null} loadError={null} />);
    expect(screen.getByText("Settings file: C:\\fixtures\\suite\\no_settings.json (not found; defaults in use)")).toBeVisible();
    expect(screen.getByText("0 rule override(s) applied.")).toBeVisible(); expect(screen.getAllByRole("row")).toHaveLength(4);
    expect(screen.getAllByText("not set")).toHaveLength(6); expect(screen.getAllByText("none")).toHaveLength(3);
    expect(screen.getByText("Settings at the last settings check:")).toBeVisible();
    expect(screen.getByText("Device facts are from the last settings check.")).toBeVisible();
    expect(screen.getByText("Read-only. Device facts are from the last settings check. Use Check again to reload; the Dashboard may reuse its cached document for up to 60 seconds.")).toBeVisible();
  });
  it("uses completed block while retaining the route device facts", () => {
    const settings = suiteSettings(); settings.loaded = true;
    settings.devices.sony_bravia_8_ii.usb_filesystem = "exfat"; settings.devices.sony_bravia_8_ii.audio_output = "tv_speakers";
    settings.devices.sony_bravia_8_ii.overrides = [{ player: "kodi", rule: "audio_codecs.truehd", source: "owner_test", reason: "tested on my TV", confidence: "owner" }];
    const block = { path: "D:\\Owner\\settings.json", loaded: true, errors: [], applied_count: 1 };
    const { rerender, container } = render(<SettingsPanel settings={settings} block={block} loadError={null} />);
    expect(screen.getByText("Settings file: D:\\Owner\\settings.json (loaded)")).toBeVisible(); expect(screen.getByText("1 rule override(s) applied.")).toBeVisible();
    for (const text of ["exFAT", "TV speakers", "kodi: audio_codecs.truehd (owner; tested on my TV)", "Settings used by the latest completed run:"]) expect(screen.getByText(text)).toBeVisible();
    rerender(<SettingsPanel settings={settings} block={{ ...block, loaded: false, errors: ["settings.json: devices must be an object", "<img src=x>"] }} loadError={null} />);
    expect(screen.getByText("Settings file: D:\\Owner\\settings.json (could not be used; defaults in use)")).toBeVisible(); expect(screen.getByText("<img src=x>")).toBeVisible(); expect(container.querySelector("img")).toBeNull();
  });
  it.each(["constructor", "toString", "__proto__"])("shows unknown settings values %s safely", value => {
    const settings = suiteSettings(); settings.devices.sony_bravia_8_ii.usb_filesystem = value; settings.devices.sony_bravia_8_ii.audio_output = value;
    render(<SettingsPanel settings={settings} block={null} loadError={null} />); expect(screen.getAllByText(value)).toHaveLength(2);
  });
  it("covers loading, errors, absent path and unsupported suite", () => {
    const { rerender } = render(<SettingsPanel settings={null} block={null} loadError={null} />); expect(screen.getByText("Loading settings…")).toBeVisible();
    const settings = { ...suiteSettings(), supported: false, path: null };
    rerender(<SettingsPanel settings={settings} block={null} loadError="load failed" />);
    expect(screen.getByRole("alert")).toHaveTextContent("load failed");
    expect(screen.getByText("This VideoSuite version does not report owner settings (needs 0.4.0).")).toBeVisible(); expect(screen.getByText("No settings file path reported by VideoSuite.")).toBeVisible();
  });
});
