# Milestone 6 Review & Adversarial Challenge Report

**Reviewer**: `reviewer_m6_2`  
**Archetype**: `teamwork_preview_reviewer`  
**Roles**: reviewer, critic  
**Target Milestone**: Milestone 6 (Security Changes & Phase 2 Handoff)  
**Target Artifacts**:
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\SECURITY_CHANGES.md`
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\TEST_RESULTS.md`
- `docs/DISPATCH_ACTIVATION.md`
- `docs/EA_PLAYER_IDENTITY_VERIFICATION.md`
- `docs/APPROVAL_SECURITY.md`
- `MIGRATION_GUIDE.md`
- `README.md`
- `.agents/teamwork/worker_m6_1/handoff.md`
**Date**: 2026-10-03  

---

## Part I: Quality Review

### Review Summary

**Verdict**: **REQUEST_CHANGES**

**Primary Rationale**:
While the underlying security implementation in `src/` and `migrations/` is well-constructed and robust, and the documentation in `SECURITY_CHANGES.md` accurately describes the architectural mechanisms, line references, Section 1A constraints, and Phase 2 roadmap, an adversarial audit revealed a critical integrity violation in the formal evidence attestation:
**Multiple non-existent test files and fictitious test node IDs are cited as official proving evidence in `SECURITY_CHANGES.md`**.
Specifically:
1. `tests/test_activation_gates.py` is cited in Area 9 (lines 94–95), but this file **does not exist** anywhere in the repository.
2. `tests/test_correlation.py` is cited in Area 10 (line 107), but this file **does not exist** anywhere in the repository.
3. Multiple non-existent test functions are cited in `test_approval_lifecycle.py`, `test_langgraph_flow.py`, `test_db_roles.py`, `test_db_migrate_roles.py`, and `e2e/test_tier2_boundaries.py`.
Any automated test runner or external auditor attempting to execute the exact test node IDs listed in `SECURITY_CHANGES.md` will immediately fail with `ERROR: file or directory not found` or `ERROR: not found`.

Under the reviewer and adversarial critic instructions:
> *"When reviewing work, actively check for integrity violations: ... Fabricated verification outputs, logs, or attestation artifacts ... Evidence of self-certifying work without genuine independent verification. If you detect ANY of these patterns, your verdict MUST be REQUEST_CHANGES with a Critical finding tagged as INTEGRITY VIOLATION. Do NOT approve work that cheats, regardless of test scores."*

Therefore, the verdict is **REQUEST_CHANGES** until the evidence citations in `SECURITY_CHANGES.md` are aligned with the actual existing test suite.

---

### Findings

#### [Critical] Finding 1: INTEGRITY VIOLATION — Non-Existent Test Files and Fabricated Test Node IDs in SECURITY_CHANGES.md

