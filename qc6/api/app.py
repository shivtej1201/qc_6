"""QC V6 REST API (/api/v2) + live events (SSE) + the web app (blueprint §11).

Auth in this release: `QC6_AUTH=dev` (default) acts as the member named in the `X-QC-User` header (an e-mail), or the
workspace owner. OIDC via Keycloak replaces this in phase 6 (blueprint §12); every write already records the actor.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import urllib.parse
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from qc6 import services
from qc6.crawl import challenge
from qc6.db.pool import close_pool, get_pool
from qc6.domain import rules as R
from qc6.settings import settings

WEB_DIST = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "web", "dist")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_pool()
    yield
    await close_pool()


app = FastAPI(title="QC V6", version="6.0.0a1", lifespan=lifespan)


CSP = ("default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
       "base-uri 'none'; object-src 'none'; form-action 'self'")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("Content-Security-Policy", CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if request.url.path.startswith("/api/"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


ETAG_SKIP = ("/api/v2/stream", "/api/v2/objects/", "/metrics")


@app.middleware("http")
async def etag(request: Request, call_next):
    """ETag on JSON reads (blueprint §11): an API client that sends If-None-Match gets 304 when nothing changed.
    Cache-Control stays no-store, so browsers never keep workspace data on disk."""
    resp = await call_next(request)
    path = request.url.path
    if (request.method != "GET" or not path.startswith("/api/") or resp.status_code != 200 or path.startswith(ETAG_SKIP)
            or not resp.headers.get("content-type", "").startswith("application/json")):
        return resp
    body = b"".join([chunk async for chunk in resp.body_iterator])
    import hashlib
    tag = '"' + hashlib.blake2b(body, digest_size=12).hexdigest() + '"'
    headers = {k: v for k, v in resp.headers.items() if k.lower() != "content-length"}
    headers["ETag"] = tag
    from fastapi import Response
    inm = request.headers.get("if-none-match", "")
    if tag in [t.strip() for t in inm.split(",")]:
        return Response(status_code=304, headers={k: v for k, v in headers.items() if k.lower() != "content-type"})
    return Response(body, status_code=200, headers=headers, media_type="application/json")


def problem(status: int, title: str, detail: str = "") -> HTTPException:
    return HTTPException(status, {"title": title, "detail": detail})


@app.exception_handler(HTTPException)
async def _http_exc(request: Request, exc: HTTPException):
    body = exc.detail if isinstance(exc.detail, dict) else {"title": str(exc.detail)}
    return JSONResponse(body, status_code=exc.status_code, media_type="application/problem+json")


# ------------------------------------------------------------------ identity
async def current_member(request: Request) -> Dict[str, Any]:
    from qc6 import auth
    try:
        mode, email = await auth.caller_email(dict(request.headers), request.client.host if request.client else None,
                                              dict(request.query_params))
    except auth.AuthError as e:
        raise problem(e.status, e.title, e.detail)
    pool = await get_pool()
    if email:
        m = await pool.fetchrow("SELECT * FROM members WHERE lower(email) = lower($1) ORDER BY id LIMIT 1", email)
        if m is None:
            raise problem(403, "You're signed in but not a member here", f"Ask a workspace owner to add {email}.")
    elif mode == "dev":
        m = await pool.fetchrow("SELECT * FROM members WHERE role = 'owner' ORDER BY id LIMIT 1")
        if m is None and not await pool.fetchval("SELECT EXISTS (SELECT 1 FROM members)"):
            # first start on this machine: create a workspace and its owner so the app opens straight to "Add a site"
            await services.create_workspace(pool, os.environ.get("QC6_WORKSPACE", "My workspace"),
                                            os.environ.get("QC6_OWNER_EMAIL", "owner@localhost"),
                                            os.environ.get("QC6_OWNER_NAME", "Owner"))
            m = await pool.fetchrow("SELECT * FROM members WHERE role = 'owner' ORDER BY id LIMIT 1")
    else:
        m = None
    if m is None:
        raise problem(401, "Sign in first", "No workspace member matches this session.")
    return dict(m)


async def site_for(site_id: int, me: Dict[str, Any]) -> Dict[str, Any]:
    pool = await get_pool()
    s = await pool.fetchrow("SELECT * FROM sites WHERE id = $1 AND workspace_id = $2", site_id, me["workspace_id"])
    if s is None:
        raise problem(404, "Site not found")
    return dict(s)


def can_edit(me) -> None:
    if me["role"] not in ("owner", "editor"):
        raise problem(403, "Viewers can't change this", "Ask an owner to make you an editor.")


def public_site(s: Dict[str, Any]) -> Dict[str, Any]:
    from qc6.notify import mask
    out = {k: v for k, v in s.items() if k != "access_token"}
    st = dict(out.get("settings") or {})
    if st.get("staging"):
        from qc6.site_access import public_staging
        st["staging"] = public_staging(st["staging"])          # never the login itself, not even encrypted
    if st.get("alerts"):
        al = dict(st["alerts"])
        for ch in ("slack", "teams"):
            if al.get(f"{ch}_webhook"):
                al[f"{ch}_webhook"] = mask(al[f"{ch}_webhook"])
        st["alerts"] = al
    for k in ("github_token", "jira_token"):
        if st.get(k):
            st[k] = "set"
    out["settings"] = st
    return out


def fix_out(f) -> Dict[str, Any]:
    d = dict(f)
    d.pop("fix_key", None)
    return d


# ------------------------------------------------------------------ me / members
@app.get("/api/v2/me")
async def me_(me=Depends(current_member)):
    pool = await get_pool()
    ws = await pool.fetchrow("SELECT id, name FROM workspaces WHERE id = $1", me["workspace_id"])
    return {"member": {k: me[k] for k in ("id", "name", "email", "role")}, "workspace": dict(ws)}


@app.get("/api/v2/members")
async def members(me=Depends(current_member)):
    pool = await get_pool()
    rows = await pool.fetch("SELECT id, name, email, role FROM members WHERE workspace_id = $1 ORDER BY name", me["workspace_id"])
    return {"members": [dict(r) for r in rows]}


# ------------------------------------------------------------------ sites
class SiteIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    base_url: str = Field(min_length=4, max_length=500)
    settings: Dict[str, Any] = {}


@app.get("/api/v2/sites")
async def sites(me=Depends(current_member)):
    pool = await get_pool()
    rows = await pool.fetch("""
        SELECT s.*, (SELECT score FROM score_snapshots ss WHERE ss.site_id = s.id ORDER BY ss.id DESC LIMIT 1) AS score,
               (SELECT row_to_json(r) FROM (SELECT id, kind, status, created_at, finished_at FROM runs WHERE site_id = s.id
                 ORDER BY id DESC LIMIT 1) r) AS last_run,
               (SELECT COUNT(*) FROM fixes f WHERE f.site_id = s.id AND f.status IN ('open','in_progress')) AS open_fixes
          FROM sites s WHERE s.workspace_id = $1 ORDER BY s.name""", me["workspace_id"])
    return {"sites": [public_site(dict(r)) for r in rows]}


@app.post("/api/v2/sites", status_code=201)
async def create_site(body: SiteIn, me=Depends(current_member)):
    can_edit(me)
    pool = await get_pool()
    try:
        sid = await services.create_site(pool, me["workspace_id"], body.name, body.base_url, body.settings, me["id"])
    except services.NotAllowed as e:
        raise problem(400, "Check the address", str(e))
    except Exception as e:
        if "unique" in str(e).lower():
            raise problem(409, "This site is already set up")
        raise
    return public_site(await site_for(sid, me))


@app.get("/api/v2/sites/{site_id}")
async def site(site_id: int, me=Depends(current_member)):
    return public_site(await site_for(site_id, me))


class SitePatch(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    rate_rps: Optional[float] = Field(default=None, gt=0, le=200)
    visitors: Optional[int] = Field(default=None, ge=1, le=20)
    mobile_share: Optional[float] = Field(default=None, ge=0, le=1)
    consent: Optional[str] = None
    check_schedule: Optional[str] = Field(default=None, max_length=60)
    sitemap: Optional[str] = Field(default=None, max_length=500)
    alerts: Optional[Dict[str, Any]] = None
    tickets: Optional[Dict[str, Any]] = None
    preset: Optional[str] = None
    gsc_property: Optional[str] = Field(default=None, max_length=300)
    ga4_property_id: Optional[str] = Field(default=None, max_length=20)
    scope: Optional[Dict[str, List[str]]] = None
    staging: Optional[Dict[str, Any]] = None
    max_pages: Optional[int] = Field(default=None, ge=1, le=1_000_000)
    read_timeout: Optional[float] = Field(default=None, ge=2, le=120)
    max_retries: Optional[int] = Field(default=None, ge=0, le=5)


@app.patch("/api/v2/sites/{site_id}")
async def patch_site(site_id: int, body: SitePatch, me=Depends(current_member)):
    can_edit(me)
    s = await site_for(site_id, me)
    st = dict(s["settings"] or {})
    if body.consent is not None and body.consent not in ("accept", "reject"):
        raise problem(400, "Cookie banner choice must be accept or reject")
    if body.check_schedule is not None:
        from qc6.scheduler import ScheduleError, parse as parse_schedule
        try:
            parse_schedule(body.check_schedule)
        except ScheduleError as e:
            raise problem(400, "Check the schedule", str(e))
    if body.alerts is not None:
        from qc6.notify import valid_webhook
        al = dict(st.get("alerts") or {})
        for ch in ("slack", "teams"):
            v = body.alerts.get(f"{ch}_webhook")
            if v is None or (isinstance(v, str) and "…" in v):
                continue                                   # unchanged (the API only ever shows a masked value)
            if v == "":
                al.pop(f"{ch}_webhook", None)
            elif not valid_webhook(ch, v):
                raise problem(400, f"That isn't a {ch.title()} incoming webhook address",
                              "Copy the webhook URL from the channel's Incoming Webhooks setting.")
            else:
                al[f"{ch}_webhook"] = v
        if "on" in body.alerts:
            al["on"] = [k for k in body.alerts["on"] if k in ("summary", "regressed", "journey_failed", "access")]
        st["alerts"] = al
    if body.tickets is not None:
        from qc6.tickets import clean_config
        try:
            st["tickets"] = clean_config({**(st.get("tickets") or {}), **body.tickets})
        except ValueError as e:
            raise problem(400, "Check the ticket settings", str(e))
    from qc6.site_access import AccessError, clean_scope, clean_staging
    try:
        if body.scope is not None:
            st["scope"] = clean_scope(body.scope)
        if body.staging is not None:
            stg = clean_staging(body.staging, st.get("staging"))
            if stg is None:
                st.pop("staging", None)
            else:
                st["staging"] = stg
    except AccessError as e:
        raise problem(400, "Check the crawl scope / staging access", str(e))
    if body.gsc_property is not None:
        g = body.gsc_property.strip()
        if g and not (g.startswith("sc-domain:") or re.match(r"^https?://[^/\s]+/$", g)):
            raise problem(400, "Check the Search Console property",
                          'Use "sc-domain:example.com" for a domain property or the full address ending in / for a URL-prefix property.')
        st["gsc_property"] = g
    if body.ga4_property_id is not None:
        if body.ga4_property_id and not body.ga4_property_id.isdigit():
            raise problem(400, "The GA4 property id is a number", "Find it in GA4 under Admin, Property details.")
        st["ga4_property_id"] = body.ga4_property_id
    if body.preset is not None:
        from qc6.presets import PRESETS
        if body.preset not in PRESETS:
            raise problem(400, "Pick one of the speed presets", ", ".join(PRESETS))
        st["preset"] = body.preset
        if body.rate_rps is None:                          # choosing a preset sets its speed
            st["rate_rps"] = PRESETS[body.preset]["target_rps"]
    for k in ("rate_rps", "visitors", "mobile_share", "consent", "check_schedule", "sitemap", "max_pages", "read_timeout", "max_retries"):
        v = getattr(body, k)
        if v is not None:
            st[k] = v
    pool = await get_pool()
    await pool.execute("UPDATE sites SET settings = $2, name = COALESCE($3, name) WHERE id = $1", site_id, st, body.name)
    await services.log(pool, me["workspace_id"], me["id"], "site.update", f"site:{site_id}",
                       {k: v for k, v in body.model_dump(exclude_none=True).items() if k not in ("alerts", "tickets", "staging")}
                       | ({"staging": "changed"} if body.staging is not None else {}))
    return public_site(await site_for(site_id, me))


@app.post("/api/v2/sites/{site_id}/alerts/test")
async def test_alert(site_id: int, me=Depends(current_member)):
    can_edit(me)
    s = await site_for(site_id, me)
    from qc6 import notify
    cfg = (s["settings"] or {}).get("alerts") or {}
    sent = []
    for ch in ("slack", "teams"):
        url = cfg.get(f"{ch}_webhook")
        if url and notify.valid_webhook(ch, url):
            try:
                status = await notify._post(ch, url, f"{s['name']}: test alert from QC", ["If you can read this, alerts work."])
                sent.append({"channel": ch, "ok": 200 <= status < 300, "status": status})
            except httpx.HTTPError as e:
                sent.append({"channel": ch, "ok": False, "error": type(e).__name__})
    if not sent:
        raise problem(400, "No alert channel set", "Add a Slack or Teams webhook first.")
    await services.log(await get_pool(), me["workspace_id"], me["id"], "alerts.test", f"site:{site_id}",
                       {"sent": [x["channel"] for x in sent]})
    return {"sent": sent}


class RecordingIn(BaseModel):
    code: str = Field(min_length=10, max_length=50_000)
    name: str = Field(min_length=1, max_length=120)
    device: str = "desktop"


@app.post("/api/v2/sites/{site_id}/journeys/convert")
async def convert_recording(site_id: int, body: RecordingIn, me=Depends(current_member)):
    await site_for(site_id, me)
    from qc6.recorder import convert
    from qc6.workers.journey import SpecError
    try:
        return convert(body.code, body.name, body.device)
    except SpecError as e:
        raise problem(400, "The recording couldn't be turned into a journey", str(e))


# ------------------------------------------------------------------ today
def _ago_sentence(run) -> str:
    if not run:
        return "No checks yet."
    s = run["summary"] or {}
    parts = []
    if s.get("pages"):
        parts.append(f"Checked {R.format_count(int(s['pages']))} pages")
    if s.get("visited"):
        parts.append(f"visited {R.format_count(int(s['visited']))} of them in a real browser")
    if s.get("journeys"):
        parts.append(f"walked {s['journeys']} journey{'s' if s['journeys'] != 1 else ''}")
    return (", ".join(parts) + ".") if parts else "The last check finished."


@app.get("/api/v2/sites/{site_id}/today")
async def today(site_id: int, me=Depends(current_member)):
    s = await site_for(site_id, me)
    pool = await get_pool()
    last = await pool.fetchrow("SELECT * FROM runs WHERE site_id = $1 ORDER BY id DESC LIMIT 1", site_id)
    done = await pool.fetchrow("SELECT * FROM runs WHERE site_id = $1 AND status = 'completed' AND NOT (options ? 'spot') ORDER BY id DESC LIMIT 1", site_id)
    cards: List[Dict[str, Any]] = []
    for f in await pool.fetch("SELECT * FROM fixes WHERE site_id = $1 AND regressed AND status = 'open' ORDER BY pages_affected DESC LIMIT 3", site_id):
        cards.append({"kind": "regressed", "fix_id": f["id"], "title": f["title"], "text": "It was fixed and verified before; the last check found it again.",
                      "action": "Open fix"})
    for j in await pool.fetch("""SELECT j.name, jr.failure_text, jr.id FROM journeys j JOIN LATERAL (
                                   SELECT * FROM journey_runs WHERE journey_id = j.id ORDER BY id DESC LIMIT 1) jr ON true
                                 WHERE j.site_id = $1 AND jr.status != 'passed'""", site_id):
        cards.append({"kind": "journey", "journey_run_id": j["id"], "title": f"\"{j['name']}\" fails", "text": j["failure_text"] or "",
                      "action": "See the step"})
    hb = await pool.fetchrow("SELECT paused_until, pause_reason FROM host_budget WHERE host = $1 AND paused_until > now()", s["primary_host"])
    if hb:
        cards.append({"kind": "access", "title": f"{s['primary_host']} paused our visitors", "text": hb["pause_reason"],
                      "action": "Copy the rule"})
    for f in await pool.fetch("""SELECT * FROM fixes WHERE site_id = $1 AND status = 'open' AND owner_id IS NULL AND NOT regressed
                                 AND rule != 'JOURNEY_FAILED'
                                 AND severity IN ('critical','high') ORDER BY CASE severity WHEN 'critical' THEN 0 ELSE 1 END,
                                 pages_affected DESC LIMIT 3""", site_id):
        cards.append({"kind": "unassigned", "fix_id": f["id"], "title": f["title"], "text": "Nobody owns this yet.", "action": "Assign"})
    scores = await pool.fetch("SELECT score, created_at FROM score_snapshots WHERE site_id = $1 ORDER BY id DESC LIMIT 30", site_id)
    counts = await pool.fetchrow("""SELECT COUNT(*) FILTER (WHERE status IN ('open','in_progress')) AS open,
                                           COUNT(*) FILTER (WHERE status = 'fixed_pending_check') AS waiting,
                                           COUNT(*) FILTER (WHERE status = 'verified') AS verified FROM fixes WHERE site_id = $1""", site_id)
    first = me["name"].split()[0] if me["name"] else ""
    n = len(cards[:5])
    if last and last["status"] in ("running", "queued"):
        lede = "A check is running now. You can watch it live."
    elif done:
        lede = (f"The last check finished {done['finished_at']:%d %b at %H:%M}. " +
                (f"{n} thing{'s' if n != 1 else ''} need{'s' if n == 1 else ''} you." if n else "Nothing needs you right now."))
    else:
        lede = "Run the first check to see how the site is doing."
    sev = {r["severity"]: r["n"] for r in await pool.fetch(
        "SELECT severity, count(*) AS n FROM fixes WHERE site_id = $1 AND status IN ('open','in_progress') GROUP BY severity", site_id)}
    jr = await pool.fetchrow("""SELECT count(*) AS total, count(*) FILTER (WHERE jr.status = 'passed') AS passed FROM journeys j
                                JOIN LATERAL (SELECT status FROM journey_runs WHERE journey_id = j.id ORDER BY id DESC LIMIT 1) jr ON true
                                WHERE j.site_id = $1 AND j.enabled""", site_id)
    flow_runs = await pool.fetch("""SELECT id, finished_at FROM runs WHERE site_id = $1 AND status = 'completed' AND NOT (options ? 'spot') ORDER BY id DESC LIMIT 8""", site_id)
    ev = {(r["run_id"], r["kind"]): r["n"] for r in await pool.fetch(
        """SELECT e.run_id, e.kind, count(DISTINCT e.fix_id) AS n FROM fix_events e JOIN fixes f ON f.id = e.fix_id
           WHERE f.site_id = $1 AND e.run_id = ANY($2::bigint[]) AND e.kind IN ('found','verified','regressed') GROUP BY 1, 2""",
        site_id, [r["id"] for r in flow_runs])}
    flow = [{"run_id": r["id"], "at": r["finished_at"], "new": ev.get((r["id"], "found"), 0), "verified": ev.get((r["id"], "verified"), 0),
             "regressed": ev.get((r["id"], "regressed"), 0)} for r in reversed(flow_runs)]
    summ = (done["summary"] if done else None) or {}
    kpis = {"open_by_severity": {k: sev.get(k, 0) for k in ("critical", "high", "medium", "low")},
            "pages": summ.get("pages"), "visited": summ.get("visited"),
            "journeys": {"total": jr["total"], "passed": jr["passed"]} if jr else {"total": 0, "passed": 0},
            "score_change": round(scores[0]["score"] - scores[1]["score"], 1) if len(scores) > 1 else None}
    return {"greeting": f"Hello, {first}" if first else "Hello", "lede": lede, "cards": cards[:5], "kpis": kpis, "flow": flow,
            "score": scores[0]["score"] if scores else None,
            "trend": [{"score": r["score"], "at": r["created_at"]} for r in reversed(scores)],
            "checked": _ago_sentence(done), "last_run": dict(last) if last else None, "fixes": dict(counts),
            "site": public_site(s)}


@app.get("/api/v2/sites/{site_id}/insights")
async def insights(site_id: int, runs: int = Query(12, ge=2, le=60), me=Depends(current_member)):
    from qc6.insights import site_insights
    await site_for(site_id, me)
    return await site_insights(await get_pool(), site_id, runs)


@app.get("/api/v2/sites/{site_id}/presets")
async def presets(site_id: int, me=Depends(current_member)):
    """V5 speed presets with how long the page check would take for this site (from its last check's size)."""
    from qc6.presets import DEFAULT_PRESET, PRESETS, estimate
    s = await site_for(site_id, me)
    pool = await get_pool()
    pages = await pool.fetchval("""SELECT (summary->>'pages')::int FROM runs WHERE site_id = $1 AND status = 'completed' AND NOT (options ? 'spot')
                                   AND summary ? 'pages' ORDER BY id DESC LIMIT 1""", site_id)
    st = s["settings"] or {}
    return {"current": st.get("preset", DEFAULT_PRESET), "rate_rps": st.get("rate_rps"), "pages": pages,
            "presets": [{"id": k, **v, "estimate": estimate(pages, v["target_rps"]) if pages else None} for k, v in PRESETS.items()]}


# ------------------------------------------------------------------ fixes
VIEWS = {"needs": "status IN ('open','in_progress')", "verify": "status = 'fixed_pending_check'",
         "verified": "status = 'verified'", "all": "true"}
SEV_ORDER = "CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END"


@app.get("/api/v2/sites/{site_id}/fixes")
async def list_fixes(site_id: int, view: str = "needs", q: str = "", limit: int = Query(100, le=500), offset: int = 0,
                     me=Depends(current_member)):
    await site_for(site_id, me)
    if view not in VIEWS:
        raise problem(400, "Unknown view")
    pool = await get_pool()
    args: List[Any] = [site_id]
    where = f"f.site_id = $1 AND {VIEWS[view]}"
    if q:
        args.append(f"%{q.lower()}%")
        where += f" AND (lower(f.title) LIKE ${len(args)} OR lower(coalesce(t.name,'')) LIKE ${len(args)})"
    rows = await pool.fetch(f"""SELECT f.*, t.name AS template_name, m.name AS owner_name FROM fixes f
        LEFT JOIN templates t ON t.id = f.template_id LEFT JOIN members m ON m.id = f.owner_id
        WHERE {where} ORDER BY f.regressed DESC, {SEV_ORDER}, f.pages_affected DESC, f.id LIMIT {int(limit)} OFFSET {int(offset)}""", *args)
    counts = await pool.fetchrow(f"""SELECT {', '.join(f"COUNT(*) FILTER (WHERE {v}) AS {k}" for k, v in VIEWS.items())}
                                     FROM fixes WHERE site_id = $1""", site_id)
    return {"fixes": [fix_out(r) for r in rows], "counts": dict(counts)}


@app.get("/api/v2/fixes/{fix_id}")
async def get_fix(fix_id: int, me=Depends(current_member)):
    pool = await get_pool()
    f = await pool.fetchrow("""SELECT f.*, t.name AS template_name, m.name AS owner_name FROM fixes f JOIN sites s ON s.id = f.site_id
        LEFT JOIN templates t ON t.id = f.template_id LEFT JOIN members m ON m.id = f.owner_id
        WHERE f.id = $1 AND s.workspace_id = $2""", fix_id, me["workspace_id"])
    if f is None:
        raise problem(404, "Fix not found")
    pages = await pool.fetch("SELECT DISTINCT page_url FROM findings WHERE fix_id = $1 AND run_id = $2 ORDER BY page_url LIMIT 50",
                             fix_id, f["last_seen_run"])
    from qc6 import history
    hist = await history.for_run(pool, f["last_seen_run"], [fix_id], [r["page_url"] for r in pages]) if f["last_seen_run"] else {}
    shots = await pool.fetch("""SELECT DISTINCT ON (fd.page_url) fd.page_url, fd.evidence->>'screenshot' AS screenshot
        FROM findings fd WHERE fd.fix_id = $1 AND fd.evidence ? 'screenshot' ORDER BY fd.page_url LIMIT 3""", fix_id)
    if not shots and f["template_id"] is not None:
        shots = await pool.fetch("""SELECT page_url, screenshot FROM visits WHERE template_id = $1 AND status = 'ok'
                                    AND screenshot IS NOT NULL ORDER BY id DESC LIMIT 3""", f["template_id"])
    events = await pool.fetch("""SELECT e.kind, e.detail, e.created_at, m.name AS actor FROM fix_events e
        LEFT JOIN members m ON m.id = e.actor_id WHERE e.fix_id = $1 ORDER BY e.id DESC LIMIT 50""", fix_id)
    return {"fix": fix_out(f), "pages": [r["page_url"] for r in pages],
            "page_history": {r["page_url"]: history.remark(hist.get((fix_id, r["page_url"]))) for r in pages},
            "examples": [dict(r) for r in shots if r["screenshot"]], "events": [dict(e) for e in events]}


class FixPatch(BaseModel):
    status: Optional[str] = None
    owner_id: Optional[int] = None
    unassign: bool = False
    comment: Optional[str] = Field(default=None, max_length=4000)
    ticket_url: Optional[str] = Field(default=None, max_length=500)


@app.patch("/api/v2/fixes/{fix_id}")
async def patch_fix(fix_id: int, body: FixPatch, me=Depends(current_member)):
    can_edit(me)
    pool = await get_pool()
    ok = await pool.fetchval("SELECT 1 FROM fixes f JOIN sites s ON s.id = f.site_id WHERE f.id = $1 AND s.workspace_id = $2",
                             fix_id, me["workspace_id"])
    if not ok:
        raise problem(404, "Fix not found")
    if body.ticket_url and not re.match(r"^https://", body.ticket_url):
        raise problem(400, "Ticket links must start with https://")
    try:
        f = await services.update_fix(pool, fix_id, me["id"], body.status, body.owner_id, body.unassign, body.comment, body.ticket_url)
    except services.NotAllowed as e:
        raise problem(409, "That change isn't possible", str(e))
    return fix_out(f)


@app.post("/api/v2/fixes/{fix_id}/ticket")
async def create_ticket(fix_id: int, request: Request, me=Depends(current_member)):
    """Create a GitHub issue / Jira ticket for a fix. When the ticket is closed there, the fix moves to
    'fixed — waiting for check' (see /ticket/sync); only the next check can verify it."""
    can_edit(me)
    pool = await get_pool()
    f = await pool.fetchrow("SELECT f.* FROM fixes f JOIN sites s ON s.id = f.site_id WHERE f.id = $1 AND s.workspace_id = $2",
                            fix_id, me["workspace_id"])
    if f is None:
        raise problem(404, "Fix not found")
    if f["ticket_url"]:
        raise problem(409, "This fix already has a ticket", f["ticket_url"])
    s = dict(await pool.fetchrow("SELECT * FROM sites WHERE id = $1", f["site_id"]))
    pages = [r["page_url"] for r in await pool.fetch("SELECT DISTINCT page_url FROM findings WHERE fix_id = $1 AND run_id = $2 LIMIT 10",
                                                     fix_id, f["last_seen_run"])]
    from qc6 import tickets
    link = str(request.base_url).rstrip("/") + f"/#/fixes?fix={fix_id}"
    try:
        t = await tickets.create((s["settings"] or {}).get("tickets") or {}, dict(f), s, pages, link)
    except ValueError as e:
        raise problem(400, "The ticket wasn't created", str(e))
    except httpx.HTTPError as e:
        raise problem(502, "The ticket service didn't answer", type(e).__name__)
    await pool.execute("UPDATE fixes SET ticket_key = $2 WHERE id = $1", fix_id, t["key"])
    await services.update_fix(pool, fix_id, me["id"], ticket_url=t["url"])
    return t


@app.post("/api/v2/sites/{site_id}/tickets/sync")
async def sync_tickets(site_id: int, me=Depends(current_member)):
    """Read the state of every linked ticket; closed ones move their fix to 'fixed — waiting for check'."""
    can_edit(me)
    s = await site_for(site_id, me)
    pool = await get_pool()
    from qc6 import tickets
    moved = 0
    for f in await pool.fetch("SELECT id, status, ticket_key FROM fixes WHERE site_id = $1 AND ticket_key IS NOT NULL "
                              "AND status IN ('open','in_progress')", site_id):
        st = await tickets.state((s["settings"] or {}).get("tickets") or {}, f["ticket_key"])
        if st == "done":
            await services.update_fix(pool, f["id"], me["id"], status="fixed_pending_check",
                                      comment=f"Ticket {f['ticket_key']} was closed.")
            moved += 1
    return {"moved_to_waiting": moved}


# ------------------------------------------------------------------ runs + live stream
class RunIn(BaseModel):
    kind: str = "full"
    options: Dict[str, Any] = {}


@app.post("/api/v2/sites/{site_id}/runs", status_code=201)
async def start_run(site_id: int, body: RunIn, me=Depends(current_member)):
    can_edit(me)
    await site_for(site_id, me)
    allowed = {"urls", "sitemap", "max_pages", "rate_rps", "preset", "per_template", "visitors", "images", "fast_visits", "fast_journeys"}
    opts = {k: v for k, v in body.options.items() if k in allowed}
    if "urls" in opts:
        from qc6.url_lists import ListError, for_site
        if not isinstance(opts["urls"], list):
            raise problem(400, "urls must be a list of addresses")
        try:
            opts["urls"] = for_site([str(u) for u in opts["urls"]], await site_for(site_id, me))["urls"]
        except ListError as e:
            raise problem(400, "Check the addresses", str(e))
    pool = await get_pool()
    try:
        rid = await services.start_run(pool, site_id, body.kind, opts, me["id"])
    except services.NotAllowed as e:
        raise problem(409, "A check is already running", str(e))
    return {"run_id": rid}


class SpotIn(BaseModel):
    text: str = Field(max_length=20_000_000)
    kind: str = "coverage"
    whole_site: bool = False


async def _start_list_check(site_id: int, me, urls: List[str], kind: str, whole_site: bool, source: str, name: str = ""):
    from qc6.url_lists import ListError, for_site
    can_edit(me)
    s = await site_for(site_id, me)
    if kind not in ("coverage", "full"):
        raise problem(400, "Pick a page check or a full check")
    try:
        got = for_site(urls, s)
    except ListError as e:
        raise problem(400, "Check the addresses", str(e))
    n = len(got["urls"])
    label = (got["urls"][0] if n == 1 else f"{n:,} pages" + (f" from {name}" if name else ""))
    opts: Dict[str, Any] = {"urls": got["urls"]}
    if not whole_site:
        opts["spot"] = {"label": label, "source": source, "file": name or None, "count": n}
    pool = await get_pool()
    try:
        rid = await services.start_run(pool, site_id, kind, opts, me["id"])
    except services.NotAllowed as e:
        raise problem(409, "A check is already running", str(e))
    await services.log(pool, me["workspace_id"], me["id"], "run.start_list", f"run:{rid}", {"source": source, "file": name, "urls": n})
    return {"run_id": rid, "urls": n, "other_hosts": got["other_hosts"], "other_examples": got["other_examples"],
            "duplicates": got["duplicates"], "spot": not whole_site, "label": label}


@app.post("/api/v2/sites/{site_id}/checks/urls", status_code=201)
async def check_urls(site_id: int, body: SpotIn, me=Depends(current_member)):
    """Check one address or a pasted list. A spot check: results of its own, site-wide figures untouched."""
    from qc6.url_lists import from_text
    urls = from_text(body.text)
    return await _start_list_check(site_id, me, urls, body.kind, body.whole_site, "single" if len(urls) == 1 else "list")


@app.post("/api/v2/sites/{site_id}/checks/upload", status_code=201)
async def check_upload(site_id: int, request: Request, me=Depends(current_member)):
    """Check the URLs in an uploaded sheet (.csv, .tsv, .txt or .xlsx; the URL column or any http(s) cell)."""
    from qc6.url_lists import ListError, MAX_BYTES, from_upload
    form = await request.form()
    f = form.get("file")
    if f is None or not hasattr(f, "read"):
        raise problem(400, "Choose a file to upload")
    data = await f.read(MAX_BYTES + 1)
    name = os.path.basename(getattr(f, "filename", "") or "list")[:120]
    try:
        urls = from_upload(data, name)
    except ListError as e:
        raise problem(400, "That file can't be used", str(e))
    return await _start_list_check(site_id, me, urls, str(form.get("kind") or "coverage"),
                                   str(form.get("whole_site") or "").lower() in ("1", "true", "on", "yes"), "sheet", name)


@app.get("/api/v2/sites/{site_id}/runs")
async def runs(site_id: int, me=Depends(current_member)):
    await site_for(site_id, me)
    pool = await get_pool()
    rows = await pool.fetch("SELECT id, kind, status, progress, summary, error, created_at, started_at, finished_at, "
                            "options->'spot' AS spot FROM runs WHERE site_id = $1 ORDER BY id DESC LIMIT 50", site_id)
    return {"runs": [dict(r) for r in rows]}


@app.get("/api/v2/runs/{run_id}")
async def run(run_id: int, me=Depends(current_member)):
    pool = await get_pool()
    r = await pool.fetchrow("SELECT r.* FROM runs r JOIN sites s ON s.id = r.site_id WHERE r.id = $1 AND s.workspace_id = $2",
                            run_id, me["workspace_id"])
    if r is None:
        raise problem(404, "Run not found")
    visits = await pool.fetch("SELECT page_url, device, status, timeline, started_at FROM visits WHERE run_id = $1 ORDER BY id DESC LIMIT 20", run_id)
    found = await pool.fetch("""SELECT f.id, f.title, f.severity, COUNT(DISTINCT fd.page_url) AS pages FROM findings fd
        JOIN fixes f ON f.id = fd.fix_id WHERE fd.run_id = $1 GROUP BY f.id ORDER BY MAX(fd.id) DESC LIMIT 12""", run_id)
    return {"run": dict(r), "recent_visits": [dict(v) for v in visits], "found": [dict(x) for x in found]}


async def _run_transition(run_id: int, me, src: tuple, dst: str, title: str, detail: str):
    can_edit(me)
    pool = await get_pool()
    row = await pool.fetchrow("UPDATE runs r SET status = $3, finished_at = CASE WHEN $3 = 'cancelled' THEN now() ELSE r.finished_at END "
                              "FROM sites s WHERE r.id = $1 AND s.id = r.site_id AND s.workspace_id = $2 AND r.status = ANY($4::text[]) "
                              "RETURNING r.id, r.site_id", run_id, me["workspace_id"], dst, list(src))
    if row is None:
        raise problem(409, title, detail)
    await services.log(pool, me["workspace_id"], me["id"], f"run.{dst}", f"run:{run_id}", {})
    return {"status": dst}


@app.post("/api/v2/runs/{run_id}/pause")
async def pause_run(run_id: int, me=Depends(current_member)):
    return await _run_transition(run_id, me, ("running",), "pausing", "Only a running check can be paused", "")


@app.post("/api/v2/runs/{run_id}/resume")
async def resume_run(run_id: int, me=Depends(current_member)):
    return await _run_transition(run_id, me, ("paused",), "queued", "Only a paused check can be resumed", "")


@app.post("/api/v2/runs/{run_id}/stop")
async def stop_run(run_id: int, me=Depends(current_member)):
    return await _run_transition(run_id, me, ("queued", "paused"), "cancelled", "This check can't be cancelled now",
                                 "Pause it first; a paused or queued check can be cancelled.")


# ------------------------------------------------------------------ delete
@app.delete("/api/v2/runs/{run_id}")
async def delete_run(run_id: int, me=Depends(current_member)):
    """Delete a check (full or spot) with its pages, findings, visits, scores and files. Not while it runs."""
    from qc6 import deletion
    can_edit(me)
    pool = await get_pool()
    if not await pool.fetchval("SELECT EXISTS (SELECT 1 FROM runs r JOIN sites s ON s.id = r.site_id WHERE r.id = $1 AND s.workspace_id = $2)",
                               run_id, me["workspace_id"]):
        raise problem(404, "Check not found")
    try:
        out = await deletion.delete_check(pool, run_id)
    except deletion.Refused as e:
        raise problem(409, "This check can't be deleted now", str(e))
    await services.log(pool, me["workspace_id"], me["id"], "run.delete", f"run:{run_id}", out)
    return out


@app.delete("/api/v2/journeys/{journey_id}")
async def delete_journey(journey_id: int, me=Depends(current_member)):
    from qc6 import deletion
    can_edit(me)
    pool = await get_pool()
    if not await pool.fetchval("SELECT EXISTS (SELECT 1 FROM journeys j JOIN sites s ON s.id = j.site_id WHERE j.id = $1 AND s.workspace_id = $2)",
                               journey_id, me["workspace_id"]):
        raise problem(404, "Journey not found")
    out = await deletion.delete_journey(pool, journey_id)
    await services.log(pool, me["workspace_id"], me["id"], "journey.delete", f"journey:{journey_id}", out)
    return out


class SiteDelete(BaseModel):
    confirm: str


@app.delete("/api/v2/sites/{site_id}")
async def delete_site(site_id: int, body: SiteDelete, me=Depends(current_member)):
    """Delete a site and everything QC stored about it. Owners only; type the site's name to confirm."""
    from qc6 import deletion
    if me["role"] != "owner":
        raise problem(403, "Only an owner can delete a site")
    s = await site_for(site_id, me)
    if body.confirm.strip() != s["name"]:
        raise problem(400, "Type the site's name to confirm", f'Type "{s["name"]}" exactly.')
    pool = await get_pool()
    try:
        out = await deletion.delete_site(pool, site_id)
    except deletion.Refused as e:
        raise problem(409, "The site can't be deleted now", str(e))
    await services.log(pool, me["workspace_id"], me["id"], "site.delete", f"site:{site_id}", out)
    return out


@app.get("/api/v2/stream")
async def stream(run: int, request: Request, me=Depends(current_member)):
    """Server-Sent Events for one run: progress, visitor steps, journeys, completion."""
    pool = await get_pool()
    ok = await pool.fetchval("SELECT 1 FROM runs r JOIN sites s ON s.id = r.site_id WHERE r.id = $1 AND s.workspace_id = $2",
                             run, me["workspace_id"])
    if not ok:
        raise problem(404, "Run not found")
    queue: asyncio.Queue = asyncio.Queue(maxsize=1000)

    def on_msg(conn, pid, channel, payload):
        try:
            data = json.loads(payload)
        except ValueError:
            return
        if data.get("run") == run and not queue.full():
            queue.put_nowait(data)

    conn = await pool.acquire()
    await conn.add_listener("qc6", on_msg)

    async def gen():
        try:
            yield "retry: 3000\n\n"
            last_beat = time.monotonic()
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=1.0)
                    yield f"event: {ev.get('event', 'message')}\ndata: {json.dumps(ev, default=str)}\n\n"
                    if ev.get("event") in ("completed", "failed"):
                        break
                except asyncio.TimeoutError:
                    if time.monotonic() - last_beat > 15:
                        yield ": keep-alive\n\n"
                        last_beat = time.monotonic()
        finally:
            await conn.remove_listener("qc6", on_msg)
            await pool.release(conn)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# ------------------------------------------------------------------ templates, pages, visits
