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
    target_delta: float | int,
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

