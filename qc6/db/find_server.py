"""Find a local PostgreSQL for the start scripts (`python -m qc6.db.find_server`).

Tries the usual first-install setups, in order, against 127.0.0.1:5432:
  * your own login name with no password (Homebrew `postgresql@16`, Postgres.app on macOS),
  * `postgres` with no password (some Linux packages, Docker with trust auth),
  * `postgres` with QC6_DB_PASSWORD (the Windows installer and most servers).
Prints the address QC should use (never the password: that stays in QC6_DB_PASSWORD) and exits 0.
Exit 2: nothing answers on the port (PostgreSQL isn't running). Exit 3: it answers but wants a password.
"""
from __future__ import annotations

import asyncio
import getpass
import os
import sys
from typing import List, Optional, Tuple

HOST = os.environ.get("QC6_DB_HOST", "127.0.0.1")
PORT = int(os.environ.get("QC6_DB_PORT", "5432"))
DB = os.environ.get("QC6_DB_NAME", "qc6")


async def _try(user: str, password: Optional[str]) -> str:
    import asyncpg
    try:
        conn = await asyncpg.connect(host=HOST, port=PORT, user=user, password=password, database="postgres", timeout=4)
        await conn.close()
        return "ok"
    except (OSError, asyncio.TimeoutError):
        return "down"
    except asyncpg.InvalidPasswordError:
        return "auth"
    except asyncpg.InvalidAuthorizationSpecificationError:
        return "auth"                      # no such role / not allowed
    except asyncpg.PostgresError:
        return "auth"


async def main() -> int:
    pw = os.environ.get("QC6_DB_PASSWORD") or None
    me = os.environ.get("USER") or getpass.getuser()
    tries: List[Tuple[str, Optional[str]]] = [(me, None), ("postgres", None)]
    if pw:
        tries = [("postgres", pw), (me, pw)] + tries
    seen = set()
    results = []
    for user, password in tries:
        if (user, password) in seen:
            continue
        seen.add((user, password))
        r = await _try(user, password)
        results.append(r)
        if r == "ok":
            print(f"postgresql://{user}@{HOST}:{PORT}/{DB}")
            return 0
        if r == "down":
            return 2
    return 3


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
