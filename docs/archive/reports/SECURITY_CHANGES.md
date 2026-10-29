> Archived document
>
> This file is retained for historical context and is not an authoritative
> operational guide. See `docs/SECURITY_BOUNDARIES.md` for the current implementation.
> Archived on `2026-10-29` from `SECURITY_CHANGES.md`.

# Security Changes: Phase 1 Secure Data Foundation

Each entry documents the security requirement or defect, the hardened architecture and fix, and the proving test evidence. Test names refer to files located under `tests/`.

### Required Phase 1 Completion Statement
> **"Secure data foundation complete. Dispatch remains disabled. No player delivery has occurred."**

---

## 1. Database Role Separation & Least Privilege (C-6)

| # | Security Area | Architecture & Fix | Evidence |
|---|---|---|---|
| 1.1 | **Owner Role Isolation** | <ul><li>`fc27_owner` created as `NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT`. Direct authentication is forbidden.</li><li>Public schema `CREATE` privilege revoked from `PUBLIC`.</li><li>`fc27_owner` owns all tables, views, sequences, triggers, and procedures.</li><li>Ownership migration transfers legacy objects deterministically (`src/db_roles.py`).</li></ul> | `tests/test_db_roles.py::test_role_attributes`<br>`tests/test_db_roles.py::test_public_cannot_create_in_public_schema`<br>`tests/test_db_migrate_roles.py::test_objects_owned_by_fc27_owner`<br>`tests/e2e/test_tier1_features.py::TestDatabaseInfrastructureFeatures::test_feature_01_role_bootstrap_roles_exist_and_configured` |
| 1.2 | **Migration Role Scoping** | <ul><li>`fc27_migrator` created as `LOGIN NOINHERIT`, with role membership in `fc27_owner`.</li><li>Migration runner executes `SET ROLE fc27_owner` under advisory lock (`0x0FC27_0001`).</li><li>Checkpointer table setup (`setup()`) runs under owner role during migration, preventing runtime DDL.</li></ul> | `tests/test_db_migrate_roles.py::test_migrate_refuses_without_membership`<br>`tests/test_db_migrate_roles.py::test_checkpoint_setup_requires_owner`<br>`tests/test_db_roles.py::test_bootstrap_transfers_legacy_objects_owned_by_admin` |
| 1.3 | **Runtime Application Least Privilege** | <ul><li>`fc27_app` connects `graph-api` with DML privileges only.</li><li>Denied `CREATE` on schema `public`; cannot execute DDL.</li><li>Explicitly denied `UPDATE` and `DELETE` on `public.dispatches`.</li><li>Posture verification (`check_runtime_posture`) runs at startup and in `/health/ready`.</li></ul> | `tests/test_db_migrate_roles.py::test_app_cannot_run_ddl`<br>`tests/test_db_roles.py::test_check_runtime_posture`<br>`tests/test_graph_api_v1.py::test_adversarial_readiness_fails_closed_on_db_role_posture_violation`<br>`tests/e2e/test_tier1_features.py::TestDatabaseInfrastructureFeatures::test_feature_01_public_schema_create_revoked` |
| 1.4 | **Versioned Grants Policy** | <ul><li>`migrations/post/grants.sql` declares explicit version header (`-- grants-policy-version: 5`).</li><li>Migration runner validates grants policy against `GRANTS_POLICY` SHA-256 hash and records it in `schema_policy_versions`.</li></ul> | `tests/test_db_migrate_roles.py::test_grants_policy_constant_matches_file`<br>`tests/test_db_migrate_roles.py::test_grants_policy_recorded`<br>`tests/e2e/test_tier1_features.py::TestDatabaseInfrastructureFeatures::test_feature_03_grants_policy_versioning` |

---

## 2. SECURITY DEFINER Hardening (`public.fc27_dispatch_transition`)

