"""Plain URL QC results for one check, and a ready-to-paste ticket for the Ops team.

Everything comes from the check's crawl database (runs/<id>/coverage.db): the pages checked, every unique link and
image found on them (each checked once) and the pages each one is found on. Used by the result screen, the
"Ops ticket" tab and the first sheets of the Excel report.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from qc6.workbook import BROKEN_SQL, _meaning, db_path

LIST_TOP = 50          # rows per list in the ticket text; the Excel sheets have every row

FIX = {"link": "Restore the page, or update / remove the link on the pages listed.",
       "redirect": "Change the link to point straight at the final address.",
       "image": "Re-upload the image or correct its address on the pages listed.",
       "page": "Make the page answer 200 again, or take it out of the sitemap."}


def _status(first: Any, final: Any, bucket: Any) -> str:
    if first is None and final is None:
        return _meaning(None, bucket)
    code = first if first is not None else final
    s = f"{code} {_meaning(code, bucket)}"
    if first is not None and final is not None and first != final:
        s += f" -> {final} {_meaning(final)}"
    return s


def collect(run_id: int, limit: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Counts and lists for the check, or None when it has no crawl database (imported / visit-only checks)."""
    path = db_path(run_id)
    if not os.path.exists(path):
        return None
    job = f"RUN{run_id}"
    lim = f"LIMIT {int(limit)}" if limit else ""
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        _issue_found_on(c, job)
        one = lambda sql: c.execute(sql, (job,)).fetchone()                                    # noqa: E731
        total, done, ok, redirected = (int(x or 0) for x in one(
            "SELECT COUNT(*), SUM(crawl_state != 'PENDING'), "
            "SUM(crawl_state = 'COMPLETED' AND COALESCE(http_status, 0) = 200 AND COALESCE(initial_http_status, 200) = 200), "
            "SUM(crawl_state = 'COMPLETED' AND COALESCE(initial_http_status, 0) BETWEEN 300 AND 399 AND COALESCE(http_status, 0) = 200) "
            "FROM on_page_source_pages WHERE job_id = ?"))
        bad_pages = [{"url": u, "status": _status(a, b, None if b else (st or "").title()), "detail": r or e or ""}
                     for u, a, b, st, r, e in c.execute(
            f"SELECT source_page_url, initial_http_status, http_status, crawl_state, remark, error_type FROM on_page_source_pages "
            f"WHERE job_id = ? AND crawl_state != 'PENDING' AND NOT (crawl_state = 'COMPLETED' AND COALESCE(http_status, 0) = 200 "
            f"AND COALESCE(initial_http_status, 200) = 200) ORDER BY source_page_id {lim}", (job,))]
        n_bad_pages = done - ok

        def links(where: str, kind: str) -> Dict[str, Any]:
            n = one(f"SELECT COUNT(*) FROM on_page_validation_cache c WHERE c.job_id = ? AND c.url_scope IN ('INTERNAL','EXTERNAL') AND {where}")[0]
            pages = one(f"SELECT COUNT(DISTINCT r.source_page_url) FROM on_page_validation_cache c "
                        f"JOIN on_page_link_relationships r INDEXED BY idx_oplr_dest ON r.job_id = c.job_id AND r.normalized_found_url = c.normalized_url "
                        f"WHERE c.job_id = ? AND c.url_scope IN ('INTERNAL','EXTERNAL') AND {where}")[0]
            rows = c.execute(f"""SELECT c.normalized_url, c.url_scope, c.initial_http_status, c.http_status, c.status_bucket, c.final_url,
                    COALESCE(f.pages, 0), COALESCE(f.first_on, c.first_seen_source_url), f.examples,
                    (SELECT r.anchor_text FROM on_page_link_relationships r INDEXED BY idx_oplr_dest WHERE r.job_id = c.job_id
                       AND r.normalized_found_url = c.normalized_url AND r.anchor_text != '' LIMIT 1)
                 FROM on_page_validation_cache c LEFT JOIN temp.qt_found f ON f.u = c.normalized_url
                 WHERE c.job_id = ? AND c.url_scope IN ('INTERNAL','EXTERNAL') AND {where}
                 ORDER BY COALESCE(f.pages, 0) DESC, c.normalized_url {lim}""", (job,)).fetchall()
            return {"unique": int(n or 0), "pages": int(pages or 0), "fix": FIX[kind], "rows": [
                {"url": u, "scope": "on this site" if sc == "INTERNAL" else "other site", "status": _status(a, b, bk),
                 "final_url": fu if fu and fu != u else "", "pages": p, "example": ex1 or "", "examples": exs or "", "text": t or ""}
                for u, sc, a, b, bk, fu, p, ex1, exs, t in rows]}

        broken = links(BROKEN_SQL, "link")
        redirects = links(f"COALESCE(c.redirect_count, 0) > 0 AND NOT {BROKEN_SQL}", "redirect")
        try:
            n_img = one(f"SELECT COUNT(*) FROM on_page_image_validation_cache c WHERE c.job_id = ? AND {BROKEN_SQL}")[0]
            img_pages = one(f"SELECT COUNT(DISTINCT r.source_page_url) FROM on_page_image_validation_cache c "
                            f"JOIN on_page_image_relationships r INDEXED BY idx_opir_img ON r.job_id = c.job_id AND r.normalized_image_url = c.normalized_url "
                            f"WHERE c.job_id = ? AND {BROKEN_SQL}")[0]
            all_img = int(one("SELECT COUNT(*) FROM on_page_image_validation_cache WHERE job_id = ?")[0] or 0)
            img_rows = c.execute(f"""SELECT c.normalized_url, c.initial_http_status, c.http_status, c.status_bucket,
                    COALESCE(f.pages, 0), COALESCE(f.first_on, c.first_seen_source_url), f.examples
                 FROM on_page_image_validation_cache c LEFT JOIN temp.qt_img_found f ON f.u = c.normalized_url
                 WHERE c.job_id = ? AND {BROKEN_SQL} ORDER BY COALESCE(f.pages, 0) DESC, c.normalized_url {lim}""", (job,)).fetchall()
        except sqlite3.OperationalError as e:
            if "no such table" not in str(e):
                raise
            n_img, img_pages, all_img, img_rows = 0, 0, 0, []
        images = {"unique": int(n_img or 0), "pages": int(img_pages or 0), "fix": FIX["image"], "rows": [
            {"url": u, "status": _status(a, b, bk), "pages": p, "example": ex1 or "", "examples": exs or ""}
            for u, a, b, bk, p, ex1, exs in img_rows]}
        all_links = int(one("SELECT COUNT(*) FROM on_page_validation_cache WHERE job_id = ? AND url_scope IN ('INTERNAL','EXTERNAL')")[0] or 0)
    finally:
        c.close()
    return {"pages": {"total": total, "checked": done, "ok": ok, "redirected": redirected, "not_ok": n_bad_pages,
                      "rows": bad_pages, "fix": FIX["page"]},
            "links": {"unique": all_links}, "images": {"unique": all_img},
            "broken_links": broken, "redirects": redirects, "broken_images": images}


