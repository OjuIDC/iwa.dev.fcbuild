"""Pure functions that validate a Commissioner decision against a persisted
thread state. No I/O; used by the approval store inside its transaction."""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from .schemas import DecisionIntent, DecisionKind


class DecisionConflict(ValueError):
    """The decision does not match the persisted, approvable lead package."""


def package_version(lead_packages: dict[str, Any] | None) -> str | None:
    if not lead_packages:
        return None
    blob = json.dumps(lead_packages, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def resolve_candidate(state: dict[str, Any], intent: DecisionIntent) -> dict[str, Any] | None:
    """Return the persisted candidate an APPROVE refers to, or None for REJECT."""
    if intent.decision is DecisionKind.REJECT:
        return None
    packages = state.get("lead_packages") or {}
    package = packages.get(intent.owner.value)
    if not isinstance(package, dict):
        raise DecisionConflict(f"no lead package for owner {intent.owner.value}")
    if package.get("approvable") is False:
        raise DecisionConflict("lead package has no approvable candidates")
    candidate = package.get(intent.selection.value)
    if not isinstance(candidate, dict):
        raise DecisionConflict(f"selection {intent.selection.value} is not available in this package")
    if candidate.get("candidate_id") != intent.candidate_id or candidate.get("player_id") != intent.player_id:
        raise DecisionConflict("decision does not match the persisted lead package")
    return candidate


def apply_decision_to_state(state: dict[str, Any], intent: DecisionIntent, approved_by: str, decided_at: str) -> dict[str, Any]:
    if state.get("status") != "awaiting_approval":
        raise DecisionConflict("thread is not awaiting approval")
    candidate = resolve_candidate(state, intent)
    new_state = copy.deepcopy(state)
    approved = intent.decision is DecisionKind.APPROVE
    if approved:
        new_state["dispatch"] = {
            "owner": intent.owner.value,
            "selection": intent.selection.value,
            "candidate": candidate,
            "dispatched_to_player_agent": intent.player_id,
        }
    new_state.update(
        approved=approved,
        status="approved" if approved else "rejected",
        approved_by=approved_by,
        commissioner_input=intent.notes,
    )
    new_state["audit_log"] = list(new_state.get("audit_log", [])) + [
        {"node": "approval", "ts": decided_at, "summary": "APPROVED" if approved else "REJECTED", "approved_by": approved_by}
    ]
    return new_state
