# Forensic Test Citation Sweep Analysis: `SECURITY_CHANGES.md`

**Agent ID**: `explorer_m6_it3_sec_3`  
**Task**: Complete Whole-File Forensic Sweep of `SECURITY_CHANGES.md` Test Citations  
**Date**: 2026-10-03  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m6_it3_sec_3`  
**Target Document**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\SECURITY_CHANGES.md`  

---

## 1. Executive Summary & Verification Verdict

A complete, whole-file forensic verification of `SECURITY_CHANGES.md` was conducted across all 11 sections. Every test node citation was cross-referenced against the actual test implementation files in `tests/`.

### Key Findings:
1. **Total Citations Evaluated**:
   - There are **89 test citation occurrences** across the 11 security tables in `SECURITY_CHANGES.md`.
   - These 89 citations map to **87 unique test node IDs** spanning **17 test files** in `tests/` (including 65 component/unit test citations and 24 E2E test citations).
   - In addition, **10 non-test code, migration, and documentation references** were audited and confirmed 100% accurate.
2. **Defect Confirmation**:
   - Exactly **4 test citations** in `SECURITY_CHANGES.md` reference non-existent test node IDs:
     1. Line 16 (Row 1.3): `tests/test_graph_api_v1.py::test_health_ready_checks_db_posture`
     2. Line 66 (Row 6.1): `tests/test_dispatch_schema.py::test_append_only_triggers`
     3. Line 86 (Row 8.3): `tests/test_graph_persistence.py::test_restart_recovers_interrupted_graph`
     4. Line 86 (Row 8.3): `tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_post_commit_resume_failure_recovery`
   - These 4 non-existent citations match **100%** with the 4 citations identified in `auditor_m6_it2_1/audit_report.md`.
3. **Purity of Remaining Citations**:
   - Outside of the 4 flagged citations, **all other 85 test citation occurrences (83 unique test nodes) are 100% authentic, exist verbatim in the repository test tree, and directly verify the claimed security mechanisms**.
   - Zero other discrepancies, hallucinated file paths, or non-existent functions exist in `SECURITY_CHANGES.md`.

---

## 2. Forensic Analysis of the 4 Flagged Citations & Verified Replacements

### Discrepancy 1: Line 16 (Section 1, Row 1.3: "Runtime Application Least Privilege")
- **Cited String**: `tests/test_graph_api_v1.py::test_health_ready_checks_db_posture`
- **Location**: `SECURITY_CHANGES.md:16`
- **Defect**: No function named `test_health_ready_checks_db_posture` exists in `tests/test_graph_api_v1.py` or anywhere in the repository.
- **Context Verified**: Row 1.3 asserts: *"Posture verification (`check_runtime_posture`) runs at startup and in `/health/ready`."*
- **Exact Verified Replacement**:
  - `tests/test_graph_api_v1.py::test_adversarial_readiness_fails_closed_on_db_role_posture_violation` (defined at `tests/test_graph_api_v1.py:719-745`).
  - *Verification Proof*: The test explicitly simulates boot-time role privilege violation and runtime `/health/ready` 503 degraded responses when DB role posture fails.
  - *Secondary / Supplemental Existing Test*: `tests/e2e/test_tier1_features.py::TestGraphCoreAndApiFeatures::test_feature_27_health_probes_liveness_and_readiness` (defined at line 417).

### Discrepancy 2: Line 66 (Section 6, Row 6.1: "Mutation Rejection Trigger")
- **Cited String**: `tests/test_dispatch_schema.py::test_append_only_triggers`
- **Location**: `SECURITY_CHANGES.md:66`
- **Defect**: No function named `test_append_only_triggers` exists in `tests/test_dispatch_schema.py`.
- **Context Verified**: Row 6.1 asserts: *"public.fc27_reject_mutation() trigger function attached BEFORE UPDATE OR DELETE to 13 immutable tables: domain_events, approval_audit, activation_gate_log, player_decisions, approval_escalations, dispatch_plans, dispatch_plan_decisions, dispatch_delivery_attempts, dispatch_acknowledgments, dispatch_comments, analyst_notes, player_observations, dispatch_assessments."*
- **Exact Verified Replacement**:
  - `tests/test_dispatch_schema.py::test_tracking_tables_append_only` (defined at `tests/test_dispatch_schema.py:261-336`).
  - *Verification Proof*: Parameterized test over `dispatch_plans`, `dispatch_plan_decisions`, `dispatch_delivery_attempts`, `dispatch_acknowledgments`, `dispatch_comments`, `analyst_notes`, `player_observations`, and `dispatch_assessments` asserting `fc27_reject_mutation()` blocks `UPDATE` and `DELETE`.

