"""Which URLs found on a page belong in the SEO URL / image report, and why the others don't.

Every URL the parser finds goes through `UrlRules.classify` before anything is recorded: it is either reportable
(validated and reported) or excluded with a reason (JS, CSS, Font, Preload / resource hint, Google Tag Manager,
Analytics, Ads tracking, CleverTap, Tracking pixel, Monitoring, Integration resource, Invalid URL). Excluded URLs are
kept in their own table (on_page_excluded_urls) with the page and the reason, so for every page

    URLs found = URLs reported + URLs excluded

and nothing disappears silently. The rules are data, not code: defaults below, extended or overridden per site
(settings.url_rules) or with a JSON file (QC6_URL_RULES):

    {"exclude_hosts": {"track.example.com": "Analytics"},   # host or any subdomain of it
     "exclude_url_contains": {"/pixel?": "Tracking pixel"},
     "include_hosts": ["fonts.example.com"],                 # never excluded by host
     "exclude_extensions": {"pdf": "Document"},              # add; "keep_extensions": ["js"] removes
     "include_technical_resources": false}                   # true: JS / CSS / fonts are reported (technical audit)
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
from typing import Any, Dict, Iterable, Optional, Tuple

TYPE_REASONS: Dict[str, str] = {
    "SCRIPT": "JS",
    "STYLESHEET": "CSS",
    "CSS_RESOURCE": "CSS",
    "LINK_RESOURCE": "Preload / resource hint",
}
TECHNICAL = {"JS", "CSS", "Font", "Preload / resource hint", "Source map"}

EXTENSIONS: Dict[str, str] = {
    "js": "JS", "mjs": "JS", "cjs": "JS", "jsx": "JS", "ts": "JS", "map": "Source map",
    "css": "CSS", "woff": "Font", "woff2": "Font", "ttf": "Font", "otf": "Font", "eot": "Font",
}

# host (or any subdomain of it) -> reason
HOSTS: Dict[str, str] = {
    "googletagmanager.com": "Google Tag Manager",
    "tagmanager.google.com": "Google Tag Manager",
    "google-analytics.com": "Analytics",
    "analytics.google.com": "Analytics",
    "ssl.google-analytics.com": "Analytics",
    "stats.g.doubleclick.net": "Analytics",
    "googleoptimize.com": "Analytics",
    "doubleclick.net": "Ads tracking",
    "googleadservices.com": "Ads tracking",
    "googlesyndication.com": "Ads tracking",
    "adservice.google.com": "Ads tracking",
    "clevertap.com": "CleverTap",
    "clevertap-prod.com": "CleverTap",
    "wzrkt.com": "CleverTap",
    "connect.facebook.net": "Tracking pixel",
    "bat.bing.com": "Tracking pixel",
    "snap.licdn.com": "Tracking pixel",
    "px.ads.linkedin.com": "Tracking pixel",
    "analytics.tiktok.com": "Tracking pixel",
    "tr.snapchat.com": "Tracking pixel",
    "sc-static.net": "Tracking pixel",
    "ct.pinterest.com": "Tracking pixel",
    "clarity.ms": "Analytics",
    "hotjar.com": "Analytics",
    "hotjar.io": "Analytics",
    "mixpanel.com": "Analytics",
    "amplitude.com": "Analytics",
    "cdn.segment.com": "Analytics",
    "api.segment.io": "Analytics",
    "nr-data.net": "Monitoring",
    "js-agent.newrelic.com": "Monitoring",
    "browser.sentry-cdn.com": "Monitoring",
    "ingest.sentry.io": "Monitoring",
    "webengage.com": "Integration resource",
    "webengage.co": "Integration resource",
    "moengage.com": "Integration resource",
    "cdn.onesignal.com": "Integration resource",
}
# substring of the URL (after the host is lower-cased) -> reason
URL_CONTAINS: Dict[str, str] = {
    "facebook.com/tr": "Tracking pixel",
    "google.com/ads/ga-audiences": "Ads tracking",
    "google.com/pagead/": "Ads tracking",
}


def _host(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


class UrlRules:
    def __init__(self, extra: Optional[Dict[str, Any]] = None):
        extra = dict(extra or {})
        self.hosts = {**HOSTS, **{str(k).lower().lstrip("."): str(v) for k, v in (extra.get("exclude_hosts") or {}).items()}}
        self.include_hosts = {str(h).lower().lstrip(".") for h in (extra.get("include_hosts") or [])}
        self.contains = {**URL_CONTAINS, **{str(k).lower(): str(v) for k, v in (extra.get("exclude_url_contains") or {}).items()}}
        self.ext = {**EXTENSIONS, **{str(k).lower().lstrip("."): str(v) for k, v in (extra.get("exclude_extensions") or {}).items()}}
        for k in extra.get("keep_extensions") or []:
            self.ext.pop(str(k).lower().lstrip("."), None)
        self.technical = bool(extra.get("include_technical_resources"))
        self._host_cache: Dict[str, Optional[str]] = {}

    @classmethod
    def load(cls, site_rules: Optional[Dict[str, Any]] = None) -> "UrlRules":
        """Defaults + QC6_URL_RULES (a JSON file) + the site's own rules (later wins)."""
        merged: Dict[str, Any] = {}
        path = os.environ.get("QC6_URL_RULES")
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                merged = _merge(merged, json.load(f))
        if site_rules:
            merged = _merge(merged, site_rules)
        return cls(merged)

    def _host_reason(self, host: str) -> Optional[str]:
        if host in self._host_cache:
            return self._host_cache[host]
        reason = None
        if host and not any(host == h or host.endswith("." + h) for h in self.include_hosts):
            parts = host.split(".")
            for i in range(len(parts) - 1):
                r = self.hosts.get(".".join(parts[i:]))
                if r:
                    reason = r
                    break
        if len(self._host_cache) < 50_000:
            self._host_cache[host] = reason
        return reason

    def classify(self, href: str, url_type: str, resolved: str = "") -> Optional[str]:
        """None = reportable; otherwise the exclusion reason. `resolved` is the absolute URL when known."""
        if not self.technical and url_type in TYPE_REASONS:
            return TYPE_REASONS[url_type]
        m = re.search(r"\.([A-Za-z0-9]{1,6})$", _path(resolved or href))
        if m:
            r = self.ext.get(m.group(1).lower())
            if r and not (self.technical and r in TECHNICAL):
                return r
        target = resolved or href
        if "://" in target or target.startswith("//"):
            host = _host(target if "://" in target else "https:" + target)
            r = self._host_reason(host)
            if r:
                return r
            low = target.lower()
            for k, v in self.contains.items():
                if k in low:
                    return v
        return None


def _path(href: str) -> str:
    h = href.strip()
    if "://" in h or h.startswith("//") or h.startswith("/"):
        try:
            return urllib.parse.urlsplit(h if not h.startswith("//") else "https:" + h).path or ""
        except ValueError:
            return h.split("?", 1)[0].split("#", 1)[0]
    return h.split("?", 1)[0].split("#", 1)[0]


def _merge(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(a)
    for k, v in (b or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        elif isinstance(v, list) and isinstance(out.get(k), list):
            out[k] = list(dict.fromkeys(out[k] + v))
        else:
            out[k] = v
    return out


DEFAULT = UrlRules()


def summary(rows: Iterable[Tuple[str, int]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for reason, n in rows:
        out[reason] = out.get(reason, 0) + int(n or 0)
    return out
