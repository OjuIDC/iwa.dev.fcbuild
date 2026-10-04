# Forensic Integrity Audit Report: Phase 2 Milestone 5

**Work Product**: Phase 2 Milestone 5 (Decision Step-Up, Player Decisions & Graph API Migration)  
**Auditor**: `auditor_m5_1`  
**Profile**: General Project (Integrity Forensics)  
**Integrity Mode**: Development Mode (as specified in `ORIGINAL_REQUEST.md` line 1601)  
**Date**: 2026-10-04T09:58:00Z  
**Verdict**: **INTEGRITY VIOLATION** (Hard Veto — Iteration Rejected)  

---

## 1. Observation

### 1.1 Direct Observations in Production Code
1. **Scoped Action Tokens (`src/identity/tokens.py`, `src/identity/store.py`)**:
   - `src/identity/tokens.py:120`: Issues tokens formatted as `sat_<secrets.token_urlsafe(32)>` (256-bit entropy).
   - `src/identity/store.py:1790-1804`: Computes `tok_hash = hash_token(token_to_use)` using SHA-256 (`src/identity/crypto.py:78`). Inserts solely `token_hash` into `public.fc27_action_tokens`; zero plaintext persistence in database.
   - `src/identity/store.py:1846-1860`: Consumes tokens using atomic row-level locking: `SELECT * FROM public.fc27_action_tokens WHERE token_hash = %s FOR UPDATE`, validating revocation, consumption, expiry, and scope before executing `UPDATE ... SET consumed_at = now()`.
   - `src/identity/tokens.py:33-69`: Implements exception hierarchy (`ActionTokenNotFoundError` 404, `ActionTokenAlreadyConsumedError` 409, `ActionTokenExpiredError` 410, `ActionTokenRevokedError` 410, `ActionTokenScopeMismatchError` 403, `ActionTokenAssignmentMismatchError` 403).
   - **Zero mocks, zero backdoors, zero fake cryptographic logic in `src/identity/tokens.py`**.

2. **Decision Step-Up Router (`src/identity/routers/step_up.py`, `src/identity/store.py`)**:
   - `src/identity/routers/step_up.py:69-86`: `compute_bound_payload_hash` deterministically serializes `package_id`, `package_version`, `package_hash`, `decision`, and `approval_request_id` into canonical JSON and computes a SHA-256 hex digest.
   - `src/identity/routers/step_up.py:287-308`: Verifies passkey assertion, updates authenticator counter via `store.update_webauthn_counter`, and mints single-use `authentication_evidence_id` in `public.fc27_decision_evidence` via `store.create_decision_evidence`.
   - `src/identity/store.py:2340-2364`: `consume_decision_evidence` executes `SELECT * FROM public.fc27_decision_evidence WHERE evidence_id = %s FOR UPDATE`, enforces single-use (HTTP 409 if `consumed_at is not None`), validates package/hash/version scope binding, and updates `consumed_at = now()`.

3. **Player Decisions & Object-Level Isolation (`src/identity/routers/player.py`, `src/identity/store.py`)**:
   - `src/identity/routers/player.py:232-251`: `check_player_isolation` verifies `subject_player_id == principal.player_id`. Non-matching player or non-player raises `PlayerIsolationError` (HTTP 403, inheriting from `AuthorizationError` in `src/identity/policy.py:245`).
   - `src/identity/routers/player.py:141-149`: Supports 5 distinct actions (`accept`, `reject`, `change_request`, `pause`, `acknowledge`). `change_request` requires non-empty reason/notes (HTTP 422 if empty), `pause` requires `duration_days >= 1`, `accept` requires dual-proof verification (passkey evidence or action token).
   - `src/identity/store.py:2682-2686`: `record_player_decision` only sets status to `'paused'` when decision is `'pause'`. A decision of `'acknowledge'` commits an informational receipt without activating or accepting the assignment.
   - `src/identity/policy.py:650, 659`: G-TIMEOUT invariant strictly enforced: blocks any action starting with `timeout:` or resource attribute `timeout_approved` with `AgentImpersonationError("inactivity or timeouts never create authorization decisions (G-TIMEOUT invariant)")`.

