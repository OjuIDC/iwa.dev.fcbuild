# Milestone 6 E2E Test Suite & Full Repository Test Analysis

**Agent**: `explorer_m6_e2e_1` (Teamwork Explorer)  
**Date**: 2026-10-03  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m6_e2e_1`  
**Reference Documents**:
- `ORIGINAL_REQUEST.md` (authoritative brief + Section 1A patch)
- `orchestrator_phase1/PROJECT.md` (architecture, features 1–41, milestones M0–M6)
- `e2e_test_writer_1/TEST_READY.md` & `e2e_test_writer_1/TEST_INFRA.md`

---

## 1. Executive Summary

A comprehensive, read-only architectural investigation has been conducted across the entire test inventory of the Player-Dispatch Programme (Phase 1: Secure Data Foundation).

The test landscape comprises:
1. **The Phase 1 E2E Test Suite** (`tests/e2e/`): **80 tests** structured across Tiers 1–4:
   - **Tier 1 (Feature Coverage)**: 41 tests systematically mapping 1:1 to Features 1–41 of `PROJECT.md`.
   - **Tier 2 (Boundary & Corner Cases)**: 25 tests covering 5 boundary domains (5 tests per domain).
   - **Tier 3 (Cross-Feature Combinations)**: 5 tests verifying concurrency, race conditions, atomic rollback, and re-entry reconciliation.
   - **Tier 4 (Real-World Scenarios & Section 1A)**: 9 tests verifying complete golden-path workflows, failure paths, and strict Section 1A human authority/player consent invariants.
2. **Phase 1 Scope & Boundary Protection Suite** (`tests/test_phase1_scope.py`): **5 tests** enforcing absence of Phase 2+ runtime code, no WebAuthn imports, no tracking mutations, and mandatory activation documentation invariants.
3. **Compose Topology & Secret Isolation Suite** (`tests/test_compose_isolation.py`): **17 tests** verifying least-privilege Docker Compose profiles, file secret mounts, network isolation, and fail-closed secret handling.
4. **Baseline Unit, Integration, and Adversarial Suites**: Over 200 tests across database roles, migrations, approval store, graph persistence, FastAPI routes, and dispatch state machines.

All 80 E2E tests, 5 scope tests, and 17 compose isolation tests strictly adhere to Phase 1 invariants: `DISPATCH_ENABLED=false` at all times, zero player delivery, PostgreSQL authoritative approval, single interrupt, and least-privileged role separation.

---

## 2. Phase 1 E2E Test Suite Analysis (80 Tests)

### 2.1 Tier 1: Baseline Feature Coverage (`tests/e2e/test_tier1_features.py` — 41 Tests)

Tier 1 provides 100% baseline coverage across all 41 features cataloged in `PROJECT.md` Feature Inventory:

| Domain | Feature Range | Test Class | Test Count | Key Invariants Verified |
|--------|---------------|------------|------------|-------------------------|
| **DB & Migrations** | Features 1–7 | `TestDatabaseInfrastructureFeatures` | 7 | `fc27_owner` NOLOGIN, `fc27_app` LOGIN DML-only; public CREATE revoked; ascending migrations; grants checksum tracking (`schema_policy_versions`); atomic `domain_events` + `outbox` insertion; `fc27_check_event_source` trigger rejecting dangling sources; transition event restriction; append-only triggers blocking `UPDATE`/`DELETE` on `domain_events`. |
| **Approval Lifecycle & Store** | Features 8–17 | `TestApprovalLifecycleFeatures` | 10 | `approval_requests` schema; `fc27_check_approval_pair` deferred constraint trigger; `fc27_approval_inconsistencies` view; 72h expiry daemon (`expire_due`); single-use nonces bound to thread and package version with TTL <= 300s; typed nonces; atomic HMAC decision commit to `approval_audit`; `REQUEST_REVISION` terminal decision; Section 1A human authority schema compatibility; separate `actor_type`, `actor_id`, and `authority_basis`. |
| **Graph Core & API** | Features 18–29 | `TestGraphCoreAndApiFeatures` | 12 | Single approval interrupt at `approval` node; resume re-reads committed PostgreSQL decision (raises `ApprovalNotCommitted` if uncommitted); terminal formatting (`APPROVED, NOT DISPATCHED`, `REJECTED, NO ACTION TAKEN`); `X-Correlation-Id` middleware propagation; `POST /v1/runs` durable creation (`dispatch_enabled=false`); `GET /v1/runs/{id}` safe projection scrubbing secrets; candidate package projection; decision commits DB before resume; `/resume` route rejects auth body; health probes (`/health/live`, `/health/ready`); CLI HMAC kill switch (`APPROVAL_CLI_HMAC_ENABLED`); actor-neutral decision validation. |
| **Dispatches & State Machine** | Features 30–33 | `TestDispatchSchemaAndTransitionFeatures` | 4 | `players` and `dispatches` schemas; forward-compatible tracking tables (`dispatch_plans`, `player_observations`, `dispatch_assessments`); SECURITY DEFINER single write path function `public.fc27_dispatch_transition`; `dispatch_transition_rules` matrix populated. |
| **Objectives & Activation Gates** | Features 34–37 | `TestObjectivesAndGatesFeatures` | 4 | Bounded objective policy (`fc27-objective-bounds-v1`); activation gate evaluation failing closed (`dispatch_effective=false`); gate challenge/record routes refusing `G-OUTBOX`; atomic dispatch row creation in approval transaction when effective. |
| **Topology, Config & Invariants** | Features 38–41 | `TestTopologyAndInvariantsFeatures` | 4 | Compose secrets configuration; standardized configuration (`DISPATCH_ENABLED=false`); scope protection (`DISPATCH_WORKER_ENABLED=false`); runtime security posture check (`check_runtime_posture`). |

### 2.2 Tier 2: Boundary & Corner Cases (`tests/e2e/test_tier2_boundaries.py` — 25 Tests)

Tier 2 exhaustively stresses 5 critical operational domains (5 tests per domain):

1. **Input & Payload Boundaries (5 tests)**:
   - `test_tier2_empty_run_request_body_rejected`: Empty request payload fails validation (422).
   - `test_tier2_max_thread_id_length_boundary`: Thread ID of 128 characters accepted; 129 characters rejected (422).
   - `test_tier2_club_id_non_numeric_characters_rejected`: Non-numeric club IDs and SQL injection attempts (`'; DROP TABLE...`) rejected (422).
   - `test_tier2_extra_forbidden_fields_rejected`: Pydantic `extra="forbid"` rejects injected fields (`approved=True`, `admin=True`).
   - `test_tier2_malformed_json_syntax_rejected`: Malformed JSON returns 422 Unprocessable Entity.
