"""Google Search Console + GA4 for keyword intelligence (CrawlPulse FEAT-12, FEAT-13, FEAT-14). Free.

Setup: a Google Cloud service account with the Search Console API (and Analytics Data API) enabled, added as a user of
the Search Console property (and GA4 property). On the server: GOOGLE_SERVICE_ACCOUNT_FILE=/path/key.json. Per site
(Settings): the Search Console property ("sc-domain:example.com" or "https://www.example.com/") and, optionally, the GA4
property id. Sync runs after every completed check and on demand.

What QC derives (definitions from the CrawlPulse keyword spec):
* striking distance: queries at average position 4-15 with at least `min_impressions` impressions (default 2,500);
* CTR gap: actual CTR against the expected CTR for the position (benchmark curve below);
* cannibalisation: a query where two or more of the site's pages rank in the top 20, with at least 1,000 impressions.

These are Google's own numbers for the site's own property; QC fetches nothing from Google Search itself.
"""
from __future__ import annotations

import urllib.parse
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import httpx

TOKEN_URL = "https://oauth2.googleapis.com/token"
GSC_API = "https://searchconsole.googleapis.com/webmasters/v3/sites/{site}/searchAnalytics/query"
PAGE_SIZE = 25_000
MAX_ROWS = 200_000

# organic CTR by position (CrawlPulse keyword spec, industry benchmark); positions past 10 decay to ~1 %
EXPECTED_CTR = {1: 0.285, 2: 0.157, 3: 0.110, 4: 0.080, 5: 0.062, 6: 0.048, 7: 0.036, 8: 0.028, 9: 0.022, 10: 0.018}

_transport: Optional[httpx.AsyncBaseTransport] = None


DAILY_DAYS = 150                # 60 days before + 60 after a fix, plus slack
MAX_DAILY_ROWS = 2_000_000


def set_transport(t) -> None:
    global _transport
    _transport = t


def expected_ctr(position: float) -> float:
    p = max(1, int(round(position)))
    return EXPECTED_CTR.get(p, max(0.004, 0.018 * (0.8 ** (p - 10))))


def configured(site: Dict[str, Any]) -> Dict[str, Any]:
    from qcv5.integrations.google_search_console import load_service_account
    st = site.get("settings") or {}
    try:
        sa = load_service_account()
        sa_err = None
    except ValueError as e:
        sa, sa_err = None, str(e)
    return {"service_account": bool(sa), "service_account_error": sa_err, "gsc_property": st.get("gsc_property") or None,
            "ga4_property_id": st.get("ga4_property_id") or None, "ready": bool(sa and st.get("gsc_property"))}


async def sync(pool, site: Dict[str, Any], days: int = 28) -> Dict[str, Any]:
    """Replace the site's search snapshot with the last `days` days (ending 2 days ago: Search Console lags)."""
    from qcv5.integrations.google_search_console import load_service_account, make_jwt
    cfg = configured(site)
    if not cfg["ready"]:
        raise RuntimeError("Search Console isn't set up: enter the property on the Growth screen and set GOOGLE_SERVICE_ACCOUNT_FILE on the server.")
    sa = load_service_account()
    end = date.today() - timedelta(days=2)
    start = end - timedelta(days=max(1, min(days, 480)) - 1)
    kw: Dict[str, Any] = {"timeout": 60}
    if _transport is not None:
        kw["transport"] = _transport
    rows: List[tuple] = []
    truncated = False
    async with httpx.AsyncClient(**kw) as c:
        tr = await c.post(sa.get("token_uri") or TOKEN_URL, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                                                  "assertion": make_jwt(sa)})
        if tr.status_code != 200:
            raise RuntimeError(f"Google didn't accept the service account (HTTP {tr.status_code}).")
        auth = {"Authorization": f"Bearer {tr.json()['access_token']}"}
        url = GSC_API.format(site=urllib.parse.quote(cfg["gsc_property"], safe=""))
        while True:
            r = await c.post(url, headers=auth, json={"startDate": start.isoformat(), "endDate": end.isoformat(),
                                                      "dimensions": ["query", "page"], "rowLimit": PAGE_SIZE,
                                                      "startRow": len(rows), "dataState": "final"})
            if r.status_code == 403:
                raise RuntimeError("Search Console said no: add the service account's e-mail as a user of the property.")
            if r.status_code != 200:
                raise RuntimeError(f"Search Console answered HTTP {r.status_code}.")
            batch = r.json().get("rows", [])
            rows += [(x["keys"][0][:500], x["keys"][1][:2000], int(x.get("clicks", 0)), int(x.get("impressions", 0)),
                      float(x.get("ctr", 0)), float(x.get("position", 0))) for x in batch]
            if len(batch) < PAGE_SIZE:
                break
            if len(rows) >= MAX_ROWS:
                truncated = True
                break
        # day-by-day figures per page, for the before/after impact of fixes: 150 days the first time, then the newest days
        last = await pool.fetchval("SELECT max(day) FROM search_page_days WHERE site_id = $1", site["id"])
        dstart = max(end - timedelta(days=DAILY_DAYS - 1), (last - timedelta(days=3)) if last else end - timedelta(days=DAILY_DAYS - 1))
        days_rows: List[tuple] = []
        while True:
            r = await c.post(url, headers=auth, json={"startDate": dstart.isoformat(), "endDate": end.isoformat(),
                                                      "dimensions": ["page", "date"], "rowLimit": PAGE_SIZE,
                                                      "startRow": len(days_rows), "dataState": "final"})
            if r.status_code != 200:
                break                                       # the 28-day snapshot is still good; impact waits for the next sync
            batch = r.json().get("rows", [])
            days_rows += [(x["keys"][0][:2000], date.fromisoformat(x["keys"][1]), int(x.get("clicks", 0)),
                           int(x.get("impressions", 0)), float(x.get("position", 0))) for x in batch]
            if len(batch) < PAGE_SIZE or len(days_rows) >= MAX_DAILY_ROWS:
                break
    ga = None
    if cfg["ga4_property_id"]:
        from qc6 import ga4
        try:
            ga = await ga4.traffic(site, days=days)
        except httpx.HTTPError:
            ga = None
    async with pool.acquire() as conn:
        async with conn.transaction():
            if days_rows:
                await conn.execute("DELETE FROM search_page_days WHERE site_id = $1 AND day >= $2", site["id"], dstart)
                await conn.copy_records_to_table("search_page_days", records=[(site["id"], *r) for r in days_rows],
                                                 columns=["site_id", "page", "day", "clicks", "impressions", "position"])
                await conn.execute("DELETE FROM search_page_days WHERE site_id = $1 AND day < $2", site["id"],
                                   end - timedelta(days=480))      # Search Console keeps 16 months; so do we
            await conn.execute("DELETE FROM search_rows WHERE site_id = $1", site["id"])
            await conn.copy_records_to_table("search_rows", records=[(site["id"], *r) for r in rows],
                                             columns=["site_id", "query", "page", "clicks", "impressions", "ctr", "position"])
            if ga is not None:
                await conn.execute("DELETE FROM ga4_pages WHERE site_id = $1", site["id"])
                await conn.copy_records_to_table("ga4_pages", records=[(site["id"], p, n) for p, n in ga["paths"].items()],
                                                 columns=["site_id", "path", "sessions"])
            await conn.execute("""INSERT INTO search_syncs (site_id, synced_at, start_date, end_date, gsc_rows, ga4_pages, truncated, error)
                                  VALUES ($1, now(), $2, $3, $4, $5, $6, NULL)
                                  ON CONFLICT (site_id) DO UPDATE SET synced_at = now(), start_date = $2, end_date = $3, gsc_rows = $4,
                                    ga4_pages = $5, truncated = $6, error = NULL""",
                               site["id"], start, end, len(rows), len(ga["paths"]) if ga else 0, truncated)
    return {"rows": len(rows), "ga4_pages": len(ga["paths"]) if ga else 0, "truncated": truncated,
            "start": start.isoformat(), "end": end.isoformat()}


