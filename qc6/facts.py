"""Link and image facts in Parquet, queried with DuckDB (blueprint §4.1).

A 300K-page run has ~11M link and ~7.5M image relationships. They don't belong in Postgres: after the coverage
crawl they are streamed from the engine's SQLite into Parquet (100K-row row groups, zstd) under
<data>/runs/<run>/facts/{links,images}.parquet, joined with each unique URL's validation result, and the explorer
queries them with DuckDB (columnar scans; filters and paging push down into the file).
"""
from __future__ import annotations

import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional, Tuple

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from qc6.settings import settings

BATCH = 100_000

LINKS_SQL = """
SELECT r.source_page_url, r.found_url, r.normalized_found_url AS url, r.found_url_type AS kind, r.found_url_scope AS scope,
       COALESCE(r.anchor_text, '') AS anchor_text, r.occurrence_count AS occurrences, COALESCE(r.representative_location, '') AS location,
       c.http_status, c.initial_http_status, COALESCE(c.final_url, '') AS final_url, COALESCE(c.redirect_count, 0) AS redirects,
       COALESCE(c.status_bucket, '') AS status_bucket, COALESCE(c.response_time_ms, 0) AS response_ms,
       COALESCE(r.rel, '') AS rel, CASE WHEN lower(COALESCE(r.rel, '')) LIKE '%nofollow%' THEN 1 ELSE 0 END AS nofollow,
       COALESCE(r.anchor_texts, '[]') AS anchor_texts
  FROM on_page_link_relationships r
  LEFT JOIN on_page_validation_cache c ON c.job_id = r.job_id AND c.normalized_url = r.normalized_found_url
 WHERE r.job_id = ? AND r.id > ? ORDER BY r.id LIMIT ?"""
IMAGES_SQL = """
SELECT r.source_page_url, r.image_url, r.normalized_image_url AS url, r.image_type AS kind, r.internal_external AS scope,
       r.alt_present, COALESCE(r.alt_text, '') AS alt_text, r.width, r.height, COALESCE(r.loading, '') AS loading,
       r.occurrence_count AS occurrences, c.http_status, COALESCE(c.status_bucket, '') AS status_bucket,
       COALESCE(c.content_type, '') AS content_type, c.content_length, COALESCE(c.image_issue, '') AS image_issue
  FROM on_page_image_relationships r
  LEFT JOIN on_page_image_validation_cache c ON c.job_id = r.job_id AND c.normalized_url = r.normalized_image_url
 WHERE r.job_id = ? AND r.id > ? ORDER BY r.id LIMIT ?"""

LINK_SCHEMA = pa.schema([("source_page_url", pa.string()), ("found_url", pa.string()), ("url", pa.string()), ("kind", pa.string()),
                         ("scope", pa.string()), ("anchor_text", pa.string()), ("occurrences", pa.int32()), ("location", pa.string()),
                         ("http_status", pa.int32()), ("initial_http_status", pa.int32()), ("final_url", pa.string()),
                         ("redirects", pa.int32()), ("status_bucket", pa.string()), ("response_ms", pa.float64()),
                         ("rel", pa.string()), ("nofollow", pa.int32()), ("anchor_texts", pa.string())])
IMAGE_SCHEMA = pa.schema([("source_page_url", pa.string()), ("image_url", pa.string()), ("url", pa.string()), ("kind", pa.string()),
                          ("scope", pa.string()), ("alt_present", pa.int32()), ("alt_text", pa.string()), ("width", pa.string()),
                          ("height", pa.string()), ("loading", pa.string()), ("occurrences", pa.int32()), ("http_status", pa.int32()),
                          ("status_bucket", pa.string()), ("content_type", pa.string()), ("content_length", pa.int64()),
                          ("image_issue", pa.string())])


def facts_dir(run_id: int) -> str:
    return os.path.join(settings.data_dir, "runs", str(run_id), "facts")


def _coerce(rows: List[Tuple], schema: pa.Schema) -> pa.Table:
    cols = list(zip(*rows)) if rows else [[] for _ in schema]
    arrays = []
    for f, col in zip(schema, cols):
        if pa.types.is_integer(f.type):
            col = [None if v in (None, "") else int(float(v)) for v in col]
        elif pa.types.is_floating(f.type):
            col = [None if v in (None, "") else float(v) for v in col]
        else:
            col = [None if v is None else str(v) for v in col]
        arrays.append(pa.array(col, type=f.type))
    return pa.Table.from_arrays(arrays, schema=schema)