@app.get("/api/v2/sites/{site_id}/templates")
async def templates_(site_id: int, me=Depends(current_member)):
    await site_for(site_id, me)
    pool = await get_pool()
    rows = await pool.fetch("""SELECT t.id, t.name, t.path_pattern, t.page_count,
        (SELECT COUNT(*) FROM visits v WHERE v.template_id = t.id) AS visits,
        (SELECT COUNT(*) FROM fixes f WHERE f.template_id = t.id AND f.status IN ('open','in_progress')) AS open_fixes
        FROM templates t WHERE t.site_id = $1 AND t.id IN (SELECT DISTINCT template_id FROM pages p JOIN runs r ON r.id = p.run_id
        WHERE r.site_id = $1 AND r.id = (SELECT MAX(id) FROM runs WHERE site_id = $1 AND status = 'completed' AND kind IN ('coverage','full') AND NOT (options ? 'spot')))
        ORDER BY t.page_count DESC""", site_id)
    return {"templates": [dict(r) for r in rows]}


@app.get("/api/v2/sites/{site_id}/page")
async def page(site_id: int, url: str, me=Depends(current_member)):
    await site_for(site_id, me)
    pool = await get_pool()
    p = await pool.fetchrow("""SELECT p.*, t.name AS template_name FROM pages p JOIN runs r ON r.id = p.run_id
        LEFT JOIN templates t ON t.id = p.template_id WHERE r.site_id = $1 AND p.url = $2 ORDER BY p.id DESC LIMIT 1""", site_id, url)
    if p is None:
        raise problem(404, "We haven't checked this page yet")
    v = await pool.fetchrow("""SELECT v.* FROM visits v JOIN runs r ON r.id = v.run_id WHERE r.site_id = $1 AND v.page_url = $2
        ORDER BY v.id DESC LIMIT 1""", site_id, url)
    fixes = await pool.fetch("""SELECT f.id, f.title, f.severity, f.status, count(DISTINCT fd.run_id) AS checks,
                                       min(coalesce(r.finished_at, r.started_at)) AS first_seen, max(coalesce(r.finished_at, r.started_at)) AS last_seen
        FROM findings fd JOIN fixes f ON f.id = fd.fix_id JOIN runs r ON r.id = fd.run_id AND r.status = 'completed'
        WHERE f.site_id = $1 AND fd.page_url = $2 AND f.status != 'verified'
        GROUP BY f.id ORDER BY CASE f.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END""", site_id, url)
    return {"page": dict(p), "visit": dict(v) if v else None, "fixes": [dict(f) for f in fixes]}


