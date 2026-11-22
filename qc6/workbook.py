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
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
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
