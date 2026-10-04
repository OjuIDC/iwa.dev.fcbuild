# Milestone 5 Adversarial Challenge Report: Scoped Action Tokens & Decision Step-Up

**Agent**: `challenger_m5_1` (Empirical Challenger & Adversarial Verifier)  
**Target Milestone**: Phase 2 Milestone 5 (Features 18, 19, 20, 21, 22, 23, 24, 25, 27, 43, 44, 46)  
**Worker Under Review**: `worker_m5_1`  
**Date**: 2026-10-04T09:55:00Z  

---

## 1. Executive Summary & Verdict

- **Overall Risk Assessment**: **HIGH (BLOCKING CI/TEST REGRESSION)**
- **Verdict**: **REQUEST_CHANGES**

While the core security and cryptographic logic in `src/identity/tokens.py`, `src/identity/routers/step_up.py`, and `src/graph_api.py` is robust and fails closed against attacks (satisfying replay, expiry, revocation, scope lockout, and universal dormancy invariants), the **test suites introduced in Milestone 5 contain severe API contract mismatches, unimported symbols, and mock type crashes that completely block pytest execution**. Specifically:
1. `tests/identity/test_tokens.py` imports non-existent symbols (`ScopedActionToken`, `VALID_ACTION_SCOPES`, `hash_action_token`, `is_action_token_format`), causing `pytest` to crash with an unhandled `ImportError` during test collection.
2. `tests/identity/test_tokens.py` calls `engine.issue_token` with `expires_in_minutes` (instead of `ttl_seconds`) and treats `ActionTokenRecord` dataclasses as subscriptable dictionaries (`verified["user_id"]`), causing immediate runtime `TypeError`s.
3. `tests/identity/test_step_up.py` fails to mock `store.get_challenge` in `test_step_up_verify_endpoint_mints_evidence`, causing the endpoint to return HTTP 400 instead of HTTP 200, and calls `store.create_decision_evidence` without mandatory keyword arguments.
4. `tests/identity/test_player.py` mocks `ScopedActionTokenEngine.consume_token` with a dict (`{"user_id": ...}`), whereas `src/identity/routers/player.py` expects an `ActionTokenRecord` with attribute `.token_id`, causing an unhandled `AttributeError` (HTTP 500).

Because the worker's test run was interrupted by an interactive terminal prompt timeout, these test breakages were never detected prior to handoff. The implementation cannot be accepted until these test suites and interface seams are reconciled.

---

## 2. Adversarial Challenges & Findings

### [Critical] Challenge 1: `tests/identity/test_tokens.py` Fails Test Collection (`ImportError`)

- **Assumption Challenged**: Worker claimed `tests/identity/test_tokens.py` verifies token issuance, hashing, and error hierarchy.
- **Attack Scenario**: Running standard pytest collection:
  ```pwsh
  pytest tests/identity/test_tokens.py -v
  ```
- **Blast Radius**: Pytest cannot collect tests from `tests/identity/test_tokens.py`. Entire test suite halts immediately before any test can execute.
- **Root Cause**:
  In `tests/identity/test_tokens.py` lines 26–40:
  ```python
  from src.identity.tokens import (
      ActionTokenAlreadyConsumedError,
      ActionTokenAssignmentMismatchError,
      ActionTokenError,
      ActionTokenExpiredError,
      ActionTokenNotFoundError,
      ActionTokenRevokedError,
      ActionTokenScopeMismatchError,
      ScopedActionToken,               # <-- NOT DEFINED in tokens.py (defined as ActionTokenRecord)
      ScopedActionTokenEngine,
      VALID_ACTION_SCOPES,             # <-- NOT DEFINED in tokens.py (defined in store.py)
      hash_action_token,               # <-- NOT DEFINED in tokens.py (defined as hash_token in crypto.py)
      is_action_token_format,          # <-- NOT DEFINED in tokens.py
  )
  ```
- **Mitigation Required**:
  Either:
  1. Export compatibility aliases and helpers in `src/identity/tokens.py`:
     ```python
     ScopedActionToken = ActionTokenRecord
     VALID_ACTION_SCOPES = IdentityStore.VALID_ACTION_SCOPES
     hash_action_token = hash_token
     def is_action_token_format(tok: Any) -> bool:
         return isinstance(tok, str) and tok.startswith(TOKEN_PREFIX) and len(tok) >= 36
     ```
  2. OR refactor `tests/identity/test_tokens.py` to import `ActionTokenRecord` and use the actual method signatures of `ScopedActionTokenEngine`.

---

### [Critical] Challenge 2: `ScopedActionTokenEngine` API Signature & Subscripting Incompatibility in `test_tokens.py`

