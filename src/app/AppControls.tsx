import { useEffect, useState } from "react";
import { appControl, fetchActiveJob, fetchAppStatus } from "./api";
import type { ActiveJob, AppStatus } from "./api";

export type AppControlsProps = { status: AppStatus; onReconnect: () => void | Promise<void> };
const terminal = new Set(["done", "error", "cancelled", "gone"]);

export function AppControls({ status, onReconnect }: AppControlsProps) {
  const [updates, setUpdates] = useState<Map<string, Partial<ActiveJob>>>(new Map());
  const jobs = status.active_jobs.map(job => ({ ...job, ...updates.get(status.session_id + job.job_id) }));
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const subscription = JSON.stringify(status.active_jobs.map(job => job.job_id));
  useEffect(() => {
    let disposed = false;
    const timers = new Set<ReturnType<typeof setTimeout>>();
    function schedule(callback: () => void, delay: number) {
      const timer = setTimeout(() => { timers.delete(timer); callback(); }, delay);
      timers.add(timer);
    }
    const active = JSON.parse(subscription) as string[];
    for (const id of active) {
      const poll = async () => {
        try {
          const fresh = await fetchActiveJob(id);
          if (disposed) return;
          setUpdates(old => new Map(old).set(status.session_id + id, fresh));
          if (!terminal.has(fresh.status)) schedule(() => { void poll(); }, 1000);
        } catch { if (!disposed) setMessage("Job connection lost. Reconnect to retry."); }
      };
      // Delay permits one render of the server census before updates arrive.
      schedule(() => { void poll(); }, 0);
    }
    return () => { disposed = true; timers.forEach(clearTimeout); };
  }, [subscription, status.session_id, revision]);

  async function control(mode: "keep" | "when_finished" | "stop") {
    setBusy(true);
    try {
      await appControl(status, "quit", { mode });
      setMessage(mode === "keep" ? "App will keep running." : mode === "stop"
        ? "Stopping owned work. Check app status for cleanup errors." : "App will quit when work finishes.");
      await onReconnect();
    } catch (error) { setMessage(error instanceof Error ? error.message : "App control failed."); }
    finally { setBusy(false); }
  }
  return <section aria-label="App controls">
    <p>You can close this tab. Reopen the shortcut to return. Use Quit to stop the app.</p>
    <p>Active install: {status.install_id}. {status.quit_pending ? "Quit pending; new work is blocked." : "App running."}</p>
    {status.checks.filter(c => c.status !== "ok").map(c => <p key={c.id} role="alert">{c.message}</p>)}
    <ul>{jobs.map(j => <li key={j.job_id}>{j.kind}: {j.current} — {j.status}</li>)}</ul>
    <button type="button" onClick={() => { setRevision(n => n + 1); void onReconnect(); }}>Reconnect</button>
    {jobs.some(j => !terminal.has(j.status)) && <p>Quit choices apply to the active work listed above.</p>}
    <button type="button" disabled={busy} onClick={() => { void control("keep"); }}>Keep running</button>
    <button type="button" disabled={busy} onClick={() => { void control("when_finished"); }}>Quit when finished</button>
    <button type="button" disabled={busy} onClick={() => { void control("stop"); }}>Stop jobs and quit</button>
    <button type="button" disabled={busy} onClick={() => {
      void appControl(status, "open-log", {}).then(() => setMessage("Log opened."), () => setMessage("Could not open app log."));
    }}>Open log</button>
    <p role="status">{message}</p>
  </section>;
}

function ConnectedApp() {
  const [status, setStatus] = useState<AppStatus | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    async function refresh() {
      try {
        const fresh = await fetchAppStatus();
        if (!disposed) { setStatus(fresh); setError(""); }
      } catch { if (!disposed) setError("App connection lost. Reopen the shortcut to return."); }
      if (!disposed) timer = setTimeout(() => { void refresh(); }, 2000);
    }
    void refresh();
    return () => { disposed = true; clearTimeout(timer); };
  }, []);
  async function reconnect() {
    try { setStatus(await fetchAppStatus()); setError(""); }
    catch { setError("App connection lost. Reopen the shortcut to return."); }
  }
  return <>{status && <AppControls status={status} onReconnect={reconnect} />}{error && <p role="alert">{error}</p>}</>;
}

export function AppConnection({ enabled = import.meta.env.PROD }: { enabled?: boolean }) {
  return enabled ? <ConnectedApp /> : null;
}
