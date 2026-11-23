"""The simple QC flow: give a sitemap address, one page address or a sheet of addresses; QC lists the URLs (download
them as a sheet), then "Start crawling" checks every one of them and the report is ready when it finishes.

No setup: the site is found, or created, from the address's host with the standard settings. The sitemap is read in
the API process (it is network-bound and quick next to the crawl), with live progress, and can be cancelled; the crawl
itself runs in the worker like every check, with Pause / Resume / Cancel.
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import os
import tempfile
import urllib.parse
from collections import Counter
from typing import Any, Dict, List, Optional

from qc6 import services
from qc6.site_access import is_site_host
from qc6.url_lists import MAX_URLS, ListError, _clean, for_site

log = logging.getLogger("qc6.jobs")
_tasks: Dict[int, asyncio.Task] = {}
LIST_COLS = "id, site_id, source, label, status, url_count, progress, note, error, run_id, created_at, updated_at"


def _host(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


async def site_for_host(pool, workspace_id: int, url: str, actor_id: Optional[int]) -> Dict[str, Any]:
    """The workspace's site for this address (www. or not), or a new one with the standard settings."""
    u = urllib.parse.urlsplit(url)
    host = (u.hostname or "").lower()
    if u.scheme not in ("http", "https") or not host or ("." not in host and host != "localhost"):
        raise ListError("Enter a full address starting with https://, for example https://www.example.com/sitemap.xml")
    bare = host[4:] if host.startswith("www.") else host
    for s in await pool.fetch("SELECT * FROM sites WHERE workspace_id = $1 ORDER BY id", workspace_id):
        ph = s["primary_host"].lower()
        if ph == host or (ph[4:] if ph.startswith("www.") else ph) == bare:
            return dict(s)
    sid = await services.create_site(pool, workspace_id, host, f"{u.scheme}://{u.netloc}", {}, actor_id)
    return dict(await pool.fetchrow("SELECT * FROM sites WHERE id = $1", sid))


async def _new_list(pool, ws: int, site_id: int, source: str, label: str, actor: Optional[int], status: str = "ready",
                    urls: Optional[List[str]] = None, note: Optional[Dict[str, Any]] = None) -> int:
    urls = urls or []
    return await pool.fetchval("INSERT INTO url_lists (workspace_id, site_id, source, label, status, urls, url_count, note, created_by) "
                               "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id", ws, site_id, source, label[:500], status, urls,
                               len(urls), note or {}, actor)


async def from_sitemap(pool, me: Dict[str, Any], sitemap_url: str) -> int:
    sitemap_url = sitemap_url.strip()
    site = await site_for_host(pool, me["workspace_id"], sitemap_url, me["id"])
    lid = await _new_list(pool, me["workspace_id"], site["id"], "sitemap", sitemap_url, me["id"], status="extracting")
    _tasks[lid] = asyncio.create_task(_extract(pool, lid, site, sitemap_url))
    return lid


async def _extract(pool, lid: int, site: Dict[str, Any], sitemap_url: str) -> None:
    from qc6.workers.runner import discover_urls
    last = {"t": 0.0}

    async def progress(sitemaps: int, urls: int, duplicates: int, rejected: int, errors: int):
        now = asyncio.get_running_loop().time()
        if now - last["t"] < 0.7:             # at most ~1 write a second, plenty for a live counter
            return
        last["t"] = now
        await pool.execute("UPDATE url_lists SET progress = $2, updated_at = now() WHERE id = $1 AND status = 'extracting'", lid,
                           {"sitemaps": sitemaps, "urls": urls, "duplicates": duplicates, "rejected": rejected, "errors": errors})
    try:
        opts: Dict[str, Any] = {"sitemap": sitemap_url, "max_pages": MAX_URLS}
        urls = await discover_urls(site, opts, progress_callback=progress)
        if not urls:
            raise ListError("No page addresses came back from that sitemap. Open it in your browser: if it shows the addresses, "
                            "the site is probably showing QC a bot check instead. Save the sitemap from your browser (Ctrl+S) "
                            "and use Upload a file; for the crawl, give the site's CDN team QC's allow rule (Show the allow rule, below).")
        from qc6.settings import settings
        from qc6.workers.runner import save_alternates
        save_alternates(opts, os.path.join(settings.data_dir, "lists", f"{lid}.alternates.json.gz"))
        await pool.execute("UPDATE url_lists SET status = 'ready', urls = $2, url_count = $3, updated_at = now(), "
                           "note = note || $4::jsonb, sitemaps = $5, sitemap_idx = $6, alternates_file = $7 "
                           "WHERE id = $1 AND status = 'extracting'", lid, urls, len(urls),
                           {"outside_scope": int(opts.get("_scope_excluded") or 0), "capped": len(urls) >= MAX_URLS,
                            "duplicates": int(opts.get("_repeats") or 0),
                            "sitemaps": len(opts.get("_sitemaps") or []), "hreflang_pairs": int(opts.get("_alternates_count") or 0)},
                           opts.get("_sitemaps") or [], opts.get("_sm_idx") or [], opts.get("_alternates_file"))
    except asyncio.CancelledError:
        await pool.execute("UPDATE url_lists SET status = 'cancelled', updated_at = now() WHERE id = $1 AND status = 'extracting'", lid)
        raise
    except Exception as e:                      # shown to the person as is: keep it plain
        log.warning("sitemap list %s failed: %s", lid, e)
        if isinstance(e, ModuleNotFoundError):
            msg = (f"A Python package QC needs isn't installed ({e.name}). Close QC and start it again with start.bat "
                   "(or ./start.sh): it installs what's missing.")
        elif isinstance(e, (ListError, RuntimeError)):
            msg = str(e)
        else:
            msg = f"The sitemap couldn't be read ({type(e).__name__}: {e})"
        await pool.execute("UPDATE url_lists SET status = 'failed', error = $2, updated_at = now() WHERE id = $1 AND status = 'extracting'",
                           lid, msg[:600])
    finally:
        _tasks.pop(lid, None)


