# Deployment and security hardening

> **Reconciliation Note (`PARTIALLY_SUPERSEDED`)**: This document has been reconciled against the live repository inventory (`27` unique services, `33` service declarations across `9` Compose files: `docker-compose.yml`, `docker-compose.cpu.yml`, `docker-compose.gpu.yml`, `docker-compose.voice-gpu.yml`, `docker-compose.langfuse.yml`, `docker-compose.community-worker.yml`, `docker-compose.discord.yml`, `docker-compose.ghcr.yml`, and `docker-compose.community-eval.yml`) and the separate PostgreSQL databases (`fc27` on PostgreSQL 16 in schema `public`, plus the optional `langfuse` database on PostgreSQL 17 on internal network `fc27_obs`). For the authoritative runtime operations runbook, least-privilege role bootstrap (`fc27_owner`, `fc27_migrator`, `fc27_app`, `fc27_dispatch`, `fc27_relay`), and security invariants, see [`OPERATIONS.md`](OPERATIONS.md) and [`SECURITY_BOUNDARIES.md`](SECURITY_BOUNDARIES.md). The approval model is covered in [`FC27_APPROVAL_FLOW.md`](FC27_APPROVAL_FLOW.md) and [`APPROVAL_SECURITY.md`](APPROVAL_SECURITY.md). Live FC 27 provider calls are disabled by default (`FC27_PROVIDER_ENABLED=false`; see [`FC27_PROVIDER_INTEGRATION.md`](FC27_PROVIDER_INTEGRATION.md)).

## 1. Host preparation (Repository-wide vs. Subsystem-specific)
1. **Repository-wide**: Update the OS, Docker Engine/Desktop, the Compose v2 plugin, and the container runtime. Keep the host firewall enabled; every host-published port across all Compose files binds to `127.0.0.1` (enforced by `tests/test_compose_isolation.py`).
2. **Subsystem-specific (optional GPU profiles)**: For NVIDIA GPUs (`docker-compose.gpu.yml`, `docker-compose.voice-gpu.yml`, `--profile local-ollama`), install the NVIDIA Container Toolkit and validate it with `docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi`.

## 2. Secrets and credential delivery
1. Copy `config.example.env` to `.env` and replace every `replace_with_*` value with an independently generated random value.
2. Store the Commissioner token in a password manager. Put only its SHA-256 in `.env`: `python scripts/commissioner_decide.py hash-token`.
3. **File-backed Compose secrets (`./secrets/<name>` -> `/run/secrets/<name>`, gitignored)**:
   - Durable control-plane passwords: `postgres_password` (`POSTGRES_PASSWORD_FILE=/run/secrets/postgres_password` on `postgres`) and `redis_password` (`/run/secrets/redis_password` consumed by `redis` via `exec redis-server --appendonly yes --requirepass "$(cat /run/secrets/redis_password)"`).
   - Role bootstrap and database URIs: `postgres_admin_uri`, `fc27_migrator_password`, `fc27_app_password`, `fc27_dispatch_password`, `fc27_relay_password`, `fc27_migrator_uri`, `fc27_app_uri`, `fc27_relay_uri`, `fc27_dispatch_uri`.
   - Identity and BFF secrets: `bff_service_token`, `identity_encryption_key`, `bootstrap_token`.
   - Optional Discord overlay secret: `discord_bot_token` (`docker-compose.discord.yml`).
4. **Environment-backed settings (`.env`)**: `GRAPH_SERVICE_TOKEN`, `GRAPH_COMMISSIONER_TOKEN_SHA256`, `APPROVAL_SIGNING_SECRET`, `COMMISSIONER_ID`, `MCP_API_TOKEN`, `WS_AUTH_TOKEN`, `VOICE_API_TOKEN`, client `REDIS_URL` password interpolation (`REDIS_PASSWORD`), and optional Langfuse overlay secrets (`LANGFUSE_SECRET_KEY`, `LANGFUSE_ENCRYPTION_KEY`, `LANGFUSE_DB_PASSWORD`, `LANGFUSE_SALT`, `NEXTAUTH_SECRET`).
5. `src/settings.py` (`read_secret`) accepts both `NAME` and `NAME_FILE`, allowing remaining environment-backed application secrets to move to file mounts without code changes.
6. `docker compose config` checks required interpolation variables; it does not prove that secret files exist or contain usable credentials. Provision and verify those files separately before any authorized startup.

