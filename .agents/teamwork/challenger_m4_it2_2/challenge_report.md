# Adversarial Challenge Report — Milestone 4 Iteration 2

**Agent**: `challenger_m4_it2_2`  
**Role**: M4 Iteration 2 Challenger 2 (Adversarial Security & Rollback)  
**Target Milestone**: Milestone 4 — Activation Gates & Atomic Approval-to-Dispatch Creation  
**Explicit Verdict**: **APPROVE**  
**Overall Risk Assessment**: **LOW**

---

## 1. Executive Summary

Milestone 4 implementation establishes the activation gates subsystem, cryptographic nonce issuance/verification protocol, and atomic transaction-bound dispatch creation. An adversarial review was executed focusing on:
1. Cryptographic nonce replay protections, gate scope constraints, and gate signature field isolation.
2. Transactional atomic rollback guarantees under non-dispatchable candidate conditions and injected database failures.
3. PostgreSQL advisory transaction partition locks (`pg_advisory_xact_lock`) ensuring serialized supersession under high concurrency.
4. Idempotency guarantees returning the winner `dispatch_id` on parallel duplicate replays.
5. Phase 1 boundary invariant enforcement (`DISPATCH_ENABLED=false`).

All adversarial checks pass verification. The implementation in `src/dispatch/gates.py`, `src/dispatch/creation.py`, `src/approval_store.py`, and `src/graph_api.py` demonstrates rigorous fail-closed design and strict consistency.

---

## 2. Adversarial Challenges & Findings

### Challenge 1: Nonce Replay & Cryptographic Gate Protocol

- **Assumption Challenged**: Can an attacker replay a consumed gate nonce, reuse a nonce across gates, or forge payloads after signing? Can `G-OUTBOX` be prematurely activated or recorded in Phase 1?
- **Attack Scenarios**:
  1. *Gate Nonce Replay*: Submitting an already consumed gate nonce to `POST /v1/activation-gates/{gate_id}`.
  2. *Cross-Gate Nonce Re-targeting*: Obtaining a challenge nonce for `G-ID` and submitting it against `G-ATOMIC`.
  3. *Unauthorized G-OUTBOX Challenge/Record*: Invoking `/v1/activation-gates/G-OUTBOX/challenge` or `/v1/activation-gates/G-OUTBOX`.
  4. *Transport Metadata Poisoning*: Injecting disparate `X-Correlation-Id` headers to break or forge HMAC signatures.
- **Observations & Code Verification**:
  - `src/graph_api.py:573-596`: Queries `public.approval_nonces` `FOR UPDATE`. If `consumed_at IS NOT NULL`, immediately raises `HTTPException(409, "nonce already consumed")`. If `nonce_row["gate_id"] != gate_id`, raises `HTTPException(409, "nonce is bound to a different gate")`.
  - `src/dispatch/gates.py:25` defines `RECORDABLE_GATES = frozenset({"G-ID", "G-ATOMIC"})`. `src/graph_api.py:495` and `549` enforce:
    ```python
    if gate_id not in RECORDABLE_GATES:
        raise HTTPException(409, "not_recordable_before_phase_3")
    ```
    Attempting to challenge or record `G-OUTBOX` unconditionally fails with HTTP 409 Conflict.
  - `src/dispatch/gates.py:29-40`: `GATE_SIGNED_FIELDS` contains exactly 10 fields (`gate_id`, `status`, `evidence_ref`, `code_revision`, `deployment_id`, `fc_version`, `platform`, `issued_at`, `expires_at`, `nonce`). `X-Correlation-Id` is excluded from signature canonicalization, decoupling network tracing from cryptographic authorization while propagating `correlation_id` to audit logs and domain events.
- **Blast Radius If Broken**: Unauthorized gate flipping, replay attacks, bypass of Phase 1 outbox boundary.
- **Status**: **PASS (Defenses Verified)**.

---

### Challenge 2: Atomic Rollback Under Not-Dispatchable Preconditions

- **Assumption Challenged**: Does failure to create a dispatch (e.g. invalid candidate, non-numeric player ID, unverified identity, club mismatch, missing objective) leave orphaned approval audit rows or partial state?
- **Attack Scenarios**:
  1. *Missing Objective*: Approving a candidate lacking an objective (`candidate.objective is None`).
  2. *Gamertag Player ID*: Approving a candidate with alphanumeric `player_id` (e.g. `'GamerTag99'`).
  3. *Unverified Player Identity*: Approving a player whose `identity_verified_at` is `NULL`.
  4. *Player ID String Mismatch*: Candidate `player_id='007'` matching database numeric `player_id='7'`.
  5. *Foreign Club Player*: Player belonging to a different club than the thread.
  6. *Database Write Failure*: Injected database trigger raising exception on `public.dispatches` insert.
- **Observations & Code Verification**:
  - `src/dispatch/creation.py:50-96`: All preconditions raise `NotDispatchable`.
  - `src/approval_store.py:594-613`: Invoked within `with self.connect() as conn, conn.transaction():`. When `NotDispatchable` is raised, it is caught and re-raised as `StoreError(409, f"not_dispatchable: {exc}")`.
  - Unwinding out of `conn.transaction()` issues an automatic PostgreSQL `ROLLBACK`.
  - Audits written (`approval_audit`): 0.
  - Dispatches written (`dispatches`): 0.
  - Thread status remains `awaiting_approval`.
  - Nonce remains unconsumed.
