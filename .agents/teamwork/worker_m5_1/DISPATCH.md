# DISPATCH — 2026-10-04T05:30:00Z

## Mission
Implement Phase 2 Milestone 5: Decision Step-Up, Player Decisions & Graph API Migration.

## Authoritative Inputs
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (MANDATORY: You MUST read this before starting work)
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m5_step_up_1\handoff.md`
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m5_player_decisions_1\handoff.md`
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\spec_miner_m5_graph_migration_1\handoff.md`

## MANDATORY INTEGRITY WARNING
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

## Non-Negotiable Invariants
1. Universal dormancy: `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` strictly enforced across all environments, settings, and endpoints.
2. Zero player delivery; Phase 2 must NEVER activate player dispatch, delivery adapters, or outbox relay.
3. No Phase 3 dispatch worker, outbox relay, or player-agent execution pipeline.
4. Preserve Phase 1 baseline commit `3339d6d672bbc59f251e1e2a6977bc9ea65edffe`.
5. PostgreSQL is authoritative for all identity and authorization state.
6. No agent-human impersonation (automated services/LLM nodes cannot authenticate or decide for humans; `require_human_principal` enforced).
7. Gamertags and display names are presentation data only; use immutable local user UUIDs and numeric player IDs as primary identity.
8. Inactivity and timeouts alter escalation state but NEVER create approvals or decisions (G-TIMEOUT invariant).

## File Ownership
You exclusively own and may edit or create:
- `src/identity/tokens.py`
- `src/identity/routers/player.py`
- `src/identity/routers/step_up.py`
- `src/identity/routers/__init__.py`
- `src/identity/store.py`
- `src/schemas.py`
- `src/graph_api.py`
- `tests/identity/test_tokens.py`
- `tests/identity/test_player.py`
- `tests/identity/test_step_up.py`

## Scope & Implementation Details

### 1. Scoped Action Tokens Engine (`src/identity/tokens.py`)
- Implement `ScopedActionTokenEngine` managing issuance, verification, single-use consumption, and revocation.
- Token format: `sat_<secrets.token_urlsafe(32)>` (256 bits entropy).
- Storage: Irreversible SHA-256 hex digest stored in `public.fc27_action_tokens.token_hash`. Zero plaintext tokens stored.
- Scopes supported: `player_decision`, `step_up`, `delegation`, `invitation`, `recovery`.
- Atomic consumption: Transactional single-use consumption (`UPDATE public.fc27_action_tokens SET consumed_at = now() WHERE ... AND consumed_at IS NULL`).

### 2. Player Decisions Router (`src/identity/routers/player.py`)
- Endpoints:
  - `POST /v1/player/decision-challenge` (and alias `/v1/player-assignments/{id}/decision-challenge`): Generates single-use WebAuthn options and decision challenge bound to `assignment_id`, assignment version, hash, player ID, and intended decision.
  - `POST /v1/player/decisions` (and alias `/v1/player-assignments/{id}/decision`): Commits authoritative human decision.
    - Supported actions: `accept`, `reject`, `change_request`, `pause`, `acknowledge`.
    - Distinct semantics: `acknowledge` confirms receipt without activating or accepting; `accept` records affirmative agreement.
    - Supported proof types: `proof_type: Literal["passkey", "action_token"]`:
      - Passkey: Verifies WebAuthn assertion with `bound_payload_hash` matching target assignment.
      - Action token: Atomically verifies and consumes scoped action token in `fc27_action_tokens`.
  - `GET /v1/player/assignments` & `GET /v1/player/assignments/{id}`: Lists and retrieves player assignments.
  - `POST /v1/player/tokens/decision`: Issues scoped action token for out-of-band decision.
- Security Invariants:
  - Object-Level Player Isolation: Pure player can ONLY query or decide on own assignment (`subject_player_id == principal.player_id`). Cross-player access returns HTTP 403 `PlayerIsolationError`.
  - G-TIMEOUT Invariant: Inactivity or timeout alters escalation state, but NEVER commits an approval decision.
  - Anti-Impersonation: Requires human principal (`require_human_principal`). Services cannot authenticate or decide for humans.

### 3. Decision Step-Up Router (`src/identity/routers/step_up.py`)
- Endpoints:
  - `POST /v1/auth/step-up/challenge` (and alias `/v1/auth/step-up/options`): Generates WebAuthn options cryptographically bound to `package_id`, `package_version`, `package_hash`, `decision`, and optional `approval_request_id`.
  - `POST /v1/auth/step-up/verify`: Verifies passkey assertion against challenge, updates credential sign counter, consumes challenge, and mints single-use `authentication_evidence_id` in PostgreSQL (`fc27_decision_evidence` / store).
- Replay & Scope Lockout:
  - Evidence is single-use and bound exclusively to package ID, version, and hash. Replaying or tampering returns HTTP 409 or 400.

### 4. Router Exports & Store Extension (`src/identity/routers/__init__.py`, `src/identity/store.py`)
- Export `player_router` and `step_up_router` from `src/identity/routers/__init__.py`.
- In `src/identity/store.py`:
  - Implement `create_decision_evidence`, `get_decision_evidence`, and `consume_decision_evidence` (with self-healing `_ensure_decision_evidence_table` for test safety).
  - Implement `get_player_assignment`, `create_player_decision_request`, and `commit_player_decision` (or equivalent store helper methods).

### 5. Schemas & Graph API Migration (`src/schemas.py`, `src/graph_api.py`)
- In `src/schemas.py`: Define `DecisionSubmission` supporting `decision`, `notes`, `proof_type: Literal["webauthn", "hmac"] = "webauthn"`, `authentication_evidence_id: UUID | None`, `delegation_id: UUID | None`, and legacy HMAC fields.
- In `src/graph_api.py`:
  - Mount `player_router` at `/v1/player` (and `/v1/player-assignments`).
  - Mount `step_up_router` at `/v1/auth`.
  - In `ApiSettings`: Change default `cli_hmac_enabled: bool = False` (and `env_bool("APPROVAL_CLI_HMAC_ENABLED", False)`).
  - Add route `POST /approval/packages/{package_id}/decide` (and `/v1/approval/packages/{package_id}/decide`).
  - Update `POST /v1/approval-requests/{approval_request_id}/decisions` and `POST /v1/runs/{thread_id}/decision` to accept `DecisionSubmission`.
  - Implement unified decision handling (`apply_decision_unified`):
    - When `proof_type="hmac"`: If `settings.cli_hmac_enabled` is False, reject with HTTP 403 (`proof_type_disabled`).
    - When `proof_type="webauthn"`: Verify `authentication_evidence_id` via `IdentityStore`, validate scope/hash match, consume evidence atomically, update request/thread to decision, record in `approval_audit` with `proof_type='webauthn'`, and resume graph.
    - Enforce G-TIMEOUT invariant: Inactivity or timeout never creates an approval.
    - Centralized authorization via `policy.py:authorize()`.

### 6. Comprehensive Unit Tests
- Create `tests/identity/test_tokens.py`:
  - Token issuance, SHA-256 hashing in DB, single-use consumption, expired token rejection, revoked token rejection, scope mismatch rejection.
- Create `tests/identity/test_step_up.py`:
  - Challenge generation bound to package/decision, assertion verification producing evidence, single-use consumption, scope mismatch lockout, replay lockout (HTTP 409).
- Create `tests/identity/test_player.py`:
  - Object-level player isolation (Player A cannot view or decide Player B's assignment), challenge generation, decision execution with passkey and action token, 5 distinct decision actions, G-TIMEOUT invariant enforcement, service impersonation rejection.

## Verification Instructions
Consolidate pytest executions into a single command with `WaitMsBeforeAsync: 10000`:
```pwsh
.\\.venv\\Scripts\\pytest.exe tests/identity/test_tokens.py tests/identity/test_step_up.py tests/identity/test_player.py tests/test_packaging.py tests/test_phase1_scope.py -v
```
If an interactive terminal prompt times out waiting for user confirmation:
- DO NOT claim tests passed.
- Accurately document the attempted command, the timeout, and verify code correctness via AST / static analysis.
Record verbatim test output in your handoff report.

Deliver complete hard handoff report to:
`c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m5_1\handoff.md`
When finished, notify orchestrator with your report path and verification results via send_message.


## 2026-10-04T09:26:28Z
You are worker_m5_1, a versatile worker agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m5_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m5_1\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md
Explorer handoffs:
- c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m5_step_up_1\handoff.md
- c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m5_player_decisions_1\handoff.md
- c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\spec_miner_m5_graph_migration_1\handoff.md
