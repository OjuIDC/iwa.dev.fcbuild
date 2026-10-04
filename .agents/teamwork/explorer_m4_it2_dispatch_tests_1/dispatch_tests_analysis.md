# Milestone 4 Dispatch Creation Tests Analysis Report

**Agent**: `explorer_m4_it2_dispatch_tests_1`  
**Role**: Milestone 4 Iteration 2 Dispatch Creation Tests Explorer  
**Date**: 2026-10-03T20:54:00Z  
**Target File**: `tests/test_dispatch_creation.py` & `src/graph_api.py`  
**Verdict**: **FULLY COMPATIBLE — NO TEST CHANGES NEEDED IN `tests/test_dispatch_creation.py`**

---

## 1. Executive Summary

Milestone 4 introduced dynamic rollout activation gates (`G-MIGRATION` through `G-OUTBOX`) and an updated `/health/ready` probe in `src/graph_api.py`.
An adversarial challenge by `challenger_m4_1` identified a contract regression where `/health/ready` returned `"dispatch configured but blocked (...)"` when `settings.dispatch_enabled = True` and gates failed, omitting the critical Phase 1 invariant string `"DISPATCH_ENABLED must be false"`. This broke existing contract assertions in `tests/test_graph_api_v1.py` (lines 320 and 656) and `tests/test_langgraph_flow.py` (line 42).

The proposed fix in `src/graph_api.py` line 419:
```python
"reason": f"Phase 1 safety invariant violation: DISPATCH_ENABLED must be false (dispatch configured but blocked: {', '.join(failed)})",
```
was analyzed against all 24 tests in `tests/test_dispatch_creation.py`.

### Core Findings:
1. **Line 67 Verification**: In `tests/test_dispatch_creation.py`, line 67 (`test_configured_but_blocked_behaves_disabled`) asserts:
   ```python
   assert "blocked" in data.get("reason", "") or "G-WORKER" in data.get("reason", "")
   ```
   Under the proposed payload, `data["reason"]` contains BOTH `"blocked"` (from `"dispatch configured but blocked: ..."`) AND `"G-WORKER"` (from `{', '.join(failed)}`). The assertion evaluates to `True or True == True`.
2. **Zero Blast Radius on Other 23 Tests**: An exhaustive audit of all other 23 tests in `tests/test_dispatch_creation.py` revealed that **none** of them call `/health/ready` or inspect gate reason payloads. Tests utilizing the `enabled_client` fixture rely on synthetic gate records where `dispatch_effective` is `True` and test atomic dispatch persistence, objective validation, and advisory locks.
3. **Full System Convergence**: Applying this update resolves the regression in `tests/test_graph_api_v1.py` and `tests/test_langgraph_flow.py` while ensuring 100% of the 24 tests in `tests/test_dispatch_creation.py` continue to pass without requiring any modifications to test files.

---

## 2. Line 67 Inspection & Assertion Analysis

### Location & Context
- **File**: `tests/test_dispatch_creation.py`
- **Function**: `test_configured_but_blocked_behaves_disabled(pg_uri)` (lines 57–76)
  *(Note: Identified in dispatch instructions as `test_health_ready_probe_degraded_when_dispatch_enabled_and_gates_fail`; the actual function name in the codebase is `test_configured_but_blocked_behaves_disabled`)*.

```python
57: def test_configured_but_blocked_behaves_disabled(pg_uri):
58:     """AC-45: dispatch_enabled=True settings, no gate records -> no dispatch, health blocked."""
59:     app_settings = settings(pg_uri, dispatch_enabled=True, code_revision="rev1", deployment_id="dep1")
60:     app = create_app(app_settings, pipeline=StubPipeline(packages=fake_packages_dispatchable()))
61: 
62:     with TestClient(app) as client:
63:         # Check health probe reports blocked / degraded
64:         r_ready = client.get("/health/ready")
65:         assert r_ready.status_code == 503
66:         data = r_ready.json()
67:         assert "blocked" in data.get("reason", "") or "G-WORKER" in data.get("reason", "")
68: 
69:         # Run pipeline and approve -> behaves as disabled
70:         tid = start(client)
71:         r_dec = decide(client, tid, intent={**APPROVE_COACH_BEST, "player_id": "1001", "candidate_id": "coach-best-1001"})
72:         assert r_dec.status_code == 200, r_dec.text
73:         assert r_dec.json()["dispatch"]["enabled"] is False
74:         assert r_dec.json()["dispatch"]["created"] is False
```

