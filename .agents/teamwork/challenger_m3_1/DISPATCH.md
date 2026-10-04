# Task Assignment: challenger_m3_1 (Milestone 3 Challenger 1 - State Machine & Phase 1 Invariants)

## Mission
Adversarially challenge Milestone 3 state machine transitions, optimistic locking under concurrency, and strict Phase 1 delivery blocks.

## Working Directory
`c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_1`

## Mandatory Reading
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (authoritative user request + Section 1A patch)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`
3. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_1\handoff.md`
4. `migrations/0003_dispatch_foundation.sql` (Sections C and D)
5. `src/dispatch/transition.py`
6. `tests/test_dispatch_transition.py`

## Adversarial Challenge Vectors
1. **Phase 1 Delivery Bypass Attacks**:
   - Attempt transition to `delivered` using every permitted role (`fc27_app`, `fc27_dispatch`, superuser), every actor kind (`worker`, `commissioner`, `system`), with valid and invalid preconditions.
   - Verify that transition to `delivered` UNCONDITIONALLY fails closed with SQLSTATE `FC002` ("Phase 1 invariant violation: transition to delivered is disabled (DISPATCH_ENABLED=false)").
2. **State Machine Edge Bypasses**:
   - Attempt arbitrary, out-of-order, or forbidden transitions (e.g. `pending -> completed`, `pending -> delivered`, `cancelled -> pending`, `superseded -> active`).
   - Verify all forbidden edges raise SQLSTATE `FC002`.
   - Verify terminal states (`completed`, `expired`, `cancelled`, `superseded`) are completely immutable and frozen.
3. **Concurrency & Race Condition Attacks**:
   - Simulate concurrent conflicting transitions against the same dispatch row with stale `expected_version` or stale `expected_status`.
   - Verify that exactly one succeeds and the other fails with SQLSTATE `FC001` (VersionConflictError).
   - Verify that row-level locking (`SELECT FOR UPDATE`) prevents deadlocks or torn states.
4. **Outbox & Domain Event Atomicity**:
   - When a transition is rejected or rolled back, verify that zero rows remain in `public.domain_events` or `public.outbox`.
   - Verify event types and payloads match the transition.
5. **Execute Test Verification**:
   ```pwsh
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_transition.py
   ```

## Output Requirements
Formulate an explicit verdict: **APPROVE** or **REQUEST_CHANGES**.
Write `challenge_report.md` and `handoff.md` in your working directory.
Notify the orchestrator with `send_message` when done.
Do NOT modify production source code in `src/` or `migrations/`.


## 2026-10-03T17:49:47Z
[Message] timestamp=2026-10-03T17:49:47Z sender=7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b priority=MESSAGE_PRIORITY_HIGH content=You are challenger_m3_1, a teamwork_preview_challenger agent.
Your working directory is: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_1

Read your instructions in:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_1\DISPATCH.md

MANDATORY: You MUST read:
1. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md (authoritative user request + Section 1A patch)
2. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md
3. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_1\handoff.md

Task:
Adversarially challenge Milestone 3 state machine transitions, optimistic concurrency locking, and strict Phase 1 delivery blocks:
1. Phase 1 delivery bypass attacks: attempt transition to delivered with all roles and actors; verify unconditional FC002 block.
2. State machine edge bypasses: attempt out-of-order/forbidden transitions; verify FC002 rejection and terminal state freezing.
3. Concurrency attacks: conflicting concurrent transitions with stale version/status; verify exactly one succeeds and second raises FC001.
4. Outbox atomicity: verify rolled-back transitions leave zero traces in domain_events or outbox.
5. Run tests:
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_transition.py

Formulate explicit verdict: APPROVE or REQUEST_CHANGES.
Write challenge_report.md and handoff.md in your working directory.
Notify orchestrator via send_message when done.
Do NOT modify production source code in src/ or migrations/.
