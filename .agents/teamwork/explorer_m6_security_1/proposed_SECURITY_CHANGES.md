# Security Changes: Phase 1 Secure Data Foundation

Each entry documents the security requirement or defect, the hardened architecture and fix, and the proving test evidence. Test names refer to files located under `tests/`.

---

## 1. Database Role Separation & Least Privilege (C-6)

| # | Security Area | Architecture & Fix | Evidence |
|---|---|---|---|
| 1.1 | **Owner Role Isolation** | <ul><li>`fc27_owner` created as `NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT`. Direct authentication is forbidden.</li><li>Public schema `CREATE` privilege revoked from `PUBLIC`.</li><li>`fc27_owner` owns all tables, views, sequences, triggers, and procedures.</li><li>Ownership migration transfers legacy objects deterministically (`src/db_roles.py`).</li></ul> | `test_db_roles.py::test_role_attributes`<br>`test_db_roles.py::test_public_cannot_create_in_public_schema`<br>`test_db_migrate_roles.py::test_objects_owned_by_fc27_owner`<br>`e2e/test_tier1_features.py::test_feature_01_role_bootstrap_roles_exist_and_configured` |
| 1.2 | **Migration Role Scoping** | <ul><li>`fc27_migrator` created as `LOGIN NOINHERIT`, with role membership in `fc27_owner`.</li><li>Migration runner executes `SET ROLE fc27_owner` under advisory lock (`0x0FC27_0001`).</li><li>Checkpointer table setup (`setup()`) runs under owner role during migration, preventing runtime DDL.</li></ul> | `test_db_migrate_roles.py::test_migrate_refuses_without_membership`<br>`test_db_migrate_roles.py::test_checkpoint_setup_requires_owner`<br>`test_db_roles.py::test_bootstrap_transfers_legacy_objects_owned_by_admin` |
| 1.3 | **Runtime Application Least Privilege** | <ul><li>`fc27_app` connects `graph-api` with DML privileges only.</li><li>Denied `CREATE` on schema `public`; cannot execute DDL.</li><li>Explicitly denied `UPDATE` and `DELETE` on `public.dispatches`.</li><li>Posture verification (`check_runtime_posture`) runs at startup and in `/health/ready`.</li></ul> | `test_db_migrate_roles.py::test_app_cannot_run_ddl`<br>`test_db_roles.py::test_runtime_posture_clean`<br>`test_graph_api_v1.py::test_health_ready_checks_db_posture`<br>`e2e/test_tier1_features.py::test_feature_01_public_schema_create_revoked` |
| 1.4 | **Versioned Grants Policy** | <ul><li>`migrations/post/grants.sql` declares explicit version header (`-- grants-policy-version: 5`).</li><li>Migration runner validates grants policy against `GRANTS_POLICY` SHA-256 hash and records it in `schema_policy_versions`.</li></ul> | `test_db_migrate_roles.py::test_grants_policy_version_check`<br>`e2e/test_tier1_features.py::test_feature_03_grants_policy_versioning` |

---

## 2. SECURITY DEFINER Hardening (`public.fc27_dispatch_transition`)

| # | Security Area | Architecture & Fix | Evidence |
|---|---|---|---|
| 2.1 | **Search Path Isolation** | <ul><li>Stored procedure `public.fc27_dispatch_transition` configured with `SECURITY DEFINER` and owned by `fc27_owner`.</li><li>Explicitly isolated with `SET search_path = pg_catalog, pg_temp` to prevent search path hijacking.</li><li>`REVOKE ALL ON FUNCTION ... FROM PUBLIC;` and granted execute only to `fc27_app` and `fc27_dispatch`.</li></ul> | `test_dispatch_transition.py::test_function_attributes`<br>`test_dispatch_transition.py::test_runtime_roles_cannot_replace_function` |
| 2.2 | **No Dynamic SQL** | <ul><li>Zero dynamic SQL used in procedure body (no `EXECUTE` statement, no `format()`).</li><li>All table and view references are fully qualified relations.</li></ul> | `test_dispatch_transition.py::test_function_source_has_no_dynamic_sql` |
| 2.3 | **Transition Rule Matrix** | <ul><li>Transitions validated against allowlist table `dispatch_transition_rules(from_status, to_status, db_role, actor_kind)`.</li><li>Role-based access enforces worker edges vs. app edges; illegal transitions raise SQLSTATE `FC002`.</li><li>Optimistic concurrency checks expected status and version, raising SQLSTATE `FC001` on mismatch.</li></ul> | `test_dispatch_transition.py::test_illegal_transition_edge_rejected`<br>`test_dispatch_transition.py::test_cross_role_transition_rejected`<br>`test_dispatch_transition.py::test_stale_version_conflict` |
| 2.4 | **Phase 1 Delivery Block** | <ul><li>Explicit database check halts execution if `p_to_status = 'delivered'` with SQLSTATE `FC002` ("Phase 1 invariant violation: transition to delivered is disabled (DISPATCH_ENABLED=false)").</li><li>No code path in the database can transition any dispatch to delivered in Phase 1.</li></ul> | `test_dispatch_transition.py::test_phase1_delivered_edge_blocked`<br>`e2e/test_tier2_boundaries.py::test_phase1_delivery_transition_blocked` |