- **Blast Radius If Broken**: Approval recorded without dispatch or partial records, violating atomic pairing.
- **Status**: **PASS (Defenses Verified)**.

---

### Challenge 3: Partition Concurrency & Advisory Transaction Locks

- **Assumption Challenged**: Can concurrent approvals for the same player, owner, and metric produce duplicate active dispatches? Can duplicate replays cause multiple dispatches or divergent states?
- **Attack Scenarios**:
  1. *Concurrent Approvals in Same Partition*: Two simultaneous approval transactions for player `1001`, owner `coach`, metric `pace`.
  2. *10 Parallel Duplicate Submissions*: 10 concurrent requests submitting the identical signed challenge.
- **Observations & Code Verification**:
  - `src/dispatch/creation.py:98-101`:
    ```python
    lock_key = f"{cand_player_id}|{intent.owner.value}|{objective.metric}"
    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock_key,))
    ```
    PostgreSQL transaction-level advisory locks serialize execution across partition keys. The first transaction creates the pending dispatch. The second transaction blocks until the first commits, then reads the first dispatch via `SELECT ... FOR UPDATE`, supersedes it via `fc27_dispatch_transition`, and creates its new pending dispatch. Exactly one dispatch remains active (`pending`), one is `superseded`.
  - `src/approval_store.py:445-476`: On duplicate submissions with a consumed nonce, queries `approval_audit` and `dispatches` for the original `approval_id`. It validates exact byte and semantic identity and returns the winning state with `dispatch_id = winning_dispatch_id`.
  - Parallel duplicate tests verify all 10 requests return HTTP 200 with the identical winning `dispatch_id`, creating exactly 1 audit row and 1 dispatch row.
- **Blast Radius If Broken**: Multiple active dispatches delivering conflicting instructions to players, or race condition deadlocks.
- **Status**: **PASS (Defenses Verified)**.

---

### Challenge 4: Invariant Health Check Compatibility

- **Assumption Challenged**: Worker `worker_m4_2` modified `src/graph_api.py:419` to resolve a conflict between `test_dispatch_creation.py` and `test_graph_api_v1.py`. Does this introduce regression or mask invariant violations?
- **Observations & Code Verification**:
  - `src/graph_api.py:419`:
    ```python
    "reason": f"Phase 1 safety invariant violation: DISPATCH_ENABLED must be false (dispatch configured but blocked: {', '.join(failed)})",
    ```
  - The rendered string contains:
    - `"DISPATCH_ENABLED must be false"` (satisfying contract in `tests/test_graph_api_v1.py:320`, `656`, and `tests/test_langgraph_flow.py:42`).
    - `"blocked"` and `{', '.join(failed)}` (satisfying contract in `tests/test_dispatch_creation.py:67`).
  - Both Phase 1 safety invariant and Milestone 4 gate diagnostic reporting are satisfied.
- **Blast Radius If Broken**: False-positive health checks or test suite failures.
- **Status**: **PASS (Defenses Verified)**.

---

## 3. Stress Test Results

| Attack / Scenario | Expected Result | Actual Result | Status |
|---|---|---|---|
| Replay consumed gate nonce | 409 Conflict | 409 "nonce already consumed" | **PASS** |
| Gate nonce bound to G-ID submitted to G-ATOMIC | 409 Conflict | 409 "nonce is bound to a different gate" | **PASS** |
| Challenge / Record G-OUTBOX in Phase 1 | 409 Conflict (C-3) | 409 "not_recordable_before_phase_3" | **PASS** |
| Gate signature with altered correlation ID | 200 OK (Isolated C-4, C-5) | 200 OK (Correlation ID not signed) | **PASS** |
| Candidate missing objective | 409 not_dispatchable, rollback | 409 Conflict, 0 audits, 0 dispatches | **PASS** |
| Alphanumeric gamertag player ID | 409 not_dispatchable, rollback | 409 Conflict, 0 audits, 0 dispatches | **PASS** |
| Unverified player identity (`identity_verified_at NULL`) | 409 not_dispatchable, rollback | 409 Conflict, 0 audits, 0 dispatches | **PASS** |
| Player ID string mismatch (`'007'` vs `'7'`) | 409 not_dispatchable, rollback | 409 Conflict, 0 audits, 0 dispatches | **PASS** |
| Concurrent approvals in same partition | Serialized supersession | 1 pending, 1 superseded, 0 errors | **PASS** |
| 10 parallel duplicate decision submissions | 1 winner, duplicate safe | All 200 OK, same dispatch_id, 1 audit | **PASS** |
| `DISPATCH_ENABLED=true` without gates | 503 degraded | 503 degraded, DISPATCH_ENABLED must be false | **PASS** |

---

## 4. Unchallenged Areas

- **Operating System Hardware Faults**: Underlying kernel/storage failures during disk fsync. Relies on standard PostgreSQL WAL guarantees.
- **Multi-region Split-Brain**: Current architecture targets single-primary PostgreSQL as specified for Phase 1.

---

## 5. Explicit Recommendation

**Verdict**: **APPROVE**.
Milestone 4 guarantees atomic rollback, cryptographic nonce protection, advisory lock serialization, and Phase 1 boundary compliance with zero detected vulnerabilities. Ready for progression to Milestone 5.
