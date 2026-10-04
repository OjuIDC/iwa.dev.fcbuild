# DISPATCH: worker_m5_2 (Phase 2 Milestone 5 Iteration 2 Remediation Worker)

**Role**: `teamwork_preview_worker`
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m5_2`
**Authoritative Request**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md`
**Project Plan**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
**Previous Audit Report**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m5_1\handoff.md`
**Previous Reviewer Reports**:
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m5_1\handoff.md`
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m5_2\handoff.md`
**Previous Challenger Reports**:
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m5_1\challenge_report.md`
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m5_2\handoff.md`

---

## MANDATORY INTEGRITY WARNING
> DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

---

## Non-Negotiable Invariants
1. Universal dormancy: `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` strictly enforced across all environments, settings, and endpoints.
2. Zero player delivery; Phase 2 must NEVER activate player dispatch, delivery adapters, or outbox relay.
3. No Phase 3 dispatch worker, outbox relay, or player-agent execution pipeline.
4. Preserve Phase 1 baseline commit `3339d6d672bbc59f251e1e2a6977bc9ea65edffe`.
5. PostgreSQL is authoritative for all identity and authorization state.
6. No agent-human impersonation (automated services/LLM nodes cannot authenticate or decide for humans; `require_human_principal` enforced).
7. Gamertags and display names are presentation data only; use immutable local user UUIDs and numeric player IDs as primary identity.
8. Inactivity and timeouts alter escalation state but NEVER create approvals or decisions (G-TIMEOUT invariant).

---

## File Ownership
You exclusively own and may edit:
- `src/identity/tokens.py`
- `src/identity/store.py`
- `src/identity/routers/step_up.py`
- `src/identity/routers/player.py`
- `src/graph_api.py`
- `tests/identity/test_tokens.py`
- `tests/identity/test_step_up.py`
- `tests/identity/test_player.py`

---

## Detailed Remediation Blueprint

### 1. `src/identity/tokens.py` & `tests/identity/test_tokens.py`
- **In `src/identity/tokens.py`**:
  - Export compatibility aliases and helper functions:
    ```python
    ScopedActionToken = ActionTokenRecord
    VALID_ACTION_SCOPES = IdentityStore.VALID_ACTION_SCOPES
    hash_action_token = hash_token

    def is_action_token_format(val: Any) -> bool:
        return isinstance(val, str) and val.startswith(TOKEN_PREFIX) and len(val) >= 36
    ```
  - In `ScopedActionTokenEngine.issue_token`: add optional keyword argument `expires_in_minutes: int | None = None`. If provided and `ttl_seconds` is default, set `ttl_seconds = expires_in_minutes * 60`.
- **In `tests/identity/test_tokens.py`**:
  - Fix imports: import `ActionTokenRecord`, `ScopedActionToken`, `VALID_ACTION_SCOPES`, `hash_action_token`, `is_action_token_format` cleanly from `src.identity.tokens`.
  - In test functions: unpack `raw_token, record = engine.issue_token(...)` where appropriate, or verify both tuple unpacking and attribute access.
  - In `verify_token` assertions: access `verified.user_id` (attribute on `ActionTokenRecord`), not dictionary subscription `verified["user_id"]`.
  - In tests mocking store lookups: mock `mock_store.get_action_token_by_token` instead of `mock_store.get_action_token_by_hash`.

### 2. `src/identity/store.py`
- In `create_decision_evidence`:
  - Ensure all non-essential parameters have default values so callers don't fail with missing positional arguments:
    `challenge_id: UUID | None = None`, `approval_request_id: UUID | str | None = None`, `credential_id: bytes = b""`, `bound_payload_hash: str = ""`.
- In `consume_decision_evidence`:
  - Enforce explicit parameter binding checks against `expected_package_id`, `expected_package_version`, `expected_package_hash`.
  - Do NOT allow `"unspecified"` to bypass equality checks if expected values are provided.

### 3. `src/identity/routers/player.py`
- In `execute_player_decision`:
  - When decision is `accept` and authenticated via WebAuthn passkey: validate and consume `authentication_evidence_id` via `store.consume_decision_evidence(...)`, verifying not already consumed and bound to the assignment.
  - In action token flow: handle both `ActionTokenRecord` instances (with `.token_id`) and dicts defensively:
    ```python
    token_ref = str(getattr(consumed_token, "token_id", None) or (consumed_token.get("token_id") if isinstance(consumed_token, dict) else str(uuid4())))
    ```
  - In Pydantic validation: ensure validation exceptions provide clean error details.

### 4. `src/graph_api.py`
- Fix imports:
  - Add `from datetime import datetime, timedelta, timezone` if missing.
- In HMAC decision fallback branch (around lines 1060–1065):
  - Pass integer UNIX timestamps to `SignedDecision`:
    `issued_at=int(datetime.now(timezone.utc).timestamp())`,
    `expires_at=int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp())`.
- In `apply_decision_unified`:
  - Pass `expected_package_id=package_id`, `expected_package_version=package_version`, and `expected_package_hash=package_hash` to `id_store.consume_decision_evidence(...)`.

### 5. `tests/identity/test_step_up.py`
- In `test_step_up_verify_endpoint_mints_evidence`:
  - Configure `mock_store.get_challenge.return_value = {
      "challenge_id": uuid4(),
      "challenge_str": "test_challenge_str",
      "challenge_type": "step_up_decision",
      "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
      "user_id": principal.user_id,
      "bound_payload_hash": "..."
  }` so that `challenge_type == "step_up_decision"` passes cleanly and returns HTTP 200.

### 6. `tests/identity/test_player.py`
- In `test_player_decision_action_token_flow`:
  - Configure `mock_consume.return_value = MagicMock(token_id=uuid4(), user_id=principal.user_id)` or an `ActionTokenRecord` instance so `.token_id` does not raise AttributeError.
- In `test_player_decision_actions_validation`:
  - Convert `detail` to string before checking substring:
    `assert "change_request requires non-empty notes or reason" in str(resp_cr_empty.json()["detail"]).lower()`.
- In lines 255 and 324:
  - Ensure `mock_store.record_player_decision.return_value` contains required dictionary keys:
    `{"request_id": str(uuid4()), "status": "executed", "decision": "accept", "decided_at": datetime.now(timezone.utc).isoformat()}`.
- In `test_g_timeout_invariant_rejection`:
  - Pass an action starting with `"system:timeout"` (e.g. `"system:timeout:auto_approve"`) or ensure `policy.py` check raises `AgentImpersonationError`.

---

## Verification & Execution Protocol
1. Run the unified test suite in a single consolidated invocation with `WaitMsBeforeAsync: 10000`:
   ```powershell
   .\.venv\Scripts\pytest.exe tests/identity/test_tokens.py tests/identity/test_step_up.py tests/identity/test_player.py tests/test_packaging.py tests/test_phase1_scope.py -v
   ```
2. If an interactive permission prompt times out waiting for user response:
   - DO NOT claim tests passed.
   - Accurately document the attempted command, the timeout, and verify via AST and static analysis.
3. Record all test outputs and findings in your handoff report:
   `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m5_2\handoff.md`
4. Notify orchestrator via `send_message` upon completion.


## 2026-10-04T09:58:17Z
[Message] timestamp=2026-10-04T09:58:17Z sender=25210876-a9ad-4f78-a87c-dec88e86edb9 priority=MESSAGE_PRIORITY_HIGH content=You are worker_m5_2, a versatile worker agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m5_2
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m5_2\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md
Previous Forensic Audit Report with violations: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m5_1\handoff.md
Previous Reviewer Reports:
- c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m5_1\handoff.md
- c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m5_2\handoff.md
Previous Challenger Reports:
- c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m5_1\challenge_report.md
- c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m5_2\handoff.md