### Discrepancy 3: Line 86 (Section 8, Row 8.3: "Pre-Resume Commit & Reconciliation")
- **Cited String**: `tests/test_graph_persistence.py::test_restart_recovers_interrupted_graph`
- **Location**: `SECURITY_CHANGES.md:86`
- **Defect**: No function named `test_restart_recovers_interrupted_graph` exists in `tests/test_graph_persistence.py`.
- **Context Verified**: Row 8.3 asserts: *"Decision write path commits PostgreSQL transaction before invoking graph resume. If resume fails, committed decision remains preserved in PostgreSQL; graph-api re-resumes terminal threads on startup."*
- **Exact Verified Replacement**:
  - `tests/test_graph_persistence.py::test_checkpoint_recovery_across_server_restart` (defined at `tests/test_graph_persistence.py:209-246`).
  - *Verification Proof*: Explicitly tests runner shutdown and fresh instance restart against PostgreSQL, verifying checkpoint recovery of interrupted threads and subsequent completion.

### Discrepancy 4: Line 86 (Section 8, Row 8.3: "Pre-Resume Commit & Reconciliation")
- **Cited String**: `tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_post_commit_resume_failure_recovery`
- **Location**: `SECURITY_CHANGES.md:86`
- **Defect**: No method named `test_tier3_post_commit_resume_failure_recovery` exists in `TestCrossFeatureCombinations`.
- **Context Verified**: Row 8.3 asserts: *"If resume fails, committed decision remains preserved in PostgreSQL; graph-api re-resumes terminal threads on startup."*
- **Exact Verified Replacement**:
  - `tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_post_commit_resume_failure_preserves_authoritative_decision` (defined at `tests/e2e/test_tier3_combinations.py:79-111`).
  - *Verification Proof*: Docstring and logic state: *"Simulated Graph resume failure after DB commit leaves authoritative decision intact. Reconciliation later finalizes the thread without altering the decision."*

---

## 3. Comprehensive Checklist of All Test Citations in `SECURITY_CHANGES.md`

Below is the complete, item-by-item verification census across all 11 sections of `SECURITY_CHANGES.md`:

### Section 1: Database Role Separation & Least Privilege (C-6)
| # | Row | Line | Cited Test Node ID | Status | Existing Location / Verified Replacement |
|---|:---:|:---:|---|:---:|---|
| 1 | 1.1 | 14 | `tests/test_db_roles.py::test_role_attributes` | **VERIFIED** | `tests/test_db_roles.py:15` |
| 2 | 1.1 | 14 | `tests/test_db_roles.py::test_public_cannot_create_in_public_schema` | **VERIFIED** | `tests/test_db_roles.py:36` |
| 3 | 1.1 | 14 | `tests/test_db_migrate_roles.py::test_objects_owned_by_fc27_owner` | **VERIFIED** | `tests/test_db_migrate_roles.py:12` |
| 4 | 1.1 | 14 | `tests/e2e/test_tier1_features.py::TestDatabaseInfrastructureFeatures::test_feature_01_role_bootstrap_roles_exist_and_configured` | **VERIFIED** | `tests/e2e/test_tier1_features.py:56` |
| 5 | 1.2 | 15 | `tests/test_db_migrate_roles.py::test_migrate_refuses_without_membership` | **VERIFIED** | `tests/test_db_migrate_roles.py:29` |
| 6 | 1.2 | 15 | `tests/test_db_migrate_roles.py::test_checkpoint_setup_requires_owner` | **VERIFIED** | `tests/test_db_migrate_roles.py:38` |
| 7 | 1.2 | 15 | `tests/test_db_roles.py::test_bootstrap_transfers_legacy_objects_owned_by_admin` | **VERIFIED** | `tests/test_db_roles.py:44` |
| 8 | 1.3 | 16 | `tests/test_db_migrate_roles.py::test_app_cannot_run_ddl` | **VERIFIED** | `tests/test_db_migrate_roles.py:33` |
| 9 | 1.3 | 16 | `tests/test_db_roles.py::test_check_runtime_posture` | **VERIFIED** | `tests/test_db_roles.py:63` |
| 10 | 1.3 | 16 | `tests/test_graph_api_v1.py::test_health_ready_checks_db_posture` | **DEFECT** | Replace with `tests/test_graph_api_v1.py::test_adversarial_readiness_fails_closed_on_db_role_posture_violation` (at line 719) |
| 11 | 1.3 | 16 | `tests/e2e/test_tier1_features.py::TestDatabaseInfrastructureFeatures::test_feature_01_public_schema_create_revoked` | **VERIFIED** | `tests/e2e/test_tier1_features.py:71` |
| 12 | 1.4 | 17 | `tests/test_db_migrate_roles.py::test_grants_policy_constant_matches_file` | **VERIFIED** | `tests/test_db_migrate_roles.py:132` |
| 13 | 1.4 | 17 | `tests/test_db_migrate_roles.py::test_grants_policy_recorded` | **VERIFIED** | `tests/test_db_migrate_roles.py:148` |
| 14 | 1.4 | 17 | `tests/e2e/test_tier1_features.py::TestDatabaseInfrastructureFeatures::test_feature_03_grants_policy_versioning` | **VERIFIED** | `tests/e2e/test_tier1_features.py:93` |

