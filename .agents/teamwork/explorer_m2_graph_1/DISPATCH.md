# Dispatch: Milestone 2 Graph Core Explorer

## Identity
- Archetype: teamwork_preview_explorer
- Working Directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_graph_1

## Mandatory Inputs
You MUST read:
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (including Section 1A patch).
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`.
3. `src/graph.py`.
4. `docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md` (Task 5).

## Task
Perform deep exploration and detailed design for `src/graph.py` (Graph Core & Persistence):
1. Single Interrupt Mechanism:
   - Verify where `interrupt()` is called in the approval node.
   - Verify there is NO double interruption (e.g. ensure `interrupt_before=["approval"]` is removed or not configured).
   - Verify the interrupt payload matches the approvable package / approval request data.
2. PostgreSQL Authoritative Decision Re-read on Resume:
   - When the graph resumes after interrupt, verify how the decision is obtained.
   - Ensure the resume request payload is NEVER trusted for authorization.
   - Implement `deps.decision_reader(thread_id)` that re-reads committed decision from PostgreSQL (`approval_audit` / `graph_threads`).
   - Validate that committed decision matches `package_version` and `approval_request_id`.
   - Raise `ApprovalNotCommitted` if no valid decision is committed in PostgreSQL.
3. Terminal State Formatting:
   - Verify terminal state strings: must be formatted as `"APPROVED, NOT DISPATCHED"` or `"REJECTED, NO ACTION TAKEN"`.
   - Ensure no obsolete or forward strings like `"APPROVED and ACTIVE"` exist.
4. Checkpoint State vs Authorization State:
   - Clarify role of LangGraph PostgresSaver checkpointer: workflow continuation state only, never authorization authority.
5. Write complete analysis to `graph_analysis.md` and complete handoff report to `handoff.md` in your working directory.
6. Notify orchestrator via `send_message`.
Do NOT modify production source code in `src/` or `migrations/`.


## 2026-10-03T15:29:54Z
Sender: 7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b
Content:
You are explorer_m2_graph_1, a teamwork_preview_explorer agent.
Your working directory is: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_graph_1

Read your instructions in:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_graph_1\DISPATCH.md

MANDATORY: You MUST read:
1. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md (including Section 1A patch)
2. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md
3. src/graph.py
4. docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md (Task 5)

Task:
Perform deep exploration and detailed design for src/graph.py (Graph Core & Persistence):
1. Single Interrupt Mechanism: verify exactly one interrupt() call in approval node; no interrupt_before=["approval"].
2. PostgreSQL Authoritative Decision Re-read on Resume: deps.decision_reader(thread_id), resume payload never trusted for authorization, validate decision against package_version/approval_request_id, raise ApprovalNotCommitted if uncommitted/mismatched.
3. Terminal State Formatting: "APPROVED, NOT DISPATCHED" and "REJECTED, NO ACTION TAKEN" (never "APPROVED and ACTIVE").
4. LangGraph Checkpoint persistence role.
Write graph_analysis.md and handoff.md in your working directory, then notify orchestrator with send_message.
Do NOT modify production source code in src/ or migrations/.
