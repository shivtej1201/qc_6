"""Search Console keyword intelligence and the opportunity score, against a fake Google (no network)."""
import asyncio
import json
import os

os.environ["QC6_ALLOW_PRIVATE_TARGETS"] = "true"

import httpx  # noqa: E402
import pytest  # noqa: E402

from fixture_site import FixtureSite  # noqa: E402
from qc6 import ga4, opportunity, search_console as sc, services  # noqa: E402
from qc6.workers.runner import execute_run  # noqa: E402


def _service_account(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    p = tmp_path / "sa.json"
    p.write_text(json.dumps({"type": "service_account", "client_email": "qc@test.iam.gserviceaccount.com", "private_key": pem,
                             "token_uri": "https://oauth2.googleapis.com/token"}))
    return str(p)


@pytest.fixture(scope="module")
def world(migrated, tmp_path_factory):
    from qc6.settings import settings
    tmp = tmp_path_factory.mktemp("growth")
    settings.data_dir = str(tmp)
    site = FixtureSite()
    os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"] = _service_account(tmp)
    b = site.base
    rows = [  # query, page, clicks, impressions, ctr, position
        ("personal loan", f"{b}/product/0", 300, 12000, 0.025, 7.2),
        ("personal loan", f"{b}/product/1", 20, 4000, 0.005, 12.5),
        ("emi calculator", f"{b}/article/0", 90, 9000, 0.01, 2.1),
        ("loan in city", f"{b}/city/0", 5, 300, 0.0167, 30.0),
    ]
    calls = {"gsc": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "oauth2.googleapis.com":
            assert b"jwt-bearer" in req.content
            return httpx.Response(200, json={"access_token": "ya29.test", "expires_in": 3600})
        assert req.headers["authorization"] == "Bearer ya29.test"
        if req.url.host == "searchconsole.googleapis.com":
            body = json.loads(req.content)
            if body["dimensions"] == ["page", "date"]:          # day-by-day figures per page (fix impact)
                calls["daily"] = calls.get("daily", 0) + 1
                assert body["startRow"] == 0
                day = body["endDate"]
                return httpx.Response(200, json={"rows": [{"keys": [p, day], "clicks": c, "impressions": i, "position": pos}
                                                          for q, p, c, i, ctr, pos in rows[:2]]})
            calls["gsc"] += 1
            assert body["dimensions"] == ["query", "page"]
            start = body["startRow"]
            page = rows[start:start + body["rowLimit"]]
            return httpx.Response(200, json={"rows": [{"keys": [q, p], "clicks": c, "impressions": i, "ctr": ctr, "position": pos}
                                                      for q, p, c, i, ctr, pos in page]})
        if req.url.host == "analyticsdata.googleapis.com":
            body = json.loads(req.content)
            if body["dimensions"][0]["name"] == "landingPage":
                return httpx.Response(200, json={"rows": [{"dimensionValues": [{"value": "/product/0"}], "metricValues": [{"value": "5400"}]},
                                                          {"dimensionValues": [{"value": "/article/0"}], "metricValues": [{"value": "800"}]}]})
            return httpx.Response(200, json={"rows": [{"dimensionValues": [{"value": "mobile"}], "metricValues": [{"value": "70"}]},
                                                      {"dimensionValues": [{"value": "desktop"}], "metricValues": [{"value": "30"}]}]})
        return httpx.Response(404)
    t = httpx.MockTransport(handler)
    sc.set_transport(t)
    ga4.set_transport(t)
    import asyncpg
    from qc6.db.pool import _init

    async def seed():
        pool = await asyncpg.create_pool(migrated, min_size=1, max_size=4, init=_init)
        ws = await services.create_workspace(pool, "Growth Co", "g@growth.test", "Gita")
        sid = await services.create_site(pool, ws["workspace_id"], "Growth", site.base, {"rate_rps": 40})
        rid = await services.start_run(pool, sid, "coverage", {"urls": site.urls(3), "images": False})
        await execute_run(pool, rid)
        return pool, sid, rid
    loop = asyncio.new_event_loop()
    pool, sid, rid = loop.run_until_complete(seed())
    yield {"loop": loop, "pool": pool, "site": sid, "run": rid, "base": b, "calls": calls}
    sc.set_transport(None)
    ga4.set_transport(None)
    os.environ.pop("GOOGLE_SERVICE_ACCOUNT_FILE", None)
    loop.run_until_complete(pool.close())
    loop.close()
    site.close()


def _site(w):
    return dict(w["loop"].run_until_complete(w["pool"].fetchrow("SELECT * FROM sites WHERE id = $1", w["site"])))


def test_without_search_data_the_score_says_so(world):
    w = world
    d = w["loop"].run_until_complete(opportunity.page_opportunities(w["pool"], w["site"], w["run"]))
    assert d["data"] == {"search_console": False, "ga4": False, "links": True}
    assert d["confidence"] == 0.45 and len(d["pages"]) == 9
    assert all(p["clicks"] is None and p["bucket"] not in ("growth", "ctr", "consolidate") for p in d["pages"])
    assert "Not a Google ranking score" in d["basis"]


def test_sync_pages_through_results_and_needs_setup(world, monkeypatch):
    w = world
    with pytest.raises(RuntimeError, match="isn't set up"):
        w["loop"].run_until_complete(sc.sync(w["pool"], _site(w)))
    w["loop"].run_until_complete(w["pool"].execute(
        "UPDATE sites SET settings = settings || '{\"gsc_property\": \"sc-domain:example.test\", \"ga4_property_id\": \"123\"}' WHERE id = $1", w["site"]))
    monkeypatch.setattr(sc, "PAGE_SIZE", 3)                     # 4 rows -> two pages of results
    out = w["loop"].run_until_complete(sc.sync(w["pool"], _site(w)))
    assert out["rows"] == 4 and out["ga4_pages"] == 2 and out["truncated"] is False and w["calls"]["gsc"] == 2
    daily = w["loop"].run_until_complete(w["pool"].fetch("SELECT page, clicks FROM search_page_days WHERE site_id = $1 ORDER BY page", w["site"]))
    assert w["calls"]["daily"] == 1 and len(daily) == 2 and sum(r["clicks"] for r in daily) > 0        # day-by-day figures kept


def test_striking_distance_and_cannibalisation(world):
    w = world
    st = w["loop"].run_until_complete(sc.striking_distance(w["pool"], w["site"]))
    assert [(r["page"].rsplit("/", 2)[-2:], r["impressions"]) for r in st] == [(["product", "0"], 12000), (["product", "1"], 4000)]
    assert st[0]["clicks_at_position_3"] == int(12000 * 0.11) and 0 < st[0]["ctr_gap"] <= 1
    can = w["loop"].run_until_complete(sc.cannibalisation(w["pool"], w["site"]))
    assert len(can) == 1 and can[0]["query"] == "personal loan" and can[0]["pages_top20"] == 2
    assert [p["position"] for p in can[0]["pages"]] == [7.2, 12.5]


def test_opportunities_with_search_data(world):
    w = world
    d = w["loop"].run_until_complete(opportunity.page_opportunities(w["pool"], w["site"], w["run"]))
    assert d["data"]["search_console"] and d["data"]["ga4"] and d["confidence"] == 1.0
    by = {p["url"][len(w["base"]):]: p for p in d["pages"]}
    p0 = by["/product/0"]
    assert p0["bucket"] in ("critical_fix", "growth") and p0["sessions"] == 5400
    assert any('"personal loan"' in r and "12,000 impressions" in r for r in p0["reasons"])
    a0 = by["/article/0"]          # traffic + a high-severity fix open: the spec puts "critical fix" before "CTR"
    assert a0["bucket"] == "critical_fix" and any("high fix open" in r for r in a0["reasons"])
    assert any("usually gets" in r for r in a0["reasons"])        # the CTR gap is still explained
    assert d["pages"][0]["url"].endswith("/product/0")               # most to gain comes first
    assert sum(b["pages"] for b in d["buckets"]) == 9
    assert by["/product/0"]["score"] > by["/product/2"]["score"]


def test_api(world):
    from fastapi.testclient import TestClient
    from qc6.api.app import app
    w = world
    with TestClient(app) as c:
        H = {"X-QC-User": "g@growth.test"}
        r = c.get(f"/api/v2/sites/{w['site']}/growth/opportunities?bucket=critical_fix", headers=H).json()
        assert r["total"] >= 1 and all(p["bucket"] == "critical_fix" for p in r["pages"])
        assert any(p["url"].endswith("/article/0") for p in r["pages"])
        s = c.get(f"/api/v2/sites/{w['site']}/growth/search", headers=H).json()
        assert s["config"]["ready"] and s["last_sync"]["gsc_rows"] == 4 and len(s["striking"]) == 2
        assert c.post(f"/api/v2/sites/{w['site']}/growth/sync", headers=H).json()["rows"] == 4
        assert c.patch(f"/api/v2/sites/{w['site']}", json={"gsc_property": "example.com"}, headers=H).status_code == 400
        assert c.patch(f"/api/v2/sites/{w['site']}", json={"ga4_property_id": "G-123"}, headers=H).status_code == 400
        assert c.patch(f"/api/v2/sites/{w['site']}", json={"gsc_property": "https://www.example.test/"}, headers=H).status_code == 200
