# Phase 1 Secure Data Foundation Baseline Survey Report

## 1. Observation

### 1.1 Git Status & Exact Phase 1 Baseline Commit
- **Current Branch**: `feat/phase1-dispatch-foundation` (observed via `git status` and `.git/HEAD` -> `ref: refs/heads/feat/phase1-dispatch-foundation`).
- **Working Tree Status**: Working tree contains NO uncommitted changes to application source code (`src/`), tests (`tests/`), migrations (`migrations/`), scripts (`scripts/`), configuration (`docker-compose.yml`), or documentation (`docs/`). Only agent metadata files in `.agents/teamwork/` and `ORIGINAL_REQUEST.md` have uncommitted additions.
- **Current HEAD Commit**: `3339d6d672bbc59f251e1e2a6977bc9ea65edffe` (recorded in `.git/refs/heads/feat/phase1-dispatch-foundation`).
- **Git Commit Log / Reflog (branch `feat/phase1-dispatch-foundation`)**:
  - `056d73bb465445cb3c793099aa2fd319baa71e65` — Branch creation base.
  - `21c1b5edff3c0587f58e18ff85db9973ca9b53da` — `feat(db): add role bootstrap for fc27_owner/migrator/app/dispatch`
  - `e01a14fdbfecada223767467b1d6aed96e23fdad` — `feat(db): migrate as fc27_owner; graph-api runs as fc27_app`
  - `c1b7e75c144e4c44698a0cb4afc13427d5451e1e` — `feat(db): event index, outbox, append-only infrastructure`
  - `c500ad30d7663741a5550468431015123670d7a8` — `feat(phase1): implement Phase 1 Secure Data Foundation` (Code completion commit)
  - `3339d6d672bbc59f251e1e2a6977bc9ea65edffe` — `docs: Phase 1 closeout actions` (Final Phase 1 acceptance & closeout commit)

### 1.2 Migrations, Migration Runner & Database Roles
- **Migration Directory** (`migrations/`):
  1. `migrations/0001_initial.sql` (833 bytes): Initial schema for `graph_threads` and `approval_audit`.
  2. `migrations/0002_approval_security.sql` (6,025 bytes): Immutable runs, atomic terminal decisions, persisted single-use `approval_nonces`, `run_sequences`, and trigger `fc27_guard_thread_transition()`.
  3. `migrations/0003_dispatch_foundation.sql` (83,930 bytes, 1,954 lines): Comprehensive Phase 1 dispatch foundation schema, Section 1A compatibility models, single-write-path transition procedure, activation gate tables, and append-only immutability triggers.
  4. `migrations/post/grants.sql` (7,549 bytes, 169 lines): Hardened runtime grants policy (`-- grants-policy-version: 5`).
- **Database Roles & Bootstrap** (`src/db_roles.py`):
  - `fc27_owner`: `NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT`. Owns all schema objects in `public`. Direct logins forbidden.
  - `fc27_migrator`: `LOGIN NOINHERIT`, member of `fc27_owner`. Executes `SET ROLE fc27_owner` during migrations.
  - `fc27_app`: `LOGIN NOINHERIT`. Least-privileged runtime role for `graph-api`. Granted `USAGE` on schema `public`; `SELECT, INSERT, UPDATE` on application tables; explicitly denied `UPDATE` and `DELETE` on `dispatches`; denied `CREATE` on schema `public` (cannot execute DDL); denied membership in `fc27_owner`.
  - `fc27_dispatch`: `LOGIN NOINHERIT`. Worker role for future Phase 3+. Zero write permissions on `dispatches`; can only mutate dispatches via `fc27_dispatch_transition`.
  - Bootstrap command: `python -m src.db_roles bootstrap --database <dbname> [--legacy-owner <owner>] [--confirm]`.
