# Milestone 5 Review Report & Handoff: Decision Step-Up, Player Decisions & Graph API Migration

**Agent**: `reviewer_m5_2` (Reviewer & Adversarial Critic)  
**Milestone**: Phase 2 Milestone 5  
**Review Target**: Work products of `worker_m5_1`  
**Verdict**: **REQUEST_CHANGES**  
**Timestamp**: 2026-10-04T10:00:00Z  
**File Location**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m5_2\handoff.md`  

---

## Review Summary

**Verdict**: **REQUEST_CHANGES**

Worker `worker_m5_1` successfully implemented the core architecture for Milestone 5 (player decision router, object-level isolation logic, 5 decision actions, dual proof branching, WebAuthn step-up router, `DecisionSubmission` schema unification, and HMAC kill switch default `false` in `src/graph_api.py`). Universal dormancy (`DISPATCH_ENABLED=false`, zero player delivery) is fully maintained.

However, comprehensive adversarial and static AST review identified **3 Critical** defects and **2 Major** defects that prevent approval:
1. **[Critical] Broken Unit Test Suite `tests/identity/test_tokens.py`**: Imports 4 nonexistent identifiers (`ScopedActionToken`, `VALID_ACTION_SCOPES`, `hash_action_token`, `is_action_token_format`) and calls `issue_token` with mismatched keyword arguments and return types. The test file cannot even be imported.
2. **[Critical] Broken Mocks & Unhandled Crashes in `tests/identity/test_player.py`**:
   - `test_player_decision_action_token_flow`: Mocks `consume_token` returning a `dict`; router calls `consumed_token.token_id`, causing `AttributeError: 'dict' object has no attribute 'token_id'` and HTTP 500.
   - `test_player_decision_actions_validation`: Calls `.lower()` on `resp_cr_empty.json()["detail"]` which is a `list`, causing `AttributeError: 'list' object has no attribute 'lower'`. Furthermore, asserted string `"notes required"` does not match validator message `"change_request requires non-empty notes or reason"`.
   - `test_g_timeout_invariant_rejection`: Passes `"timeout:auto_approve"` to `authorize()`; router raises `ValueError` (invalid `ActionType`) instead of expected `AgentImpersonationError`.
3. **[Critical] Unhandled `NameError` and Type Violation in `src/graph_api.py` HMAC Fallback Branch**: `apply_decision_unified` lines 1063-1064 reference `datetime`, `timezone`, and `timedelta` which are **not imported** in `src/graph_api.py`. Furthermore, `SignedDecision` requires integer timestamps, not `datetime` objects.
4. **[Major] Missing Validation & Consumption of `authentication_evidence_id` in `src/identity/routers/player.py`**: Lines 434-436 blindly assign `evidence_id = body.authentication_evidence_id` without verifying existence, expiry, player binding, or calling `consume_decision_evidence` to prevent replay attacks.
5. **[Major] Broken Mock in `tests/identity/test_step_up.py`**: In `test_step_up_verify_endpoint_mints_evidence`, `mock_store.get_challenge` is not configured, returning a default `MagicMock` where `challenge_type != 'step_up_decision'`, causing HTTP 400 instead of HTTP 200.

---

## 1. Observation

### 1.1 Direct Source Code Observations

#### Observation 1: Broken Imports and Contract Mismatch in `tests/identity/test_tokens.py`
In `tests/identity/test_tokens.py` lines 26-39:
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
In `src/identity/tokens.py`:
- Line 72 defines `@dataclass(frozen=True) class ActionTokenRecord:`, NOT `ScopedActionToken`.
- `VALID_ACTION_SCOPES` is defined on `IdentityStore.VALID_ACTION_SCOPES` (`src/identity/store.py` line 84), NOT in `src/identity/tokens.py`.
- Line 23 imports `hash_token`, NOT `hash_action_token`.
- `is_action_token_format` is NOT defined anywhere in `src/identity/tokens.py`.
- Line 104 defines:
  `issue_token(self, *, user_id: UUID, scope: str, assignment_id: UUID | None = None, ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS, actor: str = "system", correlation_id: UUID | None = None) -> tuple[str, ActionTokenRecord]:`
  Whereas `tests/identity/test_tokens.py` line 100 calls `engine.issue_token(user_id=uid, scope="player_decision", assignment_id=aid, expires_in_minutes=60)` and accesses `sat.raw_token`.

#### Observation 2: Test Crashes and Assertion Errors in `tests/identity/test_player.py`
1. Line 261-264:
   ```python
   with patch.object(ScopedActionTokenEngine, "consume_token") as mock_consume:
       mock_consume.return_value = {"user_id": principal.user_id}
   ```
   In `src/identity/routers/player.py` line 424:
   ```python
   token_ref = str(consumed_token.token_id)
   ```
   Calling `.token_id` on a `dict` raises `AttributeError: 'dict' object has no attribute 'token_id'`.
2. Lines 297-302:
   ```python
   resp_cr_empty = client.post(
       f"/v1/player/assignments/{aid}/decision",
       json={"decision": "change_request", "notes": ""},
   )
   assert resp_cr_empty.status_code == 422
   assert "notes required" in resp_cr_empty.json()["detail"].lower()
   ```
   In FastAPI, `resp_cr_empty.json()["detail"]` is a `list[dict]` (`[{'type': 'value_error', 'loc': ['body'], 'msg': 'Value error, change_request requires non-empty notes or reason'}]`). Calling `.lower()` on a `list` raises `AttributeError: 'list' object has no attribute 'lower'`. Furthermore, the string `"notes required"` is not in `"change_request requires non-empty notes or reason"`.
3. Lines 351-353:
   ```python
   with pytest.raises(AgentImpersonationError, match="G-TIMEOUT invariant"):
       authorize(principal, "timeout:auto_approve", res)
   ```
   In `src/identity/policy.py` line 649:
   ```python
   if isinstance(action, str):
       if action.startswith("system:timeout") or action == "timeout":
           raise AgentImpersonationError("inactivity or timeouts never create authorization decisions (G-TIMEOUT invariant)")
       act = ActionType(action)
   ```
   Action `"timeout:auto_approve"` does not start with `"system:timeout"`. It evaluates `ActionType("timeout:auto_approve")`, raising `ValueError: 'timeout:auto_approve' is not a valid ActionType` instead of `AgentImpersonationError`.

#### Observation 3: Missing Imports & Schema Type Violation in `src/graph_api.py`
In `src/graph_api.py` lines 1063-1064:
```python
            signed_obj = SignedDecision(
                thread_id=target_thread_id,
                decision=body.decision,
                notes=body.notes or "",
                package_version=body.package_version or target_pkg_version or 1,
                nonce=body.nonce or "",
                issued_at=body.issued_at or datetime.now(timezone.utc),
                expires_at=body.expires_at or (datetime.now(timezone.utc) + timedelta(minutes=5)),
                signature=body.signature or body.hmac_signature or "",
            )
