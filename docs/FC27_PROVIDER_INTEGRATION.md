# FC 27 Provider Integration & Safe Adapter Boundary

- **Status**: Current
- **Audience**: Developers, Architects, Security Reviewers
- **Last verified commit**: `b2c7bbda0d4a5f5788caa5e2cb74844c9bf5c5f0`
- **Source of truth**: `src/fc27_community_provider.py`, `src/fc27_authorized_api.py`, `src/fc27_authorized_provider.py`, `src/fc27_authorized_transport.py`, `src/fc27_provider_contract.py`, `src/fc27_clubs_api_contracts.py`, `src/fc27_api_cache.py`, `src/fc27_api_rate_limit.py`, `config/fc27_safe_adapter_bundle_manifest.json`, `THIRD_PARTY_NOTICES.md`
- **Supersedes**: `docs/archive/providers/mcp_ws.md`

> **Current Working Real-Data Provider (`fc-clubs-api-community-v1` / `LIVE_EA_CLUBS_COMMUNITY_CLIENT`)**:
>
> The current supported real-data acquisition path for East Coasters (`club_id=330323`, `platform=common-gen5`, `game_version=FC27`) is the bounded one-shot community client wrapper in `src/fc27_community_provider.py` and `scripts/fc27_community_eval.py` (`Dockerfile.community-eval`, `docker-compose.community-eval.yml`), which successfully retrieved, validated, normalized, pseudonymized (`plr_ecf_<12-hex>`), persisted (`10` matches, `60` player rows), and analyzed (`Search -> Scout -> Analyst`) real East Coasters match data with `raw_response_retained=False`. See [`docs/FC27_COMMUNITY_PROVIDER_EVALUATION.md`](FC27_COMMUNITY_PROVIDER_EVALUATION.md) for full details.
>
> **Historical Transport Findings Recorded (`PART D`)**:
> - **Standards-Compliant Python `urllib` Live Probe (`feature/fc27-real-east-coasters-ingestion` @ `76d20c4ef6`)**: Confirmed deterministic HTTP `403 Forbidden` at the Akamai edge when calling `https://proclubs.ea.com/api/fc/clubs/info` with standard Python `urllib` headers and TLS profile (`STANDARD_LIVE_PROBE_ACTIVE=NO`).
> - **Bounded Headless Chromium Browser-Observed Provider (`feature/fc27-browser-observed-provider` @ `414b4f3d67`)**: Confirmed official public page URL (`https://www.ea.com/games/ea-sports-fc/clubs/overview?clubId=330323&platform=common-gen5`) and `common-gen5` platform, while headless Chromium navigation encountered `ERR_HTTP2_PROTOCOL_ERROR` across both Windows host and Linux container environments (`BROWSER_PROVIDER_ACTIVE=NO`).
> - **Direct Partner/EA Written Authorization Boundary (`EA_APPROVED_ACCESS_REQUEST_DRAFT`)**: The provider-neutral `FC27AuthorizedProviderAdapter` (`src/fc27_authorized_provider.py`) remains retained as a supporting contract/adapter layer (`FC27_PROVIDER_ENABLED=false` by default).

---

## 1. Provider-Neutral Architecture Overview

`iwa.dev.fcbuild` uses an **API-first, provider-neutral ingestion architecture** for East Coasters (`club_id=330323`, `platform=common-gen5`, `game_version=FC27`):

```text
FC27AuthorizedProviderDeclaration + FC27AuthorizedTransportConfig
        │ (fail-closed gates verified before any operation)
        ▼
FC27AuthorizedTransportClient (standard HTTPS GET + TLS verification; 0 redirects, 0 cookies)
        │ (bounded token-bucket rate limiter + 3-state circuit breaker + 1-attempt budget)
        ▼
FC27ClubsEndpointOperation (11 typed endpoint contracts + 13 strict Pydantic response schemas)
        │ (raw JSON validated in-memory only; raw_response_retained=False)
        ▼
Normalization & Pseudonymization (plr_ecf_<12-hex> + FC27PseudonymCollisionDetector + 23-bit ev bitmap)
        │
        ├──► FC27BoundedNormalizedCache (stores normalized objects only; max 128 entries / 256 KiB)
        └──► FC27PerformanceRepository (PostgreSQL 16 persistence when FC27_PERSISTENCE_ENABLED=true)
```

