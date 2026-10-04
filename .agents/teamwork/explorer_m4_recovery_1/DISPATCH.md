# Dispatch Instructions for explorer_m4_recovery_1

**Agent ID**: `explorer_m4_recovery_1`
**Role**: Commissioner Bootstrap & Recovery Designer
**Milestone**: Phase 2 Milestone 4 — WebAuthn Passkeys, Staff TOTP & Bootstrap/Recovery
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_recovery_1`

## Authoritative Inputs
- Authoritative Request: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (MANDATORY TO READ §9.1, §9.2, §12)
- Project Plan: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
- Database Schema: `migrations/0004_identity_foundation.sql` (`fc27_recovery_codes`, `fc27_reject_mutation` trigger)
- Cryptography Engine: `src/identity/crypto.py` (`hash_recovery_code`, `verify_recovery_code`)
- Identity Store: `src/identity/store.py`
- E2E Tests: `tests/e2e/test_phase2_tier1_features.py` (Recovery & Bootstrap features) and `tests/e2e/phase2_helpers.py`

## Mandatory Non-Negotiable Invariants
- `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` strictly enforced at all times.
- Zero player delivery; no active worker or relay.
- Bootstrap lock: One-time ceremony; once completed, permanently locked. Any second bootstrap attempt MUST fail closed with HTTP 409 Conflict.
- Recovery codes are NEVER stored in plaintext; stored strictly as Argon2id hashes (`hash_recovery_code()`).
- Database immutability trigger `fc27_reject_mutation`: updates and deletes on `fc27_recovery_codes` are rejected by PostgreSQL! Consumption MUST be handled by recording usage (e.g. tracking used code ID or separate consumption table / events).
- Single-use recovery: a code cannot be reused.

## Investigation & Design Tasks
1. Inspect Bootstrap Requirements:
   - Initial Commissioner initialization: email, gamertag display, initial passkey registration options, generating initial recovery codes set (e.g. 10 codes).
   - Locking mechanism: `public.fc27_bootstrap_locks` or dedicated bootstrap lock row. Verify lock is atomic and unbypassable.
2. Inspect Recovery Code Requirements:
   - High-entropy code generation: e.g. 16–24 alphanumeric or hyphenated characters (e.g. `fc27-xxxx-xxxx-xxxx`).
   - Argon2id hashing via `src/identity/crypto.py:hash_recovery_code`.
   - Persistence in `public.fc27_recovery_codes` (`code_id`, `user_id`, `code_hash`, `created_at`, `used_at`).
   - Note on `fc27_reject_mutation`: trigger prevents `UPDATE` on `fc27_recovery_codes`. Inspect how `used_at` or consumption was defined in `migrations/0004_identity_foundation.sql` lines 120–150! If the trigger rejects UPDATE on `fc27_recovery_codes`, design the clean consumption strategy (e.g. `fc27_consumed_recovery_codes` or inspect the exact trigger conditions in migration 0004).
3. Design `src/identity/recovery.py`:
   - Generating recovery code batch (e.g. 10 codes).
   - Verifying recovery code against user's stored hashes.
   - Issuing emergency commissioner session / credential reset upon successful recovery.
   - Domain event logging (`identity.recovery_initiated`, `identity.recovery_completed`, `identity.recovery_failed`).
4. Design API Router (`src/identity/routers/recovery.py` and bootstrap endpoints):
   - `GET /v1/auth/bootstrap/status`: Returns whether bootstrap has been completed (`{"bootstrapped": bool}`).
   - `POST /v1/auth/bootstrap/init`: Starts bootstrap ceremony (if not locked); returns initial passkey registration options.
   - `POST /v1/auth/bootstrap/complete`: Completes bootstrap with initial passkey verification, creates Commissioner user, provisions role, generates & returns recovery codes (plaintext shown ONCE), locks bootstrap permanently.
   - `POST /v1/auth/recovery/verify`: Submits recovery code; verifies Argon2id hash, records consumption, issues recovery session.
5. Provide exact code structures, data schemas, exception mappings, and test blueprints.
6. Deliver complete handoff report to:
   `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_recovery_1\handoff.md`.
   Notify orchestrator via `send_message`.


## 2026-10-04T05:47:52Z
[Message] timestamp=2026-10-04T05:47:52Z sender=25210876-a9ad-4f78-a87c-dec88e86edb9 priority=MESSAGE_PRIORITY_HIGH content=You are explorer_m4_recovery_1, a Read-only exploration agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_recovery_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_recovery_1\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md
