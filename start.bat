@echo off
setlocal
REM QC V6 - start on Windows (double-click, or run .\start.bat in PowerShell / Command Prompt).
REM Same steps as start.sh on macOS / Linux: this file only finds Python and installs packages; the rest
REM (PostgreSQL, data folder, encryption key, database tables, worker + web app) is qc6\launcher.py, shared by both.
REM Needs: Python 3.11+ (python.org, tick "Add python.exe to PATH") and PostgreSQL 16 (postgresql.org/download/windows).
REM Node is NOT needed: the web app is already built. Another port: set PORT=8004 before start.bat.
cd /d "%~dp0"

REM ---- 1. Python 3.11 or newer (python.exe on PATH, or the py launcher)
set "PY="
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul && set "PY=python"
if not defined PY ( py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul && set "PY=py -3" )
if not defined PY (
  echo Python 3.11 or newer was not found. Install it from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" during installation. Then run start.bat again.
  pause & exit /b 1
)

REM ---- 2. first start: virtual environment and packages (a venv from an older Python is made again)
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul || rmdir /s /q .venv
)
if exist .venv\Scripts\python.exe goto have_venv
echo Setting up QC V6 for the first time - this takes a few minutes...
%PY% -m venv .venv
if errorlevel 1 (echo Could not create the virtual environment. & pause & exit /b 1)
.venv\Scripts\python -m pip install -q --upgrade pip
.venv\Scripts\python -m pip install -q -r requirements.txt
if errorlevel 1 (echo Installing packages failed. Check your internet connection. & rmdir /s /q .venv & pause & exit /b 1)
copy /y requirements.txt .venv\requirements.installed >nul
:have_venv

REM ---- 3. packages added or changed by an update
fc /b requirements.txt .venv\requirements.installed >nul 2>nul
if errorlevel 1 (
  echo Updating QC V6 packages - a minute or two...
  .venv\Scripts\python -m pip install -q -r requirements.txt
  if errorlevel 1 (echo Installing packages failed. Check your internet connection, then run start.bat again. & pause & exit /b 1)
  copy /y requirements.txt .venv\requirements.installed >nul
)

REM ---- 4. Chromium for real-visitor checks (once)
if exist .venv\.chromium-ok goto have_chromium
.venv\Scripts\python -m playwright install chromium
if errorlevel 1 (echo WARNING: Chromium could not be downloaded. Page checks work; real-visitor checks and journeys need it.) else (type nul > .venv\.chromium-ok)
:have_chromium

REM ---- 5. the rest, the same as on macOS / Linux
.venv\Scripts\python -m qc6.launcher
if errorlevel 1 pause
