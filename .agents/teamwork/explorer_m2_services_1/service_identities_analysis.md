# Service Identities & Security Boundaries Analysis (Phase 2 Milestone 2)

**Author**: `explorer_m2_services_1`  
**Milestone**: Phase 2 Milestone 2 — Centralized Authorization & Service Boundaries  
**Target Module**: `src/identity/services.py`, `src/identity/auth.py`, `src/graph_api.py`  

---

## 1. Executive Summary

This document establishes the comprehensive architectural and technical specification for **Service Identities, Anti-Impersonation Boundaries, Gamertag Separation, and Graph API Integration** in Phase 2 Milestone 2 of the FC27 Player Dispatch Programme.

### Core Guarantees & Non-Negotiable Invariants:
1. **PostgreSQL Authority**: All users, sessions, roles, delegations, and decisions reside authoritatively in PostgreSQL. LangGraph checkpoints are workflow execution state only and never identity or authorization state (§3.2).
2. **Zero Agent-Human Impersonation**: Services (BFF, Graph API), workers, and AI agents (LLMs, LangGraph nodes, tools) CANNOT authenticate or decide for humans. Human decisions require authentic human cryptographic evidence (§3.3, §1A.2).
3. **Discrete, Unshared Service Identities**: BFF, Graph runtime, and future workers have strictly isolated credentials and narrow scopes. No shared root or omnipotent secret is permitted (§11).
4. **Zero Direct PostgreSQL Access for BFF**: The BFF service communicates exclusively via the external REST API over HTTPS. It has zero PostgreSQL connection strings, zero DB roles, and zero direct database privileges (§11).
5. **Gamertag Display Separation**: Gamertags and display names are presentation metadata only. Authorization, foreign keys, and audit logs bind exclusively to immutable UUIDs (`user_id`) and verified numeric EA player identifiers (`player_id`) (§3.4).
6. **Inactivity Never Authorizes**: Timeouts or lack of response alter escalation status, but NEVER commit approvals or decisions (§3.5, §1A.1).
7. **Dispatch Dormancy**: Universal `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` across all identity and service boundaries (§3.1).

---

## 2. Service Principals & Trust Boundaries

```
+--------------------------------------------------------------------------------------------------+
|                                    SECURITY BOUNDARY TOPOLOGY                                    |
+--------------------------------------------------------------------------------------------------+

   [ Web / Mobile Clients ]
              |
              | HTTPS (Session Cookie / Bearer Token)
              v
+-------------------------------+
|      BFF Gateway Service      |  <-- Identity: bff_service (BFF_SERVICE_TOKEN)
|  (No DB Access, Read Scopes)  |  <-- ZERO PostgreSQL credentials or network access
+-------------------------------+
              |
              | REST API over HTTPS (Bearer BFF_SERVICE_TOKEN + X-Club-ID)
              v
+-------------------------------+              +------------------------------------+
|        FastAPI Graph API      | <----------- | Internal Ingest / Watchers         |
|      (Authentication Layer)   |  (X-API-Key) | Identity: graph_service            |
+-------------------------------+              | (GRAPH_SERVICE_TOKEN)              |
              |                                +------------------------------------+
              |
              | Connection Pool (fc27_app role: least-privilege DML)
              v
+-------------------------------+
|     PostgreSQL 16 Database    |
| (fc27_users, sessions, roles) |
+-------------------------------+
              ^
              | (DORMANT in Phase 2: DISPATCH_WORKER_ENABLED=false)
              |
+-------------------------------+
|    Future Dispatch Worker     |  <-- Identity: worker_service (WORKER_SERVICE_TOKEN)
|  (Connection: fc27_dispatch)  |  <-- Strictly isolated credential
+-------------------------------+
```

### 2.1 `bff_service` Principal Specification
- **Application Role**: `UserRole.BFF_SERVICE` (`"bff_service"` in `fc27_user_roles`).
- **Principal Object**:
  ```python
  PrincipalContext(
      user_id=UUID("00000000-0000-0000-0000-000000000001"),
      club_id=forwarded_club_id,  # From X-Club-ID header, validated against club regex
      roles=frozenset({UserRole.BFF_SERVICE}),
      auth_level=AuthLevel.AUTHENTICATED,
      auth_method=AuthMethod.SERVICE_TOKEN,
      is_service=True,
      display_name="BFF Gateway Service",
  )
  ```
