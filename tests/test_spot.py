"""Single URL / pasted list / uploaded sheet checks ("spot checks") and why they don't move site-wide figures."""
import asyncio
import io
import os

os.environ["QC6_ALLOW_PRIVATE_TARGETS"] = "true"

import pytest  # noqa: E402

from fixture_site import FixtureSite  # noqa: E402
from qc6 import services  # noqa: E402
from qc6 import url_lists as L  # noqa: E402
from qc6.workers.analysis import close_run  # noqa: E402
from qc6.workers.runner import execute_run  # noqa: E402

SITE = {"primary_host": "www.example.com"}


def test_parse_csv_text_and_xlsx():
    csv_bytes = b"Title,Page URL,Notes\nHome,https://www.example.com/,x\nLoan,https://www.example.com/loan?a=1,b#frag\n,not a url,\n"
    assert L.from_upload(csv_bytes, "list.csv") == ["https://www.example.com/", "https://www.example.com/loan?a=1"]
    text = "https://www.example.com/a?x=1,2  https://www.example.com/b#top,\nhttps://other.test/c"
    assert L.from_text(text) == ["https://www.example.com/a?x=1,2", "https://www.example.com/b", "https://other.test/c"]
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["name", "url"])
    ws.append(["a", "https://WWW.example.com/A"])
    ws.append(["b", "https://www.example.com/A"])
    ws.append(["c", "https://blog.example.com/post"])
    buf = io.BytesIO()
    wb.save(buf)
    urls = L.from_upload(buf.getvalue(), "list.xlsx")
    got = L.for_site(urls, SITE)
    assert got["urls"] == ["https://www.example.com/A", "https://blog.example.com/post"] and got["duplicates"] == 1
    with pytest.raises(L.ListError, match="xls"):
        L.from_upload(b"junk", "old.xls")
    with pytest.raises(L.ListError, match="None of the addresses"):
        L.for_site(["https://other.test/x"], SITE)
    assert L.for_site(["https://www.example.com/x", "https://evil.test/x"], SITE)["other_hosts"] == 1


@pytest.fixture(scope="module")
def site_srv():
    s = FixtureSite()
    yield s
    s.close()


def test_spot_check_leaves_site_figures_alone(pg_pool, site_srv, tmp_path, monkeypatch):
    from qc6.settings import settings
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))

    async def go():
        pool = await pg_pool()
        ws = await services.create_workspace(pool, "Spot Co", "s@spot.test", "Sona")
        sid = await services.create_site(pool, ws["workspace_id"], "Spot", site_srv.base, {"rate_rps": 40})
        full = await services.start_run(pool, sid, "coverage", {"urls": site_srv.urls(3), "images": False})
        await execute_run(pool, full)
        before = {r["id"]: dict(r) for r in await pool.fetch("SELECT id, status, pages_affected, last_seen_run, title FROM fixes WHERE site_id = $1", sid)}
        snaps = await pool.fetchval("SELECT count(*) FROM score_snapshots WHERE site_id = $1", sid)
        tpl = {r["id"]: r["page_count"] for r in await pool.fetch("SELECT id, page_count FROM templates WHERE site_id = $1", sid)}
        one = f"{site_srv.base}/product/0"
        spot = await services.start_run(pool, sid, "coverage", {"urls": [one], "images": False, "spot": {"label": one, "source": "single"}})
        await execute_run(pool, spot)
        after = {r["id"]: dict(r) for r in await pool.fetch("SELECT id, status, pages_affected, last_seen_run, title FROM fixes WHERE site_id = $1", sid)}
        out = {
            "status": await pool.fetchval("SELECT status FROM runs WHERE id = $1", spot),
            "spot_findings": await pool.fetchval("SELECT count(*) FROM findings WHERE run_id = $1", spot),
            "spot_cross": await pool.fetchval("""SELECT count(*) FROM findings fd JOIN fixes f ON f.id = fd.fix_id WHERE fd.run_id = $1
                                                 AND (f.subject IN ('ORPHAN_PAGE', 'NOT_IN_SITEMAP') OR f.rule = 'NEAR_DUPLICATE')""", spot),
            "snaps": (snaps, await pool.fetchval("SELECT count(*) FROM score_snapshots WHERE site_id = $1", sid)),
            "tpl": (tpl, {r["id"]: r["page_count"] for r in await pool.fetch("SELECT id, page_count FROM templates WHERE site_id = $1", sid)}),
        }
        from qc6.inventory import checks_with_pages, list_pages, previous_check
        checks = await checks_with_pages(pool, sid)
        out["checks"] = [(c["id"], bool(c["spot"])) for c in checks]
        out["prev_of_spot"] = await previous_check(pool, sid, spot)
        out["spot_pages"] = (await list_pages(pool, sid, spot, {}))["total"]
        await pool.close()
        return full, spot, before, after, out
    full, spot, before, after, out = asyncio.run(go())
    assert out["status"] == "completed" and out["spot_findings"] > 0
    assert out["spot_cross"] == 0                            # no "orphan page" from a one-page check
    assert before == after                                   # same titles, page counts, last-seen check, statuses
    assert out["snaps"][0] == out["snaps"][1]                # no health score from a spot check
    assert out["tpl"][0] == out["tpl"][1]                    # page types keep their size
    assert out["checks"][0] == (spot, True) and (full, False) in out["checks"]
    assert out["prev_of_spot"] is None and out["spot_pages"] == 1


