import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { get, getToken, post } from "../api";
import { useAuth } from "../auth";
import { Empty, ErrorBox, StatusBadge, useToast } from "../components/ui";
import { fmtAgo, useLoad } from "../hooks";
import type { Camera } from "../types";

export default function Live() {
  const { can } = useAuth();
  const { data: cams, error, reload } = useLoad(() => get<Camera[]>("/cameras"), [], 5000);
  const [focus, setFocus] = useState<number | null>(null);
  if (error) return <ErrorBox error={error} />;
  if (!cams) return <p className="muted">Loading cameras</p>;
  const shown = focus ? cams.filter((c) => c.id === focus) : cams;
  return (
    <>
      <div className="page-head">
        <div><h1>Live view</h1><p>Boxes, zones and counts are drawn by the analytics worker on the frames it analysed.</p></div>
        <span className="spacer" />
        {focus && <button onClick={() => setFocus(null)}>Show all cameras</button>}
      </div>
      {cams.length === 0 ? (
        <div className="panel"><Empty title="No cameras to watch yet">{can("cameras:manage") && <Link to="/cameras">Add a camera</Link>}</Empty></div>
      ) : (
        <div className="live-grid" style={focus ? { gridTemplateColumns: "1fr" } : undefined}>
          {shown.map((c) => <LiveTile key={c.id} cam={c} onFocus={() => setFocus(focus ? null : c.id)} canControl={can("streams:control")} onChanged={reload} />)}
        </div>
      )}
    </>
  );
}

function LiveTile({ cam, onFocus, canControl, onChanged }: { cam: Camera; onFocus: () => void; canControl: boolean; onChanged: () => void }) {
  const [mode, setMode] = useState<"analytics" | "hls">("analytics");
  const toast = useToast();
  const liveUrl = cam.live_url || null;

  const toggle = async () => {
    try {
      await post(`/cameras/${cam.id}/stream/${cam.enabled ? "stop" : "start"}`);
      toast(cam.enabled ? `Stopped ${cam.name}` : `Starting ${cam.name}`);
      onChanged();
    } catch (e) { toast(String((e as Error).message), "err"); }
  };

  return (
    <article className="live-tile">
      <div className="frame">
        {!cam.enabled ? <span>Stream stopped</span>
          : mode === "hls" && liveUrl ? <HlsPlayer url={liveUrl} />
          : <LiveFrame cameraId={cam.id} alt={`Live view of ${cam.name}`} />}
      </div>
      <footer>
        <b>{cam.name}</b><StatusBadge status={cam.enabled ? cam.status : "DISABLED"} />
        <span className="small muted">{cam.status === "ONLINE" ? "" : cam.status_message || (cam.last_seen_at ? `last frame ${fmtAgo(cam.last_seen_at)}` : "")}</span>
        <span className="spacer" />
        {liveUrl && cam.enabled && <button className="btn-quiet" onClick={() => setMode(mode === "hls" ? "analytics" : "hls")}>{mode === "hls" ? "Analytics view" : "Raw stream"}</button>}
        <button className="btn-quiet" onClick={onFocus}>Enlarge</button>
        {canControl && <button onClick={toggle}>{cam.enabled ? "Stop" : "Start"}</button>}
      </footer>
    </article>
  );
}

/** Annotated live frames, fetched one at a time (long-poll) rather than as an MJPEG stream.
 * An <img> showing MJPEG keeps its connection open after the page is left, and browsers allow only
 * 6 connections per server: a few visits to Live view used to leave every later request queued
 * ("Loading cameras" forever). Here every request is short and is cancelled on unmount. */
function LiveFrame({ cameraId, alt }: { cameraId: number; alt: string }) {
  const [url, setUrl] = useState<string | null>(null);
  const [stalled, setStalled] = useState(false);
  useEffect(() => {
    let stopped = false;
    let ctrl: AbortController | null = null;
    let current: string | null = null;
    let after = 0;
    let failures = 0;
    const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
    const run = async () => {
      while (!stopped) {
        if (document.visibilityState !== "visible") { await sleep(500); continue; }
        ctrl = new AbortController();
        const timer = setTimeout(() => ctrl?.abort(), 8000);
        try {
          const res = await fetch(`/api/live/${cameraId}/frame?after=${after}&wait=1`, {
            headers: { Authorization: `Bearer ${getToken()}` }, signal: ctrl.signal, cache: "no-store",
          });
          if (!res.ok) throw new Error(String(res.status));
          const ts = parseFloat(res.headers.get("X-Frame-Ts") || "0");
          const blob = await res.blob();
          if (stopped) break;
          if (ts !== after || !current) {
            after = ts;
            const next = URL.createObjectURL(blob);
            setUrl(next);
            if (current) URL.revokeObjectURL(current);
            current = next;
          }
          failures = 0;
          setStalled(false);
        } catch {
          if (stopped) break;
          failures += 1;
          if (failures >= 3) setStalled(true);
          await sleep(Math.min(5000, 500 * failures));
        } finally {
          clearTimeout(timer);
        }
      }
    };
    run();
    return () => { stopped = true; ctrl?.abort(); if (current) URL.revokeObjectURL(current); };
  }, [cameraId]);
  if (!url) return <span>{stalled ? "Reconnecting" : "Connecting"}</span>;
  return <img src={url} alt={alt} style={stalled ? { opacity: 0.5 } : undefined} />;
}

function HlsPlayer({ url }: { url: string }) {
  const ref = useRef<HTMLVideoElement>(null);
  useEffect(() => {
    const v = ref.current;
    if (!v) return;
    if (v.canPlayType("application/vnd.apple.mpegurl")) { v.src = url; return; }
    let hls: any;
    import("hls.js").then(({ default: Hls }) => {
      if (Hls.isSupported()) { hls = new Hls({ lowLatencyMode: true }); hls.loadSource(url); hls.attachMedia(v); }
    });
    return () => hls?.destroy();
  }, [url]);
  return <video ref={ref} autoPlay muted playsInline />;
}
