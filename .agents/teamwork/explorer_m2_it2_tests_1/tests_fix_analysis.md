# Test Suite Fix Analysis: Milestone 2 Iteration 2 Gate Resolution

- **Author**: `explorer_m2_it2_tests_1` (teamwork_preview_explorer)
- **Role**: M2 Iteration 2 Test Suite Explorer
- **Target Files**: `tests/test_graph_persistence.py`, `tests/test_graph_api_v1.py`
- **Referenced Sources**: `ORIGINAL_REQUEST.md`, `PROJECT.md`, `challenger_m2_1/challenge_report.md`
- **Date**: 2026-10-03

---

## 1. Executive Summary

During Milestone 2 Iteration 1 adversarial review, `challenger_m2_1` uncovered an architectural defect in LangGraph state propagation:
`approval_request_id` was omitted from `FC27State` (`TypedDict`) and not populated in `prepare_approval`. This resulted in:
1. The approval interrupt payload having `"approval_request_id": None` (violating Spec §4.3).
2. The mismatch validation check in `approval` (`if state_req_id and row_req_id and str(row_req_id) != str(state_req_id):`) being permanently bypassed as dead code because `state.get("approval_request_id")` was always `None` (violating Spec §4.4).
3. Adversarial Attack 2.5 (`test_adversarial_resume_refusal_mismatched_approval_request_id`) failing because mutating `approval_request_id` in the checkpoint via `runner.app.update_state` was dropped by LangGraph, allowing resume to succeed without raising `ApprovalNotCommitted`.

To document the defect without blocking the test runner, `challenger_m2_1` marked `test_adversarial_resume_refusal_mismatched_approval_request_id` with `@pytest.mark.xfail(strict=True)`.

This investigation determines:
1. Removing `@pytest.mark.xfail` will strictly enforce that `ApprovalNotCommitted` is raised when `approval_request_id` is mismatched.
2. Under the proposed worker fixes for `src/graph.py` and `src/approval_store.py`, no regressions will occur across all 20 tests in `tests/test_graph_persistence.py` and all 31 tests in `tests/test_graph_api_v1.py`.
3. Adding explicit assertions to `test_single_approval_interrupt_at_approval_node` and `test_checkpoint_recovery_across_server_restart` will lock in Spec §4.3 and §7.1 compliance.

---

## 2. Root Cause Analysis of the Test Failure

### 2.1 The Test Mechanism (Attack 2.5)
In `tests/test_graph_persistence.py`:
```python
@pytest.mark.xfail(
    strict=True,
    reason="Empirical Finding: FC27State omits approval_request_id, so LangGraph channels drop it and state.get('approval_request_id') remains None, causing lines 176-179 in src/graph.py to be dead code.",
)
def test_adversarial_resume_refusal_mismatched_approval_request_id(wired, pg_uri):
    """Attack 2.5: Committed decision pointing to a different approval_request_id fails closed."""
    runner, app = wired()
    with TestClient(app) as c:
        tid = start_run(c)["thread_id"]
    cfg = run_config(tid, "330323")

    # Commit decision directly to PostgreSQL without resuming the graph
    store = ApprovalStore(pg_uri)
    p = Principal(Role.commissioner, "batmanL8")
    ch = store.issue_challenge(tid, DecisionIntent(decision="REJECT"), p, 60)
    signed_payload = SignedDecision.model_validate({**ch, "signature": approval.sign(ch, SIGNING_SECRET)})
    store.apply_decision(tid, signed_payload, p, SIGNING_SECRET)

    # Graph is still waiting at approval
    assert runner.values(tid)["graph_next"] == ["approval"]

    # Bind a conflicting approval_request_id in state
    runner.app.update_state(
        {"configurable": {"thread_id": tid}},
        {"approval_request_id": "00000000-0000-0000-0000-000000000000"},
    )

    with pytest.raises(ApprovalNotCommitted, match="different approval request"):
        runner.app.invoke(Command(resume={"thread_id": tid}), cfg, durability="sync")
```

