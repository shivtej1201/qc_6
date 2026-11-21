"""Link intelligence after each page check (CrawlPulse FEAT-09-02, FEAT-11-01, FEAT-10-02):

* click depth: clicks from the home page along internal links (breadth-first walk);
* link strength: internal PageRank (power iteration, damping 0.85), shown as a 0-10 percentile;
* inlinks: how many different pages of the site link to a page;
* canonical targets: a canonical that points at a broken page, a redirect, a noindex page, or a page whose own
  canonical points somewhere else (a chain);
* near-duplicates: pages whose main text is almost the same (64-bit SimHash of word shingles, banded so 300,000 pages
  never need pairwise comparison).

Inputs are what the check already stored (pages in Postgres, links in Parquet); nothing is fetched again.
"""
from __future__ import annotations

import html as htmllib
import os
import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np

from qc6.domain.templates import _h64

DAMPING = 0.85
ITERATIONS = 30
DEEP = 3                    # more than 3 clicks from home is deep (CrawlPulse internal-link spec)
NEAR_DUP_BITS = 8           # SimHash distance <= 8 of 64 bits: about 88 % the same text (CrawlPulse: > 85 %)
NEAR_DUP_MIN_WORDS = 40     # shorter texts are "thin content" (reported already), not duplicates
BUCKET_CAP = 5000           # a band bucket bigger than this is boilerplate, not a duplicate cluster

_DROP = re.compile(r"<(script|style|noscript|svg|template|nav|header|footer|aside|form)\b[^>]*>.*?</\1\s*>", re.S | re.I)
_MAIN = re.compile(r"<(main|article)\b[^>]*>(.*?)</\1\s*>", re.S | re.I)
_BODY = re.compile(r"<body\b[^>]*>(.*)</body\s*>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_WORD = re.compile(r"[^\W_]{2,}", re.U)


# ------------------------------------------------------------------ near-duplicate text
def main_text_words(html: str, limit: int = 400_000) -> List[str]:
    h = _DROP.sub(" ", (html or "")[:limit])
    m = _MAIN.findall(h)
    region = " ".join(x[1] for x in m) if m else (_BODY.search(h).group(1) if _BODY.search(h) else h)
    return _WORD.findall(htmllib.unescape(_TAG.sub(" ", region)).lower())


def text_simhash(html: str, shingle: int = 1) -> Optional[int]:
    """SimHash of the distinct words of the main text. Single words separate best on page-length texts: two small
    edits in 80 words move it ~2 bits, a different text ~28 bits (4-word shingles: 11 vs 31)."""
    words = main_text_words(html)
    if len(words) < NEAR_DUP_MIN_WORDS:
        return None
    w = [0] * 64
    for sh in {" ".join(words[i:i + shingle]) for i in range(len(words) - shingle + 1)}:
        h = _h64(sh)
        for b in range(64):
            w[b] += 1 if (h >> b) & 1 else -1
    return sum(1 << b for b in range(64) if w[b] > 0)


def _popcount(x: np.ndarray) -> np.ndarray:
    if hasattr(np, "bitwise_count"):
        return np.bitwise_count(x)
    return np.unpackbits(x.view(np.uint8).reshape(-1, 8), axis=1).sum(axis=1).reshape(x.shape)


def near_duplicate_groups(fps: Dict[str, int], bits: int = NEAR_DUP_BITS) -> List[List[str]]:
    """Groups of pages whose fingerprints differ by 1..bits bits (identical text is V5's exact-duplicate rule).
    Pigeonhole banding: split 64 bits into bits+1 bands; two fingerprints within `bits` agree exactly on at least one
    band, so only pages sharing a band value are compared, with NumPy, a block at a time."""
    urls = list(fps)
    n = len(urls)
    if n < 2:
        return []
    vals = np.array([fps[u] for u in urls], dtype=np.uint64)
    nb = bits + 1
    width = 64 // nb
    parent = np.arange(n)

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = int(parent[i])
        return i
    for band in range(nb):
        lo = band * width
        w = 64 - lo if band == nb - 1 else width
        key = (vals >> np.uint64(lo)) & np.uint64((1 << w) - 1)
        order = np.argsort(key, kind="stable")
        ks = key[order]
        cuts = np.flatnonzero(np.diff(ks)) + 1
        for members in np.split(order, cuts):
            m = len(members)
            if m < 2 or m > BUCKET_CAP:
                continue
            mv = vals[members]
            for start in range(0, m, 512):                       # bounded memory: 512 x m distances at a time
                block = mv[start:start + 512]
                d = _popcount(block[:, None] ^ mv[None, :])
                ii, jj = np.nonzero((d > 0) & (d <= bits))
                for x, y in zip(ii.tolist(), jj.tolist()):
                    i, j = int(members[start + x]), int(members[y])
                    if i < j:
                        ri, rj = find(i), find(j)
                        if ri != rj:
                            parent[ri] = rj
    groups: Dict[int, List[str]] = defaultdict(list)
    for i, u in enumerate(urls):
        groups[find(i)].append(u)
    return sorted((sorted(g) for g in groups.values() if len(g) > 1), key=lambda g: (-len(g), g[0]))


# ------------------------------------------------------------------ link graph
def load_edges(links_parquet: str, index: Dict[str, int], final_of: Dict[str, str]) -> Tuple[np.ndarray, np.ndarray]:
    """Internal anchor links between pages of the check, as (source index, target index) arrays, de-duplicated.
    A link to an address that redirects counts for the page it lands on."""
    if not os.path.exists(links_parquet):
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64)
    import duckdb
    con = duckdb.connect()
    try:
        rows = con.execute(f"""SELECT DISTINCT source_page_url, url, final_url FROM read_parquet('{links_parquet.replace("'", "''")}')
                               WHERE upper(kind) = 'ANCHOR' AND upper(scope) = 'INTERNAL'""").fetchall()
    finally:
        con.close()
    src, dst = [], []
    for s, u, f in rows:
        i = index.get(s)
        j = index.get(u)
        if j is None and f:
            j = index.get(f)
        if j is None and u in final_of:
            j = index.get(final_of[u])
        if i is not None and j is not None and i != j:
            src.append(i)
            dst.append(j)
    if not src:
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64)
    pairs = np.unique(np.stack([np.array(src, dtype=np.int64), np.array(dst, dtype=np.int64)], axis=1), axis=0)
    return pairs[:, 0], pairs[:, 1]


