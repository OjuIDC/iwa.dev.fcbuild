# Dispatch: worker_m3_3

## Identity
- Role: Milestone 3 Iteration 3 Worker (Fixtures Foreign Key Sequencing Implementation & Verification)
- Working Directory: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_3`
- Parent: Orchestrator Phase 1 (`7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b`)

## Mandatory Inputs to Read First
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (authoritative user request + Section 1A patch)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`
3. Explorer 1 Report: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_it3_fixtures_1\fixtures_analysis.md` (includes exact drop-in code for `tests/conftest.py`)
4. Explorer 2 Report: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_it3_dispatch_tests_1\dispatch_tests_analysis.md`
5. Explorer 3 Report: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_it3_schema_tests_1\schema_tests_analysis.md`

## MANDATORY INTEGRITY WARNING
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

## Write Ownership
- `tests/conftest.py` (exclusive write ownership for this milestone iteration)
You MUST NOT modify files in `src/` or `migrations/`.

## Task
1. Implement the drop-in fixture replacements in `tests/conftest.py` for `sample_approved_dispatch_prereqs` and `make_dispatch` per Section 4 of `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_it3_fixtures_1\fixtures_analysis.md`:
   - Step 1: Insert `graph_threads` with `status = 'awaiting_approval'`, `package_version = pkg_v`, `approval_request_id = NULL`.
   - Step 2: Insert `approval_requests` with `approval_request_id = req_id`, `thread_id = tid`, `package_version = pkg_v`, `status = 'approved'`, `decided_at = now()`.
   - Step 3: Update `graph_threads` with `status = 'approved'`, `state = '{"status":"approved"}'`, `approval_request_id = req_id`, `approved_by = 'batmanL8'`, `approved_at = now()`, `decision_payload = '{"decision":"approved"}'::jsonb WHERE thread_id = tid`.
   - Step 4: Insert `approval_audit` with `thread_id = tid`, `approval_request_id = req_id`.
   - Ensure both `sample_approved_dispatch_prereqs` (primary + secondary prereqs) and `make_dispatch` use this exact sequence within their transaction blocks.
   - Retain custom SQLSTATE registration (`psycopg.errors._sqlcodes`) for `FC001` through `FC007`.

2. Execute test verification suites:
   ```pwsh
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_transition.py
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_schema.py tests/test_db_migrate_roles.py
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_events_outbox.py tests/test_migration_0003.py tests/test_approval_lifecycle.py tests/test_approval_security.py tests/test_graph_persistence.py tests/test_graph_api_v1.py
   ```

3. Deliverables:
   - Write `handoff.md` in your working directory (`c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_3\handoff.md`).
   - Notify orchestrator via `send_message` when done.


## 2026-10-03T19:01:51Z
You are worker_m3_3, a teamwork_preview_worker agent.
Your working directory is: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_3

Read your instructions in:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_3\DISPATCH.md

MANDATORY: You MUST read:
1. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md (authoritative user request + Section 1A patch)
2. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md
3. Explorer 1 Report: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_it3_fixtures_1\fixtures_analysis.md (Section 4 drop-in replacement code)
4. Explorer 2 Report: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_it3_dispatch_tests_1\dispatch_tests_analysis.md
5. Explorer 3 Report: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_it3_schema_tests_1\schema_tests_analysis.md (Section 3 refactored helper code)

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

Write Ownership:
- tests/conftest.py (exclusive write ownership for this milestone iteration)
You MUST NOT modify files in src/ or migrations/.

Task:
1. In tests/conftest.py:
   - Implement the modular helper function _seed_approved_approval_prereqs(cur, thread_id, req_id, pkg_v, player_id, ...) that executes the deadlock-free 5-step lifecycle sequence:
     Step 1: Insert graph_threads with status = 'running' (or 'awaiting_approval' with package_version set) and approval_request_id = NULL.
     Step 2: Insert approval_requests referencing thread_id.
     Step 3: Update graph_threads setting status = 'awaiting_approval', package_version = pkg_v, approval_request_id = req_id.
     Step 4: Update approval_requests setting status = 'approved', decided_at = now().
     Step 5: Update graph_threads setting status = 'approved', approved_by = 'batmanL8', approved_at = now(), decision_payload = '{"decision":"approved"}'::jsonb.
     Step 6: Insert approval_audit with thread_id and req_id.
   - Refactor sample_approved_dispatch_prereqs and make_dispatch to use _seed_approved_approval_prereqs within their transaction blocks.
   - Ensure custom SQLSTATE registration (psycopg.errors._sqlcodes) for FC001 through FC007 remains intact.
2. Execute test verification suites:
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_transition.py
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_schema.py tests/test_db_migrate_roles.py
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_events_outbox.py tests/test_migration_0003.py tests/test_approval_lifecycle.py tests/test_approval_security.py tests/test_graph_persistence.py tests/test_graph_api_v1.py
3. Write handoff.md in your working directory.
4. Notify orchestrator via send_message when done.
