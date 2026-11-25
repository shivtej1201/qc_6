import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import type { Fix, FixStatus, Severity } from "./api";

// ------------------------------------------------------------------ formatting (design spec §2)
export function count(n: number | null | undefined, system: "in" | "intl" = "in"): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return Math.round(n).toLocaleString(system === "in" ? "en-IN" : "en-US");
}
export function ago(iso?: string | null): string {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  const s = Math.round((Date.now() - t) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short" }) + ", " +
    new Date(iso).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" });
}
export function day(iso: string): string {
  return new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short" });
}
export const STATUS_TEXT: Record<FixStatus, string> = {
  open: "Open", in_progress: "In progress", fixed_pending_check: "Fixed — waiting for check", verified: "Verified", wont_fix: "Won't fix",
};
export function statusPill(f: Pick<Fix, "status" | "regressed">) {
  if (f.regressed && f.status === "open") return <span className="pill new">Regressed</span>;
  const cls = f.status === "verified" ? "good" : f.status === "in_progress" ? "warn" : f.status === "fixed_pending_check" ? "good" : "";
  return <span className={`pill ${cls}`}>{STATUS_TEXT[f.status]}</span>;
}
export function Sev({ s }: { s: Severity }) {
  return <span className={`sev ${s}`}>{s[0].toUpperCase() + s.slice(1)}</span>;
}
export function Avatar({ name }: { name?: string | null }) {
  if (!name) return <span className="avatar none" title="Unassigned">?</span>;
  return <span className="avatar" title={name}>{name.split(/\s+/).map((p) => p[0]).slice(0, 2).join("").toUpperCase()}</span>;
}

// ------------------------------------------------------------------ icons (Lucide-style strokes)
const PATHS: Record<string, string> = {
  today: "M4 12a8 8 0 1 0 16 0 8 8 0 0 0-16 0zM12 7v5l3 2",
  live: "M3 12h4l2-6 4 12 2-6h6",
  fix: "M9 11l3 3 8-8M20 12v6a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h9",
  page: "M7 3h7l5 5v13H7zM14 3v5h5",
  journey: "M5 19a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM19 9a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM7 17h6a3 3 0 0 0 0-6h-2a3 3 0 0 1 0-6h6",
  plug: "M9 2v5M15 2v5M6 7h12v4a6 6 0 0 1-12 0zM12 17v5",
  shield: "M12 3l8 3v6c0 4.5-3.4 8-8 9-4.6-1-8-4.5-8-9V6z",
  desktop: "M3 5h18v11H3zM8 20h8M12 16v4",
  mobile: "M8 3h8v18H8zM11 18h2",
  up: "M4 17l6-6 4 4 6-8",
  growth: "M3 17l6-6 4 4 8-8M15 7h6v6",
  search: "M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14zM20 20l-4-4",
  menu: "M4 7h16M4 12h16M4 17h16",
  close: "M6 6l12 12M18 6L6 18",
  more: "M5 12h.01M12 12h.01M19 12h.01",
  link: "M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1",
  play: "M7 5l12 7-12 7z",
  layers: "M12 3l9 5-9 5-9-5zM3 13l9 5 9-5",
  settings: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z",
  pause: "M9 6v12M15 6v12",
  database: "M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3zM4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3",
  mail: "M3 6h18v12H3zM3 7l9 6 9-6",
};
export function Icon({ name }: { name: string }) {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d={PATHS[name] || ""} fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

// ------------------------------------------------------------------ toast
const ToastCtx = createContext<(msg: string) => void>(() => {});
export const useToast = () => useContext(ToastCtx);
export function ToastProvider({ children }: { children: ReactNode }) {
  const [msg, setMsg] = useState<string | null>(null);
  const timer = useRef<number>();
  const show = useCallback((m: string) => {
    setMsg(m);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setMsg(null), 3200);
  }, []);
  return (
    <ToastCtx.Provider value={show}>
      {children}
      {msg && <div className="toast" role="status">{msg}</div>}
    </ToastCtx.Provider>
  );
}

// ------------------------------------------------------------------ data hook
export function useLoad<T>(fn: () => Promise<T>, deps: unknown[]): { data: T | null; error: string | null; reload: () => void; loading: boolean } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let live = true;
    setLoading(true);
    fn().then((d) => { if (live) { setData(d); setError(null); } })
      .catch((e) => { if (live) setError(e?.detail || e?.message || String(e)); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);
  return { data, error, loading, reload: () => setTick((t) => t + 1) };
}

export function ErrorBox({ text, onRetry }: { text: string; onRetry?: () => void }) {
  return (
    <div className="error-box" role="alert">
      <p style={{ margin: 0 }}>{text}</p>
      {onRetry && <button className="btn" style={{ marginTop: 10 }} onClick={onRetry}>Try again</button>}
    </div>
  );
}

export async function copyText(text: string): Promise<boolean> {
  try { await navigator.clipboard.writeText(text); return true; } catch { return false; }
}

export function Spark({ values }: { values: number[] }) {
  if (values.length < 2) return null;
  const min = Math.min(...values) - 2, max = Math.max(...values) + 2;
  const pts = values.map((v, i) => [(i / (values.length - 1)) * 300, 52 - ((v - min) / Math.max(1, max - min)) * 46]);
  const d = pts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`).join(" ");
  const last = pts[pts.length - 1];
  return (
    <svg className="spark" viewBox="0 0 300 56" preserveAspectRatio="none" role="img"
      aria-label={`Health score trend from ${Math.round(values[0])} to ${Math.round(values[values.length - 1])}`}>
      <path d={`${d} L300 56 L0 56 Z`} fill="var(--measure-soft)" />
      <path d={d} fill="none" stroke="var(--measure)" strokeWidth="2" vectorEffect="non-scaling-stroke" />
      <circle cx={last[0]} cy={last[1]} r="3.5" fill="var(--measure)" />
    </svg>
  );
}


/* Report downloads in one menu: PDF summary, Excel (one sheet), CSV, ZIP (the CSV, compressed). */
export function DownloadMenu({ siteId, run }: { siteId: number; run?: number | null }) {
  const q = run ? `?run=${run}` : "";
  const items: [string, string, string][] = [
    [`/api/v2/sites/${siteId}/export.report.xlsx${q}`, "Management report (Excel)", "Dashboard with charts, pivot summary, non-200 links, tickets: small, ready to present"],
    [`/api/v2/sites/${siteId}/export.workbook.xlsx${q}`, "Full audit workbook (Excel)", "Executive summary, sitemap progress, business / language / page type, status, issues, broken, redirect, external URLs, QC problems"],
    [`/api/v2/sites/${siteId}/report.pdf`, "PDF summary", "one page for people"],
    [`/api/v2/sites/${siteId}/export.xlsx${q}`, "Excel, one sheet", "summary, problems, every page row: small"],
    [`/api/v2/sites/${siteId}/export.csv${q}`, "CSV", "the same, as plain text"],
    [`/api/v2/sites/${siteId}/export.zip${q}`, "ZIP", "the CSV compressed: smallest"],
    ...(run ? [[`/api/v2/runs/${run}/non-domain.xlsx`, "Non-domain URL audit (Excel)", "URLs on other hosts: QA / UAT / dev / staging, other subdomains, external, with template and component"] as [string, string, string]] : []),
    ...(run ? [[`/api/v2/runs/${run}/package.zip`, "All data as CSVs (ZIP)", "every link, unique URL, broken / redirect / external URL, images, SEO"] as [string, string, string]] : []),
  ];
  return (
    <details className="menu">
      <summary className="btn"><Icon name="page" />Download report</summary>
      <ul role="list">
        {items.filter(([u]) => !(run && u.endsWith("report.pdf"))).map(([u, l, d]) => (
          <li key={u}><a href={u} download><b>{l}</b><span>{d}</span></a></li>
        ))}
      </ul>
    </details>
  );
}


/* A modal "are you sure": what will go, a red confirm button, Esc / Cancel to back out. Focus stays inside. */
export function Confirm({ title, children, confirmText, onConfirm, onClose, typeToConfirm }: {
  title: string; children: ReactNode; confirmText: string; onConfirm: () => Promise<void> | void; onClose: () => void; typeToConfirm?: string }) {
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    box.current?.querySelector<HTMLElement>(typeToConfirm ? "input" : "button.quiet")?.focus();
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      if (e.key === "Tab" && box.current) {
        const els = [...box.current.querySelectorAll<HTMLElement>("button, input")].filter((x) => !x.hasAttribute("disabled"));
        const first = els[0], last = els[els.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    };
    document.addEventListener("keydown", key);
    return () => { document.removeEventListener("keydown", key); prev?.focus?.(); };
  }, []);
  const ok = !typeToConfirm || typed.trim() === typeToConfirm;
  return (
    <>
      <div className="palette-back" onClick={onClose} />
      <div className="dialog" role="alertdialog" aria-modal="true" aria-labelledby="confirm-title" aria-describedby="confirm-body" ref={box}>
        <h2 id="confirm-title">{title}</h2>
        <div id="confirm-body" className="dim" style={{ margin: "8px 0 14px" }}>{children}</div>
        {typeToConfirm && (
          <label className="grid" style={{ gap: 4, marginBottom: 14 }}><span>Type <b>{typeToConfirm}</b> to confirm</span>
            <input className="input" value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" /></label>
        )}
        {err && <div className="error-box" style={{ marginBottom: 12 }}>{err}</div>}
        <div className="row">
          <button className="btn danger" disabled={!ok || busy} onClick={async () => {
            setBusy(true); setErr(null);
            try { await onConfirm(); } catch (e: any) { setErr(e.detail || e.title || e.message); setBusy(false); }
          }}>{busy ? "Deleting…" : confirmText}</button>
          <button className="btn quiet" onClick={onClose}>Cancel</button>
        </div>
      </div>
    </>
  );
}
