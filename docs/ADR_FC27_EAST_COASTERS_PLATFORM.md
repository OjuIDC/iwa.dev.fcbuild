# Architecture Decision Record: FC 27 East Coasters Team-Performance & Coaching Platform

## Status
Accepted

## Date
2026-10-25

## Context & Motivation
The private `iwa.dev.fcbuild` platform is being refocused and evolved into an FC 27 Pro Clubs team-performance, build-optimization, and coaching platform dedicated to the East Coasters.

The platform exists to answer a single guiding product question:
> **"What combination of player builds, roles, tactics, formations, and behaviors gives the East Coasters the best chance of improving and winning?"**

The platform addresses this challenge by synthesizing:
1. FC 27 match and player data ingestion
2. Individual player-performance analysis
3. Player-build creation and optimization
4. Role and position suitability analysis
5. Lineup and formation evaluation
6. Player chemistry and partnership analysis
7. Tactical tendencies and team-level performance diagnostics
8. Concrete, actionable recommendations tailored for the East Coasters

---

## Architectural Principles

### 1. Functional Core, Imperative Shell
- **Functional Core (`src/fc27_analytics.py`)**:
  All analytics calculations, scoring rubrics, partnership metrics, formation evaluations, and match-over-match trend calculations are implemented as pure, deterministic functions without side effects, I/O, or hidden state.
  All formulas are versioned (e.g. `formula_version="fc27-v1.0"`).
- **Domain Contracts (`src/schemas.py`)**:
  Strictly typed, versioned Pydantic domain models define the contract for `ProviderEnvelope`, `ClubMatch`, `PlayerMatch`, `PlayerBuild`, `Lineup`, `TeamSnapshot`, and `EastCoastersRecommendation`.
  Fail-closed schema validation rejects unknown positions, roles, playstyles, or invalid attribute values without silent coercion.
- **Provider Layer (`src/fc27_provider.py`)**:
  Abstracts raw data ingestion behind normalized envelopes. Ingestion from external streams (such as MatchPulse) is fully decoupled. The platform operates self-sufficiently with fixture-backed synthetic providers.
- **Coaching Engine (`src/fc27_coaching.py`)**:
  A deterministic, rule-grounded coaching layer that synthesizes analytical findings into structured recommendations for the East Coasters.
  Recommendations are categorized into tactical, build, lineup, role, or behavioral guidance, backed by explicit empirical evidence and sample-size confidence tiers.

### 2. MatchPulse Integration Boundary
- MatchPulse is strictly an external, optional telemetry source.
- `iwa.dev.fcbuild` has zero runtime imports, direct dependencies, or shared code execution with `matchpulse-live-public`.
- Data exchange occurs solely via versioned schema envelopes (`ProviderEnvelope`) containing checksums, timestamps, and data quality flags.

### 3. Sample Size Governance & Honest Uncertainty
All analytics and coaching outputs MUST disclose their sample size and evidence confidence:
- **Tier 0 (N = 0 to 2 matches)**: `INSUFFICIENT_DATA` — No authoritative conclusions permitted; metrics marked provisional.
- **Tier 1 (N = 3 to 4 matches)**: `EXPLORATORY` — Early indications only; high variance warnings.
- **Tier 2 (N = 5 to 9 matches)**: `LOW_CONFIDENCE` — Preliminary trends detectable.
- **Tier 3 (N = 10 to 19 matches)**: `MODERATE_CONFIDENCE` — Stable individual and partnership patterns.
- **Tier 4 (N >= 20 matches)**: `HIGH_CONFIDENCE` — Statistically robust sample for strategic coaching changes.

Every recommendation must separate:
1. **Empirical Observations** (what the match numbers show)
2. **Correlations** (relationships observed between builds/tactics and outcomes)
3. **Domain Heuristics** (established Pro Clubs mechanics and playstyle synergies)
4. **Actionable Recommendations** (concrete steps the club or individual should take)

### 4. Safety Invariants & Posture
- All dispatch and autonomous mutation flags default to fail-closed:
  - `FC27_PROVIDER_ENABLED=false` (unless test fixtures or approved providers are active)
  - `FC27_LIVE_INGESTION_ENABLED=false`
  - `FC27_RECOMMENDATIONS_ENABLED=false by default`
  - `DISPATCH_ENABLED=false`
  - `DISPATCH_WORKER_ENABLED=false`
  - `FC27_PROVIDER_DRY_RUN=true`
- All security postures established in Phase 1 (least-privilege database roles, HMAC/WebAuthn human approval requirements, immutability triggers) remain inviolable.

---

## 5. Provider Research Provenance & Licensing Gate

### 5.1 Evaluated Community Research Source
- **Source Repository**: `https://github.com/Interactive-63/eafc-pro-clubs-api-research`
- **Inspected Commit SHA**: `4cb629268b4f21a1d09de3f9df7012ae0b28b0a8` (Date: 2026-10-18T22:21:30Z)
- **License Status**: `license: null` (No license file committed; GitHub metadata explicitly indicates no open-source license granted).
- **Legal & Compliance Finding**: Under applicable copyright law, an unlicensed repository reserves all rights to its author. Therefore:
  1. No code, text, raw datasets, or schemas may be copied or distributed directly.
  2. The platform uses clean-room independent implementation of internal data contracts, schemas, and algorithms based on independent analysis.
  3. Community findings are treated strictly as unverified observations and hypotheses, never as authoritative or stable contracts.
  4. The platform makes no claims that community-researched endpoints are official EA APIs.

---

## 6. Provider Access Safety Assessment

### 6.1 Provider Characteristics
- **Expected Hostnames**: `proclubs.ea.com` (Assets: `content.easports.com`, `media.contentapi.ea.com`)
- **HTTP Methods**: `GET` only
- **Authentication**: Unofficial endpoints; live access requires session extraction or internal headers. No official public developer portal or API keys exist.
- **Platform Scope**: `common-gen5` (crossplay current generation)
- **Rate and Concurrency Limits**: Strict default concurrency of 1, max retries 2 with exponential backoff on transient errors (408, 429, 500, 502, 503, 504), zero retries on terminal errors (400, 401, 403, 404).

### 6.2 Access Classification
- **Access Readiness**: `RESEARCH_ONLY`
- **Live Access Status**: Strictly BLOCKED. Live network calls fail closed with `FC27LiveAccessBlockedError` unless explicitly authorized in a subsequent phase.

---

## 7. East Coasters Identity Contract

### 7.1 Provisional Identity Rule
- Club ID `330323` and Club Name `East Coasters` are treated as **`PROVISIONAL`** until verified via an authorized, reproducible live capture.
- A matching ID in synthetic fixtures transitions state to **`SYNTHETIC_ONLY`**, never **`VERIFIED`**.
- Platform (`common-gen5`) is mandatory for identity scoping.
- Conflicting club evidence (e.g. name or platform discrepancy) transitions to **`CONFLICTED`**.
- Deletion or deactivation transitions to **`RETIRED`**.
- State transitions are fully reversible upon new evidence.

---

## 8. Analytics Compatibility Matrix

| Domain Field | Provider Field | Availability | Transform | Units | Valid Range | Missing Data Policy | Confidence Impact | Fixture Coverage | Validation Status |
|---|---|---|---|---|---|---|---|---|---|
| `player_id` | `player_id` | DIRECT | identity | string | `^[A-Za-z0-9._:-]{1,64}$` | Reject performance | Fatal if missing | Synthetic, Research | PROVISIONAL |
| `position` | `position` | DIRECT | uppercase_match | categorical | `VALID_FC27_POSITIONS` | Reject performance | Fatal if missing | Synthetic, Research | PROVISIONAL |
| `role` | `role` | DIRECT | string_match | categorical | `VALID_FC27_ROLES` | Position default fallback | Moderate reduction | Synthetic | PROVISIONAL |
| `minutes_played` | `minutes_played` | DIRECT | int_cast | minutes | `0..130` | Default to 90 | Low reduction | Synthetic | PROVISIONAL |
| `result` | `result` | DIRECT | uppercase_match | categorical | `WIN`, `LOSS`, `DRAW` | Derive from score | None | Synthetic, Research | PROVISIONAL |
| `formation` | `formation` | DIRECT | string_match | categorical | `VALID_FC27_FORMATIONS` | Reject match | Fatal if missing | Synthetic | PROVISIONAL |
| `lineup` | `lineup` | DERIVED | extract active IDs | list of IDs | 1 to 11 players | Extract from performances | None | Synthetic | PROVISIONAL |
| `goals` | `goals` | DIRECT | int_cast | count | `>= 0` | Default to 0 | None | Synthetic, Research | PROVISIONAL |
| `assists` | `assists` | DIRECT | int_cast | count | `>= 0` | Default to 0 | None | Synthetic, Research | PROVISIONAL |
| `passing_metrics` | `passes_completed/att` | DIRECT | ratio_percentage | percentage | `0.0..100.0` | Default 0 if 0 attempts | None | Synthetic, Research | PROVISIONAL |
| `defensive_metrics` | `tackles_won/att` | DIRECT | ratio_percentage | percentage | `0.0..100.0` | Default 0 if 0 attempts | None | Synthetic, Research | PROVISIONAL |
| `possession_turnover`| `event_105-110` | DERIVED | sum third events | count | `>= 0` | Omit turnover analysis | Moderate reduction | Synthetic, Research | PROVISIONAL |
| `goalkeeper_saves` | `event_267` | PROXY | sum save events | count | `>= 0` | Omit GK save analysis | High reduction | Research | PROVISIONAL |
| `match_rating` | `match_rating` | DIRECT | float_cast | score | `1.0..10.0` | Reject performance | Fatal if missing | Synthetic, Research | PROVISIONAL |
| `build_reference` | N/A | UNAVAILABLE | registry lookup | string | registered build ID | Unobserved build | Critical for builds | Club registry | PROVISIONAL |
| `opponent_club_name` | `opponent_club_name` | DIRECT | string_match | string | 1 to 128 chars | Default "Unknown" | Low reduction | Synthetic, Research | PROVISIONAL |
| `match_timestamp` | `match_timestamp` | DIRECT | iso8601_parse | ISO timestamp | Valid UTC ISO | Fallback to capture ts | None | Synthetic, Research | PROVISIONAL |
| `game_version` | `game_version` | DIRECT | string_match | categorical | `FC27` | Fail-closed reject | Fatal if mismatch | Synthetic, Research | PROVISIONAL |
| `data_quality_status`| `data_quality_status`| DIRECT | string_match | categorical | `VERIFIED`, `PROVISIONAL` | Default PROVISIONAL | Adjusts confidence | Synthetic | PROVISIONAL |

