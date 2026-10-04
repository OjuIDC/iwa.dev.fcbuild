# Handoff Report: Milestone 1 Schema Exploration & Design

## 1. Observation

1. **Current Schema & Migration Baseline:**
   - `migrations/0001_initial.sql`: Defines `graph_threads(thread_id, state, status, updated_at)` and `approval_audit(id, thread_id, commissioner, decision, notes, owner, selection, candidate_id, player_id, decided_at)` (lines 3–21).
   - `migrations/0002_approval_security.sql`: Adds `club_id`, `request_hash`, `package_version`, `created_at`, `approved_by`, `approved_at`, `decision_payload` to `graph_threads` (lines 5–12); adds `graph_threads_status_chk` (`running`, `awaiting_approval`, `approved`, `rejected`, `failed`), `graph_threads_terminal_fields_chk`, `fc27_guard_thread_transition()` (lines 28–70); adds `approval_nonces` table (lines 99–109); adds `run_sequences` (lines 117–122).
   - `migrations/0003_dispatch_foundation.sql`: Currently contains Section A only (lines 1–122), creating `domain_events` (lines 5–27), `outbox` (lines 29–38), `fc27_reject_mutation()` append-only trigger (lines 40–55), and initial `fc27_check_event_source()` (lines 57–121). Section B is absent.
   - `migrations/post/grants.sql`: Currently on `-- grants-policy-version: 2` (line 1), granting DML to `fc27_app` on 0001/0002 tables, checkpoints, and 0003 Section A `domain_events`/`outbox` (lines 1–51).
   - `src/db_migrate.py`: Defines `GRANTS_POLICY = GrantsPolicy(version=2, sha256="a0c8d37ed61daff8900b116f992fa791f5072c11862c17c264cbc9251f0f4f79")` (line 21).

2. **Program Requirements & Invariants:**
   - `ORIGINAL_REQUEST.md`: Section 3.3 requires `approval_requests` binding exact recommendation package ID, package version, package hash, Graph thread/run, required authority, lifecycle status, creation time, and expiry (lines 132–144).
   - Section 1A Patch (`ORIGINAL_REQUEST.md` lines 967–1317): Requires separating club authorization, delegated authority, player consent, and fallback authority. Inactivity/timeout must never create approval. Agents must never decide for humans. Requires 7 compatibility tables: `authority_policies`, `authority_delegations`, `player_assignments`, `player_decision_requests`, `player_decisions`, `approval_escalations`, and `authority_conflicts`.
   - `docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md`: Task 4 details Section B requirements: `approval_requests` table with immutable provenance triggers and terminal freezing, `graph_threads` FK and extended status checks, `fc27_guard_thread_transition()` replacement, `fc27_check_approval_pair()` deferrable constraint triggers, `fc27_approval_inconsistencies` view, `approval_audit` fields, `approval_nonces` fields, `fc27_check_event_source()` update, and legacy backfill (lines 395–434).

3. **Runtime Role Posture:**
   - `fc27_owner`: Owns all database relations; `NOLOGIN`.
   - `fc27_migrator`: Executes migrations with `SET ROLE fc27_owner`.
   - `fc27_app`: Graph API runtime identity; has DML on approval tables, but no DDL or ownership.
   - `fc27_dispatch`: Dispatch worker identity; must have zero access to approval or identity tables.

---

## 2. Logic Chain

1. **Dual-Track Schema Convergence:**
   - *Premise (Observation 1 & 2):* Task 4 specifies the core `approval_requests` lifecycle and expiry mechanism, while the Section 1A patch mandates separating club authority from player consent and affected-player fallback.
   - *Inference:* If only Task 4 were implemented, the schema would assume Commissioner-only authority and would require destructive schema migrations in Phase 2/3. By implementing Task 4 Section B alongside the 7 Section 1A compatibility tables in migration 0003, both requirements are satisfied simultaneously without breaking changes.

2. **Transactional Consistency via Constraint Triggers:**
   - *Premise (Observation 1 & 2):* `graph_threads` tracks LangGraph execution status, while `approval_requests` tracks authoritative authorization status. If either status drifts, the system could permit unauthorized execution or orphan approvals.
   - *Inference:* Placing a `DEFERRABLE INITIALLY DEFERRED` constraint trigger (`fc27_check_approval_pair`) on both tables allows atomic multi-step mutations within a single transaction while guaranteeing strict status, version, and thread pairing at transaction `COMMIT`.
   - *Deduction:* Any transaction modifying one table without the other fails closed at commit, satisfying acceptance criterion AC-55.

