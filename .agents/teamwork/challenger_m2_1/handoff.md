# Milestone 2 Adversarial Challenge Handoff Report

- **Agent**: `challenger_m2_1` (teamwork_preview_challenger)
- **Target**: Milestone 2 (Graph Core & Persistent Graph API)
- **Verdict**: **REQUEST_CHANGES**

---

## 1. Observation

### Test Execution
- **Command**:
  ```powershell
  $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_graph_persistence.py
  ```
- **Output**:
  ```
  ................x...                                                     [100%]
  19 passed, 1 xfailed in 34.09s
  ```
  Exit code: 0.

### Source Code Observations
1. In `src/graph.py` lines 52–74:
   ```python
   class FC27State(TypedDict, total=False):
       thread_id: str
       club_id: str
       fc_version: str
       platform: str
       trigger: str
       run_seq: int
       match: dict | None
       dashboard: dict
       candidates: list[dict]
       role_reports: dict[str, dict]
       lead_packages: dict[str, dict]
       package_version: str | None
       approval_request: dict
       approval_decision: dict
       status: str
       errors: Annotated[list[str], operator.add]
       model_ids: dict[str, str]
       prompt_version: str
       policy_version: str
       code_revision: str
       audit_log: Annotated[list[dict], operator.add]
   ```
   `approval_request_id` is **not present** as a field in `FC27State`.

2. In `src/graph.py` lines 143–154 (`prepare_approval`):
   ```python
   request = {
       "thread_id": state["thread_id"],
       "package_version": version,
       "package_hash": version,
       "approvable_owners": approvable,
       "approval_request_id": state.get("approval_request_id"),
       "safe_summary": f"approvable owners: {approvable or 'none'}",
       "required_authority": "commissioner",
       "fc_version": FC_VERSION,
       "platform": state["platform"],
   }
   return {
       "lead_packages": packages,
       "package_version": version,
       "approval_request": request,
       "status": "awaiting_approval",
       "audit_log": _audit("prepare_approval", state, f"approvable owners: {approvable or 'none'}"),
   }
   ```
   `state.get("approval_request_id")` evaluates to `None` at this point because `approval_request_id` has not been generated or added to state. Furthermore, `prepare_approval` does not return `"approval_request_id"` in the state update dictionary.

3. In `src/graph.py` lines 176–179 (`approval`):
   ```python
   state_req_id = state.get("approval_request_id")
   row_req_id = row.get("approval_request_id")
   if state_req_id and row_req_id and str(row_req_id) != str(state_req_id):
       raise ApprovalNotCommitted("committed decision refers to a different approval request")
   ```
   Because `state_req_id` is `None`, the conditional `if state_req_id and row_req_id:` evaluates to falsy, making this validation check unreachable dead code.

4. In `tests/test_graph_persistence.py`:
   When `test_adversarial_resume_refusal_mismatched_approval_request_id` attempted to assert that `ApprovalNotCommitted` is raised upon a mismatched `approval_request_id`, pytest reported:
   ```
   E Failed: DID NOT RAISE <class 'src.graph.ApprovalNotCommitted'>
   ```
   Confirming empirically that the check was bypassed.

5. Across all tested terminal outcomes (`approved`, `rejected`, `revision_requested`):
   - Statuses strictly format as `"APPROVED, NOT DISPATCHED"`, `"REJECTED, NO ACTION TAKEN"`, and `"REVISION_REQUESTED, NO ACTION TAKEN"`.
   - String scans for `"APPROVED and ACTIVE"`, `"ACTIVE"`, and active dispatch verbs in terminal states and audit summaries yielded 0 occurrences.

---

## 2. Logic Chain

1. **Spec Requirement Analysis**:
   - `ORIGINAL_REQUEST.md` §4.3 mandates: "The interrupt payload must contain only identifiers and display-safe data... Include the approved subset of: thread_id; approval_request_id; package_id; package_version; package_hash; safe summary; required approving authority."
   - `ORIGINAL_REQUEST.md` §4.4 mandates: "After resume, the Graph must reread the authoritative PostgreSQL decision. Validate: decision exists; approval request matches; package ID matches; package version matches; package hash matches..."
2. **State & Channel Dropping**:
   - In LangGraph, state channels are defined strictly by the schema provided to `StateGraph` (here, `FC27State`). Keys that are not defined in `FC27State` are not persisted across node checkpoints.
   - Because `approval_request_id` is omitted from `FC27State`, any attempt to bind `approval_request_id` into LangGraph state is dropped by the checkpointer.
3. **Sequence Mismatch**:
   - In `src/graph_api.py`, `create_run_internal` invokes `run_pipeline(state)` before `store.complete_run`.
   - `run_pipeline(state)` executes through `prepare_approval` and interrupts at `approval`.
   - `store.complete_run` creates the PostgreSQL row in `approval_requests` and updates `graph_threads.approval_request_id` only *after* the graph has paused.
   - Because LangGraph's checkpointer is not updated with this new ID and `prepare_approval` did not generate it, LangGraph's state never holds `approval_request_id`.
4. **Dead Validation**:
   - In `approval(state)`, `state_req_id = state.get("approval_request_id")` is always `None`.
   - Therefore, `if state_req_id and row_req_id and str(row_req_id) != str(state_req_id):` is a dead guard. If a thread is resumed against a committed decision with a mismatched approval request ID, the graph core fails to detect it and does not raise `ApprovalNotCommitted`.
5. **Mitigation Feasibility**:
   - The fix requires:
     a) Adding `approval_request_id: str | None` to `FC27State` in `src/graph.py`.
     b) Generating `approval_request_id = state.get("approval_request_id") or str(uuid4())` inside `prepare_approval` and returning it in the update dict.
     c) Reusing this ID in `ApprovalStore.complete_run` when inserting the `approval_requests` row.

---

## 3. Caveats

- In the current database layer, foreign keys and the `public.fc27_check_approval_pair()` constraint trigger prevent a mismatch between `graph_threads.approval_request_id` and `approval_requests.approval_request_id`. Therefore, the blast radius is confined to the LangGraph core workflow layer and human-in-the-loop interrupt payload presentation (which currently exposes `approval_request_id: null`).
- All other 19 adversarial tests (tamper resistance, uncommitted status refusal, Section 1A actor type/authority basis enforcement, process restart recovery, and terminal string formatting) passed cleanly with real PostgreSQL state.

---

## 4. Conclusion

The Milestone 2 implementation is exceptionally strong in resume payload tamper resistance, PostgreSQL authority enforcement, checkpoint crash recovery, and exact terminal string formatting. However, due to the omission of `approval_request_id` from `FC27State` and its absence in the interrupt payload and resume validation, the implementation fails **Spec §4.3** and **Spec §4.4**.

**Verdict**: **REQUEST_CHANGES**

---

## 5. Verification Method

1. Run the test suite:
   ```powershell
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_graph_persistence.py
   ```
2. Verify:
   - 19 passed tests covering single interrupt, tamper resistance, uncommitted refusal, crash recovery, Section 1A constraints, and formatting.
   - 1 test (`test_adversarial_resume_refusal_mismatched_approval_request_id`) marked `xfail` demonstrating the dead validation code in `src/graph.py`.
3. Invalidation condition:
   - Once worker updates `FC27State` to include `approval_request_id` and generates it in `prepare_approval`, removing `@pytest.mark.xfail` from `test_adversarial_resume_refusal_mismatched_approval_request_id` will cause it to pass cleanly (`XPASS` if strict, or regular `pass`), enabling full approval.
