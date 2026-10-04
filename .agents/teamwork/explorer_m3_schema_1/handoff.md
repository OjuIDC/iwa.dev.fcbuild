# Handoff Report: Milestone 3 Schema & Triggers Exploration

**Agent**: `explorer_m3_schema_1`  
**Handoff Type**: Hard (Task Complete)  
**Date**: 2026-10-03  
**Target File**: `.agents/teamwork/explorer_m3_schema_1/handoff.md`  

---

## 1. Observation

1. **`migrations/0003_dispatch_foundation.sql` (lines 1–810)**:
   - Contains Section A (lines 3–123): `domain_events`, `outbox`, append-only trigger `fc27_reject_mutation()`, event source trigger `fc27_check_event_source()`.
   - Contains Section B (lines 124–810): `approval_requests`, `graph_threads` link and guards, `fc27_check_approval_pair()` constraint trigger, `fc27_approval_inconsistencies` diagnostic view, Section 1A compatibility tables (`authority_policies`, `authority_delegations`, `player_assignments`, `player_decision_requests`, `player_decisions`, `approval_escalations`, `authority_conflicts`), and `approval_audit` / `approval_nonces` alterations.
   - Section C (`players`, `dispatches`, forward tracking tables) and Section D (`fc27_dispatch_transition`, `dispatch_transition_rules`) do not exist yet in the file.
2. **`migrations/post/grants.sql` (lines 1–82)**:
   - Line 1 specifies `-- grants-policy-version: 3`.
   - Lines 36–49 grant `SELECT, INSERT` on `domain_events` to `fc27_app`, `SELECT` to `fc27_dispatch`, and `INSERT` on `outbox` to `fc27_app`.
   - Neither `public.players` nor `public.dispatches` currently have grant statements in this file.
3. **`src/db_migrate.py` (lines 17–21)**:
   - Line 21 defines `GRANTS_POLICY = GrantsPolicy(version=3, sha256="9ac85df90575adbc3938fb5ec68c459eb1c70de74d5191bebc050fdde215f4b1")`.
   - Modifying `grants.sql` requires bumping the header to `version: 4` and updating this constant synchronously.
4. **`docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md` (Tasks 6 & 7, lines 657–916)**:
   - Task 6 defines migration 0003 Part C: `players`, `dispatches`, `created_correlation_id uuid NOT NULL`, partial unique index `dispatches_one_active_per_objective` on `(player_id, owner, (approved_instruction->'objective'->>'metric'))`, triggers `fc27_dispatch_insert_guard` and `fc27_dispatch_immutable`, and forward-compatible tracking tables.
   - Task 7 defines migration 0003 Part D: `fc27_dispatch_transition` SECURITY DEFINER with `SET search_path = pg_catalog, pg_temp`, owner `fc27_owner`, rule matrix `dispatch_transition_rules`, SQLSTATEs `FC001`–`FC007`, and patch allowlists.
5. **`docs/superpowers/specs/2026-10-02-player-dispatch-design.md` (§11.3, §14.3, §14.4, §14.7)**:
   - §14.3 specifies exact column types and constraints for `players` and `dispatches`.
   - §14.7 specifies least-privilege role matrix: `fc27_app` gets `SELECT, INSERT` on `dispatches` (NO `UPDATE` or `DELETE`), and `fc27_dispatch` gets `SELECT` on `dispatches` (NO `INSERT`, `UPDATE`, or `DELETE`).
   - §11.3 specifies that in Phase 1, `commissioner` actors may only execute `→ superseded`, and user actors raise `FC004`.
6. **`src/approval_store.py` (lines 590–605)**:
   - The `apply_decision` method contains an explicit hook for `dispatch_creator(conn, ...)` when `dispatch_enabled` is true and decision is `APPROVE`.

---

## 2. Logic Chain

1. **State Machine Integrity**:
   - Because `dispatches` represents the execution boundary for player-directed instructions, accidental or rogue updates outside the state machine would violate the Phase 1 invariant that no unauthorized player delivery or state progression can occur (Observation 4, 5).
   - Therefore, `fc27_app` must be granted only `SELECT, INSERT` on `dispatches` (to create initial `pending` rows inside the approval transaction), while `fc27_dispatch` receives only `SELECT`. Direct `UPDATE` and `DELETE` are denied to both roles.
2. **Deterministic State Transitions via SECURITY DEFINER**:
   - Because neither runtime role can directly update `dispatches`, all status changes must be mediated by `public.fc27_dispatch_transition`.
   - Because `fc27_dispatch_transition` runs as `fc27_owner` (SECURITY DEFINER) with `SET search_path = pg_catalog, pg_temp`, it bypasses runtime role UPDATE restrictions while enforcing allowlisted transitions from `dispatch_transition_rules`, checking optimistic version locking (`FC001`), validating actor identity (`FC004`), and verifying in-transaction Commissioner audit proof for supersession.
