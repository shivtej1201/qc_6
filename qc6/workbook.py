"""The full report workbook, laid out like the QC V5 audit workbook (Executive Summary, Sitemap Progress, Business /
Language / Page Type summaries, Destination Status, Source Page Issues, Destination Issues, Broken / Redirect /
External URLs, Validation Cache Reuse) plus QC's own Problems sheet with the 'seen before' remarks.

Everything comes from the check's own crawl database (runs/<id>/coverage.db, the same tables V5 reported from) and
from Postgres (problems, page types, score). Written in streaming mode; each sheet stops at Excel's row limit and
says so in its last row. The single-sheet report (qc6/export.py) is still there for a small file.
"""
from __future__ import annotations

import gzip
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from qc6.sqlite_ro import ro_uri
from qc6.settings import settings

EXCEL_MAX_ROWS = 1_048_576
BROKEN_SQL = ("(c.status_bucket IN ('4xx','5xx','Timeout','DNS/Network','SSL','Robots blocked','WAF blocked','Other errors') "
              "OR (c.status_bucket = '3xx' AND (c.http_status IS NULL OR c.http_status >= 400)))")
REL_COLS = ["source_page_url", "source_page_status", "found_url", "normalized_found_url", "found_url_type", "url_syntax_type",
            "found_url_scope", "found_url_status", "found_url_status_bucket", "found_url_final_url", "anchor_text", "rel",
            "hreflang", "occurrence_count", "business", "language", "page_type", "validation_source", "remark", "checked_at"]
REL_SQL = """SELECT r.source_page_url, r.source_page_status, r.found_url, r.normalized_found_url, r.found_url_type, r.url_syntax_type,
                    r.found_url_scope, c.http_status, c.status_bucket, c.final_url, r.anchor_text, r.rel, r.hreflang,
                    r.occurrence_count, r.business, r.language, r.page_type, r.validation_source, c.remark, c.checked_at
             FROM on_page_link_relationships r LEFT JOIN on_page_validation_cache c
               ON c.job_id = r.job_id AND c.normalized_url = r.normalized_found_url
             WHERE r.job_id = ? AND r.found_url_scope IN ('INTERNAL','EXTERNAL')"""
SRC_COLS = ["source_page_id", "source_sitemap_number", "source_sitemap", "source_page_url", "crawl_state", "initial_http_status",
            "http_status", "final_url", "redirect_count", "response_time_ms", "content_type", "business", "language", "page_type",
            "classification_method", "raw_occurrences_count", "relationships_count", "robots_status", "waf_status", "error_type",
            "remark", "checked_at"]
DEST_COLS = ["normalized_url", "url_scope", "validation_state", "initial_http_status", "http_status", "final_url", "redirect_count",
             "redirect_chain", "response_time_ms", "content_type", "status_bucket", "status_classification", "robots_status",
             "waf_status", "error_type", "first_seen_source_url", "reuse_count", "remark", "checked_at"]
BUCKET_ORDER = ["2xx", "3xx", "4xx", "5xx", "Timeout", "DNS/Network", "SSL", "Robots blocked", "WAF blocked", "Other errors", "PENDING"]
GROUP_COLS = ["Source Pages Processed", "Completed", "Failed", "Blocked", "Timed Out", "Relationships Extracted"]


def db_path(run_id: int) -> str:
    return os.path.join(settings.data_dir, "runs", str(run_id), "coverage.db")


def _group_sql(col: str) -> str:
    return f"""SELECT {col} AS k, SUM(crawl_state != 'PENDING'), SUM(crawl_state = 'COMPLETED'), SUM(crawl_state = 'FAILED'),
                      SUM(crawl_state = 'BLOCKED'), SUM(crawl_state = 'TIMED_OUT'), SUM(relationships_count)
               FROM on_page_source_pages WHERE job_id = ? GROUP BY {col} ORDER BY 2 DESC, 1"""


