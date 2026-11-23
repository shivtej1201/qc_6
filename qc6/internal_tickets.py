"""Internal tickets, one per ISSUE TYPE: all redirecting links are one ticket, all broken links another, each kind of
page problem another. Every affected URL goes in the ticket's attachment (an Excel sheet), not in a ticket of its own.

A ticket is the group of the site's fixes with the same issue key:
  address issues (broken / redirecting links, broken images, near-duplicates...): key = the rule
  page checks that share one rule (soft 404, structured data missing...):       key = rule + the check's code
Its number (QC-0001...) is stable per site and issue, kept in issue_tickets with what the Ops team's tool called it.
The text comes from the site's ticket template; {placeholders} are listed in PLACEHOLDERS.
"""
from __future__ import annotations

import os
import re
import tempfile
from typing import Any, Dict, List, Optional

from qc6.domain.rules import RULES

PRIORITY = {"critical": "P1 - Critical", "high": "P2 - High", "medium": "P3 - Medium", "low": "P4 - Low"}
SEV_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
LISTED = 20                  # addresses / pages written into the ticket text; the attachment has all of them
OPEN = "f.status NOT IN ('verified','wont_fix')"

# one name per issue type, as people would write it on a ticket
GROUP_NAMES = {
    "BROKEN_LINK": "Broken links (4xx / 5xx / no answer)", "REDIRECTED_LINK": "Links that redirect (3xx)",
    "BROKEN_IMAGE": "Broken images", "LINK_CHECK": "Links with problems", "NEAR_DUPLICATE": "Near-duplicate pages",
    "PAGE_NOT_FOUND": "Sitemap pages that return an error", "PAGE_SERVER_ERROR": "Sitemap pages with server errors",
    "TITLE_DUPLICATE": "Pages with duplicate titles", "NOINDEX_IN_SITEMAP": "Noindex pages listed in the sitemap",
    "CANONICAL_ELSEWHERE": "Pages whose canonical points elsewhere", "JOURNEY_FAILED": "User journeys that fail",
    "ACCESS_CHALLENGED": "Hosts that block QC as a bot", "H1_MISSING": "Pages without a main heading (H1)",
    "TITLE_MISSING": "Pages without a title", "DESCRIPTION_MISSING": "Pages without a meta description",
    "SLOW_SERVER": "Slow server responses", "MIXED_CONTENT": "Insecure (http) content on https pages",
}
# what to do, written for the whole group (the per-URL rule text names one address)
GROUP_FIX = {
    "BROKEN_LINK": "For each address in the attachment: restore the page, or update / remove the link on the pages listed. "
                   "If the target moved, redirect it to its new address.",
    "REDIRECTED_LINK": "For each address in the attachment: change the links on the pages listed to point straight at the "
                       "final address (column 'Goes to'), so no redirect is needed.",
    "BROKEN_IMAGE": "For each image in the attachment: restore it at its address, or correct the image address on the pages listed.",
    "LINK_CHECK": "Fix each link in the attachment as its 'Detail' column says.",
    "NEAR_DUPLICATE": "For each group in the attachment: merge the pages, make their main text different, or point the copies' "
                      "canonical at the main page.",
}
ADDRESS_RULES = {"BROKEN_LINK", "REDIRECTED_LINK", "BROKEN_IMAGE", "LINK_CHECK", "JOURNEY_FAILED", "ACCESS_CHALLENGED"}

DEFAULT_TITLE = "[{ticket_id}] {site}: {issue} - {affected}"
DEFAULT_BODY = """Ticket: {ticket_id}
Issue: {issue}
Priority: {priority}
Type: {type}
Website: {site}
Affected: {affected}
Page types: {page_types}
Status in QC: {status}
First found: {first_found} · Last seen: {last_seen} (QC check #{check})

DESCRIPTION
{issue}: {affected}.
{why}

AFFECTED ADDRESSES (first {listed} of {urls}; all of them are in the attachment)
{url_list}

STEPS TO REPRODUCE
1. Open {example_page}
2. {reproduce}
3. Repeat for the other addresses in the attachment.

EXPECTED
{expected}

ACTUAL
{issue}: {affected}.

HOW TO FIX
{fix}

HOW WE WILL CHECK
QC re-checks every page on the next check. The ticket closes by itself when none of the addresses fail.

ATTACHMENT
{attachment} - every address with its status, the number of pages it is on, example pages, and every page / address pair.

Raised from QC V6 ({link})"""

