import { useState } from "react";
import { chooseFiles } from "./api";
import { SuiteApiError, errorSentence } from "../suite/api";
import { pathIdentity } from "../suite/paths";
import { checkCandidates, ONE_TITLE_TEXT, switchInput, TRANSFER_NOTICE } from "./types";
import type { CompareForm, InputKind } from "./types";

const KINDS: { kind: InputKind; label: string }[] = [
  { kind: "files", label: "Downloaded files" },
  { kind: "magnets", label: "Magnet links" },
];

/** Step 1: what to compare. Candidates are kept exactly as listed: no title detection, grouping or sorting. */
export function InputPanel({ form, onChange, disabled }: {
  form: CompareForm;
  onChange: (next: CompareForm) => void;
  disabled: boolean;
}) {
  const [chooser, setChooser] = useState<{ busy: boolean; message: string | null }>({ busy: false, message: null });
  const files = form.inputKind === "files";
  const text = files ? form.pathsText : form.magnetsText;
  const check = checkCandidates(form.inputKind, text);
  const showProblems = text.trim() !== "" && check.problems.length > 0;
  const fieldId = files ? "compare-paths" : "compare-magnets";

  const choose = () => {
    if (chooser.busy) return;
    setChooser({ busy: true, message: null });
    chooseFiles().then(({ paths, cancelled }) => {
      if (cancelled || paths.length === 0) { setChooser({ busy: false, message: "No files were chosen." }); return; }
      const lines = form.pathsText.split(/\r?\n/).map((l) => l.trim()).filter((l) => l !== "");
      const known = new Set(lines.map(pathIdentity));
      const added = paths.filter((p) => !known.has(pathIdentity(p)));
      onChange({ ...form, pathsText: [...lines, ...added].join("\n") });
      setChooser({ busy: false, message: `${added.length} file(s) added.` });
    }).catch((err: unknown) => {
      const unavailable = err instanceof SuiteApiError && (err.status === 503 || err.status === 0);
      setChooser({
        busy: false,
        message: unavailable ? "The Windows file dialog is not available here. Paste the full paths instead." : errorSentence(err),
      });
    });
  };

  return <section className="compare-section" aria-labelledby="compare-input-title">
    <h2 id="compare-input-title" tabIndex={-1}>1. What are you comparing?</h2>
    <div role="radiogroup" aria-label="What are you comparing?" className="compare-choices">
      {KINDS.map(({ kind, label }) => <label key={kind} className="compare-choice">
        <input type="radio" name="compare-input" value={kind} checked={form.inputKind === kind} disabled={disabled}
               onChange={() => onChange(switchInput(form, kind))} />
        {label}
      </label>)}
    </div>

    <p className="compare-note">{ONE_TITLE_TEXT}</p>

    {files
      ? <>
        <button type="button" onClick={choose} disabled={disabled || chooser.busy}>
          {chooser.busy ? "Waiting for the file dialog…" : "Choose files…"}
        </button>
        {chooser.message !== null && <p role="status" className="compare-muted">{chooser.message}</p>}
        <label htmlFor={fieldId}>Video files on this computer (one full path per line, 2–50)</label>
        <p className="compare-muted" id="compare-paths-help">
          Or paste paths: in Explorer, Shift + right-click a file and choose Copy as path. Files are read where they are;
          nothing is uploaded or copied.
        </p>
      </>
      : <>
        <p className="compare-notice">{TRANSFER_NOTICE}</p>
        <label htmlFor={fieldId}>Magnet links (one per line, 2–8)</label>
      </>}
    <textarea
      id={fieldId}
      rows={files ? 6 : 5}
      spellCheck={false}
      value={text}
      disabled={disabled}
      aria-invalid={showProblems}
      aria-describedby={showProblems ? "compare-input-problems" : files ? "compare-paths-help" : undefined}
      placeholder={files ? "D:\\Movies\\Film (2021) 2160p.mkv" : "magnet:?xt=urn:btih:…"}
      onChange={(e) => onChange(files ? { ...form, pathsText: e.target.value } : { ...form, magnetsText: e.target.value, fileChoices: {} })}
    />
    {check.problems.length === 0 && <p className="compare-muted">{check.items.length} {files ? "file(s)" : "link(s)"} ready.</p>}
    {showProblems && <ul id="compare-input-problems" className="compare-problems" aria-label="Problems with the list">
      {check.problems.map((p, i) => <li key={i}>{p}</li>)}
    </ul>}
  </section>;
}
