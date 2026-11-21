"""Every page of a check, filterable (FEAT-03-02), what changed since the check before (FEAT-03-03), and a side-by-side
comparison of any two checks (FEAT-17-02). Everything is filtered in Postgres, paged, and exportable as CSV."""
from __future__ import annotations

import csv
import io
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

OPEN = ("open", "in_progress", "fixed_pending_check")
STATUS = {"2xx": "p.http_status BETWEEN 200 AND 299", "3xx": "p.http_status BETWEEN 300 AND 399",
          "4xx": "p.http_status BETWEEN 400 AND 499", "5xx": "p.http_status >= 500", "none": "p.http_status IS NULL"}
SORTS = {"url": "p.url", "status": "p.http_status", "fixes": "open_fixes", "type": "template_name", "depth": "depth",
         "authority": "authority", "words": "words"}


async def checks_with_pages(pool, site_id: int) -> List[Dict[str, Any]]:
    """Completed checks that have a page list, newest first."""
    rows = await pool.fetch("""SELECT r.id, r.kind, r.finished_at, (r.summary->>'pages')::int AS pages, r.options->>'imported_from' AS imported,
                                      r.options->'spot'->>'label' AS spot
                               FROM runs r WHERE r.site_id = $1 AND r.status = 'completed'
                                 AND EXISTS (SELECT 1 FROM pages p WHERE p.run_id = r.id) ORDER BY r.id DESC LIMIT 60""", site_id)
    return [dict(r) for r in rows]


async def previous_check(pool, site_id: int, run_id: int) -> Optional[int]:
    if await pool.fetchval("SELECT options ? 'spot' FROM runs WHERE id = $1", run_id):
        return None                                   # a spot check isn't compared with the whole site
    return await pool.fetchval("""SELECT max(r.id) FROM runs r WHERE r.site_id = $1 AND r.status = 'completed' AND r.id < $2
                                  AND NOT (r.options ? 'spot')
                                  AND EXISTS (SELECT 1 FROM pages p WHERE p.run_id = r.id)""", site_id, run_id)


def _filters(f: Dict[str, Any], args: List[Any]) -> List[str]:
    """SQL conditions for the explorer filters. Every value goes in as a bind parameter."""
    where: List[str] = []

    def bind(v: Any) -> str:
        args.append(v)
        return f"${len(args)}"
    if f.get("status") in STATUS:
        where.append(STATUS[f["status"]])
    if f.get("template"):
        where.append(f"p.template_id = {bind(int(f['template']))}")
    if f.get("indexable") == "no":
        where.append("(p.facts->>'noindex') IN ('1', 'true')")
    elif f.get("indexable") == "yes":
        where.append("coalesce(p.facts->>'noindex', '0') NOT IN ('1', 'true')")
    if f.get("canonical") == "missing":
        where.append("coalesce(p.facts->>'canonical_url', '') = ''")
    elif f.get("canonical") == "other":
        where.append("coalesce(p.facts->>'canonical_url', '') NOT IN ('', p.url, coalesce(p.final_url, p.url))")
    elif f.get("canonical") == "self":
        where.append("p.facts->>'canonical_url' IN (p.url, coalesce(p.final_url, p.url))")
    if f.get("depth_min") not in (None, ""):
        where.append(f"(p.facts->>'depth')::int >= {bind(int(f['depth_min']))}")
    if f.get("fixes") == "yes":
        where.append("coalesce(fc.n, 0) > 0")
    elif f.get("fixes") == "no":
        where.append("coalesce(fc.n, 0) = 0")
    if f.get("change") == "new":
        where.append("prev.url IS NULL AND $3::bigint IS NOT NULL")
    elif f.get("change") == "status":
        where.append("prev.url IS NOT NULL AND prev.http_status IS DISTINCT FROM p.http_status")
    if f.get("q"):
        where.append(f"(p.url ILIKE '%' || {bind(str(f['q'])[:300])} || '%' OR p.title ILIKE '%' || ${len(args)} || '%')")
    return where


