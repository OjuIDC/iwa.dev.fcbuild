# Adversarial Challenge Report: Milestone 2 Iteration 2 (API & Store Integration)

- **Challenger Agent**: `challenger_m2_it2_2` (teamwork_preview_challenger)
- **Target Roles**: `worker_m2_2` (Graph Core & Persistent Graph API), `worker_m2_1`
- **Target Files**: `src/approval_store.py`, `src/graph_api.py`, `src/graph.py`
- **Execution Date**: 2026-10-03
- **Verdict**: **APPROVE**

---

## Executive Summary

We conducted an adversarial empirical challenge against the API and Store integration in `src/approval_store.py`, `src/graph_api.py`, and `src/graph.py`, evaluating the remediation implemented by `worker_m2_2` for the previous gate failure (unbound `approval_request_id` in LangGraph state).

We probed four mandatory attack dimensions:
1. **Identifier Projection & Checkpoint Binding**: Creating runs via `POST /v1/runs` and empirically tracing `approval_request_id` through the response projection, PostgreSQL `public.graph_threads`, PostgreSQL `public.approval_requests`, LangGraph checkpoint channel values, and the `prepare_approval` interrupt payload.
2. **Request-Centric Decision Resolution & Lifecycle**: Submitting decisions via `POST /v1/approval-requests/{approval_request_id}/decisions`, verifying thread resolution, atomic PostgreSQL state transition, clean LangGraph resumption to `"APPROVED, NOT DISPATCHED"`, idempotent retries, and non-existent request handling.
3. **Input Tampering & Malformed UUID Injection**: Directly injecting malformed UUID strings, SQL injection snippets, and invalid types into `store.complete_run` and the HTTP API routes to verify graceful 422 error handling without unhandled database exceptions.
4. **Trigger Integrity & Concurrency Resilience**: Adversarially mutating database rows to challenge the `fc27_check_approval_pair()` constraint trigger and immutability guards, and stress-testing the system under concurrent multi-threaded decision submissions.

**Result**: All 79 tests passed cleanly across `tests/test_graph_api_v1.py` and `tests/test_approval_lifecycle.py` in 86.43s. The integration is robust, strictly consistent, and defends in depth.

**Overall Risk Assessment**: **LOW**

---

## Adversarial Challenges & Findings

### Challenge 1 [Low - Passed]: Cross-Layer Binding of `approval_request_id`
- **Assumption Challenged**: The `approval_request_id` generated during `prepare_approval` is faithfully propagated through the entire stack without divergence or desynchronization between PostgreSQL and LangGraph checkpoint memory.
- **Attack Scenario**: Create a graph run via `POST /v1/runs` using a real `GraphRunner` and PostgreSQL checkpointer. Extract `approval_request_id` from the HTTP 201 response. Inspect the underlying PostgreSQL rows in `graph_threads` and `approval_requests`. Fetch the exact checkpoint state and interrupt payload from `runner.app.get_state({"configurable": {"thread_id": tid}})`. Assert all IDs are identical, non-null, and canonical UUIDs.
- **Blast Radius If Failed**: If IDs diverge, resumption would fail closed or validate against the wrong approval request.
- **Empirical Result**: **PASS**.
  - `POST /v1/runs` response returned canonical UUID `approval_request_id`.
  - `public.graph_threads.approval_request_id` matches exactly.
  - `public.approval_requests.approval_request_id` matches exactly and binds `thread_id` and `package_version`.
  - LangGraph checkpoint channel value `approval_request_id` matches exactly.
  - Snapshot interrupt payload `snap.interrupts[0].value["approval_request_id"]` matches exactly.
  - The dead-code defect identified in Iteration 1 is completely resolved.

### Challenge 2 [Low - Passed]: Thread Resolution via Request-Centric Decision Route
- **Assumption Challenged**: Submitting a signed decision to `POST /v1/approval-requests/{approval_request_id}/decisions` resolves the correct `thread_id`, commits the decision to PostgreSQL before resuming the graph, reaches the exact invariant terminal state `"APPROVED, NOT DISPATCHED"`, and safely handles idempotent retries.
- **Attack Scenario**:
  1. Create a run, issue an HMAC challenge, sign the payload, and POST to `/v1/approval-requests/{approval_request_id}/decisions`.
  2. Verify HTTP 200 response contains `"status": "approved"`, `"graph_resume": "resumed"`, `"dispatch": {"enabled": false}`.
  3. Verify PostgreSQL rows in `graph_threads`, `approval_requests`, and `approval_audit` are updated atomically.
  4. Verify LangGraph state transitions to `"APPROVED, NOT DISPATCHED"`.
  5. Resubmit the exact same signed decision payload: verify HTTP 200 idempotent response without duplicate audits.
  6. Submit decision to a non-existent UUID: verify clean HTTP 404.
