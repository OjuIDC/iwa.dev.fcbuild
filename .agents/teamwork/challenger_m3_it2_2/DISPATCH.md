# Dispatch: challenger_m3_it2_2

## Identity
- Role: Milestone 3 Iteration 2 Challenger 2 (Role Boundaries, Provenance Integrity & Zero-Echo Security)
- Working Directory: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_it2_2`
- Parent: Orchestrator Phase 1 (`7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b`)

## Mandatory Inputs to Read First
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (authoritative user request + Section 1A patch)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`
3. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_2\handoff.md`
4. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_2\challenge_report.md` (previous gate failure report)
5. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_it2_1\handoff.md`
6. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_it2_2\handoff.md`

## Challenge Scope & Adversarial Attacks
Adversarially challenge role boundaries, direct table manipulation, provenance immutability, search path isolation, and candidate input zero-echo leak:
1. **Direct Write Bypass Attacks**:
   - Verify `fc27_app` cannot execute direct `UPDATE` or `DELETE` on `dispatches` (must fail with `InsufficientPrivilege`).
   - Verify `fc27_dispatch` cannot execute direct `INSERT`, `UPDATE`, or `DELETE` on `dispatches` (must fail with `InsufficientPrivilege`).
   - Verify direct `UPDATE` and `DELETE` on all 8 tracking companion tables is blocked for runtime roles.
2. **Provenance Column & Terminal Row Tampering**:
   - Verify all 17 provenance fields on `dispatches` cannot be updated even under superuser.
   - Verify terminal dispatch rows (`delivered`, `failed`, `cancelled`) are permanently frozen against any update or deletion.
3. **Search Path Hijacking Attacks**:
   - Verify `fc27_dispatch_transition` enforces `search_path = pg_catalog, pg_temp` and explicit `public.*` schema qualification, preventing function shadowing or temp-table hijacking.
   - Verify `PUBLIC` execute privilege is revoked on `fc27_dispatch_transition`.
4. **Candidate Input Zero-Echo Verification (Challenge 1 from previous round)**:
   - Probe `Objective`, `Candidate`, and `PlayerObservation` with invalid metrics, out-of-bounds target deltas, and malicious payload strings.
   - Verify `ValidationError` never echoes candidate inputs or out-of-bounds metrics (e.g. `26.123`).
5. **Fixture Terminal Constraint Verification (Challenge 2 from previous round)**:
   - Verify `sample_approved_dispatch_prereqs` and `make_dispatch` cleanly pass all terminal field constraints and deferred triggers.
6. **Execution Verification**:
   Run tests:
   ```pwsh
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_schema.py tests/test_db_migrate_roles.py
   ```
7. **Deliverables**:
   - Write `challenge_report.md` and `handoff.md` in your working directory.
   - Explicit verdict: `APPROVE` or `REQUEST_CHANGES`.
   - Notify orchestrator via `send_message`.
   - Do NOT modify production source code in `src/` or `migrations/`.


## 2026-10-03T18:40:33Z
You are challenger_m3_it2_2, a teamwork_preview_challenger agent.
Your working directory is: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_it2_2

Read your instructions in:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_it2_2\DISPATCH.md

MANDATORY: You MUST read:
1. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md (authoritative user request + Section 1A patch)
2. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md
3. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_2\handoff.md
4. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_2\challenge_report.md
5. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_it2_1\handoff.md
6. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_it2_2\handoff.md

Task:
Adversarially challenge role boundaries, direct table manipulation, provenance immutability, search path isolation, and candidate input zero-echo leak:
1. Direct Write Bypass Attacks:
   - Verify fc27_app cannot execute direct UPDATE or DELETE on dispatches (must fail with InsufficientPrivilege).
   - Verify fc27_dispatch cannot execute direct INSERT, UPDATE, or DELETE on dispatches (must fail with InsufficientPrivilege).
   - Verify direct UPDATE and DELETE on all 8 tracking companion tables is blocked for runtime roles.
2. Provenance Column & Terminal Row Tampering:
   - Verify all 17 provenance fields on dispatches cannot be updated even under superuser.
   - Verify terminal dispatch rows (delivered, failed, cancelled) are permanently frozen against any update or deletion.
3. Search Path Hijacking Attacks:
   - Verify fc27_dispatch_transition enforces search_path = pg_catalog, pg_temp and explicit public.* schema qualification, preventing function shadowing or temp-table hijacking.
   - Verify PUBLIC execute privilege is revoked on fc27_dispatch_transition.
4. Candidate Input Zero-Echo Verification (Challenge 1 from previous round):
   - Probe Objective, Candidate, and PlayerObservation with invalid metrics, out-of-bounds target deltas, and malicious payload strings.
   - Verify ValidationError never echoes candidate inputs or out-of-bounds metrics (e.g. 26.123).
5. Fixture Terminal Constraint Verification (Challenge 2 from previous round):
   - Verify sample_approved_dispatch_prereqs and make_dispatch cleanly pass all terminal field constraints and deferred triggers.
6. Execution Verification:
   Run tests:
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_schema.py tests/test_db_migrate_roles.py
7. Formulate explicit verdict: APPROVE or REQUEST_CHANGES.
8. Write challenge_report.md and handoff.md in your working directory.
9. Notify orchestrator via send_message when done.
Do NOT modify production source code in src/ or migrations/.
