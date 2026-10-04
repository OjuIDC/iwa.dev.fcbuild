# Task Assignment — worker_m1_1

## Objective
Implement Phase 2 Milestone 1: Identity Data Foundation & Cryptography.

## Authoritative Inputs
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (Read completely before starting!)
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
- Explorer handoffs:
  - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_schema_1\handoff.md` (and `schema_analysis.md`)
  - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_crypto_1\handoff.md`
  - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_packaging_1\handoff.md`

## File Ownership
You exclusively own and may edit/create:
- `pyproject.toml`
- `requirements.txt`
- `tests/test_phase1_scope.py`
- `migrations/0004_identity_foundation.sql`
- `migrations/post/grants.sql`
- `src/db_migrate.py`
- `src/identity/__init__.py`
- `src/identity/crypto.py`
- `src/identity/store.py`
- `tests/identity/test_crypto.py`
- `tests/identity/test_store.py`

## Implementation Tasks
1. Update `pyproject.toml` and `requirements.txt` adding `webauthn>=2.5,<3`, `pyotp>=2.9,<3`, `cryptography>=44,<45`, and `argon2-cffi>=23,<25`. Ensure exact line synchronization per `explorer_m1_packaging_1/handoff.md`.
2. Update `tests/test_phase1_scope.py` allowing `src/identity/` and scope-restricted `webauthn` imports, while maintaining Phase 3+ boundary protections (no dispatch worker, no outbox relay, DISPATCH_ENABLED=false, DISPATCH_WORKER_ENABLED=false).
3. Author `migrations/0004_identity_foundation.sql` implementing all 9 tables (`fc27_users`, `fc27_user_roles`, `fc27_invitations`, `fc27_sessions`, `fc27_webauthn_credentials`, `fc27_totp_credentials`, `fc27_recovery_codes`, `fc27_action_tokens`, `fc27_challenges`), constraints, and triggers per `explorer_m1_schema_1/handoff.md`.
4. Update `migrations/post/grants.sql` with version bump to 6 granting least-privilege DML to `fc27_app` and 0 grants to `fc27_dispatch`. Update `src/db_migrate.py` `GRANTS_POLICY` with version 6 and exact SHA256 digest.
5. Create `src/identity/crypto.py` with `hash_token`, `hash_recovery_code`, `verify_recovery_code`, `IdentityCrypto` MultiFernet envelope encryption and key rotation per `explorer_m1_crypto_1/handoff.md`.
6. Create `src/identity/store.py` implementing `IdentityStore` persistence for all identity tables, atomic transactions, dual-expiry sessions, and domain event emissions.
7. Create comprehensive unit tests in `tests/identity/test_crypto.py` and `tests/identity/test_store.py`.
8. Execute verification tests (`pytest tests/test_packaging.py tests/test_phase1_scope.py tests/identity/`) and ensure all pass cleanly.
9. Report findings and results in `handoff.md`.

## Mandatory Integrity Warning
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.


## 2026-10-04T02:56:26Z
You are worker_m1_1, a versatile worker agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m1_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m1_1\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md

Explorer handoffs to implement:
- c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_schema_1\handoff.md
- c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_schema_1\schema_analysis.md
- c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_crypto_1\handoff.md
- c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_packaging_1\handoff.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md before starting work.

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

File ownership:
You exclusively own and may edit/create:
- pyproject.toml
- requirements.txt
- tests/test_phase1_scope.py
- migrations/0004_identity_foundation.sql
- migrations/post/grants.sql
- src/db_migrate.py
- src/identity/__init__.py
- src/identity/crypto.py
- src/identity/store.py
- tests/identity/test_crypto.py
- tests/identity/test_store.py

Implement Milestone 1:
1. Update pyproject.toml and requirements.txt adding webauthn>=2.5,<3, pyotp>=2.9,<3, cryptography>=44,<45, argon2-cffi>=23,<25 with strict line-for-line sync.
2. Update tests/test_phase1_scope.py to permit src/identity/ and webauthn imports in identity scope while asserting Phase 3+ boundary guards (NO dispatch worker, NO outbox relay, DISPATCH_ENABLED=false, DISPATCH_WORKER_ENABLED=false).
3. Author migrations/0004_identity_foundation.sql with all 9 tables, constraints, indices, and triggers.
4. Update migrations/post/grants.sql (-- grants-policy-version: 6) and src/db_migrate.py with updated GRANTS_POLICY SHA256 digest.
5. Create src/identity/crypto.py with hash_token, hash_recovery_code, verify_recovery_code, IdentityCrypto MultiFernet envelope encryption and key rotation.
6. Create src/identity/store.py with IdentityStore implementing atomic persistence across all identity tables.
7. Author tests/identity/test_crypto.py and tests/identity/test_store.py.
8. Execute verification tests and ensure 100% pass.
9. Deliver your handoff report to:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m1_1\handoff.md
When finished, notify me with a summary and the path to your handoff report via send_message.
