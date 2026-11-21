import { useEffect, useState } from "react";
import { api, type Compare, type Site } from "../api";
import { go } from "../main";
import { useViewMotion } from "../motion";
import { ErrorBox, Sev, count, day, useLoad } from "../ui";

/* All pages of a check, filterable, with what changed since the check before; and two checks side by side. */

const FILTERS: { key: string; label: string; options: [string, string][] }[] = [
  { key: "change", label: "Since the last check", options: [["", "Any"], ["new", "New pages"], ["gone", "Pages gone"], ["status", "Status changed"]] },
  { key: "status", label: "Answer", options: [["", "Any"], ["2xx", "Fine (2xx)"], ["3xx", "Redirect (3xx)"], ["4xx", "Not found / refused (4xx)"], ["5xx", "Server error (5xx)"], ["none", "No answer"]] },
  { key: "indexable", label: "Search engines", options: [["", "Any"], ["yes", "May be indexed"], ["no", "Asked not to index"]] },
  { key: "canonical", label: "Canonical", options: [["", "Any"], ["self", "Points to itself"], ["other", "Points elsewhere"], ["missing", "Missing"]] },
  { key: "fixes", label: "Open fixes", options: [["", "Any"], ["yes", "Has open fixes"], ["no", "None"]] },
];
const CHANGE: Record<string, string> = { new: "New", gone: "Gone", status: "Status changed" };

export function PagesView({ site, params }: { site: Site; params: URLSearchParams }) {
  const [tab, setTab] = useState(params.get("tab") === "compare" ? "compare" : "pages");
  const ref = useViewMotion<HTMLDivElement>([tab]);
  return (
    <div className="view" ref={ref}>
      <div className="head">
        <div>
          <h1>All pages</h1>
          <p className="lede">Every page QC checked, what changed since the check before, and any two checks side by side.</p>
        </div>
        <div className="seg" role="group" aria-label="Show">
          <button aria-pressed={tab === "pages"} onClick={() => setTab("pages")}>Pages</button>
          <button aria-pressed={tab === "compare"} onClick={() => setTab("compare")}>Compare checks</button>
        </div>
      </div>
      {tab === "pages" ? <Explorer site={site} /> : <CompareChecks site={site} />}
    </div>
  );
}

