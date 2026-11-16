# FC27 Discord Notification Dispatcher

## Overview

The FC27 Discord Notification Dispatcher runs as an opt-in background asyncio loop inside the existing `fc27-discord-bot` service (`src/fc27_discord_bot.py` and `src/fc27_discord_dispatcher.py`). It uses the existing bot client and token with zero additional provider authority (`DISCORD_PROVIDER_REQUESTS=0`).

Pipeline flow for `job_type = 'post_new_results'`:

```text
public.fc27_discord_jobs (pending)
  -> acquire single-flight advisory lock (optional, enabled by default)
  -> recover expired leases & fail exhausted retries
  -> claim lease (FOR UPDATE SKIP LOCKED, fc27_player_dev_writer)
  -> validate payload & load authoritative match/stats (fc27_discord_reader)
  -> verify no active quarantine or quality block
  -> build sanitized Discord message (mref_ecf_*/plr_ecf_* only, AllowedMentions.none)
  -> pre-send duplicate check on retry (ref:<match_ref>)
  -> send once to FC27_DISCORD_RESULTS_CHANNEL_ID
  -> mark job completed (fc27_player_dev_writer)
```

Pipeline flow for `job_type = 'refresh_roster_messages'` (Migration `0018`, Grants Policy `19` — Finding 8: `IMPLEMENTED IN PR, NOT ACTIVE` until Migration `0018` is deployed/activated):

```text
upsert_roster_player / rename_roster_player (fc27_match_results_writer, same DB transaction)
  -> SELECT public.fc27_enqueue_roster_refresh_job(player_id, roster_revision, reason_code, requested_at)
     [SECURITY DEFINER owned by fc27_owner, SET search_path = pg_catalog, pg_temp; REVOKE ALL FROM PUBLIC]
  -> inserts pending 'refresh_roster_messages' job with deterministic idempotency_key
     roster-refresh:<player_id>:<roster_revision>:v1
  -> dispatcher claims lease (fc27_player_dev_writer, SKIP LOCKED, SINGLE_FLIGHT=true, LEASE_MINUTES=5, MAX_JOBS_PER_CYCLE=5)
  -> validates payload & resolves current canonical roster + linked provider/manual matches (fc27_discord_reader)
  -> bounds work per job (max matches/edits/history scans), enqueueing deterministic continuation if needed
  -> fetches existing Discord snapshots, checks bot authorship & allowlisted channels/guilds, compares payloads
  -> edits in place ONLY when changed (AllowedMentions.none); never reposts deleted messages; persists unambiguous recovery
  -> completes job (or retries transient 429/5xx/DB serialization vs dead-lettering permanent failures)
```

## Database Role & Security Boundaries

The dispatcher preserves full least-privilege role separation without relaxing slash-command read-only posture or adding database migrations:

1. **Authoritative Match & Stats Reads (`fc27_discord_reader`)**
   - Wrapped by `GuardedDiscordReadOnlyConnection` (`SELECT`-only).
   - Queries `public.fc27_matches`, `public.fc27_export_batches`, `public.fc27_quarantine_records`, and `public.fc27_player_match_stats` (explicitly excluding `display_alias`).
   - Enforces `validate_fc27_role_uri_separation` so reader and writer credentials cannot share roles or URIs.

2. **Job Lifecycle Updates (`fc27_player_dev_writer`)**
   - Wrapped by `GuardedDiscordJobQueueConnection`, which verifies `current_user == 'fc27_player_dev_writer'` and non-superuser/non-owner posture.
   - Uses existing column-scoped `UPDATE` grants on `public.fc27_discord_jobs` (`state`, `claim_owner`, `claimed_at`, `lease_expires_at`, `attempt_count`, `last_error_category`, `available_at`, `completed_at`).
   - Rejects `INSERT`, `DELETE`, DDL/DCL, updates to immutable columns (`job_type`, `payload`, `idempotency_key`, `created_at`), and any access to match/stat/quarantine/identity/governance tables.

