# Phase 1: Dispatch Data Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the PostgreSQL foundation for approved-recommendation dispatch. It covers:
- least-privilege DB roles;
- the authoritative approval lifecycle, including expiry;
- the dispatch schema and its hardened transition function;
- domain events and the outbox;
- atomic approval-to-dispatch creation behind `DISPATCH_ENABLED=false`.

It adds no UI, identity or worker code.

**Architecture:**
- One forward-only migration, `0003_dispatch_foundation.sql`, runs as the NOLOGIN owner `fc27_owner` from a one-shot `db-migrate` job.
- graph-api runs as the DML-only role `fc27_app`. It keeps sole ownership of the approval transaction and, when gates allow, creates dispatches inside that transaction.
- Every dispatch status change goes through the SECURITY DEFINER function `public.fc27_dispatch_transition`.

**Tech stack:**
- Python 3.11, FastAPI, psycopg 3, pydantic 2, pytest
- PostgreSQL 16, LangGraph 1.2.12 with `langgraph-checkpoint-postgres` 3.1.2
- Docker Compose

**Spec:** `docs/superpowers/specs/2026-10-02-player-dispatch-design.md` (v2.1). Executors read the spec sections cited by each task.

## Global Constraints

**Scope**
- Every audit and event row carries `fc_version = '27'` and `platform = 'common-gen5'`. CHECK constraints enforce this on `dispatches` and `players`.
- Phase 1 MUST NOT add these paths (AC-58):
  - `src/dashboard/`
  - `src/identity/`
  - `src/dispatch/worker.py`
  - any WebAuthn, personalization, outbox-relay or tracking code
- `DISPATCH_ENABLED` defaults to `false` everywhere. The only reader is `src/graph_api.py`, at startup (spec §7.5).

**Database roles and privileges**
- Role names are exactly `fc27_owner` (NOLOGIN), `fc27_migrator`, `fc27_app` and `fc27_dispatch`.
- No runtime role may own objects, belong to `fc27_owner`, be a superuser or hold `CREATE` on schema `public` (§14.0).

**SQL rules**
- SQL is parameterized only. In SQL functions, use no dynamic SQL (no `EXECUTE`, no `format()` of identifiers) and schema-qualify every relation (`public.x`).
- Every SECURITY DEFINER function has `SET search_path = pg_catalog, pg_temp`, `REVOKE ALL … FROM PUBLIC`, and an explicit `GRANT EXECUTE` to named roles only.
- Use DB time (`now()`) for every lifecycle comparison. Never compare against Python clock values.
- **Global lock order** (spec §7.6): open `approval_nonces` rows for the thread (ordered by `nonce`), then `approval_requests`, then `graph_threads`, then `dispatches` (only inside the definer function).
- New graph-api routes in Phase 1 live under `/v1/` (`/v1/config/dispatch`, `/v1/activation-gates/{gate_id}`). Existing routes (`/run`, `/threads/...`) are unchanged.
- Python types: `approval_request_id` and `dispatch_id` are `uuid.UUID` in Python. Convert them to `str` only inside `Jsonb` payloads and in `source_id`.
- `src.dispatch.transition.transition()` must be called inside an open transaction (`conn.transaction()`), because `set_config(..., true)` is transaction-local. It raises `RuntimeError` otherwise.
- `COMMISSIONER_ID` must match `^[A-Za-z0-9._-]{1,64}$`. graph-api validates this at startup (`ConfigError`).
- Owner approval needed for scope: Phase 1 also adds `/v1/config/dispatch`, `/v1/activation-gates/{gate_id}` and `X-Correlation-Id` handling, beyond the ten AM-15 items. They support activation gates (AM-5) and audit correlation (AM-8). Remove them if the owner declines.

**Defaults and fixed copy**
- Approval request TTL default: 72 hours (`APPROVAL_REQUEST_TTL_HOURS`). Nonce TTL stays capped at 300 s (`MAX_APPROVAL_TTL_SECONDS`).
- Disabled-dispatch copy, verbatim: `Dispatch is disabled. This approval records the selection only; nothing was delivered to any player.`
- The Redis wake-up message format is `{"event_id": <int>}`. Phase 1 writes outbox rows only and publishes nothing.

**Tests**
- Tests run against real PostgreSQL (`TEST_POSTGRES_URI`, trust auth).
- Run them with `python -m pytest -o addopts="" -q -rs`. The existing suite must stay green at every commit.

**Commits**
- Use Conventional Commits.
- End every commit message with:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01CCvif9mP4DdAPQ8kvTRrpT
  ```

## Review Focus

1. **Legacy deployments where the image superuser *is* the bootstrap superuser** (`POSTGRES_USER=fc27`). `REASSIGN OWNED BY fc27` fails for that role, so ownership must move object by object. Test: `test_bootstrap_transfers_legacy_objects_owned_by_admin` (Task 1).
2. **Expiry racing a decision at the deadline.** Exactly one outcome must win, with no deadlock. The lock order is always nonce(s) → approval request → thread. Test: `test_expiry_and_decision_race_has_single_outcome` (Task 4).
3. **Player IDs that look numeric but differ** (`"007"` vs `"7"`), and integers in candidate JSON. There is no normalization: the comparison is exact string equality, and anything else is `not_dispatchable`. Test: `test_player_id_is_compared_exactly` (Task 8).
4. **Two `db-migrate` runs at once**, or a re-run on an already-migrated v2.1 database while graph-api is serving. The run must be idempotent under the advisory lock, and grants must re-apply. Test: `test_concurrent_migrate_is_idempotent` (Task 2).
5. **A malformed `X-Correlation-Id` header** (not a UUID, or 500 characters long). It returns 422 and is never stored. Test: `test_malformed_correlation_id_rejected` (Task 5).

---

## Prerequisites (before Task 1)

1. Merge the spec branch `docs/player-dispatch-spec` into `main` through a PR. It contains spec commits only.
2. Create a worktree and branch: `feat/phase1-dispatch-foundation` from the updated `main` (superpowers:using-git-worktrees).
3. Start test PostgreSQL with `eval "$(scripts/test_postgres.sh)"`, or use an existing `TEST_POSTGRES_URI`.
4. Confirm the baseline: `python -m pytest -o addopts="" -q -rs` passes.
5. Move the `client` fixture (now in `tests/test_approval_security.py`) and the `wired` fixture (now in `tests/test_langgraph_flow.py`) into `tests/conftest.py` unchanged, so the new test files can use them. Commit with `test: share client and wired fixtures`.

## File map

| Path | Status | Responsibility |
|---|---|---|
| `src/db_roles.py` | create | Role bootstrap (cluster and database parts), legacy ownership transfer, runtime posture check |
| `src/db_migrate.py` | modify | Role-aware migration as `fc27_owner`, `target=` support, LangGraph checkpoint setup, `migrations/post/grants.sql`, post-conditions |
| `migrations/0003_dispatch_foundation.sql` | create (grows across Tasks 3, 6, 7, 8) | All Phase 1 schema |
| `migrations/post/grants.sql` | create (grows with tables) | Idempotent grants, re-applied on every migrate |
| `src/events.py` | create | `record_event()`: inserts `domain_events` + `outbox` in the caller's transaction |
| `src/approval_store.py` | modify | Approval-request lifecycle, expiry, correlation and proof fields, dispatch creation hook |
| `src/decision.py` | modify | Remove the legacy `state["dispatch"]` dictionary |
| `src/graph.py` | modify | `from_conninfo(setup=False)`; resume only decided threads; approval node accepts `revision_requested` |
| `src/graph_api.py` | modify | New settings, posture check, expiry task, response `dispatch` block, legacy record, correlation header, HMAC kill switch, health, `/config/dispatch`, `/activation-gates/{gate_id}` |
| `src/schemas.py` | modify | `Objective` model; optional `Candidate.objective` |
| `src/dispatch/__init__.py`, `src/dispatch/objective.py`, `src/dispatch/creation.py`, `src/dispatch/transition.py`, `src/dispatch/gates.py` | create | Objective bounds and dispatchability; creation inside the approval transaction; Python wrapper for the SQL transition; activation-gate evaluation |
| `tests/conftest.py` | modify | Role bootstrap; `pg_uri` yields `fc27_app`; new fixtures |
| `tests/helpers.py` | modify | Settings fields; numeric-ID package fixtures with objectives |
| `tests/test_db_roles.py`, `tests/test_db_migrate_roles.py`, `tests/test_events_outbox.py`, `tests/test_migration_0003.py`, `tests/test_approval_lifecycle.py`, `tests/test_dispatch_schema.py`, `tests/test_dispatch_transition.py`, `tests/test_dispatch_creation.py`, `tests/test_phase1_scope.py` | create | Per-task tests |
| `tests/test_approval_security.py`, `tests/test_langgraph_flow.py`, `tests/test_compose_isolation.py` | modify | Adapt to least privilege and AM-4 (named in tasks) |
| `docker-compose.yml`, `config.example.env` | modify | `db-migrate`, `db-bootstrap` profile, `fc27_app` URI, flags |
| `docs/DISPATCH_ACTIVATION.md`, `docs/EA_PLAYER_IDENTITY_VERIFICATION.md` | create | Disabled state, gates, identity-evidence template |
| `MIGRATION_GUIDE.md`, `docs/APPROVAL_SECURITY.md`, `README.md`, `TEST_RESULTS.md` | modify | Upgrade steps, HMAC rotation and kill switch, status, AC trace |

---

### Task 1: Database role bootstrap and least-privilege test harness

Spec: §14.0, §25 step 2, AM-1.

**Files:**
- Create: `src/db_roles.py`, `tests/test_db_roles.py`
- Modify: `tests/conftest.py`

**Interfaces:**
- Produces:
  - `ROLE_OWNER = "fc27_owner"`, `ROLE_MIGRATOR = "fc27_migrator"`, `ROLE_APP = "fc27_app"`, `ROLE_DISPATCH = "fc27_dispatch"`.
  - `bootstrap_cluster(admin_conninfo: str, passwords: Mapping[str, str | None]) -> None`. Idempotent. Creates the roles:

    | Role | Attributes |
    |---|---|
    | owner | `NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT` |
    | migrator | `LOGIN NOINHERIT` |
    | app | `LOGIN NOINHERIT` |
    | dispatch | `LOGIN NOINHERIT` |

    It then runs `GRANT fc27_owner TO fc27_migrator`, and sets passwords that are not `None` with `sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(r), sql.Literal(pw))`.
  - `bootstrap_database(admin_db_conninfo: str, *, legacy_owner: str | None, confirm: bool) -> BootstrapReport`. Runs:
    - `GRANT CONNECT ON DATABASE` to the three login roles;
    - `REVOKE CREATE ON SCHEMA public FROM PUBLIC`;
    - `GRANT USAGE, CREATE ON SCHEMA public TO fc27_owner`;
    - `GRANT USAGE ON SCHEMA public TO fc27_app, fc27_dispatch`;
    - an ownership transfer, described in Step 3.
  - `@dataclass BootstrapReport(pending_transfers: list[str], transferred: list[str])`.
  - `check_runtime_posture(conn) -> list[str]`. Returns the violations for `current_user`: `"superuser"`, `"owner_member"` or `"create_on_public"`. An empty list means OK.
  - CLI: `python -m src.db_roles bootstrap --database NAME [--legacy-owner ROLE] [--confirm]`.
    - It reads `POSTGRES_ADMIN_URI` (or `_FILE`) and `FC27_MIGRATOR_PASSWORD`, `FC27_APP_PASSWORD`, `FC27_DISPATCH_PASSWORD` (or `_FILE`), all through `settings.read_secret`.
    - Exit codes: 0 when done; 3 when transfers are pending without `--confirm`; 2 for a configuration error.
- Test fixtures produced in `tests/conftest.py`:
  - `pg_roles` (session): runs `bootstrap_cluster(ADMIN_URI, {all: None})`.
  - `pg_db` (function): creates the DB, then `bootstrap_database(…, legacy_owner=None, confirm=True)`, and yields `DbUris(admin, migrator, app, dispatch)`. Each is the same DB with a different `user`.
  - `pg_uri`: kept for now as the **admin** URI with migrations applied. Task 2 switches it to `fc27_app`.

- [ ] **Step 1: Write the failing tests** in `tests/test_db_roles.py`:

```python
def test_bootstrap_is_idempotent(pg_admin_uri): ...          # call bootstrap_cluster twice; no error
def test_role_attributes(pg_db):
    # owner: rolcanlogin false; app/dispatch: not superuser, not member of fc27_owner;
    # migrator: pg_has_role('fc27_migrator','fc27_owner','MEMBER') is true
