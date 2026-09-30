import { useState, type ReactNode } from "react";
import { Link, NavLink, useNavigate } from "react-router-dom";
import { post, setToken } from "../api";
import { useAuth } from "../auth";
import { eventLabel, fmtClock, useClock, useIncidentStream, type LiveIncident } from "../hooks";
import { Icon, Logo } from "./Icons";
import { Dialog, ErrorBox, useToast } from "./ui";

const NAV: { to: string; label: string; icon: () => JSX.Element; perm: string }[] = [
  { to: "/", label: "Gate overview", icon: Icon.dashboard, perm: "analytics:view" },
  { to: "/live", label: "Live view", icon: Icon.live, perm: "live:view" },
  { to: "/events", label: "Incidents", icon: Icon.events, perm: "events:view" },
  { to: "/recordings", label: "Recordings", icon: Icon.recordings, perm: "recordings:view" },
  { to: "/analytics", label: "Statistics", icon: Icon.analytics, perm: "analytics:view" },
];
const SETUP: typeof NAV = [
  { to: "/cameras", label: "Cameras", icon: Icon.cameras, perm: "cameras:view" },
  { to: "/zones", label: "Zones & rules", icon: Icon.zones, perm: "zones:view" },
  { to: "/models", label: "Models & data", icon: Icon.models, perm: "system:view" },
  { to: "/system", label: "System health", icon: Icon.system, perm: "system:view" },
  { to: "/users", label: "Users & audit", icon: Icon.users, perm: "users:manage" },
];

export default function Layout({ children }: { children: ReactNode }) {
  const { user, can, logout } = useAuth();
  const now = useClock(1000);
  const toast = useToast();
  const nav = useNavigate();
  const [latest, setLatest] = useState<LiveIncident | null>(null);
  const [pwOpen, setPwOpen] = useState(!!user?.must_change_password);
  const [theme, setTheme] = useState<string>(() => document.documentElement.dataset.theme || "auto");

  useIncidentStream((e) => {
    setLatest(e);
    toast(<span><b>{eventLabel(e.event_type)}</b> at {e.camera_name}<br />{e.title}</span>,
      e.severity === "high" || e.severity === "critical" ? "attn" : "info");
  }, can("events:view"));

  const cycleTheme = () => {
    const next = theme === "auto" ? "dark" : theme === "dark" ? "light" : "auto";
    if (next === "auto") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = next;
    try { localStorage.setItem("vms.theme", next); } catch { /* ignore */ }
    setTheme(next);
  };

  return (
    <div className="shell">
      <aside className="rail">
        <div className="brand"><Logo /><div><b>Gatehouse</b><small>Video management</small></div></div>
        <nav className="nav" aria-label="Main">
          {NAV.some((n) => can(n.perm)) && <div className="nav-label">Monitor</div>}
          {NAV.filter((n) => can(n.perm)).map((n) => (
            <NavLink key={n.to} to={n.to} end={n.to === "/"}><n.icon />{n.label}</NavLink>
          ))}
          {SETUP.some((n) => can(n.perm)) && <div className="nav-label">Configure</div>}
          {SETUP.filter((n) => can(n.perm)).map((n) => (
            <NavLink key={n.to} to={n.to}><n.icon />{n.label}</NavLink>
          ))}
        </nav>
        <div className="rail-foot">
          <div className="who">{user?.full_name || user?.username}</div>
          <div className="role">{user?.roles.map((r) => r.replace(/_/g, " ")).join(", ")}</div>
          <div className="actions">
            <button onClick={() => setPwOpen(true)}>Password</button>
            <button onClick={cycleTheme}>Theme: {theme}</button>
          </div>
        </div>
      </aside>
      <div className="main">
        <div className="topbar">
          <div className="clock">{now.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false })}
            <small>{now.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" })}</small></div>
          <div className="ticker">
            {latest ? (<>
              <span className={`badge sev sev-${latest.severity}`}>{eventLabel(latest.event_type)}</span>
              <Link to={`/events?open=${latest.id}`}>{fmtClock(latest.event_timestamp)}, {latest.camera_name}: {latest.title}</Link>
            </>) : <span className="idle"><i />Listening for new incidents</span>}
          </div>
          <div className="user-chip">
            <button className="btn-quiet" onClick={async () => { await logout(); nav("/login"); }}>Sign out</button>
          </div>
        </div>
        <main className="content">{children}</main>
      </div>
      {(pwOpen || user?.must_change_password) && <ChangePassword forced={!!user?.must_change_password}
        onClose={() => setPwOpen(false)} onSignOut={async () => { await logout(); nav("/login"); }} />}
    </div>
  );
}

function ChangePassword({ onClose, onSignOut, forced }: { onClose: () => void; onSignOut: () => void; forced: boolean }) {
  const { refresh } = useAuth();
  const toast = useToast();
  const [cur, setCur] = useState("");
  const [next, setNext] = useState("");
  const [err, setErr] = useState<unknown>(null);
  const save = async () => {
    try {
      const r = await post<{ access_token: string }>("/auth/change-password", { current_password: cur, new_password: next });
      setToken(r.access_token);
      // the server refuses every other request until the password is changed, so reload to refetch the pages
      if (forced) { window.location.reload(); return; }
      await refresh();
      toast("Password changed");
      onClose();
    } catch (e) { setErr(e); }
  };
  return (
    <Dialog title="Change password" onClose={forced ? () => {} : onClose}
      footer={<><button onClick={forced ? onSignOut : onClose}>{forced ? "Sign out" : "Cancel"}</button><button className="btn-primary" onClick={save} disabled={!cur || next.length < 8}>Change password</button></>}>
      {forced && <p className="hint" style={{ marginTop: 0 }}>This account still uses its initial password. Choose your own now.</p>}
      <div className="form-grid">
        <label className="field full">Current password<input type="password" value={cur} onChange={(e) => setCur(e.target.value)} autoFocus /></label>
        <label className="field full">New password (8+ characters, letters and digits)<input type="password" value={next} onChange={(e) => setNext(e.target.value)} /></label>
      </div>
      {err ? <div style={{ marginTop: 10 }}><ErrorBox error={err} /></div> : null}
    </Dialog>
  );
}
