"""Lead-package construction with an explicit, testable ranking policy.

Policy ``fc27-rank-v1`` for each role R:
    final_score(player) = clamp(base_score(player) + adjustment_R(player), 0, 100)
where base_score is the deterministic evidence score below and adjustment_R is
the bounded (+/-10) adjustment from role R's schema-valid output. Players are
ranked by final_score (ties: base_score, then player id) and split into three
contiguous tiers. Each tier yields at most two selections:

    best tier   -> best, best_runner_up
    middle tier -> middle, middle_runner_up
    lowest tier -> lowest, ignore (the last player of the lowest tier)

Players are never duplicated. With fewer players, fewer selections exist and
``availability`` says so. A package is approvable only if its role output was
valid and at least one candidate exists. No placeholder player is ever created.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import math
import re
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, model_validator

from .decision import package_version as compute_package_version
from .dispatch.objective import METRIC_BOUNDS, validate_metric_bounds
from .fc27_analytics import FORMULA_VERSION, determine_sample_size_tier
from .roles import RoleResult
from .schemas import (
    FORMATION_REGISTRY_VERSION,
    PRE_APPROVAL_POLICY_VERSION,
    SAMPLE_SIZE_TIERS,
    VALID_FC27_FORMATIONS,
    VALID_FC27_POSITIONS,
    VALID_FC27_ROLES,
    VALID_PRE_APPROVAL_REASON_CODES,
    Candidate,
    EastCoastersRecommendation,
    LeadPackage,
    Lineup,
    Objective,
    PreApprovalReasonCode,
    PreApprovalValidationResult,
    SwarmRole,
    Tier,
)
from .settings import is_fc27_recommendations_enabled

POLICY_VERSION = "fc27-rank-v1"
SELECTIONS = ("best", "best_runner_up", "middle", "middle_runner_up", "lowest", "ignore")
_UNSET = object()

VALID_SAMPLE_SIZE_TIERS: frozenset[str] = frozenset(
    {
        "INSUFFICIENT_DATA",
        "EXPLORATORY",
        "LOW_CONFIDENCE",
        "MODERATE_CONFIDENCE",
        "HIGH_CONFIDENCE",
    }
)

_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_CANDIDATE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_HASH_CANDIDATE_ID_RE = re.compile(r"^[0-9a-f]{24}$")
_PSEUDONYMOUS_PLAYER_REF_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_EXTERNAL_ID_TOKEN_RE = re.compile(
    r"(?:^|[_:.-])(persona|nucleus|ea_account|account_id|user_id|gamertag|gamer_tag|email|token|cookie|session|secret|password)(?:$|[_:.-])",
    re.IGNORECASE,
)

_ALLOWED_PACKAGE_KEYS: frozenset[str] = frozenset(
    {
        "owner",
        "approvable",
        "reason",
        "policy_version",
        "availability",
        *SELECTIONS,
        # Optional pre-approval validation metadata keys
        "objective",
        "formation",
        "lineup",
        "assignments",
        "roster",
        "analytics_metadata",
        "analytics_backed",
        "confidence_tier",
        "formula_version",
        "sample_size",
        "eligible_matches",
        "minimum_sample_size",
        "recommendation",
        "recommendations",
        "package_type",
        "is_recommendation",
        "review_required",
    }
)



class RankedLeadPackage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner: SwarmRole
    approvable: bool
    reason: str | None = None
    policy_version: str = POLICY_VERSION
    availability: dict[str, int]
    best: Candidate | None = None
    best_runner_up: Candidate | None = None
    middle: Candidate | None = None
    middle_runner_up: Candidate | None = None
    lowest: Candidate | None = None
    ignore: Candidate | None = None

    @model_validator(mode="after")
    def _invariants(self):
        chosen = [getattr(self, s) for s in SELECTIONS if getattr(self, s) is not None]
        if len({c.candidate_id for c in chosen}) != len(chosen) or len({c.player_id for c in chosen}) != len(chosen):
            raise ValueError("duplicate candidate or player in lead package")
        if any(c.owner != self.owner for c in chosen):
            raise ValueError("cross-role candidate injection")
        if self.approvable and not chosen:
            raise ValueError("an empty package cannot be approvable")
        return self


def players_from_dashboard(dashboard: dict[str, Any], club_id: str) -> dict[str, dict]:
    matches = dashboard.get("matches") or []
    latest = matches[0] if matches and isinstance(matches[0], dict) else {}
    block = next((v for k, v in (latest.get("players") or {}).items() if str(k) == club_id), {}) or {}
    if not block:
        members = dashboard.get("members") or {}
        if isinstance(members, list):
            block = {str(m.get("name") or m.get("gamertag")): m for m in members
                     if isinstance(m, dict) and (m.get("name") or m.get("gamertag"))}
        elif isinstance(members, dict):
            block = members
    return {str(k)[:128]: v for k, v in block.items() if str(k).strip() and isinstance(v, dict)}


def base_score(stats: dict) -> float:
    def num(*keys):
        for k in keys:
            try:
                return float(stats.get(k) or 0)
            except (TypeError, ValueError):
                continue
        return 0.0
    attempts = max(num("passattempts", "passAttempts"), 1.0)
    made = num("passesmade", "passesMade")
    raw = num("rating") * 10 + num("goals") * 5 + num("assists") * 3 + (made / attempts) * 5
    return round(max(0.0, min(100.0, raw)), 2)


def _tiers(ranked: list) -> tuple[list, list, list]:
    size = math.ceil(len(ranked) / 3) if ranked else 0
    return ranked[:size], ranked[size:2 * size], ranked[2 * size:]


def _candidate_id(thread_id: str, role: str, selection: str, player: str) -> str:
    return hashlib.sha256(f"{POLICY_VERSION}:{thread_id}:{role}:{selection}:{player}".encode()).hexdigest()[:24]


def build_role_package(role: SwarmRole, players: dict[str, dict], result: RoleResult | None, thread_id: str) -> RankedLeadPackage:
    availability = {"players": len(players), "selections": 0}
    if result is None or not result.valid:
        reason = "role output invalid or missing" + (f": {result.error}" if result and result.error else "")
        return RankedLeadPackage(owner=role, approvable=False, reason=reason, availability=availability)
    if not players:
        return RankedLeadPackage(owner=role, approvable=False, reason="no eligible players", availability=availability)
    adj = result.adjustments(set(players))
    ranked = sorted(
        ((round(max(0.0, min(100.0, base_score(s) + adj.get(p, 0.0))), 2), base_score(s), p, s) for p, s in players.items()),
        key=lambda t: (-t[0], -t[1], t[2]),
    )
    best, middle, lowest = _tiers(ranked)
    picks: dict[str, tuple] = {}
    for tier_items, names in ((best, ("best", "best_runner_up")), (middle, ("middle", "middle_runner_up"))):
        for name, item in zip(names, tier_items[:2]):
            picks[name] = (item, Tier.best if name.startswith("best") else Tier.middle)
    if lowest:
        picks["lowest"] = (lowest[0], Tier.lowest)
        if len(lowest) > 1:
            picks["ignore"] = (lowest[-1], Tier.lowest)
    summary = (result.output.summary if result.output else "")[:300]
    fields: dict[str, Candidate] = {}
    for sel, ((final, base, player, stats), tier) in picks.items():
        fields[sel] = Candidate(
            candidate_id=_candidate_id(thread_id, role.value, sel, player), player_id=player, owner=role, tier=tier,
            score=final,
            rationale=f"{POLICY_VERSION}: base {base} + {role.value} adjustment {round(final - base, 2)}",
            action=f"{role.value} advisory for {player}. Role summary: {summary or 'n/a'}",
            evidence_refs=[f"rating={stats.get('rating', 0)}", f"goals={stats.get('goals', 0)}", f"assists={stats.get('assists', 0)}"],
        )
    availability["selections"] = len(fields)
    return RankedLeadPackage(
        owner=role,
        approvable=True,
        availability=availability,
        best=fields.get("best"),
        best_runner_up=fields.get("best_runner_up"),
        middle=fields.get("middle"),
        middle_runner_up=fields.get("middle_runner_up"),
        lowest=fields.get("lowest"),
        ignore=fields.get("ignore"),
    )


def build_lead_packages(dashboard: dict, club_id: str, role_results: dict[str, RoleResult], thread_id: str) -> dict[str, dict]:
    players = players_from_dashboard(dashboard, club_id)
    return {
        role.value: build_role_package(role, players, role_results.get(role.value), thread_id).model_dump(mode="json", exclude_none=True)
        for role in SwarmRole
    }


# ==============================================================================
# Deterministic Pre-Approval Validation (Phase E)
# ==============================================================================


def _add_reason(reasons: list[PreApprovalReasonCode], code: PreApprovalReasonCode) -> None:
    if code not in reasons:
        reasons.append(code)


def _is_nonfinite_number(val: Any) -> bool:
    if isinstance(val, bool):
        return False
    if isinstance(val, (int, float)):
        return not math.isfinite(val)
    if isinstance(val, str):
        s = val.strip().lower()
        if s in ("nan", "+nan", "-nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"):
            return True
    return False


def _is_valid_pseudonymous_player_ref(ref: Any) -> bool:
    if not isinstance(ref, str) or not ref.strip():
        return False
    if "@" in ref:
        return False
    if not _PSEUDONYMOUS_PLAYER_REF_RE.fullmatch(ref):
        return False
    if _EXTERNAL_ID_TOKEN_RE.search(ref):
        return False
    return True


def _validate_objective_entry(
    obj_raw: Any,
    reasons: list[PreApprovalReasonCode],
) -> tuple[bool, dict[str, Any] | None]:
    """Validate an Objective or dict using existing METRIC_BOUNDS without duplicating bounds."""
    if isinstance(obj_raw, Objective):
        if _is_nonfinite_number(obj_raw.target_delta):
            _add_reason(reasons, "OBJECTIVE_VALUE_NONFINITE")
            return False, None
        return True, obj_raw.model_dump(mode="json")

    if not isinstance(obj_raw, dict):
        _add_reason(reasons, "PACKAGE_SCHEMA_INVALID")
        return False, None

    ok = True
    metric = obj_raw.get("metric")
    bounds_policy = (
        obj_raw.get("bounds_policy")
        or obj_raw.get("policy_version")
        or "fc27-objective-bounds-v1"
    )
    target_delta = (
        obj_raw.get("target_delta")
        if "target_delta" in obj_raw
        else (obj_raw.get("target_value") if "target_value" in obj_raw else obj_raw.get("target"))
    )
    current_value = obj_raw.get("current_value")

    # 1. Check finiteness of target_delta and optional current_value first
    if _is_nonfinite_number(target_delta) or _is_nonfinite_number(current_value):
        _add_reason(reasons, "OBJECTIVE_VALUE_NONFINITE")
        ok = False

    # 2. Check supported metric & bounds policy against METRIC_BOUNDS
    policy_table = METRIC_BOUNDS.get(str(bounds_policy))
    if policy_table is None or not isinstance(metric, str) or metric not in policy_table:
        _add_reason(reasons, "OBJECTIVE_METRIC_UNSUPPORTED")
        return False, None

    if not ok:
        return False, None

    if target_delta is None or isinstance(target_delta, bool) or not isinstance(target_delta, (int, float)):
        _add_reason(reasons, "OBJECTIVE_BOUND_VIOLATION")
        return False, None

    # 3. Optional current_value finiteness & domain sanity
    if current_value is not None:
        if isinstance(current_value, bool) or not isinstance(current_value, (int, float)):
            _add_reason(reasons, "OBJECTIVE_BOUND_VIOLATION")
            return False, None
        unit_spec = policy_table[metric]["unit"]
        if current_value < 0.0:
            _add_reason(reasons, "OBJECTIVE_BOUND_VIOLATION")
            return False, None
        if unit_spec == "pct_points" and current_value > 100.0:
            _add_reason(reasons, "OBJECTIVE_BOUND_VIOLATION")
            return False, None
        if unit_spec == "rating_points" and current_value > 10.0:
            _add_reason(reasons, "OBJECTIVE_BOUND_VIOLATION")
            return False, None

    # 4. Optional unit check against METRIC_BOUNDS
    supplied_unit = obj_raw.get("unit") if "unit" in obj_raw else obj_raw.get("units")
    if supplied_unit is not None and supplied_unit != policy_table[metric]["unit"]:
        _add_reason(reasons, "OBJECTIVE_BOUND_VIOLATION")
        return False, None

    # 5. Match window bounds and metric delta bounds via validate_metric_bounds / Objective
    min_matches = obj_raw.get("min_matches", 1)
    max_matches = obj_raw.get("max_matches", 5)
    direction = obj_raw.get("direction", "increase")
    if (
        isinstance(min_matches, bool)
        or isinstance(max_matches, bool)
        or not isinstance(min_matches, int)
        or not isinstance(max_matches, int)
        or not (1 <= min_matches <= max_matches <= 10)
        or direction not in ("increase", "decrease")
    ):
        _add_reason(reasons, "OBJECTIVE_BOUND_VIOLATION")
        return False, None

    try:
        validate_metric_bounds(metric, float(target_delta), str(bounds_policy))
        validated_obj = Objective(
            metric=metric,  # type: ignore[arg-type]
            direction=direction,
            target_delta=float(target_delta),
            min_matches=min_matches,
            max_matches=max_matches,
            bounds_policy=str(bounds_policy),  # type: ignore[arg-type]
        )
    except Exception:
        _add_reason(reasons, "OBJECTIVE_BOUND_VIOLATION")
        return False, None

    return True, validated_obj.model_dump(mode="json")


def _validate_position_slot(slot: Any) -> bool:
    if not isinstance(slot, str) or not slot.strip():
        return False
    s = slot.strip()
    if s in VALID_FC27_POSITIONS:
        return True
    base = re.sub(r"[0-9]+$", "", s)
    return base in VALID_FC27_POSITIONS


def _validate_formation_and_assignments_entry(
    *,
    formation: Any,
    lineup: Any,
    assignments: Any,
    roster: Any,
    reasons: list[PreApprovalReasonCode],
) -> tuple[bool, bool]:
    """Validate formation, lineup, assignments, and roster when present.

    Returns (applicable, ok).
    """
    applicable = any(x is not None for x in (formation, lineup, assignments, roster))
    if not applicable:
        return False, True

    ok = True

    if formation is not None:
        if not isinstance(formation, str) or formation not in VALID_FC27_FORMATIONS:
            _add_reason(reasons, "FORMATION_UNSUPPORTED")
            ok = False

    if roster is not None:
        if not isinstance(roster, (list, tuple)):
            _add_reason(reasons, "ROSTER_SIZE_INVALID")
            ok = False
        else:
            if len(roster) < 1 or len(roster) > 11:
                _add_reason(reasons, "ROSTER_SIZE_INVALID")
                ok = False
            seen_r: set[str] = set()
            for p_ref in roster:
                if not _is_valid_pseudonymous_player_ref(p_ref):
                    _add_reason(reasons, "ROLE_ASSIGNMENT_INVALID")
                    if isinstance(p_ref, str) and ("@" in p_ref or _EXTERNAL_ID_TOKEN_RE.search(p_ref)):
                        _add_reason(reasons, "APPROVAL_POLICY_VIOLATION")
                    ok = False
                elif p_ref in seen_r:
                    _add_reason(reasons, "DUPLICATE_PLAYER_ASSIGNMENT")
                    ok = False
                else:
                    seen_r.add(p_ref)

    for item in (lineup, assignments):
        if item is None:
            continue
        if isinstance(item, Lineup):
            item_dict: Any = item.model_dump(mode="json")
        else:
            item_dict = item

        if isinstance(item_dict, dict) and ("positions" in item_dict or "roles" in item_dict or "formation" in item_dict):
            if "formation" in item_dict and item_dict["formation"] is not None:
                f_val = item_dict["formation"]
                if not isinstance(f_val, str) or f_val not in VALID_FC27_FORMATIONS:
                    _add_reason(reasons, "FORMATION_UNSUPPORTED")
                    ok = False

            positions = item_dict.get("positions")
            roles = item_dict.get("roles")
            if positions is not None or roles is not None:
                if not isinstance(positions, dict) or not isinstance(roles, dict):
                    _add_reason(reasons, "ROLE_ASSIGNMENT_INVALID")
                    ok = False
                else:
                    if len(positions) < 1 or len(positions) > 11:
                        _add_reason(reasons, "ROSTER_SIZE_INVALID")
                        ok = False
                    if set(positions.keys()) != set(roles.keys()):
                        _add_reason(reasons, "ROLE_ASSIGNMENT_INVALID")
                        ok = False
                    seen_players: set[str] = set()
                    for slot, p_ref in positions.items():
                        if not _validate_position_slot(slot):
                            _add_reason(reasons, "ROLE_ASSIGNMENT_INVALID")
                            ok = False
                        if not _is_valid_pseudonymous_player_ref(p_ref):
                            _add_reason(reasons, "ROLE_ASSIGNMENT_INVALID")
                            if isinstance(p_ref, str) and ("@" in p_ref or _EXTERNAL_ID_TOKEN_RE.search(p_ref)):
                                _add_reason(reasons, "APPROVAL_POLICY_VIOLATION")
                            ok = False
                        elif p_ref in seen_players:
                            _add_reason(reasons, "DUPLICATE_PLAYER_ASSIGNMENT")
                            ok = False
                        else:
                            seen_players.add(p_ref)
                    for slot, r_name in roles.items():
                        if not _validate_position_slot(slot):
                            _add_reason(reasons, "ROLE_ASSIGNMENT_INVALID")
                            ok = False
                        if not isinstance(r_name, str) or r_name not in VALID_FC27_ROLES:
                            _add_reason(reasons, "ROLE_ASSIGNMENT_INVALID")
                            ok = False

                    subs = item_dict.get("substitutes")
                    if subs is None and isinstance(item_dict.get("tactical_context"), dict):
                        subs = item_dict["tactical_context"].get("substitutes")
                    if subs is not None:
                        if not isinstance(subs, (list, tuple)) or len(subs) > 7:
                            _add_reason(reasons, "ROSTER_SIZE_INVALID")
                            ok = False
                        else:
                            for sub_ref in subs:
                                if not _is_valid_pseudonymous_player_ref(sub_ref):
                                    _add_reason(reasons, "ROLE_ASSIGNMENT_INVALID")
                                    if isinstance(sub_ref, str) and ("@" in sub_ref or _EXTERNAL_ID_TOKEN_RE.search(sub_ref)):
                                        _add_reason(reasons, "APPROVAL_POLICY_VIOLATION")
                                    ok = False
                                elif sub_ref in seen_players:
                                    _add_reason(reasons, "DUPLICATE_PLAYER_ASSIGNMENT")
                                    ok = False
                                else:
                                    seen_players.add(sub_ref)

        elif isinstance(item_dict, (list, tuple)):
            if len(item_dict) < 1 or len(item_dict) > 11:
                _add_reason(reasons, "ROSTER_SIZE_INVALID")
                ok = False
            seen_players_list: set[str] = set()
            for entry in item_dict:
                if not isinstance(entry, dict):
                    _add_reason(reasons, "ROLE_ASSIGNMENT_INVALID")
                    ok = False
                    continue
                p_ref = entry.get("local_player_reference") or entry.get("player_id")
                pos = entry.get("position")
                role_name = entry.get("role")
                if not _is_valid_pseudonymous_player_ref(p_ref):
                    _add_reason(reasons, "ROLE_ASSIGNMENT_INVALID")
                    if isinstance(p_ref, str) and ("@" in p_ref or _EXTERNAL_ID_TOKEN_RE.search(p_ref)):
                        _add_reason(reasons, "APPROVAL_POLICY_VIOLATION")
                    ok = False
                elif p_ref in seen_players_list:
                    _add_reason(reasons, "DUPLICATE_PLAYER_ASSIGNMENT")
                    ok = False
                else:
                    seen_players_list.add(p_ref)
                if not _validate_position_slot(pos):
                    _add_reason(reasons, "ROLE_ASSIGNMENT_INVALID")
                    ok = False
                if not isinstance(role_name, str) or role_name not in VALID_FC27_ROLES:
                    _add_reason(reasons, "ROLE_ASSIGNMENT_INVALID")
                    ok = False

        elif isinstance(item_dict, dict):
            if len(item_dict) < 1 or len(item_dict) > 11:
                _add_reason(reasons, "ROSTER_SIZE_INVALID")
                ok = False
            seen_players_map: set[str] = set()
            for k, v in item_dict.items():
                if not isinstance(v, dict):
                    _add_reason(reasons, "ROLE_ASSIGNMENT_INVALID")
                    ok = False
                    continue
                if "position" in v:
                    p_ref = v.get("local_player_reference") or v.get("player_id") or k
                    pos = v.get("position")
                else:
                    pos = k
                    p_ref = v.get("local_player_reference") or v.get("player_id")
                role_name = v.get("role")
                if not _is_valid_pseudonymous_player_ref(p_ref):
                    _add_reason(reasons, "ROLE_ASSIGNMENT_INVALID")
                    if isinstance(p_ref, str) and ("@" in p_ref or _EXTERNAL_ID_TOKEN_RE.search(p_ref)):
                        _add_reason(reasons, "APPROVAL_POLICY_VIOLATION")
                    ok = False
                elif p_ref in seen_players_map:
                    _add_reason(reasons, "DUPLICATE_PLAYER_ASSIGNMENT")
                    ok = False
                else:
                    seen_players_map.add(p_ref)
                if not _validate_position_slot(pos):
                    _add_reason(reasons, "ROLE_ASSIGNMENT_INVALID")
                    ok = False
                if not isinstance(role_name, str) or role_name not in VALID_FC27_ROLES:
                    _add_reason(reasons, "ROLE_ASSIGNMENT_INVALID")
                    ok = False
        else:
            _add_reason(reasons, "ROLE_ASSIGNMENT_INVALID")
            ok = False

    return True, ok


def _extract_analytics_dict(meta: Any) -> dict[str, Any] | None:
    if meta is None:
        return None
    if isinstance(meta, BaseModel):
        dumped = meta.model_dump(mode="json")
        if "window" in dumped and isinstance(dumped["window"], dict):
            return {
                **dumped["window"],
                "formula_version": dumped.get("formula_version", dumped["window"].get("formula_version")),
                "confidence_tier": dumped["window"].get("confidence_tier"),
            }
        return dumped
    if isinstance(meta, dict):
        if "window" in meta and isinstance(meta["window"], dict):
            return {
                **meta["window"],
                "formula_version": meta.get("formula_version", meta["window"].get("formula_version")),
                "confidence_tier": meta["window"].get("confidence_tier"),
            }
        return dict(meta)
    return {"_invalid_type": True}


def _validate_analytics_metadata_entry(
    meta_dict: dict[str, Any],
    *,
    minimum_sample_size: int,
    reasons: list[PreApprovalReasonCode],
) -> tuple[bool, str | None, str | None]:
    """Validate confidence tier, formula version, and eligible sample count."""
    if meta_dict.get("_invalid_type"):
        _add_reason(reasons, "CONFIDENCE_METADATA_MISSING")
        return False, None, None

    ok = True
    conf_tier = meta_dict.get("confidence_tier") or meta_dict.get("sample_size_tier")
    form_ver = meta_dict.get("formula_version")

    eligible_raw = (
        meta_dict.get("eligible_matches")
        if "eligible_matches" in meta_dict
        else (meta_dict.get("sample_size") if "sample_size" in meta_dict else meta_dict.get("match_count"))
    )
    min_req_raw = meta_dict.get("minimum_sample_size", minimum_sample_size)
    try:
        effective_min_sample = max(3, int(min_req_raw))
    except (TypeError, ValueError):
        effective_min_sample = max(3, minimum_sample_size)

    # 1. Confidence tier presence and enum membership
    resolved_tier: str | None = None
    if not isinstance(conf_tier, str) or not conf_tier.strip() or conf_tier not in VALID_SAMPLE_SIZE_TIERS:
        _add_reason(reasons, "CONFIDENCE_METADATA_MISSING")
        ok = False
    else:
        resolved_tier = conf_tier

    # 2. Formula version presence and support
    resolved_formula: str | None = None
    if not isinstance(form_ver, str) or not form_ver.strip():
        _add_reason(reasons, "CONFIDENCE_METADATA_MISSING")
        _add_reason(reasons, "FORMULA_VERSION_UNSUPPORTED")
        ok = False
    elif form_ver != FORMULA_VERSION:
        _add_reason(reasons, "FORMULA_VERSION_UNSUPPORTED")
        resolved_formula = form_ver
        ok = False
    else:
        resolved_formula = form_ver

    # 3. Sample count & INSUFFICIENT_DATA policy
    if eligible_raw is not None:
        if isinstance(eligible_raw, bool) or not isinstance(eligible_raw, int) or eligible_raw < 0:
            _add_reason(reasons, "CONFIDENCE_METADATA_MISSING")
            ok = False
        else:
            expected_tier = determine_sample_size_tier(eligible_raw)
            if resolved_tier is not None and resolved_tier != expected_tier:
                _add_reason(reasons, "APPROVAL_POLICY_VIOLATION")
                if eligible_raw < effective_min_sample or expected_tier == "INSUFFICIENT_DATA":
                    _add_reason(reasons, "INSUFFICIENT_SAMPLE")
                    _add_reason(reasons, "INSUFFICIENT_DATA")
                ok = False
            if eligible_raw < effective_min_sample or expected_tier == "INSUFFICIENT_DATA":
                _add_reason(reasons, "INSUFFICIENT_SAMPLE")
                _add_reason(reasons, "INSUFFICIENT_DATA")
                ok = False

    if resolved_tier == "INSUFFICIENT_DATA":
        _add_reason(reasons, "INSUFFICIENT_SAMPLE")
        _add_reason(reasons, "INSUFFICIENT_DATA")
        ok = False

    return ok, resolved_tier, resolved_formula


def validate_pre_approval_packages(
    lead_packages: Any,
    *,
    package_version_hash: Any = _UNSET,
    package_version: Any = _UNSET,
    candidates: Iterable[dict[str, Any]] | None = None,
    thread_id: str | None = None,
    lineup: Any = None,
    formation: Any = None,
    assignments: Any = None,
    roster: Any = None,
    analytics_metadata: Any = None,
    recommendations: Any = None,
    recommendations_enabled: bool | None = None,
    minimum_sample_size: int = 3,
    require_analytics_metadata: bool = False,
    review_required: bool = False,
) -> PreApprovalValidationResult:
    """Deterministically validate lead packages and optional FC 27 metadata before human approval.

    Pure function: performs zero network I/O, zero database writes, generates zero
    recommendations, and never exposes player aliases or secrets.
    """
    validated_at = datetime.now(timezone.utc).isoformat()
    reasons: list[PreApprovalReasonCode] = []
    warnings: list[str] = []

    effective_recs_enabled = (
        is_fc27_recommendations_enabled()
        if recommendations_enabled is None
        else bool(recommendations_enabled)
    )

    # Resolve provided package version hash argument
    provided_hash = package_version_hash if package_version_hash is not _UNSET else package_version

    # 1. Check recommendations argument against recommendation gate
    if recommendations is not None:
        has_recs = True if isinstance(recommendations, (dict, BaseModel)) else bool(list(recommendations) if isinstance(recommendations, Iterable) and not isinstance(recommendations, (str, bytes)) else recommendations)
        if has_recs and not effective_recs_enabled:
            _add_reason(reasons, "RECOMMENDATION_DISABLED")

    # 2. Validate package collection non-empty and normalize to ordered items
    package_items: list[tuple[str, Any]] = []
    canonical_for_hash: Any = None

    if lead_packages is None:
        _add_reason(reasons, "EMPTY_LEAD_PACKAGES")
    elif isinstance(lead_packages, dict):
        if len(lead_packages) == 0:
            _add_reason(reasons, "EMPTY_LEAD_PACKAGES")
        else:
            canonical_Dict: dict[str, Any] = {}
            for k in sorted(lead_packages.keys(), key=str):
                v = lead_packages[k]
                canonical_Dict[str(k)] = v.model_dump(mode="json", exclude_none=True) if isinstance(v, BaseModel) else v
                package_items.append((str(k), canonical_Dict[str(k)]))
            canonical_for_hash = canonical_Dict
    elif isinstance(lead_packages, (list, tuple)):
        if len(lead_packages) == 0:
            _add_reason(reasons, "EMPTY_LEAD_PACKAGES")
        else:
            temp_dict: dict[str, Any] = {}
            for idx, item in enumerate(lead_packages):
                dumped = item.model_dump(mode="json", exclude_none=True) if isinstance(item, BaseModel) else item
                owner_key = str(dumped.get("owner", f"pkg_{idx}")) if isinstance(dumped, dict) else f"pkg_{idx}"
                if owner_key in temp_dict:
                    _add_reason(reasons, "DUPLICATE_CANDIDATE_PACKAGE")
                    package_items.append((f"{owner_key}__dup_{idx}", dumped))
                else:
                    temp_dict[owner_key] = dumped
                    package_items.append((owner_key, dumped))
            canonical_for_hash = {k: temp_dict[k] for k in sorted(temp_dict.keys())}
    else:
        _add_reason(reasons, "PACKAGE_SCHEMA_INVALID")
        _add_reason(reasons, "EMPTY_LEAD_PACKAGES")

    # 3. Validate package_version SHA-256 presence, format, and canonical recomputation
    computed_hash: str | None = None
    if isinstance(canonical_for_hash, dict) and len(canonical_for_hash) > 0:
        try:
            computed_hash = compute_package_version(canonical_for_hash)
        except Exception:
            computed_hash = None

    resolved_hash: str | None = None
    if provided_hash is _UNSET or provided_hash is None or (isinstance(provided_hash, str) and not provided_hash.strip()):
        _add_reason(reasons, "PACKAGE_HASH_MISSING")
    elif not isinstance(provided_hash, str) or not _SHA256_HEX_RE.fullmatch(provided_hash):
        _add_reason(reasons, "PACKAGE_HASH_INVALID")
    else:
        resolved_hash = provided_hash
        if computed_hash is not None and provided_hash != computed_hash:
            _add_reason(reasons, "PACKAGE_HASH_MISMATCH")

    # 4. Prepare graph state candidate lookup sets if provided
    allowed_player_ids: set[str] | None = None
    allowed_candidate_ids: set[str] | None = None
    if candidates is not None:
        allowed_player_ids = set()
        cand_ids_found: set[str] = set()
        for c_item in candidates:
            if isinstance(c_item, dict):
                pid = c_item.get("player_id")
                cid = c_item.get("candidate_id")
                if isinstance(pid, str) and pid.strip():
                    allowed_player_ids.add(pid)
                if isinstance(cid, str) and cid.strip():
                    cand_ids_found.add(cid)
        if cand_ids_found:
            allowed_candidate_ids = cand_ids_found

    # 5. Validate each package deterministically
    validated_package_count = 0
    rejected_package_count = 0
    approvable_package_count = 0
    seen_owners: set[str] = set()
    seen_candidate_ids_global: set[str] = set()
    seen_selection_fingerprints: set[str] = set()
    formation_applicable = False
    resolved_confidence_tier: str | None = None
    resolved_formula_version: str | None = None
    effective_review_required = bool(review_required)

    valid_roles = {r.value for r in SwarmRole}

    for key, pkg in package_items:
        pkg_ok = True

        if isinstance(pkg, EastCoastersRecommendation):
            if not effective_recs_enabled:
                _add_reason(reasons, "RECOMMENDATION_DISABLED")
            _add_reason(reasons, "PACKAGE_SCHEMA_INVALID")
            rejected_package_count += 1
            continue

        if not isinstance(pkg, dict):
            _add_reason(reasons, "PACKAGE_SCHEMA_INVALID")
            rejected_package_count += 1
            continue

        if pkg.get("review_required") is True:
            effective_review_required = True

        # Check recommendation-specific markers inside package
        if (
            pkg.get("package_type") == "recommendation"
            or pkg.get("is_recommendation") is True
            or pkg.get("recommendation") is not None
            or pkg.get("recommendations") is not None
            or "recommendation_id" in pkg
            or "empirical_observations" in pkg
        ):
            if not effective_recs_enabled:
                _add_reason(reasons, "RECOMMENDATION_DISABLED")
                pkg_ok = False

        # Check unknown keys on package
        unknown_pkg_keys = set(pkg.keys()) - _ALLOWED_PACKAGE_KEYS
        if unknown_pkg_keys:
            _add_reason(reasons, "PACKAGE_SCHEMA_INVALID")
            pkg_ok = False

        owner = pkg.get("owner")
        if not isinstance(owner, str) or owner not in valid_roles:
            _add_reason(reasons, "PACKAGE_SCHEMA_INVALID")
            pkg_ok = False
        else:
            base_key = key.split("__dup_")[0]
            if base_key != owner:
                _add_reason(reasons, "CANDIDATE_ID_MISMATCH")
                pkg_ok = False
            if owner in seen_owners:
                _add_reason(reasons, "DUPLICATE_CANDIDATE_PACKAGE")
                pkg_ok = False
            seen_owners.add(owner)

        # Package-level objective check
        pkg_obj_norm: dict[str, Any] | None = None
        if pkg.get("objective") is not None:
            obj_ok, pkg_obj_norm = _validate_objective_entry(pkg["objective"], reasons)
            if not obj_ok:
                pkg_ok = False

        # Package-level formation / lineup / assignments / roster check
        pkg_form_app, pkg_form_ok = _validate_formation_and_assignments_entry(
            formation=pkg.get("formation"),
            lineup=pkg.get("lineup"),
            assignments=pkg.get("assignments"),
            roster=pkg.get("roster"),
            reasons=reasons,
        )
        if pkg_form_app:
            formation_applicable = True
        if not pkg_form_ok:
            pkg_ok = False

        # Package-level analytics / confidence / formula check
        pkg_has_analytics = (
            pkg.get("analytics_metadata") is not None
            or pkg.get("analytics_backed") is True
            or "confidence_tier" in pkg
            or "formula_version" in pkg
            or "sample_size" in pkg
            or "eligible_matches" in pkg
        )
        if pkg_has_analytics:
            pkg_meta = _extract_analytics_dict(pkg.get("analytics_metadata")) or {}
            for m_key in ("confidence_tier", "formula_version", "sample_size", "eligible_matches", "minimum_sample_size"):
                if m_key in pkg and m_key not in pkg_meta:
                    pkg_meta[m_key] = pkg[m_key]
            meta_ok, tier_val, form_val = _validate_analytics_metadata_entry(
                pkg_meta,
                minimum_sample_size=minimum_sample_size,
                reasons=reasons,
            )
            if tier_val is not None:
                resolved_confidence_tier = tier_val
            if form_val is not None:
                resolved_formula_version = form_val
            if not meta_ok:
                pkg_ok = False

        # Validate candidate selections
        pkg_candidate_ids: set[str] = set()
        pkg_player_ids: set[str] = set()
        clean_selections: dict[str, Any] = {}

        for sel in SELECTIONS:
            cand = pkg.get(sel)
            if cand is None:
                continue
            if isinstance(cand, Candidate):
                cand_dict: Any = cand.model_dump(mode="json")
            else:
                cand_dict = cand

            if not isinstance(cand_dict, dict):
                _add_reason(reasons, "PACKAGE_SCHEMA_INVALID")
                pkg_ok = False
                continue

            cid = cand_dict.get("candidate_id")
            pid = cand_dict.get("player_id")
            c_owner = cand_dict.get("owner")

            if not isinstance(cid, str) or not cid.strip() or not _CANDIDATE_ID_RE.fullmatch(cid):
                _add_reason(reasons, "CANDIDATE_ID_MISMATCH")
                pkg_ok = False
            if not isinstance(pid, str) or not pid.strip() or not _is_valid_pseudonymous_player_ref(pid):
                _add_reason(reasons, "PACKAGE_SCHEMA_INVALID")
                pkg_ok = False
            if isinstance(owner, str) and str(c_owner) != owner:
                _add_reason(reasons, "CANDIDATE_ID_MISMATCH")
                pkg_ok = False

            # Check deterministic candidate_id conventions
            if isinstance(cid, str) and isinstance(pid, str) and isinstance(owner, str):
                if "-" in cid and cid.split("-", 1)[0] in valid_roles:
                    expected_hyphen_id = f"{owner}-{sel}-{pid}"
                    if cid != expected_hyphen_id:
                        _add_reason(reasons, "CANDIDATE_ID_MISMATCH")
                        pkg_ok = False
                elif thread_id and _HASH_CANDIDATE_ID_RE.fullmatch(cid) and pkg.get("policy_version", POLICY_VERSION) == POLICY_VERSION:
                    expected_hash_id = _candidate_id(thread_id, owner, sel, pid)
                    if cid != expected_hash_id:
                        _add_reason(reasons, "CANDIDATE_ID_MISMATCH")
                        pkg_ok = False

                if allowed_player_ids is not None and pid not in allowed_player_ids:
                    _add_reason(reasons, "CANDIDATE_ID_MISMATCH")
                    pkg_ok = False
                if allowed_candidate_ids is not None and cid not in allowed_candidate_ids:
                    _add_reason(reasons, "CANDIDATE_ID_MISMATCH")
                    pkg_ok = False

                if cid in pkg_candidate_ids or cid in seen_candidate_ids_global:
                    _add_reason(reasons, "DUPLICATE_CANDIDATE_PACKAGE")
                    pkg_ok = False
                if pid in pkg_player_ids:
                    _add_reason(reasons, "DUPLICATE_CANDIDATE_PACKAGE")
                    pkg_ok = False

                pkg_candidate_ids.add(cid)
                seen_candidate_ids_global.add(cid)
                pkg_player_ids.add(pid)

            # Candidate objective validation
            cand_for_model = dict(cand_dict)
            if cand_dict.get("objective") is not None:
                c_obj_ok, c_obj_norm = _validate_objective_entry(cand_dict["objective"], reasons)
                if not c_obj_ok:
                    pkg_ok = False
                    cand_for_model.pop("objective", None)
                elif pkg_obj_norm is not None and c_obj_norm != pkg_obj_norm:
                    _add_reason(reasons, "OBJECTIVE_BOUND_VIOLATION")
                    pkg_ok = False

            try:
                validated_cand = Candidate.model_validate(cand_for_model)
                clean_selections[sel] = validated_cand
            except Exception:
                _add_reason(reasons, "PACKAGE_SCHEMA_INVALID")
                pkg_ok = False

        # Check duplicate selection fingerprint across packages (if identical player+selection+score+rationale reused with forged owner)
        if pkg_candidate_ids:
            fp = "|".join(f"{sel}:{pkg.get(sel, {}).get('candidate_id')}" for sel in SELECTIONS if isinstance(pkg.get(sel), dict))
            if fp and fp in seen_selection_fingerprints:
                _add_reason(reasons, "DUPLICATE_CANDIDATE_PACKAGE")
                pkg_ok = False
            if fp:
                seen_selection_fingerprints.add(fp)

        # Validate core package structure via RankedLeadPackage
        approvable_flag = pkg.get("approvable")
        if not isinstance(approvable_flag, bool):
            _add_reason(reasons, "PACKAGE_SCHEMA_INVALID")
            pkg_ok = False
        elif pkg_ok and isinstance(owner, str) and owner in valid_roles:
            availability = pkg.get("availability")
            if availability is None:
                availability = {"players": len(clean_selections), "selections": len(clean_selections)}
            elif not isinstance(availability, dict) or not all(
                isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) and v >= 0
                for k, v in availability.items()
            ):
                _add_reason(reasons, "PACKAGE_SCHEMA_INVALID")
                pkg_ok = False

            if pkg_ok:
                try:
                    RankedLeadPackage(
                        owner=SwarmRole(owner),
                        approvable=approvable_flag,
                        reason=pkg.get("reason"),
                        policy_version=pkg.get("policy_version", POLICY_VERSION),
                        availability=availability,
                        **clean_selections,
                    )
                    if len(clean_selections) == len(SELECTIONS):
                        LeadPackage(
                            owner=SwarmRole(owner),
                            **clean_selections,
                        )
                except Exception:
                    _add_reason(reasons, "PACKAGE_SCHEMA_INVALID")
                    pkg_ok = False

        if pkg_ok:
            validated_package_count += 1
            if approvable_flag is True:
                approvable_package_count += 1
        else:
            rejected_package_count += 1

    # 6. Top-level formation / lineup / assignments / roster validation
    top_form_app, _ = _validate_formation_and_assignments_entry(
        formation=formation,
        lineup=lineup,
        assignments=assignments,
        roster=roster,
        reasons=reasons,
    )
    if top_form_app:
        formation_applicable = True

    # 7. Top-level analytics / confidence / sample policy validation
    if analytics_metadata is not None or require_analytics_metadata:
        top_meta = _extract_analytics_dict(analytics_metadata)
        if top_meta is None:
            _add_reason(reasons, "CONFIDENCE_METADATA_MISSING")
            _add_reason(reasons, "FORMULA_VERSION_UNSUPPORTED")
        else:
            if top_meta.get("review_required") is True:
                effective_review_required = True
            _, tier_val, form_val = _validate_analytics_metadata_entry(
                top_meta,
                minimum_sample_size=minimum_sample_size,
                reasons=reasons,
            )
            if tier_val is not None:
                resolved_confidence_tier = tier_val
            if form_val is not None:
                resolved_formula_version = form_val

    if effective_review_required:
        _add_reason(reasons, "REVIEW_REQUIRED")

    if validated_package_count > 0 and approvable_package_count == 0 and not reasons:
        warnings.append("zero_approvable_role_packages")

    valid = (len(reasons) == 0) and (rejected_package_count == 0) and (validated_package_count > 0)
    approvable = valid and (not effective_review_required) and (approvable_package_count > 0)

    return PreApprovalValidationResult(
        valid=valid,
        approvable=approvable,
        policy_version=PRE_APPROVAL_POLICY_VERSION,
        validated_at=validated_at,
        package_version=resolved_hash if resolved_hash is not None else computed_hash,
        reason_codes=tuple(reasons),
        warnings=tuple(warnings),
        validated_package_count=validated_package_count,
        rejected_package_count=rejected_package_count,
        confidence_tier=resolved_confidence_tier,
        formula_version=resolved_formula_version,
        formation_registry_version=FORMATION_REGISTRY_VERSION if formation_applicable else "NOT_APPLICABLE",
        review_required=effective_review_required,
    )


def emit_pre_approval_validation_trace(
    tracer: Any,
    *,
    thread_id: str,
    club_id: str | None,
    result: PreApprovalValidationResult | dict[str, Any],
) -> dict[str, Any]:
    """Emit metadata-only pre-approval validation trace event via the existing tracer abstraction."""
    summary = result.to_safe_summary() if isinstance(result, PreApprovalValidationResult) else dict(result)
    metadata = {
        "thread_id": str(thread_id),
        "club_id": str(club_id) if club_id is not None else None,
        "package_version": summary.get("package_version"),
        "policy_version": summary.get("policy_version", PRE_APPROVAL_POLICY_VERSION),
        "package_count": int(summary.get("validated_package_count", 0)),
        "reason_codes": list(summary.get("reason_codes") or ()),
        "confidence_tier": summary.get("confidence_tier"),
        "formula_version": summary.get("formula_version"),
        "timestamp": summary.get("validated_at"),
    }
    if summary.get("review_required"):
        event_name = "preapproval.validation.review_required"
        level = "WARNING"
    elif summary.get("valid") and summary.get("approvable"):
        event_name = "preapproval.validation.passed"
        level = "DEFAULT"
    else:
        event_name = "preapproval.validation.failed"
        level = "WARNING"

    if tracer is not None and hasattr(tracer, "event"):
        tracer.event(event_name, str(thread_id), metadata, level=level)
    return {"event_name": event_name, "level": level, "metadata": metadata}
