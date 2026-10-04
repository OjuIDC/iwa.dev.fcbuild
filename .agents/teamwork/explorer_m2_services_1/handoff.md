# Handoff Report: Service Identities & Security Boundaries (Phase 2 Milestone 2)

**Author**: `explorer_m2_services_1` (Read-only exploration agent)  
**Target Milestone**: Phase 2 Milestone 2 — Centralized Authorization & Service Boundaries  
**Target File**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_services_1\handoff.md`  

---

## 1. Observation

### 1.1 Authoritative Requirements & Non-Negotiable Invariants
From `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md`:
1. **§3.1 Strict Dormancy**:
   > "DISPATCH_ENABLED=false at all times. DISPATCH_WORKER_ENABLED=false at all times." (lines 1693–1707)
2. **§3.2 PostgreSQL Authority**:
   > "PostgreSQL remains authoritative for: users; roles; invitations; sessions; credentials; identity bindings; delegations; authentication evidence; action-token state; authoritative human decisions; audit/domain events. LangGraph checkpoints remain workflow state, not identity or authorization state." (lines 1708–1730)
3. **§3.3 Zero Agent-Human Impersonation**:
   > "No agent may authenticate or decide for a human. This includes: player agents; Coach agents; Commissioner-facing agents; model agents; LangGraph; MCP tools; background workers; BFF service; WebSocket service; Redis subscribers. Agents may assist humans. Agents cannot become the human actor." (lines 1731–1749)
4. **§3.4 Identity is Not Display Name (Gamertag Separation)**:
   > "Gamertags and display names are presentation data. Do not use a mutable display name as the internal authorization identity. Internally use immutable local user identifiers. Keep EA player identity evidence separate from local authentication identity. If stable EA numeric player identity is still unverified, preserve that state and do not invent, infer, or substitute a gamertag as the missing identifier." (lines 1750–1762)
5. **§3.5 Inactivity Never Authorizes**:
   > "No timeout, absence, inactivity period, unread notification, offline status, failed delivery, session expiry, lack of response, or escalation event may automatically create: approval; delegation; player acceptance; player acknowledgment; dispatch; activation. Escalation changes which authenticated human MAY become eligible to act. It does not create the decision." (lines 1763–1779)
6. **§11 Agent G: BFF and Internal Service Identity**:
   > "The BFF service identity must have: unique service identity; narrow API scope; separate credential; credential rotation path; audit identity; no authority to impersonate users; no authority to submit human decisions without authoritative human authentication evidence. The BFF must have no direct PostgreSQL access in the final architecture. Where Phase 2 introduces the identity now, maintain that trust boundary. Similarly maintain separate identities for: Graph API/service; BFF; future dispatch worker; operational tooling. Do not reuse one omnipotent shared service credential." (lines 2279–2310)
7. **§12 Agent H: Secrets and Cryptographic Material**:
   > "No secrets committed to Git; no secrets emitted through logs; no secrets returned by APIs; no secrets included in exception text; encrypted-at-rest storage for recoverable sensitive values; irreversible hashing for tokens/code values that do not need recovery; explicit key/secret versioning; rotation support; fail-closed startup when required key material is unavailable." (lines 2311–2328)

### 1.2 Existing Schema & Migration Observations
1. **`migrations/0004_identity_foundation.sql`**:
   - Lines 6–17: `fc27_users` table establishes `user_id uuid PRIMARY KEY DEFAULT gen_random_uuid()`, `club_id text NOT NULL`, `login_id text NOT NULL UNIQUE`, `display_name text NOT NULL`, `status text NOT NULL DEFAULT 'invited'`, and optional `player_id text REFERENCES public.players(player_id)`.
   - Lines 31–42: Database trigger `fc27_guard_user_status_transition()` strictly blocks mutations to `user_id`, `club_id`, and `created_at` (`RAISE check_violation USING MESSAGE = 'user_id is immutable'`).
   - Lines 76–91: `fc27_user_roles` defines allowed roles: `'commissioner'`, `'commissioner_delegate'`, `'coach'`, `'analyst'`, `'staff'`, `'player'`, `'bff_service'`, `'graph_service'`.
   - Lines 130–146: `fc27_sessions` defines `auth_method text NOT NULL CHECK (auth_method IN ('passkey', 'totp', 'service_token', 'recovery_code'))`.
2. **`migrations/post/grants.sql`**:
   - Lines 169–204: DML permissions (`SELECT, INSERT, UPDATE, DELETE`) on `fc27_users`, `fc27_sessions`, `fc27_user_roles`, `fc27_challenges`, etc. are granted strictly to database role `fc27_app`.
   - Lines 38, 46, 87, 93, 102: Selected read/write grants on outbox and dispatches are granted to `fc27_dispatch`.
   - **Critical Observation**: The `BFF` service has **ZERO database roles and ZERO grants** in PostgreSQL. The trust boundary is absolute at the database engine level.

### 1.3 Existing Codebase State
1. **`src/security.py`**:
   - Lines 1–79: Phase 1 legacy implementation only supported two roles: `Role.service` (static token) and `Role.commissioner` (SHA-256 hashed token). It lacks the 8 Phase 2 roles, has no concept of `bff_service`, `graph_service`, or worker boundaries, and has no session integration.
2. **`src/graph_api.py`**:
   - Lines 683–715: Mounts routes with `_p: Principal = Depends(service)`.
   - Lines 869–897: Mounts decision routes with `p: Principal = Depends(commissioner)` and `SignedDecision` HMAC payload.
   - Lines 35–43: `ApiSettings` only loads `GRAPH_SERVICE_TOKEN` and `GRAPH_COMMISSIONER_TOKEN_SHA256`. It lacks `BFF_SERVICE_TOKEN` and worker token isolation.
3. **`src/identity/store.py`**:
   - Lines 470–560: `create_session()`, `get_session_by_token()`, `rotate_session()`, and `revoke_session()` are fully implemented and verified.
4. **Peer Explorer Artifacts**:
   - `explorer_m2_policy_1/proposed_policy.py`: Establishes `PrincipalContext`, `UserRole`, `ActionType`, `HUMAN_DECISION_ACTIONS` (12 actions strictly forbidden for service principals), and `authorize()` engine with `AgentImpersonationError` (403), `CrossClubAccessError` (403), `PlayerIsolationError` (403).
   - `explorer_m2_delegation_1/handoff.md`: Establishes human delegation lifecycle, explicit deputy scopes, and confirms timeouts never auto-approve.

---

## 2. Logic Chain

### 2.1 Service Identities Architecture
1. **Observation 1.1 §11 & Observation 1.2**: BFF, Graph runtime, and future workers must have discrete credentials and distinct scopes, and BFF must have zero direct database access.
2. **Deduction**:
   - `bff_service`: An external HTTPS API caller. It forwards end-user requests from Web/Mobile frontends to the Graph API and Auth endpoints. It holds an opaque bearer credential (`BFF_SERVICE_TOKEN`). It has no PostgreSQL connection string, no database user, and no direct DB access. It is permitted only read projection actions (`package:view`, `assignment:view`, `run:view`, `roster:view`, etc.).
   - `graph_service`: The internal runtime caller executing graph workflows. It holds `GRAPH_SERVICE_TOKEN`. It communicates with the database using the least-privileged `fc27_app` connection pool. It is permitted workflow operations (`run:create`, `run:view`, `run:resume`).
   - `worker`: Future background dispatch worker. In Phase 2, `DISPATCH_WORKER_ENABLED=false` is enforced. Its future credential (`WORKER_SERVICE_TOKEN`) must be isolated.
   - **Shared Key Prohibition**: Server startup must fail closed if `WORKER_SERVICE_TOKEN`, `BFF_SERVICE_TOKEN`, or `GRAPH_SERVICE_TOKEN` share identical values, preventing the "omnipotent master key" anti-pattern.
   - **Rotation Support**: Supports dual active keys (`BFF_SERVICE_TOKEN` and `BFF_SERVICE_TOKEN_ROTATION`) evaluated in constant-time via `hmac.compare_digest` to allow seamless zero-downtime secret rollover.

### 2.2 Anti-Impersonation Boundary Enforcement
1. **Observation 1.1 §3.3 & Observation 1.3 (peer policy)**: Services, workers, and agents cannot authenticate or decide for humans.
2. **Deduction**:
   - **Credential-Bound Principal**: Authentication derives the caller principal strictly from the validated cryptographic token (`Authorization: Bearer` or `X-API-Key`). Any request body field attempting to claim `user_id` or `actor` is disregarded for identity resolution.
   - **Service Flagging**: If authenticated via a service token, `principal.is_service = True`.
   - **Policy Engine Guard**: In `policy.authorize()`, if `principal.is_service` and the requested action is in `HUMAN_DECISION_ACTIONS` (such as `package:approve`, `package:reject`, `assignment:accept`, `delegation:create`), the engine raises `AgentImpersonationError(403)`.
   - **Cryptographic Evidence Gate**: High-value human decisions require hardware-bound WebAuthn assertion evidence (`authentication_evidence_id` referencing a single-use verified challenge bound to `package_id`, `version`, `payload_hash`, `decision`, and `principal.user_id`). Services and agents do not hold the user's private key inside hardware authenticators, making forgery mathematically infeasible.

### 2.3 Gamertag Display Separation
1. **Observation 1.1 §3.4 & Observation 1.2 (schema triggers)**: Gamertags and display names are presentation data only; authorization keys strictly on immutable UUIDs.
2. **Deduction**:
   - `fc27_users.user_id` is an immutable UUID guarded by database triggers.
   - `fc27_users.display_name` is mutable presentation text.
   - Authorization evaluation in `policy.authorize()` and tenancy queries in `store.py` key exclusively on `principal.user_id` (UUID) and `principal.player_id` (numeric regex `^[0-9]{1,20}$`).
   - Any attempt to match permissions or tenancy using `display_name` is an architectural defect. If an attacker names their account `"Commissioner"`, authorization only checks `principal.roles`, ignoring `display_name`.
   - If a player's numeric EA `player_id` is null/unverified, the system leaves it null. The system never infers or substitutes a gamertag as a player identifier.

### 2.4 Integration with Graph API (`src/graph_api.py`)
1. **Observation 1.3 (`src/graph_api.py`)**: Current routes depend on legacy `Principal` from `src/security.py`.
2. **Deduction**:
   - Replace legacy dependency with `get_current_principal` returning `PrincipalContext`.
   - Fast-path constant-time service token matching for M2M calls.
   - PostgreSQL session cache/store resolution for human callers.
   - In decision endpoints (`/v1/runs/{thread_id}/decision` and `/v1/approval-requests/{id}/decisions`):
     1. Inject `principal: PrincipalContext = Depends(get_current_principal)`.
     2. Construct `Resource(resource_type="package", club_id=req_row["club_id"], resource_id=package_id, ...)`.
     3. Evaluate `policy.authorize(principal, ActionType.PACKAGE_APPROVE, resource, authority_basis=body.authority_basis)`.
     4. Service callers fail immediately with 403 `AgentImpersonationError`.
     5. For human callers, verify `authentication_evidence_id` against `fc27_challenges`.
     6. Commit decision to `approval_decisions` and emit domain event to `domain_events`.

---

## 3. Caveats

1. **Phase 2 Boundary & Dispatch Dormancy**:
   - While service identities are established for BFF, Graph, and Worker, the worker remains dormant (`DISPATCH_WORKER_ENABLED=false`).
   - Under no circumstances does any service identity invoke delivery adapters or external dispatch pipes.
2. **BFF Client Implementation Deferred to Phase 4**:
   - This milestone defines the *identity, credential, scope, and trust boundary* of `bff_service`. The actual BFF dashboard UI/gateway code is implemented in Phase 4.
3. **Session Store Latency**:
   - Evaluating human sessions on every request hits PostgreSQL `fc27_sessions` (with `touch=True`). In high-throughput environments, this can be optimized with Redis caching in later phases; however, for Phase 2, PostgreSQL is the single authoritative store.
4. **Legacy HMAC Deprecation**:
   - The CLI HMAC proof mechanism remains supported in Phase 2 only when `APPROVAL_CLI_HMAC_ENABLED=true` for backward compatibility with Phase 1 test fixtures. In production, `APPROVAL_CLI_HMAC_ENABLED=false` enforces WebAuthn passkeys exclusively.

---

## 4. Conclusion

The service identities, anti-impersonation boundaries, gamertag separation, and Graph API integration for Phase 2 Milestone 2 are completely designed, specified, and backed by reference code and tests:
1. **Delivered Reference Implementation**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_services_1\proposed_services.py` containing:
   - `ServiceCredentials` (fail-closed loading, rotation support, distinct key invariant).
   - `get_current_principal` (FastAPI dependency supporting Bearer/X-API-Key, service tokens, and server-side sessions).
   - `require_human_principal` & `require_service_principal` boundary guards.
   - `AntiImpersonationGuard` (action-level decision prohibition for automated services).
   - `resolve_display_identity` & `assert_authorization_key_is_immutable` (gamertag display isolation).
