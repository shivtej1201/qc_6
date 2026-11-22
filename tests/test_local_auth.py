"""QC's own sign-in (QC6_AUTH=local): passwords, sessions, two-factor, recovery codes, lockout, CSRF."""
import asyncio
import os
import time

import pytest

from qc6 import local_auth as A
from qc6 import services


def test_password_hashing():
    h = A.hash_password("correct horse battery")
    assert h.startswith("scrypt$15$8$1$") and A.check_password("correct horse battery", h)
    assert not A.check_password("wrong horse battery", h) and not A.check_password("x", None)
    assert A.hash_password("correct horse battery") != h                       # salted
    with pytest.raises(A.AuthFailed):
        A.hash_password("short")


def test_totp_rfc6238_and_reuse():
    import base64
    secret = base64.b32encode(b"12345678901234567890").decode()                # RFC 6238 SHA-1 test seed
    assert A.totp_at(secret, 59 // 30, digits=8) == "94287082"                 # RFC 6238 Appendix B, T=59
    assert A.totp_at(secret, 1111111109 // 30, digits=8) == "07081804"
    step = A.totp_check(secret, A.totp_at(secret, int(time.time() // 30)), None)
    assert step is not None
    assert A.totp_check(secret, A.totp_at(secret, step), step) is None         # same code twice: no
    assert A.otpauth_uri(secret, "a@b.test").startswith("otpauth://totp/QC%20V6:a%40b.test?secret=")


H = {"X-QC-Requested-With": "qc6"}


@pytest.fixture(scope="module")
def client(migrated):
    os.environ.update({"QC6_AUTH": "local", "QC6_SECRET_KEY": "test-key-for-2fa", "QC6_COOKIE_SECURE": "false"})
    A._ip_hits.clear()
    import asyncpg
    from qc6.db.pool import _init

    async def seed():
        pool = await asyncpg.create_pool(migrated, min_size=1, max_size=2, init=_init)
        ws = await services.create_workspace(pool, "Local Co", "lena@local.test", "Lena")
        await services.add_member(pool, ws["workspace_id"], "omar@local.test", "Omar", "editor")
        for email in ("lena@local.test", "omar@local.test"):
            mid = await pool.fetchval("SELECT id FROM members WHERE email = $1", email)
            await A.set_password(pool, mid, "a long enough password")
        await pool.close()
    asyncio.run(seed())
    from fastapi.testclient import TestClient
    from qc6.api.app import app
    with TestClient(app) as c:
        yield c
    for k in ("QC6_AUTH", "QC6_COOKIE_SECURE"):
        os.environ.pop(k, None)


def _login(c, email, pw="a long enough password"):
    c.cookies.clear()
    return c.post("/api/v2/auth/login", json={"email": email, "password": pw}, headers=H)


def test_sign_in_flow_two_factor_and_recovery(client):
    c = client
    c.cookies.clear()
    r = c.get("/api/v2/me")
    assert r.status_code == 401 and r.json()["detail"] == "auth=local"
    assert c.get("/api/v2/auth/mode").json() == {"mode": "local", "signed_in": False}
    assert c.post("/api/v2/auth/login", json={"email": "lena@local.test", "password": "a long enough password"}).status_code == 403   # no CSRF header
    bad = _login(c, "lena@local.test", "nope nope nope")
    unknown = _login(c, "nobody@local.test", "nope nope nope")
    assert bad.status_code == unknown.status_code == 401 and bad.json()["title"] == unknown.json()["title"] == "Wrong e-mail or password"
    r = _login(c, "lena@local.test")
    assert r.status_code == 200 and r.json() == {"two_factor": False}
    ck = r.headers["set-cookie"].lower()
    assert "httponly" in ck and "samesite=strict" in ck
    assert c.get("/api/v2/me").json()["member"]["email"] == "lena@local.test"
    # turn on two-factor
    setup = c.post("/api/v2/auth/2fa/setup", headers=H).json()
    code = A.totp_at(setup["secret"], int(time.time() // 30))
    wrong = "111111" if code != "111111" and A.totp_at(setup["secret"], int(time.time() // 30) + 1) != "111111" else "222222"
    assert c.post("/api/v2/auth/2fa/enable", json={"secret": setup["secret"], "code": wrong}, headers=H).status_code == 400
    codes = c.post("/api/v2/auth/2fa/enable", json={"secret": setup["secret"], "code": code}, headers=H).json()["recovery_codes"]
    assert len(codes) == 10 and c.get("/api/v2/auth/account").json()["two_factor"] is True
    assert c.post("/api/v2/auth/logout", headers=H).status_code == 200 and c.get("/api/v2/me").status_code == 401
    # sign in again: password alone is not enough
    assert _login(c, "lena@local.test").json() == {"two_factor": True}
    assert c.get("/api/v2/me").status_code == 401
    assert c.post("/api/v2/auth/2fa", json={"code": "000000" if code != "000000" else "111111"}, headers=H).status_code == 401
    now_code = A.totp_at(setup["secret"], int(time.time() // 30) + 1)          # next step's code (allowed: +-1)
    assert c.post("/api/v2/auth/2fa", json={"code": now_code}, headers=H).status_code == 200
    assert c.get("/api/v2/me").status_code == 200
    _login(c, "lena@local.test")
    assert c.post("/api/v2/auth/2fa", json={"code": now_code}, headers=H).status_code == 401                # used already
    assert c.post("/api/v2/auth/2fa", json={"code": codes[0]}, headers=H).json()["recovery_codes_left"] == 9
    _login(c, "lena@local.test")
    assert c.post("/api/v2/auth/2fa", json={"code": codes[0]}, headers=H).status_code == 401              # single use
    # turn it off again (needs the password)
    c.post("/api/v2/auth/2fa", json={"code": codes[1]}, headers=H)
    assert c.post("/api/v2/auth/2fa/disable", json={"current": "wrong", "new": ""}, headers=H).status_code == 400
    assert c.post("/api/v2/auth/2fa/disable", json={"current": "a long enough password", "new": ""}, headers=H).status_code == 200


def test_lockout_and_password_change(client):
    c = client
    for _ in range(5):
        assert _login(c, "omar@local.test", "wrong password!!").status_code == 401
    r = _login(c, "omar@local.test")
    assert r.status_code == 429 and "locked" in r.json()["title"]
    import asyncio as aio

    async def unlock():
        import asyncpg
        from qc6.settings import settings
        conn = await asyncpg.connect(settings.database_url)
        await conn.execute("UPDATE members SET locked_until = NULL WHERE email = 'omar@local.test'")
        await conn.close()
    aio.run(unlock())
    A._ip_hits.clear()
    assert _login(c, "omar@local.test").status_code == 200
    other = dict(c.cookies)
    c.cookies.clear()
    _login(c, "omar@local.test")
    assert c.post("/api/v2/auth/password", json={"current": "a long enough password", "new": "short"}, headers=H).status_code == 400
    assert c.post("/api/v2/auth/password", json={"current": "a long enough password", "new": "an even longer password"}, headers=H).status_code == 200
    assert c.get("/api/v2/me").status_code == 200                                   # this session stays
    c.cookies.clear()
    c.cookies.update(other)
    assert c.get("/api/v2/me").status_code == 401                                   # the other one was signed out


def test_endpoints_off_outside_local(client, monkeypatch):
    monkeypatch.setenv("QC6_AUTH", "dev")
    assert client.post("/api/v2/auth/login", json={"email": "x", "password": "y"}).status_code == 404
