"""Structured data and EMI checks on every crawled page (run in the crawl's parse step, on the HTML already fetched).

Schema (JSON-LD, incl. @graph; microdata for Product): which types are present, required fields present / missing,
invalid values, per type:
  Product           name, image, description, sku (or gtin / mpn), offers
  Offer             price (a number above 0), priceCurrency (3 letters), availability (a schema.org value)
  AggregateRating   ratingValue (number, within worst..best), ratingCount or reviewCount (> 0)
  BreadcrumbList    itemListElement: ListItem with position 1..n in order, name, item (URL; may be left out on the last)
  Organization      name, url, logo
  WebPage           name or url
  FAQPage           mainEntity: Question with name and acceptedAnswer.text

EMI: looked for in every supported structure, so EMI shown on the page is never reported missing:
  structured data    any JSON-LD key / value naming EMI (e.g. a PriceSpecification named "EMI", "emi": ...)
  embedded state     JSON in <script> (e.g. __NEXT_DATA__, window.__INITIAL_STATE__) with an emi... key and an amount
  visible text       "EMI starts at ₹1,234", "EMI ₹999/month", "No Cost EMI", "₹1,234/month"
  microdata          itemprop names containing emi
Status: present (with where and the amount), invalid (EMI mentioned but no usable amount, or an amount <= 0 or
above the price), missing (the page sells something with a price, or matches the site's emi.required_paths, and
no EMI anywhere), not applicable (nothing to pay for). Site settings `emi`: {"required_paths": ["/products/*"],
"min_price": 3000, "check": true}.
"""
from __future__ import annotations

import fnmatch
import html as htmlmod
import json
import re
import urllib.parse
from typing import Any, Dict, Iterable, List, Optional, Tuple

AVAILABILITY = {"instock", "outofstock", "preorder", "backorder", "discontinued", "instoreonly", "limitedavailability",
                "onlineonly", "soldout", "presale", "reserved", "madetoorder"}
