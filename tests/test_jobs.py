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

    # Data explorer: read-only SQL on the check's own crawl database
    sch = c.get(f"/api/v2/runs/{rid}/explore/schema").json()
    assert {"pages", "links", "url_status", "images", "issues"} <= {v["name"] for v in sch["views"]}
    assert "url_results" in {t["name"] for t in sch["tables"]} and sch["examples"]
    q = c.post(f"/api/v2/runs/{rid}/explore", json={"sql": "SELECT url, http_status FROM pages ORDER BY url;", "limit": 25}, headers=H).json()
    assert q["columns"] == ["url", "http_status"] and len(q["rows"]) == 25 and q["total"] == 90 and q["has_more"]
    q2 = c.post(f"/api/v2/runs/{rid}/explore", json={"sql": "SELECT url FROM pages ORDER BY url", "offset": 75, "limit": 25}, headers=H).json()
    assert len(q2["rows"]) == 15 and not q2["has_more"] and q2["total"] == 90
    for bad in ("DELETE FROM url_results", "UPDATE on_page_source_pages SET http_status = 1", "SELECT 1; DROP TABLE jobs",
                "ATTACH DATABASE '/tmp/x.db' AS x", "SELECT load_extension('x')", "SELECT * FROM pragma_table_info('jobs')",
                "SELECT * FROM no_such_table", ""):
        r = c.post(f"/api/v2/runs/{rid}/explore", json={"sql": bad or " "}, headers=H)
        assert r.status_code in (400, 422), (bad, r.text)
    assert c.post(f"/api/v2/runs/{rid}/explore", json={"sql": "SELECT COUNT(*) FROM pages"}, headers=H).json()["rows"] == [[90]]
    csv_r = c.post(f"/api/v2/runs/{rid}/explore.csv", json={"sql": "SELECT url FROM pages"}, headers=H)
    assert csv_r.status_code == 200 and csv_r.headers["x-row-count"] == "90" and csv_r.text.count("\n") == 91
    assert c.post("/api/v2/runs/999999/explore", json={"sql": "SELECT 1"}, headers=H).status_code == 404


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
        assert r.json()["status"] == "cancelling"                      # stopping; "cancelled" once the worker has stopped
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


def test_sitemap_files_saved_from_a_browser(env):
    """A sitemap behind a bot check: the person saves it from their browser and uploads it (several files at once)."""
    import gzip
    c, site, dsn = env
    a = "".join(f"<url><loc>{site.base}/product/{i}</loc></url>" for i in range(3)).encode()
    b = f"<url><loc>{site.base}/city/1?a=1&amp;b=2</loc></url>".encode()
    files = [("file", ("sitemap-1.xml", b'<?xml version="1.0"?><urlset>' + a + b + b"</urlset>", "application/xml")),
             ("file", ("sitemap-2.xml.gz", gzip.compress(b"<urlset>" + b + b"</urlset>"), "application/gzip")),
             ("file", ("index.xml", f"<sitemapindex><sitemap><loc>{site.base}/s1.xml</loc></sitemap></sitemapindex>".encode(), "text/xml"))]
    r = c.post("/api/v2/lists/upload", files=files, headers=H)
    assert r.status_code == 201, r.text
    j = r.json()
    assert j["url_count"] == 4 and j["note"]["duplicates"] == 1 and site.base + "/city/1?a=1&b=2" in j["sample"]
    assert j["label"] == "sitemap-1.xml and 1 more file" and "sitemap index" in j["skipped_files"][0]
    only_index = c.post("/api/v2/lists/upload", files=[files[2]], headers=H)
    assert only_index.status_code == 400 and f"{site.base}/s1.xml" in only_index.json()["detail"]


def test_missing_package_message(env, monkeypatch):
    c, site, dsn = env
    import qc6.workers.runner as R

    async def boom(*a, **k):
        raise ModuleNotFoundError("No module named 'numpy'", name="numpy")
    monkeypatch.setattr(R, "discover_urls", boom)
    j = _wait_list(c, c.post("/api/v2/lists", json={"source": "sitemap", "url": site.base + "/sitemap.xml"}, headers=H).json()["id"])
    assert j["status"] == "failed" and "isn't installed (numpy)" in j["error"] and "start.bat" in j["error"]


