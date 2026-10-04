# Handoff Report: Milestone 2 Iteration 2 Test Suite Investigation

- **Agent**: `explorer_m2_it2_tests_1`
- **Role**: M2 Iteration 2 Test Suite Explorer
- **Type**: Hard Handoff (Investigation Complete)
- **Target Recipient**: `orchestrator_phase1` (and `worker_m2_2`)
- **Date**: 2026-10-03

---

## 1. Observation

1. **`tests/test_graph_persistence.py` Lines 529–534**:
   `test_adversarial_resume_refusal_mismatched_approval_request_id` is marked with `@pytest.mark.xfail`:
   ```python
   @pytest.mark.xfail(
       strict=True,
       reason="Empirical Finding: FC27State omits approval_request_id, so LangGraph channels drop it and state.get('approval_request_id') remains None, causing lines 176-179 in src/graph.py to be dead code.",
   )
   def test_adversarial_resume_refusal_mismatched_approval_request_id(wired, pg_uri):
       """Attack 2.5: Committed decision pointing to a different approval_request_id fails closed."""
   ```
2. **`challenger_m2_1/challenge_report.md` Lines 21–33 & 56–58**:
   The challenger observed:
   > "In `src/graph.py`, `approval_request_id` is omitted from `FC27State` (`TypedDict`), and is not generated prior to the approval interrupt in `prepare_approval`... Test `test_adversarial_resume_refusal_mismatched_approval_request_id` in `tests/test_graph_persistence.py` failed when asserting that `ApprovalNotCommitted` is raised upon a mismatched `approval_request_id`... `E Failed: DID NOT RAISE <class 'src.graph.ApprovalNotCommitted'>`"
3. **`src/graph.py` Lines 52–74**:
   `FC27State(TypedDict, total=False)` defines:
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
   `approval_request_id` is missing.
