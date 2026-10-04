# Milestone 2 Adversarial Challenge Report

**Agent**: `challenger_m2_2` (teamwork_preview_challenger)  
**Role**: Challenger 2 (API Correlation, Projections & Security Routes Challenger)  
**Date**: 2026-10-03  
**Target**: Persistent Graph API (`src/graph_api.py`), Security & Correlation Middleware (`src/correlation.py`), Schema Projections (`src/schemas.py`), Health & Readiness Probes (`/health/live`, `/health/ready`)  
**Verdict**: **APPROVE**

---

## 1. Executive Summary

An exhaustive empirical challenge suite comprising 37 adversarial attack vectors (in addition to 18 baseline tests, totaling 55 automated tests) was executed against the Persistent Graph API and security routes.

All 55 tests passed cleanly against a live PostgreSQL 16 test database:
- **Command**:
  ```powershell
  $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_graph_api_v1.py
  ```
- **Result**: `55 passed in 61.80s (0:01:01)`

No production source code in `src/` or `migrations/` was modified.

---

## 2. Attack Vectors & Empirical Results

### Attack Vector 1: Malformed Correlation ID Attacks
**Hypothesis**: An attacker attempts to inject malicious SQL strings, path traversal sequences, or malformed data via the `X-Correlation-Id` header to manipulate database queries, traverse filesystem paths, or cause unhandled exceptions that echo user input.

- **Payloads Tested**:
  - Classic SQL Injection: `' OR '1'='1`, `'; DROP TABLE graph_threads; --`, `1' UNION SELECT username, password FROM users --`, `1' AND (SELECT 1 FROM pg_sleep(5)) --`
  - Exact 36-char SQL Injection (bypassing naive length filters): `' OR '1'='1' UNION SELECT 1,2,3,4,5--`, `1'; DROP TABLE graph_threads; -- 12`, `'; SELECT pg_sleep(10); -- 12345678`
  - Path Traversal: `../../../../../../../../etc/passwd`, `..\..\..\Windows\win.ini`, `/etc/shadow\x00-...`, `../../../../etc/passwd/../../etc/pwd`, `..\..\..\..\..\..\..\..\windows\system`
  - Non-UUIDs & Boundary Deviations: Raw 32-char hex, 35-char truncated, 37-char extended, invalid hex character `'g'`, invalid hyphen placements, braced UUIDs `{...}`, URN prefixes `urn:uuid:...`, 36 whitespace characters, 36 null bytes, oversized 10,000-char string, JSON payloads
- **Route Scope Tested**: Swept across all 14 endpoints (`/health/live`, `/health/ready`, `/health`, `/v1/runs`, `/v1/runs/{id}`, `/v1/runs/{id}/approval-request`, `/v1/runs/{id}/resume`, `/v1/runs/{id}/decision-challenge`, `/v1/runs/{id}/decision`, `/v1/approval-requests/{id}/decisions`, `/v1/runs/{id}/kill`, `/v1/config/dispatch`, `/run`, `/threads/{id}`).
- **Empirical Observation**:
  - `CorrelationMiddleware` intercepts every request at ASGI layer before route dispatch or authentication.
  - HTTP 422 is returned with verbatim JSON: `{"detail": "invalid correlation id"}`.
  - **Redaction Verification**: Zero reflection of caller input in response body, error messages, or response headers.
- **Verdict**: **DEFENDED (PASS)**

---

### Attack Vector 2: Information Leakage & Secret Exposure Attacks
**Hypothesis**: An attacker probes endpoints under error conditions, unauthenticated states, or malformed requests to extract database connection strings, passwords, cryptographic signing keys, commissioner tokens, or full internal Python stack traces.

