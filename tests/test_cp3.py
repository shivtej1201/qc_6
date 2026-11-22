"""Gaps found against the CrawlPulse 3 code: robots.txt Crawl-delay, the missing page rules, and (further down) the
fix / opportunity / brief / anchor / HTML-viewer features."""
import asyncio
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ["QC6_ALLOW_PRIVATE_TARGETS"] = "true"

import pytest  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "engine"))
from qc6 import robots_delay  # noqa: E402

H = {"X-QC-Requested-With": "qc6"}


# ------------------------------------------------------------------ 1. Crawl-delay
def test_crawl_delay_parsing():
    p = robots_delay.parse
    assert p("User-agent: *\nCrawl-delay: 1.5\n") == 1.5                              # decimals count
    assert p("User-agent: QC-Audit\nCrawl-delay: 4\n\nUser-agent: *\nCrawl-delay: 1\n") == 4.0   # our own group wins
    assert p("User-agent: qc-audit\nAllow: /\n\nUser-agent: *\nCrawl-delay: 7\n") is None       # our group, no delay
    assert p("User-agent: Googlebot\nCrawl-delay: 9\n\nUser-agent: *\nDisallow: /x\n") is None  # someone else's
    assert p("User-agent: a\nUser-agent: *\nCrawl-delay: 2 # seconds\n") == 2.0               # shared group, comment
    for bad in ("User-agent: *\nCrawl-delay: 0", "User-agent: *\nCrawl-delay: abc", "User-agent: *\nCrawl-delay: 99999", ""):
        assert p(bad) is None
    assert robots_delay.paced_rate(12, 2.0) == 0.5 and robots_delay.paced_rate(12, None) == 12


class _Polite(BaseHTTPRequestHandler):
    hits: list = []

    def do_GET(self):
        if self.path == "/robots.txt":
            body, ctype = b"User-agent: *\nCrawl-delay: 1.2\n", "text/plain"
        else:
            _Polite.hits.append((time.monotonic(), self.path))
            n = self.path.strip("/") or "0"
            body = (f"<html><head><meta charset='utf-8'><title>Polite page {n} of the test site</title></head><body><h1>Page {n}</h1>"
                    + "<p>" + "text " * 60 + "</p></body></html>").encode()
            ctype = "text/html"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_crawl_delay_is_respected_by_the_crawl(migrated, tmp_path, monkeypatch):
    from qc6 import services
    from qc6.settings import settings
    from qc6.workers.runner import execute_run
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    srv = ThreadingHTTPServer(("127.0.0.3", 0), _Polite)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.3:{srv.server_address[1]}"
    _Polite.hits = []

    async def go():
        import asyncpg
        from qc6.db.pool import _init
        pool = await asyncpg.create_pool(migrated, min_size=1, max_size=4, init=_init)
        ws = await services.create_workspace(pool, "Polite Co", "p@polite.test", "Pia")
        sid = await services.create_site(pool, ws["workspace_id"], "P", base, {"rate_rps": 40})
        rid = await services.start_run(pool, sid, "coverage", {"urls": [f"{base}/{i}" for i in range(1, 5)], "images": False})
        await execute_run(pool, rid)
        ev = await pool.fetchval("SELECT message FROM run_events WHERE run_id = $1 AND kind = 'crawl_delay'", rid)
        st = await pool.fetchval("SELECT status FROM runs WHERE id = $1", rid)
        await pool.close()
        return ev, st
    ev, st = asyncio.run(go())
    srv.shutdown()
    assert st == "completed" and ev and "1.2 s between requests" in ev
    times = sorted(t for t, _ in _Polite.hits)
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert len(times) >= 4 and min(gaps) >= 1.1, gaps            # 40 requests a second asked, 1 per 1.2 s given


# ------------------------------------------------------------------ 2. page rules
def _issues(html, ct="text/html", xr="", url="https://a.com/p"):
    from qcv5.audit.seo_audit import page_facts, page_issues
    from qcv5.crawler.on_page_audit_engine import extract_on_page_urls
    _, rels, parser = extract_on_page_urls(url, url, html)
    f = page_facts("J", url, url, parser, rels, xr, "now", ct)
    f["source_page_url"] = url
    return {c for _, c, _, _ in page_issues(f, {url})}


GOOD = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><title>A good page title for testing</title>'
        '<link rel="canonical" href="https://a.com/p"><script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"Product","name":"x"}</script></head><body><h1>Hi</h1><p>'
        + "word " * 80 + "</p></body></html>")
NEW = {"CHARSET_MISSING", "STRUCTURED_DATA_NO_CONTEXT", "STRUCTURED_DATA_NO_TYPE", "HTML_EMPTY", "HTML_STRUCTURE_INCOMPLETE",
       "INDEX_SIGNALS_CONFLICT", "CANONICAL_CROSS_DOMAIN"}


