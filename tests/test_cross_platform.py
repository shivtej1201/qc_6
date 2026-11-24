"""Same behaviour on Windows, macOS and Linux: read-only SQLite paths with spaces / '#' / backslashes, the shared
launcher's key and port handling, both start scripts hand over to it."""
import os
import pathlib
import sqlite3

from qc6 import launcher
from qc6.sqlite_ro import ro_uri


def test_ro_uri_handles_spaces_hash_and_windows_paths(tmp_path):
    d = tmp_path / "QC data #1 (100%)"
    d.mkdir()
    db = d / "coverage.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE t (x)")
    c.execute("INSERT INTO t VALUES (7)")
    c.commit()
    c.close()
    ro = sqlite3.connect(ro_uri(str(db)), uri=True)
    assert ro.execute("SELECT x FROM t").fetchone() == (7,)
    try:
        ro.execute("INSERT INTO t VALUES (8)")
        raise AssertionError("read-only connection wrote")
    except sqlite3.OperationalError:
        pass
    ro.close()
    # what the same call gives on Windows: a proper file URI SQLite accepts
    win = pathlib.PureWindowsPath(r"C:\Users\First Last\QC #1\runs\1\coverage.db").as_uri() + "?mode=ro"
    assert win == "file:///C:/Users/First%20Last/QC%20%231/runs/1/coverage.db?mode=ro"


def test_launcher_makes_one_key_and_keeps_it(tmp_path, monkeypatch):
    monkeypatch.delenv("QC6_SECRET_KEY", raising=False)
    monkeypatch.setenv("QC6_DATA_DIR", str(tmp_path / "var"))
    launcher.data_dir_and_key()
    k1 = os.environ["QC6_SECRET_KEY"]
    monkeypatch.delenv("QC6_SECRET_KEY")
    launcher.data_dir_and_key()
    assert os.environ["QC6_SECRET_KEY"] == k1 and len(k1) >= 40          # same key on the next start
    if os.name != "nt":
        assert oct((tmp_path / "var" / "secret.key").stat().st_mode & 0o777) == "0o600"
    monkeypatch.setenv("QC6_SECRET_KEY", "set-by-admin")
    launcher.data_dir_and_key()
    assert os.environ["QC6_SECRET_KEY"] == "set-by-admin"                 # an explicit key always wins


def test_both_start_scripts_use_the_shared_launcher():
    root = pathlib.Path(__file__).resolve().parent.parent
    bat = (root / "start.bat").read_bytes()
    sh = (root / "start.sh").read_text()
    assert b"-m qc6.launcher" in bat and "-m qc6.launcher" in sh
    assert b"\r\n" in bat and b"\n" not in bat.replace(b"\r\n", b"")      # Windows line endings in the .bat
    assert "\r" not in sh
    assert b"sys.version_info >= (3, 11)" in bat and "sys.version_info >= (3, 11)" in sh
    for word in ("QC6_DB_PASSWORD", "find_server", "migrate", "uvicorn"):    # no second copy of the startup steps
        assert word.encode() not in bat and word not in sh.split("# ---- 4.")[0] + sh.split("exec .venv/bin/python -m qc6.launcher")[1]
    assert os.access(root / "start.sh", os.X_OK) and os.access(root / "start.command", os.X_OK)