def export(db_path: str, job: str, run_id: int) -> Dict[str, int]:
    """Stream relationships from the engine's SQLite into Parquet. Memory stays bounded (one 100K batch)."""
    out = facts_dir(run_id)
    os.makedirs(out, exist_ok=True)
    counts = {}
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        for name, sql, schema in (("links", LINKS_SQL, LINK_SCHEMA), ("images", IMAGES_SQL, IMAGE_SCHEMA)):
            path = os.path.join(out, f"{name}.parquet")
            tmp = path + ".tmp"
            writer = pq.ParquetWriter(tmp, schema, compression="zstd")
            last, n = 0, 0
            try:
                id_sql = sql.replace("SELECT ", "SELECT r.id AS _id, ", 1)
                while True:
                    rows = c.execute(id_sql, (job, last, BATCH)).fetchall()
                    if not rows:
                        break
                    last = rows[-1][0]
                    writer.write_table(_coerce([r[1:] for r in rows], schema), row_group_size=BATCH)
                    n += len(rows)
            finally:
                writer.close()
            os.replace(tmp, path)
            counts[name] = n
    finally:
        c.close()
    return counts


# ------------------------------------------------------------------ queries (DuckDB)
_local = threading.local()

FILTERS = {
    "broken": "status_bucket IN ('BROKEN','CLIENT_ERROR','SERVER_ERROR','NOT_FOUND') OR http_status >= 400",
    "redirect": "redirects > 0 OR (http_status BETWEEN 300 AND 399)",
    "external": "scope = 'EXTERNAL'",
    "internal": "scope = 'INTERNAL'",
    "ok": "http_status BETWEEN 200 AND 299 AND redirects = 0",
    "missing_alt": "alt_present = 0",
    "nofollow": "nofollow = 1",                       # per-link rel=nofollow (CrawlPulse link graph: is_nofollow on every edge)
}
IMAGE_FILTERS = {**FILTERS, "redirect": "http_status BETWEEN 300 AND 399",
                 "ok": "http_status BETWEEN 200 AND 299"}


def _filters(dataset: str) -> Dict[str, str]:
    return ({k: v for k, v in IMAGE_FILTERS.items() if k != "nofollow"} if dataset == "images"
            else {k: v for k, v in FILTERS.items() if k != "missing_alt"})


SORTS = {"page": "source_page_url", "url": "url", "status": "http_status", "occurrences": "occurrences"}


def _db() -> duckdb.DuckDBPyConnection:
    con = getattr(_local, "con", None)
    if con is None:
        con = _local.con = duckdb.connect(":memory:")
        con.execute("SET threads TO 2")
    return con


