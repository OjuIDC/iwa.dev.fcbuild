# BRIEFING — 2026-10-04T09:41:00Z

## Mission
Investigate and spec-mine Graph API decision endpoint migration and legacy HMAC kill switch for Phase 2 Milestone 5 (Features 43, 44, 46).

## 🔒 My Identity
- Archetype: specification-miner
- Roles: Specification Miner, Read-only investigator
- Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\spec_miner_m5_graph_migration_1
- Original parent: 25210876-a9ad-4f78-a87c-dec88e86edb9
- Milestone: Phase 2 Milestone 5

## 🔒 Key Constraints
- Read-only: Do NOT implement anything or edit codebase source files directly.
- Prioritize authoritative sources over LLM prior knowledge.
- Deliver comprehensive handoff report to `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\spec_miner_m5_graph_migration_1\handoff.md`.
- Features 43, 44, 46: Authenticated Human Decision Endpoint, Phase 1 CLI/HMAC Kill Switch, Inactivity / Timeout No-Approval Guard.
- G-TIMEOUT invariant: Zero autonomous approvals without affirmative human passkey evidence. Timeouts alter escalation state but never commit an approval decision.
- APPROVAL_CLI_HMAC_ENABLED config toggle (default false). When false, reject proof_type="hmac" with HTTP 403.
- WebAuthn verification via authentication_evidence_id in IdentityStore.
- Centralized authorization check via policy.py:authorize().

## Current Parent
- Conversation ID: 25210876-a9ad-4f78-a87c-dec88e86edb9
- Updated: 2026-10-04T09:41:00Z

## Task Summary
- **What to build**: Specification, data models, API diff, and verification plan for Graph API decision endpoint migration.
- **Success criteria**: Detailed handoff.md with all 5 components, Features Discovered table, Edge Cases table, migration diff, policy & IdentityStore integration specs, kill switch spec, timeout invariant analysis.
- **Interface contracts**: PROJECT.md, ORIGINAL_REQUEST.md, src/graph_api.py, src/identity/policy.py, src/identity/store.py, src/identity/deps.py
- **Code layout**: src/graph_api.py, src/identity/*, tests/test_graph_api.py, tests/e2e/*

## Key Decisions Made
- Discovered existing decision endpoints in `src/graph_api.py`: `POST /v1/approval-requests/{approval_request_id}/decisions`, `POST /v1/runs/{thread_id}/decision`, legacy `POST /threads/{thread_id}/decision`.
- Designed unification with requested package-centric route: `POST /approval/packages/{package_id}/decide` (and `/v1/approval/packages/{package_id}/decide`) resolving to the same underlying decision coordinator.
- Designed `DecisionSubmission` schema supporting `proof_type="webauthn"` (default) and `proof_type="hmac"`, with automatic field validation and normalization.
- Defined kill switch behavior: `APPROVAL_CLI_HMAC_ENABLED: bool = False` by default; when false, `proof_type="hmac"` or legacy HMAC signature is rejected with HTTP 403.
- Defined `authentication_evidence_id` lifecycle: checked against `IdentityStore`, validated for single-use, bound payload hash match, user matching, and replay lockout.
- Integrated `policy.authorize()` with anti-impersonation (rejecting services like BFF and Graph service) and supporting both primary commissioner and human delegates.
- Verified G-TIMEOUT invariant: background loop and endpoint fail closed on expired requests, transitioning to `expired`, never `approved`.

## Artifact Index
- DISPATCH.md — Dispatch instructions and mission
- BRIEFING.md — Situational awareness and identity
- progress.md — Liveness heartbeat and milestone tracking
- handoff.md — Final deliverable report

## Loaded Skills
None
