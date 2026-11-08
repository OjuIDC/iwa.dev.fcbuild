# FC 27 PostgreSQL Data Model & Privacy Schema

- **Status**: Current
- **Audience**: Database Engineers, Developers, Security Reviewers
- **Last verified commit**: `764d95687d0ccf848ec5b52fbb19638cc38123e4`
- **Source of truth**: `migrations/0012_fc27_performance_and_quarantine.sql`, `migrations/0013_fc27_manual_match_results.sql`, `migrations/post/grants.sql`, `src/fc27_repository.py`, `src/fc27_match_results.py`, `src/fc27_clubs.py`, `src/db_migrate.py`, `src/db_roles.py`
- **Supersedes**: `NONE`

This document specifies the normalized PostgreSQL 16 schema for East Coasters (`club_id=330323`, `platform=common-gen5`, `game_version=FC27`) introduced in `migrations/0012_fc27_performance_and_quarantine.sql` and the isolated manual Match Results domain introduced in `migrations/0013_fc27_manual_match_results.sql`, owned by `fc27_owner`, and governed by Grants Policy Version 15 (`migrations/post/grants.sql`), `src/fc27_repository.py`, and `src/fc27_match_results.py`.

---

## 1. Separation from `PostgresSaver` Checkpoint Tables & Manual Match Results Boundary