class _Book:
    """openpyxl write-only workbook with bold, filled header rows, frozen header and filters."""

    def __init__(self) -> None:
        import openpyxl
        from openpyxl.styles import Font, PatternFill
        self.wb = openpyxl.Workbook(write_only=True)
        self.bold = Font(bold=True)
        self.title_font = Font(bold=True, size=14)
        self.fill = PatternFill("solid", fgColor="DDF1EF")

    def _cells(self, ws, values: Sequence[Any], font=None, fill=None):
        from openpyxl.cell import WriteOnlyCell
        out = []
        for v in values:
            c = WriteOnlyCell(ws, value=v)
            if font is not None:
                c.font = font
            if fill is not None:
                c.fill = fill
            out.append(c)
        return out

    def table(self, name: str, header: Sequence[str], rows: Iterable[Sequence[Any]], widths: Optional[Sequence[int]] = None,
              note_if_empty: str = "Nothing to report.") -> int:
        ws = self.wb.create_sheet(name[:31])
        for i, w in enumerate(widths or [], start=1):
            ws.column_dimensions[_col(i)].width = w
        ws.freeze_panes = "A2"
        ws.append(self._cells(ws, header, self.bold, self.fill))
        n = 0
        for r in rows:
            if n >= EXCEL_MAX_ROWS - 2:
                ws.append([f"Excel stops at {EXCEL_MAX_ROWS:,} rows; the rest are in the CSV / ZIP download."])
                break
            ws.append([_cell(v) for v in r])
            n += 1
        if n == 0:
            ws.append([note_if_empty])
        elif n < EXCEL_MAX_ROWS - 2:
            ws.auto_filter.ref = f"A1:{_col(len(header))}{n + 1}"
        return n

    def key_values(self, name: str, title: str, head: List[Tuple[str, Any]], metrics: List[Tuple[str, Any]]) -> None:
        ws = self.wb.create_sheet(name[:31])
        ws.column_dimensions["A"].width = 62
        ws.column_dimensions["B"].width = 48
        ws.append(self._cells(ws, [title], self.title_font))
        for k, v in head:
            ws.append([k, _cell(v)])
        ws.append([])
        ws.append(self._cells(ws, ["Metric", "Measured Value"], self.bold, self.fill))
        for k, v in metrics:
            ws.append(self._cells(ws, [k], self.bold) if v is None else [k, _cell(v)])

    def save(self) -> str:
        fd, path = tempfile.mkstemp(suffix=".xlsx")
        os.close(fd)
        self.wb.save(path)
        return path


def _col(i: int) -> str:
    s = ""
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _cell(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, datetime):                        # Excel can't hold time zones: write UTC, readable
        if v.tzinfo is not None:
            v = v.astimezone(timezone.utc).replace(tzinfo=None)
        return v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, str):
        v = v[:32000]                                   # Excel's cell limit is 32,767 characters
        return "'" + v if v[:1] in ("=", "+", "-", "@") else v      # never let a cell run as a formula
    return v