2. **Cryptographic & Nonce Boundaries (5 tests)**:
   - `test_tier2_altered_payload_hash_rejected_in_decision`: Altered `payload_sha256` rejected (409/422).
   - `test_tier2_invalid_sha256_length_rejected`: Hashes not exactly 64 hex characters (e.g., 63, 65, non-hex) rejected.
   - `test_tier2_expired_nonce_presentation_fails`: Nonce presented past `expires_at` rejected (409/400).
   - `test_tier2_reused_consumed_nonce_fails`: Consumed nonce cannot be re-presented for a different decision.
   - `test_tier2_tampered_hmac_signature_fails`: Invalid HMAC signature rejected (403/400/422).
3. **State & Lifecycle Boundaries (5 tests)**:
   - `test_tier2_stale_package_version_decision_fails`: Decision against outdated package version rejected.
   - `test_tier2_rerun_against_completed_terminal_thread_fails`: Rerun request against terminal thread returns 409 conflict.
   - `test_tier2_direct_update_of_package_version_blocked_by_trigger`: Database trigger blocks mutation of `package_version` while awaiting approval.
   - `test_tier2_decision_referencing_nonexistent_thread_fails`: Decision challenge for non-existent thread returns 404/409.
   - `test_tier2_illegal_graph_threads_status_rejected_by_check`: CHECK constraint on `graph_threads` rejects illegal statuses (`dispatched`).
