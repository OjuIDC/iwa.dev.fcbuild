# Handoff Report: Phase 1 Database & Infrastructure Survey

**Agent**: `explorer_survey_db_1` (`teamwork_preview_explorer`)  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_survey_db_1`  
**Recipient**: `orchestrator_phase1` (`7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b`)  
**Task**: Comprehensive survey of repository database migrations, migration runner, database roles and grants, Docker Compose setup, environment handling, and database test harnesses against Phase 1 requirements (specifically Agent A and Agent D).  
**Handoff Type**: Hard (Task complete)

---

## 1. Observation

1. **Existing Migrations**:
   - `migrations/0001_initial.sql:3-21`: Creates baseline tables `graph_threads` and `approval_audit`.
   - `migrations/0002_approval_security.sql:5-123`: Alters `graph_threads` (adds columns `club_id`, `request_hash`, `package_version`, `created_at`, `approved_by`, `approved_at`, `decision_payload`); installs transition guard trigger `fc27_guard_thread_transition()` (lines 39-70); alters `approval_audit` and creates unique index `approval_audit_one_decision_per_thread` (line 96); creates `approval_nonces` (lines 99-109) and `run_sequences` (lines 117-122).
   - `migrations/0003_dispatch_foundation.sql:5-121`: Creates `domain_events` (lines 5-27) and `outbox` (lines 29-38); installs append-only triggers `fc27_domain_events_immutable` (lines 49-51) and `fc27_approval_audit_immutable` (lines 53-55); installs source check trigger `fc27_check_event_source()` (lines 57-121) which validates foreign sources against tables `approval_audit`, `approval_requests`, `dispatches`, etc.
   - `migrations/post/grants.sql:1-51`: Grants limited table/sequence permissions to `fc27_app` and `fc27_dispatch`.
2. **Migration Runner**:
   - `src/db_migrate.py:12-21`: Defines `MIGRATIONS_DIR`, `_LOCK_KEY = 0x0FC27_0001`, and `GRANTS_POLICY = GrantsPolicy(version=2, sha256="a0c8d37ed61daff8900b116f992fa791f5072c11862c17c264cbc9251f0f4f79")`.
   - `src/db_migrate.py:84-93`: Verifies `fc27_owner` role exists and that `current_user` has membership in `fc27_owner`, then executes `SET ROLE fc27_owner`.
   - `src/db_migrate.py:94-130`: Acquires `pg_advisory_lock(_LOCK_KEY)`, ensures `schema_migrations` and `schema_policy_versions` tables, computes SHA-256 for each migration, and fails if an applied migration's hash changed (line 120-121).
   - `src/db_migrate.py:131-136`: If `checkpoint_setup=True`, executes `PostgresSaver(check_conn).setup()` under `fc27_owner`.
   - `src/db_migrate.py:138-159`: Validates `grants.sql` against `GRANTS_POLICY` version and SHA-256 before applying.
   - `src/db_migrate.py:38-58, 160-162`: Verifies postconditions ensuring `fc27_app` is not superuser, not owner member, and has no `CREATE` privilege on schema `public`.
3. **Database Roles**:
   - `src/db_roles.py:13-16`: Defines roles `fc27_owner`, `fc27_migrator`, `fc27_app`, `fc27_dispatch`.
   - `src/db_roles.py:40-59`: `bootstrap_cluster()` creates roles, grants `fc27_owner` to `fc27_migrator`, sets passwords.
   - `src/db_roles.py:80-200`: `bootstrap_database()` revokes `CREATE` on public from PUBLIC, grants `USAGE, CREATE` to `fc27_owner`, grants `USAGE` to `fc27_app` and `fc27_dispatch`, and transfers object ownership to `fc27_owner`.
   - `src/db_roles.py:60-78` & `src/db_migrate.py:60-75`: `check_runtime_posture()` tests current user attributes for privilege leakage.
4. **Docker Compose & Configuration**:
   - `docker-compose.yml:37-52`: `postgres` service configures `POSTGRES_USER: fc27`.
   - `docker-compose.yml:148-199`: `graph-api` service configures `POSTGRES_URI: postgresql://fc27:${POSTGRES_PASSWORD:?Set POSTGRES_PASSWORD in .env}@postgres:5432/fc27` (running as superuser `fc27`).
   - `docker-compose.yml:15-17`: `fc27_data` network has `internal: true`.
   - `docker-compose.yml:25-26`: `FC27_MCP_URL: http://fc27-mcp:8000` and `GRAPH_URL: http://graph-api:8001`.
   - `scripts/commissioner_decide.py:38`, `tests/test_commissioner_cli.py:19`, `docs/APPROVAL_SECURITY.md:32`: Reads `FC27_GRAPH_URL` instead of `GRAPH_URL`.
   - `DISPATCH_ENABLED`: Completely missing from `docker-compose.yml`, `config.example.env`, and `src/settings.py`.