PLACEHOLDERS = {
    "ticket_id": "QC ticket number, e.g. QC-0003", "issue": "the issue type, e.g. Links that redirect (3xx)",
    "priority": "P1 to P4 from the worst severity in the group", "severity": "critical / high / medium / low",
    "type": "kind of problem (Links, Images, SEO, Speed...)", "site": "website",
    "affected": "e.g. '100 URLs on 10,000 pages'", "urls": "number of affected addresses", "pages": "number of affected pages",
    "listed": "how many addresses are written in the ticket", "url_list": "first addresses, one per line, with their status",
    "page_types": "page types involved", "status": "Open / Raised / Closed", "example_page": "one affected page",
    "reproduce": "what to look at on that page", "expected": "what should happen", "why": "why it matters", "fix": "how to fix",
    "first_found": "date first found", "last_seen": "date last seen", "check": "latest QC check number",
    "attachment": "file name of the attachment", "link": "link to the ticket in QC", "raised_as": "the Ops ticket number, once raised",
}
_PH = re.compile(r"\{([a-z_]+)\}")
_ADDR = "^(https?://|/)"


def ticket_id(n: int) -> str:
    return f"QC-{n:04d}"


def group_key_sql(alias: str = "f") -> str:
    return f"CASE WHEN {alias}.subject = '' OR {alias}.subject ~ '{_ADDR}' THEN {alias}.rule ELSE {alias}.rule || ':' || {alias}.subject END"


def humanize(code: str) -> str:
    s = code.replace("_", " ").strip().lower()
    return s[:1].upper() + s[1:]


def group_name(key: str) -> str:
    rule, _, sub = key.partition(":")
    if sub:
        return humanize(sub)
    return GROUP_NAMES.get(rule) or humanize(rule)


def kind_of(key: str, dimension: str) -> str:
    rule = key.split(":")[0]
    if "IMAGE" in rule:
        return "Images"
    if "LINK" in rule or rule in ("PAGE_NOT_FOUND", "PAGE_SERVER_ERROR"):
        return "Links"
    return {"technical": "Technical SEO", "content": "Content", "performance": "Speed", "experience": "Experience",
            "security": "Security", "links": "Links"}.get((dimension or "").lower(), (dimension or "Quality").title())


