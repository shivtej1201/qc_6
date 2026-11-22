#!/usr/bin/env bash
# QC V6 — start everything on this machine (Linux / macOS).
# Needs: Python 3.11+, PostgreSQL 16 running. Node is NOT needed (the web app is pre-built in web/dist).
#   export QC6_DATABASE_URL=postgresql://postgres:password@127.0.0.1:5432/qc6     (create the database first)
#   ./start.sh            -> http://127.0.0.1:8003
set -euo pipefail
cd "$(dirname "$0")"
: "${QC6_DATABASE_URL:?Set QC6_DATABASE_URL, e.g. postgresql://postgres:password@127.0.0.1:5432/qc6}"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
  cp requirements.txt .venv/requirements.installed
fi
if ! cmp -s requirements.txt .venv/requirements.installed; then     # an update added or changed packages
  echo "Updating QC V6 packages..."
  .venv/bin/pip install -q -r requirements.txt
  cp requirements.txt .venv/requirements.installed
fi
if [ ! -f .venv/.chromium-ok ]; then
  if .venv/bin/python -m playwright install chromium; then touch .venv/.chromium-ok
  else echo "WARNING: could not download Chromium. Page checks work; real-visitor checks and journeys need it." >&2
       echo "         Retry later with: .venv/bin/python -m playwright install chromium" >&2; fi
fi
export QC6_DATA_DIR="${QC6_DATA_DIR:-$PWD/var}"
.venv/bin/python -m qc6.db.migrate
.venv/bin/python -m qc6.workers.runner & WORKER=$!
trap 'kill $WORKER 2>/dev/null' EXIT
echo "QC V6 on http://127.0.0.1:${PORT:-8003}"
.venv/bin/python -m uvicorn qc6.api.app:app --host 127.0.0.1 --port "${PORT:-8003}"
