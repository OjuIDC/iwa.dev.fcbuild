#!/usr/bin/env python3
"""Repository-owned changed-file classifier and trusted-context evaluator for FC27 CI.

Classifies changed files across the 11 canonical FC27 CI categories without
requiring repository secrets or third-party actions, enforces fail-closed broad
coverage fallback, and computes trusted-context and GitHub Pages eligibility.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Final

CHANGE_CATEGORIES: Final[tuple[str, ...]] = (
    "python-runtime",
    "database",
    "compose",
    "discord",
    "polling",
    "analytics",
    "security",
    "documentation",
    "packaging",
    "workflow",
    "performance",
)

BROAD_COVERAGE_TRIGGER_FILES: Final[frozenset[str]] = frozenset(
    {
        "src/settings.py",
        "src/fc27_ci_test_groups.py",
        "tests/conftest.py",
        "pyproject.toml",
        "requirements.txt",
        "requirements-dev.txt",
        "config.example.env",
    }
)


def classify_single_path(rel_path: str) -> tuple[set[str], bool]:
    """Returns (categories, triggers_broad_coverage) for a normalized POSIX path."""
    norm = rel_path.strip().replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    norm = norm.lstrip("/")
    if not norm:
        return set(), True

    cats: set[str] = set()
    broad = norm in BROAD_COVERAGE_TRIGGER_FILES

    # 1. Workflows & CI scripts
    if (
        norm.startswith(".github/workflows/")
        or norm.startswith(".github/")
        or norm in {
            "scripts/classify_ci_changes.py",
            "scripts/build_test_report.py",
            "scripts/build_container_report.py",
            "scripts/sanitize_test_report.py",
            "scripts/aggregate_test_reports.py",
            "tests/test_github_workflows.py",
            "tests/test_ci_report_sanitization.py",
        }
    ):
        cats.add("workflow")
        broad = True

    # 2. Packaging & dependency files
    if (
        norm == "pyproject.toml"
        or (norm.startswith("requirements") and norm.endswith(".txt"))
        or norm == "THIRD_PARTY_NOTICES.md"
        or norm == "LICENSE"
        or norm == "tests/test_packaging.py"
    ):
        cats.add("packaging")
        broad = True

    # 3. Docker & Compose files
    if (
        norm.startswith("Dockerfile")
        or norm.startswith("docker-compose")
        or norm in {".dockerignore", "tests/test_compose_isolation.py"}
    ):
        cats.add("compose")

    # 4. Database, migrations, repository, backup/restore
    if (
        norm.startswith("migrations/")
        or norm in {
            "src/db_migrate.py",
            "src/db_roles.py",
            "src/fc27_repository.py",
            "src/fc27_match_results.py",
            "src/fc27_backup_restore.py",
            "src/approval_store.py",
            "src/events.py",
            "scripts/fc27_backup.py",
            "scripts/fc27_restore_verify.py",
            "scripts/test_postgres.sh",
            "tests/test_db_roles.py",
            "tests/test_db_migrate_roles.py",
            "tests/test_fc27_persistence_and_queries.py",
            "tests/test_fc27_match_results.py",
            "tests/test_fc27_backup_restore.py",
            "tests/test_migration_0003.py",
            "tests/test_events_outbox.py",
        }
        or norm.startswith("src/dispatch/")
        or norm.startswith("tests/dispatch/")
    ):
        cats.add("database")

    # 5. Discord
    if "discord" in norm or "match_results" in norm:
        cats.add("discord")

    # 6. Polling
    if "polling" in norm or "community_worker" in norm or "community-worker" in norm:
        cats.add("polling")

    # 7. Analytics & advisory pipeline
    if any(
        token in norm
        for token in (
            "analytics",
            "dashboard_queries",
            "search",
            "scout",
            "analyst",
            "coach",
            "tactics",
            "director",
            "advisory",
            "pandas_view",
        )
    ):
        cats.add("analytics")

    # 8. Performance
    if "performance" in norm:
        cats.add("performance")

    # 9. Security, identity, approval, provider contracts, adversarial
    if (
        norm.startswith("src/identity/")
        or norm.startswith("tests/identity/")
        or norm.startswith("tests/adversarial/")
        or "adversarial" in norm
        or norm in {
            "src/security.py",
            "src/approval.py",
            "src/fc27_clubs_api_contracts.py",
            "src/fc27_community_provider.py",
            "src/fc27_privacy.py",
            "scripts/check_matchpulse_boundary.py",
            "tests/test_approval_security.py",
            "tests/test_fc27_privacy.py",
        }
    ):
        cats.add("security")

    # 10. Documentation & governance manifests
    if (
        norm.startswith("docs/")
        or norm.endswith(".md")
        or norm in {
            "scripts/check_documentation_consistency.py",
            "config/documentation_consistency_policy.json",
            "config/documentation_waivers.json",
            "config/fc27_archive_manifest.json",
            "config/fc27_removed_items_manifest.json",
            "tests/test_documentation_consistency.py",
            "tests/test_fc27_archive_manifest.py",
            "tests/test_fc27_repository_cleanup.py",
            "tests/test_archive_integrity.py",
        }
    ):
        cats.add("documentation")

    # 11. Python runtime / application code & tests
    if (
        norm.startswith("src/")
        or (
            norm.startswith("scripts/")
            and not norm.endswith(".md")
            and norm not in {
                "scripts/check_documentation_consistency.py",
                "scripts/classify_ci_changes.py",
                "scripts/build_test_report.py",
                "scripts/sanitize_test_report.py",
                "scripts/aggregate_test_reports.py",
            }
        )
        or (
            norm.startswith("tests/")
            and norm not in {
                "tests/test_documentation_consistency.py",
                "tests/test_fc27_archive_manifest.py",
                "tests/test_fc27_repository_cleanup.py",
                "tests/test_archive_integrity.py",
                "tests/test_packaging.py",
                "tests/test_compose_isolation.py",
                "tests/test_github_workflows.py",
                "tests/test_ci_report_sanitization.py",
            }
        )
    ):
        cats.add("python-runtime")

    if not cats:
        # Unknown path: fail-closed broad coverage
        broad = True

    return cats, broad


def evaluate_trusted_context(
    *,
    event_name: str,
    ref: str,
    repository: str,
    head_repo_full_name: str = "",
    head_repo_fork: bool = False,
    enable_pages_var: str = "false",
) -> dict[str, Any]:
    """Determines whether the current CI execution is in a trusted context and whether Pages is approved."""
    evt = (event_name or "").strip()
    repo = (repository or "").strip()
    head_repo = (head_repo_full_name or "").strip()

    if evt == "pull_request_target":
        trusted = False
    elif evt in ("push", "workflow_dispatch", "schedule"):
        trusted = bool(repo) and not head_repo_fork
    elif evt == "pull_request":
        trusted = (
            bool(repo)
            and bool(head_repo)
            and head_repo == repo
            and not head_repo_fork
        )
    else:
        trusted = False

    pages_approved = (enable_pages_var or "").strip().lower() == "true"
    pages_decision = (
        "APPROVED_PRIVATE_OR_ACCEPTABLE"
        if pages_approved
        else "NOT_APPROVED_USE_JOB_SUMMARY_ONLY"
    )
    can_publish_pages = bool(trusted and pages_approved)
    browser_preview = (
        "PAGES_AND_JOB_SUMMARY" if can_publish_pages else "JOB_SUMMARY_ONLY"
    )

    return {
        "trusted_context": trusted,
        "pages_approved": pages_approved,
        "pages_security_decision": pages_decision,
        "can_publish_pages": can_publish_pages,
        "browser_preview": browser_preview,
        "pages_publication_trusted_context_only": "YES",
        "untrusted_fork_pages_publication": "NO",
    }


def _norm_rel_path(raw: str) -> str:
    s = raw.strip().replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return s.lstrip("/")


def classify_changed_files(
    changed_files: list[str],
    *,
    event_name: str = "pull_request",
    ref: str = "",
    repository: str = "iwa-ojuidc/iwa.dev.fcbuild",
    head_repo_full_name: str = "iwa-ojuidc/iwa.dev.fcbuild",
    head_repo_fork: bool = False,
    enable_pages_var: str = "false",
) -> dict[str, Any]:
    """Computes categories and fast-workflow job selection flags for a list of changed paths."""
    normalized_files = [
        _norm_rel_path(f)
        for f in changed_files
        if f and _norm_rel_path(f)
    ]

    matched_categories: set[str] = set()
    unknown_files: list[str] = []
    broad_coverage = False

    if not normalized_files or event_name in ("workflow_dispatch", "schedule"):
        broad_coverage = True

    for path in normalized_files:
        cats, is_broad = classify_single_path(path)
        if not cats:
            unknown_files.append(path)
        matched_categories.update(cats)
        if is_broad:
            broad_coverage = True

    docs_only = (
        bool(normalized_files)
        and not broad_coverage
        and matched_categories == {"documentation"}
    )
    compose_only = (
        bool(normalized_files)
        and not broad_coverage
        and matched_categories.issubset({"compose", "documentation"})
        and "compose" in matched_categories
    )

    # Quality job ALWAYS runs so path filtering can never produce a green check with 0 validating jobs
    run_quality = True

    if broad_coverage:
        run_unit_domain = True
        run_postgres_focused = True
        run_docker_compose_focused = True
        fallback_reason = (
            "broad_coverage_fallback"
            if normalized_files
            else "empty_diff_or_manual_trigger_broad_fallback"
        )
    elif docs_only:
        run_unit_domain = False
        run_postgres_focused = False
        run_docker_compose_focused = False
        fallback_reason = "documentation_only_fast_path"
    elif compose_only:
        run_unit_domain = False
        run_postgres_focused = False
        run_docker_compose_focused = True
        fallback_reason = "compose_focused_fast_path"
    else:
        run_unit_domain = bool(
            matched_categories.intersection(
                {
                    "python-runtime",
                    "database",
                    "discord",
                    "polling",
                    "analytics",
                    "security",
                    "performance",
                }
            )
        )
        run_postgres_focused = bool(
            matched_categories.intersection(
                {
                    "database",
                    "discord",
                    "polling",
                    "analytics",
                    "security",
                    "python-runtime",
                    "performance",
                }
            )
        )
        run_docker_compose_focused = bool(
            matched_categories.intersection({"compose", "discord", "polling"})
        )
        # Fail-closed guard: if neither unit-domain, postgres, nor compose was selected for non-docs change
        if not (run_unit_domain or run_postgres_focused or run_docker_compose_focused):
            run_unit_domain = True
            run_postgres_focused = True
            run_docker_compose_focused = True
            fallback_reason = "fail_closed_unmatched_broad_fallback"
        else:
            fallback_reason = "category_targeted_selection"

    trust_info = evaluate_trusted_context(
        event_name=event_name,
        ref=ref,
        repository=repository,
        head_repo_full_name=head_repo_full_name,
        head_repo_fork=head_repo_fork,
        enable_pages_var=enable_pages_var,
    )

    selected_jobs = ["quality"]
    if run_unit_domain:
        selected_jobs.append("unit-domain")
    if run_postgres_focused:
        selected_jobs.append("postgres-focused")
    if run_docker_compose_focused:
        selected_jobs.append("docker-compose-focused")

    return {
        "changed_files_count": len(normalized_files),
        "changed_files": normalized_files[:200],
        "categories": sorted(matched_categories),
        "unknown_files": unknown_files,
        "broad_coverage": broad_coverage,
        "docs_only": docs_only,
        "fallback_reason": fallback_reason,
        "run_quality": run_quality,
        "run_unit_domain": run_unit_domain,
        "run_postgres_focused": run_postgres_focused,
        "run_docker_compose_focused": run_docker_compose_focused,
        "selected_jobs": selected_jobs,
        **trust_info,
    }


def _git_changed_files(base_ref: str, head_ref: str) -> list[str]:
    if not base_ref:
        return []
    try:
        proc = subprocess.run(
            ["git", "diff", "--name-only", f"{base_ref}...{head_ref}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        proc2 = subprocess.run(
            ["git", "diff", "--name-only", base_ref, head_ref],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        if proc2.returncode == 0:
            return [line.strip() for line in proc2.stdout.splitlines() if line.strip()]
    except Exception:
        pass
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Classify changed files and compute FC27 CI job selection and trust context."
    )
    parser.add_argument("--base-ref", default="", help="Base Git ref or SHA for diff.")
    parser.add_argument("--head-ref", default="HEAD", help="Head Git ref or SHA for diff.")
    parser.add_argument(
        "--files",
        nargs="*",
        default=None,
        help="Explicit list of changed files (overrides git diff when provided).",
    )
    parser.add_argument(
        "--event-name",
        default=os.environ.get("GITHUB_EVENT_NAME", "pull_request"),
    )
    parser.add_argument(
        "--ref",
        default=os.environ.get("GITHUB_REF", ""),
    )
    parser.add_argument(
        "--repository",
        default=os.environ.get("GITHUB_REPOSITORY", "iwa-ojuidc/iwa.dev.fcbuild"),
    )
    parser.add_argument(
        "--head-repo",
        default=os.environ.get("GITHUB_HEAD_REPO", ""),
    )
    parser.add_argument(
        "--head-repo-fork",
        default=os.environ.get("GITHUB_HEAD_REPO_FORK", "false"),
    )
    parser.add_argument(
        "--enable-pages-var",
        default=os.environ.get("FC27_ENABLE_PAGES_TEST_REPORTS", "false"),
    )
    parser.add_argument(
        "--github-output",
        default=os.environ.get("GITHUB_OUTPUT", ""),
        help="Optional path to $GITHUB_OUTPUT file.",
    )
    parser.add_argument(
        "--json-out",
        default="",
        help="Optional path to write JSON classification output.",
    )
    args = parser.parse_args(argv)

    if args.files is not None:
        files = list(args.files)
    else:
        files = _git_changed_files(args.base_ref, args.head_ref)

    head_repo = args.head_repo or args.repository
    is_fork = str(args.head_repo_fork).strip().lower() == "true"

    result = classify_changed_files(
        files,
        event_name=args.event_name,
        ref=args.ref,
        repository=args.repository,
        head_repo_full_name=head_repo,
        head_repo_fork=is_fork,
        enable_pages_var=args.enable_pages_var,
    )

    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

    if args.github_output:
        gh_out = Path(args.github_output)
        lines = [
            f"run_quality={'true' if result['run_quality'] else 'false'}",
            f"run_unit_domain={'true' if result['run_unit_domain'] else 'false'}",
            f"run_postgres_focused={'true' if result['run_postgres_focused'] else 'false'}",
            f"run_docker_compose_focused={'true' if result['run_docker_compose_focused'] else 'false'}",
            f"broad_coverage={'true' if result['broad_coverage'] else 'false'}",
            f"docs_only={'true' if result['docs_only'] else 'false'}",
            f"categories={','.join(result['categories'])}",
            f"selected_jobs={','.join(result['selected_jobs'])}",
            f"trusted_context={'true' if result['trusted_context'] else 'false'}",
            f"can_publish_pages={'true' if result['can_publish_pages'] else 'false'}",
            f"pages_security_decision={result['pages_security_decision']}",
            f"browser_preview={result['browser_preview']}",
        ]
        with gh_out.open("a", encoding="utf-8") as fh:
            for line in lines:
                fh.write(line + "\n")

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