### 2.2 Why the Test Failed
1. **LangGraph Channel Filtering**: `StateGraph(FC27State)` creates channels only for fields explicitly annotated in `FC27State`. Keys passed to `update_state` that are not part of `FC27State` are silently discarded.
2. **Missing Key in State Definition**: `FC27State` (`src/graph.py` lines 52–74) defined `thread_id`, `club_id`, `package_version`, `approval_request`, etc., but **omitted** `approval_request_id`.
3. **Dead Code in Guard**: In `approval(state)` (`src/graph.py` lines 176–179):
   ```python
   state_req_id = state.get("approval_request_id")
   row_req_id = row.get("approval_request_id")
   if state_req_id and row_req_id and str(row_req_id) != str(state_req_id):
       raise ApprovalNotCommitted("committed decision refers to a different approval request")
   ```
   Because `state_req_id` was `None`, the conditional `if state_req_id and row_req_id:` evaluated to `False`. The guard was never reached, and the resume proceeded.
4. **XFail Tag**: `challenger_m2_1` applied `@pytest.mark.xfail(strict=True)` to prevent pipeline breakage while reporting the gate failure.

---

## 3. Impact of Removing `@pytest.mark.xfail`

When the worker applies the production fixes:
1. `approval_request_id: str | None` is declared in `FC27State`.
2. `prepare_approval` generates `req_id = state.get("approval_request_id") or str(uuid4())` and returns `"approval_request_id": req_id`.
3. `complete_run` in `src/approval_store.py` inserts `public.approval_requests` using `req_id`.
4. In the test, when `runner.app.update_state` sets `approval_request_id: "00000000-0000-0000-0000-000000000000"`:
   - LangGraph retains this value in the checkpoint.
   - Upon `runner.app.invoke(Command(resume=...))`, `state_req_id` is `"00000000-0000-0000-0000-000000000000"`.
   - `row_req_id` is the real UUID committed by `store.apply_decision`.
   - `str(row_req_id) != str(state_req_id)` evaluates to `True`.
   - `ApprovalNotCommitted("committed decision refers to a different approval request")` is raised.
   - The test expectation `with pytest.raises(ApprovalNotCommitted, match="different approval request")` is satisfied.

**Strict xfail behavior**: Under `pytest`, a test decorated with `@pytest.mark.xfail(strict=True)` that passes will produce an `XPASS` error (exit code 1). Therefore, removing `@pytest.mark.xfail` is required for the test suite to pass.

---

## 4. Regression Analysis Across Test Suites