5. **Database Tests**:
   - `tests/conftest.py:26-35, 107-163`: Session fixtures require `TEST_POSTGRES_URI`, run `bootstrap_cluster()`, dynamically spin up isolated DB `fc27_test_<uuid>`, run `bootstrap_database()`, execute `migrate()`, and provide typed fixtures `pg_admin_db_uri`, `pg_migrator_uri`, `pg_uri` (`fc27_app`), and `pg_dispatch_uri`.
   - `tests/test_db_migrate_roles.py:1-182`: 18 tests verifying owner object ownership, app role DDL blocking, checkpointer DDL requirements, migration tamper detection, concurrent migration idempotency, postcondition enforcement, and grants policy verification.
   - `tests/test_db_roles.py:1-120`: 9 tests verifying role attributes, cluster bootstrap idempotency, public schema isolation, object transfer, runtime posture checks, and password redaction.
   - `tests/test_approval_security.py:1-320`: 23 tests verifying thread ID conventions, terminal state immutability, role-based key authorization, nonce single-use/expiry/candidate binding, atomic commit of decisions and audits, and race conditions.
   - `tests/test_events_outbox.py:1-155`: 7 tests verifying `record_event()` outbox writes, append-only triggers, foreign source verification, and dispatch event restrictions.
6. **Schema & Runtime Usage Gaps**:
   - `recommendation_packages` table: Missing. Lead packages are stored only as a JSONB field inside `graph_threads.state` (`src/lead_packages.py:132`, `src/graph.py:132-137`).
   - `approval_requests` table: Missing. Nonces are used instead of structured approval requests (`src/approval_store.py:154-159`), though `migrations/0003_dispatch_foundation.sql:86` already expects `approval_requests`.
   - Decision idempotency: In `src/approval_store.py:188-194`, submitting an identical decision twice raises `StoreError(409, "nonce already used (replay rejected)")`, violating Section 3.4 & 11.1 requirement that identical idempotent decisions succeed safely.
   - Event integration: `src/events.py:record_event()` is never called in `src/approval_store.py`, `src/graph.py`, or `src/graph_api.py`.

---

## 2. Logic Chain

1. **Schema Deficiencies (Agent A)**:
   - *From Observation 1 & 6*: `recommendation_packages` and `approval_requests` do not exist as relational tables in `migrations/0001`, `0002`, or `0003`.
   - *From ORIGINAL_REQUEST.md Section 3.2, 3.3*: Phase 1 mandates immutable `recommendation_packages` (with deterministic hash, explicit version, and supersession) and `approval_requests` (binding exact package ID, version, and hash).
   - *From Observation 1 (0003:86)*: `migrations/0003_dispatch_foundation.sql` already includes a check trigger referencing `source_table = 'approval_requests'`.
   - *Inference*: A new migration (`0004`) is required to introduce `recommendation_packages` and `approval_requests`, and update `migrations/post/grants.sql` with a corresponding version bump in `src/db_migrate.py:GRANTS_POLICY`.
2. **Decision Idempotency Deficiencies (Agent A)**:
   - *From Observation 1 & 6*: `approval_audit_one_decision_per_thread` is a unique index on `thread_id`. `approval_nonces.consumed_at` is marked on first decision. `ApprovalStore.apply_decision` checks `nonce["consumed_at"] is not None` and throws 409.
   - *From ORIGINAL_REQUEST.md Section 3.4 & 11.1*: "duplicate identical idempotent request: safe", "Repeated identical idempotent decision submission is safe."
   - *Inference*: `ApprovalStore.apply_decision` must be updated to detect an identical repeated submission (matching exact request, package ID, version, hash, decision, notes, actor) and return the committed decision safely rather than raising 409 Conflict.
3. **Audit & Event Integration Deficiencies (Agent A)**:
   - *From Observation 1 & 6*: `domain_events` and `outbox` exist in schema and `record_event()` exists in `src/events.py`, but are only referenced in `tests/test_events_outbox.py`.
   - *From ORIGINAL_REQUEST.md Section 3.5 & 3.6*: Authoritative events for package created, approval request created, approval decision committed, graph resume requested, and terminal states must be recorded in the same database transaction.
   - *Inference*: `ApprovalStore` and `graph.py` must be integrated with `record_event()` to emit domain events and outbox rows during package generation, approval request issuance, and decision commit.
