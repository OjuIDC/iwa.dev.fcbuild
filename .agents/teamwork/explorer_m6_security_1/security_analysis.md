# Security Analysis: Phase 1 Secure Data Foundation

**Agent**: `explorer_m6_security_1`  
**Milestone**: M6 (Final Verification & Security Documentation)  
**Date**: 2026-10-03  
**Status**: Complete  

---

## Executive Summary

Phase 1 establishes the authoritative PostgreSQL data foundation, immutable event and audit logging, versioned recommendation and approval lifecycle, LangGraph persistence, and least-privileged persistent Graph API for the FC27 Pro Clubs platform. In accordance with the non-negotiable Phase 1 invariants:
1. `DISPATCH_ENABLED=false` is strictly enforced across the database, application server, and deployment topology.
2. No player delivery adapters, workers, or delivery paths exist or operate.
3. LangGraph approval resume does not accept authorization payloads and re-reads committed PostgreSQL decisions exclusively.
4. Section 1A human authority models and player consent foundations are fully represented and fail closed.

This document details the architecture, code locations, database constraints, triggers, and test evidence across all 11 required security areas.

---

## 1. Deep Dive: 11 Security Architecture Areas

### 1. Database Role Separation & Least Privilege

#### Architecture & Implementation
The database privilege model enforces strict separation of concerns across four distinct database roles:
- **`fc27_owner`**: Defined as `NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT`. It is the authoritative owner of all schema objects, tables, views, sequences, triggers, and functions in schema `public`. Direct login is impossible. Schema `public` has `CREATE` revoked from `PUBLIC` (`REVOKE CREATE ON SCHEMA public FROM PUBLIC; GRANT USAGE, CREATE ON SCHEMA public TO fc27_owner;`).
- **`fc27_migrator`**: Defined as `LOGIN NOINHERIT`. Role membership in `fc27_owner` is granted to it (`GRANT fc27_owner TO fc27_migrator`). The migration runner (`src/db_migrate.py:117`) executes `SET ROLE fc27_owner` under an advisory lock (`pg_advisory_lock(0x0FC27_0001)`) to apply DDL migrations and checkpointer schema setup.
- **`fc27_app`**: Defined as `LOGIN NOINHERIT`. The persistent `graph-api` service connects exclusively using this role (`POSTGRES_URI_FILE: /run/secrets/fc27_app_uri`). It possesses DML privileges only (`SELECT, INSERT, UPDATE` on mutable runtime tables; `SELECT, INSERT` on immutable audit/event tables; `SELECT` on migration and metadata views). It is explicitly denied `CREATE` on schema `public`, is NOT a member of `fc27_owner`, and has NO `UPDATE` or `DELETE` on `public.dispatches`.
- **`fc27_dispatch`**: Defined as `LOGIN NOINHERIT`. Reserved for future worker processes. It possesses read-only access to dispatches and tracking tables, with scoped update on outbox publication columns (`UPDATE (published_at, attempts, last_error, locked_until) ON public.outbox`). It has NO `INSERT`, `UPDATE`, or `DELETE` on `public.dispatches`.

#### Code Locations & Definitions
- `src/db_roles.py:13-16`: Role constants (`ROLE_OWNER`, `ROLE_MIGRATOR`, `ROLE_APP`, `ROLE_DISPATCH`).
- `src/db_roles.py:40-59`: `bootstrap_cluster()` creating roles with `NOINHERIT` and `NOLOGIN` attributes.
- `src/db_roles.py:80-200`: `bootstrap_database()` revoking public `CREATE`, granting usage, and transferring table/function ownership to `fc27_owner`.
- `src/db_roles.py:60-78` & `src/db_migrate.py:86-101`: `check_runtime_posture()` verifying caller is not superuser, not member of `fc27_owner`, and lacks `CREATE` on public.
- `src/db_migrate.py:42-84`: `verify_postconditions()` validating role privileges and function attributes.
- `migrations/post/grants.sql:1-169`: Versioned grant statements (`-- grants-policy-version: 5`) checked against `GRANTS_POLICY` SHA-256 hash (`src/db_migrate.py:25`).

#### Proving Test Evidence
- `tests/test_db_roles.py:15-35`: `test_role_attributes`
- `tests/test_db_roles.py:36-43`: `test_public_cannot_create_in_public_schema`
- `tests/test_db_migrate_roles.py:12-28`: `test_objects_owned_by_fc27_owner`
- `tests/test_db_migrate_roles.py:33-37`: `test_app_cannot_run_ddl`
- `tests/test_db_migrate_roles.py:38-43`: `test_checkpoint_setup_requires_owner`
- `tests/e2e/test_tier1_features.py:55-79`: `test_feature_01_role_bootstrap_roles_exist_and_configured`, `test_feature_01_public_schema_create_revoked`

---

### 2. SECURITY DEFINER Hardening (`public.fc27_dispatch_transition`)

