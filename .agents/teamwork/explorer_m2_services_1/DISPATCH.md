# Dispatch Instructions for explorer_m2_services_1

**Agent ID**: `explorer_m2_services_1`
**Role**: Service Identities & Non-Impersonation Boundary Designer
**Milestone**: Phase 2 Milestone 2 — Centralized Authorization & Service Boundaries
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_services_1`

## Authoritative Inputs
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (Read §3, §4, §5, §11, §12)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
3. Existing schema: `migrations/0004_identity_foundation.sql`, `src/identity/store.py`, and `src/graph_api.py`

## Mission & Objectives
Investigate and design Service Identities and Security Boundaries:
1. Define service principals:
   - `bff_service`: External API caller for the web/BFF layer. Credentials, token format, allowed scopes, rate limits.
   - `graph_service`: Internal caller executing Graph API transitions.
   - Worker boundary: Scoped service tokens for future workers; strict prohibition against shared root/omnipotent keys.
2. Anti-Impersonation Boundary:
   - Strict rule: Services and agents CANNOT impersonate humans. Services cannot authenticate as humans or commit human decisions.
   - Human decisions require human authentication evidence (`proof_type=webauthn` or scoped human action token).
3. Gamertag Display Separation:
   - Gamertags are purely display metadata; authorization keys exclusively on immutable `user_id` / `player_id` UUIDs.
4. Integration with Graph API (`src/graph_api.py`):
   - How requests to `/approval/packages/{package_id}/decide` and other endpoints authenticate caller principals and enforce `policy.authorize()`.
5. Deliver complete service credential design, authentication middleware/helpers, boundary guards, and test design.

Write report to: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_services_1\handoff.md`.

## 2026-10-04T03:54:20Z
[Message] timestamp=2026-10-04T03:54:20Z sender=25210876-a9ad-4f78-a87c-dec88e86edb9 priority=MESSAGE_PRIORITY_HIGH
You are explorer_m2_services_1, a Read-only exploration agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_services_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_services_1\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md before starting work.

Your task:
1. Investigate and design Service Identities and Security Boundaries for Phase 2 Milestone 2.
2. Define service principals:
   - bff_service: Dedicated service identity for BFF API caller; narrow scope, no direct DB access.
   - graph_service: Dedicated internal identity for Graph API runtime; least-privileged DB role (fc27_app).
   - worker boundary: Scope-limited service identities for future worker; strict prohibition against shared root/omnipotent keys.
3. Design anti-impersonation boundaries:
   - Strict rule: Services and agents CANNOT impersonate humans. Services cannot authenticate as humans or commit human decisions.
   - Human decisions require authentic human credentials/evidence (WebAuthn step-up or scoped action tokens).
4. Design gamertag display separation:
   - Gamertags are display data only; authorization keys strictly on immutable UUIDs (user_id, player_id).
5. Integration with Graph API (src/graph_api.py):
   - How requests to /approval/packages/{package_id}/decide authenticate caller principals and enforce policy.authorize().
6. Provide exact credential loading, authentication helpers, boundary guards, and test design.
7. Deliver your complete handoff report to:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_services_1\handoff.md
When finished, notify me with a summary and the path to your handoff report via send_message.
