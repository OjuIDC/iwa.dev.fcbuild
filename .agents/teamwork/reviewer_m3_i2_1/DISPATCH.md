# Dispatch Instructions for reviewer_m3_i2_1

**Agent ID**: `reviewer_m3_i2_1`
**Role**: API Routers & FastAPI Integration Reviewer (Iteration 2)
**Milestone**: Phase 2 Milestone 3 — Server-Side Sessions & Invitations Lifecycle (Iteration 2 Verification)
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_i2_1`

## Inputs
- Authoritative Request: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (MANDATORY TO READ)
- Project Plan: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
- Previous Iteration Audit Report: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m3_1\handoff.md`
- Worker Handoff: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_2\handoff.md`

## Review Tasks
1. Inspect API router implementations:
   - `src/identity/routers/sessions.py`: verify `/rotate` rejects arbitrary client elevation to `step_up_passkey` with 403. Verify all human session endpoints enforce `Depends(require_human_principal)`.
   - `src/identity/routers/invitations.py`: verify token issuance (64 hex characters), hash storage, public validation, and single-use consumption.
   - `src/identity/routers/users.py`: verify user listing and status query endpoints.
   - `src/graph_api.py`: verify router inclusion and exception handlers.
2. Verify policy enforcement:
   - Verify `SESSION_REVOKE` and `SESSION_VIEW` cannot be called by unauthorized non-commissioner/non-delegate users against other users' sessions (HTTP 403).
3. Execute verification tests:
   - `pytest tests/test_packaging.py tests/test_phase1_scope.py -v`
   - `pytest tests/identity/test_routers_invitations.py tests/identity/test_routers_sessions.py tests/identity/test_routers_users.py -v`
   - `pytest tests/e2e/test_phase2_tier1_features.py -k "invitation or session" -v`
4. Provide explicit verdict: APPROVE or REQUEST_CHANGES.
5. Deliver complete handoff report to:
   `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_i2_1\handoff.md`.
   Notify orchestrator via `send_message`.


## 2026-10-04T05:35:56Z
You are reviewer_m3_i2_1, a high-reliability reviewer agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_i2_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_i2_1\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md
Worker handoff: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_2\handoff.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md and c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_i2_1\DISPATCH.md before starting work.

Your task:
Review the API Routers and FastAPI Integration for Phase 2 Milestone 3 Iteration 2:
1. Inspect src/identity/routers/sessions.py, src/identity/routers/invitations.py, src/identity/routers/users.py, src/identity/routers/__init__.py, and src/graph_api.py.
2. Verify route definitions, request/response models, input validations, error mappings, and anti-impersonation (Depends(require_human_principal)).
3. Verify /rotate rejects unverified privilege escalation to step_up_passkey with 403 Forbidden.
4. Execute verification tests:
   - pytest tests/test_packaging.py tests/test_phase1_scope.py -v
   - pytest tests/identity/test_routers_invitations.py tests/identity/test_routers_sessions.py tests/identity/test_routers_users.py -v
   - pytest tests/e2e/test_phase2_tier1_features.py -k "invitation or session" -v
5. Document all commands, test outputs, findings, and explicit verdict (APPROVE or REQUEST_CHANGES) in:
   c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_i2_1\handoff.md
When finished, notify me with your verdict and report path via send_message.