4. **`src/graph.py` Lines 143–153**:
   In `prepare_approval(state)`:
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
   ```
   `state.get("approval_request_id")` evaluates to `None`. The return dict does not emit `"approval_request_id"`.
5. **`src/graph.py` Lines 176–179**:
   In `approval(state)`:
   ```python
       state_req_id = state.get("approval_request_id")
       row_req_id = row.get("approval_request_id")
       if state_req_id and row_req_id and str(row_req_id) != str(state_req_id):
           raise ApprovalNotCommitted("committed decision refers to a different approval request")
   ```
   When `state_req_id` is `None`, the conditional is bypassed.
6. **`tests/test_graph_persistence.py` Lines 74–80**:
   `test_single_approval_interrupt_at_approval_node` asserts `snap.interrupts[0].value["thread_id"]` and `package_version`, but does not assert `approval_request_id`.
7. **`tests/test_graph_api_v1.py` Lines 117–137 & 245–257**:
   `test_v1_post_runs_creates_durable_run` asserts `data["approval_request_id"] is not None`. `test_v1_approval_request_decisions_route` loads the request row via `SELECT thread_id FROM public.approval_requests WHERE approval_request_id = %s`.
8. **`src/approval_store.py` Lines 140–168**:
   `store.complete_run` currently inserts `approval_requests` using the database default `gen_random_uuid()` rather than reading from `state["approval_request_id"]`.

---

## 2. Logic Chain

1. **Step 1 (Root Cause Linking)**: Observation 1 and Observation 2 document that `test_adversarial_resume_refusal_mismatched_approval_request_id` failed because `ApprovalNotCommitted` was not raised. Observations 3, 4, and 5 explain why: because `approval_request_id` is absent from `FC27State`, LangGraph's channel system drops the key when `runner.app.update_state` attempts to set it. As a result, `state.get("approval_request_id")` remains `None` inside `approval(state)`, making the guard at lines 176–179 dead code.
2. **Step 2 (Verification Mechanism)**: Once `approval_request_id` is added to `FC27State` and generated in `prepare_approval`, LangGraph will persist it in the checkpoint. In `test_adversarial_resume_refusal_mismatched_approval_request_id`, calling `runner.app.update_state(..., {"approval_request_id": "00000000-0000-0000-0000-000000000000"})` will properly mutate the checkpoint state channel. When `runner.app.invoke(Command(resume=...))` resumes `approval`, `state_req_id` will be `"00000000-0000-0000-0000-000000000000"` while `row_req_id` is the real UUID from PostgreSQL. The check `str(row_req_id) != str(state_req_id)` will evaluate to `True`, successfully raising `ApprovalNotCommitted("committed decision refers to a different approval request")`.
3. **Step 3 (xfail Removal Mandate)**: Under `pytest`, when a test decorated with `@pytest.mark.xfail(strict=True)` passes, it results in an `XPASS` error. Therefore, removing `@pytest.mark.xfail` is required for the suite to pass cleanly, and removing it converts the test into an active regression prevention gate.
4. **Step 4 (Absence of Regressions Across Test Suites)**:
   - In `test_graph_persistence.py`: All 19 other tests either test uncommitted failure before reaching the request ID check, test prior mismatch checks (e.g. package version), or test valid runs where `approval_request_id` in the checkpoint matches PostgreSQL. None of them will regress.
   - In `test_graph_api_v1.py`: Tests using `StubPipeline` or real runners require `complete_run` to populate `approval_request_id`. As long as `complete_run` falls back to `gen_random_uuid()` when `state.get("approval_request_id")` is absent (for `StubPipeline`), both routes and state projections continue to pass without regression.
   - In database triggers: `fc27_approval_requests_pair_check` and `fc27_graph_threads_pair_check` evaluate 1:1 match of `approval_request_id` across `graph_threads` and `approval_requests`. When `complete_run` populates both with the same ID, the triggers succeed and `fc27_approval_inconsistencies` remains at 0 rows.
5. **Step 5 (Test Suite Hardening)**: Adding explicit assertions in `test_single_approval_interrupt_at_approval_node` (Observation 6) and `test_checkpoint_recovery_across_server_restart` guarantees continuous automated verification of Spec §4.3 and §7.1.

---

## 3. Caveats

1. **Production Code Write Boundary**: As an explorer agent, no modifications were made directly to `src/` or `migrations/`. The analysis specifies the exact contractual changes needed by the worker (`worker_m2_2`).
2. **Database Engine Assumptions**: The analysis assumes PostgreSQL 16 with UUID casting behavior where `%s` accepts a canonical 36-character UUID string (standard `psycopg` and PostgreSQL behavior).
3. **Stub Pipeline Compatibility**: Tests in `tests/test_graph_api_v1.py` rely on `StubPipeline`, which does not run `prepare_approval`. Therefore, `ApprovalStore.complete_run` must handle both present and absent `approval_request_id`.

---

## 4. Conclusion

1. The `@pytest.mark.xfail` decorator on `test_adversarial_resume_refusal_mismatched_approval_request_id` in `tests/test_graph_persistence.py` should be removed by the worker.
2. Once `FC27State` defines `approval_request_id: str | None`, `prepare_approval` populates it, and `ApprovalStore.complete_run` persists it, removing `xfail` will allow `test_adversarial_resume_refusal_mismatched_approval_request_id` to pass cleanly as a green test.
3. No regressions will occur in `tests/test_graph_persistence.py` or `tests/test_graph_api_v1.py`.
4. `test_single_approval_interrupt_at_approval_node` and `test_checkpoint_recovery_across_server_restart` should be updated with explicit assertions on `approval_request_id` to permanently protect against Spec §4.3 and §7.1 regressions.

---

## 5. Verification Method

To independently verify the test suite after worker implementation:

1. **File Inspection**:
   - Inspect `tests/test_graph_persistence.py` line 529: ensure `@pytest.mark.xfail` has been removed.
   - Inspect lines 78–81: ensure assertions for `approval_request_id` on `snap.interrupts[0].value` and `values` are present.
2. **Execute Targeted Test Suites**:
   Run with active PostgreSQL 16 container:
   ```powershell
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"
   .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_graph_persistence.py
   .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_graph_api_v1.py
   ```
   **Expected Result**:
   - `tests/test_graph_persistence.py`: 20 passed, 0 xfailed, 0 failed.
   - `tests/test_graph_api_v1.py`: 31 passed, 0 failed.
3. **Invalidation Condition**:
   If `test_adversarial_resume_refusal_mismatched_approval_request_id` fails with `DID NOT RAISE <class 'src.graph.ApprovalNotCommitted'>`, `FC27State` in `src/graph.py` does not contain `approval_request_id`. If `fc27_approval_inconsistencies` detects mismatched rows, `complete_run` in `src/approval_store.py` did not synchronize `approval_requests.approval_request_id` with `graph_threads.approval_request_id`.