def test_public_cannot_create_in_public_schema(pg_db):
    # has_schema_privilege('fc27_app','public','CREATE') is false; same for 'fc27_dispatch'
def test_bootstrap_transfers_legacy_objects_owned_by_admin(pg_db):
    # as admin: CREATE TABLE legacy_t(...); CREATE FUNCTION legacy_f() ...
    # bootstrap_database(confirm=False).pending_transfers == ['function public.legacy_f()', 'table public.legacy_t']
    # after confirm=True: pg_class/pg_proc owner of both is fc27_owner
def test_check_runtime_posture(pg_db):
    # as admin: ['superuser']; as fc27_app: []
    # after admin GRANT fc27_owner TO fc27_app: ['owner_member']; after GRANT CREATE ON SCHEMA public TO fc27_app: ['create_on_public']
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest -o addopts="" -q tests/test_db_roles.py`
Expected: FAIL with `ModuleNotFoundError: src.db_roles`.

- [ ] **Step 3: Implement `src/db_roles.py` and the fixtures**

**Ownership transfer.**
- Enumerate the objects in schema `public` whose owner is not `fc27_owner` and that are not extension members (`pg_depend.deptype = 'e'`):
  - `pg_class` with `relkind` in `r`, `v`, `m`, `p`;
  - `relkind = 'S'` only when the sequence is not owned by a table;
  - `pg_proc` functions.
- Optionally restrict the list to objects owned by `legacy_owner`.
- Issue `ALTER TABLE|VIEW|MATERIALIZED VIEW|SEQUENCE|FUNCTION … OWNER TO fc27_owner` per object.
- Do **not** use `REASSIGN OWNED`, because it fails for the bootstrap superuser (Review Focus 1).
- Return the sorted descriptions in the form `'<kind> public.<name>'`.

- [ ] **Step 4: Run them to verify they pass**

Run: `python -m pytest -o addopts="" -q tests/test_db_roles.py && python -m pytest -o addopts="" -q`
Expected: all PASS. The existing suite is unchanged because `pg_uri` is still admin.

- [ ] **Step 5: Commit**: `feat(db): add role bootstrap for fc27_owner/migrator/app/dispatch`

---

### Task 2: Role-aware migration runner, checkpoint setup, and app runs as `fc27_app`

Spec: §14.0, §25 step 3, A-9. **A-9 is verified:** `PostgresSaver.setup()` needs `CREATE` on `public` (even `CREATE TABLE IF NOT EXISTS` fails without it), while runtime use needs DML only. Setup therefore runs in the migrator.

**Files:**
- Modify: `src/db_migrate.py`, `src/graph.py:217-228`, `src/graph_api.py`, `tests/conftest.py`, `tests/test_approval_security.py:276-287`
- Create: `migrations/post/grants.sql`, `tests/test_db_migrate_roles.py`

**Interfaces:**
- Consumes: Task 1 roles and fixtures.
- Produces:
  - `migrate(conninfo: str, directory: Path = MIGRATIONS_DIR, *, target: str | None = None, checkpoint_setup: bool = True) -> list[str]`.
    - It requires the role `fc27_owner` to exist and `pg_has_role(current_user, 'fc27_owner', 'MEMBER')`. Otherwise it raises `MigrationError("run python -m src.db_roles bootstrap first")`.
    - It runs `SET ROLE fc27_owner` for the session, so `schema_migrations` and all objects are owner-owned.
    - It applies versions up to and including `target`.
    - It then runs `PostgresSaver.setup()` on a separate autocommit `dict_row` connection, also after `SET ROLE fc27_owner`. This happens **while the main connection still holds the advisory lock**, so concurrent runs cannot race on `CREATE TABLE IF NOT EXISTS`.
    - It then executes `migrations/post/grants.sql`, then `verify_postconditions()`.
  - `class MigrationError(RuntimeError)`.
  - `verify_postconditions(conn) -> list[str]`. In this task it checks `fc27_app` and `fc27_dispatch` posture through `check_runtime_posture`-equivalent catalog queries for those roles. Task 7 extends it with checks on the transition function. Checks on objects that do not exist yet are skipped.
  - `main()` returns 1 and prints the violations when `MigrationError` is raised.
  - `GraphRunner.from_conninfo(conninfo, deps, max_pool=10, *, setup: bool = False)`.
  - New `ApiSettings` fields:

    | Field | Default | Env var |
    |---|---|---|
    | `run_migrations` | **`False`** | `GRAPH_RUN_MIGRATIONS` |
    | `checkpoint_setup` | `False` | `GRAPH_CHECKPOINT_SETUP` |
    | `enforce_db_posture` | `True` | — |

  - Fixture changes:
    - `pg_db_migrated` = `pg_db` plus `migrate(migrator)`, yielding `DbUris`.
    - `pg_uri` now yields the **`fc27_app`** URI of a DB migrated as `fc27_migrator`.
    - New fixtures: `pg_admin_db_uri` (superuser on the same DB), `pg_migrator_uri` and `pg_dispatch_uri`.

- [ ] **Step 1: Write the failing tests** in `tests/test_db_migrate_roles.py`:

```python
def test_objects_owned_by_fc27_owner(pg_db_migrated):     # every public relation + function owner == 'fc27_owner'
def test_migrate_refuses_without_membership(pg_db):      # migrate(app_uri) raises MigrationError
def test_app_cannot_run_ddl(pg_uri):                     # CREATE TABLE x() -> psycopg.errors.InsufficientPrivilege
def test_checkpoint_setup_requires_owner(pg_uri):        # PostgresSaver(conn).setup() as fc27_app -> InsufficientPrivilege
def test_app_can_use_checkpointer(pg_uri):               # GraphRunner.from_conninfo(pg_uri, deps) put/get works (no setup)
def test_tamper_check_still_applies(pg_db_migrated):     # edit a copied migration dir file -> RuntimeError "modified after"
def test_concurrent_migrate_is_idempotent(pg_db_migrated):  # 4 threads migrate(migrator_uri) -> all return [], no error
def test_migrate_fails_on_bad_postcondition(pg_db_migrated):  # AC-57: admin GRANT CREATE ON SCHEMA public TO fc27_app; migrate(migrator) -> MigrationError naming 'create_on_public'
def test_db_migrate_cli_exit_code(pg_db_migrated):           # AC-57: subprocess `python -m src.db_migrate` with that violation -> returncode != 0
def test_graph_api_refuses_owner_member_or_create(pg_db_migrated)  # AC-57: GRANT fc27_owner TO fc27_app -> ConfigError 'owner_member'; GRANT CREATE ... -> 'create_on_public'
def test_graph_api_refuses_privileged_role(pg_admin_db_uri):
    # create_app(settings(pg_admin_db_uri)) under TestClient -> raises ConfigError containing "superuser"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest -o addopts="" -q tests/test_db_migrate_roles.py`
