# FC27 Discord Analytics & Manual Match Results Bot

## Overview

The East Coasters FC27 Discord bot (`src/fc27_discord_bot.py`, `src/fc27_match_results.py`, `scripts/fc27_discord_bot.py`) provides:
1. A strictly read-only slash-command interface over persisted, sanitized provider/export PostgreSQL match records (`public.fc27_matches`, `public.fc27_player_match_stats`) and deterministic analytical reports (`Search -> Scout -> Analyst -> Coach -> Tactics -> Director`).
2. A role-gated manual Match Results system (`source_classification = 'MANUAL_MATCH_RESULT'`) backed by isolated PostgreSQL 16 tables (`public.fc27_manual_matches`, `public.fc27_manual_match_players`, `public.fc27_manual_match_events`, `public.fc27_manual_match_audit` in `migrations/0013_fc27_manual_match_results.sql`) and a dedicated least-privilege writer role (`fc27_match_results_writer`).

## Security & Governance Invariants

- **Disabled by default**: `FC27_DISCORD_BOT_ENABLED=false`.
- **Zero provider authority**: `DISCORD_PROVIDER_REQUESTS = 0`; the bot cannot trigger live EA or community-provider calls.
- **Strict domain separation**:
  - Provider/import analytics commands use `GuardedDiscordReadOnlyConnection` (`DISCORD_DATABASE_WRITE_ACCESS = False`), enforcing `SELECT`-only access to `public.fc27_*` tables and blocking `public.checkpoints*`, `public.recommendation_approvals`, and `public.dispatch_outbox`.
  - Manual Match Results write commands (`/fc27 matchday`, `/fc27 result`, `/fc27 editresult`, `/fc27 deletematch`) execute exclusively through `GuardedMatchResultsWriterConnection` using the dedicated `fc27_match_results_writer` role (`FC27_MATCH_RESULTS_WRITER_URI_FILE`), which has zero privileges on `public.fc27_matches`, `public.fc27_player_match_stats`, poll state, approval, dispatch, outbox, or identity tables.
- **Role-ID write authorization**: Write commands require the caller's numeric Discord role IDs to intersect configured `FC27_DISCORD_MANAGER_ROLE_IDS`, `FC27_DISCORD_CAPTAIN_ROLE_IDS`, or `FC27_DISCORD_ADMIN_ROLE_IDS`. Role names are never trusted. Unauthorized write attempts return the exact ephemeral message `"You don't have permission."` and append an `UNAUTHORIZED_WRITE_ATTEMPT` record to `public.fc27_manual_match_audit`.
- **Allowlist fail-closed**: Requires explicit positive numeric `FC27_DISCORD_ALLOWED_GUILD_IDS` and `FC27_DISCORD_ALLOWED_CHANNEL_IDS`. Direct messages and non-slash freeform messages are rejected.
- **Least-privilege permissions**: Never requests `Administrator` or `message_content` intent; uses only `View Channels`, `Send Messages`, `Embed Links`, `Attach Files`, and `Use Application Commands`.
- **Secret-file token loading**: Reads `DISCORD_BOT_TOKEN_FILE` (`/run/secrets/discord_bot_token`) or `DISCORD_BOT_TOKEN` via `read_secret(...)` and scrubs tokens from `repr()` and CLI logs.
- **Privacy & safe formatting**: Provider analytics emit only `plr_ecf_<12-lowercase-hex>` references. Manual Match Results store only operator-entered club display labels (never Xbox account IDs, XUIDs, or verified provider identities) and render missing optional values as `"N/A"`. All outbound Discord payloads are verified by `assert_discord_output_sanitized(...)` to block `None`, `null`, `undefined`, `NaN`, tracebacks, and connection strings.
- **Immutable advisory-report caching (`src/fc27_advisory_cache.py`)**: Read-only advisory commands (`/fc27 analysis`, `/fc27 data-gaps`, `/fc27 coach`, `/fc27 tactics`, `/fc27 director`) resolve their six-stage `Search -> Scout -> Analyst -> Coach -> Tactics -> Director` bundle through `FC27AdvisoryCache` keyed by the persisted PostgreSQL batch digest (`fetch_persisted_advisory_cache_key`), serving warm responses in `< 100 ms` p95 while preserving outbound `assert_discord_output_sanitized(...)` verification on every response. See [`docs/FC27_ADVISORY_CACHE.md`](FC27_ADVISORY_CACHE.md).

## Manual Match Results Commands (9 Commands)

1. `/fc27 matchday` — Create a `SCHEDULED` manual match fixture (`opponent`, `date` `YYYY-MM-DD`, `kickoff_time` `HH:MM`, optional `notes`) and post/link the Matchday card.
2. `/fc27 result` — Record a `FULL_TIME` manual match result (`opponent`, `home_score`, `away_score`, optional `scorers`, `motm`, `possession`, `shots`, `shots_on_target`, `passes`, `pass_accuracy`, `corners`, `tackles`, `fouls`, `player_stats`, `notes`, `match_id`). Edits an existing linked Matchday card in place when `match_id` is supplied.
3. `/fc27 editresult` — Partially update an existing manual match (`match_id`, optional score/stat/scorer/player_stat fields, optional `expected_revision` optimistic concurrency check) and edit the linked Discord message in place (or post a replacement card and audit `RELINK_DISCORD_MESSAGE` if the original Discord message was deleted).
4. `/fc27 lastresult` — Display the most recent non-deleted `FULL_TIME` manual match card (`No match results recorded yet.` when empty).
5. `/fc27 match` — Display a specific non-deleted manual match card by `match_id`.
6. `/fc27 results` — Paginated list (5 per page) of non-deleted `FULL_TIME` manual matches with interactive `Previous` / `Next` buttons (`MatchResultsPaginationView`).
7. `/fc27 topscorers` — Leaderboard of top 10 manual goal scorers (`goals > 0`, ordered by `goals DESC, assists DESC, normalized_label ASC`).
8. `/fc27 playerstats` — Aggregated manual match stats (`matches_played`, `goals`, `assists`, `tackles`, `passes`, `avg_pass_accuracy`, `shots`, `motm_count`) for a player display label.
9. `/fc27 deletematch` — Soft-delete a manual match (`deleted_at` timestamp + `SOFT_DELETE_MATCH` audit record) with confirmation prompt (`MatchDeleteConfirmationView` timeout disables buttons without deleting).

### Deterministic Scorer & Player-Stat Input Syntax

- **Scorers (`scorers`)**: Comma-separated entries supporting `Player x2, Other` or `Player (2), Other (1)` (default `1` goal when count omitted). Duplicate normalized player labels and scorer goal totals exceeding `home_score` are rejected.
- **Player stats (`player_stats`)**: Semicolon-separated player clauses with comma-separated `<value> <metric>` pairs:
  `Lao: 2 goals, 1 assist, 4 tackles, 32 passes, 84 pass_accuracy; Jay: 1 goal, 2 assists`
  Supported metrics: `goals`, `assists`, `tackles`, `passes`, `pass_accuracy` (`0..100`), `shots`, `rating` (`0.0..10.0`). Reconciled deterministically with `scorers`.

## Provider Analytics `/fc27` Slash Commands (13 Commands + Aliases)

1. `/fc27 status` — Provider, 12-hour polling, and database match count summary
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
