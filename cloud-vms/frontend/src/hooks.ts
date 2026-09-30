import { useCallback, useEffect, useRef, useState } from "react";
import { post } from "./api";

/** Load data, optionally re-polling every `intervalMs`.
 * Polls never overlap (a slow server isn't flooded), a response for old `deps` never overwrites
 * newer data, and nothing is updated after the page is left. */
export function useLoad<T>(fn: () => Promise<T>, deps: unknown[], intervalMs?: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const gen = useRef(0); // bumped when deps change or the component unmounts
  const inflight = useRef(false);
  const reload = useCallback(async (poll = false) => {
    if (poll && inflight.current) return;
    const g = gen.current;
    inflight.current = true;
    try {
      const d = await fnRef.current();
      if (g !== gen.current) return;
      setData(d);
      setError(null);
      setUpdatedAt(Date.now());
    } catch (e) {
      if (g === gen.current) setError(e);
    } finally {
      if (g === gen.current) { inflight.current = false; setLoading(false); }
    }
  }, []);
  useEffect(() => {
    gen.current += 1;
    inflight.current = false;
    setLoading(true);
    reload();
    const t = intervalMs
      ? setInterval(() => { if (document.visibilityState === "visible") reload(true); }, intervalMs)
      : undefined;
    // refresh straight away when the browser tab becomes visible again
    const onVisible = () => { if (intervalMs && document.visibilityState === "visible") reload(true); };
    document.addEventListener("visibilitychange", onVisible);
    return () => { gen.current += 1; clearInterval(t); document.removeEventListener("visibilitychange", onVisible); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  const manualReload = useCallback(() => reload(false), [reload]);
  return { data, error, reload: manualReload, loading, setData, updatedAt };
}

export interface LiveIncident {
  id: number; camera_id: number; camera_name: string; event_type: string; title: string;
  severity: string; status: string; event_timestamp: string;
}

/** Server-Sent Events feed of new incidents (reconnects with a fresh short-lived token). */
export function useIncidentStream(onIncident: (e: LiveIncident) => void, enabled: boolean) {
  const cb = useRef(onIncident);
  cb.current = onIncident;
  useEffect(() => {
    if (!enabled) return;
    let es: EventSource | null = null;
    let stopped = false;
    let retry: ReturnType<typeof setTimeout>;
    const connect = async () => {
      try {
        const { token } = await post<{ token: string }>("/events/stream-token");
        if (stopped) return;
        es = new EventSource(`/api/events/stream?token=${encodeURIComponent(token)}`);
        es.addEventListener("incident", (m) => cb.current(JSON.parse((m as MessageEvent).data)));
        es.onerror = () => { es?.close(); if (!stopped) retry = setTimeout(connect, 5000); };
      } catch {
        if (!stopped) retry = setTimeout(connect, 10000);
      }
    };
    connect();
    return () => { stopped = true; clearTimeout(retry); es?.close(); };
  }, [enabled]);
}

export function useClock(ms = 1000) {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => { const t = setInterval(() => setNow(new Date()), ms); return () => clearInterval(t); }, [ms]);
  return now;
}

// ---------------------------------------------------------------- formatting
export const fmtTime = (iso?: string | null) =>
  iso ? new Date(iso).toLocaleString(undefined, { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—";
export const fmtClock = (iso?: string | null) =>
  iso ? new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }) : "—";
export const fmtAgo = (iso?: string | null) => {
  if (!iso) return "never";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
};
export const fmtBytes = (b: number) => {
  if (!b) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(u.length - 1, Math.floor(Math.log(b) / Math.log(1024)));
  return `${(b / 1024 ** i).toFixed(i ? 1 : 0)} ${u[i]}`;
};
export const fmtDuration = (s: number | null | undefined) => {
  if (s == null) return "—";
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
  return `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m`;
};
export const label = (s: string) =>
  s.replace(/_DETECTED$/, "").replace(/_/g, " ").toLowerCase().replace(/^\w/, (c) => c.toUpperCase());
export const className = (s: string) => s.replace(/_/g, "-");

export const EVENT_LABEL: Record<string, string> = {
  INTRUSION_DETECTED: "Intrusion",
  RESTRICTED_AREA_ACCESS: "Restricted-area access",
  LOITERING_DETECTED: "Loitering",
  CROWD_DETECTED: "Crowd",
  CAMERA_OFFLINE: "Camera offline",
  RECORDING_FAILURE: "Recording failure",
};
export const eventLabel = (t: string) => EVENT_LABEL[t] || label(t);

export const CLASS_LABEL: Record<string, string> = {
  person: "Person", bicycle: "Bicycle", two_wheeler: "Two-wheeler", three_wheeler: "Auto-rickshaw", car: "Car",
  van: "Van / tempo", lcv: "Light goods", bus: "Bus", truck: "Truck",
};
export const classLabel = (c: string) => CLASS_LABEL[c] || label(c);

export function localInputValue(d: Date) {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
