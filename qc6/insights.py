"""Insights: how the site and the team are trending, from data QC already stores. Nothing here is estimated.

Every block says what it is based on (`basis`) so the UI can show "based on the last 12 checks" and never imply more
than was measured. Blocks with no data come back empty, and the UI says so in plain words.
"""
from __future__ import annotations

from statistics import median
from typing import Any, Dict, List, Optional

SEV_ORDER = ["critical", "high", "medium", "low"]


async def site_insights(pool, site_id: int, runs: int = 12) -> Dict[str, Any]:
    run_rows = await pool.fetch(
        """SELECT id, kind, status, started_at, finished_at, summary FROM runs
           WHERE site_id = $1 AND status = 'completed' AND NOT (options ? 'spot') ORDER BY id DESC LIMIT $2""", site_id, runs)
    run_rows = list(reversed(run_rows))
    run_ids = [r["id"] for r in run_rows]
    last = run_rows[-1]["id"] if run_rows else None
    out: Dict[str, Any] = {
        "runs_considered": len(run_rows),
        "last_run_id": last,
        "score": await _score(pool, site_id, run_ids),
        "fix_flow": await _fix_flow(pool, site_id, run_rows),
        "backlog": await _backlog(pool, site_id),
        "time_to_verify": await _time_to_verify(pool, site_id),
        "page_types": await _page_types(pool, site_id, last),
        "vitals": await _vitals(pool, last),
        "visits": await _visits(pool, last),
        "crawl": await _crawl(pool, last, run_rows),
        "owners": await _owners(pool, site_id),
        "journeys": await _journeys(pool, site_id),
        "regressions": await _regressions(pool, site_id),
    }
    out["headlines"] = headlines(out)
    return out


async def _score(pool, site_id: int, run_ids: List[int]) -> Dict[str, Any]:
    if not run_ids:
        return {"points": [], "basis": "no completed checks yet"}
    rows = await pool.fetch("""SELECT run_id, score, dimensions, created_at FROM score_snapshots
                               WHERE site_id = $1 AND run_id = ANY($2::bigint[]) ORDER BY run_id""", site_id, run_ids)
    pts = [{"run_id": r["run_id"], "at": r["created_at"], "score": r["score"], "dimensions": r["dimensions"]} for r in rows]
    change: Dict[str, float] = {}
    if len(pts) >= 2:
        a, b = pts[0]["dimensions"] or {}, pts[-1]["dimensions"] or {}
        change = {k: round(float(b.get(k, 0)) - float(a.get(k, 0)), 1) for k in b}
    return {"points": pts, "dimension_change": change, "basis": f"health score of the last {len(pts)} checks"}


async def _fix_flow(pool, site_id: int, run_rows) -> Dict[str, Any]:
    if not run_rows:
        return {"runs": [], "basis": "no completed checks yet"}
    ids = [r["id"] for r in run_rows]
    rows = await pool.fetch("""SELECT e.run_id, e.kind, count(*) AS n FROM fix_events e JOIN fixes f ON f.id = e.fix_id
                               WHERE f.site_id = $1 AND e.run_id = ANY($2::bigint[]) AND e.kind IN ('found', 'verified', 'regressed')
                               GROUP BY e.run_id, e.kind""", site_id, ids)
    by = {(r["run_id"], r["kind"]): r["n"] for r in rows}
    # distinct fixes after the first check in the range (that one is the starting point, not "new")
    tot = await pool.fetch("""SELECT e.kind, count(DISTINCT e.fix_id) AS n FROM fix_events e JOIN fixes f ON f.id = e.fix_id
                              WHERE f.site_id = $1 AND e.run_id = ANY($2::bigint[]) AND e.kind IN ('found', 'verified', 'regressed')
                              GROUP BY e.kind""", site_id, ids[1:])
    totals = {"new": 0, "verified": 0, "regressed": 0}
    for r in tot:
        totals["new" if r["kind"] == "found" else r["kind"]] = r["n"]
    out = [{"run_id": r["id"], "at": r["finished_at"] or r["started_at"], "new": by.get((r["id"], "found"), 0),
            "verified": by.get((r["id"], "verified"), 0), "regressed": by.get((r["id"], "regressed"), 0)} for r in run_rows]
    return {"runs": out, "totals_after_first": totals,
            "basis": f"fixes found, confirmed and back again in each of the last {len(out)} checks"}


