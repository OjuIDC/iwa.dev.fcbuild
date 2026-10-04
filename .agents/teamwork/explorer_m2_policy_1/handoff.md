# Phase 2 Milestone 2: Centralized Authorization & Service Boundaries
## Handoff Report: `src/identity/policy.py` Design & Specification

**Author**: `explorer_m2_policy_1` (Authorization Policy & RBAC/ABAC Designer)  
**Recipient**: `parent` (Orchestrator, ID: `25210876-a9ad-4f78-a87c-dec88e86edb9`)  
**Milestone**: Phase 2 Milestone 2 — Centralized Authorization & Service Boundaries  
**Target File**: `src/identity/policy.py`  
**Reference Artifacts**:
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_policy_1\proposed_policy.py` (Full drop-in implementation)
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_policy_1\proposed_test_policy.py` (Complete unit & adversarial test suite)

---

## 1. Observation

Direct inspection of authoritative project specifications, database migrations, and existing implementations revealed:

1. **Phase 2 Authorization Requirements** (`ORIGINAL_REQUEST.md` §7, §4, §1A.8):
   - §7 requires:
     > "Implement centralized authorization rather than duplicating role checks in individual routes. Conceptual API: `authorize(principal, action, resource, authority_basis=None)`. Authorization must evaluate both subject and object."
   - §7.2 requires:
     > "Player isolation: A player must not read another player's private assignment, submit another player's decision, obtain another player's decision challenge, revoke another player's credential, or access staff-only authorization data. Test this as object-level authorization, not merely route-role authorization."
   - §7.3 requires:
     > "Delegation validation must check: principal -> delegation holder -> active -> not expired -> not revoked -> club scope -> action scope -> resource scope -> required authentication strength. A delegation cannot be broadened through interpretation. No matching authorization means deny."
   - §4 establishes four distinct authority bases:
     > "`primary`, `explicit_delegate`, `affected_player_consent`, `affected_player_fallback`. These must remain distinguishable in storage, authorization logic, API contracts, and audit evidence."
   - §1A.5 & §1A.6 detail player fallback rules:
     > "Eligible assignments are strictly developmental and player-specific (personal training, build recommendations, AP allocations, drill schedules, Mastery/Amp). A player must not use fallback authority to approve: another player's assignment, a club-wide package, team tactics, formations, transfers, recruitment, roster changes, discipline, fair-play determinations, policy exceptions, identity/access control changes, delegation policy changes, or global dispatch activation."

2. **Database Migration Schema** (`migrations/0004_identity_foundation.sql` lines 76-90):
   - The authoritative application roles defined in `public.fc27_user_roles` are:
     ```sql
     'commissioner',
     'commissioner_delegate',
     'coach',
     'analyst',
     'staff',
     'player',
     'bff_service',
     'graph_service'
     ```
   - User table `public.fc27_users` defines `user_id` as UUID PK, `club_id text NOT NULL`, `player_id text NULL REFERENCES public.players(player_id)`, `login_id text NOT NULL UNIQUE`, and `display_name text NOT NULL`. Trigger `fc27_users_status_guard` enforces immutability of `user_id` and `club_id`.
   - `fc27_sessions` defines `auth_level` (`authenticated`, `mfa_totp`, `step_up_passkey`) and `auth_method` (`passkey`, `totp`, `service_token`, `recovery_code`).

3. **Phase 1 Authority Policies & Delegation Schema** (`migrations/0003_dispatch_foundation.sql` lines 465-514):
   - `public.authority_policies` explicitly constrains:
     ```sql
     CHECK (allow_player_fallback = false OR scope = 'player_specific')
     CHECK (allow_player_fallback = false OR action_type NOT IN (
         'club_strategy', 'team_tactics', 'formations', 'programme_release',
         'club_dispatch_activation', 'policy_exception', 'roster',
         'transfer', 'recruitment', 'discipline', 'fair_play',
         'identity_change', 'access_control', 'delegation_policy'
     ))
     ```
   - `public.authority_delegations` specifies:
     `delegator_id text`, `delegate_id text`, `scope text CHECK (scope IN ('club', 'player_specific'))`, `allowed_action_types text[]`, `expires_at`, `revoked_at`.

