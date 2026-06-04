import React, {
  type ChangeEvent,
  type DragEvent,
  useRef,
  useState,
  useEffect
} from "react";
import axios from "axios";
import type { VideoData } from "./types";
import { BRAVIA_8_II_SPEC } from "./types";
import "./App.css";
import Switch from "./Switch";

// ── Constants ────────────────────────────────────────────────────────────────
const VIDEO_EXTS = [".mkv", ".mp4", ".ts", ".m2ts", ".hevc", ".h265"];

const API = "http://127.0.0.1:8000";

const DV_HIERARCHY = [
  { label: "Profile 7 FEL", verdict: "Best Source",    cls: "r1", notes: "Full dual-layer DV — but EL is WASTED on Bravia 8 II. Convert to P8.1 MP4 for this TV." },
  { label: "Profile 7 MEL", verdict: "Excellent", cls: "r2", notes: "Thin EL stub — RPU accessible via Just Player on some Sony TVs. Better than FEL for this TV." },
  { label: "Profile 8.1",   verdict: "Best for TV", cls: "r3", notes: "Single-layer DV, HDR10-compat base — 100% utilized on Bravia 8 II. Ideal target in MP4." },
  { label: "Profile 8.4",   verdict: "Very Good", cls: "r3", notes: "Single-layer DV on HLG base." },
  { label: "Profile 5",     verdict: "Very Good", cls: "r3", notes: "Single-layer streaming DV — no HDR10 fallback." },
  { label: "Profile 8.2",   verdict: "Good",      cls: "r5", notes: "Single-layer DV, SDR-compat base." },
  { label: "Profile 4",     verdict: "Limited",   cls: "r6", notes: "Older dual-layer — limited consumer support." },
];

const HDR_HIERARCHY = [
  { label: "HDR10+", verdict: "Falls to HDR10", cls: "r9", notes: "Sony Bravia does not support HDR10+. File plays as HDR10 — no dynamic metadata benefit on this TV." },
  { label: "HDR10",  verdict: "Decent",    cls: "r9",  notes: "Static metadata HDR." },
  { label: "HLG",    verdict: "Basic",     cls: "r10", notes: "Broadcast HDR." },
  { label: "SDR",    verdict: "Worst",     cls: "r11", notes: "No HDR." },
];

const FILE_COLORS = ["#ffd700", "#2997ff", "#30d158", "#ff9f0a", "#bf5af2"];

// ── Helpers ──────────────────────────────────────────────────────────────────
// Helper removed — no longer used after unifying analysis flow.

function cleanFileName(name: string): string {
  return name.replace(/^[0-9a-f]{32}_/i, "");
}

class ResultErrorBoundary extends React.Component<
  { children: React.ReactNode },
  { hasError: boolean }
> {
  state = { hasError: false };

  static getDerivedStateFromError() {
    return { hasError: true };
  }

  render() {
    if (this.state.hasError) {
      return <div className="error-box">A result card failed to render — other results are unaffected.</div>;
    }
    return this.props.children;
  }
}

function scoreColor(score: number) {
  if (score >= 70) return "#30d158";
  if (score >= 40) return "#ff9f0a";
  return "#ff453a";
}

function getVerdict(item: VideoData, isBest: boolean) {
  if (isBest) return "BEST CHOICE";
  if ((item.tv_score ?? 0) >= 75 && (item.confidence_score ?? 0) >= 60) return "GREAT";
  if ((item.score ?? 0) < 15) return "AVOID";
  if ((item.confidence_score ?? 0) < 40) return "LOW CONFIDENCE";
  if ((item.tv_score ?? 0) >= 30) return "COMPARABLE";
  return "OK";
}

function getToolStatusLabel(status: string) {
  if (status === "partial")   return "Bounded";
  if (status === "ok")        return "Ready";
  if (status === "error")     return "Issue";
  if (status === "skipped")   return "Skipped";
  return "Missing";
}

