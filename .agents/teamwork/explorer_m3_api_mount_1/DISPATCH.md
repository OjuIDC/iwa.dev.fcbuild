# Dispatch Instructions for explorer_m3_api_mount_1

**Agent ID**: `explorer_m3_api_mount_1`
**Role**: API Integration & Routing Designer
**Milestone**: Phase 2 Milestone 3 — Server-Side Sessions & Invitations Lifecycle
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_api_mount_1`

## Authoritative Inputs
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (Read §5, §7, §8, §11, §14)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
3. Existing API & tests: `src/graph_api.py`, `tests/e2e/test_phase2_tier1_features.py`, `tests/e2e/phase2_helpers.py`, `src/identity/deps.py`

## Mission & Objectives
Investigate and design the integration of Identity routers into FastAPI and existing endpoints:
1. Router Layout & Mount:
   - Create router structure under `src/identity/routers/`:
     - `src/identity/routers/__init__.py`
     - `src/identity/routers/sessions.py`
     - `src/identity/routers/invitations.py`
   - Mount routers in `src/graph_api.py`:
     - `app.include_router(invitations_router, prefix="/v1/auth/invitations", tags=["invitations"])`
     - `app.include_router(sessions_router, prefix="/v1/auth/sessions", tags=["sessions"])`
     - `GET /v1/auth/me` endpoint.
2. Compatibility with Dual-Track E2E Test Suite:
   - Inspect `tests/e2e/test_phase2_tier1_features.py` and `tests/e2e/phase2_helpers.py` for expected URL paths, request/response JSON schemas, and status codes for invitations and sessions.
   - Ensure routes match the exact paths expected by Tier 1–4 E2E tests.
3. Dependency Injection & Error Mapping:
   - Wire `get_current_principal` and `require_permission` from `src/identity/deps.py`.
   - Map domain errors (`IdentityStoreError`, `AuthorizationError`, `CrossClubAccessError`, `SessionExpiredError`) to standard FastAPI JSON responses with proper HTTP status codes.
4. Deliver complete mounting plan, helper blueprints, and test design.

Write report to: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_api_mount_1\handoff.md`.


## 2026-10-04T04:30:19Z

From: 25210876-a9ad-4f78-a87c-dec88e86edb9 (parent)
Priority: MESSAGE_PRIORITY_HIGH

You are explorer_m3_api_mount_1, a Read-only exploration agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_api_mount_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_api_mount_1\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md before starting work.

Your task:
Investigate and design the integration of Identity routers into FastAPI (src/graph_api.py) and existing endpoints for Phase 2 Milestone 3:
1. Inspect src/graph_api.py, src/identity/deps.py, tests/e2e/test_phase2_tier1_features.py, and tests/e2e/phase2_helpers.py.
2. Design router package layout under src/identity/routers/:
   - src/identity/routers/__init__.py
   - src/identity/routers/sessions.py
   - src/identity/routers/invitations.py
3. Design mounting in src/graph_api.py:
   - app.include_router(invitations_router, prefix="/v1/auth/invitations", tags=["invitations"])
   - app.include_router(sessions_router, prefix="/v1/auth/sessions", tags=["sessions"])
   - GET /v1/auth/me endpoint (or inside sessions router mounted at root/v1/auth).
4. Verify alignment with Tier 1-4 E2E tests:
   - Check exact URL paths, headers (e.g., Authorization: Bearer <session_token> or X-Session-Token), request bodies, response models, status codes.
5. Error handling and dependency injection:
   - Wire get_current_principal, require_permission from src/identity/deps.py.
   - Exception handlers for IdentityStoreError, AuthorizationError, etc.
6. Provide exact code structure, router integration diff, and test design.
7. Deliver complete handoff report to:
   c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_api_mount_1\handoff.md
When finished, notify me with a summary and the path to your handoff report via send_message.
