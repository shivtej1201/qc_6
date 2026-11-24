#!/usr/bin/env bash
# QC V6 — start everything on this machine: macOS (Apple Silicon or Intel) or Linux.
#   ./start.sh                 (on a Mac you can also double-click start.command in Finder)
# Needs: Python 3.11 or newer, and PostgreSQL running on this machine (same as start.bat on Windows). Node is NOT needed (the web app is pre-built).
#   macOS:  brew install python@3.12 postgresql@16 && brew services start postgresql@16      (or Postgres.app)
#   Linux:  your distribution's python3 (3.11+) and postgresql packages
# The database "qc6" is created on the first start. Set QC6_DATABASE_URL yourself to use another server.
# Written for bash 3.2 too (the one macOS ships), so no bash-4-only features.
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8003}"
OS="$(uname -s)"

say()  { printf '%s\n' "$*"; }
fail() { printf '\n%s\n' "$*" >&2; exit 1; }

# ---- 1. Python 3.11+ (Homebrew, python.org installer, pyenv, or the system one)
find_python() {
  local c v
  for c in python3.13 python3.12 python3.11 python3 \
           /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 \
           /usr/local/bin/python3.13 /usr/local/bin/python3.12 /usr/local/bin/python3.11 \
           /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
           /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
           /Library/Frameworks/Python.framework/Versions/3.11/bin/python3; do
    if command -v "$c" >/dev/null 2>&1; then
      v="$("$c" -c 'import sys; print(1 if sys.version_info >= (3, 11) else 0)' 2>/dev/null || echo 0)"
      if [ "$v" = "1" ]; then command -v "$c"; return 0; fi
    fi
  done
  return 1
}
if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
  PY="$(find_python)" || {
    if [ "$OS" = "Darwin" ]; then
      fail "Python 3.11 or newer wasn't found. Install it with Homebrew (brew install python@3.12) or from
https://www.python.org/downloads/macos/ , then run ./start.sh again. (The python3 that comes with macOS is too old.)"
    else
      fail "Python 3.11 or newer wasn't found. Install it with your package manager (e.g. sudo apt install python3.12 python3.12-venv), then run ./start.sh again."
    fi
  }
  say "Setting up QC V6 for the first time with $("$PY" --version) - this takes a few minutes..."
  rm -rf .venv
  "$PY" -m venv .venv || fail "Could not create the virtual environment. On Debian/Ubuntu: sudo apt install python3-venv"
  .venv/bin/python -m pip install -q --upgrade pip
  .venv/bin/python -m pip install -q -r requirements.txt || { rm -rf .venv; fail "Installing packages failed. Check your internet connection, then run ./start.sh again."; }
  cp requirements.txt .venv/requirements.installed
fi

# ---- 2. packages added or changed by an update
if ! cmp -s requirements.txt .venv/requirements.installed; then
  say "Updating QC V6 packages - a minute or two..."
  .venv/bin/python -m pip install -q -r requirements.txt || fail "Installing packages failed. Check your internet connection, then run ./start.sh again."
  cp requirements.txt .venv/requirements.installed
fi

# ---- 3. Chromium for real-visitor checks (once)
if [ ! -f .venv/.chromium-ok ]; then
  if .venv/bin/python -m playwright install chromium; then touch .venv/.chromium-ok
  else say "WARNING: could not download Chromium. Page checks work; real-visitor checks and journeys need it." >&2
       say "         Retry later with: .venv/bin/python -m playwright install chromium" >&2; fi
fi

# ---- 4. everything else is the same on every system (qc6/launcher.py, also used by start.bat on Windows):
#      find PostgreSQL (asks for the password if it needs one), data folder + encryption key, database tables,
#      the worker and the web app on http://127.0.0.1:$PORT. Ctrl+C stops both.
export PORT
exec .venv/bin/python -m qc6.launcher
