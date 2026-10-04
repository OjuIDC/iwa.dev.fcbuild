# Dispatch: Milestone 1 Store & Decision Explorer

## Identity
- Archetype: teamwork_preview_explorer
- Working Directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_store_1

## Mandatory Inputs
You MUST read:
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (including Section 1A patch).
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`.
3. `src/approval_store.py`, `src/decision.py`, `src/events.py`.
4. `docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md` (Task 4).

## Objective
Analyze and design the exact Python changes for `src/approval_store.py` and `src/decision.py`:
1. `ApprovalStore`:
   - `__init__(self, conninfo, *, request_ttl=timedelta(hours=72))`
   - `complete_run()`: Insert into `approval_requests`, update thread, record `approval.created` via `record_event`.
   - `issue_challenge()`: Call `expire_due()`, lock request `FOR SHARE`, check expiry.
   - `apply_decision()`: Strict lock ordering (nonce -> request `FOR UPDATE` -> thread), check revocation and expiry, check `fc27_approval_inconsistencies`, safe idempotent replay (return committed decision instead of 409 Conflict!), write `approval_audit` with `proof_type`, `correlation_id`, `approval_request_id`, record `approval.decided`, atomic commit.
   - `expire_due()`: Proactive expiry in independent transactions, lock nonces, lock request, lock thread, update to `expired`, revoke nonces, record `approval.expired`.
   - `pending_approvals()`: Query inbox joining `approval_requests` and `graph_threads`.
   - Section 1A authority validation: Validate `actor_type`, `authority_basis` (`primary`), reject unsupported types/bases in Phase 1.
2. `src/decision.py`:
   - Eliminate legacy `"dispatch"` dictionary assignment in `apply_decision_to_state()`.

## Scope Boundaries
- Read-only exploration and design. Do NOT modify source files.
- Deliver exact implementation plan and function designs.

## Output Requirements
- Write `store_analysis.md` in your working directory.
- Write `handoff.md` following the Handoff Protocol.
- Notify orchestrator with `send_message`.


## 2026-10-03T14:15:37Z
Sender: 7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b
Message:
You are explorer_m1_store_1, a teamwork_preview_explorer agent.
Your working directory is: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_store_1

Read your instructions in:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_store_1\DISPATCH.md

MANDATORY: You MUST read:
1. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md (including Section 1A patch)
2. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md
3. src/approval_store.py, src/decision.py, src/events.py
4. docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md (Task 4)

Task:
Perform a deep technical exploration and detailed implementation design for src/approval_store.py and src/decision.py for Milestone 1:
- complete_run(): approval_requests creation with TTL, event emission.
- issue_challenge(): expire_due call, lock order.
- apply_decision(): lock order (nonce -> request -> thread), inconsistency check, safe idempotent replay (return committed decision, do NOT fail with 409), audit record with correlation/request ID, event emission, Section 1A authority validation (reject unsupported in Phase 1).
- expire_due(): proactive expiry transaction, revoke nonces, emit approval.expired.
- pending_approvals(): inbox query.
- src/decision.py: remove legacy dispatch dictionary.
Write store_analysis.md and handoff.md in your working directory, then notify orchestrator with send_message.
Do NOT modify repository files.
