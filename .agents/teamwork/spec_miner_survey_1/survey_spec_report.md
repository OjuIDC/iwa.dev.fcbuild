# Specification and Requirements Survey: Phase 1 Secure Data Foundation & Programme Roadmap

**Author**: `spec_miner_survey_1` (Specification Miner)  
**Date**: 2026-10-03  
**Status**: Comprehensive Authoritative Spec Survey  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\spec_miner_survey_1`  
**Base Repository**: `iwa-ojuidc/iwa.dev.fcbuild`  
**Authoritative Documents**:
1. `ORIGINAL_REQUEST.md` (Approved Execution Brief: Phase 1 Secure Data Foundation)
2. `docs/superpowers/specs/2026-10-02-player-dispatch-design.md` (Architecture Specification v2.1 + AM-1..AM-15 + C-1..C-10)
3. `docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md` (Phase 1 Implementation Plan)
4. `docs/APPROVAL_SECURITY.md` & `docs/DEPLOYMENT_AND_SECURITY.md`
5. Existing Codebase: `migrations/`, `src/db_roles.py`, `src/db_migrate.py`, `src/events.py`, `src/approval_store.py`, `src/graph.py`, `src/graph_api.py`, `docker-compose.yml`

---

## 1. Executive Mission & Scope Constraints

### 1.1 Objective
Implement **Phase 1: Secure Data Foundation** for the player-dispatch programme. Phase 1 establishes the authoritative PostgreSQL database schema, append-only event/audit infrastructure, versioned recommendation and approval model, LangGraph sync checkpoint persistence, and a persistent Graph API with least-privileged database access.

### 1.2 Programme Guard & Required Statements
- Phase 1 is **NOT** the completed player-dispatch programme.
- At the Phase 1 boundary, implementation stops immediately.
- **Required Phase 1 Completion Statement**:
  > *"Secure data foundation complete. Dispatch remains disabled. No player delivery has occurred."*
- **Persistent Programme Status Checklist**:
  - [ ] Phase 1: Secure data foundation
  - [ ] Phase 2: Identity and authorization
  - [ ] Phase 3: Dispatch worker and player-agent pipeline
  - [ ] Phase 4: Dashboard UI and real-time delivery
  - [ ] Phase 5: Tracking and analytics
  - [ ] Phase 6: Operations, audit, and rollout
  *(When Phase 1 is accepted, only the Phase 1 box may be checked; checking Phase 1 must never imply overall programme completion).*

---

## 2. Non-Negotiable Phase 1 Invariants

These invariants are programme-level hard requirements that override any convenience shortcuts:

1. **`DISPATCH_ENABLED=false` at all times**: Evaluated at startup and every 60 seconds; production Phase 1 remains disabled.
2. **No Player Delivery**: No Phase 1 code path, service, or adapter may deliver anything to a player or player device.
3. **No Adapter Invocation**: No approval endpoint or graph node may call a player delivery adapter (dashboard storage or WebSocket delivery).
4. **No Active Dispatch Transitions in Graph**: No LangGraph node may transition to `dispatched`, `delivered`, `acknowledged`, `activated`, or `tracking`.
5. **Terminal Outcomes**: `approved` (`APPROVED, NOT DISPATCHED`) and `rejected` (`REJECTED, NO ACTION TAKEN`) are the only valid terminal Phase 1 outcomes. No wording such as "APPROVED and ACTIVE" or "dispatched" may appear.
6. **PostgreSQL Authority**: PostgreSQL is the sole authoritative system of record for approvable packages, approval requests, approval decisions, and authoritative domain/audit events.
7. **Workflow vs Authorization Separation**: LangGraph checkpoints represent execution workflow state, **not** authorization state. Checkpoints do not authorize anything.
8. **Untrusted Transports & Bodies**: Redis pub-sub, WebSocket messages, LLM outputs, request bodies, Graph state, and `Command(resume=...)` payloads are **not** authoritative approval evidence.
9. **Resume Wake-Up Semantics**: A resume request only wakes or re-enters the Graph; it cannot approve, reject, or convey authorization.
10. **Committed Decision Binding**: Only a valid committed PostgreSQL decision bound to the exact current package ID, version, and hash may produce an approved or rejected Graph terminal state.
11. **Package Immutability**: A recommendation package must not be modified in place after becoming approvable. Material changes require a new package version, hash, and a new approval lifecycle.
12. **Synthetic vs Production Gates**: Test-only synthetic satisfaction of activation gates is for testing only, not production authorization.
13. **No Production Activation in Phase 1**: No production dispatch activation may be enabled as part of Phase 1 acceptance.
14. **No Premature Completion Claims**: Phase 1 completion must never be described or documented as programme completion.

---

## 3. Detailed Phase 1 Requirements by Architectural Domain

### 3.1 Domain A: Authoritative Data Model & Migrations

#### A.1 `graph_runs` / `graph_threads` (Run & Thread State)
- **Table Reconciliation**: In the existing repository, `graph_threads` (created in `0001_initial.sql`, hardened in `0002_approval_security.sql`) serves as the durable run store.
- **Required Semantics**:
  - Unique thread/run identity: `thread_id text PRIMARY KEY CHECK (char_length(thread_id) BETWEEN 1 AND 128)`, formatted as `fc27-<clubId>-run-<YYYYMMDD>-<seq>`.
  - Correlation to club: `club_id text NOT NULL CHECK (club_id ~ '^[0-9]+$')`.
  - Durable status: `status text NOT NULL CHECK (status IN ('running', 'awaiting_approval', 'approved', 'rejected', 'revision_requested', 'expired', 'failed'))`. No dispatch/delivery states allowed.
  - Request hash: `request_hash text` for idempotency and duplicate request detection.
  - Linked approval request: `approval_request_id uuid UNIQUE REFERENCES public.approval_requests` (added in 0003).
  - Versioned lead package binding: `package_version text` (SHA-256 hex string). `package_version` becomes immutable once `status = 'awaiting_approval'`.
  - Decision metadata: `approved_by text`, `approved_at timestamptz`, `decision_payload jsonb`.
  - Enforced terminal-fields constraint: `(status IN ('approved','rejected','revision_requested')) = (approved_by IS NOT NULL AND approved_at IS NOT NULL AND decision_payload IS NOT NULL)`.
  - Database-level transition guard trigger: `fc27_guard_thread_transition()` blocks deletion or modification of terminal rows, blocks skipping `awaiting_approval`, and enforces immutability of `package_version`.

#### A.2 `recommendation_packages` (Versioned Immutable Candidates)
- **Required Semantics**:
  - Deterministic canonical serialization and payload hashing: SHA-256 over canonical JSON of lead packages (`package_version`).
  - Immutable package identity/version relationship: An existing approvable package cannot be silently overwritten or modified.
  - Structured objective model (`Objective`):
    - Metrics allowlist: `pass_accuracy`, `tackle_success`, `shot_accuracy`, `rating`, `goals`, `assists`.
    - Direction: `increase` | `decrease`.
    - Target delta: Bounded per metric per versioned policy `fc27-objective-bounds-v1` (D-30):
      - `pass_accuracy`, `tackle_success`, `shot_accuracy`: 1–25 percentage points.
      - `rating`: 0.1–2.0.
      - `goals`, `assists`: 1–5.
    - Observation window: `1 <= min_matches <= max_matches <= 10`.
    - Bounds policy: `"fc27-objective-bounds-v1"` stored in candidate payload and covered by `package_version`.
  - Candidate identity: `player_id` must be exact numeric string (`^[0-9]{1,20}$`) matching EA stable player ID; no gamertag fallback.
  - Non-dispatchable condition: Approving a candidate without structured objective or with gamertag identifier returns 409 `not_dispatchable`.

#### A.3 `approval_requests` (Authoritative Approval Lifecycle)
- **Table Schema**:
  - `approval_request_id uuid PRIMARY KEY DEFAULT gen_random_uuid()`
  - `thread_id text UNIQUE NOT NULL REFERENCES graph_threads`
  - `package_version text NOT NULL CHECK (package_version ~ '^[0-9a-f]{64}$')`
  - `status text NOT NULL CHECK (status IN ('awaiting_approval', 'approved', 'rejected', 'revision_requested', 'expired'))`
  - `created_at timestamptz NOT NULL DEFAULT now()`
  - `expires_at timestamptz NOT NULL CHECK (expires_at > created_at)`
  - `decided_at timestamptz`, `expired_at timestamptz`, `version int NOT NULL DEFAULT 1`
  - Consistency CHECKs: `((status = 'expired') = (expired_at IS NOT NULL))` and `((status IN ('approved','rejected','revision_requested')) = (decided_at IS NOT NULL))`.
- **Approval Pair Constraint Trigger (`fc27_check_approval_pair`)**:
  - `DEFERRABLE INITIALLY DEFERRED` constraint trigger on both `graph_threads` and `approval_requests`.
  - Checks at commit that status matches, `package_version` matches, and any `awaiting_approval` thread with a `package_version` has exactly one linked request.
- **Approval Inconsistencies View (`fc27_approval_inconsistencies`)**:
  - Reconciles `status_mismatch`, `package_mismatch`, `unlinked_request`, `missing_link`, and `legacy_unlinked`.
  - Checked on graph-api startup and on `/health`. Any non-legacy mismatch sets health to `degraded` and returns 409 `inconsistent_state` for decisions.
- **Approval Expiry Semantics (AM-4)**:
  - Default TTL: 72 hours (`APPROVAL_REQUEST_TTL_HOURS`).
  - `ApprovalStore.expire_due()`: Handles expired requests in single transactions (locks nonces, locks request, locks thread, sets both to `expired`, revokes unconsumed nonces, records `approval.expired`).
  - Evaluated lazily in `issue_challenge` and `apply_decision`, and proactively via background task every 60s.
  - Expired approvals fail closed with 409 `approval_expired`. Checkpoints are retained for audit with `expired` status; never resumed.

#### A.4 `approval_decisions` / `approval_audit` & `approval_nonces`
- **Authoritative Decisions Store (`approval_audit`)**:
  - `id bigserial PRIMARY KEY`
  - `thread_id text NOT NULL REFERENCES graph_threads`
  - `decision text NOT NULL CHECK (decision IN ('APPROVE', 'REJECT', 'REQUEST_REVISION'))`
  - `approved_by text NOT NULL` (e.g. `commissioner:<id>`)
  - `nonce text UNIQUE REFERENCES approval_nonces(nonce)`
  - `package_version text NOT NULL`
  - `signature_sha256 text NOT NULL`
  - `proof_type text NOT NULL DEFAULT 'hmac' CHECK (proof_type IN ('hmac', 'webauthn'))`
  - `actor_user_id uuid NULL` (Phase 2 hook)
  - `webauthn_credential_id bytea NULL` (Phase 2 hook)
  - `correlation_id uuid NULL`
  - `approval_request_id uuid NULL REFERENCES approval_requests`
  - `owner text NULL`, `selection text NULL`, `candidate_id text NULL`, `player_id text NULL`, `notes text NOT NULL DEFAULT ''`
  - `decided_at timestamptz NOT NULL DEFAULT now()`
  - Append-only trigger: `fc27_approval_audit_immutable` rejects any `UPDATE` or `DELETE`.
  - Unique constraint: `approval_audit_one_decision_per_thread ON approval_audit(thread_id)`.
- **Single-Use Nonces (`approval_nonces`)**:
  - `nonce text PRIMARY KEY CHECK (char_length(nonce) >= 32)`
  - `subject_type text NOT NULL DEFAULT 'thread' CHECK (subject_type IN ('thread', 'dispatch_plan', 'dispatch_cancel', 'role_grant', 'activation_gate'))`
  - `thread_id text NULL REFERENCES graph_threads(thread_id)`
  - `dispatch_id uuid NULL`, `user_id uuid NULL`, `gate_id text NULL`
  - `payload_sha256 text NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$')`
  - `package_version text NULL`
  - `issued_at timestamptz NOT NULL`, `expires_at timestamptz NOT NULL CHECK (expires_at > issued_at)`
  - `consumed_at timestamptz`, `revoked_at timestamptz`, `created_by text NOT NULL`
  - TTL capped at 300 seconds (`MAX_APPROVAL_TTL_SECONDS`).
