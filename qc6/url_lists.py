"""URLs to check from a pasted list, a single address, or an uploaded sheet (CSV, TSV, TXT or Excel .xlsx).

The URL column is the one headed url / link / address / page / loc, otherwise every cell that starts with http(s)://.
Only the site's own hosts are kept; everything else is reported back, never fetched.
"""
from __future__ import annotations

import csv
import html
import io
import re
import urllib.parse
from typing import Any, Dict, Iterable, List, Tuple

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


_LOC = re.compile(rb"<(?:[A-Za-z0-9_]+:)?loc>\s*(?:<!\[CDATA\[)?\s*(.*?)\s*(?:\]\]>)?\s*</(?:[A-Za-z0-9_]+:)?loc>", re.S | re.I)
MAX_XML = 200 * 1024 * 1024              # a gzipped sitemap is unpacked up to this size, never more


def _unpack(data: bytes) -> bytes:
    if data[:2] != b"\x1f\x8b":
        return data
    import zlib
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = d.decompress(data, MAX_XML + 1)
    if len(out) > MAX_XML:
        raise ListError("That sitemap unpacks to more than 200 MB. Upload its parts one by one.")
    return out


def is_sitemap(data: bytes, filename: str) -> bool:
    head = _unpack(data[:400_000] if data[:2] != b"\x1f\x8b" else data)[:2000].lstrip().lower()
    return (filename or "").lower().endswith((".xml", ".xml.gz", ".gz")) or head.startswith(b"<?xml") or \
        b"<urlset" in head or b"<sitemapindex" in head


def from_sitemap_file(data: bytes, filename: str) -> Dict[str, Any]:
    """<loc> addresses from a sitemap file saved from a browser. Read as text with a pattern, never as an XML tree
    (no entities, no external references). A sitemap index lists other sitemaps: those come back separately."""
    xml = _unpack(data)
    locs = [html.unescape(m.group(1).decode("utf-8", "replace")) for m in _LOC.finditer(xml)]
    index = b"<sitemapindex" in xml[:5000].lower()
    urls = [c for c in (_clean(u) for u in locs) if c]
    return {"urls": [] if index else urls, "sitemaps": urls if index else []}


def from_upload(data: bytes, filename: str) -> List[str]:
    if len(data) > MAX_BYTES:
        raise ListError("The file is bigger than 20 MB. Split it, or send only the URL column.")
    if is_sitemap(data, filename):
        got = from_sitemap_file(data, filename)
        if got["sitemaps"]:
            more = "\n".join(got["sitemaps"][:10]) + (f"\n… and {len(got['sitemaps']) - 10} more" if len(got["sitemaps"]) > 10 else "")
            raise ListError(f"{filename} is a sitemap index: it lists {len(got['sitemaps'])} other sitemap"
                            f"{'s' if len(got['sitemaps']) != 1 else ''}, not pages. Open each in your browser, save it, and upload "
                            f"them together (you can pick several files at once):\n{more}")
        return got["urls"]
    return parse(_rows_from(data, filename))


def from_text(text: str) -> List[str]:
    return parse([[t.rstrip(",;")] for t in (text or "").split()])      # URLs have no spaces; commas may be in queries


TRACKING = re.compile(r"^(utm_[a-z0-9_]+|fbclid|gclid|gbraid|wbraid|dclid|msclkid|yclid|_ga|_gl|mc_cid|mc_eid|igshid|ttclid|li_fat_id)$", re.I)


def dedup_key(url: str) -> str:
    """Two addresses with the same key are the same page (CrawlPulse crawling engine §4.2): scheme and host lowercased,
    default port and #fragment dropped, query parameters sorted, tracking parameters (utm_*, gclid, fbclid...) removed.
    Used only to spot repeats: the address that is fetched is always the first one exactly as listed."""
    try:
        u = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return url
    scheme, host = u.scheme.lower(), (u.hostname or "").lower()
    port = u.port if u.port and not ((scheme == "http" and u.port == 80) or (scheme == "https" and u.port == 443)) else None
    try:
        q = urllib.parse.parse_qsl(u.query, keep_blank_values=True)
    except ValueError:
        q = []
    q = sorted((k, v) for k, v in q if not TRACKING.match(k))
    return urllib.parse.urlunsplit((scheme, host + (f":{port}" if port else ""), u.path or "/", urllib.parse.urlencode(q), ""))


def dedupe(urls: Iterable[str]) -> Tuple[List[str], int]:
    """First address of each page, in order, and how many repeats were dropped."""
    seen, keep, dropped = set(), [], 0
    for u in urls:
        k = dedup_key(u)
        if k in seen:
            dropped += 1
            continue
        seen.add(k)
        keep.append(u)
    return keep, dropped


def for_site(urls: List[str], site: Dict[str, Any]) -> Dict[str, Any]:
    """Keep the site's own addresses (deduplicated by dedup_key, in order); report the rest."""
    unique, dropped = dedupe(urls)
    keep, other = [], []
    for u in unique:
        (keep if is_site_url(u, site["primary_host"]) else other).append(u)
    if not keep:
        raise ListError(f"None of the addresses are on {site['primary_host']}." + (f" For example: {other[0]}" if other else
                        " No address starting with http:// or https:// was found."))
    if len(keep) > MAX_URLS:
        raise ListError(f"{len(keep):,} addresses; the most in one check is {MAX_URLS:,}.")
    return {"urls": keep, "other_hosts": len(other), "other_examples": other[:5], "duplicates": dropped}
