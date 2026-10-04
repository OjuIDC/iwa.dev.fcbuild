# Tier 5 Adversarial Challenge Report: Security Changes and Boundary Invariants

**Agent ID**: challenger_m6_it2_2  
**Archetype**: teamwork_preview_challenger (roles: critic, specialist)  
**Target Milestone**: Milestone 6 Iteration 2 (Tier 5 Adversarial Coverage Hardening)  
**Verdict**: **REQUEST_CHANGES**  

---

## Challenge Summary

**Overall Risk Assessment**: **MEDIUM**  
While the architectural security controls, database least-privilege policies, Section 1A invariants, and Phase 1 boundary enforcements are robust, tamper-resistant, and correctly implemented in code and database DDL, empirical verification discovered that **4 test node IDs cited in `SECURITY_CHANGES.md` are fictitious or inaccurate** (referencing non-existent test functions). Forensic audits requiring 100% executable test citations cannot accept citations to non-existent node IDs.

---

## Challenges

### [High] Challenge 1: Fictitious and Inaccurate Proving Test Citations in `SECURITY_CHANGES.md`

- **Assumption Challenged**:  
  `worker_m6_2` claimed in `handoff.md` (lines 40–41) that:
  > *"In `SECURITY_CHANGES.md`, substituting all 24 citations with authentic test nodes located in `tests/test_dispatch_creation.py`, `tests/test_graph_api_v1.py`, `tests/test_migration_0003.py`, and the canonical E2E test classes ensures that forensic auditors find 100% executable evidence paths."*
  We challenged whether every cited node ID across Sections 1–11 physically exists in the repository.

- **Attack Scenario & Empirical Discovery**:  
  We conducted an exhaustive node-by-node AST and symbol verification across all 87 test node citations in Sections 1–11 of `SECURITY_CHANGES.md`. While zero references to deleted test files (`test_activation_gates.py`, `test_correlation.py`) or legacy monolithic classes (`TestTier1Features`, etc.) exist, **4 cited test node IDs do not exist anywhere in the codebase**:
  
  1. **Section 1.3 (Line 16)**:
     - Cited: `tests/test_graph_api_v1.py::test_health_ready_checks_db_posture`
     - Finding: Does NOT exist in `tests/test_graph_api_v1.py` or anywhere in the repository. (The actual readiness probes in `test_graph_api_v1.py` are `test_health_ready_fails_if_dispatch_enabled_true` and `test_adversarial_db_error_masking_in_health_ready`; database posture checks are in `tests/test_db_roles.py::test_check_runtime_posture`, `tests/test_db_migrate_roles.py::test_graph_api_refuses_owner_member_or_create`, and `tests/test_db_migrate_roles.py::test_graph_api_refuses_privileged_role`).
  
  2. **Section 6.1 (Line 66)**:
     - Cited: `tests/test_dispatch_schema.py::test_append_only_triggers`
     - Finding: Does NOT exist in `tests/test_dispatch_schema.py`. The actual test is `tests/test_dispatch_schema.py::test_tracking_tables_append_only` (line 261).
  
  3. **Section 8.3 (Line 86)**:
     - Cited: `tests/test_graph_persistence.py::test_restart_recovers_interrupted_graph`
     - Finding: Does NOT exist in `tests/test_graph_persistence.py`. The actual test is `tests/test_graph_persistence.py::test_checkpoint_recovery_across_server_restart` (line 209).
  
  4. **Section 8.3 (Line 86)**:
     - Cited: `tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_post_commit_resume_failure_recovery`
     - Finding: Does NOT exist in `tests/e2e/test_tier3_combinations.py`. The actual test is `tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_post_commit_resume_failure_preserves_authoritative_decision` (line 79).

- **Blast Radius**:  
  Forensic acceptance verification fails when executing cited pytest node IDs. Test runners invoking the cited IDs encounter `ERROR: not found` collection failures.

- **Mitigation**:  
  Update `SECURITY_CHANGES.md` lines 16, 66, and 86 to reference the authentic, executable test node IDs that exist in the repository.

---

### [Low] Challenge 2: Phase 1 Boundary Enforcement & Universal `DISPATCH_ENABLED=false`