def _issue_found_on(c: sqlite3.Connection, job: str) -> None:
    """Pages each problem address (broken or redirecting link, broken image) is found on. Only problem addresses,
    through the destination index, so it stays quick on a 3 lakh page check."""
    specs = [("qt_found", "on_page_validation_cache", "on_page_link_relationships r INDEXED BY idx_oplr_dest", "normalized_found_url",
              f"c.url_scope IN ('INTERNAL','EXTERNAL') AND ({BROKEN_SQL} OR COALESCE(c.redirect_count, 0) > 0)"),
             ("qt_img_found", "on_page_image_validation_cache", "on_page_image_relationships r INDEXED BY idx_opir_img", "normalized_image_url",
              f"({BROKEN_SQL} OR COALESCE(c.redirect_count, 0) > 0)")]
    for name, cache, rel, col, where in specs:
        try:
            c.execute(f"""CREATE TEMP TABLE IF NOT EXISTS {name} AS
                SELECT c.normalized_url AS u,
                       (SELECT COUNT(DISTINCT r.source_page_url) FROM {rel} WHERE r.job_id = c.job_id AND r.{col} = c.normalized_url) AS pages,
                       (SELECT r.source_page_url FROM {rel} WHERE r.job_id = c.job_id AND r.{col} = c.normalized_url ORDER BY r.id LIMIT 1) AS first_on,
                       (SELECT GROUP_CONCAT(s, ' | ') FROM (SELECT r.source_page_url AS s FROM {rel} WHERE r.job_id = c.job_id
                          AND r.{col} = c.normalized_url ORDER BY r.id LIMIT 3)) AS examples
                FROM {cache} c WHERE c.job_id = ? AND {where}""", (job,))
            c.execute(f"CREATE INDEX IF NOT EXISTS temp.{name}_u ON {name}(u)")
        except sqlite3.OperationalError as e:        # a check made before images were recorded: no image tables
            if "no such" not in str(e):
                raise
            c.execute(f"CREATE TEMP TABLE IF NOT EXISTS {name} (u TEXT, pages INT, first_on TEXT, examples TEXT)")


def priority(q: Dict[str, Any]) -> str:
    pages = max(1, q["pages"]["checked"])
    hard = any(("5" == (r["status"][:1])) or "No answer" in r["status"] for r in q["broken_links"]["rows"][:LIST_TOP])
    if q["pages"]["not_ok"] / pages >= 0.01 or hard:
        return "P1 - High"
    if q["broken_links"]["unique"] or q["broken_images"]["unique"] or q["pages"]["not_ok"]:
        return "P2 - Medium"
    if q["redirects"]["unique"] or q["pages"]["redirected"]:
        return "P3 - Low"
    return ""


def _when(ts: Optional[datetime], tz: str) -> str:
    if not ts:
        return "-"
    try:
        from zoneinfo import ZoneInfo
        ts = ts.astimezone(ZoneInfo(tz))
    except Exception:
        ts = ts.astimezone(timezone.utc)
    return ts.strftime("%d %b %Y %H:%M ") + (ts.tzname() or "")