---

## 3. Approval Expiry Semantics

| # | Security Area | Architecture & Fix | Evidence |
|---|---|---|---|
| 3.1 | **Time-to-Live Clamping** | <ul><li>Approval requests have default 72-hour TTL (`approval_requests.expires_at = now() + 72h`).</li><li>Decision challenge nonces are clamped to 120s by default (max 300s, or request expiration, whichever is earlier).</li><li>Expired nonces cannot be signed or submitted (HTTP 409 `approval_expired`).</li></ul> | `test_approval_security.py::test_nonce_expiration_is_enforced`<br>`test_approval_lifecycle.py::test_approval_request_ttl_clamping` |
| 3.2 | **Atomic Expiry & Nonce Revocation** | <ul><li>Background daemon (`ApprovalStore.expire_due()`) scans due requests every 60s and runs proactively before challenges/decisions.</li><li>Single paired transaction transitions `approval_requests.status` and `graph_threads.status` to `'expired'`.</li><li>Revokes all open, unconsumed nonces for the thread (`revoked_at = now()`).</li><li>Emits `approval.expired` domain event and outbox record atomically.</li></ul> | `test_approval_lifecycle.py::test_approval_request_expiry_daemon`<br>`e2e/test_tier3_combinations.py::test_tier3_decision_vs_expiry_race_condition` |
| 3.3 | **Deadlock-Free Lock Hierarchy** | <ul><li>Strict global lock order: `approval_nonces` (ordered by nonce) → `approval_requests` (`FOR UPDATE`) → `graph_threads` (`FOR UPDATE`).</li><li>Guarantees deterministic resolution during concurrent expiry and decision submissions.</li></ul> | `e2e/test_tier3_combinations.py::test_tier3_decision_vs_expiry_race_condition` |

---

## 4. Section 1A Human Authority & Player Consent Models

| # | Security Area | Architecture & Fix | Evidence |
|---|---|---|---|
| 4.1 | **Human-in-the-Loop Invariant** | <ul><li>Strict prohibition: *No agent may approve or reject for a human.*</li><li>Applies to club agents, coach agents, player agents, Graph nodes, LLMs, and background workers.</li><li>Submitting an agent actor name raises HTTP 403.</li></ul> | `src/approval_store.py:379-380`<br>`test_m1_adversarial_challenger.py::test_agent_cannot_decide`<br>`docs/APPROVAL_SECURITY.md` |
| 4.2 | **Inactivity Invariant** | <ul><li>Strict prohibition: *Inactivity never constitutes approval.*</li><li>No timeout, absence, unread notification, or escalation state change can create an approval.</li><li>Timeouts transition requests to `'expired'` or update escalation history without writing `approval_audit`.</li></ul> | `e2e/test_tier4_scenarios.py::test_section_1a_inactivity_and_timeout_never_creates_approval`<br>`test_approval_lifecycle.py::test_timeout_never_creates_approval` |
| 4.3 | **Phase 1 Fail-Closed Policy** | <ul><li>Decisions in Phase 1 permit ONLY `actor_type == 'commissioner'` and `authority_basis == 'primary'`.</li><li>Requests with `actor_type == 'player'` or `authority_basis == 'affected_player_fallback'` raise HTTP 403.</li><li>Multi-player packages are prohibited from affected-player fallback eligibility.</li></ul> | `src/approval_store.py:381-386`<br>`e2e/test_tier4_scenarios.py::test_section_1a_authority_separation_club_vs_player`<br>`e2e/test_tier4_scenarios.py::test_section_1a_player_fallback_prohibits_multi_player_packages` |
| 4.4 | **Data Model Readiness** | <ul><li>Authoritative schema supports future multi-tier workflows without ambiguity: `authority_policies`, `authority_delegations`, `player_assignments`, `player_decision_requests`, `player_decisions`, `approval_escalations`, `authority_conflicts`.</li><li>`approval_audit` extended with actor type, actor ID, authority role, authority basis, decision scope, and conflict state.</li></ul> | `migrations/0003_dispatch_foundation.sql:464-674`<br>`e2e/test_tier1_features.py::test_feature_16_human_authority_schema_compatibility` |

