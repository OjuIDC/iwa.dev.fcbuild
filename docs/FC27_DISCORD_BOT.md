# FC27 Read-Only Discord Analytics Bot

## Overview

The East Coasters FC27 Discord bot (`src/fc27_discord_bot.py`, `scripts/fc27_discord_bot.py`) provides a strictly read-only slash-command interface over persisted, sanitized PostgreSQL match records and deterministic analytical reports (`Search -> Scout -> Analyst -> Coach -> Tactics -> Director`).

## Security & Governance Invariants

- **Disabled by default**: `FC27_DISCORD_BOT_ENABLED=false`.
- **Zero provider authority**: `DISCORD_PROVIDER_REQUESTS = 0`; the bot cannot trigger live EA or community-provider calls.
- **Zero database write authority**: `DISCORD_DATABASE_WRITE_ACCESS = False`; `GuardedDiscordReadOnlyConnection` enforces `SELECT`-only access to `public.fc27_*` tables and blocks `public.checkpoints*`, `public.recommendation_approvals`, and `public.dispatch_outbox`.
- **Allowlist fail-closed**: Requires explicit positive numeric `FC27_DISCORD_ALLOWED_GUILD_IDS` and `FC27_DISCORD_ALLOWED_CHANNEL_IDS`. Direct messages and non-slash freeform messages are rejected.
- **Least-privilege permissions**: Never requests `Administrator` or `message_content` intent; uses only `View Channels`, `Send Messages`, `Embed Links`, `Attach Files`, and `Use Application Commands`.
- **Secret-file token loading**: Reads `DISCORD_BOT_TOKEN_FILE` (`/run/secrets/discord_bot_token`) or `DISCORD_BOT_TOKEN` via `read_secret(...)` and scrubs tokens from `repr()` and CLI logs.
- **Pseudonymous identity only**: Emits only `plr_ecf_<12-lowercase-hex>` references; gamer tags, raw player names, and raw event strings (`1:2871893|2:0|3:8650754`) are blocked by `assert_discord_output_sanitized(...)`.

## Supported `/fc27` Slash Commands (13 Commands)

1. `/fc27 status` — Provider, 12-hour polling, and database match count summary
2. `/fc27 latest` — Paginated sanitized match summaries
3. `/fc27 form` — Team form, sample size, and confidence tier
4. `/fc27 analysis` — Analyst findings, counter-evidence, confounders, and data gaps
5. `/fc27 data-gaps` — Data gaps and unresolved questions
6. `/fc27 polling` — 12-hour polling schedule (`ONCE_EVERY_12_HOURS`, `43200s`) and provider window continuity status
7. `/fc27 coach` — Observational Coach hypotheses (`recommendation=false`)
8. `/fc27 tactics` — Experimental Tactics candidates (`recommendation=false`, `approvable=false`)
9. `/fc27 director` — Director evidence reconciliation (`recommendation=false`, `dispatchable=false`)
10. `/fc27 export-records` — Bounded sanitized JSON export attachment
11. `/fc27 export-csv` — Bounded sanitized CSV export attachment
12. `/fc27 export-excel` — Bounded sanitized Excel XML export attachment
13. `/fc27 help` — Command reference and safety posture
