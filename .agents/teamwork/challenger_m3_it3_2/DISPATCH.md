# Dispatch: challenger_m3_it3_2

## Identity
- Role: Milestone 3 Iteration 3 Challenger 2 (Role Boundaries, Provenance Immutability & Schema Verification)
- Working Directory: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_it3_2`
- Parent: Orchestrator Phase 1 (`7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b`)

## Mandatory Inputs to Read First
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (authoritative user request + Section 1A patch)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`
3. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_3\handoff.md`
4. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_4\handoff.md`
5. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_it3_1\handoff.md`
6. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_it3_2\handoff.md`

## Challenge Scope & Adversarial Attacks
Adversarially challenge role boundaries, direct table manipulation, provenance immutability, zero-echo security, and test suite execution:
1. **Direct Write Bypass & Role Isolation**:
   - Verify `fc27_app` has no direct `UPDATE` or `DELETE` on `dispatches`.
   - Verify `fc27_dispatch` has no direct `INSERT`, `UPDATE`, or `DELETE` on `dispatches`.
   - Verify all 8 tracking companion tables reject all `UPDATE` and `DELETE` mutations.
2. **Provenance Immutability & Zero-Echo Verification**:
   - Verify all 17 provenance fields and terminal rows remain immutable.
   - Verify `Objective`, `Candidate`, and `PlayerObservation` do not leak candidate delta values in error strings (`hide_input_in_errors=True`).
3. **Execution Verification**:
   Execute the full schema and migration role test suites:
   ```pwsh
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_schema.py tests/test_db_migrate_roles.py
   ```
   Verify all tests pass (100% PASS, including `test_tracking_tables_append_only[player_observations]`).
4. Explicit verdict: `APPROVE` or `REQUEST_CHANGES`.
5. Write `challenge_report.md` and `handoff.md` in your working directory.
6. Notify orchestrator via `send_message`.
Do NOT modify production source code in `src/` or `migrations/`.

## 2026-10-03T19:48:49Z
You are challenger_m3_it3_2, a teamwork_preview_challenger agent.
Your working directory is: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_it3_2

Read your instructions in:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_it3_2\DISPATCH.md

MANDATORY: You MUST read:
1. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md (authoritative user request + Section 1A patch)
2. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md
3. Worker 3 Handoff: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_3\handoff.md
4. Worker 4 Handoff: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_4\handoff.md
5. Reviewer 1 Handoff: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_it3_1\handoff.md
6. Reviewer 2 Handoff: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_it3_2\handoff.md

Task:
Adversarially challenge role boundaries, direct table manipulation, provenance immutability, zero-echo security, and test suite execution:
1. Direct Write Bypass & Role Isolation:
   - Verify fc27_app has no direct UPDATE or DELETE on dispatches.
   - Verify fc27_dispatch has no direct INSERT, UPDATE, or DELETE on dispatches.
   - Verify all 8 tracking companion tables reject all UPDATE and DELETE mutations.
2. Provenance Immutability & Zero-Echo Verification:
   - Verify all 17 provenance fields and terminal rows remain immutable.
   - Verify Objective, Candidate, and PlayerObservation do not leak candidate delta values in error strings (hide_input_in_errors=True).
3. Execution Verification:
   Execute the full schema and migration role test suites:
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_schema.py tests/test_db_migrate_roles.py
   Verify all tests pass (100% PASS, including test_tracking_tables_append_only[player_observations]).
4. Explicit verdict: APPROVE or REQUEST_CHANGES.
5. Write challenge_report.md and handoff.md in your working directory.
6. Notify orchestrator via send_message when done.
Do NOT modify production source code in src/ or migrations/.