## 3. Credential placement, `fc27_data` network memberships, and PostgreSQL role posture (enforced by `tests/test_compose_isolation.py`)

### 3.1 Service credential placement

| Credential / Secret | Delivery | Held by |
|---|---|---|
| `postgres_password` | File secret (`/run/secrets/postgres_password`) | `postgres` only |
| `redis_password` | File secret (`/run/secrets/redis_password`) | `redis` container (`REDIS_PASSWORD` remains in `.env` for client `REDIS_URL` interpolation) |
| `postgres_admin_uri`, `fc27_*_password` (`migrator`, `app`, `dispatch`, `relay`) | File secrets (`/run/secrets/*`) | `db-bootstrap` only (`--profile bootstrap`) |
| `fc27_migrator_uri` (`POSTGRES_URI_FILE`) | File secret (`/run/secrets/fc27_migrator_uri`) | `db-migrate` only |
| `fc27_app_uri` (`POSTGRES_URI_FILE`) | File secret (`/run/secrets/fc27_app_uri`) | `graph-api`, plus optional overlays `fc27-community-worker` (`--profile fc27-polling`) and `fc27-discord-bot` (`--profile fc27-discord`) |
| `fc27_relay_uri` (`POSTGRES_URI_FILE`) | File secret (`/run/secrets/fc27_relay_uri`) | `outbox-relay` only |
| `fc27_dispatch_uri` (`POSTGRES_URI_FILE`) | File secret (`/run/secrets/fc27_dispatch_uri`) | `dispatch-worker` only |
| `GRAPH_COMMISSIONER_TOKEN_SHA256`, `APPROVAL_SIGNING_SECRET`, `bff_service_token`, `identity_encryption_key`, `bootstrap_token` | Env + file secrets | `graph-api` only |
| Commissioner token (plaintext), `APPROVAL_SIGNING_SECRET` | Operator workstation | the Commissioner's workstation (`scripts/commissioner_decide.py`) |
| `GRAPH_SERVICE_TOKEN` | Environment | `graph-api` (to verify it), `live-watcher`, `voice-gateway` |
| `MCP_API_TOKEN` | Environment | `fc27-mcp`, `graph-api`, `live-watcher`, `post-match-rundown` |
| `WS_AUTH_TOKEN` | Environment | `ws-server` and its clients |
| `VOICE_API_TOKEN` | Environment | `voice-gateway` and its clients |
| `discord_bot_token` (`DISCORD_BOT_TOKEN_FILE`) | File secret (`/run/secrets/discord_bot_token`) | `fc27-discord-bot` only (`docker-compose.discord.yml`, `--profile fc27-discord`) |

### 3.2 Bounded `fc27_data` network memberships
PostgreSQL (`postgres`) sits on the internal `fc27_data` bridge network (`internal: true`). `graph-api` is **not** the only service on `fc27_data`; the bounded services that join `fc27_data` are:
- **Base `docker-compose.yml`**: `postgres`, `db-bootstrap` (`--profile bootstrap`), `db-migrate`, `graph-api`, `outbox-relay`, and `dispatch-worker`.
- **Optional profile overlays**: `fc27-community-worker` (`docker-compose.community-worker.yml`, `--profile fc27-polling`) and `fc27-discord-bot` (`docker-compose.discord.yml`, `--profile fc27-discord`).
- All other services (`redis`, `fc27-mcp`, `model-health`, `live-watcher`, `post-match-rundown`, `ws-server`, `rtsp-analyzer`, `stt`, `tts`, `voice-gateway`, `ollama`, `ollama-init`, `fc27-community-client-evaluation`, and `langfuse-*`) are excluded from `fc27_data`.