def test_timeline_minutes_retry_and_request_ids(env):
    """P2/P3 operations: the check's timeline and per-minute figures, retry of a failed check, request ids and codes."""
    c, site, dsn = env
    j = _wait_list(c, c.post("/api/v2/lists", json={"source": "sitemap", "url": site.base + "/sitemap.xml"}, headers=H).json()["id"])
    rid = c.post(f"/api/v2/lists/{j['id']}/start", json={}, headers=H).json()["run_id"]

    async def go():
        pool = await _pool(dsn)
        await pool.execute("UPDATE sites SET settings = settings || '{\"rate_rps\": 40}'::jsonb")
        await execute_run(pool, rid)
        # make it a failed check, as a crash would, then retry it
        await pool.execute("UPDATE runs SET status = 'failed', error = 'boom' WHERE id = $1", rid)
        await pool.close()
    asyncio.run(go())
    t = c.get(f"/api/v2/runs/{rid}/timeline").json()
    kinds = [e["kind"] for e in t["events"]]
    assert {"created", "started", "stage", "completed"} <= set(kinds)
    assert sum(m["pages"] for m in t["minutes"]) > 0 and all("avg_ms" in m for m in t["minutes"])
    r = c.post(f"/api/v2/runs/{rid}/retry", headers=H)
    assert r.status_code == 200 and r.json()["status"] == "queued"
    assert c.get(f"/api/v2/runs/{rid}/timeline").json()["events"][0]["kind"] == "retry"
    again = c.post(f"/api/v2/runs/{rid}/retry", headers=H)
    assert again.status_code == 409 and again.json()["code"] and again.json()["request_id"]
    assert c.post(f"/api/v2/runs/{rid}/stop", headers=H).json()["status"] == "cancelled"
    # request ids: echoed when sane, made up otherwise, and on every problem body
    ok = c.get("/api/v2/worker", headers={"X-Request-ID": "trace-12345678"})
    assert ok.headers["X-Request-ID"] == "trace-12345678"
    bad = c.get("/api/v2/runs/999999/timeline", headers={"X-Request-ID": "<script>"})
    assert bad.status_code == 404 and bad.headers["X-Request-ID"] != "<script>" and bad.json()["code"] == "CHECK_NOT_FOUND"
    assert bad.json()["request_id"] == bad.headers["X-Request-ID"]


def test_cancelling_with_a_dead_worker_becomes_cancelled_and_pruning(env):
    c, site, dsn = env
    from qc6.workers.runner import prune

    async def go():
        pool = await _pool(dsn)
        sid = await pool.fetchval("SELECT id FROM sites ORDER BY id LIMIT 1")
        rid = await pool.fetchval("INSERT INTO runs (site_id, kind, status, worker_id, heartbeat_at, started_at) "
                                  "VALUES ($1, 'coverage', 'cancelling', 'dead-w', now(), now()) RETURNING id", sid)
        await pool.execute("INSERT INTO workers (id, last_seen) VALUES ('dead-w', now() - interval '10 minutes') ON CONFLICT DO NOTHING")
        got = {r["id"]: r["status"] for r in await recover_stale(pool)}
        fin = await pool.fetchval("SELECT finished_at FROM runs WHERE id = $1", rid)
        # old figures go, recent ones stay; the check itself is never touched
        await pool.execute("INSERT INTO run_minutes (run_id, minute, pages) VALUES ($1, now() - interval '200 days', 5), ($1, now(), 7)", rid)
        await pool.execute("INSERT INTO run_events (run_id, kind, message, created_at) VALUES ($1, 'x', 'old', now() - interval '200 days')", rid)
        pruned = await prune(pool, days=90)
        left = await pool.fetchval("SELECT sum(pages) FROM run_minutes WHERE run_id = $1", rid)
        still = await pool.fetchval("SELECT count(*) FROM runs WHERE id = $1", rid)
        await pool.close()
        return rid, got, fin, pruned, left, still
    rid, got, fin, pruned, left, still = asyncio.run(go())
    assert got[rid] == "cancelled" and fin is not None
    assert pruned["minutes"] == 1 and pruned["events"] >= 1 and left == 7 and still == 1