@app.get("/api/v2/sites/{site_id}/pages")
async def pages(site_id: int, q: str = "", limit: int = Query(50, le=500), me=Depends(current_member)):
    await site_for(site_id, me)
    pool = await get_pool()
    rid = await pool.fetchval("SELECT MAX(id) FROM runs WHERE site_id = $1 AND status = 'completed' AND kind IN ('coverage','full') AND NOT (options ? 'spot')", site_id)
    rows = await pool.fetch("""SELECT p.url, p.http_status, p.title, t.name AS template_name FROM pages p LEFT JOIN templates t
        ON t.id = p.template_id WHERE p.run_id = $1 AND ($2 = '' OR p.url ILIKE '%' || $2 || '%' OR p.title ILIKE '%' || $2 || '%')
        ORDER BY p.url LIMIT $3""",
                            rid, q, limit)
    return {"pages": [dict(r) for r in rows]}


INVENTORY_FILTERS = ("status", "template", "indexable", "canonical", "depth_min", "fixes", "change", "q")


async def _inventory_run(site_id: int, run: Optional[int], pool) -> int:
    from qc6.inventory import checks_with_pages
    checks = await checks_with_pages(pool, site_id)
    if not checks:
        raise problem(404, "No finished check yet", "Pages appear here after the first check.")
    if run is None:                                   # the latest check of the whole site, not a spot check
        return next((c["id"] for c in checks if not c["spot"]), checks[0]["id"])
    if run not in {c["id"] for c in checks}:
        raise problem(404, "That check isn't one of this site's finished checks")
    return run


