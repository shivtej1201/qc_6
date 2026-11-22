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
