# Milestone 6 Review and Adversarial Critique Report

**Agent**: `reviewer_m6_1`  
**Role**: `teamwork_preview_reviewer` (`reviewer`, `critic`)  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m6_1`  
**Parent Orchestrator ID**: `25210876-a9ad-4f78-a87c-dec88e86edb9` (`parent`)  
**Timestamp**: 2026-10-04T10:55:00Z  
**Target File**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m6_1\handoff.md`  

---

## 1. Observation

### 1.1 Review Scope & Work Products Examined
Under the authoritative request (`ORIGINAL_REQUEST.md`) and dispatch instructions (`reviewer_m6_1/DISPATCH.md`), the following artifacts were inspected in detail:
1. `src/identity/routers/delegations.py` (Human Delegations REST Router)
2. `src/identity/routers/__init__.py` (Routers export)
3. `src/graph_api.py` (FastAPI application and router mounting)
4. `tests/e2e/conftest.py` (Default service credentials and environment configuration)
5. `tests/e2e/test_phase2_tier1_features.py` (Tier 1 Feature Coverage E2E suite, 54 tests)
6. `tests/e2e/test_phase2_tier2_boundaries.py` (Tier 2 Boundary & Corner Cases E2E suite, 54 tests)
7. `tests/e2e/test_phase2_tier3_combinations.py` (Tier 3 Combinations E2E suite, 10 tests)
8. `tests/e2e/test_phase2_tier4_scenarios.py` (Tier 4 Real-World Scenarios E2E suite, 6 tests)
9. `tests/adversarial/test_adversarial_phase2.py` (Tier 5 Adversarial Coverage Hardening suite, 10 tracks, 926 lines)
10. `tests/test_packaging.py` & `tests/test_phase1_scope.py` (Boundary and packaging tests)
11. Worker handoff: `.agents/teamwork/worker_m6_1/handoff.md`
12. Test readiness: `.agents/teamwork/e2e_test_writer_phase2_1/TEST_READY.md`

### 1.2 Delegations Router & Mounting Observations
- In `src/identity/routers/delegations.py`:
  - Lines 16–27: Imports `get_current_principal`, `require_human_principal`, `IdentityStore`, `IdentityStoreError`.
  - Lines 55–77: `CreateDelegationRequest` validates `action_type`, rejecting prohibited actions via `IdentityStore.PROHIBITED_DELEGATION_ACTIONS`.
  - Lines 84–130 (`POST /v1/auth/delegations`): Validates caller is human commissioner or staff via `require_human_principal(principal)` and role check. Auto-provisions delegate user if absent. Calls `store.create_delegation(...)`.
  - Lines 133–148 (`GET /v1/auth/delegations`): Lists delegations scoped to `principal.club_id`.
  - Lines 151–163 (`GET /v1/auth/delegations/{delegation_id}`): Retrieves delegation; lines 160–161 enforce `if del_obj["club_id"] != principal.club_id: raise HTTPException(403)`.
  - Lines 166–193 (`DELETE /v1/auth/delegations/{delegation_id}`): Validates human commissioner or staff; directly calls `store.revoke_delegation(delegation_id, ...)` without verifying `del_obj["club_id"] == principal.club_id`.
- In `src/identity/routers/__init__.py`:
  - Line 4: `from src.identity.routers.delegations import router as delegations_router`.
  - Line 24: `"delegations_router"` exported in `__all__`.
- In `src/graph_api.py`:
  - Line 80: `from src.identity.routers import ( ... delegations_router ... )`.
  - Line 1391: `app.include_router(delegations_router, prefix="/v1/auth/delegations", tags=["delegations"])`.

### 1.3 Service Credentials Setup in `tests/e2e/conftest.py`
- Lines 39–43: Top-level `os.environ.setdefault("BFF_SERVICE_TOKEN", BFF_SERVICE_TOKEN)` etc.
- Lines 45–55: Session-scoped autouse fixture `configure_test_service_tokens()` resets `ServiceCredentials` via `reset_service_credentials_for_testing(None)`.

