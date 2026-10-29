"""Automated documentation consistency and archive integrity tests (Part O)."""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest
import yaml

import src.settings as settings_module

REPO_ROOT = Path(__file__).resolve().parent.parent

REQUIRED_CURRENT_DOCS = (
    "README.md",
    "TEST_RESULTS.md",
    "SECURITY.md",
    "MIGRATION_GUIDE.md",
    "THIRD_PARTY_NOTICES.md",
    "docs/README.md",
    "docs/ARCHITECTURE.md",
    "docs/DEVELOPMENT.md",
    "docs/TESTING.md",
    "docs/OPERATIONS.md",
    "docs/SECURITY_BOUNDARIES.md",
    "docs/FC27_PROVIDER_INTEGRATION.md",
    "docs/FC27_ANALYTICS.md",
    "docs/FC27_APPROVAL_FLOW.md",
    "docs/FC27_TACTICAL_HYPOTHESES.md",
    "docs/FEATURE_FLAGS.md",
    "docs/FC27_DATA_MODEL.md",
    "docs/FC27_COMPONENT_STATUS.md",
    "docs/FC27_DOCUMENTATION_TRUTH_TABLE.md",
    "docs/ADR_FC27_EAST_COASTERS_PLATFORM.md",
    "docs/DISPATCH_ACTIVATION.md",
    "docs/APPROVAL_SECURITY.md",
    "docs/DEPLOYMENT_AND_SECURITY.md",
    "docs/VOICE_CAPABILITIES.md",
    "docs/EA_PLAYER_IDENTITY_VERIFICATION.md",
    "docs/MATCHPULSE_INTEGRATION_BOUNDARY.md",
    "docs/MODEL_SELECTION.md",
    "docs/ROLE_MODEL_BENCHMARK_MATRIX.md",
    "docs/WINDHOVER_INTEGRATION_GAP.md",
    "docs/FC27_EAST_COASTERS_EXPORT_PILOT_PACKAGE.md",
    "docs/notes/mcp_ws.md",
    "docs/repository_inventory.json",
    "docs/archive/README.md",
    "docs/archive/ARCHIVE_MANIFEST.md",
)

OPERATIONAL_DOCS_FORBIDDING_ARCHIVE_LINKS = (
    "docs/ARCHITECTURE.md",
    "docs/DEVELOPMENT.md",
    "docs/TESTING.md",
    "docs/OPERATIONS.md",
    "docs/SECURITY_BOUNDARIES.md",
    "docs/FC27_PROVIDER_INTEGRATION.md",
    "docs/FC27_ANALYTICS.md",
    "docs/FC27_APPROVAL_FLOW.md",
    "docs/FC27_TACTICAL_HYPOTHESES.md",
    "docs/FEATURE_FLAGS.md",
    "docs/FC27_DATA_MODEL.md",
    "SECURITY.md",
    "MIGRATION_GUIDE.md",
    "docs/APPROVAL_SECURITY.md",
    "docs/DEPLOYMENT_AND_SECURITY.md",
    "docs/DISPATCH_ACTIVATION.md",
)

MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def test_required_current_docs_exist() -> None:
    for rel_path in REQUIRED_CURRENT_DOCS:
        target = REPO_ROOT / rel_path
        assert target.is_file(), f"Missing required document: {rel_path}"
        assert target.stat().st_size > 0, f"Empty required document: {rel_path}"


def test_relative_markdown_links_in_current_docs_resolve() -> None:
    md_docs = [p for p in REQUIRED_CURRENT_DOCS if p.endswith(".md")]
    for rel_path in md_docs:
        doc_path = REPO_ROOT / rel_path
        content = doc_path.read_text(encoding="utf-8")
        for match in MARKDOWN_LINK_RE.finditer(content):
            raw_target = match.group(1).strip()
            if (
                raw_target.startswith(("http://", "https://", "mailto:", "#"))
                or "://" in raw_target
            ):
                continue
            path_part = raw_target.split("#", 1)[0].strip()
            if not path_part:
                continue
            resolved = (doc_path.parent / path_part).resolve()
            assert resolved.exists(), (
                f"Broken relative link in {rel_path}: {raw_target} -> {resolved}"
            )