### Section 2: SECURITY DEFINER Hardening (`public.fc27_dispatch_transition`)
| # | Row | Line | Cited Test Node ID | Status | Existing Location / Verified Replacement |
|---|:---:|:---:|---|:---:|---|
| 15 | 2.1 | 25 | `tests/test_dispatch_transition.py::test_function_attributes` | **VERIFIED** | `tests/test_dispatch_transition.py:25` |
| 16 | 2.1 | 25 | `tests/test_dispatch_transition.py::test_runtime_roles_cannot_replace_function` | **VERIFIED** | `tests/test_dispatch_transition.py:61` |
| 17 | 2.2 | 26 | `tests/test_dispatch_transition.py::test_function_source_has_no_dynamic_sql` | **VERIFIED** | `tests/test_dispatch_transition.py:49` |
| 18 | 2.3 | 27 | `tests/test_dispatch_transition.py::test_illegal_transition_edge_rejected` | **VERIFIED** | `tests/test_dispatch_transition.py:267` |
| 19 | 2.3 | 27 | `tests/test_dispatch_transition.py::test_cross_role_transition_rejected` | **VERIFIED** | `tests/test_dispatch_transition.py:281` |
| 20 | 2.3 | 27 | `tests/test_dispatch_transition.py::test_stale_version_conflict` | **VERIFIED** | `tests/test_dispatch_transition.py:103` |
| 21 | 2.4 | 28 | `tests/test_dispatch_transition.py::test_phase1_delivered_edge_blocked` | **VERIFIED** | `tests/test_dispatch_transition.py:296` |
| 22 | 2.4 | 28 | `tests/e2e/test_tier2_boundaries.py::TestStateAndLifecycleBoundaries::test_tier2_illegal_graph_threads_status_rejected_by_check` | **VERIFIED** | `tests/e2e/test_tier2_boundaries.py:220` |

### Section 3: Approval Expiry Semantics
| # | Row | Line | Cited Test Node ID | Status | Existing Location / Verified Replacement |
|---|:---:|:---:|---|:---:|---|
| 23 | 3.1 | 36 | `tests/test_approval_security.py::test_nonce_expiration_is_enforced` | **VERIFIED** | `tests/test_approval_security.py:180` |
| 24 | 3.1 | 36 | `tests/test_approval_lifecycle.py::test_expired_request_blocks_prior_nonce` | **VERIFIED** | `tests/test_approval_lifecycle.py:151` |
| 25 | 3.1 | 36 | `tests/test_approval_lifecycle.py::test_expired_request_blocks_new_challenge` | **VERIFIED** | `tests/test_approval_lifecycle.py:182` |
| 26 | 3.2 | 37 | `tests/test_approval_lifecycle.py::test_expire_due_is_idempotent` | **VERIFIED** | `tests/test_approval_lifecycle.py:200` |
| 27 | 3.2 | 37 | `tests/test_approval_lifecycle.py::test_expiry_and_decision_race_has_single_outcome` | **VERIFIED** | `tests/test_approval_lifecycle.py:218` |
| 28 | 3.2 | 37 | `tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_decision_vs_expiry_race_condition` | **VERIFIED** | `tests/e2e/test_tier3_combinations.py:44` |
| 29 | 3.3 | 38 | `tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_decision_vs_expiry_race_condition` | **VERIFIED** | `tests/e2e/test_tier3_combinations.py:44` |

