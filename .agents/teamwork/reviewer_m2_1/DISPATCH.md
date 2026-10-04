# DISPATCH: reviewer_m2_1

- **Assigned Role**: Reviewer 1 (Graph Core & Persistence)
- **Archetype**: teamwork_preview_reviewer
- **Working Directory**: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m2_1
- **Parent**: orchestrator_phase1 (conv id: 7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b)

## Mandatory Inputs (MUST READ FIRST)
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (authoritative user request + Section 1A patch)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`
3. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m2_1\handoff.md`

## Focus & Review Scope
Perform independent review of Milestone 2 with focus on LangGraph Core & Persistence:
1. Single Interrupt Verification: Verify exactly one `interrupt()` call inside the `approval` node in `src/graph.py`. Ensure there is NO `interrupt_before=["approval"]` anywhere.
2. PostgreSQL Authoritative Decision Re-read on Resume: Verify that resuming the graph calls `deps.decision_reader(thread_id)` directly from PostgreSQL. Verify that resume payloads from callers/HTTP bodies are never trusted for authorization. Verify that `ApprovalNotCommitted` is raised if the decision is missing or package version mismatches.
3. Terminal State Statements: Verify terminal graph state statements are formatted strictly as `"APPROVED, NOT DISPATCHED"`, `"REJECTED, NO ACTION TAKEN"`, and `"REVISION_REQUESTED, NO ACTION TAKEN"`. Check for zero occurrences of `"APPROVED and ACTIVE"`.
4. LangGraph Checkpoint persistence role: Verify checkpoints represent workflow state, not authorization state.
5. Inspect `tests/test_graph_persistence.py` and `tests/test_langgraph_flow.py`.

## Verification Commands
Run the tests:
```powershell
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_graph_persistence.py tests/test_langgraph_flow.py
```
And verify baseline tests:
```powershell
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_approval_lifecycle.py tests/test_approval_security.py
```

## Output Requirements
1. Write `review.md` in your working directory with detailed findings, evidence citations, and rationale.
2. Formulate an explicit verdict: **APPROVE** or **REQUEST_CHANGES**.
3. Write `handoff.md` following the Handoff Protocol (Observation, Logic Chain, Caveats, Conclusion, Verification Method).
4. Send a message to orchestrator (`7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b`) with your verdict and handoff path.
Do NOT modify production source code in `src/` or `migrations/`.


## 2026-10-03T16:21:22Z
You are reviewer_m2_1, a teamwork_preview_reviewer agent.
Your working directory is: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m2_1
Task: Perform independent review of Milestone 2 focusing on Graph Core & Persistence.