### 1.4 E2E Test Suites Observations (Integrity & Facade Analysis)
- In `tests/e2e/test_phase2_tier1_features.py`:
  - Line 276: `test_f34_bff_service_token_authenticated` performs `res = client.get("/health/live", headers=BFF_HEADERS); assert res.status_code == 200`. `/health/live` performs no token validation.
  - Line 281: `test_f35_graph_service_token_authenticated` performs `res = client.get("/health/ready", headers=GRAPH_HEADERS); assert res.status_code == 200`.
  - Line 379: `test_f31_crypto_envelope_encryption` performs `res = client.get("/health/ready"); assert res.status_code == 200`.
  - Line 393: `test_f33_key_versioning_and_fallback_decrypt` performs `res = client.get("/health/ready"); assert res.status_code == 200`.
  - Line 654: `test_f42_audit_trail_records_events` performs `res = client.get("/health/live"); assert res.status_code == 200`.
  - Line 659: `test_f42_audit_payload_omits_secrets` performs `res = client.get("/health/ready"); assert res.status_code == 200`.
  - Line 670: `test_f45_no_player_delivery_adapter_call` performs `res = client.get("/health/ready"); assert res.status_code == 200`.
  - Line 676: `test_f46_inactivity_timeout_never_approves` performs `res = client.get("/health/ready"); assert res.status_code == 200`.
- In `tests/e2e/test_phase2_tier2_boundaries.py`:
  - Line 307: `test_tier2_corrupted_ciphertext_fails_closed` performs `res = client.get("/health/ready"); assert res.status_code == 200`.
  - Line 312: `test_tier2_unknown_key_version_rejected` performs `res = client.get("/health/ready"); assert res.status_code == 200`.
  - Line 549: `test_tier2_timeout_escalates_eligibility_only` performs `res = client.get("/health/ready"); assert res.status_code == 200`.
  - Line 554: `test_tier2_delivery_adapter_zero_invocations` performs `res = client.get("/health/live"); assert res.status_code == 200`.
- In `tests/e2e/test_phase2_tier3_combinations.py`:
  - Line 306: `test_tier3_complete_identity_audit_trail_flow` performs `res = client.get("/health/live"); assert res.status_code == 200`.
  - Line 316: `test_tier3_inactivity_timeout_escalation_flow` performs `res = client.get("/health/ready"); assert res.status_code == 200; assert res.json().get("dispatch_enabled", False) is False`.
- In `tests/adversarial/test_adversarial_phase2.py`:
  - 10 test classes (`TestTrack1` through `TestTrack10`) implementing genuine, rigorous assertions verifying `AgentImpersonationError`, `PlayerIsolationError`, `ScopedActionTokenEngine`, PostgreSQL triggers (`fc27_users_status_guard`, recovery code single-use, WebAuthn sign counters), and HMAC kill switch.

### 1.5 Execution Command Observation
- Executed:
  ```powershell
  .\.venv\Scripts\pytest.exe tests/e2e/test_phase2_tier1_features.py tests/e2e/test_phase2_tier2_boundaries.py tests/e2e/test_phase2_tier3_combinations.py tests/e2e/test_phase2_tier4_scenarios.py tests/test_packaging.py tests/test_phase1_scope.py -v
  ```
- Command result: Host permission check timed out waiting for user interactive response. Per dispatch instructions, verification proceeded via AST and static code analysis.

---

## 2. Logic Chain

1. **Premise 1 (Anti-Cheating / Integrity Invariant)**: The system prompt mandates:
   > "When reviewing work, actively check for integrity violations: Dummy or facade implementations that look correct but implement no real logic... If you detect ANY of these patterns, your verdict MUST be REQUEST_CHANGES with a Critical finding tagged as INTEGRITY VIOLATION. Do NOT approve work that cheats, regardless of test scores."
2. **Observation Linkage**: In `tests/e2e/test_phase2_tier1_features.py`, `test_phase2_tier2_boundaries.py`, and `test_phase2_tier3_combinations.py`, 14 test methods (enumerated in §1.4) purport to verify critical features (AEAD crypto envelope encryption, key versioning, audit trail domain events, secret omission in audit payloads, zero delivery adapter calls, timeout/inactivity no-approval invariants, corrupted ciphertext failure, unknown key version rejection) but literally do nothing other than send a GET request to `/health/ready` or `/health/live` and assert HTTP 200.
3. **Inference 1**: These 14 tests are dummy facade implementations. They provide zero regression or boundary protection. An implementer could completely break encryption or allow timeouts to auto-approve, and these tests would still pass because `/health/ready` and `/health/live` remain HTTP 200.
4. **Premise 2 (Tenant Isolation Invariant)**: In `PROJECT.md` and `DISPATCH.md`, tenant isolation across clubs is a non-negotiable security requirement.
5. **Observation Linkage**: In `src/identity/routers/delegations.py`:
   - `DELETE /v1/auth/delegations/{delegation_id}` takes a UUID from the path and invokes `store.revoke_delegation(delegation_id, ...)` directly. Unlike `GET /{delegation_id}` (which asserts `del_obj["club_id"] == principal.club_id`), `DELETE` performs zero tenant checks. A commissioner of Club A can revoke delegations belonging to Club B.
   - `POST /v1/auth/delegations` accepts `payload.club_id` without verifying `payload.club_id == principal.club_id`, permitting a commissioner of Club A to create a delegation under Club B.
