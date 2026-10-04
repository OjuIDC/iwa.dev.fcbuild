# Milestone 5 Adversarial Challenger Report: Object-Level Player Isolation, 5 Decision Actions, HMAC Kill Switch & G-TIMEOUT Invariant

**Agent**: `challenger_m5_2` (Empirical Challenger)  
**Milestone**: Phase 2 Milestone 5  
**Verdict**: **REQUEST_CHANGES**  
**Timestamp**: 2026-10-04T09:58:00Z  
**File Location**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m5_2\handoff.md`  

---

## 1. Observation

### 1.1 Direct Observations of Invariants & Implementation Files
1. **Object-Level Player Isolation (`src/identity/routers/player.py`)**:
   - Lines 232-252 (`check_player_isolation`):
     ```python
     def check_player_isolation(principal: PrincipalContext, assignment: dict[str, Any]) -> None:
         subject_player_id = str(assignment["subject_player_id"])
         if principal.is_player:
             if not principal.player_id or str(principal.player_id) != subject_player_id:
                 raise PlayerIsolationError(
                     f"player isolation violation: player {principal.player_id} cannot access assignment for {subject_player_id}"
                 )
             return
         raise PlayerIsolationError(
             f"role not permitted to act as player: only player {subject_player_id} may perform this action"
         )
     ```
   - Invoked across `generate_player_decision_challenge` (line 291), `execute_player_decision` (line 374), and `get_player_assignment_detail` (line 577). `list_player_assignments` (line 545) enforces `player_id=principal.player_id`.

2. **5 Distinct Player Decision Actions (`src/identity/routers/player.py`)**:
   - `DecisionChoice`: `accept`, `reject`, `change_request`, `pause`, `acknowledge`.
   - `PlayerDecisionSubmissionRequest.validate_rules` (lines 140-149):
     ```python
     if self.decision == DecisionChoice.CHANGE_REQUEST:
         combined = (self.notes or "") + (self.reason or "")
         if not combined.strip():
             raise ValueError("change_request requires non-empty notes or reason")
     if self.duration_days is not None and self.duration_days < 1:
         raise ValueError("duration_days must be >= 1")
     ```
   - In `execute_player_decision` (lines 395-406): `accept` requires passkey evidence or action token. `acknowledge` does not require step-up proof.
   - In `src/identity/store.py:2682-2686`: `pause` transitions assignment status to `'paused'`, while `acknowledge` only marks the decision request `'decided'` and commits domain audit event without activating or altering assignment state.

3. **Graph API HMAC Kill Switch & Pre-flight Guard (`src/graph_api.py`)**:
   - Line 107: `cli_hmac_enabled: bool = False` in `ApiSettings`.
   - Lines 909-918 (`check_decision_proof_type`):
     ```python
     async def check_decision_proof_type(request: Request) -> None:
         if settings.cli_hmac_enabled:
             return
         try:
             raw_body = await request.body()
             if b"webauthn" in raw_body or b"authentication_evidence_id" in raw_body:
                 return
         except Exception:
             pass
         raise HTTPException(403, "proof_type_disabled")
     ```
     Mounted as `dependencies=[Depends(check_decision_proof_type)]` on `/approval/packages/{package_id}/decide` (line 1216), `/v1/approval-requests/{approval_request_id}/decisions` (line 1239), and `/v1/runs/{thread_id}/decision` (line 1255).
   - In `apply_decision_unified` (lines 1051-1056):
     ```python
     if body.proof_type == "hmac":
         if not settings.cli_hmac_enabled:
             raise HTTPException(403, "proof_type_disabled")
     ```

4. **G-TIMEOUT Invariant (`src/identity/policy.py`, `src/graph_api.py`)**:
   - In `src/identity/policy.py:649-651` and `658-659`:
     ```python
     if isinstance(action, str):
         if action.startswith("system:timeout") or action == "timeout":
             raise AgentImpersonationError("inactivity or timeouts never create authorization decisions (G-TIMEOUT invariant)")
     if res.attributes.get("timeout_approved") or res.attributes.get("inactivity_approved"):
         raise AgentImpersonationError("inactivity or timeouts never authorize decisions (G-TIMEOUT invariant)")
     ```
   - In `src/graph_api.py:1002-1003`:
     ```python
     if req_status != "awaiting_approval" or is_expired:
         raise HTTPException(409, "approval_expired")
     ```

5. **Universal Dormancy & Scope Protection**:
   - `docker-compose.yml:202`: `DISPATCH_ENABLED: "false"`.
   - `tests/test_packaging.py` and `tests/test_phase1_scope.py`: verify no Phase 3+ workers or relays exist and dependencies match.

### 1.2 Tool Command Execution & Timeout
- Consolidated pytest command executed:
  ```pwsh
  .\.venv\Scripts\pytest.exe tests/identity/test_player.py tests/test_packaging.py tests/test_phase1_scope.py -v
  ```
- Output received:
  ```
  Encountered error in tool execution: permission check failed for command ".\\.venv\\Scripts\\pytest.exe tests/identity/test_player.py tests/test_packaging.py tests/test_phase1_scope.py -v": Permission prompt for action 'command' on target '.\.venv\Scripts\pytest.exe tests/identity/test_player.py tests/test_packaging.py tests/test_phase1_scope.py -v' timed out waiting for user response. The user was not able to provide permission on time. You should proceed as much as possible without access to this resource.
  ```

### 1.3 Direct Observations of Test Suite Defects
1. **`tests/identity/test_player.py:301-302`**:
   ```python
   resp_cr_empty = client.post(
       f"/v1/player/assignments/{aid}/decision",
       json={"decision": "change_request", "notes": ""},
   )
   assert resp_cr_empty.status_code == 422
   assert "notes required" in resp_cr_empty.json()["detail"].lower()
   ```
   - `resp_cr_empty.json()["detail"]` is a `list[dict]` from Pydantic `RequestValidationError`. Calling `.lower()` on a `list` raises `AttributeError: 'list' object has no attribute 'lower'`.
   - The message in `player.py:144` is `"change_request requires non-empty notes or reason"`, not `"notes required"`.

2. **`tests/identity/test_player.py:261-263`**:
   ```python
   with patch.object(ScopedActionTokenEngine, "consume_token") as mock_consume:
       mock_consume.return_value = {"user_id": principal.user_id}
   ```
   - `src/identity/routers/player.py:424` executes `token_ref = str(consumed_token.token_id)`. Accessing `.token_id` on a dict raises `AttributeError: 'dict' object has no attribute 'token_id'`.
   - Lines 255-259 and 324-328: `mock_store.record_player_decision.return_value` only contains `{"decision_id": ..., "decision": ..., "status": ...}`. `PlayerDecisionResponse` construction in `player.py:501-519` accesses `decision_row["request_id"]`, raising `KeyError: 'request_id'`.

3. **`tests/identity/test_tokens.py:96-107` and `273-280`**:
   ```python
   sat = engine.issue_token(user_id=uid, scope="player_decision", assignment_id=aid, expires_in_minutes=60)
   assert sat.raw_token.startswith("sat_")
   ```
   - `ScopedActionTokenEngine.issue_token` in `src/identity/tokens.py:105` takes `ttl_seconds`, not `expires_in_minutes`, raising `TypeError: unexpected keyword argument 'expires_in_minutes'`.
   - `issue_token` returns `tuple[str, ActionTokenRecord]`. Accessing `sat.raw_token` raises `AttributeError: 'tuple' object has no attribute 'raw_token'`.

4. **`tests/identity/test_tokens.py:148-154`**:
   ```python
   verified = engine.verify_token(...)
   assert verified["user_id"] == uid
   ```
   - `verify_token` and `consume_token` return `ActionTokenRecord` (frozen dataclass). Subscripting raises `TypeError: 'ActionTokenRecord' object is not subscriptable`.

5. **`tests/identity/test_step_up.py:202-243`**:
   - `mock_store.get_challenge` is not mocked. In `src/identity/routers/step_up.py:231`, `challenge_row.get("challenge_type") != "step_up_decision"` compares `MagicMock() != "step_up_decision"`, which evaluates to `True`, raising HTTP 400. The test expects HTTP 200, failing with status 400.

---

## 2. Logic Chain

1. **Production Logic Correctness (Observations 1.1.1 - 1.1.5)**:
   - Object-level player isolation is implemented securely: `check_player_isolation` strictly binds `principal.player_id == subject_player_id` and rejects staff roles from acting as players (HTTP 403 `PlayerIsolationError`).
   - The 5 distinct player actions correctly differentiate between `accept` (requires passkey/action-token proof) and `acknowledge` (passive review receipt, zero activation). `change_request` and `pause` enforce input validation via Pydantic model validators.
   - The HMAC kill switch (`APPROVAL_CLI_HMAC_ENABLED=false` default) rejects HMAC decisions and blocks unauthenticated non-WebAuthn probes via pre-flight route dependencies.
   - The G-TIMEOUT invariant rejects timeout approval injections (HTTP 403 `AgentImpersonationError`) and blocks expired requests (HTTP 409 `approval_expired`).
   - Universal dormancy (`DISPATCH_ENABLED=false`) is 100% maintained.

2. **Test Suite Defect Impact (Observations 1.3.1 - 1.3.5)**:
   - The test files delivered by `worker_m5_1` were not executed due to terminal permission timeout and contain 5 distinct categories of runtime failures (`AttributeError`, `TypeError`, `KeyError`, and `AssertionError`).
   - In CI or under pytest, `test_player.py`, `test_tokens.py`, and `test_step_up.py` will fail immediately.
   - A milestone cannot be marked approved when its verification test suite is broken.
   - Therefore, the milestone must be returned to the worker with **REQUEST_CHANGES** for test suite remediation.

---

## 3. Caveats

- **Runtime Execution**: Live interactive execution of pytest was blocked due to environment terminal permission prompt timeout. All analysis was conducted via static analysis, complete AST inspections, and trace validation of Python execution semantics.
- **Production Code Status**: The production code in `src/` requires zero changes; all required changes are strictly confined to the unit test mocks and assertions in `tests/identity/`.

---

## 4. Conclusion

- **Verdict**: **REQUEST_CHANGES**
- **Actionable Steps for Worker (`worker_m5_1`)**:
  1. Fix `tests/identity/test_player.py:302`: Convert detail to string and check `"change_request requires non-empty notes or reason"`.
  2. Fix `tests/identity/test_player.py:262`: Mock `consumed_token` with an object possessing `token_id = uuid4()`.
  3. Fix `tests/identity/test_player.py:255` & `324`: Populate all required fields of `PlayerDecisionResponse` in `mock_store.record_player_decision.return_value`.
  4. Fix `tests/identity/test_tokens.py:96` & `273`: Replace `expires_in_minutes=60` with `ttl_seconds=3600`, and unpack tuple `raw_token, record = engine.issue_token(...)`.
  5. Fix `tests/identity/test_tokens.py:149` & `153`: Replace `verified["user_id"]` with `verified.user_id` and `consumed.consumed_at`.
  6. Fix `tests/identity/test_step_up.py:202`: Configure `mock_store.get_challenge.return_value` with `{"challenge_type": "step_up_decision", ...}`.

---

## 5. Verification Method

To verify resolution of these findings:
1. Run:
   ```pwsh
   .\.venv\Scripts\pytest.exe tests/identity/test_player.py tests/identity/test_tokens.py tests/identity/test_step_up.py tests/test_packaging.py tests/test_phase1_scope.py -v
   ```
2. Verify all tests execute with zero `AttributeError`, `TypeError`, `KeyError`, or `AssertionError`.
3. Verify `DISPATCH_ENABLED=false` and zero player delivery.
