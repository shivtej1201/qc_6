"""Findings -> fixes -> lifecycle -> score. Shared by every worker (blueprint §5 step 7-8, §10.4)."""
from __future__ import annotations

import json
import os
import urllib.parse
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from qc6.domain import fixes as fx
from qc6.domain import rules as R
from qc6.domain import score as S


@dataclass
class Item:
    rule: str
    page_url: str
    source: str                         # coverage | visit | journey | integration
    template_id: Optional[int] = None
    subject: str = ""                   # part of the fix key (broken target, a11y rule id …)
    fields: Dict[str, Any] = field(default_factory=dict)    # rendering values (value, subject text …)
    evidence: Dict[str, Any] = field(default_factory=dict)


async def notify(conn, run_id: int, event: str, **data) -> None:
    payload = json.dumps({"run": run_id, "event": event, **data}, default=str)[:7900]
    await conn.execute("SELECT pg_notify('qc6', $1)", payload)


MAX_PAGES_PER_FIX = int(os.environ.get("QC6_MAX_PAGES_PER_FIX", "20000"))   # example pages stored per fix per check


BOILERPLATE = {"Header", "Footer", "Navigation", "Modal / popup", "Header/Footer"}


def _group_of(rows, scope: str, template_names: Dict[int, str]) -> tuple:
    """(component, template label) of a fix for Template x Component x Issue tickets: the component most of its
    findings are in; the template of its pages, or 'Site-wide' when a header / footer / navigation link (or any
    problem) spans several templates."""
    from collections import Counter
    from qc6.components import component as comp_of
    comps = Counter()
    tpls = Counter()
    for r in rows:
        ev = r.evidence if isinstance(r.evidence, dict) else {}
        comps[ev.get("component") or comp_of(r.rule, subject=r.subject)] += 1
        tpls[r.template_id] += 1
    comp = comps.most_common(1)[0][0] if comps else ""
    if scope == "template" or len(tpls) <= 1:
        tid = next(iter(tpls)) if tpls else None
        label = template_names.get(tid, "") if tid is not None else ""
        return comp, label or "Unclassified pages"
    if comp in BOILERPLATE:
        return comp, "Site-wide"
    top, n = tpls.most_common(1)[0]
    if n * 2 > sum(tpls.values()) and top is not None:
        return comp, template_names.get(top, "") or "Unclassified pages"
    return comp, f"Several templates ({len(tpls)})"


async def apply(pool, run_id: int, site_id: int, items: Iterable[Item],
                template_pages: Optional[Dict[int, int]] = None, template_names: Optional[Dict[int, str]] = None,
                project_templates: bool = False, spot: bool = False) -> Dict[str, int]:
    """spot=True (a check of a few chosen URLs): findings are recorded, but existing fixes keep their site-wide
    title, page count and last-seen check; only genuinely new problems create a fix."""
    """Upsert fixes for these findings. With project_templates=True (sampled visits), a template-scoped fix that was
    seen on at least half of a template's sampled visits counts every page of that template as affected."""
    template_pages = template_pages or {}
    template_names = template_names or {}
    if spot:                                     # on a few pages, "nothing links here" or "duplicate of" means nothing
        items = [it for it in items if not cross_page(it.rule, it.subject)]
    groups: Dict[str, Dict[str, Any]] = {}
    for it in items:
        rule = R.RULES[it.rule]
        path = urllib.parse.urlsplit(it.page_url).path or "/"
        key = fx.fix_key(it.rule, rule.scope, it.template_id, it.subject, path)
        g = groups.setdefault(key, {"item": it, "pages": set(), "rows": []})
        g["pages"].add(it.page_url)
        g["rows"].append(it)
    new = updated = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for key, g in groups.items():
                it: Item = g["item"]
                rule = R.RULES[it.rule]
                n = len(g["pages"])
                if project_templates and rule.scope == "template" and it.template_id in template_pages:
                    n = max(n, template_pages[it.template_id])   # caller passes only findings seen on >= half the sample
                tname = template_names.get(it.template_id, "") if it.template_id is not None else ""
                comp, ttpl = _group_of(g["rows"], rule.scope, template_names)
                texts = R.render(it.rule, n_pages=n, template=tname or "matching", example=it.page_url,
                                 **{"subject": it.subject, **it.fields})
                row = await conn.fetchrow(
                    """INSERT INTO fixes (site_id, fix_key, rule, severity, dimension, scope, template_id, subject, title,
                                          pages_affected, first_seen_run, last_seen_run, source, texts, example_url,
                                          component, ticket_template)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$11,$12,$13,$14,$15,$16)
                       ON CONFLICT (site_id, fix_key) DO UPDATE SET """ + ("""updated_at = fixes.updated_at,
                         component = CASE WHEN fixes.component = '' THEN EXCLUDED.component ELSE fixes.component END,
                         ticket_template = CASE WHEN fixes.ticket_template = '' THEN EXCLUDED.ticket_template ELSE fixes.ticket_template END"""
                                                                             if spot else """
                         title = EXCLUDED.title, pages_affected = EXCLUDED.pages_affected, last_seen_run = EXCLUDED.last_seen_run,
                         texts = EXCLUDED.texts, example_url = EXCLUDED.example_url, component = EXCLUDED.component,
                         ticket_template = EXCLUDED.ticket_template, updated_at = now()""") + """
                       RETURNING id, (xmax = 0) AS inserted""",
                    site_id, key, it.rule, rule.severity, rule.dimension, rule.scope, it.template_id, it.subject,
                    texts["title"], n, run_id, it.source, texts, it.page_url, comp, ttpl)
                if row["inserted"]:
                    new += 1
                    await conn.execute("INSERT INTO fix_events (fix_id, kind, run_id, detail) VALUES ($1,'found',$2,$3)",
                                       row["id"], run_id, {"pages": n})
                else:
                    updated += 1
                await conn.executemany(
                    "INSERT INTO findings (run_id, fix_id, page_url, subject, evidence, source) VALUES ($1,$2,$3,$4,$5,$6) "
                    "ON CONFLICT DO NOTHING",
                    [(run_id, row["id"], r.page_url, r.subject, r.evidence, r.source) for r in g["rows"][:MAX_PAGES_PER_FIX]])
    return {"fixes_new": new, "fixes_updated": updated, "findings": sum(len(g["rows"]) for g in groups.values())}