#### Architecture & Implementation
State transitions on the `public.dispatches` table cannot be performed by direct SQL `UPDATE` or `DELETE` because runtime roles have no update grants on that table. All dispatch lifecycle mutations MUST pass through the stored procedure `public.fc27_dispatch_transition`:
- **Security Context**: Defined as `SECURITY DEFINER` and owned by `fc27_owner`. Executes with owner privileges while maintaining caller identity via `session_user`.
- **Search Path Isolation**: Pinned strictly to `SET search_path = pg_catalog, pg_temp` to prevent malicious object substitution or search path hijack attacks.
- **Access Restrictions**: `REVOKE ALL ON FUNCTION public.fc27_dispatch_transition(...) FROM PUBLIC;` and `GRANT EXECUTE ... TO fc27_app, fc27_dispatch;`.
- **No Dynamic SQL**: The function implementation uses qualified relation names and zero dynamic SQL (no `EXECUTE` or `format()`).
- **Matrix Rule Enforcement**: Transition legitimacy is evaluated against table `public.dispatch_transition_rules`, matching `(from_status, to_status, db_role, actor_kind)`.
- **Actor Kind & Identity**:
  - `fc27_dispatch` sessions are strictly forced to `actor = 'service:dispatch-worker'` (`actor_kind = 'service'`).
  - `fc27_app` sessions derive actor from `current_setting('fc27.actor', true)`, enforcing regex `^(service:graph-api|commissioner:[A-Za-z0-9._-]{1,64}|user:[0-9a-f-]{36})$`. User actors are explicitly rejected with `FC004` ("user actors require Phase 2").
- **Phase 1 Invariant Enforcement**: The function explicitly halts execution with SQLSTATE `FC002` if `p_to_status = 'delivered'` ("Phase 1 invariant violation: transition to delivered is disabled (DISPATCH_ENABLED=false)").
- **Error Codes**:
  - `FC001`: Concurrency conflict (expected status or expected version mismatch, or dispatch not found).
  - `FC002`: Illegal state machine transition or Phase 1 invariant delivery violation.
  - `FC003`: Invalid patch structure or disallowed patch keys for target edge.
  - `FC004`: Invalid caller DB role, missing/malformed actor, or Phase 2 actor claim.
  - `FC005`: Null correlation ID.
  - `FC006`: Missing required precondition row (e.g. acknowledgment, assessment, material change plan).
  - `FC007`: Reason text missing (when required) or exceeds 500 characters.

#### Code Locations & Definitions
- `migrations/0003_dispatch_foundation.sql:1365-1784`: Definition of `public.fc27_dispatch_transition`.
- `migrations/0003_dispatch_foundation.sql:1316-1364`: Table `public.dispatch_transition_rules` and permitted edges.
- `src/dispatch/transition.py:1-200`: Python client wrapper mapping SQLSTATEs to typed exceptions (`Phase1DeliveryBlockedError`, `TransitionConflictError`, etc.).

#### Proving Test Evidence
- `tests/test_dispatch_transition.py:25-48`: `test_function_attributes`
- `tests/test_dispatch_transition.py:49-60`: `test_function_source_has_no_dynamic_sql`
- `tests/test_dispatch_transition.py:61-74`: `test_runtime_roles_cannot_replace_function`
- `tests/test_dispatch_transition.py:75-88`: `test_null_correlation_id_rejected` (FC005)
- `tests/test_dispatch_transition.py:89-102`: `test_reason_length_limit` (FC007)
- `tests/test_dispatch_transition.py:103-130`: `test_stale_version_conflict`, `test_status_mismatch_conflict` (FC001)
- `tests/test_dispatch_transition.py:167-206`: `test_app_malformed_actor_rejected`, `test_app_user_actor_rejected_in_phase1` (FC004)
- `tests/test_dispatch_transition.py:267-310`: `test_illegal_transition_edge_rejected`, `test_phase1_delivered_edge_blocked` (FC002)

---

### 3. Approval Expiry Semantics

#### Architecture & Implementation
Approval requests and cryptographic decision challenges have strict, non-bypassable time limits:
- **Approval Request TTL**: Defaults to 72 hours (`ApprovalStore(..., request_ttl=timedelta(hours=72))`). Stored in `approval_requests.expires_at`. Checked by constraint `CHECK (expires_at > created_at)`.
- **Decision Challenge Nonce TTL**: Issued via `POST /v1/runs/{id}/decision-challenge` with a maximum TTL of 300 seconds (`MAX_APPROVAL_TTL_SECONDS = 300`, default 120s). The challenge expiration is clamped to the parent request's `expires_at`: `expires_at = min(issued_at + ttl, req['req_exp_epoch'])`.
- **Expiry Daemon**: `ApprovalStore.expire_due()` runs as a background task in `graph-api` (`src/graph_api.py:307-323`, every 60 seconds) and is also proactively invoked before issuing any challenge or applying any decision.
- **Atomic Paired State Transition**:
  - Locks rows in global order: open nonces for thread -> `approval_requests` -> `graph_threads`.
  - Atomically sets `approval_requests.status = 'expired'`, `expired_at = now()`.
  - Sets `graph_threads.status = 'expired'`, `updated_at = now()`.
  - Revokes all open, unconsumed nonces for the thread (`UPDATE approval_nonces SET revoked_at = now() WHERE thread_id = %s AND consumed_at IS NULL AND revoked_at IS NULL`).
  - Atomically emits domain event `approval.expired` and delivery outbox row.
