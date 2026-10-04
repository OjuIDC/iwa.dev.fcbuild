# Phase 2 Milestone 5 Adversarial Challenge Report

**Author**: `challenger_m5_2` (Empirical Challenger)  
**Milestone**: Phase 2 Milestone 5 — Decision Step-Up, Player Decisions, Graph API Migration  
**Date**: 2026-10-04T09:55:00Z  
**Verdict**: **REQUEST_CHANGES**  

---

## Executive Summary

An adversarial empirical verification of Phase 2 Milestone 5 was conducted across the 5 required tracks:
1. Object-Level Player Isolation (`src/identity/routers/player.py`)
2. 5 Distinct Player Decision Actions (`src/identity/routers/player.py`)
3. Graph API HMAC Kill Switch (`src/graph_api.py`)
4. G-TIMEOUT Invariant (Feature 46) (`src/identity/policy.py`, `src/graph_api.py`)
5. Universal Dormancy & Scope Protection (`tests/test_packaging.py`, `tests/test_phase1_scope.py`, `docker-compose.yml`)

### Overall Assessment
- **Production Implementation Core (`src/`)**: **ROBUST & SOUND**. The business logic, isolation checks, policy authorization engine, HMAC kill switch, pre-flight route guards, G-TIMEOUT guards, and universal dormancy mechanisms correctly fail closed and enforce all security invariants.
- **Verification Test Suites (`tests/identity/`)**: **CRITICAL DEFECTS IDENTIFIED**. The unit test files delivered by `worker_m5_1` (`tests/identity/test_player.py`, `tests/identity/test_tokens.py`, `tests/identity/test_step_up.py`) were not executed by the worker (execution timed out during permission check) and contain multiple fatal errors (mock dictionary type mismatches, dataclass subscripting errors, missing parameters, unmocked attributes, and Pydantic validation assertion errors). If executed with `pytest`, multiple tests will fail immediately with `AttributeError`, `TypeError`, `KeyError`, and `AssertionError`.
- **Verdict**: **REQUEST_CHANGES** pending test suite remediation.

---

## Detailed Track Verification

### Track 1: Object-Level Player Isolation (`src/identity/routers/player.py`)

| Attack Vector | Target Endpoint / Code | Mechanism | Expected | Observed Behavior | Status |
|---|---|---|---|---|---|
| **Attack 1: Cross-Player Viewing** | `GET /assignments/{id}` (`get_player_assignment_detail`, line 577) | `check_player_isolation(principal, assignment)` | HTTP 403 `PlayerIsolationError` | Evaluates `principal.player_id == subject_player_id`. When Player A accesses Player B, raises `PlayerIsolationError(403)`. | **PASS** |
| **Attack 2: Cross-Player Challenge** | `POST /decision-challenge` (`generate_player_decision_challenge`, line 291) | `check_player_isolation(principal, assignment)` | HTTP 403 `PlayerIsolationError` | Evaluates player ID match against assignment subject. Raises `PlayerIsolationError(403)`. | **PASS** |
| **Attack 3: Cross-Player Decision** | `POST /decisions` (`execute_player_decision`, line 374) | `check_player_isolation(principal, assignment)` | HTTP 403 `PlayerIsolationError` | Evaluates player ID match against assignment subject. Raises `PlayerIsolationError(403)`. | **PASS** |
| **Attack 4: Staff Impersonation** | All player endpoints (`check_player_isolation`, lines 248-251) | `principal.is_player` check | HTTP 403 `PlayerIsolationError` | Non-players (coaches, analysts, staff, commissioner) attempting player actions raise `PlayerIsolationError(403)`. | **PASS** |

### Track 2: 5 Distinct Player Decision Actions (`src/identity/routers/player.py`)

| Attack Vector | Target Endpoint / Code | Mechanism | Expected | Observed Behavior | Status |
|---|---|---|---|---|---|
| **Attack 5: Conflation of `acknowledge` with `accept`** | `execute_player_decision` (lines 395-406) & `record_player_decision` (`store.py:2682`) | Proof differentiation & DB update | `acknowledge` records receipt without activating | `accept` enforces passkey/action-token proof. `acknowledge` does not require proof, records review receipt, and does NOT activate assignment or transition to active. | **PASS** |
| **Attack 6: Incomplete `change_request`** | `PlayerDecisionSubmissionRequest.validate_rules` (lines 140-145) | Pydantic model validator | HTTP 422 if empty notes/reason | When `decision == "change_request"`, empty notes/reason raises `ValueError`, resulting in FastAPI HTTP 422. | **PASS** |
| **Attack 7: Invalid `pause`** | `PlayerDecisionSubmissionRequest.validate_rules` (lines 147-149) | Pydantic model validator | HTTP 422 if duration < 1 | When `duration_days < 1`, raises `ValueError`, resulting in FastAPI HTTP 422. | **PASS** |

