# Milestone 2 Iteration 2: API & Store Fix Strategy Analysis

- **Agent**: `explorer_m2_it2_api_1` (teamwork_preview_explorer)
- **Target Files**: `src/graph_api.py`, `src/approval_store.py`
- **Related Files**: `src/graph.py`, `migrations/0003_dispatch_foundation.sql`, `tests/test_graph_persistence.py`
- **Date**: 2026-10-03
- **Status**: Investigation Complete — Actionable Proposals Prepared

---

## 1. Executive Summary & Root Cause Analysis

During Milestone 2 Iteration 1 adversarial review (`challenger_m2_1`), a critical architectural defect was identified:
1. In `src/graph.py`, `FC27State` omitted `approval_request_id`.
2. When the graph interrupted at `approval`, the interrupt payload emitted `"approval_request_id": None` (violating Spec §4.3).
3. The guard at `src/graph.py` lines 176–179:
   ```python
   state_req_id = state.get("approval_request_id")
   row_req_id = row.get("approval_request_id")
   if state_req_id and row_req_id and str(row_req_id) != str(state_req_id):
       raise ApprovalNotCommitted("committed decision refers to a different approval request")
   ```
   evaluated to dead code because `state_req_id` was `None` (violating Spec §4.4).
4. In `src/graph_api.py:create_run_internal`, the graph runs to interruption *before* `store.complete_run` is called.
5. In `src/approval_store.py:complete_run`, PostgreSQL currently generates a brand-new random UUID via `DEFAULT gen_random_uuid()` during `INSERT INTO public.approval_requests`.
6. **The Critical Binding Problem**: If `prepare_approval` in `src/graph.py` generates `approval_request_id` ahead of the interrupt so the checkpoint and interrupt payload retain it, but `store.complete_run` generates a separate UUID in PostgreSQL, the LangGraph checkpoint ID and PostgreSQL row ID will permanently diverge! Upon resume, `row_req_id != state_req_id` would falsely trigger `ApprovalNotCommitted`, breaking valid approvals.

**Solution**:
`src/graph_api.py:create_run_internal` must extract `approval_request_id` from the interrupted graph state (`result["approval_request"]["approval_request_id"]`) and pass it to `store.complete_run`. In turn, `store.complete_run` must accept this ID and use it when inserting into `public.approval_requests`, ensuring PostgreSQL and LangGraph remain 100% synchronized and satisfying the database trigger `fc27_check_approval_pair()`.

---

## 2. Interaction Trace: `create_run_internal` ↔ `complete_run`

### Current Execution Flow:
```
HTTP POST /v1/runs
  │
  ▼
create_run_internal(req)
  │
  ├── 1. store.create_run(...) -> inserts graph_threads (status='running')
  │
  ├── 2. run_pipeline(state) -> GraphRunner.start(state)
  │        ├── search -> scout -> analyst -> coach -> tactics -> director
  │        ├── prepare_approval(state):
  │        │     computes lead_packages, package_version
  │        │     builds approval_request dict
  │        │     [ITERATION 2 FIX: generates req_id = str(uuid4()), populates request["approval_request_id"]]
  │        └── approval(state):
  │              interrupt(state["approval_request"])   <-- PAUSES HERE
  │
  ├── 3. runner.start returns state values dict (as `result`)
  │        `result["approval_request"]["approval_request_id"]` contains `req_id`
  │        `result["status"]` is 'awaiting_approval'
  │
  ├── 4. store.complete_run(thread_id, result)  <-- CURRENT DEFECT HERE:
  │        Does NOT accept or forward `approval_request_id`.
  │        PostgreSQL executes INSERT without approval_request_id, generating a DISJOINT UUID!
  │        Updates graph_threads.approval_request_id to this new disjoint UUID.
  │
  └── 5. POST /v1/runs returns RunProjection
```

### Required Iteration 2 Flow:
1. `create_run_internal` inspects `result`:
   ```python
   approval_req_id = None
   if isinstance(result.get("approval_request"), dict):
       approval_req_id = result["approval_request"].get("approval_request_id")
   if not approval_req_id:
       approval_req_id = result.get("approval_request_id")
   ```
2. `create_run_internal` invokes:
   ```python
   final = store.complete_run(state["thread_id"], result, approval_request_id=approval_req_id)
   ```
