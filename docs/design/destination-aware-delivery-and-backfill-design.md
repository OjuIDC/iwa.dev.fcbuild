# Design Note: Destination-Aware Result Delivery, Multi-Channel Linkage, and Bounded Backfill

## 1. Context and Problem Statement

The FC27 match results subsystem ingests competitive Pro Clubs match data and publishes notification messages into Discord. Historically, notifications were delivered to a single hard-coded results destination and team-chat channel configured in the bot environment.

### 1.1 Limitations of the Current Architecture

1. **Single-Linkage / LIMIT 1 Model**:
   Durable message linkage is currently persisted inside `public.fc27_manual_match_audit` with `action = 'RELINK_DISCORD_MESSAGE'` and `audit_metadata->>'kind' = 'discord_message_linkage'`.
   Querying linkage (`_fetch_latest_linkage_row`) uses an `ORDER BY audit_id DESC LIMIT 1` pattern. It stores at most one `results_message_id` and one `team_chat_message_id`. If multiple channels or destinations receive deliveries for the same match, the most recent write overwrites the lookup, or earlier deliveries become orphaned.

2. **Destination Unawareness in Job Idempotency**:
   The dispatcher queue marks `post_new_results` jobs complete using `fc27:post_new_results:<match_ref>`. Once a match is published to the initial destination, the job is terminal. When a new channel (such as `#east-coasters-analytics` or `#pro-clubs`) is authorized, existing matches cannot be re-sent through normal ingestion without mutating completed jobs or creating duplicate deliveries.

3. **Inability to Fan Out to Multiple Distinct Channels**:
   The dispatcher currently assumes a single `results_channel_id` and single `team_chat_channel_id`. It lacks an additive destination registry that decouples delivery destinations from static environment variables.

4. **Incomplete Roster-Refresh Edit Coverage**:
   When a player alias is linked or updated, roster refresh re-renders historical match messages and edits them in-place. Because linkage lookup returns only the latest single row, prior messages in other authorized channels cannot be updated.

5. **Lack of Supported Historical Backfill**:
   No safe, bounded mechanism exists to backfill the latest real persisted matches into a newly authorized destination without calling the external EA provider, bypassing the normal renderer and transport, or executing unsafe ad-hoc SQL.

---

## 2. Architecture Overview

To resolve these blockers safely and without breaking existing behavior, we introduce:
1. **Destination Registry (`public.fc27_discord_destinations`)**: An additive, operator-controlled table registering symbolic destinations with specific guild IDs, channel IDs, message kinds, and delivery permissions.
2. **Per-Destination Delivery Linkage (`public.fc27_match_discord_deliveries`)**: A relational delivery table tracking every individual message delivery per match, destination, and message kind, with explicit delivery states and compound unique constraints.
3. **Additive Migration 0022 and Grants Policy 22**: An idempotent, non-destructive migration that creates the new tables, imports existing audit linkage into legacy entries, and sets least-privilege role permissions.
4. **Independent Delivery Fan-Out**: Dispatcher refactoring to deliver across all enabled destinations independently with per-destination idempotency keys and failure isolation.
5. **Multi-Destination Roster Refresh**: In-place editing across all linked destinations for affected matches, handling 404 (Missing) and 403 (Revoked) states without deletion or replacement.
6. **Bounded Result Backfill CLI (`src.fc27_result_backfill`)**: A single-flight CLI tool supporting dry-run by default, bounded batch sizes (default 10, max 25), oldest-to-newest ordering, and sanitized reporting.

---

## 3. Detailed Data Models and Schemas

### 3.1 Destination Registry Table (`public.fc27_discord_destinations`)

```sql
CREATE TABLE public.fc27_discord_destinations (
    destination_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    destination_key TEXT NOT NULL UNIQUE,
    guild_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    message_kind TEXT NOT NULL,
    transport TEXT NOT NULL DEFAULT 'DISCORD_BOT',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    allow_backfill BOOLEAN NOT NULL DEFAULT TRUE,
    allow_normal_delivery BOOLEAN NOT NULL DEFAULT TRUE,
    allow_roster_refresh BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT fc27_discord_dest_key_chk CHECK (destination_key ~ '^[a-z0-9_-]{1,64}$'),
    CONSTRAINT fc27_discord_dest_kind_chk CHECK (message_kind IN ('FORMAL_RESULT', 'TEAM_CHAT_SUMMARY')),
    CONSTRAINT fc27_discord_dest_transport_chk CHECK (transport IN ('DISCORD_BOT', 'WEBHOOK')),
    CONSTRAINT fc27_discord_dest_guild_chk CHECK (guild_id ~ '^[0-9]{1,30}$'),
    CONSTRAINT fc27_discord_dest_channel_chk CHECK (channel_id ~ '^[0-9]{1,30}$'),
    CONSTRAINT uq_fc27_discord_destinations_target UNIQUE (guild_id, channel_id, message_kind)
);
```

