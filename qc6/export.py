"""The whole report of one check in ONE sheet: CSV, or Excel (.xlsx, one worksheet). Small on purpose:

  1. Summary            site, check, pages, health and its areas, open fixes by severity, changes since the check before,
                        visits, journeys
  2. Problems           one row per problem: severity, what's wrong, pages, status, owner, why it matters, how to fix
  3. Pages and problems one row per page x problem: the page, its answer and type, the problem (by its number in part 2),
                        what was found, and a remark: new in this check, or the earlier dates it was already found

The long texts appear once per problem (part 2), never repeated per page row, which keeps a 3,00,000-page report small.
Text cells that a spreadsheet would run as a formula get a leading apostrophe.
"""
from __future__ import annotations

import csv
import io
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, Iterable, List, Optional

from qc6.history import remark

SEV_ORDER = "CASE f.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END"
DETAIL_HEADER = ["Page", "Answer", "Page type", "Problem no.", "Severity", "Problem", "Found", "Remarks"]
EXCEL_MAX_ROWS = 1_048_576
PROBLEM_HEADER = ["Problem no.", "Severity", "What's wrong", "Pages", "Status", "Owner", "First found", "Why it matters", "How to fix",
                  "How we'll know"]


def cell(v: Any) -> Any:
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    if isinstance(v, datetime):
        return v.strftime("%d %b %Y %H:%M")
    return "" if v is None else v


async def summary_rows(pool, site: Dict[str, Any], run_id: int) -> List[List[Any]]:
    from qc6.inventory import changes_since, previous_check
    run = await pool.fetchrow("SELECT * FROM runs WHERE id = $1", run_id)
    summ = run["summary"] or {}
    sc = await pool.fetchrow("SELECT score, dimensions FROM score_snapshots WHERE run_id = $1", run_id)
    sev = {r["severity"]: (r["n"], r["pages"]) for r in await pool.fetch(
        """SELECT f.severity, count(DISTINCT f.id) AS n, count(DISTINCT fd.page_url) AS pages FROM findings fd JOIN fixes f ON f.id = fd.fix_id
           WHERE fd.run_id = $1 GROUP BY f.severity""", run_id)}
    prev = await previous_check(pool, site["id"], run_id)
    ch = await changes_since(pool, run_id, prev)
    ev = {r["kind"]: r["n"] for r in await pool.fetch(
        """SELECT kind, count(DISTINCT fix_id) AS n FROM fix_events WHERE run_id = $1 AND kind IN ('found', 'verified', 'regressed')
           GROUP BY kind""", run_id)}
    visits = {r["status"]: r["n"] for r in await pool.fetch("SELECT status, count(*) AS n FROM visits WHERE run_id = $1 GROUP BY status", run_id)}
    jr = await pool.fetchrow("SELECT count(*) AS n, count(*) FILTER (WHERE status = 'passed') AS ok FROM journey_runs WHERE run_id = $1", run_id)
    spot = (run["options"] or {}).get("spot")
    rows: List[List[Any]] = [
        ["QC site quality report"],
        ["Site", site["name"]], ["Address", site["base_url"]],
        ["Check", f"#{run_id}" + (f" (spot check: {spot.get('label')})" if spot else "")],
        ["Started", run["started_at"]], ["Finished", run["finished_at"]],
        ["Report made", datetime.now(timezone.utc).replace(tzinfo=None)],
        ["Pages checked", summ.get("pages")],
        ["Health score (qcv6-health-1.0)", round(sc["score"], 1) if sc else "not scored (spot check)" if spot else ""],
    ]
    for k, v in ((sc["dimensions"] or {}).items() if sc else []):
        rows.append([f"  {k.title()}", v])
    for s in ("critical", "high", "medium", "low"):
        n, p = sev.get(s, (0, 0))
        rows.append([f"{s.title()} problems", n, f"on {p} pages"])
    if prev:
        rows += [["Compared with", f"check #{prev}"], ["New pages", ch.get("new")], ["Pages gone", ch.get("gone")],
                 ["Pages that changed answer", ch.get("status_changed")]]
    rows += [["New problems in this check", ev.get("found", 0)], ["Confirmed fixed by this check", ev.get("verified", 0)],
             ["Came back in this check", ev.get("regressed", 0)]]
    if visits:
        rows.append(["Real-browser visits", sum(visits.values()), f"{visits.get('ok', 0)} loaded fine"])
    if jr and jr["n"]:
        rows.append(["Journeys", jr["n"], f"{jr['ok']} passed"])
    if summ.get("links") is not None:
        rows.append(["Links found", summ.get("links")])
    if summ.get("images") is not None:
        rows.append(["Images found", summ.get("images")])
    if summ.get("trust_gate"):
        rows.append(["Numbers add up (trust gate)", summ.get("trust_gate")])
    return rows


