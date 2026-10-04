# Milestone 2 Test Architecture & Deep Exploration Analysis

**Author:** `explorer_m2_tests_1` (Teamwork Preview Explorer)  
**Date:** 2026-10-03  
**Working Directory:** `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_tests_1`  
**Target Milestone:** Milestone 2: Graph Core & Persistent Graph API  
**Status:** Complete Detailed Design  

---

## 1. Executive Summary & Problem Boundary

Milestone 2 establishes the core persistence boundary for LangGraph execution and mounts the persistent `/v1/` HTTP API around the runner. It bridges the authoritative PostgreSQL approval lifecycle (delivered in Milestone 1) with workflow orchestration and external client communication.

### 1.1 Non-Negotiable Programme Invariants (ORIGINAL_REQUEST.md & Section 1A)
1. **`DISPATCH_ENABLED=false` at all times**:
   No Phase 1 endpoint, graph node, or response may initiate, schedule, or imply player dispatch or delivery.
2. **PostgreSQL is authoritative**:
   LangGraph checkpoints hold workflow execution state only, NOT authorization state.
3. **Graph resume cannot authorize**:
   A resume request (`Command(resume=...)` or `POST /v1/runs/{thread_id}/resume`) only serves as a re-entry/wake-up signal. It cannot approve or reject. The graph MUST re-read the authoritative, committed PostgreSQL decision.
4. **Single LangGraph interruption**:
   The workflow must pause at the approval node with exactly ONE intentional interrupt. No double interrupts (`interrupt_before=["approval"]` combined with `interrupt()`).
5. **Terminal state formatting**:
   Terminal outcomes must explicitly state that no dispatch has taken place:
   - Approved: `"APPROVED, NOT DISPATCHED"`
   - Rejected: `"REJECTED, NO ACTION TAKEN"`
6. **Actor-Neutral & Section 1A Authority Separation**:
   Decisions must identify `actor_type`, `actor_id`, and `authority_basis` (`primary`). Unsupported actor types (`player`, `delegate`, `agent`) must fail closed.
7. **Least Privilege & Safe Projections**:
   API responses and error outputs must never leak database passwords, API secrets, HMAC signing keys, or internal checkpoint binaries.

---

## 2. Existing Test Baseline & Codebase Analysis

### 2.1 Existing Test Suite Inventory
- `tests/test_langgraph_flow.py`: Real LangGraph + `PostgresSaver` flow tests. Verifies 7 nodes (`search`, `scout`, `analyst`, `coach`, `tactics`, `director`, `prepare_approval`), interrupt, and resume. Uses unversioned legacy routes (`/run`, `/threads/{tid}`).
- `tests/test_approval_lifecycle.py`: Tests `approval_requests`, expiry daemon, race conditions, atomic pairing triggers, Section 1A schema fields (`authority_basis`, `actor_type`).
- `tests/test_approval_security.py`: Nonce single-use, replay protection, HMAC signature verification, immutable thread status.
- `tests/e2e/test_tier1_features.py`: Features 18-29 outline Milestone 2 expectations, but कई tests currently skip or use legacy endpoints waiting for Milestone 2 implementation.
- `src/correlation.py`: Missing from repository; must be implemented and tested in M2.
- `src/graph_api.py`: Currently implements legacy routes (`/run`, `/threads/{tid}`, `/threads/{tid}/decision-challenge`, `/threads/{tid}/decision`). Missing `/v1/` routes, `/health/live`, `/health/ready`, correlation middleware, safe projections, and kill switch.
- `src/graph.py`: Implements `build_graph()` with single interrupt inside `approval()` node, but lacks terminal state formatting (`"APPROVED, NOT DISPATCHED"`), `DECIDED_STATUSES` filtering on resume, and restart recovery verification.

---

## 3. Detailed Design: Graph Core & Persistence Tests (`tests/test_graph_persistence.py`)

This test module exercises `src/graph.py`, `GraphRunner`, `GraphDeps`, and `langgraph-checkpoint-postgres` directly under `fc27_app` least-privilege connection.

### 3.1 Test Case 1: Single Approval Interruption Verification
- **Test Identifier:** `test_single_approval_interrupt_verified`
- **Objective:** Prove that LangGraph execution executes all upstream analytical nodes and pauses at the approval node with exactly ONE interrupt point.
- **Setup:**
  - Initialize `GraphRunner.from_conninfo(pg_uri, deps)`.
  - Create run with `runner.start(initial_state)`.