// ── Radar chart ──────────────────────────────────────────────────────────────
function RadarChart({ items, size = 280 }: { items: VideoData[]; size?: number }) {
  const N      = 5;
  const cx     = size / 2;
  const cy     = size / 2;
  const r      = (size / 2) * 0.62;
  const labels = ["Quality", "TV Score", "Bitrate", "Audio", "Confidence"];
  const [animated, setAnimated] = React.useState(false);

  React.useEffect(() => {
    const t = setTimeout(() => setAnimated(true), 80);
    return () => clearTimeout(t);
  }, []);

  const angle = (i: number) => (Math.PI * 2 * i) / N - Math.PI / 2;
  const pt    = (i: number, f: number) => ({
    x: cx + r * f * Math.cos(angle(i)),
    y: cy + r * f * Math.sin(angle(i)),
  });
  const polyPts = (fracs: number[]) =>
    fracs
      .map((f, i) => pt(i, Math.min(Math.max(animated ? f : 0, 0), 1)))
      .map((p) => `${p.x.toFixed(2)},${p.y.toFixed(2)}`)
      .join(" ");

  const normalize = (item: VideoData): number[] => [
    (item.score ?? 0) / 100,
    (item.tv_score ?? 0) / 100,
    Math.min((item.bitrate_mbps ?? 0) / 80, 1),
    (item.audio_score ?? 0) / 10,
    (item.confidence_score ?? 0) / 100,
  ];

  const gridLevels = [0.25, 0.5, 0.75, 1];

  // Label position nudge so text doesn't clip
  const labelOffset = (i: number) => {
    const p = pt(i, 1.32);
    return { x: p.x, y: p.y };
  };

  return (
    <svg
      width={size} height={size}
      viewBox={`0 0 ${size} ${size}`}
      style={{ overflow: "visible" }}
    >
      <defs>
        {FILE_COLORS.map((color, fi) => (
          <radialGradient key={fi} id={`radar-glow-${fi}`} cx="50%" cy="50%" r="50%">
            <stop offset="0%"   stopColor={color} stopOpacity="0.35" />
            <stop offset="100%" stopColor={color} stopOpacity="0.04" />
          </radialGradient>
        ))}
      </defs>

      {/* Grid rings */}
      {gridLevels.map((level) => (
        <polygon
          key={level}
          className="radar-grid-ring"
          points={Array.from({ length: N }, (_, i) => {
            const p = pt(i, level);
            return `${p.x.toFixed(2)},${p.y.toFixed(2)}`;
          }).join(" ")}
          fill="none"
          stroke={level === 1
            ? "rgba(255,255,255,0.14)"
            : "rgba(255,255,255,0.06)"}
          strokeWidth={level === 1 ? 1.5 : 1}
        />
      ))}

      {/* Axis lines */}
      {Array.from({ length: N }, (_, i) => {
        const p = pt(i, 1);
        return (
          <line key={i}
            x1={cx} y1={cy}
            x2={p.x.toFixed(2)} y2={p.y.toFixed(2)}
            stroke="rgba(255,255,255,0.08)" strokeWidth="1"
          />
        );
      })}

      {/* Data polygons — filled + stroked */}
      {items.map((item, fi) => {
        const fracs = normalize(item);
        const color = FILE_COLORS[fi % FILE_COLORS.length];
        return (
          <g key={item.path}>
            {/* Glow fill */}
            <polygon
              points={polyPts(fracs)}
              fill={`url(#radar-glow-${fi})`}
              style={{ transition: "all 0.6s cubic-bezier(0.34,1.56,0.64,1)" }}
            />
            {/* Stroke outline */}
            <polygon
              points={polyPts(fracs)}
              className="radar-data-outline"
              fill="none"
              stroke={color}
              strokeWidth="2"
              strokeLinejoin="round"
              style={{
                transition: "all 0.6s cubic-bezier(0.34,1.56,0.64,1)",
                filter: `drop-shadow(0 0 6px ${color}88)`,
              }}
            />
            {/* Dots at each vertex */}
            {fracs.map((f, i) => {
              const p = pt(i, Math.min(Math.max(animated ? f : 0, 0), 1));
              return (
                <circle key={i}
                  cx={p.x.toFixed(2)} cy={p.y.toFixed(2)}
                  r={5} fill={color}
                  stroke="rgba(0,0,0,0.6)" strokeWidth="1.5"
                  style={{
                    transition: `all 0.6s cubic-bezier(0.34,1.56,0.64,1) ${i * 40}ms`,
                    filter: `drop-shadow(0 0 4px ${color})`,
                  }}
                />
              );
            })}
          </g>
        );
      })}

      {/* Axis labels */}
      {labels.map((label, i) => {
        const { x, y } = labelOffset(i);
        return (
          <text key={i}
            x={x.toFixed(2)} y={y.toFixed(2)}
            textAnchor="middle" dominantBaseline="middle"
            fill="rgba(255,255,255,0.6)"
            fontSize="10.5"
            fontFamily="'SF Pro Text', Helvetica, sans-serif"
            fontWeight="500"
            letterSpacing="0.3"
          >
            {label}
          </text>
        );
      })}

      {/* Center dot */}
      <circle cx={cx} cy={cy} r={3}
        fill="rgba(255,255,255,0.15)"
        stroke="rgba(255,255,255,0.3)" strokeWidth="1"
      />
    </svg>
  );
}