- **Assumption Challenged**:  
  Phase 1 boundary cannot be bypassed via environment overrides, configuration tampering, or sneaky inclusion of Phase 2+ modules.

- **Attack Scenario & Stress Testing**:
  1. We inspected `docker-compose.yml` (line 202) and verified `DISPATCH_ENABLED: "false"`.
  2. We verified `src/graph_api.py` (lines 109, 419, 427): setting `DISPATCH_ENABLED=true` causes `/health/ready` to fail closed with HTTP 503 (`Phase 1 safety invariant violation: DISPATCH_ENABLED must be false`).
  3. We checked `migrations/0003_dispatch_foundation.sql` (line 1443): stored procedure `fc27_dispatch_transition` halts if target status is `delivered` with SQLSTATE `FC002` ("Phase 1 invariant violation: transition to delivered is disabled (DISPATCH_ENABLED=false)").
  4. We verified AST/regex scans in `tests/test_phase1_scope.py`:
     - Absence of `src/dashboard/` and `src/identity/` (0 matches).
     - Absence of forbidden prefixes (`worker`, `relay`, `tracker`, `tracking`, `ingest`, `assess`, `baseline`, `analytics`, `personalize`, `agent`) in `src/dispatch/` (0 matches).
     - Absence of `webauthn` / `py_webauthn` imports across `src/` (0 matches).
     - Absence of companion tracking table writes (`player_observations`, `dispatch_assessments`, `dispatch_plans`, `dispatch_delivery_attempts`) in `src/` (0 matches).

- **Stress Test Result**: **PASS**. Boundary protection is fully fail-closed across configuration, API, and database layers.

---

### [Low] Challenge 3: Section 1A Invariant Integrity

- **Assumption Challenged**:  
  Non-human actors (LLMs, agents, bots) or timeout/inactivity mechanisms could potentially trigger decision creation or player fallback could be granted on club policies.

- **Attack Scenario & Stress Testing**:
  1. **Agent Actor Prohibition**:
     - Evaluated `src/approval_store.py` (lines 379–380): `if actor and ("agent" in actor or actor.startswith("agent:")): raise StoreError(403, "agents cannot be decision actors")`.
     - Validated test: `tests/test_approval_lifecycle.py::test_player_agent_cannot_be_decision_actor` and `tests/e2e/test_tier4_scenarios.py::TestSection1AHumanAuthorityAndConsent::test_section_1a_player_agent_cannot_be_decision_actor`.
  2. **Inactivity / Timeout Never Approves**:
     - Evaluated `src/approval_store.py` (lines 623–715 `expire_due()`): Expiry only sets status to `'expired'`. It NEVER writes to `approval_audit`, never creates an approval decision, and never creates outbox records on `fc27:dispatch`.
     - Validated test: `tests/test_approval_lifecycle.py::test_timeout_inactivity_cannot_approve` and `tests/e2e/test_tier4_scenarios.py::TestSection1AHumanAuthorityAndConsent::test_section_1a_inactivity_and_timeout_never_creates_approval`.
  3. **Player Fallback Prohibited on Club Policies**:
     - Evaluated `migrations/0003_dispatch_foundation.sql` (line 479):
       `CHECK (allow_player_fallback = false OR scope = 'player_specific')`
     - Inserting `scope = 'club'` with `allow_player_fallback = true` raises `psycopg.errors.CheckViolation`.
     - Validated test: `tests/e2e/test_tier4_scenarios.py::TestSection1AHumanAuthorityAndConsent::test_section_1a_authority_separation_club_vs_player`.

- **Stress Test Result**: **PASS**. Section 1A invariants are mathematically and structurally guaranteed by database constraints and store logic.

---

### [Low] Challenge 4: Database Role Least Privilege & SECURITY DEFINER Lockdown

- **Assumption Challenged**:  
  Runtime application role `fc27_app` or worker role `fc27_dispatch` might possess privilege escalation routes or DDL permissions.

