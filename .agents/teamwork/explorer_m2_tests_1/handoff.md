# Handoff Report: Milestone 2 Test Architecture Deep Exploration

**Agent:** `explorer_m2_tests_1`  
**Working Directory:** `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_tests_1`  
**Handoff Type:** Hard (Task Complete)  
**Target Milestone:** Milestone 2: Graph Core & Persistent Graph API  
**Date:** 2026-10-03  

---

## 1. Observation

1. **Test Environment & Baseline Execution:**
   - Command: `$env:TEST_POSTGRES_URI = "postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q -rs tests/test_langgraph_flow.py`
   - Result: `12 passed in 15.44s` against the live `postgres:16-alpine` container on port 55432.
   - Python virtual environment is active at `c:\Users\iwadev\dev\iwa.dev.fcbuild\.venv` with `pytest 8.4.2`.

2. **Graph Core State Machine (`src/graph.py`):**
   - Lines 141-155: Single `interrupt(state["approval_request"])` is invoked inside `approval(state)` node.
   - Line 145: Re-reads database via `deps.decision_reader(state["thread_id"])` and raises `ApprovalNotCommitted` if status not in `("approved", "rejected")`.
   - Line 148: Validates `payload.get("package_version") == state.get("package_version")`.
   - Line 214: Graph is compiled without `interrupt_before=["approval"]` (`self.app = build_graph(deps).compile(checkpointer=checkpointer)`).
   - Line 153-154: Currently returns raw `row["status"]` (e.g. `"approved"`) rather than enforced terminal formatted string `"APPROVED, NOT DISPATCHED"` / `"REJECTED, NO ACTION TAKEN"`.
   - Line 244-255: `runner.resume(thread_id)` currently does not verify `deps.decision_reader(thread_id)["status"] in DECIDED_STATUSES` before calling `self.app.invoke(...)`.

3. **Graph API Endpoints & Routes (`src/graph_api.py`):**
   - Currently mounts only unversioned legacy endpoints: `GET /health`, `POST /run`, `GET /threads/{thread_id}`, `POST /threads/{thread_id}/decision-challenge`, `POST /threads/{thread_id}/decision`.
   - Missing versioned `/v1/` routes: `POST /v1/runs`, `GET /v1/runs/{thread_id}`, `GET /v1/runs/{thread_id}/approval-request`, `POST /v1/runs/{thread_id}/resume`, `POST /v1/runs/{thread_id}/decision-challenge`, `POST /v1/runs/{thread_id}/decision`, `POST /v1/approval-requests/{approval_request_id}/decisions`, `POST /v1/runs/{thread_id}/kill`.
   - Missing probes: `GET /health/live`, `GET /health/ready`.
   - Missing correlation middleware: file `src/correlation.py` does not exist in the repository.

4. **Tier 1 Feature Tests (`tests/e2e/test_tier1_features.py`):**
   - Lines 303-436 define feature test stubs for Features 18-29 (Domain 3: Graph Core & Persistent Graph API).
   - Line 383: `test_feature_24_api_get_approval_request_endpoint` handles 404 fallback because `/v1/runs/{tid}/approval-request` is not yet mounted.
   - Line 408: `test_feature_26_api_resume_route_refuses_authorization` explicitly skips because `/v1/runs/{tid}/resume` is deferred to M2.

5. **Approval Lifecycle & Consistency (`src/approval_store.py`):**
   - Lines 188-196: `store.load(thread_id)` reads `graph_threads` row with `decision_payload`.
   - Lines 409-438: Strict idempotent replay handling returning committed state without duplicate writes.
   - Lines 481-550: Decisions update `approval_requests` and `graph_threads` atomically, inserting `approval_audit`, `domain_events`, and `outbox` records in single transaction.

---

## 2. Logic Chain

1. **From Observation 2 to Graph Persistence Test Design:**
   - Observation 2 confirms `interrupt(state["approval_request"])` is executed inside `approval()`, and `build_graph()` has no `interrupt_before`.
   - Therefore, `tests/test_graph_persistence.py` can directly assert that exactly 1 interrupt is recorded in `snap.interrupts`, upstream progression completes all 7 nodes, and `snap.tasks` contains exactly 1 pending task for `"approval"`.
   - Because `approval()` queries `deps.decision_reader(tid)` and raises `ApprovalNotCommitted`, negative tests can verify that any uncommitted resume or payload tampering (`Command(resume={"status": "approved"})`) fails closed without mutating state.
   - Because `snap.tasks` and `PostgresSaver` checkpoints persist across connections, testing process restart recovery requires only creating a second `GraphRunner` pointing to the same `pg_uri` without re-running `setup()`.

