# Handoff Report: Remediation Analysis for `SECURITY_CHANGES.md` Citations

**Agent**: `explorer_m6_it3_sec_1`  
**Archetype**: `teamwork_preview_explorer` (roles: explorer, investigator, synthesizer)  
**Task**: Investigate and formulate exact remediation for lines 16 and 66 of `SECURITY_CHANGES.md`  
**Date**: 2026-10-03  

---

## 1. Observation

### Observation 1: Line 16 of `SECURITY_CHANGES.md` (Row 1.3)
- Current text at `SECURITY_CHANGES.md:16`:
  ```markdown
  | 1.3 | **Runtime Application Least Privilege** | <ul><li>`fc27_app` connects `graph-api` with DML privileges only.</li><li>Denied `CREATE` on schema `public`; cannot execute DDL.</li><li>Explicitly denied `UPDATE` and `DELETE` on `public.dispatches`.</li><li>Posture verification (`check_runtime_posture`) runs at startup and in `/health/ready`.</li></ul> | `tests/test_db_migrate_roles.py::test_app_cannot_run_ddl`<br>`tests/test_db_roles.py::test_check_runtime_posture`<br>`tests/test_graph_api_v1.py::test_health_ready_checks_db_posture`<br>`tests/e2e/test_tier1_features.py::TestDatabaseInfrastructureFeatures::test_feature_01_public_schema_create_revoked` |
  ```
- Tool observation: Grep/AST search across `tests/test_graph_api_v1.py` and the entire repository confirms function `test_health_ready_checks_db_posture` does not exist.
- Inspection of `tests/test_graph_api_v1.py` lines 719–746 revealed the exact test:
  ```python
  def test_adversarial_readiness_fails_closed_on_db_role_posture_violation(pg_migrator_uri, pg_uri):
      """Adversarial Attack: Verify boot-time and runtime posture enforcement fail closed.

      1. Boot-time: startup crashes with ConfigError if role has DDL privileges.
      2. Runtime /health/ready: returns 503 if role posture check fails.
      """
  ```

### Observation 2: Line 66 of `SECURITY_CHANGES.md` (Row 6.1)
- Current text at `SECURITY_CHANGES.md:66`:
  ```markdown
  | 6.1 | **Mutation Rejection Trigger** | <ul><li>`public.fc27_reject_mutation()` trigger function attached `BEFORE UPDATE OR DELETE` to 13 immutable tables: `domain_events`, `approval_audit`, `activation_gate_log`, `player_decisions`, `approval_escalations`, `dispatch_plans`, `dispatch_plan_decisions`, `dispatch_delivery_attempts`, `dispatch_acknowledgments`, `dispatch_comments`, `analyst_notes`, `player_observations`, `dispatch_assessments`.</li><li>Raises check violation exception: `"<table_name> is append-only"`.</li></ul> | `tests/test_events_outbox.py::test_domain_events_append_only`<br>`tests/test_events_outbox.py::test_approval_audit_append_only`<br>`tests/test_dispatch_schema.py::test_append_only_triggers`<br>`tests/e2e/test_tier1_features.py::TestDatabaseInfrastructureFeatures::test_feature_07_append_only_table_guards_reject_mutations` |
  ```
- Tool observation: Grep/AST search confirms function `test_append_only_triggers` does not exist in `tests/test_dispatch_schema.py` or anywhere in the repository.
- Inspection of `tests/test_dispatch_schema.py` lines 251–263 revealed the exact test:
  ```python
  @pytest.mark.parametrize("table", [
      "dispatch_plans",
      "dispatch_plan_decisions",
      "dispatch_delivery_attempts",
      "dispatch_acknowledgments",
      "dispatch_comments",
      "analyst_notes",
      "player_observations",
      "dispatch_assessments",
  ])
  def test_tracking_tables_append_only(pg_admin_db_uri, make_dispatch, table):
      """All tracking tables have append-only trigger rejecting UPDATE and DELETE."""
  ```

