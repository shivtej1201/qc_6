import { useEffect, useRef, useState } from "react";
import { api, type Job, type Pipeline, type QcBlock } from "../api";
import { go } from "../main";
import { useViewMotion } from "../motion";
import { Confirm, DownloadMenu, ErrorBox, Icon, ago, count, useLoad, useToast } from "../ui";

/* The simple flow, no setup: sitemap / one page / a sheet -> the URL list (download it) -> Start crawling -> live
   progress with Pause, Resume and Cancel -> the report. Everything on one screen, updated every second. */

type Source = "sitemap" | "single" | "list" | "sheet";
const SOURCES: [Source, string, string][] = [
  ["sitemap", "Sitemap", "Paste the sitemap address. QC reads it, and any sitemaps it lists, for every page address."],
  ["single", "One page", "Check a single page."],
  ["list", "Several pages", "Type or paste page addresses, one per line. Repeats and other sites are left out."],
  ["sheet", "Upload a file", "A sheet (CSV, Excel .xlsx, text) with page addresses in any column, or sitemap files saved from your browser (.xml, .xml.gz). You can pick several files."],
];

function phase(j: Job): string {
  if (j.status !== "ready") return j.status;                            // extracting | failed | cancelled (list)
  if (!j.run_id) return "ready";
  return j.run_status || "ready";                                       // queued | running | pausing | paused | completed | failed | cancelled
}
const LIVE = ["extracting", "queued", "running", "pausing", "cancelling"];

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

