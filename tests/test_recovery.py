"""A check whose worker dies is picked up again and finishes; one that keeps dying is stopped with a reason."""
import asyncio
import os

os.environ["QC6_ALLOW_PRIVATE_TARGETS"] = "true"

import pytest  # noqa: E402

from fixture_site import FixtureSite  # noqa: E402
from qc6 import services  # noqa: E402
from qc6.workers.runner import CLAIM_SQL, execute_run, recover_stale  # noqa: E402


@pytest.fixture(scope="module")
def site_srv():
    s = FixtureSite()
    yield s
    s.close()


def test_crashed_check_is_requeued_and_finishes(pg_pool, site_srv, tmp_path, monkeypatch):
    from qc6.settings import settings
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))

    async def go():
        pool = await pg_pool()
        ws = await services.create_workspace(pool, "Crash Co", "c@crash.test", "Chandra")
        sid = await services.create_site(pool, ws["workspace_id"], "Crash", site_srv.base, {"rate_rps": 6, "visitors": 2})
        urls = site_srv.urls(per_template=10)
        rid = await services.start_run(pool, sid, "coverage", {"urls": urls, "images": False})
        assert await pool.fetchval(CLAIM_SQL, "w-doomed") == rid
        task = asyncio.create_task(execute_run(pool, rid))
        for _ in range(150):
            await asyncio.sleep(0.2)
            if 3 <= int((await pool.fetchval("SELECT progress FROM runs WHERE id = $1", rid) or {}).get("pages_done", 0)) < 25:
                break
        task.cancel()                                                     # the worker dies mid-crawl
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        assert await pool.fetchval("SELECT status FROM runs WHERE id = $1", rid) == "running"
        assert await pool.fetchval(CLAIM_SQL, "w-other") is None          # the site is still blocked ...
        assert await recover_stale(pool) == []                            # ... and a fresh heartbeat is left alone
        await pool.execute("UPDATE runs SET heartbeat_at = now() - interval '5 minutes' WHERE id = $1", rid)
        assert await recover_stale(pool) == [{"id": rid, "status": "queued"}]
        assert await pool.fetchval(CLAIM_SQL, "w-other") == rid           # another worker takes it
        await execute_run(pool, rid)
        r = await pool.fetchrow("SELECT status, attempts, summary FROM runs WHERE id = $1", rid)
        pages = await pool.fetchval("SELECT count(*) FROM pages WHERE run_id = $1", rid)
        await pool.close()
        return r, pages, len(urls)
    r, pages, n = asyncio.run(go())
    assert r["status"] == "completed" and r["attempts"] == 2
    assert pages == n == r["summary"]["pages"]                            # every page once, none lost or doubled


def test_check_that_keeps_dying_is_stopped_and_pausing_becomes_paused(pg_pool):
    async def go():
        pool = await pg_pool()
        ws = await services.create_workspace(pool, "Loop Co", "l@loop.test", "Lata")
        s1 = await services.create_site(pool, ws["workspace_id"], "A", "https://a.example")
        s2 = await services.create_site(pool, ws["workspace_id"], "B", "https://b.example")
        r1 = await services.start_run(pool, s1, "coverage", {})
        r2 = await services.start_run(pool, s2, "coverage", {})
        await pool.execute("""UPDATE runs SET status = 'running', attempts = 3, heartbeat_at = now() - interval '1 hour' WHERE id = $1""", r1)
        await pool.execute("""UPDATE runs SET status = 'pausing', attempts = 1, heartbeat_at = now() - interval '1 hour' WHERE id = $1""", r2)
        got = sorted((x["id"], x["status"]) for x in await recover_stale(pool))
        err = await pool.fetchval("SELECT error FROM runs WHERE id = $1", r1)
        await pool.close()
        return got, err, r1, r2
    got, err, r1, r2 = asyncio.run(go())
    assert got == sorted([(r1, "failed"), (r2, "paused")])
    assert err.startswith("The worker running this check stopped 3 times")
