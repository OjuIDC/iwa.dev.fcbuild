# DISPATCH: worker_m6_1 (Phase 2 Milestone 6 E2E Integration & Adversarial Hardening Worker)

**Role**: `teamwork_preview_worker`
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m6_1`
**Authoritative Request**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md`
**Project Plan**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
**Test Infra Specification**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\e2e_test_writer_phase2_1\TEST_INFRA.md`
**Test Readiness Declaration**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\e2e_test_writer_phase2_1\TEST_READY.md`
**E2E Alignment Explorer Report**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m6_phase2_e2e_1\handoff.md`
**Proposed Delegations Router**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m6_phase2_e2e_1\proposed_delegations.py`
**Security Hardening Explorer Report**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m6_security_hardening_1\handoff.md`

---

## MANDATORY INTEGRITY WARNING
> DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

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

## File Ownership
You exclusively own and may edit or create:
- `src/identity/routers/delegations.py`
- `src/identity/routers/__init__.py`
- `src/graph_api.py`
- `tests/e2e/conftest.py`
- `tests/adversarial/test_adversarial_phase2.py`

---

## Mission Objectives

### Objective 1: Implement & Mount Human Delegations Router
1. Create `src/identity/routers/delegations.py` based on `explorer_m6_phase2_e2e_1/proposed_delegations.py`:
   - `POST /v1/auth/delegations` (create delegation: validate action not in `PROHIBITED_DELEGATION_ACTIONS`, require human commissioner/staff, call `store.create_delegation`).
   - `GET /v1/auth/delegations` (list delegations: call `store.list_delegations`).
   - `GET /v1/auth/delegations/{delegation_id}` (get delegation: call `store.get_delegation`).
   - `DELETE /v1/auth/delegations/{delegation_id}` (revoke delegation: call `store.revoke_delegation`).
2. Export `delegations_router` in `src/identity/routers/__init__.py`.
3. Mount `delegations_router` in `src/graph_api.py`:
   `app.include_router(delegations_router, prefix="/v1/auth/delegations", tags=["delegations"])`.

### Objective 2: Service Credentials Test Environment
In `tests/e2e/conftest.py` (or fixture setup), ensure default test environment variables for service tokens are present if unset:
- `os.environ.setdefault("BFF_SERVICE_TOKEN", "test-bff-token")`
- `os.environ.setdefault("GRAPH_SERVICE_TOKEN", "test-graph-token")`
- `os.environ.setdefault("APPROVAL_CLI_HMAC_ENABLED", "false")`

### Objective 3: Tier 5 Adversarial Coverage Hardening Suite
Create `tests/adversarial/test_adversarial_phase2.py` implementing the 10 adversarial security tracks designed in `explorer_m6_security_hardening_1/handoff.md`:
1. **Track 1: Object-Level Player Isolation**:
   - Cross-player assignment access, challenges, decision submissions, action token issuances raise HTTP 403 `PlayerIsolationError`.
   - Staff/coach attempting player decision raises HTTP 403 `PlayerIsolationError`.
2. **Track 2: G-TIMEOUT Invariant**:
   - Rejection of action string `"system:timeout:auto_approve"` with `AgentImpersonationError`.
   - Rejection of resource with `timeout_approved=True` with `AgentImpersonationError`.
   - Expired approval request fails closed with HTTP 409 `approval_expired`.
3. **Track 3: Service Anti-Impersonation**:
   - Service identity attempting human WebAuthn registration/assertion, TOTP enrollment/verify, recovery code consumption, or decision execution is rejected with HTTP 403 `AgentImpersonationError`.
4. **Track 4: Universal Dormancy & Zero Player Delivery**:
   - `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` verified across endpoints and configuration.
   - Zero player delivery; no outbox relay or worker execution.
5. **Track 5: PostgreSQL Authoritative State**:
   - Verify triggers and row locks (`SELECT ... FOR UPDATE`) in `store.py`.
6. **Track 6: Decision Step-Up Replay & Scope Lockout**:
   - Reusing `authentication_evidence_id` fails with HTTP 409 `evidence_already_consumed`.
   - Evidence minted for package A, version 1 fails with HTTP 400 when submitted for package B or version 2.
7. **Track 7: Scoped Action Tokens**:
   - High-entropy `sat_` format, SHA-256 storage, single-use consumption, expired token rejection (410), revoked token rejection (410), scope confusion rejection (403).
8. **Track 8: Commissioner Bootstrap Ceremony Lock**:
   - First bootstrap succeeds; second bootstrap returns HTTP 409 `bootstrap_already_completed`.
9. **Track 9: Commissioner Account Recovery**:
   - Crockford Base32 codes, Argon2id irreversible hashing, atomic revocation of unconsumed codes on regeneration.
10. **Track 10: Legacy HMAC Kill Switch**:
    - Default `cli_hmac_enabled = False` rejects HMAC submissions with HTTP 403 `proof_type_disabled`.

### Objective 4: Verification Execution
Execute the full test suites synchronously in a single invocation with `WaitMsBeforeAsync: 10000`:
```powershell
.\.venv\Scripts\pytest.exe tests/e2e/test_phase2_tier1_features.py tests/e2e/test_phase2_tier2_boundaries.py tests/e2e/test_phase2_tier3_combinations.py tests/e2e/test_phase2_tier4_scenarios.py tests/adversarial/test_adversarial_phase2.py tests/test_packaging.py tests/test_phase1_scope.py -v
```
If the host interactive permission prompt times out, document honestly and verify all files via static and AST analysis.

### Objective 5: Hard Handoff
Deliver complete hard handoff report to: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m6_1\handoff.md`.
Notify orchestrator via `send_message` when complete.


## 2026-10-04T10:33:52Z
[Message] timestamp=2026-10-04T10:33:52Z sender=25210876-a9ad-4f78-a87c-dec88e86edb9 priority=MESSAGE_PRIORITY_HIGH content=You are worker_m6_1, a versatile worker agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m6_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m6_1\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md
Test infra: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\e2e_test_writer_phase2_1\TEST_INFRA.md
Test readiness: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\e2e_test_writer_phase2_1\TEST_READY.md
E2E alignment explorer report: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m6_phase2_e2e_1\handoff.md
Proposed delegations router: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m6_phase2_e2e_1\proposed_delegations.py
Security hardening explorer report: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m6_security_hardening_1\handoff.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md and c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m6_1\DISPATCH.md before starting work.

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.