3. `complete_run` inserts the exact same `approval_request_id` into `public.approval_requests` and links `public.graph_threads.approval_request_id`.
4. PostgreSQL state matches LangGraph checkpoint state byte-for-byte.

---

## 3. Analysis of `src/approval_store.py:complete_run`

### 3.1 Current Method Signature & Behavior
In `src/approval_store.py:115`:
```python
def complete_run(self, thread_id: str, state: dict[str, Any]) -> dict[str, Any]:
```
Currently:
- Accepts only `thread_id` and `state`.
- Line 140–148:
  ```python
  req_row = conn.execute(
      """INSERT INTO public.approval_requests (
             thread_id, package_version, status, created_at, expires_at, version
         ) VALUES (
             %s, %s, 'awaiting_approval', now(), now() + %s, 1
         )
         RETURNING approval_request_id,
                   to_char(expires_at AT TIME ZONE 'utc', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS approval_expires_at""",
      (thread_id, version, self.request_ttl),
  ).fetchone()
  ```
- Because column `approval_request_id` is omitted from the `INSERT`, PostgreSQL defaults to `gen_random_uuid()`.

### 3.2 Enhanced Signature & Implementation Strategy
To provide **defense-in-depth**, `complete_run` should:
1. Accept `approval_request_id: UUID | str | None = None` as an optional parameter.
2. If `approval_request_id is None`, check whether `state` contains it (`state.get("approval_request", {}).get("approval_request_id")` or `state.get("approval_request_id")`).
3. If still `None` (as occurs in unit tests with `StubPipeline` or direct store tests), fall back cleanly to PostgreSQL's default `DEFAULT gen_random_uuid()`.
4. If provided as `str`, validate and cast to `UUID` (raising `StoreError(422)` on malformed UUID strings).
5. Execute the `INSERT INTO public.approval_requests` with the provided ID when present, or omit to let PostgreSQL generate it when absent.
6. Return `state` with both `state["approval_request_id"]` and `state["approval_request"]["approval_request_id"]` synchronized to `str(inserted_id)`.

This design preserves 100% backward compatibility for all existing tests (`tests/test_approval_lifecycle.py`, `tests/test_adversarial_m1.py`, etc.) while cleanly accepting graph-originated IDs.

---

## 4. Trigger Consistency with `fc27_check_approval_pair()`

In `migrations/0003_dispatch_foundation.sql:334-410`, two paired constraint triggers enforce 1:1 pairing between `graph_threads` and `approval_requests`:
- `fc27_graph_threads_pair_check` on `graph_threads` (`AFTER INSERT OR UPDATE`, `DEFERRABLE INITIALLY DEFERRED`)
- `fc27_approval_requests_pair_check` on `approval_requests` (`AFTER INSERT OR UPDATE`, `DEFERRABLE INITIALLY DEFERRED`)

### Trigger Invariants Checked at Commit:
```sql
-- On graph_threads:
IF NEW.status = 'awaiting_approval' AND NEW.package_version IS NOT NULL AND NEW.approval_request_id IS NULL THEN
    RAISE EXCEPTION 'approval pair: awaiting thread requires link' ...;
END IF;
-- And when linked:
-- v_req.status == NEW.status
-- v_req.package_version IS NOT DISTINCT FROM NEW.package_version
-- v_req.thread_id == NEW.thread_id

-- On approval_requests:
-- v_thread.approval_request_id IS NOT DISTINCT FROM NEW.approval_request_id
-- v_thread.status == NEW.status
-- v_thread.package_version IS NOT DISTINCT FROM NEW.package_version
```

### Atomic Paired Transaction in `complete_run`:
```python
with self.connect() as conn, conn.transaction():
    # 1. Insert approval_requests with approval_request_id = target_id, thread_id = thread_id, status = 'awaiting_approval', package_version = version
    # 2. Update graph_threads set status = 'awaiting_approval', package_version = version, approval_request_id = target_id where thread_id = thread_id
```
Because both operations execute inside the same `conn.transaction()` block:
1. `NEW.approval_request_id` on `approval_requests` matches `graph_threads.approval_request_id` exactly (`target_id`).
2. Both statuses are `'awaiting_approval'`.
3. Both package versions match `version`.
4. Both thread IDs match `thread_id`.
5. Upon transaction commit, the deferred triggers validate all conditions and succeed without error.