def build(run: Dict[str, Any], site: Dict[str, Any], extra: Dict[str, Any]) -> str:
    """run / site rows; extra = {"problems": [...rows], "problem_header": [...], "templates": {url: name},
    "severity": {sev: (problems, pages)}, "score": float|None}. Returns the path of a temporary .xlsx."""
    path = db_path(run["id"])
    book = _Book()
    job = f"RUN{run['id']}"
    host = site["primary_host"]
    if not os.path.exists(path):                       # imported or visit-only checks have no crawl database
        book.key_values("Executive Summary", f"QC V6 — {host} on-page URL audit", _head(run), _qc_metrics(run, extra))
        _problems_sheet(book, extra)
        return book.save()
    _simple_sheets(book, run, site)
    _tickets_sheet(book, extra)
    c = sqlite3.connect(ro_uri(path), uri=True)
    try:
        q = lambda sql, *a: c.execute(sql, a)                                           # noqa: E731
        one = lambda sql, *a: c.execute(sql, a).fetchone()                              # noqa: E731
        tot, done, ok, failed, blocked, timed = one(
            "SELECT COUNT(*), SUM(crawl_state != 'PENDING'), SUM(crawl_state = 'COMPLETED'), SUM(crawl_state = 'FAILED'), "
            "SUM(crawl_state = 'BLOCKED'), SUM(crawl_state = 'TIMED_OUT') FROM on_page_source_pages WHERE job_id = ?", job)
        done, ok, failed, blocked, timed = (int(x or 0) for x in (done, ok, failed, blocked, timed))
        occ = int(one("SELECT COALESCE(SUM(raw_occurrences_count), 0) FROM on_page_source_pages WHERE job_id = ?", job)[0])
        rels, internal, external, other, new_check, reused = (int(x or 0) for x in one(
            "SELECT COUNT(*), SUM(found_url_scope = 'INTERNAL'), SUM(found_url_scope = 'EXTERNAL'), "
            "SUM(found_url_scope NOT IN ('INTERNAL','EXTERNAL')), SUM(validation_source = 'NEW_CHECK'), "
            "SUM(validation_source = 'REUSED') FROM on_page_link_relationships WHERE job_id = ?", job))
        uniq, validated = (int(x or 0) for x in one(
            "SELECT COUNT(*), SUM(validation_state != 'PENDING') FROM on_page_validation_cache WHERE job_id = ? "
            "AND url_scope IN ('INTERNAL','EXTERNAL')", job))
        broken = int(one(f"SELECT COUNT(*) FROM on_page_link_relationships r LEFT JOIN on_page_validation_cache c "
                         f"ON c.job_id = r.job_id AND c.normalized_url = r.normalized_found_url WHERE r.job_id = ? "
                         f"AND r.found_url_scope IN ('INTERNAL','EXTERNAL') AND {BROKEN_SQL}", job)[0])
        redirects = int(one("SELECT COUNT(*) FROM on_page_link_relationships r JOIN on_page_validation_cache c "
                            "ON c.job_id = r.job_id AND c.normalized_url = r.normalized_found_url WHERE r.job_id = ? "
                            "AND r.found_url_scope IN ('INTERNAL','EXTERNAL') AND COALESCE(c.redirect_count, 0) > 0", job)[0])
        http_rel = new_check + reused
        pct = lambda a, b: round(100.0 * a / b, 2) if b else 0.0                        # noqa: E731
        state = {"completed": "COMPLETED", "running": "IN_PROGRESS (live snapshot — the crawl is still running)",
                 "paused": "PAUSED (snapshot at the pause point)", "cancelled": "CANCELLED (pages checked before the cancel)",
                 "failed": "FAILED (pages checked before the error)"}.get(run["status"], run["status"].upper())
        metrics: List[Tuple[str, Any]] = [
            ("Source pages total (sitemap inventory)", tot), ("Source pages processed", done),
            ("Source pages completion %", pct(done, tot)), ("Source pages completed (HTTP fetched & parsed)", ok),
            ("Source pages timed out", timed), ("Source pages failed", failed), ("Source pages blocked (WAF / Robots)", blocked),
            ("Source pages remaining (PENDING)", tot - done), ("URL occurrences discovered", occ),
            ("URL relationships persisted (source -> destination)", rels), ("Unique HTTP destinations discovered", uniq),
            ("Unique HTTP destinations validated", validated), ("Unique HTTP destinations pending validation", uniq - validated),
            ("New HTTP validations (NEW_CHECK)", new_check), ("Reused validations (REUSED)", reused),
            ("Validation cache reuse rate (%)", pct(reused, http_rel)), ("Internal relationships", internal),
            ("External relationships", external), ("Non-HTTP / Fragment relationships", other),
            ("Broken / Timeout / Error relationships", broken), ("Redirect relationships", redirects),
        ] + _qc_metrics(run, extra)
        book.key_values("Executive Summary", f"QC V6 — {host} on-page URL audit", _head(run, state), metrics)

        sm_rows = []
        for num, sm, assigned, proc, okk, fl, bl, to, rel in q(
                "SELECT source_sitemap_number, source_sitemap, COUNT(*), SUM(crawl_state != 'PENDING'), SUM(crawl_state = 'COMPLETED'), "
                "SUM(crawl_state = 'FAILED'), SUM(crawl_state = 'BLOCKED'), SUM(crawl_state = 'TIMED_OUT'), SUM(relationships_count) "
                "FROM on_page_source_pages WHERE job_id = ? GROUP BY 1, 2 ORDER BY 1", job):
            sm_rows.append([num, sm or "(addresses given directly)", assigned, proc, assigned - proc, okk, fl, bl, to,
                            pct(proc, assigned), rel])
        book.table("Sitemap Progress", ["Sitemap #", "Sitemap URL", "Assigned Unique", "Processed", "Pending", "Completed", "Failed",
                                        "Blocked", "Timed Out", "Completion %", "Relationships Extracted"], sm_rows,
                   [10, 80, 15, 12, 10, 12, 10, 10, 10, 13, 22])
        book.table("Business Summary", ["Business"] + GROUP_COLS, q(_group_sql("business"), job), [22, 22, 12, 10, 10, 10, 22])
        book.table("Language Summary", ["Language"] + GROUP_COLS, q(_group_sql("language"), job), [22, 22, 12, 10, 10, 10, 22])
        book.table("Page Type Summary", ["Page Type"] + GROUP_COLS, _page_types(c, job, extra.get("templates") or {}),
                   [30, 22, 12, 10, 10, 10, 22])
        counts = dict(q("SELECT status_bucket, COUNT(*) FROM on_page_validation_cache WHERE job_id = ? "
                        "AND url_scope IN ('INTERNAL','EXTERNAL') GROUP BY 1", job).fetchall())
        order = BUCKET_ORDER + sorted(k for k in counts if k not in BUCKET_ORDER)
        book.table("Destination Status", ["Status Bucket", "Unique HTTP Destinations", "Share %"],
                   ([b, counts.get(b, 0), round(100.0 * counts.get(b, 0) / uniq, 4) if uniq else 0.0] for b in order
                    if b in counts or b in BUCKET_ORDER[:10]), [18, 26, 10])
        book.table("Status Codes", STATUS_HEAD, _status_codes(c, job), [16, 12, 12, 30, 14, 18, 80],
                   note_if_empty="Nothing checked yet.")
        book.table("Source Page Issues", SRC_COLS, q(
            f"SELECT {', '.join(SRC_COLS)} FROM on_page_source_pages WHERE job_id = ? AND (crawl_state NOT IN ('COMPLETED','PENDING') "
            "OR COALESCE(http_status, 0) >= 400) ORDER BY source_page_id", job),
            [12, 10, 50, 70, 13, 10, 10, 70, 9, 12, 20, 14, 10, 16, 16, 10, 10, 12, 10, 18, 40, 22],
            note_if_empty="Every source page was fetched and parsed.")
        book.table("Destination Issues", DEST_COLS, q(
            f"SELECT {', '.join('c.' + x for x in DEST_COLS)} FROM on_page_validation_cache c WHERE c.job_id = ? "
            f"AND c.url_scope IN ('INTERNAL','EXTERNAL') AND {BROKEN_SQL} ORDER BY c.normalized_url", job),
            [70, 10, 14, 10, 10, 60, 9, 30, 12, 20, 14, 18, 12, 10, 16, 60, 10, 40, 22],
            note_if_empty="No broken, timed-out or blocked destinations.")
        book.table("Broken URLs", REL_COLS, q(f"{REL_SQL} AND {BROKEN_SQL} ORDER BY r.id", job), _rel_widths(),
                   note_if_empty="No link on a checked page points to a broken address.")
        book.table("Redirect URLs", REL_COLS, q(f"{REL_SQL} AND COALESCE(c.redirect_count, 0) > 0 ORDER BY r.id", job), _rel_widths(),
                   note_if_empty="No link on a checked page goes through a redirect.")
        book.table("External URLs (Unique)", DEST_COLS, q(
            f"SELECT {', '.join(DEST_COLS)} FROM on_page_validation_cache WHERE job_id = ? AND url_scope = 'EXTERNAL' "
            "ORDER BY normalized_url", job), [70, 10, 14, 10, 10, 60, 9, 30, 12, 20, 14, 18, 12, 10, 16, 60, 10, 40, 22])
        _unique_sheets(book, c, job)
        book.table("Validation Cache Reuse", ["Metric", "Count"], [
            ["Unique HTTP Destinations", uniq], ["NEW_CHECK Relationships", new_check], ["REUSED Relationships", reused],
            ["Total HTTP Relationships", http_rel], ["Reuse Rate %", pct(reused, http_rel)],
            ["Invariant: NEW_CHECK + REUSED == HTTP Relationships", "PASS" if new_check + reused == internal + external else "CHECK"],
            ["Invariant: NEW_CHECK == Unique HTTP Destinations", "PASS" if new_check == uniq else "CHECK"]], [52, 16])
        alt = _alternates(run, c, job)
        if alt is not None:
            book.table("Sitemap Hreflang", ["page_url", "hreflang", "alternate_url", "alternate_status", "alternate_status_bucket", "remark"],
                       alt, [70, 10, 70, 12, 14, 50], note_if_empty="Every hreflang alternate in the sitemap answers 200.")
    finally:
        c.close()
    _problems_sheet(book, extra)
    return book.save()