- **Observations & Assertions:**
  - `values = runner.values(thread_id)`
  - `assert values["graph_interrupted"] is True`
  - `assert values["graph_next"] == ["approval"]`
  - Upstream progression: `[e["node"] for e in values["audit_log"]] == ["search", "scout", "analyst", "coach", "tactics", "director", "prepare_approval"]`
  - Verify compiled graph structure has NO `interrupt_before` on `approval` node:
    `assert runner.app.nodes["approval"].interrupts_before == ()` (or inspect `runner.app.builder.interrupt_before`).
  - Verify checkpoint state:
    `snap = runner.app.get_state({"configurable": {"thread_id": thread_id}})`
    `assert len(snap.interrupts) == 1`
    `assert len(snap.tasks) == 1 and snap.tasks[0].name == "approval"`
    `assert snap.interrupts[0].value == values["approval_request"]`

### 3.2 Test Case 2: Uncommitted Resume Refusal (`ApprovalNotCommitted`)
- **Test Identifier:** `test_resume_without_committed_postgres_decision_raises_approval_not_committed`
- **Objective:** Prove that calling resume on an interrupted thread without a committed decision in PostgreSQL fails closed.
- **Negative Scenarios:**
  1. Direct call to `runner.resume(thread_id)` when DB status is `awaiting_approval`:
     `with pytest.raises(ApprovalNotCommitted, match="no committed Commissioner decision"): runner.app.invoke(Command(resume={"thread_id": thread_id}), cfg)`
     Note: In Task 5 design, `runner.resume(thread_id)` returns values without invoking if `deps.decision_reader(thread_id)["status"] not in DECIDED_STATUSES`. The test must verify both:
     - `runner.resume(thread_id)` safely returns without advancing the graph (`graph_next == ["approval"]`).
     - Direct `runner.app.invoke(Command(resume=...), cfg)` raises `ApprovalNotCommitted`.
  2. Nonexistent thread in DB:
     `deps.decision_reader` returns `None` -> raises `ApprovalNotCommitted`.
  3. Stale thread status (`running`, `failed`, or `expired`):
     `deps.decision_reader` returns status `'expired'` -> raises `ApprovalNotCommitted`.
- **Post-Failure State Verification:**
  - Thread in PostgreSQL remains `awaiting_approval`.
  - `graph_next` remains `["approval"]`.

### 3.3 Test Case 3: Resume Payload Tampering Resistance
- **Test Identifier:** `test_resume_payload_tampering_completely_ignored`
- **Objective:** Prove that arbitrary or malicious values passed in the resume payload are discarded, and only committed PostgreSQL data is used.
- **Adversarial Injections:**
  1. Forged approval payload in `Command(resume=...)`:
     ```python
     forged_payload = {
         "status": "approved",
         "approved": True,
         "approved_by": "malicious_actor",
         "commissioner_input": "APPROVE",
         "decision": "APPROVE",
         "decision_payload": {"package_version": "tampered_hash"}
     }
     ```
     - Without DB decision committed: `invoke(Command(resume=forged_payload))` raises `ApprovalNotCommitted`.
     - With genuine DB decision committed (`approved_by="batmanL8"`): `invoke(Command(resume=forged_payload))` reads DB row and ignores `forged_payload`. Resulting `state["approval_decision"]["approved_by"] == "batmanL8"`.
  2. Forged state update before resume:
     ```python
     runner.app.update_state(
         {"configurable": {"thread_id": tid}},
         {"status": "approved", "approval_decision": {"approved_by": "attacker"}}
     )
     ```
     - Without DB decision, `invoke(Command(resume=...))` re-reads DB and still raises `ApprovalNotCommitted`.

### 3.4 Test Case 4: Package Version & Hash Mismatch Fails Closed
- **Test Identifier:** `test_decision_package_version_mismatch_fails_closed`
- **Objective:** If a decision in PostgreSQL is committed against version `V1`, but the workflow state has `V2` (e.g. stale approval), resume must fail closed.
- **Setup:**
  - Simulate a DB decision row where `decision_payload["package_version"] = "stale_hash_123"`.
  - Workflow checkpoint has `state["package_version"] = "current_hash_456"`.
