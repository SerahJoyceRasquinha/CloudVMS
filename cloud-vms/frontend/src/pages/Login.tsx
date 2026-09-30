import { useState, type FormEvent } from "react";
import { Navigate } from "react-router-dom";
import { useAuth } from "../auth";
import { Logo } from "../components/Icons";
import { ErrorBox } from "../components/ui";

export default function Login() {
  const { user, login } = useAuth();
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  if (user) return <Navigate to="/" replace />;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try { await login(username, password); } catch (err) { setError(err); } finally { setBusy(false); }
  };

  return (
    <div className="login">
      <section className="login-art">
        <div className="wordmark"><Logo />Gatehouse</div>
        <div>
          <svg viewBox="0 0 600 120" style={{ width: "100%", maxWidth: 520, display: "block", marginBottom: 36 }} aria-hidden="true">
            <path d="M0 104h600" stroke="#34322d" strokeWidth="1" />
            <path d="M40 104h520" stroke="#d99a2b" strokeWidth="1.5" strokeDasharray="6 8" />
            <path d="M90 104V46l36-16 36 16v58M438 104V46l36-16 36 16v58" stroke="#8a867d" strokeWidth="1.5" fill="none" strokeLinejoin="round" />
            <path d="M162 74h276" stroke="#8a867d" strokeWidth="4" />
            <path d="M300 104V90" stroke="#d99a2b" strokeWidth="1.5" />
          </svg>
          <h1>Every camera at the gate, in one place.</h1>
          <p>Live views, entry and exit counts for people and vehicles, and incidents with the video that proves them.</p>
        </div>
        <div className="facts">
          <div><b>Live</b>MJPEG and HLS views</div>
          <div><b>Counted</b>unique entries and exits</div>
          <div><b>Evidenced</b>clip and snapshot per incident</div>
        </div>
      </section>
      <section className="login-form">
        <form onSubmit={submit}>
          <h2>Sign in</h2>
          <p className="hint">Use the account your administrator gave you.</p>
          <label className="field">Username<input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" autoFocus /></label>
          <label className="field">Password<input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" /></label>
          <ErrorBox error={error} />
          <button className="btn-primary" disabled={busy || !password}>{busy ? "Signing in" : "Sign in"}</button>
          <p className="hint">First run? The administrator password is in <code>data/initial_admin_password.txt</code>.</p>
        </form>
      </section>
    </div>
  );
}
