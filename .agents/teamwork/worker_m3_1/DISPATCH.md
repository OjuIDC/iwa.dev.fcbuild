# Dispatch Instructions for worker_m3_1

**Agent ID**: `worker_m3_1`
**Role**: Milestone 3 Implementation Worker
**Milestone**: Phase 2 Milestone 3 — Server-Side Sessions & Invitations Lifecycle
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_1`

## Authoritative Inputs
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (Read §5, §6, §7, §8, §11, §12, §14)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
3. Explorer handoffs & blueprints:
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_invitations_1\handoff.md`
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_sessions_1\handoff.md`
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_api_mount_1\handoff.md`

## Mandatory Non-Negotiable Invariants
- `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` strictly enforced at all times.
- Zero player delivery; no active worker or relay.
- Plaintext tokens (sessions and invitations) are NEVER stored in PostgreSQL; only deterministic SHA-256 hashes (`hash_token()`).
- Single-use invitation consumption enforced under atomic `FOR UPDATE` lock.
- User account status transition strictly respects `fc27_users_status_guard`: `invited` -> `active`.
- Dual session timeouts: 30-minute idle expiration strictly capped at 24-hour absolute expiration.
- Suspended or disabled accounts fail closed immediately on session validation (HTTP 401).
- Session rotation on privilege change revokes previous session and issues new session in a single transaction.

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

## File Ownership
You exclusively own and may create/edit:
- `src/identity/routers/__init__.py`
- `src/identity/routers/invitations.py`
- `src/identity/routers/sessions.py`
- `src/identity/routers/users.py`
- `src/identity/store.py` (extend store methods)
- `src/graph_api.py` (mount identity routers, attach identity_store, add exception handlers)
- `tests/identity/test_routers_invitations.py`
- `tests/identity/test_routers_sessions.py`
- `tests/identity/test_routers_users.py`

## Implementation Steps
1. **Extend `src/identity/store.py`**:
   - Verify `create_invitation` outputs 64-hex char plaintext token (`secrets.token_hex(32)`) and stores SHA-256 hash. Support `login_identifier` / `invited_email`.
   - Implement `validate_invitation(token)` returning sanitized status (active/expired/consumed/revoked).
   - Implement `consume_invitation_and_activate_user(token, display_name=None, actor="user")` under atomic `FOR UPDATE` lock. Transition user status from `invited` to `active`, assign role in `fc27_user_roles`, and emit domain events (`identity.invitation_consumed`, `identity.status_changed`, `identity.role_granted`).
   - Implement `revoke_invitation(invitation_id, actor="commissioner")` failing closed if already consumed or revoked.
   - Implement missing session methods:
     - `list_user_sessions(user_id, current_session_id=None)`
     - `get_session_by_id(session_id)`
     - `touch_session_by_token(token, idle_ttl_minutes=30)`
     - `revoke_session_by_token(token, actor="user")`
   - Implement user status methods:
     - `suspend_user(user_id, actor="commissioner")`
     - `get_user_by_id(user_id)`
     - `list_users(club_id)`

2. **Implement Identity Routers**:
   - `src/identity/routers/__init__.py`
   - `src/identity/routers/invitations.py`:
     - `POST /v1/auth/invitations`: Requires Commissioner/Staff. Body: `{"login_identifier": "...", "display_name": "...", "club_id": "...", "role": "...", "expires_in_hours": 48}`.
     - `GET /v1/auth/invitations/{token}/validate`: Public check.
     - `POST /v1/auth/invitations/{token}/accept` and `/consume`: Public activation. Body: `{"display_name": "..."}`. Returns `status="active"`, `role`, `user_id`.
     - `DELETE /v1/auth/invitations/{invitation_id}`: Requires Commissioner.
   - `src/identity/routers/sessions.py`:
     - `GET /v1/auth/session` (singular, for E2E tests) and `GET /v1/auth/me`: Inspect caller session & principal.
     - `POST /v1/auth/sessions/touch`: Extend idle timeout up to absolute ceiling.
     - `POST /v1/auth/sessions/rotate`: Atomic rotation.
     - `POST /v1/auth/logout` and `DELETE /v1/auth/sessions/me`: Revoke caller session.
     - `DELETE /v1/auth/sessions/{session_id}`: Admin revoke session.
     - `GET /v1/auth/sessions`: List active sessions.
     - `DELETE /v1/auth/sessions`: Bulk revoke user sessions.
   - `src/identity/routers/users.py`:
     - `POST /v1/auth/users/{user_id}/suspend`: Suspend user.
     - `GET /v1/auth/users/{user_id}`: Get user.
     - `GET /v1/auth/users`: List users in club.