def _query(f: Dict[str, Any], run_id: int, prev: Optional[int], site_id: int, with_fc: bool = True) -> Tuple[str, List[Any]]:
    """The explorer query. with_fc=False leaves out per-page open-fix counts (the expensive part on 300K pages);
    they are then looked up only for the rows shown."""
    args: List[Any] = [run_id, site_id, prev]
    where = _filters(f, args)
    fc_cte = f"""WITH fc AS (SELECT fd.page_url, count(DISTINCT fd.fix_id) AS n FROM findings fd JOIN fixes x ON x.id = fd.fix_id
                  WHERE fd.run_id = $1 AND x.site_id = $2 AND x.status IN {OPEN} GROUP BY fd.page_url)""" if with_fc else ""
    sql = f"""
      {fc_cte}
      SELECT p.url, p.http_status, p.final_url, p.title, t.name AS template_name, p.template_id,
             coalesce(p.facts->>'noindex', '0') IN ('1', 'true') AS noindex,
             CASE WHEN coalesce(p.facts->>'canonical_url', '') = '' THEN 'missing'
                  WHEN p.facts->>'canonical_url' IN (p.url, coalesce(p.final_url, p.url)) THEN 'self' ELSE 'other' END AS canonical,
             p.facts->>'canonical_url' AS canonical_url,
             (p.facts->>'depth')::int AS depth, (p.facts->>'authority')::float AS authority, (p.facts->>'inlinks')::int AS inlinks,
             (p.facts->>'word_count')::int AS words,
             {"coalesce(fc.n, 0)" if with_fc else "NULL::bigint"} AS open_fixes,
             CASE WHEN $3::bigint IS NULL THEN NULL WHEN prev.url IS NULL THEN 'new'
                  WHEN prev.http_status IS DISTINCT FROM p.http_status THEN 'status' ELSE NULL END AS change,
             prev.http_status AS previous_status
      FROM pages p
      LEFT JOIN templates t ON t.id = p.template_id
      {"LEFT JOIN fc ON fc.page_url = p.url" if with_fc else ""}
      LEFT JOIN pages prev ON prev.run_id = $3 AND prev.url = p.url
      WHERE p.run_id = $1 {"" if with_fc else "AND $2::bigint IS NOT NULL"} {"AND " + " AND ".join(where) if where else ""}"""
    return sql, args


_CHANGES: Dict[Tuple[int, Optional[int]], Dict[str, int]] = {}


async def changes_since(pool, run_id: int, prev: Optional[int]) -> Dict[str, int]:
    """New / changed-status / gone page counts between two checks. Fixed once both checks exist, so cached."""
    if not prev:
        return {"new": 0, "status_changed": 0, "gone": 0}
    key = (run_id, prev)
    if key not in _CHANGES:
        r = await pool.fetchrow("""SELECT count(*) FILTER (WHERE o.url IS NULL) AS new,
                                          count(*) FILTER (WHERE o.url IS NOT NULL AND o.http_status IS DISTINCT FROM p.http_status) AS status_changed
                                   FROM pages p LEFT JOIN pages o ON o.run_id = $2 AND o.url = p.url WHERE p.run_id = $1""", run_id, prev)
        gone = await pool.fetchval("""SELECT count(*) FROM pages o WHERE o.run_id = $2
                                      AND NOT EXISTS (SELECT 1 FROM pages p WHERE p.run_id = $1 AND p.url = o.url)""", run_id, prev)
        if len(_CHANGES) > 64:
            _CHANGES.clear()
        _CHANGES[key] = {"new": r["new"], "status_changed": r["status_changed"], "gone": gone}
    return _CHANGES[key]


async def list_pages(pool, site_id: int, run_id: int, f: Dict[str, Any], sort: str = "url", desc: bool = False,
                     limit: int = 100, offset: int = 0) -> Dict[str, Any]:
    prev = await previous_check(pool, site_id, run_id)
    changes = await changes_since(pool, run_id, prev)
    if f.get("change") == "gone":
        out = await _gone(pool, run_id, prev, f, limit, offset)
        out["changes"] = changes
        return out
    need_fc = f.get("fixes") in ("yes", "no") or sort == "fixes"
    count_sql, count_args = _query(f, run_id, prev, site_id, with_fc=need_fc)
    total = await pool.fetchval(f"SELECT count(*) FROM ({count_sql}) x", *count_args)
    order = SORTS.get(sort, "p.url")
    sql, args = _query(f, run_id, prev, site_id, with_fc=need_fc)
    rows = [dict(r) for r in await pool.fetch(
        f"{sql} ORDER BY {order} {'DESC' if desc else 'ASC'} NULLS LAST, p.url LIMIT {int(limit)} OFFSET {int(offset)}", *args)]
    if not need_fc and rows:                     # open fixes for just the rows on screen (index: findings run + page)
        counts = {r["page_url"]: r["n"] for r in await pool.fetch(
            f"""SELECT fd.page_url, count(DISTINCT fd.fix_id) AS n FROM findings fd JOIN fixes x ON x.id = fd.fix_id
                WHERE fd.run_id = $1 AND x.site_id = $2 AND x.status IN {OPEN} AND fd.page_url = ANY($3::text[])
                GROUP BY fd.page_url""", run_id, site_id, [r["url"] for r in rows])}
        for r in rows:
            r["open_fixes"] = counts.get(r["url"], 0)
    return {"run_id": run_id, "previous_run_id": prev, "total": total, "pages": rows, "changes": changes}


