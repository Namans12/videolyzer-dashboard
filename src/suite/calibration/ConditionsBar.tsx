import { useEffect, useId, useRef, useState } from "react";
import type { SuiteSettings } from "../types";
import { AUDIO_NAMES, FS_NAMES } from "../verdict";
import type { AudioOutput, UsbFilesystem } from "./types";
import type { useCalibration } from "./useCalibration";

type Calibration = Pick<ReturnType<typeof useCalibration>, "status" | "loading" | "mutationBusy" | "pendingAnswers" | "error" | "confirmSession">;
interface Props {
  calibration: Calibration;
  settings: SuiteSettings | null;
  deviceId?: string;
  /** Feed this value to GuidedCard.conditionsConfirmed. */
  onReadyChange: (ready: boolean) => void;
}

export function ConditionsBar({ calibration, settings, deviceId = "sony_bravia_8_ii", onReadyChange }: Props) {
  const device = settings && Object.hasOwn(settings.devices, deviceId) ? settings.devices[deviceId] : null;
  const audio: AudioOutput | null = device?.audio_output === "tv_speakers" || device?.audio_output === "soundbar" || device?.audio_output === "avr" ? device.audio_output : null;
  const filesystem: UsbFilesystem | null = device?.usb_filesystem === "fat32" || device?.usb_filesystem === "exfat" || device?.usb_filesystem === "ntfs" ? device.usb_filesystem : null;
  const usable = !!settings?.supported && settings.errors.length === 0 && !!device;
  // A changed settings view or saved session remounts the form, clearing its old confirmation.
  return <ConditionsForm key={JSON.stringify([calibration.status?.kit?.id, calibration.status?.session, deviceId, audio, filesystem, usable])}
    calibration={calibration} audio={audio} filesystem={filesystem} usable={usable} settings={settings} onReadyChange={onReadyChange} />;
}

function ConditionsForm({ calibration, audio, filesystem, usable, settings, onReadyChange }: {
  calibration: Calibration; audio: AudioOutput | null; filesystem: UsbFilesystem | null; usable: boolean; onReadyChange: (ready: boolean) => void;
  settings: SuiteSettings | null;
}) {
  const id = useId();
  const session = calibration.status?.session;
  const [software, setSoftware] = useState(() => (session?.tv_software ?? "").slice(0, 100));
  const [confirmed, setConfirmed] = useState(false);
  const [starting, setStarting] = useState(false);
  const inFlight = useRef(false);
  const mounted = useRef(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const busy = starting || calibration.loading || calibration.mutationBusy || calibration.pendingAnswers.length > 0;
  const same = !!session && session.tv_software === software.trim() && session.audio_output === audio && session.usb_filesystem === filesystem;
  const ready = usable && same && !busy && !!calibration.status?.kit;
  useEffect(() => { onReadyChange(ready); }, [onReadyChange, ready]);
  const canSubmit = usable && !!software.trim() && !!calibration.status?.kit && confirmed && !same && !busy;
  const submit = async () => {
    if (!canSubmit || inFlight.current) return;
    inFlight.current = true; setStarting(true);
    try { await calibration.confirmSession({ out: calibration.status!.kit!.out, tv_software: software.trim().slice(0, 100), audio_output: audio, usb_filesystem: filesystem }); }
    finally { inFlight.current = false; if (mounted.current) setStarting(false); }
  };
  return <section className="suite-section" aria-label="Calibration conditions">
    <h3>Calibration conditions</h3>
    <p>Audio output: {audio ? AUDIO_NAMES[audio] : "not set"} (from owner settings)</p>
    <p>USB file system: {filesystem ? FS_NAMES[filesystem] : "not set"} (from owner settings)</p>
    <p className="suite-muted">Change audio output or USB file system in your owner settings, then check settings again. Unknown conditions cannot become applied evidence.</p>
    {(!audio || !filesystem || !settings?.loaded) && <>
      <p>Open this settings file in a text editor: {settings?.path || "%LOCALAPPDATA%\\VideoComparator\\suite\\settings.json"}</p>
      <p>If it does not exist, create the folders and the file. Use this example for TV speakers and exFAT. If the file already exists, add or update these choices in its Bravia device block and keep your other settings.</p>
      <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{JSON.stringify({ schema: "videosuite-settings/1", devices: { sony_bravia_8_ii: { usb_filesystem: "exfat", audio_output: "tv_speakers" } } }, null, 2)}</pre>
      <p>Allowed values for audio output: tv_speakers | soundbar | avr (TV speakers, soundbar, AV receiver); USB file system: exfat | fat32 | ntfs.</p>
      <p>Choose the values for your setup, save the file, then press Check settings again.</p>
    </>}
    {!usable && <p>Load supported owner settings without errors before confirming conditions.</p>}
    <label htmlFor={`${id}-software`}>TV software</label>
    <input id={`${id}-software`} value={software} maxLength={100} aria-describedby={`${id}-count`} disabled={busy}
      onChange={e => { setSoftware(e.target.value.replace(/[\r\n\u0085\u2028\u2029]+/g, " ").slice(0, 100)); setConfirmed(false); }} />
    <p id={`${id}-count`} className="suite-muted">{software.length} / 100 characters</p>
    {session && <p>Confirmed session: {session.tv_software}; {session.audio_output ?? "unknown"}; {session.usb_filesystem ?? "unknown"}</p>}
    {session && !same && <p>Changing conditions starts a new session. Existing answers keep their recorded conditions; answers from older TV software need a fresh answer before apply.</p>}
    <label htmlFor={`${id}-confirm`}><input id={`${id}-confirm`} type="checkbox" checked={confirmed} disabled={busy || same}
      onChange={e => setConfirmed(e.target.checked)} />I confirm the TV software, audio output and USB file system shown above describe this playback session.</label>
    <button type="button" disabled={!canSubmit} onClick={() => void submit()}>{session ? "Start new session" : "Confirm conditions"}</button>
    {starting && <p role="status">Confirming conditions…</p>}
    {ready && <p role="status">Conditions confirmed. You can answer the tests.</p>}
    {calibration.error && <p role="alert">{calibration.error}</p>}
  </section>;
}
