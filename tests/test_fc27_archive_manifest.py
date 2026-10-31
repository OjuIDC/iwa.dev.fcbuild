from __future__ import annotations

import ast
import json
from pathlib import Path

from scripts import check_documentation_consistency as dc
from src.fc27_analyst import build_fc27_analyst_report
from src.fc27_community_provider import (
    COMMUNITY_PROVIDER_ID,
    LIVE_EA_CLUBS_COMMUNITY_CLIENT,
    FC27CommunityProviderAdapter,
    FC27CommunityProviderDeclaration,
    persist_community_provider_batch,
)
from src.fc27_demo_api import create_demo_app
from src.fc27_repository import apply_export_plan
from src.fc27_search_scout import build_fc27_scout_report, build_fc27_search_context

REPO_ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_MANIFEST_PATH = REPO_ROOT / "config" / "fc27_archive_manifest.json"
REMOVED_MANIFEST_PATH = REPO_ROOT / "config" / "fc27_removed_items_manifest.json"


def _load_archive_manifest() -> dict:
    return json.loads(ARCHIVE_MANIFEST_PATH.read_text(encoding="utf-8"))


def _load_removed_manifest() -> dict:
    return json.loads(REMOVED_MANIFEST_PATH.read_text(encoding="utf-8"))


def test_01_archive_manifest_parses() -> None:
    data = _load_archive_manifest()
    assert isinstance(data, dict)
    assert "archived_items" in data
    assert isinstance(data["archived_items"], list)
    assert len(data["archived_items"]) >= 23


def test_02_archive_manifest_version_correct() -> None:
    data = _load_archive_manifest()
    assert data.get("manifest_version") == "fc27-archive-manifest-v1"
    provider = data.get("current_operational_provider", {})
    assert provider.get("provider_id") == "fc-clubs-api-community-v1"
    assert provider.get("source_classification") == "LIVE_EA_CLUBS_COMMUNITY_CLIENT"
    assert provider.get("club_id") == "330323"
    assert provider.get("club_name") == "East Coasters"
    assert provider.get("platform") == "common-gen5"


def test_03_original_paths_absent_when_moved() -> None:
    data = _load_archive_manifest()
    for item in data["archived_items"]:
        orig = item["original_path"]
        arch = item["archived_path"]
        if orig != arch:
            assert not (REPO_ROOT / orig).exists(), f"Moved original path still exists: {orig}"


def test_04_archived_paths_exist() -> None:
    data = _load_archive_manifest()
    for item in data["archived_items"]:
        arch = item["archived_path"]
        assert (REPO_ROOT / arch).is_file(), f"Archived path missing: {arch}"


def test_05_archived_markdown_has_notice() -> None:
    archive_dir = REPO_ROOT / "docs" / "archive"
    exempt = {
        (archive_dir / "README.md").resolve(),
        (archive_dir / "ARCHIVE_MANIFEST.md").resolve(),
    }
    md_files = [p for p in archive_dir.rglob("*.md") if p.resolve() not in exempt]
    assert len(md_files) >= 22
    for md_path in md_files:
        head = "\n".join(md_path.read_text(encoding="utf-8").splitlines()[:15])
        assert "**Archived: historical reference only.**" in head, f"Missing bold notice in {md_path}"
        assert "non-authoritative" in head, f"Missing non-authoritative notice in {md_path}"
        assert "Operational status: DO NOT USE" in head, f"Missing DO NOT USE in {md_path}"
        assert "Replacement:" in head, f"Missing Replacement in {md_path}"
        assert "Archive reason:" in head, f"Missing Archive reason in {md_path}"


def test_06_archived_scripts_have_notice() -> None:
    scripts_archive = REPO_ROOT / "scripts" / "archive"
    py_files = list(scripts_archive.rglob("*.py"))
    assert len(py_files) >= 1
    for py_path in py_files:
        head = "\n".join(py_path.read_text(encoding="utf-8").splitlines()[:10])
        assert "# ARCHIVED: HISTORICAL REFERENCE ONLY" in head
        assert "# NON-OPERATIONAL" in head
        assert "# DO NOT RUN" in head
        assert "# REPLACED BY:" in head
        assert "# ARCHIVED ON:" in head
        assert "# ARCHIVE REASON:" in head


def test_07_archived_operational_status_valid() -> None:
    data = _load_archive_manifest()
    allowed = {
        "DO_NOT_USE",
        "HISTORICAL_REFERENCE",
        "SUPERSEDED",
        "EXPERIMENTAL_NOT_ACTIVATABLE",
    }
    assert set(data.get("allowed_operational_statuses", [])) == allowed
    for item in data["archived_items"]:
        assert item["operational_status"] in allowed, f"Invalid status on {item['archive_id']}"
        assert item["runtime_reachable_after"] is False


def test_08_replacements_exist_or_are_none() -> None:
    data = _load_archive_manifest()
    for item in data["archived_items"]:
        repl = item["replacement"]
        if repl != "NONE":
            assert (REPO_ROOT / repl).exists(), f"Missing replacement {repl} for {item['archive_id']}"


