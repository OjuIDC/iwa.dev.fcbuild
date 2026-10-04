# Phase 2 Milestone 2: Adversarial Challenge Report

**Author**: `challenger_m2_1`  
**Role**: Object Tenancy & Player Isolation Challenger (critic, specialist)  
**Target Milestone**: Phase 2 Milestone 2 — Centralized Authorization & Service Boundaries  
**Target Path**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m2_1\challenge_report.md`  
**Test Suite**: `tests/identity/test_adversarial_m2_1.py`

---

## 1. Challenge Summary

**Overall Risk Assessment**: **LOW**

The authorization engine implemented in `src/identity/policy.py`, `src/identity/services.py`, `src/identity/deps.py`, `src/identity/store.py`, `migrations/0005_delegations.sql`, and `migrations/post/grants.sql` exhibits exceptional architectural rigor and fail-closed security. All non-negotiable invariants from `ORIGINAL_REQUEST.md` (§3, §4, §7, §11, §12) and the Milestone 2 dispatch instructions are strictly enforced.

Zero authorization bypasses were detected. All cross-club access attempts, player isolation violations, agent impersonation attempts, and step-up bypass attempts fail closed with specific domain HTTP 403 exceptions (`CrossClubAccessError`, `PlayerIsolationError`, `AgentImpersonationError`, `StepUpRequiredError`, `DelegationInvalidError`).

---

## 2. Challenges & Adversarial Attack Vectors

### [High] Challenge 1: Object Tenancy Boundary Across All Application Roles

- **Assumption Challenged**: Can any application role (specifically high-privilege roles like Commissioner, or service roles like BFF) bypass club tenancy checks and access resources belonging to a foreign club?
- **Attack Scenario**: 
  - Construct principal contexts for all 8 application roles (`commissioner`, `commissioner_delegate`, `coach`, `analyst`, `staff`, `player`, `bff_service`, `graph_service`) belonging to Club `1899`.
  - Attempt access (`PACKAGE_VIEW`, `ASSIGNMENT_VIEW`, etc.) against target resources in Club `1900`.
  - Attempt access with missing, blank (`""`), or whitespace-padded (`" 1899"`) `club_id` attributes.
- **Blast Radius**: Cross-tenant data exfiltration or unauthorized multi-club modification.
- **Defense Observed**:
  - `src/identity/policy.py` lines 645–656 execute the Tenancy Isolation Guard before role-specific or object-specific evaluation.
  - If `not res.club_id` or `principal.club_id != res.club_id`, execution halts immediately with `CrossClubAccessError` (HTTP 403, error code `CROSS_CLUB_ACCESS_DENIED`).
  - Whitespace-padded strings fail exact string matching.
- **Verdict**: **DEFENDED (PASS)**

---

### [Critical] Challenge 2: Object-Level Player Isolation & Prohibited Fallback Actions

- **Assumption Challenged**: Can an authenticated player view or decide another player's assignment, access another player's credentials/sessions, authorize club-wide tactical packages, or use affected-player fallback to authorize club governance actions?
- **Attack Scenario**:
  - Player 1 (`10001`) attempts `ASSIGNMENT_VIEW`, `ASSIGNMENT_CHALLENGE`, and `ASSIGNMENT_ACCEPT` on Player 2's (`10002`) assignment.
  - Player 1 attempts to view Player 2's user record, passkeys, and server-side sessions.
  - Player 1 asserts `authority_basis=affected_player_consent` for Player 2's assignment.
  - Player 1 asserts `authority_basis=affected_player_fallback` on club-wide (`scope="club"`) or mixed-scope packages.
  - Player 1 asserts `authority_basis=affected_player_fallback` across all 14 prohibited action types:
    `club_strategy`, `team_tactics`, `formations`, `programme_release`, `club_dispatch_activation`, `policy_exception`, `roster`, `transfer`, `recruitment`, `discipline`, `fair_play`, `identity_change`, `access_control`, `delegation_policy`.
  - Player with unverified EA player ID (`player_id=None`) attempts to match a numeric player ID.
- **Blast Radius**: Horizontal privilege escalation between players; players hijacking club strategy or team formations.
- **Defense Observed**:
  - `src/identity/policy.py` lines 679–698 enforce strict object-level isolation: a player can only access resources where `player_id == principal.player_id` or `owner_user_id == principal.user_id`. Fails with `PlayerIsolationError`.
  - Access to `scope="club"` by players raises `PlayerIsolationError`.
  - Lines 780–817 enforce authority basis constraints: fallback on club-wide scope raises `PlayerIsolationError`; fallback on any of the 14 prohibited actions raises `RoleNotPermittedError`.
  - Unverified player ID (`None`) cannot match valid numeric IDs.
- **Verdict**: **DEFENDED (PASS)**

---

### [Critical] Challenge 3: Anti-Impersonation & Service Boundary Integrity

- **Assumption Challenged**: Can an automated service principal (`bff_service`, `graph_service`, or background worker) authenticate or decide for a human actor, or masquerade as a human by forging roles?
- **Attack Scenario**:
  - `bff_service`, `graph_service`, and worker service principals attempting each of the 12 `HUMAN_DECISION_ACTIONS`:
    `PACKAGE_APPROVE`, `PACKAGE_REJECT`, `PACKAGE_REQUEST_REVISION`, `ASSIGNMENT_ACCEPT`, `ASSIGNMENT_REJECT`, `ASSIGNMENT_CHANGE_REQUEST`, `ASSIGNMENT_PAUSE`, `ASSIGNMENT_ACKNOWLEDGE`, `DELEGATION_CREATE`, `DELEGATION_REVOKE`, `USER_ROLE_ASSIGN`, `USER_ROLE_REVOKE`.
  - An attacker constructing a principal with `is_service=True` but injecting `roles=[UserRole.COMMISSIONER]`.
  - Direct calls to `require_human_principal()` and `AntiImpersonationGuard.assert_human_decision_eligibility()`.
- **Blast Radius**: Automated agents or background workers committing unauthorized approvals or bypassing human governance.
- **Defense Observed**:
  - `src/identity/policy.py` lines 659–664 explicitly check `if principal.is_service and act in HUMAN_DECISION_ACTIONS: raise AgentImpersonationError(...)`.
  - Even if `COMMISSIONER` role is injected into `roles`, `is_service=True` halts execution immediately.
  - `require_human_principal()` in `services.py` and `deps.py` immediately rejects service callers with HTTP 403 `AGENT_IMPERSONATION_FORBIDDEN`.
- **Verdict**: **DEFENDED (PASS)**

---

### [High] Challenge 4: Cryptographic Step-Up Passkey Requirements

- **Assumption Challenged**: Can a Commissioner or Commissioner Delegate commit a high-value package decision (`PACKAGE_APPROVE` or `PACKAGE_REJECT`) using a regular password or TOTP session without WebAuthn passkey step-up assertion?
- **Attack Scenario**:
  - Commissioner with `auth_level=AuthLevel.AUTHENTICATED` attempts `PACKAGE_APPROVE` and `PACKAGE_REJECT`.
  - Commissioner with `auth_level=AuthLevel.MFA_TOTP` attempts `PACKAGE_APPROVE`.
  - Commissioner Delegate with valid delegation but `auth_level=AuthLevel.AUTHENTICATED` attempts `PACKAGE_APPROVE`.
- **Blast Radius**: Stolen session tokens or compromised TOTP seeds enabling unauthorized club-wide approvals.
- **Defense Observed**:
  - `src/identity/policy.py` lines 820–826 enforce `if act in {PACKAGE_APPROVE, PACKAGE_REJECT}: if principal.auth_level != AuthLevel.STEP_UP_PASSKEY: raise StepUpRequiredError(...)`.
  - Fails closed with HTTP 403 `STEP_UP_REQUIRED`. Only `AuthLevel.STEP_UP_PASSKEY` permits package decision execution.
- **Verdict**: **DEFENDED (PASS)**

---

### [Critical] Challenge 5: Inactivity and Timeout Invariants (G-TIMEOUT)

- **Assumption Challenged**: Can a timeout event, inactivity period, or unread notification trigger an automated approval decision?
- **Attack Scenario**:
  - Pass action strings starting with `"system:timeout"`, `"system:timeout_approval"`, or `"timeout"`.
  - Pass resource objects with attributes `attributes={"timeout_approved": True}` or `attributes={"inactivity_approved": True}`.
- **Blast Radius**: Unattended packages auto-approving upon deadline expiration, violating the core human decision mandate.
- **Defense Observed**:
  - `src/identity/policy.py` lines 631–643 intercept timeout action strings and attributes, raising `AgentImpersonationError` ("inactivity or timeouts never create authorization decisions (G-TIMEOUT invariant)").
- **Verdict**: **DEFENDED (PASS)**

---

### [Medium] Challenge 6: Human Delegation Boundary & Scope Constraints

- **Assumption Challenged**: Can a Commissioner Delegate act without delegation context, with an expired or revoked delegation, with a future delegation, cross-club, out of declared action scope, or recursively create delegations?
- **Attack Scenario**:
  - Delegate attempts package decision with `delegation=None`.
  - Delegate attempts decision with `expires_at < now` (expired).
  - Delegate attempts decision with `revoked_at` populated (revoked).
  - Delegate attempts decision with `starts_at > now` (future-dated).
  - Delegate attempts decision with mismatched `delegate_id` or mismatched `club_id`.
  - Delegate with `scope="player_specific"` attempts to approve a club-wide package (`scope="club"`).
  - Delegate attempts recursive delegation creation (`DELEGATION_CREATE`).
- **Blast Radius**: Unauthorized deputies acting beyond Commissioner intent or extending their own power.
- **Defense Observed**:
  - `src/identity/policy.py` lines 700–753 and `store.py` lines 1423–1469 evaluate all delegation validity criteria.
  - Missing, expired, revoked, future, mismatched, or out-of-scope delegations fail closed with `DelegationInvalidError`.
  - Recursive delegation fails closed with `RoleNotPermittedError`.
- **Verdict**: **DEFENDED (PASS)**

---

### [High] Challenge 7: Service Credential Separation & Gamertag Isolation

- **Assumption Challenged**: Can service tokens be shared across services? Can mutable presentation gamertags be used as authorization keys?
- **Attack Scenario**:
  - Configure `BFF_SERVICE_TOKEN` identical to `GRAPH_SERVICE_TOKEN`.
  - Configure `WORKER_SERVICE_TOKEN` identical to `GRAPH_SERVICE_TOKEN`.
  - Call `assert_authorization_key_is_immutable()` with mutable gamertag strings (`"Gamertag_Pro_Striker"`).
- **Blast Radius**: Compromise of one service compromising all services; gamertag changes breaking or hijacking authorization bindings.
- **Defense Observed**:
  - `src/identity/services.py` lines 109–112 enforce distinct token values at load time, raising `ConfigError` ("shared service credentials forbidden").
  - `assert_authorization_key_is_immutable()` strictly validates UUID or numeric EA ID format `^[0-9]{1,20}$`, rejecting presentation strings.
- **Verdict**: **DEFENDED (PASS)**

---

### [Medium] Challenge 8: Database Migration Safety & Grants Isolation

- **Assumption Challenged**: Did migration `0005_delegations.sql` break referential integrity with `approval_audit.delegation_id`? Did `grants.sql` grant privileges to `fc27_dispatch`?
- **Attack Scenario**:
  - Inspect `migrations/0005_delegations.sql` for table drops or column type mutations.
  - Inspect `migrations/post/grants.sql` for grants to `fc27_dispatch` on delegations.
  - Verify `src/db_migrate.py` `GRANTS_POLICY` version synchronization.
- **Blast Radius**: Database rollback failures; dispatch role accessing pre-dispatch authorization data.
- **Defense Observed**:
  - `0005_delegations.sql` uses `ALTER TABLE public.authority_delegations` (non-destructive extension) and creates `public.fc27_delegations` view, preserving foreign key referential integrity.
  - `grants.sql` (version 7) grants privileges only to `fc27_app`; `fc27_dispatch` has 0 grants on delegations.
  - `db_migrate.py` tracks `version=7` with live-calculated SHA256 digest.
- **Verdict**: **DEFENDED (PASS)**

---

## 3. Stress Test Results Matrix

| Track | Attack Scenario | Expected Behavior | Actual Behavior | Result |
|---|---|---|---|---|
| 1. Tenancy | Cross-club access (all 8 roles) | HTTP 403 `CrossClubAccessError` | Raises `CrossClubAccessError` | **PASS** |
| 1. Tenancy | Missing/blank `club_id` | HTTP 403 `CrossClubAccessError` | Raises `CrossClubAccessError` | **PASS** |
| 1. Tenancy | Whitespace-padded `club_id` | HTTP 403 `CrossClubAccessError` | Raises `CrossClubAccessError` | **PASS** |
| 2. Isolation | Player viewing other player's assignment | HTTP 403 `PlayerIsolationError` | Raises `PlayerIsolationError` | **PASS** |
| 2. Isolation | Player viewing other player's profile/passkey | HTTP 403 `PlayerIsolationError` | Raises `PlayerIsolationError` | **PASS** |
| 2. Isolation | Player viewing other player's session | HTTP 403 `PlayerIsolationError` | Raises `PlayerIsolationError` | **PASS** |
| 2. Isolation | Player consent for other player | HTTP 403 `PlayerIsolationError` | Raises `PlayerIsolationError` | **PASS** |
| 2. Isolation | Player fallback on other player | HTTP 403 `PlayerIsolationError` | Raises `PlayerIsolationError` | **PASS** |
| 2. Isolation | Player fallback on club/mixed scope | HTTP 403 `PlayerIsolationError` | Raises `PlayerIsolationError` | **PASS** |
| 2. Isolation | Player fallback on 14 prohibited actions | HTTP 403 `RoleNotPermittedError` | Raises `RoleNotPermittedError` | **PASS** |
| 2. Isolation | Player approving club-wide package | HTTP 403 `RoleNotPermittedError` | Raises `RoleNotPermittedError` | **PASS** |
| 2. Isolation | Unverified player ID (`None`) access | HTTP 403 `PlayerIsolationError` | Raises `PlayerIsolationError` | **PASS** |
| 3. Anti-Impersonation | BFF Service executing human decisions (12 actions) | HTTP 403 `AgentImpersonationError` | Raises `AgentImpersonationError` | **PASS** |
| 3. Anti-Impersonation | Graph Service executing human decisions (12 actions) | HTTP 403 `AgentImpersonationError` | Raises `AgentImpersonationError` | **PASS** |
| 3. Anti-Impersonation | Forged service claiming commissioner role | HTTP 403 `AgentImpersonationError` | Raises `AgentImpersonationError` | **PASS** |
| 3. Anti-Impersonation | `require_human_principal` rejecting services | HTTP 403 `AgentImpersonationError` | Raises `AgentImpersonationError` | **PASS** |
| 4. Step-Up | Commissioner package approve with `authenticated` | HTTP 403 `StepUpRequiredError` | Raises `StepUpRequiredError` | **PASS** |
| 4. Step-Up | Commissioner package approve with `mfa_totp` | HTTP 403 `StepUpRequiredError` | Raises `StepUpRequiredError` | **PASS** |
| 4. Step-Up | Commissioner package reject without step-up | HTTP 403 `StepUpRequiredError` | Raises `StepUpRequiredError` | **PASS** |
| 4. Step-Up | Delegate package approve without step-up | HTTP 403 `StepUpRequiredError` | Raises `StepUpRequiredError` | **PASS** |
| 4. Step-Up | Commissioner package approve with `step_up_passkey` | Allowed (`DecisionResult.allowed=True`) | Returns `DecisionResult.allowed=True` | **PASS** |
| 5. G-TIMEOUT | Action string `"system:timeout"` | HTTP 403 `AgentImpersonationError` | Raises `AgentImpersonationError` | **PASS** |
| 5. G-TIMEOUT | Resource attribute `timeout_approved: True` | HTTP 403 `AgentImpersonationError` | Raises `AgentImpersonationError` | **PASS** |
| 6. Delegation | Missing delegation context | HTTP 403 `DelegationInvalidError` | Raises `DelegationInvalidError` | **PASS** |
| 6. Delegation | Future delegation (`starts_at > now`) | HTTP 403 `DelegationInvalidError` | Raises `DelegationInvalidError` | **PASS** |
| 6. Delegation | Expired delegation (`expires_at < now`) | HTTP 403 `DelegationInvalidError` | Raises `DelegationInvalidError` | **PASS** |
| 6. Delegation | Revoked delegation (`revoked_at` set) | HTTP 403 `DelegationInvalidError` | Raises `DelegationInvalidError` | **PASS** |
| 6. Delegation | Cross-club delegation mismatch | HTTP 403 `DelegationInvalidError` | Raises `DelegationInvalidError` | **PASS** |
| 6. Delegation | Player-scoped delegation on club-wide package | HTTP 403 `DelegationInvalidError` | Raises `DelegationInvalidError` | **PASS** |
| 6. Delegation | Recursive delegation attempt | HTTP 403 `RoleNotPermittedError` | Raises `RoleNotPermittedError` | **PASS** |
| 7. Credentials | Shared token between BFF & Graph | Startup failure (`ConfigError`) | Raises `ConfigError` | **PASS** |
| 7. Credentials | Mutable gamertag as authorization key | Validation failure (`ValueError`) | Raises `ValueError` | **PASS** |
| 8. Migrations | Dispatch grants on delegations | Zero grants | Verified 0 grants | **PASS** |
| 8. Migrations | Grants policy version sync | Version 7 matching | Verified Version 7 | **PASS** |
| 8. Migrations | `0005_delegations.sql` FK preservation | No table drop, FK intact | Verified intact | **PASS** |

---

## 4. Unchallenged Areas

- **Live PostgreSQL Database Cluster Operations**: Live cluster execution of migrations and table row queries was tested offline via unit models, static AST inspection, and schema verification scripts due to absence of live database container in current subagent runner. Store unit methods were verified for query formulation and constraint definitions.
- **WebAuthn FIDO2 Cryptographic Ceremonies**: WebAuthn cryptographic attestation/assertion ceremony verification is scheduled for Phase 2 Milestone 4. Milestone 2 validates the resulting `AuthLevel.STEP_UP_PASSKEY` and `authentication_evidence_id` policy boundary enforcement.

---

## 5. Defense-in-Depth Observations

1. **`principal.player_id is not None` Explicit Check**:
   In `src/identity/policy.py` line 681:
   `player_matches = (res.player_id is not None and str(res.player_id) == str(principal.player_id))`
   While PostgreSQL constraints enforce that `player_id` is a numeric string (`^[0-9]{1,20}$`) and therefore can never be the literal string `"None"`, adding an explicit `and principal.player_id is not None` is recommended for future defense-in-depth hardening.
2. **Dispatch Dormancy Preserved**: Universal `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` are verified intact across all policy and credential modules. Zero delivery machinery is active.
