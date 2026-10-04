# Dispatch Instructions for reviewer_m3_1

**Agent ID**: `reviewer_m3_1`
**Role**: API Routers & FastAPI Integration Reviewer
**Milestone**: Phase 2 Milestone 3 — Server-Side Sessions & Invitations Lifecycle
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_1`

## Authoritative Inputs
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (Read §5, §6, §7, §8, §11, §14)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
3. Worker handoff: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_1\handoff.md`

## Mandatory Non-Negotiable Invariants
- `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` strictly enforced at all times.
- Zero player delivery; no active worker or relay.
- Plaintext tokens (sessions and invitations) are NEVER stored in PostgreSQL; only deterministic SHA-256 hashes (`hash_token()`).
- Single-use invitation consumption enforced under atomic `FOR UPDATE` lock.
- User account status transition strictly respects `fc27_users_status_guard`: `invited` -> `active`.
- Dual session timeouts: 30-minute idle expiration strictly capped at 24-hour absolute expiration.

## Review Scope
1. **API Routers**:
   - Inspect `src/identity/routers/invitations.py`, `src/identity/routers/sessions.py`, `src/identity/routers/users.py`, and `src/identity/routers/__init__.py`.
   - Verify request and response models, input validation (Pydantic models, forbidden extra fields, clean error serialization), and route decorators.
   - Verify route aliases matching E2E test suite expectations (`/v1/auth/invitations/{token}/accept` and `/consume`, `/v1/auth/session` and `/v1/auth/me`).
2. **FastAPI Integration**:
   - Inspect `src/graph_api.py`: verify router mounting, attachment of `app.state.identity_store`, and custom exception handlers for `IdentityStoreError` and `AuthorizationError`.
3. **Execution & Test Verification**:
   - Execute verification tests:
     - `pytest tests/test_packaging.py tests/test_phase1_scope.py -v`
     - `pytest tests/identity/test_routers_invitations.py tests/identity/test_routers_sessions.py tests/identity/test_routers_users.py -v`
     - `pytest tests/e2e/test_phase2_tier1_features.py -k "invitation or session" -v`
   - Document all executed commands, test outputs, and findings in `handoff.md`.
4. **Verdict**:
   - Provide an explicit verdict: `APPROVE` or `REQUEST_CHANGES`.

Write your report to: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_1\handoff.md`.
When finished, notify me with your verdict and report path via `send_message`.

## 2026-10-04T05:00:05Z
You are reviewer_m3_1, a high-reliability reviewer agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_1\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md
Worker handoff: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_1\handoff.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md before starting work.

Your task:
Review the API Routers and FastAPI Integration for Phase 2 Milestone 3: Server-Side Sessions & Invitations Lifecycle.
1. Inspect src/identity/routers/invitations.py, src/identity/routers/sessions.py, src/identity/routers/users.py, src/identity/routers/__init__.py, and src/graph_api.py.
2. Verify route definitions, request/response models, input validations, and error mappings.
3. Verify dependency wiring: get_current_principal, require_human_principal, and get_identity_store.
4. Execute verification tests:
   - pytest tests/test_packaging.py tests/test_phase1_scope.py -v
   - pytest tests/identity/test_routers_invitations.py tests/identity/test_routers_sessions.py tests/identity/test_routers_users.py -v
   - pytest tests/e2e/test_phase2_tier1_features.py -k "invitation or session" -v
5. Document all commands, test outputs, and findings in handoff.md.
6. Provide an explicit verdict: APPROVE or REQUEST_CHANGES.
Write report to: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_1\handoff.md
When finished, notify me with your verdict and report path via send_message.
