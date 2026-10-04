# Milestone 6 Iteration 2 Review & Adversarial Challenge Report

**Reviewer**: `reviewer_m6_it2_2`  
**Archetype**: `teamwork_preview_reviewer`  
**Roles**: reviewer, critic  
**Target Milestone**: Milestone 6 Iteration 2 (Security Documentation Realignment & Phase 2 Handoff)  
**Target Artifacts**:
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\SECURITY_CHANGES.md`
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\TEST_RESULTS.md`
- `docs/DISPATCH_ACTIVATION.md`
- `docs/EA_PLAYER_IDENTITY_VERIFICATION.md`
- `tests/e2e/test_tier1_features.py`
- `tests/e2e/test_tier2_boundaries.py`
- `tests/e2e/test_tier4_scenarios.py`
- `.agents/teamwork/worker_m6_2/handoff.md`
- `.agents/teamwork/reviewer_m6_2/review.md`  
**Date**: 2026-10-03  

---

## Part I: Quality Review

### Review Summary

**Verdict**: **REQUEST_CHANGES**

**Primary Rationale**:
Milestone 6 Iteration 2 worker remediation made substantial, genuine progress:
1. The non-existent test files flagged in Iteration 1 (`test_activation_gates.py` and `test_correlation.py`) were completely removed from `SECURITY_CHANGES.md`.
2. The tautological assertions and permissive stubs in `tests/e2e/test_tier1_features.py` (Feature 28 HMAC kill switch, Feature 37 atomic dispatch creation), `tests/e2e/test_tier2_boundaries.py` (empty payload validation, objective bounds policy validation), and `tests/e2e/test_tier4_scenarios.py` (Section 1A authority separation and check constraint validation) were genuinely hardened with robust invariant checks.
3. `TEST_RESULTS.md` was successfully realigned to reflect exactly 81 AST E2E tests and authentic class names.
4. The verbatim Phase 1 Completion Statement is intact across all required documents.
5. All 11 security hardening areas are fully documented.
6. Companion documents (`docs/DISPATCH_ACTIVATION.md` and `docs/EA_PLAYER_IDENTITY_VERIFICATION.md`) are completely aligned with Phase 1 invariants.

**However, an exhaustive adversarial audit of all 87 test citations in `SECURITY_CHANGES.md` revealed that 4 test function citations still reference non-existent test functions**:
- In **Area 1.3** (line 16): `tests/test_graph_api_v1.py::test_health_ready_checks_db_posture` does not exist. (The real test is `tests/test_graph_api_v1.py::test_adversarial_readiness_fails_closed_on_db_role_posture_violation` at line 719).
- In **Area 6.1** (line 66): `tests/test_dispatch_schema.py::test_append_only_triggers` does not exist. (The real test is `tests/test_dispatch_schema.py::test_tracking_tables_append_only` at line 261).
- In **Area 8.3** (line 86): `tests/test_graph_persistence.py::test_restart_recovers_interrupted_graph` does not exist. (The real test is `tests/test_graph_persistence.py::test_checkpoint_recovery_across_server_restart` at line 209).
- In **Area 8.3** (line 86): `tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_post_commit_resume_failure_recovery` does not exist. (The real test is `tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_post_commit_resume_failure_preserves_authoritative_decision` at line 79).

Any automated compliance harness or auditor attempting to run `pytest <citation>` on these 4 test nodes will fail with `ERROR: not found`. Under the reviewer and adversarial critic instructions:
> *"When reviewing work, actively check for integrity violations: ... Fabricated verification outputs, logs, or attestation artifacts ... Evidence of self-certifying work without genuine independent verification. If you detect ANY of these patterns, your verdict MUST be REQUEST_CHANGES with a Critical finding tagged as INTEGRITY VIOLATION. Do NOT approve work that cheats, regardless of test scores."*

Therefore, the verdict is **REQUEST_CHANGES** with a single Critical finding. All 4 replacements have been identified and verified in the existing test suite, so this can be remediated in a single, precise edit.

