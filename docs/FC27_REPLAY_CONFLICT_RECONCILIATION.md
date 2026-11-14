# FC27 Live Polling Replay-Conflict Audit and Safe Reconciliation

## Document Classification
- **Authority Tier**: Tier 2 — Architecture & Operational Governance Specification
- **Scope**: East Coasters (`club_id = 330323`, `platform = common-gen5`, `game_version = FC27`)
- **Schema Baseline**: Migration `0015`, Grants Policy Version `16` (`SCHEMA_CHANGE_REQUIRED = NO`)
- **Equivalence Contract**: `MATCH_EQUIVALENCE_CONTRACT_VERSION = "fc27-match-equivalence-v1"`

---

## 1. Root Cause Summary

During the authorized single-request Run B canary (`match_type = leagueMatch`, `provider_requests = 1`, `retries = 0`, `fallback_requests = 0`), 10 normalized community-provider records collided with persisted rows in `public.fc27_matches` under matching `(club_id, platform, game_version, external_match_id)` keys with differing `match_hash` digests. The transaction rolled back atomically (`MATCH_DELTA = 0`, `PLAYER_STAT_DELTA = 0`, `EXPORT_BATCH_DELTA = 0`, `FINGERPRINT_DELTA = 0`, `DISCORD_JOB_DELTA = 0`).

Read-only forensic inspection of repository code and retained PostgreSQL metadata established `ROOT_CAUSE = HASH_CONTRACT_DEFECT` (with compounding provenance and operational defects):

1. **Source-specific and order-sensitive fields in `compute_match_hash` (`src/fc27_privacy.py`)**:
   - `compute_match_hash` included `source_classification`, `data_quality_status`, `formation`, player `role`, `build_reference`, `shots_on_target`, `possession_losses`, and preserved the list order of `player_performances` without sorting by `local_player_reference`.
   - In the community-provider normalization path (`src/fc27_clubs_api_contracts.py`, `src/fc27_community_provider.py`), `players["330323"]` is a JSON object map whose key iteration order is non-canonical across upstream responses. Across all 17 retained matches, `17/17` (`100%`) reconstructed their stored `match_hash` using `stat_id` insertion order, while `0/17` (`0%`) were ordered by `local_player_reference`.
2. **Historical provenance mislabeling and synthetic defaults (`src/fc27_repository.py`)**:
   - `build_export_envelope_for_persistence` previously hardcoded `source_classification = "USER_SUPPLIED_EXPORT"`, `supplier_reference = "demo-fixture-loopback-v1"`, `possession_losses = 2`, `_default_role_for_position(...)`, and `_default_build_for_position(...)` (`bld_ecf_*_v1`), whereas `persist_polled_match_envelope` passed `source_reference = "fc27-community-v1:leagueMatch"`.
3. **Unpersisted conflict diagnostics and batch-wide count inflation (`src/fc27_polling_worker.py`)**:
   - Polling persistence passed `quarantine_conflicts = False` (and `_insert_quarantine_row_in_tx` unconditionally wrote to `public.domain_events`, outside `fc27_poll_writer` grants), leaving zero safe conflict metadata in `public.fc27_quarantine_records` after rollback.
   - `run_fc27_poll_cycle` set `replay_conflict_count = max(1, match_envelope.accepted_record_count)`, reporting `10` whenever *any* match in a 10-match window conflicted.
4. **Process-memory-only circuit-breaker state (`src/fc27_api_rate_limit.py`, `src/fc27_polling_worker.py`)**:
   - `FC27AdapterCircuitBreaker` lived only in process memory (`PROCESS_MEMORY_ONLY`), so exiting the one-shot Run B container discarded the `OPEN` state, and `manual_reset()` lacked audit metadata.

Because Run B did not retain raw payloads or per-match conflict diagnostics by design (`CONFLICT_ROW_MAPPING = INSUFFICIENT_EVIDENCE`), individual conflict shapes from Run B are not inferred. Any material difference in immutable match or player facts remains strictly fail-closed.

---

## 2. Retained-Data Provenance

