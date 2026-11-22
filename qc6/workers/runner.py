"""Run orchestration and the job queue (blueprint §2, §5).

`python -m qc6.workers.runner` starts a worker: it claims queued runs with SELECT … FOR UPDATE SKIP LOCKED, runs the
stages the run asks for (coverage -> visits -> journeys), and reports progress over LISTEN/NOTIFY.
One site never has two runs at once (the claim skips sites that already have a running run).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import traceback
from typing import Any, Dict, List, Optional

from qc6.db.pool import get_pool
from qc6.settings import settings
from qc6.workers.analysis import Item, apply, close_run, notify, snapshot_score

log = logging.getLogger("qc6.runner")

CLAIM_SQL = """
UPDATE runs SET status = 'running', started_at = COALESCE(started_at, now()), worker_id = $1, heartbeat_at = now(),
                attempts = attempts + 1
WHERE id = (
  SELECT r.id FROM runs r WHERE r.status = 'queued'
    AND NOT EXISTS (SELECT 1 FROM runs x WHERE x.site_id = r.site_id AND x.status IN ('running', 'pausing', 'cancelling'))
  ORDER BY r.id FOR UPDATE SKIP LOCKED LIMIT 1)
RETURNING id"""

HEARTBEAT_S = 15
STALE_S = 90            # no heartbeat for this long: the worker is gone (crash, kill, power loss)
MAX_ATTEMPTS = 3

# A check whose worker stopped is put back in the queue and resumes where it stopped (finished pages stay finished);
# after MAX_ATTEMPTS starts it is failed with a plain reason instead of looping. A check that was pausing becomes paused.
RECOVER_SQL = """
UPDATE runs SET
  status = CASE WHEN status = 'pausing' THEN 'paused' WHEN status = 'cancelling' THEN 'cancelled'
                WHEN attempts >= $2 THEN 'failed' ELSE 'queued' END,
  error = CASE WHEN status = 'running' AND attempts >= $2
               THEN 'The worker running this check stopped ' || attempts || ' times, so QC stopped retrying. Look at the worker log, then start a new check.'
               ELSE error END,
  finished_at = CASE WHEN (status = 'running' AND attempts >= $2) OR status = 'cancelling' THEN now() ELSE finished_at END,
  worker_id = NULL
WHERE status IN ('running', 'pausing', 'cancelling') AND (
  -- no heartbeat for a while (a run claimed before heartbeats existed counts from when it started)
  (heartbeat_at IS NOT NULL AND heartbeat_at < now() - make_interval(secs => $1))
  OR (worker_id IS NOT NULL AND COALESCE(heartbeat_at, started_at, created_at) < now() - make_interval(secs => $1))
  -- or its worker is known and has gone quiet: closed window, crash, reboot. Faster than waiting for the heartbeat
  OR (worker_id IS NOT NULL AND EXISTS (SELECT 1 FROM workers w WHERE w.id = runs.worker_id AND w.last_seen < now() - make_interval(secs => $3)))
  -- or nobody claimed it and it was left 'pausing' with nothing running it (paused straight away is right)
  OR (worker_id IS NULL AND status IN ('pausing', 'cancelling') AND COALESCE(heartbeat_at, started_at, created_at) < now() - make_interval(secs => $1)))