def test_dedup_key_and_streaming_sitemap():
    from qc6.url_lists import dedup_key, dedupe
    assert dedup_key("HTTPS://Shop.example:443/p?b=2&a=1&utm_source=x&gclid=1#top") == "https://shop.example/p?a=1&b=2"
    assert dedup_key("https://shop.example/p?color=red") != dedup_key("https://shop.example/p?color=blue")
    keep, dropped = dedupe(["https://s.ex/p?a=1&b=2", "https://s.ex/p?b=2&a=1&fbclid=z", "https://s.ex/p"])
    assert keep == ["https://s.ex/p?a=1&b=2", "https://s.ex/p"] and dropped == 1
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "engine"))
    from qcv5.crawler.sitemap_extractor import SitemapExtractor
    xml = (b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:xhtml="http://www.w3.org/1999/xhtml">'
           + b"".join(b'<url><loc>https://s.ex/en/%d</loc><xhtml:link rel="alternate" hreflang="hi" href="https://s.ex/hi/%d"/></url>' % (i, i)
                      for i in range(3000)) + b"</urlset>")
    r = SitemapExtractor()._stream_parse(xml, "https://s.ex/sitemap.xml", chunk=4096)
    assert len(r["urls"]) == 3000 and r["urls"][5]["alternates"] == [{"hreflang": "hi", "href": "https://s.ex/hi/5"}]
    with pytest.raises(ValueError, match="nesting"):
        SitemapExtractor()._stream_parse(b"<urlset>" + b"<a>" * 60 + b"</a>" * 60 + b"</urlset>", "")


SAMPLE_SHEETS = ["Executive Summary", "Sitemap Progress", "Business Summary", "Language Summary", "Page Type Summary",
                 "Destination Status", "Source Page Issues", "Destination Issues", "External URLs (Unique)", "Validation Cache Reuse"]


