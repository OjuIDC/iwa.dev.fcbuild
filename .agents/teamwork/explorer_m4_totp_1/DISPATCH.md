# Dispatch Instructions for explorer_m4_totp_1

**Agent ID**: `explorer_m4_totp_1`
**Role**: Staff TOTP & Secret Encryption Designer
**Milestone**: Phase 2 Milestone 4 — WebAuthn Passkeys, Staff TOTP & Bootstrap/Recovery
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_totp_1`

## Authoritative Inputs
- Authoritative Request: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (MANDATORY TO READ §9, §11, §13)
- Project Plan: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
- Database Schema: `migrations/0004_identity_foundation.sql` (`fc27_encrypted_secrets`)
- Cryptography Engine: `src/identity/crypto.py` (`encrypt_secret`, `decrypt_secret`)
- Identity Store: `src/identity/store.py`
- E2E Tests: `tests/e2e/test_phase2_tier1_features.py` (Staff TOTP features) and `tests/e2e/phase2_helpers.py`

## Mandatory Non-Negotiable Invariants
- `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` strictly enforced at all times.
- Zero player delivery; no active worker or relay.
- Plaintext TOTP seeds are NEVER stored unencrypted; MUST be encrypted via `encrypt_secret()` (MultiFernet).
- Replay protection: a TOTP code cannot be reused within the same 30-second time window (tracking `last_verified_at` or `last_used_step`).
- Drift tolerance: maximum ±1 step (30 seconds) allowed.

## Investigation & Design Tasks
1. Inspect `pyotp>=2.9` library APIs:
   - Base32 secret generation (`pyotp.random_base32()`).
   - TOTP provisioning URI generation (`totp.provisioning_uri(...)`).
   - Code verification (`totp.verify(code, valid_window=1)`).
2. Design `src/identity/totp.py`:
   - Secret generation and encryption using `encrypt_secret` from `src/identity/crypto.py`.
   - Provisioning URI generation with issuer `FC27 Dispatch` and account name.
   - Verification logic with drift tolerance (±1 step = 30s) and replay detection.
3. Design Store Persistence (`src/identity/store.py`):
   - Storage in `public.fc27_encrypted_secrets`:
     `secret_id`, `user_id`, `secret_type = 'totp_seed'`, `ciphertext`, `key_version`, `created_at`, `rotated_at`, `status = 'active'`.
   - Methods: `save_totp_secret`, `get_totp_secret`, `rotate_totp_secret`, `deactivate_totp_secret`.
4. Design API Router (`src/identity/routers/totp.py`):
   - `POST /v1/auth/totp/setup`: Authenticated staff user requests TOTP setup; generates secret, returns provisioning URI and QR image / base32 secret. Secret marked pending until verified.
   - `POST /v1/auth/totp/verify`: User submits 6-digit TOTP code; verifies code, activates secret in DB, emits `identity.totp_enrolled` domain event.
   - `POST /v1/auth/totp/authenticate`: User submits TOTP code for MFA login or step-up; verifies code, guards against replay, returns verification evidence or elevates session.
   - `GET /v1/auth/totp/status`: Returns whether TOTP is enrolled and active for user.
5. Provide exact code structures, data schemas, exception mappings, and test blueprints.
6. Deliver complete handoff report to:
   `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_totp_1\handoff.md`.
   Notify orchestrator via `send_message`.


## 2026-10-04T05:47:52Z
[Message] sender=25210876-a9ad-4f78-a87c-dec88e86edb9 priority=MESSAGE_PRIORITY_HIGH
You are explorer_m4_totp_1, a Read-only exploration agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_totp_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_totp_1\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md and c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_totp_1\DISPATCH.md before starting work.

Your task:
Investigate and design the Staff TOTP Engine and Secret Encryption for Phase 2 Milestone 4:
1. Inspect pyotp>=2.9 library APIs and src/identity/crypto.py (encrypt_secret / decrypt_secret).
2. Design src/identity/totp.py: secret generation, MultiFernet encryption, provisioning URI, drift tolerance (±1 step), replay detection.
3. Design store methods in src/identity/store.py for public.fc27_encrypted_secrets.
4. Design src/identity/routers/totp.py endpoints: /setup, /verify, /authenticate, /status.
5. Provide exact code structures, data schemas, exception mappings, and test blueprints.
6. Deliver complete handoff report to:
   c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_totp_1\handoff.md
When finished, notify me with your report path via send_message.
