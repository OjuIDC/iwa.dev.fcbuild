# Handoff Report — Milestone 6 Iteration 2 (Challenger 2)

## 1. Observation

Direct observations from codebase inspection, AST analysis, grep searches, and cross-reference verification:

1. **`SECURITY_CHANGES.md`**:
   - Line 16 cites:
     `tests/test_graph_api_v1.py::test_health_ready_checks_db_posture`
     Direct observation via `grep_search`: Symbol `test_health_ready_checks_db_posture` does NOT exist in `tests/test_graph_api_v1.py` or anywhere in the repository.
   - Line 66 cites:
     `tests/test_dispatch_schema.py::test_append_only_triggers`
     Direct observation via `grep_search`: Symbol `test_append_only_triggers` does NOT exist in `tests/test_dispatch_schema.py`. The actual function defined at line 261 of `tests/test_dispatch_schema.py` is `test_tracking_tables_append_only`.
   - Line 86 cites:
     `tests/test_graph_persistence.py::test_restart_recovers_interrupted_graph`
     Direct observation via `grep_search`: Symbol `test_restart_recovers_interrupted_graph` does NOT exist in `tests/test_graph_persistence.py`. The actual function defined at line 209 of `tests/test_graph_persistence.py` is `test_checkpoint_recovery_across_server_restart`.
   - Line 86 cites:
     `tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_post_commit_resume_failure_recovery`
     Direct observation via `grep_search`: Symbol `test_tier3_post_commit_resume_failure_recovery` does NOT exist in `tests/e2e/test_tier3_combinations.py`. The actual method defined at line 79 of `tests/e2e/test_tier3_combinations.py` is `test_tier3_post_commit_resume_failure_preserves_authoritative_decision`.
   - Lines 1–11: Zero occurrences of deleted test files (`test_activation_gates.py`, `test_correlation.py`).
   - Lines 1–11: Zero occurrences of legacy monolithic classes (`TestTier1Features`, `TestTier2Boundaries`, `TestTier3Combinations`, `TestTier4Scenarios`).
   - Lines 5–6 contain the exact mandatory Phase 1 Completion Statement:
     `> **"Secure data foundation complete. Dispatch remains disabled. No player delivery has occurred."**`

2. **Phase 1 Boundary Enforcement**:
   - `docker-compose.yml` line 202: `DISPATCH_ENABLED: "false"`.
   - `src/graph_api.py` lines 109, 419, 427: Defaults to false; if set to true, `/health/ready` probe returns HTTP 503 degraded (`Phase 1 safety invariant violation: DISPATCH_ENABLED must be false`).
   - `migrations/0003_dispatch_foundation.sql` line 1443: Stored procedure `public.fc27_dispatch_transition` halts with SQLSTATE `FC002` if `p_to_status = 'delivered'`.
   - `tests/test_phase1_scope.py`: Confirmed 0 files matching `src/dashboard`, `src/identity`, forbidden dispatch prefixes (`worker`, `relay`, etc.), `webauthn` imports, or companion tracking writes.

3. **Section 1A Invariants**:
   - `src/approval_store.py` lines 379–380: Rejects any actor name containing `"agent"` with HTTP 403 `StoreError(403, "agents cannot be decision actors")`.
   - `src/approval_store.py` lines 623–715: `expire_due()` transitions awaiting requests to `'expired'`; never writes to `approval_audit`, never approves.
   - `migrations/0003_dispatch_foundation.sql` line 479: `CHECK (allow_player_fallback = false OR scope = 'player_specific')` prohibits `allow_player_fallback = true` on `scope = 'club'`. Tested in `tests/e2e/test_tier4_scenarios.py` lines 213–220.

4. **Database Least Privilege**:
   - `src/db_roles.py` line 48: `fc27_owner` created as `NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT`.
   - `src/db_roles.py` line 93: `REVOKE CREATE ON SCHEMA public FROM PUBLIC`.
   - `migrations/post/grants.sql` lines 84–95: `fc27_app` and `fc27_dispatch` have `SELECT` only on `dispatches`; `UPDATE` and `DELETE` strictly revoked.
   - `migrations/0003_dispatch_foundation.sql` lines 1358–1375: `public.fc27_dispatch_transition` has `SECURITY DEFINER`, search path set to `pg_catalog, pg_temp`, no dynamic SQL.