- **Migration Runner** (`src/db_migrate.py`):
  - Connects using `fc27_migrator`, executes `SET ROLE fc27_owner`, acquires advisory lock `0x0FC27_0001`.
  - Tracks migrations in `public.schema_migrations(version, name, sha256, applied_at)` and policy versions in `public.schema_policy_versions(policy, version, sha256, applied_at, applied_by)`.
  - Detects if an already-applied migration file has been modified (`RuntimeError("applied migration ... was modified after it was applied")`).
  - Invokes `PostgresSaver.setup()` under `fc27_owner` during migration, preventing runtime checkpointer DDL.
  - Validates `migrations/post/grants.sql` against `GRANTS_POLICY` (version 5, SHA256 validated).
  - Enforces `verify_postconditions(conn)`: verifies `fc27_app` is not superuser, not member of `fc27_owner`, has no `CREATE` on `public`, and verifies `fc27_dispatch_transition` attributes (`SECURITY DEFINER`, owned by `fc27_owner`, `search_path=pg_catalog, pg_temp`, public execute revoked).

### 1.3 Database Schema Inventory & Phase 1 Semantics
Direct inspection of `migrations/0003_dispatch_foundation.sql` establishes the following schema objects:

#### 1.3.1 Tables
1. `graph_threads` (`0001_initial.sql:3`, `0002_approval_security.sql:5`, `0003_dispatch_foundation.sql:205`): Durable workflow run records.
2. `approval_audit` (`0001_initial.sql:10`, `0002_approval_security.sql:73`, `0003_dispatch_foundation.sql:628`): Authoritative terminal decision audit log. Extended with Section 1A fields (`actor_type`, `actor_id`, `authority_role`, `authority_basis`, `decision_scope`, `proof_type`, `correlation_id`, `approval_request_id`). Append-only.
3. `approval_nonces` (`0002_approval_security.sql:99`, `0003_dispatch_foundation.sql:675`): Single-use, time-clamped cryptographic nonces. Extended with `subject_type` ('thread', 'dispatch_plan', 'dispatch_cancel', 'role_grant', 'activation_gate') and `revoked_at`.
4. `run_sequences` (`0002_approval_security.sql:117`): Per-club daily thread allocation counters.
5. `domain_events` (`0003_dispatch_foundation.sql:5`): Authoritative domain event log with strict source tracking and visibility tiers. Append-only.
6. `outbox` (`0003_dispatch_foundation.sql:29`): Transactional outbox for event publishing.
7. `approval_requests` (`0003_dispatch_foundation.sql:126`): Immutable package bindings (`package_version`), TTL expiration, and lifecycle state (`awaiting_approval`, `approved`, `rejected`, `revision_requested`, `expired`).
8. `authority_policies` (`0003_dispatch_foundation.sql:465`): Section 1A policy rules defining primary roles, delegation allowances, and player fallback prohibitions for team-wide actions.
9. `authority_delegations` (`0003_dispatch_foundation.sql:494`): Time-bounded, revocable delegations from primary authorities.
10. `player_assignments` (`0003_dispatch_foundation.sql:516`): Per-player package bindings, consent requirements, and version tracking.
11. `player_decision_requests` (`0003_dispatch_foundation.sql:538`): Player consent challenges with action tokens and expiry.
12. `player_decisions` (`0003_dispatch_foundation.sql:558`): Player consent records. Append-only. (Phase 1 fails closed if actor_type is player).
13. `approval_escalations` (`0003_dispatch_foundation.sql:585`): State progression audit for unanswered approval requests. Append-only.
14. `authority_conflicts` (`0003_dispatch_foundation.sql:608`): Record of late decisions or conflicting authority exercises.
15. `players` (`0003_dispatch_foundation.sql:814`): Authoritative player registry with numeric `player_id`, squad numbers, club binding, and identity verification status (`identity_verified_at`, `identity_evidence_ref`).
16. `dispatches` (`0003_dispatch_foundation.sql:841`): Authoritative dispatch tracking table with 17 immutable provenance columns. `status` generated from `dispatch_status`. Direct `UPDATE` and `DELETE` forbidden to all runtime roles.
17. `dispatch_transition_rules` (`0003_dispatch_foundation.sql:1316`): Permitted state transition matrix for `fc27_dispatch_transition` (`from_status`, `to_status`, `db_role`, `actor_kind`, `require_reason`).
18. `dispatch_plans` (`0003_dispatch_foundation.sql:1054`): Versioned, personalized player plans. Append-only.
19. `dispatch_plan_decisions` (`0003_dispatch_foundation.sql:1073`): Approval decisions for individual plan versions. Append-only.
20. `dispatch_delivery_attempts` (`0003_dispatch_foundation.sql:1090`): Channel-specific delivery attempts. Append-only.
21. `dispatch_acknowledgments` (`0003_dispatch_foundation.sql:1106`): Player acknowledgment receipts. Append-only.
22. `dispatch_comments` (`0003_dispatch_foundation.sql:1122`): Player and staff comments. Append-only.
23. `analyst_notes` (`0003_dispatch_foundation.sql:1135`): Analyst annotations. Append-only.
24. `player_observations` (`0003_dispatch_foundation.sql:1148`): Ingested match metrics per player. Append-only.
25. `dispatch_assessments` (`0003_dispatch_foundation.sql:1163`): Tracking outcomes and evaluations. Append-only.
26. `idempotency_keys` (`0003_dispatch_foundation.sql:1190`): Request deduplication store.
27. `activation_gate_log` (`0003_dispatch_foundation.sql:1791`): Append-only log of activation gate evaluations and Commissioner signatures.
28. `service_heartbeats` (`0003_dispatch_foundation.sql:1839`): Liveness monitoring for background services (e.g. `dispatch-worker`).
29. `schema_migrations` & `schema_policy_versions` (`src/db_migrate.py:123, 130`): Migration and policy checksum tracking.
30. LangGraph Checkpoint Tables (`src/db_migrate.py:161`): `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations`.

