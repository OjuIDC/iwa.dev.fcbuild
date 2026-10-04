# Dispatch Instructions for challenger_m4_2

**Agent ID**: `challenger_m4_2`
**Role**: Recovery Immutability & Replay Challenger
**Milestone**: Phase 2 Milestone 4 — WebAuthn Passkeys, Staff TOTP & Bootstrap/Recovery
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m4_2`

## Authoritative Inputs
- Authoritative Request: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (MANDATORY TO READ)
- Project Plan: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
- Worker Handoff: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m4_1\handoff.md`

## Adversarial Stress Tasks
1. Bootstrap Lock Immutability:
   - Verify that once bootstrapped, subsequent calls to `POST /v1/auth/bootstrap` fail closed with HTTP 409 Conflict.
   - Verify that attempts to UPDATE or DELETE rows in `public.fc27_bootstrap_locks` trigger `fc27_reject_mutation()` and fail closed.
   - Verify that tampered bootstrap tokens fail with 401/403.
2. Recovery Code Single-Use & Immutability:
   - Verify that an already-consumed recovery code cannot be reused (fails with 400/401/404).
   - Verify that attempts to unconsume (`consumed_at = NULL`), update other attributes, or delete rows in `public.fc27_recovery_codes` trigger `fc27_guard_recovery_code_consumption` / `fc27_reject_mutation()` and fail closed.
   - Verify zero plaintext recovery codes are stored in DB (Argon2id hashes only).
3. Scope Guards:
   - `pytest tests/test_packaging.py tests/test_phase1_scope.py -v`
   - `pytest tests/identity/test_recovery.py tests/identity/test_bootstrap.py -v`
4. Provide explicit verdict: APPROVE or REQUEST_CHANGES.
5. Deliver complete handoff report to:
   `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m4_2\handoff.md`.
   Notify orchestrator via `send_message`.

## 2026-10-04T06:14:13Z
[Message] timestamp=2026-10-04T06:14:13Z sender=25210876-a9ad-4f78-a87c-dec88e86edb9 priority=MESSAGE_PRIORITY_HIGH content=You are challenger_m4_2, an adversarial verifier agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m4_2
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m4_2\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md
Worker handoff: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m4_1\handoff.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md and c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m4_2\DISPATCH.md before starting work.

Your task:
Adversarially stress-test recovery code immutability, replay prevention, and bootstrap locks for Phase 2 Milestone 4:
1. Bootstrap Lock Immutability:
   - Verify that once bootstrapped, subsequent calls to POST /v1/auth/bootstrap fail closed with HTTP 409 Conflict.
   - Verify that attempts to UPDATE or DELETE rows in public.fc27_bootstrap_locks trigger fc27_reject_mutation() and fail closed.
   - Verify that tampered bootstrap tokens fail with 401/403.
2. Recovery Code Single-Use & Immutability:
   - Verify that an already-consumed recovery code cannot be reused (fails with 400/401/404).
   - Verify that attempts to unconsume (consumed_at = NULL), update other attributes, or delete rows in public.fc27_recovery_codes trigger fc27_guard_recovery_code_consumption / fc27_reject_mutation() and fail closed.
   - Verify zero plaintext recovery codes are stored in DB (Argon2id hashes only).
3. Scope Guards:
   - pytest tests/test_packaging.py tests/test_phase1_scope.py -v
   - pytest tests/identity/test_recovery.py tests/identity/test_bootstrap.py -v
4. Document all commands, results, and explicit verdict (APPROVE or REQUEST_CHANGES) in:
   c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m4_2\handoff.md
When finished, notify me with your verdict and report path via send_message.