- **Decision Validation Rules**:
  - Nonexistent approval request: reject (404/409).
  - Wrong package, version, or hash: reject (409).
  - Expired or invalidated request: reject (409 `approval_expired`).
  - Consumed, revoked, or expired nonce: reject (409).
  - Duplicate identical idempotent submission: safe return.
  - Conflicting second terminal decision: reject (409 / unique constraint).

#### A.5 Audit & Domain Event Infrastructure (`domain_events`)
- **Schema**:
  - `event_id bigserial PRIMARY KEY`
  - `event_type text NOT NULL`
  - `entity_type text NOT NULL` (e.g. `approval_request`, `dispatch`, `activation_gate`)
  - `entity_id text NOT NULL`, `entity_version int`
  - `actor text NOT NULL` (e.g. `commissioner:<id>`, `service:graph-api`, `service:dispatch-worker`)
  - `from_status text`, `to_status text`, `reason text`
  - `correlation_id uuid NOT NULL`
  - `occurred_at timestamptz NOT NULL DEFAULT now()`
  - `source_table text CHECK (source_table IN ('approval_audit','approval_requests','dispatches','dispatch_acknowledgments','dispatch_assessments','dispatch_plans','dispatch_plan_decisions','dispatch_delivery_attempts','user_roles','activation_gate_log'))`
  - `source_id text` (e.g. `<audit_id>`, `<approval_request_id>`, `<dispatch_id>:<plan_version>`)
  - `scope_club_id text`, `scope_player_id text`
  - `visibility text NOT NULL CHECK (visibility IN ('staff','player_owner','audit_only','ops'))`
  - `fc_version text NOT NULL DEFAULT '27'`, `platform text NOT NULL DEFAULT 'common-gen5'`
  - `policy_version text`, `code_revision text`
  - Check constraint: `CHECK ((source_table IS NULL) = (source_id IS NULL))`
- **Append-Only Trigger**: `fc27_domain_events_immutable` blocks all `UPDATE` and `DELETE`.
- **Event Source Integrity Trigger (`fc27_check_event_source`)**:
  - Enforces that mandatory sourced event types link to an existing row in `source_table`.
  - Enforces entity ownership (source row belongs to the target entity).
  - Enforces transition-only event restrictions (C-10): `dispatch.*` (except `dispatch.created`), `plan.*`, and `tracking.*` events may only be written when `current_user = 'fc27_owner'` AND `session_user IN ('fc27_app', 'fc27_dispatch')`, guaranteeing they originate strictly from the SECURITY DEFINER transition function.