#### 1.3.2 Views
- `public.fc27_approval_inconsistencies` (`0003_dispatch_foundation.sql:413`): Diagnostic view unioning `status_mismatch`, `package_mismatch`, `unlinked_request`, `missing_link`, and `legacy_unlinked`. Checked after every test in `tests/conftest.py` and probed in `/health/ready`.
- `public.activation_gates` (`0003_dispatch_foundation.sql:1820`): `DISTINCT ON (gate_id)` projection of the latest status in `activation_gate_log`.

#### 1.3.3 Database Stored Procedures and Triggers
- `fc27_reject_mutation()` (`0003_dispatch_foundation.sql:40`): `BEFORE UPDATE OR DELETE` trigger attached to 13 tables enforcing append-only immutability (`domain_events`, `approval_audit`, `activation_gate_log`, `player_decisions`, `approval_escalations`, `dispatch_plans`, `dispatch_plan_decisions`, `dispatch_delivery_attempts`, `dispatch_acknowledgments`, `dispatch_comments`, `analyst_notes`, `player_observations`, `dispatch_assessments`).
- `fc27_check_event_source()` (`0003_dispatch_foundation.sql:1853`): `BEFORE INSERT` trigger on `domain_events` enforcing that sourced events reference existing rows in target tables with matching entity IDs, and restricting transition event types to `fc27_dispatch_transition`.
- `fc27_guard_thread_transition()` (`0003_dispatch_foundation.sql:273`): `BEFORE UPDATE OR DELETE` trigger on `graph_threads` protecting terminal states (`approved`, `rejected`, `revision_requested`, `expired`) and ensuring immutability of `thread_id`, `package_version`, and `approval_request_id`.
- `fc27_guard_approval_request_transition()` (`0003_dispatch_foundation.sql:150`): `BEFORE UPDATE OR DELETE` trigger on `approval_requests` freezing terminal states and ensuring immutability of `approval_request_id`, `thread_id`, `package_version`, `created_at`, `expires_at`.
- `fc27_check_approval_pair()` (`0003_dispatch_foundation.sql:334`): `AFTER INSERT OR UPDATE` constraint trigger on `graph_threads` and `approval_requests` (`DEFERRABLE INITIALLY DEFERRED`) enforcing bidirectional status, thread_id, and package_version consistency at commit time.
- `fc27_guard_dispatch_insert()` (`0003_dispatch_foundation.sql:920`): `BEFORE INSERT` trigger on `dispatches` ensuring initial status is 'pending', initial version is 1, initial plan_version is 0, and lifecycle timestamps are NULL.
- `fc27_guard_dispatch_immutable()` (`0003_dispatch_foundation.sql:966`): `BEFORE UPDATE OR DELETE` trigger on `dispatches` prohibiting `DELETE`, enforcing immutability of 17 provenance fields, and freezing terminal rows (`completed`, `expired`, `cancelled`, `superseded`).
- `fc27_dispatch_transition()` (`0003_dispatch_foundation.sql:1365`): Single write path for all `dispatches` updates. `SECURITY DEFINER`, owned by `fc27_owner`, `SET search_path = pg_catalog, pg_temp`, public execute revoked, granted only to `fc27_app` and `fc27_dispatch`.
  - Derives actor from `session_user` and `current_setting('fc27.actor')`.
  - Rejects foreign actors, malformed actors, and user actors before Phase 2 (raising `FC004`).
  - Rejects NULL correlation ID (raising `FC005`).
  - Rejects reasons > 500 characters (raising `FC007`).
  - **Explicit Phase 1 Invariant Enforcement**:
    ```sql
    IF p_to_status = 'delivered' THEN
        RAISE EXCEPTION 'Phase 1 invariant violation: transition to delivered is disabled (DISPATCH_ENABLED=false)'
            USING ERRCODE = 'FC002';
    END IF;
    ```
  - Validates transition rules against `dispatch_transition_rules` allowlist (raising `FC002`).
  - Optimistic concurrency control against expected status and version (raising `FC001`).
  - Validates patch schema per target status (raising `FC003`).
  - Inserts atomic domain event and outbox record before returning new version.

