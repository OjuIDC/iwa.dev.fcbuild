# Handoff Report: Survey of Graph, Graph API, and Runtime State

**Agent:** explorer_survey_graph_1  
**Role:** teamwork_preview_explorer  
**Working Directory:** `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_survey_graph_1`  
**Recipient:** Orchestrator (Conversation ID: `7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b`)  
**Artifacts Generated:**  
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_survey_graph_1\survey_graph_report.md`  
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_survey_graph_1\handoff.md`  
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_survey_graph_1\BRIEFING.md`  
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_survey_graph_1\progress.md`

---

## 1. Observation

1. **Interruption Mechanism:**
   - In `src/graph.py:141-144`:
     ```python
     def approval(state: FC27State) -> dict:
         # Pauses here until the Graph API resumes the thread after a committed decision.
         interrupt(state["approval_request"])
         row = deps.decision_reader(state["thread_id"])
     ```
   - In `src/graph.py:214`:
     ```python
     self.app = build_graph(deps).compile(checkpointer=checkpointer)
     ```
     `interrupt_before` is not passed to `.compile()`.
   - Grep for `interrupt_before`: Only matches lines 207 and 665 in `ORIGINAL_REQUEST.md`. No occurrence in `src/` or `tests/`.

2. **Resume Mechanism & Authority:**
   - In `src/graph.py:143-149`:
     ```python
     interrupt(state["approval_request"])
     row = deps.decision_reader(state["thread_id"])
     if not row or row.get("status") not in ("approved", "rejected"):
         raise ApprovalNotCommitted("no committed Commissioner decision for this thread")
     payload = row.get("decision_payload") or {}
     if payload.get("package_version") != state.get("package_version"):
         raise ApprovalNotCommitted("committed decision refers to a different package version")
     ```
   - In `src/graph.py:244-254`:
     ```python
     def resume(self, thread_id: str) -> dict:
         snap = self.app.get_state({"configurable": {"thread_id": thread_id}})
         if not self._approval_pending(snap):
             return dict(snap.values)
         v = snap.values
         try:
             self.app.invoke(Command(resume={"thread_id": thread_id}),
                             run_config(thread_id, v["club_id"], v.get("platform", DEFAULT_PLATFORM)), durability="sync")
         finally:
             self.tracer.flush()
         return self.values(thread_id)
     ```
   - Resume ignores the value returned by `interrupt()`.
   - The validation checks only `row["status"]` and `payload.get("package_version")`. It does not validate `approval_request_id`, package hash, or expiry.

3. **Dispatch Key Leak in `src/decision.py`:**
   - In `src/decision.py:48-54`:
     ```python
     if approved:
         new_state["dispatch"] = {
             "owner": intent.owner.value,
             "selection": intent.selection.value,
             "candidate": candidate,
             "dispatched_to_player_agent": intent.player_id,
         }
     ```
   - In `ORIGINAL_REQUEST.md:42-44`:
     > "4. No Graph node may transition to dispatched, delivered, acknowledged, activated, or tracking.
     > 5. Approved and rejected are terminal Phase 1 outcomes."

4. **Terminal States & Wording:**
   - In `src/graph.py:153-157`:
     ```python
     return {"approval_decision": decision, "status": row["status"],
             "audit_log": _audit("approval", state, f"{row['status']} by {row.get('approved_by')}")}
     ...
     def finalize_decision(state: FC27State) -> dict:
         return {"audit_log": _audit("finalize_decision", state, f"run finalized as {state['status']}")}
     ```
   - In `src/decision.py:57`:
     ```python
     status="approved" if approved else "rejected"
     ```
   - The wording `"APPROVED and ACTIVE"` is absent from the repository.
   - The required terminal wording `"APPROVED, NOT DISPATCHED"` and `"REJECTED, NO ACTION TAKEN"` is absent.

5. **Persistent Graph API Endpoints:**
   - In `src/graph_api.py:184-243`:
     - Line 184: `@app.get("/health")`
     - Line 193: `@app.post("/run", status_code=201)`
     - Line 218: `@app.get("/threads/{thread_id}")`
     - Line 227: `@app.post("/threads/{thread_id}/decision-challenge")`
     - Line 235: `@app.post("/threads/{thread_id}/decision")`
   - All endpoints are unversioned.
   - Missing routes: `POST /v1/runs`, `GET /v1/runs/{thread_id}`, `GET /v1/runs/{thread_id}/approval-request`, `POST /v1/approval-requests/{approval_request_id}/decisions`, `POST /v1/runs/{thread_id}/resume`, `GET /health/live`, `GET /health/ready`, `GET /v1/config/dispatch`.
   - `GET /threads/{thread_id}` returns `dict(row["state"])`, exposing internal state.
   - `ApiSettings` in `src/graph_api.py:34-44` lacks `dispatch_enabled` and `approval_request_ttl`.