#### A.6 Transactional Outbox Boundary (`outbox`)
- **Schema**:
  - `outbox_id bigserial PRIMARY KEY`
  - `domain_event_id bigint NOT NULL UNIQUE REFERENCES domain_events(event_id)`
  - `channel text NOT NULL DEFAULT 'fc27:events'`
  - `created_at timestamptz NOT NULL DEFAULT now()`
  - `published_at timestamptz`, `attempts int NOT NULL DEFAULT 0`, `last_error text`, `locked_until timestamptz`
- **Data Minimization (AM-10)**: Outbox rows contain NO payload column. When relayed (Phase 3), the message is strictly `{"event_id": <int>}`.
- **Phase 1 Boundary**: Phase 1 inserts outbox rows atomically with events. **NO outbox relay worker or Redis publisher runs in Phase 1.** Outbox rows accumulate unpublished until Phase 3.

#### A.7 Dispatches & Transition Schema (Phase 1 Data Foundation)
- **`players` Table**:
  - `player_id text PRIMARY KEY CHECK (player_id ~ '^[0-9]{1,20}$')`
  - `club_id text NOT NULL CHECK (club_id ~ '^[0-9]+$')`
  - `platform text NOT NULL DEFAULT 'common-gen5' CHECK (platform = 'common-gen5')`
  - `display_name text CHECK (char_length(display_name) <= 64)`
  - `active boolean NOT NULL DEFAULT true`
  - `identity_verified_at timestamptz`, `identity_evidence_ref text`
  - `created_at timestamptz NOT NULL DEFAULT now()`, `version int NOT NULL DEFAULT 1`
- **`dispatches` Table**:
  - `dispatch_id uuid PRIMARY KEY DEFAULT gen_random_uuid()`
  - `source_thread_id text NOT NULL REFERENCES graph_threads`
  - `approval_id bigint UNIQUE NOT NULL REFERENCES approval_audit(id)`
  - `approval_request_id uuid NOT NULL REFERENCES approval_requests`
  - `approval_payload_hash text NOT NULL`
  - `player_id text NOT NULL REFERENCES players`
  - `candidate_id text NOT NULL`, `owner text NOT NULL`, `selection text NOT NULL`, `club_id text NOT NULL`
  - `approved_instruction jsonb NOT NULL`, `plan_version int NOT NULL DEFAULT 0`
  - `delivery_channels text[] NOT NULL DEFAULT '{dashboard, websocket}'`
  - `dispatch_status text NOT NULL CHECK (dispatch_status IN ('pending','queued','delivered','requires_reapproval','failed','acknowledged','tracking','completed','expired','cancelled','superseded'))`
  - `version int NOT NULL DEFAULT 1`
  - Partial Unique Index: `(player_id, owner, (approved_instruction->'objective'->>'metric')) WHERE dispatch_status NOT IN ('completed','expired','cancelled','superseded')`
  - Triggers: Insert guard forces `dispatch_status = 'pending'`, `version = 1`, timestamps NULL. Immutability trigger freezes provenance columns.
- **Single Write Path: `fc27_dispatch_transition` (AM-1, §11.3)**:
  - `LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp`
  - Owner: `fc27_owner` (NOLOGIN). `REVOKE ALL ON FUNCTION FROM PUBLIC`; `GRANT EXECUTE TO fc27_app, fc27_dispatch`.
  - Static SQL only (no dynamic SQL, no `EXECUTE`, no identifier formatting).
  - Explicit error SQLSTATEs: `FC001` (version/status conflict), `FC002` (illegal edge), `FC003` (patch key/type invalid), `FC004` (actor invalid), `FC005` (NULL correlation ID), `FC006` (missing precondition), `FC007` (reason too long).
  - Atomically increments version, writes static column updates, inserts `domain_events` and `outbox`.

#### A.8 Database Roles, Privileges & Ownership (AM-1, §14.0, §14.7)
- **`fc27_owner`**: NOLOGIN role. Owns all schema objects, tables, views, sequences, triggers, and functions.
- **`fc27_migrator`**: LOGIN role, member of `fc27_owner`. Used solely by the one-shot `db-migrate` Compose job. Runs `SET ROLE fc27_owner` to apply migrations and LangGraph `PostgresSaver.setup()`.
- **`fc27_app`**: LOGIN role. Used by `graph-api`. DML only (`SELECT, INSERT, UPDATE` on threads/requests/nonces; `SELECT, INSERT` on audit/dispatches/events; `INSERT` on outbox). Has NO DDL rights, NO `CREATE` privilege on schema `public`, is NOT superuser, and is NOT a member of `fc27_owner`.
- **`fc27_dispatch`**: LOGIN role. Reserved for Phase 3 dispatch worker. Restricted DML on worker tables; NO rights on approval, identity, session, or checkpoint tables.
- **Postgres Superuser**: Used strictly once during initial bootstrap (`src/db_roles.py`). No application service connects as superuser at runtime.

---

### 3.2 Domain B: Graph Core

#### B.1 Single Approval Interruption Point
- **Remove Double Interruption**: Remove any `interrupt_before=["approval"]` configuration in graph compilation.
- **Single Point**: Retain exactly one explicit `interrupt(state["approval_request"])` call inside the `approval` node.
- **Acceptance Requirement**: Test proof that exactly one approval interrupt occurs per approval lifecycle.

#### B.2 Package Creation Before Interruption
- The node sequence leading to approval:
  1. Generate recommendations (`search` -> `scout` -> `analyst` -> `coach` -> `tactics` -> `director`).
  2. `prepare_approval`: Build approvable lead packages, canonicalize structured JSON, compute deterministic hash `package_version`, structure display-safe `approval_request`.
  3. Enter `awaiting_approval`.
  4. Persist immutable state & checkpoints via synchronous PostgreSQL saver.
  5. Call `interrupt(approval_request)`.
- No LLM narrative may serve as authorization evidence.

#### B.3 Display-Safe Interrupt Payload
- The interrupt payload passed to `interrupt()` contains only:
  - `thread_id`
  - `approval_request_id` (or linkable request)
  - `package_version` (hash)
  - `approvable_owners`
  - `fc_version` & `platform`
- Forbidden from interrupt payload: credentials, tokens, secrets, unredacted internal state, or hidden authorization tokens.

#### B.4 Strict Resume Semantics & Re-Reading Authoritative State
- **Critical Security Barrier**:
  - `Command(resume=...)`, `POST /v1/runs/{thread_id}/resume`, or request bodies containing `{"approved": true}`, `{"decision": "approved"}`, or `commissioner_input="APPROVE"` **MUST NOT** authorize the Graph.
  - Resume is merely a wake-up signal that causes the `approval` node to re-enter.
  - Upon re-entering, the `approval` node re-reads the committed decision directly from PostgreSQL using `deps.decision_reader(thread_id)`.
  - The node validates:
    1. A committed decision exists in PostgreSQL.
    2. Decision status is in `('approved', 'rejected', 'revision_requested')`.
    3. The committed decision's `package_version` matches the thread's exact `package_version`.
  - If no committed decision exists, or if there is a mismatch/stale version: raise `ApprovalNotCommitted`, fail closed, and do not advance.

#### B.5 Terminal Graph States
- Approved: `APPROVED, NOT DISPATCHED`.
- Rejected: `REJECTED, NO ACTION TAKEN`.
- Revision requested: `REVISION_REQUESTED`.
- **Prohibited**: Wording such as "APPROVED and ACTIVE" or "dispatched to player".
- **Edge Rule**: No Phase 1 graph edge may connect approval to dispatch or execution.

