# DISPATCH: challenger_m6_2 (Phase 2 Tier 5 Adversarial Coverage Challenger)

**Role**: `teamwork_preview_challenger`
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m6_2`
**Authoritative Request**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md`
**Project Plan**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
**Worker Handoff**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m6_1\handoff.md`

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

## Adversarial Challenge Mandate
Empirically stress-test the Tier 5 Adversarial Coverage Hardening suite across the 10 security tracks:
1. **Adversarial Tracks Stress-Testing**:
   - Track 1: Object-level player isolation (cross-player view, challenge, decision, token rejection).
   - Track 2: G-TIMEOUT invariant (`system:timeout` actions, `timeout_approved` attributes, expired requests).
   - Track 3: Service anti-impersonation (service tokens on human endpoints, forged headers).
   - Track 4: Universal dormancy (`DISPATCH_ENABLED=false`, zero player delivery, empty outbox).
   - Track 5: PostgreSQL authoritative state (7 triggers, row locks, DDL rejection).
   - Track 6: Decision step-up replay & scope lockout (package ID, version, hash binding).
   - Track 7: Scoped action tokens (single-use, expiry, revocation, scope confusion).
   - Track 8: Commissioner bootstrap ceremony lock (one-time lock, HTTP 409).
   - Track 9: Commissioner account recovery (Crockford Base32, Argon2id, atomic regeneration revocation).
   - Track 10: Legacy HMAC kill switch (`cli_hmac_enabled = False` default, HTTP 403 `proof_type_disabled`).
2. **Execute Empirical Tests**:
   ```powershell
   .\.venv\Scripts\pytest.exe tests/adversarial/test_adversarial_phase2.py tests/test_packaging.py tests/test_phase1_scope.py -v
   ```
   (Set `WaitMsBeforeAsync: 10000`). If permission prompt times out, document honestly and verify via AST / static analysis.
3. Provide an explicit verdict in your handoff report: **APPROVE** or **REQUEST_CHANGES**.
4. Deliver report to: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m6_2\handoff.md`.
5. Notify orchestrator via `send_message` when complete.


## 2026-10-04T10:46:38Z
You are challenger_m6_2, an adversarial verifier agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m6_2
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m6_2\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md
Worker handoff: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m6_1\handoff.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md and c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m6_2\DISPATCH.md before starting work.

Your task:
Empirically stress-test the Tier 5 Adversarial Coverage Hardening suite across the 10 security tracks:
1. Adversarial Tracks Stress-Testing:
   - Track 1: Object-level player isolation (cross-player view, challenge, decision, token rejection).
   - Track 2: G-TIMEOUT invariant (system:timeout actions, timeout_approved attributes, expired requests).
   - Track 3: Service anti-impersonation (service tokens on human endpoints, forged headers).
   - Track 4: Universal dormancy (DISPATCH_ENABLED=false, zero player delivery, empty outbox).
   - Track 5: PostgreSQL authoritative state (7 triggers, row locks, DDL rejection).
   - Track 6: Decision step-up replay & scope lockout (package ID, version, hash binding).
   - Track 7: Scoped action tokens (single-use, expiry, revocation, scope confusion).
   - Track 8: Commissioner bootstrap ceremony lock (one-time lock, HTTP 409).
   - Track 9: Commissioner account recovery (Crockford Base32, Argon2id, atomic regeneration revocation).
   - Track 10: Legacy HMAC kill switch (cli_hmac_enabled = False default, HTTP 403 proof_type_disabled).
2. Execute empirical tests:
   .\.venv\Scripts\pytest.exe tests/adversarial/test_adversarial_phase2.py tests/test_packaging.py tests/test_phase1_scope.py -v
   (Set WaitMsBeforeAsync=10000). If permission prompt times out, document honestly and verify via AST / static analysis.
3. Provide an explicit verdict in your handoff report: APPROVE or REQUEST_CHANGES.
Deliver complete report to: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m6_2\handoff.md
When finished, notify orchestrator with your verdict and report path via send_message.
