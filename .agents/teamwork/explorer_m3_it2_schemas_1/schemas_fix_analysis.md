# Schema & Objective Bounds Fix Analysis: Candidate Input Echo Leak

**Agent**: explorer_m3_it2_schemas_1  
**Target Files**: `src/schemas.py`, `src/dispatch/objective.py`  
**Related Test**: `tests/test_dispatch_schema.py::test_objective_bounds_model_and_policy`  
**Milestone**: M3 Iteration 2 (Challenger 2 Gate Remediation)  
**Date**: 2026-10-03  

---

## 1. Executive Summary

During adversarial challenge evaluation of Milestone 3, Challenger 2 identified a security defect and test failure in `Objective` bounds validation:
- **Failure**: `tests/test_dispatch_schema.py::test_objective_bounds_model_and_policy` fails on `assert "26.123" not in err_str`.
- **Root Cause**: In Pydantic v2, `ValidationError` string representations (`str(exc)`) default to formatting `[type=..., input_value=..., input_type=...]`. Because `Objective` in `src/schemas.py` configured `model_config = ConfigDict(extra="forbid")` without `hide_input_in_errors=True`, Pydantic echoed the unvalidated candidate input (`input_value={'metric': 'pass_accuracy', 'direction': 'increase', 'target_delta': 26.123, ...}`) in the error output.
- **Security Impact**: Out-of-bounds candidate metrics and adversarial candidate payloads (e.g., prompt injections, internal instructions, or reflected inputs) are echoed in application logs and API error responses, violating the zero-echo boundary requirement.
- **Solution**:
  1. Update `Objective` in `src/schemas.py` to use `model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)`.
  2. Update `Candidate` in `src/schemas.py` to also use `model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)`, protecting candidate-level validation from echoing arbitrary agent text (`rationale`, `action`, `evidence_refs`) or nested objectives.
  3. Add standalone `validate_metric_bounds` helper in `src/dispatch/objective.py` fulfilling Spec §12.1 and `PROJECT.md:149`, which sanitizes error reporting without circular dependencies.

---

## 2. Detailed Root Cause Analysis

### 2.1 Pydantic v2 `ValidationError` Formatting Mechanics
In Pydantic v2, error formatting is governed by `pydantic_core`:
1. When a `@model_validator(mode="after")` or `@field_validator` raises a `ValueError`, `pydantic_core` wraps the failure into a validation error dict:
   ```python
   {
       "type": "value_error",
       "loc": (),
       "msg": "Value error, target_delta for pass_accuracy must be between 1 and 25 (policy fc27-objective-bounds-v1)",
       "input": {
           "metric": "pass_accuracy",
           "direction": "increase",
           "target_delta": 26.123,
           "min_matches": 1,
           "max_matches": 5,
           "bounds_policy": "fc27-objective-bounds-v1",
       },
       "ctx": {"error": ValueError(...)},
   }
   ```
2. When converting the `ValidationError` to a string (`str(exc)`), Pydantic formats:
   ```
   1 validation error for Objective
     Value error, target_delta for pass_accuracy must be between 1 and 25 (policy fc27-objective-bounds-v1) [type=value_error, input_value={'metric': 'pass_accuracy', 'direction': 'increase', 'target_delta': 26.123, 'min_matches': 1, 'max_matches': 5, 'bounds_policy': 'fc27-objective-bounds-v1'}, input_type=dict]
   ```
3. Because `'target_delta': 26.123` is embedded inside `input_value`, the substring `"26.123"` appears in `str(exc)`.
4. In `tests/test_dispatch_schema.py`:
   ```python
   # Out of bounds pass_accuracy (> 25)
   with pytest.raises(ValidationError) as exc_info:
       Objective(
           metric="pass_accuracy",
           direction="increase",
           target_delta=26.123,
       )
   err_str = str(exc_info.value)
   assert "target_delta for pass_accuracy must be between 1 and 25 (policy fc27-objective-bounds-v1)" in err_str
   # Input must NOT be echoed in error message
   assert "26.123" not in err_str  # <--- FAILS HERE
   ```