async def problem_rows(pool, run_id: int) -> List[List[Any]]:
    rows = await pool.fetch(f"""
        SELECT f.id, f.severity, f.title, f.status, m.name AS owner, f.texts, count(DISTINCT fd.page_url) AS pages,
               (SELECT min(coalesce(r.finished_at, r.started_at)) FROM fix_events e JOIN runs r ON r.id = e.run_id
                 WHERE e.fix_id = f.id AND e.kind = 'found') AS first_found
        FROM findings fd JOIN fixes f ON f.id = fd.fix_id LEFT JOIN members m ON m.id = f.owner_id
        WHERE fd.run_id = $1 GROUP BY f.id, m.name ORDER BY {SEV_ORDER}, count(DISTINCT fd.page_url) DESC, f.id""", run_id)
    out = []
    for r in rows:
        t = r["texts"] or {}
        out.append([r["id"], r["severity"].title(), r["title"], r["pages"], r["status"].replace("_", " "), r["owner"] or "",
                    r["first_found"], t.get("why", ""), t.get("fix", ""), t.get("verify", "")])
    return out


DETAIL_SQL = f"""
    SELECT fd.page_url, p.http_status, t.name AS page_type, f.id AS fix_id, f.severity, f.title, f.status,
           coalesce(nullif(fd.subject, ''), fd.evidence->>'value', '') AS found,
           h.first_seen, h.n, h.days
    FROM findings fd
    JOIN fixes f ON f.id = fd.fix_id
    LEFT JOIN pages p ON p.run_id = fd.run_id AND p.url = fd.page_url
    LEFT JOIN templates t ON t.id = p.template_id
    LEFT JOIN LATERAL (
        SELECT min(coalesce(r.finished_at, r.started_at)) AS first_seen, count(DISTINCT e.run_id) AS n,
               array_agg(DISTINCT coalesce(r.finished_at, r.started_at)) AS days
        FROM findings e JOIN runs r ON r.id = e.run_id AND r.status = 'completed'
        WHERE e.fix_id = fd.fix_id AND e.page_url = fd.page_url AND e.run_id < fd.run_id) h ON true
    WHERE fd.run_id = $1
    ORDER BY {SEV_ORDER}, f.id, fd.page_url"""


def _detail(r) -> List[Any]:
    h = {"dates": sorted(d for d in (r["days"] or []) if d), "first_seen": r["first_seen"], "checks": r["n"]} if r["n"] else None
    return [r["page_url"], r["http_status"] if r["http_status"] is not None else "no answer", r["page_type"] or "", r["fix_id"],
            r["severity"].title(), r["title"], r["found"] or "", short_remark(h)]


def short_remark(h) -> str:
    """'New' or 'Seen before: 20 Nov 2026, 21 Nov 2026 (first 20 Nov 2026, 3 checks)'. Compact: it's on every row."""
    if not h:
        return "New"
    full = remark(h)                                 # "Also found on A, B (first seen C, n earlier checks)"
    return full.replace("Also found on ", "Seen before: ").replace("first seen ", "first ").replace(" earlier check", " check")


async def rows(pool, site: Dict[str, Any], run_id: int, max_rows: Optional[int] = None) -> AsyncIterator[List[Any]]:
    """Every row of the single sheet, in order. Page rows stream from a server-side cursor (constant memory).
    max_rows (Excel's sheet limit): page rows stop there and the summary says so."""
    summ = await summary_rows(pool, site, run_id)
    detail_total = await pool.fetchval("SELECT count(*) FROM findings WHERE run_id = $1", run_id)
    probs = await problem_rows(pool, run_id)
    room = None
    if max_rows is not None:
        room = max_rows - (len(summ) + 4 + len(probs) + 4 + 3)
        if detail_total > room:
            summ.insert(1, [f"NOTE: this sheet shows the first {room:,} of {detail_total:,} page rows (Excel's limit per sheet). "
                            "The CSV or ZIP download has every row."])
    for r in summ:
        yield r
    yield ["Page rows", detail_total]
    yield []
    yield ["PROBLEMS"]
    yield PROBLEM_HEADER
    for r in probs:
        yield r
    yield []
    yield ["PAGES AND PROBLEMS"]
    yield DETAIL_HEADER
    n = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            async for r in conn.cursor(DETAIL_SQL, run_id, prefetch=2000):
                if room is not None and n >= room:
                    break
                yield _detail(r)
                n += 1