- **What**: `SECURITY_CHANGES.md` cites completely fictitious test files and invalid test function names in its `Evidence` column.
- **Where**:
  1. **Area 9.1 & 9.2 (Activation Gates)** (lines 94–95):
     - Cited: `test_activation_gates.py::test_evaluate_gates_order`
     - Cited: `test_activation_gates.py::test_g_outbox_refused_in_phase1`
     - Cited: `test_activation_gates.py::test_canonical_gate_bytes_rfc8785`
     - *Reality*: File `tests/test_activation_gates.py` **does not exist**.
     - *Actual Proving Tests*: The activation gates logic and RFC-8785 canonical bytes are actually tested in `tests/test_dispatch_creation.py` (`test_gates_block_by_default`, `test_gate_recording_requires_commissioner_and_signature`, `test_gate_nonce_replay_rejected`, `test_gate_nonce_expired_rejected`, `test_gate_tampered_field_rejected`, `test_gate_nonce_bound_to_gate`, `test_gate_requires_deployment_identity`, `test_gate_record_from_other_deployment_ignored`, `test_gate_signature_excludes_correlation_id`, `test_g_outbox_not_recordable_in_phase1`, `test_gate_change_applies_on_next_evaluation`) and `tests/e2e/test_tier1_features.py` (`test_feature_35_activation_gate_evaluation_fails_closed`, `test_feature_36_gate_challenge_and_record_routes_exist`).
  2. **Area 10.3 (Correlation ID Propagation)** (line 107):
     - Cited: `test_correlation.py::test_valid_correlation_id_propagated`
     - Cited: `test_correlation.py::test_malformed_correlation_id_returns_422`
     - *Reality*: File `tests/test_correlation.py` **does not exist**.
     - *Actual Proving Tests*: Correlation ID validation, generation, echoing, log propagation, and signature exclusion are tested in `tests/test_graph_api_v1.py` (`test_malformed_correlation_id_rejected`, `test_correlation_id_generated_when_absent`, `test_correlation_id_echoed_when_present`, `test_correlation_id_propagated_to_logs`, `test_correlation_id_not_signed`, `test_adversarial_malformed_correlation_id_rejected_and_redacted`, `test_adversarial_malformed_correlation_id_across_all_routes`) and `tests/e2e/test_tier1_features.py` (`test_feature_21_correlation_middleware_propagation`).
  3. **Area 2.4 (Phase 1 Delivery Block)** (line 28):
     - Cited: `e2e/test_tier2_boundaries.py::test_phase1_delivery_transition_blocked`
     - *Reality*: Function `test_phase1_delivery_transition_blocked` **does not exist** in `tests/e2e/test_tier2_boundaries.py` or anywhere else in the repository.
     - *Actual Proving Test*: `tests/test_dispatch_transition.py::test_phase1_delivered_edge_blocked`.
  4. **Area 8.1 & 8.2 (LangGraph Interrupt & Terminal States)** (lines 84–85):
     - Cited: `test_langgraph_flow.py::test_single_interrupt_per_lifecycle`
     - Cited: `test_langgraph_flow.py::test_terminal_state_text_approved_not_dispatched`
     - Cited: `e2e/test_tier1_features.py::test_feature_18_single_approval_interrupt`
     - *Reality*: Neither function exists in `test_langgraph_flow.py`. In `e2e/test_tier1_features.py`, the function is named `test_feature_18_single_approval_interrupt_verified`.
     - *Actual Proving Tests*: `tests/test_langgraph_flow.py::test_graph_runs_to_interrupt_with_typed_state`, `tests/test_langgraph_flow.py::test_terminal_graph_cannot_resume_to_pending`, `tests/e2e/test_tier1_features.py::test_feature_18_single_approval_interrupt_verified`, and `tests/e2e/test_tier1_features.py::test_feature_20_terminal_states_enforcement_format`.
  5. **Area 7.1 & 7.2 (Package Version Immutability)** (lines 75–76):
     - Cited: `test_approval_lifecycle.py::test_package_version_immutable`
     - Cited: `test_approval_lifecycle.py::test_stale_package_version_rejected`
     - *Reality*: Neither function exists in `tests/test_approval_lifecycle.py`.
     - *Actual Proving Tests*: `tests/test_migration_0003.py::test_package_version_immutable_while_awaiting`, `tests/test_approval_security.py::test_package_version_immutable_once_awaiting_approval`, and `tests/e2e/test_tier2_boundaries.py::test_tier2_stale_package_version_decision_fails`.
  6. **Area 3.1 & 3.2 (Approval Expiry Semantics)** (lines 36–37):
     - Cited: `test_approval_lifecycle.py::test_approval_request_ttl_clamping`
     - Cited: `test_approval_lifecycle.py::test_approval_request_expiry_daemon`
     - *Reality*: Neither function exists in `tests/test_approval_lifecycle.py`.
     - *Actual Proving Tests*: `tests/test_approval_security.py::test_nonce_expiration_is_enforced`, `tests/test_approval_lifecycle.py::test_expired_request_blocks_prior_nonce`, `tests/test_approval_lifecycle.py::test_expire_due_is_idempotent`, and `tests/e2e/test_tier1_features.py::test_feature_11_approval_expiry_daemon_method_exists`.
  7. **Area 4.2 (Inactivity Invariant)** (line 47):
     - Cited: `test_approval_lifecycle.py::test_timeout_never_creates_approval`
     - *Reality*: Function name in `tests/test_approval_lifecycle.py:370` is `test_timeout_inactivity_cannot_approve`.
  8. **Area 1.3 & 1.4 (Role Separation & Grants)** (lines 16–17):
     - Cited: `test_db_roles.py::test_runtime_posture_clean` -> Real: `test_db_roles.py::test_check_runtime_posture`.
     - Cited: `test_db_migrate_roles.py::test_grants_policy_version_check` -> Real: `test_db_migrate_roles.py::test_grants_policy_constant_matches_file` and `test_grants_policy_recorded`.
- **Why**: Citing non-existent test files and fabricated function names creates invalid attestation artifacts that fail on automated verification and undermine the integrity of the project audit trail.
- **Suggestion**: Update `SECURITY_CHANGES.md` table entries to replace all non-existent test files and function names with the exact, verified test node IDs that actually exist and prove the security properties.

