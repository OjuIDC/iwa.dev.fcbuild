# FC27 Test Isolation Matrix and Pytest Marker Reference

## 1. Overview and Authority

This document is the authoritative test-isolation classification matrix for all `38` test modules in `iwa.dev.fcbuild`. It is backed in code by `src/fc27_ci_test_groups.py`, enforced dynamically during pytest collection by `tests/conftest.py` (`pytest_collection_modifyitems`), and verified in CI by `tests/test_github_workflows.py` (`test_collection_consistency_and_shard_disjointness`).

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
| `FILESYSTEM_SERIAL` | `serial` | Tests manipulating working directories or process environment for subprocesses, plus the Visual Studio MSBuild integration that writes ignored intermediates (`tests/test_visual_studio_project.py`). |
| `PERFORMANCE_SERIAL` | `serial` | Performance telemetry, latency benchmark, and `EXPLAIN ANALYZE` suites (`tests/test_fc27_performance.py`). |
| `DOCUMENTATION_PARALLEL_SAFE` | `parallel-xdist` | Read-only or `tmp_path`-isolated documentation consistency, manifest, and workflow policy tests. |
| `SECURITY_SERIAL` | `serial` | CPU-intensive cryptographic or multi-track security suites. |
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
| `quality` | `full-quality` | `7` | Static gates + `pytest` quality/manifest/workflow/report-sanitization policy modules |
| `unit_domain_parallel` | `full-unit-domain` | `15` | `pytest -n auto --dist loadfile` (`PARALLEL_RESULT_EQUIVALENCE=PASS`, `0` flakes across 5 runs) |
| `unit_domain_serial` | `full-unit-domain` | `1` | Serial `pytest` execution after `unit_domain_parallel` |
| `postgres_parallel_safe` | `full-postgres` | `7` | `pytest -n 4 --dist loadfile` using worker-isolated `fc27_tmpl_<worker>_<uuid>` template DB cloning |
| `postgres_serial` | `full-postgres` | `7` | Serial `pytest` execution after `postgres_parallel_safe` (cluster role tests, backup/restore, dashboard, performance) |
| `compose_serial` | `full-compose` | `1` | Isolated `COMPOSE_PROJECT_NAME` profile validation + `tests/test_compose_isolation.py` |
| **Total** | **All 4 Shards** | **38** | **100% of repository test collection** |

---

## 5. Complete Module Classification Matrix (38 Modules)

