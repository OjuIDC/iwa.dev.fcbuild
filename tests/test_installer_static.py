"""Static checks for host_agent/install.ps1 (pwsh is not available in CI)."""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PS1 = (ROOT / "host_agent" / "install.ps1").read_text(encoding="utf-8")
REQS = (ROOT / "host_agent" / "requirements_windows.txt").read_text(encoding="utf-8")
AGENT = (ROOT / "host_agent" / "capture_agent.py").read_text(encoding="utf-8")

MODULE_TO_PACKAGE = {
    "cv2": "opencv-python",
    "dotenv": "python-dotenv",
    "PIL": "Pillow",
    "win32api": "pywin32",
    "win32con": "pywin32",
    "win32gui": "pywin32",
}


def _third_party_modules() -> set[str]:
    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    mods: set[str] = set()
    for node in ast.walk(ast.parse(AGENT)):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods.add(node.module.split(".")[0])
    return {m for m in mods if m not in stdlib}


def _req_names() -> set[str]:
    names = set()
    for line in REQS.splitlines():
        line = line.split("#")[0].strip()
        if line:
            names.add(re.split(r"[<>=!~\[; ]", line, maxsplit=1)[0].lower())
    return names


def test_script_is_ascii_and_braces_balanced():
    assert PS1.isascii()
    code = re.sub(r"<#.*?#>", "", PS1, flags=re.S)
    code = re.sub(r"@'.*?\n'@|@\".*?\n\"@", "", code, flags=re.S)
    code = re.sub(r"'[^'\n]*'|\"[^\"\n]*\"", "", code)
    code = re.sub(r"#.*", "", code)
    assert code.count("{") == code.count("}")
    assert code.count("(") == code.count(")")


def test_uses_psscriptroot():
    assert "$PSScriptRoot" in PS1
    assert not re.search(r'"\.\\host_agent|\'\.\\host_agent', PS1)


def test_strict_error_handling():
    assert re.search(r"\$ErrorActionPreference\s*=\s*['\"]Stop['\"]", PS1)
    assert re.search(r"Set-StrictMode\s+-Version\s+Latest", PS1)
    assert "$LASTEXITCODE" in PS1


def test_launchers_do_not_leave_repo_root():
    assert 'cd /d "%~dp0.."' not in PS1
    assert PS1.count('cd /d "%~dp0"') >= 2


def test_requirements_cover_capture_agent_imports():
    names = _req_names()
    assert "python-dotenv" in names
    for mod in _third_party_modules():
        pkg = MODULE_TO_PACKAGE.get(mod, mod).lower()
        assert pkg in names, f"{mod} -> {pkg} missing from requirements_windows.txt"
    # compatible ranges, not bare names
    for line in REQS.splitlines():
        line = line.split("#")[0].strip()
        if line:
            assert ">=" in line and "<" in line, line


def test_fallback_uses_requirements_list():
    assert "mss opencv-python" not in PS1
    assert "requirements_windows.txt" in PS1


def test_download_integrity():
    assert "Get-FileHash" in PS1
    assert re.search(r"\[switch\]\s*\$AllowUnverifiedDownload", PS1)
    assert re.search(r"\$TesseractSha256", PS1)
    assert not re.search(r"\b[0-9a-fA-F]{64}\b", PS1), "must not invent hashes"


def test_env_guarded_and_placeholder():
    m = re.search(r"if \(-not \(Test-Path \$EnvFile\)\)", PS1)
    assert m
    assert "REDIS_URL=redis://:CHANGE_ME_REDIS_PASSWORD@127.0.0.1:6379/0" in PS1
    assert "localhost:6379" not in PS1
    assert "TESSERACT_CMD=" in PS1


def test_tesseract_path_persisted():
    assert re.search(r"SetEnvironmentVariable\('Path',\s*\$new,\s*'User'\)", PS1)


def test_no_secret_printing():
    for line in PS1.splitlines():
        if re.search(r"Write-(Host|Output|Warning)", line):
            assert not re.search(r"\$\w*(password|secret|token|apikey)\w*", line, re.I), line
