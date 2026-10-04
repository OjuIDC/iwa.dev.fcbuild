# Dispatch: Milestone 2 Implementation Worker

## Identity
- Archetype: teamwork_preview_worker
- Working Directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m2_1

## Mandatory Inputs
You MUST read:
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (including Section 1A patch).
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`.
3. The 3 completed Milestone 2 Explorer reports:
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_graph_1\graph_analysis.md`
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_api_1\api_analysis.md`
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_tests_1\tests_analysis.md`
4. `docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md` (Tasks 5 and 6).

## Write Ownership (Exclusively Owned Files)
You own and modify:
- `src/correlation.py`
- `src/graph.py`
- `src/graph_api.py`
- `src/schemas.py` (for API projection models if needed)
- `tests/conftest.py`
- `tests/test_graph_persistence.py`
- `tests/test_graph_api_v1.py`
- `tests/test_langgraph_flow.py` (or existing graph tests)

## Mandatory Integrity Warning
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

## Task Description
Implement Milestone 2: Graph Core & Persistent Graph API following the detailed designs from the three explorers:

1. **Graph Core & Persistence (`src/graph.py`)**:
   - Single interrupt: Ensure exactly one `interrupt()` call in `approval` node. Ensure NO double interruption (do NOT set `interrupt_before=["approval"]`).
   - PostgreSQL authoritative decision re-read: When resumed, `approval` node MUST NOT trust the resume payload. Call `deps.decision_reader(thread_id)` querying PostgreSQL `approval_audit` / `graph_threads`.
   - Validate that committed decision matches `package_version` and `approval_request_id`.
   - Raise `ApprovalNotCommitted` if no valid decision is committed in PostgreSQL.
   - Terminal state formatting: State status must be formatted as `"APPROVED, NOT DISPATCHED"`, `"REJECTED, NO ACTION TAKEN"`, or `"REVISION_REQUESTED, NO ACTION TAKEN"`. Ensure strings like `"APPROVED and ACTIVE"` do NOT exist.
   - LangGraph checkpointer (`PostgresSaver`): Workflow continuation only, using least-privileged `fc27_app` role.

2. **Correlation Middleware (`src/correlation.py`)**:
   - Implement `correlation_id_var` ContextVar.
   - Implement `CorrelationIdFilter` for standard library logging.
   - Implement `CorrelationMiddleware` for FastAPI: extracts `X-Correlation-Id` header (validates UUIDv4 format, rejects invalid with HTTP 422 `{"detail": "invalid correlation id"}`) or generates a fresh UUIDv4, binds to request context, echoes in response headers, enriches log records.

3. **Persistent Graph API (`src/graph_api.py`)**:
   - Versioned `/v1/` routes:
     - `POST /v1/runs`: starts graph run, returns safe projection.
     - `GET /v1/runs/{thread_id}`: returns safe run projection.
     - `GET /v1/runs/{thread_id}/approval-request`: returns safe approval request projection.
     - `POST /v1/runs/{thread_id}/resume`: wakes graph (cannot authorize).
     - `POST /v1/runs/{thread_id}/decision-challenge`: issues signed challenge.
     - `POST /v1/runs/{thread_id}/decision`: applies signed decision.
     - `POST /v1/runs/{thread_id}/kill`: authenticated emergency kill switch.
     - Maintain legacy routes (`/threads/{id}/...`, `/run`) as aliases.
   - Safe projections: Define projection schemas preventing leakage of internal secrets, connection URIs, model blobs, or stack traces. Redact user inputs from 422 errors.
   - Health probes:
     - `GET /health/live`: 200 OK process liveness.
     - `GET /health/ready`: 200 OK readiness verifying DB connectivity, checkpointer availability, 0 non-legacy inconsistencies, and `DISPATCH_ENABLED=false`. Return 503 if not ready.
     - `GET /health`: backward-compatible probe.
   - HMAC kill switch: If `APPROVAL_CLI_HMAC_ENABLED=false`, return 403 `proof_type_disabled`. `POST /v1/runs/{thread_id}/kill` cancels run and revokes open nonces.
   - Invariant: `DISPATCH_ENABLED=false` at all times.

4. **Tests (`tests/test_graph_persistence.py`, `tests/test_graph_api_v1.py`, `tests/conftest.py`)**:
   - Implement full test suites per `explorer_m2_tests_1/tests_analysis.md`.
   - Update conftest fixtures.

5. **Verification**:
   - Execute tests:
     `$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_db_roles.py tests/test_db_migrate_roles.py tests/test_events_outbox.py tests/test_migration_0003.py tests/test_approval_lifecycle.py tests/test_approval_security.py tests/test_graph_persistence.py tests/test_graph_api_v1.py`
   - Ensure all previous tests continue passing (zero regressions!).
   - Write `handoff.md` following the Handoff Protocol.
   - Notify orchestrator with `send_message`.


## 2026-10-03T15:43:25Z
You are worker_m2_1, a teamwork_preview_worker agent.
Your working directory is: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m2_1

Read your instructions in:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m2_1\DISPATCH.md

MANDATORY: You MUST read:
1. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md (including Section 1A patch)
2. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md
3. The 3 Milestone 2 explorer reports:
   - c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_graph_1\graph_analysis.md
   - c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_api_1\api_analysis.md
   - c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_tests_1\tests_analysis.md
4. docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md (Tasks 5 and 6)

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

Write ownership:
- src/correlation.py
- src/graph.py
- src/graph_api.py
- src/schemas.py
- tests/conftest.py
- tests/test_graph_persistence.py
- tests/test_graph_api_v1.py
- tests/test_langgraph_flow.py

Task:
Implement Milestone 2 (Graph Core & Persistent Graph API) following the detailed designs from the three explorers.
Execute tests via:
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_db_roles.py tests/test_db_migrate_roles.py tests/test_events_outbox.py tests/test_migration_0003.py tests/test_approval_lifecycle.py tests/test_approval_security.py tests/test_graph_persistence.py tests/test_graph_api_v1.py
Write handoff.md in your working directory and notify the orchestrator with send_message when done.