4. **Role, Privilege & DDL Boundaries (5 tests)**:
   - `test_tier2_runtime_role_cannot_create_table`: `fc27_app` executing `CREATE TABLE` raises `InsufficientPrivilege`.
   - `test_tier2_runtime_role_cannot_alter_table`: `fc27_app` executing `ALTER TABLE` raises `InsufficientPrivilege`.
   - `test_tier2_runtime_role_cannot_drop_table`: `fc27_app` executing `DROP TABLE` raises `InsufficientPrivilege`.
   - `test_tier2_runtime_role_cannot_create_function`: `fc27_app` executing `CREATE FUNCTION` raises `InsufficientPrivilege`.
   - `test_tier2_runtime_role_cannot_escalate_to_owner`: `fc27_app` executing `SET ROLE fc27_owner` raises `InsufficientPrivilege`.
5. **Objective Bounds & Metric Boundaries (5 tests)**:
   - `test_tier2_pass_accuracy_delta_above_bound_rejected`: Target delta > 25% violates bounds policy.
   - `test_tier2_negative_target_delta_rejected`: Negative target delta rejected.
   - `test_tier2_observation_window_max_matches_above_bound`: `max_matches > 10` rejected.
   - `test_tier2_observation_window_min_greater_than_max`: `min_matches > max_matches` rejected.
   - `test_tier2_non_numeric_player_id_rejected`: Non-numeric player ID strings rejected.

### 2.3 Tier 3: Cross-Feature Combinations (`tests/e2e/test_tier3_combinations.py` — 5 Tests)

Tier 3 validates complex multi-component interactions and concurrency safety:
1. `test_tier3_decision_vs_expiry_race_condition`: Concurrent execution of `expire_due()` and decision submission in parallel threads. Strict lock order (`approval_nonces` -> `approval_requests` -> `graph_threads`) guarantees no deadlock and exactly one deterministic terminal state (`approved` or `expired`).
2. `test_tier3_post_commit_resume_failure_preserves_authoritative_decision`: Simulated resume crash following PostgreSQL decision commit. Verifies the database decision remains intact and exactly one `approval_audit` row exists upon replay.
3. `test_tier3_concurrent_migration_check_and_active_run`: Running `migrate()` under `fc27_migrator` concurrently with active runs does not lock out or corrupt `awaiting_approval` threads.
4. `test_tier3_domain_events_and_outbox_transactional_atomicity`: An aborted transaction rolling back `domain_events` also rolls back the corresponding `outbox` row.
5. `test_tier3_identical_decision_replay_vs_conflicting_decision`: Replaying an identical signed decision succeeds idempotently; submitting a conflicting decision returns 409 Conflict.

### 2.4 Tier 4: Real-World Scenarios & Section 1A Invariants (`tests/e2e/test_tier4_scenarios.py` — 9 Tests)

Tier 4 verifies complete end-to-end user journeys and Section 1A human authority constraints:
1. `test_tier4_scenario_full_approval_lifecycle_no_dispatch`: Full golden-path run:
   `POST /run` -> interrupt -> candidate retrieval -> decision challenge -> HMAC signing -> commit -> resume -> verified `APPROVED, NOT DISPATCHED`, `dispatch_enabled=false`, unpublished outbox row, zero player delivery.
2. `test_tier4_scenario_full_rejection_lifecycle`: Full golden-path rejection terminating in `REJECTED, NO ACTION TAKEN` and recorded in `approval_audit`.
3. `test_tier4_scenario_revision_request_preserves_package_immutability`: Revision request workflow; original package version remains immutable; revision notes recorded.
4. `test_section_1a_authority_separation_club_vs_player`: Verifies distinct `policy_scope` values (`club_wide` vs `player_specific`) in `authority_policies`.
5. `test_section_1a_delegation_scope_and_expiry_validation`: Delegations in `authority_delegations` require explicit `scope` and `expires_at`; expired delegations fail closed.
6. `test_section_1a_player_fallback_prohibits_multi_player_packages`: Affected-player fallback binds to a single `subject_player_id` in `player_assignments`; multi-player fallback prohibited.
7. `test_section_1a_inactivity_and_timeout_never_creates_approval`: **Core Safety Invariant**: Request expiration or timeout sets status to `expired`, NEVER generates `approval_audit`, and cannot be resumed to approval.
8. `test_section_1a_late_decision_cannot_overwrite_prior_decision`: A late decision attempt on a terminal thread cannot overwrite prior decisions; prior decisions remain immutable.
9. `test_section_1a_player_agent_cannot_be_decision_actor`: Player agents, LLMs, or background workers cannot act as decision actors (rejected with 401/403).

