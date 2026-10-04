# Project: Player-Dispatch Programme — Phase 1: Secure Data Foundation

## Architecture
Phase 1 establishes the authoritative PostgreSQL database foundation, append-only event/audit log, versioned recommendation and approval lifecycle, LangGraph persistence, and least-privileged persistent Graph API.

```
                           ┌──────────────────────────────────────────────┐
                           │               HTTP Clients / CLI             │
                           └──────────────────────┬───────────────────────┘
                                                  │
                                                  ▼
                           ┌──────────────────────────────────────────────┐
                           │       Graph API (FastAPI, port 8001)         │
                           │           Role: fc27_app (DML only)          │
                           │           DISPATCH_ENABLED = false           │
                           │  - POST /v1/runs                             │
                           │  - GET /v1/runs/{thread_id}                  │
                           │  - GET /v1/runs/{thread_id}/approval-request │
                           │  - POST /v1/approval-requests/{id}/decisions │
                           │  - POST /v1/runs/{thread_id}/resume          │
                           │  - GET /health/live, GET /health/ready       │
                           │  - GET /v1/config/dispatch                   │
                           └──────────────┬───────────────────────────────┘
                                          │
                   ┌──────────────────────┴──────────────────────┐
                   ▼                                             ▼
┌──────────────────────────────────────┐     ┌──────────────────────────────────────┐
│        LangGraph Core Engine         │     │            Approval Store            │
│  - Exactly ONE approval interrupt    │     │  - Strict lock order (nonce->req->th)│
│  - Approvable package persisted      │     │  - Expiry daemon (72h TTL)           │
│  - Resume ignores payload            │     │  - Idempotent replay safe            │
│  - Re-reads committed PostgreSQL DB  │     │  - Authority-basis validation        │
│  - Terminal states:                  │     │  - Atomic events & outbox emission   │
│    "APPROVED, NOT DISPATCHED"        │     └──────────────────┬───────────────────┘
│    "REJECTED, NO ACTION TAKEN"       │                        │
└──────────────────┬───────────────────┘                        │
                   │                                            │
                   ▼                                            ▼
┌───────────────────────────────────────────────────────────────────────────────────┐
│                           PostgreSQL 16 Database                                  │
│  - Roles: fc27_owner (NOLOGIN), fc27_migrator (LOGIN), fc27_app, fc27_dispatch   │
│  - Tables: graph_threads, recommendation_packages, approval_requests,             │
│    approval_audit, approval_nonces, domain_events, outbox, players, dispatches,   │
│    authority_policies, authority_delegations, player_assignments,                 │
│    activation_gate_log, activation_gates view, checkpoints                        │
│  - Single write transition function: public.fc27_dispatch_transition (SECURITY DEF)│
│  - Triggers: Append-only triggers, transition guards, pair consistency check      │
└───────────────────────────────────────────────────────────────────────────────────┘
```

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | Role Bootstrap | Creates `fc27_owner` (NOLOGIN), `fc27_migrator`, `fc27_app`, `fc27_dispatch` roles, transfers legacy ownership, revokes public CREATE | M0 (Done) | Task 1 |
| 2 | Versioned Migration Runner | Applies SQL migrations up to target version under `SET ROLE fc27_owner`, runs checkpoint setup, applies versioned grants policy | M0 (Done) | Task 2 |
| 3 | Grants Policy Versioning | Enforces that `migrations/post/grants.sql` matches `GRANTS_POLICY` version and SHA-256 | M0 (Done) | Task 2, C-6 |
| 4 | Event Index & Outbox | Atomically inserts `domain_events` and delivery `outbox` records in single transaction | M0 (Done) | Task 3, Spec §14.5 |
| 5 | Event Source Guard | Trigger verifying that mandatory sourced events reference valid existing database records | M0 (Done) | Task 3, Spec §14.5 |
| 6 | Transition Event Source Identity | Restricts transition-only events (`dispatch.*`, `plan.*`, `tracking.*`) to owner-owned definer function calls | M0 (Done) | Task 3, C-10 |
| 7 | Append-Only Table Guards | Triggers preventing `UPDATE` and `DELETE` on immutable audit/event tables | M0 (Done) | Task 3, Spec §14 |
| 8 | Approval Requests Lifecycle | Manages `approval_requests` lifecycle (`awaiting_approval`, `approved`, `rejected`, `revision_requested`, `expired`) | M1 | Task 4, Spec §14.2 |
| 9 | Approval Pair Consistency Trigger | Deferred constraint trigger ensuring `graph_threads` and `approval_requests` statuses and package versions match | M1 | Task 4, Spec §7.6 |
| 10 | Approval Inconsistencies View | View exposing any discrepancy between `graph_threads` and `approval_requests` | M1 | Task 4, Spec §7.6 |
| 11 | Approval Expiry Daemon | Evaluates and marks expired approval requests and threads in paired transaction, revoking nonces | M1 | Task 4, Spec §7.4 |
| 12 | Single-Use Decision Nonce | Issues time-limited (max 300s) nonce bound to thread, package version, and payload SHA-256 | M1 | Task 4, Spec §14.2 |
| 13 | Nonce Subject Typing | Allows nonces for `thread`, `dispatch_plan`, `dispatch_cancel`, `role_grant`, and `activation_gate` | M1 | Task 4, C-4 |
| 14 | Authoritative Decision Commit | Validates HMAC signature, consumes nonce, updates request & thread status, writes `approval_audit`, domain event, outbox atomically | M1 | Task 4, Spec §7.2 |
| 15 | Request Revision Decision | Terminal decision `REQUEST_REVISION` recording reason notes without modifying original package | M1 | Task 4, Spec §7.3 |
| 16 | Human Authority Schema Compatibility | Compatibility schema for club authority, delegated authority, player consent, and fallback | M1 | Patch §1A, Agent A |
| 17 | Authority Basis & Actor Separation | Fields and constraints separating `actor_type`, `actor_id`, `authority_role`, `authority_basis`, `decision_scope` | M1 | Patch §1A, Agent A |
| 18 | Single Approval Interrupt | Pauses LangGraph execution at approval node until database decision committed; no double interrupt | M2 | Task 5, §4.1 |
| 19 | Authoritative Decision Re-read | Upon resume, Graph re-reads committed PostgreSQL decision rather than trusting resume input | M2 | Task 5, §4.4 |
| 20 | Terminal States Enforcement | Formats terminal states as `APPROVED, NOT DISPATCHED` or `REJECTED, NO ACTION TAKEN` | M2 | Task 5, §4.5 |
| 21 | Correlation Middleware | FastAPI middleware for `X-Correlation-Id` validation, echo, and propagation to events and logs | M2 | Task 5, C-5 |
| 22 | Graph API `POST /v1/runs` | Creates durable run record and initiates graph execution with `dispatch_enabled=false` | M2 | Task 5, §5.1 |
| 23 | Graph API `GET /v1/runs/{id}` | Returns safe public projection of graph run omitting internal secrets and checkpoint blobs | M2 | Task 5, §5.2 |
| 24 | Graph API `GET /v1/runs/{id}/approval-request` | Returns approval request state and display-safe candidate summary | M2 | Task 5, §5.3 |
| 25 | Graph API Decision Route | Processes authoritative Commissioner decision, committing DB before resuming graph | M2 | Task 5, §5.4 |
| 26 | Graph API Resume Route | Restricted wake-up endpoint; triggers graph resume without taking authorization payload | M2 | Task 5, §5.5 |
| 27 | Health Probes (Live & Ready) | `/health/live` process check; `/health/ready` checking DB connectivity, posture, checkpointer, `DISPATCH_ENABLED=false` | M2 | Task 5, §5.6, §5.7 |
| 28 | HMAC Kill Switch | Disables CLI HMAC proof verification when `APPROVAL_CLI_HMAC_ENABLED=false` | M2 | Task 5, Spec §18.6 |
| 29 | Actor-Neutral Decision Validation | Validates decision actor and authority basis in Graph and API without hardcoding `commissioner_input` | M2 | Patch §1A, Agent B/C |
| 30 | Dispatches & Player Schema | Schema for `players` and `dispatches` with strict checks and immutable provenance triggers | M3 | Task 6, Spec §14.3 |
| 31 | Tracking Forward Compatibility Schema | Forward-compatible tracking and observation tables (`dispatch_plans`, `player_observations`, `dispatch_assessments`) | M3 | Task 6, Spec §14.4 |
| 32 | Single Write Path Transition Function | SECURITY DEFINER function `fc27_dispatch_transition` executing allowed state machine transitions | M3 | Task 7, Spec §11.3 |
| 33 | Transition Rule Matrix | Table `dispatch_transition_rules` defining permissible transitions by DB role and actor kind | M3 | Task 7, Spec §11.3 |
| 34 | Objective Bounds Policy | Bounded metric target deltas per versioned policy `fc27-objective-bounds-v1` | M4 | Task 8, Spec §12 |
| 35 | Activation Gate Evaluation | Evaluates rollout gates (`G-MIGRATION`..`G-OUTBOX`), failing closed when any gate fails | M4 | Task 8, Spec §31.2 |
| 36 | Gate Challenge & Record Routes | Records manual activation gates via deployment-bound nonce and HMAC signature; refuses `G-OUTBOX` | M4 | Task 8, Spec §31.2, C-4 |
| 37 | Atomic Approval-to-Dispatch Creation | Creates pending dispatch row inside the approval transaction when `dispatch_effective` is true | M4 | Task 8, Spec §7.2 |
| 38 | Compose Topology & File Secrets | Configures `db-bootstrap`, `db-migrate`, `graph-api` with least-privilege roles, `./secrets/` files, no passwords in env | M5 | Task 9, Spec §24 |
| 39 | Configuration Standardization | Canonical configuration variables (`POSTGRES_URI`, `GRAPH_URL`, `FC27_MCP_URL`, `DISPATCH_ENABLED=false`) | M5 | Task 9, §6 |
| 40 | Scope Protection & Doc Corrections | Scope enforcement test `test_phase1_scope.py`, documentation updates with required Phase 1 wording | M5 | Task 9, §9 |
| 41 | E2E Security & Invariants Suite | Full test suite verifying 100% Phase 1 acceptance criteria, bypass attempts, least privilege, no dispatch | M6 | Task 10, §7, §8 |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | Authoritative Approval Lifecycle & Expiry | 0003 Part B, `approval_requests`, immutable transitions, pair consistency trigger, `approval_inconsistencies` view, store methods, human authority compatibility fields (Section 1A), grants update | none (Tasks 1-3 complete) | DONE (Verified: 87 core tests + 21 adversarial tests pass, Reviewers APPROVE, Auditor CLEAN) |
| M2 | Graph Core & Persistent Graph API | Single interrupt verification, Postgres re-read resume, terminal states formatting, correlation middleware, `/v1/` routes, safe projection, health probes, actor-neutral validation | M1 | DONE (Verified: 58 persistence/flow, 55 API v1, 20 challenger persistence, 79 challenger API & lifecycle tests pass, Reviewers APPROVE, Auditor CLEAN) |
| M3 | Dispatch Schema & Single-Write-Path Transition | 0003 Part C (`players`, `dispatches`, tracking schema), Part D (`public.fc27_dispatch_transition` SECURITY DEFINER, transition rules matrix, Python wrapper, event source check) | M1 | DONE (Verified: 22 transition, 48 schema/roles, 144 regression tests pass, Reviewers APPROVE, Challengers APPROVE, Auditor CLEAN) |
| M4 | Activation Gates & Atomic Approval-to-Dispatch | 0003 Part E (`activation_gate_log`, `activation_gates` view), objective bounds `fc27-objective-bounds-v1`, gate evaluation, atomic dispatch creation in approval txn | M2, M3 | DONE (Verified: 24 creation/gate tests pass, contract regression resolved, Reviewers APPROVE, Challengers APPROVE, Auditor CLEAN) |
| M5 | Compose Topology, Secrets & Documentation | Docker Compose secret files, `db-migrate` job as `fc27_migrator`, `graph-api` running as `fc27_app`, configuration standardization, documentation updates, scope check `test_phase1_scope.py` | M4 | DONE (Verified: 16 compose isolation & 5 scope tests pass, Reviewers APPROVE, Challengers APPROVE, Auditor CLEAN) |
| M6 | Final Verification, E2E Suite & Phase 2 Handoff | Pass 100% of E2E test suite (Tiers 1-4), adversarial bypass tests, DB least privilege tests, complete acceptance trace in `TEST_RESULTS.md`, Phase 1 completion report, Phase 2 implementation plan | M5, TEST_READY.md | DONE (Verified: 81 E2E tests, 100% trace mapping AC-1..AC-58, C-1..C-10, Reviewers APPROVE, Challengers APPROVE, Auditor CLEAN) |

