# Task Assignment: challenger_m3_2 (Milestone 3 Challenger 2 - Role Boundaries & Provenance Integrity)

## Mission
Adversarially challenge Milestone 3 role boundaries, direct table manipulation attempts, provenance column immutability, and SQL injection/search_path isolation.

## Working Directory
`c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_2`

## Mandatory Reading
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (authoritative user request + Section 1A patch)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`
3. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_1\handoff.md`
4. `migrations/0003_dispatch_foundation.sql`
5. `migrations/post/grants.sql`
6. `tests/test_dispatch_schema.py`

## Adversarial Challenge Vectors
1. **Direct Write Privilege Escalation Attacks**:
   - As `fc27_app`: attempt direct `UPDATE public.dispatches` or `DELETE FROM public.dispatches`. Verify `psycopg.errors.InsufficientPrivilege`.
   - As `fc27_dispatch`: attempt direct `INSERT`, `UPDATE`, or `DELETE` on `public.dispatches`. Verify `psycopg.errors.InsufficientPrivilege`.
   - As `fc27_dispatch`: attempt access to `approval_requests`, `approval_audit`, `approval_nonces`, `graph_threads`. Verify `psycopg.errors.InsufficientPrivilege`.
2. **Provenance Column Tampering Attacks**:
   - Even under privileged / superuser connections: attempt modifying any of the 17 provenance fields (`dispatch_id`, `source_thread_id`, `approval_id`, `approval_request_id`, `player_id`, `candidate_id`, `owner`, `club_id`, `approved_instruction`, etc.).
   - Verify trigger `fc27_dispatch_immutable` unconditionally raises `CheckViolation`.
3. **Tracking Tables Append-Only Attacks**:
   - Attempt direct UPDATE or DELETE on all 8 tracking tables (`dispatch_plans`, `dispatch_plan_decisions`, `dispatch_delivery_attempts`, `dispatch_acknowledgments`, `dispatch_comments`, `analyst_notes`, `player_observations`, `dispatch_assessments`).
   - Verify trigger `fc27_reject_mutation` blocks mutations with `CheckViolation`.
4. **Search Path Hijacking & Function Privilege Attacks**:
   - Verify `fc27_dispatch_transition` has `proconfig` containing `search_path=pg_catalog, pg_temp`.
   - Attempt creating shadowing tables or functions in `pg_temp`; verify transition function cannot be hijacked.
   - Verify `PUBLIC` execution is completely revoked on `fc27_dispatch_transition`.
5. **Objective Bounds Tampering Attacks**:
   - Forge candidates with out-of-bounds metrics, negative match limits, or non-string keys; verify Pydantic `ValidationError` fails closed and does NOT echo candidate payload.
6. **Execute Test Verification**:
   ```pwsh
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_schema.py tests/test_db_migrate_roles.py
   ```

## Output Requirements
Formulate an explicit verdict: **APPROVE** or **REQUEST_CHANGES**.
Write `challenge_report.md` and `handoff.md` in your working directory.
Notify the orchestrator with `send_message` when done.
Do NOT modify production source code in `src/` or `migrations/`.


## 2026-10-03T17:49:48Z
You are challenger_m3_2, a teamwork_preview_challenger agent.
Your working directory is: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_2

Read your instructions in:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_2\DISPATCH.md

MANDATORY: You MUST read:
1. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md (authoritative user request + Section 1A patch)
2. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md
3. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_1\handoff.md

Task:
Adversarially challenge Milestone 3 role boundaries, direct table manipulation attempts, provenance column immutability, and search_path isolation:
1. Direct write attacks: verify fc27_app cannot direct UPDATE/DELETE dispatches (InsufficientPrivilege); verify fc27_dispatch cannot direct INSERT/UPDATE/DELETE dispatches.
2. Provenance tampering attacks: verify all 17 provenance fields cannot be modified even under superuser (CheckViolation).
3. Tracking tables append-only attacks: verify direct UPDATE/DELETE on all 8 tracking tables is blocked.
4. Search path hijacking: verify fc27_dispatch_transition has search_path=pg_catalog, pg_temp and cannot be shadowed; verify PUBLIC execute is revoked.
5. Objective bounds tampering: verify out-of-bounds metrics fail closed and do not echo candidate input.
6. Run tests:
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_schema.py tests/test_db_migrate_roles.py

Formulate explicit verdict: APPROVE or REQUEST_CHANGES.
Write challenge_report.md and handoff.md in your working directory.
Notify orchestrator via send_message when done.
Do NOT modify production source code in src/ or migrations/.