def test_full_workbook_like_the_v5_sample(env):
    """The full workbook has the V5 sample's sheets and columns, filled from the crawl; plus QC's problems and the CSV package."""
    import openpyxl
    c, site, dsn = env
    j = _wait_list(c, c.post("/api/v2/lists", json={"source": "sitemap", "url": site.base + "/sitemap.xml"}, headers=H).json()["id"])
    assert {x["id"] for x in j["speeds"]} >= {"target", "enterprise_150k"} and all(x["minutes"] > 0 for x in j["speeds"])
    assert c.post(f"/api/v2/lists/{j['id']}/start", json={"preset": "warp"}, headers=H).status_code == 409
    rid = c.post(f"/api/v2/lists/{j['id']}/start", json={}, headers=H).json()["run_id"]

    async def go():
        pool = await _pool(dsn)
        await pool.execute("UPDATE sites SET settings = settings || '{\"rate_rps\": 40}'::jsonb")
        await execute_run(pool, rid)
        await pool.close()
    asyncio.run(go())
    r = c.get(f"/api/v2/sites/{j['site_id']}/export.workbook.xlsx?run={rid}")
    assert r.status_code == 200, r.text
    assert "qc-audit-workbook-" in r.headers["content-disposition"]
    wb = openpyxl.load_workbook(io.BytesIO(r.content), read_only=True)
    names = wb.sheetnames
    assert all(n in names for n in SAMPLE_SHEETS) and "QC Problems" in names and "QC Problem Pages" in names, names
    rows = lambda n: [list(x) for x in wb[n].iter_rows(values_only=True)]                       # noqa: E731
    ex = {x[0]: x[1] for x in rows("Executive Summary") if x and len(x) > 1}
    assert ex["Source pages total (sitemap inventory)"] == 90 and ex["Source pages completed (HTTP fetched & parsed)"] == 90
    assert ex["Crawl State"] == "COMPLETED" and ex["Check ID"] == f"#{rid}"
    sm = rows("Sitemap Progress")
    assert sm[0] == ["Sitemap #", "Sitemap URL", "Assigned Unique", "Processed", "Pending", "Completed", "Failed", "Blocked",
                     "Timed Out", "Completion %", "Relationships Extracted"]
    assert sm[1][1] == site.base + "/sitemap.xml" and sm[1][2] == 90 and sm[1][9] == 100.0      # each page knows its sitemap
    assert rows("Business Summary")[0] == ["Business", "Source Pages Processed", "Completed", "Failed", "Blocked", "Timed Out",
                                          "Relationships Extracted"]
    assert sum(x[1] for x in rows("Page Type Summary")[1:]) == 90
    dest = rows("Destination Status")
    assert dest[0] == ["Status Bucket", "Unique HTTP Destinations", "Share %"] and dest[1][0] == "2xx"
    assert rows("Destination Issues")[0][:3] == ["normalized_url", "url_scope", "validation_state"]
    # every exact status code, every unique URL and image whatever it answered, with the pages it is found on
    from qc6.workbook import IMG_HEAD, STATUS_HEAD, UNIQ_HEAD
    sc = rows("Status Codes")
    assert sc[0] == STATUS_HEAD
    assert ["Pages checked", 200, 200, "OK"] == sc[1][:4]
    gone = [x for x in sc if x[0] == "Links found" and x[1] == 404]
    assert gone and gone[0][3] == "Not Found" and gone[0][4] >= 1 and gone[0][5] >= 1
    assert any(x[0] == "Images" and x[1] == 200 for x in sc)
    uniq = rows("All URLs (Unique)")
    assert uniq[0] == UNIQ_HEAD
    col = {k: i for i, k in enumerate(UNIQ_HEAD)}
    assert uniq[1][col["first_status"]] != 200                                    # problems first
    g = next(x for x in uniq if x[0].endswith("/gone"))
    assert g[col["final_status"]] == 404 and g[col["meaning"]] == "Not Found" and g[col["found_on_pages"]] >= 1
    assert g[col["first_found_on"]].startswith(site.base) and g[col["example_found_on"]]
    assert any(x[col["first_status"]] == 200 for x in uniq[1:])                   # healthy addresses are listed too
    img = rows("Images (Unique)")
    assert img[0] == IMG_HEAD and len(img) > 1
    ic = {k: i for i, k in enumerate(IMG_HEAD)}
    assert all(x[ic["found_on_pages"]] >= 1 and x[ic["first_found_on"]] for x in img[1:])
    assert rows("Image Issues")[0][0] == "source_page_url"
    # simple sheets first, readable in a minute; CSS / JS never checked
    assert names[:6] == ["Summary", "Ops Ticket", "Broken Links", "Broken Images", "Redirects", "Pages Not OK"], names
    bl = rows("Broken Links")
    assert bl[0][:4] == ["Broken link", "Status", "Link goes to", "Found on (pages)"]
    assert any(x[0].endswith("/gone") and x[1] == "404 Not Found" and x[3] >= 1 and x[5] for x in bl[1:])
    assert rows("Ops Ticket")[0][0].startswith("Title: [Website QC]")
    assert not [x[0] for x in uniq[1:] if x[0].split("?")[0].endswith((".css", ".js"))]
    t = c.get(f"/api/v2/runs/{rid}/ops-ticket")
    assert t.status_code == 200, t.text
    t = t.json()
    assert t["qc"]["broken_links"]["unique"] >= 1 and t["qc"]["pages"]["checked"] == 90
    assert t["ticket"]["priority"] and "/gone" in t["ticket"]["text"] and "STEPS TO REPRODUCE" in t["ticket"]["text"]
    assert c.get("/api/v2/runs/999999/ops-ticket").status_code == 404
    # Management report: small, ready to present; healthy links summarised, not listed
    mr = c.get(f"/api/v2/sites/{j['site_id']}/export.report.xlsx?run={rid}")
    assert mr.status_code == 200, mr.text
    assert "qc-management-report-" in mr.headers["content-disposition"]
    assert len(mr.content) < len(r.content)
    import zipfile as _zf
    charts = [n for n in _zf.ZipFile(io.BytesIO(mr.content)).namelist() if n.startswith("xl/charts/chart")]
    assert len(charts) >= 3, charts                                           # native Excel charts on the dashboard
    mwb = openpyxl.load_workbook(io.BytesIO(mr.content), read_only=True)
    assert mwb.sheetnames[:5] == ["Dashboard", "Pivot Summary", "Non-200 Responses", "Tickets", "Problem Links"], mwb.sheetnames
    mrows = lambda n: [list(x) for x in mwb[n].iter_rows(values_only=True)]                       # noqa: E731
    dash = [str(v) for row in mrows("Dashboard") for v in row if v is not None]
    assert any(v.startswith("Website quality report") for v in dash) and "BROKEN LINKS" in dash and "Key findings" in dash
    piv = [str(row[0]) for row in mrows("Pivot Summary") if row and row[0]]
    for sec in ("A. OVERALL STATUS", "B. HTTP STATUS CODES", "C. SOURCE PAGE TYPE", "D. BUSINESS", "E. LANGUAGE",
                "F. PAGE TYPE x STATUS", "G. HIGHEST-IMPACT", "H. STATUS COLOUR LEGEND"):
        assert any(x.startswith(sec) for x in piv), sec
    n2 = mrows("Non-200 Responses")
    assert n2[0][:5] == ["Sr.no", "Destination URL", "First status", "Final status", "Status category"]
    gone_row = next(x for x in n2[1:] if x[1].endswith("/gone"))
    assert gone_row[3] == 404 and gone_row[4] == "Client error (4xx)" and gone_row[6] >= 1 and gone_row[7].startswith(site.base)
    assert all(x[4] != "OK (2xx)" for x in n2[1:])
    pl = mrows("Problem Links")
    assert len(pl) > 1 and all(x[10] != "OK (2xx)" for x in pl[1:] if x[0] is not None and isinstance(x[0], int))
    tk_rows = mrows("Tickets")
    assert tk_rows[0][:4] == ["Ticket", "Priority", "Severity", "Issue"] and any(str(x[0]).startswith("QC-") for x in tk_rows[1:])
    # Tickets tab: one ticket per issue type, every URL in its attachment
    assert "Tickets" in names
    tk = rows("Tickets")
    assert tk[0][:2] == ["Ticket", "Issue"] and tk[1][0].startswith("QC-") and "ATTACHMENT" in tk[1][7]
    sid = j["site_id"]
    lst = c.get(f"/api/v2/sites/{sid}/internal-tickets?view=all").json()
    issues = [t["issue"] for t in lst["tickets"]]
    assert len(issues) == len(set(issues)), issues                          # never two tickets for one issue type
    broken = next(t for t in lst["tickets"] if t["group_key"] == "BROKEN_LINK")
    async def fix_count():
        pool = await _pool(dsn)
        n = await pool.fetchval("SELECT count(*) FROM fixes WHERE site_id = $1 AND rule = 'BROKEN_LINK'", sid)
        await pool.close()
        return n
    assert broken["urls"] == asyncio.run(fix_count()) and broken["urls"] >= 1 and broken["pages"] >= 1
    assert broken["ticket_id"] == f"QC-{broken['id']:04d}" and broken["type"] == "Links" and "/gone" in broken["text"]
    assert broken["affected"].endswith("pages") and "URL" in broken["affected"]
    att = c.get(f"/api/v2/sites/{sid}/internal-tickets/{broken['id']}/attachment.xlsx")
    assert att.status_code == 200 and broken["attachment"] in att.headers["content-disposition"]
    awb = openpyxl.load_workbook(io.BytesIO(att.content), read_only=True)
    assert awb.sheetnames == ["About", "Affected addresses", "Where found"]
    arows = [list(x) for x in awb["Affected addresses"].iter_rows(values_only=True)]
    assert arows[0][:4] == ["#", "Address", "Status / detail", "Goes to"] and len(arows) - 1 == broken["urls"]
    assert any(r[1].endswith("/gone") for r in arows[1:])
    wrows = list(awb["Where found"].iter_rows(values_only=True))
    assert len(wrows) - 1 >= broken["urls"]
    assert c.get(f"/api/v2/sites/{sid}/internal-tickets/999999/attachment.xlsx").status_code == 404
    # the number stays the same for the issue
    assert c.get(f"/api/v2/sites/{sid}/internal-tickets?view=all").json()["tickets"][issues.index(broken["issue"])]["id"] == broken["id"]
    assert c.put(f"/api/v2/sites/{sid}/ticket-template", json={"title": "{nope}", "body": ""}, headers=H).status_code == 400
    r = c.put(f"/api/v2/sites/{sid}/ticket-template", json={"title": "OPS | {priority} | {issue}", "body": "{affected}\n{url_list}\n{attachment}"}, headers=H)
    assert r.status_code == 200 and not r.json()["is_default"]
    again = next(t for t in c.get(f"/api/v2/sites/{sid}/internal-tickets?view=all").json()["tickets"] if t["id"] == broken["id"])
    assert again["title"].startswith("OPS | P") and again["text"].startswith(broken["affected"]) and broken["attachment"] in again["text"]
    assert c.post(f"/api/v2/sites/{sid}/internal-tickets/{broken['id']}/raised", json={"ref": "OPS-1234"}, headers=H).json()["raised_ref"] == "OPS-1234"
    raised = c.get(f"/api/v2/sites/{sid}/internal-tickets?view=raised").json()
    assert [t["raised_ref"] for t in raised["tickets"]] == ["OPS-1234"] and raised["counts"]["raised"] == 1
    assert c.get(f"/api/v2/sites/{sid}/internal-tickets?view=all&q=ops-1234").json()["tickets"][0]["id"] == broken["id"]
    csv_r = c.get(f"/api/v2/sites/{sid}/internal-tickets.csv?view=raised")
    assert csv_r.status_code == 200 and "OPS-1234" in csv_r.text and broken["attachment"] in csv_r.text
    c.post(f"/api/v2/sites/{sid}/internal-tickets/{broken['id']}/raised", json={"ref": ""}, headers=H)
    assert c.get(f"/api/v2/sites/{sid}/internal-tickets?view=raised").json()["counts"]["raised"] == 0
    c.put(f"/api/v2/sites/{sid}/ticket-template", json={"title": "", "body": ""}, headers=H)
    assert c.get(f"/api/v2/sites/{sid}/internal-tickets").json()["template"]["is_default"]
    assert rows("QC Problem Pages")[0] == ["Page", "Answer", "Page type", "Problem no.", "Severity", "Problem", "Found", "Remarks"]
    assert len(rows("QC Problem Pages")) > 1 and rows("QC Problem Pages")[1][7] in ("New",) or "Seen before" in rows("QC Problem Pages")[1][7]
    pkg = c.get(f"/api/v2/runs/{rid}/package.zip")
    assert pkg.status_code == 200
    import zipfile
    names = zipfile.ZipFile(io.BytesIO(pkg.content)).namelist()
    assert any(n.endswith("03_url_audit.csv") for n in names) and any(n.endswith("07_broken_urls.csv") for n in names)


