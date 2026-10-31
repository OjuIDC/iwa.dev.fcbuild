# FC 27 Community Provider One-Shot Evaluation (`fc-clubs-api-community-v1`)

## 1. Purpose and Scope

This document records the bounded audit, contract design, isolated container evaluation, and downstream analytical verification of the unofficial community Python package `1erkandogan/fc27-clubs-api` (`fc-clubs-api`) for **East Coasters** (`club_id=330323`, `platform=common-gen5`, `game_version=FC27`).

The community provider is strictly isolated behind `src/fc27_community_provider.py` (`fc-clubs-api-community-v1`) and labeled with a distinct source classification:

- `SOURCE_CLASSIFICATION = LIVE_EA_CLUBS_COMMUNITY_CLIENT`

It is never conflated with `SYNTHETIC_FIXTURE`, `DEMO_FIXTURE_LOOPBACK`, `LIVE_EA_CLUBS_READ_ONLY`, or `LIVE_EA_CLUBS_BROWSER_OBSERVED`.

---

## 2. Pinned Upstream Audit (`PART B`)

| Audit Field | Verified Value |
|---|---|
| `UPSTREAM_REPOSITORY` | `https://github.com/1erkandogan/fc27-clubs-api` |
| `UPSTREAM_PACKAGE_NAME` | `fc-clubs-api` (`import fc_clubs_api`) |
| `UPSTREAM_COMMIT_SHA` | `26a5ff12ea793042bd776a858c74f3f44dd427f5` |
| `UPSTREAM_VERSION` | `0.3.1` |
| `UPSTREAM_LICENSE` | `MIT` |
| `PYTHON_REQUIREMENT` | `>=3.9` |
| `RUNTIME_DEPENDENCIES` | Python standard library (`urllib.request`, `json`, `datetime`); optional `pandas>=1.5` |
| `HTTP_TRANSPORT_USED` | Standard library `urllib.request` wrapped with redirect refusal (`follow_redirects=False`), `10.0s` timeout, `524,288`-byte ceiling, and immediate raw buffer zeroing |
| `BASE_URL` | `https://proclubs.ea.com/api/fc` |
| `COOKIES_USED` | `NO` |
| `CREDENTIAL_FLOWS_PRESENT` | `NO` |
| `PROXY_SUPPORT_PRESENT` | `NO` |
| `ARBITRARY_GET_JSON_PRESENT` | `YES_UPSTREAM_BLOCKED_IN_WRAPPER` |
| `PANDAS_OPTIONAL` | `YES` |
| `RAW_OUTPUT_MODE_PRESENT` | `YES_UPSTREAM_BLOCKED_IN_WRAPPER` |

---

## 3. Wrapper Boundaries and Safety Controls (`PART C` – `PART H`)

1. **Two One-Shot Methods Only**:
   - `FC27CommunityProviderAdapter.get_east_coasters_identity_once`: Executes at most `1` `clubs/info` request (`clubIds=330323`, `platform=common-gen5`, `output="records"`, `retries=0`).
   - `FC27CommunityProviderAdapter.get_east_coasters_recent_matches_once`: Executes at most `1` `clubs/matches` request (`clubIds=330323`, `platform=common-gen5`, `matchType=leagueMatch`, `maxResultCount<=10`, `output="records"`, `retries=0`), strictly gated on a prior validated `CLUB_IDENTITY` envelope.
2. **Blocked Upstream Surface**:
   - Never exposes `get_json`, `all_time_leaderboard`, `current_season_leaderboard`, `member_stats`, `member_career_stats`, or live `player_matches` (`PLAYER_MATCH_LIVE_REQUEST_EXECUTED=NO`).
   - Rejects `output="raw"` and `output="dataframe"` at the ingestion boundary.
3. **Privacy and Pseudonymization**:
   - Strips any upstream `"raw"` sub-dicts and CDN crest URLs in memory before validation.
   - Strips all raw player names/gamer tags (`playername`, `player_name`, `proName`, `personaName`, `gamertag`) in memory before returning from the transport hook.
   - Maps all player identifiers to deterministic `plr_ecf_<12-lowercase-hex>` pseudonyms via `FC27PseudonymCollisionDetector`.
   - Zeroes raw byte buffers (`bytearray`) in `finally:` blocks (`RAW_RESPONSE_RETAINED=NO`).
