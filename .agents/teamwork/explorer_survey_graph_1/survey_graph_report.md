# Survey Report: LangGraph Core, Persistent Graph API, and Runtime State

**Date:** 2026-10-03  
**Agent:** explorer_survey_graph_1 (teamwork_preview_explorer)  
**Target Project:** FC27 Player Dispatch Programme — Phase 1: Secure Data Foundation  
**Working Directory:** `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_survey_graph_1`  
**Reference Document:** `ORIGINAL_REQUEST.md` (authoritative Phase 1 project brief)

---

## 1. Executive Summary

This survey provides an exhaustive read-only inspection of the LangGraph implementation (`src/graph.py`), persistent Graph API (`src/graph_api.py`), approval store and cryptographic modules (`src/approval.py`, `src/approval_store.py`, `src/decision.py`), event and outbox infrastructure (`src/events.py`, `migrations/0003_dispatch_foundation.sql`), and test harness (`tests/`).

### Core Findings
1. **Double Interruption Check:**
   - There is **no double interruption** in the codebase.
   - `interrupt_before=["approval"]` is **not present** anywhere in `src/graph.py` or compiled graph definitions.
   - Exactly **one** LangGraph approval interruption point exists: `interrupt(state["approval_request"])` within the `approval` node in `src/graph.py:143`.
2. **Resume Security & State Authority:**
   - Resuming via `Command(resume=...)` is **untrusted**: `src/graph.py` completely ignores the return value of `interrupt()` and re-reads PostgreSQL (`deps.decision_reader(state["thread_id"])`).
   - However, the validation performed on resume is currently incomplete against Phase 1 requirements: it checks only `package_version` and status (`approved`/`rejected`). It does **not** validate `approval_request_id`, package ID, deterministic package hash, or expiry from an authoritative `approval_requests` table.
3. **Severe Invariant Violation in `src/decision.py`:**
   - When a decision is approved, `src/decision.py:49-54` injects:
     ```python
     new_state["dispatch"] = {
         "owner": intent.owner.value,
         "selection": intent.selection.value,
         "candidate": candidate,
         "dispatched_to_player_agent": intent.player_id,
     }
     ```
     This directly violates Phase 1 Invariants #1, #2, and #4: *No Phase 1 path may deliver anything to a player; no Graph node may transition to dispatched, delivered, acknowledged, activated, or tracking.*
4. **Terminal State Copy:**
   - Terminal states are currently `"approved"` and `"rejected"`, with audit log summaries `"APPROVED"` and `"REJECTED"`.
   - The phrase `"APPROVED and ACTIVE"` is not in code, but the required Phase 1 terminal copy (`APPROVED, NOT DISPATCHED` and `REJECTED, NO ACTION TAKEN`) is not yet implemented.
5. **Persistent Graph API Discrepancies:**
   - Routes in `src/graph_api.py` are completely **unversioned** (`/run`, `/threads/{id}`, `/threads/{id}/decision-challenge`, `/threads/{id}/decision`, `/health`).
   - Phase 1 requires versioned `/v1/` routes: `POST /v1/runs`, `GET /v1/runs/{thread_id}`, `GET /v1/runs/{thread_id}/approval-request`, `POST /v1/approval-requests/{approval_request_id}/decisions`, `POST /v1/runs/{thread_id}/resume`, `GET /health/live`, `GET /health/ready`.
   - `GET /threads/{id}` currently leaks the entire raw `state` dictionary (including internal role reports) rather than returning a sanitized, authorized caller projection.
   - `DISPATCH_ENABLED` is missing from `src/graph_api.py` settings, responses, and startup validation.
6. **Data Foundation Progress:**
   - Tasks 1, 2, and 3 from the implementation plan (`docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md`) are partially in place on the branch (`src/db_roles.py`, `src/db_migrate.py`, `src/events.py`, and `migrations/0003_dispatch_foundation.sql` Section A).
   - The authoritative `approval_requests` table and lifecycle (Task 4) have not yet been migrated into PostgreSQL or integrated into the store.

---

## 2. LangGraph Core Survey (`src/graph.py`)

