import { useCallback, useEffect, useRef, useState } from "react";
import { errorSentence, postScore } from "./api";
import { suiteReady } from "./messages";
import type { VersionState } from "./messages";
import { checkLang, checkPaths, DEFAULT_CHOOSER, toDeviceOptions } from "./paths";
import { chooserBlockReason, isScoreDocument, loadSuiteStatus } from "./screen";
import type { DevicesDocument, SettingsBlock, SuiteSettings } from "./types";
import { useJob } from "./useJob";
import { SuiteStatusBanner } from "./SuiteStatusBanner";
import { EvidenceLegend } from "./EvidenceLegend";
import { FileChooser } from "./FileChooser";
import { JobMessage } from "./JobMessage";
import { ScoreResults } from "./ScoreResults";
import { ComparePanel } from "./ComparePanel";
import { SettingsPanel } from "./SettingsPanel";
import "./suite.css";

export function SuiteScreen() {
  const [version, setVersion] = useState<VersionState>({ kind: "loading" });
  const [checking, setChecking] = useState(true);
  const [devices, setDevices] = useState<DevicesDocument | null>(null);
  const [settings, setSettings] = useState<SuiteSettings | null>(null);
  const [settingsError, setSettingsError] = useState<string | null>(null);
  const [chooser, setChooser] = useState(DEFAULT_CHOOSER);
  const [scoreJobId, setScoreJobId] = useState<string | null>(null);
  const [startingScore, setStartingScore] = useState(false);
  const [scoreStartError, setScoreStartError] = useState<string | null>(null);
  const [latestRun, setLatestRun] = useState<{ key: string; block: SettingsBlock } | null>(null);
  const checkRef = useRef<AbortController | null>(null);
  const checkingRef = useRef(true);
  const startingRef = useRef(false);
  const genRef = useRef(0);
  const mountedRef = useRef(false);
  const publishedRef = useRef(new Set<string>());
  const score = useJob(scoreJobId);
  const ready = suiteReady(version);
  const check = checkPaths(chooser.text);
  const handlers = useCallback(() => ({
    version: (state: VersionState) => {
      setVersion(state);
      if (!suiteReady(state)) {
        setDevices(null);
        setSettings(null);
        setSettingsError(null);
      }
    },
    devices: setDevices,
    settings: (s: SuiteSettings | null, error: string | null) => {
      setSettings(s);
      setSettingsError(error);
    },
    done: () => {
      checkingRef.current = false;
      setChecking(false);
    }
  }), []);
  useEffect(() => {
    mountedRef.current = true;
    const ctrl = new AbortController();
    checkRef.current = ctrl;
    const timer = setTimeout(() => loadSuiteStatus(false, ctrl.signal, handlers()), 0);
    return () => {
      mountedRef.current = false;
      clearTimeout(timer);
      ctrl.abort();
      checkRef.current?.abort();
    };
  }, [handlers]);
  const recheck = () => {
    if (checkingRef.current) return;
    checkRef.current?.abort();
    const ctrl = new AbortController();
    checkRef.current = ctrl;
    checkingRef.current = true;
    setChecking(true);
    loadSuiteStatus(true, ctrl.signal, handlers());
  };
  const publishSettings = useCallback((block: SettingsBlock, key: string) => {
    if (publishedRef.current.has(key)) return;
    publishedRef.current.add(key);
    setLatestRun({ key, block });
  }, []);
  const scoreSettings = scoreJobId !== null && score.status === "done" && isScoreDocument(score.results)
    ? score.results.settings ?? null
    : null;
  useEffect(() => {
    if (scoreJobId === null || scoreSettings === null) return;
    const key = `score:${scoreJobId}`;
    const t = setTimeout(() => publishSettings(scoreSettings, key), 0);
    return () => clearTimeout(t);
  }, [scoreJobId, scoreSettings, publishSettings]);
  const scoreDisabled = !ready || check.problems.length > 0
    || checkLang(chooser.lang) !== null || startingScore
    || score.status === "running";
  const startScore = () => {
    if (startingRef.current || scoreDisabled) return;
    startingRef.current = true;
    const gen = ++genRef.current;
    const body = { paths: [...check.paths], ...toDeviceOptions(chooser) };
    setStartingScore(true);
    setScoreStartError(null);
    setScoreJobId(null);
    postScore(body).then(r => {
      if (mountedRef.current && gen === genRef.current) setScoreJobId(r.job_id);
    })
      .catch((err: unknown) => {
        if (mountedRef.current && gen === genRef.current) setScoreStartError(errorSentence(err));
      })
      .finally(() => {
        if (mountedRef.current && gen === genRef.current) {
          startingRef.current = false;
          setStartingScore(false);
        }
      });
  };
  return <div className="suite-screen">
    <SuiteStatusBanner state={version} checking={checking} onRecheck={recheck} />
    <EvidenceLegend />

    <section className="suite-section" aria-label="Choose files">
      <h2>1. Choose files</h2>
      <FileChooser
        value={chooser}
        onChange={setChooser}
        devices={devices}
        settings={settings}
        disabled={!ready}
      />
    </section>

    <section className="suite-section" aria-label="Device check">
      <h2>2. Device check</h2>
      <button type="button" disabled={scoreDisabled} onClick={startScore}>
        {startingScore ? "Starting…" : "Score files"}
      </button>

      {scoreStartError && <p role="alert">{scoreStartError}</p>}
      <JobMessage label="Score" job={score} />
      {score.status === "running"
        && <button type="button" disabled={score.cancelRequested} onClick={() => void score.cancel()}>Cancel</button>}

      {score.status === "done" && isScoreDocument(score.results)
        && <ScoreResults records={score.results.records} devices={devices} />}
    </section>

    <section className="suite-section" aria-label="Compare and rank">
      <h2>3. Compare and rank</h2>
      <ComparePanel
        paths={check.paths}
        options={toDeviceOptions(chooser)}
        disabledReason={chooserBlockReason(chooser.text, chooser.lang, ready)}
        devices={devices}
        onSettingsBlock={publishSettings}
      />
    </section>

    {ready
      && <SettingsPanel
        settings={settings}
        block={latestRun?.block ?? null}
        loadError={settingsError}
      />}
  </div>;
}