@app.get("/api/v2/sites/{site_id}/inventory")
async def inventory(site_id: int, request: Request, run: Optional[int] = None, sort: str = "url", desc: bool = False,
                    limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0), me=Depends(current_member)):
    """Every page of a check, filterable: status, page type, indexable, canonical, depth, open fixes, new / gone / changed."""
    from qc6.inventory import checks_with_pages, list_pages
    await site_for(site_id, me)
    pool = await get_pool()
    rid = await _inventory_run(site_id, run, pool)
    f = {k: request.query_params.get(k) for k in INVENTORY_FILTERS if request.query_params.get(k)}
    try:
        out = await list_pages(pool, site_id, rid, f, sort, desc, limit, offset)
    except ValueError:
        raise problem(400, "Check the filters", "template and depth_min are numbers.")
    out["checks"] = await checks_with_pages(pool, site_id)
    return out


@app.get("/api/v2/sites/{site_id}/inventory.csv")
async def inventory_csv(site_id: int, request: Request, run: Optional[int] = None, me=Depends(current_member)):
    from qc6.inventory import csv_rows
    s = await site_for(site_id, me)
    pool = await get_pool()
    rid = await _inventory_run(site_id, run, pool)
    f = {k: request.query_params.get(k) for k in INVENTORY_FILTERS if request.query_params.get(k)}
    name = re.sub(r"[^a-z0-9.-]+", "-", s["primary_host"].lower())
    return StreamingResponse(csv_rows(pool, site_id, rid, f), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="pages-{name}-check-{rid}.csv"'})


@app.get("/api/v2/sites/{site_id}/export.{fmt}")
async def export_report(site_id: int, fmt: str, run: Optional[int] = None, me=Depends(current_member)):
    """The whole report of one check in one sheet: summary, problems, every page x problem with 'seen before' remarks."""
    from qc6 import export as ex
    if fmt not in ("csv", "xlsx", "zip"):
        raise problem(404, "Not found")
    s = await site_for(site_id, me)
    pool = await get_pool()
    rid = await _inventory_run(site_id, run, pool)
    name = f"qc-report-{re.sub(r'[^a-z0-9.-]+', '-', s['primary_host'].lower())}-check-{rid}.{fmt}"
    await services.log(pool, me["workspace_id"], me["id"], f"report.{fmt}", f"run:{rid}", {})
    if fmt == "csv":
        return StreamingResponse(ex.csv_stream(pool, s, rid), media_type="text/csv; charset=utf-8",
                                 headers={"Content-Disposition": f'attachment; filename="{name}"'})
    from starlette.background import BackgroundTask
    if fmt == "zip":
        path = await ex.zip_file(pool, s, rid, name[:-4] + ".csv")
        return FileResponse(path, filename=name, background=BackgroundTask(os.unlink, path), media_type="application/zip")
    path = await ex.xlsx_file(pool, s, rid)
    return FileResponse(path, filename=name, background=BackgroundTask(os.unlink, path),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/api/v2/sites/{site_id}/compare")
async def compare_checks(site_id: int, a: int, b: int, me=Depends(current_member)):
    """Two checks side by side: pages added / gone / changed status, fixes new / confirmed / back again, score."""
    from qc6.inventory import compare
    await site_for(site_id, me)
    try:
        return await compare(await get_pool(), site_id, a, b)
    except ValueError as e:
        raise problem(400, str(e))
    except LookupError as e:
        raise problem(404, str(e))


@app.get("/api/v2/sites/{site_id}/growth/opportunities")
async def opportunities(site_id: int, bucket: str = "", limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                        min_impressions: int = Query(2500, ge=1), me=Depends(current_member)):
    """Pages ranked by QC's opportunity score, with the reasons in words and an action bucket (CrawlPulse FEAT-15)."""
    from qc6.opportunity import explain, page_opportunities
    await site_for(site_id, me)
    pool = await get_pool()
    rid = await _inventory_run(site_id, None, pool)
    d = await page_opportunities(pool, site_id, rid, min_impressions, with_reasons=0)
    rows = [p for p in d["pages"] if not bucket or p["bucket"] == bucket]
    return {**{k: v for k, v in d.items() if k not in ("pages", "_explain")}, "total": len(rows),
            "pages": explain(d, rows[offset:offset + limit])}


@app.get("/api/v2/sites/{site_id}/growth/search")
async def search_insights(site_id: int, striking_min: int = Query(2500, ge=1), cannibal_min: int = Query(1000, ge=1),
                          me=Depends(current_member)):
    from qc6 import search_console as sc
    s = await site_for(site_id, me)
    pool = await get_pool()
    return {**await sc.status(pool, s), "striking": await sc.striking_distance(pool, site_id, striking_min),
            "cannibalisation": await sc.cannibalisation(pool, site_id, cannibal_min),
            "thresholds": {"striking_min_impressions": striking_min, "cannibal_min_impressions": cannibal_min}}


@app.post("/api/v2/sites/{site_id}/growth/sync")
async def search_sync(site_id: int, me=Depends(current_member)):
    from qc6 import search_console as sc
    can_edit(me)
    s = await site_for(site_id, me)
    pool = await get_pool()
    try:
        out = await sc.sync(pool, s)
    except (RuntimeError, ValueError) as e:
        await sc.record_error(pool, site_id, str(e))
        raise problem(400, "Search Console sync didn't work", str(e))
    except httpx.HTTPError as e:
        await sc.record_error(pool, site_id, f"Google didn't answer ({type(e).__name__}).")
        raise problem(502, "Google didn't answer", type(e).__name__)
    await services.log(pool, me["workspace_id"], me["id"], "search.sync", f"site:{site_id}", {"rows": out["rows"]})
    return out


REPORT_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"


@app.get("/api/v2/sites/{site_id}/report.{fmt}")
async def report(site_id: int, fmt: str, me=Depends(current_member)):
    """Executive summary (CrawlPulse FEAT-18-02) as a self-contained page, or the same page printed to PDF."""
    from fastapi.responses import HTMLResponse, Response
    from qc6 import report as rep
    if fmt not in ("html", "pdf"):
        raise problem(404, "Not found")
    s = await site_for(site_id, me)
    pool = await get_pool()
    if not await pool.fetchval("SELECT EXISTS (SELECT 1 FROM runs WHERE site_id = $1 AND status = 'completed')", site_id):
        raise problem(404, "No finished check yet", "The report needs at least one finished check.")
    doc = rep.render_html(await rep.gather(pool, s))
    name = re.sub(r"[^a-z0-9.-]+", "-", s["primary_host"].lower())
    if fmt == "html":
        return HTMLResponse(doc, headers={"Content-Security-Policy": REPORT_CSP})
    try:
        pdf = await rep.to_pdf(doc)
    except Exception as e:                              # Chromium missing or failed: say so, offer the HTML
        raise problem(503, "The PDF couldn't be made", f"Chromium isn't available on the server ({type(e).__name__}). "
                      "The same report is at report.html; your browser can print it to PDF.")
    await services.log(pool, me["workspace_id"], me["id"], "report.pdf", f"site:{site_id}", {})
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="qc-report-{name}.pdf"'})


@app.get("/api/v2/runs/{run_id}/facts/{dataset}")
async def facts_query(run_id: int, dataset: str, filter: str = "", q: str = Query("", max_length=300), page_url: str = "",
                      sort: str = "page", desc: bool = False, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500),
                      me=Depends(current_member)):
    """Every link / image relationship of a run (Parquet + DuckDB): filter, search, sort, page."""
    pool = await get_pool()
    ok = await pool.fetchval("SELECT 1 FROM runs r JOIN sites s ON s.id = r.site_id WHERE r.id = $1 AND s.workspace_id = $2",
                             run_id, me["workspace_id"])
    if not ok:
        raise problem(404, "Run not found")
    from qc6 import facts
    try:
        return await asyncio.to_thread(facts.query, run_id, dataset, filter, q, page_url, sort, desc, offset, limit)
    except FileNotFoundError:
        raise problem(404, "No link data for this check", "Link and image data is kept for checks that crawled every page.")
    except ValueError as e:
        raise problem(400, "Check the filter", str(e))


@app.get("/api/v2/runs/{run_id}/export/{dataset}.csv")
async def facts_csv(run_id: int, dataset: str, filter: str = "", me=Depends(current_member)):
    pool = await get_pool()
    ok = await pool.fetchval("SELECT 1 FROM runs r JOIN sites s ON s.id = r.site_id WHERE r.id = $1 AND s.workspace_id = $2",
                             run_id, me["workspace_id"])
    if not ok or dataset not in ("links", "images"):
        raise problem(404, "Not found")
    from qc6 import facts
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    try:
        await asyncio.to_thread(facts.export_csv, run_id, dataset, tmp, filter)
    except Exception:
        os.unlink(tmp)
        raise problem(404, "No link data for this check")
    from starlette.background import BackgroundTask
    return FileResponse(tmp, media_type="text/csv", filename=f"run{run_id}_{dataset}{'_' + filter if filter else ''}.csv",
                        background=BackgroundTask(os.unlink, tmp))


# ------------------------------------------------------------------ journeys
class JourneyIn(BaseModel):
    yaml: str = Field(min_length=10, max_length=20_000)


@app.get("/api/v2/sites/{site_id}/journeys")
async def journeys(site_id: int, me=Depends(current_member)):
    await site_for(site_id, me)
    pool = await get_pool()
    rows = await pool.fetch("""SELECT j.id, j.name, j.spec->>'device' AS device, j.enabled, j.source_yaml,
        COALESCE((SELECT json_agg(x ORDER BY x.id) FROM (SELECT id, status, failed_step, duration_ms, started_at FROM journey_runs
                  WHERE journey_id = j.id ORDER BY id DESC LIMIT 14) x), '[]') AS recent
        FROM journeys j WHERE j.site_id = $1 ORDER BY j.name""", site_id)
    return {"journeys": [dict(r) for r in rows]}


@app.post("/api/v2/sites/{site_id}/journeys", status_code=201)
async def save_journey(site_id: int, body: JourneyIn, me=Depends(current_member)):
    can_edit(me)
    await site_for(site_id, me)
    from qc6.workers.journey import SpecError
    try:
        jid = await services.save_journey(await get_pool(), site_id, body.yaml)
    except SpecError as e:
        raise problem(400, "The journey can't be saved", str(e))
    await services.log(await get_pool(), me["workspace_id"], me["id"], "journey.save", f"journey:{jid}", {"site_id": site_id})
    return {"journey_id": jid}


@app.get("/api/v2/journey-runs/{jr_id}")
async def journey_run(jr_id: int, me=Depends(current_member)):
    pool = await get_pool()
    jr = await pool.fetchrow("""SELECT jr.*, j.name FROM journey_runs jr JOIN journeys j ON j.id = jr.journey_id
        JOIN sites s ON s.id = j.site_id WHERE jr.id = $1 AND s.workspace_id = $2""", jr_id, me["workspace_id"])
    if jr is None:
        raise problem(404, "Journey run not found")
    steps = await pool.fetch("SELECT idx, action, label, status, detail, screenshot, duration_ms FROM step_results "
                             "WHERE journey_run_id = $1 ORDER BY idx", jr_id)
    return {"journey_run": dict(jr), "steps": [dict(s) for s in steps]}


# ------------------------------------------------------------------ crawler access (allowlisting)
@app.get("/api/v2/sites/{site_id}/access")
async def access(site_id: int, me=Depends(current_member)):
    s = await site_for(site_id, me)
    can_edit(me)                       # the token is only for people who can act on it
    return {"header": settings.access_header, "token": s["access_token"], "ua_suffix": settings.ua_suffix,
            "rules": {v: challenge.allowlist_rule(v, settings.access_header, s["access_token"], []) for v in ("akamai", "cloudflare")}}


@app.post("/api/v2/sites/{site_id}/access/test")
async def access_test(site_id: int, me=Depends(current_member)):
    """20 paced requests to the homepage with our honest identity; reports any challenge in words."""
    can_edit(me)
    s = await site_for(site_id, me)
    from qcv5.crawler.url_normalizer import URLNormalizer
    if not settings.allow_private_targets and not URLNormalizer.is_safe_url(s["base_url"]):
        raise problem(400, "This address points to a private network")
    results = []
    from qc6.site_access import AccessError, httpx_hook
    try:
        hook = httpx_hook(s)
    except AccessError as e:
        raise problem(400, "The staging login can't be used", str(e))
    async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers={"User-Agent": f"Mozilla/5.0 {settings.ua_suffix}"},
                                 event_hooks={"request": [hook]}) as c:
        for i in range(20):
            try:
                r = await c.get(s["base_url"] + "/")
                results.append(challenge.detect(r.status_code, dict(r.headers), r.text[:20000]) or "ok")
            except httpx.HTTPError as e:
                results.append(f"error: {type(e).__name__}")
            await asyncio.sleep(0.5)
    bad = [x for x in results if x != "ok"]
    text = ("All 20 requests got through. Real-visitor checks can run." if not bad else
            f"{len(bad)} of 20 requests were stopped ({bad[0]}). Send the allow rule to your CDN team.")
    return {"ok": not bad, "text": text, "results": results}