| # | Security Area | Architecture & Fix | Evidence |
|---|---|---|---|
| 2.1 | **Search Path Isolation** | <ul><li>Stored procedure `public.fc27_dispatch_transition` configured with `SECURITY DEFINER` and owned by `fc27_owner`.</li><li>Explicitly isolated with `SET search_path = pg_catalog, pg_temp` to prevent search path hijacking.</li><li>`REVOKE ALL ON FUNCTION ... FROM PUBLIC;` and granted execute only to `fc27_app` and `fc27_dispatch`.</li></ul> | `tests/test_dispatch_transition.py::test_function_attributes`<br>`tests/test_dispatch_transition.py::test_runtime_roles_cannot_replace_function` |
| 2.2 | **No Dynamic SQL** | <ul><li>Zero dynamic SQL used in procedure body (no `EXECUTE` statement, no `format()`).</li><li>All table and view references are fully qualified relations.</li></ul> | `tests/test_dispatch_transition.py::test_function_source_has_no_dynamic_sql` |
| 2.3 | **Transition Rule Matrix** | <ul><li>Transitions validated against allowlist table `dispatch_transition_rules(from_status, to_status, db_role, actor_kind)`.</li><li>Role-based access enforces worker edges vs. app edges; illegal transitions raise SQLSTATE `FC002`.</li><li>Optimistic concurrency checks expected status and version, raising SQLSTATE `FC001` on mismatch.</li></ul> | `tests/test_dispatch_transition.py::test_illegal_transition_edge_rejected`<br>`tests/test_dispatch_transition.py::test_cross_role_transition_rejected`<br>`tests/test_dispatch_transition.py::test_stale_version_conflict` |
| 2.4 | **Phase 1 Delivery Block** | <ul><li>Explicit database check halts execution if `p_to_status = 'delivered'` with SQLSTATE `FC002` ("Phase 1 invariant violation: transition to delivered is disabled (DISPATCH_ENABLED=false)").</li><li>No code path in the database can transition any dispatch to delivered in Phase 1.</li></ul> | `tests/test_dispatch_transition.py::test_phase1_delivered_edge_blocked`<br>`tests/e2e/test_tier2_boundaries.py::TestStateAndLifecycleBoundaries::test_tier2_illegal_graph_threads_status_rejected_by_check` |

---

## 3. Approval Expiry Semantics

| # | Security Area | Architecture & Fix | Evidence |
|---|---|---|---|
| 3.1 | **Time-to-Live Clamping** | <ul><li>Approval requests have default 72-hour TTL (`approval_requests.expires_at = now() + 72h`).</li><li>Decision challenge nonces are clamped to 120s by default (max 300s, or request expiration, whichever is earlier).</li><li>Expired nonces cannot be signed or submitted (HTTP 409 `approval_expired`).</li></ul> | `tests/test_approval_security.py::test_nonce_expiration_is_enforced`<br>`tests/test_approval_lifecycle.py::test_expired_request_blocks_prior_nonce`<br>`tests/test_approval_lifecycle.py::test_expired_request_blocks_new_challenge` |
| 3.2 | **Atomic Expiry & Nonce Revocation** | <ul><li>Background daemon (`ApprovalStore.expire_due()`) scans due requests every 60s and runs proactively before challenges/decisions.</li><li>Single paired transaction transitions `approval_requests.status` and `graph_threads.status` to `'expired'`.</li><li>Revokes all open, unconsumed nonces for the thread (`revoked_at = now()`).</li><li>Emits `approval.expired` domain event and outbox record atomically.</li></ul> | `tests/test_approval_lifecycle.py::test_expire_due_is_idempotent`<br>`tests/test_approval_lifecycle.py::test_expiry_and_decision_race_has_single_outcome`<br>`tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_decision_vs_expiry_race_condition` |
| 3.3 | **Deadlock-Free Lock Hierarchy** | <ul><li>Strict global lock order: `approval_nonces` (ordered by nonce) → `approval_requests` (`FOR UPDATE`) → `graph_threads` (`FOR UPDATE`).</li><li>Guarantees deterministic resolution during concurrent expiry and decision submissions.</li></ul> | `tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_decision_vs_expiry_race_condition` |

---

## 4. Section 1A Human Authority & Player Consent Models