def test_sitemap_hreflang_sheet(tmp_path):
    """Sitemap hreflang alternates that don't answer 200 are listed, with a plain remark."""
    import gzip
    import json
    import sqlite3
    from qc6 import workbook
    db = tmp_path / "c.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE on_page_validation_cache (job_id, normalized_url, http_status, status_bucket)")
    con.execute("CREATE TABLE on_page_source_pages (job_id, normalized_source_url, http_status)")
    con.executemany("INSERT INTO on_page_validation_cache VALUES ('RUN1', ?, ?, ?)",
                    [("https://s.ex/hi/1", 200, "2xx"), ("https://s.ex/hi/2", 404, "4xx"), ("https://s.ex/hi/3", 301, "3xx")])
    con.commit()
    f = tmp_path / "alt.json.gz"
    with gzip.open(f, "wt") as g:
        json.dump([["https://s.ex/en/1", "hi", "https://s.ex/hi/1"], ["https://s.ex/en/2", "hi", "https://s.ex/hi/2"],
                   ["https://s.ex/en/3", "hi", "https://s.ex/hi/3"], ["https://s.ex/en/4", "hi", "https://s.ex/hi/4"]], g)
    out = list(workbook._alternates({"options": {"_alternates_file": str(f)}}, con, "RUN1"))
    assert [r[2] for r in out] == ["https://s.ex/hi/2", "https://s.ex/hi/3", "https://s.ex/hi/4"]
    assert "broken" in out[0][5] and "redirects" in out[1][5] and "not checked" in out[2][5]


