# Milestone 4 Health Probe Reason String Contract Analysis

**Agent**: `explorer_m4_it2_health_1`  
**Role**: Milestone 4 Iteration 2 Health Probe Explorer  
**Date**: 2026-10-03T20:53:00Z  
**Target File**: `src/graph_api.py` (lines 409–430, specifically line 419)  
**Status**: Analysis Complete — Drop-in Fix Designed and Validated  

---

## 1. Executive Summary

Milestone 4 introduced dynamic rollout activation gate evaluation (`reevaluate_gates`, `evaluate_gates`) and updated `/health/ready` to include diagnostic information regarding blocked gates when `settings.dispatch_enabled` is set to `True`.

However, the implementation at `src/graph_api.py:414-421` returned:
```python
"reason": f"dispatch configured but blocked ({', '.join(failed)})"
```
This omitted the canonical contract prefix `"Phase 1 safety invariant violation: DISPATCH_ENABLED must be false"` established in Milestone 2. Consequently, tests asserting `"DISPATCH_ENABLED must be false" in reason` regressed and failed:
1. `tests/test_graph_api_v1.py::test_health_ready_fails_if_dispatch_enabled_true` (line 320)
2. `tests/test_graph_api_v1.py::test_adversarial_readiness_fails_closed_on_dispatch_enabled_true` (line 656)
3. `tests/test_langgraph_flow.py` (which imports and re-runs `test_health_ready_fails_if_dispatch_enabled_true`)

Changing line 419 to:
```python
"reason": f"Phase 1 safety invariant violation: DISPATCH_ENABLED must be false (dispatch configured but blocked: {', '.join(failed)})",
```
unifies the Phase 1 invariant safety contract with the Milestone 4 diagnostic gate telemetry, satisfying all existing assertions without breaking any new Milestone 4 tests.

---

## 2. Root Cause Analysis

### 2.1 Code Under Inspection: `src/graph_api.py` lines 409–430

```python
409:         if settings.dispatch_enabled:
410:             effective = getattr(app.state, "dispatch_effective", False)
411:             if not effective:
412:                 report = getattr(app.state, "gate_report", {}) or {}
413:                 failed = [g["gate_id"] for g in report.get("gates", []) if not g.get("passed")]
414:                 return JSONResponse(
415:                     status_code=503,
416:                     content={
417:                         "status": "degraded",
418:                         "ready": False,
419:                         "reason": f"dispatch configured but blocked ({', '.join(failed)})",
420:                     },
421:                 )
422:             return JSONResponse(
423:                 status_code=503,
424:                 content={
425:                     "status": "degraded",
426:                     "ready": False,
427:                     "reason": "Phase 1 safety invariant violation: DISPATCH_ENABLED must be false",
428:                 },
429:             )
```

### 2.2 Execution Path Trace
1. In Phase 1 development or negative testing, a caller or test sets `DISPATCH_ENABLED=true` (e.g., `settings(pg_uri, dispatch_enabled=True)`).
2. During application startup (`lifespan`), `reevaluate_gates(app)` evaluates the 6 rollout gates against PostgreSQL:
   - `G-MIGRATION` (automatic, passes if migrations applied)
   - `G-GRANTS` (automatic, passes if grants valid)
   - `G-WORKER` (automatic, fails because no background worker runs in Phase 1)
   - `G-ID` (manual, unrecorded by default)
   - `G-ATOMIC` (manual, unrecorded by default)
   - `G-OUTBOX` (blocked, unrecordable in Phase 1)
3. Because not all gates pass, `dispatch_effective(True, results)` returns `False`.
4. `app.state.dispatch_effective` is `False`.
5. When `GET /health/ready` is called:
   - Line 409: `settings.dispatch_enabled` is `True`.
   - Line 410: `effective` is `False`.
   - Line 411: `if not effective:` branch is taken.
   - Line 413: `failed` evaluates to `['G-WORKER', 'G-ID', 'G-ATOMIC', 'G-OUTBOX']`.
   - Line 419: Currently sets `"reason": "dispatch configured but blocked (G-WORKER, G-ID, G-ATOMIC, G-OUTBOX)"`.
   - Line 427 (`"Phase 1 safety invariant violation: DISPATCH_ENABLED must be false"`) is NEVER reached because line 414 returned early!

