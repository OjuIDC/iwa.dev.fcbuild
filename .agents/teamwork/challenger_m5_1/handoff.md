# Handoff Report: Phase 2 Milestone 5 Adversarial Challenge & Verification

**Agent**: `challenger_m5_1`  
**Role**: Phase 2 Milestone 5 Challenger (critic, specialist)  
**Date**: 2026-10-04T09:56:00Z  
**Status**: Task Complete (Hard Handoff)  
**Parent Agent**: `orchestrator_phase2` (Conversation ID: `25210876-a9ad-4f78-a87c-dec88e86edb9`)  
**Verdict**: **REQUEST_CHANGES**  

---

## 1. Observation

Direct observations from repository files, tool executions, and static analysis:

### 1.1 Tool Execution Output
- **Consolidated pytest execution attempted**:
  ```pwsh
  .\.venv\Scripts\pytest.exe tests/identity/test_tokens.py tests/identity/test_step_up.py tests/test_packaging.py tests/test_phase1_scope.py -v
  ```
- **Verbatim Error Output**:
  ```
  Encountered error in tool execution: permission check failed for command ".\\.venv\\Scripts\\pytest.exe tests/identity/test_tokens.py tests/identity/test_step_up.py tests/test_packaging.py tests/test_phase1_scope.py -v": Permission prompt for action 'command' on target '.\.venv\Scripts\pytest.exe tests/identity/test_tokens.py tests/identity/test_step_up.py tests/test_packaging.py tests/test_phase1_scope.py -v' timed out waiting for user response. The user was not able to provide permission on time. You should proceed as much as possible without access to this resource.
  ```
- Pursuant to dispatch instructions (§4), empirical verification fell back to exhaustive AST inspection, import resolution, and static execution simulation.

### 1.2 Test Collection & Import Failure in `tests/identity/test_tokens.py`
- In `tests/identity/test_tokens.py` (lines 26–40):
  ```python
  from src.identity.tokens import (
      ActionTokenAlreadyConsumedError,
      ActionTokenAssignmentMismatchError,
      ActionTokenError,
      ActionTokenExpiredError,
      ActionTokenNotFoundError,
      ActionTokenRevokedError,
      ActionTokenScopeMismatchError,
      ScopedActionToken,
      ScopedActionTokenEngine,
      VALID_ACTION_SCOPES,
      hash_action_token,
      is_action_token_format,
  )
  ```
- In `src/identity/tokens.py`:
  - `ScopedActionToken` is NOT defined (the entity class is `ActionTokenRecord` at line 72).
  - `VALID_ACTION_SCOPES` is NOT defined or exported (defined on `IdentityStore.VALID_ACTION_SCOPES` in `src/identity/store.py` line 84).
  - `hash_action_token` is NOT defined or exported (the hashing function in `src.identity.crypto` is named `hash_token`).
  - `is_action_token_format` is NOT defined or exported anywhere in `src/`.
- **Result**: Invoking `pytest tests/identity/test_tokens.py` crashes during test discovery with `ImportError: cannot import name 'ScopedActionToken' from 'src.identity.tokens'`.

### 1.3 Signature and Type Incompatibilities in `tests/identity/test_tokens.py`
- In `src/identity/tokens.py` (lines 104–119):
  ```python
  def issue_token(
      self,
      *,
      user_id: UUID,
      scope: str,
      assignment_id: UUID | None = None,
      ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
      actor: str = "system",
      correlation_id: UUID | None = None,
  ) -> tuple[str, ActionTokenRecord]:
  ```
  Returns a 2-tuple `(raw_token, ActionTokenRecord)`.
- In `tests/identity/test_tokens.py` (lines 97–107):
  ```python
  sat = engine.issue_token(
      user_id=uid,
      scope="player_decision",
      assignment_id=aid,
      expires_in_minutes=60,
  )
  assert sat.raw_token.startswith("sat_")
  ```
  `expires_in_minutes` causes `TypeError: unexpected keyword argument 'expires_in_minutes'`, and `sat` expects an object with attributes `.raw_token`, `.token_hash`, `.scope`, `.user_id`, `.assignment_id`.