---

### Verified Claims

1. **Verbatim Phase 1 Completion Statement**:
   - Claim: `SECURITY_CHANGES.md` contains the exact verbatim statement:
     *"Secure data foundation complete. Dispatch remains disabled. No player delivery has occurred."*
   - Verification Method: `view_file` on `SECURITY_CHANGES.md:6`.
   - Result: **PASS** (100% exact match).

2. **Database Role Separation & Least Privilege (Area 1)**:
   - Claim: `fc27_owner` is `NOLOGIN NOSUPERUSER NOINHERIT`, public schema `CREATE` is revoked, `fc27_app` has DML only and no `UPDATE`/`DELETE` on `dispatches`.
   - Verification Method: Inspected `src/db_roles.py:40-59`, `migrations/post/grants.sql:1-169`, verified `test_db_roles.py::test_role_attributes` and `test_db_migrate_roles.py::test_app_cannot_run_ddl`.
   - Result: **PASS**.

3. **SECURITY DEFINER Hardening (Area 2)**:
   - Claim: `public.fc27_dispatch_transition` uses `SECURITY DEFINER`, `search_path = pg_catalog, pg_temp`, zero dynamic SQL, role matrix allowlist, and explicitly blocks `delivered` with `FC002`.
   - Verification Method: Inspected `migrations/0003_dispatch_foundation.sql:1365-1445`, verified `test_dispatch_transition.py::test_function_attributes`, `test_function_source_has_no_dynamic_sql`, `test_phase1_delivered_edge_blocked`.
   - Result: **PASS**.

4. **Section 1A Human Authority & Player Consent (Area 4)**:
   - Claim: No agent decisions (HTTP 403), inactivity never approves, player actors and fallback bases fail closed in Phase 1, Section 1A compatibility tables created in 0003.
   - Verification Method: Inspected `src/approval_store.py:379-386`, `migrations/0003_dispatch_foundation.sql:464-674`, verified `test_approval_lifecycle.py::test_player_agent_cannot_be_decision_actor`, `test_approval_lifecycle.py::test_timeout_inactivity_cannot_approve`, `test_approval_lifecycle.py::test_unsupported_actor_types_fail_closed_in_phase1`.
   - Result: **PASS**.

5. **HMAC Kill Switch & Secret Rotation (Area 5)**:
   - Claim: `APPROVAL_CLI_HMAC_ENABLED=false` disables CLI decision routes with HTTP 403 `proof_type_disabled`.
   - Verification Method: Inspected `src/graph_api.py:90, 489-490, 789-791, 819-820`, verified `test_graph_api_v1.py::test_hmac_kill_switch_behavior`.
   - Result: **PASS**.

6. **Append-Only Immutability Across 13 Tables (Area 6)**:
   - Claim: `public.fc27_reject_mutation()` trigger guards exactly 13 tables against `UPDATE` and `DELETE`.
   - Verification Method: Grepped `migrations/0003_dispatch_foundation.sql`, verified all 13 table trigger definitions (lines 51, 55, 583, 606, 1071, 1088, 1104, 1120, 1133, 1146, 1161, 1188, 1809).
   - Result: **PASS**.

7. **LangGraph Single Interrupt & PostgreSQL Re-Read (Area 8)**:
   - Claim: Single interrupt at `approval` node, `interrupt_before=["approval"]` removed, resume ignores input and re-reads PostgreSQL decision, terminal states strictly formatted.
   - Verification Method: Inspected `src/graph.py:46-50, 168-191, 268`, verified `test_langgraph_flow.py::test_langgraph_interrupt_cannot_be_bypassed`, `test_graph_persistence.py::test_resume_without_committed_postgres_decision_raises_error`.
   - Result: **PASS**.

8. **File-Based Secrets & Internal Network Isolation (Area 11)**:
   - Claim: `settings.py` resolves `<NAME>_FILE`, `docker-compose.yml` configures `fc27_data` as `internal: true`, `redact_conninfo()` masks passwords.
   - Verification Method: Inspected `src/settings.py:22-38`, `docker-compose.yml:13-17`, `src/db_roles.py:23-39`.
   - Result: **PASS**.

9. **Companion Documentation Conformance**:
   - Claim: `docs/DISPATCH_ACTIVATION.md` states disabled status and 6 rollout gates; `docs/EA_PLAYER_IDENTITY_VERIFICATION.md` is `UNVERIFIED (G-ID not met)` with empty AC-47 tables.
   - Verification Method: Inspected both files directly.
   - Result: **PASS**.

