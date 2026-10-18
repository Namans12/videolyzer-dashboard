import { useCallback, useEffect, useRef, useState } from "react";
import { errorSentence, getSettings, getVersion, SuiteApiError } from "../api";
import type { VersionState } from "../messages";
import { SuiteStatusBanner } from "../SuiteStatusBanner";
import type { SuiteSettings } from "../types";
import { ApplyPanel } from "./ApplyPanel";
import { ChecklistTable } from "./ChecklistTable";
import { ConditionsBar } from "./ConditionsBar";
import { GuidedCard } from "./GuidedCard";
import { KitPanel } from "./KitPanel";
import { CALIBRATION_UPDATE_TEXT, calibrationAvailable } from "./messages";
import { checkFolder } from "../paths";
import { own } from "./types";
import type { AudioOutput, Descriptor, StatusDocument, UsbFilesystem } from "./types";
import { useCalibration } from "./useCalibration";
import "../suite.css";
import "./calibration.css";

const KIT_KEY = "calibration_kit";
// The backend's kit id pattern (calibration_routes.KIT_QUERY_PATTERN); anything else is ignored.
const KIT_ID = /^[A-Za-z0-9_.-]{1,128}$/;
const NEW_KIT_TEXT = "Confirm the conditions for the new kit before previewing, applying or reporting.";
type Pair = { test: string; player: string };

function readStoredKit(): string | null {
  try { const id = localStorage.getItem(KIT_KEY); return id !== null && KIT_ID.test(id) ? id : null; }
  catch { return null; }
}
function writeStoredKit(id: string | null): void {
  try { if (id === null) localStorage.removeItem(KIT_KEY); else localStorage.setItem(KIT_KEY, id); }
  catch { /* not persisted */ }
}
// A built kit awaiting its first session: {id, out} only, so a reload can still start that session.
const PENDING_KEY = "calibration_pending_kit";
const PENDING_MAX_CHARS = 2048;
interface StoredPending { id: string; out: string }
function readPendingKit(): StoredPending | null {
  try {
    const raw = localStorage.getItem(PENDING_KEY);
    if (raw === null || raw.length > PENDING_MAX_CHARS) return null;
    const value: unknown = JSON.parse(raw);
    const id = own(value, "id"), out = own(value, "out");
    // The same checks as a typed output folder; a corrupt or foreign value is ignored.
    if (typeof id !== "string" || !KIT_ID.test(id) || typeof out !== "string" || checkFolder(out).folder !== out) return null;
    return { id, out };
  } catch { return null; }
}
function writePendingKit(kit: StoredPending | null): void {
  try { if (kit === null) localStorage.removeItem(PENDING_KEY); else localStorage.setItem(PENDING_KEY, JSON.stringify({ id: kit.id, out: kit.out })); }
  catch { /* not persisted */ }
}
const samePair = (a: Pair, b: Pair) => a.test === b.test && a.player === b.player;

interface LoadHandlers { version: (s: VersionState) => void; settings: (s: SuiteSettings | null, error: string | null) => void; done: () => void }
/** Version first; the owner settings view only when the installed suite can calibrate (no other requests on 0.3/0.4). */
function loadCalibrationStatus(refresh: boolean, signal: AbortSignal, on: LoadHandlers): void {
  const live = (fn: () => void) => { if (!signal.aborted) fn(); };
  void getVersion(refresh, signal).then(async info => {
    const state: VersionState = { kind: "loaded", info };
    live(() => on.version(state));
    if (signal.aborted || !calibrationAvailable(state)) return;
    await getSettings(signal, refresh).then(s => live(() => on.settings(s, null)), (err: unknown) => live(() => on.settings(null, errorSentence(err))));
  }).catch((err: unknown) => live(() => on.version({ kind: "failed", status: err instanceof SuiteApiError ? err.status : 0, detail: err instanceof SuiteApiError ? err.detail : "" })))
    .finally(() => live(on.done));
}

