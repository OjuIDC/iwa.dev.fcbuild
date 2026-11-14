# FC27 Discord Analytics & Manual Match Results Bot

## Overview

The East Coasters FC27 Discord bot (`src/fc27_discord_bot.py`, `src/fc27_match_results.py`, `src/fc27_discord_dispatcher.py`, `scripts/fc27_discord_bot.py`) provides:
1. A strictly read-only slash-command interface over persisted, sanitized provider/export PostgreSQL match records (`public.fc27_matches`, `public.fc27_player_match_stats`) and deterministic analytical reports (`Search -> Scout -> Analyst -> Coach -> Tactics -> Director`).
2. A role-gated manual Match Results and Club Roster system (`source_classification = 'MANUAL_MATCH_RESULT'`) backed by isolated PostgreSQL 16 tables (`public.fc27_roster`, `public.fc27_manual_matches`, `public.fc27_manual_match_players`, `public.fc27_manual_match_events`, `public.fc27_manual_match_audit` in `migrations/0013_fc27_manual_match_results.sql` and `migrations/0016_fc27_canonical_roster_and_legacy_matches.sql`, Grants Policy Version `17`) and a dedicated least-privilege writer role (`fc27_match_results_writer`).
3. Canonical result cards (`build_result_content` + `build_result_embed`) shared by slash commands and continuous `post_new_results` worker notifications, resolving pseudonymized `plr_ecf_*` references against `public.fc27_roster` in a single batch query with `"Unnamed Player"` fallback and zero internal jargon.

## Security & Governance Invariants

- **Disabled by default**: `FC27_DISCORD_BOT_ENABLED=false`.
- **Required dependency**: `discord.py==2.7.1` is a core runtime dependency declared in `requirements.txt` and `pyproject.toml` (`[project].dependencies`), and is installed in `Dockerfile.discord`. The bot service remains opt-in behind `FC27_DISCORD_BOT_ENABLED` and the `fc27-discord` Compose profile.
- **Zero provider authority**: `DISCORD_PROVIDER_REQUESTS = 0`; the bot cannot trigger live EA or community-provider calls.
- **Strict domain separation**:
  - Provider/import analytics and roster read commands use `GuardedDiscordReadOnlyConnection` (`DISCORD_DATABASE_WRITE_ACCESS = False`), enforcing `SELECT`-only access to allowlisted `public.fc27_*` tables (including `public.fc27_roster`) and blocking `public.checkpoints*`, `public.recommendation_approvals`, and `public.dispatch_outbox`. `docker-compose.discord.yml` also wires the dedicated `SELECT`-only `fc27_discord_reader` role (`FC27_DISCORD_READER_URI_FILE`) and the dedicated player-development writer role `fc27_player_dev_writer` (`FC27_PLAYER_DEV_WRITER_URI_FILE` for `migrations/0014_fc27_player_development.sql` tables).
  - Manual Match Results & Roster write commands (`/setplayer`, `/renameplayer`, `/matchday`, `/result`, `/editresult`, `/deletematch` and their `/fc27 ...` equivalents) execute exclusively through `GuardedMatchResultsWriterConnection` using the dedicated `fc27_match_results_writer` role (`FC27_MATCH_RESULTS_WRITER_URI_FILE`), which has zero privileges on `public.fc27_matches`, `public.fc27_player_match_stats`, poll state, approval, dispatch, outbox, or identity tables.
- **Role-ID write authorization**:
  - Roster and match write commands (`/setplayer`, `/renameplayer`, `/matchday`, `/result`, `/editresult`) require the caller's numeric Discord role IDs to intersect configured `FC27_DISCORD_MANAGER_ROLE_IDS`, `FC27_DISCORD_CAPTAIN_ROLE_IDS`, or `FC27_DISCORD_ADMIN_ROLE_IDS`.
  - `/deletematch` is **Admin-only**: requires either configured `FC27_DISCORD_ADMIN_ROLE_IDS` or Discord guild `Administrator` permission plus explicit confirmation (`confirm_delete=True`).
  - Role names are never trusted. Unauthorized write attempts return the exact ephemeral message `"You don't have permission."` and append an `UNAUTHORIZED_WRITE_ATTEMPT` record to `public.fc27_manual_match_audit`.
