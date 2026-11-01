# Testing Guide

- **Status**: Current
- **Audience**: Developers, QA Engineers, Security Reviewers
- **Last verified commit**: `b2c7bbda0d4a5f5788caa5e2cb74844c9bf5c5f0`
- **Source of truth**: `tests/`, `scripts/test_postgres.sh`, `scripts/check_documentation_consistency.py`, `scripts/check_matchpulse_boundary.py`
- **Supersedes**: `docs/archive/test-results/PHASE2_TEST_TRACEABILITY.md`

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
   - Each test function or fixture provisions an isolated temporary database (`fc27_test_<uuid>` or `fc27_p_<uuid>`), runs `src.db_roles.bootstrap` and `src.db_migrate.migrate` (migrations `0001`–`0012` + Grants Policy Version `14`), executes assertions using the least-privileged runtime roles (`fc27_app`, `fc27_dispatch`, `fc27_relay`), and drops the temporary database on teardown.

---

## 3. Network-Blocked Tests & EA DNS Blocking

The FC 27 test suites enforce strict offline and no-EA-traffic invariants:

- **Zero Live EA Requests (`NETWORK_REQUESTS_EXECUTED=0`)**: All provider and ingestion tests use `FC27MockTransport` or loopback fixtures.
- **EA DNS Resolution Blocking (`EA_DNS_RESOLUTIONS_EXECUTED=0`)**: Tests monkeypatch `socket.getaddrinfo`, `socket.gethostbyname`, and `socket.create_connection` (and `urllib.request.urlopen` / `httpx.Client`) to fail immediately if any code attempts to resolve `ea.com`, `proclubs.ea.com`, or any external host during test execution.
- **Preflight Verification (`scripts/fc27_capture_preflight.py`)**:
  ```powershell
  # Verify default blocked-safe posture (0 network requests, 0 DNS resolutions)
  .\.venv\Scripts\python.exe scripts/fc27_capture_preflight.py

  # Verify hermetic single-request mock consumption
  .\.venv\Scripts\python.exe scripts/fc27_capture_preflight.py --mock
  ```

---

## 4. Test Suite Categories

### 4.1 Focused FC 27 Provider, Contract, Cache, & Rate-Limit Suites
- `tests/test_fc27_safe_adapter_bundle_manifest.py` — 40 deterministic tests verifying `config/fc27_safe_adapter_bundle_manifest.json` (`fc27-safe-adapter-manifest-v1`), repository-native SHA-256 digests, rejected unsafe/non-applicable components, MIT attribution in `THIRD_PARTY_NOTICES.md`, and zero runtime dependency on `.local-artifacts/` or ZIP archives.
- `tests/test_fc27_clubs_api_contracts.py` — 11 fixed endpoint operations, query parameter builders, 13 response schema models, normalizers, 23-bit event bitmap (`ev`) decoder, and `FC27PseudonymCollisionDetector`.
- `tests/test_fc27_api_cache.py` — `FC27BoundedNormalizedCache` TTL expiration, LRU eviction, byte bounds (`256 KiB`), key isolation, and cache-poisoning/raw-response rejection.
- `tests/test_fc27_api_rate_limit.py` — `FC27ProviderTokenBucketLimiter`, `Retry-After` cooldowns, `FC27RequestBudget` (`max_attempts=1`), `FC27AdapterCircuitBreaker` (`CLOSED`/`OPEN`/`HALF_OPEN`), and `classify_fc27_retry_decision`.
- `tests/test_fc27_authorized_provider.py` & `tests/test_fc27_authorized_api.py` — 18-step provider execution pipeline, candidate source evaluation (`REVIEWED_API_SOURCE_CANDIDATES`), kill switches, and `FC27SourceProvenance`.

### 4.2 FC 27 Ingestion, Persistence, Analytics, & Pre-Approval Suites
- `tests/test_fc27_api_ingestion.py` — End-to-end staged ingestion from mock provider envelopes into PostgreSQL 16 (`migrations/0012_fc27_performance_and_quarantine.sql`).
- `tests/test_fc27_persistence_and_queries.py` — Normalized PostgreSQL storage (`fc27_clubs`, `fc27_export_batches`, `fc27_matches`, `fc27_player_match_stats`, `fc27_quarantine_records`), Grants Policy Version `14`, replay conflict detection, and `src/fc27_analytics.py`.
- `tests/test_fc27_dashboard_queries.py` — Read-only dashboard queries (`src/fc27_dashboard_queries.py`), `READ ONLY` transaction enforcement, `5000ms` statement timeout, rolling windows (`<= 730` days), and alias suppression.
- `tests/test_fc27_search.py`, `tests/test_fc27_scout.py`, `tests/test_fc27_search_scout_graph.py` — Deterministic Search context assembly (`build_fc27_search_context`), Scout evidence metrics and observational hypotheses (`build_fc27_scout_report`), `FC27AnalyticsReader` (`InMemoryFC27AnalyticsReader` and `PostgresFC27AnalyticsReader`), and LangGraph `search_node` / `scout_node` adapters (`106` focused tests across items `1`–`105` + PostgreSQL 16 reader integration).
- `tests/test_fc27_analyst.py`, `tests/test_fc27_analyst_graph.py` — Deterministic Analyst evidence validation (`validate_analyst_input`, `detect_evidence_contradictions`), clustering (`build_evidence_clusters`), counter-evidence (`analyze_counter_evidence`), confounders (`identify_confounders`), competing explanations (`generate_competing_explanations`), data gaps (`identify_data_gaps`), unresolved questions (`generate_unresolved_questions`), bounded observational finding synthesis (`build_fc27_analyst_report`), language safety validation (`validate_analyst_evidence_language`), and LangGraph `analyst_evidence_node` adapter (`129` focused tests across items `1`–`128` + PostgreSQL 16 end-to-end `PostgresFC27AnalyticsReader -> search_node -> scout_node -> analyst_evidence_node` integration).
- `tests/test_fc27_demo.py` — `80` end-to-end and adversarial tests verifying the loopback synthetic FC 27 fixture server (`scripts/fc27_demo_fixture_server.py`), one-shot ingestion CLI (`scripts/fc27_demo_ingest.py`), bounded loopback transport (`src/fc27_demo.py`), cache/rate-limit/budget/circuit-breaker integration, optional PostgreSQL 16 persistence gate (`--persist` + `FC27_PERSISTENCE_ENABLED=true`), live-probe non-execution (`LIVE_PROBE_EXECUTED=NO`), and the local Demo API & HTML view (`src/fc27_demo_api.py`).
- `tests/test_fc27_preapproval_validator.py` — Deterministic pre-approval coaching package validation (`src/fc27_coaching.py`) and tactical hypothesis blocking.
- `tests/test_fc27_operator_capture.py`, `tests/test_fc27_user_export.py`, `tests/test_fc27_clubs_compatibility.py`, `tests/test_fc27_platform.py` — Offline export validation, CSV formula-injection defense, operator capture normalization, and ADR/pilot-package compatibility checks.

