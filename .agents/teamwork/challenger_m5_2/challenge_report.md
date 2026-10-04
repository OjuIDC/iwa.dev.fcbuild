# Challenge Report: Milestone 5 Scope Protection & Invariants Hardening

**Agent**: `challenger_m5_2`  
**Role**: Milestone 5 Challenger 2 (Empirical Scope Protection & Invariants Hardening)  
**Target**: Milestone 5 Implementation by `worker_m5_1`  
**Date**: 2026-10-03  
**Verdict**: **APPROVE**  

---

## 1. Executive Summary

As an empirical challenger, an exhaustive adversarial audit was conducted on Phase 1 scope protection, forbidden imports, companion tracking table writes, and activation documentation invariants.

Every verification was executed empirically using static analysis, AST inspection, literal search, directory structure enumeration, and contract cross-validation. The worker's claims were not assumed; rather, every boundary assertion was independently verified against the actual repository state.

**Overall Risk Assessment**: **LOW** (Safe to proceed; Phase 1 scope boundaries are hermetically sealed, and documentation invariants strictly forbid premature activation).

---

## 2. Adversarial Attacks and Challenge Results

### Challenge 1: Phase 2+ Scope Creep & Directory Layout (C-8, AC-58)
- **Assumption Challenged**: No premature Phase 2+ modules (dashboard, identity, workers, trackers, personalizers) exist in `src/`.
- **Attack Scenario**: An implementer could have added scaffolding files (`src/dashboard/`, `src/identity/`, `src/worker.py`, `src/tracker.py`, etc.) to facilitate future phases or test conveniences.
- **Empirical Verification**:
  - `src/dashboard` (directory and file): **CONFIRMED NON-EXISTENT**.
  - `src/identity` (directory and file): **CONFIRMED NON-EXISTENT**.
  - `src/dispatch/` directory enumeration:
    Only 5 files exist: `__init__.py`, `creation.py`, `gates.py`, `objective.py`, `transition.py`.
  - Stems and file names were tested against all 10 forbidden prefixes (`worker`, `relay`, `tracker`, `tracking`, `ingest`, `assess`, `baseline`, `analytics`, `personalize`, `agent`):
    **ZERO MATCHES** (`0/5` offending files).
  - Extended check on root `src/` for forbidden prefixes:
    Checked all 30 root files (`approval.py`, `approval_store.py`, `audit_query.py`, `benchmark.py`, `correlation.py`, `db_migrate.py`, `db_roles.py`, `decision.py`, `events.py`, `fc27_mcp_server.py`, `graph.py`, `graph_api.py`, `hierarchy.py`, `lead_packages.py`, `live_match_watcher.py`, `live_ws_server.py`, `mcp_retry.py`, `model_selection.py`, `ocr_parsing.py`, `post_match_rundown.py`, `roles.py`, `rtsp_frame_analyzer.py`, `schemas.py`, `security.py`, `settings.py`, `swarms_backend.py`, `thread_ids.py`, `tracing.py`, `voice_gateway.py`).
    **ZERO MATCHES**.
- **Blast Radius**: None. Boundary is intact.
- **Result**: **PASS**.

---

### Challenge 2: Forbidden Identity & Passkey Imports (C-8, AC-58)
- **Assumption Challenged**: No WebAuthn / FIDO2 / Passkey library or module imports exist in any Python code under `src/`.
- **Attack Scenario**: Phase 2 authentication or WebAuthn dependencies could have been imported in `src/security.py`, `src/graph_api.py`, or schema files as forward-looking helpers.
- **Empirical Verification**:
  - Case-insensitive regex and string grep for `webauthn`: **0 results** across all 35 Python files in `src/`.
  - Case-insensitive regex and string grep for `py_webauthn`: **0 results** across all 35 Python files in `src/`.
  - Case-insensitive regex and string grep for `fido`: **0 results** across all 35 Python files in `src/`.
  - Case-insensitive regex and string grep for `passkey`: **0 results** across all 35 Python files in `src/`.
  - AST Walk trace (`ast.Import` and `ast.ImportFrom` nodes): All import statements in `src/` resolve exclusively to standard library, FastAPI/Pydantic, LangGraph/LangChain, psycopg/psycopg_pool, httpx, and local `src` modules.
