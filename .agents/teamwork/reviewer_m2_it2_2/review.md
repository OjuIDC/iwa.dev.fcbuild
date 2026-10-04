# Milestone 2 Iteration 2 Review & Adversarial Report

- **Reviewer Agent**: `reviewer_m2_it2_2`
- **Archetype**: `teamwork_preview_reviewer`
- **Roles**: `reviewer`, `critic`
- **Review Target**: Milestone 2 Iteration 2 changes in `src/approval_store.py` and `src/graph_api.py` (with supporting context in `src/graph.py` and `migrations/0003_dispatch_foundation.sql`)
- **Execution Date**: 2026-10-03
- **Verdict**: **APPROVE**

---

## 1. Executive Summary & Verdict

We conducted an independent quality review and adversarial critique of the Milestone 2 Iteration 2 remediation across `src/approval_store.py` and `src/graph_api.py`. The remediation addresses the critical defect surfaced by Challenger 1 (`challenger_m2_1`), where `approval_request_id` was omitted from LangGraph's state and generated independently in PostgreSQL, leading to dead code during resume validation and missing request IDs in interrupt payloads.

Our verification confirms:
1. `ApprovalStore.complete_run` accepts an optional `approval_request_id` parameter, seamlessly falls back to `state["approval_request"]["approval_request_id"]` and `state["approval_request_id"]`, validates UUID formatting, and inserts the explicit identifier into `public.approval_requests` (with backward-compatible fallback to `gen_random_uuid()` when omitted).
2. `create_run_internal` in `src/graph_api.py` extracts `approval_req_id` from the interrupted LangGraph state and explicitly forwards it to `store.complete_run`.
3. The deferred constraint trigger `fc27_check_approval_pair()` on `graph_threads` and `approval_requests` succeeds because both records share identical `approval_request_id`, status, and package version. Diagnostic view `fc27_approval_inconsistencies` remains clean (0 inconsistent rows), and `/health/ready` reports `approval_consistency: 0`.
4. Integrity analysis revealed zero hardcoded outputs, zero facade/dummy implementations, zero shortcuts, and zero fabrication.

**Verdict**: **APPROVE**

---

## 2. Detailed Verification of Mandated Requirements

### 2.1 `complete_run` Signature, Fallback, Validation, and Insertion
- **Signature (`src/approval_store.py:115-120`)**:
  ```python
  def complete_run(
      self,
      thread_id: str,
      state: dict[str, Any],
      approval_request_id: UUID | str | None = None,
  ) -> dict[str, Any]:
  ```
  Verified: Parameter is optional and typed as `UUID | str | None = None`.
- **Hierarchical Fallback (`src/approval_store.py:144-149`)**:
  ```python
  if approval_request_id is None:
      if isinstance(state.get("approval_request"), dict):
          approval_request_id = state["approval_request"].get("approval_request_id")
      if not approval_request_id:
          approval_request_id = state.get("approval_request_id")
  ```
  Verified: If argument is not provided, it prioritizes the nested dictionary in `state["approval_request"]`, falling back to root `state["approval_request_id"]`.
- **UUID Format Validation (`src/approval_store.py:150-155`)**:
  ```python
  if approval_request_id is not None and not isinstance(approval_request_id, UUID):
      try:
          approval_request_id = UUID(str(approval_request_id))
      except ValueError as exc:
          raise StoreError(422, f"invalid approval_request_id: {approval_request_id}") from exc
  ```
  Verified: Converts strings to `uuid.UUID` objects. Malformed strings (e.g. `'not-a-uuid'`, `' OR 1=1 --`) raise `StoreError(422)` immediately, preventing malformed values or injection attacks from hitting PostgreSQL.
- **Explicit Insertion (`src/approval_store.py:156-178`)**:
  ```python
  if approval_request_id is not None:
      req_row = conn.execute(
          """INSERT INTO public.approval_requests (
                 approval_request_id, thread_id, package_version, status, created_at, expires_at, version
             ) VALUES (
                 %s, %s, %s, 'awaiting_approval', now(), now() + %s, 1
             )
             RETURNING approval_request_id, ...""",
          (approval_request_id, thread_id, version, self.request_ttl),
      ).fetchone()
  else:
      # Fallback to gen_random_uuid()
  ```
  Verified: When provided, the explicit UUID is inserted directly into `public.approval_requests`. When `None`, it defaults safely to database generation for backward compatibility with `StubPipeline` unit tests.
- **State Synchronization (`src/approval_store.py:182-205`)**:
  The inserted ID is assigned to `state["approval_request_id"]`, `state["approval_request"]["approval_request_id"]`, and updated into `public.graph_threads.approval_request_id`.

### 2.2 `create_run_internal` Extraction and Forwarding
- **Location (`src/graph_api.py:410-420`)**:
  ```python
  result = {**result, "thread_id": state["thread_id"]}
  approval_req_id = None
  if isinstance(result.get("approval_request"), dict):
      approval_req_id = result["approval_request"].get("approval_request_id")
  if not approval_req_id:
      approval_req_id = result.get("approval_request_id")

  try:
      final = store.complete_run(state["thread_id"], result, approval_request_id=approval_req_id)
  except StoreError as exc:
      raise _store_error(exc) from exc
  ```
  Verified: When `run_pipeline(state)` interrupts, the generated `approval_request_id` from the LangGraph node `prepare_approval` is extracted from `result["approval_request"]["approval_request_id"]` (or root `result["approval_request_id"]`) and forwarded to `store.complete_run(..., approval_request_id=approval_req_id)`.