### 2.2 Pydantic v2 `hide_input_in_errors=True` Configuration
Setting `hide_input_in_errors=True` on `ConfigDict`:
- Configures `pydantic_core.CoreConfig(hide_input_in_errors=True)`.
- Completely strips the `'input'` field from error dictionaries produced by `exc.errors()`.
- Suppresses the `, input_value=...` and `, input_type=...` segments when `str(exc)` formats the error.
- The resulting error string becomes:
  ```
  1 validation error for Objective
    Value error, target_delta for pass_accuracy must be between 1 and 25 (policy fc27-objective-bounds-v1) [type=value_error]
  ```
- **Result**:
  - `assert "target_delta for pass_accuracy must be between 1 and 25 (policy fc27-objective-bounds-v1)" in err_str` evaluates to `True`.
  - `assert "26.123" not in err_str` evaluates to `True`.
  - The test passes completely.

---

## 3. Scope Analysis: Candidate & Other Schemas

### 3.1 Candidate Schema (`Candidate`)
In `src/schemas.py:46`:
```python
class Candidate(BaseModel): candidate_id:str; player_id:str; owner:SwarmRole; tier:Tier; score:float=Field(ge=0,le=100); rationale:str=Field(min_length=1,max_length=4000); action:str=Field(min_length=1,max_length=4000); evidence_refs:list[str]=Field(min_length=1); objective:Objective|None=None
```

**Risk Assessment**:
1. `Candidate` holds arbitrary agent output strings (`rationale`, `action`, `evidence_refs`) and the nested `objective` model.
2. If an invalid candidate is validated via `Candidate.model_validate(payload)` or `Candidate(...)`, without `hide_input_in_errors=True`, Pydantic echoes the entire candidate dictionary in the root `ValidationError`.
3. If the nested `objective` contains an out-of-bounds metric (e.g. `26.123`), the parent `Candidate` validation error would echo `26.123` via `input_value={'candidate_id': ..., 'objective': {'target_delta': 26.123}}`.
4. `Candidate` currently has no `model_config`, allowing extra fields by default (`extra="ignore"`).

**Recommendation**:
Refactor `Candidate` into a multi-line class definition with:
```python
class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    candidate_id: str
    player_id: str
    owner: SwarmRole
    tier: Tier
    score: float = Field(ge=0, le=100)
    rationale: str = Field(min_length=1, max_length=4000)
    action: str = Field(min_length=1, max_length=4000)
    evidence_refs: list[str] = Field(min_length=1)
    objective: Objective | None = None
```

### 3.2 Containers & Other Schemas
- `PlayerObservation`: Contains `metrics: dict` and `evidence: list[str]`. Adding `model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)` prevents observation payload echoing.
- `TierSet` & `LeadPackage`: Contain lists or fields of `Candidate`. Adding `model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)` prevents candidate dumping if packaging fails.
- `DecisionIntent`: Already configured with `extra="forbid"`. Its fields are commissioner intent, not candidate/agent inputs. Adding `hide_input_in_errors=True` is safe but optional.

### 3.3 Circular Dependency Prevention
- `src/schemas.py` imports `METRIC_BOUNDS` from `src.dispatch.objective`.
- `src/dispatch/objective.py` must **NOT** import `Objective` from `src.schemas` (which would trigger `ImportError: cannot import name 'Objective' from partially initialized module 'src.schemas'`).
- The clean architectural separation is:
  - `src/dispatch/objective.py`: Defines policy constants (`METRIC_BOUNDS`, `PHYSICAL_RANGE`) and a pure-Python validation helper `validate_metric_bounds(metric, target_delta, policy)`.
  - `src/schemas.py`: Imports `METRIC_BOUNDS` and `validate_metric_bounds` from `src.dispatch.objective`, and defines the Pydantic models `Objective` and `Candidate` with `hide_input_in_errors=True`.

