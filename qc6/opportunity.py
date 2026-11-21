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


async def page_opportunities(pool, site_id: int, run_id: int, min_impressions: int = 2500) -> Dict[str, Any]:
    pages = await pool.fetch("""SELECT p.url, p.final_url, p.http_status, p.facts, coalesce(t.name, '') AS template
                                FROM pages p LEFT JOIN templates t ON t.id = p.template_id WHERE p.run_id = $1""", run_id)
    fx = await pool.fetch("""SELECT fd.page_url, f.severity, f.rule, f.subject FROM findings fd JOIN fixes f ON f.id = fd.fix_id
                             WHERE fd.run_id = $1 AND f.site_id = $2 AND f.status IN ('open', 'in_progress')""", run_id, site_id)
    worst: Dict[str, float] = defaultdict(float)
    worst_title: Dict[str, str] = {}
    content: Dict[str, set] = defaultdict(set)
    for r in fx:
        s = SEV.get(r["severity"], 0)
        if s > worst[r["page_url"]]:
            worst[r["page_url"]] = s
            worst_title[r["page_url"]] = r["severity"]
        if r["rule"] == "NEAR_DUPLICATE" or (r["rule"] == "PAGE_CHECK" and r["subject"] in CONTENT_SUBJECTS):
            content[r["page_url"]].add("near-duplicate text" if r["rule"] == "NEAR_DUPLICATE" else r["subject"].replace("_", " ").lower())
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
    out: List[Dict[str, Any]] = []
    for p in pages:
        url = p["url"]
        f = p["facts"] or {}
        g = gsc.get(url) or gsc.get(p["final_url"] or "") or {}
        path = urllib.parse.urlsplit(url).path or "/"
        sess = sessions.get(path, 0)
        depth, inl = f.get("depth"), f.get("inlinks")
        S = {"T": worst.get(url, 0.0), "Q": 1.0 if content.get(url) else 0.0, "L": 0.0, "P": 0.0, "C": 0.0, "R": 0.0}
        if inl is not None:                                  # link data exists for this check
            S["L"] = max(1.0 if depth is None else min(1.0, max(0, depth - 3) / 3), max(0.0, (3 - inl) / 3) * 0.7)
        reasons: List[str] = []
        if S["T"]:
            reasons.append(f"Has a {worst_title[url]} fix open.")
        if has_gsc and g.get("striking"):
            st = g["striking"]
            S["P"] = min(1.0, st["impressions"] / (2 * min_impressions))
            reasons.append(f"Ranks {st['position']:.1f} for \"{st['query']}\" with {st['impressions']:,} impressions; "
                           f"position 3 would bring about {int(st['impressions'] * EXPECTED_CTR[3]):,} clicks.")
        ctr = None
        if has_gsc and (g.get("impressions") or 0) >= 500 and g.get("position"):
            ctr = g["clicks"] / g["impressions"]
            exp = expected_ctr(g["position"])
            S["C"] = max(0.0, min(1.0, (exp - ctr) / exp))
            if ctr < 0.7 * exp:
                reasons.append(f"Gets {ctr:.1%} of clicks where position {g['position']:.0f} usually gets {exp:.1%}: "
                               "the title and description may not be winning the click.")
        if has_ga4 and max_sessions:
            S["R"] = math.log1p(sess) / math.log1p(max_sessions)
        if content.get(url):
            reasons.append("Content: " + ", ".join(sorted(content[url])) + ".")
        if inl is not None and (depth is None or depth > 3 or inl < 3):
            reasons.append("Hard to reach: " + ("no path of links from the home page" if depth is None else f"{depth} clicks from home")
                           + f", {inl} page{'s' if inl != 1 else ''} link here.")
        m = type_multiplier(str(f.get("site_page_type") or p["template"]))
        score = round(min(100.0, m * sum(WEIGHTS[k] * S[k] for k in avail) / wsum * 100), 1) if wsum else 0.0
        clicks, impr = (g.get("clicks") or 0), (g.get("impressions") or 0)
        traffic_known = has_gsc or has_ga4
        has_traffic = clicks > 0 or sess > 0
        if S["T"] >= 0.7 and (has_traffic or (not traffic_known and (f.get("authority") or 0) >= 7)):
            bucket = "critical_fix"
        elif S["P"] > 0:
            bucket = "growth"
        elif ctr is not None and impr >= 1000 and ctr < 0.7 * expected_ctr(g["position"]):
            bucket = "ctr"
        elif S["Q"] > 0:
            bucket = "content"
        elif inl is not None and (depth is None or depth > 3 or inl < 3) and (impr > 0 or sess > 0 or not traffic_known):
            bucket = "links"
        elif traffic_known and clicks == 0 and impr == 0 and sess == 0 and inl == 0:
            bucket = "consolidate"
            reasons.append("No clicks, no impressions, no visits and no internal links: merge it into a related page or remove it.")
        else:
            bucket = "monitor"
        out.append({"url": url, "template": p["template"], "score": score, "bucket": bucket, "reasons": reasons,
                    "clicks": clicks if has_gsc else None, "impressions": impr if has_gsc else None,
                    "sessions": sess if has_ga4 else None, "depth": depth, "authority": f.get("authority")})
    out.sort(key=lambda x: (-x["score"], -(x["authority"] or 0), x["url"]))
    counts = defaultdict(int)
    for o in out:
        counts[o["bucket"]] += 1
    return {"run_id": run_id, "pages": out, "buckets": [{"id": k, "label": l, "pages": counts.get(k, 0)} for k, l in BUCKETS],
            "data": {"search_console": has_gsc, "ga4": has_ga4, "links": any((p["facts"] or {}).get("inlinks") is not None for p in pages)},
            "confidence": round(wsum, 2), "weights": WEIGHTS,
            "basis": "QC's own prioritisation from the latest check" + (", Search Console" if has_gsc else "")
                     + (", GA4" if has_ga4 else "") + ". Not a Google ranking score."}
