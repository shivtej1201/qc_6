"""Existing-results check across checks: a link that answered 200 (no redirect) in this site's previous check, within
QC6_REUSE_HOURS (default 24), isn't fetched again; its result is reused.

Only clean successes are reused, so a link that was broken, redirecting, slow or unreachable is always checked again
(a fix is seen at once). Pages of the new check are never reused: they're crawled, that's the point of the check, and
a link to a page of this check takes that page's fresh answer. Images aren't reused. Turn it off per check (Start
crawling -> "check every link again") or for all with QC6_REUSE_HOURS=0.

The reused rows go into the new check's own crawl database as "seeded" results (referenced = 0 until a page links to
them, seeded_by = 'PREVIOUS_CHECK'), the same mechanism the engine already uses for page results, so the reports and
counts only include links the new check actually found.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any, Dict, Optional

from qc6.sqlite_ro import ro_uri
from qc6.settings import settings

log = logging.getLogger("qc6.reuse")
COLS = ("normalized_url", "url_scope", "validation_state", "initial_http_status", "http_status", "final_url", "redirect_count",
        "redirect_chain", "response_time_ms", "content_type", "status_bucket", "status_classification", "remark", "error_type",
        "error_message", "robots_status", "waf_status", "first_seen_source_url", "checked_at")


def hours() -> float:
    try:
        return max(0.0, float(os.environ.get("QC6_REUSE_HOURS", "24")))
    except ValueError:
        return 24.0


async def previous_run(pool, site_id: int, run_id: int, window_h: float) -> Optional[int]:
    return await pool.fetchval(
        """SELECT id FROM runs WHERE site_id = $1 AND id < $2 AND status = 'completed' AND kind IN ('coverage', 'full')
           AND finished_at > now() - make_interval(secs => $3) ORDER BY finished_at DESC LIMIT 1""",
        site_id, run_id, window_h * 3600.0)


def seed(db_path: str, job: str, prev_run: int, window_h: float) -> int:
    """Copy the previous check's fresh 200 link results into this check's crawl database. Idempotent; skipped when
    this check already has link results (a resumed check)."""
    prev = os.path.join(settings.data_dir, "runs", str(int(prev_run)), "coverage.db")
    if not os.path.exists(prev):
        return 0
    c = sqlite3.connect(db_path, timeout=30)
    try:
        have = c.execute("SELECT COUNT(*) FROM on_page_validation_cache WHERE job_id = ? AND COALESCE(seeded_by, '') != 'SOURCE_FETCH'",
                         (job,)).fetchone()[0]
        if have:
            return 0
        c.execute("ATTACH DATABASE ? AS prev", (prev,))
        cols = ", ".join(COLS)
        cur = c.execute(f"""
            INSERT OR IGNORE INTO on_page_validation_cache (job_id, {cols}, reuse_count, referenced, seeded_by)
            SELECT ?, {", ".join("p." + x for x in COLS)}, 0, 0, 'PREVIOUS_CHECK'
              FROM prev.on_page_validation_cache p
             WHERE p.job_id = ? AND p.referenced = 1 AND p.validation_state != 'PENDING'
               AND p.http_status BETWEEN 200 AND 299 AND COALESCE(p.initial_http_status, p.http_status) BETWEEN 200 AND 299
               AND COALESCE(p.redirect_count, 0) = 0 AND COALESCE(p.error_type, '') = ''
               AND julianday(p.checked_at) > julianday('now') - ? / 24.0
               AND p.normalized_url NOT IN (SELECT normalized_source_url FROM main.on_page_source_pages WHERE job_id = ?)""",
                        (job, f"RUN{int(prev_run)}", window_h, job))
        n = cur.rowcount or 0
        c.commit()
        c.execute("DETACH DATABASE prev")
        return int(n)
    finally:
        c.close()


def reused_count(db_path: str, job: str) -> int:
    """How many links of the finished check took an earlier check's result (were linked to, not fetched)."""
    try:
        c = sqlite3.connect(ro_uri(db_path), uri=True, timeout=10)
        try:
            return int(c.execute("SELECT COUNT(*) FROM on_page_validation_cache WHERE job_id = ? AND referenced = 1 "
                                 "AND seeded_by = 'PREVIOUS_CHECK'", (job,)).fetchone()[0])
        finally:
            c.close()
    except sqlite3.Error:
        return 0