async def record_error(pool, site_id: int, err: str) -> None:
    await pool.execute("""INSERT INTO search_syncs (site_id, error) VALUES ($1, $2)
                          ON CONFLICT (site_id) DO UPDATE SET error = $2""", site_id, err[:500])


# ------------------------------------------------------------------ keyword intelligence
async def striking_distance(pool, site_id: int, min_impressions: int = 2500, limit: int = 200) -> List[Dict[str, Any]]:
    rows = await pool.fetch("""SELECT query, page, clicks, impressions, ctr, position FROM search_rows
                               WHERE site_id = $1 AND position >= 4 AND position <= 15 AND impressions >= $2
                               ORDER BY impressions DESC LIMIT $3""", site_id, min_impressions, limit)
    out = []
    for r in rows:
        exp = expected_ctr(r["position"])
        # clicks if the page climbed to position 3 at the benchmark CTR: the size of the prize, not a promise
        out.append({**dict(r), "expected_ctr": exp, "ctr_gap": round(max(0.0, exp - r["ctr"]) / exp, 3) if exp else 0,
                    "clicks_at_position_3": int(r["impressions"] * EXPECTED_CTR[3])})
    return out


async def cannibalisation(pool, site_id: int, min_impressions: int = 1000, limit: int = 100) -> List[Dict[str, Any]]:
    rows = await pool.fetch("""WITH q AS (SELECT query, sum(impressions) AS impressions, sum(clicks) AS clicks,
                                                 count(*) FILTER (WHERE position <= 20) AS pages_top20
                                          FROM search_rows WHERE site_id = $1 GROUP BY query)
                               SELECT q.query, q.impressions, q.clicks, q.pages_top20,
                                      json_agg(json_build_object('page', r.page, 'position', round(r.position::numeric, 1),
                                               'clicks', r.clicks, 'impressions', r.impressions) ORDER BY r.position) AS pages
                               FROM q JOIN search_rows r ON r.site_id = $1 AND r.query = q.query AND r.position <= 20
                               WHERE q.pages_top20 >= 2 AND q.impressions >= $2
                               GROUP BY q.query, q.impressions, q.clicks, q.pages_top20
                               ORDER BY q.impressions DESC LIMIT $3""", site_id, min_impressions, limit)
    import json as _json
    return [{**dict(r), "pages": _json.loads(r["pages"]) if isinstance(r["pages"], str) else r["pages"]} for r in rows]


async def status(pool, site: Dict[str, Any]) -> Dict[str, Any]:
    s = await pool.fetchrow("SELECT * FROM search_syncs WHERE site_id = $1", site["id"])
    return {"config": configured(site), "last_sync": dict(s) if s else None}
