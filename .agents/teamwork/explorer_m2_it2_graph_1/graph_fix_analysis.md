# Milestone 2 Iteration 2: Graph Core Fix Strategy Analysis

**Document**: `graph_fix_analysis.md`  
**Author**: `explorer_m2_it2_graph_1` (teamwork_preview_explorer)  
**Date**: 2026-10-03  
**Target File**: `src/graph.py`  
**Reference Findings**: Challenger 1 Report (`.agents/teamwork/challenger_m2_1/challenge_report.md`)

---

## 1. Executive Summary

During Milestone 2 Gate Evaluation, Challenger 1 identified an architectural defect in `src/graph.py` concerning the lifecycle and validation of `approval_request_id`:
1. `FC27State` (`TypedDict`) omits `approval_request_id`, causing LangGraph's channel system to discard the field upon checkpoint serialization.
2. `prepare_approval` attempts `state.get("approval_request_id")` before any identifier is generated, emitting `"approval_request_id": None` in the interrupt payload (violating **Spec §4.3**).
3. In `approval(state)`, `state_req_id = state.get("approval_request_id")` evaluates to `None`. The mismatch guard at lines 176–179 (`if state_req_id and row_req_id and str(row_req_id) != str(state_req_id):`) is dead code that never executes, permanently bypassing the requirement to validate that the resumed thread binds to the exact intended approval request (violating **Spec §4.4**).
4. Adversarial test `test_adversarial_resume_refusal_mismatched_approval_request_id` in `tests/test_graph_persistence.py` failed closed assertion and required `@pytest.mark.xfail`.

This analysis provides the exact, production-ready fix strategy for `src/graph.py`, ensuring seamless integration with `src/approval_store.py` and resolving the Gate failure.

---

## 2. Root Cause & Architectural Breakdown

### 2.1 LangGraph State Channel Dropping (`FC27State`)
In LangGraph `StateGraph(FC27State)`:
- Keys defined in `FC27State` define the state schema and the communication channels between nodes.
- When `FC27State` omits `approval_request_id`:
  - Any node returning `{"approval_request_id": ...}` has that key dropped by LangGraph's channel router.
  - Checkpoint writes (`PostgresSaver`) do not preserve `approval_request_id` in the channel values.
  - Direct state mutations via `runner.app.update_state({"configurable": ...}, {"approval_request_id": ...})` fail to set the channel value.
  - When resumed, `state.get("approval_request_id")` evaluates to `None`.

### 2.2 Premature Read in `prepare_approval`
In `src/graph.py` lines 143–154:
```python
request = {
    "thread_id": state["thread_id"],
    "package_version": version,
    "package_hash": version,
    "approvable_owners": approvable,
    "approval_request_id": state.get("approval_request_id"),  # Evaluates to None!
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
Two defects exist here:
1. `approval_request_id` is read from `state`, which has not yet received an approval request ID.
2. The return dictionary does not return `"approval_request_id": req_id` at the top level, so even if the channel existed, it would not be set.

### 2.3 Dead-Code Validation in `approval` Node
In `src/graph.py` lines 176–179:
```python
state_req_id = state.get("approval_request_id")
row_req_id = row.get("approval_request_id")
if state_req_id and row_req_id and str(row_req_id) != str(state_req_id):
    raise ApprovalNotCommitted("committed decision refers to a different approval request")