async def _backlog(pool, site_id: int) -> Dict[str, Any]:
    rows = await pool.fetch("""SELECT severity, status, count(*) AS n, sum(pages_affected) AS pages FROM fixes
                               WHERE site_id = $1 GROUP BY severity, status""", site_id)
    open_like = ("open", "in_progress", "fixed_pending_check")
    sev = {s: {"open": 0, "pages": 0} for s in SEV_ORDER}
    status: Dict[str, int] = {}
    for r in rows:
        status[r["status"]] = status.get(r["status"], 0) + r["n"]
        if r["status"] in open_like and r["severity"] in sev:
            sev[r["severity"]]["open"] += r["n"]
            sev[r["severity"]]["pages"] += int(r["pages"] or 0)
    unowned = await pool.fetchval("SELECT count(*) FROM fixes WHERE site_id = $1 AND owner_id IS NULL AND status IN ('open', 'in_progress')",
                                  site_id)
    return {"by_severity": [{"severity": s, **sev[s]} for s in SEV_ORDER], "by_status": status, "unowned": unowned,
            "basis": "every fix on this site right now"}


async def _time_to_verify(pool, site_id: int) -> Dict[str, Any]:
    rows = await pool.fetch("""SELECT f.severity, min(e1.created_at) AS found, max(e2.created_at) AS verified
                               FROM fixes f JOIN fix_events e1 ON e1.fix_id = f.id AND e1.kind = 'found'
                               JOIN fix_events e2 ON e2.fix_id = f.id AND e2.kind = 'verified'
                               WHERE f.site_id = $1 GROUP BY f.id, f.severity""", site_id)
    days: Dict[str, List[float]] = {}
    for r in rows:
        d = max(0.0, (r["verified"] - r["found"]).total_seconds() / 86400)
        days.setdefault(r["severity"], []).append(d)
        days.setdefault("all", []).append(d)
    return {"median_days": {k: round(median(v), 1) for k, v in days.items()},
            "median_hours": {k: round(median(v) * 24, 1) for k, v in days.items()}, "count": len(rows),
            "basis": f"{len(rows)} fixes a check has confirmed" if rows else "no fix has been confirmed by a check yet"}


async def _page_types(pool, site_id: int, last: Optional[int]) -> Dict[str, Any]:
    rows = await pool.fetch(
        """SELECT t.id, t.name, t.page_count,
                  count(f.id) FILTER (WHERE f.status IN ('open', 'in_progress', 'fixed_pending_check')) AS open_fixes,
                  min(array_position(ARRAY['critical','high','medium','low'], f.severity))
                      FILTER (WHERE f.status IN ('open', 'in_progress', 'fixed_pending_check')) AS worst
           FROM templates t LEFT JOIN fixes f ON f.template_id = t.id
           WHERE t.site_id = $1 GROUP BY t.id ORDER BY t.page_count DESC LIMIT 25""", site_id)
    visits: Dict[int, Dict[str, int]] = {}
    if last:
        for r in await pool.fetch("""SELECT template_id, count(*) AS n, count(*) FILTER (WHERE status <> 'ok') AS bad
                                     FROM visits WHERE run_id = $1 GROUP BY template_id""", last):
            visits[r["template_id"]] = {"visited": r["n"], "trouble": r["bad"]}
    out = [{"template_id": r["id"], "name": r["name"], "pages": r["page_count"], "open_fixes": r["open_fixes"],
            "worst": SEV_ORDER[r["worst"] - 1] if r["worst"] else None, **visits.get(r["id"], {"visited": 0, "trouble": 0})}
           for r in rows]
    out.sort(key=lambda x: (-(x["open_fixes"] or 0), -(x["pages"] or 0)))
    return {"items": out, "basis": "page types found by the latest checks, most open fixes first"}


