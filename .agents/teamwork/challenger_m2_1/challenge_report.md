# Adversarial Challenge Report: Milestone 2 Graph Core & Persistence

- **Challenger Agent**: `challenger_m2_1` (teamwork_preview_challenger)
- **Target Role**: `worker_m2_1` (Graph Core & Persistent Graph API)
- **Target Files**: `src/graph.py`, `src/graph_api.py`, `src/schemas.py`, `tests/test_graph_persistence.py`
- **Execution Date**: 2026-10-03
- **Verdict**: **REQUEST_CHANGES**

---

## Executive Summary

We performed an adversarial empirical challenge against the LangGraph Core & Persistence implementation across four required vectors:
1. **Resume Payload Tamper Resistance**: Injection of forged resume bodies, mutated checkpoint states, cross-thread identity substitution, and API body validation.
2. **Uncommitted Resume Refusal**: Resumption against uncommitted PostgreSQL rows, non-terminal statuses, unsupported Section 1A actor types (`player`, `delegate`, `agent`), unsupported Section 1A authority bases (`affected_player_fallback`, `affected_player_consent`, `explicit_delegate`), and approval request binding.
3. **Checkpoint Restart & Crash Recovery**: Brutal server crash while paused at the approval interrupt, cold start reconstruction of `GraphRunner` and `PostgresSaver`, cold state preservation, cold resume refusal while uncommitted, and clean resume to terminal status upon committed PostgreSQL decision.
4. **Terminal State Exact Formatting**: Dynamic and static verification of all three terminal states (`"APPROVED, NOT DISPATCHED"`, `"REJECTED, NO ACTION TAKEN"`, `"REVISION_REQUESTED, NO ACTION TAKEN"`), audit log phrasing, and rigorous scanning for forbidden strings (`"APPROVED and ACTIVE"`, `"ACTIVE"`, `"dispatched"`, `"delivered"`).

**Result**: 19 test cases passed cleanly, demonstrating outstanding tamper resistance, rock-solid PostgreSQL state enforcement, robust restart recovery, and strict terminal state formatting.

**However, we uncovered 1 critical architectural defect**:
In `src/graph.py`, `approval_request_id` is omitted from `FC27State` (`TypedDict`), and is not generated prior to the approval interrupt in `prepare_approval`. Consequently:
1. The approval interrupt payload emitted in `prepare_approval` contains `"approval_request_id": None`, violating **Spec §4.3** ("Include the approved subset of: thread_id; approval_request_id; package_id; package_version; package_hash...").
2. Inside `approval(state)`, `state_req_id = state.get("approval_request_id")` evaluates to `None`. The check at lines 176–179:
   ```python
   state_req_id = state.get("approval_request_id")
   row_req_id = row.get("approval_request_id")
   if state_req_id and row_req_id and str(row_req_id) != str(state_req_id):
       raise ApprovalNotCommitted("committed decision refers to a different approval request")
   ```
   is **dead code** and never executes, violating **Spec §4.4** ("Validate: approval request matches").

Because of this defect, our verdict is **REQUEST_CHANGES**.

---

## Challenges & Empirical Findings

### Challenge 1 [High]: Dead Code in Approval Request ID Validation & Incomplete Interrupt Payload

- **Spec Requirement**:
  - `ORIGINAL_REQUEST.md` §4.3: "The interrupt payload must contain only identifiers and display-safe data... Include the approved subset of: thread_id; approval_request_id; package_id; package_version; package_hash; safe summary; required approving authority."
  - `ORIGINAL_REQUEST.md` §4.4: "After resume, the Graph must reread the authoritative PostgreSQL decision. Validate: decision exists; approval request matches; package ID matches; package version matches; package hash matches..."