- **Allowlist fail-closed**: Requires explicit positive numeric `FC27_DISCORD_ALLOWED_GUILD_IDS` and `FC27_DISCORD_ALLOWED_CHANNEL_IDS`. Direct messages and non-slash freeform messages are rejected.
- **Least-privilege permissions**: Never requests `Administrator` or `message_content` intent; uses only `View Channels`, `Send Messages`, `Embed Links`, `Attach Files`, and `Use Application Commands`.
- **Secret-file token loading**: Reads `DISCORD_BOT_TOKEN_FILE` (`/run/secrets/discord_bot_token`) or `DISCORD_BOT_TOKEN` via `read_secret(...)` and scrubs tokens from `repr()` and CLI logs.
- **Canonical privacy & safe formatting**: Canonical Match Result cards (`/result`, `/editresult`, `/lastresult`, `/match`, and dispatcher `post_new_results` messages) clean opponent names (`clean_opponent`), map EA match types (`leagueMatch` -> `League Match`, `playoffMatch` -> `Playoff Match`, `friendlyMatch` -> `Friendly Match`), apply outcome colors (green win, red loss, gray draw, electric blue matchday), omit missing optional fields rather than printing placeholders, split large player rosters cleanly across `👥 Players` and `👥 Players (cont.)`, and never leak internal identifiers or jargon (`mref_ecf_`, `plr_ecf_`, `ref:`, `UNKNOWN`, `None`, `null`, `N/A`, `leagueMatch`, `COMMUNITY_RESEARCH_DERIVED`, `VERIFIED`, `COMPLETED`, `Match Reference`, `Source & Quality`, `Provenance`). All outbound Discord payloads are verified by `assert_discord_output_sanitized(...)`.
- **Immutable advisory-report caching (`src/fc27_advisory_cache.py`)**: Read-only advisory commands (`/fc27 analysis`, `/fc27 data-gaps`, `/fc27 coach`, `/fc27 tactics`, `/fc27 director`) resolve their six-stage `Search -> Scout -> Analyst -> Coach -> Tactics -> Director` bundle through `FC27AdvisoryCache` keyed by the persisted PostgreSQL batch digest (`fetch_persisted_advisory_cache_key`), serving warm responses in `< 100 ms` p95 while preserving outbound `assert_discord_output_sanitized(...)` verification on every response. See [`docs/FC27_ADVISORY_CACHE.md`](FC27_ADVISORY_CACHE.md).

## Canonical Match Results & Club Roster Commands (12 Commands)

All 12 commands are registered both as **top-level slash commands** (e.g., `/result`) and under `/fc27 <subcommand>` (e.g., `/fc27 result`):

1. `/setplayer` (`/fc27 setplayer`) — Register or update a canonical club roster player mapping (`player_id`, `name`, optional `position`, optional `discord_user_id`) in `public.fc27_roster` (Manager/Captain/Admin).
2. `/roster` (`/fc27 roster`) — Display active club roster players without exposing internal `plr_ecf_` identifiers.
3. `/renameplayer` (`/fc27 renameplayer`) — Rename an existing active roster player by `player_id` or current name; updated display names propagate across `/roster`, `/match`, `/lastresult`, `/topscorers`, and `/playerstats` (Manager/Captain/Admin).
4. `/matchday` (`/fc27 matchday`) — Create a `SCHEDULED` manual match fixture (`opponent`, `date`, optional `time` defaulting to `8PM EST`, optional `competition` defaulting to `League Match`, optional `formation`, optional `notes`) with `interaction_id` idempotency, and post/link the Matchday card (Manager/Captain/Admin).
5. `/result` (`/fc27 result`) — Record a `FULL_TIME` manual match result (`opponent`, `our_score`, `opponent_score`, optional `competition` defaulting to `League Match`, optional `formation`, `scorers`, `motm`, `possession`, `shots`, `shots_on_target`, `passes`, `pass_accuracy`, `corners`, `tackles`, `fouls`, `player_stats`, `match_id`) with `interaction_id` idempotency. Edits an existing linked Matchday card in place when `match_id` is supplied (Manager/Captain/Admin).
6. `/editresult` (`/fc27 editresult`) — Partially update an existing manual match (`match_id`, optional score/competition/formation/stat/scorer/player_stat fields, optional `expected_revision` optimistic concurrency check) and edit the linked Discord message in place (Manager/Captain/Admin).
7. `/lastresult` (`/fc27 lastresult`) — Display the most recent non-deleted `FULL_TIME` manual match card with optional `opponent` filter.
8. `/match` (`/fc27 match`) — Display a specific non-deleted manual match card by numeric `match_id` (or `#ID`), migrated `legacy_ref`, or `opponent`. Missing numeric matches return `"Match #{match_id} not found. Use /results to list matches."`.
9. `/results` (`/fc27 results`) — Paginated list (5 per page default, 10 max) of non-deleted manual matches with optional `opponent` filter and interactive `Previous` / `Next` buttons (`MatchResultsPaginationView`).
10. `/topscorers` (`/fc27 topscorers`) — Leaderboard of top manual goal scorers (default limit `5`, capped at `10`, ordered by `goals DESC, assists DESC, label ASC`) with roster-resolved display names.
11. `/playerstats` (`/fc27 playerstats`) — Aggregated manual match stats (`matches_played`, `goals`, `assists`, `tackles`, `passes`, `avg_pass_accuracy`, `shots`, `motm_count`) for a roster or manual player display label.
12. `/deletematch` (`/fc27 deletematch`) — Soft-delete a manual match (`deleted_at` timestamp + `SOFT_DELETE_MATCH` audit record) with explicit confirmation (`confirm_delete=True` or `MatchDeleteConfirmationView`). **Admin-only**.