async def _vitals(pool, last: Optional[int]) -> Dict[str, Any]:
    if not last:
        return {"items": [], "basis": "no completed checks yet"}
    rows = await pool.fetch(
        """SELECT v.template_id, coalesce(t.name, 'Other pages') AS name, v.device, count(*) AS n,
                  percentile_cont(0.75) WITHIN GROUP (ORDER BY (v.vitals->>'LCP')::float) AS lcp,
                  percentile_cont(0.75) WITHIN GROUP (ORDER BY (v.vitals->>'CLS')::float) AS cls,
                  percentile_cont(0.75) WITHIN GROUP (ORDER BY (v.vitals->>'INP')::float) AS inp,
                  percentile_cont(0.75) WITHIN GROUP (ORDER BY (v.vitals->>'TTFB')::float) AS ttfb
           FROM visits v LEFT JOIN templates t ON t.id = v.template_id
           WHERE v.run_id = $1 AND v.status = 'ok' GROUP BY v.template_id, t.name, v.device ORDER BY lcp DESC NULLS LAST""", last)

    def rate(metric: str, v: Optional[float]) -> Optional[str]:   # Google's published thresholds
        if v is None:
            return None
        good, poor = {"lcp": (2500, 4000), "cls": (0.1, 0.25), "inp": (200, 500), "ttfb": (800, 1800)}[metric]
        return "good" if v <= good else "poor" if v > poor else "needs work"
    items = []
    for r in rows:
        m = {k: (round(r[k], 3) if k == "cls" else round(r[k])) if r[k] is not None else None for k in ("lcp", "cls", "inp", "ttfb")}
        items.append({"template_id": r["template_id"], "name": r["name"], "device": r["device"], "visits": r["n"],
                      **m, "rating": {k: rate(k, m[k]) for k in m}})
    return {"items": items, "basis": "75th percentile of real-browser visits in the latest check (lab data, not field data)"}


async def _visits(pool, last: Optional[int]) -> Dict[str, Any]:
    if not last:
        return {"by_status": {}, "reasons": [], "basis": "no completed checks yet"}
    st = {r["status"]: r["n"] for r in await pool.fetch("SELECT status, count(*) AS n FROM visits WHERE run_id = $1 GROUP BY status", last)}
    reasons = await pool.fetch("""SELECT split_part(coalesce(error, status), ':', 1) AS reason, count(*) AS n FROM visits
                                  WHERE run_id = $1 AND status <> 'ok' GROUP BY 1 ORDER BY 2 DESC LIMIT 5""", last)
    total = sum(st.values())
    return {"by_status": st, "total": total, "reasons": [{"reason": r["reason"], "visits": r["n"]} for r in reasons],
            "basis": f"{total} real-browser visits in the latest check"}


async def _crawl(pool, last: Optional[int], run_rows) -> Dict[str, Any]:
    if not last:
        return {"status_mix": {}, "pages_by_run": [], "basis": "no completed checks yet"}
    rows = await pool.fetch("""SELECT CASE WHEN http_status IS NULL THEN 'no answer' WHEN http_status < 300 THEN '2xx'
                                         WHEN http_status < 400 THEN '3xx' WHEN http_status < 500 THEN '4xx' ELSE '5xx' END AS b,
                                    count(*) AS n FROM pages WHERE run_id = $1 GROUP BY 1""", last)
    pages_by_run = [{"run_id": r["id"], "at": r["finished_at"], "pages": (r["summary"] or {}).get("pages"),
                     "minutes": round((r["finished_at"] - r["started_at"]).total_seconds() / 60, 1)
                     if r["finished_at"] and r["started_at"] else None} for r in run_rows]
    return {"status_mix": {r["b"]: r["n"] for r in rows}, "pages_by_run": pages_by_run,
            "basis": "pages checked in the latest check; size and duration of each recent check"}


