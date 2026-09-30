import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { download, errorText, get, patch } from "../api";
import { useAuth } from "../auth";
import { Drawer, Empty, ErrorBox, EventStatusBadge, GroupTag, Pager, SevBadge, useToast } from "../components/ui";
import { EVENT_LABEL, eventLabel, fmtTime, localInputValue, useLoad } from "../hooks";
import type { Camera, EventItem, Evidence, Page } from "../types";

const STATUSES = ["NEW", "ACKNOWLEDGED", "INVESTIGATING", "RESOLVED", "DISMISSED"];
const SEVERITIES = ["critical", "high", "medium", "low"];
const OBJECTS = ["person", "two_wheeler", "three_wheeler", "car", "van", "lcv", "bus", "truck", "bicycle", "system"];

export default function Events() {
  const [params, setParams] = useSearchParams();
  const [page, setPage] = useState(1);
  const [sort, setSort] = useState<{ key: string; order: "asc" | "desc" }>({ key: "event_timestamp", order: "desc" });
  const [f, setF] = useState(() => ({
    camera_id: params.getAll("camera_id"), event_type: params.getAll("event_type"), severity: params.getAll("severity"),
    status: params.getAll("status"), object_type: [] as string[], start: "", end: "", q: "",
  }));
  const openId = params.get("open");
  const { data: cams } = useLoad(() => get<Camera[]>("/cameras"), []);
  const query = {
    ...f, start: f.start ? new Date(f.start).toISOString() : undefined, end: f.end ? new Date(f.end).toISOString() : undefined,
    page, page_size: 25, sort: sort.key, order: sort.order,
  };
  const { data, error, reload } = useLoad(() => get<Page<EventItem>>("/events", query), [JSON.stringify(query)], 8000);
  const toast = useToast();

  const set = (k: keyof typeof f, v: any) => { setF((x) => ({ ...x, [k]: v })); setPage(1); };
  const sortBy = (key: string) => setSort((s) => ({ key, order: s.key === key && s.order === "desc" ? "asc" : "desc" }));
  const open = (id: number | null) => {
    const p = new URLSearchParams(params);
    if (id) p.set("open", String(id)); else p.delete("open");
    setParams(p, { replace: true });
  };
  const exportCsv = () => download("/events/export.csv", query, "incidents.csv").catch((e) => toast(errorText(e), "err"));

  return (
    <>
      <div className="page-head">
        <div><h1>Incidents</h1><p>Search, review evidence and record what was done about each incident.</p></div>
        <span className="spacer" />
        <button onClick={exportCsv}>Export CSV</button>
      </div>

      <div className="filters">
        <MultiSelect label="Camera" value={f.camera_id} onChange={(v) => set("camera_id", v)}
          options={(cams || []).map((c) => [String(c.id), c.name])} />
        <MultiSelect label="Type" value={f.event_type} onChange={(v) => set("event_type", v)}
          options={Object.keys(EVENT_LABEL).map((k) => [k, EVENT_LABEL[k]])} />
        <MultiSelect label="Severity" value={f.severity} onChange={(v) => set("severity", v)} options={SEVERITIES.map((s) => [s, s])} />
        <MultiSelect label="Status" value={f.status} onChange={(v) => set("status", v)} options={STATUSES.map((s) => [s, s.toLowerCase()])} />
        <MultiSelect label="Object" value={f.object_type} onChange={(v) => set("object_type", v)} options={OBJECTS.map((s) => [s, s.replace("_", " ")])} />
        <label className="field">From<input type="datetime-local" value={f.start} max={localInputValue(new Date())} onChange={(e) => set("start", e.target.value)} /></label>
        <label className="field">To<input type="datetime-local" value={f.end} onChange={(e) => set("end", e.target.value)} /></label>
        <label className="field">Search<input value={f.q} placeholder="title or notes" onChange={(e) => set("q", e.target.value)} /></label>
      </div>

      <section className="panel">
        <div className="body">
          <ErrorBox error={error} />
          {data && data.items.length === 0 ? <Empty title="No incidents match these filters">Clear a filter or widen the time range.</Empty> : (
            <div className="table-wrap">
              <table>
                <thead><tr>
                  <Th k="event_timestamp" sort={sort} onSort={sortBy}>Time</Th>
                  <Th k="event_type" sort={sort} onSort={sortBy}>Type</Th>
                  <th>What happened</th>
                  <Th k="camera_id" sort={sort} onSort={sortBy}>Camera</Th>
                  <th>Object</th>
                  <Th k="severity" sort={sort} onSort={sortBy}>Severity</Th>
                  <Th k="status" sort={sort} onSort={sortBy}>Status</Th>
                </tr></thead>
                <tbody>
                  {data?.items.map((e) => (
                    <tr key={e.id} className="clickable" onClick={() => open(e.id)}>
                      <td className="time">{fmtTime(e.event_timestamp)}</td>
                      <td>{eventLabel(e.event_type)}</td>
                      <td>{e.title}{(e.metadata?.object_count || 1) > 1 && <span className="muted"> ({e.metadata.object_count} objects)</span>}</td>
                      <td>{e.camera_name}{e.zone_name && <div className="small muted">{e.zone_name}</div>}</td>
                      <td><GroupTag cls={e.object_type} /></td>
                      <td><SevBadge severity={e.severity} /></td>
                      <td><EventStatusBadge status={e.status} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {data && <Pager page={data.page} pageSize={data.page_size} total={data.total} onPage={setPage} />}
        </div>
      </section>
      {openId && <EventDrawer id={Number(openId)} onClose={() => open(null)} onChanged={reload} />}
    </>
  );
}

function Th({ k, sort, onSort, children }: { k: string; sort: { key: string; order: string }; onSort: (k: string) => void; children: string }) {
  const active = sort.key === k;
  return <th aria-sort={active ? (sort.order === "asc" ? "ascending" : "descending") : "none"}>
    <button className={`th-sort${active ? " active" : ""}`} onClick={() => onSort(k)}>
      {children}{active && <span aria-hidden="true">{sort.order === "asc" ? "↑" : "↓"}</span>}</button></th>;
}

function MultiSelect({ label, value, onChange, options }: { label: string; value: string[]; onChange: (v: string[]) => void; options: [string, string][] }) {
  return (
    <label className="field">{label}
      <select value={value[0] || ""} onChange={(e) => onChange(e.target.value ? [e.target.value] : [])}>
        <option value="">All</option>
        {options.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
      </select>
    </label>
  );
}

function EventDrawer({ id, onClose, onChanged }: { id: number; onClose: () => void; onChanged: () => void }) {
  const { can } = useAuth();
  const toast = useToast();
  const { data: ev, error, reload, setData } = useLoad(() => get<EventItem>(`/events/${id}`), [id]);
  const { data: evidence, reload: reloadEvidence } = useLoad(() => get<Evidence[]>(`/events/${id}/evidence`), [id]);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  // clips finish encoding a few seconds after the incident: poll until they show up
  useEffect(() => {
    if (!ev || ev.has_clip || ev.object_type === "system") return;
    const t = setInterval(() => { reload(); reloadEvidence(); }, 4000);
    return () => clearInterval(t);
  }, [ev, reload, reloadEvidence]);

  const act = async (status?: string, severity?: string) => {
    setBusy(true);
    try {
      const r = await patch<EventItem>(`/events/${id}`, { status, severity, note });
      setData(r);
      setNote("");
      onChanged();
      toast(status ? `Marked ${status.toLowerCase()}` : "Saved");
    } catch (e) { toast(errorText(e), "err"); } finally { setBusy(false); }
  };

  const snap = evidence?.find((e) => e.kind === "snapshot" && e.url);
  const clip = evidence?.find((e) => e.kind === "clip" && e.url);
  const next: Record<string, string[]> = {
    NEW: ["ACKNOWLEDGED", "INVESTIGATING", "RESOLVED", "DISMISSED"], ACKNOWLEDGED: ["INVESTIGATING", "RESOLVED", "DISMISSED"],
    INVESTIGATING: ["RESOLVED", "DISMISSED"], RESOLVED: ["INVESTIGATING"], DISMISSED: ["INVESTIGATING"],
  };
  const verb: Record<string, string> = { ACKNOWLEDGED: "Acknowledge", INVESTIGATING: "Investigate", RESOLVED: "Resolve", DISMISSED: "Dismiss as false alarm" };

  return (
    <Drawer title={ev ? eventLabel(ev.event_type) : "Incident"} onClose={onClose}>
      <ErrorBox error={error} />
      {ev && (
        <div className="grid" style={{ gap: 18 }}>
          <div className="btn-row"><SevBadge severity={ev.severity} /><EventStatusBadge status={ev.status} /><span className="muted small">#{ev.id}</span></div>
          <h2 style={{ fontFamily: "var(--font)", fontWeight: 600 }}>{ev.title}</h2>
          {clip ? <video className="media" src={clip.url!} controls autoPlay muted playsInline />
            : snap ? <img className="media" src={snap.url!} alt="Snapshot at the moment of the incident" />
            : ev.object_type !== "system" && <div className="empty">Evidence is being prepared</div>}
          {clip && snap && <details><summary className="small">Snapshot at the moment of detection</summary><img className="media" src={snap.url!} alt="Snapshot" style={{ marginTop: 8 }} /></details>}
          <dl className="kv">
            <dt>When</dt><dd>{fmtTime(ev.event_timestamp)}</dd>
            <dt>Camera</dt><dd>{ev.camera_name}</dd>
            {ev.zone_name && <><dt>Zone</dt><dd>{ev.zone_name}</dd></>}
            <dt>Object</dt><dd><GroupTag cls={ev.object_type} /> {ev.confidence ? <span className="muted small">confidence {ev.confidence.toFixed(2)}</span> : null}</dd>
            {ev.metadata?.reason && <><dt>Rule</dt><dd>{ev.metadata.reason}</dd></>}
            {(ev.metadata?.object_count || 1) > 1 && <><dt>Objects</dt><dd>{ev.metadata.object_count} entered together</dd></>}
            {ev.metadata?.message && <><dt>Detail</dt><dd>{ev.metadata.message}</dd></>}
            {ev.acknowledged_at && <><dt>Acknowledged</dt><dd>{fmtTime(ev.acknowledged_at)}</dd></>}
            {ev.resolved_at && <><dt>Closed</dt><dd>{fmtTime(ev.resolved_at)}</dd></>}
            <dt>Model / rules</dt><dd className="small muted">{ev.model_version || "—"} / {ev.rule_version}</dd>
            {clip && <><dt>Clip</dt><dd className="small muted">{clip.duration_s}s, sha256 {clip.checksum_sha256.slice(0, 12)}…</dd></>}
          </dl>
          {ev.notes && <div><h3>Notes</h3><pre className="log" style={{ background: "var(--panel-2)", color: "var(--ink)" }}>{ev.notes}</pre></div>}
          {can("events:update") && (
            <div className="grid" style={{ gap: 10 }}>
              <label className="field">Add a note (saved with the next action)<textarea value={note} onChange={(e) => setNote(e.target.value)} placeholder="What did security find?" /></label>
              <div className="btn-row">
                {next[ev.status]?.map((s) => <button key={s} disabled={busy} className={s === "RESOLVED" ? "btn-primary" : s === "DISMISSED" ? "" : ""} onClick={() => act(s)}>{verb[s]}</button>)}
                {note && <button disabled={busy} onClick={() => act()}>Save note</button>}
                <span className="spacer" style={{ flex: 1 }} />
                <select value={ev.severity} onChange={(e) => act(undefined, e.target.value)} aria-label="Severity">
                  {SEVERITIES.map((s) => <option key={s} value={s}>Severity: {s}</option>)}
                </select>
              </div>
            </div>
          )}
        </div>
      )}
    </Drawer>
  );
}
