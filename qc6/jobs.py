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
from typing import Any, Dict, List, Optional, Tuple

from qc6 import intake, services
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


async def from_sitemap(pool, me: Dict[str, Any], sitemap_url: str, add_to: Optional[int] = None) -> int:
    """A new list from a sitemap (index -> child -> sub-sitemaps), or the sitemap's URLs added to an existing list."""
    sitemap_url = sitemap_url.strip()
    if add_to:
        row = await _open_for_adding(pool, me, add_to)
        site = dict(await pool.fetchrow("SELECT * FROM sites WHERE id = $1", row["site_id"]))
        lid = add_to
    else:
        site = await site_for_host(pool, me["workspace_id"], sitemap_url, me["id"])
        lid = await _new_list(pool, me["workspace_id"], site["id"], "sitemap", sitemap_url, me["id"], status="extracting")
    _tasks[lid] = asyncio.create_task(_extract(pool, lid, site, sitemap_url, append=bool(add_to)))
    return lid


async def _open_for_adding(pool, me: Dict[str, Any], lid: int):
    """Adding a source to a list: only to a ready list whose crawl hasn't started; it's 'reading' meanwhile."""
    row = await pool.fetchrow("SELECT * FROM url_lists WHERE id = $1 AND workspace_id = $2", lid, me["workspace_id"])
    if row is None:
        raise LookupError("list")
    if row["run_id"] is not None:
        raise ListError("This list's crawl has started: make a new list to check more URLs.")
    done = await pool.execute("UPDATE url_lists SET status = 'extracting', error = NULL, updated_at = now() "
                              "WHERE id = $1 AND status = 'ready' AND run_id IS NULL", lid)
    if done == "UPDATE 0":
        raise ListError("The list is still being read. Wait until it's ready, then add more.")
    return row


async def _progress(pool, lid: int, summary: Dict[str, Any], **extra: Any) -> None:
    await pool.execute("UPDATE url_lists SET progress = $2, updated_at = now() WHERE id = $1 AND status = 'extracting'", lid,
                       {**{k: v for k, v in summary.items() if k != "by_source"}, **extra, "stage": "reading"})


async def _finish(pool, lid: int, it: "intake.Intake", note: Dict[str, Any], append: bool) -> None:
    if not it.urls:
        raise ListError("None of the rows had an address on this site that QC can check.")
    await intake.save(pool, lid, it)
    src_count = len(it.by_source)
    await pool.execute("UPDATE url_lists SET status = 'ready', progress = $2, note = note || $3::jsonb, "
                       "source = CASE WHEN $4 THEN 'mixed' ELSE source END, updated_at = now() WHERE id = $1",
                       lid, {"stage": "ready", **{k: v for k, v in it.summary().items() if k != "by_source"}},
                       {**note, "duplicates": it.summary()["duplicates"], "other_hosts": it.summary()["other_hosts"],
                        "rejected": it.rejected_total, "sources": src_count}, append and src_count > 1)


async def _failed(pool, lid: int, msg: str, append: bool) -> None:
    if append:                                      # the list itself is fine: back to ready, with what went wrong
        await pool.execute("UPDATE url_lists SET status = 'ready', error = $2, progress = progress || '{\"stage\": \"ready\"}', "
                           "updated_at = now() WHERE id = $1", lid, msg[:600])
    else:
        await pool.execute("UPDATE url_lists SET status = 'failed', error = $2, updated_at = now() WHERE id = $1 AND status = 'extracting'",
                           lid, msg[:600])


def _plain_error(e: Exception, what: str) -> str:
    if isinstance(e, ModuleNotFoundError):
        return (f"A Python package QC needs isn't installed ({e.name}). Close QC and start it again with start.bat "
                "(or ./start.sh): it installs what's missing.")
    if isinstance(e, (ListError, RuntimeError)):
        return str(e)
    return f"{what} couldn't be read ({type(e).__name__}: {e})"


