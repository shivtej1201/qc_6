"""Connect only to the address that was checked (CrawlPulse crawling engine §4.4: "pin the socket to the validated IP").

Checking a hostname and then letting the HTTP client look it up again leaves a gap: a hostile DNS server can answer
with a public address for the check and a private one (127.0.0.1, 169.254.169.254, an office server) for the
connection ("DNS rebinding"). This network backend closes it: for every new connection it resolves the host once,
refuses the connection if ANY answer is a prohibited address, and connects to one of exactly those checked addresses.
TLS still runs against the hostname (SNI and certificate checks use the request's host, not the IP), so pinning
never weakens certificate verification.

The configured HTTP(S) proxy, if any, is the one host connected to as named: through a proxy the target is reached
by the proxy, not by this machine.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
import typing
import urllib.parse

import httpcore
import httpx

from qcv5.core.request_errors import SSRFRedirectBlocked
from qcv5.crawler.url_normalizer import _is_prohibited_ip, _try_parse_numeric_ip


class BlockedAddress(SSRFRedirectBlocked):
    """The host resolves to an address QC never connects to (private, loopback, link-local, metadata, CGNAT...)."""


def _proxy_hosts() -> typing.Set[str]:
    out = set()
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
        v = os.environ.get(k)
        if v:
            h = urllib.parse.urlsplit(v if "://" in v else "http://" + v).hostname
            if h:
                out.add(h.lower())
    return out


async def checked_addresses(host: str, port: int, allow_local: bool = False) -> typing.List[str]:
    """Every address the host resolves to, if none of them is prohibited; else BlockedAddress."""
    h = host.strip("[]").lower()
    literal: typing.Optional[typing.Union[ipaddress.IPv4Address, ipaddress.IPv6Address]] = None
    try:
        literal = ipaddress.ip_address(h)
    except ValueError:
        literal = _try_parse_numeric_ip(h)
    if literal is not None:
        if _is_prohibited_ip(literal, allow_local=allow_local):
            raise BlockedAddress(f"{host} is not a public address")
        return [str(literal)]
    if h == "localhost" or h.endswith(".localhost"):
        if not allow_local:
            raise BlockedAddress(f"{host} is this machine")
    infos = await asyncio.get_running_loop().getaddrinfo(h, port, type=socket.SOCK_STREAM)
    ips: typing.List[str] = []
    for info in infos:
        ip = info[4][0].split("%", 1)[0]
        if _is_prohibited_ip(ipaddress.ip_address(ip), allow_local=allow_local):
            raise BlockedAddress(f"{host} resolves to {ip}, which QC never connects to")
        if ip not in ips:
            ips.append(ip)
    if not ips:
        raise httpcore.ConnectError(f"{host} has no address")
    return ips


class PinnedBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, allow_local: bool = False, inner: typing.Optional[httpcore.AsyncNetworkBackend] = None) -> None:
        self.allow_local = allow_local
        self.inner = inner or httpcore.AnyIOBackend()
        self.proxies = _proxy_hosts()

    async def connect_tcp(self, host: str, port: int, timeout: typing.Optional[float] = None,
                          local_address: typing.Optional[str] = None,
                          socket_options: typing.Optional[typing.Iterable[typing.Any]] = None) -> httpcore.AsyncNetworkStream:
        if host.lower() in self.proxies:
            return await self.inner.connect_tcp(host, port, timeout, local_address, socket_options)
        last: typing.Optional[BaseException] = None
        for ip in await checked_addresses(host, port, self.allow_local):
            try:
                return await self.inner.connect_tcp(ip, port, timeout, local_address, socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as e:
                last = e
        assert last is not None
        raise last

    async def connect_unix_socket(self, path: str, timeout: typing.Optional[float] = None,
                                  socket_options: typing.Optional[typing.Iterable[typing.Any]] = None) -> httpcore.AsyncNetworkStream:
        raise BlockedAddress("unix sockets are never used for crawling")

    async def sleep(self, seconds: float) -> None:
        await self.inner.sleep(seconds)


def tls_verify() -> bool:
    """Certificates are checked unless QC6_TLS_VERIFY=false (e.g. a staging site with a self-made certificate).
    A failed check is reported on the page as SSL_ERROR, never silently ignored."""
    return os.environ.get("QC6_TLS_VERIFY", "true").strip().lower() not in ("0", "false", "no", "off")


def pin(transport: httpx.AsyncHTTPTransport, allow_local: bool = False) -> httpx.AsyncHTTPTransport:
    """Give an httpx transport the pinned backend (httpcore 1.x keeps it on the pool)."""
    pool = getattr(transport, "_pool", None)
    if pool is None or not hasattr(pool, "_network_backend"):
        raise RuntimeError("this httpx/httpcore version can't be pinned; refusing to crawl unpinned")
    pool._network_backend = PinnedBackend(allow_local=allow_local)
    return transport


def safe_transport(allow_local: bool = False, **kw: typing.Any) -> httpx.AsyncHTTPTransport:
    kw.setdefault("verify", tls_verify())
    return pin(httpx.AsyncHTTPTransport(**kw), allow_local)


def safe_client(allow_local: bool = False, transport_kw: typing.Optional[dict] = None, **client_kw: typing.Any) -> httpx.AsyncClient:
    """An AsyncClient for fetching from audited sites: pinned connections, certificates checked."""
    return httpx.AsyncClient(transport=safe_transport(allow_local, **(transport_kw or {})), **client_kw)
