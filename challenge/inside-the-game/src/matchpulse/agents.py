from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass

from .localization import render
from .models import AudienceProfile, Insight, MatchEvent, MatchRecap, Persona, PersonalizedInsight
from .state import MatchAccumulator

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentHandoff:
    agent: str
    ok: bool
    duration_ms: float
    detail: str = ""


class InterpretationAgent:
    name = "interpretation-agent"

    def __init__(self, state: MatchAccumulator) -> None:
        self.state = state

    def run(self, event: MatchEvent):
        return self.state.apply(event)


class ExplainabilityAgent:
    name = "explainability-agent"

    def __init__(self, state: MatchAccumulator) -> None:
        self.state = state

    def run(self, insight: Insight) -> Insight:
        missing = [eid for eid in insight.evidence.event_ids if self.state.evidence_event(insight.match_id, eid) is None]
        if missing:
            raise ValueError(f"missing evidence events: {missing}")
        if not insight.evidence.formula.strip():
            raise ValueError("evidence formula is required")
        return insight


class PersonalizationAgent:
    name = "personalization-agent"

    @staticmethod
    def relevance(insight: Insight, profile: AudienceProfile) -> float:
        score = {"major": 0.9, "notable": 0.7, "info": 0.5}[insight.severity]
        if profile.favorite_club and insight.team_id == profile.favorite_club:
            score += 0.08
        if profile.favorite_player and insight.player_id == profile.favorite_player:
            score += 0.12
        interest_map = {
            "pass_quality": "passing", "shot_speed": "shooting", "goal": "shooting",
            "pressing_surge": "pressing", "possession_swing": "possession", "tackle": "defending",
        }
        if interest_map.get(insight.kind) in profile.metric_interests:
            score += 0.08
        if profile.persona == Persona.BROADCASTER and insight.severity == "info":
            score -= 0.1
        return max(0.0, min(1.0, score))

    def run(self, insight: Insight, profile: AudienceProfile) -> PersonalizedInsight:
        title, body, overlay = render(insight, profile)
        if profile.detail.value == "compact":
            body = overlay
        elif profile.detail.value == "deep" and profile.persona == Persona.ANALYST:
            body = f"{body} Evidence: {insight.evidence.formula}; inputs={insight.evidence.inputs}."
        return PersonalizedInsight(
            insight_id=insight.insight_id, match_id=insight.match_id, profile_id=profile.profile_id,
            locale=profile.locale, persona=profile.persona, priority=self.relevance(insight, profile),
            title=title, body=body, overlay_text=overlay, clock_ms=insight.clock_ms,
            sequence=insight.sequence, severity=insight.severity, evidence=insight.evidence,
            stats=insight.stats, expires_in_ms=insight.expires_in_ms,
        )


class SupervisorAgent:
    """Purposeful multi-agent pipeline with shared state and isolated fallbacks."""

    def __init__(self, state: MatchAccumulator | None = None) -> None:
        self.state = state or MatchAccumulator()
        self.interpret = InterpretationAgent(self.state)
        self.explain = ExplainabilityAgent(self.state)
        self.personalize = PersonalizationAgent()

    def process(self, event: MatchEvent, profiles: Iterable[AudienceProfile]):
        handoffs: list[AgentHandoff] = []
        started = time.perf_counter()
        apply_result = self.interpret.run(event)
        handoffs.append(AgentHandoff(self.interpret.name, True, (time.perf_counter() - started) * 1000))
        outputs: list[PersonalizedInsight] = []
        warnings: list[str] = []
        for candidate in apply_result.insights:
            explain_started = time.perf_counter()
            try:
                grounded = self.explain.run(candidate)
                handoffs.append(AgentHandoff(self.explain.name, True, (time.perf_counter() - explain_started) * 1000))
            except Exception as exc:
                handoffs.append(AgentHandoff(self.explain.name, False, (time.perf_counter() - explain_started) * 1000, type(exc).__name__))
                warnings.append(f"dropped ungrounded insight {candidate.insight_id}")
                log.warning("Insight dropped by evidence gate: %s", type(exc).__name__)
                continue
            for profile in profiles:
                p_started = time.perf_counter()
                try:
                    outputs.append(self.personalize.run(grounded, profile))
                    handoffs.append(AgentHandoff(self.personalize.name, True, (time.perf_counter() - p_started) * 1000, profile.profile_id))
                except Exception as exc:
                    handoffs.append(AgentHandoff(self.personalize.name, False, (time.perf_counter() - p_started) * 1000, profile.profile_id))
                    warnings.append(f"profile {profile.profile_id} used no output: {type(exc).__name__}")
        return apply_result, outputs, warnings, handoffs

    def recap(self, match_id: str, profile: AudienceProfile) -> MatchRecap:
        state = self.state.get(match_id)
        if state is None:
            raise KeyError(match_id)
        candidates = self.state.insights(match_id)
        ranked = sorted((self.personalize.run(i, profile) for i in candidates), key=lambda x: (x.priority, x.sequence), reverse=True)
        selected = list(reversed(ranked[:5]))
        if profile.locale == "es":
            headline = f"Resumen del partido {match_id}"
        elif profile.locale == "fr":
            headline = f"Résumé du match {match_id}"
        else:
            headline = f"Match recap: {match_id}"
        return MatchRecap(match_id=match_id, profile_id=profile.profile_id, locale=profile.locale,
                          headline=headline, bullets=[x.body for x in selected] or ["No notable moments were generated."],
                          evidence_event_ids=sorted({eid for x in selected for eid in x.evidence.event_ids}), final_state=state)