| # | Security Area | Architecture & Fix | Evidence |
|---|---|---|---|
| 4.1 | **Human-in-the-Loop Invariant** | <ul><li>Strict prohibition: *No agent may approve or reject for a human.*</li><li>Applies to club agents, coach agents, player agents, Graph nodes, LLMs, and background workers.</li><li>Submitting an agent actor name raises HTTP 403.</li></ul> | `src/approval_store.py:379-380`<br>`tests/test_approval_lifecycle.py::test_player_agent_cannot_be_decision_actor`<br>`docs/APPROVAL_SECURITY.md` |
| 4.2 | **Inactivity Invariant** | <ul><li>Strict prohibition: *Inactivity never constitutes approval.*</li><li>No timeout, absence, unread notification, or escalation state change can create an approval.</li><li>Timeouts transition requests to `'expired'` or update escalation history without writing `approval_audit`.</li></ul> | `tests/test_approval_lifecycle.py::test_timeout_inactivity_cannot_approve`<br>`tests/e2e/test_tier4_scenarios.py::TestSection1AHumanAuthorityAndConsent::test_section_1a_inactivity_and_timeout_never_creates_approval` |
| 4.3 | **Phase 1 Fail-Closed Policy** | <ul><li>Decisions in Phase 1 permit ONLY `actor_type == 'commissioner'` and `authority_basis == 'primary'`.</li><li>Requests with `actor_type == 'player'` or `authority_basis == 'affected_player_fallback'` raise HTTP 403.</li><li>Multi-player packages are prohibited from affected-player fallback eligibility.</li></ul> | `src/approval_store.py:381-386`<br>`tests/test_approval_lifecycle.py::test_unsupported_actor_types_fail_closed_in_phase1`<br>`tests/e2e/test_tier4_scenarios.py::TestSection1AHumanAuthorityAndConsent::test_section_1a_authority_separation_club_vs_player`<br>`tests/e2e/test_tier4_scenarios.py::TestSection1AHumanAuthorityAndConsent::test_section_1a_player_fallback_prohibits_multi_player_packages` |
| 4.4 | **Data Model Readiness** | <ul><li>Authoritative schema supports future multi-tier workflows without ambiguity: `authority_policies`, `authority_delegations`, `player_assignments`, `player_decision_requests`, `player_decisions`, `approval_escalations`, `authority_conflicts`.</li><li>`approval_audit` extended with actor type, actor ID, authority role, authority basis, decision scope, and conflict state.</li></ul> | `migrations/0003_dispatch_foundation.sql:464-674`<br>`tests/e2e/test_tier1_features.py::TestApprovalLifecycleFeatures::test_feature_16_human_authority_schema_compatibility` |

---

## 5. HMAC Kill Switch & Key Rotation

| # | Security Area | Architecture & Fix | Evidence |
|---|---|---|---|
| 5.1 | **CLI HMAC Kill Switch** | <ul><li>Configuration `APPROVAL_CLI_HMAC_ENABLED=false` disables CLI decision verification.</li><li>When disabled, `graph-api` starts without `APPROVAL_SIGNING_SECRET`, and calls to `/decision-challenge`, `/decision`, and activation gate recording fail closed with HTTP 403 `proof_type_disabled`.</li></ul> | `tests/test_graph_api_v1.py::test_hmac_kill_switch_behavior`<br>`tests/test_graph_api_v1.py::test_adversarial_hmac_kill_switch_blocks_before_body_parsing`<br>`tests/e2e/test_tier1_features.py::TestGraphCoreAndApiFeatures::test_feature_28_cli_hmac_kill_switch_support` |
| 5.2 | **Secret Rotation** | <ul><li>Rotating `APPROVAL_SIGNING_SECRET` invalidates pending unconsumed nonces.</li><li>Existing committed decisions remain verified with stored `signature_sha256`.</li><li>Verification uses `hmac.compare_digest` to prevent timing attacks.</li></ul> | `docs/APPROVAL_SECURITY.md`<br>`tests/test_approval_security.py::test_modified_signed_payload_is_rejected` |

---

## 6. Append-Only Audit & Immutability

