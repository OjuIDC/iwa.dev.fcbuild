from __future__ import annotations

import ast
import json
import os
import subprocess
from pathlib import Path

from scripts import check_documentation_consistency as dc
from src import (
    fc27_clubs_api_contracts,
    fc27_community_provider,
    fc27_demo_api,
    fc27_pandas_view,
    graph_api,
    settings,
)
from src.fc27_pandas_view import _PROHIBITED_VIEW_KEYS

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_26_fixture_tests_retained() -> None:
    required_fixture_tests = [
        "tests/test_fc27_community_provider.py",
        "tests/test_fc27_pandas_view.py",
        "tests/test_fc27_clubs_api_contracts.py",
        "tests/test_fc27_persistence_and_queries.py",
        "tests/test_fc27_search.py",
        "tests/test_fc27_scout.py",
        "tests/test_fc27_analyst.py",
        "tests/test_fc27_dashboard_queries.py",
        "tests/test_fc27_demo.py",
    ]
    for rel in required_fixture_tests:
        assert (REPO_ROOT / rel).is_file(), f"Missing fixture test suite: {rel}"


def test_27_browser_diagnostic_history_recorded() -> None:
    provider_doc = (REPO_ROOT / "docs" / "FC27_PROVIDER_INTEGRATION.md").read_text(encoding="utf-8")
    assert "ERR_HTTP2_PROTOCOL_ERROR" in provider_doc
    assert "https://www.ea.com/games/ea-sports-fc/clubs/overview" in provider_doc
    assert "common-gen5" in provider_doc

    manifest = json.loads(
        (REPO_ROOT / "config" / "fc27_archive_manifest.json").read_text(encoding="utf-8")
    )
    records = {r["record_id"]: r for r in manifest.get("branch_only_research_records", [])}
    assert "BRANCH-RESEARCH-002" in records
    assert "ERR_HTTP2_PROTOCOL_ERROR" in records["BRANCH-RESEARCH-002"]["summary"]


def test_28_browser_provider_not_active() -> None:
    assert not (REPO_ROOT / "src" / "fc27_browser_provider.py").exists()
    assert not (REPO_ROOT / "Dockerfile.browser-probe").exists()


def test_29_standards_client_403_history_recorded() -> None:
    provider_doc = (REPO_ROOT / "docs" / "FC27_PROVIDER_INTEGRATION.md").read_text(encoding="utf-8")
    assert "403 Forbidden" in provider_doc or "HTTP 403" in provider_doc
    assert "proclubs.ea.com/api/fc/clubs/info" in provider_doc

    manifest = json.loads(
        (REPO_ROOT / "config" / "fc27_archive_manifest.json").read_text(encoding="utf-8")
    )
    records = {r["record_id"]: r for r in manifest.get("branch_only_research_records", [])}
    assert "BRANCH-RESEARCH-001" in records
    assert "403" in records["BRANCH-RESEARCH-001"]["summary"]


def test_30_polling_disabled() -> None:
    assert settings.is_fc27_provider_enabled() is False
    decl = fc27_community_provider.FC27CommunityProviderDeclaration()
    assert decl.polling_enabled is False
    assert decl.maximum_retries == 0


def test_31_recommendations_disabled() -> None:
    decl = fc27_community_provider.FC27CommunityProviderDeclaration()
    assert decl.recommendations_enabled is False
    comp_doc = (REPO_ROOT / "docs" / "FC27_COMPONENT_STATUS.md").read_text(encoding="utf-8")
    assert "DISABLED" in comp_doc


def test_32_dispatch_disabled() -> None:
    assert settings.is_dispatch_enabled() is False
    decl = fc27_community_provider.FC27CommunityProviderDeclaration()
    assert decl.dispatch_enabled is False


def test_33_no_raw_response_storage() -> None:
    decl = fc27_community_provider.FC27CommunityProviderDeclaration()
    assert decl.raw_response_retention is False
    migration_sql = (
        REPO_ROOT / "migrations" / "0012_fc27_performance_and_quarantine.sql"
    ).read_text(encoding="utf-8")
    assert "raw_payload" not in migration_sql.lower()
    assert "raw_response" not in migration_sql.lower()


def test_34_no_source_player_ids_in_current_path() -> None:
    assert "player_id" in _PROHIBITED_VIEW_KEYS
    assert "persona_id" in _PROHIBITED_VIEW_KEYS
    assert "nucleus_id" in _PROHIBITED_VIEW_KEYS
    assert "ea_id" in _PROHIBITED_VIEW_KEYS


def test_35_no_aliases_in_current_path() -> None:
    assert "player_name" in _PROHIBITED_VIEW_KEYS
    assert "pro_name" in _PROHIBITED_VIEW_KEYS
    assert "gamertag" in _PROHIBITED_VIEW_KEYS
    assert "psn_id" in _PROHIBITED_VIEW_KEYS