async def _extract(pool, lid: int, site: Dict[str, Any], sitemap_url: str, append: bool = False) -> None:
    """Sitemap discovery (the sitemap reader only finds URLs), then the common intake."""
    from qc6.workers.runner import discover_urls
    last = {"t": 0.0}

    async def progress(sitemaps: int, urls: int, duplicates: int, rejected: int, errors: int):
        now = asyncio.get_running_loop().time()
        if now - last["t"] < 0.7:             # at most ~1 write a second, plenty for a live counter
            return
        last["t"] = now
        await pool.execute("UPDATE url_lists SET progress = $2, updated_at = now() WHERE id = $1 AND status = 'extracting'", lid,
                           {"stage": "reading", "sitemaps": sitemaps, "rows": urls + duplicates + rejected, "valid": urls + duplicates,
                            "queued": urls, "duplicates": duplicates, "invalid": rejected, "errors": errors})
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
        it = await intake.load(pool, lid, site["primary_host"], None) if append else intake.Intake(site["primary_host"])
        src = intake.source_label("sitemap", sitemap_url)
        intake.feed_sitemap_entries(it, urls, opts.get("_sitemaps") or [], opts.get("_sm_idx") or [], src)
        c = it.by_source.setdefault(src, {"rows": 0, "valid": 0, "invalid": 0, "duplicates": 0, "other_hosts": 0,
                                          "outside_scope": 0, "added": 0})
        rep, out = int(opts.get("_repeats") or 0), int(opts.get("_scope_excluded") or 0)
        c["rows"] += rep + out
        c["valid"] += rep + out
        c["duplicates"] += rep
        c["outside_scope"] += out
        await _finish(pool, lid, it, {"outside_scope": out, "capped": len(urls) >= MAX_URLS,
                                      "sitemaps": len(opts.get("_sitemaps") or []),
                                      "hreflang_pairs": int(opts.get("_alternates_count") or 0)}, append)
        if opts.get("_alternates_file") and not append:
            await pool.execute("UPDATE url_lists SET alternates_file = $2 WHERE id = $1", lid, opts.get("_alternates_file"))
    except asyncio.CancelledError:
        if append:
            await pool.execute("UPDATE url_lists SET status = 'ready', updated_at = now() WHERE id = $1 AND status = 'extracting'", lid)
        else:
            await pool.execute("UPDATE url_lists SET status = 'cancelled', updated_at = now() WHERE id = $1 AND status = 'extracting'", lid)
        raise
    except Exception as e:                      # shown to the person as is: keep it plain
        log.warning("sitemap list %s failed: %s", lid, e)
        await _failed(pool, lid, _plain_error(e, "The sitemap"), append)
    finally:
        _tasks.pop(lid, None)


async def _site_for_urls(pool, me: Dict[str, Any], sample: List[str]) -> Dict[str, Any]:
    if not sample:
        raise ListError("No address starting with http:// or https:// was found.")
    host = Counter(_host(u) for u in sample).most_common(1)[0][0]
    first = next(u for u in sample if _host(u) == host)
    return await site_for_host(pool, me["workspace_id"], first, me["id"])


async def from_text(pool, me: Dict[str, Any], text: str, source: str = "list", label: str = "",
                    add_to: Optional[int] = None) -> int:
    """Typed / pasted addresses (one or many): the common intake, straight away (small by nature)."""
    from qc6.url_lists import from_text as urls_in
    if add_to:
        row = await _open_for_adding(pool, me, add_to)
        try:
            site = dict(await pool.fetchrow("SELECT * FROM sites WHERE id = $1", row["site_id"]))
            it = await intake.load(pool, add_to, site["primary_host"], None)
            intake.feed_text(it, text)
            await _finish(pool, add_to, it, {}, True)
        except Exception as e:
            await _failed(pool, add_to, _plain_error(e, "Those addresses"), True)
            raise
        return add_to
    probe = urls_in(text) or single_url(text)
    if not probe:
        raise ListError("Enter a full address starting with https://" if source == "single"
                        else "No address starting with http:// or https:// was found.")
    site = await _site_for_urls(pool, me, probe)
    it = intake.Intake(site["primary_host"])
    intake.feed_text(it, text)
    if not it.urls:
        raise ListError(f"None of the addresses are on {site['primary_host']}.")
    label = label or (it.urls[0] if len(it.urls) == 1 else f"{len(it.urls):,} addresses entered")
    lid = await _new_list(pool, me["workspace_id"], site["id"], "single" if len(it.urls) == 1 and source == "single" else "list",
                          label, me["id"], status="extracting")
    await _finish(pool, lid, it, {}, False)
    return lid