- **Blast Radius**: None. Phase 2 WebAuthn identity is completely deferred.
- **Result**: **PASS**.

---

### Challenge 3: Companion Tracking Table Mutations (C-8, AC-58)
- **Assumption Challenged**: No production code under `src/` or `migrations/` writes to the Phase 5 companion tracking tables (`player_observations`, `dispatch_assessments`, `dispatch_plans`, `dispatch_delivery_attempts`).
- **Attack Scenario**: Stored procedures, triggers, or backend modules could inadvertently write baseline records, mock tracking states, or attempt logging outside the test harness.
- **Empirical Verification**:
  - Grep search for `player_observations` across `src/`: **0 results**.
  - Grep search for `dispatch_assessments` across `src/`: **0 results**.
  - Grep search for `dispatch_plans` across `src/`: **0 results**.
  - Grep search for `dispatch_delivery_attempts` across `src/`: **0 results**.
  - Repository-wide audit:
    - `migrations/0003_dispatch_foundation.sql`: Defines tables, foreign keys, immutability triggers, and check constraints (`source_table IN (...)`). Contains **0 DML mutation statements** (`INSERT INTO`, `UPDATE`, `DELETE FROM`, `TRUNCATE`) on tracking tables.
    - `migrations/post/grants.sql`: Defines grants (`SELECT` to `fc27_app`, `SELECT, INSERT` to `fc27_dispatch`).
    - `tests/`: Mutations exist strictly inside tests (`test_adversarial_m3_challenger.py`, `test_dispatch_schema.py`, `test_dispatch_transition.py`).
  - Python write search: Zero SQL mutation queries targeting tracking tables exist in production code.
- **Blast Radius**: None. Tracking tables remain pure forward-compatible schema placeholders.
- **Result**: **PASS**.

---

### Challenge 4: Dispatch Activation Documentation Invariants (C-2, C-3)
- **Assumption Challenged**: `docs/DISPATCH_ACTIVATION.md` accurately documents the exact runtime safety invariant error message, all 6 rollout gates, the Phase 1 G-OUTBOX refusal, and the 4 mandatory contract statements forbidding Phase 1 operational enablement.
- **Attack Scenario**: Discrepancies between documentation and actual implementation in `src/graph_api.py` / `src/dispatch/gates.py`, or diluted statements permitting soft activation.
- **Empirical Verification**:
  - **Exact Disabled Message**:
    - Documented in `docs/DISPATCH_ACTIVATION.md` line 41:
      `Phase 1 safety invariant violation: DISPATCH_ENABLED must be false`
    - Implemented in `src/graph_api.py` lines 419 and 427:
      `"reason": f"Phase 1 safety invariant violation: DISPATCH_ENABLED must be false (dispatch configured but blocked: {', '.join(failed)})"`
      `"reason": "Phase 1 safety invariant violation: DISPATCH_ENABLED must be false"`
      **EXACT MATCH**.
  - **6 Rollout Gates Table**:
    Section 3 contains table specifying:
    1. `G-MIGRATION` (Automatic, migration 0003 applied)
    2. `G-GRANTS` (Automatic, least-privilege catalog posture)
    3. `G-WORKER` (Automatic, `service_heartbeats` <60s; fails closed in Phase 1 as no worker exists)
    4. `G-ID` (Manual, signed HMAC evidence; `UNVERIFIED (G-ID not met)`)
    5. `G-ATOMIC` (Manual, signed CI URL for atomic approval-to-dispatch tests)
    6. `G-OUTBOX` (Manual, Phase 3 outbox relay evidence; refused in Phase 1 via `not_recordable_before_phase_3`)
    **ALL 6 PRESENT AND ACCURATELY SPECIFIED**.
  - **4 Verbatim Contract Statements**:
    1. Statement 1: `"Phase 1 MUST NOT be enabled in any deployed environment"` -> Found verbatim at Section 1, item 2.
    2. Statement 2: `"Passing the Phase 1 tests alone does not authorize activation"` -> Found verbatim at Section 1, item 3.
    3. Statement 3: `"There is no production dispatch-worker in Phase 1, so G-WORKER cannot pass"` -> Found verbatim at Section 1, item 4.
    4. Statement 4: `"G-OUTBOX cannot be satisfied before Phase 3: its evidence is the Phase 3 relay and reconciliation test run, and the route refuses it in Phase 1 (not_recordable_before_phase_3)"` -> Found verbatim at Section 1, item 5.