Expected: FAIL. `pg_db_migrated` is undefined and `migrate` has no `target`.

- [ ] **Step 3: Implement**
- **`src/db_migrate.py`:** make the changes listed under Interfaces.
- **`migrations/post/grants.sql`:** wrap every statement in a `DO` block guarded by `to_regclass('public.<name>') IS NOT NULL` (or the `pg_proc` equivalent for functions), so a `target="0002"` run succeeds.
  - grant `fc27_app` `SELECT, INSERT, UPDATE` on `graph_threads`, `approval_nonces` and `run_sequences`, and `SELECT, INSERT` on `approval_audit`;
  - grant `USAGE` on their sequences;
  - grant `SELECT` on `schema_migrations`;
  - grant `SELECT, INSERT, UPDATE, DELETE` on `checkpoints`, `checkpoint_blobs`, `checkpoint_writes` and `checkpoint_migrations`.

  Write every statement so it can be re-applied.
- **`src/graph_api.py` lifespan:** when `enforce_db_posture`, open a connection and raise `ConfigError(f"database role posture violation: {violations}")` if `check_runtime_posture` returns anything. Run `GraphRunner.from_conninfo(..., setup=settings.checkpoint_setup)`.
- **Fault-injection test:** change `tests/test_approval_security.py::test_failed_audit_rolls_back_decision` to create its function and trigger through `pg_admin_db_uri`. `fc27_app` correctly cannot.

- [ ] **Step 4: Run them to verify they pass**

Run: `python -m pytest -o addopts="" -q`
Expected: all PASS, with the whole existing suite now running as `fc27_app`.

Also add `tests/helpers.py: legacy_install(admin_db_uri)`. It reproduces a pre-v2.1 database: as the admin user it applies `0001` and `0002` with the old runner semantics (it creates `schema_migrations` and inserts their checksums) and runs `PostgresSaver.setup()`. Everything is admin-owned. Task 4 uses it.

- [ ] **Step 5: Commit**: `feat(db): migrate as fc27_owner; graph-api runs as fc27_app`

---

### Task 3: Migration 0003, part A: event index, outbox, append-only infrastructure

Spec: §14.5, §15 items 1–2, §22.1, AM-3.

**Files:**
- Create: `migrations/0003_dispatch_foundation.sql` (section A), `src/events.py`, `tests/test_events_outbox.py`
- Modify: `migrations/post/grants.sql`

**Interfaces:**
- Consumes: `migrate(..., target=)` from Task 2.
- Produces these SQL objects:
  - `public.fc27_reject_mutation()`: an append-only trigger function. It raises `check_violation` with the text `'<TG_TABLE_NAME> is append-only'`.
  - `public.domain_events` and `public.outbox`: the exact columns in spec §14.5, with these details:
    - `outbox.channel` defaults to `'fc27:events'`;
    - `outbox.domain_event_id` is `UNIQUE`;
    - `domain_events` is append-only.
  - `public.fc27_check_event_source()`: a BEFORE INSERT trigger on `domain_events`.
    - It rejects a row whose `event_type` is in the required-source list but has no source, or names the wrong `source_table`.
    - It checks that the source row exists, using one static `IF … ELSIF` branch per table.
    - This task adds the `approval_audit` branch only. Tasks 4, 6, 7 and 8 extend the function with `CREATE OR REPLACE` later in the same file.
    - The source row must **belong to the entity**. For example, for `approval.decided` (with `entity_type='approval_request'`), `approval_audit.approval_request_id = entity_id`. The approval-request branch arrives in Task 4.
    - Event types `dispatch.*` (except `dispatch.created`), `plan.*` and `tracking.*` are rejected unless `current_user = 'fc27_owner'`, meaning they were inserted from the definer function.
    - The required-source list (complete; constant in the function):
      - `approval.decided` → `approval_audit`;
      - `approval.created` and `approval.expired` → `approval_requests`;
      - `dispatch.created` → `dispatches`;
      - `dispatch.acknowledged` → `dispatch_acknowledgments`;
      - `tracking.updated` and `tracking.completed` → `dispatch_assessments`.
  - The `approval_audit` append-only trigger.
  - Grants: `fc27_app` gets `SELECT, INSERT` on `domain_events`, `INSERT` on `outbox`, and `USAGE` on both sequences. `fc27_dispatch` gets `SELECT` on `domain_events` only.
- Produces this Python helper:
  - `src/events.py: record_event(conn, *, event_type: str, entity_type: str, entity_id: str, actor: str, correlation_id: UUID, visibility: Literal['staff','player_owner','audit_only','ops'], scope_club_id: str | None, scope_player_id: str | None = None, entity_version: int | None = None, from_status: str | None = None, to_status: str | None = None, reason: str | None = None, source_table: str | None = None, source_id: str | None = None) -> int`.
  - It inserts one event and one outbox row in the caller's transaction and returns `event_id`.
  - Only the `domain_events` insert uses `RETURNING`. `fc27_app` has `INSERT` but no `SELECT` on `outbox`, so the outbox insert must not use `RETURNING`.

SQL file header comment: `-- Phase 1. This file grows across Tasks 3-8 and must not be applied to a deployed database before the Phase 1 PR merges.`

- [ ] **Step 1: Write the failing tests** in `tests/test_events_outbox.py`:

```python
def test_record_event_writes_one_outbox_row(pg_uri, pg_admin_db_uri)  # count/inspect outbox via admin: channel 'fc27:events', published_at NULL, attempts 0
def test_event_scope_tags_default(pg_uri)                # fc_version '27', platform 'common-gen5'
def test_domain_events_append_only(pg_uri)               # UPDATE / DELETE -> CheckViolation 'append-only'
def test_approval_audit_append_only(pg_uri, client)      # decide once; UPDATE approval_audit -> CheckViolation
def test_sourced_event_requires_existing_source(pg_uri)  # AC-42 (data): approval.decided with source_id '999999' -> raises;
                                                         # with source_table='dispatches' -> raises (wrong table); with no source -> raises
def test_unsourced_event_types_allowed(pg_uri)           # 'system.degraded' with no source -> ok
def test_app_cannot_forge_dispatch_events(pg_uri)        # record_event('dispatch.delivered', ...) as fc27_app -> raises
def test_worker_cannot_insert_events(pg_dispatch_uri)    # INSERT domain_events as fc27_dispatch -> InsufficientPrivilege
def test_source_pairing_check(pg_uri)                    # source_table without source_id -> CheckViolation
def test_app_cannot_read_or_update_outbox(pg_uri)        # SELECT/UPDATE outbox as fc27_app -> InsufficientPrivilege
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest -o addopts="" -q tests/test_events_outbox.py`
Expected: FAIL, because `src.events` is missing.

- [ ] **Step 3: Write section A of the migration, the grants and `src/events.py`**

- [ ] **Step 4: Run them to verify they pass**

Run: `python -m pytest -o addopts="" -q`
Expected: all PASS.

- [ ] **Step 5: Commit**: `feat(db): 0003 part A - domain_events index, outbox, append-only audit`

---

### Task 4: Authoritative approval lifecycle (schema and store), including expiry

Spec: §7.2, §7.4, §7.6, §14.2, §18.6 items 2–4, §25 step 5, AM-4, AM-8, AM-14.

The schema and the store change ship together. The pair-consistency trigger makes the old `complete_run` fail at commit, so neither half is green alone.

**Files:**
- Modify: `migrations/0003_dispatch_foundation.sql` (append section B), `migrations/post/grants.sql`, `src/approval_store.py`, `src/decision.py`, `tests/test_approval_security.py` (the test that updates `package_version`, around lines 205–216)
- Create: `tests/test_approval_lifecycle.py`, `tests/test_migration_0003.py`

**Interfaces:**
- Consumes: `record_event` (Task 3).
- Produces these SQL objects:
  - `public.approval_requests` (§14.2), with these triggers:
    - `thread_id`, `package_version`, `created_at` and `expires_at` are immutable;
    - terminal rows are frozen.
  - `graph_threads` changes:
    - `approval_request_id uuid UNIQUE REFERENCES public.approval_requests`;
    - status CHECK extended with `revision_requested` and `expired`;
    - terminal-fields CHECK replaced as in §14.2.
  - `fc27_guard_thread_transition()` replaced:
    - terminal set = `approved`, `rejected`, `revision_requested`, `expired`;
    - allowed edges `awaiting_approval → {approved, rejected, revision_requested, expired}`;
    - `package_version` immutable once the status is `awaiting_approval`, with the message `package_version is immutable`;
    - `approval_request_id` set at most once.
  - `public.fc27_check_approval_pair()` with one `CONSTRAINT TRIGGER … DEFERRABLE INITIALLY DEFERRED` on each table. It checks:
    - the same status;
    - the same `package_version`;
    - `approval_requests.thread_id` equals the linked thread;
    - an `awaiting_approval` thread that has a `package_version` must be linked.

    The error message starts with `approval pair:`.
  - `public.fc27_approval_inconsistencies(kind, thread_id, approval_request_id)`: a view. `kind` is one of `status_mismatch`, `package_mismatch`, `unlinked_request`, `missing_link` or `legacy_unlinked`.
  - `approval_audit` gains `proof_type`, `actor_user_id`, `webauthn_credential_id`, `correlation_id` and `approval_request_id` (§14.2), and its decision CHECK adds `REQUEST_REVISION`.
  - `approval_nonces` gains `revoked_at`, `subject_type` (default `'thread'`), `dispatch_id` and `user_id`, with the exactly-one-subject CHECK.
  - The `fc27_check_event_source()` branch for `approval_requests`, plus the entity-ownership check for `approval_audit`.
  - Grants: `fc27_app` gets `SELECT` on the view `fc27_approval_inconsistencies`.
  - Backfill (§25 step 5).
  - Grants: `fc27_app` gets `SELECT, INSERT, UPDATE` on `approval_requests`.
