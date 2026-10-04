# Handoff Report: Phase 2 Tier 5 Adversarial Coverage Hardening Test Suite Mapping & Design

**Agent**: `explorer_m6_security_hardening_1`  
**Role**: `teamwork_preview_explorer`  
**Target Path**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m6_security_hardening_1\handoff.md`  
**Milestone**: Milestone 6 (Integration, E2E Pass, Adversarial Hardening & Acceptance)  
**Date**: 2026-10-04T10:30:00Z  

---

## 1. Observation

Direct examination of the Phase 2 codebase, database schema, migrations, and test suites revealed the following exact implementations, line numbers, and security postures across the 10 adversarial tracks:

### 1.1 Track 1: Object-Level Player Isolation
- In `src/identity/routers/player.py`:
  - Lines 232–252 define `check_player_isolation(principal: PrincipalContext, assignment: dict[str, Any])`:
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
  - Lines 280 & 361: Both `/decision-challenge` and `/decisions` enforce `principal: PrincipalContext = Depends(require_human_principal)`.
  - Lines 207–225: `compute_decision_bound_hash()` deterministically binds `assignment_id`, `assignment_version`, `assignment_hash`, `package_id`, `package_version`, `package_hash`, `subject_player_id`, `decision`, and `authority_basis`.
- In `src/identity/policy.py`:
  - Lines 696–716: `authorize()` enforces that pure players can only access resources where `res.player_id == principal.player_id` or `res.owner_user_id == principal.user_id`. Furthermore, line 710 explicitly enforces: `if res.scope == "club": raise PlayerIsolationError("player role cannot access club-wide scoped resources")`.
  - Lines 808–847: For `AuthorityBasis.AFFECTED_PLAYER_CONSENT` and `AuthorityBasis.AFFECTED_PLAYER_FALLBACK`, player ID mismatch raises `PlayerIsolationError`. For fallback, `res.scope != "player_specific"` raises `PlayerIsolationError("affected-player fallback is strictly forbidden on club-wide packages")`, and prohibited actions raise `RoleNotPermittedError`.

### 1.2 Track 2: G-TIMEOUT Invariant
- In `src/identity/policy.py`:
  - Lines 648–653: `authorize()` checks action strings:
    ```python
    if isinstance(action, str):
        if action.startswith("system:timeout") or action == "timeout":
            raise AgentImpersonationError("inactivity or timeouts never create authorization decisions (G-TIMEOUT invariant)")
        act = ActionType(action)
    ```
  - Lines 657–660: `authorize()` checks resource attributes:
    ```python
    if res.attributes.get("timeout_approved") or res.attributes.get("inactivity_approved"):
        raise AgentImpersonationError("inactivity or timeouts never authorize decisions (G-TIMEOUT invariant)")
    ```
- In `src/graph_api.py`:
  - Lines 1003–1005: When `req_status != "awaiting_approval"` or `is_expired` is true on an approval request, decision submission fails closed with `HTTPException(409, "approval_expired")`.
  - Inactivity and timeouts alter escalation state (`public.approval_escalations`) but never commit an `approved` decision.

### 1.3 Track 3: Service Anti-Impersonation
- In `src/identity/deps.py`:
  - Lines 42–56: `require_human_principal()` inspects `principal.is_service`:
    ```python
    def require_human_principal(principal: PrincipalContext = Depends(get_current_principal)) -> PrincipalContext:
        if principal.is_service:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="service principals cannot access human-only endpoints",
            )
        return principal
    ```
- In `src/identity/policy.py`:
  - Lines 676–683:
    ```python
    if principal.is_service and act in HUMAN_DECISION_ACTIONS:
        raise AgentImpersonationError(
            f"service principal '{principal.user_id}' cannot perform human action '{act.value}'"
        )
    ```
  - `HUMAN_DECISION_ACTIONS` includes `PACKAGE_APPROVE`, `PACKAGE_REJECT`, `PACKAGE_REQUEST_REVISION`, `ASSIGNMENT_ACCEPT`, `ASSIGNMENT_REJECT`, `ASSIGNMENT_CHANGE_REQUEST`, `ASSIGNMENT_PAUSE`, `ASSIGNMENT_ACKNOWLEDGE`, `DELEGATION_CREATE`, `DELEGATION_REVOKE`, `BOOTSTRAP_EXECUTE`, and `RECOVERY_EXECUTE`.
- In `src/identity/services.py`:
  - Lines 48–50 define service user UUIDs: `BFF_SERVICE_USER_ID`, `GRAPH_SERVICE_USER_ID`, `WORKER_SERVICE_USER_ID`.
  - `assert_no_service_token_collision()` ensures service credentials are differentiated; collisions raise `ConfigError`.
- In `src/graph_api.py`:
  - Lines 931–935: `apply_decision_unified()` checks:
    ```python
    if principal.is_service:
        raise AgentImpersonationError(f"service principal '{principal.user_id}' cannot commit human decisions")
    ```

### 1.4 Track 4: Universal Dormancy & Zero Player Delivery
- In `src/graph_api.py`:
  - Lines 503, 511–512: `GET /health/ready` returns `"dispatch": "disabled"`; `GET /v1/config/dispatch` returns `"configured": False, "effective": False`.
  - Lines 885–897, 1106–1111, 1197–1202: All decision endpoints return `DispatchBlock`:
    ```python
    DispatchBlock(
        enabled=False,
        created=False,
        message="Dispatch is disabled. This approval records the selection only; nothing was delivered to any player.",
        dispatch_id=None,
    )
    ```
- In `migrations/post/grants.sql`:
  - Lines 173–215: Application runtime `fc27_app` holds identity grants, while `fc27_dispatch` holds ZERO grants on identity tables (`fc27_users`, `fc27_sessions`, `fc27_invitations`, `authority_delegations`, etc.).
  - No dispatch worker, outbox relay, or player delivery adapter is invoked or active.

### 1.5 Track 5: PostgreSQL Authoritative State (locks, triggers)
- In `migrations/0004_identity_foundation.sql`:
  - Lines 27–73: `fc27_guard_user_status_transition()` trigger on `fc27_users` rejects mutation of `user_id`, `club_id`, and `created_at`; prevents reactivating `disabled` accounts; restricts `invited` accounts to transition only to `active` or `disabled`.
  - Lines 174–191: `fc27_guard_webauthn_counter()` trigger on `fc27_webauthn_credentials` raises check violation if `sign_counter` decreases (`NEW.sign_counter < OLD.sign_counter`).
  - Lines 222–250: `fc27_recovery_codes_immutable` trigger rejects `DELETE` via `fc27_reject_mutation()`; `fc27_guard_recovery_code_consumption()` trigger rejects updates to consumed codes, prevents unconsuming, and freezes attributes.
  - Lines 281–302: `fc27_guard_action_token_mutation()` trigger on `fc27_action_tokens` blocks mutation of already consumed/revoked tokens and freezes parameters.
  - Lines 331–352: `fc27_guard_challenge_consumption()` trigger on `fc27_challenges` prevents unconsuming challenges and freezes challenge parameters.
- In `migrations/0006_bootstrap_and_recovery.sql`:
  - Lines 11–26: `fc27_bootstrap_locks` has `single_bootstrap_lock` check constraint and `fc27_bootstrap_locks_immutable` trigger rejecting `BEFORE UPDATE OR DELETE` via `fc27_reject_mutation()`.
- In `src/approval_store.py` and `src/identity/store.py`:
  - Row-level locks (`SELECT ... FOR UPDATE`) serialize concurrent access on `approval_nonces`, `approval_requests`, `graph_threads`, `fc27_sessions`, `fc27_challenges`, `fc27_action_tokens`, and `fc27_recovery_codes`.

### 1.6 Track 6: Decision Step-Up Replay & Scope Lockout
- In `src/identity/routers/step_up.py`:
  - Lines 32–34 & `compute_bound_payload_hash()`: Generates deterministic SHA-256 bound payload hash over `package_id`, `package_version`, `package_hash`, `decision`, and `approval_request_id`.
  - Verifying the challenge mints an `authentication_evidence_id` in `fc27_decision_evidence`.
- In `src/graph_api.py`:
  - Lines 1087–1115: When `evidence["consumed_at"] is not None`, the system checks if the submission matches an already committed audit row. If so, it returns an idempotent replay projection; if not, it raises `HTTPException(409, "replay: authentication evidence already consumed")`.
  - Lines 1116–1121: Scope lockout checks:
    ```python
    if evidence.get("package_id") and package_id and evidence["package_id"] != package_id:
        raise HTTPException(403, "step-up evidence bound package mismatch")
    if evidence.get("decision") and evidence["decision"].lower() != body.decision.lower():
        raise HTTPException(403, "step-up evidence bound decision mismatch")
    ```
  - Lines 1127–1132: `id_store.consume_decision_evidence()` consumes evidence with expected package ID, version, and hash checks; second consumption fails closed.

### 1.7 Track 7: Scoped Action Tokens Single-Use & Expiry
- In `src/identity/tokens.py`:
  - Lines 48–73: `sat_` prefix check, 256-bit entropy generation, deterministic SHA-256 irreversible hashing. Raw tokens are never stored in PostgreSQL.
  - Lines 27–35: Specialized error hierarchy:
    - `ActionTokenNotFoundError` (HTTP 404)
    - `ActionTokenAlreadyConsumedError` (HTTP 409)
    - `ActionTokenExpiredError` (HTTP 410)
    - `ActionTokenRevokedError` (HTTP 410)
    - `ActionTokenScopeMismatchError` (HTTP 403)
    - `ActionTokenAssignmentMismatchError` (HTTP 403)
  - Valid scopes: `player_decision`, `step_up`, `delegation`, `invitation`, `recovery`.

### 1.8 Track 8: Commissioner Bootstrap Ceremony Lock
- In `src/identity/recovery.py`:
  - Lines 26–96: `BootstrapService.execute_bootstrap()`:
    1. Validates `bootstrap_token` using constant-time comparison.
    2. Validates non-empty input parameters (`login_identifier`, `display_name`, `club_id`).
    3. Checks `is_bootstrapped()`. If already bootstrapped, raises `IdentityStoreError(409, "commissioner bootstrap ceremony is permanently locked")`.
    4. Executes atomic transaction creating the user, assigning role `commissioner`, inserting `fc27_bootstrap_locks`, generating recovery codes, and minting initial session.
- In `migrations/0006_bootstrap_and_recovery.sql`:
  - Lines 11–26: `fc27_bootstrap_locks` table with primary key `commissioner_bootstrap` and immutability trigger `fc27_bootstrap_locks_immutable`.

### 1.9 Track 9: Commissioner Account Recovery & Argon2id Revocation
- In `src/identity/recovery.py`:
  - Lines 32–68: Crockford Base32 pattern `fc27-xxxx-xxxx-xxxx` (12 alphanumeric chars excluding 0, O, 1, I, l).
  - Lines 62–68: Irreversible Argon2id hashing via `hash_recovery_code()` and constant-time verification via `verify_recovery_code()`.
  - Lines 87–185: `RecoveryService.verify_and_recover()` verifies account eligibility (`active` or `recovery_required`; `suspended` or `disabled` accounts fail closed). Atomic consumption marks `consumed_at = now()`.
  - Batch regeneration atomically invalidates/replaces unconsumed codes.

### 1.10 Track 10: Legacy HMAC Kill Switch
- In `src/settings.py` & `src/graph_api.py`:
  - `settings.cli_hmac_enabled` defaults to `False`.
  - Lines 530–532, 581–583: `POST /v1/activation-gates/{gate_id}/challenge` and `POST /v1/activation-gates/{gate_id}` raise `HTTPException(403, "proof_type_disabled")` when `cli_hmac_enabled` is false.
  - Lines 830–833, 860–862: `POST /v1/runs/{thread_id}/decision-challenge` and `apply_decision_internal` raise `HTTPException(403, "proof_type_disabled")` when `cli_hmac_enabled` is false.
  - Lines 910–920: `check_decision_proof_type` dependency on `/approval/packages/{package_id}/decide`, `/v1/approval-requests/{id}/decisions`, and `/v1/runs/{thread_id}/decision` rejects `proof_type=hmac` with `HTTPException(403, "proof_type_disabled")` while permitting `proof_type=webauthn`.

---

## 2. Logic Chain

From the direct observations above, the logical progression to our conclusions and test suite design is as follows:

1. **Current Test Coverage vs. Tier 5 Hardening**:
   - Existing unit tests (`tests/identity/`) and E2E tests (`tests/e2e/test_phase2_tier1_features.py` through `test_phase2_tier4_scenarios.py`) verify the positive contracts and discrete boundary failures for Milestone 1 through Milestone 5.
   - However, they do not provide a dedicated, consolidated **Tier 5 Adversarial Coverage Hardening** test suite (`tests/adversarial/test_adversarial_phase2.py`) executing against the integrated system with live PostgreSQL triggers, transaction locks, cross-player isolation attacks, concurrency race conditions, and HMAC kill-switch verification across all 10 tracks.
2. **Track-by-Track Gap Analysis**:
   - **Track 1 Gap**: No opaque-box HTTP test confirms that an action token issued for Player A's assignment is rejected when submitted by Player B, nor that an unlinked player (`player_id IS NULL`) is denied access across all player endpoints.
   - **Track 2 Gap**: No HTTP test asserts that injecting `system:timeout*` actions or `timeout_approved` attributes in `/v1/approval-requests/{id}/decisions` or `/v1/player-assignments/{id}/decision` raises `AgentImpersonationError` (403), nor that expired requests fail closed with HTTP 409 and never transition to `approved`.
   - **Track 3 Gap**: No matrix test systematically runs all 3 service tokens (`bff_service`, `graph_service`, `worker_service`) against all 12 human-only endpoints.
   - **Track 4 Gap**: No adversarial test asserts that passing `{"dispatch_enabled": true}` in decision bodies is strictly ignored and leaves dispatch disabled, nor verifies that zero rows are added to `dispatch_delivery_attempts`.
   - **Track 5 Gap**: No concurrent test uses multithreading/connection pooling to attempt simultaneous double-consumption of challenges, action tokens, or recovery codes against PostgreSQL `SELECT ... FOR UPDATE` row locks.
   - **Track 6 Gap**: No test verifies that tampering solely with the `package_hash` in a step-up decision payload (while keeping package ID and version identical) triggers scope lockout rejection.
   - **Track 7 Gap**: No test attempts cross-scope confusion attacks (e.g. submitting a token minted with `scope="invitation"` to `/v1/player-assignments/{id}/decision`).
   - **Track 8 Gap**: No live E2E test runs two sequential HTTP calls to `POST /v1/auth/bootstrap` to confirm permanent HTTP 409 lockout and verifies that SQL `DELETE FROM fc27_bootstrap_locks` fails with `fc27_reject_mutation()`.
   - **Track 9 Gap**: No test confirms that regenerating recovery codes atomically invalidates prior unused codes, and that suspended/disabled accounts fail recovery closed.
   - **Track 10 Gap**: No parameterized test asserts that all 6 HMAC endpoints uniformly return HTTP 403 `proof_type_disabled` when `cli_hmac_enabled = False`, while WebAuthn decisions succeed.
3. **Design of Tier 5 Adversarial Test Suite**:
   - To achieve complete adversarial hardening, `tests/adversarial/test_adversarial_phase2.py` must be structured into 10 distinct test classes corresponding to the 10 tracks.
   - The test designs below specify the exact class names, method signatures, fixtures, mock payloads, expected HTTP status codes, error details, and database assertions.

---

### Detailed Tier 5 Test Suite Architecture (`tests/adversarial/test_adversarial_phase2.py`)

```python
"""Tier 5 Adversarial Coverage Hardening Test Suite (Phase 2).

Consolidated 10-track adversarial hardening suite:
- Track 1: Object-Level Player Isolation
- Track 2: G-TIMEOUT Invariant
- Track 3: Service Anti-Impersonation
- Track 4: Universal Dormancy & Zero Player Delivery
- Track 5: PostgreSQL Authoritative State (locks, triggers)
- Track 6: Decision Step-Up Replay & Scope Lockout
- Track 7: Scoped Action Tokens Single-Use & Expiry
- Track 8: Commissioner Bootstrap Ceremony Lock
- Track 9: Commissioner Account Recovery & Argon2id Revocation
- Track 10: Legacy HMAC Kill Switch
"""
```

#### Track 1: Test Suite Design (`TestTrack1ObjectLevelPlayerIsolationAdversarial`)
1. `test_adv_t1_cross_player_challenge_generation_rejected(self, client, two_players, store)`:
   - Setup: Player 1 (user A, player_id "10001") and Player 2 (user B, player_id "10002") with active sessions. Assignment 2 created for Player 2.
   - Action: Player 1 calls `POST /v1/player-assignments/{assignment_2_id}/decision-challenge` with Player 1 Bearer session token.
   - Assertion: HTTP 403 Forbidden; response JSON `detail` contains `"player isolation violation"`.
2. `test_adv_t1_cross_player_decision_submission_rejected(self, client, two_players, store)`:
   - Setup: Assignment 2 for Player 2.
   - Action: Player 1 calls `POST /v1/player-assignments/{assignment_2_id}/decision` with `decision: "accept"`.
   - Assertion: HTTP 403 Forbidden (`PlayerIsolationError`).
3. `test_adv_t1_cross_player_action_token_issuance_rejected(self, client, two_players)`:
   - Setup: Player 1 attempts to call `POST /v1/player/tokens/decision` specifying `assignment_id = assignment_2_id`.
   - Action: Submit request.
   - Assertion: HTTP 403 Forbidden.
4. `test_adv_t1_cross_player_action_token_consumption_rejected(self, client, two_players, store)`:
   - Setup: Legitimate action token minted for Player 2 on Assignment 2.
   - Action: Player 1 submits decision on Assignment 1 using Player 2's action token.
   - Assertion: HTTP 403 Forbidden (`ActionTokenAssignmentMismatchError`).
5. `test_adv_t1_staff_and_coach_cannot_submit_player_decisions(self, client, active_coach, store)`:
   - Setup: Coach user with active session. Assignment 1 for Player 1.
   - Action: Coach calls `POST /v1/player-assignments/{assignment_1_id}/decision`.
   - Assertion: HTTP 403 Forbidden (`role not permitted to act as player`).
6. `test_adv_t1_player_cannot_access_club_wide_package(self, client, two_players)`:
   - Setup: Player 1 session.
   - Action: Player 1 attempts to call `POST /v1/player/decision-challenge` on a club-scoped package (`scope="club"`).
   - Assertion: HTTP 403 Forbidden (`player role cannot access club-wide scoped resources`).
7. `test_adv_t1_unlinked_player_rejected(self, client, store)`:
   - Setup: User created with role `player` but `player_id = NULL`. Active session established.
   - Action: Calls `POST /v1/player/assignments`.
   - Assertion: HTTP 403 Forbidden (`PlayerIsolationError`).

#### Track 2: Test Suite Design (`TestTrack2GTimeoutInvariantAdversarial`)
1. `test_adv_t2_timeout_action_strings_rejected(self, client, active_commissioner)`:
   - Action: Parameterized testing of `action` strings (`"system:timeout"`, `"system:timeout_approve"`, `"timeout"`, `"system:timeout:auto"`) sent to decision endpoints.
   - Assertion: HTTP 403 / 422; `AgentImpersonationError` detail `"G-TIMEOUT invariant"`.
2. `test_adv_t2_resource_attribute_timeout_poisoning_rejected(self, client, active_commissioner)`:
   - Action: Submit decision payload with `{"attributes": {"timeout_approved": true}}` or `{"attributes": {"inactivity_approved": true}}`.
   - Assertion: HTTP 403 Forbidden (`AgentImpersonationError`).
3. `test_adv_t2_expired_approval_request_fails_closed(self, client, active_commissioner, pg_uri, store)`:
   - Setup: Approval request created with `expires_at = now() - interval '10 seconds'`.
   - Action: Submit decision `approved` via `POST /v1/approval-requests/{id}/decisions`.
   - Assertion: HTTP 409 Conflict (`approval_expired`); PostgreSQL `graph_threads.status` is `expired` or `awaiting_approval`, strictly NOT `approved`.
4. `test_adv_t2_inactivity_escalation_does_not_approve(self, pg_uri, store)`:
   - Setup: Simulate background inactivity escalation updating `approval_escalations` table.
   - Assertion: Verify `approval_requests.status` remains `awaiting_approval` or `expired`; `domain_events` contains `approval.escalated`; zero rows in `approval_decisions`.
5. `test_adv_t2_session_idle_timeout_rejects_decision(self, client, active_commissioner, pg_uri)`:
   - Setup: Set session `idle_expires_at = now() - interval '1 second'`.
   - Action: Attempt decision submission with this session.
   - Assertion: HTTP 401 Unauthorized (`session_expired`); no decision committed.

#### Track 3: Test Suite Design (`TestTrack3ServiceAntiImpersonationAdversarial`)
1. `test_adv_t3_service_tokens_rejected_on_all_human_endpoints(self, client)`:
   - Setup: Parameterize across 3 service credentials (`BFF_HEADERS`, `GRAPH_HEADERS`, `WORKER_HEADERS`) across 12 human endpoints:
     - `/v1/auth/passkeys/register/options`
     - `/v1/auth/passkeys/register/verify`
     - `/v1/auth/passkeys/login/options`
     - `/v1/auth/totp/enroll`
     - `/v1/auth/totp/verify`
     - `/v1/auth/bootstrap`
     - `/v1/auth/recovery/initiate`
     - `/v1/auth/step-up/options`
     - `/v1/auth/step-up/verify`
     - `/v1/approval-requests/{id}/decisions`
     - `/v1/player-assignments/{id}/decision-challenge`
     - `/v1/player-assignments/{id}/decision`
   - Assertion: Every request returns HTTP 403 Forbidden with detail `"service principals cannot access human-only endpoints"` or `AgentImpersonationError`.
2. `test_adv_t3_service_token_collision_startup_guard()`:
   - Setup: Instantiate `ServiceCredentialsManager` with identical keys for BFF and Graph.
   - Assertion: Raises `ConfigError("service token collision detected")`.
3. `test_adv_t3_forged_impersonation_headers_ignored(self, client)`:
   - Setup: Service token with header `X-Actor-User-Id: <commissioner_id>` calling `/v1/approval-requests/{id}/decisions`.
   - Assertion: HTTP 403 Forbidden; caller identity resolved authoritatively from token, headers ignored.

#### Track 4: Test Suite Design (`TestTrack4UniversalDormancyAndZeroDeliveryAdversarial`)
1. `test_adv_t4_dispatch_remains_disabled_across_all_endpoints(self, client)`:
   - Endpoints: `GET /health/ready`, `GET /health/live`, `GET /v1/config/dispatch`, `GET /v1/runs/{id}`, `POST /v1/runs`.
   - Assertion: Every endpoint returns `"dispatch": "disabled"` or `dispatch.enabled = false`.
2. `test_adv_t4_decision_submission_returns_dispatch_disabled_block(self, client, active_commissioner)`:
   - Action: Submit approved decision.
   - Assertion: HTTP 200; `dispatch.enabled == false`, `dispatch.created == false`, `dispatch.dispatch_id is None`.
3. `test_adv_t4_zero_player_delivery_and_empty_delivery_tables(self, pg_uri)`:
   - Direct SQL query: `SELECT COUNT(*) FROM public.dispatch_delivery_attempts` is 0.
   - `SELECT COUNT(*) FROM public.outbox WHERE published_at IS NOT NULL` is 0.
4. `test_adv_t4_force_dispatch_payload_parameter_ignored(self, client, active_commissioner)`:
   - Action: Submit decision with `{"force_dispatch": true, "dispatch_enabled": true}`.
   - Assertion: Decision succeeds but `dispatch.enabled` remains `false`.

#### Track 5: Test Suite Design (`TestTrack5PostgresAuthoritativeStateAdversarial`)
1. `test_adv_t5_bootstrap_locks_immutability_trigger(self, pg_uri)`:
   - Action: `UPDATE public.fc27_bootstrap_locks SET club_id = '999'` and `DELETE FROM public.fc27_bootstrap_locks`.
   - Assertion: Both raise `psycopg.errors.RaiseException` with message containing `"fc27_reject_mutation"`.
2. `test_adv_t5_recovery_codes_triggers_prevent_unconsuming_and_deletion(self, pg_uri)`:
   - Action: `DELETE FROM public.fc27_recovery_codes` -> raises `fc27_reject_mutation`.
   - Action: `UPDATE public.fc27_recovery_codes SET consumed_at = NULL WHERE consumed_at IS NOT NULL` -> raises `"cannot un-consume recovery code"`.
3. `test_adv_t5_user_status_guard_trigger_blocks_disabled_reactivation(self, pg_uri)`:
   - Action: `UPDATE public.fc27_users SET status = 'active' WHERE status = 'disabled'`.
   - Assertion: Raises `"disabled user account cannot be reactivated"`.
4. `test_adv_t5_webauthn_sign_counter_monotonicity_trigger(self, pg_uri)`:
   - Action: `UPDATE public.fc27_webauthn_credentials SET sign_counter = sign_counter - 1`.
   - Assertion: Raises `"webauthn sign_counter cannot decrease"`.
5. `test_adv_t5_concurrent_challenge_consumption_row_lock(self, pg_uri, store)`:
   - Setup: Single challenge created in `fc27_challenges`.
   - Action: Spawn two concurrent threads calling `store.consume_challenge()` simultaneously.
   - Assertion: Exactly 1 succeeds; second thread encounters 409 or locked rejection.
6. `test_adv_t5_runtime_role_cannot_run_ddl(self, pg_uri)`:
   - Action: Using `fc27_app` connection, execute `CREATE TABLE breach_test (id int);` or `DROP TABLE fc27_users;`.
   - Assertion: Raises `psycopg.errors.InsufficientPrivilege`.

#### Track 6: Test Suite Design (`TestTrack6DecisionStepUpReplayAndLockoutAdversarial`)
1. `test_adv_t6_stepup_evidence_single_use_consumption(self, client, active_commissioner)`:
   - Setup: Generate step-up challenge, verify assertion, obtain `evidence_id`.
   - Action 1: Submit decision on Approval Request 1 -> succeeds (200).
   - Action 2: Submit decision on Approval Request 2 with same `evidence_id`.
   - Assertion: HTTP 409 Conflict (`replay: authentication evidence already consumed`).
2. `test_adv_t6_scope_lockout_different_package_rejected(self, client, active_commissioner, store)`:
   - Setup: Evidence minted bound to `package_A`.
   - Action: Submit decision for `package_B` supplying `evidence_id`.
   - Assertion: HTTP 403 Forbidden (`step-up evidence bound package mismatch`).
3. `test_adv_t6_scope_lockout_tampered_package_hash_rejected(self, client, active_commissioner, store)`:
   - Setup: Evidence minted bound to `package_hash_1`.
   - Action: Submit decision with `package_hash: "tampered_hash_value"`.
   - Assertion: HTTP 403 / 409 mismatch.
4. `test_adv_t6_scope_lockout_decision_type_mismatch_rejected(self, client, active_commissioner)`:
   - Setup: Evidence minted for `decision: "rejected"`.
   - Action: Submit decision with `decision: "approved"`.
   - Assertion: HTTP 403 Forbidden (`step-up evidence bound decision mismatch`).
5. `test_adv_t6_identical_decision_replay_is_safe_idempotent(self, client, active_commissioner)`:
   - Action: Resubmit identical decision payload for the same thread with already consumed evidence.
   - Assertion: HTTP 200 returned; no duplicate audit row inserted.

#### Track 7: Test Suite Design (`TestTrack7ScopedActionTokensAdversarial`)
1. `test_adv_t7_token_format_and_zero_plaintext_storage(self, client, active_player, pg_uri)`:
   - Action: Issue action token via `POST /v1/player/tokens/decision`.
   - Assertion: Token starts with `sat_` and has >= 32 bytes entropy.
   - Database check: Query `fc27_action_tokens`; raw token does NOT exist in any column; only SHA-256 hash exists.
2. `test_adv_t7_token_single_use_replay_fails_with_409(self, client, active_player, store)`:
   - Action 1: Consume token -> succeeds.
   - Action 2: Consume identical token -> HTTP 409 Conflict (`ActionTokenAlreadyConsumedError`).
3. `test_adv_t7_expired_token_fails_with_410(self, client, active_player, pg_uri)`:
   - Setup: Update token `expires_at = now() - interval '1 second'`.
   - Action: Consume token.
   - Assertion: HTTP 410 Gone (`ActionTokenExpiredError`).
4. `test_adv_t7_revoked_token_fails_with_410(self, client, active_player, store)`:
   - Setup: Revoke token via store (`revoked_at = now()`).
   - Action: Consume token.
   - Assertion: HTTP 410 Gone (`ActionTokenRevokedError`).
5. `test_adv_t7_scope_confusion_attack_rejected(self, client, store)`:
   - Setup: Issue token with `scope="invitation"`.
   - Action: Submit token to `/v1/player-assignments/{id}/decision` (`expected_scope="player_decision"`).
   - Assertion: HTTP 403 Forbidden (`ActionTokenScopeMismatchError`).
6. `test_adv_t7_cross_assignment_token_rejected(self, client, active_player, store)`:
   - Setup: Token bound to Assignment 1.
   - Action: Submit token on Assignment 2.
   - Assertion: HTTP 403 Forbidden (`ActionTokenAssignmentMismatchError`).

#### Track 8: Test Suite Design (`TestTrack8CommissionerBootstrapLockAdversarial`)
1. `test_adv_t8_bootstrap_ceremony_one_time_lock(self, client, pg_uri)`:
   - Setup: Clean database.
   - Action 1: `POST /v1/auth/bootstrap` with valid token -> HTTP 201 Created.
   - Action 2: `POST /v1/auth/bootstrap` with identical payload -> HTTP 409 Conflict (`commissioner bootstrap ceremony is permanently locked`).
2. `test_adv_t8_tampered_bootstrap_token_fails_with_401(self, client)`:
   - Action: `POST /v1/auth/bootstrap` with `bootstrap_token: "forged_token"`.
   - Assertion: HTTP 401 Unauthorized.
3. `test_adv_t8_empty_fields_fail_with_422(self, client)`:
   - Action: Submit with empty `login_identifier` or `display_name`.
   - Assertion: HTTP 422 Unprocessable Entity.
4. `test_adv_t8_direct_delete_on_bootstrap_lock_blocked(self, pg_uri)`:
   - Action: Execute `DELETE FROM public.fc27_bootstrap_locks`.
   - Assertion: Database raises check/trigger error via `fc27_reject_mutation()`.

#### Track 9: Test Suite Design (`TestTrack9CommissionerAccountRecoveryAdversarial`)
1. `test_adv_t9_recovery_code_format_and_zero_plaintext(self, store, pg_uri)`:
   - Action: Generate recovery codes for commissioner.
   - Assertion: Matches pattern `^fc27-[2-9a-hj-km-np-z]{4}-[2-9a-hj-km-np-z]{4}-[2-9a-hj-km-np-z]{4}$`.
   - Database check: Query `fc27_recovery_codes`; only `$argon2id$` hashes stored.
2. `test_adv_t9_consumed_code_replay_rejected(self, client, active_commissioner, store)`:
   - Action 1: Recover account using Code 1 -> HTTP 200.
   - Action 2: Recover account using Code 1 again.
   - Assertion: HTTP 400 Bad Request (`invalid_or_consumed_code`).
3. `test_adv_t9_regeneration_atomically_revokes_unconsumed_codes(self, client, active_commissioner, store)`:
   - Setup: Commissioner has Batch 1 codes.
   - Action: Commissioner calls `POST /v1/auth/recovery/regenerate`.
   - Action: Attempt recovery using unused Code from Batch 1.
   - Assertion: HTTP 400 Bad Request (`invalid_or_consumed_code`).
4. `test_adv_t9_disabled_and_suspended_accounts_fail_recovery(self, client, store)`:
   - Setup: User with status `disabled` and valid recovery code.
   - Action: Submit recovery.
   - Assertion: HTTP 403 Forbidden (`disabled account cannot be recovered`).

#### Track 10: Test Suite Design (`TestTrack10LegacyHmacKillSwitchAdversarial`)
1. `test_adv_t10_all_six_hmac_endpoints_reject_when_disabled(self, client)`:
   - Parameterized test verifying HTTP 403 `proof_type_disabled` on:
     1. `POST /v1/activation-gates/G-ID/challenge`
     2. `POST /v1/activation-gates/G-ID`
     3. `POST /v1/runs/{thread_id}/decision-challenge`
     4. `POST /v1/approval-requests/{id}/decisions` (body: `proof_type: "hmac"`)
     5. `POST /approval/packages/{package_id}/decide` (body: `proof_type: "hmac"`)
     6. `POST /v1/runs/{thread_id}/decision` (body: `proof_type: "hmac"`)
   - Assertion: All return HTTP 403 Forbidden with `"proof_type_disabled"`.
2. `test_adv_t10_webauthn_proof_type_succeeds_when_hmac_disabled(self, client, active_commissioner)`:
   - Setup: `settings.cli_hmac_enabled = False`.
   - Action: Submit decision with `proof_type: "webauthn"` and valid `authentication_evidence_id`.
   - Assertion: HTTP 200 OK; decision commits successfully.
3. `test_adv_t10_dynamic_toggle_reactivates_and_deactivates(self, client, monkeypatch)`:
   - Action 1: Enable HMAC -> decision challenge succeeds (200).
   - Action 2: Disable HMAC -> decision challenge returns 403 `proof_type_disabled`.

---

## 3. Caveats

1. **Read-Only Explorer Boundary**: As an exploration subagent, I did not write or edit production source code or test files in `tests/adversarial/` or `src/`. All test suites are fully designed and ready for the implementation agent (`adversarial_tester_phase2` / `e2e_test_writer_phase2_1`).
2. **PostgreSQL Real Database Dependency**: Several tests in Track 5 (triggers, `SELECT ... FOR UPDATE` concurrency) require an active PostgreSQL 16 container rather than in-memory SQLite mocks, as SQLite does not support PL/pgSQL triggers, `FOR UPDATE` locking, or PostgreSQL bytea/JSONB constraints.
3. **WebAuthn FIDO2 Signature Simulation**: WebAuthn assertions in opaque-box tests utilize compliant test authenticator data structures generated by `phase2_helpers.py`. They exercise the real `webauthn` library verification paths without requiring physical hardware authenticators.

---

## 4. Conclusion

1. **Architectural Posture**: The Phase 2 identity and authorization foundation implements robust defense-in-depth across all 10 security tracks. Core mechanisms (centralized `authorize()`, immutable database triggers, Argon2id recovery codes, Scoped Action Tokens, WebAuthn step-up evidence, and legacy HMAC kill switch) are properly integrated in `src/identity/` and `src/graph_api.py`.
2. **Identified Coverage Gaps**: While baseline unit and E2E Tier 1–4 tests verify positive flows and basic edge cases, there was no unified **Tier 5 Adversarial Coverage Hardening** test suite exercising concurrent race conditions, cross-player action token substitutions, cross-scope token confusion, live database trigger immutability enforcement, and comprehensive HMAC kill-switch matrix lockout.
3. **Actionable Deliverable**: The complete test suite specification defined above covers all 10 tracks across 45+ concrete test scenarios. Creating `tests/adversarial/test_adversarial_phase2.py` with these exact specifications will achieve 100% Tier 5 Adversarial Coverage Hardening for Phase 2 Milestone 6.

---

## 5. Verification Method

To independently verify the findings and validate the Tier 5 Adversarial test suite once implemented:

1. **Verify Static Layout & File Presence**:
   ```powershell
   # Inspect presence of identity foundation and router implementations
   Get-ChildItem -Path src/identity -Recurse -File
   Get-ChildItem -Path migrations -File
   ```

2. **Verify Database Trigger Posture**:
   ```sql
   -- Inspect all 7 security triggers on identity tables
   SELECT event_object_table, trigger_name, action_timing, event_manipulation 
   FROM information_schema.triggers 
   WHERE trigger_name LIKE 'fc27_%';
   ```

3. **Execute Tier 5 Adversarial Test Suite (Post-Implementation)**:
   ```powershell
   # Run the full adversarial hardening suite
   .\.venv\Scripts\python.exe -m pytest tests/adversarial/test_adversarial_phase2.py -v
   ```

4. **Verify Zero Regressions on Existing Suites**:
   ```powershell
   # Run Phase 2 Tier 1-4 E2E suites
   .\.venv\Scripts\python.exe -m pytest tests/e2e/test_phase2_*.py -v
   
   # Run Phase 1 regression gate
   .\.venv\Scripts\python.exe -m pytest tests/test_approval_security.py tests/test_langgraph_flow.py -v
   ```

5. **Invalidation Conditions**:
   - Any test allowing a player to query or decide another player's assignment invalidates Track 1.
   - Any test allowing a timeout to commit an approval decision invalidates Track 2.
   - Any test allowing a service key to execute a human ceremony invalidates Track 3.
   - Any test causing `dispatch.enabled=true` or delivery adapter execution invalidates Track 4.
   - Any direct SQL mutation bypassing immutability triggers invalidates Track 5.
   - Any reuse of `authentication_evidence_id` on a new decision invalidates Track 6.
   - Any acceptance of a mismatched or expired action token invalidates Track 7.
   - Any second bootstrap call returning anything other than HTTP 409 invalidates Track 8.
   - Any acceptance of a reused recovery code invalidates Track 9.
   - Any HMAC decision succeeding when `cli_hmac_enabled=False` invalidates Track 10.
