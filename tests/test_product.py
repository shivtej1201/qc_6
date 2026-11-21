"""Product pages: is the product's data complete? (reader unit tests + a small shop crawled for real)"""
import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ["QC6_ALLOW_PRIVATE_TARGETS"] = "true"

import pytest  # noqa: E402

from qc6 import product as P  # noqa: E402
from qc6 import services  # noqa: E402
from qc6.workers.runner import execute_run  # noqa: E402


def ld(obj):
    return f'<script type="application/ld+json">{json.dumps(obj)}</script>'


FULL = {"@context": "https://schema.org", "@type": "Product", "name": "Phone X", "sku": "PX-1", "image": "https://x.test/px.jpg",
        "offers": {"@type": "Offer", "price": "74999.00", "priceCurrency": "INR", "availability": "https://schema.org/InStock"}}


def test_reader():
    assert P.extract("<p>nothing</p>") is None
    full = P.extract(ld(FULL))
    assert full["source"] == "json-ld" and all(full[f] for f in P.FIELDS)
    graph = P.extract(ld({"@graph": [{"@type": "BreadcrumbList"}, {**FULL, "@type": ["Product", "Thing"]}]}))
    assert all(graph[f] for f in P.FIELDS)
    agg = P.extract(ld({**FULL, "offers": {"@type": "AggregateOffer", "lowPrice": "999", "priceCurrency": "INR"}}))
    assert agg["price"] and not agg["availability"]
    bad = P.extract(ld({**FULL, "offers": {"price": "₹74,999", "priceCurrency": "Rupees", "availability": "yes"}}))
    assert not bad["price"] and not bad["currency"] and not bad["availability"]
    micro = P.extract('<div itemscope itemtype="https://schema.org/Product"><span itemprop="name">A</span>'
                      '<meta itemprop="price" content="10"><meta itemprop="priceCurrency" content="INR">'
                      '<link itemprop="availability" href="https://schema.org/InStock"><meta itemprop="sku" content="S1">'
                      '<img itemprop="image" src="a.jpg"></div>')
    assert micro["source"] == "microdata" and micro["price"] and micro["currency"] and micro["identifier"]
    assert P.extract("<script type='application/ld+json'>{broken</script>") is None


SHELL = ("<html lang='en'><head><title>{t} | Shop</title><meta name='description' content='Buy {t} online at the best price today.'>"
         "{ld}</head><body><header><a href='/'>Shop</a></header><main><h1>{t}</h1><div class='gallery'><img src='/i.png' alt='{t}'></div>"
         "<p>{t} with free delivery, easy returns and a two year warranty from the maker.</p><button>Add to cart</button></main></body></html>")
PAGES = {
    "/p/full": ld(FULL),
    "/p/noprice": ld({**FULL, "offers": {**FULL["offers"], "price": ""}}),
    "/p/bare": "",
    "/p/aggregate": ld({**FULL, "offers": {"@type": "AggregateOffer", "lowPrice": "999", "priceCurrency": "INR"}}),
    "/p/plain": ld({k: v for k, v in FULL.items() if k not in ("sku", "image")}),
    "/p/micro": "",
}
MICRO = ('<div itemscope itemtype="https://schema.org/Product"><meta itemprop="name" content="M"><meta itemprop="price" content="10">'
         '<meta itemprop="priceCurrency" content="INR"><link itemprop="availability" href="https://schema.org/InStock">'
         '<meta itemprop="sku" content="S1"><meta itemprop="image" content="/m.jpg"></div>')


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path not in PAGES:
            self.send_response(404); self.end_headers(); return
        body = SHELL.format(t=self.path.rsplit("/", 1)[-1].title(), ld=PAGES[self.path])
        if self.path == "/p/micro":
            body = body.replace("</main>", MICRO + "</main>")
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(body.encode())


def test_shop(migrated, tmp_path, monkeypatch):
    from qc6.settings import settings
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    import asyncpg
    from qc6.db.pool import _init

    async def go():
        pool = await asyncpg.create_pool(migrated, min_size=1, max_size=4, init=_init)
        ws = await services.create_workspace(pool, "Shop Co", "s@shop.test", "Shan")
        sid = await services.create_site(pool, ws["workspace_id"], "Shop", base, {"rate_rps": 40})
        rid = await services.start_run(pool, sid, "coverage", {"urls": [base + p for p in PAGES], "images": False})
        await execute_run(pool, rid)
        rows = await pool.fetch("""SELECT f.rule, fd.page_url FROM findings fd JOIN fixes f ON f.id = fd.fix_id
                                   WHERE fd.run_id = $1 AND f.rule LIKE 'PRODUCT_%'""", rid)
        fact = await pool.fetchval("SELECT facts->'product' FROM pages WHERE run_id = $1 AND url = $2", rid, base + "/p/full")
        await pool.close()
        return rows, fact
    rows, fact = asyncio.run(go())
    srv.shutdown()
    got = {}
    for r in rows:
        got.setdefault(r["page_url"].rsplit("/", 1)[-1], set()).add(r["rule"])
    assert got == {"noprice": {"PRODUCT_PRICE_MISSING"}, "bare": {"PRODUCT_DATA_MISSING"},
                   "aggregate": {"PRODUCT_AVAILABILITY_MISSING"}, "plain": {"PRODUCT_IDENTIFIER_MISSING", "PRODUCT_IMAGE_MISSING"}}
    assert fact["source"] == "json-ld" and fact["price"] is True
