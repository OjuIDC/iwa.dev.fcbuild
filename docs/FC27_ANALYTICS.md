# FC 27 Deterministic Analytics, Search, Scout, and Analyst Specification

- **Status**: Current
- **Audience**: Analysts, Developers, Commissioners
- **Last verified commit**: `b0ff4bae36ab1f31defe2f39682eb105e30254e5`
- **Source of truth**: `src/fc27_analytics.py`, `src/fc27_search_scout.py`, `src/fc27_analyst.py`, `src/fc27_dashboard_queries.py`, `src/schemas.py`, `src/settings.py`
- **Supersedes**: `NONE`

> **Formula Version**: `FORMULA_VERSION = "fc27-v1.0"` ([`src/fc27_analytics.py`](../src/fc27_analytics.py), [`src/fc27_search_scout.py`](../src/fc27_search_scout.py), [`src/fc27_analyst.py`](../src/fc27_analyst.py))
> **Default Runtime Gate**: `FC27_ANALYTICS_ENABLED=false` ([`src/settings.py`](../src/settings.py))

---

## 1. Design Principles & Pure Functional Core

The FC 27 analytics foundation in `src/fc27_analytics.py`, `src/fc27_search_scout.py`, and `src/fc27_analyst.py` is a **100% deterministic, side-effect-free pure computation layer**:

- **No LLM / Generative Inference**: Search context assembly (`build_fc27_search_context`), Scout evidence synthesis (`build_fc27_scout_report`), and Analyst evidence synthesis (`build_fc27_analyst_report`) use pure descriptive arithmetic and deterministic rule-based validation only. Zero LLM calls are executed.
- **Evidence Only (No Recommendations or Prescriptions)**: Search, Scout, and Analyst produce bounded observational evidence, evidence clusters, counter-evidence, confounders, data gaps, unresolved questions, and observational findings (`observation_classification="OBSERVATIONAL_ONLY"`, `recommendation_status="NOT_A_RECOMMENDATION"`, `approvable=False`, `dispatchable=False`, `recommendation=False`, `causal_claim=False`). They never generate coaching recommendations, tactical prescriptions, formation selections, player instructions, build assignments, approval packages, or dispatch actions.
- **No Network or Database Side Effects in Pure Functions**: Pure functions accept immutable normalized records and an injected UTC-aware `analysis_generated_at` timestamp, returning frozen Pydantic models with deterministic SHA-256 hashes (`context_hash`, `report_hash`).
- **Formula Version Pinning**: Every output model binds `FORMULA_VERSION = "fc27-v1.0"` (`RANKING_POLICY_VERSION = "fc27-rank-v1"` in `src/lead_packages.py`).

---

## 2. East Coasters Identity Scope & Match Eligibility (`build_fc27_search_context`)

`build_fc27_search_context` (`src/fc27_search_scout.py`) enforces fixed East Coasters identity and strict record hygiene before assembling the analytical window:

1. **Fixed Identity Scope (`FC27AnalysisIdentity`)**:
   - `club_id = "330323"`
   - `club_name = "East Coasters"`
   - `platform = "common-gen5"`
   - `game_version = "FC27"`
2. **Eligible Matches**: Only matches with `completion_state == "COMPLETED"`, valid East Coasters identity/platform/game_version, approved `match_type` (`LEAGUE`, `PLAYOFF`, `FRIENDLY`, `CUP`, `PRACTICE`, `UNKNOWN`), `data_quality_status` in `{"ACCEPTED", "PROVISIONAL", "VERIFIED"}`, and `played_at <= analysis_generated_at + 300s` (clock-skew tolerance) are included.
3. **Deterministic Exclusion Accounting (`FC27ExclusionSummary`)**:
   - Every candidate match is accounted for across `total_candidates`, `eligible_records`, `excluded_duplicates`, `excluded_conflicts`, `excluded_wrong_identity`, `excluded_wrong_platform`, `excluded_wrong_game_version`, `excluded_status`, `excluded_data_quality`, `excluded_future_timestamp`, `excluded_quarantine`, `excluded_missing_required_field`, `excluded_rejected_batch`, and `excluded_window_bound`.
   - Unknown optional metrics (`possession`, `shots`, `shots_on_target`, `passes_attempted`, `passes_completed`, `tackles_attempted`, `tackles_won`, `interceptions`, `saves`) remain `None` (unavailable) and are never substituted with `0`.

