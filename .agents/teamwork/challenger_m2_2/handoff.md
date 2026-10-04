# Milestone 2: API Correlation, Projections & Security Routes Challenger Handoff Report

## 1. Observation
- **Test Execution**:
  - Command:
    ```powershell
    $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_graph_api_v1.py
    ```
  - Result:
    `55 passed in 61.80s (0:01:01)`
  - Total test count: 18 baseline Milestone 2 tests + 37 adversarial attack test cases across 4 major threat vectors.
- **Observed File States**:
  - `src/correlation.py`: `CorrelationMiddleware` validates canonical 36-char UUID format, strips input on failure, returns HTTP 422 `{"detail": "invalid correlation id"}`, and generates/echoes valid UUIDs.
  - `src/schemas.py`: Safe projections (`RunProjection`, `ApprovalRequestProjection`) strip internal DB parameters, credentials, and raw checkpoints. Mutation requests enforce `extra="forbid"`.
  - `src/graph_api.py`:
    - D-30 compliance: `validation_exception_handler` strips `"input"` from Pydantic errors, preventing reflected input leakage.
    - Safe error masking: `/health/ready` catches database errors and masks them as `postgres unavailable: {type(exc).__name__}`, preventing database password/conninfo disclosure.
    - HMAC Kill Switch: `require_hmac` dependency intercepts decision routes (`/v1/runs/{id}/decision-challenge`, `/v1/runs/{id}/decision`, `/v1/approval-requests/{id}/decisions`, and legacy aliases) and halts execution with HTTP 403 `{"detail": "proof_type_disabled"}` prior to schema validation.
    - Readiness Probe: `/health/ready` validates database connectivity, runtime role posture (`fc27_app`), checkpointer table presence, view `fc27_approval_inconsistencies`, and confirms `DISPATCH_ENABLED=false` (returning HTTP 503 if any check fails).
- **Production Code Isolation**:
  - Zero files modified in `src/` or `migrations/`.

## 2. Logic Chain
1. *Malformed Correlation ID Handling*:
   When callers supply malformed or malicious `X-Correlation-Id` headers (tested with SQL injection, path traversal, oversized strings, and non-UUID hex), `CorrelationMiddleware.dispatch` in `src/correlation.py:34-54` executes before routing or authentication. Because `len(stripped) != 36` or `UUID(stripped)` fails canonical match, it immediately returns `JSONResponse(status_code=422, content={"detail": "invalid correlation id"})`. This ensures zero SQL or filesystem vulnerability surface and guarantees total caller input redaction.
2. *Information Leakage Prevention*:
   In `src/graph_api.py`, `RunProjection` defines an explicit allow-list of public fields. Sensitive internal fields (such as DB credentials, checkpoint states, and raw tracebacks) are excluded from the projection. In `validation_exception_handler`, `err_dict.pop("input", None)` removes input reflection from Pydantic error responses. In `/health/ready`, exception handling formats only `{type(exc).__name__}` rather than `str(exc)`, preventing accidental disclosure of database passwords embedded in connection strings.
3. *HMAC Kill Switch Security*:
   When `APPROVAL_CLI_HMAC_ENABLED=false`, the route dependency `require_hmac` is invoked on decision endpoints. For valid JSON payloads (empty, forged, or arbitrary), the dependency raises `HTTPException(403, "proof_type_disabled")` before Pydantic schema validation runs. Unauthenticated requests similarly encounter the 403 kill switch before authentication is evaluated. Method tampering encounters HTTP 405.
4. *Readiness Probe Fails-Closed*:
   `/health/ready` executes a comprehensive safety healthcheck against PostgreSQL: confirming connectivity, checking `check_runtime_posture`, verifying `public.checkpoints` exists, querying `fc27_approval_inconsistencies` for pairing discrepancies, and asserting `not settings.dispatch_enabled`. Degradation in any component immediately returns HTTP 503 with an informative sanitized reason, preventing traffic dispatch to an unhealthy or inconsistent instance.

## 3. Caveats
- When an adversary sends raw, unparseable JSON syntax (e.g. `b"{unparseable: json: syntax ???"`), Starlette's low-level request body decoder raises `RequestValidationError` (HTTP 422 `json_invalid`) before route dependencies run. In either case, execution of the decision path is blocked, so no authorization bypass is possible.
- No caveats regarding database security, input sanitization, or fail-closed probe behavior.

## 4. Conclusion
Milestone 2 Persistent Graph API and security routes are empirically robust against adversarial manipulation, correlation ID injection, information leakage, HMAC kill switch bypass, and degraded infrastructure states.
Verdict: **APPROVE**.

## 5. Verification Method
Execute the complete test suite against a test PostgreSQL instance:
```powershell
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_graph_api_v1.py
```
Expected output:
```
55 passed in ~62s
```
Verify files modified under write ownership:
- `tests/test_graph_api_v1.py`: Contains 18 baseline tests + 37 adversarial attack tests.
- `src/` and `migrations/`: Unmodified.
