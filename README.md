# FC27 Full Integrated Stack

Local-first football intelligence and multi-agent recommendation platform for East Coasters FC. The stack uses Docker-hosted Ollama by default, supports LM Studio as an optional fallback, processes API and capture events through Redis, stores run and approval state in PostgreSQL, and requires explicit Commissioner approval before a recommendation is treated as approved.

## Production status

The repository is deployment-capable for a controlled lab or secured single-host environment. It is not a turnkey public internet service. Before external exposure, place the Graph API and WebSocket endpoint behind an authenticated TLS reverse proxy, pin container images by digest, move secrets to a secret manager, and replace the shared Graph API token with organization identity or mTLS.

## Architecture

```text
EA FC API -> fc27-mcp -> live-watcher -> graph-api -> local role models
                              |              |
                              v              v
                            Redis       PostgreSQL audit
                              |
            post-match rundown + WebSocket broadcast

Ollama is primary. LM Studio is optional fallback.
No agent can approve its own recommendation.
```

## Repository map

```text
Dockerfile.mcp                 MCP proxy image
Dockerfile.graph               Graph, watcher, WebSocket, rundown, and RTSP image
docker-compose.yml             Base CPU-capable stack
docker-compose.gpu.yml         NVIDIA GPU overlay
docker-compose.cpu.yml         Conservative CPU overlay
config.example.env             Required environment template
config/                        Model catalog and role benchmark matrix
scripts/                       Ollama initialization and model health checks
src/fc27_mcp_server.py         EA API proxy and MCP-style JSON-RPC tools
src/graph.py                   Role-routed recommendation pipeline
src/graph_api.py               Persistent Graph API and approval endpoints
src/live_match_watcher.py      Tier 1 post-match polling
src/live_ws_server.py          Redis-to-WebSocket bridge
src/post_match_rundown.py      Structured post-match facts and recommendations
src/rtsp_frame_analyzer.py     Tier 2 RTSP OCR analyzer
host_agent/capture_agent.py    Tier 3 Windows screen-capture agent
host_agent/install.ps1         Windows setup script
tests/                         Unit and synthetic end-to-end tests
```

## Prerequisites

- Docker Engine or Docker Desktop with Docker Compose v2.
- At least 16 GB system RAM for the default two-model configuration.
- NVIDIA driver and NVIDIA Container Toolkit for the GPU overlay.
- Python 3.11 or newer for local tests and the Windows capture agent.
- Optional LM Studio server if fallback is enabled.

## Initial configuration

```bash
cp config.example.env .env
python -c "import secrets; print(secrets.token_hex(32))"
```

Generate separate random values for `POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `GRAPH_API_TOKEN`, and `APPROVAL_SIGNING_SECRET`. Do not reuse them. Do not commit `.env`.

Validate interpolation before starting:

```bash
docker compose --env-file .env config --quiet
```

## Start with NVIDIA GPU

```bash
docker compose --env-file .env   -f docker-compose.yml   -f docker-compose.gpu.yml   up -d --build
```

## Start CPU-only

```bash
docker compose --env-file .env   -f docker-compose.yml   -f docker-compose.cpu.yml   up -d --build
```

For low-memory systems, use:

```env
OLLAMA_MAX_LOADED_MODELS=1
OLLAMA_NUM_PARALLEL=1
```

## Verify deployment

```bash
docker compose --env-file .env ps
docker compose --env-file .env logs model-health
docker compose --env-file .env exec ollama ollama list
curl -fsS http://localhost:8001/health
curl -fsS http://localhost:8766/health
```

Expected Graph health includes Redis and PostgreSQL:

```json
{"status":"ok","redis":"ok","postgres":"ok"}
```

## Run a manual recommendation workflow

```bash
curl -fsS -X POST http://localhost:8001/run   -H "Content-Type: application/json"   -H "X-API-Key: $GRAPH_API_TOKEN"   -d '{"thread_id":"demo-1","club_id":"330323","trigger":"manual"}'
```

Inspect the result:

```bash
curl -fsS http://localhost:8001/threads/demo-1   -H "X-API-Key: $GRAPH_API_TOKEN"
```

Approve:

```bash
curl -fsS -X POST http://localhost:8001/threads/demo-1/decision   -H "Content-Type: application/json"   -H "X-API-Key: $GRAPH_API_TOKEN"   -d '{
    "decision":"APPROVE",
    "commissioner":"batmanL8",
    "owner":"coach",
    "selection":"best",
    "candidate_id":"copy-from-lead-package",
    "player_id":"copy-from-lead-package",
    "notes":"Reviewed and approved"
  }'
