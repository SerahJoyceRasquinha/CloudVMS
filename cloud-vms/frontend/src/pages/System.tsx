import { get } from "../api";
import { MetricLines } from "../components/Charts";
import { Empty, ErrorBox, StatusBadge } from "../components/ui";
import { fmtDuration, fmtTime, useLoad } from "../hooks";

export default function System() {
  const { data: h, error } = useLoad(() => get<any>("/system/health"), [], 5000);
  const { data: perf } = useLoad(() => get<any>("/analytics/performance", { minutes: 60 }), [], 15000);
  const sup = h?.supervisor;
  const series = (perf?.series || []).map((p: any) => ({ ...p, label: new Date(p.ts).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }) }));
  const runs: any[] = perf?.runs || [];

  return (
    <>
      <div className="page-head"><div><h1>System health</h1><p>What the workers are doing right now, and measured performance.</p></div></div>
      <ErrorBox error={error} />
      {h && (
        <div className="grid cols-4">
          <Tile title="API and database" value={h.database === "ok" ? "Healthy" : "Problem"} sub={`storage: ${h.storage_backend}`} />
          <Tile title="Detector" value={sup?.detector || "not loaded"} sub={sup?.device ? `running on ${sup.device}` : h.embedded_workers ? "loads when a camera starts" : "workers run separately"} />
          <Tile title="Host CPU / memory" value={`${h.host.cpu_percent}%`} sub={`${Math.round(h.host.memory_used_mb / 1024 * 10) / 10} of ${Math.round(h.host.memory_total_mb / 1024)} GB, ${h.host.cpu_count} cores`} />
          <Tile title="Disk free" value={`${h.host.disk_free_gb} GB`} sub={h.host.gpu ? `${h.host.gpu.name}, ${h.host.gpu.memory_allocated_mb} MB used` : "no GPU detected"} />
        </div>
      )}

      <section className="panel" style={{ marginTop: 16 }}>
        <header><h2>Camera pipelines</h2><span className="spacer" />{sup && <span className="small muted">worker group "{sup.group}", up {fmtDuration(sup.uptime_s)}, {sup.inference.processed} frames analysed</span>}</header>
        <div className="body table-wrap">
          {!sup ? <Empty title="Workers run in a separate process">Start them with python -m app.workers.run; their health still appears in the charts below.</Empty>
            : Object.keys(sup.cameras).length === 0 ? <Empty title="No streams running">Start a camera on the Cameras page.</Empty> : (
              <table>
                <thead><tr><th>Camera</th><th>Status</th><th className="num">Input fps</th><th className="num">Analysed fps</th><th className="num">Model ms</th><th className="num">Stream→decision ms</th><th className="num">Tracks</th><th className="num">Dropped</th><th className="num">Reconnects</th><th className="num">Incidents</th></tr></thead>
                <tbody>{Object.entries(sup.cameras).map(([id, c]: [string, any]) => (
                  <tr key={id}><td>#{id}</td><td><StatusBadge status={c.status === "STARTING" ? "REGISTERED" : c.status} /> <span className="small muted">{c.message}</span></td>
                    <td className="num">{c.input_fps}</td><td className="num">{c.inference_fps}</td><td className="num">{c.inference_ms}</td><td className="num">{c.end_to_end_ms}</td>
                    <td className="num">{c.active_tracks}</td><td className="num">{c.frames_dropped}</td><td className="num">{c.reconnects}</td><td className="num">{c.events_created}</td></tr>
                ))}</tbody>
              </table>
            )}
          {sup && Object.keys(sup.recorders).length > 0 && (
            <p className="small muted" style={{ marginBottom: 0 }}>Recorders: {Object.entries(sup.recorders).map(([id, r]: [string, any]) => `#${id} ${r.running ? "recording" : "restarting"} (${r.segments} segments${r.error ? `, last error: ${r.error}` : ""})`).join("; ")}</p>
          )}
        </div>
      </section>

      <div className="grid cols-2" style={{ marginTop: 16 }}>
        <section className="panel">
          <header><h2>Latency, last hour</h2></header>
          <div className="body">{series.length ? <div className="chart-box"><MetricLines data={series} lines={[
            { key: "end_to_end_ms", name: "Stream to decision (ms)", color: "var(--sev-medium)" }, { key: "inference_ms", name: "Model (ms)", color: "var(--people)" }]} /></div>
            : <Empty title="No measurements yet" />}</div>
        </section>
        <section className="panel">
          <header><h2>Throughput and CPU, last hour</h2></header>
          <div className="body">{series.length ? <div className="chart-box"><MetricLines data={series} lines={[
            { key: "inference_fps", name: "Analysed fps per camera", color: "var(--vehicles)" }, { key: "cpu_percent", name: "Worker CPU %", color: "var(--ink-3)" }]} /></div>
            : <Empty title="No measurements yet" />}</div>
        </section>
      </div>

      <section className="panel" style={{ marginTop: 16 }}>
        <header><h2>Benchmarks and evaluations</h2><span className="spacer" /><span className="small muted">Added by the scripts in ml/evaluation with --register</span></header>
        <div className="body">
          {runs.length === 0 ? <Empty title="No runs stored yet">Run ml/evaluation/benchmark_scaling.py --register to measure 1, 2, 5 and 10 cameras on this machine.</Empty> :
            runs.map((r) => <RunView key={r.id} run={r} />)}
        </div>
      </section>
    </>
  );
}

function Tile({ title, value, sub }: { title: string; value: string; sub: string }) {
  return <section className="panel"><div className="body"><div className="small muted">{title}</div><div className="num" style={{ fontSize: 22, fontWeight: 500, letterSpacing: "-0.02em", lineHeight: 1.35 }}>{value}</div><div className="small muted">{sub}</div></div></section>;
}

function RunView({ run }: { run: any }) {
  return (
    <details style={{ borderBottom: "1px solid var(--line-2)", padding: "8px 0" }} open={run.kind === "scaling"}>
      <summary><b>{run.name}</b> <span className="small muted">{fmtTime(run.created_at)}</span></summary>
      <div style={{ paddingTop: 8 }} className="table-wrap">
        {run.kind === "scaling" && Array.isArray(run.results) ? (
          <table>
            <thead><tr><th className="num">Cameras</th><th className="num">Analysed fps</th><th className="num">Target</th><th className="num">Latency ms</th><th className="num">p95 ms</th><th className="num">Dropped / min</th><th className="num">CPU %</th><th className="num">RAM MB</th><th>Keeps up</th></tr></thead>
            <tbody>{run.results.map((r: any) => (
              <tr key={r.cameras}><td className="num">{r.cameras}</td><td className="num">{r.inference_fps_total}</td><td className="num">{r.target_inference_fps_total}</td><td className="num">{r.latency_ms_mean}</td>
                <td className="num">{r.latency_ms_p95}</td><td className="num">{r.dropped_frames_per_min}</td><td className="num">{r.cpu_percent_mean}</td><td className="num">{r.ram_mb_peak}</td><td>{r.keeps_up ? "yes" : "no"}</td></tr>
            ))}</tbody>
          </table>
        ) : <pre className="log" style={{ background: "var(--panel-2)", color: "var(--ink)" }}>{JSON.stringify(run.results, null, 2)}</pre>}
        <p className="small muted">{run.config?.hardware ? `${run.config.hardware.platform}, ${run.config.hardware.cpu_count} cores, ${run.config.hardware.device}` : ""} {run.config?.model ? `model ${run.config.model}` : ""}</p>
      </div>
    </details>
  );
}
