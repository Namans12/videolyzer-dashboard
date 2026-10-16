import { describe, expect, it } from "vitest";
const sources = import.meta.glob("/src/**/*.{ts,tsx}", { query: "?raw", import: "default", eager: true }) as Record<string, string>;
describe("frontend security rules", () => {
  it("never renders HTML from strings, evaluates code or calls a non-loopback host", () => {
    const offenders: string[] = [];
    for (const [file, text] of Object.entries(sources)) {
      if (file.endsWith("security.test.ts")) continue;
      if (/dangerouslySetInnerHTML|\.innerHTML\s*=|outerHTML\s*=|\beval\(|new Function\(/.test(text)) offenders.push(file);
      for (const m of text.matchAll(/https?:\/\/[^\s"'`)]+/g)) {
        if (!/^http:\/\/127\.0\.0\.1:8000\b/.test(m[0]) && !/^http:\/\/www\.w3\.org\//.test(m[0])) offenders.push(`${file}: ${m[0]}`);
      }
    }
    expect(offenders).toEqual([]);
  });
});
