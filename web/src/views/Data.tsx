import { useEffect, useRef, useState } from "react";
import { api, localStorageGet, localStorageSet, postForFile, type ExploreResult, type Site } from "../api";
import { useViewMotion } from "../motion";
import { ErrorBox, Icon, count, useLoad, useToast } from "../ui";

/* Data explorer: read-only SQL on one check's own crawl database (pages, links, images, URL status, issues).
   The server opens the data read-only and only allows SELECT, with a time limit, so nothing can be changed or locked. */

const PAGE_SIZES = [50, 100, 250, 500];
const START_SQL = "SELECT url, http_status, remark\nFROM url_status\nWHERE http_status = 404";

export function DataView({ site }: { site: Site; params: URLSearchParams }) {
  const toast = useToast();
  const runs = useLoad(() => api.runs(site.id), [site.id]);
  const usable = (runs.data?.runs || []).filter((r) => (r.kind === "coverage" || r.kind === "full" || r.kind === "spot") && r.status !== "queued");
  const [runId, setRunId] = useState<number | null>(null);
  const run = usable.find((r) => r.id === runId) || usable[0];
  const schema = useLoad(() => (run ? api.exploreSchema(run.id) : Promise.resolve(null)), [run?.id]);
  const [sql, setSql] = useState(() => localStorageGet("qc6-explore-sql") || START_SQL);
  const [ran, setRan] = useState("");
  const [pageSize, setPageSize] = useState(100);
  const [offset, setOffset] = useState(0);
  const [res, setRes] = useState<ExploreResult | null>(null);
  const [err, setErr] = useState<{ title: string; detail: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [exporting, setExporting] = useState(false);
  const box = useRef<HTMLTextAreaElement>(null);
  const ref = useViewMotion<HTMLDivElement>([]);
  useEffect(() => { setRes(null); setErr(null); setRan(""); }, [run?.id]);

  const execute = async (text: string, off: number, size = pageSize) => {
    if (!run) return;
    const q = text.trim();
    if (!q) { setErr({ title: "Write a query first", detail: "For example: SELECT * FROM url_status LIMIT 10" }); return; }
    setBusy(true); setErr(null);
    try {
      const r = await api.explore(run.id, q, off, size);
      setRes(r); setRan(q); setOffset(off);
      localStorageSet("qc6-explore-sql", q);
    } catch (e: any) { setErr({ title: e.title || "The query couldn't run", detail: e.detail || e.message }); }
    finally { setBusy(false); }
  };
  const exportCsv = async () => {
    if (!run || !ran) return;
    setExporting(true);
    try {
      const { blob, rows } = await postForFile(`/api/v2/runs/${run.id}/explore.csv`, { sql: ran });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob); a.download = `qc-check${run.id}-query.csv`; a.click();
      window.setTimeout(() => URL.revokeObjectURL(a.href), 5000);
      toast(`${count(rows)} rows saved as CSV.`);
    } catch (e: any) { toast(e.detail || e.title || e.message); }
    finally { setExporting(false); }
  };
  const insert = (text: string) => {
    const el = box.current;
    if (!el) { setSql(sql + text); return; }
    const a = el.selectionStart, b = el.selectionEnd;
    const next = sql.slice(0, a) + text + sql.slice(b);
    setSql(next);
    window.requestAnimationFrame(() => { el.focus(); el.selectionStart = el.selectionEnd = a + text.length; });
  };
  const clear = () => { setSql(""); setRes(null); setErr(null); setRan(""); setOffset(0); box.current?.focus(); };

  if (runs.error) return <ErrorBox text={runs.error} onRetry={runs.reload} />;
  if (runs.data && !run) return <div className="view"><h1>Data explorer</h1><p className="lede">This appears after the first check.</p></div>;
  const total = res?.total;
  const from = res && res.rows.length ? offset + 1 : 0;
  const to = res ? offset + res.rows.length : 0;
  const s = schema.data;

  return (
    <div className="view" ref={ref}>
      <div className="head">
        <div>
          <h1>Data explorer</h1>
          <p className="lede">Ask the crawl data anything with SQL (SELECT only). It reads the check's own database without
            changing or locking it, even while a check is running.</p>
        </div>
        <div className="row">
          <label className="sr-only" htmlFor="ex-run">Check</label>
          <select id="ex-run" value={run?.id || ""} onChange={(e) => setRunId(Number(e.target.value))}>
            {usable.map((r) => <option key={r.id} value={r.id}>Check {r.id} · {r.status}{r.finished_at ? ` · ${new Date(r.finished_at).toLocaleDateString()}` : ""}</option>)}
          </select>
        </div>
      </div>

      <div className="explore" data-reveal>
        <aside className="panel explore-side" aria-label="Tables">
          {schema.error ? <p className="faint">{schema.error}</p> : !s ? <p className="faint">Loading…</p> : <>
            <h2>Ready-made views</h2>
            {s.views.map((t) => <TableItem key={t.name} name={t.name} columns={t.columns} onPick={insert} open={t.name === "url_status"} />)}
            <h2>Raw crawl tables</h2>
            {s.tables.map((t) => <TableItem key={t.name} name={t.name} columns={t.columns} onPick={insert} />)}
          </>}
        </aside>
        <section className="explore-main">
          <div className="panel">
            <label className="ticket-label" htmlFor="ex-sql">Query</label>
            <textarea id="ex-sql" ref={box} className="mono sql" rows={8} spellCheck={false} value={sql}
              onChange={(e) => setSql(e.target.value)}
              onKeyDown={(e) => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); execute(sql, 0); } }} />
            <div className="row" style={{ flexWrap: "wrap", marginTop: 8 }}>
              <button className="btn primary" disabled={busy || !run} onClick={() => execute(sql, 0)}><Icon name="play" />{busy ? "Running…" : "Run query"}</button>
              <button className="btn quiet" disabled={busy} onClick={clear}><Icon name="close" />Clear</button>
              <button className="btn" disabled={!ran || exporting} onClick={exportCsv}><Icon name="page" />{exporting ? "Exporting…" : "Export CSV"}</button>
              {s && <select aria-label="Example queries" value="" onChange={(e) => { if (e.target.value) { setSql(e.target.value); box.current?.focus(); } }}>
                <option value="">Examples…</option>
                {s.examples.map((x) => <option key={x.name} value={x.sql}>{x.name}</option>)}
              </select>}
              <span className="faint">Ctrl+Enter runs · stops after {s?.limits.timeout_s ?? 30} s</span>
            </div>
          </div>

          {err && <div className="error-box" role="alert"><b>{err.title}</b><p>{err.detail}</p></div>}

          {res && <>
            <div className="toolbar explore-meta" aria-live="polite">
              <span><b>{total == null ? `${count(to)}+` : count(total)}</b> {total === 1 ? "row" : "rows"}</span>
              <span className="faint">{res.elapsed_ms < 1000 ? `${res.elapsed_ms} ms` : `${(res.elapsed_ms / 1000).toFixed(1)} s`}</span>
              <span className="faint">showing {count(from)}–{count(to)}</span>
              <span className="spacer" />
              <label className="faint" htmlFor="ex-size">Rows per page</label>
              <select id="ex-size" value={pageSize} onChange={(e) => { const n = Number(e.target.value); setPageSize(n); execute(ran, 0, n); }}>
                {PAGE_SIZES.map((n) => <option key={n} value={n}>{n}</option>)}
              </select>
              <button className="btn" disabled={busy || offset === 0} onClick={() => execute(ran, Math.max(0, offset - pageSize))}>Previous</button>
              <button className="btn" disabled={busy || !res.has_more} onClick={() => execute(ran, offset + pageSize)}>Next</button>
            </div>
            <div className="table-wrap" tabIndex={0} role="region" aria-label="Query results">
              <table className="explore-table">
                <thead><tr><th className="num">#</th>{res.columns.map((c, i) => <th key={i}>{c}</th>)}</tr></thead>
                <tbody>
                  {res.rows.length === 0 && <tr><td colSpan={res.columns.length + 1} className="empty">No rows match.</td></tr>}
                  {res.rows.map((r, i) => (
                    <tr key={offset + i}><td className="num faint">{offset + i + 1}</td>
                      {r.map((v, j) => <td key={j} className={typeof v === "number" ? "num" : ""}>{v === null ? <span className="faint">null</span> : String(v)}</td>)}</tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>}
        </section>
      </div>
    </div>
  );
}

function TableItem({ name, columns, onPick, open }: { name: string; columns: string[]; onPick: (t: string) => void; open?: boolean }) {
  return (
    <details className="explore-table-item" open={open}>
      <summary><span className="mono">{name}</span><button className="link-btn" aria-label={`Insert ${name}`} onClick={(e) => { e.preventDefault(); onPick(name); }}>insert</button></summary>
      <ul>{columns.map((c) => <li key={c}><button className="link-btn mono" onClick={() => onPick(c)}>{c}</button></li>)}</ul>
    </details>
  );
}
