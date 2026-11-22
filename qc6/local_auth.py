"""Built-in sign-in (QC6_AUTH=local), for running QC without an identity provider. CrawlPulse FEAT-20-03.

* Passwords: Argon2id (64 MiB, t=3, p=4); older scrypt hashes still verify and are upgraded at the next sign-in.
* Sessions: 32 random bytes in an HttpOnly, SameSite=Strict cookie (Secure unless QC6_COOKIE_SECURE=false for plain
  http on your own machine). The database keeps only the token's SHA-256. 12 h idle, 7 days at most; sign-out ends it.
* Two-factor: TOTP (RFC 6238: SHA-1, 6 digits, 30 s, +-1 step), secret encrypted with QC6_SECRET_KEY, a code can't be
  used twice; 10 single-use recovery codes (only their hashes are kept).
* Abuse: 5 wrong passwords lock the account for 15 minutes; at most 20 attempts per address per 10 minutes. The same
  message for unknown e-mail and wrong password.
* CSRF: every request that changes something must carry the X-QC-Requested-With header, which other sites can't send.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import struct
import time
import urllib.parse
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

COOKIE = "qc6_session"
IDLE = timedelta(hours=12)
MAX_AGE = timedelta(days=7)
PENDING = timedelta(minutes=10)                # password ok, waiting for the two-factor code
MIN_PASSWORD = 10
LOCK_AFTER, LOCK_FOR = 5, timedelta(minutes=15)
_ip_hits: Dict[str, deque] = defaultdict(deque)


class AuthFailed(Exception):
    def __init__(self, status: int, title: str, detail: str = ""):
        super().__init__(title)
        self.status, self.title, self.detail = status, title, detail


# ------------------------------------------------------------------ passwords
_PH = None


def _ph():
    """Argon2id (CrawlPulse backend §: memory-hard, the OWASP first choice): 64 MiB, 3 passes, 4 lanes."""
    global _PH
    if _PH is None:
        from argon2 import PasswordHasher
        _PH = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=4)
    return _PH


def hash_password(pw: str) -> str:
    if len(pw or "") < MIN_PASSWORD:
        raise AuthFailed(400, "Choose a longer password", f"Use at least {MIN_PASSWORD} characters; a short sentence works well.")
    return _ph().hash(pw)


def _scrypt(pw: bytes, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(pw, salt=salt, n=n, r=r, p=p, maxmem=64 * 1024 * 1024, dklen=32)


def check_password(pw: str, stored: Optional[str]) -> bool:
    """Argon2id hashes, and scrypt hashes made by earlier versions (upgraded on the next sign-in, see needs_rehash)."""
    from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
    if stored and stored.startswith("$argon2"):
        try:
            return _ph().verify(stored, pw or "")
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False
    if stored and stored.startswith("scrypt$"):
        _, n, r, p, salt, h = stored.split("$")
        got = _scrypt((pw or "").encode(), base64.b64decode(salt), 2 ** int(n), int(r), int(p))
        return hmac.compare_digest(got, base64.b64decode(h))
    try:                                                # unknown e-mail: spend the same time as a real check
        _ph().verify(_dummy(), pw or "")
    except Exception:
        pass
    return False


def needs_rehash(stored: Optional[str]) -> bool:
    return bool(stored) and (not stored.startswith("$argon2") or _ph().check_needs_rehash(stored))


_DUMMY: Optional[str] = None


def _dummy() -> str:
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = _ph().hash(secrets.token_urlsafe(16))
    return _DUMMY


# ------------------------------------------------------------------ TOTP (RFC 6238)
def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_at(secret: str, step: int, digits: int = 6) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    mac = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    o = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def totp_check(secret: str, code: str, last_step: Optional[int], now: Optional[float] = None) -> Optional[int]:
    """The matching time step (within +-1, newer than last_step), or None."""
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(code) != 6:
        return None
    step = int((now or time.time()) // 30)
    for s in (step - 1, step, step + 1):
        if (last_step is None or s > last_step) and hmac.compare_digest(totp_at(secret, s), code):
            return s
    return None


def otpauth_uri(secret: str, email: str, issuer: str = "QC V6") -> str:
    return (f"otpauth://totp/{urllib.parse.quote(issuer)}:{urllib.parse.quote(email)}"
            f"?secret={secret}&issuer={urllib.parse.quote(issuer)}&algorithm=SHA1&digits=6&period=30")


def new_recovery_codes(n: int = 10) -> Tuple[List[str], List[str]]:
    codes = [f"{secrets.token_hex(3)}-{secrets.token_hex(3)}" for _ in range(n)]
    return codes, [hashlib.sha256(c.encode()).hexdigest() for c in codes]


def _seal(v: str) -> str:
    from qc6.site_access import seal
    return seal({"totp": v})


def _unseal(v: str) -> str:
    from qc6.site_access import unseal
    return unseal(v)["totp"]


# ------------------------------------------------------------------ sessions
def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _rate(ip: str) -> None:
    q = _ip_hits[ip or "?"]
    now = time.time()
    while q and now - q[0] > 600:
        q.popleft()
    if len(q) >= 20:
        raise AuthFailed(429, "Too many sign-in attempts", "Wait ten minutes, then try again.")
    q.append(now)


async def login(pool, email: str, password: str, ip: str, ua: str) -> Dict[str, Any]:
    """Password step. Returns {"token", "two_factor": bool}; with two-factor on, the session waits for the code."""
    _rate(ip)
    m = await pool.fetchrow("SELECT * FROM members WHERE lower(email) = lower($1) ORDER BY id LIMIT 1", (email or "").strip())
    now = datetime.now(timezone.utc)
    if m and m["locked_until"] and m["locked_until"] > now:
        raise AuthFailed(429, "This account is locked for a few minutes", "Too many wrong passwords. Try again in 15 minutes.")
    if not m or not check_password(password, m["password_hash"]):
        if m:
            n = m["failed_logins"] + 1
            await pool.execute("UPDATE members SET failed_logins = $2, locked_until = $3 WHERE id = $1", m["id"],
                               0 if n >= LOCK_AFTER else n, now + LOCK_FOR if n >= LOCK_AFTER else None)
        raise AuthFailed(401, "Wrong e-mail or password")
    await pool.execute("UPDATE members SET failed_logins = 0, locked_until = NULL WHERE id = $1", m["id"])
    if needs_rehash(m["password_hash"]):             # an older scrypt hash: upgrade to Argon2id now that we have the password
        await pool.execute("UPDATE members SET password_hash = $2 WHERE id = $1", m["id"], _ph().hash(password))
    token = secrets.token_urlsafe(32)
    two = bool(m["totp_secret"])
    await pool.execute("""INSERT INTO sessions (token_hash, member_id, two_factor_ok, expires_at, ip, user_agent)
                          VALUES ($1,$2,$3,$4,$5,$6)""", _hash(token), m["id"], not two, now + (PENDING if two else MAX_AGE), ip, (ua or "")[:300])
    return {"token": token, "two_factor": two, "member_id": m["id"]}


async def second_factor(pool, token: str, code: str) -> Dict[str, Any]:
    s = await pool.fetchrow("""SELECT s.*, m.totp_secret, m.totp_last_step, m.recovery_codes FROM sessions s JOIN members m ON m.id = s.member_id
                               WHERE s.token_hash = $1 AND s.expires_at > now()""", _hash(token or ""))
    if s is None:
        raise AuthFailed(401, "Sign in again", "The sign-in took too long.")
    if s["two_factor_ok"]:
        return {"ok": True}
    secret = _unseal(s["totp_secret"])
    step = totp_check(secret, code, s["totp_last_step"])
    if step is not None:
        await pool.execute("UPDATE members SET totp_last_step = $2 WHERE id = $1", s["member_id"], step)
    else:
        h = hashlib.sha256((code or "").strip().lower().encode()).hexdigest()
        left = list(s["recovery_codes"] or [])
        if h not in left:
            raise AuthFailed(401, "That code isn't right", "Use the 6-digit code from your authenticator app, or a recovery code.")
        left.remove(h)
        await pool.execute("UPDATE members SET recovery_codes = $2 WHERE id = $1", s["member_id"], left)
    await pool.execute("UPDATE sessions SET two_factor_ok = true, expires_at = $2 WHERE token_hash = $1",
                       _hash(token), datetime.now(timezone.utc) + MAX_AGE)
    return {"ok": True, "recovery_codes_left": None if step is not None else len(left)}


async def member_for(pool, token: Optional[str]) -> Optional[str]:
    """E-mail of the member behind a fully signed-in session (both steps), sliding the idle timeout."""
    if not token:
        return None
    s = await pool.fetchrow("""SELECT s.token_hash, s.last_seen, m.email FROM sessions s JOIN members m ON m.id = s.member_id
                               WHERE s.token_hash = $1 AND s.two_factor_ok AND s.expires_at > now()""", _hash(token))
    if s is None:
        return None
    if s["last_seen"] < datetime.now(timezone.utc) - IDLE:
        await pool.execute("DELETE FROM sessions WHERE token_hash = $1", s["token_hash"])
        return None
    await pool.execute("UPDATE sessions SET last_seen = now() WHERE token_hash = $1", s["token_hash"])
    return s["email"]


async def logout(pool, token: Optional[str]) -> None:
    if token:
        await pool.execute("DELETE FROM sessions WHERE token_hash = $1", _hash(token))


# ------------------------------------------------------------------ account changes
async def set_password(pool, member_id: int, new: str, end_other_sessions_except: Optional[str] = None) -> None:
    await pool.execute("UPDATE members SET password_hash = $2, failed_logins = 0, locked_until = NULL WHERE id = $1",
                       member_id, hash_password(new))
    await pool.execute("DELETE FROM sessions WHERE member_id = $1 AND token_hash IS DISTINCT FROM $2", member_id,
                       _hash(end_other_sessions_except) if end_other_sessions_except else None)


async def start_two_factor(pool, member_id: int, email: str) -> Dict[str, str]:
    secret = new_totp_secret()
    await pool.execute("UPDATE members SET totp_secret = NULL, recovery_codes = NULL WHERE id = $1", member_id)
    return {"secret": secret, "uri": otpauth_uri(secret, email)}


async def enable_two_factor(pool, member_id: int, secret: str, code: str) -> List[str]:
    if totp_check(secret, code, None) is None:
        raise AuthFailed(400, "That code isn't right", "Type the 6-digit code your authenticator app shows for QC now.")
    codes, hashes = new_recovery_codes()
    await pool.execute("UPDATE members SET totp_secret = $2, recovery_codes = $3, totp_last_step = NULL WHERE id = $1",
                       member_id, _seal(secret), hashes)
    return codes


async def disable_two_factor(pool, member_id: int) -> None:
    await pool.execute("UPDATE members SET totp_secret = NULL, recovery_codes = NULL, totp_last_step = NULL WHERE id = $1", member_id)


# ------------------------------------------------------------------ forgot password (e-mail)
RESET_FOR = timedelta(minutes=30)


def mail_configured() -> bool:
    return bool(os.environ.get("QC6_SMTP_HOST") and os.environ.get("QC6_PUBLIC_URL"))


def _send_mail(to: str, subject: str, text: str) -> None:
    """Plain SMTP with STARTTLS (QC6_SMTP_HOST, QC6_SMTP_PORT=587, QC6_SMTP_USER, QC6_SMTP_PASSWORD, QC6_MAIL_FROM)."""
    import smtplib
    import ssl
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["From"] = os.environ.get("QC6_MAIL_FROM") or os.environ.get("QC6_SMTP_USER") or "qc@localhost"
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    port = int(os.environ.get("QC6_SMTP_PORT", "587"))
    with smtplib.SMTP(os.environ["QC6_SMTP_HOST"], port, timeout=20) as s:
        if port != 25 or os.environ.get("QC6_SMTP_STARTTLS", "true").lower() != "false":
            s.starttls(context=ssl.create_default_context())
        if os.environ.get("QC6_SMTP_USER"):
            s.login(os.environ["QC6_SMTP_USER"], os.environ.get("QC6_SMTP_PASSWORD", ""))
        s.send_message(msg)


_mail = _send_mail          # tests replace this


async def request_reset(pool, email: str, ip: str) -> None:
    """Always looks the same from outside (no way to learn which e-mails have accounts). With a matching account and
    mail configured, sends a one-time link valid for 30 minutes."""
    _rate(ip)
    m = await pool.fetchrow("SELECT id, email FROM members WHERE lower(email) = lower($1) ORDER BY id LIMIT 1", (email or "").strip())
    if m is None or not mail_configured():
        hashlib.scrypt(b"x", salt=b"0" * 16, n=2 ** 14, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)    # similar time
        return
    token = secrets.token_urlsafe(32)
    await pool.execute("DELETE FROM password_resets WHERE member_id = $1 OR expires_at < now()", m["id"])
    await pool.execute("INSERT INTO password_resets (token_hash, member_id, expires_at) VALUES ($1, $2, $3)",
                       _hash(token), m["id"], datetime.now(timezone.utc) + RESET_FOR)
    link = os.environ["QC6_PUBLIC_URL"].rstrip("/") + "/#/reset?token=" + urllib.parse.quote(token)
    import asyncio
    await asyncio.to_thread(_mail, m["email"], "Reset your QC password",
                            "Someone (hopefully you) asked to reset your QC password.\n\n"
                            f"Choose a new one here within 30 minutes:\n{link}\n\n"
                            "If it wasn't you, ignore this e-mail: your password stays as it is.")


async def reset_password(pool, token: str, new: str) -> None:
    hash_password(new)                                    # a too-short password must not use up the link
    row = await pool.fetchrow("DELETE FROM password_resets WHERE token_hash = $1 RETURNING member_id, expires_at", _hash(token or ""))
    if row is None or row["expires_at"] < datetime.now(timezone.utc):
        raise AuthFailed(400, "This reset link can't be used", "It was used already or is older than 30 minutes. Ask for a new one.")
    await set_password(pool, row["member_id"], new)        # also ends every session of that account