4. **Match Event Policy (`PART G`)**:
   - Named numeric totals (`goals`, `assists`, `shots`, `passes_made`, `pass_attempts`, `tackles_made`, `tackle_attempts`, `saves`, `red_cards`, `mom`, `seconds_played`) remain authoritative.
   - Only high-confidence event bit/counter mappings are decoded; unknown bits are preserved in `unknown_event_bits_mask` without inventing event names.

---

## 4. Isolated Container One-Shot Evaluation Results (`PART M` – `PART P`)

Executed inside `fc27-community-client-evaluation` (`Dockerfile.community-eval` / `docker-compose.community-eval.yml`, `read_only: true`, `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`):

| Metric | Verified Outcome |
|---|---|
| `PROVIDER_ID` | `fc-clubs-api-community-v1` |
| `SOURCE_CLASSIFICATION` | `LIVE_EA_CLUBS_COMMUNITY_CLIENT` |
| `IDENTITY_REQUEST_EXECUTED` | `YES` (`IDENTITY_REQUESTS_COUNT=1`, `IDENTITY_RETRIES_COUNT=0`) |
| `IDENTITY_HTTP_OUTCOME` | `HTTP_200_VALID_JSON` |
| `IDENTITY_VALIDATION` | `PASS` (`club_id=330323`, `club_name=East Coasters`, `platform=common-gen5`) |
| `IDENTITY_RECORD_HASH` | `f16653921812d363aad37c747a00ffa4dbd044f35cb547a49bc3da27060a07b3` |
| `MATCH_REQUEST_EXECUTED` | `YES` (`MATCH_REQUESTS_COUNT=1`, `MATCH_RETRIES_COUNT=0`) |
| `MATCH_HTTP_OUTCOME` | `HTTP_200_VALID_JSON` |
| `MATCH_SCHEMA_STATUS` | `PASS` |
| `MATCH_NORMALIZATION_STATUS` | `PASS` |
| `MATCH_PRIVACY_STATUS` | `PASS` |
| `SOURCE_MATCH_COUNT` | `10` |
| `ACCEPTED_MATCH_COUNT` | `10` |
| `REJECTED_MATCH_COUNT` | `0` |
| `PLAYER_STAT_ROW_COUNT` | `60` |
| `MATCH_PAYLOAD_HASH` | `e8a7ede9ed3d3c54b491779b75298ff4bc88888db646b70ce9cb0af8347caaa6` |
| `PLAYER_MATCH_LIVE_REQUEST_EXECUTED` | `NO` |
| `PERSISTENCE_ENABLED_BY_DEFAULT` | `NO` (`PERSISTENCE_EXECUTED=NO`, `DATABASE_ROWS_WRITTEN=0`) |
| `SEARCH_EXECUTED` | `YES` (`SEARCH_CONTEXT_HASH=dcbedc0108c4632c77a85a08d59a487d35bb69692e11f86a5d9acd214c1ce3ac`) |
| `SCOUT_EXECUTED` | `YES` (`SCOUT_REPORT_HASH=697b91cb9b8bb98820b36eeee461a692daf9b0e1776adb97d9449c6401f676bc`) |
| `ANALYST_EXECUTED` | `YES` (`ANALYST_REPORT_HASH=4052c9033ef35447f35a06029f9f0045a9a95c649e07af429600f79e484fce03`) |
| `CONFIDENCE_TIER` | `MODERATE_CONFIDENCE` |
| `FINDING_COUNT` | `20` |
| `COUNTER_EVIDENCE_COUNT` | `71` |
| `CONFOUNDER_COUNT` | `13` |
| `DATA_GAP_COUNT` | `5` |
| `COACH_EXECUTED` | `NO` |
| `TACTICS_EXECUTED` | `NO` |
| `DIRECTOR_EXECUTED` | `NO` |
| `RECOMMENDATIONS_ENABLED` | `NO` |
| `DISPATCH_ENABLED` | `NO` |
| `CIRCUIT_STATE` | `CLOSED` |
| `RAW_RESPONSE_RETAINED` | `NO` |

