# FC27 Pro Clubs Platform (iwa.dev.fcbuild)

This is a local-first match-analysis and recommendation stack for an EA FC27 Pro Clubs club (`fc_version: "27"`, `platform: "common-gen5"`).

- Model output is advisory only.
- Club-level actions require authorization from the Commissioner or an explicitly authorized human delegate under the applicable authority policy. Player-specific assignments require the affected player's authenticated decision when the consent policy requires it. After policy-defined escalation, affected-player fallback may apply only to that player's eligible assignment. No agent may decide for a human, and inactivity never constitutes approval.

**Status.** The stack can be deployed for a controlled lab or a secured single-host setup. It is not a public internet service.
Compose configuration and the automated test suite are validated. Running the full stack in Docker on a target host has **not** been validated (see `TEST_RESULTS.md`).

## Architecture

```text
EA Clubs API ── fc27-mcp (only EA caller; bearer auth, cache, 30 req/min/endpoint, bounded retry)
                   │
live-watcher ──────┼─► graph-api ── LangGraph StateGraph + PostgresSaver (fc27_data network: graph-api + postgres only)
 (service token)   │      search → scout → analyst → coach → tactics → director → prepare_approval
                   │      → approval [interrupt()] → finalize_decision
                   │      role agents: Swarms (default) or native, via one invoke_role boundary → Ollama (LM Studio opt-in)
post-match-rundown ┘      decisions: Commissioner token + HMAC-signed, nonce-bound, atomic (scripts/commissioner_decide.py)
Redis event bus ─► ws-server (token + Origin allowlist) ─► dashboards
optional: voice profile (read-only), rtsp profile (OCR), Langfuse overlay (tracing), Windows capture agent
```

## Implemented vs planned

| Area | Status |
|---|---|
| MCP proxy | **Implemented.**<ul><li>Bearer auth, bounded concurrency, cache, rate limit</li><li>429/403/5xx backoff with jitter</li><li>ASCII club ids, `/clubs/search` fallback guidance</li></ul> |
| LangGraph orchestration | **Implemented.**<ul><li>StateGraph with PostgresSaver and per-node checkpoints (`durability="sync"`)</li><li>`interrupt()` at approval</li><li>Checkpoint metadata `{fc_version, platform, club_id}`</li></ul> |
| Commissioner approval | **Implemented.**<ul><li>Separate credentials, signed nonces, atomic transaction</li><li>Immutable terminal state enforced by a DB trigger</li></ul> |
| Role agents | **Implemented.**<ul><li>Swarms backend (`swarms==15.0.3`, telemetry forced off) and native backend</li><li>Schema-validated output; bounded adjustments change ranking</li></ul> |
| Langfuse | **Implemented, optional** (`docker-compose.langfuse.yml`, profile `langfuse`). Disabled by default. Runtime against a live Langfuse server is not validated. |
| Voice | **Implemented, optional** (`--profile voice`). Read-only status, TTS and STT. **Voice cannot authorize decisions.** |
| Watcher / OCR / rundown | **Implemented.**<ul><li>Idempotent Redis processing state</li><li>Clock parsing with debounce</li><li>Orientation-aware events</li><li>JSON-isolated prompts</li></ul> |
| Windhover | **Unresolved.** No interface contract exists; see `docs/WINDHOVER_INTEGRATION_GAP.md`. |
| Per-player observer agents, automatic player-agent dispatch | **Data foundation only (Phase 1).** Dispatch disabled; approvals record the selection only. See `docs/DISPATCH_ACTIVATION.md`. |

## Credentials (summary)

All of these come from `config.example.env`. See `docs/APPROVAL_SECURITY.md`.

| Variable | Purpose |
|---|---|
| `GRAPH_SERVICE_TOKEN` | Start runs and read threads. **Cannot approve.** |
| `GRAPH_COMMISSIONER_TOKEN_SHA256` | Hash of the Commissioner's token. The plaintext stays off the server. |
| `COMMISSIONER_ID` | The identity recorded as `approved_by`. |
| `APPROVAL_SIGNING_SECRET` | HMAC key for decisions. Held by graph-api and the Commissioner only. |
| `MCP_API_TOKEN`, `WS_AUTH_TOKEN`, `VOICE_API_TOKEN` | Per-service credentials. |
| `POSTGRES_PASSWORD`, `REDIS_PASSWORD` | Datastores. Only graph-api can reach PostgreSQL. |

