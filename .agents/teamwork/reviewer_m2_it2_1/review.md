# Quality & Adversarial Review Report: Milestone 2 Iteration 2

- **Reviewer**: `reviewer_m2_it2_1` (teamwork_preview_reviewer)
- **Roles**: Reviewer, Adversarial Critic
- **Review Target**: Worker M2 Iteration 2 changes in `src/graph.py`, `src/approval_store.py`, `src/graph_api.py`, and `tests/test_graph_persistence.py`
- **Execution Date**: 2026-10-03
- **Verdict**: **APPROVE**

---

## Review Summary

**Verdict**: **APPROVE**

The architectural defect flagged by Challenger 1 (`challenger_m2_1`) has been completely and cleanly remediated. 
1. `FC27State` now includes `approval_request_id: str | None`.
2. `prepare_approval` generates `req_id = state.get("approval_request_id") or str(uuid4())`, populates `approval_request["approval_request_id"]`, and returns `"approval_request_id": req_id` in state update.
3. The approval node actively compares `state_req_id` with `row_req_id` and raises `ApprovalNotCommitted("committed decision refers to a different approval request")` on mismatch.
4. `ApprovalStore.complete_run` honors `approval_request_id` from the state/argument and writes the exact ID into `approval_requests` and `graph_threads`.
5. `src/graph_api.py` extracts `approval_req_id` from the interrupted state and forwards it into `store.complete_run`.
6. `@pytest.mark.xfail` was removed from `test_adversarial_resume_refusal_mismatched_approval_request_id` in `tests/test_graph_persistence.py`.
7. Independent execution of `tests/test_graph_persistence.py tests/test_langgraph_flow.py` against PostgreSQL passed 58/58 tests with 0 failures and 0 xfails.
8. Independent execution of `tests/test_graph_api_v1.py` against PostgreSQL passed 55/55 tests with 0 failures.

---

## Findings

### [Minor / Defense-in-Depth] Finding 1: Asymmetric Guard on Absent `state_req_id`
- **What**: In `src/graph.py` lines 180–183:
  ```python
  state_req_id = state.get("approval_request_id")
  row_req_id = row.get("approval_request_id")
  if state_req_id and row_req_id and str(row_req_id) != str(state_req_id):
      raise ApprovalNotCommitted("committed decision refers to a different approval request")
  ```
  The condition requires both `state_req_id` and `row_req_id` to be truthy before triggering the mismatch exception.
- **Where**: `src/graph.py:182`
- **Why**: If an attacker were able to strip `approval_request_id` entirely from checkpoint state (`state_req_id` becomes `None`), but `row_req_id` exists in PostgreSQL, the check would evaluate to `False` rather than detecting the asymmetry.
- **Suggestion**: For future iterations or defense-in-depth, consider:
  ```python
  if (state_req_id or row_req_id) and str(row_req_id) != str(state_req_id):
  ```
  This is non-blocking for Milestone 2 because `prepare_approval` always injects `approval_request_id`, checkpoints are server-side in PostgreSQL, and standard tampering (injecting mismatched IDs) is fully caught.

---

## Verified Claims

1. **Claim**: `approval_request_id: str | None` is declared in `FC27State`.
   - **Method**: Verified in `src/graph.py:66`.
   - **Result**: **PASS**.

2. **Claim**: `prepare_approval` generates `req_id`, populates `request["approval_request_id"]`, and returns `"approval_request_id": req_id` in state.
   - **Method**: Verified in `src/graph.py:145, 151, 160`.
   - **Result**: **PASS**.

3. **Claim**: `approval` node actively compares `state_req_id` with `row_req_id` and raises `ApprovalNotCommitted` on mismatch.
   - **Method**: Verified in `src/graph.py:180-184` and tested via `test_adversarial_resume_refusal_mismatched_approval_request_id`.
   - **Result**: **PASS**.

4. **Claim**: `@pytest.mark.xfail` is removed from `tests/test_graph_persistence.py`.
   - **Method**: Verified lines 532-558 of `tests/test_graph_persistence.py`; no xfail decorator present.
   - **Result**: **PASS**.

5. **Claim**: All graph persistence and flow tests pass against live PostgreSQL database.
   - **Method**: Executed `$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_graph_persistence.py tests/test_langgraph_flow.py`.
   - **Result**: **PASS** (58 passed in 77.88s, exit code 0).

6. **Claim**: Graph API v1 tests continue to pass without regression.
   - **Method**: Executed `$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_graph_api_v1.py`.
   - **Result**: **PASS** (55 passed in 72.38s, exit code 0).

---

## Adversarial Challenge & Stress-Test Results

| Vector / Scenario | Target & Attack Scenario | Expected Behavior | Actual Behavior | Result |
|---|---|---|---|---|
| **Mismatched approval_request_id** | Mutate state via `runner.app.update_state({"approval_request_id": "000...000"})` | LangGraph raises `ApprovalNotCommitted("committed decision refers to a different approval request")` | `ApprovalNotCommitted` raised; thread fails closed | **PASS** |
| **Interrupt Payload Integrity** | Check interrupt payload emitted by `prepare_approval` | Must expose non-None `approval_request_id` matching state | `snap.interrupts[0].value.get("approval_request_id") == values["approval_request_id"]` | **PASS** |
| **Cold Restart Recovery** | Simulate crash while interrupted, reconstruct fresh runner | State preserved including `approval_request_id`; resumes cleanly to terminal state on DB decision | `values2.get("approval_request_id") == state1.get("approval_request_id")`; advances to `"APPROVED, NOT DISPATCHED"` | **PASS** |
| **Integrity Inspection** | Scan for hardcoded test results, facade logic, or bypassed checks | Production code contains real business logic; no mocks or dummy bypasses | Fully authentic DB queries and state comparisons | **PASS** |

---

## Coverage Gaps & Unverified Items

- **Coverage Gaps**: None. All core and adversarial vectors for Milestone 2 are covered.
- **Unverified Items**: None. All claims independently verified via inspection and pytest execution against PostgreSQL.
