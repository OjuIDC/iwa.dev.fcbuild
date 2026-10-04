# Dispatch: Milestone 2 Graph API & Security Routes Explorer

## Identity
- Archetype: teamwork_preview_explorer
- Working Directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_api_1

## Mandatory Inputs
You MUST read:
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (including Section 1A patch).
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`.
3. `src/graph_api.py`, `src/correlation.py`.
4. `docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md` (Task 6).

## Task
Perform deep exploration and detailed design for `src/graph_api.py` and `src/correlation.py`:
1. Versioned `/v1/` Routes:
   - Design explicit `/v1/` prefix for all operational routes:
     - `POST /v1/runs`
     - `GET /v1/runs/{thread_id}`
     - `GET /v1/runs/{thread_id}/approval-request`
     - `POST /v1/runs/{thread_id}/resume`
     - `POST /v1/runs/{thread_id}/decision-challenge`
     - `POST /v1/runs/{thread_id}/decision`
     - `POST /v1/runs/{thread_id}/kill`
   - Retain legacy unversioned routes (or redirect/alias them) if required by backward compatibility, or specify transition.
2. Correlation Middleware (`src/correlation.py`):
   - Context variable `correlation_id_var`.
   - Logging filter `CorrelationIdFilter`.
   - FastAPI middleware extracting `X-Correlation-ID` or generating a new UUIDv4, attaching to request state, setting response header, and populating log records.
3. Safe Projections & Sensitive Data Protection:
   - Ensure response payloads do not leak stack traces, internal connection strings, signing secrets, or raw internal errors.
   - Pydantic models for safe run and approval request projections.
4. Health Probes:
   - `GET /health`: Liveness probe (200 OK).
   - `GET /health/ready`: Readiness probe verifying PostgreSQL connection and migration status.
5. HMAC Kill Switch & Rate/Timeout Limits:
   - Authenticated run cancellation on `/v1/runs/{thread_id}/kill`.
6. Enforce Phase 1 Invariants:
   - `DISPATCH_ENABLED=false` at all times.
   - Resume endpoint wakes graph but cannot authorize anything.
7. Write complete analysis to `api_analysis.md` and complete handoff report to `handoff.md` in your working directory.
8. Notify orchestrator via `send_message`.
Do NOT modify production source code in `src/` or `migrations/`.

## 2026-10-03T15:29:54Z
You are explorer_m2_api_1, a teamwork_preview_explorer agent.
Your working directory is: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_api_1

Read your instructions in:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_api_1\DISPATCH.md

MANDATORY: You MUST read:
1. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md (including Section 1A patch)
2. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md
3. src/graph_api.py, src/correlation.py
4. docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md (Task 6)

Task:
Perform deep exploration and detailed design for src/graph_api.py and src/correlation.py:
1. Versioned /v1/ routes (/v1/runs, /v1/runs/{thread_id}, /v1/runs/{thread_id}/approval-request, /v1/runs/{thread_id}/resume, /v1/runs/{thread_id}/decision-challenge, /v1/runs/{thread_id}/decision, /v1/runs/{thread_id}/kill).
2. Correlation middleware in src/correlation.py (correlation_id_var, CorrelationIdFilter, X-Correlation-ID header extraction/generation).
3. Safe projections (no leaking internal stack traces, DB credentials, secrets).
4. Health probes (/health liveness, /health/ready readiness checking DB connectivity).
5. HMAC kill switch on /v1/runs/{thread_id}/kill.
6. Enforce DISPATCH_ENABLED=false.
Write api_analysis.md and handoff.md in your working directory, then notify orchestrator with send_message.
Do NOT modify production source code in src/ or migrations/.