3. **Deterministic Legacy Backfill:**
   - *Premise (Observation 1 & 2):* Pre-existing 0002 databases may contain historical threads. Awaiting threads with `package_version` need an active `approval_requests` record, while terminal (`approved`/`rejected`/`failed`) threads cannot be modified due to terminal guard triggers.
   - *Inference:* Backfilling only `awaiting_approval` threads with a `package_version` prior to activating the constraint trigger ensures existing pending runs remain decidable without violating terminal immutability constraints. Historical threads without links are classified as `legacy_unlinked` in `fc27_approval_inconsistencies`, preserving audit fidelity (AC-56).

4. **Least-Privilege Role Hardening & Grants Versioning:**
   - *Premise (Observation 1 & 3):* `migrations/post/grants.sql` is tracked via cryptographic SHA-256 policy versioning in `src/db_migrate.py`.
   - *Inference:* Adding Milestone 1 tables to `grants.sql` requires granting DML to `fc27_app`, withholding all grants from `fc27_dispatch`, bumping `-- grants-policy-version: 3`, and updating `GRANTS_POLICY` in `src/db_migrate.py`. This guarantees least privilege and prevents policy divergence.

---

## 3. Caveats

1. **Phase 1 Inactive Endpoints:**
   - The 7 Section 1A compatibility tables (`authority_policies`, `authority_delegations`, `player_assignments`, `player_decision_requests`, `player_decisions`, `approval_escalations`, `authority_conflicts`) are created in schema for data model compatibility only. No active player authentication, passkey registration, or player decision endpoints should be activated in Phase 1 runtime code (`DISPATCH_ENABLED=false`).
2. **Deterministic Objective Bounds:**
   - Objective bounds and candidate verification (`fc27-objective-bounds-v1`) belong to Milestone 4 / Task 8, but the schema compatibility fields on `approval_audit` support storing objective and candidate bindings immediately.
3. **Foreign Keys on Phase 2 Identity:**
   - `actor_user_id` and other user-specific columns are created as nullable `uuid` without foreign keys to `users`, because the `users` table is scheduled for Phase 2 (`0004_identity.sql`).

---

## 4. Conclusion

The technical schema design for Milestone 1 is fully specified and documented in `schema_analysis.md`. The design provides:
1. Exact DDL and trigger specifications for `0003_dispatch_foundation.sql` Section B (`approval_requests`, `graph_threads` updates, `fc27_check_approval_pair`, `fc27_approval_inconsistencies`, `approval_audit` extensions, `approval_nonces` extensions, and `fc27_check_event_source` update).
2. Complete DDL, constraints, and immutability triggers for the seven Section 1A compatibility tables.
3. Idempotent least-privilege grants script for `migrations/post/grants.sql` with version bump to 3.
4. Cryptographic policy synchronization for `src/db_migrate.py`.

The schema fulfills 100% of Milestone 1 criteria (AC-43, AC-44, AC-55, AC-56, AM-4, AM-14, Section 1A patch) and is completely ready for the implementation agent.

---

## 5. Verification Method

To independently verify the schema implementation once applied:

1. **Verify Fresh Migration Application:**
   ```bash
   python -m pytest -o addopts="" -q tests/test_db_migrate_roles.py
   python -m pytest -o addopts="" -q tests/test_migration_0003.py
   python -m pytest -o addopts="" -q tests/test_approval_lifecycle.py
   ```
2. **Verify Legacy Backfill & Inconsistency View:**
   Execute SQL against the test database:
   ```sql
   SELECT count(*) FROM public.fc27_approval_inconsistencies WHERE kind <> 'legacy_unlinked';
   ```
   *Expected Result:* Exactly `0`.
3. **Verify Grants Policy Versioning:**
   ```bash
   python -c "
   import hashlib, pathlib
   sql = pathlib.Path('migrations/post/grants.sql').read_text()
   assert sql.startswith('-- grants-policy-version: 3')
   digest = hashlib.sha256(sql.encode()).hexdigest()
   from src.db_migrate import GRANTS_POLICY
   assert GRANTS_POLICY.version == 3 and GRANTS_POLICY.sha256 == digest
   print('GRANTS_POLICY verified successfully.')
   "
   ```
4. **Invalidation Conditions:**
   - If `fc27_approval_inconsistencies` reports any `status_mismatch`, `package_mismatch`, or `missing_link`.
   - If an awaiting thread with `package_version` can be committed without a linked `approval_request`.
   - If `fc27_dispatch` acquires permissions on `approval_requests` or Section 1A tables.
   - If any `grants.sql` change is applied without bumping `GRANTS_POLICY` in `src/db_migrate.py`.