- **Allowed Actions Matrix**:
  - Read Projections: `run:view`, `package:view`, `assignment:view`, `roster:view`, `observation:view`, `assessment:view`, `user:view`.
  - Health Checks: `health:view` (`/health/live`, `/health/ready`).
  - Relay Operations: Relaying unauthenticated auth initiation requests (e.g. `POST /v1/auth/passkeys/login/options`, `POST /v1/auth/invitations/{token}/accept`).
- **Prohibited Actions**:
  - ALL human decision actions: `package:approve`, `package:reject`, `assignment:accept`, `assignment:reject`, etc.
  - Administration: `user:create`, `user:update_status`, `user:role_assign`, `delegation:create`, `delegation:revoke`.
  - Direct Mutations: Cannot directly alter graph threads, packages, or decisions.
- **Database Boundary Enforcement**:
  - The BFF process has **no database connection string** and **no PostgreSQL driver configuration**.
  - `migrations/post/grants.sql` explicitly grants DML permissions to `fc27_app` and `fc27_dispatch`. No database role or grants exist for BFF.
  - Any attempt to configure direct DB access for BFF is an immediate architecture violation.
- **Credential & Rotation**:
  - Primary Secret: `BFF_SERVICE_TOKEN` (>= 32 characters, high-entropy).
  - Standby Rotation Secret: `BFF_SERVICE_TOKEN_ROTATION` (optional second active token).
  - During rotation, the server accepts either primary or standby token using constant-time comparison (`hmac.compare_digest`). Once the BFF client switches to the new token, the old token is decommissioned.

### 2.2 `graph_service` Principal Specification
- **Application Role**: `UserRole.GRAPH_SERVICE` (`"graph_service"` in `fc27_user_roles`).
- **Principal Object**:
  ```python
  PrincipalContext(
      user_id=UUID("00000000-0000-0000-0000-000000000002"),
      club_id="0",  # Internal system scope
      roles=frozenset({UserRole.GRAPH_SERVICE}),
      auth_level=AuthLevel.AUTHENTICATED,
      auth_method=AuthMethod.SERVICE_TOKEN,
      is_service=True,
      display_name="Graph API Internal Runtime",
  )
  ```
- **Allowed Actions Matrix**:
  - Workflow Execution: `run:create`, `run:view`, `run:resume`.
  - Health & Metrics: `health:view`.
- **Prohibited Actions**:
  - Decisions: Cannot approve or reject packages (`package:approve`, `package:reject`).
  - Decisions: Cannot accept or reject player assignments (`assignment:accept`, etc.).
  - Delegations: Cannot create or revoke delegations (`delegation:create`).
  - Identity Admin: Cannot provision users or assign roles.
- **Database Boundary**:
  - Executes under the `fc27_app` PostgreSQL database role.
  - Has `SELECT, INSERT, UPDATE` on application tables (`graph_threads`, `fc27_users`, `fc27_sessions`, etc.).
  - Has zero DDL privileges (cannot `CREATE TABLE`, `DROP TABLE`, or `ALTER TABLE`).
  - Must pass runtime posture checks (`check_runtime_posture()` in `src/db_roles.py`).

### 2.3 Worker & Ops Boundary Specification
- **Application Role**: Scope-limited operational role (e.g. `UserRole.STAFF`).
- **Phase 2 Invariant**:
  - `DISPATCH_WORKER_ENABLED=false` and `DISPATCH_ENABLED=false` at all times.
  - The dispatch worker is dormant and does not execute in Phase 2.
- **Prohibition Against Shared Root Keys**:
  - Anti-Pattern: A shared master token (e.g. `MASTER_API_KEY`) used across BFF, Graph API, and worker scripts.
  - Rule: `WORKER_SERVICE_TOKEN` must be a distinct, dedicated secret loaded via `read_secret("WORKER_SERVICE_TOKEN")`.
  - At startup, the server validates:
    ```python
    if hmac.compare_digest(worker_token, graph_token) or hmac.compare_digest(worker_token, bff_primary):
        raise ConfigError("Shared service credentials forbidden")
    ```
  - Worker tokens have zero decision capability: any attempt to call `/approval/packages/{id}/decide` or `/v1/approval-requests/{id}/decisions` fails with HTTP 403 Forbidden.

---