4. **Phase 1 Security Baseline** (`src/security.py` lines 31-41):
   - Contains a rudimentary 2-role Authenticator (`Role.service` and `Role.commissioner`) designed for Phase 1 bootstrap, which must be superseded in Phase 2 by centralized object-scoped authorization in `src/identity/policy.py`.

5. **Existing Milestone 1 Persistence Layer** (`src/identity/store.py` lines 71-91):
   - Standardizes constants:
     `VALID_ROLES`, `VALID_AUTH_LEVELS = {"authenticated", "mfa_totp", "step_up_passkey"}`, `VALID_AUTH_METHODS = {"passkey", "totp", "service_token", "recovery_code"}`.

---

## 2. Logic Chain

From these direct observations, the architectural necessity and logical requirements for `src/identity/policy.py` are derived:

1. **Centralized Engine vs. Ad-Hoc Route Checks**:
   - Because route-level checks easily miss object tenancy and player boundaries, all authorization decisions must flow through a single centralized engine `authorize(principal, action, resource, authority_basis, delegation)` returning a structured `DecisionResult` or raising `AuthorizationError` (HTTP 403).
   - This prevents security drift between FastAPI endpoints (`/v1/auth/*`, `/v1/player-*`, `/v1/approval-*`, and `/v1/runs/*`).

2. **Core Domain Types (Observation 2 & 5)**:
   - `UserRole`: Must map exactly to the 8 roles in `fc27_user_roles`.
   - `AuthorityBasis`: Must model the 4 conceptual authority bases (`primary`, `explicit_delegate`, `affected_player_consent`, `affected_player_fallback`).
   - `AuthLevel` & `AuthMethod`: Must align with `fc27_sessions` in `store.py`.
   - `ActionType`: Must categorize domain actions into unambiguous string enums across packages, assignments, scouting/coaching/analytics, users, invitations, delegations, credentials, sessions, and workflow runs.
   - `PrincipalContext`: Must be an immutable context holding `user_id: UUID`, `club_id: str`, `roles: frozenset[UserRole]`, `auth_level: AuthLevel`, `auth_method: AuthMethod`, `session_id: UUID | None`, `player_id: str | None`, and `is_service: bool`.
   - `Resource`: Must represent the object target with `resource_type`, `club_id`, `owner_user_id`, `player_id`, `scope`, `action_type`, and attributes. Must provide `from_dict()` for seamless backward compatibility.

3. **Tenancy Isolation Logic (Observation 2)**:
   - In multi-tenant architecture, `principal.club_id == resource.club_id` is an absolute prerequisite.
   - Any cross-club operation must immediately raise `CrossClubAccessError` before evaluating permissions or data.

4. **Player Isolation Logic (Observation 1 & 2)**:
   - A player user may only access resources explicitly bound to them: `resource.player_id == principal.player_id` or `resource.owner_user_id == principal.user_id`.
   - A player cannot access club-wide scoped packages (`scope == "club"`) or other players' assignments.
   - Player consent (`affected_player_consent`) must bind strictly to the subject player.
   - Affected-player fallback (`affected_player_fallback`) must satisfy all Section 1A constraints:
     - Must be `scope == "player_specific"`.
     - `action_type` cannot be in `PROHIBITED_PLAYER_FALLBACK_ACTIONS`.
     - Multi-player and mixed-scope packages are strictly rejected.

5. **Delegation Evaluation Logic (Observation 1 & 3)**:
   - The `commissioner_delegate` role permits package decisions ONLY when accompanied by a valid `AuthorityDelegationContext`.
   - Must evaluate: `delegation.is_active is True`, `delegation.revoked_at is None`, `delegation.starts_at <= now <= delegation.expires_at`, `delegation.delegate_id == str(principal.user_id)`, `delegation.club_id == principal.club_id`, and `action in delegation.allowed_action_types`.
   - If `delegation.scope == "player_specific"`, the delegate cannot authorize a club-wide package.
   - Delegates cannot create further delegations (recursive delegation prohibited).