### 2.1 Graph Topology, Nodes, and Edges
- **StateGraph Definition (`src/graph.py:185-200`):**
  ```python
  g = StateGraph(FC27State)
  g.add_node("search", traced("search", search, "tool"))
  for role in ROLE_NODES:
      g.add_node(role, traced(role, role_node(role), "agent"))
  g.add_node("prepare_approval", traced("prepare_approval", prepare_approval))
  g.add_node("approval", traced("approval", approval, "guardrail"))
  g.add_node("finalize_decision", traced("finalize_decision", finalize_decision))
  
  g.add_edge(START, "search")
  g.add_conditional_edges("search", lambda s: END if s.get("status") == "failed" else "scout", ["scout", END])
  for a, b in zip(ROLE_NODES, ROLE_NODES[1:]):
      g.add_edge(a, b)
  g.add_edge(ROLE_NODES[-1], "prepare_approval")
  g.add_edge("prepare_approval", "approval")
  g.add_edge("approval", "finalize_decision")
  g.add_edge("finalize_decision", END)
  ```
- **Nodes Breakdown:**
  1. `search`: Calls MCP tool `get_club_dashboard` via `deps.mcp`. Fails to `END` if unhandled exception occurs.
  2. `scout`, `analyst`, `coach`, `tactics`, `director` (`ROLE_NODES`): Sequential agent execution using `deps.role_runner`. Produces schema-validated `RoleResult` reports and bounded score adjustments.
  3. `prepare_approval`: Calls `build_lead_packages()` (`src/lead_packages.py`), computes `package_version()`, prepares `approval_request` dictionary, sets `status="awaiting_approval"`.
  4. `approval`: Calls `interrupt(state["approval_request"])`. On resume, loads committed decision from PostgreSQL.
  5. `finalize_decision`: Logs audit record and passes state to `END`.

### 2.2 State Schema (`FC27State`)
Defined in `src/graph.py:44-66`:
```python
class FC27State(TypedDict, total=False):
    thread_id: str
    club_id: str
    fc_version: str
    platform: str
    trigger: str
    run_seq: int
    match: dict | None
    dashboard: dict
    candidates: list[dict]
    role_reports: dict[str, dict]
    lead_packages: dict[str, dict]
    package_version: str | None
    approval_request: dict
    approval_decision: dict
    status: str
    errors: Annotated[list[str], operator.add]
    model_ids: dict[str, str]
    prompt_version: str
    policy_version: str
    code_revision: str
    audit_log: Annotated[list[dict], operator.add]
```
- **Observations:**
  - `errors` and `audit_log` use reducer `operator.add`.
  - State does not currently contain structured identifiers for `approval_request_id` or `package_id`.

### 2.3 Interruption Mechanism
- **Verification:**
  - Checked `compile()` in `src/graph.py:214`:
    `self.app = build_graph(deps).compile(checkpointer=checkpointer)`
  - No `interrupt_before` or `interrupt_after` argument is passed.
  - Exactly one call to `interrupt()` occurs at `src/graph.py:143`:
    ```python
    def approval(state: FC27State) -> dict:
        interrupt(state["approval_request"])
        row = deps.decision_reader(state["thread_id"])
        ...
    ```
  - **Conclusion:** Double interruption does not exist in the code.

### 2.4 Recommendation Package Creation & Persistence
- Currently, in `prepare_approval` (`src/graph.py:130-140`):
  ```python
  results = {k: RoleResult.model_validate(v) for k, v in (state.get("role_reports") or {}).items()}
  packages = build_lead_packages(state.get("dashboard", {}), state["club_id"], results, state["thread_id"])
  version = package_version(packages)
  approvable = sorted(k for k, v in packages.items() if v.get("approvable"))
  request = {"thread_id": state["thread_id"], "package_version": version, "approvable_owners": approvable,
             "fc_version": FC_VERSION, "platform": state["platform"]}
  ```
- **Gaps against Section 4.2 of `ORIGINAL_REQUEST.md`:**
  - Packages are NOT persisted to an authoritative, immutable `recommendation_packages` database table before interruption.
  - Persistence only occurs after the graph pauses, via `ApprovalStore.complete_run()`, which dumps `state["lead_packages"]` into `graph_threads.state` (JSONB).
  - No `approval_requests` row is created in PostgreSQL before interruption.
  - No `domain_events` row (`approval.created`) is written before interruption.

