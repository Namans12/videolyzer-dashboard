import { useState } from "react";
import { API } from "../suite/config";
import type { ReportRef } from "./types";

type View =
  | { state: "loading" }
  | { state: "text"; text: string; checked: boolean }
  | { state: "refused" }
  | { state: "failed" }
  | { state: "html" };

const KIND_TEXT: Record<ReportRef["kind"], string> = { json: "comparison data", text: "text report", html: "web report", log: "log" };

/** Hex SHA-256 of the text's UTF-8 bytes, or null when this browser offers no digest function. */
async function sha256(text: string): Promise<string | null> {
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) return null;
  const digest = await subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

function pretty(kind: ReportRef["kind"], text: string): string {
  if (kind !== "json") return text;
  try { return JSON.stringify(JSON.parse(text), null, 2); } catch { return text; }
}

const withQuery = (url: string, download: "0" | "1") => `${API}${url}?download=${download}`;

/** The run's own reports, reached only through their opaque registry URLs. Text is shown as text after its
 *  hash is checked against the run result; an HTML report opens only in a fully sandboxed frame (no scripts). */
export function ReportPanel({ reports }: { reports: ReportRef[] }) {
  const [open, setOpen] = useState<{ id: string; view: View } | null>(null);
  if (reports.length === 0) return <p className="compare-muted">This run produced no report files.</p>;

  const show = (ref: ReportRef) => {
    if (ref.kind === "html") { setOpen({ id: ref.report_id, view: { state: "html" } }); return; }
    setOpen({ id: ref.report_id, view: { state: "loading" } });
    const settle = (view: View) => setOpen((cur) => (cur?.id === ref.report_id ? { id: ref.report_id, view } : cur));
    fetch(`${API}${ref.url}`, { cache: "no-store" })
      .then(async (res) => {
        if (!res.ok) { settle({ state: "failed" }); return; }
        const text = await res.text();
        const hash = await sha256(text);
        if (hash !== null && hash !== ref.sha256) { settle({ state: "refused" }); return; }
        settle({ state: "text", text: pretty(ref.kind, text), checked: hash !== null });
      })
      .catch(() => settle({ state: "failed" }));
  };

  const current = open !== null ? reports.find((r) => r.report_id === open.id) ?? null : null;
  return <div>
    <ul className="compare-plain-list" aria-label="Reports">
      {reports.map((ref) => <li key={ref.report_id} className="compare-report">
        <span className="compare-path">{ref.name}</span> <span className="compare-muted">({KIND_TEXT[ref.kind]})</span>{" "}
        <button type="button" aria-label={`View ${ref.name}`} onClick={() => show(ref)}>View</button>
        <a href={withQuery(ref.url, "1")} download={ref.name} aria-label={`Export ${ref.name}`}>Export</a>
      </li>)}
    </ul>
    {current !== null && open !== null && <section className="compare-report-view" aria-label={`Report ${current.name}`}>
      <h4 className="compare-path">{current.name}</h4>
      {open.view.state === "loading" && <p role="status">Loading the report…</p>}
      {open.view.state === "failed" && <p role="alert">The report could not be loaded. It may belong to a run the app no longer keeps.</p>}
      {open.view.state === "refused" && <p role="alert">This report changed after the run, so it is not shown.</p>}
      {open.view.state === "text" && <>
        {!open.view.checked && <p className="compare-muted">This browser could not check the report's checksum.</p>}
        <pre className="compare-report-text">{open.view.text}</pre>
      </>}
      {open.view.state === "html" && <iframe sandbox="" title={`Report ${current.name}`} src={withQuery(current.url, "0")}
                                             className="compare-report-frame" referrerPolicy="no-referrer" />}
      <button type="button" onClick={() => setOpen(null)}>Close the report</button>
    </section>}
  </div>;
}