2. **Delivered Architectural Analysis**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_services_1\service_identities_analysis.md` with complete ASCII topology, flow diagrams, and permission matrices.
3. **Delivered Test Suite Blueprint**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_services_1\proposed_test_services.py` providing 15 concrete unit, component, and security tests.

---

## 5. Verification Method

### 5.1 Verification Commands
To verify the implementation once applied by worker agents:

1. **Verify Unit & Component Tests for Services & Boundaries**:
   ```powershell
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"
   .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/identity/test_services.py tests/identity/test_policy.py
   ```
   **Expected Result**: All tests pass.

2. **Verify Phase 2 Feature Suite (Features 34, 35, 36 & Anti-Impersonation)**:
   ```powershell
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"
   .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/e2e/test_phase2_tier1_features.py -k "test_f34 or test_f35 or test_f36 or test_f04 or test_f05"
   ```
   **Expected Result**: Features 34, 35, 36 pass with HTTP 200 for authorized routes and HTTP 403 for cross-boundary decision attempts.

3. **Verify Baseline Regression Gate**:
   ```powershell
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"
   .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_approval_lifecycle.py tests/test_approval_security.py tests/test_db_roles.py tests/test_db_migrate_roles.py
   ```
   **Expected Result**: 87 core Phase 1 tests pass without regression.

