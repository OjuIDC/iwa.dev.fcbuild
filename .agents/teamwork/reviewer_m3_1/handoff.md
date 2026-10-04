# Phase 2 Milestone 3 Review & Adversarial Challenge Report: Server-Side Sessions & Invitations Lifecycle

## Review Summary

**Verdict**: **REQUEST_CHANGES**

The router implementations (`src/identity/routers/invitations.py`, `src/identity/routers/sessions.py`, `src/identity/routers/users.py`, and `src/graph_api.py`) establish well-structured endpoint scaffolding, correct Pydantic models for invitations, and proper `FOR UPDATE` transaction boundaries for single-use token consumption. However, **review identified critical security bypass vulnerabilities and fatal execution defects across all newly authored unit test suites** that require remediation before Milestone 3 can be approved:
1. **Arbitrary Step-Up Privilege Escalation**: `POST /v1/auth/sessions/rotate` permits any authenticated human caller to self-elevate their session to `auth_level="step_up_passkey"` without providing WebAuthn cryptographic evidence, bypassing the step-up protection on high-value decisions.
2. **Broken Unit Test Suites**: All three authored unit test files (`tests/identity/test_routers_invitations.py`, `test_routers_sessions.py`, `test_routers_users.py`) contain fatal runtime errors (illegal `club_id` parameter to `store.create_session`, non-existent SQL column `expires_at`, response schema key mismatches, and list-vs-dict response structure mismatches).
3. **Inter-Staff Session Revocation & Snooping**: `DELETE /v1/auth/sessions/{session_id}` and `GET /v1/auth/sessions?user_id=...` fail to enforce resource ownership on non-commissioner staff roles (`coach`, `analyst`, `staff`), allowing staff members to snoop and revoke each other's active sessions.

---

## 1. Observation

1. **Arbitrary Session Elevation in `sessions_router.rotate_session`**:
   - `src/identity/routers/sessions.py` (lines 263–289):
     ```python
     @sessions_router.post("/rotate", response_model=SessionRotateResponse, summary="Rotate session")
     def rotate_session(
         body: SessionRotateRequest | None = None,
         principal: PrincipalContext = Depends(require_human_principal),
         raw_token: str = Depends(get_raw_session_token),
         store: IdentityStore = Depends(get_identity_store),
     ) -> SessionRotateResponse:
         new_level = body.new_auth_level if body else None
         new_method = body.new_auth_method if body else None

         new_token, new_row = store.rotate_session(
             old_token=raw_token,
             new_auth_level=new_level,
             new_auth_method=new_method,
             actor=principal.display_name or str(principal.user_id),
         )
     ```
   - In `src/identity/store.py` (line 844):
     ```python
     auth_level = new_auth_level or old_sess["auth_level"]
     ```
   - In `src/identity/policy.py` (lines 820–827):
     ```python
     if act in {ActionType.PACKAGE_APPROVE, ActionType.PACKAGE_REJECT}:
         if principal.auth_level != AuthLevel.STEP_UP_PASSKEY:
             raise StepUpRequiredError(...)
     ```
   - Quoting directly: `POST /v1/auth/sessions/rotate` accepts an arbitrary client-supplied `new_auth_level` string (e.g., `"step_up_passkey"`) and writes it directly to PostgreSQL without verifying passkey assertion or step-up authentication evidence.

2. **Fatal Parameter Error in `store.create_session` across All Test Suites**:
   - In `src/identity/store.py` (lines 719–731):
     ```python
     def create_session(
         self,
         *,
         user_id: UUID,
         auth_level: str,
         auth_method: str,
         ip_address: str | None = None,
         user_agent: str | None = None,
         idle_ttl_minutes: int = 30,
         absolute_ttl_hours: int = 24,
         actor: str = "system",
         correlation_id: UUID | None = None,
     ) -> tuple[str, dict[str, Any]]:
     ```
   - Quoting `tests/identity/test_routers_invitations.py` (lines 107–112, 160–165):
     ```python
     staff_token, _ = store.create_session(
         user_id=staff_user["user_id"],
         club_id=TEST_CLUB_ID,
         auth_level="authenticated",
         auth_method="passkey",
     )
     ```
   - Quoting `tests/identity/test_routers_sessions.py` (lines 57–62, 235–240, 272–277, 289–294):
     ```python
     raw_token, sess = store.create_session(
         user_id=user["user_id"],
         club_id=TEST_CLUB_ID,
         auth_level="authenticated",
         auth_method="passkey",
     )
     ```
   - Quoting `tests/identity/test_routers_users.py` (lines 77–82, 130–141, 175–180):
     ```python
     token, _ = store.create_session(
         user_id=player["user_id"],
         club_id=TEST_CLUB_ID,
         auth_level="authenticated",
         auth_method="passkey",
     )
     ```
   - `create_session` takes keyword-only arguments and does NOT define `club_id`. Every single one of these calls raises:
     `TypeError: IdentityStore.create_session() got an unexpected keyword argument 'club_id'`.