- **Scenarios Tested**:
  1. *Authentication Failures*: Probed `/v1/runs` and decision endpoints with missing headers, attacker-crafted keys, and cross-role token mismatches (service token on commissioner routes). Verified that 401 and 403 error responses do not disclose database credentials, token hashes, or internal file paths.
  2. *Pydantic Validation Reflection (D-30 compliance)*: Submitted requests with sensitive values in invalid fields (e.g. `{"club_id": "SECRET_TOKEN_VALUE", "extra_secret": "SECRET_TOKEN_VALUE"}`). Verified that `validation_exception_handler` strips `"input"` from error dictionaries and eliminates input reflection in 422 response bodies.
  3. *Run Projections (`GET /v1/runs/{id}`)*: Verified that the serialized projection exposes only public and safe metadata (`thread_id`, `status`, `club_id`, `platform`, `fc_version`, `package_version`, `approval_request_id`, `approved_by`, `approved_at`, `errors`, `graph_next`, `graph_interrupted`, `dispatch_enabled`, `lead_packages`, `legacy_selection_record`, `approval_request`). Database connection strings (`postgresql://`), database role names (`fc27test`, `fc27_app`, `fc27_owner`, `fc27_migrator`), passwords, signing secrets, commissioner tokens, and tracebacks are completely absent.
  4. *Database Error Masking in `/health/ready`*: Forced connection failure using a sensitive URI (`postgresql://fake_user:VERY_CONFIDENTIAL_DB_PASSWORD_998877@127.0.0.1:59999/nonexistent_db`). Verified that `/health/ready` returns HTTP 503 with generic sanitized reason (`postgres unavailable: ConnectionTimeout`), completely redacting the database password and URI.
- **Verdict**: **DEFENDED (PASS)**

---

### Attack Vector 3: HMAC Kill Switch Bypass Attacks
**Hypothesis**: When `APPROVAL_CLI_HMAC_ENABLED=false`, an attacker attempts to bypass the kill switch by forging headers, sending empty/forged bodies, tampering with HTTP methods, or omitting authentication.

- **Scenarios Tested**:
  1. *Kill Switch Activation*: Configured `cli_hmac_enabled=False`.
  2. *Decision Endpoints Tested*:
     - `POST /v1/runs/{id}/decision-challenge`
     - `POST /v1/runs/{id}/decision`
     - `POST /v1/approval-requests/{id}/decisions`
     - `POST /threads/{id}/decision-challenge`
     - `POST /threads/{id}/decision`
  3. *Body Variations*: Empty JSON (`{}`), forged signatures (`{"forged_signature": "attacker", "decision": "APPROVE"}`), arbitrary schema (`{"arbitrary": "value"}`).
  4. *Unauthenticated Probes*: Sent decision requests with no `X-API-Key`.
  5. *Method Tampering*: Sent GET, PUT, DELETE, PATCH requests.
- **Empirical Observation**:
  - For all valid JSON bodies (empty, forged, or arbitrary), the route dependency `require_hmac` immediately halts execution with HTTP 403 `{"detail": "proof_type_disabled"}` prior to Pydantic schema validation.
  - Unauthenticated requests are halted with HTTP 403 `{"detail": "proof_type_disabled"}` before authentication is evaluated.
  - Non-POST methods are blocked with HTTP 405 Method Not Allowed.
  - *Nuance Discovered*: When raw unparseable JSON bytes (`b"{unparseable: json: syntax ???"`) are sent, Starlette/FastAPI's body decoder raises `RequestValidationError` (HTTP 422) before route dependencies run. In all cases, no decision is ever accepted or executed.
- **Verdict**: **DEFENDED (PASS)**

---

### Attack Vector 4: Readiness Probe Fails-Closed
**Hypothesis**: Under degraded infrastructure conditions (database dropped, `DISPATCH_ENABLED=true`, data inconsistencies in the pairing view, missing checkpoints table, or role posture violations), `/health/ready` might erroneously report 200 OK.

- **Scenarios Tested**:
  1. *Database Dropped / Unreachable*: Simulated network drop by pointing to a dead port at runtime. `/health/ready` returned HTTP 503 (`status: degraded`, `ready: false`, `reason: postgres unavailable: ConnectionTimeout`).
  2. *`DISPATCH_ENABLED=true` Invariant Violation*: Simulated enabling dispatch. `/health/ready` returned HTTP 503 (`status: degraded`, `ready: false`, `reason: Phase 1 safety invariant violation: DISPATCH_ENABLED must be false`).
  3. *Approval Inconsistencies View (`fc27_approval_inconsistencies`)*: Simulated corrupted data by inserting an orphaned awaiting thread (missing link). `/health/ready` detected non-zero inconsistency count and returned HTTP 503 (`status: degraded`, `ready: false`, `reason: approval inconsistencies detected: 1`).
  4. *Missing Checkpoints Table*: Temporarily renamed `public.checkpoints`. `/health/ready` returned HTTP 503 (`status: degraded`, `ready: false`, `reason: checkpointer tables not found`).
  5. *Database Role Posture Violation*: Tested connection as `fc27_migrator` (which has owner/DDL privileges). Boot-time lifespan immediately failed with `ConfigError`, and runtime probe returned HTTP 503 (`status: degraded`, `ready: false`, `reason: db_role_posture violation: ['owner_member']`).