- **Blast Radius If Failed**: Misrouted decisions, duplicate commits, or unhandled 500 errors.
- **Empirical Result**: **PASS**. All assertions passed without error.

### Challenge 3 [Low - Passed]: Malformed UUID Injection Handling
- **Assumption Challenged**: Callers supplying non-UUID strings, SQL injection strings, or invalid data types to `store.complete_run` or API endpoints could trigger unhandled database driver crashes (`psycopg.errors.DataError`, `InvalidTextRepresentation`) or SQL injection vulnerabilities.
- **Attack Scenario**:
  1. Call `store.complete_run` with malformed strings: `"not-a-uuid"`, `"' OR 1=1 --"`, `"12345"`, `""`, `"g" * 36`, `"550e8400-e29b-41d4-a716"`, `12345`.
  2. POST to `/v1/approval-requests/{approval_request_id}/decisions` with malformed path parameters: `"not-a-uuid"`, `"12345"`, `"'; DROP TABLE..."`.
- **Blast Radius If Failed**: SQL injection, unhandled 500 internal server errors, or process crashes.
- **Empirical Result**: **PASS**.
  - `store.complete_run` cleanly caught non-UUID values at line 150-154, raising `StoreError(422, "invalid approval_request_id: ...")` before executing any SQL queries.
  - FastAPI path validation regex (`pattern=r"^[0-9a-f-]{36}$"`) cleanly rejected malformed path strings with HTTP 422 before invoking endpoint logic.

### Challenge 4 [Low - Passed]: Defense-in-Depth Database Integrity and Concurrency
- **Assumption Challenged**: An attacker or rogue worker attempting to bypass the application layer could mutate `graph_threads` or `approval_requests` directly, or concurrent decision submissions could cause race conditions, deadlocks, or approval inconsistencies.
- **Attack Scenario**:
  1. Attempt direct update of `graph_threads.status = 'approved'` without updating `approval_requests`.
  2. Attempt direct update of `package_version` on `graph_threads` and `approval_requests`.
  3. Attempt direct mutation of `approval_request_id` on `graph_threads`.
  4. Attempt inserting an awaiting thread with `package_version` set but `approval_request_id = NULL`.
  5. Attempt inserting an `approval_requests` row for an unlinked running thread.
  6. Concurrently execute 6 threads creating runs, issuing challenges, and submitting decisions.
- **Blast Radius If Failed**: Inconsistent state between approval requests and graph threads, silent approval bypass, or database deadlocks.
- **Empirical Result**: **PASS**.
  - Multiple defensive layers actively block rogue mutations:
    - `graph_threads_terminal_fields_chk`: Blocks setting status to terminal without `approved_by`, `approved_at`, and `decision_payload`.
    - `fc27_guard_thread_transition()`: Blocks mutating `package_version` (`package_version is immutable`) and mutating `approval_request_id` (`approval_request_id is immutable once set`).
    - `fc27_guard_approval_request_transition()`: Blocks mutating `package_version` on `approval_requests`.
    - `graph_threads_approval_request_id_key` & `approval_requests_thread_id_key`: Enforce strict 1-to-1 uniqueness.
    - `fc27_check_approval_pair()`: Blocks status mismatches, unlinked awaiting threads, and cross-thread linkage on transaction commit.
  - Concurrency stress test executed 6 parallel threads with 0 deadlocks and 0 inconsistencies in `public.fc27_approval_inconsistencies`.

---

## Stress Test Results