3. **Status Domain Reconciliation**:
   - The dispatch task instructions specified: `(pending, running, paused, delivered, failed, cancelled)`.
   - The approved architecture spec (§11.3, §14.3) specifies: `('pending','queued','delivered','requires_reapproval','failed','acknowledged','tracking','completed','expired','cancelled','superseded')`, while Section 1A introduces player pause (`paused`).
   - Reconciling these produces a unified 13-state CHECK constraint encompassing all states, along with a stored generated column `status text GENERATED ALWAYS AS (dispatch_status) STORED` to guarantee seamless compatibility with all callers.
4. **Provenance & Terminal Immutability**:
   - Initial row creation is constrained by `fc27_dispatch_insert_guard` ensuring version 1, plan_version 0, pending status, and null timestamps.
   - Subsequent row updates are constrained by `fc27_dispatch_immutable` ensuring that 17 provenance columns (`dispatch_id`, `approval_id`, `player_id`, `package_version`, etc.) cannot be modified, and terminal rows (`completed`, `expired`, `cancelled`, `superseded`) cannot be updated or deleted.
5. **Grants Policy Synchronization**:
   - Adding grants for `players`, `dispatches`, and tracking tables in `migrations/post/grants.sql` alters the file content.
   - Per `src/db_migrate.py` (Observation 3), any change to `grants.sql` requires bumping the version header to `version: 4` and updating `GRANTS_POLICY` in `src/db_migrate.py` in the same commit to prevent migration runner rejection.

---

## 3. Caveats

1. **No Production Source Code Modifications**:
   - In accordance with explorer role constraints, no production files in `src/` or `migrations/` were modified during this investigation. All SQL DDL, triggers, and grant statements are documented in `schema_analysis.md`.
2. **Phase 1 Operational Guard**:
   - `DISPATCH_ENABLED = false` remains in effect. The presence of the `dispatches` table and transition functions in Milestone 3 does not activate real-world dispatch workers or player delivery adapters.
3. **Phase 2 Identity Schema Dependencies**:
   - In Milestone 3, columns like `user_id` on `dispatch_acknowledgments` and `dispatch_comments` are created without foreign keys to `public.users`, because `users` is introduced in Phase 2 (`0004_identity.sql`).

---

## 4. Conclusion

1. **Milestone 3 Part C DDL is Fully Specified**:
   - `public.players` enforces strict numeric EA IDs (`^[0-9]{1,20}$`), club scoping, verification timestamps, and synchronized status/active fields.
   - `public.dispatches` enforces 1:1 `approval_id` binding, immutable provenance, partial unique indexing for active objectives, and optimistic versioning.
   - `public.dispatch_plans`, `public.player_observations`, and `public.dispatch_assessments` (with companion tables) provide append-only, forward-compatible tracking schemas.
2. **Milestone 3 Part D Transition Engine is Fully Specified**:
   - `public.dispatch_transition_rules` and `public.fc27_dispatch_transition` establish the single write path with standardized `FC001`–`FC007` SQLSTATE codes, in-transaction Commissioner audit verification, and atomic domain event + outbox emission.
3. **Role Grants Follow Least Privilege**:
   - `fc27_app`: `SELECT, INSERT` on `players` and `dispatches` (NO `UPDATE`/`DELETE`).
   - `fc27_dispatch`: `SELECT` on `players` and `dispatches`.
   - Both roles execute `fc27_dispatch_transition`.
   - Grants policy version bump to `4` is documented and prepared.

---

## 5. Verification Method

Once implemented by the downstream implementation agent, the schema and triggers can be verified independently as follows:

1. **Migration Application & Post-conditions**:
   ```pwsh
   python -m pytest -o addopts="" -q tests/test_migration_0003.py
   python -m pytest -o addopts="" -q tests/test_db_migrate_roles.py
   ```
2. **Dispatch Schema & Provenance Immutability Tests**:
   - Run `tests/test_dispatch_schema.py`:
     - Verify non-numeric `player_id` is rejected with `CheckViolation`.
     - Verify inserting non-pending dispatch raises `CheckViolation`.
     - Verify direct update of provenance fields (`player_id`, `approval_id`, etc.) raises `check_violation`.
     - Verify `fc27_app` cannot execute direct `UPDATE public.dispatches`.
     - Verify `fc27_dispatch` cannot execute `INSERT` or `UPDATE` on `public.dispatches`.
3. **Transition Function & State Machine Tests**:
   - Run `tests/test_dispatch_transition.py`:
     - Verify passable edges succeed and return incremented version.
     - Verify illegal edges raise `FC002`.
     - Verify optimistic concurrency conflict raises `FC001`.
     - Verify missing correlation ID raises `FC005`.
     - Verify terminal rows are frozen against further transitions.
4. **Grants Policy Integrity**:
   - Run `tests/test_db_migrate_roles.py::test_grants_policy_constant_matches_file` to verify header version and SHA-256 match `GRANTS_POLICY` in `src/db_migrate.py`.
