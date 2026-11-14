"""Authoritative FC27 test isolation classification, marker mapping, and CI shard registry.

Provides deterministic classification for every repository test module across the
11 canonical isolation categories, 12 canonical pytest markers, maintained test
groups, fast PR workflow groups, and non-overlapping full CI shards.
Unknown test modules fail-safe to UNKNOWN_SERIAL.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

ISOLATION_CLASSIFICATIONS: Final[tuple[str, ...]] = (
    "PURE_UNIT_PARALLEL_SAFE",
    "OFFLINE_DOMAIN_PARALLEL_SAFE",
    "POSTGRES_PARALLEL_SAFE",
    "POSTGRES_SERIAL",
    "DOCKER_SERIAL",
    "FIXED_PORT_SERIAL",
    "FILESYSTEM_SERIAL",
    "PERFORMANCE_SERIAL",
    "DOCUMENTATION_PARALLEL_SAFE",
    "SECURITY_SERIAL",
    "UNKNOWN_SERIAL",
)

CANONICAL_PYTEST_MARKERS: Final[tuple[str, ...]] = (
    "unit",
    "domain",
    "postgres",
    "docker",
    "compose",
    "security",
    "documentation",
    "performance",
    "serial",
    "slow",
    "offline",
    "network_forbidden",
)

ALL_REGISTERED_MARKERS: Final[dict[str, str]] = {
    "unit": "Pure in-memory unit tests without database, Docker, or external network access.",
    "domain": "Offline FC27 domain, analytics, contract, and workflow logic tests.",
    "postgres": "Tests requiring a disposable PostgreSQL 16 test instance.",
    "docker": "Tests inspecting or invoking Docker CLI or container build contracts.",
    "compose": "Tests validating Docker Compose profile isolation, config, and service boundaries.",
    "security": "Adversarial, authentication, authorization, privacy, and boundary security tests.",
    "documentation": "Documentation, packaging, workflow, and repository hygiene tests.",
    "performance": "Performance telemetry, benchmark, and latency threshold tests.",
    "serial": "Tests that must execute serially due to shared resources, concurrency races, or cluster state.",
    "slow": "Longer-running integration, stress, or multi-database migration tests.",
    "offline": "Tests verified to execute completely offline without external network access.",
    "network_forbidden": "Tests enforcing strict zero-network and zero-EA-DNS resolution guards.",
    "allow_inconsistency": "Allow temporary inconsistency in fc27_approval_inconsistencies view.",
}

MAINTAINED_TEST_GROUPS: Final[tuple[str, ...]] = (
    "quality",
    "unit_domain_parallel",
    "unit_domain_serial",
    "postgres_parallel_safe",
    "postgres_serial",
    "compose_serial",
)

FULL_CI_SHARDS: Final[tuple[str, ...]] = (
    "full-quality",
    "full-unit-domain",
    "full-postgres",
    "full-compose",
)

FAST_WORKFLOW_GROUPS: Final[tuple[str, ...]] = (
    "quality",
    "unit-domain",
    "postgres-focused",
    "docker-compose-focused",
)

POSTGRES_FIXTURE_NAMES: Final[frozenset[str]] = frozenset(
    {
        "pg_admin_uri",
        "pg_db",
        "pg_db_migrated",
        "pg_uri",
        "pg_admin_db_uri",
        "pg_migrator_uri",
        "pg_dispatch_uri",
        "pg_roles",
        "pg_analytics_db",
        "pg_api_db",
        "client",
        "client_v1",
        "client_v1_hmac",
        "enabled_client",
        "wired",
        "wired_restart",
    }
)


@dataclass(frozen=True)
class TestModuleClassification:
    module: str
    classification: str
    markers: tuple[str, ...]
    maintained_group: str
    full_ci_shard: str
    fast_groups: tuple[str, ...]
    execution_mode: str
    reason: str


def _entry(
    module: str,
    classification: str,
    markers: tuple[str, ...],
    maintained_group: str,
    full_ci_shard: str,
    fast_groups: tuple[str, ...],
    reason: str,
) -> tuple[str, TestModuleClassification]:
    if classification not in ISOLATION_CLASSIFICATIONS:
        raise ValueError(f"Unsupported classification {classification!r} for {module}")
    for m in markers:
        if m not in ALL_REGISTERED_MARKERS:
            raise ValueError(f"Unregistered marker {m!r} for {module}")
    if maintained_group not in MAINTAINED_TEST_GROUPS:
        raise ValueError(f"Invalid maintained_group {maintained_group!r} for {module}")
    if full_ci_shard not in FULL_CI_SHARDS:
        raise ValueError(f"Invalid full_ci_shard {full_ci_shard!r} for {module}")
    for fg in fast_groups:
        if fg not in FAST_WORKFLOW_GROUPS:
            raise ValueError(f"Invalid fast_group {fg!r} for {module}")
    mode = "parallel-xdist" if classification.endswith("_PARALLEL_SAFE") else "serial"
    return (
        module,
        TestModuleClassification(
            module=module,
            classification=classification,
            markers=markers,
            maintained_group=maintained_group,
            full_ci_shard=full_ci_shard,
            fast_groups=fast_groups,
            execution_mode=mode,
            reason=reason,
        ),
    )


TEST_MODULE_MATRIX: Final[dict[str, TestModuleClassification]] = dict(
    [
        _entry(
            "tests/test_packaging.py",
            "PURE_UNIT_PARALLEL_SAFE",
            ("unit", "documentation", "offline"),
            "quality",
            "full-quality",
            ("quality",),
            "Read-only verification of pyproject.toml, requirements.txt synchronization, and core repository hygiene.",
        ),
        _entry(
            "tests/test_github_workflows.py",
            "DOCUMENTATION_PARALLEL_SAFE",
            ("documentation", "security", "offline", "network_forbidden"),
            "quality",
            "full-quality",
            ("quality",),
            "Static policy verification of GitHub Actions workflows, permissions, timeouts, pinning, and shard completeness.",
        ),
        _entry(
            "tests/test_ci_report_sanitization.py",
            "PURE_UNIT_PARALLEL_SAFE",
            ("unit", "security", "offline", "network_forbidden"),
            "quality",
            "full-quality",
            ("quality",),
            "Unit and adversarial policy tests for browser-report sanitization, self-contained HTML, and count reconciliation.",
        ),
        _entry(
            "tests/test_compose_isolation.py",
            "DOCKER_SERIAL",
            ("docker", "compose", "security", "serial", "offline"),
            "compose_serial",
            "full-compose",
            ("docker-compose-focused",),
            "Invokes docker compose config across profiles and verifies container/network/secret/digest isolation.",
        ),
        _entry(
            "tests/test_fc27_search.py",
            "OFFLINE_DOMAIN_PARALLEL_SAFE",
            ("unit", "domain", "offline", "network_forbidden"),
            "unit_domain_parallel",
            "full-unit-domain",
            ("unit-domain",),
            "Pure in-memory deterministic Search context assembly with zero shared state.",
        ),
        _entry(
            "tests/test_fc27_scout.py",
            "OFFLINE_DOMAIN_PARALLEL_SAFE",
            ("unit", "domain", "offline", "network_forbidden"),
            "unit_domain_parallel",
            "full-unit-domain",
            ("unit-domain",),
            "Pure in-memory deterministic Scout evidence metrics and observational hypotheses.",
        ),
        _entry(
            "tests/test_fc27_analyst.py",
            "OFFLINE_DOMAIN_PARALLEL_SAFE",
            ("unit", "domain", "offline", "network_forbidden"),
            "unit_domain_parallel",
            "full-unit-domain",
            ("unit-domain",),
            "Pure in-memory deterministic Analyst clustering, confounders, and language safety validation.",
        ),
        _entry(
            "tests/test_fc27_coach.py",
            "OFFLINE_DOMAIN_PARALLEL_SAFE",
            ("unit", "domain", "offline", "network_forbidden"),
            "unit_domain_parallel",
            "full-unit-domain",
            ("unit-domain",),
            "Pure in-memory deterministic Coach observational hypothesis synthesis.",
        ),
        _entry(
            "tests/test_fc27_tactics.py",
            "OFFLINE_DOMAIN_PARALLEL_SAFE",
            ("unit", "domain", "offline", "network_forbidden"),
            "unit_domain_parallel",
            "full-unit-domain",
            ("unit-domain",),
            "Pure in-memory deterministic Tactics experimental candidate evaluation.",
        ),
        _entry(
            "tests/test_fc27_director.py",
            "OFFLINE_DOMAIN_PARALLEL_SAFE",
            ("unit", "domain", "offline", "network_forbidden"),
            "unit_domain_parallel",
            "full-unit-domain",
            ("unit-domain",),
            "Pure in-memory deterministic Director evidence reconciliation.",
        ),
        _entry(
            "tests/test_fc27_advisory_cache.py",
            "POSTGRES_SERIAL",
            (
                "postgres",
                "domain",
                "performance",
                "serial",
                "offline",
                "network_forbidden",
            ),
            "postgres_serial",
            "full-postgres",
            ("postgres-focused",),
            "Immutable advisory-report cache, validation deduplication, and disposable PostgreSQL Discord integration tests.",
        ),
        _entry(
            "tests/test_fc27_api_cache.py",
            "PURE_UNIT_PARALLEL_SAFE",
            ("unit", "domain", "offline", "network_forbidden"),
            "unit_domain_parallel",
            "full-unit-domain",
            ("unit-domain",),
            "Pure in-memory bounded normalized-object TTL/LRU cache tests.",
        ),
        _entry(
            "tests/test_fc27_api_rate_limit.py",
            "PURE_UNIT_PARALLEL_SAFE",
            ("unit", "domain", "offline", "network_forbidden"),
            "unit_domain_parallel",
            "full-unit-domain",
            ("unit-domain",),
            "Pure in-memory token bucket rate limiter, request budget, and circuit breaker tests.",
        ),
        _entry(
            "tests/test_fc27_clubs_api_contracts.py",
            "OFFLINE_DOMAIN_PARALLEL_SAFE",
            ("unit", "domain", "offline", "network_forbidden"),
            "unit_domain_parallel",
            "full-unit-domain",
            ("unit-domain",),
            "Pure Pydantic v2 endpoint schema, normalizer, pseudonymizer, and 23-bit event bitmap tests.",
        ),
        _entry(
            "tests/test_fc27_privacy.py",
            "PURE_UNIT_PARALLEL_SAFE",
            ("unit", "domain", "security", "offline", "network_forbidden"),
            "unit_domain_parallel",
            "full-unit-domain",
            ("unit-domain",),
            "Pure in-memory privacy, pseudonymization, sensitive-field detection, CSV formula escaping, and secret redaction tests.",
        ),
        _entry(
            "tests/test_fc27_pandas_view.py",
            "OFFLINE_DOMAIN_PARALLEL_SAFE",
            ("unit", "domain", "offline", "network_forbidden"),
            "unit_domain_parallel",
            "full-unit-domain",
            ("unit-domain",),
            "Pure in-memory downstream analytical tabular view tests.",
        ),
        _entry(
            "tests/test_fc27_platform.py",
            "OFFLINE_DOMAIN_PARALLEL_SAFE",
            ("unit", "domain", "security", "offline", "network_forbidden"),
            "unit_domain_parallel",
            "full-unit-domain",
            ("unit-domain",),
            "Offline FC27 platform domain models, analytics, and feature-gate boundary tests.",
        ),
        _entry(
            "tests/test_visual_studio_project.py",
            "FILESYSTEM_SERIAL",
            ("unit", "domain", "serial", "offline"),
            "unit_domain_serial",
            "full-unit-domain",
            ("unit-domain",),
            "Windows Visual Studio MSBuild integration creates ignored intermediates; skipped when Python tooling is unavailable.",
        ),
        _entry(
            "tests/test_db_roles.py",
            "POSTGRES_SERIAL",
            ("postgres", "security", "serial", "offline"),
            "postgres_serial",
            "full-postgres",
            ("postgres-focused",),
            "Mutates cluster-global role membership (GRANT/REVOKE fc27_owner TO fc27_app) and writes temp files in tests/.",
        ),
        _entry(
            "tests/test_db_migrate_roles.py",
            "POSTGRES_SERIAL",
            ("postgres", "security", "serial", "offline"),
            "postgres_serial",
            "full-postgres",
            ("postgres-focused",),
            "Mutates cluster-global role membership (GRANT/REVOKE fc27_owner TO fc27_app) to verify posture failure gates.",
        ),
        _entry(
            "tests/test_fc27_persistence_and_queries.py",
            "POSTGRES_SERIAL",
            (
                "postgres",
                "domain",
                "security",
                "serial",
                "slow",
                "offline",
                "network_forbidden",
            ),
            "postgres_serial",
            "full-postgres",
            ("postgres-focused",),
            "Tests Migration 0012, Grants Policy v14, and temporarily mutates cluster role membership for negative posture checks.",
        ),
        _entry(
            "tests/test_fc27_backup_restore.py",
            "POSTGRES_SERIAL",
            ("postgres", "domain", "serial", "slow", "offline", "network_forbidden"),
            "postgres_serial",
            "full-postgres",
            ("postgres-focused",),
            "Executes pg_dump and pg_restore against disposable PostgreSQL 16 databases.",
        ),
        _entry(
            "tests/test_fc27_c1_activation_remediation.py",
            "POSTGRES_PARALLEL_SAFE",
            ("postgres", "domain", "security", "offline", "network_forbidden"),
            "postgres_parallel_safe",
            "full-postgres",
            ("postgres-focused",),
            "Versioned NFKC alias-HMAC normalization and complete canonical post_new_results idempotency regression tests using per-test DBs.",
        ),
        _entry(
            "tests/test_fc27_replay_conflict_reconciliation.py",
            "POSTGRES_PARALLEL_SAFE",
            ("postgres", "domain", "security", "offline", "network_forbidden"),
            "postgres_parallel_safe",
            "full-postgres",
            ("postgres-focused",),
            "Versioned FC27 match-equivalence projection, historical comm_eval hash compatibility, polling conflict quarantine, and durable circuit recovery tests using per-test DBs.",
        ),
        _entry(
            "tests/test_fc27_discord_bot.py",
            "POSTGRES_PARALLEL_SAFE",
            (
                "postgres",
                "docker",
                "compose",
                "domain",
                "security",
                "offline",
                "network_forbidden",
            ),
            "postgres_parallel_safe",
            "full-postgres",
            ("postgres-focused", "docker-compose-focused"),
            "Read-only Discord bot slash-command, PostgreSQL reader, and Dockerfile.discord/compose contract tests using per-test DBs.",
        ),
        _entry(
            "tests/test_fc27_match_results.py",
            "POSTGRES_PARALLEL_SAFE",
            ("postgres", "domain", "security", "offline", "network_forbidden"),
            "postgres_parallel_safe",
            "full-postgres",
            ("postgres-focused",),
            "Manual Match Results schema, dedicated writer role, parser, embed renderer, edit-in-place, and Discord command tests using per-test DBs.",
        ),
        _entry(
            "tests/test_fc27_polling_worker.py",
            "POSTGRES_PARALLEL_SAFE",
            (
                "postgres",
                "docker",
                "compose",
                "domain",
                "security",
                "offline",
                "network_forbidden",
            ),
            "postgres_parallel_safe",
            "full-postgres",
            ("postgres-focused", "docker-compose-focused"),
            "Disabled-by-default polling worker single-flight advisory lock, PG persistence, and compose contract tests using per-test DBs.",
        ),
        _entry(
            "tests/test_fc27_community_provider.py",
            "POSTGRES_PARALLEL_SAFE",
            (
                "postgres",
                "docker",
                "compose",
                "domain",
                "security",
                "offline",
                "network_forbidden",
            ),
            "postgres_parallel_safe",
            "full-postgres",
            ("postgres-focused", "docker-compose-focused"),
            "Community provider contract, mock evaluation, PG persistence, and isolated container contract tests using per-test DBs.",
        ),
        _entry(
            "tests/test_fc27_dashboard_queries.py",
            "POSTGRES_SERIAL",
            ("postgres", "domain", "serial", "offline", "network_forbidden"),
            "postgres_serial",
            "full-postgres",
            ("postgres-focused",),
            "Creates cluster role fc27_reader and verifies read-only PostgreSQL dashboard queries.",
        ),
        _entry(
            "tests/test_fc27_performance.py",
            "PERFORMANCE_SERIAL",
            (
                "postgres",
                "performance",
                "serial",
                "slow",
                "offline",
                "network_forbidden",
            ),
            "postgres_serial",
            "full-postgres",
            (),
            "Runs SMALL/MEDIUM/LARGE/STRESS timing benchmarks and PostgreSQL EXPLAIN ANALYZE; must run serially for stable timings.",
        ),
        _entry(
            "tests/test_fc27_advisory_pipeline_e2e.py",
            "POSTGRES_PARALLEL_SAFE",
            ("postgres", "domain", "offline", "network_forbidden"),
            "postgres_parallel_safe",
            "full-postgres",
            (),
            "End-to-end Search -> Scout -> Analyst -> Coach -> Tactics -> Director pipeline against isolated per-test DBs.",
        ),
        _entry(
            "tests/test_fc27_analyst_graph.py",
            "POSTGRES_PARALLEL_SAFE",
            ("postgres", "domain", "offline", "network_forbidden"),
            "postgres_parallel_safe",
            "full-postgres",
            (),
            "LangGraph analyst_evidence_node integration with isolated per-test PostgreSQL databases.",
        ),
        _entry(
            "tests/test_fc27_search_scout_graph.py",
            "POSTGRES_PARALLEL_SAFE",
            ("postgres", "domain", "offline", "network_forbidden"),
            "postgres_parallel_safe",
            "full-postgres",
            (),
            "LangGraph search_node and scout_node integration with isolated per-test PostgreSQL databases.",
        ),
        _entry(
            "tests/test_events_outbox.py",
            "POSTGRES_PARALLEL_SAFE",
            ("postgres", "domain", "security", "offline"),
            "postgres_parallel_safe",
            "full-postgres",
            (),
            "Append-only domain_events and outbox trigger tests on isolated per-test DBs.",
        ),
    ]
)


def normalize_test_module_path(
    path_or_rel: str | Path, root: Path | None = None
) -> str:
    """Normalizes a test file path to a repository-relative POSIX path ('tests/...')."""
    p = Path(path_or_rel)
    if p.is_absolute():
        repo_root = root or Path(__file__).resolve().parents[1]
        p = p.resolve().relative_to(repo_root.resolve())
    return p.as_posix()


def classify_test_module(
    path_or_rel: str | Path,
    *,
    uses_postgres_fixture: bool = False,
    root: Path | None = None,
) -> TestModuleClassification:
    """Returns the authoritative classification for a test module, defaulting unknown modules to UNKNOWN_SERIAL."""
    rel = normalize_test_module_path(path_or_rel, root=root)
    known = TEST_MODULE_MATRIX.get(rel)
    if known is not None:
        return known
    if uses_postgres_fixture:
        return TestModuleClassification(
            module=rel,
            classification="UNKNOWN_SERIAL",
            markers=("postgres", "serial", "offline"),
            maintained_group="postgres_serial",
            full_ci_shard="full-postgres",
            fast_groups=("postgres-focused",),
            execution_mode="serial",
            reason="Unclassified PostgreSQL-backed module defaulted fail-safe to UNKNOWN_SERIAL.",
        )
    return TestModuleClassification(
        module=rel,
        classification="UNKNOWN_SERIAL",
        markers=("serial", "offline"),
        maintained_group="unit_domain_serial",
        full_ci_shard="full-unit-domain",
        fast_groups=("unit-domain",),
        execution_mode="serial",
        reason="Unclassified test module defaulted fail-safe to UNKNOWN_SERIAL.",
    )


def modules_for_full_shard(shard: str) -> list[str]:
    """Returns sorted test module paths belonging to a full CI shard."""
    if shard not in FULL_CI_SHARDS:
        raise ValueError(f"Unknown full CI shard: {shard!r}")
    return sorted(
        m for m, info in TEST_MODULE_MATRIX.items() if info.full_ci_shard == shard
    )


def modules_for_maintained_group(group: str) -> list[str]:
    """Returns sorted test module paths belonging to a maintained test group."""
    if group not in MAINTAINED_TEST_GROUPS:
        raise ValueError(f"Unknown maintained group: {group!r}")
    return sorted(
        m for m, info in TEST_MODULE_MATRIX.items() if info.maintained_group == group
    )


def modules_for_fast_group(fast_group: str) -> list[str]:
    """Returns sorted test module paths belonging to a fast workflow group."""
    if fast_group not in FAST_WORKFLOW_GROUPS:
        raise ValueError(f"Unknown fast workflow group: {fast_group!r}")
    return sorted(
        m for m, info in TEST_MODULE_MATRIX.items() if fast_group in info.fast_groups
    )