## 3. Anti-Impersonation Boundaries

### 3.1 Axiom of Non-Impersonation
ORIGINAL_REQUEST.md §3.3 & §1A.2:
> "No agent may authenticate or decide for a human. This includes: player agents, Coach agents, Commissioner-facing agents, model agents, LangGraph, MCP tools, background workers, BFF service, WebSocket service, Redis subscribers. Agents may assist humans. Agents cannot become the human actor."

### 3.2 Multi-Layer Defense Architecture

```
[ Request Inbound ]
        |
        v
+-------------------------------------------------------------+
| Layer 1: Credential Source Verification                      |
| - Principal identity extracted ONLY from validated header   |
| - Request body actor/user_id claims are strictly IGNORED    |
| - Service token sets: principal.is_service = True           |
+-------------------------------------------------------------+
        |
        v
+-------------------------------------------------------------+
| Layer 2: Policy Engine Boundary Guard                       |
| - policy.authorize(principal, action, resource)             |
| - If principal.is_service AND action in HUMAN_ACTIONS:      |
|     RAISE AgentImpersonationError (HTTP 403)                |
+-------------------------------------------------------------+
        |
        v
+-------------------------------------------------------------+
| Layer 3: Cryptographic Evidence Requirement                 |
| - Commissioner: proof_type=webauthn + evidence_id           |
| - Player: Passkey assertion or scoped action token          |
| - Private keys reside in client hardware; services lack keys|
+-------------------------------------------------------------+
        |
        v
+-------------------------------------------------------------+
| Layer 4: Audit Trail Immutability                           |
| - domain_events records actual principal.user_id as actor   |
| - Any service attempt logged as security event              |
+-------------------------------------------------------------+
```

### 3.3 List of Forbidden Actions for Service Principals
The following actions in `HUMAN_DECISION_ACTIONS` can NEVER be executed by a service or worker principal:
1. `ActionType.PACKAGE_APPROVE` (`"package:approve"`)
2. `ActionType.PACKAGE_REJECT` (`"package:reject"`)
3. `ActionType.PACKAGE_REQUEST_REVISION` (`"package:request_revision"`)
4. `ActionType.ASSIGNMENT_ACCEPT` (`"assignment:accept"`)
5. `ActionType.ASSIGNMENT_REJECT` (`"assignment:reject"`)
6. `ActionType.ASSIGNMENT_CHANGE_REQUEST` (`"assignment:change_request"`)
7. `ActionType.ASSIGNMENT_PAUSE` (`"assignment:pause"`)
8. `ActionType.ASSIGNMENT_ACKNOWLEDGE` (`"assignment:acknowledge"`)
9. `ActionType.DELEGATION_CREATE` (`"delegation:create"`)
10. `ActionType.DELEGATION_REVOKE` (`"delegation:revoke"`)
11. `ActionType.USER_ROLE_ASSIGN` (`"user:role_assign"`)
12. `ActionType.USER_ROLE_REVOKE` (`"user:role_revoke"`)

### 3.4 Cryptographic Step-Up Evidence Verification
When an authenticated human submits a decision:
- **Commissioner Decision Flow**:
  1. Requests step-up challenge: `POST /v1/auth/step-up/options` bound to exact `(package_id, version, payload_hash, decision)`.
  2. Performs FIDO2/WebAuthn ceremony on client device.
  3. Verifies assertion: `POST /v1/auth/step-up/verify` -> returns single-use `authentication_evidence_id`.
  4. Submits decision with `proof_type="webauthn"` and `authentication_evidence_id`.
  5. The server checks that `evidence_id` belongs to `principal.user_id`, is unconsumed, matches `bound_payload_hash`, and consumes it atomically.
- **Inactivity Guard (§3.5)**:
  - If a timer or deadline expires, the escalation handler in `approval_escalations` records state transition.
  - The system NEVER auto-approves or auto-submits a decision on timeout.

---

## 4. Gamertag Display Separation

### 4.1 Problem Definition (§3.4)
In sports and gaming environments (e.g. EA Sports FC), players and coaches frequently identify themselves by gamertags, handles, or display names (e.g. `"StrikerKing99"`, `"CoachPep"`).
Allowing mutable display strings to act as authorization keys creates severe security vulnerabilities:
1. **Name Spoofing**: An attacker registers a gamertag matching a Commissioner's name to hijack authorization.
2. **Handle Reassignment**: A player changes their gamertag, causing orphaned records or authorization cross-talk.
3. **Collision / Encoding Attacks**: Unicode homoglyphs, whitespace differences, and casing mismatches undermine security boundaries.