- **Blast Radius**: None. Operators cannot misinterpret Phase 1 completion as operational enablement.
- **Result**: **PASS**.

---

### Challenge 5: EA Player Identity Verification Runbook (AC-47)
- **Assumption Challenged**: `docs/EA_PLAYER_IDENTITY_VERIFICATION.md` remains strictly `UNVERIFIED` and does not contain fabricated match captures or player records to bypass `G-ID`.
- **Attack Scenario**: Fake sample player IDs or synthetic match logs entered into AC-47 tables could be claimed as empirical proof of EA player ID stability.
- **Empirical Verification**:
  - Document status line 3: `**Status: UNVERIFIED (G-ID not met)**`.
  - Table 2.1 (`clubs/matches` and `members/stats` structures): Both rows contain `*[TBD]*` and `*Pending live EA capture*`.
  - Table 2.2 (Stability across 3 matches): All 3 rows contain `*[TBD]*` and `*Pending capture across 3 matches*`.
  - Table 2.3 (Stability across gamertag change): Row contains `*[TBD]*` and `*Pending player rename test*`.
  - Table 2.4 (Club and Platform scoping): Row contains `*[TBD]*` and `*Pending verification*`.
  - Table 2.5 (Transferred, removed, duplicate, unavailable edge cases): All 4 rows contain `*[TBD]*` and `*Pending test*`.
  - Zero fabricated or production player identities exist.
- **Blast Radius**: None. Gate `G-ID` cannot be accidentally recorded or justified without real Phase 3 live captures.
- **Result**: **PASS**.

---

### Challenge 6: Stress Testing of `tests/test_phase1_scope.py` Test Logic
- **Assumption Challenged**: The test suite in `tests/test_phase1_scope.py` is thorough, cannot be trivially circumvented, and will reliably fail if boundary violations occur.
- **Analysis**:
  - `test_no_phase2plus_code()` checks both file existence and directory existence for `dashboard` and `identity`, and walks `src/dispatch/` with rglob checking all stems and names against 10 prefixes.
  - `test_no_webauthn_imports()` uses both AST parsing and multiline regex matching, detecting direct imports, relative imports, and module aliases.
  - `test_no_tracking_writes_outside_tests()` inspects all Python files under `src/` against case-insensitive DML commands targeting all four tracking tables with or without schema prefixes.
  - `test_docs_state_disabled_and_gates()` asserts presence of all 6 gate tokens and exact error string.
  - `test_activation_doc_forbids_phase1_enablement()` uses regex search with case-insensitive patterns to guarantee all 4 contract statements and required error tokens are present.
- **Result**: **PASS**.

---

## 3. Stress Test Matrix Summary

| Test Case / Attack | Expected Behavior | Actual Behavior | Pass/Fail |
|---|---|---|---|
| Probe `src/dashboard` / `src/identity` | Non-existent | Neither directory nor file exists | **PASS** |
| Probe `src/dispatch/` forbidden prefixes | Zero files matching 10 forbidden prefixes | All 5 files are approved Phase 1 modules | **PASS** |
| Probe `src/` for WebAuthn imports | Zero imports / AST nodes | 0 occurrences in AST and text | **PASS** |
| Probe `src/` for companion tracking writes | Zero DML queries on tracking tables | 0 occurrences | **PASS** |
| Validate exact disabled error string | Matches `src/graph_api.py` | `"Phase 1 safety invariant violation: DISPATCH_ENABLED must be false"` | **PASS** |
| Validate 6 activation gates table | Lists G-MIGRATION through G-OUTBOX | Complete table present with Phase 1 status | **PASS** |
| Validate 4 mandatory boundary statements | Verbatim contractual statements present | All 4 statements present verbatim | **PASS** |
| Validate EA Runbook AC-47 tables | Status UNVERIFIED and empty tables | Status UNVERIFIED, all tables contain `[TBD]` | **PASS** |

---

## 4. Final Verdict

**Verdict**: **APPROVE**

All Phase 1 scope boundaries, forbidden imports, companion tracking writes, and activation documentation invariants are fully satisfied and rigorously protected. Milestone 5 is ready for final orchestrator sign-off and progression to Milestone 6.
