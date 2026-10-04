# TEST_INFRA — Phase 2: Identity and Authorization E2E Test Infrastructure

**Programme**: FC27 Player Dispatch  
**Phase**: 2 of 6  
**Document**: Test Infrastructure & Opaque-Box Architecture  
**Author**: e2e_test_writer_phase2_1  
**Status**: Authoritative Reference  

---

## 1. Executive Summary & Testing Philosophy

Phase 2 establishes authenticated human identities (Commissioner, Player, Staff), service identities (BFF, Graph, Worker), centralized object-level authorization, and cryptographic decision step-up while enforcing universal dispatch dormancy (`DISPATCH_ENABLED=false`, `DISPATCH_WORKER_ENABLED=false`).

The Phase 2 End-to-End (E2E) Test Suite adheres strictly to **Opaque-Box Testing Principles**:
1. **Public Interface Verification**: Tests interact exclusively via HTTP REST endpoints (`/v1/auth/*`, `/v1/player-assignments/*`, `/v1/approval-requests/*`, `/health/*`) and evaluate observable HTTP status codes, standard response payloads, and authoritative PostgreSQL persistence.
2. **Zero Internal White-Box Coupling**: Tests do not import internal private functions, patch internal variables, or depend on non-public implementation details.
3. **Dual-Track Testing Architecture**:
   - **Track A (Requirements & Functional Flow)**: Golden-path lifecycles, state machine transitions, role assignments, dual-expiry session lifecycles, and cryptographic assertion flows.
   - **Track B (Adversarial & Boundary Hardening)**: Boundary conditions, fuzz payloads, expired/tampered tokens, replay attempts, cross-tenant/cross-player access violations, privilege escalation attempts, service impersonation, and timeout-not-approval enforcement.
4. **Authoritative PostgreSQL Verification**: All claims of state change, credential persistence, session storage, and domain events are verified against PostgreSQL 16 append-only tables (`fc27_users`, `sessions`, `domain_events`, etc.).

---

## 2. Dual-Track Testing Tiers

The test suite is organized into 4 progressive tiers:

- **Tier 1: Feature Coverage (>=5 per functional domain)**  
  Covers all 46 features across 10 functional domains, providing deterministic positive and contract verification for every feature in `PROJECT.md`.
- **Tier 2: Boundary & Corner Cases (>=5 per functional domain)**  
  Exhaustive boundary testing across payload limits, empty/null inputs, expired timestamps, consumed single-use nonces/tokens, invalid characters, clock drift, and unauthorized roles.
- **Tier 3: Cross-Feature Combinations**  
  Pairwise interactions and state flows, such as Invitation -> Activation -> Passkey Registration -> Session Dual-Expiry -> Step-Up Challenge -> Decision Commit -> Session Rotation; Delegation -> Action Token -> Out-of-Scope Denial -> Revocation; Account Suspension -> Session Invalidation -> Login Lockout.
- **Tier 4: Real-World Scenarios**  
  Complete multi-step operational lifecycles: Full Commissioner Golden Path Lifecycle, Full Player Consent Lifecycle, Player Alternative Decision Outcomes (`reject`, `change_request`, `pause`, `acknowledge`), Human Delegation Administration & Execution Lifecycle, Commissioner Emergency Account Recovery Lifecycle, and Multi-Actor Adversarial Infiltration Lifecycle.

---

## 3. Comprehensive 46-Feature Mapping Matrix

