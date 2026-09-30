// Minimal stroke icons (24px grid) so the rail doesn't depend on an icon font.
const P = { fill: "none", stroke: "currentColor", strokeWidth: 1.8, strokeLinecap: "round" as const, strokeLinejoin: "round" as const };

export const Icon = {
  dashboard: () => <svg viewBox="0 0 24 24" {...P}><path d="M4 20V10l8-6 8 6v10" /><path d="M9 20v-6h6v6" /></svg>,
  live: () => <svg viewBox="0 0 24 24" {...P}><rect x="3" y="6" width="13" height="12" rx="2" /><path d="m16 10 5-3v10l-5-3" /></svg>,
  events: () => <svg viewBox="0 0 24 24" {...P}><path d="M12 3 2.5 20h19L12 3Z" /><path d="M12 10v4" /><path d="M12 17h.01" /></svg>,
  cameras: () => <svg viewBox="0 0 24 24" {...P}><path d="M3 7h11l4 3v4l-4 3H3z" /><path d="M18 12h3" /><circle cx="8.5" cy="12" r="2" /></svg>,
  zones: () => <svg viewBox="0 0 24 24" {...P}><path d="M4 6l7-2 9 4-2 11-10 1z" /><circle cx="4" cy="6" r="1.4" /><circle cx="20" cy="8" r="1.4" /></svg>,
  recordings: () => <svg viewBox="0 0 24 24" {...P}><circle cx="12" cy="12" r="8.5" /><circle cx="12" cy="12" r="3" /></svg>,
  analytics: () => <svg viewBox="0 0 24 24" {...P}><path d="M4 20V4" /><path d="M4 20h16" /><path d="M8 16v-4M12 16V8M16 16v-6" /></svg>,
  system: () => <svg viewBox="0 0 24 24" {...P}><path d="M3 12h4l2-5 4 10 2-5h6" /></svg>,
  models: () => <svg viewBox="0 0 24 24" {...P}><rect x="4" y="4" width="16" height="16" rx="3" /><path d="M9 9h6v6H9z" /></svg>,
  users: () => <svg viewBox="0 0 24 24" {...P}><circle cx="9" cy="8" r="3.5" /><path d="M2.5 20c.8-3.5 3.4-5.5 6.5-5.5s5.7 2 6.5 5.5" /><path d="M16 4.5a3.5 3.5 0 0 1 0 7M21.5 20c-.5-2.6-2-4.4-4-5.1" /></svg>,
};

export function Logo() {
  return (
    <svg width="28" height="28" viewBox="0 0 32 32" aria-hidden="true">
      <rect width="32" height="32" rx="5" fill="#1c1b18" />
      <path d="M8 24V12l8-4.5 8 4.5v12" stroke="#d99a2b" strokeWidth="2.5" fill="none" strokeLinejoin="round" />
      <path d="M8 24h16" stroke="#d99a2b" strokeWidth="2.5" />
    </svg>
  );
}