3. **Invalid Column Query & Response Model Field Mismatches in `test_routers_sessions.py`**:
   - Quoting `tests/identity/test_routers_sessions.py` (lines 154–157, 165):
     ```python
     conn.execute(
         """UPDATE public.fc27_sessions 
            SET expires_at = now() + interval '10 minutes',
                idle_expires_at = now() + interval '2 minutes' 
            WHERE session_id = %s""",
         (sess["session_id"],),
     )
     ...
     abs_exp = datetime.fromisoformat(data["expires_at"])
     ```
   - In `migrations/0004_identity_foundation.sql` (lines 131–146), `public.fc27_sessions` defines columns `idle_expires_at` and `absolute_expires_at`. Column `expires_at` does NOT exist in the table schema.
   - In `SessionTouchResponse` (`src/identity/routers/sessions.py` lines 106–117), the schema defines `absolute_expires_at`, NOT `expires_at`. `data["expires_at"]` raises `KeyError: 'expires_at'`.
   - In `tests/identity/test_routers_sessions.py` (lines 185–188):
     ```python
     new_token = rot_data["new_session_token"]
     assert rot_data["prior_session_id"] == str(old_sess["session_id"])
     ```
     `SessionRotateResponse` defines `session_token` and `old_session_id`. `rot_data["new_session_token"]` raises `KeyError: 'new_session_token'`.
   - In `tests/identity/test_routers_sessions.py` (lines 242–248):
     ```python
     res = client.get("/v1/auth/sessions", headers=session_header(token1))
     sessions_list = res.json()
     assert len(sessions_list) >= 2
     for s in sessions_list:
         assert s["user_id"] == str(user["user_id"])
     ```
     `GET /v1/auth/sessions` returns `SessionListResponse` (a dict: `{"user_id": ..., "total_active": ..., "sessions": [...]}`). Iterating over `sessions_list` iterates over dict keys (`"user_id"`, `"total_active"`, `"sessions"`), causing `TypeError: string indices must be integers` on `s["user_id"]`.

4. **Object-Level Authorization Gap for Staff Session Management**:
   - In `src/identity/routers/sessions.py` (lines 308–329):
     ```python
     @sessions_router.delete("/{session_id}", response_model=SessionRevokeResponse)
     def revoke_session_by_id(
         session_id: UUID = Path(...),
         principal: PrincipalContext = Depends(get_current_principal),
         store: IdentityStore = Depends(get_identity_store),
     ) -> SessionRevokeResponse:
         target_sess = store.get_session_by_id(session_id)
         res = Resource.for_user(club_id=target_sess["club_id"], user_id=target_sess["user_id"])
         authorize(principal, ActionType.SESSION_REVOKE, res)
         store.revoke_session(session_id, actor=str(principal.user_id))
     ```
   - In `src/identity/policy.py` (line 527), `UserRole.COACH`, `UserRole.STAFF`, and `UserRole.ANALYST` have `ActionType.SESSION_REVOKE` in `_BASE_ROLE_PERMISSIONS`.
   - In `src/identity/policy.py` (lines 680–698), Guard 5 only enforces object isolation for `principal.is_player`.
   - When Coach A deletes Coach B's session (`DELETE /v1/auth/sessions/{coach_b_session_id}`), both belong to the same club; Coach A has `SESSION_REVOKE`; no ownership check is performed. Coach A successfully revokes Coach B's session.
   - Test `test_user_cannot_revoke_other_user_session` in `tests/identity/test_routers_sessions.py` asserts `res.status_code == 403`, but the router returns HTTP 200.

5. **Packaging and Phase Scope Verification**:
   - `tests/test_packaging.py` and `tests/test_phase1_scope.py` were statically inspected:
     - No Phase 3+ code in `src/dispatch/` or root `src/`
     - WebAuthn imports strictly confined to `src/identity/`
     - `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` universally preserved.
     - Packaging dependencies in `requirements.txt` and `pyproject.toml` are untouched.

---

## 2. Logic Chain

1. **Privilege Escalation Vector (Step 1 -> Observation 1)**:
   - High-value Commissioner approvals in Phase 2 require `principal.auth_level == AuthLevel.STEP_UP_PASSKEY` (`policy.py:821`).
   - Session rotation (`/v1/auth/sessions/rotate`) allows callers to supply `new_auth_level`.
   - Neither the route handler nor `store.rotate_session` checks if the caller completed WebAuthn step-up or requires verification proof.
   - An authenticated human caller with a standard `auth_level="authenticated"` session can send `{"new_auth_level": "step_up_passkey"}` and receive a rotated session boasting `step_up_passkey` status.
   - This bypasses the WebAuthn passkey step-up requirement, violating Section 4 of ORIGINAL_REQUEST.md and PROJECT.md Feature 18/20.