### Track 3: Graph API HMAC Kill Switch (`src/graph_api.py`)

| Attack Vector | Target Endpoint / Code | Mechanism | Expected | Observed Behavior | Status |
|---|---|---|---|---|---|
| **Attack 8: HMAC Submission with Kill Switch Active** | `apply_decision_unified` (line 1051) & `require_hmac` (line 829) | `settings.cli_hmac_enabled` check | HTTP 403 `proof_type_disabled` | `cli_hmac_enabled: bool = False` by default. Submitting `proof_type="hmac"` raises HTTP 403 `proof_type_disabled`. | **PASS** |
| **Attack 9: Pre-flight Unauthenticated Probe** | Decision endpoints (`check_decision_proof_type`, lines 909-918) | FastAPI route dependency | HTTP 403 before authentication | Route dependency `dependencies=[Depends(check_decision_proof_type)]` runs before `Depends(get_current_principal)`. Probes without WebAuthn evidence immediately return HTTP 403 `proof_type_disabled`. | **PASS** |

### Track 4: G-TIMEOUT Invariant (Feature 46)

| Attack Vector | Target Endpoint / Code | Mechanism | Expected | Observed Behavior | Status |
|---|---|---|---|---|---|
| **Attack 10: Inactivity / Timeout Approval Injection** | `src/identity/policy.py:649` & `src/identity/policy.py:658` | Action & attribute inspection | HTTP 403 `AgentImpersonationError` | Any action starting with `"timeout"` or attribute `timeout_approved: true` raises `AgentImpersonationError` (HTTP 403). | **PASS** |
| **Attack 11: Expired Request Decision** | `src/graph_api.py:1002` (`apply_decision_unified`) | Request status & expiry check | HTTP 409 `approval_expired` | Non-awaiting or expired requests reject with HTTP 409 `approval_expired`. | **PASS** |

### Track 5: Scope Dormancy & Packaging

| Attack Vector | Target Files | Mechanism | Expected | Observed Behavior | Status |
|---|---|---|---|---|---|
| **Attack 12: Dispatch Dormancy Invariant** | `docker-compose.yml`, `src/graph_api.py`, `tests/test_phase1_scope.py` | Universal env check | `DISPATCH_ENABLED=false`, zero player delivery | `DISPATCH_ENABLED=false` enforced in compose and settings. Zero player delivery adapters. | **PASS** |
| **Attack 13: Packaging & Dependency Pins** | `pyproject.toml`, `requirements.txt`, `tests/test_packaging.py` | Package inspection | Requirements match pyproject | `langgraph` pinned, test tools excluded from runtime dependencies. | **PASS** |

---

## Adversarial Vulnerability Findings (Test Suite Defects)

While production code enforces all security invariants, the newly authored test files will FAIL during test execution. The defects are documented below with root cause and remediation.

### Defect 1: Pydantic Validation Error Assertion Failure in `test_player.py`
- **Location**: `tests/identity/test_player.py:301-302`
- **Flawed Code**:
  ```python
  resp_cr_empty = client.post(
      f"/v1/player/assignments/{aid}/decision",
      json={"decision": "change_request", "notes": ""},
  )
  assert resp_cr_empty.status_code == 422
  assert "notes required" in resp_cr_empty.json()["detail"].lower()
  ```
- **Root Cause**:
  1. In FastAPI, `resp.json()["detail"]` for a validation error is a `list[dict]`, e.g., `[{'type': 'value_error', 'loc': ['body'], 'msg': 'Value error, change_request requires non-empty notes or reason', ...}]`. Calling `.lower()` on a `list` raises:
     ```
     AttributeError: 'list' object has no attribute 'lower'
     ```
  2. The string in `src/identity/routers/player.py:144` is `"change_request requires non-empty notes or reason"`. Even if converted to string, it does NOT contain `"notes required"`.
- **Remediation**:
  ```python
  assert resp_cr_empty.status_code == 422
  detail_str = str(resp_cr_empty.json()["detail"]).lower()
  assert "change_request requires non-empty notes or reason" in detail_str
  ```

### Defect 2: Mock Return Value Type Mismatch in `test_player.py` (`consume_token`)
- **Location**: `tests/identity/test_player.py:261-263`
- **Flawed Code**:
  ```python
  with patch.object(ScopedActionTokenEngine, "consume_token") as mock_consume:
      mock_consume.return_value = {"user_id": principal.user_id}
  ```
- **Root Cause**:
  In `src/identity/routers/player.py:424`, the code accesses:
  ```python
  token_ref = str(consumed_token.token_id)
  ```
  Because `mock_consume.return_value` is a dictionary, accessing `.token_id` raises:
  ```
  AttributeError: 'dict' object has no attribute 'token_id'
  ```
  This causes an unhandled 500 error instead of 200 OK.
