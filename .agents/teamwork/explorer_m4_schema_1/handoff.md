# Handoff Report: Milestone 4 Database Schema & Migration Exploration

**Agent**: `explorer_m4_schema_1`  
**Role**: Milestone 4 Schema & Migration Explorer  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_schema_1`  
**Timestamp**: 2026-10-03T20:15:30Z  
**Type**: Hard Handoff  

---

## 1. Observation

1. **Section D termination in migration 0003**:
   `migrations/0003_dispatch_foundation.sql` lines 1776–1784 end Section D with:
   ```sql
   ALTER FUNCTION public.fc27_dispatch_transition(uuid, text, integer, text, text, uuid, jsonb) 
       OWNER TO fc27_owner;

   REVOKE ALL ON FUNCTION public.fc27_dispatch_transition(uuid, text, integer, text, text, uuid, jsonb) 
       FROM PUBLIC;

   GRANT EXECUTE ON FUNCTION public.fc27_dispatch_transition(uuid, text, integer, text, text, uuid, jsonb) 
       TO fc27_app, fc27_dispatch;
   ```
   No Section E currently exists in the file.

2. **Domain events table checks in migration 0003**:
   `migrations/0003_dispatch_foundation.sql` line 17 defines:
   ```sql
   source_table text CHECK (source_table IN ('approval_audit','approval_requests','dispatches','dispatch_acknowledgments','dispatch_assessments','dispatch_plans','dispatch_plan_decisions','dispatch_delivery_attempts','user_roles','activation_gate_log')),
   ```
   `activation_gate_log` is already included in the `domain_events.source_table` check constraint.

3. **Event source trigger function**:
   `migrations/0003_dispatch_foundation.sql` lines 739–809 (`fc27_check_event_source`) validate source tables for `approval.decided`, `approval.created`, `approval.expired`, `dispatch.created`, `dispatch.acknowledged`, `tracking.updated`, and `tracking.completed`. There is currently no handler branch for `activation.gate_recorded`.

4. **Approval nonces support for activation gates**:
   `migrations/0003_dispatch_foundation.sql` lines 680–720 already define `gate_id text NULL` on `public.approval_nonces`, include `'activation_gate'` in `approval_nonces_subject_type_chk`, and include:
   ```sql
   (subject_type = 'activation_gate' 
       AND gate_id IS NOT NULL 
       AND thread_id IS NULL AND dispatch_id IS NULL AND user_id IS NULL)
   ```
   in `approval_nonces_subject_columns_chk`.

5. **Grants policy and versioning**:
   `migrations/post/grants.sql` line 1 has `-- grants-policy-version: 4`.
   `src/db_migrate.py` line 21 defines:
   ```python
   GRANTS_POLICY = GrantsPolicy(version=4, sha256="9488d48e4ca1ebea64fa7fedd44cc3c02712b0fa2d9bafac424dc97b80711495")
   ```
   Lines 164–170 of `src/db_migrate.py` require that `grants.sql` line 1 matches `-- grants-policy-version: {GRANTS_POLICY.version}` and that `hashlib.sha256(sql.encode()).hexdigest() == GRANTS_POLICY.sha256`.

6. **Task 8 specification in Phase 1 plan**:
   `docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md` lines 933–938 define:
   - `public.activation_gate_log(id bigserial, gate_id text CHECK (gate_id IN ('G-ID','G-ATOMIC','G-OUTBOX')), status text CHECK (status IN ('passed','failed')), evidence_ref text NOT NULL CHECK (char_length(evidence_ref) BETWEEN 1 AND 500), code_revision text NOT NULL, deployment_id text NOT NULL, fc_version text NOT NULL CHECK (fc_version = '27'), platform text NOT NULL CHECK (platform = 'common-gen5'), nonce text NOT NULL, signature_sha256 text NOT NULL, recorded_by text NOT NULL, recorded_at timestamptz NOT NULL DEFAULT now())`. Append-only.
   - View `public.activation_gates` showing latest row per gate.
   - Table `public.service_heartbeats`.
   - Grants: `fc27_app` gets `SELECT, INSERT` on log, `SELECT` on view, `SELECT` on heartbeats; `fc27_dispatch` gets `SELECT, INSERT, UPDATE` on heartbeats.

---

## 2. Logic Chain

1. **Requirement to append Section E** (Observation 1, 6):
   Section E must be appended to `migrations/0003_dispatch_foundation.sql`. It must create `public.activation_gate_log`, attach the `fc27_activation_gate_log_immutable` trigger invoking `fc27_reject_mutation()`, create view `public.activation_gates` via `DISTINCT ON (gate_id)`, create table `public.service_heartbeats`, and update `fc27_check_event_source()`.

2. **Immutability of `activation_gate_log`** (Observation 1, 6):
   By attaching `BEFORE UPDATE OR DELETE ON public.activation_gate_log FOR EACH ROW EXECUTE FUNCTION public.fc27_reject_mutation()`, any mutation or deletion is blocked at the database engine level with a `check_violation` exception, preserving strict append-only audit semantics.

3. **Event source validation completeness** (Observation 2, 3, 6):
   When `record_event()` emits `activation.gate_recorded`, `fc27_check_event_source()` must verify:
   - `NEW.source_table = 'activation_gate_log'`
   - `NEW.source_id IS NOT NULL`
   - Row with `id = NEW.source_id::bigint` exists in `public.activation_gate_log` and `gate_id = NEW.entity_id`.
   Replacing `fc27_check_event_source()` in Section E ensures database-level validation prevents spoofed gate audit events.

4. **Sequence privilege requirement** (Observation 5, 6):
   Because `activation_gate_log` uses `id bigserial PRIMARY KEY`, PostgreSQL generates the sequence `activation_gate_log_id_seq`. In order for `fc27_app` to perform `INSERT`, it requires `GRANT USAGE ON SEQUENCE public.activation_gate_log_id_seq TO fc27_app`.

5. **Grants policy bump & least privilege** (Observation 5, 6):
   `migrations/post/grants.sql` must be updated to `-- grants-policy-version: 5`.
   `fc27_app` receives `SELECT, INSERT` on `activation_gate_log`, `USAGE` on its sequence, `SELECT` on `activation_gates`, and `SELECT` on `service_heartbeats`.
   `fc27_dispatch` receives `SELECT, INSERT, UPDATE` on `service_heartbeats` only, with zero privileges on `activation_gate_log` or `activation_gates`.
   `GRANTS_POLICY` in `src/db_migrate.py` must be updated to `version=5` with the exact computed SHA-256 hash.

---

## 3. Caveats

1. **Read-Only Explorer Scope**: In accordance with the Teamwork explorer archetype, no changes have been applied to production files (`migrations/0003_dispatch_foundation.sql`, `migrations/post/grants.sql`, `src/db_migrate.py`). All SQL statements and code snippets in `schema_analysis.md` are copy-paste ready for the implementing worker.
2. **Phase 1 Gate Restrictions**: While `activation_gate_log` schema allows `'G-OUTBOX'` in its check constraint, the application layer in Phase 1 (`src/dispatch/gates.py` and `src/graph_api.py`) forbids recording `G-OUTBOX` with a 409 error. Only synthetic test fixtures may insert `G-OUTBOX` rows via admin credentials in Phase 1.
3. **No Dynamic SQL**: All triggers and functions adhere to the non-negotiable invariant of using no dynamic SQL (`EXECUTE`, `format`) and fully schema-qualifying all table references (`public.x`).

---

## 4. Conclusion

The database schema and migration requirements for Milestone 4 are fully explored, verified against repository constraints, and completely specified:
- Exact DDL for `activation_gate_log`, trigger `fc27_activation_gate_log_immutable`, view `activation_gates`, and `service_heartbeats` are detailed in `schema_analysis.md`.
- Exact trigger replacement for `fc27_check_event_source()` is formulated.
- Complete grants block for `grants.sql` (policy version 5) and integration with `src/db_migrate.py` are documented.
- No DDL changes to `approval_nonces` or `domain_events` are needed as they already contain the required columns and constraints.

---

## 5. Verification Method

1. **Inspection of DDL files**:
   - Verify `migrations/0003_dispatch_foundation.sql` contains Section E with all columns and constraints.
   - Verify `migrations/post/grants.sql` header is `-- grants-policy-version: 5`.
   - Verify `src/db_migrate.py` has `GRANTS_POLICY.version == 5` and `GRANTS_POLICY.sha256` matching `grants.sql`.

2. **Automated Test Verification**:
   - `python -m pytest tests/test_db_migrate_roles.py`:
     Verifies grants policy constant matches file, migration applies cleanly, and no postcondition violations occur.
   - `python -m pytest tests/test_events_outbox.py`:
     Verifies domain events and append-only constraints.
   - `python -m pytest tests/test_dispatch_schema.py`:
     Verifies schema constraints and least-privilege boundaries.
   - `python -m pytest tests/e2e/test_tier1_features.py`:
     Tests `test_feature_35_activation_gate_evaluation_fails_closed` will un-skip once `activation_gate_log` is migrated.

3. **Invalidation Conditions**:
   - If `grants.sql` is edited without updating `GRANTS_POLICY.sha256`, `test_grants_policy_constant_matches_file` will fail.
   - If `activation_gate_log` allows `UPDATE` or `DELETE`, `test_feature_07_append_only_table_guards_reject_mutations` pattern tests will fail.