3. **Prohibited Credentials**
   - Startup fails closed if `FC27_POLL_WRITER_URI`, `FC27_POLL_WRITER_URI_FILE`, `FC27_ALIAS_HMAC_KEY`, or `FC27_ALIAS_HMAC_KEY_FILE` is exposed to the Discord bot container.

## Configuration

All dispatcher gates are defined in `src/settings.py`, `docker-compose.discord.yml`, and `config.example.env`, and fail closed by default:

| Environment Variable | Default | Allowed Range / Format | Behavior |
| --- | --- | --- | --- |
| `FC27_DISCORD_DISPATCH_ENABLED` | `false` | boolean | Enables background dispatcher loop inside `fc27-discord-bot`. |
| `FC27_DISCORD_RESULTS_CHANNEL_ID` | unset | positive numeric Discord snowflake | Target channel for match notifications. Required when dispatch is enabled. |
| `FC27_DISCORD_MAX_JOBS_PER_CYCLE` | `5` | `1..25` | Maximum `post_new_results` jobs leased per cycle. |
| `FC27_DISCORD_LEASE_MINUTES` | `5` | `1..60` | Lease duration (`lease_expires_at = clock_timestamp() + make_interval(...)`). |
| `FC27_DISCORD_SINGLE_FLIGHT` | `true` | boolean | Acquires PostgreSQL session advisory lock (`pg_try_advisory_lock`) per cycle. |

## Job Types, Quarantine, and Failure Handling

- **Permitted Job Types:** Strictly `post_new_results` and `refresh_roster_messages`. Unimplemented or unknown `job_type` values (such as `evaluate_suggestions`) are left untouched in `pending` state (`attempt_count` unchanged) and emitted as `SKIPPED_UNKNOWN_JOB_TYPE` audit entries.
- **Least-Privilege Roster-Refresh Producer:** `fc27_match_results_writer` continues to have `REVOKE ALL` on `public.fc27_discord_jobs` and its sequence. Roster changes enqueue `refresh_roster_messages` exclusively through `public.fc27_enqueue_roster_refresh_job(...)` (`SECURITY DEFINER`, owned by `fc27_owner`, `SET search_path = pg_catalog, pg_temp`, `EXECUTE` granted only to `fc27_match_results_writer`, `PUBLIC` revoked), strictly inside the roster-update transaction.
- **Quarantine & Replay Protection:** Any job referencing a missing match, `data_quality_status = 'REJECTED'`, or an active `PENDING_REVIEW` / `REJECTED_PERMANENT` row in `public.fc27_quarantine_records` (including `REPLAY_CONFLICT`) fails closed without sending any Discord notification. Quarantine matching compares both the raw `external_match_id` and the pseudonymized job `match_ref`, because worker-generated replay quarantines store the pseudonymized reference.
- **Transient Failures vs Terminal Failures:**
  - Discord HTTP `429` (`DISCORD_RATE_LIMITED`), `5xx` / timeouts (`DISCORD_TRANSIENT_FAILURE`), DB serialization/deadlocks (`DB_TRANSIENT_SERIALIZATION`), and history lookup failures or timeouts (`DISCORD_HISTORY_*`) release the job back to `pending` with bounded `available_at` backoff until `attempt_count` reaches `10`.
  - Channel permission/missing errors (`DISCORD_CHANNEL_FORBIDDEN`, `DISCORD_CHANNEL_NOT_FOUND`), unauthorized destinations (`UNAUTHORIZED_DESTINATION`), non-bot-authored target messages (`NON_BOT_AUTHORED_MESSAGE`), missing/deleted linked messages under no-repost policy (`LINKED_MESSAGE_MISSING`), unknown canonical players (`UNKNOWN_ROSTER_PLAYER`), bad requests (`DISCORD_PAYLOAD_REJECTED`), malformed payloads, invalid revisions, privacy violations, or reaching `attempt_count >= 10` mark the row `failed` with a sanitized `last_error_category`.
