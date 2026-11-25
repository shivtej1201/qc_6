"""Every URL found on a page is reported or excluded with a reason; robots.txt matched as RFC 9309; components."""
import asyncio
import os
import sys

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "engine"))
from qcv5.crawler.on_page_audit_engine import RobotsCache, extract_on_page_images, extract_on_page_urls  # noqa: E402
from qcv5.crawler.robots_rules import Robots  # noqa: E402
from qcv5.crawler.url_rules import UrlRules  # noqa: E402

PAGE = """<html><head>
<script src="https://www.googletagmanager.com/gtm.js?id=GTM-X"></script>
<link rel="stylesheet" href="/s/site.css"><link rel="preload" href="/f/a.woff2" as="font">
<link rel="canonical" href="https://www.example.com/p/1">
<script src="https://static.clevertap.com/js/a.js"></script></head><body>
<header><nav class="mega-menu"><a href="/menu-1">M</a></nav><div class="breadcrumb"><a href="http://www.example.com/cat">Cat</a></div></header>
<noscript><iframe src="https://www.googletagmanager.com/ns.html?id=GTM-X"></iframe></noscript>
<noscript><img src="https://www.facebook.com/tr?id=1&ev=PageView"></noscript>
<main><ul class="product-grid"><li><a href="/p/2">P2</a></li></ul><a href="https://wzrkt.com/x">x</a>
<a href="https://track.example.org/c">c</a><img src="https://cdn.example.com/a.jpg"></main>
<footer><a href="/about">About</a></footer></body></html>"""


def test_found_equals_reported_plus_excluded_with_reasons():
    raw, rels, p = extract_on_page_urls("https://www.example.com/p/1", "https://www.example.com/p/1", PAGE)
    excl = {(x["reason"], x["url_type"]) for x in p.excluded_urls}
    assert ("Google Tag Manager", "IFRAME") in excl and ("JS", "SCRIPT") in excl and ("CSS", "STYLESHEET") in excl
    assert ("CleverTap", "ANCHOR") in excl and ("Preload / resource hint", "LINK_RESOURCE") in excl
    assert p.discovered_url_count == raw + sum(x["occurrence_count"] for x in p.excluded_urls)
    reported = {r["normalized_found_url"] for r in rels}
    assert not any("googletagmanager" in u or "clevertap" in u or "wzrkt" in u for u in reported)
    assert "http://www.example.com/cat" in reported                     # HTTP URLs are kept (reported as HTTP)
    comp = {r["normalized_found_url"]: r["representative_location"] for r in rels}
    assert comp["http://www.example.com/cat"] == "Breadcrumb" and comp["https://www.example.com/menu-1"] == "Navigation"
    assert comp["https://www.example.com/p/2"] == "Product listing" and comp["https://www.example.com/about"] == "Footer"
    ri, imgs = extract_on_page_images(p, "www.example.com")
    assert [i["normalized_image_url"] for i in imgs] == ["https://cdn.example.com/a.jpg"]
    assert p.excluded_images[0]["reason"] == "Tracking pixel" and p.discovered_image_count == ri + 1


def test_rules_are_configurable():
    r = UrlRules.load({"exclude_hosts": {"track.example.org": "Analytics"}, "include_hosts": ["static.clevertap.com"],
                       "include_technical_resources": True})
    _, rels, p = extract_on_page_urls("https://www.example.com/p/1", "https://www.example.com/p/1", PAGE, r)
    reasons = {x["normalized_url"]: x["reason"] for x in p.excluded_urls}
    assert reasons["https://track.example.org/c"] == "Analytics"
    assert "https://static.clevertap.com/js/a.js" not in reasons                 # include_hosts + technical audit on
    assert any(x["found_url"] == "/s/site.css" for x in rels)


def test_robots_rfc9309_longest_match_wildcards_and_own_group():
    r = Robots("User-agent: *\nDisallow: /\nAllow: /images/\nDisallow: /*?sort=\nAllow: /*.jpg$\n")
    assert r.check("https://c/images/a.png") == (True, "Allow: /images/")      # urllib.robotparser said blocked
    assert r.check("https://c/p/a.jpg")[0] and not r.check("https://c/p/a.jpg?x=1")[0]
    assert r.check("https://c/list?sort=1") == (False, "Disallow: /*?sort=")
    own = Robots("User-agent: QC-Audit\nDisallow: /private\n\nUser-agent: *\nDisallow: /")
    assert own.check("https://c/a")[0] and not own.check("https://c/private/x")[0]


def test_blocked_image_names_the_rule_and_unreachable_robots_blocks_nothing(monkeypatch):
    from qcv5.crawler.url_normalizer import URLNormalizer
    monkeypatch.setattr(URLNormalizer, "is_safe_url", staticmethod(lambda u, allow_local=False: True))
    def handler(req):
        if req.url.host == "cdn.test":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
        if req.url.host == "waf.test":
            return httpx.Response(403, text="denied")                # a CDN refusing robots.txt is not a robots block
        return httpx.Response(200)

    async def go():
        rc = RobotsCache()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return (await rc.check(c, "https://cdn.test/private/a.jpg"), await rc.check(c, "https://cdn.test/a.jpg"),
                    await rc.check(c, "https://waf.test/a.jpg"))
    blocked, ok, waf = asyncio.run(go())
    assert blocked[0] is False and "'Disallow: /private/' in https://cdn.test/robots.txt" == blocked[1]
    assert ok == (True, "") and waf == (True, "")
