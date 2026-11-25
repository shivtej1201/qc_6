import { useEffect, useState } from "react";
import { api, type Site } from "../api";
import { go } from "../main";
import { useViewMotion } from "../motion";
import { ErrorBox, Icon, count, useLoad, useToast } from "../ui";

/* The crawl queue of a check: every page with its state (queued, completed, failed), attempts, HTTP status, time
   taken, when it finished, the error and where it came from. Live while the check runs; failed pages can be
   queued again as a new list. */

const STATES: [string, string][] = [["all", "All"], ["queued", "Queued"], ["completed", "Completed"], ["failed", "Failed"]];
const PAGE = 100;

export function QueueView({ site, params }: { site: Site; params: URLSearchParams }) {
  const toast = useToast();
  const runs = useLoad(() => api.runs(site.id), [site.id]);
  const usable = (runs.data?.runs || []).filter((r) => r.kind === "coverage" || r.kind === "full");
  const [runId, setRunId] = useState<number | null>(Number(params.get("run")) || null);
  const run = usable.find((r) => r.id === runId) || usable[0];
  const [state, setState] = useState("all");
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [tick, setTick] = useState(0);
  const data = useLoad(() => (run ? api.queue(run.id, state, query, offset, PAGE) : Promise.resolve(null)), [run?.id, state, query, offset, tick]);
  const ref = useViewMotion<HTMLDivElement>([]);
  useEffect(() => { const t = window.setTimeout(() => { setQuery(q.trim()); setOffset(0); }, 300); return () => window.clearTimeout(t); }, [q]);
  const live = !!data.data && ["running", "queued", "pausing", "cancelling"].includes(data.data.run_status);
  useEffect(() => { if (!live) return; const t = window.setInterval(() => setTick((n) => n + 1), 3000); return () => window.clearInterval(t); }, [live]);

  if (runs.error) return <ErrorBox text={runs.error} onRetry={runs.reload} />;
  if (runs.data && !run) return <div className="view"><h1>Crawl queue</h1><p className="lede">This appears after the first check.</p></div>;
  const d = data.data;
  const c = d?.counts;
  const done = c ? c.COMPLETED + c.FAILED + c.EXCLUDED : 0;
  const retry = async () => {
    if (!run) return;
    try { const r = await api.retryFailed(run.id); toast(`${count(r.urls)} failed pages are in a new list.`); go("start", { job: r.list_id }); }
    catch (e: any) { toast(e.detail || e.title || e.message); }
  };
  return (
    <div className="view" ref={ref}>
      <div className="head">
        <div>
          <h1>Crawl queue</h1>
          <p className="lede">Every page of a check: queued, completed or failed, with attempts, status, time taken and the error.
            Sitemap, sheet and typed pages all go through this one queue.</p>
        </div>
        <div className="row">
          <label className="sr-only" htmlFor="q-run">Check</label>
          <select id="q-run" value={run?.id || ""} onChange={(e) => { setRunId(Number(e.target.value)); setOffset(0); }}>
            {usable.map((r) => <option key={r.id} value={r.id}>Check {r.id} · {r.status}</option>)}
          </select>
        </div>
      </div>
      {data.error ? <ErrorBox text={data.error} onRetry={data.reload} /> : !d ? <p className="faint">Loading…</p> : (<>
        <dl className="pipe-grid" data-reveal>
          <div><dt>Total</dt><dd>{count(c!.TOTAL)}</dd></div>
          <div><dt>Queued</dt><dd>{count(c!.QUEUED)}</dd></div>
          <div><dt>Completed</dt><dd>{count(c!.COMPLETED)}</dd></div>
          <div className={c!.FAILED ? "bad" : ""}><dt>Failed / blocked / timed out</dt><dd>{count(c!.FAILED)}</dd></div>
        </dl>
        <div className="progress-line" role="progressbar" aria-label="Pages done" aria-valuemin={0} aria-valuemax={c!.TOTAL} aria-valuenow={done}
          style={{ margin: "10px 0" }}><i style={{ width: `${c!.TOTAL ? (100 * done / c!.TOTAL).toFixed(1) : 0}%` }} /></div>
        <div className="toolbar" data-reveal>
          <div className="seg" role="group" aria-label="State">
            {STATES.map(([k, l]) => <button key={k} aria-pressed={state === k} onClick={() => { setState(k); setOffset(0); }}>{l}</button>)}
          </div>
          <label className="sr-only" htmlFor="q-search">Find a page</label>
          <input id="q-search" className="search" type="search" placeholder="Find a page" value={q} onChange={(e) => setQ(e.target.value)} />
          {c!.FAILED > 0 && !live && <button className="btn" onClick={retry}><Icon name="play" />Retry the {count(c!.FAILED)} failed pages</button>}
        </div>
        <div className="table-wrap" tabIndex={0} role="region" aria-label="Queue">
          <table className="explore-table">
            <thead><tr><th className="num">#</th><th>Page</th><th>State</th><th className="num">Status</th><th className="num">Tries</th>
              <th className="num">Time</th><th>Finished</th><th>Source</th><th>Error</th></tr></thead>
            <tbody>
              {d.rows.length === 0 && <tr><td colSpan={9} className="empty">Nothing here.</td></tr>}
              {d.rows.map((r) => (
                <tr key={r.id}>
                  <td className="num faint">{r.id}</td>
                  <td className="mono" title={r.url}>{r.url}</td>
                  <td><span className={`pill ${r.state === "COMPLETED" ? "good" : r.state === "QUEUED" ? "" : "warn"}`}>{r.state.toLowerCase().replace("_", " ")}</span></td>
                  <td className="num">{r.status ?? ""}</td>
                  <td className="num">{r.attempts || ""}</td>
                  <td className="num">{r.ms != null && r.state !== "QUEUED" ? `${count(r.ms)} ms` : ""}</td>
                  <td className="faint">{r.done_at ? new Date(r.done_at).toLocaleTimeString() : ""}</td>
                  <td title={r.sources.join(" | ")}>{r.sources[0] || ""}{r.sources.length > 1 ? ` +${r.sources.length - 1}` : ""}</td>
                  <td title={r.error}>{r.error}</td>
                </tr>))}
            </tbody>
          </table>
        </div>
        <div className="row" style={{ marginTop: 8 }}>
          <span className="faint">{count(d.total ? offset + 1 : 0)}–{count(offset + d.rows.length)} of {count(d.total)}</span>
          <button className="btn" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Previous</button>
          <button className="btn" disabled={offset + PAGE >= d.total} onClick={() => setOffset(offset + PAGE)}>Next</button>
        </div>
      </>)}
    </div>
  );
}
