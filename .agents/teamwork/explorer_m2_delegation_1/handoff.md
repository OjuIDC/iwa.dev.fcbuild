# Handoff Report: Human Authority & Delegation Engine (Phase 2 Milestone 2)

**Author**: `explorer_m2_delegation_1` (Read-only exploration agent)  
**Target Milestone**: Phase 2 Milestone 2 — Centralized Authorization & Service Boundaries  
**Target File**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_delegation_1\handoff.md`  

---

## 1. Observation

### 1.1 Mandate & Invariants from Authoritative Brief
From `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md`:
- **§1A.1 Core Authority Model** (lines 973–995):
  > "1. Club authorization: The Commissioner remains the primary authority for club-level decisions."
  > "2. Delegated club authorization: An explicitly designated human deputy may authorize only the club-level actions covered by an active, unexpired, and unrevoked delegation."
  > "3. Player consent: An affected player may accept, reject, pause, acknowledge, or request changes to a player-specific assignment addressed to that player."
  > "4. Affected-player fallback: When the configured escalation policy has been satisfied and no eligible club approver has acted, the affected player may become eligible to authorize only their own eligible player-specific assignment."
  > "Fallback authority does not mean automatic approval. No absence, inactivity period, timeout, unread notification, offline status, delivery failure, or lack of response may itself create an approval. Inactivity may only change which authenticated human actor becomes eligible to receive and decide the request."
- **§1A.2 Human Decision Requirement** (lines 996–1006):
  > "No agent may approve or reject for a human. This applies to: club agents; coach agents; Commissioner-facing agents; player agents; Graph nodes; language models; background workers; Redis subscribers; WebSocket clients."
- **§4.2 Human Delegate** (lines 1801–1814):
  > "A human delegate may act only if an explicit delegation is: active; not expired; not revoked; associated with that authenticated user; applicable to the current club; applicable to the action type; applicable to the decision scope; compatible with the requested authority level. Role membership alone must not magically expand a delegation."
- **§7.3 Delegation** (lines 2167–2185):
  > "Activate the explicit human delegation foundation created in Phase 1. Delegation validation must check: principal -> delegation holder -> active -> not expired -> not revoked -> club scope -> action scope -> resource scope -> required authentication strength. A delegation cannot be broadened through interpretation. No matching authorization means deny."

### 1.2 Existing Schema Observations
From `c:\Users\iwadev\dev\iwa.dev.fcbuild\migrations\0003_dispatch_foundation.sql`:
- **`public.authority_delegations`** (lines 494–514):
  ```sql
  CREATE TABLE IF NOT EXISTS public.authority_delegations (
      delegation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      delegator_id text NOT NULL,
      delegate_id text NOT NULL,
      delegate_role text NOT NULL DEFAULT 'deputy',
      scope text NOT NULL CHECK (scope IN ('club', 'player_specific')),
      allowed_action_types text[] NOT NULL DEFAULT '{}',
      issued_at timestamptz NOT NULL DEFAULT now(),
      expires_at timestamptz NOT NULL,
      revoked_at timestamptz,
      revoked_by text,
      revocation_reason text,
      created_at timestamptz NOT NULL DEFAULT now(),
      CHECK (expires_at > issued_at),
      CHECK (revoked_at IS NULL OR (revoked_by IS NOT NULL AND revocation_reason IS NOT NULL))
  );
  ```
- **Foreign Key in `approval_audit`** (lines 658–659):
  ```sql
  ALTER TABLE public.approval_audit
      ADD COLUMN IF NOT EXISTS delegation_id uuid NULL 
          REFERENCES public.authority_delegations(delegation_id);
  ```
- **`public.authority_policies`** (lines 465–490):
  ```sql
  CREATE TABLE IF NOT EXISTS public.authority_policies (
      policy_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      policy_name text UNIQUE NOT NULL CHECK (char_length(policy_name) BETWEEN 1 AND 64),
      scope text NOT NULL CHECK (scope IN ('club', 'player_specific', 'mixed')),
      action_type text NOT NULL CHECK (char_length(action_type) BETWEEN 1 AND 64),
      primary_role text NOT NULL DEFAULT 'commissioner',
      allow_delegation boolean NOT NULL DEFAULT false,
      allow_player_consent boolean NOT NULL DEFAULT false,
      allow_player_fallback boolean NOT NULL DEFAULT false,
      escalation_timeout_seconds integer CHECK (escalation_timeout_seconds IS NULL OR escalation_timeout_seconds > 0),
      is_active boolean NOT NULL DEFAULT true,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      version integer NOT NULL DEFAULT 1,
      CHECK (allow_player_fallback = false OR scope = 'player_specific'),
      CHECK (
          allow_player_fallback = false OR
          action_type NOT IN (
              'club_strategy', 'team_tactics', 'formations', 'programme_release',
              'club_dispatch_activation', 'policy_exception', 'roster',
              'transfer', 'recruitment', 'discipline', 'fair_play',
              'identity_change', 'access_control', 'delegation_policy'
          )
      )
  );
  ```
- **`public.player_assignments` & `public.player_decisions`** (lines 515–584):
  `player_decisions` binds `subject_player_id`, `actor_id`, `actor_type = 'player'`, `decision IN ('accept', 'reject', 'change_request', 'pause', 'acknowledge')`, `authority_basis IN ('affected_player_consent', 'affected_player_fallback')`, and enforces `CHECK (actor_id = subject_player_id)`.
- **`public.approval_escalations`** (lines 585–606):
  Contains `from_state`, `to_state`, `trigger_reason IN ('timer_elapsed', 'manual_escalation', 'delegate_unavailable', 'policy_rule', 'conflict_detected')`, `fallback_eligible boolean`, `eligible_actor_type text`.

From `c:\Users\iwadev\dev\iwa.dev.fcbuild\migrations\0004_identity_foundation.sql`:
- Lines 6–17: `public.fc27_users` defines `user_id uuid PRIMARY KEY`, `club_id text NOT NULL`, `login_id text NOT NULL UNIQUE`, `status text NOT NULL CHECK (status IN ('invited', 'active', 'suspended', 'recovery_required', 'disabled'))`, and optional `player_id text REFERENCES public.players(player_id)`.
- Lines 76–95: `public.fc27_user_roles` defines `(user_id, role)` where `role IN ('commissioner', 'commissioner_delegate', 'coach', 'analyst', 'staff', 'player', 'bff_service', 'graph_service')`.
- Lines 355–378: `domain_events_source_table_check` list of allowed source tables contains `fc27_users`, `fc27_user_roles`, `fc27_invitations`, `fc27_sessions`, `fc27_webauthn_credentials`, etc., but **does NOT include** `authority_delegations` or `fc27_delegations`.

### 1.3 Test Expectations & Existing Contracts
From `c:\Users\iwadev\dev\iwa.dev.fcbuild\tests\e2e\test_phase2_tier1_features.py`:
- Lines 596–627:
  ```python
  payload = {
      "delegate_user_id": str(uuid.uuid4()),
      "club_id": TEST_CLUB_ID,
      "action_type": "training_decision",
      "scope": "tactical_drills",
      "expires_in_hours": 24,
  }
  res = client.post("/v1/auth/delegations", headers=COM_HEADERS, json=payload)
  assert res.status_code in (200, 201)
  data = res.json()
  assert "delegation_id" in data
  assert data["action_type"] == "training_decision"
  # GET /v1/auth/delegations
  # DELETE /v1/auth/delegations/{del_id}
  ```
From `c:\Users\iwadev\dev\iwa.dev.fcbuild\tests\e2e\test_phase2_tier3_combinations.py`:
- Lines 139–179:
  ```python
  del_res = client.post(
      "/v1/auth/delegations",
      headers=COM_HEADERS,
      json={
          "delegate_user_id": deputy_id,
          "club_id": TEST_CLUB_ID,
          "action_type": "training_drills",
          "scope": "tactical",
          "expires_in_hours": 12,
      },
  )
  # Deputy attempts action -> Revocation -> Deputy attempts action -> 403 Forbidden
  ```
From `c:\Users\iwadev\dev\iwa.dev.fcbuild\tests\e2e\test_tier4_scenarios.py`:
- Lines 221–229:
  ```python
  assert table_exists(conn, "authority_delegations")
  assert column_exists(conn, "authority_delegations", "expires_at")
  assert column_exists(conn, "authority_delegations", "scope")
  ```

---

## 2. Logic Chain

### 2.1 The Need for Reconciliation between Phase 1 and Phase 2 Schema
1. **Observation 1.2** shows that `public.authority_delegations` was created in `0003_dispatch_foundation.sql`.
2. However, the 0003 table definition has several limitations when confronted with Phase 2 requirements (Observation 1.1) and tests (Observation 1.3):
   - It has no `club_id` column, but delegations must be strictly scoped to a club.
   - It defines `scope text NOT NULL CHECK (scope IN ('club', 'player_specific'))`, whereas the Phase 2 tests (Observation 1.3) pass `"tactical_drills"` and `"tactical"`, causing immediate PostgreSQL constraint check violations.
   - It defines `delegator_id text` and `delegate_id text`, but Phase 2 identity models users as `fc27_users.user_id uuid`.
   - It lacks `starts_at timestamptz` (only has `issued_at` and `expires_at`).
   - It is missing from `domain_events_source_table_check` in `0004_identity_foundation.sql`.
3. If an entirely new table `fc27_delegations` were created without linking to `authority_delegations`:
   - `approval_audit.delegation_id` has an explicit foreign key `REFERENCES public.authority_delegations(delegation_id)`.
   - `tests/e2e/test_tier4_scenarios.py` and `tests/e2e/test_tier1_features.py` assert `table_exists(conn, "authority_delegations")`.
   - Any approval decision attempting to reference an `fc27_delegations` UUID would fail foreign key validation against `authority_delegations`.
4. **Logical Inference**:
   The optimal, non-destructive migration design is `migrations/0005_delegations.sql`, which:
   - Alters `public.authority_delegations` to add `club_id text NOT NULL`, `starts_at timestamptz NOT NULL DEFAULT now()`, `target_player_id text NULL`, and `target_package_type text NULL`.
   - Expands the `scope` check constraint to accept descriptive scopes (e.g. `char_length(scope) BETWEEN 1 AND 64`).
   - Creates a view `public.fc27_delegations` pointing to `public.authority_delegations` (or creates a unified schema) to satisfy both naming conventions.
   - Updates `domain_events` check constraint to permit `'authority_delegations'` and `'fc27_delegations'` as source tables.
   - Updates `migrations/post/grants.sql` to grant `SELECT, INSERT, UPDATE` on `authority_delegations` and `fc27_delegations` to `fc27_app`, bumping `GRANTS_POLICY` version to 7.

### 2.2 Authority Model Hierarchy & Boundaries
From Observation 1.1:
1. **Primary Authority (`primary`)**:
   - Sole authority: Commissioner (active user with role `commissioner` in same club).
   - Can authorize club-wide strategy, formations, roster, transfers, discipline, policy changes, and dispatch release.
   - Step-up requirement: High-value actions require WebAuthn assertion cryptographically bound to the decision payload.
2. **Delegated Authority (`explicit_delegate`)**:
   - Human deputy (active user in same club with role `commissioner_delegate`, `coach`, or `staff`).
   - Must hold an active, unrevoked, unexpired delegation record where `starts_at <= now() < expires_at`.
   - Strictly action-scoped (e.g. `training_decision`, `drill_schedule`).
   - Prohibited from: club strategy, formations, roster, transfers, discipline, policy exceptions, identity changes, recursive delegation.
3. **Player Consent (`affected_player_consent`)**:
   - Human player (active user with role `player` whose `player_id` matches `subject_player_id`).
   - Allowed outcomes: `accept`, `reject`, `change_request`, `pause`, `acknowledge`.
   - Must personally authenticate via passkey ceremony.
   - Player agents cannot decide for players. Acknowledgment != Acceptance.
4. **Affected-Player Fallback (`affected_player_fallback`)**:
   - Dual-direction escalation without automatic decision:
     - Case A: Primary club approver is unavailable -> escalation deadline elapses -> policy permits fallback (`allow_player_fallback = true`) -> affected player becomes eligible to authorize their own developmental assignment.
     - Case B: Player does not respond to player-scoped assignment -> escalation deadline elapses -> request transitions to `primary_reminder_due` or `held_no_authorized_approver` for Commissioner intervention.
   - Absolute invariant G-TIMEOUT: Inactivity or timeout changes eligibility, but NEVER creates an approval.

### 2.3 Evaluation Engine in `policy.py`
To avoid scattering checks across routers:
- Implement `authorize(conn, principal, action, resource, authority_basis, delegation_id)` in `src/identity/policy.py`.
- Tenancy check: `principal.club_id == resource.club_id`.
- Role check: Validate subject roles.
- Authority basis check:
  - If `primary`: verify `commissioner` role.
  - If `explicit_delegate`: call `IdentityStore.evaluate_delegation()`.
  - If `affected_player_consent`: verify player owns assignment and action is player-scoped.
  - If `affected_player_fallback`: verify assignment is marked `fallback_eligible` and player owns assignment.
- Fail closed: Any mismatch raises `AuthorizationDenied(403)`.

---

## 3. Caveats

1. **Phase 1 HMAC vs Phase 2 WebAuthn Proofs**:
   During Phase 2, legacy Phase 1 HMAC signing remains accessible only when `APPROVAL_CLI_HMAC_ENABLED=true`. Once Phase 2 Milestone 5 step-up verification is complete, the kill switch will disable HMAC. Delegations in Milestone 2 must be ready to work with both proof types.
2. **Stable EA Player ID Mapping**:
   In Phase 2, local player accounts map to local player records via `fc27_users.player_id`. External EA numeric identity evidence remains separate and unverified per Section 3.4; local numeric IDs (e.g. `"330323"`) serve as authoritative tenancy boundaries.
3. **Multi-tenancy Scope**:
   All delegation evaluations assume single-club tenancy per user session. A user cannot exercise delegated authority across different club IDs.

---

## 4. Conclusion & Complete Design Specifications

### 4.1 Authority Model Hierarchy Specification

| Authority Basis | Permitted Actor | Scope & Target Restrictions | Prohibited Actions | Required Authentication |
|---|---|---|---|---|
| `primary` | Commissioner (`fc27_user_roles.role = 'commissioner'`) | All club-level packages, formations, roster, policies, player packages | None | Active session + WebAuthn passkey step-up |
| `explicit_delegate` | Designated human deputy in same club | Explicitly scoped action types (`allowed_action_types`), valid time window (`starts_at` to `expires_at`), matching `club_id` | Club strategy, team tactics, formations, transfers, roster changes, discipline, fair play, policy exceptions, identity/delegation admin, recursive delegation | Active session (+ step-up if required by policy) |
| `affected_player_consent` | Subject player (`player_id == subject_player_id`) | Own personal assignment (`player_specific`), personal drill/build recommendations | Club-wide actions, other players' assignments, multi-player packages, mixed packages | Active player session + passkey assertion |
| `affected_player_fallback` | Affected player (or intervening Commissioner) | Only player's own developmental assignment where `allow_player_fallback = true` | Club strategy, transfers, roster, discipline, policy exceptions, multi-player packages | Authenticated human ceremony (never automatic) |

### 4.2 Database Schema: `migrations/0005_delegations.sql`

```sql
-- Phase 2 Milestone 2: Delegations Schema Extension
-- Extends public.authority_delegations to integrate with fc27_users, club scoping, and Phase 2 tests.

