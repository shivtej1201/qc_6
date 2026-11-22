import { useEffect, useRef, useState } from "react";
import { api, type Job, type Severity } from "../api";
import { go } from "../main";
import { useViewMotion } from "../motion";
import { Confirm, DownloadMenu, ErrorBox, Icon, ago, count, useLoad, useToast } from "../ui";

/* The simple flow, no setup: sitemap / one page / a sheet -> the URL list (download it) -> Start crawling -> live
   progress with Pause, Resume and Cancel -> the report. Everything on one screen, updated every second. */

type Source = "sitemap" | "single" | "sheet";
const SOURCES: [Source, string, string][] = [
  ["sitemap", "Sitemap", "Paste the sitemap address. QC reads it, and any sitemaps it lists, for every page address."],
  ["single", "One page", "Check a single page."],
  ["sheet", "Upload a file", "A sheet (CSV, Excel .xlsx, text) with page addresses in any column, or sitemap files saved from your browser (.xml, .xml.gz). You can pick several files."],
];
const SEV: Severity[] = ["critical", "high", "medium", "low"];
const SEV_TEXT: Record<Severity, string> = { critical: "Critical", high: "High", medium: "Medium", low: "Low" };

function phase(j: Job): string {
  if (j.status !== "ready") return j.status;                            // extracting | failed | cancelled (list)
  if (!j.run_id) return "ready";
  return j.run_status || "ready";                                       // queued | running | pausing | paused | completed | failed | cancelled
}
const LIVE = ["extracting", "queued", "running", "pausing"];

function speed(j: Job) {
  const p = j.run_progress || {};
  const done = p.pages_done || 0, total = p.pages_total || j.url_count || 0;
  const secs = j.run_started ? (Date.now() - new Date(j.run_started).getTime()) / 1000 : 0;
  const rate = secs > 5 && done > 0 ? done / secs : 0;
  const eta = rate > 0 ? (total - done) / rate : null;
  return { done, total, rate, eta, frac: total ? Math.min(1, done / total) : 0 };
}
function duration(s: number | null): string {
  if (s == null || !isFinite(s)) return "working it out";
  if (s < 60) return "under a minute";
  const m = Math.round(s / 60);
  return m < 60 ? `about ${m} min` : `about ${Math.floor(m / 60)} h ${m % 60} min`;
}

