# Milestone 2: Graph API & Security Routes Deep Exploration & Detailed Design

## Executive Summary
This document provides the architectural analysis and implementation design for **Milestone 2 (Graph API & Security Routes)**, focusing on `src/graph_api.py`, `src/correlation.py`, and supporting schema contracts in `src/schemas.py`.

The primary goals are:
1. Transition all operational endpoints to versioned `/v1/` routes with clean backward compatibility aliasing for legacy endpoints.
2. Implement correlation tracking in `src/correlation.py` via `correlation_id_var`, `CorrelationIdFilter`, and FastAPI middleware enforcing UUID canonicalization, 422 rejection on malformed IDs, response header echoing, and log enrichment.
3. Guarantee safe projections: define strict Pydantic response models that omit private checkpoint internals, strip raw inputs from validation errors (D-30), redact database URIs via `redact_conninfo()`, and transform legacy `dispatch` keys to `legacy_selection_record`.
4. Deploy dual health probes: `/health/live` (process liveness) and `/health/ready` (deep readiness verifying DB connection, runtime role posture, checkpointer tables, approval consistency view, and `DISPATCH_ENABLED=false`).
5. Support HMAC kill switch on decision routes (`APPROVAL_CLI_HMAC_ENABLED=false` -> 403 `proof_type_disabled`) and authenticated run cancellation on `POST /v1/runs/{thread_id}/kill`.
6. Enforce Phase 1 invariants: `DISPATCH_ENABLED=false` at all times, disabled-dispatch response messaging, resume wake-up without authorization payload, and terminal states formatted as `APPROVED, NOT DISPATCHED` / `REJECTED, NO ACTION TAKEN`.

---

## 1. Architectural Context & Milestone 1 Baseline

### 1.1 Verified Repository State
- **Milestone 1 Complete**: Authoritative `approval_requests` lifecycle, 72h expiry daemon (`ApprovalStore.expire_due`), strict lock ordering (`approval_nonces` -> `approval_requests` -> `graph_threads`), pair-consistency trigger (`fc27_check_approval_pair()`), inconsistency view (`fc27_approval_inconsistencies`), Section 1A human authority compatibility fields (`actor_type`, `authority_basis`, `authority_role`, `decision_scope`), and append-only audit/domain events are all implemented and passing (87 core tests + 31 adversarial tests passing).
- **Current `src/graph_api.py` Limitations**:
  - Uses unversioned legacy routes: `POST /run`, `GET /threads/{id}`, `POST /threads/{id}/decision-challenge`, `POST /threads/{id}/decision`.
  - Lacks dedicated `src/correlation.py` middleware for `X-Correlation-Id` extraction, validation, and structured log filtering.
  - Lacks safe projection response models (returns raw dictionaries from `state` and DB rows).
  - Lacks `/health/live` and `/health/ready` split; `/health` only pings PostgreSQL without posture or safety checks.
  - Lacks HMAC kill switch (`APPROVAL_CLI_HMAC_ENABLED`) handling and run cancellation route (`/v1/runs/{thread_id}/kill`).
  - Does not sanitize 422 validation errors to strip user `input` fields (D-30).

---

## 2. Versioned `/v1/` Routes Specification

### 2.1 Route Matrix & Role Authorization

