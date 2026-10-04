# Dispatch Instructions for explorer_m3_sessions_1

**Agent ID**: `explorer_m3_sessions_1`
**Role**: Session Lifecycle & Timeout Designer
**Milestone**: Phase 2 Milestone 3 — Server-Side Sessions & Invitations Lifecycle
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_sessions_1`

## Authoritative Inputs
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (Read §5, §7, §8, §12)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
3. Existing schema & store: `migrations/0004_identity_foundation.sql` (`fc27_sessions`), `src/identity/store.py`, `src/identity/policy.py`, `src/identity/services.py`, `src/identity/deps.py`

## Mission & Objectives
Investigate and design `src/identity/routers/sessions.py` and session lifecycle management:
1. Endpoints:
   - `GET /v1/auth/sessions/me` or `/v1/auth/me`: Inspect current session, principal, auth level, roles, expiry timestamps.
   - `POST /v1/auth/sessions/touch` or automatic touch on authenticated request: Extend idle expiration up to absolute ceiling.
   - `POST /v1/auth/sessions/rotate`: Atomic rotation of session token on privilege escalation.
   - `DELETE /v1/auth/sessions/me` or `/v1/auth/sessions/{session_id}`: Explicit revocation/logout.
   - `GET /v1/auth/sessions`: List active sessions for the authenticated user.
2. Dual-Expiry Rules:
   - Idle expiration: 30 minutes from last activity.
   - Absolute expiration: 24 hours from session creation.
   - Touching a session extends `idle_expires_at` but MUST NOT exceed `absolute_expires_at`.
   - Expired sessions fail closed immediately with HTTP 401.
3. Session Rotation:
   - On step-up or privilege change, old session is revoked (`revoked_at = now()`) and a fresh session is issued in the same transaction. Old session ID cannot be reused.
4. Deliver complete endpoint blueprints, Pydantic request/response schemas, error handling, and test design.

Write report to: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_sessions_1\handoff.md`.


## 2026-10-04T04:30:19Z
[Message] timestamp=2026-10-04T04:30:19Z sender=25210876-a9ad-4f78-a87c-dec88e86edb9 priority=MESSAGE_PRIORITY_HIGH content=You are explorer_m3_sessions_1, a Read-only exploration agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_sessions_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_sessions_1\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md before starting work.

Your task:
Investigate and design src/identity/routers/sessions.py and session lifecycle management for Phase 2 Milestone 3:
1. Inspect migrations/0004_identity_foundation.sql (fc27_sessions), src/identity/store.py, src/identity/policy.py, src/identity/services.py, src/identity/deps.py, and E2E test helpers in tests/e2e/.
2. Design session endpoints:
   - GET /v1/auth/me or GET /v1/auth/sessions/me: current session, principal, roles, auth level, expiry timestamps.
   - POST /v1/auth/sessions/touch: extend idle expiration up to absolute ceiling.
   - POST /v1/auth/sessions/rotate: atomic rotation of session token on privilege escalation.
   - DELETE /v1/auth/sessions/me or DELETE /v1/auth/sessions/{session_id}: explicit revocation.
   - GET /v1/auth/sessions: list active sessions for user.
3. Dual-expiry rules:
   - 30-minute idle expiration from last activity.
   - 24-hour absolute expiration from creation.
   - Touch logic: idle_expires_at extended, strictly capped at absolute_expires_at.
   - Expired sessions fail closed immediately with 401.
4. Session rotation:
   - Old session revoked_at = now() and new session issued in the same transaction.
5. Provide exact endpoint blueprints, Pydantic schemas, store methods needed, and test recommendations.
6. Deliver complete handoff report to:
   c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_sessions_1\handoff.md
When finished, notify me with a summary and the path to your handoff report via send_message.