---

## 9. Security and Privacy Threat Assessment

1. **SSRF Prevention & Host Whitelisting**: Transport layer enforces strict hostname allowlisting (`proclubs.ea.com`) and HTTPS schemes. All redirects to untrusted destinations are rejected.
2. **Credential & Cookie Redaction**: Outgoing and incoming header logs automatically sanitize sensitive fields (`Authorization`, `Cookie`, `Set-Cookie`, `X-Origin-Session`, tokens).
3. **Payload Sanitization & Size Ceilings**: Responses exceeding 5 MB are rejected immediately with `FC27PayloadTooLargeError` to prevent denial-of-service.
4. **Idempotent Ingestion & Quarantine**: Match payloads are hashed via deterministic SHA-256. Replaying identical matches causes zero duplicate side effects. Conflicting payloads for the same match ID are quarantined.
5. **Segregation of Synthetic & Observed Records**: Synthetic test records are explicitly tagged (`source_classification: SYNTHETIC`) and blocked from silent merging into observed records.
6. **No Secret Storage**: No credentials, session cookies, or user tokens are stored in source control or fixtures.

---

## 10. Bounded Capture Harness & Mock Validation Specification

### 10.1 Access Status and Operational Posture
- `ACCESS_STATUS`: `UNAPPROVED`
- `PROVIDER_CONTRACT_STATUS`: `PROVISIONAL`
- `LIVE_EXECUTION_STATUS`: `BLOCKED`

Live access remains strictly blocked at code and configuration levels (`FC27LiveAccessBlockedError`). The capture harness does NOT provide EA provider credentials or session access; it provides immutable, in-memory operator scoping and budget controls.

### 10.2 Bounded Capability and One-Use Budgeting
- **Exact One-Request Scope**: Authorization (`FC27CaptureAuthorization`) is bound strictly to `operation=club_identity_lookup`, `hostname=proclubs.ea.com`, `method=GET`, `path=/api/fc/clubs/info`, `club_id=330323`, and `platform=common-gen5`.
- **Zero Retries & Redirects**: `maximum_attempts=1`, `maximum_redirects=0`. All transient (408, 429, 5xx) and terminal errors consume the attempt without retry. Redirects are rejected immediately.
- **Immediate Consumption**: The attempt is consumed via `FC27CaptureBudget.consume()` immediately prior to transport execution. Re-use of an authorization or budget fails closed with `FC27BudgetExhaustedError`.

### 10.3 Sanitization and Privacy Boundaries
- **Allowlist Retention**: Only non-personal identity fields (`provider`, `game_version`, `platform`, `club_id`, `club_name`, `region`, `captured_at`, `payload_sha256`, `source_classification`, `verification_state`, `verification_confidence`, `contract_version`, `schema_version`) are retained.
- **Sensitive Keyword Detection**: Deep recursive inspection scans all dictionary keys and list paths for terms including `email`, `password`, `token`, `cookie`, `session`, `account`, `persona`, `gamer`, `player`, `roster`, and `device`.
- **Raw-Response Prohibition**: Raw response bytes are permanently discarded after hashing and sanitization. No raw payloads or sensitive field values are written to disk, logged, or retained.
- **Incident Handling**: Detection of unresolved sensitive field paths sets `safe_to_retain=False` and blocks club identity verification.

### 10.4 Identity Verification and Human Approval Rules
- **Provisional by Default**: Mock data, synthetic fixtures, and unapproved captures remain `PROVISIONAL` or `SYNTHETIC_ONLY`.
- **Human Approval Requirement**: Transition to `VERIFIED` strictly requires all matching identity criteria, zero sensitive data findings, valid envelope, `source_classification=AUTHORIZED_CAPTURE`, and explicit `human_verification_approved=True`.
- **Discrepancy Conflict**: Mismatches in `club_id`, `club_name`, or `platform` transition identity to `CONFLICTED`.

### 10.5 Boundaries and Exclusions
- Match-history, member, roster, and player statistics capture remain strictly out of scope.
- Browser-session extraction, cookie scraping, account impersonation, and automated login remain prohibited.
- General live ingestion, dispatch, dispatch workers, and recommendations remain disabled.

---

## 11. Provider Access Decision

### 11.1 Reviewed Sources and Authority Hierarchy
1. **Official EA User Agreement (ea.com/legal/user-agreement)**: Evaluated Section 6 ("Rules of Conduct"), which explicitly prohibits using automated tools, bots, or scrapers to access EA services, as well as reverse engineering or extracting data without express written permission from EA.
2. **Official EA Sports FC Web Properties & Portals**: Confirmed EA does not provide an open or self-service public developer API for Pro Clubs. The restricted "FC Community API" is limited to pre-approved commercial partners (e.g. FUTBIN, FUT.GG) for Ultimate Team, with applications closed.
3. **Public HTTP Behavior**: The endpoint `https://proclubs.ea.com/api/fc/clubs/info` serves internal web companion widgets. While observed unauthenticated in web browser sessions, it lacks third-party developer licensing or service-level agreements.
4. **Community Research**: Unofficial community wrappers (e.g. `fc-clubs-api`, `proclubs-sdk`) confirm observed endpoint mechanics but do not establish legal or contractual authorization.

### 11.2 Access Classification and Findings
- **Official API Availability**: NO official public Pro Clubs API exists.
- **Authentication Method**: NO official API-key, OAuth2, or service-account authentication flow is provided by EA.
- **Automation Permission**: NO official programmatic automation permission exists; automated access without written consent breaches the EA User Agreement.
- **Rate Limits & Retention Policies**: Undocumented and unmanaged by EA for third parties.
- **Privacy Impact**: Direct responses can expose persistent gamer tags, account IDs, and persona IDs.
- **Provider Access Classification**: `RESEARCH_ONLY`.
- **Operational Safety Posture**:
  - `ACCESS_STATUS`: `UNAPPROVED`
  - `PROVIDER_CONTRACT_STATUS`: `PROVISIONAL`
  - `LIVE_EXECUTION_STATUS`: `BLOCKED`
  - `CLUB_ID_VERIFICATION_STATUS`: `PROVISIONAL`

### 11.3 Integration Strategy Decision Matrix

| Strategy | Legal / Terms Risk | Security Risk | Privacy Risk | Stability | Automation | Completeness | Implementation Effort | Maintenance Effort | Usability | Recommendation |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Strategy A: Official API** | LOW | LOW | LOW | HIGH | HIGH | HIGH | LOW | LOW | HIGH | Not Available (No public API) |
| **Strategy B: Undocumented Public Endpoint** | HIGH | MEDIUM | HIGH | LOW | HIGH | MEDIUM | MEDIUM | HIGH | HIGH | `DO_NOT_IMPLEMENT` |
| **Strategy C: User-Supplied Export** | LOW | LOW | LOW | MEDIUM | MEDIUM | HIGH | LOW | LOW | HIGH | **PRIMARY_PATH** |
| **Strategy D: Club-Managed Manual Entry** | LOW | LOW | LOW | HIGH | LOW | MEDIUM | LOW | LOW | MEDIUM | **FALLBACK_PATH** |

### 11.4 Selected Paths and Live Transport Decision
- **Primary Integration Path**: `USER_SUPPLIED_EXPORT`. Enables East Coasters administrators to lawfully export and import their own club data with explicit consent and without automated scraping.
- **Fallback Integration Path**: `MANUAL_ENTRY`. Enables direct administrative entry of verified club attributes and match outcomes.
- **Live Transport Decision**: `DO_NOT_IMPLEMENT`.
- **Live Transport Design Status**: `DESIGN_ONLY`, `NOT_IMPLEMENTED`, `NOT_AUTHORIZED_FOR_EXECUTION`. A live concrete HTTP transport will not be implemented unless EA publishes an official, supported third-party API.

### 11.5 User-Supplied Export Contract Specification
The user-supplied intake path operates strictly on sanitized, consented observations.

#### Required Fields
- `provider`: Exact literal `"EA_FC_PRO_CLUBS"`
- `game_version`: Exact literal `"FC27"`
- `platform`: Target platform (e.g. `"common-gen5"`)
- `club_id`: Expected club identifier (e.g. `"330323"`)
- `club_name`: Expected club name (e.g. `"East Coasters"`)
- `captured_at`: ISO 8601 UTC timestamp of export creation
- `source_classification`: `"USER_SUPPLIED_EXPORT"`
- `supplied_by_role`: `"club_administrator"` or `"player"`
- `consent_attestation`: Explicit boolean attesting user ownership and consent
- `payload_sha256`: SHA-256 digest of original export content
- `contract_version`: Schema contract version (`"1.0.0"`)
- `schema_version`: Identity schema version (`"1.0"`)

#### Prohibited Fields
The following fields are strictly rejected:
- Authentication headers, bearer tokens, API keys
- Session cookies, session IDs, tracking tokens
- Nucleus IDs, account IDs, persona IDs
- Personal email addresses, IP addresses, device identifiers
- Raw roster arrays and unconsented member telemetry

#### Validation Invariants
1. Extra or unmapped fields are quarantined or rejected fail-closed.
2. User-supplied exports remain `PROVISIONAL` and cannot automatically transition identity to `VERIFIED`.
3. Discrepancies in `club_id`, `club_name`, or `platform` transition identity to `CONFLICTED`.
4. Duplicate imports are strictly idempotent via `payload_sha256`.
5. Raw upload files are not retained permanently in storage.

---

## 12. User-Supplied Export Architecture & Operations

### 12.1 Rationale for User-Supplied Export
Due to the absence of a public, authenticated third-party developer API for EA SPORTS FC 27 Pro Clubs and the strict legal and terms-of-service risks associated with automated web scraping or undocumented endpoint reverse-engineering, `USER_SUPPLIED_EXPORT` is the authoritative primary integration path for the East Coasters.

