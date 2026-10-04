# Dispatch Instructions for reviewer_m4_2

**Agent ID**: `reviewer_m4_2`
**Role**: Bootstrap Lock & Recovery Reviewer
**Milestone**: Phase 2 Milestone 4 — WebAuthn Passkeys, Staff TOTP & Bootstrap/Recovery
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m4_2`

## Authoritative Inputs
- Authoritative Request: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (MANDATORY TO READ)
- Project Plan: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
- Worker Handoff: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m4_1\handoff.md`

## Review Tasks
1. Inspect `migrations/0006_bootstrap_and_recovery.sql`, `migrations/post/grants.sql` (v8), and `src/db_migrate.py`:
   - `public.fc27_bootstrap_locks` with trigger `fc27_reject_mutation()` on UPDATE/DELETE.
   - Grants: `SELECT, INSERT` on `fc27_bootstrap_locks` granted to `fc27_app`.
   - View `public.fc27_credentials` mapping `sign_counter AS sign_count`.
2. Inspect `src/identity/recovery.py` and `src/identity/routers/recovery.py`:
   - `generate_recovery_codes`: 10 Crockford Base32 unconfusable hyphenated codes `fc27-xxxx-xxxx-xxxx`.
   - Argon2id hashing via `src/identity/crypto.py:hash_recovery_code`.
   - Atomic consumption under trigger `fc27_guard_recovery_code_consumption` (`UPDATE ... SET consumed_at = now() WHERE code_id = %s AND consumed_at IS NULL`).
   - One-time bootstrap lock: `POST /v1/auth/bootstrap` succeeds on first run, permanently locked; subsequent calls return HTTP 409 Conflict.
   - `GET /v1/auth/bootstrap/status` returns correct state.
3. Execute verification tests:
   - `pytest tests/test_packaging.py tests/test_phase1_scope.py -v`
   - `pytest tests/identity/test_recovery.py tests/identity/test_bootstrap.py -v`
4. Provide explicit verdict: APPROVE or REQUEST_CHANGES.
5. Deliver complete handoff report to:
   `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m4_2\handoff.md`.

## 2026-10-04T06:14:13Z
You are reviewer_m4_2, a high-reliability reviewer agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m4_2
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m4_2\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md
Worker handoff: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m4_1\handoff.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md and c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m4_2\DISPATCH.md before starting work.

Your task:
Review the Commissioner Bootstrap Lock and Recovery Codes implementation for Phase 2 Milestone 4:
1. Inspect migrations/0006_bootstrap_and_recovery.sql, migrations/post/grants.sql (v8), and src/db_migrate.py:
   - public.fc27_bootstrap_locks with trigger fc27_reject_mutation() on UPDATE/DELETE.
   - Grants: SELECT, INSERT on fc27_bootstrap_locks granted to fc27_app.
   - View public.fc27_credentials mapping sign_counter AS sign_count.
2. Inspect src/identity/recovery.py and src/identity/routers/recovery.py:
   - generate_recovery_codes: 10 Crockford Base32 unconfusable hyphenated codes fc27-xxxx-xxxx-xxxx.
   - Argon2id hashing via src/identity/crypto.py:hash_recovery_code.
   - Atomic consumption under trigger fc27_guard_recovery_code_consumption (UPDATE ... SET consumed_at = now() WHERE code_id = %s AND consumed_at IS NULL).
   - One-time bootstrap lock: POST /v1/auth/bootstrap succeeds on first run, permanently locked; subsequent calls return HTTP 409 Conflict.
   - GET /v1/auth/bootstrap/status returns correct state.
3. Execute verification tests:
   - pytest tests/test_packaging.py tests/test_phase1_scope.py -v
   - pytest tests/identity/test_recovery.py tests/identity/test_bootstrap.py -v
4. Document all commands, test outputs, findings, and explicit verdict (APPROVE or REQUEST_CHANGES) in:
   c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m4_2\handoff.md
When finished, notify me with your verdict and report path via send_message.
