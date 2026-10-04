# Dispatch Instructions for explorer_m3_invitations_1

**Agent ID**: `explorer_m3_invitations_1`
**Role**: Invitations & User Lifecycle Designer
**Milestone**: Phase 2 Milestone 3 — Server-Side Sessions & Invitations Lifecycle
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_invitations_1`

## Authoritative Inputs
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (Read §5, §6, §7, §8, §12)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
3. Existing schema & store: `migrations/0004_identity_foundation.sql` (`fc27_invitations`, `fc27_users`, `fc27_user_roles`), `src/identity/store.py`

## Mission & Objectives
Investigate and design `src/identity/routers/invitations.py` and account lifecycle management:
1. Endpoints:
   - `POST /v1/auth/invitations`: Create new invitation. Requires Commissioner or authorized Staff. Validates intended role, expiration window, email, club ID. Returns one-time plaintext invitation token.
   - `GET /v1/auth/invitations/{token}/validate`: Public endpoint to inspect invitation validity (not expired, not consumed, not revoked, returns club, intended role, email).
   - `POST /v1/auth/invitations/{token}/consume`: Consume invitation to activate/provision user. Atomically creates user/assigns roles/marks invitation consumed.
   - `DELETE /v1/auth/invitations/{invitation_id}`: Revoke invitation before consumption.
2. Token Security & Storage:
   - Plaintext token is high-entropy hex string (32 bytes / 64 hex chars).
   - Stored in DB strictly as deterministic SHA-256 digest (`token_hash = hash_token(token)`).
   - Single-use consumption enforced atomically (`consumed_at IS NULL` under `FOR UPDATE`).
3. User Provisioning & Status State Machine:
   - User creation respects `fc27_users_status_guard` trigger: transitions from `invited` to `active`.
   - Cannot activate disabled accounts.
   - Role assignment: attaches intended role in `fc27_user_roles`.
   - Domain event logging: `identity.invitation_created`, `identity.invitation_consumed`, `identity.invitation_revoked`.
4. Deliver complete endpoint blueprints, Pydantic schemas, store methods needed, and test design.

Write report to: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_invitations_1\handoff.md`.


## 2026-10-04T04:30:19Z
You are explorer_m3_invitations_1, a Read-only exploration agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_invitations_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_invitations_1\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md before starting work.

Your task:
Investigate and design src/identity/routers/invitations.py and account lifecycle management for Phase 2 Milestone 3:
1. Inspect migrations/0004_identity_foundation.sql (fc27_invitations, fc27_users, fc27_user_roles), src/identity/store.py, src/identity/crypto.py, and E2E test helpers in tests/e2e/.
2. Design invitation endpoints:
   - POST /v1/auth/invitations: Commissioner or authorized Staff creates invitation. Validates role, expiry, email, club ID. Returns one-time plaintext token.
   - GET /v1/auth/invitations/{token}/validate: public validation (not expired, not consumed, not revoked).
   - POST /v1/auth/invitations/{token}/consume: consume token, activate/provision user.
   - DELETE /v1/auth/invitations/{invitation_id}: revoke invitation.
3. Token security & storage:
   - Plaintext token high-entropy hex (32 bytes / 64 hex chars).
   - Stored in DB strictly as deterministic SHA-256 digest (hash_token(token)).
   - Single-use consumption enforced atomically under FOR UPDATE.
4. User provisioning & status machine:
   - User creation respects fc27_users_status_guard: invited -> active.
   - Cannot activate disabled accounts.
   - Role assignment in fc27_user_roles.
   - Domain event logging (identity.invitation_created, identity.invitation_consumed, identity.invitation_revoked).
5. Provide exact endpoint blueprints, Pydantic schemas, store methods needed, and test recommendations.
6. Deliver complete handoff report to:
   c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_invitations_1\handoff.md
When finished, notify me with a summary and the path to your handoff report via send_message.