- Produces this Python:

  **`ApprovalStore.__init__(self, conninfo: str, *, request_ttl: timedelta = timedelta(hours=72))`**
  - `request_ttl` must be greater than 0.

  **`complete_run`** (signature unchanged)
  - For `awaiting_approval` with a version, one transaction:
    1. Insert the request with `expires_at = now() + request_ttl`.
    2. Update the thread with the link.
    3. `record_event('approval.created', visibility='staff', source_table='approval_requests')`.
  - Returns the state plus `approval_request_id` and `approval_expires_at` (ISO).

  **`issue_challenge`** (signature unchanged)
  - Calls `expire_due(thread_id=…)` first.
  - Locks the request `FOR SHARE`, then the thread.
  - Returns 409 `approval_expired` when the request is not awaiting or is past due.
  - The nonce `expires_at` is `least(now()+ttl, request.expires_at)`.

  **`apply_decision(thread_id, signed, principal, signing_secret, *, correlation_id: UUID | None = None, dispatch_enabled: bool = False, dispatch_creator: Callable[..., UUID] | None = None) -> dict`**
  - Lock order: nonce → request (`FOR UPDATE`) → thread.
  - A revoked nonce, a request that is not awaiting, or a request past due returns 409 `approval_expired`.
  - Sets the request's `status` and `decided_at` and increments its `version`.
  - Inserts `approval_audit` with `proof_type='hmac'`, `correlation_id` and `approval_request_id`, `RETURNING id`.
  - Calls `record_event('approval.decided', actor=f"commissioner:{principal.name}", source_table='approval_audit', source_id=str(audit_id), visibility='staff')`.
  - When `dispatch_enabled` and the decision is `APPROVE`, calls `dispatch_creator(conn, …)` (Task 8). `NotDispatchable` becomes `StoreError(409, "not_dispatchable: …")`.
  - Returns the state plus `approval_audit_id`, `approval_request_id` and `dispatch_id` (or `None`).

  **`expire_due(*, thread_id: str | None = None, limit: int = 100) -> list[str]`**
  - Handles each due request in its own transaction:
    1. Lock the thread's open nonces.
    2. Lock the request and re-check that it is due.
    3. Lock the thread.
    4. Set both records to `expired` and set `expired_at`.
    5. Revoke the nonces.
    6. Record `approval.expired`.

  **`terminal_thread_ids`**
  - Selects `approved`, `rejected` and `revision_requested` only.

  **`pending_approvals(limit: int = 50) -> list[dict]`**
  - The inbox query: `approval_requests r JOIN graph_threads g USING (thread_id) WHERE r.status = 'awaiting_approval' AND g.status = 'awaiting_approval' AND r.expires_at > now()`, ordered by `created_at`.

  **Event calls.** All `record_event` calls are keyword-only:
  - `entity_type='approval_request'`, `entity_id=str(approval_request_id)`, `scope_club_id=<thread club_id>`.
  - `complete_run` and `expire_due` generate their own `correlation_id = uuid4()`. `apply_decision` uses the caller's ID or generates one.
  - `apply_decision` selects `club_id` with the thread row and passes the thread mapping to `dispatch_creator`.

  **Check order in `apply_decision`:**
  1. nonce exists and is bound to this thread;
  2. nonce `revoked_at`;
  3. request status and `expires_at` (`approval_expired`);
  4. `fc27_approval_inconsistencies` contains the thread → 409 `inconsistent_state`;
  5. nonce consumed;
  6. nonce expiry;
  7. payload match;
  8. thread state.

  **`decision.apply_decision_to_state`**
  - Never writes a `dispatch` key.

- [ ] **Step 1: Write the failing tests**

In `tests/test_migration_0003.py`:

```python
def test_0003_applies_on_legacy_0002_data(pg_db)     # AC-56: legacy_install(admin) (admin-owned 0001/0002 + checkpoints);
    # bootstrap_database(confirm=True) transfers ownership; as admin insert awaiting thread WITH
    # package_version, awaiting WITHOUT, approved thread+audit+consumed nonce, rejected thread, unconsumed nonce; migrate(migrator) applies only 0003;
    # exactly one approval_requests row (eligible thread) and it is linked; terminal rows byte-identical;
    # fc27_approval_inconsistencies contains only 'legacy_unlinked'
def test_pair_mismatch_fails_at_commit(pg_uri, client)   # AC-55: in one txn change only graph_threads.status -> commit raises 'approval pair:'; reverse also
def test_awaiting_thread_requires_link(pg_uri)
def test_package_version_immutable_while_awaiting(pg_uri, client)
```

In `tests/test_approval_lifecycle.py`:

```python
def test_complete_run_creates_linked_request(client, pg_uri)
def test_decision_updates_request_and_thread_together(client, pg_uri)   # proof_type 'hmac', correlation_id set, approval.decided source = audit id
def test_approve_writes_no_dispatch_key(client, pg_uri)                 # AC-44
def test_expired_request_blocks_prior_nonce(pg_uri)                     # AC-43: request_ttl=timedelta(seconds=1); challenge; sleep 1.2;
    # apply_decision -> 409 approval_expired; both records 'expired'; nonce revoked
def test_expired_request_blocks_new_challenge(pg_uri)
def test_expire_due_is_idempotent(pg_uri)
def test_expiry_and_decision_race_has_single_outcome(pg_uri)            # Review Focus 2: 4x expire_due + 4x apply_decision at the deadline;
    # final status in {'approved','expired'}; one audit row iff approved; completes within 10 s (no deadlock)
def test_inbox_query_never_shows_terminal_threads(pg_uri, client)       # AC-55: uses ApprovalStore.pending_approvals()
@pytest.mark.parametrize("table", ["approval_audit", "domain_events", "outbox"])
def test_failed_decision_rolls_back_everything(pg_uri, pg_admin_db_uri, table)  # admin adds a BEFORE INSERT failing trigger on `table`;
    # request and thread still awaiting; 0 audit rows; no new events; nonce unconsumed (AC-1 fault injection)
def test_pending_approvals_matches_inbox_rule(pg_uri, client)
```

In `tests/conftest.py`, make the `pg_uri` fixture teardown assert that `SELECT count(*) FROM fc27_approval_inconsistencies WHERE kind <> 'legacy_unlinked'` is 0. Tests that create mismatches deliberately mark themselves with `@pytest.mark.allow_inconsistency`, and the fixture skips the check for them (AC-55 "empty after the suite").

In `tests/test_approval_security.py`, rewrite the stale-package test. It now asserts that a direct `UPDATE graph_threads SET package_version=…` on an awaiting thread raises `CheckViolation` with `package_version is immutable`.

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest -o addopts="" -q tests/test_migration_0003.py tests/test_approval_lifecycle.py`
Expected: FAIL.

- [ ] **Step 3: Implement section B and the store changes**

- [ ] **Step 4: Run them to verify they pass**

Run: `python -m pytest -o addopts="" -q`
Expected: all PASS, including the existing approval and LangGraph suites.

- [ ] **Step 5: Commit**: `feat(approval): authoritative approval_requests lifecycle with expiry`

---

### Task 5: graph-api and graph integration: expiry, resume refusal, flag-disabled response, correlation, HMAC kill switch, health

Spec: §7.4 item 4, §7.5, §7.6, §18.6, §22.7, AM-4, AM-5, AM-8, AM-14.

**Files:**
- Modify: `src/graph.py`, `src/graph_api.py`, `tests/helpers.py`, `tests/test_langgraph_flow.py:107-126`
- Test: `tests/test_approval_lifecycle.py` (append)

**Interfaces:**
- Consumes: Task 4 store APIs.
- Produces:

  **`ApiSettings` new fields and env vars**

  | Field | Default | Env var |
  |---|---|---|
  | `dispatch_enabled: bool` | `False` | `DISPATCH_ENABLED` |
  | `approval_request_ttl: timedelta` | 72 h | `APPROVAL_REQUEST_TTL_HOURS` (int, 1–720) |
  | `cli_hmac_enabled: bool` | `True` | `APPROVAL_CLI_HMAC_ENABLED` |
  | `expiry_interval_seconds: int` | `60` | — |

  When `cli_hmac_enabled` is false, `signing_secret` may be `None`.

  **App state**
  - `app.state.dispatch_effective: bool`. Phase 1 sets it in lifespan from Task 8's `evaluate_gates`; until Task 8 it is `False`. The 60 s background loop re-evaluates it (spec §7.5).
  - `app.state.gate_report`.

  **Background task.** A lifespan `asyncio` task runs every `expiry_interval_seconds` through `run_in_threadpool`. It calls `store.expire_due()` and, from Task 8, re-evaluates the gates. It is cancelled on shutdown.

  **Correlation header.** `X-Correlation-Id` is optional. A value that is not a UUID gives 422 `invalid correlation id`. The ID is generated server-side when absent and echoed back in a response header.

  **Decision response** = the state plus:

  ```json
  "dispatch": {"enabled": false, "created": false,
               "message": "Dispatch is disabled. This approval records the selection only; nothing was delivered to any player."}
  ```

  When effective and created: `{"enabled": true, "created": true, "dispatch_id": "<uuid>"}`.

  **`GET /threads/{id}`.** A stored legacy `dispatch` key is returned as `legacy_selection_record` with `"note": "recorded selection only; never delivered"`. The `dispatch` key is removed from the response.

  **HMAC kill switch.** With `cli_hmac_enabled=False`, `POST /threads/{id}/decision` returns 403 `proof_type_disabled` and `/decision-challenge` returns 403 too.

  **Inconsistent state.**
  - Decisions on a thread listed in `fc27_approval_inconsistencies` (`kind != 'legacy_unlinked'`) return 409 `inconsistent_state`. This is checked in `issue_challenge` and again in `apply_decision` (Task 4 check order).
  - `/health` adds `approval_consistency` (a count), `dispatch` (`disabled`, `blocked (<gate>)` or `enabled`) and `db_role_posture` (`ok`).

  **`GET /v1/config/dispatch`** (service or commissioner) → `{"configured": bool, "effective": bool, "gates": [...], "fc_version": "27", "platform": "common-gen5"}`.

  **Graph changes**
  - `GraphRunner.__init__` stores `deps.decision_reader`.
  - `GraphRunner.resume(thread_id)` returns `values` **without invoking** unless `decision_reader(thread_id)["status"] in DECIDED_STATUSES`.
  - `DECIDED_STATUSES = ("approved", "rejected", "revision_requested")` lives in `src/graph.py`.
  - The approval node accepts `DECIDED_STATUSES` and still raises `ApprovalNotCommitted` for anything else.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_approval_lifecycle.py`):