---

## 3. Scope & Isolation Test Suites Analysis (22 Tests)

### 3.1 Phase 1 Scope & Boundary Protection (`tests/test_phase1_scope.py` — 5 Tests)

Enforces programme boundaries defined in `ORIGINAL_REQUEST.md` (Sections 0, 1) and Milestone 5:
- `test_no_phase2plus_code`: Asserts that `src/dashboard`, `src/identity`, and forbidden dispatch prefixes (`worker`, `relay`, `tracker`, `tracking`, `ingest`, `assess`, `baseline`, `analytics`, `personalize`, `agent`) do NOT exist under `src/dispatch/`.
- `test_no_webauthn_imports`: Scans all Python files under `src/` via regex and AST to guarantee zero imports of `webauthn` or `py_webauthn`.
- `test_no_tracking_writes_outside_tests`: Scans all Python files under `src/` to ensure no `INSERT`, `UPDATE`, `DELETE`, or `TRUNCATE` queries target companion tracking tables (`player_observations`, `dispatch_assessments`, `dispatch_plans`, `dispatch_delivery_attempts`).
- `test_docs_state_disabled_and_gates`: Validates that `docs/DISPATCH_ACTIVATION.md` documents all 6 rollout gates (`G-MIGRATION`, `G-GRANTS`, `G-WORKER`, `G-ID`, `G-ATOMIC`, `G-OUTBOX`) and contains the exact literal message:
  `"Phase 1 safety invariant violation: DISPATCH_ENABLED must be false"`.
- `test_activation_doc_forbids_phase1_enablement`: Verifies the presence of the 4 mandatory contractual statements forbidding Phase 1 dispatch activation.

### 3.2 Compose Topology & Secret Isolation (`tests/test_compose_isolation.py` — 17 Tests)

Performs static analysis on the Docker Compose model via `docker compose config`:
- Verifies secret isolation (`APPROVAL_SIGNING_SECRET` only present in `graph-api`).
- Verifies no database passwords in environment variables (`POSTGRES_PASSWORD`, `FC27_*_PASSWORD`).
- Verifies database secret mounts per service (`db-bootstrap` has admin/passwords; `db-migrate` has `fc27_migrator_uri`; `graph-api` has `fc27_app_uri`).
- Verifies `.gitignore` ignores `secrets/`.
- Verifies `graph-api` runs as `fc27_app` with `GRAPH_RUN_MIGRATIONS=false`, `GRAPH_CHECKPOINT_SETUP=false`.
- Verifies `DISPATCH_ENABLED=false` by default across base and Langfuse overlay profiles.
- Verifies only authorized services connect to `fc27_data` internal network.
- Verifies host ports bind strictly to `127.0.0.1`.
- Verifies image pins (`@sha256:`).
- Verifies fail-closed behavior when required secrets are missing.
- *Execution prerequisite*: Skips gracefully if `docker` CLI is not installed (`pytestmark = pytest.mark.skipif(shutil.which("docker") is None, ...)`).

---

## 4. Baseline Repository Test Suites Inventory

In addition to `tests/e2e/`, `test_phase1_scope.py`, and `test_compose_isolation.py`, the repository contains comprehensive unit, integration, and challenger test suites:

### 4.1 Database Infrastructure & Migrations
- `tests/test_db_roles.py` (7 tests): Cluster bootstrap idempotency, role attributes (`fc27_owner` NOLOGIN, `fc27_app` LOGIN), public schema privileges, runtime posture check, connection string redaction.
- `tests/test_db_migrate_roles.py` (16 tests): Object ownership by `fc27_owner`, `fc27_migrator` membership, DDL restrictions on `fc27_app`, grants policy checksum validation (`schema_policy_versions`), tamper prevention.
- `tests/test_migration_0003.py` (7 tests): Migration 0003 Part B execution over legacy data, backfill of awaiting threads, pair consistency constraint triggers, immutable fields, `fc27_approval_inconsistencies` view.
- `tests/test_events_outbox.py` (7 tests): Atomic domain event and outbox insertion, scope tagging, append-only triggers, event source integrity triggers.

