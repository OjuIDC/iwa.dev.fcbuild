# Dispatch: Milestone 4 Challenger 2 (Atomic Creation, Concurrency & Rollback)

**Agent**: `challenger_m4_2`
**Role**: Milestone 4 Challenger 2 (Atomic Creation, Concurrency & Rollback)
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m4_2`
**Timestamp**: 2026-10-03T20:42:00Z

---

## 1. Objective
Adversarially challenge Milestone 4 atomic approval-to-dispatch creation, transaction rollback guarantees, advisory lock partition serialization, supersede atomicity, player validation, and duplicate replay deduplication.

## 2. Mandatory Reading
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (authoritative user request + Section 1A patch)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`
3. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m4_1\handoff.md`
4. Reviewer 1 & 2 Handoffs:
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m4_1\handoff.md`
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m4_2\handoff.md`
5. `src/dispatch/creation.py`
6. `src/approval_store.py`
7. `tests/test_dispatch_creation.py`

## 3. Adversarial Attack & Challenge Scenarios
1. **Atomic Rollback Guarantee**:
   - Probe dispatch failure scenarios (e.g. invalid objective, gamertag string, unverified player, unknown player, or DB trigger error on insert).
   - Verify transaction rolls back 100%: 0 rows in `approval_audit`, 0 rows in `dispatches`, graph thread remains `awaiting_approval`, approval request remains `awaiting_approval`, decision nonce remains unconsumed.
2. **Advisory Lock & Concurrency Serialization**:
   - Probe parallel approval requests targeting the same `(player_id, owner, metric)` partition.
   - Verify `pg_advisory_xact_lock(hashtextextended(player_id || '|' || owner || '|' || metric, 0))` serializes execution without deadlocks, primary key errors, or constraint violations on `dispatches_one_active_per_objective`.
   - Verify exactly 1 dispatch remains active while the other is superseded.
3. **Supersede Atomicity**:
   - Verify prior active dispatch moves to `superseded` with `superseded_by = new_id`.
   - Verify foreign key and transition constraints are satisfied.
4. **Idempotent Replay Protocol**:
   - Probe 10 parallel duplicate decision submissions with the same nonce.
   - Verify exactly 1 decision commits and winner's `dispatch_id` is returned to all 9 duplicate replays.
5. **Execution Verification**:
   Execute creation-specific tests in `tests/test_dispatch_creation.py`:
   ```powershell
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_creation.py -k "atomic or rollback or dispatchable or concur or supersede or duplicate"
   ```
6. Formulate explicit verdict: **APPROVE** or **REQUEST_CHANGES**.
7. Write `challenge_report.md` and `handoff.md` in your working directory.
8. Notify orchestrator via `send_message` when done.
Do NOT modify production source code in `src/` or `migrations/`.


## 2026-10-03T20:41:44Z
Dispatcher: 7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b
Message:
You are challenger_m4_2, a teamwork_preview_challenger agent.
Your working directory is: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m4_2

Read your instructions in:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m4_2\DISPATCH.md

MANDATORY: You MUST read:
1. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md (authoritative user request + Section 1A patch)
2. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md
3. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m4_1\handoff.md
4. Reviewer 1 & 2 Handoffs:
   - c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m4_1\handoff.md
   - c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m4_2\handoff.md
5. src/dispatch/creation.py
6. src/approval_store.py
7. tests/test_dispatch_creation.py

Task:
Adversarially challenge Milestone 4 atomic approval-to-dispatch creation, transaction rollback guarantees, advisory lock partition serialization, supersede atomicity, player validation, and duplicate replay deduplication:
1. Atomic Rollback Guarantee:
   - Verify candidate failure scenarios (invalid objective, gamertag string, unverified player, unknown player, or DB trigger error on insert).
   - Verify transaction rolls back 100%: 0 rows in approval_audit, 0 rows in dispatches, graph thread remains awaiting_approval, approval request remains awaiting_approval, decision nonce remains unconsumed.
2. Advisory Lock & Concurrency Serialization:
   - Verify pg_advisory_xact_lock(hashtextextended(player_id || '|' || owner || '|' || metric, 0)) serializes concurrent approval requests for the same partition without deadlocks, primary key errors, or constraint violations on dispatches_one_active_per_objective.
   - Verify exactly 1 dispatch remains active while the other is superseded.
3. Supersede Atomicity:
   - Verify prior active dispatch moves to superseded with superseded_by = new_id.
   - Verify foreign key and transition constraints are satisfied.
4. Idempotent Replay Protocol:
   - Verify 10 parallel duplicate decision submissions with the same nonce yield exactly 1 winner and all 9 duplicate replays return the winner's dispatch_id.
5. Execution Verification:
   Run tests:
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_creation.py -k "atomic or rollback or dispatchable or concur or supersede or duplicate"
6. Explicit verdict: APPROVE or REQUEST_CHANGES.
7. Write challenge_report.md and handoff.md in your working directory.
8. Notify orchestrator via send_message when done.
Do NOT modify production source code in src/ or migrations/.