### 3.3 PostgreSQL role-model status and least-privilege remediation plan (`POSTGRES_ROLE_MODEL=FAIL`)
- **Implemented role boundaries (`src/db_roles.py`, `migrations/post/grants.sql` policy version `15`)**:
  - `fc27_owner` is `NOLOGIN` and owns all schema objects in `public`.
  - `fc27_migrator` is the dedicated migration login used exclusively by `db-migrate` (`src.db_migrate`).
  - `fc27_match_results_writer` is the dedicated least-privilege writer login (`src/db_roles.py`, `migrations/post/grants.sql` policy version `15`) scoped strictly to `public.fc27_manual_matches`, `public.fc27_manual_match_players`, `public.fc27_manual_match_events`, and `public.fc27_manual_match_audit`.
  - `fc27_dispatch` (`dispatch-worker`) and `fc27_relay` (`outbox-relay`) have narrowly scoped table grants.
- **Current role-model gap (`DISCORD_ANALYTICS_READ_ONLY=NO` at PostgreSQL authorization layer)**:
  - `fc27-discord-bot` application code (`src/fc27_discord_bot.py`) enforces `SELECT`-only SQL queries on analytics tables, and manual Match Results write commands use `GuardedMatchResultsWriterConnection` (`FC27_MATCH_RESULTS_WRITER_URI_FILE`), but `docker-compose.discord.yml` still mounts `fc27_app_uri` (`fc27_app`) as its default reader credential. Because `fc27_app` holds write grants (`INSERT`, `UPDATE`) on operational and FC27 provider tables in `migrations/post/grants.sql`, the default Discord analytics credential is **not** read-only at the PostgreSQL authorization layer until secret rotation and a dedicated `fc27_discord_reader` role are provisioned (`BLOCKED_SECRET_ROTATION`).
- **Role remediation classification & follow-up security plan**:
  - `fc27_owner`, `fc27_migrator`, `fc27_match_results_writer`, `fc27_dispatch`, `fc27_relay`: `IMPLEMENT_NOW` (implemented in `src/db_roles.py` and `migrations/post/grants.sql`).
  - Shared `fc27_app_uri` mount on `fc27-discord-bot` and `fc27-community-worker`: `DEPRECATE_FIRST` / `BLOCKED_SECRET_ROTATION` (documented as transitional until dedicated runtime role secret files `secrets/fc27_discord_reader_uri` and `secrets/fc27_match_results_writer_uri` are provisioned on the host).
  - `fc27_discord_reader` (`SELECT`-only PostgreSQL role for `fc27-discord-bot`): `SEPARATE_SECURITY_PR` / `BLOCKED_RUNTIME_CREDENTIAL_ROTATION` / `REVIEW_REQUIRED`.
  - Dedicated provider/community-worker writer role: `SEPARATE_SECURITY_PR` / `BLOCKED_RUNTIME_CREDENTIAL_ROTATION` / `REVIEW_REQUIRED`.

