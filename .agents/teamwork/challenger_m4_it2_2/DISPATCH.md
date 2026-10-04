# Dispatch: Milestone 4 Iteration 2 Challenger 2 (Adversarial Security & Rollback)

**Agent**: `challenger_m4_it2_2`
**Role**: M4 Iteration 2 Challenger 2 (Adversarial Security & Rollback)
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m4_it2_2`
**Timestamp**: 2026-10-03T21:02:00Z

---

## 1. Objective
Adversarially challenge Milestone 4 atomic rollback guarantees, cryptographic nonce protections, advisory lock concurrency, and Phase 1 boundary invariants.

## 2. Mandatory Reading
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (authoritative user request + Section 1A patch)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`
3. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m4_2\handoff.md`
4. `src/dispatch/gates.py`
5. `src/dispatch/creation.py`
6. `src/approval_store.py`
7. `tests/test_dispatch_creation.py`

## 3. Tasks & Attacks
1. Challenge Nonce & Gate Protocol:
   - Verify replayed nonces fail with 409 Conflict.
   - Verify G-OUTBOX challenge/record fails with 409 Conflict (C-3).
   - Verify correlation ID isolation in `GATE_SIGNED_FIELDS` (C-4, C-5).
2. Challenge Atomic Rollback & Partition Locks:
   - Verify candidates lacking objective or with unverified player IDs fail with 409 `not_dispatchable`, completely rolling back transaction.
   - Verify advisory lock `pg_advisory_xact_lock(hashtextextended(player_id || '|' || owner || '|' || metric, 0))` prevents duplicate active dispatches on concurrent approvals.
3. Formulate explicit verdict: **APPROVE** or **REQUEST_CHANGES**.
4. Write `challenge_report.md` and `handoff.md` in your working directory.
5. Notify orchestrator via `send_message` when done.
Do NOT modify production source code in `src/` or `migrations/`.

## 2026-10-03T21:02:27Z
You are challenger_m4_it2_2, a teamwork_preview_challenger agent.
Your working directory is: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m4_it2_2

Read your instructions in:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m4_it2_2\DISPATCH.md

MANDATORY: You MUST read:
1. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md (authoritative user request + Section 1A patch)
2. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md
3. Worker Handoff: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m4_2\handoff.md
4. src/dispatch/gates.py
5. src/dispatch/creation.py
6. src/approval_store.py
7. tests/test_dispatch_creation.py

Task:
Adversarially challenge Milestone 4 atomic rollback guarantees, cryptographic nonce protections, advisory lock concurrency, and Phase 1 boundary invariants:
1. Challenge Nonce & Gate Protocol:
   - Replayed nonces fail with 409 Conflict
   - G-OUTBOX challenge/record fails with 409 Conflict (C-3)
   - Correlation ID isolation in GATE_SIGNED_FIELDS (C-4, C-5)
2. Challenge Atomic Rollback & Partition Locks:
   - Candidates lacking objective or with unverified player IDs fail with 409 not_dispatchable, completely rolling back transaction
   - Advisory lock pg_advisory_xact_lock(hashtextextended(player_id || '|' || owner || '|' || metric, 0)) prevents duplicate active dispatches on concurrent approvals
   - 10 duplicate replay submissions return winner dispatch_id
3. Execution verification:
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_creation.py
4. Explicit verdict: APPROVE or REQUEST_CHANGES.
5. Write challenge_report.md and handoff.md in your working directory.
6. Notify orchestrator via send_message when done.
Do NOT modify production source code in src/ or migrations/.