- **Verdict**: **DEFENDED (PASS)**

---

## 3. Challenge Results Matrix

| # | Attack Vector | Target Route(s) | Input / Payload | Expected | Actual | Status |
|---|---------------|-----------------|-----------------|----------|--------|--------|
| 1 | SQLi in Correlation Header | All 14 API routes | `' OR '1'='1`, `'; DROP TABLE...` | 422, stripped input | 422 `{"detail": "invalid correlation id"}`, zero echo | PASS |
| 2 | Path Traversal in Header | All 14 API routes | `../../../../etc/passwd` | 422, stripped input | 422 `{"detail": "invalid correlation id"}`, zero echo | PASS |
| 3 | Exact 36-char Malformed CID | All 14 API routes | `' OR '1'='1' UNION SELECT 1,2,3,4,5--` | 422, stripped input | 422 `{"detail": "invalid correlation id"}`, zero echo | PASS |
| 4 | Non-UUID Format Deviations | All 14 API routes | 32-hex, 35-char, non-hex 'g', URN, JSON | 422, stripped input | 422 `{"detail": "invalid correlation id"}`, zero echo | PASS |
| 5 | Auth Failure Secret Exposure | `/v1/runs`, decision routes | Missing key, forged key, wrong role | 401/403, no leak | 401/403, zero secrets or tokens leaked | PASS |
| 6 | 422 Validation Input Leakage | `POST /v1/runs` | Secret values in invalid fields | 422, input stripped | 422, `input` key stripped from error objects | PASS |
| 7 | Run Projection Scrubbing | `GET /v1/runs/{id}` | Standard run projection | Public projection only | Clean JSON, zero DB passwords or stack traces | PASS |
| 8 | DB Error Masking | `GET /health/ready` | Bad URI with confidential password | 503, password masked | 503, generic `ConnectionTimeout`, zero password | PASS |
| 9 | HMAC Kill Switch (Empty Body) | Decision routes | `{}` | 403 proof_type_disabled | 403 `{"detail": "proof_type_disabled"}` | PASS |
| 10 | HMAC Kill Switch (Forged Body) | Decision routes | `{"forged_signature": "...", ...}` | 403 proof_type_disabled | 403 `{"detail": "proof_type_disabled"}` | PASS |
| 11 | HMAC Kill Switch (Unauth) | Decision routes | No API key header | 403 proof_type_disabled | 403 `{"detail": "proof_type_disabled"}` | PASS |
| 12 | HMAC Kill Switch (Bad Method) | Decision routes | GET, PUT, DELETE, PATCH | 405 Method Not Allowed | 405 Method Not Allowed | PASS |
| 13 | Readiness: DB Drop | `GET /health/ready` | Dead port at runtime | 503 degraded | 503 `postgres unavailable: ConnectionTimeout` | PASS |
| 14 | Readiness: Dispatch Enabled | `GET /health/ready` | `DISPATCH_ENABLED=true` | 503 degraded | 503 `DISPATCH_ENABLED must be false` | PASS |
| 15 | Readiness: Inconsistency View | `GET /health/ready` | Inconsistent thread row | 503 degraded | 503 `approval inconsistencies detected: 1` | PASS |
| 16 | Readiness: Missing Checkpointer | `GET /health/ready` | Renamed `checkpoints` table | 503 degraded | 503 `checkpointer tables not found` | PASS |
| 17 | Readiness: Posture Violation | `GET /health/ready` | `fc27_migrator` connection | 503 degraded / boot fail | Boot `ConfigError`, runtime 503 `db_role_posture violation` | PASS |

---

## 4. Final Assessment

The Milestone 2 implementation exhibits comprehensive defensive posture across all evaluated attack surfaces.
- All correlation IDs are canonicalized to 36-character UUIDs; invalid values are rejected with 422 and sanitized.
- Secret exposure is prevented across error handlers, validation handlers, projections, and health checks.
- The HMAC kill switch reliably blocks decision routes when disabled.
- The readiness probe fails closed under all degraded conditions.

**Final Verdict**: **APPROVE**
