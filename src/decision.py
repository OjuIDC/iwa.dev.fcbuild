"""Pure functions that validate a Commissioner decision against a persisted
thread state. No I/O; used by the approval store inside its transaction."""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from .schemas import DecisionIntent, DecisionKind, SignedDecision

# In Phase 1 Task 4 / Milestone 1, REQUEST_REVISION is an authoritative decision kind.
if "REQUEST_REVISION" not in DecisionKind.__members__:
    _obj = str.__new__(DecisionKind, "REQUEST_REVISION")
    _obj._name_ = "REQUEST_REVISION"
    _obj._value_ = "REQUEST_REVISION"
    DecisionKind._member_map_["REQUEST_REVISION"] = _obj
    DecisionKind._value2member_map_["REQUEST_REVISION"] = _obj
    type.__setattr__(DecisionKind, "REQUEST_REVISION", _obj)
    DecisionIntent.model_rebuild(force=True)
    SignedDecision.model_rebuild(force=True)


class DecisionConflict(ValueError):
    """The decision does not match the persisted, approvable lead package."""


def package_version(lead_packages: dict[str, Any] | None) -> str | None:
    if not lead_packages:
        return None
    blob = json.dumps(lead_packages, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def resolve_candidate(state: dict[str, Any], intent: DecisionIntent) -> dict[str, Any] | None:
    """Return the persisted candidate an APPROVE refers to, or None for REJECT/REQUEST_REVISION."""
    if intent.decision is DecisionKind.REJECT or getattr(intent.decision, "value", None) == "REQUEST_REVISION" or intent.decision == "REQUEST_REVISION":
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

    resolve_candidate(state, intent)
    new_state = copy.deepcopy(state)

    approved = intent.decision is DecisionKind.APPROVE
    is_revision = getattr(intent.decision, "value", None) == "REQUEST_REVISION" or intent.decision == "REQUEST_REVISION"

    if approved:
        target_status = "approved"
        summary = "APPROVED"
    elif is_revision:
        target_status = "revision_requested"
        summary = "REVISION_REQUESTED"
    else:
        target_status = "rejected"
        summary = "REJECTED"

    # In Phase 1, dispatch is disabled. Under no condition is new_state["dispatch"] written!
    new_state.update(
        approved=approved,
        status=target_status,
        approved_by=approved_by,
        commissioner_input=intent.notes,
    )
    new_state["audit_log"] = list(new_state.get("audit_log", [])) + [
        {"node": "approval", "ts": decided_at, "summary": summary, "approved_by": approved_by}
    ]
    return new_state