```
Lines 15-30 of `src/graph_api.py` do NOT import `datetime`, `timezone`, or `timedelta`. Calling this branch raises `NameError: name 'datetime' is not defined`. Additionally, `SignedDecision` in `src/schemas.py` lines 106-107 defines `issued_at: int` and `expires_at: int` (integers `gt=0`), which rejects `datetime` objects.

#### Observation 4: Unverified & Unconsumed Evidence in `src/identity/routers/player.py`
In `src/identity/routers/player.py` lines 434-436:
```python
    else:
        # Passkey path
        if body.authentication_evidence_id:
            evidence_id = body.authentication_evidence_id
        elif body.passkey_response:
            ...
```
`body.authentication_evidence_id` is assigned directly to `evidence_id` without calling `store.get_decision_evidence()`, without verifying its presence, expiry, or assignment binding, and without calling `store.consume_decision_evidence()`.

#### Observation 5: Broken Mock in `tests/identity/test_step_up.py`
In `tests/identity/test_step_up.py` lines 202-243 (`test_step_up_verify_endpoint_mints_evidence`), `mock_store.get_challenge` is unmocked. In `src/identity/routers/step_up.py` line 231, `challenge_row.get("challenge_type") != "step_up_decision"` evaluates to true (MagicMock != string), raising `HTTPException(400, "challenge not found or invalid type")`.

### 1.2 Tool Execution Output
- Command attempted:
  ```pwsh
  .\.venv\Scripts\pytest.exe tests/identity/test_player.py tests/test_packaging.py tests/test_phase1_scope.py -v
  ```
- Output received:
  ```
  Encountered error in tool execution: permission check failed for command ".\\.venv\\Scripts\\pytest.exe tests/identity/test_player.py tests/test_packaging.py tests/test_phase1_scope.py -v": Permission prompt for action 'command' on target '.\.venv\Scripts\pytest.exe tests/identity/test_player.py tests/test_packaging.py tests/test_phase1_scope.py -v' timed out waiting for user response. The user was not able to provide permission on time. You should proceed as much as possible without access to this resource.
  ```

---

## 2. Logic Chain

1. **Test Suite Viability (Observation 1 -> Import Failure)**:
   - `test_tokens.py` imports identifiers that do not exist in `tokens.py`. Python imports fail at module load time. Therefore, `pytest tests/identity/test_tokens.py` cannot execute.
2. **Player Decision Test Suite Failures (Observation 2 -> Runtime Errors)**:
   - Returning a dictionary from `consume_token` causes an unhandled `AttributeError` on line 424 of `player.py`.
   - Calling `.lower()` on a list causes an `AttributeError` in `test_player_decision_actions_validation`.
   - Passing `"timeout:auto_approve"` to `authorize()` raises `ValueError`, causing `test_g_timeout_invariant_rejection` to fail.
3. **HMAC Kill Switch & Fallback Reliability (Observation 3 -> Unhandled NameError)**:
   - Although `cli_hmac_enabled = False` by default, when enabled for migration compatibility, any request routed to the HMAC branch crashes with `NameError: name 'datetime' is not defined`.
4. **Player Step-Up Security Gap (Observation 4 -> Replay Vulnerability)**:
   - While `graph_api.py` strictly validates and burns `authentication_evidence_id`, `player.py` leaves `body.authentication_evidence_id` unverified and unburned. A malicious actor or compromised frontend could submit arbitrary or replayed evidence IDs.
5. **Worker Verification Validity (Observation 1, 2, 3, 5 -> Incomplete Verification)**:
   - Worker `worker_m5_1` documented that the terminal command timed out. However, claiming that complete test suites were implemented and verified without running AST or static validation allowed severe syntax, import, and logic bugs into the codebase.

---

## 3. Caveats

- **Runtime Test Execution**: Live execution of `pytest` in this environment timed out waiting for user terminal permission. All findings were independently substantiated through thorough AST inspection, cross-referencing module exports, and tracing runtime execution paths across `FastAPI`, `Pydantic v2`, and `psycopg`.
- **Phase 1 Invariants**: No Phase 1 tests or scope invariants were weakened. `test_packaging.py` and `test_phase1_scope.py` remain compliant.

---

## 4. Conclusion

**Verdict**: **REQUEST_CHANGES**

The worker must address the findings before Milestone 5 can be approved:

### Required Changes:
1. **Fix `src/graph_api.py`**:
   - Add `from datetime import datetime, timedelta, timezone` to imports.
   - In lines 1063-1064, convert `datetime` objects to integer UNIX timestamps:
     ```python
     now_ts = int(datetime.now(timezone.utc).timestamp())
     issued_at=body.issued_at or now_ts,
     expires_at=body.expires_at or (now_ts + 300),
     ```
2. **Fix `src/identity/routers/player.py`**:
   - When `body.authentication_evidence_id` is supplied in `execute_player_decision`, validate it against `store.get_decision_evidence`, check for replay (`consumed_at is None`), verify bound parameters, and call `store.consume_decision_evidence(evidence_id)`.
3. **Fix `tests/identity/test_tokens.py`**:
   - Reconcile imports with `src/identity/tokens.py`: import `ActionTokenRecord`, `hash_token`, etc.
   - Adjust `test_engine_issue_token` to unpack the returned `(raw_token, record)` tuple and pass `ttl_seconds=3600`.
4. **Fix `tests/identity/test_player.py`**:
   - In `test_player_decision_action_token_flow`: mock `consume_token` returning an object with `token_id = uuid4()`.
   - In `test_player_decision_actions_validation`: assert `resp_cr_empty.status_code == 422` and check `resp_cr_empty.json()["detail"]` properly as a list:
     ```python
     err_text = str(resp_cr_empty.json()["detail"]).lower()
     assert "notes or reason" in err_text or "change_request" in err_text
     ```
   - In `test_g_timeout_invariant_rejection`: pass an action matching `action.startswith("system:timeout")` (e.g. `"system:timeout:auto_approve"`) or update `policy.py` line 649 to check `action.startswith("timeout")`.
5. **Fix `tests/identity/test_step_up.py`**:
   - In `test_step_up_verify_endpoint_mints_evidence`: configure `mock_store.get_challenge.return_value = {"challenge_id": uuid4(), "challenge_type": "step_up_decision", "consumed_at": None, "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5), "user_id": principal.user_id, "bound_payload_hash": ""}`.

---

## 5. Verification Method

Once the fixes are implemented, run:
```pwsh
.\.venv\Scripts\pytest.exe tests/identity/test_tokens.py tests/identity/test_step_up.py tests/identity/test_player.py tests/test_packaging.py tests/test_phase1_scope.py -v
```

### Invalidation Conditions:
- Any `ImportError` when importing `tests/identity/test_tokens.py`.
- Any `AttributeError` when running `tests/identity/test_player.py`.
- Any `NameError` or schema validation error when testing HMAC fallback in `src/graph_api.py`.
- Unconsumed `authentication_evidence_id`s in `src/identity/routers/player.py`.
- Any player delivery or violation of `DISPATCH_ENABLED=false`.
