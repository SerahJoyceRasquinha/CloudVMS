import { Bar, BarChart, CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { SeriesBucket } from "../types";
import { classLabel } from "../hooks";

const C = { people: "var(--people)", vehicles: "var(--vehicles)", peopleOut: "var(--people-out)", vehiclesOut: "var(--vehicles-out)" };
const axis = { stroke: "var(--ink-3)", fontSize: 11.5, tickLine: false, axisLine: false } as const;

function tickLabel(bucket: string, hourly: boolean) {
  if (hourly) return bucket.slice(11, 13) + ":00";
  const d = new Date(bucket + "T00:00:00");
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

/** Entries above the axis, exits mirrored below — the gate's flow over the day. */
export function GateFlowChart({ series, hourly, mode = "crossings" }:
  { series: SeriesBucket[]; hourly: boolean; mode?: "crossings" | "seen" }) {
  const data = series.map((b) => mode === "seen"
    ? { t: tickLabel(b.bucket, hourly), full: b.bucket, person_in: b.person_seen, vehicle_in: b.vehicle_seen, person_out: 0, vehicle_out: 0 }
    : { t: tickLabel(b.bucket, hourly), full: b.bucket, person_in: b.person_in, vehicle_in: b.vehicle_in,
        person_out: -b.person_out, vehicle_out: -b.vehicle_out });
  return (
    <ResponsiveContainer width="100%" height="100%">
      <BarChart data={data} stackOffset="sign" margin={{ top: 8, right: 8, left: -18, bottom: 0 }} barCategoryGap="18%" maxBarSize={56}>
        <CartesianGrid vertical={false} stroke="var(--line-2)" />
        <XAxis dataKey="t" {...axis} interval="preserveStartEnd" minTickGap={12} />
        <YAxis {...axis} allowDecimals={false} tickFormatter={(v) => String(Math.abs(v))} />
        <ReferenceLine y={0} stroke="var(--ink-3)" />
        <Tooltip cursor={{ fill: "var(--panel-2)" }}
          contentStyle={{ background: "var(--panel)", border: "1px solid var(--line)", borderRadius: 4, fontSize: 12.5, color: "var(--ink)", boxShadow: "none" }}
          labelFormatter={(_, p) => (p && p[0] ? String((p[0].payload as any).full) : "")}
          formatter={(v: number, name: string) => [Math.abs(v), {
            person_in: mode === "seen" ? "People seen" : "People in", vehicle_in: mode === "seen" ? "Vehicles seen" : "Vehicles in",
            person_out: "People out", vehicle_out: "Vehicles out" }[name] || name]} />
        <Bar dataKey="person_in" stackId="a" fill={C.people} radius={[1, 1, 0, 0]} />
        <Bar dataKey="vehicle_in" stackId="a" fill={C.vehicles} radius={[1, 1, 0, 0]} />
        {mode === "crossings" && <Bar dataKey="person_out" stackId="a" fill={C.peopleOut} />}
        {mode === "crossings" && <Bar dataKey="vehicle_out" stackId="a" fill={C.vehiclesOut} radius={[0, 0, 1, 1]} />}
      </BarChart>
    </ResponsiveContainer>
  );
}

export function FlowLegend({ mode = "crossings" }: { mode?: "crossings" | "seen" }) {
  return (
    <div className="legend">
      <span><i style={{ background: C.people }} />{mode === "seen" ? "People seen" : "People in"}</span>
      <span><i style={{ background: C.vehicles }} />{mode === "seen" ? "Vehicles seen" : "Vehicles in"}</span>
      {mode === "crossings" && <><span><i style={{ background: C.peopleOut }} />People out</span>
        <span><i style={{ background: C.vehiclesOut }} />Vehicles out</span></>}
    </div>
  );
}

export function TypeBars({ counts }: { counts: Record<string, number> }) {
  const rows = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  const max = Math.max(1, ...rows.map((r) => r[1]));
  if (!rows.length) return <p className="muted small" style={{ margin: 0 }}>No vehicles counted in this period.</p>;
  return (
    <div className="bars-h">
      {rows.map(([k, v]) => (
        <div className="row" key={k}>
          <span>{classLabel(k)}</span>
          <div className="track"><div className="fill" style={{ width: `${(v / max) * 100}%` }} /></div>
          <b className="num" style={{ textAlign: "right" }}>{v}</b>
        </div>
      ))}
    </div>
  );
}

export function SimpleBars({ data, color = "var(--ink-3)" }: { data: { name: string; value: number }[]; color?: string }) {
  return (
    <ResponsiveContainer width="100%" height="100%">
      <BarChart data={data} layout="vertical" margin={{ top: 0, right: 16, left: 8, bottom: 0 }}>
        <XAxis type="number" hide allowDecimals={false} />
        <YAxis type="category" dataKey="name" {...axis} width={150} />
        <Tooltip cursor={{ fill: "var(--panel-2)" }} contentStyle={{ background: "var(--panel)", border: "1px solid var(--line)", borderRadius: 4, fontSize: 12.5, color: "var(--ink)", boxShadow: "none" }} />
        <Bar dataKey="value" fill={color} radius={[0, 1, 1, 0]} barSize={14} name="Count" />
      </BarChart>
    </ResponsiveContainer>
  );
}

export function MetricLines({ data, lines }: { data: any[]; lines: { key: string; name: string; color: string }[] }) {
  return (
    <ResponsiveContainer width="100%" height="100%">
      <LineChart data={data} margin={{ top: 8, right: 12, left: -12, bottom: 0 }}>
        <CartesianGrid vertical={false} stroke="var(--line-2)" />
        <XAxis dataKey="label" {...axis} minTickGap={24} />
        <YAxis {...axis} />
        <Tooltip contentStyle={{ background: "var(--panel)", border: "1px solid var(--line)", borderRadius: 4, fontSize: 12.5, color: "var(--ink)", boxShadow: "none" }} />
        {lines.map((l) => <Line key={l.key} type="monotone" dataKey={l.key} name={l.name} stroke={l.color} dot={false} strokeWidth={1.5} />)}
      </LineChart>
    </ResponsiveContainer>
  );
}