### Observation 3: Ancillary Citations at Line 86 of `SECURITY_CHANGES.md` (Row 8.3)
- Current text at `SECURITY_CHANGES.md:86`:
  ```markdown
  | 8.3 | **Pre-Resume Commit & Reconciliation** | <ul><li>Decision write path commits PostgreSQL transaction before invoking graph resume.</li><li>If resume fails, committed decision remains preserved in PostgreSQL; `graph-api` re-resumes terminal threads on startup.</li></ul> | `tests/test_graph_persistence.py::test_restart_recovers_interrupted_graph`<br>`tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_post_commit_resume_failure_recovery` |
  ```
- Function `test_restart_recovers_interrupted_graph` does not exist in `tests/test_graph_persistence.py`. The real test at line 209 is `test_checkpoint_recovery_across_server_restart`.
- Method `test_tier3_post_commit_resume_failure_recovery` does not exist in `TestCrossFeatureCombinations`. The real test at line 79 of `tests/e2e/test_tier3_combinations.py` is `test_tier3_post_commit_resume_failure_preserves_authoritative_decision`.

---

## 2. Logic Chain

1. **Step 1 (Grounding Claim in Reality)**: In formal security attestation documents like `SECURITY_CHANGES.md`, evidence citations must refer to authentic, executable test node IDs (Observation 1, 2). Citing non-existent test names causes collection errors in automated test runners (`pytest <node_id>`) and triggers audit integrity violations.
2. **Step 2 (Mapping Row 1.3 to Proving Evidence)**: Row 1.3 claims that posture verification (`check_runtime_posture`) runs at startup and in `/health/ready`. In `tests/test_graph_api_v1.py:719`, `test_adversarial_readiness_fails_closed_on_db_role_posture_violation` explicitly tests boot-time startup failure on DDL role posture violation and runtime `/health/ready` 503 degraded failure when posture check fails. Thus, `test_adversarial_readiness_fails_closed_on_db_role_posture_violation` is the direct 1:1 proving test for Row 1.3.
3. **Step 3 (Mapping Row 6.1 to Proving Evidence)**: Row 6.1 claims that `public.fc27_reject_mutation()` prevents UPDATE and DELETE on immutable tables, specifically citing `dispatch_plans`, `dispatch_plan_decisions`, `dispatch_delivery_attempts`, `dispatch_acknowledgments`, `dispatch_comments`, `analyst_notes`, `player_observations`, and `dispatch_assessments`. In `tests/test_dispatch_schema.py:261`, `test_tracking_tables_append_only` is parametrized across all 8 of these tables, verifying that UPDATE and DELETE raise `CheckViolation` with `"<table_name> is append-only"`. Thus, `test_tracking_tables_append_only` is the direct 1:1 proving test for Row 6.1.
4. **Step 4 (Comprehensive Coverage for Row 8.3)**: To ensure that the worker's subsequent edit achieves a 100% CLEAN audit verdict without requiring another review loop, the 2 citations at line 86 must also be replaced with `test_checkpoint_recovery_across_server_restart` (line 209 of `tests/test_graph_persistence.py`) and `test_tier3_post_commit_resume_failure_preserves_authoritative_decision` (line 79 of `tests/e2e/test_tier3_combinations.py`).

---

## 3. Caveats

- **No Caveats**: The investigation inspected all target test files directly, traced AST lines, and cross-referenced the Forensic Auditor report (`auditor_m6_it2_1/audit_report.md`), Reviewer report (`reviewer_m6_it2_2/review.md`), and Challenger report (`challenger_m6_it2_2/challenge_report.md`).
- Per investigator role constraints, no production source code or test files were modified.

---

## 4. Conclusion

The exact test replacements and drop-in text blocks for `SECURITY_CHANGES.md` are:

### Task 1: Row 1.3 (Line 16)
- **Replace**: `tests/test_graph_api_v1.py::test_health_ready_checks_db_posture`
- **With**: `tests/test_graph_api_v1.py::test_adversarial_readiness_fails_closed_on_db_role_posture_violation`
- **Drop-in Text**:
  ```markdown
  | 1.3 | **Runtime Application Least Privilege** | <ul><li>`fc27_app` connects `graph-api` with DML privileges only.</li><li>Denied `CREATE` on schema `public`; cannot execute DDL.</li><li>Explicitly denied `UPDATE` and `DELETE` on `public.dispatches`.</li><li>Posture verification (`check_runtime_posture`) runs at startup and in `/health/ready`.</li></ul> | `tests/test_db_migrate_roles.py::test_app_cannot_run_ddl`<br>`tests/test_db_roles.py::test_check_runtime_posture`<br>`tests/test_graph_api_v1.py::test_adversarial_readiness_fails_closed_on_db_role_posture_violation`<br>`tests/e2e/test_tier1_features.py::TestDatabaseInfrastructureFeatures::test_feature_01_public_schema_create_revoked` |
  ```

### Task 2: Row 6.1 (Line 66)
- **Replace**: `tests/test_dispatch_schema.py::test_append_only_triggers`
- **With**: `tests/test_dispatch_schema.py::test_tracking_tables_append_only`
- **Drop-in Text**:
  ```markdown
  | 6.1 | **Mutation Rejection Trigger** | <ul><li>`public.fc27_reject_mutation()` trigger function attached `BEFORE UPDATE OR DELETE` to 13 immutable tables: `domain_events`, `approval_audit`, `activation_gate_log`, `player_decisions`, `approval_escalations`, `dispatch_plans`, `dispatch_plan_decisions`, `dispatch_delivery_attempts`, `dispatch_acknowledgments`, `dispatch_comments`, `analyst_notes`, `player_observations`, `dispatch_assessments`.</li><li>Raises check violation exception: `"<table_name> is append-only"`.</li></ul> | `tests/test_events_outbox.py::test_domain_events_append_only`<br>`tests/test_events_outbox.py::test_approval_audit_append_only`<br>`tests/test_dispatch_schema.py::test_tracking_tables_append_only`<br>`tests/e2e/test_tier1_features.py::TestDatabaseInfrastructureFeatures::test_feature_07_append_only_table_guards_reject_mutations` |
  ```

### Task 3 / Comprehensive Fix: Row 8.3 (Line 86)
- **Replace**:
  - `tests/test_graph_persistence.py::test_restart_recovers_interrupted_graph` -> `tests/test_graph_persistence.py::test_checkpoint_recovery_across_server_restart`
  - `tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_post_commit_resume_failure_recovery` -> `tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_post_commit_resume_failure_preserves_authoritative_decision`
- **Drop-in Text**:
  ```markdown
  | 8.3 | **Pre-Resume Commit & Reconciliation** | <ul><li>Decision write path commits PostgreSQL transaction before invoking graph resume.</li><li>If resume fails, committed decision remains preserved in PostgreSQL; `graph-api` re-resumes terminal threads on startup.</li></ul> | `tests/test_graph_persistence.py::test_checkpoint_recovery_across_server_restart`<br>`tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_post_commit_resume_failure_preserves_authoritative_decision` |
  ```

---

## 5. Verification Method

To independently verify the validity and executability of all four replacement test nodes:

1. **Verify Line 16 Replacement**:
   ```bash
   pytest tests/test_graph_api_v1.py::test_adversarial_readiness_fails_closed_on_db_role_posture_violation
   ```
2. **Verify Line 66 Replacement**:
   ```bash
   pytest tests/test_dispatch_schema.py::test_tracking_tables_append_only
   ```
3. **Verify Line 86 Replacements**:
   ```bash
   pytest tests/test_graph_persistence.py::test_checkpoint_recovery_across_server_restart
   pytest tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_post_commit_resume_failure_preserves_authoritative_decision
   ```

**Invalidation Conditions**:
- If any of the above commands fails with `ERROR: not found`, the test citation does not exist.
- If any test does not exercise the specific invariant described in the corresponding row of `SECURITY_CHANGES.md`, the citation is invalid.
