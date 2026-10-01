# Deployment and security hardening

## 1. Host preparation
1. Update the OS, Docker Engine/Desktop, Compose plugin, GPU driver, and container runtime.
2. For NVIDIA, install the NVIDIA Container Toolkit and validate `docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi`.
3. Keep the host firewall enabled. Do not expose Ollama, PostgreSQL, Redis, or ClickHouse to the public internet.
4. Create an unprivileged deployment account. Do not run the stack from an administrator shell routinely.

## 2. Secrets
1. Copy `.env.models.example` to `.env.models`.
2. Generate a signing secret with `python -c "import secrets; print(secrets.token_hex(32))"`.
3. Store production secrets in Docker secrets or a vault, not committed `.env` files.
4. Rotate the approval signing secret after suspected disclosure. Pending approvals must become invalid.

## 3. GPU sizing and profiles
1. Start with one GPU, two loaded models, and two parallel requests.
2. If VRAM pressure or model eviction occurs, set `OLLAMA_MAX_LOADED_MODELS=1` and `OLLAMA_NUM_PARALLEL=1`.
3. Use `docker-compose.cpu.yml` only for CPU fallback. Expect much lower throughput.
4. Keep player observers on the smaller model and serialize expensive lead deliberations when hardware is limited.

## 4. Start local inference
```bash
docker compose --env-file .env.models -f docker-compose.yml -f docker-compose.ollama.yml up -d ollama
docker compose --env-file .env.models -f docker-compose.yml -f docker-compose.ollama.yml run --rm ollama-init
docker compose --env-file .env.models -f docker-compose.yml -f docker-compose.ollama.yml up -d model-health
```

## 5. Validate inference
```bash
docker compose -f docker-compose.yml -f docker-compose.ollama.yml ps
docker compose -f docker-compose.yml -f docker-compose.ollama.yml exec ollama ollama list
docker compose -f docker-compose.yml -f docker-compose.ollama.yml logs model-health
```
The health sidecar verifies model presence and performs a deterministic short generation against every required model.

## 6. Start the FC27 services
```bash
docker compose --env-file .env.models -f docker-compose.yml -f docker-compose.ollama.yml up --build -d postgres redis fc27-mcp graph ws-server post-match-rundown
```
Start the live watcher only after MCP, graph, model-health, PostgreSQL, and Redis are healthy.

## 7. Human approval boundary
1. Swarm outputs are recommendations only.
2. The lead package contains exactly six selections: best, best runner-up, middle, middle runner-up, lowest, and ignore.
3. Approval is scoped to one request, thread, owner swarm, candidate, player, time window, and nonce.
4. Sign approval server-side. Never put the signing secret in a browser or agent prompt.
5. Persist consumed nonces in Redis or PostgreSQL with a uniqueness constraint. The in-memory test store is not production storage.
6. Dispatch must verify the signature and exact candidate/player match, then write an immutable audit receipt before notifying the player agent.

## 8. Network hardening
1. Keep Ollama on an internal Docker network with `expose`, not `ports`.
2. Bind Graph API, MCP, WebSocket, Langfuse, Redis, and PostgreSQL to localhost or a management VLAN where possible.
3. Put external UI traffic behind TLS and an authenticated reverse proxy.
4. Add service-to-service authentication for Graph API, approval endpoints, and MCP. Do not rely on network location alone.
5. Restrict EA API egress to the MCP container. Other agent containers should not have unrestricted outbound internet.

## 9. Container hardening
1. Run with `no-new-privileges`, dropped capabilities, read-only filesystems, PID limits, memory limits, and CPU limits where compatible.
2. Pin images by digest after validation.
3. Scan images and Python dependencies in CI.
4. Mount only required files and volumes. Never mount the Docker socket into an agent container.

## 10. Application safeguards
1. Validate all LLM output with Pydantic schemas before ranking or dispatch.
2. Use deterministic statistical scoring as evidence. Treat model prose as advisory.
3. Reject missing evidence references, duplicate candidates, cross-swarm ownership, stale approvals, replayed nonces, and candidate tampering.
4. Redact secrets and authorization headers from logs and traces.
5. Place size limits on prompts, JSON payloads, WebSocket messages, and audit metadata.

## 11. Test and recovery
```bash
pytest -q tests/test_workflow.py
python scripts/model_health.py
```
Back up PostgreSQL and model configuration. Model weights can be re-pulled; approval audits and checkpoint history are the critical state.
