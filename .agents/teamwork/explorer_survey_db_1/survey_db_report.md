# Phase 1 Database & Infrastructure Survey Report

**Date**: 2026-10-03  
**Agent**: `explorer_survey_db_1` (`teamwork_preview_explorer`)  
**Scope**: Database schema, migration runner, database roles and grants, Docker Compose, configuration handling, and database test harnesses against Phase 1 requirements (specifically Agent A and Agent D).

---

## 1. Executive Summary

A comprehensive investigation was conducted across the codebase to examine the database migrations, migration runner, database role model, Docker Compose configuration, environment variables, and test harnesses.

### Key Conclusions:
1. **Migration Runner & Role Model are Substantially Hardened**: A forward-only migration runner (`src/db_migrate.py`) with advisory locking, SHA-256 tamper detection, role verification (`fc27_owner`), post-condition privilege checks, and post-migration grants policy hashing (`GRANTS_POLICY`) already exists. A 4-role model (`fc27_owner`, `fc27_migrator`, `fc27_app`, `fc27_dispatch`) is implemented in `src/db_roles.py`.
2. **Critical Schema Gaps Exist (Agent A)**:
   - There is **no dedicated `recommendation_packages` table**. Packages currently exist only as a JSONB dictionary inside `graph_threads.state` (`state->'lead_packages'`).
   - There is **no dedicated `approval_requests` table**. Approval requests exist only in memory/transient payloads and in `approval_nonces`. Notably, `migrations/0003_dispatch_foundation.sql:86` already has an event source trigger check enforcing `source_table = 'approval_requests'`, but the table does not yet exist!
   - Decisions are recorded in `approval_audit`, but duplicate identical decision submissions fail with `409 Conflict` (due to `approval_audit_one_decision_per_thread` and nonce consumption) rather than returning a safe idempotent success as required by Section 3.4 and Section 11.1.
   - Event and outbox recording (`src/events.py:record_event()`) is defined and tested in isolation (`tests/test_events_outbox.py`), but **never invoked** within the actual runtime workflow (`src/approval_store.py`, `src/graph.py`, or `src/graph_api.py`).
3. **Critical Infrastructure & Compose Gaps Exist (Agent D)**:
   - In `docker-compose.yml:160`, `graph-api` connects to PostgreSQL via `POSTGRES_URI: postgresql://fc27:${POSTGRES_PASSWORD}@postgres:5432/fc27`, which is the **database superuser/owner**, violating least privilege.
   - There is no container, init container, or dedicated orchestration step in `docker-compose.yml` to execute `python -m src.db_roles bootstrap` and `python -m src.db_migrate` under `fc27_migrator`.
   - `DISPATCH_ENABLED=false` is **completely absent** from `docker-compose.yml`, `config.example.env`, and `src/settings.py`.
   - Inconsistent configuration names exist: `GRAPH_URL` in `docker-compose.yml:26` vs `FC27_GRAPH_URL` in `scripts/commissioner_decide.py:38` and `tests/test_commissioner_cli.py:19`.

---

## 2. Database Migrations & Migration Runner

### 2.1 Existing Migration Inventory

The repository maintains migrations in `migrations/`:

| File | Purpose & Objects Created | Constraints / Guards |
|---|---|---|
| `migrations/0001_initial.sql` | Baseline tables: `graph_threads`, `approval_audit`. | `graph_threads.thread_id` PK; `approval_audit.thread_id` FK; `approval_audit.decision` CHECK (`APPROVE`, `REJECT`). |
| `migrations/0002_approval_security.sql` | Hardens runs and decisions: alters `graph_threads` (adds `club_id`, `request_hash`, `package_version`, `created_at`, `approved_by`, `approved_at`, `decision_payload`); adds `approval_nonces`, `run_sequences`. | Triggers `fc27_guard_thread_transition()` on `graph_threads` (preventing deletion/modification of terminal states and enforcing transition order); unique index `approval_audit_one_decision_per_thread`; foreign key from `approval_audit.nonce` to `approval_nonces.nonce`. |
| `migrations/0003_dispatch_foundation.sql` | Event and outbox foundation: `domain_events`, `outbox`. | Trigger `fc27_domain_events_immutable` & `fc27_approval_audit_immutable` (rejects UPDATE/DELETE); trigger `fc27_domain_events_source_check` (`fc27_check_event_source()` validating event types against source tables `approval_audit`, `approval_requests`, `dispatches`, etc.). |
| `migrations/post/grants.sql` | Post-migration table and sequence grants. | Grants limited SELECT, INSERT, UPDATE on application tables to `fc27_app`; grants SELECT on `domain_events` to `fc27_dispatch`. |