```python
def test_decision_response_states_dispatch_disabled(client)          # exact message string
def test_legacy_dispatch_key_presented_as_selection_record(client, pg_admin_db_uri)
    # admin inserts a decided thread whose state has 'dispatch': {...}; GET /threads -> 'legacy_selection_record', no 'dispatch'
def test_malformed_correlation_id_rejected(client)                    # 'not-a-uuid' and 'a'*500 -> 422; nothing stored
def test_correlation_id_recorded_on_audit(client, pg_uri)             # given uuid appears in approval_audit + domain_events
def test_hmac_kill_switch(pg_uri)                                     # cli_hmac_enabled=False, signing_secret=None -> 403 proof_type_disabled
def test_from_env_without_secret_when_hmac_disabled(monkeypatch)        # AC-49: APPROVAL_CLI_HMAC_ENABLED=false and no APPROVAL_SIGNING_SECRET -> ApiSettings.from_env() succeeds; =true without it -> ConfigError
def test_invalid_commissioner_id_rejected(monkeypatch)                 # COMMISSIONER_ID='bad id!' -> ConfigError
def test_expired_thread_not_resumed(wired, pg_uri)                    # AC-43: expire; runner.resume(tid) returns, graph_next == ['approval'];
    # direct Command(resume=…) raises ApprovalNotCommitted; checkpoint history still has the interrupt; startup reconciliation skips it
def test_dispatch_flag_cannot_be_set_by_request(client)               # POST decision with extra field 'dispatch_enabled': true -> 422 (extra=forbid);
    # header 'X-Dispatch-Enabled: true' ignored; response dispatch.enabled false
def test_health_reports_consistency_and_dispatch(client)              # approval_consistency==0, dispatch=='disabled'
def test_inconsistent_thread_blocks_decision(client, pg_admin_db_uri) # admin disables pair trigger ON THAT TEST DB ONLY,
    # creates mismatch, re-enables; challenge -> 409 inconsistent_state
```

**Test changes.**
- Change `tests/test_langgraph_flow.py::test_langgraph_interrupt_cannot_be_bypassed`: `runner.resume(tid)` no longer raises. Assert that it returns and that `graph_next == ["approval"]` (AM-4 behaviour change). Keep the `pytest.raises(ApprovalNotCommitted)` on the direct `invoke`.
- `tests/helpers.settings()` gains `dispatch_enabled=False` and `cli_hmac_enabled=True`.

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest -o addopts="" -q tests/test_approval_lifecycle.py tests/test_langgraph_flow.py`
Expected: the new tests FAIL.

- [ ] **Step 3: Implement the interfaces above in `src/graph_api.py` and `src/graph.py`**

- [ ] **Step 4: Run them to verify they pass**

Run: `python -m pytest -o addopts="" -q`
Expected: all PASS.

- [ ] **Step 5: Commit**: `feat(api): expiry task, resume refusal, disabled-dispatch response, HMAC kill switch`

---

### Task 6: Migration 0003, part C: dispatch schema and objective model

Spec: §11.4, §12.1, §13.1, §14.3, §14.4, §14.7, AM-6.

**Files:**
- Modify: `migrations/0003_dispatch_foundation.sql` (append section C), `migrations/post/grants.sql`, `src/schemas.py`, `src/roles.py`, `src/lead_packages.py`
- Create: `src/dispatch/__init__.py`, `src/dispatch/objective.py`, `tests/test_dispatch_schema.py`

**Interfaces:**
- Produces these SQL objects:
  - `public.players` (§14.3).
  - `public.dispatches` (§14.3), plus `club_id text NOT NULL CHECK (club_id ~ '^[0-9]+$')`.
    - `superseded_by` is a **deferrable initially deferred** FK.
    - The partial unique index `dispatches_one_active_per_objective` is on `(player_id, owner, (approved_instruction->'objective'->>'metric'))` where the status is not terminal.
    - Triggers:
      - `fc27_dispatch_insert_guard`: status `pending`, version 1, `plan_version` 0, all lifecycle timestamps NULL.
      - `fc27_dispatch_immutable`: provenance columns (§11.4) and `club_id` never change; terminal rows are frozen.
  - `public.dispatch_transition_rules`: table only; seeded in Task 7.
  - The other §14.3 and §14.4 tables, with the `dispatch_plans.status` CHECK = `valid`, `material_change`, `invalid`, `schema_invalid`, and the assessment results CHECK = the six values in spec §14.4.
  - `approval_nonces`: `thread_id` and `package_version` become NULLable, with the subject CHECK from spec §14.2 (subject types include `activation_gate`). Append-only triggers on `dispatch_plans`, `dispatch_plan_decisions`, `dispatch_delivery_attempts`, `dispatch_acknowledgments`, `dispatch_comments`, `analyst_notes`, `player_observations` and `dispatch_assessments`.
  - `fc27_check_event_source()` extended with branches for `dispatches`, `dispatch_acknowledgments`, `dispatch_assessments`, `dispatch_plans`, `dispatch_plan_decisions` and `dispatch_delivery_attempts`.
  - Grants per spec §14.7. In particular, `fc27_dispatch` gets **no** `INSERT`, `UPDATE` or `DELETE` on `dispatches`, and `fc27_app` gets `SELECT, INSERT` on `dispatches` (no `UPDATE`).
- Produces this Python model:
  - `src/schemas.py: class Objective(BaseModel)` with `extra="forbid"`:
    - `metric: Literal["pass_accuracy","rating","goals","assists","tackle_success","shot_accuracy"]`
    - `direction: Literal["increase","decrease"]`
    - `target_delta: float`
    - `min_matches: int`
    - `max_matches: int`
    - Validators: `1 <= min_matches <= max_matches <= 10`, and `target_delta` within `METRIC_BOUNDS[metric]`.
  - `Candidate.objective: Objective | None = None`.
  - `src/roles.py`: role-output assessments gain an optional `objective: Objective | None`.
  - `src/lead_packages.py`: copies the owner role's objective for that player onto the candidate when present. This is a data-path change only: prompts are untouched, because prompting for objectives is Phase 3.
  - `src/dispatch/objective.py: METRIC_BOUNDS: dict[str, tuple[float, float]]`. These **proposed values are recorded in spec §12.1 by this plan's spec fix-up; reviewer to confirm**:

    | Metric | Range |
    |---|---|
    | `pass_accuracy` | (1, 25) |
    | `rating` | (0.1, 2.0) |
    | `goals` | (1, 5) |
    | `assists` | (1, 5) |
    | `tackle_success` | (1, 25) |
    | `shot_accuracy` | (1, 25) |

    Plus `PHYSICAL_RANGE` from spec §19.1.

- [ ] **Step 1: Write the failing tests** in `tests/test_dispatch_schema.py`:

```python
def test_objective_bounds():                                   # valid sample; target_delta 26 for pass_accuracy -> ValidationError;
                                                               # min_matches 0 / max_matches 11 / min>max -> ValidationError; extra key -> error
