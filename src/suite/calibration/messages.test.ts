import { describe, expect, it } from "vitest";
import { calibrationAvailable, CALIBRATION_UPDATE_TEXT, matchText, predictionText } from "./messages";
import { versionInfo } from "../testing/fixtures";

describe("calibration messages", () => {
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