def _tickets_sheet(book: "_Book", extra: Dict[str, Any]) -> None:
    """One internal ticket per issue type this check found (QC-<number>); each URL is in that ticket's attachment."""
    if "tickets" not in extra:
        return
    state = {"to_raise": "To raise", "raised": "Raised", "closed": "Closed"}
    book.table("Tickets", ["Ticket", "Issue", "Priority", "Affected", "State", "Raised as", "Attachment (download in QC: Tickets)",
                           "Ticket text (copy into the Ops tool)"],
               ([t["ticket_id"], t["issue"], t["priority"], t["affected"], state.get(t["state"], t["state"]),
                 t["raised_ref"] or "", t["attachment"], t["text"]] for t in extra["tickets"]),
               [11, 40, 14, 26, 10, 14, 44, 120], note_if_empty="No issues found, so no tickets.")


def _simple_sheets(book: "_Book", run: Dict[str, Any], site: Dict[str, Any]) -> None:
    """The first sheets, readable in a minute: Summary, Ops Ticket, then one plain list per kind of problem with every
    address, the pages it is on and what to do. The detailed V5 sheets follow them."""
    from qc6 import ops_ticket
    q = ops_ticket.collect(run["id"])
    if q is None:
        return
    p, bl, rd, bi = q["pages"], q["broken_links"], q["redirects"], q["broken_images"]
    t = ops_ticket.ticket(q, run, site)
    book.key_values("Summary", f"URL QC - {site['primary_host']} - check #{run['id']}", [
        ("Suggested priority", t["priority"] or "Nothing to fix"),
        ("What QC checked", "Every page in the list, and every link and image on those pages (each unique address once). "
                            "CSS and JavaScript files are not checked."),
    ], [
        ("Pages", None), ("Pages checked", p["checked"]), ("Pages answering 200", p["ok"]),
        ("Pages not answering 200  -> sheet Pages Not OK", p["not_ok"]),
        ("Links", None), ("Unique links checked", q["links"]["unique"]),
        ("Broken links (4xx, 5xx, no answer)  -> sheet Broken Links", bl["unique"]), ("  ...pages that have one", bl["pages"]),
        ("Links that redirect  -> sheet Redirects", rd["unique"]), ("  ...pages that have one", rd["pages"]),
        ("Images", None), ("Unique images checked", q["images"]["unique"]),
        ("Broken images  -> sheet Broken Images", bi["unique"]), ("  ...pages that have one", bi["pages"]),
        ("Next", None), ("Raise a ticket", "Copy the Ops Ticket sheet (or the Ops ticket tab in QC) into the ticket."),
        ("Every status code", "Sheet Status Codes; every address with its status: All URLs (Unique), Images (Unique)."),
    ])
    ws = book.wb.create_sheet("Ops Ticket")
    ws.column_dimensions["A"].width = 160
    for line in t["text"].split("\n"):
        ws.append(book._cells(ws, [line], book.bold if line.isupper() or line.startswith("Title:") else None))
    book.table("Broken Links", ["Broken link", "Status", "Link goes to", "Found on (pages)", "Link text", "Example page",
                                "More example pages", "What to do"],
               ([r["url"], r["status"], r["scope"], r["pages"], r["text"], r["example"], r["examples"], bl["fix"]] for r in bl["rows"]),
               [70, 26, 14, 14, 30, 60, 90, 50], note_if_empty="No broken links.")
    book.table("Broken Images", ["Broken image", "Status", "Found on (pages)", "Example page", "More example pages", "What to do"],
               ([r["url"], r["status"], r["pages"], r["example"], r["examples"], bi["fix"]] for r in bi["rows"]),
               [70, 26, 14, 60, 90, 50], note_if_empty="No broken images.")
    book.table("Redirects", ["Link", "Status", "Goes to", "Found on (pages)", "Example page", "What to do"],
               ([r["url"], r["status"], r["final_url"], r["pages"], r["example"], rd["fix"]] for r in rd["rows"]),
               [70, 34, 70, 14, 60, 50], note_if_empty="No link redirects.")
    book.table("Pages Not OK", ["Page", "Status", "Detail", "What to do"],
               ([r["url"], r["status"], r["detail"], p["fix"]] for r in p["rows"]), [80, 30, 50, 50],
               note_if_empty="Every page answers 200.")