---

### Findings

#### [Critical] Finding 1: INTEGRITY VIOLATION — Non-Existent Test Function Citations in `SECURITY_CHANGES.md`

- **What**: Four citations in `SECURITY_CHANGES.md` reference test functions that do not exist in the codebase.
- **Where**:
  1. **Area 1.3 (Runtime Application Least Privilege)** (line 16):
     - *Cited*: `tests/test_graph_api_v1.py::test_health_ready_checks_db_posture`
     - *Reality*: Function `test_health_ready_checks_db_posture` does not exist anywhere in `tests/test_graph_api_v1.py` or the repository.
     - *Actual Proving Test*: `tests/test_graph_api_v1.py::test_adversarial_readiness_fails_closed_on_db_role_posture_violation` (defined at line 719 of `tests/test_graph_api_v1.py`).
  2. **Area 6.1 (Mutation Rejection Trigger)** (line 66):
     - *Cited*: `tests/test_dispatch_schema.py::test_append_only_triggers`
     - *Reality*: Function `test_append_only_triggers` does not exist anywhere in `tests/test_dispatch_schema.py` or the repository.
     - *Actual Proving Test*: `tests/test_dispatch_schema.py::test_tracking_tables_append_only` (defined at line 261 of `tests/test_dispatch_schema.py`).
  3. **Area 8.3 (Pre-Resume Commit & Reconciliation)** (line 86):
     - *Cited*: `tests/test_graph_persistence.py::test_restart_recovers_interrupted_graph`
     - *Reality*: Function `test_restart_recovers_interrupted_graph` does not exist in `tests/test_graph_persistence.py`.
     - *Actual Proving Test*: `tests/test_graph_persistence.py::test_checkpoint_recovery_across_server_restart` (defined at line 209 of `tests/test_graph_persistence.py`).
  4. **Area 8.3 (Pre-Resume Commit & Reconciliation)** (line 86):
     - *Cited*: `tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_post_commit_resume_failure_recovery`
     - *Reality*: Method `test_tier3_post_commit_resume_failure_recovery` does not exist in `TestCrossFeatureCombinations`.
     - *Actual Proving Test*: `tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_post_commit_resume_failure_preserves_authoritative_decision` (defined at line 79 of `tests/e2e/test_tier3_combinations.py`).
- **Why**: Citing non-existent test functions in formal audit and attestation documents invalidates automated regression harnesses (`pytest <node_id>`), misleading auditors and downstream Phase 2 engineers.
- **Suggestion**: Replace these 4 invalid citations with their verified existing counterparts in `SECURITY_CHANGES.md`.

---

### Verified Claims

1. **Verbatim Phase 1 Completion Statement**:
   - Claim: The exact phrase `"Secure data foundation complete. Dispatch remains disabled. No player delivery has occurred."` is present.
   - Verification:
     - `SECURITY_CHANGES.md:6`: Exact match (`PASS`).
     - `TEST_RESULTS.md:12`: Exact match (`PASS`).
     - `docs/DISPATCH_ACTIVATION.md:20`: Exact match (`PASS`).
   - Result: **PASS**.

2. **Elimination of Non-Existent Test Files**:
   - Claim: `test_activation_gates.py` and `test_correlation.py` citations were removed from `SECURITY_CHANGES.md`.
   - Verification: Grep search across `SECURITY_CHANGES.md` confirmed 0 occurrences of both filenames.
   - Result: **PASS**.

3. **Documentation of All 11 Security Hardening Areas**:
   - Claim: Areas 1 through 11 are fully documented with architecture, fixes, and evidence.
   - Verification: Inspected Sections 1 through 11 in `SECURITY_CHANGES.md`. All 11 areas are fully populated.
   - Result: **PASS**.