#### B.6 Failure Ordering & Resilience
- **Strict Execution Order**:
  1. Authenticate & authorize actor.
  2. Validate approval request and exact package version/hash.
  3. Write authoritative decision to `approval_audit`.
  4. Write domain event (`approval.decided`) and outbox entry.
  5. **COMMIT** PostgreSQL transaction.
  6. Request Graph resume (`graph_runner.resume(thread_id)`).
  7. Graph re-reads committed decision and transitions to terminal state.
- **Resume Failure Resilience**:
  - If Graph resume fails after database commit (e.g., worker crash, network timeout), the committed decision in PostgreSQL remains authoritative and immutable.
  - The decision must **not** be rolled back.
  - Startup reconciliation re-resumes all unfinalized terminal threads idempotently.

---

### 3.3 Domain C: Persistent Graph API

The persistent Graph API provides the HTTP boundary around the Graph runner using versioned routes.

#### C.1 `POST /v1/runs`
- **Purpose**: Create and start a Graph run.
- **Input**:
  - `club_id`: string of ASCII digits (`^[0-9]{1,12}$`).
  - `thread_id`: optional caller-provided thread ID (`^[A-Za-z0-9-]+$`). If provided, validated against club ID format. If omitted, allocated atomically via `run_sequences`.
  - `platform`: `common-gen5`.
  - `fc_version`: `27`.
  - `match`: optional match metadata.
  - `trigger`: string (`^[a-z_]{1,32}$`).
- **Validation & Security**:
  - Strict Pydantic schema validation (`extra="forbid"`).
  - Idempotency & duplicate protection: existing thread ID returns 409 (reruns require a new thread ID).
  - Prevents caller from injecting approval state or package overrides.
  - Response always returns `dispatch_enabled=false`.

#### C.2 `GET /v1/runs/{thread_id}`
- **Purpose**: Return authorized, safe run projection.
- **Response Fields**: `thread_id`, `status`, `current_node`, `package_version`, `approval_request_id`, `errors`, `dispatch_enabled=false`.
- **Legacy Compatibility**: If a legacy run contains a `dispatch` dictionary, it is projected as `legacy_selection_record` with note `"recorded selection only; never delivered"`.
- **Data Protection**: Secrets, model prompt internals, and raw checkpoint blobs are omitted.

#### C.3 `GET /v1/runs/{thread_id}/approval-request`
- **Purpose**: Fetch approval request details for a run.
- **Response Fields**:
  - `approval_request_id`
  - `thread_id`
  - `status` (`awaiting_approval`, `approved`, `rejected`, `revision_requested`, `expired`)
  - `package_version` (hash)
  - `display_safe_summary`: approvable candidate list, structured objectives, category/owner.
  - `expires_at`, `created_at`

#### C.4 `POST /v1/approval-requests/{approval_request_id}/decisions`
- **Purpose**: Authoritative Phase 1 decision-write path.
- **Execution Steps**:
  1. Authenticate caller (Commissioner role via CLI HMAC or bootstrap credential).
  2. Validate decision schema (`decision`: `APPROVE`, `REJECT`, or `REQUEST_REVISION`; `notes`).
  3. Begin PostgreSQL transaction.
  4. Lock and validate `approval_requests` row (`FOR UPDATE`, status `awaiting_approval`, `expires_at > now()`).
  5. Validate package version and hash binding.
  6. Enforce idempotency: replayed identical decision is safe; conflicting decision fails with 409.
  7. Persist terminal decision to `approval_audit`.
  8. Update `approval_requests.status` and `graph_threads.status` synchronously.
  9. Persist `domain_events` (`approval.decided`) and `outbox` records.
  10. **COMMIT** transaction.
  11. Trigger Graph resume.
- **Response**: Details of decision, terminal status, and explicit disabled dispatch notice:
  ```json
  {
    "status": "approved",
    "dispatch": {
      "enabled": false,
      "created": false,
      "message": "Dispatch is disabled. This approval records the selection only; nothing was delivered to any player."
    }
  }
  ```

#### C.5 `POST /v1/runs/{thread_id}/resume`
- **Purpose**: Restricted internal wake-up route.
- **Constraint**: **MUST NOT** accept an approval or rejection parameter. Request body cannot carry authorization. Invokes Graph resume to re-read PostgreSQL.

#### C.6 Health Probes
- **`GET /health/live`**: Process liveness probe only (returns 200 if process is running).
- **`GET /health/ready`**: Readiness probe verifying:
  - PostgreSQL connectivity and latency.
  - Database role posture check (`fc27_app` is not superuser, not owner member, has no CREATE on public).
  - Graph checkpointer readiness.
  - `fc27_approval_inconsistencies` count == 0.
  - `DISPATCH_ENABLED=false` configured and enforced.
  - Returns 503 if any required safety posture or dependency is invalid.

#### C.7 `GET /v1/config/dispatch`
- **Purpose**: Inspect dispatch configuration and activation gate posture.
- **Response**: `{"configured": false, "effective": false, "gates": [...], "fc_version": "27", "platform": "common-gen5"}`.

#### C.8 `POST /v1/activation-gates/{gate_id}/challenge` & `POST /v1/activation-gates/{gate_id}` (C-4)
- **Purpose**: Two-step challenge/submit gate recording for activation gates (`G-ID`, `G-ATOMIC`).
- **Replay Protection**: Single-use nonce bound to `gate_id`, payload hash, `FC27_CODE_REVISION`, and `FC27_DEPLOYMENT_ID`.
- **Phase 1 Restriction (C-3)**: `G-OUTBOX` is refused with 409 `not_recordable_before_phase_3`.

#### C.9 Correlation IDs (`X-Correlation-Id`, C-5)
- Middleware validates canonical UUID format; malformed header returns 422.
- Generated if absent, echoed in response header, logged across all spans, stored in `approval_audit.correlation_id` and `domain_events.correlation_id`.
- **Security Invariant**: Correlation IDs are trace identifiers only and are **never** part of signed HMAC payloads.

---

### 3.4 Domain D: Deployment, Compose, Roles & Configuration

#### D.1 Service Topology & Networks
- **`fc27_net`** (bridge): Application network shared by `graph-api`, `redis`, `fc27-mcp`, `ollama`.
- **`fc27_data`** (internal bridge): Isolated database network. Joined **only** by `postgres`, `graph-api`, `db-migrate`, and `db-bootstrap`. No other container or host can reach `postgres`.

#### D.2 Compose Services
- **`postgres`**: Image `postgres:16.15-alpine`. Runs on `fc27_data`.
- **`db-bootstrap`** (profile `bootstrap`): One-shot job running `python -m src.db_roles bootstrap --database fc27 --legacy-owner fc27`. Mounts admin secrets; creates roles; exits.
- **`db-migrate`**: One-shot job running `python -m src.db_migrate`. Runs as `fc27_migrator` (acting as `fc27_owner`). Mounts `fc27_migrator_uri` secret. `depends_on: postgres healthy`. Exits with 0 before runtime services start.
- **`graph-api`**: Persistent service on port 8001. Runs as least-privileged role `fc27_app`.
  - `POSTGRES_URI_FILE=/run/secrets/fc27_app_uri`
  - `GRAPH_RUN_MIGRATIONS=false`, `GRAPH_CHECKPOINT_SETUP=false`
  - `DISPATCH_ENABLED=false`
  - `depends_on`: `db-migrate` with `condition: service_completed_successfully`.

#### D.3 Secret Management & Files (C-7)
- Database credentials pass to containers via Docker Compose file-based secrets mounted from `./secrets/` (git-ignored):
  - `postgres_admin_uri` -> `db-bootstrap`
  - `fc27_migrator_uri` -> `db-migrate`
  - `fc27_app_uri` -> `graph-api`
  - Role passwords -> `db-bootstrap`
