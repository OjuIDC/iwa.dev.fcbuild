# Milestone 2 Iteration 2 Challenger Handoff Report

- **Agent**: `challenger_m2_it2_1` (teamwork_preview_challenger)
- **Roles**: critic, specialist
- **Target Files**: `src/graph.py`, `src/approval_store.py`, `src/graph_api.py`, `tests/test_graph_persistence.py`
- **Date**: 2026-10-03
- **Parent Conversation ID**: `7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b`
- **Verdict**: **APPROVE**

---

## 1. Observation

### 1.1 Direct Inspection of Source Code
1. `src/graph.py`:
   - Line 27: `from uuid import uuid4`
   - Line 66: `approval_request_id: str | None` declared in `FC27State(TypedDict, total=False)`.
   - Lines 145–160: In `prepare_approval(state)`:
     ```python
     req_id = state.get("approval_request_id") or str(uuid4())
     request = {
         "thread_id": state["thread_id"],
         "package_version": version,
         "package_hash": version,
         "approvable_owners": approvable,
         "approval_request_id": req_id,
         ...
     }
     return {
         "lead_packages": packages,
         "package_version": version,
         "approval_request_id": req_id,
         "approval_request": request,
         ...
     }
     ```
   - Lines 180–183: In `approval(state)`:
     ```python
     state_req_id = state.get("approval_request_id")
     row_req_id = row.get("approval_request_id")
     if state_req_id and row_req_id and str(row_req_id) != str(state_req_id):
         raise ApprovalNotCommitted("committed decision refers to a different approval request")
     ```
2. `src/approval_store.py`:
   - Lines 115–120: `complete_run(..., approval_request_id: UUID | str | None = None)` accepts `approval_request_id`.
   - Lines 144–178: Extracts `approval_request_id` from state/dict if omitted, validates UUID syntax (raising `StoreError(422)` on invalid UUID), and inserts explicit `approval_request_id` into `public.approval_requests`.
   - Lines 196–203: Updates `public.graph_threads.approval_request_id = inserted_id`.
3. `src/graph_api.py`:
   - Lines 411–418: `create_run_internal` extracts `approval_req_id` from `result["approval_request"]["approval_request_id"]` (or `result["approval_request_id"]`) and passes `store.complete_run(state["thread_id"], result, approval_request_id=approval_req_id)`.
4. `tests/test_graph_persistence.py`:
   - Lines 80–81: `test_single_approval_interrupt_at_approval_node` asserts:
     ```python
     assert values.get("approval_request_id") is not None
     assert snap.interrupts[0].value.get("approval_request_id") == values["approval_request_id"]
     ```
   - Line 228: `test_checkpoint_recovery_across_server_restart` asserts:
     ```python
     assert values2.get("approval_request_id") == state1.get("approval_request_id")
     ```
   - Line 532: `test_adversarial_resume_refusal_mismatched_approval_request_id` contains NO `@pytest.mark.xfail` decorator.

### 1.2 Direct Empirical Test Execution
1. Specific Attack 2.5 execution:
   - Command:
     ```powershell
     $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -v tests/test_graph_persistence.py -k test_adversarial_resume_refusal_mismatched_approval_request_id
     ```
   - Result:
     ```text
     tests/test_graph_persistence.py::test_adversarial_resume_refusal_mismatched_approval_request_id PASSED [100%]
     1 passed, 19 deselected in 1.79s
     ```
   - Exit code: `0`.
2. Complete persistence and adversarial suite execution:
   - Command:
     ```powershell
     $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_graph_persistence.py
     ```
   - Result:
     ```text
     ....................                                                     [100%]
     20 passed in 32.21s
     ```
   - Exit code: `0`.

---

## 2. Logic Chain

1. **Gate Defect Cause (Observation 1.1 -> Step 1)**:
   In Milestone 2 Iteration 1, `FC27State` did not include `approval_request_id`. When LangGraph processed state dictionaries, any unlisted key was discarded by the channel manager, causing `state_req_id` to evaluate to `None` inside `approval`. Thus, `if state_req_id and row_req_id and str(row_req_id) != str(state_req_id):` never evaluated to true, and the interrupt payload carried `"approval_request_id": None`.

