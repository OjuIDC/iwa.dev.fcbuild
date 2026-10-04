# Task Assignment: explorer_m3_it2_schemas_1 (M3 Iteration 2 Schemas Explorer)

## Mission
Investigate and design the exact fix in `src/schemas.py` and `src/dispatch/objective.py` for Challenger 2's finding regarding candidate input echo leak in `ValidationError`.

## Working Directory
`c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_it2_schemas_1`

## Mandatory Reading
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (authoritative user request + Section 1A patch)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`
3. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_2\challenge_report.md` (Gate failure feedback)
4. `src/schemas.py` (`Objective` model)
5. `src/dispatch/objective.py`
6. `tests/test_dispatch_schema.py::test_objective_bounds_model_and_policy`

## Investigation Tasks
1. Inspect `Objective` class in `src/schemas.py` and its Pydantic configuration.
2. Investigate why candidate input (e.g. `26.123`) is included in `ValidationError` representation.
3. Verify how `model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)` works in Pydantic v2.
4. Verify that `test_objective_bounds_model_and_policy` passes when `hide_input_in_errors=True` is enabled.
5. Check if any other schemas in `src/schemas.py` need `hide_input_in_errors=True`.
6. Provide exact code modifications for `src/schemas.py` and `src/dispatch/objective.py`.

## Output Requirements
Write `schemas_fix_analysis.md` and `handoff.md` in your working directory.
Notify the orchestrator with `send_message` when done.
Do NOT modify production source code in `src/`, `migrations/`, or `tests/`.
