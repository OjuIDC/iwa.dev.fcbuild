# Dispatch: Challenger 1 (Adversarial Concurrency & Deadlock Verifier)

## Identity
- Archetype: teamwork_preview_challenger
- Working Directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m1_1

## Mandatory Inputs
You MUST read:
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (including Section 1A patch).
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`.
3. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m1_1\handoff.md`.

## Task
Adversarially challenge the Milestone 1 implementation with stress tests, race conditions, and concurrency harnesses:
1. Concurrency & Deadlock Stress:
   - Construct a test script/harness that concurrently runs multiple `apply_decision` and `expire_due` calls targeting the same thread at expiry boundary.
   - Assert zero deadlocks occur under high contention (PostgreSQL lock ordering `approval_nonces` -> `approval_requests` -> `graph_threads`).
   - Assert exactly one terminal state wins (`approved` OR `expired`, never both).
2. Replay & Idempotency Challenge:
   - Challenge the safe idempotent replay: repeat identical signed decisions under concurrent conditions; verify all return 200 OK without state corruption.
   - Challenge conflicting decision replays: attempt concurrent mutations of intent/notes/selection with the same nonce; verify all return 409 Conflict.
3. Transaction Rollback Fault Injection:
   - Inject transient faults into `approval_audit`, `domain_events`, and `outbox`; verify complete atomic rollback (thread remains `awaiting_approval`, nonce remains unconsumed).
4. Run tests against PostgreSQL test cluster:
   `$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_approval_lifecycle.py`
5. Formulate explicit verdict: `APPROVE` or `REQUEST_CHANGES`.
6. Write your challenge findings to `challenge_report.md` and complete handoff report to `handoff.md`.
7. Notify the orchestrator via `send_message`.
Do NOT modify production source code in `src/` or `migrations/`.


## 2026-10-03T15:19:15Z
You are challenger_m1_1, a teamwork_preview_challenger agent.
Your working directory is: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m1_1

Read your instructions in:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m1_1\DISPATCH.md

MANDATORY: You MUST read:
1. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md (including Section 1A patch)
2. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md
3. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m1_1\handoff.md

Task:
Adversarially challenge Milestone 1 with stress tests, race conditions, and concurrency harnesses:
1. Concurrency & Deadlock Stress: test concurrent apply_decision and expire_due at deadline; assert zero deadlocks and exactly one winner.
2. Replay & Idempotency: challenge safe idempotent replay vs conflicting replays under concurrency.
3. Fault Injection: verify complete atomic transaction rollback on failure.
4. Run tests:
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_approval_lifecycle.py
5. Formulate explicit verdict: APPROVE or REQUEST_CHANGES.
6. Write challenge_report.md and handoff.md in your working directory.
7. Notify orchestrator with send_message when finished.
Do NOT modify production source code in src/ or migrations/.
