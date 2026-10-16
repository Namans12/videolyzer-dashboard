import { afterEach, vi } from "vitest";
import { API } from "../config";

export type FetchHandler = (url: string, init: RequestInit | undefined) => Response | Promise<Response>;
export type FetchMock = ReturnType<typeof installFetch>;

const UNEXPECTED_PREFIX = "unexpected fetch ";
const unexpected: string[] = [];

/** Call from a handler for any URL the test did not plan for. */
export function unexpectedFetch(url: string): never {
  throw new Error(UNEXPECTED_PREFIX + url);
}

/** Returns and clears the recorded unexpected requests (only the harness self-test uses this). */
export function takeUnexpectedFetches(): string[] {
  return unexpected.splice(0);
}

// An unexpected request fails the test even when the code under test swallows the rejection.
afterEach(() => {
  const found = takeUnexpectedFetches();
  if (found.length > 0) throw new Error(`unexpected fetch call(s): ${found.join(" | ")}`);
});

/** A minimal Response: what api.ts (`ok`, `status`, `text`) and the old App loops (`json`) use. */
export function jsonResponse(status: number, body: unknown): Response {
  const text = typeof body === "string" ? body : JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    text: () => Promise.resolve(text),
    json: () => Promise.resolve().then(() => JSON.parse(text) as unknown),
  } as unknown as Response;
}

export function installFetch(handler: FetchHandler) {
  const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    try {
      return await handler(String(input), init);
    } catch (err) {
      if (err instanceof Error && err.message.startsWith(UNEXPECTED_PREFIX)) unexpected.push(err.message);
      throw err;                         // deliberate TypeError network failures pass through unrecorded
    }
  });
  vi.stubGlobal("fetch", mock);
  return mock;
}

/** Calls whose URL is exactly API + pathAndQuery. */
export function callsTo(mock: FetchMock, pathAndQuery: string): [string, RequestInit | undefined][] {
  return mock.mock.calls
    .map((c) => [String(c[0]), c[1]] as [string, RequestInit | undefined])
    .filter(([url]) => url === API + pathAndQuery);
}

/** The parsed JSON body of the n-th call to pathAndQuery. */
export function bodyOf(mock: FetchMock, pathAndQuery: string, n = 0): unknown {
  const call = callsTo(mock, pathAndQuery)[n];
  if (!call) throw new Error(`no call ${n} to ${pathAndQuery}`);
  return JSON.parse(String(call[1]?.body));
}