@pytest.mark.parametrize("html,kw,expect", [
    (GOOD, {}, set()),
    (GOOD.replace('<meta charset="utf-8">', ""), {"ct": "text/html; charset=utf-8"}, set()),
    (GOOD.replace('<meta charset="utf-8">', ""), {}, {"CHARSET_MISSING"}),
    (GOOD.replace('"@context":"https://schema.org",', "").replace('"@type":"Product",', ""), {},
     {"STRUCTURED_DATA_NO_CONTEXT", "STRUCTURED_DATA_NO_TYPE"}),
    (GOOD.replace('{"@context":"https://schema.org","@type":"Product","name":"x"}',
                  '{"@context":"https://schema.org","@graph":[{"@type":"A"},{"name":"b"}]}'), {}, {"STRUCTURED_DATA_NO_TYPE"}),
    ('<html><head><meta charset="utf-8"></head><body></body></html>', {}, {"HTML_EMPTY"}),
    ('<meta charset="utf-8"><title>A good page title for testing</title><h1>Hi</h1><p>' + "word " * 80, {},
     {"HTML_STRUCTURE_INCOMPLETE"}),
    (GOOD.replace("<title>", '<meta name="robots" content="noindex"><title>'), {}, {"INDEX_SIGNALS_CONFLICT"}),
    (GOOD.replace("<title>", '<meta name="robots" content="index,follow"><title>'), {"xr": "noindex"}, {"INDEX_SIGNALS_CONFLICT"}),
    (GOOD.replace('https://a.com/p"', 'https://other.com/p"'), {}, {"CANONICAL_CROSS_DOMAIN"}),
    (GOOD.replace('https://a.com/p"', 'https://www.a.com/p"'), {}, set()),               # www vs bare: same site
])
def test_new_page_rules(html, kw, expect):
    assert _issues(html, **kw) & NEW == expect


# ------------------------------------------------------------------ 3-6. fix now, impact, opportunity review
class _Shop(BaseHTTPRequestHandler):
    fixed = False

    def do_GET(self):
        host = self.headers.get("Host")
        if self.path == "/robots.txt":
            body, ct = b"User-agent: *\nAllow: /\n", "text/plain"
        elif self.path == "/sitemap.xml":
            body = ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    + "".join(f"<url><loc>http://{host}/p/{i}</loc></url>" for i in range(3)) + "</urlset>").encode()
            ct = "application/xml"
        else:
            desc = '<meta name="description" content="A perfectly fine description of this page, long enough for search results.">' \
                if _Shop.fixed else ""
            body = (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
                    f"<title>Shop page {self.path} with a decent title</title>{desc}</head><body><h1>Shop {self.path}</h1>"
                    + "".join(f"<a href='/p/{i}'>Page {i}</a> " for i in range(3)) + "<p>" + "words " * 120 + "</p></body></html>").encode()
            ct = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def shop(migrated, tmp_path_factory):
    from qc6.settings import settings
    settings.data_dir = str(tmp_path_factory.mktemp("cp3"))
    import asyncpg

    async def wipe():
        c = await asyncpg.connect(migrated)
        await c.execute("TRUNCATE workspaces CASCADE")
        await c.close()
    asyncio.run(wipe())
    srv = ThreadingHTTPServer(("127.0.0.4", 0), _Shop)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    from fastapi.testclient import TestClient
    from qc6.api.app import app
    with TestClient(app) as c:
        yield c, f"http://127.0.0.4:{srv.server_address[1]}", migrated
    srv.shutdown()


def _run(dsn, rid):
    from qc6.workers.runner import execute_run

    async def go():
        import asyncpg
        from qc6.db.pool import _init
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3, init=_init)
        await pool.execute("UPDATE sites SET settings = settings || '{\"rate_rps\": 40}'::jsonb")
        await execute_run(pool, rid)
        await pool.close()
    asyncio.run(go())


