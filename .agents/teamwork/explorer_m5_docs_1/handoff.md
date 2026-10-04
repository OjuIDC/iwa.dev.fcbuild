# Handoff Report: Milestone 5 Operator & Security Documentation Investigation

**Agent**: `explorer_m5_docs_1`  
**Role**: Milestone 5 Operator & Security Documentation Explorer  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m5_docs_1`  
**Recipient**: `parent` (`7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b`)  
**Timestamp**: 2026-10-03T21:16:00Z  
**Handoff Type**: Hard (Investigation complete, full deliverables designed and drafted)  

---

## 1. Observation

1. **Exact Disabled Message & Invariant in Code**:
   - `src/graph_api.py:419`:
     ```python
     "reason": f"Phase 1 safety invariant violation: DISPATCH_ENABLED must be false (dispatch configured but blocked: {', '.join(failed)})",
     ```
   - `src/graph_api.py:427`:
     ```python
     "reason": "Phase 1 safety invariant violation: DISPATCH_ENABLED must be false",
     ```
   - `src/graph_api.py:438-439`:
     ```python
     "dispatch": "disabled",
     "dispatch_enabled": False,
     ```
   - When `DISPATCH_ENABLED=true` is set in Phase 1, `/health/ready` returns HTTP 503 degraded with this exact reason.

2. **Activation Gates in Spec §31.2 & Code**:
   - `docs/superpowers/specs/2026-10-02-player-dispatch-design.md:2059-2067`:
     Lists `G-MIGRATION`, `G-GRANTS`, `G-WORKER`, `G-ID`, `G-ATOMIC`, `G-OUTBOX`.
   - `src/dispatch/gates.py:25`:
     ```python
     RECORDABLE_GATES: Final[frozenset[str]] = frozenset({"G-ID", "G-ATOMIC"})
     ```
   - `src/dispatch/gates.py:29-40`:
     Defines `GATE_SIGNED_FIELDS`: `gate_id`, `status`, `evidence_ref`, `code_revision`, `deployment_id`, `fc_version`, `platform`, `issued_at`, `expires_at`, `nonce`.
   - `src/graph_api.py:495-496`:
     ```python
     if gate_id not in RECORDABLE_GATES:
         raise HTTPException(409, "not_recordable_before_phase_3")
     ```
   - `tests/test_dispatch_creation.py:253-255`:
     Calling `/v1/activation-gates/G-OUTBOX/challenge` asserts HTTP 409 and `"not_recordable_before_phase_3"`.
   - `tests/test_dispatch_creation.py:258-280`:
     Gate records take effect on next evaluation without requiring a server restart (Correction C-1).

3. **EA Player Identity Verification Requirements**:
   - `docs/superpowers/specs/2026-10-02-player-dispatch-design.md:1877-1884` (AC-47):
     Specifies 5 evidence items:
     - captured, redacted `clubs/matches` and `members/stats` structures;
     - same identifier across >= 3 separate matches;
     - unchanged identifier across a gamertag change;
     - club and platform scoping;
     - handling of transferred, removed, duplicate and unavailable players.
   - `docs/superpowers/specs/2026-10-02-player-dispatch-design.md:812-815` (§13.1):
     Per-player verification endpoint has NO route in Phase 1 (arrives Phase 3). Any unverified player candidate returns HTTP 409 `not_dispatchable`. No gamertag fallback permitted.
   - `docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md:1134` (Task 9):
     Specifies document status `Status: UNVERIFIED (G-ID not met)` and one section for each AC-47 item with an empty table.

4. **Fresh Install & Database Role Bootstrap**:
   - `src/db_migrate.py:111-115`:
     ```python
     cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'fc27_owner'")
     if not cur.fetchone():
         raise MigrationError("run python -m src.db_roles bootstrap first")
     ```
   - `src/db_roles.py:202-249`:
     CLI entrypoint: `python -m src.db_roles bootstrap --database fc27 --legacy-owner fc27`. Exits 3 if pending transfers exist without `--confirm`. Exits 0 on completion with `--confirm`.
   - `docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md:1054-1062` (Task 9):
     Defines 6 Compose file secrets under `./secrets/`: `postgres_admin_uri`, `fc27_migrator_password`, `fc27_app_password`, `fc27_dispatch_password`, `fc27_migrator_uri`, `fc27_app_uri`.

5. **Human Authority & Section 1A Invariants**:
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md:967-1520` (Section 1A patch):
     - Establishes distinction between club authorization (Commissioner / human delegate) and player consent (player-specific assignments).
     - Prohibits agents from approving for humans: "No agent may approve or reject for a human."
     - Prohibits timeouts from approving: "Inactivity never constitutes approval."
     - Enforces fail-closed posture for unpermitted actor types and authority bases in Phase 1.
   - `src/graph_api.py:344-351`:
     `validation_exception_handler` strips `"input"` from validation errors to ensure zero-echo behavior.
   - `src/settings.py:90`, `src/graph_api.py:489-490`:
     `APPROVAL_CLI_HMAC_ENABLED` acts as a kill switch returning HTTP 403 `proof_type_disabled`.