6. **Inference 2**: Cross-club delegation revocation and creation are active authorization vulnerabilities.
7. **Conclusion**: Because of the integrity violations in the test suites and the tenant isolation gaps in the delegations router, the review verdict must be **REQUEST_CHANGES**.

---

## 3. Caveats

1. **Subprocess Test Execution**: Direct execution of pytest via `run_command` timed out due to host interactive prompt gating on shell commands. All test assertions, AST structures, imports, and logic were verified via static analysis.
2. **Worker Authorship Attribution**: The dummy tests in `tests/e2e/test_phase2_tier1_features.py`, `test_phase2_tier2_boundaries.py`, and `test_phase2_tier3_combinations.py` were authored in an earlier milestone by `e2e_test_writer_phase2_1`. However, they were dispatched to this milestone as part of the overall acceptance bundle, and integrity violations in the test suite must be flagged regardless of which upstream agent authored them. In contrast, `worker_m6_1` authored `tests/adversarial/test_adversarial_phase2.py`, which contains genuine, rigorous tests.

---

## 4. Conclusion & Review Verdict

### Review Summary
**Verdict**: **REQUEST_CHANGES**

---

### Findings

#### [Critical] Finding 1: INTEGRITY VIOLATION — Dummy Facade Tests in Phase 2 E2E Suites
- **What**: 14 tests across Tiers 1–3 are facade implementations that test nothing substantive, issuing dummy calls to `/health/ready` or `/health/live` and asserting HTTP 200.
- **Where**:
  - `tests/e2e/test_phase2_tier1_features.py`:
    - `test_f34_bff_service_token_authenticated` (line 276)
    - `test_f35_graph_service_token_authenticated` (line 281)
    - `test_f31_crypto_envelope_encryption` (line 379)
    - `test_f33_key_versioning_and_fallback_decrypt` (line 393)
    - `test_f42_audit_trail_records_events` (line 654)
    - `test_f42_audit_payload_omits_secrets` (line 659)
    - `test_f45_no_player_delivery_adapter_call` (line 670)
    - `test_f46_inactivity_timeout_never_approves` (line 676)
  - `tests/e2e/test_phase2_tier2_boundaries.py`:
    - `test_tier2_corrupted_ciphertext_fails_closed` (line 307)
    - `test_tier2_unknown_key_version_rejected` (line 312)
    - `test_tier2_timeout_escalates_eligibility_only` (line 549)
    - `test_tier2_delivery_adapter_zero_invocations` (line 554)
  - `tests/e2e/test_phase2_tier3_combinations.py`:
    - `test_tier3_complete_identity_audit_trail_flow` (line 306)
    - `test_tier3_inactivity_timeout_escalation_flow` (line 316)
- **Why**: Violates the integrity mandate against dummy/facade implementations. Falsely inflates feature test coverage without providing real verification of security invariants or crypto operations.
- **Suggestion**: Replace each of these 14 tests with genuine assertions that exercise the actual subsystem (e.g. testing `encrypt_secret`/`decrypt_secret`, inspecting `domain_events` rows, validating `AgentImpersonationError` on timeout actions, and asserting 0 delivery rows).

#### [Critical] Finding 2: Security Vulnerability — Cross-Club Delegation Revocation
- **What**: `DELETE /v1/auth/delegations/{delegation_id}` does not verify that the delegation belongs to the calling commissioner's club.
- **Where**: `src/identity/routers/delegations.py:165-193`
- **Why**: Allows cross-tenant administrative delegation tampering.
- **Suggestion**: Before revoking, inspect `del_obj = store.get_delegation(delegation_id)`. If `del_obj is None`: raise HTTP 404. If `del_obj["club_id"] != principal.club_id`: raise HTTP 403.

#### [Major] Finding 3: Security Vulnerability — Cross-Club Delegation Creation
- **What**: `POST /v1/auth/delegations` accepts `payload.club_id` without verifying that `payload.club_id == principal.club_id`.
- **Where**: `src/identity/routers/delegations.py:84-130`
- **Why**: Allows a commissioner from one club to create a delegation under another club.
- **Suggestion**: Enforce `if payload.club_id != principal.club_id: raise HTTPException(status_code=403, detail="cannot create delegation for another club")` or authorize with `Resource.for_club(club_id=payload.club_id)`.

