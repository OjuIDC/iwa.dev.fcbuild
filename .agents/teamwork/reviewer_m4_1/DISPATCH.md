# Dispatch Instructions for reviewer_m4_1

**Agent ID**: `reviewer_m4_1`
**Role**: WebAuthn & TOTP Reviewer
**Milestone**: Phase 2 Milestone 4 — WebAuthn Passkeys, Staff TOTP & Bootstrap/Recovery
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m4_1`

## Authoritative Inputs
- Authoritative Request: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (MANDATORY TO READ)
- Project Plan: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
- Worker Handoff: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m4_1\handoff.md`

## Review Tasks
1. Inspect `src/identity/webauthn.py` and `src/identity/routers/passkeys.py`:
   - Real `webauthn>=2.5` usage, options generation, registration and authentication verification.
   - Challenge generation and single-use atomic consumption via `store.consume_challenge`.
   - Anti-cloning counter check: new sign counter must be strictly greater than stored counter if non-zero.
   - Route contracts, Pydantic schemas, and error mappings.
2. Inspect `src/identity/totp.py` and `src/identity/routers/totp.py`:
   - MultiFernet encryption of TOTP seeds via `encrypt_secret` / `decrypt_secret`.
   - Drift tolerance: $\pm 1$ step (30 seconds).
   - Replay protection: candidate step strictly greater than last verified step.
   - Anti-brute-force rate limiting: 5 failed attempts -> 429 Too Many Requests with `Retry-After`.
3. Inspect `src/identity/policy.py`:
   - Verify `TOTP_ENROLL`, `TOTP_VERIFY`, `TOTP_REVOKE` granted to Commissioner and staff, not players or services.
4. Execute verification tests:
   - `pytest tests/test_packaging.py tests/test_phase1_scope.py -v`
   - `pytest tests/identity/test_webauthn.py tests/identity/test_totp.py -v`
5. Provide explicit verdict: APPROVE or REQUEST_CHANGES.
6. Deliver complete handoff report to:
   `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m4_1\handoff.md`.
   Notify orchestrator via `send_message`.


## 2026-10-04T06:14:13Z
You are reviewer_m4_1, a high-reliability reviewer agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m4_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m4_1\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md
Worker handoff: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m4_1\handoff.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md and c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m4_1\DISPATCH.md before starting work.

Your task:
Review the WebAuthn Passkeys and Staff TOTP implementation for Phase 2 Milestone 4:
1. Inspect src/identity/webauthn.py and src/identity/routers/passkeys.py:
   - Real webauthn>=2.5 usage, options generation, registration and assertion verification.
   - Challenge generation and single-use atomic consumption via store.consume_challenge.
   - Anti-cloning counter check: new sign counter must be strictly greater than stored counter if non-zero.
   - Route contracts, Pydantic schemas, and error mappings.
2. Inspect src/identity/totp.py and src/identity/routers/totp.py:
   - MultiFernet encryption of TOTP seeds via encrypt_secret / decrypt_secret.
   - Drift tolerance: ±1 step (30 seconds).
   - Replay protection: candidate step strictly greater than last verified step.
   - Anti-brute-force rate limiting: 5 failed attempts -> 429 Too Many Requests with Retry-After.
3. Inspect src/identity/policy.py:
   - Verify TOTP_ENROLL, TOTP_VERIFY, TOTP_REVOKE granted to Commissioner and staff, not players or services.
4. Execute verification tests:
   - pytest tests/test_packaging.py tests/test_phase1_scope.py -v
   - pytest tests/identity/test_webauthn.py tests/identity/test_totp.py -v
5. Document all commands, test outputs, findings, and explicit verdict (APPROVE or REQUEST_CHANGES) in:
   c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m4_1\handoff.md
When finished, notify me with your verdict and report path via send_message.