STATUS_HEAD = ["What", "First Status", "Final Status", "Meaning", "Unique URLs", "Found On (links / uses)", "Example URL"]
UNIQ_HEAD = ["url", "scope", "first_status", "final_status", "status_bucket", "meaning", "final_url", "redirect_hops",
             "redirect_chain", "response_time_ms", "content_type", "found_on_pages", "occurrences", "first_found_on",
             "example_found_on", "remark", "checked_at"]
IMG_HEAD = ["image_url", "image_type", "internal_external", "first_status", "final_status", "status_bucket", "meaning",
            "final_url", "redirect_hops", "content_type", "size_bytes", "image_issue", "found_on_pages", "occurrences",
            "pages_missing_alt", "first_found_on", "example_found_on", "remark", "checked_at"]
BROKEN_IMG_HEAD = ["source_page_url", "source_page_status", "image_url", "image_type", "internal_external", "first_status",
                   "final_status", "status_bucket", "final_url", "alt_text", "remark"]
# issues first: anything that did not answer 200 straight away, then the healthy ones
_ISSUES_FIRST = "(COALESCE(c.initial_http_status, 0) = 200 AND COALESCE(c.http_status, 0) = 200), c.http_status, c.normalized_url"


def _meaning(code: Any, bucket: Any = None) -> str:
    from http import HTTPStatus
    if code is None:
        return {"Timeout": "No answer in time", "DNS/Network": "Address not found / network error", "SSL": "Certificate problem",
                "Robots blocked": "Not checked: robots.txt disallows it", "WAF blocked": "Blocked by the site's firewall / CDN",
                "PENDING": "Not checked yet"}.get(bucket or "", bucket or "No answer")
    try:
        return HTTPStatus(int(code)).phrase
    except ValueError:
        return "Non-standard status"