4. **Privilege & Compose Deficiencies (Agent D)**:
   - *From Observation 4*: `docker-compose.yml:160` sets `POSTGRES_URI: postgresql://fc27:...@postgres:5432/fc27`, connecting as `fc27` (the container superuser/owner).
   - *From Observation 2 & 3*: `src/db_migrate.py:60-75` and `src/db_roles.py:60-78` implement `check_runtime_posture()`, which fails with `['superuser']` if called by `fc27`.
   - *From ORIGINAL_REQUEST.md Section 6 & 11.4*: "run under the intended least-privileged application database role (`fc27_app`)", "do not let graph-api run as database owner/superuser", "migrations continue to run under the migration role (`fc27_migrator`)".
   - *Inference*: `docker-compose.yml` must configure `graph-api` with `fc27_app` credentials, and provide a migration/bootstrap execution mechanism using `fc27_migrator`.
5. **Configuration Deficiencies (Agent D)**:
   - *From Observation 4*: `DISPATCH_ENABLED` is completely missing from `docker-compose.yml`, `config.example.env`, and `src/settings.py`.
   - *From ORIGINAL_REQUEST.md Section 1.1, 6, 11.4*: `DISPATCH_ENABLED=false` must be present and enforced at all times, with readiness failing if invalid.
   - *Inference*: `src/settings.py` must load and validate `DISPATCH_ENABLED=false`, `config.example.env` and `docker-compose.yml` must explicitly declare it, and `/health/ready` in `src/graph_api.py` must check it.

---

## 3. Caveats

1. **Subsystem Isolation**: Unrelated live monitoring, OCR, rundown, and Swarms logic were inspected only to the extent they consume database configuration (`POSTGRES_URI`, `GRAPH_URL`, `FC27_MCP_URL`). Per Section 2 of `ORIGINAL_REQUEST.md`, these were not analyzed for modifications.
2. **Container Runtime Execution**: Per agent role constraints and permission model, terminal commands (`docker compose up`, `pytest`) were not executed dynamically; all observations are based on direct static analysis of source code, configurations, SQL migrations, and tests.
3. **Compatibility Seams**: Introducing `recommendation_packages` and `approval_requests` tables must preserve existing `graph_threads` records or maintain compatibility seams so existing passing tests in `tests/test_approval_security.py` continue to pass without regression.

---

## 4. Conclusion

The repository already possesses a high-quality foundation for migration serialization, advisory locking, tamper detection, and database role isolation. However, significant gaps exist between the current branch and Phase 1 acceptance criteria:
1. **Agent A must implement**:
   - Migration `0004` adding `recommendation_packages` and `approval_requests` tables with deterministic hashing and foreign key constraints.
   - Reconcile `approval_decisions` / `approval_audit` to bind to `approval_requests` and handle safe identical idempotency replays.
   - Integrate `src/events.py:record_event()` into `src/approval_store.py` and `src/graph.py` to record domain events and transactional outbox entries across the lifecycle.
   - Bump `GRANTS_POLICY` version in `src/db_migrate.py` and update `migrations/post/grants.sql`.
2. **Agent D must implement**:
   - Reconfigure `docker-compose.yml` so `graph-api` connects as `fc27_app`, not `fc27`.
   - Add automated bootstrap/migration handling for `fc27_migrator`.
   - Introduce `DISPATCH_ENABLED=false` in `docker-compose.yml`, `config.example.env`, and `src/settings.py`.
   - Standardize `GRAPH_URL` vs `FC27_GRAPH_URL` in `scripts/commissioner_decide.py`.
   - Implement `GET /health/live` and `GET /health/ready` (checking database connectivity, checkpointer readiness, role posture, and `DISPATCH_ENABLED=false`).

---

## 5. Verification Method

1. **Inspect Survey Report**:
   - Review complete details in `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_survey_db_1\survey_db_report.md`.
2. **Inspect Referenced Code & Configuration**:
   - `migrations/0001_initial.sql`, `migrations/0002_approval_security.sql`, `migrations/0003_dispatch_foundation.sql`, `migrations/post/grants.sql`.
   - `src/db_migrate.py:21, 79-166`.
   - `src/db_roles.py:13-78, 80-200`.
   - `docker-compose.yml:22-28, 37-52, 148-199`.
   - `tests/conftest.py:26-35, 107-163`.
   - `tests/test_db_migrate_roles.py:12-182`.
   - `tests/test_db_roles.py:10-119`.
   - `tests/test_approval_security.py:46-320`.
   - `tests/test_events_outbox.py:6-155`.
3. **Invalidation Conditions**:
   - If a `recommendation_packages` or `approval_requests` table already exists in an uninspected location, this finding is invalidated. (Confirmed absent via ripgrep search).
   - If `docker-compose.yml` already runs `graph-api` as `fc27_app`, this finding is invalidated. (Confirmed line 160 uses user `fc27`).
