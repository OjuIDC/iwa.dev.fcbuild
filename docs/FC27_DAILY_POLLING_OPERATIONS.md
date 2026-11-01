# FC27 12-Hour Polling Operations (`ONCE_EVERY_12_HOURS`)

## Overview

The East Coasters FC27 polling worker (`src/fc27_polling_worker.py`, `scripts/fc27_polling_worker.py`) operates on a fixed **12-hour polling schedule** (`POLL_INTERVAL_SECONDS=43200`, `POLLING_FREQUENCY=ONCE_EVERY_12_HOURS`).

## Schedule & Request Budget

| Parameter | Value |
| --- | --- |
| `POLL_INTERVAL_SECONDS` | `43200` (12 hours) |
| `POLLING_FREQUENCY` | `ONCE_EVERY_12_HOURS` |
| `MAX_PROVIDER_REQUESTS_PER_CYCLE` | `1` |
| `MAX_PROVIDER_REQUESTS_PER_24_HOURS` | `2` |
| `MAX_RETRIES_PER_CYCLE` | `0` |
| `CATCH_UP_BURSTS_ALLOWED` | `false` |
| `POLLING_ENABLED_DEFAULT` | `false` |

## Provider Window Continuity (`MAX_RESULTS=10`)

Because the upstream community provider (`fc-clubs-api-community-v1`) fetches a maximum of `10` league matches per cycle (`match_type=leagueMatch`), if East Coasters plays more than 10 league matches between two 12-hour polls, matches older than the latest 10 could fall outside the provider window.

`evaluate_provider_window_continuity(...)` compares the fetched match batch against the newest persisted match in PostgreSQL:
- If all 10 fetched matches are new and the oldest fetched match is newer than the newest previously persisted match, the worker sets `possible_history_gap=true` and `history_gap_reason=PROVIDER_WINDOW_ROLLED_OVER_BETWEEN_12H_POLLS` without issuing extra provider requests.
- Otherwise, `possible_history_gap=false` and `history_gap_reason=CONTINUOUS_WINDOW`.

## Durable Persistence & Recovery

- Every accepted normalized match is committed atomically in PostgreSQL before any downstream analytics (`Search -> Scout -> Analyst -> Coach -> Tactics -> Director`) executes.
- On startup, `recover_polling_state_from_postgres(...)` inspects `public.fc27_export_batches` and `public.fc27_matches` to reconstruct `next_eligible_at = last_persisted_poll_utc + 43200 seconds` and suppress catch-up bursts across container or host restarts.
- All timestamps are normalized in UTC (`datetime.now(timezone.utc)`) so local Daylight Saving Time transitions never trigger double-Run or catch-up bursts.

## Local Activation (`docker-compose.community-worker.yml`)

Continuous polling remains disabled by default in tracked files (`FC27_COMMUNITY_POLLING_ENABLED=false`). To run the single local worker via the opt-in Docker Compose file:

```powershell
docker compose -f docker-compose.yml -f docker-compose.community-worker.yml up -d fc27-community-worker
```
