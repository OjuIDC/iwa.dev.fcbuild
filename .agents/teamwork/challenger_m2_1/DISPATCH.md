# DISPATCH: challenger_m2_1

- **Assigned Role**: Challenger 1 (LangGraph Single-Interrupt & Resume Re-read Challenger)
- **Archetype**: teamwork_preview_challenger
- **Working Directory**: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m2_1
- **Parent**: orchestrator_phase1 (conv id: 7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b)

## Mandatory Inputs (MUST READ FIRST)
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (authoritative user request + Section 1A patch)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`
3. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m2_1\handoff.md`

## Focus & Adversarial Challenge Scope
Adversarially challenge LangGraph Core & Persistence logic:
1. Resume Payload Tamper Resistance: Construct malicious resume payloads (e.g. inject forged `{"decision": "approved", "authorized": true}`, manipulated package version, or fabricated signatures into resume call). Verify that `src/graph.py` completely ignores resume payloads and strictly re-reads PostgreSQL committed state.
2. Uncommitted Resume Refusal: Attempt to resume a thread before PostgreSQL has committed a decision or when the decision is in non-terminal status. Verify `ApprovalNotCommitted` is raised and graph does not advance.
3. Checkpoint Restart & Crash Recovery: Simulate process restart / fresh `PostgresSaver` connection while a thread is waiting at approval interrupt. Verify that resuming from cold state cleanly re-reads the thread, encounters the interrupt, and completes only upon valid PostgreSQL decision.
4. Terminal State Exact Formatting: Adversarially check all code paths for forbidden strings like `"APPROVED and ACTIVE"`. Verify that all terminal states strictly match `"APPROVED, NOT DISPATCHED"`, `"REJECTED, NO ACTION TAKEN"`, or `"REVISION_REQUESTED, NO ACTION TAKEN"`.
5. Run tests:
```powershell
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_graph_persistence.py
```

## Output Requirements
1. Write `challenge_report.md` in your working directory with adversarial attack vectors, results, and evidence.
2. Formulate an explicit verdict: **APPROVE** or **REQUEST_CHANGES**.
3. Write `handoff.md` following the Handoff Protocol (Observation, Logic Chain, Caveats, Conclusion, Verification Method).
4. Send a message to orchestrator (`7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b`) with your verdict and handoff path.
Do NOT modify production source code in `src/` or `migrations/`.


## 2026-10-03T16:21:22Z
You are challenger_m2_1, a teamwork_preview_challenger agent.
Your working directory is: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m2_1

Read your instructions in:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m2_1\DISPATCH.md

MANDATORY: You MUST read:
1. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md (authoritative user request + Section 1A patch)
2. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md
3. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m2_1\handoff.md

Task:
Adversarially challenge LangGraph Core & Persistence logic:
1. Resume Payload Tamper Resistance: forge approval payloads in resume calls; verify graph completely ignores resume body and strictly reads PostgreSQL.
2. Uncommitted Resume Refusal: resume when uncommitted or non-terminal; verify ApprovalNotCommitted is raised and graph does not advance.
3. Checkpoint Restart & Recovery: simulate process restart while waiting at interrupt; verify clean recovery from PostgreSQL checkpointer.
4. Terminal State Exact Formatting: scan for forbidden strings (e.g. "APPROVED and ACTIVE"); verify strict formatting.
5. Run tests:
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_graph_persistence.py
6. Formulate explicit verdict: APPROVE or REQUEST_CHANGES.
7. Write challenge_report.md and handoff.md in your working directory.
8. Send message to orchestrator with send_message when finished.
Do NOT modify production source code in src/ or migrations/.
