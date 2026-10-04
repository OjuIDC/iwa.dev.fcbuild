# Milestone 6 Iteration 3 Review & Adversarial Challenge Report

**Reviewer**: `reviewer_m6_it3_2`  
**Archetype**: `teamwork_preview_reviewer`  
**Roles**: reviewer, critic  
**Target Milestone**: Milestone 6 Iteration 3 (Security Documentation Realignment & Phase 2 Handoff)  
**Target Artifacts**:
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\SECURITY_CHANGES.md`
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m6_3\handoff.md`
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m6_it2_2\review.md`
- `docs/DISPATCH_ACTIVATION.md`
- `docs/EA_PLAYER_IDENTITY_VERIFICATION.md`  
**Date**: 2026-10-03  

---

## Part I: Quality Review

### Review Summary

**Verdict**: **APPROVE**

**Primary Rationale**:
Milestone 6 Iteration 3 worker remediation has successfully and completely resolved the 4 invalid test function citations identified during Iteration 2.
1. **Area 1.3 (Row 1.3, Line 16)**: Properly cites `tests/test_graph_api_v1.py::test_adversarial_readiness_fails_closed_on_db_role_posture_violation` (verifiably defined at line 719).
2. **Area 6.1 (Row 6.1, Line 66)**: Properly cites `tests/test_dispatch_schema.py::test_tracking_tables_append_only` (verifiably defined at line 261).
3. **Area 8.3 (Row 8.3, Line 86)**: Properly cites `tests/test_graph_persistence.py::test_checkpoint_recovery_across_server_restart` (verifiably defined at line 209) AND `tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_post_commit_resume_failure_preserves_authoritative_decision` (verifiably defined at line 79).
4. **Resolution of Iteration 2 Defects**: All 4 defective citation strings have been 100% eradicated from `SECURITY_CHANGES.md` (0 occurrences remain).
5. **Phase 1 Completion Statement**: Intact and verbatim at lines 5–6: `"Secure data foundation complete. Dispatch remains disabled. No player delivery has occurred."`
6. **Completeness**: All 11 security hardening areas, Residual Risks (Section 12), Phase 1 Implementation Summary (Section 13), and the Phase 2 Handoff Roadmap (Section 14) are fully populated and aligned with Phase 1 boundary invariants.
7. **Integrity Audit**: An exhaustive audit of all 87 test citations in `SECURITY_CHANGES.md` confirms that 100% (87/87) exist in the codebase and test genuine, non-facade logic. Zero production source code files (`src/`, `migrations/`) or test files were altered.

---

### Findings

#### Prior Critical Finding Remediated: 100% Resolved
- **Finding**: In Iteration 2, 4 test function citations referenced non-existent test functions (`test_health_ready_checks_db_posture`, `test_append_only_triggers`, `test_restart_recovers_interrupted_graph`, `test_tier3_post_commit_resume_failure_recovery`).
- **Remediation Status**: **CLEAN / RESOLVED**. Each invalid citation has been replaced with its authentic proving test in the test suite. No new defects were introduced.

---

### Verified Claims

1. **Row 1.3 (Line 16) Citation Accuracy**:
   - *Claim*: Line 16 cites `tests/test_graph_api_v1.py::test_adversarial_readiness_fails_closed_on_db_role_posture_violation`.
   - *Verification*: Inspected `SECURITY_CHANGES.md` line 16. The exact citation is present. Inspected `tests/test_graph_api_v1.py` line 719: verified the test function exists, exercises both boot-time crash on DDL privilege and runtime `/health/ready` 503 response on posture violation.
   - *Result*: **PASS**.

2. **Row 6.1 (Line 66) Citation Accuracy**:
   - *Claim*: Line 66 cites `tests/test_dispatch_schema.py::test_tracking_tables_append_only`.
   - *Verification*: Inspected `SECURITY_CHANGES.md` line 66. The exact citation is present. Inspected `tests/test_dispatch_schema.py` line 261: verified the test function exists, parametrizes all 8 dispatch tracking tables, and asserts `psycopg.errors.CheckViolation` matching `"append-only"` on both `UPDATE` and `DELETE`.
   - *Result*: **PASS**.

3. **Row 8.3 (Line 86) Citations Accuracy**:
   - *Claim*: Line 86 cites `tests/test_graph_persistence.py::test_checkpoint_recovery_across_server_restart` and `tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_post_commit_resume_failure_preserves_authoritative_decision`.
   - *Verification*: Inspected `SECURITY_CHANGES.md` line 86. Both exact citations are present. Inspected `tests/test_graph_persistence.py` line 209: verified `test_checkpoint_recovery_across_server_restart` tests server restart recovery against PostgreSQL checkpointer and resumption to `"APPROVED, NOT DISPATCHED"`. Inspected `tests/e2e/test_tier3_combinations.py` line 79: verified method `test_tier3_post_commit_resume_failure_preserves_authoritative_decision` inside class `TestCrossFeatureCombinations` tests post-commit resume failures and ensures exactly 1 audit row with no duplicate side-effects.
   - *Result*: **PASS**.

4. **Zero Lingering Defective Citations**:
   - *Claim*: `test_health_ready_checks_db_posture`, `test_append_only_triggers`, `test_restart_recovers_interrupted_graph`, and `test_tier3_post_commit_resume_failure_recovery` no longer appear.
   - *Verification*: Grep search across `SECURITY_CHANGES.md` yielded 0 matches for all 4 strings.
   - *Result*: **PASS**.

5. **Verbatim Phase 1 Completion Statement**:
   - *Claim*: Lines 5–6 contain `"Secure data foundation complete. Dispatch remains disabled. No player delivery has occurred."`
   - *Verification*: Inspected `SECURITY_CHANGES.md` lines 5–6:
     ```markdown
     ### Required Phase 1 Completion Statement
     > **"Secure data foundation complete. Dispatch remains disabled. No player delivery has occurred."**
     ```
     Exact character match with `ORIGINAL_REQUEST.md` Section 1.
   - *Result*: **PASS**.

6. **All 11 Security Hardening Areas Present**:
   - *Claim*: Areas 1 through 11 are thoroughly documented with architecture, fix descriptions, and proving evidence.
   - *Verification*: Verified presence and contents of:
     - Section 1: Database Role Separation & Least Privilege (C-6)
     - Section 2: SECURITY DEFINER Hardening (`public.fc27_dispatch_transition`)
     - Section 3: Approval Expiry Semantics
     - Section 4: Section 1A Human Authority & Player Consent Models
     - Section 5: HMAC Kill Switch & Key Rotation
     - Section 6: Append-Only Audit & Immutability
     - Section 7: Recommendation Package Version Immutability
     - Section 8: LangGraph Interrupt & PostgreSQL Re-Read (AM-4)
     - Section 9: Nonce-Bound & Deployment-Bound Activation Gates (C-4, C-1)
     - Section 10: Event Source Identity & Correlation ID Isolation (C-10, C-5)
     - Section 11: File-Based Secrets & Credential Sanitization (C-7)
   - *Result*: **PASS**.

7. **Residual Risks, Phase 1 Summary, and Phase 2 Roadmap**:
   - *Claim*: Sections 12, 13, and 14 are intact, aligned with Section 1A invariants, and provide clear Phase 2 work packages.
   - *Verification*: Inspected Section 12 (Residual Risks: Symmetric HMAC Signing Secret, Redis Shared Bus, LiteLLM Swarms, Offline EA Validation), Section 13 (Phase 1 Implementation Summary: M0–M6), and Section 14 (Phase 2 Handoff Roadmap: User Identity, Auth/WebAuthn Sessions, Step-Up Decisions, Delegated Authority, Player Consent, Escalation/Fallback, Conflict Resolution).
   - *Result*: **PASS**.

8. **Companion Documents Conformance**:
   - *Claim*: `docs/DISPATCH_ACTIVATION.md` and `docs/EA_PLAYER_IDENTITY_VERIFICATION.md` strictly uphold Phase 1 invariants.
   - *Verification*:
     - `docs/DISPATCH_ACTIVATION.md`: Dispatch disabled by invariant (`DISPATCH_ENABLED=false`), 6 rollout gates defined, G-WORKER cannot pass in Phase 1, G-OUTBOX refused before Phase 3, contains verbatim completion statement.
     - `docs/EA_PLAYER_IDENTITY_VERIFICATION.md`: Status `UNVERIFIED (G-ID not met)`, evidence sections 2.1–2.5 explicitly marked with `[TBD]` and "Pending live EA capture", strict numeric player ID rules enforced, gamertag fallback forbidden.
   - *Result*: **PASS**.

9. **Integrity & Write Ownership Compliance**:
   - *Claim*: No integrity violations, facade implementations, or unauthorized edits to production/test code.
   - *Verification*: Inspected git tracking; worker modified only `SECURITY_CHANGES.md` and agent directory `.agents/teamwork/worker_m6_3/`. No changes to `src/`, `migrations/`, or `tests/`. Proving tests are authentic, assert real database/API state, and contain no hardcoded stubs.
   - *Result*: **PASS**.

---

### Coverage Gaps

- **None**: All 87 test citations have been verified against existing source files. All security hardening areas are fully documented with corresponding proving tests.

---

### Unverified Items

- **Live Container Execution in Target Host**: Docker daemon container execution was not performed, per Owner Success Conditions 8 and 9. Compose topology and network isolation were verified via static analysis in `tests/test_compose_isolation.py`.

---

## Part II: Adversarial Review

### Challenge Summary

**Overall Risk Assessment**: **LOW**  
All 87 test citations correspond to actual, existing, executable test functions and methods in the codebase. All 11 security hardening areas accurately reflect real database constraints, trigger definitions, and API middleware. Phase 1 boundary invariants are completely preserved.

---

### Stress Test Results: Comprehensive 87-Citation Repository Audit

Every test cited in `SECURITY_CHANGES.md` was checked for existence and valid target definition in the repository:

| # | Cited Test in `SECURITY_CHANGES.md` | Exists in Repo? | Verified File and Line | Status |
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
| 10 | `tests/test_graph_api_v1.py::test_adversarial_readiness_fails_closed_on_db_role_posture_violation` | YES | `tests/test_graph_api_v1.py:719` | **PASS (Fixed)** |
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
| 42 | `tests/test_dispatch_schema.py::test_tracking_tables_append_only` | YES | `tests/test_dispatch_schema.py:261` | **PASS (Fixed)** |
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
| 59 | `tests/test_graph_persistence.py::test_checkpoint_recovery_across_server_restart` | YES | `tests/test_graph_persistence.py:209` | **PASS (Fixed)** |
| 60 | `tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_post_commit_resume_failure_preserves_authoritative_decision` | YES | `tests/e2e/test_tier3_combinations.py:79` | **PASS (Fixed)** |
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

**Summary**: 87 Verified, 0 Failed (100% Pass Rate).

---

### Adversarial Challenge Analysis: The Remediated Nodes

1. **Row 1.3: `test_adversarial_readiness_fails_closed_on_db_role_posture_violation`**
   - *Attack Scenario*: An attacker attempts to run the `graph-api` with a PostgreSQL connection string configured for `fc27_migrator` (which has DDL rights) or an operator fails to revoke `CREATE` on schema `public`.
   - *Test Behavior*: The test verifies that:
     1. At startup, `create_app` crashes immediately with `src.settings.ConfigError: database role posture violation` when `enforce_db_posture=True`.
     2. At runtime, if credentials change or connection posture degrades, `GET /health/ready` returns HTTP 503 (`status: "degraded"`, `"reason": "... db_role_posture violation ..."`).
   - *Assessment*: This test is an authentic, adversarial security test directly proving the security claim in Row 1.3.

2. **Row 6.1: `test_tracking_tables_append_only`**
   - *Attack Scenario*: An operator or rogue SQL injection payload attempts to modify or delete audit and tracking history in dispatch-related tracking tables (`dispatch_plans`, `dispatch_plan_decisions`, `dispatch_delivery_attempts`, `dispatch_acknowledgments`, `dispatch_comments`, `analyst_notes`, `player_observations`, `dispatch_assessments`).
   - *Test Behavior*: The test inserts valid records into each of the 8 tables and then attempts an `UPDATE` and a `DELETE`. The database trigger `fc27_reject_mutation()` intercepts both operations and raises `psycopg.errors.CheckViolation` with `"<table_name> is append-only"`.
   - *Assessment*: Directly proves the immutability claim in Row 6.1 across all 8 tracking relations.

3. **Row 8.3: `test_checkpoint_recovery_across_server_restart`**
   - *Attack Scenario*: The Graph API server crashes or is restarted while an execution is paused waiting for Commissioner approval.
   - *Test Behavior*: Shuts down runner instance 1, initializes a fresh runner instance 2 connected to the identical PostgreSQL database, confirms thread state (`status="awaiting_approval"`, `graph_interrupted=True`, `package_version` preserved), issues an approval, and verifies resumption to `"APPROVED, NOT DISPATCHED"` with complete checkpoint history intact.
   - *Assessment*: Directly proves server-restart checkpoint recovery in Row 8.3.

4. **Row 8.3: `test_tier3_post_commit_resume_failure_preserves_authoritative_decision`**
   - *Attack Scenario*: The PostgreSQL transaction committing a Commissioner's decision succeeds, but the subsequent Graph resume call fails or is interrupted by network/process crash.
   - *Test Behavior*: Verifies that the committed decision remains intact in `graph_threads` (`status="approved"`) and `approval_audit` (`decision="APPROVE"`). Replaying the request leaves the single authoritative decision row untouched (count == 1, no duplicate audit records).
   - *Assessment*: Directly proves authoritative decision preservation in Row 8.3.

---

### Unchallenged Areas

- **Live EA Sports Match Logs**: Out of scope for Phase 1; simulated via deterministic test fixtures in `tests/test_dispatch_creation.py` and `tests/e2e/test_tier2_boundaries.py`.
- **Hardware Passkey WebAuthn Ceremonies**: Out of scope for Phase 1; assigned to Phase 2 per Section 14 roadmap.