def template_of(settings: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    t = (settings or {}).get("ticket_template") or {}
    title, body = (t.get("title") or "").strip(), (t.get("body") or "").strip()
    return {"title": title or DEFAULT_TITLE, "body": body or DEFAULT_BODY, "is_default": not (title or body)}


def check_template(title: str, body: str) -> None:
    unknown = sorted({m for m in _PH.findall(f"{title}\n{body}") if m not in PLACEHOLDERS})
    if unknown:
        raise ValueError("Unknown {placeholders}: " + ", ".join("{" + u + "}" for u in unknown)
                         + ". Use the ones listed under the template.")
    if len(title) > 300 or len(body) > 20_000:
        raise ValueError("The template is too long (title up to 300, text up to 20,000 characters).")


def render(text: str, values: Dict[str, Any]) -> str:
    return _PH.sub(lambda m: str(values.get(m.group(1), m.group(0))), text)


def attachment_name(host: str, tid: str) -> str:
    return f"qc-ticket-{tid}-{re.sub(r'[^a-z0-9.-]+', '-', host.lower())}.xlsx"


def _date(ts) -> str:
    return ts.strftime("%d %b %Y") if ts else "-"


VIEWS = {"to_raise": "g.open_fixes > 0 AND it.raised_ref IS NULL", "raised": "g.open_fixes > 0 AND it.raised_ref IS NOT NULL",
         "closed": "g.open_fixes = 0", "all": "true"}

GROUPS_SQL = f"""
  WITH fx AS (SELECT f.*, {group_key_sql()} AS gkey FROM fixes f WHERE f.site_id = $1),
  g AS (SELECT gkey, COUNT(*) AS fixes, COUNT(*) FILTER (WHERE {OPEN}) AS open_fixes,
               MIN(CASE f.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END) AS sev,
               MIN(f.first_seen_run) AS first_run, MAX(f.last_seen_run) AS last_run, BOOL_OR(f.regressed) AS regressed,
               MIN(f.dimension) AS dimension, ARRAY_AGG(DISTINCT t.name) FILTER (WHERE t.name IS NOT NULL) AS page_types,
               SUM(f.pages_affected) AS pages_sum
          FROM fx f LEFT JOIN templates t ON t.id = f.template_id GROUP BY gkey)"""


async def _ensure_numbers(pool, site_id: int) -> None:
    await pool.execute(f"""INSERT INTO issue_tickets (site_id, group_key)
                           SELECT DISTINCT $1::bigint, {group_key_sql()} FROM fixes f WHERE f.site_id = $1
                           ON CONFLICT (site_id, group_key) DO NOTHING""", site_id)


async def list_for_site(pool, site: Dict[str, Any], view: str = "to_raise", q: str = "", base_link: str = "",
                        run_id: Optional[int] = None) -> Dict[str, Any]:
    """Every issue ticket of the site (or only the issues one check found), with its text from the template."""
    await _ensure_numbers(pool, site["id"])
    tpl = template_of(site.get("settings"))
    args: List[Any] = [site["id"]]
    where = VIEWS[view]
    if run_id:
        args.append(int(run_id))
        where += f""" AND EXISTS (SELECT 1 FROM fixes f2 JOIN findings fd ON fd.fix_id = f2.id
                                  WHERE f2.site_id = $1 AND {group_key_sql('f2')} = g.gkey AND fd.run_id = ${len(args)})"""
    rows = await pool.fetch(f"""{GROUPS_SQL}
        SELECT g.*, it.id AS tid, it.raised_ref, it.raised_at, r1.created_at AS first_found, r2.created_at AS last_seen
          FROM g JOIN issue_tickets it ON it.site_id = $1 AND it.group_key = g.gkey
          LEFT JOIN runs r1 ON r1.id = g.first_run LEFT JOIN runs r2 ON r2.id = g.last_run
         WHERE {where} ORDER BY (g.open_fixes = 0), g.regressed DESC, g.sev, g.pages_sum DESC, it.id""", *args)
    keys = [r["gkey"] for r in rows]
    detail = await _details(pool, site["id"], keys) if keys else {}
    tickets = []
    for r in rows:
        t = build(dict(r), site, tpl, detail.get(r["gkey"], {}), base_link)
        if q and q.lower() not in f"{t['ticket_id']} {t['issue']} {t['raised_ref'] or ''} {t['title']}".lower():
            continue
        tickets.append(t)
    counts = dict(await pool.fetchrow(f"""{GROUPS_SQL}
        SELECT {', '.join(f"COUNT(*) FILTER (WHERE {v}) AS {k}" for k, v in VIEWS.items())}
          FROM g JOIN issue_tickets it ON it.site_id = $1 AND it.group_key = g.gkey""", site["id"]))
    return {"tickets": tickets, "counts": counts, "template": tpl,
            "placeholders": [{"name": k, "means": v} for k, v in PLACEHOLDERS.items()]}


async def _details(pool, site_id: int, keys: List[str]) -> Dict[str, Dict[str, Any]]:
    """Per group: affected addresses / pages counted, the first LISTED addresses with status, and an example page.
    Open fixes only when the group still has some (a closed group shows what it had)."""
    out: Dict[str, Dict[str, Any]] = {k: {"urls": 0, "pages": 0, "list": [], "example": "", "fix": "", "why": "", "text": ""} for k in keys}
    rows = await pool.fetch(f"""
        WITH fx AS (SELECT f.*, {group_key_sql()} AS gkey,
                           BOOL_OR({OPEN}) OVER (PARTITION BY {group_key_sql()}) AS group_open
                      FROM fixes f WHERE f.site_id = $1),
        live AS (SELECT * FROM fx WHERE gkey = ANY($2::text[]) AND ({OPEN.replace('f.', 'fx.')} OR NOT group_open))
        SELECT l.gkey, COUNT(DISTINCT l.id) AS fixes,
               COUNT(DISTINCT CASE WHEN l.subject ~ '{_ADDR}' THEN l.subject END) AS urls,
               (SELECT COUNT(DISTINCT fd.page_url) FROM findings fd JOIN live l2 ON l2.id = fd.fix_id AND fd.run_id = l2.last_seen_run
                 WHERE l2.gkey = l.gkey) AS pages,
               MIN(l.example_url) AS example, (ARRAY_AGG(l.texts ORDER BY l.pages_affected DESC))[1] AS texts
          FROM live l GROUP BY l.gkey""", site_id, keys)
    for r in rows:
        d = out[r["gkey"]]
        d.update(urls=int(r["urls"] or 0), pages=int(r["pages"] or 0), example=r["example"] or "", texts=r["texts"] or {})
    lst = await pool.fetch(f"""
        SELECT gkey, subject, detail, pages, example FROM (
          SELECT {group_key_sql()} AS gkey, f.subject, f.pages_affected AS pages, f.example_url AS example,
                 (SELECT fd.evidence->>'detail' FROM findings fd WHERE fd.fix_id = f.id AND fd.run_id = f.last_seen_run LIMIT 1) AS detail,
                 ROW_NUMBER() OVER (PARTITION BY {group_key_sql()} ORDER BY f.pages_affected DESC, f.subject) AS rn
            FROM fixes f WHERE f.site_id = $1 AND {group_key_sql()} = ANY($2::text[]) AND {OPEN} AND f.subject ~ '{_ADDR}') x
         WHERE rn <= {LISTED}""", site_id, keys)
    for r in lst:
        out[r["gkey"]]["list"].append({"url": r["subject"], "detail": r["detail"] or "", "pages": r["pages"], "example": r["example"] or ""})
    return out


def build(g: Dict[str, Any], site: Dict[str, Any], tpl: Dict[str, Any], d: Dict[str, Any], base_link: str = "") -> Dict[str, Any]:
    key = g["gkey"]
    rule = key.split(":")[0]
    tid = ticket_id(g["tid"])
    sev = ("critical", "high", "medium", "low")[int(g["sev"])]
    issue = group_name(key)
    texts = d.get("texts") or {}
    rdef = RULES.get(rule)
    urls, pages = d.get("urls", 0), d.get("pages", 0)
    address = rule in ADDRESS_RULES or rule in GROUP_FIX or urls > 0
    if address and urls:
        affected = f"{urls:,} URL{'s' if urls != 1 else ''} on {pages:,} page{'s' if pages != 1 else ''}"
    else:
        affected = f"{pages:,} page{'s' if pages != 1 else ''}"
    lst = d.get("list") or []
    url_list = "\n".join(f"- {x['url']}" + (f" ({x['detail']})" if x["detail"] else "") + f" - on {x['pages']:,} pages" for x in lst)
    if not url_list:
        url_list = "- (not about single addresses: the affected pages are in the attachment)"
    example = (lst[0]["example"] if lst and lst[0]["example"] else "") or d.get("example") or site["base_url"]
    if lst:
        what = "image" if "IMAGE" in rule else "link"
        reproduce = f"Find the {what} to {lst[0]['url']} and open it" + (f": {lst[0]['detail']}" if lst[0]["detail"] else "")
    else:
        reproduce = f"Look for: {issue.lower()}"
    kind = kind_of(key, g.get("dimension") or "")
    fix = GROUP_FIX.get(rule) or texts.get("fix") or (rdef.fix if rdef and "{" not in rdef.fix else "") or "See the attachment."
    why = (rdef.why if rdef and rule not in ("PAGE_CHECK", "LINK_CHECK") else texts.get("why")) or texts.get("why") or ""
    closed = int(g["open_fixes"]) == 0
    state = "closed" if closed else "raised" if g.get("raised_ref") else "to_raise"
    att = attachment_name(site["primary_host"], tid)
    values = {
        "ticket_id": tid, "issue": issue, "priority": PRIORITY[sev], "severity": sev, "type": kind, "site": site["primary_host"],
        "affected": affected, "urls": f"{urls:,}" if urls else "-", "pages": f"{pages:,}", "listed": len(lst),
        "url_list": url_list, "page_types": ", ".join(sorted(g.get("page_types") or [])) or "-",
        "status": {"to_raise": "Open", "raised": f"Raised as {g.get('raised_ref')}", "closed": "Closed (fixed)"}[state],
        "example_page": example, "reproduce": reproduce,
        "expected": {"Links": "Every link opens a working page directly (HTTP 200, no error, no redirect).",
                     "Images": "Every image loads (HTTP 200)."}.get(kind, "Every affected page passes this check."),
        "why": why, "fix": fix, "first_found": _date(g.get("first_found")), "last_seen": _date(g.get("last_seen")),
        "check": g.get("last_run") or "-", "attachment": att, "link": f"{base_link}#/tickets?t={g['tid']}",
        "raised_as": g.get("raised_ref") or "not raised yet",
    }
    return {"id": g["tid"], "ticket_id": tid, "group_key": key, "issue": issue, "title": render(tpl["title"], values),
            "text": render(tpl["body"], values), "priority": PRIORITY[sev], "severity": sev, "type": kind,
            "urls": urls, "pages": pages, "affected": affected, "fixes": int(g["fixes"]), "state": state,
            "raised_ref": g.get("raised_ref"), "raised_at": g.get("raised_at"), "regressed": bool(g.get("regressed")),
            "attachment": att}


# ------------------------------------------------------------------ the attachment
async def attachment_rows(pool, site_id: int, group_key: str):
    """Rows for the ticket's attachment: one per affected address (or page), and every page / address pair."""
    open_only = await pool.fetchval(f"SELECT BOOL_OR({OPEN}) FROM fixes f WHERE f.site_id = $1 AND {group_key_sql()} = $2",
                                    site_id, group_key)
    cond = OPEN if open_only else "true"
    addrs = await pool.fetch(f"""
        SELECT f.id, f.subject, f.severity, f.pages_affected, f.example_url, t.name AS page_type, f.texts->>'title' AS title,
               f.texts->>'fix' AS fix_text,
               (SELECT fd.evidence->>'detail' FROM findings fd WHERE fd.fix_id = f.id AND fd.run_id = f.last_seen_run LIMIT 1) AS detail,
               (SELECT fd.evidence->>'status' FROM findings fd WHERE fd.fix_id = f.id AND fd.run_id = f.last_seen_run LIMIT 1) AS final_status,
               (SELECT fd.evidence->>'value' FROM findings fd WHERE fd.fix_id = f.id AND fd.run_id = f.last_seen_run LIMIT 1) AS value
          FROM fixes f LEFT JOIN templates t ON t.id = f.template_id
         WHERE f.site_id = $1 AND {group_key_sql()} = $2 AND {cond}
         ORDER BY f.pages_affected DESC, f.subject""", site_id, group_key)
    pairs = await pool.fetch(f"""
        SELECT fd.page_url, f.subject, fd.evidence->>'detail' AS detail, t.name AS page_type
          FROM findings fd JOIN fixes f ON f.id = fd.fix_id AND fd.run_id = f.last_seen_run
          LEFT JOIN templates t ON t.id = f.template_id
         WHERE f.site_id = $1 AND {group_key_sql()} = $2 AND {cond}
         ORDER BY f.subject, fd.page_url LIMIT 1000000""", site_id, group_key)
    return addrs, pairs


def write_attachment(ticket: Dict[str, Any], addrs, pairs) -> str:
    """The Excel file to attach to the Ops ticket: About, Affected addresses (or pages), Where found."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    wb = openpyxl.Workbook(write_only=True)
    bold, fill = Font(bold=True), PatternFill("solid", fgColor="DDF1EF")

    def head(ws, cols, widths):
        from openpyxl.cell import WriteOnlyCell
        for i, w in enumerate(widths):
            ws.column_dimensions[chr(65 + i)].width = w
        cells = []
        for c in cols:
            x = WriteOnlyCell(ws, value=c)
            x.font, x.fill = bold, fill
            cells.append(x)
        ws.append(cells)
        ws.freeze_panes = "A2"

    ws = wb.create_sheet("About")
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 110
    for k, v in (("Ticket", ticket["ticket_id"]), ("Issue", ticket["issue"]), ("Priority", ticket["priority"]),
                 ("Affected", ticket["affected"]), ("Type", ticket["type"]), ("Title", ticket["title"])):
        ws.append([k, v])
    ws.append([])
    for line in ticket["text"].split("\n"):
        ws.append(["", line])
    address = any((a["subject"] or "").startswith(("http://", "https://", "/")) for a in addrs)
    if address:
        ws = wb.create_sheet("Affected addresses")
        head(ws, ["#", "Address", "Status / detail", "Goes to", "Found on (pages)", "Example page", "Page type"], [6, 80, 30, 70, 16, 80, 22])
        for i, a in enumerate(addrs, 1):
            m = re.search(r"straight at (\S+?)\.?$", a["fix_text"] or "")
            ws.append([i, a["subject"], a["detail"] or a["value"] or "", m.group(1) if m else "", a["pages_affected"],
                       a["example_url"] or "", a["page_type"] or ""])
        ws = wb.create_sheet("Where found")
        head(ws, ["Page", "Address on it", "Status / detail"], [80, 80, 34])
        for p in pairs:
            ws.append([p["page_url"], p["subject"], p["detail"] or ""])
    else:
        ws = wb.create_sheet("Affected pages")
        head(ws, ["#", "Page", "Page type", "Detail"], [6, 90, 24, 50])
        for i, p in enumerate(pairs, 1):
            ws.append([i, p["page_url"], p["page_type"] or "", p["detail"] or ""])
    fd, path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    wb.save(path)
    return path