---

## 5. HMAC Kill Switch & Key Rotation

| # | Security Area | Architecture & Fix | Evidence |
|---|---|---|---|
| 5.1 | **CLI HMAC Kill Switch** | <ul><li>Configuration `APPROVAL_CLI_HMAC_ENABLED=false` disables CLI decision verification.</li><li>When disabled, `graph-api` starts without `APPROVAL_SIGNING_SECRET`, and calls to `/decision-challenge`, `/decision`, and activation gate recording fail closed with HTTP 403 `proof_type_disabled`.</li></ul> | `test_graph_api_v1.py::test_hmac_kill_switch`<br>`e2e/test_tier1_features.py::test_feature_28_hmac_kill_switch` |
| 5.2 | **Secret Rotation** | <ul><li>Rotating `APPROVAL_SIGNING_SECRET` invalidates pending unconsumed nonces.</li><li>Existing committed decisions remain verified with stored `signature_sha256`.</li><li>Verification uses `hmac.compare_digest` to prevent timing attacks.</li></ul> | `docs/APPROVAL_SECURITY.md`<br>`test_approval_security.py::test_modified_signed_payload_is_rejected` |

---

## 6. Append-Only Audit & Immutability

| # | Security Area | Architecture & Fix | Evidence |
|---|---|---|---|
| 6.1 | **Mutation Rejection Trigger** | <ul><li>`public.fc27_reject_mutation()` trigger function attached `BEFORE UPDATE OR DELETE` to 13 immutable tables: `domain_events`, `approval_audit`, `activation_gate_log`, `player_decisions`, `approval_escalations`, `dispatch_plans`, `dispatch_plan_decisions`, `dispatch_delivery_attempts`, `dispatch_acknowledgments`, `dispatch_comments`, `analyst_notes`, `player_observations`, `dispatch_assessments`.</li><li>Raises check violation exception: `"<table_name> is append-only"`.</li></ul> | `test_events_outbox.py::test_domain_events_append_only`<br>`test_events_outbox.py::test_approval_audit_append_only`<br>`test_dispatch_schema.py::test_append_only_triggers`<br>`e2e/test_tier1_features.py::test_feature_07_append_only_table_guards` |
| 6.2 | **Terminal Record Freezing** | <ul><li>`fc27_guard_thread_transition()` blocks modification or deletion of `graph_threads` rows in terminal states (`approved`, `rejected`, `revision_requested`, `expired`).</li><li>`fc27_guard_approval_request_transition()` freezes `approval_requests` rows in terminal states.</li></ul> | `test_approval_lifecycle.py::test_terminal_thread_cannot_be_modified`<br>`test_approval_lifecycle.py::test_terminal_approval_request_frozen` |

---

## 7. Recommendation Package Version Immutability

