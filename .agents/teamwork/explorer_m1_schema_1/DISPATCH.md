# Dispatch: Milestone 1 Schema Explorer

## Identity
- Archetype: teamwork_preview_explorer
- Working Directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_schema_1

## Mandatory Inputs
You MUST read:
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (including Section 1A patch).
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`.
3. `docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md` (Task 4).

## Objective
Analyze and design the exact SQL changes for Milestone 1:
1. `migrations/0003_dispatch_foundation.sql` Section B:
   - `approval_requests` table schema, constraints, and triggers (immutability of provenance, terminal row freezing).
   - `graph_threads` modifications (`approval_request_id` FK, extended status CHECK, replacement `fc27_guard_thread_transition` trigger).
   - `fc27_check_approval_pair()` DEFERRABLE INITIALLY DEFERRED constraint trigger.
   - `fc27_approval_inconsistencies` view.
   - `approval_audit` additions (`proof_type`, `correlation_id`, `approval_request_id`, Section 1A compatibility fields).
   - `approval_nonces` additions (`revoked_at`, `subject_type`, subject check).
   - Section 1A compatibility tables (`authority_policies`, `authority_delegations`, `player_assignments`, `player_decision_requests`, `player_decisions`, `approval_escalations`, `authority_conflicts`).
   - `fc27_check_event_source()` trigger function update (`approval_requests` branch).
   - Backfill logic for legacy data.
2. `migrations/post/grants.sql`:
   - Identify all required grants for `fc27_app` and `fc27_dispatch`.
   - Determine next `-- grants-policy-version` header.
3. `src/db_migrate.py`:
   - Determine how `GRANTS_POLICY` is updated.

## Scope Boundaries
- Read-only exploration and design. Do NOT modify source files.
- Deliver detailed SQL recommendations with exact code chunks.

## Output Requirements
- Write `schema_analysis.md` in your working directory.
- Write `handoff.md` following the Handoff Protocol.
- Notify orchestrator with `send_message`.


## 2026-10-03T14:15:37Z
You are explorer_m1_schema_1, a teamwork_preview_explorer agent.
Your working directory is: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_schema_1

Read your instructions in:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_schema_1\DISPATCH.md

MANDATORY: You MUST read:
1. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md (including Section 1A patch)
2. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md
3. docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md (Task 4)

Task:
Perform a deep technical exploration and detailed implementation design of the SQL schema for Milestone 1:
- migrations/0003_dispatch_foundation.sql Section B: approval_requests table, thread constraints, fc27_check_approval_pair trigger, fc27_approval_inconsistencies view, approval_audit fields, approval_nonces fields, event source check update, legacy backfill.
- Section 1A compatibility tables: authority_policies, authority_delegations, player_assignments, player_decision_requests, player_decisions, approval_escalations, authority_conflicts.
- migrations/post/grants.sql updates and grants policy version.
- src/db_migrate.py GRANTS_POLICY update.
Write schema_analysis.md and handoff.md in your working directory, then notify orchestrator with send_message.
Do NOT modify repository files.