| Feature # | Feature Name | Domain | Tier 1 Test Target | Tier 2 Boundary Target | Tier 3 Combination | Tier 4 Scenario |
|---|---|---|---|---|---|---|
| **F-01** | Authoritative Users Model | Domain 1: Users & Bootstrap | `test_f01_authoritative_users_creation` | `test_f01_user_invalid_club_format` | T3-01 (Activation flow) | T4-01, T4-02 |
| **F-02** | Account Status State Machine | Domain 1: Users & Bootstrap | `test_f02_account_status_transitions` | `test_f02_illegal_status_transition` | T3-06 (Suspension flow) | T4-05 (Recovery status) |
| **F-03** | Application Roles Model | Domain 1: Users & Bootstrap | `test_f03_application_roles_assignment` | `test_f03_unrecognized_role_rejected` | T3-01, T3-05 | T4-01, T4-02, T4-04 |
| **F-04** | Centralized `authorize()` Engine | Domain 4: Authorization & Isolation | `test_f04_centralized_authorize_evaluation` | `test_f04_missing_credentials_denied` | T3-03 (Delegation auth) | T4-01, T4-02, T4-06 |
| **F-05** | Object-Level Player Isolation | Domain 4: Authorization & Isolation | `test_f05_player_isolation_own_data` | `test_f05_cross_player_access_denied` | T3-04 (Player isolation) | T4-02, T4-06 |
| **F-06** | Invitation Issuance | Domain 2: Invitations | `test_f06_invitation_issuance_success` | `test_f06_invitation_empty_email_rejected` | T3-01, T3-05 | T4-02 (Player invite) |
| **F-07** | Invitation Revocation | Domain 2: Invitations | `test_f07_invitation_revocation_success` | `test_f07_revoke_already_revoked_invitation` | T3-01 (Revoke branch) | T4-06 |
| **F-08** | Invitation Acceptance / Activation | Domain 2: Invitations | `test_f08_invitation_acceptance_activates` | `test_f08_reused_invitation_token_fails` | T3-01 (Accept & activate) | T4-02 |
| **F-09** | Server-Side Session Storage | Domain 3: Sessions | `test_f09_session_creation_opaque_token` | `test_f09_unknown_session_token_rejected` | T3-01, T3-07 | T4-01, T4-02 |
| **F-10** | Dual-Expiry Session Lifecycle | Domain 3: Sessions | `test_f10_dual_expiry_idle_and_absolute` | `test_f10_expired_session_fails_closed` | T3-07 (Dual expiry test) | T4-01 |
| **F-11** | Session Rotation & Invalidation | Domain 3: Sessions | `test_f11_session_rotation_on_stepup` | `test_f11_old_session_invalidated_after_rotation` | T3-01, T3-02 | T4-01 |
| **F-12** | Administrative & User Logout | Domain 3: Sessions | `test_f12_user_and_admin_logout_revokes` | `test_f12_logout_already_revoked_session` | T3-06, T3-07 | T4-01, T4-02 |
| **F-13** | WebAuthn Registration Options | Domain 5: WebAuthn Passkeys | `test_f13_passkey_register_options` | `test_f13_options_without_auth_denied` | T3-01 (Registration) | T4-01, T4-02 |
| **F-14** | WebAuthn Registration Verification | Domain 5: WebAuthn Passkeys | `test_f14_passkey_register_verify_stores` | `test_f14_tampered_attestation_rejected` | T3-01 | T4-01, T4-02 |
| **F-15** | WebAuthn Sign-In Options | Domain 5: WebAuthn Passkeys | `test_f15_passkey_login_options_issued` | `test_f15_options_unknown_user_handled` | T3-01, T3-04 | T4-01, T4-02 |
| **F-16** | WebAuthn Sign-In Verification | Domain 5: WebAuthn Passkeys | `test_f16_passkey_login_verify_creates_session` | `test_f16_invalid_signature_login_fails` | T3-01, T3-04 | T4-01, T4-02 |
| **F-17** | Passkey Management | Domain 5: WebAuthn Passkeys | `test_f17_passkey_listing_and_revocation` | `test_f17_revoked_passkey_cannot_login` | T3-06 (Passkey revoke) | T4-05 |
| **F-18** | Decision Step-Up Challenge (Commissioner) | Domain 7: Step-Up & Replay | `test_f18_commissioner_stepup_options` | `test_f18_stepup_missing_package_binding` | T3-02 (Step-up commit) | T4-01 |
| **F-19** | Decision Step-Up Verification (Commissioner) | Domain 7: Step-Up & Replay | `test_f19_commissioner_stepup_verify_evidence` | `test_f19_tampered_stepup_assertion_fails` | T3-02 | T4-01 |
| **F-20** | Replay & Scope Lockout | Domain 7: Step-Up & Replay | `test_f20_stepup_evidence_single_use` | `test_f20_evidence_reused_on_other_package` | T3-02 (Lockout check) | T4-01, T4-06 |
| **F-21** | Player Decision Challenge | Domain 8: Player Consent | `test_f21_player_decision_challenge_issued` | `test_f21_challenge_wrong_player_rejected` | T3-04 (Consent flow) | T4-02, T4-03 |
| **F-22** | Player Decision Execution (`accept`) | Domain 8: Player Consent | `test_f22_player_decision_accept_commits` | `test_f22_accept_without_passkey_rejected` | T3-04 | T4-02 |
| **F-23** | Player Decision Execution (`reject`) | Domain 8: Player Consent | `test_f23_player_decision_reject_commits` | `test_f23_reject_wrong_assignment_fails` | T3-04 | T4-03 |
| **F-24** | Player Decision Execution (`change_request`) | Domain 8: Player Consent | `test_f24_player_decision_change_request` | `test_f24_change_request_empty_reason` | T3-04 | T4-03 |
| **F-25** | Player Decision Execution (`pause`) | Domain 8: Player Consent | `test_f25_player_decision_pause_commits` | `test_f25_pause_tampered_assignment_hash` | T3-04 | T4-03 |
| **F-26** | Player Decision Execution (`acknowledge`) | Domain 8: Player Consent | `test_f26_player_decision_acknowledge_receipt` | `test_f26_acknowledge_does_not_activate` | T3-04 | T4-03 |
| **F-27** | Scoped Action Tokens | Domain 8: Player Consent | `test_f27_scoped_action_tokens_issued` | `test_f27_expired_action_token_rejected` | T3-03, T3-04 | T4-02 |
| **F-28** | TOTP Enrollment | Domain 6: Staff TOTP & Crypto | `test_f28_totp_enrollment_presents_secret` | `test_f28_totp_unauthorized_role_rejected` | T3-05 (Staff TOTP) | T4-06 |
| **F-29** | TOTP Verification & Sign-In | Domain 6: Staff TOTP & Crypto | `test_f29_totp_verify_code_authenticates` | `test_f29_invalid_totp_code_rate_limited` | T3-05 | T4-06 |
| **F-30** | TOTP Revocation & Recovery | Domain 6: Staff TOTP & Crypto | `test_f30_totp_revocation_blocks_login` | `test_f30_revoked_totp_cannot_reverify` | T3-05 | T4-06 |
| **F-31** | Encryption at Rest (AEAD) | Domain 6: Staff TOTP & Crypto | `test_f31_crypto_envelope_encryption` | `test_f31_corrupted_ciphertext_fails_closed` | T3-05 | T4-01, T4-05 |
| **F-32** | Irreversible One-Way Hashing | Domain 6: Staff TOTP & Crypto | `test_f32_one_way_hashing_tokens_and_codes` | `test_f32_raw_token_not_persisted_in_db` | T3-01, T3-05 | T4-05 |
| **F-33** | Key Versioning & Rotation | Domain 6: Staff TOTP & Crypto | `test_f33_key_versioning_and_fallback_decrypt`| `test_f33_unknown_key_version_rejected` | T3-05 | T4-01 |
| **F-34** | BFF Service Identity | Domain 4: Authorization & Isolation | `test_f34_bff_service_token_authenticated` | `test_f34_bff_cannot_access_db_directly` | T3-04 | T4-06 (Non-impersonation) |
| **F-35** | Graph Service Identity | Domain 4: Authorization & Isolation | `test_f35_graph_service_token_authenticated`| `test_f35_graph_service_cannot_sign_human` | T3-02 | T4-01 |
| **F-36** | Worker & Ops Identities | Domain 4: Authorization & Isolation | `test_f36_worker_ops_scoped_identities` | `test_f36_worker_cannot_approve_decisions` | T3-02 | T4-06 |
| **F-37** | Explicit Human Delegation Creation | Domain 9: Delegations & Recovery | `test_f37_delegation_creation_scoped` | `test_f37_delegation_invalid_action_type` | T3-03 (Delegation) | T4-04 |
| **F-38** | Human Delegation Evaluation | Domain 9: Delegations & Recovery | `test_f38_delegation_evaluation_permits` | `test_f38_delegation_out_of_scope_denied` | T3-03 | T4-04 |
| **F-39** | Human Delegation Revocation | Domain 9: Delegations & Recovery | `test_f39_delegation_revocation_terminates` | `test_f39_revoked_delegation_fails_closed` | T3-03 | T4-04 |
| **F-40** | Commissioner Bootstrap Ceremony | Domain 1: Users & Bootstrap | `test_f40_bootstrap_ceremony_initializes` | `test_f40_bootstrap_ceremony_locks_permanent` | T3-01 | T4-01 |
| **F-41** | Commissioner Account Recovery | Domain 9: Delegations & Recovery | `test_f41_account_recovery_via_code` | `test_f41_recovery_code_single_use_enforced`| T3-06 | T4-05 |
| **F-42** | Comprehensive Identity Audit Trail | Domain 10: Invariants & Safety | `test_f42_audit_trail_records_events` | `test_f42_audit_payload_omits_secrets` | T3-09 (Full audit) | T4-01, T4-02, T4-05 |
| **F-43** | Authenticated Human Decision Endpoint | Domain 7: Step-Up & Replay | `test_f43_decision_with_webauthn_evidence` | `test_f43_decision_missing_evidence_denied` | T3-02 | T4-01 |
| **F-44** | Phase 1 CLI/HMAC Kill Switch | Domain 7: Step-Up & Replay | `test_f44_cli_hmac_disabled_rejects_hmac` | `test_f44_hmac_proof_rejected_when_off` | T3-08 (Kill switch) | T4-01 |
| **F-45** | Dispatch Safety Enforcement | Domain 10: Invariants & Safety | `test_f45_dispatch_remains_disabled_universal`| `test_f45_no_player_delivery_adapter_call` | T3-02, T3-04 | T4-01, T4-02, T4-03 |
| **F-46** | Inactivity / Timeout No-Approval Guard | Domain 10: Invariants & Safety | `test_f46_inactivity_timeout_never_approves` | `test_f46_timeout_escalates_eligibility_only`| T3-10 (Timeout test) | T4-06 |