async def csv_stream(pool, site: Dict[str, Any], run_id: int) -> AsyncIterator[bytes]:
    buf = io.StringIO()
    w = csv.writer(buf)
    yield "﻿".encode()                          # BOM: Excel opens UTF-8 CSV with the right characters
    n = 0
    async for r in rows(pool, site, run_id):
        w.writerow([cell(v) for v in r])
        n += 1
        if n % 2000 == 0:
            yield buf.getvalue().encode()
            buf.seek(0); buf.truncate()
    yield buf.getvalue().encode()


async def xlsx_file(pool, site: Dict[str, Any], run_id: int) -> str:
    """One worksheet, written in streaming mode to a temporary file (deleted by the caller after sending)."""
    import openpyxl
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font
    wb = openpyxl.Workbook(write_only=True)
    ws = wb.create_sheet("QC report")
    for col, width in zip("ABCDEFGHIJ", (60, 12, 22, 12, 10, 50, 30, 18, 60, 40)):
        ws.column_dimensions[col].width = width
    bold = Font(bold=True)
    heads = {tuple(PROBLEM_HEADER), tuple(DETAIL_HEADER)}
    async for r in rows(pool, site, run_id, max_rows=EXCEL_MAX_ROWS):
        vals = [cell(v) for v in r]
        if (len(r) == 1 and isinstance(r[0], str) and (r[0].isupper() or r[0].startswith("QC "))) or tuple(r) in heads:
            out = []
            for v in vals:
                c = WriteOnlyCell(ws, value=v)
                c.font = bold
                out.append(c)
            ws.append(out)
        else:
            ws.append(vals)
    fd, path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    wb.save(path)
    return path


async def zip_file(pool, site: Dict[str, Any], run_id: int, csv_name: str) -> str:
    """The same single CSV, deflate-compressed in a .zip (Windows opens it without extra software)."""
    import zipfile
    fd, path = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        with z.open(csv_name, "w", force_zip64=True) as f:
            async for chunk in csv_stream(pool, site, run_id):
                f.write(chunk)
    return path


async def workbook_file(pool, site: Dict[str, Any], run_id: int) -> str:
    """The full multi-sheet workbook (qc6/workbook.py), laid out like the QC V5 audit workbook, plus QC's problems and
    every page x problem row with its 'seen before' remark. Page rows go through a temporary file so the workbook is
    written in a worker thread without holding a database cursor open across threads."""
    import asyncio
    import csv
    from qc6 import workbook
    run = dict(await pool.fetchrow("SELECT * FROM runs WHERE id = $1", run_id))
    templates = {r["url"]: r["name"] for r in await pool.fetch(
        "SELECT p.url, t.name FROM pages p JOIN templates t ON t.id = p.template_id WHERE p.run_id = $1", run_id)}
    sev = {r["severity"]: (r["n"], r["pages"]) for r in await pool.fetch(
        "SELECT f.severity, count(DISTINCT f.id) AS n, count(DISTINCT fd.page_url) AS pages FROM findings fd "
        "JOIN fixes f ON f.id = fd.fix_id WHERE fd.run_id = $1 GROUP BY 1", run_id)}
    fd, tmp = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    try:
        n = 0
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            async with pool.acquire() as conn:
                async with conn.transaction():
                    async for r in conn.cursor(DETAIL_SQL, run_id, prefetch=2000):
                        w.writerow([cell(v) for v in _detail(r)])
                        n += 1
                        if n >= workbook.EXCEL_MAX_ROWS - 2:
                            break

        def page_rows():
            with open(tmp, encoding="utf-8", newline="") as f:
                for row in csv.reader(f):
                    if len(row) > 3 and row[3].isdigit():
                        row[3] = int(row[3])
                    yield row
        from qc6 import internal_tickets
        tickets = (await internal_tickets.list_for_site(pool, site, "all", "", "", run_id=run_id))["tickets"]
        extra = {"tickets": tickets, "problems": await problem_rows(pool, run_id), "problem_header": PROBLEM_HEADER, "templates": templates,
                 "severity": sev, "score": (run.get("summary") or {}).get("score"),
                 "page_rows": page_rows, "page_header": DETAIL_HEADER}
        return await asyncio.to_thread(workbook.build, run, site, extra)
    finally:
        os.unlink(tmp)