6. **Current Database & Migrations State:**
   - On branch `feat/phase1-dispatch-foundation`:
     - `migrations/0001_initial.sql`: `graph_threads`, `approval_audit`.
     - `migrations/0002_approval_security.sql`: `approval_nonces`, `run_sequences`, columns on `graph_threads`.
     - `migrations/0003_dispatch_foundation.sql`: Section A only (`domain_events`, `outbox`, append-only triggers, event source check).
     - Missing: `approval_requests`, `dispatches`, `players`, `fc27_dispatch_transition`, `activation_gate_log`.

---

## 2. Logic Chain

1. **Interruption Analysis:**
   - From Observation 1: `interrupt_before` does not exist in `compile()`. The only interruption call is `interrupt()` inside the `approval` node (`src/graph.py:143`).
   - Therefore, double interruption is already eliminated in the current code, satisfying Requirement 4.1.

2. **Resume Security Analysis:**
   - From Observation 2: `approval` never reads the return value of `interrupt()` and immediately re-queries PostgreSQL via `deps.decision_reader()`.
   - Therefore, resume payloads such as `Command(resume={"approved": True})` cannot authorize a run.
   - However, because `approval` only checks `payload.get("package_version") == state.get("package_version")`, and does not validate against an authoritative `approval_requests` table, stale or unlinked decisions could theoretically be accepted once `approval_requests` lifecycle is added unless `approval` is updated to validate the exact request ID, package hash, and request validity.

3. **Phase 1 Invariant Compliance Analysis:**
   - From Observation 3: `src/decision.py:48-54` populates `new_state["dispatch"]` with `"dispatched_to_player_agent"`.
   - This directly violates Phase 1 Invariants 1, 2, and 4 (DISPATCH_ENABLED=false, no player delivery, no transition to dispatched).
   - Therefore, `src/decision.py` must be modified to remove the legacy dispatch dict entirely.

4. **API Route Compliance Analysis:**
   - From Observation 5: Existing endpoints are unversioned and do not conform to Section 5 of `ORIGINAL_REQUEST.md`.
   - `GET /threads/{id}` leaks full state, violating the requirement for an authorized, display-safe projection.
   - `ApiSettings` lacks `dispatch_enabled`, meaning the Graph API does not yet actively enforce `DISPATCH_ENABLED=false` at startup and in responses.

---

## 3. Caveats

1. **Python Environment Execution:**
   - The default `python` executable on PATH is Python 3.14 without `pytest`. Live test execution could not be run directly in this subagent session without user permission prompts for finding virtual environments. Test analysis was conducted via static source and fixture inspection.
2. **Phase 1 Scope Boundary:**
   - Migration 0003 is currently only partially written (Section A). Sections B through E (including `approval_requests` and `dispatches` tables) are defined in the plan and specification but are not yet implemented in `migrations/0003_dispatch_foundation.sql`.

---

## 4. Conclusion

1. **Graph Core (Agent B):**
   - Double interruption is already absent.
   - Single interruption point exists at `src/graph.py:143`.
   - Recommendation package creation must be refactored to persist to PostgreSQL, create an `approval_requests` entry, and emit `approval.created` before interruption.
   - The approval interrupt payload must be sanitized to contain only required identifiers and summary.
   - The `src/decision.py` legacy `"dispatch"` dictionary must be deleted.
   - Terminal states must be updated to `"APPROVED, NOT DISPATCHED"` and `"REJECTED, NO ACTION TAKEN"`.

2. **Persistent Graph API (Agent C):**
   - Must implement `/v1/` routes: `POST /v1/runs`, `GET /v1/runs/{thread_id}`, `GET /v1/runs/{thread_id}/approval-request`, `POST /v1/approval-requests/{approval_request_id}/decisions`, `POST /v1/runs/{thread_id}/resume`, `GET /health/live`, `GET /health/ready`, and `GET /v1/config/dispatch`.
   - `GET /v1/runs/{thread_id}` must return a safe, restricted projection.
   - `DISPATCH_ENABLED=false` must be wired into `ApiSettings` and enforced across startup and all run responses.

---

## 5. Verification Method

To verify these observations independently:
1. **Check for absence of `interrupt_before`:**
   ```powershell
   Select-String -Path "src\*.py" -Pattern "interrupt_before"
   ```
   *Expected: No matches.*
2. **Inspect single interruption point:**
   View `src/graph.py` lines 140 to 145 and lines 210 to 220.
3. **Inspect dispatch leak in `decision.py`:**
   View `src/decision.py` lines 48 to 55.
4. **Inspect unversioned routes in `graph_api.py`:**
   View `src/graph_api.py` lines 184, 193, 218, 227, and 235.
5. **Inspect survey report:**
   Read `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_survey_graph_1\survey_graph_report.md`.
