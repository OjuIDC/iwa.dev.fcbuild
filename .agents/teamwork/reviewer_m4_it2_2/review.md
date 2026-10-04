# Review Report: Milestone 4 Iteration 2 (Creation, Gates & Lifecycle Integration)

**Reviewer**: `reviewer_m4_it2_2`  
**Role**: Reviewer & Adversarial Critic (Creation & Lifecycle Integration)  
**Date**: 2026-10-03T21:06:00Z  
**Verdict**: **APPROVE**  

---

## 1. Executive Summary

Milestone 4 implementation integrates activation rollout gates, cryptographic gate recording, and atomic dispatch creation into the authoritative PostgreSQL approval lifecycle. The previous contract regression reported by Challenger 1 on `/health/ready` (where the `reason` string omitted `"DISPATCH_ENABLED must be false"`) was cleanly remediated by `worker_m4_2` at `src/graph_api.py:419`.

Comprehensive static analysis, control flow tracing, schema verification, and adversarial stress-testing confirm:
1. `src/dispatch/gates.py` implements complete 6-gate evaluation (`G-MIGRATION`, `G-GRANTS`, `G-WORKER`, `G-ID`, `G-ATOMIC`, `G-OUTBOX`), enforces `RECORDABLE_GATES = frozenset({"G-ID", "G-ATOMIC"})`, rejects `G-OUTBOX` challenge/record with HTTP 409 in Phase 1, excludes `X-Correlation-Id` from HMAC signatures, and guarantees `dispatch_effective()` returns `False` in Phase 1 production.
2. `src/dispatch/creation.py` and `src/approval_store.py` enforce strict candidate objective bounds via `fc27-objective-bounds-v1`, validate player regex `^[0-9]{1,20}$` and active/verified status in `public.players`, serialize concurrent partition operations using `pg_advisory_xact_lock(hashtextextended(key, 0))`, supersede existing dispatches before inserting new pending dispatches, roll back the entire transaction if dispatch creation fails, and provide safe idempotent replay returning the winner `dispatch_id`.
3. All 24 test functions in `tests/test_dispatch_creation.py` are structurally sound, rigorous, and accurately assert the specified invariants.
4. No integrity violations, dummy logic, facade methods, or hardcoded test shortcuts were identified.

---

## 2. Review Dimensions & Detailed Findings

### 2.1 Activation Gates Evaluation & Recording (`src/dispatch/gates.py`, `src/graph_api.py`)
- **`RECORDABLE_GATES`**: Defined as `frozenset({"G-ID", "G-ATOMIC"})` (line 25). `src/graph_api.py` checks `if gate_id not in RECORDABLE_GATES: raise HTTPException(409, "not_recordable_before_phase_3")` for both challenge and record endpoints. This guarantees `G-OUTBOX` is rejected with 409 in Phase 1 (C-3).
- **`GATE_SIGNED_FIELDS`**: Exactly covers `("gate_id", "status", "evidence_ref", "code_revision", "deployment_id", "fc_version", "platform", "issued_at", "expires_at", "nonce")`. `X-Correlation-Id` is excluded, ensuring transport tracing headers do not invalidate HMAC signatures (C-4, C-5).
- **Gate Evaluation Sequence**: `evaluate_gates()` strictly evaluates:
  1. `G-MIGRATION`: verifies migration 0003 in `schema_migrations`.
  2. `G-GRANTS`: verifies `fc27_dispatch` role, absence of table DML privileges, EXECUTE grant on `fc27_dispatch_transition`, function ownership by `fc27_owner`, and `fc27_app` non-membership in `fc27_owner`.
  3. `G-WORKER`: checks `service_heartbeats` table and ensures `dispatch-worker` heartbeat age is `< 60.0s`.
  4. `G-ID`, `G-ATOMIC`, `G-OUTBOX`: checks deployment identity validity, existence of `activation_gate_log`, and latest status for `(gate_id, code_revision, deployment_id, fc_version, platform)` is `'passed'`.
- **`dispatch_effective()`**: Returns `True` iff `configured` is `True` and `all(r.passed for r in results)`. In Phase 1 production, `settings.dispatch_enabled` is always `False`. Furthermore, because `G-OUTBOX` cannot be recorded, `all(r.passed)` cannot be satisfied in Phase 1.

### 2.2 Atomic Dispatch Creation & Approval Store Integration (`src/dispatch/creation.py`, `src/approval_store.py`)
- **Candidate Objective Bounds**:
  - `candidate.get("objective")` validated against Pydantic model `Objective`.
  - Enforces `1 <= min_matches <= max_matches <= 10`.
  - `validate_metric_bounds()` enforces `METRIC_BOUNDS["fc27-objective-bounds-v1"]`:
    - `pass_accuracy`, `tackle_success`, `shot_accuracy`: `[1.0, 25.0]`
    - `rating`: `[0.1, 2.0]`
    - `goals`, `assists`: `[1.0, 5.0]`
  - Missing or invalid objective raises `NotDispatchable`.
- **Player Validation**:
  - Regex enforcement: `_PLAYER_ID_RE = re.compile(r"^[0-9]{1,20}$")`. Alphanumeric gamertags rejected with `NotDispatchable`.
  - Queries `public.players`: checks player existence, exact string equality (`p_row["player_id"] == cand_player_id`, preventing `'7'` vs `'007'` mismatches), `active=True`, `status='active'`, non-null `identity_verified_at`, and club ID alignment with thread club.
