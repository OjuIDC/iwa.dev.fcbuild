#!/usr/bin/env bash
# Local bootstrap for MatchPulse Live.
#
# Creates an isolated virtual environment, installs the project with its
# development extras, and runs the authoritative local validation gate.
#
# Usage: bash scripts/bootstrap.sh
#
# No secret is read, written, or required by this script.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_BIN="python"
fi

echo "==> Using interpreter: $($PYTHON_BIN --version 2>&1)"

if [ ! -d ".venv" ]; then
  echo "==> Creating .venv"
  "$PYTHON_BIN" -m venv .venv
fi

if [ -x ".venv/bin/python" ]; then
  VENV_PY=".venv/bin/python"
elif [ -x ".venv/Scripts/python.exe" ]; then
  VENV_PY=".venv/Scripts/python.exe"
else
  echo "FAIL: could not locate the virtual environment interpreter" >&2
  exit 1
fi

echo "==> Upgrading pip"
"$VENV_PY" -m pip install --upgrade pip

echo "==> Installing project and development dependencies"
"$VENV_PY" -m pip install -e ".[dev]"

echo "==> Byte-compiling sources"
"$VENV_PY" -m compileall -q src

echo "==> Ruff"
"$VENV_PY" -m ruff check src tests scripts

echo "==> Tests"
"$VENV_PY" -m pytest -q

echo "==> Coverage"
"$VENV_PY" -m pytest --cov=matchpulse --cov-report=term-missing --cov-fail-under=80

echo "==> Preflight"
"$VENV_PY" scripts/preflight.py

echo "==> Solution validation"
"$VENV_PY" scripts/validate_solution.py

echo "==> Bootstrap complete"
