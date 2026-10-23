import supportedDevicesJson from "../compare/supported-devices.json";
import { EvidenceLegend } from "../suite/EvidenceLegend";
import type { DevicesDocument } from "../suite/types";
import { EVIDENCE_WORDS } from "../suite/types";
import { FS_NAMES, AUDIO_NAMES } from "../suite/verdict";
import "../suite/suite.css";
import "../compare/compare.css";

const own = (v: unknown, k: string): unknown =>
  v !== null && typeof v === "object" && Object.prototype.hasOwnProperty.call(v, k) ? (v as Record<string, unknown>)[k] : undefined;

/** Read-only information about the fixed supported devices (release order), from the live catalogue. */
export function DevicesInfo({ devices }: { devices: DevicesDocument | null }) {
  const found = devices === null || !Array.isArray(devices.devices) ? [] : supportedDevicesJson.devices
    .map((fixed) => ({ fixed, device: devices.devices.find((d) => d.id === fixed.id) ?? null }))
    .filter((x) => x.device !== null);
  return <div className="compare-screen">
    <section className="compare-section" aria-labelledby="devices-title">
      <h2 id="devices-title">Devices info</h2>
      <p>
        The list is fixed in this version: Sony Bravia 8 II (TV) first, then ASUS Vivobook Pro 16X (laptop), then Galaxy
        Tab S9 Ultra (tablet). Devices cannot be added, removed or reordered here. Calibration (TV) is where your own
        playback tests improve the TV's rules.
      </p>
      {devices === null && <p role="status">
        The device list is not available. Check that VideoSuite is installed, then reopen this tab.</p>}
    </section>
    {found.map(({ fixed, device }) => {
      const d = device!;
      const fs = own(d.settings, "usb_filesystem");
      const audio = own(d.settings, "audio_output");
      return <article key={fixed.id} data-device={fixed.id} className="compare-section" aria-labelledby={`device-${fixed.id}`}>
        <h3 id={`device-${fixed.id}`}>{fixed.label}</h3>
        <p className="compare-muted">{d.label}</p>
        <dl className="compare-facts">
          <dt>Rules version</dt><dd>{typeof d.rules_version === "number" ? d.rules_version : "unknown"}</dd>
          <dt>Your own tests</dt><dd>{d.verified_by_owner ? "Some rules are confirmed by your own tests (owner evidence)"
            : "Not yet confirmed by your own tests"}</dd>
          <dt>USB file system</dt>
          <dd>{typeof fs === "string" && Object.hasOwn(FS_NAMES, fs) ? `${FS_NAMES[fs]} (from your settings)` : "not set (unknown)"}</dd>
          <dt>Audio output</dt>
          <dd>{typeof audio === "string" && Object.hasOwn(AUDIO_NAMES, audio) ? `${AUDIO_NAMES[audio]} (from your settings)` : "not set (unknown)"}</dd>
        </dl>
        <h4>Players</h4>
        <ul className="compare-plain-list">
          {(Array.isArray(d.players) ? d.players : []).map((p) => <li key={p.id}>
            <strong>{p.label}</strong>{p.default ? " (default player)" : ""}
            <span className="compare-muted"> — rules by evidence: {EVIDENCE_WORDS.map((w) => {
              const n = own(p.rule_counts, w);
              return `${w} ${typeof n === "number" ? n : 0}`;
            }).join(", ")}</span>
          </li>)}
        </ul>
      </article>;
    })}
    <EvidenceLegend />
  </div>;
}
