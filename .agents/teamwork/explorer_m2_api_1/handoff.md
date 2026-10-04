# Milestone 2: Graph API & Security Routes — Handoff Report

## 1. Observation

1. **Repository & Codebase State**:
   - `src/correlation.py`: Does not currently exist (`GetFileAttributesEx: The system cannot find the file specified`).
   - `src/graph_api.py` (lines 193-243): Exposes only unversioned legacy routes:
     - Line 193: `@app.post("/run", status_code=201)`
     - Line 218: `@app.get("/threads/{thread_id}")`
     - Line 227: `@app.post("/threads/{thread_id}/decision-challenge")`
     - Line 235: `@app.post("/threads/{thread_id}/decision")`
     - Line 184: `@app.get("/health")`
   - `src/graph_api.py` (lines 35-43): `ApiSettings` lacks `dispatch_enabled`, `cli_hmac_enabled`, `approval_request_ttl_hours`, and `expiry_interval_seconds`.
   - `src/graph_api.py` (lines 173-191): Lacks `/health/live` and `/health/ready` probe endpoints. `/health` only runs `SELECT 1` without verifying database role posture or Phase 1 safety flags.
   - `src/graph_api.py`: Lacks validation error handling stripping `input` fields (D-30 compliance).
   - `src/schemas.py`: Lacks safe projection models (`RunProjection`, `ApprovalRequestProjection`, `KillRunRequest`, `KillRunResponse`, `ResumeRequest`, `ResumeResponse`).

2. **Existing Baseline Tests**:
   - Verified with real PostgreSQL container `fc27-test-postgres` on port 55432 (`TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"`):
     - `tests/test_approval_lifecycle.py`: 19 passed in 26.06s.
     - `tests/test_approval_security.py`: 31 passed in 32.10s.
     - `tests/test_db_roles.py`, `tests/test_db_migrate_roles.py`, `tests/test_events_outbox.py`, `tests/test_migration_0003.py`: 37 passed in 155.73s.
     - `tests/test_adversarial_m1.py`, `tests/test_m1_adversarial_challenger.py`: 31 passed in 38.45s.
     - Total: **118 tests currently passing** on Milestone 1 baseline.

3. **Mandatory Documentation Requirements**:
   - `ORIGINAL_REQUEST.md` (Section 5):
     - `POST /v1/runs`, `GET /v1/runs/{thread_id}`, `GET /v1/runs/{thread_id}/approval-request`, `POST /v1/runs/{thread_id}/resume`, `POST /v1/approval-requests/{approval_request_id}/decisions`, `GET /health/live`, `GET /health/ready`.
     - Invariant 1: `DISPATCH_ENABLED=false` at all times.
     - Invariant 9: `POST /v1/runs/{thread_id}/resume` only wakes/re-enters the Graph. It cannot approve or reject.
   - `docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md` (Task 5, C-1, C-4, C-5, D-29, D-30):
     - Correlation middleware in `src/correlation.py` extracting `X-Correlation-Id`, generating UUID when absent, rejecting malformed with 422, attaching to log filter.
     - HMAC kill switch on decision routes (`APPROVAL_CLI_HMAC_ENABLED=false` -> 403 `proof_type_disabled`).
     - Validation errors strip Pydantic `input` and `ctx.input` fields (D-30).
     - Response states: `dispatch: {"enabled": false, "created": false, "message": "Dispatch is disabled. This approval records the selection only; nothing was delivered to any player."}`.

---

## 2. Logic Chain

1. **Need for `src/correlation.py`**:
   - Observability and audit trail require tracing requests across services, logs, and database records (`approval_audit.correlation_id`, `domain_events.correlation_id`).
   - Amendment C-5 dictates that correlation IDs are trace identifiers only, never authorization evidence.
   - Therefore, a dedicated middleware must:
     - Check `X-Correlation-Id`: if present, validate canonical UUID format; if malformed (non-UUID or excessive length), reject with HTTP 422 immediately before invoking downstream DB operations.
     - If absent, generate a fresh `uuid4()`.
     - Bind to a `ContextVar[UUID | None]` so `CorrelationIdFilter` can enrich log records without passing arguments through every call stack.
     - Echo the ID in `X-Correlation-Id` response header.

2. **Transition to Versioned `/v1/` Routes with Legacy Compatibility**:
   - The system prompt and Section 5 require explicit `/v1/` routes for all operations:
     `POST /v1/runs`, `GET /v1/runs/{thread_id}`, `GET /v1/runs/{thread_id}/approval-request`, `POST /v1/runs/{thread_id}/resume`, `POST /v1/runs/{thread_id}/decision-challenge`, `POST /v1/runs/{thread_id}/decision`, `POST /v1/approval-requests/{approval_request_id}/decisions`, `POST /v1/runs/{thread_id}/kill`.
   - Existing test fixtures and callers (`start()`, `decide()`, `tests/test_approval_security.py`, `tests/test_langgraph_flow.py`) call `/run`, `/threads/{id}`, etc.
   - Rather than breaking existing callers or duplicating code, defining canonical `/v1/` route handlers and declaring the legacy endpoints as direct aliases guarantees 100% backward compatibility while establishing `/v1/` as the versioned standard.

