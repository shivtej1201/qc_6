"""Delete checks, journeys and sites, with their files; refusals; what is kept."""
import asyncio
import glob
import os

os.environ["QC6_ALLOW_PRIVATE_TARGETS"] = "true"

import pytest  # noqa: E402

from fixture_site import FixtureSite  # noqa: E402
from qc6 import services  # noqa: E402
from qc6.workers.runner import execute_run  # noqa: E402

JOURNEY = """
name: Apply flow
device: desktop
steps:
  - open: /apply
  - click: "role=button[name='Continue']"
    expect: [ { visible: "text=/You are eligible/" } ]
"""


@pytest.fixture(scope="module")
def world(migrated, tmp_path_factory):
    from qc6.settings import settings
    settings.data_dir = str(tmp_path_factory.mktemp("del"))
    site = FixtureSite()
    import asyncpg
    from qc6.db.pool import _init

    async def go():
        pool = await asyncpg.create_pool(migrated, min_size=1, max_size=4, init=_init)
        ws = await services.create_workspace(pool, "Del Co", "d@del.test", "Dev")
        await services.add_member(pool, ws["workspace_id"], "v@del.test", "Viv", "viewer")
        await services.add_member(pool, ws["workspace_id"], "ed@del.test", "Ed", "editor")
        sid = await services.create_site(pool, ws["workspace_id"], "Del site", site.base, {"rate_rps": 40, "visitors": 2})
        jid = await services.save_journey(pool, sid, JOURNEY)
        r1 = await services.start_run(pool, sid, "full", {"urls": site.urls(2) + [f"{site.base}/city/9"], "fast_visits": True,
                                                         "fast_journeys": True, "per_template": 1, "images": False})
        await execute_run(pool, r1, fast_journeys=True)
        r2 = await services.start_run(pool, sid, "coverage", {"urls": site.urls(2), "images": False})
        await execute_run(pool, r2)
        await pool.close()
        return sid, jid, r1, r2
    sid, jid, r1, r2 = asyncio.run(go())
    from fastapi.testclient import TestClient
    from qc6.api.app import app
    with TestClient(app) as c:
        yield {"c": c, "site": sid, "journey": jid, "r1": r1, "r2": r2, "dsn": migrated, "data": settings.data_dir}
    site.close()


def q(w, sql, *a):
    async def go():
        import asyncpg
        conn = await asyncpg.connect(w["dsn"])
        try:
            return await conn.fetch(sql, *a)
        finally:
            await conn.close()
    return asyncio.run(go())


H = {"X-QC-User": "d@del.test"}


def test_delete_check(world):
    w, c = world, world["c"]
    r1, r2 = w["r1"], w["r2"]
    assert os.path.isdir(os.path.join(w["data"], "runs", str(r1)))
    shots = [r["screenshot"] for r in q(w, "SELECT screenshot FROM visits WHERE run_id = $1 AND screenshot IS NOT NULL", r1)]
    assert shots and all(os.path.exists(os.path.join(w["data"], "objects", s)) for s in shots)
    only_r1 = [r["id"] for r in q(w, """SELECT f.id FROM fixes f WHERE f.site_id = $1 AND EXISTS (SELECT 1 FROM findings WHERE fix_id = f.id AND run_id = $2)
                                        AND NOT EXISTS (SELECT 1 FROM findings WHERE fix_id = f.id AND run_id = $3)""", w["site"], r1, r2)]
    both = [r["id"] for r in q(w, """SELECT f.id FROM fixes f WHERE EXISTS (SELECT 1 FROM findings WHERE fix_id = f.id AND run_id = $1)
                                       AND EXISTS (SELECT 1 FROM findings WHERE fix_id = f.id AND run_id = $2)""", r1, r2)]
    assert len(only_r1) >= 2 and both
    c.patch(f"/api/v2/fixes/{only_r1[0]}", json={"comment": "keep this, we talked about it"}, headers=H)
    q(w, "UPDATE runs SET status = 'running' WHERE id = $1", r2)
    assert c.delete(f"/api/v2/runs/{r2}", headers=H).status_code == 409                       # running: refused
    q(w, "UPDATE runs SET status = 'completed' WHERE id = $1", r2)
    assert c.delete(f"/api/v2/runs/{r1}", headers={"X-QC-User": "v@del.test"}).status_code == 403   # viewer
    r = c.delete(f"/api/v2/runs/{r1}", headers={"X-QC-User": "ed@del.test"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["pages"] > 0 and out["journey_runs"] == 1 and out["screenshots_removed"] >= len(shots)
    assert not os.path.exists(os.path.join(w["data"], "runs", str(r1)))
    assert not any(os.path.exists(os.path.join(w["data"], "objects", s)) for s in shots)
    left = {r["id"] for r in q(w, "SELECT id FROM fixes WHERE site_id = $1", w["site"])}
    assert set(both) <= left and only_r1[0] in left and not (set(only_r1[1:]) & left)    # commented one kept
    assert out["fixes_removed"] == len(only_r1) - 1
    assert not q(w, "SELECT 1 FROM runs WHERE id = $1", r1) and not q(w, "SELECT 1 FROM pages WHERE run_id = $1", r1)
    assert c.delete(f"/api/v2/runs/{r1}", headers=H).status_code == 404
    acts = [r["action"] for r in q(w, "SELECT action FROM audit_log WHERE action = 'run.delete'")]
    assert acts == ["run.delete"]


def test_delete_journey_then_site(world):
    w, c = world, world["c"]
    assert c.delete(f"/api/v2/journeys/{w['journey']}", headers=H).status_code == 200
    assert not q(w, "SELECT 1 FROM journeys WHERE id = $1", w["journey"])
    r = c.request("DELETE", f"/api/v2/sites/{w['site']}", json={"confirm": "wrong"}, headers=H)
    assert r.status_code == 400 and "Del site" in r.json()["detail"]
    assert c.request("DELETE", f"/api/v2/sites/{w['site']}", json={"confirm": "Del site"}, headers={"X-QC-User": "ed@del.test"}).status_code == 403
    r = c.request("DELETE", f"/api/v2/sites/{w['site']}", json={"confirm": "Del site"}, headers=H)
    assert r.status_code == 200 and r.json()["checks"] == 1
    assert not q(w, "SELECT 1 FROM sites WHERE id = $1", w["site"]) and not q(w, "SELECT 1 FROM fixes WHERE site_id = $1", w["site"])
    assert not os.path.exists(os.path.join(w["data"], "runs", str(w["r2"])))