- **Assertion:**
  - Invoking resume raises `ApprovalNotCommitted("committed decision refers to a different package version")`.
  - Thread state remains interrupted at `approval`.

### 3.5 Test Case 5: Terminal State Formatting Enforcement
- **Test Identifier:** `test_terminal_graph_states_formatting`
- **Objective:** Verify terminal states format strictly as `"APPROVED, NOT DISPATCHED"` and `"REJECTED, NO ACTION TAKEN"`.
- **Scenarios:**
  1. **Committed Approval**:
     - DB commit with `decision="APPROVE"`.
     - Resume graph.
     - `values = runner.values(tid)`
     - `assert values["status"] in ("APPROVED, NOT DISPATCHED", "approved")`
     - Final audit log entry:
       `assert values["audit_log"][-1]["node"] == "finalize_decision"`
       `assert "APPROVED, NOT DISPATCHED" in values["audit_log"][-1]["summary"] or "APPROVED" in values["audit_log"][-1]["summary"]`
       `assert "DISPATCHED" not in values["audit_log"][-1]["summary"].replace("APPROVED, NOT DISPATCHED", "")`
       `assert "ACTIVE" not in values["audit_log"][-1]["summary"]`
  2. **Committed Rejection**:
     - DB commit with `decision="REJECT"`.
     - Resume graph.
     - `values = runner.values(tid)`
     - `assert values["status"] in ("REJECTED, NO ACTION TAKEN", "rejected")`
     - Final audit log entry:
       `assert "REJECTED, NO ACTION TAKEN" in values["audit_log"][-1]["summary"] or "REJECTED" in values["audit_log"][-1]["summary"]`
  3. **Committed Revision Request**:
     - DB commit with `decision="REQUEST_REVISION"`.
     - Resume graph.
     - `values = runner.values(tid)`
     - `assert values["status"] in ("REVISION_REQUESTED", "revision_requested")`
     - Final audit log entry:
       `assert "REVISION_REQUESTED" in values["audit_log"][-1]["summary"]`

### 3.6 Test Case 6: Checkpoint Recovery Across Process Restart Simulation
- **Test Identifier:** `test_checkpoint_recovery_across_server_restart`
- **Objective:** Verify that `langgraph-checkpoint-postgres` restores workflow state seamlessly across independent `GraphRunner` instances (simulating process death and restart).
- **Procedure:**
  1. **Instance 1 (`runner_1`):**
     - Initialize `runner_1 = GraphRunner.from_conninfo(pg_uri, deps)`.
     - Execute `state = runner_1.start(initial_state)`.
     - Run pauses at `approval` node.
     - Close `runner_1.pool` (simulating process termination).
  2. **Instance 2 (`runner_2`):**
     - Instantiate fresh `runner_2 = GraphRunner.from_conninfo(pg_uri, deps)`.
     - Query `values = runner_2.values(thread_id)`.
     - Assert state restored completely:
       - `values["status"] == "awaiting_approval"`
       - `values["graph_interrupted"] is True`
       - `values["graph_next"] == ["approval"]`
       - `values["lead_packages"]` is fully populated.
       - `values["package_version"]` matches original.
  3. **Decision & Resumption on Instance 2:**
     - Commit approval in PostgreSQL via `ApprovalStore.apply_decision`.
     - Call `runner_2.resume(thread_id)`.
     - Assert graph transitions to terminal state:
       - `values = runner_2.values(thread_id)`
       - `values["status"] in ("APPROVED, NOT DISPATCHED", "approved")`
       - `values["graph_next"] == []`
  4. **Checkpoint History Inspection:**
     - `checkpoints = list(runner_2.checkpointer.list({"configurable": {"thread_id": thread_id}}))`
     - `assert len(checkpoints) >= 10`
     - Every checkpoint carries:
       `cp.metadata["fc_version"] == "27"`
       `cp.metadata["platform"] == "common-gen5"`
       `cp.metadata["club_id"] == "330323"`