6. **Anti-Impersonation Logic (Observation 1)**:
   - Services (`bff_service`, `graph_service`, or background workers) have `is_service=True`.
   - Services must NEVER be permitted to execute actions in `HUMAN_DECISION_ACTIONS` (`PACKAGE_APPROVE`, `PACKAGE_REJECT`, `ASSIGNMENT_ACCEPT`, `ASSIGNMENT_REJECT`, etc.).
   - Attempted execution raises `AgentImpersonationError` immediately.

7. **High-Value Decision Step-Up Logic (Observation 1 & 2)**:
   - High-value Commissioner approvals (`PACKAGE_APPROVE`, `PACKAGE_REJECT`) require cryptographic WebAuthn step-up evidence (`principal.auth_level == AuthLevel.STEP_UP_PASSKEY`). Failure to provide step-up raises `StepUpRequiredError`.

8. **Inactivity and Timeout Invariants**:
   - Inactivity and timeouts alter workflow eligibility/escalation state, but never generate approval or decision records. Automated systems cannot synthesize human approval.

---

## 3. Caveats

1. **Database-Backed Delegation Lookup vs. In-Memory Context**:
   - The core `authorize()` function accepts an `AuthorityDelegationContext` parameter. When invoked in web routers, an authorization dependency will fetch the active delegation record from `store.py` (or cache) and pass it to `authorize()`.
2. **Phase 1 CLI/HMAC Decision Kill Switch**:
   - `APPROVAL_CLI_HMAC_ENABLED` allows legacy HMAC decisions during transition. However, inside `policy.py`, authorization checks are role- and proof-type neutral, validating that the caller holds the required authority basis and authentication level.
3. **Dispatch Worker & Relay Boundary**:
   - `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` remain active throughout Phase 2. `policy.py` contains guards against dispatch execution, but actual dispatch worker implementation is deferred to Phase 3.

---

## 4. Conclusion & Detailed Design

The centralized authorization system is fully articulated in `proposed_policy.py` and validated by `proposed_test_policy.py`.

### 4.1 Core Domain Types Specification

```python
class UserRole(str, Enum):
    COMMISSIONER = "commissioner"
    COMMISSIONER_DELEGATE = "commissioner_delegate"
    COACH = "coach"
    ANALYST = "analyst"
    STAFF = "staff"
    PLAYER = "player"
    BFF_SERVICE = "bff_service"
    GRAPH_SERVICE = "graph_service"

class AuthorityBasis(str, Enum):
    PRIMARY = "primary"
    EXPLICIT_DELEGATE = "explicit_delegate"
    AFFECTED_PLAYER_CONSENT = "affected_player_consent"
    AFFECTED_PLAYER_FALLBACK = "affected_player_fallback"

class AuthLevel(str, Enum):
    AUTHENTICATED = "authenticated"
    MFA_TOTP = "mfa_totp"
    STEP_UP_PASSKEY = "step_up_passkey"

class AuthMethod(str, Enum):
    PASSKEY = "passkey"
    TOTP = "totp"
    SERVICE_TOKEN = "service_token"
    RECOVERY_CODE = "recovery_code"

class ActionType(str, Enum):
    PACKAGE_VIEW = "package:view"
    PACKAGE_APPROVE = "package:approve"
    PACKAGE_REJECT = "package:reject"
    PACKAGE_REQUEST_REVISION = "package:request_revision"
    APPROVAL_CHALLENGE = "approval:challenge"
    ASSIGNMENT_VIEW = "assignment:view"
    ASSIGNMENT_CHALLENGE = "assignment:challenge"
    ASSIGNMENT_ACCEPT = "assignment:accept"
    ASSIGNMENT_REJECT = "assignment:reject"
    ASSIGNMENT_CHANGE_REQUEST = "assignment:change_request"
    ASSIGNMENT_PAUSE = "assignment:pause"
    ASSIGNMENT_ACKNOWLEDGE = "assignment:acknowledge"
    ROSTER_VIEW = "roster:view"
    OBSERVATION_VIEW = "observation:view"
    OBSERVATION_CREATE = "observation:create"
    ASSESSMENT_VIEW = "assessment:view"
    ASSESSMENT_CREATE = "assessment:create"
    USER_VIEW = "user:view"
    USER_CREATE = "user:create"
    USER_UPDATE_STATUS = "user:update_status"
    USER_ROLE_ASSIGN = "user:role_assign"
    USER_ROLE_REVOKE = "user:role_revoke"
    INVITATION_VIEW = "invitation:view"
    INVITATION_CREATE = "invitation:create"
    INVITATION_REVOKE = "invitation:revoke"
    INVITATION_ACCEPT = "invitation:accept"
    DELEGATION_VIEW = "delegation:view"
    DELEGATION_CREATE = "delegation:create"
    DELEGATION_REVOKE = "delegation:revoke"
    PASSKEY_VIEW = "passkey:view"
    PASSKEY_REGISTER = "passkey:register"
    PASSKEY_REVOKE = "passkey:revoke"
    TOTP_ENROLL = "totp:enroll"
    TOTP_VERIFY = "totp:verify"
    TOTP_REVOKE = "totp:revoke"
    SESSION_VIEW = "session:view"
    SESSION_REVOKE = "session:revoke"
    SESSION_REVOKE_ALL = "session:revoke_all"
    RUN_VIEW = "run:view"
    RUN_CREATE = "run:create"
    RUN_RESUME = "run:resume"
```