### 4.1 `tests/test_graph_persistence.py` (20 Tests)
| Test Name | Vector Tested | Effect of State & Store Fix | Regression Risk |
|---|---|---|---|
| `test_single_approval_interrupt_at_approval_node` | Single interrupt, node flow | Values and interrupt payload will now contain non-null `approval_request_id` | **None** (passes; can be strengthened) |
| `test_resume_without_committed_postgres_decision_raises_error` | Uncommitted resume failure | Trapped by `row.get("status") not in DECIDED_STATUSES` | **None** (passes) |
| `test_resume_payload_tampering_ignored` | Untrusted resume payloads | Normal approve flow has matching `approval_request_id` | **None** (passes) |
| `test_decision_package_version_mismatch_fails_closed` | Package version tampering | Version check executes prior to request ID check | **None** (passes) |
| `test_terminal_graph_states_formatting` | Approved and rejected terminal strings | Matching request IDs allow clean completion | **None** (passes) |
| `test_checkpoint_recovery_across_server_restart` | Checkpoint persistence | Checkpoint preserves `approval_request_id` | **None** (passes) |
| `test_repeated_resume_on_terminal_state_is_noop` | Idempotent repeat resume | `_approval_pending` terminates early | **None** (passes) |
| `test_startup_reconciliation_resumes_committed_threads` | Lifespan background reconciliation | Reconciled DB row matches checkpoint ID | **None** (passes) |
| `test_adversarial_resume_payload_injection_variants` | Malicious resume inputs | Trapped before request ID validation | **None** (passes) |
| `test_adversarial_state_tampering_prior_to_resume` | State override attempts | Trapped by uncommitted DB status | **None** (passes) |
| `test_adversarial_cross_thread_checkpoint_hijack` | Cross-thread hijack | Trapped by package version / thread mismatch | **None** (passes) |
| `test_adversarial_api_resume_route_payload_tampering` | Route schema tampering | Pydantic 422 validation | **None** (passes) |
| `test_adversarial_legitimate_resume_discards_malicious_body` | Legitimate resume with bad body | Matching request IDs; state reloaded from DB | **None** (passes) |
| `test_adversarial_uncommitted_resume_on_non_terminal_statuses` | Non-terminal statuses fail closed | Trapped by status check | **None** (passes) |
| `test_adversarial_resume_refusal_unsupported_actor_type` | Section 1A actor type | `graph_threads.approval_request_id` unchanged; matches checkpoint; fails on actor type | **None** (passes) |
| `test_adversarial_resume_refusal_unsupported_authority_basis` | Section 1A authority basis | `graph_threads.approval_request_id` unchanged; matches checkpoint; fails on authority basis | **None** (passes) |
| `test_adversarial_resume_refusal_mismatched_approval_request_id` | Mismatched approval request ID | Mismatched ID raises `ApprovalNotCommitted`; xfail removed | **None** (passes cleanly) |
| `test_adversarial_api_resume_endpoint_refusal_when_uncommitted` | HTTP 409 when uncommitted | Trapped by store row status | **None** (passes) |
| `test_adversarial_crash_recovery_with_rejection_decision` | Crash recovery with rejection | Matching request IDs; resumes to rejection | **None** (passes) |
| `test_adversarial_terminal_formatting_and_forbidden_strings` | Formatting and forbidden strings audit | All terminal outcomes format cleanly | **None** (passes) |

### 4.2 `tests/test_graph_api_v1.py` (31 Tests)
- `test_v1_post_runs_creates_durable_run`: Asserts `data["approval_request_id"] is not None`.
  - When `StubPipeline` is used, `store.complete_run` must generate a fallback UUID if `state.get("approval_request_id")` is absent.
  - When `GraphRunner` is used, `approval_request_id` is supplied directly from `state`.
  - In both paths, `data["approval_request_id"]` is populated as a valid UUID.
- `test_v1_approval_request_decisions_route`: Reads `req_id = resp.json()["approval_request_id"]` and calls `POST /v1/approval-requests/{req_id}/decisions`.
  - Route loads thread by `SELECT thread_id FROM public.approval_requests WHERE approval_request_id = %s`.
  - Because `complete_run` binds the database row to that same ID, the query succeeds and the decision is applied.
- All correlation middleware, health probe, HMAC kill switch, and secret redaction tests are completely decoupled from `approval_request_id` and will continue passing without alteration.

### 4.3 Database Trigger Invariant Verification
In `migrations/0003_dispatch_foundation.sql`:
1. `fc27_approval_requests_pair_check` and `fc27_graph_threads_pair_check` ensure that `approval_requests.approval_request_id == graph_threads.approval_request_id`.
2. When `complete_run` inserts `approval_requests` with `approval_request_id = %s` and updates `graph_threads` with `approval_request_id = %s`, both tables are atomically updated within the same transaction.
3. The deferred constraint triggers evaluate upon transaction commit and find exact 1:1 parity.
4. The diagnostic view `public.fc27_approval_inconsistencies` will contain 0 rows, satisfying the `conftest.py` post-test assertion.

---

## 5. Recommended Test Modifications for Worker

The worker should make the following exact changes to `tests/test_graph_persistence.py`:

### Modification 1: Remove `@pytest.mark.xfail` from `test_adversarial_resume_refusal_mismatched_approval_request_id`
**File**: `tests/test_graph_persistence.py`
**Lines**: 529–532
```diff
-@pytest.mark.xfail(
-    strict=True,
-    reason="Empirical Finding: FC27State omits approval_request_id, so LangGraph channels drop it and state.get('approval_request_id') remains None, causing lines 176-179 in src/graph.py to be dead code.",
-)
 def test_adversarial_resume_refusal_mismatched_approval_request_id(wired, pg_uri):
     """Attack 2.5: Committed decision pointing to a different approval_request_id fails closed."""
```

### Modification 2: Strengthen `test_single_approval_interrupt_at_approval_node`
**File**: `tests/test_graph_persistence.py`
**Lines**: 74–80
```diff
     # Verify compiled graph has NO interrupt_before on approval node
     snap = runner.app.get_state({"configurable": {"thread_id": tid}})
     assert len(snap.interrupts) == 1
     assert len(snap.tasks) == 1
     assert snap.tasks[0].name == "approval"
     assert snap.interrupts[0].value["thread_id"] == tid
     assert snap.interrupts[0].value["package_version"] == values["package_version"]
+    assert values.get("approval_request_id") is not None
+    assert snap.interrupts[0].value.get("approval_request_id") == values["approval_request_id"]
```
*Rationale*: Formally verifies Spec §4.3: "Include the approved subset of: thread_id; approval_request_id; package_id; package_version; package_hash...". Prevents regression where `approval_request_id` is dropped from the interrupt payload.

### Modification 3: Strengthen `test_checkpoint_recovery_across_server_restart`
**File**: `tests/test_graph_persistence.py`
**Lines**: 221–227
```diff
     # Instance 2: reconstruct fresh runner on the exact same PostgreSQL database
     runner2, app2 = make_runner()
     values2 = runner2.values(tid)
     assert values2["status"] == "awaiting_approval"
     assert values2["graph_interrupted"] is True
     assert values2["graph_next"] == ["approval"]
     assert values2["package_version"] == version1
+    assert values2.get("approval_request_id") == state1.get("approval_request_id")
     assert "lead_packages" in values2
```
*Rationale*: Formally verifies Spec §7.1 and §11.2: "Checkpoint preserves state and enables recovery across runner restarts." Guarantees that `approval_request_id` survives checkpoint recovery across server crashes.

---

## 6. Implementation Contract for Worker (`src/graph.py` & `src/approval_store.py`)

For the above tests to pass cleanly, the worker must ensure:

1. **`src/graph.py`**:
   - Add `approval_request_id: str | None` to `FC27State(TypedDict, total=False)`.
   - In `prepare_approval(state: FC27State)`:
     ```python
     from uuid import uuid4
     ...
     req_id = state.get("approval_request_id") or str(uuid4())
     ```
     Include `"approval_request_id": req_id` in `request = {...}` and in the return dictionary of `prepare_approval`.
   - In `approval(state: FC27State)`:
     Keep the existing active validation:
     ```python
     state_req_id = state.get("approval_request_id")
     row_req_id = row.get("approval_request_id")
     if state_req_id and row_req_id and str(row_req_id) != str(state_req_id):
         raise ApprovalNotCommitted("committed decision refers to a different approval request")
     ```

2. **`src/approval_store.py`**:
   - In `complete_run(self, thread_id: str, state: dict[str, Any])`:
     Accept and prioritize `state.get("approval_request_id")`:
     ```python
     req_id = state.get("approval_request_id")
     if req_id:
         req_row = conn.execute(
             """INSERT INTO public.approval_requests (
                    approval_request_id, thread_id, package_version, status, created_at, expires_at, version
                ) VALUES (
                    %s, %s, %s, 'awaiting_approval', now(), now() + %s, 1
                )
                RETURNING approval_request_id,
                          to_char(expires_at AT TIME ZONE 'utc', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS approval_expires_at""",
             (req_id, thread_id, version, self.request_ttl),
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
     ```
     This binds the database record to the exact ID persisted in the LangGraph checkpoint, while maintaining safe fallback for `StubPipeline` callers.
