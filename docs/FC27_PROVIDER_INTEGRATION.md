# FC 27 Provider Integration & Safe Adapter Boundary

> **Current Authorization Status**: `EA_APPROVED_ACCESS_REQUEST_DRAFT` (`LIVE_PROVIDER_TRAFFIC=BLOCKED_SAFE`)
>
> The FC 27 provider boundary (`src/fc27_authorized_api.py`, `src/fc27_authorized_provider.py`, `src/fc27_authorized_transport.py`, `src/fc27_clubs_api_contracts.py`, `src/fc27_api_cache.py`, `src/fc27_api_rate_limit.py`) is fully implemented and mock-validated (`217 passed` focused tests). Live external requests remain fail-closed (`FC27_PROVIDER_ENABLED=false`) until formal written authorization is recorded.

---

## 1. Provider-Neutral Architecture Overview

`iwa.dev.fcbuild` uses an **API-first, provider-neutral ingestion architecture** for East Coasters (`club_id=330323`, `platform=common-gen5`, `game_version=FC27`):

```
FC27AuthorizedProviderDeclaration + FC27AuthorizedTransportConfig
        │ (8 fail-closed gates verified before any operation)
        ▼
FC27AuthorizedHTTPTransport (standard Python urllib.request / HTTPS + TLS verification)
        │ (bounded token-bucket rate limiter + 3-state circuit breaker + 1-attempt budget)
        ▼
FC27ClubsEndpointContract (11 typed endpoint contracts + strict Pydantic response schemas)
        │ (raw JSON validated in-memory only; raw_response_retained=False)
        ▼
Normalization & Pseudonymization (plr_ecf_<12-hex> + FC27PseudonymCollisionDetector + 23-bit ev bitmap)
        │
        ├──► FC27BoundedNormalizedCache (stores normalized envelopes only; max 128 entries / 256 KiB)
        └──► FC27IngestionService / FC27Repository (PostgreSQL 16 persistence when FC27_PERSISTENCE_ENABLED=true)
```

> **Note on Operator-Captured Exports**: The offline JSON/CSV validation workflow (`scripts/fc27_user_export_import.py`, `scripts/fc27_capture_preflight.py`, and [`docs/FC27_EAST_COASTERS_EXPORT_PILOT_PACKAGE.md`](FC27_EAST_COASTERS_EXPORT_PILOT_PACKAGE.md)) is retained solely as a **secondary/offline fallback** and schema validation harness. It is **not** the primary acquisition architecture.

---

## 2. Authorization Requirements & Approved Provider Classifications

`src/fc27_provider_contract.py` defines `FC27AuthorizedProviderDeclaration` and permits only three classifications (`APPROVED_PROVIDER_CLASSIFICATIONS`):

1. `OFFICIAL_EA_PARTNER_API`
2. `LICENSED_THIRDPARTY_SPORTS_DATA_PROVIDER`
3. `LOCAL_LOOPBACK_MOCK_PROVIDER` (permitted only in local/CI mock testing against `127.0.0.1` / `localhost` / `::1`)

Every non-mock provider declaration requires:
- `terms_of_service_verified=True`
- `robots_and_acceptable_use_verified=True`
- `written_authorization_reference` (non-placeholder reference; values in `PLACEHOLDER_AUTHORIZATION_REFERENCES` such as `"PENDING"`, `"TBD"`, `"EA_APPROVED_ACCESS_REQUEST_DRAFT"`, `"NONE"` fail closed with `FC27ProviderAuthorizationError`)
- `raw_response_retention_prohibited=True`
- `member_pseudonymization_required=True`
- `allow_browser_impersonation=False`

---

## 3. Typed Endpoint Contracts (`src/fc27_clubs_api_contracts.py`)

