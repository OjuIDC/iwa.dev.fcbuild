# Dispatch: Reviewer 1 (Schema, Grants & Migrations)

## Identity
- Archetype: teamwork_preview_reviewer
- Working Directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m1_1

## Mandatory Inputs
You MUST read:
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (including Section 1A patch).
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`.
3. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m1_1\handoff.md`.

## Task
Review Milestone 1 implementation with special focus on database schema, migrations, triggers, views, and least-privilege role separation:
1. Inspect `migrations/0003_dispatch_foundation.sql`:
   - `public.approval_requests` definition, constraints, and triggers.
   - `graph_threads` updates, immutable transition triggers, and terminal field checks.
   - `fc27_check_approval_pair()` twin deferred constraint triggers.
   - Diagnostic view `public.fc27_approval_inconsistencies`.
   - Section 1A compatibility tables (`authority_policies`, `authority_delegations`, `player_assignments`, `player_decision_requests`, `player_decisions`, `approval_escalations`, `authority_conflicts`).
   - Legacy backfill logic.
2. Inspect `migrations/post/grants.sql` and `src/db_migrate.py`:
   - Verify grants policy version is 3 and matches `GRANTS_POLICY` SHA-256 in `src/db_migrate.py`.
   - Verify role isolation: `fc27_app` has only necessary DML; `fc27_dispatch` has zero permissions; no DDL permissions for runtime roles.
3. Run verification tests:
   `$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_db_roles.py tests/test_db_migrate_roles.py tests/test_events_outbox.py tests/test_migration_0003.py tests/test_approval_lifecycle.py tests/test_approval_security.py`
4. Formulate explicit verdict: `APPROVE` or `REQUEST_CHANGES`.
5. Write your complete review to `review.md` and complete handoff report to `handoff.md`.
6. Notify the orchestrator via `send_message`.
Do NOT modify production source code in `src/` or `migrations/`.