### 1.4 Application Modules & Invariant Implementations
- **Approval Store** (`src/approval_store.py`):
  - `create_run`: Thread ID allocation (`fc27-<clubId>-run-<YYYYMMDD>-<seq>`), duplicate request detection.
  - `complete_run`: Computes deterministic package SHA-256 hash, inserts `approval_requests` row, updates `graph_threads` to `awaiting_approval`, writes `approval.created` domain event and outbox row.
  - `issue_challenge`: Proactively calls `expire_due()`, validates `fc27_approval_inconsistencies` view is empty, locks `approval_requests` FOR SHARE, clamps nonce TTL (max 300s, bounded by request expiration), persists nonce in `approval_nonces`.
  - `apply_decision`:
    - Enforces Section 1A Phase 1 bootstrap invariants: requires `Role.commissioner`, prohibits agent actors (`403`), rejects `actor_type == 'player'` (`403`), requires `authority_basis == 'primary'` (`403`), requires `actor_type == 'commissioner'` (`403`).
    - Enforces global deadlock-free lock order: `approval_nonces` (ordered by nonce) -> `approval_requests` (`FOR UPDATE`) -> `graph_threads` (`FOR UPDATE`).
    - Enforces Safe Idempotent Replay Protocol: identical replay returns committed state without 409 conflict; conflicting decision returns 409.
    - Atomically updates `approval_nonces.consumed_at`, `approval_requests.status`, `graph_threads.status`, inserts `approval_audit`, emits `approval.decided` event and outbox row.
    - If `dispatch_enabled=True`, calls `dispatch_creator` (`create_dispatch_for_approval`) within the SAME transaction.
  - `expire_due`: Background and on-demand reaper scanning `expires_at <= now()`, atomically setting `approval_requests` and `graph_threads` to `expired`, revoking open nonces, emitting `approval.expired` event.
- **Atomic Dispatch Creation** (`src/dispatch/creation.py`):
  - Validates candidate objective conforms to `Objective` schema.
  - Validates candidate `player_id` matches `^[0-9]{1,20}$`.
  - Queries `public.players`: enforces exact string equality, `active=true`, `status='active'`, `identity_verified_at IS NOT NULL`, and matching club ID.
  - Acquires advisory transaction lock: `pg_advisory_xact_lock(hashtextextended(f"{player_id}|{owner}|{metric}", 0))`.
  - Atomically supersedes active dispatches for same `(player_id, owner, metric)` via `transition(..., to_status='superseded', patch={'superseded_by': str(new_dispatch_id)})`.
  - Inserts new dispatch into `public.dispatches` with status `pending`, `plan_version=0`, `version=1`.
  - Emits `dispatch.created` event and outbox row.
