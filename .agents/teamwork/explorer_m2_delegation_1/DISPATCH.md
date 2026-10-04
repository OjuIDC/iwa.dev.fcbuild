# Dispatch Instructions for explorer_m2_delegation_1

**Agent ID**: `explorer_m2_delegation_1`
**Role**: Human Delegation & Authority Model Designer
**Milestone**: Phase 2 Milestone 2 — Centralized Authorization & Service Boundaries
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_delegation_1`

## Authoritative Inputs
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (Read §1A, §4, §7.3, §11, §14)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
3. Existing schema: `migrations/0004_identity_foundation.sql` and `src/identity/store.py`

## Mission & Objectives
Investigate and design the Human Authority and Delegation Engine:
1. Authority Model Hierarchy:
   - Primary Authority: Commissioner (human identity with WebAuthn passkey).
   - `explicit_delegate`: Time-bounded, action-scoped delegation granted by Commissioner to another human.
   - `affected_player_consent`: Explicit consent/decision by the affected player on player-scoped packages.
   - `affected_player_fallback`: Escalation / fallback path if player does not respond within deadline (Commissioner intervention without automated approval).
2. Human Delegation Lifecycle:
   - Granting delegation: Creator must be active Commissioner; grantee must be active human user in same club.
   - Scope constraints: Specific action types, time bounds (`starts_at`, `expires_at`), optional target package/player constraints.
   - Evaluation logic: Active, not revoked, current time between `starts_at` and `expires_at`, club match, action type match.
   - Revocation: Immediate, explicit revocation by Commissioner.
3. Database & Storage Design:
   - Determine whether a dedicated delegation table (`fc27_delegations`) is needed in a migration (e.g. `migrations/0005_delegations.sql` or integrated schema) or handled via existing structures.
   - Atomic store operations in `src/identity/store.py`.
4. Inactivity & Timeout Invariants (G-TIMEOUT):
   - Inactivity and timeouts alter eligibility and trigger escalation, but NEVER create approvals or decisions.
   - Automated systems cannot make decisions.
5. Deliver complete schema/store recommendations, evaluation algorithms, and test design.

Write report to: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_delegation_1\handoff.md`.

## 2026-10-04T03:54:20Z
[Message] timestamp=2026-10-04T03:54:20Z sender=25210876-a9ad-4f78-a87c-dec88e86edb9 priority=MESSAGE_PRIORITY_HIGH
You are explorer_m2_delegation_1, a Read-only exploration agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_delegation_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_delegation_1\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md before starting work.

Your task:
1. Investigate and design the Human Authority and Delegation Engine for Phase 2 Milestone 2.
2. Design the authority model hierarchy:
   - Primary Authority: Commissioner (human identity with WebAuthn passkey).
   - explicit_delegate: Time-bounded, action-scoped delegation granted by Commissioner to another human.
   - affected_player_consent: Explicit consent/decision by the affected player on player-scoped packages.
   - affected_player_fallback: Escalation / fallback path if player does not respond within deadline (Commissioner intervention without automated approval).
3. Design human delegation lifecycle and evaluation:
   - Granting: Creator must be active Commissioner; grantee must be active human user in same club.
   - Scope constraints: Specific action types, time bounds (starts_at, expires_at), target constraints.
   - Evaluation: Active, unrevoked, valid time window, club match, action type match.
   - Revocation: Explicit, immediate revocation by Commissioner.
4. Database & Storage:
   - Determine whether a dedicated delegation table (fc27_delegations) is needed in migrations/0005_delegations.sql or integrated into store.py.
   - Atomic persistence methods in src/identity/store.py.
5. Inactivity & Timeout Invariants (G-TIMEOUT):
   - Inactivity and timeouts alter eligibility/escalation but NEVER create approvals or decisions.
   - Automated systems cannot make decisions.
6. Provide exact code structure, schema definitions, algorithms, and test design.
7. Deliver your complete handoff report to:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_delegation_1\handoff.md
When finished, notify me with a summary and the path to your handoff report via send_message.