def _status_codes(c: sqlite3.Connection, job: str):
    """Every exact status code seen (200, 301, 302, 404, 410, 500...), for the pages checked, the links found on them and
    the images, with how many unique addresses answered it, how many times they are used, and an example."""
    _found_on(c, job)
    parts = [
        ("Pages checked", "SELECT initial_http_status, http_status, NULL, COUNT(*), COUNT(*), MIN(source_page_url) "
                          "FROM on_page_source_pages WHERE job_id = ? AND crawl_state != 'PENDING' GROUP BY 1, 2, 3"),
        ("Links found", "SELECT c.initial_http_status, c.http_status, CASE WHEN c.http_status IS NULL THEN c.status_bucket END, "
                        "COUNT(*), COALESCE(SUM(f.occ), 0), MIN(c.normalized_url) FROM on_page_validation_cache c "
                        "LEFT JOIN temp.qc_found f ON f.u = c.normalized_url WHERE c.job_id = ? "
                        "AND c.url_scope IN ('INTERNAL','EXTERNAL') GROUP BY 1, 2, 3"),
        ("Images", "SELECT c.initial_http_status, c.http_status, CASE WHEN c.http_status IS NULL THEN c.status_bucket END, "
                   "COUNT(*), COALESCE(SUM(f.occ), 0), MIN(c.normalized_url) FROM on_page_image_validation_cache c "
                   "LEFT JOIN temp.qc_img_found f ON f.u = c.normalized_url WHERE c.job_id = ? GROUP BY 1, 2, 3"),
    ]
    for what, sql in parts:
        try:
            rows = c.execute(sql, (job,)).fetchall()
        except sqlite3.OperationalError:           # a check made before images were recorded
            continue
        for first, final, bucket, n, uses, example in sorted(rows, key=lambda r: (r[0] is None, r[0] or 0, r[1] or 0)):
            code = first if first is not None else final
            meaning = _meaning(code, bucket)
            if first is not None and final is not None and first != final:
                meaning += f" -> {_meaning(final)}"
            yield [what, first if first is not None else (bucket or "no answer"), final, meaning, n, uses, example]


