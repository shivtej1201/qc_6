"""Delete checks, journeys and sites, with their stored files.

Rules:
* A check that is running (or pausing) can't be deleted: pause or stop it first.
* Deleting a check removes its pages, visits, findings, health score, journey runs and files (crawl database, link
  facts, report ZIP, screenshots no other check uses). Fixes that no remaining check found go too, unless people
  have talked about them (a comment) or they have a ticket: those stay, so a team's discussion is never lost.
* Deleting a site removes everything of that site. Only an owner can, and must type the site's name.
* The audit log keeps a line for every delete; it can't itself be deleted from the app.
Database rows go in one transaction; files are removed after it commits.
"""
from __future__ import annotations

import os
import shutil
from typing import Any, Dict, Iterable, List

from qc6.settings import settings


class Refused(Exception):
    pass


def _run_dir(run_id: int) -> str:
    return os.path.join(settings.data_dir, "runs", str(int(run_id)))


def _remove_objects(keys: Iterable[str]) -> int:
    n = 0
    base = os.path.realpath(settings.objects_dir)
    for k in keys:
        if not k:
            continue
        p = os.path.realpath(os.path.join(base, k))
        if p.startswith(base + os.sep) and os.path.isfile(p):         # never outside the objects folder
            os.unlink(p)
            n += 1
    return n


async def _orphan_screens(conn, keys: List[str]) -> List[str]:
    """Of these screenshot keys, the ones no remaining visit or journey step uses."""
    if not keys:
        return []
    used = {r["k"] for r in await conn.fetch(
        """SELECT screenshot AS k FROM visits WHERE screenshot = ANY($1::text[])
           UNION SELECT screenshot FROM step_results WHERE screenshot = ANY($1::text[])""", keys)}
    return [k for k in keys if k not in used]


async def delete_check(pool, run_id: int) -> Dict[str, Any]:
    async with pool.acquire() as conn:
        async with conn.transaction():
            run = await conn.fetchrow("SELECT id, site_id, status FROM runs WHERE id = $1 FOR UPDATE", run_id)
            if run is None:
                raise KeyError(run_id)
            if run["status"] in ("running", "pausing", "cancelling"):
                raise Refused("This check is running. Pause or stop it first, then delete it.")
            fix_ids = [r["fix_id"] for r in await conn.fetch("SELECT DISTINCT fix_id FROM findings WHERE run_id = $1", run_id)]
            shots = [r["k"] for r in await conn.fetch(
                """SELECT screenshot AS k FROM visits WHERE run_id = $1 AND screenshot IS NOT NULL
                   UNION SELECT s.screenshot FROM step_results s JOIN journey_runs j ON j.id = s.journey_run_id
                   WHERE j.run_id = $1 AND s.screenshot IS NOT NULL""", run_id)]
            journey_runs = await conn.fetchval("SELECT count(*) FROM journey_runs WHERE run_id = $1", run_id)
            await conn.execute("DELETE FROM journey_runs WHERE run_id = $1", run_id)
            pages = await conn.fetchval("SELECT count(*) FROM pages WHERE run_id = $1", run_id)
            await conn.execute("DELETE FROM runs WHERE id = $1", run_id)            # pages, visits, findings, scores cascade
            gone = await conn.fetch(
                """DELETE FROM fixes f WHERE f.id = ANY($1::bigint[])
                     AND NOT EXISTS (SELECT 1 FROM findings fd WHERE fd.fix_id = f.id)
                     AND f.ticket_url IS NULL
                     AND NOT EXISTS (SELECT 1 FROM fix_events e WHERE e.fix_id = f.id AND e.kind = 'comment')
                   RETURNING f.id""", fix_ids)
            orphans = await _orphan_screens(conn, shots)
    shutil.rmtree(_run_dir(run_id), ignore_errors=True)
    files = _remove_objects(orphans)
    _forget_caches()
    return {"check": run_id, "site_id": run["site_id"], "pages": pages, "journey_runs": journey_runs,
            "fixes_removed": len(gone), "screenshots_removed": files}


async def delete_journey(pool, journey_id: int) -> Dict[str, Any]:
    async with pool.acquire() as conn:
        async with conn.transaction():
            j = await conn.fetchrow("SELECT id, site_id, name FROM journeys WHERE id = $1 FOR UPDATE", journey_id)
            if j is None:
                raise KeyError(journey_id)
            shots = [r["k"] for r in await conn.fetch(
                """SELECT s.screenshot AS k FROM step_results s JOIN journey_runs r ON r.id = s.journey_run_id
                   WHERE r.journey_id = $1 AND s.screenshot IS NOT NULL""", journey_id)]
            runs = await conn.fetchval("SELECT count(*) FROM journey_runs WHERE journey_id = $1", journey_id)
            await conn.execute("DELETE FROM journeys WHERE id = $1", journey_id)
            await conn.execute("""DELETE FROM fixes f WHERE f.site_id = $1 AND f.rule = 'JOURNEY_FAILED' AND f.subject = $2
                                  AND f.ticket_url IS NULL AND NOT EXISTS (SELECT 1 FROM fix_events e WHERE e.fix_id = f.id AND e.kind = 'comment')""",
                               j["site_id"], j["name"])
            orphans = await _orphan_screens(conn, shots)
    return {"journey": journey_id, "name": j["name"], "site_id": j["site_id"], "runs": runs, "screenshots_removed": _remove_objects(orphans)}


async def delete_site(pool, site_id: int) -> Dict[str, Any]:
    async with pool.acquire() as conn:
        async with conn.transaction():
            s = await conn.fetchrow("SELECT id, name FROM sites WHERE id = $1 FOR UPDATE", site_id)
            if s is None:
                raise KeyError(site_id)
            if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM runs WHERE site_id = $1 AND status IN ('running', 'pausing', 'cancelling'))", site_id):
                raise Refused("A check of this site is running. Stop it first, then delete the site.")
            run_ids = [r["id"] for r in await conn.fetch("SELECT id FROM runs WHERE site_id = $1", site_id)]
            shots = [r["k"] for r in await conn.fetch(
                """SELECT v.screenshot AS k FROM visits v JOIN runs r ON r.id = v.run_id WHERE r.site_id = $1 AND v.screenshot IS NOT NULL
                   UNION SELECT s.screenshot FROM step_results s JOIN journey_runs jr ON jr.id = s.journey_run_id
                   JOIN journeys j ON j.id = jr.journey_id WHERE j.site_id = $1 AND s.screenshot IS NOT NULL""", site_id)]
            await conn.execute("DELETE FROM sites WHERE id = $1", site_id)          # everything of the site cascades
            orphans = await _orphan_screens(conn, shots)
    for rid in run_ids:
        shutil.rmtree(_run_dir(rid), ignore_errors=True)
    files = _remove_objects(orphans)
    _forget_caches()
    return {"site": site_id, "name": s["name"], "checks": len(run_ids), "screenshots_removed": files}


def _forget_caches() -> None:
    from qc6 import inventory, opportunity
    inventory._CHANGES.clear()
    opportunity._CACHE.clear()
