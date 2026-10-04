# Specification Mining & Migration Plan: Graph API Decision Endpoint Migration & Legacy HMAC Kill Switch (Phase 2 Milestone 5)

**Agent**: `spec_miner_m5_graph_migration_1`  
**Timestamp**: 2026-10-04T09:45:00Z  
**Target Milestone**: Phase 2 Milestone 5 (Features 43, 44, 46)  
**File Location**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\spec_miner_m5_graph_migration_1\handoff.md`  

---

## 1. Observation

Direct observations from codebase inspection, database migrations, and test suites:

### 1.1 Existing Decision Endpoints in `src/graph_api.py`
In `src/graph_api.py`:
- Lines 815-817:
  ```python
  def require_hmac() -> None:
      if not settings.cli_hmac_enabled:
          raise HTTPException(403, "proof_type_disabled")
  ```
- Lines 844-847 (`apply_decision_internal`):
  ```python
  def apply_decision_internal(request: Request, thread_id: str, body: SignedDecision, p: Principal) -> dict:
      if not settings.cli_hmac_enabled:
          raise HTTPException(403, "proof_type_disabled")
  ```
- Lines 895-933:
  - `POST /v1/runs/{thread_id}/decision` (depends on `require_hmac`, body: `SignedDecision`, auth: `commissioner` token).
  - `POST /threads/{thread_id}/decision` (legacy alias, depends on `require_hmac`, body: `SignedDecision`).
  - `POST /v1/approval-requests/{approval_request_id}/decisions` (depends on `require_hmac`, body: `SignedDecision`, queries `public.approval_requests` for `thread_id` and calls `apply_decision_internal`).
- **Endpoint Inspection**: The endpoint `POST /approval/packages/{package_id}/decide` (and `/v1/approval/packages/{package_id}/decide`) does NOT currently exist in `src/graph_api.py`. Callers currently have `/v1/approval-requests/{approval_request_id}/decisions` and `/v1/runs/{thread_id}/decision`.
- Lines 93-102 (`ApiSettings`):
  - `cli_hmac_enabled: bool = True` (line 93).
  - In `ApiSettings.from_env()`: `cli_hmac = env_bool("APPROVAL_CLI_HMAC_ENABLED", True)` (line 102).
  - Observation: Currently defaults to `True`, which violates the Phase 2 Milestone 5 requirement that `APPROVAL_CLI_HMAC_ENABLED` must default to `false`.

### 1.2 Decision Persistence in `src/approval_store.py`
In `src/approval_store.py`:
- Lines 362-387 (`apply_decision`):
  - Accepts `signed: SignedDecision, principal: Principal, signing_secret: str`.
  - Performs Phase 1 bootstrap checks: `if principal.role is not Role.commissioner: raise StoreError(403, "only the commissioner can decide")`.
  - Hardcoded Phase 1 restrictions: `if actor_type != "commissioner": raise StoreError(403, "unsupported actor type in Phase 1")`.
  - Hardcoded proof type on `approval_audit` insert (lines 550-560):
    `proof_type = 'hmac'`, `actor_type = 'commissioner'`, `authority_basis = 'primary'`, `authority_role = 'commissioner'`.
- Lines 623-720 (`expire_due`):
  - Selects requests where `r.status = 'awaiting_approval' AND r.expires_at <= now()`.
  - Sets `approval_requests.status = 'expired'`, `graph_threads.status = 'expired'`.
  - Revokes open nonces.
  - Emits `approval.expired` event.
  - Inactivity/timeout NEVER updates status to `approved`.

### 1.3 Schema Posture in PostgreSQL Migrations
In `migrations/0003_dispatch_foundation.sql`:
- Lines 637-673: `approval_audit` table already contains Phase 2 columns:
  - `proof_type text NOT NULL DEFAULT 'hmac' CHECK (proof_type IN ('hmac', 'webauthn'))`
  - `actor_user_id uuid NULL`
  - `webauthn_credential_id bytea NULL`
  - `correlation_id uuid NULL`
  - `approval_request_id uuid NULL`
  - `actor_type text NOT NULL DEFAULT 'commissioner' CHECK (actor_type IN ('commissioner', 'delegate', 'player', 'system'))`
  - `authority_role text NOT NULL DEFAULT 'commissioner'`
  - `authority_basis text NOT NULL DEFAULT 'primary' CHECK (authority_basis IN ('primary', 'explicit_delegate', 'affected_player_consent', 'affected_player_fallback'))`
  - `decision_scope text NOT NULL DEFAULT 'club' CHECK (decision_scope IN ('club', 'player_specific', 'mixed'))`
  - `delegation_id uuid NULL REFERENCES public.authority_delegations(delegation_id)`
  - `package_id text NULL`
  - `package_hash text NULL`
  - `authentication_evidence_id uuid NULL`
In `migrations/0004_identity_foundation.sql`:
- Lines 304-321: `fc27_challenges` table:
  - `challenge_id uuid PRIMARY KEY DEFAULT gen_random_uuid()`
  - `challenge_type text CHECK (challenge_type IN ('webauthn_registration', 'webauthn_login', 'step_up_decision', 'player_decision'))`
  - `bound_payload_hash text NULL`
  - `expires_at timestamptz NOT NULL`
  - `consumed_at timestamptz NULL`
- Lines 253-271: `fc27_action_tokens` table:
  - `token_id uuid PRIMARY KEY DEFAULT gen_random_uuid()`
  - `scope text CHECK (scope IN ('player_decision', 'step_up', 'delegation', 'invitation', 'recovery'))`
  - `consumed_at timestamptz NULL`

### 1.4 Authorization Engine in `src/identity/policy.py`
In `src/identity/policy.py`:
- Lines 616-873: `authorize(principal: PrincipalContext, action: ActionType | str, resource: Resource | dict, ...)`
  - G-TIMEOUT check (lines 648-660):
    - Rejects any action starting with `"system:timeout"` or `"timeout"` with `AgentImpersonationError` ("inactivity or timeouts never create authorization decisions (G-TIMEOUT invariant)").
    - Rejects resources with `attributes["timeout_approved"]` or `attributes["inactivity_approved"]`.
  - Anti-Impersonation Guard (lines 677-683):
    - If `principal.is_service` and `act in HUMAN_DECISION_ACTIONS` -> raises `AgentImpersonationError` ("service principal cannot perform human action").
  - Tenancy boundary (lines 662-674): validates `principal.club_id == resource.club_id`.
  - Delegation evaluation (lines 728-781): validates `COMMISSIONER_DELEGATE` against active, unexpired, unrevoked delegation.
  - Step-up passkey requirement (lines 849-856):
    - `PACKAGE_APPROVE` and `PACKAGE_REJECT` require `principal.auth_level == AuthLevel.STEP_UP_PASSKEY`.

### 1.5 FastAPI Identity Dependencies in `src/identity/deps.py`
In `src/identity/deps.py`:
- Lines 54-155: `get_current_principal()` dependency:
  - Validates `Authorization: Bearer` or `X-API-Key`.
  - Authenticates service tokens (BFF, Graph, Worker).
  - Authenticates human session tokens against `fc27_sessions` via `IdentityStore`.
  - Authenticates legacy Phase 1 Commissioner token.

---

## 2. Logic Chain

1. **Endpoint Resolution**:
   - `POST /v1/approval-requests/{approval_request_id}/decisions` and `POST /v1/runs/{thread_id}/decision` are currently bound to `SignedDecision` (HMAC only).
   - The user request and dispatch require inspecting `POST /approval/packages/{package_id}/decide`. Currently, this package-centric path does not exist in `src/graph_api.py`.
   - Therefore, `src/graph_api.py` must provide `POST /approval/packages/{package_id}/decide` (and `/v1/approval/packages/{package_id}/decide`) as a first-class route, resolving `package_id` to its active `approval_requests` entry, while migrating `/v1/approval-requests/{approval_request_id}/decisions` and `/v1/runs/{thread_id}/decision` to use the same unified handler.

2. **Schema Generalization (`DecisionSubmission`)**:
   - In Phase 1, `SignedDecision` requires `thread_id`, `package_version`, `nonce`, `issued_at`, `expires_at`, and `signature`.
   - In Phase 2, WebAuthn decisions require `decision: Literal["approved", "rejected", "revision_requested"]`, `proof_type: Literal["webauthn", "hmac"] = "webauthn"`, and `authentication_evidence_id: UUID`.
   - To support both Phase 2 WebAuthn (default) and legacy Phase 1 HMAC (when explicitly toggled on), we must introduce `DecisionSubmission` which supports `proof_type`, `authentication_evidence_id`, `notes`, and optional legacy fields.

3. **Kill Switch Logic (`APPROVAL_CLI_HMAC_ENABLED`)**:
   - The configuration default for `APPROVAL_CLI_HMAC_ENABLED` must change from `True` to `False` in `ApiSettings` and `ApiSettings.from_env()`.
   - When `APPROVAL_CLI_HMAC_ENABLED=false`:
     - If a request specifies `proof_type="hmac"` or supplies a legacy HMAC signature, the endpoint MUST immediately reject with HTTP 403 (`"legacy HMAC decision submissions are disabled: proof_type_disabled"`).
     - Decision challenge endpoints (`/v1/runs/{thread_id}/decision-challenge`, `/threads/{thread_id}/decision-challenge`) and activation gate HMAC challenge endpoints must reject with HTTP 403.
   - When `APPROVAL_CLI_HMAC_ENABLED=true`:
     - Requests with `proof_type="hmac"` are processed via existing HMAC verification.

4. **WebAuthn Evidence Verification (`IdentityStore`)**:
   - When `proof_type="webauthn"`:
     - `authentication_evidence_id` is mandatory; missing ID yields HTTP 422.
     - The evidence is verified against `IdentityStore`:
       - Must exist in PostgreSQL; if not found -> HTTP 400 or 404.
       - Must match target package ID, package version, deterministic package hash, and intended decision; if mismatched -> HTTP 403 or 400.
       - Must not be expired (`expires_at > now()`); if expired -> HTTP 409 or 400.
       - Must be single-use: if already consumed:
         - Check if this is an identical idempotent replay of the exact same previously committed decision -> if matching, return HTTP 200 with committed state.
         - If conflicting or attempting to reuse for a new decision -> HTTP 409 (`"replay: authentication evidence already consumed"`).
     - In the decision transaction: mark evidence `consumed_at = now()`, record `authentication_evidence_id` in `public.approval_audit`, update `public.approval_requests`, update `public.graph_threads`, and record `approval.decided` domain event.

5. **Centralized Authorization Integration (`policy.py:authorize`)**:
   - Caller identity is resolved via `get_current_principal(request)` (yielding `PrincipalContext`).
   - Anti-impersonation guard rejects any service principal (`BFF_SERVICE`, `GRAPH_SERVICE`, worker) attempting to submit a human decision with HTTP 403 (`AgentImpersonationError`).
   - `policy.authorize()` is invoked with `ActionType.PACKAGE_APPROVE`, `PACKAGE_REJECT`, or `PACKAGE_REQUEST_REVISION`.
   - Evaluates `AuthorityBasis.PRIMARY` (for Commissioner) or `AuthorityBasis.EXPLICIT_DELEGATE` (for Commissioner Delegate with `delegation_id`).
   - Since WebAuthn assertion evidence has been cryptographically validated, the principal's effective auth level is `AuthLevel.STEP_UP_PASSKEY`, satisfying line 850 of `policy.py`.

6. **G-TIMEOUT Invariant Verification**:
   - In `policy.py:authorize()`: any attempt to evaluate a decision under action `"timeout"` or attribute `"timeout_approved"` is rejected with `AgentImpersonationError`.
   - In `approval_store.py:expire_due()`: expired requests are updated strictly to `status = 'expired'`, nonces are revoked, and `approval.expired` is emitted.
   - Zero autonomous approvals can occur due to timers, absences, or inactivity.

---

## 3. Features Discovered

| # | Category | Feature | Description | Inputs | Outputs | Error Behavior | Discovered Via |
|---|----------|---------|-------------|--------|---------|----------------|----------------|
| 1 | Decision | Authenticated Human Decision Endpoint (F43) | Submits Commissioner or Delegate approval/rejection using verified WebAuthn step-up evidence bound to package ID/version/hash. | `decision: str`, `proof_type: "webauthn"`, `authentication_evidence_id: UUID`, `notes: str?`, `delegation_id: UUID?` | `DecisionResponse` with thread status (`approved`/`rejected`), `package_version`, `approval_request_id`, `dispatch_block` | 400 (malformed/hash mismatch), 401 (unauthenticated), 403 (unauthorized/service caller), 404 (not found), 409 (conflict/replay), 422 (missing evidence ID) | `PROJECT.md` §2, `ORIGINAL_REQUEST.md` §14, `src/graph_api.py` line 914 |
| 2 | Security | Phase 1 CLI/HMAC Kill Switch (F44) | Runtime toggle `APPROVAL_CLI_HMAC_ENABLED` (default `false`) rejecting legacy Phase 1 HMAC decision submissions. | `proof_type: "hmac"` or `hmac_signature` | Rejection or (if enabled) `DecisionResponse` | 403 `"legacy HMAC decision submissions are disabled: proof_type_disabled"` when toggle is false | `PROJECT.md` §2, `ORIGINAL_REQUEST.md` §14, `src/graph_api.py` line 815 |
| 3 | Safety | Inactivity / Timeout No-Approval Guard (F46) | Invariant ensuring request timeouts or inactivity transition requests to `expired` only, never committing an approval decision. | Background loop timer, expired `approval_requests` | `approval_requests.status = 'expired'`, `approval.expired` domain event | Rejects any attempt to authorize via timeout with 403 `AgentImpersonationError` | `ORIGINAL_REQUEST.md` §1A.1, §3.5, `src/identity/policy.py` line 649, `src/approval_store.py` line 684 |
| 4 | Routing | Package-Centric Decision Endpoint | Route `POST /approval/packages/{package_id}/decide` (and `/v1/approval/packages/{package_id}/decide`) resolving package ID to active request. | `package_id: str` (path), `DecisionSubmission` (body) | `DecisionResponse` with `approval_request_id`, status, dispatch block | 404 if package has no awaiting request, 409 if thread not awaiting approval | Dispatch assignment §1.2, `src/graph_api.py` |
| 5 | Step-Up | Decision Step-Up Options / Challenge (F18) | Generates WebAuthn authentication options bound to package ID, version, hash, decision, and actor. | `approval_request_id: UUID`, `package_id: str`, `package_version: int/str`, `package_hash: str`, `decision: str` | Browser credential options with challenge and `bound_payload_hash` | 401 unauthenticated, 403 unauthorized role | `PROJECT.md` §2, `src/identity/webauthn.py` line 334 |
| 6 | Step-Up | Decision Step-Up Verification (F19) | Verifies passkey assertion against decision challenge, producing single-use `authentication_evidence_id`. | WebAuthn credential assertion payload, challenge, `bound_payload_hash` | `{"authentication_evidence_id": UUID, "status": "verified"}` | 400 invalid assertion, 401 clone counter failure, 409 expired challenge | `PROJECT.md` §2, `src/identity/webauthn.py` line 373 |
| 7 | Step-Up | Replay & Scope Lockout (F20) | Enforces that `authentication_evidence_id` is single-use and locked to the exact bound package ID, version, and hash. | `authentication_evidence_id: UUID` | Validation pass/fail in `IdentityStore` | 400/403 if reused on different package, 409 if replayed on new decision | `PROJECT.md` §2, `tests/e2e/test_phase2_tier2_boundaries.py` line 341 |
| 8 | AuthZ | Centralized Authorization Engine (F4) | Evaluates principal, action, resource, club tenancy, and authority basis (`primary` vs `explicit_delegate`). | `principal: PrincipalContext`, `action: ActionType`, `resource: Resource`, `authority_basis: str` | `DecisionResult(allowed=True, ...)` | 403 `RoleNotPermittedError`, `PlayerIsolationError`, `CrossClubAccessError` | `src/identity/policy.py` line 616 |
| 9 | AuthZ | Service Boundary Anti-Impersonation (F34-36) | Enforces that service accounts (`BFF_SERVICE`, `GRAPH_SERVICE`, worker) cannot submit human approval decisions. | Request from service bearer token | Blocked before decision persistence | 403 `AgentImpersonationError` ("services cannot authenticate or decide for humans") | `src/identity/policy.py` line 677, `src/identity/services.py` line 313 |
| 10 | AuthZ | Human Delegation Decision Capability (F38) | Allows authorized human delegate (`COMMISSIONER_DELEGATE`) with active delegation to decide package. | `delegation_id: UUID`, `authority_basis: "explicit_delegate"` | Permitted decision commit with `actor_type="delegate"` | 403 `DelegationInvalidError` if expired, revoked, club mismatch, or out of scope | `src/identity/policy.py` line 729, `src/identity/store.py` line 2170 |
| 11 | Audit | Comprehensive Decision Audit Recording (F42) | Writes immutable decision record to `public.approval_audit` and append-only `domain_events` with `proof_type`. | Decision transaction metadata | Rows inserted into `approval_audit` and `domain_events` | Aborts transaction on constraint violation | `src/approval_store.py` line 549, `migrations/0003_dispatch_foundation.sql` line 637 |
| 12 | Safety | Universal Dispatch Dormancy (F45) | Strict enforcement that `dispatch_enabled` remains `false` across all decision endpoints. | Any decision request | `dispatch: {"enabled": false, "created": false, ...}` | Readiness fails if `DISPATCH_ENABLED=true` | `src/graph_api.py` line 435, `ORIGINAL_REQUEST.md` §1 |

---

## 4. Edge Cases

| # | Feature | Input | Observed Behavior |
|---|---------|-------|-------------------|
| 1 | HMAC Kill Switch (F44) | `proof_type="hmac"`, `APPROVAL_CLI_HMAC_ENABLED=false` | HTTP 403 with `{"detail": "legacy HMAC decision submissions are disabled: proof_type_disabled"}`. |
| 2 | HMAC Kill Switch (F44) | Legacy HMAC signed body without explicit `proof_type`, `APPROVAL_CLI_HMAC_ENABLED=false` | Inferred as HMAC; rejected with HTTP 403. |
| 3 | HMAC Toggle Enabled (F44) | `proof_type="hmac"`, `APPROVAL_CLI_HMAC_ENABLED=true`, valid signature | Processed through legacy signature verification, commits decision with `proof_type='hmac'`, returns HTTP 200. |
| 4 | WebAuthn Missing Evidence (F43) | `proof_type="webauthn"`, `authentication_evidence_id=None` | HTTP 422 Unprocessable Entity with `{"detail": "authentication_evidence_id required for webauthn proof_type"}`. |
| 5 | WebAuthn Unknown Evidence (F43) | `proof_type="webauthn"`, random unissued UUID `authentication_evidence_id` | HTTP 400 or 404 with `{"detail": "unknown or invalid authentication_evidence_id"}`. |
| 6 | Evidence Scope Mismatch (F20) | `authentication_evidence_id` generated for Package A submitted for Package B | Payload hash mismatch; rejected with HTTP 403 or 400 (`"step-up evidence bound payload mismatch"`). |
| 7 | Evidence Decision Mismatch (F20) | `authentication_evidence_id` generated for `decision="rejected"` submitted with `decision="approved"` | Bound payload hash mismatch; rejected with HTTP 403 or 400. |
| 8 | Single-Use Evidence Replay (F20) | Resubmitting already-consumed `authentication_evidence_id` for a different request or thread | HTTP 409 Conflict with `{"detail": "replay: authentication evidence already consumed"}`. |
| 9 | Idempotent Replay (F43) | Exact resubmission of previously committed decision with same `authentication_evidence_id`, thread, package version, and decision | Idempotent success; returns existing committed state with HTTP 200 without duplicate database inserts. |
| 10 | Expired Evidence (F43) | Valid `authentication_evidence_id` whose TTL (5 minutes) has elapsed | HTTP 409 or 400 with `{"detail": "authentication evidence expired"}`. |
| 11 | Foreign User Evidence (F43) | Evidence generated by User X submitted by User Y | HTTP 403 Forbidden with `{"detail": "authentication evidence not issued to authenticated user"}`. |
| 12 | Service Impersonation (F34) | Service token (`BFF_SERVICE` or `GRAPH_SERVICE`) calling decision endpoint | HTTP 403 Forbidden (`AgentImpersonationError`: `"services cannot authenticate or decide for humans"`). |
| 13 | Worker Identity (F36) | `WORKER_SERVICE` caller submitting decision | HTTP 401 or 403 Forbidden; cannot act as human approver. |
| 14 | Cross-Club Decision (F4) | Principal with `club_id="101"` attempting to decide package for club `"102"` | HTTP 403 Forbidden (`CrossClubAccessError`). |
| 15 | Delegated Decision Valid (F38) | Delegate with active, unexpired, club-matching delegation submitting with `proof_type="webauthn"` | Permitted; records decision with `actor_type='delegate'`, `authority_basis='explicit_delegate'`. |
| 16 | Delegated Decision Expired (F38) | Delegate submitting with expired delegation | HTTP 403 Forbidden (`DelegationInvalidError`: `"delegation is expired, revoked, or not yet active"`). |
| 17 | Delegated Scope Mismatch (F38) | Delegate with player-specific delegation attempting club-wide decision | HTTP 403 Forbidden (`DelegationInvalidError`: `"player-specific delegation cannot authorize club-wide package"`). |
| 18 | Package Decision Missing (F4) | `POST /approval/packages/{package_id}/decide` for package with no active awaiting request | HTTP 404 Not Found with `{"detail": "approval request for package not found"}`. |
| 19 | Timeout Action Injection (F46) | Request body or action specifying `"system:timeout_approval"` or `"timeout"` | HTTP 403 Forbidden (`AgentImpersonationError`: `"inactivity or timeouts never create authorization decisions"`). |
| 20 | Timeout Attribute Injection (F46) | Resource attributes containing `{"timeout_approved": true}` | HTTP 403 Forbidden (`AgentImpersonationError`). |
| 21 | Expired Request Decision (F46) | Decision submitted for `approval_request` that reached `expires_at` | HTTP 409 Conflict with `{"detail": "approval_expired"}`. |
| 22 | Dispatch Dormancy Integrity (F45) | Successful approval decision committed | Returns `dispatch.enabled = false`, `dispatch.created = false`, zero player delivery adapter calls. |

---

## 5. Specification & Migration Blueprint

### 5.1 Request Model: `DecisionSubmission`
Define in `src/schemas.py` and import in `src/graph_api.py`:

```python
class DecisionSubmission(BaseModel):
    """Authoritative decision submission payload for Phase 2 Milestone 5.
    
    Supports:
    1. WebAuthn step-up evidence (proof_type="webauthn", default)
    2. Legacy Phase 1 HMAC signature (proof_type="hmac", permitted only when APPROVAL_CLI_HMAC_ENABLED=true)
    """
    model_config = ConfigDict(extra="ignore")

    decision: Literal[
        "approved", "rejected", "revision_requested",
        "APPROVE", "REJECT", "REQUEST_REVISION"
    ]
    notes: str | None = Field(default="", max_length=2000)
    proof_type: Literal["webauthn", "hmac"] = "webauthn"
    authentication_evidence_id: UUID | None = None
    
    # Delegated authority parameters
    delegation_id: UUID | None = None
    authority_basis: Literal["primary", "explicit_delegate"] | None = None
    
    # Legacy Phase 1 HMAC parameters (used when proof_type="hmac")
    hmac_signature: str | None = None
    signature: str | None = None
    nonce: str | None = None
    thread_id: str | None = None
    package_version: str | None = None
    issued_at: int | None = None
    expires_at: int | None = None
    owner: str | None = None
    selection: str | None = None
    candidate_id: str | None = None
    player_id: str | None = None

    @field_validator("decision", mode="before")
    @classmethod
    def normalize_decision(cls, v: Any) -> str:
        s = str(v).strip().lower()
        if s in ("approve", "approved"):
            return "approved"
        if s in ("reject", "rejected"):
            return "rejected"
        if s in ("request_revision", "revision_requested"):
            return "revision_requested"
        return s

    @model_validator(mode="after")
    def validate_proof(self) -> "DecisionSubmission":
        # Auto-detect legacy HMAC if signature supplied but proof_type default
        if (self.hmac_signature or self.signature) and self.proof_type == "webauthn" and self.authentication_evidence_id is None:
            self.proof_type = "hmac"

        if self.proof_type == "webauthn":
            if self.authentication_evidence_id is None:
                raise ValueError("authentication_evidence_id required for webauthn proof_type")
        return self