- **Observation**:
  1. `src/graph.py` lines 52–74 defines `FC27State(TypedDict)` without the field `approval_request_id`.
  2. In `prepare_approval` (`src/graph.py` lines 143–154), the request payload is constructed with `"approval_request_id": state.get("approval_request_id")`. Because `state` does not yet contain `approval_request_id`, this value is `None`. The interrupt payload therefore has `"approval_request_id": None`.
  3. In `src/graph_api.py`, `store.complete_run` generates the UUID for `approval_request_id` in PostgreSQL *after* the graph has already interrupted. It does not update LangGraph's checkpointer.
  4. In `approval` (`src/graph.py` lines 176–179):
     ```python
     state_req_id = state.get("approval_request_id")
     row_req_id = row.get("approval_request_id")
     if state_req_id and row_req_id and str(row_req_id) != str(state_req_id):
         raise ApprovalNotCommitted("committed decision refers to a different approval request")
     ```
     Because `state_req_id` is `None`, the guard `if state_req_id and row_req_id:` evaluates to falsy, and the validation is permanently bypassed.
- **Empirical Evidence**:
  Test `test_adversarial_resume_refusal_mismatched_approval_request_id` in `tests/test_graph_persistence.py` failed when asserting that `ApprovalNotCommitted` is raised upon a mismatched `approval_request_id`. The test passed only when marked `xfail` with strict enforcement:
  `E Failed: DID NOT RAISE <class 'src.graph.ApprovalNotCommitted'>`
- **Blast Radius**:
  While PostgreSQL triggers (`fc27_check_approval_pair()`) and `ApprovalStore` prevent mismatched database row transitions, the LangGraph workflow core itself fails its independent verification invariant to validate that the resumed thread binds to the exact intended `approval_request_id`, and human interfaces receiving the interrupt payload will receive `approval_request_id: null`.
- **Recommended Remediation for Worker**:
  1. Add `approval_request_id: str | None` to `FC27State` in `src/graph.py`.
  2. In `prepare_approval`, if `state.get("approval_request_id")` is absent, generate it deterministically or via `uuid4()`:
     ```python
     req_id = state.get("approval_request_id") or str(uuid4())
     ```
     and return `"approval_request_id": req_id` in the `prepare_approval` return dictionary so it persists in the LangGraph checkpoint and populates `request["approval_request_id"]`.
  3. In `ApprovalStore.complete_run` (`src/approval_store.py`), accept or honor the `approval_request_id` from `result["approval_request"]["approval_request_id"]` when inserting into `public.approval_requests` instead of generating a disjoint UUID.

---

## Adversarial Attack Vectors & Results