> **Note on Operator-Captured Exports**: The offline JSON/CSV validation workflow (`src/fc27_user_export.py`, `src/fc27_capture.py`, `scripts/fc27_user_export_import.py`, `scripts/fc27_capture_preflight.py`, and [`docs/FC27_EAST_COASTERS_EXPORT_PILOT_PACKAGE.md`](FC27_EAST_COASTERS_EXPORT_PILOT_PACKAGE.md)) is retained solely as a **secondary/offline fallback** and schema validation harness. It is **not** the primary acquisition architecture.

---

## 2. Authorization Requirements & Approved Provider Classifications

`src/fc27_authorized_provider.py` and `src/fc27_authorized_api.py` define `FC27AuthorizedProviderDeclaration` and `FC27SourceClassification`:

- Allowed live classifications: `EA_DIRECT_APPROVED` and `LICENSED_PARTNER_API` (plus `MOCK_CONTRACT_FIXTURE` for hermetic local tests).
- Unapproved direct scraping (`UNAPPROVED_DIRECT_EA`), browser-impersonation sidecars (`PROCLUBS_SDK_SIDECAR`), and unlicensed scrapers (`THIRD_PARTY_UNLICENSED`) are permanently rejected.
- Every live provider declaration requires verified Terms of Service, privacy compliance, a non-placeholder written authorization reference (`EA_APPROVED_ACCESS_REQUEST_DRAFT` is a draft placeholder and fails closed for live traffic), `raw_response_retained=False`, and zero browser/TLS impersonation.

---

## 3. Typed Endpoint Contracts (`src/fc27_clubs_api_contracts.py`)

Adapted safely from the MIT-licensed `1erkandogan/fc27-clubs-api` endpoint and event data specifications (see [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md)), excluding all `Impit`/Chromium/browser-impersonation components. These contracts are community-documented endpoint schemas, **not** an official EA SDK:

### 3.1 Eleven Endpoint Operations (`FC27ClubsEndpointOperation`)
| Operation Enum | Upstream Path | Target Scope Gate | Application Default Cache TTL |
|---|---|---|---|
| `SEARCH_CLUBS_BY_NAME` | `/allTimeLeaderboard/search` | Public Identity Lookup (`FC27_PUBLIC_IDENTITY_LOOKUP_ENABLED`) | `1800s` (30m) |
| `ALL_TIME_LEADERBOARD` | `/allTimeLeaderboard` | Public Identity Lookup (`FC27_PUBLIC_IDENTITY_LOOKUP_ENABLED`) | `900s` (15m) |
| `SEASON_LEADERBOARD` | `/seasonLeaderboard` | Public Identity Lookup (`FC27_PUBLIC_IDENTITY_LOOKUP_ENABLED`) | `900s` (15m) |
| `CLUB_DETAILS` | `/clubs/info` | Target Club Only (`clubIds=330323`) | `900s` (15m) |
| `CLUB_OVERALL_STATS` | `/clubs/overallStats` | Target Club Only (`clubIds=330323`) | `900s` (15m) |
| `CLUB_MATCH_HISTORY` | `/clubs/matches` | Target Club Only (`clubIds=330323`, `matchType` required) | `300s` (5m) |
| `MEMBER_CAREER_STATS` | `/members/career/stats` | Target Club Only (`clubId=330323`) | `900s` (15m) |
| `MEMBER_SEASON_STATS` | `/members/stats` | Target Club Only (`clubId=330323`) | `900s` (15m) |
| `CLUB_SETTINGS` | `/settings` | Target Club Only (`clubId=330323`) | `86400s` (24h) |
| `CLUB_PLAYOFF_ACHIEVEMENTS` | `/club/playoffAchievements` | Target Club Only (`clubId=330323`) | `3600s` (1h) |
| `ASSET_TRANSLATIONS` | `/assets/translations` | Static Asset Metadata | `86400s` (24h) |

