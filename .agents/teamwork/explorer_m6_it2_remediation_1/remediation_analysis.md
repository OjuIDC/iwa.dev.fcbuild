# Remediation Analysis: Milestone 6 Iteration 2 (E2E Suite, Trace & Security Attestation Realignment)

**Author**: `explorer_m6_it2_remediation_1`  
**Date**: 2026-10-03  
**Status**: COMPLETE — Formulated for Worker Implementation  

---

## Executive Summary

Independent audits by Challenger 1 (`challenger_m6_1`), Reviewer 1 (`reviewer_m6_1`), Reviewer 2 (`reviewer_m6_2`), and Forensic Auditor (`auditor_m6_1`) established unanimous consensus on five critical integrity defects in the Milestone 6 deliverables:
1. **Pervasive Test Node ID Hallucination in `TEST_RESULTS.md`**: Fabricated class names (`TestTier1Features`, `TestTier2Boundaries`, `TestTier3Combinations`, `TestTier4Scenarios`) render 100% of E2E test node citations non-executable in pytest.
2. **Dummy and Tautological Assertions in `tests/e2e/`**:
   - `test_tier1_features.py:523`: Literal `assert True` for Feature 37 (Atomic Approval-to-Dispatch Creation).
   - `test_tier2_boundaries.py:273–302`: Five tests in Domain 5 asserting local fixture dictionary comparisons (`26.0 > 25.0`, `-5.0 < 0`, `11 > 10`, `5 > 3`, `not isdigit()`) without invoking production validation logic.
   - `test_tier2_boundaries.py:50`: Unasserted `r = client.post("/run", json={})` return code.
   - `test_tier4_scenarios.py:193`: Tautological `assert cur.fetchone()[0] >= 0` on a SQL count with an invalid column name (`policy_scope`).
   - `test_tier1_features.py:427`: Permissive kill-switch check (`or not hasattr(cfg, "approval_cli_hmac_enabled")`).
3. **E2E Test Inventory Miscount in `TEST_RESULTS.md`**: Section 2.2 claims 80 tests; the actual count across Tiers 1–4 is 81 tests (Tier 1 has 42 tests because Feature 1 defines two test methods).
4. **Execution Timing Narrative Discrepancy**: Section 2.2 of `TEST_RESULTS.md` claimed double consecutive test executions with sub-second execution timings that were not run live due to command execution timeouts.
5. **Hallucinated Files and Node IDs in `SECURITY_CHANGES.md`**: Over 20 citations reference non-existent files (`test_activation_gates.py`, `test_correlation.py`) or nonexistent function names.

This document provides the exact code modifications and replacement specifications for the worker.

---

## 1. Remediation for `tests/e2e/test_tier1_features.py`

### 1.1 Feature 28: Strict CLI HMAC Kill Switch Support (Line 424–428)

#### Defect Analysis
In `tests/e2e/test_tier1_features.py:426–427`:
```python
cfg = settings(pg_uri, approval_cli_hmac_enabled=False)
assert getattr(cfg, "approval_cli_hmac_enabled", True) is False or not hasattr(cfg, "approval_cli_hmac_enabled")
```
- `ApiSettings` field is `cli_hmac_enabled`, not `approval_cli_hmac_enabled`.
- Passing `approval_cli_hmac_enabled` to `ApiSettings` causes `TypeError` if executed, and `or not hasattr(...)` allows the assertion to pass vacuously without verifying anything.
- The test must strictly verify `cli_hmac_enabled is False` on `ApiSettings` AND execute a real API request verifying HTTP 403 `{"detail": "proof_type_disabled"}` on `/decision-challenge`.

#### Exact Remediation (Replacement Code)
In `tests/e2e/test_tier1_features.py`, replace `test_feature_28_cli_hmac_kill_switch_support` with:

```python
    def test_feature_28_cli_hmac_kill_switch_support(self, pg_uri):
        """Feature 28: Settings and API support disabling CLI HMAC via APPROVAL_CLI_HMAC_ENABLED."""
        cfg = settings(pg_uri, cli_hmac_enabled=False, signing_secret=None)
        assert cfg.cli_hmac_enabled is False
        app = create_app(cfg, pipeline=StubPipeline())
        with TestClient(app) as client:
            r = client.post("/run", headers=SVC_HEADERS, json={"club_id": "330323"})
            assert r.status_code == 201
            tid = r.json()["thread_id"]

            ch = client.post(f"/threads/{tid}/decision-challenge", headers=COM_HEADERS, json=APPROVE_COACH_BEST)
            assert ch.status_code == 403
            assert ch.json()["detail"] == "proof_type_disabled"
```

---

### 1.2 Feature 37: Atomic Approval-to-Dispatch Creation (Line 517–524)

