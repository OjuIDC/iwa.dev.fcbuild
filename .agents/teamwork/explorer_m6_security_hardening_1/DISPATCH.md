# DISPATCH: explorer_m6_security_hardening_1 (Phase 2 Adversarial Hardening Explorer)

**Role**: `teamwork_preview_explorer`
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m6_security_hardening_1`
**Authoritative Request**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md`
**Project Plan**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
**Test Infra Specification**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\e2e_test_writer_phase2_1\TEST_INFRA.md`

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

## Exploration Mission
Map the 10 adversarial security tracks and design Tier 5 Adversarial Coverage Hardening test suites for Phase 2:
1. **Track 1: Object-Level Player Isolation**:
   - Cross-player assignment access, challenges, decision submissions, action token issuances.
   - Staff/coach acting on player assignments.
2. **Track 2: G-TIMEOUT Invariant**:
   - Escalation state updates allowed; auto-approval or decision injection strictly blocked.
   - Rejection of `system:timeout*` actions and `timeout_approved` attributes.
3. **Track 3: Service Anti-Impersonation**:
   - Service keys/tokens attempting human ceremonies (WebAuthn, TOTP, recovery, human decisions).
4. **Track 4: Universal Dormancy & Zero Player Delivery**:
   - Verification across all endpoints, settings, and configuration that `DISPATCH_ENABLED=false`, zero player delivery occurs, and outbox relay / workers are absent.
5. **Track 5: PostgreSQL Authoritative State**:
   - DDL triggers, table row-level locks (`SELECT ... FOR UPDATE`), transactional isolation.
6. **Track 6: Decision Step-Up Replay & Scope Lockout**:
   - Single-use evidence consumption; scope lockout binding package ID, version, hash.
7. **Track 7: Scoped Action Tokens**:
   - High-entropy `sat_` format; SHA-256 storage; single-use, expiry, revocation, scope confusion.
8. **Track 8: Commissioner Bootstrap Ceremony Lock**:
   - One-time bootstrap; subsequent calls return HTTP 409; `fc27_bootstrap_locks` immutability trigger.
9. **Track 9: Commissioner Account Recovery**:
   - Crockford Base32 codes; Argon2id irreversible hashing; atomic revocation of unconsumed codes on regeneration.
10. **Track 10: Legacy HMAC Kill Switch**:
    - Default `cli_hmac_enabled = False`; HTTP 403 `proof_type_disabled` when False.

Deliver complete handoff report to: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m6_security_hardening_1\handoff.md`.
Notify orchestrator via `send_message` when complete.


## 2026-10-04T10:20:28Z
You are explorer_m6_security_hardening_1, a Read-only exploration agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m6_security_hardening_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m6_security_hardening_1\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md
Test infra: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\e2e_test_writer_phase2_1\TEST_INFRA.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md and c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m6_security_hardening_1\DISPATCH.md before starting work.

Your task:
Map the 10 adversarial security tracks and design Tier 5 Adversarial Coverage Hardening test suites for Phase 2:
1. Track 1: Object-Level Player Isolation
2. Track 2: G-TIMEOUT Invariant
3. Track 3: Service Anti-Impersonation
4. Track 4: Universal Dormancy & Zero Player Delivery
5. Track 5: PostgreSQL Authoritative State (locks, triggers)
6. Track 6: Decision Step-Up Replay & Scope Lockout
7. Track 7: Scoped Action Tokens Single-Use & Expiry
8. Track 8: Commissioner Bootstrap Ceremony Lock
9. Track 9: Commissioner Account Recovery & Argon2id Revocation
10. Track 10: Legacy HMAC Kill Switch
Deliver complete handoff report with exact test designs and gap analysis to:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m6_security_hardening_1\handoff.md
When finished, notify orchestrator with your report path via send_message.