-- 1. Extend authority_delegations table
ALTER TABLE public.authority_delegations
    ADD COLUMN IF NOT EXISTS club_id text NOT NULL DEFAULT '330323' CHECK (club_id ~ '^[0-9]+$'),
    ADD COLUMN IF NOT EXISTS starts_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS target_player_id text NULL CHECK (target_player_id IS NULL OR target_player_id ~ '^[0-9]{1,20}$'),
    ADD COLUMN IF NOT EXISTS target_package_type text NULL;

-- 2. Relax scope constraint to permit descriptive scopes (e.g., 'tactical_drills', 'tactical')
ALTER TABLE public.authority_delegations
    DROP CONSTRAINT IF EXISTS authority_delegations_scope_check;

ALTER TABLE public.authority_delegations
    ADD CONSTRAINT authority_delegations_scope_check
    CHECK (char_length(scope) BETWEEN 1 AND 64);

-- 3. Add starts_at < expires_at check
ALTER TABLE public.authority_delegations
    DROP CONSTRAINT IF EXISTS authority_delegations_check;

ALTER TABLE public.authority_delegations
    ADD CONSTRAINT authority_delegations_time_window_chk
    CHECK (expires_at > starts_at AND expires_at > issued_at);

-- 4. Index for active delegations by club and delegate
CREATE INDEX IF NOT EXISTS authority_delegations_club_delegate_idx
    ON public.authority_delegations(club_id, delegate_id, expires_at)
    WHERE revoked_at IS NULL;