def test_36_no_duplicate_provider_owner() -> None:
    owners = []
    for py_file in (REPO_ROOT / "src").rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        if "class FC27CommunityProviderAdapter" in text:
            owners.append(py_file.relative_to(REPO_ROOT).as_posix())
    assert owners == ["src/fc27_community_provider.py"]


def test_37_no_duplicate_normalization_owner() -> None:
    contracts_owners = []
    pandas_owners = []
    for py_file in (REPO_ROOT / "src").rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        if "def normalize_matches_payload" in text:
            contracts_owners.append(py_file.relative_to(REPO_ROOT).as_posix())
        if "class FC27PandasView" in text:
            pandas_owners.append(py_file.relative_to(REPO_ROOT).as_posix())
    assert contracts_owners == ["src/fc27_clubs_api_contracts.py"]
    assert pandas_owners == ["src/fc27_pandas_view.py"]
    assert hasattr(fc27_clubs_api_contracts, "normalize_matches_payload")
    assert hasattr(fc27_pandas_view, "FC27PandasView")


def test_38_no_duplicate_source_classification_definition() -> None:
    assert (
        fc27_demo_api.LIVE_EA_CLUBS_COMMUNITY_CLIENT
        is fc27_community_provider.LIVE_EA_CLUBS_COMMUNITY_CLIENT
    )
    assert callable(fc27_demo_api.resolve_dashboard_source_label)
    assert (
        fc27_demo_api.resolve_dashboard_source_label("LIVE_EA_CLUBS_COMMUNITY_CLIENT")
        == "Live EA Clubs Data via pinned community client"
    )
    assert (
        fc27_demo_api.resolve_dashboard_source_label("DEMO_FIXTURE_LOOPBACK")
        == "Synthetic Fixture Data"
    )


