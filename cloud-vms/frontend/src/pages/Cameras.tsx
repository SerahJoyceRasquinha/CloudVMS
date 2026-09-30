import { useState } from "react";
import { Link } from "react-router-dom";
import { del, errorText, get, patch, post } from "../api";
import { useAuth } from "../auth";
import { Dialog, Empty, ErrorBox, StatusBadge, useToast } from "../components/ui";
import { fmtAgo, useLoad } from "../hooks";
import type { Camera } from "../types";

export default function Cameras() {
  const { can } = useAuth();
  const toast = useToast();
  const { data: cams, error, reload } = useLoad(() => get<Camera[]>("/cameras"), [], 5000);
  const [editing, setEditing] = useState<Camera | "new" | null>(null);
  const manage = can("cameras:manage");

  const toggle = async (c: Camera) => {
    try { await post(`/cameras/${c.id}/stream/${c.enabled ? "stop" : "start"}`); reload(); }
    catch (e) { toast(errorText(e), "err"); }
  };
  const remove = async (c: Camera) => {
    if (!confirm(`Delete camera "${c.name}"?\n\nEverything it recorded is erased too: its people and vehicle counts on the ` +
      "Gate overview, incidents, recordings and zones. This cannot be undone.")) return;
    try {
      await del(`/cameras/${c.id}`);
      toast(`Deleted ${c.name} and its statistics`);
      reload();
    } catch (e) { toast(errorText(e), "err"); }
  };

  return (
    <>
      <div className="page-head">
        <div><h1>Cameras</h1><p>Register gate cameras or upload recorded footage to run through the same pipeline.</p></div>
        <span className="spacer" />
        {manage && <button className="btn-primary" onClick={() => setEditing("new")}>Add camera</button>}
      </div>
      <ErrorBox error={error} />
      <section className="panel">
        <div className="body table-wrap">
          {cams && cams.length === 0 ? (
            <Empty title="No cameras registered">Add an RTSP camera, or upload one of your college gate videos to start.</Empty>
          ) : (
            <table>
              <thead><tr><th>Camera</th><th>Source</th><th>Status</th><th>Analytics</th><th>Recording</th><th className="num">Zones</th><th>Last frame</th><th /></tr></thead>
              <tbody>
                {cams?.map((c) => (
                  <tr key={c.id}>
                    <td><b>{c.name}</b>{c.location && <div className="small muted">{c.location}</div>}</td>
                    <td className="small"><span className="badge">{c.stream_type}</span> <span className="muted">{c.stream_reference}</span>{c.has_credentials && <span className="muted"> (with login)</span>}</td>
                    <td><StatusBadge status={c.enabled ? c.status : "DISABLED"} />{c.status_message && c.enabled && <div className="small muted">{c.status_message}</div>}</td>
                    <td className="small">{c.analytics_enabled ? `${c.analytics_config.inference_fps} fps` : "off"}</td>
                    <td className="small">{c.recording_enabled ? "on" : "off"}</td>
                    <td className="num"><Link to={`/zones?camera=${c.id}`}>{c.zone_count}</Link></td>
                    <td className="small muted">{fmtAgo(c.last_seen_at)}</td>
                    <td style={{ whiteSpace: "nowrap", textAlign: "right" }}>
                      {can("streams:control") && <button onClick={() => toggle(c)}>{c.enabled ? "Stop" : "Start"}</button>}{" "}
                      {manage && <button className="btn-quiet" onClick={() => setEditing(c)}>Edit</button>}
                      {manage && <button className="btn-quiet btn-danger" onClick={() => remove(c)}>Delete</button>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </section>
      {editing && <CameraForm cam={editing === "new" ? null : editing} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); reload(); }} />}
    </>
  );
}

function CameraForm({ cam, onClose, onSaved }: { cam: Camera | null; onClose: () => void; onSaved: () => void }) {
  const toast = useToast();
  const a = cam?.analytics_config;
  const [v, setV] = useState({
    name: cam?.name || "", location: cam?.location || "", description: cam?.description || "",
    stream_type: (cam?.stream_type || "file") as Camera["stream_type"], stream_reference: cam?.stream_reference || "",
    username: "", password: "", live_url: cam?.live_url || "", worker_group: cam?.worker_group || "default",
    analytics_enabled: cam?.analytics_enabled ?? true, recording_enabled: cam?.recording_enabled ?? false,
    enabled: cam?.enabled ?? true,
    inference_fps: a?.inference_fps ?? 5, imgsz: a?.imgsz ?? 640, detector_conf: a?.detector_conf ?? 0.15,
    reid_enabled: a?.reid_enabled ?? true, rider_suppression: a?.rider_suppression ?? true,
    reid_memory_minutes: Math.round((a?.reid_memory_seconds ?? 1800) / 60),
    evidence_pre_seconds: a?.evidence_pre_seconds ?? 5, evidence_post_seconds: a?.evidence_post_seconds ?? 8,
    realtime: a?.realtime ?? true, loop: a?.loop ?? true,
    segment_seconds: cam?.recording_config?.segment_seconds ?? 60, rec_fps: cam?.recording_config?.fps ?? 10,
    max_height: cam?.recording_config?.max_height ?? 720,
  });
  const [err, setErr] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [uploadInfo, setUploadInfo] = useState<string>("");
  const set = (k: keyof typeof v, val: any) => setV((x) => ({ ...x, [k]: val }));

  const upload = async (file: File) => {
    setBusy(true);
    setUploadInfo(`Uploading ${file.name}`);
    const fd = new FormData();
    fd.append("file", file);
    try {
      const r = await post<{ stream_reference: string; width: number; height: number; fps: number; duration_s: number }>("/cameras/upload-video", fd);
      set("stream_reference", r.stream_reference);
      if (!v.name) set("name", file.name.replace(/\.[^.]+$/, ""));
      setUploadInfo(`${r.width}×${r.height}, ${r.fps} fps, ${r.duration_s ?? "?"} s`);
    } catch (e) { setUploadInfo(""); setErr(e); } finally { setBusy(false); }
  };

  const body = () => ({
    name: v.name, location: v.location, description: v.description, stream_type: v.stream_type,
    stream_reference: v.stream_reference, live_url: v.live_url, worker_group: v.worker_group,
    analytics_enabled: v.analytics_enabled, recording_enabled: v.recording_enabled,
    ...(v.username || v.password ? { username: v.username, password: v.password } : {}),
    analytics_config: { inference_fps: +v.inference_fps, imgsz: +v.imgsz, detector_conf: +v.detector_conf,
      reid_enabled: v.reid_enabled, rider_suppression: v.rider_suppression,
      reid_memory_seconds: Math.max(0, +v.reid_memory_minutes) * 60,
      evidence_pre_seconds: +v.evidence_pre_seconds, evidence_post_seconds: +v.evidence_post_seconds,
      realtime: v.realtime, loop: v.loop },
    recording_config: { segment_seconds: +v.segment_seconds, fps: +v.rec_fps, max_height: +v.max_height },
  });

  const test = async () => {
    setErr(null);
    try {
      const r = await post<{ ok: boolean; message?: string; width?: number; height?: number; fps?: number }>("/cameras/test-connection", { ...body(), enabled: false });
      if (r.ok) toast(`Connected: ${r.width}×${r.height} at ${r.fps} fps`);
      else toast(`Could not connect: ${r.message}`, "err");
    } catch (e) { setErr(e); }
  };

  const save = async () => {
    setBusy(true);
    setErr(null);
    try {
      if (cam) await patch(`/cameras/${cam.id}`, body());
      else await post("/cameras", { ...body(), enabled: v.enabled });
      toast(cam ? "Camera updated" : "Camera added");
      onSaved();
    } catch (e) { setErr(e); } finally { setBusy(false); }
  };

  const isFile = v.stream_type === "file";
  return (
    <Dialog title={cam ? `Edit ${cam.name}` : "Add camera"} onClose={onClose} wide
      footer={<><button onClick={test} disabled={!v.stream_reference || busy}>Test connection</button><span style={{ flex: 1 }} />
        <button onClick={onClose}>Cancel</button><button className="btn-primary" onClick={save} disabled={busy || !v.name || !v.stream_reference}>{cam ? "Save changes" : "Add camera"}</button></>}>
      <div className="grid" style={{ gap: 14 }}>
        <div className="form-grid">
          <label className="field">Name<input value={v.name} onChange={(e) => set("name", e.target.value)} placeholder="Main gate, inbound" /></label>
          <label className="field">Location<input value={v.location} onChange={(e) => set("location", e.target.value)} placeholder="RVCE main entrance" /></label>
          <label className="field">Source
            <select value={v.stream_type} onChange={(e) => set("stream_type", e.target.value)}>
              <option value="file">Recorded video (replayed like a live camera)</option>
              <option value="rtsp">IP camera (RTSP)</option>
              <option value="http">HTTP / HLS stream</option>
              <option value="webcam">Webcam on the server</option>
            </select>
          </label>
          {isFile ? (
            <label className="field">Video file
              <input type="file" accept="video/*,.dav,.h264,.ts" onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])} />
              <span className="hint">{uploadInfo || (v.stream_reference ? `Using ${v.stream_reference}` : "MP4, AVI, MKV, MOV")}</span>
            </label>
          ) : (
            <label className="field">{v.stream_type === "webcam" ? "Device index" : "Stream URL (no password in the URL)"}
              <input value={v.stream_reference} onChange={(e) => set("stream_reference", e.target.value)}
                placeholder={v.stream_type === "rtsp" ? "rtsp://192.168.1.64:554/Streaming/Channels/101" : v.stream_type === "webcam" ? "0" : "https://..."} />
            </label>
          )}
          {(v.stream_type === "rtsp" || v.stream_type === "http") && (<>
            <label className="field">Camera username<input value={v.username} onChange={(e) => set("username", e.target.value)} autoComplete="off" placeholder={cam?.has_credentials ? "unchanged" : ""} /></label>
            <label className="field">Camera password<input type="password" value={v.password} onChange={(e) => set("password", e.target.value)} autoComplete="new-password" placeholder={cam?.has_credentials ? "unchanged" : ""} /></label>
          </>)}
          <label className="field full">Description<input value={v.description} onChange={(e) => set("description", e.target.value)} /></label>
        </div>

        <fieldset>
          <legend>Analytics</legend>
          <div className="form-grid">
            <label className="check full"><input type="checkbox" checked={v.analytics_enabled} onChange={(e) => set("analytics_enabled", e.target.checked)} />Detect, track and count people and vehicles</label>
            <label className="field">Frames analysed per second<input type="number" min={0.2} max={30} step={0.5} value={v.inference_fps} onChange={(e) => set("inference_fps", e.target.value)} /><span className="hint">5 is a good start on a laptop CPU.</span></label>
            <label className="field">Model input size<select value={v.imgsz} onChange={(e) => set("imgsz", e.target.value)}>{[480, 640, 800, 960, 1280].map((s) => <option key={s}>{s}</option>)}</select><span className="hint">Larger finds distant people, costs time.</span></label>
            <label className="check"><input type="checkbox" checked={v.reid_enabled} onChange={(e) => set("reid_enabled", e.target.checked)} />Count each person / vehicle once (appearance re-identification)</label>
            <label className="field">Recognise returning people for (minutes)<input type="number" min={0} max={1440} value={v.reid_memory_minutes} disabled={!v.reid_enabled} onChange={(e) => set("reid_memory_minutes", e.target.value)} /><span className="hint">Somebody who leaves the view and comes back within this time is not counted again.</span></label>
            <label className="check"><input type="checkbox" checked={v.rider_suppression} onChange={(e) => set("rider_suppression", e.target.checked)} />Don't count riders on two-wheelers as pedestrians</label>
            <label className="field">Evidence before incident (s)<input type="number" min={0} max={60} value={v.evidence_pre_seconds} onChange={(e) => set("evidence_pre_seconds", e.target.value)} /></label>
            <label className="field">Evidence after incident (s)<input type="number" min={1} max={120} value={v.evidence_post_seconds} onChange={(e) => set("evidence_post_seconds", e.target.value)} /></label>
            {isFile && <>
              <label className="check"><input type="checkbox" checked={v.realtime} onChange={(e) => set("realtime", e.target.checked)} />Play at real speed (simulated live camera)</label>
              <label className="check"><input type="checkbox" checked={v.loop} onChange={(e) => set("loop", e.target.checked)} />Loop the video</label>
            </>}
          </div>
        </fieldset>

        <fieldset>
          <legend>Recording and live view</legend>
          <div className="form-grid">
            <label className="check full"><input type="checkbox" checked={v.recording_enabled} onChange={(e) => set("recording_enabled", e.target.checked)} disabled={v.stream_type === "webcam"} />Record continuously in segments</label>
            <label className="field">Segment length (s)<input type="number" min={2} max={3600} value={v.segment_seconds} onChange={(e) => set("segment_seconds", e.target.value)} /></label>
            <label className="field">Recording frame rate<input type="number" min={1} max={30} value={v.rec_fps} onChange={(e) => set("rec_fps", e.target.value)} /></label>
            <label className="field">Maximum recording height (px)<select value={v.max_height} onChange={(e) => set("max_height", e.target.value)}>{[360, 480, 720, 1080].map((s) => <option key={s}>{s}</option>)}</select></label>
            <label className="field">Worker group<input value={v.worker_group} onChange={(e) => set("worker_group", e.target.value)} /><span className="hint">Which worker machine processes this camera.</span></label>
            <label className="field full">Raw live stream URL (optional, MediaMTX HLS)<input value={v.live_url} onChange={(e) => set("live_url", e.target.value)} placeholder="http://media-server:8888/gate1/index.m3u8" /></label>
          </div>
        </fieldset>
        {!cam && <label className="check"><input type="checkbox" checked={v.enabled} onChange={(e) => set("enabled", e.target.checked)} />Start the stream after saving</label>}
        {err ? <ErrorBox error={err} /> : null}
      </div>
    </Dialog>
  );
}