- **Fail-Closed Consumption**: An expired nonce or an expired approval request cannot be used to submit a decision (returns HTTP 409 `approval_expired`).

#### Code Locations & Definitions
- `src/approval_store.py:64-68`: `request_ttl` default 72 hours.
- `src/approval_store.py:38`: `MAX_APPROVAL_TTL_SECONDS = 300`.
- `src/approval_store.py:267-360`: `issue_challenge()` proactive expiry and nonce bounds.
- `src/approval_store.py:402-404, 489-496`: `apply_decision()` expiry checks.
- `src/approval_store.py:623-720`: `expire_due()` atomic paired transition and nonce revocation.
- `migrations/0003_dispatch_foundation.sql:126-146`: `approval_requests` schema and expiry constraints.

#### Proving Test Evidence
- `tests/test_approval_lifecycle.py`: `test_approval_request_expiry_daemon`
- `tests/test_approval_security.py`: `test_nonce_expiration_is_enforced`
- `tests/e2e/test_tier3_combinations.py:44-78`: `test_tier3_decision_vs_expiry_race_condition`
- `tests/e2e/test_tier4_scenarios.py:212-238`: `test_section_1a_inactivity_and_timeout_never_creates_approval`

---

### 4. Section 1A Human Authority & Player Consent

#### Architecture & Implementation
Section 1A establishes a comprehensive human authority model separating club-level strategy from player-specific assignments and operational continuity:
- **Authority Tiers**:
  1. *Club Authorization*: Commissioner is primary authority (`authority_basis = 'primary'`).
  2. *Delegated Club Authorization*: Designated human deputy under time-bounded, revocable delegation (`authority_basis = 'explicit_delegate'`).
  3. *Player Consent*: Affected player accepts, rejects, pauses, acknowledges, or requests changes to their own assignment (`authority_basis = 'affected_player_consent'`).
  4. *Affected-Player Fallback*: Affected player provides fallback authorization for eligible personal assignments after escalation (`authority_basis = 'affected_player_fallback'`).
  5. *Held State*: In the absence of an authorized actor, requests remain held (`held_no_authorized_approver`).
- **Core Invariant: Human-in-the-Loop**:
  > *No agent may approve or reject for a human.*
  Club agents, coach agents, player agents, Graph nodes, LLMs, background workers, and subscribers are strictly forbidden from acting as decision actors.
- **Core Invariant: Inactivity Never Approves**:
  > *Inactivity never constitutes approval.*
  No timeout, absence, unread notification, or escalation state transition may ever generate an approval decision. Inactivity can only escalate review eligibility or expire the request.
- **Phase 1 Fail-Closed Policy**:
  - The Phase 1 decision boundary (`src/approval_store.py:376-387`) permits ONLY `actor_type == 'commissioner'` and `authority_basis == 'primary'`.
  - Any request with `actor_type == 'player'`, `authority_basis == 'affected_player_fallback'`, or agent actor name raises HTTP 403.
  - Multi-player packages cannot be marked player-fallback eligible (`CHECK (allow_player_fallback = false OR scope = 'player_specific')`).
- **Data Model Compatibility**:
  - Section 1A tables created in migration 0003: `authority_policies`, `authority_delegations`, `player_assignments`, `player_decision_requests`, `player_decisions`, `approval_escalations`, `authority_conflicts`.
  - Fields added to `approval_audit`: `actor_type`, `actor_id`, `authority_role`, `authority_basis`, `decision_scope`, `delegation_id`, `subject_player_id`, `assignment_id`, `conflict_state`.

#### Code Locations & Definitions
- `ORIGINAL_REQUEST.md:961-1591`: Section 1A specification patch.
- `migrations/0003_dispatch_foundation.sql:464-674`: Section 1A tables and `approval_audit` schema extensions.
- `src/approval_store.py:376-387`: Authority basis and actor type enforcement in `apply_decision`.
- `src/approval_store.py:548-578`: Section 1A columns persisted to `approval_audit`.
- `src/graph.py:185-190`: Decision re-read validation checking `actor_type` and `authority_basis`.

#### Proving Test Evidence
- `tests/e2e/test_tier4_scenarios.py:171-240`: `test_section_1a_authority_separation_club_vs_player`, `test_section_1a_delegation_scope_and_expiry_validation`, `test_section_1a_player_fallback_prohibits_multi_player_packages`, `test_section_1a_inactivity_and_timeout_never_creates_approval`
- `tests/test_m1_adversarial_challenger.py`: Adversarial authority tests verifying player actor rejection and agent actor refusal.

---

### 5. HMAC Kill Switch & Rotation (`APPROVAL_CLI_HMAC_ENABLED`)