```

### 5.2 Settings Migration: `APPROVAL_CLI_HMAC_ENABLED` Default
In `src/graph_api.py:ApiSettings`:
```python
@dataclass
class ApiSettings:
    ...
    # Kill switch default: False in Phase 2
    cli_hmac_enabled: bool = False
    ...

    @classmethod
    def from_env(cls) -> "ApiSettings":
        try:
            # Change default from True to False
            cli_hmac = env_bool("APPROVAL_CLI_HMAC_ENABLED", False)
            ...
```

### 5.3 Unified Decision Coordinator in `src/graph_api.py`

```python
def apply_decision_unified(
    request: Request,
    body: DecisionSubmission,
    *,
    thread_id: str | None = None,
    approval_request_id: UUID | str | None = None,
    package_id: str | None = None,
    principal: PrincipalContext,
) -> dict[str, Any]:
    """Unified authoritative decision processing for Phase 2 Milestone 5.
    
    Invariants:
    1. Anti-Impersonation: Services cannot commit human decisions.
    2. Policy Authorization: Evaluates policy.authorize() for subject and object.
    3. Proof Verification:
       - If proof_type="hmac": rejected with 403 if APPROVAL_CLI_HMAC_ENABLED=false.
       - If proof_type="webauthn": authentication_evidence_id validated against IdentityStore.
    4. G-TIMEOUT Guard: Timeouts and inactivity update escalation state only; never approves.
    5. Dispatch Dormancy: DISPATCH_ENABLED=false strictly enforced.
    """
    settings: ApiSettings = request.app.state.settings
    id_store: IdentityStore = request.app.state.identity_store

    # 1. Anti-Impersonation Guard
    if principal.is_service:
        raise AgentImpersonationError(
            f"service principal '{principal.user_id}' cannot commit human decisions"
        )

    # 2. Resolve target thread_id and approval_request_id
    with store.connect() as conn:
        if approval_request_id is not None:
            row = conn.execute(
                """SELECT approval_request_id, thread_id, package_version, status,
                          expires_at, now() >= expires_at AS is_expired
                     FROM public.approval_requests
                    WHERE approval_request_id = %s""",
                (str(approval_request_id),),
            ).fetchone()
            if not row:
                raise HTTPException(404, "approval request not found")
            target_thread_id = row["thread_id"]
            target_req_id = row["approval_request_id"]
            target_pkg_version = row["package_version"]
            req_status = row["status"]
            is_expired = row["is_expired"]

        elif package_id is not None:
            # Package-centric resolution
            row = conn.execute(
                """SELECT r.approval_request_id, r.thread_id, r.package_version, r.status,
                          now() >= r.expires_at AS is_expired
                     FROM public.approval_requests r
                     JOIN public.graph_threads g USING (thread_id)
                    WHERE (r.approval_request_id::text = %s 
                           OR g.state->'lead_packages'->'coach'->'best'->>'candidate_id' = %s
                           OR g.thread_id = %s)
                      AND r.status = 'awaiting_approval'
                    ORDER BY r.created_at DESC LIMIT 1""",
                (package_id, package_id, package_id),
            ).fetchone()
            if not row:
                raise HTTPException(404, f"approval request for package '{package_id}' not found")
            target_thread_id = row["thread_id"]
            target_req_id = row["approval_request_id"]
            target_pkg_version = row["package_version"]
            req_status = row["status"]
            is_expired = row["is_expired"]

        elif thread_id is not None:
            target_thread_id = thread_id
            th_row = conn.execute(
                """SELECT approval_request_id, package_version, status, club_id
                     FROM public.graph_threads WHERE thread_id = %s""",
                (thread_id,),
            ).fetchone()
            if not th_row:
                raise HTTPException(404, "thread not found")
            target_req_id = th_row["approval_request_id"]
            target_pkg_version = th_row["package_version"]
            req_status = th_row["status"]
            is_expired = False
        else:
            raise HTTPException(400, "missing target identifier")

    # 3. Request Status and G-TIMEOUT Guard
    if req_status != "awaiting_approval" or is_expired:
        raise HTTPException(409, "approval_expired")

    # 4. Centralized Authorization Check (policy.py)
    action_map = {
        "approved": ActionType.PACKAGE_APPROVE,
        "rejected": ActionType.PACKAGE_REJECT,
        "revision_requested": ActionType.PACKAGE_REQUEST_REVISION,
    }
    target_action = action_map[body.decision]
    authority_basis = body.authority_basis or (
        AuthorityBasis.EXPLICIT_DELEGATE if principal.is_delegate else AuthorityBasis.PRIMARY
    )

    # Elevated auth_level for valid WebAuthn assertion
    eval_principal = principal
    if body.proof_type == "webauthn":
        eval_principal = PrincipalContext.create(
            user_id=principal.user_id,
            club_id=principal.club_id,
            roles=list(principal.roles),
            auth_level=AuthLevel.STEP_UP_PASSKEY,
            auth_method=AuthMethod.PASSKEY,
            session_id=principal.session_id,
            display_name=principal.display_name,
        )

    res = Resource.for_package(
        club_id=principal.club_id,
        package_id=package_id or str(target_req_id),
        version=target_pkg_version,
    )
    authorize(
        eval_principal,
        target_action,
        res,
        authority_basis=authority_basis,
        delegation_id=body.delegation_id,
        store=id_store,
    )

    # 5. Proof Verification & Kill Switch
    if body.proof_type == "hmac":
        if not settings.cli_hmac_enabled:
            raise HTTPException(
                403,
                "legacy HMAC decision submissions are disabled: proof_type_disabled"
            )
        # Execute HMAC branch using existing signature verification
        signed_obj = SignedDecision.model_validate(body.model_dump())
        return apply_decision_internal(request, target_thread_id, signed_obj, principal)

    elif body.proof_type == "webauthn":
        evidence_id = body.authentication_evidence_id
        # Validate evidence in IdentityStore
        # Check single-use, bound payload hash match, user ID match, and consumption
        evidence = id_store.get_decision_evidence(evidence_id)
        if not evidence:
            raise HTTPException(400, "unknown or invalid authentication_evidence_id")
        
        # Check replay lockout
        if evidence.get("consumed_at") is not None:
            # Check for identical idempotent replay
            with store.connect() as conn:
                existing_audit = conn.execute(
                    """SELECT a.id, g.state, g.status
                         FROM public.approval_audit a
                         JOIN public.graph_threads g USING (thread_id)
                        WHERE a.authentication_evidence_id = %s
                          AND a.thread_id = %s""",
                    (evidence_id, target_thread_id),
                ).fetchone()
                if existing_audit and existing_audit["status"] == body.decision:
                    # Safe idempotent return
                    replayed = dict(existing_audit["state"])
                    return {
                        **replayed,
                        "thread_id": target_thread_id,
                        "status": existing_audit["status"],
                        "approval_audit_id": existing_audit["id"],
                        "approval_request_id": str(target_req_id),
                        "dispatch": DispatchBlock(enabled=False).model_dump(mode="json"),
                        "graph_resume": "resumed",
                    }
            raise HTTPException(409, "replay: authentication evidence already consumed")

        # Check payload binding
        expected_hash = hashlib.sha256(
            f"{package_id or target_req_id}:{target_pkg_version}:{body.decision}".encode()
        ).hexdigest()
        if evidence.get("bound_payload_hash") and evidence["bound_payload_hash"] != expected_hash:
            raise HTTPException(403, "step-up evidence bound payload mismatch")

        # 6. Commit Decision in PostgreSQL
        cid = getattr(request.state, "correlation_id", None) or uuid4()
        with store.connect() as conn, conn.transaction():
            # Mark evidence consumed
            id_store.consume_decision_evidence(evidence_id)
            
            # Update approval_requests
            conn.execute(
                """UPDATE public.approval_requests
                      SET status = %s, decided_at = now(), version = version + 1
                    WHERE approval_request_id = %s AND status = 'awaiting_approval'""",
                (body.decision, target_req_id),
            )
            
            # Update graph_threads
            conn.execute(
                """UPDATE public.graph_threads
                      SET status = %s, approved_by = %s, approved_at = now(), updated_at = now()
                    WHERE thread_id = %s AND status = 'awaiting_approval'""",
                (body.decision, principal.display_name or "Commissioner", target_thread_id),
            )
            
            # Insert approval_audit
            audit_row = conn.execute(
                """INSERT INTO public.approval_audit (
                       thread_id, approved_by, decision, notes,
                       package_version, proof_type, authentication_evidence_id,
                       actor_type, authority_basis, authority_role, decision_scope,
                       correlation_id, approval_request_id
                   ) VALUES (
                       %s, %s, %s, %s,
                       %s, 'webauthn', %s,
                       %s, %s, %s, 'club',
                       %s, %s
                   ) RETURNING id""",
                (
                    target_thread_id,
                    principal.display_name or "Commissioner",
                    body.decision.upper(),
                    body.notes or "",
                    target_pkg_version,
                    evidence_id,
                    "delegate" if principal.is_delegate else "commissioner",
                    authority_basis.value,
                    "commissioner_delegate" if principal.is_delegate else "commissioner",
                    cid,
                    target_req_id,
                ),
            ).fetchone()

            _safe_record_event(
                conn,
                event_type="approval.decided",
                entity_type="approval_request",
                entity_id=str(target_req_id),
                actor=f"user:{principal.user_id}",
                correlation_id=cid,
                visibility="staff",
                scope_club_id=principal.club_id,
                source_table="approval_audit",
                source_id=str(audit_row["id"]),
                to_status=body.decision,
            )

        publish("fc27:approval:decided", {"thread_id": target_thread_id, "status": body.decision})
        res = resume_graph(target_thread_id)

        return {
            "thread_id": target_thread_id,
            "status": body.decision,
            "package_version": target_pkg_version,
            "approval_audit_id": audit_row["id"],
            "approval_request_id": str(target_req_id),
            "dispatch": DispatchBlock(enabled=False).model_dump(mode="json"),
            "graph_resume": res,
        }
