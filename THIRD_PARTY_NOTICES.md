# Third-Party Notices and Upstream Attribution

## 1. `1erkandogan/fc27-clubs-api` (Audited Non-Transport Subset)

- **Upstream Repository**: https://github.com/1erkandogan/fc27-clubs-api
- **Reviewed Upstream Commit**: `26a5ff12ea793042bd776a858c74f3f44dd427f5`
- **Reviewed Upstream Version**: `0.3.1`
- **Upstream License**: `MIT`
- **Verified Safe Adapter Bundle**: `iwa.dev.fcbuild-safe-adapter-verified.zip`
- **Verified Bundle SHA-256**: `eb9c42745f9966aa6af197c1b289809575c703d0d4ccef05e4c9678370f301a8`
- **Tracked Repository-Native Manifest**: `config/fc27_safe_adapter_bundle_manifest.json` (`fc27-safe-adapter-manifest-v1`)

### Important Scope and Licensing Notice
The MIT license below governs **only** the open-source software structure and code artifacts adapted from `1erkandogan/fc27-clubs-api`. It does **not** grant any license, permission, or right to access Electronic Arts Inc. ("EA") infrastructure, bypass EA edge access controls, or store/redistribute EA SPORTS FC Clubs data. Live provider access in `iwa.dev.fcbuild` remains disabled by default (`FC27_PROVIDER_ENABLED=false`) pending an approved provider declaration and written authorization.

### Component Provenance Matrix

| Upstream File / Symbol | Local Replacement File / Symbol | Adoption Classification | Behavioral Changes & Safety Hardening |
| :--- | :--- | :--- | :--- |
| `src/fc_clubs_api/enums.py` (`Platform`, `MatchType`) & bundle `fcbuild_clubs/contracts.py` (`Endpoint`, `MatchType`, `CONTRACTS`) | `src/fc27_clubs_api_contracts.py` (`FC27ClubsEndpointOperation`, `FC27UpstreamMatchType`, `FC27_ENDPOINT_CONTRACTS`) | `ADAPTED_WITH_LICENSE` | Restricted to `common-gen5` and `FC27` for East Coasters (`330323`); fixed 11 operation contracts with exact path and query allowlists; zero arbitrary URL/path methods. |
| `src/fc_clubs_api/_columns.py` & bundle `fcbuild_clubs/schema.py` (`validate`) | `src/fc27_clubs_api_contracts.py` (`FC27ClubSearchResultSchema`, `FC27ClubIdentitySchema`, `FC27ClubAggregateStatisticsSchema`, `FC27MatchEnvelopeSchema`, `FC27ClubSideMatchRecordSchema`, `FC27AggregateMatchStatisticsSchema`, `FC27PlayerMatchStatisticsSchema`, `FC27CurrentMemberStatisticsSchema`, `FC27CareerMemberStatisticsSchema`, `FC27PlayoffAchievementSchema`, `FC27LeaderboardEntrySchema`, `FC27MatchEventBitmapSchema`, `FC27AssetAndCrestMetadataSchema`) | `ADAPTED_WITH_LICENSE` | Reimplemented as strict immutable Pydantic v2 schemas with bounded strings/arrays/integers, finite number checks, UTC normalization, score/result consistency, completed<=attempted checks, duplicate detection, and schema-drift classification. |
| `src/fc_clubs_api/_normalize.py` & bundle `fcbuild_clubs/normalize.py` | `src/fc27_clubs_api_contracts.py` (`normalize_club_identity_payload`, `normalize_club_statistics_payload`, `normalize_matches_payload`, `normalize_member_statistics_payload`, `normalize_playoff_achievements_payload`, `normalize_fc27_match_type`) | `ADAPTED_WITH_LICENSE` | Maps into authoritative `iwa.dev.fcbuild` domain models (`ClubIdentityRecord`, `FC27SuppliedClubStatistics`, `FC27SuppliedMatch`, `FC27SuppliedPlayerPerformance`, `FC27NormalizedMemberStatistic`, `FC27NormalizedPlayoffAchievement`); suppresses gamer tags, aliases, EA/Persona/Nucleus IDs, and external player IDs; applies East Coasters `plr_ecf_<12-hex>` pseudonymization with collision rejection. |
| `src/fc_clubs_api/events.py` | `src/fc27_clubs_api_contracts.py` (`validate_event_bitmap`, `enumerate_event_bits`, `decode_fc27_match_events`, `validate_aggregate_event_counts`) | `ADAPTED_WITH_LICENSE` | Pure deterministic bit enumeration and decoding (`fc27-events-1.0.0`); preserves unknown bit positions as unknown with `review_required=True`; never generates cheating or fraud labels. |
| `src/fc_clubs_api/assets.py` | `src/fc27_clubs_api_contracts.py` (`FC27AssetAndCrestMetadataSchema`, `normalize_asset_and_crest_metadata`) | `CONCEPT_ONLY` | Normalizes numeric asset and crest IDs as metadata-only values without constructing external CDN URLs or executing network fetches. |
| Bundle `fcbuild_clubs/resilience.py` (`TTLCache`, `TokenBucketRateLimiter`, `RequestBudget`, `CircuitBreaker`) | `src/fc27_api_cache.py`, `src/fc27_api_rate_limit.py`, `src/fc27_authorized_provider.py` | `ADAPTED_WITH_LICENSE` | Separated into dedicated bounded normalized-object cache (`FC27BoundedNormalizedCache`, disabled by default, max 128 entries, max 262,144 bytes, raw-response/PII rejection) and provider-scoped token-bucket rate limiter (`FC27ProviderTokenBucketLimiter`, disabled by default, conservative 1 token / 300s mock default), request budget (`MAX_ATTEMPTS=1`, `MAX_RETRIES=0`), circuit breaker (`OPEN` by default without approved provider), and pure retry classifier. |
| `src/fc_clubs_api/_http.py` | **None (Rejected)** | `REJECTED` | Upstream module uses spoofed Chrome 141 `User-Agent`, `sec-ch-ua`, `Sec-Fetch-*`, `Origin`, and `Referer` headers to bypass HTTP 403 filtering. Never copied or enabled. |
| `src/fc_clubs_api/client.py:get_json` & `find_club_id` | **None (Rejected)** | `REJECTED` | Arbitrary endpoint caller and automatic club-ID replacement are prohibited. |
| `src/fc_clubs_api/_output.py` & `__main__.py` | **None (Rejected)** | `REJECTED` | Raw payload output and CLI scraping entrypoints are prohibited. |

### MIT License Notice

```text
MIT License

Copyright (c) 2025 Erkan Dogan

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