---

## 2. Logic Chain

1. **Test Citation Authenticity (Observation 1)**:
   - Forensic verification requires that every proving test cited in `SECURITY_CHANGES.md` exists and can be directly executed by `pytest <node_id>`.
   - In `SECURITY_CHANGES.md`, 83 of 87 citations correctly point to existing, executable test nodes.
   - However, 4 citations reference nonexistent function/method names (`test_health_ready_checks_db_posture`, `test_append_only_triggers`, `test_restart_recovers_interrupted_graph`, `test_tier3_post_commit_resume_failure_recovery`).
   - Consequently, an automated runner verifying citations fails on those 4 node IDs, requiring changes to `SECURITY_CHANGES.md`.

2. **Boundary Enforcement & Section 1A Invariants (Observations 2, 3)**:
   - Universal `DISPATCH_ENABLED=false` is enforced in Compose configuration, API startup/readiness probes, and database transition functions.
   - AST scans prove that no Phase 2+ code, WebAuthn dependencies, or tracking mutations exist in `src/`.
   - Section 1A constraints preventing non-human agent decisions, ensuring timeouts never approve, and prohibiting player fallback on club policies are enforced by store logic and database check constraints.

3. **Database Least Privilege (Observation 4)**:
   - Role separation between owner (NOLOGIN), migrator (LOGIN with owner membership), app (DML only), and dispatch (SELECT only on dispatches) ensures complete isolation. Single write path through `public.fc27_dispatch_transition` prevents unauthorized mutations.

---

## 3. Caveats

- In accordance with agent constraints ("Do NOT modify production source code or test files"), challenger agent did not modify `SECURITY_CHANGES.md` directly, but reported the exact required line edits.
- Live external network connections (EA API, live LLM inference, Langfuse servers) and Docker container runners were not executed live, consistent with documented Phase 1 exclusions.

---

## 4. Conclusion

- **Verdict**: **REQUEST_CHANGES**
- Architectural invariants, boundary enforcement, Section 1A logic, and database least privilege all **PASS**.
- `SECURITY_CHANGES.md` must be remediated to replace the 4 non-existent test node citations with their authentic counterparts:
  1. Line 16: Replace `tests/test_graph_api_v1.py::test_health_ready_checks_db_posture` with `tests/test_db_migrate_roles.py::test_graph_api_refuses_owner_member_or_create` (and/or `tests/e2e/test_tier1_features.py::TestGraphCoreAndApiFeatures::test_feature_27_health_probes_liveness_and_readiness`).
  2. Line 66: Replace `tests/test_dispatch_schema.py::test_append_only_triggers` with `tests/test_dispatch_schema.py::test_tracking_tables_append_only`.
  3. Line 86: Replace `tests/test_graph_persistence.py::test_restart_recovers_interrupted_graph` with `tests/test_graph_persistence.py::test_checkpoint_recovery_across_server_restart`.
  4. Line 86: Replace `tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_post_commit_resume_failure_recovery` with `tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_post_commit_resume_failure_preserves_authoritative_decision`.

---

## 5. Verification Method

To independently verify this finding:

1. **Verify Non-Existent Node IDs**:
   ```bash
   grep -n "test_health_ready_checks_db_posture" tests/test_graph_api_v1.py
   grep -n "test_append_only_triggers" tests/test_dispatch_schema.py
   grep -n "test_restart_recovers_interrupted_graph" tests/test_graph_persistence.py
   grep -n "test_tier3_post_commit_resume_failure_recovery" tests/e2e/test_tier3_combinations.py
   ```
   *Expected Result*: All 4 grep commands return empty (exit code 1).

2. **Verify Authentic Replacement Node IDs**:
   ```bash
   grep -n "test_graph_api_refuses_owner_member_or_create" tests/test_db_migrate_roles.py
   grep -n "test_tracking_tables_append_only" tests/test_dispatch_schema.py
   grep -n "test_checkpoint_recovery_across_server_restart" tests/test_graph_persistence.py
   grep -n "test_tier3_post_commit_resume_failure_preserves_authoritative_decision" tests/e2e/test_tier3_combinations.py
   ```
   *Expected Result*: All 4 grep commands match their respective definitions.