### 3.2 Eight Adapter Operations (`FC27AuthorizedOperation`)
`FC27AuthorizedProviderAdapter` (`src/fc27_authorized_provider.py`) exposes:
- `FETCH_CLUB_SUMMARY` (`club_summary`)
- `FETCH_MATCH_HISTORY` (`match_history`)
- `FETCH_MEMBER_STATISTICS` (`member_statistics`)
- `FETCH_EXPORT_BUNDLE` (`export_bundle`)
- `FETCH_PUBLIC_CLUB_SEARCH` (`public_club_search`)
- `FETCH_PUBLIC_LEADERBOARD` (`public_leaderboard`)
- `FETCH_CLUB_SETTINGS` (`club_settings`)
- `FETCH_PLAYOFF_ACHIEVEMENTS` (`playoff_achievements`)

### 3.3 23-Bit Match Event Bitmap (`ev`) Decoder
`decode_match_event_bitmap(ev)` in `src/fc27_clubs_api_contracts.py` decodes bits `0..22` (`MATCH_EVENT_BIT_DEFINITIONS`) and flags any unknown higher bits (`>= bit 23`) in `unknown_bits_mask` / `unknown_bit_positions` without silent data loss.

---

## 4. Bounded Cache, Rate Limiter, Circuit Breaker, & Request Budget

> **Important Operational Disclosure**: All cache TTLs, token-bucket intervals, and request budgets in `src/fc27_api_cache.py` and `src/fc27_api_rate_limit.py` are **application-enforced protective defaults** designed to prevent accidental polling or burst traffic. They are **not** published EA rate-limit allowances.

1. **Bounded Normalized Cache (`src/fc27_api_cache.py`)**:
   - `FC27BoundedNormalizedCache` (`max_entries=128`, `max_value_bytes=262_144`, `FC27_API_CACHE_ENABLED=false` by default).
   - Rejects raw provider dicts/strings/bytes; caches only validated, pseudonymized normalized objects.
   - Enforces `stale_if_error=False` (never serves expired data when a provider error occurs).
2. **Token-Bucket Rate Limiter (`src/fc27_api_rate_limit.py`)**:
   - `FC27ProviderTokenBucketLimiter` (`FC27_API_RATE_LIMIT_ENABLED=false` by default) enforces `1` token per `300.0` seconds (`max_queue_depth=0`) as a conservative default.
3. **Request Budget (`FC27RequestBudget`)**:
   - Enforces `max_attempts=1`, `max_retries=0`, `max_elapsed_seconds=10.0`.
4. **3-State Circuit Breaker (`FC27AdapterCircuitBreaker`)**:
   - States: `OPEN`, `CLOSED`, `HALF_OPEN` (`FC27_API_CIRCUIT_BREAKER_ENABLED=true` by default). Defaults to `OPEN` when live provider traffic is unapproved (`SELECTED_PROVIDER=NONE`).
   - Any `HTTP 401`, `403`, redirect, HTML challenge, CAPTCHA, or sensitive-field violation trips the circuit breaker to `OPEN` immediately.
5. **Pure Retry Classification (`classify_fc27_retry_decision`)**:
   - Returns `should_retry=False` by default (`max_retries=0`) and classifies `401`/`403`/redirects/challenges as non-retryable terminal errors.

---

## 5. Privacy Controls, Provenance, & Offline Preflight CLI

- **Deterministic Pseudonymization**: Member IDs are transformed via `pseudonymize_club_member_id(raw_id, club_id="330323")` -> `plr_ecf_<12-hex>`. Raw personas are discarded (`raw_persona_persisted=False`, `display_alias=None`).
- **Collision Guard**: `FC27PseudonymCollisionDetector` aborts with `PSEUDONYM_COLLISION` if two distinct raw member keys ever map to the same pseudonym.
- **Provenance Metadata**: Every normalized envelope attaches `FC27SourceProvenance` (`provider_id`, `provider_classification`, `authorization_reference`, `fetched_at_utc`, `request_correlation_id`, `payload_sha256`, `endpoint_operation`, `cache_status`, `rate_limit_tokens_remaining`, `circuit_breaker_state`, `raw_response_retained=False`, `raw_persona_persisted=False`).
- **Offline Preflight & Lookup CLIs**:
  ```powershell
  .\.venv\Scripts\python.exe scripts/fc27_capture_preflight.py
  .\.venv\Scripts\python.exe scripts/fc27_capture_preflight.py --mock
  .\.venv\Scripts\python.exe scripts/fc27_club_lookup.py --help
  ```

