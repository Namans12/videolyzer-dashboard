import { checkLang, checkPaths } from "./paths";
import type { FileChooserValue, FsChoice } from "./paths";
import type { DevicesDocument, SuiteSettings } from "./types";
import { FS_NAMES } from "./verdict";

export function FileChooser({ value, onChange, devices, settings, disabled }: {
  value: FileChooserValue;
  onChange: (v: FileChooserValue) => void;
  devices: DevicesDocument | null;
  settings: SuiteSettings | null;
  disabled: boolean
}) {
  const checked = checkPaths(value.text);
  const language = checkLang(value.lang);
  const problems = [...(value.text.trim() ? checked.problems : []), ...(language ? [language] : [])];
  const device = devices?.devices.find(d => d.id === value.device);
  const facts = Object.entries(settings?.devices ?? {}).filter(([, d]) => d.usb_filesystem);
  return <div className="suite-file-chooser">
    <p className="suite-muted">
      The browser cannot read files from your disk here: type or paste full paths
      (in Explorer: Shift + right-click a file, Copy as path).
    </p>

    <label htmlFor="suite-paths">Video files on this computer (one full path per line, up to 50)</label>
    <textarea
      id="suite-paths"
      rows={8}
      spellCheck={false}
      placeholder="D:\Movies\Film (2021) 2160p.mkv"
      value={value.text}
      disabled={disabled}
      onChange={e => onChange({ ...value, text: e.target.value })}
    />

    {checked.paths.length > 0 && <p>
      {checked.paths.length} file(s) ready.</p>}

    {problems.length > 0
      && <ul className="suite-problems">
        {problems.map((p, i) => <li key={i}>{p}</li>)}
      </ul>}

    <label htmlFor="suite-device">Device</label>
    <select
      id="suite-device"
      value={value.device}
      disabled={disabled}
      onChange={e => onChange({ ...value, device: e.target.value, player: "all" })}
    >
      <option value="all">All devices</option>
      {devices?.devices.map(d => <option key={d.id} value={d.id}>
        {d.label}
      </option>)}
    </select>

    <label htmlFor="suite-player">Player</label>
    <select
      id="suite-player"
      value={value.player}
      disabled={disabled || value.device === "all"}
      onChange={e => onChange({ ...value, player: e.target.value })}
    >
      <option value="all">All players</option>
      {device?.players.map(p => <option key={p.id} value={p.id}>
        {p.label}
        {p.default ? " (default)" : ""}
      </option>)}
    </select>

    <label htmlFor="suite-fs">USB file system</label>
    <select
      id="suite-fs"
      value={value.fs}
      disabled={disabled}
      onChange={e => onChange({ ...value, fs: e.target.value as FsChoice })}
    >
      <option value="settings">Owner settings (per device)</option>
      <option value="exfat">exFAT</option>
      <option value="fat32">FAT32</option>
      <option value="ntfs">NTFS</option>
    </select>

    {settings
      && (facts.length
        ? facts.map(([id, d]) => <p key={id} className="suite-muted">
          From your settings: {d.label}: {Object.hasOwn(FS_NAMES, d.usb_filesystem!)
          ? FS_NAMES[d.usb_filesystem!]
          : d.usb_filesystem}</p>)
        : <p className="suite-muted">Your settings file sets no USB file system; VideoSuite assumes none.</p>)}

    <label htmlFor="suite-lang">Audio and subtitle language</label>
    <input
      id="suite-lang"
      value={value.lang}
      disabled={disabled}
      onChange={e => onChange({ ...value, lang: e.target.value })}
    />
  </div>;
}