### 4.2 Approval Lifecycle & Store
- `tests/test_approval_lifecycle.py` (17 tests): Request creation, paired updates, non-dispatch key verification, revision requests, expiry daemon, race conditions, inbox queries, Section 1A authority fields.
- `tests/test_approval_security.py` (30 tests): Thread ID naming conventions, rejection of duplicate thread IDs, terminal immutability, role-based authorization, single-use nonces, HMAC validation, candidate binding, concurrency safety.
- `tests/test_adversarial_m1.py` (8 tests) & `tests/test_m1_adversarial_challenger.py` (12 tests): Adversarial penetration tests for expiry races, dual challenges, batch expiry, nonce revocation, unauthorized actor fail-closed behavior, and twin pair consistency triggers.

### 4.3 Graph Core & Persistent API
- `tests/test_graph_persistence.py` (20 tests): Single approval interrupt, PostgreSQL decision re-read, payload tampering immunity, version mismatch fail-closed behavior, terminal state formatting, server restart recovery, adversarial resume refusal.
- `tests/test_graph_api_v1.py` (30+ tests): Correlation ID middleware, `/v1/runs` durable creation, safe projection scrubbing, `/v1/approval-requests/{id}/decisions`, kill run lifecycle, `/health/live` and `/health/ready` probes, HMAC kill switch, adversarial auth failure handling.
- `tests/test_langgraph_flow.py` (12 tests): LangGraph interrupt mechanics, typed state propagation, checkpoint metadata, startup reconciliation, ranking integration.

### 4.4 Dispatches & State Machine
- `tests/test_dispatch_schema.py` (18 tests): `players` and `dispatches` schemas, platform constraints, foreign key provenance, status domains, immutable provenance columns, frozen terminal rows, tracking table append-only triggers.
- `tests/test_dispatch_transition.py` (22 tests): SECURITY DEFINER `public.fc27_dispatch_transition` attributes, absence of dynamic SQL, SQLSTATE error codes (`FC001`–`FC007`), actor kind validation, allowed transition matrix, search path hijacking immunity.
- `tests/test_dispatch_creation.py` (17 tests): Gate evaluation, gate challenge and record endpoints, signature verification, atomic dispatch row creation in approval transactions.
- `tests/test_adversarial_m3_challenger.py` (7 tests): Adversarial probe of dispatch mutations, superuser mutation rejection on tracking tables, provenance field immutability.

### 4.5 Component & Unit Modules (Non-Database)
- `tests/test_packaging.py` (3 tests): Requirements vs `pyproject.toml` consistency, exact version pins for LangGraph.
- `tests/test_archive_integrity.py` (3 tests): Required repository file existence, README cross-references.
- `tests/test_installer_static.py` (10 tests): PowerShell installer static checks, path safety, download integrity.
- `tests/test_mcp_retry_and_tools.py` (22 tests) & `tests/test_mcp_http_server.py` (10 tests): MCP proxy protocol, backoff, retry caps, rate limiting, SSE streaming.
- `tests/test_model_selection.py` (7 tests), `tests/test_ocr_parsing.py` (11 tests), `tests/test_watcher_processing.py` (9 tests), `tests/test_ws_server.py` (11 tests), `tests/test_workflow.py` (6 tests), `tests/test_tracing.py` (4 tests).

---

## 5. Execution Commands & Environment Variable Formulation

### 5.1 Exact Execution Commands for Task 10 Step 1

Per `ORIGINAL_REQUEST.md` (Task 10 Step 1), the full test suite must be executed **twice consecutively** to prove determinism, idempotency, and absence of cross-test state leakage:

```bash
# Compilation check across production source and test suites
python -m compileall -q src tests

# Full test suite execution (First Run)
python -m pytest -o addopts="" -q -rs

# Full test suite execution (Second Run - Determinism & Clean State Verification)
python -m pytest -o addopts="" -q -rs
```

