"""Bring QC V5 audits into V6 (blueprint §15.1): V5 runs become V6 checks, V5 issues become V6 fixes.

    python -m qc6.import_v5 list   path/to/qc_v5/audit_cache.db
    python -m qc6.import_v5 import path/to/qc_v5/audit_cache.db --site 1 [--job onpage_64a55489fab9 ...] [--v5-data path/to/qc_v5/data/onpage]

V5 and V6 crawl with the same engine and write the same SQLite tables, so an imported audit goes through exactly the
import path a V6 page check uses: same fix grouping, same plain-language texts, same score. Jobs are imported oldest
first, so the fix lifecycle (found -> confirmed fixed -> back again) builds up as it would have in V6.

What is different, and said on the check: V5 kept no page HTML, so pages are grouped into page types by URL pattern
instead of by layout; links and images are copied into the explorer as they were. A job that was already imported is
skipped (re-running is safe). The V5 database is opened read-only.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import sys
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from qc6.domain import templates as T


def _ro(db: str) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{os.path.abspath(db)}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def list_jobs(db: str) -> List[Dict[str, Any]]:
    c = _ro(db)
    try:
        rows = c.execute("""SELECT job_id, name, status, source_pages_total, source_pages_pending, url_relationships_persisted,
                                   created_at, updated_at FROM on_page_audit_jobs ORDER BY created_at, job_id""").fetchall()
        issues = {r[0]: r[1] for r in c.execute("SELECT job_id, count(*) FROM on_page_issues GROUP BY job_id")}
    finally:
        c.close()
    return [{**dict(r), "issues": issues.get(r["job_id"], 0)} for r in rows]


def url_fingerprints(db: str, job: str) -> Dict[str, int]:
    """Without HTML, a page's 'layout' is its URL pattern: same pattern -> same fingerprint, others far apart."""
    c = _ro(db)
    try:
        urls = [r[0] for r in c.execute("SELECT source_page_url FROM on_page_source_pages WHERE job_id = ?", (job,))]
    finally:
        c.close()
    out = {}
    for u in urls:
        pat = T.path_pattern(urllib.parse.urlsplit(u).path or "/")
        out[u] = int.from_bytes(hashlib.blake2b(pat.encode(), digest_size=8).digest(), "big")
    return out


def _ts(v: Optional[str]) -> Optional[datetime]:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _v5_result(v5_data: Optional[str], job: str) -> Dict[str, Any]:
    if not v5_data:
        return {}
    out: Dict[str, Any] = {}
    for name in ("result.json", "config.json"):
        p = os.path.join(v5_data, job, name)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                out[name] = json.load(f)
    return out


async def import_job(pool, db: str, job: Dict[str, Any], site: Dict[str, Any], v5_data: Optional[str] = None) -> Dict[str, Any]:
    from qc6 import facts
    from qc6.workers.coverage import import_coverage
    done = await pool.fetchval("SELECT id FROM runs WHERE site_id = $1 AND options->>'v5_job' = $2", site["id"], job["job_id"])
    if done:
        return {"job": job["job_id"], "skipped": f"already imported as check #{done}"}
    extra = _v5_result(v5_data, job["job_id"])
    trust = (extra.get("result.json") or {}).get("final_qc_status")
    opts = {"v5_job": job["job_id"], "v5_name": job["name"], "imported_from": "QC V5",
            "v5_preset": (extra.get("config.json") or {}).get("preset"), "template_basis": "url_pattern"}
    started, finished = _ts(job["created_at"]), _ts(job["updated_at"]) or _ts(job["created_at"])
    run_id = await pool.fetchval(
        """INSERT INTO runs (site_id, kind, status, options, started_at, created_at) VALUES ($1, 'coverage', 'running', $2, $3, $3)
           RETURNING id""", site["id"], opts, started or datetime.now(timezone.utc))
    try:
        stats = {"source_pages_pending": job["source_pages_pending"] or 0,
                 "url_relationships_persisted": job["url_relationships_persisted"]}
        fact_counts = await asyncio.to_thread(facts.export, db, job["job_id"], run_id)   # links first: the link analysis reads them
        summary = await import_coverage(pool, run_id, site, db, job["job_id"], url_fingerprints(db, job["job_id"]), stats,
                                        {"final_qc_status": trust})
        summary["facts"] = fact_counts
        summary.update({"imported_from": "QC V5", "v5_job": job["job_id"], "template_basis": "url_pattern",
                        "trust_gate": trust or "NOT VERIFIED (V5 result.json not given)"})
        await pool.execute("UPDATE runs SET status = 'completed', summary = $2, finished_at = $3 WHERE id = $1",
                           run_id, summary, finished or datetime.now(timezone.utc))
    except Exception as e:
        await pool.execute("UPDATE runs SET status = 'failed', error = $2, finished_at = now() WHERE id = $1",
                           run_id, f"V5 import failed: {type(e).__name__}: {e}"[:500])
        raise
    return {"job": job["job_id"], "run_id": run_id, "pages": summary.get("pages"), "fixes": summary.get("fixes_new"),
            "issues_v5": summary.get("issues_v5"), "trust_gate": summary["trust_gate"]}


async def import_all(pool, db: str, site_id: int, jobs: Optional[List[str]] = None, v5_data: Optional[str] = None) -> List[Dict[str, Any]]:
    site = await pool.fetchrow("SELECT * FROM sites WHERE id = $1", site_id)
    if site is None:
        raise SystemExit(f"There is no site {site_id}. See the site numbers with: python -m qc6.admin list-sites")
    site = dict(site)
    picked = [j for j in list_jobs(db) if j["status"] == "COMPLETED" and (not jobs or j["job_id"] in jobs)]
    missing = set(jobs or []) - {j["job_id"] for j in picked}
    if missing:
        raise SystemExit(f"Not found or not completed in {db}: {', '.join(sorted(missing))}")
    out = []
    for j in picked:                      # oldest first, so the fix lifecycle builds up in order
        out.append(await import_job(pool, db, j, site, v5_data))
    return out


async def _main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m qc6.import_v5", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list", help="show the audits in a V5 database")
    ls.add_argument("db")
    im = sub.add_parser("import", help="import completed V5 audits into a V6 site")
    im.add_argument("db")
    im.add_argument("--site", type=int, required=True)
    im.add_argument("--job", action="append", help="only this V5 job (repeatable); default: every completed job")
    im.add_argument("--v5-data", help="V5 data/onpage folder, to carry over each audit's trust gate and preset")
    a = ap.parse_args(argv)
    if a.cmd == "list":
        for j in list_jobs(a.db):
            print(f"{j['job_id']:<24} {j['status']:<10} {j['source_pages_total'] or 0:>8,} pages {j['issues']:>8,} issues  "
                  f"{(j['created_at'] or '')[:16]}  {j['name'] or ''}")
        return 0
    from qc6.db.migrate import migrate
    from qc6.db.pool import close_pool, get_pool
    await migrate()
    pool = await get_pool()
    try:
        for r in await import_all(pool, a.db, a.site, a.job, a.v5_data):
            if r.get("skipped"):
                print(f"{r['job']}: skipped, {r['skipped']}")
            else:
                print(f"{r['job']}: check #{r['run_id']}, {r['pages']:,} pages, {r['issues_v5']:,} V5 issues -> "
                      f"{r['fixes']} new fixes, trust gate {r['trust_gate']}")
    finally:
        await close_pool()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
