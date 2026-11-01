# Feature Flags & Safety Gates (`iwa.dev.fcbuild`)

- **Status**: Current
- **Audience**: Operators, Developers, Security Reviewers
- **Last verified commit**: `b2c7bbda0d4a5f5788caa5e2cb74844c9bf5c5f0`
- **Source of truth**: `src/settings.py`, `src/graph_api.py`, `config.example.env`, `docker-compose.yml`
- **Supersedes**: `NONE`

Every network, ingestion, persistence, analytical, recommendation, authentication-fallback, observability, and outbound dispatch capability in `iwa.dev.fcbuild` is governed by explicit fail-closed feature flags defined in [`src/settings.py`](../src/settings.py) and [`config.example.env`](../config.example.env).

---

## 1. Master Feature Flag Matrix

| Flag Name | Default | Accepted Values | Fail-Closed Behavior When Default (`false` / `true`) | Dependency Gates | Permits Network / DB Writes / Recs / Dispatch | Operational Owner | Linked Documentation |
|---|---|---|---|---|---|---|---|
| `FC27_PROVIDER_ENABLED` | `false` | `true`, `false` | Blocks all provider adapter & transport calls (`FC27ProviderDisabledError` / `FC27TransportDisabledError`) | Root gate for all FC 27 provider operations | Network: No (unless child gates also enabled) | Platform Operator | [`FC27_PROVIDER_INTEGRATION.md`](FC27_PROVIDER_INTEGRATION.md) |
| `FC27_PUBLIC_IDENTITY_LOOKUP_ENABLED` | `false` | `true`, `false` | Blocks `SEARCH_CLUBS_BY_NAME`, `ALL_TIME_LEADERBOARD`, `SEASON_LEADERBOARD` (`FC27ScopeViolationError`) | Requires `FC27_PROVIDER_ENABLED=true` | Read-only public lookup only; no DB writes | Platform Operator | [`FC27_PROVIDER_INTEGRATION.md`](FC27_PROVIDER_INTEGRATION.md) |
| `FC27_UPSTREAM_COMPATIBILITY_ENABLED` | `false` | `true`, `false` | Blocks non-loopback upstream contract translation paths | Requires `FC27_PROVIDER_ENABLED=true` and written authorization | Contract translation only | Platform Operator | [`FC27_PROVIDER_INTEGRATION.md`](FC27_PROVIDER_INTEGRATION.md) |
| `FC27_STANDARD_HTTP_TRANSPORT_ENABLED` | `false` | `true`, `false` | Blocks `FC27AuthorizedHTTPTransport` socket execution (`FC27TransportDisabledError`) | Requires `FC27_PROVIDER_ENABLED=true` | Permits HTTPS transport to allowlisted provider host only | Platform Operator | [`FC27_PROVIDER_INTEGRATION.md`](FC27_PROVIDER_INTEGRATION.md) |
| `FC27_LIVE_INGESTION_ENABLED` | `false` | `true`, `false` | Blocks any non-loopback live provider request (`FC27ProviderAuthorizationError`) | Requires `FC27_PROVIDER_ENABLED=true`, `FC27_STANDARD_HTTP_TRANSPORT_ENABLED=true`, `FC27_PROVIDER_DRY_RUN=false`, and valid `FC27_PROVIDER_AUTH_REFERENCE` | Permits live authorized provider fetch when all gates pass | Platform Operator + Legal/API Approval | [`FC27_PROVIDER_INTEGRATION.md`](FC27_PROVIDER_INTEGRATION.md) |
| `FC27_API_CACHE_ENABLED` | `false` | `true`, `false` | Disables in-memory normalized response caching (`cache_status="BYPASS"`) | Used by `FC27AuthorizedProviderAdapter` | In-memory normalized cache only (`<= 128` entries) | Platform Operator | [`FC27_PROVIDER_INTEGRATION.md`](FC27_PROVIDER_INTEGRATION.md) |
| `FC27_API_RATE_LIMIT_ENABLED` | `false` | `true`, `false` | When enabled (`true` in adapter config), enforces token-bucket rate limit (`FC27RateLimitExceededError`) | Used by `FC27AuthorizedProviderAdapter` | Protective rate control (`1` req / `300s` default) | Platform Operator | [`FC27_PROVIDER_INTEGRATION.md`](FC27_PROVIDER_INTEGRATION.md) |
| `FC27_API_CIRCUIT_BREAKER_ENABLED` | `true` | `true`, `false` | Active by default (`is_fc27_api_circuit_breaker_enabled() -> True`); circuit breaker defaults to `OPEN` for unapproved live traffic (`FC27CircuitBreakerOpenError`) | Requires `FC27_LIVE_INGESTION_ENABLED=true` to transition to `CLOSED` for live hosts | Blocks traffic when `OPEN` | Platform Operator | [`FC27_PROVIDER_INTEGRATION.md`](FC27_PROVIDER_INTEGRATION.md) |
| `FC27_PROVIDER_DRY_RUN` | `true` | `true`, `false` | Enforces dry-run validation; prevents live persistence from provider calls | Must be `false` (with `FC27_PERSISTENCE_ENABLED=true`) before persisting provider data | Blocks DB writes when `true` | Platform Operator | [`FC27_PROVIDER_INTEGRATION.md`](FC27_PROVIDER_INTEGRATION.md) |
| `FC27_PERSISTENCE_ENABLED` | `false` | `true`, `false` | Blocks `FC27IngestionService` from writing to `fc27_*` PostgreSQL tables (`FC27PersistenceDisabledError`) | Requires verified `ClubIdentityState.VERIFIED_CLUB_ID` (`330323`) | Permits normalized PostgreSQL writes to `fc27_*` tables | Platform Operator | [`FC27_DATA_MODEL.md`](FC27_DATA_MODEL.md) |
| `FC27_ANALYTICS_ENABLED` | `false` | `true`, `false` | Keeps runtime dashboard/analytics endpoints gated (`FC27AnalyticsDisabledError`) | Reads only from normalized `fc27_*` tables | Read-only analytics; no DB writes, no recs, no dispatch | Platform Operator | [`FC27_ANALYTICS.md`](FC27_ANALYTICS.md) |
| `FC27_RECOMMENDATIONS_ENABLED` | `false` | `true`, `false` | Blocks coaching recommendation generation/submission (`FC27RecommendationsDisabledError`) | Requires `FC27_ANALYTICS_ENABLED=true` and `validate_coaching_recommendation` pass | Permits coaching package creation only; never approves or dispatches | Commissioner / Platform Operator | [`FC27_APPROVAL_FLOW.md`](FC27_APPROVAL_FLOW.md) |
| `DISPATCH_ENABLED` | `false` | `true`, `false` | Hard-blocks outbound player dispatch (`Phase 1 safety invariant violation` if enabled without gates) | Requires all 6 gates in `docs/DISPATCH_ACTIVATION.md` (`G-MIGRATION`..`G-OUTBOX`) | Outbound dispatch prohibited while `false` | Commissioner + Security Owner | [`DISPATCH_ACTIVATION.md`](DISPATCH_ACTIVATION.md) |
| `DISPATCH_WORKER_ENABLED` | `false` | `true`, `false` | Prevents `src/dispatch/worker.py` from polling `dispatch_outbox` (`RuntimeError`) | Requires `DISPATCH_ENABLED=true` and Compose `--profile dispatch` | Outbox worker execution | Platform Operator | [`DISPATCH_ACTIVATION.md`](DISPATCH_ACTIVATION.md) |
| `APPROVAL_CLI_HMAC_ENABLED` | `false` | `true`, `false` | Rejects symmetric HMAC CLI decisions; enforces WebAuthn passkey step-up only | Requires `APPROVAL_SIGNING_SECRET` (`>= 32` bytes) | Emergency Commissioner CLI step-up only | Commissioner | [`SECURITY_BOUNDARIES.md`](SECURITY_BOUNDARIES.md) |
| `LM_STUDIO_FALLBACK_ENABLED` | `false` | `true`, `false` | Disables local LM Studio (`127.0.0.1:1234`) fallback in `src/model_selection.py` | Localhost-only (`validate_local_url` rejects remote hosts) | Local inference only | Platform Operator | [`ARCHITECTURE.md`](ARCHITECTURE.md) |
| `LANGFUSE_ENABLED` | `false` | `true`, `false` | Disables Langfuse telemetry export (`src/tracing.py`) | Localhost-only (`LANGFUSE_HOST`) | Local observability metadata only | Platform Operator | [`OPERATIONS.md`](OPERATIONS.md) |
| `LANGFUSE_CAPTURE_CONTENT` | `false` | `true`, `false` | Redacts prompt/completion payloads even when `LANGFUSE_ENABLED=true` | Requires `LANGFUSE_ENABLED=true` | Telemetry content capture (keep `false` in production) | Security Owner | [`SECURITY_BOUNDARIES.md`](SECURITY_BOUNDARIES.md) |