### 2.5 Approval Interrupt Payload
- Currently emitted (`src/graph.py:135-136`):
  ```python
  {"thread_id": state["thread_id"], "package_version": version, "approvable_owners": approvable,
   "fc_version": FC_VERSION, "platform": state["platform"]}
  ```
- **Gaps against Section 4.3 of `ORIGINAL_REQUEST.md`:**
  - Missing `approval_request_id`.
  - Missing `package_id`.
  - Missing deterministic `package_hash`.
  - Missing `safe summary`.
  - Missing `required approving authority`.

### 2.6 Resume Semantics & Validation
- **Current Resume Execution (`src/graph.py:141-155, 244-254`):**
  - Graph API or test calls `runner.resume(thread_id)`.
  - `runner.resume()` executes `self.app.invoke(Command(resume={"thread_id": thread_id}), ...)`.
  - Inside `approval(state)`:
    ```python
    interrupt(state["approval_request"])
    row = deps.decision_reader(state["thread_id"])
    if not row or row.get("status") not in ("approved", "rejected"):
        raise ApprovalNotCommitted("no committed Commissioner decision for this thread")
    payload = row.get("decision_payload") or {}
    if payload.get("package_version") != state.get("package_version"):
        raise ApprovalNotCommitted("committed decision refers to a different package version")
    ```
  - **Strengths:**
    - The value returned by `interrupt()` is ignored.
    - An injected `Command(resume={"status": "approved"})` or forged state cannot authorize the run.
    - PostgreSQL is re-read.
  - **Gaps against Section 4.4 of `ORIGINAL_REQUEST.md`:**
    - Checks only `row["status"]` and `package_version`.
    - Does not validate that an authoritative `approval_requests` record matches and remains valid (unexpired, not cancelled).
    - Does not validate `package_id` or `package_hash`.
    - When validation fails, raising `ApprovalNotCommitted` leaves the LangGraph task in an exception state rather than transitioning cleanly to a fail-closed auditable state.
    - Terminal states returned are `"approved"` or `"rejected"`, not `"APPROVED, NOT DISPATCHED"` or `"REJECTED, NO ACTION TAKEN"`.

---

## 3. Persistent Graph API Survey (`src/graph_api.py`)

### 3.1 Route Inventory & Path Discrepancy

| Expected Route (`ORIGINAL_REQUEST.md`) | Current Route in `src/graph_api.py` | Role / Auth | Status / Gap |
|---|---|---|---|
| `POST /v1/runs` | `POST /run` (line 194) | `Role.service` | Unversioned; lacks `dispatch_enabled=false` in response; does not accept `X-Correlation-Id`. |
| `GET /v1/runs/{thread_id}` | `GET /threads/{thread_id}` (line 219) | `Role.service` | Unversioned; leaks full internal `state` dict (intermediate role reports, prompt versions, etc.) instead of safe caller projection; lacks `dispatch_enabled=false`. |
| `GET /v1/runs/{thread_id}/approval-request` | *None* | `Role.service` | **Missing completely.** |
| `POST /v1/approval-requests/{approval_request_id}/decisions` | `POST /threads/{thread_id}/decision` (line 236) | `Role.commissioner` | Keyed by `thread_id`, not `approval_request_id`; requires prior `decision-challenge` nonce call; does not write to `domain_events`/`outbox`. |
| `POST /v1/runs/{thread_id}/resume` | *None* | `Role.service` (internal) | **Missing completely.** Resuming is only an automatic side effect of `POST /threads/{id}/decision` or startup reconciliation. |
| `GET /health/live` | *None* (only `GET /health`) | Public | Liveness-only endpoint missing. |
| `GET /health/ready` | *None* (only `GET /health`) | Public | Readiness checking DB, checkpointer, posture, and `DISPATCH_ENABLED=false` missing. |
| `GET /v1/config/dispatch` | *None* | `Role.service` / `commissioner` | Missing dispatch config & gate inspection endpoint. |

### 3.2 Security and Access Control (`src/security.py`)
- Authentication mechanism:
  - Header: `X-API-Key`
  - Authenticator verifies against:
    1. `GRAPH_SERVICE_TOKEN` (service role)
    2. `GRAPH_COMMISSIONER_TOKEN_SHA256` (commissioner role, constant-time hash comparison)
