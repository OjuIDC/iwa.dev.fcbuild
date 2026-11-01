# FC27 Director Evidence Reconciliation (`src/fc27_director.py`)

## Overview

The deterministic East Coasters FC27 Director Evidence Reconciliation Engine (`src/fc27_director.py`) is the terminal synthesis layer in the read-only advisory pipeline:

```text
Search -> Scout -> Analyst -> Coach -> Tactics -> Director
```

It consumes `FC27SearchContext`, `FC27ScoutReport`, `FC27AnalystReport`, `FC27CoachReport`, and `FC27TacticsReport` and emits an immutable `FC27DirectorReport` (`DIRECTOR_VERSION = "1.0.0"`, `FORMULA_VERSION = "fc27-analytics-1.0.0"`).

## Reconciliation Rules

- **Non-overlapping experiment sequence**: At most one active experiment per tactical category (`category`) is placed in `experiment_sequence` (`FC27DirectorExperimentSequenceItem`), ranked by confidence tier, sample count, and candidate ID.
- **Sample budget cap**: Active sequenced experiments are bounded by `MAX_SEQUENCED_EXPERIMENTS = 3` and sample budget (`sample_budget_matches`).
- **Deferred candidates**: Overlapping or lower-priority tactical candidates are placed in `deferred_candidates` (`FC27DirectorDeferredCandidate`) with an explicit `deferral_reason` and `prerequisite_condition`.
- **Rejected candidates**: `DATA_COLLECTION_HOLD` candidates and blocked findings are placed in `rejected_candidates` (`FC27DirectorRejectedCandidate`) with an explicit `rejection_reason`.
- **Measurement priorities**: Data gaps are ranked in `measurement_priorities` (`FC27DirectorMeasurementPriority`) with `blocking=True` gaps prioritized first.

## Governance & Safety Invariants

- **Human review required**: `review_status = "RECONCILIATION_ONLY_HUMAN_REVIEW_REQUIRED"` and `recommendation_status = "NOT_A_RECOMMENDATION"`.
- **No autonomous execution**: `recommendation=False`, `approvable=False`, `dispatchable=False`, `causal_claim=False`.
- **Full evidence lineage**: `FC27DirectorEvidenceLineage` records and verifies `search_context_hash`, `scout_report_hash`, `analyst_report_hash`, `coach_report_hash`, and `tactics_report_hash`.
- **Database isolation**: Director performs zero writes to `public.checkpoints*`, `public.recommendation_approvals`, or `public.dispatch_outbox`.
