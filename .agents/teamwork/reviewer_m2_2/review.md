# Milestone 2 Review Report: Persistent Graph API & Security Routes

**Reviewer**: reviewer_m2_2 (`teamwork_preview_reviewer`)  
**Roles**: Reviewer, Adversarial Critic  
**Scope**: Milestone 2 Persistent Graph API, Security Routes, Correlation Middleware, Safe Projections, Health Probes, HMAC Kill Switch  
**Date**: 2026-10-03  

---

## 1. Executive Summary & Verdict

**Verdict**: **APPROVE**  
**Integrity Assessment**: **CLEAN** (No integrity violations, no dummy facades, no hardcoded test outputs, no bypassed tasks).  
**Test Suite Status**: 100% PASS across both Milestone 2 tests (`tests/test_graph_api_v1.py`: 18/18 passed) and baseline lifecycle/infrastructure suites (`tests/test_db_roles.py`, `tests/test_events_outbox.py`, `tests/test_approval_lifecycle.py`: 33/33 passed).

The implementation in `src/graph_api.py`, `src/correlation.py`, and `src/schemas.py` thoroughly satisfies all non-negotiable Phase 1 invariants, specification requirements, and security route contracts.

---

## 2. Review Findings by Focus Area

### 2.1 Versioned `/v1/` Routes & Legacy Aliases
- **Routes Audited**:
  - `POST /v1/runs` (`RunRequest` with `extra="forbid"`, numeric club ID validation, durable run creation, returns `RunProjection` with `dispatch_enabled=false`).
  - `GET /v1/runs/{id}` (`RunProjection` omitting secrets, checkpointer blobs, and internal credentials).
  - `GET /v1/runs/{id}/approval-request` (`ApprovalRequestProjection` with candidate summary, version/hash binding, expiry status).
  - `POST /v1/runs/{id}/resume` (`ResumeRequest` with `extra="forbid"`, fails closed with 409 if uncommitted in PostgreSQL, carries zero authorization).
  - `POST /v1/runs/{id}/decision-challenge` (guarded by `require_hmac` and commissioner role).
  - `POST /v1/runs/{id}/decision` (guarded by `require_hmac` and commissioner role, commits DB before resuming Graph, returns `DispatchBlock(enabled=False)`).
  - `POST /v1/approval-requests/{id}/decisions` (companion request-centric decision route, maps request ID to thread ID and applies decision).
  - `POST /v1/runs/{id}/kill` (authenticated kill switch locking nonces and thread with `FOR UPDATE`, marking status `'expired'`, revoking open nonces, recording auditable domain event).
  - Legacy Aliases: `/run`, `/threads/{id}`, `/threads/{id}/decision-challenge`, `/threads/{id}/decision` seamlessly mapped to internal handlers.
- **Assessment**: Correct, clean separation of concern, proper role enforcement (`Role.service` vs `Role.commissioner`).

### 2.2 Correlation Middleware (`src/correlation.py`)
- **Audit Findings**:
  - `CorrelationMiddleware` intercepts `X-Correlation-Id` header.
  - Length check: strictly enforces 36 characters.
  - Canonical format check: `cid = UUID(stripped)` and `str(cid).lower() == stripped.lower()` ensures standard 8-4-4-4-12 representation without braces or prefixes.
  - Input stripping on 422: malformed headers immediately return 422 `{"detail": "invalid correlation id"}` without reflecting the invalid caller input.
  - Generation when absent: generates `uuid4()` if header is missing.
  - Header echo: echoes `X-Correlation-Id` on all HTTP responses.
  - ContextVar propagation: sets `correlation_id_var` and ensures cleanup in `finally: correlation_id_var.reset(token)`.
  - Log integration: `CorrelationIdFilter` attaches `correlation_id` to log records.
- **Assessment**: Meets Spec §22.3 and Amendment C-5 completely.

### 2.3 Safe Projections (`src/schemas.py`)
- **Audit Findings**:
  - `RunProjection` carefully defines public fields. Database connection strings, passwords, signing secrets, and LangGraph raw checkpoint blobs are omitted.
  - `ApprovalRequestProjection` projects display-safe candidate summary (`candidate_id`, `player_id`, `tier`, `score`) and package version/hash.
  - `DispatchBlock` explicitly communicates `enabled=false`, `created=false`, and message: *"Dispatch is disabled. This approval records the selection only; nothing was delivered to any player."*
  - Negative security tests confirm `"password"`, `"postgresql://"`, `"secret"`, and `"checkpoint_blobs"` never appear in API responses.
  - Pydantic validation error handler (`validation_exception_handler`) strips `"input"` from 422 error details, preventing reflection of probe payloads or credentials.
