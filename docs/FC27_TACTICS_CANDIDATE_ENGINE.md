# FC27 Tactics Candidate Engine (`src/fc27_tactics.py`)

## Overview

The deterministic East Coasters FC27 Tactics Candidate Engine (`src/fc27_tactics.py`) translates active `FC27CoachHypothesis` artifacts (`MODERATE_CONFIDENCE` or `HIGH_CONFIDENCE`) into bounded, reversible experimental tactical candidates (`FC27TacticsReport`, `TACTICS_VERSION = "1.0.0"`).

## Candidate Taxonomy

Allowed `candidate_type` values:
- `FORMATION_COMPARISON_TEST`
- `BUILD_ARCHETYPE_COMPARISON_TEST`
- `TRANSITION_REST_DEFENSE_TEST`
- `PRESSING_TRIGGER_OBSERVATION_TEST`
- `PASSING_PROGRESSION_TEMPO_TEST`
- `SET_PIECE_OBSERVATION_TEST`
- `DATA_COLLECTION_HOLD`

## Governance & Safety Invariants

- **Experimental only**: Every candidate and report carries `recommendation=False`, `approvable=False`, `dispatchable=False`, `causal_claim=False`, and `recommendation_status="EXPERIMENTAL_CANDIDATE_NOT_A_RECOMMENDATION"`.
- **Upstream hash verification**: Verifies `search_context_hash`, `scout_report_hash`, `analyst_report_hash`, and `coach_report_hash`.
- **Single-variable discipline**: `single_variable_change=True` and `len(changed_variables) == 1` for every active candidate (`DATA_COLLECTION_HOLD` has `changed_variables=()`).
- **Mandatory risk & rollback metadata**: Every candidate defines `preconditions`, `target_metrics`, `stop_loss_triggers`, `rollback_condition`, `risk_notes`, `confounders`, and `data_gaps`.
- **Privacy & roster protection**: Rejects gamer tags, raw player names, and lineup/selection directives (`bench`, `drop`, `replace player`).