- Principal resolution:
  - Service requests obtain `Principal(Role.service, "service")`.
  - Commissioner requests obtain `Principal(Role.commissioner, settings.commissioner_id)`.
- Evaluation:
  - Secure credential separation; no passwords or secret tokens logged.
  - Missing Phase 1 middleware for `X-Correlation-Id` validation and propagation (`src/correlation.py`).

### 3.3 Graph API Runtime Configuration (`ApiSettings`)
- Located in `src/graph_api.py:34-63`:
  ```python
  @dataclass
  class ApiSettings:
      postgres_uri: str
      authenticator: Authenticator
      signing_secret: str
      approval_ttl_seconds: int = 120
      redis_url: str | None = None
      run_migrations: bool = False
      checkpoint_setup: bool = False
      enforce_db_posture: bool = True
  ```
- **Gaps:**
  - `dispatch_enabled: bool = False` (`DISPATCH_ENABLED`) is **missing** from `ApiSettings`.
  - `approval_request_ttl: timedelta = timedelta(hours=72)` is missing.
  - `cli_hmac_enabled: bool = True` (kill switch) is missing.
  - `code_revision` and `deployment_id` settings are missing.

---

## 4. Supporting Modules & Stores Survey

### 4.1 Decision Application & Invariant Violation (`src/decision.py`)
Lines 42-65:
```python
def apply_decision_to_state(state: dict[str, Any], intent: DecisionIntent, approved_by: str, decided_at: str) -> dict[str, Any]:
    if state.get("status") != "awaiting_approval":
        raise DecisionConflict("thread is not awaiting approval")
    candidate = resolve_candidate(state, intent)
    new_state = copy.deepcopy(state)
    approved = intent.decision is DecisionKind.APPROVE
    if approved:
        new_state["dispatch"] = {
            "owner": intent.owner.value,
            "selection": intent.selection.value,
            "candidate": candidate,
            "dispatched_to_player_agent": intent.player_id,
        }
    new_state.update(
        approved=approved,
        status="approved" if approved else "rejected",
        approved_by=approved_by,
        commissioner_input=intent.notes,
    )
    new_state["audit_log"] = list(new_state.get("audit_log", [])) + [
        {"node": "approval", "ts": decided_at, "summary": "APPROVED" if approved else "REJECTED", "approved_by": approved_by}
    ]
    return new_state
```
- **Analysis:**
  - Injecting `"dispatched_to_player_agent"` directly simulates player dispatch.
  - In Phase 1, `state["dispatch"]` must be completely eliminated. Approval must record selection only and set terminal status to `"approved"` (with terminal wording `APPROVED, NOT DISPATCHED`).

### 4.2 Approval Store (`src/approval_store.py`)
- **Current Flow in `apply_decision()` (lines 162-236):**
  1. Checks `principal.role is Role.commissioner`.
  2. Verifies HMAC-SHA256 signature using `approval.verify_signature()`.
  3. Begins database transaction.
  4. Locks `approval_nonces` row `FOR UPDATE`. Verifies unconsumed, not expired, digest match.
  5. Locks `graph_threads` row `FOR UPDATE`. Verifies `status = 'awaiting_approval'`, `package_version` matches.
  6. Computes `new_state = apply_decision_to_state(...)`.
  7. Updates `approval_nonces` setting `consumed_at = now()`.
  8. Updates `graph_threads` setting `status = new_state["status"]`, `state = Jsonb(new_state)`, `approved_by`, `approved_at`, `decision_payload`.
  9. Inserts into `approval_audit`.
- **Gaps against Phase 1 Requirements:**
  - Table `approval_requests` does not exist and is not locked/updated.
  - Lock order does not follow global specification order: `nonces` -> `approval_requests` -> `graph_threads`.
  - Expiry is only checked for the short-lived challenge nonce (300 s max), not for the approval request lifecycle (72 h TTL).
  - No `domain_events` or `outbox` records are inserted via `record_event()`.

### 4.3 Database Schema & Migrations
- `migrations/0001_initial.sql`:
  - `graph_threads`, `approval_audit`.
