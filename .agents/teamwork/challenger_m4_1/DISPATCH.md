# Dispatch Instructions for challenger_m4_1

**Agent ID**: `challenger_m4_1`
**Role**: Passkey Anti-Cloning & Challenge Challenger
**Milestone**: Phase 2 Milestone 4 — WebAuthn Passkeys, Staff TOTP & Bootstrap/Recovery
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m4_1`

## Authoritative Inputs
- Authoritative Request: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (MANDATORY TO READ)
- Project Plan: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
- Worker Handoff: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m4_1\handoff.md`

## Adversarial Stress Tasks
1. Passkey Anti-Cloning:
   - Verify that an assertion with equal or lower sign counter is rejected (anti-cloning / replay protection).
   - Verify that PostgreSQL trigger `fc27_guard_webauthn_counter` prevents decreasing `sign_counter`.
2. Challenge Lifecycle & Single-Use:
   - Verify challenge replay fails closed (challenge cannot be consumed twice).
   - Verify expired challenges fail closed.
   - Verify cross-user challenge mismatch fails closed.
3. TOTP Replay & Drift:
   - Verify TOTP code cannot be replayed in the same 30-second window.
   - Verify codes outside $\pm 1$ step drift fail closed (HTTP 400).
   - Verify 5 consecutive failed attempts trigger HTTP 429 rate limit.
4. Anti-Impersonation:
   - Verify service tokens calling passkey or TOTP endpoints fail with 403 AgentImpersonationError.
5. Scope Guards:
   - `pytest tests/test_packaging.py tests/test_phase1_scope.py -v`
   - `pytest tests/identity/test_webauthn.py tests/identity/test_totp.py -v`
6. Provide explicit verdict: APPROVE or REQUEST_CHANGES.
7. Deliver complete handoff report to:
   `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m4_1\handoff.md`.
   Notify orchestrator via `send_message`.


## 2026-10-04T06:14:13Z
From: 25210876-a9ad-4f78-a87c-dec88e86edb9 (parent)
Priority: MESSAGE_PRIORITY_HIGH

You are challenger_m4_1, an adversarial verifier agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m4_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m4_1\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md
Worker handoff: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m4_1\handoff.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md and c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m4_1\DISPATCH.md before starting work.

Your task:
Adversarially stress-test Passkey anti-cloning, challenge lifecycle, and TOTP invariants for Phase 2 Milestone 4:
1. Passkey Anti-Cloning:
   - Verify that an assertion with equal or lower sign counter is rejected (anti-cloning / replay protection).
   - Verify that PostgreSQL trigger fc27_guard_webauthn_counter prevents decreasing sign_counter.
2. Challenge Lifecycle & Single-Use:
   - Verify challenge replay fails closed (challenge cannot be consumed twice).
   - Verify expired challenges fail closed.
   - Verify cross-user challenge mismatch fails closed.
3. TOTP Replay & Drift:
   - Verify TOTP code cannot be replayed in the same 30-second window.
   - Verify codes outside ±1 step drift fail closed (HTTP 400).
   - Verify 5 consecutive failed attempts trigger HTTP 429 rate limit.
4. Anti-Impersonation:
   - Verify service tokens calling passkey or TOTP endpoints fail with 403 AgentImpersonationError.
5. Scope Guards:
   - pytest tests/test_packaging.py tests/test_phase1_scope.py -v
   - pytest tests/identity/test_webauthn.py tests/identity/test_totp.py -v
6. Document all commands, results, and explicit verdict (APPROVE or REQUEST_CHANGES) in:
   c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m4_1\handoff.md
When finished, notify me with your verdict and report path via send_message.