- **Attack Scenario & Stress Testing**:
  1. `fc27_owner`: Confirmed defined as `NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT`. Direct authentication is forbidden.
  2. Schema `public`: Confirmed `CREATE` privilege revoked from `PUBLIC`.
  3. `fc27_app`: Granted DML only (`SELECT`, `INSERT`, `UPDATE` on specific tables). Explicitly denied `CREATE` on `public` and denied `UPDATE`/`DELETE` on `public.dispatches`.
  4. `fc27_dispatch`: Granted `SELECT` only on `public.dispatches`, 0 write privileges on `dispatches`. Has no access to approval tables.
  5. `public.fc27_dispatch_transition`: Configured with `SECURITY DEFINER`, `search_path = pg_catalog, pg_temp`, zero dynamic SQL (`EXECUTE` / `format()` absent). Only `fc27_app` and `fc27_dispatch` have execute grant.
  6. Grants policy: Versioned (`-- grants-policy-version: 5`), SHA-256 hashed and recorded in `schema_policy_versions`.

- **Stress Test Result**: **PASS**. Role isolation and least privilege are rigorously enforced.

---

## Stress Test Results Summary

| Scenario | Expected Behavior | Actual Behavior | Result |
|---|---|---|:---:|
| Audit node IDs in `SECURITY_CHANGES.md` §1–11 | 100% of node IDs exist in test suite | 4 node IDs missing from repository | **FAIL** |
| Verify deleted test files absent from docs | 0 occurrences of `test_activation_gates.py`, `test_correlation.py` | 0 occurrences found | **PASS** |
| Verify legacy monolithic class names absent | 0 occurrences of `TestTier1Features`, etc. | 0 occurrences found | **PASS** |
| Setting `DISPATCH_ENABLED=true` in `graph-api` | `/health/ready` probe returns HTTP 503 degraded | Fails closed with safety violation reason | **PASS** |
| Verify Phase 2+ modules absent from `src/` | 0 files matching dashboard, identity, workers, trackers | 0 files found | **PASS** |
| Verify `webauthn` imports absent from `src/` | 0 imports or references | 0 imports found | **PASS** |
| Verify companion tracking writes absent from `src/` | 0 mutations across 4 tracking tables | 0 mutations found | **PASS** |
| Submit decision with `actor="agent:coach"` | HTTP 403 `agents cannot be decision actors` | Rejected immediately | **PASS** |
| Evaluation of expired approval request | Request transitions to `expired`; 0 approvals written | Transitions to `expired`; 0 approvals written | **PASS** |
| Insert club policy with `allow_player_fallback=true` | PostgreSQL raises `CheckViolation` | Rejected by check constraint | **PASS** |
| Attempt DDL as `fc27_app` | PostgreSQL raises `InsufficientPrivilege` | Rejected by database | **PASS** |
| Attempt `UPDATE dispatches` as `fc27_app` | PostgreSQL raises `InsufficientPrivilege` | Rejected by database | **PASS** |
| Attempt `UPDATE dispatches` as `fc27_dispatch` | PostgreSQL raises `InsufficientPrivilege` | Rejected by database | **PASS** |

---

## Required Remediation

To achieve **APPROVE** verdict:
1. In `SECURITY_CHANGES.md`, line 16:
   - Replace `tests/test_graph_api_v1.py::test_health_ready_checks_db_posture` with `tests/test_db_migrate_roles.py::test_graph_api_refuses_owner_member_or_create` (and/or `tests/e2e/test_tier1_features.py::TestGraphCoreAndApiFeatures::test_feature_27_health_probes_liveness_and_readiness`).
2. In `SECURITY_CHANGES.md`, line 66:
   - Replace `tests/test_dispatch_schema.py::test_append_only_triggers` with `tests/test_dispatch_schema.py::test_tracking_tables_append_only`.
3. In `SECURITY_CHANGES.md`, line 86:
   - Replace `tests/test_graph_persistence.py::test_restart_recovers_interrupted_graph` with `tests/test_graph_persistence.py::test_checkpoint_recovery_across_server_restart`.
   - Replace `tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_post_commit_resume_failure_recovery` with `tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_post_commit_resume_failure_preserves_authoritative_decision`.

---

## Unchallenged Areas

- Docker live runtime container execution was not challenged with a live daemon, as no Docker daemon is accessible in this execution environment; static compose validation was verified instead.
- Live EA server and live LLM inference network endpoints were not challenged live as hermetic test mocks are mandated for Phase 1.