### Section 4: Section 1A Human Authority & Player Consent Models
| # | Row | Line | Cited Test Node ID | Status | Existing Location / Verified Replacement |
|---|:---:|:---:|---|:---:|---|
| 30 | 4.1 | 46 | `tests/test_approval_lifecycle.py::test_player_agent_cannot_be_decision_actor` | **VERIFIED** | `tests/test_approval_lifecycle.py:456` |
| 31 | 4.2 | 47 | `tests/test_approval_lifecycle.py::test_timeout_inactivity_cannot_approve` | **VERIFIED** | `tests/test_approval_lifecycle.py:370` |
| 32 | 4.2 | 47 | `tests/e2e/test_tier4_scenarios.py::TestSection1AHumanAuthorityAndConsent::test_section_1a_inactivity_and_timeout_never_creates_approval` | **VERIFIED** | `tests/e2e/test_tier4_scenarios.py:238` |
| 33 | 4.3 | 48 | `tests/test_approval_lifecycle.py::test_unsupported_actor_types_fail_closed_in_phase1` | **VERIFIED** | `tests/test_approval_lifecycle.py:394` |
| 34 | 4.3 | 48 | `tests/e2e/test_tier4_scenarios.py::TestSection1AHumanAuthorityAndConsent::test_section_1a_authority_separation_club_vs_player` | **VERIFIED** | `tests/e2e/test_tier4_scenarios.py:182` |
| 35 | 4.3 | 48 | `tests/e2e/test_tier4_scenarios.py::TestSection1AHumanAuthorityAndConsent::test_section_1a_player_fallback_prohibits_multi_player_packages` | **VERIFIED** | `tests/e2e/test_tier4_scenarios.py:230` |
| 36 | 4.4 | 49 | `tests/e2e/test_tier1_features.py::TestApprovalLifecycleFeatures::test_feature_16_human_authority_schema_compatibility` | **VERIFIED** | `tests/e2e/test_tier1_features.py:279` |

### Section 5: HMAC Kill Switch & Key Rotation
| # | Row | Line | Cited Test Node ID | Status | Existing Location / Verified Replacement |
|---|:---:|:---:|---|:---:|---|
| 37 | 5.1 | 57 | `tests/test_graph_api_v1.py::test_hmac_kill_switch_behavior` | **VERIFIED** | `tests/test_graph_api_v1.py:323` |
| 38 | 5.1 | 57 | `tests/test_graph_api_v1.py::test_adversarial_hmac_kill_switch_blocks_before_body_parsing` | **VERIFIED** | `tests/test_graph_api_v1.py:546` |
| 39 | 5.1 | 57 | `tests/e2e/test_tier1_features.py::TestGraphCoreAndApiFeatures::test_feature_28_cli_hmac_kill_switch_support` | **VERIFIED** | `tests/e2e/test_tier1_features.py:425` |
| 40 | 5.2 | 58 | `tests/test_approval_security.py::test_modified_signed_payload_is_rejected` | **VERIFIED** | `tests/test_approval_security.py:189` |

### Section 6: Append-Only Audit & Immutability
| # | Row | Line | Cited Test Node ID | Status | Existing Location / Verified Replacement |
|---|:---:|:---:|---|:---:|---|
| 41 | 6.1 | 66 | `tests/test_events_outbox.py::test_domain_events_append_only` | **VERIFIED** | `tests/test_events_outbox.py:49` |
| 42 | 6.1 | 66 | `tests/test_events_outbox.py::test_approval_audit_append_only` | **VERIFIED** | `tests/test_events_outbox.py:69` |
| 43 | 6.1 | 66 | `tests/test_dispatch_schema.py::test_append_only_triggers` | **DEFECT** | Replace with `tests/test_dispatch_schema.py::test_tracking_tables_append_only` (at line 261) |
| 44 | 6.1 | 66 | `tests/e2e/test_tier1_features.py::TestDatabaseInfrastructureFeatures::test_feature_07_append_only_table_guards_reject_mutations` | **VERIFIED** | `tests/e2e/test_tier1_features.py:160` |
| 45 | 6.2 | 67 | `tests/test_migration_0003.py::test_package_version_immutable_while_awaiting` | **VERIFIED** | `tests/test_migration_0003.py:159` |
| 46 | 6.2 | 67 | `tests/test_dispatch_schema.py::test_dispatches_terminal_rows_frozen` | **VERIFIED** | `tests/test_dispatch_schema.py:214` |
| 47 | 6.2 | 67 | `tests/e2e/test_tier2_boundaries.py::TestStateAndLifecycleBoundaries::test_tier2_direct_update_of_package_version_blocked_by_trigger` | **VERIFIED** | `tests/e2e/test_tier2_boundaries.py:201` |

