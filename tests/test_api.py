"""API v2 on a real run of the fixture site."""
import asyncio
import os

os.environ["QC6_ALLOW_PRIVATE_TARGETS"] = "true"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from fixture_site import FixtureSite  # noqa: E402
from qc6 import services  # noqa: E402
from qc6.settings import settings  # noqa: E402

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
    site = FixtureSite()
    data = tmp_path_factory.mktemp("data")
    settings.data_dir = str(data)
    import asyncpg
    from qc6.db.pool import _init
    from qc6.workers.runner import execute_run

    async def seed():
        pool = await asyncpg.create_pool(migrated, min_size=1, max_size=4, init=_init)
        ws = await services.create_workspace(pool, "API Co", "lead@api.test", "Asha Rao")
        viewer = await services.add_member(pool, ws["workspace_id"], "view@api.test", "Vik Viewer", "viewer")
        dev = await services.add_member(pool, ws["workspace_id"], "dev@api.test", "Rahul Mehta", "editor")
        sid = await services.create_site(pool, ws["workspace_id"], "Fixture API", site.base, {"rate_rps": 40, "visitors": 3})
        await services.save_journey(pool, sid, JOURNEY)
        rid = await services.start_run(pool, sid, "full", {"urls": site.urls(4), "fast_visits": True, "fast_journeys": True,
                                                          "per_template": 2, "images": False})
        await execute_run(pool, rid, fast_journeys=True)
        other = await services.create_workspace(pool, "Other Co", "x@other.test", "Xavier")
        osid = await services.create_site(pool, other["workspace_id"], "Theirs", "https://other.test")
        await pool.close()
        return {"site": sid, "run": rid, "dev": dev, "viewer": viewer, "other_site": osid}
    w = asyncio.run(seed())
    from qc6.api.app import app
    with TestClient(app) as c:
        w["c"] = c
        yield w
    site.close()


H = {"X-QC-User": "lead@api.test"}


def test_today_and_sites(world):
    c = world["c"]
    t = c.get(f"/api/v2/sites/{world['site']}/today", headers=H).json()
    assert t["greeting"] == "Hello, Asha" and t["score"] is not None
    assert t["checked"].startswith("Checked 12 pages, visited") and "walked 1 journey" in t["checked"]
    kinds = [x["kind"] for x in t["cards"]]
    assert "journey" not in kinds and "unassigned" in kinds and len(t["cards"]) <= 5      # the journey passed
    s = c.get("/api/v2/sites", headers=H).json()["sites"]
    assert [x["name"] for x in s] == ["Fixture API"] and "access_token" not in s[0]


def test_fix_inbox_detail_and_changes(world):
    c = world["c"]
    data = c.get(f"/api/v2/sites/{world['site']}/fixes?view=needs", headers=H).json()
    assert data["counts"]["needs"] == len(data["fixes"]) > 0
    sev = [f["severity"] for f in data["fixes"]]
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    assert sev == sorted(sev, key=order.get)
    broken = next(f for f in data["fixes"] if f["rule"] == "BROKEN_LINK")
    d = c.get(f"/api/v2/fixes/{broken['id']}", headers=H).json()
    assert set(d["fix"]["texts"]) == {"title", "why", "fix", "verify"} and len(d["pages"]) == 12
    assert d["events"][0]["kind"] == "found"
    r = c.patch(f"/api/v2/fixes/{broken['id']}", headers=H, json={"owner_id": world["dev"], "status": "in_progress",
                                                                  "comment": "Taking this one."})
    assert r.status_code == 200 and r.json()["owner_id"] == world["dev"]
    r = c.patch(f"/api/v2/fixes/{broken['id']}", headers=H, json={"status": "verified"})
    assert r.status_code == 409 and "Only a completed check" in r.json()["detail"]
    r = c.patch(f"/api/v2/fixes/{broken['id']}", headers={"X-QC-User": "view@api.test"}, json={"status": "open"})
    assert r.status_code == 403
    q = c.get(f"/api/v2/sites/{world['site']}/fixes?view=all&q=heading", headers=H).json()["fixes"]
    assert q and all("heading" in f["title"].lower() for f in q)


