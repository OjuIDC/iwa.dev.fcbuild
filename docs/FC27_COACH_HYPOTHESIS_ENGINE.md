# FC27 Coach Hypothesis Engine (`src/fc27_coach.py`)

## Overview

The deterministic East Coasters FC27 Coach Hypothesis Engine (`src/fc27_coach.py`) is the observational hypothesis layer (`Search -> Scout -> Analyst -> Coach`). It consumes validated `FC27SearchContext`, `FC27ScoutReport`, and `FC27AnalystReport` objects and emits an immutable `FC27CoachReport` (`COACH_VERSION = "1.0.0"`, `FORMULA_VERSION = "fc27-analytics-1.0.0"`).

## Governance & Safety Invariants

- **Observational only**: Every artifact carries `recommendation=False`, `approvable=False`, `dispatchable=False`, `causal_claim=False`, and `recommendation_status="NOT_A_RECOMMENDATION"`.
- **Upstream hash verification**: Recomputes and verifies `search_context_hash`, `scout_report_hash`, and `analyst_report_hash` before generating any hypothesis.
- **Confidence ceiling**: Each `FC27CoachHypothesis` is capped at `min(search_confidence, scout_confidence, analyst_finding_confidence)` and never exceeds `MODERATE_CONFIDENCE` when sample size is 10 matches or confounders exist.
- **Blocked findings**: Findings with `DESCRIPTIVE_ONLY` or `INSUFFICIENT_DATA` confidence cannot produce active coaching hypotheses and are recorded in `blocked_finding_ids`.
- **Required fields per hypothesis**: `supporting_evidence`, `counter_evidence`, `confounders`, `data_gaps`, `falsification_condition`, `practice_experiment` (`FC27CoachPracticeExperiment`), `observation_checklist` (`FC27CoachObservationChecklist`), and `measurement_plan` (`FC27CoachMeasurementPlan`).
- **Evidence language enforcement**: Validated through `validate_analyst_evidence_language(...)`; imperative commands (`must`, `should`, `switch to`, `drop player`, `bench`) and causal claims are rejected with `FC27CoachLanguageError`.
- **Verified upstream model fast-path & advisory caching**: Already-validated frozen `FC27SearchContext`, `FC27ScoutReport`, and `FC27AnalystReport` instances with intact structural signatures reuse their verified SHA-256 report hashes while detecting any `object.__setattr__` tampering, and completed `FC27CoachReport` bundles are cached in [`src/fc27_advisory_cache.py`](../src/fc27_advisory_cache.py) (see [`docs/FC27_ADVISORY_CACHE.md`](FC27_ADVISORY_CACHE.md)).