### 2.2 Migration Runner Architecture (`src/db_migrate.py`)

1. **Ordering & Discovery** (`src/db_migrate.py:26-36`):
   - Scans `migrations/*.sql` matching regex `^(\d{4})_[a-z0-9_]+\.sql$`.
   - Enforces unique version numbers.
2. **Serialization & Concurrency** (`src/db_migrate.py:14, 94, 165`):
   - Obtains PostgreSQL session-level advisory lock `_LOCK_KEY = 0x0FC27_0001` (`SELECT pg_advisory_lock(...)`).
   - Unlocks in a `finally` block.
3. **Execution Context & Role Verification** (`src/db_migrate.py:84-92`):
   - Verifies `fc27_owner` role exists in `pg_roles`.
   - Checks `pg_has_role(current_user, 'fc27_owner', 'MEMBER')`. If current user is not a member, raises `MigrationError("run python -m src.db_roles bootstrap first")`.
   - Executes `SET ROLE fc27_owner` before applying DDL.
4. **Idempotency & Tamper Detection** (`src/db_migrate.py:97-129`):
   - Tracking table `schema_migrations (version text PK, name text, sha256 text, applied_at timestamptz)`.
   - Re-running checks existing versions in `schema_migrations`.
   - Computes SHA-256 of migration file content; if `version in done` and SHA-256 differs, raises `RuntimeError(f"applied migration {path.name} was modified after it was applied")`.
5. **LangGraph Checkpoint Setup** (`src/db_migrate.py:131-136`):
   - If `checkpoint_setup=True`, connects with `autocommit=True`, runs `SET ROLE fc27_owner`, and executes `PostgresSaver(check_conn).setup()`.
   - This creates `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations`.
6. **Post-Migration Grants Enforcement** (`src/db_migrate.py:138-159`):
   - Reads `migrations/post/grants.sql`.
   - Validates header: `-- grants-policy-version: 2`.
   - Compares SHA-256 against `GRANTS_POLICY = GrantsPolicy(version=2, sha256="a0c8d37ed61daff8900b116f992fa791f5072c11862c17c264cbc9251f0f4f79")`.
   - Records grant policy in `public.schema_policy_versions`.
   - Executes SQL.
7. **Postcondition Verification** (`src/db_migrate.py:38-58, 160-162`):
   - `verify_postconditions()` checks:
     - `fc27_app` is NOT superuser (`rolsuper = false`).
     - `fc27_app` is NOT a member of `fc27_owner`.
     - `fc27_app` does NOT have CREATE on schema `public`.
     - `fc27_dispatch` is NOT a member of `fc27_owner`.

---

## 3. Database Roles, Grants & Privilege Separation

### 3.1 Role Model Definition (`src/db_roles.py:13-16`)

| Role Name | Attributes | Description |
|---|---|---|
| `fc27_owner` | `NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT` | Schema owner for all objects in `public`. Cannot login directly. |
| `fc27_migrator` | `LOGIN NOINHERIT` (Member of `fc27_owner`) | Dedicated migration user. Connects, executes `SET ROLE fc27_owner`, applies DDL. |
| `fc27_app` | `LOGIN NOINHERIT` | Runtime application identity (used by `graph-api`). Connects to DB, USAGE on `public`, restricted DML. |
| `fc27_dispatch` | `LOGIN NOINHERIT` | Future worker identity (Phase 3). Connects to DB, USAGE on `public`, read-only to `domain_events`. |

### 3.2 Bootstrap Operations (`src/db_roles.py:40-200`)

1. **`bootstrap_cluster(admin_conninfo, passwords)`**:
   - Creates the 4 roles if they do not exist.
   - Executes `GRANT fc27_owner TO fc27_migrator`.
   - Sets passwords for login roles using `ALTER ROLE <role> PASSWORD <pw>`.
2. **`bootstrap_database(admin_db_conninfo, legacy_owner, confirm)`**:
   - Executes `GRANT CONNECT ON DATABASE <dbname> TO fc27_migrator, fc27_app, fc27_dispatch`.
   - Executes `REVOKE CREATE ON SCHEMA public FROM PUBLIC`.
   - Executes `GRANT USAGE, CREATE ON SCHEMA public TO fc27_owner`.
   - Executes `GRANT USAGE ON SCHEMA public TO fc27_app, fc27_dispatch`.
   - Identifies objects in `public` not owned by `fc27_owner` (tables, sequences, functions) and transfers ownership to `fc27_owner` when `confirm=True`.