---

## 6. Repository-Native Safe Adapter Bundle Configuration (`config/fc27_safe_adapter_bundle_manifest.json`)

The verified safe-adapter bundle (`BUNDLE_NAME=iwa.dev.fcbuild-safe-adapter-verified.zip`, `BUNDLE_SHA256=eb9c42745f9966aa6af197c1b289809575c703d0d4ccef05e4c9678370f301a8`) is governed by a repository-native provenance manifest (`MANIFEST_VERSION=fc27-safe-adapter-manifest-v1` in `config/fc27_safe_adapter_bundle_manifest.json`) and validated deterministically by `tests/test_fc27_safe_adapter_bundle_manifest.py`:

1. **Original bundle artifact**:
   - Stored only as a git-ignored local input under `.local-artifacts/` (`iwa.dev.fcbuild-safe-adapter-verified.zip`).
   - Never committed to Git and never required by CI or production runtime.
2. **Temporary inspection workspace**:
   - Extracted locally under `.local-artifacts/safe-adapter-inspection-eb9c42745f99` (with local inventory map `.local-artifacts/safe-adapter-folder-map.json`) strictly for static verification.
   - Never committed to Git and never imported at runtime (`no zipimport` or `.local-artifacts` imports).
3. **Repository-native integration**:
   - Tracked source and test modules (`src/fc27_clubs_api_contracts.py`, `src/fc27_api_cache.py`, `src/fc27_api_rate_limit.py`, `src/fc27_authorized_provider.py`, `src/fc27_authorized_transport.py`, `src/fc27_provider_contract.py`, `src/fc27_authorized_api.py`, and `tests/test_fc27_*.py`).
   - Tracked provenance manifest `config/fc27_safe_adapter_bundle_manifest.json` (`fc27-safe-adapter-manifest-v1`) recording exact SHA-256 digests, adaptation summaries, feature flags, and rejected unsafe/non-applicable components.
   - Tracked upstream MIT license attribution in `THIRD_PARTY_NOTICES.md`.

---

## 7. Bounded Community Client Evaluation (`src/fc27_community_provider.py`) & Disabled-by-Default Polling Worker (`src/fc27_polling_worker.py`)

For isolated one-shot evaluation and persistence of the pinned MIT-licensed `1erkandogan/fc27-clubs-api@26a5ff12ea793042bd776a858c74f3f44dd427f5` (`v0.3.1`) unofficial community client (`fc-clubs-api-community-v1`, not an official EA SDK), `src/fc27_community_provider.py` and `scripts/fc27_community_eval.py` provide a strictly gated wrapper (`FC27_COMMUNITY_PROVIDER_ENABLED=false` by default, `--confirm` required) under `SOURCE_CLASSIFICATION = LIVE_EA_CLUBS_COMMUNITY_CLIENT`, with optional downstream tabular helpers in `src/fc27_pandas_view.py`. See [`FC27_COMMUNITY_PROVIDER_EVALUATION.md`](FC27_COMMUNITY_PROVIDER_EVALUATION.md).

In addition, `src/fc27_polling_worker.py` and `scripts/fc27_polling_worker.py` (`Dockerfile.community-worker`, `docker-compose.community-worker.yml`, service `fc27-community-worker` under opt-in profile `fc27-polling`) wrap `FC27CommunityProviderAdapter` in a disabled-by-default single-flight polling worker (`FC27_COMMUNITY_POLLING_ENABLED=false` by default, `300..600s` interval, `1` request per cycle, `0` retries, conditional `Search -> Scout -> Analyst` only when new matches are inserted). See [`FC27_POLLING_WORKER.md`](FC27_POLLING_WORKER.md).