- **Bounded Refresh & Continuation:** Each `refresh_roster_messages` job examines at most `refresh_max_matches_per_job` (default `10`), `refresh_max_formal_edits_per_job` (default `10`), `refresh_max_team_chat_edits_per_job` (default `10`), and `refresh_max_history_scans_per_job` (default `5`), handing off remaining matches via a deterministic `CONTINUATION` job (`roster-refresh:<player_id>:<roster_revision>:cont:<n>:v1`, bounded depth `<= 20`) instead of looping without limit.
- **Crash & Restart Idempotency:** Expired `claimed` leases (`lease_expires_at <= clock_timestamp()`) are recovered cleanly on the next cycle. On any retried attempt (`attempt_count > 1` or prior `LEASE_EXPIRED`), the dispatcher paginates bot message history in `FC27_DISCORD_RESULTS_CHANNEL_ID` back to the job's `created_at` (not a fixed latest-message window) looking for `ref:<match_ref>` before calling `channel.send`; if already posted, the job is completed without sending a duplicate message. For `refresh_roster_messages`, existing Discord message payloads are fetched and diffed before calling edit, skipped safely if recovery finds multiple ambiguous candidates, and never replaced on `NotFound`.
- **Lease Coverage:** Each claim is renewed and re-verified immediately before history scanning and again immediately before publishing or editing. A claim that was recovered or reassigned in the meantime is skipped as `LEASE_LOST` and never used to send or edit. Timed-out Discord operations are cancelled and settled before the job is released for retry.

## Privacy & Sanitization

- Outgoing notifications emit only pseudonymous `mref_ecf_<12hex>` and `plr_ecf_<12hex>` identifiers.
- `display_alias`, raw `external_match_id`, database UUIDs (`match_id`), `export_id`, numeric `club_id`, database URIs, and tokens are excluded and checked by `assert_discord_output_sanitized`.
- Discord mentions (`@everyone`, `@here`, user/role mentions) are neutralized and `discord.AllowedMentions.none()` is enforced on every channel send.

## Activation & Rollback Procedure

Finding 8 (`refresh_roster_messages`) status: **IMPLEMENTED IN PR, NOT ACTIVE** in production until separately approved and migrated (`Migration 0018`, `Grants Policy 19`).

When operator approval is granted to deliver queued `post_new_results` and `refresh_roster_messages` notifications:

1. Run `python -m src.db_migrate` to apply Migration `0018` (`fc27_discord_jobs_job_type_chk` + `public.fc27_enqueue_roster_refresh_job`) and Grants Policy `19` (`EXECUTE` granted solely to `fc27_match_results_writer`, `PUBLIC` revoked).
2. Verify `FC27_DISCORD_RESULTS_CHANNEL_ID` (and optional `FC27_DISCORD_TEAM_CHAT_CHANNEL_ID`) is set and allowlisted, and the bot holds `View Channel`, `Send Messages`, and `Read Message History` permissions in the allowlisted channel(s).
3. Confirm `/run/secrets/fc27_player_dev_writer_uri` and `/run/secrets/fc27_discord_reader_uri` (or `fc27_app_uri`) are mounted and healthy.
4. Set `FC27_DISCORD_DISPATCH_ENABLED=true` in the runtime Discord environment and recreate/restart `fc27-discord-bot`.
5. Confirm startup logs emit `DISCORD_DISPATCH_ENABLED=YES`, `DISCORD_PROVIDER_REQUESTS=0`, and `public.fc27_discord_jobs` transitions queued `post_new_results` and `refresh_roster_messages` rows to `completed`.
6. To disable or roll back roster-refresh dispatching immediately without touching matches or polling state:
   - Set `FC27_DISCORD_DISPATCH_ENABLED=false` and recreate/restart `fc27-discord-bot`, OR deploy a previous image (`PERMITTED_DISPATCH_JOB_TYPES` restricted to `post_new_results`), which leaves any pending `refresh_roster_messages` rows untouched in PostgreSQL (`fc27_discord_jobs` constraint `0018` and completed rows remain safely backward-compatible).