- `migrations/0002_approval_security.sql`:
  - `graph_threads` columns: `club_id`, `request_hash`, `package_version`, `created_at`, `approved_by`, `approved_at`, `decision_payload`.
  - Trigger `graph_threads_guard`: blocks updates/deletes to terminal rows.
  - `approval_nonces` table.
  - `run_sequences` table.
- `migrations/0003_dispatch_foundation.sql` (In progress):
  - Section A added: `public.domain_events`, `public.outbox`, triggers `fc27_reject_mutation` and `fc27_check_event_source`.
  - Missing Sections:
    - Section B: `approval_requests` table, `graph_threads.approval_request_id`, status check extensions (`revision_requested`, `expired`), constraint triggers `fc27_approval_pair_check`, consistency view `fc27_approval_inconsistencies`.
    - Section C: `players`, `dispatches`, objective bounds, immutable provenance triggers.
    - Section D: `fc27_dispatch_transition` SECURITY DEFINER function.
    - Section E: `activation_gate_log`, `service_heartbeats`.

---

## 5. Existing Test Suite Survey

### 5.1 Test Files and Coverage

| Test File | Count | Current Scope & Passing Status |
|---|---|---|
| `tests/test_langgraph_flow.py` | 12 | Tests typed state, interruption, resume, resume bypass rejection, terminal thread immutability, checkpoint metadata (`fc_version="27"`, `durability="sync"`), startup reconciliation, role ranking. |
| `tests/test_approval_security.py` | 30 | Tests thread ID convention, terminal protection trigger, 401/403 authorization, nonces (single-use, thread binding, expiration, tampering), concurrent approval races, atomic rollback on audit failure. |
| `tests/test_events_outbox.py` | 12 | Tests `record_event` inserting into `domain_events` and `outbox`, append-only triggers, and event source check triggers. |
| `tests/test_db_roles.py` | 7 | Tests cluster bootstrap, non-superuser role attributes, public create revocation, legacy ownership transfer, conninfo redaction. |
| `tests/test_db_migrate_roles.py` | 15 | Tests migration runner under `fc27_owner`, advisory lock, app role unable to run DDL, checkpointer setup privilege separation, grants policy checksum. |
| `tests/test_compose_isolation.py` | 17 | Static checks on Docker Compose secret isolation, network boundaries, loopback binds, image pins. |
| `tests/test_workflow.py` | 6 | Tests candidate ranking, tier sets, signature canonicalization and verification. |

### 5.2 Test Fixtures & Mocks (`tests/conftest.py`, `tests/helpers.py`)
- Real PostgreSQL testing via `pg_db` and `pg_db_migrated`:
  - Spins up disposable schemas on `TEST_POSTGRES_URI`.
  - `pg_uri` connects strictly as `fc27_app` (least-privilege DML role).
  - `pg_admin_db_uri` connects as administrative role for fault-injection tests.
- `wired` fixture in `tests/conftest.py`:
  - Sets up `GraphRunner` with `PostgresSaver` and `FastAPI` test client.
  - Uses `FakeRoles` and stubbed MCP dashboard for deterministic graph runs.
- `StubPipeline` in `tests/helpers.py`:
  - Allows testing `src/graph_api.py` HTTP layer without running full LangGraph state engine.

---

## 6. Gap Analysis Against Phase 1 Requirements

### 6.1 Agent B Requirements: LangGraph Core

| Requirement (ORIGINAL_REQUEST §4) | Codebase Status | Gap & Required Remedy |
|---|---|---|
| **4.1 Remove double interruption** | **Satisfied** | Only one `interrupt()` call exists in `approval` node. No `interrupt_before=["approval"]` exists. |
| **4.2 Recommendation package creation before interrupt** | **Deficient** | Packages are not stored in PostgreSQL before interrupt; `approval_requests` row is not created; no `approval.created` domain event is recorded. Must persist package, compute hash, create approval request, and record event before pausing. |
| **4.3 Safe interrupt payload** | **Deficient** | Payload lacks `approval_request_id`, `package_id`, `package_hash`, display-safe summary, and required authority. |
| **4.4 Resume re-reads PostgreSQL & validates** | **Partially Satisfied** | Resume re-reads PostgreSQL and ignores resume body, but checks only `package_version`. Must validate against authoritative `approval_requests` row (request ID, version, hash, expiry). |
| **4.5 Terminal Graph states** | **Deficient** | States are `"approved"` / `"rejected"` instead of `"APPROVED, NOT DISPATCHED"` / `"REJECTED, NO ACTION TAKEN"`. Must eliminate `state["dispatch"]` in `src/decision.py`. |
| **4.6 Failure ordering** | **Partially Satisfied** | Decision commits before Graph resume is requested. Startup reconciliation handles crashed resumes. However, `domain_events` write is not yet part of the transaction. |