| # | Security Area | Architecture & Fix | Evidence |
|---|---|---|---|
| 6.1 | **Mutation Rejection Trigger** | <ul><li>`public.fc27_reject_mutation()` trigger function attached `BEFORE UPDATE OR DELETE` to 13 immutable tables: `domain_events`, `approval_audit`, `activation_gate_log`, `player_decisions`, `approval_escalations`, `dispatch_plans`, `dispatch_plan_decisions`, `dispatch_delivery_attempts`, `dispatch_acknowledgments`, `dispatch_comments`, `analyst_notes`, `player_observations`, `dispatch_assessments`.</li><li>Raises check violation exception: `"<table_name> is append-only"`.</li></ul> | `tests/test_events_outbox.py::test_domain_events_append_only`<br>`tests/test_events_outbox.py::test_approval_audit_append_only`<br>`tests/test_dispatch_schema.py::test_tracking_tables_append_only`<br>`tests/e2e/test_tier1_features.py::TestDatabaseInfrastructureFeatures::test_feature_07_append_only_table_guards_reject_mutations` |
| 6.2 | **Terminal Record Freezing** | <ul><li>`fc27_guard_thread_transition()` blocks modification or deletion of `graph_threads` rows in terminal states (`approved`, `rejected`, `revision_requested`, `expired`).</li><li>`fc27_guard_approval_request_transition()` freezes `approval_requests` rows in terminal states.</li></ul> | `tests/test_migration_0003.py::test_package_version_immutable_while_awaiting`<br>`tests/test_dispatch_schema.py::test_dispatches_terminal_rows_frozen`<br>`tests/e2e/test_tier2_boundaries.py::TestStateAndLifecycleBoundaries::test_tier2_direct_update_of_package_version_blocked_by_trigger` |

---

## 7. Recommendation Package Version Immutability

| # | Security Area | Architecture & Fix | Evidence |
|---|---|---|---|
| 7.1 | **Deterministic Hash Binding** | <ul><li>`package_version(packages)` computes deterministic SHA-256 hash across canonical lead packages.</li><li>`graph_threads_guard` trigger forbids changing `package_version` once awaiting approval.</li><li>`fc27_guard_approval_request_transition` forbids changing `package_version`.</li></ul> | `tests/test_migration_0003.py::test_package_version_immutable_while_awaiting`<br>`tests/test_approval_security.py::test_package_version_immutable_once_awaiting_approval`<br>`tests/e2e/test_tier1_features.py::TestApprovalLifecycleFeatures::test_feature_08_approval_requests_table_schema` |
| 7.2 | **Stale Package Rejection** | <ul><li>Decision updates enforce `WHERE package_version = signed.package_version`. Altered candidate payloads return HTTP 409 conflict and roll back.</li><li>`REQUEST_REVISION` stores notes in audit while leaving package version immutable.</li></ul> | `tests/test_graph_persistence.py::test_decision_package_version_mismatch_fails_closed`<br>`tests/e2e/test_tier2_boundaries.py::TestStateAndLifecycleBoundaries::test_tier2_stale_package_version_decision_fails`<br>`tests/e2e/test_tier4_scenarios.py::TestRealWorldLifecycleScenarios::test_tier4_scenario_revision_request_preserves_package_immutability` |

---

## 8. LangGraph Interrupt & PostgreSQL Re-Read (AM-4)