| HTTP Method | Route Path | Authorized Role | Request Body | Response Model / Status | Description |
|---|---|---|---|---|---|
| `POST` | `/v1/runs` | `service` | `RunRequest` | `RunProjection` (201) | Initiates run, creates durable DB record, executes pipeline, completes run to `awaiting_approval` or `failed`. |
| `GET` | `/v1/runs/{thread_id}` | `service` | None | `RunProjection` (200) | Returns public safe projection. Omits checkpoint blobs and secrets; sanitizes legacy dispatch keys. |
| `GET` | `/v1/runs/{thread_id}/approval-request` | `service`, `commissioner` | None | `ApprovalRequestProjection` (200) | Returns display-safe candidate summary, package hash/version, expiry status. |
| `POST` | `/v1/runs/{thread_id}/resume` | `service` | `ResumeRequest` (empty/extra=forbid) | `ResumeResponse` (200) | Restricted wake-up signal. Rereads PostgreSQL. Refuses if uncommitted. Carries NO authorization payload. |
| `POST` | `/v1/runs/{thread_id}/decision-challenge` | `commissioner` | `DecisionIntent` | `ChallengeResponse` (200) | Issues single-use nonce (TTL ≤ 300s clamped to request expiry). Blocked by HMAC kill switch or inconsistencies. |
| `POST` | `/v1/runs/{thread_id}/decision` | `commissioner` | `SignedDecision` | `DecisionResponse` (200) | Verifies signature, consumes nonce, commits DB transaction, echoes disabled dispatch, then wakes graph. |
| `POST` | `/v1/approval-requests/{approval_request_id}/decisions` | `commissioner` | `SignedDecision` | `DecisionResponse` (200) | Authoritative request-centric decision path. Resolves thread from request, executes decision transaction. |
| `POST` | `/v1/runs/{thread_id}/kill` | `commissioner` | `KillRunRequest` | `KillRunResponse` (200) | Authenticated run cancellation. Revokes nonces, transitions thread/request to `failed` or `expired`. |
| `GET` | `/health/live` | Public / Ops | None | `HealthLiveResponse` (200) | Process liveness only. No external dependency check. |
| `GET` | `/health/ready` | Public / Ops | None | `HealthReadyResponse` (200 / 503) | Readiness check: DB ping, posture check, checkpointer check, inconsistency count, `DISPATCH_ENABLED=false`. |
| `GET` | `/health` | Public / Ops | None | `HealthResponse` (200 / 503) | Consolidated health probe (backwards compatible). Reports consistency, posture, dispatch status. |
| `GET` | `/v1/config/dispatch` | `service`, `commissioner` | None | `DispatchConfigResponse` (200) | Reports configured vs effective dispatch status, gates list, fc_version, platform. |

### 2.2 Backward Compatibility & Legacy Aliasing Strategy
Existing integration tests, live-watchers, and CLI scripts rely on `/run`, `/threads/{thread_id}`, `/threads/{thread_id}/decision-challenge`, and `/threads/{thread_id}/decision`.
To avoid breaking existing callers:
1. Canonical route handlers are defined for the `/v1/` routes.
2. Legacy routes are declared as aliases that invoke the exact same handler logic.
3. Legacy routes are tagged with `include_in_schema=False` or `deprecated=True` in OpenAPI docs.
4. Legacy route mapping:
   - `POST /run` -> calls `create_run_endpoint`
   - `GET /threads/{thread_id}` -> calls `get_run_endpoint`
   - `POST /threads/{thread_id}/decision-challenge` -> calls `decision_challenge_endpoint`
   - `POST /threads/{thread_id}/decision` -> calls `decision_endpoint`

---

## 3. Correlation Middleware (`src/correlation.py`)

### 3.1 Design Principles (Spec §22.3, C-5)
- **Trace Identifier Only**: `X-Correlation-Id` is strictly an observability trace identifier. It MUST NEVER be incorporated into canonical HMAC or WebAuthn signed payloads.
- **Fail Closed on Malformed Input**: Any supplied header that is not a canonical UUID (e.g., `'not-a-uuid'`, `'a'*500`, SQL injection attempt) MUST be rejected with HTTP 422 (`{"detail": "invalid correlation id"}`). Nothing is recorded or stored.
- **Auto-Generation & Response Echoing**: If absent, the server generates a fresh `uuid.uuid4()`. The valid UUID is attached to request state, bound to the context variable, and echoed in the `X-Correlation-Id` response header.
- **Log Enrichment**: A `logging.Filter` (`CorrelationIdFilter`) populates `record.correlation_id` on every log entry emitted during request handling.

### 3.2 Concrete Implementation Design (`src/correlation.py`)

