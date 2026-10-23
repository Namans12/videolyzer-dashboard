import { describe, expect, it } from "vitest";
const sources = import.meta.glob("/src/**/*.{ts,tsx}", { query: "?raw", import: "default", eager: true }) as Record<string, string>;
describe("frontend security rules", () => {
  it("never renders HTML from strings, evaluates code or calls a non-loopback host", () => {
    const offenders: string[] = [];
    for (const [file, text] of Object.entries(sources)) {
      if (file.endsWith("security.test.ts") || /\.test\.tsx?$/.test(file)) continue;
      if (/dangerouslySetInnerHTML|\.innerHTML\s*=|outerHTML\s*=|\beval\(|new Function\(/.test(text)) offenders.push(file);
      for (const m of text.matchAll(/https?:\/\/[^\s"'`)]+/g)) {
        if (!/^http:\/\/www\.w3\.org\//.test(m[0])) offenders.push(`${file}: ${m[0]}`);
      }
    }
    expect(offenders).toEqual([]);
  });
  it("renders backend text and reports without executable HTML", () => {
    const offenders: string[] = [];
    let frames = 0;
    for (const [file, text] of Object.entries(sources)) {
      if (/\.test\.tsx?$/.test(file)) continue;
      if (/srcDoc|srcdoc|allow-scripts|allow-same-origin|window\.open\(|createContextualFragment|insertAdjacentHTML|document\.write/.test(text)) {
        offenders.push(file);
      }
      for (const m of text.matchAll(/<iframe\b[^>]*>/g)) {
        frames += 1;
        if (!/\ssandbox=""/.test(m[0])) offenders.push(`${file}: ${m[0]}`);
      }
    }
    expect(offenders).toEqual([]);
    expect(frames).toBe(1);                       // the one in-app HTML report viewer, always fully sandboxed
    // unified reports are reached only through the run's opaque registry URLs, never a local path or file URL
    const panel = sources["/src/compare/ReportPanel.tsx"];
    expect(panel).toBeDefined();
    expect(panel).not.toMatch(/file:|\.path\b/);
  });
});
