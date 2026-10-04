# Dispatch Instructions for auditor_m2_1

**Agent ID**: `auditor_m2_1`
**Role**: Milestone 2 Forensic Integrity Auditor
**Milestone**: Phase 2 Milestone 2 — Centralized Authorization & Service Boundaries
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m2_1`

## Authoritative Inputs
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md`
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
3. Worker handoff: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m2_1\handoff.md`

## Forensic Integrity Audit Tasks
Perform an independent forensic integrity audit across:
`migrations/0005_delegations.sql`, `migrations/post/grants.sql`, `src/db_migrate.py`, `src/identity/policy.py`, `src/identity/services.py`, `src/identity/deps.py`, `src/identity/store.py`, and `tests/identity/`.

Mandatory Audit Checks:
1. Anti-Cheating & Mock Verification:
   - Verify that tests execute against real implementations, 0 mocks of authorization policy or service credentials in tests.
2. Authorization Authenticity:
   - Verify that `authorize()` genuinely evaluates object tenancy, player isolation, and delegations without bypasses.
3. Database Integrity & Least Privilege:
   - Verify `0005_delegations.sql` uses genuine SQL constraints.
   - Verify `migrations/post/grants.sql` (policy version 7) grants least-privilege DML to `fc27_app` and exactly 0 grants on delegations to `fc27_dispatch`.
   - Verify `db_migrate.py` synchronizes `GRANTS_POLICY` version 7.
4. Non-Impersonation & G-TIMEOUT Invariants:
   - Verify service principals cannot perform human decisions.
   - Verify timeouts and inactivity NEVER create approvals.
   - Verify `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` remain strictly enforced. Zero player delivery.
5. Secret Leakage Audit:
   - Verify zero plaintext secrets, keys, or seeds leaked in code, tests, or exceptions.
   - Constant-time comparison used for service tokens.

Provide explicit verdict in your handoff report: **CLEAN** or **INTEGRITY VIOLATION**.
Write complete report to: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m2_1\handoff.md`.


## 2026-10-04T04:21:23Z
You are auditor_m2_1, a forensic integrity auditor.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m2_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m2_1\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md
Worker handoff: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m2_1\handoff.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md before starting work.

Your task:
Perform an independent forensic integrity audit of Phase 2 Milestone 2: Centralized Authorization & Service Boundaries across migrations/0005_delegations.sql, migrations/post/grants.sql, src/db_migrate.py, src/identity/policy.py, src/identity/services.py, src/identity/deps.py, src/identity/store.py, and tests/identity/.

Mandatory Audit Checks:
1. Anti-Cheating & Mock Verification: Verify tests execute against real implementations, 0 mocks of authorization policy or service credentials in tests.
2. Authorization Authenticity: Verify authorize() genuinely evaluates object tenancy, player isolation, and delegations without facades.
3. Database Integrity & Least Privilege: Verify migrations use genuine SQL constraints; Grants policy v7 gives fc27_app scoped DML and fc27_dispatch 0 grants on delegations; db_migrate.py synchronizes GRANTS_POLICY v7.
4. Non-Impersonation & G-TIMEOUT Invariants: Verify service principals cannot perform human decisions; verify timeouts and inactivity NEVER create approvals; verify DISPATCH_ENABLED=false, DISPATCH_WORKER_ENABLED=false, zero player delivery.
5. Secret Leakage Audit: Verify zero plaintext secrets, keys, or seeds leaked in code, tests, or exceptions. Constant-time token verification used.

Provide an explicit verdict in your handoff report: CLEAN or INTEGRITY VIOLATION.
Write complete report to:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m2_1\handoff.md
When finished, notify me with your verdict and report path via send_message.