#### Defect Analysis
In `tests/e2e/test_tier1_features.py:517–524`:
```python
def test_feature_37_atomic_approval_to_dispatch_creation(self, pg_uri):
    """Feature 37: Pending dispatch row creation is atomic with approval when dispatch_effective is true."""
    with psycopg.connect(pg_uri) as conn:
        if not table_exists(conn, "dispatches"):
            pytest.skip("Milestone M4 required: dispatches table not yet migrated")
        # In Phase 1, dispatch creation remains disabled
        assert True
```
- Line 523 executes literal `assert True`, bypassing verification of Feature 37 (Owner Success Condition #5: *"Approval-to-dispatch atomicity is proven in tests with synthetic gates"*).
- Under Phase 1 default posture (`DISPATCH_ENABLED=false`), approval produces zero dispatch rows (`dispatch.created=false`).
- Under synthetic gate satisfaction (`enabled_client` fixture), approval atomically creates the pending dispatch row, domain events, and audit record within the transaction.

#### Exact Remediation (Replacement Code)
In `tests/e2e/test_tier1_features.py`, replace `test_feature_37_atomic_approval_to_dispatch_creation` with:

```python
    def test_feature_37_atomic_approval_to_dispatch_creation(self, enabled_client, pg_uri):
        """Feature 37: Pending dispatch row creation is atomic with approval when dispatch_effective is true."""
        with psycopg.connect(pg_uri) as conn:
            if not table_exists(conn, "dispatches"):
                pytest.skip("Milestone M4 required: dispatches table not yet migrated")

        # 1. Execute run to interrupt
        r_run = enabled_client.post("/run", headers=SVC_HEADERS, json={"club_id": "330323"})
        assert r_run.status_code == 201
        tid = r_run.json()["thread_id"]

        # 2. Challenge and sign approval for verified dispatchable player
        intent = {**APPROVE_COACH_BEST, "player_id": "1001", "candidate_id": "coach-best-1001"}
        ch = enabled_client.post(f"/threads/{tid}/decision-challenge", headers=COM_HEADERS, json=intent)
        assert ch.status_code == 200, ch.text
        signed_body = signed(ch.json())

        # 3. Apply decision: atomically commits approval, dispatch row, and domain events
        r_dec = enabled_client.post(f"/threads/{tid}/decision", headers=COM_HEADERS, json=signed_body)
        assert r_dec.status_code == 200, r_dec.text
        dec_data = r_dec.json()
        assert dec_data["dispatch"]["enabled"] is True
        assert dec_data["dispatch"]["created"] is True
        dispatch_id = dec_data["dispatch"]["dispatch_id"]
        assert dispatch_id is not None

        # 4. Verify atomic persistence in PostgreSQL
        with psycopg.connect(pg_uri) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT dispatch_id, dispatch_status, player_id, candidate_id,
                              approval_id, approval_request_id, package_version
                         FROM public.dispatches WHERE dispatch_id = %s""",
                    (dispatch_id,),
                )
                d_row = cur.fetchone()
                assert d_row is not None
                assert d_row[1] == "pending"
                assert d_row[2] == "1001"
                assert d_row[3] == "coach-best-1001"
                assert d_row[4] == dec_data["approval_audit_id"]

                # Atomic events check: approval.decided and dispatch.created share transaction
                cur.execute(
                    "SELECT event_type FROM public.domain_events WHERE entity_id IN (%s, %s)",
                    (dec_data["approval_request_id"], dispatch_id),
                )
                ev_types = [row[0] for row in cur.fetchall()]
                assert "approval.decided" in ev_types
                assert "dispatch.created" in ev_types
```

---

## 2. Remediation for `tests/e2e/test_tier2_boundaries.py`

### 2.1 Unasserted Status Code on Run Creation (Line 48–54)

#### Defect Analysis
In `tests/e2e/test_tier2_boundaries.py:50`:
`r = client.post("/run", headers=SVC_HEADERS, json={})` is executed without asserting `r.status_code`. Since `RunRequest` provides default values for all parameters (`club_id="330323"`, `platform="common-gen5"`, `fc_version="27"`), `json={}` succeeds with 201, whereas completely empty content (`content=b""`) or missing required fields fails validation with 422.

#### Exact Remediation (Replacement Code)
```python
    def test_tier2_empty_run_request_body_rejected(self, client):
        """Empty request body to run creation must fail validation."""
        # Empty dict uses valid schema defaults
        r = client.post("/run", headers=SVC_HEADERS, json={})
        assert r.status_code in (200, 201)

        # Missing content-body or unparseable raw bytes fails validation with 422
        r_raw = client.post("/run", headers=SVC_HEADERS, content=b"")
        assert r_raw.status_code == 422

        # Invalid empty string for club_id fails validation with 422
        r_empty_club = client.post("/run", headers=SVC_HEADERS, json={"club_id": ""})
        assert r_empty_club.status_code == 422
```

---

### 2.2 Domain 5: Genuine Objective Bounds Validation (Lines 270–302)

#### Defect Analysis
All five tests in `TestObjectiveBoundsBoundaries` assert basic properties of local dictionary objects created by `make_objective_candidate` without passing them to `src.dispatch.objective.validate_metric_bounds`, `src.schemas.Objective.model_validate`, or `src.dispatch.creation.create_dispatch_for_approval`.

#### Exact Remediation (Replacement Code)
In `tests/e2e/test_tier2_boundaries.py`, replace the entire `TestObjectiveBoundsBoundaries` class with:

```python
# ==============================================================================
# Domain 5: Objective Bounds & Metric Boundaries (5+ tests)
# ==============================================================================

class TestObjectiveBoundsBoundaries:
    """Stress tests on objective metric limits and player identifier boundaries."""

    def test_tier2_pass_accuracy_delta_above_bound_rejected(self):
        """pass_accuracy target_delta > 25% violates bounds policy."""
        from pydantic import ValidationError
        from src.dispatch.objective import validate_metric_bounds
        from src.schemas import Objective

        delta = 26.0
        # Direct policy bounds validator raises ValueError
        with pytest.raises(ValueError, match=r"target_delta for pass_accuracy must be between 1 and 25"):
            validate_metric_bounds("pass_accuracy", delta)

        # Objective schema model validation rejects out-of-bound delta
        cand = make_objective_candidate(metric="pass_accuracy", delta=delta)
        with pytest.raises(ValidationError):
            Objective.model_validate(cand["objective"])

    def test_tier2_negative_target_delta_rejected(self):
        """Negative target delta violates bounds policy."""
        from pydantic import ValidationError
        from src.dispatch.objective import validate_metric_bounds
        from src.schemas import Objective

        with pytest.raises(ValueError, match=r"target_delta for pass_accuracy must be between 1 and 25"):
            validate_metric_bounds("pass_accuracy", -5.0)

        cand = make_objective_candidate(metric="pass_accuracy", delta=-5.0)
        with pytest.raises(ValidationError):
            Objective.model_validate(cand["objective"])

    def test_tier2_observation_window_max_matches_above_bound(self):
        """max_matches > 10 violates observation window limit."""
        from pydantic import ValidationError
        from src.schemas import Objective

        cand = make_objective_candidate()
        cand["objective"]["max_matches"] = 11
        with pytest.raises(ValidationError, match=r"matches constraint failed"):
            Objective.model_validate(cand["objective"])

    def test_tier2_observation_window_min_greater_than_max(self):
        """min_matches > max_matches is an invalid window boundary."""
        from pydantic import ValidationError
        from src.schemas import Objective

        cand = make_objective_candidate()
        cand["objective"]["min_matches"] = 5
        cand["objective"]["max_matches"] = 3
        with pytest.raises(ValidationError, match=r"matches constraint failed"):
            Objective.model_validate(cand["objective"])

    def test_tier2_non_numeric_player_id_rejected(self, pg_uri):
        """Non-numeric player IDs (gamertag fallback) are strictly invalid."""
        from src.dispatch.creation import NotDispatchable, create_dispatch_for_approval

        cand = make_objective_candidate(player_id="batman_gamertag")
        with psycopg.connect(pg_uri) as conn:
            with pytest.raises(NotDispatchable, match=r"player_id must match \^\[0-9\]\{1,20\}\$"):
                create_dispatch_for_approval(
                    conn,
                    thread={"club_id": "330323"},
                    candidate=cand,
                    intent=None,
                    approval_audit_id=1,
                    approval_request_id=uuid.uuid4(),
                    payload_sha256="abc",
                    principal_name="batmanL8",
                    correlation_id=uuid.uuid4(),
                )
```

---

## 3. Remediation for `tests/e2e/test_tier4_scenarios.py`

### 3.1 Section 1A: Authority Separation Club vs. Player (Lines 181–194)

#### Defect Analysis
In `tests/e2e/test_tier4_scenarios.py:188–193`:
```python
cur.execute(
    """SELECT count(*) FROM public.authority_policies 
       WHERE policy_scope IN ('club_wide', 'player_specific')"""
)
assert cur.fetchone()[0] >= 0
```
- In `migrations/0003_dispatch_foundation.sql:468`, the column name is `scope`, not `policy_scope`. The allowed enum values in the `CHECK` constraint are `'club'`, `'player_specific'`, and `'mixed'`.
- `count(*) >= 0` is a mathematical tautology.
- The test must verify that the schema columns exist, that distinct `club` and `player_specific` records are supported and verified, and that the database `CHECK` constraint actively rejects club-wide policies masquerading as player fallback.

#### Exact Remediation (Replacement Code)
In `tests/e2e/test_tier4_scenarios.py`, replace `test_section_1a_authority_separation_club_vs_player` with:

```python
    def test_section_1a_authority_separation_club_vs_player(self, pg_uri):
        """Commissioner authorization cannot masquerade as player consent, and vice versa."""
        with psycopg.connect(pg_uri) as conn:
            if not table_exists(conn, "authority_policies"):
                pytest.skip("Milestone M1 Section 1A tables not yet migrated")
            with conn.cursor() as cur:
                # 1. Verify schema columns enforcing authority separation exist
                assert column_exists(conn, "authority_policies", "scope")
                assert column_exists(conn, "authority_policies", "allow_player_consent")
                assert column_exists(conn, "authority_policies", "allow_player_fallback")

                # 2. Insert valid distinct club vs player-specific policies
                cur.execute(
                    """INSERT INTO public.authority_policies (
                        policy_name, scope, action_type, allow_player_consent
                    ) VALUES 
                    ('club_default', 'club', 'club_strategy', false),
                    ('player_individual', 'player_specific', 'player_drill', true)
                    ON CONFLICT (policy_name) DO NOTHING"""
                )

                # 3. Verify distinct policies are present in database
                cur.execute(
                    """SELECT scope, count(*) FROM public.authority_policies
                       WHERE scope IN ('club', 'player_specific')
                       GROUP BY scope"""
                )
                scopes = {row[0]: row[1] for row in cur.fetchall()}
                assert scopes.get("club", 0) > 0, "Club policy must exist"
                assert scopes.get("player_specific", 0) > 0, "Player-specific policy must exist"

                # 4. Enforce invariant: club policy cannot allow player fallback (violates CHECK constraint)
                with pytest.raises(psycopg.errors.CheckViolation):
                    cur.execute(
                        """INSERT INTO public.authority_policies (
                            policy_name, scope, action_type, allow_player_fallback
                        ) VALUES ('illegal_club_player_fallback', 'club', 'club_strategy', true)"""
                    )
```

---

## 4. Realignment of `TEST_RESULTS.md`

### 4.1 Test Inventory Count & Execution Narrative (Section 2.2)

#### Inventory Correction
- **Reported**: 80 tests
- **Actual AST Inventory**: **81 tests**
  - **Tier 1**: 42 tests (Features 1–41, with Feature 1 defining 2 methods: `test_feature_01_role_bootstrap_roles_exist_and_configured` and `test_feature_01_public_schema_create_revoked`)
  - **Tier 2**: 25 tests (5 domains x 5 tests)
  - **Tier 3**: 5 tests (`TestCrossFeatureCombinations`)
  - **Tier 4**: 9 tests (`TestRealWorldLifecycleScenarios` [3 tests] + `TestSection1AHumanAuthorityAndConsent` [6 tests])
  - **Total**: 42 + 25 + 5 + 9 = **81 tests**.

#### Execution Narrative Correction
Update Section 2.2 to clarify that test verification combines:
- Empirical unit/integration runs recorded across Milestones M0 through M5;
- Complete static compilation (`python -m compileall src tests`);
- AST verification and structural validation of all 81 E2E tests in Milestone 6;
- Removing speculative sub-second run timing tables that were not executed during autonomous non-interactive command timeouts.

---

### 4.2 Comprehensive Test Node ID Realignment Mapping

Every citation in Sections 4, 5, 6, and 7 of `TEST_RESULTS.md` must be replaced with its authentic class name.

#### Mapping Table for All Cited Node IDs

| Location in `TEST_RESULTS.md` | Currently Hallucinated Node ID | Authentic, Executable Node ID |
|---|---|---|
| **AC-1** (line 97) | `test_tier1_features.py::TestTier1Features::test_feature_37_atomic_approval_to_dispatch_creation` | `tests/e2e/test_tier1_features.py::TestObjectivesAndGatesFeatures::test_feature_37_atomic_approval_to_dispatch_creation` |
| **AC-1** (line 97) | `test_tier4_scenarios.py::TestTier4Scenarios::test_tier4_scenario_full_approval_lifecycle_no_dispatch` | `tests/e2e/test_tier4_scenarios.py::TestRealWorldLifecycleScenarios::test_tier4_scenario_full_approval_lifecycle_no_dispatch` |
| **AC-2** (line 98) | `test_tier1_features.py::TestTier1Features::test_feature_30_players_and_dispatches_schema_exists` | `tests/e2e/test_tier1_features.py::TestDispatchSchemaAndTransitionFeatures::test_feature_30_players_and_dispatches_schema_exists` |
| **AC-4** (line 100) | `test_tier1_features.py::TestTier1Features::test_feature_33_transition_rule_matrix_populated` | `tests/e2e/test_tier1_features.py::TestDispatchSchemaAndTransitionFeatures::test_feature_33_transition_rule_matrix_populated` |
| **AC-5** (line 101) | `test_tier1_features.py::TestTier1Features::test_feature_34_objective_bounds_policy_validation` | `tests/e2e/test_tier1_features.py::TestObjectivesAndGatesFeatures::test_feature_34_objective_bounds_policy_validation` |
| **AC-5** (line 101) | `test_tier2_boundaries.py::TestTier2Boundaries::test_tier2_non_numeric_player_id_rejected` | `tests/e2e/test_tier2_boundaries.py::TestObjectiveBoundsBoundaries::test_tier2_non_numeric_player_id_rejected` |
| **AC-9** (line 105) | `test_tier3_combinations.py::TestTier3Combinations::test_tier3_identical_decision_replay_vs_conflicting_decision` | `tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_identical_decision_replay_vs_conflicting_decision` |
| **AC-18** (line 114) | `test_tier3_combinations.py::TestTier3Combinations::test_tier3_identical_decision_replay_vs_conflicting_decision` | `tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_identical_decision_replay_vs_conflicting_decision` |
| **AC-38** (line 134) | `test_tier1_features.py::TestTier1Features::test_feature_32_single_write_path_transition_function_exists` | `tests/e2e/test_tier1_features.py::TestDispatchSchemaAndTransitionFeatures::test_feature_32_single_write_path_transition_function_exists` |
| **AC-39** (line 135) | `tests/test_dispatch_worker_cannot_write_dispatches (via test_dispatch_schema.py)` | `tests/test_dispatch_schema.py::test_dispatch_worker_cannot_write_dispatches` |
| **AC-39** (line 135) | `tests/test_dispatch_worker_cannot_access_approval_tables (via test_dispatch_schema.py)` | `tests/test_dispatch_schema.py::test_dispatch_worker_cannot_access_approval_tables` |
| **AC-39** (line 135) | `test_tier2_boundaries.py::TestTier2Boundaries::test_tier2_runtime_role_cannot_create_table` | `tests/e2e/test_tier2_boundaries.py::TestRolePrivilegeAndDdlBoundaries::test_tier2_runtime_role_cannot_create_table` |
| **AC-39** (line 135) | `test_tier2_boundaries.py::TestTier2Boundaries::test_tier2_runtime_role_cannot_alter_table` | `tests/e2e/test_tier2_boundaries.py::TestRolePrivilegeAndDdlBoundaries::test_tier2_runtime_role_cannot_alter_table` |
| **AC-39** (line 135) | `test_tier2_boundaries.py::TestTier2Boundaries::test_tier2_runtime_role_cannot_drop_table` | `tests/e2e/test_tier2_boundaries.py::TestRolePrivilegeAndDdlBoundaries::test_tier2_runtime_role_cannot_drop_table` |
| **AC-39** (line 135) | `test_tier2_boundaries.py::TestTier2Boundaries::test_tier2_runtime_role_cannot_create_function` | `tests/e2e/test_tier2_boundaries.py::TestRolePrivilegeAndDdlBoundaries::test_tier2_runtime_role_cannot_create_function` |
| **AC-39** (line 135) | `test_tier2_boundaries.py::TestTier2Boundaries::test_tier2_runtime_role_cannot_escalate_to_owner` | `tests/e2e/test_tier2_boundaries.py::TestRolePrivilegeAndDdlBoundaries::test_tier2_runtime_role_cannot_escalate_to_owner` |
| **AC-42** (line 138) | `test_tier1_features.py::TestTier1Features::test_feature_05_event_source_guard_missing_source_rejected` | `tests/e2e/test_tier1_features.py::TestDatabaseInfrastructureFeatures::test_feature_05_event_source_guard_missing_source_rejected` |
| **AC-43** (line 139) | `test_tier1_features.py::TestTier1Features::test_feature_11_approval_expiry_daemon_method_exists` | `tests/e2e/test_tier1_features.py::TestApprovalLifecycleFeatures::test_feature_11_approval_expiry_daemon_method_exists` |
| **AC-43** (line 139) | `test_tier2_boundaries.py::TestTier2Boundaries::test_tier2_expired_nonce_presentation_fails` | `tests/e2e/test_tier2_boundaries.py::TestCryptoAndNonceBoundaries::test_tier2_expired_nonce_presentation_fails` |
| **AC-44** (line 140) | `test_tier1_features.py::TestTier1Features::test_feature_20_terminal_states_enforcement_format` | `tests/e2e/test_tier1_features.py::TestGraphCoreAndApiFeatures::test_feature_20_terminal_states_enforcement_format` |
| **AC-44** (line 140) | `test_tier4_scenarios.py::TestTier4Scenarios::test_tier4_scenario_full_approval_lifecycle_no_dispatch` | `tests/e2e/test_tier4_scenarios.py::TestRealWorldLifecycleScenarios::test_tier4_scenario_full_approval_lifecycle_no_dispatch` |
| **AC-45** (line 141) | `test_tier1_features.py::TestTier1Features::test_feature_35_activation_gate_evaluation_fails_closed` | `tests/e2e/test_tier1_features.py::TestObjectivesAndGatesFeatures::test_feature_35_activation_gate_evaluation_fails_closed` |
| **AC-46** (line 142) | `test_tier2_boundaries.py::TestTier2Boundaries::test_tier2_non_numeric_player_id_rejected` | `tests/e2e/test_tier2_boundaries.py::TestObjectiveBoundsBoundaries::test_tier2_non_numeric_player_id_rejected` |
| **AC-49** (line 145) | `test_tier1_features.py::TestTier1Features::test_feature_28_cli_hmac_kill_switch_support` | `tests/e2e/test_tier1_features.py::TestGraphCoreAndApiFeatures::test_feature_28_cli_hmac_kill_switch_support` |
| **AC-52** (line 148) | `test_tier1_features.py::TestTier1Features::test_feature_32_single_write_path_transition_function_exists` | `tests/e2e/test_tier1_features.py::TestDispatchSchemaAndTransitionFeatures::test_feature_32_single_write_path_transition_function_exists` |
| **AC-53** (line 149) | `test_tier1_features.py::TestTier1Features::test_feature_33_transition_rule_matrix_populated` | `tests/e2e/test_tier1_features.py::TestDispatchSchemaAndTransitionFeatures::test_feature_33_transition_rule_matrix_populated` |
| **AC-55** (line 151) | `test_tier1_features.py::TestTier1Features::test_feature_09_approval_pair_consistency_trigger_exists` | `tests/e2e/test_tier1_features.py::TestApprovalLifecycleFeatures::test_feature_09_approval_pair_consistency_trigger_exists` |
| **AC-55** (line 151) | `test_tier1_features.py::TestTier1Features::test_feature_10_approval_inconsistencies_view_exists` | `tests/e2e/test_tier1_features.py::TestApprovalLifecycleFeatures::test_feature_10_approval_inconsistencies_view_exists` |
| **AC-56** (line 152) | `test_tier1_features.py::TestTier1Features::test_feature_01_role_bootstrap_roles_exist_and_configured` | `tests/e2e/test_tier1_features.py::TestDatabaseInfrastructureFeatures::test_feature_01_role_bootstrap_roles_exist_and_configured` |
| **AC-56** (line 152) | `test_tier1_features.py::TestTier1Features::test_feature_02_migration_runner_applied_version_order` | `tests/e2e/test_tier1_features.py::TestDatabaseInfrastructureFeatures::test_feature_02_migration_runner_applied_version_order` |
| **AC-57** (line 153) | `test_tier1_features.py::TestTier1Features::test_feature_27_health_probes_liveness_and_readiness` | `tests/e2e/test_tier1_features.py::TestGraphCoreAndApiFeatures::test_feature_27_health_probes_liveness_and_readiness` |
| **AC-58** (line 154) | `test_tier1_features.py::TestTier1Features::test_feature_40_scope_protection_phase_isolation` | `tests/e2e/test_tier1_features.py::TestTopologyAndInvariantsFeatures::test_feature_40_scope_protection_phase_isolation` |
| **C-1** (line 162) | `test_tier1_features.py::TestTier1Features::test_feature_35_activation_gate_evaluation_fails_closed` | `tests/e2e/test_tier1_features.py::TestObjectivesAndGatesFeatures::test_feature_35_activation_gate_evaluation_fails_closed` |
| **C-2** (line 163) | `test_tier1_features.py::TestTier1Features::test_feature_40_scope_protection_phase_isolation` | `tests/e2e/test_tier1_features.py::TestTopologyAndInvariantsFeatures::test_feature_40_scope_protection_phase_isolation` |
| **C-3** (line 164) | `test_tier1_features.py::TestTier1Features::test_feature_36_gate_challenge_and_record_routes_exist` | `tests/e2e/test_tier1_features.py::TestObjectivesAndGatesFeatures::test_feature_36_gate_challenge_and_record_routes_exist` |
| **C-4** (line 165) | `test_tier1_features.py::TestTier1Features::test_feature_13_nonce_subject_typing_support` | `tests/e2e/test_tier1_features.py::TestApprovalLifecycleFeatures::test_feature_13_nonce_subject_typing_support` |
| **C-4** (line 165) | `test_tier1_features.py::TestTier1Features::test_feature_36_gate_challenge_and_record_routes_exist` | `tests/e2e/test_tier1_features.py::TestObjectivesAndGatesFeatures::test_feature_36_gate_challenge_and_record_routes_exist` |
| **C-5** (line 166) | `test_tier1_features.py::TestTier1Features::test_feature_21_correlation_middleware_propagation` | `tests/e2e/test_tier1_features.py::TestGraphCoreAndApiFeatures::test_feature_21_correlation_middleware_propagation` |
| **C-6** (line 167) | `test_tier1_features.py::TestTier1Features::test_feature_03_grants_policy_versioning` | `tests/e2e/test_tier1_features.py::TestDatabaseInfrastructureFeatures::test_feature_03_grants_policy_versioning` |
| **C-7** (line 168) | `test_tier1_features.py::TestTier1Features::test_feature_38_compose_topology_and_file_secrets` | `tests/e2e/test_tier1_features.py::TestTopologyAndInvariantsFeatures::test_feature_38_compose_topology_and_file_secrets` |
| **C-8** (line 169) | `test_tier1_features.py::TestTier1Features::test_feature_40_scope_protection_phase_isolation` | `tests/e2e/test_tier1_features.py::TestTopologyAndInvariantsFeatures::test_feature_40_scope_protection_phase_isolation` |
| **C-9** (line 170) | `test_tier1_features.py::TestTier1Features::test_feature_32_single_write_path_transition_function_exists` | `tests/e2e/test_tier1_features.py::TestDispatchSchemaAndTransitionFeatures::test_feature_32_single_write_path_transition_function_exists` |
| **C-10** (line 171) | `test_tier1_features.py::TestTier1Features::test_feature_06_transition_event_source_identity_restricted` | `tests/e2e/test_tier1_features.py::TestDatabaseInfrastructureFeatures::test_feature_06_transition_event_source_identity_restricted` |
| **Section 1A Authority** (line 179) | `test_tier1_features.py::TestTier1Features::test_feature_16_human_authority_schema_compatibility` | `tests/e2e/test_tier1_features.py::TestApprovalLifecycleFeatures::test_feature_16_human_authority_schema_compatibility` |
| **Section 1A Authority** (line 179) | `test_tier4_scenarios.py::TestTier4Scenarios::test_section_1a_authority_separation_club_vs_player` | `tests/e2e/test_tier4_scenarios.py::TestSection1AHumanAuthorityAndConsent::test_section_1a_authority_separation_club_vs_player` |
| **Section 1A Actor** (line 180) | `test_tier1_features.py::TestTier1Features::test_feature_17_authority_basis_and_actor_separation` | `tests/e2e/test_tier1_features.py::TestApprovalLifecycleFeatures::test_feature_17_authority_basis_and_actor_separation` |
| **Section 1A Delegation** (line 181) | `test_tier4_scenarios.py::TestTier4Scenarios::test_section_1a_delegation_scope_and_expiry_validation` | `tests/e2e/test_tier4_scenarios.py::TestSection1AHumanAuthorityAndConsent::test_section_1a_delegation_scope_and_expiry_validation` |
| **Section 1A Fallback** (line 182) | `test_tier4_scenarios.py::TestTier4Scenarios::test_section_1a_player_fallback_prohibits_multi_player_packages` | `tests/e2e/test_tier4_scenarios.py::TestSection1AHumanAuthorityAndConsent::test_section_1a_player_fallback_prohibits_multi_player_packages` |
| **Section 1A Inactivity** (line 183) | `test_tier4_scenarios.py::TestTier4Scenarios::test_section_1a_inactivity_and_timeout_never_creates_approval` | `tests/e2e/test_tier4_scenarios.py::TestSection1AHumanAuthorityAndConsent::test_section_1a_inactivity_and_timeout_never_creates_approval` |
| **Section 1A Late Dec.** (line 185) | `test_tier4_scenarios.py::TestTier4Scenarios::test_section_1a_late_decision_cannot_overwrite_prior_decision` | `tests/e2e/test_tier4_scenarios.py::TestSection1AHumanAuthorityAndConsent::test_section_1a_late_decision_cannot_overwrite_prior_decision` |
| **Section 1A Actor Neutral** (line 186) | `test_tier1_features.py::TestTier1Features::test_feature_29_actor_neutral_decision_validation` | `tests/e2e/test_tier1_features.py::TestGraphCoreAndApiFeatures::test_feature_29_actor_neutral_decision_validation` |
| **Section 1A Actor Neutral** (line 186) | `test_tier4_scenarios.py::TestTier4Scenarios::test_section_1a_player_agent_cannot_be_decision_actor` | `tests/e2e/test_tier4_scenarios.py::TestSection1AHumanAuthorityAndConsent::test_section_1a_player_agent_cannot_be_decision_actor` |

#### Section 7 (Feature Inventory) Class Organization
Update Section 7 to cite each feature with its authentic test class:
- **Features 1–7**: `tests/e2e/test_tier1_features.py::TestDatabaseInfrastructureFeatures::<test_method>`
- **Features 8–17**: `tests/e2e/test_tier1_features.py::TestApprovalLifecycleFeatures::<test_method>`
- **Features 18–29**: `tests/e2e/test_tier1_features.py::TestGraphCoreAndApiFeatures::<test_method>`
- **Features 30–33**: `tests/e2e/test_tier1_features.py::TestDispatchSchemaAndTransitionFeatures::<test_method>`
- **Features 34–37**: `tests/e2e/test_tier1_features.py::TestObjectivesAndGatesFeatures::<test_method>`
- **Features 38–41**: `tests/e2e/test_tier1_features.py::TestTopologyAndInvariantsFeatures::<test_method>`

---

## 5. Remediation for `SECURITY_CHANGES.md` (Sections 1–11)

### Defect Analysis
Multiple entries in the `Evidence` column of `SECURITY_CHANGES.md` cite nonexistent test files (`test_activation_gates.py`, `test_correlation.py`) or nonexistent test methods.

### Exact Realignment Mapping for `SECURITY_CHANGES.md`

| Section | Row | Currently Hallucinated Citation | Authentic Replacement Citation | Rationale |
|---|---|---|---|---|
| **1.3** | 16 | `test_db_roles.py::test_runtime_posture_clean` | `test_db_roles.py::test_check_runtime_posture` | Real function name in `tests/test_db_roles.py:63` |
| **1.3** | 16 | `e2e/test_tier1_features.py::test_feature_01_public_schema_create_revoked` | `tests/e2e/test_tier1_features.py::TestDatabaseInfrastructureFeatures::test_feature_01_public_schema_create_revoked` | Canonical class path |
| **1.4** | 17 | `test_db_migrate_roles.py::test_grants_policy_version_check` | `tests/test_db_migrate_roles.py::test_grants_policy_constant_matches_file`<br>`tests/test_db_migrate_roles.py::test_grants_policy_recorded` | Real function names in `test_db_migrate_roles.py:132, 148` |
| **1.4** | 17 | `e2e/test_tier1_features.py::test_feature_03_grants_policy_versioning` | `tests/e2e/test_tier1_features.py::TestDatabaseInfrastructureFeatures::test_feature_03_grants_policy_versioning` | Canonical class path |
| **2.4** | 28 | `e2e/test_tier2_boundaries.py::test_phase1_delivery_transition_blocked` | `tests/test_dispatch_transition.py::test_phase1_delivered_edge_blocked`<br>`tests/e2e/test_tier2_boundaries.py::TestStateAndLifecycleBoundaries::test_tier2_illegal_graph_threads_status_rejected_by_check` | Function does not exist in `test_tier2_boundaries.py` |
| **3.1** | 36 | `test_approval_lifecycle.py::test_approval_request_ttl_clamping` | `tests/test_approval_lifecycle.py::test_expired_request_blocks_prior_nonce`<br>`tests/test_approval_lifecycle.py::test_expired_request_blocks_new_challenge` | Real function names in `test_approval_lifecycle.py:151, 182` |
| **3.2** | 37 | `test_approval_lifecycle.py::test_approval_request_expiry_daemon` | `tests/test_approval_lifecycle.py::test_expire_due_is_idempotent`<br>`tests/test_approval_lifecycle.py::test_expiry_and_decision_race_has_single_outcome` | Real function names in `test_approval_lifecycle.py:200, 218` |
| **3.2** | 37 | `e2e/test_tier3_combinations.py::test_tier3_decision_vs_expiry_race_condition` | `tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_decision_vs_expiry_race_condition` | Canonical class path |
| **4.1** | 46 | `test_m1_adversarial_challenger.py::test_agent_cannot_decide` | `tests/test_approval_lifecycle.py::test_player_agent_cannot_be_decision_actor` | Real function name in `test_approval_lifecycle.py` |
| **4.2** | 47 | `test_approval_lifecycle.py::test_timeout_never_creates_approval` | `tests/test_approval_lifecycle.py::test_timeout_inactivity_cannot_approve` | Real function name in `test_approval_lifecycle.py:370` |
| **4.2** | 47 | `e2e/test_tier4_scenarios.py::test_section_1a_inactivity_and_timeout_never_creates_approval` | `tests/e2e/test_tier4_scenarios.py::TestSection1AHumanAuthorityAndConsent::test_section_1a_inactivity_and_timeout_never_creates_approval` | Canonical class path |
| **4.3** | 48 | `e2e/test_tier4_scenarios.py::test_section_1a_authority_separation_club_vs_player` | `tests/test_approval_lifecycle.py::test_unsupported_actor_types_fail_closed_in_phase1`<br>`tests/e2e/test_tier4_scenarios.py::TestSection1AHumanAuthorityAndConsent::test_section_1a_authority_separation_club_vs_player` | Canonical class path |
| **5.1** | 57 | `test_graph_api_v1.py::test_hmac_kill_switch` | `tests/test_graph_api_v1.py::test_hmac_kill_switch_behavior`<br>`tests/test_graph_api_v1.py::test_adversarial_hmac_kill_switch_blocks_before_body_parsing` | Real function names in `test_graph_api_v1.py:323, 547` |
| **5.1** | 57 | `e2e/test_tier1_features.py::test_feature_28_hmac_kill_switch` | `tests/e2e/test_tier1_features.py::TestGraphCoreAndApiFeatures::test_feature_28_cli_hmac_kill_switch_support` | Real function name |
| **6.1** | 66 | `e2e/test_tier1_features.py::test_feature_07_append_only_table_guards` | `tests/e2e/test_tier1_features.py::TestDatabaseInfrastructureFeatures::test_feature_07_append_only_table_guards_reject_mutations` | Real function name |
| **6.2** | 67 | `test_approval_lifecycle.py::test_terminal_thread_cannot_be_modified`<br>`test_approval_lifecycle.py::test_terminal_approval_request_frozen` | `tests/test_migration_0003.py::test_package_version_immutable_while_awaiting`<br>`tests/test_dispatch_schema.py::test_dispatches_terminal_rows_frozen`<br>`tests/e2e/test_tier2_boundaries.py::TestStateAndLifecycleBoundaries::test_tier2_direct_update_of_package_version_blocked_by_trigger` | Real proving tests for terminal record freezing |
| **7.1** | 75 | `test_approval_lifecycle.py::test_package_version_immutable` | `tests/test_migration_0003.py::test_package_version_immutable_while_awaiting`<br>`tests/test_approval_security.py::test_package_version_immutable_once_awaiting_approval` | Real proving tests for package version immutability |
| **7.1** | 75 | `e2e/test_tier1_features.py::test_feature_08_approval_requests_lifecycle` | `tests/e2e/test_tier1_features.py::TestApprovalLifecycleFeatures::test_feature_08_approval_requests_table_schema` | Real function name |
| **7.2** | 76 | `test_approval_lifecycle.py::test_stale_package_version_rejected` | `tests/test_graph_persistence.py::test_decision_package_version_mismatch_fails_closed`<br>`tests/e2e/test_tier2_boundaries.py::TestStateAndLifecycleBoundaries::test_tier2_stale_package_version_decision_fails` | Real proving tests for stale package rejection |
| **7.2** | 76 | `e2e/test_tier4_scenarios.py::test_tier4_scenario_revision_request_preserves_package_immutability` | `tests/e2e/test_tier4_scenarios.py::TestRealWorldLifecycleScenarios::test_tier4_scenario_revision_request_preserves_package_immutability` | Canonical class path |
| **8.1** | 84 | `test_langgraph_flow.py::test_single_interrupt_per_lifecycle` | `tests/test_graph_persistence.py::test_single_approval_interrupt_at_approval_node`<br>`tests/test_langgraph_flow.py::test_graph_runs_to_interrupt_with_typed_state` | Real proving tests in flow and persistence |
| **8.1** | 84 | `e2e/test_tier1_features.py::test_feature_18_single_approval_interrupt` | `tests/e2e/test_tier1_features.py::TestGraphCoreAndApiFeatures::test_feature_18_single_approval_interrupt_verified` | Real function name |
| **8.2** | 85 | `test_langgraph_flow.py::test_terminal_state_text_approved_not_dispatched` | `tests/test_graph_persistence.py::test_terminal_graph_states_formatting`<br>`tests/test_langgraph_flow.py::test_existing_langgraph_thread_resumes`<br>`tests/e2e/test_tier1_features.py::TestGraphCoreAndApiFeatures::test_feature_20_terminal_states_enforcement_format` | Real proving tests for terminal states |
| **8.2** | 85 | `test_graph_api_v1.py::test_resume_cannot_authorize` | `tests/test_graph_api_v1.py::test_v1_resume_refuses_authorization_payload` | Real function name in `test_graph_api_v1.py:200` |
| **9.1** | 94 | `test_activation_gates.py::test_evaluate_gates_order`<br>`test_activation_gates.py::test_g_outbox_refused_in_phase1` | `tests/test_dispatch_creation.py::test_gates_block_by_default`<br>`tests/test_dispatch_creation.py::test_g_outbox_not_recordable_in_phase1`<br>`tests/e2e/test_tier1_features.py::TestObjectivesAndGatesFeatures::test_feature_35_activation_gate_evaluation_fails_closed` | `test_activation_gates.py` does not exist; logic is in `test_dispatch_creation.py` |
| **9.2** | 95 | `test_activation_gates.py::test_canonical_gate_bytes_rfc8785`<br>`test_graph_api_v1.py::test_gate_challenge_and_record_flow` | `tests/test_dispatch_creation.py::test_gate_recording_requires_commissioner_and_signature`<br>`tests/test_dispatch_creation.py::test_gate_tampered_field_rejected`<br>`tests/test_dispatch_creation.py::test_gate_requires_deployment_identity`<br>`tests/e2e/test_tier1_features.py::TestObjectivesAndGatesFeatures::test_feature_36_gate_challenge_and_record_routes_exist` | Real proving tests for RFC-8785 and gate challenges |
| **9.3** | 96 | `test_graph_api_v1.py::test_dynamic_gate_reevaluation` | `tests/test_dispatch_creation.py::test_gate_change_applies_on_next_evaluation` | Real function name in `test_dispatch_creation.py:258` |
| **10.1** | 104 | `test_events_outbox.py::test_domain_events_source_check` | `tests/test_events_outbox.py::test_sourced_event_requires_existing_source` | Real function name in `test_events_outbox.py:82` |
| **10.1** | 104 | `e2e/test_tier1_features.py::test_feature_05_event_source_guard` | `tests/e2e/test_tier1_features.py::TestDatabaseInfrastructureFeatures::test_feature_05_event_source_guard_missing_source_rejected` | Real function name |
| **10.2** | 105 | `test_dispatch_transition.py::test_worker_actor_derivation` | `tests/test_dispatch_transition.py::test_worker_actor_derivation`<br>`tests/test_events_outbox.py::test_app_cannot_forge_dispatch_events` | Real function names |
| **10.2** | 105 | `e2e/test_tier1_features.py::test_feature_06_transition_event_source_identity` | `tests/e2e/test_tier1_features.py::TestDatabaseInfrastructureFeatures::test_feature_06_transition_event_source_identity_restricted` | Real function name |
| **10.3** | 106 | `test_correlation.py::test_valid_correlation_id_propagated`<br>`test_correlation.py::test_malformed_correlation_id_returns_422` | `tests/test_graph_api_v1.py::test_correlation_id_generated_when_absent`<br>`tests/test_graph_api_v1.py::test_correlation_id_echoed_when_present`<br>`tests/test_graph_api_v1.py::test_correlation_id_propagated_to_logs`<br>`tests/test_graph_api_v1.py::test_malformed_correlation_id_rejected`<br>`tests/test_graph_api_v1.py::test_adversarial_malformed_correlation_id_rejected_and_redacted`<br>`tests/test_dispatch_creation.py::test_correlation_id_shared_by_dispatch_and_events`<br>`tests/e2e/test_tier1_features.py::TestGraphCoreAndApiFeatures::test_feature_21_correlation_middleware_propagation` | `test_correlation.py` does not exist; logic is in `test_graph_api_v1.py` & `test_dispatch_creation.py` |
| **10.4** | 107 | `test_graph_api_v1.py::test_validation_error_strips_input` | `tests/test_graph_api_v1.py::test_adversarial_pydantic_validation_strips_sensitive_inputs` | Real function name in `test_graph_api_v1.py:474` |
| **11.1** | 115 | `test_compose_isolation.py::test_only_graph_api_receives_approval_material` | `tests/test_compose_isolation.py::test_signing_secret_only_in_graph_api`<br>`tests/test_compose_isolation.py::test_db_secret_mounts_per_service` | Real function names in `test_compose_isolation.py:99, 122` |
| **11.3** | 117 | `test_graph_api_v1.py::test_run_projection_omits_secrets` | `tests/test_graph_api_v1.py::test_v1_get_run_safe_projection_omits_secrets`<br>`tests/test_graph_api_v1.py::test_adversarial_run_projection_scrubs_internal_secrets` | Real function names in `test_graph_api_v1.py:162, 499` |

---

## 6. Implementation Readiness & Worker Action Plan

The worker (`worker_m6_1` or subsequent remediation worker) can implement this entire remediation deterministically using standard file edits:
1. Edit `tests/e2e/test_tier1_features.py`:
   - Replace lines 424–428 (`test_feature_28_cli_hmac_kill_switch_support`) with Section 1.1 replacement code.
   - Replace lines 517–524 (`test_feature_37_atomic_approval_to_dispatch_creation`) with Section 1.2 replacement code.
2. Edit `tests/e2e/test_tier2_boundaries.py`:
   - Update line 50 (`test_tier2_empty_run_request_body_rejected`) with Section 2.1 replacement code.
   - Replace lines 270–302 (`TestObjectiveBoundsBoundaries`) with Section 2.2 replacement code.
3. Edit `tests/e2e/test_tier4_scenarios.py`:
   - Replace lines 181–194 (`test_section_1a_authority_separation_club_vs_player`) with Section 3.1 replacement code.
4. Edit `TEST_RESULTS.md`:
   - Update Section 2.2 test count from 80 to 81 (Tier 1: 42, Tier 2: 25, Tier 3: 5, Tier 4: 9) and clarify verification provenance.
   - Replace hallucinated class names across Sections 4, 5, 6, and 7 per Section 4.2 mapping table.
   - Fix AC-39 node IDs to canonical format (`tests/test_dispatch_schema.py::test_dispatch_worker_cannot_write_dispatches`).
5. Edit `SECURITY_CHANGES.md`:
   - Replace all invalid test citations across Sections 1–11 per Section 5 mapping table.
