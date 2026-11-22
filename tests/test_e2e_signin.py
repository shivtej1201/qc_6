"""QC's own sign-in in the real web app (QC6_AUTH=local): sign in, turn two-factor on in "Your account", sign out,
sign in again with a code, and axe on both screens."""
import asyncio
import os
import socket
import threading
import time

import pytest

from qc6 import local_auth as A
from qc6 import services

WEB_DIST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "dist")
AXE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "qc6", "assets", "axe.min.js")
pytestmark = pytest.mark.skipif(not os.path.isdir(WEB_DIST), reason="web app not built (cd web && npm run build)")
PW = "a long enough password"


@pytest.fixture(scope="module")
def app_url(migrated):
    old = {k: os.environ.get(k) for k in ("QC6_AUTH", "QC6_SECRET_KEY", "QC6_COOKIE_SECURE")}
    os.environ.update({"QC6_AUTH": "local", "QC6_SECRET_KEY": "e2e-key", "QC6_COOKIE_SECURE": "false"})
    A._ip_hits.clear()
    import asyncpg
    import uvicorn
    from qc6.db.pool import _init

    async def seed():
        pool = await asyncpg.create_pool(migrated, min_size=1, max_size=2, init=_init)
        await pool.execute("TRUNCATE workspaces CASCADE")
        ws = await services.create_workspace(pool, "Sign-in Co", "nia@signin.test", "Nia")
        await services.create_site(pool, ws["workspace_id"], "Example", "https://example.test/", {})
        mid = await pool.fetchval("SELECT id FROM members WHERE email = 'nia@signin.test'")
        await A.set_password(pool, mid, PW)
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
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


async def _axe(page):
    with open(AXE, encoding="utf-8") as f:
        await page.evaluate(f.read() + "\n;window.axe = axe;")
    return await page.evaluate("""async () => (await axe.run(document, { resultTypes: ['violations'] })).violations
        .filter(v => ['serious', 'critical'].includes(v.impact)).map(v => v.id)""")


def test_sign_in_and_two_factor_in_browser(app_url):
    from playwright.async_api import async_playwright

    async def go():
        errors = []
        async with async_playwright() as p:
            b = await p.chromium.launch()
            page = await b.new_page(viewport={"width": 390, "height": 844})
            page.on("pageerror", lambda e: errors.append(str(e)))
            await page.goto(f"{app_url}/#/today")
            await page.wait_for_selector("h1:text('Sign in')")
            bad_signin = await _axe(page)
            await page.fill("input[type=email]", "nia@signin.test")
            await page.fill("input[type=password]", "wrong password here")
            await page.get_by_role("button", name="Sign in").click()
            await page.wait_for_selector("text=Wrong e-mail or password")
            await page.fill("input[type=password]", PW)
            await page.get_by_role("button", name="Sign in").click()
            await page.wait_for_selector("text=Your account", state="attached")

            await page.goto(f"{app_url}/#/account")
            await page.wait_for_selector("h1:text('Your account')")
            await page.get_by_role("button", name="Set up two-factor").click()
            key = (await page.locator("b.mono").text_content()).replace(" ", "")
            await page.fill("#tf-code", A.totp_at(key, int(time.time() // 30)))
            await page.get_by_role("button", name="Turn on").click()
            await page.wait_for_selector("text=Save these recovery codes")
            codes = (await page.locator("pre").text_content()).split()
            bad_account = await _axe(page)
            await page.get_by_role("button", name="I've saved them").click()
            await page.wait_for_selector("text=10 recovery codes left")

            await page.get_by_role("button", name="Sign out").click()
            await page.wait_for_selector("h1:text('Sign in')")
            await page.fill("input[type=email]", "nia@signin.test")
            await page.fill("input[type=password]", PW)
            await page.get_by_role("button", name="Sign in").click()
            await page.wait_for_selector("h1:text('Two-factor code')")
            await page.fill("input[autocomplete=one-time-code]", "000000")
            await page.get_by_role("button", name="Continue").click()
            await page.wait_for_selector("text=That code isn't right")
            page.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
            await page.fill("input[autocomplete=one-time-code]", codes[0])
            await page.get_by_role("button", name="Continue").click()
            await page.wait_for_selector("h1:not(:text('Two-factor code'))")
            await page.goto(f"{app_url}/#/account")
            await page.wait_for_selector("text=9 recovery codes left")
            wide = await page.evaluate("document.documentElement.scrollWidth > innerWidth + 1")
            await b.close()
        return errors, bad_signin, bad_account, codes, wide

    errors, bad_signin, bad_account, codes, wide = asyncio.run(go())
    assert len(codes) == 10
    assert not bad_signin and not bad_account, (bad_signin, bad_account)
    assert not wide
    assert not errors, errors
