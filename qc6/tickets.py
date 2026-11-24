"""Turn a fix into a GitHub issue or a Jira Cloud ticket and follow its state (free plans of both).

Site settings ("tickets"):
    GitHub: {"provider": "github", "repo": "owner/name"}             token: QC6_GITHUB_TOKEN (or "token" in settings)
    Jira:   {"provider": "jira", "base_url": "https://acme.atlassian.net", "project": "WEB", "email": "bot@acme.com"}
            token: QC6_JIRA_TOKEN (or "token" in settings)
Only api.github.com and *.atlassian.net are ever contacted (no requests to arbitrary or internal hosts).
Tokens are never returned by the API.
"""
from __future__ import annotations

import asyncio
import base64
import os
import re
import urllib.parse
from typing import List, Any, Dict, Optional

import httpx

_transport: Optional[httpx.AsyncBaseTransport] = None


def set_transport(t: Optional[httpx.AsyncBaseTransport]) -> None:
    global _transport
    _transport = t


def clean_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    p = cfg.get("provider")
    if p in (None, "", "none"):
        return {}
    if p == "github":
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", str(cfg.get("repo", ""))):
            raise ValueError("GitHub repository must look like owner/name.")
        return {k: cfg[k] for k in ("provider", "repo", "token", "labels") if cfg.get(k) is not None}
    if p == "jira":
        u = urllib.parse.urlsplit(str(cfg.get("base_url", "")))
        if u.scheme != "https" or not (u.hostname or "").endswith(".atlassian.net"):
            raise ValueError("Jira address must be your Jira Cloud site, like https://acme.atlassian.net")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,9}", str(cfg.get("project", ""))):
            raise ValueError("Jira project key looks like WEB or SEO.")
        if not re.fullmatch(r"[^@\s]+@[^@\s]+", str(cfg.get("email", ""))):
            raise ValueError("Jira needs the e-mail of the account that owns the API token.")
        return {"provider": "jira", "base_url": f"https://{u.hostname}", "project": cfg["project"], "email": cfg["email"],
                **({"token": cfg["token"]} if cfg.get("token") else {})}
    raise ValueError("Tickets go to GitHub or Jira.")


def body_text(fix: Dict[str, Any], site: Dict[str, Any], pages: list, link: str) -> str:
    t = fix.get("texts") or {}
    lines = [f"**What's wrong**\n{t.get('title', fix['title'])}. {fix.get('pages_affected', 0)} page(s) affected.",
             f"**Why it matters**\n{t.get('why', '')}", f"**How to fix**\n{t.get('fix', '')}",
             f"**How we'll know**\n{t.get('verify', '')}"]
    if pages:
        lines.append("**Example pages**\n" + "\n".join(f"- {p}" for p in pages[:10]))
    lines.append(f"Found by QC on {site['name']}. Open the fix: {link}")
    return "\n\n".join(lines)


async def _client() -> httpx.AsyncClient:
    kw: Dict[str, Any] = {"timeout": 20, "follow_redirects": False}
    if _transport is not None:
        kw["transport"] = _transport
    return httpx.AsyncClient(**kw)


async def create(cfg: Dict[str, Any], fix: Dict[str, Any], site: Dict[str, Any], pages: list, link: str) -> Dict[str, str]:
    cfg = clean_config(cfg)
    if not cfg:
        raise ValueError("Tickets aren't set up for this site (a GitHub repository or Jira project and its token on the server).")
    title = f"[QC] {fix['title']}"[:240]
    body = body_text(fix, site, pages, link)
    async with await _client() as c:
        if cfg["provider"] == "github":
            token = os.environ.get("QC6_GITHUB_TOKEN") or cfg.get("token")
            if not token:
                raise ValueError("Add a GitHub token (QC6_GITHUB_TOKEN) with permission to create issues.")
            r = await c.post(f"https://api.github.com/repos/{cfg['repo']}/issues",
                             headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                             json={"title": title, "body": body, "labels": cfg.get("labels") or ["qc", f"severity:{fix['severity']}"]})
            if r.status_code not in (200, 201):
                raise ValueError(f"GitHub refused the issue (HTTP {r.status_code}). Check the token's access to {cfg['repo']}.")
            d = r.json()
            return {"url": d["html_url"], "key": f"#{d['number']}"}
        token = os.environ.get("QC6_JIRA_TOKEN") or cfg.get("token")
        if not token:
            raise ValueError("Add a Jira API token (QC6_JIRA_TOKEN).")
        auth = base64.b64encode(f"{cfg['email']}:{token}".encode()).decode()
        r = await c.post(f"{cfg['base_url']}/rest/api/2/issue", headers={"Authorization": f"Basic {auth}"},
                         json={"fields": {"project": {"key": cfg["project"]}, "summary": title, "description": body.replace("**", "*"),
                                          "issuetype": {"name": "Task"}, "labels": ["qc"]}})
        if r.status_code not in (200, 201):
            raise ValueError(f"Jira refused the ticket (HTTP {r.status_code}). Check the project key and token.")
        key = r.json()["key"]
        return {"url": f"{cfg['base_url']}/browse/{key}", "key": key}


async def state(cfg: Dict[str, Any], key: str, client: Optional[httpx.AsyncClient] = None) -> Optional[str]:
    """'open' or 'done' for a linked ticket, None if it can't be read (network error, 404, no access)."""
    cfg = clean_config(cfg)
    if not cfg:
        return None
    if client is None:
        async with await _client() as c:
            return await state(cfg, key, c)
    for attempt in range(2):                          # one retry for a network blip or a 5xx / 429
        try:
            if cfg["provider"] == "github":
                token = os.environ.get("QC6_GITHUB_TOKEN") or cfg.get("token")
                r = await client.get(f"https://api.github.com/repos/{cfg['repo']}/issues/{key.lstrip('#')}",
                                     headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
            else:
                token = os.environ.get("QC6_JIRA_TOKEN") or cfg.get("token")
                auth = base64.b64encode(f"{cfg['email']}:{token}".encode()).decode()
                r = await client.get(f"{cfg['base_url']}/rest/api/2/issue/{key}?fields=status",
                                     headers={"Authorization": f"Basic {auth}"})
        except httpx.HTTPError:
            r = None
        if r is not None and r.status_code < 500 and r.status_code != 429:
            break
        if attempt == 0:
            await asyncio.sleep(0.5)
    if r is None or r.status_code != 200:
        return None
    if cfg["provider"] == "github":
        return "done" if r.json().get("state") == "closed" else "open"
    cat = ((r.json().get("fields") or {}).get("status") or {}).get("statusCategory", {}).get("key")
    return "done" if cat == "done" else "open"


SYNC_CONCURRENCY = 8


async def states(cfg: Dict[str, Any], keys: List[str]) -> Dict[str, Optional[str]]:
    """State of many tickets: each key read once, a few at a time over one connection pool (was: one by one, a new
    connection each, and the first network error stopped the whole sync)."""
    uniq = list(dict.fromkeys(k for k in keys if k))
    out: Dict[str, Optional[str]] = {}
    if not uniq or not clean_config(cfg):
        return {k: None for k in uniq}
    sem = asyncio.Semaphore(SYNC_CONCURRENCY)
    async with await _client() as c:
        async def one(k: str) -> None:
            async with sem:
                out[k] = await state(cfg, k, c)
        await asyncio.gather(*(one(k) for k in uniq))
    return out
