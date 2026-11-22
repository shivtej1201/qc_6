"""P1 gaps: connect only to the checked address (DNS rebinding), certificates checked and reported."""
import asyncio
import os
import socket
import sys

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "engine"))
from qcv5.core.request_errors import classify_request_exception  # noqa: E402
from qcv5.crawler import safe_net  # noqa: E402
from qcv5.crawler.http_pool import PerHostPoolTransport  # noqa: E402


def test_blocked_addresses():
    async def go():
        out = {}
        for host in ("127.0.0.1", "169.254.169.254", "100.64.3.4", "10.1.2.3", "0x7f.1", "localhost", "[::1]"):
            try:
                await safe_net.checked_addresses(host, 80)
                out[host] = "allowed"
            except safe_net.BlockedAddress:
                out[host] = "blocked"
        out["8.8.8.8"] = (await safe_net.checked_addresses("8.8.8.8", 443))[0]
        out["local-ok"] = await safe_net.checked_addresses("127.0.0.1", 80, allow_local=True)
        return out
    got = asyncio.run(go())
    assert all(got[h] == "blocked" for h in ("127.0.0.1", "169.254.169.254", "100.64.3.4", "10.1.2.3", "0x7f.1", "localhost", "[::1]"))
    assert got["8.8.8.8"] == "8.8.8.8" and got["local-ok"] == ["127.0.0.1"]


def test_rebinding_is_refused_at_connect_time(monkeypatch):
    """The pre-check sees a public address; the connection-time lookup answers 127.0.0.1. QC must not connect."""
    answers = iter([[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))],
                    [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]])

    async def fake_getaddrinfo(self, host, port, **kw):
        return next(answers)
    monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "getaddrinfo", fake_getaddrinfo)
    connected = []


    async def go():
        first = await safe_net.checked_addresses("rebind.test", 80)          # what a pre-check would have seen
        t = safe_net.safe_transport()
        orig = t._pool._network_backend.inner.connect_tcp

        async def spy(host, *a, **k):
            connected.append(host)
            return await orig(host, *a, **k)
        t._pool._network_backend.inner.connect_tcp = spy
        async with httpx.AsyncClient(transport=t) as c:
            with pytest.raises(Exception) as ei:
                await c.get("http://rebind.test/")
        return first, ei.value
    first, err = asyncio.run(go())
    assert first == ["93.184.216.34"]
    assert connected == [] and classify_request_exception(err) == "Blocked (SSRF)"


def test_connects_to_the_checked_ip_with_the_real_hostname(monkeypatch):
    """Pinning: the socket goes to the checked IP, while the Host header (and SNI) stay the site's name."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    seen = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            seen["host"] = self.headers.get("Host")
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    async def fake_getaddrinfo(self, host, p, **kw):
        assert host == "shop.example"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", p))]
    monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "getaddrinfo", fake_getaddrinfo)

    async def go():
        async with httpx.AsyncClient(transport=safe_net.safe_transport(allow_local=True)) as c:
            return (await c.get(f"http://shop.example:{port}/")).text
    assert asyncio.run(go()) == "ok" and seen["host"] == f"shop.example:{port}"
    srv.shutdown()


def test_engine_pool_is_pinned_and_verifies_certificates(monkeypatch):
    monkeypatch.delenv("QC6_TLS_VERIFY", raising=False)
    t = PerHostPoolTransport(limits=httpx.Limits(max_connections=4), verify=safe_net.tls_verify())
    shard = t._pool_for(httpx.URL("https://www.example.com/"))
    assert isinstance(shard._pool._network_backend, safe_net.PinnedBackend)
    assert shard._pool._ssl_context.verify_mode.name == "CERT_REQUIRED"
    monkeypatch.setenv("QC6_TLS_VERIFY", "false")
    assert safe_net.tls_verify() is False


def test_bad_certificate_becomes_an_ssl_finding():
    """A self-signed certificate fails the check and is classified so the page gets SSL_ERROR, not silence."""
    import ssl
    import subprocess
    import tempfile
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    d = tempfile.mkdtemp()
    key, crt = os.path.join(d, "k.pem"), os.path.join(d, "c.pem")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", crt, "-days", "1",
                    "-subj", "/CN=localhost"], check=True, capture_output=True)

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(crt, key)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    async def go():
        async with httpx.AsyncClient(transport=safe_net.safe_transport(allow_local=True)) as c:
            try:
                await c.get(f"https://127.0.0.1:{srv.server_address[1]}/")
            except Exception as e:
                return e
    err = asyncio.run(go())
    srv.shutdown()
    assert err is not None and classify_request_exception(err) == "SSL Error"