6. **Planned Test Assertions on Documentation**:
   - `docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md:1110-1114`:
     ```python
     def test_docs_state_disabled_and_gates(): # DISPATCH_ACTIVATION.md lists G-MIGRATION..G-OUTBOX and the exact disabled message
     def test_activation_doc_forbids_phase1_enablement(): # C-2: DISPATCH_ACTIVATION.md contains the statements that Phase 1 must not be enabled in any deployed environment, that passing Phase 1 tests alone does not authorize activation, that there is no production dispatch-worker in Phase 1, and that G-OUTBOX cannot be satisfied before Phase 3
     ```

---

## 2. Logic Chain

1. **Deduction of `docs/DISPATCH_ACTIVATION.md` Content**:
   - From Observation 1, the system enforces `DISPATCH_ENABLED=false` at startup and readiness. The exact string `"Phase 1 safety invariant violation: DISPATCH_ENABLED must be false"` is asserted by `test_docs_state_disabled_and_gates`.
   - From Observation 2, `src/dispatch/gates.py` defines the 6 rollout gates and their evaluation rules. The gate table in `docs/DISPATCH_ACTIVATION.md` must accurately detail all 6 gates, their automatic vs manual types, and their Phase 1 states.
   - From Observation 2 and Observation 6, the document must incorporate the exact mandatory statements verbatim in substance:
     - "Phase 1 MUST NOT be enabled in any deployed environment"
     - "Passing the Phase 1 tests alone does not authorize activation"
     - "There is no production dispatch-worker in Phase 1, so G-WORKER cannot pass"
     - "G-OUTBOX cannot be satisfied before Phase 3: its evidence is the Phase 3 relay and reconciliation test run, and the route refuses it in Phase 1 (`not_recordable_before_phase_3`)"
   - From Observation 2, the signing protocol requires RFC-8785 canonical bytes over `GATE_SIGNED_FIELDS` and HMAC-SHA256, taking effect on the next evaluation cycle (C-1) and binding to deployment identity (C-4).

2. **Deduction of `docs/EA_PLAYER_IDENTITY_VERIFICATION.md` Content**:
   - From Observation 3, spec AC-47 specifies 5 explicit verification items required before G-ID can pass.
   - Because no live match captures have been recorded yet, Task 9 requires the document status to be `Status: UNVERIFIED (G-ID not met)` with empty evidence tables under each AC-47 heading.
   - Per §13.1, gamertags must be explicitly documented as untrusted presentation strings with zero fallback permission.

3. **Deduction of `MIGRATION_GUIDE.md` Content**:
   - From Observation 4, `db-migrate` strictly checks for `fc27_owner` and will fail if bootstrap has not run. Therefore, fresh install instructions must explicitly document running `docker compose --profile bootstrap run --rm db-bootstrap --confirm`.
   - From Observation 4, 6 secret files under `./secrets/` must be documented with `umask 077` and git-ignored status (C-7).
   - From Task 9, a 5-step upgrade procedure for v2.1 with backup, role bootstrap, migration, and verification must be provided.

