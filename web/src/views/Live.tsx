import { useEffect, useRef, useState } from "react";
import { api, type Run, type Site } from "../api";
import { go } from "../main";
import { fadeText, useViewMotion } from "../motion";
import { Confirm, ErrorBox, Icon, Sev, ago, count, useLoad, useToast } from "../ui";

const STATUS: Record<string, string> = { running: "Running", queued: "Waiting to start", pausing: "Pausing…", paused: "Paused", cancelling: "Stopping…",
  completed: "Completed", failed: "Stopped", cancelled: "Cancelled" };
const STEP_TEXT: Record<string, string> = {
  open: "Loading the page", settled: "Page settled", consent: "Answered the cookie banner", read: "Reading", scrolled: "Scrolling down",
  measured: "Measuring", done: "Done, moving on",
};

type Lane = { url: string; device: string; status: string; steps: string[] };

export function LiveView({ site, params }: { site: Site; params: URLSearchParams }) {
  const runs = useLoad(() => api.runs(site.id), [site.id]);
  const runId = Number(params.get("run")) || runs.data?.runs[0]?.id || 0;
  const detail = useLoad(() => (runId ? api.run(runId) : Promise.resolve(null)), [runId]);
  const [run, setRun] = useState<Run | null>(null);
  const [lanes, setLanes] = useState<Lane[]>([]);
  const [found, setFound] = useState<{ id: number; title: string; severity: any; pages: number }[]>([]);
  const ref = useViewMotion<HTMLDivElement>([!!detail.data]);
  const lede = useRef<HTMLParagraphElement>(null);
  const toast = useToast();

  useEffect(() => {
    if (!detail.data) return;
    setRun(detail.data.run);
    setFound(detail.data.found);
    setLanes(detail.data.recent_visits.slice(0, 8).map((v: any) => ({ url: v.page_url, device: v.device, status: v.status,
      steps: (v.timeline || []).map((s: any) => s.step) })));
  }, [detail.data]);

  useEffect(() => {
    if (!run || !["queued", "running", "pausing", "cancelling"].includes(run.status)) return;
    const es = new EventSource(`/api/v2/stream?run=${run.id}`);
    let lane = 0;
    const onProgress = (e: MessageEvent) => {
      const d = JSON.parse(e.data);
      setRun((r) => (r ? { ...r, status: "running", progress: { ...r.progress, ...d } } : r));
      fadeText(lede.current);
    };
    es.addEventListener("progress", onProgress);
    es.addEventListener("visit", (e: MessageEvent) => {
      const d = JSON.parse(e.data);
      onProgress(e);
      setLanes((ls) => {
        const next = [...ls];
        next[(d.lane ?? lane++) % 8] = { url: d.url, device: d.device, status: d.status, steps: d.steps || [] };
        return next;
      });
    });
    es.addEventListener("journey", (e: MessageEvent) => {
      const d = JSON.parse(e.data);
      toast(`Journey "${d.name}" ${d.status === "passed" ? "passed" : `failed at step ${d.failed_step}`}.`);
    });
    const refresh = window.setInterval(() => { api.run(run.id).then((d) => setFound(d.found)).catch(() => {}); }, 8000);
    const finish = () => { es.close(); window.clearInterval(refresh); detail.reload(); runs.reload(); };
    es.addEventListener("completed", finish);
    es.addEventListener("failed", finish);
    es.addEventListener("paused", finish);
    return () => { es.close(); window.clearInterval(refresh); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run?.id, run?.status === "running" || run?.status === "queued"]);

  const [toDelete, setToDelete] = useState<Run | null>(null);
  if (runs.error || detail.error) return <ErrorBox text={(runs.error || detail.error)!} onRetry={detail.reload} />;
  if (!runs.data) return <p className="faint">Loading…</p>;
  if (!runId || !run) return (
    <div className="view"><h1>Live run</h1>
      <p className="lede">No check has run yet. Start one from Today.</p>
      <button className="btn primary" style={{ marginTop: 16 }} onClick={() => go("today")}>Go to Today</button></div>
  );

  const p = run.progress || {};
  const pagesDone = p.pages_done ?? run.summary?.pages ?? 0, pagesTotal = p.pages_total ?? run.summary?.pages ?? 0;
  const visitsDone = p.visits_done ?? run.summary?.visited ?? 0, visitsTotal = p.visits_total ?? run.summary?.visits_planned ?? 0;
  const live = ["queued", "running", "pausing", "cancelling"].includes(run.status);
  const control = async (what: "pause" | "resume" | "cancel" | "retry") => {
    try {
      if (what === "pause") { await api.pauseRun(run.id); toast("Pausing. Visitors finish the page they're on; nothing already checked is lost."); }
      if (what === "resume") { await api.resumeRun(run.id); toast("Resuming where it stopped."); }
      if (what === "cancel") { const r = await api.cancelRun(run.id); toast(r.status === "cancelling" ? "Stopping: the pages in hand finish, then it's cancelled." : "Check cancelled."); }
      if (what === "retry") { await api.retryRun(run.id); toast("Back in the queue. It carries on from where it stopped."); }
      detail.reload(); runs.reload();
    } catch (e: any) { toast(e.detail || e.title || e.message); }
  };
  const frac = p.stage === "visits" ? (visitsTotal ? visitsDone / visitsTotal : 0) : (pagesTotal ? pagesDone / pagesTotal : 0);
  const sentence = live
    ? (p.stage === "visits" ? `Visitors have checked ${count(visitsDone)} of ${count(visitsTotal)} sample pages in a real browser.`
      : `${count(pagesDone)} of ${count(pagesTotal)} pages checked.`)
    : run.status === "paused" ? "Paused. Resume to carry on where it stopped." : run.status === "completed" ? `Finished ${ago(run.finished_at)}. ${count(run.summary?.pages)} pages checked${run.summary?.visited ? `, ${count(run.summary.visited)} visited in a real browser` : ""}.`
      : run.status === "failed" ? `This check stopped: ${run.error}` : `This check was ${run.status}.`;
  const imported = run.options?.imported_from === "QC V5";
  const spot = run.spot || run.options?.spot;

  return (
    <div className="view" ref={ref}>
      <div className="head">
        <div>
          <h1>{live ? `Checking ${site.primary_host}` : `Check of ${site.primary_host}`}</h1>
          <p className="lede" ref={lede} aria-live="polite">{sentence}</p>
        </div>
        <div className="row">
          {run.status === "running" && <button className="btn" onClick={() => control("pause")}><Icon name="pause" />Pause</button>}
          {run.status === "paused" && <button className="btn primary" onClick={() => control("resume")}><Icon name="play" />Resume</button>}
          {["paused", "queued", "running", "pausing"].includes(run.status) && <button className="btn quiet" onClick={() => control("cancel")}>Cancel</button>}
          {run.status === "failed" && <button className="btn primary" onClick={() => control("retry")}><Icon name="play" />Retry from where it stopped</button>}
          <span className={`pill ${run.status === "completed" ? "good" : run.status === "failed" ? "bad" : "warn"}`}>
            {STATUS[run.status] || run.status}</span>
        </div>
      </div>
      {spot && <p className="pill new" style={{ marginBottom: 14 }}>Spot check: {spot.label}{spot.file ? ` (from ${spot.file})` : ""}.
        Its results are its own; the site's health score and fixes list aren't changed by it.</p>}
      {imported && <p className="pill new" style={{ marginBottom: 14 }}>Imported from QC V5 ({run.options?.v5_name || run.options?.v5_job}).
        V5 kept no page HTML, so page types come from URL patterns; there were no real-browser visits.</p>}
      {live && <div className="progress-line" aria-hidden="true"><i style={{ width: `${(frac * 100).toFixed(1)}%` }} /></div>}
      <p className="faint" style={{ fontSize: 13, margin: "0 0 20px" }}>
        Stage: {run.status === "paused" ? `paused after ${(p.stages_done || []).length ? (p.stages_done || []).join(", ") : "part of the page check"}` : p.stage === "visits" ? "real-visitor checks" : p.stage === "coverage" ? "every page and link" : live ? "starting" : "finished"} ·
        Each visitor reads one page at a time and pauses like a person.
      </p>
      <div className="grid g-main">
        <div className="panel" data-reveal>
          <div className="panel-head"><h2>Visitors</h2><span className="faint" style={{ fontSize: 13 }}>{live ? "right now" : "last pages visited"}</span></div>
          {lanes.length === 0 ? <p className="empty">Visitors start after the pages are listed.</p> : (
            <div className="lanes">
              {lanes.filter(Boolean).map((l, i) => (
                <div className="lane" key={i}>
                  <Icon name={l.device === "mobile" ? "mobile" : "desktop"} />
                  <span className="path" title={l.url}>{safePath(l.url)}</span>
                  <span className="act">{l.status === "challenged" ? "Stopped: bot check" : l.status === "failed" ? "Couldn't load" :
                    STEP_TEXT[l.steps[l.steps.length - 1]] || "Loading"}</span>
                </div>
              ))}
            </div>
          )}
        </div>
        <div className="panel" data-reveal>
          <div className="panel-head"><h2>Found in this check</h2><span className="faint" style={{ fontSize: 13 }}>grouped into fixes</span></div>
          {found.length === 0 ? <p className="empty">Nothing yet.</p> : (
            <ul className="found">
              {found.map((f) => (
                <li key={f.id}><Sev s={f.severity} /><a href={`#/fixes?fix=${f.id}`} style={{ color: "inherit" }}>{f.title}</a></li>
              ))}
            </ul>
          )}
        </div>
      </div>
      <Timeline runId={run.id} live={live} />
      <div className="panel" data-reveal style={{ marginTop: 16 }}>
        <div className="panel-head"><h2>Past checks</h2><span className="faint" style={{ fontSize: 13 }}>the last 50</span></div>
        <div className="table-wrap flat" tabIndex={0} role="region" aria-label="Past checks">
          <table>
            <thead><tr><th>Check</th><th>What</th><th>Status</th><th>Finished</th><th className="num">Pages</th><th><span className="sr-only">Actions</span></th></tr></thead>
            <tbody>{(runs.data?.runs || []).map((r) => (
              <tr key={r.id} aria-current={r.id === run.id ? "true" : undefined}>
                <td className="num nowrap">#{r.id}</td>
                <td className="nowrap">{r.spot ? <><span className="spot-tag">spot</span> {r.spot.label}</> : r.options?.imported_from ? "Imported from V5"
                  : ({ full: "Full check", coverage: "Page check", visit: "Real-browser visits", journey: "Journeys" } as Record<string, string>)[r.kind] || r.kind}</td>
                <td>{STATUS[r.status] || r.status}</td>
                <td className="nowrap">{r.finished_at ? ago(r.finished_at) : "—"}</td>
                <td className="num">{count(r.summary?.pages)}</td>
                <td className="nowrap"><button className="btn quiet small" onClick={() => go("live", { run: r.id })}>Open</button>
                  {!["running", "pausing", "cancelling"].includes(r.status) &&
                    <button className="btn quiet small" onClick={() => setToDelete(r)} aria-label={`Delete check ${r.id}`}>Delete</button>}</td>
              </tr>))}
            </tbody>
          </table>
        </div>
      </div>
      {toDelete && (
        <Confirm title={`Delete check #${toDelete.id}?`} confirmText="Delete the check" onClose={() => setToDelete(null)}
          onConfirm={async () => {
            const out = await api.deleteRun(toDelete.id);
            toast(`Check #${toDelete.id} deleted: ${count(out.pages)} pages` + (out.fixes_removed ? `, ${out.fixes_removed} fixes only it had found` : "") + ".");
            setToDelete(null); runs.reload(); go("live");
          }}>
          <p style={{ margin: 0 }}>Its pages, findings, visits, screenshots and report files are removed. Fixes that no other check found go too,
            unless someone commented on them or opened a ticket. This can't be undone; the audit log keeps a note of it.</p>
        </Confirm>
      )}
    </div>
  );
}

function safePath(u: string) {
  try { const x = new URL(u); return x.pathname + x.search; } catch { return u; }
}


/* What happened to this check and how fast it went, minute by minute (refreshes while it runs). */
function Timeline({ runId, live }: { runId: number; live: boolean }) {
  const t = useLoad(() => api.timeline(runId), [runId]);
  useEffect(() => {
    if (!live) return;
    const i = window.setInterval(t.reload, 5000);
    return () => window.clearInterval(i);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live, runId]);
  if (!t.data || (t.data.events.length === 0 && t.data.minutes.length === 0)) return null;
  const mins = t.data.minutes.slice(-30);
  const peak = Math.max(1, ...mins.map((m) => m.pages));
  return (
    <div className="grid g-2" style={{ marginTop: 16 }}>
      <div className="panel" data-reveal>
        <div className="panel-head"><h2>Timeline</h2><span className="faint" style={{ fontSize: 13 }}>newest first</span></div>
        <ol className="run-timeline" tabIndex={0} aria-label="Timeline of this check">
          {t.data.events.slice(0, 30).map((e, i) => (
            <li key={i} className={`ev-${e.kind}`}><time dateTime={e.created_at}>{new Date(e.created_at).toLocaleTimeString()}</time><span>{e.message}</span></li>
          ))}
        </ol>
      </div>
      <div className="panel" data-reveal>
        <div className="panel-head"><h2>Pages per minute</h2><span className="faint" style={{ fontSize: 13 }}>last {mins.length} min</span></div>
        {mins.length === 0 ? <p className="empty">Figures appear after the first minute of crawling.</p> : (<>
          <div className="minute-bars" role="img" aria-label={`Pages per minute, latest ${mins[mins.length - 1].pages}`}>
            {mins.map((m) => <i key={m.minute} style={{ height: `${Math.max(4, (m.pages / peak) * 100)}%` }} className={m.errors ? "err" : ""}
              title={`${new Date(m.minute).toLocaleTimeString()}: ${m.pages} pages, ${m.errors} errors, ${m.avg_ms ?? "–"} ms`} />)}
          </div>
          <details className="sample"><summary>As a table</summary>
            <div className="table-wrap flat"><table><thead><tr><th>Minute</th><th className="num">Pages</th><th className="num">Errors</th><th className="num">Avg. response</th></tr></thead>
              <tbody>{[...mins].reverse().map((m) => <tr key={m.minute}><td>{new Date(m.minute).toLocaleTimeString()}</td><td className="num">{count(m.pages)}</td>
                <td className="num">{count(m.errors)}</td><td className="num">{m.avg_ms == null ? "–" : `${m.avg_ms} ms`}</td></tr>)}</tbody></table></div></details>
        </>)}
      </div>
    </div>
  );
}