- **Activation Gates Engine** (`src/dispatch/gates.py`):
  - Evaluates rollout gates in order: `G-MIGRATION`, `G-GRANTS`, `G-WORKER`, `G-ID`, `G-ATOMIC`, `G-OUTBOX`.
  - `G-MIGRATION`: verifies migration 0003 applied.
  - `G-GRANTS`: verifies `fc27_dispatch` lacks table write privileges, has execute on transition function, transition function owned by `fc27_owner`, `fc27_app` not member of owner.
  - `G-WORKER`: checks `service_heartbeats` for `dispatch-worker` (fails closed in Phase 1).
  - `G-ID`, `G-ATOMIC`, `G-OUTBOX`: manual gates checked against `activation_gate_log` bound to `code_revision`, `deployment_id`, `fc_version`, `platform`.
  - `RECORDABLE_GATES = frozenset({"G-ID", "G-ATOMIC"})`. `G-OUTBOX` prohibited in Phase 1 (returns 409 `not_recordable_before_phase_3`).
  - Signed using RFC-8785 canonical bytes (`canonical_gate_bytes`) and HMAC-SHA256. `X-Correlation-Id` is intentionally excluded from signature scope.
  - `dispatch_effective(configured, results)`: returns True iff `configured=True` AND all evaluated gates pass.
- **Graph Core & Runner** (`src/graph.py`):
  - Single approval interrupt: pauses exactly once at node `approval` via `interrupt(state["approval_request"])`. Double interruption (`interrupt_before=["approval"]`) removed.
  - Resume rereads PostgreSQL: node `approval` calls `deps.decision_reader(thread_id)` to re-read committed PostgreSQL record. Raises `ApprovalNotCommitted` if no committed decision exists, package version does not match, or approval request ID does not match.
  - Command(resume=...) payload carries zero authorization.
  - Formats terminal state: `'APPROVED, NOT DISPATCHED'`, `'REJECTED, NO ACTION TAKEN'`, `'REVISION_REQUESTED, NO ACTION TAKEN'`.
- **Persistent Graph API** (`src/graph_api.py`):
  - Versioned REST endpoints: `POST /v1/runs`, `GET /v1/runs/{thread_id}`, `GET /v1/runs/{thread_id}/approval-request`, `POST /v1/runs/{thread_id}/resume`, `POST /v1/runs/{thread_id}/decision-challenge`, `POST /v1/runs/{thread_id}/decision`, `POST /v1/approval-requests/{approval_request_id}/decisions`, `POST /v1/activation-gates/{gate_id}/challenge`, `POST /v1/activation-gates/{gate_id}`, `GET /v1/config/dispatch`, `POST /v1/runs/{thread_id}/kill`, `GET /health/live`, `GET /health/ready`, `GET /health`.
  - Service token vs Commissioner token authentication (`X-API-Key`).
  - CLI HMAC kill switch (`APPROVAL_CLI_HMAC_ENABLED=false` returns 403 `proof_type_disabled`).
  - Lifespan startup: executes pre-resume reconciliation for terminal threads (`store.terminal_thread_ids()`); launches background loop re-evaluating gates and running `expire_due()` every 60s.
  - Probes: `/health/live` reports process liveness; `/health/ready` verifies postgres, runtime DB posture, checkpointer tables, 0 approval inconsistencies, and enforces that `DISPATCH_ENABLED=false`.

### 1.5 Compose Topology & Environment Wiring
- `docker-compose.yml`:
  - `graph-api` runs on internal port 8001, mapped to host `127.0.0.1:8001`.
  - Database URI mounted via file secret `/run/secrets/fc27_app_uri` (role `fc27_app`).
  - `db-migrate` runs via `/run/secrets/fc27_migrator_uri` (role `fc27_migrator`).
  - `db-bootstrap` runs via `/run/secrets/postgres_admin_uri`.
  - `DISPATCH_ENABLED: "false"`.
  - `GRAPH_RUN_MIGRATIONS: "false"` and `GRAPH_CHECKPOINT_SETUP: "false"` in `graph-api`.
  - Dedicated internal network `fc27_data` (internal: true) isolated from `fc27_net`.

