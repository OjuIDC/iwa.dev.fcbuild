# Milestone 2 Independent Review Report: Graph Core & Persistence

**Reviewer**: `reviewer_m2_1`  
**Date**: 2026-10-03  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m2_1`  
**Verdict**: **APPROVE**  

---

## 1. Executive Summary

Milestone 2 implementation of LangGraph Core and Persistence has been thoroughly evaluated against the programme requirements (`ORIGINAL_REQUEST.md`, Section 1A patch, and `PROJECT.md`).

All core invariants have been verified:
1. **Single Approval Interrupt**: Verified exactly one `interrupt()` call located in the `approval` node in `src/graph.py` (line 164). Verified zero occurrences of `interrupt_before=["approval"]` across the entire codebase.
2. **Authoritative Decision Re-read on Resume**: Verified that resuming the graph directly invokes `deps.decision_reader(thread_id)` querying PostgreSQL `graph_threads`. Untrusted resume payloads (`Command(resume=...)` or HTTP body) are completely discarded. Mismatched package version, wrong approval request ID, uncommitted status, or invalid Section 1A authority basis immediately raise `ApprovalNotCommitted`, failing closed.
3. **Terminal State Formatting**: Formatted strictly as `"APPROVED, NOT DISPATCHED"`, `"REJECTED, NO ACTION TAKEN"`, and `"REVISION_REQUESTED, NO ACTION TAKEN"`. Verified zero occurrences of `"APPROVED and ACTIVE"` across all code and documentation.
4. **Checkpoint Role Separation**: Checkpoints via `PostgresSaver` store workflow execution state and history, not authorization state. Direct checkpoint tampering fails to bypass the PostgreSQL re-read.
5. **Test Verification**: Executed `tests/test_graph_persistence.py` and `tests/test_langgraph_flow.py` against live PostgreSQL (`postgresql://fc27test@127.0.0.1:55432/postgres`), passing all 46 test cases in 69.00s.
6. **Integrity Audit**: Zero hardcoded test outputs, zero facade/dummy implementations, and zero test cheats detected.

---

## 2. In-Depth Technical Verification

### 2.1 Single Interrupt Verification (AC §11.2 / Feature 18)
- **Source Inspection (`src/graph.py`)**:
  - Exactly one `interrupt()` call exists at line 164 inside node `approval`:
    ```python
    def approval(state: FC27State) -> dict:
        # Pauses here until the Graph API resumes the thread after a committed decision.
        interrupt(state["approval_request"])
    ```
  - The graph compilation at line 264 uses `build_graph(deps).compile(checkpointer=checkpointer)`.
  - No `interrupt_before` argument is passed.
  - Upstream analytical pipeline (`search -> scout -> analyst -> coach -> tactics -> director -> prepare_approval`) executes uninterrupted before pausing at `approval`.
- **Codebase Grep**:
  - `interrupt_before`: Found only in `ORIGINAL_REQUEST.md` and negative assertion in `tests/test_graph_persistence.py:73`.
  - `interrupt(`: Found only in `src/graph.py:164`.

### 2.2 PostgreSQL Authoritative Decision Re-read on Resume (AC §11.2 / Feature 19)
- **Implementation Mechanism**:
  - Resumption in `GraphRunner.resume(thread_id)` checks `deps.decision_reader(thread_id)["status"] in DECIDED_STATUSES` before attempting invocation.
  - In `approval(state)`:
    - Line 164: `interrupt(state["approval_request"])` (return value discarded).
    - Line 165: `row = deps.decision_reader(state["thread_id"])` (reads committed PostgreSQL thread row).
    - Validation checks:
      1. `if not row or row.get("status") not in DECIDED_STATUSES:` raises `ApprovalNotCommitted("no committed Commissioner decision for this thread")`.
      2. `if payload.get("package_version") != expected_version:` raises `ApprovalNotCommitted("committed decision refers to a different package version")`.
      3. `if row.get("package_version") != expected_version:` raises `ApprovalNotCommitted("committed thread refers to a different package version")`.
      4. `if state_req_id and row_req_id and str(row_req_id) != str(state_req_id):` raises `ApprovalNotCommitted("committed decision refers to a different approval request")`.
      5. `if actor_type and actor_type != "commissioner":` raises `ApprovalNotCommitted("unsupported actor type in Phase 1: ...")`.
      6. `if authority_basis and authority_basis != "primary":` raises `ApprovalNotCommitted("unsupported authority basis in Phase 1: ...")`.
- **Untrusted Input Invalidation**:
  - `Command(resume=...)` payloads are never passed to state or read by node logic.
  - HTTP `POST /v1/runs/{id}/resume` uses schema `ResumeRequest(extra="forbid")`, rejecting authorization injection with 422.
  - Direct state manipulation (`app.update_state(...)`) is defeated because `deps.decision_reader` rereads PostgreSQL state.

### 2.3 Terminal State Statements (AC §11.2 / Feature 20)
- **Mapping in `src/graph.py`**:
  ```python
  TERMINAL_STATUS_MAP = {
      "approved": "APPROVED, NOT DISPATCHED",
      "rejected": "REJECTED, NO ACTION TAKEN",
      "revision_requested": "REVISION_REQUESTED, NO ACTION TAKEN",
  }
  ```