- **Remediation**:
  Mock an object with a `token_id` attribute:
  ```python
  mock_token_record = MagicMock()
  mock_token_record.token_id = uuid4()
  mock_consume.return_value = mock_token_record
  ```

### Defect 3: Incomplete Mock Return Value in `test_player.py` (`record_player_decision`)
- **Location**: `tests/identity/test_player.py:255-259` and `324-328`
- **Flawed Code**:
  ```python
  mock_store.record_player_decision.return_value = {
      "decision_id": uuid4(),
      "decision": "accept",
      "status": "decided",
  }
  ```
- **Root Cause**:
  In `src/identity/routers/player.py:501-519`, `PlayerDecisionResponse` accesses:
  `decision_row["request_id"]`, `decision_row["assignment_id"]`, `decision_row["subject_player_id"]`, `decision_row["authority_basis"]`, `decision_row["assignment_version"]`, `decision_row["assignment_hash"]`, `decision_row["package_id"]`, `decision_row["package_version"]`, `decision_row["package_hash"]`, `decision_row["decided_at"]`.
  Missing these keys raises `KeyError: 'request_id'`.
- **Remediation**:
  Provide all required response fields in the mock dictionary or mock a full database row.

### Defect 4: Unexpected Parameter & Return Type in `test_tokens.py` (`issue_token`)
- **Location**: `tests/identity/test_tokens.py:96-107` and `273-280`
- **Flawed Code**:
  ```python
  sat = engine.issue_token(
      user_id=uid,
      scope="player_decision",
      assignment_id=aid,
      expires_in_minutes=60,
  )
  assert sat.raw_token.startswith("sat_")
  ```
- **Root Cause**:
  1. `ScopedActionTokenEngine.issue_token` in `src/identity/tokens.py:105` takes `ttl_seconds`, NOT `expires_in_minutes`. Passing `expires_in_minutes` raises:
     ```
     TypeError: ScopedActionTokenEngine.issue_token() got an unexpected keyword argument 'expires_in_minutes'
     ```
  2. `issue_token` returns `tuple[str, ActionTokenRecord]`. Accessing `sat.raw_token` on a tuple raises:
     ```
     AttributeError: 'tuple' object has no attribute 'raw_token'
     ```
- **Remediation**:
  ```python
  raw_token, token_record = engine.issue_token(
      user_id=uid,
      scope="player_decision",
      assignment_id=aid,
      ttl_seconds=3600,
  )
  assert raw_token.startswith("sat_")
  ```

### Defect 5: Dataclass Subscripting in `test_tokens.py` (`verify_token` & `consume_token`)
- **Location**: `tests/identity/test_tokens.py:148-154`
- **Flawed Code**:
  ```python
  verified = engine.verify_token(...)
  assert verified["user_id"] == uid

  consumed = engine.consume_token(...)
  assert consumed["consumed_at"] is not None
  ```
- **Root Cause**:
  `verify_token` and `consume_token` return `ActionTokenRecord` instances (a frozen dataclass). Attempting dictionary subscripting raises:
  ```
  TypeError: 'ActionTokenRecord' object is not subscriptable
  ```
- **Remediation**:
  Access dataclass fields via attributes:
  ```python
  assert verified.user_id == uid
  assert consumed.consumed_at is not None
  ```

### Defect 6: Missing Store Challenge Mock in `test_step_up.py`
- **Location**: `tests/identity/test_step_up.py:202-243`
- **Flawed Code**:
  `mock_store.get_challenge` is not configured.
- **Root Cause**:
  In `src/identity/routers/step_up.py:230-231`:
  ```python
  challenge_row = store.get_challenge(challenge_str)
  if not challenge_row or challenge_row.get("challenge_type") != "step_up_decision":
      raise HTTPException(status_code=400, detail="challenge not found or invalid type")
  ```
  An unmocked `store.get_challenge` returns a `MagicMock`. Comparing `MagicMock() != "step_up_decision"` evaluates to `True`, raising HTTP 400.
  The test asserts HTTP 200, but gets HTTP 400.
- **Remediation**:
  Configure `mock_store.get_challenge.return_value = {"challenge_type": "step_up_decision", "consumed_at": None, "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5), "user_id": principal.user_id, "bound_payload_hash": "..."}`.

---

## Conclusion & Verdict

Because Milestone 5 cannot pass automated testing until these test suite defects are resolved, the required verdict is:

**REQUEST_CHANGES**

The worker `worker_m5_1` should apply the specific remediations outlined above to `tests/identity/test_player.py`, `tests/identity/test_tokens.py`, and `tests/identity/test_step_up.py`. Production code in `src/` requires no modifications.
