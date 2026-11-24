"""Data Explorer: read-only SQL on a check's own crawl database (<data>/runs/<run>/coverage.db, SQLite).

Safe by construction, not by filtering words:
* the file is opened read-only (``mode=ro``): nothing can be written to it;
* an authorizer allows only reading (SELECT, reading columns, functions, WITH RECURSIVE): ATTACH, PRAGMA, writing
  temp tables, creating anything are refused by SQLite itself;
* one statement per query (sqlite3 refuses more), and it must start with SELECT or WITH;
* a time limit (progress handler) and a row cap per page; at most a few queries at once;
* the database is in WAL mode, so a reader never blocks the crawler writing to it, even during a check.
"""
from __future__ import annotations

import csv
import os
import re
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from qc6.settings import settings

MAX_SQL = 20_000
TIMEOUT_S = 30.0
EXPORT_TIMEOUT_S = 180.0
EXPORT_MAX_ROWS = 1_000_000
MAX_CELL = 2_000
_gate = threading.BoundedSemaphore(3)

# Friendlier names for the engine's tables (temporary views; the real tables can be queried too).
VIEWS = {
    "pages": """SELECT source_page_url AS url, http_status, initial_http_status, final_url, redirect_count, page_type,
                       business, language, page_title AS title, response_time_ms, error_type, remark, relationships_count AS links,
                       image_relationships_count AS images, crawl_state, checked_at FROM on_page_source_pages""",
    "links": """SELECT r.source_page_url, r.found_url, r.normalized_found_url AS url, r.found_url_type AS kind,
                       r.found_url_scope AS scope, r.anchor_text, r.rel, r.occurrence_count AS occurrences,
                       c.http_status, c.initial_http_status, c.final_url, c.redirect_count, c.status_bucket, c.remark,
                       c.error_type, c.response_time_ms
                  FROM on_page_link_relationships r
                  LEFT JOIN on_page_validation_cache c ON c.job_id = r.job_id AND c.normalized_url = r.normalized_found_url""",
    "url_status": """SELECT normalized_url AS url, url_scope AS scope, http_status, initial_http_status, final_url, redirect_count,
                            status_bucket, remark, error_type, error_message, response_time_ms, first_seen_source_url,
                            reuse_count, seeded_by, checked_at FROM on_page_validation_cache WHERE referenced = 1""",
    "images": """SELECT r.source_page_url, r.image_url, r.normalized_image_url AS url, r.image_type AS kind,
                        r.internal_external AS scope, r.alt_present, r.alt_text, c.http_status, c.status_bucket, c.remark,
                        c.content_type, c.content_length, c.image_issue
                   FROM on_page_image_relationships r
                   LEFT JOIN on_page_image_validation_cache c ON c.job_id = r.job_id AND c.normalized_url = r.normalized_image_url""",
    "issues": """SELECT rule_code, category, severity, page_url, element_type, found_url, http_status, final_url, detail
                   FROM on_page_issues""",
}

EXAMPLES = [
    ("Links that answer 404", "SELECT url, http_status, remark, first_seen_source_url\nFROM url_status\nWHERE http_status = 404"),
    ("Pages linking to broken URLs", "SELECT source_page_url, url, http_status\nFROM links\nWHERE http_status >= 400\nORDER BY source_page_url"),
    ("Status code counts", "SELECT http_status, COUNT(*) AS urls\nFROM url_status\nGROUP BY http_status\nORDER BY urls DESC"),
    ("Network / DNS / timeout remarks", "SELECT url, status_bucket, remark\nFROM url_status\nWHERE status_bucket IN ('Timeout', 'DNS/Network', 'SSL')"),
    ("Most reused URLs (checked once)", "SELECT url, reuse_count, http_status\nFROM url_status\nORDER BY reuse_count DESC\nLIMIT 50"),
    ("Images without alt text", "SELECT source_page_url, url\nFROM images\nWHERE alt_present = 0"),
    ("Issues by rule", "SELECT rule_code, severity, COUNT(*) AS n\nFROM issues\nGROUP BY rule_code, severity\nORDER BY n DESC"),
]

_ALLOWED = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, getattr(sqlite3, "SQLITE_RECURSIVE", 33)}
_DENY_FUNCS = {"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer"}


class QueryError(ValueError):
    pass


def db_path(run_id: int) -> str:
    return os.path.join(settings.data_dir, "runs", str(int(run_id)), "coverage.db")


def _authorizer(action, arg1, arg2, dbname, source):
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in _DENY_FUNCS:
        return sqlite3.SQLITE_DENY
    if action in _ALLOWED:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _open(run_id: int) -> sqlite3.Connection:
    path = db_path(run_id)
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5, check_same_thread=False)
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    for name, sql in VIEWS.items():
        need = set(re.findall(r"\b(on_page_\w+)\b", sql))
        if need <= have:
            con.execute(f"CREATE TEMP VIEW {name} AS {sql}")
    con.execute("PRAGMA query_only = 1")
    con.set_authorizer(_authorizer)
    return con


_COMMENTS = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)