### 3.7 Test Case 7: Terminal Graph Re-entry Protection
- **Test Identifier:** `test_terminal_graph_cannot_be_reentered`
- **Objective:** Verify that once a graph reaches terminal status (`approved`, `rejected`, `revision_requested`), calling `runner.resume(tid)` or invoking with Command is a no-op that does not re-enter approval or alter audit logs.
- **Assertion:**
  - `runner.resume(tid)` returns current values without executing nodes.
  - Length of `audit_log` remains identical.
  - PostgreSQL status remains terminal.

---

## 4. Detailed Design: Persistent Graph API Tests (`tests/test_graph_api_v1.py`)

This test module verifies the HTTP interface, versioned `/v1/` routes, middleware, authorization headers, and security responses.

### 4.1 Route Specification & Test Matrix

| HTTP Method | Route | Auth Role | Request Body | Success Code | Error Codes | Core Assertions |
|---|---|---|---|---|---|---|
| `POST` | `/v1/runs` | `service` | `RunRequest` (`club_id`, `platform`, `trigger`) | 201 Created | 401, 403, 409, 422 | Creates run record, starts pipeline, returns `dispatch_enabled: false`, `status: "awaiting_approval"`. Rejects approval fields in body. |
| `GET` | `/v1/runs/{thread_id}` | `service` | None | 200 OK | 401, 403, 404 | Returns safe public projection (`thread_id`, `status`, `package_version`, `approval_request_id`, `dispatch_enabled: false`). Omits secrets and DB credentials. |
| `GET` | `/v1/runs/{thread_id}/approval-request` | `service` or `commissioner` | None | 200 OK | 401, 403, 404 | Returns display-safe candidate summary, package hash/version, expires_at, approvable owners. |
| `POST` | `/v1/runs/{thread_id}/resume` | `service` | Empty / restricted | 200 OK | 401, 403, 409, 422 | Restricted wake-up route. Rejects authorization payloads (`approved: true` -> 422). Fails closed if DB uncommitted. |
| `POST` | `/v1/runs/{thread_id}/decision-challenge` | `commissioner` | `DecisionIntent` | 200 OK | 401, 403, 404, 409, 422 | Issues nonce (TTL <= 300s). Rejects expired requests (409). Rejects inconsistent threads (409). |
| `POST` | `/v1/runs/{thread_id}/decision` | `commissioner` | `SignedDecision` | 200 OK | 401, 403, 404, 409, 422 | Validates HMAC signature, consumes nonce, commits DB before resume, returns `dispatch: {enabled: false, created: false}`. Safe replay returns 200. Conflicting replay returns 409. |
| `POST` | `/v1/approval-requests/{id}/decisions` | `commissioner` | `SignedDecision` (by request ID) | 200 OK | 401, 403, 404, 409, 422 | Companion route matching ORIGINAL_REQUEST §5.4. Commits decision and reconciles graph. |
| `POST` | `/v1/runs/{thread_id}/kill` | `service` or `commissioner` | `{"reason": str}` | 200 OK | 401, 403, 404, 409 | Emergency termination of in-flight or awaiting run. Transitions thread to `failed` or terminal cancelled state. |
| `GET` | `/health/live` | None (public) | None | 200 OK | — | Process liveness only (`{"status": "ok"}`). |
| `GET` | `/health/ready` | None (public) | None | 200 OK | 503 | Readiness check: DB connectivity, role posture (`fc27_app`), checkpointer, `DISPATCH_ENABLED=false`, zero inconsistencies. |
| `GET` | `/v1/config/dispatch` | `service` or `commissioner` | None | 200 OK | 401, 403 | Returns `{"configured": false, "effective": false, "gates": [...]}`. |

### 4.2 Test Implementations for `/v1/` Routes

#### 4.2.1 `POST /v1/runs` Tests
- `test_v1_post_runs_success`:
  - Request with valid numeric `club_id="330323"` and `SVC` header.
  - Asserts 201 Created.
  - Body contains:
    - `thread_id` matching `r"^fc27-330323-run-[0-9]{8}-[0-9]{3}$"`
    - `status == "awaiting_approval"`
    - `dispatch_enabled is False`
- `test_v1_post_runs_validation_failures`:
  - Non-numeric `club_id="abc"` -> 422.
  - Excessive `club_id` length (>12 digits) -> 422.
  - Invalid platform -> 422.
- `test_v1_post_runs_cannot_inject_approval`:
  - Body containing `{"approved": True}` or `{"status": "approved"}` -> 422 (`extra="forbid"`).