This architecture empowers club managers to collect and supply club match summaries and lineups offline, maintaining complete data custody and operational control without credentials, session hijacking, or browser scraping.

### 12.2 Versioned Contracts
- **Contract Version**: `1.0.0`
- **Schema Version**: `1.0.0`
- **Target Platform**: `common-gen5`
- **Game Version**: `FC27`
- **Expected Club ID**: `330323`
- **Expected Club Name**: `East Coasters`

### 12.3 Supplier Attestation and Team Consent
Every import requires cryptographic metadata and attestation:
1. **Supplier Role**: Must be one of `CLUB_MANAGER`, `CLUB_ADMIN`, or `DATA_STEWARD`.
   - Requires authority over at least `CLUB_IDENTITY` and `MATCH_RESULTS`.
   - Optional scopes: `LINEUPS`, `PLAYER_PERFORMANCE`, `BUILD_REFERENCES`.
   - Prohibits personal email addresses or authentication tokens in `supplier_reference`.
2. **Team Consent**: Attested via `FC27ConsentAttestation`:
   - Mandatory categories: `CLUB_IDENTITY` and `MATCH_RESULTS`.
   - Optional categories: `LINEUPS`, `PLAYER_PERFORMANCE`, `BUILD_REFERENCES`.
   - Requires explicit acknowledgments for retention, analytics usage, and team review.
   - Any revoked consent (`revoked_at` is set) fails closed immediately.
   - Consent authorizes only offline analytics; it does NOT grant EA provider authorization or live verified status.

### 12.4 Sensitive-Field Scanner & Incident Response
The intake system incorporates a recursive, case-insensitive, token-based sensitive field scanner:
- Scans JSON keys, CSV headers, nested objects, and list elements across camelCase, snake_case, kebab-case, and dotted notation.
- Prohibited patterns include: `authorization`, `auth`, `bearer`, `cookie`, `session`, `token`, `password`, `secret`, `api_key`, `client_secret`, `account_id`, `persona_id`, `nucleus_id`, `user_id`, `email`, `ip_address`, `device_id`, `gamer_tag`, and raw unconsented roster telemetry.
- **Fail-Closed Behavior**: When a sensitive field is detected, the import is rejected immediately with exit code 1.
- **Zero Value Leakage**: Diagnostics and logs record only the path of the prohibited field (e.g. `matches[0].player_performances[0].email`), never the sensitive value.
- **Non-Retention**: Detected payloads are marked `SAFE_TO_RETAIN_RAW_UPLOAD: NO` and are discarded from memory immediately without being written to disk.

### 12.5 File Formats
1. **JSON (`parse_fc27_user_export_json`)**:
   - Max file size: 5 MiB (`5,242,880` bytes).
   - Strict UTF-8; top-level arrays and duplicate keys rejected.
   - Requires `payload_sha256` matching canonical digest.
2. **CSV (`parse_fc27_user_export_csv`)**:
   - Flat single-file match summary with exact approved columns: `club_id, club_name, platform, game_version, external_match_id, played_at, result, goals_for, goals_against, opponent_name, formation`.
   - Formula-injection defense: Cells beginning with `=`, `+`, `-`, or `@` are rejected fail-closed.
   - Strict cell length (max 256 chars), row bounds (max 100 matches), and strict numeric/timestamp parsing.

### 12.6 Canonical Hashing & Idempotency
- **Payload Integrity**: `compute_payload_sha256()` uses deterministic JSON serialization (sorted keys, compact separators, ISO-8601 UTC timestamps).
- **Match Record Hashing**: `compute_match_hash()` computes SHA-256 over canonical match attributes.
- **Idempotent Planning**: Re-importing an identical payload results in `DUPLICATE_IGNORE` across all records. If a previously imported match ID appears with different data without explicit versioning, it is quarantined as `QUARANTINE_CONFLICT` and blocks application.

### 12.7 Club Identity Conflict Handling
Supplied club identity is evaluated strictly against the East Coasters target:
- Exact match (`330323`, `East Coasters`, `common-gen5`, `FC27`) -> `PROVISIONAL`.
- Mismatched club ID, name, or platform -> `CONFLICTED` (blocks import).
- No import path automatically returns `VERIFIED`.

### 12.8 Baseline Preview & Sample-Size Confidence Tiers
To prevent premature or misleading coaching conclusions, imported data is mapped to confidence tiers based on analytics-eligible completed matches:
- **0 to 2 Matches**: `INSUFFICIENT_DATA`
- **3 to 4 Matches**: `EXPLORATORY`
- **5 to 9 Matches**: `LOW_CONFIDENCE`
- **10 to 19 Matches**: `MODERATE_CONFIDENCE`
- **20+ Matches**: `HIGH_CONFIDENCE`
- **Coaching Recommendations**: Remain strictly disabled (`COACHING_READINESS: NO`).

### 12.9 Operator Runbook & File Custody
- **Tool**: `scripts/fc27_user_export_import.py`
  - Validation: `python scripts/fc27_user_export_import.py --input <path> --format <json|csv> --validate`
  - Dry-Run: `python scripts/fc27_user_export_import.py --input <path> --format <json|csv> --dry-run`
  - Apply: `python scripts/fc27_user_export_import.py --input <path> --format <json|csv> --apply --confirm-apply`
- **Storage Boundary**: Persistence is reported as `DEFERRED` pending dedicated relational migrations. Raw uploads are never stored.
- **File Deletion**: Operators are instructed to delete or archive local export source files once validated.

### 12.10 Current Platform Status (Maintained)
- `ACCESS_STATUS`: `UNAPPROVED`
- `PROVIDER_CONTRACT_STATUS`: `PROVISIONAL`
- `LIVE_EXECUTION_STATUS`: `BLOCKED`
- `CLUB_ID_VERIFICATION_STATUS`: `PROVISIONAL`

---

## 13. Audited `fc27-clubs-api` Multi-Club Compatibility Layer

### 13.1 Upstream Audit Provenance & Licensing
- **Upstream Repository**: `https://github.com/1erkandogan/fc27-clubs-api`
- **Reviewed Commit SHA**: `26a5ff12ea793042bd776a858c74f3f44dd427f5` (2026-10-03T11:05:32Z)
- **Reviewed Version**: `0.3.1`
- **Upstream License**: `MIT` (`LICENSE.md` SHA-256: `2b5e42f174b039db7454e7d13d61c39d5e05a1e283121d66768e0ac6398455b5`)
- **Audit Reference & Date**: `AUDIT-FC27-CLUBS-API-2026-10-27` (`2026-10-27`)
- **License Compatibility**: `YES`
- **Security & Dependency Review**: `PASS` (Zero mandatory third-party network dependencies; `pandas` optional extra rejected from runtime requirements).
- **Transport Review**: `REJECTED` (`TRANSPORT_REVIEW_PASS: NO`, `BROWSER_IMPERSONATION_REQUIRED: YES`). Upstream `src/fc_clubs_api/_http.py` injects browser-profile emulation headers (`User-Agent: Mozilla/5.0 ... Chrome/141.0.0.0`, `sec-ch-ua`, and `sec-fetch-site: same-origin`) specifically to bypass Akamai 403 blocks on `proclubs.ea.com`.
- **Adaptation Decision**: `APPROVED_SUBSET`
- **Adoption Strategy**: `INTERNAL_REIMPLEMENTATION` in `src/fc27_clubs.py` (no runtime dependency on `fc-clubs-api` or `pandas`).

### 13.2 Unofficial Provider Classification & No EA Endorsement
- `PROVIDER_NAME`: `EA_FC_CLUBS_WEB`
- `PROVIDER_STATUS`: `UNOFFICIAL_PUBLIC_WEB_INTERFACE`
- `OFFICIAL_EA_DEVELOPER_API`: `NO`
- `SUPPORT_STATUS`: `UNSUPPORTED`
- `CONTRACT_STABILITY`: `UNCONFIRMED`
- `PROVIDER_AUTHENTICATION_METHOD`: `NONE_DOCUMENTED`
- `TERMS_REVIEW_REQUIRED`: `YES`
- **No EA Approval or Endorsement**: The official EA Community API program does not authorize this integration. `iwa.dev.fcbuild` is not an approved EA client, does not display any EA badge, does not claim EA OAuth support, and never collects EA credentials or browser sessions.

### 13.3 Component Adoption & Rejection Matrix

| Upstream Component | Classification | Disposition & Modification Summary |
|---|---|---|
| `src/fc_clubs_api/enums.py` | `CLEAN_ROOM_REIMPLEMENTED` | Reimplemented platform (`common-gen5`, `nx`) and operation enums in `src/fc27_clubs.py`. |
| `src/fc_clubs_api/exceptions.py` | `CLEAN_ROOM_REIMPLEMENTED` | Mapped error semantics into `FC27ClubsCompatibilityAdapter.translate_error()`. |
| `src/fc_clubs_api/_columns.py` | `ADAPTED_WITH_LICENSE` | Adapted field mappings into strongly typed Pydantic internal records (`FC27ClubIdentityMappedRecord`, `FC27ClubOverviewRecord`, `FC27ClubMatchSummaryRecord`, `FC27ClubMemberSummaryRecord`, `FC27MemberCareerStatsRecord`, `FC27PlayerMatchStatsRecord`). |
| `src/fc_clubs_api/_normalize.py` | `CLEAN_ROOM_REIMPLEMENTED` | Reimplemented strict numeric string coercion (`_coerce_int`, `_coerce_float`) and UTC ISO-8601 timestamp normalization (`_normalize_timestamp`). |
| `src/fc_clubs_api/events.py` | `ADAPTED_WITH_LICENSE` | Reconciled 74 single-event definitions with `FC27_EVENT_REGISTRY` via `reconcile_event_registry()`, keeping all community-derived mappings strictly `PROVISIONAL`. |
| `src/fc_clubs_api/assets.py` | `CONCEPT_ONLY` | Retained non-personal crest/kit identifier extraction (`team_id`, `crest_asset_id`, `selected_kit_type`) only. |
| `src/fc_clubs_api/_http.py` | `REJECTED` | Rejected due to Chrome 141 User-Agent, `sec-ch-ua`, and `sec-fetch-site: same-origin` Akamai evasion headers. |
| `src/fc_clubs_api/client.py:get_json` | `REJECTED` | Rejected unguarded arbitrary endpoint caller. |
| `src/fc_clubs_api/client.py:find_club_id` | `REJECTED` | Rejected automatic single-result selection on club search; explicit human club ID confirmation is mandatory. |
| `src/fc_clubs_api/_output.py` | `REJECTED` | Rejected `pandas` DataFrame output layer and raw JSON pass-through. |
| `src/fc_clubs_api/__main__.py` | `REJECTED` | Replaced with dry-run-only `scripts/fc27_club_lookup.py`. |
| `Interactive-63/eafc-pro-clubs-api-research` | `CONCEPT_ONLY` | Unlicensed upstream research repository cited as conceptual provenance only; zero code copied. |