async def from_urls(pool, me: Dict[str, Any], urls: List[str], source: str, label: str) -> int:
    """Kept for callers with a ready list of addresses: the same intake."""
    return await from_text(pool, me, "\n".join(urls), source, label)


async def from_files(pool, me: Dict[str, Any], files: List[Tuple[str, str]], add_to: Optional[int] = None) -> int:
    """Uploaded sheets (CSV / TSV / TXT / XLSX) and sitemap files, saved to disk first: read in chunks in the background
    with live progress, through the common intake. files: (path on disk, original name)."""
    if add_to:
        row = await _open_for_adding(pool, me, add_to)
        site = dict(await pool.fetchrow("SELECT * FROM sites WHERE id = $1", row["site_id"]))
        lid = add_to
    else:
        sample: List[str] = []
        for path, name in files:                       # the first rows tell which site this is
            sample += await asyncio.to_thread(_sample_urls, path, name)
            if len(sample) >= 500:
                break
        site = await _site_for_urls(pool, me, sample)
        label = files[0][1] + (f" and {len(files) - 1} more file{'s' if len(files) > 2 else ''}" if len(files) > 1 else "")
        lid = await _new_list(pool, me["workspace_id"], site["id"], "sheet", label, me["id"], status="extracting")
    _tasks[lid] = asyncio.create_task(_read_files(pool, lid, site, files, append=bool(add_to)))
    return lid


def _sample_urls(path: str, name: str, n: int = 2000) -> List[str]:
    from qc6.url_lists import _clean, from_sitemap_file, is_sitemap
    with open(path, "rb") as f:
        head = f.read(400_000)
    if is_sitemap(head, name):
        with open(path, "rb") as f:
            got = from_sitemap_file(f.read(), name)
        return (got["urls"] or got["sitemaps"])[:n]
    out: List[str] = []
    for i, (_, cell, cells, header) in enumerate(intake.sheet_rows(path, name)):
        out += [c for c in ((_clean(cell),) if header else (_clean(v) for v in cells)) if c]
        if i >= n:
            break
    return out


async def _read_files(pool, lid: int, site: Dict[str, Any], files: List[Tuple[str, str]], append: bool) -> None:
    from qc6.url_lists import from_sitemap_file, is_sitemap
    last = {"t": 0.0}
    skipped: List[str] = []
    try:
        it = await intake.load(pool, lid, site["primary_host"], None) if append else intake.Intake(site["primary_host"])

        async def progress(summary: Dict[str, Any]) -> None:
            now = asyncio.get_running_loop().time()
            if now - last["t"] >= 0.7:
                last["t"] = now
                await _progress(pool, lid, summary, files=len(files))
        for path, name in files:
            try:
                with open(path, "rb") as f:
                    head = f.read(400_000)
                if is_sitemap(head, name):
                    with open(path, "rb") as f:
                        got = await asyncio.to_thread(from_sitemap_file, f.read(), name)
                    if got["sitemaps"]:
                        sm = got["sitemaps"]
                        more = "\n".join(sm[:10]) + (f"\n… and {len(sm) - 10} more" if len(sm) > 10 else "")
                        skipped.append(f"{name} is a sitemap index: it lists {len(sm)} other sitemap{'s' if len(sm) != 1 else ''}, "
                                       f"not pages. Add the sitemap's address (QC reads every child sitemap), or save each "
                                       f"child sitemap from your browser and upload them together:\n{more}")
                        continue
                    src = intake.source_label("sitemap_file", name)
                    for u in got["urls"]:
                        it.add(u, src, {"file": name}, already_clean=True)
                    await progress(it.summary())
                else:
                    await intake.read_sheet(it, path, name, intake.source_label("sheet", name), progress)
            except ListError as e:
                skipped.append(str(e))
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        if not it.urls and skipped:
            raise ListError(skipped[0])
        await _finish(pool, lid, it, {"skipped_files": skipped[:5]}, append)
    except asyncio.CancelledError:
        await pool.execute(f"UPDATE url_lists SET status = '{'ready' if append else 'cancelled'}', updated_at = now() "
                           "WHERE id = $1 AND status = 'extracting'", lid)
        raise
    except Exception as e:
        log.warning("sheet list %s failed: %s", lid, e)
        await _failed(pool, lid, _plain_error(e, "The file"), append)
    finally:
        for path, _ in files:
            if os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
        _tasks.pop(lid, None)


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


