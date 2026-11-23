"""QC checks page URLs and image URLs only; the pre-built web app is one intact bundle."""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engine"))

from qcv5.crawler.on_page_audit_engine import extract_on_page_urls, is_skipped_asset  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


def test_css_js_fonts_are_not_checked_links_and_images_are():
    html = """<html><head><link rel=stylesheet href="/a.css"><link rel=preload href="/f.woff2" as=font>
      <link rel=modulepreload href="/m.mjs"><script src="/app.js"></script><link rel=canonical href="https://s.ex/p"></head>
      <body><a href="/x">x</a><a href="/lib.js?v=2">js</a><a href="/doc.pdf">pdf</a><img src="/i.png">
      <style>.a{background:url(/bg.jpg)} @font-face{src:url(/f.ttf)}</style></body></html>"""
    n, rels, parser = extract_on_page_urls("https://s.ex/p", "https://s.ex/p", html)
    urls = {r["normalized_found_url"] for r in rels}
    assert urls == {"https://s.ex/p", "https://s.ex/x", "https://s.ex/doc.pdf"}, urls
    assert n == 3
    assert any(i["url"] == "/i.png" for i in parser.images)                       # images still checked
    assert is_skipped_asset("https://cdn.ex/x/app.min.js?v=3", "ANCHOR") and not is_skipped_asset("https://s.ex/js-guide", "ANCHOR")


def test_web_app_build_is_one_intact_bundle():
    """A merge once left conflict lines in web/dist/index.html and loaded two app bundles into one page."""
    html = open(os.path.join(ROOT, "web", "dist", "index.html"), encoding="utf-8").read()
    scripts = re.findall(r'<script[^>]+src="/([^"]+)"', html)
    styles = re.findall(r'<link[^>]+stylesheet[^>]+href="/(assets/[^"]+)"', html)
    assert len(scripts) == 1 and len(styles) == 1, (scripts, styles)
    for f in scripts + styles:
        assert os.path.exists(os.path.join(ROOT, "web", "dist", f)), f
    body = re.sub(r"<[^>]*>", "", html)
    assert not re.search(r"^\s*(<<<<<<<|=======|>>>>>>>|main|claude/\S+)\s*$", body, re.M)