4. **Residual Risks, Phase 1 Summary, and Phase 2 Roadmap**:
   - Claim: Sections 12, 13, and 14 are intact and aligned with Section 1A invariants.
   - Verification: Inspected `SECURITY_CHANGES.md` lines 121–181. Covers HMAC secret risk, Redis shared bus, LiteLLM swarms, offline EA validation, M0–M6 summary, and 7-part Phase 2 roadmap.
   - Result: **PASS**.

5. **Remediation of Test Tautologies in E2E Suite**:
   - Claim: Test tautologies in Tier 1, Tier 2, and Tier 4 were replaced with genuine invariant checks.
   - Verification:
     - `tests/e2e/test_tier1_features.py:425-438`: Verified `test_feature_28_cli_hmac_kill_switch_support` asserts HTTP 403 `{"detail": "proof_type_disabled"}` when `cli_hmac_enabled=False`.
     - `tests/e2e/test_tier1_features.py:527-570`: Verified `test_feature_37_atomic_approval_to_dispatch_creation` asserts row insertion in `public.dispatches` (`dispatch_status="pending"`, `player_id="1001"`) and shared transaction commit of `approval.decided` and `dispatch.created` domain events.
     - `tests/e2e/test_tier2_boundaries.py:48-61`: Verified `test_tier2_empty_run_request_body_rejected` asserts HTTP 200/201 on schema default, HTTP 422 on raw empty bytes, and HTTP 422 on empty string `club_id`.
     - `tests/e2e/test_tier2_boundaries.py:277-348`: Verified `TestObjectiveBoundsBoundaries` calls production validators `validate_metric_bounds`, `Objective.model_validate`, and `create_dispatch_for_approval` (asserting `ValueError`, `ValidationError`, and `NotDispatchable`).
     - `tests/e2e/test_tier4_scenarios.py:182-220`: Verified `test_section_1a_authority_separation_club_vs_player` validates schema columns (`scope`, `allow_player_consent`, `allow_player_fallback`), inserts distinct policies, asserts positive counts, and catches `psycopg.errors.CheckViolation` when attempting to allow player fallback on club scope.
   - Result: **PASS**.

6. **Realignment of `TEST_RESULTS.md`**:
   - Claim: Reflects 81 AST E2E tests and authentic class names.
   - Verification: AST counts confirm 81 tests (42 Tier 1, 25 Tier 2, 5 Tier 3, 9 Tier 4). Grep for monolithic class names (`TestTier1Features`, `TestTier2Boundaries`, `TestTier3Combinations`, `TestTier4Scenarios`) returned 0 occurrences in `TEST_RESULTS.md`.
   - Result: **PASS**.

7. **Companion Documents Alignment**:
   - Claim: `docs/DISPATCH_ACTIVATION.md` and `docs/EA_PLAYER_IDENTITY_VERIFICATION.md` conform to Phase 1 invariants.
   - Verification:
     - `docs/DISPATCH_ACTIVATION.md`: Dispatch disabled by invariant, 6 gates defined, G-WORKER blocked in Phase 1, G-OUTBOX refused with 409 before Phase 3.
     - `docs/EA_PLAYER_IDENTITY_VERIFICATION.md`: Status `UNVERIFIED (G-ID not met)`, tables 2.1–2.5 filled with `[TBD]`, invariants enforce numeric IDs and reject gamertag fallback.
   - Result: **PASS**.

8. **Zero Production Code Modifications**:
   - Claim: No modifications made to `src/` or `migrations/`.
   - Verification: Inspected file timestamps and git tracking; zero production source code files altered.
   - Result: **PASS**.

---

### Coverage Gaps

- **Attestation Node Executability**: Four test nodes cited in `SECURITY_CHANGES.md` cannot be executed by an automated test harness because their function names do not match the physical codebase.
  - Risk Level: **Medium** (documentation defect; the underlying code behavior is thoroughly proven by real, existing tests).
  - Recommendation: Update `SECURITY_CHANGES.md` with the verified replacements.

---

### Unverified Items