2. **Resolution and Channel Persistence (Observation 1.1 -> Step 2)**:
   Adding `approval_request_id: str | None` to `FC27State` enables LangGraph's state graph channels to retain the value across node execution, checkpoint persistence, and `update_state` invocations.

3. **Interrupt Payload and Database Consistency (Observation 1.1 -> Step 3)**:
   Generating `req_id = state.get("approval_request_id") or str(uuid4())` in `prepare_approval` ensures a valid UUID is set in state and included in `request["approval_request_id"]` before `interrupt(state["approval_request"])` is invoked. `src/graph_api.py` forwards this ID to `store.complete_run()`, which inserts the identical ID into `public.approval_requests` and `public.graph_threads`. Both database rows and the LangGraph checkpoint are bound to the identical UUID.

4. **Active Validation on Resume (Observation 1.1 & 1.2 -> Step 4)**:
   When `runner.app.update_state` modifies `approval_request_id` to `"00000000-0000-0000-0000-000000000000"`, `state_req_id` is now retained as that string. Inside `approval(state)`, `state_req_id` and `row_req_id` are both non-null strings that do not match, actively triggering `ApprovalNotCommitted("committed decision refers to a different approval request")`. This was proven empirically by Attack 2.5 passing without `xfail`.

5. **Clean Legitimate Resumption and Cold Recovery (Observation 1.1 & 1.2 -> Step 5)**:
   On legitimate runs, `state_req_id == row_req_id`, bypassing the mismatch guard and advancing cleanly to terminal statuses. Across cold server restarts (closing the pool and creating a new runner), the checkpoint in PostgreSQL preserves `approval_request_id`, matching the original interrupted run.

---

## 3. Caveats

- **Scope Boundary**: Verification was strictly focused on Milestone 2 Graph Core & Persistence (`src/graph.py`, `src/approval_store.py`, `src/graph_api.py`, `tests/test_graph_persistence.py`). Milestone 3 dispatch transitions and future phases remain out of scope for M2.
- No caveats regarding test execution: 100% of the target test suite (20/20 tests) executed and passed against the PostgreSQL test container.

---

## 4. Conclusion

The defect flagged in Milestone 2 Gate Review is **fully resolved**. The LangGraph core, API, and store now correctly generate, persist, expose, and validate `approval_request_id`. Resumption against mismatched IDs fails closed with `ApprovalNotCommitted`, legitimate runs resume cleanly, interrupt payloads contain valid non-null IDs, and cold restarts preserve checkpoint integrity.

**Verdict**: **APPROVE**

---

## 5. Verification Method

### 5.1 Independent Test Command
Run the complete graph persistence test suite:
```powershell
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_graph_persistence.py
```
Expected output: `20 passed in ~32s`, exit code `0`.

Run specifically Attack 2.5 without xfail:
```powershell
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -v tests/test_graph_persistence.py -k test_adversarial_resume_refusal_mismatched_approval_request_id
```
Expected output: `1 passed, 19 deselected`, exit code `0`.

### 5.2 Files to Inspect
- `src/graph.py`: Lines 66, 145–160, 180–183.
- `src/approval_store.py`: Lines 115–120, 144–178, 196–203.
- `src/graph_api.py`: Lines 411–418.
- `tests/test_graph_persistence.py`: Lines 80–81, 228, 532.

### 5.3 Invalidation Conditions
- If `snap.interrupts[0].value.get("approval_request_id")` is `None` -> Invalidation: `prepare_approval` failed to populate interrupt payload.
- If `ApprovalNotCommitted` is not raised on mismatched state request ID -> Invalidation: guard in `approval` bypassed.
- If `complete_run` generates a different UUID than `req_id` -> Invalidation: database row does not match graph checkpoint.
