"""Coverage crawl: the V5 engine (proven at 300K pages / 3 h 22 min), wrapped for V6 (blueprint §7).

* identifies honestly: V5's request headers + `User-Agent: … QC-Audit/6.0` + `X-QC-Audit: <site token>`
* fingerprints every parsed page for template clustering (no second fetch)
* runs V5's report builder (18-file ZIP kept for compatibility), then imports pages, templates and issues into
  Postgres as V6 findings -> fixes.
"""
from __future__ import annotations

import asyncio
import time
import json
import logging
import os
import sqlite3
import threading
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from qcv5.audit.on_page_issue_rules import ACTIONS
from qcv5.crawler.on_page_audit_engine import OnPageAuditEngine
from qcv5.reports.on_page_csv_packager import build_on_page_csv_package

from qc6.domain import templates as T
from qc6.linkgraph import text_simhash
from qc6.product import extract as product_extract
from qc6.settings import settings
from qc6.workers.analysis import Item, apply, close_run, notify, snapshot_score, spot_pages

log = logging.getLogger("qc6.coverage")

BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"


class FingerprintEngine(OnPageAuditEngine):
    """V5 engine + a SimHash of each parsed page's layout, computed in the existing parse pass."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fingerprints: Dict[str, int] = {}
        self.text_fps: Dict[str, int] = {}            # SimHash of the main text, for near-duplicate pages
        self.products: Dict[str, Any] = {}            # product structured data per page (qc6.product)
        self._fp_lock = threading.Lock()

    def _parse_and_classify(self, src_url, final_url, html, seo_ctx=None):
        out = super()._parse_and_classify(src_url, final_url, html, seo_ctx)
        if html:
            fp = T.simhash(html)
            tfp = text_simhash(html)
            prod = product_extract(html)
            with self._fp_lock:
                self.fingerprints[src_url] = fp
                if tfp is not None:
                    self.text_fps[src_url] = tfp
                self.products[src_url] = prod
        return out

    def save_fingerprints(self, path: str) -> None:
        """Checkpoint both fingerprint maps (atomic replace), so a paused or crashed check keeps its page types."""
        import json as _json
        with self._fp_lock:
            data = {"layout": {k: str(v) for k, v in self.fingerprints.items()},
                    "text": {k: str(v) for k, v in self.text_fps.items()}, "product": dict(self.products)}
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(data, f)
        os.replace(tmp, path)

    def load_fingerprints(self, path: str) -> None:
        import json as _json
        with open(path, encoding="utf-8") as f:
            data = _json.load(f)
        if "layout" not in data:                       # older checkpoints: a flat url -> layout map
            data = {"layout": data, "text": {}}
        self.fingerprints.update({k: int(v) for k, v in data["layout"].items()})
        self.text_fps.update({k: int(v) for k, v in data.get("text", {}).items()})
        self.products.update(data.get("product", {}))


# V5 rule code -> V6 rule (+ how to build subject/fields). Anything else becomes PAGE_CHECK / LINK_CHECK with V5's
# own recommended action as the fix text, so nothing V5 finds is lost.
LINK_BROKEN = {"NOT_FOUND", "GONE", "CLIENT_ERROR", "BROKEN_REDIRECT", "REDIRECT_TO_BROKEN", "SERVER_ERROR",
               "DNS_FAILURE", "CONNECTION_FAILED", "SSL_ERROR", "REDIRECT_LOOP", "TIMEOUT"}
DIRECT = {"H1_MISSING": "H1_MISSING", "MISSING_TITLE": "TITLE_MISSING", "META_DESCRIPTION_MISSING": "DESCRIPTION_MISSING",
          "NOINDEX_IN_SITEMAP": "NOINDEX_IN_SITEMAP", "SITEMAP_URL_NOT_CANONICAL": "CANONICAL_ELSEWHERE",
          "SLOW_RESPONSE": "SLOW_SERVER", "MIXED_CONTENT": "MIXED_CONTENT", "MIXED_CONTENT_IMAGE": "MIXED_CONTENT",
          "SOURCE_NOT_FOUND": "PAGE_NOT_FOUND", "SOURCE_SERVER_ERROR": "PAGE_SERVER_ERROR",
          "DUPLICATE_TITLE": "TITLE_DUPLICATE", "SOURCE_WAF_BLOCKED": "ACCESS_CHALLENGED"}
SKIP = {"CLASSIFICATION_NEEDS_REVIEW"}       # internal bookkeeping, not a site problem


def human(code: str) -> str:
    return code.replace("_", " ").capitalize()


def map_issue(r: sqlite3.Row, template_id: Optional[int], host: str) -> Optional[Item]:
    code, el = r["rule_code"], (r["element_type"] or "").upper()
    page = r["page_url"]
    target = r["subject_url"] or r["found_url"] or ""
    if code in SKIP:
        return None
    ev = {"v5_rule": code, "detail": (r["detail"] or "")[:300], "status": r["http_status"]}
    if code in LINK_BROKEN and r["category"] != "SOURCE_PAGE":
        rule = "BROKEN_IMAGE" if "IMAGE" in el or r["category"] in ("IMAGE", "BROKEN_IMAGE") else "BROKEN_LINK"
        return Item(rule, page, "coverage", template_id, target, {"value": r["http_status"] or code}, ev)
    if code in ("REDIRECTED", "REDIRECT_CHAIN", "IMAGE_REDIRECTED"):
        return Item("REDIRECTED_LINK", page, "coverage", template_id, target, {"value": r["final_url"] or "the final address"}, ev)
    if code in DIRECT:
        rule = DIRECT[code]
        subject = {"TITLE_DUPLICATE": (r["anchor_or_alt_text"] or target)[:200], "ACCESS_CHALLENGED": host}.get(rule, "")
        value = {"PAGE_NOT_FOUND": r["http_status"] or 404, "PAGE_SERVER_ERROR": r["http_status"] or 500,
                 "CANONICAL_ELSEWHERE": target, "SLOW_SERVER": (r["detail"] or "over 3 s")[:40]}.get(rule, "")
        return Item(rule, page, "coverage", template_id if rule not in ("TITLE_DUPLICATE",) else None, subject, {"value": value}, ev)
    action = ACTIONS.get(code) or (r["detail"] or human(code))
    if r["category"] in ("LINK", "INTERNAL", "EXTERNAL", "IMAGE", "BROKEN_LINK", "REDIRECT") and target:
        return Item("LINK_CHECK", page, "coverage", template_id, f"{code}|{target}", {"value": human(code), "subject": action}, ev)
    return Item("PAGE_CHECK", page, "coverage", template_id, code, {"value": human(code).lower(), "subject": action}, ev)


def identity_headers(site: Dict[str, Any]) -> Dict[str, str]:
    """How QC introduces itself on every request, to every host: a real browser UA plus 'QC-Audit/6.0'.
    The site's allow-token and staging login are private: see qc6.site_access.httpx_hook."""
    return {"User-Agent": f"{BROWSER_UA} {settings.ua_suffix}"}