def test_candidate_objective_optional():                       # existing fake_packages still validate
def test_lead_packages_copy_owner_objective():                 # owner role assessment objective appears on the candidate; absent -> None
def test_players_reject_non_numeric_id(pg_admin_db_uri)        # 'gamer_tag' -> CheckViolation
def test_dispatch_insert_must_be_pending_v1(pg_uri)            # fc27_app insert with status 'queued' -> raises
@pytest.mark.parametrize("column", ["approved_instruction","player_id","candidate_id","owner","approval_id","approval_payload_hash","source_thread_id","club_id"])
def test_dispatch_provenance_immutable(pg_admin_db_uri, column)  # AC-2: even superuser UPDATE raises (trigger)
def test_dispatch_worker_cannot_write_dispatches(pg_dispatch_uri)  # AC-39: INSERT/UPDATE/DELETE -> InsufficientPrivilege
def test_dispatch_worker_cannot_touch_approval_tables(pg_dispatch_uri)  # SELECT/UPDATE approval_audit, approval_requests, graph_threads -> InsufficientPrivilege
def test_app_cannot_update_dispatches(pg_uri)                  # AC-39
def test_append_only_dispatch_tables(pg_admin_db_uri)          # parametrized over the 8 tables
def test_one_active_dispatch_per_player_owner_metric(pg_uri)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest -o addopts="" -q tests/test_dispatch_schema.py`
Expected: FAIL.

- [ ] **Step 3: Implement section C, the grants and the models**

- [ ] **Step 4: Run them to verify they pass**

Run: `python -m pytest -o addopts="" -q`
Expected: all PASS.

- [ ] **Step 5: Commit**: `feat(db): 0003 part C - dispatch schema, objective model, worker grants`

---

### Task 7: Hardened `fc27_dispatch_transition`

Spec: §11 (all), AM-1, AM-11 (SQL part), AM-12 (derivation).

**Files:**
- Modify: `migrations/0003_dispatch_foundation.sql` (append section D), `migrations/post/grants.sql`, `src/db_migrate.py` (`verify_postconditions`)
- Create: `src/dispatch/transition.py`, `tests/test_dispatch_transition.py`

**Interfaces:**
- Produces this SQL function and data:

  **Function.** `public.fc27_dispatch_transition(p_dispatch_id uuid, p_expected_status text, p_expected_version integer, p_to_status text, p_reason text, p_correlation_id uuid, p_patch jsonb DEFAULT '{}'::jsonb) RETURNS integer`.
  - `LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp`.
  - `OWNER TO fc27_owner`.
  - `REVOKE ALL ON FUNCTION … FROM PUBLIC`, then `GRANT EXECUTE` to `fc27_app` and `fc27_dispatch`.

  **Seed of `public.dispatch_transition_rules`.** Exactly these rows:

  | from_status | to_status | db_role | actor_kind |
  |---|---|---|---|
  | pending | queued | fc27_dispatch | service |
  | queued | delivered | fc27_dispatch | service |
  | queued | requires_reapproval | fc27_dispatch | service |
  | queued | failed | fc27_dispatch | service |
  | delivered | expired | fc27_dispatch | service |
  | acknowledged | tracking | fc27_dispatch | service |
  | tracking | completed | fc27_dispatch | service |
  | tracking | expired | fc27_dispatch | service |
  | requires_reapproval | delivered | fc27_app | commissioner |
  | requires_reapproval | cancelled | fc27_app | commissioner |
  | failed | queued | fc27_app | commissioner |
  | failed | queued | fc27_app | user |
  | delivered | acknowledged | fc27_app | user |
  | each of {pending, queued, delivered, acknowledged, tracking, requires_reapproval, failed} | cancelled | fc27_app | commissioner |
  | each of {pending, queued, delivered, acknowledged, tracking, requires_reapproval, failed} | superseded | fc27_app | commissioner |

  `requires_reapproval → cancelled` appears twice (once in its own row, once in the "each of" row); seed it once.

  **SQLSTATEs**, mapped by graph-api to 409 or 422:

  | SQLSTATE | Meaning |
  |---|---|
  | `FC001` | version or status conflict |
  | `FC002` | edge not allowed for role and actor |
  | `FC003` | patch key or type invalid |
  | `FC004` | actor invalid |
  | `FC005` | NULL correlation ID |
  | `FC006` | precondition row missing (ack, assessment, plan) |
  | `FC007` | reason too long |

  **Actor derivation**

  | `session_user` | Behaviour |
  |---|---|
  | `fc27_dispatch` | The actor is `service:dispatch-worker`. A non-empty `current_setting('fc27.actor', true)` that differs raises `FC004`. |
  | `fc27_app` | The setting must match `^(service:graph-api\|commissioner:[A-Za-z0-9._-]{1,64}\|user:[0-9a-f-]{36})$`. A `user:` actor raises `FC004` (`user actors require phase 2`). Actor kind: `service` → `service`, `commissioner:` → `commissioner`. |
  | any other role | `insufficient_privilege` |

  **Validation order and commissioner proof:** exactly as spec §11.3 (actor, correlation, reason, lock and version, edge, commissioner proof, patch, preconditions). In Phase 1 the only commissioner edge that can pass is `→ superseded`. It requires an `approval_audit` row with `xmin = pg_catalog.pg_current_xact_id()::xid` and `approved_by = <actor id>`. Every other commissioner edge raises `FC004`.

  **Patch allowlist and set-by-function columns:** exactly as in spec §11.3. The `failed` reason-to-retryable map:

  | `failure_reason` | `failure_retryable` |
  |---|---|
  | `personalization_unavailable` | true |
  | `plan_schema_invalid` | true |
  | `plan_policy_invalid` | false |
  | `player_not_enrolled` | true |
  | `player_identity_unverified` | false |
  | `delivery_store_error` | true |

  **`superseded_by` rule.** It must be a uuid that does **not** yet exist in `dispatches`. The deferred FK guarantees the dispatch exists at commit. Task 8 inserts the new dispatch right after superseding.

  **Event mapping.** Event type and visibility per `to_status`:

  | to_status | Event | Visibility |
  |---|---|---|
  | queued | `dispatch.queued` | player_owner |
  | delivered | `dispatch.delivered` (source `dispatch_plans`, `'<dispatch_id>:<plan_version>'`) | player_owner |
  | requires_reapproval | `plan.requires_reapproval` (source `dispatch_plans`) | staff |
  | failed | `dispatch.failed` | staff |
  | acknowledged | `dispatch.acknowledged` (source `dispatch_acknowledgments`) | player_owner |
  | tracking | `tracking.started` | player_owner |
  | completed | `tracking.completed` (source the final assessment) | player_owner |
  | expired | `dispatch.expired` | player_owner |
  | cancelled | `dispatch.cancelled` | player_owner |
  | superseded | `dispatch.superseded` | staff |

  Every event carries `scope_player_id = dispatch.player_id` and `scope_club_id = dispatch.club_id`, and gets one outbox row.

- Produces this Python wrapper:
  - `src/dispatch/transition.py: transition(conn, *, dispatch_id: UUID, expected_status: str, expected_version: int, to_status: str, reason: str | None, correlation_id: UUID, patch: Mapping[str, Any] | None = None, actor: str | None = None) -> int`.
  - When `actor` is given, it runs `SELECT set_config('fc27.actor', %s, true)` first. It raises `RuntimeError` when `conn.info.transaction_status` is idle, meaning no transaction is open.
  - Patch values are JSON-serializable: UUIDs are passed as `str`.
  - It maps `FC00x` to `TransitionError(code: str, message: str)`.
- Extends `verify_postconditions()` with: function owner, `prosecdef`, `proconfig` containing `search_path=pg_catalog, pg_temp`, and no PUBLIC in `proacl`.

- [ ] **Step 1: Write the failing tests** in `tests/test_dispatch_transition.py`. Use a fixture `make_dispatch(status=…)` that inserts a `pending` dispatch as `fc27_app`, inside an approval-like transaction, and then walks it with transitions to the requested start state.

```python
def test_function_attributes(pg_uri)                       # AC-38 pg_proc query
def test_function_source_has_no_dynamic_sql()              # AC-38 static: read 0003 SQL between the function's $$…$$;
                                                           # assert no r'\bEXECUTE\b' and no 'format('; every FROM/UPDATE/JOIN target and every
                                                           # 'INSERT INTO <x>' target starts with 'public.' or 'pg_catalog.' (ignore 'SELECT … INTO v_' and 'RETURNING … INTO v_' variable targets)
def test_runtime_roles_cannot_replace_function(pg_uri, pg_dispatch_uri)   # AC-39 CREATE OR REPLACE / ALTER FUNCTION -> InsufficientPrivilege
@pytest.mark.parametrize("from_s,to_s,role,actor", P1_PASSABLE_EDGES)      # AC-4: every Phase 1 passable edge succeeds
def test_allowed_edges(...)                                # start states are seeded by an admin UPDATE of dispatch_status (and preconditions:
                                                           # plan rows, ack rows, final assessment, approval_audit-in-txn for supersede)
@pytest.mark.parametrize("from_s,to_s,role,actor", P1_DEFERRED_EDGES)       # user edges and non-supersede commissioner edges -> FC004 in P1
def test_deferred_edges_rejected_in_phase1(...)
def test_every_other_edge_rejected(...)                    # 11x11 minus allowed, both roles, valid patch supplied per target -> FC002
def test_null_correlation_id_rejected(pg_dispatch_uri)     # FC005
def test_unknown_patch_key_rejected(pg_dispatch_uri)       # {'dispatch_status': 'completed'} -> FC003
def test_wrong_patch_type_rejected(pg_dispatch_uri)        # {'plan_version': 'x'} -> FC003
def test_worker_cannot_set_foreign_actor(pg_dispatch_uri)  # set_config('fc27.actor','commissioner:x') -> FC004
def test_app_malformed_actor_rejected(pg_uri)              # 'root' -> FC004 ; 'user:<uuid>' -> FC004 (phase 2)
def test_worker_actor_recorded(pg_dispatch_uri)            # domain_events.actor == 'service:dispatch-worker'
def test_stale_version_conflict(pg_dispatch_uri)           # FC001
def test_failed_retryable_is_derived(pg_dispatch_uri)      # patch failure_reason=plan_policy_invalid -> failure_retryable false;
                                                           # patch containing failure_retryable -> FC003
def test_acknowledged_edge_rejects_phase1_actors(pg_uri)  # delivered->acknowledged: 'user:' actor -> FC004 (phase 2); commissioner -> FC002
def test_tracking_requires_baseline_patch(pg_dispatch_uri) # AC-52 SQL: missing baseline_metrics -> FC003; with it -> tracking, tracking_started_at set, one event
def test_completed_requires_final_assessment(pg_dispatch_uri)  # FC006 without; with is_final 'improving' -> outcome copied
def test_search_path_hijack_has_no_effect(pg_dispatch_uri) # AC-40: create TEMP table 'dispatches' and TEMP function 'now'
                                                           # in pg_temp as fc27_dispatch; transition still updates public.dispatches correctly
