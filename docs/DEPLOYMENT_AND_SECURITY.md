# Deployment and security hardening

This page describes the implemented stack. The approval model is covered in [APPROVAL_SECURITY.md](APPROVAL_SECURITY.md)
and the full change list is in `../SECURITY_CHANGES.md`.

## 1. Host preparation
1. Update the OS, Docker Engine/Desktop, the Compose v2 plugin, the GPU driver and the container runtime.
2. For NVIDIA GPUs, install the NVIDIA Container Toolkit and validate it with `docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi`.
3. Keep the host firewall enabled. Every host-published port binds to `127.0.0.1` (a test enforces this).

## 2. Secrets
1. Copy `config.example.env` to `.env` and replace every `replace_with_*` value with an independently generated random value.
2. Store the Commissioner token in a password manager. Put only its SHA-256 in `.env`: `python scripts/commissioner_decide.py hash-token`.
3. In this lab deployment, secrets reach containers as environment variables. `src/settings.read_secret` also accepts `NAME_FILE`, so you can move to Docker/Compose secrets or a vault without code changes.
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
docker compose --env-file .env -f docker-compose.yml -f docker-compose.gpu.yml up -d --build   # or docker-compose.cpu.yml
docker compose --env-file .env ps
curl -fsS http://127.0.0.1:8001/health
```
graph-api applies the SQL migrations (`migrations/`) and the LangGraph checkpoint schema at startup.
To disable this, set `GRAPH_RUN_MIGRATIONS=false` and run `python -m src.db_migrate` yourself.

## 5. GPU sizing
Start with `OLLAMA_MAX_LOADED_MODELS=2` and `OLLAMA_NUM_PARALLEL=2`. If you see out-of-memory errors or model eviction, reduce both to 1.

## 6. Network hardening
- Ollama, MCP and PostgreSQL publish no host ports.
- Redis is published on `127.0.0.1:6379` for the Windows capture agent only.
- Put any remote access behind a VPN or a TLS reverse proxy with authentication. Do not change the bindings to `0.0.0.0`.
- MCP is the only component that calls `proclubs.ea.com`.

## 7. Container hardening
- Application containers run with `no-new-privileges`, `cap_drop: ALL`, an init process and non-root users.
- Base images are pinned by tag and digest. The digests were resolved from the registries on 2026-10-01; re-verify them with `docker buildx imagetools inspect <image>`.
- Never mount the Docker socket into any container.

## 8. Backups
The critical state is in PostgreSQL: `graph_threads`, `approval_audit`, `approval_nonces` and the LangGraph `checkpoint*` tables.
Back them up regularly. Model weights can be pulled again.
