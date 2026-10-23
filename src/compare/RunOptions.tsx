import {
  AUDIO_LABELS, chooseDevice, DEPTH_LABELS, FS_LABELS, FULL_NEEDS_FILES_TEXT, RESULTS_LABELS, supportedDevices,
} from "./types";
import type { AudioOutput, CompareForm, Depth, FsChoice, ResultsChoice } from "./types";
import type { DevicesDocument } from "../suite/types";

const RESULTS: { value: ResultsChoice; hint: string }[] = [
  { value: "overall", hint: "the device-neutral order of the versions; no device rules are used" },
  { value: "device", hint: "what plays and looks best on the device and player you choose" },
  { value: "both", hint: "both answers, shown separately" },
];
const DEPTHS: { value: Depth; hint: string }[] = [
  { value: "quick", hint: "Metadata estimate, picture not measured. Reads the file facts only, so it is fast." },
  { value: "full", hint: "Measures the picture: an accurate comparison of every pair of the readable files. This can take a long time." },
];

/** Steps 2–3: results, depth, device, player and conditions. Only the three fixed devices, in release order. */
export function RunOptions({ form, onChange, devices, disabled }: {
  form: CompareForm;
  onChange: (next: CompareForm) => void;
  devices: DevicesDocument | null;
  disabled: boolean;
}) {
  const overall = form.results === "overall";
  const magnets = form.inputKind === "magnets";
  const options = supportedDevices(devices);
  const current = options.find((d) => d.id === form.device) ?? null;
  const primary = form.primaryPlayer || current?.defaultPlayer || "";
  const deviceDisabled = disabled || overall;

  return <section className="compare-section" aria-labelledby="compare-options-title">
    <h2 id="compare-options-title">2. What do you want to know?</h2>
    <div role="radiogroup" aria-label="Results" className="compare-choices compare-choices-column">
      {RESULTS.map(({ value, hint }) => <label key={value} className="compare-choice">
        <input type="radio" name="compare-results" value={value} checked={form.results === value} disabled={disabled}
               onChange={() => onChange({ ...form, results: value })} />
        <span><strong>{RESULTS_LABELS[value]}</strong> <span className="compare-muted">— {hint}</span></span>
      </label>)}
    </div>

    <h3>How deep?</h3>
    <div role="radiogroup" aria-label="Depth" className="compare-choices compare-choices-column">
      {DEPTHS.map(({ value, hint }) => <label key={value} className="compare-choice">
        <input type="radio" name="compare-depth" value={value} checked={form.depth === value}
               disabled={disabled || (magnets && value === "full")}
               onChange={() => onChange({ ...form, depth: value })} />
        <span><strong>{DEPTH_LABELS[value]}</strong> <span className="compare-muted">— {hint}</span></span>
      </label>)}
      {magnets && <p className="compare-note">{FULL_NEEDS_FILES_TEXT}</p>}
    </div>

    <h3>Device</h3>
    {overall && <p className="compare-muted">Best overall uses no device, player or playback conditions.</p>}
    {options.length === 0
      ? <p className="compare-muted">The device list is not available yet.</p>
      : <div role="radiogroup" aria-label="Device" className="compare-choices compare-choices-column">
        {options.map((d) => <label key={d.id} className="compare-choice">
          <input type="radio" name="compare-device" value={d.id} checked={form.device === d.id} disabled={deviceDisabled}
                 onChange={() => onChange(chooseDevice(form, d.id, devices))} />
          <span><strong>{d.label}</strong> <span className="compare-muted">{d.catalogueLabel}</span></span>
        </label>)}
      </div>}

    <div className="compare-grid">
      <div>
        <label htmlFor="compare-player">Player</label>
        <select id="compare-player" value={primary} disabled={deviceDisabled || current === null}
                onChange={(e) => onChange({ ...form, primaryPlayer: e.target.value })}>
          {current?.players.map((p) => <option key={p.id} value={p.id}>{p.label}{p.default ? " (default)" : ""}</option>)}
        </select>
      </div>
      <label className="compare-check">
        <input type="checkbox" checked={form.includeAlternatives} disabled={deviceDisabled}
               onChange={(e) => onChange({ ...form, includeAlternatives: e.target.checked })} />
        Also show the other players on this device as alternatives
      </label>
      <div>
        <label htmlFor="compare-fs">USB file system</label>
        <select id="compare-fs" value={form.fs ?? ""} disabled={deviceDisabled}
                onChange={(e) => onChange({ ...form, fs: e.target.value === "" ? null : e.target.value as FsChoice })}>
          <option value="">Your saved setting (unknown if not set)</option>
          {(Object.keys(FS_LABELS) as FsChoice[]).map((k) => <option key={k} value={k}>{FS_LABELS[k]}</option>)}
        </select>
      </div>
      <div>
        <label htmlFor="compare-audio">Audio output</label>
        <select id="compare-audio" value={form.audioOutput ?? ""} disabled={deviceDisabled}
                onChange={(e) => onChange({ ...form, audioOutput: e.target.value === "" ? null : e.target.value as AudioOutput })}>
          <option value="">Your saved setting (unknown if not set)</option>
          {(Object.keys(AUDIO_LABELS) as AudioOutput[]).map((k) => <option key={k} value={k}>{AUDIO_LABELS[k]}</option>)}
        </select>
      </div>
      <div>
        <label htmlFor="compare-lang">Audio and subtitle language</label>
        <input id="compare-lang" value={form.lang} disabled={disabled} spellCheck={false}
               onChange={(e) => onChange({ ...form, lang: e.target.value })} />
      </div>
    </div>
    <p className="compare-muted">
      A choice here applies to this run only. It does not change your saved settings and is not recorded as a test on
      your device.
    </p>
  </section>;
}
