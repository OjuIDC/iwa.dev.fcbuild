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

import hashlib
import math
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from .roles import RoleResult
from .schemas import Candidate, SwarmRole, Tier

POLICY_VERSION = "fc27-rank-v1"
SELECTIONS = ("best", "best_runner_up", "middle", "middle_runner_up", "lowest", "ignore")


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
    block = next((v for k, v in (latest.get("players") or {}).items() if str(k) == str(club_id)), {}) or {}
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
    return RankedLeadPackage(owner=role, approvable=True, availability=availability, **fields)


def build_lead_packages(dashboard: dict, club_id: str, role_results: dict[str, RoleResult], thread_id: str) -> dict[str, dict]:
    players = players_from_dashboard(dashboard, club_id)
    return {
        role.value: build_role_package(role, players, role_results.get(role.value), thread_id).model_dump(mode="json", exclude_none=True)
        for role in SwarmRole
    }
