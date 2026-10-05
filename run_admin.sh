#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONIOENCODING=utf-8

PY=python3
command -v "$PY" >/dev/null 2>&1 || PY=python
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "[X] Python 3.10+ is required."
  exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "[*] Creating virtual environment .venv ..."
  "$PY" -m venv .venv
fi

echo "[*] Installing dependencies ..."
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r admin/requirements.txt
# pynacl is optional – only needed to write GitHub secrets from the console
.venv/bin/python -m pip install --quiet pynacl || true

echo "[*] Starting the admin console ..."
exec .venv/bin/python admin/app.py