---

## 4. Exact Code Replacements

### 4.1 Changes to `src/dispatch/objective.py`

#### Target File: `src/dispatch/objective.py`
Add `validate_metric_bounds()` helper and export it:

```python
"""Objective bounds policy and validation for recommendation dispatches."""
from __future__ import annotations

from typing import Final

# Bounded metric target deltas per versioned policy fc27-objective-bounds-v1 (Spec §12.1, D-30)
METRIC_BOUNDS: Final[dict[str, dict[str, tuple[float, float]]]] = {
    "fc27-objective-bounds-v1": {
        "pass_accuracy": (1.0, 25.0),
        "tackle_success": (1.0, 25.0),
        "shot_accuracy": (1.0, 25.0),
        "rating": (0.1, 2.0),
        "goals": (1.0, 5.0),
        "assists": (1.0, 5.0),
    }
}

# Physical metric ranges (Spec §19.1) - separate object from METRIC_BOUNDS
PHYSICAL_RANGE: Final[dict[str, tuple[float, float]]] = {
    "sprint_speed": (1.0, 100.0),
    "acceleration": (1.0, 100.0),
    "stamina": (1.0, 100.0),
}


def validate_metric_bounds(
    metric: str,
    target_delta: float,
    policy: str = "fc27-objective-bounds-v1",
) -> None:
    """Validate that target_delta falls within bounds policy without leaking delta in error."""
    policy_bounds = METRIC_BOUNDS.get(policy)
    if policy_bounds is None or metric not in policy_bounds:
        raise ValueError(f"unknown bounds policy {policy}")

    low, high = policy_bounds[metric]
    if not (low <= target_delta <= high):
        low_str = int(low) if low.is_integer() else low
        high_str = int(high) if high.is_integer() else high
        raise ValueError(
            f"target_delta for {metric} must be between {low_str} and {high_str} (policy {policy})"
        )
```

### 4.2 Changes to `src/schemas.py`

#### Target File: `src/schemas.py`
Lines 5–46:
- Import `validate_metric_bounds` from `src.dispatch.objective`.
- Configure `Objective.model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)`.
- Use `validate_metric_bounds` in `_validate_bounds`.
- Configure `Candidate.model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)`.