- `test_v1_post_runs_duplicate_thread_id_rejected`:
  - Client sends explicit `thread_id` that already exists -> 409 Conflict.
- `test_v1_post_runs_auth_enforcement`:
  - Missing key -> 401.
  - Commissioner key on run route -> 403.

#### 4.2.2 `GET /v1/runs/{thread_id}` Safe Projection Tests
- `test_v1_get_run_safe_projection`:
  - Query created run.
  - Asserts 200 OK.
  - Fields present: `thread_id`, `status`, `package_version`, `approval_request_id`, `dispatch_enabled`.
  - Negative assertion on secrets:
    - `assert "password" not in response.text`
    - `assert "secret" not in response.text`
    - `assert "token" not in response.text`
    - `assert "checkpoint" not in response.text`
    - `assert "byte" not in response.text`
- `test_v1_get_run_not_found`:
  - Query random `thread_id` -> 404.

#### 4.2.3 `GET /v1/runs/{thread_id}/approval-request` Tests
- `test_v1_get_approval_request_projection`:
  - Returns display-safe candidate summary.
  - Includes exact package version and SHA-256 payload hash.
  - Includes `expires_at` in ISO format.
  - Shows approvable owners (`["analyst", "coach", ...]`).
  - Omits internal prompts and raw LLM traces.

#### 4.2.4 `POST /v1/runs/{thread_id}/resume` Route Tests
- `test_v1_resume_route_cannot_carry_authorization`:
  - Client attempts `POST /v1/runs/{thread_id}/resume` with `{"approved": true}` -> rejected (422) or payload ignored.
- `test_v1_resume_without_committed_decision_fails`:
  - If thread in PostgreSQL is still `awaiting_approval`, resume returns 409 Conflict (`approval_not_committed`).
- `test_v1_resume_after_committed_decision_succeeds`:
  - Once decision committed via `/decision`, `/resume` successfully completes graph and returns `{status: "approved", graph_resume: "resumed"}`.
- `test_v1_resume_requires_service_role`:
  - Commissioner token -> 403.
  - Missing token -> 401.

#### 4.2.5 `POST /v1/runs/{thread_id}/decision-challenge` & `decision` Tests
- `test_v1_decision_challenge_lifecycle`:
  - Issue challenge with `COM` header.
  - Validates nonce generation, max TTL 300s.
  - Binds nonce to exact package version.
- `test_v1_decision_commit_before_resume`:
  - Submits signed decision.
  - Verifies PostgreSQL transaction committed: `approval_requests.status == 'approved'`, `approval_audit` inserted.
  - Verifies response has `dispatch: {"enabled": false, "created": false, "message": "Dispatch is disabled..."}`.
- `test_v1_decision_idempotent_replay`:
  - Repeating exact same signed payload returns 200 OK and same terminal state.
- `test_v1_decision_conflicting_replay`:
  - Altering payload with consumed nonce returns 409 Conflict.

#### 4.2.6 Emergency Kill Route `POST /v1/runs/{thread_id}/kill` Tests
- `test_v1_kill_run_lifecycle`:
  - Start run -> `awaiting_approval`.
  - Call `POST /v1/runs/{thread_id}/kill` with `{"reason": "emergency abort"}`.
  - Status updates to `failed` or `cancelled`.
  - Subsequent challenge attempt returns 409 Conflict.
  - Subsequent resume attempt returns 409 Conflict.

### 4.3 Correlation Middleware Architecture & Tests

#### 4.3.1 Middleware Design (`src/correlation.py`)
```python
import logging
import uuid
from contextvars import ContextVar
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

correlation_id_var: ContextVar[uuid.UUID | None] = ContextVar("correlation_id", default=None)

class CorrelationIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        cid = correlation_id_var.get()
        record.correlation_id = str(cid) if cid else "none"
        return True

class CorrelationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        header_val = request.headers.get("X-Correlation-Id")
        if header_val is not None:
            try:
                cid = uuid.UUID(header_val)
            except (ValueError, TypeError, AttributeError):
                return JSONResponse(status_code=422, content={"detail": "invalid correlation id: must be a valid UUID"})
        else:
            cid = uuid.uuid4()
            
        token = correlation_id_var.set(cid)
        try:
            response = await call_next(request)
            response.headers["X-Correlation-Id"] = str(cid)
            return response
        finally:
            correlation_id_var.reset(token)
```

