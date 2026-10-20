import { describe, expect, it, vi } from "vitest";
import { API, apiBase } from "./config";

describe("API origin configuration", () => {
  it("ignores even an explicit override in production", async () => {
    vi.stubEnv("DEV", false);
    vi.stubEnv("VITE_API_BASE", "https://remote.invalid");
    vi.resetModules();
    try {
      expect((await import("./config")).API).toBe("");
    } finally {
      vi.unstubAllEnvs();
      vi.resetModules();
    }
  });
  it("reads the explicit development override", async () => {
    vi.stubEnv("DEV", true);
    vi.stubEnv("VITE_API_BASE", "http://127.0.0.1:9123");
    vi.resetModules();
    try {
      expect((await import("./config")).API).toBe("http://127.0.0.1:9123");
    } finally {
      vi.unstubAllEnvs();
      vi.resetModules();
    }
  });
  it("uses relative API URLs by default and only an explicit dev override", () => {
    expect(API).toBe("");
    expect(apiBase(undefined)).toBe("");
    expect(apiBase("")).toBe("");
    expect(apiBase("http://127.0.0.1:9123")).toBe("http://127.0.0.1:9123");
    expect(apiBase("http://localhost:6001/")).toBe("http://localhost:6001");
    expect(apiBase("http://[::1]:6001")).toBe("http://[::1]:6001");
  });
  it.each(["https://evil.example", "//evil.example", "/api", "http://127.0.0.1.evil:9",
    "http://user@localhost:9", "http://localhost:9/path", "http://localhost:9?q=x",
    "http://localhost:9#x", "http://localhost:0", "http://localhost:65536"])("refuses %s", value => {
    expect(() => apiBase(value)).toThrow();
  });
});