- **Symbolic `destination_key`**: Stable key (e.g., `east-coasters-formal`, `east-coasters-team-chat`, `legacy-formal`, `legacy-team-chat`).
- **Protected Numeric Snowflakes**: Guild and channel IDs are stored as validated snowflake text without hardcoding live production values in migration source.
- **Granular Feature Flags**: `allow_backfill`, `allow_normal_delivery`, `allow_roster_refresh` allow enabling or disabling specific operations independently per destination.

### 3.2 Per-Destination Delivery Linkage Table (`public.fc27_match_discord_deliveries`)

```sql
CREATE TABLE public.fc27_match_discord_deliveries (
    delivery_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    match_ref TEXT NOT NULL,
    destination_id BIGINT NOT NULL REFERENCES public.fc27_discord_destinations(destination_id) ON DELETE RESTRICT,
    destination_key TEXT NOT NULL,
    message_kind TEXT NOT NULL,
    guild_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    message_id TEXT,
    delivery_state TEXT NOT NULL DEFAULT 'PENDING',
    idempotency_key TEXT NOT NULL UNIQUE,
    source_job_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    attempted_at TIMESTAMPTZ,
    delivered_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    last_error_category TEXT,
    CONSTRAINT fc27_deliveries_kind_chk CHECK (message_kind IN ('FORMAL_RESULT', 'TEAM_CHAT_SUMMARY')),
    CONSTRAINT fc27_deliveries_state_chk CHECK (delivery_state IN ('PENDING', 'CLAIMED', 'DELIVERED', 'FAILED', 'REVOKED', 'MISSING')),
    CONSTRAINT fc27_deliveries_guild_chk CHECK (guild_id ~ '^[0-9]{1,30}$'),
    CONSTRAINT fc27_deliveries_channel_chk CHECK (channel_id ~ '^[0-9]{1,30}$'),
    CONSTRAINT fc27_deliveries_message_chk CHECK (message_id IS NULL OR message_id ~ '^[0-9]{1,30}$'),
    CONSTRAINT uq_fc27_match_discord_deliveries_match_dest UNIQUE (match_ref, destination_key, message_kind)
);
```

- **Delivery States**:
  - `PENDING`: Initial registration before dispatch attempt.
  - `CLAIMED`: Actively claimed by a dispatcher or backfill process.
  - `DELIVERED`: Confirmed delivered to Discord, with verified `message_id`.
  - `FAILED`: Terminal or retriable delivery failure with `last_error_category`.
  - `REVOKED`: Destination channel permissions revoked (HTTP 403). No repost allowed.
  - `MISSING`: Message was deleted or purged from Discord (HTTP 404). No repost allowed.
- **Uniqueness**:
  - `UNIQUE (match_ref, destination_key, message_kind)` prevents multiple sends of the same match to the same destination.
  - `UNIQUE (idempotency_key)` guarantees global idempotency across workers and retries.

---

## 4. Idempotency Model

Idempotency keys follow a strict deterministic format:

```
post-result:<match_ref>:<destination_key>:<message_kind>:v1
```

- **`match_ref`**: Canonical match identifier (e.g. EA match ID or manual match reference).
- **`destination_key`**: Stable symbolic destination name.
- **`message_kind`**: `FORMAL_RESULT` or `TEAM_CHAT_SUMMARY`.
- **`v1`**: Format version to accommodate future protocol evolutions.

### Delivery Lifecycle:
1. **Reservation**: `public.fc27_reserve_discord_delivery(...)` transitions state from `PENDING` to `CLAIMED` and sets `attempted_at`. If already `DELIVERED`, the function returns the existing `message_id` immediately.
2. **Rendering & Transport**: Standard message rendering (`build_post_new_results_notification` or `build_post_new_results_team_chat_message`) and transport sending (`send_notification`).
3. **Commit Success**: `public.fc27_record_discord_delivery_success(...)` transitions state to `DELIVERED` and persists `message_id` and `delivered_at`.
4. **Failure Recording**: `public.fc27_record_discord_delivery_failure(...)` records `FAILED` with sanitized error category.

---

## 5. Migration Strategy & Legacy Import (Migration 0022)

### 5.1 Reconciling Version Numbers
- The unmerged `feat/fc27-all-types-polling` branch uses Migration `0021` and Grants Policy `21`.
- To avoid collision and maintain clean sequencing, this branch designates **Migration `0022`** (`0022_fc27_destination_aware_delivery_and_linkage.sql`) and advances **Grants Policy to Version `22`**.

### 5.2 Legacy Linkage Import
Migration 0022 safely imports all existing records from `public.fc27_manual_match_audit` where `action = 'RELINK_DISCORD_MESSAGE'` and `audit_metadata->>'kind' = 'discord_message_linkage'` into `fc27_match_discord_deliveries` under placeholder destinations:
- `legacy-formal` (`guild_id = '0'`, `channel_id = '0'`, `enabled = FALSE`, `allow_backfill = FALSE`, `allow_roster_refresh = TRUE`)
- `legacy-team-chat` (`guild_id = '0'`, `channel_id = '0'`, `enabled = FALSE`, `allow_backfill = FALSE`, `allow_roster_refresh = TRUE`)