async def from_urls(pool, me: Dict[str, Any], urls: List[str], source: str, label: str) -> int:
    """One address, a pasted list or a sheet. The site is the host most of the addresses are on."""
    if not urls:
        raise ListError("No address starting with http:// or https:// was found." if source != "single"
                        else "Enter a full address starting with https://")
    host = Counter(_host(u) for u in urls).most_common(1)[0][0]
    bare = host[4:] if host.startswith("www.") else host
    first = next(u for u in urls if _host(u) == host)
    site = await site_for_host(pool, me["workspace_id"], first, me["id"])
    got = for_site([u for u in urls if is_site_host(_host(u), site["primary_host"]) or _host(u).endswith(bare)], site)
    other = len(urls) - len(got["urls"]) - got["duplicates"]
    return await _new_list(pool, me["workspace_id"], site["id"], source, label, me["id"], urls=got["urls"],
                           note={"duplicates": got["duplicates"], "other_hosts": max(0, other)})


def single_url(text: str) -> List[str]:
    u = _clean(text.strip())
    if not u and text.strip() and "://" not in text:
        u = _clean("https://" + text.strip())
    return [u] if u else []


async def cancel(pool, lid: int) -> None:
    t = _tasks.get(lid)
    if t is not None:
        t.cancel()
    await pool.execute("UPDATE url_lists SET status = 'cancelled', updated_at = now() WHERE id = $1 AND status = 'extracting'", lid)


async def recover(pool) -> None:
    """On start: a sitemap being read when the API stopped can't carry on."""
    await pool.execute("UPDATE url_lists SET status = 'failed', error = 'QC was restarted while reading this sitemap. Read it again.', "
                       "updated_at = now() WHERE status = 'extracting'")


async def start_crawl(pool, me: Dict[str, Any], lid: int, browser: bool = False, preset: Optional[str] = None) -> int:
    row = await pool.fetchrow("SELECT * FROM url_lists WHERE id = $1 AND workspace_id = $2", lid, me["workspace_id"])
    if row is None:
        raise LookupError("list")
    if row["status"] != "ready":
        raise ListError("The URL list isn't ready yet." if row["status"] == "extracting" else "This URL list can't be crawled. Make a new one.")
    opts: Dict[str, Any] = {"urls": list(row["urls"]), "max_pages": MAX_URLS, "from_list": lid}
    from qc6.presets import PRESETS, speeds
    site_settings = await pool.fetchval("SELECT settings FROM sites WHERE id = $1", row["site_id"]) or {}
    p = preset or speeds(row["url_count"], site_settings)["speed_default"]
    if p != "site" and p not in PRESETS:
        raise ListError("Pick one of the speeds offered.")
    if p != "site":     # the chosen speed wins over the site's stored rate, and an untouched preset may rise to its ceiling
        opts["preset"], opts["rate_rps"] = p, PRESETS[p]["target_rps"]
    if row["sitemaps"]:                         # keep where each page came from, for the report's Sitemap Progress
        opts["_sitemaps"], opts["_sm_idx"] = list(row["sitemaps"]), list(row["sitemap_idx"])
    if row["alternates_file"]:
        opts["_alternates_file"] = row["alternates_file"]
    if row["source"] != "sitemap":              # a few pages: its own results; the site's health score isn't changed
        n = row["url_count"]
        opts["spot"] = {"label": row["label"] if n == 1 else f"{n:,} pages from {row['label']}", "source": row["source"],
                        "file": row["label"] if row["source"] == "sheet" else None, "count": n}
    rid = await services.start_run(pool, row["site_id"], "full" if browser else "coverage", opts, me["id"])
    await pool.execute("UPDATE url_lists SET run_id = $2, updated_at = now() WHERE id = $1", lid, rid)
    return rid