Read-only audit of `public.fc27_export_batches`, `public.fc27_matches` (`17` rows, bounded audit ordinals `M01..M17`), and `public.fc27_player_match_stats` (`97` rows) proved:

- **Origin**: All 17 retained matches entered through three earlier bounded community-provider persistence batches (`B01` on `2026-11-01`, `10` matches; `B02` on `2026-11-08`, `4` new + `6` duplicates; `B03` on `2026-11-10`, `3` new + `7` duplicates), followed by an idempotent poll at `2026-11-11T02:20:50Z`.
- **Not synthetic demo data (`DEMO_FIXTURE_LINEAGE_PROVEN = NO`)**: Variable player counts (`5` to `7` per match across `97` rows), organic match timestamps, and `comm_eval_<16hex>` batch IDs rule out `src/fc27_demo.py` synthetic fixtures (`exp_demo_ecf_loopback_001`, fixed 5-match / 11-player template).
- **Misclassification explanation (`RETAINED_ROWS_SOURCE_CLASSIFICATION_ACCURATE = NO`)**: The retained rows carry `USER_SUPPLIED_EXPORT` and `demo-fixture-loopback-v1` solely because `build_export_envelope_for_persistence` previously defaulted to those values for community-provider persistence.

---

## 3. Versioned Canonical-Equivalence Contract (`fc27-match-equivalence-v1`)

`src/fc27_privacy.py` defines `MATCH_EQUIVALENCE_CONTRACT_VERSION = "fc27-match-equivalence-v1"` via `project_match_equivalence_v1`, `compute_match_equivalence_hash`, and `compare_match_equivalence_v1`.

When `(club_id, platform, game_version, external_match_id)` already exists in `public.fc27_matches`:

1. **Fast path**: If `existing_match_hash == incoming_match_hash`, the match is an exact idempotent duplicate.
2. **Versioned equivalence check**: If `existing_match_hash != incoming_match_hash`, persistence projects both the persisted row and the incoming candidate under `fc27-match-equivalence-v1`:
   - Zero differing paths (`len(diff_paths) == 0`): the match is proven equivalent on all immutable match facts and observed player statistics; the existing persisted row and its historical `match_hash` are preserved untouched as an idempotent duplicate.
   - One or more differing paths (`len(diff_paths) > 0`): persistence **fails closed** with `FC27ReplayConflictError`.

### 3.1 Fields Included (Always Fail-Closed Material Facts)

Any difference in the following fields ALWAYS produces a `REPLAY_CONFLICT`:

- **Match scope & identity**: `club_id`, `platform`, `game_version` (uppercase), `external_match_id`, `opponent_name` (normalized NFC text).
- **Match outcome & timing**: canonicalized `match_type` (`leagueMatch` <-> `LEAGUE`, `playoffMatch` <-> `PLAYOFF`, `friendlyMatch` <-> `FRIENDLY`; cross-type differences such as `LEAGUE` vs `PLAYOFF` remain conflicts), `played_at` (UTC epoch-second precision, `YYYY-MM-DDTHH:MM:SSZ`), `match_status`, `result` (`WIN` / `DRAW` / `LOSS`), `goals_for`, `goals_against`.
  - **Persisted match-type evidence**: `public.fc27_matches` has no dedicated `match_type` column, so `_load_persisted_match_projections_in_cursor` recovers authoritative evidence in two ways: (1) explicit `fc27-community-*:<type>` suffixes on `fc27_export_batches.supplier_reference`, and (2) constant-time SHA-256 verification against `fc27_matches.match_hash` using `stat_id`-ordered `fc27_player_match_stats` for historical `comm_eval_*` batches (`17/17` retained matches verify as `LEAGUE`) and `USER_SUPPLIED_EXPORT` batches.
  - **Missing or unverified evidence fails closed**: when a persisted match was imported without a concrete `match_type` (`None` or `UNKNOWN`) or its `match_hash` cannot be cryptographically verified (`match_type_unverified = True`), comparing against an incoming typed record emits `matches[idx].match_type` and triggers `REPLAY_CONFLICT`; a missing type is never treated as an unconstrained wildcard.