def _found_on(c: sqlite3.Connection, job: str) -> None:
    """Per unique address: on how many pages it is found, how often, the first page and up to three example pages.
    Temporary tables of this read-only connection; built once and shared by the sheets."""
    if c.execute("SELECT 1 FROM temp.sqlite_master WHERE name = 'qc_found'").fetchone():
        return
    specs = [("qc_found", "on_page_link_relationships", "normalized_found_url", "AND found_url_scope IN ('INTERNAL','EXTERNAL')", "0"),
             ("qc_img_found", "on_page_image_relationships", "normalized_image_url", "", "SUM(alt_present = 0)")]
    for name, table, col, where, alt in specs:
        try:
            c.execute(f"""CREATE TEMP TABLE {name} AS
                SELECT {col} AS u, COUNT(DISTINCT source_page_url) AS pages, SUM(occurrence_count) AS occ, {alt} AS no_alt,
                       MIN(CASE WHEN rn = 1 THEN source_page_url END) AS first_on,
                       GROUP_CONCAT(CASE WHEN rn <= 3 THEN source_page_url END, ' | ') AS examples
                FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY {col} ORDER BY id) AS rn FROM {table} WHERE job_id = ? {where})
                GROUP BY {col}""", (job,))
            c.execute(f"CREATE INDEX temp.{name}_u ON {name}(u)")
        except sqlite3.OperationalError:
            c.execute(f"CREATE TEMP TABLE IF NOT EXISTS {name} (u TEXT, pages INT, occ INT, no_alt INT, first_on TEXT, examples TEXT)")


def _unique_sheets(book: "_Book", c: sqlite3.Connection, job: str) -> None:
    """Every unique link and image address found on the checked pages, whatever it answered, with where it was found."""
    _found_on(c, job)
    rows = c.execute(f"""SELECT c.normalized_url, c.url_scope, c.initial_http_status, c.http_status, c.status_bucket, NULL, c.final_url,
                 c.redirect_count, c.redirect_chain, c.response_time_ms, c.content_type, COALESCE(f.pages, 0), COALESCE(f.occ, 0),
                 COALESCE(f.first_on, c.first_seen_source_url), f.examples, c.remark, c.checked_at
          FROM on_page_validation_cache c LEFT JOIN temp.qc_found f ON f.u = c.normalized_url
          WHERE c.job_id = ? AND c.url_scope IN ('INTERNAL','EXTERNAL') ORDER BY {_ISSUES_FIRST}""", (job,))
    book.table("All URLs (Unique)", UNIQ_HEAD, (_with_meaning(r, 2, 3, 4, 5) for r in rows),
               [70, 10, 11, 11, 13, 26, 60, 9, 40, 12, 20, 12, 11, 60, 90, 40, 20],
               note_if_empty="No links were found on the checked pages.")
    try:
        rows = c.execute(f"""SELECT c.normalized_url, c.image_type, c.internal_external, c.initial_http_status, c.http_status, c.status_bucket,
                 NULL, c.final_url, c.redirect_count, c.content_type, c.content_length, c.image_issue, COALESCE(f.pages, 0),
                 COALESCE(f.occ, 0), COALESCE(f.no_alt, 0), COALESCE(f.first_on, c.first_seen_source_url), f.examples, c.remark, c.checked_at
              FROM on_page_image_validation_cache c LEFT JOIN temp.qc_img_found f ON f.u = c.normalized_url
              WHERE c.job_id = ? ORDER BY {_ISSUES_FIRST}""", (job,)).fetchall()
        broken = c.execute(f"""SELECT r.source_page_url, r.source_page_status, r.image_url, r.image_type, r.internal_external,
                 c.initial_http_status, c.http_status, c.status_bucket, c.final_url, r.alt_text, c.remark
              FROM on_page_image_relationships r JOIN on_page_image_validation_cache c
                ON c.job_id = r.job_id AND c.normalized_url = r.normalized_image_url
              WHERE r.job_id = ? AND ({BROKEN_SQL} OR COALESCE(c.redirect_count, 0) > 0) ORDER BY r.id""", (job,))
    except sqlite3.OperationalError:
        return
    book.table("Images (Unique)", IMG_HEAD, (_with_meaning(r, 3, 4, 5, 6) for r in rows),
               [70, 10, 12, 11, 11, 13, 26, 60, 9, 18, 11, 22, 12, 11, 14, 60, 90, 40, 20],
               note_if_empty="No images were found on the checked pages.")
    book.table("Image Issues", BROKEN_IMG_HEAD, broken, [70, 11, 70, 10, 12, 11, 11, 13, 60, 30, 40],
               note_if_empty="Every image on the checked pages answers 200 directly.")


def _with_meaning(r: Sequence[Any], first: int, final: int, bucket: int, at: int) -> List[Any]:
    r = list(r)
    code = r[first] if r[first] is not None else r[final]
    r[at] = _meaning(code, r[bucket])
    if r[first] is not None and r[final] is not None and r[first] != r[final]:
        r[at] += f" -> {_meaning(r[final])}"
    return r


