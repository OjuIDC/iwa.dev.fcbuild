# DISPATCH: challenger_m2_2

- **Assigned Role**: Challenger 2 (API Correlation, Projections & Security Routes Challenger)
- **Archetype**: teamwork_preview_challenger
- **Working Directory**: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m2_2
- **Parent**: orchestrator_phase1 (conv id: 7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b)

## Mandatory Inputs (MUST READ FIRST)
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (authoritative user request + Section 1A patch)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`
3. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m2_1\handoff.md`

## Focus & Adversarial Challenge Scope
Adversarially challenge Persistent Graph API & Security routes:
1. Malformed Correlation ID Attacks: Send invalid correlation IDs (SQL injection strings, path traversal, oversized strings, JSON payloads, truncated UUIDs, non-UUID hex). Verify HTTP 422 `{"detail": "invalid correlation id"}` is returned, AND verify that user input is completely redacted/stripped from response.
2. Information Leakage & Secret Exposure Attacks: Probe all `/v1/runs` and `/v1/approval-requests` routes. Check whether any internal database connection strings, passwords, HMAC keys, or full tracebacks leak in response bodies under normal or error conditions.
3. HMAC Kill Switch Bypass Attacks: When `APPROVAL_CLI_HMAC_ENABLED=false`, attempt to invoke decision routes with forged headers, unexpected HTTP methods, or invalid bodies. Verify that requests are blocked with HTTP 403 `{"detail": "proof_type_disabled"}` BEFORE any body parsing or validation occurs.
4. Readiness Probe Fails-Closed Attack: Simulate degraded conditions: invalid DB connection, `DISPATCH_ENABLED=true`, or inconsistent view rows. Verify `/health/ready` immediately fails closed with HTTP 503.
5. Run tests:
```powershell
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_graph_api_v1.py
```

## Output Requirements
1. Write `challenge_report.md` in your working directory with adversarial attack vectors, results, and evidence.
2. Formulate an explicit verdict: **APPROVE** or **REQUEST_CHANGES**.
3. Write `handoff.md` following the Handoff Protocol (Observation, Logic Chain, Caveats, Conclusion, Verification Method).
4. Send a message to orchestrator (`7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b`) with your verdict and handoff path.
Do NOT modify production source code in `src/` or `migrations/`.

## 2026-10-03T16:21:22Z
Received dispatch from parent 7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b.
Task: Adversarially challenge Persistent Graph API & Security routes.
Verdict required: APPROVE or REQUEST_CHANGES.
Do NOT modify production source code in src/ or migrations/.
