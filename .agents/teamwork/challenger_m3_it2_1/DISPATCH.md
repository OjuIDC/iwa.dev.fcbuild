# Dispatch: challenger_m3_it2_1

## Identity
- Role: Milestone 3 Iteration 2 Challenger 1 (State Machine Transitions, Concurrency & Phase 1 Invariants)
- Working Directory: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_it2_1`
- Parent: Orchestrator Phase 1 (`7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b`)

## Mandatory Inputs to Read First
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (authoritative user request + Section 1A patch)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`
3. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_2\handoff.md`
4. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_2\challenge_report.md` (previous gate failure report)
5. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_it2_1\handoff.md`
6. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_it2_2\handoff.md`

## Challenge Scope & Adversarial Attacks
Adversarially challenge the single-write-path transition function (`public.fc27_dispatch_transition`) and state machine:
1. **Phase 1 Delivery Block Bypass Attacks**:
   - Probe transitions with `to_status = 'delivered'` across all roles (`fc27_owner`, `fc27_app`, `fc27_dispatch`), actor kinds (`commissioner`, `system`, `player_agent`), and starting statuses (`queued`, `running`, `paused`).
   - Verify all attempts fail closed unconditionally with SQLSTATE `FC002` and zero state change.
2. **Transition Rule Matrix & Actor Validation Attacks**:
   - Probe invalid state transitions (e.g. `queued -> cancelled` by unauthorized actor, `delivered -> queued`, skips like `queued -> paused`). Verify `FC002`.
   - Probe invalid actors / missing session configuration (`fc27.actor`). Verify `FC003`.
   - Probe pause / cancel transitions without mandatory reason. Verify `FC005`.
3. **Commissioner Retry Edge**:
   - Verify permitted commissioner transition `failed -> queued` succeeds without requiring an active `approval_audit` record in the current transaction.
   - Verify commissioner `superseded` transition strictly requires an active in-transaction `approval_audit` record (`FC004` if missing).
4. **Concurrency & Optimistic Locking**:
   - Verify `SELECT FOR UPDATE` serializes concurrent transitions on the same dispatch row and prevents lost updates.
5. **Execution Verification**:
   Run tests:
   ```pwsh
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_transition.py
   ```
6. **Deliverables**:
   - Write `challenge_report.md` and `handoff.md` in your working directory.
   - Explicit verdict: `APPROVE` or `REQUEST_CHANGES`.
   - Notify orchestrator via `send_message`.
   - Do NOT modify production source code in `src/` or `migrations/`.


## 2026-10-03T18:40:33Z
You are challenger_m3_it2_1, a teamwork_preview_challenger agent.
Your working directory is: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_it2_1

Read your instructions in:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_it2_1\DISPATCH.md

MANDATORY: You MUST read:
1. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md (authoritative user request + Section 1A patch)
2. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md
3. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_2\handoff.md
4. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_2\challenge_report.md
5. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_it2_1\handoff.md
6. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_it2_2\handoff.md

Task:
Adversarially challenge the single-write-path transition function (public.fc27_dispatch_transition) and state machine:
1. Phase 1 Delivery Block Bypass Attacks:
   - Probe transitions with to_status = 'delivered' across all roles (fc27_owner, fc27_app, fc27_dispatch), actor kinds (commissioner, system, player_agent), and starting statuses (queued, running, paused).
   - Verify all attempts fail closed unconditionally with SQLSTATE FC002 and zero state change.
2. Transition Rule Matrix & Actor Validation Attacks:
   - Probe invalid state transitions (e.g. queued -> cancelled by unauthorized actor, delivered -> queued, skips like queued -> paused). Verify FC002.
   - Probe invalid actors / missing session configuration (fc27.actor). Verify FC003.
   - Probe pause / cancel transitions without mandatory reason. Verify FC005.
3. Commissioner Retry Edge:
   - Verify permitted commissioner transition failed -> queued succeeds without requiring an active approval_audit record in the current transaction.
   - Verify commissioner superseded transition strictly requires an active in-transaction approval_audit record (FC004 if missing).
4. Concurrency & Optimistic Locking:
   - Verify SELECT FOR UPDATE serializes concurrent transitions on the same dispatch row and prevents lost updates.
5. Execution Verification:
   Run tests:
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_transition.py
6. Formulate explicit verdict: APPROVE or REQUEST_CHANGES.
7. Write challenge_report.md and handoff.md in your working directory.
8. Notify orchestrator via send_message when done.
Do NOT modify production source code in src/ or migrations/.