### 4.2 Separation Blueprint

| Attribute | Storage Location | Mutability | Used For Authorization? | Used In Audit / Events? |
|---|---|---|---|---|
| `user_id` | `fc27_users.user_id` | **Immutable** (UUIDv4) | **YES (Primary Identity)** | **YES (`actor_id`)** |
| `player_id` | `fc27_users.player_id` | **Immutable / Set-Once** (`^[0-9]{1,20}$`) | **YES (Roster Isolation)** | **YES (`player_id`)** |
| `login_id` | `fc27_users.login_id` | Normalized lowercase | **NO** (Auth Lookup Only) | For login events |
| `display_name` | `fc27_users.display_name` | **Mutable Presentation** | **STRICTLY NO** | UI presentation only |
| `gamertag` | Virtual alias / UI field | **Mutable Presentation** | **STRICTLY NO** | UI presentation only |

### 4.3 Database Enforcement
1. **Trigger Protection**: `migrations/0004_identity_foundation.sql` (lines 31–42):
   ```sql
   IF NEW.user_id <> OLD.user_id THEN
       RAISE check_violation USING MESSAGE = 'user_id is immutable';
   END IF;
   IF NEW.club_id <> OLD.club_id THEN
       RAISE check_violation USING MESSAGE = 'club_id is immutable';
   END IF;
   ```
2. **Roster Match Rule**:
   - When verifying player-scoped assignments in `policy.authorize()`:
     ```python
     player_matches = (res.player_id is not None and res.player_id == principal.player_id)
     user_matches = (res.owner_user_id is not None and res.owner_user_id == principal.user_id)
     if not (player_matches or user_matches):
         raise PlayerIsolationError(...)
     ```
   - The check never references `principal.display_name` or `resource.display_name`.
3. **Unverified EA Identity Rule (§3.4)**:
   - If a player's numeric EA `player_id` is null or unverified, the system preserves `player_id = NULL`.
   - The system NEVER infers, synthesizes, or substitutes a gamertag as the missing `player_id`.

---

## 5. Integration with Graph API (`src/graph_api.py`)

### 5.1 Route Mapping & Principal Injection
In Phase 2, `src/graph_api.py` migrates to versioned routes with centralized authorization:

```python
@app.post("/v1/runs/{thread_id}/decision", response_model=DecisionResponse)
def apply_decision(
    request: Request,
    body: DecisionSubmission,
    thread_id: str = ThreadIdPath,
    principal: PrincipalContext = Depends(get_current_principal),
) -> dict:
    ...
```

### 5.2 Step-by-Step Decision Flow

```
1. Client HTTP POST /v1/runs/{thread_id}/decision
   Header: Authorization: Bearer <session_or_service_token>
   Body: {
       "decision": "approved",
       "proof_type": "webauthn",
       "authentication_evidence_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
       "authority_basis": "primary"
   }

2. Authentication Dependency (get_current_principal)
   - Evaluates token.
   - If service token -> principal.is_service = True.
   - If session token -> principal.is_service = False, loads user_id, roles, club_id.

3. Resource Loading
   - Loads approval_request and graph_thread from PostgreSQL:
     req_row = store.get_approval_request(thread_id)
   - Constructs Resource:
     resource = Resource(
         resource_type="package",
         club_id=req_row["club_id"],
         resource_id=req_row["package_id"],
         version=req_row["package_version"],
         payload_hash=req_row["package_hash"],
     )

4. Centralized Authorization Engine Call
   policy.authorize(
       principal=principal,
       action=ActionType.PACKAGE_APPROVE,
       resource=resource,
       authority_basis=body.authority_basis,
   )
   --> If principal.is_service: raises AgentImpersonationError (403)
   --> If principal.club_id != resource.club_id: raises CrossClubAccessError (403)
   --> If role != commissioner and no delegation: raises RoleNotPermittedError (403)

5. Proof Verification
   - If body.proof_type == "webauthn":
       store.verify_and_consume_step_up_evidence(
           evidence_id=body.authentication_evidence_id,
           user_id=principal.user_id,
           bound_payload_hash=resource.payload_hash,
       )
   - If body.proof_type == "hmac":
       if not settings.cli_hmac_enabled:
           raise HTTPException(403, "proof_type_disabled")
       store.verify_hmac_signature(...)

6. PostgreSQL Decision Transaction
   - Writes row to public.approval_decisions with:
       actor_id = principal.user_id
       actor_type = "commissioner"
       authority_basis = body.authority_basis
       proof_type = body.proof_type
       evidence_id = body.authentication_evidence_id
   - Emits domain_event: "identity.decision_committed" with actor=principal.user_id.

7. Workflow Resume (Non-Authoritative Signal)
   - graph.resume_graph(thread_id) wakes the graph runner.
   - Graph runner reads PostgreSQL decision row.
   - DISPATCH_ENABLED=false: Response confirms decision recorded, no player delivery.
```

