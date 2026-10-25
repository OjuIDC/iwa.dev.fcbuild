"""Comprehensive Test Suite for FC 27 East Coasters Platform.

Tests versioned schemas, fail-closed validation, synthetic provider adapter,
functional analytics core, and evidence-grounded coaching layer.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.fc27_analytics import (
    analyze_partnerships,
    calculate_player_contribution,
    calculate_team_balance,
    determine_sample_size_tier,
    evaluate_build_suitability,
    evaluate_formation_performance,
    generate_team_snapshot,
)
from src.fc27_coaching import (
    generate_coaching_recommendations,
    is_recommendations_enabled,
)
from src.fc27_provider import (
    FC27ProviderError,
    FC27SyntheticProvider,
    compute_payload_hash,
    create_envelope,
    normalize_match_envelope,
)
from src.schemas import (
    ClubMatch,
    EastCoastersRecommendation,
    Lineup,
    PlayerBuild,
    PlayerMatch,
    ProviderEnvelope,
    TeamSnapshot,
)


# ==============================================================================
# Phase 3: Versioned Contracts & Fail-Closed Validation
# ==============================================================================

def test_player_match_valid():
    pm = PlayerMatch(
        match_id="m1",
        player_id="p1",
        position="CAM",
        role="Playmaker",
        minutes_played=90,
        match_rating=8.5,
        goals=1,
        assists=2,
        passes_attempted=40,
        passes_completed=36,
        tackles_attempted=4,
        tackles_won=3,
    )
    assert pm.player_id == "p1"
    assert pm.pass_completion_rate == 90.0
    assert pm.tackle_success_rate == 75.0


def test_player_match_rejects_unknown_position():
    with pytest.raises(ValidationError, match="unknown FC 27 position"):
        PlayerMatch(
            match_id="m1",
            player_id="p1",
            position="WATERBOY",  # Unknown position
            role="Playmaker",
            minutes_played=90,
            match_rating=7.0,
        )


def test_player_match_rejects_unknown_role():
    with pytest.raises(ValidationError, match="unknown FC 27 role"):
        PlayerMatch(
            match_id="m1",
            player_id="p1",
            position="ST",
            role="Chef",  # Unknown role
            minutes_played=90,
            match_rating=7.0,
        )


def test_player_match_rejects_extra_fields():
    with pytest.raises(ValidationError):
        PlayerMatch(
            match_id="m1",
            player_id="p1",
            position="ST",
            role="Advanced Forward",
            minutes_played=90,
            match_rating=7.0,
            unauthorized_extra_field="malicious_payload",
        )


def test_player_build_validation():
    build = PlayerBuild(
        build_id="b1",
        player_id="p1",
        position="ST",
        primary_role="Advanced Forward",
        height_cm=185,
        weight_kg=78,
        archetype="Sniper",
        attributes={"pace": 88, "shooting": 85, "dribbling": 82, "passing": 75, "defending": 40, "physical": 76},
        playstyles=["Finesse Shot", "Quick Step"],
    )
    assert build.height_cm == 185
    assert build.attributes["pace"] == 88


def test_player_build_rejects_out_of_bounds_attributes():
    with pytest.raises(ValidationError, match="out of bounds"):
        PlayerBuild(
            build_id="b1",
            player_id="p1",
            position="ST",
            primary_role="Advanced Forward",
            height_cm=185,
            weight_kg=78,
            archetype="Sniper",
            attributes={"pace": 150},  # Over 99
        )


def test_club_match_rejects_invalid_formation():
    with pytest.raises(ValidationError, match="unknown FC 27 formation"):
        ClubMatch(
            match_id="m1",
            opponent_club_id="opp1",
            opponent_club_name="Rival FC",
            match_timestamp="2026-10-24T20:00:00Z",
            goals_for=2,
            goals_against=1,
            result="WIN",
            formation="1-1-8",  # Invalid formation
            lineup=["p1", "p2"],
        )


# ==============================================================================
# Phase 4: Provider Adapter & Ingestion Envelope Tests
# ==============================================================================

def test_envelope_creation_and_hash_integrity():
    raw = {"match_id": "test_m1", "goals_for": 3, "goals_against": 1}
    env = create_envelope(
        provider="synthetic_fc27",
        provider_version="1.0.0",
        raw_payload=raw,
        source_reference="test://ref1",
    )
    assert env.provider == "synthetic_fc27"
    assert env.data_quality_status == "VERIFIED"
    assert env.payload_hash == compute_payload_hash(raw)


def test_envelope_tamper_detection():
    provider = FC27SyntheticProvider()
    env = provider.generate_match_fixture(1)

    # Tamper with the raw payload without re-hashing
    tampered_payload = dict(env.payload)
    tampered_payload["goals_for"] = 99
    tampered_env = env.model_copy(update={"payload": tampered_payload})

    with pytest.raises(FC27ProviderError, match="payload hash mismatch"):
        normalize_match_envelope(tampered_env)


def test_rejected_envelope_fails_closed():
    provider = FC27SyntheticProvider()
    env = provider.generate_match_fixture(1)
    rejected_env = env.model_copy(update={"data_quality_status": "REJECTED"})

    with pytest.raises(FC27ProviderError, match="cannot normalize rejected envelope"):
        normalize_match_envelope(rejected_env)


def test_synthetic_provider_normalization():
    provider = FC27SyntheticProvider()
    matches = provider.get_season_fixtures(count=5)
    assert len(matches) == 5
    for m in matches:
        assert m.club_id == "330323"
        assert len(m.player_performances) == 10
        assert m.provider_provenance is not None


# ==============================================================================
# Phase 5: Functional Analytics Core Tests
# ==============================================================================

def test_player_contribution_pure_calculation():
    pm1 = PlayerMatch(
        match_id="m1", player_id="p_batmanL8", position="CAM", role="Playmaker",
        minutes_played=90, match_rating=8.2, goals=1, assists=1,
        passes_attempted=40, passes_completed=36, tackles_attempted=2, tackles_won=2,
    )
    pm2 = PlayerMatch(
        match_id="m2", player_id="p_batmanL8", position="CAM", role="Playmaker",
        minutes_played=90, match_rating=8.8, goals=0, assists=2,
        passes_attempted=50, passes_completed=45, tackles_attempted=3, tackles_won=2,
    )
    stats = calculate_player_contribution([pm1, pm2])
    assert stats["matches_analyzed"] == 2
    assert stats["avg_match_rating"] == 8.5
    assert stats["pass_accuracy_pct"] == 90.0
    assert stats["sample_size_tier"] == "INSUFFICIENT_DATA"


def test_sample_size_tier_thresholds():
    assert determine_sample_size_tier(0) == "INSUFFICIENT_DATA"
    assert determine_sample_size_tier(2) == "INSUFFICIENT_DATA"
    assert determine_sample_size_tier(3) == "EXPLORATORY"
    assert determine_sample_size_tier(4) == "EXPLORATORY"
    assert determine_sample_size_tier(5) == "LOW_CONFIDENCE"
    assert determine_sample_size_tier(9) == "LOW_CONFIDENCE"
    assert determine_sample_size_tier(10) == "MODERATE_CONFIDENCE"
    assert determine_sample_size_tier(19) == "MODERATE_CONFIDENCE"
    assert determine_sample_size_tier(20) == "HIGH_CONFIDENCE"


def test_build_suitability_evaluation():
    build = PlayerBuild(
        build_id="b1", player_id="p1", position="CDM", primary_role="Anchor",
        height_cm=186, weight_kg=82, archetype="Tower",
        attributes={"defending": 88, "physical": 85, "passing": 76, "pace": 68},
        playstyles=["Block", "Bruiser"],
    )
    eval_res = evaluate_build_suitability(build, "Anchor")
    assert eval_res["suitability_score"] >= 75.0
    # Anticipate recommendation triggered
    assert any("Anticipate" in r for r in eval_res["recommendations"])


def test_formation_evaluation_aggregation():
    provider = FC27SyntheticProvider()
    matches = provider.get_season_fixtures(count=8)
    res = evaluate_formation_performance(matches)
    assert len(res) >= 2
    for form, f_data in res.items():
        assert f_data["match_count"] > 0
        assert "win_rate_pct" in f_data
        assert "points_per_game" in f_data


def test_team_snapshot_generation():
    provider = FC27SyntheticProvider()
    matches = provider.get_season_fixtures(count=6)
    snap = generate_team_snapshot(matches, window_name="Season Opener")
    assert snap.club_id == "330323"
    assert snap.match_count == 6
    assert snap.sample_size_tier == "LOW_CONFIDENCE"
    assert len(snap.strengths) > 0


# ==============================================================================
# Phase 6: East Coasters Coaching Engine Tests
# ==============================================================================

def test_coaching_recommendations_generation():
    provider = FC27SyntheticProvider()
    matches = provider.get_season_fixtures(count=8)
    builds = {
        "p_striker99": PlayerBuild(
            build_id="b_st", player_id="p_striker99", position="ST", primary_role="Advanced Forward",
            height_cm=182, weight_kg=76, archetype="Finisher",
            attributes={"pace": 86, "shooting": 88, "dribbling": 80, "physical": 72},
            playstyles=["Finesse Shot"],
        )
    }
    recs = generate_coaching_recommendations(matches, builds)
    assert len(recs) >= 2
    for r in recs:
        assert r.club_id == "330323"
        assert len(r.empirical_observations) >= 1
        assert len(r.heuristics) >= 1
        assert r.confidence in ("PROVISIONAL", "EXPLORATORY", "LOW", "MODERATE", "HIGH")
        assert r.formula_version == "fc27-v1.0"


def test_coaching_recommendations_empty_matches_fails_safe():
    recs = generate_coaching_recommendations([])
    assert len(recs) == 1
    assert recs[0].confidence == "PROVISIONAL"
    assert recs[0].minimum_sample_met is False
    assert "at least 3 to 5 matches" in recs[0].recommendation


def test_coaching_kill_switch_behavior(monkeypatch):
    monkeypatch.setenv("FC27_RECOMMENDATIONS_ENABLED", "false")
    assert is_recommendations_enabled() is False
    provider = FC27SyntheticProvider()
    matches = provider.get_season_fixtures(count=5)
    recs = generate_coaching_recommendations(matches)
    assert recs == []