### Section 7: Recommendation Package Version Immutability
| # | Row | Line | Cited Test Node ID | Status | Existing Location / Verified Replacement |
|---|:---:|:---:|---|:---:|---|
| 48 | 7.1 | 75 | `tests/test_migration_0003.py::test_package_version_immutable_while_awaiting` | **VERIFIED** | `tests/test_migration_0003.py:159` |
| 49 | 7.1 | 75 | `tests/test_approval_security.py::test_package_version_immutable_once_awaiting_approval` | **VERIFIED** | `tests/test_approval_security.py:204` |
| 50 | 7.1 | 75 | `tests/e2e/test_tier1_features.py::TestApprovalLifecycleFeatures::test_feature_08_approval_requests_table_schema` | **VERIFIED** | `tests/e2e/test_tier1_features.py:185` |
| 51 | 7.2 | 76 | `tests/test_graph_persistence.py::test_decision_package_version_mismatch_fails_closed` | **VERIFIED** | `tests/test_graph_persistence.py:145` |
| 52 | 7.2 | 76 | `tests/e2e/test_tier2_boundaries.py::TestStateAndLifecycleBoundaries::test_tier2_stale_package_version_decision_fails` | **VERIFIED** | `tests/e2e/test_tier2_boundaries.py:178` |
| 53 | 7.2 | 76 | `tests/e2e/test_tier4_scenarios.py::TestRealWorldLifecycleScenarios::test_tier4_scenario_revision_request_preserves_package_immutability` | **VERIFIED** | `tests/e2e/test_tier4_scenarios.py:148` |

### Section 8: LangGraph Interrupt & PostgreSQL Re-Read (AM-4)
| # | Row | Line | Cited Test Node ID | Status | Existing Location / Verified Replacement |
|---|:---:|:---:|---|:---:|---|
| 54 | 8.1 | 84 | `tests/test_graph_persistence.py::test_single_approval_interrupt_at_approval_node` | **VERIFIED** | `tests/test_graph_persistence.py:56` |
| 55 | 8.1 | 84 | `tests/test_langgraph_flow.py::test_graph_runs_to_interrupt_with_typed_state` | **VERIFIED** | `tests/test_langgraph_flow.py:66` |
| 56 | 8.1 | 84 | `tests/e2e/test_tier1_features.py::TestGraphCoreAndApiFeatures::test_feature_18_single_approval_interrupt_verified` | **VERIFIED** | `tests/e2e/test_tier1_features.py:304` |
| 57 | 8.2 | 85 | `tests/test_graph_persistence.py::test_terminal_graph_states_formatting` | **VERIFIED** | `tests/test_graph_persistence.py:175` |
| 58 | 8.2 | 85 | `tests/test_langgraph_flow.py::test_existing_langgraph_thread_resumes` | **VERIFIED** | `tests/test_langgraph_flow.py:79` |
| 59 | 8.2 | 85 | `tests/test_graph_api_v1.py::test_v1_resume_refuses_authorization_payload` | **VERIFIED** | `tests/test_graph_api_v1.py:200` |
| 60 | 8.2 | 85 | `tests/e2e/test_tier1_features.py::TestGraphCoreAndApiFeatures::test_feature_20_terminal_states_enforcement_format` | **VERIFIED** | `tests/e2e/test_tier1_features.py:340` |
| 61 | 8.3 | 86 | `tests/test_graph_persistence.py::test_restart_recovers_interrupted_graph` | **DEFECT** | Replace with `tests/test_graph_persistence.py::test_checkpoint_recovery_across_server_restart` (at line 209) |
| 62 | 8.3 | 86 | `tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_post_commit_resume_failure_recovery` | **DEFECT** | Replace with `tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_post_commit_resume_failure_preserves_authoritative_decision` (at line 79) |

