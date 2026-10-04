# BRIEFING — 2026-10-04T04:36:00Z

## Mission
Investigate and design `src/identity/routers/invitations.py` and account lifecycle management for Phase 2 Milestone 3.

## 🔒 My Identity
- Archetype: explorer
- Roles: Invitations & User Lifecycle Designer
- Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_invitations_1
- Original parent: 25210876-a9ad-4f78-a87c-dec88e86edb9
- Milestone: Phase 2 Milestone 3

## 🔒 Key Constraints
- Read-only investigation — do NOT implement
- Token security: high-entropy hex (32 bytes / 64 hex chars), stored in DB strictly as deterministic SHA-256 digest (`hash_token(token)`)
- Single-use consumption enforced atomically under `FOR UPDATE`
- User status machine respects `fc27_users_status_guard`: transitions `invited` -> `active`, cannot activate `disabled` accounts
- Role assignment in `fc27_user_roles`
- Domain event logging (`identity.invitation_created`, `identity.invitation_consumed`, `identity.invitation_revoked`)
- Write only to working directory `.agents/teamwork/explorer_m3_invitations_1/`

## Current Parent
- Conversation ID: 25210876-a9ad-4f78-a87c-dec88e86edb9
- Updated: 2026-10-04T04:30:19Z

## Investigation State
- **Explored paths**:
  - `migrations/0004_identity_foundation.sql`: Tables `fc27_users`, `fc27_user_roles`, `fc27_invitations`, trigger `fc27_users_status_guard`.
  - `src/identity/store.py`: `create_user`, `assign_role`, `create_invitation`, `get_invitation_by_token`, `consume_invitation`, `revoke_invitation`.
  - `src/identity/crypto.py`: `hash_token`.
  - `src/identity/policy.py`: `ActionType.INVITATION_*`, RBAC permissions matrix for Commissioner and Staff, `authorize()` engine.
  - `src/identity/deps.py`: `get_current_principal`, `require_human_principal`.
  - `tests/e2e/test_phase2_tier1_features.py` & `test_phase2_tier2_boundaries.py`: Existing tests for `/v1/auth/invitations` creation, revocation, and acceptance (`/accept` alias).
- **Key findings**:
  1. Token Generation: Must use `secrets.token_hex(32)` (64 hex characters / 32 bytes) rather than `token_urlsafe(32)`. SHA-256 hash matches `CHECK (token_hash ~ '^[0-9a-f]{64}$')`.
  2. Endpoints Needed: `POST /v1/auth/invitations`, `GET /v1/auth/invitations/{token}/validate`, `POST /v1/auth/invitations/{token}/consume` (plus `/accept` alias for tier test compatibility), and `DELETE /v1/auth/invitations/{invitation_id}`.
  3. Status Machine & Trigger: Trigger `fc27_guard_user_status_transition` fires on `BEFORE UPDATE ON fc27_users`. Rejects reactivation of `disabled` accounts. Validates `invited` -> `active` and auto-sets `activated_at = now()`.
  4. Store Atomicity: Need atomic store method `consume_invitation_and_activate_user` executing under `FOR UPDATE` on both `fc27_invitations` and `fc27_users`.
  5. Domain Events: Must log `identity.invitation_created` (or `identity.invitation_issued`), `identity.invitation_consumed`, and `identity.invitation_revoked`.
- **Unexplored areas**: None for invitations lifecycle.

## Key Decisions Made
- Token format: 64 hex characters (32 bytes entropy).
- Dual routing for consumption: `POST /{token}/consume` as primary, `POST /{token}/accept` as alias to satisfy tier tests without breaking spec.
- Pydantic models: Built with `hide_input_in_errors=True` and `extra="forbid"` to prevent information leakage in error messages.
- Revocation of already-revoked invitation raises 409 to satisfy boundary test assertion `res.status_code in (404, 409)`.

## Artifact Index
- [c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_invitations_1\BRIEFING.md] — Persistent situational awareness
- [c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_invitations_1\DISPATCH.md] — Dispatch instructions
- [c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_invitations_1\progress.md] — Liveness heartbeat
- [c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_invitations_1\handoff.md] — Final handoff report