### 13.4 Separation of Concerns: Authentication, Authorization, Membership, and Manager Authority
The compatibility layer enforces a strict 11-way architectural separation:
1. **Application Authentication (`FC27ApplicationClubAccess`)**: Reuses `iwa.dev.fcbuild` actor identity. Authentication alone grants zero club permissions without an assigned `FC27AccessRole` (`VIEWER`, `TEAM_MEMBER`, `DATA_STEWARD`, `CLUB_MANAGER`, `CLUB_ADMIN`) or explicit action grant.
2. **Public Club Identity Lookup (`VIEW_PUBLIC_CLUB_IDENTITY`)**: Permitees may inspect metadata-only public club identity, but a successful lookup **never** grants team membership, manager authority, persistence, recommendations, or dispatch.
3. **Team Membership & Manager Authority**: Initial East Coasters state maintains `USER_MEMBERSHIP_STATUS=USER_ASSERTED` and `MANAGER_AUTHORITY_STATUS=UNVERIFIED`. Manager authority cannot be inferred from a successful lookup, roster presence, matching alias, possession of a club ID, public page access, login, or self-assertion (`"I think I am a manager"`). It requires separate reviewed authority evidence (`reviewed_authority_evidence_id`), and fails closed upon expiration or revocation.

### 13.5 Multi-Club Query Model, Request Builder, and Provider Policy
- **Capability Truthfulness (`FC27ProviderCapabilities`)**: Explicitly distinguishes between `declared_operations` (all 10 operations), `mock_enabled_operations` (all 10 operations), `design_eligible_operations` (`("CLUB_IDENTITY",)`), and `live_executable_operations` (`()` — empty because no approved transport exists).
- **Multi-Club Design**: `FC27ClubQuery` and `lookup_fc27_club()` support any validated numeric `club_id` and supported platform (`common-gen5`, `nx`). East Coasters (`330323`, `East Coasters`, alias `EastCoasters FC`, abbreviation `ECF`, region claim `East Coast US 1`) is represented purely as a seeded configuration profile (`EAST_COASTERS_SEED_PROFILE`).
- **Pure Request Builder (`build_fc27_club_request`)**: Constructs `GET https://proclubs.ea.com/api/fc/clubs/info?platform=<platform>&clubIds=<club_id>` while rejecting CRLF injection, query injection, multiple-club injection, negative/nonnumeric IDs, embedded credentials, HTTP, alternate hosts/ports, fragments, Unicode lookalikes, and custom/cookie/auth headers.
- **Public Provider Policy (`FC27PublicProviderPolicy`)**: Enforces HTTPS only, exact host/path allowlists, GET only, zero retries, `maximum_requests=1`, connect timeout <= 5s, total timeout <= 10s, response size <= 1 MiB (`1,048,576` bytes), `application/json`, no redirects, no raw-body logging or retention, and prohibition of scheduler, dispatch, or worker execution contexts.

### 13.6 Response Sanitizer, Identity Matching, Cache, and Circuit Breaker
- **Allowlist Sanitizer (`sanitize_identity_payload`)**: Retains only `club_id`, `club_name`, `platform`, `provider_region_value`, non-personal `crest_kit_identifiers`, `provider`, `game_version`, `capture_timestamp`, `payload_hash`, and `provenance`. Drops all member lists, gamer tags, persona IDs, account IDs, user IDs, cookies, tokens, and sessions, recording only dropped/sensitive field paths.
- **Identity Matching (`compare_club_identity`)**: Uses Unicode `NFKC` + casefolding while preserving the exact provider-returned name. Exact match against canonical name or known alias returns `EXACT_MATCH`. Abbreviation (`ECF`) alone returns `REVIEW_REQUIRED`. Provider region IDs are never automatically mapped to `East Coast US 1`.
- **Sanitized Cache (`FC27Cache`)**: Bounded in-memory LRU cache (`TTL <= 600s`), keyed by `(provider, operation, game_version, platform, club_id)`. Rejects raw bodies and unsanitized payloads.
- **Circuit Breaker (`FC27CircuitBreaker`)**: Opens immediately on HTTP 401, 403, redirects, CAPTCHAs, HTML/Akamai challenges, session requirements, or repeated malformed responses. When open, blocks calls, requires manual review reset (`reset(manual=True)`), and returns the `USER_SUPPLIED_EXPORT` fallback.

### 13.7 Event & Formula Reconciliation and Controlled Probe Gate
- **Event & Formula Reconciliation (`reconcile_event_registry`)**: Maps upstream event definitions as `PROVISIONAL` (`source_commit=26a5ff12ea793042bd776a858c74f3f44dd427f5`), flags semantic mismatches as `CONFLICTED`, rejects non-FC27 (e.g., FC 26) promotions, preserves unknown event IDs with raw counts, and degrades analysis confidence when required events are unknown.
- **Controlled Probe Gate & Offline Fallback**: Because the upstream transport requires browser-header emulation to avoid Akamai 403 responses, no live HTTP transport is enabled (`READY_FOR_ONE_PUBLIC_IDENTITY_PROBE=NO`, `FC27_PUBLIC_IDENTITY_LOOKUP_ENABLED=false`, `FC27_UPSTREAM_COMPATIBILITY_ENABLED=false`). `USER_SUPPLIED_EXPORT` remains the active primary integration path.

### 13.8 Upstream MIT License Notice
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

---

## 14. Offline Club Evidence Workflow & Attestation Templates

### 14.1 Offline Public-Source Observation Model
To corroborate club identity claims without executing automated requests against EA services or scraping public websites, `src/fc27_clubs.py` implements an offline, metadata-only evidence evaluation workflow (`FC27OfflinePublicObservation`, `evaluate_offline_club_evidence`, `FC27OfflineEvidenceManifest`):
- **No Network or Scraping**: Zero page fetching, zero HTML scraping, zero browser-call replay, zero raw HTML storage, and zero player-linked data retention.
- **Explicit Source Independence Groups (`FC27SourceIndependenceGroup`)**:
  1. `PROCLUBS_TRACKER`: `https://proclubstracker.com/club/330323?platform=common-gen5` (`proclubstracker.com`, `www.proclubstracker.com`)
  2. `MYPROCLUBS`: `https://myproclubs.com/East%20Coasters/330323-5` (`myproclubs.com`, `www.myproclubs.com`)
  3. `PROCLUBS_ANALYTICS`: `https://www.proclubsanalytics.com/club/330323?edition=fc27&platform=common-gen5&clubName=East+Coasters&lang=en` (`proclubsanalytics.com`, `www.proclubsanalytics.com`)
- **Strict URL Policy (`canonicalize_secondary_url`)**: Enforces `https` only, ASCII host only (no Unicode lookalikes or punycode), no credentials, no localhost or private IP literals, no explicit ports, and an explicit domain allowlist. `compute_url_sha256` strips `#fragment` while preserving query parameters.
- **Provenance & Deduplication**: Each observation records `url_sha256`, `evidence_sha256` (deterministic UTF-8 JSON SHA-256 of normalized fields), `independence_group`, `source_operator`, `observed_at`, `page_title`, `normalized_fields`, `reviewer_reference`, and `review_status`. Duplicate URL hashes, identical `evidence_sha256` digests, or repeated observations from the same `independence_group` are deduplicated so a single site cannot self-corroborate. If the same `observation_id` appears with a different `evidence_sha256`, the evaluation flags `EVIDENCE_ID_CONFLICT`.

### 14.2 East Coasters Evidence Outcome, Status Hierarchy, & Authority Separation
- **Evidence Status Hierarchy (`FC27EvidenceStatusHierarchy`)**: `UNSET -> USER_ASSERTED -> PROVISIONAL -> CORROBORATED -> CONFLICTED -> VERIFIED`.
- **Corroboration & Conflict Rules**: A claim (`club_name`, `club_id`, `platform`, `game_version`) becomes `CORROBORATED` only when `>= 2` distinct `FC27SourceIndependenceGroup` observations agree and zero observations conflict. Any disagreement across `club_name`, `club_id`, `platform`, `game_version`, `club_abbreviation`, or `matchmaking_region` sets that field's status to `CONFLICTED` (majority vote never clears a conflict).
- When the three independent East Coasters secondary observations are evaluated:
  - `CLUB_NAME_STATUS`: `CORROBORATED`
  - `CLUB_ID_STATUS`: `CORROBORATED`
  - `PLATFORM_STATUS`: `CORROBORATED`
  - `GAME_VERSION_STATUS`: `CORROBORATED`
  - `REGION_STATUS`: `USER_ASSERTED` (`East Coast US 1`)
  - `MEMBERSHIP_STATUS`: `USER_ASSERTED`
  - `MANAGER_AUTHORITY_STATUS`: `UNVERIFIED`
- **Corroborated Does Not Mean Verified**: `corroborated_equals_verified` is strictly `False`. Corroboration across secondary public observations never grants `VERIFIED` status, team membership, or manager authority.

### 14.3 Non-Personal Role Attestation Templates
`get_attestation_templates()` provides structured, non-personal attestation templates (`FC27RoleAttestationTemplate`) for:
- `CLUB_MANAGER`: `supplier_reference="ecf-role-ref-club-manager-01"`
- `CLUB_ADMIN`: `supplier_reference="ecf-role-ref-club-admin-01"`
- `DATA_STEWARD`: `supplier_reference="ecf-role-ref-data-steward-01"`

