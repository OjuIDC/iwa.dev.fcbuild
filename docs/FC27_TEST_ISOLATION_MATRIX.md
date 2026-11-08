# FC27 Test Isolation Matrix and Pytest Marker Reference

## 1. Overview and Authority

This document is the authoritative test-isolation classification matrix for all `130` test modules in `iwa.dev.fcbuild`. It is backed in code by `src/fc27_ci_test_groups.py`, enforced dynamically during pytest collection by `tests/conftest.py` (`pytest_collection_modifyitems`), and verified in CI by `tests/test_github_workflows.py` (`test_collection_consistency_and_shard_disjointness`).

- **Fail-Safe Default**: Any newly added test module not explicitly registered in `TEST_MODULE_MATRIX` defaults automatically to `UNKNOWN_SERIAL` (`serial`, `offline`), ensuring unknown tests are never silently dropped or parallelized unsafely.
- **Collection Completeness Invariant**:
  - `COLLECTION_UNION_COMPLETE=YES` (`full pytest --collect-only` == union of all maintained test groups == union of all 4 full CI shards).
  - `COLLECTION_INTERSECTION_VALID=YES` (maintained test groups are mutually disjoint; full CI shards `full-quality`, `full-unit-domain`, `full-postgres`, and `full-compose` are mutually disjoint).

---

## 2. Canonical Test Isolation Classifications (11 Categories)

| Classification | Execution Mode | Description |
| :--- | :--- | :--- |
| `PURE_UNIT_PARALLEL_SAFE` | `parallel-xdist` | Pure in-memory unit tests without database, Docker, shared files, or external network access. |
| `OFFLINE_DOMAIN_PARALLEL_SAFE` | `parallel-xdist` | Offline FC27 domain, analytics, contract, and workflow tests using `tmp_path` or ephemeral port-0 loopback sockets. |
| `POSTGRES_PARALLEL_SAFE` | `parallel-xdist` | PostgreSQL 16 integration tests that operate exclusively within their own worker-isolated per-test database (`fc27_test_<worker>_<uuid>` cloned from `fc27_tmpl_<worker>_<uuid>`) without mutating cluster-global roles. |
| `POSTGRES_SERIAL` | `serial` | PostgreSQL tests that mutate cluster-global role membership (`GRANT/REVOKE fc27_owner TO fc27_app`), create cluster roles (`fc27_reader`), execute `pg_dump`/`pg_restore`, or run multi-thread concurrency races. |
| `DOCKER_SERIAL` | `serial` | Tests invoking `docker` or `docker compose` CLI across profiles (`tests/test_compose_isolation.py`). |
| `FIXED_PORT_SERIAL` | `serial` | Tests starting local HTTP fixture servers on loopback ports (`tests/test_fc27_demo.py`). |
| `FILESYSTEM_SERIAL` | `serial` | Tests manipulating working directories or process environment for subprocesses, plus the Visual Studio MSBuild integration that writes ignored intermediates (`tests/test_swarms_backend.py`, `tests/test_visual_studio_project.py`). |
| `PERFORMANCE_SERIAL` | `serial` | Performance telemetry, latency benchmark, and `EXPLAIN ANALYZE` suites (`tests/test_fc27_performance.py`). |
| `DOCUMENTATION_PARALLEL_SAFE` | `parallel-xdist` | Read-only or `tmp_path`-isolated documentation consistency, manifest, and workflow policy tests. |
| `SECURITY_SERIAL` | `serial` | CPU-intensive cryptographic stress tests (`tests/identity/test_crypto_stress.py`) or multi-track adversarial suites (`tests/adversarial/test_adversarial_phase2.py`). |
| `UNKNOWN_SERIAL` | `serial` | Default fail-safe serial classification for historical stub modules or unclassified test modules. |

---

## 3. Canonical Registered Pytest Markers (12 Markers + `allow_inconsistency`)

All markers below are registered in `pyproject.toml` (`[tool.pytest.ini_options].markers`) and `src/fc27_ci_test_groups.py`:

| Marker | Purpose |
| :--- | :--- |
| `unit` | Pure in-memory unit tests without database, Docker, or external network access. |
| `domain` | Offline FC27 domain, analytics, contract, and workflow logic tests. |
| `postgres` | Tests requiring a disposable PostgreSQL 16 test instance. |
| `docker` | Tests inspecting or invoking Docker CLI or container build contracts. |
| `compose` | Tests validating Docker Compose profile isolation, config, and service boundaries. |
| `security` | Adversarial, authentication, authorization, privacy, and boundary security tests. |
| `documentation` | Documentation consistency, archive manifest, and repository hygiene tests. |
| `performance` | Performance telemetry, benchmark, and latency threshold tests. |
| `serial` | Tests that must execute serially due to shared resources, concurrency races, or cluster state. |
| `slow` | Longer-running integration, stress, or multi-database migration tests. |
| `offline` | Tests verified to execute completely offline without external network access. |
| `network_forbidden` | Tests enforcing strict zero-network and zero-EA-DNS resolution guards. |
| `allow_inconsistency` | Allow temporary inconsistency in `fc27_approval_inconsistencies` view during negative state tests. |

---

## 4. Maintained Test Groups and Full CI Shards Summary

| Maintained Group | Full CI Shard | Module Count | Execution Strategy |
| :--- | :--- | :--- | :--- |
| `quality` | `full-quality` | `8` | Static gates + `pytest` quality/manifest/workflow/report-sanitization policy modules |
| `unit_domain_parallel` | `full-unit-domain` | `48` | `pytest -n auto --dist loadfile` (`PARALLEL_RESULT_EQUIVALENCE=PASS`, `0` flakes across 5 runs) |
| `unit_domain_serial` | `full-unit-domain` | `7` | Serial `pytest` execution after `unit_domain_parallel` |
| `postgres_parallel_safe` | `full-postgres` | `51` | `pytest -n 4 --dist loadfile` using worker-isolated `fc27_tmpl_<worker>_<uuid>` template DB cloning |
| `postgres_serial` | `full-postgres` | `15` | Serial `pytest` execution after `postgres_parallel_safe` (cluster role tests, multi-thread race harnesses, backup/restore, performance) |
| `compose_serial` | `full-compose` | `1` | Isolated `COMPOSE_PROJECT_NAME` profile validation + `tests/test_compose_isolation.py` |
| **Total** | **All 4 Shards** | **130** | **100% of repository test collection** |

---

## 5. Complete Module Classification Matrix (130 Modules)

