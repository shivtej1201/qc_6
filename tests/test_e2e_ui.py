"""End to end through the real web app in Chromium (blueprint §14): Today -> Fixes -> assign -> "I've fixed it" ->
Insights -> Settings, a keyboard-only path, and axe on every screen (0 serious/critical on our own UI)."""
import asyncio
import os
import socket
import threading
import time

os.environ["QC6_ALLOW_PRIVATE_TARGETS"] = "true"

import pytest  # noqa: E402

from fixture_site import FixtureSite  # noqa: E402
from qc6 import services  # noqa: E402
from qc6.settings import settings  # noqa: E402

WEB_DIST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "dist")
AXE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "qc6", "assets", "axe.min.js")
pytestmark = pytest.mark.skipif(not os.path.isdir(WEB_DIST), reason="web app not built (cd web && npm run build)")
SCREENS = ["today", "insights", "live", "fixes", "pages", "links", "journeys", "integrations", "access", "settings"]


@pytest.fixture(scope="module")
def app_url(migrated, tmp_path_factory):
    site = FixtureSite()
    settings.data_dir = str(tmp_path_factory.mktemp("e2e"))
    import asyncpg
    import uvicorn
    from qc6.db.pool import _init
    from qc6.workers.runner import execute_run

    async def seed():
        pool = await asyncpg.create_pool(migrated, min_size=1, max_size=4, init=_init)
        await pool.execute("TRUNCATE workspaces CASCADE")          # this module owns the database from here on
        ws = await services.create_workspace(pool, "E2E Co", "lead@e2e.test", "Asha Rao")
        await services.add_member(pool, ws["workspace_id"], "dev@e2e.test", "Rahul Mehta", "editor")
        sid = await services.create_site(pool, ws["workspace_id"], "Fixture", site.base, {"rate_rps": 40, "visitors": 3})
        rid = await services.start_run(pool, sid, "full", {"urls": site.urls(3), "fast_visits": True, "fast_journeys": True,
                                                          "per_template": 2, "images": False})
        await execute_run(pool, rid, fast_journeys=True)
        await pool.close()
    asyncio.run(seed())
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    from qc6.api.app import app
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(timeout=10)
    site.close()


async def _axe(page):
    with open(AXE, encoding="utf-8") as f:
        await page.evaluate(f.read() + "\n;window.axe = axe;")      # evaluate, not a <script>: our CSP blocks inline scripts
    res = await page.evaluate("""async () => {
        const r = await axe.run(document, { resultTypes: ['violations'] });
        return r.violations.filter(v => ['serious', 'critical'].includes(v.impact))
                 .map(v => ({ id: v.id, impact: v.impact, nodes: v.nodes.slice(0, 3).map(n => n.target.join(' ') + ' ' + JSON.stringify(((n.any||[])[0]||{}).data||{})) }));
    }""")
    return res


def test_ui_end_to_end(app_url):
    from playwright.async_api import async_playwright

    async def go():
        problems, errors = {}, []
        async with async_playwright() as p:
            b = await p.chromium.launch()
            for scheme in ("light", "dark"):
                page = await b.new_page(viewport={"width": 1280, "height": 900}, color_scheme=scheme)
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.on("console", lambda m: m.type == "error" and errors.append(m.text))
                for v in SCREENS:
                    await page.goto(f"{app_url}/#/{v}")
                    await page.wait_for_selector("h1")
                    await page.wait_for_timeout(700)                     # let the reveal animation finish
                    bad = await _axe(page)
                    if bad:
                        problems[f"{scheme}:{v}"] = bad
                await page.close()

            # the main path: Fixes -> open one -> give it to Rahul -> "I've fixed it"
            page = await b.new_page(viewport={"width": 1280, "height": 900})
            await page.goto(f"{app_url}/#/fixes")
            await page.locator("tr[data-fix]").first.click()
            await page.wait_for_selector("#fix-title")
            await page.select_option("#owner", label="Rahul Mehta")
            await page.wait_for_selector("text=Rahul Mehta now owns this fix.")
            await page.get_by_role("button", name="I've fixed it").click()
            await page.wait_for_selector("text=Marked fixed. The next check will confirm it.")
            drawer_bad = await _axe(page)
            await page.keyboard.press("Escape")
            await page.wait_for_selector("#fix-title", state="detached")

            # Insights shows the check; Settings saves a V5 preset
            await page.goto(f"{app_url}/#/insights")
            await page.wait_for_selector("text=Health over time")
            await page.get_by_role("button", name="Show as table").first.click()
            assert await page.locator("table").count() >= 2
            await page.goto(f"{app_url}/#/settings")
            await page.get_by_text("Careful", exact=True).click()
            assert await page.input_value("#s-rate") == "8"
            await page.get_by_role("button", name="Save settings").click()
            await page.wait_for_selector("text=Settings saved.")

            # keyboard only: tab to the Fixes link in the rail, open it, open a fix with Enter
            await page.goto(f"{app_url}/#/today")
            await page.wait_for_selector("h1")
            for _ in range(30):
                await page.keyboard.press("Tab")
                if await page.evaluate("document.activeElement && document.activeElement.getAttribute('href') === '#/fixes'"):
                    break
            await page.keyboard.press("Enter")
            await page.wait_for_selector("tr[data-fix]")
            await page.locator("tr[data-fix]").first.focus()
            await page.keyboard.press("Enter")
            await page.wait_for_selector("#fix-title")
            focused = await page.evaluate("document.activeElement && document.activeElement.textContent")

            # narrow screen: no sideways scrolling
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.goto(f"{app_url}/#/insights")
            await page.wait_for_selector("text=Health over time")
            overflow = await page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
            await b.close()
        return problems, drawer_bad, errors, focused, overflow

    problems, drawer_bad, errors, focused, overflow = asyncio.run(go())
    assert problems == {}, problems
    assert drawer_bad == [], drawer_bad
    # axe tries to fetch() cross-origin stylesheets to read their rules; our CSP (connect-src 'self') rightly blocks
    # that. The page itself loads the same stylesheet through <link> (style-src) and logs no errors without axe.
    errors = [e for e in errors if not ("fonts.googleapis.com" in e and "connect-src" in e)]
    assert errors == [], errors
    assert focused == "Close"                                   # focus moves into the fix drawer
    assert overflow <= 0, f"page scrolls sideways by {overflow}px at phone width"