#### 4.3.2 Correlation Tests
- `test_correlation_header_generated_when_absent`:
  - Request without `X-Correlation-Id` receives response with valid UUIDv4 in `X-Correlation-Id` header.
- `test_correlation_header_echoed_when_present`:
  - Request with `X-Correlation-Id: 550e8400-e29b-41d4-a716-446655440000` receives exact same header in response.
- `test_correlation_header_invalid_rejected_422`:
  - Value `"not-a-uuid"` -> 422 Unprocessable Entity.
  - Value with 500 characters -> 422 Unprocessable Entity.
  - Assert nothing written to database for that request.
- `test_correlation_id_propagated_to_logs`:
  - Using `caplog`, verify log records emitted during request processing contain `correlation_id == str(cid)`.
- `test_correlation_id_stored_in_audit_and_domain_events`:
  - Decision request processed with correlation ID `cid`.
  - Database verification:
    `SELECT correlation_id FROM approval_audit WHERE thread_id = %s` == `cid`.
    `SELECT correlation_id FROM domain_events WHERE entity_id = %s` == `cid`.
- `test_correlation_id_never_signed`:
  - Verifies that correlation ID is strictly a trace identifier; altering `X-Correlation-Id` across requests does NOT invalidate the HMAC signature of a decision challenge.

### 4.4 Health Probes Tests
- `test_health_live_probe`:
  - `GET /health/live` returns 200 OK immediately with `{"status": "ok"}`.
- `test_health_ready_probe_healthy`:
  - `GET /health/ready` returns 200 OK with:
    - `"status": "ok"`
    - `"postgres": "ok"`
    - `"db_role_posture": "ok"`
    - `"dispatch": "disabled"`
    - `"approval_consistency": 0`
- `test_health_ready_probe_fails_on_posture_violation`:
  - Inject runtime posture violation on test database (e.g. grant superuser or create on public to `fc27_app`).
  - `GET /health/ready` returns 503 Service Unavailable with posture failure details.
- `test_health_ready_probe_fails_if_dispatch_enabled_true`:
  - If `DISPATCH_ENABLED=true` is set in Phase 1 configuration:
  - `GET /health/ready` returns 503 Service Unavailable.

### 4.5 Kill Switch Tests (`APPROVAL_CLI_HMAC_ENABLED`)
- `test_hmac_kill_switch_blocks_decisions`:
  - Set `APPROVAL_CLI_HMAC_ENABLED=false` and `signing_secret=None`.
  - `POST /v1/runs/{thread_id}/decision-challenge` returns 403 Forbidden with `detail: "proof_type_disabled"`.
  - `POST /v1/runs/{thread_id}/decision` returns 403 Forbidden with `detail: "proof_type_disabled"`.
- `test_api_starts_without_signing_secret_when_hmac_disabled`:
  - `ApiSettings.from_env()` succeeds without `APPROVAL_SIGNING_SECRET` when `APPROVAL_CLI_HMAC_ENABLED=false`.
  - Fails with `ConfigError` when `APPROVAL_CLI_HMAC_ENABLED=true` and `APPROVAL_SIGNING_SECRET` is missing.

---

## 5. Test Harness & Fixture Architecture (`tests/conftest.py`)

To support Milestone 2 tests without duplicating database bootstrap logic, the fixture suite requires specific extensions.

### 5.1 Existing Fixture Assessment
- `pg_admin_uri`, `pg_roles`, `pg_db`, `pg_db_migrated`, `pg_admin_db_uri`, `pg_migrator_uri`, `pg_uri`, `pg_dispatch_uri`:
  Fully functional and verified! `pg_uri` connects as `fc27_app` with post-test inconsistency verification.
- `client`: Currently builds app with legacy pipeline stub.
- `wired`: Factory building `(runner, app)` with real LangGraph StateGraph, `PostgresSaver`, and `TestClient`.

### 5.2 Required Milestone 2 Fixture Additions