```
Because `state_req_id` is always `None`, the compound condition `if state_req_id and row_req_id:` evaluates to falsy. Consequently, if an attacker or misconfiguration associates a committed decision with a different approval request, the graph fails to detect the mismatch.

### 2.4 End-to-End Alignment with `ApprovalStore.complete_run`
When the graph pauses at `interrupt(state["approval_request"])`, execution returns to `create_run_internal` in `src/graph_api.py`, which calls:
`final = store.complete_run(state["thread_id"], result)`
For the validation in `approval` to succeed on legitimate runs:
- `prepare_approval` must generate `req_id = state.get("approval_request_id") or str(uuid4())`.
- `result["approval_request_id"]` (and `result["approval_request"]["approval_request_id"]`) will contain this `req_id`.
- `store.complete_run` must use this `req_id` when executing `INSERT INTO public.approval_requests (approval_request_id, thread_id, ...)` so that PostgreSQL's `approval_requests.approval_request_id` and `graph_threads.approval_request_id` match the checkpointed `req_id`.
- When resumed, `deps.decision_reader(thread_id)` loads `row["approval_request_id"] == req_id == state["approval_request_id"]`, and the check succeeds.

---

## 3. Specification & Invariant Compliance

| Specification | Requirement | Resolution in `src/graph.py` |
|---|---|---|
| **Spec §4.3** | "The interrupt payload must contain only identifiers and display-safe data... Include the approved subset of: thread_id; approval_request_id; package_id; package_version; package_hash..." | `prepare_approval` populates `request["approval_request_id"]` with a valid UUID string. |
| **Spec §4.4** | "After resume, the Graph must reread the authoritative PostgreSQL decision. Validate: decision exists; approval request matches; package ID matches; package version matches..." | `approval` node actively compares `state_req_id` with `row_req_id` and raises `ApprovalNotCommitted` on mismatch. |
| **Spec §11.2** | "Approval request binds exact package ID/version/hash. Graph checkpoint persists while awaiting approval." | `approval_request_id` persists in the LangGraph checkpoint across pause/resume cycles. |

---

## 4. Recommended Exact Code Modifications for Worker

### Modification 1: Import `uuid4` in `src/graph.py`
Add `uuid4` import to line 26 of `src/graph.py`:

```python
from typing import Annotated, Any, Callable, TypedDict
from uuid import uuid4
```

### Modification 2: Add `approval_request_id: str | None` to `FC27State`
In `src/graph.py` (around line 64):

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
    approval_request_id: str | None
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

### Modification 3: Update `prepare_approval` in `src/graph.py`
In `src/graph.py` (lines 138–160):

```python
    def prepare_approval(state: FC27State) -> dict:
        results = {k: RoleResult.model_validate(v) for k, v in (state.get("role_reports") or {}).items()}
        packages = build_lead_packages(state.get("dashboard", {}), state["club_id"], results, state["thread_id"])
        version = package_version(packages)
        approvable = sorted(k for k, v in packages.items() if v.get("approvable"))
        req_id = state.get("approval_request_id") or str(uuid4())
        request = {
            "thread_id": state["thread_id"],
            "package_version": version,
            "package_hash": version,
            "approvable_owners": approvable,
            "approval_request_id": req_id,
            "safe_summary": f"approvable owners: {approvable or 'none'}",
            "required_authority": "commissioner",
            "fc_version": FC_VERSION,
            "platform": state["platform"],
        }
        return {
            "lead_packages": packages,
            "package_version": version,
            "approval_request_id": req_id,
            "approval_request": request,
            "status": "awaiting_approval",
            "audit_log": _audit("prepare_approval", state, f"approvable owners: {approvable or 'none'}"),
        }
```

### Modification 4: Active Validation in `approval` Node
In `src/graph.py` (lines 176–180):

```python
        state_req_id = state.get("approval_request_id")
        row_req_id = row.get("approval_request_id")
        if state_req_id and row_req_id and str(row_req_id) != str(state_req_id):
            raise ApprovalNotCommitted("committed decision refers to a different approval request")
```

*(Note: If strict failure is desired when a committed row is missing an approval request entirely, the worker may use `if state_req_id and (not row_req_id or str(row_req_id) != str(state_req_id)):`.)*

---

## 5. Machine-Applicable Diff Patch

```diff
--- a/src/graph.py
+++ b/src/graph.py
@@ -26,6 +26,7 @@
 from typing import Annotated, Any, Callable, TypedDict
+from uuid import uuid4
 
 import httpx
 from langgraph.graph import END, START, StateGraph
@@ -62,6 +63,7 @@
     role_reports: dict[str, dict]
     lead_packages: dict[str, dict]
     package_version: str | None
+    approval_request_id: str | None
     approval_request: dict
     approval_decision: dict
     status: str
@@ -140,11 +142,12 @@
         packages = build_lead_packages(state.get("dashboard", {}), state["club_id"], results, state["thread_id"])
         version = package_version(packages)
         approvable = sorted(k for k, v in packages.items() if v.get("approvable"))
+        req_id = state.get("approval_request_id") or str(uuid4())
         request = {
             "thread_id": state["thread_id"],
             "package_version": version,
             "package_hash": version,
             "approvable_owners": approvable,
-            "approval_request_id": state.get("approval_request_id"),
+            "approval_request_id": req_id,
             "safe_summary": f"approvable owners: {approvable or 'none'}",
             "required_authority": "commissioner",
             "fc_version": FC_VERSION,
             "platform": state["platform"],
         }
         return {
             "lead_packages": packages,
             "package_version": version,
+            "approval_request_id": req_id,
             "approval_request": request,
             "status": "awaiting_approval",
             "audit_log": _audit("prepare_approval", state, f"approvable owners: {approvable or 'none'}"),
         }
```

---

## 6. Verification Method

Once the worker applies these changes in `src/graph.py` (alongside the partner changes in `src/approval_store.py` and `tests/test_graph_persistence.py`):
1. **Targeted Adversarial Test**:
   Remove the `@pytest.mark.xfail` decorator on `test_adversarial_resume_refusal_mismatched_approval_request_id` in `tests/test_graph_persistence.py`.
   Execute:
   ```powershell
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_graph_persistence.py -k test_adversarial_resume_refusal_mismatched_approval_request_id
   ```
   **Expected Result**: PASS (raises `ApprovalNotCommitted("committed decision refers to a different approval request")`).
2. **Full Persistence Suite**:
   ```powershell
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_graph_persistence.py
   ```
   **Expected Result**: 20 passed, 0 failed, 0 xfailed.
3. **Graph API and Flow Suites**:
   ```powershell
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_langgraph_flow.py tests/test_graph_api_v1.py
   ```
   **Expected Result**: 100% pass, zero regressions.