# Problems that depend on other pages (links into a page, duplicates, the sitemap): a spot check can't judge them, so it
# never confirms them fixed. Only a check of the whole site can.
CROSS_PAGE_RULES = {"NEAR_DUPLICATE", "DEEP_PAGE", "NOT_REACHABLE_FROM_HOME", "TITLE_DUPLICATE", "CANONICAL_CHAIN",
                    "NOINDEX_IN_SITEMAP", "CANONICAL_ELSEWHERE", "PRODUCT_DATA_MISSING"}
CROSS_PAGE_SUBJECTS = {"ORPHAN_PAGE", "NOT_IN_SITEMAP", "DUPLICATE_TITLE", "DUPLICATE_META_DESCRIPTION", "DUPLICATE_H1",
                       "DUPLICATE_CONTENT", "TRAILING_SLASH_DUPLICATE", "NOINDEX_IN_SITEMAP", "SITEMAP_URL_NOT_CANONICAL"}


def cross_page(rule: str, subject: str) -> bool:
    return rule in CROSS_PAGE_RULES or (subject or "") in CROSS_PAGE_SUBJECTS or (subject or "").startswith("SITEMAP_")


async def spot_pages(pool, run_id: int) -> Optional[set]:
    """The URLs of a spot check (single URL or uploaded list), or None for a check of the whole site."""
    o = await pool.fetchval("SELECT options FROM runs WHERE id = $1", run_id) or {}
    return set(o.get("_urls") or o.get("urls") or []) if o.get("spot") else None


async def close_run(pool, run_id: int, site_id: int, sources: List[str], complete: bool = True,
                    pages: Optional[set] = None) -> Dict[str, int]:
    """Lifecycle for fixes of the given sources after a run. Only a complete run can verify a fix.
    pages: for a spot check, the URLs it covered; a fix it didn't see is verified only if every page the fix was last
    found on is among them (a check of 3 URLs says nothing about the other 3,00,000 pages)."""
    moved = defaultdict(int)
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch("SELECT id, status, last_seen_run, rule, subject FROM fixes WHERE site_id = $1 AND source = ANY($2::text[])",
                                    site_id, sources)
            seen_ids = {r["fix_id"] for r in await conn.fetch("SELECT DISTINCT fix_id FROM findings WHERE run_id = $1", run_id)}
            covered: Dict[int, bool] = {}
            if pages is not None:
                unseen = [r["id"] for r in rows if r["id"] not in seen_ids]
                for r in await conn.fetch("""SELECT fd.fix_id, bool_and(fd.page_url = ANY($2::text[])) AS all_in FROM findings fd
                                             JOIN fixes f ON f.id = fd.fix_id AND fd.run_id = f.last_seen_run
                                             WHERE fd.fix_id = ANY($1::bigint[]) GROUP BY fd.fix_id""", unseen, list(pages)):
                    covered[r["fix_id"]] = r["all_in"]
            for r in rows:
                seen = r["id"] in seen_ids
                if not seen and not complete:
                    continue
                if not seen and pages is not None and (not covered.get(r["id"], False) or cross_page(r["rule"], r["subject"])):
                    continue
                new_status, regressed = fx.after_run(r["status"], seen)
                if new_status != r["status"] or regressed:
                    await conn.execute("UPDATE fixes SET status = $2, regressed = $3, updated_at = now() WHERE id = $1",
                                       r["id"], new_status, regressed)
                    kind = "regressed" if regressed else ("verified" if new_status == "verified" else "status")
                    await conn.execute("INSERT INTO fix_events (fix_id, kind, run_id, detail) VALUES ($1,$2,$3,$4)",
                                       r["id"], kind, run_id, {"from": r["status"], "to": new_status})
                    moved[kind] += 1
    return dict(moved)


async def snapshot_score(pool, run_id: int, site_id: int, pages_total: int) -> Dict[str, Any]:
    async with pool.acquire() as conn:
        open_fixes = await conn.fetch("SELECT severity, dimension, pages_affected FROM fixes WHERE site_id = $1 "
                                      "AND status IN ('open', 'in_progress', 'fixed_pending_check') "
                                      "AND rule NOT LIKE 'ENV\\_URL\\_%'", site_id)   # the environment-URL audit is additive
        sc = S.compute(pages_total, [dict(r) for r in open_fixes])
        if sc["score"] is not None:
            await conn.execute("INSERT INTO score_snapshots (site_id, run_id, formula, score, dimensions) VALUES ($1,$2,$3,$4,$5) "
                               "ON CONFLICT (run_id, formula) DO UPDATE SET score = EXCLUDED.score, dimensions = EXCLUDED.dimensions",
                               site_id, run_id, sc["formula"], sc["score"], sc["dimensions"])
    return sc
