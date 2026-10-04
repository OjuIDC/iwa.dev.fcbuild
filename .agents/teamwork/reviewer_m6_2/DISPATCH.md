# DISPATCH: reviewer_m6_2 (Tier 5 Adversarial Suite & Security Invariants Reviewer)

**Role**: `teamwork_preview_reviewer`
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m6_2`
**Authoritative Request**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md`
**Project Plan**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
**Worker Handoff**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m6_1\handoff.md`
**Security Hardening Explorer Report**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m6_security_hardening_1\handoff.md`

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
1. Inspect `tests/adversarial/test_adversarial_phase2.py`:
   - Verify coverage of all 10 security tracks:
     - Track 1: Object-Level Player Isolation
     - Track 2: G-TIMEOUT Invariant
     - Track 3: Service Anti-Impersonation
     - Track 4: Universal Dormancy & Zero Player Delivery
     - Track 5: PostgreSQL Authoritative State (locks, triggers)
     - Track 6: Decision Step-Up Replay & Scope Lockout
     - Track 7: Scoped Action Tokens Single-Use & Expiry
     - Track 8: Commissioner Bootstrap Lock
     - Track 9: Commissioner Account Recovery & Argon2id Revocation
     - Track 10: Legacy HMAC Kill Switch
2. Inspect non-negotiable invariants:
   - Universal dormancy: `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false`.
   - Zero player delivery; no outbox relay or worker execution.
   - Anti-impersonation: `require_human_principal`.
   - G-TIMEOUT: timeout/inactivity never creates decisions.
3. Execute verification tests:
   ```powershell
   .\.venv\Scripts\pytest.exe tests/adversarial/test_adversarial_phase2.py tests/test_packaging.py tests/test_phase1_scope.py -v
   ```
   (Set `WaitMsBeforeAsync: 10000`). If permission prompt times out, document honestly and verify via AST / static analysis.
4. Provide an explicit verdict in your handoff report: **APPROVE** or **REQUEST_CHANGES**.
5. Deliver report to: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m6_2\handoff.md`.
6. Notify orchestrator via `send_message` when complete.

## 2026-10-04T10:46:38Z
You are reviewer_m6_2, a high-reliability reviewer agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m6_2
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m6_2\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md
Worker handoff: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m6_1\handoff.md
Security hardening report: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m6_security_hardening_1\handoff.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md and c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m6_2\DISPATCH.md before starting work.

Your task:
Review the Tier 5 Adversarial Coverage Hardening suite and security invariants for Milestone 6:
1. Inspect tests/adversarial/test_adversarial_phase2.py:
   - Verify coverage of all 10 security tracks:
     - Track 1: Object-Level Player Isolation
     - Track 2: G-TIMEOUT Invariant
     - Track 3: Service Anti-Impersonation
     - Track 4: Universal Dormancy & Zero Player Delivery
     - Track 5: PostgreSQL Authoritative State (locks, triggers)
     - Track 6: Decision Step-Up Replay & Scope Lockout
     - Track 7: Scoped Action Tokens Single-Use & Expiry
     - Track 8: Commissioner Bootstrap Lock
     - Track 9: Commissioner Account Recovery & Argon2id Revocation
     - Track 10: Legacy HMAC Kill Switch
2. Inspect non-negotiable invariants:
   - Universal dormancy: DISPATCH_ENABLED=false and DISPATCH_WORKER_ENABLED=false.
   - Zero player delivery; no outbox relay or worker execution.
   - Anti-impersonation: require_human_principal.
   - G-TIMEOUT: timeout/inactivity never creates decisions.
3. Execute verification tests:
   .\.venv\Scripts\pytest.exe tests/adversarial/test_adversarial_phase2.py tests/test_packaging.py tests/test_phase1_scope.py -v
   (Set WaitMsBeforeAsync=10000). If permission prompt times out, document honestly and verify via AST / static analysis.
4. Provide an explicit verdict in your handoff report: APPROVE or REQUEST_CHANGES.
Deliver complete report to: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m6_2\handoff.md
When finished, notify orchestrator with your verdict and report path via send_message.