## Quick start

```bash
# 1. Populate configuration and secrets
cp config.example.env .env                        # fill non-secret settings
python scripts/commissioner_decide.py hash-token    # generate GRAPH_COMMISSIONER_TOKEN_SHA256
# Populate ./secrets/ files (see MIGRATION_GUIDE.md) with umask 077

# 2. Start PostgreSQL and run one-time role bootstrap
docker compose up -d postgres
docker compose --profile bootstrap run --rm db-bootstrap --confirm

# 3. Launch the stack
docker compose up -d --build
curl -fsS http://127.0.0.1:8001/health/ready
```

## Running a recommendation and deciding

```bash
# Start a run (service token). The server allocates fc27-<clubId>-run-<YYYYMMDD>-<seq>.
curl -fsS -X POST http://127.0.0.1:8001/run -H "X-API-Key: $GRAPH_SERVICE_TOKEN" \
     -H "Content-Type: application/json" -d '{"club_id":"330323","trigger":"manual"}'

# Inspect and decide (Commissioner workstation; secrets via env or *_FILE)
python scripts/commissioner_decide.py show    fc27-330323-run-20261001-001
python scripts/commissioner_decide.py approve fc27-330323-run-20261001-001 coach best --notes "reviewed"
```

**Human approval point.**
- The graph pauses at `interrupt()` with the payload `{thread_id, package_version, approvable_owners, fc_version: "27", platform: "common-gen5"}`.
- The graph resumes only after the signed decision commits.
- The API then calls `Command(resume={"thread_id": ...})` itself. There is no separate resume endpoint.
- If that resume fails, it is retried at the next graph-api startup.

**Thread rules.**
- A `thread_id` is created once. Re-posting it returns 409; a rerun needs a new id, or omit it and the server allocates one.
- Approved or rejected threads can never change; a database trigger enforces this.

**Audit trail.**

```bash
docker compose exec graph-api python -m src.audit_query fc27-330323-run-20261001-001
```

This shows node progression, the interrupt, the committed decision and checkpoint metadata, with secrets redacted.

## LM Studio fallback

`LM_STUDIO_FALLBACK_ENABLED` controls it:

- `false` (default), `0`, `no` or unset: no LM Studio request is ever made, even when Ollama fails.
- `true`, `1` or `yes`: LM Studio routes are added after healthy Ollama routes.
- Any other value fails at startup.

## Optional profiles

```bash
docker compose --env-file .env --profile voice up -d --build      # STT/TTS + read-only voice gateway (127.0.0.1:9000)
docker compose --env-file .env --profile rtsp up -d rtsp-analyzer # requires XBOX_IP; calibrate OCR regions first
docker compose --env-file .env -f docker-compose.yml -f docker-compose.langfuse.yml --profile langfuse up -d --build
```

Langfuse uses its own Postgres, ClickHouse, MinIO and Redis on an internal network. Its UI is at `127.0.0.1:3000`.

Traces are keyed by `thread_id` and cover:
- nodes and role calls
- validation failures
- the approval interrupt and resume

Credential-like keys are always redacted. Long free text is omitted unless `LANGFUSE_CAPTURE_CONTENT=true`.

If Langfuse is unreachable, runs continue and tracing errors are only logged. To run without traces, omit the overlay.

**Windows capture agent.** Run `host_agent/install.ps1`, then edit `host_agent/.env`. The installer verifies the SHA-256 of each download (pass `-TesseractSha256` and similar), or you must pass `-AllowUnverifiedDownload` explicitly.