3. **`check_runtime_posture(conn)` (`src/db_roles.py:60-78` and `src/db_migrate.py:60-75`)**:
   - Asserts caller is not superuser, not owner member, and has no `CREATE` on `public`.
   - Used by `src/graph_api.py:148-154` during lifespan startup.

### 3.3 Post-Migration Grants Analysis (`migrations/post/grants.sql`)

- `graph_threads`: `SELECT, INSERT, UPDATE` to `fc27_app`.
- `approval_nonces`: `SELECT, INSERT, UPDATE` to `fc27_app`.
- `run_sequences`: `SELECT, INSERT, UPDATE` to `fc27_app`.
- `approval_audit`: `SELECT, INSERT` to `fc27_app` (triggers block UPDATE/DELETE).
- `schema_migrations`: `SELECT` to `fc27_app`.
- `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations`: `SELECT, INSERT, UPDATE, DELETE` to `fc27_app`.
- `domain_events`: `SELECT, INSERT` to `fc27_app`; `SELECT` to `fc27_dispatch`.
- `outbox`: `INSERT` to `fc27_app` (Notice: `fc27_app` cannot SELECT, UPDATE, or DELETE `outbox`!).
- Sequences: `USAGE` on `approval_audit_id_seq`, `domain_events_event_id_seq`, `outbox_outbox_id_seq` to `fc27_app`.

---

## 4. Docker Compose & Configuration Handling

### 4.1 Docker Compose Services (`docker-compose.yml`)

1. **`postgres` service (`docker-compose.yml:37-52`)**:
   - Base image: `postgres:16.15-alpine@sha256:...`.
   - Environment: `POSTGRES_DB: fc27`, `POSTGRES_USER: fc27`, `POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}`.
   - Network: `fc27_data` only (internal network, no host port exposed).
   - Healthcheck: `pg_isready -U fc27 -d fc27`.
2. **`graph-api` service (`docker-compose.yml:148-199`)**:
   - Port: internal 8001, published to `127.0.0.1:${GRAPH_HOST_PORT:-8001}:8001`.
   - Networks: `fc27_net`, `fc27_data`.
   - **Configuration Problem**:
     ```yaml
     POSTGRES_URI: postgresql://fc27:${POSTGRES_PASSWORD:?Set POSTGRES_PASSWORD in .env}@postgres:5432/fc27
     ```
     `graph-api` is passed the credentials for `fc27` (the postgres container superuser/owner), NOT `fc27_app`!
3. **Network Isolation**:
   - `fc27_data` has `internal: true` (`docker-compose.yml:15-17`). Only `postgres` and `graph-api` are attached. Tested by `tests/test_compose_isolation.py:56-60`.

### 4.2 Configuration Variables & Environment Audit

| Environment Variable | Canonical Requirement | Current Repository Status & Locations | Gap / Mismatch |
|---|---|---|---|
| `POSTGRES_URI` | App runtime URI connecting as `fc27_app` | Used in `graph_api.py:54`, `db_migrate.py:169`, `docker-compose.yml:160`. | In `docker-compose.yml`, configured as superuser `fc27` rather than `fc27_app`. |
| `POSTGRES_ADMIN_URI` | Cluster admin URI for bootstrap | Used in `src/db_roles.py:211` for role bootstrap CLI. | Not in `docker-compose.yml` or `config.example.env`. |
| `FC27_MIGRATOR_PASSWORD`, `FC27_APP_PASSWORD`, `FC27_DISPATCH_PASSWORD` | Passwords for created roles | Read via `settings.read_secret` in `src/db_roles.py:212-214`. | Missing from `config.example.env` and `docker-compose.yml`. |
| `FC27_MCP_URL` | Canonical MCP proxy URL | `http://fc27-mcp:8000` in `docker-compose.yml:25`, `src/graph.py:83`, `src/live_match_watcher.py:78`, `src/post_match_rundown.py:61`. | Consistent. |
| `GRAPH_URL` | Canonical persistent graph-api URL | `http://graph-api:8001` in `docker-compose.yml:26`, `src/live_match_watcher.py:80`, `src/voice_gateway.py:9`. | **Mismatch**: `scripts/commissioner_decide.py:38`, `tests/test_commissioner_cli.py:19`, and `docs/APPROVAL_SECURITY.md:32` use `FC27_GRAPH_URL`. |
| `DISPATCH_ENABLED` | Must be `false` at all times | Required by Section 1.1, 6, 11.4. | **Missing**: Not defined in `src/settings.py`, `src/graph_api.py`, `docker-compose.yml`, or `config.example.env`. |
| `GRAPH_RUN_MIGRATIONS` | Whether graph-api runs migrations on startup | `src/graph_api.py:59`, defaults to `False`. | If set to `true`, runtime app role fails because migrations require `fc27_owner` DDL. Migrations must run via migration identity. |