### Response Payload Comparison

| Field / Assertion | Current Behavior (`src/graph_api.py:419`) | Proposed Updated Behavior | Result |
| :--- | :--- | :--- | :--- |
| **`r_ready.status_code`** | `503` | `503` | **PASS** (identical) |
| **`data["status"]`** | `"degraded"` | `"degraded"` | **PASS** (identical) |
| **`data["ready"]`** | `False` | `False` | **PASS** (identical) |
| **`data["reason"]` Content** | `"dispatch configured but blocked (G-WORKER, G-ID, G-ATOMIC, G-OUTBOX)"` | `"Phase 1 safety invariant violation: DISPATCH_ENABLED must be false (dispatch configured but blocked: G-WORKER, G-ID, G-ATOMIC, G-OUTBOX)"` | **PASS** |
| **`assert "blocked" in data.get("reason", "")`** | `True` (`"blocked" in ...`) | `True` (`"blocked"` in `"(dispatch configured but blocked: ...)"`) | **PASS** |
| **`assert "G-WORKER" in data.get("reason", "")`** | `True` | `True` | **PASS** |
| **`assert "DISPATCH_ENABLED must be false" in data["reason"]`** (`test_graph_api_v1.py`) | `False` ❌ | `True` ✅ | **FIXED** |

---

## 3. Comprehensive Audit of All 24 Tests in `tests/test_dispatch_creation.py`

Every single test function in `tests/test_dispatch_creation.py` was inspected for dependencies on `/health/ready`, gate evaluation payloads, or reason string contracts:

| # | Test Function Name | Line | Scope / Method Under Test | Health Probe Dependency | Impact of Proposed Change | Status |
|---|---|---|---|---|---|---|
| 1 | `test_gates_block_by_default` | 41 | Direct DB `evaluate_gates()` & `dispatch_effective()` | None (direct function calls) | None | **Compatible** |
| 2 | `test_configured_but_blocked_behaves_disabled` | 57 | `GET /health/ready` & fallback to disabled approval | **Calls `/health/ready`** (line 64); asserts line 67 | Directly tested; fully compatible | **Compatible** |
| 3 | `test_gate_recording_requires_commissioner_and_signature` | 81 | `POST /v1/activation-gates/{gate_id}/challenge` & record | None | None | **Compatible** |
| 4 | `test_gate_nonce_replay_rejected` | 127 | Gate nonce single-use check (409 replay) | None | None | **Compatible** |
| 5 | `test_gate_nonce_expired_rejected` | 146 | Gate nonce expiration (>300s TTL check) | None | None | **Compatible** |
| 6 | `test_gate_tampered_field_rejected` | 167 | Gate HMAC signature tampering | None | None | **Compatible** |
| 7 | `test_gate_nonce_bound_to_gate` | 182 | Nonce binding to specific `gate_id` | None | None | **Compatible** |
| 8 | `test_gate_requires_deployment_identity` | 198 | Deployment identity check (`code_revision`, `deployment_id`) | None | None | **Compatible** |
| 9 | `test_gate_record_from_other_deployment_ignored` | 208 | DB isolation across foreign deployments | None | None | **Compatible** |
| 10 | `test_gate_signature_excludes_correlation_id` | 229 | Correlation ID immunity in signature digest | None | None | **Compatible** |
| 11 | `test_g_outbox_not_recordable_in_phase1` | 248 | Refusal of `G-OUTBOX` challenge/record in Phase 1 | None | None | **Compatible** |
| 12 | `test_gate_change_applies_on_next_evaluation` | 258 | Dynamic re-evaluation without restart (`reevaluate_gates`) | None | None | **Compatible** |
| 13 | `test_correlation_id_shared_by_dispatch_and_events` | 286 | Shared `correlation_id` across dispatches & domain events | None | None | **Compatible** |
| 14 | `test_approve_creates_dispatch_atomically` | 317 | Atomic pending dispatch row & events on approval | None | None | **Compatible** |
| 15 | `test_dispatch_failure_rolls_back_decision` | 355 | Trigger rollback test on dispatches table failure | None | None | **Compatible** |
| 16 | `test_not_dispatchable_without_objective` | 400 | Candidate missing objective bounds check | None | None | **Compatible** |
| 17 | `test_not_dispatchable_gamertag_player` | 412 | Player ID format regex validation (`^[0-9]{1,20}$`) | None | None | **Compatible** |
| 18 | `test_not_dispatchable_unverified_player` | 421 | Player verification check (`identity_verified_at IS NOT NULL`) | None | None | **Compatible** |
| 19 | `test_player_id_is_compared_exactly` | 432 | Exact string comparison ('7' vs '007') | None | None | **Compatible** |
| 20 | `test_not_dispatchable_unknown_player` | 448 | Nonexistent player in DB | None | None | **Compatible** |
| 21 | `test_reject_creates_no_dispatch` | 461 | REJECT decision produces 0 dispatches | None | None | **Compatible** |
| 22 | `test_supersede_is_atomic` | 474 | Prior dispatch transitions to `superseded` atomically | None | None | **Compatible** |
| 23 | `test_concurrent_supersede_is_serialized` | 505 | Advisory lock partition serialization | None | None | **Compatible** |
| 24 | `test_parallel_duplicate_approvals_single_dispatch` | 542 | 10 concurrent duplicate submissions -> 1 dispatch row | None | None | **Compatible** |

