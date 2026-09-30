import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider, useAuth } from "./auth";
import Layout from "./components/Layout";
import { ToastProvider } from "./components/ui";
import Analytics from "./pages/Analytics";
import Cameras from "./pages/Cameras";
import Dashboard from "./pages/Dashboard";
import Events from "./pages/Events";
import Live from "./pages/Live";
import Login from "./pages/Login";
import Models from "./pages/Models";
import Recordings from "./pages/Recordings";
import System from "./pages/System";
import Users from "./pages/Users";
import Zones from "./pages/Zones";
// fonts are bundled (not fetched from Google) so the dashboard looks the same on an offline campus network
import "@fontsource/ibm-plex-sans/latin-400.css";
import "@fontsource/ibm-plex-sans/latin-500.css";
import "@fontsource/ibm-plex-sans/latin-600.css";
import "@fontsource/ibm-plex-mono/latin-400.css";
import "@fontsource/ibm-plex-mono/latin-500.css";
import "./styles.css";

try {
  const t = localStorage.getItem("vms.theme");
  if (t === "dark" || t === "light") document.documentElement.dataset.theme = t;
} catch { /* ignore */ }

function Guard({ perm, children }: { perm: string; children: JSX.Element }) {
  const { can } = useAuth();
  return can(perm) ? children : <p className="muted">Your account doesn't have access to this page.</p>;
}

function App() {
  const { user, loading } = useAuth();
  if (loading) return null;
  if (!user) return <Routes><Route path="*" element={<Login />} /></Routes>;
  const home = user.permissions.includes("analytics:view") ? <Dashboard /> : <Navigate to="/events" replace />;
  return (
    <Layout>
      <Routes>
        <Route path="/" element={home} />
        <Route path="/live" element={<Guard perm="live:view"><Live /></Guard>} />
        <Route path="/events" element={<Guard perm="events:view"><Events /></Guard>} />
        <Route path="/recordings" element={<Guard perm="recordings:view"><Recordings /></Guard>} />
        <Route path="/analytics" element={<Guard perm="analytics:view"><Analytics /></Guard>} />
        <Route path="/cameras" element={<Guard perm="cameras:view"><Cameras /></Guard>} />
        <Route path="/zones" element={<Guard perm="zones:view"><Zones /></Guard>} />
        <Route path="/models" element={<Guard perm="system:view"><Models /></Guard>} />
        <Route path="/system" element={<Guard perm="system:view"><System /></Guard>} />
        <Route path="/users" element={<Guard perm="users:manage"><Users /></Guard>} />
        <Route path="/login" element={<Navigate to="/" replace />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Layout>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <AuthProvider>
        <ToastProvider>
          <App />
        </ToastProvider>
      </AuthProvider>
    </BrowserRouter>
  </StrictMode>,
);
