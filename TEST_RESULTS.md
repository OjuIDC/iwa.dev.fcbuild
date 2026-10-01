# Test results (2026-10-01)

## Environment
- Python 3.13.15 (development venv) and **Python 3.11.16** (fresh `uv` venv matching the `python:3.11.16-slim` image runtime).
- PostgreSQL 16.15, a real server: local cluster, UTF-8, `TEST_POSTGRES_URI=postgresql://fc27test@127.0.0.1:55432/postgres`.
  - Every transaction, lock, constraint, trigger and concurrency test runs against it, with one fresh migrated database per test.
- Key packages: langgraph 1.2.12, langgraph-checkpoint-postgres 3.1.2, swarms 15.0.3, langfuse 4.16.0, fastapi 0.142.2, pydantic 2.13.5, psycopg 3.3.6, websockets 16.1.1, espeak-ng (real binary), tesseract (real binary).
- A fresh `pip install -r requirements-dev.txt -r requirements-swarms.txt -r requirements-langfuse.txt` followed by `pip check` reported **No broken requirements found**.
- docker CLI v29.8.2 with Compose v5.5.1. **No Docker daemon was available**: no image was built and no container was run.

## Commands and results
| Command | Result |
|---|---|
| `python -m compileall -q src tests host_agent voice` | exit 0 (3.13 and 3.11) |
| `python -m pytest` (3.13) | **254 passed, 0 skipped, 0 failed** |
| `python -m pytest` (3.11) | **254 passed, 0 skipped, 0 failed** |
| `docker compose --env-file .env config --quiet` | pass |
| `… -f docker-compose.yml -f docker-compose.cpu.yml config --quiet` | pass |
| `… -f docker-compose.yml -f docker-compose.gpu.yml config --quiet` | pass |
| `… --profile voice config --quiet` | pass |
| `… --profile rtsp config --quiet` | pass |
| `… -f docker-compose.yml -f docker-compose.langfuse.yml --profile langfuse config --quiet` | pass |
| `… --profile voice -f docker-compose.yml -f docker-compose.gpu.yml -f docker-compose.voice-gpu.yml config --quiet` | pass |
| Linting / type checking | None is configured in the repository (no ruff/flake8/mypy config), so none was run |

The Compose commands used a temporary validation-only env file outside the repository, built from `config.example.env` with placeholder values. It was not committed.

## Baseline (before changes)
- 26 tests passed with `python -m pytest`. `pytest -q`, the command the README gave, failed with `ModuleNotFoundError: src`.
- None of those tests exercised API authorization, races, MCP retries or state transitions.

## Tests by file
| File | Tests | Covers |
|---|---|---|
| test_approval_security.py | 30 | <ul><li>Roles and 401/403</li><li>Immutable runs, DB trigger</li><li>Nonces: single-use, thread/candidate binding, expiry, replay</li><li>Signature tampering, stale package</li><li>8-way approve race, approve/reject race</li><li>Audit atomicity, audit-failure rollback</li><li>Sequence concurrency</li></ul> |
| test_langgraph_flow.py | 12 | <ul><li>Interrupt, resume, bypass attempt, terminal re-resume</li><li>Checkpoint metadata, startup reconciliation</li><li>Ranking fixture, invalid role output</li><li>No duplicates, empty package</li><li>Search failure, audit_query</li></ul> |
| test_mcp_retry_and_tools.py / test_mcp_http_server.py | 57 / 10 | <ul><li>429 Retry-After, bounded retry, 403 fail-closed, 5xx</li><li>Non-retryable 4xx, cache, rate limit, ASCII club ids</li><li>Auth, 413, 503 saturation, SSE</li></ul> |
| test_ws_server.py | 15 | <ul><li>Missing auth, untrusted Origin</li><li>Subprotocol auth, broadcast</li><li>Slow/failing clients, limits</li></ul> |
| test_watcher_processing.py / test_ocr_parsing.py / test_rundown_match_and_prompt.py | 9 / 19 / 9 | <ul><li>Watcher idempotency and retry</li><li>67:32 parsing, debounce, kickoff reset, orientation (real tesseract run included)</li><li>Rundown match type, tackles, prompt injection</li></ul> |
| test_compose_isolation.py | 17 | <ul><li>Secret placement, Postgres network isolation, loopback ports</li><li>Digest pins, fail-closed secrets</li><li>Langfuse overlay</li></ul> |
| test_identifiers_and_tts.py | 19 | <ul><li>Unicode/full-width/sign/space club ids rejected</li><li>TTS `--` (real espeak-ng)</li></ul> |
| test_roles.py / test_swarms_backend.py / test_model_selection.py | 5 / 6 / 7 | <ul><li>Schema forbids approval fields</li><li>Swarms adapter, telemetry off, logging not hijacked</li><li>LM Studio disabled/invalid</li></ul> |
| test_tracing.py | 6 | <ul><li>Disabled default, fail-closed config</li><li>Trace correlation per thread, metadata, redaction (real Langfuse SDK, in-memory exporter)</li></ul> |
| others | 33 | <ul><li>Commissioner CLI end to end</li><li>Packaging, installer static checks</li><li>Voice gateway, workflow signing</li></ul> |

All 40 test names required by the task exist and pass.

## Meaningfulness checks (mutation)
- Removing signature verification from `approval_store.apply_decision` → `test_modified_signed_payload_is_rejected` fails.
- Disabling the role check in `security.require_role` → 2 tests fail.
- The MCP agent reports that removing auth, or loosening the 403 limit, makes the matching tests fail.

## Not validated
- Docker image builds, container start-up, healthchecks and inter-container networking: no Docker daemon was available.
- Live EA API behaviour, live Ollama/LM Studio inference, GPU allocation, the Swarms backend against a real model server, and a running Langfuse server.
- `host_agent/install.ps1` execution: no PowerShell was available. Only static checks ran.
