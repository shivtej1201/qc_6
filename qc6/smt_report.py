"""Management report: one compact Excel file to present to senior management (SMT) and hand to the teams.

Sheets
  Dashboard          KPI tiles (with change since the previous check), key findings, four native charts, next steps
  Pivot Summary      A status reconciliation, B status codes, C page type, D business, E language,
                     F page type x status matrix, G top 20 failed destinations, H colour legend
  Non-200 Responses  every unique link that is not a plain 200: status, impact, sample page, page type, business,
                     redirect target, response time, reason (clickable)
  Tickets            one ticket per issue type, with priority and the recommended action
  Problem Links      page -> link pairs for the non-200 links only (capped; all pairs are in the ticket attachments
                     and the CSV ZIP)
  Images Not OK / Pages Not OK   when there are any

Small on purpose: shared strings (each URL stored once), healthy links summarised rather than listed, hyperlinks only
where people click, no per-cell styling on long sheets (conditional formats instead). Built with XlsxWriter.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from qc6.workbook import db_path

PROBLEM_LINKS_MAX = 50_000          # page -> link rows written; the attachments / ZIP have every pair
HYPERLINKS_MAX = 5_000              # clickable cells per sheet (Excel allows 65,530; each one adds to the file)

CATEGORY_SQL = """CASE
  WHEN c.status_bucket = 'Timeout' THEN 'Timeout'
  WHEN c.status_bucket = 'DNS/Network' THEN 'DNS / Network error'
  WHEN c.status_bucket = 'SSL' THEN 'SSL error'
  WHEN c.status_bucket IN ('Robots blocked', 'WAF blocked') THEN 'Blocked'
  WHEN c.http_status BETWEEN 500 AND 599 THEN 'Server error (5xx)'
  WHEN c.http_status BETWEEN 400 AND 499 THEN 'Client error (4xx)'
  WHEN c.http_status BETWEEN 300 AND 399 OR (COALESCE(c.redirect_count, 0) > 0 AND c.http_status BETWEEN 200 AND 299) THEN 'Redirect (3xx)'
  WHEN c.http_status BETWEEN 200 AND 299 THEN 'OK (2xx)'
  WHEN c.status_bucket = 'PENDING' THEN 'Not checked'
  ELSE 'Other error' END"""
CATEGORIES = ["OK (2xx)", "Redirect (3xx)", "Client error (4xx)", "Server error (5xx)", "Timeout", "DNS / Network error",
              "SSL error", "Blocked", "Other error", "Not checked"]
BROKEN = {"Client error (4xx)", "Server error (5xx)", "Timeout", "DNS / Network error", "SSL error", "Other error"}
CAT_COLOR = {"OK (2xx)": "#09644F", "Redirect (3xx)": "#C98A00", "Client error (4xx)": "#C2410C", "Server error (5xx)": "#B42318",
             "Timeout": "#7A3E9D", "DNS / Network error": "#5A4B42", "SSL error": "#8E5B3A", "Blocked": "#8A8A8A",
             "Other error": "#6B6B6B", "Not checked": "#BDBDBD"}
CAT_FILL = {"OK (2xx)": "#E2EFDA", "Redirect (3xx)": "#FFF2CC", "Client error (4xx)": "#FCE4D6", "Server error (5xx)": "#FADBD8",
            "Timeout": "#FADBD8", "DNS / Network error": "#FADBD8", "SSL error": "#FADBD8", "Blocked": "#EAECEE",
            "Other error": "#EAECEE", "Not checked": "#F2F2F2"}
LEGEND = [("OK (2xx)", "Reachable and answered 200 directly.", "No action."),
          ("Redirect (3xx)", "The link goes through a redirect before the final page.", "Point the link straight at the final address."),
          ("Client error (4xx)", "Broken: 404 Not Found, 410 Gone, 403 Forbidden...", "Restore the page, or fix / remove the link."),
          ("Server error (5xx)", "The server failed (500, 502, 503, 504).", "Check server and CDN logs; fix the backend."),
          ("Timeout / DNS / SSL", "No answer in time, unknown host, or certificate problem.", "Check hosting, DNS and certificates."),
          ("Blocked", "Not checked: robots.txt or the site's firewall refused QC.", "Allow QC (CDN allow rule) or accept the gap.")]
STATUS_TEXT = {200: "OK", 301: "Moved Permanently", 302: "Found", 303: "See Other", 307: "Temporary Redirect",
               308: "Permanent Redirect", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
               405: "Method Not Allowed", 408: "Request Timeout", 410: "Gone", 429: "Too Many Requests",
               500: "Internal Server Error", 502: "Bad Gateway", 503: "Service Unavailable", 504: "Gateway Timeout"}

BRAND, INK, MUTED, SAND, LINE = "#0B4D3E", "#25201E", "#5A4B42", "#F4EBE2", "#D9CBBF"


# ------------------------------------------------------------------ data
def _conn(run_id: int) -> Optional[sqlite3.Connection]:
    p = db_path(run_id)
    if not os.path.exists(p):
        return None
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    c.execute("PRAGMA temp_store = MEMORY")
    return c


def _prepare(c: sqlite3.Connection, job: str, templates: Dict[str, str]) -> None:
    """Temporary tables of this read-only connection: each destination with its category, and each source page's type
    (the crawl's own classification, else QC's page type, else 'Unclassified')."""
    c.execute(f"""CREATE TEMP TABLE d AS SELECT c.normalized_url AS u, {CATEGORY_SQL} AS cat, c.initial_http_status AS first,
                  c.http_status AS final, c.final_url, c.redirect_count, c.response_time_ms, c.error_type, c.remark, c.url_scope
                  FROM on_page_validation_cache c WHERE c.job_id = ? AND c.referenced = 1
                  AND c.url_scope IN ('INTERNAL', 'EXTERNAL')""", (job,))
    c.execute("CREATE UNIQUE INDEX temp.d_u ON d(u)")
    c.execute("CREATE TEMP TABLE tpl (url TEXT PRIMARY KEY, name TEXT)")
    if templates:
        c.executemany("INSERT OR IGNORE INTO temp.tpl VALUES (?, ?)", templates.items())


PT = "COALESCE(NULLIF(NULLIF(r.page_type, 'NEEDS_REVIEW'), ''), t.name, 'Unclassified')"
BU = "COALESCE(NULLIF(NULLIF(r.business, 'NEEDS_REVIEW'), ''), 'Unclassified')"
LANG = "COALESCE(NULLIF(UPPER(r.language), ''), 'Unknown')"
REL_JOIN = """FROM on_page_link_relationships r INDEXED BY idx_oplr_dest JOIN temp.d d ON d.u = r.normalized_found_url
              LEFT JOIN temp.tpl t ON t.url = r.source_page_url
              WHERE r.job_id = ? AND r.found_url_scope IN ('INTERNAL', 'EXTERNAL')"""


def kpis(c: sqlite3.Connection, job: str) -> Dict[str, int]:
    one = lambda sql: c.execute(sql, (job,)).fetchone()                                            # noqa: E731
    pages, pages_ok = one("SELECT COUNT(*), SUM(crawl_state = 'COMPLETED' AND COALESCE(http_status, 0) = 200 "
                          "AND COALESCE(initial_http_status, 200) = 200) FROM on_page_source_pages WHERE job_id = ? AND crawl_state != 'PENDING'")
    cats = dict(c.execute("SELECT cat, COUNT(*) FROM temp.d GROUP BY cat").fetchall()) if _has_d(c) else {}
    rels = one("SELECT COUNT(*) FROM on_page_link_relationships WHERE job_id = ? AND found_url_scope IN ('INTERNAL', 'EXTERNAL')")[0]
    try:
        img_total, img_bad = one("""SELECT COUNT(*), SUM(c.status_bucket IN ('4xx','5xx','Timeout','DNS/Network','SSL','Other errors')
                                    OR (c.status_bucket = '3xx' AND (c.http_status IS NULL OR c.http_status >= 400)))
                                    FROM on_page_image_validation_cache c WHERE c.job_id = ?""")
    except sqlite3.OperationalError:
        img_total, img_bad = 0, 0
    links = sum(cats.values())
    return {"pages": int(pages or 0), "pages_not_ok": int((pages or 0) - (pages_ok or 0)), "links": links, "relationships": int(rels or 0),
            "broken": sum(v for k, v in cats.items() if k in BROKEN), "redirects": int(cats.get("Redirect (3xx)", 0)),
            "blocked": int(cats.get("Blocked", 0)), "ok": int(cats.get("OK (2xx)", 0)),
            "images": int(img_total or 0), "images_broken": int(img_bad or 0)}


def _has_d(c) -> bool:
    return bool(c.execute("SELECT 1 FROM temp.sqlite_master WHERE name = 'd'").fetchone())


def _kpis_of_run(run_id: Optional[int]) -> Optional[Dict[str, int]]:
    if not run_id:
        return None
    c = _conn(run_id)
    if c is None:
        return None
    try:
        _prepare(c, f"RUN{run_id}", {})
        return kpis(c, f"RUN{run_id}")
    except sqlite3.Error:
        return None
    finally:
        c.close()


def collect(run_id: int, templates: Dict[str, str]) -> Optional[Dict[str, Any]]:
    c = _conn(run_id)
    if c is None:
        return None
    job = f"RUN{run_id}"
    try:
        _prepare(c, job, templates)
        out: Dict[str, Any] = {"kpi": kpis(c, job)}
        # A status reconciliation: unique destinations and link occurrences per category
        uniq = dict(c.execute("SELECT cat, COUNT(*) FROM temp.d GROUP BY cat").fetchall())
        occ = dict(c.execute(f"SELECT d.cat, COUNT(*) {REL_JOIN} GROUP BY d.cat", (job,)).fetchall())
        out["status"] = [(k, int(uniq.get(k, 0)), int(occ.get(k, 0))) for k in CATEGORIES if uniq.get(k) or k in CATEGORIES[:5]]
        # B exact status codes (first answer -> final answer)
        codes = c.execute(f"""SELECT d.first, d.final, d.cat, COUNT(DISTINCT d.u), COUNT(r.id) {REL_JOIN.replace('JOIN temp.d d', 'JOIN temp.d d')}
                              GROUP BY d.first, d.final, d.cat ORDER BY COUNT(r.id) DESC""", (job,)).fetchall()
        out["codes"] = codes
        # C/D/E breakdowns
        def by(dim: str):
            return c.execute(f"""SELECT {dim} AS k, COUNT(DISTINCT r.normalized_found_url),
                                   COUNT(DISTINCT CASE WHEN d.cat != 'OK (2xx)' THEN r.normalized_found_url END),
                                   COUNT(*), SUM(d.cat != 'OK (2xx)')
                                 {REL_JOIN} GROUP BY k ORDER BY SUM(d.cat != 'OK (2xx)') DESC, COUNT(*) DESC""", (job,)).fetchall()
        out["page_type"], out["business"], out["language"] = by(PT), by(BU), by(LANG)
        # F matrix page type x category (link occurrences)
        m = c.execute(f"SELECT {PT} AS k, d.cat, COUNT(*) {REL_JOIN} GROUP BY k, d.cat", (job,)).fetchall()
        matrix: Dict[str, Dict[str, int]] = {}
        for k, cat, n in m:
            matrix.setdefault(k, {})[cat] = n
        out["matrix"] = matrix
        # Non-200 destinations with impact, sample page, its type and business
        out["non200"] = c.execute(f"""
            SELECT d.u, d.first, d.final, d.cat, COUNT(*) AS rels, COUNT(DISTINCT r.source_page_url) AS pages,
                   MIN(r.source_page_url), MIN({PT}), GROUP_CONCAT(DISTINCT {BU}), CASE WHEN d.final_url != d.u THEN d.final_url END,
                   d.response_time_ms, COALESCE(NULLIF(d.error_type, ''), d.remark), d.url_scope
            {REL_JOIN} AND d.cat != 'OK (2xx)' GROUP BY d.u ORDER BY rels DESC, pages DESC, d.u""", (job,)).fetchall()
        out["problem_links_total"] = c.execute(f"SELECT COUNT(*) {REL_JOIN} AND d.cat != 'OK (2xx)'", (job,)).fetchone()[0]
        out["problem_links"] = c.execute(f"""
            SELECT r.source_page_url, {PT}, {BU}, {LANG}, COALESCE(r.representative_location, ''), COALESCE(r.anchor_text, ''),
                   r.found_url, d.first, d.final, d.cat, CASE WHEN d.final_url != d.u THEN d.final_url END
            {REL_JOIN} AND d.cat != 'OK (2xx)' ORDER BY d.cat, r.normalized_found_url, r.source_page_url LIMIT {PROBLEM_LINKS_MAX}""",
                                         (job,)).fetchall()
        try:
            out["images"] = c.execute("""
                SELECT c.normalized_url, c.initial_http_status, c.http_status, c.status_bucket,
                       (SELECT COUNT(DISTINCT r.source_page_url) FROM on_page_image_relationships r INDEXED BY idx_opir_img
                         WHERE r.job_id = c.job_id AND r.normalized_image_url = c.normalized_url),
                       (SELECT r.source_page_url FROM on_page_image_relationships r INDEXED BY idx_opir_img
                         WHERE r.job_id = c.job_id AND r.normalized_image_url = c.normalized_url LIMIT 1)
                FROM on_page_image_validation_cache c WHERE c.job_id = ? AND (c.status_bucket IN ('4xx','5xx','Timeout','DNS/Network','SSL','Other errors')
                  OR COALESCE(c.redirect_count, 0) > 0 OR (c.status_bucket = '3xx' AND (c.http_status IS NULL OR c.http_status >= 400)))
                ORDER BY 5 DESC, 1""", (job,)).fetchall()
        except sqlite3.OperationalError:
            out["images"] = []
        out["pages_bad"] = c.execute("""SELECT source_page_url, initial_http_status, http_status, crawl_state,
                                        COALESCE(NULLIF(error_type, ''), remark, '') FROM on_page_source_pages
                                        WHERE job_id = ? AND crawl_state != 'PENDING' AND NOT (crawl_state = 'COMPLETED'
                                        AND COALESCE(http_status, 0) = 200 AND COALESCE(initial_http_status, 200) = 200)
                                        ORDER BY source_page_id""", (job,)).fetchall()
        return out
    finally:
        c.close()


# ------------------------------------------------------------------ findings in plain words
def findings(d: Dict[str, Any], tickets: List[Dict[str, Any]], prev: Optional[Dict[str, int]]) -> List[str]:
    k = d["kpi"]
    out: List[str] = []
    healthy = 100.0 * k["ok"] / k["links"] if k["links"] else 100.0
    out.append(f"{healthy:.1f}% of the {k['links']:,} unique links work directly; {k['broken']:,} are broken and "
               f"{k['redirects']:,} redirect.")
    if tickets:
        t = tickets[0]
        out.append(f"Biggest issue: {t['issue']} ({t['affected']}), priority {t['priority'].split(' ')[0]}.")
    pt = [r for r in d["page_type"] if r[4]]
    if pt:
        worst = max(pt, key=lambda r: (r[4] / r[3] if r[3] else 0, r[4]))
        out.append(f"Page type most affected: {worst[0]}, where {100.0 * worst[4] / worst[3]:.1f}% of links are not OK "
                   f"({worst[4]:,} of {worst[3]:,}).")
    bu = [r for r in d["business"] if r[4] and r[0] != "Unclassified"]
    if bu:
        worst = max(bu, key=lambda r: (r[4] / r[3] if r[3] else 0, r[4]))
        out.append(f"Business most affected: {worst[0]}, {100.0 * worst[2] / worst[1]:.1f}% of its destinations fail "
                   f"({worst[2]:,} of {worst[1]:,}).")
    if prev:
        delta = k["broken"] - prev["broken"]
        word = "more" if delta > 0 else "fewer"
        out.append(f"Since the previous check: {abs(delta):,} {word} broken links ({prev['broken']:,} -> {k['broken']:,})"
                   + (f", redirects {prev['redirects']:,} -> {k['redirects']:,}." if prev["redirects"] != k["redirects"] else "."))
    if k["relationships"]:
        saved = 100.0 * (1 - k["links"] / k["relationships"])
        out.append(f"Efficiency: {k['relationships']:,} links found on {k['pages']:,} pages, {k['links']:,} unique, each checked "
                   f"once ({saved:.1f}% of repeat checks avoided).")
    return out[:6]


# ------------------------------------------------------------------ the workbook
def build(run: Dict[str, Any], site: Dict[str, Any], d: Dict[str, Any], tickets: List[Dict[str, Any]],
          prev: Optional[Dict[str, int]], prev_run: Optional[int], score: Optional[float], source: str = "") -> str:
    import xlsxwriter
    fd, path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    wb = xlsxwriter.Workbook(path, {"strings_to_urls": False, "strings_to_numbers": False, "strings_to_formulas": False})
    wb.set_properties({"title": f"QC report - {site['primary_host']} - check #{run['id']}", "author": "QC V6",
                       "comments": "Management report: dashboard, pivot summary, non-200 links, tickets"})
    F = _formats(wb)
    k = d["kpi"]
    dash = wb.add_worksheet("Dashboard")
    piv = wb.add_worksheet("Pivot Summary")
    pos = _pivot(wb, piv, F, d, run, site, source)
    _non200(wb, wb.add_worksheet("Non-200 Responses"), F, d)
    _tickets(wb.add_worksheet("Tickets"), F, tickets)
    _problem_links(wb.add_worksheet("Problem Links"), F, d)
    if d["images"]:
        _images(wb.add_worksheet("Images Not OK"), F, d["images"])
    if d["pages_bad"]:
        _pages(wb.add_worksheet("Pages Not OK"), F, d["pages_bad"])
    _dashboard(wb, dash, F, d, run, site, tickets, prev, prev_run, score, pos, source)
    dash.activate()
    wb.close()
    return path


def _formats(wb) -> Dict[str, Any]:
    f = lambda **o: wb.add_format({"font_name": "Calibri", "font_size": 10, "valign": "vcenter", **o})   # noqa: E731
    F = {
        "title": f(bold=True, font_size=20, font_color=BRAND), "sub": f(font_color=MUTED, font_size=10),
        "h": f(bold=True, font_size=12, font_color="#FFFFFF", bg_color=BRAND),
        "th": f(bold=True, font_color="#FFFFFF", bg_color="#2C6E5E", border=1, border_color=LINE, text_wrap=True),
        "td": f(border=1, border_color=LINE), "tdw": f(border=1, border_color=LINE, text_wrap=True),
        "num": f(border=1, border_color=LINE, num_format="#,##0"), "pct": f(border=1, border_color=LINE, num_format="0.0%"),
        "tot": f(bold=True, border=1, border_color=LINE, bg_color="#E8EFEC"),
        "totn": f(bold=True, border=1, border_color=LINE, bg_color="#E8EFEC", num_format="#,##0"),
        "totp": f(bold=True, border=1, border_color=LINE, bg_color="#E8EFEC", num_format="0.0%"),
        "link": f(border=1, border_color=LINE, font_color="#0B5CAD", underline=1),
        "kl": f(font_color=MUTED, bg_color=SAND, font_size=9, bold=True, indent=1),
        "kv": f(bold=True, font_size=24, bg_color=SAND, indent=1, num_format="#,##0"),
        "kvbad": f(bold=True, font_size=24, bg_color=SAND, indent=1, num_format="#,##0", font_color="#B42318"),
        "kvwarn": f(bold=True, font_size=24, bg_color=SAND, indent=1, num_format="#,##0", font_color="#9A6A00"),
        "kvok": f(bold=True, font_size=24, bg_color=SAND, indent=1, num_format="#,##0", font_color=BRAND),
        "ks": f(font_color=MUTED, bg_color=SAND, font_size=9, indent=1),
        "kup": f(font_color="#B42318", bg_color=SAND, font_size=9, indent=1, bold=True),
        "kdown": f(font_color=BRAND, bg_color=SAND, font_size=9, indent=1, bold=True),
        "bullet": f(text_wrap=True, valign="top", font_size=11), "note": f(italic=True, font_color=MUTED, font_size=9),
        "big": f(bold=True, font_size=13, font_color=INK),
    }
    for cat, fill in CAT_FILL.items():
        F["cat:" + cat] = f(border=1, border_color=LINE, bg_color=fill, bold=True)
    for p, fill in {"P1": "#FADBD8", "P2": "#FCE4D6", "P3": "#FFF2CC", "P4": "#D9E1F2"}.items():
        F["pri:" + p] = f(border=1, border_color=LINE, bg_color=fill, bold=True)
    return F


def _section(ws, F, row: int, title: str, ncols: int) -> int:
    ws.merge_range(row, 0, row, ncols - 1, title, F["h"])
    ws.set_row(row, 20)
    return row + 1


def _status_text(code) -> str:
    if code is None:
        return ""
    return STATUS_TEXT.get(int(code), "")


def _pivot(wb, ws, F, d, run, site, source) -> Dict[str, Any]:
    k = d["kpi"]
    ws.set_column(0, 0, 34)
    ws.set_column(1, 7, 18)
    ws.write(0, 0, f"Pivot summary - {site['primary_host']} - QC check #{run['id']}", F["title"])
    ws.write(1, 0, f"{k['pages']:,} pages · {k['relationships']:,} links found · {k['links']:,} unique destinations checked once "
                   f"({(1 - k['links'] / k['relationships']) * 100 if k['relationships'] else 0:.1f}% repeat checks avoided)"
                   + (f" · source: {source}" if source else ""), F["sub"])
    pos: Dict[str, Any] = {}
    r = _section(ws, F, 3, "A. OVERALL STATUS (RECONCILIATION)", 5)
    ws.write_row(r, 0, ["Status category", "Unique URLs", "Unique URLs %", "Link occurrences", "Link occurrences %"], F["th"])
    r += 1
    tu = sum(x[1] for x in d["status"]) or 1
    to = sum(x[2] for x in d["status"]) or 1
    pos["status"] = (r, r + len(d["status"]) - 1)
    for cat, u, o in d["status"]:
        ws.write(r, 0, cat, F["cat:" + cat])
        ws.write_number(r, 1, u, F["num"])
        ws.write_number(r, 2, u / tu, F["pct"])
        ws.write_number(r, 3, o, F["num"])
        ws.write_number(r, 4, o / to, F["pct"])
        r += 1
    ws.write(r, 0, "Total", F["tot"]); ws.write_number(r, 1, tu if d["status"] else 0, F["totn"]); ws.write_number(r, 2, 1, F["totp"])  # noqa: E702
    ws.write_number(r, 3, to if d["status"] else 0, F["totn"]); ws.write_number(r, 4, 1, F["totp"])                                   # noqa: E702
    r = _section(ws, F, r + 2, "B. HTTP STATUS CODES (FIRST ANSWER -> FINAL ANSWER)", 6)
    ws.write_row(r, 0, ["First status", "Final status", "Meaning", "Category", "Unique URLs", "Link occurrences"], F["th"])
    r += 1
    for first, final, cat, u, o in d["codes"]:
        ws.write(r, 0, first if first is not None else "no answer", F["td"])
        ws.write(r, 1, final if final is not None else "", F["td"])
        m = _status_text(first) + (f" -> {_status_text(final)}" if first is not None and final is not None and first != final else "")
        ws.write(r, 2, m or cat, F["td"])
        ws.write(r, 3, cat, F["cat:" + cat])
        ws.write_number(r, 4, u, F["num"]); ws.write_number(r, 5, o, F["num"])                                            # noqa: E702
        r += 1

    def breakdown(r, title, label, rows, key):
        r = _section(ws, F, r + 1, title, 5)
        ws.write_row(r, 0, [label, "Unique destinations", "Non-200 destinations", "Link occurrences", "Non-200 occurrences %"], F["th"])
        r += 1
        start = r
        for name, ud, nd, rel, nrel in rows:
            ws.write(r, 0, name, F["td"]); ws.write_number(r, 1, ud, F["num"]); ws.write_number(r, 2, nd, F["num"])        # noqa: E702
            ws.write_number(r, 3, rel, F["num"]); ws.write_number(r, 4, (nrel or 0) / rel if rel else 0, F["pct"])        # noqa: E702
            r += 1
        pos[key] = (start, r - 1)
        ws.write(r, 0, "Total", F["tot"])
        ws.write_number(r, 1, d["kpi"]["links"], F["totn"])
        ws.write_number(r, 2, d["kpi"]["links"] - d["kpi"]["ok"], F["totn"])
        ws.write_number(r, 3, sum(x[3] for x in rows), F["totn"])
        tot = sum(x[3] for x in rows)
        ws.write_number(r, 4, (sum((x[4] or 0) for x in rows) / tot) if tot else 0, F["totp"])
        if rows:
            ws.conditional_format(start, 4, r - 1, 4, {"type": "data_bar", "bar_color": "#E6A38F", "bar_solid": True,
                                                       "min_type": "num", "min_value": 0, "max_type": "num", "max_value": 1})
        return r + 1

    r = breakdown(r + 1, "C. SOURCE PAGE TYPE", "Page type", d["page_type"], "page_type")
    r = breakdown(r, "D. BUSINESS", "Business", d["business"], "business")
    r = breakdown(r, "E. LANGUAGE", "Language", d["language"], "language")
    cats = [c for c in CATEGORIES if any(c in v for v in d["matrix"].values())]
    r = _section(ws, F, r + 1, "F. PAGE TYPE x STATUS (LINK OCCURRENCES)", max(len(cats) + 2, 5))
    ws.write_row(r, 0, ["Page type"] + cats + ["Total"], F["th"])
    r += 1
    mstart = r
    for pt in sorted(d["matrix"], key=lambda p: -sum(v for c, v in d["matrix"][p].items() if c != "OK (2xx)")):
        row = d["matrix"][pt]
        ws.write(r, 0, pt, F["td"])
        for j, cat in enumerate(cats, 1):
            ws.write_number(r, j, row.get(cat, 0), F["num"])
        ws.write_number(r, len(cats) + 1, sum(row.values()), F["totn"])
        r += 1
    if d["matrix"] and len(cats) > 1:
        ws.conditional_format(mstart, 2, r - 1, len(cats), {"type": "2_color_scale", "min_color": "#FFFFFF", "max_color": "#F4B7A6"})
    r = _section(ws, F, r + 1, "G. HIGHEST-IMPACT FAILED DESTINATIONS (TOP 20)", 7)
    ws.write_row(r, 0, ["Destination URL", "Status", "Category", "Link occurrences", "Pages", "Sample source page", "Page type"], F["th"])
    r += 1
    for row in d["non200"][:20]:
        u, first, final, cat, rels, pages, sample, pt = row[:8]
        ws.write_url(r, 0, u, F["link"], u)
        ws.write(r, 1, f"{first or ''}{' -> ' + str(final) if final is not None and first != final else ''}", F["td"])
        ws.write(r, 2, cat, F["cat:" + cat]); ws.write_number(r, 3, rels, F["num"]); ws.write_number(r, 4, pages, F["num"])   # noqa: E702
        ws.write_url(r, 5, sample, F["link"], sample); ws.write(r, 6, pt, F["td"])                                            # noqa: E702
        r += 1
    if not d["non200"]:
        ws.write(r, 0, "Every link answers 200 directly.", F["note"]); r += 1                                                 # noqa: E702
    r = _section(ws, F, r + 1, "H. STATUS COLOUR LEGEND", 3)
    ws.write_row(r, 0, ["Category", "Meaning", "Recommended action"], F["th"])
    r += 1
    for cat, meaning, action in LEGEND:
        ws.write(r, 0, cat, F["cat:" + (cat if cat in CAT_FILL else "Timeout")]); ws.write(r, 1, meaning, F["tdw"])            # noqa: E702
        ws.write(r, 2, action, F["tdw"])
        r += 1
    ws.freeze_panes(3, 0)
    ws.hide_gridlines(2)
    return pos


def _dashboard(wb, ws, F, d, run, site, tickets, prev, prev_run, score, pos, source) -> None:
    k = d["kpi"]
    ws.hide_gridlines(2)
    ws.set_column(0, 0, 2)
    ws.set_column(1, 14, 12.5)
    when = run.get("finished_at") or run.get("started_at")
    when_s = when.astimezone(timezone.utc).strftime("%d %b %Y") if isinstance(when, datetime) else ""
    ws.write(1, 1, f"Website quality report - {site['primary_host']}", F["title"])
    ws.write(2, 1, f"QC check #{run['id']}" + (f" · {when_s}" if when_s else "") + f" · {k['pages']:,} pages checked"
             + (f" · compared with check #{prev_run}" if prev else "") + (f" · {source}" if source else ""), F["sub"])

    def tile(col, label, value, sub, fmt="kv", delta=None, good_down=True):
        for rr in range(4, 8):
            for cc in (col, col + 1):
                ws.write_blank(rr, cc, None, F["ks"])
        ws.merge_range(4, col, 4, col + 1, label, F["kl"])
        ws.merge_range(5, col, 6, col + 1, value, F[fmt])
        if delta is not None and delta != 0:
            worse = (delta > 0) == good_down
            ws.merge_range(7, col, 7, col + 1, f"{'▲' if delta > 0 else '▼'} {abs(delta):,} vs last check", F["kup" if worse else "kdown"])
        else:
            ws.merge_range(7, col, 7, col + 1, sub, F["ks"])
    ws.set_row(5, 22)
    ws.set_row(6, 22)
    pd = (lambda key: k[key] - prev[key]) if prev else (lambda key: None)                                                       # noqa: E731
    tile(1, "PAGES CHECKED", k["pages"], f"{k['pages_not_ok']:,} not answering 200", "kv")
    tile(3, "UNIQUE LINKS", k["links"], f"{k['relationships']:,} links on the pages", "kv")
    tile(5, "BROKEN LINKS", k["broken"], f"{(k['broken'] / k['links'] * 100) if k['links'] else 0:.1f}% of unique links",
         "kvbad" if k["broken"] else "kvok", pd("broken"))
    tile(7, "REDIRECTING LINKS", k["redirects"], f"{(k['redirects'] / k['links'] * 100) if k['links'] else 0:.1f}% of unique links",
         "kvwarn" if k["redirects"] else "kvok", pd("redirects"))
    tile(9, "BROKEN IMAGES", k["images_broken"], f"of {k['images']:,} unique images", "kvbad" if k["images_broken"] else "kvok",
         pd("images_broken"))
    tile(11, "PAGES NOT OK", k["pages_not_ok"], f"of {k['pages']:,} pages", "kvbad" if k["pages_not_ok"] else "kvok", pd("pages_not_ok"))
    tile(13, "HEALTH SCORE", round(score) if score is not None else "-", "QC score out of 100", "kv")

    r = 9
    ws.write(r, 1, "Key findings", F["big"])
    r += 1
    for line in findings(d, tickets, prev):
        ws.merge_range(r, 1, r, 14, "•  " + line, F["bullet"])
        ws.set_row(r, 18)
        r += 1

    # charts, from the Pivot Summary tables
    r += 1
    def chart_status():
        a, b = pos["status"]
        ch = wb.add_chart({"type": "doughnut"})
        pts = [{"fill": {"color": CAT_COLOR.get(x[0], "#999999")}} for x in d["status"]]
        ch.add_series({"name": "Unique links by status", "categories": ["Pivot Summary", a, 0, b, 0],
                       "values": ["Pivot Summary", a, 1, b, 1], "points": pts,
                       "data_labels": {"percentage": True, "leader_lines": True, "font": {"size": 8}}})
        ch.set_title({"name": "Unique links by status", "name_font": {"size": 11, "bold": True}})
        ch.set_hole_size(55)
        ch.set_legend({"position": "right", "font": {"size": 8}})
        return ch

    def chart_bar(key, title, col, color, limit=10):
        a, b = pos[key]
        b = min(b, a + limit - 1)
        ch = wb.add_chart({"type": "bar"})
        ch.add_series({"name": title, "categories": ["Pivot Summary", a, 0, b, 0], "values": ["Pivot Summary", a, col, b, col],
                       "fill": {"color": color}, "gap": 60, "data_labels": {"value": True, "font": {"size": 8},
                                                                             "num_format": "0.0%" if col == 4 else "#,##0"}})
        ch.set_title({"name": title, "name_font": {"size": 11, "bold": True}})
        ch.set_legend({"none": True})
        ch.set_y_axis({"reverse": True, "crossing": "max", "num_font": {"size": 8}})        # worst first, scale at the bottom
        ch.set_x_axis({"num_font": {"size": 8}, "major_gridlines": {"visible": True, "line": {"color": "#EEEEEE"}},
                       "num_format": "0%" if col == 4 else "#,##0"})
        return ch

    size = {"x_scale": 1.13, "y_scale": 1.05}
    if pos.get("status") and d["status"]:
        ws.insert_chart(r, 1, chart_status(), size)
    if pos.get("page_type") and d["page_type"]:
        ws.insert_chart(r, 8, chart_bar("page_type", "Non-200 links by page type", 2, "#C2410C"), size)
    r += 17
    if pos.get("business") and d["business"]:
        ws.insert_chart(r, 1, chart_bar("business", "Share of links not OK, by business", 4, "#B42318"), size)
    if tickets:
        # top issues table for the chart lives on this sheet, to the right (hidden helper columns)
        top = tickets[:8]
        ws.write(r - 1, 20, "Issue"); ws.write(r - 1, 21, "Pages")                                                             # noqa: E702
        for i, t in enumerate(top):
            ws.write(r + i, 20, t["issue"][:60])
            ws.write_number(r + i, 21, t["pages"])
        ws.set_column(20, 21, None, None, {"hidden": True})
        ch = wb.add_chart({"type": "bar"})
        ch.add_series({"name": "Pages affected by issue", "categories": ["Dashboard", r, 20, r + len(top) - 1, 20],
                       "values": ["Dashboard", r, 21, r + len(top) - 1, 21], "fill": {"color": BRAND}, "gap": 60,
                       "data_labels": {"value": True, "font": {"size": 8}, "num_format": "#,##0"}})
        ch.set_title({"name": "Issues by pages affected", "name_font": {"size": 11, "bold": True}})
        ch.set_legend({"none": True})
        ch.set_y_axis({"reverse": True, "crossing": "max", "num_font": {"size": 8}})
        ch.set_x_axis({"num_font": {"size": 8}, "major_gridlines": {"visible": True, "line": {"color": "#EEEEEE"}}})
        ws.insert_chart(r, 8, ch, size)
    r += 17

    ws.write(r, 1, "Next steps (one ticket per issue type; every URL is in the ticket's attachment)", F["big"])
    r += 1
    ws.write_row(r, 1, ["Ticket", "Priority"], F["th"])
    ws.merge_range(r, 3, r, 7, "Issue", F["th"])
    ws.merge_range(r, 8, r, 9, "Affected", F["th"])
    ws.merge_range(r, 10, r, 14, "Recommended action", F["th"])
    r += 1
    for t in tickets[:8]:
        ws.set_row(r, 30)
        ws.write(r, 1, t["ticket_id"], F["td"])
        pr = t["priority"].split(" ")[0]
        ws.write(r, 2, pr, F.get("pri:" + pr, F["td"]))
        ws.merge_range(r, 3, r, 7, t["issue"], F["tdw"])
        ws.merge_range(r, 8, r, 9, t["affected"], F["tdw"])
        ws.merge_range(r, 10, r, 14, t.get("fix") or "", F["tdw"])
        r += 1
    if not tickets:
        ws.write(r, 1, "No open issues.", F["note"])
        r += 1
    ws.write(r + 1, 1, "Details: Pivot Summary, Non-200 Responses, Tickets and Problem Links sheets. Every link with every page, "
                       "including the working ones: QC -> Download report -> All data as CSVs (ZIP).", F["note"])
    ws.set_landscape()
    ws.fit_to_pages(1, 0)
    ws.set_paper(9)


def _table_sheet(ws, F, header, widths, rows_iter, link_cols=(), cat_col=None, note_empty="Nothing to report."):
    for i, w in enumerate(widths):
        ws.set_column(i, i, w)
    ws.write_row(0, 0, header, F["th"])
    ws.set_row(0, 30)
    n = 0
    links = 0
    for row in rows_iter:
        n += 1
        for j, v in enumerate(row):
            if v is None or v == "":
                continue
            if j in link_cols and isinstance(v, str) and v.startswith(("http://", "https://")) and links < HYPERLINKS_MAX and len(v) < 2000:
                ws.write_url(n, j, v, F["link"], v)
                links += 1
            elif j == cat_col:
                ws.write(n, j, v, F.get("cat:" + str(v), F["td"]))
            elif isinstance(v, (int, float)):
                ws.write_number(n, j, v)
            else:
                ws.write_string(n, j, str(v)[:32000])
    if n:
        ws.autofilter(0, 0, n, len(header) - 1)
    else:
        ws.write(1, 0, note_empty, F["note"])
    ws.freeze_panes(1, 0)
    return n


def _non200(wb, ws, F, d) -> None:
    rows = ((i, u, first, final, cat, rels, pages, sample, pt, bu, target, round(rt) if rt else None, reason,
             "Other site" if scope == "EXTERNAL" else "This site")
            for i, (u, first, final, cat, rels, pages, sample, pt, bu, target, rt, reason, scope) in enumerate(d["non200"], 1))
    _table_sheet(ws, F, ["Sr.no", "Destination URL", "First status", "Final status", "Status category", "Link occurrences",
                         "Pages affected", "Sample source page", "Source page type", "Business", "Redirect target",
                         "Response time (ms)", "Reason", "Where it goes"],
                 [7, 60, 11, 11, 18, 13, 12, 55, 18, 16, 50, 12, 24, 12], rows, link_cols=(1, 7, 10), cat_col=4,
                 note_empty="Every link answers 200 directly.")


def _tickets(ws, F, tickets) -> None:
    rows = ((t["ticket_id"], t["priority"].split(" ")[0], t["severity"].title(), t["issue"], t["type"], t["urls"] or None, t["pages"],
             {"to_raise": "To raise", "raised": "Raised", "closed": "Closed"}.get(t["state"], t["state"]), t["raised_ref"] or "",
             t.get("fix") or "", t.get("why") or "", t["attachment"]) for t in tickets)
    n = _table_sheet(ws, F, ["Ticket", "Priority", "Severity", "Issue", "Type", "URLs", "Pages", "State", "Raised as",
                             "Recommended action", "Impact", "Attachment (download in QC: Tickets)"],
                     [10, 9, 10, 36, 12, 9, 10, 10, 12, 60, 50, 36], rows, note_empty="No issues found.")
    for p, fmt in (("P1", "pri:P1"), ("P2", "pri:P2"), ("P3", "pri:P3"), ("P4", "pri:P4")):
        if n:
            ws.conditional_format(1, 1, n, 1, {"type": "cell", "criteria": "==", "value": f'"{p}"', "format": F[fmt]})


def _problem_links(ws, F, d) -> None:
    rows = ((i, *r) for i, r in enumerate(d["problem_links"], 1))
    n = _table_sheet(ws, F, ["Sr.no", "Source page URL", "Page type", "Business", "Language", "Component", "Anchor text",
                             "Destination URL", "First status", "Final status", "Status category", "Redirect target"],
                     [7, 60, 16, 14, 9, 16, 30, 60, 10, 10, 18, 50], rows, cat_col=10, note_empty="Every link answers 200 directly.")
    total = d["problem_links_total"]
    if total > n:
        ws.write(n + 2, 1, f"Showing {n:,} of {total:,} page -> link rows. Every pair is in each ticket's attachment "
                           f"(QC -> Tickets) and in QC -> Download report -> All data as CSVs (ZIP).", F["note"])


def _images(ws, F, images) -> None:
    rows = ((i, u, a, b, bucket, pages, ex) for i, (u, a, b, bucket, pages, ex) in enumerate(images, 1))
    _table_sheet(ws, F, ["Sr.no", "Image URL", "First status", "Final status", "Status", "Pages", "Example page"],
                 [7, 70, 11, 11, 12, 10, 60], rows, link_cols=(1, 6))


def _pages(ws, F, pages) -> None:
    rows = ((i, u, a, b, st, reason) for i, (u, a, b, st, reason) in enumerate(pages, 1))
    _table_sheet(ws, F, ["Sr.no", "Page URL", "First status", "Final status", "Crawl state", "Reason"],
                 [7, 80, 11, 11, 14, 40], rows, link_cols=(1,))


# ------------------------------------------------------------------ entry point
async def report_file(pool, site: Dict[str, Any], run_id: int) -> Optional[str]:
    """Build the management report for a check; None when the check has no crawl data."""
    import asyncio
    from qc6 import internal_tickets
    run = dict(await pool.fetchrow("SELECT * FROM runs WHERE id = $1", run_id))
    templates = {r["url"]: r["name"] for r in await pool.fetch(
        "SELECT p.url, t.name FROM pages p JOIN templates t ON t.id = p.template_id WHERE p.run_id = $1", run_id)}
    d = await asyncio.to_thread(collect, run_id, templates)
    if d is None:
        return None
    tickets = (await internal_tickets.list_for_site(pool, site, "all", "", "", run_id=run_id))["tickets"]
    texts = await _ticket_texts(pool, site["id"], [t["group_key"] for t in tickets])
    for t in tickets:
        t.update(texts.get(t["group_key"], {}))
    prev_run = await pool.fetchval("""SELECT id FROM runs WHERE site_id = $1 AND id < $2 AND status = 'completed'
                                      AND kind IN ('coverage', 'full') AND NOT (options ? 'spot') ORDER BY id DESC LIMIT 1""",
                                   site["id"], run_id)
    prev = await asyncio.to_thread(_kpis_of_run, prev_run)
    source = await pool.fetchval("SELECT label FROM url_lists WHERE run_id = $1 ORDER BY id DESC LIMIT 1", run_id) or ""
    score = (run.get("summary") or {}).get("score")
    return await asyncio.to_thread(build, run, site, d, tickets, prev, prev_run if prev else None, score, source)


async def _ticket_texts(pool, site_id: int, keys: List[str]) -> Dict[str, Dict[str, str]]:
    """The 'how to fix' and 'why' lines each ticket uses, for the Tickets sheet and the dashboard's next steps."""
    from qc6 import internal_tickets as it
    from qc6.domain.rules import RULES
    out: Dict[str, Dict[str, str]] = {}
    if not keys:
        return out
    rows = await pool.fetch(f"""SELECT DISTINCT ON (gkey) gkey, texts FROM (SELECT {it.group_key_sql()} AS gkey, f.texts, f.pages_affected
                               FROM fixes f WHERE f.site_id = $1) x WHERE gkey = ANY($2::text[]) ORDER BY gkey, pages_affected DESC""",
                            site_id, keys)
    for r in rows:
        rule = r["gkey"].split(":")[0]
        texts = r["texts"] or {}
        rd = RULES.get(rule)
        out[r["gkey"]] = {"fix": it.GROUP_FIX.get(rule) or texts.get("fix") or "",
                          "why": (rd.why if rd and rule not in ("PAGE_CHECK", "LINK_CHECK") else "") or texts.get("why") or ""}
    return out
