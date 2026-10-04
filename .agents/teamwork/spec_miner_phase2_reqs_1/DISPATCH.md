# Task Assignment — spec_miner_phase2_reqs_1

## Objective
Extract the comprehensive specification and requirements for Phase 2: Identity and Authorization from `ORIGINAL_REQUEST.md`.

## Authoritative Inputs
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (Focus on `## 2026-10-04T02:30:08Z` and Section 1A patch at `## 2026-10-03T14:11:05Z`)

## Scope & Constraints
- Read-only specification extraction.
- Enumerate all features, acceptance criteria, security invariants, API endpoints, data models, and non-negotiables.
- Categorize by functional workstream (Users, Roles, Invitations, Sessions, WebAuthn, Step-up, Player Decisions, TOTP, Secrets, Service Identities, Delegations, Bootstrap/Recovery, Audit, Graph API integration).
- Report at `.agents/teamwork/spec_miner_phase2_reqs_1/handoff.md`.

## 2026-10-04T02:35:31Z
[Message] timestamp=2026-10-04T02:35:31Z sender=25210876-a9ad-4f78-a87c-dec88e86edb9 priority=MESSAGE_PRIORITY_HIGH content=You are spec_miner_phase2_reqs_1, a Read-only specification investigator.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\spec_miner_phase2_reqs_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\spec_miner_phase2_reqs_1\DISPATCH.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md before starting work (focusing on header ## 2026-10-04T02:30:08Z and Section 1A patch at ## 2026-10-03T14:11:05Z).

Your task:
1. Extract all requirements, invariants, acceptance criteria, security checks, and endpoints for Phase 2: Identity and Authorization.
2. Structure all features into a comprehensive inventory:
   - Users & account states
   - Application roles & object-level authorization
   - Invitations & activation
   - Sessions (server-side, revocation, rotation)
   - WebAuthn / Passkeys (registration & sign-in ceremonies)
   - Decision step-up (Commissioner & Player)
   - Player passkeys & player decisions (accepted, rejected, changes_requested, paused, acknowledged)
   - Action tokens & decision challenges
   - Staff TOTP
   - Secrets & cryptography (encryption at rest, key rotation, no secret leaks)
   - Service identities (BFF, Graph, Worker boundaries)
   - Explicit human delegation authorization
   - Commissioner bootstrap & recovery
   - Identity audit events
   - Graph API integration (transitioning from bootstrap HMAC to authenticated human evidence)
   - Security adversarial tests & acceptance traces
   - Non-negotiable invariants (DISPATCH_ENABLED=false, DISPATCH_WORKER_ENABLED=false, zero player delivery, no timeout approval)
3. Write your complete specification handoff report to:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\spec_miner_phase2_reqs_1\handoff.md
When finished, notify me with a summary and the path to your handoff report via send_message.
