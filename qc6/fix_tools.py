"""Fix tools from the CrawlPulse 3 gap check:

* recheck(): "Check this fix now" — a spot check of just the pages the fix was last found on. It confirms the fix only
  when every one of those pages was re-checked and the problem is gone (the spot-check rules in workers/analysis.py).
* impact(): the Search Console figures of the fix's pages before and after it was verified, over 14, 28 and 60 days.
  Correlation, not proof: other changes, seasons and Google updates move the same numbers.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from qc6 import services

MAX_RECHECK_PAGES = 1000
WINDOWS = (14, 28, 60)
GSC_LAG_DAYS = 3                       # Search Console figures settle about 2-3 days after the day


async def fix_pages(pool, fix_id: int, limit: int = MAX_RECHECK_PAGES) -> List[str]:
    """Pages the fix was found on in the latest check that saw it."""
    rid = await pool.fetchval("SELECT max(run_id) FROM findings WHERE fix_id = $1", fix_id)
    if rid is None:
        return []
    return [r["page_url"] for r in await pool.fetch(
        "SELECT DISTINCT page_url FROM findings WHERE fix_id = $1 AND run_id = $2 ORDER BY page_url LIMIT $3", fix_id, rid, limit)]


async def recheck(pool, fix: Dict[str, Any], actor_id: Optional[int]) -> Dict[str, Any]:
    pages = await fix_pages(pool, fix["id"])
    if not pages:
        raise services.NotAllowed("This fix has no pages to check.")
    if fix["source"] == "journey":
        raise services.NotAllowed("Journey problems are checked by running the journey again (Journeys, Run now).")
    kind = "full" if fix["source"] == "visit" else "coverage"            # browser-found problems need the browser
    n = len(pages)
    opts: Dict[str, Any] = {"urls": pages, "images": kind == "full",
                            "spot": {"label": f"Fix #{fix['id']}: {n} page{'s' if n != 1 else ''}", "source": "fix",
                                     "fix_id": fix["id"], "count": n}}
    rid = await services.start_run(pool, fix["site_id"], kind, opts, actor_id)
    await pool.execute("INSERT INTO fix_events (fix_id, run_id, kind, actor_id, detail) VALUES ($1, $2, 'recheck', $3, $4)",
                       fix["id"], rid, actor_id, {"pages": n})
    return {"run_id": rid, "pages": n, "kind": kind}


async def impact(pool, fix: Dict[str, Any], today: Optional[date] = None) -> Dict[str, Any]:
    """Before / after figures per window for the fix's pages, around the day the fix was verified."""
    verified = await pool.fetchval("SELECT max(created_at) FROM fix_events WHERE fix_id = $1 AND kind = 'verified'", fix["id"])
    if verified is None:
        return {"state": "not_verified", "windows": [],
                "text": "Impact is measured from the day a check confirms the fix. This fix isn't confirmed yet."}
    pages = await fix_pages(pool, fix["id"], 5000)
    has_data = await pool.fetchval("SELECT EXISTS (SELECT 1 FROM search_page_days WHERE site_id = $1)", fix["site_id"])
    if not has_data:
        return {"state": "no_search_console", "verified_at": verified.isoformat(), "windows": [],
                "text": "Connect Search Console on the Growth screen and sync; impact needs day-by-day figures."}
    pivot = verified.astimezone(timezone.utc).date()
    latest = await pool.fetchval("SELECT max(day) FROM search_page_days WHERE site_id = $1", fix["site_id"])
    usable_until = min(latest, (today or date.today()) - timedelta(days=GSC_LAG_DAYS))
    out = []
    for w in WINDOWS:
        before = await _window(pool, fix["site_id"], pages, pivot - timedelta(days=w), pivot - timedelta(days=1))
        after_end = pivot + timedelta(days=w - 1)
        have_days = max(0, (min(after_end, usable_until) - pivot).days + 1)
        if have_days < w:
            out.append({"days": w, "ready": False, "days_so_far": have_days, "before": before,
                        "text": f"{have_days} of {w} days after the fix have settled figures so far."})
            continue
        after = await _window(pool, fix["site_id"], pages, pivot, after_end)
        out.append({"days": w, "ready": True, "before": before, "after": after, "change": _change(before, after)})
    return {"state": "ok", "verified_at": verified.isoformat(), "pages": len(pages), "windows": out,
            "text": "Same pages, same number of days before and after the fix was confirmed. Other changes, seasons and "
                    "Google updates move these numbers too: read it as a signal, not proof."}


async def _window(pool, site_id: int, pages: List[str], start: date, end: date) -> Dict[str, Any]:
    r = await pool.fetchrow("""SELECT COALESCE(sum(clicks), 0) AS clicks, COALESCE(sum(impressions), 0) AS impressions,
                                      CASE WHEN sum(impressions) > 0 THEN sum(position * impressions) / sum(impressions) END AS position
                               FROM search_page_days WHERE site_id = $1 AND page = ANY($2::text[]) AND day BETWEEN $3 AND $4""",
                            site_id, pages, start, end)
    clicks, imps = int(r["clicks"]), int(r["impressions"])
    return {"from": start.isoformat(), "to": end.isoformat(), "clicks": clicks, "impressions": imps,
            "ctr": round(clicks / imps, 4) if imps else None,
            "position": round(float(r["position"]), 1) if r["position"] is not None else None}


def _change(b: Dict[str, Any], a: Dict[str, Any]) -> Dict[str, Any]:
    def pct(x, y):
        return None if not x else round(100.0 * (y - x) / x, 1)            # no "infinite %" from a zero before
    return {"clicks": a["clicks"] - b["clicks"], "clicks_pct": pct(b["clicks"], a["clicks"]),
            "impressions": a["impressions"] - b["impressions"], "impressions_pct": pct(b["impressions"], a["impressions"]),
            "position": None if a["position"] is None or b["position"] is None else round(a["position"] - b["position"], 1)}
