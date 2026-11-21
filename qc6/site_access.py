"""What QC sends to a site, and to whom (FEAT-02-02 crawl scope, FEAT-02-03 staging access).

Two kinds of request headers:

* **Public identity** goes everywhere: a real browser User-Agent with "QC-Audit/6.0" appended. QC never hides what it is.
* **Site-private headers** go ONLY to the site's own hosts (the primary host, its parent domain and subdomains): the
  site's X-QC-Audit allow-token, and for a pre-production site the Basic-auth login and any extra headers the site
  owner gave. They are never sent to third parties (analytics, CDNs of other companies, sites the pages link to), so a
  linked site can't pick up the token or a staging password.

Staging credentials are stored encrypted (Fernet, key from QC6_SECRET_KEY) inside the site's settings and are never
returned by the API; the API only says whether they're set and which header names are in use.

Crawl scope: include / exclude patterns per site, matched against the URL path and query ("/blog/*", "*?sort=*").
"""
from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import os
import re
import urllib.parse
from typing import Any, Dict, Iterable, List, Optional, Tuple

from qc6.settings import settings

# headers a site owner may not set: they would change who QC says it is, or break the HTTP exchange
FORBIDDEN_HEADERS = {"user-agent", "host", "content-length", "connection", "transfer-encoding", "cookie2", "x-qc-audit",
                     "authorization", "proxy-authorization", "te", "upgrade", "keep-alive", "expect"}
HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")


class AccessError(ValueError):
    pass


# ------------------------------------------------------------------ hosts
def is_site_host(host: str, primary: str) -> bool:
    """The site's own hosts: the primary host, its parent domain (www. dropped) and that domain's subdomains."""
    host, primary = (host or "").lower().strip("[]"), (primary or "").lower()
    if not host or not primary:
        return False
    base = primary[4:] if primary.startswith("www.") else primary
    return host == primary or host == base or host.endswith("." + base)


def is_site_url(url: str, primary: str) -> bool:
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return u.scheme in ("http", "https") and is_site_host(u.hostname or "", primary)


# ------------------------------------------------------------------ encryption
def _fernet():
    from cryptography.fernet import Fernet
    key = os.environ.get("QC6_SECRET_KEY", "")
    if not key:
        raise AccessError("Set QC6_SECRET_KEY on the server first (any long random text); QC encrypts staging logins with it.")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()))


def seal(data: Dict[str, Any]) -> str:
    return _fernet().encrypt(json.dumps(data).encode()).decode()


def unseal(token: str) -> Dict[str, Any]:
    from cryptography.fernet import InvalidToken
    try:
        return json.loads(_fernet().decrypt(token.encode()))
    except InvalidToken:
        raise AccessError("The staging login can't be read: QC6_SECRET_KEY changed since it was saved. Enter it again.")


# ------------------------------------------------------------------ staging access
def clean_staging(given: Dict[str, Any], current: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Validate what the Settings form sent and return the value to store under settings['staging'].
    given: {"username", "password", "headers": {name: value}} — empty strings mean 'keep', clear=True removes all."""
    if given.get("clear"):
        return None
    old = unseal(current["sealed"]) if current and current.get("sealed") else {}
    user = given.get("username")
    pw = given.get("password")
    new = {"username": old.get("username", ""), "password": old.get("password", ""), "headers": dict(old.get("headers") or {})}
    if user is not None:
        new["username"] = str(user)[:200]
    if pw:                                           # empty = keep the stored password (the form never shows it)
        new["password"] = str(pw)[:500]
    if ":" in new["username"]:
        raise AccessError("A Basic-auth user name can't contain ':'.")
    for name, value in (given.get("headers") or {}).items():
        if not HEADER_NAME.match(name or "") or name.lower() in FORBIDDEN_HEADERS:
            raise AccessError(f"QC can't send a header called {name!r}. Use the login fields for Authorization; "
                              "User-Agent and X-QC-Audit are set by QC.")
        if value is None or value == "":
            new["headers"].pop(name, None)
        elif value != "•••":                         # the form shows stored values masked; unchanged = keep
            if "\n" in str(value) or "\r" in str(value) or len(str(value)) > 2000:
                raise AccessError(f"The value for {name} must be one line, at most 2,000 characters.")
            new["headers"][name] = str(value)
    if len(new["headers"]) > 10:
        raise AccessError("At most 10 extra headers.")
    if not new["username"] and not new["password"] and not new["headers"]:
        return None
    return {"sealed": seal(new), "username": new["username"], "header_names": sorted(new["headers"]),
            "has_password": bool(new["password"])}


def public_staging(st: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not st:
        return None
    return {"username": st.get("username", ""), "has_password": st.get("has_password", False),
            "header_names": st.get("header_names", [])}


def private_headers(site: Dict[str, Any]) -> Dict[str, str]:
    """Headers for requests to the site's own hosts only."""
    out = {settings.access_header: site["access_token"]}
    st = (site.get("settings") or {}).get("staging")
    if st and st.get("sealed"):
        s = unseal(st["sealed"])
        out.update(s.get("headers") or {})
        if s.get("username") or s.get("password"):
            out["Authorization"] = "Basic " + base64.b64encode(f"{s.get('username', '')}:{s.get('password', '')}".encode()).decode()
    return out


def httpx_hook(site: Dict[str, Any]):
    """httpx request hook: add the site-private headers only when the request goes to one of the site's hosts
    (also for each redirect hop, which httpx sends as a new request)."""
    headers = private_headers(site)
    primary = site.get("primary_host") or urllib.parse.urlsplit(site.get("base_url", "")).hostname or ""

    async def hook(request):
        if is_site_host(request.url.host, primary):
            for k, v in headers.items():
                request.headers[k] = v
        else:
            for k in headers:
                request.headers.pop(k, None)
    return hook


# ------------------------------------------------------------------ crawl scope
def clean_scope(scope: Dict[str, Any]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for k in ("include", "exclude"):
        pats = [str(p).strip() for p in (scope.get(k) or []) if str(p).strip()]
        if len(pats) > 50:
            raise AccessError(f"At most 50 {k} patterns.")
        for p in pats:
            if len(p) > 300 or not (p.startswith("/") or p.startswith("*")):
                raise AccessError(f"Patterns start with / or *, for example /blog/* or *?sort=*  (got {p!r}).")
        out[k] = pats
    return out


def _target(url: str) -> str:
    u = urllib.parse.urlsplit(url)
    return (u.path or "/") + (f"?{u.query}" if u.query else "")


def in_scope(url: str, scope: Optional[Dict[str, Any]]) -> bool:
    if not scope:
        return True
    t = _target(url)
    inc, exc = scope.get("include") or [], scope.get("exclude") or []
    if inc and not any(fnmatch.fnmatchcase(t, p) for p in inc):
        return False
    return not any(fnmatch.fnmatchcase(t, p) for p in exc)


def apply_scope(urls: Iterable[str], scope: Optional[Dict[str, Any]]) -> Tuple[List[str], int]:
    kept, dropped = [], 0
    for u in urls:
        if in_scope(u, scope):
            kept.append(u)
        else:
            dropped += 1
    return kept, dropped
