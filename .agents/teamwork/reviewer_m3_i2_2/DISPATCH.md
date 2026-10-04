# Dispatch Instructions for reviewer_m3_i2_2

**Agent ID**: `reviewer_m3_i2_2`
**Role**: Store Lifecycle & Tenancy Reviewer (Iteration 2)
**Milestone**: Phase 2 Milestone 3 — Server-Side Sessions & Invitations Lifecycle (Iteration 2 Verification)
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_i2_2`

## Inputs
- Authoritative Request: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (MANDATORY TO READ)
- Project Plan: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
- Previous Iteration Audit Report: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m3_1\handoff.md`
- Worker Handoff: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_2\handoff.md`

## Review Tasks
1. Inspect `src/identity/store.py`:
   - `create_session`: accepts `club_id` and `**kwargs`, forwards `scope_club_id=club_id` to audit events, and includes `club_id` in returned dict.
   - `rotate_session`: atomic concurrency locking with `AND revoked_at IS NULL` and `res.rowcount == 0` check raising `IdentityStoreError(409)`.
   - `assign_user_role`: alias method exists and delegates to `assign_role`.
   - Single-use invitation consumption under `FOR UPDATE` lock.
2. Inspect `src/identity/policy.py`:
   - Step 5 session object-level isolation: prevents unauthorized session view/revocation across users.
3. Execute verification tests:
   - `pytest tests/test_packaging.py tests/test_phase1_scope.py -v`
   - `pytest tests/identity/test_store.py tests/identity/test_policy.py tests/identity/test_delegations.py -v`
4. Provide explicit verdict: APPROVE or REQUEST_CHANGES.
5. Deliver complete handoff report to:
   `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_i2_2\handoff.md`.
   Notify orchestrator via `send_message`.


## 2026-10-04T05:35:56Z
You are reviewer_m3_i2_2, a high-reliability reviewer agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_i2_2
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_i2_2\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md
Worker handoff: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_2\handoff.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md and c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_i2_2\DISPATCH.md before starting work.

Your task:
Review the Store Lifecycle, Concurrency Locking, and Tenancy Isolation for Phase 2 Milestone 3 Iteration 2:
1. Inspect src/identity/store.py:
   - create_session supports club_id and **kwargs, forwards scope_club_id to audit events, and returns club_id.
   - rotate_session uses atomic concurrency locking with AND revoked_at IS NULL and checks res.rowcount == 0 to raise 409 Conflict.
   - assign_role defaults actor="system" and assign_user_role alias delegates cleanly.
2. Inspect src/identity/policy.py:
   - Step 5 session object isolation: SESSION_VIEW, SESSION_REVOKE, SESSION_REVOKE_ALL enforce ownership (str(owner_user_id) == str(principal.user_id)), permitting only Commissioner or Delegate for cross-user management.
3. Execute verification tests:
   - pytest tests/test_packaging.py tests/test_phase1_scope.py -v
   - pytest tests/identity/test_store.py tests/identity/test_policy.py tests/identity/test_delegations.py -v
4. Document all commands, test outputs, findings, and explicit verdict (APPROVE or REQUEST_CHANGES) in:
   c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_i2_2\handoff.md
When finished, notify me with your verdict and report path via send_message.