---

## 5. Database Test Suite & Harnesses

### 5.1 Test Harness Architecture (`tests/conftest.py`)

- **Server Requirement**: Real PostgreSQL 16 server required via `TEST_POSTGRES_URI` (or `scripts/test_postgres.sh`).
- **Session-scoped cluster bootstrap**: `pg_roles` fixture (`tests/conftest.py:107-109`) executes `bootstrap_cluster(pg_admin_uri, ...)`.
- **Per-test DB isolation**: `pg_db` fixture (`tests/conftest.py:111-139`) generates database `fc27_test_<uuid>`, runs `bootstrap_database(uri, legacy_owner=None, confirm=True)`, yields typed connection objects `DbUris(admin, migrator, app, dispatch)`, and drops database on teardown.
- **Migrated DB fixture**: `pg_db_migrated` (`tests/conftest.py:144-146`) applies migrations via `migrate(pg_db.migrator)`.
- **Connection injection**: `pg_uri` fixture yields `pg_db_migrated.app` (the `fc27_app` connection).

### 5.2 Existing Database Test Inventory

1. **`tests/test_db_roles.py`**:
   - `test_bootstrap_is_idempotent`: repeated `bootstrap_cluster` calls succeed.
   - `test_role_attributes`: verifies `fc27_owner` has `NOLOGIN`, `fc27_app` and `fc27_dispatch` are not superusers and not members of `fc27_owner`, `fc27_migrator` is member of `fc27_owner`.
   - `test_public_cannot_create_in_public_schema`: verifies `fc27_app` and `fc27_dispatch` lack `CREATE` on public schema.
   - `test_bootstrap_transfers_legacy_objects_owned_by_admin`: confirms ownership transfer of legacy tables/functions to `fc27_owner`.
   - `test_check_runtime_posture`: confirms posture checking detects superuser, owner membership, or public create.
   - `test_redact_conninfo`: confirms credentials redacted in logs/output.
   - `test_bootstrap_cli_never_prints_password`: CLI error reporting password redaction.
2. **`tests/test_db_migrate_roles.py`**:
   - `test_objects_owned_by_fc27_owner`: asserts all public tables, views, sequences, functions owned by `fc27_owner`.
   - `test_migrate_refuses_without_membership`: `fc27_app` cannot run migrations.
   - `test_app_cannot_run_ddl`: `fc27_app` gets `InsufficientPrivilege` on `CREATE TABLE`.
   - `test_checkpoint_setup_requires_owner`: `fc27_app` cannot execute `PostgresSaver.setup()`.
   - `test_app_can_use_checkpointer`: `fc27_app` can perform checkpoint reads/writes.
   - `test_tamper_check_still_applies`: modifying an applied migration file raises error.
   - `test_concurrent_migrate_is_idempotent`: multiple threads running `migrate()` safely serialize.
   - `test_migrate_fails_on_bad_postcondition`: fails if public create privilege is granted.
   - `test_graph_api_refuses_owner_member_or_create` & `test_graph_api_refuses_privileged_role`: verifies `graph-api` fails startup if connecting with privileged roles.
   - `test_grants_policy_constant_matches_file` & `test_modified_grants_without_bump_refused`: verifies `grants.sql` integrity and versioning.
3. **`tests/test_approval_security.py`**:
   - `test_run_rejects_existing_thread`: duplicate run requests rejected with 409.
   - `test_terminal_thread_cannot_be_reset` & `test_database_trigger_blocks_terminal_rewrite`: DB trigger `fc27_guard_thread_transition` blocks mutating or resetting terminal threads.
   - `test_approver_identity_comes_from_credential`: approver identity verified from credential.
   - `test_nonce_is_single_use`, `test_nonce_is_thread_bound`, `test_nonce_is_candidate_bound`, `test_nonce_expiration_is_enforced`, `test_stale_candidate_package_cannot_be_approved`: nonce challenge integrity.
   - `test_concurrent_approve_has_exactly_one_winner`, `test_audit_and_decision_commit_together`, `test_failed_audit_rolls_back_decision`: transaction atomicity.