| Attack Vector | Target / Mechanism | Expected Behavior | Actual Behavior | Result |
|---|---|---|---|---|
| **Attack 1.1** | Inject forged `Command(resume=...)` payloads (`{"decision": "approved", "authorized": true}`, `"APPROVE"`, raw boolean, etc.) | Graph ignores payload; raises `ApprovalNotCommitted`; thread remains awaiting approval | Raised `ApprovalNotCommitted`; state stayed `awaiting_approval` | **PASS** |
| **Attack 1.2** | Direct manipulation of LangGraph checkpoint state prior to resume (`update_state` with `status="approved"`) | Graph re-reads PostgreSQL committed state; detects uncommitted DB; fails closed | Overridden state ignored; raised `ApprovalNotCommitted` | **PASS** |
| **Attack 1.3** | Cross-thread identity hijacking: inject an already-approved thread ID into a pending thread's checkpoint | Graph detects expected package version / request mismatch; fails closed | Raised `ApprovalNotCommitted`; pending thread not advanced | **PASS** |
| **Attack 1.4** | Payload tampering on HTTP route `POST /v1/runs/{id}/resume` with extra fields (`{"decision": "approved"}`) | FastAPI schema rejects with HTTP 422 Unprocessable Entity | Returned 422 `extra fields not permitted` | **PASS** |
| **Attack 1.5** | Resume call with malicious payload body (`{"status": "MALICIOUS", "approved_by": "EVIL"}`) when valid DB decision exists | Graph completely discards resume payload and populates state exclusively from PostgreSQL | Values populated exclusively from PostgreSQL (`approved_by="batmanL8"`, `status="APPROVED, NOT DISPATCHED"`) | **PASS** |
| **Attack 2.1** | Resuming thread when PostgreSQL row is missing (`decision_reader` returns `None`) | Graph raises `ApprovalNotCommitted` | Raised `ApprovalNotCommitted("no committed Commissioner decision for this thread")` | **PASS** |
| **Attack 2.2** | Resuming thread when PostgreSQL status is non-terminal (`running`, `awaiting_approval`, `expired`) | `runner.resume()` no-ops; direct invoke raises `ApprovalNotCommitted` | Returned awaiting values on `resume()`; raised `ApprovalNotCommitted` on direct invoke | **PASS** |
| **Attack 2.3** | Section 1A Actor Type Enforcement: DB decision with `actor_type` in `('player', 'delegate', 'agent')` | Graph raises `ApprovalNotCommitted("unsupported actor type in Phase 1")` | Raised `ApprovalNotCommitted("unsupported actor type in Phase 1: ...")` | **PASS** |
| **Attack 2.4** | Section 1A Authority Basis Enforcement: DB decision with `authority_basis` in `('affected_player_fallback', 'affected_player_consent', 'explicit_delegate')` | Graph raises `ApprovalNotCommitted("unsupported authority basis in Phase 1")` | Raised `ApprovalNotCommitted("unsupported authority basis in Phase 1: ...")` | **PASS** |
| **Attack 2.5** | Committed decision referencing a different `approval_request_id` | Graph raises `ApprovalNotCommitted("different approval request")` | `FC27State` dropped field; check was bypassed as dead code | **FAIL (XFAIL)** |
| **Attack 2.6** | HTTP route `POST /v1/runs/{id}/resume` when thread is uncommitted | API returns HTTP 409 Conflict | Returned 409 `{"detail": "approval_not_committed"}` | **PASS** |
| **Attack 3.1** | Brutal process crash while paused at approval interrupt; cold start with fresh runner and connection pool | Cold runner cleanly re-reads interrupted checkpoint, preserves all node reports and version, refuses uncommitted resume, and advances only upon valid DB commit | Clean cold recovery; status remained awaiting; completed to `"APPROVED, NOT DISPATCHED"` only after DB decision | **PASS** |
| **Attack 3.2** | Crash recovery with rejection decision | Cold runner cleanly resumes to `"REJECTED, NO ACTION TAKEN"` | Resumed to `"REJECTED, NO ACTION TAKEN"`; graph ended cleanly | **PASS** |
| **Attack 4.1** | Exact terminal string verification and forbidden strings audit across all 3 terminal statuses (`approved`, `rejected`, `revision_requested`) | All statuses match exact strings; zero occurrences of `"APPROVED and ACTIVE"`, `"ACTIVE"`, or active dispatch words in statuses or audit logs | Zero forbidden strings; exact terminal strings verified in state and audit summaries | **PASS** |

---

## Stress Test Results Summary

- Total tests in `tests/test_graph_persistence.py`: **20**
- Baseline Worker tests: **8 passed**
- Adversarial Challenger tests: **11 passed, 1 xfailed**
- Execution command:
  ```powershell
  $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_graph_persistence.py
  ```
- Output: `19 passed, 1 xfailed in 34.09s` (exit code 0)

---

## Verdict & Actionable Recommendation

**Verdict**: **REQUEST_CHANGES**

**Action Items for Worker**:
1. Update `FC27State` in `src/graph.py` to include `approval_request_id: str | None`.
2. Generate or populate `approval_request_id` in `prepare_approval` so that:
   - The approval interrupt payload exposes the valid `approval_request_id` per Spec §4.3.
   - The checkpoint retains `approval_request_id`.
   - The validation at lines 176–179 in `approval` executes active comparison rather than evaluating dead code.
3. Update `complete_run` in `src/approval_store.py` to reuse the graph's `approval_request_id` if present in the state.
