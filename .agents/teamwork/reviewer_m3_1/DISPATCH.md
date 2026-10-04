# Task Assignment: reviewer_m3_1 (Milestone 3 Reviewer 1 - Schema, Triggers & Grants)

## Mission
Perform independent review of Milestone 3 focusing on database schema, immutability triggers, least-privilege grants policy v4, and schema tests.

## Working Directory
`c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_1`

## Mandatory Reading
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (authoritative user request + Section 1A patch)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`
3. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_1\handoff.md`
4. `migrations/0003_dispatch_foundation.sql` (Section C)
5. `migrations/post/grants.sql`
6. `src/db_migrate.py`
7. `tests/test_dispatch_schema.py`

## Review Verification Checklist
1. **`public.players` Schema**:
   - `player_id` is numeric string format `^[0-9]{1,20}$`. Non-numeric or length > 20 rejected.
   - `club_id` is numeric string format `^[0-9]+$`.
   - `platform` default `'common-gen5'`.
   - `display_name` max length 64.
   - `squad_number` between 1 and 99, unique per club when set.
2. **`public.dispatches` Schema & Triggers**:
   - Foreign keys to `graph_threads`, `approval_requests`, `approval_audit`, `players`.
   - `dispatch_status` domain matches authoritative specification.
   - Partial unique index `dispatches_one_active_per_objective` prevents duplicate active objectives per player and owner.
   - Trigger `fc27_dispatch_insert_guard` ensures initial insert has `dispatch_status = 'pending'`, `version = 1`, `plan_version = 0`, and lifecycle timestamps NULL.
   - Trigger `fc27_dispatch_immutable` prevents mutation of all 17 provenance fields and freezes terminal rows.
3. **Tracking Tables**:
   - 8 tracking tables (`dispatch_plans`, `dispatch_plan_decisions`, `dispatch_delivery_attempts`, `dispatch_acknowledgments`, `dispatch_comments`, `analyst_notes`, `player_observations`, `dispatch_assessments`) protected by append-only trigger `fc27_reject_mutation()`.
4. **Grants Policy v4**:
   - `migrations/post/grants.sql` has header `-- grants-policy-version: 4`.
   - `fc27_app`: SELECT, INSERT on `players` and `dispatches` (NO direct UPDATE/DELETE on `dispatches`).
   - `fc27_dispatch`: SELECT on `players` and `dispatches` (NO direct INSERT, UPDATE, DELETE).
   - `src/db_migrate.py`: `GRANTS_POLICY` version is 4 and sha256 matches `grants.sql`. Catalog postconditions verify function security properties.
5. **Execute Test Verification**:
   ```pwsh
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_db_roles.py tests/test_db_migrate_roles.py tests/test_migration_0003.py tests/test_dispatch_schema.py
   ```

## Output Requirements
Formulate an explicit verdict: **APPROVE** or **REQUEST_CHANGES**.
Write `review.md` and `handoff.md` in your working directory.
Notify the orchestrator with `send_message` when done.
Do NOT modify production source code in `src/` or `migrations/`.


## 2026-10-03T17:49:47Z
You are reviewer_m3_1, a teamwork_preview_reviewer agent.
Your working directory is: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_1

Read your instructions in:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_1\DISPATCH.md

MANDATORY: You MUST read:
1. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md (authoritative user request + Section 1A patch)
2. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md
3. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_1\handoff.md

Task:
Perform independent review of Milestone 3 focusing on database schema, immutability triggers, least-privilege grants policy v4, and schema tests:
1. Verify public.players table constraints (numeric player_id regex, club_id regex, platform defaults).
2. Verify public.dispatches table foreign keys, insert guard trigger, 17 immutable provenance columns, terminal row freezing, and partial unique index.
3. Verify 8 forward-compatible tracking tables protected by append-only triggers.
4. Verify migrations/post/grants.sql (version 4) least-privilege grants and src/db_migrate.py GRANTS_POLICY sha256 synchronization.
5. Run tests:
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_db_roles.py tests/test_db_migrate_roles.py tests/test_migration_0003.py tests/test_dispatch_schema.py

Formulate explicit verdict: APPROVE or REQUEST_CHANGES.
Write review.md and handoff.md in your working directory.
Notify orchestrator via send_message when done.
Do NOT modify production source code in src/ or migrations/.