4. **`tests/test_events_outbox.py`**:
   - `test_record_event_writes_one_outbox_row`: atomic write of domain event and outbox row.
   - `test_domain_events_append_only` & `test_approval_audit_append_only`: DB triggers block UPDATE and DELETE.
   - `test_sourced_event_requires_existing_source`: DB trigger validates foreign source ID and source table.
   - `test_app_cannot_forge_dispatch_events`: app role blocked from inserting transition-only event types.
5. **`tests/test_compose_isolation.py`**:
   - Verifies only `graph-api` receives approval secrets and `POSTGRES_URI`.
   - Verifies only `graph-api` and `postgres` are attached to `fc27_data`.
   - Verifies loopback binding for exposed host ports and image pinning.

---

## 6. Gap Analysis: Existing Code vs Phase 1 Requirements

### 6.1 Agent A Gaps: Authoritative Data Model & Migrations

| Requirement | Reference | Existing Code State | Identified Gap |
|---|---|---|---|
| **`graph_runs`** | Section 3.1 | `graph_threads` table (`migrations/0001`, `0002`) with `thread_id`, `state`, `status`, `club_id`, `request_hash`, `package_version`, `created_at`, `approved_by`, `approved_at`, `decision_payload`. | Table name is `graph_threads` rather than `graph_runs`. Need to ensure all semantics (unique run/thread ID, correlation, durable status, safe failure info) match Phase 1 requirements, or provide a view/table reconciliation without breaking existing code. |
| **`recommendation_packages`** | Section 3.2 | Recommendation packages are generated as dictionaries in `src/lead_packages.py` and stored inside `graph_threads.state -> 'lead_packages'` (JSONB). `package_version` is stored as text. | **Missing relational table**: No `recommendation_packages` table exists. Section 3.2 requires immutable package identity/version relationship, thread/run correlation, club correlation, package type, canonical structured payload, deterministic payload hash, validation state, creation metadata, explicit supersession/version relationship, and prevention of silent overwrites. |
| **`approval_requests`** | Section 3.3 | Approval requests are handled via `approval_nonces` table (`nonce`, `thread_id`, `payload_sha256`, `package_version`, `issued_at`, `expires_at`, `consumed_at`, `created_by`). `state["approval_request"]` is a dict. | **Missing relational table**: No `approval_requests` table exists. Note that `migrations/0003_dispatch_foundation.sql:86` already references `approval_requests` in `fc27_check_event_source()`! The table needs to bind exact package ID, version, hash, thread/run, required authority, lifecycle status, expiry/invalidation. |
| **`approval_decisions`** | Section 3.4 | Decisions are stored in `approval_audit` (`id`, `thread_id`, `commissioner`, `decision`, `notes`, `owner`, `selection`, `candidate_id`, `player_id`, `approved_by`, `nonce`, `package_version`, `signature_sha256`). | 1. Table name is `approval_audit` rather than `approval_decisions` (or needs reconciliation).<br>2. Missing explicit binding to `approval_request_id`, `package_id`, and `package_hash`.<br>3. **Idempotency failure**: `approval_audit_one_decision_per_thread` unique constraint causes duplicate identical decision submissions to error with 409 Conflict instead of returning safe idempotent success. |
| **Domain Events Lifecycle Integration** | Section 3.5 | `domain_events` table exists in `0003_dispatch_foundation.sql` and `record_event()` in `src/events.py`. | **Never invoked in runtime**: Neither `ApprovalStore` nor `graph.py` nor `graph_api.py` calls `record_event()`. Authoritative events for package created, approval request created, approval decision committed, graph resume requested, terminal state reached, and failures are not written. |
| **Outbox Boundary** | Section 3.6 | `outbox` table exists in `0003_dispatch_foundation.sql`. `record_event()` inserts an outbox row. | Outbox writes only occur when `record_event()` is called (currently only in tests). When domain events are wired into the lifecycle, transactional atomicity between domain events, outbox, and authoritative decisions must be preserved without starting a dispatch relay. |

### 6.2 Agent D Gaps: Compose, Database Roles, & Configuration