---

## 5. First Real East Coasters Batch Persistence, Replay, and Dashboard Verification (`PART H` – `PART O`)

Following independent review of `src/fc27_community_provider.py`, `src/fc27_pandas_view.py`, `scripts/fc27_community_eval.py`, `Dockerfile.community-eval`, and `docker-compose.community-eval.yml`:

- **Pre-Persistence Preview (`PART I`)**:
  - `PROVIDER_ID = fc-clubs-api-community-v1` (`SOURCE_CLASSIFICATION = LIVE_EA_CLUBS_COMMUNITY_CLIENT`, unofficial MIT-licensed community client `1erkandogan/fc27-clubs-api@26a5ff12ea793042bd776a858c74f3f44dd427f5` `v0.3.1`, not an official EA SDK).
  - `MATCH_COUNT = 10`, `PLAYER_STAT_ROW_COUNT = 60`, `EARLIEST_MATCH = 2026-10-27T02:18:44Z`, `LATEST_MATCH = 2026-10-31T03:38:20Z`, `MATCH_TYPES = LEAGUE`, `PAYLOAD_HASH = e8a7ede9ed3d3c54b491779b75298ff4bc88888db646b70ce9cb0af8347caaa6`, `DUPLICATE_PLAN = 0`, `CONFLICT_PLAN = 0`.
- **Single-Transaction PostgreSQL Persistence (`PART J`)**:
  - `LIVE_DATA_PERSISTED = YES`, `MATCH_ROWS_INSERTED = 10`, `TEAM_STAT_ROWS_INSERTED = 10`, `PLAYER_STAT_ROWS_INSERTED = 60`, `DUPLICATES_IGNORED = 0`, `REPLAY_CONFLICTS = 0`.
  - `RAW_RESPONSE_PERSISTED = NO`, `PLAYER_NAMES_PERSISTED = NO`, `CHECKPOINT_TABLES_MODIFIED = NO`, `APPROVAL_TABLES_MODIFIED = NO`, `DISPATCH_TABLES_MODIFIED = NO`.
- **Idempotency & Conflict Rollback (`PART K` & `PART L`)**:
  - Replaying the identical 10-match batch (`ADDITIONAL_PROVIDER_REQUESTS = 0`) results in `IDEMPOTENT_REPLAY = PASS`, `NEW_MATCH_ROWS = 0`, `NEW_TEAM_STAT_ROWS = 0`, `NEW_PLAYER_STAT_ROWS = 0`, `DUPLICATES_IGNORED = 10`, `REPLAY_CONFLICTS = 0`.
  - Modifying `goals_for` for an existing `external_match_id` against disposable storage triggers `FC27ReplayConflictError` (`REPLAY_CONFLICT_DETECTED = YES`, `TRANSACTION_ROLLBACK = YES`, `DATABASE_STATE_UNCHANGED = YES`).
- **Persisted Read-Only Analytics & Local Dashboard (`PART M` – `PART O`)**:
  - Reading back from PostgreSQL via `PostgresFC27AnalyticsReader` (`run_analytics_from_persisted_community_records`) produces `SEARCH_HASH = fcece2013e27da3134822978ccd5f19a32f7cc6bc59a6ef9d3c2d87a715fc193`, `SCOUT_HASH = 139be9c73de16766cf2f6417bb6ee730604d7b26d2e7d86effcea7029f44f42f`, `ANALYST_HASH = cf4bc66b5cd4181716c4b5a686fa5639702bbe2c6021807742d6556d66fc972f`, `ANALYSIS_CONFIDENCE = MODERATE_CONFIDENCE` (`20` observational findings).
  - Served on `127.0.0.1:8001` via `src.fc27_demo_api:create_demo_app` with `SOURCE_CLASSIFICATION = LIVE_EA_CLUBS_COMMUNITY_CLIENT` (`"Live EA Clubs Data via pinned community client"`), while `FC27_COMMUNITY_PROVIDER_ENABLED=false` by default, `pandas` remains optional downstream (`src/fc27_pandas_view.py`), the browser-observed provider remains local experimental research excluded from `main`, and polling, Coach, Tactics, Director, recommendations, and dispatch remain disabled.