async def _owners(pool, site_id: int) -> Dict[str, Any]:
    rows = await pool.fetch(
        """SELECT m.id, m.name,
                  count(f.id) FILTER (WHERE f.status IN ('open', 'in_progress')) AS open,
                  count(f.id) FILTER (WHERE f.status = 'fixed_pending_check') AS waiting,
                  count(f.id) FILTER (WHERE f.status = 'verified' AND f.updated_at > now() - interval '30 days') AS verified_30d
           FROM members m JOIN fixes f ON f.owner_id = m.id AND f.site_id = $1
           GROUP BY m.id ORDER BY open DESC, m.name""", site_id)
    return {"items": [dict(r) for r in rows], "basis": "fixes with an owner. Waiting = marked fixed, the next check will confirm it"}


async def _journeys(pool, site_id: int) -> Dict[str, Any]:
    rows = await pool.fetch(
        """SELECT j.id, j.name, count(r.id) AS runs, count(r.id) FILTER (WHERE r.status = 'passed') AS passed,
                  percentile_cont(0.5) WITHIN GROUP (ORDER BY r.duration_ms) FILTER (WHERE r.status = 'passed') AS median_ms
           FROM journeys j LEFT JOIN LATERAL (SELECT * FROM journey_runs WHERE journey_id = j.id ORDER BY id DESC LIMIT 10) r ON true
           WHERE j.site_id = $1 GROUP BY j.id ORDER BY j.name""", site_id)
    return {"items": [{"id": r["id"], "name": r["name"], "runs": r["runs"], "passed": r["passed"],
                       "median_s": round(r["median_ms"] / 1000, 1) if r["median_ms"] else None} for r in rows],
            "basis": "the last 10 runs of each journey"}


async def _regressions(pool, site_id: int) -> Dict[str, Any]:
    rows = await pool.fetch("""SELECT f.id, f.title, f.severity, f.pages_affected, max(e.created_at) AS at
                               FROM fixes f JOIN fix_events e ON e.fix_id = f.id AND e.kind = 'regressed'
                               WHERE f.site_id = $1 GROUP BY f.id ORDER BY at DESC LIMIT 6""", site_id)
    return {"items": [dict(r) for r in rows], "basis": "fixes that came back after a check had confirmed them"}


def headlines(d: Dict[str, Any]) -> List[str]:
    """Up to four plain sentences a person would say first. Only from numbers above; nothing invented."""
    out: List[str] = []
    pts = d["score"]["points"]
    if len(pts) >= 2 and pts[0]["score"] is not None and pts[-1]["score"] is not None:
        diff = round(pts[-1]["score"] - pts[0]["score"])
        if diff:
            out.append(f"Health is {'up' if diff > 0 else 'down'} {abs(diff)} points over the last {len(pts)} checks.")
        else:
            out.append(f"Health has held steady over the last {len(pts)} checks.")
        ch = d["score"].get("dimension_change") or {}
        if ch:
            worst = min(ch, key=ch.get)
            if ch[worst] <= -5:
                out.append(f"Most of the drop is in {worst}: down {abs(round(ch[worst]))} points.")
    t = d["fix_flow"].get("totals_after_first") or {}
    if len(d["fix_flow"]["runs"]) > 1:
        new, ver, back = t.get("new", 0), t.get("verified", 0), t.get("regressed", 0)
        if new or ver:
            out.append(f"Since the first of these checks, {ver} fix{'' if ver == 1 else 'es'} {'was' if ver == 1 else 'were'} confirmed "
                       f"and {new} new problem{'' if new == 1 else 's'} found."
                       + (" The list is shrinking." if ver > new + back else " New problems are arriving faster than fixes." if new > ver else ""))
        if back:
            out.append(f"{back} fix{'' if back == 1 else 'es'} came back after a check had confirmed {'it' if back == 1 else 'them'}.")
    crit = next((s for s in d["backlog"]["by_severity"] if s["severity"] == "critical"), None)
    if crit and crit["open"]:
        out.append(f"{crit['open']} critical fix{'' if crit['open'] == 1 else 'es'} open, touching {crit['pages']:,} pages.")
    elif d["backlog"]["unowned"]:
        n = d["backlog"]["unowned"]
        out.append(f"{n} open fix{'' if n == 1 else 'es'} {'has' if n == 1 else 'have'} nobody looking after {'it' if n == 1 else 'them'}.")
    return out[:4]