def test_09_archived_scripts_not_imported() -> None:
    active_roots = [
        REPO_ROOT / "src",
        REPO_ROOT / "tests",
        REPO_ROOT / "host_agent",
        REPO_ROOT / "dashboard",
    ]
    py_files: list[Path] = []
    for root in active_roots:
        if root.exists():
            py_files.extend(root.rglob("*.py"))
    for p in (REPO_ROOT / "scripts").glob("*.py"):
        py_files.append(p)

    for py_file in py_files:
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "matchpulse_conformance" not in alias.name
                    assert "scripts.archive" not in alias.name
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert "matchpulse_conformance" not in node.module
                assert "scripts.archive" not in node.module


def test_10_archived_scripts_not_called_by_workflows() -> None:
    wf_dir = REPO_ROOT / ".github" / "workflows"
    for wf in wf_dir.glob("*.yml"):
        text = wf.read_text(encoding="utf-8")
        assert "scripts/archive/" not in text
        assert "matchpulse_conformance.py" not in text


def test_11_archived_scripts_not_called_by_docker() -> None:
    for df in REPO_ROOT.glob("Dockerfile*"):
        text = df.read_text(encoding="utf-8")
        assert "scripts/archive/" not in text
        assert "matchpulse_conformance.py" not in text


def test_12_archived_scripts_not_called_by_compose() -> None:
    for cf in REPO_ROOT.glob("docker-compose*.yml"):
        text = cf.read_text(encoding="utf-8")
        assert "scripts/archive/" not in text
        assert "matchpulse_conformance.py" not in text


def test_13_archived_documents_not_linked_as_current() -> None:
    report = dc.validate_repository(REPO_ROOT, include_archive=False)
    archive_citation_errors = [
        r for r in report.results if r.reason_code == "ARCHIVED_DOC_CITED_AS_CURRENT"
    ]
    assert not archive_citation_errors


def test_14_current_docs_index_complete() -> None:
    docs_readme = (REPO_ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    for doc_path in sorted((REPO_ROOT / "docs").glob("*.md")):
        if doc_path.name == "README.md":
            continue
        assert doc_path.name in docs_readme, f"{doc_path.name} missing from docs/README.md"


def test_15_archive_index_complete() -> None:
    archive_readme = (REPO_ROOT / "docs" / "archive" / "README.md").read_text(encoding="utf-8")
    archive_manifest_md = (REPO_ROOT / "docs" / "archive" / "ARCHIVE_MANIFEST.md").read_text(
        encoding="utf-8"
    )
    data = _load_archive_manifest()
    for item in data["archived_items"]:
        arch_name = Path(item["archived_path"]).name
        assert arch_name in archive_readme, f"{arch_name} missing from docs/archive/README.md"
        assert item["archived_path"] in archive_manifest_md, (
            f"{item['archived_path']} missing from docs/archive/ARCHIVE_MANIFEST.md"
        )


def test_16_removed_items_manifest_parses() -> None:
    data = _load_removed_manifest()
    assert data.get("manifest_version") == "fc27-removed-items-manifest-v1"
    assert isinstance(data.get("removed_items"), list)
    assert len(data["removed_items"]) >= 3


def test_17_removed_paths_absent() -> None:
    data = _load_removed_manifest()
    for item in data["removed_items"]:
        assert not (REPO_ROOT / item["path"]).exists(), f"Removed path still exists: {item['path']}"


def test_18_deletion_proof_populated() -> None:
    data = _load_removed_manifest()
    required_fields = {
        "path",
        "classification",
        "proof_summary",
        "replacement",
        "last_commit",
        "reason",
        "tests_run",
        "rollback_method",
    }
    for item in data["removed_items"]:
        assert required_fields.issubset(item.keys())
        assert item["proof_summary"].strip()
        assert item["last_commit"].strip()
        assert item["rollback_method"].strip()
        if item["replacement"] != "NONE":
            assert (REPO_ROOT / item["replacement"]).exists()


def test_19_current_community_provider_present() -> None:
    assert COMMUNITY_PROVIDER_ID == "fc-clubs-api-community-v1"
    assert LIVE_EA_CLUBS_COMMUNITY_CLIENT == "LIVE_EA_CLUBS_COMMUNITY_CLIENT"
    assert callable(FC27CommunityProviderAdapter)


def test_20_current_community_provider_bounded() -> None:
    decl = FC27CommunityProviderDeclaration()
    assert decl.provider_id == "fc-clubs-api-community-v1"
    assert decl.club_id == "330323"
    assert decl.club_name == "East Coasters"
    assert decl.platform == "common-gen5"
    assert decl.output_format == "records"
    assert decl.maximum_identity_requests == 1
    assert decl.maximum_match_requests == 1
    assert decl.maximum_retries == 0
    assert decl.maximum_matches <= 10


def test_21_current_dashboard_present() -> None:
    from fastapi.testclient import TestClient

    app = create_demo_app(demo_mode_override=False)
    with TestClient(app) as client:
        for path in ("/demo", "/demo/status", "/demo/matches", "/demo/analysis"):
            resp = client.get(path)
            assert resp.status_code == 403
            assert resp.json()["detail"]["error"] == "FC27_DEMO_MODE_DISABLED"


def test_22_search_present() -> None:
    assert callable(build_fc27_search_context)


def test_23_scout_present() -> None:
    assert callable(build_fc27_scout_report)


def test_24_analyst_present() -> None:
    assert callable(build_fc27_analyst_report)


def test_25_current_persistence_present() -> None:
    assert callable(persist_community_provider_batch)
    assert callable(apply_export_plan)
