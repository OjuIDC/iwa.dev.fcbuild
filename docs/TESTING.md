# Testing Guide

- **Status**: Current
- **Audience**: Developers, QA Engineers, Security Reviewers
- **Last verified commit**: `b2c7bbda0d4a5f5788caa5e2cb74844c9bf5c5f0`
- **Source of truth**: `tests/`, `scripts/test_postgres.sh`, `scripts/check_matchpulse_boundary.py`
- **Supersedes**: `NONE`

This guide documents the test architecture, PostgreSQL 16 integration harness, offline/DNS network guards, test suite categories, and cleanup procedures for `iwa.dev.fcbuild`.

> **Note on Pass Counts**: Do not hardcode historical test counts as permanent expectations in operational guides. Dated, commit-bound pass counts are recorded in [`TEST_RESULTS.md`](../TEST_RESULTS.md).

---

## 1. Behavior Without PostgreSQL (`TEST_POSTGRES_URI` Unset)

- Pure unit, contract, cache, rate-limit, pre-approval validator, operator-capture, user-export, packaging, workflow, archive-integrity, and documentation-consistency tests run completely in memory without a database.
- Tests that require a live PostgreSQL 16 server inspect `TEST_POSTGRES_ADMIN_URI` / `TEST_POSTGRES_URI` via `tests/conftest.py` (or module-specific fixtures). When no PostgreSQL instance is reachable at `127.0.0.1:55432`, database-backed tests skip gracefully with a descriptive reason rather than failing with connection timeouts.
- For full merge-gate or release verification, skipping PostgreSQL tests is **not** sufficient: a live PostgreSQL 16 instance must be provided so all database-backed suites execute.

---

## 2. Running PostgreSQL 16 Integration Tests (`TEST_POSTGRES_URI`)

1. Start an ephemeral PostgreSQL 16 container bound strictly to loopback (`127.0.0.1:55432`):
   ```powershell
   docker rm -f fc27-test-postgres 2>$null
   docker run -d --name fc27-test-postgres `
     -e POSTGRES_USER=fc27test `
     -e POSTGRES_HOST_AUTH_METHOD=trust `
     -p 127.0.0.1:55432:5432 `
     postgres:16
   ```
2. Export both `TEST_POSTGRES_URI` and `TEST_POSTGRES_ADMIN_URI`:
   ```powershell
   $env:TEST_POSTGRES_URI = "postgresql://fc27test@127.0.0.1:55432/postgres"
   $env:TEST_POSTGRES_ADMIN_URI = "postgresql://fc27test@127.0.0.1:55432/postgres"
   ```
3. How the test harness isolates databases (`tests/conftest.py` and `tests/test_fc27_persistence_and_queries.py`):
   - Each test function or fixture provisions an isolated temporary database (`fc27_test_<uuid>` or `fc27_p_<uuid>`), runs `src.db_roles.bootstrap` and `src.db_migrate.migrate` (migrations `0001`–`0013` + Grants Policy Version `15`), executes assertions using the least-privileged runtime roles (`fc27_app`, `fc27_match_results_writer`, `fc27_dispatch`, `fc27_relay`), and drops the temporary database on teardown.

---

## 3. Network-Blocked Tests & EA DNS Blocking

The FC 27 test suites enforce strict offline and no-EA-traffic invariants:

- **Zero Live EA Requests (`NETWORK_REQUESTS_EXECUTED=0`)**: All provider and ingestion tests use `FC27MockTransport` or loopback fixtures.
- **EA DNS Resolution Blocking (`EA_DNS_RESOLUTIONS_EXECUTED=0`)**: Tests monkeypatch `socket.getaddrinfo`, `socket.gethostbyname`, and `socket.create_connection` (and `urllib.request.urlopen` / `httpx.Client`) to fail immediately if any code attempts to resolve `ea.com`, `proclubs.ea.com`, or any external host during test execution.
- **Offline Status Verification (`scripts/fc27_polling_worker.py` and `scripts/fc27_discord_bot.py`)**:
  ```powershell
  # Verify default blocked-safe posture (0 network requests, 0 DNS resolutions)
  .\.venv\Scripts\python.exe scripts/fc27_polling_worker.py --status
  .\.venv\Scripts\python.exe scripts/fc27_discord_bot.py --status
  ```

---

## 4. Test Suite Categories

### 4.1 Focused FC 27 Provider, Contract, Cache, & Rate-Limit Suites
- `tests/test_fc27_clubs_api_contracts.py` — 11 fixed endpoint operations, query parameter builders, 13 response schema models, normalizers, 23-bit event bitmap (`ev`) decoder, strict duplicate-key JSON rejection (`parse_json_strict_no_duplicates`), `PROHIBITED_HEADER_NAMES`, and `FC27PseudonymCollisionDetector`.
- `tests/test_fc27_api_cache.py` — `FC27BoundedNormalizedCache` TTL expiration, LRU eviction, byte bounds (`256 KiB`), key isolation, and cache-poisoning/raw-response rejection.
- `tests/test_fc27_api_rate_limit.py` — `FC27ProviderTokenBucketLimiter`, `Retry-After` cooldowns, `FC27RequestBudget` (`max_attempts=1`), `FC27AdapterCircuitBreaker` (`CLOSED`/`OPEN`/`HALF_OPEN`), and `classify_fc27_retry_decision`.
- `tests/test_fc27_community_provider.py` — Bounded community provider wrapper (`src/fc27_community_provider.py`, `src/fc27_polling_worker.py`), kill switches, and `FC27SourceProvenance`.