---

## 2. Fail-Closed Dependency Chain

No downstream stage can bypass an upstream gate:

```
FC27_PROVIDER_ENABLED=false
  └──► FC27_STANDARD_HTTP_TRANSPORT_ENABLED=false
         └──► FC27_LIVE_INGESTION_ENABLED=false (also requires FC27_PROVIDER_DRY_RUN=false)
                └──► FC27_PERSISTENCE_ENABLED=false
                       └──► FC27_ANALYTICS_ENABLED=false
                              └──► FC27_RECOMMENDATIONS_ENABLED=false
                                     └──► Commissioner WebAuthn Approval (human-in-the-loop)
                                            └──► DISPATCH_ENABLED=false / DISPATCH_WORKER_ENABLED=false
```

---

## 3. Local Demo Flags & Bounded Parameters (`docs/FC27_DEMO.md`)

The local East Coasters FC 27 one-shot ingestion and analytics demo (`src/fc27_demo.py`, `src/fc27_demo_api.py`, `scripts/fc27_demo_ingest.py`) is governed by dedicated fail-closed demo settings in `src/settings.py` and `config.example.env`:

- **`FC27_DEMO_MODE`** (default `false`; accepted `true`, `false`): Master gate for `/demo` and `/demo/*` routes in `src/fc27_demo_api.py` (`is_fc27_demo_mode_enabled()`). Returns HTTP `403` (`DEMO_MODE_DISABLED`) when `false`.
- **`FC27_DEMO_SOURCE`** (default `fixture`; accepted `fixture`, `live`): Selects the demo data source (`get_fc27_demo_source()`). Live probe execution remains disabled (`LIVE_PROBE_EXECUTED=NO`).
- **`FC27_DEMO_BIND_HOST`** (default `127.0.0.1`; accepted `127.0.0.1` only): Enforces loopback-only socket binding (`get_fc27_demo_bind_host()`). Any non-loopback host (`0.0.0.0`, external IP) fails closed with `DEMO_BIND_HOST_INVALID`.
- **`FC27_DEMO_MAX_REQUESTS`** (default `1`; accepted `1`): Caps per-run transport requests (`get_fc27_demo_max_requests()`) to prevent polling or multi-request loops.
- **`FC27_DEMO_MAX_RETRIES`** (default `0`; accepted `0`): Prohibits transport retries (`get_fc27_demo_max_retries()`).
- **`FC27_DEMO_RAW_RESPONSE_RETENTION`** (default `false`; accepted `false` only): Prohibits raw HTTP response retention (`is_fc27_demo_raw_response_retention_enabled()`); setting `true` fails closed with `RAW_RESPONSE_RETENTION_FORBIDDEN`.
- **`FC27_DEMO_PORT`** (default `8097`; accepted `0` or `1024..65535`): Local port for `src/fc27_demo_api.py` (`get_fc27_demo_port()`).
- **`FC27_COMMUNITY_PROVIDER_ENABLED`** (default `false`; accepted `true`, `false`): Master gate for the bounded one-shot community client wrapper in `src/fc27_community_provider.py` and `scripts/fc27_community_eval.py` (`is_fc27_community_provider_enabled()`). Requires explicit `--confirm` on the CLI; see [`FC27_COMMUNITY_PROVIDER_EVALUATION.md`](FC27_COMMUNITY_PROVIDER_EVALUATION.md).