```python
"""FC27 Correlation ID tracking and logging integration.

Implements Spec §22.3 and Amendment C-5:
- correlation_id_var: ContextVar[UUID | None]
- CorrelationIdFilter: adds correlation_id to logging.LogRecord
- CorrelationMiddleware: FastAPI middleware extracting or generating UUIDs,
  rejecting malformed headers with 422, echoing response headers.
"""
from __future__ import annotations

import logging
from contextvars import ContextVar
from uuid import UUID, uuid4

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

correlation_id_var: ContextVar[UUID | None] = ContextVar("correlation_id", default=None)


class CorrelationIdFilter(logging.Filter):
    """Logging filter attaching correlation_id to all log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        cid = correlation_id_var.get()
        record.correlation_id = str(cid) if cid is not None else None
        return True


class CorrelationMiddleware(BaseHTTPMiddleware):
    """Middleware enforcing canonical UUID correlation headers."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        raw_header = request.headers.get("x-correlation-id")
        if raw_header is not None:
            # Reject excessive lengths early before UUID parsing
            if len(raw_header) > 64:
                return JSONResponse(
                    status_code=422,
                    content={"detail": "invalid correlation id"},
                )
            try:
                # Require standard UUID representation
                cid = UUID(raw_header)
                # Ensure it didn't accept garbage via lax parsing
                if str(cid).lower() != raw_header.strip().lower():
                    return JSONResponse(
                        status_code=422,
                        content={"detail": "invalid correlation id"},
                    )
            except (ValueError, TypeError, AttributeError):
                return JSONResponse(
                    status_code=422,
                    content={"detail": "invalid correlation id"},
                )
        else:
            cid = uuid4()

        token = correlation_id_var.set(cid)
        request.state.correlation_id = cid
        try:
            response = await call_next(request)
            response.headers["X-Correlation-Id"] = str(cid)
            return response
        finally:
            correlation_id_var.reset(token)
```

---

## 4. Safe Projections & Sensitive Data Protection

### 4.1 Threat Model & Information Leakage Vectors
1. **Stack Traces and Internal Errors**:
   - Unhandled exceptions must not leak Python stack traces, file paths, or DB driver internals.
   - All connection strings must pass through `redact_conninfo(conninfo)` before appearing in logs or error messages.
2. **Database Credentials & File Secrets**:
   - Secrets (`POSTGRES_URI`, `APPROVAL_SIGNING_SECRET`, etc.) must never be returned in API bodies, configuration endpoints, or health reports.
3. **Internal Checkpoint Internals**:
   - `langgraph` state objects include internal task IDs, raw channel values, and model execution metadata. The API projection must extract only display-safe fields.
4. **Validation Error Echoing (D-30)**:
   - Default Pydantic 422 responses echo back user input (e.g. `input: 26.123` for objective bounds). This violates D-30.
   - An application-level exception handler MUST strip `input` fields and `ctx` references from validation error payloads.
5. **Legacy Selection Record Handling**:
   - Legacy `dispatch` dictionaries in old threads must be converted to `legacy_selection_record` with `"note": "recorded selection only; never delivered"`, removing the raw `dispatch` key so callers are not misled into believing dispatch occurred.

### 4.2 Safe Pydantic Response Models (`src/schemas.py`)

```python
class DispatchBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    created: bool = False
    message: str = "Dispatch is disabled. This approval records the selection only; nothing was delivered to any player."
    dispatch_id: str | None = None


class RunProjection(BaseModel):
    model_config = ConfigDict(extra="ignore")
    thread_id: str
    status: str
    club_id: str
    platform: str
    fc_version: str
    package_version: str | None = None
    approval_request_id: str | None = None
    approved_by: str | None = None
    approved_at: str | None = None
    errors: list[str] = Field(default_factory=list)
    graph_next: list[str] = Field(default_factory=list)
    graph_interrupted: bool = False
    dispatch_enabled: bool = False
    lead_packages: dict[str, Any] | None = None
    legacy_selection_record: dict[str, Any] | None = None


class ApprovalRequestProjection(BaseModel):
    model_config = ConfigDict(extra="ignore")
    approval_request_id: str
    thread_id: str
    package_version: str
    package_hash: str | None = None
    status: str
    created_at: str
    expires_at: str
    is_expired: bool
    approvable_owners: list[str] = Field(default_factory=list)
    display_summary: dict[str, Any] | None = None


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResumeResponse(BaseModel):
    thread_id: str
    status: str
    graph_resume: str


class DecisionResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    thread_id: str
    status: str
    package_version: str | None = None
    approval_audit_id: int | None = None
    approval_request_id: str | None = None
    dispatch: DispatchBlock
    graph_resume: str


class KillRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=500, default="operator kill switch triggered")


class KillRunResponse(BaseModel):
    thread_id: str
    status: str
    killed: bool
    reason: str
```