// ── Score bar chart ───────────────────────────────────────────────────────────
function ScoreBarChart({ items }: { items: VideoData[] }) {
  const [mounted, setMounted] = React.useState(false);
  React.useEffect(() => {
    const t = setTimeout(() => setMounted(true), 120);
    return () => clearTimeout(t);
  }, []);

  const metrics: Array<{ key: keyof VideoData; label: string; max: number }> = [
    { key: "score",            label: "Quality",    max: 100 },
    { key: "tv_score",         label: "TV Score",   max: 100 },
    { key: "confidence_score", label: "Confidence", max: 100 },
    { key: "audio_score",      label: "Audio",      max: 10  },
  ];

  return (
    <div className="score-bar-chart">
      {metrics.map(({ key, label, max }, mi) => (
        <div key={key} className="sbc-metric">
          <span className="sbc-metric-label">{label}</span>
          <div className="sbc-tracks">
            {items.map((item, fi) => {
              const val   = (item[key] as number) ?? 0;
              const pct   = Math.min((val / max) * 100, 100);
              const color = FILE_COLORS[fi % FILE_COLORS.length];
              return (
                <div key={item.path} className="sbc-track-row">
                  <span className="sbc-file-dot" style={{ background: color }} />
                  <div className="sbc-track">
                    <div
                      className="sbc-fill"
                      style={{
                        width: mounted ? `${pct}%` : "0%",
                        background: `linear-gradient(90deg, ${color}cc, ${color})`,
                        boxShadow: mounted ? `0 0 8px ${color}55` : "none",
                        transition: `width 0.7s cubic-bezier(0.34,1.2,0.64,1) ${(mi * 2 + fi) * 60}ms,
                                     box-shadow 0.7s ease ${(mi * 2 + fi) * 60}ms`,
                      }}
                    />
                  </div>
                  <span className="sbc-value" style={{ color }}>
                    {key === "audio_score" ? `${val}/10` : val}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );
}

// ── Bitrate comparison ────────────────────────────────────────────────────────
function BitrateChart({ items }: { items: VideoData[] }) {
  const [mounted, setMounted] = React.useState(false);
  React.useEffect(() => {
    const t = setTimeout(() => setMounted(true), 150);
    return () => clearTimeout(t);
  }, []);

  const maxBr = Math.max(...items.map((i) => i.bitrate_mbps ?? 0), 1);

  return (
    <div className="bitrate-chart">
      {items.map((item, fi) => {
        const pct   = ((item.bitrate_mbps ?? 0) / maxBr) * 100;
        const color = FILE_COLORS[fi % FILE_COLORS.length];
        const mbps  = (item.bitrate_mbps ?? 0).toFixed(2);
        // Tier label
        const tier  = (item.bitrate_mbps ?? 0) > 60 ? "REMUX"
                    : (item.bitrate_mbps ?? 0) > 30 ? "HIGH"
                    : (item.bitrate_mbps ?? 0) > 12 ? "MID"
                    : "LOW";
        return (
          <div key={item.path} className="bc-row">
            <div className="bc-label-col">
              <span className="bc-name" style={{ color }}>
                {item.file.length > 20
                  ? item.file.slice(0, 20) + "…"
                  : item.file}
              </span>
              <span className="bc-tier" style={{
                color: tier === "REMUX" ? "#30d158"
                     : tier === "HIGH"  ? "#ff9f0a"
                     : tier === "MID"   ? "#2997ff"
                     : "#ff453a",
              }}>{tier}</span>
            </div>
            <div className="bc-track">
              <div
                className="bc-fill"
                style={{
                  width: mounted ? `${pct}%` : "0%",
                  background: `linear-gradient(90deg, ${color}99, ${color})`,
                  boxShadow: mounted ? `0 0 10px ${color}44` : "none",
                  transition: `width 0.75s cubic-bezier(0.34,1.2,0.64,1) ${fi * 80}ms`,
                }}
              />
              {/* Value label inside bar if wide enough */}
              {pct > 30 && (
                <span className="bc-fill-label" style={{ color: "rgba(0,0,0,0.8)" }}>
                  {mbps} Mbps
                </span>
              )}
            </div>
            {pct <= 30 && (
              <span className="bc-value" style={{ color }}>{mbps} Mbps</span>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ── TV compatibility panel ───────────────────────────────────────────────────
function TVPanel({ items }: { items: VideoData[] }) {
  const dvProfiles = [
    { profile: "8.1",  support: "Yes",     label: "Best native target" },
    { profile: "8.4",  support: "Yes",     label: "HLG-base — excellent" },
    { profile: "5",    support: "Yes",     label: "Single-layer streaming" },
    { profile: "8.2",  support: "Yes",     label: "SDR-compat base" },
    { profile: "8.x",  support: "Yes",     label: "Generic Profile 8" },
    { profile: "7",    support: "Partial", label: "BL+RPU used; EL ignored" },
    { profile: "4",    support: "Limited", label: "Unreliable" },
    { profile: "None", support: "No",      label: "No Dolby Vision" },
  ];

  return (
    <div className="tv-panel">
      <div className="panel-head">
        <p className="panel-kicker">Sony Bravia 8 Mark II</p>
        <h3>Dolby Vision Profile Support</h3>
      </div>

      <div className="tv-dv-grid">
        {dvProfiles.map((row) => {
          const matchedFiles = items.filter(
            (item) => item.dv_profile === row.profile
          );
          return (
            <div key={row.profile} className={`tv-dv-row support-${row.support.toLowerCase()}`}>
              <span className="tv-dv-profile">Profile {row.profile}</span>
              <span className={`tv-dv-support support-badge-${row.support.toLowerCase()}`}>
                {row.support}
              </span>
              <span className="tv-dv-label">{row.label}</span>
              {matchedFiles.length > 0 && (
                <span className="tv-dv-match-list">
                  {matchedFiles.map((f, fileIndex) => {
                    const color = FILE_COLORS[items.findIndex((item) => item.path === f.path) % FILE_COLORS.length];
                    const name = cleanFileName(f.file).replace(/\.[^.]+$/, "");
                    const visibleName = name.length > 28 ? name.slice(0, 28) + "…" : name;
                    return (
                      <span key={f.path} className="tv-dv-match-chip" style={{ borderColor: color + "66", background: color + "14", color }}>
                        <span className="tv-dv-chip-dot" style={{ background: color }} />
                        <span className="tv-dv-chip-name">{fileIndex === 0 ? "← " : ""}{visibleName}</span>
                      </span>
                    );
                  })}
                </span>
              )}
            </div>
          );
        })}
      </div>

      {items.length > 0 && (
        <div className="tv-scores-row">
          {items.map((item, fi) => (
            <div key={item.path} className="tv-score-chip">
              <span
                className="tv-score-dot"
                style={{ background: FILE_COLORS[fi % FILE_COLORS.length] }}
              />
              <div>
                <p className="tv-sc-file">
                  {cleanFileName(item.file).length > 20 ? cleanFileName(item.file).slice(0, 20) + "…" : cleanFileName(item.file)}
                </p>
                <p
                  className="tv-sc-score"
                  style={{ color: FILE_COLORS[fi % FILE_COLORS.length] }}
                >
                  {item.tv_score ?? "–"}{" "}
                  <span className="tv-sc-label">{item.tv_label ?? ""}</span>
                </p>
                <p className="tv-sc-note">{item.tv_dv_note ?? ""}</p>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── USB panel ────────────────────────────────────────────────────────────────
function USBPanel({ items }: { items: VideoData[] }) {
  return (
    <div className="usb-panel">
      <div className="panel-head">
        <p className="panel-kicker">USB Playback</p>
        <h3>Drive &amp; File Compatibility</h3>
      </div>

      <div className="usb-spec-grid">
        <div className="usb-spec-block">
          <h4>Drive Format</h4>
          {BRAVIA_8_II_SPEC.usb_fs.map((fs) => (
            <p key={fs} className={fs.includes("recommended") ? "usb-rec" : "usb-ok"}>
              {fs}
            </p>
          ))}
        </div>
        <div className="usb-spec-block">
          <h4>Video Codecs</h4>
          {BRAVIA_8_II_SPEC.usb_video.map((v) => (
            <p key={v} className="usb-ok">{v}</p>
          ))}
        </div>
        <div className="usb-spec-block">
          <h4>Audio Codecs</h4>
          {BRAVIA_8_II_SPEC.usb_audio.map((a) => (
            <p key={a} className="usb-ok">{a}</p>
          ))}
        </div>
        <div className="usb-spec-block">
          <h4>Containers</h4>
          {BRAVIA_8_II_SPEC.usb_containers.map((c) => (
            <p key={c} className="usb-ok">{c}</p>
          ))}
        </div>
      </div>

      {items.length > 0 && (
        <div className="usb-file-compat">
          <h4>Per-file compatibility</h4>
          {items.map((item, fi) => (
            <div key={item.path} className={`usb-file-row ${item.usb_compatible ? "usb-compat-ok" : "usb-compat-fail"}`}>
              <span
                className="usb-dot"
                style={{ background: FILE_COLORS[fi % FILE_COLORS.length] }}
              />
              <div className="usb-file-info">
                <p className="usb-file-name">{cleanFileName(item.file)}</p>
                <p className="usb-file-status">
                  {item.usb_compatible
                    ? "✓ Compatible with USB playback"
                    : "✗ Has issues for USB playback"}
                </p>
                {(item.usb_issues ?? []).map((issue, i) => (
                  <p key={i} className="usb-issue">⚠ {issue}</p>
                ))}
                {(item.usb_warnings ?? []).map((warn, i) => (
                  <p key={i} className="usb-warning">ⓘ {warn}</p>
                ))}
              </div>
              <span className="usb-size">
                {item.file_size_gb ? `${item.file_size_gb.toFixed(1)} GB` : ""}
              </span>
            </div>
          ))}
        </div>
      )}

      <div className="usb-tips">
        <h4>Tips for your Bravia 8 II</h4>
        {BRAVIA_8_II_SPEC.usb_notes.map((note, i) => (
          <p key={i} className="usb-tip">• {note}</p>
        ))}
      </div>
    </div>
  );
}

// ── Legend ───────────────────────────────────────────────────────────────────
function FileLegend({ items }: { items: VideoData[] }) {
  if (items.length <= 1) return null;
  return (
    <div className="file-legend">
      {items.map((item, fi) => (
        <div key={item.path} className="legend-item">
          <span className="legend-dot" style={{ background: FILE_COLORS[fi % FILE_COLORS.length] }} />
          <span className="legend-name">
            {cleanFileName(item.file).length > 28 ? cleanFileName(item.file).slice(0, 28) + "…" : cleanFileName(item.file)}
          </span>
          <span className="legend-rank">#{item.batch_rank ?? fi + 1}</span>
        </div>
      ))}
    </div>
  );
}

// ── Main App ─────────────────────────────────────────────────────────────────
export default function App() {
  const [path,             setPath]             = useState("");
  const [data,             setData]             = useState<VideoData[]>(() => {
    try {
      const saved = localStorage.getItem("last_results");
      return saved ? JSON.parse(saved) as VideoData[] : [];
    } catch {
      return [];
    }
  });
  const [error,            setError]            = useState("");
  const [isLoading,        setIsLoading]        = useState(false);
  const [selectedFiles,    setSelectedFiles]    = useState<File[]>([]);
  const [dragActive,       setDragActive]       = useState(false);
  const [fastMode,         setFastMode]         = useState(true);
  const [isLightMode,      setIsLightMode]      = useState(() => {
    return localStorage.getItem("theme") === "light";
  });
  useEffect(() => {
  localStorage.setItem("theme", isLightMode ? "light" : "dark");
  }, [isLightMode]);
  useEffect(() => {
    localStorage.setItem("last_results", JSON.stringify(data));
  }, [data]);
  const abortRef = useRef(false);
  const [jobId,       setJobId]       = useState<string | null>(null);
  const [progressMsg, setProgressMsg] = useState("");
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // ── File selection ─────────────────────────────────────────────────────────
  const addFiles = (incoming: FileList | File[]) => {
    const valid = Array.from(incoming).filter((f) =>
      VIDEO_EXTS.some((ext) => f.name.toLowerCase().endsWith(ext))
    );
    if (!valid.length) return;
    setSelectedFiles((prev) => {
      const names = new Set(prev.map((f) => f.name));
      return [...prev, ...valid.filter((f) => !names.has(f.name))];
    });
    setError("");
    setPath("");
  };

  const removeFile = (name: string) => {
    setSelectedFiles((prev) => prev.filter((f) => f.name !== name));
  };

  const handleFileInput = (e: ChangeEvent<HTMLInputElement>) => {
    if (e.target.files) addFiles(e.target.files);
    e.target.value = "";
  };

  const handleDragOver = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragActive(true);
  };

  const handleDragLeave = () => setDragActive(false);

  const handleDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragActive(false);
    if (e.dataTransfer.files) addFiles(e.dataTransfer.files);
  };

  // ── Analysis ───────────────────────────────────────────────────────────────
  const analyzeSelection = async () => {
    abortRef.current = false;
    const trimmedPath = path.trim();
    if (!selectedFiles.length && !trimmedPath) {
      setError("Drag & drop files, choose with Browse, or paste a path.");
      setData([]);
      return;
    }

    setIsLoading(true);
    setError("");

    try {
      let results: VideoData[] = [];
      let cancelled = false;

      const waitForJob = async (job_id: string) => {
        await new Promise<void>((resolve, reject) => {
          const poll = async () => {
            if (abortRef.current) {
              cancelled = true;
              resolve();
              return;
            }
            try {
              const jobRes = await fetch(`${API}/job/${job_id}`);
              const job = await jobRes.json() as {
                status: string; progress: string; current: string;
                results: VideoData[]; error: string | null;
              };
              setProgressMsg(`${job.current || "…"} (${job.progress})`);
              if (job.status === "done") {
                results = job.results;
                resolve();
              } else if (job.status === "error") {
                reject(new Error(job.error ?? "Analysis failed."));
              } else {
                setTimeout(poll, 600);
              }
            } catch (e) { reject(e); }
          };
          poll();
        });
      };

      if (trimmedPath) {
        const res = await fetch(
          `${API}/analyze-path/?path=${encodeURIComponent(trimmedPath)}&fast=${fastMode}`
        );
        if (!res.ok) {
          const payload = await res.json().catch(() => ({})) as { detail?: string };
          throw new Error(payload.detail ?? "Analysis request failed.");
        }
        const { job_id } = await res.json() as { job_id: string };
        setJobId(job_id);
        await waitForJob(job_id);
        setJobId(null);
        setProgressMsg("");
      } else if (selectedFiles.length === 1) {
        const formData = new FormData();
        formData.append("file", selectedFiles[0]);
        const res = await fetch(`${API}/analysis/?fast=${fastMode}`, {
          method: "POST", body: formData,
        });
        if (!res.ok) {
          const payload = await res.json().catch(() => ({})) as { detail?: string };
          const prefix = res.status === 507 ? "🖴 Disk full: "
                       : res.status === 413 ? "📦 File too large: " : "";
          throw new Error(prefix + (payload.detail ?? "Single-file analysis failed."));
        }
        const { job_id } = await res.json() as { job_id: string; total: number };
        setJobId(job_id);
        await waitForJob(job_id);
        setJobId(null);
        setProgressMsg("");
      } else if (selectedFiles.length > 0) {
        const formData = new FormData();
        selectedFiles.forEach((f) => formData.append("files", f));
        const res = await fetch(`${API}/analyze-multiple/?fast=${fastMode}`, {
          method: "POST", body: formData,
        });
        if (!res.ok) {
          const payload = await res.json().catch(() => ({})) as { detail?: string };
          const prefix = res.status === 507 ? "🖴 Disk full: "
                       : res.status === 413 ? "📦 File too large: " : "";
          throw new Error(prefix + (payload.detail ?? "Multi-file analysis failed."));
        }
        const { job_id } = await res.json() as { job_id: string; total: number };
        setJobId(job_id);
        await waitForJob(job_id);
        setJobId(null);
        setProgressMsg("");
      }

      if (cancelled) {
        return;
      }

      results = (Array.isArray(results) ? results : [])
        .filter(Boolean)
        .sort((a, b) =>
          ((b.tv_score ?? 0) + (b.confidence_score ?? 0)) -
          ((a.tv_score ?? 0) + (a.confidence_score ?? 0))
          );

      setData(results);
      localStorage.setItem("last_results", JSON.stringify(results));
    } catch (err: unknown) {
      let message = "Analysis failed.";
      if (axios.isAxiosError(err)) {
        const status = err.response?.status;
        const detail = err.response?.data?.detail;
        if (status === 507) {
          message = `🖴 Server disk full: ${detail ?? "Free up space on the server and retry."}`;
        } else {
          message =
            (typeof detail === "string" && detail) || err.message || message;
        }
      } else if (err instanceof Error) {
        const text = err.message;
        message = text.includes("path input")
          ? "📁 " + text   // already has good instructions
          : text.includes("507")
          ? "🖴 Server disk full — free up space in the uploads/ folder and retry."
          : text;
      }
      setError(message);
      setData([]);
    } finally {
      abortRef.current = false;
      setIsLoading(false);
    }
  };

  const clearAll = () => {
    setPath("");
    setSelectedFiles([]);
    setError("");
  };

  const bestPath = data[0]?.path;
  const showDashboard = data.length > 0;

  return (
    <div className={`app-shell ${isLightMode ? "theme-light" : "theme-dark"}`}>
      {/* ── Nav ─────────────────────────────────────────────────────────── */}
      <nav className="top-nav">
        <div className="nav-links">
          <a href="#analyze">Analyze</a>
          {showDashboard && <a href="#dashboard">Dashboard</a>}
          {showDashboard && <a href="#tv-usb">TV &amp; USB</a>}
          <a href="#hierarchy">Hierarchy</a>
        </div>
        <Switch isLightMode={isLightMode} setIsLightMode={setIsLightMode} />
      </nav>

      {/* ── Hero ────────────────────────────────────────────────────────── */}
      <section id="analyze" className="hero-section">
        <div className="hero-copy">
          <p className="eyebrow">Multi-file HDR &amp; Dolby Vision inspector</p>
          <h1 className="app-title">
            <span>Video Metadata</span>
            <span>Analyzer</span>
          </h1>
          <p className="app-subtitle">
            Compare multiple files at once using MediaInfo, ffprobe, ffmpeg, and dovi_tool.
            Scores are calibrated for your Sony Bravia 8 Mark II via USB.
          </p>
        </div>

        <div className="hero-panel">
          {/* ── Drag zone ─────────────────────────────────────────────── */}
          <div
            className={`drop-zone ${dragActive ? "drop-zone-active" : ""}`}
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            onDrop={handleDrop}
            onClick={() => fileInputRef.current?.click()}
          >
            <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
              <polyline points="17 8 12 3 7 8"/>
              <line x1="12" y1="3" x2="12" y2="15"/>
            </svg>
            <p className="drop-main">Drop video files here</p>
            <p className="drop-sub">MKV · MP4 · TS · M2TS · HEVC · H265</p>
            <p className="drop-sub" style={{ marginTop: 4, fontSize: 11, opacity: 0.6 }}>
              For files larger than 4 GB, paste the path below instead
            </p>
          </div>

          {/* ── Selected file list ─────────────────────────────────────── */}
          {selectedFiles.length > 0 && (
            <div className="file-chip-list">
              {selectedFiles.map((f, i) => (
                <div
                  key={f.name}
                  className="file-chip"
                  style={{ borderColor: FILE_COLORS[i % FILE_COLORS.length] }}
                >
                  <span
                    className="chip-dot"
                    style={{ background: FILE_COLORS[i % FILE_COLORS.length] }}
                  />
                  <span className="chip-name">
                    {f.name.length > 28 ? f.name.slice(0, 28) + "…" : f.name}
                  </span>
                  <button
                    className="chip-remove"
                    type="button"
                    onClick={(e) => { e.stopPropagation(); removeFile(f.name); }}
                  >
                    ×
                  </button>
                </div>
              ))}
            </div>
          )}

          <div className="control-grid">
            {/* ── Path row ─────────────────────────────────────────────── */}
            <div className="path-row">
              <label className="input-label" htmlFor="video-path">
                Or paste a file / folder path
              </label>
              <form
                className="search-form"
                onSubmit={(e) => { e.preventDefault(); void analyzeSelection(); }}
              >
                <button type="submit" className="search-icon-button" aria-label="Analyze">
                  <svg width={17} height={16} fill="none" xmlns="http://www.w3.org/2000/svg">
                    <path d="M7.667 12.667A5.333 5.333 0 107.667 2a5.333 5.333 0 000 10.667zM14.334 14l-2.9-2.9"
                      stroke="currentColor" strokeWidth="1.333" strokeLinecap="round" strokeLinejoin="round"/>
                  </svg>
                </button>
                <input
                  id="video-path"
                  className="search-input"
                  placeholder="C:\Movies\  or  /mnt/media/"
                  value={path}
                  onChange={(e) => {
                    setPath(e.target.value);
                    if (selectedFiles.length) setSelectedFiles([]);
                  }}
                  type="text"
                />
                <button
                  className="search-reset"
                  type="button"
                  aria-label="Clear"
                  onClick={clearAll}
                  disabled={!path && !selectedFiles.length}
                >
                  <svg xmlns="http://www.w3.org/2000/svg" className="reset-icon" fill="none"
                    viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12"/>
                  </svg>
                </button>
              </form>
            </div>

            {/* ── Browse + fast mode row ────────────────────────────────── */}
            <div className="choose-row">
              <span className="input-label">Options</span>
              <div className="options-row">
                <button
                  type="button"
                  className="secondary-button"
                  onClick={() => fileInputRef.current?.click()}
                  disabled={isLoading}
                >
                  Browse Files
                </button>

                <label className="fast-toggle">
                  <input
                    type="checkbox"
                    checked={fastMode}
                    onChange={(e) => setFastMode(e.target.checked)}
                  />
                  <span>Fast mode</span>
                  <span className="fast-hint">(skip RPU deep scan)</span>
                </label>
              </div>
            </div>
          </div>

          {/* Progress indicator */}
          {(jobId && progressMsg) && (
            <div className="progress-toast">
              <div className="progress-spinner" />
              <div className="progress-text">
                <span className="progress-label">Analyzing</span>
                <span className="progress-file">{progressMsg}</span>
              </div>
            </div>
          )}
          <div className="analyze-row">
            <button
              onClick={() => void analyzeSelection()}
              disabled={isLoading}
              className="primary-button"
            >
              {isLoading
                ? "Analyzing…"
                : selectedFiles.length > 1
                ? `Analyze ${selectedFiles.length} Files`
                : "Analyze"}
            </button>
            {isLoading && (
              <button
                className="secondary-button"
                onClick={() => { abortRef.current = true; setIsLoading(false); }}
              >
                Cancel
              </button>
            )}
          </div>
        </div>
      </section>

      <input
        ref={fileInputRef}
        type="file"
        accept=".mkv,.mp4,.ts,.m2ts,.hevc,.h265,video/*"
        multiple
        hidden
        onChange={handleFileInput}
      />

      {error && <div className="error-box">{error}</div>}

      {/* ── Dashboard ───────────────────────────────────────────────────── */}
      {showDashboard && (
        <section id="dashboard" className="dashboard-section">
          <div className="dashboard-inner">
            <div className="dash-header">
              <h2>
                {data.length === 1
                  ? "Analysis Result"
                  : `Comparing ${data.length} Files`}
              </h2>
              <p className="dash-sub">
                Ranked by TV score for Sony Bravia 8 Mark II USB playback
              </p>
              <button
                className="secondary-button"
                style={{ marginLeft: "auto", marginTop: "12px" }}
                onClick={() => {
                  setData([]);
                  // selectedFiles are still in state — just re-run
                  void analyzeSelection();
                }}
                disabled={isLoading || (!selectedFiles.length && !path)}
              >
                ↺ Re-analyze
              </button>
            </div>

            <FileLegend items={data} />

            {/* ── Leaderboard ─────────────────────────────────────────── */}
            {data.length > 1 && (
              <div className="leaderboard">
                {data.map((item, fi) => {
                  const isBest = item.path === bestPath;
                  const color  = FILE_COLORS[fi % FILE_COLORS.length];
                  const tvPct  = Math.min((item.tv_score ?? 0), 100);
                  return (
                    <div key={item.path}
                      className={`lb-row ${isBest ? "lb-best" : ""}`}
                      style={{ animationDelay: `${fi * 80}ms` }}
                    >
                      {/* Rank ring */}
                      <div className="lb-rank-ring">
                        <svg width="44" height="44" viewBox="0 0 44 44">
                          <circle cx="22" cy="22" r="18"
                            fill="none" stroke="rgba(255,255,255,0.07)" strokeWidth="3" />
                          <circle cx="22" cy="22" r="18"
                            fill="none" stroke={color} strokeWidth="3"
                            strokeDasharray={`${(tvPct / 100) * 113} 113`}
                            strokeLinecap="round"
                            transform="rotate(-90 22 22)"
                            style={{ filter: `drop-shadow(0 0 4px ${color}88)` }}
                          />
                          <text x="22" y="22"
                            textAnchor="middle" dominantBaseline="central"
                            fill={color} fontSize="12" fontWeight="700"
                          >
                            #{fi + 1}
                          </text>
                        </svg>
                      </div>

                      <div className="lb-file">
                        <p className="lb-name">
                          {item.file.replace(/^[0-9a-f]{32}_/i, "")}
                        </p>
                        <p className="lb-meta">
                          DV {item.dv_profile} · {item.source} · {(item.bitrate_mbps ?? 0).toFixed(1)} Mbps
                        </p>
                      </div>

                      <div className="lb-scores">
                        <div className="lb-score-block">
                          <span className="lb-score-val" style={{ color }}>
                            {item.tv_score ?? "–"}
                          </span>
                          <span className="lb-score-tag">TV</span>
                        </div>
                        <div className="lb-score-block">
                          <span className="lb-score-val" style={{ color: "rgba(255,255,255,0.7)" }}>
                            {item.score}
                          </span>
                          <span className="lb-score-tag">Q</span>
                        </div>
                        <span className={`lb-verdict lb-verdict-${isBest ? "best" : "ok"}`}
                          style={isBest ? { background: color + "22", color, borderColor: color + "55" } : {}}>
                          {getVerdict(item, isBest)}
                        </span>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}

            {/* ── Charts grid ─────────────────────────────────────────── */}
            <div className="charts-grid">
              {data.length >= 2 && (
                <div className="chart-card">
                  <h3>Multi-dimension Radar</h3>
                  <p className="chart-sub">Quality · TV Score · Bitrate · Audio · Confidence</p>
                  <div className="radar-wrap">
                    <RadarChart items={data} size={260} />
                  </div>
                </div>
              )}

              <div className="chart-card">
                <h3>Score Comparison</h3>
                <p className="chart-sub">All scoring dimensions side by side</p>
                <ScoreBarChart items={data} />
              </div>

              <div className="chart-card">
                <h3>Bitrate</h3>
                <p className="chart-sub">Video stream bitrate in Mbps</p>
                <BitrateChart items={data} />
              </div>
            </div>
          </div>
        </section>
      )}

      {/* ── TV & USB panels ─────────────────────────────────────────────── */}
      {showDashboard && (
        <section id="tv-usb" className="tv-usb-section">
          <div className="tv-usb-inner">
            <TVPanel items={data} />
            <USBPanel items={data} />
          </div>
        </section>
      )}

      {/* ── Per-file result cards ────────────────────────────────────────── */}
      <div className="results-grid">
        {data.map((item, idx) => {
          const isBest      = item.path === bestPath;
          const color       = FILE_COLORS[idx % FILE_COLORS.length];
          const scoreWidth  = Math.min(item.score ?? 0, 100);
          const confWidth   = Math.min(item.confidence_score ?? 0, 100);
          const tvWidth     = Math.min(item.tv_score ?? 0, 100);

          return (
            <ResultErrorBoundary key={item.path || idx}>
            <article className={`result-card ${isBest ? "result-card-best" : ""}`}
              style={{ "--card-accent": color } as React.CSSProperties}>
              <div className="result-header">
                <div className="result-header-copy">
                  <p className="result-kicker" style={{ color }}>
                    #{item.batch_rank ?? idx + 1} · {item.source}
                  </p>
                  <h2 className="result-file">{cleanFileName(item.file)}</h2>
                  <p className="result-path">{item.path}</p>
                </div>

                <div className="score-panel" style={{ borderColor: color + "44" }}>
                  <span className="score-label">Quality</span>
                  <span
                    className="score-value"
                    style={{ color: scoreColor(item.score) }}
                    title="Quality Score: overall source fidelity independent of TV. Considers DV profile, bitrate, bit depth, audio, source type."
                  >
                    {item.score}
                  </span>
                  <span className="score-label" style={{ marginTop: 6 }}>TV Score</span>
                  <span
                    className="score-tv"
                    style={{ color }}
                    title="TV Score: calibrated for Sony Bravia 8 Mark II USB playback. 80+ = Excellent, 65+ = Very Good, 50+ = Good."
                  >
                    {item.tv_score ?? "–"}
                  </span>
                  <span className="meta-line">Conf. {item.confidence_score ?? 0}/100</span>
                </div>
              </div>

              <div className="tag-row">
                <span className="tag tag-dv">DV {item.dv_profile}</span>
                <span className="tag tag-source">{item.audio ?? "?"}</span>
                <span className="tag tag-bitrate" style={{ background: color }}>
                  {(item.bitrate_mbps ?? 0).toFixed(2)} Mbps
                </span>
                <span className="tag tag-runtime">{(item.duration_min ?? 0).toFixed(1)} min</span>
                <span className={`tag tag-usb ${item.usb_compatible ? "tag-usb-ok" : "tag-usb-fail"}`}>
                  USB {item.usb_compatible ? "✓" : "✗"}
                </span>
                <span className="tag tag-source">{item.confidence_label ?? "?"}</span>
              </div>

              {item.quick_summary  && <div className="summary-line">{item.quick_summary}</div>}
              {item.recommendation && <div className="detail-line">{item.recommendation}</div>}
              {item.dv_profile === "7" && (
                <div className="conversion-block">
                  <p className="fact-kicker">⚙ Conversion for this TV</p>
                  <code className="conv-cmd">
                    dovi_tool -m 2 convert --discard -i input.hevc -o bl_rpu.hevc
                  </code>
                  <code className="conv-cmd">
                    mp4muxer -i bl_rpu.hevc --dv-profile 8 --dv-bl-compatible-id 1 -o output.mp4
                  </code>
                  <p style={{fontSize: 12, color: "var(--soft-text)", marginTop: 6}}>
                    Converts P7 → P8.1 MP4. Discards EL (already unused on your TV).
                  </p>
                </div>
              )}
              {item.insights       && <div className="insight-line">{item.insights}</div>}

              <div className="result-submeta">
                <span>Analyzer: {item.dv_tool ?? "unknown"}</span>
                <span>BL {item.bl ?? "?"} / EL {item.el ?? "?"} / RPU {item.rpu ?? "?"}</span>
                <span>TV: {item.tv_playback ?? "?"}</span>
              </div>

              {/* Quality bars */}
              <div className="quality-bars">
                {[
                  { label: "Quality",    pct: scoreWidth,  cls: "" },
                  { label: "TV Score",   pct: tvWidth,     cls: "tv" },
                  { label: "Confidence", pct: confWidth,   cls: "secondary" },
                ].map(({ label, pct, cls }) => (
                  <div key={label} className="bar">
                    <span>{label}</span>
                    <div className="bar-track">
                      <div className={`bar-fill ${cls}`} style={{ width: `${pct}%` }} />
                    </div>
                    <span className="bar-val">{pct}</span>
                  </div>
                ))}
              </div>

              {/* Fact panels */}
              <div className="result-sections">
                <section className="fact-panel">
                  <div className="fact-panel-head">
                    <p className="fact-kicker">Dolby Vision</p>
                    <h3>Signal readout</h3>
                  </div>
                  <dl className="fact-list">
                    {(item.signal_facts ?? []).map((fact) => (
                      <div className="fact-row" key={fact.label}>
                        <dt>{fact.label}</dt>
                        <dd>{fact.value}</dd>
                      </div>
                    ))}
                  </dl>
                </section>

                <section className="fact-panel">
                  <div className="fact-panel-head">
                    <p className="fact-kicker">Media</p>
                    <h3>Container &amp; stream facts</h3>
                  </div>
                  <dl className="fact-list">
                    {(item.media_facts ?? []).map((fact) => (
                      <div className="fact-row" key={fact.label}>
                        <dt>{fact.label}</dt>
                        <dd>{fact.value}</dd>
                      </div>
                    ))}
                  </dl>
                </section>
              </div>

              {/* Toolchain */}
              <section className="toolchain-section">
                <div className="fact-panel-head">
                  <p className="fact-kicker">Toolchain</p>
                  <h3>What each tool contributed</h3>
                </div>
                <div className="tool-grid">
                  {(item.tool_reports ?? []).map((report) => (
                    <article
                      key={report.name}
                      className={`tool-card tool-card-${report.status}`}
                    >
                      <div className="tool-card-head">
                        <h4>{report.name}</h4>
                        <span className={`tool-status tool-status-${report.status}`}>
                          {getToolStatusLabel(report.status)}
                        </span>
                      </div>
                      <p className="tool-headline">{report.headline}</p>
                      {report.details.length > 0 && (
                        <ul className="tool-detail-list">
                          {report.details.map((d, di) => (
                            <li key={di}>{d}</li>
                          ))}
                        </ul>
                      )}
                    </article>
                  ))}
                </div>
              </section>

              {isBest && data.length > 1 && (
                <div className="best-badge" style={{ color }}>
                  ★ Top score in this batch
                </div>
              )}

              <a
                href={encodeURI(`file:///${item.path.replace(/\\/g, "/")}`)}
                className="open-link"
              >
                Open File
              </a>
              <div className="meta-line">Verdict: {getVerdict(item, isBest)}</div>
            </article>
            </ResultErrorBoundary>
          );
        })}
      </div>

      {/* ── Info sections ────────────────────────────────────────────────── */}
      <section id="hierarchy" className="info-section info-section-light">
        <div className="info-copy">
          <p className="section-kicker">Hierarchy</p>
          <h2>Dolby Vision and HDR from best to worst.</h2>
          <p className="quick-ranking">
            <b>Quick ranking:</b>{" "}
            <span className="rank r1">P7 FEL</span> {">"}
            <span className="rank r2"> P7 MEL</span> {">"}
            <span className="rank r3"> P8.1 <span className="approx">≈</span> P5 </span> {">"}
            <span className="rank r3"> P8.4</span> {">"}
            <span className="rank r5"> P8.2</span> {">"}
            <span className="rank r6"> P4</span> {">"}
            <span className="rank r7"> HDR10+</span> {">"}
            <span className="rank r9"> HDR10</span> {">"}
            <span className="rank r10"> HLG</span>
          </p>
        </div>

        <div className="hierarchy-grid">
          <div className="hierarchy-card">
            <h3>Dolby Vision</h3>
            <ol className="hierarchy-list">
              {DV_HIERARCHY.map((item) => (
                <li key={item.label}>
                  <div className="hierarchy-item-head">
                    <span>{item.label}</span>
                    <span className={item.cls}>{item.verdict}</span>
                  </div>
                  <p>{item.notes}</p>
                </li>
              ))}
            </ol>
          </div>

          <div className="hierarchy-card">
            <h3>HDR (non-DV)</h3>
            <ol className="hierarchy-list">
              {HDR_HIERARCHY.map((item) => (
                <li key={item.label}>
                  <div className="hierarchy-item-head">
                    <span>{item.label}</span>
                    <span className={item.cls}>{item.verdict}</span>
                  </div>
                  <p>{item.notes}</p>
                </li>
              ))}
            </ol>
          </div>
        </div>
      </section>
    </div>
  );
}