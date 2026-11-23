"""Internal tickets: one per issue QC finds, always, written from the site's ticket template.

Every fix is a ticket named QC-<number>. Its text is the template with the issue filled in (what is wrong, priority,
affected pages and addresses, how to fix, how to check). People copy it into the Ops team's tool and record the number
they got there ("Mark raised"). The template is per site and editable; {placeholders} are listed in PLACEHOLDERS.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

PRIORITY = {"critical": "P1 - Critical", "high": "P2 - High", "medium": "P3 - Medium", "low": "P4 - Low"}
STATUS_TEXT = {"open": "Open", "in_progress": "In progress", "fixed_pending_check": "Fixed, waiting for QC to confirm",
               "verified": "Verified fixed", "wont_fix": "Won't fix"}
PAGES_LISTED = 20

DEFAULT_TITLE = "[{ticket_id}] {site}: {title} ({pages} pages)"
DEFAULT_BODY = """Ticket: {ticket_id}
Title: {title}
Priority: {priority}
Type: {type}
Website: {site}
Page type: {page_type}
Status in QC: {status}
First found: {first_found} · Last seen: {last_seen} (QC check #{check})

DESCRIPTION
{title}. {pages} page(s) affected.
{why}

AFFECTED ADDRESSES
{addresses}

AFFECTED PAGES ({pages}, first {pages_listed} shown)
{page_list}

STEPS TO REPRODUCE
1. Open {example_page}
2. {reproduce}

EXPECTED
{expected}

ACTUAL
{title}.

HOW TO FIX
{fix}

HOW WE WILL CHECK
{verify}
QC re-checks these pages on the next check and closes the ticket by itself when they pass.

Raised from QC V6 ({link})"""

PLACEHOLDERS = {
    "ticket_id": "QC ticket number, e.g. QC-0042", "title": "what is wrong, in one line", "priority": "P1 to P4 from the severity",
    "severity": "critical / high / medium / low", "type": "kind of problem (links, images, SEO, speed...)",
    "site": "website", "page_type": "page type, or 'several page types'", "status": "status in QC",
    "pages": "number of pages affected", "pages_listed": "how many pages are listed", "page_list": "affected pages, one per line",
    "addresses": "the broken / redirecting addresses themselves, when the issue is about links or images",
    "example_page": "one affected page", "reproduce": "what to look at on that page", "expected": "what should happen",
    "why": "why it matters", "fix": "how to fix", "verify": "how to confirm the fix",
    "first_found": "date first found", "last_seen": "date last seen", "check": "QC check number", "link": "link to the issue in QC",
    "raised_as": "the Ops ticket number, once raised",
}
_PH = re.compile(r"\{([a-z_]+)\}")


def ticket_id(fix_id: int) -> str:
    return f"QC-{fix_id:04d}"


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


def _kind(dimension: str, rule: str) -> str:
    r = rule.upper()
    if "IMAGE" in r or "IMG" in r:
        return "Images"
    if "LINK" in r or "REDIRECT" in r or "BROKEN" in r or "404" in r:
        return "Links"
    return {"seo": "SEO", "tech_seo": "Technical SEO", "performance": "Speed", "speed": "Speed", "accessibility": "Accessibility",
            "a11y": "Accessibility", "content": "Content", "security": "Security"}.get((dimension or "").lower(), (dimension or "Quality").title())


def _date(ts) -> str:
    return ts.strftime("%d %b %Y") if ts else "-"


def build(f: Dict[str, Any], site: Dict[str, Any], tpl: Dict[str, Any], pages: List[str], subjects: List[str],
          first_found=None, last_seen=None, link: str = "") -> Dict[str, Any]:
    t = f.get("texts") or {}
    title = t.get("title") or f["title"]
    kind = _kind(f.get("dimension") or "", f.get("rule") or "")
    subjects = [s for s in subjects if s]
    address_like = [s for s in subjects if s.startswith(("http://", "https://", "/"))]
    if address_like:
        what = "image" if kind == "Images" else "link"
        reproduce = f"Find the {what} to {address_like[0]} and open it"
    else:
        reproduce = "Look for: " + title.rstrip(".")
    values = {
        "ticket_id": ticket_id(f["id"]), "title": title, "priority": PRIORITY.get(f["severity"], f["severity"]),
        "severity": f["severity"], "type": kind, "site": site["primary_host"],
        "page_type": f.get("template_name") or ("one page" if f.get("scope") == "page" else "several page types"),
        "status": STATUS_TEXT.get(f["status"], f["status"]), "pages": f"{int(f.get('pages_affected') or 0):,}",
        "pages_listed": min(len(pages), PAGES_LISTED),
        "page_list": "\n".join(f"- {p}" for p in pages[:PAGES_LISTED]) or "- (see the issue in QC)",
        "addresses": "\n".join(f"- {s}" for s in address_like[:PAGES_LISTED]) or "- (not about one address: see the pages below)",
        "example_page": f.get("example_url") or (pages[0] if pages else site["base_url"]),
        "reproduce": reproduce,
        "expected": {"Links": "Every link opens a working page directly (HTTP 200, no error, no redirect).",
                     "Images": "Every image loads (HTTP 200)."}.get(kind, "Every affected page passes this check."),
        "why": t.get("why") or "", "fix": t.get("fix") or "", "verify": t.get("verify") or "",
        "first_found": _date(first_found), "last_seen": _date(last_seen), "check": f.get("last_seen_run") or "-",
        "link": link, "raised_as": f.get("raised_ref") or "not raised yet",
    }
    raised = bool(f.get("raised_ref"))
    closed = f["status"] in ("verified", "wont_fix")
    return {"id": f["id"], "ticket_id": values["ticket_id"], "fix_id": f["id"],
            "title": render(tpl["title"], values), "text": render(tpl["body"], values), "issue": title,
            "address": address_like[0] if address_like else "",
            "priority": values["priority"], "severity": f["severity"], "type": kind, "pages": int(f.get("pages_affected") or 0),
            "qc_status": f["status"], "qc_status_text": values["status"],
            "state": "closed" if closed else "raised" if raised else "to_raise",
            "raised_ref": f.get("raised_ref"), "raised_at": f.get("raised_at"), "regressed": bool(f.get("regressed"))}


VIEWS = {"to_raise": "f.status NOT IN ('verified','wont_fix') AND f.raised_ref IS NULL",
         "raised": "f.status NOT IN ('verified','wont_fix') AND f.raised_ref IS NOT NULL",
         "closed": "f.status IN ('verified','wont_fix')", "all": "true"}
SEV_ORDER = "CASE f.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END"


async def list_for_site(pool, site: Dict[str, Any], view: str = "to_raise", q: str = "", limit: int = 200,
                        offset: int = 0, base_link: str = "", run_id: Optional[int] = None) -> Dict[str, Any]:
    tpl = template_of(site.get("settings"))
    args: List[Any] = [site["id"]]
    where = f"f.site_id = $1 AND {VIEWS[view]}"
    if run_id:                                    # the issues one check found (its report's Tickets sheet)
        args.append(int(run_id))
        where += f" AND EXISTS (SELECT 1 FROM findings fd WHERE fd.fix_id = f.id AND fd.run_id = ${len(args)})"
    if q:
        args.append(f"%{q.lower()}%")
        n = len(args)
        where += f" AND (lower(f.title) LIKE ${n} OR lower(coalesce(f.raised_ref,'')) LIKE ${n} OR ('qc-' || lpad(f.id::text, 4, '0')) LIKE ${n})"
    rows = await pool.fetch(f"""
        SELECT f.*, t.name AS template_name, r1.created_at AS first_found, r2.created_at AS last_seen
          FROM fixes f LEFT JOIN templates t ON t.id = f.template_id
          LEFT JOIN runs r1 ON r1.id = f.first_seen_run LEFT JOIN runs r2 ON r2.id = f.last_seen_run
         WHERE {where} ORDER BY f.regressed DESC, {SEV_ORDER}, f.pages_affected DESC, f.id
         LIMIT {int(limit)} OFFSET {int(offset)}""", *args)
    pages: Dict[int, List[str]] = {}
    subjects: Dict[int, List[str]] = {}
    if rows:
        for r in await pool.fetch(f"""
            SELECT fix_id, page_url, subject FROM (
              SELECT fd.fix_id, fd.page_url, fd.subject,
                     ROW_NUMBER() OVER (PARTITION BY fd.fix_id ORDER BY fd.page_url) AS rn
                FROM findings fd JOIN fixes f ON f.id = fd.fix_id AND fd.run_id = f.last_seen_run
               WHERE fd.fix_id = ANY($1::bigint[])) x WHERE rn <= {PAGES_LISTED}""", [r["id"] for r in rows]):
            pages.setdefault(r["fix_id"], [])
            if r["page_url"] not in pages[r["fix_id"]]:
                pages[r["fix_id"]].append(r["page_url"])
            subjects.setdefault(r["fix_id"], [])
            if r["subject"] and r["subject"] not in subjects[r["fix_id"]]:
                subjects[r["fix_id"]].append(r["subject"])
    tickets = [build(dict(r), site, tpl, pages.get(r["id"], []), subjects.get(r["id"], []), r["first_found"], r["last_seen"],
                     f"{base_link}#/tickets?t={r['id']}") for r in rows]
    counts = dict(await pool.fetchrow(f"""SELECT {', '.join(f"COUNT(*) FILTER (WHERE {v}) AS {k}" for k, v in VIEWS.items())}
                                          FROM fixes f WHERE f.site_id = $1""", site["id"]))
    return {"tickets": tickets, "counts": counts, "template": tpl,
            "placeholders": [{"name": k, "means": v} for k, v in PLACEHOLDERS.items()]}