def test_39_no_orphan_current_document() -> None:
    docs_readme = (REPO_ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    for md in (REPO_ROOT / "docs").glob("*.md"):
        if md.name == "README.md":
            continue
        assert md.name in docs_readme, f"Orphan current doc: {md.name}"


def test_40_no_orphan_current_script() -> None:
    scripts_readme = (REPO_ROOT / "scripts" / "README.md").read_text(encoding="utf-8")
    for script_path in (REPO_ROOT / "scripts").iterdir():
        if script_path.is_dir() or script_path.name == "README.md":
            continue
        assert script_path.name in scripts_readme, f"Orphan script: {script_path.name}"


def test_41_no_orphan_current_docker_service() -> None:
    arch_doc = (REPO_ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    dev_doc = (REPO_ROOT / "docs" / "DEVELOPMENT.md").read_text(encoding="utf-8")
    voice_doc = (REPO_ROOT / "docs" / "VOICE_CAPABILITIES.md").read_text(encoding="utf-8")
    prov_doc = (REPO_ROOT / "docs" / "FC27_PROVIDER_INTEGRATION.md").read_text(encoding="utf-8")
    eval_doc = (REPO_ROOT / "docs" / "FC27_COMMUNITY_PROVIDER_EVALUATION.md").read_text(encoding="utf-8")
    combined = arch_doc + "\n" + dev_doc + "\n" + voice_doc + "\n" + prov_doc + "\n" + eval_doc
    for service_name in [
        "postgres",
        "redis",
        "fc27-mcp",
        "graph-api",
        "ollama",
        "ollama-init",
        "stt",
        "tts",
        "fc27-community-client-evaluation",
    ]:
        assert service_name in combined, f"Service {service_name} not documented"


def test_42_no_stale_root_level_phase_report() -> None:
    root_mds = {p.name for p in REPO_ROOT.glob("*.md")}
    allowed_root_mds = {
        "README.md",
        "SECURITY.md",
        "MIGRATION_GUIDE.md",
        "TEST_RESULTS.md",
        "THIRD_PARTY_NOTICES.md",
    }
    assert root_mds == allowed_root_mds


def test_43_no_active_reference_to_archived_commands() -> None:
    active_docs = [REPO_ROOT / "README.md"] + list((REPO_ROOT / "docs").glob("*.md"))
    for doc in active_docs:
        if doc.name == "FC27_DOCUMENTATION_TRUTH_TABLE.md":
            continue
        text = doc.read_text(encoding="utf-8")
        assert "python scripts/matchpulse_conformance.py" not in text
        assert "python scripts/archive/" not in text


def test_44_no_current_reference_to_deleted_files() -> None:
    for py_file in (REPO_ROOT / "src").rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        assert "agency_org" not in text
        assert ".env.models.example" not in text
        assert "requirements-test.txt" not in text


def test_45_no_ignored_local_artifact_tracked() -> None:
    proc = subprocess.run(
        ["git", "ls-files", ".local-artifacts", ".local-data"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == ""


def test_46_repository_inventory_matches_tracked_files() -> None:
    inv_path = REPO_ROOT / "docs" / "repository_inventory.json"
    inv_data = json.loads(inv_path.read_text(encoding="utf-8"))
    inv_paths = {entry["path"] for entry in inv_data["files"]}

    proc = subprocess.run(
        ["git", "ls-files"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    tracked_paths = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
    new_cleanup_files = {
        "config/fc27_archive_manifest.json",
        "config/fc27_removed_items_manifest.json",
        "scripts/README.md",
        "tests/test_fc27_archive_manifest.py",
        "tests/test_fc27_repository_cleanup.py",
        "Dockerfile.community-worker",
        "Dockerfile.discord",
        "docker-compose.community-worker.yml",
        "docker-compose.discord.yml",
        "docs/FC27_BACKUP_AND_RECOVERY.md",
        "docs/FC27_COACH_HYPOTHESIS_ENGINE.md",
        "docs/FC27_DAILY_POLLING_OPERATIONS.md",
        "docs/FC27_DIRECTOR_RECONCILIATION.md",
        "docs/FC27_DISCORD_BOT.md",
        "docs/FC27_POLLING_WORKER.md",
        "docs/FC27_TACTICS_CANDIDATE_ENGINE.md",
        "scripts/fc27_backup.py",
        "scripts/fc27_discord_bot.py",
        "scripts/fc27_polling_worker.py",
        "scripts/fc27_restore_verify.py",
        "src/fc27_backup_restore.py",
        "src/fc27_coach.py",
        "src/fc27_director.py",
        "src/fc27_discord_bot.py",
        "src/fc27_polling_worker.py",
        "src/fc27_tactics.py",
        "tests/test_fc27_advisory_pipeline_e2e.py",
        "tests/test_fc27_backup_restore.py",
        "tests/test_fc27_coach.py",
        "tests/test_fc27_director.py",
        "tests/test_fc27_discord_bot.py",
        "tests/test_fc27_polling_worker.py",
        "tests/test_fc27_tactics.py",
        "docs/FC27_PERFORMANCE_BASELINE.md",
        "docs/FC27_LANGUAGE_DECISION_MATRIX.md",
        "docs/FC27_LOCAL_INFERENCE_BOUNDARY.md",
        "src/fc27_performance.py",
        "tests/test_fc27_performance.py",
        ".dockerignore",
        ".github/workflows/container-images.yml",
        ".github/workflows/test-fast.yml",
        ".github/workflows/test-nightly.yml",
        "docs/FC27_CI_BROWSER_REPORTS.md",
        "docs/FC27_CI_TEST_STRATEGY.md",
        "docs/FC27_TEST_ISOLATION_MATRIX.md",
        "scripts/aggregate_test_reports.py",
        "scripts/build_container_report.py",
        "scripts/build_test_report.py",
        "scripts/classify_ci_changes.py",
        "scripts/sanitize_test_report.py",
        "src/fc27_ci_test_groups.py",
        "tests/test_ci_report_sanitization.py",
        "tests/test_github_workflows.py",
        "docker-compose.ghcr.yml",
        "docs/FC27_CONTAINER_PUBLISHING.md",
        "docs/FC27_GHCR_RUNTIME_PROMOTION.md",
        "docs/VISUAL_STUDIO_SETUP.md",
        "iwa.dev.fcbuild.pyproj",
        "iwa.dev.fcbuild.sln",
    }
    for rel in new_cleanup_files:
        if (REPO_ROOT / rel).exists():
            tracked_paths.add(rel)

    assert inv_paths == tracked_paths
    assert inv_data.get("total_files") == len(inv_paths)


def test_47_documentation_strict_validation_passes() -> None:
    report = dc.validate_repository(REPO_ROOT, include_archive=True)
    assert report.errors == 0
    assert report.warnings == 0


def test_48_current_startup_commands_resolve() -> None:
    assert callable(fc27_demo_api.create_demo_app)
    assert callable(graph_api.create_app)


def test_49_current_cli_entrypoints_import() -> None:
    py_scripts = sorted((REPO_ROOT / "scripts").glob("*.py"))
    assert len(py_scripts) == 19
    for script_path in py_scripts:
        source = script_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(script_path))
        assert isinstance(tree, ast.Module)


def test_50_current_docker_entrypoints_import() -> None:
    docker_targets = [
        "src/fc27_mcp_server.py",
        "src/graph_api.py",
        "voice/stt_service.py",
        "voice/tts_service.py",
        "scripts/fc27_community_eval.py",
        "scripts/fc27_polling_worker.py",
        "scripts/fc27_discord_bot.py",
    ]
    for rel in docker_targets:
        path = REPO_ROOT / rel
        assert path.is_file(), f"Missing Docker entrypoint target: {rel}"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        assert isinstance(tree, ast.Module)


def test_51_visual_studio_solution_and_hygiene_rules() -> None:
    gitignore_text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    for required_ignore in (
        "/.venv/",
        "/.vs/",
        "*.suo",
        "*.user",
        "*.userosscache",
        "*.sln.docstates",
    ):
        assert required_ignore in gitignore_text, f"Missing .gitignore rule: {required_ignore}"

    for ignored_probe in (
        ".venv/pyvenv.cfg",
        ".vs/iwa.dev.fcbuild/v18/.suo",
        "iwa.dev.fcbuild.suo",
        "iwa.dev.fcbuild.pyproj.user",
        "iwa.dev.fcbuild.userosscache",
        "iwa.dev.fcbuild.sln.docstates",
    ):
        proc = subprocess.run(
            ["git", "check-ignore", "-q", ignored_probe],
            cwd=str(REPO_ROOT),
            check=False,
        )
        assert proc.returncode == 0, f"Expected {ignored_probe} to be gitignored"

    for tracked_vs_file in (
        "iwa.dev.fcbuild.sln",
        "iwa.dev.fcbuild.pyproj",
        "docs/VISUAL_STUDIO_SETUP.md",
    ):
        vs_path = REPO_ROOT / tracked_vs_file
        assert vs_path.is_file(), f"Missing Visual Studio file: {tracked_vs_file}"
        proc = subprocess.run(
            ["git", "check-ignore", "--no-index", "-q", tracked_vs_file],
            cwd=str(REPO_ROOT),
            check=False,
        )
        assert proc.returncode == 1, f"Expected {tracked_vs_file} NOT to be gitignored"

    sln_text = (REPO_ROOT / "iwa.dev.fcbuild.sln").read_text(encoding="utf-8")
    assert "Microsoft Visual Studio Solution File, Format Version 12.00" in sln_text
    assert '"iwa.dev.fcbuild.pyproj"' in sln_text
    assert ".dcproj" not in sln_text
    for folder_name in (
        "Application",
        "Tests",
        "Database and Migrations",
        "Infrastructure and Containers",
        "Documentation",
        "Configuration",
        "Automation",
    ):
        assert f'"{folder_name}"' in sln_text

    pyproj_text = (REPO_ROOT / "iwa.dev.fcbuild.pyproj").read_text(encoding="utf-8")
    assert "<TestFramework>Pytest</TestFramework>" in pyproj_text
    assert "<UnitTestRootDirectory>tests</UnitTestRootDirectory>" in pyproj_text
    assert "<UnitTestPattern>test_*.py</UnitTestPattern>" in pyproj_text
    assert "<SuppressEnvironmentCreationPrompt>True</SuppressEnvironmentCreationPrompt>" in pyproj_text
    assert '<Interpreter Include=".venv\\">' in pyproj_text
    assert "<StartupFile>scripts\\check_documentation_consistency.py</StartupFile>" in pyproj_text
    assert "fc27_discord_bot.py --run" not in pyproj_text
    assert "fc27_polling_worker.py --run" not in pyproj_text

    vs_doc_text = (REPO_ROOT / "docs" / "VISUAL_STUDIO_SETUP.md").read_text(encoding="utf-8")
    assert "SOURCE_CONTROL_TRACKED_UNMODIFIED" in vs_doc_text
    assert "iwa.dev.fcbuild.sln" in vs_doc_text
    assert "Python Environments" in vs_doc_text

    current_user = os.environ.get("USERNAME", "").strip()
    for rel_file in ("iwa.dev.fcbuild.sln", "iwa.dev.fcbuild.pyproj", "docs/VISUAL_STUDIO_SETUP.md"):
        content = (REPO_ROOT / rel_file).read_text(encoding="utf-8")
        assert "C:\\Users\\" not in content
        assert "/Users/" not in content
        if current_user and len(current_user) >= 3:
            assert f"\\Users\\{current_user}\\".lower() not in content.lower()
            assert f"/Users/{current_user}/".lower() not in content.lower()
        for banned_prefix in (".venv\\", ".vs\\", ".local-data\\", ".local-artifacts\\", "secrets\\"):
            if rel_file == "iwa.dev.fcbuild.pyproj" and banned_prefix == ".venv\\":
                assert '<Compile Include=".venv\\' not in content
                assert '<Content Include=".venv\\' not in content
            elif rel_file in ("iwa.dev.fcbuild.sln", "iwa.dev.fcbuild.pyproj"):
                assert f'Include="{banned_prefix}' not in content
