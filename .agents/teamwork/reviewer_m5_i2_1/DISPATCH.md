# DISPATCH: reviewer_m5_i2_1 (Scoped Tokens, Step-Up Engine & Test Suite Reviewer)

**Role**: `teamwork_preview_reviewer`
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m5_i2_1`
**Authoritative Request**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md`
**Project Plan**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
**Worker Handoff**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m5_2\handoff.md`
**Previous Audit Report**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m5_1\handoff.md`
**Previous Reviewer Report**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m5_1\handoff.md`

---

## Non-Negotiable Invariants
1. Universal dormancy: `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` strictly enforced across all environments, settings, and endpoints.
2. Zero player delivery; Phase 2 must NEVER activate player dispatch, delivery adapters, or outbox relay.
3. No Phase 3 dispatch worker, outbox relay, or player-agent execution pipeline.
4. Preserve Phase 1 baseline commit `3339d6d672bbc59f251e1e2a6977bc9ea65edffe`.
5. PostgreSQL is authoritative for all identity and authorization state.
6. No agent-human impersonation (automated services/LLM nodes cannot authenticate or decide for humans; `require_human_principal` enforced).
7. Gamertags and display names are presentation data only; use immutable local user UUIDs and numeric player IDs as primary identity.
8. Inactivity and timeouts alter escalation state but NEVER create approvals or decisions (G-TIMEOUT invariant).

---

## Review Scope & Instructions
1. Inspect `src/identity/tokens.py`:
   - Verify compatibility exports: `ScopedActionToken`, `VALID_ACTION_SCOPES`, `hash_action_token`, `is_action_token_format`.
   - Verify `ActionTokenRecord` supports attribute access, `raw_token`, tuple unpacking (`__iter__`, `__len__`), and mapping subscription (`__getitem__`).
   - Verify `issue_token` handles `expires_in_minutes` optional parameter.
   - Verify atomic `SELECT ... FOR UPDATE` consumption, hashing via SHA-256, and exception hierarchy.
2. Inspect `src/identity/routers/step_up.py`:
   - Verify `compute_bound_payload_hash` deterministically hashes package identity, version, hash, decision, and request ID.
   - Verify `step_up_verify` WebAuthn assertion verification, counter checks, single-use `authentication_evidence_id` minting, and scope binding.
3. Inspect `tests/identity/test_tokens.py` & `tests/identity/test_step_up.py`:
   - Verify all test functions import cleanly without `ImportError`.
   - Verify `test_step_up_verify_endpoint_mints_evidence` mocks `store.get_challenge` with `challenge_type="step_up_decision"` so it returns HTTP 200.
4. Execute verification tests:
   ```powershell
   .\.venv\Scripts\pytest.exe tests/identity/test_tokens.py tests/identity/test_step_up.py tests/test_packaging.py tests/test_phase1_scope.py -v
   ```
   (Set `WaitMsBeforeAsync: 10000`). If permission prompt times out, document honestly and verify via AST / static analysis.
5. Provide an explicit verdict in your handoff report: **APPROVE** or **REQUEST_CHANGES**.
6. Deliver report to: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m5_i2_1\handoff.md`.
7. Notify orchestrator via `send_message` when complete.


## 2026-10-04T10:12:12Z
[Message] timestamp=2026-10-04T10:12:12Z sender=25210876-a9ad-4f78-a87c-dec88e86edb9 priority=MESSAGE_PRIORITY_HIGH content=You are reviewer_m5_i2_1, a high-reliability reviewer agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m5_i2_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m5_i2_1\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md
Worker handoff: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m5_2\handoff.md
Previous review report: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m5_1\handoff.md
Previous audit report: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m5_1\handoff.md