---

## 4. Test Infrastructure Architecture

```
tests/e2e/
├── __init__.py
├── conftest.py                       # Shared test fixtures & database utilities
├── phase2_helpers.py                 # Phase 2 opaque-box client helpers & crypto fixtures
├── test_phase2_tier1_features.py     # Tier 1: 50+ Feature Coverage tests (Domains 1-10)
├── test_phase2_tier2_boundaries.py   # Tier 2: 50+ Boundary & Corner Case tests (Domains 1-10)
├── test_phase2_tier3_combinations.py # Tier 3: 10 Cross-Feature Interaction & Flow tests
└── test_phase2_tier4_scenarios.py    # Tier 4: 6 End-to-End Operational Lifecycle tests
```

### 4.1 Opaque-Box HTTP Testing Conventions
- All tests execute via `TestClient(app)` against standard versioned endpoints:
  - `/v1/auth/bootstrap`
  - `/v1/auth/invitations`
  - `/v1/auth/invitations/{token}/accept`
  - `/v1/auth/passkeys/register/options`
  - `/v1/auth/passkeys/register/verify`
  - `/v1/auth/passkeys/login/options`
  - `/v1/auth/passkeys/login/verify`
  - `/v1/auth/passkeys`
  - `/v1/auth/session`
  - `/v1/auth/sessions/{id}`
  - `/v1/auth/logout`
  - `/v1/auth/totp/enroll`
  - `/v1/auth/totp/verify`
  - `/v1/auth/step-up/options`
  - `/v1/auth/step-up/verify`
  - `/v1/auth/delegations`
  - `/v1/auth/delegations/{id}`
  - `/v1/auth/recovery/initiate`
  - `/v1/auth/recovery/verify`
  - `/v1/player-assignments/{id}/decision-challenge`
  - `/v1/player-assignments/{id}/decision`
  - `/v1/approval-requests/{id}/decisions`
- Authentication is passed via HTTP request headers:
  - Bearer Session Token: `Authorization: Bearer <opaque_session_token>`
  - Service Tokens: `X-API-Key: <service_token>`
- WebAuthn ceremonies are driven via compliant FIDO2 test assertions conforming to W3C Web Authentication specification.
- Timeouts and inactivity are verified to never mutate decision outcomes.

---

## 5. Execution & Verification Commands

```powershell
# Run the complete Phase 2 E2E suite
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_phase2_*.py -v

# Run individual tiers
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_phase2_tier1_features.py -v
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_phase2_tier2_boundaries.py -v
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_phase2_tier3_combinations.py -v
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_phase2_tier4_scenarios.py -v

# Run full repository suite including Phase 1 regression gate
.\.venv\Scripts\python.exe -m pytest tests/e2e/ -v
```
