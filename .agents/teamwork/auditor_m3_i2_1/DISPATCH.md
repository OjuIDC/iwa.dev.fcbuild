# Dispatch Instructions for auditor_m3_i2_1

**Agent ID**: `auditor_m3_i2_1`
**Role**: Milestone 3 Iteration 2 Forensic Integrity Auditor
**Milestone**: Phase 2 Milestone 3 — Server-Side Sessions & Invitations Lifecycle (Iteration 2 Audit)
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m3_i2_1`

## Inputs
- Authoritative Request: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (MANDATORY TO READ)
- Project Plan: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
- Previous Iteration Audit Report (with violations): `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m3_1\handoff.md`
- Worker Handoff: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_2\handoff.md`

## Mandatory Forensic Checks
1. Verification of Remediations:
   - Check `src/identity/store.py`:
     - Verify `create_session` signature accepts `club_id` and `**kwargs`.
     - Verify `rotate_session` uses atomic UPDATE with `AND revoked_at IS NULL` and checks `res.rowcount == 0` to raise 409.
     - Verify `assign_user_role` alias correctly delegates to `assign_role`.
   - Check `src/identity/policy.py`:
     - Verify Step 5 session isolation: `SESSION_REVOKE` and `SESSION_VIEW` enforce user ownership and prevent cross-user revocation unless commissioner or delegate.
   - Check `src/identity/routers/sessions.py`:
     - Verify `/rotate` rejects arbitrary client elevation to `step_up_passkey` with 403 Forbidden.
     - Verify `require_human_principal` is enforced on all human session endpoints.
   - Check test suites:
     - Verify tests execute against real implementations, 0 mocks of session lifecycle, hashing, or database triggers.
     - Verify `expires_at` -> `absolute_expires_at` column fixes in test suites.
2. Invariant Checks:
   - Verify `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` strictly enforced at all times.
   - Zero player delivery; no active worker or relay.
   - Zero plaintext tokens stored in DB.
3. Secret Leakage Audit:
   - Verify zero plaintext secrets, tokens, or seeds leaked in code, tests, or exceptions.
4. Binary Verdict:
   - State an explicit verdict: **CLEAN** or **INTEGRITY VIOLATION**.
   - If ANY violation is detected, detail the evidence chain.
5. Deliver complete handoff report to:
   `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m3_i2_1\handoff.md`.
   Notify orchestrator via `send_message`.


## 2026-10-04T05:35:57Z
You are auditor_m3_i2_1, a forensic integrity auditor.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m3_i2_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m3_i2_1\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md
Worker handoff: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_2\handoff.md
Previous audit report with violations: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m3_1\handoff.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md, the previous audit report, and c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m3_i2_1\DISPATCH.md before starting work.

Your task:
Perform an independent forensic integrity audit of Phase 2 Milestone 3 Iteration 2 across:
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
3. Database Integrity & Concurrency: Verify genuine row-level locking (FOR UPDATE) in invitations, atomic UPDATE with AND revoked_at IS NULL and 409 check in rotate_session.
4. Non-Negotiable Invariants: Verify DISPATCH_ENABLED=false and DISPATCH_WORKER_ENABLED=false remain strictly enforced, zero player delivery, no active worker/relay. Verify anti-impersonation: services cannot authenticate as humans.
5. Secret Leakage Audit: Verify zero leaked secrets, tokens, or seeds in code, tests, or exceptions. Constant-time token verification used where applicable.
6. Verify Remediations: verify create_session signature, rotate_session atomic concurrency lock, assign_user_role alias, and Step 5 session isolation in policy.py.

Provide an explicit binary verdict in your handoff report: CLEAN or INTEGRITY VIOLATION.
Write complete report to: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m3_i2_1\handoff.md
When finished, notify me with your verdict and report path via send_message.