| Test Module | Classification | Markers | Maintained Group | Full CI Shard | Fast Workflow Groups | Mode |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `tests/test_archive_integrity.py` | `DOCUMENTATION_PARALLEL_SAFE` | `documentation, offline` | `quality` | `full-quality` | `quality` | `parallel-xdist` |
| `tests/test_ci_report_sanitization.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, security, offline, network_forbidden` | `quality` | `full-quality` | `quality` | `parallel-xdist` |
| `tests/test_documentation_consistency.py` | `DOCUMENTATION_PARALLEL_SAFE` | `documentation, offline, network_forbidden` | `quality` | `full-quality` | `quality` | `parallel-xdist` |
| `tests/test_fc27_archive_manifest.py` | `DOCUMENTATION_PARALLEL_SAFE` | `documentation, offline` | `quality` | `full-quality` | `quality` | `parallel-xdist` |
| `tests/test_fc27_repository_cleanup.py` | `DOCUMENTATION_PARALLEL_SAFE` | `documentation, offline` | `quality` | `full-quality` | `quality` | `parallel-xdist` |
| `tests/test_fc27_safe_adapter_bundle_manifest.py` | `DOCUMENTATION_PARALLEL_SAFE` | `documentation, security, offline, network_forbidden` | `quality` | `full-quality` | `quality` | `parallel-xdist` |
| `tests/test_github_workflows.py` | `DOCUMENTATION_PARALLEL_SAFE` | `documentation, security, offline, network_forbidden` | `quality` | `full-quality` | `quality` | `parallel-xdist` |
| `tests/test_packaging.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, documentation, offline` | `quality` | `full-quality` | `quality` | `parallel-xdist` |
| `tests/test_compose_isolation.py` | `DOCKER_SERIAL` | `docker, compose, security, serial, offline` | `compose_serial` | `full-compose` | `docker-compose-focused` | `serial` |
| `tests/adversarial/test_r4_adversarial_empirical_challenge.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, security, offline` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/identity/test_adversarial_m2_1.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, security, offline` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/identity/test_adversarial_m4_1.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, security, offline` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/identity/test_adversarial_r2_2.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, security, offline` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/identity/test_bootstrap.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, security, offline` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/identity/test_crypto.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, security, offline` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/identity/test_policy.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, security, offline` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/identity/test_r12_model_validation.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, security, offline` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/identity/test_r3_atomic_decision_commit.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, security, offline` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/identity/test_r3_evidence_binding.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, security, offline` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/identity/test_r4_player_decision_proofs.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, security, offline` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/identity/test_r5_legacy_tokens.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, security, offline` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/identity/test_recovery.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, security, offline` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/identity/test_services.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, security, offline` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/identity/test_totp.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, security, offline` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/identity/test_webauthn.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, security, offline` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_end_to_end.py` | `OFFLINE_DOMAIN_PARALLEL_SAFE` | `unit, domain, offline` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_fc27_advisory_cache.py` | `POSTGRES_SERIAL` | `postgres, domain, performance, serial, offline, network_forbidden` | `postgres_serial` | `full-postgres` | `postgres-focused` | `serial` |
| `tests/test_fc27_analyst.py` | `OFFLINE_DOMAIN_PARALLEL_SAFE` | `unit, domain, offline, network_forbidden` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_fc27_api_cache.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, domain, offline, network_forbidden` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_fc27_api_rate_limit.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, domain, offline, network_forbidden` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_fc27_authorized_api.py` | `OFFLINE_DOMAIN_PARALLEL_SAFE` | `unit, domain, security, offline, network_forbidden` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_fc27_authorized_provider.py` | `OFFLINE_DOMAIN_PARALLEL_SAFE` | `unit, domain, security, offline, network_forbidden` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_fc27_clubs_api_contracts.py` | `OFFLINE_DOMAIN_PARALLEL_SAFE` | `unit, domain, offline, network_forbidden` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_fc27_clubs_compatibility.py` | `OFFLINE_DOMAIN_PARALLEL_SAFE` | `unit, domain, offline, network_forbidden` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_fc27_coach.py` | `OFFLINE_DOMAIN_PARALLEL_SAFE` | `unit, domain, offline, network_forbidden` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_fc27_director.py` | `OFFLINE_DOMAIN_PARALLEL_SAFE` | `unit, domain, offline, network_forbidden` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_fc27_operator_capture.py` | `OFFLINE_DOMAIN_PARALLEL_SAFE` | `unit, domain, security, offline, network_forbidden` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_fc27_pandas_view.py` | `OFFLINE_DOMAIN_PARALLEL_SAFE` | `unit, domain, offline, network_forbidden` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_fc27_platform.py` | `OFFLINE_DOMAIN_PARALLEL_SAFE` | `unit, domain, security, offline, network_forbidden` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_fc27_scout.py` | `OFFLINE_DOMAIN_PARALLEL_SAFE` | `unit, domain, offline, network_forbidden` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_fc27_search.py` | `OFFLINE_DOMAIN_PARALLEL_SAFE` | `unit, domain, offline, network_forbidden` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_fc27_tactics.py` | `OFFLINE_DOMAIN_PARALLEL_SAFE` | `unit, domain, offline, network_forbidden` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_fc27_user_export.py` | `OFFLINE_DOMAIN_PARALLEL_SAFE` | `unit, domain, security, offline, network_forbidden` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_identifiers_and_tts.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, offline` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_installer_static.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, offline` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_integrated_services.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, offline` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_m1_phase2_adversarial.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, security, offline` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_m2_phase2_adversarial.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, security, offline` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_m4_phase2_adversarial.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, security, offline` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_mcp_http_server.py` | `OFFLINE_DOMAIN_PARALLEL_SAFE` | `unit, domain, offline` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_mcp_retry_and_tools.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, domain, offline` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_model_selection.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, domain, offline` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_ocr_parsing.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, domain, offline` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_phase1_scope.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, security, offline` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_roles.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, domain, offline` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_rundown_match_and_prompt.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, domain, offline` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_typed_graph_voice.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, domain, offline` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_voice_gateway_api.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, domain, offline` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_watcher_processing.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, domain, offline` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_workflow.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, domain, offline` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_ws_server.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, domain, offline` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/identity/test_crypto_stress.py` | `SECURITY_SERIAL` | `unit, security, serial, slow, offline` | `unit_domain_serial` | `full-unit-domain` | `unit-domain` | `serial` |
| `tests/test_adversarial_m2_2.py` | `UNKNOWN_SERIAL` | `security, serial, offline` | `unit_domain_serial` | `full-unit-domain` | `unit-domain` | `serial` |
| `tests/test_adversarial_m2_it2_2.py` | `UNKNOWN_SERIAL` | `security, serial, offline` | `unit_domain_serial` | `full-unit-domain` | `unit-domain` | `serial` |
| `tests/test_dispatch_transition_probe.py` | `UNKNOWN_SERIAL` | `security, serial, offline` | `unit_domain_serial` | `full-unit-domain` | `unit-domain` | `serial` |
| `tests/test_m2_adversarial_challenger.py` | `UNKNOWN_SERIAL` | `security, serial, offline` | `unit_domain_serial` | `full-unit-domain` | `unit-domain` | `serial` |
| `tests/test_swarms_backend.py` | `FILESYSTEM_SERIAL` | `unit, domain, serial, offline` | `unit_domain_serial` | `full-unit-domain` | `unit-domain` | `serial` |
| `tests/test_visual_studio_project.py` | `FILESYSTEM_SERIAL` | `unit, domain, serial, offline` | `unit_domain_serial` | `full-unit-domain` | `unit-domain` | `serial` |
| `tests/adversarial/test_r3_r4_empirical_challenger.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, security, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/adversarial/test_r6_empirical_security_boundary_challenges.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, security, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/dispatch/test_outbox_relay.py` | `POSTGRES_SERIAL` | `postgres, domain, serial, offline` | `postgres_serial` | `full-postgres` | — | `serial` |
| `tests/dispatch/test_worker.py` | `POSTGRES_SERIAL` | `postgres, domain, serial, offline` | `postgres_serial` | `full-postgres` | — | `serial` |
| `tests/e2e/test_phase2_tier1_features.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, domain, security, slow, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/e2e/test_phase2_tier2_boundaries.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, security, slow, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/e2e/test_phase2_tier3_combinations.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, domain, security, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/e2e/test_phase2_tier4_scenarios.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, domain, security, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/e2e/test_tier1_features.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, domain, slow, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/e2e/test_tier2_boundaries.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, security, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/e2e/test_tier3_combinations.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, domain, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/e2e/test_tier4_scenarios.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, domain, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/identity/test_adversarial_m3_2.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, security, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/identity/test_adversarial_r1_2.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, security, slow, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/identity/test_bootstrap_ceremony.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, security, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/identity/test_bootstrap_secret_fail_closed.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, security, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/identity/test_decision_proof_discriminated_union.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, security, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/identity/test_delegations.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, security, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/identity/test_player.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, security, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/identity/test_r5_negative_matrix.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, security, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/identity/test_routers_delegations.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, security, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/identity/test_routers_invitations.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, security, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/identity/test_routers_player_verification.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, security, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/identity/test_routers_sessions.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, security, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/identity/test_routers_users.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, security, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/identity/test_session_lifecycle_adversarial.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, security, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/identity/test_step_up.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, security, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/identity/test_store.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, security, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/identity/test_store_totp_atomic.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, security, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/identity/test_tokens.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, security, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/test_adversarial_m1.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, security, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/test_adversarial_m3_challenger.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, security, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/test_approval_lifecycle.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, domain, security, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/test_approval_security.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, security, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/test_commissioner_cli.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, domain, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/test_dispatch_creation.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, domain, security, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/test_dispatch_schema.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, domain, security, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/test_dispatch_transition.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, domain, security, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/test_events_outbox.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, domain, security, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/test_fc27_advisory_pipeline_e2e.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, domain, offline, network_forbidden` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/test_fc27_analyst_graph.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, domain, offline, network_forbidden` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/test_fc27_api_ingestion.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, domain, offline, network_forbidden` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/test_fc27_community_provider.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, docker, compose, domain, security, offline, network_forbidden` | `postgres_parallel_safe` | `full-postgres` | `postgres-focused, docker-compose-focused` | `parallel-xdist` |
| `tests/test_fc27_discord_bot.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, docker, compose, domain, security, offline, network_forbidden` | `postgres_parallel_safe` | `full-postgres` | `postgres-focused, docker-compose-focused` | `parallel-xdist` |
| `tests/test_fc27_match_results.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, domain, security, offline, network_forbidden` | `postgres_parallel_safe` | `full-postgres` | `postgres-focused` | `parallel-xdist` |
| `tests/test_fc27_polling_worker.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, docker, compose, domain, security, offline, network_forbidden` | `postgres_parallel_safe` | `full-postgres` | `postgres-focused, docker-compose-focused` | `parallel-xdist` |
| `tests/test_fc27_preapproval_validator.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, domain, security, offline, network_forbidden` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/test_fc27_search_scout_graph.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, domain, offline, network_forbidden` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/test_graph_api_v1.py` | `POSTGRES_SERIAL` | `postgres, domain, security, serial, slow, offline` | `postgres_serial` | `full-postgres` | — | `serial` |
| `tests/test_graph_persistence.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, domain, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/test_langgraph_flow.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, domain, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/test_m1_adversarial_challenger.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, security, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/test_migration_0003.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, domain, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/test_tracing.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, domain, offline` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/adversarial/test_adversarial_phase2.py` | `SECURITY_SERIAL` | `postgres, security, serial, slow, offline` | `postgres_serial` | `full-postgres` | — | `serial` |
| `tests/adversarial/test_r3_it3_stress_harness.py` | `POSTGRES_SERIAL` | `postgres, security, serial, slow, offline` | `postgres_serial` | `full-postgres` | — | `serial` |
| `tests/adversarial/test_r4_empirical_concurrency_race.py` | `POSTGRES_SERIAL` | `postgres, security, serial, offline` | `postgres_serial` | `full-postgres` | — | `serial` |
| `tests/adversarial/test_r5_it2_adversarial_empirical_challenge.py` | `POSTGRES_SERIAL` | `postgres, security, serial, slow, offline` | `postgres_serial` | `full-postgres` | — | `serial` |
| `tests/identity/test_r4_concurrency_race.py` | `POSTGRES_SERIAL` | `postgres, security, serial, offline` | `postgres_serial` | `full-postgres` | — | `serial` |
| `tests/identity/test_r4_player_concurrency.py` | `POSTGRES_SERIAL` | `postgres, security, serial, offline` | `postgres_serial` | `full-postgres` | — | `serial` |
| `tests/test_db_migrate_roles.py` | `POSTGRES_SERIAL` | `postgres, security, serial, offline` | `postgres_serial` | `full-postgres` | `postgres-focused` | `serial` |
| `tests/test_db_roles.py` | `POSTGRES_SERIAL` | `postgres, security, serial, offline` | `postgres_serial` | `full-postgres` | `postgres-focused` | `serial` |
| `tests/test_fc27_backup_restore.py` | `POSTGRES_SERIAL` | `postgres, domain, serial, slow, offline, network_forbidden` | `postgres_serial` | `full-postgres` | `postgres-focused` | `serial` |
| `tests/test_fc27_dashboard_queries.py` | `POSTGRES_SERIAL` | `postgres, domain, serial, offline, network_forbidden` | `postgres_serial` | `full-postgres` | `postgres-focused` | `serial` |
| `tests/test_fc27_demo.py` | `FIXED_PORT_SERIAL` | `postgres, domain, serial, offline, network_forbidden` | `postgres_serial` | `full-postgres` | — | `serial` |
| `tests/test_fc27_performance.py` | `PERFORMANCE_SERIAL` | `postgres, performance, serial, slow, offline, network_forbidden` | `postgres_serial` | `full-postgres` | — | `serial` |
| `tests/test_fc27_persistence_and_queries.py` | `POSTGRES_SERIAL` | `postgres, domain, security, serial, slow, offline, network_forbidden` | `postgres_serial` | `full-postgres` | `postgres-focused` | `serial` |
