import React, {
  type ChangeEvent,
  type DragEvent,
  useRef,
  useState,
  useEffect
} from "react";
import type {
  VideoData, MagnetFile, MagnetTorrent,
  ComparisonPayload, PerMagnetRecord,
} from "./types";
import { BRAVIA_8_II_SPEC } from "./types";
import "./App.css";
import Switch from "./Switch";

// ── Magnet link icon (reusable) ──────────────────────────────────────────────
// Renders the same SVG used in the magnet input field, wrapped in an <a>
// pointing at the magnet URI. Native browser handling opens the user's
// default torrent client. Falls back to a non-link span if no URI provided.
function MagnetLink({ uri, size = 14, title = "Open magnet in torrent client" }: {
  uri: string | null | undefined;
  size?: number;
  title?: string;
}) {
  const svg = (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
         stroke="currentColor" strokeWidth="1.8"
         strokeLinecap="round" strokeLinejoin="round"
         style={{ verticalAlign: "middle" }}>
      <path d="M16 4h4v4"/>
      <path d="M14 10l6-6"/>
      <path d="M8 20H4v-4"/>
      <path d="M10 14l-6 6"/>
      <path d="M8 4h8a4 4 0 0 1 4 4v8"/>
    </svg>
  );
  if (!uri) {
    return <span style={{ opacity: 0.35, display: "inline-flex" }} title="No magnet URI">{svg}</span>;
  }
  return (
    <a href={uri} title={title}
       style={{
         display: "inline-flex", alignItems: "center",
         color: "var(--accent-bright, #2997ff)",
         textDecoration: "none", marginRight: 6,
       }}
       onClick={(e) => {
         // Some setups prompt-confirm magnet:// — let the default handler run.
         e.stopPropagation();
       }}>
      {svg}
    </a>
  );
}

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
  // Hover-to-highlight state. When set, the matching file's polygon is
  // pulled to the front (rendered last) and stays at full opacity while
  // every other polygon dims back. Solves the "yellow disappears under
  // orange" problem when many shapes overlap.
  const [hoveredIndex, setHoveredIndex] = useState<number | null>(null);

  const N    = 5;
  const cx   = size / 2;
  const cy   = size / 2;
  const r    = (size / 2) * 0.65;
  const labels = ["Quality", "TV Score", "Bitrate", "Audio", "Confidence"];

  const angle = (i: number) => (Math.PI * 2 * i) / N - Math.PI / 2;
  const pt = (i: number, f: number) => ({
    x: cx + r * f * Math.cos(angle(i)),
    y: cy + r * f * Math.sin(angle(i)),
  });
  const polyPts = (fracs: number[]) =>
    fracs
      .map((f, i) => pt(i, Math.min(Math.max(f, 0), 1)))
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

  // Render polygons in priority order: non-hovered first, hovered last
  // (so SVG draws it on top). When nothing is hovered the original index
  // order is preserved, so the picture looks identical to before.
  const drawOrder = items.map((_, i) => i);
  if (hoveredIndex !== null) {
    const pos = drawOrder.indexOf(hoveredIndex);
    if (pos !== -1) {
      drawOrder.splice(pos, 1);
      drawOrder.push(hoveredIndex);
    }
  }

  return (
    <div className="radar-host">
      <svg
        width={size}
        height={size}
        viewBox={`0 0 ${size} ${size}`}
        style={{ overflow: "visible" }}
      >
        {gridLevels.map((level) => (
          <polygon
            key={level}
            points={Array.from({ length: N }, (_, i) => {
              const p = pt(i, level);
              return `${p.x.toFixed(2)},${p.y.toFixed(2)}`;
            }).join(" ")}
            fill="none"
            // Theme-aware grid: outer ring uses the "strong" var, inner rings
            // use the regular grid var. Both swap between dark/light via CSS.
            stroke={`var(${level === 1 ? "--chart-grid-strong" : "--chart-grid"})`}
            strokeWidth="1"
          />
        ))}

        {Array.from({ length: N }, (_, i) => {
          const p = pt(i, 1);
          return (
            <line
              key={i}
              x1={cx} y1={cy}
              x2={p.x.toFixed(2)} y2={p.y.toFixed(2)}
              stroke="var(--chart-axis)"
              strokeWidth="1"
            />
          );
        })}

        {drawOrder.map((fi) => {
          const item = items[fi];
          const fracs = normalize(item);
          const color = FILE_COLORS[fi % FILE_COLORS.length];
          // Three visual states per polygon:
          //   none hovered    → normal (fill 22 / stroke 2)
          //   this is hovered → emphasised (fill 44 / stroke 3 / pop circles)
          //   other is hovered → dimmed   (fill 0a / stroke 1 / faded circles)
          const isHovered = hoveredIndex === fi;
          const isDimmed  = hoveredIndex !== null && !isHovered;
          const fillAlpha = isHovered ? "44" : isDimmed ? "0a" : "22";
          const strokeWidth = isHovered ? 3 : isDimmed ? 1 : 2;
          const circleRadius = isHovered ? 5 : isDimmed ? 3 : 4;
          const groupOpacity = isDimmed ? 0.45 : 1;
          return (
            <g key={item.path} style={{ opacity: groupOpacity, transition: "opacity .15s ease" }}>
              <polygon
                points={polyPts(fracs)}
                fill={color + fillAlpha}
                stroke={color}
                strokeWidth={strokeWidth}
                strokeLinejoin="round"
              />
              {fracs.map((f, i) => {
                const p = pt(i, Math.min(Math.max(f, 0), 1));
                return (
                  <circle
                    key={i}
                    cx={p.x.toFixed(2)}
                    cy={p.y.toFixed(2)}
                    r={circleRadius}
                    fill={color}
                  />
                );
              })}
            </g>
          );
        })}

        {labels.map((label, i) => {
          const p = pt(i, 1.28);
          return (
            <text
              key={i}
              x={p.x.toFixed(2)}
              y={p.y.toFixed(2)}
              textAnchor="middle"
              dominantBaseline="middle"
              // Theme-aware: dark mode uses ~white@65%, light mode uses ~black@65%.
              fill="var(--chart-label)"
              fontSize="11"
              fontFamily="'SF Pro Text', Helvetica, sans-serif"
            >
              {label}
            </text>
          );
        })}
      </svg>

      {/* Legend — one colored dot per file. Hover any dot to bring that
          file's shape to the front and dim the others. The dot doubles as
          a clickable index; on mobile, tap toggles persistent highlight. */}
      <div className="radar-legend" role="list">
        {items.map((item, fi) => {
          const color = FILE_COLORS[fi % FILE_COLORS.length];
          const isHovered = hoveredIndex === fi;
          return (
            <button
              key={item.path}
              type="button"
              role="listitem"
              className={`radar-legend-item${isHovered ? " is-active" : ""}`}
              title={cleanFileName(item.file)}
              onMouseEnter={() => setHoveredIndex(fi)}
              onMouseLeave={() => setHoveredIndex(null)}
              onFocus={() => setHoveredIndex(fi)}
              onBlur={() => setHoveredIndex(null)}
              onClick={() => setHoveredIndex(isHovered ? null : fi)}
              aria-label={`Highlight ${cleanFileName(item.file)} on the radar`}
            >
              <span className="radar-legend-dot" style={{ background: color }} />
              <span className="radar-legend-text">
                #{fi + 1}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

// ── Score bar chart ───────────────────────────────────────────────────────────
function ScoreBarChart({ items }: { items: VideoData[] }) {
  const metrics: Array<{ key: keyof VideoData; label: string; max: number }> = [
    { key: "score",            label: "Quality",    max: 100 },
    { key: "tv_score",         label: "TV Score",   max: 100 },
    { key: "confidence_score", label: "Confidence", max: 100 },
    { key: "audio_score",      label: "Audio",      max: 10  },
  ];

  return (
    <div className="score-bar-chart">
      {metrics.map(({ key, label, max }) => (
        <div key={key} className="sbc-metric">
          <span className="sbc-metric-label">{label}</span>
          <div className="sbc-tracks">
            {items.map((item, fi) => {
              const val  = (item[key] as number) ?? 0;
              const pct  = Math.min((val / max) * 100, 100);
              const color = FILE_COLORS[fi % FILE_COLORS.length];
              return (
                <div key={item.path} className="sbc-track-row">
                  <span className="sbc-file-dot" style={{ background: color }} />
                  <div className="sbc-track">
                    <div
                      className="sbc-fill"
                      style={{ width: `${pct}%`, background: color }}
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
  const maxBr = Math.max(...items.map((i) => i.bitrate_mbps ?? 0), 1);
  return (
    <div className="bitrate-chart">
      {items.map((item, fi) => {
        const pct   = ((item.bitrate_mbps ?? 0) / maxBr) * 100;
        const color = FILE_COLORS[fi % FILE_COLORS.length];
        return (
          <div key={item.path} className="bc-row">
            <span className="bc-label" style={{ color }}>
              {cleanFileName(item.file).length > 22 ? cleanFileName(item.file).slice(0, 22) + "…" : cleanFileName(item.file)}
            </span>
            <div className="bc-track">
              <div className="bc-fill" style={{ width: `${pct}%`, background: color }} />
            </div>
            <span className="bc-value" style={{ color }}>
              {(item.bitrate_mbps ?? 0).toFixed(2)} Mbps
            </span>
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
          // For each matching file, capture its index in the original
          // `items` array so we can colour the dot to match the file's
          // colour everywhere else in the dashboard (radar/score bars/
          // bitrate chart all share the same FILE_COLORS index).
          const matchedIndexes = items
            .map((item, i) => (item.dv_profile === row.profile ? i : -1))
            .filter((i) => i !== -1);
          return (
            <div key={row.profile} className={`tv-dv-row support-${row.support.toLowerCase()}`}>
              <span className="tv-dv-profile">Profile {row.profile}</span>
              <span className={`tv-dv-support support-badge-${row.support.toLowerCase()}`}>
                {row.support}
              </span>
              <span className="tv-dv-label">{row.label}</span>
              {matchedIndexes.length > 0 && (
                <span className="tv-dv-match" aria-label={`Files at this profile: ${matchedIndexes.map((i) => `#${i + 1}`).join(", ")}`}>
                  {matchedIndexes.map((i) => (
                    <span
                      key={i}
                      className="tv-dv-match-dot"
                      style={{ background: FILE_COLORS[i % FILE_COLORS.length] }}
                      title={cleanFileName(items[i].file)}
                    />
                  ))}
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
  const [isLightMode,      setIsLightMode]      = useState<boolean>(() => {
    const saved = localStorage.getItem("theme");
    if (saved === "light") return true;
    if (saved === "dark")  return false;
    // First visit or stale value → default to dark mode
    return false;
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

  const [magnetUri,     setMagnetUri]     = useState("");
  const [magnetFiles,   setMagnetFiles]   = useState<MagnetFile[]>([]);
  const [magnetTorrent, setMagnetTorrent] = useState<MagnetTorrent | null>(null);
  const [magnetJobId,   setMagnetJobId]   = useState<string | null>(null);

  // ── Multi-magnet comparison ─────────────────────────────────────────────
  const [magnetList,     setMagnetList]     = useState("");
  const [compareJobId,   setCompareJobId]   = useState<string | null>(null);
  const [compareData,    setCompareData]    = useState<ComparisonPayload | null>(null);
  const [comparePerMag,  setComparePerMag]  = useState<PerMagnetRecord[]>([]);
  const [compareProgress, setCompareProgress] = useState("");

  // ── Mutually-exclusive input setters ───────────────────────────────────────
  // Only one of the four input modes (files / path / magnet / magnet-list)
  // should be populated at a time. Each setter wipes the other three when it
  // receives a non-empty value, so the unified Analyze button below always
  // has exactly one source of truth to act on.
  const setExclusivePath = (value: string) => {
    setPath(value);
    if (value) {
      setSelectedFiles([]);
      setMagnetUri("");
      setMagnetList("");
    }
  };
  const setExclusiveMagnetUri = (value: string) => {
    setMagnetUri(value);
    if (value) {
      setSelectedFiles([]);
      setPath("");
      setMagnetList("");
    }
  };
  const setExclusiveMagnetList = (value: string) => {
    setMagnetList(value);
    if (value) {
      setSelectedFiles([]);
      setPath("");
      setMagnetUri("");
    }
  };

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
    // Files are mutex with the three text inputs.
    setPath("");
    setMagnetUri("");
    setMagnetList("");
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
    // Switching modes — wipe any previous magnet-comparison state so the
    // user doesn't see a stale matrix below the new local-file results.
    setCompareData(null);
    setComparePerMag([]);
    setMagnetFiles([]);
    setMagnetTorrent(null);

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
      if (err instanceof Error) {
        const text = err.message;
        message = text.includes("path input")
          ? "📁 " + text
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

  const analyzeMagnet = async () => {
    const trimmed = magnetUri.trim();
    if (!trimmed) {
      setError("Paste a magnet: URI first.");
      return;
    }
    if (!trimmed.toLowerCase().startsWith("magnet:?")) {
      setError("That doesn't look like a magnet URI — it should start with 'magnet:?'.");
      return;
    }

    setError("");
    setIsLoading(true);
    setMagnetFiles([]);
    setMagnetTorrent(null);
    // Switching modes — clear any prior multi-magnet comparison so the
    // single-magnet result renders cleanly on its own.
    setCompareData(null);
    setComparePerMag([]);

    try {
      const res = await fetch(`${API}/magnet/?fast=${fastMode}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ magnet: trimmed }),
      });
      if (!res.ok) {
        const payload = await res.json().catch(() => ({})) as { detail?: string };
        throw new Error(payload.detail ?? "Magnet request failed.");
      }
      const { job_id } = await res.json() as { job_id: string };
      setMagnetJobId(job_id);
      setJobId(job_id);

      type MagnetJob = {
        status: string; progress: string; current: string;
        results: VideoData[]; error: string | null;
        magnet_files: MagnetFile[];
        magnet_torrent: MagnetTorrent | null;
        events: { msg: string; ts: number }[];
      };

      const job = await new Promise<MagnetJob>((resolve, reject) => {
        const poll = async () => {
          if (abortRef.current) {
            try {
              await fetch(`${API}/magnet/${job_id}/cancel`, { method: "POST" });
            } catch { /* ignore */ }
            reject(new Error("Cancelled."));
            return;
          }
          try {
            const r = await fetch(`${API}/job/${job_id}`);
            const j = await r.json() as MagnetJob;
            const last = j.events.at(-1);
            if (last) setProgressMsg(last.msg);
            if (j.status === "done") resolve(j);
            else if (j.status === "error") reject(new Error(j.error ?? "Magnet job failed."));
            else setTimeout(poll, 800);
          } catch (e) { reject(e); }
        };
        poll();
      });

      setMagnetFiles(job.magnet_files);
      setMagnetTorrent(job.magnet_torrent);
      const ranked = (job.results ?? []).filter(Boolean);
      setData(ranked);
      localStorage.setItem("last_results", JSON.stringify(ranked));
      setProgressMsg("");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Magnet analysis failed.");
      setData([]);
    } finally {
      abortRef.current = false;
      setIsLoading(false);
      setMagnetJobId(null);
      setJobId(null);
    }
  };

  // ── Multi-magnet comparison ─────────────────────────────────────────────
  const analyzeMagnetList = async () => {
    // Parse: one magnet per line, blank lines ignored.
    const magnets = magnetList
      .split(/\r?\n/)
      .map((l) => l.trim())
      .filter((l) => l.length > 0);

    if (magnets.length < 2) {
      setError("Paste at least 2 magnet URIs (one per line) to compare.");
      return;
    }
    if (magnets.length > 8) {
      setError("Up to 8 magnets at a time.");
      return;
    }
    const bad = magnets.findIndex((m) => !m.toLowerCase().startsWith("magnet:?"));
    if (bad >= 0) {
      setError(`Line ${bad + 1} doesn't look like a magnet URI.`);
      return;
    }

    setIsLoading(true);
    setError("");
    setCompareData(null);
    setComparePerMag([]);
    setCompareProgress("");
    // Switching into multi-magnet mode — wipe any prior single-file
    // dashboard so we don't render stale rows before the new results.
    setData([]);
    setMagnetFiles([]);
    setMagnetTorrent(null);

    try {
      const res = await fetch(`${API}/compare-magnets/?fast=${fastMode}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ magnets }),
      });
      const payload = await res.json();
      if (!res.ok) throw new Error(payload.detail ?? "compare-magnets request failed.");
      const { job_id } = payload as { job_id: string };
      setCompareJobId(job_id);

      type CompareJob = {
        status: string;
        progress: string;
        error: string | null;
        per_magnet: PerMagnetRecord[];
        comparison: ComparisonPayload | null;
      };

      const job = await new Promise<CompareJob>((resolve, reject) => {
        const poll = async () => {
          if (abortRef.current) {
            await fetch(`${API}/compare-magnets/${job_id}/cancel`, { method: "POST" });
            reject(new Error("Cancelled"));
            return;
          }
          try {
            const r = await fetch(`${API}/job/${job_id}`);
            const j = await r.json() as CompareJob;
            setComparePerMag(j.per_magnet ?? []);
            setCompareProgress(j.progress ?? "");
            if (j.status === "done") resolve(j);
            else if (j.status === "error") reject(new Error(j.error ?? "compare-magnets failed."));
            else setTimeout(poll, 1000);
          } catch (e) { reject(e); }
        };
        poll();
      });

      if (job.comparison) {
        setCompareData(job.comparison);
        // Promote each enriched release's underlying analysis into `data`
        // so the existing full dashboard renders too (radar chart, score
        // bars, bitrate chart, TV/USB panels, per-file detail cards).
        // The compare matrix above the dashboard gives the side-by-side
        // view; the dashboard below gives the deep-dive per release.
        const releaseAnalyses: VideoData[] = job.comparison.releases
          .map((r) => r.analysis)
          .filter(Boolean);
        // Annotate each with batch_rank so the existing rank pills work.
        releaseAnalyses.forEach((a, i) => { a.batch_rank = i + 1; });
        setData(releaseAnalyses);
        localStorage.setItem("last_results", JSON.stringify(releaseAnalyses));
      }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "compare-magnets failed.");
    } finally {
      abortRef.current = false;
      setIsLoading(false);
      setCompareJobId(null);
    }
  };

  // ── Unified Analyze dispatcher ────────────────────────────────────────────
  // Derive the active input mode from which field is populated. Only one
  // can be non-empty at any time thanks to the setExclusive* helpers above,
  // so the order here is just a tiebreaker for the initial render.
  type InputMode = "files" | "path" | "magnet" | "magnet-list" | null;
  const trimmedMagnetList = magnetList.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);

  // ── Multi-magnet chip helpers ──────────────────────────────────────────────
  // The textarea was replaced with a chip list so the input doesn't get
  // visually overwhelming after pasting 4-8 long URIs. We still store the
  // URIs as a newline-joined string in `magnetList` (so the existing
  // analyze/compare path keeps working unchanged), but the UI renders them
  // as "Magnet 1", "Magnet 2"… pills with × buttons.
  const extractMagnets = (text: string): string[] =>
    text.split(/[\s,;]+/)             // splits on whitespace, commas, semicolons
        .map((s) => s.trim())
        .filter((s) => s.toLowerCase().startsWith("magnet:?"));

  const addMagnetsFromText = (text: string) => {
    const incoming = extractMagnets(text);
    if (!incoming.length) return false;
    // Dedupe by exact URI match against what's already in the list.
    const existing = new Set(trimmedMagnetList);
    const merged   = [...trimmedMagnetList];
    for (const m of incoming) {
      if (!existing.has(m)) {
        existing.add(m);
        merged.push(m);
      }
    }
    if (merged.length === trimmedMagnetList.length) return false;  // all dupes
    setExclusiveMagnetList(merged.join("\n"));
    setError("");
    return true;
  };

  const removeMagnetAt = (i: number) => {
    const next = trimmedMagnetList.filter((_, idx) => idx !== i);
    setMagnetList(next.join("\n"));   // direct setter — clearing one chip
                                       // shouldn't wipe other input modes
  };

  const clearAllMagnets = () => setMagnetList("");

  // Local state for the chip-list input itself (the paste receiver).
  const [magnetInputValue, setMagnetInputValue] = useState("");
  const activeMode: InputMode =
    selectedFiles.length > 0 ? "files" :
    path.trim()              ? "path" :
    magnetUri.trim()         ? "magnet" :
    trimmedMagnetList.length ? "magnet-list" :
    null;

  const analyzeLabel = (() => {
    // Loading: show whichever in-flight job is running, with progress when
    // we have it. magnetJobId/compareJobId are set as soon as the request
    // is accepted; compareProgress is "i/N" updated by the SSE poller.
    if (isLoading) {
      if (magnetJobId)  return "Fetching torrent…";
      if (compareJobId) return compareProgress ? `Comparing… ${compareProgress}` : "Comparing…";
      return "Analyzing…";
    }
    switch (activeMode) {
      case "files":
        return selectedFiles.length > 1
          ? `Analyze ${selectedFiles.length} Files`
          : "Analyze File";
      case "path":         return "Analyze Path";
      case "magnet":       return "Analyze Magnet";
      case "magnet-list":  return `Compare ${trimmedMagnetList.length} Magnets`;
      default:             return "Analyze";
    }
  })();

  const handleAnalyze = () => {
    switch (activeMode) {
      case "files":
      case "path":         return void analyzeSelection();
      case "magnet":       return void analyzeMagnet();
      case "magnet-list":  return void analyzeMagnetList();
      default:             return;
    }
  };

  const analyzeDisabled = isLoading || activeMode === null;

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
                onSubmit={(e) => { e.preventDefault(); handleAnalyze(); }}
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
                  onChange={(e) => setExclusivePath(e.target.value)}
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

            {/* ── Magnet link row ─────────────────────────────────────── */}
            <div className="path-row">
              <label className="input-label" htmlFor="magnet-uri">
                Or paste a magnet link
              </label>
              <form
                className="search-form"
                onSubmit={(e) => { e.preventDefault(); handleAnalyze(); }}
              >
                <span className="search-icon-button" aria-hidden="true"
                      style={{ pointerEvents: "none" }}>
                  <svg width={17} height={16} viewBox="0 0 24 24" fill="none"
                       stroke="currentColor" strokeWidth="1.5"
                       strokeLinecap="round" strokeLinejoin="round">
                    <path d="M16 4h4v4"/>
                    <path d="M14 10l6-6"/>
                    <path d="M8 20H4v-4"/>
                    <path d="M10 14l-6 6"/>
                    <path d="M8 4h8a4 4 0 0 1 4 4v8"/>
                  </svg>
                </span>
                <input
                  id="magnet-uri"
                  className="search-input"
                  placeholder="magnet:?xt=urn:btih:…"
                  value={magnetUri}
                  onChange={(e) => setExclusiveMagnetUri(e.target.value)}
                  type="text"
                  autoComplete="off"
                  spellCheck={false}
                />
                <button
                  className="search-reset"
                  type="button"
                  aria-label="Clear magnet"
                  onClick={() => { setMagnetUri(""); setMagnetFiles([]); setMagnetTorrent(null); }}
                  disabled={!magnetUri && !magnetFiles.length}
                >
                  <svg xmlns="http://www.w3.org/2000/svg" className="reset-icon" fill="none"
                    viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12"/>
                  </svg>
                </button>
              </form>
            </div>

            {/* ── Multi-magnet comparison row ───────────────────────────── */}
            <div className="path-row">
              <label className="input-label" htmlFor="magnet-chip-input">
                Or compare multiple magnets (2–8)
                {trimmedMagnetList.length > 0 && (
                  <span style={{
                    marginLeft: 8, fontWeight: 400, fontSize: 12,
                    color: "var(--accent-bright)",
                  }}>
                    {trimmedMagnetList.length} added
                  </span>
                )}
              </label>
              <div className="magnet-chip-container">
                {/* Chip list — each pasted magnet collapses to "Magnet N" with
                    the full URI available on hover and a × to remove it. */}
                {trimmedMagnetList.length > 0 && (
                  <div className="magnet-chip-list">
                    {trimmedMagnetList.map((uri, i) => (
                      <span
                        key={`${i}-${uri.slice(-20)}`}
                        className="magnet-chip"
                        title={uri}
                      >
                        <MagnetLink uri={uri} size={11} title="Open this magnet" />
                        <span className="magnet-chip-label">Magnet {i + 1}</span>
                        <button
                          type="button"
                          className="magnet-chip-remove"
                          aria-label={`Remove magnet ${i + 1}`}
                          onClick={() => removeMagnetAt(i)}
                        >
                          ×
                        </button>
                      </span>
                    ))}
                    {trimmedMagnetList.length > 1 && (
                      <button
                        type="button"
                        className="magnet-chip-clear"
                        onClick={clearAllMagnets}
                      >
                        Clear all
                      </button>
                    )}
                  </div>
                )}
                {/* Paste receiver. The user can paste a single magnet, several
                    newline-separated magnets, or even a wall of mixed text —
                    only `magnet:?` URIs are extracted. */}
                <input
                  id="magnet-chip-input"
                  className="magnet-chip-input"
                  type="text"
                  spellCheck={false}
                  autoComplete="off"
                  placeholder={
                    trimmedMagnetList.length === 0
                      ? "Paste magnet link(s) — one or many at once"
                      : trimmedMagnetList.length >= 8
                        ? "Maximum 8 magnets — remove one to add another"
                        : "Paste another magnet…"
                  }
                  value={magnetInputValue}
                  disabled={trimmedMagnetList.length >= 8}
                  onChange={(e) => {
                    const v = e.target.value;
                    // If the typed/pasted value contains a magnet URI, absorb
                    // it into the chip list and clear the input.
                    if (extractMagnets(v).length > 0) {
                      addMagnetsFromText(v);
                      setMagnetInputValue("");
                    } else {
                      setMagnetInputValue(v);
                    }
                  }}
                  onPaste={(e) => {
                    // onChange handles it cleanly for most browsers, but on
                    // some Edge versions the paste fires before onChange sees
                    // the new value. Reading clipboardData here is a safety net.
                    const text = e.clipboardData?.getData("text") ?? "";
                    if (extractMagnets(text).length > 0) {
                      e.preventDefault();
                      addMagnetsFromText(text);
                      setMagnetInputValue("");
                    }
                  }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && magnetInputValue.trim()) {
                      e.preventDefault();
                      if (addMagnetsFromText(magnetInputValue)) {
                        setMagnetInputValue("");
                      }
                    }
                  }}
                />
              </div>
              {compareJobId && comparePerMag.length > 0 && (
                <div className="compare-status-list">
                  {comparePerMag.map((rec, i) => {
                    const statusClass =
                      rec.status === "done"  ? "compare-status-ok"  :
                      rec.status === "error" ? "compare-status-err" :
                                               "compare-status-busy";
                    return (
                      <div key={i} className="compare-status-row">
                        <MagnetLink uri={rec.magnet} size={12} />
                        <span>
                          [#{i + 1}] {rec.torrent?.name || rec.magnet.slice(0, 60) + "…"}
                          {" — "}
                          <span className={statusClass}>{rec.status}</span>
                          {rec.error && ` — ${rec.error}`}
                        </span>
                      </div>
                    );
                  })}
                </div>
              )}
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

          {jobId && progressMsg && (
            <div style={{
              margin: "12px 0 0", padding: "10px 14px",
              borderRadius: 10, background: "rgba(41,151,255,0.1)",
              border: "1px solid rgba(41,151,255,0.25)",
              fontSize: 13, color: "var(--accent-bright)",
            }}>
              ⏳ Analyzing: {progressMsg}
            </div>
          )}
          <div className="analyze-row" style={{ flexDirection: "column", gap: 8 }}>
            {/* Active-mode chip — tells the user which input the bottom
                button is about to act on. Hidden while nothing is selected
                so the empty state stays clean. */}
            {activeMode && !isLoading && (
              <div style={{
                fontSize: 11, color: "var(--soft-text)",
                textTransform: "uppercase", letterSpacing: 0.5,
              }}>
                Mode:{" "}
                <span style={{ color: "var(--accent-bright)", fontWeight: 600 }}>
                  {activeMode === "files"        ? `${selectedFiles.length} file(s)` :
                   activeMode === "path"         ? "Local path" :
                   activeMode === "magnet"       ? "Single magnet" :
                                                   `${trimmedMagnetList.length} magnets to compare`}
                </span>
              </div>
            )}
            <div style={{ display: "flex", gap: 12, justifyContent: "center" }}>
              <button
                onClick={handleAnalyze}
                disabled={analyzeDisabled}
                className="primary-button"
                title={
                  analyzeDisabled && !isLoading
                    ? "Pick files, a path, a magnet, or paste multiple magnets to enable."
                    : undefined
                }
              >
                {analyzeLabel}
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

      {magnetFiles.length > 0 && (
        <section id="magnet-results" className="dashboard-section">
          <div className="dashboard-inner">
            <div className="dash-header">
              <h2>Magnet Contents</h2>
              <p className="dash-sub" style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <MagnetLink uri={magnetUri || null} size={14}
                            title="Re-open this magnet in your torrent client" />
                <span>
                  {magnetTorrent?.name ?? "torrent"}{" "}
                  {magnetTorrent?.info_hash && (
                    <span style={{ opacity: 0.6, fontFamily: "monospace", fontSize: 12 }}>
                      · {magnetTorrent.info_hash}
                    </span>
                  )}
                </span>
              </p>
            </div>
            <div className="leaderboard">
              {magnetFiles.map((mf) => {
                const color =
                  mf.verdict === "good" ? "#30d158" :
                  mf.verdict === "bad"  ? "#ff453a" : "#ff9f0a";
                const verdictLabel =
                  mf.verdict === "good" ? (mf.ffprobe_ok ? "PLAYABLE" : "LIKELY OK")
                  : mf.verdict === "bad" ? "AVOID" : "SKIP";
                return (
                  <div key={mf.index} className="lb-row">
                    <span className="lb-rank" style={{ color }}>{verdictLabel}</span>
                    <div className="lb-file">
                      <p className="lb-name">{mf.name}</p>
                      <p className="lb-meta">
                        {mf.ext || "no ext"} · {mf.size_gb.toFixed(2)} GB
                        {mf.reasons.length > 0 && " · " + mf.reasons.join(" · ")}
                      </p>
                    </div>
                  </div>
                );
              })}
            </div>
            <p className="dash-sub" style={{ marginTop: 12 }}>
              Playable files appear in the dashboard below with full DV / HDR analysis.
              All torrent data has been cleaned up from disk.
            </p>
          </div>
        </section>
      )}

      {/* ── Multi-magnet comparison view ───────────────────────────────── */}
      {compareData && compareData.releases.length >= 2 && (
        <section id="compare" className="dashboard-section">
          <div className="dashboard-inner">
            <div className="dash-header">
              <h2>Magnet Comparison ({compareData.releases.length} releases)</h2>
            </div>

            {/* Winner banner */}
            {compareData.winner.winner_index !== null && (
              <div style={{
                margin: "12px 0 20px", padding: "14px 16px",
                // Theme-aware winner gradient — works on both dark and light.
                background: "var(--compare-winner-bg)",
                border: "1px solid var(--compare-winner-border)",
                borderRadius: 12,
              }}>
                <div style={{ fontSize: 11, color: "var(--soft-text)", marginBottom: 4,
                              textTransform: "uppercase", letterSpacing: 0.5 }}>
                  Recommended
                </div>
                <div style={{ fontSize: 16, fontWeight: 600, marginBottom: 8,
                              wordBreak: "break-all",
                              display: "flex", alignItems: "center", gap: 6 }}>
                  <MagnetLink
                    uri={compareData.winner.winner_index !== null
                      ? compareData.comparison_matrix.columns[compareData.winner.winner_index]?.magnet_uri ?? null
                      : null}
                    size={18}
                    title="Open winning magnet in torrent client"
                  />
                  <span>{compareData.winner.winner_name}</span>
                </div>
                <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13,
                             color: "var(--muted-text)" }}>
                  {compareData.winner.reasons.map((r, i) => (
                    <li key={i}>{r}</li>
                  ))}
                </ul>
              </div>
            )}

            {/* Column header cards */}
            <div style={{
              display: "grid",
              gridTemplateColumns: `180px repeat(${compareData.comparison_matrix.columns.length}, minmax(220px, 1fr))`,
              gap: 0, marginBottom: 12, alignItems: "stretch",
            }}>
              <div />
              {compareData.comparison_matrix.columns.map((col, i) => (
                <div key={i} style={{
                  padding: "10px 12px",
                  background: compareData.winner.winner_index === i
                    ? "var(--compare-winner-bg)" : "var(--compare-cell-bg)",
                  border: `1px solid ${compareData.winner.winner_index === i
                    ? "var(--compare-winner-border)" : "var(--compare-cell-border-strong)"}`,
                  borderRadius: 8, margin: 2,
                }}>
                  <div style={{ fontSize: 11, color: "var(--soft-text)", marginBottom: 4,
                                display: "flex", alignItems: "center", gap: 4 }}>
                    <MagnetLink uri={col.magnet_uri} size={13} />
                    <span>#{i + 1} · score {col.composite_score}/100</span>
                  </div>
                  <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 6,
                                wordBreak: "break-all", lineHeight: 1.3 }}>
                    {col.name}
                  </div>
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
                    {col.badges.map((b, bi) => (
                      <span key={bi} style={{
                        fontSize: 10, padding: "2px 6px", borderRadius: 4,
                        background: "rgba(41,151,255,0.18)",
                        border: "1px solid rgba(41,151,255,0.3)",
                      }}>{b}</span>
                    ))}
                  </div>
                  {(col.error_count > 0 || col.warn_count > 0) && (
                    <div style={{
                      marginTop: 6, fontSize: 11,
                      color: col.error_count > 0
                        ? "var(--severity-error)" : "var(--severity-warn)",
                    }}>
                      {col.error_count > 0 && `${col.error_count} error(s) `}
                      {col.warn_count  > 0 && `${col.warn_count} warning(s)`}
                    </div>
                  )}
                </div>
              ))}
            </div>

            {/* Comparison matrix */}
            <div style={{
              display: "grid",
              gridTemplateColumns: `180px repeat(${compareData.comparison_matrix.columns.length}, minmax(220px, 1fr))`,
              gap: 0,
              border: "1px solid var(--compare-cell-border)",
              borderRadius: 8, overflow: "hidden",
            }}>
              {compareData.comparison_matrix.rows.map((row, ri) => (
                <React.Fragment key={ri}>
                  <div style={{
                    padding: "10px 12px",
                    background: ri % 2 ? "var(--compare-cell-bg)" : "var(--compare-cell-bg-alt)",
                    fontSize: 12, color: "var(--muted-text)", fontWeight: 500,
                    borderBottom: "1px solid var(--compare-cell-border)",
                  }}>
                    {row.label}
                  </div>
                  {row.values.map((v, ci) => (
                    <div key={ci} style={{
                      padding: "10px 12px",
                      background: row.highlight === ci
                        ? "var(--compare-winner-bg)"
                        : ri % 2 ? "var(--compare-cell-bg)" : "var(--compare-cell-bg-alt)",
                      fontSize: 12,
                      color: "var(--page-text)",
                      borderBottom: "1px solid var(--compare-cell-border)",
                      borderLeft: "1px solid var(--compare-cell-border)",
                      fontWeight: row.highlight === ci ? 600 : 400,
                    }}>
                      {v}
                    </div>
                  ))}
                </React.Fragment>
              ))}
            </div>

            {/* Verification flags per release */}
            <h3 style={{ marginTop: 24, marginBottom: 10, fontSize: 14 }}>
              Verification Flags
            </h3>
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              {compareData.releases.map((rel, i) => (
                <div key={i} style={{
                  padding: "10px 12px",
                  background: "var(--compare-cell-bg)",
                  border: "1px solid var(--compare-cell-border-strong)",
                  borderRadius: 8,
                }}>
                  <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 6,
                                wordBreak: "break-all",
                                color: "var(--page-text)",
                                display: "flex", alignItems: "center", gap: 4 }}>
                    <MagnetLink uri={rel.magnet_uri} size={13} />
                    <span>#{i + 1} · {rel.name}</span>
                  </div>
                  <div style={{ fontSize: 12, color: "var(--muted-text)", marginBottom: 4 }}>
                    {rel.verification.summary} · trust {rel.verification.trust_score}/100
                  </div>
                  {rel.verification.flags.length > 0 && (
                    <ul style={{ margin: "6px 0 0", paddingLeft: 18, fontSize: 12 }}>
                      {rel.verification.flags.map((f, fi) => (
                        <li key={fi} style={{
                          // Severity colors are theme-aware — see App.css
                          // `--severity-*` vars. Dark mode uses brighter
                          // shades, light mode uses darker for WCAG AA.
                          color: f.severity === "error" ? "var(--severity-error)"
                               : f.severity === "warn"  ? "var(--severity-warn)"
                               : "var(--severity-info)",
                        }}>
                          <strong>[{f.severity}]</strong> {f.message}
                          {f.evidence && (
                            <div style={{ fontSize: 10, color: "var(--soft-text)",
                                          fontFamily: "monospace", marginTop: 2 }}>
                              {f.evidence}
                            </div>
                          )}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              ))}
            </div>
          </div>
        </section>
      )}

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
                  return (
                    <div key={item.path} className={`lb-row ${isBest ? "lb-best" : ""}`}>
                      <span className="lb-rank" style={{ color }}># {item.batch_rank ?? fi + 1}</span>
                      <div className="lb-file">
                        <p className="lb-name">{cleanFileName(item.file)}</p>
                        <p className="lb-meta">
                          DV {item.dv_profile} · {item.source} · {(item.bitrate_mbps ?? 0).toFixed(1)} Mbps
                        </p>
                      </div>
                      <div className="lb-scores">
                        <span
                          className="lb-score-tv"
                          style={{ color }}
                          title="TV Score: calibrated for Sony Bravia 8 Mark II USB playback. 80+ = Excellent, 65+ = Very Good, 50+ = Good."
                        >
                          TV {item.tv_score ?? "–"}
                        </span>
                        <span
                          className="lb-score-q"
                          title="Quality Score: overall source fidelity independent of TV. Considers DV profile, bitrate, bit depth, audio, source type."
                        >
                          Q {item.score}
                        </span>
                        <span className="lb-verdict">{getVerdict(item, isBest)}</span>
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