#### Architecture & Implementation
- **HMAC Proof Model**: Commissioner decision challenges and activation gate submissions require cryptographic HMAC-SHA256 signatures over canonical payload representations using `APPROVAL_SIGNING_SECRET`.
- **Kill Switch Mechanism**:
  - Environment variable: `APPROVAL_CLI_HMAC_ENABLED` (boolean, defaults to `true`).
  - When set to `false`, the Graph API starts without requiring `APPROVAL_SIGNING_SECRET`.
  - Any attempt to invoke `/decision-challenge`, `/decision`, or activation gate challenge/recording routes immediately fails closed with HTTP 403 `proof_type_disabled`.
- **Secret Rotation**:
  - Generating a new 32-byte secret and updating `APPROVAL_SIGNING_SECRET` invalidates all open, unconsumed challenge nonces.
  - Existing database decisions remain immutable with their stored `signature_sha256`.
  - Constant-time string digest comparison (`hmac.compare_digest`) prevents timing side-channel attacks.

#### Code Locations & Definitions
- `src/settings.py:22-38`: `read_secret("APPROVAL_SIGNING_SECRET", required=cli_hmac, min_length=32 if cli_hmac else 0)`.
- `src/graph_api.py:90`: `env_bool("APPROVAL_CLI_HMAC_ENABLED", True)`.
- `src/graph_api.py:789-792`: `require_hmac()` dependency raising HTTP 403 `proof_type_disabled`.
- `src/graph_api.py:489-491, 540-542`: Activation gate endpoints checking `cli_hmac_enabled`.
- `src/approval.py:34-45`: HMAC-SHA256 verification using `hmac.compare_digest`.

#### Proving Test Evidence
- `tests/test_graph_api_v1.py`: `test_hmac_kill_switch_blocks_decision_endpoints`
- `tests/test_approval_security.py`: `test_modified_signed_payload_is_rejected`
- `tests/e2e/test_tier1_features.py`: `test_feature_28_hmac_kill_switch`

---

### 6. Append-Only Audit & Immutability

#### Architecture & Implementation
Authoritative records cannot be rewritten, truncated, or deleted:
- **Append-Only Trigger Function**: `public.fc27_reject_mutation()` is defined as a PL/pgSQL trigger function that raises a check violation exception: `RAISE check_violation USING MESSAGE = TG_TABLE_NAME || ' is append-only';`.
- **Covered Tables**: Attached as `BEFORE UPDATE OR DELETE` triggers on:
  - `public.domain_events`
  - `public.approval_audit`
  - `public.activation_gate_log`
  - `public.player_decisions`
  - `public.approval_escalations`
  - `public.dispatch_plans`
  - `public.dispatch_plan_decisions`
  - `public.dispatch_delivery_attempts`
  - `public.dispatch_acknowledgments`
  - `public.dispatch_comments`
  - `public.analyst_notes`
  - `public.player_observations`
  - `public.dispatch_assessments`
- **Terminal State Guards**:
  - `fc27_guard_thread_transition()` blocks update or deletion of `graph_threads` rows in terminal states (`approved`, `rejected`, `revision_requested`, `expired`).
  - `fc27_guard_approval_request_transition()` blocks update or deletion of `approval_requests` rows in terminal states.
  - `fc27_guard_dispatch_immutable()` blocks direct update or deletion of `dispatches`.
- **Grant Level Enforcement**: `fc27_app` has only `SELECT, INSERT` on all audit and event tables.

#### Code Locations & Definitions
- `migrations/0003_dispatch_foundation.sql:40-56`: `fc27_reject_mutation()` function and triggers on `domain_events` and `approval_audit`.
- `migrations/0003_dispatch_foundation.sql:1806-1810`: Trigger on `activation_gate_log`.
- `migrations/0003_dispatch_foundation.sql:581-583, 604-606`: Triggers on `player_decisions` and `approval_escalations`.
- `migrations/0003_dispatch_foundation.sql:1069-1189`: Triggers on tracking and observation tables.
- `migrations/0002_approval_security.sql:39-71`: `fc27_guard_thread_transition()`.
- `migrations/0003_dispatch_foundation.sql:150-203`: `fc27_guard_approval_request_transition()`.

#### Proving Test Evidence
- `tests/test_events_outbox.py:35-50`: `test_domain_events_append_only`, `test_approval_audit_append_only`
- `tests/test_approval_lifecycle.py`: `test_terminal_thread_cannot_be_modified`, `test_terminal_approval_request_frozen`
- `tests/test_dispatch_schema.py`: `test_append_only_triggers_on_tracking_tables`
- `tests/e2e/test_tier1_features.py`: `test_feature_07_append_only_table_guards`

---

### 7. Package Version Immutability

#### Architecture & Implementation
A recommendation package cannot be modified in place once it enters the approval lifecycle:
- **Canonical Package Hashing**: `package_version(packages)` computes the deterministic SHA-256 hash of structured lead packages.
- **Database Immutability Constraints**:
  - `graph_threads_guard` trigger (`migrations/0003_dispatch_foundation.sql:294-299`):
    ```sql
    IF (OLD.status = 'awaiting_approval' OR OLD.package_version IS NOT NULL) 
       AND NEW.package_version IS DISTINCT FROM OLD.package_version THEN
        RAISE EXCEPTION 'package_version is immutable' USING ERRCODE = 'check_violation';
    END IF;
    ```
  - `fc27_approval_requests_guard` trigger (`migrations/0003_dispatch_foundation.sql:178-180`):
    ```sql
    IF NEW.package_version <> OLD.package_version THEN
        RAISE EXCEPTION 'package_version is immutable' USING ERRCODE = 'check_violation';
    END IF;
    ```
