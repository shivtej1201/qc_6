"""Speed presets and per-site crawl settings carried over from QC V5.

The presets and the request model are V5's (qcv5/services/onpage_service.py), measured on the V5 benchmark against a
simulated Akamai edge. They are starting points: the live CDN decides what rate it really sustains, and QC never tries
to get round a limit (it slows down and tells you).
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

# V5 request model (10K benchmark): primary requests per page, fixed overhead, report packaging time
PRIMARY_REQ_PER_PAGE = 1.0732
FIXED_OVERHEAD_SEC = 20.0
PACKAGE_SEC_PER_PAGE = 0.0019

PRESETS: Dict[str, Dict[str, Any]] = {
    "conservative": {
        "label": "Careful", "description": "8 requests a second. The safest choice for a CDN you haven't measured.",
        "target_rps": 8.0, "max_rps": 10.0, "source_concurrency": 10, "dest_concurrency": 20, "image_concurrency": 10,
    },
    "target": {
        "label": "Standard", "description": "12 requests a second: 10,000 pages in about 16 minutes, if the CDN allows 12.",
        "target_rps": 12.0, "max_rps": 12.0, "source_concurrency": 20, "dest_concurrency": 20, "image_concurrency": 30,
    },
    "max_tested": {
        "label": "Fast", "description": "14 requests a second: the fastest rate that stayed stable on the V5 test edge.",
        "target_rps": 14.0, "max_rps": 14.0, "source_concurrency": 20, "dest_concurrency": 20, "image_concurrency": 30,
    },
    "enterprise_150k": {
        "label": "Large site", "description": "16 requests a second, rising to 18 when the CDN keeps up: about 150,000 pages "
                       "in 3 hours. Measured on a simulated edge only; agree the rate with your CDN team first.",
        "target_rps": 16.0, "max_rps": 18.0, "source_concurrency": 35, "dest_concurrency": 35, "image_concurrency": 50,
    },
}
DEFAULT_PRESET = "target"
BIG_LIST = 50_000            # from this many pages the crawl starts at the Large-site speed unless another is picked


def site_rate(site_settings: Dict[str, Any]) -> Optional[float]:
    """A speed someone set for this site (typed in, or brought from V5): a hard limit, unlike an untouched preset."""
    st = site_settings or {}
    p = PRESETS.get(st.get("preset") or DEFAULT_PRESET, PRESETS[DEFAULT_PRESET])
    r = st.get("rate_rps")
    return float(r) if r and float(r) != p["target_rps"] else None


def speeds(pages: int, site_settings: Dict[str, Any]) -> Dict[str, Any]:
    """The speeds offered when crawling starts, each with how long this many pages would take, and the one picked first."""
    out = [{"id": k, "label": v["label"], "rps": v["target_rps"], "max_rps": v["max_rps"],
            "minutes": estimate(pages, v["target_rps"])["minutes"]} for k, v in PRESETS.items()]
    own = site_rate(site_settings)
    if own:
        out.insert(0, {"id": "site", "label": "This site's limit", "rps": own, "max_rps": own, "minutes": estimate(pages, own)["minutes"]})
    return {"speeds": out, "speed_default": "site" if own else preset_for(pages)}


def preset_for(pages: int) -> str:
    """The starting speed for a list of this size: Standard keeps 10,000 pages within about 16 minutes; a big list
    gets the Large-site rate (16, rising to 18 while the site keeps up), still slowing down whenever the site pushes back."""
    return "enterprise_150k" if int(pages or 0) >= BIG_LIST else DEFAULT_PRESET

# per-site crawl settings (V5 config names in brackets) with their allowed range
LIMITS = {
    "max_pages": (1, 1_000_000),       # MAX_URLS_PER_JOB
    "read_timeout": (2.0, 120.0),      # READ_TIMEOUT
    "max_retries": (0, 5),             # MAX_RETRIES
}


def estimate(pages: int, rps: float) -> Dict[str, Any]:
    """Expected wall-clock for the page check at a given rate (V5's measured request model)."""
    pages, rps = max(0, int(pages)), max(0.1, float(rps))
    seconds = pages * PRIMARY_REQ_PER_PAGE / rps + FIXED_OVERHEAD_SEC + pages * PACKAGE_SEC_PER_PAGE
    return {"pages": pages, "rps": rps, "seconds": round(seconds), "minutes": round(seconds / 60, 1),
            "basis": "V5's measured request model; a slower CDN can only make it take longer"}


def engine_options(site_settings: Dict[str, Any], opts: Dict[str, Any]) -> Dict[str, Any]:
    """Engine settings for one page check: run options win, then the site's settings, then its preset."""
    st = site_settings or {}
    p = PRESETS.get(opts.get("preset") or st.get("preset") or DEFAULT_PRESET, PRESETS[DEFAULT_PRESET])
    custom = opts.get("rate_rps") or st.get("rate_rps")
    rate = float(custom or p["target_rps"])
    # a speed someone typed in is a hard limit; only an untouched preset may rise to the preset's ceiling
    ceiling = float(p["max_rps"]) if not custom or float(custom) == p["target_rps"] else rate
    out = {
        "target_rps": rate,
        "max_rps": max(rate, ceiling),
        "source_concurrency": int(opts.get("source_concurrency") or p["source_concurrency"]),
        "dest_concurrency": int(opts.get("dest_concurrency") or p["dest_concurrency"]),
        "image_concurrency": int(opts.get("image_concurrency") or p["image_concurrency"]),
    }
    for k in ("read_timeout", "max_retries"):
        v = opts.get(k, st.get(k))
        if v is not None:
            lo, hi = LIMITS[k]
            out[k] = type(lo)(min(hi, max(lo, float(v))))
    return out


def max_pages(site_settings: Dict[str, Any], opts: Dict[str, Any], default: int = 300_000) -> int:
    v = opts.get("max_pages") or (site_settings or {}).get("max_pages") or default
    return int(min(LIMITS["max_pages"][1], max(1, int(v))))


# ---------------------------------------------------------------- V5 .env import
V5_KEYS = {
    "MAX_URLS_PER_JOB": ("max_pages", int),
    "READ_TIMEOUT": ("read_timeout", float),
    "MAX_RETRIES": ("max_retries", int),
    "REQUESTS_PER_SECOND": ("rate_rps", float),
}


def parse_v5_env(text: str) -> Dict[str, Any]:
    """Read the settings V6 understands from a V5 .env file. Unknown keys are listed, never guessed at."""
    found: Dict[str, Any] = {}
    ignored = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip().removeprefix("export ").strip(), v.strip().strip('"').strip("'")
        if k in V5_KEYS:
            name, cast = V5_KEYS[k]
            try:
                val = cast(v)
            except ValueError:
                ignored.append(f"{k} (not a number: {v!r})")
                continue
            if name == "rate_rps":
                val = min(200.0, max(0.5, val))   # V5's 100 req/s default was an HTTP-pool limit, not a CDN-safe rate
            elif name in LIMITS:
                lo, hi = LIMITS[name]
                val = type(lo)(min(hi, max(lo, val)))
            found[name] = val
        elif re.fullmatch(r"[A-Z][A-Z0-9_]+", k):
            ignored.append(k)
    return {"settings": found, "ignored": ignored}


def page_type_label(page_type: Optional[str], business: Optional[str]) -> Optional[str]:
    """The site's own name for a page type (V5 reads it from data-pageType, e.g. 'b2c-pdp' -> 'B2C PDP')."""
    pt = (page_type or "").strip()
    if not pt or pt.upper() == "NEEDS_REVIEW" or pt.lower().startswith(("ambiguous", "html_data")):
        return None
    b = (business or "").strip()
    return f"{b} {pt}".strip() if b and b.upper() != "NEEDS_REVIEW" else pt
