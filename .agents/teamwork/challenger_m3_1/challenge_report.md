# Adversarial Challenge Report: Phase 2 Milestone 3 (Server-Side Sessions & Invitations Lifecycle)

**Agent ID**: `challenger_m3_1`  
**Role**: Empirical Challenger (critic, specialist)  
**Target Milestone**: Phase 2 Milestone 3 — Server-Side Sessions & Invitations Lifecycle  
**Worker Under Review**: `worker_m3_1`  
**Timestamp**: 2026-10-04T05:25:00Z  

---

## Challenge Summary

**Overall risk assessment**: HIGH  
**Verdict**: **REQUEST_CHANGES**

While the core data structures in `src/identity/store.py` and the PostgreSQL schema (`fc27_sessions`, `fc27_invitations`) correctly implement SHA-256 token hashing, dual-expiry checks in `get_session_by_token`, and user status fail-closed semantics, adversarial stress-testing identified **three major defects and gaps**:
1. **Worker Test Suite Fatal Crashes**: The worker's primary test `tests/identity/test_routers_sessions.py` has two fatal bugs:
   - Line 154 executes SQL `UPDATE public.fc27_sessions SET expires_at = ...`, which crashes in PostgreSQL with `UndefinedColumn` because the column is `absolute_expires_at` (there is no `expires_at` column in `fc27_sessions`).
   - Line 165 reads `data["expires_at"]`, which crashes with `KeyError` because `SessionTouchResponse` only returns `idle_expires_at` and `absolute_expires_at`.
2. **Anti-Impersonation Boundary Gaps**: Service tokens (BFF, Graph, Worker) calling `GET /v1/auth/sessions` and `DELETE /v1/auth/sessions` without `?user_id=` are NOT rejected with HTTP 403; they return HTTP 200 OK because `require_human_principal` is omitted and `target_user_id == principal.user_id` skips `authorize()`. Furthermore, `GET /v1/auth/session` synthesizes a fake human commissioner session for service tokens instead of failing closed with 403.
3. **Session Rotation Race Condition**: In `store.rotate_session()`, reading the old session and revoking it occur in two separate transactions without a `WHERE revoked_at IS NULL` atomic update check, opening a concurrency race where duplicate rotated sessions can be issued.

---

## Challenges

### [High] Challenge 1: Worker Test `test_routers_sessions.py` Fails with Fatal Database and Schema Errors

- **Assumption challenged**: The worker claimed that `tests/identity/test_routers_sessions.py` is fully verified, structurally compliant, and ready for execution.
- **Attack scenario**: Executing `pytest tests/identity/test_routers_sessions.py -v` executes `test_session_touch_capped_at_absolute_timeout`:
  - Line 154 runs:
    ```sql
    UPDATE public.fc27_sessions 
    SET expires_at = now() + interval '10 minutes',
        idle_expires_at = now() + interval '2 minutes' 
    WHERE session_id = %s
    ```
    In `migrations/0004_identity_foundation.sql`, `fc27_sessions` defines `absolute_expires_at` and `idle_expires_at`. There is no `expires_at` column. PostgreSQL immediately aborts the transaction with `psycopg.errors.UndefinedColumn`.
  - Line 165 executes `abs_exp = datetime.fromisoformat(data["expires_at"])`. In `src/identity/routers/sessions.py`, `SessionTouchResponse` defines `idle_expires_at` and `absolute_expires_at`, but no `expires_at`. Python immediately raises `KeyError: 'expires_at'`.
- **Blast radius**: Continuous integration and automated test suites fail completely. The dual-expiry capping invariant remains unverified in CI until corrected.
- **Mitigation**:
  1. Fix `tests/identity/test_routers_sessions.py` line 154 to `SET absolute_expires_at = now() + interval '10 minutes'`.
  2. Fix line 165 to read `data["absolute_expires_at"]`.

---

### [High] Challenge 2: Service Tokens Bypass Anti-Impersonation on Human Session Management Endpoints

- **Assumption challenged**: Automated services (BFF, Graph, Worker) cannot interact with or manage human sessions (§3.3 and Dispatch Task 4).
- **Attack scenario**:
  1. Service caller (e.g. `X-API-Key: bff_service_test_token...`) calls `GET /v1/auth/sessions`:
     `list_active_sessions` in `src/identity/routers/sessions.py` uses `Depends(get_current_principal)`. It checks `if target_user_id != principal.user_id:`. Since no query parameter was passed, `target_user_id == principal.user_id`. It skips `authorize()` and returns `SessionListResponse` with HTTP 200 OK!
  2. Service caller calls `DELETE /v1/auth/sessions`:
     `revoke_all_sessions` similarly skips `authorize()` and returns HTTP 200 OK.
  3. Service caller calls `GET /v1/auth/session` (or `/me`):
     `_build_session_response` explicitly checks `if principal.is_service:`, synthesizes a fake session with `display_name="Commissioner"`, and returns HTTP 200 OK!
