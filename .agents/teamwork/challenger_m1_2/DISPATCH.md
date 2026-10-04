# Task Assignment — challenger_m1_2

## Objective
Empirically stress-test the identity store, database schema, packaging sync, and scope boundaries for Phase 2 Milestone 1.

## Authoritative Inputs
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (Read first!)
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
- Implementation files: `pyproject.toml`, `requirements.txt`, `tests/test_phase1_scope.py`, `src/identity/store.py`, `migrations/0004_identity_foundation.sql`

## Stress Test Areas
1. Packaging sync: `pytest tests/test_packaging.py`.
2. Scope boundary tests: `pytest tests/test_phase1_scope.py` (ensure `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` remain strictly enforced, no worker/relay).
3. Schema & trigger boundaries: test recovery codes cannot be deleted (`fc27_reject_mutation`), sign counters cannot decrement, duplicate logins fail.
4. Session lifecycle: idle vs absolute timeout logic, session rotation invalidation.

Write and execute empirical stress tests. Report results in `handoff.md` with explicit verdict: APPROVE or REQUEST_CHANGES.


## 2026-10-04T03:07:26Z
You are challenger_m1_2, an adversarial verifier agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m1_2
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m1_2\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md before starting work.

Your task:
Empirically stress-test the identity store, database schema, packaging sync, and scope boundaries for Phase 2 Milestone 1.
1. Packaging sync: pytest tests/test_packaging.py.
2. Scope boundary tests: pytest tests/test_phase1_scope.py (ensure DISPATCH_ENABLED=false and DISPATCH_WORKER_ENABLED=false remain strictly enforced, no worker/relay).
3. Schema & trigger boundaries: test recovery codes cannot be deleted (fc27_reject_mutation), sign counters cannot decrement, duplicate logins fail.
4. Session lifecycle: idle vs absolute timeout logic, session rotation invalidation.
Write stress test scripts / execute tests, document in handoff.md.
Provide an explicit verdict: APPROVE or REQUEST_CHANGES.
Write your complete handoff report to:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m1_2\handoff.md
When finished, notify me with your verdict and report path via send_message.