export function StartView({ params, onSite }: { params: URLSearchParams; onSite: (siteId: number) => void }) {
  const [source, setSource] = useState<Source>("sitemap");
  const [value, setValue] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const jobs = useLoad(() => api.jobs(), []);
  const worker = useLoad(() => api.worker(), []);
  const jobId = Number(params.get("job")) || 0;
  const [job, setJob] = useState<Job | null>(null);
  const [nonce, setNonce] = useState(0);
  const ref = useViewMotion<HTMLDivElement>([!!jobs.data]);
  const toast = useToast();
  const fileRef = useRef<HTMLInputElement>(null);

  // the job on screen: refreshed every second while anything is moving
  useEffect(() => {
    if (!jobId) { setJob(null); return; }
    let stop = false, timer = 0;
    const tick = async () => {
      try {
        const j = await api.job(jobId);
        if (stop) return;
        setJob(j);
        if (LIVE.includes(phase(j))) timer = window.setTimeout(tick, 1000);
        else { jobs.reload(); if (j.site_id) onSite(j.site_id); }
      } catch (e: any) { if (!stop) setErr(e.title || e.message); }
    };
    tick();
    return () => { stop = true; window.clearTimeout(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId, job ? phase(job) : "", nonce]);
  // the jobs list and the worker: every few seconds while one is live
  useEffect(() => {
    const live = (jobs.data?.jobs || []).some((j) => LIVE.includes(phase(j)));
    if (!live) return;
    const t = window.setInterval(() => { jobs.reload(); worker.reload(); }, 3000);
    return () => window.clearInterval(t);
  }, [jobs.data, jobs, worker]);

  const open = (id: number) => go("start", { job: id });
  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setErr(null);
    try {
      const j: Job & { skipped_files?: string[] } = source === "sheet" ? await api.uploadList(files) : await api.newList(source, value.trim());
      if (j.skipped_files?.length) toast(`Some files were left out: ${j.skipped_files[0]}`);
      setValue(""); setFiles([]); if (fileRef.current) fileRef.current.value = "";
      jobs.reload();
      open(j.id);
    } catch (x: any) { setErr(x.detail ? `${x.title}. ${x.detail}` : x.title || x.message); }
    finally { setBusy(false); }
  };

  return (
    <div className="view start" ref={ref}>
      <div className="head"><div>
        <h1>Check a website</h1>
        <p className="lede">Give QC a sitemap, one page, or a sheet of pages. It lists the pages, checks every one, and gives you the report.</p>
      </div></div>
      {worker.data && !worker.data.alive && (
        <div className="error-box" role="status" style={{ marginBottom: 16 }}>
          <b>The QC worker isn't running</b>, so checks wait in the queue. Start QC with <span className="mono">start.bat</span> (Windows)
          or <span className="mono">./start.sh</span>; it opens the worker too. If you closed the "QC V6 worker" window, start it again.
        </div>
      )}
      <form className="panel grid start-form" onSubmit={create} data-reveal aria-labelledby="src-h">
        <h2 id="src-h" className="sr-only">What to check</h2>
        <div className="seg" role="group" aria-label="What to check">
          {SOURCES.map(([k, label]) => (
            <button key={k} type="button" aria-pressed={source === k} onClick={() => { setSource(k); setErr(null); }}>{label}</button>
          ))}
        </div>
        <p className="faint" style={{ margin: 0, fontSize: 14 }}>{SOURCES.find((s) => s[0] === source)![2]}</p>
        {source === "sheet" ? (
          <label className="drop">
            <input ref={fileRef} type="file" multiple accept=".csv,.tsv,.txt,.xlsx,.xlsm,.xml,.gz" onChange={(e) => setFiles([...(e.target.files || [])])} />
            <Icon name="page" /><span>{files.length === 1 ? <><b>{files[0].name}</b> ({count(Math.round(files[0].size / 1024))} KB)</>
              : files.length > 1 ? <><b>{files.length} files</b> ({files.slice(0, 3).map((f) => f.name).join(", ")}{files.length > 3 ? "…" : ""})</>
              : <>Choose files, or drop them here</>}</span>
          </label>
        ) : (
          <div className="row start-input">
            <label className="sr-only" htmlFor="start-url">{source === "sitemap" ? "Sitemap address" : "Page address"}</label>
            <input id="start-url" className="input" type="text" inputMode="url" autoComplete="url" required value={value}
              placeholder={source === "sitemap" ? "https://www.example.com/sitemap.xml" : "https://www.example.com/some-page"}
              onChange={(e) => setValue(e.target.value)} />
          </div>
        )}
        {err && <div className="error-box" role="alert">{err}</div>}
        <div><button className="btn primary" type="submit" disabled={busy || (source === "sheet" ? files.length === 0 : !value.trim())}>
          {busy ? "Working…" : source === "sitemap" ? "Get the URLs" : source === "single" ? "Add this page" : files.length > 1 ? "Read the files" : "Read the file"}</button></div>
      </form>

      {jobId > 0 && (job ? <JobCard job={job} onChange={() => { setNonce((n) => n + 1); jobs.reload(); worker.reload(); }} toast={toast} />
        : err ? <ErrorBox text={err} /> : <p className="faint" style={{ marginTop: 16 }}>Loading…</p>)}

      <section className="panel" data-reveal style={{ marginTop: 16 }} aria-labelledby="jobs-h">
        <div className="panel-head"><h2 id="jobs-h">Your checks</h2><span className="faint" style={{ fontSize: 13 }}>newest first</span></div>
        {jobs.error ? <ErrorBox text={jobs.error} onRetry={jobs.reload} /> : !jobs.data ? <p className="faint">Loading…</p> :
          jobs.data.jobs.length === 0 ? <p className="empty">Nothing yet. Paste a sitemap address above to start.</p> : (
            <div className="table-wrap flat" tabIndex={0} role="region" aria-label="Your checks">
              <table className="jobs">
                <thead><tr><th>What</th><th className="hide-phone">Pages</th><th>Status</th><th className="hide-phone">When</th><th className="hide-phone"><span className="sr-only">Open</span></th></tr></thead>
                <tbody>
                  {jobs.data.jobs.map((j) => {
                    const ph = phase(j), sp = speed(j);
                    return (
                      <tr key={j.id} aria-current={j.id === jobId ? "true" : undefined}>
                        <td className="what"><a href={`#/start?job=${j.id}`}><b>{j.primary_host}</b></a><span title={j.label}>{SOURCE_TEXT[j.source]}: {j.label}</span></td>
                        <td className="num hide-phone">{count(j.url_count)}</td>
                        <td><StatusPill ph={ph} />{ph === "running" && <span className="faint" style={{ fontSize: 12, marginLeft: 6 }}>{Math.round(sp.frac * 100)}%</span>}</td>
                        <td className="faint hide-phone" style={{ whiteSpace: "nowrap" }}>{ago(j.created_at)}</td>
                        <td className="hide-phone"><button className="btn small" onClick={() => open(j.id)}>Open</button></td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
      </section>
    </div>
  );
}

const SOURCE_TEXT: Record<string, string> = { sitemap: "Sitemap", single: "Page", sheet: "File", list: "List" };
const PILL: Record<string, [string, string]> = {
  extracting: ["warn", "Reading sitemap"], ready: ["new", "URLs ready"], queued: ["warn", "Waiting to start"], running: ["warn", "Crawling"],
  pausing: ["warn", "Pausing…"], paused: ["new", "Paused"], completed: ["good", "Report ready"], failed: ["bad", "Stopped"], cancelled: ["", "Cancelled"],
};
function StatusPill({ ph }: { ph: string }) {
  const [c, t] = PILL[ph] || ["", ph];
  return <span className={`pill ${c}`}>{t}</span>;
}

function Steps({ ph }: { ph: string }) {
  const at = ["extracting", "failed-list"].includes(ph) ? 0 : ph === "ready" ? 1 : ph === "completed" ? 3 : 2;
  const names = ["Get URLs", "Start crawling", "Crawl & QC", "Report"];
  return (
    <ol className="stepper" aria-label="Progress">
      {names.map((n, i) => <li key={n} className={i < at ? "done" : i === at ? "now" : ""} aria-current={i === at ? "step" : undefined}><i>{i + 1}</i>{n}</li>)}
    </ol>
  );
}

function JobCard({ job: j, onChange, toast }: { job: Job; onChange: () => void; toast: (m: string) => void }) {
  const ph = phase(j);
  const [browser, setBrowser] = useState(false);
  const [busy, setBusy] = useState(false);
  const [blocker, setBlocker] = useState<{ run: number; status: string; text: string } | null>(null);
  const [del, setDel] = useState(false);
  const sp = speed(j);
  const act = async (f: () => Promise<unknown>, msg?: string) => {
    setBusy(true);
    try { await f(); if (msg) toast(msg); setBlocker(null); onChange(); }
    catch (e: any) {
      if (e.data?.busy_run) setBlocker({ run: e.data.busy_run, status: e.data.busy_status, text: e.detail });
      else toast(e.detail ? `${e.title}. ${e.detail}` : e.title || e.message);
    } finally { setBusy(false); }
  };
  const start = () => act(() => api.startList(j.id, browser), "Crawling started.");
  const runCtl = (what: "pause" | "resume" | "cancel", id = j.run_id!) => act(
    () => (what === "pause" ? api.pauseRun(id) : what === "resume" ? api.resumeRun(id) : api.cancelRun(id)),
    what === "pause" ? "Pausing: finishing the pages in hand, then it stops. Nothing already checked is lost."
      : what === "resume" ? "Resuming where it stopped." : "Cancelled.");
  const stage = j.run_progress?.stage;

  return (
    <section className="panel job" data-reveal aria-labelledby="job-h" aria-live="polite" style={{ marginTop: 16 }}>
      <div className="panel-head">
        <h2 id="job-h">{j.primary_host}</h2>
        <StatusPill ph={ph} />
      </div>
      <p className="faint job-src">{SOURCE_TEXT[j.source]}: <span className="mono">{j.label}</span></p>
      <Steps ph={ph} />

      {ph === "extracting" && (<>
        <div className="big-num"><b>{count(j.progress?.urls || 0)}</b> page addresses found
          <span>{j.progress?.sitemaps ? ` in ${count(j.progress.sitemaps)} sitemap${j.progress.sitemaps === 1 ? "" : "s"}` : ""}</span></div>
        <div className="progress-line indeterminate" aria-hidden="true"><i /></div>
        <div className="row"><button className="btn" disabled={busy} onClick={() => act(() => api.cancelList(j.id), "Stopped reading the sitemap.")}>Cancel</button></div>
      </>)}

      {j.status === "failed" && <div className="error-box" role="alert" style={{ whiteSpace: "pre-line" }}>{j.error}</div>}
      {j.status === "cancelled" && <p>Reading the sitemap was cancelled. Paste it again above to start over.</p>}

      {j.status === "ready" && ph === "ready" && (<>
        <div className="big-num"><b>{count(j.url_count)}</b> page{j.url_count === 1 ? "" : "s"} to check
          <span>{[j.note?.duplicates ? `${count(j.note.duplicates)} repeats removed` : "", j.note?.other_hosts ? `${count(j.note.other_hosts)} on other sites left out` : "",
            j.note?.capped ? "the most in one check" : ""].filter(Boolean).join(" · ")}</span></div>
        <div className="row" style={{ flexWrap: "wrap" }}>
          <a className="btn" href={`/api/v2/lists/${j.id}/urls.xlsx`} download><Icon name="page" />Download URLs (Excel)</a>
          <a className="btn" href={`/api/v2/lists/${j.id}/urls.csv`} download><Icon name="page" />Download URLs (CSV)</a>
        </div>
        {j.sample && j.sample.length > 0 && (
          <details className="sample"><summary>See the first {Math.min(20, j.url_count)} addresses</summary>
            <ol>{j.sample.map((u) => <li key={u} className="mono">{u}</li>)}</ol></details>
        )}
      </>)}
      {j.status === "ready" && ph !== "ready" && (
        <p className="url-line">{count(j.url_count)} URL{j.url_count === 1 ? "" : "s"} in the list ·{" "}
          <a href={`/api/v2/lists/${j.id}/urls.xlsx`} download>Excel</a> · <a href={`/api/v2/lists/${j.id}/urls.csv`} download>CSV</a></p>
      )}

      {ph === "ready" && (
        <div className="start-cta">
          <label className="check"><input type="checkbox" checked={browser} onChange={(e) => setBrowser(e.target.checked)} />
            Also open sample pages in a real browser (speed, layout, console errors). Slower.</label>
          <button className="btn primary big" disabled={busy} onClick={start}><Icon name="play" />Start crawling</button>
        </div>
      )}

      {blocker && (
        <div className="error-box" role="alert">
          <p style={{ margin: "0 0 8px" }}>{blocker.text}</p>
          <div className="row">
            {blocker.status === "paused" && <button className="btn" onClick={() => runCtl("resume", blocker.run)}>Resume check {blocker.run}</button>}
            {["running", "queued"].includes(blocker.status) && <button className="btn" onClick={() => runCtl("pause", blocker.run)}>Pause check {blocker.run}</button>}
            <button className="btn" onClick={() => runCtl("cancel", blocker.run).then(start)}>Cancel check {blocker.run} and start this one</button>
          </div>
        </div>
      )}

      {["queued", "running", "pausing", "paused"].includes(ph) && (<>
        <div className="big-num"><b>{count(sp.done)}</b> of {count(sp.total)} pages checked
          <span>{ph === "queued" ? "Waiting for the worker to pick it up…" : ph === "pausing" ? "Pausing: finishing the pages in hand…"
            : ph === "paused" ? "Paused. Resume carries on where it stopped." : stage === "analysing" ? "Every page read. Grouping problems and building the report…"
            : stage === "visits" ? "Opening sample pages in a real browser…"
            : sp.rate ? `${sp.rate.toFixed(1)} pages a second · ${duration(sp.eta)} left` : "Starting…"}</span></div>
        <div className={`progress-line ${ph === "queued" || stage === "analysing" ? "indeterminate" : ""}`} role="progressbar" aria-label="Pages checked"
          aria-valuemin={0} aria-valuemax={sp.total} aria-valuenow={sp.done}><i style={{ width: `${(sp.frac * 100).toFixed(1)}%` }} /></div>
        <div className="row">
          {["running", "queued"].includes(ph) && <button className="btn" disabled={busy} onClick={() => runCtl("pause")}><Icon name="pause" />Pause</button>}
          {ph === "paused" && <button className="btn primary" disabled={busy} onClick={() => runCtl("resume")}><Icon name="play" />Resume</button>}
          <button className="btn quiet" disabled={busy} onClick={() => runCtl("cancel")}><Icon name="close" />Cancel</button>
          <button className="btn quiet" onClick={() => go("live", { run: j.run_id! })}>Details</button>
        </div>
      </>)}

      {ph === "completed" && (<>
        <div className="big-num"><b>{count(j.run_summary?.pages ?? j.url_count)}</b> pages checked
          <span>finished {ago(j.run_finished)}{j.run_summary?.score != null ? ` · health score ${Math.round(j.run_summary.score)}` : ""}</span></div>
        <div className="sev-row">
          {SEV.map((s) => (
            <div key={s} className={`sev-box ${s}`}><span>{SEV_TEXT[s]}</span><b>{count(j.results?.[s]?.problems || 0)}</b>
              <small>{j.results?.[s] ? `on ${count(j.results[s]!.pages)} pages` : "none"}</small></div>
          ))}
        </div>
        <div className="row" style={{ flexWrap: "wrap" }}>
          <DownloadMenu siteId={j.site_id} run={j.run_id} />
          <button className="btn" onClick={() => go("fixes")}>See the problems</button>
          <button className="btn quiet" onClick={start} disabled={busy}>Check again</button>
        </div>
      </>)}

      {ph === "failed" && j.run_id && (<>
        <div className="error-box" role="alert">The crawl stopped: {j.run_error}</div>
        <div className="row" style={{ marginTop: 12 }}><button className="btn primary" onClick={start} disabled={busy}>Try again</button></div>
      </>)}
      {ph === "cancelled" && j.run_id && (<>
        <p>Cancelled after {count(sp.done)} of {count(sp.total)} pages.</p>
        <div className="row"><button className="btn primary" onClick={start} disabled={busy}><Icon name="play" />Start crawling again</button></div>
      </>)}

      {!LIVE.includes(ph) && (
        <div className="row" style={{ marginTop: 14, justifyContent: "flex-end" }}>
          <button className="btn quiet small" onClick={() => setDel(true)}>Remove from this list</button>
        </div>
      )}
      {del && (
        <Confirm title="Remove this from your checks?" confirmText="Remove" onClose={() => setDel(false)}
          onConfirm={async () => { await api.deleteList(j.id); setDel(false); go("start"); }}>
          The URL list goes. Its report stays under Live, Past checks, where you can delete it too.
        </Confirm>
      )}
    </section>
  );
}
