"""Analyst, fan, and broadcaster experience checks for the same evidence."""

from __future__ import annotations

from matchpulse.agents import PersonalizationAgent, SupervisorAgent
from matchpulse.models import AudienceProfile, DetailLevel, EventType, MatchEvent, Persona

AGENT = PersonalizationAgent()


def profile(profile_id: str, persona: str, **kwargs) -> AudienceProfile:
    return AudienceProfile(profile_id=profile_id, persona=persona, locale=kwargs.pop("locale", "en"), **kwargs)


def shot_event() -> MatchEvent:
    return MatchEvent(match_id="p-1", sequence=1, clock_ms=1_000, event_type=EventType.SHOT,
                      team_id="Northbridge-FC", player_id="NB-9", outcome="on_target", speed_kmh=118)


def test_one_event_produces_three_distinct_experiences():
    pipeline = SupervisorAgent()
    profiles = [profile("analyst", "analyst"), profile("fan", "fan"), profile("broadcaster", "broadcaster")]
    _, outputs, warnings, _ = pipeline.process(shot_event(), profiles)
    assert not warnings
    assert len(outputs) == 3
    assert len({item.body for item in outputs}) == 3
    assert len({item.overlay_text for item in outputs}) == 3
    assert {item.persona for item in outputs} == {"analyst", "fan", "broadcaster"}
    assert len({tuple(item.evidence.event_ids) for item in outputs}) == 1


def test_every_personalized_output_keeps_the_same_evidence():
    pipeline = SupervisorAgent()
    profiles = [profile("analyst", "analyst"), profile("fan", "fan"), profile("broadcaster", "broadcaster")]
    event = shot_event()
    _, outputs, _, _ = pipeline.process(event, profiles)
    for item in outputs:
        assert item.evidence.event_ids == [event.event_id]
        assert item.evidence.formula
        assert item.stats["speed_kmh"] == 118
        assert item.stats["on_target"] == 1


def test_favorite_club_and_player_raise_priority():
    neutral = profile("neutral", "fan")
    supporter = profile("supporter", "fan", favorite_club="Northbridge-FC", favorite_player="NB-9")
    event = shot_event()
    pipeline = SupervisorAgent()
    _, outputs, _, _ = pipeline.process(event, [neutral, supporter])
    priority = {item.profile_id: item.priority for item in outputs}
    assert priority["supporter"] > priority["neutral"]
    assert 0.0 <= priority["supporter"] <= 1.0


def test_metric_interest_raises_priority():
    uninterested = profile("plain", "fan")
    interested = profile("interested", "fan", metric_interests=["shooting"])
    pipeline = SupervisorAgent()
    _, outputs, _, _ = pipeline.process(shot_event(), [uninterested, interested])
    priority = {item.profile_id: item.priority for item in outputs}
    assert priority["interested"] > priority["plain"]


def test_deep_analyst_detail_exposes_the_evidence_formula():
    pipeline = SupervisorAgent()
    profiles = [profile("analyst", "analyst", detail=DetailLevel.DEEP)]
    _, outputs, _, _ = pipeline.process(shot_event(), profiles)
    assert "Evidence:" in outputs[0].body
    assert outputs[0].evidence.formula in outputs[0].body


def test_compact_broadcaster_detail_collapses_to_the_overlay():
    pipeline = SupervisorAgent()
    profiles = [profile("broadcaster", "broadcaster", detail=DetailLevel.COMPACT)]
    _, outputs, _, _ = pipeline.process(shot_event(), profiles)
    assert outputs[0].body == outputs[0].overlay_text


def test_broadcaster_priority_drops_for_routine_information():
    from matchpulse.models import Evidence, Insight

    info = Insight(insight_id="i", match_id="p-2", sequence=1, clock_ms=1, kind="tackle", severity="info",
                   stats={"field_x": 50.0}, evidence=Evidence(event_ids=["e1"], inputs={"x": 50}, formula="x", confidence=.5))
    broadcaster = profile("broadcaster", "broadcaster")
    fan = profile("fan", "fan")
    assert AGENT.relevance(info, broadcaster) < AGENT.relevance(info, fan)


def test_relevance_is_bounded_for_every_persona():
    from matchpulse.models import Evidence, Insight

    major = Insight(insight_id="i", match_id="p-3", sequence=1, clock_ms=1, kind="goal", severity="major",
                    team_id="Northbridge-FC", player_id="NB-9", stats={"score": "1"},
                    evidence=Evidence(event_ids=["e1"], inputs={}, formula="f", confidence=.9))
    favourite = profile("favourite", "fan", favorite_club="Northbridge-FC", favorite_player="NB-9",
                        metric_interests=["shooting"])
    for target in (profile("analyst", "analyst"), profile("fan", "fan"), profile("broadcaster", "broadcaster"), favourite):
        score = AGENT.relevance(major, target)
        assert 0.0 <= score <= 1.0
        assert isinstance(target.persona, Persona)