### 4.3 Validation Error Sanitizer (D-30 Compliance)
```python
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    clean_errors = []
    for err in exc.errors():
        err_dict = dict(err)
        err_dict.pop("input", None)
        if "ctx" in err_dict and isinstance(err_dict["ctx"], dict):
            err_dict["ctx"] = {k: v for k, v in err_dict["ctx"].items() if k != "input"}
        clean_errors.append(err_dict)
    return JSONResponse(status_code=422, content={"detail": clean_errors})
```

---

## 5. Health Probes Architecture

### 5.1 `/health/live` (Liveness Probe)
- **Role**: Immediate process liveness check for container runtimes (Kubernetes / Docker healthcheck).
- **Behavior**: Returns HTTP 200 immediately without executing database queries or network calls.
- **Response**:
  ```json
  {
    "status": "ok",
    "process": "live",
    "fc_version": "27"
  }
  ```

### 5.2 `/health/ready` (Readiness Probe)
- **Role**: Traffic admission probe. Fails closed (HTTP 503) if any database dependency, migration requirement, role posture, or Phase 1 invariant is violated.
- **Validation Checklist**:
  1. **PostgreSQL Connectivity**: `SELECT 1` executes with a 3-second timeout.
  2. **Role Posture Check**: `check_runtime_posture(conn)` returns an empty list for `fc27_app` (ensuring no superuser, no `fc27_owner` membership, no `CREATE ON SCHEMA public`).
  3. **Checkpointer Setup**: `checkpoints` and `checkpoint_blobs` tables exist.
  4. **Approval Consistency**: `SELECT count(*) FROM public.fc27_approval_inconsistencies WHERE kind <> 'legacy_unlinked'` equals 0.
  5. **Safety Invariant Verification**: `settings.dispatch_enabled` MUST be `False`. If set to `True`, readiness probe returns 503 with `"reason": "Phase 1 safety invariant violation: DISPATCH_ENABLED must be false"`.
- **Success Response (HTTP 200)**:
  ```json
  {
    "status": "ok",
    "ready": true,
    "postgres": "ok",
    "db_role_posture": "ok",
    "approval_consistency": 0,
    "checkpointer": "ok",
    "dispatch": "disabled",
    "dispatch_enabled": false,
    "fc_version": "27",
    "platform": "common-gen5"
  }
  ```
- **Failure Response (HTTP 503)**:
  ```json
  {
    "status": "degraded",
    "ready": false,
    "reason": "..."
  }
  ```

### 5.3 Consolidated `/health` (Legacy Compatible)
- Returns:
  ```json
  {
    "status": "ok",
    "postgres": "ok",
    "fc_version": "27",
    "approval_consistency": 0,
    "dispatch": "disabled",
    "db_role_posture": "ok"
  }
  ```

---

## 6. HMAC Kill Switch & Kill Route Design

### 6.1 Decision Route Kill Switch (`APPROVAL_CLI_HMAC_ENABLED`)
Per Spec §18.6:
1. `ApiSettings` reads `APPROVAL_CLI_HMAC_ENABLED` (boolean, default `True`).
2. When `APPROVAL_CLI_HMAC_ENABLED=false`:
   - `signing_secret` is NOT required at startup; `ApiSettings.from_env()` allows `APPROVAL_SIGNING_SECRET` to be absent without throwing `ConfigError`.
   - `POST /v1/runs/{thread_id}/decision-challenge` returns HTTP 403 `{"detail": "proof_type_disabled"}`.
   - `POST /v1/runs/{thread_id}/decision` returns HTTP 403 `{"detail": "proof_type_disabled"}`.
   - The disabled HMAC state is logged at startup.

