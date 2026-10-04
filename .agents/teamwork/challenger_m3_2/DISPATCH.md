# Dispatch Instructions for challenger_m3_2

**Agent ID**: `challenger_m3_2`
**Role**: Invitation Lifecycle & State Machine Challenger
**Milestone**: Phase 2 Milestone 3 — Server-Side Sessions & Invitations Lifecycle
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_2`

## Authoritative Inputs
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (Read §5, §6, §7, §8, §11, §12)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
3. Worker handoff: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_1\handoff.md`

## Mandatory Non-Negotiable Invariants
- `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` strictly enforced at all times.
- Zero player delivery; no active worker or relay.
- Plaintext tokens are NEVER stored in PostgreSQL; only deterministic SHA-256 hashes (`hash_token()`).
- Single-use invitation consumption enforced under atomic `FOR UPDATE` lock.
- User account status transition strictly respects `fc27_users_status_guard`: `invited` -> `active`.

## Adversarial Verification Tasks
Empirically stress-test the invitation and state machine subsystem:
1. **Token Security & Storage**:
   - Verify that plaintext tokens are generated with 64 hex characters (32 bytes entropy).
   - Inspect PostgreSQL database to verify that ONLY SHA-256 digests (`^[0-9a-f]{64}$`) are stored in `fc27_invitations.token_hash`. Zero plaintext tokens.
2. **Replay & Single-Use Protection**:
   - Attempt repeated consumption of the same invitation token. Verify that second attempt fails with HTTP 409 Conflict.
   - Attempt consumption of expired invitation token. Verify fail-closed with HTTP 410 Gone.
   - Attempt consumption of revoked invitation token. Verify fail-closed with HTTP 410 Gone.
   - Attempt revoking an already-revoked invitation. Verify fail-closed with HTTP 409 Conflict.
3. **State Machine & Role Boundaries**:
   - Verify that consuming an invitation successfully transitions user from `invited` to `active` and assigns the target role in `fc27_user_roles`.
   - Verify that attempting to invite or activate a `disabled` account fails closed with HTTP 403 Forbidden.
   - Verify that Staff cannot invite Commissioner or Commissioner Delegate.
   - Verify that Player cannot issue invitations (HTTP 403).
   - Verify that cross-club invitation creation fails closed (HTTP 403).
4. **Packaging & Scope Invariants**:
   - Run `pytest tests/test_packaging.py tests/test_phase1_scope.py -v` to ensure zero drift and invariant enforcement (`DISPATCH_ENABLED=false`).
5. **Execution**:
   - Write and execute adversarial test scripts, run `pytest tests/identity/test_routers_invitations.py tests/identity/test_routers_users.py -v`.
   - Document commands, test execution logs, and empirical evidence in `handoff.md`.
6. **Verdict**:
   - Provide an explicit verdict: `APPROVE` or `REQUEST_CHANGES`.

Write your report to: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_2\handoff.md`.
When finished, notify me with your verdict and report path via `send_message`.


## 2026-10-04T05:00:05Z
You are challenger_m3_2, an adversarial verifier agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_2
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_2\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md
Worker handoff: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_1\handoff.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md before starting work.

Your task:
Empirically stress-test invitation token hashing, single-use, and state machine invariants for Phase 2 Milestone 3.
1. Token Security & Storage: verify 64-hex char plaintext tokens; verify database strictly stores SHA-256 digests in fc27_invitations.token_hash (0 plaintext tokens).
2. Replay & Single-Use: verify concurrent/repeated consumption attempts fail with 409 Conflict; verify expired tokens fail with 410; verify revoked tokens fail with 410; verify revoking already-revoked invitation fails with 409.
3. State Machine & Boundaries: verify consumption transitions invited -> active and assigns role; verify disabled accounts cannot be invited or activated (403); verify staff cannot invite commissioner/delegate; verify player cannot invite; verify cross-club rejection.
4. Packaging & Scope Invariants: verify pytest tests/test_packaging.py tests/test_phase1_scope.py -v passes (DISPATCH_ENABLED=false).
5. Write and execute stress tests, document in handoff.md.
6. Provide an explicit verdict: APPROVE or REQUEST_CHANGES.
Write report to: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_2\handoff.md
When finished, notify me with your verdict and report path via send_message.
