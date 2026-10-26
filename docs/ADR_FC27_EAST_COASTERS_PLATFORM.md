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