```

Reject by changing `APPROVE` to `REJECT`. A decision is accepted only for the configured Commissioner identity and a thread in `awaiting_approval` state.

## Automated tests

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m compileall -q src tests host_agent
pytest -q
```

Windows PowerShell activation:

```powershell
.\.venv\Scripts\Activate.ps1
```

## Tier 1 monitoring

The live watcher polls the MCP proxy and triggers the Graph API when a new match appears. It sends the configured Graph API token on internal requests. Watch events with:

```bash
docker compose --env-file .env exec redis   redis-cli -a "$REDIS_PASSWORD" PSUBSCRIBE 'fc27:*'
```

## Tier 2 RTSP analyzer

Set `XBOX_IP` and start the optional profile:

```bash
docker compose --env-file .env --profile rtsp up -d rtsp-analyzer
```

The OCR coordinates are estimates. Calibrate them against the actual display and game HUD before relying on alerts.

## Tier 3 Windows capture agent

1. Keep Docker and Redis running on the same Windows host.
2. Run PowerShell as Administrator.
3. Run `host_agent/install.ps1`.
4. Edit `host_agent/.env` and replace `CHANGE_ME_REDIS_PASSWORD`.
5. Run calibration before normal capture.

Redis is bound to `127.0.0.1` by default. A capture agent on another host will not connect. Use a VPN or a mutually authenticated proxy instead of publishing Redis directly to a LAN or the internet.

## LM Studio fallback

Fallback is disabled by default:

```env
LM_STUDIO_FALLBACK_ENABLED=false
```

To enable it, start LM Studio's local server, load model identifiers matching `config/model_catalog.json`, then set:

```env
LM_STUDIO_FALLBACK_ENABLED=true
LM_STUDIO_BASE_URL=http://host.docker.internal:1234/v1
```

The Compose file maps `host.docker.internal` for Linux portability. Do not expose LM Studio publicly.

## Security hardening checklist

- Keep Ollama, MCP, PostgreSQL, and internal service ports unpublished.
- Keep host-published ports bound to `127.0.0.1` unless protected by a firewall and reverse proxy.
- Put Graph and WebSocket access behind TLS and user authentication for remote access.
- Use Docker secrets, Kubernetes secrets, Azure Key Vault, or another vault instead of `.env` in production.
- Pin all images by digest after validation. `latest` is convenient for Ollama development but is not reproducible.
- Run image and dependency scans in CI.
- Keep `no-new-privileges`, dropped capabilities, and non-root application users enabled.
- Do not mount the Docker socket into any agent.
- Restrict EA API egress to the MCP service where the host platform permits it.
- Treat model output as advisory and validate all structured output before dispatch.
- Retain PostgreSQL backups for thread state and approval audit records.
- Redact access tokens, player identifiers, and prompts from logs where required.
- Rotate the Graph API token and passwords after suspected disclosure.
- Use distinct production and development model catalogs.

## Troubleshooting

### Compose reports an unset variable

Run:

```bash
docker compose --env-file .env config
```

The base file intentionally fails when required secrets are missing. Confirm `.env` is in the repository root and contains no spaces around `=`.

### Ollama is unhealthy

```bash
docker compose logs ollama
docker compose logs ollama-init
docker compose exec ollama ollama list
```

On NVIDIA hosts, validate the runtime separately:

```bash
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
```

Reduce `OLLAMA_MAX_LOADED_MODELS` and `OLLAMA_NUM_PARALLEL` after GPU out-of-memory errors.

### Model health never becomes healthy

The sidecar performs a real generation against every model in `REQUIRED_MODELS`. Verify exact model names, available disk space, and Ollama logs. Pull manually with:

```bash
docker compose exec ollama ollama pull qwen2.5:7b
```