## Interface Contracts

### ApprovalStore ↔ Graph API / Graph Core
- `ApprovalStore.complete_run(thread_id, state) -> dict`:
  - Inserts `approval_requests` row (`status='awaiting_approval'`, `expires_at = now() + request_ttl`).
  - Updates `graph_threads` linking `approval_request_id`.
  - Emits `approval.created` domain event + outbox row.
  - Returns state dict with `approval_request_id` and `approval_expires_at` (ISO).
- `ApprovalStore.apply_decision(thread_id, signed, principal, signing_secret, *, correlation_id, dispatch_enabled, dispatch_creator) -> dict`:
  - Lock order: `approval_nonces` (thread nonces) → `approval_requests` (`FOR UPDATE`) → `graph_threads`.
  - Validates: nonce unconsumed/unrevoked, request awaiting/not expired, hash & version match, authority basis permitted.
  - Commits `approval_audit`, updates request/thread status, emits `approval.decided` + outbox row.
  - If identical replay: safely returns existing decision without error.
  - If conflicting decision: raises `StoreError(409)`.
- `ApprovalStore.expire_due(*, thread_id=None, limit=100) -> list[str]`:
  - Scans for `expires_at <= now()` with status `awaiting_approval`.
  - Atomically marks request & thread `expired`, revokes open nonces, emits `approval.expired`.