- **Normalized FC 27 Provider/Import Domain Tables (`migrations/0012_fc27_performance_and_quarantine.sql`)**: `fc27_clubs`, `fc27_export_batches`, `fc27_matches`, `fc27_player_match_stats`, and `fc27_quarantine_records`.
- **Manual Match Results Domain Tables (`migrations/0013_fc27_manual_match_results.sql`)**: `fc27_manual_matches`, `fc27_manual_match_players`, `fc27_manual_match_events`, and `fc27_manual_match_audit` (`source_classification = 'MANUAL_MATCH_RESULT'`). Kept strictly separate from `fc27_matches` and `fc27_player_match_stats`.
- **LangGraph `PostgresSaver` Tables**: `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, and `checkpoint_migrations` are managed exclusively by `langgraph.checkpoint.postgres.PostgresSaver` (`src/graph.py`, `src/db_migrate.py`) for workflow thread durability.
- **Strict Boundary**: `src/fc27_repository.py`, `src/fc27_match_results.py`, `src/fc27_analytics.py`, and `src/fc27_dashboard_queries.py` **never** read or write `PostgresSaver` tables, and `PostgresSaver` checkpoint blobs are **never** treated as normalized FC 27 domain records.

---

## 2. Normalized Tables (`migrations/0012_fc27_performance_and_quarantine.sql`)

### 2.1 `fc27_clubs` — Verified Club Registry
Stores canonical club identity and authority metadata:
- **Columns**: `club_id`, `platform`, `game_version`, `canonical_club_name`, `known_aliases`, `abbreviation`, `identity_state`, `manager_authority_status`, `source_classification`, `created_at`, `updated_at`.
- **Target Club**: `club_id = '330323'`, `canonical_club_name = 'East Coasters'`, `platform = 'common-gen5'`, `game_version = 'FC27'`.
- **Authority State (`manager_authority_status`)**: Defaults to `'UNVERIFIED'`. Club identity never self-grants human authority.
- **Immutability under `fc27_app`**: `fc27_app` has `SELECT, INSERT, UPDATE` only (`DELETE` is revoked).

### 2.2 `fc27_export_batches` — Idempotent Ingestion Batches
Tracks every validated export or provider batch ingested into PostgreSQL:
- **Columns**: `batch_id`, `export_id`, `club_id`, `platform`, `game_version`, `schema_version`, `source_classification`, `payload_sha256`, `valid_match_count`, `valid_player_stat_count`, `quarantined_record_count`, `replay_conflict_count`, `exported_at_utc`, `ingested_at`.
- **Idempotency**: Re-ingesting an identical `(export_id, payload_sha256)` is an idempotent no-op. Re-ingesting the same `export_id` with a different `payload_sha256` is flagged as `REPLAY_CONFLICT` and quarantined.

### 2.3 `fc27_matches` — Normalized Club Match Records
Stores validated club-level match outcomes:
- **Match Status (`match_status`)**: `'COMPLETED'`, `'ABANDONED'`, or `'PARTIAL'`. Only `'COMPLETED'` matches with `data_quality_status = 'VALID'` are eligible for `src/fc27_analytics.py` calculations.
- **Data Quality Status (`data_quality_status`)**: `'VALID'`, `'QUARANTINED'`, or `'REPLAY_CONFLICT'`.
- **Tactical & Outcome Columns**: `match_id`, `club_id`, `platform`, `game_version`, `played_at_utc`, `opponent_club_id`, `opponent_club_name`, `is_home`, `goals_for`, `goals_against`, `result`, `formation`, `match_hash`, `batch_id`.

### 2.4 `fc27_player_match_stats` — Pseudonymized Player Match Statistics
Stores per-player match performance using deterministic pseudonyms:
- **Pseudonym Constraint**: `player_pseudonym` matches `^plr_ecf_[0-9a-f]{12}$`, generated deterministically via SHA-256 (`src/fc27_clubs.py`, `src/fc27_clubs_api_contracts.py`).
- **Alias Visibility**: Display aliases are suppressed by default (`include_display_aliases=False` / `include_safe_aliases=False`) in analytics and dashboard queries.
- **Performance Columns**: `position_group`, `appearances`, `minutes_played`, `goals`, `assists`, `shots`, `passes_completed`, `passes_attempted`, `tackles_won`, `tackles_attempted`, `interceptions`, `saves`, `clean_sheet`, `red_cards`, `man_of_the_match`, `rating`, `stat_hash`.

### 2.5 `fc27_quarantine_records` — Invalid & Conflicting Record Quarantine
Captures schema violations, score/stat contradictions, and replay conflicts without polluting analytical tables:
- **Columns**: `quarantine_id`, `batch_id`, `club_id`, `platform`, `game_version`, `record_type`, `record_identifier`, `reason_code`, `reason_detail`, `sanitized_summary_json` (PII-scrubbed metadata only; raw personas and raw payloads are stripped), `quarantined_at`, `retention_expires_at` (`quarantined_at + INTERVAL '30 days'`).

---

## 3. Manual Match Results Domain (`migrations/0013_fc27_manual_match_results.sql`)

To preserve analytical integrity of provider-derived tables (`public.fc27_matches` and `public.fc27_player_match_stats`), operator-entered Discord Match Results are stored in four dedicated tables owned by `fc27_owner`:

1. **`public.fc27_manual_matches`**:
   - Stores scheduled fixtures (`status = 'SCHEDULED'`) and completed manual results (`status = 'FULL_TIME'`) with `source_classification = 'MANUAL_MATCH_RESULT'`.
   - Columns: `id`, `opponent`, `match_date`, `kickoff_time_text`, `kickoff_at_utc`, `notes`, `home_score` (`0..30`), `away_score` (`0..30`), `status`, `motm_display_label`, `motm_normalized_label`, `possession` (`0..100`), `shots`, `shots_on_target` (`<= shots`), `passes`, `pass_accuracy` (`0..100`), `corners`, `tackles`, `fouls`, `discord_message_id`, `discord_channel_id`, `source_classification`, `created_by_discord_id`, `updated_by_discord_id`, `created_at`, `updated_at`, `revision` (`>= 1` for optimistic concurrency), `deleted_at` (soft deletion timestamp).
2. **`public.fc27_manual_match_players`**:
   - Stores per-match manual player statistics (`match_id`, `normalized_label`, `display_label`, `goals` `0..30`, `assists` `0..30`, `tackles`, `passes`, `pass_accuracy` `0..100`, `shots`, `rating` `0.0..10.0`).
   - Manual player labels are operator-entered labels only; they are never automatically linked to provider identities or Xbox account IDs.
3. **`public.fc27_manual_match_events`**:
   - Stores normalized goal/assist event rows (`match_id`, `event_type`, `normalized_label`, `display_label`, `event_count` `1..30`).
4. **`public.fc27_manual_match_audit`**:
   - Append-only audit log (`id`, `match_id`, `action`, `actor_discord_id`, `before_revision`, `after_revision`, `change_summary_json`, `created_at`) protected by trigger `trg_fc27_manual_match_audit_immutable` blocking `UPDATE` and `DELETE`.

---

## 4. Privacy Exclusions & Ownership Rules

1. **Zero Raw Persona Persistence**: `fc27_player_match_stats` has no column for `personaName`, `blazeId`, `nucleusId`, PSN ID, Xbox Gamertag, or EA Account ID. Manual Match Results (`fc27_manual_match_players`) accept bounded operator-entered display labels only and reject Xbox IDs/gamertags.
2. **Zero Raw Provider Payload Persistence**: Neither `fc27_export_batches` nor `fc27_quarantine_records` stores raw HTTP response bodies (`raw_response_retained=False`).
3. **Database Ownership & Grants Policy Version 15**:
   - All provider-derived `fc27_*` tables and all manual Match Results `fc27_manual_*` tables are owned by `fc27_owner` (`migrations/0012_fc27_performance_and_quarantine.sql`, `migrations/0013_fc27_manual_match_results.sql`, and `migrations/post/grants.sql`).
   - `fc27_app` has DML `SELECT, INSERT, UPDATE` only on provider-derived `fc27_*` tables and `SELECT`-only on `fc27_manual_*` tables (`DELETE` is revoked from `fc27_app` across all `fc27_*` tables).
   - `fc27_match_results_writer` has `SELECT, INSERT, UPDATE` on `fc27_manual_matches`, `SELECT, INSERT, UPDATE, DELETE` on `fc27_manual_match_players` and `fc27_manual_match_events` (for transactional stat replacement during edits), and `SELECT, INSERT` on `fc27_manual_match_audit` (zero `DELETE` on `fc27_manual_matches` and `fc27_manual_match_audit`, and zero access to provider-derived or control-plane tables).
   - `fc27_dispatch` and `fc27_relay` have **zero** privileges on any `fc27_*` analytics or manual Match Results table.