- **Partition Advisory Locking**:
  - Computes `lock_key = f"{cand_player_id}|{intent.owner.value}|{objective.metric}"`.
  - Executes `pg_advisory_xact_lock(hashtextextended(%s, 0))`. Automatically released at transaction termination.
- **Atomic Supersession**:
  - Queries existing active dispatches for `(player_id, owner, metric)` with `FOR UPDATE`.
  - Executes `transition(..., to_status="superseded", patch={"superseded_by": str(new_dispatch_id)})` via `fc27_dispatch_transition`.
  - Inserts new dispatch row with `dispatch_status='pending'`, version 1, plan_version 0.
  - Emits `dispatch.created` domain event and outbox row.
- **Single-Transaction Rollback on `NotDispatchable`**:
  - `apply_decision` executes dispatch creation inside `with self.connect() as conn, conn.transaction():`.
  - Catching `NotDispatchable` converts it to `StoreError(409)`. Psycopg rolls back the transaction. Nonce consumption, thread/request status updates, and audit inserts are fully rolled back.
- **Safe Idempotent Replay**:
  - Replays of consumed nonces verify complete identity against `approval_audit`.
  - Queries `SELECT dispatch_id FROM public.dispatches WHERE approval_id = %s`.
  - Returns winner `dispatch_id` with HTTP 200 OK without creating duplicate rows or conflicting transitions.

### 2.3 Remediation Verification (`src/graph_api.py:419`)
- `worker_m4_2` updated `src/graph_api.py:419` to:
  `"reason": f"Phase 1 safety invariant violation: DISPATCH_ENABLED must be false (dispatch configured but blocked: {', '.join(failed)})"`
- Verified string satisfies:
  - `"DISPATCH_ENABLED must be false"` required by `tests/test_graph_api_v1.py` lines 320 and 656, and `tests/test_langgraph_flow.py` line 42.
  - `"blocked"` and failed gate listing (e.g. `"G-WORKER"`) required by `tests/test_dispatch_creation.py:67`.

### 2.4 Test Suite Soundness (`tests/test_dispatch_creation.py`)
- Contains 24 distinct, comprehensive test functions:
  1. `test_gates_block_by_default`
  2. `test_configured_but_blocked_behaves_disabled`
  3. `test_gate_recording_requires_commissioner_and_signature`
  4. `test_gate_nonce_replay_rejected`
  5. `test_gate_nonce_expired_rejected`
  6. `test_gate_tampered_field_rejected`
  7. `test_gate_nonce_bound_to_gate`
  8. `test_gate_requires_deployment_identity`
  9. `test_gate_record_from_other_deployment_ignored`
  10. `test_gate_signature_excludes_correlation_id`
  11. `test_g_outbox_not_recordable_in_phase1`
  12. `test_gate_change_applies_on_next_evaluation`
  13. `test_correlation_id_shared_by_dispatch_and_events`
  14. `test_approve_creates_dispatch_atomically`
  15. `test_dispatch_failure_rolls_back_decision`
  16. `test_not_dispatchable_without_objective`
  17. `test_not_dispatchable_gamertag_player`
  18. `test_not_dispatchable_unverified_player`
  19. `test_player_id_is_compared_exactly`
  20. `test_not_dispatchable_unknown_player`
  21. `test_reject_creates_no_dispatch`
  22. `test_supersede_is_atomic`
  23. `test_concurrent_supersede_is_serialized`
  24. `test_parallel_duplicate_approvals_single_dispatch`
- Tests thoroughly cover happy path, adversarial tamper attempts, concurrent executions, and negative boundaries.

---

## 3. Adversarial Critic Challenge Analysis

| Challenge Dimension | Scenario / Attack Vector | Defense / Code Behavior | Result |
|---|---|---|---|
| **Partition Lock Collisions** | Hash collisions on 64-bit advisory lock key | `hashtextextended(key, 0)` generates 64-bit hash. Negligible collision probability ($p \approx 10^{-18}$); even on collision, transaction-scoped lock serializes rather than deadlocks or corrupts. | PASS |
| **Tampered Replay Attack** | Attacker replays consumed nonce with modified payload or different player ID | Strict byte-level equality check in `apply_decision` (lines 456–465) against `approval_audit` fields; mismatch raises 409 conflict. | PASS |
| **Leading Zero Gamertag Bypass** | Numeric string `'007'` sent when player is `'7'` | Line 85 of `creation.py` enforces exact string match `p_row["player_id"] == cand_player_id`; rejects `'007'` when DB has `'7'`. | PASS |
| **Partial Rollback Exploitation** | DB trigger fails dispatches insert; thread left in inconsistent state | Entire decision, nonce consumption, request update, and audit insert wrapped in single transactional context; complete rollback verified. | PASS |
| **G-OUTBOX Premature Activation** | Attacker requests challenge for G-OUTBOX | Endpoint checks `gate_id not in RECORDABLE_GATES`, returns 409 `not_recordable_before_phase_3`. | PASS |

---

## 4. Integrity Violation Assessment

- **Hardcoded test outputs**: None. Database queries, schema migrations, and role posture are dynamically checked.
- **Dummy/facade logic**: None. Full transactional and state machine logic implemented.
- **Shortcuts/bypasses**: None. Advisory locks, security definer transitions, and immutable audit trails are fully integrated.
- **Fabricated claims**: None.

**Verdict**: **APPROVE**