## Tests

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt -r requirements-swarms.txt -r requirements-langfuse.txt
eval "$(scripts/test_postgres.sh)"     # disposable PostgreSQL 16 on 127.0.0.1 (or set TEST_POSTGRES_URI yourself)
python -m compileall -q src tests host_agent voice
python -m pytest
```

Some tests are skipped, each with a stated reason:
- Database tests need `TEST_POSTGRES_URI`. If it is set but unreachable, those tests fail.
- Swarms and Langfuse tests need their requirement files installed.
- Compose tests need the docker CLI.

## Upgrading from the shared-token release

See `MIGRATION_GUIDE.md`. In short:
1. Rotate every credential.
2. Replace `GRAPH_API_TOKEN` with the new service and Commissioner credentials.
3. Let graph-api apply migration 0002.
4. Re-run any thread that was `awaiting_approval` before the upgrade.

## Programme Roadmap & Phase Status

Phase 1 intentionally implements only the secure data foundation. The dashboard, identity, player-agent pipeline, personalization, outbox relay, tracking, analytics, audit UI, and operational deployment remain required deliverables in Phases 2 through 6.

- [x] Phase 1: Secure data foundation
- [ ] Phase 2: Identity and authorization
- [ ] Phase 3: Dispatch worker and player-agent pipeline
- [ ] Phase 4: Dashboard UI and real-time delivery
- [ ] Phase 5: Tracking and analytics
- [ ] Phase 6: Operations, audit, and rollout

### Mandatory Phase 1 Status Statement
> "Secure data foundation complete. Dispatch remains disabled. No player delivery has occurred."

### Architectural Invariant Distinctions
The platform enforces clear boundaries across lifecycle concepts:
- **Approval != Activation**: Commissioner approval commits recommendation intent; it does not activate a plan.
- **Approval != Dispatch**: Approvals record the selection only; no player delivery or dispatch occurs in Phase 1.
- **Graph Resume != Authorization**: A resume signal only wakes the workflow; authorization must be read from committed PostgreSQL records.
- **Redis Publication != Authoritative Delivery**: Redis provides transient notification wake-ups only; PostgreSQL is the sole authoritative audit store.
- **Phase 1 Completion != Programme Completion**: Phase 1 provides the secure data foundation; the complete programme requires Phases 2 through 6.

## Known limits

- `POST /run` executes the graph synchronously, so callers need a long timeout (the watcher uses `GRAPH_RUN_TIMEOUT_SEC`, default 900).
- Retries inside the MCP container are logged but not sent to Langfuse, because MCP holds no Langfuse credentials.
- Detecting new matches can lag by up to the MCP match cache (300 s) plus the poll interval.
- OCR screen regions are estimates. Calibrate them before relying on alerts; OCR alone never authorizes anything.
- These live behaviours are unvalidated: the EA endpoint behaviour, live model inference, GPU allocation, the Swarms backend against a real Ollama, and the Langfuse server.

## Repository map

```text
src/graph_api.py          HTTP API (create_app factory), roles, run/decision endpoints
src/graph.py              LangGraph StateGraph, GraphRunner, interrupt/resume
src/approval_store.py     transactional run/decision persistence, nonces
src/approval.py           canonical payload + HMAC-SHA256
src/security.py           service vs commissioner authentication
src/roles.py, swarms_backend.py, lead_packages.py   role agents, ranking policy
src/tracing.py            optional Langfuse tracer with redaction
src/fc27_mcp_server.py, mcp_retry.py               EA proxy
src/live_match_watcher.py, post_match_rundown.py, live_ws_server.py, rtsp_frame_analyzer.py, ocr_parsing.py
src/audit_query.py        audit CLI; src/db_migrate.py + migrations/  schema
scripts/commissioner_decide.py, test_postgres.sh, model_health.py, ollama-init.sh
host_agent/               Windows capture agent and installer
docs/                     approval security, deployment, voice, models, Windhover gap, notes/
audit/historical/         superseded audit notes (historical only)
```

See also `docs/VOICE_CAPABILITIES.md`, `docs/DEPLOYMENT_AND_SECURITY.md`, `IMPLEMENTATION_REPORT.md` and `SECURITY_CHANGES.md`.

## License

Apache License 2.0. See `LICENSE`.
