"""When a problem on a page was found before (the "seen before" remark on reports, the fix drawer and the page view).

For every (fix, page) of a check: the earlier checks that found the same problem on the same page, the first date it
was seen, and how many checks found it. Spot checks count too, since they looked at the page. Dates are when each
check finished.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

MAX_DATES = 6                 # dates spelled out in a remark; the count says how many in all


def _d(ts: Optional[datetime]) -> str:
    return ts.strftime("%d %b %Y") if ts else "?"


async def for_run(pool, run_id: int, fix_ids: Optional[Iterable[int]] = None,
                  pages: Optional[Iterable[str]] = None) -> Dict[Tuple[int, str], Dict[str, Any]]:
    """{(fix_id, page_url): {"dates": [earlier dates, oldest first], "first_seen": date, "checks": n}} for this check's
    findings (optionally only some fixes / pages). Pairs found for the first time are absent."""
    rows = await pool.fetch(
        """SELECT cur.fix_id, cur.page_url,
                  array_agg(coalesce(r.finished_at, r.started_at, r.created_at) ORDER BY e.run_id) AS seen
           FROM findings cur
           JOIN findings e ON e.fix_id = cur.fix_id AND e.page_url = cur.page_url AND e.run_id < cur.run_id
           JOIN runs r ON r.id = e.run_id AND r.status = 'completed'
           WHERE cur.run_id = $1 AND ($2::bigint[] IS NULL OR cur.fix_id = ANY($2::bigint[]))
             AND ($3::text[] IS NULL OR cur.page_url = ANY($3::text[]))
           GROUP BY cur.fix_id, cur.page_url""",
        run_id, list(fix_ids) if fix_ids is not None else None, list(pages) if pages is not None else None)
    out: Dict[Tuple[int, str], Dict[str, Any]] = {}
    for r in rows:
        seen = sorted({x for x in r["seen"] if x})
        if seen:
            out[(r["fix_id"], r["page_url"])] = {"dates": seen, "first_seen": seen[0], "checks": len(seen)}
    return out


def remark(h: Optional[Dict[str, Any]]) -> str:
    """'New in this check' or 'Also found on 20 Nov 2026, 21 Nov 2026 (first seen 20 Nov 2026, 3 earlier checks)'."""
    if not h:
        return "New in this check"
    days: List[str] = []
    for d in h["dates"]:
        s = _d(d)
        if s not in days:
            days.append(s)
    shown = days[-MAX_DATES:]
    more = len(days) - len(shown)
    text = "Also found on " + ", ".join(shown) + (f" and {more} earlier day{'s' if more != 1 else ''}" if more else "")
    n = h["checks"]
    return f"{text} (first seen {_d(h['first_seen'])}, {n} earlier check{'s' if n != 1 else ''})"