- **Paired Consistency Constraint**: Constraint trigger `fc27_check_approval_pair()` enforces that `graph_threads.package_version` and `approval_requests.package_version` match exactly.
- **Decision Binding**: When applying a decision, `apply_decision()` requires `WHERE package_version = signed.package_version`. If the lead package changes or a stale version is submitted, the transaction raises HTTP 409 conflict and rolls back.
- **Revision Requests**: Selecting `REQUEST_REVISION` records revision notes in `approval_audit` while leaving the original package version unchanged. Re-running requires allocating a new thread ID or generating a new version.

#### Code Locations & Definitions
- `src/decision.py:20-35`: `package_version()` hashing function.
- `src/approval_store.py:124-128, 508-510`: Package version extraction and stale package validation.
- `migrations/0003_dispatch_foundation.sql:294-299`: Trigger guard on `graph_threads`.
- `migrations/0003_dispatch_foundation.sql:178-180`: Trigger guard on `approval_requests`.
- `migrations/0003_dispatch_foundation.sql:334-411`: Paired consistency constraint trigger `fc27_check_approval_pair()`.

#### Proving Test Evidence
- `tests/test_approval_lifecycle.py`: `test_package_version_immutable`
- `tests/test_approval_lifecycle.py`: `test_stale_package_version_rejected`
- `tests/test_approval_lifecycle.py`: `test_approval_pair_consistency_trigger`
- `tests/e2e/test_tier4_scenarios.py:147-165`: `test_tier4_scenario_revision_request_preserves_package_immutability`

---

### 8. LangGraph Interrupt & Resume (AM-4 PostgreSQL Re-Read)

#### Architecture & Implementation
The workflow engine cannot be coerced into unauthorized approval:
- **Single Interrupt Point**: The LangGraph `StateGraph` pauses execution at node `approval` via `interrupt(state["approval_request"])` (`src/graph.py:168`). Double interruption (`interrupt_before=["approval"]`) is completely eliminated.
- **Workflow State vs. Authorization Evidence**: LangGraph checkpoints represent execution workflow state, NOT authorization state. A resume command (`Command(resume=...)` or `POST /v1/runs/{id}/resume`) serves solely as a wake-up signal.
- **PostgreSQL Re-Read**: When node `approval` is resumed, it invokes `deps.decision_reader(state["thread_id"])` to read the committed record from `graph_threads` and `approval_audit`.
- **Validation Checks**:
  - Committed row exists and status is in `('approved', 'rejected', 'revision_requested')`.
  - `package_version` in the database matches the package version in workflow state.
  - `approval_request_id` in the database matches workflow state.
  - Actor type is `'commissioner'` and authority basis is `'primary'`.
  - If any check fails, it raises `ApprovalNotCommitted` (failing closed).
- **Terminal State Formatting**:
  - `'approved'` -> `'APPROVED, NOT DISPATCHED'`
  - `'rejected'` -> `'REJECTED, NO ACTION TAKEN'`
  - `'revision_requested'` -> `'REVISION_REQUESTED, NO ACTION TAKEN'`
- **Failure Ordering & Safe Reconciliation**:
  The decision write path in `graph-api` commits the PostgreSQL transaction (including `approval_audit`, `domain_events`, and `outbox`) BEFORE triggering `resume_graph()`. If graph resume fails, the committed database state is preserved, and graph-api retries resuming all unfinalized terminal threads upon next startup (`src/graph_api.py:300-302`).

#### Code Locations & Definitions
- `src/graph.py:166-207`: Node `approval` calling `interrupt()` and re-reading committed decision.
- `src/graph.py:46-50`: `TERMINAL_STATUS_MAP` enforcing terminal wording.
- `src/graph.py:78-80`: `ApprovalNotCommitted` exception definition.
- `src/graph.py:249-250`: Graph edge wiring verifying single interrupt.
- `src/graph_api.py:839-841`: Decision commit prior to graph resume.

#### Proving Test Evidence
- `tests/test_langgraph_flow.py`: `test_langgraph_interrupt_cannot_be_bypassed`
- `tests/test_langgraph_flow.py`: `test_single_interrupt_per_lifecycle`
- `tests/test_langgraph_flow.py`: `test_terminal_state_text_approved_not_dispatched`
- `tests/test_graph_persistence.py`: `test_restart_recovers_interrupted_graph`
- `tests/test_adversarial_m2_it2_2.py`: Attack vector tests attempting forged resume payloads.

---

### 9. Nonce-Bound & Deployment-Bound Activation Gates