def test_operational_docs_do_not_link_to_archive_as_authority() -> None:
    for rel_path in OPERATIONAL_DOCS_FORBIDDING_ARCHIVE_LINKS:
        doc_path = REPO_ROOT / rel_path
        content = doc_path.read_text(encoding="utf-8")
        for match in MARKDOWN_LINK_RE.finditer(content):
            raw_target = match.group(1).strip()
            assert "archive/" not in raw_target, (
                f"Operational document {rel_path} must not cite archive path: {raw_target}"
            )


def test_archived_documents_have_header_and_manifest_entry() -> None:
    archive_root = REPO_ROOT / "docs" / "archive"
    manifest_text = (archive_root / "ARCHIVE_MANIFEST.md").read_text(encoding="utf-8")
    archived_files = sorted(
        p
        for p in archive_root.rglob("*.md")
        if p.name not in {"README.md", "ARCHIVE_MANIFEST.md"}
    )
    assert len(archived_files) >= 18
    for archived_file in archived_files:
        rel_archive = archived_file.relative_to(REPO_ROOT).as_posix()
        text = archived_file.read_text(encoding="utf-8")
        assert text.startswith("> Archived document"), (
            f"Archived file {rel_archive} is missing standard archive header"
        )
        assert "not an authoritative" in text or "Non-authoritative" in text
        assert rel_archive in manifest_text, (
            f"Archived file {rel_archive} is not listed in docs/archive/ARCHIVE_MANIFEST.md"
        )


