"""Structured data checks (Product, Offer, AggregateRating, BreadcrumbList, Organization, WebPage, FAQPage) and EMI found
in any supported structure (never reported missing when the page shows it)."""
from qc6 import schema_check as sc
from qc6.components import component

PDP = '''<html><head><script type="application/ld+json">{"@context":"https://schema.org","@graph":[
{"@type":"Product","name":"Phone X","image":"https://c/x.jpg","sku":"X1","offers":{"@type":"Offer","price":"₹74,999",
 "priceCurrency":"INR","availability":"https://schema.org/InStock"},
 "aggregateRating":{"@type":"AggregateRating","ratingValue":"4.4","reviewCount":"0"}},
{"@type":"BreadcrumbList","itemListElement":[{"@type":"ListItem","position":1,"name":"Home","item":"https://c/"},
 {"@type":"ListItem","position":3,"name":"Phones"}]},
{"@type":"Organization","name":"C","url":"https://c/"}]}</script>
<script id="__NEXT_DATA__">{"props":{"product":{"emiStartsAt":"3,125"}}}</script></head>
<body><h1>Phone X</h1></body></html>'''


def _probs(res):
    return {(p["type"], p["field"], p["kind"]) for p in res["problems"]}


def test_product_offer_rating_breadcrumb_organization():
    r = sc.check(PDP, "https://c/p/x")
    assert r["product"] and r["offer"] and r["currency"] == "INR" and r["price"] is None
    assert _probs(r) == {("Product", "description", "missing"), ("Offer", "price", "invalid"),
                         ("AggregateRating", "ratingCount", "invalid"), ("BreadcrumbList", "ListItem.position", "invalid"),
                         ("Organization", "logo", "missing")}
    f = sc.findings("https://c/p/x", r)
    assert ("SCHEMA_VALUE_INVALID", "Offer.price") in {(a, b) for a, b, _ in f}
    assert component("SCHEMA_VALUE_INVALID", subject="Offer.price") == "Offer schema"


def test_good_product_and_faq_and_webpage():
    good = '''<script type="application/ld+json">[{"@type":"Product","name":"A","image":"i.jpg","description":"d","gtin13":"1",
      "offers":{"@type":"Offer","price":"25000.00","priceCurrency":"INR","availability":"InStock"}},
      {"@type":"FAQPage","mainEntity":[{"@type":"Question","name":"Q?","acceptedAnswer":{"@type":"Answer","text":"A."}},
        {"@type":"Question","name":"Q2?"}]},{"@type":"WebPage"}]</script><p>EMI starts at ₹2,083/month</p>'''
    r = sc.check(good, "https://c/p/a")
    assert r["price"] == 25000.0
    assert _probs(r) == {("FAQPage", "Question.acceptedAnswer.text", "missing"), ("WebPage", "name", "missing")}
    assert r["emi"]["status"] == "present" and r["emi"]["amount"] == 2083.0 and r["emi"]["sources"] == ["visible text"]


def test_emi_found_in_each_supported_structure():
    price = '<script type="application/ld+json">{"@type":"Product","name":"A","offers":{"price":"30000","priceCurrency":"INR"}}</script>'
    state = price + '<script>window.__INITIAL_STATE__={"pdp":{"monthlyEmi":2500}}</script>'
    ld = '<script type="application/ld+json">{"@type":"Product","name":"A","offers":{"price":"30000","priceCurrency":"INR",' \
         '"priceSpecification":{"@type":"UnitPriceSpecification","name":"EMI","price":"2500","priceCurrency":"INR"}}}</script>'
    micro = price + '<span itemprop="emiAmount" content="2500"></span>'
    for html, src in ((state, "embedded state"), (ld, "structured data"), (micro, "microdata")):
        e = sc.check(html, "https://c/p/a")["emi"]
        assert e["status"] == "present" and src in e["sources"], (src, e)
    assert sc.check(price, "https://c/p/a")["emi"]["status"] == "missing"                  # a price, no EMI anywhere
    assert sc.check("<p>About us</p>", "https://c/about")["emi"]["status"] == "not_applicable"
    assert sc.check("<p>No Cost EMI available</p>", "https://c/x")["emi"]["status"] == "invalid"   # mentioned, no amount
    assert sc.check(price + "<p>EMI ₹45,000/month</p>", "https://c/x")["emi"]["status"] == "invalid"   # above the price
    assert sc.check("<p>hi</p>", "https://c/products/x", {"required_paths": ["/products/*"]})["emi"]["status"] == "missing"
    assert sc.check(price, "https://c/p/a", {"min_price": 50000})["emi"]["status"] == "not_applicable"
    assert sc.check(price, "https://c/p/a", {"check": False})["emi"]["status"] == "not_checked"


def test_broken_json_ld_is_reported_not_crashed():
    r = sc.check('<script type="application/ld+json">{"@type": "Product", </script>', "https://c/x")
    assert ("JSON-LD", "block", "invalid") in _probs(r)