## 4. Canonical migration authority (`db-migrate` / `src.db_migrate`) and startup
```bash
cp config.example.env .env   # then fill in every value and populate ./secrets/*
docker compose --env-file .env config --quiet
docker compose --env-file .env up -d --build   # uses the remote Ollama at OLLAMA_BASE_URL (araba)
# Bundled Ollama instead (set OLLAMA_BASE_URL=http://ollama:11434 first):
# docker compose --env-file .env -f docker-compose.yml -f docker-compose.gpu.yml --profile local-ollama up -d --build   # or docker-compose.cpu.yml
docker compose --env-file .env ps
curl -fsS http://127.0.0.1:8001/health
```
- **Canonical migration service and runner**: Compose service `db-migrate` is the current deployment migration service, and `src.db_migrate` (`python -m src.db_migrate`) is the canonical migration runner. `graph-api` does **not** uniquely own migration behavior.
- **Graph startup migration disabled in Compose (`MIGRATION_RACE_RISK=NO`)**: In `docker-compose.yml`, `graph-api` sets `GRAPH_RUN_MIGRATIONS=false` and `GRAPH_CHECKPOINT_SETUP=false` and waits for `db-migrate` (`condition: service_completed_successfully`).
- **Scoped execution under advisory lock**: `src.db_migrate` connects as `fc27_migrator`, switches to `SET ROLE fc27_owner`, and acquires PostgreSQL session advisory lock `0x0FC27_0001` (`pg_advisory_lock`). It applies numbered SQL migrations `0001` through `0013` (`migrations/0001_initial.sql` .. `migrations/0013_fc27_manual_match_results.sql` recorded in `public.schema_migrations`), invokes `PostgresSaver.setup()` for LangGraph checkpoint tables by default, and applies reviewed post-migration grants (`migrations/post/grants.sql`, `GRANTS_POLICY_VERSION = 15` recorded in `public.schema_policy_versions`). The Python `migrate` function accepts a `checkpoint_setup` argument; the CLI uses the default.

## 5. GPU sizing (Subsystem-specific)
The default remote server (araba, RTX 3080 10 GB) is sized and tuned by its owner; its `OLLAMA_*` settings are not controlled from this repository. For the bundled container (`--profile local-ollama`), start with `OLLAMA_MAX_LOADED_MODELS=2` and `OLLAMA_NUM_PARALLEL=2`. If you see out-of-memory errors or model eviction, reduce both to 1.

## 6. Network hardening
- MCP (`fc27-mcp`) and PostgreSQL (`postgres`) publish no host ports; the bundled Ollama (`--profile local-ollama`), `fc27-community-worker` (`--profile fc27-polling`), and `fc27-discord-bot` (`--profile fc27-discord`) publish none either. The default remote Ollama server is reached outbound over plain HTTP on the lab network and has no authentication; it relies on its host firewall, so run the stack only on an admitted client.
- Redis is published on `127.0.0.1:6379` for the Windows capture agent only.
- Put any remote access behind a VPN or a TLS reverse proxy with authentication. Do not change the bindings to `0.0.0.0`.
- Live calls to `proclubs.ea.com` are disabled and blocked by default (`FC27_PROVIDER_ENABLED=false`, `FC27_LIVE_INGESTION_ENABLED=false`, `FC27_COMMUNITY_POLLING_ENABLED=false`, `FC27_COMMUNITY_PROVIDER_ENABLED=false`); only the provider-neutral boundary (`src/fc27_authorized_transport.py`) or the explicitly gated community worker (`src/fc27_polling_worker.py`) may contact an external endpoint when all gates are explicitly enabled.

## 7. Container and control-plane hardening
- **Repository-wide**: Application containers run with `no-new-privileges`, `cap_drop: ALL`, an init process (`init: true`), and non-root users. Base and runtime images are pinned by tag and `@sha256:` digest; re-verify them with `docker buildx imagetools inspect <image>`. Never mount the Docker socket into any container. Optional profiles (`bootstrap`, `voice`, `rtsp`, `local-ollama`, `langfuse`, `fc27-polling`, `fc27-discord`) are disabled by default.
- **Durable control-plane (`postgres` and `redis`)**: Both services run with file-backed secrets (`postgres_password`, `redis_password`), bounded CPU/memory/PID limits (`postgres`: `cpus: 1.50`, `mem_limit: 1g`, `pids_limit: 128`, `stop_grace_period: 90s`; `redis`: `cpus: 1.00`, `mem_limit: 512m`, `pids_limit: 128`, `stop_grace_period: 45s`), and `json-file` log rotation (`max-size: 10m`, `max-file: "3"`).
- **Subsystem-specific (`fc27-community-worker` and `fc27-discord-bot`)**: Run as non-root `10001:65534` with `read_only: true` root filesystems, `tmpfs: /tmp:size=32m,noexec,nosuid,nodev`, `cpus: 0.50`, `mem_limit: 256m`, `pids_limit: 64`, and `stop_grace_period: 30s`.