-- 5. Compatibility View fc27_delegations
CREATE OR REPLACE VIEW public.fc27_delegations AS
    SELECT 
        delegation_id,
        club_id,
        delegator_id,
        delegate_id,
        delegate_role,
        scope,
        allowed_action_types,
        target_player_id,
        target_package_type,
        starts_at,
        expires_at,
        revoked_at,
        revoked_by,
        revocation_reason,
        created_at
    FROM public.authority_delegations;

-- 6. Expand domain_events check constraint to accept authority_delegations & fc27_delegations
ALTER TABLE public.domain_events DROP CONSTRAINT IF EXISTS domain_events_source_table_check;
ALTER TABLE public.domain_events ADD CONSTRAINT domain_events_source_table_check
CHECK (source_table IN (
    'approval_audit',
    'approval_requests',
    'dispatches',
    'dispatch_acknowledgments',
    'dispatch_assessments',
    'dispatch_plans',
    'dispatch_plan_decisions',
    'dispatch_delivery_attempts',
    'user_roles',
    'activation_gate_log',
    'fc27_users',
    'fc27_user_roles',
    'fc27_invitations',
    'fc27_sessions',
    'fc27_webauthn_credentials',
    'fc27_totp_credentials',
    'fc27_recovery_codes',
    'fc27_action_tokens',
    'fc27_challenges',
    'authority_delegations',
    'fc27_delegations'
));
```

### 4.3 Grants Update: `migrations/post/grants.sql`
Bump `grants-policy-version: 7`:
```sql
    IF to_regclass('public.authority_delegations') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.authority_delegations TO fc27_app;
    END IF;
    IF to_regclass('public.fc27_delegations') IS NOT NULL THEN
        GRANT SELECT ON public.fc27_delegations TO fc27_app;
    END IF;