def test_documented_scripts_tests_flags_and_compose_services_exist() -> None:
    # Verify scripts referenced in docs exist
    expected_scripts = (
        "scripts/commissioner_decide.py",
        "scripts/fc27_capture_preflight.py",
        "scripts/fc27_club_lookup.py",
        "scripts/fc27_user_export_import.py",
        "scripts/check_matchpulse_boundary.py",
    )
    for script_rel in expected_scripts:
        assert (REPO_ROOT / script_rel).is_file(), f"Missing script {script_rel}"

    # Verify feature flags in docs/FEATURE_FLAGS.md exist in src/settings.py, config.example.env, or docker-compose.yml
    flags_doc = (REPO_ROOT / "docs" / "FEATURE_FLAGS.md").read_text(encoding="utf-8")
    settings_source = (REPO_ROOT / "src" / "settings.py").read_text(encoding="utf-8")
    config_example = (REPO_ROOT / "config.example.env").read_text(encoding="utf-8")
    compose_raw = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    expected_flags = (
        "FC27_PROVIDER_ENABLED",
        "FC27_PUBLIC_IDENTITY_LOOKUP_ENABLED",
        "FC27_UPSTREAM_COMPATIBILITY_ENABLED",
        "FC27_STANDARD_HTTP_TRANSPORT_ENABLED",
        "FC27_LIVE_INGESTION_ENABLED",
        "FC27_API_CACHE_ENABLED",
        "FC27_API_RATE_LIMIT_ENABLED",
        "FC27_API_CIRCUIT_BREAKER_ENABLED",
        "FC27_PROVIDER_DRY_RUN",
        "FC27_PERSISTENCE_ENABLED",
        "FC27_ANALYTICS_ENABLED",
        "FC27_RECOMMENDATIONS_ENABLED",
        "DISPATCH_ENABLED",
        "DISPATCH_WORKER_ENABLED",
        "APPROVAL_CLI_HMAC_ENABLED",
    )
    for flag in expected_flags:
        assert flag in flags_doc, f"Flag {flag} missing from docs/FEATURE_FLAGS.md"
        assert (
            flag in settings_source
            or flag in config_example
            or flag in compose_raw
        ), f"Flag {flag} missing from src/settings.py, config.example.env, and docker-compose.yml"
    assert settings_module.is_fc27_provider_enabled() is False
    assert settings_module.is_fc27_api_circuit_breaker_enabled() is True

    # Verify Compose services exist in docker-compose.yml
    compose_data = yaml.safe_load(
        (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    )
    services = set(compose_data.get("services", {}).keys())
    for svc in (
        "postgres",
        "redis",
        "fc27-mcp",
        "graph-api",
        "dispatch-worker",
        "outbox-relay",
        "db-bootstrap",
        "db-migrate",
    ):
        assert svc in services, f"Service {svc} missing from docker-compose.yml"


def test_security_and_architecture_invariants_in_current_docs() -> None:
    sec_doc = (REPO_ROOT / "docs" / "SECURITY_BOUNDARIES.md").read_text(encoding="utf-8")
    arch_doc = (REPO_ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    approval_doc = (REPO_ROOT / "docs" / "FC27_APPROVAL_FLOW.md").read_text(encoding="utf-8")
    tactical_doc = (REPO_ROOT / "docs" / "FC27_TACTICAL_HYPOTHESES.md").read_text(
        encoding="utf-8"
    )
    provider_doc = (REPO_ROOT / "docs" / "FC27_PROVIDER_INTEGRATION.md").read_text(
        encoding="utf-8"
    )

    # 1. HMAC described as symmetric shared-secret, never public-key
    assert "symmetric shared secret" in sec_doc.lower()
    assert "not** use a public key" in sec_doc.lower() or "never a public key" in sec_doc.lower()
    assert "shared secret" in approval_doc.lower()

    # 2. Tactical claims classified as EXTERNAL_TACTICAL_HYPOTHESIS and NOT_APPROVABLE
    assert "SOURCE_CLASSIFICATION=EXTERNAL_TACTICAL_HYPOTHESIS" in tactical_doc
    assert "RECOMMENDATION_STATUS=NOT_APPROVABLE" in tactical_doc
    assert "EAST_COASTERS_VALIDATION=NOT_RUN" in tactical_doc
    assert "CURRENT_CONFIDENCE=UNVERIFIED" in tactical_doc
    for idx in range(1, 11):
        assert f"TH-FC27-{idx:03d}" in tactical_doc

    # 3. PostgresSaver, Redis, resume-payload, and approval-vs-dispatch boundaries preserved
    assert "PostgresSaver" in sec_doc and "PostgresSaver" in arch_doc
    assert "non-authoritative" in sec_doc.lower()
    assert "Command(resume=" in sec_doc
    assert "APPROVED, NOT DISPATCHED" in approval_doc
    assert "DISPATCH_ENABLED=false" in sec_doc

    # 4. No disabled flags claimed enabled by default & no browser impersonation instructions
    assert "FC27_PROVIDER_ENABLED=false" in provider_doc
    assert "allow_browser_impersonation=False" in provider_doc


def test_repository_inventory_json_is_valid_and_metadata_only() -> None:
    inv_path = REPO_ROOT / "docs" / "repository_inventory.json"
    data = json.loads(inv_path.read_text(encoding="utf-8"))
    assert "files" in data
    files = data["files"]
    assert isinstance(files, list) and len(files) >= 100
    required_keys = {
        "path",
        "category",
        "status",
        "authoritative_role",
        "replacement_path",
        "archive_decision",
        "reason",
        "last_relevant_commit",
    }
    paths_seen = set()
    for entry in files:
        assert set(entry.keys()) == required_keys
        paths_seen.add(entry["path"])
        assert not entry["path"].startswith(".local-artifacts")
        assert not entry["path"].startswith(".local-data")
    assert "README.md" in paths_seen
    assert "docs/ARCHITECTURE.md" in paths_seen
    assert "tests/test_documentation_consistency.py" in paths_seen