function Explorer({ site }: { site: Site }) {
  const [f, setF] = useState<Record<string, string>>({});
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const [sort, setSort] = useState<[string, boolean]>(["url", false]);
  const tpl = useLoad(() => api.templates(site.id), [site.id]);
  useEffect(() => { const t = window.setTimeout(() => { setF((x) => ({ ...x, q: q.trim() })); setOffset(0); }, 300); return () => window.clearTimeout(t); }, [q]);
  const params: Record<string, string> = Object.fromEntries(Object.entries({ ...f, sort: sort[0], desc: String(sort[1]), offset: String(offset), limit: "100" })
    .filter(([, v]) => v !== "" && v != null));
  const d = useLoad(() => api.inventory(site.id, params), [site.id, JSON.stringify(params)]);
  const set = (k: string, v: string) => { setF({ ...f, [k]: v }); setOffset(0); };
  const [showFilters, setShowFilters] = useState(false);
  const active = Object.entries(f).filter(([k, v]) => v && k !== "q").length;
  const head = (key: string, label: string, num = false) => (
    <th className={num ? "num" : ""} aria-sort={sort[0] === key ? (sort[1] ? "descending" : "ascending") : "none"}>
      <button className="th-sort" onClick={() => setSort([key, sort[0] === key ? !sort[1] : num])}>{label}{sort[0] === key ? (sort[1] ? " ↓" : " ↑") : ""}</button>
    </th>
  );
  if (d.error) return <ErrorBox text={d.error} onRetry={d.reload} />;
  const x = d.data;
  const csvParams = Object.fromEntries(Object.entries({ ...f, run: x ? String(x.run_id) : "" }).filter(([, v]) => v));
  return (
    <>
      {x && x.previous_run_id && (
        <div className="row" style={{ marginBottom: 14 }} data-reveal>
          {(["new", "gone", "status"] as const).map((k) => {
            const n = k === "status" ? x.changes.status_changed : x.changes[k];
            return n != null && <button key={k} className={`btn ${f.change === k ? "primary" : ""}`} onClick={() => set("change", f.change === k ? "" : k)}>
              <b className="num">{count(n)}</b> {k === "new" ? "new pages" : k === "gone" ? "pages gone" : "changed status"}</button>;
          })}
          <span className="faint" style={{ fontSize: 13 }}>since check #{x.previous_run_id}</span>
        </div>
      )}
      <button className="btn filters-toggle" aria-expanded={showFilters} aria-controls="page-filters" onClick={() => setShowFilters(!showFilters)}>
        Filters{active ? ` · ${active}` : ""}</button>
      <div className={`filters ${showFilters ? "open" : ""}`} id="page-filters" data-reveal>
        {FILTERS.map((fl) => (
          <label key={fl.key}><span>{fl.label}</span>
            <select className="input" value={f[fl.key] || ""} onChange={(e) => set(fl.key, e.target.value)}>
              {fl.options.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
        ))}
        <label><span>Page type</span>
          <select className="input" value={f.template || ""} onChange={(e) => set("template", e.target.value)}>
            <option value="">Any</option>
            {(tpl.data?.templates || []).map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}</select></label>
        <label><span>Check</span>
          <select className="input" value={f.run || ""} onChange={(e) => set("run", e.target.value)}>
            <option value="">Latest</option>
            {(x?.checks || []).map((c) => <option key={c.id} value={c.id}>#{c.id} · {day(c.finished_at)}{c.imported ? " · from V5" : ""}</option>)}</select></label>
        <label className="grow"><span>Find</span>
          <input className="search" type="search" placeholder="Address or title" value={q} onChange={(e) => setQ(e.target.value)} /></label>
      </div>
      <div className="row" style={{ justifyContent: "space-between", margin: "4px 0 10px" }}>
        <span className="dim">{!x ? "Loading…" : f.change === "gone" ? `${count(x.total)} page${x.total === 1 ? "" : "s"} in check #${x.previous_run_id} but not in check #${x.run_id}`
          : `${count(x.total)} page${x.total === 1 ? "" : "s"} in check #${x.run_id}`}</span>
        <a className="btn quiet" href={api.inventoryCsv(site.id, csvParams)} download>Download as CSV</a>
      </div>
      <div className="table-wrap" tabIndex={0} role="region" aria-label="Pages">
        <table>
          <thead><tr>{head("url", "Page")}{head("status", "Answer", true)}{head("type", "Page type")}<th>Search engines</th>
            {head("depth", "Clicks from home", true)}{head("authority", "Link strength", true)}{head("fixes", "Open fixes", true)}<th>Change</th></tr></thead>
          <tbody>
            {x && x.pages.length === 0 && <tr><td colSpan={8} className="empty">No pages match these filters.</td></tr>}
            {x?.pages.map((p) => (
              <tr key={p.url} className="click" tabIndex={0} onClick={() => go("page", { url: p.url })} onKeyDown={(e) => e.key === "Enter" && go("page", { url: p.url })}>
                <td className="title"><span className="mono">{path(p.url)}</span>{p.title && <span className="sub">{p.title}</span>}</td>
                <td className="num">{p.http_status ?? (p.change === "gone" ? "—" : "no answer")}</td>
                <td>{p.template_name ? <span className="chip">{p.template_name}</span> : <span className="faint">—</span>}</td>
                <td>{p.change === "gone" ? "—" : p.noindex ? <span className="pill warn">Not indexed</span> : p.canonical === "other" ? <span className="pill warn">Canonical elsewhere</span>
                  : p.canonical === "missing" ? <span className="faint">No canonical</span> : <span className="faint">OK</span>}</td>
                <td className="num">{p.depth ?? "—"}</td>
                <td className="num">{p.authority != null ? p.authority.toFixed(1) : "—"}</td>
                <td className="num">{p.open_fixes ?? "—"}</td>
                <td>{p.change ? <span className={`pill ${p.change === "gone" ? "bad" : "new"}`}>{CHANGE[p.change]}{p.change === "status" ? ` ${p.previous_status ?? "?"} → ${p.http_status ?? "?"}` : ""}</span> : null}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {x && x.total > 100 && (
        <div className="row" style={{ marginTop: 12 }}>
          <button className="btn" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 100))}>Previous</button>
          <span className="dim">{count(offset + 1)}–{count(Math.min(offset + 100, x.total))} of {count(x.total)}</span>
          <button className="btn" disabled={offset + 100 >= x.total} onClick={() => setOffset(offset + 100)}>Next</button>
        </div>
      )}
    </>
  );
}

function CompareChecks({ site }: { site: Site }) {
  const checks = useLoad(() => api.inventory(site.id, { limit: "1" }).then((d) => d.checks), [site.id]);
  const [a, setA] = useState<number | null>(null);
  const [b, setB] = useState<number | null>(null);
  useEffect(() => {
    const c = checks.data || [];
    if (c.length >= 2 && a == null) { setB(c[0].id); setA(c[1].id); }
  }, [checks.data]);
  const cmp = useLoad<Compare | null>(() => (a && b && a !== b ? api.compare(site.id, a, b) : Promise.resolve(null)), [site.id, a, b]);
  if (checks.error) return <ErrorBox text={checks.error} onRetry={checks.reload} />;
  const c = checks.data || [];
  if (checks.data && c.length < 2) return <div className="panel empty">Compare needs two finished checks. Run another check and come back.</div>;
  const pick = (v: number | null, setter: (n: number) => void, id: string, label: string) => (
    <label htmlFor={id}><span>{label}</span>
      <select id={id} className="input" value={v ?? ""} onChange={(e) => setter(Number(e.target.value))}>
        {c.map((x) => <option key={x.id} value={x.id}>#{x.id} · {day(x.finished_at)} · {count(x.pages)} pages</option>)}</select></label>
  );
  const d = cmp.data;
  return (
    <>
      <div className="filters" data-reveal>{pick(a, setA, "cmp-a", "Before")}{pick(b, setB, "cmp-b", "After")}</div>
      {a === b && <p className="dim">Pick two different checks.</p>}
      {cmp.error && <ErrorBox text={cmp.error} />}
      {d && (
        <>
          <div className="grid g-3" style={{ marginTop: 6 }}>
            <div className="panel" data-reveal><p className="section-label">Pages</p>
              <div className="hero num">{count(d.pages.after)}<span> now, {count(d.pages.before)} before</span></div>
              <div className="facts">
                <div className="fact"><b className="num">{count(d.pages.added)}</b><span>new</span></div>
                <div className="fact"><b className="num">{count(d.pages.removed)}</b><span>gone</span></div>
                <div className="fact"><b className="num">{count(d.pages.status_changed)}</b><span>changed status</span></div>
              </div></div>
            <div className="panel" data-reveal><p className="section-label">Fixes in between</p>
              <div className="facts">
                <div className="fact"><b className="num">{d.fixes.new.count}</b><span>new problems</span></div>
                <div className="fact"><b className="num">{d.fixes.confirmed.count}</b><span>confirmed fixed</span></div>
                <div className="fact"><b className="num">{d.fixes.back.count}</b><span>back again</span></div>
              </div></div>
            <div className="panel" data-reveal><p className="section-label">Health</p>
              {d.score ? <><div className="hero num">{d.score.change > 0 ? "+" : ""}{d.score.change}<span> ({Math.round(d.score.before)} → {Math.round(d.score.after)})</span></div>
                <div className="dim-change">{Object.entries(d.score.dimensions).filter(([, v]) => v !== 0).map(([k, v]) =>
                  <span key={k} className="chip">{k} <b className={v > 0 ? "up" : "down"}>{v > 0 ? "+" : ""}{v}</b></span>)}</div></>
                : <p className="dim">No health score for one of these checks.</p>}</div>
          </div>
          <div className="grid g-2" style={{ marginTop: 16 }}>
            {(["new", "confirmed", "back"] as const).map((k) => d.fixes[k].count > 0 && (
              <div className="panel" data-reveal key={k}>
                <h2 style={{ marginBottom: 10 }}>{k === "new" ? "New problems" : k === "confirmed" ? "Confirmed fixed" : "Came back"}</h2>
                <ul className="found">{d.fixes[k].items.map((it) => <li key={it.id}><Sev s={it.severity} /><a href={`#/fixes?fix=${it.id}`} style={{ color: "inherit" }}>{it.title}</a></li>)}</ul>
              </div>
            ))}
            <div className="panel" data-reveal>
              <h2 style={{ marginBottom: 10 }}>Page types</h2>
              <div className="table-wrap flat" tabIndex={0} role="region" aria-label="Page types before and after">
                <table><thead><tr><th>Page type</th><th className="num">Before</th><th className="num">After</th></tr></thead>
                  <tbody>{d.page_types.map((t) => <tr key={t.name}><td className="title nowrap">{t.name}</td><td className="num">{count(t.before)}</td><td className="num">{count(t.after)}</td></tr>)}</tbody></table>
              </div>
            </div>
            {d.samples.status_changed.length > 0 && (
              <div className="panel" data-reveal>
                <h2 style={{ marginBottom: 10 }}>Changed status</h2>
                <ul className="reasons">{d.samples.status_changed.map((r) => <li key={r.url}><a className="mono" href={`#/page?url=${encodeURIComponent(r.url)}`}>{path(r.url)}</a>
                  <b className="num">{r.before ?? "—"} → {r.after ?? "—"}</b></li>)}</ul>
              </div>
            )}
          </div>
        </>
      )}
    </>
  );
}

function path(u: string) {
  try { const x = new URL(u); return x.pathname + x.search; } catch { return u; }
}