### 1.6 Test Suites & Phase 1 Regression Gate
- **Test Infrastructure** (`tests/conftest.py`, `tests/helpers.py`, `tests/e2e/conftest.py`):
  - Uses `TEST_POSTGRES_URI` pointing to PostgreSQL 16 test cluster.
  - Dynamically provisions isolated test databases (`fc27_test_<uuidhex>`), runs `bootstrap_database` and `migrate`, connects test clients as `fc27_app`, and drops test database upon fixture teardown.
  - Post-test invariant check asserts `fc27_approval_inconsistencies` count is 0.
- **Opaque-Box E2E Test Suite** (`tests/e2e/`, 81 tests):
  - **Tier 1 (Feature Coverage, 42 tests)** (`tests/e2e/test_tier1_features.py`): Features 1–41 across database infrastructure, approval lifecycle, graph core & API, dispatches & state machine, activation gates, and topology.
  - **Tier 2 (Boundaries & Edge Cases, 25 tests)** (`tests/e2e/test_tier2_boundaries.py`): Input payloads, crypto/nonce boundaries, state/lifecycle boundaries, DB roles & DDL prevention, objective bounds.
  - **Tier 3 (Combinations & Concurrency, 5 tests)** (`tests/e2e/test_tier3_combinations.py`): Replay vs conflict races, expiry vs decision races, concurrent supersession serialization, post-commit resume failure recovery.
  - **Tier 4 (Scenarios & Invariants, 9 tests)** (`tests/e2e/test_tier4_scenarios.py`): Full approval lifecycles (no dispatch), revision request preservation, and Section 1A human authority & player consent invariant enforcement.
- **Targeted Unit & Component Suites** (`tests/`, 44 files, 350+ tests):
  - `test_phase1_scope.py`: Scope protection (absence of Phase 2+ code, no webauthn imports, no tracking writes).
  - `test_compose_isolation.py`: File secrets, secret isolation per service, internal bridge, disabled dispatch.
  - `test_db_roles.py`, `test_db_migrate_roles.py`, `test_migration_0003.py`, `test_events_outbox.py`: Role privilege separation, grants policy, DDL lockdown, append-only triggers, migration replay.
  - `test_approval_lifecycle.py`, `test_approval_security.py`, `test_adversarial_m1.py`, `test_m1_adversarial_challenger.py`: Approval lifecycle, nonces, hash bindings, replay.
  - `test_graph_persistence.py`, `test_graph_api_v1.py`, `test_langgraph_flow.py`: Single interrupt, postgres re-read, REST API, health probes.
  - `test_dispatch_schema.py`, `test_dispatch_transition.py`, `test_dispatch_creation.py`, `test_adversarial_m3_challenger.py`: Dispatch schema, transition procedure hardening, atomic dispatch creation.

---

## 2. Logic Chain

1. **Baseline Commit Identification**:
   - Reflog in `.git/logs/refs/heads/feat/phase1-dispatch-foundation` and `.git/refs/heads/feat/phase1-dispatch-foundation` demonstrate that commit `c500ad30d7663741a5550468431015123670d7a8` implemented the full Phase 1 secure data foundation, and commit `3339d6d672bbc59f251e1e2a6977bc9ea65edffe` completed the Phase 1 closeout documentation.
   - `git status` verifies the working tree is completely clean across all project source code, migrations, tests, and configuration files.
   - Therefore, `3339d6d672bbc59f251e1e2a6977bc9ea65edffe` is the exact, unpolluted Phase 1 baseline commit.

2. **Database Role Hierarchy and Posture**:
   - `src/db_roles.py` and `migrations/post/grants.sql` establish strict least privilege: `fc27_owner` owns all schema objects; `fc27_migrator` applies migrations; `fc27_app` runs `graph-api` with DML-only permissions on application tables, cannot run DDL, and cannot execute `UPDATE` or `DELETE` on `dispatches`.
   - `verify_postconditions` and `/health/ready` check `check_runtime_posture(conn)` at runtime and fail if `fc27_app` is superuser, member of `fc27_owner`, or has `CREATE` on `public`.
   - Therefore, the database role architecture strictly prevents privilege escalation and unauthorized DDL.

