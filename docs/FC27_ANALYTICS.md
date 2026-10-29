# FC 27 Deterministic Analytics Specification

> **Formula Version**: `FORMULA_VERSION = "1.0.0"` ([`src/fc27_analytics.py`](../src/fc27_analytics.py))
> **Default Runtime Gate**: `FC27_ANALYTICS_ENABLED=false` ([`src/settings.py`](../src/settings.py))

---

## 1. Design Principles & Pure Functional Core

The FC 27 analytics engine in `src/fc27_analytics.py` is a **100% deterministic, side-effect-free pure computation layer**:

- **No LLM / Generative Inference**: Analytics metrics are computed exclusively using exact integer and `Decimal` arithmetic (`ROUND_HALF_UP`).
- **No Network or Database Side Effects in `src/fc27_analytics.py`**: Pure functions accept immutable sequences of `FC27NormalizedMatchRecord` and `FC27NormalizedPlayerMatchStatRecord` and return frozen Pydantic summary models (`ClubAnalyticsSummary`, `FormationBreakdownItem`, `PlayerAnalyticsSummary`, `RecentFormSummary`, `RollingAnalyticsSnapshot`).
- **Formula Version Pinning**: Every output model and coaching package binds `formula_version="1.0.0"`. Modifying any formula requires an explicit version bump and regression suite update.

---

## 2. Match Eligibility & Exclusions

`compute_rolling_snapshot` and all summary functions enforce strict data hygiene before calculating metrics:

1. **Eligible Matches**: Only matches with `match_status == MatchStatus.COMPLETED` (`"COMPLETED"`), matching `club_id`, matching `platform`, and `data_quality_status == DataQualityStatus.VALID` are included in win/draw/loss, goal, and player rate calculations.
2. **Excluded Matches**:
   - `MatchStatus.ABANDONED` (`"ABANDONED"`): Counted in `abandoned_match_count` and `excluded_match_count`, never in `eligible_match_count`.
   - `MatchStatus.PARTIAL` (`"PARTIAL"`): Counted in `partial_match_count` and `excluded_match_count`, never in `eligible_match_count`.
   - Quarantined (`DataQualityStatus.QUARANTINED`) or replay-conflicting (`DataQualityStatus.REPLAY_CONFLICT`) records: Excluded prior to aggregation and surfaced in `excluded_match_count` / `quarantined_record_count`.

---

## 3. Sample-Size Confidence Tiers (`classify_sample_size_tier`)

To prevent small-sample over-interpretation, `classify_sample_size_tier(eligible_match_count)` in `src/fc27_analytics.py` classifies every analytical snapshot into one of four tiers (`SampleSizeTier` in `src/fc27_Complete_models.py`):

| Tier Enum | Eligible Completed Matches | Analytical & Coaching Interpretation |
|---|---|---|
| `INSUFFICIENT_DATA` | `0 .. 2` | Below `MIN_EXPLORATORY_MATCHES` (`3`). Trend claims and coaching packages are blocked by `src/fc27_coaching.py`. |
| `EXPLORATORY` | `3 .. 9` | Exploratory descriptive statistics only. Below `MIN_PROVISIONAL_TRENDS_MATCHES` (`10`); not eligible for tactical trend recommendations. |
| `PROVISIONAL_TRENDS` | `10 .. 19` | Provisional trend visibility (`10..19` matches). Eligible for bounded coaching review if all pre-approval gates pass. |
| `HIGH_SAMPLE_READINESS` | `20+` | Meets `MIN_HIGH_SAMPLE_READINESS_MATCHES` (`20`). Full baseline comparison context available. |

---

## 4. Context Windows & Rolling Horizons

Defined as module constants in `src/fc27_analytics.py`:

- **Recent Form Window (`RECENT_FORM_WINDOW = 5`)**: `compute_recent_form` evaluates the 5 most recent eligible `COMPLETED` matches ordered by `(played_at_utc DESC, match_id DESC)`, producing a deterministic `form_string` (e.g., `"WWDLW"`), `points_earned`, `goals_for`, and `goals_against`.
- **Default Baseline Context (`DEFAULT_RECENT_CONTEXT_MATCH_LIMIT = 20`)**: `compute_rolling_snapshot` defaults to evaluating the 20 most recent eligible `COMPLETED` matches (`match_limit=20`).
- **Maximum Optional Rolling Window (`MAX_ROLLING_WINDOW_DAYS = 730`)**: When an optional date horizon (`window_days`) is requested against `reference_time_utc`, it must satisfy `1 <= window_days <= 730` (up to 2 years). Any `window_days <= 0` or `> 730` raises `ValueError`.

---

## 5. Pure Analytical Functions (`src/fc27_analytics.py`)

1. **`compute_club_summary`**: Computes `eligible_match_count`, `excluded_match_count`, `abandoned_match_count`, `partial_match_count`, `wins`, `draws`, `losses`, `win_rate_pct`, `points_per_match` (`3` for win, `1` for draw, `0` for loss), `goals_for`, `goals_against`, `goal_difference`, `goals_for_per_match`, `goals_against_per_match`, `clean_sheets`, `clean_sheet_rate_pct`, and `sample_size_tier`.
2. **`compute_formation_breakdown`**: Groups eligible matches by normalized formation (`normalize_formation`, e.g., `"4-2-1-3"`, `"4-1-2-1-2 Narrow"`, `"UNSPECIFIED"`), computing per-formation match count, W/D/L, `win_rate_pct`, `goals_for_per_match`, `goals_against_per_match`, and per-formation `sample_size_tier` (minimum `MIN_FORMATION_SAMPLE_MATCHES = 5` required for formation-specific coaching claims).
3. **`compute_player_summaries`**: Aggregates per-player appearances (`appearances > 0`), `total_minutes_played`, `goals`, `assists`, `goal_contributions`, `goals_per_90`, `assists_per_90`, `pass_completion_pct`, `tackle_success_pct`, `average_rating`, `man_of_the_match_count`, `primary_position_group`, and per-player `sample_size_tier`.
4. **`compute_recent_form`**: Computes the last-5 match form breakdown.
5. **`compute_rolling_snapshot`**: Combines club summary, recent form, formation breakdown, and player summaries into an immutable `RollingAnalyticsSnapshot` stamped with `formula_version="1.0.0"`.

---

## 6. Alias Privacy & Read-Only PostgreSQL Dashboard Queries

- **Alias Suppression by Default**: Both `compute_player_summaries(..., include_safe_aliases=False)` and `FC27DashboardQueryService` (`src/fc27_dashboard_queries.py`) default to `include_safe_aliases=False`, emitting only deterministic `player_pseudonym` (`plr_ecf_<12-hex>`) values and setting `safe_alias=None`.
- **PostgreSQL `READ ONLY` Enforcement (`src/fc27_dashboard_queries.py`)**:
  - Every dashboard query executes inside `SET TRANSACTION READ ONLY` with `SET LOCAL statement_timeout = '5000ms'` (`READ_ONLY_STATEMENT_TIMEOUT_MS = 5000`).
  - Queries read exclusively from normalized PostgreSQL tables (`fc27_clubs`, `fc27_matches`, `fc27_player_match_stats`, `fc27_quarantine_records`) and never trigger external network calls.

---

## 7. Correlation vs. Causation & Tactical Hypothesis Policy

- Descriptive formation and player metrics reflect **observed historical correlations**, which are confounded by opponent strength, squad availability, role familiarity, and match state.
- External community meta claims (such as `4-2-1-3` or `4-1-2-1-2 Narrow` superiority, dual-CDM spacing, or attribute thresholds) are classified strictly as `EXTERNAL_TACTICAL_HYPOTHESIS` (`NOT_APPROVABLE`) in [`docs/FC27_TACTICAL_HYPOTHESES.md`](FC27_TACTICAL_HYPOTHESES.md) and are never hard-coded as analytical bonuses in `src/fc27_analytics.py`.