### 4.3 Graph, Approval Security, & Database Role Suites
- `tests/test_graph_persistence.py`, `tests/test_langgraph_flow.py`, `tests/test_graph_api_v1.py`, `tests/test_end_to_end.py` — LangGraph single `interrupt()`, `PostgresSaver` checkpoint recovery, `/v1/*` routes, and `ApprovalNotCommitted` resume protection.
- `tests/test_approval_security.py`, `tests/test_approval_lifecycle.py`, `tests/test_commissioner_cli.py`, `tests/test_events_outbox.py` — Nonce binding, HMAC-SHA256 verification, 72h request expiry, append-only triggers, and sourced domain events.
- `tests/test_db_roles.py`, `tests/test_db_migrate_roles.py`, `tests/test_migration_0003.py` — Role bootstrap, migration checksums, Grants Policy Version `14`, and runtime role posture checks.

### 4.4 Identity, Adversarial, Dispatch, & E2E Suites
- `tests/identity/*.py` (32 files) — Phase 2 user/session/passkey/TOTP/recovery/delegation/player-proof/step-up ceremonies, 14-field `fc27_decision_evidence` binding, and multi-connection PostgreSQL concurrency races.
- `tests/adversarial/*.py` & `tests/test_*adversarial*.py` — Empirical security boundary challenges, SQL injection/DDL negative checks, and concurrency stress harnesses.
- `tests/test_dispatch_schema.py`, `tests/test_dispatch_transition.py`, `tests/test_dispatch_creation.py`, `tests/dispatch/*.py`, `tests/test_phase1_scope.py` — Dispatch transition state machine, rollout gates (`G-MIGRATION`..`G-OUTBOX`), and dispatch dormancy invariants (`DISPATCH_ENABLED=false`).
- `tests/e2e/*.py` — Tiers 1–4 end-to-end verification across Phase 1 and Phase 2 capabilities.

### 4.5 Packaging, Workflow, Compose Isolation, & Documentation Suites
- `tests/test_packaging.py` — `pyproject.toml` vs. `requirements.txt` synchronization.
- `tests/test_workflow.py` — Pure 3/3/3 tier reduction and HMAC canonical signing.
- `tests/test_archive_integrity.py` — Required repository files and README references.
- `tests/test_compose_isolation.py` — Resolved Docker Compose secret, network, port, and digest isolation.
- `tests/test_documentation_consistency.py` — Verifies documented paths, scripts, tests, feature flags, Compose services, archive manifest completeness, and security/tactical documentation invariants.
- `scripts/check_documentation_consistency.py` — Standalone static and strict (`--check`, `--check --strict`, `--json`) documentation consistency validator requiring zero network, Docker, or PostgreSQL access:
  ```powershell
  .\.venv\Scripts\python.exe scripts/check_documentation_consistency.py --check
  .\.venv\Scripts\python.exe scripts/check_documentation_consistency.py --check --strict
  ```
- `scripts/check_matchpulse_boundary.py` — Enforces sibling-repository isolation from `..\matchpulse-live-public`.
- `tests/test_fc27_community_provider.py` & `tests/test_fc27_pandas_view.py` — Offline unit and integration tests for the bounded community provider wrapper (`src/fc27_community_provider.py`, `scripts/fc27_community_eval.py`) and optional downstream analytical view (`src/fc27_pandas_view.py`).
- `tests/test_fc27_polling_worker.py` — `101` deterministic unit, PostgreSQL 16 integration, single-flight advisory lock, least-privilege role/table boundary, cache/rate-limit/budget/circuit-breaker, conditional analytics (`Search -> Scout -> Analyst` on new matches only), dashboard status, and Docker Compose hardening tests for the disabled-by-default polling worker (`src/fc27_polling_worker.py`, `scripts/fc27_polling_worker.py`, `Dockerfile.community-worker`, `docker-compose.community-worker.yml`).

---

## 5. Expected Skipped-Test Behavior

When running the full suite **with** PostgreSQL 16 and Docker Compose v2 configured, only intentional phase-evolution guards in `tests/test_phase1_scope.py` (which check `if (dispatch_dir / "worker.py").exists(): pytest.skip(...)` because Phase 3 dormant worker/relay modules exist in `src/dispatch/` while remaining disabled by `DISPATCH_WORKER_ENABLED=false`) are skipped. No PostgreSQL or security test may be skipped due to missing database configuration during a full-suite verification run.

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