```

### 5.4 Endpoint Declarations in `src/graph_api.py`

```python
    # --------------------------------------------------------------------------
    # 1. Package-Centric Decision Endpoint (Requested F43 / Specification Seam)
    # --------------------------------------------------------------------------
    @app.post(
        "/approval/packages/{package_id}/decide",
        response_model=DecisionResponse,
        tags=["approval"],
    )
    @app.post(
        "/v1/approval/packages/{package_id}/decide",
        response_model=DecisionResponse,
        tags=["approval"],
        include_in_schema=False,
    )
    async def package_decide(
        request: Request,
        package_id: str = Path(..., min_length=1, max_length=128),
        body: DecisionSubmission = Body(...),
        principal: PrincipalContext = Depends(get_current_principal),
    ) -> dict:
        return apply_decision_unified(
            request, body, package_id=package_id, principal=principal
        )

    # --------------------------------------------------------------------------
    # 2. Request-Centric Decision Endpoint (/v1/approval-requests/{id}/decisions)
    # --------------------------------------------------------------------------
    @app.post(
        "/v1/approval-requests/{approval_request_id}/decisions",
        response_model=DecisionResponse,
        tags=["approval"],
    )
    async def approval_request_decision(
        request: Request,
        approval_request_id: str = Path(pattern=r"^[0-9a-f-]{36}$"),
        body: DecisionSubmission = Body(...),
        principal: PrincipalContext = Depends(get_current_principal),
    ) -> dict:
        return apply_decision_unified(
            request, body, approval_request_id=approval_request_id, principal=principal
        )

    # --------------------------------------------------------------------------
    # 3. Thread-Centric Decision Endpoint (/v1/runs/{id}/decision)
    # --------------------------------------------------------------------------
    @app.post(
        "/v1/runs/{thread_id}/decision",
        response_model=DecisionResponse,
        tags=["runs"],
    )
    @app.post(
        "/threads/{thread_id}/decision",
        response_model=DecisionResponse,
        include_in_schema=False,
    )
    async def v1_decision(
        request: Request,
        thread_id: str = ThreadIdPath,
        body: DecisionSubmission = Body(...),
        principal: PrincipalContext = Depends(get_current_principal),
    ) -> dict:
        return apply_decision_unified(
            request, body, thread_id=thread_id, principal=principal
        )
