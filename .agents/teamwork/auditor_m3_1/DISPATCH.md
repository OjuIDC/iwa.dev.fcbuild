# Dispatch Instructions for auditor_m3_1

**Agent ID**: `auditor_m3_1`
**Role**: Milestone 3 Forensic Integrity Auditor
**Milestone**: Phase 2 Milestone 3 — Server-Side Sessions & Invitations Lifecycle
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m3_1`

## Authoritative Inputs
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (Read §0, §1A, §5, §6, §7, §8, §11, §12, §14)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
3. Worker handoff: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_1\handoff.md`

## Mandatory Forensic Integrity Checks
You must perform an exhaustive, independent forensic integrity audit of Phase 2 Milestone 3 across:
- `src/identity/routers/invitations.py`
- `src/identity/routers/sessions.py`
- `src/identity/routers/users.py`
- `src/identity/routers/__init__.py`
- `src/identity/store.py`
- `src/graph_api.py`
- `src/identity/deps.py`
- `src/identity/policy.py`
- `tests/identity/test_routers_invitations.py`
- `tests/identity/test_routers_sessions.py`
- `tests/identity/test_routers_users.py`

### 1. Anti-Cheating & Mock Verification
- Verify that test suites and API endpoints interact with authentic PostgreSQL persistence and authentic cryptography (`hash_token()`).
- Verify zero mocks, facades, monkeypatching, or bypasses of server-side session checks, token hashing, or database triggers in source code or production routes.

### 2. Cryptographic Authenticity & Token Storage
- Verify that plaintext session and invitation tokens are high-entropy (64 hex characters) and NEVER persisted in the database.
- Verify that `fc27_invitations.token_hash` and `fc27_sessions.token_hash` strictly store SHA-256 digests (`hash_token()`).
- Verify that token lookup uses deterministic SHA-256 comparison and secret tokens are not logged or exposed in error messages.

### 3. Database Integrity & Concurrency Controls
- Verify that invitation consumption uses atomic row-level locking (`SELECT ... FOR UPDATE`) inside transactions to prevent double-consumption race conditions.
- Verify that user activation strictly honors `fc27_users_status_guard` trigger (only `invited` -> `active`, auto-populates `activated_at`, blocks reactivation of `disabled` accounts).
- Verify that session rotation revokes the old session and creates the new session in the same transaction.

### 4. Non-Negotiable Invariants
- Verify that `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` remain strictly enforced.
- Verify zero player delivery; no active worker, outbox relay, or player dispatch execution pipeline.
- Verify anti-impersonation: services/agents cannot authenticate as humans or commit human decisions.

### 5. Secret Leakage Audit
- Scan code, tests, and logs for leaked secrets, API keys, tokens, or private seeds. Verify constant-time token comparison where applicable and clean redaction in logs/exceptions.

## Verdict Criteria
Provide an explicit verdict in your handoff report:
- `CLEAN`: Zero integrity violations found.
- `INTEGRITY VIOLATION`: Cheating, facades, unhandled bypasses, secret leakage, or invariant violations detected.

Write your report to: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m3_1\handoff.md`.
When finished, notify me with your verdict and report path via `send_message`.


## 2026-10-04T05:00:05Z
You are auditor_m3_1, a forensic integrity auditor.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m3_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m3_1\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md
Worker handoff: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_1\handoff.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md before starting work.

Your task:
Perform an independent forensic integrity audit of Phase 2 Milestone 3 across:
- src/identity/routers/invitations.py
- src/identity/routers/sessions.py
- src/identity/routers/users.py
- src/identity/routers/__init__.py
- src/identity/store.py
- src/graph_api.py
- src/identity/deps.py
- src/identity/policy.py
- tests/identity/

Mandatory Audit Checks:
1. Anti-Cheating & Mock Verification: Verify tests and routes execute against real implementations, 0 mocks of session lifecycle, hashing, or database triggers.
2. Cryptographic Authenticity: Verify authentic SHA-256 hashing (hash_token()), 64-hex char tokens, and zero plaintext tokens stored in DB.
3. Database Integrity & Concurrency: Verify genuine row-level locking (FOR UPDATE), atomic single-use consumption, trigger adherence (fc27_users_status_guard).
4. Non-Negotiable Invariants: Verify DISPATCH_ENABLED=false and DISPATCH_WORKER_ENABLED=false remain strictly enforced, zero player delivery, no active worker/relay. Verify anti-impersonation: services cannot authenticate as humans.
5. Secret Leakage Audit: Verify zero leaked secrets, tokens, or seeds in code, tests, or exceptions. Constant-time token verification used where applicable.

Provide an explicit verdict in your handoff report: CLEAN or INTEGRITY VIOLATION.
Write complete report to: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m3_1\handoff.md
When finished, notify me with your verdict and report path via send_message.
