# DISPATCH: reviewer_m2_2

- **Assigned Role**: Reviewer 2 (Graph API & Security Routes)
- **Archetype**: teamwork_preview_reviewer
- **Working Directory**: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m2_2
- **Parent**: orchestrator_phase1 (conv id: 7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b)

## Mandatory Inputs (MUST READ FIRST)
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (authoritative user request + Section 1A patch)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`
3. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m2_1\handoff.md`

## Focus & Review Scope
Perform independent review of Milestone 2 with focus on Persistent Graph API & Security Routes:
1. Versioned `/v1/` routes in `src/graph_api.py`: Check all routes (`/v1/runs`, `/v1/runs/{id}`, `/v1/runs/{id}/approval-request`, `/v1/runs/{id}/resume`, `/v1/runs/{id}/decision-challenge`, `/v1/runs/{id}/decision`, `/v1/approval-requests/{id}/decisions`, `/v1/runs/{id}/kill`). Check backward-compatible legacy route aliases.
2. Correlation Middleware in `src/correlation.py`: Check UUIDv4 validation, stripping of malformed input on 422 errors, `ContextVar` propagation, `CorrelationIdFilter` log injection, and `X-Correlation-Id` header echo.
3. Safe Projections: Verify projection models in `src/schemas.py` and responses in `src/graph_api.py`. Confirm no leakage of DB credentials, internal stack traces, or secrets.
4. Health Probes: Check `/health/live` and `/health/ready`. Verify `/health/ready` returns 503 if DB is unreachable, role posture is invalid, or if `DISPATCH_ENABLED=true`.
5. HMAC Kill Switch: Verify `/v1/runs/{id}/decision-challenge`, `/v1/runs/{id}/decision`, and `/v1/approval-requests/{id}/decisions` return 403 `{"detail": "proof_type_disabled"}` when `APPROVAL_CLI_HMAC_ENABLED=false` before body validation.
6. Inspect `tests/test_graph_api_v1.py`.

## Verification Commands
Run the tests:
```powershell
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_graph_api_v1.py
```
And verify baseline tests:
```powershell
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_db_roles.py tests/test_events_outbox.py tests/test_approval_lifecycle.py
```

## Output Requirements
1. Write `review.md` in your working directory with detailed findings, evidence citations, and rationale.
2. Formulate an explicit verdict: **APPROVE** or **REQUEST_CHANGES**.
3. Write `handoff.md` following the Handoff Protocol (Observation, Logic Chain, Caveats, Conclusion, Verification Method).
4. Send a message to orchestrator (`7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b`) with your verdict and handoff path.
Do NOT modify production source code in `src/` or `migrations/`.


## 2026-10-03T16:21:22Z
You are reviewer_m2_2, a teamwork_preview_reviewer agent.
Your working directory is: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m2_2
Task: Perform independent review of Milestone 2 focusing on Persistent Graph API & Security Routes.
