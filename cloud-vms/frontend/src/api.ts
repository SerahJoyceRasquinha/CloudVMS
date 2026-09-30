// Tiny API client: attaches the session token, turns error bodies into readable messages.

const TOKEN_KEY = "vms.token";
const REQUEST_TIMEOUT_MS = 20000;

export class ApiError extends Error {
  status: number;
  code: string;
  details: unknown;
  constructor(status: number, code: string, message: string, details?: unknown) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export function getToken(): string | null {
  try { return localStorage.getItem(TOKEN_KEY); } catch { return null; }
}
export function setToken(t: string | null) {
  try { if (t) localStorage.setItem(TOKEN_KEY, t); else localStorage.removeItem(TOKEN_KEY); } catch { /* private mode */ }
}

let onUnauthorized: () => void = () => {};
export function setUnauthorizedHandler(fn: () => void) { onUnauthorized = fn; }

type Query = Record<string, string | number | boolean | undefined | null | (string | number)[]>;

export function qs(q?: Query): string {
  if (!q) return "";
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(q)) {
    if (v === undefined || v === null || v === "") continue;
    if (Array.isArray(v)) v.forEach((x) => p.append(k, String(x)));
    else p.append(k, String(v));
  }
  const s = p.toString();
  return s ? `?${s}` : "";
}

export async function api<T = any>(method: string, path: string, body?: unknown, query?: Query): Promise<T> {
  const headers: Record<string, string> = {};
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  let payload: BodyInit | undefined;
  if (body instanceof FormData) payload = body;
  else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  // uploads may take long; everything else gives up after a while so a stuck request can't freeze a page
  const ctrl = new AbortController();
  const timer = body instanceof FormData ? undefined : setTimeout(() => ctrl.abort(), REQUEST_TIMEOUT_MS);
  let res: Response;
  try {
    res = await fetch(`/api${path}${qs(query)}`, { method, headers, body: payload, signal: ctrl.signal });
  } catch {
    clearTimeout(timer);
    throw ctrl.signal.aborted
      ? new ApiError(0, "timeout", "The server took too long to answer. Retrying automatically.")
      : new ApiError(0, "network", "Can't reach the server. Check that the backend is running.");
  }
  clearTimeout(timer);
  if (res.status === 401 && path !== "/auth/login") onUnauthorized();
  const type = res.headers.get("content-type") || "";
  const data = type.includes("application/json") ? await res.json() : await res.text();
  if (!res.ok) {
    const err = (data && (data as any).error) || {};
    throw new ApiError(res.status, err.code || "http_error", err.message || `Request failed (${res.status})`, err.details);
  }
  return data as T;
}

export const get = <T = any>(path: string, query?: Query) => api<T>("GET", path, undefined, query);
export const post = <T = any>(path: string, body?: unknown, query?: Query) => api<T>("POST", path, body, query);
export const patch = <T = any>(path: string, body?: unknown) => api<T>("PATCH", path, body);
export const put = <T = any>(path: string, body?: unknown, query?: Query) => api<T>("PUT", path, body, query);
export const del = <T = any>(path: string) => api<T>("DELETE", path);

export async function download(path: string, query: Query, filename: string) {
  const res = await fetch(`/api${path}${qs(query)}`, { headers: { Authorization: `Bearer ${getToken()}` } });
  if (!res.ok) throw new ApiError(res.status, "download_failed", "Export failed");
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

export function errorText(e: unknown): string {
  if (e instanceof ApiError) {
    const d = e.details;
    if (Array.isArray(d) && d.length) {
      const parts = d.map((x: any) => (typeof x === "string" ? x : x.field ? `${x.field}: ${x.message}` : x.message));
      return `${e.message}: ${parts.join("; ")}`;
    }
    return e.message;
  }
  return e instanceof Error ? e.message : String(e);
}
