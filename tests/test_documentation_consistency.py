"""Deterministic documentation consistency validator unit and integration tests (DC21 & DC22)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import socket
import subprocess
import urllib.request

import pytest

import scripts.check_documentation_consistency as dc
import src.settings as settings_module

REPO_ROOT = Path(__file__).resolve().parent.parent


def _base_policy() -> dict:
    return {
        "policy_version": "fc27-documentation-consistency-v1",
        "current_document_roots": [
            "README.md",
            "TEST_RESULTS.md",
            "SECURITY.md",
            "MIGRATION_GUIDE.md",
            "THIRD_PARTY_NOTICES.md",
            "docs/",
        ],
        "archive_prefix": "docs/archive/",
        "archive_index_files": [
            "docs/archive/README.md",
            "docs/archive/ARCHIVE_MANIFEST.md",
        ],
        "excluded_prefixes": [
            ".git/",
            ".venv/",
            ".local-data/",
            ".local-artifacts/",
        ],
        "required_authority_metadata_docs": [
            "README.md",
            "docs/ARCHITECTURE.md",
        ],
        "authority_exempt_archive_reference_files": [
            "docs/archive/README.md",
            "docs/archive/ARCHIVE_MANIFEST.md",
            "docs/FC27_DOCUMENTATION_TRUTH_TABLE.md",
            "docs/README.md",
        ],
        "required_index_links": [
            "ARCHITECTURE.md",
            "archive/README.md",
            "archive/ARCHIVE_MANIFEST.md",
        ],
        "allowed_component_statuses": [
            "IMPLEMENTED",
            "PARTIAL",
            "PROPOSED",
            "DEFERRED",
            "DISABLED",
            "REJECTED",
            "SUPERSEDED",
            "ARCHIVED",
            "NOT_FOUND",
        ],
        "required_components": [
            "graph-api",
            "recommendations",
        ],
        "required_feature_flags": {
            "FC27_PROVIDER_ENABLED": "false",
            "FC27_API_CIRCUIT_BREAKER_ENABLED": "true",
        },
        "required_security_invariants": [
            "APPROVAL_IS_NOT_DISPATCH",
            "REDIS_IS_NOT_AUDIT",
        ],
        "non_waivable_categories": [
            "SECURITY_INVARIANT",
            "SECRET_HYGIENE",
            "REAL_DATA_HYGIENE",
        ],
        "non_waivable_reason_codes": [
            "SECURITY_HMAC_PUBLIC_KEY_MISSTATEMENT",
            "DOCUMENT_SECRET_LITERAL",
            "ARCHIVE_REFERENCED_AS_CURRENT",
        ],
        "bounds": {
            "max_file_bytes": 1048576,
            "max_markdown_files": 250,
            "max_references_per_file": 2000,
            "max_results": 5000,
            "max_execution_seconds": 30,
        },
    }


def _make_minimal_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / "src").mkdir(parents=True)
    (repo / "scripts").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    (repo / "migrations" / "post").mkdir(parents=True)
    (repo / "docs" / "archive" / "reports").mkdir(parents=True)

    policy_path = repo / "config" / "documentation_consistency_policy.json"
    policy_path.write_text(json.dumps(_base_policy(), indent=2), encoding="utf-8")

    (repo / ".gitignore").write_text(
        ".env\nsecrets/\n.local-data/\n.local-artifacts/\n", encoding="utf-8"
    )
    (repo / "src" / "settings.py").write_text(
        "FC27_PROVIDER_ENABLED = False\nFC27_API_CIRCUIT_BREAKER_ENABLED = True\n",
        encoding="utf-8",
    )
    (repo / "src" / "graph_api.py").write_text(
        'def create_app():\n    pass\n# @app.get("/health")\n# @app.post("/v1/runs")\n',
        encoding="utf-8",
    )
    (repo / "scripts" / "sample_tool.py").write_text(
        'import argparse\np = argparse.ArgumentParser()\np.add_argument("--dry-run")\n',
        encoding="utf-8",
    )
    (repo / "tests" / "test_sample.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    (repo / "migrations" / "0001_initial.sql").write_text(
        "CREATE TABLE public.graph_threads (id text PRIMARY KEY);\n", encoding="utf-8"
    )
    (repo / "migrations" / "post" / "grants.sql").write_text(
        "-- grants-policy-version: 14\n", encoding="utf-8"
    )
    (repo / "docker-compose.yml").write_text(
        "services:\n"
        "  graph-api:\n"
        "    profiles: [ops]\n"
        "    networks: [fc27_net, fc27_data]\n"
        "networks:\n"
        "  fc27_net: {}\n"
        "  fc27_data:\n"
        "    internal: true\n",
        encoding="utf-8",
    )

    (repo / "README.md").write_text(
        "# Minimal Repo\n\n"
        "- **Status**: Current\n"
        "- **Audience**: Developers\n"
        "- **Last verified commit**: `764d95687d0ccf848ec5b52fbb19638cc38123e4`\n"
        "- **Source of truth**: `src/settings.py`, `migrations/0001_initial.sql`\n"
        "- **Supersedes**: `NONE`\n\n"
        "See [Architecture](docs/ARCHITECTURE.md) and `src/settings.py`.\n",
        encoding="utf-8",
    )
    (repo / "TEST_RESULTS.md").write_text(
        "# Test Results\n\n"
        "- Verification Date: `2026-10-29`\n"
        "- Commit: `764d95687d0ccf848ec5b52fbb19638cc38123e4`\n"
        "- Environment: Windows / Python 3.13 / PostgreSQL 16\n"
        "- NETWORK_REQUESTS_EXECUTED=0\n"
        "- EA_DNS_RESOLUTIONS_EXECUTED=0\n"
        "- check_matchpulse_boundary.py and fc27_capture_preflight.py passed.\n",
        encoding="utf-8",
    )
    (repo / "SECURITY.md").write_text("# Security Policy\n", encoding="utf-8")
    (repo / "MIGRATION_GUIDE.md").write_text("# Migration Guide\n", encoding="utf-8")
    (repo / "THIRD_PARTY_NOTICES.md").write_text("# Notices\n", encoding="utf-8")

    (repo / "docs" / "README.md").write_text(
        "# Docs Index\n\n"
        "## 1. Start Here\n"
        "- [Architecture](ARCHITECTURE.md)\n"
        "## 14. Archive\n"
        "- [Archive Readme](archive/README.md)\n"
        "- [Archive Manifest](archive/ARCHIVE_MANIFEST.md)\n",
        encoding="utf-8",
    )
    (repo / "docs" / "ARCHITECTURE.md").write_text(
        "# System Architecture\n\n"
        "- **Status**: Current\n"
        "- **Audience**: Developers\n"
        "- **Last verified commit**: `764d95687d0ccf848ec5b52fbb19638cc38123e4`\n"
        "- **Source of truth**: `src/graph_api.py`, `docker-compose.yml`\n"
        "- **Supersedes**: `docs/archive/reports/OLD_REPORT.md` (historical, non-authoritative)\n\n"
        "Compose service `graph-api` joins compose network `fc27_net`.\n",
        encoding="utf-8",
    )
    (repo / "docs" / "FEATURE_FLAGS.md").write_text(
        "# Feature Flags\n\n"
        "| Flag Name | Default | Accepted Values | Fail-Closed Behavior |\n"
        "|---|---|---|---|\n"
        "| `FC27_PROVIDER_ENABLED` | `false` | `true`, `false` | Blocks provider access |\n"
        "| `FC27_API_CIRCUIT_BREAKER_ENABLED` | `true` | `true`, `false` | Opens circuit breaker |\n",
        encoding="utf-8",
    )
    (repo / "docs" / "SECURITY_BOUNDARIES.md").write_text(
        "# Security Boundaries\n\n"
        "- `APPROVAL_IS_NOT_DISPATCH`\n"
        "- `REDIS_IS_NOT_AUDIT`\n"
        "- Emergency CLI uses symmetric HMAC-SHA256 shared secret (never a public key).\n",
        encoding="utf-8",
    )
    (repo / "docs" / "FC27_COMPONENT_STATUS.md").write_text(
        "# Component Status\n\n"
        "| Component | Status | Deployment Note | Source Evidence | Test Evidence |\n"
        "|---|---|---|---|---|\n"
        "| `graph-api` | `IMPLEMENTED` | `IMPLEMENTED_NOT_DEPLOYMENT_VALIDATED` | `src/graph_api.py` | `tests/test_sample.py` |\n"
        "| `recommendations` | `DISABLED` | `IMPLEMENTED_NOT_DEPLOYMENT_VALIDATED` | `src/settings.py` | `tests/test_sample.py` |\n",
        encoding="utf-8",
    )
    (repo / "docs" / "FC27_TACTICAL_HYPOTHESES.md").write_text(
        "# Tactical Hypotheses\n\n"
        "SOURCE_CLASSIFICATION=EXTERNAL_TACTICAL_HYPOTHESIS\n"
        "EAST_COASTERS_VALIDATION=NOT_RUN\n"
        "CURRENT_CONFIDENCE=UNVERIFIED\n"
        "RECOMMENDATION_STATUS=NOT_APPROVABLE\n",
        encoding="utf-8",
    )
    (repo / "docs" / "archive" / "README.md").write_text(
        "# Archive Readme\n", encoding="utf-8"
    )
    (repo / "docs" / "archive" / "ARCHIVE_MANIFEST.md").write_text(
        "# Archive Manifest\n\n"
        "| Archived Path | Previous Path | Title | Original Purpose | Archive Date | Reason | Superseded By | Operational Status | Reviewer |\n"
        "|---|---|---|---|---|---|---|---|---|\n"
        "| `docs/archive/reports/OLD_REPORT.md` | `OLD_REPORT.md` | Old Report | Historical | 2026-10-29 | Superseded | `docs/ARCHITECTURE.md` | `DO NOT USE` | Operator |\n",
        encoding="utf-8",
    )
    (repo / "docs" / "archive" / "reports" / "OLD_REPORT.md").write_text(
        "> Archived document\n"
        "> Status: Non-authoritative historical record.\n"
        "> Operational status: DO NOT USE\n"
        "> Original path: OLD_REPORT.md\n"
        "> Archived on: 2026-10-29\n"
        "> Superseded by: docs/ARCHITECTURE.md\n"
        "> Reason: Superseded by current architecture docs.\n"
        "> Warning: Do not execute commands in this archived document.\n\n"
        "# Old Report\n```bash\npython old.py\n```\n",
        encoding="utf-8",
    )
    return repo, policy_path


# ==============================================================================
# DC21: 60 Required Unit Test Scenarios (Isolated tmp_path Fixtures)
# ==============================================================================


def test_01_valid_relative_file_reference(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    report = dc.validate_repository(repo, policy_path=policy)
    assert report.errors == 0
    assert report.warnings == 0


def test_02_broken_relative_file_reference(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "SECURITY.md").write_text(
        "See [Missing](docs/DOES_NOT_EXIST.md) and `src/missing_mod.py`.\n",
        encoding="utf-8",
    )
    report = dc.validate_repository(repo, policy_path=policy)
    codes = {r.reason_code for r in report.results}
    assert "MARKDOWN_RELATIVE_LINK_BROKEN" in codes
    assert "DOC_PATH_NOT_FOUND" in codes


def test_03_path_traversal(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "SECURITY.md").write_text(
        "See [Outside](../../outside.md) and `../../outside_dir/file.py`.\n",
        encoding="utf-8",
    )
    report = dc.validate_repository(repo, policy_path=policy)
    codes = {r.reason_code for r in report.results}
    assert "MARKDOWN_LINK_OUTSIDE_REPOSITORY" in codes or "DOC_PATH_TRAVERSAL" in codes


def test_04_case_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "SECURITY.md").write_text("Reference `src/Settings.py`.\n", encoding="utf-8")
    report = dc.validate_repository(repo, policy_path=policy)
    codes = {r.reason_code for r in report.results}
    assert "DOC_PATH_CASE_MISMATCH" in codes


def test_05_valid_script_reference(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "SECURITY.md").write_text(
        "```bash\npython scripts/sample_tool.py --dry-run\n```\n", encoding="utf-8"
    )
    report = dc.validate_repository(repo, policy_path=policy)
    assert report.errors == 0


def test_06_missing_script_reference(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "SECURITY.md").write_text(
        "```bash\npython scripts/nonexistent_script.py\n```\n", encoding="utf-8"
    )
    report = dc.validate_repository(repo, policy_path=policy)
    assert any(r.reason_code == "SCRIPT_NOT_FOUND" for r in report.results)


def test_07_valid_test_reference(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "SECURITY.md").write_text(
        "```bash\npytest tests/test_sample.py -q\n```\n", encoding="utf-8"
    )
    report = dc.validate_repository(repo, policy_path=policy)
    assert report.errors == 0


def test_08_missing_test_reference(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "SECURITY.md").write_text(
        "```bash\npytest tests/test_does_not_exist.py -q\n```\n", encoding="utf-8"
    )
    report = dc.validate_repository(repo, policy_path=policy)
    assert any(r.reason_code == "TEST_FILE_NOT_FOUND" for r in report.results)


def test_09_valid_compose_service(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "SECURITY.md").write_text(
        "Compose service `graph-api` runs under `--profile ops`.\n", encoding="utf-8"
    )
    report = dc.validate_repository(repo, policy_path=policy)
    assert report.errors == 0


def test_10_missing_compose_service(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "SECURITY.md").write_text(
        "Compose service `nonexistent-service` runs in production.\n", encoding="utf-8"
    )
    report = dc.validate_repository(repo, policy_path=policy)
    assert any(r.reason_code == "COMPOSE_SERVICE_NOT_FOUND" for r in report.results)


def test_11_compose_profile_mismatch(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "SECURITY.md").write_text(
        "Service `graph-api` uses profile `nonexistent_profile`.\n", encoding="utf-8"
    )
    report = dc.validate_repository(repo, policy_path=policy)
    assert any(
        r.reason_code in {"COMPOSE_PROFILE_MISMATCH", "COMPOSE_PROFILE_NOT_FOUND"}
        for r in report.results
    )


def test_12_compose_network_mismatch(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "SECURITY.md").write_text(
        "Compose network `bogus_net` is used by containers.\n", encoding="utf-8"
    )
    report = dc.validate_repository(repo, policy_path=policy)
    assert any(r.reason_code == "COMPOSE_NETWORK_MISMATCH" for r in report.results)


def test_13_existing_feature_flag(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    report = dc.validate_repository(repo, policy_path=policy)
    assert not any(r.category == "FEATURE_FLAG_REFERENCE" for r in report.results)


def test_14_missing_feature_flag(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    ff = repo / "docs" / "FEATURE_FLAGS.md"
    ff.write_text(
        ff.read_text(encoding="utf-8")
        + "| `BOGUS_UNKNOWN_FLAG` | `false` | `true`, `false` | Blocks bogus |\n",
        encoding="utf-8",
    )
    report = dc.validate_repository(repo, policy_path=policy)
    assert any(r.reason_code == "FEATURE_FLAG_NOT_FOUND" for r in report.results)


def test_15_feature_flag_default_mismatch(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "docs" / "FEATURE_FLAGS.md").write_text(
        "# Feature Flags\n\n"
        "| Flag Name | Default | Accepted Values | Fail-Closed Behavior |\n"
        "|---|---|---|---|\n"
        "| `FC27_PROVIDER_ENABLED` | `true` | `true`, `false` | Blocks provider access |\n"
        "| `FC27_API_CIRCUIT_BREAKER_ENABLED` | `true` | `true`, `false` | Opens circuit breaker |\n",
        encoding="utf-8",
    )
    report = dc.validate_repository(repo, policy_path=policy)
    assert any(r.reason_code == "FEATURE_FLAG_DEFAULT_MISMATCH" for r in report.results)


def test_16_feature_flag_fail_closed_mismatch(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "docs" / "FEATURE_FLAGS.md").write_text(
        "# Feature Flags\n\n"
        "| Flag Name | Default | Accepted Values | Fail-Closed Behavior |\n"
        "|---|---|---|---|\n"
        "| `FC27_PROVIDER_ENABLED` | `false` | `true`, `false` | unlocks all downstream gates |\n"
        "| `FC27_API_CIRCUIT_BREAKER_ENABLED` | `true` | `true`, `false` | Opens circuit breaker |\n",
        encoding="utf-8",
    )
    report = dc.validate_repository(repo, policy_path=policy)
    assert any(
        r.reason_code == "FEATURE_FLAG_FAIL_CLOSED_MISMATCH" for r in report.results
    )


def test_17_existing_migration(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "MIGRATION_GUIDE.md").write_text(
        "Applies `migrations/0001_initial.sql`.\n", encoding="utf-8"
    )
    report = dc.validate_repository(repo, policy_path=policy)
    assert report.errors == 0


def test_18_missing_migration(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "MIGRATION_GUIDE.md").write_text(
        "Applies `migrations/0099_nonexistent.sql`.\n", encoding="utf-8"
    )
    report = dc.validate_repository(repo, policy_path=policy)
    assert any(r.reason_code == "MIGRATION_NOT_FOUND" for r in report.results)


def test_19_postgressaver_domain_table_conflation(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "MIGRATION_GUIDE.md").write_text(
        "PostgresSaver stores normalized FC 27 domain tables directly.\n",
        encoding="utf-8",
    )
    report = dc.validate_repository(repo, policy_path=policy)
    assert any(
        r.reason_code == "SECURITY_POSTGRESSAVER_DOMAIN_CONFLATION"
        for r in report.results
    )


def test_20_existing_local_endpoint(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "SECURITY.md").write_text(
        "Submit runs via `POST /v1/runs`.\n", encoding="utf-8"
    )
    report = dc.validate_repository(repo, policy_path=policy)
    assert report.errors == 0


def test_21_missing_local_endpoint(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "SECURITY.md").write_text(
        "Call `GET /v1/nonexistent-endpoint` to bypass checks.\n", encoding="utf-8"
    )
    report = dc.validate_repository(repo, policy_path=policy)
    assert any(
        r.reason_code == "APPLICATION_ENDPOINT_NOT_FOUND" for r in report.results
    )


def test_22_endpoint_authentication_mismatch(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "SECURITY.md").write_text(
        "Endpoint /v1/runs is unauthenticated in production.\n", encoding="utf-8"
    )
    report = dc.validate_repository(repo, policy_path=policy)
    assert any(r.reason_code == "APPLICATION_AUTH_MISMATCH" for r in report.results)


def test_23_archived_file_with_notice(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    report = dc.validate_repository(repo, policy_path=policy, include_archive=True)
    assert report.errors == 0


def test_24_archived_file_without_notice(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "docs" / "archive" / "reports" / "OLD_REPORT.md").write_text(
        "# Missing Notice Header\n", encoding="utf-8"
    )
    report = dc.validate_repository(repo, policy_path=policy)
    assert any(r.reason_code == "ARCHIVE_NOTICE_MISSING" for r in report.results)


def test_25_archive_manifest_missing_entry(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "docs" / "archive" / "reports" / "UNLISTED.md").write_text(
        "> Archived document\n"
        "> Status: Non-authoritative historical record.\n"
        "> Operational status: DO NOT USE\n"
        "> Original path: UNLISTED.md\n"
        "> Archived on: 2026-10-29\n"
        "> Superseded by: docs/ARCHITECTURE.md\n"
        "> Reason: Historical.\n"
        "> Warning: Do not execute commands in this archived document.\n",
        encoding="utf-8",
    )
    report = dc.validate_repository(repo, policy_path=policy)
    assert any(
        r.reason_code == "ARCHIVE_MANIFEST_ENTRY_MISSING" for r in report.results
    )


def test_26_manifest_entry_missing_file(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "docs" / "archive" / "reports" / "OLD_REPORT.md").unlink()
    report = dc.validate_repository(repo, policy_path=policy)
    assert any(r.reason_code == "ARCHIVE_FILE_MISSING" for r in report.results)


def test_27_archived_doc_referenced_as_current(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "SECURITY.md").write_text(
        "See `docs/archive/reports/OLD_REPORT.md` as the current authoritative spec.\n",
        encoding="utf-8",
    )
    report = dc.validate_repository(repo, policy_path=policy)
    assert any(
        r.reason_code in {"DOC_PATH_ARCHIVED_AS_CURRENT", "ARCHIVE_REFERENCED_AS_CURRENT"}
        for r in report.results
    )


def test_28_hmac_described_correctly(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "SECURITY.md").write_text(
        "HMAC-SHA256 uses a symmetric shared secret (never a public key).\n",
        encoding="utf-8",
    )
    report = dc.validate_repository(repo, policy_path=policy)
    assert report.errors == 0


def test_29_hmac_incorrectly_called_public_key_verification(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "SECURITY.md").write_text(
        "Commissioner CLI uses HMAC public key verification.\n", encoding="utf-8"
    )
    report = dc.validate_repository(repo, policy_path=policy)
    assert any(
        r.reason_code == "SECURITY_HMAC_PUBLIC_KEY_MISSTATEMENT" for r in report.results
    )


def test_30_resume_payload_described_as_authority(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "SECURITY.md").write_text(
        "Passing Command(resume={'approved': True}) authorizes the decision.\n",
        encoding="utf-8",
    )
    report = dc.validate_repository(repo, policy_path=policy)
    assert any(
        r.reason_code == "SECURITY_RESUME_AUTHORITY_MISSTATEMENT"
        for r in report.results
    )


def test_31_approval_dispatch_conflation(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "SECURITY.md").write_text(
        "Commissioner approval automatically dispatches the message.\n",
        encoding="utf-8",
    )
    report = dc.validate_repository(repo, policy_path=policy)
    assert any(
        r.reason_code == "SECURITY_APPROVAL_DISPATCH_CONFLATION" for r in report.results
    )


def test_32_redis_audit_conflation(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "SECURITY.md").write_text(
        "Redis is the audit of record for all decisions.\n", encoding="utf-8"
    )
    report = dc.validate_repository(repo, policy_path=policy)
    assert any(
        r.reason_code == "SECURITY_REDIS_AUDIT_MISSTATEMENT" for r in report.results
    )


def test_33_unlabeled_tactical_claim(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "SECURITY.md").write_text(
        "4-2-1-3 is the optimal formation that maximizes win probability.\n",
        encoding="utf-8",
    )
    report = dc.validate_repository(repo, policy_path=policy)
    assert any(r.reason_code == "TACTICAL_CLAIM_UNLABELED" for r in report.results)


def test_34_properly_classified_tactical_hypothesis(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "docs" / "FC27_TACTICAL_HYPOTHESES.md").write_text(
        "# Hypotheses\n"
        "4-2-1-3 is claimed as optimal formation under EXTERNAL_TACTICAL_HYPOTHESIS (UNVERIFIED, NOT_APPROVABLE).\n",
        encoding="utf-8",
    )
    report = dc.validate_repository(repo, policy_path=policy)
    assert not any(r.category == "TACTICAL_CLAIM" for r in report.results)


def test_35_fc25_fc26_evidence_represented_as_fc27_fact(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "SECURITY.md").write_text(
        "FC 25 proves FC 27 stamina drain behavior.\n", encoding="utf-8"
    )
    report = dc.validate_repository(repo, policy_path=policy)
    assert any(r.reason_code == "TACTICAL_FC_VERSION_MISMATCH" for r in report.results)


def test_36_valid_document_metadata(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    report = dc.validate_repository(repo, policy_path=policy)
    assert not any(r.category == "DOCUMENT_AUTHORITY" for r in report.results)


def test_37_missing_source_of_truth_metadata(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "README.md").write_text(
        "# Minimal Repo\n\n"
        "- **Status**: Current\n"
        "- **Last verified commit**: `764d95687d0ccf848ec5b52fbb19638cc38123e4`\n",
        encoding="utf-8",
    )
    report = dc.validate_repository(repo, policy_path=policy)
    assert any(
        r.reason_code == "DOCUMENT_SOURCE_OF_TRUTH_MISSING" for r in report.results
    )


def test_38_duplicate_document_authority(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    # Give README.md and docs/ARCHITECTURE.md the exact same H1 title
    (repo / "README.md").write_text(
        "# System Architecture\n\n"
        "- **Status**: Current\n"
        "- **Last verified commit**: `764d95687d0ccf848ec5b52fbb19638cc38123e4`\n"
        "- **Source of truth**: `src/settings.py`\n",
        encoding="utf-8",
    )
    report = dc.validate_repository(repo, policy_path=policy)
    assert any(
        r.reason_code == "DOCUMENT_DUPLICATE_AUTHORITY" for r in report.results
    )


def test_39_suspected_secret_literal_is_redacted(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    fake_secret = "ghp_1234567890abcdefghijklmnopqrstuvwx"
    (repo / "SECURITY.md").write_text(
        f"Token: {fake_secret}\n", encoding="utf-8"
    )
    report = dc.validate_repository(repo, policy_path=policy)
    secret_findings = [
        r for r in report.results if r.reason_code == "DOCUMENT_SECRET_LITERAL"
    ]
    assert len(secret_findings) == 1
    assert fake_secret not in secret_findings[0].referenced_value
    assert secret_findings[0].referenced_value.startswith("<redacted:sha256:")


def test_40_allowed_secret_placeholder(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "SECURITY.md").write_text(
        "Use `postgresql://fc27_app:<password>@127.0.0.1:5432/fc27` or `replace_with_random`.\n",
        encoding="utf-8",
    )
    report = dc.validate_repository(repo, policy_path=policy)
    assert not any(r.category == "SECRET_HYGIENE" for r in report.results)


def test_41_instruction_to_commit_dotenv(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "SECURITY.md").write_text(
        "Run `git add .env` before pushing.\n", encoding="utf-8"
    )
    report = dc.validate_repository(repo, policy_path=policy)
    assert any(
        r.reason_code == "DOCUMENT_INSTRUCTS_SECRET_COMMIT" for r in report.results
    )


def test_42_instruction_to_commit_local_data(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "SECURITY.md").write_text(
        "Run `git add .local-data/east_coasters.json`.\n", encoding="utf-8"
    )
    report = dc.validate_repository(repo, policy_path=policy)
    assert any(
        r.reason_code == "DOCUMENT_INSTRUCTS_PRIVATE_DATA_COMMIT"
        for r in report.results
    )


def test_43_test_report_with_required_metadata(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    report = dc.validate_repository(repo, policy_path=policy)
    assert not any(r.category == "TEST_REFERENCE" for r in report.results)


def test_44_undated_test_count_presented_as_current(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "SECURITY.md").write_text(
        "The repository always has exactly 3,100 tests.\n", encoding="utf-8"
    )
    report = dc.validate_repository(repo, policy_path=policy)
    assert any(
        r.reason_code == "TEST_REPORT_UNDATED_COUNT_AS_CURRENT" for r in report.results
    )


def test_45_documentation_index_complete(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    report = dc.validate_repository(repo, policy_path=policy)
    assert not any(r.reason_code == "DOC_INDEX_ENTRY_MISSING" for r in report.results)


def test_46_documentation_index_missing_entry(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "docs" / "README.md").write_text(
        "# Docs Index\n- [Archive Readme](archive/README.md)\n", encoding="utf-8"
    )
    report = dc.validate_repository(repo, policy_path=policy)
    assert any(r.reason_code == "DOC_INDEX_ENTRY_MISSING" for r in report.results)


def test_47_external_link_syntax_accepted_without_network(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "SECURITY.md").write_text(
        "See [Upstream](https://github.com/1erkandogan/fc27-clubs-api).\n",
        encoding="utf-8",
    )
    report = dc.validate_repository(repo, policy_path=policy)
    assert report.errors == 0
    assert report.warnings == 0


def test_48_malformed_external_link_warned(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "SECURITY.md").write_text(
        "See [Bad](https://example.com/foo https://example.com/bar).\n",
        encoding="utf-8",
    )
    report = dc.validate_repository(repo, policy_path=policy)
    assert report.errors == 0
    assert report.warnings == 1
    assert report.results[0].reason_code == "MARKDOWN_EXTERNAL_LINK_SUSPECT"


def test_49_deterministic_json_ordering(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "SECURITY.md").write_text(
        "See `src/missing_b.py` and `src/missing_a.py`.\n", encoding="utf-8"
    )
    r1 = json.dumps(dc.validate_repository(repo, policy_path=policy).to_dict(), sort_keys=True)
    r2 = json.dumps(dc.validate_repository(repo, policy_path=policy).to_dict(), sort_keys=True)
    assert r1 == r2


def test_50_strict_mode_warning_behavior(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "SECURITY.md").write_text(
        "See [Bad](https://example.com/foo https://example.com/bar).\n",
        encoding="utf-8",
    )
    rc_normal = dc.main(["--repo-root", str(repo), "--policy", str(policy), "--check"])
    rc_strict = dc.main(
        ["--repo-root", str(repo), "--policy", str(policy), "--check", "--strict"]
    )
    assert rc_normal == 0
    assert rc_strict == 1


def test_51_exit_code_zero(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    rc = dc.main(["--repo-root", str(repo), "--policy", str(policy), "--check", "--strict"])
    assert rc == 0


def test_52_exit_code_one(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "SECURITY.md").write_text("Broken `src/nonexistent.py`.\n", encoding="utf-8")
    rc = dc.main(["--repo-root", str(repo), "--policy", str(policy), "--check"])
    assert rc == 1


def test_53_invalid_policy_exit_code_two(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    bad_policy = copy.deepcopy(_base_policy())
    bad_policy["policy_version"] = "invalid-version"
    policy.write_text(json.dumps(bad_policy), encoding="utf-8")
    rc = dc.main(["--repo-root", str(repo), "--policy", str(policy), "--check"])
    assert rc == 2


def test_54_waiver_exact_match(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "SECURITY.md").write_text("Broken `src/planned_future.py`.\n", encoding="utf-8")
    waiver_file = repo / "config" / "documentation_consistency_waivers.json"
    waiver_file.write_text(
        json.dumps(
            {
                "waivers": [
                    {
                        "waiver_id": "W-TEST-001",
                        "reason_code": "DOC_PATH_NOT_FOUND",
                        "document_path": "SECURITY.md",
                        "reference_pattern": "src/planned_future.py",
                        "justification": "Planned module in next PR",
                        "owner": "platform-team",
                        "created_date": "2026-10-29",
                        "expiration_date": "2099-12-31",
                        "tracking_id": "ISSUE-101",
                        "approved_by": "commissioner",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    report = dc.validate_repository(repo, policy_path=policy, waiver_path=waiver_file)
    assert report.errors == 0
    assert report.waivers_active == 1
    assert report.results[0].waived is True
    assert report.results[0].waiver_reference == "W-TEST-001"


def test_55_expired_waiver_rejected(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "SECURITY.md").write_text("Broken `src/old.py`.\n", encoding="utf-8")
    waiver_file = repo / "config" / "documentation_consistency_waivers.json"
    waiver_file.write_text(
        json.dumps(
            {
                "waivers": [
                    {
                        "waiver_id": "W-EXP-001",
                        "reason_code": "DOC_PATH_NOT_FOUND",
                        "document_path": "SECURITY.md",
                        "reference_pattern": "src/old.py",
                        "justification": "Expired waiver",
                        "owner": "platform-team",
                        "created_date": "2025-01-01",
                        "expiration_date": "2025-02-01",
                        "tracking_id": "ISSUE-102",
                        "approved_by": "commissioner",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    rc = dc.main(
        [
            "--repo-root",
            str(repo),
            "--policy",
            str(policy),
            "--waivers",
            str(waiver_file),
            "--check",
        ]
    )
    assert rc == 2


def test_56_broad_waiver_rejected(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    waiver_file = repo / "config" / "documentation_consistency_waivers.json"
    waiver_file.write_text(
        json.dumps(
            {
                "waivers": [
                    {
                        "waiver_id": "W-BROAD-001",
                        "reason_code": "DOC_PATH_NOT_FOUND",
                        "document_path": "*",
                        "reference_pattern": "*",
                        "justification": "Broad wildcard waiver",
                        "owner": "platform-team",
                        "created_date": "2026-10-29",
                        "expiration_date": "2099-12-31",
                        "tracking_id": "ISSUE-103",
                        "approved_by": "commissioner",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    rc = dc.main(
        [
            "--repo-root",
            str(repo),
            "--policy",
            str(policy),
            "--waivers",
            str(waiver_file),
            "--check",
        ]
    )
    assert rc == 2


def test_57_security_contradiction_cannot_be_waived(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    waiver_file = repo / "config" / "documentation_consistency_waivers.json"
    waiver_file.write_text(
        json.dumps(
            {
                "waivers": [
                    {
                        "waiver_id": "W-SEC-001",
                        "reason_code": "SECURITY_HMAC_PUBLIC_KEY_MISSTATEMENT",
                        "document_path": "SECURITY.md",
                        "reference_pattern": "HMAC public key",
                        "justification": "Cannot waive security contradiction",
                        "owner": "platform-team",
                        "created_date": "2026-10-29",
                        "expiration_date": "2099-12-31",
                        "tracking_id": "ISSUE-104",
                        "approved_by": "commissioner",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    rc = dc.main(
        [
            "--repo-root",
            str(repo),
            "--policy",
            str(policy),
            "--waivers",
            str(waiver_file),
            "--check",
        ]
    )
    assert rc == 2


def test_58_secret_finding_cannot_be_waived(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    waiver_file = repo / "config" / "documentation_consistency_waivers.json"
    waiver_file.write_text(
        json.dumps(
            {
                "waivers": [
                    {
                        "waiver_id": "W-SECRET-001",
                        "reason_code": "DOCUMENT_SECRET_LITERAL",
                        "document_path": "SECURITY.md",
                        "reference_pattern": "redacted",
                        "justification": "Cannot waive secret literal",
                        "owner": "platform-team",
                        "created_date": "2026-10-29",
                        "expiration_date": "2099-12-31",
                        "tracking_id": "ISSUE-105",
                        "approved_by": "commissioner",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    rc = dc.main(
        [
            "--repo-root",
            str(repo),
            "--policy",
            str(policy),
            "--waivers",
            str(waiver_file),
            "--check",
        ]
    )
    assert rc == 2


def test_59_local_artifacts_ignored() -> None:
    gitignore_text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".local-artifacts/" in gitignore_text
    proc = subprocess.run(
        ["git", "check-ignore", ".local-artifacts/documentation-consistency-report.json"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0


def test_60_zero_external_network_use(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbid_network(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError(f"Network or DNS call prohibited: {args}")

    monkeypatch.setattr(socket, "getaddrinfo", _forbid_network)
    monkeypatch.setattr(socket, "create_connection", _forbid_network)
    monkeypatch.setattr(urllib.request, "urlopen", _forbid_network)

    report = dc.validate_repository(REPO_ROOT, include_archive=True)
    assert report.errors == 0
    assert report.warnings == 0


# ==============================================================================
# DC22: Real Repository Integration Tests
# ==============================================================================


def test_real_repository_passes_strict_consistency_check() -> None:
    report = dc.validate_repository(REPO_ROOT, include_archive=False)
    assert report.policy_version == "fc27-documentation-consistency-v1"
    assert report.checked_documents >= 30
    assert report.references_scanned >= 500
    assert report.errors == 0, f"Unexpected errors: {report.results}"
    assert report.warnings == 0, f"Unexpected warnings: {report.results}"


def test_real_repository_passes_with_archive_included() -> None:
    report = dc.validate_repository(REPO_ROOT, include_archive=True)
    assert report.checked_documents >= 48
    assert report.errors == 0, f"Unexpected archive errors: {report.results}"
    assert report.warnings == 0, f"Unexpected archive warnings: {report.results}"


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
        assert required_keys.issubset(entry.keys())
        paths_seen.add(entry["path"])
    assert "README.md" in paths_seen
    assert "docs/ARCHITECTURE.md" in paths_seen
    assert "docs/archive/ARCHIVE_MANIFEST.md" in paths_seen
    assert settings_module.is_fc27_provider_enabled() is False
    assert settings_module.is_fc27_api_circuit_breaker_enabled() is True


def test_safe_adapter_bundle_documentation_consistency_checks(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "SECURITY.md").write_text(
        ".local-artifacts/ is tracked in git.\n"
        "The runtime uses zipimport to load modules.\n"
        ".local-artifacts/ is required for CI.\n",
        encoding="utf-8",
    )
    report = dc.validate_repository(repo, policy_path=policy)
    codes = {r.reason_code for r in report.results}
    assert "SAFE_BUNDLE_LOCAL_ARTIFACTS_MISSTATED" in codes
    assert "SAFE_BUNDLE_ZIP_RUNTIME_MISSTATED" in codes
    assert "SAFE_BUNDLE_CI_DEPENDENCY_MISSTATED" in codes


def test_uvicorn_exe_stale_demo_factory_rejected(tmp_path: Path) -> None:
    repo, policy = _make_minimal_repo(tmp_path)
    (repo / "src" / "fc27_demo_api.py").write_text(
        "def create_demo_app():\n    return None\n",
        encoding="utf-8",
    )
    (repo / "SECURITY.md").write_text(
        "```powershell\n"
        ".\\.venv\\Scripts\\uvicorn.exe src.fc27_demo_api:create_app --factory --host 127.0.0.1 --port 8001\n"
        "```\n",
        encoding="utf-8",
    )
    report = dc.validate_repository(repo, policy_path=policy)
    codes = {r.reason_code for r in report.results}
    assert "APP_FACTORY_NOT_FOUND" in codes