Each template includes `attestation_timestamp`, `expires_at`, `evidence_references`, `reviewer_reference`, `review_decision`, `review_reason`, and `manager_authority_status="UNVERIFIED"`. Personal email addresses (`@`), tokens, passwords, and session identifiers are rejected fail-closed.

### 14.4 Consented User-Export Pilot Sequence
The later user-export pilot follows an explicit 8-step sequence (`USER_EXPORT_PILOT_SEQUENCE`), documented in full in `docs/FC27_EAST_COASTERS_EXPORT_PILOT_PACKAGE.md`:
1. Authorized supplier provides local export outside repository
2. Consent scopes reviewed
3. `--validate`
4. Human metadata review
5. `--dry-run`
6. Readiness preview (`0-2`: `INSUFFICIENT_DATA`, `3-9`: `EXPLORATORY`, `10-19`: `PROVISIONAL_TRENDS`, `20+`: `HIGH_SAMPLE_READINESS`)
7. No `--apply`
8. No recommendations

---

## 15. PostgreSQL Performance Storage & Quarantine Foundation (Migration 0012)

### 15.1 Authoritative Database Engine & Rejection of SQLite Proposal
- **Authoritative Store**: PostgreSQL 16 (`psycopg` v3) is the sole relational store for FC 27 performance and quarantine persistence.
- **SQLite Rejection**: External proposals to introduce SQLite containers, `sqlite3` drivers, `PRAGMA journal_mode=WAL`, or `INSERT OR IGNORE` semantics were formally **rejected** to preserve a single transactional persistence architecture with LangGraph `PostgresSaver`, `public.domain_events`, and `public.outbox`.

### 15.2 Migration `0012` Tables (`migrations/0012_fc27_performance_and_quarantine.sql`)
1. `public.fc27_clubs`: Composite primary key `(club_id, platform, game_version)`. Enforces `manager_authority_status = 'UNVERIFIED'` so club identity records can never self-grant manager authority.
2. `public.fc27_export_batches`: Tracks applied export batch provenance (`export_id`, `contract_version`, `schema_version`, `supplier_role`, non-personal `supplier_reference`, `consent_version`, `consented_categories`, `payload_sha256`, and non-negative match counts where `accepted_matches + quarantined_matches <= total_matches`).
3. `public.fc27_matches`: Stores normalized match outcomes with `UNIQUE (club_id, platform, game_version, external_match_id)`, score-result consistency checks, and 64-character lowercase `match_hash`.
4. `public.fc27_player_match_stats`: Stores normalized per-player match metrics with `UNIQUE (match_id, local_player_reference)`, non-negative counts, `passes_completed <= passes_attempted`, `tackles_won <= tackles_attempted`, `shots_on_target <= shots`, `provider_rating` in `0.0..10.0`, pseudonymous `local_player_reference`, and optional `display_alias`.
5. `public.fc27_quarantine_records`: Stores sanitized diagnostic metadata only (`anomaly_category` restricted to `VALIDATION_ANOMALY`, `SCHEMA_DRIFT`, `OUT_OF_RANGE_OBSERVATION`, `REPLAY_CONFLICT`, `INTEGRITY_MISMATCH`, `REVIEW_REQUIRED`, `SENSITIVE_FIELD_REJECTED`; `diagnostic_paths` restricted to field paths only; 30-day default retention window `expires_at`).

### 15.3 Integrity Hash Semantics, Replay Conflicts, & Quarantine Retention
- **Integrity & Change Detection Only**: Unkeyed SHA-256 digests (`payload_sha256`, `match_hash`) are classified strictly as `INTEGRITY_CHANGE_DETECTION` values. They do not authenticate the external source.
- **Idempotency & Replay Conflict Blocking**:
  - Re-applying an identical export or a match with an identical `(club_id, platform, game_version, external_match_id)` and `match_hash` is idempotent (`DUPLICATE_IGNORE`, emitting `fc27.export.duplicate_ignored`).
  - Re-importing an existing `external_match_id` with a modified `match_hash` is classified as `REPLAY_CONFLICT`, blocks the entire batch apply transactionally, and records sanitized quarantine metadata (`fc27.match.quarantined`).
- **Narrowly Authorized Retention Purge**: `fc27_app` has no `DELETE` privilege on `public.fc27_quarantine_records`. Expired records (`expires_at <= now()`) are purged exclusively through the `fc27_owner` `SECURITY DEFINER` function `public.fc27_purge_expired_quarantine(timestamptz)`, emitting `fc27.quarantine.expired`.

### 15.4 Grants Policy Version 14, Feature Gate, & Deferred Phases
- **Grants Policy Version 14**: `migrations/post/grants.sql` grants least-privilege `SELECT`/`INSERT`/column-scoped `UPDATE` to `fc27_app` and zero FC 27 persistence privileges to `fc27_dispatch` and `fc27_relay`. `PostgresSaver` internal tables remain untouched.
- **Default-Disabled Feature Flag**: `FC27_PERSISTENCE_ENABLED=false` (`is_fc27_persistence_enabled()` in `src/settings.py`). When disabled, `--apply --confirm-apply` returns `IMPORT_STATUS: DEFERRED` (exit code `1`) and performs zero database writes.
- **Deferred Phases**:
  - Live EA polling, recommendations, dispatch, Langfuse egress, and Discord alert relays remain disabled.

---

## 16. Read-Only PostgreSQL Analytics Boundary (`src/fc27/dashboard_queries.py`)

### 16.1 Analytics Window Decision & Season Equivalence Posture
- **Primary Rolling Window**: `LAST_20_COMPLETED_MATCHES` (`window_matches=20`, configurable within `1..250`).
- **Recent Comparison Window**: `LAST_5_COMPLETED_MATCHES` (`recent_window_matches=5`, constrained to `1..min(50, window_matches)`).
- **Maximum Calendar Cutoff**: `MAX_WINDOW_DAYS=730` (`window_days=730`, configurable within `1..730`).
- **Season Equivalence**: `SEASON_EQUIVALENCE=UNVERIFIED`. In-game FC 27 Clubs season reset timestamps are not exposed in offline exports, so queries are explicitly bounded by match-count windows and the 730-day calendar cutoff rather than claiming verified in-game season boundaries.

### 16.2 Read-Only Transaction & Privacy Enforcement
- **Feature Gate**: `FC27_ANALYTICS_ENABLED=false` by default (`is_fc27_analytics_enabled()` in `src/settings.py`). When disabled, analytics query helpers fail closed (`FC27AnalyticsDisabledError`) before opening any database connection.
- **Read-Only Session Posture**: Every query helper executes inside `conn.transaction()` with `SET LOCAL TRANSACTION READ ONLY` and `SET LOCAL statement_timeout = ...` (`100..30000` ms, default `5000` ms).
- **Parameterized SQL Only**: All SQL queries use static literals with `%s` placeholders; dynamic SQL concatenation is prohibited.
- **Aliases Hidden by Default**: `include_aliases=False` is the default across all query helpers (`query_club_overview`, `query_recent_match_log`, `query_player_rolling_summary`, `query_role_split_summary`, `query_club_analytics_snapshot`); `display_alias` is `None` unless `include_aliases=True` is explicitly requested by an authorized caller.
- **Zero Side Effects**: Analytics queries never read or modify `PostgresSaver` internal tables, never write to `public.domain_events` or `public.outbox`, and never generate tactical or roster recommendations.

---

## 17. Deterministic Pre-Approval Validation Boundary (`fc27-preapproval-v1`)

### 17.1 Validator Placement & Execution Order
Deterministic pre-approval validation runs **after** lead-package construction and **before** any human approval interrupt or `public.approval_requests` insertion:
```text
search -> scout -> analyst -> coach -> tactics -> director
  -> prepare_approval (build_lead_packages + validate_pre_approval_packages)
  -> [valid & approvable & !review_required] -> approval (interrupt) -> finalize_decision -> END
  -> [invalid or review_required]            -> finalize_decision (status="failed") -> END
```
Defense-in-depth enforcement is also applied in:
1. `ApprovalStore.complete_run` (`src/approval_store.py`): Re-validates `lead_packages` and `package_version` before inserting into `public.approval_requests`. If validation fails or `review_required=True`, the thread is persisted as `status = 'failed'` with `package_version = NULL` and zero `public.approval_requests` rows.
2. `ApprovalStore.issue_challenge` (`src/approval_store.py`): Rejects challenge nonce issuance (`409 pre_approval_validation_failed`) if `pre_approval_validation` indicates failure or `review_required=True`.
3. `approval` node (`src/graph.py`): Raises `ApprovalNotCommitted` before `interrupt()` if invoked without a passing `pre_approval_validation` state.

### 17.2 `PreApprovalValidationResult` Contract (`src/schemas.py`)
`PreApprovalValidationResult` is a frozen Pydantic model (`ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)`) with policy version `PRE_APPROVAL_POLICY_VERSION = "fc27-preapproval-v1"` containing:
- `valid: bool`, `approvable: bool`, `review_required: bool`
- `policy_version: str`, `validated_at: str`, `package_version: str | None`
- `reason_codes: tuple[PreApprovalReasonCode, ...]` (bounded `<= 64`), `warnings: tuple[str, ...]` (bounded `<= 64`)
- `validated_package_count: int`, `rejected_package_count: int`
- `confidence_tier: str | None`, `formula_version: str | None`, `formation_registry_version: str`

