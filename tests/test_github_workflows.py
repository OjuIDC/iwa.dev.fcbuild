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

from scripts.build_container_report import (
    aggregate_container_reports,
    build_single_image_report,
    check_dockerignore_excludes,
    parse_spdx_sbom,
    parse_trivy_vulnerabilities,
    scan_history_and_env_for_secrets,
    scan_image_filesystem_tar_names,
)
from scripts.classify_ci_changes import (
    BROAD_COVERAGE_TRIGGER_FILES,
    CHANGE_CATEGORIES,
    classify_changed_files,
)
from scripts.sanitize_test_report import sanitize_text_for_report, scan_text_content
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


def test_container_images_workflow_yaml_permissions_pins_and_timeouts() -> None:
    wf = _load_workflow("container-images.yml")
    assert wf["name"] == "container-images"
    assert wf.get("permissions") == {"contents": "read", "packages": "read"}

    triggers = wf.get("on") or wf.get(True)
    assert isinstance(triggers, dict)
    assert "pull_request_target" not in triggers
    assert "pull_request" in triggers
    assert "push" in triggers
    assert "workflow_dispatch" in triggers

    jobs = wf.get("jobs", {})
    assert set(jobs.keys()) == {
        "gate-context",
        "build-and-scan",
        "container-report",
        "publish-ghcr",
        "result",
    }

    for job_name, job in jobs.items():
        timeout = job.get("timeout-minutes")
        assert isinstance(timeout, int) and 1 <= timeout <= 30, (
            f"container-images.yml:{job_name} must define timeout-minutes"
        )
        perms = job.get("permissions", {})
        if job_name != "publish-ghcr":
            assert perms.get("packages") != "write", (
                f"PR-reachable job {job_name} must never have packages: write"
            )
            assert perms.get("id-token") != "write", (
                f"PR-reachable job {job_name} must never have id-token: write"
            )
        else:
            assert perms == {
                "contents": "read",
                "actions": "read",
                "packages": "write",
                "id-token": "write",
                "attestations": "write",
            }
            if_cond = str(job.get("if", ""))
            assert "github.event_name != 'pull_request'" in if_cond
            assert "github.repository == 'iwa-ojuidc/iwa.dev.fcbuild'" in if_cond
            assert "needs.gate-context.outputs.allow_publish == 'true'" in if_cond
            assert "needs.build-and-scan.result == 'success'" in if_cond
            assert "needs.container-report.result == 'success'" in if_cond

        for step in job.get("steps", []):
            uses = step.get("uses")
            if uses:
                assert SHA_PIN_RE.match(uses), (
                    f"Action {uses!r} in container-images.yml:{job_name} must be pinned to a 40-char commit SHA"
                )
            if job_name != "publish-ghcr" and uses:
                assert not uses.startswith("docker/login-action@"), (
                    f"PR-reachable job {job_name} must never authenticate to ghcr.io"
                )


def test_container_workflow_build_push_steps_and_metadata_tag_rules() -> None:
    wf = _load_workflow("container-images.yml")
    jobs = wf["jobs"]

    for job_name in ("build-and-scan", "publish-ghcr"):
        matrix_inc = jobs[job_name]["strategy"]["matrix"]["include"]
        images = {entry["image"]: entry["dockerfile"] for entry in matrix_inc}
        assert images == {
            "fc27-community-worker": "Dockerfile.community-worker",
            "fc27-discord-bot": "Dockerfile.discord",
        }

        build_steps = [
            s
            for s in jobs[job_name]["steps"]
            if str(s.get("uses", "")).startswith("docker/build-push-action@")
        ]
        assert len(build_steps) == 1
        with_cfg = build_steps[0]["with"]
        for req_key in (
            "context",
            "file",
            "platforms",
            "push",
            "tags",
            "labels",
            "cache-from",
            "cache-to",
        ):
            assert req_key in with_cfg, f"{job_name} build-push-action missing {req_key}"
        for forbidden_key in ("build-args", "secrets", "ssh"):
            assert forbidden_key not in with_cfg, (
                f"{job_name} build-push-action must not pass {forbidden_key}"
            )
        assert "env" not in build_steps[0], (
            f"{job_name} build-push-action must not pass operational env secrets"
        )
        if job_name == "build-and-scan":
            assert with_cfg["push"] is False
            assert with_cfg["load"] is True
        else:
            assert with_cfg["push"] is True
            assert with_cfg["sbom"] is True
            assert with_cfg["provenance"] == "mode=max"

        meta_steps = [
            s
            for s in jobs[job_name]["steps"]
            if str(s.get("uses", "")).startswith("docker/metadata-action@")
        ]
        assert len(meta_steps) == 1
        tags_raw = str(meta_steps[0]["with"]["tags"])
        assert "latest" not in tags_raw, "latest tag must not be implicitly published"
        assert "type=sha,prefix=sha-,format=short" in tags_raw
        assert "pattern=v{{version}}" in tags_raw
        assert "pattern={{major}}.{{minor}}" in tags_raw
        assert "pattern={{major}}" in tags_raw

    # Check attestation and cosign steps on publish-ghcr
    publish_uses = [str(s.get("uses", "")) for s in jobs["publish-ghcr"]["steps"]]
    assert any(u.startswith("actions/attest-build-provenance@") for u in publish_uses)
    assert any(u.startswith("sigstore/cosign-installer@") for u in publish_uses)