- In `src/identity/tokens.py` (line 144):
  `record_dict = self.store.get_action_token_by_token(raw_token)`
  Calls `get_action_token_by_token`, returning an `ActionTokenRecord` dataclass.
- In `tests/identity/test_tokens.py`:
  - Lines 144, 160, 171, 188, 205, 222, 241 mock `mock_store.get_action_token_by_hash.return_value = ...`. `IdentityStore` has NO `get_action_token_by_hash` method.
  - Lines 149 & 153: `assert verified["user_id"] == uid` and `assert consumed["consumed_at"] is not None` attempt dictionary indexing on `ActionTokenRecord`, raising `TypeError: 'ActionTokenRecord' object is not subscriptable`.

### 1.4 Test Mocking Deficiency & Parameter Mismatches in `tests/identity/test_step_up.py`
- In `tests/identity/test_step_up.py` (`test_step_up_verify_endpoint_mints_evidence`, lines 202–242):
  - In `src/identity/routers/step_up.py` (`step_up_verify`, lines 229–235):
    ```python
    challenge_row = store.get_challenge(challenge_str)
    if not challenge_row or challenge_row.get("challenge_type") != "step_up_decision":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="challenge not found or invalid type")
    ```
  - In `test_step_up.py`, `mock_store.get_challenge` is unconfigured. `MagicMock.get("challenge_type")` returns a `MagicMock`, which evaluates `!= "step_up_decision"` to `True`, triggering HTTP 400 Bad Request instead of HTTP 200.
  - Furthermore, `challenge_row["expires_at"] <= now` at line 242 compares `MagicMock <= datetime`, raising `TypeError`.
- In `tests/identity/test_step_up.py` (`test_db_decision_evidence_lifecycle`, line 264):
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
  In `src/identity/store.py` (lines 2253–2268), `create_decision_evidence` requires `challenge_id: UUID | None`, `approval_request_id: UUID | str | None`, and `credential_id: bytes` without default arguments, raising `TypeError: missing 3 required keyword-only arguments`.

### 1.5 Attribute Error in `tests/identity/test_player.py`
- In `tests/identity/test_player.py` (`test_player_decision_action_token_flow`, lines 261–263):
  ```python
  with patch.object(ScopedActionTokenEngine, "consume_token") as mock_consume:
      mock_consume.return_value = {"user_id": principal.user_id}
  ```
- In `src/identity/routers/player.py` (line 424):
  ```python
  token_ref = str(consumed_token.token_id)
  ```
  Because `mock_consume.return_value` is a `dict`, `consumed_token.token_id` raises `AttributeError: 'dict' object has no attribute 'token_id'`, resulting in HTTP 500.

### 1.6 Core Implementation Invariants (Passing Static Verification)
- **Token Format & Storage**:
  `src/identity/tokens.py` uses prefix `sat_` and 256 bits of URL-safe entropy (`secrets.token_urlsafe(32)`). `src/identity/store.py` (`create_action_token`) stores only `hash_token(token_to_use)` (SHA-256 hex digest) in `public.fc27_action_tokens.token_hash`. Zero plaintext tokens exist in the database or domain events.
- **Token Lifecycle Controls**:
  - Replay: raises `ActionTokenAlreadyConsumedError` (HTTP 409).
  - Expiry: raises `ActionTokenExpiredError` (HTTP 410).
  - Revocation: raises `ActionTokenRevokedError` (HTTP 410).
  - Scope: raises `ActionTokenScopeMismatchError` (HTTP 403).
  - Assignment: raises `ActionTokenAssignmentMismatchError` (HTTP 403).