---

## 3. Sample-Size Confidence Tiers (`determine_sample_size_tier`)

To prevent small-sample over-interpretation, `determine_sample_size_tier(match_count)` in `src/fc27_analytics.py` (and `SAMPLE_SIZE_TIERS` in `src/schemas.py`) classifies every analytical window, subgroup, and hypothesis into one of five authoritative tiers:

| Confidence Tier | Eligible Completed Matches | Analytical & Hypothesis Interpretation |
|---|---|---|
| `INSUFFICIENT_DATA` | `0 .. 2` | Below minimum exploratory sample (`< 3` matches). Trend descriptions and hypothesis generation (`FC27Hypothesis`) are strictly blocked. |
| `EXPLORATORY` | `3 .. 4` | Early exploratory descriptive observations only (`3..4` matches). High variance; not eligible for coaching recommendations. |
| `LOW_CONFIDENCE` | `5 .. 9` | Low-confidence descriptive correlations (`5..9` matches). Below the `10`-match threshold required for pre-approval coaching packages. |
| `MODERATE_CONFIDENCE` | `10 .. 19` | Moderate-confidence multi-match observations (`10..19` matches). Eligible for downstream Analyst/Coach evaluation when enabled. |
| `HIGH_CONFIDENCE` | `20+` | Full 20-match baseline window (`>= 20` matches). Highest descriptive confidence tier; still observational correlation only. |

Every metric group and hypothesis carries `eligible_sample_count`, `confidence_tier`, `data_quality_status`, `evidence_references`, and `limitations`. No hypothesis may carry a `confidence_tier` higher than its underlying evidence.

---

## 4. Context Windows & Deterministic Ordering

Defined in `src/fc27_dashboard_queries.py` and `src/fc27_search_scout.py`:

- **Baseline Window (`BASELINE_MATCH_LIMIT = 20`)**: Latest `20` eligible completed matches ordered deterministically by `(-played_at.timestamp(), match_id ASC, match_hash ASC)`.
- **Recent-Form Window (`RECENT_FORM_MATCH_LIMIT = 5`)**: Latest `5` eligible completed matches from the baseline window.
- **Optional Rolling Horizon (`FC27_ANALYTICS_MAX_WINDOW_DAYS = 730`)**: Optional rolling day filter (`1 <= window_days <= 730`) evaluated relative to the injected `analysis_generated_at` timestamp.

---

## 5. Scout Evidence Metrics (`build_fc27_scout_report`)

`build_fc27_scout_report(search_context)` in `src/fc27_search_scout.py` produces an immutable `FC27ScoutReport` with a canonical SHA-256 `report_hash`:

1. **Team Evidence (`FC27TeamEvidence`)**: Win/draw/loss rates (`None` when `0` matches), `goals_per_match`, `goals_conceded_per_match`, `average_goal_differential`, `clean_sheet_percentage`, recent-form (last 5) vs. baseline (last 20) deltas, and optional `club_side_splits` / `match_type_splits`.
2. **Player Evidence (`FC27PlayerEvidence`)**: Keyed and ordered strictly by pseudonymous `player_ref` (`plr_ecf_<12-hex>` or `player_<id>`). Computes `appearances`, `starts`, `minutes_played`, `goals`, `assists`, `goal_contributions`, `pass_completion_rate`, `tackle_success_rate`, `interceptions`, `shots`, `shots_on_target`, `shot_accuracy_rate`, `conversion_rate`, `saves`, `clean_sheet_appearances`, `average_rating`, `recent_form_change`, and position/role/build distributions. **Produces no overall player-quality score and never ranks human players from best to worst.**
3. **Formation Evidence (`FC27FormationEvidence`)**: Per-formation appearances, W/D/L, `win_rate`, goals for/against, goal differential, clean sheets, and recent-vs-baseline deltas without declaring any formation optimal.
4. **Role Evidence (`FC27RoleEvidence`)**: Grouped by `(role, focus, position)` with distinct match counts, team outcomes during observations, and player contribution rates without claiming the role caused the outcome.
5. **Build Evidence (`FC27BuildEvidence`)**: Grouped by `(build_reference, position, role)` with outcome and contribution aggregates, rejecting any private/URL/email build identifiers.
6. **Partnership Evidence (`FC27PartnershipEvidence`)**: Co-appearance statistics for pairs `(player_ref_a < player_ref_b)` with overlapping minutes (`minutes_played > 0`), shared outcomes, goal differential, clean sheets, and combined goal contributions phrased strictly as observed on-field co-appearance.
7. **Opponent Evidence (`FC27OpponentEvidence`)**: Normalized opponent outcome summaries and match-type splits.
8. **Observational Hypotheses (`FC27Hypothesis`)**: Up to `20` deterministic, template-generated hypotheses across `FORMATION_ASSOCIATION`, `ROLE_ASSOCIATION`, `BUILD_ASSOCIATION`, `PARTNERSHIP_ASSOCIATION`, and `RECENT_FORM_CHANGE`, stamped with `status="OBSERVATIONAL_ONLY"`, `source_classification="EAST_COASTERS_NORMALIZED_EVIDENCE"`, `limitations`, and `confounders`, ordered by `(confidence_tier DESC, eligible_match_count DESC, abs(absolute_delta) DESC, category ASC, hypothesis_id ASC)`.

---

## 6. Analyst Evidence Synthesis (`build_fc27_analyst_report`)

`build_fc27_analyst_report(analyst_input)` in `src/fc27_analyst.py` consumes validated `FC27SearchContext` and `FC27ScoutReport` outputs and synthesizes a deterministic, immutable `FC27AnalystReport` (`report_hash` SHA-256) with strict bounds:

1. **Cross-Layer Validation & Contradiction Detection (`validate_analyst_input`, `detect_evidence_contradictions`)**:
   - Verifies East Coasters identity (`club_id="330323"`, `club_name="East Coasters"`, `platform="common-gen5"`, `game_version="FC27"`), `FORMULA_VERSION="fc27-v1.0"`, `search_context_hash`, `scout_report_hash`, analysis window alignment, Search/Scout team aggregate parity, confidence-tier non-inflation, recent-vs-baseline delta sign consistency, hypothesis directional consistency, subgroup total bounds (`FORMATION_TOTAL_OVERFLOW`, `ROLE_TOTAL_OVERFLOW`, `PARTNERSHIP_OVERFLOW`), and evidence reference resolution.
   - Fails closed (`FC27AnalystContradictionError`) on any `BLOCKING` + `UNRESOLVED` contradiction, while retaining non-blocking `WARNING` contradictions (e.g., `RECENT_VS_BASELINE_METRIC_DIVERGENCE`) in `report.contradictions`.
2. **Deterministic Evidence Clustering (`build_evidence_clusters`, `MAX_EVIDENCE_CLUSTERS = 20`)**:
   - Groups observations across `12` approved categories (`TEAM_OUTCOME_PATTERN`, `RECENT_FORM_PATTERN`, `FORMATION_ASSOCIATION`, `ROLE_ASSOCIATION`, `BUILD_ASSOCIATION`, `PARTNERSHIP_ASSOCIATION`, `MATCH_TYPE_PATTERN`, `OPPONENT_CONTEXT_PATTERN`, `PLAYER_CONTRIBUTION_PATTERN`, `DATA_QUALITY_PATTERN`, `CONTRADICTORY_EVIDENCE`, `INSUFFICIENT_EVIDENCE`).
   - Ordered deterministically by `(-confidence_tier_rank, -eligible_match_count, category_priority, subject_reference, cluster_id)`.
3. **Explicit Counter-Evidence Analysis (`analyze_counter_evidence`, `MAX_COUNTER_EVIDENCE_ITEMS = 10` per cluster)**:
   - Evaluates opposite outcome directions, lower recent-form metrics, formation/role/partnership inconsistency, match-type concentration/dependence, repeated opponent encounters, small sample size (`< 3` `DISQUALIFYING`, `3..4` `MODERATE`, `5..9` `MINOR`), missing optional telemetry, and exclusion concentration.
   - Any `DISQUALIFYING` counter-evidence item strictly prevents `SUPPORTED_OBSERVATION` status.