def test_container_semver_tag_validation_and_gate_simulation() -> None:
    semver_re = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
    for invalid_tag in ("v1", "release-1", "latest-test", "v1.2.3-secret", "1.2.3", "v01.2.3"):
        assert not semver_re.match(invalid_tag), f"Invalid tag {invalid_tag} must be rejected"
    for valid_tag in ("v0.1.0", "v1.2.3", "v2.0.0"):
        assert semver_re.match(valid_tag), f"Valid semver tag {valid_tag} must be accepted"


def test_dockerignore_and_dockerfile_non_root_secret_free_posture() -> None:
    dockerignore_path = REPO_ROOT / ".dockerignore"
    passed, missing = check_dockerignore_excludes(dockerignore_path)
    assert passed is True, f".dockerignore missing required patterns: {missing}"

    text = dockerignore_path.read_text(encoding="utf-8")
    for pattern in ("*.pem", "*.key", "*.pfx", "*.kdbx", "*.sql", "*.sql.gz", ".git", ".venv"):
        assert pattern in text, f".dockerignore must exclude {pattern}"

    for df_name, expected_cmd_token in (
        ("Dockerfile.community-worker", "--status"),
        ("Dockerfile.discord", "--status"),
    ):
        df_text = (REPO_ROOT / df_name).read_text(encoding="utf-8")
        assert "USER 10001:65534" in df_text
        assert expected_cmd_token in df_text
        if df_name == "Dockerfile.community-worker":
            assert "FC27_COMMUNITY_POLL_INTERVAL_SECONDS=43200" in df_text
        for line in df_text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("ARG ", "ENV ")):
                assert not any(
                    sec in stripped.upper()
                    for sec in ("PASSWORD", "TOKEN", "SECRET", "API_KEY", "BEARER", "PRIVATE_KEY")
                ), f"{df_name} must not define secret ARG/ENV: {stripped}"

    # Verify compose files preserve local build targets and require no GHCR login
    worker_compose = yaml.safe_load(
        (REPO_ROOT / "docker-compose.community-worker.yml").read_text(encoding="utf-8")
    )
    discord_compose = yaml.safe_load(
        (REPO_ROOT / "docker-compose.discord.yml").read_text(encoding="utf-8")
    )
    w_svc = worker_compose["services"]["fc27-community-worker"]
    d_svc = discord_compose["services"]["fc27-discord-bot"]
    assert w_svc["build"]["dockerfile"] == "Dockerfile.community-worker"
    assert d_svc["build"]["dockerfile"] == "Dockerfile.discord"
    assert str(w_svc["user"]) == "10001:65534"
    assert str(d_svc["user"]) == "10001:65534"
    assert w_svc["read_only"] is True
    assert d_svc["read_only"] is True
    assert w_svc["cap_drop"] == ["ALL"]
    assert d_svc["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in w_svc["security_opt"]
    assert "no-new-privileges:true" in d_svc["security_opt"]


def test_container_secret_scanners_trivy_gate_and_report_sanitization(tmp_path: Path) -> None:
    # 1. Image history & env secret scanner fails closed on leaked secrets
    clean_ok, clean_hits = scan_history_and_env_for_secrets(
        ["/bin/sh -c #(nop) USER 10001:65534"],
        ["PATH=/usr/local/bin:/usr/bin", "FC27_COMMUNITY_POLL_INTERVAL_SECONDS=43200"],
    )
    assert clean_ok is True
    assert clean_hits == []

    bad_ok, bad_hits = scan_history_and_env_for_secrets(
        ["/bin/sh -c ENV DISCORD_BOT_TOKEN=secret123"],
        ["POSTGRES_PASSWORD=supersecret"],
    )
    assert bad_ok is False
    assert len(bad_hits) >= 2

    # 2. Image filesystem scanner fails closed on .env, secrets/, .local-data/, .local-artifacts/, *.7z
    fs_ok, fs_hits = scan_image_filesystem_tar_names(
        ["app/src/fc27_polling_worker.py", "app/pyproject.toml"]
    )
    assert fs_ok is True
    assert fs_hits == []

    for forbidden_member in (
        "app/.env",
        "app/secrets/postgres_password.txt",
        "app/.local-data/state.db",
        "app/.local-artifacts/ci/out.json",
        "app/fc27-secret-recovery.7z",
    ):
        ok, hits = scan_image_filesystem_tar_names([forbidden_member])
        assert ok is False, f"Filesystem scanner must reject {forbidden_member}"
        assert len(hits) == 1

    # 3. Trivy vulnerability parser and Critical/High fail-closed gate
    trivy_clean = tmp_path / "trivy-clean.json"
    trivy_clean.write_text('{"Results": [{"Vulnerabilities": [{"Severity": "LOW"}]}]}', encoding="utf-8")
    v_clean = parse_trivy_vulnerabilities(trivy_clean)
    assert v_clean["trivy_high_critical_gate"] == "PASS"
    assert v_clean["LOW"] == 1

    trivy_crit = tmp_path / "trivy-crit.json"
    trivy_crit.write_text('{"Results": [{"Vulnerabilities": [{"Severity": "CRITICAL"}]}]}', encoding="utf-8")
    v_crit = parse_trivy_vulnerabilities(trivy_crit)
    assert v_crit["trivy_high_critical_gate"] == "FAIL"

    trivy_os_high = tmp_path / "trivy-os-high.json"
    trivy_os_high.write_text(
        '{"Results": [{"Class": "os-pkgs", "Type": "debian", "Vulnerabilities": [{"Severity": "HIGH", "FixedVersion": "1.2"}]}]}',
        encoding="utf-8",
    )
    v_os_active = parse_trivy_vulnerabilities(trivy_os_high, reference_date="2026-11-04")
    assert v_os_active["trivy_high_critical_gate"] == "FAIL"
    assert v_os_active["waiver_id"] == "NONE"
    assert v_os_active["HIGH_FIXED_OS_WAIVED"] == 0
    assert v_os_active["HIGH_FIXED_UNWAIVED"] == 1

    v_os_expired = parse_trivy_vulnerabilities(trivy_os_high, reference_date="2027-01-01")
    assert v_os_expired["trivy_high_critical_gate"] == "FAIL"

    trivy_py_high = tmp_path / "trivy-py-high.json"
    trivy_py_high.write_text(
        '{"Results": [{"Class": "lang-pkgs", "Type": "pip", "Vulnerabilities": [{"Severity": "HIGH", "FixedVersion": "2.0"}]}]}',
        encoding="utf-8",
    )
    v_py_high = parse_trivy_vulnerabilities(trivy_py_high, reference_date="2026-11-04")
    assert v_py_high["trivy_high_critical_gate"] == "FAIL"
    assert v_py_high["waiver_id"] == "NONE"

    # 4. SPDX SBOM parser and sanitized report generation
    sbom_file = tmp_path / "sbom.spdx.json"
    sbom_file.write_text(
        '{"spdxVersion": "SPDX-2.3", "packages": [{"name": "python"}, {"name": "psycopg"}]}',
        encoding="utf-8",
    )
    sbom_res = parse_spdx_sbom(sbom_file)
    assert sbom_res["sbom_generation"] == "PASS"
    assert sbom_res["package_count"] == 2

    validation_data = {
        "image_name": "fc27-community-worker",
        "dockerfile": "Dockerfile.community-worker",
        "base_image": "python:3.11.18-slim@sha256:064f01707f2fc8b06bae86bb43875266c6e41bac7994e77ec5dc623c4a4d0545",
        "target_platform": "linux/amd64",
        "size_bytes": 185000000,
        "local_digest": "sha256:" + "a" * 64,
        "runtime_user": "10001:65534",
        "user_non_root_pass": True,
        "dockerignore_pass": True,
        "image_secret_scan": "PASS",
        "secrets_in_image_history": 0,
        "secrets_in_image_filesystem": 0,
        "smoke_test_result": "PASS",
        "read_only_rootfs_pass": True,
        "poll_interval_seconds": "43200",
        "validation_gate": "PASS",
    }
    report_out = tmp_path / "report-worker"
    summary = build_single_image_report(
        validation=validation_data,
        trivy_counts=v_clean,
        sbom_info=sbom_res,
        build_duration_seconds=12.4,
        publication_status="not published (PR mode)",
        planned_tags=["not published (PR mode)"],
        output_dir=report_out,
    )
    assert summary["overall_gate"] == "PASS"
    agg_out = tmp_path / "agg"
    agg = aggregate_container_reports(
        input_dir=tmp_path,
        output_dir=agg_out,
        workflow_name="container-images",
    )
    assert agg["overall_status"] == "PASS"
    html_text = sanitize_text_for_report((agg_out / "index.html").read_text(encoding="utf-8"))
    scan = scan_text_content(html_text, source_label="index.html", is_html=True)
    assert scan["status"] == "PASS"
    assert scan["findings"] == []


def test_base_image_waiver_remediation_contracts_and_operational_overlay_unchanged() -> None:
    """Verifies Part M base-image digest pin, waiver removal, and untouched operational GHCR overlay."""
    from scripts.build_container_report import (
        BASE_IMAGE_OS_WAIVER_POLICY,
        EXPECTED_IMAGE_CONTRACTS,
    )

    expected_base = (
        "python:3.11.18-slim@sha256:064f01707f2fc8b06bae86bb43875266c6e41bac7994e77ec5dc623c4a4d0545"
    )
    assert re.match(r"^python:3\.11\.\d+-slim@sha256:[0-9a-f]{64}$", expected_base)

    # 1. Both production Dockerfiles and EXPECTED_IMAGE_CONTRACTS pin the exact official Python 3.11.18-slim digest
    for image_name, df_name in (
        ("fc27-community-worker", "Dockerfile.community-worker"),
        ("fc27-discord-bot", "Dockerfile.discord"),
    ):
        contract = EXPECTED_IMAGE_CONTRACTS[image_name]
        assert contract["base_image"] == expected_base
        assert contract["user"] == "10001:65534"
        df_text = (REPO_ROOT / df_name).read_text(encoding="utf-8")
        assert df_text.startswith(f"FROM {expected_base}\n")
        assert "USER 10001:65534" in df_text
        assert '"pip>=26.1.2"' in df_text
        assert '"setuptools>=84.2.1"' in df_text
        assert '"wheel>=0.48.0"' in df_text

    # 2. The expired-risk waiver is removed and no replacement waiver is introduced
    assert BASE_IMAGE_OS_WAIVER_POLICY["waiver_id"] == "NONE"
    assert BASE_IMAGE_OS_WAIVER_POLICY["expires_on"] == "NONE"
    assert BASE_IMAGE_OS_WAIVER_POLICY["max_os_high_fixed"] == 0
    assert BASE_IMAGE_OS_WAIVER_POLICY["waived_base_python_build_pkgs"] == ()
    assert BASE_IMAGE_OS_WAIVER_POLICY["allow_critical"] is False
    assert BASE_IMAGE_OS_WAIVER_POLICY["allow_library_high_fixed"] is False
    report_script_text = (REPO_ROOT / "scripts" / "build_container_report.py").read_text(
        encoding="utf-8"
    )
    assert "WAIVER-FC27-BASE-IMAGE-DEBIAN-3.11.16-SLIM" not in report_script_text

    # 3. Operational GHCR runtime overlay digests remain untouched during candidate evaluation
    ghcr_overlay = yaml.safe_load(
        (REPO_ROOT / "docker-compose.ghcr.yml").read_text(encoding="utf-8")
    )
    services = ghcr_overlay["services"]
    assert (
        services["fc27-discord-bot"]["image"]
        == "ghcr.io/iwa-ojuidc/iwa.dev.fcbuild/fc27-discord-bot@sha256:b8b571088b9a490fa4a1a517c60e1f43305ad863f3ecea9fc5df636541e7e084"
    )
    assert (
        services["fc27-community-worker"]["image"]
        == "ghcr.io/iwa-ojuidc/iwa.dev.fcbuild/fc27-community-worker@sha256:8628acc26c9c73d9360a5e47d66c10c988e03d8273b5f15bb1f7cd3e1dc1fb79"
    )