---

## 4. Disabled-by-Default Community Polling Worker Flags (`docs/FC27_POLLING_WORKER.md`)

The East Coasters FC 27 community polling worker (`src/fc27_polling_worker.py`, `scripts/fc27_polling_worker.py`, `docker-compose.community-worker.yml`) is governed by fail-closed settings in `src/settings.py` and `config.example.env`:

- **`FC27_COMMUNITY_POLLING_ENABLED`** (default `false`; accepted `true`, `false`): Master gate for polling cycle execution (`is_fc27_community_polling_enabled()`). Returns `POLLING_DISABLED` with `requests_executed=0` when `false`.
- **`FC27_COMMUNITY_POLL_INTERVAL_SECONDS`** (default `43200`; accepted `43200` only): Fixed 12-hour polling interval in seconds (`get_fc27_community_poll_interval_seconds()`).
- **`FC27_COMMUNITY_POLL_MIN_SECONDS`** (default `43200`; accepted `43200` only): Minimum sleep interval in seconds (`get_fc27_community_poll_min_seconds()`). Fails closed on `!= 43200`.
- **`FC27_COMMUNITY_POLL_MAX_SECONDS`** (default `43200`; accepted `43200` only): Maximum sleep interval in seconds (`get_fc27_community_poll_max_seconds()`). Fails closed on `!= 43200`.
- **`FC27_COMMUNITY_POLL_MAX_REQUESTS`** (default `1`; accepted `1` only): Caps provider HTTP requests per poll cycle (`get_fc27_community_poll_max_requests()`). Fails closed on any value `!= 1`.
- **`FC27_COMMUNITY_POLL_MAX_RETRIES`** (default `0`; accepted `0` only): Prohibits automatic retries during polling (`get_fc27_community_poll_max_retries()`). Fails closed on any value `!= 0`.
- **`FC27_COMMUNITY_POLL_ANALYTICS_ON_NEW_ONLY`** (default `true`; accepted `true` only): Ensures `Search -> Scout -> Analyst -> Coach -> Tactics -> Director` executes only when `inserted_match_count > 0` (`is_fc27_community_poll_analytics_on_new_only()`).
- **`FC27_COMMUNITY_POLL_SINGLE_FLIGHT`** (default `true`; accepted `true` only): Enforces in-process and PostgreSQL advisory single-flight lock per poll cycle (`is_fc27_community_poll_single_flight()`).
- **`FC27_COMMUNITY_POLL_ONCE`** (default `false`; accepted `true`, `false`): Causes the polling worker loop to exit after at most 1 bounded poll cycle (`is_fc27_community_poll_once()`).
- **`FC27_DISCORD_BOT_ENABLED`** (default `false`; accepted `true`, `false`): Master gate for the read-only East Coasters Discord analytics bot (`is_fc27_discord_bot_enabled()`). Requires non-empty `FC27_DISCORD_ALLOWED_GUILD_IDS`, `FC27_DISCORD_ALLOWED_CHANNEL_IDS`, and `DISCORD_BOT_TOKEN_FILE`; see [`FC27_DISCORD_BOT.md`](FC27_DISCORD_BOT.md).
