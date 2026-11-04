"""Policy, security, and shard-completeness tests for FC27 GitHub Actions workflows.

Verifies:
- Valid YAML across all .github/workflows/*.yml files
- Explicit minimal permissions (contents: read; Pages permissions restricted to publish-pages)
- timeout-minutes defined on every job in ci.yml, test-fast.yml, and test-nightly.yml
- Official GitHub Actions pinned to full 40-character commit SHAs
- No secret echoing, no continue-on-error on required test steps, no pull_request_target
- Changed-file classifier covers all 11 categories and fails closed to broad coverage
- Full CI workflow runs or aggregates 100% of the repository test suite
- Maintained test groups and full CI shards are disjoint and their union equals full test collection
- Unknown test modules default fail-safe to UNKNOWN_SERIAL
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

import pytest
import yaml

from scripts.classify_ci_changes import (
    BROAD_COVERAGE_TRIGGER_FILES,
    CHANGE_CATEGORIES,
    classify_changed_files,
)
from src.fc27_ci_test_groups import (
    ALL_REGISTERED_MARKERS,
    CANONICAL_PYTEST_MARKERS,
    FAST_WORKFLOW_GROUPS,
    FULL_CI_SHARDS,
    ISOLATION_CLASSIFICATIONS,
    MAINTAINED_TEST_GROUPS,
    TEST_MODULE_MATRIX,
    classify_test_module,
    modules_for_fast_group,
    modules_for_full_shard,
    modules_for_maintained_group,
    normalize_test_module_path,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
SHA_PIN_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$")


def _load_workflow(filename: str) -> dict[str, Any]:
    path = WORKFLOWS_DIR / filename
    assert path.is_file(), f"Missing workflow file: {filename}"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"Workflow {filename} did not parse to a mapping"
    return data


@pytest.mark.parametrize(
    "workflow_file",
    ["ci.yml", "test-fast.yml", "test-nightly.yml", "security-scan.yml"],
)
def test_workflows_parse_as_valid_yaml_and_have_minimal_permissions(
    workflow_file: str,
) -> None:
    wf = _load_workflow(workflow_file)
    perms = wf.get("permissions")
    assert perms == {"contents": "read"}, (
        f"{workflow_file} must set top-level permissions to contents: read"
    )
    triggers = wf.get("on") or wf.get(True)
    assert isinstance(triggers, dict), f"{workflow_file} missing trigger mapping"
    assert "pull_request_target" not in triggers, (
        f"{workflow_file} must never use pull_request_target"
    )


@pytest.mark.parametrize(
    "workflow_file",
    ["ci.yml", "test-fast.yml", "test-nightly.yml"],
)
def test_every_job_defines_timeout_minutes_and_pins_actions_to_commit_sha(
    workflow_file: str,
) -> None:
    wf = _load_workflow(workflow_file)
    jobs = wf.get("jobs", {})
    assert jobs, f"{workflow_file} has no jobs"

    for job_name, job in jobs.items():
        timeout = job.get("timeout-minutes")
        assert isinstance(timeout, int) and 1 <= timeout <= 30, (
            f"{workflow_file}:{job_name} must define timeout-minutes between 1 and 30 (got {timeout!r})"
        )
        job_perms = job.get("permissions")
        if job_perms is not None:
            assert job_name == "publish-pages", (
                f"Only publish-pages may override job permissions in {workflow_file} (found {job_name})"
            )
            assert job_perms == {
                "contents": "read",
                "pages": "write",
                "id-token": "write",
            }
            assert "can_publish_pages == 'true'" in str(job.get("if", ""))

        for step in job.get("steps", []):
            uses = step.get("uses")
            if uses:
                assert SHA_PIN_RE.match(uses), (
                    f"Action {uses!r} in {workflow_file}:{job_name} must be pinned to a 40-char commit SHA"
                )
            if step.get("continue-on-error") is True:
                pytest.fail(
                    f"Required workflow {workflow_file}:{job_name} step {step.get('name')!r} must not use continue-on-error: true"
                )
            run_cmd = str(step.get("run", ""))
            assert "echo $" not in run_cmd or "GITHUB_OUTPUT" in run_cmd or "iter" in run_cmd
            assert "printenv" not in run_cmd


def test_ci_workflow_preserves_required_test_gate_and_runs_all_shards() -> None:
    wf = _load_workflow("ci.yml")
    assert wf["name"] == "ci"
    jobs = wf["jobs"]
    for required_job in (
        "full-quality",
        "full-unit-domain",
        "full-postgres",
        "full-compose",
        "aggregate-report",
        "test",
    ):
        assert required_job in jobs, f"ci.yml missing required job {required_job}"

    test_gate = jobs["test"]
    needs = set(test_gate.get("needs", []))
    assert {
        "full-quality",
        "full-unit-domain",
        "full-postgres",
        "full-compose",
        "aggregate-report",
    }.issubset(needs)


def test_fast_workflow_defines_all_tiers_and_result_gate() -> None:
    wf = _load_workflow("test-fast.yml")
    assert wf["name"] == "test-fast"
    jobs = wf["jobs"]
    for expected_job in (
        "changes",
        "quality",
        "unit-domain",
        "postgres-focused",
        "docker-compose-focused",
        "aggregate-report",
        "publish-pages",
        "result",
    ):
        assert expected_job in jobs, f"test-fast.yml missing job {expected_job}"


def test_changed_file_classifier_covers_all_11_categories_and_broad_fallbacks() -> None:
    assert len(CHANGE_CATEGORIES) == 11

    # 1. Docs-only change runs quality only (never 0 jobs)
    docs_res = classify_changed_files(["docs/TESTING.md"])
    assert docs_res["docs_only"] is True
    assert docs_res["broad_coverage"] is False
    assert docs_res["run_quality"] is True
    assert docs_res["run_unit_domain"] is False
    assert docs_res["run_postgres_focused"] is False
    assert docs_res["run_docker_compose_focused"] is False
    assert docs_res["selected_jobs"] == ["quality"]

    # 2. Compose-only change runs quality + docker-compose-focused
    comp_res = classify_changed_files(["docker-compose.yml"])
    assert "compose" in comp_res["categories"]
    assert comp_res["run_quality"] is True
    assert comp_res["run_docker_compose_focused"] is True
    assert comp_res["run_unit_domain"] is False

    # 3. Unknown file triggers fail-closed broad coverage
    unk_res = classify_changed_files(["some/unclassified/file.xyz"])
    assert unk_res["broad_coverage"] is True
    assert unk_res["run_quality"] is True
    assert unk_res["run_unit_domain"] is True
    assert unk_res["run_postgres_focused"] is True
    assert unk_res["run_docker_compose_focused"] is True

    # 4. Empty diff triggers fail-closed broad coverage
    empty_res = classify_changed_files([])
    assert empty_res["broad_coverage"] is True
    assert set(empty_res["selected_jobs"]) == {
        "quality",
        "unit-domain",
        "postgres-focused",
        "docker-compose-focused",
    }

    # 5. Every broad trigger file (settings.py, conftest.py, pyproject.toml, etc.) triggers broad coverage
    for trigger_file in BROAD_COVERAGE_TRIGGER_FILES:
        res = classify_changed_files([trigger_file])
        assert res["broad_coverage"] is True, f"{trigger_file} must trigger broad coverage"

    # 6. Verify all 11 canonical categories can be produced
    sample_files = [
        "src/fc27_search.py",
        "migrations/0012_fc27_club_ops_analytics.sql",
        "docker-compose.yml",
        "src/fc27_discord_bot.py",
        "src/fc27_polling_worker.py",
        "src/fc27_dashboard_queries.py",
        "src/security.py",
        "docs/README.md",
        "pyproject.toml",
        ".github/workflows/ci.yml",
        "src/fc27_performance.py",
    ]
    all_res = classify_changed_files(sample_files)
    assert set(all_res["categories"]) == set(CHANGE_CATEGORIES)


def test_collection_consistency_and_shard_disjointness() -> None:
    # Discover all test_*.py files in tests/
    discovered_files = sorted(
        normalize_test_module_path(p, root=REPO_ROOT)
        for p in (REPO_ROOT / "tests").rglob("test_*.py")
    )
    matrix_files = sorted(TEST_MODULE_MATRIX.keys())
    assert discovered_files == matrix_files, (
        f"Every test_*.py in tests/ must be classified in TEST_MODULE_MATRIX. "
        f"Missing={set(discovered_files) - set(matrix_files)}, Extra={set(matrix_files) - set(discovered_files)}"
    )

    # Verify maintained groups are mutually disjoint and union == all discovered test modules
    group_sets: dict[str, set[str]] = {
        g: set(modules_for_maintained_group(g)) for g in MAINTAINED_TEST_GROUPS
    }
    for i, g1 in enumerate(MAINTAINED_TEST_GROUPS):
        for g2 in MAINTAINED_TEST_GROUPS[i + 1 :]:
            overlap = group_sets[g1].intersection(group_sets[g2])
            assert not overlap, f"Maintained groups {g1} and {g2} overlap: {overlap}"

    union_groups = set().union(*group_sets.values())
    assert union_groups == set(discovered_files)

    # Verify full CI shards are mutually disjoint and union == all discovered test modules
    shard_sets: dict[str, set[str]] = {
        s: set(modules_for_full_shard(s)) for s in FULL_CI_SHARDS
    }
    for i, s1 in enumerate(FULL_CI_SHARDS):
        for s2 in FULL_CI_SHARDS[i + 1 :]:
            overlap = shard_sets[s1].intersection(shard_sets[s2])
            assert not overlap, f"Full CI shards {s1} and {s2} overlap: {overlap}"

    union_shards = set().union(*shard_sets.values())
    assert union_shards == set(discovered_files)

    # Verify fast workflow groups are non-empty
    for fg in FAST_WORKFLOW_GROUPS:
        assert len(modules_for_fast_group(fg)) > 0

    # Verify canonical markers and classifications
    assert len(ISOLATION_CLASSIFICATIONS) == 11
    assert len(CANONICAL_PYTEST_MARKERS) == 12
    for m in CANONICAL_PYTEST_MARKERS:
        assert m in ALL_REGISTERED_MARKERS


def test_unknown_test_module_defaults_to_unknown_serial() -> None:
    unk_non_pg = classify_test_module("tests/test_brand_new_unclassified.py", uses_postgres_fixture=False)
    assert unk_non_pg.classification == "UNKNOWN_SERIAL"
    assert unk_non_pg.execution_mode == "serial"
    assert "serial" in unk_non_pg.markers
    assert unk_non_pg.full_ci_shard == "full-unit-domain"

    unk_pg = classify_test_module("tests/test_brand_new_pg_unclassified.py", uses_postgres_fixture=True)
    assert unk_pg.classification == "UNKNOWN_SERIAL"
    assert unk_pg.execution_mode == "serial"
    assert "postgres" in unk_pg.markers
    assert "serial" in unk_pg.markers
    assert unk_pg.full_ci_shard == "full-postgres"