#### Architecture & Implementation
Rollout gates govern whether automated dispatch can ever be activated:
- **Rollout Gates (§31.2)**:
  - `G-MIGRATION`: Automatic check for migration `0003`.
  - `G-GRANTS`: Automatic check for catalog privileges and owner function ownership.
  - `G-WORKER`: Automatic check for fresh `service_heartbeats` row (<60s). Fails closed in Phase 1 because no production worker exists.
  - `G-ID`: Manual signed gate verifying stable EA player IDs.
  - `G-ATOMIC`: Manual signed gate verifying atomic approval CI evidence.
  - `G-OUTBOX`: Manual signed gate verifying Phase 3 outbox relay. Prohibited in Phase 1.
- **Dynamic Re-Evaluation (C-1)**: `reevaluate_gates(app)` runs at startup and every 60s in background, updating `app.state.dispatch_effective`. A recorded gate takes effect on the next evaluation without restarting.
- **Challenge & Nonce Binding (C-4)**:
  - Calling `POST /v1/activation-gates/{gate_id}/challenge` issues a single-use nonce stored in `approval_nonces` with `subject_type = 'activation_gate'`.
  - The payload binds `gate_id`, `status`, `evidence_ref`, `code_revision`, `deployment_id`, `fc_version`, `platform`, `issued_at`, `expires_at`, `nonce`.
- **RFC-8785 Canonical Bytes**: `canonical_gate_bytes()` serializes fields in deterministic lexicographical key order with minimal separators `(',', ':')` and UTF-8 encoding.
- **HMAC-SHA256 Signature**: Signed by Commissioner; verified with `hmac.compare_digest`.
- **Append-Only Gate Log**: Successfully recorded gates insert into `public.activation_gate_log` (`id, gate_id, status, evidence_ref, code_revision, deployment_id, nonce, signature_sha256, recorded_by`). The view `public.activation_gates` returns the latest record per `gate_id`.
- **Phase 1 Boundary Enforcement**:
  - Only `G-ID` and `G-ATOMIC` are recordable (`RECORDABLE_GATES = frozenset({"G-ID", "G-ATOMIC"})`).
  - Attempting to record `G-OUTBOX` returns HTTP 409 `not_recordable_before_phase_3`.

#### Code Locations & Definitions
- `src/dispatch/gates.py:23-41`: `RECORDABLE_GATES` and `GATE_SIGNED_FIELDS`.
- `src/dispatch/gates.py:60-83`: `canonical_gate_bytes()`, `sign_gate()`, `verify_gate_signature()`.
- `src/dispatch/gates.py:84-215`: `evaluate_gates()` checking all 6 gates.
- `src/dispatch/gates.py:218-223`: `dispatch_effective()`.
- `src/graph_api.py:140-170`: `reevaluate_gates()`.
- `src/graph_api.py:482-644`: `gate_challenge` and `gate_record` endpoints.
- `migrations/0003_dispatch_foundation.sql:1790-1836`: `activation_gate_log` table and `activation_gates` view.

#### Proving Test Evidence
- `tests/test_activation_gates.py`: `test_canonical_gate_bytes_rfc8785`, `test_evaluate_gates_order`, `test_g_outbox_refused_in_phase1`
- `tests/test_graph_api_v1.py`: `test_gate_challenge_and_record_flow`
- `tests/e2e/test_tier1_features.py`: `test_feature_35_activation_gate_evaluation`, `test_feature_36_gate_challenge_and_record`

---

### 10. Event Source Identity & Correlation ID Isolation

#### Architecture & Implementation
Authoritative domain event logging and distributed tracing are hardened against forgery and cross-tenant pollution:
- **Event Source Verification Trigger**: Trigger function `public.fc27_check_event_source()` on `domain_events` BEFORE INSERT verifies that:
  - Event `approval.decided` has `source_table = 'approval_audit'`, non-null `source_id`, and matching row in `approval_audit`.
  - Event `approval.created` / `approval.expired` has `source_table = 'approval_requests'`, non-null `source_id`, and matching row in `approval_requests`.
  - Event `dispatch.created` has `source_table = 'dispatches'` and matching row in `dispatches`.
  - Event `activation.gate_recorded` has `source_table = 'activation_gate_log'` and matching row in `activation_gate_log`.
- **Transition-Only Event Restriction (C-10)**:
  Events `dispatch.*` (except `created`), `plan.*`, and `tracking.*` can only be inserted when `current_user = 'fc27_owner'` and `session_user IN ('fc27_app', 'fc27_dispatch')`. This guarantees that transition events originate ONLY from inside the `fc27_dispatch_transition` SECURITY DEFINER function. Direct inserts from migrator or app sessions are rejected.
- **Correlation ID Tracking & Isolation (C-5)**:
  - `CorrelationMiddleware` inspects `X-Correlation-Id` header on all HTTP requests.
  - Enforces canonical 36-character UUID format (rejects malformed headers with HTTP 422).
  - Generates a fresh `uuid4()` if missing.
  - Sets `correlation_id_var` (ContextVar) and attaches correlation ID to all log records via `CorrelationIdFilter`.
  - Propagates correlation ID into domain events and outbox rows.
  - Echoes `X-Correlation-Id` in the response header.
  - Intentionally excluded from HMAC signature payloads so transport intermediaries cannot invalidate decision signatures.