### 17.3 Package Integrity, Objective Bounds, Formation Registry, & Sample Policy
- **Package Integrity**: Verifies non-empty `lead_packages` (`EMPTY_LEAD_PACKAGES`), schema validity (`PACKAGE_SCHEMA_INVALID`), 64-hex SHA-256 `package_version` presence (`PACKAGE_HASH_MISSING`), format (`PACKAGE_HASH_INVALID`), and canonical recomputation via `src.decision.package_version` (`PACKAGE_HASH_MISMATCH`), candidate consistency against `candidates` (`CANDIDATE_ID_MISMATCH`), and duplicate role/candidate/player detection (`DUPLICATE_CANDIDATE_PACKAGE`).
- **Objective Bounds Reuse**: Reuses `METRIC_BOUNDS` and `validate_metric_bounds` from `src/dispatch/objective.py` and `Objective` from `src/schemas.py` (`OBJECTIVE_METRIC_UNSUPPORTED`, `OBJECTIVE_BOUND_VIOLATION`, `OBJECTIVE_VALUE_NONFINITE` for `NaN`, `+Inf`, `-Inf`).
- **Formation & Assignment Registry Reuse**: Reuses `VALID_FC27_FORMATIONS`, `VALID_FC27_POSITIONS`, and `VALID_FC27_ROLES` from `src/schemas.py` (`FORMATION_UNSUPPORTED`, `ROLE_ASSIGNMENT_INVALID`, `DUPLICATE_PLAYER_ASSIGNMENT`, `ROSTER_SIZE_INVALID`). When current role-ranking packages do not carry formation/lineup structures, `formation_registry_version` is reported truthfully as `"NOT_APPLICABLE"` rather than fabricating formation metadata; when present, it reports `"fc27-formations-v1"`.
- **Confidence & Sample Policy**: Reuses `FORMULA_VERSION = "fc27-v1.0"` and `determine_sample_size_tier` from `src/fc27_analytics.py` (`CONFIDENCE_METADATA_MISSING`, `FORMULA_VERSION_UNSUPPORTED`, `INSUFFICIENT_SAMPLE`, `INSUFFICIENT_DATA`, `REVIEW_REQUIRED`).
- **Recommendation Gate**: Reuses `is_fc27_recommendations_enabled()` (`FC27_RECOMMENDATIONS_ENABLED=false` by default). Any recommendation object or marker when disabled fails closed with `RECOMMENDATION_DISABLED`.