# ------------------------------------------------------------------ objects (screenshots)
@app.get("/api/v2/objects/{name}")
async def objects(name: str, me=Depends(current_member)):
    if not re.fullmatch(r"(visit|journey)-[0-9]+(-[0-9]+)?\.jpg", name):
        raise problem(404, "Not found")
    path = os.path.join(settings.objects_dir, name)
    if not os.path.isfile(path):
        raise problem(404, "Not found")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


# ------------------------------------------------------------------ integrations (free; from QC V5)
@app.get("/api/v2/integrations")
async def integrations(me=Depends(current_member)):
    from qcv5.integrations import catalogue
    return catalogue()


class IntegrationRun(BaseModel):
    url: str = Field(min_length=4, max_length=2048)
    params: Dict[str, Any] = {}


@app.post("/api/v2/integrations/{iid}/run")
async def integration_run(iid: str, body: IntegrationRun, me=Depends(current_member)):
    from qcv5.integrations import BY_ID, run_integration
    if iid not in BY_ID:
        raise problem(404, "Unknown integration")
    await services.log(await get_pool(), me["workspace_id"], me["id"], "integration.run", f"integration:{iid}", {"url": body.url})
    return await run_integration(iid, body.url, **{k: v for k, v in body.params.items() if isinstance(v, (str, int, float, bool, list))})