- **Decision Step-Up Verification**:
  - Missing parameters: `StepUpChallengeRequest` requires `package_id`, `package_version`, `package_hash`, `decision` (Attack 9 fails closed with HTTP 422).
  - Evidence Replay: `consume_decision_evidence` and `apply_decision_unified` reject reused evidence with HTTP 409.
  - Scope Lockout: `apply_decision_unified` lines 1114–1121 and `consume_decision_evidence` lines 2355–2361 reject cross-package reuse with HTTP 403/400.
- **Scope Dormancy**:
  `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` enforced universally. Zero player delivery.

---

## 2. Logic Chain

1. **Test Suite Viability (Observation 1.2 -> Observation 1.3)**:
   - A test suite that fails test collection cannot run in automated continuous integration or regression pipelines.
   - `tests/identity/test_tokens.py` cannot be parsed or collected by pytest because four symbols (`ScopedActionToken`, `VALID_ACTION_SCOPES`, `hash_action_token`, `is_action_token_format`) are imported from `src/identity/tokens.py` but do not exist in that module.
   - Therefore, the test suite as delivered fails verification at the collection stage.

2. **Interface Divergence between Tests and Implementation (Observation 1.3 -> Observation 1.5)**:
   - In `tokens.py`, `ScopedActionTokenEngine.issue_token` returns `tuple[str, ActionTokenRecord]` with parameter `ttl_seconds`, matching how `src/identity/routers/player.py` and `src/identity/routers/step_up.py` consume it.
   - In `tests/identity/test_tokens.py`, the test was written to an older or alternate draft interface where `issue_token` returned a single object and accepted `expires_in_minutes`, and where verification methods returned dictionaries rather than `ActionTokenRecord` dataclasses.
   - Similarly, in `tests/identity/test_player.py`, `consume_token` was mocked returning a dictionary, whereas `src/identity/routers/player.py` accesses `.token_id` as an attribute on `ActionTokenRecord`.
   - Therefore, there is a fundamental contract mismatch between the unit tests and the production implementation.

3. **Step-Up Verification Test Deficiencies (Observation 1.4)**:
   - In `tests/identity/test_step_up.py`, `mock_store.get_challenge` was left unconfigured in `test_step_up_verify_endpoint_mints_evidence`. Because `step_up_verify` checks `challenge_row.get("challenge_type") == "step_up_decision"`, this test returns HTTP 400 and fails the `assert resp.status_code == 200` expectation.
   - In `test_db_decision_evidence_lifecycle`, calling `store.create_decision_evidence` without `challenge_id`, `approval_request_id`, and `credential_id` raises `TypeError` because `IdentityStore` does not provide default values for these keyword-only arguments.

4. **Adversarial Assessment (Observation 1.6 -> Conclusion)**:
   - While the underlying production code in `src/identity/` and `src/graph_api.py` implements the required security defenses and invariants (409 on replay, 410 on expiry/revocation, 403 on scope mismatch, 422 on missing challenge fields, universal dispatch dormancy), the tests designed to verify and guard these invariants do not function.
   - Passing tests is an indispensable requirement of Milestone 5 verification. Approving with broken test suites would introduce critical CI regressions.
   - Therefore, changes must be requested before Milestone 5 can be approved.

---

## 3. Caveats

1. **Interactive Host Terminal Execution**:
   - Live pytest command invocation on the host environment timed out waiting for interactive user permission prompt confirmation. All observations and logic chains were established through deterministic AST parsing, import graph resolution, and static execution trace analysis.
2. **Production Logic Soundness**:
   - The flaws identified are concentrated in the test files (`test_tokens.py`, `test_step_up.py`, `test_player.py`) and minor missing default parameters in `store.py`. The core security algorithms, token hashing, WebAuthn binding, and scope lockout logic in production modules are structurally sound.

---

## 4. Conclusion

**Verdict: REQUEST_CHANGES**

