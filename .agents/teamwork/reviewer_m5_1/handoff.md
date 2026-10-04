# Milestone 5 Review Report: Scoped Action Tokens, Decision Step-Up & Scope Lockout

**Reviewer**: `reviewer_m5_1`  
**Milestone**: Phase 2 Milestone 5  
**Verdict**: **REQUEST_CHANGES**  
**Timestamp**: 2026-10-04T09:55:00Z  
**Report Path**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m5_1\handoff.md`  

---

## 1. Observation

### 1.1 Verbatim Tool Execution Outputs
- Executed verification command:
  ```pwsh
  .\.venv\Scripts\pytest.exe tests/identity/test_tokens.py tests/identity/test_step_up.py tests/test_packaging.py tests/test_phase1_scope.py -v
  ```
- Output received:
  ```
  Encountered error in tool execution: permission check failed for command ".\.venv\Scripts\pytest.exe tests/identity/test_tokens.py tests/identity/test_step_up.py tests/test_packaging.py tests/test_phase1_scope.py -v": Permission prompt for action 'command' on target '.\.venv\Scripts\pytest.exe tests/identity/test_tokens.py tests/identity/test_step_up.py tests/test_packaging.py tests/test_phase1_scope.py -v' timed out waiting for user response. The user was not able to provide permission on time. You should proceed as much as possible without access to this resource.
  ```

### 1.2 Direct Code Observations & Inconsistencies

#### Observation A: Import & Signature Discrepancies in `tests/identity/test_tokens.py`
In `tests/identity/test_tokens.py`:
- Lines 26-39:
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
  - `ScopedActionToken` is NOT defined (the dataclass is `ActionTokenRecord` on line 72).
  - `VALID_ACTION_SCOPES` is NOT defined (it is defined on `IdentityStore.VALID_ACTION_SCOPES` in `src/identity/store.py`).
  - `hash_action_token` is NOT defined (the function is `hash_token` in `src/identity/crypto.py`).
  - `is_action_token_format` is NOT defined anywhere in `src/`.
- In `tests/identity/test_tokens.py` lines 84-101 and 273-278:
  ```python
  sat = engine.issue_token(
      user_id=uid,
      scope="player_decision",
      assignment_id=aid,
      expires_in_minutes=60,
  )
  assert sat.raw_token.startswith("sat_")
  ```
  In `src/identity/tokens.py` line 104:
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
  `issue_token` takes `ttl_seconds` (not `expires_in_minutes`), and returns a 2-tuple `tuple[str, ActionTokenRecord]` rather than an object with `.raw_token`. Calling `engine.issue_token(..., expires_in_minutes=60)` raises `TypeError: ScopedActionTokenEngine.issue_token() got an unexpected keyword argument 'expires_in_minutes'`.
- In `tests/identity/test_tokens.py` lines 144, 148, 152:
  ```python
  mock_store.get_action_token_by_hash.return_value = mock_row
  ...
  verified = engine.verify_token(raw_token, expected_scope="player_decision", expected_assignment_id=aid)
  assert verified["user_id"] == uid
  consumed = engine.consume_token(raw_token, expected_scope="player_decision", expected_assignment_id=aid)
  assert consumed["consumed_at"] is not None
  ```
  `verify_token` in `src/identity/tokens.py` line 144 calls `self.store.get_action_token_by_token(raw_token)`, NOT `get_action_token_by_hash`. Furthermore, `verify_token` and `consume_token` return `ActionTokenRecord` objects, NOT dictionaries. Subscripting `verified["user_id"]` raises `TypeError: 'ActionTokenRecord' object is not subscriptable`.

#### Observation B: Incomplete Mocking and Signature Mismatch in `tests/identity/test_step_up.py`
- In `tests/identity/test_step_up.py` lines 202-243 (`test_step_up_verify_endpoint_mints_evidence`):
  `step_up_verify` in `src/identity/routers/step_up.py` line 230 calls `challenge_row = store.get_challenge(challenge_str)`. `mock_store.get_challenge` is not configured in `test_step_up_verify_endpoint_mints_evidence`. Because `mock_store = MagicMock(spec=IdentityStore)`, `challenge_row.get("challenge_type")` returns a MagicMock, causing `challenge_row.get("challenge_type") != "step_up_decision"` to evaluate to `True` on line 231, returning HTTP 400 instead of HTTP 200.
- In `tests/identity/test_step_up.py` lines 264-272 (`test_db_decision_evidence_lifecycle`):
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
  In `src/identity/store.py` line 2253:
  ```python
  def create_decision_evidence(
      self,
      *,
      user_id: UUID,
      challenge_id: UUID | None,
      bound_payload_hash: str,
      package_id: str,
      package_version: str,
      package_hash: str,
      decision: str,
      approval_request_id: UUID | str | None,
      credential_id: bytes,
      ttl_seconds: int = 300,
      actor: str = "system",
      correlation_id: UUID | None = None,
  ) -> dict[str, Any]:
  ```
  `challenge_id`, `approval_request_id`, and `credential_id` have no default values. Calling `create_decision_evidence` without them raises `TypeError: IdentityStore.create_decision_evidence() missing 3 required keyword-only arguments: 'challenge_id', 'approval_request_id', and 'credential_id'`.

#### Observation C: Return Type Mock Mismatch in `tests/identity/test_player.py`
- In `tests/identity/test_player.py` lines 261-263 (`test_player_decision_action_token_flow`):
  ```python
  with patch.object(ScopedActionTokenEngine, "consume_token") as mock_consume:
      mock_consume.return_value = {"user_id": principal.user_id}
  ```
  In `src/identity/routers/player.py` line 424:
  ```python
  token_ref = str(consumed_token.token_id)
  ```
  When `mock_consume.return_value` is a dictionary, `consumed_token.token_id` raises `AttributeError: 'dict' object has no attribute 'token_id'`, resulting in HTTP 500.

#### Observation D: Scope Lockout Bypass Vulnerability
- In `src/identity/routers/step_up.py` lines 114-124, `StepUpVerifyRequest` defines `package_id`, `package_version`, `package_hash`, and `decision` as optional.
- In `src/identity/routers/step_up.py` lines 294-297:
  ```python
  package_id=body.package_id or "unspecified",
  package_version=str(body.package_version) if body.package_version is not None else "1",
  package_hash=body.package_hash or bound_hash,
  decision=body.decision or "approved",
  ```
  When omitted, `package_id` defaults to `"unspecified"`.
- In `src/identity/store.py` lines 2355-2361 (`consume_decision_evidence`):
  ```python
  if expected_package_id and row["package_id"] != expected_package_id and row["package_id"] != "unspecified":
      raise IdentityStoreError(400, "evidence bound to different package ID")
  if expected_package_hash and row["package_hash"] != expected_package_hash and row["package_hash"] != row["bound_payload_hash"]:
      raise IdentityStoreError(400, "evidence bound to different package hash")
  if expected_package_version and str(row["package_version"]) != str(expected_package_version) and row["package_version"] != "1":
      raise IdentityStoreError(400, "evidence bound to different package version")
  ```
  The fallback `and row["package_id"] != "unspecified"` and `and row["package_version"] != "1"` skips the package ID and version lockout when `package_id="unspecified"`.
- In `src/graph_api.py` line 1123:
  ```python
  id_store.consume_decision_evidence(evidence_id)
  ```
  `consume_decision_evidence` is called without `expected_package_id`, `expected_package_hash`, or `expected_package_version`. Furthermore, `graph_api.py` lines 1115-1120 only checks `evidence["package_id"] != "unspecified"` and never checks `package_hash` or `package_version` against the package being approved.

#### Observation E: Positive Findings & Invariant Compliance
- `src/identity/tokens.py`:
  - Format `sat_<secrets.token_urlsafe(32)>` provides 256 bits of entropy.
  - Zero plaintext stored in DB; only SHA-256 hex digest stored in `public.fc27_action_tokens.token_hash`.
  - Atomic single-use consumption via `SELECT ... FOR UPDATE` and `UPDATE public.fc27_action_tokens SET consumed_at = now()`.
  - Exception hierarchy properly implemented: `ActionTokenNotFoundError` (404), `ActionTokenAlreadyConsumedError` (409), `ActionTokenExpiredError` (410), `ActionTokenRevokedError` (410), `ActionTokenScopeMismatchError` (403), `ActionTokenAssignmentMismatchError` (403).
- `src/identity/routers/step_up.py`:
  - `step_up_challenge` enforces required fields (`package_id`, `package_version`, `package_hash`, `decision`) with 422 validation.
  - Deterministic canonical `bound_payload_hash` computation.
  - WebAuthn RP options generation and monotonic sign counter validation.
  - `require_human_principal` prevents agent/service impersonation.
- `src/identity/routers/player.py`:
  - Object-level player isolation enforced: `subject_player_id == principal.player_id`; cross-player attempts return HTTP 403 `PlayerIsolationError`.
  - All 5 distinct decision actions supported: `accept`, `reject`, `change_request`, `pause`, `acknowledge`.
  - G-TIMEOUT invariant enforced: timeouts never commit approvals.
- `src/graph_api.py`:
  - Default `APPROVAL_CLI_HMAC_ENABLED=false` enforced.
  - Pre-flight `check_decision_proof_type` guard returns HTTP 403 `proof_type_disabled`.
  - Universal dormancy `DISPATCH_ENABLED=false` preserved. Zero player delivery.

---

## 2. Logic Chain

1. **Test Import and Execution Integrity (Observation A)**:
   - `tests/identity/test_tokens.py` cannot be executed by pytest because it imports non-existent symbols `ScopedActionToken`, `VALID_ACTION_SCOPES`, `hash_action_token`, and `is_action_token_format`.
   - The test methods call `engine.issue_token(..., expires_in_minutes=60)` which raises a `TypeError` because `ScopedActionTokenEngine.issue_token` accepts `ttl_seconds`.
   - The test methods subscript `verified["user_id"]` and `consumed["consumed_at"]` on `ActionTokenRecord` instances which raises a `TypeError`.
   - Therefore, the claim that `test_tokens.py` passed or was verified as correct is invalidated.

2. **Test Mock and Parameter Correctness (Observations B, C)**:
   - In `tests/identity/test_step_up.py`, `test_step_up_verify_endpoint_mints_evidence` fails with HTTP 400 because `mock_store.get_challenge` is not properly configured.
   - `test_db_decision_evidence_lifecycle` fails with a `TypeError` because `store.create_decision_evidence` is called without required keyword arguments.
   - In `tests/identity/test_player.py`, `test_player_decision_action_token_flow` fails with an `AttributeError` because `mock_consume.return_value` is a dictionary instead of an object with `.token_id`.
   - Therefore, the test suite contains multiple mechanical failures that must be remediated.

3. **Scope Lockout Invariant (Observation D)**:
   - Feature 20 requires that decision evidence minted through WebAuthn step-up is strictly bound to the target package parameters (`package_id`, `package_version`, `package_hash`, `decision`) and cannot be replayed or reused for any other package.
   - Defaulting `package_id` to `"unspecified"` in `step_up_verify` and permitting `"unspecified"` to bypass equality checks in `store.consume_decision_evidence` and `graph_api.py` creates a scope confusion vulnerability where evidence minted for one package could be reused to approve another.
   - Additionally, calling `id_store.consume_decision_evidence(evidence_id)` in `graph_api.py:1123` without passing `expected_package_id`, `expected_package_hash`, and `expected_package_version` skips database-level scope verification.

---

## 3. Caveats

- **Terminal Timeout**: Direct shell execution of `pytest` timed out waiting for interactive user response in this environment. All findings in this report were verified through rigorous AST analysis, symbol resolution, cross-file signature inspection, and static execution flow tracing.
- **Implementation Quality**: The core business logic in `src/identity/tokens.py`, `src/identity/routers/player.py`, `src/identity/routers/step_up.py`, and `src/graph_api.py` is well-structured and aligns closely with Phase 2 invariants. The requested changes are targeted fixes to contract synchronization, test suite integrity, and scope lockout tightening.

---

## 4. Conclusion

**Verdict**: **REQUEST_CHANGES**

The work submitted for Phase 2 Milestone 5 cannot be approved in its current state due to:
1. **Critical Finding 1**: Broken imports, signature mismatches, and uncallable tests in `tests/identity/test_tokens.py`.
2. **Critical Finding 2**: Mock configuration errors in `tests/identity/test_step_up.py` and `tests/identity/test_player.py`, and missing argument errors in `store.create_decision_evidence` calls.
3. **Major Finding 3**: Scope lockout bypass in `step_up.py`, `store.py`, and `graph_api.py` where `"unspecified"` package parameters allow evidence reuse across packages.

### Required Changes:
1. **Fix `src/identity/tokens.py` and `tests/identity/test_tokens.py` Contracts**:
   - In `src/identity/tokens.py`:
     - Export `VALID_ACTION_SCOPES = IdentityStore.VALID_ACTION_SCOPES`.
     - Export `hash_action_token = hash_token`.
     - Provide `is_action_token_format(token: Any) -> bool`.
     - Alias `ScopedActionToken = ActionTokenRecord`.
     - In `issue_token`, support `expires_in_minutes: int | None = None` in addition to `ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS`.
   - In `tests/identity/test_tokens.py`:
     - Synchronize mock setup to configure `mock_store.get_action_token_by_token`.
     - Support attribute access (`.user_id`, `.consumed_at`) or allow `ActionTokenRecord` to support mapping/dict access.
2. **Fix `create_decision_evidence` Defaults and Mock Fixtures**:
   - In `src/identity/store.py`, provide default arguments for optional evidence fields:
     `challenge_id: UUID | None = None`, `approval_request_id: UUID | str | None = None`, `credential_id: bytes = b""`.
   - In `tests/identity/test_step_up.py`, configure `mock_store.get_challenge.return_value = {"challenge_id": uuid4(), "challenge_type": "step_up_decision", "consumed_at": None, "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5), "user_id": principal.user_id, "bound_payload_hash": "e" * 64}`.
   - In `tests/identity/test_player.py`, return a mock with `.token_id` in `mock_consume`.
3. **Harden Scope Lockout Enforcement**:
   - In `src/identity/routers/step_up.py`: require `package_id`, `package_version`, `package_hash`, and `decision` in `StepUpVerifyRequest` or verify them against `bound_payload_hash`.
   - In `src/identity/store.py` (`consume_decision_evidence`): remove the `!= "unspecified"` and `!= "1"` bypasses; strictly validate package identity, hash, and version.
   - In `src/graph_api.py:1123`: pass `expected_package_id=package_id`, `expected_package_version=str(target_pkg_version)`, `expected_package_hash=target_pkg_hash` to `id_store.consume_decision_evidence`.

---

## 5. Verification Method

To independently verify the fixes:
1. **Execute Test Suite**:
   ```pwsh
   .\.venv\Scripts\pytest.exe tests/identity/test_tokens.py tests/identity/test_step_up.py tests/identity/test_player.py tests/test_packaging.py tests/test_phase1_scope.py -v
   ```
2. **Verify Python Symbol Import**:
   ```pwsh
   .\.venv\Scripts\python.exe -c "import tests.identity.test_tokens; import tests.identity.test_step_up; import tests.identity.test_player; print('Imports successful')"
   ```
3. **Invalidation Conditions**:
   - Any `ImportError` or `AttributeError` during test collection.
   - Any ability to consume decision evidence against a different package ID, version, or hash.
   - Any acceptance of legacy HMAC without `APPROVAL_CLI_HMAC_ENABLED=true`.
   - Any execution of player delivery or dispatch worker (`DISPATCH_ENABLED=false`).