def test_nofollow_links_and_html_archive(env, monkeypatch):
    """Per-link rel=nofollow is kept with every link; QC6_KEEP_HTML=true keeps each page's HTML, gzipped, with an index."""
    import csv
    import gzip
    from qc6 import facts
    from qc6.settings import settings
    c, site, dsn = env
    monkeypatch.setenv("QC6_KEEP_HTML", "true")
    one = c.post("/api/v2/lists", json={"source": "single", "url": site.base + "/product/2"}, headers=H).json()
    rid = c.post(f"/api/v2/lists/{one['id']}/start", json={}, headers=H).json()["run_id"]

    async def go():
        pool = await _pool(dsn)
        await execute_run(pool, rid)
        await pool.close()
    asyncio.run(go())
    got = facts.query(rid, "links", "", "", "", "page", False, 0, 500)
    assert got["rows"] and all("rel" in r and "nofollow" in r for r in got["rows"])
    # a page with a real nofollow link, on its own little server
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    class NF(BaseHTTPRequestHandler):
        def do_GET(self):
            body = (b"<html><head><title>Nofollow test page for QC</title></head><body><h1>Hi</h1>"
                    b'<a rel="nofollow sponsored" href="/paid">Paid</a> <a href="/free">Free</a></body></html>')
            self.send_response(200 if self.path in ("/", "/paid", "/free") else 404)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.2", 0), NF)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.2:{srv.server_address[1]}"
    lid = c.post("/api/v2/lists", json={"source": "single", "url": base + "/"}, headers=H).json()["id"]
    rid2 = c.post(f"/api/v2/lists/{lid}/start", json={}, headers=H).json()["run_id"]

    async def go2():
        pool = await _pool(dsn)
        await execute_run(pool, rid2)
        await pool.close()
    asyncio.run(go2())
    srv.shutdown()
    nf = facts.query(rid2, "links", "nofollow", "", "", "page", False, 0, 500)["rows"]
    assert [r["url"] for r in nf] == [base + "/paid"] and nf[0]["rel"] == "nofollow sponsored"
    hdir = os.path.join(settings.data_dir, "runs", str(rid), "html")
    rows = list(csv.DictReader(open(os.path.join(hdir, "index.csv"))))
    assert rows and rows[0]["url"].endswith("/product/2") and rows[0]["http_status"] == "200"
    with gzip.open(os.path.join(hdir, rows[0]["file"]), "rt") as f:
        assert "<html" in f.read().lower()


