# Dispatch Instructions for explorer_m2_policy_1

**Agent ID**: `explorer_m2_policy_1`
**Role**: Authorization Policy & RBAC/ABAC Designer
**Milestone**: Phase 2 Milestone 2 — Centralized Authorization & Service Boundaries
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_policy_1`

## Authoritative Inputs
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (Read §0, §1A, §4, §5, §7, §11, §12)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
3. Existing schema: `migrations/0004_identity_foundation.sql` and `src/identity/store.py`

## Mission & Objectives
Investigate and design `src/identity/policy.py` for centralized, object-scoped authorization:
1. Define core domain types: `Principal`, `UserRole`, `ActionType`, `Resource`, `DecisionResult`, `AuthorizationError`.
2. Define authorization matrix covering all 8 roles:
   - `commissioner`: Full authority across club, package approvals, delegations, invitations, user administration.
   - `commissioner_delegate`: Scoped authority based on active human delegation.
   - `coach`: Viewing packages, rosters, submitting assessments, viewing observations.
   - `analyst`: Viewing observations, analytics, submitting assessments.
   - `staff`: Operational duties, invitation management (if delegated), profile viewing.
   - `player`: Strictly object-scoped to self (`player_id == principal.player_id` or `user_id == principal.user_id`). Player consent/decisions on own assignments.
   - `bff_service`: Dedicated frontend gateway principal; narrow API scopes; no direct DB mutation.
   - `graph_service`: Internal workflow caller; least-privileged application operations.
3. Object-level isolation:
   - Club tenancy: `principal.club_id == resource.club_id`.
   - Player tenancy: Players cannot view or mutate other players' data or assignments.
4. Non-negotiable invariants:
   - Zero agent-human impersonation.
   - Gamertag is display only; immutable UUID is primary identity.
   - `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false`.
5. Deliver comprehensive architectural specification, function signatures, Pydantic/dataclass models, and test design.

Write report to: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_policy_1\handoff.md`.


## 2026-10-04T03:54:20Z
You are explorer_m2_policy_1, a Read-only exploration agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_policy_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_policy_1\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md before starting work.

Your task:
1. Investigate and design src/identity/policy.py for centralized, object-scoped authorization.
2. Define core domain types: Principal, UserRole (8 roles: commissioner, commissioner_delegate, coach, analyst, staff, player, bff_service, graph_service), ActionType, Resource, DecisionResult, and AuthorizationError.
3. Define the authorization matrix covering all 8 roles and their permitted action types.
4. Define object-level resource rules:
   - Club tenancy: principal.club_id == resource.club_id.
   - Player isolation: player can ONLY access/decide where resource.player_id == principal.player_id or resource.user_id == principal.user_id.
   - Commissioner & delegate permissions: package approvals, delegations, user administration.
5. Define non-negotiable invariants:
   - Zero agent-human impersonation (agents/services cannot authenticate or decide for humans).
   - Gamertag is display only; immutable UUID is primary identity.
   - DISPATCH_ENABLED=false and DISPATCH_WORKER_ENABLED=false strictly enforced.
6. Provide exact code structure, type definitions, function signatures, error handling, and test recommendations.
7. Deliver your complete handoff report to:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_policy_1\handoff.md
When finished, notify me with a summary and the path to your handoff report via send_message.
