# Dispatch Instructions for challenger_m3_i2_2

**Agent ID**: `challenger_m3_i2_2`
**Role**: Invitation Lifecycle & State Machine Challenger (Iteration 2)
**Milestone**: Phase 2 Milestone 3 — Server-Side Sessions & Invitations Lifecycle (Iteration 2 Verification)
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_i2_2`

## Inputs
- Authoritative Request: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (MANDATORY TO READ)
- Project Plan: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
- Previous Iteration Audit Report: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m3_1\handoff.md`
- Worker Handoff: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_2\handoff.md`

## Adversarial Stress Tasks
1. Token Hashing & Storage:
   - Verify plaintext tokens are 64 hex characters.
   - Verify PostgreSQL database stores strictly deterministic SHA-256 digests (`hash_token()`).
2. Single-Use Consumption & Concurrency:
   - Stress-test concurrent consumption attempts: verify replay fails with 409 Conflict.
   - Verify expired tokens fail with 410 Gone.
   - Verify revoked tokens fail with 410 Gone.
3. User Lifecycle Transitions:
   - Verify consumption transitions user from `invited` to `active` under `fc27_users_status_guard`.
   - Verify suspended accounts fail closed immediately on session touch or validation.
4. Scope Guards:
   - `pytest tests/test_packaging.py tests/test_phase1_scope.py -v`
   - `pytest tests/identity/test_routers_invitations.py tests/identity/test_routers_users.py tests/identity/test_adversarial_m3_2.py -v`
5. Provide explicit verdict: APPROVE or REQUEST_CHANGES.
6. Deliver complete handoff report to:
   `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_i2_2\handoff.md`.
   Notify orchestrator via `send_message`.


## 2026-10-04T05:35:56Z
[Message] timestamp=2026-10-04T05:35:56Z sender=25210876-a9ad-4f78-a87c-dec88e86edb9 priority=MESSAGE_PRIORITY_HIGH content=You are challenger_m3_i2_2, an adversarial verifier agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_i2_2
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_i2_2\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md
Worker handoff: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_2\handoff.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md and c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_i2_2\DISPATCH.md before starting work.

Your task:
Adversarially stress-test invitation token hashing, single-use, and state machine invariants for Phase 2 Milestone 3 Iteration 2:
1. Token Hashing & Storage: verify 64-hex char tokens; verify database strictly stores SHA-256 digests in fc27_invitations.token_hash (0 plaintext tokens in DB).
2. Replay & Single-Use: verify concurrent consumption attempts fail with 409 Conflict; verify expired tokens fail with 410 Gone; verify revoked tokens fail with 410 Gone.
3. State Machine & Boundaries: verify consumption transitions invited -> active and assigns role; verify disabled/suspended accounts fail closed immediately; verify cross-club rejection.
4. Scope Guards: verify tests/test_packaging.py tests/test_phase1_scope.py -v passes (DISPATCH_ENABLED=false).
5. Execute stress tests:
   - pytest tests/identity/test_routers_invitations.py tests/identity/test_routers_users.py tests/identity/test_adversarial_m3_2.py -v
6. Document all commands, results, and explicit verdict (APPROVE or REQUEST_CHANGES) in:
   c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_i2_2\handoff.md
When finished, notify me with your verdict and report path via send_message.