| # | Security Area | Architecture & Fix | Evidence |
|---|---|---|---|
| 7.1 | **Deterministic Hash Binding** | <ul><li>`package_version(packages)` computes deterministic SHA-256 hash across canonical lead packages.</li><li>`graph_threads_guard` trigger forbids changing `package_version` once awaiting approval.</li><li>`fc27_guard_approval_request_transition` forbids changing `package_version`.</li></ul> | `test_approval_lifecycle.py::test_package_version_immutable`<br>`e2e/test_tier1_features.py::test_feature_08_approval_requests_lifecycle` |
| 7.2 | **Stale Package Rejection** | <ul><li>Decision updates enforce `WHERE package_version = signed.package_version`. Altered candidate payloads return HTTP 409 conflict and roll back.</li><li>`REQUEST_REVISION` stores notes in audit while leaving package version immutable.</li></ul> | `test_approval_lifecycle.py::test_stale_package_version_rejected`<br>`e2e/test_tier4_scenarios.py::test_tier4_scenario_revision_request_preserves_package_immutability` |

---

## 8. LangGraph Interrupt & PostgreSQL Re-Read (AM-4)

| # | Security Area | Architecture & Fix | Evidence |
|---|---|---|---|
| 8.1 | **Single Interrupt Architecture** | <ul><li>Workflow pauses exactly once at node `approval` via `interrupt(state["approval_request"])`.</li><li>Double interruption (`interrupt_before=["approval"]`) eliminated.</li></ul> | `test_langgraph_flow.py::test_single_interrupt_per_lifecycle`<br>`e2e/test_tier1_features.py::test_feature_18_single_approval_interrupt` |
| 8.2 | **PostgreSQL Decision Re-Read** | <ul><li>Resume payload (`Command(resume=...)`, `POST /v1/runs/{id}/resume`) cannot carry authorization.</li><li>Node `approval` invokes `deps.decision_reader(thread_id)` to re-read committed PostgreSQL record.</li><li>Raises `ApprovalNotCommitted` if decision missing, package version mismatched, or approval request ID mismatched.</li><li>Formats terminal state: `'APPROVED, NOT DISPATCHED'`, `'REJECTED, NO ACTION TAKEN'`.</li></ul> | `test_langgraph_flow.py::test_langgraph_interrupt_cannot_be_bypassed`<br>`test_langgraph_flow.py::test_terminal_state_text_approved_not_dispatched`<br>`test_graph_api_v1.py::test_resume_cannot_authorize` |
| 8.3 | **Pre-Resume Commit & Reconciliation** | <ul><li>Decision write path commits PostgreSQL transaction before invoking graph resume.</li><li>If resume fails, committed decision remains preserved in PostgreSQL; `graph-api` re-resumes terminal threads on startup.</li></ul> | `test_graph_persistence.py::test_restart_recovers_interrupted_graph`<br>`e2e/test_tier3_combinations.py::test_tier3_post_commit_resume_failure_recovery` |

---

## 9. Nonce-Bound & Deployment-Bound Activation Gates (C-4, C-1)

| # | Security Area | Architecture & Fix | Evidence |
|---|---|---|---|
| 9.1 | **Rollout Gate Hierarchy** | <ul><li>Six rollout gates evaluated in order: `G-MIGRATION`, `G-GRANTS`, `G-WORKER`, `G-ID`, `G-ATOMIC`, `G-OUTBOX`.</li><li>`G-WORKER` fails closed in Phase 1 (no production worker).</li><li>`G-OUTBOX` prohibited in Phase 1 (`RECORDABLE_GATES = {"G-ID", "G-ATOMIC"}`; returns 409 `not_recordable_before_phase_3`).</li></ul> | `test_activation_gates.py::test_evaluate_gates_order`<br>`test_activation_gates.py::test_g_outbox_refused_in_phase1`<br>`e2e/test_tier1_features.py::test_feature_35_activation_gate_evaluation` |
| 9.2 | **Canonical Bytes & Deployment Binding** | <ul><li>Manual gates recorded via challenge (`POST /v1/activation-gates/{id}/challenge`) and record routes.</li><li>Payload canonicalized using RFC-8785 rules (`canonical_gate_bytes`) and signed with HMAC-SHA256.</li><li>Binds strictly to deployment identity: `code_revision`, `deployment_id`, `fc_version`, `platform`.</li><li>Appended to immutable `activation_gate_log`.</li></ul> | `test_activation_gates.py::test_canonical_gate_bytes_rfc8785`<br>`test_graph_api_v1.py::test_gate_challenge_and_record_flow`<br>`e2e/test_tier1_features.py::test_feature_36_gate_challenge_and_record` |
| 9.3 | **Dynamic Re-Evaluation (C-1)** | <ul><li>Background loop in `graph-api` re-evaluates gates every 60s without process restart.</li><li>Updates `app.state.dispatch_effective`.</li><li>`/health/ready` probe verifies gates and fails closed if `DISPATCH_ENABLED=true` is set.</li></ul> | `src/graph_api.py:140-170, 314-316`<br>`test_graph_api_v1.py::test_dynamic_gate_reevaluation` |