### Graph Core ↔ PostgreSQL
- Interruption: `interrupt(state["approval_request"])` called exactly once inside `approval` node.
- Resume: `deps.decision_reader(thread_id)` re-reads PostgreSQL `approval_audit` / `graph_threads`.
  - Validates: committed decision exists, matches `package_version` and `approval_request_id`.
  - Raises `ApprovalNotCommitted` if missing, stale, or mismatched.
  - Formats terminal state: `"APPROVED, NOT DISPATCHED"` or `"REJECTED, NO ACTION TAKEN"`.

### Database Security Definer Transition Function
- `public.fc27_dispatch_transition(p_dispatch_id uuid, p_expected_status text, p_expected_version int, p_target_status text, p_actor text, p_correlation_id uuid, p_reason text, p_patch jsonb) -> int`:
  - SECURITY DEFINER, `search_path = pg_catalog, pg_temp`.
  - Grants: `GRANT EXECUTE ON FUNCTION public.fc27_dispatch_transition TO fc27_app, fc27_dispatch`.
  - Returns new version integer on success.
  - Raises SQLSTATEs `FC001` (conflict), `FC002` (illegal transition), `FC003` (patch invalid), `FC004` (actor invalid), `FC005` (null correlation ID), `FC006` (missing precondition), `FC007` (reason too long).

