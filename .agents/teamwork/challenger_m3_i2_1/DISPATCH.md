# Dispatch Instructions for challenger_m3_i2_1

**Agent ID**: `challenger_m3_i2_1`
**Role**: Session Dual-Expiry & Lifecycle Challenger (Iteration 2)
**Milestone**: Phase 2 Milestone 3 — Server-Side Sessions & Invitations Lifecycle (Iteration 2 Verification)
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_i2_1`

## Inputs
- Authoritative Request: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (MANDATORY TO READ)
- Project Plan: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
- Previous Iteration Audit Report: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m3_1\handoff.md`
- Worker Handoff: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_2\handoff.md`

## Adversarial Stress Tasks
1. Dual-Expiry & Touch:
   - Verify idle expiration (30m) fails closed with 401 even within 24h absolute.
   - Verify absolute expiration (24h) fails closed with 401.
   - Verify touch strictly caps `idle_expires_at <= absolute_expires_at`.
2. Session Rotation & Concurrency:
   - Stress-test concurrent rotation replay: verify second concurrent rotation attempt fails with 409 Conflict.
   - Verify unproven elevation to `step_up_passkey` on `/rotate` fails with 403 Forbidden.
3. Session Ownership & Isolation:
   - Verify non-commissioner/non-delegate user cannot revoke or view another user's session (assert 403 Forbidden).
   - Verify player cannot revoke or view another player's or staff session (assert 403 Forbidden).
4. Run stress tests and packaging checks:
   - `pytest tests/test_packaging.py tests/test_phase1_scope.py -v`
   - `pytest tests/identity/test_routers_sessions.py tests/identity/test_session_lifecycle_adversarial.py -v`
5. Provide explicit verdict: APPROVE or REQUEST_CHANGES.
6. Deliver complete handoff report to:
   `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_i2_1\handoff.md`.
   Notify orchestrator via `send_message`.

## 2026-10-04T05:35:56Z
You are challenger_m3_i2_1, an adversarial verifier agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_i2_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_i2_1\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md
Worker handoff: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_2\handoff.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md and c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_i2_1\DISPATCH.md before starting work.

Your task:
Adversarially stress-test session dual-expiry, rotation concurrency, and ownership isolation for Phase 2 Milestone 3 Iteration 2:
1. Dual-Expiry & Touch: verify idle expiration (30m) fails closed with 401; verify absolute expiration (24h) fails closed with 401; verify touch strictly caps idle_expires_at <= absolute_expires_at.
2. Rotation & Concurrency: stress-test concurrent rotation replay and verify second attempt fails with 409 Conflict. Verify unproven step_up_passkey elevation fails with 403 Forbidden.
3. Ownership Isolation: verify non-commissioner/non-delegate cannot revoke or view another user's session (assert 403 Forbidden). Verify player cannot revoke or view other sessions.
4. Execute stress tests and packaging checks:
   - pytest tests/test_packaging.py tests/test_phase1_scope.py -v
   - pytest tests/identity/test_routers_sessions.py tests/identity/test_session_lifecycle_adversarial.py -v
5. Document all commands, results, and explicit verdict (APPROVE or REQUEST_CHANGES) in:
   c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_i2_1\handoff.md
When finished, notify me with your verdict and report path via send_message.