### 4.2 8-Role Authorization Matrix

| Role | Permitted Actions | Prohibited Actions & Invariants |
|---|---|---|
| `commissioner` | Full club authority: `PACKAGE_*`, `APPROVAL_CHALLENGE`, `USER_*`, `INVITATION_*`, `DELEGATION_*`, `ROSTER_VIEW`, `OBSERVATION_*`, `ASSESSMENT_*`, `PASSKEY_*`, `SESSION_*`, `RUN_VIEW` | Cross-club actions prohibited; cannot approve via automated agent; step-up passkey required for package decisions; cannot impersonate player consent. |
| `commissioner_delegate` | Package decisions (`PACKAGE_APPROVE`, `PACKAGE_REJECT`, `PACKAGE_REQUEST_REVISION`) conditional on delegation; `PACKAGE_VIEW`, `ASSIGNMENT_VIEW`, `ROSTER_VIEW`, `OBSERVATION_VIEW`, `ASSESSMENT_VIEW`, `DELEGATION_VIEW`, `PASSKEY_*`, `SESSION_*`, `RUN_VIEW` | Cannot create delegations (no recursive delegation); cannot manage user roles; cannot exceed delegation action or scope; step-up passkey required. |
| `coach` | `PACKAGE_VIEW`, `ASSIGNMENT_VIEW`, `ROSTER_VIEW`, `OBSERVATION_*`, `ASSESSMENT_*`, `USER_VIEW`, `PASSKEY_*`, `TOTP_*`, `SESSION_*` | Cannot approve packages (unless delegated); cannot manage user accounts or roles; cannot submit decisions for players. |
| `analyst` | `PACKAGE_VIEW`, `ASSIGNMENT_VIEW`, `ROSTER_VIEW`, `OBSERVATION_*`, `ASSESSMENT_*`, `USER_VIEW`, `TOTP_*`, `SESSION_*` | Cannot approve packages; cannot manage users or delegations; cannot submit decisions for players. |
| `staff` | `ROSTER_VIEW`, `USER_VIEW`, `INVITATION_VIEW`, `INVITATION_CREATE` (non-privileged), `TOTP_*`, `SESSION_*` | Cannot approve packages; cannot grant delegations; cannot modify player records. |
| `player` | `ASSIGNMENT_VIEW`, `ASSIGNMENT_CHALLENGE`, `ASSIGNMENT_ACCEPT`, `ASSIGNMENT_REJECT`, `ASSIGNMENT_CHANGE_REQUEST`, `ASSIGNMENT_PAUSE`, `ASSIGNMENT_ACKNOWLEDGE`, `USER_VIEW` (self), `PASSKEY_*`, `SESSION_*` | Strictly isolated: cannot access another player's assignment, cannot access club-wide packages; fallback forbidden on prohibited actions (transfers, tactics, rosters, discipline). |
| `bff_service` | `RUN_VIEW`, `PACKAGE_VIEW`, `ASSIGNMENT_VIEW`, `ROSTER_VIEW`, `OBSERVATION_VIEW`, `ASSESSMENT_VIEW`, `USER_VIEW` | Read views only; zero direct DB access; strictly forbidden from submitting human decisions (`HUMAN_DECISION_ACTIONS`). |
| `graph_service` | `RUN_CREATE`, `RUN_VIEW`, `RUN_RESUME` | Internal workflow execution only; least-privilege DB role (`fc27_app`); strictly forbidden from approving packages or delivering to players. |

