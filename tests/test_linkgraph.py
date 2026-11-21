"""Link intelligence on a purpose-built site: click depth, link strength, unreachable pages, canonical targets,
near-duplicates; and the pure functions on hand-made graphs."""
import asyncio
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ["QC6_ALLOW_PRIVATE_TARGETS"] = "true"

import numpy as np  # noqa: E402
import pytest  # noqa: E402

from qc6 import linkgraph as G  # noqa: E402
from qc6 import services  # noqa: E402
from qc6.workers.runner import execute_run  # noqa: E402

STORY = ("Our personal loan helps you pay for a wedding, a trip or a medical bill without touching your savings. "
         "You can borrow from fifty thousand to forty lakh rupees and repay over one to seven years. Interest is fixed "
         "for the whole term, so your monthly instalment never changes. Apply online in a few minutes with your PAN and "
         "salary slips, and most approved loans reach your bank account within a day. There are no hidden charges and "
         "you may prepay after twelve instalments.")
OTHER = ("A fixed deposit grows your money at a guaranteed rate. Choose a term between twelve and sixty months and get "
         "interest monthly, quarterly or at maturity. Senior citizens earn an extra quarter percent. Deposits can be "
         "renewed automatically and you can take a loan against your deposit if you need cash before it matures. "
         "The minimum deposit is fifteen thousand rupees and the rate is locked on the day you invest.")


def page(title, links=(), canonical=None, noindex=False, text=""):
    head = f"<title>{title}</title><meta name='description' content='{title} description text for tests.'>"
    if canonical:
        head += f"<link rel='canonical' href='{canonical}'>"
    if noindex:
        head += "<meta name='robots' content='noindex'>"
    body = "".join(f"<a href='{h}'>{h}</a> " for h in links)
    return f"<html lang='en'><head>{head}</head><body><nav>{body}</nav><main><h1>{title}</h1><p>{text}</p></main></body></html>"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        b = f"http://{self.headers['Host']}"
        P = {
            "/": page("Home", ["/a", "/dup1", "/dup2", "/fd", "/canon-broken", "/canon-redirect", "/canon-noindex", "/chain"]),
            "/a": page("A", ["/b"]), "/b": page("B", ["/c"]), "/c": page("C", ["/d"]), "/d": page("D", ["/e"]), "/e": page("E"),
            "/island1": page("Island 1", ["/island2"]), "/island2": page("Island 2", ["/island1"]),
            "/canon-broken": page("CB", canonical=f"{b}/gone"),
            "/canon-redirect": page("CR", canonical=f"{b}/moved"),
            "/canon-noindex": page("CN", canonical=f"{b}/hidden"),
            "/hidden": page("Hidden", noindex=True),
            "/chain": page("Chain", canonical=f"{b}/mid"), "/mid": page("Mid", canonical=f"{b}/a"),
            "/dup1": page("Loan offer Pune", text=STORY),
            "/dup2": page("Loan offer Nagpur", text=STORY.replace("a trip", "a holiday")),
            "/fd": page("Fixed deposit", text=OTHER),
        }
        if self.path == "/moved":
            self.send_response(301); self.send_header("Location", "/a"); self.end_headers(); return
        if self.path not in P:
            self.send_response(404); self.end_headers(); self.wfile.write(b"gone"); return
        body = P[self.path].encode()
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers(); self.wfile.write(body)