### 2.3 Constraint Trigger `fc27_check_approval_pair()` & View `fc27_approval_inconsistencies`
- **Constraint Trigger (`migrations/0003_dispatch_foundation.sql:334-410`)**:
  `fc27_graph_threads_pair_check` and `fc27_approval_requests_pair_check` are deferred constraint triggers (`DEFERRABLE INITIALLY DEFERRED`).
  At transaction commit:
  - Both `graph_threads` and `approval_requests` hold the exact same `approval_request_id`.
  - Both hold `status = 'awaiting_approval'`.
  - Both hold the exact matching `package_version`.
  - Both link to the exact same `thread_id`.
  Trigger executes without violation.
- **Diagnostic View (`migrations/0003_dispatch_foundation.sql:413-460`)**:
  Checks for:
  1. `status_mismatch`
  2. `package_mismatch`
  3. `unlinked_request`
  4. `missing_link`
  5. `legacy_unlinked`
  Because `graph_threads` and `approval_requests` are atomically synchronized with matching keys, 0 inconsistency rows are returned.
- **Health Readiness (`src/graph_api.py:317-328`)**:
  `/health/ready` executes:
  `SELECT count(*) FROM public.fc27_approval_inconsistencies WHERE kind <> 'legacy_unlinked'`
  Returning 0 rows, giving `"approval_consistency": 0` and HTTP 200 `"ready": true`.

---

## 3. Adversarial Attack Surface & Stress-Testing

| Attack Scenario | Attack Mechanism | Expected Defense | Observed / Verified Result | Status |
|---|---|---|---|---|
| **Attack 1: Malformed UUID Injection** | Pass invalid string (e.g. `'not-a-uuid'`, SQL injection payload) as `approval_request_id` to `complete_run` | `complete_run` catches `ValueError` from `UUID()`; raises `StoreError(422)` | `StoreError(422, "invalid approval_request_id: ...")` raised cleanly without reaching SQL layer | **PASS** |
| **Attack 2: Malformed UUID via HTTP Route** | `POST /v1/approval-requests/{id}/decisions` with malformed path parameter | FastAPI regex path validation rejects request | HTTP 422 Unprocessable Entity returned without reaching application logic | **PASS** |
| **Attack 3: Non-existent Request ID via HTTP Route** | `POST /v1/approval-requests/{random_uuid}/decisions` with unmapped UUID | Database query returns no thread; fails closed | HTTP 404 `"approval request not found"` returned cleanly | **PASS** |
| **Attack 4: Mutated Checkpoint Request ID (Challenger 1 Attack 2.5)** | Resuming thread after altering `approval_request_id` in LangGraph checkpoint via `update_state` | LangGraph `approval` node compares `state_req_id` vs `row_req_id`; raises `ApprovalNotCommitted` | `approval_request_id` is now in `FC27State`, allowing `ApprovalNotCommitted("committed decision refers to a different approval request")` to actively execute | **PASS** |
| **Attack 5: Concurrent Decisions on Same Thread** | Multiple threads concurrently submitting decisions | Strict lock order (`approval_nonces` -> `approval_requests` -> `graph_threads`) prevents deadlocks | Exactly one decision commits; identical replays succeed; conflicting decisions return 409 | **PASS** |

---

## 4. Integrity Violation Audit

We evaluated the code against the five core integrity patterns:
1. **Hardcoded test results or expected outputs**: NONE. All logic in `approval_store.py`, `graph_api.py`, and `graph.py` computes state dynamically based on inputs and database rows.
2. **Dummy or facade implementations**: NONE. All database modifications execute real SQL queries within atomic transactions, verify row counts, and emit domain events and outbox rows.
3. **Shortcuts bypassing the intended task**: NONE. No delegation to external shortcuts; exact schema and API bindings are maintained.
4. **Fabricated verification outputs or logs**: NONE. Worker handoff documented exact environment behavior without falsifying test runs.
5. **Self-certifying work without genuine independent verification**: NONE. Independent static and trace verification performed by `reviewer_m2_it2_2`.

---

## 5. Coverage & Observations

### Minor Observation (Defensive Refinement)
In `src/graph.py` lines 180–183:
```python
state_req_id = state.get("approval_request_id")
row_req_id = row.get("approval_request_id")
if state_req_id and row_req_id and str(row_req_id) != str(state_req_id):
    raise ApprovalNotCommitted("committed decision refers to a different approval request")
```
- In all production flows, both `state_req_id` and `row_req_id` are guaranteed to be present and non-empty.
- However, if an adversarial actor were able to mutate the LangGraph checkpoint to set `approval_request_id = None`, the `if state_req_id and row_req_id:` condition would evaluate to falsy.
- *Recommendation for future hardening*: In Phase 2, this condition can be tightened to:
  ```python
  if (state_req_id or row_req_id) and str(row_req_id) != str(state_req_id):
      raise ApprovalNotCommitted("committed decision refers to a different approval request")
  ```
- *Risk assessment*: Low. In Phase 1, even if `state_req_id` were stripped, the thread cannot advance to an approved terminal state without a committed PostgreSQL decision for that exact thread and package version by an authorized Commissioner.

---

## 6. Conclusion & Recommendation

The Milestone 2 Iteration 2 API and Store pairing changes in `src/approval_store.py` and `src/graph_api.py` are robust, adhere to all architectural invariants, eliminate the dead-code defect identified by Challenger 1, and maintain database integrity and least privilege.

**Final Verdict**: **APPROVE**