### 6.2 Run Kill Switch Route (`POST /v1/runs/{thread_id}/kill`)
- **Purpose**: Authenticated operational kill switch allowing the Commissioner (or authorized service) to terminate an in-flight or awaiting run.
- **Locking & State Machine**:
  1. Enforces `require_role(p, Role.commissioner)`.
  2. Transactional lock order:
     - Lock open nonces for thread `FOR UPDATE`.
     - Lock `approval_requests` `FOR UPDATE` (if linked).
     - Lock `graph_threads` `FOR UPDATE`.
  3. Precondition verification:
     - If thread is already in a terminal state (`approved`, `rejected`, `revision_requested`, `expired`, `failed`):
       - Return 409 Conflict: `"cannot kill terminal thread"`.
  4. Terminal State Transition:
     - Mark thread status = `'failed'` with updated state `errors: errors + [f"killed: {body.reason}"]`.
     - If linked approval request is awaiting, mark request status = `'expired'` or `'failed'` and set `expired_at = now()`.
     - Revoke any unconsumed nonces: `UPDATE approval_nonces SET revoked_at = now() WHERE thread_id = %s AND consumed_at IS NULL`.
     - Record domain event `approval.expired` or `system.degraded` via `record_event`.
  5. Response: Returns `KillRunResponse(thread_id=thread_id, status="failed", killed=True, reason=body.reason)`.

---

## 7. Phase 1 Invariants & Graph Core Alignment

### 7.1 Invariants Enforced in API
1. **`DISPATCH_ENABLED=false` Immutable**:
   - `RunRequest`, `SignedDecision`, and `KillRunRequest` forbid extra fields (`extra="forbid"`). Callers cannot inject `dispatch_enabled=true`.
   - Request headers like `X-Dispatch-Enabled: true` are completely ignored.
   - Decision responses unconditionally include:
     ```json
     "dispatch": {
       "enabled": false,
       "created": false,
       "message": "Dispatch is disabled. This approval records the selection only; nothing was delivered to any player."
     }
     ```
2. **Resume Wakes Graph But Cannot Authorize**:
   - `POST /v1/runs/{thread_id}/resume` takes `ResumeRequest` with `extra="forbid"`. Any payload containing `"approved"`, `"decision"`, or `"commissioner_input"` triggers 422.
   - Handler inspects PostgreSQL: if DB status is NOT in `DECIDED_STATUSES = ("approved", "rejected", "revision_requested")`, graph invocation is refused and returns `{"status": row["status"], "graph_resume": "refused_uncommitted"}`.
   - Graph runner itself verifies `deps.decision_reader(thread_id)` and refuses resume if no committed DB decision exists.
3. **Terminal State Formatting**:
   - Graph approval node formats terminal state as `"APPROVED, NOT DISPATCHED"` or `"REJECTED, NO ACTION TAKEN"` or `"REVISION REQUESTED, NO ACTION TAKEN"`.
   - Never transitions to `dispatched`, `delivered`, `acknowledged`, `activated`, or `tracking`.

---

## 8. Detailed Refactor Plan for `src/graph_api.py`

### 8.1 Settings & App Factory Architecture
```python
@dataclass
class ApiSettings:
    postgres_uri: str
    authenticator: Authenticator
    signing_secret: str | None = None
    approval_ttl_seconds: int = 120
    redis_url: str | None = None
    run_migrations: bool = False
    checkpoint_setup: bool = False
    enforce_db_posture: bool = True
    dispatch_enabled: bool = False
    cli_hmac_enabled: bool = True
    approval_request_ttl_hours: int = 72
    expiry_interval_seconds: int = 60

    @classmethod
    def from_env(cls) -> "ApiSettings":
        try:
            cli_hmac = env_bool("APPROVAL_CLI_HMAC_ENABLED", True)
            auth = Authenticator(
                read_secret("GRAPH_SERVICE_TOKEN", min_length=32),
                read_secret("GRAPH_COMMISSIONER_TOKEN_SHA256"),
                read_secret("COMMISSIONER_ID"),
            )
            return cls(
                postgres_uri=read_secret("POSTGRES_URI"),
                authenticator=auth,
                signing_secret=read_secret("APPROVAL_SIGNING_SECRET", required=cli_hmac, min_length=32 if cli_hmac else 0),
                approval_ttl_seconds=int(os.getenv("APPROVAL_TTL_SECONDS", "120")),
                redis_url=read_secret("REDIS_URL", required=False),
                run_migrations=env_bool("GRAPH_RUN_MIGRATIONS", False),
                checkpoint_setup=env_bool("GRAPH_CHECKPOINT_SETUP", False),
                dispatch_enabled=env_bool("DISPATCH_ENABLED", False),
                cli_hmac_enabled=cli_hmac,
                approval_request_ttl_hours=int(os.getenv("APPROVAL_REQUEST_TTL_HOURS", "72")),
            )
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
```