def query(run_id: int, dataset: str, flt: str = "", q: str = "", page_url: str = "", sort: str = "page",
          desc: bool = False, offset: int = 0, limit: int = 100) -> Dict[str, Any]:
    if dataset not in ("links", "images"):
        raise ValueError("dataset must be links or images")
    path = os.path.join(facts_dir(run_id), f"{dataset}.parquet")
    if not os.path.exists(path):
        raise FileNotFoundError(dataset)
    where, args = ["true"], []
    if flt:
        if flt not in _filters(dataset):
            raise ValueError("unknown filter")
        if flt == "nofollow" and "nofollow" not in pq.read_schema(path).names:
            raise ValueError("This check was made before nofollow links were recorded; run a new check")
        where.append(f"({_filters(dataset)[flt]})")
    if q:
        where.append("(url ILIKE ? OR source_page_url ILIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    if page_url:
        where.append("source_page_url = ?")
        args.append(page_url)
    order = SORTS.get(sort, "source_page_url") + (" DESC" if desc else "") + ", url"
    w = " AND ".join(where)
    con = _db()
    total = con.execute(f"SELECT COUNT(*) FROM read_parquet(?) WHERE {w}", [path, *args]).fetchone()[0]
    cur = con.execute(f"SELECT * FROM read_parquet(?) WHERE {w} ORDER BY {order} LIMIT ? OFFSET ?",
                      [path, *args, max(1, min(int(limit), 500)), max(0, int(offset))])
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    summary = con.execute(f"""SELECT COUNT(*) AS n_rows, COUNT(DISTINCT url) AS n_unique,
                              COUNT(*) FILTER (WHERE {_filters(dataset)['broken']}) AS n_broken,
                              COUNT(*) FILTER (WHERE {_filters(dataset)['redirect']}) AS n_redirects FROM read_parquet(?)""", [path]).fetchone()
    return {"total": total, "rows": rows, "summary": dict(zip(("rows", "unique_urls", "broken", "redirects"), summary))}


def export_csv(run_id: int, dataset: str, dest: str, flt: str = "") -> int:
    """CSV straight from Parquet with DuckDB's COPY (fast, streams). Returns rows written."""
    if dataset not in ("links", "images"):
        raise ValueError("dataset must be links or images")
    path = os.path.join(facts_dir(run_id), f"{dataset}.parquet")
    if not os.path.exists(path):
        raise FileNotFoundError(dataset)
    w = f"WHERE {_filters(dataset)[flt]}" if flt in _filters(dataset) else ""
    have = set(pq.read_schema(path).names)          # checks made before a column existed export without it
    schema = [f for f in (LINK_SCHEMA if dataset == "links" else IMAGE_SCHEMA) if f.name in have]
    # Spreadsheet formula injection: a text cell starting with = + - @ tab or CR gets a leading apostrophe (as in V5).
    cols = ", ".join(
        "CASE WHEN regexp_matches(\"%s\", '^[=+@\\t\\r-]') THEN chr(39) || \"%s\" ELSE \"%s\" END AS \"%s\"" % ((f.name,) * 4)
        if pa.types.is_string(f.type) else '"%s"' % f.name for f in schema)
    src = path.replace("'", "''")
    dst = dest.replace("'", "''")
    con = duckdb.connect(":memory:")
    try:
        con.execute(f"COPY (SELECT {cols} FROM (SELECT * FROM read_parquet('{src}') {w})) TO '{dst}' (HEADER, DELIMITER ',')")
        return con.execute(f"SELECT COUNT(*) FROM read_parquet('{path}') {w}").fetchone()[0]
    finally:
        con.close()


# ------------------------------------------------------------------ anchor text (CrawlPulse 3 link intelligence)
GENERIC_ANCHORS = ("click here", "here", "read more", "more", "learn more", "know more", "find out more", "this", "link",
                   "click", "details", "more details", "view more", "see more", "view details", "continue", "continue reading",
                   "go", "this page", "website", "page", "visit", "check it out", "explore", "discover more")


def anchors(run_id: int, limit: int = 50) -> Dict[str, Any]:
    """Internal anchor-text report: generic anchors ("click here"), links with no text, the most used anchors, and
    pages that every link calls the same thing (or a different thing every time)."""
    path = os.path.join(facts_dir(run_id), "links.parquet")
    if not os.path.exists(path):
        raise FileNotFoundError("links")
    con = _db()
    have = set(pq.read_schema(path).names)
    # one row per (page, target, text): a page can link to the same target with several texts ("Page 1", "Read more")
    texts = ("CASE WHEN json_array_length(anchor_texts) > 0 THEN from_json(anchor_texts, '[\"VARCHAR\"]') ELSE [anchor_text] END"
             if "anchor_texts" in have else "[anchor_text]")
    src = ("(SELECT url, source_page_url, lower(trim(regexp_replace(t, '\\s+', ' ', 'g'))) AS a, t AS anchor_text FROM "
           f"(SELECT url, source_page_url, unnest({texts}) AS t FROM read_parquet(?) WHERE scope = 'INTERNAL' AND kind = 'ANCHOR'))")
    gen = list(GENERIC_ANCHORS)
    total = con.execute(f"SELECT count(*) FROM {src}", [path]).fetchone()[0]
    generic = con.execute(f"""SELECT a, count(*) AS links, count(DISTINCT source_page_url) AS pages, any_value(url) AS example_target,
                                     any_value(source_page_url) AS example_page
                              FROM {src} WHERE a IN (SELECT unnest(?::VARCHAR[])) GROUP BY a ORDER BY links DESC""", [path, gen]).fetchall()
    empty = con.execute(f"""SELECT count(*), count(DISTINCT source_page_url) FROM {src} WHERE a = ''""", [path]).fetchone()
    empty_examples = con.execute(f"SELECT source_page_url, url FROM {src} WHERE a = '' LIMIT 10", [path]).fetchall()
    top = con.execute(f"""SELECT any_value(anchor_text), count(*) AS links, count(DISTINCT url) AS targets FROM {src}
                          WHERE a <> '' GROUP BY a ORDER BY links DESC LIMIT ?""", [path, limit]).fetchall()
    per_target = con.execute(f"""WITH t AS (SELECT url, a, count(*) AS n FROM {src} WHERE a <> '' GROUP BY url, a),
                                    s AS (SELECT url, sum(n) AS links, count(*) AS variants, max(n) AS top_n,
                                                 arg_max(a, n) AS top_anchor FROM t GROUP BY url)
                                 SELECT url, links, variants, top_anchor, round(100.0 * top_n / links, 1) AS top_share
                                 FROM s WHERE links >= 10 ORDER BY links DESC LIMIT 500""", [path]).fetchall()
    same = [r for r in per_target if r[4] >= 95 and r[3] not in gen]
    scattered = [r for r in per_target if r[2] >= 8 and r[4] < 20]
    gen_links = sum(r[1] for r in generic)
    return {
        "internal_links": total, "generic_links": gen_links, "generic_share": round(100.0 * gen_links / total, 1) if total else 0.0,
        "generic": [{"anchor": r[0], "links": r[1], "pages": r[2], "example_target": r[3], "example_page": r[4]} for r in generic],
        "empty": {"links": empty[0], "pages": empty[1], "examples": [{"page": a, "target": b} for a, b in empty_examples]},
        "top": [{"anchor": r[0], "links": r[1], "targets": r[2]} for r in top],
        "same_everywhere": [{"url": r[0], "links": r[1], "anchor": r[3], "share": r[4]} for r in same[:50]],
        "scattered": [{"url": r[0], "links": r[1], "variants": r[2], "top_anchor": r[3], "share": r[4]} for r in scattered[:50]],
        "basis": "Internal <a> links of this check; every different text a page uses for a target counts once, compared case- "
                 "and space-insensitively. 'No text' counts targets a page links to only without any text (an image link "
                 "with no alt text, an empty <a>).",
    }