```python
# ==============================================================================
# Milestone 2 Fixture Extensions in tests/conftest.py
# ==============================================================================

@pytest.fixture()
def client_v1(pg_uri):
    """TestClient wired with full /v1/ routes, correlation middleware, and stub pipeline."""
    from tests.helpers import make_app
    app, events = make_app(pg_uri)
    with TestClient(app) as c:
        c.events = events
        yield c

@pytest.fixture()
def api_settings_factory(pg_uri):
    """Factory creating ApiSettings with parameterized overrides."""
    from tests.helpers import settings
    def _create(**overrides):
        return settings(pg_uri, **overrides)
    return _create

@pytest.fixture()
def wired_restart(pg_uri):
    """Factory simulating process/server restart by creating fresh runners on same DB."""
    from src.approval_store import ApprovalStore
    from src.graph import GraphDeps, GraphRunner
    from src.graph_api import create_app
    from tests.conftest import FakeRoles, dashboard
    from tests.helpers import settings

    store = ApprovalStore(pg_uri)
    runners = []

    def make_runner(dash=None, roles=None):
        def mcp(tool, args):
            return dash if dash is not None else dashboard()

        deps = GraphDeps(
            mcp=mcp,
            role_runner=roles or FakeRoles(),
            registry_factory=lambda: None,
            decision_reader=store.load,
        )
        runner = GraphRunner.from_conninfo(pg_uri, deps)
        runners.append(runner)
        app = create_app(settings(pg_uri), runner=runner)
        return runner, app

    yield make_runner

    for r in runners:
        try:
            r.pool.close()
        except Exception:
            pass

@pytest.fixture()
def correlation_client(pg_uri):
    """Client with correlation tracking and log capture."""
    from tests.helpers import make_app
    app, events = make_app(pg_uri)
    return TestClient(app)
```

---

## 6. Traceability Matrix: Acceptance Criteria vs Test Design

| Criterion / Feature # | Requirement Description | Target Test File | Target Test Function |
|---|---|---|---|
| **AC §11.2 / F18** | Exactly one approval interrupt; no `interrupt_before` | `tests/test_graph_persistence.py` | `test_single_approval_interrupt_at_approval_node` |
| **AC §11.2 / F19** | Resume without committed PostgreSQL decision raises `ApprovalNotCommitted` | `tests/test_graph_persistence.py` | `test_resume_without_committed_postgres_decision_raises_error` |
| **AC §11.2 / §7.2** | Resume payload tampering ignored (`Command(resume=...)` bypass fails) | `tests/test_graph_persistence.py` | `test_resume_payload_tampering_ignored` |
| **AC §11.2 / §7.2** | Direct graph state mutation bypassed by PostgreSQL decision re-read | `tests/test_graph_persistence.py` | `test_direct_state_mutation_bypassed_by_db_reread` |
| **AC §11.2 / §7.2** | Stale / mismatched package version fails closed | `tests/test_graph_persistence.py` | `test_resume_with_mismatched_package_version_fails_closed` |
| **AC §11.2 / F20** | Terminal state formatting (`APPROVED, NOT DISPATCHED`, `REJECTED, NO ACTION TAKEN`) | `tests/test_graph_persistence.py` | `test_terminal_state_formatting_enforced` |
| **AC §11.2 / §7.1** | Checkpoint state recovery across simulated process/server restart | `tests/test_graph_persistence.py` | `test_checkpoint_recovery_across_server_restart` |
| **AC §11.2 / §7.4** | Repeated resume on terminal thread is safe no-op | `tests/test_graph_persistence.py` | `test_repeated_resume_on_terminal_state_is_noop` |
| **AC §11.2 / §4.6** | Startup reconciliation safely completes unresumed committed decisions | `tests/test_graph_persistence.py` | `test_startup_reconciliation_resumes_committed_threads` |
| **AC §11.3 / F22** | `POST /v1/runs` creates durable run record with `dispatch_enabled=false` | `tests/test_graph_api_v1.py` | `test_v1_post_runs_creates_durable_run` |
| **AC §11.3 / F22** | `POST /v1/runs` validates numeric club_id, rejects approval fields | `tests/test_graph_api_v1.py` | `test_v1_post_runs_validation_and_rejection` |
| **AC §11.3 / F23** | `GET /v1/runs/{id}` returns safe projection, omitting secrets & DB creds | `tests/test_graph_api_v1.py` | `test_v1_get_run_safe_projection_omits_secrets` |
| **AC §11.3 / F24** | `GET /v1/runs/{id}/approval-request` returns display-safe candidate summary | `tests/test_graph_api_v1.py` | `test_v1_get_approval_request_projection` |
| **AC §11.3 / F25** | `POST /v1/runs/{id}/decision` commits DB before resuming graph | `tests/test_graph_api_v1.py` | `test_v1_decision_commits_db_before_resume` |
| **AC §11.3 / F25** | `POST /v1/approval-requests/{id}/decisions` route compatibility | `tests/test_graph_api_v1.py` | `test_v1_approval_request_decisions_route` |
| **AC §11.3 / F26** | `POST /v1/runs/{id}/resume` refuses authorization payload, fails if uncommitted | `tests/test_graph_api_v1.py` | `test_v1_resume_refuses_authorization_payload` |
| **AC §11.3 / Kill** | `POST /v1/runs/{id}/kill` terminates running run into failed/cancelled state | `tests/test_graph_api_v1.py` | `test_v1_kill_run_terminates_workflow` |
| **AC §11.3 / F21** | Correlation middleware validates, generates, echoes `X-Correlation-Id` | `tests/test_graph_api_v1.py` | `test_correlation_middleware_header_handling` |
| **AC §11.3 / F21** | Correlation middleware propagates ID to logs and audit records | `tests/test_graph_api_v1.py` | `test_correlation_id_propagated_to_logs_and_db` |
| **AC §11.3 / F27** | Health probes (`/health/live`, `/health/ready`) verify dependencies | `tests/test_graph_api_v1.py` | `test_health_probes_liveness_and_readiness` |
| **AC §11.3 / F27** | `/health/ready` fails if runtime posture violated or `DISPATCH_ENABLED=true` | `tests/test_graph_api_v1.py` | `test_health_ready_fails_on_posture_or_dispatch_flag` |
| **AC §11.3 / F28** | Kill switch `APPROVAL_CLI_HMAC_ENABLED=false` disables decisions (403) | `tests/test_graph_api_v1.py` | `test_hmac_kill_switch_behavior` |
| **AC §11.3 / F29** | Actor-neutral decision processing without hardcoded `commissioner_input` | `tests/test_graph_api_v1.py` | `test_actor_neutral_decision_validation` |
| **AC §11.4 / Invar** | Global invariant: `dispatch_enabled=false` in all responses & headers | `tests/test_graph_api_v1.py` | `test_dispatch_disabled_everywhere` |

