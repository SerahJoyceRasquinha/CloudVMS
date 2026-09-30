import { useState } from "react";
import { download, errorText, get } from "../api";
import { FlowLegend, GateFlowChart, SimpleBars, TypeBars } from "../components/Charts";
import { Empty, ErrorBox, StatusBadge, useToast } from "../components/ui";
import { eventLabel, fmtDuration, localInputValue, useLoad } from "../hooks";
import type { Camera, Summary } from "../types";

export default function Analytics() {
  const toast = useToast();
  const now = new Date();
  const [start, setStart] = useState(localInputValue(new Date(now.getFullYear(), now.getMonth(), now.getDate() - 6)));
  const [end, setEnd] = useState(localInputValue(now));
  const [camId, setCamId] = useState("");
  const { data: cams } = useLoad(() => get<Camera[]>("/cameras"), []);
  const q = { start: new Date(start).toISOString(), end: new Date(end).toISOString(), camera_id: camId ? [camId] : [] };
  const { data: s, error } = useLoad(() => get<Summary>("/analytics/summary", q), [start, end, camId]);
  const lines = s?.counting_lines_configured;

  return (
    <>
      <div className="page-head">
        <div><h1>Statistics</h1><p>Unique entries and exits, vehicle mix and incidents for any period.</p></div>
        <span className="spacer" />
        <label className="field">From<input type="datetime-local" value={start} onChange={(e) => setStart(e.target.value)} /></label>
        <label className="field">To<input type="datetime-local" value={end} onChange={(e) => setEnd(e.target.value)} /></label>
        <label className="field">Camera<select value={camId} onChange={(e) => setCamId(e.target.value)}><option value="">All cameras</option>{cams?.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}</select></label>
        <button onClick={() => download("/analytics/crossings.csv", q, "unique_crossings.csv").catch((e) => toast(errorText(e), "err"))}>Export crossings CSV</button>
      </div>
      <ErrorBox error={error} />
      {s && (<>
        <section className="panel">
          <div className="stat-row">
            <div className="stat"><b>{lines ? s.people.entries : s.people.unique_seen}</b><span>people {lines ? "entered" : "seen"}</span></div>
            <div className="stat"><b>{s.people.exits}</b><span>people exited</span></div>
            <div className="stat"><b>{lines ? s.vehicles.entries : s.vehicles.unique_seen}</b><span>vehicles {lines ? "entered" : "seen"}</span></div>
            <div className="stat"><b>{s.vehicles.exits}</b><span>vehicles exited</span></div>
          </div>
        </section>
        <div className="grid cols-3" style={{ marginTop: 16 }}>
          <section className="panel span-2">
            <header><h2>Flow</h2><span className="spacer" /><FlowLegend mode={lines ? "crossings" : "seen"} /></header>
            <div className="body">{s.series.length ? <div className="chart-box tall"><GateFlowChart series={s.series} hourly={s.range.bucket === "hour"} mode={lines ? "crossings" : "seen"} /></div> : <Empty title="No traffic counted in this period" />}</div>
          </section>
          <section className="panel">
            <header><h2>Vehicle mix</h2></header>
            <div className="body"><TypeBars counts={lines && Object.keys(s.vehicles.types_in).length ? s.vehicles.types_in : s.vehicles.types_seen} /></div>
          </section>
        </div>
        <div className="grid cols-2" style={{ marginTop: 16 }}>
          <section className="panel">
            <header><h2>Incidents by type</h2><span className="spacer" /><span className="small muted">{s.events.total} total{s.events.mean_time_to_ack_s != null ? `, ${fmtDuration(s.events.mean_time_to_ack_s)} to acknowledge` : ""}</span></header>
            <div className="body">{Object.keys(s.events.by_type).length ? <div className="chart-box"><SimpleBars data={Object.entries(s.events.by_type).map(([k, v]) => ({ name: eventLabel(k), value: v }))} /></div> : <Empty title="No incidents" />}</div>
          </section>
          <section className="panel">
            <header><h2>By camera</h2></header>
            <div className="body table-wrap">
              <table>
                <thead><tr><th>Camera</th><th>Status</th><th className="num">People in</th><th className="num">Out</th><th className="num">Vehicles in</th><th className="num">Out</th><th className="num">Incidents</th></tr></thead>
                <tbody>{s.per_camera.map((c) => (
                  <tr key={c.camera_id}><td>{c.name}</td><td><StatusBadge status={c.status} /></td><td className="num">{c.person_in}</td><td className="num">{c.person_out}</td>
                    <td className="num">{c.vehicle_in}</td><td className="num">{c.vehicle_out}</td><td className="num">{c.events}</td></tr>
                ))}</tbody>
              </table>
            </div>
          </section>
        </div>
        <section className="panel" style={{ marginTop: 16 }}>
          <header><h2>How these numbers are counted</h2></header>
          <div className="body small" style={{ color: "var(--ink-2)", maxWidth: "80ch" }}>
            <p style={{ marginTop: 0 }}>An <b>entry</b> is one tracked person or vehicle crossing a counting line in the marked direction. The same track is never counted twice in the same direction, and someone hidden for a few seconds (behind a bus, say) is linked back to their earlier track by appearance, so they aren't counted again.</p>
            <p><b>Seen by cameras</b> counts distinct tracks confirmed anywhere in view, whether or not they crossed a line. People are not matched across different cameras.</p>
            <p style={{ marginBottom: 0 }}><b>Estimated on site</b> is entries minus exits for the period. It drifts if people leave through a gate without a camera.</p>
          </div>
        </section>
      </>)}
    </>
  );
}