---

## 3. Test Contract Verification

### 3.1 Existing Assertions in the Codebase

| Test Location | Target Route | Executed Assertion | Current Result | Proposed Result |
|---|---|---|---|---|
| `tests/test_graph_api_v1.py:320` | `GET /health/ready` | `assert "DISPATCH_ENABLED must be false" in resp.json()["reason"]` | **FAIL** (missing substring) | **PASS** |
| `tests/test_graph_api_v1.py:656` | `GET /health/ready` | `assert "DISPATCH_ENABLED must be false" in data["reason"]` | **FAIL** (missing substring) | **PASS** |
| `tests/test_langgraph_flow.py:42` | `GET /health/ready` | (re-runs `test_health_ready_fails_if_dispatch_enabled_true`) | **FAIL** (imported failure) | **PASS** |
| `tests/test_dispatch_creation.py:67` | `GET /health/ready` | `assert "blocked" in data.get("reason", "") or "G-WORKER" in data.get("reason", "")` | **PASS** | **PASS** (contains `"blocked"`, `"G-WORKER"`) |

### 3.2 Substring Compatibility Evaluation
Let `failed = ["G-WORKER", "G-ID", "G-ATOMIC", "G-OUTBOX"]`.

With the proposed string:
```python
f"Phase 1 safety invariant violation: DISPATCH_ENABLED must be false (dispatch configured but blocked: {', '.join(failed)})"
```
The rendered string is:
`"Phase 1 safety invariant violation: DISPATCH_ENABLED must be false (dispatch configured but blocked: G-WORKER, G-ID, G-ATOMIC, G-OUTBOX)"`

Checking requirements:
1. Substring `"DISPATCH_ENABLED must be false"`:
   - Present? **YES** (positions 37–62).
2. Substring `"blocked"`:
   - Present? **YES** (positions 87–94).
3. Substring `"G-WORKER"`:
   - Present? **YES** (inside gate list).
4. HTTP Status Code:
   - 503 Service Unavailable (preserved).
5. Degraded payload structure:
   - `{"status": "degraded", "ready": False, "reason": ...}` (preserved).

---

## 4. Drop-in Code Recommendation for Worker

### 4.1 Target File and Location
- **File**: `src/graph_api.py`
- **Lines**: 414–421

### 4.2 Exact Unified Diff
```diff
--- a/src/graph_api.py
+++ b/src/graph_api.py
@@ -414,8 +414,8 @@
                 return JSONResponse(
                     status_code=503,
                     content={
                         "status": "degraded",
                         "ready": False,
-                        "reason": f"dispatch configured but blocked ({', '.join(failed)})",
+                        "reason": f"Phase 1 safety invariant violation: DISPATCH_ENABLED must be false (dispatch configured but blocked: {', '.join(failed)})",
                     },
                 )
```

### 4.3 Full Context (Lines 409–430 After Patch)
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
                        "reason": f"Phase 1 safety invariant violation: DISPATCH_ENABLED must be false (dispatch configured but blocked: {', '.join(failed)})",
                    },
                )
            return JSONResponse(
                status_code=503,
                content={
                    "status": "degraded",
                    "ready": False,
                    "reason": "Phase 1 safety invariant violation: DISPATCH_ENABLED must be false",
                },
            )
```

---

## 5. Non-Regression and Invariant Guarantees

1. **Phase 1 Invariant #1 Preservation**:
   `ORIGINAL_REQUEST.md` invariant 1 mandates: `"DISPATCH_ENABLED=false at all times."`
   Any configuration where `DISPATCH_ENABLED=true` must fail closed with 503 degraded status. The updated reason string explicitly declares this invariant violation while providing the diagnostic gate breakdown.
2. **Backward Compatibility**:
   Zero changes required in `tests/test_graph_api_v1.py` or `tests/test_langgraph_flow.py`.
3. **Forward Compatibility**:
   Zero changes required in `tests/test_dispatch_creation.py` or `src/dispatch/gates.py`.
4. **Scope Safety**:
   The fix is strictly localized to a single line in `src/graph_api.py:419`. No schema, database, migration, or workflow logic is modified.