4. **Confounders & Competing Explanations (`identify_confounders`, `generate_competing_explanations`)**:
   - Classifies `12` registry confounders (`opponent_strength_unavailable`, `opponent_formation_unavailable`, `match_type`, `club_side_context_unavailable`, `teammate_availability`, `human_controlled_player_count`, `missing_formation_metadata`, `missing_role_metadata`, `missing_build_metadata`, `unequal_group_sample_size`, `repeated_opponent_concentration`, `short_recent_form_window`, `patch_version_timing_unavailable`) as `MEASURED`, `PARTIALLY_MEASURED`, `UNMEASURED`, or `NOT_APPLICABLE` (`MAX_CONFOUNDERS = 10` per finding).
   - Generates template-based `FC27CompetingExplanation` records (`MAX_COMPETING_EXPLANATIONS = 10` per finding) with zero free-form LLM calls.
5. **Data Gaps & Unresolved Questions (`identify_data_gaps`, `generate_unresolved_questions`)**:
   - Produces up to `20` `FC27DataGap` records and `20` `FC27UnresolvedQuestion` records derived strictly from sample-size, metadata, telemetry, partnership overlap, match-type, opponent-context, and exclusion gaps.
6. **Bounded Observational Findings (`FC27AnalystFinding`, `MAX_ANALYST_FINDINGS = 20`)**:
   - Classifies each finding as `SUPPORTED_OBSERVATION`, `MIXED_EVIDENCE`, `COUNTER_EVIDENCE_DOMINANT`, `INSUFFICIENT_EVIDENCE`, or `UNRESOLVED`.
   - Enforces `observation_classification="OBSERVATIONAL_ONLY"`, `approvable=False`, `dispatchable=False`, `recommendation=False`, `causal_claim=False`, non-empty `limitations`, `confounders`, `competing_explanations`, `falsification_criteria`, and `additional_data_needed`, and validates statement text via `validate_analyst_evidence_language` (rejecting causal, optimal, prescriptive, player-ranking, or psychological/effort claims).

---

## 7. Read-Only Reader Boundary & LangGraph Node Adapters

- **`FC27AnalyticsReader` (`src/fc27_search_scout.py`)**: Read-only protocol implemented by `InMemoryFC27AnalyticsReader` (for unit tests) and `PostgresFC27AnalyticsReader` (reusing `src/fc27_dashboard_queries.py` with `SET LOCAL transaction_read_only = on`, `5000ms` statement timeout, `include_aliases=False`, and zero `PostgresSaver` table access).
- **`search_node` & `scout_node` (`src/fc27_search_scout.py`)**: LangGraph-compatible node adapters that validate East Coasters scope, do not mutate incoming graph state, perform zero direct SQL, zero network/DNS calls, zero LLM calls, and zero database/event/outbox writes, returning bounded `FC27SearchScoutStateUpdate` dictionaries (`search_context`, `scout_report`, `analysis_report_hash`, `analysis_confidence`, `analysis_limitations`).
- **`analyst_evidence_node` (`src/fc27_analyst.py`)**: LangGraph-compatible node adapter consuming `search_context` and `scout_report` from graph state (without modifying active `build_graph` topology in `src/graph.py`), enforcing `FC27_ANALYTICS_ENABLED` when `enforce_feature_flag=True`, performing zero LLM/Swarms/provider/DB/PostgresSaver/Redis/outbox/approval/dispatch calls, and returning bounded `FC27AnalystStateUpdate` dictionaries (`analyst_report`, `analyst_report_hash`, `analyst_confidence`, `analyst_findings`, `analyst_data_gaps`, `analyst_unresolved_questions`, `analyst_limitations`).
- **Privacy & Observability**: Display aliases, gamer tags, external player IDs, EA account/Persona/Nucleus IDs, and raw payloads are rejected (`FC27PrivacyViolationError`). Structured logs (`build_safe_observability_metadata`, `build_analyst_observability_metadata`) emit safe counts, confidence tiers, and hashes only.