def ticket(q: Dict[str, Any], run: Dict[str, Any], site: Dict[str, Any], source: str = "") -> Dict[str, str]:
    """A ticket an Ops engineer can act on without opening QC: title, priority, what, where, how to see it, what to do."""
    host = site["primary_host"]
    tz = (site.get("settings") or {}).get("timezone") or "Asia/Kolkata"
    p, bl, rd, bi = q["pages"], q["broken_links"], q["redirects"], q["broken_images"]
    bits = [f"{bl['unique']:,} broken link{'s' if bl['unique'] != 1 else ''}" if bl["unique"] else "",
            f"{bi['unique']:,} broken image{'s' if bi['unique'] != 1 else ''}" if bi["unique"] else "",
            f"{p['not_ok']:,} page{'s' if p['not_ok'] != 1 else ''} not answering 200" if p["not_ok"] else "",
            f"{rd['unique']:,} redirecting link{'s' if rd['unique'] != 1 else ''}" if rd["unique"] else ""]
    bits = [b for b in bits if b]
    pr = priority(q)
    title = (f"[Website QC] {host}: " + ", ".join(bits) if bits else f"[Website QC] {host}: no broken links or images found")
    title += f" (QC check #{run['id']})"
    out: List[str] = [f"Title: {title}", "",
                      f"Priority (suggested): {pr or 'None - nothing to fix'}",
                      "Category: Website - broken links / redirects / images",
                      "Raised from: QC V6 automated URL check",
                      f"Website: {host}",
                      f"QC check: #{run['id']}, {_when(run.get('started_at'), tz)} to {_when(run.get('finished_at'), tz)}",
                      f"Pages checked: {p['checked']:,}" + (f" (from {source})" if source else ""), "",
                      "SUMMARY",
                      f"QC opened {p['checked']:,} pages of {host} and checked every link ({q['links']['unique']:,} unique addresses) "
                      f"and every image ({q['images']['unique']:,} unique) on them, each once. CSS and JavaScript files are not checked.",
                      f"- Pages answering 200: {p['ok']:,} of {p['checked']:,}",
                      f"- Pages not answering 200: {p['not_ok']:,}",
                      f"- Broken links (4xx, 5xx, no answer): {bl['unique']:,} unique addresses, on {bl['pages']:,} pages",
                      f"- Links that redirect: {rd['unique']:,} unique addresses, on {rd['pages']:,} pages",
                      f"- Broken images: {bi['unique']:,} unique images, on {bi['pages']:,} pages", ""]

    def section(name: str, block: Dict[str, Any], line) -> None:
        if not block["rows"]:
            return
        n = block.get("unique", len(block["rows"]))
        shown = block["rows"][:LIST_TOP]
        out.append(f"{name.upper()} ({len(shown):,} of {n:,}, most-used first)" if n > len(shown) else f"{name.upper()} ({n:,})")
        for i, r in enumerate(shown, 1):
            out.append(f"{i}. {line(r)}")
        out.append(f"Fix: {block['fix']}")
        out.append("")

    section("Pages not answering 200", {"rows": p["rows"], "unique": p["not_ok"], "fix": p["fix"]},
            lambda r: f"{r['url']} - {r['status']}" + (f" ({r['detail']})" if r["detail"] else ""))
    section("Broken links", bl, lambda r: f"{r['url']} - {r['status']} - on {r['pages']:,} page{'s' if r['pages'] != 1 else ''}"
            + (f', link text "{r["text"]}"' if r["text"] else "") + (f" - e.g. {r['example']}" if r["example"] else ""))
    section("Broken images", bi, lambda r: f"{r['url']} - {r['status']} - on {r['pages']:,} page{'s' if r['pages'] != 1 else ''}"
            + (f" - e.g. {r['example']}" if r["example"] else ""))
    section("Links that redirect", rd, lambda r: f"{r['url']} - {r['status']}" + (f" - goes to {r['final_url']}" if r["final_url"] else "")
            + f" - on {r['pages']:,} page{'s' if r['pages'] != 1 else ''}" + (f" - e.g. {r['example']}" if r["example"] else ""))
    ex = next((r for r in bl["rows"] + bi["rows"] if r.get("example")), None)
    if ex:
        what = "image" if ex in bi["rows"] else "link"
        out += ["STEPS TO REPRODUCE", f"1. Open {ex['example']}",
                f"2. Find the {what} {ex['url']}" + (f' (link text "{ex["text"]}")' if ex.get("text") else ""),
                f"3. Open it: it answers {ex['status']}", ""]
    out += ["EXPECTED", "Every page, link and image answers 200 directly (no error, no redirect).", "",
            "ACTUAL", "As listed above." if bits else "Nothing to fix: every page, link and image answers 200.", "",
            "ATTACHMENT", f"QC report qc-audit-workbook-{host}-{run['id']}.xlsx - sheets Broken Links, Broken Images, Redirects and "
            "Pages Not OK list every address with every page it is on."]
    return {"title": title, "priority": pr, "text": "\n".join(out)}
