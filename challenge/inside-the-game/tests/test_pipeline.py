from matchpulse.agents import SupervisorAgent
from matchpulse.models import AudienceProfile, EventType, MatchEvent


def profile(profile_id: str, persona: str, locale: str):
    return AudienceProfile(profile_id=profile_id, persona=persona, locale=locale)


def test_same_evidence_becomes_distinct_multilingual_experiences():
    pipeline = SupervisorAgent()
    profiles = [profile("analyst", "analyst", "en"), profile("fan", "fan", "fr"), profile("tv", "broadcaster", "es")]
    event = MatchEvent(match_id="match-1", sequence=1, clock_ms=1000, event_type=EventType.SHOT,
                       team_id="Northbridge-FC", player_id="NB-9", outcome="on_target", speed_kmh=111)
    applied, outputs, warnings, handoffs = pipeline.process(event, profiles)
    assert not warnings
    assert applied.state.event_count == 1
    assert len(outputs) == 3
    assert {item.locale for item in outputs} == {"en", "fr", "es"}
    assert len({item.body for item in outputs}) == 3
    assert {eid for item in outputs for eid in item.evidence.event_ids} == {event.event_id}
    assert all(h.ok for h in handoffs)


def test_sustained_press_needs_three_grounded_events_in_30_seconds():
    pipeline = SupervisorAgent()
    p = [profile("fan", "fan", "en")]
    all_outputs = []
    for sequence, clock in enumerate((1000, 9000, 18_000), start=1):
        event = MatchEvent(match_id="match-2", sequence=sequence, clock_ms=clock, event_type=EventType.PRESSURE,
                           team_id="Riverside-Athletic", player_id="RA-6", pressure=0.9)
        _, outputs, _, _ = pipeline.process(event, p)
        all_outputs.extend(outputs)
    assert len(all_outputs) == 1
    assert all_outputs[0].stats["high_pressure_events_30s"] == 3
    assert len(all_outputs[0].evidence.event_ids) == 3


def test_duplicate_is_idempotent_and_out_of_order_is_rejected():
    pipeline = SupervisorAgent()
    p = [profile("fan", "fan", "en")]
    event = MatchEvent(match_id="match-3", sequence=2, clock_ms=5000, event_type=EventType.TACKLE,
                       team_id="Northbridge-FC", player_id="NB-8", outcome="success", x=80, y=50)
    first, _, _, _ = pipeline.process(event, p)
    duplicate, outputs, _, _ = pipeline.process(event, p)
    assert not first.duplicate
    assert duplicate.duplicate
    assert outputs == []
    assert duplicate.state.event_count == 1