---

## 10. Event Source Identity & Correlation ID Isolation (C-10, C-5)

| # | Security Area | Architecture & Fix | Evidence |
|---|---|---|---|
| 10.1 | **Source Verification Trigger** | <ul><li>`public.fc27_check_event_source()` verifies mandatory sourced events (`approval.decided`, `approval.created`, `dispatch.created`, `activation.gate_recorded`) match real rows in source tables.</li></ul> | `test_events_outbox.py::test_domain_events_source_check`<br>`e2e/test_tier1_features.py::test_feature_05_event_source_guard` |
| 10.2 | **Transition-Only Event Origin (C-10)** | <ul><li>Events `dispatch.*` (except created), `plan.*`, and `tracking.*` can only be inserted when `current_user = 'fc27_owner'` and `session_user IN ('fc27_app', 'fc27_dispatch')`.</li><li>Direct inserts outside the definer function are blocked.</li></ul> | `test_dispatch_transition.py::test_worker_actor_derivation`<br>`e2e/test_tier1_features.py::test_feature_06_transition_event_source_identity` |
| 10.3 | **Correlation ID Propagation (C-5)** | <ul><li>`CorrelationMiddleware` validates canonical 36-character UUIDs; rejects invalid headers with HTTP 422.</li><li>Attached to ContextVar `correlation_id_var`, logs (`CorrelationIdFilter`), domain events, outbox rows, and response headers.</li><li>Excluded from decision signature payloads to prevent header-based signature invalidation.</li></ul> | `test_correlation.py::test_valid_correlation_id_propagated`<br>`test_correlation.py::test_malformed_correlation_id_returns_422`<br>`e2e/test_tier1_features.py::test_feature_21_correlation_middleware` |
| 10.4 | **Zero-Echo Error Sanitization** | <ul><li>`RequestValidationError` handler strips the `input` field from error responses, preventing model prompt reflection and recommendation parameter leakage.</li></ul> | `src/graph_api.py:342-351`<br>`test_graph_api_v1.py::test_validation_error_strips_input` |

---

## 11. File-Based Secrets & Credential Sanitization (C-7)

| # | Security Area | Architecture & Fix | Evidence |
|---|---|---|---|
| 11.1 | **File Secret Mounting** | <ul><li>`src/settings.py` resolves `<NAME>_FILE` environment variables pointing to mounted secret files in `./secrets/`.</li><li>No raw passwords or connection URIs exposed in process environment or `x-common`.</li></ul> | `src/settings.py:22-38`<br>`test_compose_isolation.py::test_only_graph_api_receives_approval_material` |
| 11.2 | **Internal Network Isolation** | <ul><li>PostgreSQL network `fc27_data` is marked `internal: true`. Joined ONLY by `postgres`, `db-bootstrap`, `db-migrate`, and `graph-api`.</li><li>Unrelated containers (`ws-server`, `fc27-mcp`, `live-watcher`) have zero network route to PostgreSQL.</li></ul> | `docker-compose.yml:13-17`<br>`test_compose_isolation.py::test_only_graph_api_can_reach_postgres` |
| 11.3 | **Connection String Redaction** | <ul><li>`redact_conninfo()` replaces credentials in URIs (`:***@`) and key-value strings (`password=***`).</li><li>Integrated in migration errors, CLI runners, and logging.</li><li>Public run projections (`GET /v1/runs/{id}`) omit internal checkpoint internals and secrets.</li></ul> | `src/db_roles.py:23-39`<br>`test_db_roles.py::test_redact_conninfo`<br>`test_graph_api_v1.py::test_run_projection_omits_secrets` |

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