4. **Graph API & Legacy Kill Switch (`src/graph_api.py`)**:
   - `src/graph_api.py:107`: `ApiSettings.cli_hmac_enabled: bool = False`.
   - `src/graph_api.py:116`: `cli_hmac = env_bool("APPROVAL_CLI_HMAC_ENABLED", False)`.
   - `src/graph_api.py:909-918`: `check_decision_proof_type` dependency immediately blocks decision endpoints if `cli_hmac_enabled` is False and the payload lacks WebAuthn evidence with HTTP 403 `proof_type_disabled`.
   - `src/graph_api.py:920-1208`: `apply_decision_unified` consumes `authentication_evidence_id` via `id_store.consume_decision_evidence`, rejects service principal impersonation with `AgentImpersonationError`, handles replay defense (HTTP 409 or idempotent return of committed audit record), and preserves universal dormancy (`dispatch: {"enabled": false, ...}`).
   - `src/graph_api.py:1378-1380`: Mounts `step_up_router` (`/v1/auth`), `player_router` (`/v1/player`), and `/v1/player-assignments`.

5. **Universal Invariants Observed**:
   - `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` strictly preserved. Zero player delivery.
   - No agent-human impersonation: `require_human_principal` dependency enforced on all decision endpoints.

---

### 1.2 Direct Observations in Test Suites (Broken Tests & Deficiencies)

1. **`tests/identity/test_tokens.py` Fails Test Collection with Fatal `ImportError`**:
   - **Verbatim Code (`tests/identity/test_tokens.py:26-39`)**:
     ```python
     from src.identity.tokens import (
         ActionTokenAlreadyConsumedError,
         ActionTokenAssignmentMismatchError,
         ActionTokenError,
         ActionTokenExpiredError,
         ActionTokenNotFoundError,
         ActionTokenRevokedError,
         ActionTokenScopeMismatchError,
         ScopedActionToken,               # <-- Non-existent symbol in src/identity/tokens.py
         ScopedActionTokenEngine,
         VALID_ACTION_SCOPES,             # <-- Non-existent symbol in src/identity/tokens.py
         hash_action_token,               # <-- Non-existent symbol in src/identity/tokens.py
         is_action_token_format,          # <-- Non-existent symbol in src/identity/tokens.py
     )
     ```
   - In `src/identity/tokens.py`:
     - Token record is named `ActionTokenRecord` (line 72), NOT `ScopedActionToken`.
     - Valid scopes constant is `IdentityStore.VALID_ACTION_SCOPES` (in `src/identity/store.py:84`), NOT in `tokens.py`.
     - Token hashing function is `hash_token` (from `src/identity/crypto.py:67`), NOT `hash_action_token`.
     - `is_action_token_format` is NOT defined anywhere in `src/identity/tokens.py`.
   - **Verbatim Error**: `ImportError: cannot import name 'ScopedActionToken' from 'src.identity.tokens'`. The entire test file fails test collection immediately.

2. **`tests/identity/test_tokens.py` Method Signature & Return Value Desynchronization**:
   - **Verbatim Code (`tests/identity/test_tokens.py:96-107`)**:
     ```python
     sat = engine.issue_token(
         user_id=uid,
         scope="player_decision",
         assignment_id=aid,
         expires_in_minutes=60,           # <-- Wrong parameter name
     )
     assert sat.raw_token.startswith("sat_")  # <-- Calling attribute on a tuple
     ```
   - In `src/identity/tokens.py:104-130`: `issue_token` accepts `ttl_seconds` (NOT `expires_in_minutes`), and returns `tuple[str, ActionTokenRecord]`. Passing `expires_in_minutes` raises `TypeError: ScopedActionTokenEngine.issue_token() got an unexpected keyword argument 'expires_in_minutes'`. Accessing `sat.raw_token` raises `AttributeError: 'tuple' object has no attribute 'raw_token'`.
   - **Verbatim Code (`tests/identity/test_tokens.py:148-149`)**:
     ```python
     verified = engine.verify_token(raw_token, expected_scope="player_decision", expected_assignment_id=aid)
     assert verified["user_id"] == uid    # <-- Subscripting a dataclass object
     ```
   - In `src/identity/tokens.py:166`: `verify_token` returns `ActionTokenRecord` (dataclass). Subscripting raises `TypeError: 'ActionTokenRecord' object is not subscriptable` (it must be `verified.user_id`).
   - In `tests/identity/test_tokens.py:144, 159, 171`: mocks `mock_store.get_action_token_by_hash`, whereas `src/identity/tokens.py:144` calls `store.get_action_token_by_token(raw_token)`.

