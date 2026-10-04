# Task Assignment: worker_m3_1 (Milestone 3 Implementation)

## Mission
Implement Milestone 3: Dispatch Schema & Single-Write-Path Transition Function, ensuring strict database-level security, immutable provenance, forward-compatible tracking tables, and single-write-path transition enforcement.

## Working Directory
`c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_1`

## Mandatory Reading (Prior to implementation)
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (authoritative user request + Section 1A patch)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`
3. The 3 Milestone 3 Explorer Reports:
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_schema_1\schema_analysis.md`
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_transition_1\transition_analysis.md`
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_tests_1\tests_analysis.md`
4. `docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md` (Tasks 6 & 7)

## MANDATORY INTEGRITY WARNING
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

## Write Ownership
You hold exclusive write ownership of the following files:
- `migrations/0003_dispatch_foundation.sql` (Sections C and D)
- `migrations/post/grants.sql` (grants-policy-version: 4)
- `src/db_migrate.py` (update GRANTS_POLICY version to 4 and sha256 to match grants.sql)
- `src/dispatch/transition.py` (new single-write-path transition service)
- `src/dispatch/objective.py` (new objective bounds policy)
- `src/schemas.py` (Objective schema with bounded validation)
- `tests/conftest.py` (test fixtures: sample_player, sample_approved_dispatch_prereqs, make_dispatch)
- `tests/test_dispatch_schema.py` (18 schema, constraint, trigger, and role boundary tests)
- `tests/test_dispatch_transition.py` (22 state machine, transition rules, and safety tests)

DO NOT modify files outside your write ownership without explicit orchestrator permission.

## Implementation Tasks

### 1. `migrations/0003_dispatch_foundation.sql`
- **Section C: Players, Dispatches & Tracking Schema**:
  - `public.players`: PK `player_id` (CHECK `player_id ~ '^[0-9]{1,20}$'`), `club_id` (CHECK `club_id ~ '^[0-9]+$'`), `platform` default `'common-gen5'`, `display_name` (<=64 chars), optional `squad_number` (1-99) unique per club, `active` boolean default true, `status` in ('active', 'inactive', 'suspended'), timestamps.
  - `public.dispatches`: PK `dispatch_id`, FKs to `graph_threads(thread_id)`, `approval_audit(id)`, `approval_requests(approval_request_id)`, `players(player_id)`, `approved_instruction jsonb`, `dispatch_status` (full authoritative domain), `status text GENERATED ALWAYS AS (dispatch_status) STORED`, `version integer NOT NULL DEFAULT 1`, `plan_version integer NOT NULL DEFAULT 0`, `dispatch_effective boolean NOT NULL DEFAULT true`, lifecycle timestamps, `superseded_by` self-FK, `last_transition_reason text`.
  - Partial Unique Index: `dispatches_one_active_per_objective` on `(player_id, owner, (approved_instruction->'objective'->>'metric')) WHERE dispatch_status NOT IN ('completed', 'expired', 'cancelled', 'superseded')`.
  - Trigger `fc27_dispatch_insert_guard`: Initial insert must be `pending`, version 1, plan_version 0, and lifecycle timestamps NULL.
  - Trigger `fc27_dispatch_immutable`: Provenance columns (17 fields) immutable; terminal rows frozen against UPDATE/DELETE (except setting `superseded_by` once).
  - Forward-compatible tracking tables (all with append-only trigger `fc27_reject_mutation`):
    - `public.dispatch_plans` (status check: valid, material_change, invalid, schema_invalid)
    - `public.dispatch_plan_decisions`
    - `public.dispatch_delivery_attempts`
    - `public.dispatch_acknowledgments`
    - `public.dispatch_comments`
    - `public.analyst_notes`
    - `public.player_observations`
    - `public.dispatch_assessments` (result check: tracking_error, insufficient_data, target_met, regressed, improving, no_change)
- **Section D: Transition Rules Matrix & Single Write Path Function**:
  - `public.dispatch_transition_rules`: Table defining valid edges and permissions. Seed with authoritative transitions from plan and explorer design.
  - `public.fc27_dispatch_transition`:
    - `SECURITY DEFINER` function with `SET search_path = pg_catalog, pg_temp` owned by `fc27_owner`.
    - Row-level lock (`SELECT FOR UPDATE`).
    - Validates optimistic lock (`expected_status`, `expected_version`).
    - Validates caller identity (`session_user`, `fc27.actor`) against `dispatch_transition_rules`.
    - Validates reason requirement and max 500 characters.
    - **Phase 1 Invariant Enforcement**: Unconditional block: transition to `delivered` raises SQLSTATE `FC002` ("Phase 1 invariant violation: transition to delivered is disabled (DISPATCH_ENABLED=false)").
    - SQLSTATE taxonomy: `FC001` (conflict), `FC002` (illegal transition / block), `FC003` (patch invalid), `FC004` (actor invalid), `FC005` (null correlation ID), `FC006` (precondition failed), `FC007` (reason invalid/too long).
    - Atomically updates row, increments version, and emits domain event and outbox row in the same transaction.

