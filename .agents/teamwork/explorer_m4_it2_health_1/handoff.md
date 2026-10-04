# Handoff Report: Milestone 4 Iteration 2 Health Probe Reason String Contract Fix

**Agent**: `explorer_m4_it2_health_1`  
**Role**: Milestone 4 Iteration 2 Health Probe Explorer  
**Date**: 2026-10-03T20:54:00Z  
**Type**: Hard Handoff  

---

## 1. Observation

1. **`src/graph_api.py:409-430`**:
   The `/health/ready` probe implements the following logic when `settings.dispatch_enabled` is `True`:
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
2. **`tests/test_graph_api_v1.py:311-321`**:
   ```python
   311: def test_health_ready_fails_if_dispatch_enabled_true(pg_uri):
   312:     """AC §11.3 / F27: /health/ready returns 503 if DISPATCH_ENABLED=true."""
   313:     app, _ = make_app(pg_uri)
   314:     # Manually configure dispatch_enabled = True on settings
   315:     cfg = settings(pg_uri, dispatch_enabled=True)
   316:     bad_app = create_app(cfg, pipeline=StubPipeline())
   317:     with TestClient(bad_app) as client:
   318:         resp = client.get("/health/ready")
   319:         assert resp.status_code == 503
   320:         assert "DISPATCH_ENABLED must be false" in resp.json()["reason"]
   ```
3. **`tests/test_graph_api_v1.py:646-657`**:
   ```python
   646: def test_adversarial_readiness_fails_closed_on_dispatch_enabled_true(pg_uri):
   647:     """Adversarial Attack: Simulate DISPATCH_ENABLED=true; verify /health/ready returns 503."""
   648:     bad_cfg = settings(pg_uri, dispatch_enabled=True)
   649:     app = create_app(bad_cfg, pipeline=StubPipeline())
   650:     with TestClient(app) as client:
   651:         resp = client.get("/health/ready")
   652:         assert resp.status_code == 503
   653:         data = resp.json()
   654:         assert data["status"] == "degraded"
   655:         assert data["ready"] is False
   656:         assert "DISPATCH_ENABLED must be false" in data["reason"]
   ```
4. **`tests/test_langgraph_flow.py:30-45`**:
   Explicitly imports and runs `test_health_ready_fails_if_dispatch_enabled_true`:
   ```python
   42:     test_health_ready_fails_if_dispatch_enabled_true,
   ```
5. **`tests/test_dispatch_creation.py:57-68`**:
   ```python
   57: def test_configured_but_blocked_behaves_disabled(pg_uri):
   58:     """AC-45: dispatch_enabled=True settings, no gate records -> no dispatch, health blocked."""
   ...
   64:         r_ready = client.get("/health/ready")
   65:         assert r_ready.status_code == 503
   66:         data = r_ready.json()
   67:         assert "blocked" in data.get("reason", "") or "G-WORKER" in data.get("reason", "")
   ```
6. **`challenger_m4_1/challenge_report.md:27-65`**:
   Challenger 1 flagged this regression under Challenge 1:
   "When `dispatch_enabled=True` and gates are unrecorded (default state), `src/graph_api.py:414` returns `"reason": f"dispatch configured but blocked ({', '.join(failed)})"`. This omits the substring `"DISPATCH_ENABLED must be false"`, causing existing contract tests `tests/test_graph_api_v1.py::test_health_ready_fails_if_dispatch_enabled_true`, `tests/test_graph_api_v1.py::test_adversarial_readiness_fails_closed_on_dispatch_enabled_true`, and `tests/test_langgraph_flow.py::test_health_ready_fails_if_dispatch_enabled_true` to fail on assertion."

---

## 2. Logic Chain