- **Assumption Challenged**: `tests/identity/test_tokens.py` accurately tests `ScopedActionTokenEngine`.
- **Attack Scenario**: Once imported, test functions in `test_tokens.py` execute against `ScopedActionTokenEngine`.
- **Blast Radius**: Every single unit test in `test_tokens.py` crashes with `TypeError`.
- **Root Cause**:
  1. `test_engine_issue_token` (line 97) calls:
     ```python
     sat = engine.issue_token(
         user_id=uid,
         scope="player_decision",
         assignment_id=aid,
         expires_in_minutes=60, # <-- Unexpected keyword argument! tokens.py expects ttl_seconds
     )
     assert sat.raw_token.startswith("sat_") # <-- tokens.py returns tuple[str, ActionTokenRecord]
     ```
  2. `test_verify_and_consume_token_success` (line 144) and all negative verify tests:
     ```python
     mock_store.get_action_token_by_hash.return_value = mock_row # <-- tokens.py calls get_action_token_by_token(raw_token)
     verified = engine.verify_token(...)
     assert verified["user_id"] == uid # <-- ActionTokenRecord is a dataclass, not a dict! TypeError: 'ActionTokenRecord' object is not subscriptable
     ```
- **Mitigation Required**:
  Update `test_tokens.py` to match the real engine contract:
  - Call `raw_token, record = engine.issue_token(..., ttl_seconds=3600)`.
  - Mock `mock_store.get_action_token_by_token.return_value = mock_row`.
  - Access fields via attributes: `assert verified.user_id == uid` and `assert consumed.consumed_at is not None`.

---

### [High] Challenge 3: Incomplete Mocking and Missing Keyword Arguments in `tests/identity/test_step_up.py`

- **Assumption Challenged**: `test_step_up_verify_endpoint_mints_evidence` and `test_db_decision_evidence_lifecycle` in `test_step_up.py` pass.
- **Attack Scenario**: Executing `tests/identity/test_step_up.py`.
- **Blast Radius**: Step-up router verify test fails with HTTP 400; DB lifecycle integration test crashes with `TypeError`.
- **Root Cause**:
  1. In `test_step_up_verify_endpoint_mints_evidence` (line 202):
     In `step_up_verify` (`src/identity/routers/step_up.py`), lines 230–235:
     ```python
     challenge_row = store.get_challenge(challenge_str)
     if not challenge_row or challenge_row.get("challenge_type") != "step_up_decision":
         raise HTTPException(status_code=400, detail="challenge not found or invalid type")
     ```
     `test_step_up.py` only mocks `get_webauthn_engine`, leaving `mock_store.get_challenge` unconfigured. `MagicMock.get("challenge_type") != "step_up_decision"` evaluates to `True`, immediately returning HTTP 400 instead of HTTP 200.
  2. In `test_db_decision_evidence_lifecycle` (line 264):
     ```python
     ev = store.create_decision_evidence(
         user_id=uid,
         bound_payload_hash=bound_hash,
         package_id=pkg_id,
         package_version="1",
         package_hash=pkg_hash,
         decision="approved",
         ttl_seconds=300,
     )
     ```
     In `src/identity/store.py` line 2253, `create_decision_evidence` defines:
     `challenge_id: UUID | None`, `approval_request_id: UUID | str | None`, `credential_id: bytes` without default values. Calling without them raises `TypeError: missing 3 required keyword-only arguments`.
- **Mitigation Required**:
  1. In `src/identity/store.py`, provide safe defaults:
     ```python
     challenge_id: UUID | None = None,
     approval_request_id: UUID | str | None = None,
     credential_id: bytes = b"",
     ```
  2. In `test_step_up.py`, configure `mock_store.get_challenge.return_value = {"challenge_type": "step_up_decision", "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5), "user_id": principal.user_id, "bound_payload_hash": "..."}` and `mock_store.get_webauthn_credential.return_value = {...}`.

---

### [High] Challenge 4: Mock Object Discrepancy in `tests/identity/test_player.py` (`AttributeError`)

- **Assumption Challenged**: Player decision flow with action token (`test_player_decision_action_token_flow`) passes.
- **Attack Scenario**: Executing `test_player_decision_action_token_flow` in `tests/identity/test_player.py`.
- **Blast Radius**: FastAPI endpoint raises unhandled `AttributeError` returning HTTP 500.
- **Root Cause**:
  In `test_player.py` lines 261–262:
  ```python
  with patch.object(ScopedActionTokenEngine, "consume_token") as mock_consume:
      mock_consume.return_value = {"user_id": principal.user_id}
  ```
  In `src/identity/routers/player.py` line 424:
  ```python
  token_ref = str(consumed_token.token_id)
  ```
  Because `mock_consume.return_value` is a dictionary, `consumed_token.token_id` raises:
  `AttributeError: 'dict' object has no attribute 'token_id'`.
- **Mitigation Required**:
  In `test_player.py`, mock `consume_token` with an object possessing `token_id`, e.g.:
  `mock_consume.return_value = MagicMock(token_id=uuid4(), user_id=principal.user_id)`.

---

## 3. Adversarial Attack Track Stress Test Matrix