---

## 4. Cross-Suite Regression Impact

### 1. `tests/test_graph_api_v1.py`
- Line 320 (`test_health_ready_fails_if_dispatch_enabled_true`):
  ```python
  resp = client.get("/health/ready")
  assert resp.status_code == 503
  assert "DISPATCH_ENABLED must be false" in resp.json()["reason"]
  ```
  **Before update**: Fails with `assert "DISPATCH_ENABLED must be false" in "dispatch configured but blocked (...)"`.  
  **After update**: **PASSES**.

- Line 656 (`test_adversarial_readiness_fails_closed_on_dispatch_enabled_true`):
  ```python
  resp = client.get("/health/ready")
  assert resp.status_code == 503
  data = resp.json()
  assert data["status"] == "degraded"
  assert data["ready"] is False
  assert "DISPATCH_ENABLED must be false" in data["reason"]
  ```
  **Before update**: Fails assertion.  
  **After update**: **PASSES**.

### 2. `tests/test_langgraph_flow.py`
- Line 42 imports `test_health_ready_fails_if_dispatch_enabled_true` from `tests.test_graph_api_v1`.
  **Before update**: Fails assertion.  
  **After update**: **PASSES**.

---

## 5. Worker Recommendations & Action Plan

### Required Code Change (Worker Scope)
The worker agent should make a 1-line update in `src/graph_api.py` around line 419:

```diff
--- a/src/graph_api.py
+++ b/src/graph_api.py
@@ -416,7 +416,7 @@ def create_app(settings: Settings, ...) -> FastAPI:
                     content={
                         "status": "degraded",
                         "ready": False,
-                        "reason": f"dispatch configured but blocked ({', '.join(failed)})",
+                        "reason": f"Phase 1 safety invariant violation: DISPATCH_ENABLED must be false (dispatch configured but blocked: {', '.join(failed)})",
                     },
                 )
```

### Verification Test Commands for Worker
1. **Targeted Dispatch Creation Tests**:
   ```powershell
   pytest tests/test_dispatch_creation.py -v
   ```
   *Expected: 24 passed.*

2. **Targeted Readiness Contract Regression Tests**:
   ```powershell
   pytest tests/test_graph_api_v1.py -k "test_health_ready" -v
   pytest tests/test_langgraph_flow.py -k "test_health_ready" -v
   ```
   *Expected: All matching tests pass.*

3. **Combined Regression Execution**:
   ```powershell
   pytest tests/test_dispatch_creation.py tests/test_graph_api_v1.py tests/test_langgraph_flow.py
   ```
   *Expected: All tests pass.*