| # | Security Area | Architecture & Fix | Evidence |
|---|---|---|---|
| 8.1 | **Single Interrupt Architecture** | <ul><li>Workflow pauses exactly once at node `approval` via `interrupt(state["approval_request"])`.</li><li>Double interruption (`interrupt_before=["approval"]`) eliminated.</li></ul> | `tests/test_graph_persistence.py::test_single_approval_interrupt_at_approval_node`<br>`tests/test_langgraph_flow.py::test_graph_runs_to_interrupt_with_typed_state`<br>`tests/e2e/test_tier1_features.py::TestGraphCoreAndApiFeatures::test_feature_18_single_approval_interrupt_verified` |
| 8.2 | **PostgreSQL Decision Re-Read** | <ul><li>Resume payload (`Command(resume=...)`, `POST /v1/runs/{id}/resume`) cannot carry authorization.</li><li>Node `approval` invokes `deps.decision_reader(thread_id)` to re-read committed PostgreSQL record.</li><li>Raises `ApprovalNotCommitted` if decision missing, package version mismatched, or approval request ID mismatched.</li><li>Formats terminal state: `'APPROVED, NOT DISPATCHED'`, `'REJECTED, NO ACTION TAKEN'`.</li></ul> | `tests/test_graph_persistence.py::test_terminal_graph_states_formatting`<br>`tests/test_langgraph_flow.py::test_existing_langgraph_thread_resumes`<br>`tests/test_graph_api_v1.py::test_v1_resume_refuses_authorization_payload`<br>`tests/e2e/test_tier1_features.py::TestGraphCoreAndApiFeatures::test_feature_20_terminal_states_enforcement_format` |
| 8.3 | **Pre-Resume Commit & Reconciliation** | <ul><li>Decision write path commits PostgreSQL transaction before invoking graph resume.</li><li>If resume fails, committed decision remains preserved in PostgreSQL; `graph-api` re-resumes terminal threads on startup.</li></ul> | `tests/test_graph_persistence.py::test_checkpoint_recovery_across_server_restart`<br>`tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_post_commit_resume_failure_preserves_authoritative_decision` |

---

## 9. Nonce-Bound & Deployment-Bound Activation Gates (C-4, C-1)

| # | Security Area | Architecture & Fix | Evidence |
|---|---|---|---|
| 9.1 | **Rollout Gate Hierarchy** | <ul><li>Six rollout gates evaluated in order: `G-MIGRATION`, `G-GRANTS`, `G-WORKER`, `G-ID`, `G-ATOMIC`, `G-OUTBOX`.</li><li>`G-WORKER` fails closed in Phase 1 (no production worker).</li><li>`G-OUTBOX` prohibited in Phase 1 (`RECORDABLE_GATES = {"G-ID", "G-ATOMIC"}`; returns 409 `not_recordable_before_phase_3`).</li></ul> | `tests/test_dispatch_creation.py::test_gates_block_by_default`<br>`tests/test_dispatch_creation.py::test_g_outbox_not_recordable_in_phase1`<br>`tests/e2e/test_tier1_features.py::TestObjectivesAndGatesFeatures::test_feature_35_activation_gate_evaluation_fails_closed` |
| 9.2 | **Canonical Bytes & Deployment Binding** | <ul><li>Manual gates recorded via challenge (`POST /v1/activation-gates/{id}/challenge`) and record routes.</li><li>Payload canonicalized using RFC-8785 rules (`canonical_gate_bytes`) and signed with HMAC-SHA256.</li><li>Binds strictly to deployment identity: `code_revision`, `deployment_id`, `fc_version`, `platform`.</li><li>Appended to immutable `activation_gate_log`.</li></ul> | `tests/test_dispatch_creation.py::test_gate_recording_requires_commissioner_and_signature`<br>`tests/test_dispatch_creation.py::test_gate_tampered_field_rejected`<br>`tests/test_dispatch_creation.py::test_gate_requires_deployment_identity`<br>`tests/e2e/test_tier1_features.py::TestObjectivesAndGatesFeatures::test_feature_36_gate_challenge_and_record_routes_exist` |
| 9.3 | **Dynamic Re-Evaluation (C-1)** | <ul><li>Background loop in `graph-api` re-evaluates gates every 60s without process restart.</li><li>Updates `app.state.dispatch_effective`.</li><li>`/health/ready` probe verifies gates and fails closed if `DISPATCH_ENABLED=true` is set.</li></ul> | `src/graph_api.py:140-170, 314-316`<br>`tests/test_dispatch_creation.py::test_gate_change_applies_on_next_evaluation` |

---

## 10. Event Source Identity & Correlation ID Isolation (C-10, C-5)