| Requirement | Reference | Existing Code State | Identified Gap |
|---|---|---|---|
| **Persistent `graph-api` Service Role** | Section 6, 11.4 | `docker-compose.yml:148-199` runs `graph-api`, but sets `POSTGRES_URI` to `postgresql://fc27:...@postgres:5432/fc27` (superuser `fc27`). | `graph-api` must run under `fc27_app`, not as database superuser/owner. |
| **Migration & Bootstrap Execution in Compose** | Section 6, 11.4 | `docker-compose.yml` has no migration runner service. `graph-api` defaults `GRAPH_RUN_MIGRATIONS=False`. | Need an automated or documented bootstrap/migration path in Compose using `fc27_migrator`, completely separated from `graph-api` runtime. |
| **Checkpointer DDL Privileges** | Section 6, 7.5 | `PostgresSaver.setup()` requires DDL (owner privileges). In `src/graph_api.py:60, 164`, `setup` is controlled by `GRAPH_CHECKPOINT_SETUP` (default False). | Must ensure `graph-api` runtime NEVER calls checkpointer setup, and checkpointer tables are guaranteed to be created by the migration path (`src/db_migrate.py:131-136`). |
| **Enforce `DISPATCH_ENABLED=false`** | Section 1.1, 6, 11.4 | Not defined anywhere in runtime configuration. | Must be defined in `src/settings.py`, validated as `false` at startup, present in `docker-compose.yml` and `config.example.env`, and verified by readiness checks (`/health/ready`). Startup/readiness must fail if `DISPATCH_ENABLED != false`. |
| **Standardize Configuration Names** | Section 6, 11.4 | `GRAPH_URL` in `docker-compose.yml:26`, `live_match_watcher.py:80`, `voice_gateway.py:9`; `FC27_GRAPH_URL` in `commissioner_decide.py:38`, `test_commissioner_cli.py:19`, `docs/APPROVAL_SECURITY.md:32`. | Standardize on canonical `GRAPH_URL` (with fallback to `FC27_GRAPH_URL` where required for CLI backwards compatibility). |
| **Readiness & Liveness Probes** | Section 5.6, 5.7, 11.3 | Currently `graph_api.py:184-191` only has `GET /health` which returns `{"status": "ok", "postgres": "ok"}`. | Missing `GET /health/live` (process liveness only) and `GET /health/ready` (checks DB connectivity, checkpointer readiness, role posture check, `DISPATCH_ENABLED=false`). |
| **Live Watcher Target Endpoint** | Section 6, 11.4 | `src/live_match_watcher.py:308` posts to `{GRAPH_URL}/run`. | Requirement 6 & 11.4 states existing callers must use versioned endpoint `POST /v1/runs`. |

---

## 7. Recommendations for Implementation Phase

Based on this survey, the implementation work for Phase 1 can be cleanly partitioned:

1. **For Agent A (Data Model & Migrations)**:
   - Create migration `0004_phase1_authoritative_foundation.sql`:
     - Create `recommendation_packages` table with deterministic hash (`payload_sha256`), canonical payload, version, supersession linkage, and immutability triggers.
     - Create `approval_requests` table binding exact `package_id`, `package_version`, `package_hash`, `thread_id`, and lifecycle status.
     - Reconcile or introduce `approval_decisions` table (or align `approval_audit`) binding exact `approval_request_id`, `package_id`, `package_version`, `package_hash`, and add idempotency handling for identical replays.
     - Update `migrations/post/grants.sql` and update `GRANTS_POLICY` version (`version=3`) and sha256 in `src/db_migrate.py`.
   - Update `src/events.py` and `src/approval_store.py` to record authoritative domain events and outbox entries for:
     - `package.created`
     - `approval.created`
     - `approval.decided`
     - `graph.resumed` / terminal state transitions.
2. **For Agent D (Compose, DB Roles, & Configuration)**:
   - Update `src/settings.py` to add `DISPATCH_ENABLED` (strictly required, fails if not `false`).
   - Standardize `GRAPH_URL` across `scripts/commissioner_decide.py` and documentation.
   - Update `docker-compose.yml`:
     - Set `DISPATCH_ENABLED=false`.
     - Configure `POSTGRES_URI` for `graph-api` using `fc27_app`.
     - Add bootstrap/migration execution handling or entrypoint for `fc27_migrator`.
     - Update `config.example.env` with `DISPATCH_ENABLED=false`, `FC27_APP_PASSWORD`, `FC27_MIGRATOR_PASSWORD`, etc.
   - Update `src/graph_api.py`:
     - Implement `GET /health/live` and `GET /health/ready` (failing if `DISPATCH_ENABLED != false` or DB posture invalid).
     - Update `src/live_match_watcher.py` to target `/v1/runs`.