- **Blast radius**: Service tokens are machine-to-machine credentials and must NEVER have or manage server-side user sessions. Allowing services to inspect sessions or receive synthetic commissioner sessions violates the zero agent-human impersonation invariant.
- **Mitigation**:
  1. Protect `GET /v1/auth/sessions` and `DELETE /v1/auth/sessions` with `require_human_principal` dependency.
  2. In `_build_session_response`, remove the `principal.is_service` branch and raise `AgentImpersonationError` (HTTP 403) if a service token attempts to access `/v1/auth/session`.

---

### [Medium] Challenge 3: Atomic Session Rotation Lacks Single-Transaction Concurrency Lock

- **Assumption challenged**: `store.rotate_session()` atomically revokes the old session and creates the new session in a single transaction.
- **Attack scenario**:
  In `src/identity/store.py` lines 828-837:
  ```python
  old_sess = self.get_session_by_token(old_token, touch=False)  # Transaction 1: commits and releases lock
  if not old_sess:
      raise IdentityStoreError(401, ...)
  with self.connect() as conn, conn.transaction():              # Transaction 2: opens new connection
      conn.execute("UPDATE public.fc27_sessions SET revoked_at = now() WHERE session_id = %s", ...)
  ```
  Two concurrent step-up requests with the same bearer token can both execute Transaction 1 before Transaction 2 executes in either thread. Both see `revoked_at IS NULL`, and both proceed to Transaction 2 to issue two active rotated sessions for the same prior token.
- **Blast radius**: Potential session splitting and credential multiplication during concurrent requests.
- **Mitigation**:
  Perform the session lookup and revocation in a single transaction with an atomic conditional update:
  `UPDATE public.fc27_sessions SET revoked_at = now() WHERE session_id = %s AND revoked_at IS NULL RETURNING *`.
  If zero rows are updated, raise `IdentityStoreError(401, "session already revoked or rotated")`.

---

## Stress Test Results

| Scenario | Expected Behavior | Actual Behavior | Pass/Fail |
|---|---|---|---|
| **Dual-Expiry: Idle Expired (>30m)** | HTTP 401 fail-closed on `/session`, `/touch`, `/rotate` even with 23h remaining | Fails closed with 401 via `get_session_by_token` | **PASS** |
| **Dual-Expiry: Absolute Expired (>24h)** | HTTP 401 fail-closed on `/session`, `/touch`, `/rotate` even with fresh idle | Fails closed with 401 via `get_session_by_token` | **PASS** |
| **Dual-Expiry: Touch Ceiling** | `idle_expires_at` strictly capped at `absolute_expires_at` in DB | Clamped to absolute in DB; worker test crashes on line 154/165 | **FAIL (Test Defect)** |
| **Rotation: Revocation of Old Token** | Old token fails closed with 401; new token active with 200 | Old token immediately 401; new token 200 | **PASS** |
| **Rotation: Replay Prevention** | Replaying rotation with old token returns 401 | Returns 401 | **PASS** |
| **User Status: Suspension Invalidation** | Suspending user immediately invalidates all active sessions across all devices | All sessions immediately return 401 | **PASS** |
| **User Status: Disabled Invalidation** | Disabled account cannot authenticate even if session row has `revoked_at IS NULL` | Returns 401 (`user_status != 'active'`) | **PASS** |
| **Anti-Impersonation: Service /touch** | Calling `/v1/auth/sessions/touch` with BFF/Graph/Worker tokens returns 403 | Returns 403 `AgentImpersonationError` | **PASS** |
| **Anti-Impersonation: Service /rotate** | Calling `/v1/auth/sessions/rotate` with BFF/Graph/Worker tokens returns 403 | Returns 403 `AgentImpersonationError` | **PASS** |
| **Anti-Impersonation: Service /sessions** | Service tokens calling `/v1/auth/sessions` rejected with 403 | Returns HTTP 200 OK (omits human check) | **FAIL (Design Gap)** |
| **Anti-Impersonation: Service /session** | Service tokens calling `/v1/auth/session` rejected with 403 | Returns HTTP 200 OK (synthesizes commissioner) | **FAIL (Design Gap)** |
| **Player Isolation: Cross-Player View** | Player A querying Player B sessions returns 403 `PlayerIsolationError` | Returns 403 `PlayerIsolationError` | **PASS** |
| **Player Isolation: Cross-Player Revoke** | Player A revoking Player B session by ID returns 403 `PlayerIsolationError` | Returns 403 `PlayerIsolationError` | **PASS** |
| **Player Isolation: Bulk Revoke** | Player A bulk revoking Player B sessions returns 403 | Returns 403 `RoleNotPermittedError` | **PASS** |

---

## Unchallenged Areas

- **WebAuthn FIDO2 Ceremonies & Passkeys**: Passkey credential creation, counter verification, and signature assertions are scheduled for Milestone 4; only token hashing and sessions were challenged in Milestone 3.
- **Multi-Instance PostgreSQL Connection Pool Exhaustion**: Stress harness executed under single-instance test environment; distributed multi-node replication lag not challenged.
