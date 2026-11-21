"""SEO opportunity score (CrawlPulse FEAT-15, spec "seo-opportunity-scoring"): which pages are worth working on first.

    score = M_type x (wT*S_T + wP*S_P + wC*S_C + wR*S_R + wQ*S_Q + wL*S_L) / (sum of weights QC has data for) x 100

S_T technical defect (worst open fix: critical 1, high .7, medium .4, low .2) · S_P striking distance (a query at
position 4-15 with demand) · S_C CTR gap against the benchmark for its position · S_R commercial value (GA4 sessions,
log-scaled) · S_Q content quality (thin, duplicate or near-duplicate text) · S_L link equity deficit (deep, unreachable
or few inbound links). Dimensions without data (no Search Console, no GA4) are left out and the weights re-normalised;
`confidence` says how much of the model had data. This is QC's own prioritisation, not a Google ranking score.
"""
from __future__ import annotations

import math
import urllib.parse
from collections import defaultdict
from typing import Any, Dict, List, Optional

from qc6.search_console import EXPECTED_CTR, expected_ctr

WEIGHTS = {"T": 0.25, "P": 0.25, "C": 0.15, "R": 0.15, "Q": 0.10, "L": 0.10}
SEV = {"critical": 1.0, "high": 0.7, "medium": 0.4, "low": 0.2}
SEV_NAMES = ["critical", "high", "medium", "low"]
CONTENT_SUBJECTS = {"THIN_CONTENT", "DUPLICATE_CONTENT", "DUPLICATE_TITLE", "DUPLICATE_META_DESCRIPTION"}
BUCKETS = [("critical_fix", "Critical technical fix"), ("growth", "High organic growth"), ("ctr", "CTR improvement"),
           ("content", "Content quality refresh"), ("links", "Internal link opportunity"),
           ("consolidate", "Consolidate or remove"), ("monitor", "Monitoring only")]


def type_multiplier(name: str) -> float:
    n = (name or "").lower()
    if any(k in n for k in ("login", "cart", "checkout", "account", "policy", "privacy", "terms", "sign in")):
        return 0.10
    if any(k in n for k in ("plp", "clp", "category", "categories", "listing", "collection")):
        return 1.20
    if any(k in n for k in ("blog", "article", "news", "guide", "post")):
        return 0.80
    return 1.0                                          # PDP, brand, unknown: neutral


_CACHE: Dict[tuple, Any] = {}           # (site, run, fixes version, search sync, min impressions) -> result


async def _version(pool, site_id: int) -> tuple:
    r = await pool.fetchrow("""SELECT (SELECT max(updated_at) FROM fixes WHERE site_id = $1) AS fixes,
                                      (SELECT synced_at FROM search_syncs WHERE site_id = $1) AS sync""", site_id)
    return (r["fixes"], r["sync"])


async def page_opportunities(pool, site_id: int, run_id: int, min_impressions: int = 2500,
                             with_reasons: Optional[int] = None) -> Dict[str, Any]:
    """Scores and buckets for every page of the check; reasons are written for the first `with_reasons` pages of
    whatever the caller shows (None = all). Cached per check until a fix changes or search data syncs again."""
    key = (site_id, run_id, *await _version(pool, site_id), min_impressions)
    if key not in _CACHE:
        for k in list(_CACHE):
            if k[0] == site_id:
                del _CACHE[k]
        _CACHE[key] = await _compute(pool, site_id, run_id, min_impressions)
    d = _CACHE[key]
    out = {**{k: v for k, v in d.items() if k not in ("rows", "explain")}, "pages": d["rows"], "_explain": d["explain"]}
    if with_reasons is None:
        out["pages"] = explain(out, out["pages"])
    return out