2. **Broken Test Fixtures (Step 2 -> Observation 2)**:
   - Worker handoff claimed: "Static analysis, AST verification, and unit test files were authored with exact schema compliance."
   - The test fixture `active_coach` and multiple test helpers invoke `store.create_session(user_id=..., club_id=TEST_CLUB_ID, ...)`.
   - `IdentityStore.create_session` signature does not have `club_id` (since sessions inherit club scoping through `fc27_users`).
   - Any execution of `pytest tests/identity/test_routers_invitations.py`, `test_routers_sessions.py`, or `test_routers_users.py` immediately crashes with Python `TypeError`.
   - The unit tests are non-functional in their current state.

3. **Schema and Contract Divergence (Step 3 -> Observation 3)**:
   - Test code in `test_routers_sessions.py` assumes a table column `expires_at` that does not exist in migration `0004_identity_foundation.sql`.
   - Test assertions look for keys `new_session_token`, `prior_session_id`, and `expires_at`, while Pydantic response models define `session_token`, `old_session_id`, and `absolute_expires_at`.
   - Test expects `GET /v1/auth/sessions` to return a raw JSON list, but router returns a structured dict model `SessionListResponse`.
   - These divergences demonstrate the tests were written without reconciling against the Pydantic models and database schema.

4. **Multi-Tenant / Inter-Staff Isolation Flaw (Step 4 -> Observation 4)**:
   - `Resource.for_user(club_id, user_id)` sets `owner_user_id = user_id`.
   - `policy.authorize()` only applies ownership checking if `principal.is_player`.
   - Non-player staff roles (`coach`, `analyst`, `staff`) possess `SESSION_VIEW` and `SESSION_REVOKE` for self-management, but the router allows them to view and revoke any session within their club.
   - An explicit check (`if target_user_id != principal.user_id and not principal.is_commissioner: raise HTTPException(403)`) is missing in `sessions.py`.

---

## 3. Findings

### [Critical] Finding 1: Unverified Step-Up Privilege Escalation via `/v1/auth/sessions/rotate`
- **What**: `POST /v1/auth/sessions/rotate` accepts unverified `new_auth_level` from request body and promotes session to `step_up_passkey` without WebAuthn evidence.
- **Where**: `src/identity/routers/sessions.py:265-279`, `src/identity/store.py:844`.
- **Why**: Allows complete bypass of WebAuthn decision step-up security. Any authenticated caller can acquire `step_up_passkey` privilege.
- **Suggestion**: Disallow arbitrary client-specified `new_auth_level` in `SessionRotateRequest`. Elevation to `step_up_passkey` must ONLY occur via the dedicated WebAuthn step-up verification handler (`/v1/auth/step-up/verify`), or require a verified `authentication_evidence_id`.

### [Critical] Finding 2: Non-Executable Unit Test Suites with Fatal Parameter & Schema Bugs
- **What**: Authored test files (`test_routers_invitations.py`, `test_routers_sessions.py`, `test_routers_users.py`) fail on fixture initialization and runtime execution.
- **Where**:
  - `tests/identity/test_routers_invitations.py:109, 162`
  - `tests/identity/test_routers_sessions.py:59, 155, 165, 185, 188, 237, 246, 274, 290`
  - `tests/identity/test_routers_users.py:79, 133, 177`
- **Why**:
  - `store.create_session` does not accept `club_id`.
  - Column `expires_at` does not exist on `public.fc27_sessions`.
  - Response model key mismatches (`new_session_token` vs `session_token`, `prior_session_id` vs `old_session_id`).
  - Response type mismatch (`SessionListResponse` dict vs list expectation).
- **Suggestion**:
  1. Remove `club_id` parameter from all `store.create_session(...)` calls in tests.
  2. In `test_session_touch_capped_at_absolute_timeout`, update SQL to set `absolute_expires_at` instead of `expires_at`.
  3. Align `SessionRotateResponse` and `SessionRotateRequest` with test aliases, or update test assertions to match models (`rot_data["session_token"]`, `rot_data["old_session_id"]`, `data["absolute_expires_at"]`).
  4. In `test_list_active_sessions`, unpack `sessions_list = res.json()["sessions"]`.

### [Major] Finding 3: Inter-Staff Session Viewing and Revocation Authorization Gap
- **What**: Any coach, analyst, or staff user can view or revoke active sessions belonging to other staff members in the same club.
- **Where**: `src/identity/routers/sessions.py:319-322, 345-347`, `src/identity/policy.py:680`.
- **Why**: `policy.py` only isolates players by owner; staff roles hold `SESSION_VIEW` and `SESSION_REVOKE` permissions globally within the club.
- **Suggestion**: In `src/identity/routers/sessions.py`, explicitly verify:
  ```python
  if target_sess["user_id"] != principal.user_id and not principal.is_commissioner:
      raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="cannot revoke another user's session")
  ```
  Apply the same check in `list_active_sessions` when `user_id` query param is provided.

