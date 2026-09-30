import { useState } from "react";
import { errorText, get, patch, post } from "../api";
import { useAuth } from "../auth";
import { Dialog, ErrorBox, Pager, useToast } from "../components/ui";
import { fmtAgo, fmtTime, useLoad } from "../hooks";
import type { AuditEntry, Camera, Page, Role, User } from "../types";

export default function Users() {
  const { user: me, can } = useAuth();
  const { data: users, reload, error } = useLoad(() => get<User[]>("/users"), []);
  const { data: roles } = useLoad(() => get<Role[]>("/roles"), []);
  const { data: cams } = useLoad(() => get<Camera[]>("/cameras"), []);
  const [edit, setEdit] = useState<User | "new" | null>(null);
  const [auditPage, setAuditPage] = useState(1);
  const [action, setAction] = useState("");
  const { data: audit } = useLoad(() => (can("audit:view") ? get<Page<AuditEntry>>("/audit-logs", { page: auditPage, page_size: 20, action }) : Promise.resolve(null)), [auditPage, action], 10000);

  return (
    <>
      <div className="page-head"><div><h1>Users & audit</h1><p>Who can see which cameras, and a record of every security-relevant change.</p></div>
        <span className="spacer" /><button className="btn-primary" onClick={() => setEdit("new")}>Add user</button></div>
      <ErrorBox error={error} />
      <section className="panel">
        <div className="body table-wrap">
          <table>
            <thead><tr><th>User</th><th>Roles</th><th>Cameras</th><th>Last sign-in</th><th>Status</th><th /></tr></thead>
            <tbody>{users?.map((u) => (
              <tr key={u.id}><td><b>{u.username}</b>{u.full_name && <div className="small muted">{u.full_name}</div>}</td>
                <td>{u.roles.join(", ")}</td>
                <td className="small">{u.camera_scope === null ? "All cameras" : u.camera_scope.map((id) => cams?.find((c) => c.id === id)?.name || `#${id}`).join(", ") || "None"}</td>
                <td className="small muted">{fmtAgo(u.last_login_at)}</td>
                <td>{u.is_active ? "active" : <span className="muted">deactivated</span>}</td>
                <td style={{ textAlign: "right" }}><button className="btn-quiet" onClick={() => setEdit(u)}>Edit</button></td></tr>
            ))}</tbody>
          </table>
        </div>
      </section>

      {roles && (
        <section className="panel" style={{ marginTop: 16 }}>
          <header><h2>Roles</h2></header>
          <div className="body grid cols-4">
            {roles.map((r) => <div key={r.id}><h3>{r.name}</h3><p className="small muted" style={{ margin: "2px 0 6px" }}>{r.description}</p>
              <div className="small mono muted">{r.permissions.join(", ")}</div></div>)}
          </div>
        </section>
      )}

      {audit && (
        <section className="panel" style={{ marginTop: 16 }}>
          <header><h2>Audit log</h2><span className="spacer" />
            <select value={action} onChange={(e) => { setAction(e.target.value); setAuditPage(1); }} aria-label="Filter actions">
              <option value="">All actions</option><option value="auth.">Sign-ins</option><option value="camera.">Cameras</option><option value="zone.">Zones</option>
              <option value="event.">Incidents</option><option value="user.">Users</option><option value="model.">Models</option></select></header>
          <div className="body table-wrap">
            <table>
              <thead><tr><th>Time</th><th>Who</th><th>Action</th><th>Target</th><th>Details</th><th>IP</th></tr></thead>
              <tbody>{audit.items.map((a) => (
                <tr key={a.id}><td className="time small">{fmtTime(a.ts)}</td><td>{a.username || "—"}</td><td>{a.action}</td>
                  <td className="small">{a.target_type} {a.target_id}</td><td className="small muted" style={{ maxWidth: 380 }}>{JSON.stringify(a.details)}</td><td className="small muted">{a.ip}</td></tr>
              ))}</tbody>
            </table>
            <Pager page={audit.page} pageSize={audit.page_size} total={audit.total} onPage={setAuditPage} />
          </div>
        </section>
      )}
      {edit && roles && <UserDialog user={edit === "new" ? null : edit} roles={roles} cams={cams || []} self={me?.id} onClose={() => setEdit(null)} onSaved={() => { setEdit(null); reload(); }} />}
    </>
  );
}

function UserDialog({ user, roles, cams, self, onClose, onSaved }:
  { user: User | null; roles: Role[]; cams: Camera[]; self?: number; onClose: () => void; onSaved: () => void }) {
  const toast = useToast();
  const [v, setV] = useState({
    username: user?.username || "", full_name: user?.full_name || "", password: "", roles: user?.roles || ["viewer"],
    allCams: user ? user.camera_scope === null : true, scope: user?.camera_scope || [], is_active: user?.is_active ?? true,
  });
  const [err, setErr] = useState<unknown>(null);
  const toggle = <T,>(list: T[], x: T) => (list.includes(x) ? list.filter((y) => y !== x) : [...list, x]);
  const save = async () => {
    try {
      if (user) {
        await patch(`/users/${user.id}`, { full_name: v.full_name, roles: v.roles, is_active: v.is_active,
          ...(v.allCams ? { scope_all_cameras: true } : { camera_scope: v.scope }), ...(v.password ? { password: v.password } : {}) });
      } else {
        await post("/users", { username: v.username, full_name: v.full_name, password: v.password, roles: v.roles, camera_scope: v.allCams ? null : v.scope });
      }
      toast(user ? "User updated" : "User added");
      onSaved();
    } catch (e) { setErr(e); toast(errorText(e), "err"); }
  };
  return (
    <Dialog title={user ? `Edit ${user.username}` : "Add user"} onClose={onClose}
      footer={<><button onClick={onClose}>Cancel</button><button className="btn-primary" onClick={save}>{user ? "Save changes" : "Add user"}</button></>}>
      <div className="form-grid">
        <label className="field">Username<input value={v.username} disabled={!!user} onChange={(e) => setV({ ...v, username: e.target.value })} /></label>
        <label className="field">Full name<input value={v.full_name} onChange={(e) => setV({ ...v, full_name: e.target.value })} /></label>
        <label className="field full">{user ? "New password (leave empty to keep)" : "Initial password"}<input type="password" value={v.password} autoComplete="new-password" onChange={(e) => setV({ ...v, password: e.target.value })} /></label>
        <fieldset className="full"><legend>Roles</legend><div className="btn-row">
          {roles.map((r) => <label key={r.id} className="check" title={r.description}><input type="checkbox" checked={v.roles.includes(r.name)} onChange={() => setV({ ...v, roles: toggle(v.roles, r.name) })} />{r.name}</label>)}
        </div></fieldset>
        <fieldset className="full"><legend>Cameras this user can see</legend>
          <label className="check"><input type="checkbox" checked={v.allCams} onChange={(e) => setV({ ...v, allCams: e.target.checked })} />All cameras, including ones added later</label>
          {!v.allCams && <div className="btn-row" style={{ marginTop: 8 }}>{cams.map((c) => <label key={c.id} className="check"><input type="checkbox" checked={v.scope.includes(c.id)} onChange={() => setV({ ...v, scope: toggle(v.scope, c.id) })} />{c.name}</label>)}</div>}
        </fieldset>
        {user && user.id !== self && <label className="check full"><input type="checkbox" checked={v.is_active} onChange={(e) => setV({ ...v, is_active: e.target.checked })} />Account active</label>}
      </div>
      {err ? <div style={{ marginTop: 10 }}><ErrorBox error={err} /></div> : null}
    </Dialog>
  );
}
