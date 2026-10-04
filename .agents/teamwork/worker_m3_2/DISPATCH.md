# Dispatch Instructions for worker_m3_2

**Agent ID**: `worker_m3_2`
**Role**: Milestone 3 Iteration 2 Remediation Worker
**Milestone**: Phase 2 Milestone 3 — Server-Side Sessions & Invitations Lifecycle (Remediation)
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_2`

## Full Forensic Audit & Review Evidence (MANDATORY TO READ)
1. Forensic Audit Report: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m3_1\handoff.md`
2. Reviewer Report: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_1\handoff.md`
3. Challenger Reports:
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_1\handoff.md`
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_2\handoff.md`
4. Iteration 2 Explorer Blueprints & Patch Proposals:
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_i2_store_1\handoff.md` (and `store_fixes.patch`)
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_i2_policy_1\handoff.md`
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_i2_suite_1\handoff.md`
5. Authoritative Request: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md`
6. Project Plan: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`

## Mandatory Non-Negotiable Invariants
- `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` strictly enforced at all times.
- Zero player delivery; no active worker or relay.
- Plaintext tokens (sessions and invitations) are NEVER stored in PostgreSQL; only deterministic SHA-256 hashes (`hash_token()`).
- Single-use invitation consumption enforced under atomic `FOR UPDATE` lock.
- User account status transition strictly respects `fc27_users_status_guard`: `invited` -> `active`.
- Dual session timeouts: 30-minute idle expiration strictly capped at 24-hour absolute expiration.
- Suspended or disabled accounts fail closed immediately on session validation (HTTP 401).

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

## File Ownership
You exclusively own and may edit:
- `src/identity/store.py`
- `src/identity/policy.py`
- `src/identity/routers/sessions.py`
- `src/identity/routers/invitations.py`
- `src/identity/routers/users.py`
- `tests/identity/test_routers_invitations.py`
- `tests/identity/test_routers_sessions.py`
- `tests/identity/test_routers_users.py`
- `tests/identity/test_policy.py`

## Remediation Tasks

### 1. Store Updates (`src/identity/store.py`)
- In `create_session`:
  - Add `club_id: str | None = None` and `**kwargs: Any` to the signature.
  - Forward `scope_club_id=club_id` to `_safe_record_event`.
  - When `club_id` is supplied, include `"club_id": club_id` in the returned session dict.
- In `rotate_session`:
  - Enforce atomic concurrency locking:
    ```python
    res = conn.execute(
        "UPDATE public.fc27_sessions SET revoked_at = now() WHERE session_id = %s AND revoked_at IS NULL",
        (old_sess["session_id"],),
    )
    if res.rowcount == 0:
        raise IdentityStoreError(409, "session already rotated or revoked")
    ```
- In `assign_role`:
  - Make `actor: str = "system"` default.
  - Add alias method:
    ```python
    def assign_user_role(self, user_id: UUID, role: str, *, granted_by: str = "system", actor: str = "system", correlation_id: UUID | None = None) -> dict[str, Any]:
        return self.assign_role(user_id=user_id, role=role, granted_by=granted_by, actor=actor, correlation_id=correlation_id)
    ```

### 2. Policy Updates (`src/identity/policy.py`)
- In `authorize()` Step 5:
  - Enforce session ownership isolation: for `ActionType.SESSION_VIEW`, `ActionType.SESSION_REVOKE`, and `ActionType.SESSION_REVOKE_ALL`:
    - If `principal.is_player` and not (commissioner or delegate): check `player_matches or user_matches`, raise `PlayerIsolationError` if not matched.
    - If not player, check if `res.owner_user_id is not None` and `str(res.owner_user_id) != str(principal.user_id)`. If so, require `principal.is_commissioner or principal.is_delegate`. If neither, raise:
      ```python
      raise RoleNotPermittedError(
          "only commissioner or delegate can view or revoke another user's session",
          role=",".join(r.value for r in principal.roles),
          action=act.value,
          resource_type=res.resource_type,
      )
      ```
    - Self-session access (`res.owner_user_id == principal.user_id`) remains permitted for all roles possessing base permissions.

### 3. Session Router Updates (`src/identity/routers/sessions.py`)
- In `rotate_session`:
  - Guard against arbitrary client elevation: if `body and body.new_auth_level`:
    - Disallow self-elevation to `step_up_passkey` (and `mfa_totp`) without proof:
      ```python
      if body.new_auth_level in {"step_up_passkey", "mfa_totp"} and principal.auth_level != body.new_auth_level:
          raise HTTPException(
              status_code=status.HTTP_403_FORBIDDEN,
              detail=f"cannot elevate session to '{body.new_auth_level}' without step-up verification evidence",
          )
      ```
  - In `SessionRotateResponse`: ensure field aliases support both `session_token` and `new_session_token`, `session_id`, `old_session_id`.