### 8.2 Lifespan Tasks: Background Expiry & Gate Evaluation
```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Migrations (if enabled)
    if settings.run_migrations:
        migrate(settings.postgres_uri)

    # 2. Runtime Posture Check
    if settings.enforce_db_posture:
        from src.db_roles import check_runtime_posture, redact_conninfo
        with psycopg.connect(settings.postgres_uri) as conn:
            violations = check_runtime_posture(conn)
            if violations:
                raise ConfigError(f"database role posture violation: {violations}")

    # 3. Graph Runner Initialization
    if graph["runner"] is None and pipeline is None:
        from .graph import GraphDeps, GraphRunner
        from .roles import get_role_runner
        from .tracing import tracer_from_env

        graph["runner"] = GraphRunner.from_conninfo(
            settings.postgres_uri,
            GraphDeps(role_runner=get_role_runner(), decision_reader=store.load, tracer=tracer_from_env()),
            setup=settings.checkpoint_setup,
        )
        graph["owned"] = True

    # 4. Startup Reconciliation
    if graph["runner"] is not None:
        for thread_id in store.terminal_thread_ids():
            resume_graph(thread_id)

    # 5. Background Daemon: Expiry & Gate Re-evaluation
    app.state.dispatch_effective = False
    app.state.gate_report = {"configured": settings.dispatch_enabled, "effective": False, "gates": []}
    
    stop_event = asyncio.Event()

    async def _background_loop():
        while not stop_event.is_set():
            try:
                await asyncio.to_thread(store.expire_due)
                reevaluate_gates(app)
            except Exception:
                log.exception("background expiry/gate evaluation failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=settings.expiry_interval_seconds)
            except asyncio.TimeoutError:
                pass

    bg_task = asyncio.create_task(_background_loop())

    yield

    # Shutdown
    stop_event.set()
    bg_task.cancel()
    try:
        await bg_task
    except asyncio.CancelledError:
        pass
    if graph.get("owned"):
        graph["runner"].pool.close()
```

---

## 9. Test Verification Plan

The implementation should be verified by a dedicated test suite (`tests/test_graph_api_v1.py` and `tests/test_correlation_middleware.py`) covering:

1. **Correlation Middleware**:
   - `test_malformed_correlation_id_rejected`: `'not-a-uuid'` and `'a'*500` return 422 with `{"detail": "invalid correlation id"}`. No DB operation.
   - `test_correlation_id_generated_when_absent`: response header `X-Correlation-Id` is a valid UUID.
   - `test_correlation_id_echoed`: supplied valid UUID is echoed unchanged in response header.
   - `test_correlation_id_in_log_record`: log records emitted during request contain `.correlation_id == the UUID`.
   - `test_correlation_id_not_signed`: correlation ID does not affect signature validity.
2. **Versioned Routes**:
   - `POST /v1/runs` creates run, returns 201 with `dispatch_enabled=false`.
   - `GET /v1/runs/{thread_id}` returns safe projection; strips checkpoint internals; converts legacy `dispatch` to `legacy_selection_record`.
   - `GET /v1/runs/{thread_id}/approval-request` returns exact request, package hash/version, display-safe candidate summary.
   - `POST /v1/runs/{thread_id}/resume` fails closed when uncommitted; resumes cleanly when committed; refuses authorization body.
   - `POST /v1/approval-requests/{approval_request_id}/decisions` commits decision atomically.
3. **HMAC Kill Switch & Probes**:
   - With `APPROVAL_CLI_HMAC_ENABLED=false`, decision challenge and decision routes return 403 `proof_type_disabled`.
   - `POST /v1/runs/{thread_id}/kill` terminates running/awaiting runs; revokes nonces; fails closed on terminal runs.
   - `GET /health/live` returns 200 process liveness.
   - `GET /health/ready` returns 200 when healthy; returns 503 if DB down, posture violated, or `DISPATCH_ENABLED=true`.
   - Validation errors strip user input (D-30).