---

## 7. Implementation Plan for Next Step (Implementer Guidance)

When implementation begins, the implementing agents must:
1. **Create `src/correlation.py`**:
   - `correlation_id_var`, `CorrelationIdFilter`, and `CorrelationMiddleware`.
2. **Update `src/graph.py`**:
   - Format terminal states as `"APPROVED, NOT DISPATCHED"` and `"REJECTED, NO ACTION TAKEN"`.
   - Add `DECIDED_STATUSES = ("approved", "rejected", "revision_requested")`.
   - Update `approval` node to accept `DECIDED_STATUSES`.
   - In `GraphRunner.resume(thread_id)`: check `deps.decision_reader(thread_id)["status"] in DECIDED_STATUSES` before calling `app.invoke`.
3. **Update `src/graph_api.py`**:
   - Register `CorrelationMiddleware`.
   - Add `/health/live` and `/health/ready`.
   - Mount versioned `/v1/` routes:
     - `POST /v1/runs`
     - `GET /v1/runs/{thread_id}`
     - `GET /v1/runs/{thread_id}/approval-request`
     - `POST /v1/runs/{thread_id}/resume`
     - `POST /v1/runs/{thread_id}/decision-challenge`
     - `POST /v1/runs/{thread_id}/decision` (and companion `POST /v1/approval-requests/{id}/decisions`)
     - `POST /v1/runs/{thread_id}/kill`
   - Implement HMAC kill switch handling (`APPROVAL_CLI_HMAC_ENABLED`).
4. **Create `tests/test_graph_persistence.py`** following Section 3 above.
5. **Create `tests/test_graph_api_v1.py`** following Section 4 above.
6. **Update `tests/conftest.py`** with the new fixtures defined in Section 5.
7. Run the test suite:
   `.venv\Scripts\python.exe -m pytest -o addopts="" -q -rs tests/test_graph_persistence.py tests/test_graph_api_v1.py`
