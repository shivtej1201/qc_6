import { useEffect, useState } from "react";
import { api, type Anchors, type Site } from "../api";
import { go } from "../main";
import { useViewMotion } from "../motion";
import { ErrorBox, count, useLoad } from "../ui";

const FILTERS: Record<string, [string, string][]> = {
  links: [["", "All"], ["broken", "Broken"], ["redirect", "Redirects"], ["external", "Other sites"], ["nofollow", "Nofollow"]],
  images: [["", "All"], ["broken", "Broken"], ["missing_alt", "No description (alt)"], ["external", "Other sites"]],
};
const PAGE = 100;

export function LinksView({ site, params }: { site: Site; params: URLSearchParams }) {
  const runs = useLoad(() => api.runs(site.id), [site.id]);
  const dataset = params.get("d") === "images" ? "images" : "links";
  const anchorsTab = params.get("d") === "anchors";
  const [filter, setFilter] = useState(params.get("f") || "");
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const run = runs.data?.runs.find((r) => r.status === "completed" && (r.kind === "full" || r.kind === "coverage"));
  const data = useLoad(() => (run ? api.facts(run.id, dataset, { filter, q: query, offset, limit: PAGE, sort: "page" }) : Promise.resolve(null)),
    [run?.id, dataset, filter, query, offset]);
  const ref = useViewMotion<HTMLDivElement>([dataset]);
  useEffect(() => { const t = window.setTimeout(() => { setQuery(q.trim()); setOffset(0); }, 300); return () => window.clearTimeout(t); }, [q]);
  useEffect(() => { setOffset(0); }, [filter, dataset]);

  if (runs.error) return <ErrorBox text={runs.error} onRetry={runs.reload} />;
  if (runs.data && !run) return <div className="view"><h1>Links and images</h1><p className="lede">This appears after the first complete check.</p></div>;
  const s = data.data?.summary;
  return (
    <div className="view" ref={ref}>
      <div className="head">
        <div>
          <h1>{dataset === "links" ? "Links" : "Images"}</h1>
          <p className="lede">Every {dataset === "links" ? "link" : "image"} on every page from the last complete check
            {s ? <> — <b>{count(s.rows)}</b> placements of <b>{count(s.unique_urls)}</b> different addresses, <b>{count(s.broken)}</b> broken.</> : "."}</p>
        </div>
        <div className="seg" role="group" aria-label="Show">
          <button aria-pressed={dataset === "links" && !anchorsTab} onClick={() => go("links", { d: "links" })}>Links</button>
          <button aria-pressed={dataset === "images"} onClick={() => go("links", { d: "images" })}>Images</button>
          <button aria-pressed={anchorsTab} onClick={() => go("links", { d: "anchors" })}>Anchor text</button>
        </div>
      </div>
      {anchorsTab && run ? <AnchorReport runId={run.id} /> : <>
      <div className="toolbar" data-reveal>
        <div className="seg" role="group" aria-label="Filter">
          {FILTERS[dataset].map(([k, label]) => <button key={k} aria-pressed={filter === k} onClick={() => setFilter(k)}>{label}</button>)}
        </div>
        <label className="sr-only" htmlFor="links-q">Search addresses</label>
        <input id="links-q" className="search" type="search" placeholder="Search page or target address" value={q} onChange={(e) => setQ(e.target.value)} />
        {run && <a className="btn" href={api.factsCsvUrl(run.id, dataset, filter)}>Download CSV</a>}
      </div>
      {data.error ? <ErrorBox text={data.error} onRetry={data.reload} /> : (
        <div className="table-wrap" data-reveal>
          <table>
            <thead><tr><th>On page</th><th>{dataset === "links" ? "Links to" : "Image"}</th><th>{dataset === "links" ? "Text" : "Description (alt)"}</th><th className="num">Status</th></tr></thead>
            <tbody>
              {!data.data && <tr><td colSpan={4} className="faint">Loading…</td></tr>}
              {data.data && data.data.rows.length === 0 && <tr><td colSpan={4} className="empty">Nothing matches.</td></tr>}
              {data.data?.rows.map((r, i) => (
                <tr key={i}>
                  <td className="mono" style={{ maxWidth: 280, overflowWrap: "anywhere" }}><a href={`#/page?url=${encodeURIComponent(r.source_page_url)}`}>{path(r.source_page_url)}</a></td>
                  <td className="mono" style={{ maxWidth: 340, overflowWrap: "anywhere" }}>{r.url}{r.final_url && r.final_url !== r.url ? <span className="sub">→ {r.final_url}</span> : null}</td>
                  <td style={{ maxWidth: 220, overflowWrap: "anywhere" }}>{dataset === "links" ? r.anchor_text || <span className="faint">no text</span> : (r.alt_present ? r.alt_text || <span className="faint">empty</span> : <span className="pill warn">missing</span>)}</td>
                  <td className="num"><span className={`pill ${r.http_status >= 400 ? "bad" : r.http_status >= 300 ? "warn" : r.http_status ? "good" : ""}`}>{r.http_status ?? "—"}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {!anchorsTab && data.data && data.data.total > PAGE && (
        <div className="row" style={{ marginTop: 12, justifyContent: "space-between" }}>
          <span className="faint">{count(offset + 1)}–{count(Math.min(offset + PAGE, data.data.total))} of {count(data.data.total)}</span>
          <div className="row">
            <button className="btn" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Previous</button>
            <button className="btn" disabled={offset + PAGE >= data.data.total} onClick={() => setOffset(offset + PAGE)}>Next</button>
          </div>
        </div>
      )}
      </>}
    </div>
  );
}

/* Internal anchor text: generic anchors, links with no text, most used anchors, and pages named the same / differently. */
function AnchorReport({ runId }: { runId: number }) {
  const a = useLoad(() => api.anchors(runId), [runId]);
  if (a.error) return <ErrorBox text={a.error} onRetry={a.reload} />;
  const x: Anchors | null = a.data;
  if (!x) return <p className="faint">Loading…</p>;
  return (
    <div className="grid" style={{ gap: 16 }} data-reveal>
      <div className="stat-row">
        <div className="stat"><b>{count(x.internal_links)}</b><span className="stat-label">Internal links</span></div>
        <div className="stat"><b className={x.generic_share >= 5 ? "down" : ""}>{x.generic_share}%</b><span className="stat-label">Generic text</span>
          <span className="stat-sub">{count(x.generic_links)} links like "click here"</span></div>
        <div className="stat"><b className={x.empty.links ? "down" : ""}>{count(x.empty.links)}</b><span className="stat-label">No text at all</span>
          <span className="stat-sub">on {count(x.empty.pages)} pages</span></div>
        <div className="stat"><b>{count(x.same_everywhere.length)}</b><span className="stat-label">Always the same text</span>
          <span className="stat-sub">pages with 10+ links</span></div>
      </div>
      <div className="grid g-2">
        <div className="panel">
          <div className="panel-head"><h2>Generic anchor text</h2><span className="faint" style={{ fontSize: 13 }}>says nothing about the target</span></div>
          {x.generic.length === 0 ? <p className="empty">None. Every link says where it goes.</p> : (
            <div className="table-wrap flat"><table><thead><tr><th>Text</th><th className="num">Links</th><th className="num">Pages</th><th>For example</th></tr></thead>
              <tbody>{x.generic.map((g) => <tr key={g.anchor}><td>"{g.anchor}"</td><td className="num">{count(g.links)}</td><td className="num">{count(g.pages)}</td>
                <td className="mono" style={{ maxWidth: 260, overflowWrap: "anywhere" }}><a href={`#/page?url=${encodeURIComponent(g.example_page)}`}>{path(g.example_page)}</a> → {path(g.example_target)}</td></tr>)}</tbody></table></div>
          )}
        </div>
        <div className="panel">
          <div className="panel-head"><h2>Most used anchor text</h2></div>
          <div className="table-wrap flat"><table><thead><tr><th>Text</th><th className="num">Links</th><th className="num">Pages linked</th></tr></thead>
            <tbody>{x.top.slice(0, 15).map((t) => <tr key={t.anchor}><td>{t.anchor}</td><td className="num">{count(t.links)}</td><td className="num">{count(t.targets)}</td></tr>)}</tbody></table></div>
        </div>
      </div>
      {(x.same_everywhere.length > 0 || x.scattered.length > 0) && (
        <div className="grid g-2">
          <div className="panel">
            <div className="panel-head"><h2>Same text every time</h2><span className="faint" style={{ fontSize: 13 }}>95%+ of links: fine for menus, odd for content</span></div>
            {x.same_everywhere.length === 0 ? <p className="empty">None.</p> : <ul className="plain">{x.same_everywhere.slice(0, 12).map((r) =>
              <li key={r.url}><span className="mono">{path(r.url)}</span> <span className="faint">"{r.anchor}", {r.share}% of {count(r.links)} links</span></li>)}</ul>}
          </div>
          <div className="panel">
            <div className="panel-head"><h2>Different text almost every time</h2><span className="faint" style={{ fontSize: 13 }}>no clear name for the page</span></div>
            {x.scattered.length === 0 ? <p className="empty">None.</p> : <ul className="plain">{x.scattered.slice(0, 12).map((r) =>
              <li key={r.url}><span className="mono">{path(r.url)}</span> <span className="faint">{r.variants} different texts across {count(r.links)} links</span></li>)}</ul>}
          </div>
        </div>
      )}
      <p className="basis">{x.basis}</p>
    </div>
  );
}

function path(u: string) { try { const x = new URL(u); return x.pathname + x.search; } catch { return u; } }
