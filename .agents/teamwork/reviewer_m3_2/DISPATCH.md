# Dispatch Instructions for reviewer_m3_2

**Agent ID**: `reviewer_m3_2`
**Role**: Store Lifecycle & Tenancy Reviewer
**Milestone**: Phase 2 Milestone 3 — Server-Side Sessions & Invitations Lifecycle
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_2`

## Authoritative Inputs
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (Read §5, §6, §7, §8, §11, §12)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
3. Worker handoff: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_1\handoff.md`

## Mandatory Non-Negotiable Invariants
- `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` strictly enforced at all times.
- Zero player delivery; no active worker or relay.
- Plaintext tokens (sessions and invitations) are NEVER stored in PostgreSQL; only deterministic SHA-256 hashes (`hash_token()`).
- Single-use invitation consumption enforced under atomic `FOR UPDATE` lock.
- User account status transition strictly respects `fc27_users_status_guard`: `invited` -> `active`.
- Dual session timeouts: 30-minute idle expiration strictly capped at 24-hour absolute expiration.

## Review Scope
1. **Store Lifecycle & PostgreSQL Transactions**:
   - Inspect `src/identity/store.py`:
     - Invitation methods: `create_invitation`, `validate_invitation`, `consume_invitation_and_activate_user`, `revoke_invitation`.
     - Session methods: `create_session`, `get_session_by_token`, `rotate_session`, `touch_session_by_token`, `revoke_session_by_token`, `get_session_by_id`, `list_user_sessions`, `revoke_all_user_sessions`.
     - User methods: `get_user_by_id`, `suspend_user`, `list_users`.
   - Verify transaction boundaries (`conn.transaction()`), explicit row locks (`FOR UPDATE`), and event recording to `domain_events`.
   - Verify adherence to database triggers (`fc27_users_status_guard`, monotonic counters, rejection of mutations where applicable).
2. **Tenancy & Authorization Integration**:
   - Inspect `src/identity/policy.py` and `src/identity/deps.py`:
     - Verify club tenancy isolation: caller from club 101 cannot access or modify resources in club 102.
     - Verify legacy commissioner fallback and system-level tenancy.
3. **Execution & Test Verification**:
   - Execute verification tests:
     - `pytest tests/test_packaging.py tests/test_phase1_scope.py -v`
     - `pytest tests/identity/test_store.py tests/identity/test_policy.py tests/identity/test_services.py tests/identity/test_delegations.py -v`
   - Document all executed commands, test outputs, and findings in `handoff.md`.
4. **Verdict**:
   - Provide an explicit verdict: `APPROVE` or `REQUEST_CHANGES`.

Write your report to: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_2\handoff.md`.
When finished, notify me with your verdict and report path via `send_message`.


## 2026-10-04T05:00:05Z
You are reviewer_m3_2, a high-reliability reviewer agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_2
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_2\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md
Worker handoff: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_1\handoff.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md before starting work.

Your task:
Review the Store Lifecycle, Database Triggers, and Tenancy Isolation for Phase 2 Milestone 3: Server-Side Sessions & Invitations Lifecycle.
1. Inspect src/identity/store.py (invitation, session, and user lifecycle methods), transaction boundaries, row locking (FOR UPDATE), and trigger compliance (fc27_users_status_guard).
2. Inspect src/identity/policy.py and src/identity/deps.py for club tenancy isolation and legacy commissioner fallback.
3. Execute verification tests:
   - pytest tests/test_packaging.py tests/test_phase1_scope.py -v
   - pytest tests/identity/test_store.py tests/identity/test_policy.py tests/identity/test_services.py tests/identity/test_delegations.py -v
4. Document all commands, test outputs, and findings in handoff.md.
5. Provide an explicit verdict: APPROVE or REQUEST_CHANGES.
Write report to: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_2\handoff.md
When finished, notify me with your verdict and report path via send_message.