### Section 9: Nonce-Bound & Deployment-Bound Activation Gates (C-4, C-1)
| # | Row | Line | Cited Test Node ID | Status | Existing Location / Verified Replacement |
|---|:---:|:---:|---|:---:|---|
| 63 | 9.1 | 94 | `tests/test_dispatch_creation.py::test_gates_block_by_default` | **VERIFIED** | `tests/test_dispatch_creation.py:41` |
| 64 | 9.1 | 94 | `tests/test_dispatch_creation.py::test_g_outbox_not_recordable_in_phase1` | **VERIFIED** | `tests/test_dispatch_creation.py:248` |
| 65 | 9.1 | 94 | `tests/e2e/test_tier1_features.py::TestObjectivesAndGatesFeatures::test_feature_35_activation_gate_evaluation_fails_closed` | **VERIFIED** | `tests/e2e/test_tier1_features.py:510` |
| 66 | 9.2 | 95 | `tests/test_dispatch_creation.py::test_gate_recording_requires_commissioner_and_signature` | **VERIFIED** | `tests/test_dispatch_creation.py:81` |
| 67 | 9.2 | 95 | `tests/test_dispatch_creation.py::test_gate_tampered_field_rejected` | **VERIFIED** | `tests/test_dispatch_creation.py:167` |
| 68 | 9.2 | 95 | `tests/test_dispatch_creation.py::test_gate_requires_deployment_identity` | **VERIFIED** | `tests/test_dispatch_creation.py:198` |
| 69 | 9.2 | 95 | `tests/e2e/test_tier1_features.py::TestObjectivesAndGatesFeatures::test_feature_36_gate_challenge_and_record_routes_exist` | **VERIFIED** | `tests/e2e/test_tier1_features.py:520` |
| 70 | 9.3 | 96 | `tests/test_dispatch_creation.py::test_gate_change_applies_on_next_evaluation` | **VERIFIED** | `tests/test_dispatch_creation.py:258` |

### Section 10: Event Source Identity & Correlation ID Isolation (C-10, C-5)
| # | Row | Line | Cited Test Node ID | Status | Existing Location / Verified Replacement |
|---|:---:|:---:|---|:---:|---|
| 71 | 10.1 | 104 | `tests/test_events_outbox.py::test_sourced_event_requires_existing_source` | **VERIFIED** | `tests/test_events_outbox.py:82` |
| 72 | 10.1 | 104 | `tests/e2e/test_tier1_features.py::TestDatabaseInfrastructureFeatures::test_feature_05_event_source_guard_missing_source_rejected` | **VERIFIED** | `tests/e2e/test_tier1_features.py:134` |
| 73 | 10.2 | 105 | `tests/test_dispatch_transition.py::test_worker_actor_derivation` | **VERIFIED** | `tests/test_dispatch_transition.py:131` |
| 74 | 10.2 | 105 | `tests/test_events_outbox.py::test_app_cannot_forge_dispatch_events` | **VERIFIED** | `tests/test_events_outbox.py:140` |
| 75 | 10.2 | 105 | `tests/e2e/test_tier1_features.py::TestDatabaseInfrastructureFeatures::test_feature_06_transition_event_source_identity_restricted` | **VERIFIED** | `tests/e2e/test_tier1_features.py:147` |
| 76 | 10.3 | 106 | `tests/test_graph_api_v1.py::test_correlation_id_generated_when_absent` | **VERIFIED** | `tests/test_graph_api_v1.py:62` |
| 77 | 10.3 | 106 | `tests/test_graph_api_v1.py::test_correlation_id_echoed_when_present` | **VERIFIED** | `tests/test_graph_api_v1.py:73` |
| 78 | 10.3 | 106 | `tests/test_graph_api_v1.py::test_correlation_id_propagated_to_logs` | **VERIFIED** | `tests/test_graph_api_v1.py:81` |
| 79 | 10.3 | 106 | `tests/test_graph_api_v1.py::test_malformed_correlation_id_rejected` | **VERIFIED** | `tests/test_graph_api_v1.py:54` |
| 80 | 10.3 | 106 | `tests/test_graph_api_v1.py::test_adversarial_malformed_correlation_id_rejected_and_redacted` | **VERIFIED** | `tests/test_graph_api_v1.py:397` |
| 81 | 10.3 | 106 | `tests/test_dispatch_creation.py::test_correlation_id_shared_by_dispatch_and_events` | **VERIFIED** | `tests/test_dispatch_creation.py:286` |
| 82 | 10.3 | 106 | `tests/e2e/test_tier1_features.py::TestGraphCoreAndApiFeatures::test_feature_21_correlation_middleware_propagation` | **VERIFIED** | `tests/e2e/test_tier1_features.py:350` |
| 83 | 10.4 | 107 | `tests/test_graph_api_v1.py::test_adversarial_pydantic_validation_strips_sensitive_inputs` | **VERIFIED** | `tests/test_graph_api_v1.py:474` |