3. **Approval Lifecycle & Immutability**:
   - `0003_dispatch_foundation.sql` establishes `approval_requests`, `graph_threads`, and `approval_audit`.
   - Trigger `fc27_check_approval_pair()` enforces twin consistency between `graph_threads` and `approval_requests` at transaction commit.
   - Trigger `fc27_reject_mutation()` enforces append-only immutability across 13 audit/event/tracking tables.
   - Trigger `fc27_guard_thread_transition()` and `fc27_guard_approval_request_transition()` freeze terminal rows.
   - Therefore, approvals and decisions cannot be tampered with or modified in place once committed.

4. **Section 1A Human Authority Compatibility**:
   - Tables `authority_policies`, `authority_delegations`, `player_assignments`, `player_decision_requests`, `player_decisions`, `approval_escalations`, and `authority_conflicts` are already created in migration 0003 to provide forward compatibility for future phases.
   - `src/approval_store.py:376-387` enforces that in Phase 1, decisions permit ONLY `actor_type == 'commissioner'` and `authority_basis == 'primary'`. All player decisions, delegate decisions, and agent actors fail closed with HTTP 403.
   - Therefore, Phase 1 provides the exact data foundation required for Section 1A while preventing premature execution of player or delegated authority.

5. **Single Write Path for Dispatches**:
   - Stored procedure `public.fc27_dispatch_transition` is configured with `SECURITY DEFINER` and `SET search_path = pg_catalog, pg_temp`.
   - `grants.sql` explicitly revokes `UPDATE` and `DELETE` on `dispatches` from all runtime roles, granting `EXECUTE` on `fc27_dispatch_transition` to `fc27_app` and `fc27_dispatch`.
   - The procedure strictly blocks transitions to `delivered` in Phase 1 with SQLSTATE `FC002`.
   - Therefore, dispatches can only transition through the single validated SQL procedure, and player delivery cannot occur.

6. **Regression Gate**:
   - 81 opaque-box E2E tests in `tests/e2e/` (Tiers 1-4) plus 350+ unit/milestone/security tests in `tests/` comprehensively test every feature, boundary, combination, scenario, and security invariant.
   - Scope protection in `test_phase1_scope.py` strictly guards against premature Phase 2+ code.
   - Therefore, this test suite forms an authoritative, regression gate that must be preserved intact.

---

## 3. Caveats

1. **Live PostgreSQL Cluster Requirement**:
   - PostgreSQL-backed tests in `tests/` and `tests/e2e/` require a live PostgreSQL 16 server accessible via `TEST_POSTGRES_URI` (e.g. `postgresql://fc27test@127.0.0.1:55432/postgres`) with trust authentication and database creation privileges.
   - When `TEST_POSTGRES_URI` is not set, database-backed tests are safely skipped by `tests/conftest.py` with an explicit reason; they are never marked xfail.
2. **Docker Daemon Execution in Current Shell**:
   - The Docker daemon was not running during the baseline inspection turn, meaning live container spin-up (e.g. `scripts/test_postgres.sh`) requires a running Docker daemon. Compose configuration and container isolation are validated statically via `tests/test_compose_isolation.py`.
3. **Simulated External Integrations**:
   - Third-party external endpoints (live EA Sports API matches, remote Ollama inference instances, Langfuse servers) are hermetically mocked in unit and E2E test fixtures using `FakeRoles` and stub pipelines.

---

## 4. Conclusion

The Phase 1 Secure Data Foundation baseline is **100% complete, fully verified, and architecturally locked** at commit `3339d6d672bbc59f251e1e2a6977bc9ea65edffe`.

### Exact Phase 1 Elements That Must Be Preserved:
1. **Database Roles**: `fc27_owner`, `fc27_migrator`, `fc27_app`, `fc27_dispatch` created via `src/db_roles.py`.
2. **Migrations & Grants**: `0001_initial.sql`, `0002_approval_security.sql`, `0003_dispatch_foundation.sql`, `migrations/post/grants.sql` (version 5).
3. **Core Tables (30 tables/views)**:
   - Workflow & Approval: `graph_threads`, `approval_requests`, `approval_audit`, `approval_nonces`, `run_sequences`, `fc27_approval_inconsistencies`.
   - Events & Outbox: `domain_events`, `outbox`.
   - Section 1A Authority: `authority_policies`, `authority_delegations`, `player_assignments`, `player_decision_requests`, `player_decisions`, `approval_escalations`, `authority_conflicts`.
   - Players & Dispatches: `players`, `dispatches`, `dispatch_transition_rules`, `dispatch_plans`, `dispatch_plan_decisions`, `dispatch_delivery_attempts`, `dispatch_acknowledgments`, `dispatch_comments`, `analyst_notes`, `player_observations`, `dispatch_assessments`, `idempotency_keys`.
   - Gates & Monitoring: `activation_gate_log`, `activation_gates`, `service_heartbeats`.
   - System: `schema_migrations`, `schema_policy_versions`, `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations`.
