"""Product pages (CrawlPulse FEAT-08-02): is the product's data there for search engines and shopping results?

Read from the page's own structured data, in the crawl's existing parse pass (no extra request):
JSON-LD (any block; @graph; @type as a list) and, failing that, schema.org microdata.

Checked on product pages: name, price (a number above 0), currency (3 letters), availability (a schema.org value),
an identifier (sku, gtin8/12/13/14 or mpn), an image. "Product pages" are pages with Product data, plus pages without
it whose page type mostly has it, or that the site labels PDP (data-pageType).
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

_LD = re.compile(r"<script\b[^>]*type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script\s*>", re.S | re.I)
_MICRO = re.compile(r"itemtype\s*=\s*[\"']https?://schema\.org/Product[\"']", re.I)
_TAG = re.compile(r"<[a-z][^>]*\bitemprop\s*=\s*[\"'](name|price|priceCurrency|availability|sku|gtin\d*|mpn|image)[\"'][^>]*>([^<]{0,200})", re.I)
_ATTR = re.compile(r"\b(content|href|src)\s*=\s*[\"']([^\"']*)[\"']", re.I)
AVAILABILITY = {"instock", "outofstock", "preorder", "backorder", "discontinued", "instoreonly", "limitedavailability",
                "onlineonly", "presale", "soldout", "reserved", "madetoorder"}
FIELDS = ["name", "price", "currency", "availability", "identifier", "image"]


def _types(node: Dict[str, Any]) -> List[str]:
    t = node.get("@type")
    return [str(x).lower() for x in (t if isinstance(t, list) else [t]) if x]


def _walk(obj: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if isinstance(obj, list):
        for x in obj:
            out += _walk(x)
    elif isinstance(obj, dict):
        if "product" in _types(obj) or "productgroup" in _types(obj):
            out.append(obj)
        for k in ("@graph", "mainEntity", "itemListElement", "hasVariant"):
            if k in obj:
                out += _walk(obj[k])
    return out


def _first_offer(p: Dict[str, Any]) -> Dict[str, Any]:
    o = p.get("offers")
    if isinstance(o, list):
        o = o[0] if o else {}
    if isinstance(o, dict) and "aggregateoffer" in _types(o):
        return {"price": o.get("lowPrice") or o.get("price"), "priceCurrency": o.get("priceCurrency"),
                "availability": o.get("availability"), **({} if not isinstance(o.get("offers"), list) or not o["offers"] else o["offers"][0])}
    return o if isinstance(o, dict) else {}


def _price_ok(v: Any) -> bool:
    try:
        return float(str(v).replace(",", "").strip()) > 0
    except (TypeError, ValueError):
        return False


def extract(html: str, limit: int = 600_000) -> Optional[Dict[str, Any]]:
    """{"source": "json-ld"|"microdata", "name": .., "price": .., ...} with what is present, or None when no Product."""
    h = (html or "")[:limit]
    products: List[Dict[str, Any]] = []
    for block in _LD.findall(h):
        try:
            products += _walk(json.loads(block.strip()))
        except (ValueError, RecursionError):
            continue
    if products:
        p = products[0]
        o = _first_offer(p)
        avail = str(o.get("availability") or "").rsplit("/", 1)[-1].lower()
        ident = next((str(p.get(k)) for k in ("sku", "gtin", "gtin13", "gtin14", "gtin12", "gtin8", "mpn") if p.get(k)), "") or \
            next((str(o.get(k)) for k in ("sku", "gtin13", "gtin", "mpn") if o.get(k)), "")
        img = p.get("image")
        return {"source": "json-ld", "name": bool(str(p.get("name") or "").strip()), "price": _price_ok(o.get("price")),
                "currency": bool(re.fullmatch(r"[A-Za-z]{3}", str(o.get("priceCurrency") or ""))),
                "availability": avail in AVAILABILITY, "identifier": bool(ident.strip()),
                "image": bool(img if not isinstance(img, list) else any(img)), "value_price": str(o.get("price") or "")[:40]}
    if _MICRO.search(h):
        props: Dict[str, str] = {}
        for m in _TAG.finditer(h):
            attrs = {a.lower(): v for a, v in _ATTR.findall(m.group(0))}
            value = attrs.get("content") or attrs.get("href") or attrs.get("src") or m.group(2).strip()
            props.setdefault(m.group(1).lower(), value or "")
        avail = props.get("availability", "").rsplit("/", 1)[-1].lower()
        return {"source": "microdata", "price": _price_ok(props.get("price")),
                "currency": bool(re.fullmatch(r"[A-Za-z]{3}", props.get("pricecurrency", ""))),
                "availability": avail in AVAILABILITY,
                "identifier": any(props.get(k) for k in props if k in ("sku", "mpn") or k.startswith("gtin")),
                "image": bool(props.get("image")), "name": bool(props.get("name")), "value_price": props.get("price", "")[:40]}
    return None


def problems(pages: List[Dict[str, Any]]) -> List[tuple]:
    """pages: url, template_id, product (extract() or None), site_type. Returns (url, rule, fields) findings."""
    by_tpl: Dict[Any, List[Dict[str, Any]]] = {}
    for p in pages:
        by_tpl.setdefault(p.get("template_id"), []).append(p)
    out: List[tuple] = []
    for tpl, ps in by_tpl.items():
        with_data = sum(1 for p in ps if p.get("product"))
        mostly = tpl is not None and len(ps) >= 2 and with_data * 2 >= len(ps)
        for p in ps:
            pr = p.get("product")
            label = str(p.get("site_type") or "").upper()
            if pr is None:
                if (mostly or "PDP" in label.split()) and p.get("http_status") and 200 <= p["http_status"] < 300:
                    out.append((p["url"], "PRODUCT_DATA_MISSING", {}))
                continue
            for f in FIELDS:
                if not pr.get(f):
                    out.append((p["url"], f"PRODUCT_{f.upper()}_MISSING", {"value": pr.get("value_price") or ""}))
    return out
