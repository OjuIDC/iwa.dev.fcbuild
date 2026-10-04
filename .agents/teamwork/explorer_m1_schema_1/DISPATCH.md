# Task Assignment — explorer_m1_schema_1

## Objective
Design the complete SQL schema and migration definition for Phase 2 Milestone 1: Identity Data Foundation.

## Authoritative Inputs
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (§5, §17)
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
- Existing migrations: `migrations/0001_initial.sql`, `0002_approval_security.sql`, `0003_dispatch_foundation.sql`, `migrations/post/grants.sql`

## Scope & Deliverables
- Design `migrations/0004_identity_foundation.sql`:
  - `fc27_users`: UUID PK, club_id, login_id (unique, normalized), display_name, status (`invited`, `active`, `suspended`, `recovery_required`, `disabled`), timestamps.
  - `fc27_user_roles`: user_id FK, role (`commissioner`, `commissioner_delegate`, `coach`, `analyst`, `staff`, `player`, `bff_service`, `graph_service`), granted_at, granted_by.
  - `fc27_invitations`: invitation_id UUID PK, token_hash (SHA-256), club_id, intended_role, invited_email, creator_id, issued_at, expires_at, consumed_at, revoked_at.
  - `fc27_sessions`: session_id UUID PK, token_hash (SHA-256), user_id FK, auth_level, auth_method, ip_address, user_agent, created_at, last_used_at, idle_expires_at, absolute_expires_at, revoked_at.
  - `fc27_webauthn_credentials`: credential_id (bytea PK), user_id FK, public_key (bytea), sign_counter (bigint), label, aaguid, created_at, last_used_at, revoked_at.
  - `fc27_totp_credentials`: user_id PK/FK, encrypted_seed, key_version, status (`enrolled`, `active`, `revoked`), enrolled_at, last_verified_at, revoked_at.
  - `fc27_recovery_codes`: code_id UUID PK, user_id FK, code_hash (Argon2id), consumed_at, created_at.
  - `fc27_action_tokens`: token_id UUID PK, token_hash (SHA-256), user_id FK, assignment_id UUID NULL, scope, expires_at, consumed_at, revoked_at.
  - `fc27_challenges`: challenge_id UUID PK, challenge_type, challenge_str, user_id NULL, bound_payload_hash NULL, expires_at, consumed_at.
  - Append-only trigger attachment: `fc27_reject_mutation()` on recovery codes and audit logs.
  - Grants update in `migrations/post/grants.sql` for `fc27_app`.
- Provide exact SQL DDL and recommendation in handoff report.
- Report at `.agents/teamwork/explorer_m1_schema_1/handoff.md`.
## 2026-10-04T02:46:14Z
From: 25210876-a9ad-4f78-a87c-dec88e86edb9 (parent)

You are explorer_m1_schema_1, a Read-only exploration agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_schema_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_schema_1\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md before starting work.

Your task:
1. Design the exact SQL schema and migration definition for Phase 2 Milestone 1: Identity Data Foundation.
2. Define migrations/0004_identity_foundation.sql:
   - fc27_users (UUID PK, club_id, login_id, display_name, status, timestamps)
   - fc27_user_roles (user_id, role, granted_at, granted_by)
   - fc27_invitations (invitation_id, token_hash, club_id, intended_role, invited_email, creator_id, issued_at, expires_at, consumed_at, revoked_at)
   - fc27_sessions (session_id, token_hash, user_id, auth_level, auth_method, ip_address, user_agent, created_at, last_used_at, idle_expires_at, absolute_expires_at, revoked_at)
   - fc27_webauthn_credentials (credential_id bytea PK, user_id, public_key, sign_counter, label, aaguid, created_at, last_used_at, revoked_at)
   - fc27_totp_credentials (user_id PK, encrypted_seed, key_version, status, enrolled_at, last_verified_at, revoked_at)
   - fc27_recovery_codes (code_id, user_id, code_hash, consumed_at, created_at)
   - fc27_action_tokens (token_id, token_hash, user_id, assignment_id, scope, expires_at, consumed_at, revoked_at)
   - fc27_challenges (challenge_id, challenge_type, challenge_str, user_id, bound_payload_hash, expires_at, consumed_at)
   - Attach fc27_reject_mutation() triggers on append-only tables
3. Define updates to migrations/post/grants.sql for fc27_app least-privilege DML.
4. Deliver your complete recommendations and SQL definitions in your handoff report to:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_schema_1\handoff.md
When finished, notify me with a summary and the path to your handoff report via send_message.
