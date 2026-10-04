# DISPATCH: challenger_m2_it2_2

- **Assigned Role**: Challenger 2 (API & Store Integration Challenger)
- **Archetype**: teamwork_preview_challenger
- **Working Directory**: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m2_it2_2
- **Parent**: orchestrator_phase1 (conv id: 7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b)

## Mandatory Inputs (MUST READ FIRST)
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (authoritative user request + Section 1A patch)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`
3. Gate Failure: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m2_1\challenge_report.md`
4. Worker Handoff: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m2_2\handoff.md`

## Adversarial Challenge Scope
Adversarially challenge the API and Store integration:
1. Probe `/v1/runs` route: create run, verify `approval_request_id` in response projection matches `graph_threads.approval_request_id` and `approval_requests.approval_request_id`.
2. Probe `POST /v1/approval-requests/{approval_request_id}/decisions`: verify decision submission resolves the correct thread and applies cleanly.
3. Test invalid UUID injection into `complete_run` or API endpoints: verify handled gracefully without unhandled database exceptions.
4. Verify trigger `fc27_check_approval_pair()` integrity under concurrent load or unusual payloads.
5. Run tests:
```powershell
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_graph_api_v1.py tests/test_approval_lifecycle.py
```

## Output Requirements
1. Write `challenge_report.md` and `handoff.md` in your working directory.
2. Formulate explicit verdict: **APPROVE** or **REQUEST_CHANGES**.
3. Send message to orchestrator via `send_message`.
Do NOT modify production source code in `src/` or `migrations/`.

## 2026-10-03T16:57:02Z
You are challenger_m2_it2_2, a teamwork_preview_challenger agent.
Your working directory is: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m2_it2_2
MANDATORY: You MUST read:
1. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
2. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md
3. Gate Failure: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m2_1\challenge_report.md
4. Worker Handoff: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m2_2\handoff.md
