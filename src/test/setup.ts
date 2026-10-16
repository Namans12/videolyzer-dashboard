import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  vi.restoreAllMocks();                   // spies that make Storage throw (T14) are gone before clear()
  try { localStorage.clear(); } catch { /* storage unavailable in this test */ }
});