def pagerank(n: int, src: np.ndarray, dst: np.ndarray, d: float = DAMPING, it: int = ITERATIONS) -> np.ndarray:
    if n == 0:
        return np.zeros(0)
    out = np.bincount(src, minlength=n).astype(float)
    pr = np.full(n, 1.0 / n)
    for _ in range(it):
        share = np.where(out[src] > 0, pr[src] / out[src], 0.0)
        nxt = np.bincount(dst, weights=share, minlength=n)
        dangling = pr[out == 0].sum()
        pr = (1 - d) / n + d * (nxt + dangling / n)
    return pr


def click_depth(n: int, src: np.ndarray, dst: np.ndarray, home: Optional[int]) -> np.ndarray:
    depth = np.full(n, -1, dtype=np.int64)
    if home is None or n == 0:
        return depth
    order = np.argsort(src, kind="stable")
    s_sorted, d_sorted = src[order], dst[order]
    starts = np.searchsorted(s_sorted, np.arange(n + 1))
    depth[home] = 0
    frontier = [home]
    level = 0
    while frontier:
        level += 1
        nxt = []
        for u in frontier:
            for v in d_sorted[starts[u]:starts[u + 1]]:
                if depth[v] < 0:
                    depth[v] = level
                    nxt.append(int(v))
        frontier = nxt
    return depth


def find_home(urls: List[str], base_url: str) -> Optional[int]:
    import urllib.parse
    b = urllib.parse.urlsplit(base_url)
    want = {f"{b.scheme}://{b.netloc}/", f"{b.scheme}://{b.netloc}", base_url.rstrip("/") + "/", base_url}
    for i, u in enumerate(urls):
        if u in want:
            return i
    return None


def percentile_0_10(x: np.ndarray) -> np.ndarray:
    if len(x) == 0:
        return x
    ranks = x.argsort().argsort().astype(float)
    return np.round(10.0 * ranks / max(1, len(x) - 1), 1)


# ------------------------------------------------------------------ canonical targets
def canonical_problems(pages: List[Dict[str, Any]], link_status: Dict[str, Tuple[Optional[int], str]]) -> List[Tuple[str, str, str]]:
    """(page_url, rule, target) for canonicals pointing somewhere that isn't a working, indexable final page."""
    by_url = {p["url"]: p for p in pages}
    by_final = {p.get("final_url") or p["url"]: p for p in pages}
    out = []
    for p in pages:
        c = (p.get("canonical_url") or "").strip()
        if not c or c in (p["url"], p.get("final_url")):
            continue
        t = by_url.get(c) or by_final.get(c)
        if t is not None:
            st = t.get("http_status")
            tc = (t.get("canonical_url") or "").strip()
            if st is not None and st >= 400:
                out.append((p["url"], "CANONICAL_TO_BROKEN", c))
            elif (st is not None and 300 <= st < 400) or (t.get("final_url") and t["final_url"] != t["url"] and t["url"] == c):
                out.append((p["url"], "CANONICAL_TO_REDIRECT", c))
            elif t.get("noindex"):
                out.append((p["url"], "CANONICAL_TO_NOINDEX", c))
            elif tc and tc not in (t["url"], t.get("final_url")):
                out.append((p["url"], "CANONICAL_CHAIN", c))
        elif c in link_status:                    # not in the sitemap, but QC checked it as a link
            st, final = link_status[c]
            if st is not None and st >= 400:
                out.append((p["url"], "CANONICAL_TO_BROKEN", c))
            elif final and final != c:
                out.append((p["url"], "CANONICAL_TO_REDIRECT", c))
    return out


