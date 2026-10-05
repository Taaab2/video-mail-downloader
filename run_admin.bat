@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONIOENCODING=utf-8

where python >nul 2>nul
if errorlevel 1 (
  echo [X] Python was not found. Install Python 3.10+ from https://python.org and try again.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [*] Creating virtual environment .venv ...
  python -m venv .venv
  if errorlevel 1 ( echo [X] Failed to create the virtual environment. & pause & exit /b 1 )
)

echo [*] Installing dependencies ...
.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
.venv\Scripts\python.exe -m pip install --quiet -r admin\requirements.txt
rem pynacl is only needed to write GitHub secrets from the console
.venv\Scripts\python.exe -m pip install --quiet pynacl >nul 2>nul

echo [*] Starting the admin console ...
.venv\Scripts\python.exe admin\app.py
pause