3. **Mount in `src/graph_api.py`**:
   - In `create_app()`: instantiate and attach `app.state.identity_store = IdentityStore(dsn=settings.database_url)`.
   - Add exception handlers for `IdentityStoreError` (mapping `status_code` and `detail`) and `AuthorizationError` (mapping `status_code=403`).
   - Include routers with prefixes `/v1/auth/invitations`, `/v1/auth`, and `/v1/auth/users`.

4. **Author Test Suites**:
   - `tests/identity/test_routers_invitations.py` (test create, validate, consume, replay 409, expired 410, revoked 410, cross-club 403, player cannot invite 403).
   - `tests/identity/test_routers_sessions.py` (test inspect, touch, idle expiry, absolute expiry, rotate, logout, revoke by id, suspended user 401).
   - `tests/identity/test_routers_users.py` (test suspend, get, list).

5. **Execute Verification**:
   - Run pytest on packaging and scope: `pytest tests/test_packaging.py tests/test_phase1_scope.py -v`
   - Run pytest on all identity test suites: `pytest tests/identity/ -v`
   - Run relevant E2E tests: `pytest tests/e2e/test_phase2_tier1_features.py -k "invitation or session" -v`

6. **Deliver Hard Handoff Report**:
   - Write report to: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_1\handoff.md`.


## 2026-10-04T04:39:16Z
You are worker_m3_1, a versatile worker agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_1\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md

Explorer handoffs to implement:
- c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_invitations_1\handoff.md
- c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_sessions_1\handoff.md
- c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_api_mount_1\handoff.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md before starting work.

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

File ownership:
You exclusively own and may edit/create:
- src/identity/routers/__init__.py
- src/identity/routers/invitations.py
- src/identity/routers/sessions.py
- src/identity/routers/users.py
- src/identity/store.py
- src/graph_api.py
- tests/identity/test_routers_invitations.py
- tests/identity/test_routers_sessions.py
- tests/identity/test_routers_users.py

Implement Milestone 3 per your DISPATCH.md and Explorer handoffs:
1. Extend src/identity/store.py with missing methods: list_user_sessions, get_session_by_id, touch_session_by_token, revoke_session_by_token, validate_invitation, consume_invitation_and_activate_user, suspend_user, get_user_by_id, list_users. Ensure token generation is 64-char hex, stored as SHA-256 hash.
2. Implement src/identity/routers/invitations.py, src/identity/routers/sessions.py, src/identity/routers/users.py, and src/identity/routers/__init__.py.
3. Update src/graph_api.py: attach app.state.identity_store, add exception handlers for IdentityStoreError and AuthorizationError, include identity routers.
4. Author comprehensive test suites: tests/identity/test_routers_invitations.py, test_routers_sessions.py, test_routers_users.py.
5. Execute verification commands:
   - .\.venv\Scripts\pytest.exe tests/test_packaging.py tests/test_phase1_scope.py -v
   - .\.venv\Scripts\pytest.exe tests/identity/ -v
   - .\.venv\Scripts\pytest.exe tests/e2e/test_phase2_tier1_features.py -k "invitation or session" -v
6. Deliver complete hard handoff report to:
   c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_1\handoff.md
When finished, notify me with your report path and verification results via send_message.
