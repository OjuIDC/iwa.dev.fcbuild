# Dispatch: reviewer_m3_it2_2

## Identity
- Role: Milestone 3 Iteration 2 Reviewer 2 (Transition Function, Service, Objective Bounds & Pydantic Validation)
- Working Directory: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_it2_2`
- Parent: Orchestrator Phase 1 (`7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b`)

## Mandatory Inputs to Read First
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (authoritative user request + Section 1A patch)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`
3. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_2\handoff.md`
4. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_2\challenge_report.md` (previous gate failure report)

## Review Scope & Objectives
Review Milestone 3 Iteration 2 changes for the single-write-path transition function, Python service wrapper, and objective bounds:
1. **Transition Function (`migrations/0003_dispatch_foundation.sql:1499-1514`)**:
   - Verify Step 7 in `fc27_dispatch_transition` is properly scoped to `IF v_actor_kind = 'commissioner' AND p_to_status = 'superseded'` to permit valid commissioner retries (such as `failed -> queued`) while enforcing audit proof for supersession.
   - Verify Phase 1 invariant: transitions to `delivered` strictly fail closed.
   - Verify row lock `SELECT FOR UPDATE` on dispatches and search_path isolation (`search_path = pg_catalog, pg_temp`).
2. **Objective Bounds & Zero-Echo Security (`src/schemas.py` & `src/dispatch/objective.py`)**:
   - Verify `hide_input_in_errors=True` is configured on `Objective`, `Candidate`, and `PlayerObservation`.
   - Verify `validate_metric_bounds` in `src/dispatch/objective.py` enforces policy `fc27-objective-bounds-v1` and emits sanitized error messages that never echo invalid candidate input values.
3. **Execution Verification**:
   Run tests using pytest:
   ```pwsh
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_transition.py tests/test_events_outbox.py tests/test_approval_lifecycle.py
   ```
4. **Deliverables**:
   - Write `review.md` and `handoff.md` in your working directory.
   - Explicit verdict: `APPROVE` or `REQUEST_CHANGES`.
   - Notify orchestrator via `send_message`.
   - Do NOT modify production source code in `src/` or `migrations/`.

## 2026-10-03T18:34:23Z
You are reviewer_m3_it2_2, a teamwork_preview_reviewer agent.
Your working directory is: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_it2_2

Read your instructions in:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_it2_2\DISPATCH.md

MANDATORY: You MUST read:
1. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md (authoritative user request + Section 1A patch)
2. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md
3. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_2\handoff.md
4. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_2\challenge_report.md

Task:
Review Milestone 3 Iteration 2 changes for the single-write-path transition function, Python service wrapper, and objective bounds:
1. Transition Function (migrations/0003_dispatch_foundation.sql:1499-1514):
   - Verify Step 7 in fc27_dispatch_transition is properly scoped to IF v_actor_kind = 'commissioner' AND p_to_status = 'superseded' to permit valid commissioner retries (such as failed -> queued) while enforcing audit proof for supersession.
   - Verify Phase 1 invariant: transitions to delivered strictly fail closed.
   - Verify row lock SELECT FOR UPDATE on dispatches and search_path isolation (search_path = pg_catalog, pg_temp).
2. Objective Bounds & Zero-Echo Security (src/schemas.py & src/dispatch/objective.py):
   - Verify hide_input_in_errors=True is configured on Objective, Candidate, and PlayerObservation.
   - Verify validate_metric_bounds in src/dispatch/objective.py enforces policy fc27-objective-bounds-v1 and emits sanitized error messages that never echo invalid candidate input values.
3. Execution Verification:
   Run tests:
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_transition.py tests/test_events_outbox.py tests/test_approval_lifecycle.py
4. Formulate explicit verdict: APPROVE or REQUEST_CHANGES.
5. Write review.md and handoff.md in your working directory.
6. Notify orchestrator via send_message when done.
Do NOT modify production source code in src/ or migrations/.
