import type { SettingsBlock, SuiteSettings } from "./types";
import { AUDIO_NAMES, FS_NAMES } from "./verdict";

export function SettingsPanel({ settings, block, loadError }: {
  settings: SuiteSettings | null;
  block: SettingsBlock | null;
  loadError: string | null
}) {
  const b = block ?? settings;
  const errors = Array.isArray(b?.errors) ? b.errors : [];
  return <section className="suite-settings" aria-label="Owner settings">
    <h3>Owner settings (read-only)</h3>

    <p className="suite-muted">
      {block !== null
        ? "Settings used by the latest completed run:"
        : "Settings at the last settings check:"}</p>

    {loadError && <p role="alert">{loadError}</p>}

    {settings && !settings.supported
      && <p>This VideoSuite version does not report owner settings (needs 0.4.0).</p>}

    {b
      && <>
        {b.path === null
          ? <p>No settings file path reported by VideoSuite.</p>
          : <p>Settings file: {b.path} ({b.loaded
            ? "loaded"
            : errors.length ? "could not be used; defaults in use" : "not found; defaults in use"})</p>}

        {errors.length > 0 && <ul>
          {errors.map((e, i) => <li key={i}>{e}</li>)}
        </ul>}
        <p>
          {b.applied_count} rule override(s) applied.</p>
      </>}

    {settings
      && <>
        <p className="suite-muted">Device facts are from the last settings check.</p>
        <table className="suite-table">
          <thead>
            <tr>
              {["Device", "USB file system", "Audio output", "Overrides"].map(h => <th key={h}>{h}</th>)}
            </tr>
          </thead>

          <tbody>
            {Object.entries(settings.devices).map(([id, d]) => <tr key={id}>
              <td>{d.label}</td>

              <td>
                {d.usb_filesystem
                  ? Object.hasOwn(FS_NAMES, d.usb_filesystem) ? FS_NAMES[d.usb_filesystem] : d.usb_filesystem
                  : "not set"}
              </td>

              <td>
                {d.audio_output
                  ? Object.hasOwn(AUDIO_NAMES, d.audio_output) ? AUDIO_NAMES[d.audio_output] : d.audio_output
                  : "not set"}
              </td>

              <td>
                {d.overrides.length
                  ? d.overrides.map(v => `${v.player}: ${v.rule} (${v.confidence}; ${v.reason})`).join("; ")
                  : "none"}
              </td>
            </tr>)}
          </tbody>
        </table>
      </>}

    {settings === null && block === null && !loadError && <p>Loading settings…</p>}

    <p className="suite-muted">
      Read-only. Device facts are from the last settings check. Use Check again to reload;
      the Dashboard may reuse its cached document for up to 60 seconds.
    </p>
  </section>;
}
