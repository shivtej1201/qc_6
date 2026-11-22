"""The simple flow: sitemap / one URL / sheet -> URL list (download as a sheet) -> Start crawling -> report; Pause, Resume
and Cancel at any point; a check left 'pausing' by a closed worker no longer blocks the site."""
import asyncio
import io
import os
import time

os.environ["QC6_ALLOW_PRIVATE_TARGETS"] = "true"

import pytest  # noqa: E402

from fixture_site import FixtureSite  # noqa: E402
from qc6.workers.runner import execute_run, recover_stale  # noqa: E402

H = {"X-QC-Requested-With": "qc6"}


@pytest.fixture(scope="module")
def env(migrated, tmp_path_factory):
    from qc6.settings import settings
    settings.data_dir = str(tmp_path_factory.mktemp("jobs"))
    site = FixtureSite()
    import asyncpg
    from qc6.db.pool import _init

    async def wipe():
        c = await asyncpg.connect(migrated)
        await c.execute("TRUNCATE workspaces CASCADE")
        await c.close()
    asyncio.run(wipe())
    from fastapi.testclient import TestClient
    from qc6.api.app import app
    with TestClient(app) as c:
        yield c, site, migrated
    site.srv.shutdown()


def _pool(dsn):
    import asyncpg
    from qc6.db.pool import _init
    return asyncpg.create_pool(dsn, min_size=1, max_size=3, init=_init)


def _wait_list(c, lid, tries=100):
    for _ in range(tries):
        j = c.get(f"/api/v2/lists/{lid}").json()
        if j["status"] != "extracting":
            return j
        time.sleep(0.1)
    raise AssertionError("sitemap never finished")


def test_sitemap_to_report(env):
    c, site, dsn = env
    r = c.post("/api/v2/lists", json={"source": "sitemap", "url": site.base + "/sitemap.xml"}, headers=H)
    assert r.status_code == 201, r.text
    j = _wait_list(c, r.json()["id"])
    assert j["status"] == "ready" and j["url_count"] == 90 and len(j["sample"]) == 20
    assert j["primary_host"] == "127.0.0.1"                       # the site was made from the address, no setup
    sheet = c.get(f"/api/v2/lists/{j['id']}/urls.csv")
    assert sheet.status_code == 200 and sheet.text.count("\n") == 91 and "/product/0" in sheet.text
    x = c.get(f"/api/v2/lists/{j['id']}/urls.xlsx")
    import openpyxl
    ws = openpyxl.load_workbook(io.BytesIO(x.content), read_only=True).worksheets[0]
    assert sum(1 for _ in ws.iter_rows()) == 91

    rid = c.post(f"/api/v2/lists/{j['id']}/start", json={}, headers=H).json()["run_id"]
    # a second start of the same site is refused, naming the check in the way
    again = c.post(f"/api/v2/lists/{j['id']}/start", json={}, headers=H)
    assert again.status_code == 409 and again.json()["busy_run"] == rid and again.json()["busy_status"] == "queued"

    async def run():
        pool = await _pool(dsn)
        await pool.execute("UPDATE sites SET settings = settings || '{\"rate_rps\": 40}'::jsonb")
        await execute_run(pool, rid)
        await pool.close()
    asyncio.run(run())
    job = c.get(f"/api/v2/lists/{j['id']}").json()
    assert job["run_status"] == "completed" and job["run_summary"]["pages"] == 90
    rep = c.get(f"/api/v2/sites/{job['site_id']}/export.csv?run={rid}")
    assert rep.status_code == 200 and "QC site quality report" in rep.text[:100] and "High problems" in rep.text
    assert any(x["id"] == j["id"] for x in c.get("/api/v2/lists").json()["jobs"])


def test_single_url_and_sheet(env):
    c, site, dsn = env
    one = c.post("/api/v2/lists", json={"source": "single", "url": site.base + "/product/3#top"}, headers=H).json()
    assert one["status"] == "ready" and one["url_count"] == 1 and one["sample"] == [site.base + "/product/3"]
    csv_bytes = f"Page URL\n{site.base}/city/1\n{site.base}/city/2\n{site.base}/city/2\nhttps://other.test/x\n".encode()
    up = c.post("/api/v2/lists/upload", files={"file": ("pages.csv", csv_bytes, "text/csv")}, headers=H)
    assert up.status_code == 201, up.text
    up = up.json()
    assert up["url_count"] == 2 and up["note"]["duplicates"] == 1 and up["note"]["other_hosts"] == 1
    assert up["site_id"] == one["site_id"]                         # same host, same site
    bad = c.post("/api/v2/lists", json={"source": "single", "url": "not an address"}, headers=H)
    assert bad.status_code == 400
    rid = c.post(f"/api/v2/lists/{up['id']}/start", json={}, headers=H).json()["run_id"]

    async def run():
        pool = await _pool(dsn)
        await execute_run(pool, rid)
        spot = await pool.fetchval("SELECT options->'spot'->>'source' FROM runs WHERE id = $1", rid)
        await pool.close()
        return spot
    assert asyncio.run(run()) == "sheet"                           # a few pages: its own results
    assert c.delete(f"/api/v2/lists/{one['id']}", headers=H).status_code == 200