- No password or database URI appears in `.env`, rendered compose configs, logs, or error messages.
- Connection strings in error paths are sanitized using `redact_conninfo()`.

#### D.4 Configuration Standardization
- Canonical variable names:
  - `POSTGRES_URI` (or `POSTGRES_URI_FILE`)
  - `FC27_MCP_URL`
  - `GRAPH_URL` (pointing to `http://graph-api:8001`)
  - `COMMISSIONER_ID` (pattern `^[A-Za-z0-9._-]{1,64}$`)
  - `DISPATCH_ENABLED=false`
  - `APPROVAL_CLI_HMAC_ENABLED=true`
  - `APPROVAL_REQUEST_TTL_HOURS=72`

---

### 3.5 Domain E: Security, Concurrency & Lifecycle Testing

All Phase 1 requirements must be covered by rigorous negative and adversarial tests:

#### E.1 Graph Interruption & State
- Exactly one interrupt occurred during run.
- Checkpoint is durably written to PostgreSQL while awaiting approval.
- Restart of process/runner retains resumability from the interrupted checkpoint.

#### E.2 Authorization Bypass Attempts (Negative Security Tests)
Prove each of the following fails to authorize:
1. `Command(resume={"approved": true})` -> fails/rejected (`ApprovalNotCommitted`).
2. Resume payload with text `"APPROVE"` -> fails.
3. `POST /v1/runs/{thread_id}/resume` with `approved=true` -> fails (body rejected / ignored).
4. Direct modification of checkpoint state in database -> fails.
5. Decision submitted with mismatched `package_version` -> fails (409).
6. Decision submitted with mismatched package hash -> fails (409).
7. Decision referencing a different thread ID or request ID -> fails (409).
8. Decision using expired nonce or expired approval request -> fails (409 `approval_expired`).
9. Conflicting terminal decision on already decided thread -> fails (409).

#### E.3 Valid Terminal Transitions
- Committed `APPROVE` -> Graph resumes -> final state `APPROVED, NOT DISPATCHED`.
- Committed `REJECT` -> Graph resumes -> final state `REJECTED, NO ACTION TAKEN`.

#### E.4 Idempotency & Concurrency Tests
- Duplicate identical run request: returns safe existing run or idempotency response.
- Duplicate identical decision submission: safe idempotent return.
- Repeated resume call after terminal completion: safe, no-op return.
- Post-commit Graph resume crash simulation: committed decision in PostgreSQL remains intact; subsequent reconciliation resumes successfully without modifying decision.

#### E.5 Database Least Privilege & Safety Tests
Verify connected as runtime identity `fc27_app`:
- `CREATE TABLE`, `CREATE FUNCTION`, `ALTER TABLE`, `DROP TABLE` fail with `insufficient_privilege`.
- Cannot acquire or inherit `fc27_owner`.
- `pg_roles.rolsuper` is false.
- `UPDATE` or `DELETE` on `domain_events` fails (append-only trigger).
- `UPDATE` or `DELETE` on `approval_audit` fails (append-only trigger).
- `UPDATE` or `DELETE` on terminal `graph_threads` fails (`fc27_guard_thread_transition`).
- Direct `UPDATE` of `package_version` on `awaiting_approval` thread fails.
- Direct insert of transition-only domain events (`dispatch.delivered`) fails.

#### E.6 No-Dispatch Proof
- `DISPATCH_ENABLED=false` verified in config and `/health`.
- No dispatch worker process is running.
- No player delivery adapter is invoked.
- No dashboard delivery or WebSocket publication is treated as delivery.
- No terminal state leads to active dispatch.

---

## 4. Phase 2 through Phase 6 Boundaries & Deferred Requirements

Phase 1 explicitly defers all functionality belonging to Phases 2 through 6. The boundary seams and preserved requirements are:

### Phase 2: Identity and Authorization
- **Deferred Requirements**:
  - Migration `0004_identity.sql`: tables `users`, `user_roles`, `webauthn_credentials`, `webauthn_challenges`, `totp_credentials`, `user_sessions`, `action_tokens`, `invitations`.
  - Local user accounts (argon2id password hashing) and user lifecycle.
  - Role management: `commissioner`, `coach`, `analyst`, `player`, `operator`, `auditor`.
  - Server-side sessions (`user_sessions`): HttpOnly session cookies, idle/absolute timeouts.
  - Commissioner WebAuthn passkey registration, sign-in, and decision step-up ceremonies (`py_webauthn`).
  - Player passkeys for authenticated acknowledgment.
  - Session-bound, single-use action tokens (`action_tokens`) for mutations (AM-2).
  - Staff TOTP two-factor authentication (AES-256-GCM encrypted seeds via `IDENTITY_ENCRYPTION_KEY`).
  - BFF service role (`bff`): session forwarding without direct database access.
  - Commissioner host bootstrap and recovery procedures.
- **Phase 1 Seams**:
  - Columns `actor_user_id` and `webauthn_credential_id` exist in `approval_audit`.
  - Foreign keys to `users` are omitted in Phase 1 and added in Phase 2.
  - `fc27_dispatch_transition` rejects `user:` actors with `FC004` until Phase 2.

### Phase 3: Dispatch Worker & Player-Agent Pipeline
- **Deferred Requirements**:
  - `dispatch-worker` container and background process.
  - Transactional outbox relay (`outbox` polling via `SKIP LOCKED` and publishing `{"event_id": n}` to Redis).
  - Worker heartbeat tracking (`service_heartbeats`) and reconciliation loops.
  - Per-player logical agent `PlayerAgent(player_id)` (no long-running process; instantiated per job).
  - Four-way personalization outcome handling (AM-12):
    1. Model unavailable -> `failed` (`personalization_unavailable`, retryable).
    2. Schema-invalid output -> `failed` (`plan_schema_invalid`, retryable).
    3. Policy-invalid content (`dispatch-policy-v1`) -> `failed` (`plan_policy_invalid`, non-retryable).
    4. Valid material change -> `requires_reapproval`.
    5. Valid plan -> `delivered`.
  - Re-approval challenge and decision workflow for material changes (`dispatch_plan_decisions`).
  - Delivery adapters: `DashboardDeliveryAdapter` (authoritative storage in `dispatch_plans`) and `WebSocketDeliveryAdapter` (wake-up notifications).
  - Gate `G-OUTBOX` recording and verification.
  - Stable EA player ID verification evidence (`docs/EA_PLAYER_IDENTITY_VERIFICATION.md`, AC-47).
- **Phase 1 Seams**:
  - Forward-compatible tables created: `dispatches`, `dispatch_plans`, `dispatch_plan_decisions`, `dispatch_delivery_attempts`.
  - Transition function enforces worker-only edges for `fc27_dispatch`.

### Phase 4: Dashboard UI & Real-Time Delivery
- **Deferred Requirements**:
  - Dashboard BFF container (`src/dashboard/`): FastAPI + Jinja2 server-rendered HTML + HTMX.
  - Zero database credentials in BFF: all domain mutations delegated to `graph-api` via typed HTTP client.
  - Four authorization-aware UI areas: Commissioner, Coach & Analyst, Player, Operations & Audit (Screens 1 to 10).
  - Commissioner Approval Inbox with WebAuthn step-up ceremony.
  - Dispatch Center (managing dispatches, retries, cancellations, supersessions).
  - Player Workspace (responsive down to 360px viewport, passkey-based acknowledgment).
  - Authorized domain-event projection: Player sees only own dispatches; non-audit users see `actor_kind`, never raw actor IDs.
  - Authorized WebSocket fan-out from Redis `fc27:events` with resume cursors.
  - Hardened CSP headers (no inline scripts/styles, no CDN dependencies, server-rendered SVG charts).
