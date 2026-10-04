# Task Assignment: reviewer_m3_2 (Milestone 3 Reviewer 2 - Transition Function & Service)

## Mission
Perform independent review of Milestone 3 focusing on `public.fc27_dispatch_transition`, `public.dispatch_transition_rules`, the Python transition service wrapper, objective bounds validation, and transition tests.

## Working Directory
`c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_2`

## Mandatory Reading
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (authoritative user request + Section 1A patch)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`
3. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_1\handoff.md`
4. `migrations/0003_dispatch_foundation.sql` (Section D)
5. `src/dispatch/transition.py`
6. `src/dispatch/objective.py`
7. `src/schemas.py`
8. `tests/test_dispatch_transition.py`

## Review Verification Checklist
1. **`public.fc27_dispatch_transition` Security & Semantics**:
   - `SECURITY DEFINER` function with `SET search_path = pg_catalog, pg_temp`, owned by `fc27_owner`.
   - Zero dynamic SQL (`EXECUTE`, `format(`) inside function body.
   - Row-level lock (`SELECT FOR UPDATE`) on target dispatch.
   - Optimistic concurrency locking (`expected_status`, `expected_version`) raising `FC001` on mismatch.
   - Rule matrix validation against `public.dispatch_transition_rules` raising `FC002` on illegal transition.
   - **Phase 1 Invariant**: Transition to `delivered` strictly blocked raising `FC002` ("Phase 1 invariant violation: transition to delivered is disabled (DISPATCH_ENABLED=false)").
   - Actor header derivation (`fc27.actor`) and caller role authorization raising `FC004` on unauthorized actor.
   - Mandatory correlation ID raising `FC005` on NULL.
   - Precondition checks raising `FC006` when required objects missing.
   - Reason validation raising `FC007` when reason exceeds 500 characters or missing when required.
   - Atomic emission of domain event and outbox row in the same transaction.
2. **Python Wrapper `src/dispatch/transition.py`**:
   - Requires active database transaction block.
   - Sets transaction-local `set_config('fc27.actor', ...)`.
   - Maps SQLSTATE codes `FC001`–`FC007` to clean typed Python exceptions.
3. **Objective Bounds Model `src/dispatch/objective.py` & `src/schemas.py`**:
   - Metric bounds policy `fc27-objective-bounds-v1` for bounded ranges (`pass_accuracy`, `rating`, `goals`, etc.).
   - Physical range validation (`sprint_speed`, `acceleration`, `stamina`).
   - Match constraints `1 <= min_matches <= max_matches <= 10`.
   - Validation errors do not leak/echo candidate input.
4. **Execute Test Verification**:
   ```pwsh
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_transition.py tests/test_events_outbox.py tests/test_approval_lifecycle.py
   ```

## Output Requirements
Formulate an explicit verdict: **APPROVE** or **REQUEST_CHANGES**.
Write `review.md` and `handoff.md` in your working directory.
Notify the orchestrator with `send_message` when done.
Do NOT modify production source code in `src/` or `migrations/`.


## 2026-10-03T17:49:47Z
[Message] timestamp=2026-10-03T17:49:47Z sender=7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b priority=MESSAGE_PRIORITY_HIGH content=You are reviewer_m3_2, a teamwork_preview_reviewer agent.
Your working directory is: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_2

Read your instructions in:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_2\DISPATCH.md

MANDATORY: You MUST read:
1. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md (authoritative user request + Section 1A patch)
2. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md
3. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_1\handoff.md

Task:
Perform independent review of Milestone 3 focusing on public.fc27_dispatch_transition, public.dispatch_transition_rules, Python transition service wrapper, objective bounds validation, and transition tests:
1. Verify public.fc27_dispatch_transition is SECURITY DEFINER with search_path=pg_catalog, pg_temp, owned by fc27_owner, row locking, and zero dynamic SQL.
2. Verify Phase 1 invariant: transition to delivered unconditionally blocked raising SQLSTATE FC002.
3. Verify optimistic locking (expected_status, expected_version), actor derivation, mandatory correlation ID, and atomic outbox emission.
4. Verify Python wrapper in src/dispatch/transition.py maps SQLSTATEs FC001–FC007 to typed exceptions.
5. Verify objective bounds model in src/dispatch/objective.py and src/schemas.py.
6. Run tests:
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_transition.py tests/test_events_outbox.py tests/test_approval_lifecycle.py

Formulate explicit verdict: APPROVE or REQUEST_CHANGES.
Write review.md and handoff.md in your working directory.
Notify orchestrator via send_message when done.
Do NOT modify production source code in src/ or migrations/.
