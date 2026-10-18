import { describe, expect, it } from "vitest";
import { calibrationAvailable, CALIBRATION_UPDATE_TEXT, deviceName, matchText, playerName, predictionText } from "./messages";
import { versionInfo } from "../testing/fixtures";

describe("calibration messages", () => {
  it("names the real bundled player and device ids and preserves unknown ids safely", () => {
    for (const [id, name] of Object.entries({ builtin_usb: "Built-in USB player", kodi: "Kodi", just_player: "Just Player",
      vlc: "VLC for Android", samsung_video: "Samsung Video (built-in)", windows_player: "Windows Media Player / Movies & TV (with Store codec extensions)" })) {
      expect(playerName(id)).toBe(name);
    }
    expect(playerName("vlc", "vivobook_pro_16x")).toBe("VLC 3.0.x (Direct3D11 output)");
    for (const [id, name] of Object.entries({ sony_bravia_8_ii: "Sony Bravia 8 II", galaxy_tab_s9_ultra: "Galaxy Tab S9 Ultra", vivobook_pro_16x: "ASUS Vivobook Pro 16X" })) {
      expect(deviceName(id)).toBe(name);
    }
    for (const id of ["constructor", "__proto__", "toString", "future_player"]) {
      expect(playerName(id)).toBe(id); expect(deviceName(id)).toBe(id);
    }
  });
  it("gates only on a ready version's calibration capability", () => {
    expect(calibrationAvailable({ kind: "loaded", info: versionInfo({ capabilities: ["calibration"] }) })).toBe(true);
    expect(calibrationAvailable({ kind: "loaded", info: versionInfo({ tool_version: "0.5.0", capabilities: [] }) })).toBe(false);
    expect(calibrationAvailable({ kind: "loaded", info: versionInfo({ status: "too_old", capabilities: ["calibration"] }) })).toBe(false);
    expect(calibrationAvailable({ kind: "loading" })).toBe(false);
    expect(CALIBRATION_UPDATE_TEXT).toContain("update VideoSuite to 0.5.0");
  });
  it("always includes the evidence word and handles hostile lookup keys", () => {
    expect(predictionText({ outcome: "yes", text: "HDR", evidence: "inferred" })).toBe("HDR [inferred]");
    expect(matchText("constructor")).toBe("No prediction to compare.");
    expect(matchText("__proto__")).toBe("No prediction to compare.");
    expect(matchText("differs")).toBe("Differs from the prediction.");
  });
});