@pytest.fixture(scope="module")
def checked(migrated, tmp_path_factory):
    from qc6.settings import settings
    settings.data_dir = str(tmp_path_factory.mktemp("graph"))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    paths = ["/", "/a", "/b", "/c", "/d", "/e", "/island1", "/island2", "/canon-broken", "/canon-redirect", "/canon-noindex",
             "/hidden", "/chain", "/mid", "/dup1", "/dup2", "/fd"]
    import asyncpg
    from qc6.db.pool import _init

    async def go():
        pool = await asyncpg.create_pool(migrated, min_size=1, max_size=4, init=_init)
        ws = await services.create_workspace(pool, "Graph Co", "g@graph.test", "Gauri")
        sid = await services.create_site(pool, ws["workspace_id"], "Graph", base, {"rate_rps": 40})
        rid = await services.start_run(pool, sid, "coverage", {"urls": [base + p for p in paths], "images": False})
        await execute_run(pool, rid)
        facts = {r["url"][len(base):]: r["facts"] for r in await pool.fetch("SELECT url, facts FROM pages WHERE run_id = $1", rid)}
        fixes = await pool.fetch("""SELECT f.rule, f.title, f.subject, array_agg(DISTINCT fd.page_url) AS pages FROM fixes f
                                    JOIN findings fd ON fd.fix_id = f.id WHERE f.site_id = $1 GROUP BY f.id""", sid)
        summary = await pool.fetchval("SELECT summary FROM runs WHERE id = $1", rid)
        await pool.close()
        return facts, [dict(r) for r in fixes], summary
    out = asyncio.run(go())
    srv.shutdown()
    return base, out


def test_depth_and_strength(checked):
    base, (facts, fixes, summary) = checked
    assert summary["link_graph"]["home_found"] is True
    assert [facts[p].get("depth") for p in ("/", "/a", "/b", "/c", "/d", "/e")] == [0, 1, 2, 3, 4, 5]
    assert facts["/island1"].get("depth") is None and facts["/island1"]["inlinks"] == 1
    assert facts["/a"]["inlinks"] == 1 and facts["/"]["inlinks"] == 0
    assert all(0 <= f["authority"] <= 10 for f in facts.values())
    assert facts["/e"]["authority"] > facts["/island1"]["authority"] or facts["/e"]["authority"] >= 0


def _pages(fixes, rule, base):
    return sorted(p[len(base):] for f in fixes if f["rule"] == rule for p in f["pages"])


def test_link_problems_become_fixes(checked):
    base, (facts, fixes, summary) = checked
    assert _pages(fixes, "DEEP_PAGE", base) == ["/d", "/e"]
    assert _pages(fixes, "NOT_REACHABLE_FROM_HOME", base) == ["/island1", "/island2"]
    assert _pages(fixes, "CANONICAL_TO_BROKEN", base) == ["/canon-broken"]
    assert _pages(fixes, "CANONICAL_TO_REDIRECT", base) == ["/canon-redirect"]
    assert _pages(fixes, "CANONICAL_TO_NOINDEX", base) == ["/canon-noindex"]
    assert _pages(fixes, "CANONICAL_CHAIN", base) == ["/chain"]
    nd = [f for f in fixes if f["rule"] == "NEAR_DUPLICATE"]
    assert len(nd) == 1 and sorted(p[len(base):] for p in nd[0]["pages"]) == ["/dup1", "/dup2"]
    assert nd[0]["title"] == "2 pages have almost the same main text"


def test_pure_functions():
    # star: everyone links to 0 -> 0 is strongest; chain depth
    src, dst = np.array([1, 2, 3, 0]), np.array([0, 0, 0, 1])
    pr = G.pagerank(4, src, dst)
    assert pr.argmax() == 0 and abs(pr.sum() - 1) < 1e-6
    assert list(G.click_depth(4, src, dst, 0)) == [0, 1, -1, -1]
    assert list(G.percentile_0_10(np.array([0.1, 0.5, 0.3]))) == [0.0, 10.0, 5.0]
    a = G.text_simhash(f"<main>{STORY}</main>")
    b = G.text_simhash(f"<main>{STORY.replace('a trip', 'a holiday')}</main>")
    c = G.text_simhash(f"<main>{OTHER}</main>")
    assert G.text_simhash("<main>too short</main>") is None
    assert 0 < bin(a ^ b).count("1") <= G.NEAR_DUP_BITS < bin(a ^ c).count("1")
    assert G.near_duplicate_groups({"x": a, "y": b, "z": c}) == [["x", "y"]]
    assert G.near_duplicate_groups({"x": a, "y": a}) == []                    # identical text: V5's exact-duplicate rule
    words = G.main_text_words("<header>menu menu</header><main><p>Hello world</p></main><footer>foot</footer>")
    assert words == ["hello", "world"]
