# DISPATCH — spec_miner_m5_graph_migration_1

**Timestamp**: 2026-10-04T09:35:00Z  
**Role**: Graph API Migration & HMAC Kill Switch Spec Miner  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\spec_miner_m5_graph_migration_1`  
**Authoritative Request**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md`  
**Project Plan**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`  

---

## Mission
Investigate and spec-mine Graph API decision endpoint migration and legacy HMAC kill switch for Phase 2 Milestone 5:
1. Requirements & Scope:
   - Features 43, 44, 46: Authenticated Human Decision Endpoint, Phase 1 CLI/HMAC Kill Switch, and Inactivity / Timeout No-Approval Guard.
   - Inspect existing endpoint in `src/graph_api.py`:
     `POST /approval/packages/{package_id}/decide`.
   - Migration requirements:
     - Replace Phase 1 HMAC requirement with validated `authentication_evidence_id` linked to WebAuthn step-up verification (`proof_type="webauthn"`).
     - Backward compatibility and kill switch:
       - Config toggle: `APPROVAL_CLI_HMAC_ENABLED: bool` (default `false`).
       - If `APPROVAL_CLI_HMAC_ENABLED=false` and request provides `proof_type="hmac"`, reject with HTTP 403 / 400 (`"legacy HMAC decision submissions are disabled"`).
       - Only allow HMAC when explicitly enabled in dev/test via `APPROVAL_CLI_HMAC_ENABLED=true`.
     - Inactivity / Timeout No-Approval Guard (G-TIMEOUT invariant):
       - Timeouts alter escalation state but NEVER commit an approval decision.
       - Zero autonomous approvals without affirmative human passkey evidence.
     - Centralized Authorization check (`src/identity/policy.py:authorize()`):
       - Validate commissioner or valid delegation authority before committing decision.
2. Provide exact endpoints, request/response models, migration diffs for `src/graph_api.py`, configuration settings, and comprehensive test suite plans in `handoff.md`. Do NOT edit source files directly.
Deliver handoff report to: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\spec_miner_m5_graph_migration_1\handoff.md`
When finished, notify orchestrator with report path via send_message.


## 2026-10-04T09:15:19Z
[Message] timestamp=2026-10-04T09:15:19Z sender=25210876-a9ad-4f78-a87c-dec88e86edb9 priority=MESSAGE_PRIORITY_HIGH content=You are spec_miner_m5_graph_migration_1, a Read-only specification investigator.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\spec_miner_m5_graph_migration_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\spec_miner_m5_graph_migration_1\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md and c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\spec_miner_m5_graph_migration_1\DISPATCH.md before starting work.

Your task:
Investigate and spec-mine Graph API decision endpoint migration and legacy HMAC kill switch for Phase 2 Milestone 5:
1. Features 43, 44, 46: Authenticated Human Decision Endpoint, Phase 1 CLI/HMAC Kill Switch, and Inactivity / Timeout No-Approval Guard.
2. Inspect existing endpoint in src/graph_api.py: POST /approval/packages/{package_id}/decide.
3. Formulate exact migration plan:
   - Accept proof_type="webauthn" with authentication_evidence_id validated against IdentityStore.
   - Backward compatibility / kill switch: APPROVAL_CLI_HMAC_ENABLED (default false). When false, proof_type="hmac" is rejected (HTTP 403).
   - Inactivity / Timeout No-Approval Guard: verify G-TIMEOUT invariant.
   - Centralized authorization check via policy.py:authorize().
4. Deliver complete handoff report to:
   c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\spec_miner_m5_graph_migration_1\handoff.md
When finished, notify me with your report path via send_message.