def test_page_journeys_templates_access_objects(world):
    c = world["c"]
    tpl = c.get(f"/api/v2/sites/{world['site']}/templates", headers=H).json()["templates"]
    assert len(tpl) >= 3 and all(t["visits"] >= 1 for t in tpl[:3])
    url = c.get(f"/api/v2/sites/{world['site']}/pages?q=city", headers=H).json()["pages"][0]["url"]
    p = c.get(f"/api/v2/sites/{world['site']}/page", params={"url": url}, headers=H).json()
    if p["visit"]:
        assert p["visit"]["render_diff"]["h1_only_after_js"] is True
        shot = c.get(f"/api/v2/objects/{p['visit']['screenshot']}", headers=H)
        assert shot.status_code == 200 and shot.headers["content-type"] == "image/jpeg"
    assert c.get("/api/v2/objects/..%2F..%2Fetc%2Fpasswd", headers=H).status_code == 404
    j = c.get(f"/api/v2/sites/{world['site']}/journeys", headers=H).json()["journeys"]
    assert j[0]["name"] == "Apply flow" and j[0]["recent"][0]["status"] == "passed"
    steps = c.get(f"/api/v2/journey-runs/{j[0]['recent'][0]['id']}", headers=H).json()["steps"]
    assert [s["label"] for s in steps] == ["Open /apply", 'Tap "Continue"']
    bad = c.post(f"/api/v2/sites/{world['site']}/journeys", headers=H, json={"yaml": "name: x\nsteps:\n  - click: \"role=button[name='Pay now']\""})
    assert bad.status_code == 400 and "irreversible" in bad.json()["detail"]
    acc = c.get(f"/api/v2/sites/{world['site']}/access", headers=H).json()
    assert acc["header"] == "X-QC-Audit" and acc["token"] in acc["rules"]["akamai"]
    assert c.get(f"/api/v2/sites/{world['site']}/access", headers={"X-QC-User": "view@api.test"}).status_code == 403


def test_workspace_isolation_and_runs(world):
    c = world["c"]
    assert c.get(f"/api/v2/sites/{world['other_site']}", headers=H).status_code == 404
    assert c.get(f"/api/v2/sites/{world['other_site']}/fixes", headers=H).status_code == 404
    r = c.post(f"/api/v2/sites/{world['site']}/runs", headers=H, json={"kind": "coverage", "options": {"urls": ["x"]}})
    assert r.status_code == 201
    r2 = c.post(f"/api/v2/sites/{world['site']}/runs", headers=H, json={"kind": "coverage"})
    assert r2.status_code == 409
    assert c.post(f"/api/v2/runs/{r.json()['run_id']}/stop", headers=H).json()["status"] == "cancelled"
    run = c.get(f"/api/v2/runs/{world['run']}", headers=H).json()
    assert run["run"]["status"] == "completed" and run["found"]


def test_security_headers(world):
    r = world["c"].get("/api/v2/me", headers=H)
    assert r.headers["x-content-type-options"] == "nosniff" and "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"


def test_links_explorer(world):
    c = world["c"]
    r = c.get(f"/api/v2/runs/{world['run']}/facts/links?filter=broken&limit=50", headers=H).json()
    assert r["total"] == 12 and all(x["url"].endswith("/gone") for x in r["rows"])    # every page links to /gone
    assert c.get(f"/api/v2/runs/{world['run']}/facts/links?filter=bogus", headers=H).status_code == 400
    csvr = c.get(f"/api/v2/runs/{world['run']}/export/links.csv?filter=broken", headers=H)
    assert csvr.status_code == 200 and csvr.text.count("\n") == 13


def test_insights(world):
    c = world["c"]
    r = c.get(f"/api/v2/sites/{world['site']}/insights", headers=H)
    assert r.status_code == 200
    d = r.json()
    assert d["runs_considered"] == 1 and d["last_run_id"] == world["run"]
    assert len(d["score"]["points"]) == 1 and d["score"]["points"][0]["score"] is not None
    flow = d["fix_flow"]["runs"]
    assert flow[0]["new"] > 0 and flow[0]["verified"] == 0                       # first check: everything is new
    open_total = sum(s["open"] for s in d["backlog"]["by_severity"])
    assert open_total == sum(v for k, v in d["backlog"]["by_status"].items() if k in ("open", "in_progress", "fixed_pending_check"))
    assert d["page_types"]["items"] and all("open_fixes" in t for t in d["page_types"]["items"])
    assert sum(d["crawl"]["status_mix"].values()) > 0
    assert d["visits"]["total"] == sum(d["visits"]["by_status"].values()) > 0
    assert any(v["lcp"] is not None for v in d["vitals"]["items"])
    assert d["journeys"]["items"][0]["runs"] == 1
    assert d["time_to_verify"]["count"] == 0 and "no fix" in d["time_to_verify"]["basis"]
    assert all(isinstance(h, str) and h.endswith(".") for h in d["headlines"])
    assert c.get(f"/api/v2/sites/{world['other_site']}/insights", headers=H).status_code == 404   # another workspace's site