- **Live Container Execution in Target Host**: Docker daemon container execution was not performed, per Owner Success Conditions 8 and 9. Compose topology and network isolation were verified via static analysis in `tests/test_compose_isolation.py`.

---

## Part II: Adversarial Review

### Challenge Summary

**Overall Risk Assessment**: **MEDIUM** (Low operational risk as all security invariant implementations are genuine and pass; Medium attestation risk due to invalid test node names in release documentation).

---

### Challenges

#### [Critical] Challenge 1: The "Ghost Function" Trap in Formal Verification
- **Assumption Challenged**: That every cited test node in `SECURITY_CHANGES.md` can be run directly from the command line by an external auditor.
- **Attack Scenario**: An external compliance auditor executes the exact node ID from Area 1.3:
  ```bash
  pytest tests/test_graph_api_v1.py::test_health_ready_checks_db_posture
  ```
- **Blast Radius**: Pytest reports:
  ```text
  ERROR: not found: tests/test_graph_api_v1.py::test_health_ready_checks_db_posture
  ```
  The compliance audit fails, and the attestation artifact is deemed unreliable.
- **Mitigation**: Update line 16 to cite `tests/test_graph_api_v1.py::test_adversarial_readiness_fails_closed_on_db_role_posture_violation`.

#### [High] Challenge 2: Incomplete Mechanical Remediation in Agent Workflows
- **Assumption Challenged**: That fixing an explicit list of findings from Reviewer Iteration 1 automatically cleans up all related documentation defects.
- **Attack Scenario**: Worker agent addresses only the items explicitly called out in the reviewer's previous report (`test_activation_gates.py`, `test_correlation.py`, etc.) without running a global AST scan across all citations in the file.
- **Blast Radius**: Latent invalid citations remain in the document across subsequent review iterations, eroding confidence in automated agent workflows.
- **Mitigation**: Perform a 100% comprehensive AST scan of all citations in documentation before submitting handoffs.

---

### Stress Test Results

