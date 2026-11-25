"""Crawl speed + accuracy: per-host limit that follows answer time, precise network remarks, second check of
transient failures (a working URL must not be reported as 'DNS / network issue')."""
import asyncio
import os
import socket
import sys

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "engine"))
from qcv5.core.request_errors import describe_request_exception  # noqa: E402
from qcv5.crawler import on_page_audit_engine as eng  # noqa: E402


# ------------------------------------------------------------------ remarks
def test_describe_request_exception_is_precise():
    req = httpx.Request("GET", "https://example.test/")
    dns_tmp = httpx.ConnectError("x", request=req)
    dns_tmp.__cause__ = socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")
    dns_none = httpx.ConnectError("x", request=req)
    dns_none.__cause__ = socket.gaierror(socket.EAI_NONAME, "Name or service not known")
    refused = httpx.ConnectError("x", request=req)
    refused.__cause__ = ConnectionRefusedError(111, "Connection refused")
    assert describe_request_exception(httpx.PoolTimeout("x", request=req)).startswith("QC connection pool busy")
    assert describe_request_exception(httpx.ConnectTimeout("x", request=req)).startswith("Connection timeout")
    assert describe_request_exception(httpx.ReadTimeout("x", request=req)).startswith("Response timeout")
    assert describe_request_exception(dns_tmp).startswith("DNS temporary failure")
    assert describe_request_exception(dns_none).startswith("DNS resolution failure")
    assert describe_request_exception(refused) == "Connection refused"


# ------------------------------------------------------------------ adaptive per-host limit
def test_adaptive_limit_grows_shrinks_and_survives_cancel():
    async def go():
        lim = eng.AdaptiveLimit(2)
        await lim.__aenter__(); await lim.__aenter__()
        waiter = asyncio.ensure_future(lim.__aenter__())
        await asyncio.sleep(0)
        assert not waiter.done() and lim.active == 2
        lim.set_limit(3)                              # grows: the waiter gets a slot right away
        await asyncio.sleep(0)
        assert waiter.done() and lim.active == 3
        lim.set_limit(1)                              # shrinks: nobody is cut off, new ones wait
        late = asyncio.ensure_future(lim.__aenter__())
        await asyncio.sleep(0)
        assert not late.done()
        late.cancel()
        await asyncio.sleep(0)
        assert not lim._waiters                       # a cancelled waiter leaves no ghost behind
        for _ in range(3):
            await lim.__aexit__(None, None, None)
        assert lim.active == 0
        async with lim:
            assert lim.active == 1
    asyncio.run(go())


# ------------------------------------------------------------------ second check of transient failures
def _res(bucket, status, remark):
    return {"terminal_state": "done", "initial_http_status": status, "http_status": status, "final_url": "",
            "redirect_count": 0, "redirect_chain": [], "response_time_ms": 5, "content_type": "text/html",
            "status_bucket": bucket, "status_classification": bucket, "remark": remark,
            "error_type": "" if status else bucket, "error_message": "", "robots_status": "", "waf_status": ""}


def test_second_check_recovers_blips_and_confirms_real_failures(monkeypatch):
    monkeypatch.setattr(eng.asyncio, "sleep", _no_sleep)
    e = object.__new__(eng.OnPageAuditEngine)
    e.job_id, e.metrics, e._stop_requested = "j", {}, False
    e._rechecking, e._recheck_items, e._dest_state, e._img_state = False, [], {}, {}
    e.connect_timeout, e.read_timeout = 5.0, 15.0
    calls = []

    async def fetch(client, url, need_body=False, kind="", timeout=None):
        calls.append((url, kind, timeout.read))
        return _res("2xx", 200, "OK") if "ok" in url else _res("DNS/Network", None, "DNS resolution failure")

    e.fetch_url_manual_redirects = fetch
    blip = e._dest_record("https://a.test/ok", "external", _res("DNS/Network", None, "DNS temporary failure"))
    dead = e._dest_record("https://gone.test/x", "external", _res("DNS/Network", None, "DNS resolution failure"))
    fine = e._dest_record("https://a.test/fine", "internal", _res("2xx", 200, "OK"))
    for r in (blip, dead, blip, fine):                # the same URL twice is checked once; a 200 is not rechecked
        e._note_recheck("dest", r["normalized_url"], r)

    async def go():
        e._write_q = asyncio.Queue()
        await e._recheck_transient(None)
        out = {}
        while not e._write_q.empty():
            kind, rec = e._write_q.get_nowait()
            out[rec["normalized_url"]] = rec
        return out
    out = asyncio.run(go())
    assert sorted(u for u, _, _ in calls) == ["https://a.test/ok", "https://gone.test/x"]
    assert all(k == "recheck" and t >= 45 for _, k, t in calls)       # calmer, longer timeouts
    assert out["https://a.test/ok"]["http_status"] == 200
    assert "worked on a second check" in out["https://a.test/ok"]["remark"]
    assert "confirmed on a second check" in out["https://gone.test/x"]["remark"]
    assert e.metrics["recheck_recovered"] == 1 and e.metrics["recheck_confirmed"] == 1
    assert e._recheck_items == [] and e._rechecking is False


_real_sleep = asyncio.sleep


async def _no_sleep(_s, *a, **k):
    await _real_sleep(0)


def test_pause_interrupts_the_unreachable_site_probe():
    """The 'is the site reachable?' probe waits up to 11 x 45 s; a pause / cancel must not wait for it."""
    import time as _t
    e = object.__new__(eng.OnPageAuditEngine)
    e.probe_url, e.primary_domain, e.akamai_cooldown_sec, e.probe_interval_sec = "https://down.test/robots.txt", "down.test", 0, 45.0
    e.metrics = {"http_requests": 0, "requests_by_kind": {"probe": 0}}
    e._stop_requested = False

    class Down:
        async def get(self, *a, **k):
            raise httpx.ConnectError("All connection attempts failed")

    async def go():
        e._stop_ev = asyncio.Event()
        e._stop_loop = asyncio.get_running_loop()
        asyncio.get_running_loop().call_later(0.3, e.request_stop)
        t0 = _t.perf_counter()
        await e._wait_for_primary_ready(Down())
        return _t.perf_counter() - t0
    assert asyncio.run(go()) < 2
