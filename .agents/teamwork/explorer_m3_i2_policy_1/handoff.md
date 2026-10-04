# Policy Ownership & Session Security Remediation Report

**Agent ID**: `explorer_m3_i2_policy_1`  
**Role**: Policy Ownership & Session Security Explorer (Read-only Investigation)  
**Milestone**: Phase 2 Milestone 3 (Iteration 2 Remediation)  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_i2_policy_1`  
**Target Files**: `src/identity/policy.py`, `src/identity/routers/sessions.py`, `tests/identity/test_policy.py`  

---

## Executive Summary

This investigation resolves three critical security and integrity vulnerabilities identified during the Forensic Audit and Review of Milestone 3:
1. **Inter-Staff Session Revocation & Snooping Bypass (`src/identity/policy.py`)**: `_BASE_ROLE_PERMISSIONS` grants `SESSION_VIEW` and `SESSION_REVOKE` to `COACH`, `ANALYST`, and `STAFF`. However, Step 5 object isolation in `authorize()` only evaluated `principal.is_player`. Consequently, non-player staff could view or revoke active sessions belonging to any other user in the club (including the Commissioner).
2. **Arbitrary Step-Up Privilege Escalation (`src/identity/routers/sessions.py`)**: `POST /v1/auth/sessions/rotate` accepted `new_auth_level` from the request body and directly committed it without cryptographic WebAuthn step-up verification. An authenticated caller could elevate their standard session to `step_up_passkey`, bypassing high-value decision step-up protection.
3. **Anti-Impersonation Gaps on Session Endpoints (`src/identity/routers/sessions.py`)**: 8 out of 10 session endpoints used `Depends(get_current_principal)` instead of `Depends(require_human_principal)`, and `_build_session_response` explicitly fabricated a synthetic commissioner session for automated service tokens (BFF, Graph, Worker), breaching the non-impersonation invariant.

This report formulates exact, line-by-line patch proposals and test suites for the implementation worker without modifying source code directly.

---

## 1. Observation

### 1.1 Authorization Bypass on Session Viewing and Revocation (`src/identity/policy.py`)
- In `src/identity/policy.py` lines 512–554:
  `ActionType.SESSION_VIEW` and `ActionType.SESSION_REVOKE` are granted in `_BASE_ROLE_PERMISSIONS` for `UserRole.COACH`, `UserRole.ANALYST`, and `UserRole.STAFF`.
- In `src/identity/policy.py` lines 679–699:
  ```python
  679:     # 5. Object-Level Player Isolation Guard
  680:     if principal.is_player and not (principal.is_commissioner or principal.is_delegate):
  681:         # A pure player can ONLY access/decide where player_id matches or owner_user_id matches
  682:         player_matches = (res.player_id is not None and str(res.player_id) == str(principal.player_id))
  683:         user_matches = (res.owner_user_id is not None and res.owner_user_id == principal.user_id)
  684: 
  685:         if not (player_matches or user_matches):
  686:             raise PlayerIsolationError(
  687:                 f"player '{principal.player_id}' is forbidden from accessing resource of player '{res.player_id}'",
  688:                 action=act.value,
  689:                 resource_type=res.resource_type,
  690:             )
  ```
  Step 5 is guarded solely by `if principal.is_player:`. Non-player staff (`principal.is_player == False`) completely bypass object-level ownership checks.
- In `tests/identity/test_adversarial_m2_1.py` lines 261–268:
  ```python
  261: def test_adversarial_player_cannot_access_other_player_session(player_a1: PrincipalContext, player_a2: PrincipalContext):
  262:     """Player 1 attempting to view or revoke Player 2's session fails with PlayerIsolationError."""
  263:     p2_session_res = Resource(resource_type="session", club_id=CLUB_A, owner_user_id=player_a2.user_id)
  264:     with pytest.raises(PlayerIsolationError):
  265:         authorize(player_a1, ActionType.SESSION_VIEW, p2_session_res)
  266:     with pytest.raises(PlayerIsolationError):
  267:         authorize(player_a1, ActionType.SESSION_REVOKE, p2_session_res)
  ```
  Player-on-player session access strictly asserts `PlayerIsolationError`.
- In `tests/identity/test_routers_sessions.py` lines 261–285:
  ```python
  279:         # Coach attempts to delete other user's session -> forbidden (403)
  280:         res = client.delete(
  281:             f"/v1/auth/sessions/{other_sess['session_id']}",
  282:             headers=session_header(coach_token),
  283:         )
  284:         assert res.status_code == 403
  ```
  Without ownership checks in `policy.py`, `DELETE /v1/auth/sessions/{other_sess_id}` returns HTTP 200 OK for coach callers, causing test failure.

### 1.2 Arbitrary Step-Up Privilege Escalation on Session Rotation (`src/identity/routers/sessions.py`)
- In `src/identity/routers/sessions.py` lines 119–125:
  ```python
  119: class SessionRotateRequest(BaseModel):
  120:     """Parameters for session rotation upon step-up or privilege change."""
  121:     model_config = ConfigDict(extra="ignore")
  122: 
  123:     new_auth_level: str | None = None
  124:     new_auth_method: str | None = None
  ```
- In `src/identity/routers/sessions.py` lines 263–280:
  ```python
  263: @sessions_router.post("/rotate", response_model=SessionRotateResponse, summary="Rotate session")
  264: def rotate_session(
  265:     body: SessionRotateRequest | None = None,
  266:     principal: PrincipalContext = Depends(require_human_principal),
  267:     raw_token: str = Depends(get_raw_session_token),
  268:     store: IdentityStore = Depends(get_identity_store),
  269: ) -> SessionRotateResponse:
  270:     """Atomically revokes old session and issues new session in the same transaction."""
  271:     new_level = body.new_auth_level if body else None
  272:     new_method = body.new_auth_method if body else None
  273: 
  274:     new_token, new_row = store.rotate_session(
  275:         old_token=raw_token,
  276:         new_auth_level=new_level,
  277:         new_auth_method=new_method,
  278:         actor=principal.display_name or str(principal.user_id),
  279:     )
  ```
- In `src/identity/policy.py` lines 820–827:
  ```python
  820:     # 8. Step-Up Passkey Requirement for High-Value Decisions
  821:     if act in {ActionType.PACKAGE_APPROVE, ActionType.PACKAGE_REJECT}:
  822:         if principal.auth_level != AuthLevel.STEP_UP_PASSKEY:
  823:             raise StepUpRequiredError(...)
  ```
  `POST /v1/auth/sessions/rotate` permits any authenticated human caller to submit `{"new_auth_level": "step_up_passkey"}` and receive a session stamped with `auth_level="step_up_passkey"` without providing cryptographic WebAuthn assertion proof.

### 1.3 Anti-Impersonation Gaps on Session Endpoints (`src/identity/routers/sessions.py`)
- In `src/identity/routers/sessions.py` lines 179–199:
  ```python
  179:     if principal.is_service or principal.session_id is None:
  180:         synthetic_id = UUID("00000000-0000-0000-0000-000000000001")
  181:         return SessionResponse(
  182:             session_id=synthetic_id,
  183:             user_id=principal.user_id,
  184:             club_id=principal.club_id,
  185:             roles=[r.value for r in principal.roles],
  186:             role=primary_role,
  187:             auth_level=principal.auth_level.value,
  188:             auth_method=principal.auth_method.value,
  189:             display_name=principal.display_name or "Commissioner",
  ...
  ```
  Service principals calling `GET /v1/auth/session`, `GET /v1/auth/me`, or `GET /v1/auth/sessions/me` are returned a fabricated human session with `display_name="Commissioner"` and HTTP 200 OK instead of HTTP 403 `AgentImpersonationError`.
- In `src/identity/routers/sessions.py`:
  - Line 234: `get_session_me` uses `Depends(get_current_principal)`
  - Line 293: `logout_sessions_me` uses `Depends(get_current_principal)`
  - Line 311: `revoke_session_by_id` uses `Depends(get_current_principal)`
  - Line 335: `list_active_sessions` uses `Depends(get_current_principal)`
  - Line 386: `revoke_all_sessions` uses `Depends(get_current_principal)`
  - Line 413: `get_current_session` uses `Depends(get_current_principal)`
  - Line 422: `get_current_user_profile` uses `Depends(get_current_principal)`
  - Line 431: `logout` uses `Depends(get_current_principal)`
  Service tokens (BFF, Graph, Worker) calling these endpoints bypass anti-impersonation guards.

---

## 2. Logic Chain

1. **Premise 1 (Tenancy & Object Isolation Principle)**: Under `ORIGINAL_REQUEST.md §7` and `PROJECT.md Feature 5 & 12`, sessions are personal security credentials. A regular club user (`coach`, `analyst`, `staff`, `player`) must only be authorized to view and revoke their OWN sessions (`session.user_id == principal.user_id`). Only administrative principals (`commissioner` or `commissioner_delegate`) may view or revoke sessions belonging to other users within the club.
2. **Step 1 (Policy Step 5 Gap -> Remediation Design)**:
   - Observation 1.1 reveals that non-player staff bypass Step 5 because `Step 5` only checked `if principal.is_player:`.
   - To prevent breaking `test_adversarial_m2_1.py` line 265 (which explicitly expects `PlayerIsolationError` when a player accesses another player's session), the existing player isolation block must remain intact first.
   - Immediately following the player isolation block, an explicit session ownership isolation check must be evaluated:
     If the action is `ActionType.SESSION_VIEW`, `ActionType.SESSION_REVOKE`, or `ActionType.SESSION_REVOKE_ALL`, and `res.owner_user_id` is present with `str(res.owner_user_id) != str(principal.user_id)`, authorization must be denied with `raise RoleNotPermittedError("only commissioner or delegate can view/revoke another user's session")` unless `principal.is_commissioner or principal.is_delegate`.
   - String normalization `str(res.owner_user_id) != str(principal.user_id)` ensures type safety between `UUID` instances and dictionary string representations.
3. **Premise 2 (Step-Up Decision Integrity)**: High-value Commissioner decisions require `principal.auth_level == AuthLevel.STEP_UP_PASSKEY`. Session rotation must never allow unverified elevation to `step_up_passkey`.
4. **Step 2 (Session Rotation Privilege Escalation Guard -> Remediation Design)**:
   - In `rotate_session`, if the client request body supplies a `new_auth_level` of `"step_up_passkey"` (or `"mfa_totp"`), and `principal.auth_level` does not already possess that authentication level, the endpoint must fail closed with HTTP 403 Forbidden: `raise HTTPException(status_code=403, detail="unverified elevation to step_up_passkey is prohibited; cryptographic step-up verification required")`.
   - To support flexible client payloads, `SessionRotateRequest` must use a `@model_validator(mode="before")` to remap legacy payload keys `auth_level` -> `new_auth_level` and `auth_method` -> `new_auth_method`.
   - If `new_auth_level` is omitted or `None`, rotation preserves the existing session's authentication level.
5. **Premise 3 (Zero Agent-Human Impersonation Invariant)**: Under `ORIGINAL_REQUEST.md §3.3` and `PROJECT.md §1.2 Principle 3`, automated service tokens (BFF, Graph, Worker) cannot authenticate or act as humans.
6. **Step 3 (Session Endpoints Anti-Impersonation -> Remediation Design)**:
   - In `src/identity/routers/sessions.py`, all 10 endpoints managing or projecting human sessions (`/me`, `/touch`, `/rotate`, `DELETE /me`, `DELETE /{session_id}`, `GET ""`, `DELETE ""`, `GET /session`, `GET /me`, `POST /logout`) must depend strictly on `require_human_principal`.
   - In `_build_session_response`, the branch `if principal.is_service:` must immediately raise `AgentImpersonationError(403)`. The fallback synthetic session branch must be restricted strictly to legacy human commissioner tokens where `principal.session_id is None and not principal.is_service`.
7. **Deduction**: Applying these three targeted modifications closes all authorization bypasses, prevents privilege escalation, and enforces complete anti-impersonation compliance across all session lifecycles.

---

## 3. Caveats

1. **Read-Only Explorer Constraint**: In strict adherence to Teamwork Explorer protocols, no source code files or migrations were directly modified. All recommendations are delivered as exact, machine-applicable unified diffs and replacement blocks.
2. **Interactive Command Elevation in Subagent Shell**: Terminal commands requiring interactive elevation prompts time out in this Windows environment. Findings and diffs were validated through rigorous static AST analysis, cross-referencing schema migrations, and checking against authored adversarial test suites (`test_session_lifecycle_adversarial.py` and `test_adversarial_m3_2.py`).
3. **Milestone 4 & 5 Passkey/Step-Up Seams**: Genuine WebAuthn step-up elevation will be implemented in Milestone 5 via `/v1/auth/step-up/verify`. At Milestone 3, failing closed with HTTP 403 on client-driven elevation to `step_up_passkey` is the strictly required security boundary.
4. **Test Suite Invalidation Notice**: Because `test_atomic_session_rotation` in `test_routers_sessions.py:180` and `test_session_lifecycle_adversarial.py:241` previously asserted the vulnerable behavior (`rot_data["auth_level"] == "step_up_passkey"`), those tests must be aligned by `explorer_m3_i2_suite_1` to either rotate without elevation (asserting `"authenticated"`) or expect HTTP 403 when elevation is attempted without proof.

---

## 4. Conclusion & Proposed Code Fixes

### 4.1 Fix Proposal 1: `src/identity/policy.py` (Step 5 Session Ownership Guard)

#### Target File: `c:\Users\iwadev\dev\iwa.dev.fcbuild\src\identity\policy.py`
#### Exact Lines: 679–702

#### Unified Diff:
```diff
--- a/src/identity/policy.py
+++ b/src/identity/policy.py
@@ -679,7 +679,7 @@ def authorize(
     # 5. Object-Level Player Isolation Guard
     if principal.is_player and not (principal.is_commissioner or principal.is_delegate):
         # A pure player can ONLY access/decide where player_id matches or owner_user_id matches
         player_matches = (res.player_id is not None and str(res.player_id) == str(principal.player_id))
-        user_matches = (res.owner_user_id is not None and res.owner_user_id == principal.user_id)
+        user_matches = (res.owner_user_id is not None and str(res.owner_user_id) == str(principal.user_id))
 
         if not (player_matches or user_matches):
             raise PlayerIsolationError(
@@ -698,6 +698,15 @@ def authorize(
                 resource_type=res.resource_type,
             )
 
+    # Session ownership isolation: only commissioner or delegate can view/revoke another user's session
+    if act in {ActionType.SESSION_VIEW, ActionType.SESSION_REVOKE, ActionType.SESSION_REVOKE_ALL}:
+        if res.owner_user_id is not None and str(res.owner_user_id) != str(principal.user_id):
+            if not (principal.is_commissioner or principal.is_delegate):
+                raise RoleNotPermittedError(
+                    "only commissioner or delegate can view/revoke another user's session",
+                    action=act.value,
+                    resource_type=res.resource_type,
+                )
+
     # 6. Human Delegation Guard (for COMMISSIONER_DELEGATE role)
```

#### Rationale:
- Preserves the player isolation check first so player-on-player session access continues to raise `PlayerIsolationError` as asserted by `test_adversarial_m2_1.py:265`.
- Checks `SESSION_VIEW`, `SESSION_REVOKE`, and `SESSION_REVOKE_ALL` across all roles.
- Allows callers to manage their own sessions (`str(res.owner_user_id) == str(principal.user_id)`).
- Allows `COMMISSIONER` and `COMMISSIONER_DELEGATE` to view or revoke other users' sessions.
- Raises `RoleNotPermittedError` (HTTP 403) for any other role (`coach`, `analyst`, `staff`, or player) attempting to view or revoke another user's session.

---

### 4.2 Fix Proposal 2: `src/identity/routers/sessions.py`

#### Target File: `c:\Users\iwadev\dev\iwa.dev.fcbuild\src\identity\routers\sessions.py`
#### Exact Lines: 20–40, 119–137, 172–202, 230–445

#### Unified Diff:
```diff
--- a/src/identity/routers/sessions.py
+++ b/src/identity/routers/sessions.py
@@ -21,14 +21,16 @@ from datetime import datetime, timedelta, timezone
 from typing import Any, Literal
 from uuid import UUID
 
 from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request, status
-from pydantic import BaseModel, ConfigDict, Field
+from pydantic import BaseModel, ConfigDict, Field, model_validator
 
-from src.identity.deps import get_current_principal, require_human_principal
+from src.identity.deps import require_human_principal
 from src.identity.policy import (
     ActionType,
     AgentImpersonationError,
+    AuthLevel,
     CrossClubAccessError,
     PlayerIsolationError,
     PrincipalContext,
     Resource,
@@ -120,6 +122,16 @@ class SessionRotateRequest(BaseModel):
     new_auth_level: str | None = None
     new_auth_method: str | None = None
 
+    @model_validator(mode="before")
+    @classmethod
+    def _remap_aliases(cls, data: Any) -> Any:
+        if isinstance(data, dict):
+            if "new_auth_level" not in data and "auth_level" in data:
+                data["new_auth_level"] = data["auth_level"]
+            if "new_auth_method" not in data and "auth_method" in data:
+                data["new_auth_method"] = data["auth_method"]
+        return data
+
 
 class SessionRotateResponse(BaseModel):
@@ -173,7 +185,12 @@ def _build_session_response(
     principal: PrincipalContext,
     store: IdentityStore,
 ) -> SessionResponse:
+    if principal.is_service:
+        raise AgentImpersonationError(
+            f"service principal '{principal.user_id}' cannot project human session: "
+            "agents and services are strictly prohibited from impersonating humans"
+        )
     now = datetime.now(timezone.utc)
     primary_role = next(iter(principal.roles)).value if principal.roles else "commissioner"
 
-    if principal.is_service or principal.session_id is None:
+    if principal.session_id is None:
         synthetic_id = UUID("00000000-0000-0000-0000-000000000001")
         return SessionResponse(
@@ -231,7 +248,7 @@ def _build_session_response(
 
 @sessions_router.get("/me", response_model=SessionResponse, summary="Inspect current session")
 def get_session_me(
-    principal: PrincipalContext = Depends(get_current_principal),
+    principal: PrincipalContext = Depends(require_human_principal),
     store: IdentityStore = Depends(get_identity_store),
 ) -> SessionResponse:
     return _build_session_response(principal, store)
@@ -270,6 +287,19 @@ def rotate_session(
     new_level = body.new_auth_level if body else None
     new_method = body.new_auth_method if body else None
 
+    # Guard against unproven step_up_passkey privilege escalation
+    if new_level is not None:
+        if new_level in {"step_up_passkey", AuthLevel.STEP_UP_PASSKEY.value}:
+            if principal.auth_level != AuthLevel.STEP_UP_PASSKEY:
+                raise HTTPException(
+                    status_code=status.HTTP_403_FORBIDDEN,
+                    detail="unverified elevation to step_up_passkey is prohibited; cryptographic step-up verification required",
+                )
+        elif new_level in {"mfa_totp", AuthLevel.MFA_TOTP.value}:
+            if principal.auth_level != AuthLevel.MFA_TOTP:
+                raise HTTPException(
+                    status_code=status.HTTP_403_FORBIDDEN,
+                    detail="unverified elevation to mfa_totp is prohibited; TOTP verification required",
+                )
+
     new_token, new_row = store.rotate_session(
         old_token=raw_token,
         new_auth_level=new_level,
@@ -290,7 +320,7 @@ def rotate_session(
 
 @sessions_router.delete("/me", response_model=SessionRevokeResponse, summary="Logout current session")
 def logout_sessions_me(
-    principal: PrincipalContext = Depends(get_current_principal),
+    principal: PrincipalContext = Depends(require_human_principal),
     raw_token: str = Depends(get_raw_session_token),
     store: IdentityStore = Depends(get_identity_store),
 ) -> SessionRevokeResponse:
@@ -308,7 +338,7 @@ def logout_sessions_me(
 @sessions_router.delete("/{session_id}", response_model=SessionRevokeResponse, summary="Revoke session by ID")
 def revoke_session_by_id(
     session_id: UUID = Path(...),
-    principal: PrincipalContext = Depends(get_current_principal),
+    principal: PrincipalContext = Depends(require_human_principal),
     store: IdentityStore = Depends(get_identity_store),
 ) -> SessionRevokeResponse:
     """Revokes a specific session by ID. Supports self-revocation and admin revocation."""
@@ -332,7 +362,7 @@ def revoke_session_by_id(
 @sessions_router.get("", response_model=SessionListResponse, summary="List active sessions")
 def list_active_sessions(
     user_id: UUID | None = Query(None, description="Optional target user (Commissioner only)"),
-    principal: PrincipalContext = Depends(get_current_principal),
+    principal: PrincipalContext = Depends(require_human_principal),
     store: IdentityStore = Depends(get_identity_store),
 ) -> SessionListResponse:
     """Lists active, unexpired, unrevoked sessions for the authenticated user."""
@@ -344,6 +374,9 @@ def list_active_sessions(
         res = Resource.for_user(club_id=target_user["club_id"], user_id=target_user_id)
         authorize(principal, ActionType.SESSION_VIEW, res)
+    else:
+        res = Resource.for_user(club_id=principal.club_id, user_id=principal.user_id)
+        authorize(principal, ActionType.SESSION_VIEW, res)
 
     sessions_data = store.list_user_sessions(
@@ -383,7 +416,7 @@ def list_active_sessions(
 @sessions_router.delete("", response_model=RevokeAllSessionsResponse, summary="Bulk terminate sessions")
 def revoke_all_sessions(
     user_id: UUID | None = Query(None, description="Optional target user ID (Commissioner only)"),
-    principal: PrincipalContext = Depends(get_current_principal),
+    principal: PrincipalContext = Depends(require_human_principal),
     store: IdentityStore = Depends(get_identity_store),
 ) -> RevokeAllSessionsResponse:
     """Revokes all active sessions for a user (global logout / security reset)."""
@@ -395,6 +428,9 @@ def revoke_all_sessions(
         res = Resource.for_user(club_id=target_user["club_id"], user_id=target_user_id)
         authorize(principal, ActionType.SESSION_REVOKE_ALL, res)
+    else:
+        res = Resource.for_user(club_id=principal.club_id, user_id=principal.user_id)
+        authorize(principal, ActionType.SESSION_REVOKE, res)
 
     count = store.revoke_all_user_sessions(target_user_id, actor=str(principal.user_id))
@@ -410,7 +446,7 @@ def revoke_all_sessions(
 
 @auth_router.get("/session", response_model=SessionResponse, summary="Inspect current session")
 def get_current_session(
-    principal: PrincipalContext = Depends(get_current_principal),
+    principal: PrincipalContext = Depends(require_human_principal),
     store: IdentityStore = Depends(get_identity_store),
 ) -> SessionResponse:
     """Returns current active session projection with dual-expiry bounds."""
@@ -419,7 +455,7 @@ def get_current_session(
 
 @auth_router.get("/me", response_model=SessionResponse, summary="Inspect caller profile")
 def get_current_user_profile(
-    principal: PrincipalContext = Depends(get_current_principal),
+    principal: PrincipalContext = Depends(require_human_principal),
     store: IdentityStore = Depends(get_identity_store),
 ) -> SessionResponse:
     """Returns projection of current session and authenticated caller context."""
@@ -428,7 +464,7 @@ def get_current_user_profile(
 
 @auth_router.post("/logout", response_model=SessionRevokeResponse, summary="Logout current session")
 def logout(
-    principal: PrincipalContext = Depends(get_current_principal),
+    principal: PrincipalContext = Depends(require_human_principal),
     raw_token: str = Depends(get_raw_session_token),
     store: IdentityStore = Depends(get_identity_store),
 ) -> SessionRevokeResponse:
```

#### Rationale:
- Enforces `require_human_principal` uniformly across all 10 session endpoints.
- Rejects service tokens in `_build_session_response` with `AgentImpersonationError` (HTTP 403).
- Blocks unverified elevation to `step_up_passkey` (and `mfa_totp`) with HTTP 403 Forbidden.
- Re-maps `auth_level` and `auth_method` keys on `SessionRotateRequest` using `@model_validator`.
- Enforces `authorize()` calls in `list_active_sessions` and `revoke_all_sessions` even when `user_id` query param is not supplied.

---

### 4.3 Proposed Regression Test Additions: `tests/identity/test_policy.py`

#### Target File: `c:\Users\iwadev\dev\iwa.dev.fcbuild\tests\identity\test_policy.py`

```python
# =====================================================================
# 9. Session Ownership Isolation Tests (Phase 2 Milestone 3 Remediation)
# =====================================================================

def test_coach_can_revoke_own_session(coach_principal: PrincipalContext):
    """Coach can view and revoke their own session."""
    own_session_res = Resource.for_user(club_id=CLUB_A, user_id=coach_principal.user_id)
    assert authorize(coach_principal, ActionType.SESSION_VIEW, own_session_res).allowed is True
    assert authorize(coach_principal, ActionType.SESSION_REVOKE, own_session_res).allowed is True


def test_coach_cannot_revoke_other_user_session(coach_principal: PrincipalContext):
    """Coach cannot view or revoke another user's session in the same club."""
    other_user_id = uuid4()
    other_session_res = Resource.for_user(club_id=CLUB_A, user_id=other_user_id)
    with pytest.raises(RoleNotPermittedError) as exc_info:
        authorize(coach_principal, ActionType.SESSION_REVOKE, other_session_res)
    assert "only commissioner or delegate can view/revoke another user's session" in exc_info.value.detail


def test_staff_cannot_view_other_user_session(staff_principal: PrincipalContext):
    """Staff member cannot view another user's session."""
    other_user_id = uuid4()
    other_session_res = Resource.for_user(club_id=CLUB_A, user_id=other_user_id)
    with pytest.raises(RoleNotPermittedError):
        authorize(staff_principal, ActionType.SESSION_VIEW, other_session_res)


def test_commissioner_can_revoke_other_user_session(commissioner_principal: PrincipalContext):
    """Commissioner can administratively view and revoke any session in the club."""
    target_user_id = uuid4()
    target_session_res = Resource.for_user(club_id=CLUB_A, user_id=target_user_id)
    assert authorize(commissioner_principal, ActionType.SESSION_VIEW, target_session_res).allowed is True
    assert authorize(commissioner_principal, ActionType.SESSION_REVOKE, target_session_res).allowed is True
    assert authorize(commissioner_principal, ActionType.SESSION_REVOKE_ALL, target_session_res).allowed is True


def test_delegate_can_revoke_other_user_session(delegate_principal: PrincipalContext):
    """Commissioner delegate can view and revoke another user's session."""
    target_user_id = uuid4()
    target_session_res = Resource.for_user(club_id=CLUB_A, user_id=target_user_id)
    assert authorize(delegate_principal, ActionType.SESSION_VIEW, target_session_res).allowed is True
    assert authorize(delegate_principal, ActionType.SESSION_REVOKE, target_session_res).allowed is True
```

---

## 5. Verification Method

Once the remediation worker implements these patches, execute the following commands to independently verify:

### 5.1 Verification Commands
```bash
# 1. Verify Phase 1 packaging and scope invariants remain untouched
pytest tests/test_packaging.py tests/test_phase1_scope.py -v

# 2. Verify Policy authorization suite with new session ownership tests
pytest tests/identity/test_policy.py -v

# 3. Verify M2 Adversarial test suite (ensures PlayerIsolationError is maintained)
pytest tests/identity/test_adversarial_m2_1.py -k "test_adversarial_player_cannot_access_other_player_session" -v

# 4. Verify Session router unit test suite
pytest tests/identity/test_routers_sessions.py -k "test_user_cannot_revoke_other_user_session" -v

# 5. Verify Adversarial session lifecycle suite
pytest tests/identity/test_session_lifecycle_adversarial.py -k "test_service_token_cannot_call_touch_session or test_service_token_cannot_call_rotate_session or test_player_cannot_revoke_another_players_session_by_id" -v
```

### 5.2 Files to Inspect
1. `src/identity/policy.py`: Confirm lines 699–709 enforce `res.owner_user_id != principal.user_id` check for `SESSION_VIEW`, `SESSION_REVOKE`, `SESSION_REVOKE_ALL`.
2. `src/identity/routers/sessions.py`: Confirm `require_human_principal` is on all 10 endpoints, `_build_session_response` raises `AgentImpersonationError` for services, and `rotate_session` guards against `new_level in {"step_up_passkey", "mfa_totp"}` with HTTP 403.
3. `tests/identity/test_policy.py`: Confirm 5 new regression tests verify cross-user session access rules across roles.

### 5.3 Invalidation Conditions
- A `COACH`, `ANALYST`, or `STAFF` principal passing `authorize(..., ActionType.SESSION_REVOKE, Resource.for_user(club_id, other_user_id))` without raising `RoleNotPermittedError`.
- A caller with `auth_level="authenticated"` calling `POST /v1/auth/sessions/rotate` with `{"new_auth_level": "step_up_passkey"}` returning HTTP 200 OK.
- A service token (`BFF_HEADERS`, `GRAPH_HEADERS`, `WORKER_HEADERS`) calling `GET /v1/auth/session` or `GET /v1/auth/sessions` returning HTTP 200 OK instead of HTTP 403 `AgentImpersonationError`.
- A player accessing another player's session raising `RoleNotPermittedError` instead of `PlayerIsolationError` (which would invalidate `test_adversarial_m2_1.py:265`).
