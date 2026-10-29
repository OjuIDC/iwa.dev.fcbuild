# Feature Flags & Safety Gates (`iwa.dev.fcbuild`)

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
| `LM_STUDIO_FALLBACK_ENABLED` | `false` | `true`, `false` | Disables local LM Studio (`127.0.0.1:1234`) fallback in `src/llm_router.py` | Localhost-only (`validate_local_url` rejects remote hosts) | Local inference only | Platform Operator | [`ARCHITECTURE.md`](ARCHITECTURE.md) |
| `LANGFUSE_ENABLED` | `false` | `true`, `false` | Disables Langfuse telemetry export (`src/observability.py`) | Localhost-only (`LANGFUSE_HOST`) | Local observability metadata only | Platform Operator | [`OPERATIONS.md`](OPERATIONS.md) |
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