```python
<<<<
from src.dispatch.objective import METRIC_BOUNDS

class Objective(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: Literal[
        "pass_accuracy",
        "rating",
        "goals",
        "assists",
        "tackle_success",
        "shot_accuracy",
    ]
    direction: Literal["increase", "decrease"]
    target_delta: float
    min_matches: int = 1
    max_matches: int = 5
    bounds_policy: Literal["fc27-objective-bounds-v1"] = "fc27-objective-bounds-v1"

    @model_validator(mode="after")
    def _validate_bounds(self):
        if not (1 <= self.min_matches <= self.max_matches <= 10):
            raise ValueError("matches constraint failed: requires 1 <= min_matches <= max_matches <= 10")

        policy_bounds = METRIC_BOUNDS.get(self.bounds_policy)
        if policy_bounds is None or self.metric not in policy_bounds:
            raise ValueError(f"unknown bounds policy {self.bounds_policy}")

        low, high = policy_bounds[self.metric]
        if not (low <= self.target_delta <= high):
            low_str = int(low) if low.is_integer() else low
            high_str = int(high) if high.is_integer() else high
            raise ValueError(
                f"target_delta for {self.metric} must be between {low_str} and {high_str} (policy {self.bounds_policy})"
            )
        return self

class SwarmRole(str,Enum): scout="scout"; analyst="analyst"; coach="coach"; tactics="tactics"; director="director"
class Tier(str,Enum): best="best"; middle="middle"; lowest="lowest"
class Selection(str,Enum): best="best"; best_runner_up="best_runner_up"; middle="middle"; middle_runner_up="middle_runner_up"; lowest="lowest"; ignore="ignore"
class PlayerObservation(BaseModel): player_id:str=Field(min_length=1,max_length=80); match_id:str; metrics:dict[str,float|int|str]; evidence:list[str]=Field(min_length=1); observer_agent:str
class Candidate(BaseModel): candidate_id:str; player_id:str; owner:SwarmRole; tier:Tier; score:float=Field(ge=0,le=100); rationale:str=Field(min_length=1,max_length=4000); action:str=Field(min_length=1,max_length=4000); evidence_refs:list[str]=Field(min_length=1); objective:Objective|None=None
====
from src.dispatch.objective import METRIC_BOUNDS, validate_metric_bounds

class Objective(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    metric: Literal[
        "pass_accuracy",
        "rating",
        "goals",
        "assists",
        "tackle_success",
        "shot_accuracy",
    ]
    direction: Literal["increase", "decrease"]
    target_delta: float
    min_matches: int = 1
    max_matches: int = 5
    bounds_policy: Literal["fc27-objective-bounds-v1"] = "fc27-objective-bounds-v1"

    @model_validator(mode="after")
    def _validate_bounds(self):
        if not (1 <= self.min_matches <= self.max_matches <= 10):
            raise ValueError("matches constraint failed: requires 1 <= min_matches <= max_matches <= 10")
        validate_metric_bounds(self.metric, self.target_delta, self.bounds_policy)
        return self

class SwarmRole(str,Enum): scout="scout"; analyst="analyst"; coach="coach"; tactics="tactics"; director="director"
class Tier(str,Enum): best="best"; middle="middle"; lowest="lowest"
class Selection(str,Enum): best="best"; best_runner_up="best_runner_up"; middle="middle"; middle_runner_up="middle_runner_up"; lowest="lowest"; ignore="ignore"
class PlayerObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    player_id: str = Field(min_length=1, max_length=80)
    match_id: str
    metrics: dict[str, float | int | str]
    evidence: list[str] = Field(min_length=1)
    observer_agent: str

class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    candidate_id: str
    player_id: str
    owner: SwarmRole
    tier: Tier
    score: float = Field(ge=0, le=100)
    rationale: str = Field(min_length=1, max_length=4000)
    action: str = Field(min_length=1, max_length=4000)
    evidence_refs: list[str] = Field(min_length=1)
    objective: Objective | None = None
>>>>
```

---

## 5. Verification Matrix

| Verification Case | Mechanism | Expected Outcome |
|---|---|---|
| `Objective(metric="pass_accuracy", target_delta=15.0)` | Valid instantiation | `obj.bounds_policy == "fc27-objective-bounds-v1"`, `obj.target_delta == 15.0` |
| `Objective(metric="pass_accuracy", target_delta=26.123)` | Out-of-bounds delta | `ValidationError` raised. `err_str` contains `"target_delta for pass_accuracy must be between 1 and 25 (policy fc27-objective-bounds-v1)"`. `"26.123"` NOT in `err_str`. |
| `Objective(metric="rating", min_matches=6, max_matches=5)` | Inverted match count | `ValidationError` raised with `"matches constraint failed: requires 1 <= min_matches <= max_matches <= 10"`. Inputs hidden. |
| `Objective(extra_field="rejected")` | Extra field rejection | `ValidationError` raised with `extra_forbidden`. Input string not echoed. |
| `Candidate(score=105)` | Score out of range | `ValidationError` raised with `le=100`. Score value not echoed. |
| `Candidate(objective=Objective(...))` with invalid delta | Nested candidate validation | Child and parent validation errors hide candidate input payloads. |
| `from src.dispatch.objective import METRIC_BOUNDS, PHYSICAL_RANGE` | Import stability | Policy table frozen, `PHYSICAL_RANGE is not METRIC_BOUNDS`. |
| Circular import check | Module loading | `src.schemas` imports `src.dispatch.objective`; `src.dispatch.objective` has zero imports from `src.schemas`. No circular import. |
