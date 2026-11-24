# FC27 Durable Service Heartbeats

## Purpose

`fc27-community-worker` and `fc27-discord-bot` publish durable PostgreSQL
liveness heartbeats so the read-only `/fc27 status` operations snapshot can
report real database-clock heartbeat ages instead of `heartbeat source unknown`.

The Docker healthchecks are unchanged: the bot keeps its container-local
heartbeat file for its healthcheck, and the worker keeps its `--status`
healthcheck. The durable PostgreSQL heartbeat is an independent observability
signal.

## Table

`public.fc27_service_heartbeats` — one row per reviewed service key.

| Column | Meaning |
| --- | --- |
| `service_key` | Primary key; one of the fixed keys below |
| `instance_digest` | SHA-256 (64 lowercase hex) of process-local OS entropy; non-reversible, never a container/host/PID/guild/role/credential encoding |
| `runtime_status` | `STARTING`, `READY`, `DEGRADED`, `STOPPING` |
| `started_at` | Database time when the current instance first reported |
| `last_seen_at` | Database time of the most recent report |
| `update_count` | Reports for the current instance (bounded; no history rows) |
| `created_at` | Database time the row was first created |

Fixed service keys:

- `fc27-community-worker`
- `fc27-discord-bot`

Any other key, unknown runtime status, or malformed digest is rejected by the
procedure.

## Writer API

Only `public.fc27_record_service_heartbeat(p_service_key, p_instance_digest,
p_runtime_status)` may write heartbeats. It is `SECURITY DEFINER`, owned by
`fc27_owner`, with `SET search_path = pg_catalog, pg_temp`, no dynamic SQL, and a
fixed allowlist. The observed time is always `clock_timestamp()`; callers can
never supply it.

Upsert semantics:

- Same instance digest: `started_at` is retained, `last_seen_at` advances,
  `update_count` increments.
- New instance digest: `started_at` resets, `last_seen_at` advances,
  `update_count` resets to 1.

Least-privilege grants (Grants Policy 27):

- `fc27_poll_writer` (community worker) — EXECUTE on the writer only.
- `fc27_match_results_writer` (Discord bot) — EXECUTE on the writer only.
- `fc27_discord_reader` / `fc27_app` — EXECUTE on the read helper only.
- No runtime role receives direct `INSERT`/`UPDATE`/`DELETE`/`SELECT` on the
  table. The procedure is the only write path.

## Runtime behaviour

Each runtime updates its heartbeat approximately every 60 seconds.

Environment (bounded, production-safe defaults):

| Variable | Default | Bounds |
| --- | --- | --- |
| `FC27_SERVICE_HEARTBEAT_ENABLED` | `true` | boolean |
| `FC27_SERVICE_HEARTBEAT_INTERVAL_SECONDS` | `60` | 30–300 |
| `FC27_SERVICE_HEARTBEAT_CONNECT_TIMEOUT_SECONDS` | `2` | 1–5 |
| `FC27_SERVICE_HEARTBEAT_STATEMENT_TIMEOUT_MS` | `1500` | 500–5000 |

- Worker: `STARTING` at startup, `READY` after initialization, periodic refresh,
  `STOPPING` on graceful shutdown.
- Bot: `STARTING` at init, `READY` only after gateway connected / bot ready /
  dispatch initialized, periodic refresh, `DEGRADED` on gateway disconnect,
  `READY` after resume, `STOPPING` on shutdown.

Heartbeat failure is isolated: the loop never raises into the primary service,
never crashes it, and logs only a sanitized, rate-limited category (never the
DSN, secret path, digest, or raw exception text). Records never overlap.

## Status classification

`fc27_get_service_heartbeat_status()` returns database-clock ages; the snapshot
classifies each service as:

| Condition | State |
| --- | --- |
| No row | `UNKNOWN` |
| age ≤ 120s and `READY` | `HEALTHY` |
| age > 120s and ≤ 300s | `DEGRADED` |
| age > 300s | `CRITICAL` |
| `runtime_status` ≠ `READY` with fresh heartbeat | `DEGRADED` |

`/fc27 status` renders bounded lines such as `Worker: healthy, heartbeat 34s`
and `Bot: healthy, heartbeat 21s`. It never renders the instance digest, raw
row IDs, database role, or container details. Docker healthcheck state remains
separate from the durable heartbeat state.

Thresholds: warning 120 seconds, critical 300 seconds.

## Recovery and retention

- One row per service means no retention job is required.
- Heartbeats are transient liveness state and are excluded from the automated
  domain backup bundle (`FC27_TRANSIENT_LIVENESS_TABLES`).
- If heartbeat rows are ever restored, `last_seen_at` preserves the original
  database timestamp on restore; the read helper always computes age as
  `clock_timestamp() - last_seen_at`, so restored stale rows classify
  `DEGRADED`/`CRITICAL` and are never treated as fresh merely because a restore
  occurred. The next live report advances the row naturally.
- No manual cleanup is required during ordinary restarts.

## Troubleshooting

```sql
-- Current rows (no digest, no secrets)
SELECT service_key, runtime_status, started_at, last_seen_at, update_count
FROM public.fc27_service_heartbeats
ORDER BY service_key;

-- Raw ages from the database clock
SELECT public.fc27_get_service_heartbeat_status();
```

Common causes of `UNKNOWN`: the runtime has not started since Migration 0026,
the heartbeat is disabled, the writer URI secret file is missing, or the
runtime role lacks EXECUTE (check Grants Policy 27).