---

## 5. Exact Recommended Code Modifications for Worker

### Modification 1: `src/approval_store.py`

**Location**: Lines 115–186 in `src/approval_store.py`

#### Before:
```python
    def complete_run(self, thread_id: str, state: dict[str, Any]) -> dict[str, Any]:
        status = state.get("status")
        if status not in ("awaiting_approval", "failed"):
            raise StoreError(500, "pipeline returned an invalid status")
        version = package_version(state.get("lead_packages")) if status == "awaiting_approval" else None
        if status == "awaiting_approval" and version is None:
            status = "failed"
            state = {**state, "status": "failed", "errors": list(state.get("errors", [])) + ["no lead packages produced"]}
        state = {**state, "package_version": version}

        with self.connect() as conn, conn.transaction():
            if status == "failed":
                row = conn.execute(
                    """UPDATE public.graph_threads
                          SET state = %s, status = 'failed', package_version = NULL, updated_at = now()
                        WHERE thread_id = %s AND status = 'running'
                    RETURNING thread_id""",
                    (Jsonb(state), thread_id),
                ).fetchone()
                if row is None:
                    raise StoreError(409, f"thread {thread_id} is no longer running")
                return state

            # status == 'awaiting_approval' with version
            req_row = conn.execute(
                """INSERT INTO public.approval_requests (
                       thread_id, package_version, status, created_at, expires_at, version
                   ) VALUES (
                       %s, %s, 'awaiting_approval', now(), now() + %s, 1
                   )
                   RETURNING approval_request_id,
                             to_char(expires_at AT TIME ZONE 'utc', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS approval_expires_at""",
                (thread_id, version, self.request_ttl),
            ).fetchone()
            if req_row is None:
                raise StoreError(500, "failed to insert approval_request")

            approval_request_id: UUID = req_row["approval_request_id"]
            approval_expires_at: str = req_row["approval_expires_at"]

            state = {
                **state,
                "approval_request_id": str(approval_request_id),
                "approval_expires_at": approval_expires_at,
            }

            thread_row = conn.execute(
                """UPDATE public.graph_threads
                      SET state = %s, status = 'awaiting_approval', package_version = %s,
                          approval_request_id = %s, updated_at = now()
                    WHERE thread_id = %s AND status = 'running'
                RETURNING thread_id, club_id""",
                (Jsonb(state), version, approval_request_id, thread_id),
            ).fetchone()
            if thread_row is None:
                raise StoreError(409, f"thread {thread_id} is no longer running")

            _safe_record_event(
                conn,
                event_type="approval.created",
                entity_type="approval_request",
                entity_id=str(approval_request_id),
                actor="system",
                correlation_id=uuid4(),
                visibility="staff",
                scope_club_id=thread_row["club_id"],
                source_table="approval_requests",
                source_id=str(approval_request_id),
                to_status="awaiting_approval",
            )

        return state
```