### 4.2 FC 27 Ingestion, Persistence, Analytics, & Pre-Approval Suites
- `tests/test_fc27_persistence_and_queries.py` — Normalized PostgreSQL storage (`fc27_clubs`, `fc27_export_batches`, `fc27_matches`, `fc27_player_match_stats`, `fc27_quarantine_records`), Grants Policy Version `15`, replay conflict detection, and `src/fc27_analytics.py`.
- `tests/test_fc27_match_results.py` — Manual Match Results schema (`migrations/0013_fc27_manual_match_results.sql`), dedicated `fc27_match_results_writer` least-privilege boundaries, deterministic scorer and player-stat parsers, shared East Coasters FC embed renderers, role-ID write authorization, edit-in-place and deleted-message replacement relinking, optimistic concurrency, soft deletion, and `/fc27` commands.
- `tests/test_fc27_dashboard_queries.py` — Read-only dashboard queries (`src/fc27_dashboard_queries.py`), `READ ONLY` transaction enforcement, `5000ms` statement timeout, rolling windows (`<= 730` days), and alias suppression.
- `tests/test_fc27_search.py`, `tests/test_fc27_scout.py`, `tests/test_fc27_search_scout_graph.py` — Deterministic Search context assembly (`build_fc27_search_context`), Scout evidence metrics and observational hypotheses (`build_fc27_scout_report`), `FC27AnalyticsReader` (`InMemoryFC27AnalyticsReader` and `PostgresFC27AnalyticsReader`), and LangGraph `search_node` / `scout_node` adapters (`106` focused tests across items `1`–`105` + PostgreSQL 16 reader integration).
- `tests/test_fc27_analyst.py`, `tests/test_fc27_analyst_graph.py` — Deterministic Analyst evidence validation (`validate_analyst_input`, `detect_evidence_contradictions`), clustering (`build_evidence_clusters`), counter-evidence (`analyze_counter_evidence`), confounders (`identify_confounders`), competing explanations (`generate_competing_explanations`), data gaps (`identify_data_gaps`), unresolved questions (`generate_unresolved_questions`), bounded observational finding synthesis (`build_fc27_analyst_report`), language safety validation (`validate_analyst_evidence_language`), and LangGraph `analyst_evidence_node` adapter (`129` focused tests across items `1`–`128` + PostgreSQL 16 end-to-end `PostgresFC27AnalyticsReader -> search_node -> scout_node -> analyst_evidence_node` integration).
- `tests/test_fc27_discord_bot.py` — End-to-end and adversarial tests verifying the Discord bot (`src/fc27_discord_bot.py`), `/fc27 status` polling/continuity/repository status inspection, advisory cache integration, and manual Match Results slash commands.
- `tests/test_fc27_coach.py`, `tests/test_fc27_tactics.py`, `tests/test_fc27_director.py`, `tests/test_fc27_advisory_pipeline_e2e.py` — Deterministic Coach/Tactics/Director advisory pipeline (`src/fc27_coach.py`, `src/fc27_tactics.py`, `src/fc27_director.py`; historical `tests/test_fc27_preapproval_validator.py` and `src/fc27_coaching.py` superseded in Stage B3).
- `tests/test_fc27_privacy.py`, `tests/test_fc27_platform.py` — Privacy and pseudonymization boundary (`src/fc27_privacy.py`), sensitive-field scanning, CSV formula-injection defense, Discord mention suppression, and ADR/platform compatibility checks.

### 4.3 Advisory Pipeline & Database Role Suites
- `tests/test_fc27_advisory_pipeline_e2e.py` — End-to-end deterministic advisory pipeline (`Search -> Scout -> Analyst -> Coach -> Tactics -> Director`; historical `tests/test_graph_persistence.py`, `tests/test_langgraph_flow.py`, `tests/test_graph_api_v1.py`, `tests/test_approval_security.py`, `tests/test_approval_lifecycle.py`, `tests/test_commissioner_cli.py`, `tests/test_events_outbox.py` superseded in Stage B3).
- `tests/test_db_roles.py`, `tests/test_db_migrate_roles.py` — Role bootstrap, migration checksums (`migrations/0001_initial.sql` through `migrations/0014_fc27_player_development.sql`), Grants Policy Version `16`, Stage C player development & Discord job schema invariants, and runtime role posture checks (historical `tests/test_migration_0003.py` superseded in Stage B3).