async def start_crawl(pool, me: Dict[str, Any], lid: int, browser: bool = False, preset: Optional[str] = None,
                      reuse: bool = True) -> int:
    row = await pool.fetchrow("SELECT * FROM url_lists WHERE id = $1 AND workspace_id = $2", lid, me["workspace_id"])
    if row is None:
        raise LookupError("list")
    if row["status"] != "ready":
        raise ListError("The URL list isn't ready yet." if row["status"] == "extracting" else "This URL list can't be crawled. Make a new one.")
    opts: Dict[str, Any] = {"urls": list(row["urls"]), "max_pages": MAX_URLS, "from_list": lid, "reuse": bool(reuse)}
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
    srcs = (row["intake"] if isinstance(row["intake"], dict) else {}).get("by_source") or {}
    has_sitemap = row["source"] == "sitemap" or any(str(k).startswith("Sitemap: ") for k in srcs)
    if not has_sitemap:                         # a few pages: its own results; the site's health score isn't changed
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
               r.summary AS run_summary, r.error AS run_error, r.started_at AS run_started, r.finished_at AS run_finished, l.intake
        FROM url_lists l JOIN sites s ON s.id = l.site_id LEFT JOIN runs r ON r.id = l.run_id
        WHERE l.workspace_id = $1 ORDER BY l.id DESC LIMIT {int(limit)}""", workspace_id)
    out = [dict(r) for r in rows]
    for j in out:
        j["pipeline"] = pipeline(j)
        j.pop("intake", None)
    return out


async def get(pool, workspace_id: int, lid: int) -> Optional[Dict[str, Any]]:
    for j in await pool.fetch(f"""
        SELECT l.id, l.site_id, l.source, l.label, l.status, l.url_count, l.progress, l.note, l.error, l.created_at, l.updated_at,
               s.primary_host, r.id AS run_id, r.status AS run_status, r.kind AS run_kind, r.progress AS run_progress,
               r.summary AS run_summary, r.error AS run_error, r.started_at AS run_started, r.finished_at AS run_finished,
               l.urls[1:20] AS sample, l.intake
        FROM url_lists l JOIN sites s ON s.id = l.site_id LEFT JOIN runs r ON r.id = l.run_id
        WHERE l.workspace_id = $1 AND l.id = $2""", workspace_id, lid):
        out = dict(j)
        if out["run_id"] and out["run_status"] in ("completed", "cancelled", "paused", "running", "pausing", "cancelling"):
            sev = await pool.fetch("""SELECT f.severity, COUNT(DISTINCT f.id) AS problems, COUNT(DISTINCT fd.page_url) AS pages
                                      FROM findings fd JOIN fixes f ON f.id = fd.fix_id WHERE fd.run_id = $1 GROUP BY 1""", out["run_id"])
            out["results"] = {r["severity"]: {"problems": r["problems"], "pages": r["pages"]} for r in sev}
        out["pipeline"] = pipeline(out)
        out.pop("intake", None)
        if out["status"] == "ready":
            from qc6.presets import speeds
            out.update(speeds(out["url_count"], await pool.fetchval("SELECT settings FROM sites WHERE id = $1", out["site_id"]) or {}))
        return out
    return None


def pipeline(j: Dict[str, Any]) -> Dict[str, Any]:
    """One progress model for every source: read -> valid -> duplicates / unusable -> queued -> crawled / failed -> left."""
    import json
    intake_ = j.get("intake") or {}
    if isinstance(intake_, str):
        intake_ = json.loads(intake_ or "{}")
    prog = j.get("progress") or {}
    src = intake_ if j.get("status") != "extracting" else prog
    rp = j.get("run_progress") or {}
    queued = int(src.get("queued") or j.get("url_count") or 0)
    done = int(rp.get("pages_done") or 0) if j.get("run_id") else 0
    if j.get("run_status") == "completed":
        done = queued
    failed = int(rp.get("pages_failed") or 0)
    stage = ("reading" if j.get("status") == "extracting" else "failed" if j.get("status") == "failed"
             else (j.get("run_status") or "ready") if j.get("run_id") else "ready")
    return {
        "stage": stage, "sources": sorted((intake_.get("by_source") or {}).keys()) or [j.get("label")],
        "by_source": intake_.get("by_source") or {}, "sitemaps": int(prog.get("sitemaps") or 0),
        "read": int(src.get("rows") or 0), "valid": int(src.get("valid") or 0), "invalid": int(src.get("invalid") or 0),
        "duplicates": int(src.get("duplicates") or 0), "other_hosts": int(src.get("other_hosts") or 0),
        "outside_scope": int(src.get("outside_scope") or 0), "rejected": int(src.get("rejected") or 0),
        "queued": queued, "completed": min(done, queued), "failed": failed,
        "remaining": max(0, queued - done) if j.get("run_id") else queued,
        "links_reused": int((j.get("run_summary") or {}).get("links_reused_from_previous") or rp.get("links_reused") or 0),
    }


async def delete(pool, workspace_id: int, lid: int) -> bool:
    await cancel(pool, lid)
    return (await pool.execute("DELETE FROM url_lists WHERE id = $1 AND workspace_id = $2", lid, workspace_id)) != "DELETE 0"


# ------------------------------------------------------------------ the URL sheet
async def urls_of(pool, workspace_id: int, lid: int) -> Optional[Dict[str, Any]]:
    r = await pool.fetchrow("SELECT l.urls, l.label, l.source, s.primary_host FROM url_lists l JOIN sites s ON s.id = l.site_id "
                            "WHERE l.id = $1 AND l.workspace_id = $2 AND l.status = 'ready'", lid, workspace_id)
    return dict(r) if r else None


async def url_rows(pool, lid: int, r: Dict[str, Any]) -> List[List[Any]]:
    """Each queued URL with its sources and where it was first seen (sheet row / sitemap / line)."""
    import json
    rows = await pool.fetch("SELECT seq, url, sources, ref FROM list_urls WHERE list_id = $1 ORDER BY seq", lid)
    if not rows:                                       # a list made before the common intake
        return [[i, u, "", ""] for i, u in enumerate(r["urls"], 1)]
    out = []
    for x in rows:
        ref = x["ref"] if isinstance(x["ref"], dict) else json.loads(x["ref"] or "{}")
        where = (f"row {ref['row']}" if ref.get("row") else f"line {ref['line']}" if ref.get("line") else ref.get("sitemap") or "")
        out.append([x["seq"], x["url"], " | ".join(x["sources"]), where])
    return out


HEAD = ["#", "URL", "Source(s)", "First seen at"]


def urls_csv(rows: List[List[Any]]):
    yield ("\ufeff" + ",".join(HEAD) + "\r\n").encode()
    buf = io.StringIO()
    w = csv.writer(buf)
    for i, row in enumerate(rows, 1):
        w.writerow(row)
        if i % 5000 == 0:
            yield buf.getvalue().encode()
            buf.seek(0)
            buf.truncate()
    yield buf.getvalue().encode()


def urls_xlsx(rows: List[List[Any]], label: str) -> str:
    import xlsxwriter
    fd, path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    wb = xlsxwriter.Workbook(path, {"constant_memory": True, "strings_to_urls": False, "strings_to_formulas": False})
    ws = wb.add_worksheet("URLs")
    for i, w in enumerate((8, 100, 40, 50)):
        ws.set_column(i, i, w)
    ws.write_row(0, 0, HEAD, wb.add_format({"bold": True}))
    for i, row in enumerate(rows, 1):
        ws.write_row(i, 0, row)
    wb.close()
    return path
