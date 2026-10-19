from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass
from threading import RLock

from .models import EventType, Evidence, Insight, MatchEvent, MatchState, TeamState


def _distance(event: MatchEvent) -> float:
    if None in (event.x, event.y, event.end_x, event.end_y):
        return 0.0
    return math.hypot(event.end_x - event.x, event.end_y - event.y)


@dataclass(frozen=True)
class ApplyResult:
    state: MatchState
    duplicate: bool
    insights: list[Insight]


class MatchAccumulator:
    """Thread-safe, deterministic match state and evidence-backed signal engine."""

    def __init__(self) -> None:
        self._states: dict[str, MatchState] = {}
        self._seen: dict[str, set[str]] = defaultdict(set)
        self._recent: dict[str, deque[MatchEvent]] = defaultdict(lambda: deque(maxlen=40))
        self._evidence_events: dict[str, dict[str, MatchEvent]] = defaultdict(dict)
        self._insights: dict[str, list[Insight]] = defaultdict(list)
        self._lock = RLock()

    def get(self, match_id: str) -> MatchState | None:
        with self._lock:
            state = self._states.get(match_id)
            return state.model_copy(deep=True) if state else None

    def evidence_event(self, match_id: str, event_id: str) -> MatchEvent | None:
        with self._lock:
            event = self._evidence_events.get(match_id, {}).get(event_id)
            return event.model_copy(deep=True) if event else None

    def insights(self, match_id: str) -> list[Insight]:
        with self._lock:
            return [item.model_copy(deep=True) for item in self._insights.get(match_id, [])]

    def apply(self, event: MatchEvent) -> ApplyResult:
        with self._lock:
            state = self._states.setdefault(event.match_id, MatchState(match_id=event.match_id))
            if event.event_id in self._seen[event.match_id]:
                state.duplicate_count += 1
                return ApplyResult(state.model_copy(deep=True), True, [])
            if event.sequence <= state.last_sequence:
                raise ValueError(f"out-of-order sequence {event.sequence}; last accepted is {state.last_sequence}")

            self._seen[event.match_id].add(event.event_id)
            self._evidence_events[event.match_id][event.event_id] = event
            self._recent[event.match_id].append(event)
            state.last_sequence = event.sequence
            state.clock_ms = max(state.clock_ms, event.clock_ms)
            state.event_count += 1
            if event.event_type == EventType.MATCH_START:
                state.status = "live"
            elif event.event_type == EventType.MATCH_END:
                state.status = "finished"
            elif state.status == "pending":
                state.status = "live"

            team = None
            if event.team_id:
                team = state.teams.setdefault(event.team_id, TeamState(team_id=event.team_id))
            self._update_team(team, event)
            generated = self._derive_insights(event, state)
            self._insights[event.match_id].extend(generated)
            return ApplyResult(state.model_copy(deep=True), False, generated)

    @staticmethod
    def _update_team(team: TeamState | None, event: MatchEvent) -> None:
        if team is None:
            return
        if event.event_type == EventType.PASS:
            team.passes += 1
            team.completed_passes += int(event.outcome == "success")
        elif event.event_type in {EventType.SHOT, EventType.GOAL}:
            team.shots += 1
            team.shots_on_target += int(event.outcome in {"on_target", "success"} or event.event_type == EventType.GOAL)
            team.goals += int(event.event_type == EventType.GOAL)
        elif event.event_type == EventType.TACKLE:
            team.tackles += int(event.outcome == "success")
        elif event.event_type == EventType.PRESSURE:
            team.pressure_events += 1
        elif event.event_type == EventType.POSSESSION:
            share = event.metadata.get("share")
            if isinstance(share, (int, float)):
                team.possession_pct = max(0.0, min(100.0, float(share)))

    def _derive_insights(self, event: MatchEvent, state: MatchState) -> list[Insight]:
        out: list[Insight] = []
        base = dict(match_id=event.match_id, sequence=event.sequence, clock_ms=event.clock_ms,
                    team_id=event.team_id, player_id=event.player_id)

        if event.event_type == EventType.GOAL:
            score = {k: v.goals for k, v in state.teams.items()}
            out.append(Insight(**base, kind="goal", severity="major", stats={"score": str(score)},
                evidence=Evidence(event_ids=[event.event_id], inputs={"event_type": "goal", "score": str(score)},
                                  formula="authoritative goal event + aggregate team goals", confidence=1.0), expires_in_ms=20_000))

        if event.event_type == EventType.SHOT and event.speed_kmh is not None and event.speed_kmh >= 95:
            on_target = int(event.outcome == "on_target")
            out.append(Insight(**base, kind="shot_speed", severity="notable",
                stats={"speed_kmh": round(event.speed_kmh, 1), "on_target": on_target},
                evidence=Evidence(event_ids=[event.event_id], inputs={"speed_kmh": round(event.speed_kmh, 1), "on_target": on_target},
                                  formula="highlight when measured shot speed >= 95 km/h", confidence=0.96)))

        if event.event_type == EventType.PASS:
            distance = _distance(event)
            progressive = max(0.0, (event.end_x or 0) - (event.x or 0))
            pressure = event.pressure or 0.0
            difficulty = min(100.0, distance * 1.15 + progressive * 0.55 + pressure * 28)
            if difficulty >= 55:
                out.append(Insight(**base, kind="pass_quality", severity="notable",
                    stats={"distance_units": round(distance, 1), "difficulty": round(difficulty, 1),
                           "progressive_units": round(progressive, 1), "completed": int(event.outcome == "success")},
                    evidence=Evidence(event_ids=[event.event_id],
                        inputs={"distance": round(distance, 2), "progressive": round(progressive, 2), "pressure": round(pressure, 2)},
                        formula="min(100, distance*1.15 + progressive*0.55 + pressure*28)", confidence=0.91)))

        if event.event_type == EventType.PRESSURE and (event.pressure or 0) >= 0.72:
            recent = [e for e in self._recent[event.match_id]
                      if e.event_type == EventType.PRESSURE and e.team_id == event.team_id
                      and event.clock_ms - e.clock_ms <= 30_000 and (e.pressure or 0) >= 0.72]
            if len(recent) >= 3:
                ids = [e.event_id for e in recent[-5:]]
                average = sum(e.pressure or 0 for e in recent[-5:]) / min(5, len(recent))
                out.append(Insight(**base, kind="pressing_surge", severity="notable",
                    stats={"high_pressure_events_30s": len(recent), "average_pressure": round(average, 2)},
                    evidence=Evidence(event_ids=ids,
                        inputs={"events": len(recent), "window_seconds": 30, "average_pressure": round(average, 2)},
                        formula="count pressure >= .72 for same team in trailing 30 seconds", confidence=0.94)))

        if event.event_type == EventType.POSSESSION and event.team_id:
            share = state.teams[event.team_id].possession_pct
            previous = event.metadata.get("previous_share")
            if isinstance(previous, (int, float)) and abs(share - float(previous)) >= 10:
                out.append(Insight(**base, kind="possession_swing", severity="notable",
                    stats={"from_pct": round(float(previous), 1), "to_pct": round(share, 1), "swing_points": round(share-float(previous), 1)},
                    evidence=Evidence(event_ids=[event.event_id], inputs={"previous": float(previous), "current": share},
                        formula="current possession share - previous possession share", confidence=0.98)))

        if event.event_type == EventType.TACKLE and event.outcome == "success" and (event.x or 0) >= 65:
            out.append(Insight(**base, kind="tackle", severity="notable",
                stats={"field_x": round(event.x or 0, 1), "won": 1},
                evidence=Evidence(event_ids=[event.event_id], inputs={"field_x": round(event.x or 0, 1)},
                    formula="successful tackle in attacking 35% of pitch", confidence=0.97)))
        return out
