# Dispatch: challenger_m3_it3_1

## Identity
- Role: Milestone 3 Iteration 3 Challenger 1 (State Machine Transitions, Concurrency & Delivery Invariants)
- Working Directory: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_it3_1`
- Parent: Orchestrator Phase 1 (`7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b`)

## Mandatory Inputs to Read First
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (authoritative user request + Section 1A patch)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`
3. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_3\handoff.md`
4. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_4\handoff.md`
5. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_it3_1\handoff.md`
6. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_it3_2\handoff.md`

## Challenge Scope & Adversarial Attacks
Adversarially challenge `public.fc27_dispatch_transition` and `tests/test_dispatch_transition.py`:
1. **Phase 1 Delivery Block Invariant**:
   - Probe transitions with `to_status = 'delivered'` across all roles, actors, and statuses. Verify immediate failure with `FC002`.
2. **Transition Rules & Actor Validation**:
   - Verify unpermitted edges fail with `FC002`, invalid actors fail with `FC003`, pause/cancel without reason fail with `FC005`.
   - Verify commissioner retry (`failed -> queued`) succeeds without active in-transaction `approval_audit`.
3. **Execution Verification**:
   Execute the full transition test suite:
   ```pwsh
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_transition.py
   ```
   Verify all 22 tests pass (100% PASS).
4. Explicit verdict: `APPROVE` or `REQUEST_CHANGES`.
5. Write `challenge_report.md` and `handoff.md` in your working directory.
6. Notify orchestrator via `send_message`.
Do NOT modify production source code in `src/` or `migrations/`.

## 2026-10-03T19:48:49Z
[Message] timestamp=2026-10-03T19:48:49Z sender=7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b priority=MESSAGE_PRIORITY_HIGH content=You are challenger_m3_it3_1, a teamwork_preview_challenger agent.
Your working directory is: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_it3_1

Read your instructions in:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_it3_1\DISPATCH.md

MANDATORY: You MUST read:
1. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md (authoritative user request + Section 1A patch)
2. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md
3. Worker 3 Handoff: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_3\handoff.md
4. Worker 4 Handoff: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_4\handoff.md
5. Reviewer 1 Handoff: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_it3_1\handoff.md
6. Reviewer 2 Handoff: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_it3_2\handoff.md

Task:
Adversarially challenge public.fc27_dispatch_transition and tests/test_dispatch_transition.py:
1. Phase 1 Delivery Block Invariant:
   - Probe transitions with to_status = 'delivered' across all roles, actors, and statuses. Verify immediate failure with FC002.
2. Transition Rules & Actor Validation:
   - Verify unpermitted edges fail with FC002, invalid actors fail with FC003, pause/cancel without reason fail with FC005.
   - Verify commissioner retry (failed -> queued) succeeds without active in-transaction approval_audit.
3. Execution Verification:
   Execute the full transition test suite:
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_transition.py
   Verify all 22 tests pass (100% PASS).
4. Explicit verdict: APPROVE or REQUEST_CHANGES.
5. Write challenge_report.md and handoff.md in your working directory.
6. Notify orchestrator via send_message when done.
Do NOT modify production source code in src/ or migrations/.