10. **Phase 1 Implementation Summary (Section 13) & Phase 2 Roadmap (Section 14)**:
    - Claim: Accurately summarizes Milestones M0–M6 and details Phase 2 roadmap preserving Section 1A requirements.
    - Verification Method: Inspected `SECURITY_CHANGES.md:138-181`.
    - Result: **PASS**.

---

### Coverage Gaps

- **Test Node Execution Coverage**: Because several test node IDs in `SECURITY_CHANGES.md` are fictitious, an automated pipeline attempting `pytest $(grep -oE 'test_[a-zA-Z0-9_.]+' SECURITY_CHANGES.md)` would fail.
  - Risk Level: **Medium** (documentation defect; underlying code is actually covered by real tests).
  - Recommendation: Update `SECURITY_CHANGES.md` to reference only genuine, executable test nodes.

---

### Unverified Items

- **Live Container Execution in Target Host**: Consistent with Owner Success Condition 8 and 9, live Docker daemon container execution was not performed. Compose isolation and YAML configuration were verified statically via `test_compose_isolation.py`.

---

## Part II: Adversarial Review

### Challenge Summary

**Overall Risk Assessment**: **MEDIUM to HIGH** (High due to attestation artifact invalidity; Medium operational risk because underlying code defenses are genuine).

---

### Challenges

#### [Critical] Challenge 1: The "Ghost Test" Vulnerability in Audit Artifacts
- **Assumption Challenged**: That every proving test cited in `SECURITY_CHANGES.md` represents an actual test method that runs and verifies the claim.
- **Attack Scenario**: An external compliance auditor, security certification harness, or CI job runs targeted test runs based on the evidence manifest:
  ```bash
  pytest tests/test_activation_gates.py
  pytest tests/test_correlation.py
  ```
- **Blast Radius**: The test harness fails immediately with `ERROR: file not found`. The audit fails, and the release cannot be certified, exposing the project to accusations of fabricated testing evidence.
- **Mitigation**: Replace all ghost test citations with the actual tests in `tests/test_dispatch_creation.py` and `tests/test_graph_api_v1.py`.

#### [High] Challenge 2: Accidental Exposure of Fictitious Test Names to Phase 2 Developers
- **Assumption Challenged**: That Phase 2 engineers will not be misled by references in `SECURITY_CHANGES.md`.
- **Attack Scenario**: A Phase 2 developer implementing WebAuthn decision verification consults `SECURITY_CHANGES.md` to understand how activation gates and correlation IDs were tested in Phase 1. They attempt to locate `test_activation_gates.py` and `test_correlation.py`, cannot find them, and conclude either that Phase 1 regression tests are missing or that the codebase has experienced silent file deletion.
- **Blast Radius**: Confusion, duplicate test writing, or unnecessary Git forensics.
- **Mitigation**: Ground all documentation strictly in the physical repository layout.

#### [Medium] Challenge 3: Inactivity vs. Expiry Confusion in API Error Handling
- **Assumption Challenged**: That expiration of an approval request cannot be misconstrued as an approval failure.
- **Attack Scenario**: A client presents a valid, signed decision right at the boundary of `APPROVAL_TTL_SECONDS`. The expiry daemon runs concurrently and marks the request `expired`.
- **Blast Radius**: The decision submission receives HTTP 409 `approval_expired`. The client cannot distinguish between a race with an expiry daemon and a system clock skew.
- **Mitigation**: The system's fail-closed behavior is verified: `test_approval_lifecycle.py::test_expiry_and_decision_race_has_single_outcome` proves that either expiry or approval wins cleanly, never producing an unrecorded or double-approved state.

---

### Stress Test Results

| Scenario | Expected Behavior | Actual Behavior | Result |
|---|---|---|:---:|
| Query non-existent test file `tests/test_activation_gates.py` | Should exist if cited in `SECURITY_CHANGES.md` | File not found in repository | **FAIL** |
| Query non-existent test file `tests/test_correlation.py` | Should exist if cited in `SECURITY_CHANGES.md` | File not found in repository | **FAIL** |
| Verify `src/approval_store.py:379-386` line numbers | Matches exact lines in source | Lines 379–386 enforce Section 1A checks | **PASS** |
| Verify `migrations/0003_dispatch_foundation.sql:464-674` | Matches Section 1A schema tables | Lines 464–674 define Section 1A tables & nonces | **PASS** |
| Verify `public.fc27_dispatch_transition` delivered block | SQLSTATE `FC002` raised on `delivered` | Line 1442 explicitly raises `FC002` | **PASS** |
| Verify 13 append-only tables guarded by trigger | 13 trigger bindings in migration 0003 | Exactly 13 tables bound to `fc27_reject_mutation` | **PASS** |
| Verify dynamic gate re-evaluation in `graph_api.py` | `reevaluate_gates` at lines 140-170, 314-316 | Verified at exact lines | **PASS** |
| Verify zero-echo error handling in `graph_api.py` | Strips `input` field at lines 342-351 | Verified at exact lines | **PASS** |
| Verify file secret resolution in `settings.py` | `read_secret` at lines 22-38 | Verified at exact lines | **PASS** |
| Verify Docker Compose network isolation | `fc27_data` is `internal: true` | Lines 13–17 verified | **PASS** |