def test_presets_and_v5_settings(world):
    c = world["c"]
    p = c.get(f"/api/v2/sites/{world['site']}/presets", headers=H).json()
    assert [x["id"] for x in p["presets"]] == ["conservative", "target", "max_tested", "enterprise_150k"]
    assert p["pages"] and all(x["estimate"]["minutes"] > 0 for x in p["presets"])
    r = c.patch(f"/api/v2/sites/{world['site']}", json={"preset": "conservative", "max_pages": 5000, "read_timeout": 20, "max_retries": 1},
                headers=H)
    st = r.json()["settings"]
    assert (st["preset"], st["rate_rps"], st["max_pages"], st["read_timeout"], st["max_retries"]) == ("conservative", 8.0, 5000, 20, 1)
    assert c.patch(f"/api/v2/sites/{world['site']}", json={"preset": "warp"}, headers=H).status_code == 400
    assert c.patch(f"/api/v2/sites/{world['site']}", json={"max_pages": 0}, headers=H).status_code == 422
    assert c.patch(f"/api/v2/sites/{world['site']}", json={"preset": "target"}, headers={"X-QC-User": "view@api.test"}).status_code == 403
    c.patch(f"/api/v2/sites/{world['site']}", json={"preset": "target", "max_pages": 300000}, headers=H)   # put back for later tests


def test_site_page_type_names_the_template(world):
    """Article pages carry data-pageType="b2c-blog": V5's classifier reads it and V6 names the page type after it."""
    c = world["c"]
    names = [t["name"] for t in c.get(f"/api/v2/sites/{world['site']}/templates", headers=H).json()["templates"]]
    assert "B2C BLOG pages" in names and "Product pages" in names     # no data-pageType -> named from the URL


def test_v5_importer(world, migrated):
    """A V5 audit database (same engine, same tables) becomes a V6 check with fixes; re-import is skipped; source untouched."""
    import hashlib
    from qc6.import_v5 import import_all, list_jobs
    db = os.path.join(settings.data_dir, "runs", str(world["run"]), "coverage.db")
    before = hashlib.sha256(open(db, "rb").read()).hexdigest()
    jobs = list_jobs(db)
    assert [j["job_id"] for j in jobs] == [f"RUN{world['run']}"] and jobs[0]["status"] == "COMPLETED" and jobs[0]["issues"] > 0

    async def go():
        import asyncpg
        from qc6.db.pool import _init
        pool = await asyncpg.create_pool(migrated, min_size=1, max_size=2, init=_init)
        ws = await pool.fetchval("SELECT workspace_id FROM sites WHERE id = $1", world["site"])
        sid = await services.create_site(pool, ws, "Imported from V5", "http://localhost:1")
        first = await import_all(pool, db, sid)
        again = await import_all(pool, db, sid)
        run = await pool.fetchrow("SELECT * FROM runs WHERE id = $1", first[0]["run_id"])
        n_fix = await pool.fetchval("SELECT count(*) FROM fixes WHERE site_id = $1", sid)
        await pool.close()
        return sid, first, again, run, n_fix
    sid, first, again, run, n_fix = asyncio.run(go())
    assert run["status"] == "completed" and run["options"]["imported_from"] == "QC V5"
    assert run["summary"]["template_basis"] == "url_pattern" and run["summary"]["pages"] == jobs[0]["source_pages_total"]
    assert first[0]["fixes"] == n_fix > 0
    assert "already imported" in again[0]["skipped"]
    assert hashlib.sha256(open(db, "rb").read()).hexdigest() == before
    names = {t["name"] for t in world["c"].get(f"/api/v2/sites/{sid}/templates", headers=H).json()["templates"]}
    assert {"Product pages", "B2C BLOG pages"} <= names                  # URL pattern + the site's data-pageType label