4. **Deduction of `README.md` Content**:
   - From Observation 4, Quick Start must guide operators through creating `./secrets/`, running `db-bootstrap --confirm`, and checking `/health/ready`.
   - From Task 9 and Original Request §9, the player dispatch row must state: `"**Data foundation only (Phase 1).** Dispatch disabled; approvals record the selection only. See docs/DISPATCH_ACTIVATION.md."`
   - From Original Request §10 and §15, the persistent programme checklist (Phases 1–6) and verbatim Phase 1 completion statement must be included.

5. **Deduction of `docs/APPROVAL_SECURITY.md` Content**:
   - From Observation 5, the Section 1A authority model must be documented, emphasizing that no agent may decide for a human and that inactivity/timeout never creates approval.
   - From Observation 5, zero-echo candidate error handling, HMAC rotation, and the `APPROVAL_CLI_HMAC_ENABLED` kill switch must be documented.

---

## 3. Caveats

1. **No Production Files Modified**: In strict adherence to the explorer role and dispatch instructions, no production source code, tests, or documentation files were edited. All designs and complete drafts are stored in `.agents/teamwork/explorer_m5_docs_1/docs_analysis.md`.
2. **Pending Live Data for AC-47**: The 5 tables in `docs/EA_PLAYER_IDENTITY_VERIFICATION.md` are intentionally empty placeholders. Populating these tables requires live match data capture against the EA Pro Clubs API prior to Phase 3.
3. **Milestone 5 Compose & Test Execution**: Testing against `tests/test_phase1_scope.py` will occur once the implementer applies the drafts and creates the test files in Milestone 5.

---

## 4. Conclusion

The investigation is complete. Complete, verbatim text drafts and ready-to-apply diffs for all 5 Milestone 5 documents have been produced and validated against code, specifications, and test contracts in `docs_analysis.md`. The implementer can immediately take these drafts and write them into production documentation without architectural ambiguity or missing constraints.

---

## 5. Verification Method

To independently verify this investigation and the drafted documentation:

1. **Inspect Draft Artifacts**:
   - View `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m5_docs_1\docs_analysis.md`.
   - Check Section 3 for the complete text of `docs/DISPATCH_ACTIVATION.md`.
   - Check Section 4 for the complete text of `docs/EA_PLAYER_IDENTITY_VERIFICATION.md`.
   - Check Section 5, 6, and 7 for the diffs of `MIGRATION_GUIDE.md`, `README.md`, and `docs/APPROVAL_SECURITY.md`.

2. **Verify Against Existing Gate Tests**:
   Run the gate evaluation test suite to verify the code behavior described in the documentation:
   ```bash
   python -m pytest -q tests/test_dispatch_creation.py
   ```
   Confirm all gate tests pass.

3. **Verify Documentation String Invariants**:
   Inspect the draft in `docs_analysis.md` to confirm the presence of:
   - Exact message: `"Phase 1 safety invariant violation: DISPATCH_ENABLED must be false"`
   - Gate names: `G-MIGRATION`, `G-GRANTS`, `G-WORKER`, `G-ID`, `G-ATOMIC`, `G-OUTBOX`
   - Exact phrase: `"Status: UNVERIFIED (G-ID not met)"`
   - Exact phrase: `"Phase 1 MUST NOT be enabled in any deployed environment"`
   - Exact phrase: `"Passing the Phase 1 tests alone does not authorize activation"`
   - Exact phrase: `"There is no production dispatch-worker in Phase 1, so G-WORKER cannot pass"`
   - Exact phrase: `"G-OUTBOX cannot be satisfied before Phase 3: its evidence is the Phase 3 relay and reconciliation test run, and the route refuses it in Phase 1 (not_recordable_before_phase_3)"`
   - Exact Phase 1 completion statement: `"Secure data foundation complete. Dispatch remains disabled. No player delivery has occurred."`