def check_sql(sql: str) -> str:
    """Basic validation (the read-only connection and authorizer are the real guard)."""
    s = (sql or "").strip()
    if not s:
        raise QueryError("Write a query first, for example: SELECT * FROM url_status LIMIT 10")
    if len(s) > MAX_SQL:
        raise QueryError(f"The query is too long (over {MAX_SQL:,} characters).")
    s = s.rstrip().rstrip(";").rstrip()
    head = _COMMENTS.sub(" ", s).strip().split(None, 1)
    if not head or head[0].upper() not in ("SELECT", "WITH"):
        raise QueryError("Only SELECT queries are allowed here (they can start with WITH). The data can't be changed.")
    if ";" in _COMMENTS.sub(" ", re.sub(r"'(?:[^']|'')*'", "''", s)):
        raise QueryError("Run one query at a time (remove the ';' in the middle).")
    return s


def _deadline(con: sqlite3.Connection, seconds: float):
    end = time.monotonic() + seconds
    con.set_progress_handler(lambda: 1 if time.monotonic() > end else 0, 20_000)


def _cell(v: Any) -> Any:
    if v is None or isinstance(v, (int, float)):
        return v
    if isinstance(v, bytes):
        return f"<{len(v)} bytes>"
    v = str(v)
    return v if len(v) <= MAX_CELL else v[:MAX_CELL] + "…"


def _friendly(e: Exception) -> str:
    msg = str(e)
    if "interrupted" in msg:
        return f"The query took longer than {int(TIMEOUT_S)} seconds and was stopped. Add a WHERE or LIMIT."
    if "not authorized" in msg or "prohibited" in msg:
        return "Only reading is allowed here: that statement or function isn't permitted."
    if "readonly" in msg or "read-only" in msg:
        return "The crawl data is read-only here."
    return msg


def run(run_id: int, sql: str, offset: int = 0, limit: int = 100, count: bool = True) -> Dict[str, Any]:
    s = check_sql(sql)
    limit, offset = max(1, min(int(limit), 500)), max(0, int(offset))
    if not _gate.acquire(timeout=10):
        raise QueryError("Other queries are running; try again in a moment.")
    t0 = time.perf_counter()
    try:
        con = _open(run_id)
        try:
            _deadline(con, TIMEOUT_S)
            cur = con.execute(f"SELECT * FROM (\n{s}\n) LIMIT ? OFFSET ?", (limit + 1, offset))
            cols = [d[0] for d in cur.description or []]
            rows = [[_cell(v) for v in r] for r in cur.fetchall()]
            more = len(rows) > limit
            rows = rows[:limit]
            total: Optional[int] = None
            if count:
                if offset == 0 and not more:
                    total = len(rows)
                else:
                    try:
                        _deadline(con, TIMEOUT_S)
                        total = con.execute(f"SELECT COUNT(*) FROM (\n{s}\n)").fetchone()[0]
                    except sqlite3.OperationalError:
                        total = None                     # too slow to count: the page still shows
        finally:
            con.close()
    except sqlite3.Error as e:
        raise QueryError(_friendly(e)) from None
    finally:
        _gate.release()
    return {"columns": cols, "rows": rows, "total": total, "has_more": more, "offset": offset, "limit": limit,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1)}


def export_csv(run_id: int, sql: str, dest: str) -> int:
    s = check_sql(sql)
    if not _gate.acquire(timeout=10):
        raise QueryError("Other queries are running; try again in a moment.")
    n = 0
    try:
        con = _open(run_id)
        try:
            _deadline(con, EXPORT_TIMEOUT_S)
            cur = con.execute(f"SELECT * FROM (\n{s}\n) LIMIT {EXPORT_MAX_ROWS}")
            with open(dest, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow([d[0] for d in cur.description or []])
                while True:
                    chunk = cur.fetchmany(5_000)
                    if not chunk:
                        break
                    w.writerows(chunk)
                    n += len(chunk)
        finally:
            con.close()
    except sqlite3.Error as e:
        raise QueryError(_friendly(e)) from None
    finally:
        _gate.release()
    return n


def schema(run_id: int) -> Dict[str, Any]:
    path = db_path(run_id)
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        tables = []
        for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            cols = [r[1] for r in con.execute(f'PRAGMA table_info("{name}")')]
            tables.append({"name": name, "columns": cols, "kind": "table"})
        have = {t["name"] for t in tables}
        views = []
        for name, sql in VIEWS.items():
            if set(re.findall(r"\b(on_page_\w+)\b", sql)) <= have:
                cols = [c.strip().split()[-1].split(".")[-1]
                        for c in re.split(r",(?![^(]*\))", re.sub(r"\s+", " ", sql.split("FROM")[0][len(" SELECT"):]))]
                views.append({"name": name, "columns": cols, "kind": "view"})
    finally:
        con.close()
    return {"views": views, "tables": tables, "examples": [{"name": n, "sql": q} for n, q in EXAMPLES],
            "limits": {"timeout_s": TIMEOUT_S, "page_max": 500, "export_max_rows": EXPORT_MAX_ROWS}}