def test_terminal_rows_frozen(pg_dispatch_uri)             # from completed any edge -> FC002
def test_event_and_outbox_written_per_transition(pg_dispatch_uri)
```

The `FC006` precondition for `acknowledged` (a `dispatch_acknowledgments` row with the current `plan_version`) is implemented now and shares its code path with `completed`. Its positive test lands in Phase 2, when `user` actors exist.

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest -o addopts="" -q tests/test_dispatch_transition.py`
Expected: FAIL, because the function is missing.

- [ ] **Step 3: Implement section D and `src/dispatch/transition.py`**

Function body order, exactly as spec §11.3 "Validation":
1. Derive the actor.
2. Check `p_correlation_id IS NOT NULL` (`FC005`).
3. Check the reason length (`FC007`).
4. Check patch keys and types against a `CASE p_to_status` allowlist (`FC003`).
5. `SELECT … FROM public.dispatches WHERE dispatch_id = p_dispatch_id FOR UPDATE`, then check status and version (`FC001`).
6. Check `EXISTS` in `public.dispatch_transition_rules` (`FC002`).
7. Check preconditions (`FC006`).
8. Run one static `UPDATE public.dispatches SET … CASE …`.
9. Insert into `public.domain_events` and `public.outbox`.
10. Return the new version.

- [ ] **Step 4: Run them to verify they pass**

Run: `python -m pytest -o addopts="" -q`
Expected: all PASS.

- [ ] **Step 5: Commit**: `feat(db): hardened SECURITY DEFINER dispatch transition function`

---

### Task 8: Activation gates and atomic approval-to-dispatch creation

Spec: §7.1, §7.2, §7.5, §13.1, §21 (supersede), §31.2, AM-5, AM-6.

**Files:**
- Modify: `migrations/0003_dispatch_foundation.sql` (append section E), `migrations/post/grants.sql`, `src/approval_store.py`, `src/graph_api.py`, `tests/helpers.py`
- Create: `src/dispatch/gates.py`, `src/dispatch/creation.py`, `tests/test_dispatch_creation.py`

**Interfaces:**
- Consumes:
  - `transition()` (Task 7);
  - `apply_decision(..., dispatch_enabled=, dispatch_creator=)` (Task 4);
  - `Objective` and `METRIC_BOUNDS` (Task 6).
- Produces these SQL objects:
  - `public.activation_gate_log(id bigserial, gate_id text CHECK (gate_id IN ('G-ID','G-ATOMIC','G-OUTBOX')), status text CHECK (status IN ('passed','failed')), evidence_ref text NOT NULL CHECK (char_length(evidence_ref) BETWEEN 1 AND 500), recorded_by text NOT NULL, recorded_at timestamptz NOT NULL DEFAULT now())`. It is append-only.
  - The view `public.activation_gates`, showing the latest row per gate.
  - `public.service_heartbeats` (§14.5).
  - The log table includes `signature_sha256 text UNIQUE NOT NULL`.
  - Grants: `fc27_app` gets `SELECT, INSERT` on the log, `SELECT` on the `activation_gates` view and `SELECT` on heartbeats; `fc27_dispatch` gets `SELECT, INSERT, UPDATE` on heartbeats.
- Produces this Python:
  - `src/dispatch/gates.py`:
    - `@dataclass GateResult(gate_id: str, passed: bool, detail: str)`.
    - `evaluate_gates(conn) -> list[GateResult]`, covering G-MIGRATION, G-GRANTS, G-WORKER (heartbeat < 60 s), G-ID, G-ATOMIC and G-OUTBOX, in that order.
    - `dispatch_effective(configured: bool, results: list[GateResult]) -> bool`.
  - `src/dispatch/creation.py`:
    - `class NotDispatchable(Exception)`.
    - `create_dispatch_for_approval(conn, *, thread: Mapping[str, Any], candidate: Mapping[str, Any], intent: DecisionIntent, approval_audit_id: int, approval_request_id: str, payload_sha256: str, principal_name: str, correlation_id: UUID) -> UUID`.
    - It checks dispatchability, all of which must hold:
      1. The candidate has a valid `Objective`.
      2. `candidate["player_id"]` is a `str` matching `^[0-9]{1,20}$`, compared with **exact string equality**.
      3. A `players` row exists that is `active`, has a non-NULL `identity_verified_at`, and has `club_id` equal to the thread's `club_id`.
    - It first runs `SELECT pg_advisory_xact_lock(hashtextextended(player_id || '|' || owner || '|' || metric, 0))`.
    - Then, for each active dispatch with the same `(player_id, owner, objective.metric)`, it calls `transition(..., to_status="superseded", patch={"superseded_by": new_id}, actor=f"commissioner:{principal_name}")`.
    - It inserts `dispatches` (status `pending`) with `approved_instruction = {"candidate": candidate, "objective": objective, "owner", "selection", "package_version", "fc_version": "27", "platform": "common-gen5"}`.
    - It records `dispatch.created` (`source_table='dispatches'`, `visibility='player_owner'`, scoped).
  - `graph_api` changes:
    - The lifespan computes `app.state.gate_report = evaluate_gates(conn)` and `app.state.dispatch_effective = dispatch_effective(settings.dispatch_enabled, report)`.
    - If configured but not effective, it logs `CRITICAL` with the failing gate IDs.
    - The decision route passes `dispatch_enabled=app.state.dispatch_effective, dispatch_creator=create_dispatch_for_approval`.
    - `POST /v1/activation-gates/{gate_id}` (commissioner only):
      - Body `{status: "passed"|"failed", evidence_ref: str, issued_at: int, signature: str}`. The signature is HMAC-SHA256 with `APPROVAL_SIGNING_SECRET` over the canonical JSON `{gate_id, status, evidence_ref, issued_at}`.
      - `issued_at` must be within ±120 s of DB time.
      - `signature_sha256` is unique, so a replay returns 409.
      - With the CLI HMAC disabled, the route returns 403 `proof_type_disabled`; Phase 2 adds WebAuthn.
      - `gate_id` must be G-ID, G-ATOMIC or G-OUTBOX; anything else returns 422 `automatic gate`.
      - It records `recorded_by=f"commissioner:{id}"` and an `activation.gate_recorded` event (`visibility='audit_only'`).
      - It returns the latest gate row and a note that gate changes take effect on restart.
  - `tests/helpers.py`: `fake_packages_dispatchable(player_ids=("1001","1002","1003"))` with an objective `{"metric":"pass_accuracy","direction":"increase","target_delta":5,"min_matches":3,"max_matches":5}`, plus `seed_verified_players(admin_db_uri, ids, club_id="330323")`.

- [ ] **Step 1: Write the failing tests** in `tests/test_dispatch_creation.py`:

```python
def test_gates_block_by_default(pg_uri)                     # all gates evaluated; G-WORKER, G-ID, G-ATOMIC, G-OUTBOX fail; effective False
def test_configured_but_blocked_behaves_disabled(pg_uri)    # AC-45: dispatch_enabled=True settings, no gate records -> no dispatch, health 'blocked (G-WORKER,…)'
def test_gate_recording_requires_commissioner_and_signature(client)  # service token -> 403; G-WORKER -> 422; bad/missing signature -> 403;
    # stale issued_at -> 409; replay -> 409; valid -> 200, 'activation.gate_recorded' audit_only event sourced to activation_gate_log
def test_approve_creates_dispatch_atomically(enabled_client, pg_uri)   # AC-1/AC-2: one pending dispatch; provenance equals
    # signed payload (candidate_id, player_id, owner, selection, approval_id, approval_payload_hash, approval_request_id);
    # events: approval.decided + dispatch.created; outbox rows 2 (+1 approval.created earlier)
def test_dispatch_failure_rolls_back_decision(enabled_client, pg_admin_db_uri)  # injected trigger on dispatches insert -> 500/StoreError;
    # thread+request still awaiting; 0 audit, 0 dispatch, 0 new events; nonce unconsumed
def test_not_dispatchable_without_objective(enabled_client)          # AC-5 409 not_dispatchable; decision rolled back
def test_not_dispatchable_gamertag_player(enabled_client)            # AC-46: candidate player_id 'GamerTag99' -> 409
def test_not_dispatchable_unverified_player(enabled_client, pg_admin_db_uri)  # identity_verified_at NULL -> 409
def test_player_id_is_compared_exactly(enabled_client, pg_admin_db_uri)       # Review Focus 3: players '7', candidate '007' -> 409
def test_not_dispatchable_unknown_player(enabled_client)                      # AC-46: numeric player absent from players -> 409
def test_concurrent_supersede_is_serialized(enabled_client, pg_uri)           # two threads, same player/owner/metric, approved in parallel ->
    # both 200; exactly one active dispatch; the other 'superseded'; no 500
def test_reject_creates_no_dispatch(enabled_client)
def test_supersede_is_atomic(enabled_client, pg_uri)                 # AC-9: second approval same player/owner/metric -> old 'superseded',
    # superseded_by = new id, new 'pending'; exactly one active
def test_parallel_duplicate_approvals_single_dispatch(enabled_client, pg_uri)  # AC-18 (data): 10 parallel -> 1 winner, 1 audit, 1 dispatch
```