### 5.2 Files to Inspect
- `src/identity/services.py` (or integrated `src/identity/auth.py`): verify `load_service_credentials()`, `get_current_principal()`, and `require_human_principal()`.
- `src/identity/policy.py`: verify `UserRole.BFF_SERVICE`, `UserRole.GRAPH_SERVICE`, `HUMAN_DECISION_ACTIONS`, and `AgentImpersonationError`.
- `src/graph_api.py`: verify decision route dependency injection uses `get_current_principal` and evaluates `policy.authorize()`.
- `migrations/post/grants.sql`: verify zero grants exist for BFF service.

### 5.3 Invalidation Conditions
The design is invalidated if:
1. `BFF_SERVICE_TOKEN` and `GRAPH_SERVICE_TOKEN` are configured to share the same value without raising a startup error.
2. A service principal (`bff_service`, `graph_service`, or worker) is able to successfully invoke `/approval/packages/{id}/decide`, `/v1/runs/{id}/decision`, or `/v1/player-assignments/{id}/decision` without raising HTTP 403 `AgentImpersonationError`.
3. An authorization check or database query uses `display_name` or `gamertag` instead of `user_id` or `player_id`.
4. PostgreSQL grants are added allowing direct connection or DML to a BFF database user.
5. Inactivity, timer expiry, or request timeout writes an approval decision.
6. Any test execution enables `DISPATCH_ENABLED=true` or performs delivery to a player.
