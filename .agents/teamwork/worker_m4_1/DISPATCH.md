# Dispatch Instructions for worker_m4_1

**Agent ID**: `worker_m4_1`
**Role**: Milestone 4 Implementation Worker
**Milestone**: Phase 2 Milestone 4 — WebAuthn Passkeys, Staff TOTP & Commissioner Bootstrap/Recovery
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m4_1`

## Authoritative Inputs
- Authoritative Request: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (MANDATORY TO READ)
- Project Plan: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
- Explorer Reports & Blueprints:
  - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_webauthn_1\handoff.md`
  - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_totp_1\handoff.md`
  - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_recovery_1\handoff.md`

## Mandatory Non-Negotiable Invariants
- `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` strictly enforced at all times.
- Zero player delivery; no active worker, outbox relay, or delivery adapter.
- No agent-human impersonation (automated services cannot create, authenticate, or recover human accounts).
- Plaintext secrets (TOTP seeds, recovery codes) are NEVER stored unencrypted. TOTP seeds encrypted via `MultiFernet` (`src/identity/crypto.py`). Recovery codes stored strictly as Argon2id hashes (`hash_recovery_code()`).
- Single-use guarantees: Recovery codes, invitations, challenges, and TOTP steps cannot be reused.
- Bootstrap lock: One-time ceremony; once completed, permanently locked. Any subsequent attempt fails closed with HTTP 409 Conflict.
- Anti-cloning counter: WebAuthn signature counter must be strictly monotonic.

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

## File Ownership
You exclusively own and may create/edit:
- `migrations/0006_bootstrap_and_recovery.sql`
- `migrations/post/grants.sql`
- `src/db_migrate.py`
- `src/identity/webauthn.py`
- `src/identity/totp.py`
- `src/identity/recovery.py`
- `src/identity/store.py`
- `src/identity/policy.py`
- `src/identity/routers/passkeys.py`
- `src/identity/routers/totp.py`
- `src/identity/routers/recovery.py`
- `src/identity/routers/__init__.py`
- `src/graph_api.py`
- `tests/identity/test_webauthn.py`
- `tests/identity/test_totp.py`
- `tests/identity/test_recovery.py`
- `tests/identity/test_bootstrap.py`

## Implementation Tasks

### 1. Migrations & Database Hardening
- Create `migrations/0006_bootstrap_and_recovery.sql`:
  - `public.fc27_bootstrap_locks` with `lock_id = 'commissioner_bootstrap'`, trigger `fc27_reject_mutation()` on UPDATE/DELETE.
  - Update `domain_events_source_table_check` to include `fc27_bootstrap_locks`.
  - View `public.fc27_credentials` mapping `sign_counter AS sign_count` over `fc27_webauthn_credentials`.
- Update `migrations/post/grants.sql` (bump to Grants Policy Version 8) and `src/db_migrate.py`:
  - Grant `SELECT, INSERT` on `fc27_bootstrap_locks` to `fc27_app`.
  - Update SHA256 digest in `src/db_migrate.py`.

### 2. WebAuthn Passkeys Engine (`src/identity/webauthn.py`)
- Implement `WebAuthnConfig`, `WebAuthnEngine`, and `get_webauthn_engine(store)`.
- Use real `webauthn>=2.5` APIs: `generate_registration_options`, `verify_registration_response`, `generate_authentication_options`, `verify_authentication_response`.
- Challenge generation, base64url serialization, and persistence/atomic consumption via `store.create_challenge` and `store.consume_challenge`.
- Anti-cloning signature counter check.

### 3. Staff TOTP Engine (`src/identity/totp.py`)
- Implement `generate_totp_seed()`, `get_provisioning_uri()`, `generate_svg_qr_data_uri()`.
- Implement `verify_totp_code(secret, code, last_verified_at, valid_window=1)`:
  - Drift tolerance: $\pm 1$ step = 30 seconds.
  - Replay protection: candidate step must be strictly greater than last verified step.
