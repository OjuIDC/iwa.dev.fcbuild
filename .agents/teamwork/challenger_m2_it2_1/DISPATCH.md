# DISPATCH: challenger_m2_it2_1

- **Assigned Role**: Challenger 1 (LangGraph State & Mismatched Request ID Challenger)
- **Archetype**: teamwork_preview_challenger
- **Working Directory**: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m2_it2_1
- **Parent**: orchestrator_phase1 (conv id: 7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b)

## Mandatory Inputs (MUST READ FIRST)
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (authoritative user request + Section 1A patch)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`
3. Gate Failure: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m2_1\challenge_report.md`
4. Worker Handoff: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m2_2\handoff.md`

## Adversarial Challenge Scope
Adversarially challenge the resolved defect:
1. Re-run Attack 2.5 (`test_adversarial_resume_refusal_mismatched_approval_request_id`) with NO `xfail` marker. Verify that `ApprovalNotCommitted("committed decision refers to a different approval request")` is genuinely raised.
2. Verify that legitimate runs with matching `approval_request_id` resume cleanly without error.
3. Test edge cases: mutated approval request ID in checkpoint vs committed database row; null request ID injection; invalid UUID formatting.
4. Verify interrupt payload contains valid non-null `approval_request_id` and checkpoint recovery preserves it across cold restarts.
5. Run tests:
```powershell
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_graph_persistence.py
```

## Output Requirements
1. Write `challenge_report.md` and `handoff.md` in your working directory.
2. Formulate explicit verdict: **APPROVE** or **REQUEST_CHANGES**.
3. Send message to orchestrator via `send_message`.
Do NOT modify production source code in `src/` or `migrations/`.

## 2026-10-03T16:57:02Z
You are challenger_m2_it2_1, a teamwork_preview_challenger agent.
Your working directory is: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m2_it2_1

Read your instructions in:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m2_it2_1\DISPATCH.md

MANDATORY: You MUST read:
1. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md (authoritative user request + Section 1A patch)
2. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md
3. Gate Failure: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m2_1\challenge_report.md
4. Worker Handoff: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m2_2\handoff.md

Task:
Adversarially challenge the resolved defect in src/graph.py and tests/test_graph_persistence.py:
1. Run test_adversarial_resume_refusal_mismatched_approval_request_id with NO xfail marker; verify that ApprovalNotCommitted is genuinely raised on mismatched approval_request_id.
2. Verify that legitimate runs with matching approval_request_id resume cleanly.
3. Verify interrupt payload contains valid non-null approval_request_id and checkpoint recovery preserves it across cold restarts.
4. Run tests:
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_graph_persistence.py
5. Formulate explicit verdict: APPROVE or REQUEST_CHANGES.
6. Write challenge_report.md and handoff.md in your working directory.
7. Send message to orchestrator via send_message.
Do NOT modify production source code in src/ or migrations/.