- **Phase 1 Seams**:
  - Display-safe summaries in Graph API projections.
  - Standardized error codes and error response schemas.

### Phase 5: Tracking & Analytics
- **Deferred Requirements**:
  - Baseline initialization upon player acknowledgment (`delivered -> acknowledged` model A).
  - Observation ingestion (`player_observations`) from match events via `live-watcher` and MCP.
  - Deterministic metric calculations and observation windows (`dispatch_assessments`).
  - Tracking status transitions (`acknowledged -> tracking -> completed | expired`).
  - Trend reporting (only when data points >= `min_trend_points`, default 3).
  - Reporting measured outcomes back into Coach and Analyst LangGraph inputs.
- **Phase 1 Seams**:
  - Tables `player_observations` and `dispatch_assessments` exist with append-only triggers.
  - Edge `acknowledged -> tracking` requires baseline patch in `fc27_dispatch_transition`.
  - No tracker processes or calculations run in Phase 1.

### Phase 6: Operations, Audit & Rollout
- **Deferred Requirements**:
  - Player Agent Operational View.
  - LangGraph Thread & Checkpoint Inspector (reading history via `GraphRunner.history`).
  - Source-of-truth labelled Audit Explorer and audited CSV exports.
  - Redacted configuration views and operational health dashboards.
  - Langfuse dispatch telemetry spans.
  - TLS edge reverse proxy (Caddy) for LAN/mobile access.
  - Full end-to-end multi-container acceptance suite.
- **Phase 1 Seams**:
  - Checkpoint storage in standard `checkpoints` tables via PostgresSaver.
  - Redaction helpers (`redact_conninfo()`) and standardized `/health` probes.

---

## 5. Features Discovered & Analyzed

| # | Category | Feature | Description | Inputs | Outputs | Error Behavior | Discovered Via |
|---|----------|---------|-------------|--------|---------|----------------|----------------|
| 1 | Data / DB | Role Bootstrap | Creates `fc27_owner` (NOLOGIN), `fc27_migrator`, `fc27_app`, `fc27_dispatch` roles, transfers legacy ownership, revokes public CREATE | Admin URI, role passwords | `BootstrapReport` | Non-zero exit code on unconfirmed transfers or connection errors | `src/db_roles.py`, Spec §14.0 |
| 2 | Data / DB | Versioned Migration Runner | Applies SQL migrations up to target version under `SET ROLE fc27_owner`, runs checkpoint setup, applies versioned grants policy | Migrator URI, target version | Applied migration version list | `MigrationError` if tampering detected, grants hash mismatch, or posture invalid | `src/db_migrate.py`, Spec §25 |
| 3 | Data / DB | Grants Policy Versioning | Enforces that `migrations/post/grants.sql` matches `GRANTS_POLICY` version and SHA-256 | `grants.sql` contents | Policy record in `schema_policy_versions` | `MigrationError` if file modified without version bump | `src/db_migrate.py`, Spec §25, C-6 |
| 4 | Data / DB | Event Index & Outbox | Atomically inserts `domain_events` and delivery `outbox` records in single transaction | Event attributes, actor, correlation ID | Generated `event_id` | Check violation if source row missing or invalid | `src/events.py`, `0003_dispatch_foundation.sql`, Spec §14.5 |
| 5 | Data / DB | Event Source Guard | Trigger verifying that mandatory sourced events reference valid existing database records | New `domain_events` row | Allows insert or raises | Exception if source row missing or wrong table | `0003_dispatch_foundation.sql`, Spec §14.5 |
| 6 | Data / DB | Transition Event Source Identity | Restricts transition-only events (`dispatch.*`, `plan.*`, `tracking.*`) to owner-owned definer function calls | `current_user`, `session_user` | Allows insert or raises | Exception if called outside definer function | `0003_dispatch_foundation.sql`, C-10 |
| 7 | Data / DB | Append-Only Table Guards | Triggers preventing `UPDATE` and `DELETE` on immutable audit/event tables | Table mutation attempts | Blocks mutation | `check_violation` with message `<table_name> is append-only` | `0003_dispatch_foundation.sql`, Spec §14 |
| 8 | Data / Lifecycle | Approval Requests Lifecycle | Manages `approval_requests` lifecycle (`awaiting_approval`, `approved`, `rejected`, `revision_requested`, `expired`) | Thread ID, package version, TTL | Request record, request ID, expiry time | 409 if thread not running or already linked | Spec §14.2, Plan Task 4 |
| 9 | Data / Lifecycle | Approval Pair Consistency Trigger | Deferred constraint trigger ensuring `graph_threads` and `approval_requests` statuses and package versions match | Commit-time transaction state | Commits or aborts | Transaction rollback if statuses or versions mismatch | Spec §7.6, §14.2, AM-14 |
| 10 | Data / Lifecycle | Approval Inconsistencies View | View exposing any discrepancy between `graph_threads` and `approval_requests` | Database state | List of inconsistency records | Causes `/health` to report degraded and blocks decisions | Spec §7.6, §14.2, AM-14 |
| 11 | Data / Lifecycle | Approval Expiry Daemon | Evaluates and marks expired approval requests and threads in paired transaction, revoking nonces | Due approval requests (`expires_at <= now()`) | Expired records count | Non-awaiting requests skipped | Spec §7.4, AM-4 |
| 12 | Data / Nonce | Single-Use Decision Nonce | Issues time-limited (max 300s) nonce bound to thread, package version, and payload SHA-256 | Thread ID, intent, principal, TTL | Nonce string, signed challenge payload | 409 if thread not awaiting or package missing | `src/approval_store.py`, Spec §14.2 |
| 13 | Data / Nonce | Nonce Subject Typing | Allows nonces for `thread`, `dispatch_plan`, `dispatch_cancel`, `role_grant`, and `activation_gate` | Subject type, target entity ID | Typed nonce row | Check constraint violation if invalid combination | Spec §14.2, C-4 |
| 14 | Data / Decision | Authoritative Decision Commit | Validates HMAC signature, consumes nonce, updates request & thread status, writes `approval_audit`, domain event, outbox in single atomic transaction | Signed payload, principal, secret | State dict with audit ID & request ID | 409 on replayed nonce, expired request, or conflict | `src/approval_store.py`, Spec §7.2 |
| 15 | Data / Decision | Request Revision Decision | Terminal decision `REQUEST_REVISION` recording reason notes without modifying original package | Signed revision payload | Terminal status `revision_requested` | 409 if request expired or not awaiting | Spec §7.3, D-12 |
| 16 | Data / Dispatch | Dispatch Entity Creation | Inserts pending dispatch record bound to candidate, objective, and approval evidence when dispatch is enabled | Approved candidate, objective, approval audit ID | Dispatch UUID | 409 `not_dispatchable` if player unverified or objective invalid | Spec §7.2, §14.3, Plan Task 8 |
| 17 | Data / Dispatch | Active Dispatch Supersession | Takes advisory lock and transitions existing active dispatch for same player/owner/metric to `superseded` | Player ID, owner, metric, new dispatch UUID | Superseded dispatch status | Fails if advisory lock cannot be acquired | Spec §7.2, §21 |
| 18 | Data / Dispatch | Hardened Transition Function | SECURITY DEFINER function `fc27_dispatch_transition` executing allowed state machine transitions | Dispatch ID, expected status, version, target status, reason, correlation ID, patch | New version integer | SQLSTATEs `FC001` through `FC007` on validation failures | Spec §11.3, AM-1 |
| 19 | Data / Dispatch | Static Transition Rule Matrix | Table `dispatch_transition_rules` defining permissible transitions by DB role and actor kind | `from_status`, `to_status`, `db_role`, `actor_kind` | Permitted lookup | `FC002` if transition edge not found | Spec §11.3 |
| 20 | Data / Dispatch | Failure Reason Derivation | Automatically derives `failure_retryable` from `failure_reason` inside transition function | `failure_reason` patch key | Sets boolean `failure_retryable` | `FC003` if caller attempts to pass `failure_retryable` directly | Spec §11.2, AM-12 |
| 21 | Data / Gates | Activation Gate Evaluation | Evaluates rollout gates (`G-MIGRATION`, `G-GRANTS`, `G-WORKER`, `G-ID`, `G-ATOMIC`, `G-OUTBOX`) against DB state and deployment identity | DB connection, deployment identity | `list[GateResult]`, `dispatch_effective` boolean | Fails closed (`dispatch_effective = false`) if any gate fails | Spec §31.2, AM-5 |
| 22 | Data / Gates | Gate Challenge & Record | Records manual activation gates via deployment-bound nonce and Commissioner HMAC signature | Gate ID, status, evidence ref, signature | Gate log entry, effective after next cycle | 409 if deployment ID missing or gate replayed; 403 on bad signature | Spec §31.2, C-4 |
| 23 | Graph | Single Approval Interrupt | Pauses LangGraph execution at approval node until database decision committed | Approvable packages in state | Interrupted checkpoint state | Never pauses more than once | `src/graph.py`, ORIGINAL_REQUEST §4.1 |
| 24 | Graph | Authoritative Decision Re-read | Upon resume, Graph re-reads committed PostgreSQL decision rather than trusting resume input | Thread ID | Updated graph state | `ApprovalNotCommitted` if decision missing or hash mismatch | `src/graph.py`, Spec §7.4 |
| 25 | Graph | Terminal States Enforcement | Formats terminal states as `APPROVED, NOT DISPATCHED` or `REJECTED, NO ACTION TAKEN` | Committed decision status | Terminal state string | Disallows active/dispatched transitions | ORIGINAL_REQUEST §4.5 |
| 26 | Graph | Sync Postgres Checkpointer | Synchronously writes graph checkpoints to `checkpoints` table via `PostgresSaver` | Thread state, checkpoint config | Stored checkpoint records | Exception if checkpoint write fails | `src/graph.py`, Spec §14.0 |
| 27 | API | `POST /v1/runs` | Creates durable run record and initiates graph execution | Club ID, thread ID, trigger, match | Run state dictionary | 409 on duplicate thread ID; 422 on invalid club ID | ORIGINAL_REQUEST §5.1 |
| 28 | API | `GET /v1/runs/{thread_id}` | Returns safe public projection of graph run | Thread ID path parameter | Run projection dictionary | 404 if thread not found | ORIGINAL_REQUEST §5.2 |
| 29 | API | `GET /v1/runs/{thread_id}/approval-request` | Returns approval request state and display-safe candidate summary | Thread ID path parameter | Approval request view | 404 if not found | ORIGINAL_REQUEST §5.3 |
| 30 | API | `POST /v1/approval-requests/{id}/decisions` | Processes authoritative Commissioner decision, committing DB before resuming graph | Approval request ID, decision intent, signature | Decision response with disabled dispatch note | 409 on conflict/expiry; 403 on invalid auth | ORIGINAL_REQUEST §5.4 |
| 31 | API | `POST /v1/runs/{thread_id}/resume` | Restricted wake-up endpoint; triggers graph resume without taking authorization payload | Thread ID path parameter | Status dictionary | 404 if thread not found | ORIGINAL_REQUEST §5.5 |
| 32 | API | `GET /health/live` | Process liveness probe | None | `{"status": "ok"}` | Process crash if down | ORIGINAL_REQUEST §5.6 |
| 33 | API | `GET /health/ready` | Readiness probe checking DB, role posture, checkpointer, consistency, and safety flags | None | `{"status": "ok", "db_role_posture": "ok", ...}` | 503 if DB down or posture violated | ORIGINAL_REQUEST §5.7 |
| 34 | API | `GET /v1/config/dispatch` | Exposes dispatch configuration and gate evaluation results | None | Config status dictionary | 403 if unauthorized | Spec §7.5, D-29 |
| 35 | API | Correlation Middleware | Validates and propagates `X-Correlation-Id` as trace context | `X-Correlation-Id` header | Injected context var & response header | 422 if header is not valid UUID | `src/correlation.py`, C-5 |
| 36 | API | CLI HMAC Kill Switch | Disables CLI HMAC decision endpoints via `APPROVAL_CLI_HMAC_ENABLED=false` | Config setting | Returns 403 `proof_type_disabled` | 403 on challenge/decision routes | Spec §18.6, AM-8 |
| 37 | Config / Sec | Secret Redaction Helper | Replaces passwords in database connection strings with `***` | Raw connection string | Redacted connection string | None (safe fallback) | `src/db_roles.py`, C-7 |
| 38 | Config / Sec | Compose Secret Files | Passes database passwords and URIs via Compose secret files under `./secrets/` | Secret files mounted in `/run/secrets/` | Read into service environments | Service fails closed if secret file missing | Spec §24, C-7 |

