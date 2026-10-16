import type { DeviceOptions } from "./types";

export const MAX_PATHS = 50;
export const MAX_PATH_LENGTH = 4096;
export const VIDEO_EXTENSIONS = [".mkv", ".mp4", ".m4v", ".ts", ".m2ts", ".hevc", ".h265"];
export const LANG_RE = /^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?$/;
export const FOLDER_FORBIDDEN = ["&", "^", "%", "!", '"', "'", "<", ">", "|"];
const DRIVE_RE = /^[A-Za-z]:[\\/]/;
export function pathIdentity(path: string): string {
  return DRIVE_RE.test(path) || path.startsWith("\\\\") ? path.toLowerCase() : path;
}
export function hasControl(value: string): boolean {
  return Array.from(value).some(ch => {
    const code = ch.codePointAt(0)!;
    return code <= 31 || code === 127 || code === 133 || code === 8232 || code === 8233;
  });
}
function unquote(s: string): string { return s.length >= 2 && s.startsWith('"') && s.endsWith('"') ? s.slice(1, -1).trim() : s; }
export function checkPaths(text: string): { paths: string[]; problems: string[] } {
  const paths: string[] = []; const problems: string[] = []; const seen = new Set<string>();
  String(text ?? "").split(/\r?\n/).forEach((raw, i) => {
    const n = i + 1; const p = unquote(raw.trim());
    if (p === "") return;
    if (hasControl(p)) { problems.push(`Line ${n}: the path contains a control character.`); return; }
    if (p.length > MAX_PATH_LENGTH) { problems.push(`Line ${n}: the path is longer than ${MAX_PATH_LENGTH} characters.`); return; }
    if (!DRIVE_RE.test(p)) { problems.push(`Line ${n}: use a full local path that starts with a drive letter, such as D:\\Movies\\film.mkv.`); return; }
    if (!VIDEO_EXTENSIONS.some(e => p.toLowerCase().endsWith(e))) { problems.push(`Line ${n}: not a supported video file (${VIDEO_EXTENSIONS.join(" ")}).`); return; }
    const identity = pathIdentity(p);
    if (!seen.has(identity)) { seen.add(identity); paths.push(p); }
  });
  if (paths.length === 0 && problems.length === 0) problems.push("Enter at least one video file path (one per line).");
  if (paths.length > MAX_PATHS) problems.push(`Too many files: ${paths.length}. VideoSuite takes at most ${MAX_PATHS} at a time.`);
  return { paths, problems };
}
export function checkFolder(text: string): { folder: string | null; problem: string | null } {
  const p = unquote(String(text ?? "").trim());
  if (p === "") return { folder: null, problem: "Enter the folder for the comparison reports." };
  if (hasControl(p)) return { folder: null, problem: "The folder path contains a control character." };
  if (p.length > 1000) return { folder: null, problem: "The folder path is longer than 1000 characters." };
  if (!DRIVE_RE.test(p)) return { folder: null, problem: "Use a full local folder path that starts with a drive letter, such as D:\\Reports\\run1." };
  if (FOLDER_FORBIDDEN.some(c => p.includes(c))) return { folder: null, problem: `The folder path cannot contain any of these characters: ${FOLDER_FORBIDDEN.join(" ")}` };
  return { folder: p, problem: null };
}
export function checkLang(text: string): string | null { return LANG_RE.test(String(text ?? "")) ? null : "Language must be a code such as eng or pt-BR."; }
export type FsChoice = "settings" | "fat32" | "exfat" | "ntfs";
export interface FileChooserValue { text: string; device: string; player: string; fs: FsChoice; lang: string; }
export const DEFAULT_CHOOSER: FileChooserValue = { text: "", device: "all", player: "all", fs: "settings", lang: "eng" };
export function toDeviceOptions(v: FileChooserValue): DeviceOptions {
  return { ...(v.device !== "all" ? { device: v.device } : {}), ...(v.player !== "all" ? { player: v.player } : {}), ...(v.fs !== "settings" ? { fs: v.fs } : {}), lang: v.lang };
}