- **Zero-Echo Error Sanitization**:
  The `RequestValidationError` handler in `src/graph_api.py:342-351` strips the `input` field from all error payloads, preventing model prompt reflection and confidential candidate recommendation leakage.

#### Code Locations & Definitions
- `migrations/0003_dispatch_foundation.sql:1852-1952`: `fc27_check_event_source()` trigger function.
- `src/correlation.py:12-66`: `correlation_id_var`, `CorrelationIdFilter`, and `CorrelationMiddleware`.
- `src/events.py:1-60`: `record_event()` inserting atomically into `domain_events` and `outbox`.
- `src/graph_api.py:340-351`: Middleware attachment and zero-echo error handler.

#### Proving Test Evidence
- `tests/test_events_outbox.py:15-34`: `test_domain_events_source_check`
- `tests/test_dispatch_transition.py:131-165`: `test_worker_actor_derivation` (C-10 transition event identity)
- `tests/test_correlation.py`: `test_valid_correlation_id_propagated`, `test_malformed_correlation_id_returns_422`
- `tests/e2e/test_tier1_features.py`: `test_feature_05_event_source_guard`, `test_feature_06_transition_event_source_identity`

---

### 11. File-Based Secrets & `redact_conninfo`

#### Architecture & Implementation
Zero secret leakage across environments, process arguments, logs, and network layers:
- **File Secret Resolution (C-7)**: `src/settings.py::read_secret()` supports reading credentials from mounted secret files via `<NAME>_FILE` environment variables (e.g. `POSTGRES_URI_FILE=/run/secrets/fc27_app_uri`).
- **Docker Compose Topology**:
  - `docker-compose.yml` mounts secrets from host directory `./secrets/` (`postgres_admin_uri`, `fc27_migrator_password`, `fc27_app_password`, `fc27_dispatch_password`, `fc27_migrator_uri`, `fc27_app_uri`).
  - Passwords and database URIs are excluded from `x-common` and service environment variables.
  - Services use `security_opt: [no-new-privileges:true]`, `cap_drop: [ALL]`, and `init: true`.
- **Network Segmentation**:
  - Network `fc27_data` is marked `internal: true`. It is joined ONLY by `postgres`, `db-bootstrap`, `db-migrate`, and `graph-api`.
  - Application services (`ws-server`, `fc27-mcp`, `live-watcher`, etc.) reside on `fc27_net` and have no network route to PostgreSQL.
- **Connection String Redaction**:
  - Function `redact_conninfo(conninfo)` replaces passwords in connection URIs with `:***@` and in key-value strings with `password=***`.
  - Used in `src/db_roles.py`, `src/db_migrate.py`, exception handlers, and log filters.
- **Safe Run Projections**:
  - Public endpoints (`GET /v1/runs/{id}`, `GET /v1/runs/{id}/approval-request`) project only display-safe candidate summaries (`candidate_id`, `player_id`, `tier`, `score`).
  - Model prompts, credentials, internal checkpointer blobs, and raw LLM narratives are excluded from public JSON responses.

#### Code Locations & Definitions
- `src/settings.py:22-38`: `read_secret()` supporting file secrets and minimum length validation.
- `src/db_roles.py:23-39`: `redact_conninfo()` implementation.
- `docker-compose.yml:9-18, 372-385`: `fc27_data` internal network and secrets configuration.
- `src/graph_api.py:223-246`: `_to_run_projection()` safe projection.

#### Proving Test Evidence
- `tests/test_db_roles.py`: `test_redact_conninfo`
- `tests/test_compose_isolation.py:15-45`: `test_only_graph_api_receives_approval_material`, `test_only_graph_api_can_reach_postgres`
- `tests/e2e/test_tier1_features.py`: `test_feature_38_compose_topology_and_file_secrets`

---

## 2. Verification of Phase 1 Completion Statement

The non-negotiable Phase 1 completion statement is:
> **"Secure data foundation complete. Dispatch remains disabled. No player delivery has occurred."**

### Verification Evidence & Analysis
1. **`DISPATCH_ENABLED=false` at all times**:
   - `src/graph_api.py:80`: Default setting is `dispatch_enabled: bool = False`.
   - `docker-compose.yml:202`: `DISPATCH_ENABLED: "false"`.
   - `src/graph_api.py:409-430`: Readiness probe `/health/ready` actively checks `DISPATCH_ENABLED`. If configured as true in Phase 1, it fails with HTTP 503: `"Phase 1 safety invariant violation: DISPATCH_ENABLED must be false"`.
2. **Database Level Delivery Block**:
   - `migrations/0003_dispatch_foundation.sql:1441-1445`: In stored procedure `public.fc27_dispatch_transition`, attempting transition to `p_to_status = 'delivered'` raises exception `FC002` ("Phase 1 invariant violation: transition to delivered is disabled (DISPATCH_ENABLED=false)").
   - Verified by `tests/test_dispatch_transition.py:296-309`: `test_phase1_delivered_edge_blocked`.