### 6.2 Agent C Requirements: Persistent Graph API

| Requirement (ORIGINAL_REQUEST §5) | Codebase Status | Gap & Required Remedy |
|---|---|---|
| **Versioned routes (`/v1/`)** | **Deficient** | All endpoints are unversioned (`/run`, `/threads/{id}`). Must implement `/v1/` routes. |
| **5.1 `POST /v1/runs`** | **Deficient** | Unversioned; does not return `dispatch_enabled=false`. |
| **5.2 `GET /v1/runs/{thread_id}`** | **Deficient** | Leaks raw internal state dict; must return sanitized projection (`thread_id`, `status`, `current_node`, `package_version`, `approval_request_id`, `safe_errors`, `dispatch_enabled=false`). |
| **5.3 `GET /v1/runs/{thread_id}/approval-request`** | **Missing** | Route does not exist. |
| **5.4 `POST /v1/approval-requests/{approval_request_id}/decisions`** | **Deficient** | Endpoint is `POST /threads/{thread_id}/decision`. Needs to be keyed by `approval_request_id`, lock request, validate exact hash, and emit domain events. |
| **5.5 `POST /v1/runs/{thread_id}/resume`** | **Missing** | Dedicated restricted resume trigger route does not exist. |
| **5.6 `GET /health/live` & 5.7 `GET /health/ready`** | **Deficient** | Only legacy `/health` exists. Liveness and readiness separation required; readiness must verify `DISPATCH_ENABLED=false` and DB posture. |
| **5.8 Restrictive service access policy** | **Satisfied** | Authenticator enforces `service` vs. `commissioner` roles via constant-time token comparisons. |

---

## 7. Actionable Implementation Recommendations

### For Agent B (Graph Core):
1. **Refactor `prepare_approval` Node:**
   - Compute canonical package and deterministic package hash.
   - Insert package into database and create bound `approval_requests` record (or call store helper) before calling `interrupt()`.
   - Record `approval.created` domain event in outbox.
   - Pass safe interrupt payload: `thread_id`, `approval_request_id`, `package_id`, `package_version`, `package_hash`, safe summary, and required authority.
2. **Refactor `approval` Node:**
   - Re-read PostgreSQL record from `approval_requests` joined with `approval_audit`.
   - Verify `package_version`, `package_hash`, approval request validity, and decision value.
   - If uncommitted, expired, or invalid, fail closed without crashing LangGraph task.
   - Set terminal state to `"APPROVED, NOT DISPATCHED"` or `"REJECTED, NO ACTION TAKEN"`.
3. **Cleanse `src/decision.py`:**
   - Completely delete lines setting `new_state["dispatch"] = {"dispatched_to_player_agent": ...}`.

### For Agent C (Persistent Graph API):
1. **Add Versioned Routes to `src/graph_api.py`:**
   - Mount `/v1/runs`, `/v1/runs/{thread_id}`, `/v1/runs/{thread_id}/approval-request`, `/v1/approval-requests/{approval_request_id}/decisions`, and `/v1/runs/{thread_id}/resume`.
   - Keep legacy unversioned routes as deprecated redirects or aliases if needed for test backward compatibility.
2. **Implement Safe Run Projection:**
   - Create Pydantic projection model for `GET /v1/runs/{thread_id}` that explicitly excludes secrets, raw LLM transcripts, and checkpoint internals.
3. **Implement Health Endpoints:**
   - Add `/health/live` returning process status 200.
   - Add `/health/ready` verifying PostgreSQL connectivity, checkpointer state, runtime database role posture, and `DISPATCH_ENABLED=false`.
4. **Configuration Hardening:**
   - Add `dispatch_enabled: bool = False` to `ApiSettings`, ensuring `DISPATCH_ENABLED=false` is enforced at all times.

---

*Report compiled by explorer_survey_graph_1.*
