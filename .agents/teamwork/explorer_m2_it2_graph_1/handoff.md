# Handoff Report: Milestone 2 Iteration 2 Graph Fix Investigation

- **Agent**: `explorer_m2_it2_graph_1`
- **Role**: M2 Iteration 2 Graph Explorer
- **Handoff Type**: Hard (Investigation complete)
- **Target Component**: `src/graph.py`
- **Related Report**: `graph_fix_analysis.md`

---

## 1. Observation

1. **`src/graph.py` Lines 52–74**:
   `FC27State` defines the schema for the LangGraph workflow channels:
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
   `approval_request_id` is completely absent from `FC27State`.
2. **`src/graph.py` Lines 143–154 (`prepare_approval`)**:
   `request["approval_request_id"]` is constructed as `state.get("approval_request_id")`. Because `state` does not contain `approval_request_id`, this evaluates to `None`. Furthermore, the return dictionary of `prepare_approval` does not include `"approval_request_id"` at top level.
3. **`src/graph.py` Lines 176–179 (`approval`)**:
   ```python
   state_req_id = state.get("approval_request_id")
   row_req_id = row.get("approval_request_id")
   if state_req_id and row_req_id and str(row_req_id) != str(state_req_id):
       raise ApprovalNotCommitted("committed decision refers to a different approval request")
   ```
   Because `state_req_id` is always `None`, `if state_req_id and row_req_id:` evaluates to falsy, making the validation dead code.
4. **`tests/test_graph_persistence.py` Lines 529–558**:
   Test `test_adversarial_resume_refusal_mismatched_approval_request_id` failed closed assertion:
   `E Failed: DID NOT RAISE <class 'src.graph.ApprovalNotCommitted'>`
   and is currently decorated with `@pytest.mark.xfail(strict=True)`.
5. **Execution Results**:
   Running `$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_graph_persistence.py` yielded:
   `19 passed, 1 xfailed in 29.06s` (exit code 0).

---

## 2. Logic Chain

1. **From Observation 1**: LangGraph's `StateGraph` allocates internal channels and checkpoint serialization based on keys declared in `FC27State`. Keys omitted from `FC27State` are silently discarded by the channel router upon node execution and checkpoint reads/writes.
2. **From Observation 2 & Logic Step 1**: In `prepare_approval`, reading `state.get("approval_request_id")` returns `None` because no prior node or input populated it. Even if a value were generated without being declared in `FC27State`, returning it without a declared channel would cause it to be dropped upon interruption. Consequently, the interrupt payload emitted via `interrupt(state["approval_request"])` contains `approval_request_id: None`, violating Spec §4.3.
3. **From Observation 3 & Logic Steps 1–2**: When the graph is resumed, `state.get("approval_request_id")` retrieves `None` from the checkpoint. Because `state_req_id` is falsy, the mismatch check at lines 176–179 never executes, allowing a resumed graph to proceed against a database row associated with a disjoint or mismatched approval request ID, violating Spec §4.4.
4. **From Observation 4 & Logic Step 3**: In `test_adversarial_resume_refusal_mismatched_approval_request_id`, calling `runner.app.update_state` with a mismatched `approval_request_id` is dropped, resulting in no exception being raised during resume.
5. **From Observations 1–4 to Conclusion**:
   - Adding `from uuid import uuid4` and `approval_request_id: str | None` to `FC27State` enables channel tracking and checkpoint persistence.
   - In `prepare_approval`, generating `req_id = state.get("approval_request_id") or str(uuid4())`, returning `"approval_request_id": req_id` in the update dict, and setting `request["approval_request_id"] = req_id` ensures valid interrupt payloads and persistent checkpoint state.
   - When paired with `ApprovalStore.complete_run` honoring `req_id`, normal runs see `state_req_id == row_req_id`, while mismatched runs actively trigger `ApprovalNotCommitted("committed decision refers to a different approval request")`.

---

## 3. Caveats

1. **Coordinated Store Update**: Modifying `src/graph.py` alone is necessary but must be paired with `ApprovalStore.complete_run` in `src/approval_store.py` (explored by `explorer_m2_it2_api_1`) reusing `state.get("approval_request_id")` during the `INSERT INTO public.approval_requests`. If `ApprovalStore.complete_run` continues generating a disjoint UUID, normal runs would fail the mismatch check.
2. **Production Code Freeze**: Per dispatch instructions, no edits have been applied to `src/graph.py` during this investigation. Implementation is reserved for `worker_m2_1`.

---

## 4. Conclusion

The Challenger 1 finding is fully verified and reproducible. The exact fix strategy in `src/graph.py` requires three discrete changes:
1. Import `uuid4` from `uuid`.
2. Add `approval_request_id: str | None` to `FC27State`.
3. In `prepare_approval`, set `req_id = state.get("approval_request_id") or str(uuid4())`, populate `request["approval_request_id"] = req_id`, and return `"approval_request_id": req_id` in the state update dict.

The validation in `approval` (lines 176–179) is already correctly phrased and will actively execute once the state channel is persisted.

---

## 5. Verification Method

1. **Inspect Modified Files**:
   Confirm changes in `src/graph.py` match the diff provided in `graph_fix_analysis.md`.
2. **Execute Test Suite**:
   Remove the `@pytest.mark.xfail` decorator on `test_adversarial_resume_refusal_mismatched_approval_request_id` in `tests/test_graph_persistence.py`.
   Run:
   ```powershell
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_graph_persistence.py
   ```
3. **Invalidation Conditions**:
   - If any test raises `ApprovalNotCommitted` on legitimate approval/rejection runs (indicates `complete_run` and `prepare_approval` generated mismatched UUIDs).
   - If `test_adversarial_resume_refusal_mismatched_approval_request_id` fails to raise `ApprovalNotCommitted`.