export function CalibrationScreen() {
  const [version, setVersion] = useState<VersionState>({ kind: "loading" });
  const [checking, setChecking] = useState(true);
  const [settings, setSettings] = useState<SuiteSettings | null>(null);
  const [settingsError, setSettingsError] = useState<string | null>(null);
  const checkRef = useRef<AbortController | null>(null);
  const checkingRef = useRef(true);
  // The workspace registers its calibration.settingsChanged here, so every settings check invalidates a loaded preview.
  const invalidateRef = useRef<(() => void) | null>(null);
  const registerInvalidate = useCallback((fn: (() => void) | null) => { invalidateRef.current = fn; }, []);
  const handlers = useCallback((): LoadHandlers => ({
    version: state => {
      setVersion(state);
      if (!calibrationAvailable(state)) { setSettings(null); setSettingsError(null); }
    },
    // A fetched document (or a failure, which means "unknown") replaces the settings; a preview made under the old ones is dropped.
    settings: (s, error) => { setSettings(s); setSettingsError(error); invalidateRef.current?.(); },
    done: () => { checkingRef.current = false; setChecking(false); },
  }), []);
  useEffect(() => {
    const ctrl = new AbortController();
    checkRef.current = ctrl;
    const timer = setTimeout(() => loadCalibrationStatus(false, ctrl.signal, handlers()), 0);
    return () => { clearTimeout(timer); ctrl.abort(); checkRef.current?.abort(); };
  }, [handlers]);
  /** The one settings re-check used by the banner's "Check again" and the workspace's "Check settings again". */
  const recheck = () => {
    if (checkingRef.current) return;
    invalidateRef.current?.();
    checkRef.current?.abort();
    const ctrl = new AbortController();
    checkRef.current = ctrl;
    checkingRef.current = true;
    setChecking(true);
    loadCalibrationStatus(true, ctrl.signal, handlers());
  };
  const available = calibrationAvailable(version);
  return <div className="suite-screen calibration-screen">
    <h2>Calibration (TV)</h2>
    <p className="suite-muted">Play the kit's test files on the TV, answer what you see, then apply the answers to your owner settings. VideoSuite stores every answer.</p>
    <SuiteStatusBanner state={version} checking={checking} onRecheck={recheck} />
    {version.kind === "loaded" && version.info.status === "ok" && !available
      && <div role="status" className="suite-banner suite-banner-warn calibration-gate">
        <p>{CALIBRATION_UPDATE_TEXT}</p>
        <p>The Suite tab keeps working with the installed VideoSuite.</p>
      </div>}
    {available && <CalibrationWorkspace settings={settings} settingsError={settingsError} checking={checking} onRecheck={recheck}
      onRegisterInvalidate={registerInvalidate} />}
  </div>;
}

interface PendingKit { id: string; out: string; descriptors: Descriptor[]; toolVersion: string; restored: boolean }
/** A freshly built kit is not in the store until its first session (R11), so its descriptor (or, after a reload,
 * just its id and output folder) stands in for the status until then. built_at/catalogue_version are never shown. */
function pendingStatus(kit: PendingKit, status: StatusDocument | null): StatusDocument {
  return { schema: "videosuite-calibrate-status/1", tool_version: kit.toolVersion, recovery_required: status?.recovery_required ?? false,
    kit: { id: kit.id, catalogue_version: 0, built_at: "", out: kit.out, descriptors: kit.descriptors },
    session: null, answers: [], summary: { done: 0, total: kit.descriptors.length, matches: 0, differs: 0 } };
}

const CHECKING_TEXT = "Checking owner settings… Answers, previews, apply, undo and reports wait until the check finishes.";
const UNKNOWN_SETTINGS_TEXT = "Owner settings are unknown. Check settings again before answering, previewing or applying.";
const SETTINGS_ERRORS_TEXT = "Owner settings have errors. Fix them before answering, previewing or applying.";
const DIFFERENT_SETTINGS_TEXT = "The owner settings differ from the confirmed session. Confirm the conditions again before answering, previewing or applying.";
const DEVICE_ID = "sony_bravia_8_ii";
/** The same reading of the settings view as ConditionsBar (prototype-safe lookup, known values only). */
function settingsConditions(settings: SuiteSettings | null): { usable: boolean; audio: AudioOutput | null; filesystem: UsbFilesystem | null } {
  const device = settings && Object.hasOwn(settings.devices, DEVICE_ID) ? settings.devices[DEVICE_ID] : null;
  const a = device?.audio_output, f = device?.usb_filesystem;
  return { usable: !!settings?.supported && settings.errors.length === 0 && !!device,
    audio: a === "tv_speakers" || a === "soundbar" || a === "avr" ? a : null,
    filesystem: f === "fat32" || f === "exfat" || f === "ntfs" ? f : null };
}

