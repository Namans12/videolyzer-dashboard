import type { VersionState } from "../messages";
import { evidenceWord, SHOWN_AS } from "../verdict";
import type { Prediction } from "./types";

export const CALIBRATION_UPDATE_TEXT = "Please update VideoSuite to 0.5.0 to use calibration.";
export const CALIBRATION_CONTRACT_TEXT = "VideoSuite returned a calibration document the Dashboard cannot use.";
export const PREVIEW_AGAIN_TEXT = "The answers or settings changed. Preview again before applying.";
export const SETTINGS_CHANGED_TEXT = "The settings file changed while applying. Preview and apply again.";
export const RECOVERY_REQUIRED_TEXT = "An earlier apply or undo needs recovery. Review the recovery message before continuing.";
export const KIT_NOT_ACTIVE_TEXT = "Kit not active: it was undone or replaced; later settings are not attributed to it.";
export const OUTPUT_FOLDER_TEXT = "Use a separate local output folder. Its path cannot contain & or %.";
export const SESSION_REQUIRED_TEXT = "Confirm the TV software, audio output and USB filesystem before answering.";
export const ANSWER_NOT_IN_KIT_TEXT = "This answer is not in the kit descriptor.";
export const SAVING_TEXT = "Saving answer…";

// Bundled device profile labels; keep the established short TV player names.
const PLAYER_NAMES: Record<string, string> = { builtin_usb: "Built-in USB player", kodi: "Kodi", just_player: "Just Player",
  vlc: "VLC for Android", samsung_video: "Samsung Video (built-in)",
  windows_player: "Windows Media Player / Movies & TV (with Store codec extensions)" };
export function playerName(id: string, device?: string): string {
  if (id === "vlc" && device === "vivobook_pro_16x") return "VLC 3.0.x (Direct3D11 output)";
  return Object.hasOwn(PLAYER_NAMES, id) ? PLAYER_NAMES[id] : id;
}
const DEVICE_NAMES: Record<string, string> = { sony_bravia_8_ii: "Sony Bravia 8 II",
  galaxy_tab_s9_ultra: "Galaxy Tab S9 Ultra", vivobook_pro_16x: "ASUS Vivobook Pro 16X" };
export function deviceName(id: string): string { return Object.hasOwn(DEVICE_NAMES, id) ? DEVICE_NAMES[id] : id; }

/** Decimal units, matching the MB/GB labels; one decimal makes both sizes easy to compare. */
export function kitSize(bytes: number): string {
  return bytes >= 1_000_000_000 ? `about ${(bytes / 1_000_000_000).toFixed(1)} GB` : `about ${(bytes / 1_000_000).toFixed(1)} MB`;
}

export function ruleSummary(rule: string | null, value: unknown): string {
  if (value === null || value === undefined) return "No rule";
  if (!Array.isArray(value)) return typeof value === "string" ? `${value} (unknown)` : "Unrecognized rule (unknown)";
  const word = typeof value[0] === "string" ? value[0] : "unknown";
  if (rule?.startsWith("hdr_map.")) {
    const picture = Object.hasOwn(SHOWN_AS, word) ? SHOWN_AS[word] : word;
    const quality = typeof value[2] === "number" && Number.isFinite(value[2]) ? value[2] : "unknown";
    return `${picture} picture, quality ${quality} (${evidenceWord(value[3])})`;
  }
  return `${word} (${evidenceWord(value[1])})`;
}

export function calibrationAvailable(state: VersionState): boolean {
  return state.kind === "loaded" && state.info.status === "ok" && Array.isArray(state.info.capabilities)
    && state.info.capabilities.includes("calibration");
}
export function predictionText(prediction: Prediction): string { return `${prediction.text} [${prediction.evidence}]`; }
export function matchText(match: string): string {
  switch (match) {
    case "matches": return "Matches the prediction.";
    case "differs": return "Differs from the prediction.";
    case "unsure": return "No definite answer.";
    default: return "No prediction to compare.";
  }
}