def test_spot_check_confirms_only_fully_covered_fixes(pg_pool):
    async def go():
        pool = await pg_pool()
        ws = await services.create_workspace(pool, "Cover Co", "c@cover.test", "Cyrus")
        sid = await services.create_site(pool, ws["workspace_id"], "Cover", "https://cover.test")
        a = await services.start_run(pool, sid, "coverage", {})
        b = await pool.fetchval("""INSERT INTO runs (site_id, kind, status, options) VALUES ($1, 'coverage', 'running', '{"spot": {"label": "x"}}')
                                   RETURNING id""", sid)
        mk = lambda key: pool.fetchval("""INSERT INTO fixes (site_id, fix_key, rule, severity, dimension, scope, title, status, pages_affected,
                 first_seen_run, last_seen_run, source, texts) VALUES ($1,$2,'H1_MISSING','medium','content','page','t','fixed_pending_check',1,$3,$3,'coverage','{}')
                 RETURNING id""", sid, key, a)
        f1, f2 = await mk("k1"), await mk("k2")
        await pool.execute("INSERT INTO findings (run_id, fix_id, page_url, source) VALUES ($1,$2,'https://cover.test/x','coverage'), ($1,$3,'https://cover.test/y','coverage')",
                           a, f1, f2)
        moved = await close_run(pool, b, sid, ["coverage"], complete=True, pages={"https://cover.test/x"})
        st = {r["id"]: r["status"] for r in await pool.fetch("SELECT id, status FROM fixes WHERE site_id = $1", sid)}
        await pool.close()
        return f1, f2, st, moved
    f1, f2, st, moved = asyncio.run(go())
    assert st[f1] == "verified" and st[f2] == "fixed_pending_check" and moved == {"verified": 1}


def test_api_single_and_upload(pg_pool, site_srv, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from qc6.api.app import app
    from qc6.settings import settings
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))

    async def seed():
        pool = await pg_pool()
        ws = await services.create_workspace(pool, "Up Co", "u@up.test", "Uma")
        sid = await services.create_site(pool, ws["workspace_id"], "Up", site_srv.base.replace("127.0.0.1", "localhost"))
        await pool.close()
        return sid
    sid = asyncio.run(seed())
    base = site_srv.base.replace("127.0.0.1", "localhost")
    H = {"X-QC-User": "u@up.test"}
    with TestClient(app) as c:
        r = c.post(f"/api/v2/sites/{sid}/checks/urls", json={"text": f"{base}/product/1"}, headers=H)
        assert r.status_code == 201 and r.json()["urls"] == 1 and r.json()["spot"] and r.json()["label"] == f"{base}/product/1"
        c.post(f"/api/v2/runs/{r.json()['run_id']}/stop", headers=H)              # queued -> cancelled, so the next can start
        files = {"file": ("pages.csv", f"url\n{base}/a\n{base}/b\nhttps://elsewhere.test/c\n".encode(), "text/csv")}
        r = c.post(f"/api/v2/sites/{sid}/checks/upload", files=files, data={"kind": "full", "whole_site": "false"}, headers=H)
        body = r.json()
        assert r.status_code == 201 and body["urls"] == 2 and body["other_hosts"] == 1 and body["label"] == "2 pages from pages.csv"
        c.post(f"/api/v2/runs/{body['run_id']}/stop", headers=H)
        assert c.post(f"/api/v2/sites/{sid}/checks/urls", json={"text": "https://elsewhere.test/x"}, headers=H).status_code == 400
        assert c.post(f"/api/v2/sites/{sid}/runs", json={"kind": "coverage", "options": {"urls": ["https://elsewhere.test/x"]}}, headers=H).status_code == 400
        runs = c.get(f"/api/v2/sites/{sid}/runs", headers=H).json()["runs"]
        assert runs[0]["spot"]["file"] == "pages.csv"