### [Minor] Finding 4: Incomplete Payload Remapping in `SessionRotateRequest`
- **What**: `SessionRotateRequest` only defines `new_auth_level` and `new_auth_method`, but tests/clients submit `auth_level` and `auth_method`.
- **Where**: `src/identity/routers/sessions.py:119-125`.
- **Why**: When `auth_level` is sent in JSON, it is ignored, resulting in `new_level = None`.
- **Suggestion**: Add a Pydantic `@model_validator(mode="before")` to map `auth_level` -> `new_auth_level` and `auth_method` -> `new_auth_method`.

---

## 4. Adversarial Challenge Analysis

### Challenge 1: Step-Up Passkey Bypass via `/v1/auth/sessions/rotate`
- **Assumption Challenged**: Rotation is a benign token-refresh operation.
- **Attack Scenario**: Attacker with valid coach or commissioner session calls `POST /v1/auth/sessions/rotate` with `{"new_auth_level": "step_up_passkey"}`. The API revokes the old session and issues a new valid token stamped with `auth_level="step_up_passkey"`. Attacker now possesses credentials that satisfy policy checks for `ActionType.PACKAGE_APPROVE` without possessing a physical passkey or passing WebAuthn challenge verification.
- **Blast Radius**: Complete bypass of cryptographic decision step-up security for club recommendations.
- **Mitigation**: Strictly disallow client-driven elevation of `auth_level` on `/rotate`. Auth level upgrades must only be performed by internal step-up ceremonies with verified cryptographic attestation.

### Challenge 2: Staff Mutual Denial of Service
- **Assumption Challenged**: `Resource.for_user` prevents cross-user mutations.
- **Attack Scenario**: Disgruntled coach discovers colleague's UUID and calls `DELETE /v1/auth/sessions/{colleague_session_id}`. Because both are coaches in the same club and coaches possess `SESSION_REVOKE`, the call succeeds with HTTP 200, terminating the colleague's active session.
- **Blast Radius**: Unauthorized administrative denial of service between staff members.
- **Mitigation**: Require `principal.is_commissioner` whenever target `user_id != principal.user_id`.

---

## 5. Caveats

1. **Terminal Command Execution**:
   - As observed and documented in the subagent environment, interactive permission prompts time out when running shell commands via `run_command`. All analyses in this report are grounded in rigorous AST analysis, line-by-line inspection of Python source code, SQL migrations, and Pydantic schema validation.
2. **Phase 1 Invariants**:
   - Universal invariants (`DISPATCH_ENABLED=false`, zero player delivery, no active relay or worker) remain unbroken.

---

## 6. Conclusion

The implementation has achieved strong foundational architecture for Phase 2 Milestone 3:
- Cryptographic tokens are properly hashed with deterministic SHA-256 (`hash_token()`).
- Invitation consumption runs under `FOR UPDATE` transaction locks preventing double-use.
- FastAPI routers are correctly mounted in `src/graph_api.py` with custom exception handlers.
- Pydantic models for invitations enforce rigorous field constraints.

However, because of **Critical Finding 1 (Step-Up Privilege Escalation)**, **Critical Finding 2 (Non-Executable Unit Tests)**, and **Major Finding 3 (Inter-Staff Session Authorization Gap)**, the work cannot be approved in its current state.

**Verdict**: **REQUEST_CHANGES**

---

## 7. Verification Method

Once worker applies the required fixes, independently verify with:

```bash
# 1. Verify Phase 1 packaging and scope invariants remain clean
pytest tests/test_packaging.py tests/test_phase1_scope.py -v

# 2. Verify all three router test suites execute and pass
pytest tests/identity/test_routers_invitations.py -v
pytest tests/identity/test_routers_sessions.py -v
pytest tests/identity/test_routers_users.py -v

# 3. Verify E2E Tier 1 session and invitation features pass
pytest tests/e2e/test_phase2_tier1_features.py -k "invitation or session" -v
```

### Invalidation Conditions:
- Calling `store.create_session(..., club_id=...)` in any test.
- Calling `POST /v1/auth/sessions/rotate` with `new_auth_level="step_up_passkey"` returning a session with `auth_level="step_up_passkey"`.
- Non-commissioner coach successfully revoking another coach's session via `DELETE /v1/auth/sessions/{id}` returning HTTP 200 instead of HTTP 403.
- `test_routers_sessions.py` referencing non-existent column `expires_at`.