- **Canonical player roster & core statistics** (sorted deterministically by `(local_player_reference, position, minutes_played, goals, assists, ...)`):
  - Player count (`len(player_performances)`),
  - `local_player_reference`, `position`, `minutes_played`, `goals`, `assists`,
  - `passes_attempted`, `passes_completed`, `tackles_attempted`, `tackles_won`,
  - `interceptions`, `shots`, `saves`, and `provider_rating` (half-even rounded to 2 decimals; clean-sheet status is likewise fail-closed via match-level `goals_against`).
- **User-export-only tactical/extended fields when neither side has community-provider provenance**:
  - `formation`, player `role`, `build_reference`, `shots_on_target`, and `possession_losses` remain strictly compared whenever comparing two genuine user-supplied exports.
  - External JSON imports parsed via `parse_fc27_user_export_json` reject reserved community-provider provenance markers (`COMMUNITY_RESEARCH_DERIVED`, `comm_eval_*`, `comm_poll_*`, `fc27-community-*`) so untrusted exports cannot spoof provider provenance or poison polling quarantine state.

### 3.2 Fields Excluded and Why Exclusions Are Safe

| Excluded / Reconciled Aspect | Reason Exclusion Is Safe |
|---|---|
| `source_classification`, `supplier_reference`, `export_id`, `observed_at`, `generated_at`, `supplied_at`, `data_quality_status` | Provenance and ingestion-envelope metadata describe *how and when* a batch arrived, not what occurred in the match. Persisted provenance on retained rows is never overwritten. |
| Raw JSON map key order / `stat_id` insertion order of `player_performances` | Player performances represent an unordered set per match keyed by `local_player_reference`. Canonical sorting eliminates upstream JSON dictionary order sensitivity without weakening per-player stat checks. |
| Sub-second or timezone-syntax formatting of `played_at` (`+00:00` vs `Z` vs `.000000Z`) | Upstream provider timestamps are Unix epoch seconds; normalizing to UTC integer seconds preserves exact match time while eliminating ISO formatting artifacts. Any difference `>= 1` second remains a conflict. |
| Provider-unobserved tactical/extended fields (`formation`, `role`, `build_reference`, `shots_on_target`, `possession_losses`) when at least one side has community-provider provenance | `fc-clubs-api` match endpoints do not expose formation, role, build ID, shots on target, or possession losses (`build_export_envelope_for_persistence` historically substituted synthetic defaults such as `possession_losses = 2` and `bld_ecf_*_v1`). Excluding unobserved provider defaults prevents false conflicts against user exports or historical `comm_eval_*` batches while keeping all 12 provider-observed player stats fail-closed. |
| `display_alias` | Never persisted in `fc27_player_match_stats` and stripped at the privacy boundary; alias observations are governed separately by `fc27_player_alias_fingerprints`. |

---

## 4. Historical-Hash Compatibility and Zero Retained-Data Mutation

- `compute_match_hash` (`src/fc27_privacy.py`) is **unchanged**. Every previously computed `match_hash` remains deterministic and reproducible.
- **No retained-data rewrite**: Zero `INSERT`, `UPDATE`, or `DELETE` statements were executed against the retained database (`RETAINED_DATABASE_WRITES = 0`). All 17 retained matches, their stored `match_hash` values, their `export_id` links, and all 97 `fc27_player_match_stats` rows remain untouched.
- New community-provider batches generated after this change accurately record `source_classification = "COMMUNITY_RESEARCH_DERIVED"`, `supplier_role = "DATA_STEWARD"`, `role = "UNKNOWN"`, `possession_losses = 0`, and `build_reference = None`, while seamlessly reconciling with historical `comm_eval_*` / `demo-fixture-loopback-v1` rows through `fc27-match-equivalence-v1`.

---

## 5. Privacy-Safe Conflict Quarantine and Durable Circuit Protection

When an incoming poll batch contains one or more materially conflicting matches (or a Discord job idempotency conflict):