def test_metrics(world, monkeypatch):
    c = world["c"]
    r = c.get("/metrics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    t = r.text
    for name in ("qc6_runs_queued", "qc6_oldest_queued_job_seconds", "qc6_visit_failure_ratio_24h", "qc6_host_paused",
                 "qc6_fixes_open", "qc6_disk_free_bytes", "qc6_visits_24h"):
        assert f"# TYPE {name} gauge" in t
    assert 'qc6_runs_24h{status="completed"}' in t
    monkeypatch.setenv("QC6_METRICS_TOKEN", "s3cret")
    assert c.get("/metrics").status_code == 401
    assert c.get("/metrics", headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_etag_revalidation(world):
    c = world["c"]
    r = c.get(f"/api/v2/sites/{world['site']}/insights", headers=H)
    tag = r.headers["etag"]
    assert r.headers["cache-control"] == "no-store" and r.headers["x-frame-options"] == "DENY"
    again = c.get(f"/api/v2/sites/{world['site']}/insights", headers={**H, "If-None-Match": tag})
    assert again.status_code == 304 and again.content == b""
    assert c.get(f"/api/v2/sites/{world['site']}/insights", headers={**H, "If-None-Match": '"stale"'}).status_code == 200


def test_every_write_is_audited(world, migrated):
    c = world["c"]
    sid = world["site"]
    c.post(f"/api/v2/sites/{sid}/journeys", json={"yaml": JOURNEY.replace("Apply flow", "Audited flow")}, headers=H)
    c.patch(f"/api/v2/sites/{sid}", json={"visitors": 3}, headers=H)

    async def actions():
        import asyncpg
        conn = await asyncpg.connect(migrated)
        rows = await conn.fetch("SELECT action, actor_id FROM audit_log ORDER BY id")
        await conn.close()
        return rows
    rows = asyncio.run(actions())
    acts = {r["action"] for r in rows}
    assert {"site.create", "run.start", "journey.save", "site.update", "fix.update"} <= acts
    assert all(r["actor_id"] for r in rows if r["action"] in ("journey.save", "site.update"))


def test_scope_and_staging_settings(world, migrated, monkeypatch):
    monkeypatch.setenv("QC6_SECRET_KEY", "api-test-secret")
    c, sid = world["c"], world["site"]
    r = c.patch(f"/api/v2/sites/{sid}", json={"scope": {"include": ["/product/*"], "exclude": ["*?sort=*"]},
                                              "staging": {"username": "qa", "password": "Hunter2!", "headers": {"X-Env": "staging"}}},
                headers=H)
    assert r.status_code == 200
    st = r.json()["settings"]
    assert st["scope"] == {"include": ["/product/*"], "exclude": ["*?sort=*"]}
    assert st["staging"] == {"username": "qa", "has_password": True, "header_names": ["X-Env"]}
    for path in (f"/api/v2/sites/{sid}", "/api/v2/sites", f"/api/v2/sites/{sid}/today"):
        body = c.get(path, headers=H).text
        assert "Hunter2!" not in body and "sealed" not in body and "staging\"" not in body.replace('"staging":', "")
    assert c.patch(f"/api/v2/sites/{sid}", json={"staging": {"headers": {"User-Agent": "x"}}}, headers=H).status_code == 400
    assert c.patch(f"/api/v2/sites/{sid}", json={"scope": {"exclude": ["blog"]}}, headers=H).status_code == 400

    async def audit():
        import asyncpg
        conn = await asyncpg.connect(migrated)
        rows = await conn.fetch("SELECT detail::text AS d FROM audit_log WHERE action = 'site.update'")
        await conn.close()
        return [r["d"] for r in rows]
    logs = asyncio.run(audit())
    assert any('"staging": "changed"' in d for d in logs) and not any("Hunter2!" in d for d in logs)
    c.patch(f"/api/v2/sites/{sid}", json={"scope": {"include": [], "exclude": []}, "staging": {"clear": True}}, headers=H)
    assert "staging" not in c.get(f"/api/v2/sites/{sid}", headers=H).json()["settings"]


def test_inventory_endpoints(world):
    c, sid = world["c"], world["site"]
    d = c.get(f"/api/v2/sites/{sid}/inventory?status=2xx&sort=fixes&desc=true", headers=H).json()
    assert d["run_id"] == world["run"] and d["total"] > 0 and d["checks"][0]["id"] == world["run"]
    r = c.get(f"/api/v2/sites/{sid}/inventory.csv", headers=H)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv") and "attachment" in r.headers["content-disposition"]
    assert r.text.count("\n") == c.get(f"/api/v2/sites/{sid}/inventory", headers=H).json()["total"] + 1
    assert c.get(f"/api/v2/sites/{sid}/inventory?run=999999", headers=H).status_code == 404
    assert c.get(f"/api/v2/sites/{sid}/compare?a={world['run']}&b={world['run']}", headers=H).status_code == 400
    assert c.get(f"/api/v2/sites/{world['other_site']}/inventory", headers=H).status_code == 404


def test_executive_report(world):
    c, sid = world["c"], world["site"]
    c.patch(f"/api/v2/sites/{sid}", json={"name": "Fixture <script>alert(1)</script>"}, headers=H)
    r = c.get(f"/api/v2/sites/{sid}/report.html", headers=H)
    assert r.status_code == 200 and r.headers["content-security-policy"].startswith("default-src 'none'")
    assert "<script>alert" not in r.text and "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text
    assert "The fixes that matter most" in r.text and "health out of 100" in r.text
    pdf = c.get(f"/api/v2/sites/{sid}/report.pdf", headers=H)
    assert pdf.status_code == 200 and pdf.content[:5] == b"%PDF-" and len(pdf.content) > 5000
    assert "attachment" in pdf.headers["content-disposition"]
    assert c.get(f"/api/v2/sites/{world['other_site']}/report.pdf", headers=H).status_code == 404
    assert c.get(f"/api/v2/sites/{sid}/report.docx", headers=H).status_code == 404
    c.patch(f"/api/v2/sites/{sid}", json={"name": "Fixture API"}, headers=H)