## 8. Critical database state, backup scope, and historical recovery status
- **Required full-database backup scope (`public` schema)**: A complete operational backup must cover the **full required database state** across all active domains:
  1. **Schema and migration ledger**: `schema_migrations`, `schema_policy_versions`.
  2. **FC27 provider-derived matches and club metadata**: `fc27_clubs`, `fc27_matches`.
  3. **Player statistics**: `fc27_player_match_stats`.
  4. **Polling reconstruction records, ingest/export history, and quarantine**: `fc27_export_batches`, `fc27_quarantine_records`.
  5. **Identity state**: `fc27_users`, `fc27_user_roles`, `fc27_invitations`, `fc27_sessions`, `fc27_webauthn_credentials`, `fc27_totp_credentials`, `fc27_recovery_codes`, `fc27_action_tokens`, `fc27_challenges`, `fc27_bootstrap_locks`, `fc27_credentials`.
  6. **Approval state, nonces, and authority audit**: `graph_threads`, `run_sequences`, `approval_requests`, `approval_nonces`, `approval_audit`, `fc27_decision_evidence`, `authority_policies`, `authority_delegations`, `fc27_delegations`, `player_assignments`, `player_decision_requests`, `player_decisions`, `approval_escalations`, `authority_conflicts`, `idempotency_keys`.
  7. **Dispatch state**: `dispatches`, `dispatch_plans`, `dispatch_transition_rules`, `player_observations`, `dispatch_assessments`, `dispatch_delivery_attempts`, `dispatch_acknowledgments`, `dispatch_plan_decisions`, `dispatch_comments`, `analyst_notes`, `players`, `activation_gates`, `activation_gate_log`, `service_heartbeats`, `dispatch_worker_claims`.
  8. **Outbox and domain events**: `domain_events`, `outbox`.
  9. **LangGraph checkpoint state (when enabled)**: `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations`.
  10. **Manual Match Results and Match Results audit/Discord linkage**: `fc27_manual_matches`, `fc27_manual_match_players`, `fc27_manual_match_events`, `fc27_manual_match_audit` (`migrations/0013_fc27_manual_match_results.sql`).
- **Current backup implementation gap (`PARTIAL_FC27_DOMAIN_TABLES_ONLY`)**: Current `src/fc27_backup_restore.py` (`scripts/fc27_backup.py`; see [`FC27_BACKUP_AND_RECOVERY.md`](FC27_BACKUP_AND_RECOVERY.md)) archives the **11** tables in `FC27_DOMAIN_TABLES` (`fc27_clubs`, `fc27_export_batches`, `fc27_matches`, `fc27_player_match_stats`, `fc27_quarantine_records`, `schema_migrations`, `schema_policy_versions`, `fc27_manual_matches`, `fc27_manual_match_players`, `fc27_manual_match_events`, `fc27_manual_match_audit`). Note that `fc27_match_team_stats` and `fc27_manual_discord_linked_matches` in `FC27BackupTableCounts` are derived manifest row-count aliases, not separate physical PostgreSQL tables. Expanding executable backup code to cover the omitted identity, approval, dispatch, outbox, and checkpoint tables—along with full-stack restoration ownership/procedure, archive encryption and NAS transfer, and failure handling—is tracked as a dedicated follow-up (`SEPARATE_BACKUP_REMEDIATION_PR`).
- **Historical database recovery status (`HISTORICAL_RECOVERY_VERIFIED=NO`)**: No authoritative historical PostgreSQL backup or poll-state archive has yet been verified. Model weights can be pulled again.
