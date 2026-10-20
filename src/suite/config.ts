// Shared by Analyze, Suite, Calibration, cancellation and event streams.
export function apiBase(value: string | undefined): string {
  if (!value) return "";
  const match = /^https?:\/\/(localhost|127\.0\.0\.1|\[::1\])(?::([0-9]+))?\/?$/.exec(value);
  if (!match || (match[2] && (+match[2] < 1 || +match[2] > 65535))) {
    throw new Error("VITE_API_BASE must be an explicit loopback origin");
  }
  return value.replace(/\/$/, "");
}

export const API = apiBase(import.meta.env.DEV ? import.meta.env.VITE_API_BASE : undefined);