async def run_coverage(pool, run_id: int, site: Dict[str, Any], urls: List[str], opts: Dict[str, Any],
                       control: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    run_dir = os.path.join(settings.data_dir, "runs", str(run_id))
    os.makedirs(run_dir, exist_ok=True)
    db_path = os.path.join(run_dir, "coverage.db")
    job = f"RUN{run_id}"
    host = site["primary_host"]
    from qc6.presets import engine_options
    eo = engine_options(site.get("settings") or {}, opts)          # V5 presets + per-site V5 settings
    share = (1.0 - settings.visit_share) if opts.get("with_visits", True) else 1.0   # visitors get the rest of the budget
    coverage_rate, coverage_max = max(0.5, eo["target_rps"] * share), max(0.5, eo["max_rps"] * share)
    eng = FingerprintEngine(
        db_path=db_path, job_id=job, job_name=f"Run {run_id}", allow_local=settings.allow_private_targets,
        primary_domain=host, target_rps=coverage_rate, min_rps=max(0.5, coverage_rate / 3), max_rps=coverage_max,
        source_concurrency=eo["source_concurrency"], dest_concurrency=eo["dest_concurrency"],
        image_concurrency=eo["image_concurrency"], audit_images=opts.get("images", True),
        read_timeout=eo.get("read_timeout"), max_retries=eo.get("max_retries"),
        audit_seo=True, probe_url=f"{site['base_url'].rstrip('/')}/robots.txt")
    eng.crawl_delay = float(opts.get("_crawl_delay") or 0.0)   # robots.txt Crawl-delay: at least this long between requests
    eng.extra_headers = identity_headers(site)
    from qc6.site_access import httpx_hook
    eng.site_request_hook = httpx_hook(site)
    sms, idx = opts.get("_sitemaps") or [], opts.get("_sm_idx") or []
    if len(idx) == len(urls) and sms:             # each page's own sitemap (number = its place in the list, from 1)
        entries = [{"url": u, "source_sitemap": sms[i], "source_sitemap_number": i + 1} for u, i in zip(urls, idx)]
    else:
        entries = [{"url": u, "source_sitemap": "", "source_sitemap_number": 1} for u in urls]
    eng.initialize_inventory(entries)                 # idempotent: on resume, finished pages stay finished
    fp_path = os.path.join(run_dir, "fingerprints.json")
    if os.path.exists(fp_path):                       # fingerprints of pages parsed before a pause or a crash
        eng.load_fingerprints(fp_path)
    if control is not None:
        control["engine"] = eng
    from qc6 import html_store
    store = html_store.HtmlStore(run_dir) if html_store.enabled() else None
    eng.html_sink = store

    last = {"done": None, "errors": 0, "ms": 0.0}

    async def record_minute(c_done: int, c_err: int, c_ms: float) -> None:
        """Per-minute figures (CrawlPulse job orchestration §4.5): pages, errors and response time in each minute."""
        if last["done"] is None:                      # first tick (or a resume): start counting from here
            last.update(done=c_done, errors=c_err, ms=c_ms)
            return
        dp, de, dm = c_done - last["done"], c_err - last["errors"], c_ms - last["ms"]
        last.update(done=c_done, errors=c_err, ms=c_ms)
        if dp <= 0:
            return
        await pool.execute("""INSERT INTO run_minutes (run_id, minute, pages, errors, latency_ms_sum)
                              VALUES ($1, date_trunc('minute', now()), $2, $3, $4)
                              ON CONFLICT (run_id, minute) DO UPDATE SET pages = run_minutes.pages + EXCLUDED.pages,
                                errors = run_minutes.errors + EXCLUDED.errors,
                                latency_ms_sum = run_minutes.latency_ms_sum + EXCLUDED.latency_ms_sum""",
                           run_id, dp, max(0, de), max(0.0, dm))

    async def progress_loop():
        tick = 0
        while True:
            await asyncio.sleep(2.0)
            tick += 1
            if tick % 30 == 0:                        # every minute: a crash loses at most a minute of fingerprints
                try:
                    await asyncio.to_thread(eng.save_fingerprints, fp_path)
                except OSError:
                    log.warning("could not checkpoint fingerprints for run %s", run_id)
            try:
                c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
                done, errs, ms = c.execute(
                    "SELECT COUNT(*), COALESCE(SUM(CASE WHEN COALESCE(error_type, '') != '' OR COALESCE(http_status, 0) >= 400 "
                    "THEN 1 ELSE 0 END), 0), COALESCE(SUM(response_time_ms), 0) FROM on_page_source_pages "
                    "WHERE job_id = ? AND crawl_state != 'PENDING'", (job,)).fetchone()
                c.close()
            except sqlite3.Error:
                continue
            try:
                await record_minute(int(done), int(errs), float(ms))
            except Exception:
                log.warning("per-minute figures for run %s not saved", run_id)
            prog = {"stage": "coverage", "pages_done": done, "pages_total": len(urls)}
            async with pool.acquire() as conn:
                await conn.execute("UPDATE runs SET progress = progress || $2::jsonb WHERE id = $1", run_id, prog)
                await notify(conn, run_id, "progress", **prog)

    ticker = asyncio.create_task(progress_loop())
    try:
        stats = await eng.run()
    finally:
        ticker.cancel()
        eng.close()
        if store is not None:
            store.close()
        if control is not None:
            control["engine"] = None
    if eng._stop_requested:                           # paused: keep the checkpoint, build nothing yet
        eng.save_fingerprints(fp_path)
        return {"paused": True, "pages_done": int(stats.get("source_pages_processed") or 0)}
    async with pool.acquire() as conn:                # every page is read; grouping problems and building the report next
        prog = {"stage": "analysing", "pages_done": len(urls), "pages_total": len(urls)}
        await conn.execute("UPDATE runs SET progress = progress || $2::jsonb WHERE id = $1", run_id, prog)
        await notify(conn, run_id, "progress", **prog)
    steps = _Steps()
    pkg = await asyncio.to_thread(build_on_page_csv_package, db_path, job, os.path.join(run_dir, "reports"),
                                  package_name="QC_V6_COVERAGE", project_id=host)
    steps.done("report_package")
    from qc6 import facts
    fact_counts = await asyncio.to_thread(facts.export, db_path, job, run_id)
    steps.done("link_facts")
    out = await import_coverage(pool, run_id, site, db_path, job, eng.fingerprints, stats, pkg, text_fps=eng.text_fps,
                                products=eng.products, steps=steps)
    out["facts"] = fact_counts
    out["timings"] = steps.out
    return out


class _Steps:
    """Seconds each post-crawl step took: printed to the worker log and kept in the run summary (timings)."""
    def __init__(self):
        self.t, self.out = time.perf_counter(), {}

    def done(self, name: str) -> None:
        now = time.perf_counter()
        self.out[name] = round(now - self.t, 2)
        self.t = now
        print(f"[QC6] step {name}: {self.out[name]:.2f} s", flush=True)


async def import_coverage(pool, run_id: int, site: Dict[str, Any], db_path: str, job: str,
                          fingerprints: Dict[str, int], stats: Dict[str, Any], pkg: Dict[str, Any],
                          text_fps: Optional[Dict[str, int]] = None, products: Optional[Dict[str, Any]] = None,
                          steps: Optional["_Steps"] = None) -> Dict[str, Any]:
    steps = steps or _Steps()
    spot = await spot_pages(pool, run_id)
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    pages = c.execute("""SELECT s.source_page_url AS url, s.http_status, s.final_url, s.page_title, s.page_type, s.business,
                                p.h1_count, p.title_length, p.meta_description_length, p.word_count, p.internal_links,
                                p.external_links, p.noindex, p.canonical_url, p.schema_types, p.js_rendering_suspected
                           FROM on_page_source_pages s LEFT JOIN on_page_seo_pages p
                             ON p.job_id = s.job_id AND p.source_page_url = s.source_page_url
                          WHERE s.job_id = ?""", (job,)).fetchall() if _has_col(c, "on_page_seo_pages", "canonical_url") else \
        c.execute("""SELECT s.source_page_url AS url, s.http_status, s.final_url, s.page_title, s.page_type, s.business,
                            p.h1_count, p.title_length,
                            p.meta_description_length, p.word_count, p.internal_links, p.external_links, p.noindex,
                            '' AS canonical_url, p.schema_types, p.js_rendering_suspected
                       FROM on_page_source_pages s LEFT JOIN on_page_seo_pages p
                         ON p.job_id = s.job_id AND p.source_page_url = s.source_page_url
                      WHERE s.job_id = ?""", (job,)).fetchall()

    # templates
    idx = T.TemplateIndex(radius=10)
    page_tpl: Dict[str, int] = {}
    for r in pages:
        fp = fingerprints.get(r["url"])
        if fp is not None:
            page_tpl[r["url"]] = idx.assign(fp, urllib.parse.urlsplit(r["url"]).path)
    # the site's own page-type labels (V5 reads data-pageType, e.g. "b2c-pdp" -> "B2C PDP") name a template when
    # most of its pages agree; otherwise the name comes from the URL pattern
    from collections import Counter
    from qc6.presets import page_type_label
    site_labels: Dict[int, Counter] = {}
    for r in pages:
        t = page_tpl.get(r["url"])
        lab = page_type_label(r["page_type"], r["business"])
        if t is not None:
            site_labels.setdefault(t, Counter())[lab] += 1
    def tpl_name(t: Dict[str, Any]) -> str:
        cnt = site_labels.get(t["id"])
        if cnt:
            lab, n = cnt.most_common(1)[0]
            if lab and n >= 0.6 * sum(cnt.values()):
                return f"{lab} pages"
        return t["name"]
    db_tpl: Dict[int, int] = {}
    names: Dict[int, str] = {}
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Templates keep their identity across runs (fix keys depend on it): a cluster joins the site's existing
            # template with the nearest layout fingerprint (within the clustering radius) and the same URL pattern
            # family; only genuinely new layouts get a new template.
            existing = [(r["id"], T.from_signed(r["fingerprint"]), r["path_pattern"])
                        for r in await conn.fetch("SELECT id, fingerprint, path_pattern FROM templates WHERE site_id = $1", site["id"])]
            taken: set = set()
            for t in idx.summary():
                fp = idx.centroids[t["id"]]
                cands = [(T.distance(fp, efp), eid) for eid, efp, pat in existing
                         if eid not in taken and T.distance(fp, efp) <= idx.radius]
                same_pattern = [c for c in cands if next(p for i, _, p in existing if i == c[1]) == t["pattern"]]
                pick = min(same_pattern or cands, default=None)
                if pick is not None:
                    tid = pick[1]
                    if spot is None:                  # a spot check matches page types but doesn't redefine them
                        await conn.execute("UPDATE templates SET name = $2, path_pattern = $3, page_count = $4 WHERE id = $1",
                                           tid, tpl_name(t), t["pattern"], t["pages"])
                else:
                    tid = await conn.fetchval(
                        "INSERT INTO templates (site_id, fingerprint, name, path_pattern, page_count) VALUES ($1,$2,$3,$4,$5) RETURNING id",
                        site["id"], T.to_signed(fp), tpl_name(t), t["pattern"], t["pages"])
                taken.add(tid)
                db_tpl[t["id"]] = tid
                names[tid] = tpl_name(t)
            await conn.executemany(
                """INSERT INTO pages (run_id, url, path, template_id, http_status, final_url, title, facts, fetched_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8, now()) ON CONFLICT (run_id, url) DO NOTHING""",
                [(run_id, r["url"], urllib.parse.urlsplit(r["url"]).path or "/",
                  db_tpl.get(page_tpl.get(r["url"], -1)), r["http_status"], r["final_url"], r["page_title"],
                  {**{k: r[k] for k in ("h1_count", "title_length", "meta_description_length", "word_count", "internal_links",
                                        "external_links", "noindex", "canonical_url", "schema_types", "js_rendering_suspected")},
                   **({"site_page_type": page_type_label(r["page_type"], r["business"])}
                      if page_type_label(r["page_type"], r["business"]) else {})})
                 for r in pages])

    # issues -> findings -> fixes
    issues = c.execute("SELECT * FROM on_page_issues WHERE job_id = ?", (job,)).fetchall()
    c.close()
    items = []
    for r in issues:
        tpl = db_tpl.get(page_tpl.get(r["page_url"], -1))
        it = map_issue(r, tpl, site["primary_host"])
        if it is not None:
            items.append(it)
    if products:                                       # product pages: is the product's data complete?
        from qc6.presets import page_type_label
        from qc6.product import problems as product_problems
        prows = [{"url": r["url"], "http_status": r["http_status"], "template_id": db_tpl.get(page_tpl.get(r["url"], -1)),
                  "product": products.get(r["url"]), "site_type": page_type_label(r["page_type"], r["business"])} for r in pages]
        for url, rule, fields in product_problems(prows):
            items.append(Item(rule, url, "coverage", db_tpl.get(page_tpl.get(url, -1)), "", fields, {"product": products.get(url)}))
        await pool.execute("""UPDATE pages p SET facts = p.facts || jsonb_build_object('product', x.prod::jsonb)
                              FROM unnest($2::text[], $3::text[]) AS x(url, prod) WHERE p.run_id = $1 AND p.url = x.url""",
                           run_id, [u for u, v in products.items() if v], [json.dumps(v) for v in products.values() if v])
    steps.done("pages_and_problems")
    graph = await _link_intelligence(pool, run_id, site, pages, page_tpl, db_tpl, text_fps or {}, items, spot=spot is not None)
    steps.done("link_intelligence")
    applied = await apply(pool, run_id, site["id"], items, template_names=names, spot=spot is not None)
    steps.done("fixes")
    complete = int(stats.get("source_pages_pending") or 0) == 0
    moved = await close_run(pool, run_id, site["id"], ["coverage"], complete=complete, pages=spot)
    # a spot check doesn't speak for the whole site: no health score from it
    sc = await snapshot_score(pool, run_id, site["id"], len(pages)) if spot is None else {"score": None}
    steps.done("lifecycle_and_score")
    summary = {"pages": len(pages), "templates": len(db_tpl), "issues_v5": len(issues), **applied, "lifecycle": moved,
               "link_graph": graph,
               "score": sc.get("score"), "trust_gate": pkg.get("final_qc_status"), "zip": pkg.get("zip_path"),
               "links": stats.get("url_relationships_persisted"), "images": stats.get("image_relationships_persisted")}
    return summary


async def _link_intelligence(pool, run_id: int, site: Dict[str, Any], pages, page_tpl: Dict[str, int], db_tpl: Dict[int, int],
                             text_fps: Dict[str, int], items: List[Item], spot: bool = False) -> Dict[str, Any]:
    """Click depth, link strength and inlinks into each page's facts; deep / unreachable pages, canonical targets and
    near-duplicates become fixes like any other finding. Uses the links already exported to Parquet."""
    from qc6 import facts, linkgraph
    rows = [{"url": r["url"], "final_url": r["final_url"], "http_status": r["http_status"],
             "canonical_url": r["canonical_url"] or "", "noindex": bool(r["noindex"])} for r in pages]
    links = os.path.join(facts.facts_dir(run_id), "links.parquet")
    try:
        res = await asyncio.to_thread(linkgraph.analyse, rows, site["base_url"], links, text_fps)
    except Exception:                                  # never lose a finished check over the extra analysis
        log.warning("link analysis failed for run %s", run_id, exc_info=True)
        return {"error": "link analysis failed; see the worker log"}
    m = res["metrics"]
    if spot:                                           # a few pages are not the site's link graph
        for v in m.values():
            v["depth"] = v["inlinks"] = v["authority"] = None
        res["problems"] = [p for p in res["problems"] if p[1] not in ("DEEP_PAGE", "NOT_REACHABLE_FROM_HOME")]
    urls = list(m)
    await pool.execute("""UPDATE pages p SET facts = p.facts || jsonb_strip_nulls(jsonb_build_object(
                              'depth', x.depth, 'inlinks', x.inlinks, 'authority', x.authority, 'canonical_url', x.canonical))
                          FROM unnest($2::text[], $3::int[], $4::int[], $5::float8[], $6::text[]) AS x(url, depth, inlinks, authority, canonical)
                          WHERE p.run_id = $1 AND p.url = x.url""",
                       run_id, urls, [m[u]["depth"] for u in urls], [m[u]["inlinks"] for u in urls], [m[u]["authority"] for u in urls],
                       [m[u]["canonical_url"] for u in urls])
    for url, rule, subject, fields in res["problems"]:
        items.append(Item(rule, url, "coverage", db_tpl.get(page_tpl.get(url, -1)), subject, fields,
                          {"metrics": m.get(url, {}), **({"target": subject} if subject else {})}))
    return {k: res[k] for k in ("home_found", "edges", "near_duplicate_groups")} | {"problems": len(res["problems"])}


def _has_col(c: sqlite3.Connection, table: str, col: str) -> bool:
    return any(r[1] == col for r in c.execute(f"PRAGMA table_info({table})"))