### 2. `migrations/post/grants.sql` & `src/db_migrate.py`
- Update `grants.sql` header to `-- grants-policy-version: 4`.
- Least-privilege grants:
  - `fc27_app`: SELECT, INSERT on `players` and `dispatches` (NO direct UPDATE/DELETE on `dispatches`). SELECT, INSERT on tracking tables. EXECUTE on `public.fc27_dispatch_transition`.
  - `fc27_dispatch`: SELECT on `players` and `dispatches` (NO direct INSERT, UPDATE, DELETE). SELECT, INSERT on tracking tables. EXECUTE on `public.fc27_dispatch_transition`.
- Synchronously update `GRANTS_POLICY` in `src/db_migrate.py` with version 4 and the new SHA-256 hash.

### 3. `src/dispatch/transition.py` & `src/dispatch/objective.py`
- `src/dispatch/objective.py`: Metric bounds policy `fc27-objective-bounds-v1` for bounded ranges (`pass_accuracy`, `rating`, `goals`).
- `src/schemas.py`: Pydantic `Objective` schema integration with bounds validation.
- `src/dispatch/transition.py`: Python wrapper around `public.fc27_dispatch_transition` with structured error mapping to Python exceptions (`VersionConflictError`, `IllegalTransitionError`, `InvalidActorError`, etc.).

### 4. `tests/conftest.py`, `tests/test_dispatch_schema.py`, `tests/test_dispatch_transition.py`
- Implement fixtures in `tests/conftest.py`: `sample_player`, `sample_approved_dispatch_prereqs`, `make_dispatch`.
- Implement all 18 schema tests in `tests/test_dispatch_schema.py`.
- Implement all 22 transition tests in `tests/test_dispatch_transition.py`.

## Verification Commands
Execute the test suites against local PostgreSQL 16:
```pwsh
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_db_roles.py tests/test_db_migrate_roles.py tests/test_events_outbox.py tests/test_migration_0003.py tests/test_approval_lifecycle.py tests/test_approval_security.py tests/test_graph_persistence.py tests/test_graph_api_v1.py tests/test_dispatch_schema.py tests/test_dispatch_transition.py
```

## Completion Criteria
1. Migration 0003 applies cleanly and grants policy matches SHA-256 in `src/db_migrate.py`.
2. All 18 schema tests in `tests/test_dispatch_schema.py` pass.
3. All 22 transition tests in `tests/test_dispatch_transition.py` pass.
4. No regressions across existing suites (`test_db_roles.py`, `test_approval_lifecycle.py`, `test_graph_persistence.py`, `test_graph_api_v1.py`).
5. Write detailed 5-component `handoff.md` in your working directory and notify the orchestrator via `send_message`.


## 2026-10-03T17:31:13Z
[Message] timestamp=2026-10-03T17:31:13Z sender=7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b priority=MESSAGE_PRIORITY_HIGH content=You are worker_m3_1, a teamwork_preview_worker agent.
Your working directory is: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_1

Read your instructions in:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_1\DISPATCH.md

MANDATORY: You MUST read:
1. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md (authoritative user request + Section 1A patch)
2. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md
3. The 3 Milestone 3 explorer reports:
   - c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_schema_1\schema_analysis.md
   - c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_transition_1\transition_analysis.md
   - c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_tests_1\tests_analysis.md
4. docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md (Tasks 6 & 7)

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

Write ownership:
- migrations/0003_dispatch_foundation.sql (Sections C and D)
- migrations/post/grants.sql (grants-policy-version: 4)
- src/db_migrate.py (update GRANTS_POLICY version to 4 and sha256 to match grants.sql)
- src/dispatch/transition.py
- src/dispatch/objective.py
- src/schemas.py
- tests/conftest.py
- tests/test_dispatch_schema.py
- tests/test_dispatch_transition.py

Task:
Implement Milestone 3 (Dispatch Schema, Hardened Transition Function, Objective Bounds, and Test Suites).
Execute tests via:
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_db_roles.py tests/test_db_migrate_roles.py tests/test_events_outbox.py tests/test_migration_0003.py tests/test_approval_lifecycle.py tests/test_approval_security.py tests/test_graph_persistence.py tests/test_graph_api_v1.py tests/test_dispatch_schema.py tests/test_dispatch_transition.py

Write handoff.md in your working directory and notify the orchestrator with send_message when done.