async def _gone(pool, run_id: int, prev: Optional[int], f: Dict[str, Any], limit: int, offset: int) -> Dict[str, Any]:
    if not prev:
        return {"run_id": run_id, "previous_run_id": None, "total": 0, "pages": [], "changes": {"new": 0, "status_changed": 0, "gone": 0}}
    q = str(f.get("q") or "")[:300]
    base = """FROM pages o LEFT JOIN templates t ON t.id = o.template_id WHERE o.run_id = $2
              AND NOT EXISTS (SELECT 1 FROM pages p WHERE p.run_id = $1 AND p.url = o.url) AND ($3 = '' OR o.url ILIKE '%' || $3 || '%')"""
    total = await pool.fetchval(f"SELECT count(*) {base}", run_id, prev, q)
    rows = await pool.fetch(f"""SELECT o.url, o.http_status AS previous_status, NULL::int AS http_status, o.title, t.name AS template_name,
                                       'gone' AS change {base} ORDER BY o.url LIMIT {int(limit)} OFFSET {int(offset)}""", run_id, prev, q)
    return {"run_id": run_id, "previous_run_id": prev, "total": total, "pages": [dict(r) for r in rows],
            "changes": {"gone": total}}


CSV_COLUMNS = ["url", "http_status", "template_name", "title", "noindex", "canonical", "canonical_url", "depth", "inlinks",
               "authority", "words", "open_fixes", "change", "previous_status"]