def test_clear_all_data(env):
    """Clear all data: every site, check, list, problem, ticket and their files go; accounts and the audit log stay."""
    from qc6.settings import settings
    c, site, dsn = env
    j = _wait_list(c, c.post("/api/v2/lists", json={"source": "sitemap", "url": site.base + "/sitemap.xml"}, headers=H).json()["id"])
    rid = c.post(f"/api/v2/lists/{j['id']}/start", json={}, headers=H).json()["run_id"]

    async def run_it(status=None):
        pool = await _pool(dsn)
        if status:
            await pool.execute("UPDATE runs SET status = $2 WHERE id = $1", rid, status)
        else:
            await pool.execute("UPDATE runs SET options = options || '{\"rate_rps\": 40}'::jsonb WHERE id = $1", rid)
            await execute_run(pool, rid)
        await pool.close()
    asyncio.run(run_it())
    run_dir = os.path.join(settings.data_dir, "runs", str(rid))
    assert os.path.isdir(run_dir)
    assert c.post("/api/v2/workspace/clear-data", json={"confirm": "delete"}, headers=H).status_code == 400
    asyncio.run(run_it("running"))
    r = c.post("/api/v2/workspace/clear-data", json={"confirm": "DELETE"}, headers=H)
    assert r.status_code == 409 and "running" in r.text
    asyncio.run(run_it("completed"))
    r = c.post("/api/v2/workspace/clear-data", json={"confirm": "DELETE"}, headers=H)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["sites"] >= 1 and out["checks"] >= 1 and out["url_lists"] >= 1
    assert not os.path.exists(run_dir)
    assert c.get("/api/v2/sites").json()["sites"] == [] and c.get("/api/v2/lists").json()["jobs"] == []
    assert c.get("/api/v2/me").status_code == 200                                      # still signed in

    async def left():
        pool = await _pool(dsn)
        n = {t: await pool.fetchval(f"SELECT count(*) FROM {t}") for t in ("sites", "runs", "url_lists", "fixes", "findings", "pages")}
        audit = await pool.fetchval("SELECT count(*) FROM audit_log WHERE action = 'workspace.clear_data'")
        members = await pool.fetchval("SELECT count(*) FROM members")
        await pool.close()
        return n, audit, members
    n, audit, members = asyncio.run(left())
    assert all(v == 0 for v in n.values()), n
    assert audit == 1 and members >= 1