---

## 6. Edge Cases & Observed / Documented Behaviors

| # | Feature | Input / Condition | Observed / Documented Behavior |
|---|---------|-------------------|--------------------------------|
| 1 | Run Creation | Duplicate `thread_id` submitted with identical request body | Rejected with 409 conflict: `thread ... already exists (..., identical request); reruns must use a new thread_id`. Overwrites forbidden. |
| 2 | Run Creation | Non-numeric `club_id` (e.g. `"club_abc"`) | Rejected with 422 Unprocessable Entity: `club_id must contain ASCII digits only`. |
| 3 | Package Hashing | Key reordering or whitespace variance in `lead_packages` | Deterministic SHA-256 hash guaranteed by canonical JSON formatting (`sort_keys=True, separators=(',', ':')`). |
| 4 | Package Hashing | Candidate modified in memory after being marked approvable | Hash mismatch between thread `package_version` and decision payload; transaction rejected with 409 `package_version mismatch`. |
| 5 | Approval Expiry | Decision submitted using nonce issued before expiry, but presented after `expires_at` deadline | Transaction fails with 409 `approval_expired`. Nonce is marked revoked. Thread and request both transition to `expired`. |
| 6 | Approval Expiry Race | Concurrent execution of `expire_due()` and `apply_decision()` at exact expiration timestamp | Strict lock ordering (open nonces -> `approval_requests` -> `graph_threads`) prevents deadlocks; exactly one transaction wins (either `approved` or `expired`). |
| 7 | Approval Decision | Decision submitted referencing non-existent `approval_request_id` | Rejected with 404/409; transaction aborts. |
| 8 | Approval Decision | Second decision submitted on already terminal thread (duplicate approval) | Replay of identical decision returns original response safely; conflicting decision fails with 409 and unique constraint violation on `approval_audit`. |
| 9 | Graph Resume | `Command(resume={"approved": true})` called directly on Graph | Graph re-reads database; if database has no committed decision, raises `ApprovalNotCommitted`. Forged resume value has zero effect. |
| 10 | Graph Resume | Simulated crash/network failure immediately after decision transaction commits in DB | Committed decision in PostgreSQL is preserved. Graph remains at interrupt until startup reconciliation re-resumes and finalizes thread. |
| 11 | DB Security | `fc27_app` attempts to execute `CREATE TABLE` or `ALTER TABLE` | PostgreSQL raises `insufficient_privilege`. Application role has no DDL permissions on `public`. |
| 12 | DB Security | `fc27_app` attempts to execute `UPDATE public.dispatches` | PostgreSQL raises `insufficient_privilege`. Status updates must go through SECURITY DEFINER `fc27_dispatch_transition`. |
| 13 | DB Security | Direct SQL `INSERT` of `dispatch.delivered` event into `domain_events` by `fc27_app` | Trigger `fc27_check_event_source` rejects insert because transition-only events require `current_user = 'fc27_owner'` inside definer function. |
| 14 | DB Security | Malicious search_path injection (`SET search_path = pg_temp, public`) before calling transition function | Function runs with explicit `SET search_path = pg_catalog, pg_temp` and schema-qualified references, rendering shadowing objects in `pg_temp` inert. |
| 15 | Dispatch Transition | Transition function called with NULL correlation ID | Raises exception with SQLSTATE `FC005` (`NULL correlation ID`). |
| 16 | Dispatch Transition | Patch payload contains unapproved or unknown key (e.g. `{"dispatch_status": "completed"}`) | Raises exception with SQLSTATE `FC003` (`patch key or type invalid`). |
| 17 | Dispatch Transition | Caller supplies `fc27.actor` setting while logged in as `fc27_dispatch` | Function ignores or rejects non-service actor with SQLSTATE `FC004`; always records `service:dispatch-worker`. |
| 18 | Dispatch Transition | Non-supersede Commissioner edge attempted in Phase 1 | Raises exception with SQLSTATE `FC004` (other Commissioner edges deferred to Phase 2 WebAuthn). |
| 19 | Objective Validation | `target_delta` exceeds upper bound in `METRIC_BOUNDS` (e.g., 26 for `pass_accuracy`) | Pydantic validation raises `ValidationError`; error message names metric and permitted range without echoing user input. |
| 20 | Player ID Matching | Candidate `player_id` is `"007"` while enrolled player ID is `"7"` | String comparison fails; rejected with 409 `not_dispatchable`. No string normalization or gamertag fallback permitted. |
| 21 | Correlation ID | Malformed header `X-Correlation-Id: not-a-uuid` passed to API | Middleware rejects request with 422 Unprocessable Entity. No database query executed. |
| 22 | Migration Runner | Already-applied migration file in `migrations/` edited on disk | Checksum mismatch detected against `schema_migrations`; runner aborts with error `modified after being applied`. |
| 23 | Migration Runner | Post-migration grants file `migrations/post/grants.sql` edited without bumping policy version | Checksum mismatch detected against `GRANTS_POLICY.sha256`; runner aborts with `MigrationError`. |
| 24 | Activation Gates | Attempt to record `G-OUTBOX` via API in Phase 1 | Endpoint returns 409 `not_recordable_before_phase_3` (`RECORDABLE_GATES` limited to `G-ID` and `G-ATOMIC`). |
| 25 | Activation Gates | Gate record submitted with `code_revision` matching different git commit | Evaluation ignores record; gate status remains `failed`; `dispatch_effective` remains `false`. |