2. **From Observation 3 to Persistent Graph API Test Design:**
   - Observation 3 shows the absence of `/v1/` routes, `/health/live`, `/health/ready`, and `src/correlation.py`.
   - Therefore, `tests/test_graph_api_v1.py` must test:
     a) Versioned route coverage (`POST /v1/runs`, `GET /v1/runs/{id}`, `GET /v1/runs/{id}/approval-request`, `POST /v1/runs/{id}/resume`, `POST /v1/runs/{id}/decision-challenge`, `POST /v1/runs/{id}/decision`, `POST /v1/runs/{id}/kill`).
     b) Correlation middleware (`X-Correlation-Id` generation, echo, 422 on invalid UUID, propagation to context and logs).
     c) Health probes (`/health/live` process check vs `/health/ready` verifying DB, posture, checkpointer, and `DISPATCH_ENABLED=false`).
     d) Kill switch (`APPROVAL_CLI_HMAC_ENABLED=false` returning 403 on decisions).
     e) Safe projections omitting DB passwords, signing keys, tokens, and raw stack traces.

3. **From Observation 1 & 4 to Test Harness Architecture:**
   - Observation 1 demonstrates that tests run cleanly against `pg_uri` (`fc27_app` least-privilege connection).
   - Observation 4 shows that existing E2E tests expect `/v1/` routes.
   - Therefore, extending `tests/conftest.py` with `client_v1`, `wired_restart`, and `api_settings_factory` enables both standalone unit testing and seamless greening of `tests/e2e/test_tier1_features.py`.

---

## 3. Caveats

1. **Read-Only Scope:**
   As an explorer agent, no production code in `src/` or `migrations/` was modified during this turn. All code designs and test architectures are documented in `tests_analysis.md`.
2. **PostgreSQL Container Dependency:**
   Execution of the test suite requires a running PostgreSQL instance accessible via `TEST_POSTGRES_URI` (currently verified on `127.0.0.1:55432`).
3. **Database Roles Dependency:**
   Tests depend on the 4 roles (`fc27_owner`, `fc27_migrator`, `fc27_app`, `fc27_dispatch`) configured by Milestone 0/1. The existing fixture `pg_uri` provides the correct `fc27_app` connection.

---

## 4. Conclusion

The Milestone 2 test architecture is fully explored, specified, and structured into two primary test suites and fixture enhancements:
1. **`tests/test_graph_persistence.py`**:
   - Single interrupt verification (`len(snap.interrupts) == 1`).
   - Uncommitted resume raises `ApprovalNotCommitted`.
   - Resume payload tampering completely ignored.
   - Stale/mismatched package version fails closed.
   - Terminal state formatting (`"APPROVED, NOT DISPATCHED"` and `"REJECTED, NO ACTION TAKEN"`).
   - Checkpoint state recovery across simulated process/server restart.
   - Terminal graph re-entry protection.
2. **`tests/test_graph_api_v1.py`**:
   - Versioned `/v1/` routes (`POST /v1/runs`, `GET /v1/runs/{id}`, `GET /v1/runs/{id}/approval-request`, `POST /v1/runs/{id}/resume`, `POST /v1/runs/{id}/decision-challenge`, `POST /v1/runs/{id}/decision`, `POST /v1/runs/{id}/kill`).
   - Correlation middleware validation, generation, echo, and context propagation.
   - Safe projections asserting zero leakage of credentials, tokens, or raw traces.
   - Health probes (`/health/live` and `/health/ready`).
   - Kill switch `APPROVAL_CLI_HMAC_ENABLED=false` verification.
   - `DISPATCH_ENABLED=false` enforced across all responses.
3. **`tests/conftest.py` Fixtures**:
   - `client_v1`, `wired_restart`, `api_settings_factory`, `correlation_client`.

Full test implementation specifications, test function code, and assertion contracts are documented in `tests_analysis.md`.

---

## 5. Verification Method

1. **Verify Test Analysis Document:**
   Inspect `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_tests_1\tests_analysis.md` for complete test case designs and code specifications.

2. **Verify Baseline Test Suite Execution:**
   Run the following PowerShell command to verify the current test baseline:
   ```powershell
   $env:TEST_POSTGRES_URI = "postgresql://fc27test@127.0.0.1:55432/postgres"
   .venv\Scripts\python.exe -m pytest -o addopts="" -q -rs tests/test_langgraph_flow.py tests/test_approval_lifecycle.py tests/test_approval_security.py
   ```
   Expected: All existing tests PASS.

3. **Downstream Implementation Verification Command (when M2 implementation lands):**
   ```powershell
   $env:TEST_POSTGRES_URI = "postgresql://fc27test@127.0.0.1:55432/postgres"
   .venv\Scripts\python.exe -m pytest -o addopts="" -q -rs tests/test_graph_persistence.py tests/test_graph_api_v1.py
   ```
   Expected: 100% of Milestone 2 test cases PASS.
