# Task Assignment: auditor_m3_1 (Milestone 3 Forensic Auditor)

## Mission
Perform exhaustive forensic integrity audit across all files modified or added in Milestone 3, verifying genuine implementations, zero facades/shortcuts, strict adherence to Phase 1 invariants, and Section 1A constraints.

## Working Directory
`c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m3_1`

## Mandatory Reading
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (authoritative user request + Section 1A patch)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`
3. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_1\handoff.md`

## Audit Files
- `migrations/0003_dispatch_foundation.sql` (Sections C and D)
- `migrations/post/grants.sql`
- `src/db_migrate.py`
- `src/dispatch/transition.py`
- `src/dispatch/objective.py`
- `src/schemas.py`
- `tests/conftest.py`
- `tests/test_dispatch_schema.py`
- `tests/test_dispatch_transition.py`

## Forensic Audit Protocol
1. **Static Analysis & Authenticity**:
   - Zero hardcoded test return values, mock facades, or dummy implementations.
   - Genuine DDL for `players`, `dispatches`, partial unique indexes, and 8 tracking tables.
   - Genuine stored procedure `public.fc27_dispatch_transition` with zero dynamic SQL (`EXECUTE`, `format(`), `SECURITY DEFINER`, search_path `pg_catalog, pg_temp`, row locking, and atomic outbox emission.
   - Genuine `GRANTS_POLICY` version 4 and SHA-256 integrity check.
   - Genuine Pydantic bounds validation in `Objective`.
2. **Phase 1 Invariants & Security Guard**:
   - Verify `DISPATCH_ENABLED=false` is maintained.
   - Verify transition to `delivered` is unconditionally blocked in `public.fc27_dispatch_transition` with SQLSTATE `FC002`.
   - Verify no player delivery logic or external adapters are invoked.
   - Verify Section 1A human authority constraints are preserved.
3. **Execution Verification**:
   Execute the full verification suite against local PostgreSQL 16:
   ```pwsh
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_db_roles.py tests/test_db_migrate_roles.py tests/test_events_outbox.py tests/test_migration_0003.py tests/test_approval_lifecycle.py tests/test_approval_security.py tests/test_graph_persistence.py tests/test_graph_api_v1.py tests/test_dispatch_schema.py tests/test_dispatch_transition.py
   ```

## Output Requirements
Formulate an explicit binary verdict: **CLEAN** or **INTEGRITY VIOLATION**. (Binary veto).
Write `audit_report.md` and `handoff.md` in your working directory.
Notify the orchestrator with `send_message` when done.
Do NOT modify production source code in `src/` or `migrations/`.

## 2026-10-03T17:49:48Z
You are auditor_m3_1, a teamwork_preview_auditor agent.
Your working directory is: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m3_1

Read your instructions in:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m3_1\DISPATCH.md

MANDATORY: You MUST read:
1. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md (authoritative user request + Section 1A patch)
2. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md
3. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_1\handoff.md

Task:
Perform exhaustive forensic integrity audit across all files modified or added in Milestone 3:
migrations/0003_dispatch_foundation.sql, migrations/post/grants.sql, src/db_migrate.py, src/dispatch/transition.py, src/dispatch/objective.py, src/schemas.py, tests/conftest.py, tests/test_dispatch_schema.py, tests/test_dispatch_transition.py.

Verify:
1. Static Analysis & Authenticity: zero fake implementations, mock shortcuts, or hardcoded test values. Genuine DDL, genuine stored procedure with zero dynamic SQL, genuine triggers, genuine grants policy v4 with matching SHA-256, genuine objective bounds validation.
2. Invariants: DISPATCH_ENABLED=false strictly enforced; transition to delivered blocked closed; zero player delivery or adapter calls; Section 1A constraints strictly maintained.
3. Execution Verification: run full verification suite:
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_db_roles.py tests/test_db_migrate_roles.py tests/test_events_outbox.py tests/test_migration_0003.py tests/test_approval_lifecycle.py tests/test_approval_security.py tests/test_graph_persistence.py tests/test_graph_api_v1.py tests/test_dispatch_schema.py tests/test_dispatch_transition.py

Formulate explicit binary verdict: CLEAN or INTEGRITY VIOLATION. (Binary veto).
Write audit_report.md and handoff.md in your working directory.
Notify orchestrator via send_message when done.
Do NOT modify production source code in src/ or migrations/.
