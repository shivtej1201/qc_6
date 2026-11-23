"""robots.txt Crawl-delay (CrawlPulse 3 politeness): when a site asks for N seconds between requests, QC waits at least
that long between requests to it — page fetches, link checks on its own hosts, real-browser visits and journeys.

Read from the group for QC's own name (`QC-Audit`), else the `*` group, the same way robots groups are matched for
Allow / Disallow. Decimal values (1.5) count; Python's robotparser drops them. Nonsense values are ignored.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

QC_TOKEN = "qc-audit"
MAX_HONOURED_S = 3600.0          # an hour between requests is the most anyone means; beyond that the value is a typo


def parse(text: str, token: str = QC_TOKEN) -> Optional[float]:
    """Crawl-delay in seconds for `token` (or '*'), or None when the site doesn't set one."""
    groups: List[Tuple[List[str], Dict[str, str]]] = []
    agents: List[str] = []
    rules: Dict[str, str] = {}
    in_rules = False
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, val = (x.strip() for x in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if in_rules:                                  # a new group starts
                groups.append((agents, rules))
                agents, rules, in_rules = [], {}, False
            agents.append(val.lower())
        else:
            in_rules = True
            if key == "crawl-delay" and "crawl-delay" not in rules:
                rules["crawl-delay"] = val
    if agents:
        groups.append((agents, rules))
    mine = [r for a, r in groups if any(x not in ("", "*") and (token.startswith(x) or x.startswith(token)) for x in a)]
    star = [r for a, r in groups if "*" in a]
    for chosen in (mine, star):
        for r in chosen:
            v = r.get("crawl-delay")
            if v is None:
                continue
            try:
                d = float(re.sub(r"[^0-9.]", "", v) or "x")
            except ValueError:
                continue
            if 0 < d <= MAX_HONOURED_S:
                return d
        if chosen:
            return None                                   # our group exists but sets no delay: '*' doesn't apply
    return None


async def fetch(site: Dict, allow_local: bool) -> Optional[float]:
    """Read the site's robots.txt (honest identity, pinned connection) and return its Crawl-delay for QC."""
    import httpx
    from qc6.settings import settings
    from qc6.site_access import httpx_hook
    from qc6.workers.coverage import identity_headers
    from qcv5.crawler.safe_net import safe_transport
    try:
        async with httpx.AsyncClient(timeout=15, headers=identity_headers(site), transport=safe_transport(allow_local),
                                     event_hooks={"request": [httpx_hook(site)]}) as c:
            r = await c.get(site["base_url"].rstrip("/") + "/robots.txt")
        if r.status_code != 200:
            return None
        return parse(r.text[:500_000])
    except (httpx.HTTPError, OSError, ValueError):
        return None


def paced_rate(rate_rps: float, delay_s: Optional[float]) -> float:
    """The request rate after honouring a Crawl-delay."""
    return min(rate_rps, 1.0 / delay_s) if delay_s else rate_rps