Adapted safely from the MIT-licensed [`1erkandogan/fc27-clubs-api`](https://github.com/1erkandogan/fc27-clubs-api) endpoint and event specifications (see [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md)), excluding all `Impit`/Chromium/browser-impersonation components:

### 3.1 Eleven Endpoint Operations (`FC27ClubsEndpointOperation`)
| Operation Enum | Upstream Path | Target Scope Gate | Default Cache Policy |
|---|---|---|---|
| `SEARCH_CLUBS_BY_NAME` | `/allTimeLeaderboard/search` | Public Identity Lookup (`FC27_PUBLIC_IDENTITY_LOOKUP_ENABLED`) | `search_results` (`1800s` TTL) |
| `ALL_TIME_LEADERBOARD` | `/allTimeLeaderboard` | Public Identity Lookup (`FC27_PUBLIC_IDENTITY_LOOKUP_ENABLED`) | `leaderboard` (`900s` TTL) |
| `SEASON_LEADERBOARD` | `/seasonLeaderboard` | Public Identity Lookup (`FC27_PUBLIC_IDENTITY_LOOKUP_ENABLED`) | `leaderboard` (`900s` TTL) |
| `CLUB_DETAILS` | `/clubs/info` | Target Club Only (`clubIds=330323`) | `club_Summary` (`900s` TTL) |
| `CLUB_OVERALL_STATS` | `/clubs/overallStats` | Target Club Only (`clubIds=330323`) | `overall_stats` (`900s` TTL) |
| `CLUB_MATCH_HISTORY` | `/clubs/matches` | Target Club Only (`clubIds=330323`, `matchType` required) | `match_history` (`300s` TTL) |
| `MEMBER_CAREER_STATS` | `/members/career/stats` | Target Club Only (`clubId=330323`) | `member_stats` (`900s` TTL) |
| `MEMBER_SEASON_STATS` | `/members/stats` | Target Club Only (`clubId=330323`) | `member_stats` (`900s` TTL) |
| `CLUB_SETTINGS` | `/settings` | Target Club Only (`clubId=330323`) | `settings_metadata` (`86400s` TTL) |
| `CLUB_PLAYOFF_ACHIEVEMENTS` | `/club/playoffAchievements` | Target Club Only (`clubId=330323`) | `playoff_achievements` (`3600s` TTL) |
| `ASSET_TRANSLATIONS` | `/assets/translations` | Static Asset Metadata | `settings_metadata` (`86400s` TTL) |

### 3.2 Eight Adapter Operations (`FC27AuthorizedOperation`)
`FC27AuthorizedProviderAdapter` (`src/fc27_authorized_provider.py`) exposes:
- `FETCH_CLUB_SUMMARY` (`CLUB_DETAILS` + `CLUB_OVERALL_STATS`)
- `FETCH_MATCH_HISTORY` (`CLUB_MATCH_HISTORY`)
- `FETCH_MEMBER_STATISTICS` (`MEMBER_SEASON_STATS` + `MEMBER_CAREER_STATS`)
- `FETCH_EXPORT_BUNDLE` (composite normalized `FC27NormalizedExportEnvelope`)
- `FETCH_PUBLIC_CLUB_SEARCH` (`SEARCH_CLUBS_BY_NAME`, gated by `FC27_PUBLIC_IDENTITY_LOOKUP_ENABLED`)
- `FETCH_PUBLIC_LEADERBOARD` (`ALL_TIME_LEADERBOARD` / `SEASON_LEADERBOARD`, gated by `FC27_PUBLIC_IDENTITY_LOOKUP_ENABLED`)
- `FETCH_CLUB_SETTINGS` (`CLUB_SETTINGS`)
- `FETCH_PLAYOFF_ACHIEVEMENTS` (`CLUB_PLAYOFF_ACHIEVEMENTS`)

### 3.3 23-Bit Match Event Bitmap (`ev`) Decoder
`decode_match_event_bitmap(ev)` in `src/fc27_clubs_api_contracts.py` decodes bits `0..22` (`MATCH_EVENT_BIT_DEFINITIONS`, e.g., `SCORED_GOAL`, `ASSIST`, `RED_CARD`, `CLEAN_SHEET`, `HAT_TRICK`, `MAN_OF_THE_MATCH`) and flags any unknown higher bits (`>= bit 23`) in `unknown_bit_positions` without silent data loss.

---

## 4. Bounded Cache, Rate Limiter, Circuit Breaker, & Request Budget

> **Important Operational Disclosure**: All cache TTLs, token-bucket intervals, and request budgets in `src/fc27_api_cache.py` and `src/fc27_api_rate_limit.py` are **application-enforced protective defaults** designed to prevent accidental polling or burst traffic. They are **not** published EA rate-limit allowances.

1. **Bounded Normalized Cache (`src/fc27_api_cache.py`)**:
   - `FC27BoundedNormalizedCache` (`max_entries=128`, `max_payload_bytes=262_144`).
   - Rejects raw provider dicts/strings (`FC27RawCacheProhibitedError`); caches only validated, pseudonymized `FC27NormalizedExportEnvelope` or `FC27NormalizedPublicLookupResult` objects.
   - Enforces `stale_if_error_seconds=0` (never serves expired data when a provider error occurs).
2. **Token-Bucket Rate Limiter (`src/fc27_api_rate_limit.py`)**:
   - `FC27ProviderTokenBucketLimiter` defaults to `capacity=1` token and `refill_interval_seconds=300.0` (1 request per 5 minutes in mock mode).
   - Never sleeps or blocks threads; raises `FC27RateLimitExceededError` with `retry_after_seconds` when exhausted.
3. **Request Budget (`FC27RequestBudget`)**:
   - Enforces `max_attempts=1`, `max_retries=0`, `per_attempt_timeout_seconds=10.0`, `total_deadline_seconds=10.0`.
4. **3-State Circuit Breaker (`FC27AdapterCircuitBreaker`)**:
   - States: `OPEN`, `CLOSED`, `HALF_OPEN`. Defaults to `OPEN` when live provider traffic is unapproved (`initial_open=True`).
   - Any `HTTP 401`, `403`, `429`, `451`, or `5xx` failure trips the circuit breaker to `OPEN` immediately (`failure_threshold=1`, `cooldown_seconds=900.0`).
5. **Pure Retry Classification (`classify_fc27_retry_decision`)**:
   - Returns `should_retry=False` for all status codes under single-attempt safety rules (`max_retries=0`), classifying `401/403/451` as `TERMINAL_AUTH_OR_POLICY` and `429` as `RATE_LIMITED_NO_RETRY`.

---

## 5. Privacy Controls, Provenance, & Preflight CLI

- **Deterministic Pseudonymization**: Member IDs are transformed via `pseudonymize_member_id(club_id, raw_id)` -> `plr_ecf_<12-hex>`. Raw personas are discarded in memory (`raw_persona_persisted=False`).
- **Collision Guard**: `FC27PseudonymCollisionDetector` raises `FC27PseudonymCollisionError` if two distinct raw member keys ever map to the same pseudonym.
- **Provenance Metadata**: Every normalized envelope attaches `FC27SourceProvenance` (`provider_id`, `provider_classification`, `authorization_reference`, `fetched_at_utc`, `request_correlation_id`, `payload_sha256`, `endpoint_operation`, `cache_status`, `rate_limit_tokens_remaining`, `circuit_breaker_state`, `raw_response_retained=False`, `raw_persona_persisted=False`).
- **Preflight & Offline Lookup CLIs**:
  ```bash
  # Verify capture/export preflight posture (zero network calls)
  python scripts/fc27_capture_preflight.py --help

  # Verify club identity lookup gates (fails closed by default)
  python scripts/fc27_club_lookup.py --help
  ```
