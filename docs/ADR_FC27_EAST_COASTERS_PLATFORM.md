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
  - `FC27_RECOMMENDATIONS_ENABLED=false`
  - `DISPATCH_ENABLED=false`
- All security postures established in Phase 1 (least-privilege database roles, HMAC/WebAuthn human approval requirements, immutability triggers) remain inviolable.
