# Milestone 2 Reviewer 2 Handoff Report: Persistent Graph API & Security Routes

## 1. Observation

- **Review Target**:
  - `src/graph_api.py`: Versioned `/v1/` routes, legacy aliases, `/health/live`, `/health/ready`, `/health`, HMAC kill switch dependency, and `/v1/runs/{id}/kill` operational switch.
  - `src/correlation.py`: `CorrelationMiddleware`, `CorrelationIdFilter`, `correlation_id_var` ContextVar, and UUID validation/stripping logic.
  - `src/schemas.py`: `RunProjection`, `ApprovalRequestProjection`, `ResumeRequest`, `ResumeResponse`, `DecisionResponse`, `DispatchBlock`, `KillRunRequest`, `KillRunResponse`, `HealthLiveResponse`, `HealthReadyResponse`.
  - `tests/test_graph_api_v1.py`: 18 tests covering correlation middleware, durable runs, projections, authorization refusal on resume, kill switch, health probes, and legacy aliases.

- **Independent Test Verification Results**:
  - Command 1:
    ```powershell
    $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_graph_api_v1.py
    ```
    Output:
    ```
    ..................                                                       [100%]
    18 passed in 22.19s
    ```
  - Command 2:
    ```powershell
    $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_db_roles.py tests/test_events_outbox.py tests/test_approval_lifecycle.py
    ```
    Output:
    ```
    .................................                                        [100%]
    33 passed in 42.79s
    ```

- **Detailed Code Observations**:
  - `src/correlation.py:35-56`: `CorrelationMiddleware.dispatch` checks `len(stripped) != 36` and `UUID(stripped)` with `str(cid).lower() != stripped.lower()`, returning 422 `{"detail": "invalid correlation id"}` without echoing malformed input. Sets `correlation_id_var` and echoes `response.headers["X-Correlation-Id"] = str(cid)`.
  - `src/graph_api.py:269-277`: Custom `validation_exception_handler` strips `"input"` from all Pydantic `RequestValidationError` dictionaries.
  - `src/graph_api.py:291-356`: `/health/live` returns process liveness. `/health/ready` validates PostgreSQL connection, role posture, checkpointer tables, approval inconsistencies view, and fails with 503 if `settings.dispatch_enabled == True`.
  - `src/graph_api.py:509-523`: `v1_resume` validates `ResumeRequest` (which has `extra="forbid"` and 0 fields), checks that thread status is already in `DECIDED_STATUSES` (raising 409 if uncommitted), and invokes `resume_graph(thread_id)` without taking authorization inputs.
  - `src/graph_api.py:527-529, 537, 594, 614`: `require_hmac` dependency raises 403 `{"detail": "proof_type_disabled"}` before body validation when `cli_hmac_enabled=False`.
  - `src/graph_api.py:638-728`: `/v1/runs/{thread_id}/kill` locks open nonces and thread with `FOR UPDATE`, marks status `'expired'`, revokes open nonces, and records auditable domain events.
  - `src/schemas.py:80-112`: Safe projections (`RunProjection`, `ApprovalRequestProjection`) omit database URIs, passwords, signing secrets, and LangGraph raw checkpoint blobs.

- **Integrity Check**:
  - Checked for hardcoded test results: None found.
  - Checked for dummy/facade implementations: None found; real PostgreSQL transactions, locks, and queries are executed.
  - Checked for shortcuts or bypassed requirements: None found.
  - Checked for fabricated verification logs: All outputs were independently captured via direct tool execution.

## 2. Logic Chain

1. Per Section 5 of `ORIGINAL_REQUEST.md` and Milestone 2 specification, the Persistent Graph API must provide versioned `/v1/` routes, safe projections, robust health probes, correlation tracing, and fail-closed security switches.
2. In `src/correlation.py`, all incoming requests are screened for a valid 36-character canonical UUID; if valid, it is tracked via `ContextVar` and propagated into log records via `CorrelationIdFilter`; if absent, a new UUIDv4 is minted; if invalid, it returns 422 with sanitized detail. Observed directly in lines 35–65 and verified by `test_malformed_correlation_id_rejected` and `test_correlation_id_echoed_when_present`.
3. In `src/schemas.py` and `src/graph_api.py`, `RunProjection` and `ApprovalRequestProjection` restrict returned fields to safe metadata and candidate summaries, while `_to_run_projection` forces `dispatch_enabled=False`. Verified via `test_v1_get_run_safe_projection_omits_secrets` which confirms no passwords or connection strings are leaked.
4. In `src/graph_api.py`, `v1_resume` rejects any body payloads via `ResumeRequest(extra="forbid")` and refuses to resume if the status is not already in `DECIDED_STATUSES`. Verified by `test_v1_resume_refuses_authorization_payload` (422 on payload) and `test_v1_resume_fails_closed_when_uncommitted` (409 on uncommitted decision).
5. In `src/graph_api.py`, `health_ready` evaluates database readiness and explicitly halts with 503 if `settings.dispatch_enabled == True`. Verified by `test_health_ready_fails_if_dispatch_enabled_true`.
6. In `src/graph_api.py`, `require_hmac` dependency guards all decision endpoints, terminating requests with 403 `{"detail": "proof_type_disabled"}` prior to request body validation when `APPROVAL_CLI_HMAC_ENABLED=false`. Verified by `test_hmac_kill_switch_behavior`.
7. Therefore, all Milestone 2 requirements within the scope of Reviewer 2 are completely satisfied, functionally verified, and architecturally compliant.

## 3. Caveats

- `CorrelationMiddleware` enforces canonical 36-character UUID representation, which accepts standard UUID formats (RFC 4122 v1 through v5). When absent, it generates UUIDv4. This conforms to industry best practice for distributed tracing headers.
- Production source code in `src/` and migrations in `migrations/` were strictly inspected without modifications, in accordance with the reviewer role constraints.

## 4. Conclusion

- **Verdict**: **APPROVE**
- Milestone 2 Persistent Graph API and Security Routes are verified with 100% test pass rate, robust adversarial defense, zero integrity violations, and full adherence to Phase 1 non-negotiable invariants.

## 5. Verification Method

To independently reproduce and verify this review:
1. Run Milestone 2 Graph API test suite:
   ```powershell
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_graph_api_v1.py
   ```
   Expected: 18 passed.
2. Run baseline regression test suites:
   ```powershell
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_db_roles.py tests/test_events_outbox.py tests/test_approval_lifecycle.py
   ```
   Expected: 33 passed.
3. Review `review.md` and `handoff.md` in `.agents/teamwork/reviewer_m2_2/`.