def _cell(v: Any) -> Any:
    """Spreadsheet formula injection: text starting with = + - @ tab or CR gets a leading apostrophe (as in V5)."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return "" if v is None else v


async def csv_rows(pool, site_id: int, run_id: int, f: Dict[str, Any]) -> AsyncIterator[str]:
    prev = await previous_check(pool, site_id, run_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    yield buf.getvalue()
    if f.get("change") == "gone":
        data = (await _gone(pool, run_id, prev, f, 1_000_000, 0))["pages"]
        batches = [data]
    else:
        sql, args = _query(f, run_id, prev, site_id)
        batches = None
    if batches is not None:
        for rows in batches:
            buf.seek(0); buf.truncate()
            for r in rows:
                w.writerow([_cell(r.get(c)) for c in CSV_COLUMNS])
            yield buf.getvalue()
        return
    async with pool.acquire() as conn:
        async with conn.transaction():                       # server-side cursor: constant memory at 300K rows
            cur = conn.cursor(f"{sql} ORDER BY p.url", *args, prefetch=2000)
            n = 0
            buf.seek(0); buf.truncate()
            async for r in cur:
                w.writerow([_cell(r[c]) for c in CSV_COLUMNS])
                n += 1
                if n % 2000 == 0:
                    yield buf.getvalue()
                    buf.seek(0); buf.truncate()
            yield buf.getvalue()


# ---------------------------------------------------------------- compare two checks
async def compare(pool, site_id: int, a: int, b: int, sample: int = 25) -> Dict[str, Any]:
    if a == b:
        raise ValueError("Pick two different checks.")
    a, b = min(a, b), max(a, b)                         # always older -> newer
    runs = {r["id"]: dict(r) for r in await pool.fetch(
        "SELECT id, finished_at, summary, options FROM runs WHERE site_id = $1 AND id = ANY($2::bigint[]) AND status = 'completed'",
        site_id, [a, b])}
    if set(runs) != {a, b}:
        raise LookupError("Both checks must be finished checks of this site.")
    counts = await pool.fetchrow("""SELECT (SELECT count(*) FROM pages WHERE run_id = $1) AS a, (SELECT count(*) FROM pages WHERE run_id = $2) AS b,
        (SELECT count(*) FROM pages n WHERE n.run_id = $2 AND NOT EXISTS (SELECT 1 FROM pages o WHERE o.run_id = $1 AND o.url = n.url)) AS added,
        (SELECT count(*) FROM pages o WHERE o.run_id = $1 AND NOT EXISTS (SELECT 1 FROM pages n WHERE n.run_id = $2 AND n.url = o.url)) AS removed,
        (SELECT count(*) FROM pages n JOIN pages o ON o.run_id = $1 AND o.url = n.url WHERE n.run_id = $2
            AND o.http_status IS DISTINCT FROM n.http_status) AS status_changed""", a, b)
    added = await pool.fetch("""SELECT n.url, n.http_status FROM pages n WHERE n.run_id = $2
        AND NOT EXISTS (SELECT 1 FROM pages o WHERE o.run_id = $1 AND o.url = n.url) ORDER BY n.url LIMIT $3""", a, b, sample)
    removed = await pool.fetch("""SELECT o.url, o.http_status FROM pages o WHERE o.run_id = $1
        AND NOT EXISTS (SELECT 1 FROM pages n WHERE n.run_id = $2 AND n.url = o.url) ORDER BY o.url LIMIT $3""", a, b, sample)
    changed = await pool.fetch("""SELECT n.url, o.http_status AS before, n.http_status AS after FROM pages n
        JOIN pages o ON o.run_id = $1 AND o.url = n.url WHERE n.run_id = $2 AND o.http_status IS DISTINCT FROM n.http_status
        ORDER BY (n.http_status >= 400) DESC, n.url LIMIT $3""", a, b, sample)
    ev = await pool.fetch("""SELECT e.kind, f.id, f.title, f.severity, f.pages_affected FROM fix_events e JOIN fixes f ON f.id = e.fix_id
        WHERE f.site_id = $1 AND e.run_id > $2 AND e.run_id <= $3 AND e.kind IN ('found', 'verified', 'regressed')
        ORDER BY CASE f.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, f.pages_affected DESC""",
                         site_id, a, b)
    fixes: Dict[str, List[Dict[str, Any]]] = {"new": [], "confirmed": [], "back": []}
    seen: Dict[str, set] = {k: set() for k in fixes}
    for r in ev:
        k = {"found": "new", "verified": "confirmed", "regressed": "back"}[r["kind"]]
        if r["id"] not in seen[k]:
            seen[k].add(r["id"])
            fixes[k].append({"id": r["id"], "title": r["title"], "severity": r["severity"], "pages": r["pages_affected"]})
    sc = {r["run_id"]: dict(r) for r in await pool.fetch(
        "SELECT run_id, score, dimensions FROM score_snapshots WHERE run_id = ANY($1::bigint[])", [a, b])}
    score = None
    if a in sc and b in sc:
        da, db = sc[a]["dimensions"] or {}, sc[b]["dimensions"] or {}
        score = {"before": sc[a]["score"], "after": sc[b]["score"], "change": round(sc[b]["score"] - sc[a]["score"], 1),
                 "dimensions": {k: round(float(db.get(k, 0)) - float(da.get(k, 0)), 1) for k in db}}
    tpl = await pool.fetch("""SELECT coalesce(t.name, 'Other pages') AS name,
                                     count(*) FILTER (WHERE p.run_id = $1) AS before, count(*) FILTER (WHERE p.run_id = $2) AS after
                              FROM pages p LEFT JOIN templates t ON t.id = p.template_id WHERE p.run_id IN ($1, $2)
                              GROUP BY 1 ORDER BY after DESC""", a, b)
    return {"a": {"id": a, "finished_at": runs[a]["finished_at"]}, "b": {"id": b, "finished_at": runs[b]["finished_at"]},
            "pages": {"before": counts["a"], "after": counts["b"], "added": counts["added"], "removed": counts["removed"],
                      "status_changed": counts["status_changed"]},
            "samples": {"added": [dict(r) for r in added], "removed": [dict(r) for r in removed], "status_changed": [dict(r) for r in changed]},
            "fixes": {k: {"count": len(v), "items": v[:sample]} for k, v in fixes.items()},
            "score": score, "page_types": [dict(r) for r in tpl]}
