import { useCallback, useEffect, useRef, useState } from "react";
import { errorSentence, postCompare, postRank } from "./api";
import { isCompareDocument, isRankDocument, MAX_EVENTS_SHOWN, shouldRank } from "./compare";
import type { LastRun } from "./compare";
import { checkFolder } from "./paths";
import type { DevicesDocument, DeviceOptions, SettingsBlock } from "./types";
import {
  compareRunText,
  estimateText,
  LOG_SUMMARY_TEXT,
  pairProgressText,
  pairsDoneText,
  SATURATED_TEXT,
  summarizeCompareEvents
} from "./messages";
import { useJob } from "./useJob";
import { JobMessage } from "./JobMessage";
import { RankResult } from "./RankResult";

export function ComparePanel({ paths, options, disabledReason, devices, onSettingsBlock }: {
  paths: string[];
  options: DeviceOptions;
  disabledReason: string | null;
  devices: DevicesDocument | null;
  onSettingsBlock: (block: SettingsBlock, key: string) => void
}) {
  const [mode, setMode] = useState<"fast" | "accurate">("fast");
  const [top, setTop] = useState(4);
  const [out, setOut] = useState("");
  const [compareJobId, setCompareJobId] = useState<string | null>(null);
  const [rankJobId, setRankJobId] = useState<string | null>(null);
  const [lastRun, setLastRun] = useState<LastRun | null>(null);
  const [problems, setProblems] = useState<string[]>([]);
  const [startError, setStartError] = useState<string | null>(null);
  const [rankStartError, setRankStartError] = useState<string | null>(null);
  const [startingCompare, setStartingCompare] = useState(false);
  const [startingRank, setStartingRank] = useState(false);
  const startingRef = useRef(false);
  const startingRankRef = useRef(false);
  const genRef = useRef(0);
  const mountedRef = useRef(false);
  const rankedForRef = useRef<string | null>(null);
  const publishedRef = useRef(new Set<string>());
  const compare = useJob(compareJobId);
  const rank = useJob(rankJobId);
  const rankDue = shouldRank(compare);
  const compareSettings = compareJobId !== null && isCompareDocument(compare.results)
    && compare.status !== "running"
    ? compare.results.settings ?? null
    : null;
  const rankSettings = rankJobId !== null && rank.status === "done" && isRankDocument(rank.results)
    ? rank.results.settings ?? null
    : null;
  const handoff = rankDue && rankJobId === null && rankStartError === null;
  const busy = startingCompare || startingRank || compare.status === "running" || rank.status === "running"
    || handoff;
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);
  const startRank = useCallback((run: LastRun, forCompare: string) => {
    if (startingRankRef.current) return;
    startingRankRef.current = true;
    rankedForRef.current = forCompare;
    const gen = genRef.current;
    setStartingRank(true);
    setRankStartError(null);
    postRank({ paths: run.paths, reports_dir: run.outDir, ...run.options })
      .then(r => {
        if (mountedRef.current && gen === genRef.current) setRankJobId(r.job_id);
      })
      .catch((err: unknown) => {
        if (mountedRef.current && gen === genRef.current) setRankStartError(errorSentence(err));
      })
      .finally(() => {
        startingRankRef.current = false;
        if (mountedRef.current && gen === genRef.current) setStartingRank(false);
      });
  }, []);
  useEffect(() => {
    if (!rankDue || lastRun === null || compareJobId === null || rankJobId !== null
      || rankStartError !== null
      || rankedForRef.current === compareJobId) return;
    const t = setTimeout(() => {
      if (rankedForRef.current !== compareJobId) startRank(lastRun, compareJobId);
    }, 0);
    return () => clearTimeout(t);
  }, [rankDue, compareJobId, lastRun, rankJobId, rankStartError, startRank]);
  useEffect(() => {
    if (compareJobId === null || compareSettings === null) return;
    const key = `compare:${compareJobId}`;
    if (publishedRef.current.has(key)) return;
    const t = setTimeout(() => {
      if (publishedRef.current.has(key)) return;
      publishedRef.current.add(key);
      onSettingsBlock(compareSettings, key);
    }, 0);
    return () => clearTimeout(t);
  }, [compareJobId, compareSettings, onSettingsBlock]);
  useEffect(() => {
    if (rankJobId === null || rankSettings === null) return;
    const key = `rank:${rankJobId}`;
    if (publishedRef.current.has(key)) return;
    const t = setTimeout(() => {
      if (publishedRef.current.has(key)) return;
      publishedRef.current.add(key);
      onSettingsBlock(rankSettings, key);
    }, 0);
    return () => clearTimeout(t);
  }, [rankJobId, rankSettings, onSettingsBlock]);
  const start = (saved?: LastRun) => {
    if (startingRef.current || busy || disabledReason !== null) return;
    const checked = checkFolder(saved?.outDir ?? out);
    const k = saved?.top ?? top;
    const files = saved?.paths ?? paths;
    const invalid = [
      ...(files.length < 2 ? ["Choose at least two video files to compare."] : []),
      ...(checked.problem ? [checked.problem] : []),
      ...(!Number.isInteger(k) || k < 1 || k > 6 ? ["Top must be a whole number from 1 to 6."] : [])
    ];
    setProblems(invalid);
    if (invalid.length || checked.folder === null) return;
    startingRef.current = true;
    const gen = ++genRef.current;
    const run: LastRun = {
      paths: [...files],
      outDir: checked.folder,
      mode: saved?.mode ?? mode,
      top: k,
      options: { ...(saved?.options ?? options) }
    };
    setStartingCompare(true);
    setStartError(null);
    setRankStartError(null);
    setRankJobId(null);
    setCompareJobId(null);
    setLastRun(run);
    postCompare({ paths: run.paths, out_dir: run.outDir, mode: run.mode, top: run.top })
      .then(r => {
        if (mountedRef.current && gen === genRef.current) setCompareJobId(r.job_id);
      })
      .catch((err: unknown) => {
        if (mountedRef.current && gen === genRef.current) setStartError(errorSentence(err));
      })
      .finally(() => {
        if (mountedRef.current && gen === genRef.current) {
          startingRef.current = false;
          setStartingCompare(false);
        }
      });
  };
  const sum = summarizeCompareEvents(compare.events);
  return <div className="suite-compare">
    <label htmlFor="suite-mode">Comparison mode</label>
    <select
      id="suite-mode"
      value={mode}
      onChange={e => setMode(e.target.value as "fast" | "accurate")}
    >
      <option value="fast">Fast</option>
      <option value="accurate">Accurate (much slower)</option>
    </select>

    <label htmlFor="suite-top">Files kept per list (top K, 1-6)</label>
    <input
      id="suite-top"
      type="number"
      min={1}
      max={6}
      value={top}
      onChange={e => setTop(Number(e.target.value))}
    />

    <label htmlFor="suite-out">Folder for the comparison reports</label>
    <input id="suite-out" value={out} onChange={e => setOut(e.target.value)} />

    <p>{compareRunText(paths.length, top)}</p>
    {disabledReason && <p>{disabledReason}</p>}

    {problems.length > 0
      && <ul className="suite-problems">
        {problems.map(p => <li key={p}>{p}</li>)}
      </ul>}

    <button
      type="button"
      disabled={disabledReason !== null || busy}
      onClick={() => start()}
    >
      {startingCompare ? "Starting the comparison…" : "Compare and rank"}
    </button>

    {startError && <p role="alert">{startError}</p>}
    {startingRank && <p>Starting the ranking…</p>}

    <JobMessage label="Comparison" job={compare} />

    {compare.status === "running"
      && <>
        {sum.estimate && <p>{estimateText(sum.estimate, sum.estimate.mode)}</p>}

        {sum.saturated
          ? <p>{SATURATED_TEXT}</p>
          : sum.current
          && <>
            <p>{pairProgressText(sum.current)}</p>
            {sum.current.percent !== null
              && <progress
                max={100}
                value={sum.current.percent}
                aria-label="Current pair progress"
              />}
          </>}

        <p>At least {pairsDoneText(sum)}</p>
        <button type="button" disabled={compare.cancelRequested} onClick={() => void compare.cancel()}>Cancel comparison</button>

        <details className="suite-log">
          <summary>{LOG_SUMMARY_TEXT}</summary>
          <pre>
            {compare.events.slice(-MAX_EVENTS_SHOWN).map(e => e.msg).join("\n")}
          </pre>
        </details>
      </>}

    {compare.status === "cancelled" && lastRun
      && <button
        type="button"
        disabled={busy || disabledReason !== null}
        onClick={() => start(lastRun)}
      >Run the comparison again</button>}

    {compare.status !== "running" && isCompareDocument(compare.results)
      && <>
        <p>{estimateText(compare.results.estimate, compare.results.mode)}</p>

        <ul>
          {compare.results.pairs.map((p, i) => p.report === null && <li key={`${i}-${p.a}-${p.b}`}>
            Failed pair: {p.a} vs {p.b}
          </li>)}
          {compare.results.dropped.map((d, i) => <li key={i}>Not compared: {d.file}: {d.reason}</li>)}
        </ul>
      </>}

    {rankStartError
      && <>
        <p role="alert">{rankStartError}</p>
        {lastRun && compareJobId
          && <button
            type="button"
            disabled={startingRank}
            onClick={() => startRank(lastRun, compareJobId)}
          >Retry ranking</button>}
      </>}

    <JobMessage label="Ranking" job={rank} />
    {rank.status === "running"
      && <button type="button" disabled={rank.cancelRequested} onClick={() => void rank.cancel()}>Cancel ranking</button>}

    {rank.status === "done" && isRankDocument(rank.results)
      && <RankResult doc={rank.results} devices={devices} />}
  </div>;
}