def canonical_map(links_parquet: str) -> Dict[str, str]:
    """page -> its canonical address, from the canonical links the crawl recorded (one per page; the first wins)."""
    if not os.path.exists(links_parquet):
        return {}
    import duckdb
    con = duckdb.connect()
    try:
        rows = con.execute(f"""SELECT source_page_url, min(url) FROM read_parquet('{links_parquet.replace("'", "''")}')
                               WHERE upper(kind) = 'CANONICAL' GROUP BY source_page_url""").fetchall()
    finally:
        con.close()
    return {s: u for s, u in rows if u}


def link_status_map(links_parquet: str, targets: Iterable[str]) -> Dict[str, Tuple[Optional[int], str]]:
    targets = list({t for t in targets if t})
    if not targets or not os.path.exists(links_parquet):
        return {}
    import duckdb
    con = duckdb.connect()
    try:
        con.execute("CREATE TEMP TABLE t(url VARCHAR)")
        con.executemany("INSERT INTO t VALUES (?)", [(t,) for t in targets])
        rows = con.execute(f"""SELECT l.url, max(l.http_status), max(l.final_url) FROM read_parquet('{links_parquet.replace("'", "''")}') l
                               JOIN t ON t.url = l.url GROUP BY l.url""").fetchall()
    finally:
        con.close()
    return {u: (s, f or "") for u, s, f in rows}


# ------------------------------------------------------------------ all together
def analyse(pages: List[Dict[str, Any]], base_url: str, links_parquet: str,
            text_fps: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """pages: url, final_url, http_status, canonical_url, noindex, template_id.
    Returns per-page metrics and problems [(page_url, rule, subject, fields)]."""
    canon = canonical_map(links_parquet)
    for p in pages:                                # the page table may not carry canonicals; the link data does
        if not p.get("canonical_url") and p["url"] in canon:
            p["canonical_url"] = canon[p["url"]]
    urls = [p["url"] for p in pages]
    index = {u: i for i, u in enumerate(urls)}
    for i, p in enumerate(pages):                  # a link may point at the final address of a redirected page
        f = p.get("final_url")
        if f and f not in index:
            index[f] = i
    final_of = {p["url"]: p.get("final_url") or p["url"] for p in pages}
    src, dst = load_edges(links_parquet, index, final_of)
    n = len(urls)
    pr = pagerank(n, src, dst)
    home = find_home(urls, base_url)
    depth = click_depth(n, src, dst, home)
    inlinks = np.bincount(dst, minlength=n) if n else np.zeros(0, dtype=np.int64)
    strength = percentile_0_10(pr)
    metrics = {u: {"depth": int(depth[i]) if depth[i] >= 0 else None, "inlinks": int(inlinks[i]),
                   "authority": float(strength[i]), "canonical_url": pages[i].get("canonical_url") or None}
               for i, u in enumerate(urls)}
    problems: List[Tuple[str, str, str, Dict[str, Any]]] = []
    ok = lambda p: p.get("http_status") is not None and 200 <= p["http_status"] < 300 and not p.get("noindex")
    if home is not None:
        for i, p in enumerate(pages):
            if not ok(p) or i == home:
                continue
            if depth[i] > DEEP:
                problems.append((p["url"], "DEEP_PAGE", "", {"value": int(depth[i])}))
            elif depth[i] < 0 and inlinks[i] > 0:  # linked from somewhere, but never from a path that starts at home
                problems.append((p["url"], "NOT_REACHABLE_FROM_HOME", "", {}))
    for url, rule, target in canonical_problems(pages, link_status_map(links_parquet, [p.get("canonical_url") for p in pages])):
        problems.append((url, rule, target, {}))
    groups: List[List[str]] = []
    if text_fps:
        groups = near_duplicate_groups({u: fp for u, fp in text_fps.items() if u in index and ok(pages[index[u]])})
        for g in groups:
            lead = g[0]
            for u in g:
                problems.append((u, "NEAR_DUPLICATE", lead, {"value": len(g)}))
    return {"metrics": metrics, "problems": problems, "home_found": home is not None, "edges": int(len(src)),
            "near_duplicate_groups": len(groups)}