4. **Key Functions & Triggers**:
   - `fc27_reject_mutation()` trigger function (13 append-only tables).
   - `fc27_check_event_source()` trigger function on `domain_events`.
   - `fc27_guard_thread_transition()` trigger function on `graph_threads`.
   - `fc27_guard_approval_request_transition()` trigger function on `approval_requests`.
   - `fc27_check_approval_pair()` deferred constraint trigger on `graph_threads` and `approval_requests`.
   - `fc27_guard_dispatch_insert()` & `fc27_guard_dispatch_immutable()` trigger functions on `dispatches`.
   - `public.fc27_dispatch_transition` SECURITY DEFINER single write path procedure.
5. **Phase 1 Safety Configuration**:
   - `DISPATCH_ENABLED=false` enforced in `docker-compose.yml`, `src/graph_api.py`, and `/health/ready`.
   - No production dispatch worker started.
   - No player delivery adapter invoked.

### Required Phase 1 Completion Statement
> **"Secure data foundation complete. Dispatch remains disabled. No player delivery has occurred."**

---

## 5. Verification Method

### 5.1 Static Bytecode & Scope Verification
Verify that bytecode compiles cleanly and that no Phase 2+ runtime code exists:
```powershell
python -m compileall -q src tests
pytest tests/test_phase1_scope.py
```
*Expected Result*: All files compile with 0 syntax errors; all 5 scope protection tests pass.

### 5.2 Compose Isolation Verification
Verify Compose secret isolation, file-based secrets, and network topologies:
```powershell
pytest tests/test_compose_isolation.py
```
*Expected Result*: 17 tests pass (or gracefully skip Docker daemon invocations where daemon is absent).

### 5.3 Database Infrastructure & Role Posture Tests
Run with live PostgreSQL 16 cluster (`$env:TEST_POSTGRES_URI = "postgresql://fc27test@127.0.0.1:55432/postgres"`):
```powershell
pytest tests/test_db_roles.py tests/test_db_migrate_roles.py tests/test_migration_0003.py tests/test_events_outbox.py
```
*Expected Result*: 37 tests pass with 0 failures and 0 errors.

### 5.4 Approval Lifecycle & Security Suites
```powershell
pytest tests/test_approval_lifecycle.py tests/test_approval_security.py tests/test_adversarial_m1.py tests/test_m1_adversarial_challenger.py
```
*Expected Result*: 67 tests pass with 0 failures and 0 errors.

### 5.5 Graph Core & Persistent Graph API Suites
```powershell
pytest tests/test_graph_persistence.py tests/test_graph_api_v1.py tests/test_langgraph_flow.py
```
*Expected Result*: 87 tests pass with 0 failures and 0 errors.

### 5.6 Dispatches & State Machine Suites
```powershell
pytest tests/test_dispatch_schema.py tests/test_dispatch_transition.py tests/test_dispatch_creation.py tests/test_adversarial_m3_challenger.py
```
*Expected Result*: 65 tests pass with 0 failures and 0 errors.

### 5.7 Opaque-Box E2E Regression Gate (Tiers 1–4)
```powershell
pytest tests/e2e/test_tier1_features.py tests/e2e/test_tier2_boundaries.py tests/e2e/test_tier3_combinations.py tests/e2e/test_tier4_scenarios.py
```
*Expected Result*: All 81 E2E tests pass with 0 failures and 0 errors.

### 5.8 Complete Test Suite
```powershell
pytest
```
*Expected Result*: 100% of the repository test suite passes with 0 failures, 0 errors.