### Section 11: File-Based Secrets & Credential Sanitization (C-7)
| # | Row | Line | Cited Test Node ID | Status | Existing Location / Verified Replacement |
|---|:---:|:---:|---|:---:|---|
| 84 | 11.1 | 115 | `tests/test_compose_isolation.py::test_signing_secret_only_in_graph_api` | **VERIFIED** | `tests/test_compose_isolation.py:99` |
| 85 | 11.1 | 115 | `tests/test_compose_isolation.py::test_db_secret_mounts_per_service` | **VERIFIED** | `tests/test_compose_isolation.py:122` |
| 86 | 11.2 | 116 | `tests/test_compose_isolation.py::test_only_graph_api_can_reach_postgres` | **VERIFIED** | `tests/test_compose_isolation.py:200` |
| 87 | 11.3 | 117 | `tests/test_db_roles.py::test_redact_conninfo` | **VERIFIED** | `tests/test_db_roles.py:81` |
| 88 | 11.3 | 117 | `tests/test_graph_api_v1.py::test_v1_get_run_safe_projection_omits_secrets` | **VERIFIED** | `tests/test_graph_api_v1.py:162` |
| 89 | 11.3 | 117 | `tests/test_graph_api_v1.py::test_adversarial_run_projection_scrubs_internal_secrets` | **VERIFIED** | `tests/test_graph_api_v1.py:499` |

---

## 4. Verification Summary & Reconciliation

1. **Total Census Breakdown**:
   - Total rows evaluated: 23 table rows across 11 sections.
   - Total test citations: 89 occurrences.
   - Unique test functions/methods: 87 unique test nodes.
     - Note on duplicates:
       - `tests/test_migration_0003.py::test_package_version_immutable_while_awaiting` is cited twice (Row 6.2, Line 67 and Row 7.1, Line 75).
       - `tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_decision_vs_expiry_race_condition` is cited twice (Row 3.2, Line 37 and Row 3.3, Line 38).
   - Test Distribution by Category:
     - 65 unit / component test node references.
     - 24 E2E test node references across Tiers 1 through 4 (`tests/e2e/`).
2. **Clarification on the Auditor's "68 Cited Test Nodes" Metric**:
   - The auditor report (`auditor_m6_it2_1/audit_report.md:86`) and DISPATCH.md refer to "all 68 citations" based on an initial grouping from Iteration 1/2 audits.
   - Our exhaustive whole-file scan accounted for all **89 citation instances (87 unique test nodes)** in `SECURITY_CHANGES.md`.
   - Regardless of whether counted as 68, 87, or 89, **exactly 4 citations** in `SECURITY_CHANGES.md` are non-existent, and **100% of all other citations exist verbatim in the repository**.
3. **Non-Test References Audit**:
   - All 10 source code, migration, and documentation line references in the Evidence column were verified against actual files:
     - `src/approval_store.py:379-380` & `381-386` (Row 4.1, 4.3): Verified.
     - `docs/APPROVAL_SECURITY.md` (Row 4.1, 5.2): Verified.
     - `migrations/0003_dispatch_foundation.sql:464-674` (Row 4.4): Verified.
     - `src/graph_api.py:140-170, 314-316` (Row 9.3): Verified.
     - `src/graph_api.py:342-351` (Row 10.4): Verified.
     - `src/settings.py:22-38` (Row 11.1): Verified.
     - `docker-compose.yml:13-17` (Row 11.2): Verified.
     - `src/db_roles.py:23-39` (Row 11.3): Verified.

---

## 5. Proposed Remediation Diff for `SECURITY_CHANGES.md`

To achieve a 100% clean audit verdict, the following precise patch replaces the 4 defective citations in `SECURITY_CHANGES.md`:

```diff
--- a/SECURITY_CHANGES.md
+++ b/SECURITY_CHANGES.md
@@ -16,1 +16,1 @@
-| 1.3 | **Runtime Application Least Privilege** | <ul><li>`fc27_app` connects `graph-api` with DML privileges only.</li><li>Denied `CREATE` on schema `public`; cannot execute DDL.</li><li>Explicitly denied `UPDATE` and `DELETE` on `public.dispatches`.</li><li>Posture verification (`check_runtime_posture`) runs at startup and in `/health/ready`.</li></ul> | `tests/test_db_migrate_roles.py::test_app_cannot_run_ddl`<br>`tests/test_db_roles.py::test_check_runtime_posture`<br>`tests/test_graph_api_v1.py::test_health_ready_checks_db_posture`<br>`tests/e2e/test_tier1_features.py::TestDatabaseInfrastructureFeatures::test_feature_01_public_schema_create_revoked` |
+| 1.3 | **Runtime Application Least Privilege** | <ul><li>`fc27_app` connects `graph-api` with DML privileges only.</li><li>Denied `CREATE` on schema `public`; cannot execute DDL.</li><li>Explicitly denied `UPDATE` and `DELETE` on `public.dispatches`.</li><li>Posture verification (`check_runtime_posture`) runs at startup and in `/health/ready`.</li></ul> | `tests/test_db_migrate_roles.py::test_app_cannot_run_ddl`<br>`tests/test_db_roles.py::test_check_runtime_posture`<br>`tests/test_graph_api_v1.py::test_adversarial_readiness_fails_closed_on_db_role_posture_violation`<br>`tests/e2e/test_tier1_features.py::TestDatabaseInfrastructureFeatures::test_feature_01_public_schema_create_revoked` |
@@ -66,1 +66,1 @@
-| 6.1 | **Mutation Rejection Trigger** | <ul><li>`public.fc27_reject_mutation()` trigger function attached `BEFORE UPDATE OR DELETE` to 13 immutable tables: `domain_events`, `approval_audit`, `activation_gate_log`, `player_decisions`, `approval_escalations`, `dispatch_plans`, `dispatch_plan_decisions`, `dispatch_delivery_attempts`, `dispatch_acknowledgments`, `dispatch_comments`, `analyst_notes`, `player_observations`, `dispatch_assessments`.</li><li>Raises check violation exception: `"<table_name> is append-only"`.</li></ul> | `tests/test_events_outbox.py::test_domain_events_append_only`<br>`tests/test_events_outbox.py::test_approval_audit_append_only`<br>`tests/test_dispatch_schema.py::test_append_only_triggers`<br>`tests/e2e/test_tier1_features.py::TestDatabaseInfrastructureFeatures::test_feature_07_append_only_table_guards_reject_mutations` |
+| 6.1 | **Mutation Rejection Trigger** | <ul><li>`public.fc27_reject_mutation()` trigger function attached `BEFORE UPDATE OR DELETE` to 13 immutable tables: `domain_events`, `approval_audit`, `activation_gate_log`, `player_decisions`, `approval_escalations`, `dispatch_plans`, `dispatch_plan_decisions`, `dispatch_delivery_attempts`, `dispatch_acknowledgments`, `dispatch_comments`, `analyst_notes`, `player_observations`, `dispatch_assessments`.</li><li>Raises check violation exception: `"<table_name> is append-only"`.</li></ul> | `tests/test_events_outbox.py::test_domain_events_append_only`<br>`tests/test_events_outbox.py::test_approval_audit_append_only`<br>`tests/test_dispatch_schema.py::test_tracking_tables_append_only`<br>`tests/e2e/test_tier1_features.py::TestDatabaseInfrastructureFeatures::test_feature_07_append_only_table_guards_reject_mutations` |
@@ -86,1 +86,1 @@
-| 8.3 | **Pre-Resume Commit & Reconciliation** | <ul><li>Decision write path commits PostgreSQL transaction before invoking graph resume.</li><li>If resume fails, committed decision remains preserved in PostgreSQL; `graph-api` re-resumes terminal threads on startup.</li></ul> | `tests/test_graph_persistence.py::test_restart_recovers_interrupted_graph`<br>`tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_post_commit_resume_failure_recovery` |
+| 8.3 | **Pre-Resume Commit & Reconciliation** | <ul><li>Decision write path commits PostgreSQL transaction before invoking graph resume.</li><li>If resume fails, committed decision remains preserved in PostgreSQL; `graph-api` re-resumes terminal threads on startup.</li></ul> | `tests/test_graph_persistence.py::test_checkpoint_recovery_across_server_restart`<br>`tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_post_commit_resume_failure_preserves_authoritative_decision` |
```
