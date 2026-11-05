# Deployment and security hardening

> **Reconciliation Note**: For the current authoritative runtime operations runbook, least-privilege role bootstrap (`fc27_owner`, `fc27_migrator`, `fc27_app`, `fc27_dispatch`, `fc27_relay`), and security invariants, see [`OPERATIONS.md`](OPERATIONS.md) and [`SECURITY_BOUNDARIES.md`](SECURITY_BOUNDARIES.md). The approval model is covered in [`FC27_APPROVAL_FLOW.md`](FC27_APPROVAL_FLOW.md) and [`APPROVAL_SECURITY.md`](APPROVAL_SECURITY.md). Live FC 27 provider calls are disabled by default (`FC27_PROVIDER_ENABLED=false`; see [`FC27_PROVIDER_INTEGRATION.md`](FC27_PROVIDER_INTEGRATION.md)).

## 1. Host preparation
1. Update the OS, Docker Engine/Desktop, the Compose v2 plugin, the GPU driver and the container runtime.
2. For NVIDIA GPUs, install the NVIDIA Container Toolkit and validate it with `docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi`.
3. Keep the host firewall enabled. Every host-published port binds to `127.0.0.1` (a test enforces this).

## 2. Secrets
1. Copy `config.example.env` to `.env` and replace every `replace_with_*` value with an independently generated random value.
2. Store the Commissioner token in a password manager. Put only its SHA-256 in `.env`: `python scripts/commissioner_decide.py hash-token`.
3. In this lab deployment, secrets reach containers as environment variables. `src/settings.py` (`read_secret`) also accepts `NAME_FILE`, so you can move to Docker/Compose secrets or a vault without code changes.
4. Compose fails closed: `docker compose config` errors if any required secret is missing.

## 3. Credential placement (enforced by `tests/test_compose_isolation.py`)

| Credential | Held by |
|---|---|
| `POSTGRES_URI`, `GRAPH_COMMISSIONER_TOKEN_SHA256`, `APPROVAL_SIGNING_SECRET` | graph-api only |
| Commissioner token (plaintext), `APPROVAL_SIGNING_SECRET` | the Commissioner's workstation (`scripts/commissioner_decide.py`) |
| `GRAPH_SERVICE_TOKEN` | graph-api (to verify it), live-watcher, voice-gateway |
| `MCP_API_TOKEN` | fc27-mcp, graph-api, live-watcher, post-match-rundown |
| `WS_AUTH_TOKEN` | ws-server and its clients |
| `VOICE_API_TOKEN` | voice-gateway and its clients |

PostgreSQL sits on the internal `fc27_data` network, which only graph-api joins.

## 4. Start
```bash
cp config.example.env .env   # then fill in every value
docker compose --env-file .env config --quiet
docker compose --env-file .env up -d --build   # uses the remote Ollama at OLLAMA_BASE_URL (araba)
# Bundled Ollama instead (set OLLAMA_BASE_URL=http://ollama:11434 first):
# docker compose --env-file .env -f docker-compose.yml -f docker-compose.gpu.yml --profile local-ollama up -d --build   # or docker-compose.cpu.yml
docker compose --env-file .env ps
curl -fsS http://127.0.0.1:8001/health
```
graph-api applies the SQL migrations (`migrations/`) and the LangGraph checkpoint schema at startup.
To disable this, set `GRAPH_RUN_MIGRATIONS=false` and run `python -m src.db_migrate` yourself.

## 5. GPU sizing
The default remote server (araba, RTX 3080 10 GB) is sized and tuned by its owner; its `OLLAMA_*` settings are not controlled from this repository. For the bundled container (`--profile local-ollama`), start with `OLLAMA_MAX_LOADED_MODELS=2` and `OLLAMA_NUM_PARALLEL=2`. If you see out-of-memory errors or model eviction, reduce both to 1.

## 6. Network hardening
- MCP and PostgreSQL publish no host ports; the bundled Ollama (`--profile local-ollama`) publishes none either. The default remote Ollama server is reached outbound over plain HTTP on the lab network and has no authentication; it relies on its host firewall, so run the stack only on an admitted client.
- Redis is published on `127.0.0.1:6379` for the Windows capture agent only.
- Put any remote access behind a VPN or a TLS reverse proxy with authentication. Do not change the bindings to `0.0.0.0`.
- Live calls to `proclubs.ea.com` are disabled and blocked by default (`FC27_PROVIDER_ENABLED=false`, `FC27_LIVE_INGESTION_ENABLED=false`); only the provider-neutral boundary (`src/fc27_authorized_transport.py`) may contact an approved provider after formal written authorization is configured.

## 7. Container hardening
- Application containers run with `no-new-privileges`, `cap_drop: ALL`, an init process and non-root users.
- Base images are pinned by tag and digest. The digests were resolved from the registries on 2026-10-01; re-verify them with `docker buildx imagetools inspect <image>`.
- Never mount the Docker socket into any container.

## 8. Backups
The critical state is in PostgreSQL: `graph_threads`, `approval_audit`, `approval_nonces` and the LangGraph `checkpoint*` tables.
Back them up regularly. Model weights can be pulled again.