### Graph API returns 401

The caller must send the exact `GRAPH_API_TOKEN` in `X-API-Key`. The live watcher now does this automatically through its environment.

### Graph API returns 500 or unhealthy PostgreSQL

```bash
docker compose logs postgres graph-api
docker compose exec postgres pg_isready -U fc27 -d fc27
```

Changing `POSTGRES_PASSWORD` after the volume is initialized does not update the existing database password. Restore the original secret or deliberately recreate the volume after backing up data.

### Redis authentication errors

Ensure the same password is used in the Redis command, `REDIS_URL`, the Windows agent, and CLI commands. Existing clients without a password will fail after hardening.

### Watcher sees matches but cannot trigger analysis

```bash
docker compose logs live-watcher graph-api
```

Confirm `GRAPH_URL=http://graph-api:8001`, the API token is shared through the common environment, and Graph health is green.

### WebSocket clients receive no rundown

Confirm `post-match-rundown` is running and the WebSocket server subscribes to `fc27:rundown:ready`:

```bash
docker compose logs post-match-rundown ws-server
```

### LM Studio fallback fails on Linux

Confirm `host.docker.internal` resolves inside `graph-api`:

```bash
docker compose exec graph-api getent hosts host.docker.internal
```

Confirm LM Studio listens on a non-loopback interface permitted by its settings and the host firewall allows the Docker bridge.

### Windows capture agent ignores `.env`

The current agent explicitly loads `host_agent/.env`. Confirm it contains the authenticated Redis URL and that the launcher starts from the repository root.

### OCR produces false goals or timers

Run calibration, confirm resolution scaling, disable HDR if it changes contrast, and lower capture FPS while tuning. Never allow OCR alone to authorize a player action.

## Voice capabilities

Voice is feasible and should be added as an optional edge service, not embedded into every agent. Recommended flow:

```text
Microphone -> local speech-to-text -> authenticated voice gateway
           -> Graph API or read-only query -> Commissioner confirmation
           -> local text-to-speech -> speaker
```

Use one shared speech-to-text and text-to-speech service. Agent identity should be represented by voice profiles, not separate speech models per agent. Keep wake-word processing local, require push-to-talk or explicit confirmation for approvals, and never accept an approval from speech recognition alone. A voice approval should display the exact action, require a second authenticated confirmation, and then write the same PostgreSQL approval audit as the text workflow.

Voice adds CPU/GPU, microphone privacy, latency, interruption handling, and speaker-authentication concerns. It is reasonable as a separate Compose profile after the text workflow is stable. See `docs/VOICE_CAPABILITIES.md`.

## Known limits

- Docker runtime and live model inference must be tested on the target host.
- The EA endpoint is external and may change behavior or rate limits.
- RTSP and Windows OCR require hardware-specific calibration.
- The Graph pipeline currently produces role reports, while the schema and hierarchy modules provide the stricter 3/3/3 candidate and signed-dispatch workflow. The next production integration step is to make the Graph API persist those typed lead packages directly rather than free-form role reports.
- The shared Graph API token is suitable for a secured lab. Use identity-aware authentication for multi-user production deployment.

## Typed lead packages

Every Graph run now persists five schema-validated lead packages. Each package contains exactly `best`, `best_runner_up`, `middle`, `middle_runner_up`, `lowest`, and `ignore`. Approval must name the owner swarm, selection, candidate ID, and player ID. Any mismatch fails closed.

## Optional voice profile

```bash
docker compose --env-file .env --profile voice -f docker-compose.yml -f docker-compose.gpu.yml up -d --build

# Also accelerate Faster Whisper on NVIDIA:
docker compose --env-file .env --profile voice -f docker-compose.yml -f docker-compose.gpu.yml -f docker-compose.voice-gpu.yml up -d --build
```

The local STT service uses Faster Whisper. The local TTS service uses eSpeak NG for a lightweight offline baseline. The voice gateway listens on `127.0.0.1:9000`. It supports read-only thread status and TTS, but all spoken approve, reject, authorize, execute, deploy, or activate commands are blocked and converted into a visual-confirmation requirement. Voice never calls the Graph approval endpoint.