#### After:
```python
    def complete_run(
        self,
        thread_id: str,
        state: dict[str, Any],
        approval_request_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        status = state.get("status")
        if status not in ("awaiting_approval", "failed"):
            raise StoreError(500, "pipeline returned an invalid status")
        version = package_version(state.get("lead_packages")) if status == "awaiting_approval" else None
        if status == "awaiting_approval" and version is None:
            status = "failed"
            state = {**state, "status": "failed", "errors": list(state.get("errors", [])) + ["no lead packages produced"]}
        state = {**state, "package_version": version}

        with self.connect() as conn, conn.transaction():
            if status == "failed":
                row = conn.execute(
                    """UPDATE public.graph_threads
                          SET state = %s, status = 'failed', package_version = NULL, updated_at = now()
                        WHERE thread_id = %s AND status = 'running'
                    RETURNING thread_id""",
                    (Jsonb(state), thread_id),
                ).fetchone()
                if row is None:
                    raise StoreError(409, f"thread {thread_id} is no longer running")
                return state

            # status == 'awaiting_approval' with version
            if approval_request_id is None:
                if isinstance(state.get("approval_request"), dict):
                    approval_request_id = state["approval_request"].get("approval_request_id")
                if not approval_request_id:
                    approval_request_id = state.get("approval_request_id")

            if approval_request_id is not None and not isinstance(approval_request_id, UUID):
                try:
                    approval_request_id = UUID(str(approval_request_id))
                except ValueError as exc:
                    raise StoreError(422, f"invalid approval_request_id: {approval_request_id}") from exc

            if approval_request_id is not None:
                req_row = conn.execute(
                    """INSERT INTO public.approval_requests (
                           approval_request_id, thread_id, package_version, status, created_at, expires_at, version
                       ) VALUES (
                           %s, %s, %s, 'awaiting_approval', now(), now() + %s, 1
                       )
                       RETURNING approval_request_id,
                                 to_char(expires_at AT TIME ZONE 'utc', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS approval_expires_at""",
                    (approval_request_id, thread_id, version, self.request_ttl),
                ).fetchone()
            else:
                req_row = conn.execute(
                    """INSERT INTO public.approval_requests (
                           thread_id, package_version, status, created_at, expires_at, version
                       ) VALUES (
                           %s, %s, 'awaiting_approval', now(), now() + %s, 1
                       )
                       RETURNING approval_request_id,
                                 to_char(expires_at AT TIME ZONE 'utc', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS approval_expires_at""",
                    (thread_id, version, self.request_ttl),
                ).fetchone()

            if req_row is None:
                raise StoreError(500, "failed to insert approval_request")

            inserted_id: UUID = req_row["approval_request_id"]
            approval_expires_at: str = req_row["approval_expires_at"]

            state = {
                **state,
                "approval_request_id": str(inserted_id),
                "approval_expires_at": approval_expires_at,
            }
            if isinstance(state.get("approval_request"), dict):
                state["approval_request"] = {
                    **state["approval_request"],
                    "approval_request_id": str(inserted_id),
                }

            thread_row = conn.execute(
                """UPDATE public.graph_threads
                      SET state = %s, status = 'awaiting_approval', package_version = %s,
                          approval_request_id = %s, updated_at = now()
                    WHERE thread_id = %s AND status = 'running'
                RETURNING thread_id, club_id""",
                (Jsonb(state), version, inserted_id, thread_id),
            ).fetchone()
            if thread_row is None:
                raise StoreError(409, f"thread {thread_id} is no longer running")

            _safe_record_event(
                conn,
                event_type="approval.created",
                entity_type="approval_request",
                entity_id=str(inserted_id),
                actor="system",
                correlation_id=uuid4(),
                visibility="staff",
                scope_club_id=thread_row["club_id"],
                source_table="approval_requests",
                source_id=str(inserted_id),
                to_status="awaiting_approval",
            )

        return state
```

---

### Modification 2: `src/graph_api.py`

**Location**: Lines 404–416 in `src/graph_api.py` (`create_run_internal`)

#### Before:
```python
        result = {**result, "thread_id": state["thread_id"]}
        try:
            final = store.complete_run(state["thread_id"], result)
        except StoreError as exc:
            raise _store_error(exc) from exc
```

#### After:
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

---

## 6. End-to-End Verification Method

1. **Verify Adversarial Mismatch Test**:
   In `tests/test_graph_persistence.py`, remove `@pytest.mark.xfail` from `test_adversarial_resume_refusal_mismatched_approval_request_id`.
   When `approval_request_id` is retained in `FC27State` and bound correctly to PostgreSQL:
   - Valid runs execute and resume cleanly.
   - Injecting a mismatched ID via `runner.app.update_state(...)` actively raises `ApprovalNotCommitted("committed decision refers to a different approval request")`.
   - The test passes cleanly without `xfail`.

2. **Verify API Decision Paths**:
   - `GET /v1/runs/{thread_id}/approval-request` returns the exact `approval_request_id`.
   - `POST /v1/approval-requests/{approval_request_id}/decisions` finds the matching row and commits the decision.
   - Resuming the thread succeeds with `status = "APPROVED, NOT DISPATCHED"`.

3. **Verify Regression Safety**:
   - Run the full test suite:
     - `tests/test_approval_lifecycle.py`
     - `tests/test_approval_security.py`
     - `tests/test_graph_api_v1.py`
     - `tests/test_graph_persistence.py`
     - `tests/test_langgraph_flow.py`
   - All tests must pass with 0 regressions.
