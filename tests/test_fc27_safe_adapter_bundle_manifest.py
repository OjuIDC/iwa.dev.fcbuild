"""Deterministic validation tests for the tracked FC 27 Safe Adapter Bundle Manifest (Items 1-40).

Validates `config/fc27_safe_adapter_bundle_manifest.json` and the repository-native
integration without requiring `.local-artifacts/` in CI. When `.local-artifacts/` is
present locally, also verifies the local ZIP SHA-256 and extracted source hashes.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

from src import settings

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "config" / "fc27_safe_adapter_bundle_manifest.json"
THIRD_PARTY_NOTICES_PATH = REPO_ROOT / "THIRD_PARTY_NOTICES.md"
LOCAL_BUNDLE_ZIP = REPO_ROOT / ".local-artifacts" / "iwa.dev.fcbuild-safe-adapter-verified.zip"

EXPECTED_MANIFEST_VERSION = "fc27-safe-adapter-manifest-v1"
EXPECTED_BUNDLE_NAME = "iwa.dev.fcbuild-safe-adapter-verified.zip"
EXPECTED_BUNDLE_SHA256 = "eb9c42745f9966aa6af197c1b289809575c703d0d4ccef05e4c9678370f301a8"
EXPECTED_UPSTREAM_REPO = "https://github.com/1erkandogan/fc27-clubs-api"
EXPECTED_UPSTREAM_COMMIT = "26a5ff12ea793042bd776a858c74f3f44dd427f5"
EXPECTED_UPSTREAM_VERSION = "0.3.1"
EXPECTED_UPSTREAM_LICENSE = "MIT"

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|PENDING)$")


def _load_manifest() -> dict[str, Any]:
    raw = MANIFEST_PATH.read_text(encoding="utf-8")
    return json.loads(raw)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_01_manifest_exists() -> None:
    assert MANIFEST_PATH.is_file()


def test_02_manifest_parses_as_strict_json() -> None:
    raw = MANIFEST_PATH.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert isinstance(parsed, dict)


def test_03_manifest_version_correct() -> None:
    m = _load_manifest()
    assert m["manifest_version"] == EXPECTED_MANIFEST_VERSION


def test_04_bundle_name_correct() -> None:
    m = _load_manifest()
    assert m["bundle_name"] == EXPECTED_BUNDLE_NAME


def test_05_bundle_sha256_correct() -> None:
    m = _load_manifest()
    assert m["bundle_sha256"] == EXPECTED_BUNDLE_SHA256
    assert SHA256_RE.match(m["bundle_sha256"])
    if LOCAL_BUNDLE_ZIP.is_file():
        assert _sha256_file(LOCAL_BUNDLE_ZIP) == EXPECTED_BUNDLE_SHA256


def test_06_upstream_repository_correct() -> None:
    m = _load_manifest()
    assert m["upstream_repository"] == EXPECTED_UPSTREAM_REPO


def test_07_upstream_commit_correct() -> None:
    m = _load_manifest()
    assert m["upstream_commit"] == EXPECTED_UPSTREAM_COMMIT


def test_08_upstream_version_correct() -> None:
    m = _load_manifest()
    assert m["upstream_version"] == EXPECTED_UPSTREAM_VERSION


def test_09_upstream_license_correct() -> None:
    m = _load_manifest()
    assert m["upstream_license"] == EXPECTED_UPSTREAM_LICENSE


def test_10_every_adopted_component_has_a_destination() -> None:
    m = _load_manifest()
    assert len(m["components"]) >= 7
    for comp in m["components"]:
        dest = comp.get("repository_destination")
        assert isinstance(dest, str) and dest.strip()


def test_11_every_destination_exists() -> None:
    m = _load_manifest()
    for comp in m["components"]:
        dest_path = REPO_ROOT / comp["repository_destination"]
        assert dest_path.is_file(), f"Missing destination: {comp['repository_destination']}"


def test_12_every_adopted_source_has_a_source_hash() -> None:
    m = _load_manifest()
    for comp in m["components"]:
        assert SHA256_RE.match(str(comp.get("source_sha256", "")))


def test_13_every_destination_has_a_repository_hash() -> None:
    m = _load_manifest()
    for comp in m["components"]:
        assert SHA256_RE.match(str(comp.get("repository_sha256", "")))


def test_14_every_adopted_component_lists_tests() -> None:
    m = _load_manifest()
    for comp in m["components"]:
        tests = comp.get("tests")
        assert isinstance(tests, list) and len(tests) >= 1
        for t in tests:
            assert (REPO_ROOT / t).is_file(), f"Missing test file: {t}"


def test_15_every_adopted_component_lists_feature_flags() -> None:
    m = _load_manifest()
    for comp in m["components"]:
        flags = comp.get("feature_flags")
        assert isinstance(flags, list) and len(flags) >= 1


def test_16_enabled_by_default_is_false_where_applicable() -> None:
    m = _load_manifest()
    for comp in m["components"]:
        assert comp.get("enabled_by_default") is False


def test_17_circuit_breaker_default_documented_correctly() -> None:
    m = _load_manifest()
    defaults = m["feature_flag_defaults"]
    assert defaults["FC27_API_CIRCUIT_BREAKER_ENABLED"] is True
    rate_comp = next(
        c for c in m["components"] if c["logical_component_name"] == "fc27_api_rate_limit"
    )
    assert rate_comp["circuit_breaker_enabled_by_default"] is True


def test_18_rejected_components_have_reasons() -> None:
    m = _load_manifest()
    assert len(m["rejected_components"]) >= 7
    for rej in m["rejected_components"]:
        assert isinstance(rej.get("component"), str) and rej["component"].strip()
        assert isinstance(rej.get("source_path"), str) and rej["source_path"].strip()
        assert isinstance(rej.get("rejection_reason"), str) and len(rej["rejection_reason"]) >= 10
        assert isinstance(rej.get("prohibited_capability"), str) and rej["prohibited_capability"].strip()
        assert isinstance(rej.get("replacement"), str) and rej["replacement"].strip()


def _rejected_capabilities() -> set[str]:
    m = _load_manifest()
    return {r["prohibited_capability"] for r in m["rejected_components"]}


def test_19_browser_transport_rejected() -> None:
    assert "browser_transport" in _rejected_capabilities()


def test_20_browser_headers_rejected() -> None:
    assert "browser_headers" in _rejected_capabilities()


def test_21_tls_impersonation_rejected() -> None:
    assert "tls_impersonation" in _rejected_capabilities()


def test_22_cookies_rejected() -> None:
    assert "cookies" in _rejected_capabilities()


def test_23_sessions_rejected() -> None:
    assert "sessions" in _rejected_capabilities()


def test_24_arbitrary_urls_rejected() -> None:
    assert "arbitrary_urls" in _rejected_capabilities()


def test_25_raw_response_storage_rejected() -> None:
    assert "raw_response_storage" in _rejected_capabilities()


def test_26_no_absolute_paths() -> None:
    raw = MANIFEST_PATH.read_text(encoding="utf-8")
    assert "C:\\" not in raw and "c:\\" not in raw
    assert "/Users/" not in raw and "/home/" not in raw


def test_27_no_local_usernames() -> None:
    raw = MANIFEST_PATH.read_text(encoding="utf-8").lower()
    assert "iwadev" not in raw


def test_28_no_secret_values() -> None:
    raw = MANIFEST_PATH.read_text(encoding="utf-8").lower()
    for forbidden in ("bearer ", "sk-", "password=", "secret_key=", "api_key=", "set-cookie:"):
        assert forbidden not in raw


def test_29_no_local_artifacts_runtime_import() -> None:
    for py_file in (REPO_ROOT / "src").rglob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        assert ".local-artifacts" not in content, f"{py_file} references .local-artifacts"


def test_30_no_zip_runtime_import() -> None:
    for py_file in (REPO_ROOT / "src").rglob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        assert "zipimport" not in content, f"{py_file} uses zipimport"
        tree = ast.parse(content, filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name not in ("zipimport", "zipfile"), f"{py_file} imports {alias.name}"
            elif isinstance(node, ast.ImportFrom):
                assert node.module not in ("zipimport", "zipfile"), f"{py_file} imports from {node.module}"
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.endswith(".zip") and node.value != "iwa.dev.fcbuild-safe-adapter-verified.zip":
                    raise AssertionError(f"{py_file} contains runtime .zip path literal: {node.value}")


def test_31_no_duplicate_destinations() -> None:
    m = _load_manifest()
    dests = [c["repository_destination"] for c in m["components"]]
    assert len(dests) == len(set(dests))


def test_32_no_duplicate_logical_components() -> None:
    m = _load_manifest()
    names = [c["logical_component_name"] for c in m["components"]]
    assert len(names) == len(set(names))


def test_33_third_party_notice_exists() -> None:
    assert THIRD_PARTY_NOTICES_PATH.is_file()


def test_34_mit_attribution_recorded() -> None:
    txt = THIRD_PARTY_NOTICES_PATH.read_text(encoding="utf-8")
    assert "MIT License" in txt
    assert "fc27-clubs-api" in txt
    assert EXPECTED_UPSTREAM_COMMIT in txt


def test_35_manifest_matches_current_tracked_files() -> None:
    m = _load_manifest()
    for comp in m["components"]:
        dest_path = REPO_ROOT / comp["repository_destination"]
        actual_sha = _sha256_file(dest_path)
        assert actual_sha == comp["repository_sha256"], (
            f"SHA-256 mismatch for {comp['repository_destination']}: "
            f"expected {comp['repository_sha256']}, got {actual_sha}"
        )


def test_36_manifest_hashes_deterministic() -> None:
    h1 = _sha256_file(MANIFEST_PATH)
    h2 = _sha256_file(MANIFEST_PATH)
    assert h1 == h2 and SHA256_RE.match(h1)


def test_37_manifest_includes_repository_integration_commit_or_pending() -> None:
    m = _load_manifest()
    assert COMMIT_RE.match(str(m.get("integration_commit", "")))


def test_38_runtime_modules_do_not_import_manifest_to_grant_permissions() -> None:
    for py_file in (REPO_ROOT / "src").rglob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        assert "fc27_safe_adapter_bundle_manifest.json" not in content


def test_39_feature_defaults_remain_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "FC27_PROVIDER_ENABLED",
        "FC27_LIVE_INGESTION_ENABLED",
        "FC27_API_CACHE_ENABLED",
        "FC27_API_RATE_LIMIT_ENABLED",
        "FC27_API_CIRCUIT_BREAKER_ENABLED",
        "FC27_PERSISTENCE_ENABLED",
        "FC27_ANALYTICS_ENABLED",
        "FC27_RECOMMENDATIONS_ENABLED",
        "DISPATCH_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)
    assert settings.is_fc27_provider_enabled() is False
    assert settings.is_fc27_live_ingestion_enabled() is False
    assert settings.is_fc27_api_cache_enabled() is False
    assert settings.is_fc27_api_rate_limit_enabled() is False
    assert settings.is_fc27_api_circuit_breaker_enabled() is True
    assert settings.is_fc27_persistence_enabled() is False
    assert settings.is_fc27_analytics_enabled() is False
    assert settings.is_fc27_recommendations_enabled() is False


def test_40_matchpulse_boundary_unchanged() -> None:
    from scripts.check_matchpulse_boundary import main as check_boundary

    assert check_boundary() == 0