### 4.3 Evaluation Pipeline (`authorize()`)

```
Incoming Request (principal, action, resource, authority_basis, delegation)
   │
   ├── 1. Action Normalization & Dispatch Invariant Check
   │      └─ Fail if action requests dispatch delivery (DISPATCH_ENABLED=false)
   │
   ├── 2. Tenancy Boundary Check
   │      └─ Fail if principal.club_id != resource.club_id -> CrossClubAccessError (403)
   │
   ├── 3. Anti-Impersonation Guard
   │      └─ Fail if principal.is_service and action in HUMAN_DECISION_ACTIONS -> AgentImpersonationError (403)
   │
   ├── 4. Base Role Permission Check
   │      └─ Fail if action not in _BASE_ROLE_PERMISSIONS for any role -> RoleNotPermittedError (403)
   │
   ├── 5. Object-Level Player Isolation Guard
   │      └─ If principal is player:
   │            ├─ Must match resource.player_id == principal.player_id OR resource.owner_user_id == principal.user_id
   │            └─ Cannot access resource.scope == "club" -> PlayerIsolationError (403)
   │
   ├── 6. Human Delegation Guard (for commissioner_delegate)
   │      └─ If delegate approving package:
   │            ├─ Validate delegation active, unrevoked, unexpired
   │            ├─ Validate delegation.delegate_id == principal.user_id
   │            ├─ Validate action in delegation.allowed_action_types
   │            └─ Validate delegation.scope matches resource.scope -> DelegationInvalidError (403)
   │
   ├── 7. Authority Basis Validation
   │      ├─ PRIMARY requires commissioner role
   │      ├─ EXPLICIT_DELEGATE requires commissioner_delegate role
   │      ├─ AFFECTED_PLAYER_CONSENT requires player role on own assignment
   │      └─ AFFECTED_PLAYER_FALLBACK requires:
   │            ├─ player role on own assignment
   │            ├─ scope == "player_specific"
   │            └─ action_type NOT in PROHIBITED_PLAYER_FALLBACK_ACTIONS
   │
   ├── 8. Step-Up Passkey Requirement
   │      └─ If action in (PACKAGE_APPROVE, PACKAGE_REJECT):
   │            └─ Require principal.auth_level == AuthLevel.STEP_UP_PASSKEY -> StepUpRequiredError (403)
   │
   └── RETURN DecisionResult(allowed=True, ...)
```

---

## 5. Verification Method

To independently verify this design and code artifacts:

1. **Inspect Proposed Implementation Files**:
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_policy_1\proposed_policy.py`
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_policy_1\proposed_test_policy.py`

2. **Integration Verification Command** (once placed into `src/identity/policy.py` and `tests/identity/test_policy.py` by the implementer):
   ```bash
   pytest tests/identity/test_policy.py -v
   ```

3. **Check Acceptance Invariants**:
   - Cross-club attempt between Club `101` and `102` raises `CrossClubAccessError` (HTTP 403).
   - Player accessing another player's assignment raises `PlayerIsolationError` (HTTP 403).
   - BFF service attempting `PACKAGE_APPROVE` raises `AgentImpersonationError` (HTTP 403).
   - Commissioner approving without step-up passkey raises `StepUpRequiredError` (HTTP 403).
   - Player fallback attempting tactics or transfer raises `RoleNotPermittedError` (HTTP 403).
   - Expired or revoked delegation raises `DelegationInvalidError` (HTTP 403).

4. **Invalidation Conditions**:
   - Any modification that allows a service principal to approve a package.
   - Any route allowing player access to another player's assignment.
   - Any fallback policy allowing club-wide tactics or multi-player packages.
