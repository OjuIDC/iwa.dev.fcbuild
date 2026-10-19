"""English, Spanish, and French rendering checks for the same evidence."""

from __future__ import annotations

from matchpulse.agents import SupervisorAgent
from matchpulse.localization import render
from matchpulse.models import AudienceProfile, EventType, Insight, MatchEvent, Persona

EVIDENCE = {
    "event_ids": ["e1"],
    "inputs": {"speed_kmh": 118},
    "formula": "speed_kmh = measured velocity",
    "confidence": 0.92,
}
STATS = {"speed_kmh": 118, "on_target": 1}


def shot_insight() -> Insight:
    return Insight(
        insight_id="i-1", match_id="m-1", sequence=3, clock_ms=45_000, kind="shot_speed",
        severity="notable", team_id="Northbridge-FC", player_id="NB-9", stats=STATS, evidence=EVIDENCE,
    )


def profile(locale: str, persona: str = "fan") -> AudienceProfile:
    return AudienceProfile(profile_id=f"{persona}-{locale}", persona=persona, locale=locale)


def test_same_evidence_renders_three_languages():
    insight = shot_insight()
    en_title, en_body, en_overlay = render(insight, profile("en"))
    es_title, es_body, es_overlay = render(insight, profile("es"))
    fr_title, fr_body, fr_overlay = render(insight, profile("fr"))
    assert len({en_title, es_title, fr_title}) == 3
    assert len({en_body, es_body, fr_body}) == 3
    assert len({en_overlay, es_overlay, fr_overlay}) == 3
    assert "118" in en_body and "118" in es_body and "118" in fr_body


def test_spanish_and_french_use_their_own_vocabulary():
    insight = shot_insight()
    _, es_body, _ = render(insight, profile("es"))
    _, fr_body, _ = render(insight, profile("fr"))
    assert "dispara" in es_body
    assert "frappe" in fr_body


def test_persona_changes_the_wording_inside_a_language():
    insight = shot_insight()
    _, fan_body, _ = render(insight, profile("en", "fan"))
    _, analyst_body, _ = render(insight, profile("en", "analyst"))
    _, broadcaster_body, _ = render(insight, profile("en", "broadcaster"))
    assert len({fan_body, analyst_body, broadcaster_body}) == 3


def test_pipeline_emits_one_response_per_locale():
    pipeline = SupervisorAgent()
    profiles = [profile("en"), profile("es"), profile("fr")]
    event = MatchEvent(match_id="loc-1", sequence=1, clock_ms=1_000, event_type=EventType.SHOT,
                       team_id="Northbridge-FC", player_id="NB-9", outcome="on_target", speed_kmh=118)
    _, outputs, warnings, _ = pipeline.process(event, profiles)
    assert not warnings
    assert {item.locale for item in outputs} == {"en", "es", "fr"}
    assert len({item.body for item in outputs}) == 3
    assert all(item.locale in {"en", "es", "fr"} for item in outputs)


def test_recap_headline_follows_the_requested_locale():
    pipeline = SupervisorAgent()
    profiles = [profile("en"), profile("es"), profile("fr")]
    event = MatchEvent(match_id="loc-2", sequence=1, clock_ms=1_000, event_type=EventType.SHOT,
                       team_id="Northbridge-FC", player_id="NB-9", outcome="on_target", speed_kmh=118)
    pipeline.process(event, profiles)
    headlines = {locale: pipeline.recap("loc-2", profile(locale)).headline for locale in ("en", "es", "fr")}
    assert headlines["es"].startswith("Resumen del partido")
    assert headlines["fr"].startswith("Résumé du match")
    assert headlines["en"].startswith("Match recap")
    assert len(set(headlines.values())) == 3


def test_unknown_locale_falls_back_to_english_without_raising():
    insight = shot_insight()
    _, en_body, _ = render(insight, profile("en"))
    broken = AudienceProfile(profile_id="xx", persona=Persona.FAN, locale="en")
    broken.locale = "de"
    _, fallback_body, _ = render(insight, broken)
    assert fallback_body == en_body