### 4.4 Historical Control-Plane Suites (Superseded in Stage B3)
- Historical identity (`tests/identity/*.py`), adversarial (`tests/adversarial/*.py`), dispatch (`tests/test_dispatch_schema.py`, `tests/test_dispatch_transition.py`, `tests/test_dispatch_creation.py`, `tests/test_phase1_scope.py`), and E2E (`tests/e2e/*.py`) control-plane test modules were superseded and removed in Stage B3.

### 4.5 Packaging, Workflow, Compose Isolation, & Boundary Suites
- `tests/test_packaging.py` — `pyproject.toml` vs. `requirements.txt` synchronization, required repository files, migration SHA-256 pinning, and single-owner checks.
- `tests/test_github_workflows.py` — CI workflow policy, zero orphaned test modules (`ORPHANED_TEST_MODULES=0`), and shard completeness (`src/fc27_ci_test_groups.py`).
- `tests/test_compose_isolation.py` — Resolved Docker Compose secret, network, port, and digest isolation.
- `scripts/check_matchpulse_boundary.py` — Enforces sibling-repository isolation from `..\matchpulse-live-public`.
- `tests/test_fc27_community_provider.py` & `tests/test_fc27_pandas_view.py` — Offline unit and integration tests for the bounded community provider wrapper (`src/fc27_community_provider.py`) and optional downstream analytical view (`src/fc27_pandas_view.py`).
- `tests/test_fc27_polling_worker.py` — `101` deterministic unit, PostgreSQL 16 integration, single-flight advisory lock, least-privilege role/table boundary, cache/rate-limit/budget/circuit-breaker, conditional analytics (`Search -> Scout -> Analyst` on new matches only), dashboard status, and Docker Compose hardening tests for the disabled-by-default polling worker (`src/fc27_polling_worker.py`, `scripts/fc27_polling_worker.py`, `Dockerfile.community-worker`, `docker-compose.community-worker.yml`).
- `tests/test_fc27_performance.py` — `40` offline tests verifying `src/fc27_performance.py` (`FC27_PERFORMANCE_TELEMETRY_ENABLED=false` default, `extra="forbid"`, `frozen=True`, privacy scrubbing, 20-run retention, 2 MiB file size cap, `SMALL`/`MEDIUM`/`LARGE`/`STRESS` benchmark scenarios, disposable PostgreSQL `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`, analytics layers, loopback dashboard endpoints, offline Discord commands, Polars evaluation `NOT_JUSTIFIED`, threshold evaluation, and `KEEP_PYTHON` default decisions).
- `tests/test_github_workflows.py` & `tests/test_ci_report_sanitization.py` — CI workflow hardening, action SHA pinning, changed-file classification (`scripts/classify_ci_changes.py`), 32-module test isolation matrix (`src/fc27_ci_test_groups.py`), shard completeness (`COLLECTION_UNION_COMPLETE=YES`), and 38-case browser report sanitization and count-reconciliation policy tests (`scripts/build_test_report.py`, `scripts/aggregate_test_reports.py`, `scripts/sanitize_test_report.py`). See [`FC27_CI_TEST_STRATEGY.md`](FC27_CI_TEST_STRATEGY.md) and [`FC27_CI_BROWSER_REPORTS.md`](FC27_CI_BROWSER_REPORTS.md).

---

## 5. Expected Skipped-Test Behavior

When running the full suite **with** PostgreSQL 16 and Docker Compose v2 configured, zero active tests are skipped (historical phase-evolution guards in `tests/test_phase1_scope.py` were superseded and removed in Stage B3). No PostgreSQL or security test may be skipped due to missing database configuration during a full-suite verification run.

---

## 6. Full-Suite Execution & Temporary Container Cleanup

Run the complete test suite and guarantee container cleanup using a `try / finally` block:

```powershell
$ErrorActionPreference = "Stop"
$dockerCfgDir = "$env:TEMP\docker-cfg-fc27"
New-Item -ItemType Directory -Force -Path $dockerCfgDir | Out-Null
Set-Content -Path "$dockerCfgDir\config.json" -Value '{"cliPluginsExtraDirs":["C:\\Program Files\\Docker\\cli-plugins"]}' -Encoding utf8
$env:DOCKER_CONFIG = $dockerCfgDir

docker rm -f fc27-test-postgres 2>$null | Out-Null
docker run -d --name fc27-test-postgres `
  -e POSTGRES_USER=fc27test `
  -e POSTGRES_HOST_AUTH_METHOD=trust `
  -p 127.0.0.1:55432:5432 `
  postgres:16 | Out-Null

try {
    $env:TEST_POSTGRES_URI = "postgresql://fc27test@127.0.0.1:55432/postgres"
    $env:TEST_POSTGRES_ADMIN_URI = "postgresql://fc27test@127.0.0.1:55432/postgres"
    .\.venv\Scripts\pytest.exe -q --basetemp="$env:TEMP\pytest-fc27"
} finally {
    docker rm -f fc27-test-postgres 2>$null | Out-Null
    Remove-Item -Recurse -Force $dockerCfgDir -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force "$env:TEMP\pytest-fc27" -ErrorAction SilentlyContinue
}
```
