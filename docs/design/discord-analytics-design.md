# East Coasters FC 27 Discord Analytics Architecture Design

- **Status**: Recovered historical pre-implementation design; statements below describe that baseline, not the current checkout.
- **Audience**: Developers, Operators, Security Reviewers
- **Last verified commit (historical source)**: `86d79f9be9800920598c510bc95552692e8711ec`
- **Historical source references**: `src/fc27_search_scout.py`, `src/fc27_analyst.py`, `src/fc27_pandas_view.py`, `src/fc27_dashboard_queries.py`, `src/fc27_clubs_api_contracts.py`
- **Superseded by**: [Current Discord bot implementation and operations guide](../FC27_DISCORD_BOT.md)

This document defines the **design-only architecture** (`DISCORD_BOT_IMPLEMENTED=NO`) for future East Coasters FC 27 (`club_id="330323"`, `club_name="East Coasters"`, `platform="common-gen5"`, `game_version="FC27"`) Discord analytics delivery. No Discord bot is implemented, enabled, or executed in this phase.

---

## 1. Core Architectural Boundary: Single Acquisition Service

- **Single Acquisition Service Enforced (`SINGLE_ACQUISITION_SERVICE_ENFORCED=YES`)**: Only a single, operator-authorized acquisition service at the repository provider boundary may contact an external Clubs data source.
- **Zero Provider Calls from Discord Agents (`DISCORD_AGENTS_CALL_PROVIDER=NO`, `DISCORD_PROVIDER_CALLS=0`)**: Do **not** use a swarm where multiple Discord agents independently call EA endpoints. Discord agents have zero network authority toward `ea.com` or `proclubs.ea.com` and may consume only already-validated, normalized, persisted, and sanitized records from internal read-only APIs or PostgreSQL read models (`src/fc27_dashboard_queries.py`, `src/fc27_search_scout.py`, `src/fc27_analyst.py`, `src/fc27_pandas_view.py`).
- **Canonical Internal Format (`CANONICAL_INTERNAL_FORMAT=records`)**: All internal handoffs use normalized typed `records`. Raw provider responses (`raw`) are never persisted, cached, or emitted to Discord.

---

## 2. Bounded Discord Agent Roles

| Bounded Agent | Inputs | Responsibilities & Strict Prohibitions |
|---|---|---|
| **Discord Intake Agent** | Slash-command interaction metadata (`guild_id`, `channel_id`, `user_id`, command name) | Validates allowlisted guild, allowlisted channel, and allowlisted command; accepts no arbitrary provider URL; starts no live scrape or provider request directly. |
| **Match Summary Agent** | Sanitized match read models (`src/fc27_dashboard_queries.py`) | Reports latest sanitized East Coasters match summaries (`match_id`, `played_at`, `result`, `goals_for`, `goals_against`, opponent club name, team aggregates); never exposes source player IDs, gamer tags, or raw payloads. |
| **Form Analyst Agent** | `FC27SearchContext` and `FC27ScoutReport` (`src/fc27_search_scout.py`) | Reports team-level rolling form (`last_5`, `last_20`), observational metrics, and sample-size confidence tiers (`INSUFFICIENT_DATA` through `HIGH_CONFIDENCE`). |
| **Evidence Review Agent** | `FC27AnalystReport` (`src/fc27_analyst.py`) | Reports bounded observational findings together with mandatory counter-evidence, confounders, competing explanations, and data gaps. |
| **Export Agent** | Sanitized normalized records via `FC27PandasView` (`src/fc27_pandas_view.py`) | Produces sanitized `records`, CSV, or Excel exports on explicit operator command; enforces output-size and file-retention bounds; strips aliases and external IDs. |
| **Health Agent** | Sanitized provider/cache/circuit/ingestion status read model | Reports provider status, last successful ingestion timestamp, bounded cache state, circuit-breaker state (`CLOSED`/`OPEN`/`HALF_OPEN`), and polling state (`DISABLED`); exposes no secrets, tokens, or raw responses. |

---

## 3. Required Discord Safety & Governance Controls

1. **Allowlisted Guilds, Channels, and Commands**:
   - Commands are rejected fail-closed outside explicitly allowlisted `guild_id` and `channel_id` values.
   - Only fixed slash commands in the approved command surface are registered.
2. **Least-Privilege Bot Permissions (`NO_ADMIN_PERMISSION=YES`)**:
   - The bot must **never** request or hold the Discord `ADMINISTRATOR` permission, `MANAGE_GUILD`, `MANAGE_ROLES`, or `MANAGE_WEBHOOKS`.
   - The `MESSAGE_CONTENT` privileged intent is disabled unless explicitly required by a separately reviewed ADR; all interactions use structured slash commands.
3. **Zero Arbitrary Execution or Fetch**:
   - No arbitrary code execution, no arbitrary SQL queries, and no arbitrary URL fetching (`get_json` or user-supplied URLs prohibited).
   - No provider credentials, OAuth tokens, cookies, or session state may exist in the Discord service environment.
4. **Privacy, Anti-Surveillance, & Language Safety**:
   - No raw provider response output (`RAW_RESPONSE_OUTPUT=NO`).
   - No player aliases, gamer tags (`playername`, `proName`), or external player IDs (`playerId`, `personaId`, `blazeId`) by default; player-level exports use deterministic `plr_ecf_<12-hex>` pseudonyms only.
   - No personal best-to-worst player quality rankings (`NO_PERSONAL_PLAYER_RANKING=YES`).
   - No psychological, effort, morale, tilt, or causal tactical claims (`src/fc27_analyst.py` language validator enforced).
5. **Rate Limits, Audit Metadata, & Bounded Exports**:
   - Per-guild and per-user token-bucket rate limits on all commands and export requests.
   - Structured audit metadata recorded for every command invocation (`correlation_id`, `guild_id`, `channel_id`, `command`, `outcome`).
   - Explicit operator role authorization required for `/fc27 export *` commands.
   - Strict output-size bounds (`<= 256 KiB` per attachment, `<= 2,000` characters per message) and ephemeral file-retention bounds (`0` persistent disk files; generated in memory via `io.StringIO` / `io.BytesIO` and zeroed after response).
6. **Polling & Dispatch Dormancy**:
   - Continuous polling remains disabled (`POLLING_ENABLED=NO`).
   - Recommendation dispatch remains disabled (`DISPATCH_ENABLED=false`).

---

## 4. Recommended Future Command Surface

Only the following read-only slash commands are in scope for future implementation:

```text
/fc27 status
/fc27 latest
/fc27 form
/fc27 analysis
/fc27 data-gaps
/fc27 export records
/fc27 export csv
/fc27 export excel
```

Prohibited command categories (must never be added):
- Commands that trigger live provider scraping, browser navigation, or continuous polling.
- Commands that bypass Commissioner approval or trigger recommendation dispatch.
- Commands that query arbitrary club IDs outside East Coasters (`330323`, `common-gen5`, `FC27`).
