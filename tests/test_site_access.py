"""Site-private headers (allow-token, staging login) reach only the site's own hosts; crawl scope; encryption."""
import asyncio
import base64
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

os.environ["QC6_ALLOW_PRIVATE_TARGETS"] = "true"
os.environ.setdefault("QC6_SECRET_KEY", "test-secret-key-for-staging-logins")

from qc6 import site_access as A  # noqa: E402

SEEN = []


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        SEEN.append((self.headers["Host"].split(":")[0], self.path, self.headers.get("X-QC-Audit"),
                     self.headers.get("Authorization"), self.headers.get("X-Env"), self.headers.get("User-Agent", "")))
        if self.path == "/page":
            other = self.server.other
            body = f'<html><body><h1>Hi</h1><img src="http://localhost:{other}/pixel.gif"></body></html>'.encode()
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(body)
        elif self.path == "/away":
            self.send_response(302); self.send_header("Location", f"http://localhost:{self.server.other}/landed"); self.end_headers()
        else:
            self.send_response(200); self.send_header("Content-Type", "image/gif"); self.end_headers(); self.wfile.write(b"GIF89a")


@pytest.fixture(scope="module")
def servers():
    a = ThreadingHTTPServer(("127.0.0.1", 0), H)
    b = ThreadingHTTPServer(("127.0.0.1", 0), H)
    a.other = b.server_address[1]
    for s in (a, b):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    yield a.server_address[1], b.server_address[1]
    a.shutdown(); b.shutdown()


def _site(port):
    st = A.clean_staging({"username": "qa", "password": "p@ss:word", "headers": {"X-Env": "staging"}}, None)
    return {"id": 1, "primary_host": "127.0.0.1", "base_url": f"http://127.0.0.1:{port}", "access_token": "tok-site",
            "settings": {"staging": st}}


def test_hosts():
    assert A.is_site_host("www.example.com", "www.example.com")
    assert A.is_site_host("example.com", "www.example.com") and A.is_site_host("img.example.com", "www.example.com")
    assert not A.is_site_host("example.com.evil.net", "www.example.com") and not A.is_site_host("notexample.com", "www.example.com")


def test_scope():
    sc = A.clean_scope({"include": ["/loans/*", "/cards/*"], "exclude": ["*?sort=*", "/loans/archive/*"]})
    urls = ["https://x.test/loans/a", "https://x.test/loans/a?sort=price", "https://x.test/loans/archive/old",
            "https://x.test/cards/b", "https://x.test/blog/c"]
    kept, dropped = A.apply_scope(urls, sc)
    assert kept == ["https://x.test/loans/a", "https://x.test/cards/b"] and dropped == 3
    assert A.apply_scope(urls, None) == (urls, 0)
    with pytest.raises(A.AccessError):
        A.clean_scope({"include": ["loans"]})


def test_staging_encrypted_masked_and_kept():
    st = A.clean_staging({"username": "qa", "password": "secret1", "headers": {"X-Env": "staging"}}, None)
    assert "secret1" not in str(st) and "staging" not in st["sealed"]                # encrypted at rest
    assert A.public_staging(st) == {"username": "qa", "has_password": True, "header_names": ["X-Env"]}
    st2 = A.clean_staging({"username": "qa2", "password": "", "headers": {"X-Env": "•••"}}, st)   # form re-saved, nothing retyped
    assert A.unseal(st2["sealed"]) == {"username": "qa2", "password": "secret1", "headers": {"X-Env": "staging"}}
    assert A.clean_staging({"clear": True}, st2) is None
    for bad in ({"headers": {"User-Agent": "x"}}, {"headers": {"X-QC-Audit": "x"}}, {"headers": {"Bad Name": "x"}},
                {"headers": {"X-A": "a\r\nInjected: 1"}}, {"username": "a:b"}):
        with pytest.raises(A.AccessError):
            A.clean_staging(bad, None)


def test_http_client_sends_private_headers_to_the_site_only(servers):
    a, b = servers
    site = _site(a)

    async def go():
        async with httpx.AsyncClient(event_hooks={"request": [A.httpx_hook(site)]}, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0 QC-Audit/6.0"}) as c:
            await c.get(f"http://127.0.0.1:{a}/away")            # redirects to the other host
            await c.get(f"http://localhost:{b}/direct")
    SEEN.clear()
    asyncio.run(go())
    site_reqs = [x for x in SEEN if x[0] == "127.0.0.1"]
    other = [x for x in SEEN if x[0] == "localhost"]
    auth = "Basic " + base64.b64encode(b"qa:p@ss:word").decode()
    assert site_reqs and all(x[2] == "tok-site" and x[3] == auth and x[4] == "staging" for x in site_reqs)
    assert len(other) == 2 and all(x[2] is None and x[3] is None and x[4] is None for x in other)
    assert all("QC-Audit/6.0" in x[5] for x in SEEN)           # identity is public, everywhere


def test_browser_sends_private_headers_to_the_site_only(servers):
    from playwright.async_api import async_playwright
    from qc6.workers.visit import make_route_guard
    a, b = servers
    site = _site(a)

    async def go():
        async with async_playwright() as p:
            br = await p.chromium.launch()
            ctx = await br.new_context()
            await ctx.route("**/*", make_route_guard(site, None))
            page = await ctx.new_page()
            await page.goto(f"http://127.0.0.1:{a}/page", wait_until="load")
            await page.wait_for_timeout(300)
            await br.close()
    SEEN.clear()
    asyncio.run(go())
    page = [x for x in SEEN if x[1] == "/page"]
    pixel = [x for x in SEEN if x[1] == "/pixel.gif"]
    assert page and page[0][2] == "tok-site" and page[0][3].startswith("Basic ") and page[0][4] == "staging"
    assert pixel and pixel[0][0] == "localhost" and pixel[0][2] is None and pixel[0][3] is None and pixel[0][4] is None