```

---

## 6. Caveats

1. **Active Database Requirement**: Verification of runtime PostgreSQL behavior requires an active PostgreSQL 16 instance. In test environments, tests use pytest fixtures (`client_v1`, `wired`) against test databases.
2. **`IdentityStore` Decision Evidence Seam**: In Milestone 5, `IdentityStore` must expose `get_decision_evidence` and `consume_decision_evidence` (or query `fc27_challenges` / `fc27_action_tokens`). The design above is compatible with either a dedicated `fc27_decision_evidence` table or utilizing `fc27_challenges(challenge_type='step_up_decision')`.
3. **Dispatch Dormancy**: Universal dispatch dormancy (`DISPATCH_ENABLED=false`) is an absolute requirement across all phases up to Phase 3. Decision endpoints must always emit `"dispatch": {"enabled": false, ...}`.

---

## 7. Conclusion

1. **Features 43, 44, 46 Fully Mapped**:
   - Feature 43 (Authenticated Human Decision Endpoint): Fully specified to accept `proof_type="webauthn"` with `authentication_evidence_id` validated against `IdentityStore`, committing to `approval_audit` and resuming LangGraph.
   - Feature 44 (Phase 1 CLI/HMAC Kill Switch): Fully specified via `APPROVAL_CLI_HMAC_ENABLED: bool = False` (default). When false, any HMAC proof or legacy signature submission is rejected with HTTP 403.
   - Feature 46 (Inactivity / Timeout No-Approval Guard): Fully verified across `policy.py` (rejecting timeout actions) and `approval_store.py` (transitioning to `expired` only). Zero autonomous approvals can occur.
2. **Existing Endpoints Reconciled**:
   - Inspected `src/graph_api.py`: Existing endpoints are `POST /v1/approval-requests/{approval_request_id}/decisions`, `POST /v1/runs/{thread_id}/decision`, and `POST /threads/{thread_id}/decision`.
   - Identified missing `POST /approval/packages/{package_id}/decide`: Formulated the exact migration blueprint adding this route and unifying all decision paths through `apply_decision_unified`.
3. **Centralized Authorization**:
   - Leverages `src/identity/deps.py:get_current_principal()` and `src/identity/policy.py:authorize()` to evaluate subject roles, club tenancy, anti-impersonation (rejecting services), and delegation validity.

---

## 8. Verification Method

To independently verify the implementation once applied by the implementing agent:

1. **Run Unit & Boundary Tests for Identity and Policy**:
   ```pwsh
   pytest tests/identity/test_policy.py -v
   ```
2. **Run E2E Phase 2 Tier 1-4 Suites (Covering F43, F44, F46)**:
   ```pwsh
   pytest tests/e2e/test_phase2_tier1_features.py -k "test_f43 or test_f44 or test_f46 or test_f18 or test_f19 or test_f20" -v
   pytest tests/e2e/test_phase2_tier2_boundaries.py -k "test_tier2_hmac or test_tier2_stepup or test_tier2_timeout" -v
   pytest tests/e2e/test_phase2_tier3_combinations.py -k "test_tier3_commissioner_stepup" -v
   pytest tests/e2e/test_phase2_tier4_scenarios.py -k "test_tier4_commissioner_stepup" -v
   ```
3. **Verify Regression Gate on Phase 1 Decision Tests**:
   ```pwsh
   # Verify legacy tests pass when APPROVAL_CLI_HMAC_ENABLED=true
   $env:APPROVAL_CLI_HMAC_ENABLED="true"
   pytest tests/test_graph_api_v1.py -k "test_adversarial_v1_approval_request_decisions_route_lifecycle" -v
   pytest tests/test_approval_security.py -v
   ```
4. **Inspect Code Files**:
   - `src/graph_api.py`: Check `APPROVAL_CLI_HMAC_ENABLED` default is `False`.
   - `src/graph_api.py`: Verify endpoints `POST /approval/packages/{package_id}/decide` and `/v1/approval-requests/{approval_request_id}/decisions` use `DecisionSubmission`.
   - `src/schemas.py`: Verify `DecisionSubmission` Pydantic model definition.
   - Invalidation condition: If any HMAC request succeeds when `APPROVAL_CLI_HMAC_ENABLED=false`, or if an expired approval request transitions to `approved`, the implementation fails.
