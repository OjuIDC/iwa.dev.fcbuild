# Challenge Report: Tier 5 Adversarial Coverage Hardening & Trace Integrity

**Agent ID**: `challenger_m6_1`  
**Archetype**: `teamwork_preview_challenger`  
**Roles**: critic, specialist  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m6_1`  
**Date**: 2026-10-03  
**Verdict**: **REQUEST_CHANGES**  

---

## 1. Challenge Summary

**Overall Risk Assessment**: **HIGH / CRITICAL**

While the core implementation in `src/` and the underlying unit/integration test suites (`tests/test_*.py`) exhibit strong architectural integrity, our adversarial inspection revealed three critical integrity flaws in `TEST_RESULTS.md` and `tests/e2e/`:
1. **Pervasive Test Node ID Hallucination**: Every E2E test node cited across Sections 4, 5, 6, and 7 of `TEST_RESULTS.md` uses non-existent class names (`TestTier1Features`, `TestTier2Boundaries`, `TestTier3Combinations`, `TestTier4Scenarios`). Not a single cited E2E test node ID can be executed directly by `pytest` as written.
2. **Dummy and Tautological Assertions in E2E Suite**:
   - `tests/e2e/test_tier1_features.py`: `test_feature_37_atomic_approval_to_dispatch_creation` (line 523) contains an explicit `assert True` dummy assertion.
   - `tests/e2e/test_tier2_boundaries.py`: `TestObjectiveBoundsBoundaries` (lines 273–302) contains 5 tests that assert basic dictionary field comparisons without invoking `Objective` Pydantic validation, `validate_metric_bounds()`, or candidate verification.
   - `tests/e2e/test_tier4_scenarios.py`: `test_section_1a_authority_separation_club_vs_player` (line 193) contains `assert cur.fetchone()[0] >= 0` on a SQL `count(*)`, which is tautologically true for any table state.
3. **E2E Test Inventory Discrepancy**: `TEST_RESULTS.md` and worker reports claim 80 E2E tests, whereas `tests/e2e/` contains 81 test methods (Tier 1 has 42 tests, Tier 2 has 25, Tier 3 has 5, Tier 4 has 9).

---

## 2. Adversarial Challenges

### Challenge 1 (Critical): Test Node Authenticity Failure in `TEST_RESULTS.md`

- **Assumption Challenged**: All test node IDs cited in `TEST_RESULTS.md` represent authentic, runnable pytest nodes.
- **Attack / Verification**:
  Attempted to resolve the cited pytest node IDs:
  - `tests/e2e/test_tier1_features.py::TestTier1Features::test_feature_37_atomic_approval_to_dispatch_creation`
  - `tests/e2e/test_tier2_boundaries.py::TestTier2Boundaries::test_tier2_non_numeric_player_id_rejected`
  - `tests/e2e/test_tier3_combinations.py::TestTier3Combinations::test_tier3_identical_decision_replay_vs_conflicting_decision`
  - `tests/e2e/test_tier4_scenarios.py::TestTier4Scenarios::test_tier4_scenario_full_approval_lifecycle_no_dispatch`
- **Blast Radius**:
  Running `pytest` on any of these node IDs results in an immediate `ERROR: not found`. Anyone auditing the acceptance trace by running cited test nodes will experience 100% test selection failure for all E2E citations.
- **Root Cause**:
  `explorer_m6_trace_1` fabricated class names (`TestTier1Features`, etc.) instead of reading the actual class declarations in `tests/e2e/`. `worker_m6_1` imported this draft into `TEST_RESULTS.md` without verifying node authenticity against the codebase.
- **Actual vs Cited Class Structure**:
  | File | Cited Class Name in `TEST_RESULTS.md` | Actual Class Names in Code |
  |---|---|---|
  | `test_tier1_features.py` | `TestTier1Features` (DOES NOT EXIST) | `TestDatabaseInfrastructureFeatures`<br>`TestApprovalLifecycleFeatures`<br>`TestGraphCoreAndApiFeatures`<br>`TestDispatchSchemaAndTransitionFeatures`<br>`TestObjectivesAndGatesFeatures`<br>`TestTopologyAndInvariantsFeatures` |
  | `test_tier2_boundaries.py` | `TestTier2Boundaries` (DOES NOT EXIST) | `TestInputPayloadBoundaries`<br>`TestCryptoAndNonceBoundaries`<br>`TestStateAndLifecycleBoundaries`<br>`TestRolePrivilegeAndDdlBoundaries`<br>`TestObjectiveBoundsBoundaries` |
  | `test_tier3_combinations.py` | `TestTier3Combinations` (DOES NOT EXIST) | `TestCrossFeatureCombinations` |
  | `test_tier4_scenarios.py` | `TestTier4Scenarios` (DOES NOT EXIST) | `TestRealWorldLifecycleScenarios`<br>`TestSection1AHumanAuthorityAndConsent` |
  Additionally, line 135 cites `tests/test_dispatch_worker_cannot_write_dispatches (via test_dispatch_schema.py)` instead of the canonical `tests/test_dispatch_schema.py::test_dispatch_worker_cannot_write_dispatches`.

---

### Challenge 2 (Critical): Dummy Assertions & Anti-Triviality Failures in `tests/e2e/`

- **Assumption Challenged**: All tests in `tests/e2e/` perform deep, non-trivial assertions validating production logic, rejection codes, and database state.
- **Attack / Verification**:
  1. **Literal `assert True` in Tier 1**:
     In `tests/e2e/test_tier1_features.py`, lines 517–524:
     ```python
     def test_feature_37_atomic_approval_to_dispatch_creation(self, pg_uri):
         """Feature 37: Pending dispatch row creation is atomic with approval when dispatch_effective is true."""
         with psycopg.connect(pg_uri) as conn:
             if not table_exists(conn, "dispatches"):
                 pytest.skip("Milestone M4 required: dispatches table not yet migrated")
             # In Phase 1, dispatch creation remains disabled
             assert True
     ```
     This test passes unconditionally with `assert True` without verifying atomicity, rollback, or pending dispatch row creation.
  2. **Tautological Assertions in Tier 2 Domain 5**:
     In `tests/e2e/test_tier2_boundaries.py`, lines 273–302 (`TestObjectiveBoundsBoundaries`):
     - `test_tier2_pass_accuracy_delta_above_bound_rejected`: Calls `cand = make_objective_candidate(metric="pass_accuracy", delta=26.0)` and asserts `assert cand["objective"]["target_delta"] > 25.0`.
     - `test_tier2_negative_target_delta_rejected`: Asserts `assert cand["objective"]["target_delta"] < 0`.
     - `test_tier2_observation_window_max_matches_above_bound`: Asserts `assert cand["objective"]["max_matches"] > 10`.
     - `test_tier2_observation_window_min_greater_than_max`: Asserts `assert cand["objective"]["min_matches"] > cand["objective"]["max_matches"]`.
     - `test_tier2_non_numeric_player_id_rejected`: Asserts `assert not cand["player_id"].isdigit()`.
     *None* of these 5 tests pass the candidate or objective to `Objective(**cand["objective"])`, `validate_metric_bounds()`, or `create_dispatch_for_approval()`. They assert properties of the local dictionary fixture only!
  3. **Tautological `count(*) >= 0` in Tier 4**:
     In `tests/e2e/test_tier4_scenarios.py`, lines 181–194:
     ```python
     cur.execute("SELECT count(*) FROM public.authority_policies WHERE policy_scope IN ('club_wide', 'player_specific')")
     assert cur.fetchone()[0] >= 0
     ```
     A SQL count is mathematical always `>= 0`, meaning this test will pass even if the table is completely empty or lacks required policies.
  4. **Permissive Kill-Switch Assertion in Tier 1**:
     In `tests/e2e/test_tier1_features.py`, line 427:
     ```python
     assert getattr(cfg, "approval_cli_hmac_enabled", True) is False or not hasattr(cfg, "approval_cli_hmac_enabled")
     ```
     If the setting does not exist on `cfg`, the test passes vacuously.
  5. **Missing Assertion on Run Creation in Tier 2**:
     In `tests/e2e/test_tier2_boundaries.py`, line 50:
     `r = client.post("/run", headers=SVC_HEADERS, json={})` is executed but its status code is never asserted (only `r_raw = client.post(..., content=b"")` is checked).
- **Blast Radius**:
  The E2E test suite creates a false sense of verification for Feature 37 and Domain 5 boundaries, masking potential boundary regressions in objective validation and dispatch creation.
- **Mitigation**:
  - Update `test_feature_37_atomic_approval_to_dispatch_creation` to execute an actual atomic approval with synthetic gates (mirroring `tests/test_dispatch_creation.py::test_approve_creates_dispatch_atomically`) or assert that no dispatch row is created under `DISPATCH_ENABLED=false`.
  - Update `TestObjectiveBoundsBoundaries` to instantiate `Objective(**cand["objective"])` inside `pytest.raises(ValidationError)` and `create_dispatch_for_approval` inside `pytest.raises(NotDispatchable)`.
  - Update `test_section_1a_authority_separation_club_vs_player` to assert `count > 0` or check specific policy scope records.

---

### Challenge 3 (High): Test Suite Count Mismatch in Traceability Documentation

- **Assumption Challenged**: The E2E test suite comprises exactly 80 tests as documented in `TEST_RESULTS.md:45`.
- **Attack / Verification**:
  Direct AST count of all `def test_*` functions in `tests/e2e/`:
  - `tests/e2e/test_tier1_features.py`: 42 tests (Feature 1 has two tests: `test_feature_01_role_bootstrap_roles_exist_and_configured` and `test_feature_01_public_schema_create_revoked`)
  - `tests/e2e/test_tier2_boundaries.py`: 25 tests
  - `tests/e2e/test_tier3_combinations.py`: 5 tests
  - `tests/e2e/test_tier4_scenarios.py`: 9 tests
  - **Total**: 42 + 25 + 5 + 9 = **81 tests**.
- **Blast Radius**:
  The official acceptance trace misreports the test inventory by -1 test, indicating that the count was derived theoretically (41 features + 25 + 5 + 9 = 80) rather than via empirical test execution.
- **Mitigation**: Update `TEST_RESULTS.md` to state 81 E2E tests across Tiers 1–4.

---

### Challenge 4 (High): Owner Success Conditions #1 and #5 Compromised

- **Condition 1**: "The full existing and new test suite passes"
  - *Assessment*: While tests pass with 0 exit code, the presence of `assert True` in Tier 1 and 5 tautological dictionary checks in Tier 2 violates the standard of genuine verification required by the Owner brief.
- **Condition 5**: "Approval-to-dispatch atomicity is proven in tests with synthetic gates"
  - *Assessment*: Although thoroughly proven in `tests/test_dispatch_creation.py`, the corresponding E2E Tier 1 test (`test_feature_37_atomic_approval_to_dispatch_creation`) was stubbed with `assert True`, meaning E2E coverage for Condition 5 is incomplete.

---

## 3. Stress Test Results Matrix

| Area / Target | Hypothesis | Expected Behavior | Actual Behavior | Result |
|---|---|---|---|:---:|
| Node ID Authenticity | Nodes cited in `TEST_RESULTS.md` exist | Pytest discovers all cited node IDs | 100% of E2E nodes fail due to wrong class names | **FAIL** |
| Assertion Depth: Feature 37 | Feature 37 E2E test tests atomic dispatch | Validates dispatch row & rollback | `assert True` on line 523 | **FAIL** |
| Assertion Depth: Objective Bounds | Tier 2 Domain 5 tests reject invalid objectives | Invokes schema/validator | Asserts local dict values (`cand["..."] > 25`) | **FAIL** |
| Assertion Depth: Section 1A | Tier 4 tests verify policy separation | Asserts distinct policies exist | `assert count >= 0` (always True) | **FAIL** |
| Test Inventory Consistency | `TEST_RESULTS.md` reports true E2E count | Reports 81 tests | Reports 80 tests | **FAIL** |
| Production Immutability | No changes to `src/` or `migrations/` | 0 modifications | 0 modifications | **PASS** |
| Security Invariants | `DISPATCH_ENABLED=false`, least privilege | Strictly enforced | Strictly enforced in DB & config | **PASS** |

---

## 4. Required Actionable Remediation

To achieve **APPROVE** status, the implementation team must execute the following remediation:

1. **Fix Test Node IDs in `TEST_RESULTS.md`**:
   Replace all hallucinated class names with authentic class names across Sections 4, 5, 6, and 7:
   - Replace `test_tier1_features.py::TestTier1Features::` with the respective actual class:
     - `TestDatabaseInfrastructureFeatures` for Features 1–7
     - `TestApprovalLifecycleFeatures` for Features 8–17
     - `TestGraphCoreAndApiFeatures` for Features 18–29
     - `TestDispatchSchemaAndTransitionFeatures` for Features 30–33
     - `TestObjectivesAndGatesFeatures` for Features 34–37
     - `TestTopologyAndInvariantsFeatures` for Features 38–41
   - Replace `test_tier2_boundaries.py::TestTier2Boundaries::` with:
     - `TestInputPayloadBoundaries`
     - `TestCryptoAndNonceBoundaries`
     - `TestStateAndLifecycleBoundaries`
     - `TestRolePrivilegeAndDdlBoundaries`
     - `TestObjectiveBoundsBoundaries`
   - Replace `test_tier3_combinations.py::TestTier3Combinations::` with `TestCrossFeatureCombinations::`
   - Replace `test_tier4_scenarios.py::TestTier4Scenarios::` with:
     - `TestRealWorldLifecycleScenarios`
     - `TestSection1AHumanAuthorityAndConsent`
   - Replace `tests/test_dispatch_worker_cannot_write_dispatches (via test_dispatch_schema.py)` with `tests/test_dispatch_schema.py::test_dispatch_worker_cannot_write_dispatches`
   - Replace `tests/test_dispatch_worker_cannot_access_approval_tables (via test_dispatch_schema.py)` with `tests/test_dispatch_schema.py::test_dispatch_worker_cannot_access_approval_tables`
   - Update E2E test count in Section 2.2 from 80 to 81.

2. **Harden Assertions in `tests/e2e/test_tier1_features.py`**:
   - Replace `assert True` in `test_feature_37_atomic_approval_to_dispatch_creation` with genuine logic: verify that under `DISPATCH_ENABLED=false`, approval creates zero dispatch rows, or using synthetic gates verify atomic creation of the pending dispatch row.

3. **Harden Assertions in `tests/e2e/test_tier2_boundaries.py`**:
   - Update `TestObjectiveBoundsBoundaries`:
     - Test `Objective.model_validate(cand["objective"])` inside `with pytest.raises(ValueError):` for `delta > 25.0`, `delta < 0`, `max_matches > 10`, and `min_matches > max_matches`.
     - Test `create_dispatch_for_approval` inside `with pytest.raises(NotDispatchable):` for `player_id="batman_gamertag"`.

4. **Harden Assertion in `tests/e2e/test_tier4_scenarios.py`**:
   - In `test_section_1a_authority_separation_club_vs_player`, replace `assert count >= 0` with `assert count > 0` or verify distinct `policy_scope` values.

---

## 5. Explicit Verdict

**REQUEST_CHANGES**

Milestone 6 acceptance cannot proceed while the acceptance trace publishes non-existent test node IDs and the E2E test suite contains `assert True` and tautological assertions. Once the above 4 remediation items are applied, the trace will be 100% authentic, verifiable, and hardened.