function CalibrationWorkspace({ settings, settingsError, checking, onRecheck, onRegisterInvalidate }: {
  settings: SuiteSettings | null; settingsError: string | null; checking: boolean; onRecheck: () => void;
  onRegisterInvalidate: (fn: (() => void) | null) => void;
}) {
  const [kitId, setKitId] = useState<string | null>(readStoredKit);
  const calibration = useCalibration(kitId);
  const [selected, setSelected] = useState<Pair | null>(null);
  // Answers stay blocked until ConditionsBar reports the current conditions match the saved session.
  const [conditionsConfirmed, setConditionsConfirmed] = useState(false);
  const [remembered, setRemembered] = useState<StoredPending | null>(readPendingKit);
  const { settingsChanged } = calibration;
  useEffect(() => { onRegisterInvalidate(settingsChanged); return () => onRegisterInvalidate(null); }, [onRegisterInvalidate, settingsChanged]);
  const activeKit = calibration.status?.kit?.id ?? null;
  useEffect(() => {
    if (activeKit === null) return;
    writeStoredKit(activeKit);
    // The remembered prepared kit now has a session (or the store reports it): forget it.
    if (readPendingKit()?.id === activeKit) writePendingKit(null);
  }, [activeKit]);
  const built = calibration.buildDocument;
  const builtPending = built !== null && built.kit_id !== activeKit ? built : null;
  const builtId = builtPending?.kit_id ?? null, builtOut = builtPending?.out ?? null;
  useEffect(() => { if (builtId !== null && builtOut !== null) writePendingKit({ id: builtId, out: builtOut }); }, [builtId, builtOut]);
  // A new build supersedes the remembered kit; a remembered kit that is now active is resolved.
  if (remembered !== null && (built !== null || remembered.id === activeKit)) setRemembered(null);
  const restored = remembered !== null && built === null && remembered.id !== activeKit && !calibration.loading ? remembered : null;
  const pending: PendingKit | null = builtPending
    ? { id: builtPending.kit_id, out: builtPending.out, descriptors: builtPending.descriptors, toolVersion: builtPending.tool_version, restored: false }
    : restored ? { id: restored.id, out: restored.out, descriptors: [], toolVersion: calibration.status?.tool_version ?? "", restored: true } : null;
  const base = pending ? { ...calibration, status: pendingStatus(pending, calibration.status), answers: [] } : calibration;
  // While settings are being checked every component sees a loading calibration: inputs, answers and actions wait.
  const view = { ...base, loading: base.loading || checking };
  const current = settingsConditions(settings);
  const session = view.status?.session ?? null;
  const blockReason = pending ? NEW_KIT_TEXT : checking ? CHECKING_TEXT
    : settings !== null && settings.errors.length > 0 ? SETTINGS_ERRORS_TEXT
    : settingsError !== null || !current.usable ? UNKNOWN_SETTINGS_TEXT
    : session !== null && (session.audio_output !== current.audio || session.usb_filesystem !== current.filesystem) ? DIFFERENT_SETTINGS_TEXT
    : null;
  const descriptors = view.status?.kit?.descriptors ?? [];
  const valid = selected !== null && descriptors.some(d => samePair(d, selected));
  const fallback = descriptors.find(d => !view.answers.some(a => samePair(a, d))) ?? descriptors[0];
  // Pin the first unanswered test once, so answering it does not jump to the next one.
  if (!valid && fallback !== undefined) setSelected({ test: fallback.test, player: fallback.player });
  const selection = valid ? selected : fallback !== undefined ? { test: fallback.test, player: fallback.player } : null;
  const onSelect = (d: Descriptor) => setSelected({ test: d.test, player: d.player });
  const missing = kitId !== null && calibration.status === null && !calibration.loading && calibration.error !== null;
  const showLatest = () => { writeStoredKit(null); setKitId(null); };
  return <>
    <KitPanel calibration={calibration} />
    {missing && <div role="alert" className="suite-banner suite-banner-error calibration-kit-note">
      <p>The remembered kit {kitId} could not be loaded.</p>
      <button type="button" onClick={showLatest}>Show the latest kit</button>
    </div>}
    {pending
      ? <div role="status" className="suite-banner suite-banner-info calibration-kit-note">
        <p>New kit {pending.id} is ready in {pending.out}. Confirm the conditions below to start its first session; the previous kit's answers stay saved.</p>
        {pending.restored && <p>Its tests appear once the session starts.</p>}
      </div>
      : view.status?.kit && <p className="suite-muted">Kit {view.status.kit.id} in {view.status.kit.out}</p>}
    {!view.status?.kit && !calibration.loading && !missing && <p>No calibration kit yet: prepare one above, then confirm the conditions.</p>}
    {settingsError && <p role="alert">Owner settings could not be loaded: {settingsError}</p>}
    <ConditionsBar calibration={view} settings={settings} onReadyChange={setConditionsConfirmed} />
    <button type="button" disabled={checking} onClick={onRecheck}>Check settings again</button>
    <div className="calibration-columns">
      <ChecklistTable descriptors={descriptors} answers={view.answers} selected={selection} onSelect={onSelect} />
      <GuidedCard calibration={view} selected={selection} onSelect={onSelect} conditionsConfirmed={conditionsConfirmed && blockReason === null} />
    </div>
    <ApplyPanel calibration={view} disabledReason={blockReason} />
  </>;
}
