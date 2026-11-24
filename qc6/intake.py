"""One intake for every way URLs reach QC: manual entry, a sitemap (index -> child -> sub-sitemaps), an uploaded sheet
(CSV / TSV / TXT / XLSX, any size up to QC6_MAX_UPLOAD_MB), a sitemap file saved from a browser, and URLs added to a
list later from another source.

    any source -> read in chunks -> clean -> normalize (url_lists.dedup_key) -> global dedup (across every source of
    the list) -> the list = the crawl queue -> the one crawl engine (pages -> links / images -> each unique URL
    validated once -> report -> tickets -> e-mail)

Nothing is processed differently because of where a URL came from. Each unique URL keeps every source that listed it
and where it was first seen (sheet row and its cells, sitemap, manual), so the report can say where it came from.
Rows that can't be used are kept with the reason (not a URL, another site, outside the crawl scope).
"""
from __future__ import annotations

import asyncio
import csv
import io
import os
import urllib.parse
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

from qc6.url_lists import HEADERS, MAX_URLS, ListError, _clean, dedup_key

CHUNK = 5_000
MAX_REJECTS = 20_000                 # kept per list (counted beyond that)
MAX_CONTEXT_CELLS = 12
MAX_CELL = 200
MAX_UPLOAD_BYTES = int(os.environ.get("QC6_MAX_UPLOAD_MB", "100")) * 1024 * 1024