### Deterministic Scorer & Player-Stat Input Syntax

- **Scorers (`scorers`)**: Comma-separated entries supporting `Player x2, Other` or `Player (2), Other (1)` (default `1` goal when count omitted). Duplicate normalized player labels and scorer goal totals exceeding `home_score` are rejected.
- **Player stats (`player_stats`)**: Semicolon-separated player clauses with comma-separated `<value> <metric>` pairs:
  `Lao: 2 goals, 1 assist, 4 tackles, 32 passes, 84 pass_accuracy; Jay: 1 goal, 2 assists`
  Supported metrics: `goals`, `assists`, `tackles`, `passes`, `pass_accuracy` (`0..100`), `shots`, `rating` (`0.0..10.0`). Reconciled deterministically with `scorers`.

## Provider Analytics `/fc27` Slash Commands (13 Commands + Aliases)

1. `/fc27 status` — Comprehensive read-only status summary (moved from the former `/demo/status` endpoint in Stage B4) covering club (`East Coasters`, `330323`, `common-gen5`, `FC27`), provider (`fc-clubs-api-community-v1`), 12-hour polling policy and multi-match-type rotation (`ONCE_EVERY_12_HOURS`, `43200s`, max 2 requests/24h, `max_requests=1`, `max_retries=0`, single-flight, analytics-on-new-only, `approved_match_types`, `current_rotation_match_type`, `next_rotation_match_type`, `last_attempted_match_type`, `last_successful_match_type`), runtime health (`polling_enabled`, `worker_running`, `last_status`, `last_attempt_at`, `last_success_at`, `last_new_match_at`, `next_eligible_at`, `consecutive_failures`, `circuit_state`, `total_cycles`, `total_requests`, `total_matches_inserted`, `total_duplicates_ignored`, `total_replay_conflicts`, `alias_hmac_key_configured`, `fingerprints_inserted`, `fingerprints_refreshed`, `fingerprint_collisions`, `discord_jobs_inserted`, `discord_jobs_existing`, `discord_job_conflicts`, `pending_discord_jobs_count`), window Continuity (`continuity_status`, `window_max_results`, `previous_latest_match_id_seen`, `latest_match_id_in_window`, `overlap_count_with_stored_matches`), repository availability (`database_available`, `persisted_match_count`, `latest_match_timestamp`, `quarantine_count`), manual Match Results availability (`manual_match_results_available`, `manual_match_count`), and safety gates (`DISCORD_PROVIDER_REQUESTS=0`, `DISCORD_DATABASE_WRITE_ACCESS=False`, `RECOMMENDATIONS=DISABLED`, `DISPATCH=DISABLED`). Missing timestamps or IDs render as `N/A`.
2. `/fc27 latest` (aliases `/fc27 recent`, `/fc27 last5`) — Paginated sanitized match summaries
3. `/fc27 form` (alias `/fc27 summary`) — Team form, sample size, and confidence tier
4. `/fc27 analysis` — Analyst findings, counter-evidence, confounders, and data gaps
5. `/fc27 data-gaps` — Data gaps and unresolved questions
6. `/fc27 polling` — 12-hour polling schedule (`ONCE_EVERY_12_HOURS`, `43200s`) and provider window continuity status
7. `/fc27 coach` — Observational Coach hypotheses (`recommendation=false`)
8. `/fc27 tactics` — Experimental Tactics candidates (`recommendation=false`, `approvable=false`)
9. `/fc27 director` — Director evidence reconciliation (`recommendation=false`, `dispatchable=false`)
10. `/fc27 export-records` (alias `/fc27 export`) — Bounded sanitized JSON export attachment
11. `/fc27 export-csv` — Bounded sanitized CSV export attachment
12. `/fc27 export-excel` — Bounded sanitized Excel XML export attachment
13. `/fc27 help` (plus `/fc27 club`, `/fc27 extremes`, `/fc27 players`, `/fc27 squad`) — Command reference and safety posture