## Code Layout
- `src/db_roles.py`: Role definitions, cluster/database bootstrap, posture check, `redact_conninfo()`.
- `src/db_migrate.py`: Migration runner as `fc27_owner`, checkpoint setup, `GRANTS_POLICY` versioning.
- `src/correlation.py`: `correlation_id_var`, `CorrelationIdFilter`, FastAPI correlation middleware.
- `src/events.py`: `record_event()` helper inserting into `domain_events` and `outbox`.
- `src/approval_store.py`: Approval lifecycle, expiry daemon, challenge/decision handling, authority validation.
- `src/decision.py`: Decision state helper without legacy dispatch dictionary.
- `src/graph.py`: LangGraph workflow with single interrupt, PostgreSQL decision re-read, terminal state wording.
- `src/graph_api.py`: FastAPI server with `/v1/` routes, safe projections, health checks, gate challenge/record.
- `src/schemas.py`: Core Pydantic schemas, `Objective` model, run request/response models.
- `src/dispatch/`:
  - `objective.py`: Objective bounds validation and candidate verification.
  - `transition.py`: Python wrapper for `fc27_dispatch_transition`.
  - `gates.py`: Activation gate evaluation.
  - `creation.py`: Atomic dispatch creation in approval transaction.
- `migrations/`:
  - `0001_initial.sql`, `0002_approval_security.sql`, `0003_dispatch_foundation.sql`.
  - `post/grants.sql`: Versioned least-privilege grants.
- `tests/`:
  - `conftest.py`: Database fixtures (`pg_admin_db_uri`, `pg_migrator_uri`, `pg_uri`, `pg_dispatch_uri`, `client`, `wired`).
  - `test_db_roles.py`, `test_db_migrate_roles.py`, `test_events_outbox.py`: DB infrastructure tests.
  - `test_migration_0003.py`, `test_approval_lifecycle.py`: Lifecycle, expiry, and consistency tests.
  - `test_dispatch_schema.py`, `test_dispatch_transition.py`, `test_dispatch_creation.py`: Dispatch schema tests.
  - `test_compose_isolation.py`, `test_phase1_scope.py`: Compose topology and scope protection tests.
  - `e2e/`: E2E Testing Track suite (Tiers 1-4).