# ------------------------------------------------------------------ metrics (blueprint §13)
@app.get("/metrics", include_in_schema=False)
async def metrics_(request: Request):
    """Prometheus text format. With QC6_METRICS_TOKEN set, scrapers send `Authorization: Bearer <token>`;
    without it, only this machine may read it (no workspace data beyond counts is exposed either way)."""
    import hmac
    from fastapi.responses import PlainTextResponse
    from qc6 import metrics
    token = os.environ.get("QC6_METRICS_TOKEN", "")
    if token:
        given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(given.encode(), token.encode()):
            raise problem(401, "Metrics need the scrape token")
    elif (request.client.host if request.client else "") not in ("127.0.0.1", "::1", "testclient"):
        raise problem(403, "Metrics are only served to this machine", "Set QC6_METRICS_TOKEN to let a remote Prometheus scrape them.")
    return PlainTextResponse(await metrics.render(await get_pool()), media_type="text/plain; version=0.0.4")


# ------------------------------------------------------------------ web app
if os.path.isdir(WEB_DIST):
    app.mount("/assets", StaticFiles(directory=os.path.join(WEB_DIST, "assets")), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        if path.startswith("api/"):
            raise problem(404, "Not found")
        f = os.path.join(WEB_DIST, path)
        if path and os.path.isfile(f) and os.path.realpath(f).startswith(os.path.realpath(WEB_DIST)):
            return FileResponse(f)
        return FileResponse(os.path.join(WEB_DIST, "index.html"))