export function StartView({ params, onSite, onSitesChanged }: { params: URLSearchParams; onSite: (siteId: number) => void; onSitesChanged: () => void }) {
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
    let stop = false, timer = 0, first = true;
    const tick = async () => {
      try {
        const j = await api.job(jobId);
        if (stop) return;
        setJob(j);
        if (first) { first = false; jobs.reload(); }      // the open check changed state: the list below shows it too
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
        ) : source === "list" ? (
          <>
            <label className="sr-only" htmlFor="start-list">Page addresses, one per line</label>
            <textarea id="start-list" className="input mono" rows={6} required value={value}
              placeholder={"https://www.example.com/page-a\nhttps://www.example.com/page-b"} onChange={(e) => setValue(e.target.value)} />
          </>
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
          {busy ? "Working…" : source === "sitemap" ? "Get the URLs" : source === "single" ? "Add this page" : source === "list" ? "Add these pages" : files.length > 1 ? "Read the files" : "Read the file"}</button></div>
      </form>

      {jobId > 0 && (job ? <JobCard job={job} onChange={() => { setNonce((n) => n + 1); jobs.reload(); worker.reload(); }} toast={toast}
        onSiteDeleted={() => { onSitesChanged(); jobs.reload(); go("start"); }} />
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

const SOURCE_TEXT: Record<string, string> = { sitemap: "Sitemap", single: "Page", sheet: "File", list: "List", mixed: "Several sources" };
const PILL: Record<string, [string, string]> = {
  extracting: ["warn", "Reading sitemap"], ready: ["new", "URLs ready"], queued: ["warn", "Waiting to start"], running: ["warn", "Crawling"],
  pausing: ["warn", "Pausing…"], cancelling: ["warn", "Stopping…"], paused: ["new", "Paused"], completed: ["good", "Report ready"], failed: ["bad", "Stopped"], cancelled: ["", "Cancelled"],
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

function JobCard({ job: j, onChange, toast, onSiteDeleted }: { job: Job; onChange: () => void; toast: (m: string) => void; onSiteDeleted: () => void }) {
  const [delSite, setDelSite] = useState(false);
  const ph = phase(j);
  const [browser, setBrowser] = useState(false);
  const [pace, setPace] = useState<string | null>(null);
  const chosen = pace || j.speed_default || "target";
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
  const [reuse, setReuse] = useState(true);
  const start = () => act(() => api.startList(j.id, browser, chosen, reuse), "Crawling started.");
  const runCtl = (what: "pause" | "resume" | "cancel", id = j.run_id!) => act(
    () => (what === "pause" ? api.pauseRun(id) : what === "resume" ? api.resumeRun(id) : api.cancelRun(id)),
    what === "pause" ? "Pausing: finishing the pages in hand, then it stops. Nothing already checked is lost."
      : what === "resume" ? "Resuming where it stopped." : "Cancelling: the pages in hand finish first.");
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
        <div className="big-num"><b>{count(j.pipeline?.queued ?? j.progress?.queued ?? 0)}</b> unique page addresses so far
          <span>{j.progress?.sitemaps ? ` from ${count(j.progress.sitemaps)} sitemap${j.progress.sitemaps === 1 ? "" : "s"}` : ""}
            {j.pipeline?.read ? ` · ${count(j.pipeline.read)} rows read` : ""}</span></div>
        <div className="progress-line indeterminate" aria-hidden="true"><i /></div>
        <div className="row"><button className="btn" disabled={busy} onClick={() => act(() => api.cancelList(j.id), "Stopped reading.")}>Cancel</button></div>
      </>)}
      {j.pipeline && <PipelineView p={j.pipeline} listId={j.id} />}
      {j.status === "ready" && !j.run_id && j.error && <div className="error-box" role="alert" style={{ whiteSpace: "pre-line" }}>{j.error}</div>}

      {j.status === "failed" && <div className="error-box" role="alert" style={{ whiteSpace: "pre-line" }}>{j.error}</div>}
      {j.status === "failed" && /403|bot check|refused|CDN/i.test(j.error || "") && <AllowRule siteId={j.site_id} toast={toast} />}
      {j.status === "cancelled" && <p>Reading the sitemap was cancelled. Paste it again above to start over.</p>}

      {j.status === "ready" && ph === "ready" && (<>
        <div className="big-num"><b>{count(j.url_count)}</b> page{j.url_count === 1 ? "" : "s"} to check
          <span>{[j.note?.duplicates ? `${count(j.note.duplicates)} repeats removed` : "", j.note?.other_hosts ? `${count(j.note.other_hosts)} on other sites left out` : "",
            j.note?.capped ? "the most in one check" : ""].filter(Boolean).join(" · ")}</span></div>
        <div className="row" style={{ flexWrap: "wrap" }}>
          <a className="btn" href={`/api/v2/lists/${j.id}/urls.xlsx`} download><Icon name="page" />Download URLs (Excel)</a>
          <a className="btn" href={`/api/v2/lists/${j.id}/urls.csv`} download><Icon name="page" />Download URLs (CSV)</a>
        </div>
        <AddMore listId={j.id} onAdded={onChange} toast={toast} />
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
          {j.speeds && (
            <fieldset className="speed-pick">
              <legend>Speed</legend>
              <div className="seg" role="radiogroup" aria-label="Speed">
                {j.speeds.map((s) => (
                  <button key={s.id} type="button" role="radio" aria-checked={chosen === s.id}
                    onClick={() => setPace(s.id)}>{s.label} · {s.rps === s.max_rps ? s.rps : `${s.rps}–${s.max_rps}`}/s</button>
                ))}
              </div>
              {(() => { const s = j.speeds!.find((x) => x.id === chosen); return s && (
                <p className="faint speed-note">{count(j.url_count)} pages: {duration(s.minutes * 60)} at {s.rps} requests a second to your site.
                  {" "}QC slows down by itself if the site answers slowly or says "too many requests". Pick a faster speed only for a site that can take it.</p>); })()}
            </fieldset>
          )}
          <label className="check"><input type="checkbox" checked={browser} onChange={(e) => setBrowser(e.target.checked)} />
            Also open sample pages in a real browser (speed, layout, console errors). Slower.</label>
          <label className="check"><input type="checkbox" checked={reuse} onChange={(e) => setReuse(e.target.checked)} />
            Reuse links that answered 200 in this site's last check (within 24 h). Anything that failed is always checked again.</label>
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

      {["queued", "running", "pausing", "paused", "cancelling"].includes(ph) && (<>
        <div className="big-num"><b>{count(sp.done)}</b> of {count(sp.total)} pages checked
          <span>{ph === "queued" ? "Waiting for the worker to pick it up…" : ph === "pausing" ? "Pausing: finishing the pages in hand…" : ph === "cancelling" ? "Stopping: finishing the pages in hand, then it's cancelled…"
            : ph === "paused" ? "Paused. Resume carries on where it stopped." : stage === "analysing" ? "Every page read. Grouping problems and building the report…"
            : stage === "visits" ? "Opening sample pages in a real browser…"
            : sp.rate ? `${sp.rate.toFixed(1)} pages a second · ${duration(sp.eta)} left` : "Starting…"}</span></div>
        <div className={`progress-line ${ph === "queued" || stage === "analysing" ? "indeterminate" : ""}`} role="progressbar" aria-label="Pages checked"
          aria-valuemin={0} aria-valuemax={sp.total} aria-valuenow={sp.done}><i style={{ width: `${(sp.frac * 100).toFixed(1)}%` }} /></div>
        <div className="row">
          {["running", "queued"].includes(ph) && <button className="btn" disabled={busy} onClick={() => runCtl("pause")}><Icon name="pause" />Pause</button>}
          {ph === "paused" && <button className="btn primary" disabled={busy} onClick={() => runCtl("resume")}><Icon name="play" />Resume</button>}
          {ph !== "cancelling" && <button className="btn quiet" disabled={busy} onClick={() => runCtl("cancel")}><Icon name="close" />Cancel</button>}
          <button className="btn quiet" onClick={() => go("live", { run: j.run_id! })}>Details</button>
        </div>
      </>)}

      {ph === "completed" && (<>
        <div className="big-num"><b>{count(j.run_summary?.pages ?? j.url_count)}</b> pages checked
          <span>finished {ago(j.run_finished)}{j.run_summary?.score != null ? ` · health score ${Math.round(j.run_summary.score)}` : ""}</span></div>
        {j.run_id && <QcResult runId={j.run_id} toast={toast} />}
        <div className="row" style={{ flexWrap: "wrap" }}>
          <DownloadMenu siteId={j.site_id} run={j.run_id} />
          <button className="btn quiet" onClick={() => go("fixes")}>All problems</button>
          <button className="btn quiet" onClick={start} disabled={busy}>Check again</button>
        </div>
      </>)}

      {ph === "failed" && j.run_id && (<>
        <div className="error-box" role="alert">The crawl stopped: {j.run_error}</div>
        {/403|challenge|bot|CDN|blocked/i.test(j.run_error || "") && <AllowRule siteId={j.site_id} toast={toast} />}
        <div className="row" style={{ marginTop: 12 }}>
          <button className="btn primary" disabled={busy} onClick={() => act(() => api.retryRun(j.run_id!), "Back in the queue. It carries on from where it stopped.")}>
            <Icon name="play" />Retry from where it stopped</button>
          <button className="btn quiet" onClick={start} disabled={busy}>Start a new crawl</button></div>
      </>)}
      {ph === "cancelled" && j.run_id && (<>
        <p>Cancelled after {count(sp.done)} of {count(sp.total)} pages.</p>
        <div className="row"><button className="btn primary" onClick={start} disabled={busy}><Icon name="play" />Start crawling again</button></div>
      </>)}

      {!LIVE.includes(ph) && (
        <div className="row" style={{ marginTop: 14, justifyContent: "flex-end" }}>
          <button className="btn quiet small" onClick={() => setDel(true)}>Remove from this list</button>
          <button className="btn quiet small" onClick={() => setDelSite(true)}>Delete {j.primary_host} and all its checks…</button>
        </div>
      )}
      {delSite && (
        <Confirm title={`Delete ${j.primary_host}?`} confirmText="Delete the site" typeToConfirm={j.primary_host} onClose={() => setDelSite(false)}
          onConfirm={async () => {
            const site = await api.site(j.site_id);
            const out = await api.deleteSite(j.site_id, site.name);
            toast(`${j.primary_host} deleted, with ${out.checks} check${out.checks === 1 ? "" : "s"}.`);
            setDelSite(false); onSiteDeleted();
          }}>
          <p style={{ margin: 0 }}>Removes everything QC stored about {j.primary_host}: every check, page, problem, journey, screenshot and report.
            This can't be undone.</p>
        </Confirm>
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


/* A CDN or bot check said no. QC doesn't get round it; it asks to be let in, openly. */
function AllowRule({ siteId, toast }: { siteId: number; toast: (m: string) => void }) {
  const [rule, setRule] = useState<{ header: string; token: string; rules: Record<string, string> } | null>(null);
  const show = async () => {
    try { setRule(await api.access(siteId)); } catch (e: any) { toast(e.title || e.message); }
  };
  const copy = async (t: string) => {
    try { await navigator.clipboard.writeText(t); toast("Copied. Send it to the site's CDN or security team."); }
    catch { toast("Select the text and copy it."); }
  };
  return (
    <div className="panel allow-rule" style={{ marginTop: 12 }}>
      <p style={{ margin: 0 }}><b>Let QC in, openly.</b> QC identifies itself on every request and never pretends to be a person.
        Ask the site's CDN or security team to allow requests that carry QC's header.</p>
      {!rule ? <div className="row"><button className="btn" onClick={show}>Show the allow rule</button></div> : (<>
        <p className="faint" style={{ margin: "8px 0 4px", fontSize: 13 }}>Header <span className="mono">{rule.header}: {rule.token}</span></p>
        {Object.entries(rule.rules).map(([cdn, text]) => (
          <details key={cdn} className="sample"><summary>{cdn === "akamai" ? "Akamai" : cdn === "cloudflare" ? "Cloudflare" : cdn}</summary>
            <pre className="ai-ev" style={{ whiteSpace: "pre-wrap" }}>{text}</pre>
            <button className="btn small" onClick={() => copy(text)}>Copy</button></details>
        ))}
      </>)}
    </div>
  );
}

/* The URL QC in four numbers, the problem lists, and a ticket for the Ops team ready to copy. */
function QcResult({ runId, toast }: { runId: number; toast: (m: string) => void }) {
  const r = useLoad(() => api.opsTicket(runId), [runId]);
  const [tab, setTab] = useState<"problems" | "ticket">("problems");
  if (r.error) return <p className="faint">URL results aren't available for this check: {r.error}</p>;
  if (!r.data) return <p className="faint">Loading the results…</p>;
  const { qc, ticket } = r.data;
  const copy = async (text: string, what: string) => {
    try { await navigator.clipboard.writeText(text); toast(`${what} copied.`); }
    catch { toast("Couldn't copy: select the text and press Ctrl+C."); }
  };
  const tiles: [string, number, string, string][] = [
    ["Pages not OK", qc.pages.not_ok, `of ${count(qc.pages.checked)} pages checked`, qc.pages.not_ok ? "bad" : "good"],
    ["Broken links", qc.broken_links.unique, `on ${count(qc.broken_links.pages)} pages · ${count(qc.links.unique)} links checked`, qc.broken_links.unique ? "bad" : "good"],
    ["Broken images", qc.broken_images.unique, `on ${count(qc.broken_images.pages)} pages · ${count(qc.images.unique)} images checked`, qc.broken_images.unique ? "bad" : "good"],
    ["Redirecting links", qc.redirects.unique, `on ${count(qc.redirects.pages)} pages`, qc.redirects.unique ? "warn" : "good"],
  ];
  return (
    <div className="qc-result">
      <div className="qc-tiles">
        {tiles.map(([name, n, sub, tone]) => (
          <div key={name} className={`qc-tile ${tone}`}><span>{name}</span><b>{count(n)}</b><small>{sub}</small></div>
        ))}
      </div>
      <div className="seg qc-tabs" role="tablist" aria-label="Results">
        <button role="tab" id="qt-p" aria-controls="qp-p" aria-selected={tab === "problems"} onClick={() => setTab("problems")}>Problems</button>
        <button role="tab" id="qt-t" aria-controls="qp-t" aria-selected={tab === "ticket"} onClick={() => setTab("ticket")}>Ops ticket</button>
      </div>
      {tab === "problems" ? (
        <div role="tabpanel" id="qp-p" aria-labelledby="qt-p">
          {qc.pages.not_ok + qc.broken_links.unique + qc.broken_images.unique + qc.redirects.unique === 0
            ? <p className="good-note">Every page, link and image answers 200. Nothing to fix.</p> : (<>
            <QcList title="Pages not answering 200" block={{ unique: qc.pages.not_ok, pages: qc.pages.not_ok, fix: qc.pages.fix, rows: qc.pages.rows }} kind="page" />
            <QcList title="Broken links" block={qc.broken_links} kind="link" />
            <QcList title="Broken images" block={qc.broken_images} kind="image" />
            <QcList title="Links that redirect" block={qc.redirects} kind="redirect" />
            <p className="faint" style={{ fontSize: 13 }}>Each list shows the 10 used on most pages. The Excel report (Download report → Full audit workbook) lists every one, with every page it is on.</p>
          </>)}
        </div>
      ) : (
        <div role="tabpanel" id="qp-t" aria-labelledby="qt-t" className="ticket">
          <p className="faint" style={{ margin: "0 0 8px" }}>Ready to paste into the Ops team's ticket. Suggested priority: <b>{ticket.priority || "none, nothing to fix"}</b>.</p>
          <label className="ticket-label" htmlFor="ticket-title">Title</label>
          <div className="ticket-title"><input id="ticket-title" readOnly value={ticket.title} onFocus={(e) => e.target.select()} />
            <button className="btn" onClick={() => copy(ticket.title, "Title")}>Copy title</button></div>
          <label className="ticket-label" htmlFor="ticket-text">Ticket</label>
          <textarea id="ticket-text" readOnly value={ticket.text} rows={18} onFocus={(e) => e.target.select()} />
          <div className="row"><button className="btn primary" onClick={() => copy(ticket.text, "Ticket")}><Icon name="page" />Copy the whole ticket</button></div>
        </div>
      )}
    </div>
  );
}

function QcList({ title, block, kind }: { title: string; block: QcBlock; kind: "page" | "link" | "image" | "redirect" }) {
  if (!block.unique) return null;
  const rows = block.rows.slice(0, 10);
  return (
    <section className="qc-list">
      <h3>{title} <span className="faint">{count(block.unique)}</span></h3>
      <div className="table-wrap"><table>
        <thead><tr><th>{kind === "page" ? "Page" : kind === "image" ? "Image" : "Link"}</th><th>Status</th>
          {kind !== "page" && <th className="num">On pages</th>}{kind !== "page" && <th className="hide-phone">For example on</th>}</tr></thead>
        <tbody>{rows.map((x) => (
          <tr key={x.url}><td className="mono url-cell">{x.url}{x.final_url ? <small>goes to {x.final_url}</small> : null}{x.text ? <small>link text “{x.text}”</small> : null}</td>
            <td>{x.status}</td>
            {kind !== "page" && <td className="num">{count(x.pages)}</td>}
            {kind !== "page" && <td className="mono url-cell hide-phone">{x.example}</td>}</tr>
        ))}</tbody>
      </table></div>
      <p className="fix-line">What to do: {block.fix}</p>
    </section>
  );
}

/* One progress model for every source (sitemap, typed pages, sheet, several at once). */
function PipelineView({ p, listId }: { p: Pipeline; listId: number }) {
  const cells: [string, number, string?][] = [
    ["Read", p.read], ["Valid URLs", p.valid], ["Duplicates removed", p.duplicates],
    ["Unusable / other sites", p.invalid + p.other_hosts + p.outside_scope], ["Queued (unique)", p.queued],
    ["Crawled", p.completed], ["Failed", p.failed, "bad"], ["Remaining", p.remaining],
  ];
  const srcs = Object.entries(p.by_source || {});
  return (
    <div className="pipeline" aria-label="Progress">
      <dl className="pipe-grid">
        {cells.map(([k, v, cls]) => <div key={k} className={cls && v ? cls : ""}><dt>{k}</dt><dd>{count(v)}</dd></div>)}
      </dl>
      {p.links_reused > 0 && <p className="faint">{count(p.links_reused)} links reused from the previous check (answered 200, not fetched again).</p>}
      {srcs.length > 0 && (
        <details className="pipe-src" open={srcs.length > 1}>
          <summary>By source ({srcs.length})</summary>
          <div className="table-wrap flat"><table>
            <thead><tr><th>Source</th><th className="num">Read</th><th className="num">Added</th><th className="num">Duplicates</th><th className="num">Unusable</th></tr></thead>
            <tbody>{srcs.map(([name, c]) => (
              <tr key={name}><td className="mono" title={name}>{name}</td><td className="num">{count(c.rows)}</td><td className="num">{count(c.added)}</td>
                <td className="num">{count(c.duplicates)}</td><td className="num">{count(c.invalid + c.other_hosts + c.outside_scope)}</td></tr>))}</tbody>
          </table></div>
          {p.rejected > 0 && <a href={`/api/v2/lists/${listId}/rejected.csv`} download>Download the {count(p.rejected)} unusable rows, with the reason</a>}
        </details>
      )}
    </div>
  );
}

function AddMore({ listId, onAdded, toast }: { listId: number; onAdded: () => void; toast: (m: string) => void }) {
  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState<"list" | "sitemap" | "file">("list");
  const [value, setValue] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  if (!open) return <button className="btn quiet" onClick={() => setOpen(true)}>+ Add more URLs from another sitemap, typed pages or a file</button>;
  const go_ = async () => {
    setBusy(true);
    try {
      if (kind === "file") await api.addFilesToList(listId, files);
      else await api.addToList(listId, kind, value.trim());
      toast("Adding: repeats of URLs already in the list are left out.");
      setValue(""); setFiles([]); setOpen(false); onAdded();
    } catch (e: any) { toast(e.detail ? `${e.title}. ${e.detail}` : e.title || e.message); }
    finally { setBusy(false); }
  };
  return (
    <div className="panel add-more">
      <div className="seg" role="group" aria-label="Add from">
        {([["list", "Pages"], ["sitemap", "Sitemap"], ["file", "File"]] as const).map(([k, l]) =>
          <button key={k} type="button" aria-pressed={kind === k} onClick={() => setKind(k)}>{l}</button>)}
      </div>
      {kind === "file" ? <input type="file" multiple accept=".csv,.tsv,.txt,.xlsx,.xlsm,.xml,.gz" onChange={(e) => setFiles([...(e.target.files || [])])} />
        : kind === "sitemap" ? <input className="input" value={value} placeholder="https://www.example.com/sitemap-2.xml" onChange={(e) => setValue(e.target.value)} />
        : <textarea className="input mono" rows={4} value={value} placeholder="One address per line" onChange={(e) => setValue(e.target.value)} />}
      <div className="row">
        <button className="btn primary" disabled={busy || (kind === "file" ? !files.length : !value.trim())} onClick={go_}>{busy ? "Adding…" : "Add to this list"}</button>
        <button className="btn quiet" onClick={() => setOpen(false)}>Close</button>
      </div>
    </div>
  );
}
