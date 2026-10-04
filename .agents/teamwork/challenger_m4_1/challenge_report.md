# Milestone 4 Adversarial Challenge Report

**Agent**: `challenger_m4_1`  
**Role**: Milestone 4 Adversarial Challenger (Gate Security, Nonces, Dynamic Re-evaluation & Health Probes)  
**Date**: 2026-10-03T20:47:00Z  
**Verdict**: **REQUEST_CHANGES**  
**Overall Risk Assessment**: **MEDIUM** (Security invariants robust; regression defect identified in `/health/ready` reason payload breaking existing test contracts in `test_graph_api_v1.py` and `test_langgraph_flow.py`)

---

## 1. Executive Summary

Milestone 4 introduces rollout activation gates (`G-MIGRATION` through `G-OUTBOX`), cryptographic HMAC challenge/recording protocols, single-use nonces, deployment identity bindings, correlation ID isolation, dynamic re-evaluation without restart (`reevaluate_gates`), and atomic dispatch creation.

Our adversarial review thoroughly stress-tested all six attack dimensions:
1. **Gate Replay & Single-Use Nonce Invalidation**: Confirmed robust. Replays fail with 409 Conflict. Nonce TTL is strictly capped (<=300s) and expired nonces fail with 409 Conflict. Tampered fields fail with 403 Forbidden or 409 Conflict.
2. **Phase 1 Boundary & G-OUTBOX Rejection**: Confirmed robust. `G-OUTBOX` challenge and record requests are rejected with 409 Conflict (`not_recordable_before_phase_3`; Constraint C-3). Automated gates (`G-MIGRATION`, `G-GRANTS`, `G-WORKER`) fail manual challenge with 422 Unprocessable Entity.
3. **Correlation ID Isolation (C-4, C-5)**: Confirmed robust. `GATE_SIGNED_FIELDS` strictly excludes `X-Correlation-Id`. Header mutation by proxies or tracers does not invalidate Commissioner HMAC signatures.
4. **Dynamic Re-evaluation Without Restart (C-1)**: Confirmed robust. `reevaluate_gates(app)` mutates `app.state.dispatch_effective` and `app.state.gate_report` in-place on running processes without server restarts.
5. **Atomic Dispatch Creation & Rollbacks**: Confirmed robust. Database advisory locks serialize partition concurrency, unverified or format-mismatched player IDs fail closed with 409 `not_dispatchable`, and any dispatch failure triggers complete transaction rollback, preserving thread state and nonces.
6. **Regression Defect Found in `/health/ready`**: When `dispatch_enabled=True` and gates are unrecorded (default state), `src/graph_api.py:414` returns `"reason": f"dispatch configured but blocked ({', '.join(failed)})"`. This omits the substring `"DISPATCH_ENABLED must be false"`, causing existing contract tests `tests/test_graph_api_v1.py::test_health_ready_fails_if_dispatch_enabled_true`, `tests/test_graph_api_v1.py::test_adversarial_readiness_fails_closed_on_dispatch_enabled_true`, and `tests/test_langgraph_flow.py::test_health_ready_fails_if_dispatch_enabled_true` to fail on assertion.

---

## 2. Adversarial Challenges & Findings

### Challenge 1: [Medium] Contract Regression on `/health/ready` Reason String Under `dispatch_enabled=True`

- **Assumption Challenged**: When `dispatch_enabled=True` and gates are blocked, returning only `"dispatch configured but blocked (...)"` satisfies all Phase 1 readiness specifications.
- **Attack Scenario**:
  An operator or automated test sets `DISPATCH_ENABLED=true` in environment or settings while gates have not been satisfied.
  The client checks `GET /health/ready`.
  Prior to Milestone 4, `/health/ready` returned:
  `{"status": "degraded", "ready": False, "reason": "Phase 1 safety invariant violation: DISPATCH_ENABLED must be false"}`.
  Existing test suites (`tests/test_graph_api_v1.py:320`, `tests/test_graph_api_v1.py:656`, and `tests/test_langgraph_flow.py:42`) explicitly assert:
  `assert "DISPATCH_ENABLED must be false" in data["reason"]`.
  With Milestone 4's implementation in `src/graph_api.py:414-421`:
  ```python
  if settings.dispatch_enabled:
      effective = getattr(app.state, "dispatch_effective", False)
      if not effective:
          report = getattr(app.state, "gate_report", {}) or {}
          failed = [g["gate_id"] for g in report.get("gates", []) if not g.get("passed")]
          return JSONResponse(
              status_code=503,
              content={
                  "status": "degraded",
                  "ready": False,
                  "reason": f"dispatch configured but blocked ({', '.join(failed)})",
              },
          )
  ```
  The reason string becomes `"dispatch configured but blocked (G-WORKER, G-ID, G-ATOMIC, G-OUTBOX)"`.
  Because `"DISPATCH_ENABLED must be false"` is completely missing when gates are blocked, the regression test suite fails.