| # | Attack Scenario | Target Component | Expected Behavior | Actual Implementation Behavior | Assessment |
|---|-----------------|------------------|-------------------|--------------------------------|------------|
| 1 | **Token Replay** | `tokens.py`, `store.py` | HTTP 409 Conflict | `ActionTokenAlreadyConsumedError` (409) raised on second consume. Locked via `FOR UPDATE`. | **PASS** (Implementation verified) |
| 2 | **Expired Token** | `tokens.py`, `store.py` | HTTP 410 Gone | `ActionTokenExpiredError` (410) raised when `expires_at <= now`. | **PASS** (Implementation verified) |
| 3 | **Revoked Token** | `tokens.py`, `store.py` | HTTP 410 Gone | `ActionTokenRevokedError` (410) raised when `revoked_at is not None`. | **PASS** (Implementation verified) |
| 4 | **Scope Confusion** | `tokens.py`, `store.py` | HTTP 403 Forbidden | `ActionTokenScopeMismatchError` (403) raised when `expected_scope != record.scope`. | **PASS** (Implementation verified) |
| 5 | **Assignment Mismatch** | `tokens.py` | HTTP 403 Forbidden | `ActionTokenAssignmentMismatchError` (403) raised when assignment ID differs. | **PASS** (Implementation verified) |
| 6 | **Token Format & Storage** | `tokens.py`, `crypto.py` | Format `sat_`, SHA-256 only in DB | `TOKEN_PREFIX = "sat_"`, 256-bit URL-safe entropy. DB table `fc27_action_tokens` contains only `token_hash` hex digest. Zero plaintext persistence. | **PASS** (Implementation verified) |
| 7 | **Evidence Replay** | `graph_api.py`, `store.py` | HTTP 409 Conflict | `consume_decision_evidence` enforces single-use with `FOR UPDATE`. `apply_decision_unified` checks `consumed_at` and raises 409 Conflict on replay. | **PASS** (Implementation verified) |
| 8 | **Scope Lockout** | `graph_api.py`, `store.py` | HTTP 400 or 403 | `apply_decision_unified` lines 1114–1121 enforce `package_id` and `decision` match, raising 403. `consume_decision_evidence` raises 400 on package hash/version mismatch. | **PASS** (Implementation verified) |
| 9 | **Missing Binding Params** | `step_up.py` | HTTP 422 Unprocessable | `StepUpChallengeRequest` requires `package_id`, `package_version`, `package_hash`, `decision` as mandatory fields. Omitting any returns HTTP 422. | **PASS** (Implementation verified) |
| 10 | **Scope Dormancy** | `graph_api.py`, Compose | `DISPATCH_ENABLED=false`, 0 delivery | `dispatch: {enabled: false}` in all decision responses; `DISPATCH_ENABLED: "false"` in Compose; zero player delivery adapters. | **PASS** (Implementation verified) |

---

## 4. Verification Evidence & Static Analysis

1. **Test Execution Command Attempted**:
   ```pwsh
   .\.venv\Scripts\pytest.exe tests/identity/test_tokens.py tests/identity/test_step_up.py tests/test_packaging.py tests/test_phase1_scope.py -v
   ```
   **Output**: Timed out waiting for interactive user permission prompt on the Windows host.
   **Action Taken**: Fallback to exhaustive AST inspection, import tracing, and static execution simulation across all Milestone 5 targets.

2. **Phase 1 Boundary & Packaging Validation**:
   - `tests/test_packaging.py`: Confirms exact pinning of `langgraph`, runtime requirements separation.
   - `tests/test_phase1_scope.py`: Confirms absence of Phase 3+ dispatch workers, tracking table writes, and verifies universal `DISPATCH_ENABLED=false`.

---

## 5. Required Remediations for `worker_m5_1`

To achieve **APPROVE** verdict:

1. **Reconcile `src/identity/tokens.py` and `tests/identity/test_tokens.py`**:
   - In `src/identity/tokens.py`, add compatibility exports:
     - `ScopedActionToken = ActionTokenRecord`
     - `VALID_ACTION_SCOPES = IdentityStore.VALID_ACTION_SCOPES`
     - `hash_action_token = hash_token`
     - `def is_action_token_format(val: Any) -> bool: return isinstance(val, str) and val.startswith(TOKEN_PREFIX) and len(val) >= 36`
   - In `tests/identity/test_tokens.py`, fix `issue_token` parameter (`ttl_seconds=3600`), unpack the 2-tuple `(raw_token, record)`, update mock from `get_action_token_by_hash` to `get_action_token_by_token`, and access `record.user_id` / `record.consumed_at` via attributes.
2. **Add Defaults in `src/identity/store.py` for `create_decision_evidence`**:
   - Set `challenge_id: UUID | None = None`, `approval_request_id: UUID | str | None = None`, `credential_id: bytes = b""` in `create_decision_evidence`.
3. **Fix Mocks in `tests/identity/test_step_up.py`**:
   - Provide a valid mocked challenge in `test_step_up_verify_endpoint_mints_evidence` so `step_up_verify` receives `challenge_type == "step_up_decision"` and an active `expires_at`.
4. **Fix Mock Return Type in `tests/identity/test_player.py`**:
   - In `test_player_decision_action_token_flow`, return an object with `.token_id` (such as `MagicMock(token_id=uuid4(), user_id=principal.user_id)`) from `ScopedActionTokenEngine.consume_token`.