3. **Safe Projections & Sensitive Data Protection**:
   - Direct return of graph state or raw database rows can expose checkpoint blobs, model prompts, and internal error structures.
   - Creating Pydantic projection models (`RunProjection`, `ApprovalRequestProjection`, `DispatchBlock`) guarantees that only approved public fields are emitted.
   - Exception handlers must wrap all errors through `redact_conninfo()` to prevent password or URI exposure.
   - Exception handler for `RequestValidationError` must sanitize Pydantic errors by popping `input` and `ctx.input` (D-30 compliance).
   - If a legacy run contains a `"dispatch"` key in state, the projection replaces it with `"legacy_selection_record": {..., "note": "recorded selection only; never delivered"}` and removes the raw `"dispatch"` key.

4. **Health Probes Split**:
   - Cloud runtimes need independent liveness and readiness signals:
     - `/health/live`: Fast process check (HTTP 200).
     - `/health/ready`: Dependency check verifying DB connection, role posture (`fc27_app` least privilege), checkpointer availability, approval pair consistency count (`fc27_approval_inconsistencies` == 0), and explicit safety invariant `settings.dispatch_enabled == False`. If `DISPATCH_ENABLED=true` in Phase 1, `/health/ready` returns HTTP 503 to prevent erroneous traffic routing.

5. **HMAC Kill Switch & Cancellation**:
   - When `APPROVAL_CLI_HMAC_ENABLED=false`, CLI decisions must be rejected with 403 `proof_type_disabled`. `signing_secret` is not required at startup.
   - `POST /v1/runs/{thread_id}/kill` provides authenticated emergency abort: verifies Commissioner authorization, locks nonces/requests/threads, updates thread status to `failed`, revokes unconsumed nonces, and records auditable cancellation events.

---

## 3. Caveats

1. **Phase 1 Boundary Enforcement**:
   - Phase 1 strictly forbids player delivery adapters, outbox relay dispatch workers, and Phase 2 user/WebAuthn sessions.
   - The decision route in Phase 1 accepts only `actor_type="commissioner"` and `authority_basis="primary"`. Player decisions and delegated authority fail closed.
2. **Checkpointer DDL in Migrations**:
   - `PostgresSaver.setup()` runs during migrations under `fc27_owner`. Normal Graph API startup runs under least-privileged `fc27_app` with `checkpoint_setup=False` and `run_migrations=False`.
3. **Background Daemon Threading**:
   - The 60-second background expiry loop (`store.expire_due()`) and gate re-evaluation must run via `asyncio.to_thread` or threadpool to avoid blocking FastAPI's async event loop.

---

## 4. Conclusion

The detailed design for `src/graph_api.py` and `src/correlation.py` is fully specified in `api_analysis.md`.
The implementation plan is concrete, testable, and strictly adheres to all Phase 1 invariants:
1. `src/correlation.py` will implement `correlation_id_var`, `CorrelationIdFilter`, and `CorrelationMiddleware`.
2. `src/schemas.py` will define safe projection models (`RunProjection`, `ApprovalRequestProjection`, `KillRunRequest`, `KillRunResponse`, `ResumeRequest`, `ResumeResponse`).
3. `src/graph_api.py` will mount versioned `/v1/` routes with legacy aliases, HMAC kill switches, dual health probes, and validation error stripping.
4. Production source code in `src/` and `migrations/` has remained completely untouched during this exploration phase.

---

## 5. Verification Method

To independently verify the baseline and future implementation:
1. Ensure the PostgreSQL container is running:
   ```powershell
   docker ps  # fc27-test-postgres on port 55432
   ```
2. Run the full baseline test suite:
   ```powershell
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"
   .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_approval_lifecycle.py tests/test_approval_security.py tests/test_db_roles.py tests/test_db_migrate_roles.py tests/test_events_outbox.py tests/test_migration_0003.py
   ```
   **Expected**: 87 core tests pass.
3. Run the adversarial M1 test suite:
   ```powershell
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"
   .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_adversarial_m1.py tests/test_m1_adversarial_challenger.py
   ```
   **Expected**: 31 adversarial tests pass.
4. Invalidation condition: Any code change that causes existing tests to fail, allows `DISPATCH_ENABLED=true` to pass readiness, leaks database credentials in error responses, or allows `resume` to authorize a run invalidates the design.