### 17.4 Metadata-Only Tracing, `PostgresSaver` Non-Modification, & Rollback
- **Metadata-Only Tracing (`emit_pre_approval_validation_trace`)**: Emits `preapproval.validation.passed`, `preapproval.validation.failed`, or `preapproval.validation.review_required` via the existing tracer abstraction with safe metadata only (`thread_id`, `club_id`, `package_version`, `policy_version`, `package_count`, `reason_codes`, `confidence_tier`, `formula_version`, `timestamp`). Never emits player aliases, raw payloads, candidate bodies, prompts, or connection strings.
- **Database & `PostgresSaver` Non-Modification**: No database migration is added in this phase (`migrations/0001` through `0012` are unchanged), `PostgresSaver` internal tables (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations`) are untouched, and `GRAPH_THREAD_DB_CONSTRAINT` remains `DEFERRED`.
- **Rollback Procedure**: Revert the pure code changes in `src/schemas.py`, `src/lead_packages.py`, `src/graph.py`, `src/approval_store.py`, and `tests/test_fc27_preapproval_validator.py`; no database schema rollback is required.

---

## 18. East Coasters FC 27 API Acquisition, Transport Policy, and Stage 4 Exception Gate

### 18.1 Operator Club-Authority & Member-Consent Attestation (`src/fc27_clubs.py`)
- **Attestation Record (`EAST_COASTERS_API_AUTHORITY_ATTESTATION`)**:
  - `attestation_reference = "api-east-coasters-authority-2026-10"`
  - `club_id = "330323"`, `club_name = "East Coasters"`, `platform = "common-gen5"`, `game_version = "FC27"`
  - `authority_status = "USER_ATTESTED"`, `member_consent_status = "USER_ATTESTED"`, `captain_consent_status = "USER_ATTESTED"`
  - `review_status = "PENDING_HUMAN_REVIEW"`, `manager_authority_database_status = "UNVERIFIED"`
- **Boundary Rule**: `USER_ATTESTED` authorizes bounded technical acquisition and normalization preview for East Coasters (`330323`) only. It does **not** set `manager_authority_status = 'VERIFIED'` in `public.fc27_clubs`, and member/player performance rows cannot be persisted unless `review_status == 'APPROVED'` and `AUTHORIZE_NORMALIZED_EAST_COASTERS_PERSISTENCE=YES`.

### 18.2 Upstream Client Provenance & Transport Classification
Both community repositories are third-party client implementations for EA's underlying `https://proclubs.ea.com/api/fc/...` endpoints, not independent data providers:
1. **Python Reference (`https://github.com/1erkandogan/fc27-clubs-api`)**:
   - Pinned Commit: `26a5ff12ea793042bd776a858c74f3f44dd427f5` (`0.3.1`, `MIT`).
   - Transport Note: Upstream `src/fc_clubs_api/_http.py` sends spoofed Chrome 141 headers (`User-Agent: Mozilla/5.0 ... Chrome/141.0.0.0`, `sec-ch-ua`, `sec-fetch-site: same-origin`, `Referer: https://www.ea.com/`). Only pure normalization and event-decoding logic are adapted in `src/fc27_clubs.py`; upstream HTTP headers are excluded.
2. **TypeScript/Node Reference (`https://github.com/alex-jordan547/proclubs-sdk`)**:
   - Pinned Commit: `57ede368098dbb524cbf16a7577fea3e1911e328` (`0.3.3`, `MIT`).
   - Transport Note: Upstream `src/transport/impitTransport.ts` uses `impit` (`new Impit({ browser: 'chrome' })`) for Rust-level Chrome TLS/HTTP2 fingerprint impersonation alongside Chrome headers. `impit` and browser-profile headers are prohibited in standard repository code.

### 18.3 Standards-Compliant 6-Operation Transport Policy
- **Feature Gate**: `FC27_STANDARD_HTTP_TRANSPORT_ENABLED=false` by default (`is_fc27_standard_http_transport_enabled()` in `src/settings.py`).
- **Headers**: Only `Accept: application/json` and `User-Agent: iwa.dev.fcbuild/0.1` (`STANDARD_APPLICATION_USER_AGENT`). Browser `Mozilla/5.0`, `Chrome/*`, `sec-ch-ua*`, `Sec-Fetch-*`, `Origin`, `Referer`, and `Cookie` headers are rejected before socket creation.
- **Operations (`FC27_API_INGESTION_POLICIES`)**:
  - `CLUB_IDENTITY`: `/api/fc/clubs/info` (`platform=common-gen5`, `clubIds=330323`, max `1,048,576` bytes)
  - `CLUB_STATISTICS`: `/api/fc/clubs/overallStats` (`platform=common-gen5`, `clubIds=330323`, max `1,048,576` bytes)
  - `RECENT_MATCHES`: `/api/fc/clubs/matches` (`platform=common-gen5`, `clubIds=330323`, `matchType=leagueMatch|playoffMatch|friendlyMatch`, `maxResultCount=1..25`, max `1,048,576` bytes)
  - `MATCH_PLAYER_STATISTICS`: `/api/fc/clubs/matches` (`platform=common-gen5`, `clubIds=330323`, `matchType=leagueMatch|playoffMatch|friendlyMatch`, `maxResultCount=1..25`, max `1,048,576` bytes)
  - `CURRENT_MEMBER_STATISTICS`: `/api/fc/members/stats` (`platform=common-gen5`, `clubId=330323`, max `1,048,576` bytes)
  - `MEMBER_CAREER_STATISTICS`: `/api/fc/members/career/stats` (`platform=common-gen5`, `clubId=330323`, max `1,048,576` bytes)
- **Safety Invariants**: `https` only, exact host `proclubs.ea.com` (default port `443`), `GET` only, `timeout_seconds = 10`, `max_attempts = 1`, `retries = 0`, `follow_redirects = False`. Any `401`, `403`, `429`, redirect (`301`/`302`/`303`/`307`/`308`), HTML challenge, or CAPTCHA immediately opens `FC27CircuitBreaker` and halts subsequent stages.

### 18.4 Empirical Stage F1 Execution & Stage 4 Browser-Profile Exception Gate
- **Stage F1 (`CLUB_IDENTITY`) Execution**:
  - Executed `1` bounded `STANDARD_PYTHON_HTTPS` `GET` request to `https://proclubs.ea.com/api/fc/clubs/info?platform=common-gen5&clubIds=330323` with `Accept: application/json` and `User-Agent: iwa.dev.fcbuild/0.1`.
  - Result: `HTTP 403 Forbidden` (`content_type="text/html"`, `response_size_bytes=403`, `outcome_status="HTTP_403_FORBIDDEN"`).
  - Circuit Breaker: Immediately transitioned to `OPEN` (`circuit_breaker_reason="HTTP_403"`).
  - Subsequent Stages (`F2_CLUB_STATISTICS`, `F3_RECENT_MATCHES`, `F4_MEMBER_AND_PLAYER_PREVIEW`): Halted with `0` requests executed, `0` retries, `0` redirects followed, and `0` raw response bytes retained.
- **Stage 4 Exception Gate (`FC27Stage4BrowserProfileExceptionAssessment`)**:
  - `STANDARD_HTTP_WORKED = NO`
  - `BROWSER_PROFILE_EXCEPTION_REQUIRED = YES`
  - `BROWSER_PROFILE_EXCEPTION_EXECUTED = NO`
  - Executing browser headers or TLS impersonation (`impit` / `curl_cffi`) remains prohibited without a separate explicit operator exception decision, isolated Docker egress containment (`proclubs.ea.com:443` only), and legal/ToS sign-off.
- **Normalized API Capture Persistence & Analytics Readiness**:
  - `normalize_api_capture_to_export_envelope`, `persist_authorized_east_coasters_capture` (`source_classification='AUTHORIZED_CAPTURE'`, pseudonymous `plr_ecf_<hash12>` player keys, `display_alias=NULL`), and `validate_east_coasters_read_only_analytics` are implemented and verified in `tests/test_fc27_api_ingestion.py` using deterministic fixtures against PostgreSQL 16. No live EA data was persisted in this phase (`PERSISTENCE_APPLIED=NO`).

---

## 19. East Coasters Operator-Captured Displayed Data Workflow & Data-Portability Governance

### 19.1 Operator-Captured Displayed Data Contract (`OPERATOR_CAPTURED_DISPLAYED_DATA`)
- **Semantic Definition**:
  - `OPERATOR_CAPTURED_DISPLAYED_DATA` denotes normalized records manually transcribed by a club-authorized East Coasters operator from values visibly displayed in an authorized game/team interface or internal club records.
  - Data is **not** obtained by replaying hidden browser requests, using `proclubs-sdk`/`impit`, browser TLS impersonation, browser automation, or credentials stored by this repository.
  - Normalized records remain `PROVISIONAL` (`data_quality_status = 'PROVISIONAL'`, `manager_authority_status = 'UNVERIFIED'`) until independent human review; they are never mapped conceptually to provider-verified data.
- **PostgreSQL `0012` Persistence Mapping**:
  - To preserve migration `0012_fc27_performance_and_quarantine.sql` unchanged (`MIGRATIONS_MODIFIED=NO`), `OPERATOR_CAPTURED_DISPLAYED_DATA` is preserved verbatim in `FC27UserExportEnvelope.source_classification`, `compute_match_hash`, and `domain_events.reason`, while `src/fc27_repository.py` maps the SQL `source_classification` column to the compatible `'USER_SUPPLIED_EXPORT'` `CHECK` value and forces `data_quality_status = 'PROVISIONAL'` and `display_alias = NULL`.
- **Fixed Identity & Attestation Invariants**:
  - Club Identity: `club_id = "330323"`, `club_name = "East Coasters"` (permitting only NFKC Unicode normalization, leading/trailing whitespace removal, repeated-space collapse, and case-insensitive exact match), `platform = "common-gen5"`, `game_version = "FC27"`.
  - Attestation (`FC27OperatorCaptureAttestation`):
    - `attestation_reference = "api-east-coasters-authority-2026-10"`
    - `operator_authority_status = "USER_ATTESTED"`
    - `member_consent_status = "USER_ATTESTED"`
    - `captain_consent_status = "USER_ATTESTED"`
    - `manager_authority_database_status = "UNVERIFIED"`
    - `allowed_purpose = "TEAM_REVIEW"`
    - `publication_allowed = false`, `recommendations_allowed = false`, `dispatch_allowed = false`
- **Privacy & Raw-Input Disposal**:
  - Player references must use pseudonymous format `plr_ecf_<bounded deterministic reference>` (`^plr_ecf_[a-z0-9_]{3,48}$`).
  - `STORE_DISPLAY_ALIASES=NO`, `STORE_GAMER_TAGS=NO`, `STORE_EXTERNAL_PLAYER_IDENTIFIERS=NO`, `PUBLICATION_ALLOWED=NO`.
  - Unknown optional player statistics remain `null` (`None`) in domain validation and are never silently substituted with `0`.
  - `normalize_operator_displayed_capture` reads input into a bounded mutable `bytearray` and overwrites every byte with `0` in `finally` before releasing the buffer (`RAW_INPUT_RETAINED_BY_APPLICATION=NO`).

### 19.2 JSON Schema, Synthetic Template, and CLI Commands
- **JSON Schema (Draft 2020-12)**: [`config/fc27_east_coasters_normalized_export.schema.json`](../config/fc27_east_coasters_normalized_export.schema.json)
- **Synthetic Test Template**: [`tests/synthetic_fc27_east_coasters_normalized_template.json`](../tests/synthetic_fc27_east_coasters_normalized_template.json) (`data_classification="SYNTHETIC"`, `real_player_data=false`, `safe_for_tests=true`)
- **CLI Commands (`scripts/fc27_user_export_import.py`)**:
  - **Validate (default safe mode, zero DB connections)**:
    ```powershell
    .\.venv\Scripts\python.exe scripts/fc27_user_export_import.py --input .local-data/east-coasters-fc27-normalized.json --format json --operator-displayed --validate
    ```
  - **Dry-Run (zero write transactions)**:
    ```powershell
    .\.venv\Scripts\python.exe scripts/fc27_user_export_import.py --input .local-data/east-coasters-fc27-normalized.json --format json --operator-displayed --dry-run
    ```
  - **Apply (separately gated by `FC27_PERSISTENCE_ENABLED=true` and `--confirm-apply`)**:
    ```powershell
    .\.venv\Scripts\python.exe scripts/fc27_user_export_import.py --input .local-data/east-coasters-fc27-normalized.json --format json --operator-displayed --apply --confirm-apply
    ```

### 19.3 Product-Supported Export Availability Assessment
The operator checked official EA SPORTS FC product surfaces and documentation for supported club data export mechanisms:

| Export / Access Mechanism | Surface Checked | Availability Status | Notes |
| :--- | :--- | :--- | :--- |
| **In-Game Export** | EA SPORTS FC Clubs in-game menus (Club Overview, Match History, Squad Hub) | `NOT_FOUND` | Game UI displays recent match summaries and member stats on-screen only; no JSON/CSV export function exists in-game. |
| **EA Account Data Export** | EA Account & Privacy Settings (`myaccount.ea.com` -> Privacy -> Download Your Data) | `REQUIRES_EA_CONFIRMATION` | Individual account archive exists for a single player's EA account, but whether it includes multi-member Club (`330323`) match tables in structured form is `NOT_DOCUMENTED`. |
| **Downloadable Match History** | EA SPORTS FC Web App & Companion App | `NOT_FOUND` | Web/Companion apps target Ultimate Team (FUT) management and do not expose downloadable Clubs match history exports. |
| **Club-Management Export** | Clubs Manager / Commissioner in-game tools | `NOT_FOUND` | No commissioner roster or match-log export tool is provided in the Clubs management interface. |
| **Official API or Partner Access** | EA developer portals & public documentation | `NOT_DOCUMENTED` | EA does not publish public developer documentation, API keys, or OAuth scopes for `/api/fc/clubs/*` endpoints. |
| **Support Request for Data Portability** | EA Help / Privacy Support (`help.ea.com`) | `REQUIRES_EA_CONFIRMATION` | Requires submitting a formal inquiry to EA Privacy / Player Support to confirm whether club-level match history for Club ID `330323` can be exported upon manager request. |

### 19.4 Concise Permission / Data-Portability Request Draft to EA (`EA_PERMISSION_REQUEST_SENT=NO`)
> **Status**: Drafted in documentation only (`EA_PERMISSION_REQUEST_SENT = NO`). Do **not** transmit automatically.

```text
Subject: Request for Authorized Access or Data Export Method – EA SPORTS FC Clubs (Club ID: 330323, "East Coasters", common-gen5)

To: EA SPORTS FC Community / Developer Relations & Data Privacy Team

Hello,

I am the club manager for the EA SPORTS FC Clubs team "East Coasters" (Club ID: 330323, platform: common-gen5). With the explicit consent of our participating club members, we maintain a private, non-commercial team analytics repository to review our own club match results, formations, and pseudonymized player performance metrics.

We observed that standard HTTPS requests (without browser TLS impersonation) to the unauthenticated web endpoints under https://proclubs.ea.com/api/fc/clubs/info are blocked by edge CDN filtering (HTTP 403), and we do not wish to use browser-profile TLS impersonation or unauthorized scraping against EA services.

Could you please advise whether EA provides:
1. An officially supported export tool or data-portability download for Club ID 330323 match history and club statistics on platform common-gen5; or
2. Written permission, an allowlisted User-Agent/API token, or an approved read-only interface for low-volume, manual retrieval of our own club's statistics (Club ID 330323)?

Scope of requested access:
- Club: East Coasters (Club ID: 330323, platform: common-gen5)
- Purpose: Internal, non-commercial team coaching and performance analytics
- Frequency: Manual post-session retrieval only (no continuous polling, no public redistribution, no third-party scraping)

Thank you for your time and guidance.

Sincerely,
Club Manager, East Coasters (Club ID: 330323)
```

---

## 20. Authorized API-First Source Discovery, Provider-Neutral Contract, & Selection Gate

### 20.1 API-First Requirement & Non-Negotiable Transport Boundaries
- **API-First Objective (`API_FIRST_REQUIREMENT=YES`, `MANUAL_FILE_PRIMARY_PATH=NO`)**:
  - The operator requires API-based automated read-only acquisition for East Coasters (`club_id = "330323"`, `platform = "common-gen5"`, `game_version = "FC27"`). Manual JSON/CSV entry is retained only as an auxiliary fallback and is not the primary solution.
- **Strict Prohibition of Bypass Transports**:
  - Ordinary standards-compliant HTTPS requests to `https://proclubs.ea.com/api/fc/clubs/info` return `HTTP 403 Forbidden` (Akamai edge filtering).
  - Using Chromium, Chrome, Playwright, Puppeteer, Selenium, `impit`, `curl-impersonate`, browser client hints (`sec-ch-ua*`, `Sec-Fetch-*`), TLS/JA3/JA4/HTTP2 fingerprint impersonation, EA cookies, EA sessions, CAPTCHA handling, or proxy rotation to bypass `HTTP 403` is strictly prohibited.
  - A provider or API endpoint may be integrated for live automated access **only** if it explicitly grants automated read-only access rights, normalized-storage rights, 730-day retention rights, and consented member/player data processing rights.

### 20.2 Candidate API Source Discovery & Evaluation Matrix (`PROVIDERS_REVIEWED=9`)
Nine candidates across all seven source categories were evaluated in `src/fc27_authorized_provider.py` (`REVIEWED_API_SOURCE_CANDIDATES`):

| # | Candidate Provider | Legal Entity | Category | Classification | FC 27 `330323` (`common-gen5`) Support | Proxies `proclubs.ea.com` | Browser / TLS Impersonation or Session Required | Storage & 730-Day Retention Rights | Selection Decision |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 1 | `EA_FC_PROCLUBS_PUBLIC_WEB_ENDPOINTS` | Electronic Arts Inc. | 1. Official EA web surface | `NOT_ACCEPTABLE` | Yes (blocked by HTTP 403) | No (first-party) | Yes (Akamai blocks standard HTTP) | `NOT_GRANTED` (EA User Agreement Sec. 6) | **Rejected** (HTTP 403 without browser/TLS impersonation) |
| 2 | `EA_APPROVED_PARTNER_OR_WRITTEN_ACCESS_PROGRAM` | Electronic Arts Inc. | 2. EA partner / approved access | `NOT_ACCEPTABLE` (until granted) | Yes (subject to EA approval) | No (first-party) | No (once allowlisted/keyed) | `REQUIRES_WRITTEN_EA_APPROVAL` | **Blocked Pending EA Response** (`EA_ACCESS_REQUEST_SENT=NO`) |
| 3 | `EA_ACCOUNT_DATA_PORTABILITY_EXPORT_SERVICE` | Electronic Arts Inc. | 3. EA data-portability export | `NOT_ACCEPTABLE` | Individual account archive only | No | Yes (manual account login) | Personal archive only | **Rejected** (not an automated Club `330323` API) |
| 4 | `PANDASCORE_AND_SPORTRADAR_ESPORTS_APIS` | PandaScore SAS / Sportradar AG | 4. Licensed esports providers | `NOT_ACCEPTABLE` | No (pro broadcast tournaments only) | No | No | Commercial contract (tournament data only) | **Rejected** (zero retail FC 27 Clubs `330323` coverage) |
| 5 | `FC27_CLUBS_API_PYTHON_CLIENT` | Erkan Dogan (`1erkandogan/fc27-clubs-api`) | 5. Open-source client | `UNOFFICIAL_CLIENT` | Client code only | Yes (calls `proclubs.ea.com`) | Yes (spoofed Chrome 141 headers) | `NONE` (MIT covers code only, not EA data) | **Rejected** (`UNOFFICIAL_CLIENT`) |
| 6 | `PROCLUBS_SDK_TYPESCRIPT_CLIENT` | Alex Jordan (`alex-jordan547/proclubs-sdk`) | 5. Open-source client | `UNOFFICIAL_CLIENT` | Client code only | Yes (calls `proclubs.ea.com`) | Yes (`impit` Rust Chrome TLS impersonation) | `NONE` (MIT covers code only, not EA data) | **Rejected** (`UNOFFICIAL_CLIENT`) |
| 7 | `PROCLUBSHEAD_AND_TRACKER_WEB_SITES` | Third-party stat sites (`proclubshead.com`, `tracker.gg`) | 6. Gaming stat websites | `UNVERIFIED_RELAY` | Partial web views only | Yes | Yes | `PROHIBITED_BY_SITE_TERMS` | **Rejected** (`UNVERIFIED_RELAY`) |
| 8 | `COMMUNITY_HOSTED_PROCLUBS_PROXY_RELAYS` | Unverified community proxy hosts | 6. Community proxy wrappers | `UNVERIFIED_RELAY` | Unverified proxy | Yes | Yes (server-side bypass) | `UNVERIFIED` (no EA sublicense) | **Rejected** (`UNVERIFIED_RELAY`) |
| 9 | `PUBLIC_STATIC_EAFC_RATINGS_DATASETS` | Community open-data contributors (Kaggle) | 7. Public static datasets | `NOT_ACCEPTABLE` | No (offline attributes only) | No | No | Static offline ratings only | **Rejected** (no East Coasters Clubs records) |

- **Summary Counts**:
  - `PROVIDERS_REVIEWED = 9`
  - `OFFICIAL_AUTHORIZED_PROVIDERS = 0` (`OFFICIAL_PROVIDERS_FOUND = 0`)
  - `LICENSED_THIRD_PARTY_PROVIDERS = 0` (`LICENSED_THIRD_PARTY_PROVIDERS_FOUND = 0`)
  - `COMMUNITY_DOCUMENTED_PROVIDERS = 0` (`COMMUNITY_DOCUMENTED_PROVIDERS_FOUND = 0`)
  - `UNOFFICIAL_CLIENTS_REJECTED = 2`
  - `UNVERIFIED_RELAYS_REJECTED = 2`
  - `NOT_ACCEPTABLE_REJECTED = 5`

### 20.3 Refined EA Approved-Access & Data-Portability Request (`EA_ACCESS_REQUEST_READY=YES`, `EA_ACCESS_REQUEST_SENT=NO`)
> **Status**: Ready in [`src/fc27_authorized_provider.py`](../src/fc27_authorized_provider.py) (`EA_APPROVED_ACCESS_REQUEST_DRAFT`) and below (`EA_PERMISSION_REQUEST_READY = YES`, `EA_ACCESS_REQUEST_READY = YES`, `EA_PERMISSION_REQUEST_SENT = NO`, `EA_ACCESS_REQUEST_SENT = NO`). Do **not** transmit without separate operator authorization.

```text
Subject: Formal Request for Approved Read-Only API Access or Structured Club Data Portability — EA SPORTS FC 27 Clubs ("East Coasters", Club ID: 330323, Platform: common-gen5)

To: Electronic Arts Inc. — EA SPORTS FC Developer Relations, Partner Licensing, & Data Privacy Team

1. Requesting Club & Authority Attestation
- Club Name: East Coasters
- Club ID: 330323
- Platform: common-gen5
- Game Version: FC27
- Attestation Reference: api-east-coasters-authority-2026-10
- Authority & Consent Posture: Submitted by the club-authorized operator with explicit captain and participating member consent (USER_ATTESTED) strictly for internal, non-commercial team performance analysis (INTERNAL_TEAM_ANALYTICS / TEAM_REVIEW).
- Operator Contact: [OPERATOR_CONTACT_PLACEHOLDER — DO_NOT_COMMIT_PERSONAL_EMAIL]

2. Requested Access Pathways (Any One Approved Pathway)
1. Official club-data export API or scheduled archive for Club ID 330323
2. Approved read-only API endpoint with an allowlisted application User-Agent or API key
3. EA Partner or developer-program read-only credentials
4. Written authorization for low-rate, standards-compliant HTTPS read-only access to Club ID 330323 records
5. Structured data-portability archive containing Club ID 330323 match and member records

3. Requested Read-Only Data Scope (Scoped Exclusively to Club ID 330323 on common-gen5)
- CLUB_IDENTITY (club ID, name, platform, region/kit metadata)
- CLUB_STATISTICS (aggregate matches played, wins, draws, losses, goals for/against, division, skill rating)
- RECENT_MATCHES (match ID, UTC timestamp, competition type, result, score, opponent name, formation)
- MATCH_TEAM_STATISTICS (possession, shots, shots on target, pass accuracy, tackles, fouls)
- MATCH_PLAYER_STATISTICS (per-match position, minutes, goals, assists, rating, passes, tackles, interceptions, saves)
- CURRENT_MEMBER_STATISTICS (current club games, goals, assists, clean sheets, average rating, pass/tackle success)
- MEMBER_CAREER_STATISTICS (career club games, goals, assists, average rating)
- PLAYOFF_ACHIEVEMENTS (season ID, season name, best division, playoff record, finishing position)

4. Technical, Security, Privacy, Retention, & Deletion Controls
- Request Rate Budget: Read-only HTTPS GET only; initially 1 request per manually authorized operation, strictly capped at <= 5 requests/minute and <= 6 requests/session. Continuous background polling is disabled.
- Standards-Compliant Transport: No cookie or session reuse, no EA account credential storage, and no browser automation or TLS fingerprint impersonation (no Impit/Chromium).
- Data Minimization & Pseudonymization: Raw HTTP responses are zeroed in memory immediately after parsing and never persisted. All member/player identifiers are deterministically pseudonymized (plr_ecf_<hash12>); gamer tags, PSN/Xbox IDs, EA IDs, Persona IDs, and Nucleus IDs are suppressed.
- Non-Publication & Non-Resale: No public player-data publication and no commercial redistribution or resale.
- Retention & Deletion Procedure: Maximum 730-day retention for normalized records in access-controlled PostgreSQL 16 storage (Grants Policy v14), with immediate record purge upon member consent withdrawal or EA request. Recommendations and automated dispatch remain disabled.
```

### 20.4 Provider-Neutral Contract & Bounded Transport Policy
- **Modules**:
  - [`src/fc27_authorized_provider.py`](../src/fc27_authorized_provider.py) & [`src/fc27_provider_contract.py`](../src/fc27_provider_contract.py)
  - [`src/fc27_authorized_transport.py`](../src/fc27_authorized_transport.py) & [`src/fc27_authorized_api.py`](../src/fc27_authorized_api.py)
- **Supported Operations (`FC27AuthorizedOperation`)**:
  - `CLUB_IDENTITY`, `CLUB_STATISTICS`, `RECENT_MATCHES`, `MATCH_PLAYER_STATISTICS`, `CURRENT_MEMBER_STATISTICS`, `MEMBER_CAREER_STATISTICS`, `PLAYOFF_ACHIEVEMENTS`.
- **Fail-Closed Declaration (`FC27AuthorizedProviderDeclaration`)**:
  - Requires `OFFICIAL_AUTHORIZED` or `LICENSED_THIRD_PARTY` classification, HTTPS documentation/terms/privacy URLs, non-empty `terms_version`, `privacy_policy_version`, `license_or_agreement`, `authorization_reference`, future UTC `authorization_expires_at`, exact scope `approved_club_ids=("330323",)`, `approved_platforms=("common-gen5",)`, `approved_game_versions=("FC27",)`, `normalized_storage_permission=True`, `retention_permission=True` (`max_retention_days >= 730`), and operation-specific `member_data_permission` / `player_data_permission`.
- **Bounded Transport (`FC27AuthorizedTransportClient`)**:
  - Enforces HTTPS only, exact host/path/query allowlists, timeout `<= 10.0`s, response ceiling `<= 1,048,576` bytes, redirect refusal, duplicate-key and `NaN`/`Infinity` JSON rejection, recursive sensitive-field scanning, in-place `bytearray` zeroing in `finally`, API-key/Authorization redaction (`redact_sensitive_auth_text`), rate-budget tracking (`FC27RateBudgetTracker`), and `FC27CircuitBreaker`. Exposes zero arbitrary URL (`get_json`) methods.

### 20.5 Provider Selection Gate Decision (`Part H`)
- `SELECTED_PROVIDER = NONE`
- `SELECTED_PROVIDER_CLASSIFICATION = NONE`
- `LIVE_PROVIDER_IMPLEMENTATION = NOT_STARTED`
- `LIVE_REQUEST_EXECUTED = NO`
- `LIVE_REQUESTS_EXECUTED = 0`
- `BLOCKER = NO_AUTHORIZED_API_SOURCE_FOUND`
