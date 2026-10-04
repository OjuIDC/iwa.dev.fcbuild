# Dispatch Instructions for reviewer_m2_1

**Agent ID**: `reviewer_m2_1`
**Role**: Policy & Service Boundaries Reviewer
**Milestone**: Phase 2 Milestone 2 — Centralized Authorization & Service Boundaries
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m2_1`

## Authoritative Inputs
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md`
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
3. Worker handoff: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m2_1\handoff.md`

## Review Tasks
1. Review `src/identity/policy.py`:
   - Complete implementation of `authorize(principal, action, resource, authority_basis, delegation)`
   - All 8 application roles accurately modeled
   - All 4 authority bases modeled
   - Strict club tenancy (`CrossClubAccessError`) and object-level player isolation (`PlayerIsolationError`)
   - Anti-impersonation guard (`AgentImpersonationError` on human decision actions by service)
   - Step-up passkey verification requirement
   - G-TIMEOUT invariant: timeouts/inactivity never authorize
2. Review `src/identity/services.py` & `src/identity/deps.py`:
   - `ServiceCredentialsManager` loading distinct keys with rotation support
   - Constant-time token verification (`hmac.compare_digest`)
   - Gamertag display separation (`DisplayIdentity`, immutable UUID keys)
   - FastAPI `get_current_principal` dependency
3. Execute verification tests:
   - `pytest tests/test_packaging.py tests/test_phase1_scope.py -v`
   - `pytest tests/identity/test_policy.py tests/identity/test_services.py -v`
4. Document commands, findings, and explicit verdict (**APPROVE** or **REQUEST_CHANGES**) in:
   `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m2_1\handoff.md`


## 2026-10-04T04:21:22Z
[Message] timestamp=2026-10-04T04:21:22Z sender=25210876-a9ad-4f78-a87c-dec88e86edb9 priority=MESSAGE_PRIORITY_HIGH content=You are reviewer_m2_1, a high-reliability reviewer agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m2_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m2_1\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md
Worker handoff: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m2_1\handoff.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md before starting work.

Your task:
Review the implementation of Phase 2 Milestone 2: Centralized Authorization & Service Boundaries.
1. Review src/identity/policy.py (authorize(), 8 roles, 4 authority bases, tenancy, player isolation, anti-impersonation, G-TIMEOUT).
2. Review src/identity/services.py & src/identity/deps.py (ServiceCredentialsManager, token validation, gamertag separation, get_current_principal).
3. Execute verification tests:
   - pytest tests/test_packaging.py tests/test_phase1_scope.py -v
   - pytest tests/identity/test_policy.py tests/identity/test_services.py -v
4. Document commands, findings, and explicit verdict (APPROVE or REQUEST_CHANGES) in:
   c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m2_1\handoff.md
When finished, notify me with your verdict and report path via send_message.