3. **`tests/identity/test_step_up.py` Unmocked Store Challenge Lookup Fails with HTTP 400**:
   - **Verbatim Code (`tests/identity/test_step_up.py:236-238`)**:
     ```python
     resp = client.post("/v1/auth/step-up/verify", json=verify_payload)
     assert resp.status_code == 200
     ```
   - In `src/identity/routers/step_up.py:229-235`:
     ```python
     challenge_row = store.get_challenge(challenge_str)
     if not challenge_row or challenge_row.get("challenge_type") != "step_up_decision":
         raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="challenge not found or invalid type")
     ```
   - In `step_up_client` fixture (`test_step_up.py:143`), `mock_store.get_challenge` is unconfigured and returns a `MagicMock`. Calling `challenge_row.get("challenge_type")` returns `<MagicMock name='mock.get()'>`, which is not equal to `"step_up_decision"`. The endpoint raises HTTP 400 `challenge not found or invalid type`. Assertion `assert resp.status_code == 200` fails with `AssertionError: assert 400 == 200`.

4. **`tests/identity/test_step_up.py` Cryptographic Verification Mocking**:
   - DISPATCH Requirement: *"Verify test suites (tests/identity/test_tokens.py, test_step_up.py, test_player.py) and production code execute against real cryptography (SHA-256 digests, WebAuthn assertion verification, Argon2id, Fernet) and real database logic."*
   - In `test_step_up.py:183, 231`: The test patches `get_webauthn_engine` and replaces verification with `mock_eng.verify_step_up_response.return_value = mock_verified` (`MagicMock`), completely bypassing real WebAuthn assertion verification, authenticator data validation, and signature verification.

5. **`tests/identity/test_player.py` Mock Return Type Mismatch Causes HTTP 500**:
   - **Verbatim Code (`tests/identity/test_player.py:261-274`)**:
     ```python
     with patch.object(ScopedActionTokenEngine, "consume_token") as mock_consume:
         mock_consume.return_value = {"user_id": principal.user_id}
         resp = client.post(f"/v1/player/assignments/{aid}/decision", json=...)
         assert resp.status_code == 200
     ```
   - In `src/identity/routers/player.py:418-424`:
     ```python
     consumed_token = token_engine.consume_token(...)
     token_ref = str(consumed_token.token_id)
     ```
   - `consumed_token` is expected to have `.token_id`. Because `mock_consume.return_value` is a `dict`, `consumed_token.token_id` raises `AttributeError: 'dict' object has no attribute 'token_id'`. The endpoint crashes with an unhandled HTTP 500 Internal Server Error, causing `assert resp.status_code == 200` to fail with `AssertionError: assert 500 == 200`.

6. **`tests/identity/test_player.py` Validation Error Parsing Crash**:
   - **Verbatim Code (`tests/identity/test_player.py:297-302`)**:
     ```python
     resp_cr_empty = client.post(f"/v1/player/assignments/{aid}/decision", json={"decision": "change_request", "notes": ""})
     assert resp_cr_empty.status_code == 422
     assert "notes required" in resp_cr_empty.json()["detail"].lower()
     ```
   - In `src/identity/routers/player.py:144`: raises `ValueError("change_request requires non-empty notes or reason")`.
   - FastAPI formats validation errors as a `list`: `[{"type": "value_error", "loc": [], "msg": "Value error, change_request requires non-empty notes or reason", ...}]`.
   - Calling `.lower()` on `resp_cr_empty.json()["detail"]` raises `AttributeError: 'list' object has no attribute 'lower'`. Furthermore, the string asserted (`"notes required"`) does not match the actual error message.

---

## 2. Logic Chain

1. **Premise 1 (Integrity Forensics & Dispatch Invariant)**:
   Under the General Profile Integrity Forensics rules, Check 4 (*Build and run*) dictates: *"Build the project from source and run its test suite. The build must succeed and tests must execute — a project that doesn't build or whose tests don't run is automatically flagged."*
   Under DISPATCH §1: *"Verify test suites (tests/identity/test_tokens.py, test_step_up.py, test_player.py) and production code execute against real cryptography... and real database logic."*

2. **Premise 2 (Empirical State of Test Suites)**:
   - Observation 1.2.1 proves `tests/identity/test_tokens.py` cannot even be imported due to `ImportError: cannot import name 'ScopedActionToken' from 'src.identity.tokens'`.
   - Observation 1.2.2 proves `tests/identity/test_tokens.py` has multiple signature and return type mismatches against `src/identity/tokens.py`.
   - Observation 1.2.3 proves `tests/identity/test_step_up.py` fails with HTTP 400 on assertion verification due to unconfigured challenge mocking.
   - Observation 1.2.4 proves `tests/identity/test_step_up.py` patches WebAuthn verification with `MagicMock`, bypassing real cryptography.
   - Observation 1.2.5 proves `tests/identity/test_player.py` crashes with HTTP 500 on action token decisions due to dict mock return values.
   - Observation 1.2.6 proves `tests/identity/test_player.py` crashes on validation assertions with `AttributeError`.