The `enabled_client` fixture builds the app with `dispatch_enabled=True`. As admin it inserts `activation_gate_log` rows for G-ID, G-ATOMIC and G-OUTBOX, plus a fresh `service_heartbeats('dispatch-worker')` row. It uses dispatchable packages and seeded verified players.

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest -o addopts="" -q tests/test_dispatch_creation.py`
Expected: FAIL.

- [ ] **Step 3: Implement section E, gates, creation and the API wiring**

- [ ] **Step 4: Run them to verify they pass**

Run: `python -m pytest -o addopts="" -q`
Expected: all PASS.

- [ ] **Step 5: Commit**: `feat(dispatch): activation gates and atomic approval-to-dispatch creation`

---

### Task 9: Compose, configuration, isolation tests and operator documentation

Spec: §7.5, §18.6, §24, §25, §31.2, AM-5, AM-6, AM-8, AM-15.

**Files:**
- Modify: `docker-compose.yml`, `config.example.env`, `tests/test_compose_isolation.py`, `MIGRATION_GUIDE.md`, `docs/APPROVAL_SECURITY.md`, `README.md`
- Create: `docs/DISPATCH_ACTIVATION.md`, `docs/EA_PLAYER_IDENTITY_VERIFICATION.md`, `tests/test_phase1_scope.py`

**Interfaces:**
- **Compose services**

  | Service | Image or build | Command | Environment | Networks | Notes |
  |---|---|---|---|---|---|
  | `db-bootstrap` (profile `bootstrap`) | `Dockerfile.graph` | `python -m src.db_roles bootstrap --database fc27 --legacy-owner fc27` | `POSTGRES_ADMIN_URI=postgresql://fc27:${POSTGRES_PASSWORD}@postgres:5432/fc27`; `FC27_MIGRATOR_PASSWORD`, `FC27_APP_PASSWORD`, `FC27_DISPATCH_PASSWORD` | `fc27_data` | `restart: "no"` |
  | `db-migrate` | `Dockerfile.graph` | `python -m src.db_migrate` | `POSTGRES_URI=postgresql://fc27_migrator:${FC27_MIGRATOR_PASSWORD}@postgres:5432/fc27`, and nothing else | `fc27_data` | `restart: "no"`; `depends_on: postgres healthy` |

- **graph-api changes:**
  - `POSTGRES_URI=postgresql://fc27_app:${FC27_APP_PASSWORD:?…}@postgres:5432/fc27`
  - `GRAPH_RUN_MIGRATIONS: "false"`, `GRAPH_CHECKPOINT_SETUP: "false"`
  - `DISPATCH_ENABLED: "false"`
  - `APPROVAL_REQUEST_TTL_HOURS: ${APPROVAL_REQUEST_TTL_HOURS:-72}`
  - `APPROVAL_CLI_HMAC_ENABLED: ${APPROVAL_CLI_HMAC_ENABLED:-true}`
  - `depends_on.db-migrate.condition: service_completed_successfully`
- **`config.example.env`:** add `FC27_MIGRATOR_PASSWORD`, `FC27_APP_PASSWORD` and `FC27_DISPATCH_PASSWORD`, each `=replace_with_long_random_password`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_compose_isolation.py`:

```python
def test_signing_secret_only_in_graph_api(model, lf_model)     # AC-49 across base, voice, rtsp, langfuse, bootstrap profiles
def test_migrator_credential_only_in_db_migrate(model)         # 'fc27_migrator' appears only in db-migrate's POSTGRES_URI
def test_graph_api_uses_app_role(model)                        # URI user == fc27_app; GRAPH_RUN_MIGRATIONS == 'false'
def test_dispatch_disabled_by_default(model)                   # DISPATCH_ENABLED == 'false'
def test_db_migrate_has_no_other_secrets(model)                # env keys ⊆ {POSTGRES_URI, PYTHONUNBUFFERED, ...common non-secret}
```

Update `APPROVAL_SECRETS` handling so that `POSTGRES_URI` is allowed in `graph-api` and `db-migrate`, and `POSTGRES_ADMIN_URI` only in `db-bootstrap`. Update `test_only_graph_api_can_reach_postgres` so that its allow-list for `fc27_data` is exactly `graph-api`, `db-migrate` and `db-bootstrap` (bootstrap profile). Add `FC27_MIGRATOR_PASSWORD`, `FC27_APP_PASSWORD` and `FC27_DISPATCH_PASSWORD` to `test_missing_secret_fails_closed`.

In `tests/test_phase1_scope.py` (AC-58):

```python
def test_no_phase2plus_code():                                 # not exists: src/dashboard, src/identity, src/dispatch/worker.py;
                                                               # no 'webauthn' import anywhere under src/
def test_docs_state_disabled_and_gates():                      # DISPATCH_ACTIVATION.md lists G-MIGRATION..G-OUTBOX and the exact disabled message
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest -o addopts="" -q tests/test_compose_isolation.py tests/test_phase1_scope.py`
Expected: FAIL.

- [ ] **Step 3: Implement the Compose changes and write the docs**

**`docs/DISPATCH_ACTIVATION.md`** covers:
- what "disabled" means, with the exact message;
- the gate table from spec §31.2;
- how to record a gate against `POST /v1/activation-gates/{gate_id}`, using a short signing snippet built on `src.approval` canonicalization (placeholders `<COMMISSIONER_TOKEN>` and `<APPROVAL_SIGNING_SECRET>`);
- that per-player identity verification has no route in Phase 1; it arrives in Phase 3 (spec §13.1).
- the fact that dispatch cannot be enabled before Phase 3.

**`docs/EA_PLAYER_IDENTITY_VERIFICATION.md`** has `Status: UNVERIFIED (G-ID not met)` and one section for each AC-47 evidence item, each with an empty evidence table. Copy the headings from spec AC-47.

**`README.md` quick start and `MIGRATION_GUIDE.md`** document **fresh installs**: set the passwords, `docker compose up -d postgres`, `docker compose --profile bootstrap run --rm db-bootstrap --confirm`, then `docker compose up -d`. `db-migrate` fails with "run python -m src.db_roles bootstrap first" until the bootstrap has run.

**`MIGRATION_GUIDE.md`** gains an "Upgrading to v2.1 (Phase 1)" section:
1. Back up with `pg_dump -Fc`.
2. Set the three new passwords in `.env`.
3. `docker compose --profile bootstrap run --rm db-bootstrap` (review the transfer list), then re-run with `--confirm`.
4. `docker compose up -d db-migrate graph-api`.
5. Verify `/health` shows `approval_consistency: 0`, `dispatch: disabled` and `db_role_posture: ok`.
6. Rollback means restoring the dump. A pre-v2.1 graph-api fails closed on decisions.

**`docs/APPROVAL_SECURITY.md`** gains the HMAC rotation steps and the `APPROVAL_CLI_HMAC_ENABLED` kill switch.

**`README.md`:** the "Per-player observer agents, automatic player-agent dispatch" row becomes "**Data foundation only (Phase 1).** Dispatch disabled; approvals record the selection only. See `docs/DISPATCH_ACTIVATION.md`."

- [ ] **Step 4: Run them to verify they pass**

Run: `python -m pytest -o addopts="" -q`
Expected: all PASS. The Compose tests need the docker CLI and skip with a reason otherwise. The CI "Compose config" step also passes with the new env vars.

- [ ] **Step 5: Commit**: `chore(compose): db-migrate job, fc27_app runtime role, dispatch disabled; docs`

---

### Task 10: Phase 1 verification and acceptance trace

**Files:**
- Modify: `TEST_RESULTS.md`, `SECURITY_CHANGES.md`

- [ ] **Step 1: Run the full suite twice**, the second time with `-p no:randomly` if the plugin is installed.

Command: `python -m compileall -q src tests && python -m pytest -o addopts="" -q -rs`
Expected: 0 failures. Skips only for docker, Redis or optional extras, each with a reason.

- [ ] **Step 2: Run a live Compose smoke test** on a disposable volume, if Docker is available:
  1. `bootstrap`
  2. `migrate`
  3. graph-api up
  4. `POST /run` with a stub-free configuration (or skip)
  5. challenge, then decide
  6. `GET /health`

  Record the outputs. If Docker is unavailable, say so explicitly in `TEST_RESULTS.md`. Never claim an unrun result.

- [ ] **Step 3: Write the AC trace** in `TEST_RESULTS.md`, under "Phase 1 acceptance trace". Write one row per AC listed for Phase 1 in spec §31.1, with the proving test node ID and its result.

- [ ] **Step 4: Update `SECURITY_CHANGES.md`** with these items:
  - role separation
  - the SECURITY DEFINER hardening
  - approval expiry semantics
  - the HMAC kill switch
  - append-only audit
  - `package_version` immutability
  - the AM-4 resume behaviour change

- [ ] **Step 5: Commit**: `docs: Phase 1 acceptance trace and security changes`

- [ ] **Step 6: Run a whole-branch review** with superpowers:requesting-code-review, then open a PR to `main`. The PR is titled "Phase 1: dispatch data foundation (dispatch disabled)" and its body lists the ACs and the residual risks.

---

## Spec fix-ups made alongside this plan (commit on the spec branch)

While planning, these points were resolved. They are recorded in the spec in the same branch, in a separate commit:

1. **A-9 is verified.** `setup()` needs `CREATE`; graph-api never calls it.
2. **Legacy ownership moves object by object.** Bootstrap uses per-object `ALTER … OWNER`, not `REASSIGN OWNED`.
3. **`dispatches.club_id`** is added for event scoping.
4. **Activation gates are append-only.** `activation_gate_log` plus the `activation_gates` view replace the mutable table, and gates are recorded through a graph-api route (a CLI wrapper comes later).
5. **Staff visibility** includes `player_owner` events.
6. **`superseded_by`** is a deferred FK to a not-yet-existing new dispatch.
7. **Event-type list** adds `dispatch.queued`, `dispatch.expired`, `dispatch.cancelled`, `dispatch.superseded` and `activation.gate_recorded`.
8. **`METRIC_BOUNDS`** are as proposed above.
9. **`package_version` is immutable** while a thread is `awaiting_approval`.
10. **The `user` actor kind** includes `failed → queued`.