def _host(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


class Intake:
    """Normalize + deduplicate URLs from any number of sources; counts per source for the progress view."""

    def __init__(self, primary_host: str = "", scope: Optional[Dict[str, Any]] = None):
        self.primary = (primary_host or "").lower()
        self.scope = scope
        self.keys: Dict[str, int] = {}
        self.urls: List[str] = []
        self.sources: List[List[str]] = []
        self.refs: List[Dict[str, Any]] = []
        self.by_source: Dict[str, Dict[str, int]] = {}
        self.rejects: List[Tuple[str, Dict[str, Any], str, str]] = []
        self.rejected_total = 0
        self.capped = False

    # -- seeding from a list that already exists (adding another source to it)
    def preload(self, rows: Iterable[Tuple[str, List[str], Dict[str, Any]]]) -> None:
        for url, sources, ref in rows:
            self.keys[dedup_key(url)] = len(self.urls)
            self.urls.append(url)
            self.sources.append(list(sources))
            self.refs.append(dict(ref or {}))

    def _count(self, source: str, field: str, n: int = 1) -> None:
        c = self.by_source.setdefault(source, {"rows": 0, "valid": 0, "invalid": 0, "duplicates": 0, "other_hosts": 0,
                                               "outside_scope": 0, "added": 0})
        c[field] += n

    def _reject(self, source: str, ref: Dict[str, Any], value: Any, reason: str, field: str) -> None:
        self._count(source, field)
        self.rejected_total += 1
        if len(self.rejects) < MAX_REJECTS:
            self.rejects.append((source, ref, str(value if value is not None else "")[:2000], reason))

    def host_ok(self, url: str) -> bool:
        if not self.primary:
            return True
        from qc6.site_access import is_site_host
        h = _host(url)
        bare = self.primary[4:] if self.primary.startswith("www.") else self.primary
        return is_site_host(h, self.primary) or h == bare or h.endswith("." + bare)

    def add(self, raw: Any, source: str, ref: Optional[Dict[str, Any]] = None, already_clean: bool = False) -> str:
        """One candidate. Returns 'added' | 'duplicate' | 'invalid' | 'other_host' | 'outside_scope' | 'capped'."""
        ref = ref or {}
        self._count(source, "rows")
        url = raw if already_clean else _clean(raw)
        if not url:
            self._reject(source, ref, raw, "Not a web address (must start with http:// or https://)", "invalid")
            return "invalid"
        self._count(source, "valid")
        if not self.host_ok(url):
            self._reject(source, ref, url, f"Another site (not {self.primary})", "other_hosts")
            return "other_host"
        if self.scope:
            from qc6.site_access import in_scope
            if not in_scope(url, self.scope):
                self._reject(source, ref, url, "Outside the site's crawl scope", "outside_scope")
                return "outside_scope"
        k = dedup_key(url)
        i = self.keys.get(k)
        if i is not None:                                    # the same page from this or another source: one job
            self._count(source, "duplicates")
            if source not in self.sources[i]:
                self.sources[i].append(source)
            return "duplicate"
        if len(self.urls) >= MAX_URLS:
            self.capped = True
            self._reject(source, ref, url, f"Over {MAX_URLS:,} pages in one check", "invalid")
            return "capped"
        self.keys[k] = len(self.urls)
        self.urls.append(url)
        self.sources.append([source])
        self.refs.append(ref)
        self._count(source, "added")
        return "added"

    def summary(self) -> Dict[str, Any]:
        tot = {f: sum(c[f] for c in self.by_source.values())
               for f in ("rows", "valid", "invalid", "duplicates", "other_hosts", "outside_scope", "added")}
        return {**tot, "queued": len(self.urls), "rejected": self.rejected_total, "capped": self.capped,
                "by_source": self.by_source}

    def sitemap_view(self) -> Tuple[List[str], List[int]]:
        """Each URL's first source as the report's 'sitemap' column: real sitemap addresses for sitemap pages,
        'Sheet: a.xlsx' / 'Manual entry' for the rest (the report's Sitemap Progress then shows every source)."""
        names: List[str] = []
        index: Dict[str, int] = {}
        idx: List[int] = []
        for srcs, ref in zip(self.sources, self.refs):
            name = ref.get("sitemap") or srcs[0]
            if name not in index:
                index[name] = len(names)
                names.append(name)
            idx.append(index[name])
        return names, idx


# ------------------------------------------------------------------ reading sources in chunks
def _cells(row: List[Any]) -> List[str]:
    return ["" if v is None else str(v)[:MAX_CELL] for v in row[:MAX_CONTEXT_CELLS]]


def sheet_rows(path: str, filename: str) -> Iterator[Tuple[int, Any, List[str], Optional[List[str]]]]:
    """Stream a sheet from disk: (row number, URL cell or None for 'every http cell', the row's cells, header).
    Constant memory: CSV is read line by line, XLSX in read-only mode."""
    name = (filename or "").lower()
    if name.endswith(".xls"):
        raise ListError("Old .xls files aren't supported. Save the sheet as .xlsx or CSV.")
    if name.endswith((".xlsx", ".xlsm")):
        import openpyxl
        try:
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        except Exception as e:
            raise ListError(f"That Excel file can't be read ({type(e).__name__}). Save it again as .xlsx or CSV.")
        try:
            yield from _rows_with_header((list(r) for r in wb.worksheets[0].iter_rows(values_only=True)))
        finally:
            wb.close()
        return
    with open(path, "rb") as fb:
        head = fb.read(8192)
    try:
        dialect = csv.Sniffer().sniff(head.decode("utf-8-sig", errors="replace")[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
        yield from _rows_with_header(csv.reader(f, dialect))


def _rows_with_header(rows: Iterable[List[Any]]) -> Iterator[Tuple[int, Any, List[str], Optional[List[str]]]]:
    it = iter(rows)
    first = next(it, None)
    if first is None:
        return
    col = next((i for i, v in enumerate(first) if str(v or "").strip().lower() in HEADERS), None)
    header = _cells(first) if col is not None else None
    n = 1
    if col is None:                                     # no header row: the first row is data
        yield n, None, _cells(first), None
    for row in it:
        n += 1
        if col is not None:
            yield n, (row[col] if col < len(row) else None), _cells(row), header
        else:
            yield n, None, _cells(row), None


def feed_sheet_chunk(intake: Intake, rows: Iterator, source: str, file: str, n: int = CHUNK) -> int:
    """Take up to n rows from the sheet into the intake. Returns how many rows were read (0 = finished)."""
    read = 0
    for row_no, cell, cells, header in rows:
        read += 1
        ref = {"row": row_no, "file": file, "cells": cells}
        if header:
            ref["header"] = header
        if cell is not None or header is not None:          # the URL column
            intake.add(cell, source, ref)
        else:                                               # no URL column: every http(s) cell of the row
            found = [c for c in (_clean(v) for v in cells) if c]
            if not found:
                if any(str(v).strip() for v in cells):
                    intake.add(cells[0] if cells else "", source, ref)      # counted and kept as an unusable row
                continue
            for u in found:
                intake.add(u, source, ref, already_clean=True)
        if read >= n:
            break
    return read


async def read_sheet(intake: Intake, path: str, filename: str, source: str,
                     progress: Optional[Callable[[Dict[str, Any]], Any]] = None) -> None:
    """Read a sheet in chunks off the event loop, reporting progress after each chunk."""
    rows = sheet_rows(path, filename)
    try:
        while True:
            got = await asyncio.to_thread(feed_sheet_chunk, intake, rows, source, filename)
            if progress is not None:
                await progress(intake.summary())
            if got == 0:
                break
    finally:
        rows.close()                     # the file is closed now (Windows can't delete an open file)


def feed_text(intake: Intake, text: str, source: str = "Manual entry") -> None:
    """Pasted / typed addresses: one per line (or separated by spaces); each line keeps its number."""
    n = 0
    for line in (text or "").splitlines():
        for tok in line.split():
            tok = tok.rstrip(",;")
            if not tok:
                continue
            n += 1
            if "://" not in tok and "." in tok and "/" not in tok.split(".")[0]:
                tok = "https://" + tok                      # example.com/page typed without https://
            intake.add(tok, source, {"line": n})


def feed_sitemap_entries(intake: Intake, urls: List[str], sitemaps: List[str], idx: List[int], source: str) -> None:
    for i, u in enumerate(urls):
        sm = sitemaps[idx[i]] if i < len(idx) and idx[i] < len(sitemaps) else ""
        intake.add(u, source, {"sitemap": sm} if sm else {}, already_clean=True)


# ------------------------------------------------------------------ storing the list
async def save(pool, lid: int, intake: Intake, *, replace: bool = True) -> None:
    """Write the queue (url_lists.urls, in order) with every URL's sources and first place, and the rejected rows."""
    import json
    names, idx = intake.sitemap_view()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if replace:
                await conn.execute("DELETE FROM list_urls WHERE list_id = $1", lid)
            recs = [(lid, i + 1, u, s, json.dumps(r)) for i, (u, s, r) in enumerate(zip(intake.urls, intake.sources, intake.refs))]
            for k in range(0, len(recs), 20_000):
                await conn.copy_records_to_table("list_urls", records=[(a, b, c, d, e) for a, b, c, d, e in recs[k:k + 20_000]],
                                                 columns=["list_id", "seq", "url", "sources", "ref"])
            if intake.rejects:
                await conn.copy_records_to_table("list_rejects", records=[(lid, s, json.dumps(r), v, why) for s, r, v, why in intake.rejects],
                                                 columns=["list_id", "source", "ref", "value", "reason"])
                intake.rejects = []
            summ = intake.summary()
            await conn.execute("""UPDATE url_lists SET urls = $2, url_count = $3, sitemaps = $4, sitemap_idx = $5, intake = $6,
                                  updated_at = now() WHERE id = $1""", lid, intake.urls, len(intake.urls), names, idx, summ)


async def load(pool, lid: int, primary: str, scope: Optional[Dict[str, Any]]) -> Intake:
    """The intake of an existing list, to add another source to it (global dedup across everything already there)."""
    import json
    it = Intake(primary, scope)
    rows = await pool.fetch("SELECT url, sources, ref FROM list_urls WHERE list_id = $1 ORDER BY seq", lid)
    if rows:
        it.preload((r["url"], list(r["sources"]), json.loads(r["ref"]) if isinstance(r["ref"], str) else (r["ref"] or {}))
                   for r in rows)
    else:                                                    # a list made before the intake existed
        row = await pool.fetchrow("SELECT urls, source, label, sitemaps, sitemap_idx FROM url_lists WHERE id = $1", lid)
        sms, idx = list(row["sitemaps"] or []), list(row["sitemap_idx"] or [])
        label = source_label(row["source"], row["label"])
        it.preload((u, [label], {"sitemap": sms[idx[i]]} if i < len(idx) and sms else {}) for i, u in enumerate(row["urls"]))
    prev = await pool.fetchval("SELECT intake FROM url_lists WHERE id = $1", lid)
    if isinstance(prev, str):
        prev = json.loads(prev or "{}")
    for src, c in ((prev or {}).get("by_source") or {}).items():
        it.by_source[src] = dict(c)
    it.rejected_total = int((prev or {}).get("rejected") or 0)
    return it


def source_label(kind: str, label: str = "") -> str:
    return {"sitemap": f"Sitemap: {label}", "sheet": f"Sheet: {label}", "single": "Manual entry", "list": "Manual entry",
            "sitemap_file": f"Sitemap file: {label}"}.get(kind, label or kind)
