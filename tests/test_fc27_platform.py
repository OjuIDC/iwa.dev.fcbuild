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


def test_synthetic_provider_player_rating_determinism():
    import copy

    p1 = FC27SyntheticProvider()
    p2 = FC27SyntheticProvider()

    # 1. Identical inputs return identical ratings across calls
    env1_a = p1.generate_match_fixture(match_index=1, outcome="WIN")
    env1_b = p1.generate_match_fixture(match_index=1, outcome="WIN")
    norm1_a = normalize_match_envelope(env1_a)
    norm1_b = normalize_match_envelope(env1_b)
    ratings1_a = [p.match_rating for p in norm1_a.player_performances]
    ratings1_b = [p.match_rating for p in norm1_b.player_performances]
    assert ratings1_a == ratings1_b

    # 2. Results remain identical across independently constructed provider instances
    env1_inst2 = p2.generate_match_fixture(match_index=1, outcome="WIN")
    norm1_inst2 = normalize_match_envelope(env1_inst2)
    ratings_inst2 = [p.match_rating for p in norm1_inst2.player_performances]
    assert ratings1_a == ratings_inst2

    # 3. Ratings remain within expected bounds (between 4.0 and 10.0, step 0.1)
    for r in ratings1_a:
        assert 4.0 <= r <= 10.0
        assert round(r, 1) == r

    for idx in range(1, 10):
        for out in ("WIN", "LOSS", "DRAW"):
            env = p1.generate_match_fixture(match_index=idx, outcome=out)
            norm = normalize_match_envelope(env)
            for p in norm.player_performances:
                assert 4.0 <= p.match_rating <= 10.0
                assert round(p.match_rating, 1) == p.match_rating

    # 4. Changing player ID or match index can produce a different deterministic bucket
    env2 = p1.generate_match_fixture(match_index=2, outcome="WIN")
    norm2 = normalize_match_envelope(env2)
    ratings2 = [p.match_rating for p in norm2.player_performances]
    assert len(set(ratings1_a)) > 1
    assert ratings1_a != ratings2

    # 5. Input data (canonical roster) is not mutated
    roster_before = copy.deepcopy(p1.get_canonical_roster())
    _ = p1.generate_match_fixture(match_index=99)
    roster_after = p1.get_canonical_roster()
    assert roster_before == roster_after


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

def test_coaching_recommendations_generation(monkeypatch):
    monkeypatch.setenv("FC27_RECOMMENDATIONS_ENABLED", "true")
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


def test_coaching_recommendations_empty_matches_fails_safe(monkeypatch):
    monkeypatch.setenv("FC27_RECOMMENDATIONS_ENABLED", "true")
    recs = generate_coaching_recommendations([])
    assert len(recs) == 1
    assert recs[0].confidence == "PROVISIONAL"
    assert recs[0].minimum_sample_met is False
    assert "at least 3 to 5 matches" in recs[0].recommendation


@pytest.mark.parametrize(
    "env_val,expected",
    [
        (None, False),          # 1. Missing FC27_RECOMMENDATIONS_ENABLED means disabled
        ("false", False),       # 2. "false" means disabled
        ("0", False),           # 3. "0" means disabled
        ("no", False),          # 4. "no" means disabled
        ("true", True),         # 5. "true" means enabled
        ("1", True),            # 6. "1" means enabled
        ("yes", True),          # 7. "yes" means enabled
        ("True", True),         # 8. Mixed-case "True"
        ("YES", True),          # 8. Mixed-case "YES"
        ("TrUe", True),         # 8. Mixed-case "TrUe"
        ("False", False),       # 8. Mixed-case "False"
        ("No", False),          # 8. Mixed-case "No"
        ("random_val", False),  # 9. Unrecognized value fails closed
        ("", False),            # 9. Empty string fails closed
        ("2", False),           # 9. "2" fails closed
    ],
)
def test_coaching_recommendations_kill_switch_matrix(monkeypatch, env_val, expected):
    if env_val is None:
        monkeypatch.delenv("FC27_RECOMMENDATIONS_ENABLED", raising=False)
    else:
        monkeypatch.setenv("FC27_RECOMMENDATIONS_ENABLED", env_val)
    assert is_recommendations_enabled() is expected


def test_coaching_kill_switch_prevents_generation_by_default(monkeypatch):
    # 10. Disabling recommendations (by default without env var) prevents recommendation generation
    monkeypatch.delenv("FC27_RECOMMENDATIONS_ENABLED", raising=False)
    assert is_recommendations_enabled() is False
    provider = FC27SyntheticProvider()
    matches = provider.get_season_fixtures(count=5)
    recs_default = generate_coaching_recommendations(matches)
    assert recs_default == []

    # Disabling recommendations explicitly also prevents generation
    monkeypatch.setenv("FC27_RECOMMENDATIONS_ENABLED", "false")
    recs_explicit = generate_coaching_recommendations(matches)
    assert recs_explicit == []


def test_coaching_enabled_does_not_enable_prohibited_actions(monkeypatch):
    import os

    monkeypatch.setenv("FC27_RECOMMENDATIONS_ENABLED", "true")
    assert is_recommendations_enabled() is True

    # Enabling recommendations must not enable live ingestion, dispatch, or worker
    assert os.getenv("FC27_LIVE_INGESTION_ENABLED", "false").lower() in ("false", "0", "no", "")
    assert os.getenv("DISPATCH_ENABLED", "false").lower() in ("false", "0", "no", "")
    assert os.getenv("DISPATCH_WORKER_ENABLED", "false").lower() in ("false", "0", "no", "")