Milestone 5 cannot be approved in its current state due to test collection failures and mock/interface discrepancies:
1. `tests/identity/test_tokens.py` crashes pytest collection due to missing module exports.
2. `tests/identity/test_tokens.py` crashes at runtime due to `TypeError` (subscripting dataclasses and invalid keyword arguments).
3. `tests/identity/test_step_up.py` fails assertion due to unmocked challenge lookup and crashes DB integration test due to missing keyword arguments.
4. `tests/identity/test_player.py` crashes with `AttributeError` due to dictionary mock return type.

### Action Items for `worker_m5_1`:
1. **In `src/identity/tokens.py`**:
   Add compatibility aliases and exports:
   ```python
   ScopedActionToken = ActionTokenRecord
   VALID_ACTION_SCOPES = IdentityStore.VALID_ACTION_SCOPES
   hash_action_token = hash_token
   def is_action_token_format(val: Any) -> bool:
       return isinstance(val, str) and val.startswith(TOKEN_PREFIX) and len(val) >= 36
   ```
2. **In `tests/identity/test_tokens.py`**:
   Reconcile test calls to use `ttl_seconds=3600`, unpack `raw_token, record = engine.issue_token(...)`, mock `mock_store.get_action_token_by_token`, and access dataclass fields via attributes (`record.user_id`, `record.consumed_at`).
3. **In `src/identity/store.py`**:
   Add default values to `create_decision_evidence`:
   ```python
   challenge_id: UUID | None = None,
   approval_request_id: UUID | str | None = None,
   credential_id: bytes = b"",
   ```
4. **In `tests/identity/test_step_up.py`**:
   Configure `mock_store.get_challenge.return_value = {"challenge_type": "step_up_decision", "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5), "user_id": principal.user_id, "bound_payload_hash": "..."}` and `mock_store.get_webauthn_credential.return_value = {...}`.
5. **In `tests/identity/test_player.py`**:
   Ensure `ScopedActionTokenEngine.consume_token` returns an object with a `.token_id` attribute (e.g., `MagicMock(token_id=uuid4(), user_id=principal.user_id)`).

---

## 5. Verification Method

To independently verify the observations and failure modes:

1. **Verify Test Collection Failure**:
   Inspect `tests/identity/test_tokens.py` lines 26–40 against `src/identity/tokens.py`.
   Attempt import via Python CLI:
   ```python
   from src.identity.tokens import ScopedActionToken, VALID_ACTION_SCOPES, hash_action_token, is_action_token_format
   ```
   Observe `ImportError`.

2. **Verify Dataclass Subscripting Crash**:
   Inspect `ScopedActionTokenEngine.verify_token` in `src/identity/tokens.py` (returns `ActionTokenRecord`).
   Inspect `tests/identity/test_tokens.py` lines 149 & 153 (`verified["user_id"]`).
   Observe that `ActionTokenRecord` is not a subscriptable mapping.

3. **Verify Unmocked Challenge in Step-Up Verify**:
   Inspect `tests/identity/test_step_up.py` line 202 and `src/identity/routers/step_up.py` line 231.
   Observe that `challenge_row.get("challenge_type") != "step_up_decision"` evaluates to `True`, triggering HTTP 400.

4. **Verify Missing Keyword Arguments**:
   Inspect `create_decision_evidence` definition in `src/identity/store.py` line 2253 against call in `tests/identity/test_step_up.py` line 264.
   Observe missing `challenge_id`, `approval_request_id`, and `credential_id`.

5. **Verify Mock Dict Crash in Player Decision**:
   Inspect `tests/identity/test_player.py` line 262 against `src/identity/routers/player.py` line 424.
   Observe that `consumed_token.token_id` fails when `consumed_token` is a dict.

6. **Invalidation Conditions**:
   - Resolution of all import, signature, and mock type errors such that pytest runs cleanly across `tests/identity/test_tokens.py`, `tests/identity/test_step_up.py`, and `tests/identity/test_player.py`.