1. **Full domain rollback**: The primary transaction rolls back completely — zero rows are written to `fc27_matches`, `fc27_player_match_stats`, `fc27_export_batches`, `fc27_player_alias_fingerprints`, or `fc27_discord_jobs`, and neither `fc27_clubs.updated_at` nor the match-type rotation cursor advances.
2. **Sanitized quarantine persistence**: In a dedicated follow-up transaction using existing `fc27_poll_writer` privileges (Grants Policy `16`, Migration `0015`, `emit_domain_events = False`), `persist_polled_match_envelope` inserts one `PENDING_REVIEW` `REPLAY_CONFLICT` row per conflicting match into `public.fc27_quarantine_records`:
   - `external_match_id` stores only the pseudonymous `mref_ecf_<12hex>` match reference (never a raw external match ID or player identifier);
   - `diagnostic_paths` stores only bounded schema paths (for example `matches[0].goals_for` or `matches[0].player_performances[2].passes_completed`), capped at `<= 100` entries and `<= 4096` UTF-8 JSON bytes and validated by `public.fc27_is_valid_diagnostic_paths(diagnostic_paths)`;
   - zero raw provider payloads, aliases, or HMAC values are stored;
   - `run_fc27_poll_cycle` records the exact `replay_conflict_count` (`len(conflicting_matches)`).
3. **Restart-durable circuit-breaker enforcement**:
   - On startup or state recovery, `recover_polling_state_from_postgres` checks `public.fc27_quarantine_records` for any `PENDING_REVIEW` `REPLAY_CONFLICT` row belonging to community polling (`COMMUNITY_RESEARCH_DERIVED`, `comm_poll_*`, or `comm_eval_*`), failing closed (`PROVIDER_BLOCKED` / `DATABASE_STATE_RECOVERY_ERROR`, `provider_requests = 0`) if PostgreSQL state recovery itself encounters an error.
   - While any polling replay conflict remains `PENDING_REVIEW`, a new worker process immediately trips its circuit breaker to `OPEN` (`PROVIDER_BLOCKED` / `CIRCUIT_BREAKER_OPEN`, `provider_requests = 0`) before acquiring single-flight locks or issuing transport calls.
   - `FC27AdapterCircuitBreaker.manual_reset(authorization_reference=...)` and `authorize_half_open_probe(authorization_reference=...)` now require a bounded privacy-safe `authorization_reference` and record `reset_count`, `last_reset_at_monotonic`, and `last_reset_authorization_reference` in `safe_metadata()`.

---

## 6. Post-Merge Operational Boundaries and Prerequisites

This remediation pull request is **code, tests, and documentation only**:

- **No circuit reset is performed in this PR** (`CIRCUIT_RESET_PERFORMED = NO`).
- **No second live provider request was executed** (`SECOND_PROVIDER_REQUESTS = 0`).
- **No recurring polling worker or Discord bot activation occurred** (`WORKER_STATUS = stopped`, `BOT_STATUS = stopped`).
- **No schema migration (`0016`) or grants-policy change was introduced** (`MIGRATION_MAX = 0015`, `GRANTS_POLICY = 16`).

### Operator Procedure After Merge

1. **Pull merged `main` and rebuild worker image locally** while keeping `fc27-community-worker` and `fc27-discord-bot` stopped.
2. **Verify a fresh retained PostgreSQL backup** (`scripts/fc27_backup.py` and disposable restore check `scripts/fc27_restore_verify.py`) before any live canary is considered.
3. **Separately reviewed circuit-reset & quarantine-review procedure required**:
   - Before any future retry after a conflict, an operator must inspect `public.fc27_quarantine_records` (`fetch_quarantine_records`), resolve/disposition any `PENDING_REVIEW` `REPLAY_CONFLICT` rows via `review_quarantine_record` with non-PII `reviewed_by` and `review_notes`, and execute a separately reviewed circuit-reset procedure supplying an explicit `authorization_reference`.
4. **Separate operator authorization required before any second canary**:
   - Even after merge, a second bounded one-shot live canary (`leagueMatch`, `max_requests = 1`, `retries = 0`) requires explicit separate operator authorization (`LIVE_PROVIDER_REQUEST_AUTHORIZED = YES` under a new bounded runbook). Recurring worker activation and Discord bot activation remain disabled until post-canary verification succeeds cleanly.
