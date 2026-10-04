# Dispatch: worker_m3_4

## Identity
- Role: Milestone 3 Iteration 3 Polish Worker (Schema Test Column Alignment)
- Working Directory: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_4`
- Parent: Orchestrator Phase 1 (`7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b`)

## Mandatory Inputs to Read First
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (authoritative user request + Section 1A patch)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`
3. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_3\handoff.md`
4. `tests/test_dispatch_schema.py` (lines 310–335)
5. `migrations/0003_dispatch_foundation.sql` (lines 1148–1162 for `public.player_observations`)

## MANDATORY INTEGRITY WARNING
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

## Write Ownership
- `tests/test_dispatch_schema.py` (exclusive write ownership)
Do NOT modify files in `src/`, `migrations/`, or `tests/conftest.py`.

## Task
1. In `tests/test_dispatch_schema.py:328–331`:
   In `test_tracking_tables_append_only`, table `public.player_observations` defines column `ingested_at timestamptz` (not `created_at`).
   Replace line 330:
   ```python
   col = "ingested_at" if table == "player_observations" else "created_at"
   cur.execute(f"UPDATE public.{table} SET {col} = now() WHERE {where}")
   ```
2. Execute test verification suites:
   ```pwsh
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_schema.py tests/test_dispatch_transition.py tests/test_db_migrate_roles.py
   ```
   Verify 100% pass across all 48 tests.
3. Write `handoff.md` in your working directory.
4. Notify orchestrator via `send_message` when done.


## 2026-10-03T19:36:20Z
You are worker_m3_4, a teamwork_preview_worker agent.
Your working directory is: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_4

Read your instructions in:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_4\DISPATCH.md

MANDATORY: You MUST read:
1. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md (authoritative user request + Section 1A patch)
2. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md
3. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_3\handoff.md
4. tests/test_dispatch_schema.py (lines 310–335)
5. migrations/0003_dispatch_foundation.sql (lines 1148–1162 for public.player_observations)

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

Write Ownership:
- tests/test_dispatch_schema.py (exclusive write ownership)
Do NOT modify files in src/, migrations/, or tests/conftest.py.

Task:
1. In tests/test_dispatch_schema.py:328–331:
   In test_tracking_tables_append_only, table public.player_observations defines column ingested_at timestamptz (not created_at).
   Replace line 330:
   col = "ingested_at" if table == "player_observations" else "created_at"
   cur.execute(f"UPDATE public.{table} SET {col} = now() WHERE {where}")

2. Execute test verification suites:
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_schema.py tests/test_dispatch_transition.py tests/test_db_migrate_roles.py
   Verify 100% pass across all tests.

3. Write handoff.md in your working directory.
4. Notify orchestrator via send_message when done.