| # | Test Citation in `SECURITY_CHANGES.md` | Exists in Repo? | Verified Line in Source | Status |
|---|---|:---:|:---:|:---:|
| 1 | `tests/test_db_roles.py::test_role_attributes` | YES | `tests/test_db_roles.py:15` | **PASS** |
| 2 | `tests/test_db_roles.py::test_public_cannot_create_in_public_schema` | YES | `tests/test_db_roles.py:36` | **PASS** |
| 3 | `tests/test_db_migrate_roles.py::test_objects_owned_by_fc27_owner` | YES | `tests/test_db_migrate_roles.py:12` | **PASS** |
| 4 | `tests/e2e/test_tier1_features.py::TestDatabaseInfrastructureFeatures::test_feature_01_role_bootstrap_roles_exist_and_configured` | YES | `tests/e2e/test_tier1_features.py:56` | **PASS** |
| 5 | `tests/test_db_migrate_roles.py::test_migrate_refuses_without_membership` | YES | `tests/test_db_migrate_roles.py:29` | **PASS** |
| 6 | `tests/test_db_migrate_roles.py::test_checkpoint_setup_requires_owner` | YES | `tests/test_db_migrate_roles.py:38` | **PASS** |
| 7 | `tests/test_db_roles.py::test_bootstrap_transfers_legacy_objects_owned_by_admin` | YES | `tests/test_db_roles.py:44` | **PASS** |
| 8 | `tests/test_db_migrate_roles.py::test_app_cannot_run_ddl` | YES | `tests/test_db_migrate_roles.py:33` | **PASS** |
| 9 | `tests/test_db_roles.py::test_check_runtime_posture` | YES | `tests/test_db_roles.py:63` | **PASS** |
| 10 | `tests/test_graph_api_v1.py::test_health_ready_checks_db_posture` | **NO** | Not found in repo | **FAIL** |
| 11 | `tests/e2e/test_tier1_features.py::TestDatabaseInfrastructureFeatures::test_feature_01_public_schema_create_revoked` | YES | `tests/e2e/test_tier1_features.py:71` | **PASS** |
| 12 | `tests/test_db_migrate_roles.py::test_grants_policy_constant_matches_file` | YES | `tests/test_db_migrate_roles.py:132` | **PASS** |
| 13 | `tests/test_db_migrate_roles.py::test_grants_policy_recorded` | YES | `tests/test_db_migrate_roles.py:148` | **PASS** |
| 14 | `tests/e2e/test_tier1_features.py::TestDatabaseInfrastructureFeatures::test_feature_03_grants_policy_versioning` | YES | `tests/e2e/test_tier1_features.py:93` | **PASS** |
| 15 | `tests/test_dispatch_transition.py::test_function_attributes` | YES | `tests/test_dispatch_transition.py:25` | **PASS** |
| 16 | `tests/test_dispatch_transition.py::test_function_source_has_no_dynamic_sql` | YES | `tests/test_dispatch_transition.py:49` | **PASS** |
| 17 | `tests/test_dispatch_transition.py::test_runtime_roles_cannot_replace_function` | YES | `tests/test_dispatch_transition.py:61` | **PASS** |
| 18 | `tests/test_dispatch_transition.py::test_illegal_transition_edge_rejected` | YES | `tests/test_dispatch_transition.py:267` | **PASS** |
| 19 | `tests/test_dispatch_transition.py::test_cross_role_transition_rejected` | YES | `tests/test_dispatch_transition.py:281` | **PASS** |
| 20 | `tests/test_dispatch_transition.py::test_stale_version_conflict` | YES | `tests/test_dispatch_transition.py:103` | **PASS** |
| 21 | `tests/test_dispatch_transition.py::test_phase1_delivered_edge_blocked` | YES | `tests/test_dispatch_transition.py:296` | **PASS** |
| 22 | `tests/e2e/test_tier2_boundaries.py::TestStateAndLifecycleBoundaries::test_tier2_illegal_graph_threads_status_rejected_by_check` | YES | `tests/e2e/test_tier2_boundaries.py:220` | **PASS** |
| 23 | `tests/test_approval_security.py::test_nonce_expiration_is_enforced` | YES | `tests/test_approval_security.py:180` | **PASS** |
| 24 | `tests/test_approval_lifecycle.py::test_expired_request_blocks_prior_nonce` | YES | `tests/test_approval_lifecycle.py:151` | **PASS** |
| 25 | `tests/test_approval_lifecycle.py::test_expired_request_blocks_new_challenge` | YES | `tests/test_approval_lifecycle.py:182` | **PASS** |
| 26 | `tests/test_approval_lifecycle.py::test_expire_due_is_idempotent` | YES | `tests/test_approval_lifecycle.py:200` | **PASS** |
| 27 | `tests/test_approval_lifecycle.py::test_expiry_and_decision_race_has_single_outcome` | YES | `tests/test_approval_lifecycle.py:218` | **PASS** |
| 28 | `tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_decision_vs_expiry_race_condition` | YES | `tests/e2e/test_tier3_combinations.py:44` | **PASS** |
| 29 | `tests/test_approval_lifecycle.py::test_player_agent_cannot_be_decision_actor` | YES | `tests/test_approval_lifecycle.py:456` | **PASS** |
| 30 | `tests/test_approval_lifecycle.py::test_timeout_inactivity_cannot_approve` | YES | `tests/test_approval_lifecycle.py:370` | **PASS** |
| 31 | `tests/e2e/test_tier4_scenarios.py::TestSection1AHumanAuthorityAndConsent::test_section_1a_inactivity_and_timeout_never_creates_approval` | YES | `tests/e2e/test_tier4_scenarios.py:238` | **PASS** |
| 32 | `tests/test_approval_lifecycle.py::test_unsupported_actor_types_fail_closed_in_phase1` | YES | `tests/test_approval_lifecycle.py:394` | **PASS** |
| 33 | `tests/e2e/test_tier4_scenarios.py::TestSection1AHumanAuthorityAndConsent::test_section_1a_authority_separation_club_vs_player` | YES | `tests/e2e/test_tier4_scenarios.py:182` | **PASS** |
| 34 | `tests/e2e/test_tier4_scenarios.py::TestSection1AHumanAuthorityAndConsent::test_section_1a_player_fallback_prohibits_multi_player_packages` | YES | `tests/e2e/test_tier4_scenarios.py:230` | **PASS** |
| 35 | `tests/e2e/test_tier1_features.py::TestApprovalLifecycleFeatures::test_feature_16_human_authority_schema_compatibility` | YES | `tests/e2e/test_tier1_features.py:279` | **PASS** |
| 36 | `tests/test_graph_api_v1.py::test_hmac_kill_switch_behavior` | YES | `tests/test_graph_api_v1.py:323` | **PASS** |
| 37 | `tests/test_graph_api_v1.py::test_adversarial_hmac_kill_switch_blocks_before_body_parsing` | YES | `tests/test_graph_api_v1.py:546` | **PASS** |
| 38 | `tests/e2e/test_tier1_features.py::TestGraphCoreAndApiFeatures::test_feature_28_cli_hmac_kill_switch_support` | YES | `tests/e2e/test_tier1_features.py:425` | **PASS** |
| 39 | `tests/test_approval_security.py::test_modified_signed_payload_is_rejected` | YES | `tests/test_approval_security.py:189` | **PASS** |
| 40 | `tests/test_events_outbox.py::test_domain_events_append_only` | YES | `tests/test_events_outbox.py:49` | **PASS** |
| 41 | `tests/test_events_outbox.py::test_approval_audit_append_only` | YES | `tests/test_events_outbox.py:69` | **PASS** |
| 42 | `tests/test_dispatch_schema.py::test_append_only_triggers` | **NO** | Not found in repo | **FAIL** |
| 43 | `tests/e2e/test_tier1_features.py::TestDatabaseInfrastructureFeatures::test_feature_07_append_only_table_guards_reject_mutations` | YES | `tests/e2e/test_tier1_features.py:160` | **PASS** |
| 44 | `tests/test_migration_0003.py::test_package_version_immutable_while_awaiting` | YES | `tests/test_migration_0003.py:159` | **PASS** |
| 45 | `tests/test_dispatch_schema.py::test_dispatches_terminal_rows_frozen` | YES | `tests/test_dispatch_schema.py:214` | **PASS** |
| 46 | `tests/e2e/test_tier2_boundaries.py::TestStateAndLifecycleBoundaries::test_tier2_direct_update_of_package_version_blocked_by_trigger` | YES | `tests/e2e/test_tier2_boundaries.py:201` | **PASS** |
| 47 | `tests/test_approval_security.py::test_package_version_immutable_once_awaiting_approval` | YES | `tests/test_approval_security.py:204` | **PASS** |
| 48 | `tests/e2e/test_tier1_features.py::TestApprovalLifecycleFeatures::test_feature_08_approval_requests_table_schema` | YES | `tests/e2e/test_tier1_features.py:185` | **PASS** |
| 49 | `tests/test_graph_persistence.py::test_decision_package_version_mismatch_fails_closed` | YES | `tests/test_graph_persistence.py:145` | **PASS** |
| 50 | `tests/e2e/test_tier2_boundaries.py::TestStateAndLifecycleBoundaries::test_tier2_stale_package_version_decision_fails` | YES | `tests/e2e/test_tier2_boundaries.py:178` | **PASS** |
| 51 | `tests/e2e/test_tier4_scenarios.py::TestRealWorldLifecycleScenarios::test_tier4_scenario_revision_request_preserves_package_immutability` | YES | `tests/e2e/test_tier4_scenarios.py:148` | **PASS** |
| 52 | `tests/test_graph_persistence.py::test_single_approval_interrupt_at_approval_node` | YES | `tests/test_graph_persistence.py:56` | **PASS** |
| 53 | `tests/test_langgraph_flow.py::test_graph_runs_to_interrupt_with_typed_state` | YES | `tests/test_langgraph_flow.py:66` | **PASS** |
| 54 | `tests/e2e/test_tier1_features.py::TestGraphCoreAndApiFeatures::test_feature_18_single_approval_interrupt_verified` | YES | `tests/e2e/test_tier1_features.py:304` | **PASS** |
| 55 | `tests/test_graph_persistence.py::test_terminal_graph_states_formatting` | YES | `tests/test_graph_persistence.py:175` | **PASS** |
| 56 | `tests/test_langgraph_flow.py::test_existing_langgraph_thread_resumes` | YES | `tests/test_langgraph_flow.py:79` | **PASS** |
| 57 | `tests/test_graph_api_v1.py::test_v1_resume_refuses_authorization_payload` | YES | `tests/test_graph_api_v1.py:200` | **PASS** |
| 58 | `tests/e2e/test_tier1_features.py::TestGraphCoreAndApiFeatures::test_feature_20_terminal_states_enforcement_format` | YES | `tests/e2e/test_tier1_features.py:340` | **PASS** |
| 59 | `tests/test_graph_persistence.py::test_restart_recovers_interrupted_graph` | **NO** | Not found in repo | **FAIL** |
| 60 | `tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_post_commit_resume_failure_recovery` | **NO** | Not found in repo | **FAIL** |
| 61 | `tests/test_dispatch_creation.py::test_gates_block_by_default` | YES | `tests/test_dispatch_creation.py:41` | **PASS** |
| 62 | `tests/test_dispatch_creation.py::test_g_outbox_not_recordable_in_phase1` | YES | `tests/test_dispatch_creation.py:248` | **PASS** |
| 63 | `tests/e2e/test_tier1_features.py::TestObjectivesAndGatesFeatures::test_feature_35_activation_gate_evaluation_fails_closed` | YES | `tests/e2e/test_tier1_features.py:510` | **PASS** |
| 64 | `tests/test_dispatch_creation.py::test_gate_recording_requires_commissioner_and_signature` | YES | `tests/test_dispatch_creation.py:81` | **PASS** |
| 65 | `tests/test_dispatch_creation.py::test_gate_tampered_field_rejected` | YES | `tests/test_dispatch_creation.py:167` | **PASS** |
| 66 | `tests/test_dispatch_creation.py::test_gate_requires_deployment_identity` | YES | `tests/test_dispatch_creation.py:198` | **PASS** |
| 67 | `tests/e2e/test_tier1_features.py::TestObjectivesAndGatesFeatures::test_feature_36_gate_challenge_and_record_routes_exist` | YES | `tests/e2e/test_tier1_features.py:520` | **PASS** |
| 68 | `tests/test_dispatch_creation.py::test_gate_change_applies_on_next_evaluation` | YES | `tests/test_dispatch_creation.py:258` | **PASS** |
| 69 | `tests/test_events_outbox.py::test_sourced_event_requires_existing_source` | YES | `tests/test_events_outbox.py:82` | **PASS** |
| 70 | `tests/e2e/test_tier1_features.py::TestDatabaseInfrastructureFeatures::test_feature_05_event_source_guard_missing_source_rejected` | YES | `tests/e2e/test_tier1_features.py:134` | **PASS** |
| 71 | `tests/test_dispatch_transition.py::test_worker_actor_derivation` | YES | `tests/test_dispatch_transition.py:131` | **PASS** |
| 72 | `tests/test_events_outbox.py::test_app_cannot_forge_dispatch_events` | YES | `tests/test_events_outbox.py:140` | **PASS** |
| 73 | `tests/e2e/test_tier1_features.py::TestDatabaseInfrastructureFeatures::test_feature_06_transition_event_source_identity_restricted` | YES | `tests/e2e/test_tier1_features.py:147` | **PASS** |
| 74 | `tests/test_graph_api_v1.py::test_correlation_id_generated_when_absent` | YES | `tests/test_graph_api_v1.py:62` | **PASS** |
| 75 | `tests/test_graph_api_v1.py::test_correlation_id_echoed_when_present` | YES | `tests/test_graph_api_v1.py:73` | **PASS** |
| 76 | `tests/test_graph_api_v1.py::test_correlation_id_propagated_to_logs` | YES | `tests/test_graph_api_v1.py:81` | **PASS** |
| 77 | `tests/test_graph_api_v1.py::test_malformed_correlation_id_rejected` | YES | `tests/test_graph_api_v1.py:54` | **PASS** |
| 78 | `tests/test_graph_api_v1.py::test_adversarial_malformed_correlation_id_rejected_and_redacted` | YES | `tests/test_graph_api_v1.py:397` | **PASS** |
| 79 | `tests/test_dispatch_creation.py::test_correlation_id_shared_by_dispatch_and_events` | YES | `tests/test_dispatch_creation.py:286` | **PASS** |
| 80 | `tests/e2e/test_tier1_features.py::TestGraphCoreAndApiFeatures::test_feature_21_correlation_middleware_propagation` | YES | `tests/e2e/test_tier1_features.py:350` | **PASS** |
| 81 | `tests/test_graph_api_v1.py::test_adversarial_pydantic_validation_strips_sensitive_inputs` | YES | `tests/test_graph_api_v1.py:474` | **PASS** |
| 82 | `tests/test_compose_isolation.py::test_signing_secret_only_in_graph_api` | YES | `tests/test_compose_isolation.py:99` | **PASS** |
| 83 | `tests/test_compose_isolation.py::test_db_secret_mounts_per_service` | YES | `tests/test_compose_isolation.py:122` | **PASS** |
| 84 | `tests/test_compose_isolation.py::test_only_graph_api_can_reach_postgres` | YES | `tests/test_compose_isolation.py:200` | **PASS** |
| 85 | `tests/test_db_roles.py::test_redact_conninfo` | YES | `tests/test_db_roles.py:81` | **PASS** |
| 86 | `tests/test_graph_api_v1.py::test_v1_get_run_safe_projection_omits_secrets` | YES | `tests/test_graph_api_v1.py:162` | **PASS** |
| 87 | `tests/test_graph_api_v1.py::test_adversarial_run_projection_scrubs_internal_secrets` | YES | `tests/test_graph_api_v1.py:499` | **PASS** |