```

### 4.4 Identity Store Persistence Methods: `src/identity/store.py`

```python
    # =================================================================
    # 9. Human Delegations
    # =================================================================

    PROHIBITED_DELEGATION_ACTIONS = {
        "club_strategy", "team_tactics", "formations", "programme_release",
        "club_dispatch_activation", "policy_exception", "roster",
        "transfer", "recruitment", "discipline", "fair_play",
        "identity_change", "access_control", "delegation_policy"
    }

    def create_delegation(
        self,
        *,
        delegator_id: str,
        delegate_user_id: UUID,
        club_id: str,
        action_type: str | list[str],
        scope: str = "club",
        expires_in_hours: int = 24,
        starts_at: datetime | None = None,
        target_player_id: str | None = None,
        target_package_type: str | None = None,
        actor: str = "commissioner",
        correlation_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Creates an explicit, time-bounded, action-scoped delegation."""
        actions = [action_type] if isinstance(action_type, str) else list(action_type)
        if not actions:
            raise IdentityStoreError(422, "at least one action_type must be specified")

        # Prohibit delegating club governance actions
        for act in actions:
            if act in self.PROHIBITED_DELEGATION_ACTIONS:
                raise IdentityStoreError(403, f"action '{act}' cannot be delegated")

        if expires_in_hours <= 0 or expires_in_hours > 168:
            raise IdentityStoreError(422, "expires_in_hours must be between 1 and 168")

        now = datetime.now(timezone.utc)
        start_time = starts_at or now
        expires_at = start_time + timedelta(hours=expires_in_hours)
        delegation_id = uuid4()
        cid = correlation_id or uuid4()

        with self.connect() as conn, conn.transaction():
            # Validate delegate user exists, is in same club, and is active
            delegate_user = conn.execute(
                "SELECT club_id, status FROM public.fc27_users WHERE user_id = %s",
                (delegate_user_id,),
            ).fetchone()
            if not delegate_user:
                raise IdentityStoreError(404, "delegate user not found")
            if delegate_user["club_id"] != club_id:
                raise IdentityStoreError(403, "cannot delegate across clubs")
            if delegate_user["status"] != "active":
                raise IdentityStoreError(403, f"delegate user is {delegate_user['status']}, must be active")

            row = conn.execute(
                """
                INSERT INTO public.authority_delegations (
                    delegation_id, delegator_id, delegate_id, club_id, scope,
                    allowed_action_types, starts_at, expires_at,
                    target_player_id, target_package_type, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                RETURNING *
                """,
                (
                    delegation_id,
                    delegator_id,
                    str(delegate_user_id),
                    club_id,
                    scope,
                    actions,
                    start_time,
                    expires_at,
                    target_player_id,
                    target_package_type,
                ),
            ).fetchone()

            _safe_record_event(
                conn,
                event_type="identity.delegation_granted",
                entity_type="delegation",
                entity_id=str(delegation_id),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=club_id,
                reason=f"actions:{','.join(actions)}",
                source_table="authority_delegations",
                source_id=str(delegation_id),
            )
            res = dict(row)
            res["action_type"] = actions[0] if len(actions) == 1 else actions
            return res

    def get_delegation(self, delegation_id: UUID) -> dict[str, Any] | None:
        """Retrieves delegation by UUID."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM public.authority_delegations WHERE delegation_id = %s",
                (delegation_id,),
            ).fetchone()
            if not row:
                return None
            res = dict(row)
            actions = res.get("allowed_action_types") or []
            res["action_type"] = actions[0] if len(actions) == 1 else actions
            return res

    def list_delegations(
        self,
        club_id: str,
        delegate_user_id: UUID | None = None,
        active_only: bool = True,
    ) -> list[dict[str, Any]]:
        """Lists delegations for a club with optional active filtering."""
        with self.connect() as conn:
            clauses = ["club_id = %s"]
            params: list[Any] = [club_id]
            if delegate_user_id:
                clauses.append("delegate_id = %s")
                params.append(str(delegate_user_id))
            if active_only:
                clauses.append("revoked_at IS NULL AND expires_at > now()")

            where_sql = " AND ".join(clauses)
            rows = conn.execute(
                f"SELECT * FROM public.authority_delegations WHERE {where_sql} ORDER BY created_at DESC",
                params,
            ).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                actions = d.get("allowed_action_types") or []
                d["action_type"] = actions[0] if len(actions) == 1 else actions
                result.append(d)
            return result

    def revoke_delegation(
        self,
        delegation_id: UUID,
        *,
        revoked_by: str,
        reason: str = "commissioner_revoked",
        actor: str = "commissioner",
        correlation_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Explicitly revokes an active delegation immediately."""
        cid = correlation_id or uuid4()
        with self.connect() as conn, conn.transaction():
            row = conn.execute(
                "SELECT * FROM public.authority_delegations WHERE delegation_id = %s FOR UPDATE",
                (delegation_id,),
            ).fetchone()
            if not row:
                raise IdentityStoreError(404, "delegation not found")
            if row["revoked_at"] is not None:
                return dict(row)

            updated = conn.execute(
                """
                UPDATE public.authority_delegations
                SET revoked_at = now(), revoked_by = %s, revocation_reason = %s
                WHERE delegation_id = %s
                RETURNING *
                """,
                (revoked_by, reason, delegation_id),
            ).fetchone()

            _safe_record_event(
                conn,
                event_type="identity.delegation_revoked",
                entity_type="delegation",
                entity_id=str(delegation_id),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=row["club_id"],
                reason=reason,
                source_table="authority_delegations",
                source_id=str(delegation_id),
            )
            return dict(updated)

    def evaluate_delegation(
        self,
        delegation_id: UUID,
        *,
        principal_user_id: UUID,
        club_id: str,
        action: str,
        resource: dict[str, Any],
    ) -> tuple[bool, str]:
        """Pure evaluation function verifying active unexpired unrevoked scope-matching delegation."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM public.authority_delegations WHERE delegation_id = %s",
                (delegation_id,),
            ).fetchone()
            if not row:
                return False, "delegation_not_found"

            if row["delegate_id"] != str(principal_user_id):
                return False, "delegate_user_mismatch"

            if row["revoked_at"] is not None:
                return False, "delegation_revoked"

            now = datetime.now(timezone.utc)
            if row["starts_at"] and now < row["starts_at"]:
                return False, "delegation_not_yet_active"

            if now >= row["expires_at"]:
                return False, "delegation_expired"

            if row["club_id"] != club_id:
                return False, "club_mismatch"

            allowed = set(row.get("allowed_action_types") or [])
            if action not in allowed:
                return False, "action_type_not_delegated"

            if row.get("target_player_id"):
                res_player = resource.get("subject_player_id") or resource.get("player_id")
                if str(row["target_player_id"]) != str(res_player):
                    return False, "target_player_mismatch"

            return True, "authorized"
```

### 4.5 Centralized Policy Engine: `src/identity/policy.py`

```python
"""Centralized Object-Level Authorization Engine (Phase 2 Milestone 2)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from src.identity.store import IdentityStore


@dataclass(frozen=True)
class PrincipalContext:
    user_id: UUID
    club_id: str
    roles: set[str]
    auth_level: str  # 'authenticated', 'mfa_totp', 'step_up_passkey'
    auth_method: str # 'passkey', 'totp', 'service_token', 'recovery_code'
    session_id: UUID | None = None
    player_id: str | None = None


class AuthorizationDenied(Exception):
    def __init__(self, status_code: int = 403, detail: str = "Forbidden"):
        super().__init__(f"{status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


def authorize(
    principal: PrincipalContext,
    action: str,
    resource: dict[str, Any],
    *,
    authority_basis: str | None = None,
    delegation_id: UUID | None = None,
    store: IdentityStore | None = None,
) -> bool:
    """Centralized authorization evaluating subject, object, tenancy, roles, and delegation.

    Raises AuthorizationDenied(403) on denial; returns True on permission.
    """
    # 1. Tenancy Guard: Principal club must match resource club
    target_club_id = str(resource.get("club_id", principal.club_id))
    if principal.club_id != target_club_id:
        raise AuthorizationDenied(403, "cross_club_access_forbidden")

    # 2. Service Non-Impersonation Guard
    if "bff_service" in principal.roles and action.startswith("decision."):
        raise AuthorizationDenied(403, "bff_cannot_commit_human_decisions")
    if "graph_service" in principal.roles and action.startswith("decision."):
        raise AuthorizationDenied(403, "graph_service_cannot_commit_human_decisions")

    # 3. Object-Level Player Isolation
    if principal.roles == {"player"} or ("player" in principal.roles and len(principal.roles) == 1):
        # Dedicated player attempting to access an assignment
        subj_player = resource.get("subject_player_id") or resource.get("player_id")
        if subj_player and str(subj_player) != str(principal.player_id):
            raise AuthorizationDenied(403, "player_cross_tenant_isolation_violation")
        # Player role cannot access commissioner endpoints
        if action.startswith("commissioner.") or action.startswith("delegation.create"):
            raise AuthorizationDenied(403, "player_cannot_access_commissioner_actions")

    # 4. Authority Basis Evaluation
    basis = authority_basis or "primary"

    if basis == "primary":
        if "commissioner" not in principal.roles:
            raise AuthorizationDenied(403, "primary_authority_requires_commissioner_role")
        return True

    elif basis == "explicit_delegate":
        if not delegation_id:
            raise AuthorizationDenied(403, "delegation_id_required_for_delegated_authority")
        if not store:
            raise AuthorizationDenied(500, "identity_store_required_for_delegation_eval")

        allowed, reason = store.evaluate_delegation(
            delegation_id,
            principal_user_id=principal.user_id,
            club_id=principal.club_id,
            action=action,
            resource=resource,
        )
        if not allowed:
            raise AuthorizationDenied(403, f"delegation_denied:{reason}")
        return True

    elif basis == "affected_player_consent":
        if "player" not in principal.roles:
            raise AuthorizationDenied(403, "player_consent_requires_player_role")
        subj_player = resource.get("subject_player_id")
        if not subj_player or str(subj_player) != str(principal.player_id):
            raise AuthorizationDenied(403, "player_consent_only_for_own_assignment")
        return True

    elif basis == "affected_player_fallback":
        if "player" not in principal.roles:
            raise AuthorizationDenied(403, "player_fallback_requires_player_role")
        subj_player = resource.get("subject_player_id")
        if not subj_player or str(subj_player) != str(principal.player_id):
            raise AuthorizationDenied(403, "player_fallback_only_for_own_assignment")
        if not resource.get("fallback_eligible", False):
            raise AuthorizationDenied(403, "assignment_not_eligible_for_player_fallback")
        return True

    else:
        raise AuthorizationDenied(403, f"unsupported_authority_basis:{basis}")
```

### 4.6 REST Router Specification: `src/identity/routers/delegations.py`
Endpoints under `/v1/auth/delegations`:
- `POST /v1/auth/delegations`:
  - Request: `{"delegate_user_id": str, "club_id": str, "action_type": str, "scope": str, "expires_in_hours": int}`
  - Auth: Commissioner session (`require_role("commissioner")`).
  - Response 201: `{"delegation_id": str, "club_id": str, "delegate_user_id": str, "action_type": str, "scope": str, "expires_at": str}`
- `GET /v1/auth/delegations`:
  - Auth: Commissioner or authenticated staff.
  - Query: `active_only=true`.
  - Response 200: list of delegation objects.
- `DELETE /v1/auth/delegations/{delegation_id}`:
  - Auth: Commissioner session.
  - Response 200/204: immediate revocation.

### 4.7 Inactivity & Timeout Invariants (G-TIMEOUT) Guard Specification

```
                         [Approval Request Created]
                                     |
                                     v
                        Status: awaiting_approval
                         Escalation: awaiting_primary
                                     |
                                     +-----> (Timeout / Inactivity Detected)
                                     |
                                     v
                  UPDATE fc27_approval_escalations
                  SET to_state = 'evaluating_fallback', trigger_reason = 'timer_elapsed'
                                     |
                                     v
                  Does policy permit fallback for this personal assignment?
                                    / \
                             YES   /   \   NO (Club-wide / Roster / Transfer)
                                  /     \
                                 v       v
         to_state = awaiting_affected_player   to_state = held_no_authorized_approver
                  |                                     |
                  v                                     v
      Eligible: Affected Player               Eligible: None (Holds for Commissioner)
                  |                                     |
   +--------------+--------------+                      |
   |                             |                      |
(Authenticated Player         (Player Idle /            |
 Passkey Decision)             Request Expired)         |
   |                             |                      |
   v                             v                      v
Status: approved / rejected    Status: expired       Status: held / expired
No delivery (Dormant)          No Action Taken       No Action Taken
```
**Core Invariant Rules**:
1. `SELECT ... WHERE timer_elapsed`: Updates `fc27_approval_escalations` table ONLY.
2. Under no circumstance is `INSERT INTO public.approval_audit (decision, ...)` or `UPDATE approval_requests SET status = 'approved'` executed by a timer or scheduler.
3. Automated systems (`actor_type = 'system'`) are barred from submitting approvals.
4. An expired request transitions to `status = 'expired'`; nonces are revoked.

---

## 5. Verification Method

To verify the implementation of Milestone 2:

### 5.1 Migration Execution & Integrity
Run database migration to verify `0005_delegations.sql` applies cleanly:
```bash
python -m src.db_migrate
```
Verify postconditions and grants:
```sql
SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'authority_delegations';
SELECT table_name FROM information_schema.views WHERE table_name = 'fc27_delegations';
SELECT has_table_privilege('fc27_app', 'authority_delegations', 'INSERT');
```

### 5.2 Unit & Component Tests
Execute newly authored test suite in `tests/identity/test_policy.py` and `tests/identity/test_delegations.py`:
```bash
pytest -v tests/identity/test_policy.py tests/identity/test_delegations.py
```
Key assertions:
1. `test_delegation_creation_and_expiration`: Verifies time bounds and scope storage.
2. `test_delegation_cross_club_denial`: Verifies delegate in club B cannot be delegated authority for club A.
3. `test_delegation_immediate_revocation`: Verifies mid-flight revocation immediately returns 403 on subsequent evaluation.
4. `test_prohibited_delegation_actions`: Verifies attempting to delegate `club_strategy`, `formations`, or `transfer` raises 403.
5. `test_player_isolation_cross_player_blocked`: Verifies player 1 cannot access or decide player 2's assignment.
6. `test_g_timeout_invariant_no_approval_on_expiry`: Verifies timer expiry updates escalation state to `evaluating_fallback` or `held_no_authorized_approver` and NEVER creates an approval decision.

### 5.3 E2E Integration Suite Verification
Run the integrated E2E test suite:
```bash
pytest -v tests/e2e/test_phase2_tier1_features.py -k "delegation"
pytest -v tests/e2e/test_phase2_tier3_combinations.py -k "delegation"
pytest -v tests/e2e/test_tier4_scenarios.py -k "delegation"
```

### 5.4 Invalidation Conditions
This design is invalidated if:
1. `authority_delegations` is dropped or replaced without preserving the foreign key constraint from `approval_audit.delegation_id`.
2. A timeout is observed inserting an `approved` decision row into `approval_audit`.
3. A player agent is permitted to authenticate on behalf of a human player.
4. A delegate is permitted to authorize club strategy or recursive delegations.