- **Audit Log Emission**:
  - `approval` node logs: `{terminal_status} by {row.get('approved_by')}`.
  - `finalize_decision` node logs: `run finalized as {status}`.
- **Negative Assertion**:
  - Codebase search for `"APPROVED and ACTIVE"` yielded 0 occurrences in source code (only the negative assertion in `test_graph_persistence.py` and specification in `ORIGINAL_REQUEST.md`).

### 2.4 Checkpoint Persistence Role
- **Separation of Concerns**:
  - Checkpoints managed via `PostgresSaver(pool)` record state progression across nodes (`durability="sync"`).
  - Checkpoints carry workflow context (`thread_id`, `club_id`, `candidates`, `lead_packages`, `package_version`, `approval_request`).
  - Authorization state is strictly isolated in PostgreSQL domain tables (`graph_threads`, `approval_requests`, `approval_audit`).
  - Server restart recovery preserves the interrupted graph while ensuring completion requires a committed PostgreSQL decision (`test_checkpoint_recovery_across_server_restart`).
  - Startup lifespan reconciliation in `src/graph_api.py` scans `store.terminal_thread_ids()` and resumes uncompleted threads if a crash occurred post-commit (`test_startup_reconciliation_resumes_committed_threads`).

---

## 3. Adversarial Review & Stress-Testing

### 3.1 Challenge Dimensions Evaluated

1. **Adversarial Scenario: Forged Resume Payload Injection**
   - *Attack*: Attacker submits `Command(resume={"approved": True, "status": "approved", "approved_by": "attacker"})`.
   - *Result*: `approval` node completely discards the resume value, invokes `deps.decision_reader`, observes no committed decision, and raises `ApprovalNotCommitted`.
   - *Outcome*: **BLOCKED**.

2. **Adversarial Scenario: Checkpoint State Manipulation**
   - *Attack*: Attacker calls `app.update_state({"configurable": {"thread_id": tid}}, {"status": "approved", "approval_decision": {"approved_by": "attacker"}})`.
   - *Result*: On resume, node re-reads PostgreSQL committed state via `deps.decision_reader(tid)`. Since DB state is `awaiting_approval`, raises `ApprovalNotCommitted`.
   - *Outcome*: **BLOCKED**.

3. **Adversarial Scenario: Stale/Mismatched Package Version Attack**
   - *Attack*: Decision committed in PostgreSQL against version `V1`, but thread prepared package version `V2`.
   - *Result*: `if payload.get("package_version") != expected_version` and `if row.get("package_version") != expected_version` check detects mismatch and raises `ApprovalNotCommitted("committed decision refers to a different package version")`.
   - *Outcome*: **BLOCKED**.

4. **Adversarial Scenario: Repeated Resume on Terminal Thread**
   - *Attack*: Caller repeatedly calls `resume(tid)` on a thread that already reached terminal state (`APPROVED, NOT DISPATCHED`).
   - *Result*: `GraphRunner.resume` checks `self._approval_pending(snap)`. Because no approval task is pending, returns existing snapshot without re-entering approval or appending duplicate audit entries.
   - *Outcome*: **SAFE NO-OP**.

5. **Adversarial Scenario: Premature Resume Before DB Commit**
   - *Attack*: Resume triggered concurrently before PostgreSQL decision transaction commits.
   - *Result*: PostgreSQL `status` remains `'awaiting_approval'`. `deps.decision_reader` returns non-terminal status, raising `ApprovalNotCommitted`. Once the transaction commits, subsequent resume succeeds cleanly.
   - *Outcome*: **FAIL-CLOSED CONCURRENCY SAFETY**.

6. **Adversarial Scenario: Phase 1 Safety Configuration Violation (`DISPATCH_ENABLED=true`)**
   - *Attack*: Environment configured with `DISPATCH_ENABLED=true`.
   - *Result*: `/health/ready` probe detects invariant violation and returns 503 Service Unavailable (`"Phase 1 safety invariant violation: DISPATCH_ENABLED must be false"`).
   - *Outcome*: **PROBE REJECTION**.

---

## 4. Integrity Violation Check

A systematic check for integrity violations was conducted:
- [x] **No hardcoded test outputs**: Source code in `src/` does not embed specific thread IDs, test-specific hashes, or fake return data.
- [x] **No facade implementations**: Real `StateGraph`, real `PostgresSaver`, real `ConnectionPool`, real FastAPI middleware, and real PostgreSQL transactions.
- [x] **No shortcuts**: Single interrupt, authoritative PostgreSQL re-read, HMAC validation, and Section 1A authority validation are fully enforced.
- [x] **No fabricated test logs**: Test execution verified with live pytest run exiting code 0 with 46 passes.
- [x] **Verdict**: CLEAN.

---

## 5. Review Verdict

**Verdict**: **APPROVE**  
Milestone 2 meets all functional, architectural, and security requirements without defects or regressions.