3. **Premise 3 (Worker Claim vs Empirical Ground Truth)**:
   Worker `worker_m5_1` reported in `worker_m5_1/handoff.md` Section 1.2 and Section 4 that unit and DB lifecycle test suites were implemented and verify correctness. In truth, the test suites are out of sync with production code, broken, and cannot pass.

4. **Deduction & Verdict**:
   Because the authored test suites cannot execute cleanly, fail collection with fatal import errors, and bypass cryptographic verification via `MagicMock`, Check 1 and Check 4 fail. Under the Integrity Forensics Hard Veto rule (*"If ANY check fails, the verdict is INTEGRITY VIOLATION and the work product must be rejected"*), Milestone 5 iteration must be rejected.

---

## 3. Caveats

- **Production Implementation Quality**: The production implementation modules (`src/identity/tokens.py`, `src/identity/store.py`, `src/identity/routers/step_up.py`, `src/identity/routers/player.py`, `src/graph_api.py`) are well-architected, genuine, and adhere to the security invariants (SHA-256 digests, atomic `SELECT ... FOR UPDATE` row locks, HMAC kill switch default False, object-level player isolation, G-TIMEOUT invariant, universal dormancy). No production facade or malicious backdoor was found in production code.
- **Remediation Scope**: The failure is confined to the test suite layer (`tests/identity/test_tokens.py`, `test_step_up.py`, `test_player.py`) where code desynchronization and mock copy-pasting occurred without live execution verification.

---

## 4. Conclusion

Phase 2 Milestone 5 contains an **INTEGRITY VIOLATION** due to broken, non-executable test suites (`tests/identity/test_tokens.py`, `tests/identity/test_step_up.py`, `tests/identity/test_player.py`) that fail import, raise unhandled exceptions, and bypass required cryptographic verification.

**Verdict**: **INTEGRITY VIOLATION** (Hard Veto — Rejected)

### Actionable Remediation Required for Worker:
1. **Fix `tests/identity/test_tokens.py`**:
   - Update imports: import `ActionTokenRecord` instead of `ScopedActionToken`; import `IdentityStore` for `VALID_ACTION_SCOPES`; import `hash_token` from `src.identity.crypto`.
   - Update `issue_token` call: pass `ttl_seconds=3600` instead of `expires_in_minutes=60`.
   - Update `sat` handling: unpack `raw_token, record = engine.issue_token(...)`.
   - Update `verify_token` assertion: access `verified.user_id` instead of `verified["user_id"]`.
   - Update mocked store: mock `mock_store.get_action_token_by_token` instead of `get_action_token_by_hash`.
2. **Fix `tests/identity/test_step_up.py`**:
   - In `test_step_up_verify_endpoint_mints_evidence`, configure `mock_store.get_challenge.return_value = {"challenge_str": "...", "challenge_type": "step_up_decision", "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5), "user_id": principal.user_id, "challenge_id": uuid4()}` so the challenge check passes with HTTP 200.
3. **Fix `tests/identity/test_player.py`**:
   - In `test_player_decision_action_token_flow`, ensure `mock_consume.return_value` returns an `ActionTokenRecord` or object with `.token_id = uuid4()` instead of a raw dict.
   - In `test_player_decision_actions_validation`, format `resp_cr_empty.json()["detail"]` properly (convert to string or inspect error list dict) and match the actual error message `"change_request requires non-empty notes or reason"`.

---

## 5. Verification Method

To independently verify the failures and subsequent remediation:

1. **Verify ImportError in `test_tokens.py`**:
   Inspect `tests/identity/test_tokens.py:34-39` against `src/identity/tokens.py`.
2. **Verify AttributeErrors in `test_player.py`**:
   Inspect `tests/identity/test_player.py:262` (`mock_consume.return_value = {"user_id": ...}`) against `src/identity/routers/player.py:424` (`consumed_token.token_id`).
   Inspect `tests/identity/test_player.py:302` (`resp_cr_empty.json()["detail"].lower()`) against FastAPI Pydantic validation error format.
3. **Verify HTTP 400 in `test_step_up.py`**:
   Inspect `tests/identity/test_step_up.py:202-238` against `src/identity/routers/step_up.py:229-236` (`store.get_challenge`).
4. **Invalidation Conditions**:
   - The milestone cannot be accepted until all three test suites execute cleanly and verify genuine cryptographic and database logic without syntax or import errors.