_LD = re.compile(r"<script[^>]+type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.S | re.I)
_SCRIPT = re.compile(r"<script(?![^>]+ld\+json)[^>]*>(.*?)</script>", re.S | re.I)
_TAGS = re.compile(r"<(script|style|noscript|template)[^>]*>.*?</\1>|<[^>]+>", re.S | re.I)
_EMI_KEY = re.compile(r"[\"'](?:[A-Za-z_]*emi[A-Za-z_]*|monthly_?payment|monthlyInstal+ment)[\"']\s*:\s*\"?(?:₹|Rs\.?|INR)?\s*([\d,]+(?:\.\d+)?)", re.I)
_EMI_TEXT = re.compile(
    r"(?:\bEMI\b[^₹\d<]{0,40}(?:₹|Rs\.?|INR)\s*([\d,]+(?:\.\d+)?)|(?:₹|Rs\.?|INR)\s*([\d,]+(?:\.\d+)?)\s*(?:/|per)\s*(?:mo\b|month|mon\b)"
    r"|(?:₹|Rs\.?|INR)\s*([\d,]+(?:\.\d+)?)[^<\n]{0,30}\bEMI\b)", re.I)
_EMI_WORD = re.compile(r"\b(?:no[\s-]*cost\s+)?EMI\b", re.I)
_MICRO = re.compile(r"<[a-z][^>]*\bitemprop\s*=\s*[\"']([^\"']*emi[^\"']*)[\"'][^>]*>", re.I)
_CONTENT = re.compile(r"\bcontent\s*=\s*[\"']([^\"']*)[\"']", re.I)
_MICRO_PRODUCT = re.compile(r"itemtype\s*=\s*[\"']https?://schema\.org/Product[\"']", re.I)
_ITEMPROP = re.compile(r"<[a-z][^>]*\bitemprop\s*=\s*[\"'](name|price|priceCurrency|availability|sku|gtin\d*|mpn|image|description)[\"'][^>]*>", re.I)


def _num(v: Any) -> Optional[float]:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        if re.fullmatch(r"-?\d+(?:\.\d+)?", s):
            return float(s)
    return None


def _types(node: Dict[str, Any]) -> List[str]:
    t = node.get("@type")
    out = t if isinstance(t, list) else [t]
    return [str(x).rsplit("/", 1)[-1] for x in out if x]


def _walk(obj: Any, out: List[Dict[str, Any]], depth: int = 0) -> None:
    if depth > 12:
        return
    if isinstance(obj, dict):
        if obj.get("@type"):
            out.append(obj)
        for v in obj.values():
            if isinstance(v, (dict, list)):
                _walk(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:500]:
            _walk(v, out, depth + 1)


def _present(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, (list, dict)):
        return bool(v)
    return True


def _url_ok(v: Any) -> bool:
    if isinstance(v, dict):
        v = v.get("@id") or v.get("url")
    return isinstance(v, str) and v.strip().lower().startswith(("http://", "https://", "/"))


class _Report:
    def __init__(self):
        self.problems: List[Tuple[str, str, str, str]] = []      # (type, field, kind missing|invalid, detail)

    def missing(self, typ: str, field: str) -> None:
        self.problems.append((typ, field, "missing", ""))

    def invalid(self, typ: str, field: str, value: Any, why: str) -> None:
        self.problems.append((typ, field, "invalid", f"{str(value)[:60]!r}: {why}"))


def _offer(r: _Report, o: Dict[str, Any]) -> Dict[str, Any]:
    price = o.get("price", o.get("lowPrice"))
    if not _present(price):
        ps = o.get("priceSpecification")
        if isinstance(ps, list):
            ps = ps[0] if ps else {}
        if isinstance(ps, dict):
            price = ps.get("price")
    cur = o.get("priceCurrency")
    if not _present(cur) and isinstance(o.get("priceSpecification"), dict):
        cur = o["priceSpecification"].get("priceCurrency")
    avail = str(o.get("availability") or "").rsplit("/", 1)[-1].lower()
    out = {"price": None, "currency": None, "availability": None}
    if not _present(price):
        r.missing("Offer", "price")
    else:
        n = _num(price)
        if n is None:
            r.invalid("Offer", "price", price, "not a plain number (no currency sign or text)")
        elif n <= 0:
            r.invalid("Offer", "price", price, "not above zero")
        else:
            out["price"] = n
    if not _present(cur):
        r.missing("Offer", "priceCurrency")
    elif not re.fullmatch(r"[A-Za-z]{3}", str(cur).strip()):
        r.invalid("Offer", "priceCurrency", cur, "not a 3-letter currency code like INR")
    else:
        out["currency"] = str(cur).upper()
    if not _present(o.get("availability")):
        r.missing("Offer", "availability")
    elif avail not in AVAILABILITY:
        r.invalid("Offer", "availability", o.get("availability"), "not a schema.org availability value")
    else:
        out["availability"] = avail
    return out


def _check_nodes(nodes: List[Dict[str, Any]], r: _Report) -> Dict[str, Any]:
    info: Dict[str, Any] = {"types": [], "price": None, "currency": None, "offer": False, "product": False}
    seen = set()
    for n in nodes:
        for t in _types(n):
            if t not in seen:
                seen.add(t)
                info["types"].append(t)
    for n in nodes:
        ts = set(_types(n))
        if ts & {"Product", "ProductGroup", "IndividualProduct", "ProductModel"}:
            info["product"] = True
            for f in ("name", "image", "description"):
                if not _present(n.get(f)):
                    r.missing("Product", f)
            if not any(_present(n.get(k)) for k in ("sku", "gtin", "gtin8", "gtin12", "gtin13", "gtin14", "mpn", "productID")):
                r.missing("Product", "sku")
            offers = n.get("offers")
            if isinstance(offers, list):
                offers = offers[0] if offers else None
            if not isinstance(offers, dict):
                if not ts & {"ProductGroup"}:
                    r.missing("Product", "offers")
            else:
                info["offer"] = True
                o = _offer(r, offers)
                info["price"] = info["price"] or o["price"]
                info["currency"] = info["currency"] or o["currency"]
            if isinstance(n.get("aggregateRating"), dict):
                _rating(r, n["aggregateRating"])
        elif ts & {"Offer", "AggregateOffer"} and not info["offer"]:
            pass                                           # standalone offers are checked with their product
        if "AggregateRating" in ts and not n.get("_checked"):
            _rating(r, n)
        if "BreadcrumbList" in ts:
            items = n.get("itemListElement")
            if not isinstance(items, list) or not items:
                r.missing("BreadcrumbList", "itemListElement")
            else:
                for i, it in enumerate(items, 1):
                    if not isinstance(it, dict):
                        r.invalid("BreadcrumbList", "itemListElement", it, "not a ListItem")
                        break
                    pos = _num(it.get("position"))
                    if pos is None:
                        r.missing("BreadcrumbList", "ListItem.position")
                    elif int(pos) != i:
                        r.invalid("BreadcrumbList", "ListItem.position", it.get("position"), f"expected {i} (positions run 1..n in order)")
                    item = it.get("item")
                    name = it.get("name") or (item.get("name") if isinstance(item, dict) else None)
                    if not _present(name):
                        r.missing("BreadcrumbList", "ListItem.name")
                    if i < len(items) and not _url_ok(item):
                        r.missing("BreadcrumbList", "ListItem.item")
        if ts & {"Organization", "Corporation", "LocalBusiness", "OnlineStore"}:
            for f in ("name", "url", "logo"):
                if not _present(n.get(f)):
                    r.missing("Organization", f)
        if ts & {"WebPage", "ItemPage", "CollectionPage", "AboutPage", "ContactPage"}:
            if not (_present(n.get("name")) or _present(n.get("url"))):
                r.missing("WebPage", "name")
        if "FAQPage" in ts:
            qs = n.get("mainEntity")
            qs = qs if isinstance(qs, list) else [qs] if isinstance(qs, dict) else []
            if not qs:
                r.missing("FAQPage", "mainEntity")
            for q in qs[:200]:
                if not isinstance(q, dict) or "Question" not in _types(q):
                    r.invalid("FAQPage", "mainEntity", q if not isinstance(q, dict) else q.get("@type"), "not a Question")
                    continue
                if not _present(q.get("name")):
                    r.missing("FAQPage", "Question.name")
                a = q.get("acceptedAnswer")
                a = a[0] if isinstance(a, list) and a else a
                if not isinstance(a, dict) or not _present(a.get("text")):
                    r.missing("FAQPage", "Question.acceptedAnswer.text")
    return info


def _rating(r: _Report, n: Dict[str, Any]) -> None:
    n["_checked"] = True
    v = _num(n.get("ratingValue"))
    if not _present(n.get("ratingValue")):
        r.missing("AggregateRating", "ratingValue")
    elif v is None:
        r.invalid("AggregateRating", "ratingValue", n.get("ratingValue"), "not a number")
    else:
        best, worst = _num(n.get("bestRating")) or 5.0, _num(n.get("worstRating")) or 1.0
        if not (min(worst, 0.0) <= v <= best):
            r.invalid("AggregateRating", "ratingValue", n.get("ratingValue"), f"outside {worst:g}..{best:g}")
    cnt = _num(n.get("ratingCount")) or _num(n.get("reviewCount"))
    if not (_present(n.get("ratingCount")) or _present(n.get("reviewCount"))):
        r.missing("AggregateRating", "ratingCount")
    elif cnt is None or cnt <= 0:
        r.invalid("AggregateRating", "ratingCount", n.get("ratingCount") or n.get("reviewCount"), "not a count above zero")


def _microdata_product(html: str, r: _Report, info: Dict[str, Any]) -> None:
    if info["product"] or not _MICRO_PRODUCT.search(html):
        return
    info["product"] = True
    info["types"].append("Product (microdata)")
    props = {m.group(1).lower() for m in _ITEMPROP.finditer(html)}
    for f in ("name", "image", "description"):
        if f not in props:
            r.missing("Product", f)
    if not props & {"sku", "mpn"} and not any(p.startswith("gtin") for p in props):
        r.missing("Product", "sku")
    if "price" not in props:
        r.missing("Offer", "price")
    else:
        info["offer"] = True
        m = re.search(r"itemprop\s*=\s*[\"']price[\"'][^>]*?content\s*=\s*[\"']([^\"']+)", html, re.I)
        n = _num(m.group(1)) if m else None
        info["price"] = n if n and n > 0 else info["price"]
    if "pricecurrency" not in props:
        r.missing("Offer", "priceCurrency")


def _emi(html: str, ld_blocks: List[Any], info: Dict[str, Any], page_url: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    found: List[Tuple[str, Optional[float]]] = []
    for blk in ld_blocks:                                          # structured data
        txt = json.dumps(blk)
        if re.search(r"\bemi\b|\"[A-Za-z_]*emi[A-Za-z_]*\"\s*:", txt, re.I):
            m = _EMI_KEY.search(txt)
            amt = _num(m.group(1)) if m else None
            if amt is None:
                for node in _iter_nodes(blk):
                    if re.search(r"\bemi\b", str(node.get("name") or ""), re.I):
                        amt = _num(node.get("price"))
                        break
            found.append(("structured data", amt))
    for m in _SCRIPT.finditer(html[:3_000_000]):                   # embedded JSON / app state
        body = m.group(1)
        if len(body) > 20 and ("emi" in body.lower()):
            k = _EMI_KEY.search(body)
            if k:
                found.append(("embedded state", _num(k.group(1))))
                break
    for m in _MICRO.finditer(html):                                # microdata
        c = _CONTENT.search(m.group(0))
        found.append(("microdata", _num(c.group(1)) if c else None))
        break
    text = htmlmod.unescape(_TAGS.sub(" ", html[:3_000_000]))      # visible text
    t = _EMI_TEXT.search(text)
    if t:
        found.append(("visible text", _num(next(g for g in t.groups() if g))))
    elif _EMI_WORD.search(text):
        found.append(("visible text", None))
    path = urllib.parse.urlsplit(page_url).path or "/"
    required = [p for p in (cfg.get("required_paths") or []) if isinstance(p, str)]
    min_price = float(cfg.get("min_price") or 0)
    by_path = bool(required) and any(fnmatch.fnmatchcase(path, p) for p in required)
    by_price = info.get("price") is not None and info["price"] >= min_price
    applicable = by_path or by_price
    amounts = [a for _, a in found if a is not None]
    sources = sorted({s for s, _ in found})
    if found:
        bad = [a for a in amounts if a <= 0 or (info.get("price") and a > info["price"])]
        if not amounts:
            return {"status": "invalid", "sources": sources, "amount": None, "why": "EMI is mentioned but no EMI amount was found"}
        if bad and len(bad) == len(amounts):
            return {"status": "invalid", "sources": sources, "amount": bad[0],
                    "why": "the EMI amount is zero or more than the price"}
        good = [a for a in amounts if a not in bad]
        return {"status": "present", "sources": sources, "amount": min(good) if good else None, "why": ""}
    if applicable:
        return {"status": "missing", "sources": [], "amount": None,
                "why": "the page matches the EMI pages setting" if by_path else "the page has a price but no EMI anywhere"}
    return {"status": "not_applicable", "sources": [], "amount": None, "why": ""}


def _iter_nodes(obj: Any) -> Iterable[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    _walk(obj, out)
    return out


def check(html: str, page_url: str = "", emi_cfg: Optional[Dict[str, Any]] = None, limit: int = 4_000_000) -> Dict[str, Any]:
    """The schema + EMI result of one page: {"types", "product", "offer", "price", "currency", "problems": [...],
    "emi": {"status", "sources", "amount", "why"}}."""
    html = (html or "")[:limit]
    r = _Report()
    blocks: List[Any] = []
    nodes: List[Dict[str, Any]] = []
    for m in _LD.finditer(html):
        raw = m.group(1).strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            try:
                obj = json.loads(re.sub(r",\s*([}\]])", r"\1", raw.replace("\n", " ")))
            except ValueError:
                r.invalid("JSON-LD", "block", raw[:40], "not valid JSON")
                continue
        blocks.append(obj)
        _walk(obj, nodes)
    info = _check_nodes(nodes, r)
    _microdata_product(html, r, info)
    cfg = emi_cfg or {}
    emi = _emi(html, blocks, info, page_url, cfg) if cfg.get("check", True) else {"status": "not_checked", "sources": [],
                                                                                  "amount": None, "why": ""}
    probs = []
    seen = set()
    for typ, field, kind, detail in r.problems:
        if (typ, field, kind) not in seen:
            seen.add((typ, field, kind))
            probs.append({"type": typ, "field": field, "kind": kind, "detail": detail})
    return {"types": info["types"], "product": info["product"], "offer": info["offer"], "price": info["price"],
            "currency": info["currency"], "problems": probs, "emi": emi}


def findings(url: str, res: Dict[str, Any]) -> List[Tuple[str, str, Dict[str, Any]]]:
    """(rule, subject, fields) for the fixes: one per schema type + field + kind, and the EMI status."""
    out: List[Tuple[str, str, Dict[str, Any]]] = []
    for p in res.get("problems") or []:
        if p["type"] in ("Product", "Offer") and p["field"] in ("name", "price", "priceCurrency", "availability", "sku", "image") \
                and p["kind"] == "missing":
            continue                                       # already reported by the PRODUCT_* checks (qc6.product)
        rule = "SCHEMA_FIELD_MISSING" if p["kind"] == "missing" else "SCHEMA_VALUE_INVALID"
        out.append((rule, f"{p['type']}.{p['field']}", {"value": p["detail"] or p["field"]}))
    e = res.get("emi") or {}
    if e.get("status") == "missing":
        out.append(("EMI_MISSING", "", {"value": e.get("why") or ""}))
    elif e.get("status") == "invalid":
        out.append(("EMI_INVALID", "", {"value": e.get("why") or ""}))
    return out