| # | Security Area | Architecture & Fix | Evidence |
|---|---|---|---|
| 10.1 | **Source Verification Trigger** | <ul><li>`public.fc27_check_event_source()` verifies mandatory sourced events (`approval.decided`, `approval.created`, `dispatch.created`, `activation.gate_recorded`) match real rows in source tables.</li></ul> | `tests/test_events_outbox.py::test_sourced_event_requires_existing_source`<br>`tests/e2e/test_tier1_features.py::TestDatabaseInfrastructureFeatures::test_feature_05_event_source_guard_missing_source_rejected` |
| 10.2 | **Transition-Only Event Origin (C-10)** | <ul><li>Events `dispatch.*` (except created), `plan.*`, and `tracking.*` can only be inserted when `current_user = 'fc27_owner'` and `session_user IN ('fc27_app', 'fc27_dispatch')`.</li><li>Direct inserts outside the definer function are blocked.</li></ul> | `tests/test_dispatch_transition.py::test_worker_actor_derivation`<br>`tests/test_events_outbox.py::test_app_cannot_forge_dispatch_events`<br>`tests/e2e/test_tier1_features.py::TestDatabaseInfrastructureFeatures::test_feature_06_transition_event_source_identity_restricted` |
| 10.3 | **Correlation ID Propagation (C-5)** | <ul><li>`CorrelationMiddleware` validates canonical 36-character UUIDs; rejects invalid headers with HTTP 422.</li><li>Attached to ContextVar `correlation_id_var`, logs (`CorrelationIdFilter`), domain events, outbox rows, and response headers.</li><li>Excluded from decision signature payloads to prevent header-based signature invalidation.</li></ul> | `tests/test_graph_api_v1.py::test_correlation_id_generated_when_absent`<br>`tests/test_graph_api_v1.py::test_correlation_id_echoed_when_present`<br>`tests/test_graph_api_v1.py::test_correlation_id_propagated_to_logs`<br>`tests/test_graph_api_v1.py::test_malformed_correlation_id_rejected`<br>`tests/test_graph_api_v1.py::test_adversarial_malformed_correlation_id_rejected_and_redacted`<br>`tests/test_dispatch_creation.py::test_correlation_id_shared_by_dispatch_and_events`<br>`tests/e2e/test_tier1_features.py::TestGraphCoreAndApiFeatures::test_feature_21_correlation_middleware_propagation` |
| 10.4 | **Zero-Echo Error Sanitization** | <ul><li>`RequestValidationError` handler strips the `input` field from error responses, preventing model prompt reflection and recommendation parameter leakage.</li></ul> | `src/graph_api.py:342-351`<br>`tests/test_graph_api_v1.py::test_adversarial_pydantic_validation_strips_sensitive_inputs` |

---

## 11. File-Based Secrets & Credential Sanitization (C-7)

| # | Security Area | Architecture & Fix | Evidence |
|---|---|---|---|
| 11.1 | **File Secret Mounting** | <ul><li>`src/settings.py` resolves `<NAME>_FILE` environment variables pointing to mounted secret files in `./secrets/`.</li><li>No raw passwords or connection URIs exposed in process environment or `x-common`.</li></ul> | `src/settings.py:22-38`<br>`tests/test_compose_isolation.py::test_signing_secret_only_in_graph_api`<br>`tests/test_compose_isolation.py::test_db_secret_mounts_per_service` |
| 11.2 | **Internal Network Isolation** | <ul><li>PostgreSQL network `fc27_data` is marked `internal: true`. Joined ONLY by `postgres`, `db-bootstrap`, `db-migrate`, and `graph-api`.</li><li>Unrelated containers (`ws-server`, `fc27-mcp`, `live-watcher`) have zero network route to PostgreSQL.</li></ul> | `docker-compose.yml:13-17`<br>`tests/test_compose_isolation.py::test_only_graph_api_can_reach_postgres` |
| 11.3 | **Connection String Redaction** | <ul><li>`redact_conninfo()` replaces credentials in URIs (`:***@`) and key-value strings (`password=***`).</li><li>Integrated in migration errors, CLI runners, and logging.</li><li>Public run projections (`GET /v1/runs/{id}`) omit internal checkpoint internals and secrets.</li></ul> | `src/db_roles.py:23-39`<br>`tests/test_db_roles.py::test_redact_conninfo`<br>`tests/test_graph_api_v1.py::test_v1_get_run_safe_projection_omits_secrets`<br>`tests/test_graph_api_v1.py::test_adversarial_run_projection_scrubs_internal_secrets` |