1. In Milestone 4, `reevaluate_gates` sets `app.state.dispatch_effective = False` in default/test environments because automated and manual rollout gates (`G-WORKER`, `G-ID`, `G-ATOMIC`, `G-OUTBOX`) are unrecorded.
2. In tests where `dispatch_enabled=True` is tested, `app.state.dispatch_effective` is `False`.
3. Consequently, the conditional in `src/graph_api.py` line 411 (`if not effective:`) evaluates to `True`, triggering the early return at line 414.
4. Line 419 sets `"reason": f"dispatch configured but blocked ({', '.join(failed)})"`.
5. This string contains `"dispatch configured but blocked (G-WORKER, ...)"`, but completely omits `"DISPATCH_ENABLED must be false"`.
6. Therefore, `assert "DISPATCH_ENABLED must be false" in resp.json()["reason"]` in `test_graph_api_v1.py:320`, `test_graph_api_v1.py:656`, and `test_langgraph_flow.py` evaluates to `False`, raising an `AssertionError`.
7. Changing line 419 to:
   ```python
   "reason": f"Phase 1 safety invariant violation: DISPATCH_ENABLED must be false (dispatch configured but blocked: {', '.join(failed)})",
   ```
   produces a string containing both:
   - `"Phase 1 safety invariant violation: DISPATCH_ENABLED must be false"` (satisfying `test_graph_api_v1.py:320`, `test_graph_api_v1.py:656`, `test_langgraph_flow.py`, and `ORIGINAL_REQUEST.md` §1 / §5.7).
   - `"dispatch configured but blocked: ..."` (satisfying `test_dispatch_creation.py:67` which checks for `"blocked"` or `"G-WORKER"`).
8. This single line replacement resolves the regression, restores full backward compatibility with Milestone 2 test suites, and retains the diagnostic failure list introduced in Milestone 4.

---

## 3. Caveats

No caveats.
The fix is completely localized to `src/graph_api.py:419`. No database migrations, schema alterations, or other application endpoints are affected.

---

## 4. Conclusion

The regression in `/health/ready` identified by Challenger 1 is fully confirmed and diagnosed.
The drop-in replacement in `src/graph_api.py:419`:
```python
"reason": f"Phase 1 safety invariant violation: DISPATCH_ENABLED must be false (dispatch configured but blocked: {', '.join(failed)})",
```
completely restores the contract required by existing tests while preserving all Milestone 4 gate diagnostic functionality.

### Recommended Worker Action:
Apply the following replacement in `src/graph_api.py`:

**Target line**: 419  
**Existing line**:
```python
                        "reason": f"dispatch configured but blocked ({', '.join(failed)})",
```
**Replacement line**:
```python
                        "reason": f"Phase 1 safety invariant violation: DISPATCH_ENABLED must be false (dispatch configured but blocked: {', '.join(failed)})",
```

---

## 5. Verification Method

1. **Code Inspection**:
   Inspect `src/graph_api.py:414-421` to verify line 419 matches the exact string.
2. **Automated Test Verification**:
   Execute the three affected test suites:
   ```bash
   poetry run pytest tests/test_graph_api_v1.py -k "test_health_ready_fails_if_dispatch_enabled_true or test_adversarial_readiness_fails_closed_on_dispatch_enabled_true"
   poetry run pytest tests/test_langgraph_flow.py -k "test_health_ready_fails_if_dispatch_enabled_true"
   poetry run pytest tests/test_dispatch_creation.py -k "test_configured_but_blocked_behaves_disabled"
   ```
3. **Full Suite Verification**:
   ```bash
   poetry run pytest tests/test_graph_api_v1.py
   poetry run pytest tests/test_langgraph_flow.py
   poetry run pytest tests/test_dispatch_creation.py
   ```
   All tests must pass with zero assertion errors on `/health/ready`.
4. **Invalidation Conditions**:
   The fix is invalidated if:
   - Any test expecting `"DISPATCH_ENABLED must be false"` fails.
   - Any test expecting `"blocked"` fails.
   - The status code returned is anything other than 503 when `dispatch_enabled=True`.