3. **Absence of Player Delivery Components**:
   - No player delivery adapters, workers, or webhook dispatchers exist in the repository.
   - `tests/test_phase1_scope.py:52-84` (`test_no_phase2plus_code`) proves zero worker, relay, or delivery code exists under `src/dispatch/`.
4. **Outbox Invariant**:
   - The transactional outbox table `public.outbox` records events atomically with domain events. In Phase 1, no relay worker runs (`published_at` remains NULL). The presence of an outbox row does not represent player dispatch.
   - Gate `G-OUTBOX` is explicitly blocked from being satisfied in Phase 1 (returns HTTP 409 `not_recordable_before_phase_3`).
5. **Terminal State Integrity**:
   - LangGraph workflows terminate exclusively in `APPROVED, NOT DISPATCHED` or `REJECTED, NO ACTION TAKEN`. No edges lead to dispatch or delivery.

**Verdict**: The completion statement is **VERIFIED AND TRUE**.

---

## 3. Phase 1 Implementation Summary

The Phase 1 implementation successfully establishes all required data, lifecycle, and API infrastructure:
- **Milestone M0 (Bootstrap & Roles)**: Created `fc27_owner` (NOLOGIN), `fc27_migrator`, `fc27_app`, and `fc27_dispatch` roles. Revoked public `CREATE`. Versioned migration runner and post-grants policy.
- **Milestone M1 (Approval Lifecycle & Section 1A)**: Implemented `approval_requests`, atomic terminal decision commit, 72h TTL, background expiry daemon, single-use decision nonces, pair consistency triggers, diagnostic views, and Section 1A compatibility tables (`authority_policies`, `player_assignments`, `player_decisions`, etc.).
- **Milestone M2 (Graph Core & Persistent API)**: Eliminated double interrupt (retained single interrupt in `approval` node). Implemented AM-4 PostgreSQL decision re-read on resume. Pinned terminal state wording. Deployed persistent FastAPI Graph API (`/v1/runs`, `/v1/runs/{id}`, `/v1/approval-requests/{id}/decisions`, `/v1/runs/{id}/resume`, health probes). Integrated correlation middleware and HMAC kill switch.
- **Milestone M3 (Dispatch Schema & Transition Function)**: Created `players`, `dispatches`, and forward-compatible tracking tables. Implemented `public.fc27_dispatch_transition` SECURITY DEFINER function with hardened search path, no dynamic SQL, and `dispatch_transition_rules` matrix.
- **Milestone M4 (Activation Gates & Atomic Dispatch Creation)**: Implemented rollout gate evaluation (`G-MIGRATION`..`G-OUTBOX`), dynamic background re-evaluation (C-1), RFC-8785 canonical JSON bytes, challenge/record endpoints (C-4), and atomic pending dispatch creation inside approval transaction.
- **Milestone M5 (Compose Topology & Scope Protection)**: Hardened Docker Compose with least-privilege roles, internal network isolation (`fc27_data`), file-based secrets (`./secrets/`), and created scope enforcement test suite (`test_phase1_scope.py`).
- **Milestone M6 (Final Verification & Trace)**: Verified 100% test pass rate across Tiers 1-4 E2E test suites, completed acceptance trace, security changes documentation, and Phase 2 handoff roadmap.

---

## 4. Phase 2 Handoff Roadmap: Identity and Authorization

At the Phase 1 boundary, implementation stops. Phase 2 owns the implementation of production identity, authentication, and human authority workflows.

### Phase 2 Architecture & Work Breakdown
1. **User Identity & Account Management**:
   - Local user accounts for Commissioner, staff roles, and players.
   - Account invitations and onboarding flows.
   - Encrypted identity secrets storage.
2. **Authentication & Session Infrastructure**:
   - Server-side secure session management.
   - Commissioner WebAuthn / FIDO2 passkey registration and sign-in.
   - Player passkey registration and sign-in for mobile/web clients.
   - TOTP protection for administrative staff roles.
3. **Step-Up Decision Authentication**:
   - WebAuthn step-up authentication challenge for Commissioner approval decisions.
   - Replacing symmetric HMAC decision proofs with asymmetric passkey assertion signatures.
4. **Delegated Human Authority Administration**:
   - Administrative endpoints for creating, activating, expiring, and revoking human delegations in `authority_delegations`.
   - Scoped delegation tokens restricting action types and scopes.
5. **Player Consent Pipeline**:
   - Player decision challenge endpoints (`POST /v1/player-decision-requests/{id}/challenge`).
   - Authenticated player decision submission (`POST /v1/player-decision-requests/{id}/decision`) recording player acceptance, rejection, pause, change request, or acknowledgment in `player_decisions`.
   - Action tokens bound to player assignments and session IDs.
6. **Escalation & Fallback Engine**:
   - Timer-driven evaluation of `approval_escalations` transitioning requests from `awaiting_primary` to `primary_reminder_due` and `evaluating_fallback`.
   - Policy-based fallback evaluation enabling eligible affected players to authorize personal developmental assignments.
7. **Authority Conflict Resolution**:
   - Detection and auditable recording of late or conflicting decisions in `authority_conflicts`.
   - Reconciliation workflows without mutating prior immutable audit records.