def _head(run: Dict[str, Any], state: Optional[str] = None) -> List[Tuple[str, Any]]:
    return [("Snapshot Timestamp (UTC)", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
            ("Check ID", f"#{run['id']}"), ("Crawl State", state or run["status"].upper()),
            ("Started (UTC)", run["started_at"].strftime("%Y-%m-%d %H:%M") if run.get("started_at") else ""),
            ("Finished (UTC)", run["finished_at"].strftime("%Y-%m-%d %H:%M") if run.get("finished_at") else "")]


def _qc_metrics(run: Dict[str, Any], extra: Dict[str, Any]) -> List[Tuple[str, Any]]:
    out: List[Tuple[str, Any]] = [("QC findings", None)]
    if extra.get("score") is not None:
        out.append(("Health score (0-100)", round(float(extra["score"]), 1)))
    sev = extra.get("severity") or {}
    for s in ("critical", "high", "medium", "low"):
        n, p = sev.get(s, (0, 0))
        out.append((f"{s.capitalize()} problems (pages affected)", f"{n} ({p} pages)"))
    return out


def _page_types(c: sqlite3.Connection, job: str, templates: Dict[str, str]):
    """The site's own page type label when it has one (data-pageType), else QC's detected page type."""
    agg: Dict[str, List[int]] = {}
    for url, pt, state, rel in c.execute("SELECT source_page_url, page_type, crawl_state, relationships_count "
                                         "FROM on_page_source_pages WHERE job_id = ?", (job,)):
        name = pt if pt and pt != "NEEDS_REVIEW" else (templates.get(url) or "NEEDS_REVIEW")
        a = agg.setdefault(name, [0, 0, 0, 0, 0, 0])
        a[0] += state != "PENDING"
        a[1] += state == "COMPLETED"
        a[2] += state == "FAILED"
        a[3] += state == "BLOCKED"
        a[4] += state == "TIMED_OUT"
        a[5] += int(rel or 0)
    return sorted(([k] + v for k, v in agg.items()), key=lambda r: (-r[1], r[0]))


def _rel_widths() -> List[int]:
    return [70, 10, 70, 70, 12, 12, 10, 10, 12, 60, 30, 12, 10, 10, 14, 10, 16, 14, 40, 22]


def _alternates(run: Dict[str, Any], c: sqlite3.Connection, job: str):
    """Sitemap <xhtml:link rel=alternate hreflang> pairs whose alternate address doesn't answer 200 in this check."""
    path = (run.get("options") or {}).get("_alternates_file")
    if not path or not os.path.exists(path):
        return None
    with gzip.open(path, "rt", encoding="utf-8") as f:
        pairs = json.load(f)
    status: Dict[str, Tuple[Any, Any]] = {}
    for u, s, b in c.execute("SELECT normalized_url, http_status, status_bucket FROM on_page_validation_cache WHERE job_id = ?", (job,)):
        status[u] = (s, b)
    for u, s in c.execute("SELECT normalized_source_url, http_status FROM on_page_source_pages WHERE job_id = ? AND http_status IS NOT NULL", (job,)):
        status.setdefault(u, (s, f"{str(s)[0]}xx" if s else ""))

    def rows():
        for page, lang, href in pairs:
            s, b = status.get(href, (None, None))
            if s == 200:
                continue
            if s is None:
                remark = "Alternate not checked in this crawl (not linked from checked pages and not in the list)"
            elif 300 <= int(s) < 400:
                remark = "Alternate redirects: hreflang should point to the final address"
            else:
                remark = "Alternate is broken: search engines ignore this hreflang pair"
            yield [page, lang, href, s, b, remark]
    return rows()


def _problems_sheet(book: _Book, extra: Dict[str, Any]) -> None:
    if extra.get("problems") is not None:
        book.table("QC Problems", extra["problem_header"], extra["problems"], [12, 10, 50, 10, 16, 16, 20, 60, 60, 50],
                   note_if_empty="No problems found.")
    if extra.get("page_rows") is not None:
        book.table("QC Problem Pages", extra["page_header"], extra["page_rows"](), [70, 10, 22, 12, 10, 50, 40, 60],
                   note_if_empty="No page has a problem.")
