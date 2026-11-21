"""URLs to check from a pasted list, a single address, or an uploaded sheet (CSV, TSV, TXT or Excel .xlsx).

The URL column is the one headed url / link / address / page / loc, otherwise every cell that starts with http(s)://.
Only the site's own hosts are kept; everything else is reported back, never fetched.
"""
from __future__ import annotations

import csv
import io
import urllib.parse
from typing import Any, Dict, Iterable, List

from qc6.site_access import is_site_url

MAX_URLS = 300_000
MAX_BYTES = 20 * 1024 * 1024
HEADERS = {"url", "urls", "link", "links", "address", "addresses", "page", "pages", "page url", "page_url", "loc", "source url"}


class ListError(ValueError):
    pass


def _clean(v: Any) -> str:
    s = str(v or "").strip().strip('"').strip("'")
    if not s.lower().startswith(("http://", "https://")):
        return ""
    try:
        u = urllib.parse.urlsplit(s)
    except ValueError:
        return ""
    if not u.netloc:
        return ""
    return urllib.parse.urlunsplit((u.scheme.lower(), u.netloc.lower(), u.path or "/", u.query, ""))   # no #fragment


def _rows_from(data: bytes, filename: str) -> Iterable[List[Any]]:
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        import openpyxl
        try:
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception as e:
            raise ListError(f"That Excel file can't be read ({type(e).__name__}). Save it again as .xlsx or CSV.")
        try:
            for row in wb.worksheets[0].iter_rows(values_only=True):
                yield list(row)
        finally:
            wb.close()
        return
    if name.endswith(".xls"):
        raise ListError("Old .xls files aren't supported. Save the sheet as .xlsx or CSV.")
    text = data.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    yield from csv.reader(io.StringIO(text), dialect)


def parse(rows: Iterable[List[Any]]) -> List[str]:
    rows = iter(rows)
    first = next(rows, None)
    if first is None:
        return []
    col = None
    for i, v in enumerate(first):
        if str(v or "").strip().lower() in HEADERS:
            col = i
            break
    out: List[str] = []

    def take(row):
        cells = [row[col]] if col is not None and col < len(row) else row
        for v in cells:
            c = _clean(v)
            if c:
                out.append(c)
    if col is None:
        take(first)
    for row in rows:
        take(row)
        if len(out) > MAX_URLS * 2:
            break
    return out


def from_upload(data: bytes, filename: str) -> List[str]:
    if len(data) > MAX_BYTES:
        raise ListError("The file is bigger than 20 MB. Split it, or send only the URL column.")
    return parse(_rows_from(data, filename))


def from_text(text: str) -> List[str]:
    return parse([[t.rstrip(",;")] for t in (text or "").split()])      # URLs have no spaces; commas may be in queries


def for_site(urls: List[str], site: Dict[str, Any]) -> Dict[str, Any]:
    """Keep the site's own addresses (deduplicated, in order); report the rest."""
    seen, keep, other = set(), [], []
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        (keep if is_site_url(u, site["primary_host"]) else other).append(u)
    if not keep:
        raise ListError(f"None of the addresses are on {site['primary_host']}." + (f" For example: {other[0]}" if other else
                        " No address starting with http:// or https:// was found."))
    if len(keep) > MAX_URLS:
        raise ListError(f"{len(keep):,} addresses; the most in one check is {MAX_URLS:,}.")
    return {"urls": keep, "other_hosts": len(other), "other_examples": other[:5], "duplicates": len(urls) - len(seen)}