def test_check_this_fix_now_verifies_it(shop):
    c, base, dsn = shop
    _Shop.fixed = False
    lid = c.post("/api/v2/lists", json={"source": "sitemap", "url": base + "/sitemap.xml"}, headers=H).json()["id"]
    for _ in range(100):
        j = c.get(f"/api/v2/lists/{lid}").json()
        if j["status"] != "extracting":
            break
        time.sleep(0.1)
    rid = c.post(f"/api/v2/lists/{lid}/start", json={}, headers=H).json()["run_id"]
    _run(dsn, rid)
    fixes = c.get(f"/api/v2/sites/{j['site_id']}/fixes?view=all&limit=500").json()["fixes"]
    fix = next(f for f in fixes if "description" in f["title"].lower())
    assert fix["status"] == "open"
    _Shop.fixed = True                                                       # the team ships the fix
    r = c.post(f"/api/v2/fixes/{fix['id']}/recheck", headers=H)
    assert r.status_code == 201, r.text
    assert r.json()["pages"] == 3 and r.json()["kind"] == "coverage"
    _run(dsn, r.json()["run_id"])
    after = c.get(f"/api/v2/fixes/{fix['id']}").json()
    assert after["fix"]["status"] == "verified"
    assert any(e["kind"] == "recheck" for e in after["events"])
    run = c.get(f"/api/v2/runs/{r.json()['run_id']}").json()["run"]
    assert run["options"]["spot"]["source"] == "fix" and run["options"]["spot"]["fix_id"] == fix["id"]
    # impact: not measurable without Search Console figures, and says so
    assert c.get(f"/api/v2/fixes/{fix['id']}/impact").json()["state"] == "no_search_console"


def test_fix_impact_windows(shop):
    from datetime import date, timedelta
    from qc6 import fix_tools
    c, base, dsn = shop

    async def go():
        import asyncpg
        from qc6.db.pool import _init
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2, init=_init)
        f = dict(await pool.fetchrow("SELECT * FROM fixes WHERE status = 'verified' ORDER BY id LIMIT 1"))
        pages = await fix_tools.fix_pages(pool, f["id"])
        pivot = date(2026, 9, 1)
        await pool.execute("UPDATE fix_events SET created_at = $2 WHERE fix_id = $1 AND kind = 'verified'", f["id"], pivot)
        rows = []
        for i in range(1, 61):                          # 60 days before: 2 clicks/day/page; after: 5 clicks/day/page
            for p in pages:
                rows.append((f["site_id"], p, pivot - timedelta(days=i), 2, 100, 9.0))
                rows.append((f["site_id"], p, pivot + timedelta(days=i - 1), 5, 120, 6.0))
        rows.append((f["site_id"], "https://elsewhere.example/x", pivot, 999, 999, 1.0))      # not the fix's page
        await pool.copy_records_to_table("search_page_days", records=rows,
                                         columns=["site_id", "page", "day", "clicks", "impressions", "position"])
        full = await fix_tools.impact(pool, f, today=pivot + timedelta(days=80))
        early = await fix_tools.impact(pool, f, today=pivot + timedelta(days=20))
        await pool.close()
        return full, early, len(pages)
    full, early, n = asyncio.run(go())
    w14 = full["windows"][0]
    assert full["state"] == "ok" and w14["ready"] and w14["before"]["clicks"] == 2 * 14 * n and w14["after"]["clicks"] == 5 * 14 * n
    assert w14["change"]["clicks_pct"] == 150.0 and w14["change"]["position"] == -3.0       # moved up 3 places
    assert all(w["ready"] for w in full["windows"])
    assert [w["ready"] for w in early["windows"]] == [True, False, False]                     # 3-day lag: 28 and 60 not yet
    assert "of 28 days" in early["windows"][1]["text"]


def test_opportunity_review_and_score_breakdown(shop):
    c, base, dsn = shop
    sid = c.get("/api/v2/sites").json()["sites"][0]["id"]
    d = c.get(f"/api/v2/sites/{sid}/growth/opportunities?review=").json()
    assert d["pages"] and "parts_labels" in d and all("parts" in p and "multiplier" in p for p in d["pages"])
    p = d["pages"][0]
    assert abs(sum(p["parts"].values()) - p["score"]) <= 0.2 * len(p["parts"]) + 0.1         # the parts add up to the score
    url = p["url"]
    assert c.post(f"/api/v2/sites/{sid}/growth/opportunities/review", json={"url": url, "status": "accepted"}, headers=H).status_code == 201
    assert c.post(f"/api/v2/sites/{sid}/growth/opportunities/review",
                  json={"url": url, "status": "dismissed", "note": "seasonal page"}, headers=H).status_code == 201
    hist = c.get(f"/api/v2/sites/{sid}/growth/opportunities/review", params={"url": url}).json()["history"]
    assert [h["status"] for h in hist] == ["dismissed", "accepted"] and hist[0]["note"] == "seasonal page"
    dismissed = c.get(f"/api/v2/sites/{sid}/growth/opportunities?review=dismissed").json()
    assert [x["url"] for x in dismissed["pages"]] == [url] and dismissed["pages"][0]["review"]["status"] == "dismissed"
    still_open = c.get(f"/api/v2/sites/{sid}/growth/opportunities?review=open").json()
    assert url not in [x["url"] for x in still_open["pages"]] and still_open["review_counts"]["dismissed"] == 1
    bad = c.post(f"/api/v2/sites/{sid}/growth/opportunities/review", json={"url": url, "status": "maybe"}, headers=H)
    assert bad.status_code == 422
