import { describe, expect, it } from "vitest";
import { checkFolder, checkLang, checkPaths, DEFAULT_CHOOSER, hasControl, pathIdentity, toDeviceOptions } from "./paths";

describe("path validation", () => {
  it("case-folds only Windows path identities", () => {
    expect(pathIdentity("D:\\A.mkv")).toBe(pathIdentity("d:\\a.mkv"));
    expect(pathIdentity("D:/A.mkv")).toBe(pathIdentity("d:/a.mkv"));
    expect(pathIdentity("\\\\HOST\\Share\\A.mkv")).toBe(pathIdentity("\\\\host\\share\\a.mkv"));
    expect(pathIdentity("/a/B.mkv")).not.toBe(pathIdentity("/a/b.mkv"));
    expect(pathIdentity("Relative.MKV")).toBe("Relative.MKV");
  });
  it("removes Windows case duplicates with the same result as exact duplicates, keeping first spelling", () => {
    const first = "D:\\Movies\\A.mkv";
    expect(checkPaths(`${first}\nd:\\movies\\a.mkv`)).toEqual(checkPaths(`${first}\n${first}`));
    expect(checkPaths(`${first}\nd:\\movies\\a.mkv`)).toEqual({ paths: [first], problems: [] });
  });
  it("preserves unusual paths, strips surrounding quotes and deduplicates", () => {
    const amp = "D:\\Médias\\Tom & Jerry (2021) 100%.mkv";
    expect(checkPaths(`${amp}\r\n\r\n  "E:\\Movies\\Спайдер-Мен 2160p.MKV"  \n${amp}\n`)).toEqual({ paths: [amp, "E:\\Movies\\Спайдер-Мен 2160p.MKV"], problems: [] });
    expect(checkPaths("D:/Movies/x.m2ts").paths).toHaveLength(1);
  });
  it("reports all invalid lines while retaining valid candidates", () => {
    expect(checkPaths("C:\\x\\notes.txt\n\\\\host\\share\\a.mkv\nrelative.mkv\nC:\\ok.ts")).toEqual({ paths: ["C:\\ok.ts"], problems: [
      "Line 1: not a supported video file (.mkv .mp4 .m4v .ts .m2ts .hevc .h265).",
      "Line 2: use a full local path that starts with a drive letter, such as D:\\Movies\\film.mkv.",
      "Line 3: use a full local path that starts with a drive letter, such as D:\\Movies\\film.mkv.",
    ] });
    expect(checkPaths("   \n")).toEqual({ paths: [], problems: ["Enter at least one video file path (one per line)."] });
  });
  it("checks count and length limits", () => {
    const files = (n: number) => Array.from({ length: n }, (_, i) => `D:\\m\\f${i}.mkv`).join("\n");
    expect(checkPaths(files(50)).problems).toEqual([]);
    expect(checkPaths(files(51)).problems).toEqual(["Too many files: 51. VideoSuite takes at most 50 at a time."]);
    expect(checkPaths("D:\\" + "a".repeat(4097) + ".mkv").problems).toEqual(["Line 1: the path is longer than 4096 characters."]);
  });
  it.each([0, 31, 127, 133, 8232, 8233])("rejects control %s", cp => {
    expect(checkPaths("D:\\a" + String.fromCodePoint(cp) + "b.mkv").problems).toEqual(["Line 1: the path contains a control character."]);
  });
  it("allows non-ASCII printable characters", () => { expect(hasControl("D:\\Médias\\Спайдер & 100%.mkv")).toBe(false); expect(hasControl("a\u00a0b")).toBe(false); });
  it("validates report folders", () => {
    expect(checkFolder(' "D:\\Reports\\run 1" ')).toEqual({ folder: "D:\\Reports\\run 1", problem: null });
    expect(checkFolder("D:\\R&D").problem).toBe("The folder path cannot contain any of these characters: & ^ % ! \" ' < > |");
    expect(checkFolder("").problem).toBe("Enter the folder for the comparison reports.");
    expect(checkFolder("D:\\R\u0001x").problem).toBe("The folder path contains a control character.");
    expect(checkFolder("D:\\" + "x".repeat(1001)).problem).toBe("The folder path is longer than 1000 characters.");
    expect(checkFolder("relative").folder).toBeNull();
  });
  it("validates language and omits default options", () => {
    expect(checkLang("eng")).toBeNull(); expect(checkLang("pt-BR")).toBeNull(); expect(checkLang("english")).toBe("Language must be a code such as eng or pt-BR.");
    expect(toDeviceOptions(DEFAULT_CHOOSER)).toEqual({ lang: "eng" });
    expect(toDeviceOptions({ ...DEFAULT_CHOOSER, device: "sony_bravia_8_ii", player: "kodi", fs: "exfat", lang: "pt-BR" })).toEqual({ device: "sony_bravia_8_ii", player: "kodi", fs: "exfat", lang: "pt-BR" });
  });
});
