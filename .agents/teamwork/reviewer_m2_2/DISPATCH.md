# Dispatch Instructions for reviewer_m2_2

**Agent ID**: `reviewer_m2_2`
**Role**: Delegations & Database Hardening Reviewer
**Milestone**: Phase 2 Milestone 2 — Centralized Authorization & Service Boundaries
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m2_2`

## Authoritative Inputs
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md`
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
3. Worker handoff: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m2_1\handoff.md`

## Review Tasks
1. Review `migrations/0005_delegations.sql`:
   - Non-destructive alter of `public.authority_delegations` (adding `club_id`, `starts_at`, relaxing `scope`)
   - Creation of `public.fc27_delegations` view
   - Update of `domain_events_source_table_check` to accept `authority_delegations` and `fc27_delegations`
2. Review `migrations/post/grants.sql` & `src/db_migrate.py`:
   - Policy version 7 declared in header and tracked in `db_migrate.py`
   - Scoped DML granted to `fc27_app` on `authority_delegations` and `fc27_delegations`
   - Exactly 0 grants on delegations to `fc27_dispatch`
3. Review `src/identity/store.py` delegation methods:
   - `create_delegation`, `get_delegation`, `list_delegations`, `revoke_delegation`, `evaluate_delegation`
   - Audit event emission (`identity.delegation_granted`, `identity.delegation_revoked`) into `domain_events`
4. Execute verification tests:
   - `pytest tests/identity/test_delegations.py -v`
   - Packaging and migration sync tests
5. Document commands, findings, and explicit verdict (**APPROVE** or **REQUEST_CHANGES**) in:
   `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m2_2\handoff.md`

## 2026-10-04T04:21:22Z
You are reviewer_m2_2, a high-reliability reviewer agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m2_2
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m2_2\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md
Worker handoff: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m2_1\handoff.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md before starting work.

Your task:
Review the database migration, security constraints, and delegation persistence for Phase 2 Milestone 2.
1. Inspect migrations/0005_delegations.sql, migrations/post/grants.sql (version 7), and src/db_migrate.py. Verify least privilege: fc27_app has scoped DML; fc27_dispatch has 0 grants.
2. Review delegation store methods in src/identity/store.py (create_delegation, get_delegation, list_delegations, revoke_delegation, evaluate_delegation) and event emission to domain_events.
3. Execute verification tests:
   - pytest tests/identity/test_delegations.py -v
   - pytest tests/test_packaging.py tests/test_phase1_scope.py -v
4. Document commands, findings, and explicit verdict (APPROVE or REQUEST_CHANGES) in:
   c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m2_2\handoff.md
When finished, notify me with your verdict and report path via send_message.
