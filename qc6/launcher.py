"""Start QC on this machine: the same steps on Windows, macOS and Linux (`python -m qc6.launcher`).

start.bat (Windows) and start.sh / start.command (macOS, Linux) only find Python, make the virtual environment and
install packages; everything after that is here, so the two can't drift apart:

  1. the database: QC6_DATABASE_URL if set, else the local PostgreSQL (your login without a password, "postgres"
     without one, then "postgres" with a password typed here, not saved)
  2. the data folder (QC6_DATA_DIR, default ./var) and the encryption key (QC6_SECRET_KEY, else one made once and kept
     in <data>/secret.key) that seals the SMTP password and staging logins
  3. the database tables (migrations)
  4. the worker (checks queue here) and the web app on http://127.0.0.1:<PORT> (default 8003), opened in the browser
  5. Ctrl+C (or closing the window) stops both; the worker hands a running check back to the queue first
"""
from __future__ import annotations

import asyncio
import getpass
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def say(msg: str = "") -> None:
    print(msg, flush=True)


def fail(msg: str) -> None:
    print("\n" + msg, file=sys.stderr, flush=True)
    sys.exit(1)


def _mac() -> bool:
    return sys.platform == "darwin"


def _win() -> bool:
    return os.name == "nt"


def find_database() -> None:
    if os.environ.get("QC6_DATABASE_URL"):
        return
    from qc6.db import find_server
    import contextlib
    import io

    def attempt() -> tuple:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = asyncio.run(find_server.main())
        return rc, buf.getvalue().strip()
    rc, url = attempt()
    if rc == 3 and not os.environ.get("QC6_DB_PASSWORD"):
        say("")
        say('PostgreSQL is running but wants a password. Enter the password of the "postgres" user')
        say("(the one you chose when you installed PostgreSQL). It is used for this run only, not saved.")
        os.environ["QC6_DB_PASSWORD"] = getpass.getpass("PostgreSQL password: ")
        rc, url = attempt()
    if rc == 0:
        os.environ["QC6_DATABASE_URL"] = url
        return
    if rc == 2:
        if _win():
            fail("PostgreSQL isn't running on this PC. Install PostgreSQL 16 from https://www.postgresql.org/download/windows/ "
                 "(it starts by itself as a Windows service), or start the \"postgresql-x64-16\" service in Services. "
                 "Then run start.bat again.")
        if _mac():
            fail("PostgreSQL isn't running on this Mac. Either:\n  brew install postgresql@16 && brew services start postgresql@16\n"
                 "or install Postgres.app (https://postgresapp.com), open it and press Initialize. Then run ./start.sh again.")
        fail("PostgreSQL isn't running. Install and start it (e.g. sudo apt install postgresql && sudo systemctl start postgresql),\n"
             "or set QC6_DATABASE_URL to your server. Then run ./start.sh again.")
    fail("PostgreSQL didn't accept that password. Start QC again, or set QC6_DATABASE_URL (and QC6_DB_PASSWORD) yourself,\n"
         "e.g. postgresql://postgres@127.0.0.1:5432/qc6")


def data_dir_and_key() -> None:
    d = os.path.abspath(os.environ.get("QC6_DATA_DIR") or os.path.join(HERE, "var"))
    os.makedirs(d, exist_ok=True)
    os.environ["QC6_DATA_DIR"] = d
    if os.environ.get("QC6_SECRET_KEY"):
        return
    path = os.path.join(d, "secret.key")
    if not os.path.exists(path):
        key = secrets.token_urlsafe(48)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(key)
        say(f"Made an encryption key for saved passwords: {path} (keep it with the data folder; back it up with it).")
    with open(path, encoding="ascii") as f:
        os.environ["QC6_SECRET_KEY"] = f.read().strip()


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def migrate() -> None:
    from qc6.db.migrate import _explain, migrate as run
    try:
        applied = asyncio.run(run())
        if applied:
            say("Database updated: " + ", ".join(applied))
    except Exception as e:                                   # noqa: BLE001 - one plain sentence on first start
        fail(_explain(e, os.environ.get("QC6_DATABASE_URL", "")))


def main() -> None:
    if sys.version_info < (3, 11):
        fail(f"QC needs Python 3.11 or newer; this is {sys.version.split()[0]}.")
    os.chdir(HERE)
    try:
        port = int(os.environ.get("QC6_PORT") or os.environ.get("PORT") or "8003")
    except ValueError:
        fail("PORT should be a number, e.g. 8003.")
    find_database()
    data_dir_and_key()
    if not port_free(port):
        fail(f"Something is already using port {port} (another QC window?). Close it, or start with another port: "
             + ("set PORT=8004 then start.bat" if _win() else "PORT=8004 ./start.sh"))
    migrate()

    # the worker in its own process (same Python, same settings); new process group on Windows so Ctrl+C reaches it
    # through us, in order
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if _win() else {}
    worker = subprocess.Popen([sys.executable, "-m", "qc6.workers.runner"], cwd=HERE, env=os.environ.copy(), **kw)
    url = f"http://127.0.0.1:{port}"
    say("")
    say(f"QC V6 is running: {url}   (Ctrl+C or close this window to stop)")
    threading.Timer(2.0, lambda: webbrowser.open(url)).start() if os.environ.get("QC6_NO_BROWSER") != "1" else None

    def stop_worker() -> None:
        if worker.poll() is not None:
            return
        try:
            worker.send_signal(signal.CTRL_BREAK_EVENT if _win() else signal.SIGTERM)   # hand the check back, then exit
            worker.wait(timeout=30)
        except Exception:                                     # noqa: BLE001
            worker.kill()
    try:
        import uvicorn
        uvicorn.run("qc6.api.app:app", host="127.0.0.1", port=port, log_level="info")
    finally:
        stop_worker()


if __name__ == "__main__":
    main()
