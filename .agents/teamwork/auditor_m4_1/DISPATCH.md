# Dispatch Instructions for auditor_m4_1

**Agent ID**: `auditor_m4_1`
**Role**: Milestone 4 Forensic Integrity Auditor
**Milestone**: Phase 2 Milestone 4 — WebAuthn Passkeys, Staff TOTP & Bootstrap/Recovery
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m4_1`

## Authoritative Inputs
- Authoritative Request: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (MANDATORY TO READ)
- Project Plan: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
- Worker Handoff: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m4_1\handoff.md`

## Mandatory Forensic Checks
1. Anti-Cheating & Mock Verification:
   - Verify tests and production implementations execute against real libraries (`webauthn>=2.5`, `pyotp>=2.9`, `cryptography`, `argon2-cffi`).
   - Exactly ZERO mocks or bypasses of cryptographic signature verification, Argon2id hashing, MultiFernet encryption, or database triggers.
2. Cryptographic Authenticity:
   - WebAuthn: authentic `webauthn` library options generation, assertion verification, and counter validation.
   - TOTP: authentic `pyotp` verification with drift tolerance ($\pm 1$ step) and MultiFernet encrypted seeds.
   - Recovery: authentic Argon2id hashing (`hash_recovery_code()`) and zero plaintext codes stored in DB.
3. Database Integrity & Concurrency:
   - Row-level locking on challenge consumption (`SELECT ... FOR UPDATE`).
   - Trigger `fc27_guard_recovery_code_consumption` enforces atomic single-use on `fc27_recovery_codes`.
   - Table `fc27_bootstrap_locks` with trigger `fc27_reject_mutation()` prevents duplicate bootstrap ceremonies.
4. Non-Negotiable Invariants:
   - Verify `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` remain strictly enforced.
   - Zero player delivery; no active worker, outbox relay, or delivery adapter.
   - Anti-impersonation: services cannot register or assert passkeys, TOTP, or recovery codes for humans.
5. Secret Leakage Audit:
   - Verify zero plaintext secrets, recovery codes, or TOTP seeds leaked in code, test files, logs, or error responses.
   - Constant-time comparisons used for tokens and secrets (`hmac.compare_digest`).

Provide an explicit binary verdict in your handoff report: **CLEAN** or **INTEGRITY VIOLATION**.
Write complete report to:
`c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m4_1\handoff.md`.
Notify orchestrator via `send_message`.


## 2026-10-04T06:14:13Z
You are auditor_m4_1, a forensic integrity auditor.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m4_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m4_1\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md
Worker handoff: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m4_1\handoff.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md and c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m4_1\DISPATCH.md before starting work.

Your task:
Perform an independent forensic integrity audit of Phase 2 Milestone 4 across:
- migrations/0006_bootstrap_and_recovery.sql, migrations/post/grants.sql, src/db_migrate.py
- src/identity/webauthn.py
- src/identity/totp.py
- src/identity/recovery.py
- src/identity/store.py
- src/identity/policy.py
- src/identity/routers/passkeys.py, totp.py, recovery.py
- tests/identity/

Mandatory Audit Checks:
1. Anti-Cheating & Mock Verification: Verify tests and production implementations execute against real libraries (webauthn>=2.5, pyotp>=2.9, cryptography, argon2-cffi). Exactly ZERO mocks or bypasses of cryptographic signature verification, Argon2id hashing, MultiFernet encryption, or database triggers.
2. Cryptographic Authenticity: Authentic webauthn options and assertion verification; authentic pyotp verification with drift tolerance (±1 step) and MultiFernet encrypted seeds; authentic Argon2id hashing (hash_recovery_code()) and zero plaintext codes stored in DB.
3. Database Integrity & Concurrency: Row-level locking on challenge consumption (SELECT ... FOR UPDATE); trigger fc27_guard_recovery_code_consumption enforces atomic single-use on fc27_recovery_codes; table fc27_bootstrap_locks with trigger fc27_reject_mutation() prevents duplicate bootstrap ceremonies.
4. Non-Negotiable Invariants: Verify DISPATCH_ENABLED=false and DISPATCH_WORKER_ENABLED=false remain strictly enforced; zero player delivery; anti-impersonation: services cannot register or assert passkeys, TOTP, or recovery codes for humans.
5. Secret Leakage Audit: Verify zero plaintext secrets, recovery codes, or TOTP seeds leaked in code, test files, logs, or error responses; constant-time comparisons used for tokens and secrets (hmac.compare_digest).

Provide an explicit binary verdict in your handoff report: CLEAN or INTEGRITY VIOLATION.
Write complete report to: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m4_1\handoff.md
When finished, notify me with your verdict and report path via send_message.