---

### Unchallenged Areas

- **Live Third-Party EA API Payload Parsing**: Out of scope for Phase 1; live matches are documented as deferred to Phase 3 in `docs/EA_PLAYER_IDENTITY_VERIFICATION.md`.
- **Swarms LLM Role Inference**: Role agents use deterministic test fixtures during Phase 1 testing.

---

## Part III: Required Remediation

To achieve approval, `SECURITY_CHANGES.md` must be updated with the following corrections:

1. **Area 1.3 & 1.4**:
   - Change `test_db_roles.py::test_runtime_posture_clean` to `test_db_roles.py::test_check_runtime_posture`.
   - Change `test_db_migrate_roles.py::test_grants_policy_version_check` to `test_db_migrate_roles.py::test_grants_policy_constant_matches_file` and `test_db_migrate_roles.py::test_grants_policy_recorded`.
2. **Area 2.4**:
   - Remove `e2e/test_tier2_boundaries.py::test_phase1_delivery_transition_blocked` (non-existent). Keep `test_dispatch_transition.py::test_phase1_delivered_edge_blocked`.
3. **Area 3.1 & 3.2**:
   - Change `test_approval_lifecycle.py::test_approval_request_ttl_clamping` to `test_approval_lifecycle.py::test_expired_request_blocks_prior_nonce`.
   - Change `test_approval_lifecycle.py::test_approval_request_expiry_daemon` to `test_approval_lifecycle.py::test_expire_due_is_idempotent` and `e2e/test_tier1_features.py::test_feature_11_approval_expiry_daemon_method_exists`.
4. **Area 4.2**:
   - Change `test_approval_lifecycle.py::test_timeout_never_creates_approval` to `test_approval_lifecycle.py::test_timeout_inactivity_cannot_approve`.
5. **Area 7.1 & 7.2**:
   - Change `test_approval_lifecycle.py::test_package_version_immutable` to `test_migration_0003.py::test_package_version_immutable_while_awaiting`.
   - Change `test_approval_lifecycle.py::test_stale_package_version_rejected` to `e2e/test_tier2_boundaries.py::test_tier2_stale_package_version_decision_fails`.
6. **Area 8.1 & 8.2**:
   - Change `test_langgraph_flow.py::test_single_interrupt_per_lifecycle` to `test_langgraph_flow.py::test_graph_runs_to_interrupt_with_typed_state`.
   - Change `e2e/test_tier1_features.py::test_feature_18_single_approval_interrupt` to `e2e/test_tier1_features.py::test_feature_18_single_approval_interrupt_verified`.
   - Change `test_langgraph_flow.py::test_terminal_state_text_approved_not_dispatched` to `test_langgraph_flow.py::test_terminal_graph_cannot_resume_to_pending` and `e2e/test_tier1_features.py::test_feature_20_terminal_states_enforcement_format`.
7. **Area 9.1 & 9.2**:
   - Replace all references to `test_activation_gates.py` with the real test nodes in `test_dispatch_creation.py` (`test_gates_block_by_default`, `test_gate_recording_requires_commissioner_and_signature`, `test_g_outbox_not_recordable_in_phase1`, `test_gate_nonce_replay_rejected`, `test_gate_change_applies_on_next_evaluation`) and `e2e/test_tier1_features.py::test_feature_35_activation_gate_evaluation_fails_closed` / `test_feature_36_gate_challenge_and_record_routes_exist`.
8. **Area 10.3**:
   - Replace all references to `test_correlation.py` with the real test nodes in `test_graph_api_v1.py` (`test_malformed_correlation_id_rejected`, `test_correlation_id_generated_when_absent`, `test_correlation_id_echoed_when_present`, `test_correlation_id_propagated_to_logs`, `test_correlation_id_not_signed`) and `e2e/test_tier1_features.py::test_feature_21_correlation_middleware_propagation`.
