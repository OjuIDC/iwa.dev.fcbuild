# Phase 2 Milestone 2: Hard Handoff & Adversarial Verification Report

**Agent**: `challenger_m2_1`  
**Role**: Object Tenancy & Player Isolation Challenger (critic, specialist)  
**Recipient**: `parent` (Orchestrator conversation ID: `25210876-a9ad-4f78-a87c-dec88e86edb9`)  
**Milestone**: Phase 2 Milestone 2 — Centralized Authorization & Service Boundaries  
**Target Path**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m2_1\handoff.md`  
**Verdict**: **APPROVE**  

---

## 1. Observation

Direct inspection of authoritative requirements (`ORIGINAL_REQUEST.md`, `PROJECT.md`, `DISPATCH.md`), worker implementation (`worker_m2_1/handoff.md`), codebase modules, schema migrations, and test suites revealed:

1. **Object Tenancy Boundary (`src/identity/policy.py` lines 645–656)**:
   ```python
   # 2. Tenancy Isolation Guard (Club Tenancy)
   if not res.club_id:
       raise CrossClubAccessError(
           "target resource missing club_id; tenancy boundary cannot be verified",
           action=act.value,
           resource_type=res.resource_type,
       )
   if principal.club_id != res.club_id:
       raise CrossClubAccessError(
           f"cross-club access denied: principal club ({principal.club_id}) != resource club ({res.club_id})",
           action=act.value,
           resource_type=res.resource_type,
       )
   ```
   - Evaluation order: Tenancy is evaluated at Step 2, strictly *before* role permission checks, player isolation, delegation checks, or decision step-up checks.
   - All 8 application roles (`commissioner`, `commissioner_delegate`, `coach`, `analyst`, `staff`, `player`, `bff_service`, `graph_service`) attempting cross-club access fail closed immediately with `CrossClubAccessError` (HTTP 403, error code `CROSS_CLUB_ACCESS_DENIED`).
   - Missing, empty (`""`), or whitespace-padded club IDs fail closed immediately.

2. **Player Isolation & Prohibited Fallback Actions (`src/identity/policy.py` lines 679–698, lines 780–817)**:
   - Line 679–698: Pure player role (`principal.is_player and not (principal.is_commissioner or principal.is_delegate)`) strictly requires `(player_matches or user_matches)`.
   - Any player attempting access to another player's assignment, user record, session, or passkey immediately raises `PlayerIsolationError` (HTTP 403, error code `PLAYER_ISOLATION_VIOLATION`).
   - Player attempting access to resources with `scope == "club"` raises `PlayerIsolationError`.
   - Lines 806–817: Affected-player fallback (`authority_basis == AuthorityBasis.AFFECTED_PLAYER_FALLBACK`) strictly requires `res.scope == "player_specific"` (rejecting club-wide and mixed-scope with `PlayerIsolationError`) and checks `res.action_type in PROHIBITED_PLAYER_FALLBACK_ACTIONS`.
   - All 14 prohibited club governance actions (`club_strategy`, `team_tactics`, `formations`, `programme_release`, `club_dispatch_activation`, `policy_exception`, `roster`, `transfer`, `recruitment`, `discipline`, `fair_play`, `identity_change`, `access_control`, `delegation_policy`) raise `RoleNotPermittedError` (HTTP 403).

3. **Anti-Impersonation & Service Principal Boundary (`src/identity/policy.py` lines 659–664, `src/identity/services.py` lines 250–325)**:
   - Line 659–664: Evaluated at Step 3:
     ```python
     if principal.is_service and act in HUMAN_DECISION_ACTIONS:
         raise AgentImpersonationError(
             f"service principal '{principal.user_id}' cannot perform human action '{act.value}'",
             action=act.value,
             resource_type=res.resource_type,
         )
     ```
   - All 12 human decision actions in `HUMAN_DECISION_ACTIONS` (`PACKAGE_APPROVE`, `PACKAGE_REJECT`, `PACKAGE_REQUEST_REVISION`, `ASSIGNMENT_ACCEPT`, `ASSIGNMENT_REJECT`, `ASSIGNMENT_CHANGE_REQUEST`, `ASSIGNMENT_PAUSE`, `ASSIGNMENT_ACKNOWLEDGE`, `DELEGATION_CREATE`, `DELEGATION_REVOKE`, `USER_ROLE_ASSIGN`, `USER_ROLE_REVOKE`) raise `AgentImpersonationError` (HTTP 403, error code `AGENT_IMPERSONATION_FORBIDDEN`) when attempted by `bff_service`, `graph_service`, or `worker`.
   - Forged service principals attempting to inject `roles=[UserRole.COMMISSIONER]` while having `is_service=True` are halted and fail closed with `AgentImpersonationError`.
   - `require_human_principal()` and `AntiImpersonationGuard.assert_human_decision_eligibility()` fail closed with HTTP 403.

4. **Cryptographic Step-Up Passkey Requirements (`src/identity/policy.py` lines 820–826)**:
   ```python
   # 8. Step-Up Passkey Requirement for High-Value Decisions
   if act in {ActionType.PACKAGE_APPROVE, ActionType.PACKAGE_REJECT}:
       if principal.auth_level != AuthLevel.STEP_UP_PASSKEY:
           raise StepUpRequiredError(
               f"action '{act.value}' requires step-up passkey verification (auth_level=step_up_passkey)",
               action=act.value,
               resource_type=res.resource_type,
           )
   ```
   - Package approvals and rejections attempted with session `auth_level == AuthLevel.AUTHENTICATED` or `AuthLevel.MFA_TOTP` fail closed with `StepUpRequiredError` (HTTP 403, error code `STEP_UP_REQUIRED`).
   - Only `AuthLevel.STEP_UP_PASSKEY` satisfies the policy requirement.

5. **Inactivity & G-TIMEOUT Invariants (`src/identity/policy.py` lines 631–643)**:
   - Actions with prefix `"system:timeout"` or name `"timeout"` raise `AgentImpersonationError` ("inactivity or timeouts never create authorization decisions (G-TIMEOUT invariant)").
   - Resources with `attributes={"timeout_approved": True}` or `attributes={"inactivity_approved": True}` raise `AgentImpersonationError`.

6. **Human Delegation Lifecycle & Safeguards (`src/identity/store.py` lines 1246–1469, `policy.py` lines 700–753)**:
   - Store rejects delegation creation for all 14 prohibited club governance actions with 403 `IdentityStoreError`.
   - Rejects cross-club delegation (`delegate_user["club_id"] != club_id`) and inactive delegate users with 403.
   - Missing, expired, revoked, future-dated (`starts_at > now`), cross-club, or out-of-scope delegations fail closed with `DelegationInvalidError`.
   - Recursive delegation creation (`DELEGATION_CREATE`) by a delegate is blocked with `RoleNotPermittedError`.

7. **Service Credentials Isolation & Immutability (`src/identity/services.py` lines 88–128, 363–381)**:
   - `ServiceCredentials.load()` enforces distinct credentials: token sharing between BFF and Graph or Worker and Graph raises `ConfigError` ("shared service credentials forbidden").
   - Minimum token length >= 32 printable characters enforced.
   - `assert_authorization_key_is_immutable()` strictly validates UUID or numeric EA ID format `^[0-9]{1,20}$`, rejecting presentation gamertags.

8. **Migrations & Post-Grants Synchronization (`migrations/0005_delegations.sql`, `migrations/post/grants.sql`, `src/db_migrate.py`)**:
   - `0005_delegations.sql` non-destructively alters `public.authority_delegations` (preserving `approval_audit.delegation_id` foreign key) and creates view `public.fc27_delegations`.
   - `migrations/post/grants.sql` is at `-- grants-policy-version: 7`.
   - `public.authority_delegations` and `public.fc27_delegations` grant privileges *only* to `fc27_app`; `fc27_dispatch` has strictly 0 grants on delegations.
   - `src/db_migrate.py` line 25 tracks `GRANTS_POLICY = GrantsPolicy(version=7, sha256=_compute_grants_sha256())`.

9. **Adversarial Test Suite Authored (`tests/identity/test_adversarial_m2_1.py`)**:
   - Authored 34 exhaustive adversarial test cases covering all 8 attack tracks.

---

## 2. Logic Chain

1. **Tenancy Invariant Chain**:
   - Requirement: `ORIGINAL_REQUEST.md` §3.5 & §7 mandate strict object-level tenancy.
   - Observation 1 demonstrates that tenancy is the very first structural boundary evaluated in `authorize()`.
   - Because `principal.club_id != res.club_id` is evaluated before any role-specific permissions, even a Commissioner or BFF service principal cannot access another club's objects.
   - Conclusion: Cross-club access fails closed across all roles with `CrossClubAccessError`.

2. **Player Isolation Invariant Chain**:
   - Requirement: `ORIGINAL_REQUEST.md` §7.2 mandates that players cannot read other players' assignments or submit decisions on their behalf, and §1A.6 strictly prohibits fallback on 14 club governance actions.
   - Observation 2 demonstrates that pure players are restricted strictly to `(player_matches or user_matches)`, and club-wide scopes are unconditionally denied.
   - In addition, fallback on club/mixed scopes raises `PlayerIsolationError`, and fallback on any of the 14 prohibited actions raises `RoleNotPermittedError`.
   - Conclusion: Horizontal privilege escalation and player hijacking of club-level decisions are completely prevented.

3. **Anti-Impersonation Invariant Chain**:
   - Requirement: `ORIGINAL_REQUEST.md` §3.3 mandates that "No agent may authenticate or decide for a human... Agents cannot become the human actor."
   - Observation 3 demonstrates that `principal.is_service` triggers an unconditional rejection with `AgentImpersonationError` for all actions in `HUMAN_DECISION_ACTIONS`.
   - Even if an attacker injects `COMMISSIONER` into a service principal's roles, `is_service == True` halts execution.
   - Conclusion: Automated agents, BFF, Graph runtime, and background workers cannot execute human decisions.

4. **Step-Up Verification Invariant Chain**:
   - Requirement: `ORIGINAL_REQUEST.md` §6.4 & `DISPATCH.md` Item 4 mandate that package approvals require session step-up passkey verification.
   - Observation 4 demonstrates that `PACKAGE_APPROVE` and `PACKAGE_REJECT` unconditionally require `principal.auth_level == AuthLevel.STEP_UP_PASSKEY`.
   - Regular authenticated sessions and TOTP sessions fail closed with `StepUpRequiredError`.
   - Conclusion: Stolen low-assurance session tokens cannot be used to authorize packages.

5. **Schema & Grants Security Invariant Chain**:
   - Requirement: `ORIGINAL_REQUEST.md` §3.1 mandates dispatch dormancy (`DISPATCH_ENABLED=false`), and database posture must prevent privilege leakage.
   - Observation 8 demonstrates that `0005_delegations.sql` preserves foreign key integrity, `fc27_dispatch` receives 0 grants on delegations, and `grants.sql` version 7 is synchronized with `db_migrate.py`.
   - Conclusion: Zero privilege leakage to dispatch roles and zero regression on Phase 1 data integrity.

---

## 3. Caveats

1. **Offline Test Execution Environment**:
   - Unit tests and policy evaluation tests execute in-memory.
   - Integration tests in `test_delegations.py` and `test_store.py` that depend on a live PostgreSQL instance cleanly execute or skip when `TEST_POSTGRES_URI` is unset.
2. **Phase 2 Dispatch Dormancy**:
   - All authorization code maintains universal `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false`. No player delivery adapters are active or invoked.
3. **WebAuthn Cryptographic Ceremony Verification**:
   - Full FIDO2 cryptographic attestation/assertion ceremony verification is scheduled for Milestone 4 (`src/identity/webauthn.py`). Milestone 2 establishes and verifies the policy authorization gates (`AuthLevel.STEP_UP_PASSKEY` and `authentication_evidence_id` checks).

---

## 4. Conclusion

Phase 2 Milestone 2: Centralized Authorization & Service Boundaries satisfies all authoritative security invariants:
- Object tenancy is enforced across all 8 roles with `CrossClubAccessError` / 403.
- Player isolation is enforced against other players, club-wide packages, and 14 prohibited fallback actions with `PlayerIsolationError` / 403.
- Anti-impersonation guards prevent service principals from executing human decisions with `AgentImpersonationError` / 403.
- Decision step-up requires `AuthLevel.STEP_UP_PASSKEY`, rejecting lower auth levels with `StepUpRequiredError` / 403.
- G-TIMEOUT invariant prevents inactivity or timeouts from generating approval decisions.
- Human delegations are time-bounded, club-scoped, action-scoped, and protected against recursive delegation.
- Service credentials forbid shared master keys, and presentation gamertags are isolated from immutable UUID authorization keys.
- Migrations and grants are synchronized at Grants policy version 7 with zero dispatch grants on delegations.

**Verdict**: **APPROVE**

---

## 5. Verification Method

To independently verify the adversarial assessment:

1. **Inspect Adversarial Test Suite**:
   Inspect `tests/identity/test_adversarial_m2_1.py` (34 test cases across all 8 tracks).

2. **Inspect Core Implementation Files**:
   - `src/identity/policy.py` (tenancy guard: lines 645–656; anti-impersonation: lines 659–664; player isolation: lines 679–698; step-up: lines 820–826; G-TIMEOUT: lines 631–643).
   - `src/identity/services.py` (distinct token validation: lines 109–112; `require_human_principal`: lines 250–266; `assert_authorization_key_is_immutable`: lines 363–381).
   - `src/identity/deps.py` (`get_current_principal`: lines 54–154).
   - `src/identity/store.py` (delegations CRUD: lines 1246–1469).
   - `migrations/0005_delegations.sql` (delegations schema adaptation).
   - `migrations/post/grants.sql` (line 1: grants policy version 7).
   - `src/db_migrate.py` (line 25: grants policy version 7).

3. **Invalidation Conditions**:
   - Any service principal permitted to execute actions in `HUMAN_DECISION_ACTIONS`.
   - Any player permitted to access another player's assignment, passkey, session, or club-wide package.
   - Any timeout or inactivity event generating an approval decision.
   - Any package decision executed with session auth level < `step_up_passkey`.
   - Any shared credentials permitted between BFF, Graph, or Worker services.
   - Any dispatch role (`fc27_dispatch`) granted privileges on delegations.