RETURNING id, status"""
WORKER_QUIET_S = 45      # a worker writes workers.last_seen at least every HEARTBEAT_S; three missed means it's gone


async def recover_stale(pool, stale_s: float = STALE_S, max_attempts: int = MAX_ATTEMPTS) -> List[Dict[str, Any]]:
    out = [dict(r) for r in await pool.fetch(RECOVER_SQL, float(stale_s), max_attempts, float(WORKER_QUIET_S))]
    if out:
        from qc6 import events
        for r in out:
            await events.record(pool, r["id"], "worker_lost", {
                "queued": "Its worker stopped answering; the check went back in the queue and resumes where it stopped.",
                "paused": "Its worker stopped answering while pausing; the check is paused.",
                "cancelled": "Its worker stopped answering while cancelling; the check is cancelled.",
                "failed": "Its worker stopped answering again; QC stopped retrying."}.get(r["status"], "Its worker stopped answering."),
                status=r["status"])
    return out


async def worker_seen(pool, run_id: Optional[int] = None, status: Optional[str] = None) -> None:
    import socket
    await pool.execute("INSERT INTO workers (id, last_seen, run_id, hostname, pid, status) VALUES ($1, now(), $2, $3, $4, $5) "
                       "ON CONFLICT (id) DO UPDATE SET last_seen = now(), run_id = EXCLUDED.run_id, hostname = EXCLUDED.hostname, "
                       "pid = EXCLUDED.pid, status = EXCLUDED.status", settings.worker_id, run_id, socket.gethostname()[:120],
                       os.getpid(), status or ("busy" if run_id else "idle"))


KEEP_DAYS = int(os.environ.get("QC6_KEEP_METRICS_DAYS", "90"))


async def prune(pool, days: int = 0) -> Dict[str, int]:
    """Retention (CrawlPulse system architecture §4.3): per-minute figures and timeline lines of finished checks older
    than QC6_KEEP_METRICS_DAYS (default 90) are removed; checks, pages, problems and reports are never touched here."""
    d = float(days or KEEP_DAYS)
    m = await pool.execute("DELETE FROM run_minutes WHERE minute < now() - make_interval(days => $1::int)", int(d))
    e = await pool.execute("DELETE FROM run_events e USING runs r WHERE r.id = e.run_id AND e.created_at < now() - make_interval(days => $1::int) "
                           "AND r.status IN ('completed', 'failed', 'cancelled')", int(d))
    await pool.execute("DELETE FROM workers WHERE last_seen < now() - interval '7 days'")
    return {"minutes": int(m.split()[-1]), "events": int(e.split()[-1])}


async def worker_alive(pool) -> bool:
    return bool(await pool.fetchval("SELECT EXISTS (SELECT 1 FROM workers WHERE last_seen > now() - make_interval(secs => $1))",
                                    float(WORKER_QUIET_S)))


async def _heartbeat(pool, run_id: int) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_S)
        try:
            await pool.execute("UPDATE runs SET heartbeat_at = now() WHERE id = $1 AND worker_id = $2", run_id, settings.worker_id)
            await worker_seen(pool, run_id)
        except Exception:                       # a missed beat is fine; three in a row and another worker takes over
            log.warning("heartbeat for run %s failed", run_id)


async def discover_urls(site: Dict[str, Any], opts: Dict[str, Any], progress_callback=None) -> List[str]:
    from qc6.presets import max_pages
    from qc6.site_access import apply_scope, httpx_hook
    limit = max_pages(site.get("settings") or {}, opts)
    scope = (site.get("settings") or {}).get("scope")
    if opts.get("urls"):
        kept, opts["_scope_excluded"] = apply_scope(dict.fromkeys(opts["urls"]), scope)
        return kept[:limit]
    from qcv5.crawler.sitemap_extractor import SitemapExtractor
    sitemaps = [x for x in (opts.get("sitemap"), (site.get("settings") or {}).get("sitemap")) if x]
    from qc6.workers.coverage import identity_headers
    headers = identity_headers(site)
    if not sitemaps:                     # like a crawler would: robots.txt Sitemap lines, else /sitemap.xml
        import httpx
        try:
            from qcv5.crawler.safe_net import safe_transport
            async with httpx.AsyncClient(timeout=15, headers=headers, event_hooks={"request": [httpx_hook(site)]},
                                         transport=safe_transport(settings.allow_private_targets)) as c:
                r = await c.get(site["base_url"] + "/robots.txt")
                if r.status_code == 200:
                    sitemaps = [l.split(":", 1)[1].strip() for l in r.text.splitlines() if l.lower().startswith("sitemap:")]
        except httpx.HTTPError:
            pass
        sitemaps = sitemaps or [site["base_url"] + "/sitemap.xml"]
    # Honest identity on every sitemap request, and no "retry in a disguised browser" when the CDN says no (blueprint §1.1)
    ex = SitemapExtractor(allow_local=settings.allow_private_targets, max_urls=limit,
                          use_browser_fallback=False)
    ex.client.headers.update(headers)
    ex.client.event_hooks["request"].append(httpx_hook(site))     # token / staging login: the site's hosts only
    ex.robots_manager.user_agent = headers["User-Agent"]
    try:
        res = await ex.extract_all(sitemaps, progress_callback=progress_callback)
    finally:
        await ex.close()
    if not res.get("unique_urls"):
        if (res.get("hierarchy_summary") or {}).get("blocked_by_waf_or_403"):
            raise RuntimeError(
                f"{site['primary_host']} refused to show QC its sitemap (HTTP 403 from the CDN). QC doesn't try to get round a "
                "block. Two ways forward: (1) open the sitemap in your own browser, save it (Ctrl+S) and use Upload a file; "
                "(2) for the crawl itself, use Show the allow rule below and ask your "
                "CDN team to add it, since the pages are usually behind the same protection.")
    from qc6.url_lists import dedupe                 # same page with tracking tags / reordered parameters: once
    entries = res.get("unique_urls", [])
    unique, opts["_repeats"] = dedupe(u["normalized_url"] for u in entries)
    kept, opts["_scope_excluded"] = apply_scope(unique, scope)
    kept = kept[:limit]
    # which sitemap each page came from (the report's Sitemap Progress) and the sitemap's hreflang alternates
    by_url = {u["normalized_url"]: u for u in entries}
    sitemaps: List[str] = []
    index: Dict[str, int] = {}
    idx: List[int] = []
    alternates: List[List[str]] = []
    for u in kept:
        e = by_url.get(u) or {}
        sm = e.get("source_sitemap_url") or ""
        if sm not in index:
            index[sm] = len(sitemaps)
            sitemaps.append(sm)
        idx.append(index[sm])
        for a in e.get("alternates") or []:
            alternates.append([u, a.get("hreflang", ""), a.get("href", "")])
    opts["_sitemaps"], opts["_sm_idx"], opts["_alternates"] = sitemaps, idx, alternates
    return kept


def save_alternates(opts: Dict[str, Any], path: str) -> None:
    """Sitemap hreflang pairs can be many (pages x languages): kept in a gzip file next to the check, not in its row."""
    alt = opts.pop("_alternates", None) or []
    opts["_alternates_count"] = len(alt)
    if not alt:
        return
    import gzip
    import json
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(alt, f)
    opts["_alternates_file"] = path


async def journey_stage(pool, browser, run_id: int, site: Dict[str, Any], fast: bool = False,
                        opts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    from qc6.workers.journey import JourneyRunner
    ids = (opts or {}).get("journey_ids")
    rows = await pool.fetch("SELECT id, name, spec FROM journeys WHERE site_id = $1 AND enabled AND ($2::bigint[] IS NULL OR id = ANY($2::bigint[])) "
                            "ORDER BY id", site["id"], ids)
    from qc6.crawl.budget import HostBudget
    from qc6.presets import engine_options
    rate = engine_options(site.get("settings") or {}, opts or {})["target_rps"]
    await HostBudget.ensure(pool, site["primary_host"], rate_rps=rate, burst=max(2.0, rate))
    budget = HostBudget(pool, site["primary_host"], share=0.1)       # journeys take a small share of the site's rate
    runner = JourneyRunner(browser, site, settings.objects_dir, fast=fast, budget=budget)
    items, passed = [], 0
    for j in rows:
        out = await runner.run(j["spec"])
        async with pool.acquire() as conn:
            jr = await conn.fetchval(
                "INSERT INTO journey_runs (journey_id, run_id, device, status, failed_step, failure_text, duration_ms) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id",
                j["id"], run_id, out.device, out.status, out.failed_step, out.failure_text, out.duration_ms)
            await conn.executemany(
                "INSERT INTO step_results (journey_run_id, idx, action, label, status, detail, screenshot, duration_ms) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
                [(jr, s.idx, s.action, s.label, s.status, s.detail, s.screenshot, s.duration_ms) for s in out.steps])
            await notify(conn, run_id, "journey", name=out.name, status=out.status, failed_step=out.failed_step)
        if out.status == "passed":
            passed += 1
        else:
            first = out.failed_step or 0
            url = site["base_url"]
            items.append(Item("JOURNEY_FAILED", url, "journey", None, out.name, {"value": first},
                              {"failure": out.failure_text, "device": out.device}))
    applied = await apply(pool, run_id, site["id"], items) if items else {}
    moved = await close_run(pool, run_id, site["id"], ["journey"], complete=True)
    return {"journeys": len(rows), "journeys_passed": passed, "journey_lifecycle": moved, **{f"journey_{k}": v for k, v in applied.items()}}


async def execute_run(pool, run_id: int, browser=None, fast_journeys: bool = False) -> Dict[str, Any]:
    """Run the stages of one check. Stages already finished (a resumed run) are skipped. Pause: set the run to
    'pausing'; the coverage crawl stops at its checkpoint, visitors finish the page they're on, journeys stop between
    journeys, and the run becomes 'paused'. Resume puts it back in the queue."""
    run = await pool.fetchrow("SELECT * FROM runs WHERE id = $1", run_id)
    site = dict(await pool.fetchrow("SELECT * FROM sites WHERE id = $1", run["site_id"]))
    opts: Dict[str, Any] = dict(run["options"] or {})
    kind = run["kind"]
    summary: Dict[str, Any] = dict(run["summary"] or {})
    done_stages = list((run["progress"] or {}).get("stages_done", []))
    control: Dict[str, Any] = {"pause": False, "cancel": False, "engine": None, "stop": asyncio.Event()}
    own_pw = None

    async def watch():
        while True:
            await asyncio.sleep(1.0)
            st = await pool.fetchval("SELECT status FROM runs WHERE id = $1", run_id)
            if st in ("pausing", "cancelling", "cancelled", None):      # pause, cancel, or the check was deleted
                control["pause"] = True
                control["cancel"] = st != "pausing"
                control["stop"].set()
                if control["engine"] is not None:
                    control["engine"].request_stop()
                return

    from qc6 import events
    STAGE_TEXT = {"coverage": "Every page read and checked", "visits": "Sample pages opened in a real browser",
                  "extras": "Extra checks done", "journeys": "Journeys run", "render": "JavaScript pages rendered and compared"}

    async def stage_done(name: str, part: Dict[str, Any]):
        await events.record(pool, run_id, "stage", STAGE_TEXT.get(name, f"{name} finished"), stage=name)
        summary.update(part)
        done_stages.append(name)
        await pool.execute("UPDATE runs SET summary = $2, progress = progress || $3::jsonb WHERE id = $1",
                           run_id, summary, {"stages_done": done_stages})

    async def paused() -> Dict[str, Any]:
        if control["cancel"]:                    # cancelled while running: 'cancelling' becomes 'cancelled' once stopped
            await pool.execute("UPDATE runs SET status = CASE WHEN status = 'cancelling' THEN 'cancelled' ELSE status END, "
                               "summary = $2, finished_at = COALESCE(finished_at, now()) WHERE id = $1", run_id, summary)
            await events.record(pool, run_id, "cancelled", "Stopped and cancelled. Pages already checked are kept.")
            async with pool.acquire() as conn:
                await notify(conn, run_id, "cancelled")
            return {**summary, "cancelled": True}
        await pool.execute("UPDATE runs SET status = 'paused', summary = $2 WHERE id = $1 AND status = 'pausing'", run_id, summary)
        await events.record(pool, run_id, "paused", "Paused at a checkpoint. Resume carries on from here.")
        async with pool.acquire() as conn:
            await notify(conn, run_id, "paused", stages_done=done_stages)
        return {**summary, "paused": True}

    watcher = asyncio.create_task(watch())
    try:
        if await pool.fetchval("UPDATE runs SET status = 'running', started_at = COALESCE(started_at, now()) "
                               "WHERE id = $1 AND status NOT IN ('cancelled', 'cancelling', 'pausing', 'paused') RETURNING id", run_id) is None:
            control["cancel"] = control["pause"] = True       # cancelled (or paused) before it got going
            return await paused()
        resumed = bool(done_stages) or (run["attempts"] or 0) > 1 or bool((run["progress"] or {}).get("pages_done"))
        await events.record(pool, run_id, "started", "Resumed where it stopped" if resumed else "Started",
                            kind=kind, stages_done=done_stages)
        async with pool.acquire() as conn:
            await notify(conn, run_id, "started", kind=kind, resumed=bool(done_stages))
        need_browser = kind in ("visit", "journey", "full")
        if need_browser and browser is None:
            from playwright.async_api import async_playwright
            own_pw = await async_playwright().start()
            browser = await own_pw.chromium.launch()
        if kind in ("coverage", "full") and "coverage" not in done_stages:
            from qc6.workers.coverage import run_coverage
            urls = opts.get("_urls") or await discover_urls(site, opts)
            if not urls:
                raise RuntimeError("No pages found: the sitemap is empty or could not be read.")
            if not opts.get("_urls"):                    # remember the page list so a resume crawls the same pages
                opts["_urls"] = urls
                save_alternates(opts, os.path.join(settings.data_dir, "runs", str(run_id), "sitemap_alternates.json.gz"))
                await pool.execute("UPDATE runs SET options = $2 WHERE id = $1", run_id, opts)
            part = await run_coverage(pool, run_id, site, urls, {**opts, "with_visits": kind == "full"}, control)
            if part.get("paused") or control["pause"]:
                return await paused()
            await stage_done("coverage", {**part, "scope_excluded": int(opts.get("_scope_excluded") or 0)})
        if kind in ("visit", "full") and "visits" not in done_stages:
            from qc6.workers.visit_stage import run_visit_stage
            if kind == "visit":       # visit the pages of the site's latest coverage run
                pages_run = await pool.fetchval("SELECT id FROM runs WHERE site_id = $1 AND kind IN ('coverage','full') "
                                                "AND status = 'completed' AND NOT (options ? 'spot') ORDER BY id DESC LIMIT 1", site["id"])
                if pages_run is None:
                    raise RuntimeError("Run a coverage check first; real visits sample its pages.")
                await pool.execute("INSERT INTO pages (run_id, url, path, template_id, http_status, final_url, title, facts, fetched_at) "
                                   "SELECT $1, url, path, template_id, http_status, final_url, title, facts, fetched_at FROM pages "
                                   "WHERE run_id = $2 ON CONFLICT DO NOTHING", run_id, pages_run)
            part = await run_visit_stage(pool, browser, run_id, site, opts, control)
            if part.get("paused") or control["pause"]:
                return await paused()
            await stage_done("visits", part)
        if kind == "full" and "extras" not in done_stages:
            from qc6.workers import extras
            if any(extras.configured().values()):
                if control["pause"]:
                    return await paused()
                await stage_done("extras", await extras.run_extras(pool, run_id, site))
        if kind in ("journey", "full") and "journeys" not in done_stages and not opts.get("spot"):
            if control["pause"]:
                return await paused()
            part = await journey_stage(pool, browser, run_id, site, fast=fast_journeys or opts.get("fast_journeys", False), opts=opts)
            await stage_done("journeys", part)
        pages_total = await pool.fetchval("SELECT COUNT(*) FROM pages WHERE run_id = $1", run_id) or \
            await pool.fetchval("SELECT COUNT(*) FROM pages WHERE run_id = (SELECT MAX(id) FROM runs WHERE site_id = $1 "
                                "AND kind IN ('coverage','full') AND status = 'completed' AND NOT (options ? 'spot'))", site["id"]) or 0
        sc: Dict[str, Any] = {"score": None}
        if not opts.get("spot"):                     # a spot check doesn't speak for the whole site's health
            sc = await snapshot_score(pool, run_id, site["id"], pages_total)
            summary["score"] = sc.get("score")
        if control["pause"]:
            return await paused()
        await pool.execute("UPDATE runs SET status = 'completed', finished_at = now(), summary = $2 WHERE id = $1 AND status = 'running'",
                           run_id, summary)
        await events.record(pool, run_id, "completed", f"Finished: {summary.get('pages', 0)} pages checked", score=sc.get("score"))
        async with pool.acquire() as conn:
            await notify(conn, run_id, "completed", score=sc.get("score"))
        await after_run_hooks(pool, run_id, site, summary)
    except Exception as e:
        log.error("run %s failed: %s", run_id, traceback.format_exc())
        await pool.execute("UPDATE runs SET status = 'failed', finished_at = now(), error = $2, summary = $3 "
                           "WHERE id = $1 AND status IN ('running', 'pausing', 'queued', 'cancelling')", run_id, f"{type(e).__name__}: {e}"[:500], summary)
        await events.record(pool, run_id, "failed", f"Stopped with an error: {type(e).__name__}: {e}"[:400])
        async with pool.acquire() as conn:
            await notify(conn, run_id, "failed", error=str(e)[:300])
    finally:
        watcher.cancel()
        if own_pw is not None:
            await browser.close()
            await own_pw.stop()
    return summary


async def after_run_hooks(pool, run_id: int, site: Dict[str, Any], summary: Dict[str, Any]) -> None:
    """Things that happen after a completed check (alerts, IndexNow on verified fixes …). Never fail the run."""
    try:
        from qc6 import notify as alerts
        await alerts.after_run(pool, run_id, site, summary)
    except Exception:
        log.warning("after-run hooks failed for run %s: %s", run_id, traceback.format_exc())
    try:                                    # fresh Search Console / GA4 numbers for the opportunity score
        from qc6 import search_console as sc
        if sc.configured(site)["ready"]:
            try:
                await sc.sync(pool, site)
            except Exception as e:
                await sc.record_error(pool, site["id"], str(e))
    except Exception:
        log.warning("search sync after run %s failed: %s", run_id, traceback.format_exc())


async def worker_loop(poll_s: float = 2.0) -> None:
    """Claims queued checks one at a time. Ctrl+C / SIGTERM (docker stop, service stop): the check in hand stops at its
    checkpoint and goes back in the queue, so the next worker carries on where it stopped; then the worker exits."""
    from qc6 import scheduler
    pool = await get_pool()
    log.info("worker %s ready", settings.worker_id)
    last_tick = 0.0
    state: Dict[str, Any] = {"draining": False, "run": None}

    def drain() -> None:
        if state["draining"]:
            raise SystemExit(1)                    # asked twice: stop now; the heartbeat timeout hands the check on
        state["draining"] = True
        asyncio.get_running_loop().create_task(worker_seen(pool, state["run"], "draining"))
        log.warning("stopping: finishing the page in hand, then handing check %s back to the queue", state["run"])
        if state["run"] is not None:
            async def pause_for_shutdown(rid: int) -> None:
                done = await pool.execute("UPDATE runs SET status = 'pausing' WHERE id = $1 AND status = 'running'", rid)
                state["handed_back"] = rid if done != "UPDATE 0" else None
            asyncio.get_running_loop().create_task(pause_for_shutdown(state["run"]))
    import signal
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, drain)
        except (NotImplementedError, RuntimeError, ValueError):   # Windows: Ctrl+C still stops it; recovery hands the check on
            pass
    while not state["draining"]:
        if time.monotonic() - last_tick >= 60:
            last_tick = time.monotonic()
            try:
                await prune(pool)
            except Exception:
                log.warning("pruning failed: %s", traceback.format_exc())
            try:
                for st in await scheduler.tick(pool):
                    log.info("schedule %s started run %s", st["key"], st["run_id"])
            except Exception:
                log.warning("scheduler tick failed: %s", traceback.format_exc())
        try:
            await worker_seen(pool)
            for r in await recover_stale(pool):
                log.warning("run %s lost its worker; now %s", r["id"], r["status"])
        except Exception:
            log.warning("stale-run recovery failed: %s", traceback.format_exc())
        run_id = await pool.fetchval(CLAIM_SQL, settings.worker_id)
        if run_id is None:
            await asyncio.sleep(poll_s)
            continue
        log.info("claimed run %s", run_id)
        state["run"] = run_id
        import socket
        from qc6 import events
        await worker_seen(pool, run_id)
        await events.record(pool, run_id, "worker", f"Picked up by worker {settings.worker_id} on {socket.gethostname()}",
                            worker=settings.worker_id)
        beat = asyncio.create_task(_heartbeat(pool, run_id))
        try:
            await execute_run(pool, run_id)
        finally:
            beat.cancel()
            state["run"] = None
            if state.get("handed_back") == run_id:   # paused by the shutdown, not by a person: queue it for the next worker
                await pool.execute("UPDATE runs SET status = 'queued', worker_id = NULL WHERE id = $1 AND status IN ('paused', 'pausing')", run_id)
    await pool.execute("DELETE FROM workers WHERE id = $1", settings.worker_id)
    log.info("worker %s stopped", settings.worker_id)


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("QC6_LOG", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(worker_loop())