async def jobs(pool, workspace_id: int, limit: int = 30) -> List[Dict[str, Any]]:
    """Recent URL lists and their checks, newest first, for the Start screen."""
    rows = await pool.fetch(f"""
        SELECT l.id, l.site_id, l.source, l.label, l.status, l.url_count, l.progress, l.note, l.error, l.created_at, l.updated_at,
               s.primary_host, r.id AS run_id, r.status AS run_status, r.kind AS run_kind, r.progress AS run_progress,
               r.summary AS run_summary, r.error AS run_error, r.started_at AS run_started, r.finished_at AS run_finished
        FROM url_lists l JOIN sites s ON s.id = l.site_id LEFT JOIN runs r ON r.id = l.run_id
        WHERE l.workspace_id = $1 ORDER BY l.id DESC LIMIT {int(limit)}""", workspace_id)
    return [dict(r) for r in rows]


async def get(pool, workspace_id: int, lid: int) -> Optional[Dict[str, Any]]:
    for j in await pool.fetch(f"""
        SELECT l.id, l.site_id, l.source, l.label, l.status, l.url_count, l.progress, l.note, l.error, l.created_at, l.updated_at,
               s.primary_host, r.id AS run_id, r.status AS run_status, r.kind AS run_kind, r.progress AS run_progress,
               r.summary AS run_summary, r.error AS run_error, r.started_at AS run_started, r.finished_at AS run_finished,
               l.urls[1:20] AS sample
        FROM url_lists l JOIN sites s ON s.id = l.site_id LEFT JOIN runs r ON r.id = l.run_id
        WHERE l.workspace_id = $1 AND l.id = $2""", workspace_id, lid):
        out = dict(j)
        if out["run_id"] and out["run_status"] in ("completed", "cancelled", "paused", "running", "pausing", "cancelling"):
            sev = await pool.fetch("""SELECT f.severity, COUNT(DISTINCT f.id) AS problems, COUNT(DISTINCT fd.page_url) AS pages
                                      FROM findings fd JOIN fixes f ON f.id = fd.fix_id WHERE fd.run_id = $1 GROUP BY 1""", out["run_id"])
            out["results"] = {r["severity"]: {"problems": r["problems"], "pages": r["pages"]} for r in sev}
        if out["status"] == "ready":
            from qc6.presets import speeds
            out.update(speeds(out["url_count"], await pool.fetchval("SELECT settings FROM sites WHERE id = $1", out["site_id"]) or {}))
        return out
    return None


async def delete(pool, workspace_id: int, lid: int) -> bool:
    await cancel(pool, lid)
    return (await pool.execute("DELETE FROM url_lists WHERE id = $1 AND workspace_id = $2", lid, workspace_id)) != "DELETE 0"


# ------------------------------------------------------------------ the URL sheet
async def urls_of(pool, workspace_id: int, lid: int) -> Optional[Dict[str, Any]]:
    r = await pool.fetchrow("SELECT l.urls, l.label, l.source, s.primary_host FROM url_lists l JOIN sites s ON s.id = l.site_id "
                            "WHERE l.id = $1 AND l.workspace_id = $2 AND l.status = 'ready'", lid, workspace_id)
    return dict(r) if r else None


def urls_csv(urls: List[str]):
    yield "﻿#,URL\r\n".encode()
    buf = io.StringIO()
    w = csv.writer(buf)
    for i, u in enumerate(urls, 1):
        w.writerow([i, u])
        if i % 5000 == 0:
            yield buf.getvalue().encode()
            buf.seek(0)
            buf.truncate()
    yield buf.getvalue().encode()


def urls_xlsx(urls: List[str], label: str) -> str:
    import openpyxl
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font
    wb = openpyxl.Workbook(write_only=True)
    ws = wb.create_sheet("URLs")
    ws.column_dimensions["A"].width = 8
    ws.column_dimensions["B"].width = 100
    head = [WriteOnlyCell(ws, value=v) for v in ("#", "URL")]
    for c in head:
        c.font = Font(bold=True)
    ws.append(head)
    for i, u in enumerate(urls, 1):
        ws.append([i, u])
    fd, path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    wb.save(path)
    return path