- Implement thread-safe `TotpRateLimiter` (5 failed attempts -> 429 Too Many Requests).

### 4. Commissioner Bootstrap & Recovery Engine (`src/identity/recovery.py`)
- Implement `generate_recovery_codes(count=10)`: Crockford Base32 hyphenated strings `fc27-xxxx-xxxx-xxxx`.
- Implement `BootstrapService`: validates `bootstrap_token` via constant-time comparison against `BOOTSTRAP_TOKEN` / `DEFAULT_BOOTSTRAP_TOKEN`, checks lock status, creates user, role, recovery codes, bootstrap lock, and session.
- Implement `RecoveryService`: verifies candidate code against unconsumed Argon2id hashes, consumes code atomically under trigger constraints, issues emergency recovery session.

### 5. Identity Store Extensions (`src/identity/store.py`)
- Credential methods: `save_credential`, `get_credential`, `list_credentials_for_user`, `update_credential_counter` (handling bytes/hex/base64url normalization).
- TOTP methods: `get_totp_credential`, `record_totp_verification`, aliases `save_totp_secret`, `get_totp_secret`, `rotate_totp_secret`, `deactivate_totp_secret`.
- Bootstrap methods: `is_bootstrapped()`, `record_bootstrap_lock(...)`.
- Recovery methods: `save_recovery_codes`, `verify_and_consume_recovery_code`, `get_unconsumed_recovery_code_count`.

### 6. Policy Updates (`src/identity/policy.py`)
- Add `ActionType.TOTP_ENROLL`, `ActionType.TOTP_VERIFY`, `ActionType.TOTP_REVOKE` to `_BASE_ROLE_PERMISSIONS[UserRole.COMMISSIONER]` and `[UserRole.COMMISSIONER_DELEGATE]`.
- Add passkey actions `ActionType.PASSKEY_REGISTER`, `ActionType.PASSKEY_VIEW`, `ActionType.PASSKEY_REVOKE` where needed.

### 7. API Routers (`src/identity/routers/`)
- `passkeys.py`:
  - `POST /v1/auth/passkeys/register/options` & `POST /v1/auth/passkeys/register/verify`
  - `POST /v1/auth/passkeys/authenticate/options` & `POST /v1/auth/passkeys/authenticate/verify` (aliased with `/login/options` & `/login/verify`)
  - `GET /v1/auth/passkeys` & `DELETE /v1/auth/passkeys/{credential_id}`
- `totp.py`:
  - `POST /v1/auth/totp/setup` & `POST /v1/auth/totp/enroll`
  - `POST /v1/auth/totp/verify`
  - `POST /v1/auth/totp/authenticate`
  - `POST /v1/auth/totp/revoke`
  - `GET /v1/auth/totp/status`
- `recovery.py`:
  - `POST /v1/auth/bootstrap` (one-step programmatic bootstrap for tests/CLI)
  - `POST /v1/auth/bootstrap/init` & `POST /v1/auth/bootstrap/complete`
  - `GET /v1/auth/bootstrap/status`
  - `POST /v1/auth/recovery/initiate` & `POST /v1/auth/recovery/verify`
  - `POST /v1/auth/recovery/regenerate`
- Mount routers in `src/graph_api.py` and export in `src/identity/routers/__init__.py`.

### 8. Comprehensive Unit & Adversarial Tests
- Author `tests/identity/test_webauthn.py`, `test_totp.py`, `test_recovery.py`, `test_bootstrap.py`.
- Verify packaging and foundation scope:
  - `pytest tests/test_packaging.py tests/test_phase1_scope.py -v`
- Verify router integration and E2E feature alignment:
  - `pytest tests/identity/ -v`
  - `pytest tests/e2e/test_phase2_tier1_features.py -k "passkey or totp or bootstrap or recovery" -v`

### 9. Deliver Hard Handoff Report
- Write complete handoff report to `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m4_1\handoff.md`.
- Notify orchestrator with verification results via `send_message`.