| # | Attack Scenario | Expected Behavior | Actual Behavior | Result |
|---|---|---|---|---|
| **V1.1** | `POST /v1/runs` response projection verification | `approval_request_id` is non-null canonical UUID | Returned valid canonical UUID string | **PASS** |
| **V1.2** | PostgreSQL row consistency on run creation | `graph_threads.approval_request_id` == `approval_requests.approval_request_id` == response ID | Exact match across all rows and response | **PASS** |
| **V1.3** | LangGraph checkpoint state binding | Checkpoint channel value `approval_request_id` matches response ID | Checkpoint channel value matches exactly | **PASS** |
| **V1.4** | LangGraph interrupt payload binding | Interrupt payload dict contains matching `approval_request_id` | `snap.interrupts[0].value["approval_request_id"]` matches | **PASS** |
| **V2.1** | Request-centric decision submission (`POST /v1/approval-requests/{id}/decisions`) | Resolves thread, commits DB, resumes graph, returns 200 `approved` | Returned 200, status `approved`, resume `resumed` | **PASS** |
| **V2.2** | PostgreSQL terminal updates after request-centric decision | `graph_threads` and `approval_requests` both updated to `approved` | Both tables updated to `approved` atomically | **PASS** |
| **V2.3** | LangGraph terminal state after resume | Terminal state is `"APPROVED, NOT DISPATCHED"` | LangGraph state reached `"APPROVED, NOT DISPATCHED"` | **PASS** |
| **V2.4** | Idempotent decision retry on `/v1/approval-requests/{id}/decisions` | Returns HTTP 200 with replayed state, no new audit row | Returned HTTP 200 `approved`, single audit preserved | **PASS** |
| **V2.5** | Decision on non-existent `approval_request_id` | Returns HTTP 404 `"approval request not found"` | Returned HTTP 404 `"approval request not found"` | **PASS** |
| **V3.1** | `store.complete_run` with malformed string UUIDs (`not-a-uuid`, SQL injection, integers) | Raises `StoreError(422, "invalid approval_request_id")`, no DB crash | Raised `StoreError(422)` cleanly for all inputs | **PASS** |
| **V3.2** | HTTP route `/v1/approval-requests/{bad_id}/decisions` with non-UUID path | FastAPI rejects with HTTP 422, does not hit DB | Returned HTTP 422 for all malformed paths | **PASS** |
| **V4.1** | Manual update of `graph_threads.status = 'approved'` without `approval_requests` update | Deferred trigger `fc27_check_approval_pair` raises `check_violation` | Blocked with `approval pair: status mismatch` | **PASS** |
| **V4.2** | Manual update of `package_version` on `graph_threads` | `fc27_guard_thread_transition` blocks modification | Blocked with `package_version is immutable` | **PASS** |
| **V4.3** | Manual update of `package_version` on `approval_requests` | `fc27_guard_approval_request_transition` blocks modification | Blocked with `package_version is immutable` | **PASS** |
| **V4.4** | Manual update of `graph_threads.approval_request_id` | `fc27_guard_thread_transition` blocks mutation | Blocked with `approval_request_id is immutable once set` | **PASS** |
| **V4.5** | Insert rogue `approval_requests` row for unlinked running thread | Deferred trigger `fc27_check_approval_pair` raises `check_violation` | Blocked with `approval pair: thread ... does not link to approval_request` | **PASS** |
| **V4.6** | Insert awaiting thread with package version but NULL `approval_request_id` | Deferred trigger `fc27_check_approval_pair` raises `check_violation` | Blocked with `approval pair: awaiting thread requires link` | **PASS** |
| **V4.7** | Concurrent decision submissions across 6 threads | 0 deadlocks, 0 trigger violations, 0 inconsistencies in diagnostic view | All 6 succeeded, 0 inconsistencies reported | **PASS** |

---

## Test Execution Summary

- Test suites executed:
  - `tests/test_graph_api_v1.py`
  - `tests/test_approval_lifecycle.py`
- Test run command:
  ```powershell
  $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_graph_api_v1.py tests/test_approval_lifecycle.py
  ```
- Result: **79 passed in 86.43s (0:01:26)**
- Exit Code: **0**

---

## Unchallenged Areas

- **Phase 2-6 Features** (Player passkeys, WebAuthn step-up, Outbox worker dispatch): Intentionally out of scope per Phase 1 invariants and non-negotiables.

---

## Verdict

**VERDICT**: **APPROVE**

The API and Store integration in `src/approval_store.py` and `src/graph_api.py` fully satisfies all Milestone 2 Phase 1 requirements. The prior gate failure has been completely and robustly resolved with end-to-end consistency across FastAPI projections, PostgreSQL tables, and LangGraph checkpoints.