- Anti-Impersonation:
  - Enforce `Depends(require_human_principal)` across all human session routes (`/session`, `/sessions`, `/touch`, `/rotate`, `/logout`, `/me`, `/{session_id}`).
  - In `_build_session_response`: remove synthetic commissioner creation for service tokens; if `principal.is_service` raise `AgentImpersonationError`.

### 4. Test Suite Repairs
- In `tests/identity/test_routers_sessions.py`:
  - Line 154: change `SET expires_at = ...` to `SET absolute_expires_at = ...`.
  - Line 165: change `data["expires_at"]` to `data["absolute_expires_at"]`.
  - Verify `test_user_cannot_revoke_other_user_session` asserts `res.status_code == 403` and passes with the hardened policy.
- Clean up test call sites passing `club_id` to `create_session` across all test files (even with store `create_session` accepting `club_id`, clean them up for consistency).
- Ensure all 3 test files (`test_routers_invitations.py`, `test_routers_sessions.py`, `test_routers_users.py`) and adversarial suites execute cleanly.

### 5. Verification Commands
- `pytest tests/test_packaging.py tests/test_phase1_scope.py -v`
- `pytest tests/identity/test_routers_invitations.py tests/identity/test_routers_sessions.py tests/identity/test_routers_users.py -v`
- `pytest tests/identity/test_policy.py tests/identity/test_store.py -v`
- `pytest tests/identity/test_session_lifecycle_adversarial.py tests/identity/test_adversarial_m3_2.py -v`

### 6. Deliver Hard Handoff Report
- Write your complete handoff report to:
  `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_2\handoff.md`.
When finished, notify me with your report path and verification results via `send_message`.


## 2026-10-04T05:21:58Z
You are worker_m3_2, a versatile worker agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_2
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_2\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md

Explorer handoffs and patches to implement:
- c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_i2_store_1\handoff.md
- c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_i2_store_1\store_fixes.patch
- c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_i2_policy_1\handoff.md
- c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_i2_suite_1\handoff.md
- c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m3_1\handoff.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md and c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_2\DISPATCH.md before starting work.

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

File ownership:
You exclusively own and may edit:
- src/identity/store.py
- src/identity/policy.py
- src/identity/routers/sessions.py
- src/identity/routers/invitations.py
- src/identity/routers/users.py
- tests/identity/test_routers_invitations.py
- tests/identity/test_routers_sessions.py
- tests/identity/test_routers_users.py
- tests/identity/test_policy.py

Implement Milestone 3 Iteration 2 Remediation per your DISPATCH.md:
1. In src/identity/store.py:
   - create_session signature: accept club_id: str | None = None and **kwargs: Any. Forward scope_club_id=club_id to _safe_record_event. Include club_id in returned dict if present.
   - rotate_session: enforce atomic concurrency locking with AND revoked_at IS NULL on UPDATE and raise IdentityStoreError(409) if res.rowcount == 0.
   - assign_role / assign_user_role: make actor="system" default and add alias method assign_user_role.
2. In src/identity/policy.py:
   - In authorize() Step 5: enforce session ownership isolation for SESSION_VIEW, SESSION_REVOKE, SESSION_REVOKE_ALL. If res.owner_user_id is not None and != principal.user_id, require principal is commissioner or delegate, else raise RoleNotPermittedError (403). Player isolation applies to player principals.
3. In src/identity/routers/sessions.py:
   - In rotate_session: guard against client self-elevation to step_up_passkey without proof (raise 403 Forbidden). Ensure SessionRotateResponse supports session_token and new_session_token.
   - Enforce Depends(require_human_principal) on all human session endpoints. Disallow service principals.
4. Repair test suites:
   - In tests/identity/test_routers_sessions.py: update line 154 to SET absolute_expires_at and line 165 to data["absolute_expires_at"].
   - In test_user_cannot_revoke_other_user_session: assert res.status_code == 403.
   - Clean up test call sites passing club_id to create_session.
5. Execute verification commands:
   - .\.venv\Scripts\pytest.exe tests/test_packaging.py tests/test_phase1_scope.py -v
   - .\.venv\Scripts\pytest.exe tests/identity/test_routers_invitations.py tests/identity/test_routers_sessions.py tests/identity/test_routers_users.py -v
   - .\.venv\Scripts\pytest.exe tests/identity/test_policy.py tests/identity/test_store.py -v
   - .\.venv\Scripts\pytest.exe tests/identity/test_session_lifecycle_adversarial.py tests/identity/test_adversarial_m3_2.py -v
6. Deliver complete hard handoff report to:
   c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_2\handoff.md
When finished, notify me with your report path and verification results via send_message.