- **Assessment**: Fully secure against credential leakage and input reflection.

### 2.4 Health Probes (`/health/live`, `/health/ready`, `/health`)
- **Audit Findings**:
  - `/health/live`: Lightweight process liveness only (returns 200 `{"status": "ok", "process": "live"}` without hitting DB).
  - `/health/ready`: Deep readiness probe:
    1. Tests PostgreSQL connectivity (3-second timeout).
    2. Enforces database role posture (`check_runtime_posture`).
    3. Verifies existence of checkpointer tables (`public.checkpoints`).
    4. Evaluates `public.fc27_approval_inconsistencies` view.
    5. Checks `DISPATCH_ENABLED`: returns 503 if `settings.dispatch_enabled == True`.
  - `/health`: Legacy health probe reporting status, database health, and `dispatch: "disabled"`.
- **Assessment**: Robust fail-closed readiness probe adhering to Phase 1 invariants.

### 2.5 HMAC Kill Switch
- **Audit Findings**:
  - `settings.cli_hmac_enabled` (controlled by `APPROVAL_CLI_HMAC_ENABLED`) controls CLI HMAC proof verification.
  - When `APPROVAL_CLI_HMAC_ENABLED=false`, `dependencies=[Depends(require_hmac)]` on `/v1/runs/{id}/decision-challenge`, `/v1/runs/{id}/decision`, and `/v1/approval-requests/{id}/decisions` immediately returns 403 `{"detail": "proof_type_disabled"}` prior to request body validation.
  - Tested and confirmed via `test_hmac_kill_switch_behavior` where invalid JSON payload receives 403 instead of 422.
- **Assessment**: Fully functional kill switch executing before body parsing.

---

## 3. Adversarial Challenges & Stress Testing

| Challenge | Attack Scenario | Evaluated Defense | Result |
|---|---|---|---|
| **Resume Injection** | Attacker calls `POST /v1/runs/{id}/resume` with `{"approved": true, "decision": "APPROVE"}` | `ResumeRequest` enforces `extra="forbid"`, causing immediate 422 rejection. Even with empty body, returns 409 if DB decision is uncommitted. Graph re-reads Postgres. | **PASS (Immune)** |
| **Header Injection / CRLF** | Attacker supplies malformed or multi-line `X-Correlation-Id` | `len(stripped) != 36` and `UUID(stripped)` with canonical check reject anything non-conforming with 422 without input reflection. | **PASS (Immune)** |
| **Race Condition: Kill vs Decision** | Concurrent requests to `/v1/runs/{id}/kill` and `/v1/runs/{id}/decision` | Both endpoints serialize via `SELECT ... FOR UPDATE` on `approval_nonces` and `graph_threads`. If kill wins, nonces revoked and thread expired. If decision wins, kill returns 409 terminal status. | **PASS (Safe)** |
| **Secret Exfiltration via Projections** | Querying run projections to read database connection details or checkpoint blobs | Response schemas use strict Pydantic projection models with explicit whitelist of display-safe fields. | **PASS (Safe)** |
| **Readiness Bypass under Misconfiguration** | Operator sets `DISPATCH_ENABLED=true` | `/health/ready` returns 503 `Phase 1 safety invariant violation: DISPATCH_ENABLED must be false`. | **PASS (Fails Closed)** |

---

## 4. Observations & Notes

- **Observation 1 (Canonical UUID Parsing)**: `CorrelationMiddleware` validates canonical 36-character UUID representation (`xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`), supporting standard RFC 4122 UUID formats (v1 through v5), and generates UUIDv4 when absent. This allows compatibility with upstream trace headers while guaranteeing canonical syntax.
- **Observation 2 (Legacy Contract Preservation)**: The legacy route `/threads/{thread_id}/decision` omits the `"dispatch"` key to satisfy legacy AC-44 tests, while the versioned route `/v1/runs/{thread_id}/decision` includes `DispatchBlock(enabled=False, created=False)`.
- **Observation 3 (Error Sanitization)**: `validation_exception_handler` strips `"input"` from Pydantic `RequestValidationError` outputs, adhering to D-30 compliance and preventing credential or probe reflection.

---

## 5. Verdict

**APPROVE**
Milestone 2 implementation of Persistent Graph API and Security Routes is verified, secure, and ready for integration.
