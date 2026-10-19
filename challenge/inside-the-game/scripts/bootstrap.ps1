# Local bootstrap for MatchPulse Live.
#
# Creates an isolated virtual environment, installs the project with its
# development extras, and runs the authoritative local validation gate.
#
# Usage: powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1
#
# No secret is read, written, or required by this script.
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root

$python = $env:PYTHON_BIN
if (-not $python) {
    $python = if (Get-Command py -ErrorAction SilentlyContinue) { 'py' } else { 'python' }
}

Write-Output "==> Using interpreter: $python"
& $python --version

if (-not (Test-Path -LiteralPath '.venv')) {
    Write-Output '==> Creating .venv'
    & $python -m venv .venv
}

$venvPython = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Error 'Could not locate the virtual environment interpreter'
    exit 1
}

Write-Output '==> Upgrading pip'
& $venvPython -m pip install --upgrade pip

Write-Output '==> Installing project and development dependencies'
& $venvPython -m pip install -e '.[dev]'

Write-Output '==> Byte-compiling sources'
& $venvPython -m compileall -q src

Write-Output '==> Ruff'
& $venvPython -m ruff check src tests scripts

Write-Output '==> Tests'
& $venvPython -m pytest -q

Write-Output '==> Coverage'
& $venvPython -m pytest --cov=matchpulse --cov-report=term-missing --cov-fail-under=80

Write-Output '==> Preflight'
& $venvPython scripts/preflight.py

Write-Output '==> Solution validation'
& $venvPython scripts/validate_solution.py

Write-Output '==> Bootstrap complete'
