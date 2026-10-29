> Archived document
>
> This file is retained for historical context and is not an authoritative
> operational guide. See `docs/ARCHITECTURE.md and docs/FC27_COMPONENT_STATUS.md` for the current implementation.
> Archived on `2026-10-29` from `IMPLEMENTATION_REPORT.md`.
>
> Warning: Do not execute commands in this archived document.

# Implementation report: fc27-full-integrated-typed-voice 2.0.0

## Executive summary
- **Phases done:** all of Phases 0–8 are implemented with these limits:
  - Docker runtime is unvalidated: no daemon was available.
  - Windhover is documented as an unresolved contract.
- **Approval bypass closed:**
  - Service and Commissioner credentials are separate.
  - Decisions are HMAC-signed and bound to a single-use PostgreSQL nonce.
  - The decision, audit insert and nonce consumption commit as one transaction.
  - A database trigger makes decided threads immutable.
- **Orchestration:** it is now a real LangGraph StateGraph with PostgresSaver checkpoints after every node and an `interrupt()` approval boundary. Resume is verified against the committed decision.
- **Role agents:**
  - They run on Swarms (or a native backend) behind one boundary.
  - Their schema-validated output measurably changes ranking.
- **Optional tracing:** self-hosted Langfuse tracing is available.
- **Test results:** 254 tests pass on Python 3.11 and 3.13 against real PostgreSQL. The baseline was 26.

## Files changed
95 paths changed relative to the baseline commit; see `git diff --name-status` or TEST_RESULTS.md.

**New modules:**
- `src/settings.py`, `security.py`, `approval_store.py`, `decision.py`, `thread_ids.py`, `db_migrate.py`
- `src/roles.py`, `swarms_backend.py`, `tracing.py`, `mcp_retry.py`, `ocr_parsing.py`
- `scripts/commissioner_decide.py`, `scripts/test_postgres.sh`
- `migrations/0001_initial.sql`, `0002_approval_security.sql`
- `docker-compose.langfuse.yml`, `requirements-{dev,swarms,langfuse}.txt`
- `docs/APPROVAL_SECURITY.md`, `docs/WINDHOVER_INTEGRATION_GAP.md`

**Rewritten:** `graph.py`, `graph_api.py`, `approval.py`, `lead_packages.py`, `audit_query.py`, `voice/tts_service.py`, README.

**Hardened by parallel work streams:**
- `fc27_mcp_server.py`, `live_ws_server.py`
- `live_match_watcher.py`, `rtsp_frame_analyzer.py`, `post_match_rundown.py`, `host_agent/capture_agent.py`
- `host_agent/install.ps1`

**Removed:**
- `src/agency_org.py`: constants only, imported nowhere. The role agents are in `roles.py` and `swarms_backend.py`.
- `.env.models.example`: never read.
- `requirements-test.txt`: replaced by `requirements-dev.txt`.

**Moved:** the audit notes went to `audit/historical/`, each labelled superseded.

## Database changes
- Migration `0002`:
  - `graph_threads`: adds `club_id`, `request_hash`, `package_version`, `created_at`, `approved_by`, `approved_at` and `decision_payload`; a status CHECK; and a terminal-fields CHECK.
  - Adds the `graph_threads_guard` trigger. Terminal rows cannot be updated or deleted, and only `awaiting_approval` rows can become terminal.
  - `approval_audit`: adds `approved_by`, `nonce` (FK, unique), `package_version` and `signature_sha256`, plus a unique index giving one decision per thread.
  - New table `approval_nonces`.
  - New table `run_sequences`.
- LangGraph `checkpoint*` tables are created by `PostgresSaver.setup()`.
- A forward-only runner records applied migrations with their SHA-256 and serialises runs with an advisory lock.

## API behaviour changes
| Endpoint | Role | Behaviour |
|---|---|---|
| `POST /run` | service | <ul><li>201 on success</li><li>Server allocates `fc27-<club>-run-<YYYYMMDD>-<seq>`</li><li>A supplied id must conform (else 422) and be new (else 409)</li><li>Pipeline failure is recorded as `failed`</li></ul> |
| `GET /threads/{id}` | service | Returns thread state from PostgreSQL, the authority. |
| `POST /threads/{id}/decision-challenge` | commissioner | Validates the candidate and issues a bound nonce (TTL 120 s, max 300). |
| `POST /threads/{id}/decision` | commissioner | Verifies HMAC, commits atomically, then resumes the graph. 409 on any conflict. |

Errors: 401 missing or invalid credential, 403 wrong role or bad signature. The app starts through the `uvicorn … create_app --factory` factory.

## Authentication and authorization
- **Service token:** given to watcher, voice and graph-api. It can run and read, never decide.
- **Commissioner token:** graph-api holds only its SHA-256. The identity recorded is `COMMISSIONER_ID`.
- **Signing secret:** held only by graph-api and the Commissioner's CLI.
- **Comparisons:** all constant-time.
- **Compose:**
  - `x-common` carries no credentials.
  - PostgreSQL is reachable only from graph-api (internal `fc27_data` network).
  - MCP, WS and voice each have their own token.

## LangGraph implementation
- **Versions:** langgraph 1.2.12 with langgraph-checkpoint-postgres 3.1.3. PostgresSaver runs over a psycopg pool.
- **Graph:**
  - search → scout → analyst → coach → tactics → director → prepare_approval → approval (`interrupt()`) → finalize_decision.
  - search failure → END.