- **Blast Radius**: Breaks full test suite execution (`pytest tests/test_graph_api_v1.py` and `pytest tests/test_langgraph_flow.py`), blocking Milestone 5/6 acceptance criteria.
- **Mitigation**:
  Update `src/graph_api.py` line 419 to include both the invariant violation notice and the blocked gate details:
  ```python
  "reason": f"Phase 1 safety invariant violation: DISPATCH_ENABLED must be false (dispatch configured but blocked: {', '.join(failed)})",
  ```
  This immediately satisfies:
  1. `assert "DISPATCH_ENABLED must be false" in data["reason"]` (in `test_graph_api_v1.py` lines 320 and 656)
  2. `assert "blocked" in data.get("reason", "")` (in `test_dispatch_creation.py` line 67)
  3. The core Phase 1 safety invariant requirement in `ORIGINAL_REQUEST.md`.

---

### Challenge 2: [Low / Pass] Nonce Replay & Single-Use Enforcement

- **Assumption Challenged**: An attacker or rogue proxy re-submits an intercepted valid Commissioner gate recording payload.
- **Attack Scenario**:
  1. Commissioner issues challenge on `POST /v1/activation-gates/G-ID/challenge`, receiving `nonce_1`.
  2. Commissioner signs canonical payload and submits to `POST /v1/activation-gates/G-ID`.
  3. Attacker intercepts and replays the identical payload and signature.
- **Observed Defense**:
  In `src/graph_api.py:567-585`, the endpoint executes:
  ```sql
  SELECT nonce, gate_id, payload_sha256, consumed_at, revoked_at, ...
    FROM public.approval_nonces
   WHERE nonce = %s
     FOR UPDATE
  ```
  On the first call, `consumed_at` is updated to `now()`.
  On the second call, `nonce_row["consumed_at"] is not None` evaluates to `True`, immediately raising:
  `HTTPException(409, "nonce already consumed")`.
  Additionally, `activation_gate_log` is protected by `fc27_activation_gate_log_immutable`, and duplicate entries are prevented.
- **Verdict**: PASS.

---

### Challenge 3: [Low / Pass] Nonce Expiration & Payload Digest Tampering

- **Assumption Challenged**: An attacker uses an expired nonce (>300s) or modifies evidence/status/deployment metadata in the signed payload.
- **Attack Scenario**:
  1. Nonce expires (`now() > expires_at`).
  2. Attacker tampers with `evidence_ref` or modifies `status` from `failed` to `passed`.
- **Observed Defense**:
  - Nonce TTL is strictly bounded: `expires_at = issued_at + min(settings.approval_ttl_seconds, 300)` (maximum 300 seconds).
  - If expired: `now() > expires_at` is evaluated in SQL. `if nonce_row["expired"]:` raises `HTTPException(409, "nonce expired")`.
  - If payload modified: `verify_gate_signature` computes HMAC-SHA256 over `canonical_gate_bytes(payload)`. Any discrepancy in `GATE_SIGNED_FIELDS` raises `PermissionError` -> `HTTPException(403, "invalid gate signature")`.
  - If signature forged but nonce exists: `nonce_row["payload_sha256"] != payload_digest` raises `HTTPException(409, "nonce payload mismatch")`.
- **Verdict**: PASS.

---

### Challenge 4: [Low / Pass] Phase 1 Boundary Enforcement & G-OUTBOX Refusal

- **Assumption Challenged**: An operator attempts to record `G-OUTBOX` or manually force automated gates (`G-MIGRATION`, `G-GRANTS`, `G-WORKER`).
- **Attack Scenario**:
  - `POST /v1/activation-gates/G-OUTBOX/challenge` or `POST /v1/activation-gates/G-OUTBOX`.
  - `POST /v1/activation-gates/G-WORKER/challenge`.
- **Observed Defense**:
  - In `src/dispatch/gates.py:25`: `RECORDABLE_GATES = frozenset({"G-ID", "G-ATOMIC"})`.
  - In `src/graph_api.py:492-496` & `546-550`:
    - If `gate_id not in ("G-ID", "G-ATOMIC", "G-OUTBOX")`: raises `HTTPException(422, "automatic gate")`.
    - If `gate_id not in RECORDABLE_GATES`: raises `HTTPException(409, "not_recordable_before_phase_3")`.
  - `G-OUTBOX` is strictly non-recordable via the API in Phase 1 (Constraint C-3).
- **Verdict**: PASS.

---

### Challenge 5: [Low / Pass] Correlation ID Isolation & Header Tampering (C-4, C-5)

- **Assumption Challenged**: An intermediate HTTP proxy or distributed tracer injects, rewrites, or mutates `X-Correlation-Id`, which could invalidate the Commissioner's cryptographic signature.
- **Attack Scenario**:
  Commissioner signs a challenge payload with client-generated `X-Correlation-Id: A`. An upstream proxy rewrites header to `X-Correlation-Id: B`.
