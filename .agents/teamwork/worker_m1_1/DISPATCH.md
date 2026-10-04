# Dispatch: Milestone 1 Implementation Worker

## Identity
- Archetype: teamwork_preview_worker
- Working Directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m1_1

## Mandatory Inputs
You MUST read:
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (including Section 1A patch).
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`.
3. The 3 completed Milestone 1 Explorer reports:
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_schema_1\schema_analysis.md`
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_store_1\store_analysis.md`
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_tests_1\tests_analysis.md`
4. `docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md` (Task 4).

## Write Ownership (Exclusively Owned Files)
You own and modify:
- `migrations/0003_dispatch_foundation.sql`
- `migrations/post/grants.sql`
- `src/db_migrate.py`
- `src/approval_store.py`
- `src/decision.py`
- `tests/helpers.py`
- `tests/conftest.py`
- `tests/test_approval_security.py`
- `tests/test_migration_0003.py`
- `tests/test_approval_lifecycle.py`

## Mandatory Integrity Warning
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

## Implementation Tasks
1. Schema & Migrations:
   - Append Section B to `migrations/0003_dispatch_foundation.sql`:
     - `public.approval_requests` table with immutable provenance triggers and terminal row freezing.
     - `graph_threads` updates: `approval_request_id` FK, extended status checks, replacement `fc27_guard_thread_transition` trigger enforcing immutable `package_version` while awaiting.
     - `fc27_check_approval_pair()` function and twin `CONSTRAINT TRIGGER ... DEFERRABLE INITIALLY DEFERRED` triggers on both tables.
     - `fc27_approval_inconsistencies` view.
     - `approval_audit` extensions (`proof_type`, `actor_user_id`, `webauthn_credential_id`, `correlation_id`, `approval_request_id`, `REQUEST_REVISION` decision check, and Section 1A compatibility fields).
     - `approval_nonces` extensions (`revoked_at`, `subject_type`, `dispatch_id`, `user_id`, `gate_id`, nullable fields, single-subject check).
     - Section 1A compatibility tables: `authority_policies`, `authority_delegations`, `player_assignments`, `player_decision_requests`, `player_decisions`, `approval_escalations`, `authority_conflicts`.
     - `fc27_check_event_source()` trigger function update for `approval_requests`.
     - Legacy data backfill.
   - Update `migrations/post/grants.sql`: upgrade to `-- grants-policy-version: 3`, add grants for `fc27_app` on all new tables and view, grant zero access to `fc27_dispatch`.
   - Update `src/db_migrate.py`: compute SHA-256 of `grants.sql` and update `GRANTS_POLICY = GrantsPolicy(version=3, sha256="...")`.

2. Store & Decision Logic:
   - In `src/approval_store.py`:
     - Accept `request_ttl: timedelta = timedelta(hours=72)` in `__init__`.
     - `complete_run()`: In single transaction, insert `approval_requests` row with TTL, link `graph_threads`, record `approval.created` via `record_event`, return enriched state.
     - `issue_challenge()`: Run `expire_due()`, lock request `FOR SHARE`, check `fc27_approval_inconsistencies`, bound nonce TTL.
     - `apply_decision()`: Enforce lock order (`approval_nonces` -> `approval_requests` -> `graph_threads`), check inconsistencies, safe idempotent replay (return committed decision state instead of 409 Conflict!), write `approval_audit` with `proof_type`, `correlation_id`, `approval_request_id`, and Section 1A compatibility fields, record `approval.decided`, validate Section 1A authority basis (reject player/fallback/delegates in Phase 1).
     - `expire_due()`: Proactive expiry in independent transactions, set `expired`, revoke nonces, record `approval.expired`.
     - `pending_approvals()`: Inbox query.
     - `terminal_thread_ids`: Include `revision_requested`.
   - In `src/decision.py`:
     - Eliminate legacy `new_state["dispatch"]` dictionary! Support `REQUEST_REVISION`.

3. Tests & Harness:
   - Add `legacy_install` in `tests/helpers.py`.
   - Add teardown check for `fc27_approval_inconsistencies` in `tests/conftest.py`.
   - Rewrite stale candidate test in `tests/test_approval_security.py:203-210`.
   - Implement `tests/test_migration_0003.py`.
   - Implement `tests/test_approval_lifecycle.py` (including Section 1A authority separation tests).

4. Verification:
   - Run tests:
     `$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_db_roles.py tests/test_db_migrate_roles.py tests/test_events_outbox.py tests/test_migration_0003.py tests/test_approval_lifecycle.py tests/test_approval_security.py`
   - Document all test execution outputs in `handoff.md`.

## Output Requirements
- Write `handoff.md` following the Handoff Protocol.
- Send a completion message to the orchestrator.


## 2026-10-03T14:27:07Z
[Message] timestamp=2026-10-03T14:27:07Z sender=7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b priority=MESSAGE_PRIORITY_HIGH
You are worker_m1_1, a teamwork_preview_worker agent.
Your working directory is: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m1_1

Read your instructions in:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m1_1\DISPATCH.md

MANDATORY: You MUST read:
1. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md (including Section 1A patch)
2. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md
3. The 3 explorer reports:
   - c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_schema_1\schema_analysis.md
   - c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_store_1\store_analysis.md
   - c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_tests_1\tests_analysis.md
4. docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md (Task 4)

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

Write ownership:
- migrations/0003_dispatch_foundation.sql
- migrations/post/grants.sql
- src/db_migrate.py
- src/approval_store.py
- src/decision.py
- tests/helpers.py
- tests/conftest.py
- tests/test_approval_security.py
- tests/test_migration_0003.py
- tests/test_approval_lifecycle.py

Task:
Implement Milestone 1 (Authoritative Approval Lifecycle, Expiry & Core Data Model) following the detailed designs from the three explorers.
Execute tests via:
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_db_roles.py tests/test_db_migrate_roles.py tests/test_events_outbox.py tests/test_migration_0003.py tests/test_approval_lifecycle.py tests/test_approval_security.py
Write handoff.md in your working directory and notify the orchestrator with send_message when done.


## 2026-10-03T14:40:19Z
[Message] timestamp=2026-10-03T14:40:19Z sender=7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b priority=MESSAGE_PRIORITY_HIGH
**Context**: Milestone 1 implementation status check
**Content**: Checking in on your progress on Milestone 1 implementation (schema, store, tests, and test execution).
**Action**: Please update your progress.md with your current step and report status.
