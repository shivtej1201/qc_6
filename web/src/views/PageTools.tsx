import { useState } from "react";
import { api, type Brief, type Site } from "../api";
import { ErrorBox, ago, useLoad, useToast } from "../ui";

/* A content brief for one page, written by the local model from QC's evidence. Every section can be edited by hand or
   written again on its own; writing one again never touches the others. */
export function BriefPanel({ site, url }: { site: Site; url: string }) {
  const list = useLoad(() => api.briefs(site.id, url), [site.id, url]);
  const toast = useToast();
  const [busy, setBusy] = useState<string | null>(null);
  const [editing, setEditing] = useState<{ name: string; text: string } | null>(null);
  const [current, setCurrent] = useState<Brief | null>(null);
  const b = current || list.data?.briefs[0] || null;
  const run = async (key: string, f: () => Promise<Brief | void>, msg?: string) => {
    setBusy(key);
    try { const r = await f(); if (r) setCurrent(r); if (msg) toast(msg); }
    catch (e: any) { toast(e.detail ? `${e.title}. ${e.detail}` : e.title || e.message); }
    finally { setBusy(null); }
  };
  return (
    <section className="panel brief" data-reveal aria-labelledby="brief-h">
      <div className="panel-head">
        <h2 id="brief-h">Content brief</h2>
        <div className="row">
          {b && <a className="btn small" href={`/api/v2/briefs/${b.id}/brief.md`} download>Download (.md)</a>}
          <button className="btn small primary" disabled={!!busy} onClick={() => run("new", () => api.makeBrief(site.id, url), "Brief written. Edit any part, or write one part again.")}>
            {busy === "new" ? "Writing… (a minute or two)" : b ? "Write a new brief" : "Write a brief"}</button>
        </div>
      </div>
      {list.error ? <ErrorBox text={list.error} onRetry={list.reload} /> : !b ? (
        <p className="empty">A brief tells a writer what this page should cover: who it's for, which searches it should answer, an outline,
          a title and description, questions to answer, structured data, and which pages should link here. Written by the free local AI from
          this page, its Search Console queries and the site's strongest pages. Nothing is published.</p>
      ) : (
        <>
          <p className="faint" style={{ margin: "0 0 10px", fontSize: 13 }}>Written {ago(b.created_at)} with {b.model}. A draft: check it before using it.</p>
          <div className="brief-sections">
            {b.order.filter((k) => b.sections[k]).map((k) => {
              const sec = b.sections[k];
              return (
                <article key={k} className="brief-sec">
                  <header>
                    <h3>{b.labels[k]}</h3>
                    {sec.edited && <span className="pill new">edited{sec.edited_by ? ` by ${sec.edited_by}` : ""}</span>}
                    <span className="row" style={{ marginLeft: "auto", gap: 4 }}>
                      <button className="btn quiet small" onClick={() => setEditing({ name: k, text: sec.text })}>Edit</button>
                      <button className="btn quiet small" disabled={!!busy} onClick={() => run(k, () => api.rewriteBrief(b.id, k), `${b.labels[k]} written again.`)}>
                        {busy === k ? "Writing…" : "Write again"}</button>
                    </span>
                  </header>
                  {editing?.name === k ? (
                    <form onSubmit={(e) => { e.preventDefault(); run("save", () => api.editBrief(b.id, k, editing.text), "Saved.").then(() => setEditing(null)); }}>
                      <label className="sr-only" htmlFor={`bs-${k}`}>{b.labels[k]}</label>
                      <textarea id={`bs-${k}`} className="input" rows={6} value={editing.text} onChange={(e) => setEditing({ name: k, text: e.target.value })} />
                      <div className="row" style={{ marginTop: 6 }}><button className="btn small primary" type="submit">Save</button>
                        <button className="btn small quiet" type="button" onClick={() => setEditing(null)}>Cancel</button></div>
                    </form>
                  ) : <p className="brief-text">{sec.text}</p>}
                </article>
              );
            })}
          </div>
        </>
      )}
    </section>
  );
}

/* The HTML QC received for this page (only kept when QC6_KEEP_HTML=true). Shown as text; nothing in it runs. */
export function SavedHtml({ runId, url }: { runId: number; url: string }) {
  const [state, setState] = useState<{ text?: string; err?: string; loading?: boolean }>({});
  const [find, setFind] = useState("");
  const load = async () => {
    setState({ loading: true });
    try { setState({ text: await api.pageHtml(runId, url) }); }
    catch (e: any) { setState({ err: e.detail ? `${e.title}. ${e.detail}` : e.title || e.message }); }
  };
  const lines = (state.text || "").split("\n");
  const shown = lines.slice(0, 4000);
  const hits = find.trim() ? shown.reduce((n, l) => n + (l.toLowerCase().includes(find.trim().toLowerCase()) ? 1 : 0), 0) : 0;
  return (
    <section className="panel" data-reveal aria-labelledby="html-h" style={{ marginTop: 16 }}>
      <div className="panel-head">
        <h2 id="html-h">HTML QC received</h2>
        <div className="row">
          {state.text != null && <a className="btn small" href={`/api/v2/runs/${runId}/html?${new URLSearchParams({ url, download: "true" })}`} download>Download</a>}
          {state.text == null && <button className="btn small" onClick={load} disabled={state.loading}>{state.loading ? "Loading…" : "Show"}</button>}
        </div>
      </div>
      {state.err && <p className="empty">{state.err}</p>}
      {state.text != null && (<>
        <div className="row" style={{ marginBottom: 8 }}>
          <label className="sr-only" htmlFor="html-find">Find in the HTML</label>
          <input id="html-find" className="search" type="search" placeholder="Find in the HTML (e.g. canonical, robots, ld+json)" value={find} onChange={(e) => setFind(e.target.value)} />
          {find.trim() && <span className="faint" role="status">{hits} line{hits === 1 ? "" : "s"}</span>}
        </div>
        <ol className="code" tabIndex={0} aria-label="Page HTML">
          {shown.map((l, i) => <li key={i} className={find.trim() && l.toLowerCase().includes(find.trim().toLowerCase()) ? "hit" : ""}>{l || " "}</li>)}
        </ol>
        {lines.length > shown.length && <p className="faint" style={{ fontSize: 13 }}>Showing the first {shown.length.toLocaleString("en-IN")} of {lines.length.toLocaleString("en-IN")} lines; download for the rest.</p>}
      </>)}
    </section>
  );
}