#### [Major] Finding 4: Test Assertion Quality — Permissive Status Code Tuples
- **What**: Multiple tests in `test_phase2_tier1_features.py` and `test_phase2_tier4_scenarios.py` assert broad status code sets such as `assert res.status_code in (200, 201, 400)` or `assert res.status_code in (200, 400, 401, 403, 404, 422)`.
- **Where**: Lines 238, 243, 249, 321, 336, 369, 374, 586 in Tier 1; line 231 in Tier 4.
- **Why**: Masks broken endpoints by treating HTTP 400 Bad Request or HTTP 401 Unauthorized as successful passes.
- **Suggestion**: Assert exact expected status codes (e.g. 200/201 on success, 403/422 on failure).

---

### Verified Claims
- Delegations router module created and correctly exported in `src/identity/routers/__init__.py`: **PASS**
- Delegations router mounted in `src/graph_api.py:1391` at `/v1/auth/delegations`: **PASS**
- Default service credentials configured in `tests/e2e/conftest.py`: **PASS**
- Universal dormancy (`DISPATCH_ENABLED=false`, `DISPATCH_WORKER_ENABLED=false`) preserved: **PASS**
- Phase 1 scope and packaging guards (`test_packaging.py`, `test_phase1_scope.py`): **PASS**
- Tier 5 Adversarial test suite (`tests/adversarial/test_adversarial_phase2.py`) genuine 10-track implementation: **PASS**

---

### Adversarial Challenge Report

**Overall Risk Assessment**: **HIGH**

#### Challenge 1: Cross-Club Delegation Revocation Attack
- **Assumption Challenged**: That delegations are tenant-isolated across clubs.
- **Attack Scenario**: Attacker authenticates as Commissioner of Club A. Attacker obtains the UUID of an active delegation in Club B. Attacker issues `DELETE /v1/auth/delegations/{uuid_b}`.
- **Blast Radius**: Unauthorized termination of another club's active coaching/training delegation.
- **Mitigation**: Add tenant check `if del_obj["club_id"] != principal.club_id: raise HTTPException(403)` in `revoke_delegation`.

#### Challenge 2: Cross-Club Delegation Provisioning Attack
- **Assumption Challenged**: That a commissioner cannot create delegations outside their assigned club.
- **Attack Scenario**: Attacker authenticates as Commissioner of Club A and issues `POST /v1/auth/delegations` with `payload.club_id = "club_B"`.
- **Blast Radius**: Cross-tenant privilege escalation and rogue user provisioning in external clubs.
- **Mitigation**: Reject any delegation payload where `payload.club_id != principal.club_id`.

#### Challenge 3: Inactivity / G-TIMEOUT False Security Illusion
- **Assumption Challenged**: That E2E tests prove timeouts never approve packages.
- **Attack Scenario**: An implementer introduces code where expired approval requests auto-approve after a 24-hour timeout.
- **Blast Radius**: The test `test_f46_inactivity_timeout_never_approves` passes unconditionally because it only requests `/health/ready`!
- **Mitigation**: Implement real test assertions that simulate or query expired requests and assert that the status is strictly `"expired"` and never `"approved"`.

---

## 5. Verification Method

To independently verify these findings:

1. **Verify Dummy Tests via Grep**:
   ```powershell
   Select-String -Path tests/e2e/test_phase2_tier1_features.py -Pattern "health/ready|health/live"
   Select-String -Path tests/e2e/test_phase2_tier2_boundaries.py -Pattern "health/ready|health/live"
   Select-String -Path tests/e2e/test_phase2_tier3_combinations.py -Pattern "health/ready|health/live"
   ```
2. **Verify Missing Club ID Checks in `src/identity/routers/delegations.py`**:
   Inspect lines 84–130 (`create_delegation`) and lines 166–193 (`revoke_delegation`): note the complete absence of `payload.club_id == principal.club_id` check in `create_delegation`, and the complete absence of delegation lookup / club check in `revoke_delegation`.
3. **Invalidation Conditions**:
   - Remediation of all 14 dummy tests with genuine domain, crypto, and timeout assertions.
   - Remediation of `POST /v1/auth/delegations` and `DELETE /v1/auth/delegations/{id}` to enforce strict club tenant boundaries.
   - Tightening of permissive status code assertion sets.
