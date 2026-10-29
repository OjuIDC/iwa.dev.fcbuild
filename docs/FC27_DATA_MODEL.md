# FC 27 PostgreSQL Data Model & Privacy Schema

This document specifies the normalized PostgreSQL 16 schema for East Coasters (`club_id=330323`, `platform=common-gen5`, `game_version=FC27`) introduced in `migrations/0012_fc27_normalized_schema.sql`, owned by `fc27_owner`, and governed by Grants Policy Version 14 (`migrations/post/grants.sql`) and `src/fc27_repository.py`.

---

## 1. Separation from `PostgresSaver` Checkpoint Tables

- **Normalized FC 27 Domain Tables**: `fc27_clubs`, `fc27_export_batches`, `fc27_matches`, `fc27_player_match_stats`, and `fc27_quarantine_records`.
- **LangGraph `PostgresSaver` Tables**: `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, and `checkpoint_migrations` are managed exclusively by `langgraph.checkpoint.postgres.PostgresSaver` (`src/postgres_support.py`) for workflow thread durability.
- **Strict Boundary**: `src/fc27_repository.py`, `src/fc27_analytics.py`, and `src/fc27_dashboard_queries.py` **never** read or write `PostgresSaver` tables, and `PostgresSaver` **never** stores raw or normalized FC 27 club/match history tables.

---

## 2. Normalized Tables (`migrations/0012_fc27_normalized_schema.sql`)

### 2.1 `fc27_clubs` — Verified Club Registry
Stores canonical club identity and authority metadata:
- **Primary Key**: `(club_id, platform, game_version)`
- **Target Club**: `club_id = '330323'`, `club_name = 'East Coasters'`, `platform = 'common-gen5'`, `game_version = 'FC27'`
- **Identity State (`identity_state`)**: Enforced `CHECK (identity_state = 'VERIFIED_CLUB_ID')` for persisted club rows (`ClubIdentityState.VERIFIED_CLUB_ID`). Unverified clubs (`UNVERIFIED_NAME_ONLY`, `AMBIGUOUS_NAME_MATCH`, `REJECTED_MISMATCH`) are rejected by `FC27IngestionService` before persistence (`FC27IdentityGateError`).
- **Authority State (`manager_authority_status`)**: Defaults to `'UNVERIFIED'` (`CHECK (manager_authority_status IN ('UNVERIFIED', 'USER_ATTESTED', 'VERIFIED_MANAGER'))`).
- **Source Classification (`source_classification`)**: `'SYNTHETIC'`, `'COMMUNITY_RESEARCH_DERIVED'`, `'AUTHORIZED_CAPTURE'`, or `'USER_SUPPLIED_EXPORT'`.
- **Immutability under `fc27_app`**: `fc27_app` has `SELECT, INSERT, UPDATE` only (`DELETE` is revoked).

### 2.2 `fc27_export_batches` — Idempotent Ingestion Batches
Tracks every validated export envelope ingested into PostgreSQL:
- **Primary Key**: `batch_id` (`TEXT`)
- **Columns**: `export_id`, `club_id`, `platform`, `game_version`, `schema_version` (`'fc27-export-v1'`), `source_classification`, `payload_sha256` (`CHAR(64)`), `valid_match_count`, `valid_player_stat_count`, `quarantined_record_count`, `replay_conflict_count`, `exported_at_utc`, `ingested_at`.
- **Idempotency**: Re-ingesting an identical `(export_id, payload_sha256)` returns `idempotent_replay=True` with zero duplicate writes. Re-ingesting the same `export_id` with a different `payload_sha256` raises `FC27ReplayConflictError` and records a quarantine entry.

### 2.3 `fc27_matches` — Normalized Club Match Records
Stores validated club-level match outcomes:
- **Primary Key**: `(club_id, platform, game_version, match_id)`
- **Match Status (`match_status`)**: `'COMPLETED'`, `'ABANDONED'`, or `'PARTIAL'` (`MatchStatus`). Only `'COMPLETED'` matches with `data_quality_status = 'VALID'` are eligible for `src/fc27_analytics.py` calculations.
- **Data Quality Status (`data_quality_status`)**: `'VALID'`, `'QUARANTINED'`, or `'REPLAY_CONFLICT'` (`DataQualityStatus`).
- **Tactical & Outcome Columns**: `played_at_utc`, `opponent_club_id`, `opponent_club_name`, `is_home`, `goals_for` (`0..99`), `goals_against` (`0..99`), `result` (`'WIN'`, `'DRAW'`, `'LOSS'`), `formation` (normalized via `normalize_formation`), `match_hash` (`CHAR(64)` SHA-256 of canonical match fields), `batch_id`.
- **Replay Conflict Detection**: If a match with an existing `(club_id, platform, game_version, match_id)` arrives with an identical `match_hash`, it is treated as an idempotent no-op. If `match_hash` differs, the incoming record is rejected as `REPLAY_CONFLICT` and logged to `fc27_quarantine_records` without overwriting the committed match row.

### 2.4 `fc27_player_match_stats` — Pseudonymized Player Match Statistics
Stores per-player match performance using deterministic pseudonyms:
- **Primary Key**: `(club_id, platform, game_version, match_id, player_pseudonym)`
- **Foreign Key**: References `fc27_matches (club_id, platform, game_version, match_id)`
- **Pseudonym Constraint**: `player_pseudonym` must match `^plr_ecf_[0-9a-f]{12}$` (`CHECK (player_pseudonym ~ '^plr_ecf_[0-9a-f]{12}$')`), generated deterministically by `pseudonymize_member_id(club_id, raw_identifier)`.
- **Safe Alias Visibility (`safe_alias`)**: Optional synthetic/role label (`<= 32` chars, validated to ensure it does not match the raw persona or contain `@`/handles). Hidden by default (`include_safe_aliases=False`) in analytics and dashboard queries.
- **Performance Columns**: `position_group` (`'GOALKEEPER'`, `'DEFENDER'`, `'MIDFIELDER'`, `'FORWARD'`, `'UNKNOWN'`), `appearances` (`1`), `minutes_played` (`1..150`), `goals`, `assists`, `shots`, `passes_completed`, `passes_attempted` (`passes_completed <= passes_attempted`), `tackles_won`, `tackles_attempted` (`tackles_won <= tackles_attempted`), `interceptions`, `saves`, `clean_sheet`, `red_cards`, `man_of_the_match`, `rating` (`NUMERIC(4,2)`, `0.00..10.00`), `stat_hash` (`CHAR(64)`).

### 2.5 `fc27_quarantine_records` — Invalid & Conflicting Record Quarantine
Captures schema violations, score/stat contradictions, and replay conflicts without polluting analytical tables:
- **Primary Key**: `quarantine_id` (`BIGSERIAL`)
- **Columns**: `batch_id`, `club_id`, `platform`, `game_version`, `record_type` (`'MATCH'`, `'PLAYER_STAT'`, `'BATCH'`), `record_identifier`, `reason_code`, `reason_detail`, `sanitized_summary_json` (PII-scrubbed metadata only; raw personas are stripped), `quarantined_at`, `retention_expires_at` (`quarantined_at + INTERVAL '30 days'`).
- **30-Day Retention & Purge**: `fc27_app` is granted `DELETE` on `fc27_quarantine_records` solely to execute `fc27_purge_expired_quarantine()` (`DELETE FROM fc27_quarantine_records WHERE retention_expires_at <= NOW()`).

---

## 3. Privacy Exclusions & Ownership Rules

1. **Zero Raw Persona Persistence**: `fc27_player_match_stats` has no column for `personaName`, `blazeId`, `nucleusId`, PSN ID, Xbox Gamertag, or EA Account ID.
2. **Zero Raw Provider Payload Persistence**: Neither `fc27_export_batches` nor `fc27_quarantine_records` stores raw HTTP response bodies (`raw_response_retained=False`).
3. **Database Ownership & Grants Policy Version 14**:
   - All five `fc27_*` tables and `fc27_quarantine_records_quarantine_id_seq` are owned by `fc27_owner`.
   - `fc27_app` has `SELECT, INSERT, UPDATE` on `fc27_clubs`, `fc27_export_batches`, `fc27_matches`, `fc27_player_match_stats`, and `SELECT, INSERT, UPDATE, DELETE` on `fc27_quarantine_records`.
   - `fc27_dispatch` and `fc27_relay` have **zero** privileges on any `fc27_*` analytics table.