def test_summary_email_after_check(env, monkeypatch):
    """SMTP settings (password sealed, never returned), test e-mail, and the crawl summary with the Management report
    attached, sent by itself when a check completes."""
    from smtp_sink import SmtpSink
    import zipfile
    c, site, dsn = env
    monkeypatch.setenv("QC6_SECRET_KEY", "test-secret-key-for-mail")
    sink = SmtpSink()
    try:
        form = {"enabled": True, "host": "127.0.0.1", "port": sink.port, "security": "none", "username": "qc-bot",
                "password": "s3cret!", "sender": "qc@example.test", "recipients": ["lead@example.test", "seo@example.test"],
                "attach_report": True}
        bad = c.put("/api/v2/mail", json={**form, "sender": "qc@example.test\r\nBcc: x@y.z"}, headers=H)
        assert bad.status_code == 400 and "line breaks" in bad.json()["detail"]
        assert c.put("/api/v2/mail", json={**form, "recipients": ["not-an-address"]}, headers=H).status_code == 400
        saved = c.put("/api/v2/mail", json=form, headers=H)
        assert saved.status_code == 200, saved.text
        got = c.get("/api/v2/mail").json()
        assert got["password_set"] and "password" not in got and "s3cret" not in str(got)
        assert got["recipients"] == ["lead@example.test", "seo@example.test"]

        async def sealed():
            pool = await _pool(dsn)
            v = await pool.fetchval("SELECT password_sealed FROM mail_settings")
            await pool.close()
            return v
        assert asyncio.run(sealed()).startswith("v2:") and "s3cret" not in asyncio.run(sealed())

        # keep the saved password when the form leaves it empty; a wrong one is refused with a plain message
        t = c.post("/api/v2/mail/test", json={**form, "password": None}, headers=H)
        assert t.status_code == 200, t.text
        assert sink.messages[-1]["msg"]["Subject"] == "[QC] Test e-mail" and sink.messages[-1]["auth"] == ("qc-bot", "s3cret!")
        wrong = c.post("/api/v2/mail/test", json={**form, "password": "nope"}, headers=H)
        assert wrong.status_code == 502 and "username or password" in wrong.json()["detail"]

        # a check finishes -> the summary arrives by itself, report attached
        j = _wait_list(c, c.post("/api/v2/lists", json={"source": "sitemap", "url": site.base + "/sitemap.xml"}, headers=H).json()["id"])
        rid = c.post(f"/api/v2/lists/{j['id']}/start", json={}, headers=H).json()["run_id"]
        n0 = len(sink.messages)

        async def run():
            pool = await _pool(dsn)
            await pool.execute("UPDATE sites SET settings = settings || '{\"rate_rps\": 40}'::jsonb")
            await execute_run(pool, rid)
            await pool.close()
        asyncio.run(run())
        assert len(sink.messages) == n0 + 1
        m = sink.messages[-1]
        assert m["to"] == ["lead@example.test", "seo@example.test"] and m["from"] == "qc@example.test"
        msg = m["msg"]
        assert f"check {rid}" in msg["Subject"] and "broken links" in msg["Subject"]
        text = msg.get_body(preferencelist=("plain",)).get_content()
        for want in ("Crawl job ID", "Sitemap / source", "Total URLs submitted", "URLs crawled", "Successfully processed",
                     "Failed", "200 OK", "3xx", "4xx", "5xx", "DNS failures", "Timeout failures", "Redirects",
                     "Duplicate URLs skipped", "Total extracted URLs", "Total unique URLs", "Total images extracted",
                     "Started", "Finished", "Total processing time", "Report generation", "Ticket processing",
                     "Generated and attached", "sitemap.xml"):
            assert want in text, want
        assert "Total URLs submitted: 90" in text
        atts = list(msg.iter_attachments())
        assert len(atts) == 1 and atts[0].get_filename().endswith(f"check{rid}.xlsx")
        z = zipfile.ZipFile(io.BytesIO(atts[0].get_content()))
        assert "xl/workbook.xml" in z.namelist() and z.testzip() is None
        assert c.get("/api/v2/mail").json()["last_result"]["ok"] is True

        # send again by hand; e-mail off -> nothing after the next check
        again = c.post(f"/api/v2/runs/{rid}/send-summary", headers=H)
        assert again.status_code == 200 and again.json()["ok"] and len(sink.messages) == n0 + 2
        c.put("/api/v2/mail", json={**form, "enabled": False, "password": None}, headers=H)
        async def hook():
            pool = await _pool(dsn)
            from qc6 import mailer
            await mailer.after_run(pool, rid, dict(await pool.fetchrow("SELECT * FROM sites LIMIT 1")))
            await pool.close()
        asyncio.run(hook())
        assert len(sink.messages) == n0 + 2
    finally:
        sink.close()


def test_mail_report_verification(tmp_path):
    from qc6 import mailer
    import zipfile
    assert mailer.verify_xlsx(None)[0] is False
    assert mailer.verify_xlsx(str(tmp_path / "missing.xlsx"))[0] is False
    (tmp_path / "empty.xlsx").write_bytes(b"")
    assert mailer.verify_xlsx(str(tmp_path / "empty.xlsx")) == (False, "the report file is empty")
    (tmp_path / "half.xlsx").write_bytes(b"PK\x03\x04 cut short")
    assert "complete" in mailer.verify_xlsx(str(tmp_path / "half.xlsx"))[1]
    with zipfile.ZipFile(tmp_path / "ok.xlsx", "w") as z:
        z.writestr("xl/workbook.xml", "<workbook/>")
    assert mailer.verify_xlsx(str(tmp_path / "ok.xlsx")) == (True, "")
    assert mailer._fmt(125000) == "1,25,000" and mailer._fmt(999) == "999"