All existing audit records remain untouched for immutable audit trail compliance.

---

## 6. Least-Privilege Grants and Stored Procedures (Policy 22)

Access to destination and delivery tables is restricted according to the principle of least privilege:
- `fc27_owner`: Owns tables, constraints, indexes, and stored procedures.
- `fc27_dispatch`:
  - `SELECT` on `public.fc27_discord_destinations`.
  - `SELECT, INSERT, UPDATE` on `public.fc27_match_discord_deliveries`.
  - `EXECUTE` on `fc27_reserve_discord_delivery`, `fc27_record_discord_delivery_success`, `fc27_record_discord_delivery_failure`, `fc27_get_deliveries_for_refresh`, `fc27_record_delivery_refresh_state`.
- `fc27_match_results_writer`:
  - `SELECT` on `public.fc27_discord_destinations`.
  - `SELECT, INSERT, UPDATE` on `public.fc27_match_discord_deliveries`.
  - `EXECUTE` on delivery procedures and destination management.
- `fc27_discord_reader`:
  - `SELECT` on `public.fc27_discord_destinations`.
  - `SELECT` on `public.fc27_match_discord_deliveries`.
- `PUBLIC`, `fc27_app`, `fc27_poll_writer`, `fc27_relay`:
  - `REVOKE ALL` on tables and functions.

All procedures enforce `SET search_path = pg_catalog, pg_temp` and use fully qualified identifiers.

---

## 7. Normal Delivery Fan-Out (Phase 6)

During a `post_new_results` dispatch cycle:
1. Dispatcher queries all enabled destinations where `allow_normal_delivery = TRUE`.
2. For each enabled destination matching `FORMAL_RESULT`:
   - Delivery is reserved via `fc27_reserve_discord_delivery`.
   - If previously delivered, skip.
   - Message is rendered and sent.
   - Success is recorded. If send fails, failure is recorded without interrupting other destinations.
3. For each enabled destination matching `TEAM_CHAT_SUMMARY`:
   - Independent reservation, rendering, send, and recording.
4. **Backward Compatibility**: If no destinations are registered in `public.fc27_discord_destinations`, dispatcher falls back cleanly to legacy single-destination behavior (`results_channel_id` and `team_chat_channel_id`).

---

## 8. Roster-Refresh Compatibility (Phase 7)

During roster refresh (`refresh_roster_messages`):
1. For each affected match, query all deliveries from `public.fc27_match_discord_deliveries` (falling back to legacy audit if unmigrated).
2. For each delivery with `allow_roster_refresh = TRUE`:
   - Re-render formal or compact message with updated player alias.
   - Edit message in-place via `edit_notification` / `edit_team_chat_notification`.
   - If Discord returns **HTTP 404 (Not Found)**: Record delivery state `MISSING`. **Do NOT repost or delete.**
   - If Discord returns **HTTP 403 (Forbidden)**: Record delivery state `REVOKED`. **Do NOT repost or delete.**
   - Partial failures in one destination do not block edits in other destinations.

---

## 9. Bounded Historical Backfill CLI (Phase 8)

### CLI Entrypoint
```bash
python -m src.fc27_result_backfill --destination <destination-key> --latest 10 --dry-run
python -m src.fc27_result_backfill --destination <destination-key> --latest 10 --confirm
```

### Constraints & Invariants:
1. **Dry-Run by Default**: Unless `--confirm` is explicitly passed, the command executes in dry-run mode and writes nothing to PostgreSQL or Discord.
2. **Preconfigured Destination Only**: Requires an authorized `destination_key` configured in `public.fc27_discord_destinations` with `allow_backfill = TRUE`. Raw channel IDs are rejected.
3. **Bounded Match Window**: Default 10 matches, hard maximum 25 matches (`1 <= N <= 25`).
4. **Real Persisted Matches Only**: Queries `COMMUNITY_RESEARCH_DERIVED`, completed, non-quarantined matches.
5. **Oldest-to-Newest Delivery**: Chronological ordering (`ORDER BY played_at ASC`) ensures sensible Discord channel flow.
6. **Zero Provider Requests**: Re-uses persisted match data exclusively. Provider network calls remain strictly zero.
7. **Single-Flight Concurrency**: Protects execution with PostgreSQL advisory lock `pg_try_advisory_lock(hashtext('fc27_result_backfill'), hashtext(destination_key))`.
8. **Sanitized Output**: Never prints raw 17–20 digit snowflakes or secrets; logs counts, destination keys, and protected match references.

---

## 10. Privacy and Production Safety Controls

- **Zero Raw Discord IDs**: No 17–20 digit snowflake IDs in application logs, container logs, Git commit messages, or final reports.
- **Provider Boundary**: Discord bot and backfill CLI have zero network access or credentials for the EA provider.
- **Mentions Disabled**: All Discord messages sent by backfill or dispatcher enforce `allowed_mentions = {"parse": []}`.
- **Production Isolation**: This implementation is contained entirely within the isolated worktree `C:\Users\iwadev\dev\iwa.dev.fcbuild-destination-backfill`. Production containers and configuration remain completely untouched.
