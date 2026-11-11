# FC 27 PostgreSQL Data Model & Privacy Schema

- **Status**: Current
- **Audience**: Database Engineers, Developers, Security Reviewers
- **Last verified commit**: `764d95687d0ccf848ec5b52fbb19638cc38123e4`
- **Source of truth**: `migrations/0012_fc27_performance_and_quarantine.sql`, `migrations/0013_fc27_manual_match_results.sql`, `migrations/0014_fc27_player_development.sql`, `migrations/post/grants.sql`, `src/fc27_repository.py`, `src/fc27_match_results.py`, `src/fc27_clubs_api_contracts.py`, `src/db_migrate.py`, `src/db_roles.py`
- **Supersedes**: `NONE`

This document specifies the normalized PostgreSQL 16 schema for East Coasters (`club_id=330323`, `platform=common-gen5`, `game_version=FC27`) introduced in `migrations/0012_fc27_performance_and_quarantine.sql`, the isolated manual Match Results domain introduced in `migrations/0013_fc27_manual_match_results.sql`, and the player development and Discord job domain introduced in `migrations/0014_fc27_player_development.sql`, owned by `fc27_owner`, and governed by Grants Policy Version 16 (`migrations/post/grants.sql`), `src/fc27_repository.py`, and `src/fc27_match_results.py`.

---

## 1. Separation from `PostgresSaver` Checkpoint Tables & Manual Match Results Boundary

