# Dispatch: Milestone 4 Challenger 1 (Gate Security, Nonces & Dynamic Re-evaluation)

**Agent**: `challenger_m4_1`
**Role**: Milestone 4 Challenger 1 (Gate Security, Nonces & Dynamic Re-evaluation)
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m4_1`
**Timestamp**: 2026-10-03T20:42:00Z

---

## 1. Objective
Adversarially challenge Milestone 4 activation gate evaluation, cryptographic challenge/recording protocol, deployment identity binding, correlation ID isolation, and dynamic gate re-evaluation.

## 2. Mandatory Reading
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (authoritative user request + Section 1A patch)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`
3. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m4_1\handoff.md`
4. Reviewer 1 & 2 Handoffs:
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m4_1\handoff.md`
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m4_2\handoff.md`
5. `src/dispatch/gates.py`
6. `src/graph_api.py`
7. `tests/test_dispatch_creation.py`

## 3. Adversarial Attack & Challenge Scenarios
1. **Gate Replay & Single-Use Nonce Invalidation**:
   - Probe `/v1/activation-gates/{gate_id}` with replayed nonces. Verify second attempt fails with 409 Conflict.
   - Probe with expired nonces (>300s) or tampered payload hashes. Verify 409 / 403.
2. **Phase 1 Boundary & G-OUTBOX Rejection**:
   - Probe `/v1/activation-gates/G-OUTBOX/challenge` and `/v1/activation-gates/G-OUTBOX`.
   - Verify rejection with 409 Conflict (`not_recordable_before_phase_3`; C-3).
   - Verify automated gates (G-MIGRATION, G-GRANTS, G-WORKER) cannot be challenged manually (422 Unprocessable).
3. **Correlation ID Isolation (C-4, C-5)**:
   - Verify `GATE_SIGNED_FIELDS` strictly excludes `X-Correlation-Id`.
   - Verify signatures remain valid regardless of proxy or tracer correlation ID mutations.
4. **Dynamic Re-evaluation Without Restart (C-1)**:
   - Verify `reevaluate_gates()` updates `app.state.gate_report` and `dispatch_effective` without restarting graph-api.
   - Verify `/health/ready` probe reports 503 degraded when `dispatch_enabled=True` but gates fail.
5. **Execution Verification**:
   Execute gate-specific tests in `tests/test_dispatch_creation.py`:
   ```powershell
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_creation.py -k "gate or health or correlation"
   ```
6. Formulate explicit verdict: **APPROVE** or **REQUEST_CHANGES**.
7. Write `challenge_report.md` and `handoff.md` in your working directory.
8. Notify orchestrator via `send_message` when done.
Do NOT modify production source code in `src/` or `migrations/`.