---

## 6. Test Design & Verification Architecture

### 6.1 Test Matrix for Service Identities & Boundaries

| Test Identifier | Category | Target Invariant | Expected Outcome |
|---|---|---|---|
| `test_bff_service_authentication_valid` | Authentication | BFF bearer token yields `UserRole.BFF_SERVICE`, `is_service=True` | HTTP 200 / valid principal |
| `test_bff_service_token_rotation` | Key Management | Secondary rotation token authenticates seamlessly | HTTP 200 during rotation |
| `test_bff_service_zero_db_privilege` | Security Boundary | Verify BFF principal has no DB connection or DDL rights | DB connection fails / not configured |
| `test_graph_service_authentication_valid` | Authentication | `GRAPH_SERVICE_TOKEN` yields `UserRole.GRAPH_SERVICE` | HTTP 200 / valid principal |
| `test_worker_isolated_credential_enforced` | Key Management | Startup fails if Worker token matches BFF or Graph token | Raises `ConfigError` |
| `test_anti_impersonation_bff_submitting_approval` | Anti-Impersonation | BFF token attempting `POST /v1/runs/{id}/decision` | HTTP 403 `AGENT_IMPERSONATION_FORBIDDEN` |
| `test_anti_impersonation_graph_submitting_decision` | Anti-Impersonation | Graph service token calling decision endpoint | HTTP 403 `AGENT_IMPERSONATION_FORBIDDEN` |
| `test_anti_impersonation_worker_submitting_decision` | Anti-Impersonation | Worker token calling decision endpoint | HTTP 403 `AGENT_IMPERSONATION_FORBIDDEN` |
| `test_anti_impersonation_forged_actor_in_body` | Anti-Impersonation | Service sending `{"actor": "commissioner_uuid"}` | Ignores body; returns HTTP 403 |
| `test_gamertag_spoofing_prevented` | Tenancy & Identity | User with `display_name="Commissioner"` denied commissioner actions | HTTP 403 `ROLE_NOT_PERMITTED` |
| `test_gamertag_mutation_leaves_authorization_intact` | Identity Separation | Updating `display_name` does not alter session, user_id, or access | HTTP 200 / permissions unchanged |
| `test_unverified_player_id_no_gamertag_fallback` | Player Isolation | Account with NULL `player_id` cannot access player assignments | HTTP 403 `PLAYER_ISOLATION_VIOLATION` |
| `test_cross_club_service_forwarding` | Tenancy Boundary | BFF forwarding `X-Club-ID` for another club gets isolated | Access strictly club-scoped |
| `test_secrets_omitted_from_logs_and_errors` | Cryptographic Safety | Exception text and log filters redact tokens and secrets | Zero tokens in logs / 422 errors |
| `test_dispatch_dormancy_across_all_service_calls` | Dispatch Safety | `DISPATCH_ENABLED=false` asserted in all responses | `dispatch.enabled == False` |

---

## 7. Migration & Rollback Considerations

- **No Schema Changes Required**: Milestone 1 (`migrations/0004_identity_foundation.sql`) already defined `fc27_users`, `fc27_user_roles` with `'bff_service'` and `'graph_service'`, `fc27_sessions` with `'service_token'`, and `fc27_challenges`.
- **Grants Integrity**: `migrations/post/grants.sql` correctly preserves least privilege:
  - Grants are restricted to `fc27_app`, `fc27_dispatch`, and `fc27_owner`.
  - Zero grants for BFF ensures the security boundary cannot be bypassed at the SQL layer.