---

## 7. Gap Analysis & Repository State

### 7.1 What Has Been Completed on `feat/phase1-dispatch-foundation`
1. **Commit `21c1b5e` (Task 1)**: Role bootstrap (`src/db_roles.py`), least-privilege harness, tests in `tests/test_db_roles.py`.
2. **Commit `e01a14f` (Task 2)**: Role-aware migration runner as `fc27_owner`, checkpoint setup in migrator, `fc27_app` runtime enforcement, `migrations/post/grants.sql` policy version 1, tests in `tests/test_db_migrate_roles.py`.
3. **Commit `c1b7e75` (Task 3)**: Migration 0003 Section A (`domain_events`, `outbox`, append-only triggers, `fc27_check_event_source`), `src/events.py`, grants policy version 2, tests in `tests/test_events_outbox.py`.

### 7.2 What Remains to Complete Phase 1
1. **Task 4 (Lifecycle & Store)**:
   - Append Section B to `migrations/0003_dispatch_foundation.sql`: `approval_requests`, `fc27_check_approval_pair` trigger, `fc27_approval_inconsistencies` view, `approval_audit` extensions, `approval_nonces` extensions.
   - Update `src/approval_store.py`: `complete_run` request creation, `issue_challenge` request locking & expiry, `apply_decision` pair status update & audit link, `expire_due` implementation, inbox query `pending_approvals`.
   - Update `src/decision.py`: remove `state["dispatch"]` dictionary writing.
2. **Task 5 (API & Graph Integration)**:
   - Create `src/correlation.py`: context var, logging filter, middleware.
   - Update `src/graph.py`: resume refusal for non-decided threads, accept `revision_requested`.
   - Update `src/graph_api.py`: settings (`dispatch_enabled`, `approval_request_ttl`, `cli_hmac_enabled`), background expiry task, disabled dispatch response, legacy selection record projection, `/health` consistency check.
3. **Task 6 (Dispatch Schema & Objectives)**:
   - Append Section C to `migrations/0003_dispatch_foundation.sql`: `players`, `dispatches`, partial unique index, insert & immutability triggers, worker tables.
   - Update `src/schemas.py`: `Objective` schema with versioned bounds validation (`METRIC_BOUNDS`), `Candidate.objective`.
   - Create `src/dispatch/objective.py`: `METRIC_BOUNDS` policy `fc27-objective-bounds-v1`.
4. **Task 7 (Hardened Transition Function)**:
   - Append Section D to `migrations/0003_dispatch_foundation.sql`: `public.fc27_dispatch_transition` SECURITY DEFINER function and `dispatch_transition_rules` seed.
   - Create `src/dispatch/transition.py`: Python wrapper.
5. **Task 8 (Activation Gates & Dispatch Creation)**:
   - Append Section E to `migrations/0003_dispatch_foundation.sql`: `activation_gate_log`, view `activation_gates`, `service_heartbeats`.
   - Create `src/dispatch/gates.py` and `src/dispatch/creation.py`.
   - Update `src/graph_api.py`: gate routes (`/v1/activation-gates/{gate_id}/challenge` and submit), `/v1/config/dispatch`.
6. **Task 9 (Compose & Configuration)**:
   - Update `docker-compose.yml`: `db-bootstrap`, `db-migrate`, secret files, runtime `fc27_app_uri`, `DISPATCH_ENABLED=false`.
   - Create `docs/DISPATCH_ACTIVATION.md`, `docs/EA_PLAYER_IDENTITY_VERIFICATION.md`.
7. **Task 10 (Verification & Trace)**:
   - Full test suite run, acceptance trace in `TEST_RESULTS.md`, security update in `SECURITY_CHANGES.md`.

---

## 8. Conclusion

The specification for Phase 1: Secure Data Foundation is exhaustive, mathematically sound, and rigorously bound by the non-negotiable invariants in `ORIGINAL_REQUEST.md` and `docs/superpowers/specs/2026-10-02-player-dispatch-design.md`. The repository is currently on branch `feat/phase1-dispatch-foundation` with Tasks 1–3 successfully implemented and passing tests. The path forward for Tasks 4–10 is clearly mapped, verifiable, and protected against any accidental leakage of Phase 2–6 functionality.