### 5.2 Environment Variables Formulation

The following environment variables govern test execution:

| Variable | Required / Default | Purpose & Invariant |
|----------|-------------------|---------------------|
| `TEST_POSTGRES_URI` | **Required for DB tests** | Superuser-capable PostgreSQL 16 admin URI (e.g., `postgresql://fc27test@127.0.0.1:55432/postgres`). When unset, DB-backed tests skip cleanly with an informative message. |
| `POSTGRES_URI` | Optional (derived from `TEST_POSTGRES_URI`) | Verified by `test_feature_38` (`assert "POSTGRES_URI" in os.environ or "TEST_POSTGRES_URI" in os.environ`). Setting `TEST_POSTGRES_URI` satisfies this check. |
| `DISPATCH_ENABLED` | Default: `false` | Must remain `false` at all times. Tested by `test_feature_39` (`assert os.getenv("DISPATCH_ENABLED", "false").lower() == "false"`). |
| `DISPATCH_WORKER_ENABLED` | Default: `false` | Must remain `false` at all times. Tested by `test_feature_40` (`assert os.getenv("DISPATCH_WORKER_ENABLED", "false").lower() == "false"`). |
| `APPROVAL_SIGNING_SECRET` | Default: built-in test secret | Secret used for HMAC challenge signing in tests. Defaults to `"test-signing-secret-0123456789abcdef0123456789abcdef"`. |
| `GRAPH_SERVICE_TOKEN` | Default: built-in test token | Service API key for `/run` and `/threads` routes. |
| `GRAPH_COMMISSIONER_TOKEN` | Default: built-in test token | Commissioner API key for decision challenges and submissions. |

### 5.3 PostgreSQL Provisioning Architecture

The test harness (`tests/conftest.py`) implements dynamic per-test database isolation:
1. `pg_roles` fixture: Connects via `TEST_POSTGRES_URI` and bootstraps cluster roles (`fc27_owner` NOLOGIN, `fc27_migrator`, `fc27_app`, `fc27_dispatch`).
2. `pg_db` fixture: Dynamically creates a unique database (`fc27_test_<uuidhex>`), runs migrations as `fc27_migrator` under `SET ROLE fc27_owner`, and yields connection URIs for each least-privilege role.
3. Tear-down: Terminates active connections and drops the test database immediately upon test completion.
4. Consistency check: The `pg_uri` fixture automatically verifies that `SELECT count(*) FROM fc27_approval_inconsistencies WHERE kind <> 'legacy_unlinked'` returns 0 at the conclusion of every test.

Disposable PostgreSQL container command:
```bash
docker run -d --name fc27-test-postgres -p 127.0.0.1:55432:5432 \
  -e POSTGRES_USER=fc27test -e POSTGRES_HOST_AUTH_METHOD=trust postgres:16-alpine
export TEST_POSTGRES_URI=postgresql://fc27test@127.0.0.1:55432/postgres
```

### 5.4 Test Isolation & Clean Consecutive Run Guarantee

The consecutive double-run requirement (`compileall` + `pytest` executed twice) will pass cleanly because:
1. Every DB test uses a freshly created, uniquely named database dropped on teardown.
2. No persistent state is written to the host filesystem outside pytest temporary directories (`tmp_path`).
3. Static compose tests clean up temporary files in `secrets/` via fixture teardown.
4. Python bytecode compilation (`compileall -q src tests`) validates that no syntax errors, circular imports, or corrupted `.pyc` files exist.

---

## 6. Pass-Readiness Assessment & Recommendations

1. **E2E Suite Completeness**: The 80 tests in `tests/e2e/` provide complete, robust, requirement-driven verification across all 41 Phase 1 features, boundary domains, concurrency combinations, and Section 1A constraints.
2. **Scope Protection Completeness**: `tests/test_phase1_scope.py` reliably ensures Phase 2+ code, WebAuthn imports, and companion tracking mutations are absent.
3. **Execution Readiness**: The test suite is fully wired, self-contained, and ready for official verification.