---

## 12. Residual Risks & Future-Phase Mitigation Plan

1. **Symmetric HMAC Signing Secret**:
   - *Risk*: `APPROVAL_SIGNING_SECRET` is shared between `graph-api` and Commissioner workstation. A compromised `graph-api` container could forge decisions.
   - *Phase 2 Mitigation*: Transition to asymmetric WebAuthn / Passkey assertions (Ed25519/ECDSA), where private keys remain strictly on the Commissioner's hardware authenticator.
2. **Redis Shared Bus**:
   - *Risk*: Redis uses a shared password without per-service role-based ACLs.
   - *Mitigation*: Redis notifications are strictly transient wake-up signals; PostgreSQL remains the sole authoritative store. A compromised Redis cannot alter approval or dispatch state.
3. **LiteLLM Vulnerability in Swarms**:
   - *Risk*: `swarms==15.0.3` pins `litellm==1.76.1`.
   - *Mitigation*: Stack uses `invoke_role` client interface only; the LiteLLM proxy server is never run. Alternatively, `ROLE_BACKEND=native` eliminates LiteLLM entirely.
4. **Offline EA Validation**:
   - *Risk*: Live EA endpoint behavior and live model inference are simulated via deterministic test fixtures in Phase 1.
   - *Phase 3 Mitigation*: Live match integration validation in Phase 3.

---

## 13. Phase 1 Implementation Summary

The Phase 1 implementation successfully establishes all required data, lifecycle, and API infrastructure:
- **Milestone M0 (Bootstrap & Roles)**: Created `fc27_owner` (NOLOGIN), `fc27_migrator`, `fc27_app`, and `fc27_dispatch` roles. Revoked public `CREATE`. Versioned migration runner and post-grants policy.
- **Milestone M1 (Approval Lifecycle & Section 1A)**: Implemented `approval_requests`, atomic terminal decision commit, 72h TTL, background expiry daemon, single-use decision nonces, pair consistency triggers, diagnostic views, and Section 1A compatibility tables (`authority_policies`, `player_assignments`, `player_decisions`, etc.).
- **Milestone M2 (Graph Core & Persistent API)**: Eliminated double interrupt (retained single interrupt in `approval` node). Implemented AM-4 PostgreSQL decision re-read on resume. Pinned terminal state wording. Deployed persistent FastAPI Graph API (`/v1/runs`, `/v1/runs/{id}`, `/v1/approval-requests/{id}/decisions`, `/v1/runs/{id}/resume`, health probes). Integrated correlation middleware and HMAC kill switch.
- **Milestone M3 (Dispatch Schema & Transition Function)**: Created `players`, `dispatches`, and forward-compatible tracking tables. Implemented `public.fc27_dispatch_transition` SECURITY DEFINER function with hardened search path, no dynamic SQL, and `dispatch_transition_rules` matrix.
- **Milestone M4 (Activation Gates & Atomic Dispatch Creation)**: Implemented rollout gate evaluation (`G-MIGRATION`..`G-OUTBOX`), dynamic background re-evaluation (C-1), RFC-8785 canonical JSON bytes, challenge/record endpoints (C-4), and atomic pending dispatch creation inside approval transaction.
- **Milestone M5 (Compose Topology & Scope Protection)**: Hardened Docker Compose with least-privilege roles, internal network isolation (`fc27_data`), file-based secrets (`./secrets/`), and created scope enforcement test suite (`test_phase1_scope.py`).
- **Milestone M6 (Final Verification & Trace)**: Verified 100% test pass rate across Tiers 1-4 E2E test suites, completed acceptance trace, security changes documentation, and Phase 2 handoff roadmap.

---

## 14. Phase 2 Handoff Roadmap: Identity and Authorization

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
