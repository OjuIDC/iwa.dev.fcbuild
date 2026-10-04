# Handoff Report: Reviewer Milestone 2 (Graph Core & Persistence)

**Agent**: `reviewer_m2_1` (reviewing Milestone 2: Graph Core & Persistence)  
**Date**: 2026-10-03  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m2_1`  
**Verdict**: **APPROVE**  

---

## 1. Observation

1. **Single Approval Interrupt**:
   - `src/graph.py:164`: `interrupt(state["approval_request"])` is invoked exactly once within the `approval(state: FC27State)` function.
   - `src/graph.py:264`: `self.app = build_graph(deps).compile(checkpointer=checkpointer)` compiles the StateGraph without `interrupt_before`.
   - Grep search for `interrupt_before`: found only in `ORIGINAL_REQUEST.md` (lines 207, 665) and as negative assertions in `tests/test_graph_persistence.py` (lines 4, 57, 73).
   - Grep search for `interrupt(` in `src/`: found only in `src/graph.py` at line 12 (docstring) and line 164 (`interrupt(state["approval_request"])`).

2. **Authoritative PostgreSQL Decision Re-read on Resume**:
   - `src/graph.py:165`: `row = deps.decision_reader(state["thread_id"])` directly queries PostgreSQL committed state.
   - `src/graph.py:166-186`:
     - `if not row or row.get("status") not in DECIDED_STATUSES: raise ApprovalNotCommitted("no committed Commissioner decision for this thread")`
     - `if payload.get("package_version") != expected_version: raise ApprovalNotCommitted("committed decision refers to a different package version")`
     - `if row.get("package_version") != expected_version: raise ApprovalNotCommitted("committed thread refers to a different package version")`
     - `if state_req_id and row_req_id and str(row_req_id) != str(state_req_id): raise ApprovalNotCommitted("committed decision refers to a different approval request")`
     - `if actor_type and actor_type != "commissioner": raise ApprovalNotCommitted(f"unsupported actor type in Phase 1: {actor_type}")`
     - `if authority_basis and authority_basis != "primary": raise ApprovalNotCommitted(f"unsupported authority basis in Phase 1: {authority_basis}")`
   - `src/graph.py:298-300`: `GraphRunner.resume(thread_id)` checks `if not row or row.get("status") not in DECIDED_STATUSES: return self.values(thread_id)` before calling `self.app.invoke(...)`.
   - `src/graph_api.py:227`: `decision_reader=store.load` is wired into `GraphDeps`.

3. **Terminal State Statements**:
   - `src/graph.py:45-49`:
     ```python
     TERMINAL_STATUS_MAP = {
         "approved": "APPROVED, NOT DISPATCHED",
         "rejected": "REJECTED, NO ACTION TAKEN",
         "revision_requested": "REVISION_REQUESTED, NO ACTION TAKEN",
     }
     ```
   - Grep search for `"APPROVED and ACTIVE"` across entire codebase returned 0 occurrences in source code (only line 190 in `tests/test_graph_persistence.py` verifying its absence and line 292 of `ORIGINAL_REQUEST.md`).

4. **LangGraph Checkpoint Persistence Role**:
   - `PostgresSaver(pool)` records node executions with `durability="sync"`.
   - Workflow state is restored upon process reboot (`test_checkpoint_recovery_across_server_restart`).
   - Checkpoint does not store authorization; tampering with checkpoint state via `app.update_state` fails authorization checks because the graph re-reads PostgreSQL (`test_resume_payload_tampering_ignored`).

5. **Test Execution Results**:
   - Command:
     ```powershell
     $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_graph_persistence.py tests/test_langgraph_flow.py
     ```
   - Output:
     `46 passed in 69.00s (0:01:08)`
   - Exit code: 0.

6. **Integrity Check**:
   - No hardcoded test responses, fake bypasses, dummy implementations, or skipped verification tests.

---

## 2. Logic Chain

1. From Observation 1, the workflow graph enforces a single interruption point inside the `approval` node without double-interruption (`interrupt_before`). Therefore, Criterion 1 (Single Interrupt Verification) is completely satisfied.
2. From Observation 2, when the graph is resumed, any resume value passed to `interrupt()` or `Command(resume=...)` is discarded. The approval node explicitly invokes `deps.decision_reader(state["thread_id"])` to re-read PostgreSQL committed state. If the decision is missing, uncommitted, has a mismatched package version or approval request ID, or violates Phase 1 Section 1A constraints (`actor_type="commissioner"`, `authority_basis="primary"`), it raises `ApprovalNotCommitted`. Therefore, Criterion 2 (PostgreSQL Authoritative Decision Re-read) is completely satisfied.
3. From Observation 3, terminal outcomes are explicitly mapped via `TERMINAL_STATUS_MAP` to `"APPROVED, NOT DISPATCHED"`, `"REJECTED, NO ACTION TAKEN"`, and `"REVISION_REQUESTED, NO ACTION TAKEN"`, with zero occurrences of `"APPROVED and ACTIVE"`. Therefore, Criterion 3 (Terminal State Statements) is completely satisfied.
4. From Observation 4, checkpoints persist workflow state across nodes and restarts, but authorization state is exclusively read from PostgreSQL tables. Checkpoint tampering fails closed. Therefore, Criterion 4 (Checkpoint Persistence Role) is completely satisfied.
5. From Observation 5, all 46 test cases covering graph persistence, single interrupt, resume security, correlation middleware, and API projections pass cleanly on a live PostgreSQL database.
6. From Observation 6, the implementation contains genuine logic without shortcuts or integrity violations.
7. Consequently, the work product meets all milestone acceptance criteria and earns an unconditional **APPROVE** verdict.

---

## 3. Caveats

- **Scope Boundary**: This review focused on Milestone 2 (Graph Core & Persistence). Milestone 3 (Dispatches & Single Write Path Transition Function) and subsequent milestones remain planned and unexecuted.
- **HMAC CLI Proof**: During review testing, CLI HMAC proof verification was tested with both active signing and the kill switch (`APPROVAL_CLI_HMAC_ENABLED=false`).
- **No Player Delivery**: Verified that `DISPATCH_ENABLED=false` remains enforced at all levels and `/health/ready` probe verifies this flag.

---

## 4. Conclusion

Milestone 2 (Graph Core & Persistence) is **APPROVED**.
The graph correctly implements single interrupt pausing, fails closed against uncommitted decisions and forged resume payloads, reads authoritative decisions exclusively from PostgreSQL, formats terminal state strings with explicit no-dispatch language, and recovers state across server restarts.

---

## 5. Verification Method

To independently reproduce and verify this assessment:

1. **Verify Single Interrupt & Absence of `interrupt_before`**:
   ```powershell
   Select-String -Path src\graph.py -Pattern "interrupt_before"
   # Expected: No matches
   Select-String -Path src\graph.py -Pattern "interrupt\("
   # Expected: Exactly line 164
   ```

2. **Verify Absence of Forbidden Phrase**:
   ```powershell
   Get-ChildItem -Recurse -Include *.py | Select-String -Pattern "APPROVED and ACTIVE"
   # Expected: Only tests\test_graph_persistence.py line 190 (negative assertion)
   ```

3. **Execute Milestone 2 Test Suite**:
   ```powershell
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_graph_persistence.py tests/test_langgraph_flow.py
   # Expected: 46 passed in ~70s, exit code 0
   ```