| Test Module | Classification | Markers | Maintained Group | Full CI Shard | Fast Workflow Groups | Mode |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `tests/test_archive_integrity.py` | `DOCUMENTATION_PARALLEL_SAFE` | `documentation, offline` | `quality` | `full-quality` | `quality` | `parallel-xdist` |
| `tests/test_ci_report_sanitization.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, security, offline, network_forbidden` | `quality` | `full-quality` | `quality` | `parallel-xdist` |
| `tests/test_documentation_consistency.py` | `DOCUMENTATION_PARALLEL_SAFE` | `documentation, offline, network_forbidden` | `quality` | `full-quality` | `quality` | `parallel-xdist` |
| `tests/test_fc27_archive_manifest.py` | `DOCUMENTATION_PARALLEL_SAFE` | `documentation, offline` | `quality` | `full-quality` | `quality` | `parallel-xdist` |
| `tests/test_fc27_repository_cleanup.py` | `DOCUMENTATION_PARALLEL_SAFE` | `documentation, offline` | `quality` | `full-quality` | `quality` | `parallel-xdist` |
| `tests/test_github_workflows.py` | `DOCUMENTATION_PARALLEL_SAFE` | `documentation, security, offline, network_forbidden` | `quality` | `full-quality` | `quality` | `parallel-xdist` |
| `tests/test_packaging.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, documentation, offline` | `quality` | `full-quality` | `quality` | `parallel-xdist` |
| `tests/test_compose_isolation.py` | `DOCKER_SERIAL` | `docker, compose, security, serial, offline` | `compose_serial` | `full-compose` | `docker-compose-focused` | `serial` |
| `tests/test_fc27_advisory_cache.py` | `POSTGRES_SERIAL` | `postgres, domain, performance, serial, offline, network_forbidden` | `postgres_serial` | `full-postgres` | `postgres-focused` | `serial` |
| `tests/test_fc27_analyst.py` | `OFFLINE_DOMAIN_PARALLEL_SAFE` | `unit, domain, offline, network_forbidden` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_fc27_api_cache.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, domain, offline, network_forbidden` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_fc27_api_rate_limit.py` | `PURE_UNIT_PARALLEL_SAFE` | `unit, domain, offline, network_forbidden` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_fc27_clubs_api_contracts.py` | `OFFLINE_DOMAIN_PARALLEL_SAFE` | `unit, domain, offline, network_forbidden` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_fc27_coach.py` | `OFFLINE_DOMAIN_PARALLEL_SAFE` | `unit, domain, offline, network_forbidden` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_fc27_director.py` | `OFFLINE_DOMAIN_PARALLEL_SAFE` | `unit, domain, offline, network_forbidden` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_fc27_operator_capture.py` | `OFFLINE_DOMAIN_PARALLEL_SAFE` | `unit, domain, security, offline, network_forbidden` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_fc27_pandas_view.py` | `OFFLINE_DOMAIN_PARALLEL_SAFE` | `unit, domain, offline, network_forbidden` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_fc27_platform.py` | `OFFLINE_DOMAIN_PARALLEL_SAFE` | `unit, domain, security, offline, network_forbidden` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_fc27_scout.py` | `OFFLINE_DOMAIN_PARALLEL_SAFE` | `unit, domain, offline, network_forbidden` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_fc27_search.py` | `OFFLINE_DOMAIN_PARALLEL_SAFE` | `unit, domain, offline, network_forbidden` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_fc27_tactics.py` | `OFFLINE_DOMAIN_PARALLEL_SAFE` | `unit, domain, offline, network_forbidden` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_fc27_user_export.py` | `OFFLINE_DOMAIN_PARALLEL_SAFE` | `unit, domain, security, offline, network_forbidden` | `unit_domain_parallel` | `full-unit-domain` | `unit-domain` | `parallel-xdist` |
| `tests/test_visual_studio_project.py` | `FILESYSTEM_SERIAL` | `unit, domain, serial, offline` | `unit_domain_serial` | `full-unit-domain` | `unit-domain` | `serial` |
| `tests/test_fc27_advisory_pipeline_e2e.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, domain, offline, network_forbidden` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/test_fc27_analyst_graph.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, domain, offline, network_forbidden` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/test_fc27_community_provider.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, docker, compose, domain, security, offline, network_forbidden` | `postgres_parallel_safe` | `full-postgres` | `postgres-focused, docker-compose-focused` | `parallel-xdist` |
| `tests/test_fc27_discord_bot.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, docker, compose, domain, security, offline, network_forbidden` | `postgres_parallel_safe` | `full-postgres` | `postgres-focused, docker-compose-focused` | `parallel-xdist` |
| `tests/test_fc27_match_results.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, domain, security, offline, network_forbidden` | `postgres_parallel_safe` | `full-postgres` | `postgres-focused` | `parallel-xdist` |
| `tests/test_fc27_polling_worker.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, docker, compose, domain, security, offline, network_forbidden` | `postgres_parallel_safe` | `full-postgres` | `postgres-focused, docker-compose-focused` | `parallel-xdist` |
| `tests/test_fc27_search_scout_graph.py` | `POSTGRES_PARALLEL_SAFE` | `postgres, domain, offline, network_forbidden` | `postgres_parallel_safe` | `full-postgres` | — | `parallel-xdist` |
| `tests/test_db_migrate_roles.py` | `POSTGRES_SERIAL` | `postgres, security, serial, offline` | `postgres_serial` | `full-postgres` | `postgres-focused` | `serial` |
| `tests/test_db_roles.py` | `POSTGRES_SERIAL` | `postgres, security, serial, offline` | `postgres_serial` | `full-postgres` | `postgres-focused` | `serial` |
| `tests/test_fc27_backup_restore.py` | `POSTGRES_SERIAL` | `postgres, domain, serial, slow, offline, network_forbidden` | `postgres_serial` | `full-postgres` | `postgres-focused` | `serial` |
| `tests/test_fc27_dashboard_queries.py` | `POSTGRES_SERIAL` | `postgres, domain, serial, offline, network_forbidden` | `postgres_serial` | `full-postgres` | `postgres-focused` | `serial` |
| `tests/test_fc27_demo.py` | `FIXED_PORT_SERIAL` | `postgres, domain, serial, offline, network_forbidden` | `postgres_serial` | `full-postgres` | — | `serial` |
| `tests/test_fc27_performance.py` | `PERFORMANCE_SERIAL` | `postgres, performance, serial, slow, offline, network_forbidden` | `postgres_serial` | `full-postgres` | — | `serial` |
| `tests/test_fc27_persistence_and_queries.py` | `POSTGRES_SERIAL` | `postgres, domain, security, serial, slow, offline, network_forbidden` | `postgres_serial` | `full-postgres` | `postgres-focused` | `serial` |