- **Typed state** (`FC27State`): thread_id, club_id, fc_version, platform, trigger, run_seq, candidates, role_reports, lead_packages, package_version, approval_request, approval_decision, status, errors, model_ids, prompt_version, policy_version, code_revision, audit_log.
- **Resume:** the approval node re-reads the committed PostgreSQL decision and raises `ApprovalNotCommitted` otherwise. Forged resume values and `update_state` edits cannot approve, and voice has no route to resume.
- **Terminal threads:** resuming one is a no-op.
- **Pending detection:** pending approval is detected from snapshot tasks, so a failed resume attempt does not strand the thread.
- **Startup:** reconciliation resumes threads committed while the graph was not resumed.
- **Audit CLI:** `src/audit_query.py` reports node progression, interrupt, committed decision and checkpoint metadata, with secrets redacted. It errors clearly for unknown threads.

## Checkpoint and thread behaviour
- Every run uses `durability="sync"`, so one checkpoint is written per step: at least 10 for a full run, which is asserted.
- Run config metadata `{"fc_version":"27","platform":"common-gen5","club_id":...}` appears on every checkpoint, which is asserted.
- Thread sequences come from an atomic upsert on `run_sequences`. Sixteen concurrent allocations produced 1..16 with no duplicates.

## MCP and WebSocket hardening
See `docs/notes/mcp_ws.md`.
- **MCP:** bearer auth, a 127.0.0.1 default bind, a bounded ThreadingHTTPServer (503 when saturated), a 64 KiB body cap, and per-request timeouts.
- **EA retry:** jittered exponential backoff with an attempt limit and a total budget. 429 honours `Retry-After` (capped); 403 gets at most one retry and then fails closed; 5xx and network errors are retried. Other 4xx are never retried.
- **SSE:** keepalive streams.
- **Club ids:** ASCII only. `search_club` returns URL-copy guidance on failure.
- **Match types:** `playoffMatch` added.
- **WebSocket:** token required (header or `fc27.v1` + `fc27.bearer.<token>` subprotocol), exact Origin allowlist, size/queue/timeouts, per-client queues so a slow client cannot stall others, no credential logging.

## Watcher, OCR, rundown and lead-package fixes
See `docs/notes/watcher_ocr_rundown.md`.
- **Watcher:** Redis lease/done/dead state per match, so a match is marked done only after a successful graph trigger. Attempts are bounded and processing is oldest-first. Cache latency is documented.
- **OCR:** a shared stdlib-only `ocr_parsing.py`.
  - Colon-preserving whitelist; 67:32 → (67, 32); seconds validated.
  - Confidence threshold and multi-frame debounce.
  - Plausibility checks, kickoff reset, and an orientation of home/away/unknown.
- **Rundown:** uses the event's match type with a fallback chain. Tackle fields are corrected. Prompts are a fixed instruction plus a JSON data block, with untrusted strings sanitized and capped.
- **Lead packages (policy `fc27-rank-v1`):**
  - Final score = base + role adjustment (±10).
  - Unique players only; fewer selections when fewer players exist.
  - `approvable=false` for empty packages or invalid role output; no placeholder candidates.
  - Approval must match the persisted candidate and the current `package_version`.
- **LM Studio:** `LM_STUDIO_FALLBACK_ENABLED` is enforced. Invalid values raise `ConfigError`.

## Swarms status
**Implemented.**
- `ROLE_BACKEND=swarms` is the Compose default. It uses one `swarms==15.0.3` `Agent` per role (max_loops 1, autosave off).
- Each agent's LLM adapter routes through `invoke_role`, so model policy and timeouts are shared with the native backend.
- The fixed system instruction is always injected by the adapter.
- **Safety measures:**
  - Telemetry is forced off before import, and the backend refuses to run if it is on.
  - The import's root-logger hijack and `./agent_workspace` writes are neutralised.
  - Failed calls never echo input as output.
- Tests use mocked models. Behaviour against a live Ollama is unvalidated.

## Langfuse status
**Implemented, optional, disabled by default.**
- The `src/tracing.py` tracer is thread-correlated through a deterministic trace id. It records nodes, role calls, validation failures, MCP/search failures and the approval interrupt and resume.
- `fc_version` and `platform` are on every observation.
- Redaction runs before the SDK and again in its mask hook. Content is omitted unless `LANGFUSE_CAPTURE_CONTENT=true`.
- `docker-compose.langfuse.yml` brings up Langfuse 4.49.0 web and worker, Postgres 17, ClickHouse, MinIO and Redis:
  - Images are digest-pinned and run on an internal network.
  - Langfuse telemetry is off and the UI binds 127.0.0.1.
  - Startup fails closed on missing secrets.
- Tested with the real SDK and an in-memory exporter. A live Langfuse server is unvalidated.
- Retries inside the MCP container are not traced: MCP deliberately holds no Langfuse credentials.

## Windhover status
**Unresolved.** No interface contract exists in the repository. See `docs/WINDHOVER_INTEGRATION_GAP.md`. No implementation was written.

## Remaining limitations
- No Docker daemon was available, so images were not built and the stack was not started.
- `POST /run` is synchronous.
- The HMAC key is symmetric, so a compromised graph-api can forge decisions (an asymmetric signing path is documented).
- Secrets are environment variables in the lab Compose (`*_FILE` supported).
- `install.ps1` is not executed (no PowerShell).
- The ea.com club URL pattern in the search fallback is unverified.
- Per-player observer agents and automatic player-agent dispatch are not implemented.

## Exact validation commands and results
See `TEST_RESULTS.md`. Summary:
- `python -m compileall -q src tests host_agent voice`: exit 0.
- `python -m pytest`: 254 passed on 3.13.15 and on 3.11.16.
- All seven `docker compose … config --quiet` variants pass (base, cpu, gpu, voice, rtsp, langfuse overlay+profile, voice+gpu+voice-gpu).

## Unresolved failures
None in the executed suite. The items under "Not validated" in TEST_RESULTS.md were not executed, so they are neither passes nor failures.