def explain(d: Dict[str, Any], rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Fill in the reasons (plain sentences) for the rows about to be shown."""
    ex = d["_explain"]
    out = []
    for r in rows:
        out.append({**r, "reasons": ex(r["url"])})
    return out


async def _compute(pool, site_id: int, run_id: int, min_impressions: int) -> Dict[str, Any]:
    pages = await pool.fetch("""SELECT p.url, p.final_url, p.http_status, coalesce(t.name, '') AS template,
                                       (p.facts->>'depth')::int AS depth, (p.facts->>'inlinks')::int AS inlinks,
                                       (p.facts->>'authority')::float AS authority, p.facts->>'site_page_type' AS site_type
                                FROM pages p LEFT JOIN templates t ON t.id = p.template_id WHERE p.run_id = $1""", run_id)
    fx = await pool.fetch("""SELECT fd.page_url,
                                    min(array_position(ARRAY['critical','high','medium','low'], f.severity)) AS worst,
                                    array_agg(DISTINCT CASE WHEN f.rule = 'NEAR_DUPLICATE' THEN 'near-duplicate text'
                                                            WHEN f.rule = 'PAGE_CHECK' AND f.subject = ANY($3::text[])
                                                            THEN lower(replace(f.subject, '_', ' ')) END) AS content
                             FROM findings fd JOIN fixes f ON f.id = fd.fix_id
                             WHERE fd.run_id = $1 AND f.site_id = $2 AND f.status IN ('open', 'in_progress')
                             GROUP BY fd.page_url""", run_id, site_id, sorted(CONTENT_SUBJECTS))
    worst: Dict[str, str] = {}
    content: Dict[str, List[str]] = {}
    for r in fx:
        if r["worst"]:
            worst[r["page_url"]] = SEV_NAMES[r["worst"] - 1]
        c = [x for x in (r["content"] or []) if x]
        if c:
            content[r["page_url"]] = sorted(c)
    has_gsc = bool(await pool.fetchval("SELECT EXISTS (SELECT 1 FROM search_rows WHERE site_id = $1)", site_id))
    has_ga4 = bool(await pool.fetchval("SELECT EXISTS (SELECT 1 FROM ga4_pages WHERE site_id = $1)", site_id))
    gsc: Dict[str, Dict[str, Any]] = {}
    if has_gsc:
        for r in await pool.fetch("""SELECT page, sum(clicks) AS clicks, sum(impressions) AS impressions,
                                            sum(position * impressions) / nullif(sum(impressions), 0) AS position
                                     FROM search_rows WHERE site_id = $1 GROUP BY page""", site_id):
            gsc[r["page"]] = dict(r)
        for r in await pool.fetch("""SELECT DISTINCT ON (page) page, query, impressions, position FROM search_rows
                                     WHERE site_id = $1 AND position >= 4 AND position <= 15 AND impressions >= $2
                                     ORDER BY page, impressions DESC""", site_id, min_impressions):
            gsc.setdefault(r["page"], {})["striking"] = dict(r)
    sessions: Dict[str, int] = {}
    if has_ga4:
        sessions = {r["path"]: r["sessions"] for r in await pool.fetch("SELECT path, sessions FROM ga4_pages WHERE site_id = $1", site_id)}
    max_sessions = max(sessions.values(), default=0)
    avail = {"T", "Q", "L"} | ({"P", "C"} if has_gsc else set()) | ({"R"} if has_ga4 else set())
    wsum = sum(WEIGHTS[k] for k in avail)
    traffic_known = has_gsc or has_ga4
    info: Dict[str, Dict[str, Any]] = {}
    rows: List[Dict[str, Any]] = []
    for p in pages:
        url = p["url"]
        g = gsc.get(url) or gsc.get(p["final_url"] or "") or {}
        sess = sessions.get(urllib.parse.urlsplit(url).path or "/", 0) if has_ga4 else 0
        depth, inl = p["depth"], p["inlinks"]
        sev = worst.get(url)
        S_T = SEV.get(sev, 0.0) if sev and sev in ("critical", "high", "medium", "low") else 0.0
        S_Q = 1.0 if url in content else 0.0
        S_L = max(1.0 if depth is None else min(1.0, max(0, depth - 3) / 3), max(0.0, (3 - inl) / 3) * 0.7) if inl is not None else 0.0
        st = g.get("striking") if has_gsc else None
        S_P = min(1.0, st["impressions"] / (2 * min_impressions)) if st else 0.0
        ctr = exp = None
        S_C = 0.0
        if has_gsc and (g.get("impressions") or 0) >= 500 and g.get("position"):
            ctr = g["clicks"] / g["impressions"]
            exp = expected_ctr(g["position"])
            S_C = max(0.0, min(1.0, (exp - ctr) / exp))
        S_R = math.log1p(sess) / math.log1p(max_sessions) if has_ga4 and max_sessions else 0.0
        S = {"T": S_T, "Q": S_Q, "L": S_L, "P": S_P, "C": S_C, "R": S_R}
        m = type_multiplier(str(p["site_type"] or p["template"]))
        score = round(min(100.0, m * sum(WEIGHTS[k] * S[k] for k in avail) / wsum * 100), 1) if wsum else 0.0
        clicks, impr = (g.get("clicks") or 0), (g.get("impressions") or 0)
        weak = inl is not None and (depth is None or depth > 3 or inl < 3)
        if S_T >= 0.7 and (clicks > 0 or sess > 0 or (not traffic_known and (p["authority"] or 0) >= 7)):
            bucket = "critical_fix"
        elif S_P > 0:
            bucket = "growth"
        elif ctr is not None and impr >= 1000 and ctr < 0.7 * exp:
            bucket = "ctr"
        elif S_Q > 0:
            bucket = "content"
        elif weak and (impr > 0 or sess > 0 or not traffic_known):
            bucket = "links"
        elif traffic_known and clicks == 0 and impr == 0 and sess == 0 and inl == 0:
            bucket = "consolidate"
        else:
            bucket = "monitor"
        rows.append({"url": url, "template": p["template"], "score": score, "bucket": bucket,
                     "clicks": clicks if has_gsc else None, "impressions": impr if has_gsc else None,
                     "sessions": sess if has_ga4 else None, "depth": depth, "authority": p["authority"]})
        if S_T or st or (ctr is not None and ctr < 0.7 * exp) or S_Q or weak or bucket == "consolidate":
            info[url] = {"sev": sev if S_T else None, "st": st, "ctr": ctr, "exp": exp, "pos": g.get("position"),
                         "content": content.get(url), "weak": weak, "depth": depth, "inl": inl, "consolidate": bucket == "consolidate"}
    rows.sort(key=lambda x: (-x["score"], -(x["authority"] or 0), x["url"]))

    def reasons(url: str) -> List[str]:
        i = info.get(url)
        if not i:
            return []
        out: List[str] = []
        if i["sev"]:
            out.append(f"Has a {i['sev']} fix open.")
        if i["st"]:
            st = i["st"]
            out.append(f"Ranks {st['position']:.1f} for \"{st['query']}\" with {st['impressions']:,} impressions; "
                       f"position 3 would bring about {int(st['impressions'] * EXPECTED_CTR[3]):,} clicks.")
        if i["ctr"] is not None and i["ctr"] < 0.7 * i["exp"]:
            out.append(f"Gets {i['ctr']:.1%} of clicks where position {i['pos']:.0f} usually gets {i['exp']:.1%}: "
                       "the title and description may not be winning the click.")
        if i["content"]:
            out.append("Content: " + ", ".join(i["content"]) + ".")
        if i["weak"]:
            out.append("Hard to reach: " + ("no path of links from the home page" if i["depth"] is None else f"{i['depth']} clicks from home")
                       + f", {i['inl']} page{'s' if i['inl'] != 1 else ''} link here.")
        if i["consolidate"]:
            out.append("No clicks, no impressions, no visits and no internal links: merge it into a related page or remove it.")
        return out
    counts: Dict[str, int] = defaultdict(int)
    for o in rows:
        counts[o["bucket"]] += 1
    return {"run_id": run_id, "rows": rows, "explain": reasons,
            "buckets": [{"id": k, "label": l, "pages": counts.get(k, 0)} for k, l in BUCKETS],
            "data": {"search_console": has_gsc, "ga4": has_ga4, "links": any(p["inlinks"] is not None for p in pages)},
            "confidence": round(wsum, 2), "weights": WEIGHTS,
            "basis": "QC's own prioritisation from the latest check" + (", Search Console" if has_gsc else "")
                     + (", GA4" if has_ga4 else "") + ". Not a Google ranking score."}
