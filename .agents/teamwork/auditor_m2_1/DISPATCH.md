# DISPATCH: auditor_m2_1

- **Assigned Role**: Forensic Integrity Auditor
- **Archetype**: teamwork_preview_auditor
- **Working Directory**: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m2_1
- **Parent**: orchestrator_phase1 (conv id: 7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b)

## Mandatory Inputs (MUST READ FIRST)
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (authoritative user request + Section 1A patch)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`
3. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m2_1\handoff.md`

## Focus & Forensic Integrity Audit Scope
Perform an exhaustive forensic integrity audit across all files modified by `worker_m2_1`:
- `src/correlation.py`
- `src/schemas.py`
- `src/graph.py`
- `src/graph_api.py`
- `tests/conftest.py`
- `tests/test_graph_persistence.py`
- `tests/test_graph_api_v1.py`
- `tests/test_langgraph_flow.py`

Verify:
1. Static Analysis & Authenticity:
   - Check that implementations are genuine and not hardcoded facades or mock bypasses.
   - Confirm single interrupt mechanism in `src/graph.py`: verify `interrupt()` is called inside `approval` node and NO `interrupt_before=["approval"]` exists.
   - Confirm authoritative PostgreSQL re-read in `approval` node via `deps.decision_reader(thread_id)` and that resume request payloads are completely ignored for authorization.
   - Confirm terminal state string formatting strictly uses `"APPROVED, NOT DISPATCHED"`, `"REJECTED, NO ACTION TAKEN"`, and `"REVISION_REQUESTED, NO ACTION TAKEN"`. Check for zero occurrences of `"APPROVED and ACTIVE"`.
   - Confirm correlation middleware strictly validates UUID format and strips caller input on 422 errors (D-30).
   - Confirm safe projections strip database credentials, secrets, and raw internal tracebacks.
2. Invariant Forensics:
   - Verify `DISPATCH_ENABLED=false` is strictly maintained and cannot be bypassed.
   - Verify zero player delivery or adapter calls occur.
   - Verify Section 1A constraints: timeout never approves, human decision required, actor type and authority basis validation.
3. Execution Verification:
   Run the full verification suite to verify genuine execution:
   ```powershell
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_langgraph_flow.py tests/test_graph_persistence.py tests/test_graph_api_v1.py tests/test_approval_lifecycle.py tests/test_approval_security.py
   ```

## Output Requirements
1. Formulate an explicit binary verdict: **CLEAN** or **INTEGRITY VIOLATION**. (Note: Auditor has non-negotiable binary veto power).
2. Write `audit_report.md` in your working directory with full forensic evidence.
3. Write `handoff.md` following the Handoff Protocol.
4. Send a message to orchestrator (`7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b`) with your verdict and handoff path.
Do NOT modify production source code in `src/` or `migrations/`.
