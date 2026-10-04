# Dispatch Instructions for worker_m2_1

**Agent ID**: `worker_m2_1`
**Role**: Milestone 2 Implementation Worker
**Milestone**: Phase 2 Milestone 2 — Centralized Authorization & Service Boundaries
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m2_1`

## Authoritative Inputs
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md`
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
3. Explorer handoffs & blueprints:
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_policy_1\handoff.md`
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_policy_1\proposed_policy.py`
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_policy_1\proposed_test_policy.py`
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_delegation_1\handoff.md`
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_services_1\handoff.md`
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_services_1\proposed_services.py`
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_services_1\proposed_test_services.py`

## Mandatory Integrity Warning
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

## Non-Negotiable Invariants
- `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` strictly enforced at all times.
- Zero player delivery; Phase 2 must NOT activate player dispatch or delivery adapters.
- Zero agent-human impersonation: automated services cannot authenticate or decide for humans.
- Gamertag is display only; immutable UUID is primary identity.
- Inactivity and timeouts alter eligibility but NEVER create approvals or decisions.

## Exclusive Write Ownership
You exclusively own and may edit/create:
- `migrations/0005_delegations.sql`
- `migrations/post/grants.sql`
- `src/db_migrate.py`
- `src/identity/policy.py`
- `src/identity/services.py`
- `src/identity/deps.py`
- `src/identity/store.py`
- `tests/identity/test_policy.py`
- `tests/identity/test_delegations.py`
- `tests/identity/test_services.py`

## Implementation Tasks
1. Create `migrations/0005_delegations.sql` to non-destructively adapt `authority_delegations`:
   - Add `club_id text NOT NULL DEFAULT '1899' CHECK (club_id ~ '^[0-9]+$')`
   - Add `starts_at timestamptz NOT NULL DEFAULT now()`
   - Relax `scope` check constraint to allow descriptive scopes (e.g. `tactical_drills`, `club`, `player_specific`)
   - Create view `public.fc27_delegations` as synonym for `public.authority_delegations`
   - Update `domain_events_source_table_check` to accept `authority_delegations` and `fc27_delegations`
2. Update `migrations/post/grants.sql` to version 7:
   - Grant SELECT, INSERT, UPDATE (no DELETE) on `authority_delegations` and `fc27_delegations` to `fc27_app`
   - Ensure `fc27_dispatch` has 0 grants
3. Synchronize `src/db_migrate.py`:
   - Set `GRANTS_POLICY = GrantsPolicy(version=7, ...)` and compute digest
4. Create `src/identity/policy.py`:
   - Complete `authorize(principal, action, resource, authority_basis, delegation)`
   - 8 roles (`commissioner`, `commissioner_delegate`, `coach`, `analyst`, `staff`, `player`, `bff_service`, `graph_service`)
   - 4 authority bases (`primary`, `explicit_delegate`, `affected_player_consent`, `affected_player_fallback`)
   - Object-level isolation: club tenancy, player isolation, anti-impersonation
   - G-TIMEOUT invariant enforcement
5. Create `src/identity/services.py`:
   - `ServiceCredentialsManager` for `BFF_SERVICE_TOKEN`, `GRAPH_SERVICE_TOKEN`, `WORKER_SERVICE_TOKEN`
   - `validate_service_token(token: str)`
   - Anti-impersonation guards and gamertag separation helpers
6. Create `src/identity/deps.py`:
   - FastAPI dependency `get_current_principal` validating service tokens and sessions
   - `require_permission(action)`
7. Update `src/identity/store.py`:
   - Add delegation persistence: `create_delegation`, `get_delegation`, `list_delegations`, `revoke_delegation`, `evaluate_delegation`
8. Author test suites:
   - `tests/identity/test_policy.py`
   - `tests/identity/test_delegations.py`
   - `tests/identity/test_services.py`
9. Execute tests and ensure 100% pass:
   - pytest `tests/test_packaging.py` `tests/test_phase1_scope.py`
   - pytest `tests/identity/test_policy.py` `tests/identity/test_delegations.py` `tests/identity/test_services.py`
   - pytest `tests/identity/test_crypto.py` `tests/identity/test_crypto_stress.py` `tests/test_m1_phase2_adversarial.py`
10. Deliver complete hard handoff report to:
    `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m2_1\handoff.md`


## 2026-10-04T04:05:38Z
You are worker_m2_1, a versatile worker agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m2_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m2_1\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md

Explorer handoffs & blueprints to implement:
- c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_policy_1\handoff.md
- c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_policy_1\proposed_policy.py
- c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_policy_1\proposed_test_policy.py
- c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_delegation_1\handoff.md
- c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_services_1\handoff.md
- c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_services_1\proposed_services.py
- c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_services_1\proposed_test_services.py
