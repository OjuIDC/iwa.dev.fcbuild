# Dispatch Instructions for explorer_m4_webauthn_1

**Agent ID**: `explorer_m4_webauthn_1`
**Role**: WebAuthn Passkey Engine & API Designer
**Milestone**: Phase 2 Milestone 4 — WebAuthn Passkeys, Staff TOTP & Bootstrap/Recovery
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_webauthn_1`

## Authoritative Inputs
- Authoritative Request: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (MANDATORY TO READ §9, §10, §12)
- Project Plan: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
- Database Schema: `migrations/0004_identity_foundation.sql` (`fc27_credentials`)
- Cryptography Engine: `src/identity/crypto.py`
- Identity Store: `src/identity/store.py`
- E2E Tests: `tests/e2e/test_phase2_tier1_features.py` (Passkey features) and `tests/e2e/phase2_helpers.py`

## Mandatory Non-Negotiable Invariants
- `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` strictly enforced at all times.
- Zero player delivery; no active worker or relay.
- No agent-human impersonation (services cannot create or authenticate passkeys on behalf of humans).
- Cryptographic verification must use real `webauthn>=2.5` library functions (no dummy or facade signature verifiers).

## Investigation & Design Tasks
1. Inspect `webauthn>=2.5` library APIs:
   - Registration: `generate_registration_options`, `verify_registration_response`.
   - Authentication: `generate_authentication_options`, `verify_authentication_response`.
   - Handling RP ID (e.g. `localhost` or configured domain), origin (e.g. `http://localhost:8000`), user verification requirements (`preferred` / `required`).
2. Design `src/identity/webauthn.py`:
   - Challenge generation, caching/persistence (transient in-memory or redis/db with short TTL e.g. 5m).
   - Credential creation options & verification.
   - Authentication assertion options & verification.
   - Signature counter verification (anti-cloning: new counter must be strictly greater than stored counter if non-zero).
3. Design Store Persistence (`src/identity/store.py`):
   - Saving credential to `public.fc27_credentials` (`credential_id`, `user_id`, `public_key`, `sign_count`, `transports`, `aaguid`, `created_at`, `last_used_at`).
   - Querying credentials by `credential_id` or `user_id`.
   - Updating `sign_count` and `last_used_at` upon successful authentication.
4. Design API Router (`src/identity/routers/passkeys.py`):
   - `POST /v1/auth/passkeys/register/options`: Authenticated human user requests registration options.
   - `POST /v1/auth/passkeys/register/verify`: Verifies registration response, stores credential in DB, emits `identity.credential_registered` domain event.
   - `POST /v1/auth/passkeys/authenticate/options`: Requests authentication options (for login or step-up).
   - `POST /v1/auth/passkeys/authenticate/verify`: Verifies assertion response, updates sign count, issues/updates session or returns authentication evidence token.
5. Provide exact code structures, data schemas, exception mappings, and test blueprints.
6. Deliver complete handoff report to:
   `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_webauthn_1\handoff.md`.
   Notify orchestrator via `send_message`.


## 2026-10-04T05:47:52Z

[Message] timestamp=2026-10-04T05:47:52Z sender=25210876-a9ad-4f78-a87c-dec88e86edb9 priority=MESSAGE_PRIORITY_HIGH content=You are explorer_m4_webauthn_1, a Read-only exploration agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_webauthn_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_webauthn_1\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md and c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_webauthn_1\DISPATCH.md before starting work.

Your task:
Investigate and design the WebAuthn Passkey Engine and API Router for Phase 2 Milestone 4:
1. Inspect webauthn>=2.5 library APIs for options generation and registration/assertion verification.
2. Design src/identity/webauthn.py: challenge generation/caching, credential creation options/verification, authentication assertion options/verification, anti-cloning signature counter check.
3. Design store methods in src/identity/store.py for public.fc27_credentials.
4. Design src/identity/routers/passkeys.py endpoints: /options and /verify for registration and authentication.
5. Provide exact code structures, data schemas, exception mappings, and test blueprints.
6. Deliver complete handoff report to:
   c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_webauthn_1\handoff.md
When finished, notify me with your report path via send_message.
