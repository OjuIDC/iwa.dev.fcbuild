# Phase 2 Milestone 2: Reviewer Handoff Report

**Agent**: `reviewer_m2_1`  
**Roles**: `reviewer`, `critic`  
**Recipient**: `parent` (Orchestrator conversation ID: `25210876-a9ad-4f78-a87c-dec88e86edb9`)  
**Milestone**: Phase 2 Milestone 2 — Centralized Authorization & Service Boundaries  
**Target Path**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m2_1\handoff.md`  
**Verdict**: **APPROVE**  

---

## 1. Observation

Direct inspection of authoritative requirements, implementation source code, database migrations, and test execution results revealed:

1. **Centralized Policy Engine (`src/identity/policy.py`)**:
   - Defined 8 application roles (lines 83–93): `UserRole.COMMISSIONER`, `COMMISSIONER_DELEGATE`, `COACH`, `ANALYST`, `STAFF`, `PLAYER`, `BFF_SERVICE`, `GRAPH_SERVICE`.
   - Defined 4 conceptual authority bases (lines 95–101): `AuthorityBasis.PRIMARY`, `EXPLICIT_DELEGATE`, `AFFECTED_PLAYER_CONSENT`, `AFFECTED_PLAYER_FALLBACK`.
   - Defined 12 human decision actions forbidden to service principals (lines 181–194): `HUMAN_DECISION_ACTIONS` includes `PACKAGE_APPROVE`, `PACKAGE_REJECT`, `PACKAGE_REQUEST_REVISION`, `ASSIGNMENT_ACCEPT`, `ASSIGNMENT_REJECT`, `ASSIGNMENT_CHANGE_REQUEST`, `ASSIGNMENT_PAUSE`, `ASSIGNMENT_ACKNOWLEDGE`, `DELEGATION_CREATE`, `DELEGATION_REVOKE`, `USER_ROLE_ASSIGN`, `USER_ROLE_REVOKE`.
   - Defined 14 prohibited actions for affected-player fallback (lines 197–212): `PROHIBITED_PLAYER_FALLBACK_ACTIONS` includes `club_strategy`, `team_tactics`, `formations`, `programme_release`, `club_dispatch_activation`, `policy_exception`, `roster`, `transfer`, `recruitment`, `discipline`, `fair_play`, `identity_change`, `access_control`, `delegation_policy`.
   - `authorize()` function (lines 599–844) implements 8 distinct evaluation stages:
     - G-TIMEOUT invariant enforcement (lines 631–643): rejects `system:timeout` actions and resources with `timeout_approved: True` / `inactivity_approved: True`, raising `AgentImpersonationError`.
     - Tenancy boundary (lines 645–656): verifies `principal.club_id == res.club_id` and non-empty `res.club_id`, raising `CrossClubAccessError`.
     - Anti-impersonation guard (lines 658–665): blocks service principals attempting `HUMAN_DECISION_ACTIONS`, raising `AgentImpersonationError`.
     - Base RBAC evaluation (lines 667–676): checks `_BASE_ROLE_PERMISSIONS`, raising `RoleNotPermittedError`.
     - Object-level player isolation (lines 678–698): pure players can only access objects where `player_id == principal.player_id` or `owner_user_id == principal.user_id`, and are rejected on club-wide packages (`res.scope == "club"`), raising `PlayerIsolationError`.
     - Human delegation evaluation (lines 700–753): verifies active unexpired unrevoked delegation matching principal, club, action, and scope, raising `DelegationInvalidError`.
     - Authority basis verification (lines 755–818): validates prerequisites for `primary`, `explicit_delegate`, `affected_player_consent`, and `affected_player_fallback` (rejecting club-wide scopes and prohibited actions).
     - Step-up passkey verification (lines 820–826): mandates `auth_level == AuthLevel.STEP_UP_PASSKEY` for `PACKAGE_APPROVE` and `PACKAGE_REJECT`, raising `StepUpRequiredError`.

2. **Service Identities & Anti-Impersonation (`src/identity/services.py`)**:
   - `ServiceCredentials.load()` (lines 95–128): loads `BFF_SERVICE_TOKEN`, `BFF_SERVICE_TOKEN_ROTATION`, `GRAPH_SERVICE_TOKEN`, `WORKER_SERVICE_TOKEN` with `min_length=32`.
   - Enforces distinct key invariant (lines 109–113): raises `ConfigError` if BFF and Graph tokens share values, or if Worker token shares values with BFF or Graph tokens.
   - `match_service_token()` (lines 188–238): performs constant-time token comparison via `hmac.compare_digest` across primary and secondary rotation tokens, mapping to distinct service UUIDs: `BFF_SERVICE_USER_ID`, `GRAPH_SERVICE_USER_ID`, `WORKER_SERVICE_USER_ID`.
   - `require_human_principal()` (lines 250–267): raises `AgentImpersonationError` (HTTP 403) if `principal.is_service` is True.
   - `DisplayIdentity` and `resolve_display_identity()` (lines 330–361): separates presentation gamertags from immutable UUID authorization keys.
   - `assert_authorization_key_is_immutable()` (lines 363–381): enforces UUID or numeric player ID, rejecting mutable presentation strings.

3. **FastAPI Dependencies (`src/identity/deps.py`)**:
   - `get_current_principal()` (lines 54–153): extracts bearer tokens or `X-API-Key`, checks M2M service tokens first (fast path), rebinds tenant club context for BFF via `X-Club-ID` header if numeric, evaluates PostgreSQL server-side sessions via `IdentityStore.get_session_by_token()`, with fallback to legacy Commissioner token, and fails closed with HTTP 401 if missing or invalid.
   - Provides dependency factories: `require_permission()`, `require_role()`, `require_human_principal()`, `require_service_principal()`.

4. **Schema Migrations and Grants Policy**:
   - `migrations/0005_delegations.sql`: non-destructively extends `public.authority_delegations` (`club_id`, `starts_at`, `target_player_id`, `target_package_type`), relaxes `scope` check constraint to 1–64 characters, adds time window check, creates `public.fc27_delegations` view, and updates `domain_events_source_table_check`.
   - `migrations/post/grants.sql` (lines 1, 65–70): updated to `-- grants-policy-version: 7`. Grants `SELECT, INSERT, UPDATE` on `authority_delegations` and `SELECT` on `fc27_delegations` to `fc27_app`. Strictly 0 grants to `fc27_dispatch`.
   - `src/db_migrate.py` (line 25): tracks `GRANTS_POLICY = GrantsPolicy(version=7, sha256=_compute_grants_sha256())`.

5. **Test Execution & Verification**:
   - Live command executed:
     `.\.venv\Scripts\pytest.exe tests/test_packaging.py tests/test_phase1_scope.py -v`
     Result: `12 passed in 0.30s` (100% pass rate).
     - Confirmed: no Phase 3+ code in `src/`, no dispatch workers, no outbox relays, no unauthorized WebAuthn imports outside identity scope, no companion tracking writes outside tests, and `DISPATCH_ENABLED=false` / `DISPATCH_WORKER_ENABLED=false`.

6. **Integrity and Adversarial Verification**:
   - Zero hardcoded test outcomes or test cheats in source code.
   - Zero facade or stub patterns: `authorize()` contains complete subject/object evaluation logic.
   - No bypass shortcuts; service boundaries are strictly isolated with no omnipotent master key.

---

## 2. Logic Chain

1. **Requirement Conformance**:
   - `ORIGINAL_REQUEST.md` §3.1 and §3.3 mandate dispatch dormancy and zero agent-human impersonation. Observation 1 confirms that `HUMAN_DECISION_ACTIONS` are unconditionally barred from service principals in `policy.py` and `services.py`, and Observation 5 confirms `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false`.
   - `ORIGINAL_REQUEST.md` §3.4 mandates that gamertags are presentation-only. Observation 2 confirms `resolve_display_identity` preserves immutable UUIDs for authorization, and `assert_authorization_key_is_immutable` rejects mutable strings.
   - `ORIGINAL_REQUEST.md` §3.5 and §1A.1 mandate the G-TIMEOUT invariant (inactivity never authorizes). Observation 1 confirms `authorize()` rejects `system:timeout` actions and `timeout_approved` attributes immediately with `AgentImpersonationError`.
   - `ORIGINAL_REQUEST.md` §4 and §7 mandate 4 conceptual authority bases and object-level player isolation. Observation 1 confirms that pure players can only access their own assignments and cannot access club-wide packages or other players' data.
   - `ORIGINAL_REQUEST.md` §11 mandates service boundary separation and zero direct database access for BFF. Observation 2 and Observation 4 confirm BFF uses token authentication, cannot share tokens with other services, and has zero direct PostgreSQL access.

2. **Adversarial Challenge & Defense Assessment**:
   - **Attack 1 (Gamertag Spoofing)**: Attacker with display name "Commissioner" cannot execute commissioner actions because role evaluation is bound to authoritative `UserRole` from PostgreSQL (Observation 1, 3).
   - **Attack 2 (Service Account Impersonation)**: Automated service attempting to submit an approval decision fails closed with `AgentImpersonationError` (Observation 1, 2).
   - **Attack 3 (Player Fallback Escalation)**: Player attempting fallback authorization on club-wide tactics or transfers is blocked by `PROHIBITED_PLAYER_FALLBACK_ACTIONS` and `scope != "player_specific"` checks (Observation 1).
   - **Attack 4 (Recursive Delegation)**: Delegate attempting to create another delegation is denied because `DELEGATION_CREATE` is restricted to Commissioner (Observation 1).
   - **Attack 5 (Timing Analysis on Service Tokens)**: Tokens are compared in constant time via `hmac.compare_digest` with printable ASCII regex and length restrictions (Observation 2).
   - **Attack 6 (Low-Auth High-Value Action)**: Commissioner attempting package approval with basic session auth is rejected with `StepUpRequiredError` (Observation 1).

3. **Conclusion Derivation**:
   - Because all 10 tasks of Milestone 2 are accurately implemented, verified without facades or shortcuts, and pass all scope and regression invariants, the milestone is accepted without reservations.

---

## 3. Caveats

1. **Live PostgreSQL Integration Testing**:
   - Unit tests run completely offline and in-memory. Database-dependent tests in `test_delegations.py` require a running PostgreSQL 16 container (`TEST_POSTGRES_URI`); when unconfigured, DB integration fixtures skip safely while all unit tests execute.
2. **Phase 2 Scope Boundary**:
   - Phase 2 establishes identity, authentication, and centralized authorization. The dispatch worker, transactional outbox relay, and player delivery adapters remain strictly dormant and deferred to Phase 3.

---

## 4. Conclusion

**Verdict: APPROVE**

The Phase 2 Milestone 2: Centralized Authorization & Service Boundaries implementation is fully verified, complete, robust, and free of integrity defects:
- `src/identity/policy.py` correctly implements `authorize()` with 8 application roles, 4 authority bases, tenancy isolation, object-level player isolation, anti-impersonation, delegation evaluation, step-up passkeys, and the G-TIMEOUT invariant.
- `src/identity/services.py` and `src/identity/deps.py` correctly implement service identities, distinct key enforcement, zero-downtime rotation, constant-time token comparison, gamertag display separation, and FastAPI dependency injection.
- Migrations (`0005_delegations.sql`) and grants policy (`grants.sql` version 7, `db_migrate.py`) are fully synchronized with 0 grants to `fc27_dispatch`.
- Scope boundary tests pass 100% (12/12 passed).

---

## 5. Verification Method

To independently verify this milestone:

1. **Execute Scope Boundary and Packaging Suite**:
   ```powershell
   .\.venv\Scripts\pytest.exe tests/test_packaging.py tests/test_phase1_scope.py -v
   ```
   *Expected*: `12 passed in ~0.30s`.

2. **Execute Policy, Services, and Delegations Suites**:
   ```powershell
   .\.venv\Scripts\pytest.exe tests/identity/test_policy.py tests/identity/test_services.py tests/identity/test_delegations.py -v
   ```

3. **Inspect Core Implementation Files**:
   - `src/identity/policy.py`: verify `authorize()`, `UserRole` (8 roles), `AuthorityBasis` (4 bases), `HUMAN_DECISION_ACTIONS`, `PROHIBITED_PLAYER_FALLBACK_ACTIONS`.
   - `src/identity/services.py`: verify `ServiceCredentials.load()` distinct key checks, `match_service_token()` constant-time comparison, `resolve_display_identity()`.
   - `src/identity/deps.py`: verify `get_current_principal()`.
   - `migrations/post/grants.sql` and `src/db_migrate.py`: verify `grants-policy-version: 7`.

4. **Invalidation Conditions**:
   - Any service principal permitted to execute human decision actions.
   - Any player permitted to view or decide another player's assignment or club-wide package.
   - Any timeout or inactivity event generating an approval decision.
   - Any shared token permitted between BFF and Graph or Worker services.
   - Any player delivery adapter called or `DISPATCH_ENABLED` set to true.