- **Normalized FC 27 Provider/Import Domain Tables (`migrations/0012_fc27_performance_and_quarantine.sql`)**: `fc27_clubs`, `fc27_export_batches`, `fc27_matches`, `fc27_player_match_stats`, and `fc27_quarantine_records`.
- **Manual Match Results Domain Tables (`migrations/0013_fc27_manual_match_results.sql`)**: `fc27_manual_matches`, `fc27_manual_match_players`, `fc27_manual_match_events`, and `fc27_manual_match_audit` (`source_classification = 'MANUAL_MATCH_RESULT'`). Kept strictly separate from `fc27_matches` and `fc27_player_match_stats`.
- **LangGraph `PostgresSaver` Tables**: `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, and `checkpoint_migrations` were managed by historical `langgraph.checkpoint.postgres.PostgresSaver` (`src/db_migrate.py`; historical `src/graph.py` superseded in Stage B3) for workflow thread durability.
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
- **Pseudonym Constraint**: `player_pseudonym` matches `^plr_ecf_[0-9a-f]{12}$`, generated deterministically via SHA-256 (`src/fc27_clubs_api_contracts.py`).
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

## 4. Player Development & Discord Job Domain (`migrations/0014_fc27_player_development.sql`)

`migrations/0014_fc27_player_development.sql` adds seven purpose-specific tables owned by `fc27_owner` for privacy-preserving player alias fingerprinting, player-to-Discord linking, build catalog and audit, player development suggestions and assessments, and bounded Discord work jobs:

1. **`public.fc27_player_alias_fingerprints`**:
   - Keyed by `pseudonym` (`^plr_ecf_[0-9a-f]{12}$`) with unique `gamertag_hmac` (`^[0-9a-f]{64}$`), `last_seen_at`, `created_at`, and `updated_at`. Stores HMAC-SHA256 digests only; never stores raw gamertags.
2. **`public.fc27_player_links`**:
   - Stores consented player-to-Discord links (`link_id`, `pseudonym`, `discord_user_id`, `status` in `'pending' | 'confirmed' | 'revoked'`, `requested_by`, `confirmed_by`, `requested_at`, `confirmed_at`, `revoked_at`, `created_at`, `updated_at`).
   - Enforces partial unique indexes `uq_fc27_player_links_confirmed_pseudonym` and `uq_fc27_player_links_confirmed_discord_user` (`WHERE status = 'confirmed'`), allowing re-linking after revocation.
3. **`public.fc27_build_catalog`**:
   - Versioned Pro Clubs build definitions (`catalog_row_id`, `build_key`, `name`, `positions` `text[]`, `archetype`, `attribute_priorities`, `playstyles`, `match_rules`, `game_patch`, `notes`, `version`, `status` in `'active' | 'retired'`, `created_by`, `created_at`, `UNIQUE (build_key, version)`). Starts empty (`0` rows) by default so unverified external meta builds are never pre-seeded.
4. **`public.fc27_build_catalog_audit`**:
   - Append-only audit log (`audit_id`, `catalog_row_id`, `build_key`, `version`, `action` in `'create' | 'update' | 'retire'`, `actor_discord_user_id`, `snapshot`, `created_at`) protected by trigger `trg_fc27_build_catalog_audit_append_only`.
5. **`public.fc27_player_suggestions`**:
   - Tracks player development suggestions (`suggestion_id`, `pseudonym`, `kind` in `'build' | 'training'`, structured `objective` JSONB requiring `metric`, `direction`, `target_delta`, `min_matches <= max_matches`, `content`, `catalog_build_key`, `catalog_version`, `baseline`, `baseline_match_ids` `text[]`, `status` in `'suggested' | 'acknowledged' | 'tracking' | 'outcome'`, `outcome` in `'target_met' | 'improving' | 'no_change' | 'regressed' | 'insufficient_data'`, `outcome_value`, Discord IDs, and lifecycle timestamps).
6. **`public.fc27_suggestion_assessments`**:
   - Append-only assessment checkpoints (`assessment_id`, `suggestion_id`, `calculated_values`, `match_ids` `text[]`, `assessment_time`, `created_at`) protected by trigger `trg_fc27_suggestion_assessments_append_only`.
7. **`public.fc27_discord_jobs`**:
   - Bounded Discord job queue (`job_id`, `job_type` in `'post_new_results' | 'evaluate_suggestions'`, `payload`, `state` in `'pending' | 'claimed' | 'completed' | 'failed'`, `claim_owner`, `claimed_at`, `lease_expires_at`, `attempt_count`, `last_error_category`, `created_at`, `available_at`, `completed_at`, `idempotency_key` `UNIQUE`) with trigger `trg_fc27_discord_jobs_validate_claim` enforcing active claim leases (`lease_expires_at > now()`) when `state = 'claimed'`.

---

## 5. Privacy Exclusions & Ownership Rules

1. **Zero Raw Persona Persistence**: `fc27_player_match_stats` and all Stage C player-development tables have no columns for `personaName`, `blazeId`, `nucleusId`, PSN ID, Xbox Gamertag, or EA Account ID. Manual Match Results (`fc27_manual_match_players`) accept bounded operator-entered display labels only and reject Xbox IDs/gamertags.
2. **Zero Raw Provider Payload Persistence**: Neither `fc27_export_batches` nor `fc27_quarantine_records` stores raw HTTP response bodies (`raw_response_retained=False`).
3. **Database Ownership & Grants Policy Version 16**:
   - All provider-derived `fc27_*` tables, manual Match Results `fc27_manual_*` tables, and Stage C player development & Discord job tables are owned by `fc27_owner` (`migrations/0012_fc27_performance_and_quarantine.sql` through `migrations/0014_fc27_player_development.sql`, and `migrations/post/grants.sql`).
   - `fc27_poll_writer` has narrow write access to normalized provider tables (`fc27_clubs`, `fc27_export_batches`, `fc27_matches`, `fc27_player_match_stats`, `fc27_quarantine_records`), `fc27_player_alias_fingerprints`, and `SELECT, INSERT` on `fc27_discord_jobs`.
   - `fc27_player_dev_writer` has narrow write access to `fc27_player_links`, `fc27_build_catalog`, append-only `fc27_build_catalog_audit`, `fc27_player_suggestions`, append-only `fc27_suggestion_assessments`, and claim/lease/outcome updates on `fc27_discord_jobs`, plus `SELECT`-only access to `fc27_player_alias_fingerprints` and normalized provider tables.
   - `fc27_discord_reader` has `SELECT`-only access across provider, manual Match Results, and player development tables, with zero write privileges anywhere.
   - `fc27_match_results_writer` has `SELECT, INSERT, UPDATE` on `fc27_manual_matches`, `SELECT, INSERT, UPDATE, DELETE` on `fc27_manual_match_players` and `fc27_manual_match_events` (for transactional stat replacement during edits), and `SELECT, INSERT` on `fc27_manual_match_audit` (zero `DELETE` on `fc27_manual_matches` and `fc27_manual_match_audit`, and zero access to provider-derived or control-plane tables).
   - `fc27_dispatch` and `fc27_relay` have **zero** privileges on any `fc27_*` analytics, manual Match Results, or player development table.