def test_pause_resume_cancel_while_running(env):
    c, site, dsn = env
    j = _wait_list(c, c.post("/api/v2/lists", json={"source": "sitemap", "url": site.base + "/sitemap.xml"}, headers=H).json()["id"])

    async def go():
        pool = await _pool(dsn)
        await pool.execute("UPDATE sites SET settings = settings || '{\"rate_rps\": 2}'::jsonb")   # slow enough to stop midway
        rid = (await asyncio.to_thread(c.post, f"/api/v2/lists/{j['id']}/start", json={}, headers=H)).json()["run_id"]
        # pause and resume before it starts
        assert (await asyncio.to_thread(c.post, f"/api/v2/runs/{rid}/pause", headers=H)).json()["status"] == "paused"
        assert (await asyncio.to_thread(c.post, f"/api/v2/runs/{rid}/resume", headers=H)).json()["status"] == "queued"
        task = asyncio.create_task(execute_run(pool, rid))
        for _ in range(100):
            if (await pool.fetchval("SELECT (progress->>'pages_done')::int FROM runs WHERE id = $1", rid) or 0) > 0:
                break
            await asyncio.sleep(0.2)
        r = await asyncio.to_thread(c.post, f"/api/v2/runs/{rid}/pause", headers=H)
        assert r.json()["status"] == "pausing"
        t0 = time.monotonic()
        await asyncio.wait_for(task, 30)
        paused_in = time.monotonic() - t0
        assert await pool.fetchval("SELECT status FROM runs WHERE id = $1", rid) == "paused"
        assert (await asyncio.to_thread(c.post, f"/api/v2/runs/{rid}/resume", headers=H)).json()["status"] == "queued"
        await pool.execute("UPDATE runs SET status = 'running' WHERE id = $1", rid)      # as a worker claiming it would
        task = asyncio.create_task(execute_run(pool, rid))
        await asyncio.sleep(2.5)
        r = await asyncio.to_thread(c.post, f"/api/v2/runs/{rid}/stop", headers=H)
        assert r.json()["status"] == "cancelled"
        t0 = time.monotonic()
        await asyncio.wait_for(task, 30)
        stopped_in = time.monotonic() - t0
        final = await pool.fetchrow("SELECT status, finished_at FROM runs WHERE id = $1", rid)
        await pool.execute("UPDATE sites SET settings = settings || '{\"rate_rps\": 40}'::jsonb")
        await pool.close()
        return final, stopped_in, paused_in
    final, stopped_in, paused_in = asyncio.run(go())
    print(f"paused in {paused_in:.1f}s, cancelled in {stopped_in:.1f}s")
    assert final["status"] == "cancelled" and final["finished_at"] is not None
    assert stopped_in < 20 and paused_in < 20


def test_stuck_pausing_check_no_longer_blocks(env):
    c, site, dsn = env

    async def go():
        pool = await _pool(dsn)
        sid = await pool.fetchval("SELECT id FROM sites ORDER BY id LIMIT 1")
        # what the user saw: run left 'pausing' by a worker window that was closed
        rid = await pool.fetchval("INSERT INTO runs (site_id, kind, status, worker_id, heartbeat_at, started_at) "
                                  "VALUES ($1, 'coverage', 'pausing', 'gone-worker', now(), now()) RETURNING id", sid)
        await pool.execute("INSERT INTO workers (id, last_seen) VALUES ('gone-worker', now() - interval '5 minutes')")
        # and one claimed before heartbeats were written at all
        rid2 = await pool.fetchval("INSERT INTO runs (site_id, kind, status, worker_id, started_at) "
                                   "VALUES ($1, 'coverage', 'running', 'old', now() - interval '10 minutes') RETURNING id", sid)
        got = {r["id"]: r["status"] for r in await recover_stale(pool)}
        await pool.close()
        return rid, rid2, got
    rid, rid2, got = asyncio.run(go())
    assert got[rid] == "paused" and got[rid2] == "queued"
    assert c.post(f"/api/v2/runs/{rid}/stop", headers=H).json()["status"] == "cancelled"
    assert c.post(f"/api/v2/runs/{rid2}/stop", headers=H).json()["status"] == "cancelled"
    w = c.get("/api/v2/worker").json()
    assert w["alive"] is False