**Summary**: 83 Passed, 4 Failed.

---

### Unchallenged Areas

- **Live EA Sports Match Logs**: Out of scope for Phase 1; simulated via deterministic test fixtures in `tests/test_dispatch_creation.py` and `tests/e2e/test_tier2_boundaries.py`.
- **Hardware Passkey WebAuthn Ceremonies**: Out of scope for Phase 1; assigned to Phase 2 per Section 14 roadmap.

---

## Part III: Required Remediation

To achieve approval, update `SECURITY_CHANGES.md` with these four precise replacements:

1. **Area 1.3 (line 16)**:
   - *Current*: `tests/test_graph_api_v1.py::test_health_ready_checks_db_posture`
   - *Replace with*: `tests/test_graph_api_v1.py::test_adversarial_readiness_fails_closed_on_db_role_posture_violation`

2. **Area 6.1 (line 66)**:
   - *Current*: `tests/test_dispatch_schema.py::test_append_only_triggers`
   - *Replace with*: `tests/test_dispatch_schema.py::test_tracking_tables_append_only`

3. **Area 8.3 (line 86)**:
   - *Current*: `tests/test_graph_persistence.py::test_restart_recovers_interrupted_graph`
   - *Replace with*: `tests/test_graph_persistence.py::test_checkpoint_recovery_across_server_restart`

4. **Area 8.3 (line 86)**:
   - *Current*: `tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_post_commit_resume_failure_recovery`
   - *Replace with*: `tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_post_commit_resume_failure_preserves_authoritative_decision`