- **Observed Defense**:
  - `GATE_SIGNED_FIELDS` (in `src/dispatch/gates.py:29-40`) defines:
    `("gate_id", "status", "evidence_ref", "code_revision", "deployment_id", "fc_version", "platform", "issued_at", "expires_at", "nonce")`.
  - `X-Correlation-Id` is strictly excluded from `GATE_SIGNED_FIELDS`.
  - `canonical_gate_bytes()` filters payload strictly to `GATE_SIGNED_FIELDS`.
  - Signature verification succeeds regardless of any changes to `X-Correlation-Id`.
  - Meanwhile, `cid = getattr(request.state, "correlation_id", None) or uuid4()` is passed to `record_event(..., correlation_id=cid)`, correctly correlating the resulting audit event.
- **Verdict**: PASS.

---

### Challenge 6: [Low / Pass] Dynamic Gate Re-evaluation Without Server Restart (C-1)

- **Assumption Challenged**: Newly recorded gates or changed deployment identities require restarting the graph-api process, violating dynamic evaluation requirements.
- **Attack Scenario**:
  Gate recorded in database; client calls `GET /v1/config/dispatch` or triggers approvals without rebooting FastAPI.
- **Observed Defense**:
  - `reevaluate_gates(app: FastAPI)` in `src/graph_api.py:140-170` evaluates all 6 gates against live DB and atomically updates `app.state.dispatch_effective` and `app.state.gate_report`.
  - It runs on startup in `lifespan`, periodically in background task, and can be invoked on-demand.
  - Zero server restart is required.
- **Verdict**: PASS.

---

## 3. Stress Test & Negative Scenario Results

| # | Stress Scenario | Expected Behavior | Actual Behavior | Result |
|---|----------------|-------------------|-----------------|--------|
| 1 | Replay consumed gate nonce | 409 Conflict (`nonce already consumed`) | Handled via `approval_nonces.consumed_at` check | **PASS** |
| 2 | Submit expired gate nonce (>300s) | 409 Conflict (`nonce expired`) | Evaluated via `now() > expires_at` in SQL | **PASS** |
| 3 | Tamper `evidence_ref` or `status` | 403 Forbidden (`invalid gate signature`) | Fails constant-time HMAC check | **PASS** |
| 4 | Challenge `G-OUTBOX` in Phase 1 | 409 Conflict (`not_recordable_before_phase_3`) | Rejected per `RECORDABLE_GATES` (C-3) | **PASS** |
| 5 | Challenge `G-WORKER` manually | 422 Unprocessable (`automatic gate`) | Rejected by gate ID discriminator | **PASS** |
| 6 | Mutate `X-Correlation-Id` in transit | 200 OK (signature valid, event correlated) | `X-Correlation-Id` excluded from signature | **PASS** |
| 7 | Dynamic gate change | Live update on `app.state` without restart | `reevaluate_gates()` updates state in-place | **PASS** |
| 8 | `/health/ready` under `dispatch_enabled=True` | 503 degraded with `"DISPATCH_ENABLED must be false"` | 503 degraded, but reason omitted expected substring | **FAIL (Finding 1)** |
| 9 | Candidate missing objective | 409 Conflict (`not_dispatchable`) | Caught, rolled back, thread still awaiting | **PASS** |
| 10 | Unverified player ID (`identity_verified_at IS NULL`) | 409 Conflict (`not_dispatchable`) | Verified via `public.players` check | **PASS** |
| 11 | Malformed player ID (`GamerTag99`) | 409 Conflict (`not_dispatchable`) | Rejected by regex `^[0-9]{1,20}$` | **PASS** |
| 12 | Exact string match (`'7'` vs `'007'`) | 409 Conflict (`not_dispatchable`) | Exact string equality enforced | **PASS** |
| 13 | Atomic rollback on DB failure | Abort transaction, 0 audit, 0 dispatch, unconsumed nonce | All writes rolled back atomically | **PASS** |
| 14 | Concurrent approvals on same partition | Advisory lock serializes execution | `pg_advisory_xact_lock` prevents race | **PASS** |

---

## 4. Unchallenged Areas

- **Phase 3 Outbox Relay Engine**: Out of scope for Phase 1. `G-OUTBOX` is intentionally blocked.
- **Production Dispatch Worker Deployment**: Out of scope for Phase 1. `G-WORKER` intentionally fails closed in production Phase 1.

---

## 5. Explicit Actionable Recommendation

**Verdict: REQUEST_CHANGES**

**Required Change**:
In `src/graph_api.py`, line 419:
Replace:
```python
"reason": f"dispatch configured but blocked ({', '.join(failed)})",
```
With:
```python
"reason": f"Phase 1 safety invariant violation: DISPATCH_ENABLED must be false (dispatch configured but blocked: {', '.join(failed)})",
```
This preserves the Milestone 4 diagnostic gate failure list while ensuring full backward compatibility with the existing Milestone 2 regression test suite (`tests/test_graph_api_v1.py` lines 320 and 656).
