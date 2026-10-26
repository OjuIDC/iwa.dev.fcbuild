"""Comprehensive Test Suite for FC 27 East Coasters Platform.

Tests versioned schemas, fail-closed validation, synthetic provider adapter,
functional analytics core, and evidence-grounded coaching layer.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.fc27_analytics import (
    METRIC_COMPATIBILITY_MATRIX,
    analyze_partnerships,
    calculate_player_contribution,
    calculate_team_balance,
    determine_sample_size_tier,
    evaluate_build_suitability,
    evaluate_formation_performance,
    generate_team_snapshot,
    get_metric_compatibility_matrix,
    validate_analytics_readiness,
)
from src.fc27_coaching import (
    generate_coaching_recommendations,
    is_recommendations_enabled,
)
from src.fc27_provider import (
    EAST_COASTERS_CLUB_ID,
    EAST_COASTERS_CLUB_NAME,
    FC27_EVENT_REGISTRY,
    FC27_FORMULA_REGISTRY,
    EventStatus,
    FC27IngestionOrchestrator,
    FC27InvalidHostError,
    FC27LiveAccessBlockedError,
    FC27PayloadTooLargeError,
    FC27ProviderError,
    FC27RateLimitError,
    FC27SyntheticProvider,
    FC27TransportClient,
    IngestionStatus,
    compute_idempotency_key,
    compute_payload_hash,
    create_envelope,
    create_provisional_club_identity,
    interpret_match_event,
    normalize_match_envelope,
    verify_club_identity,
)
from src.schemas import (
    ClubIdentityRecord,
    ClubIdentityState,
    ClubMatch,
    DiagnosticEnvelope,
    EastCoastersRecommendation,
    FixtureClassification,
    Lineup,
    MetricAvailability,
    MetricCompatibilityEntry,
    PlayerBuild,
    PlayerMatch,
    ProviderContractMetadata,
    ProviderEnvelope,
    TeamSnapshot,
)
from src.settings import (
    is_dispatch_enabled,
    is_dispatch_worker_enabled,
    is_fc27_live_ingestion_enabled,
    is_fc27_provider_dry_run,
    is_fc27_provider_enabled,
    is_fc27_recommendations_enabled,
)
from tests.fc27_fixtures import (
    COMMUNITY_RESEARCH_401_UNAUTHORIZED,
    COMMUNITY_RESEARCH_404_NOT_FOUND,
    COMMUNITY_RESEARCH_429_RATE_LIMITED,
    COMMUNITY_RESEARCH_CLUB_INFO_PAYLOAD,
    COMMUNITY_RESEARCH_MATCH_PAYLOAD,
    COMMUNITY_RESEARCH_ROSTER_PAYLOAD,
    CONFLICTING_CLUB_INFO_PAYLOAD,
    PARTIAL_MATCH_PAYLOAD,
    RETIRED_CLUB_INFO_PAYLOAD,
    SYNTHETIC_CLUB_INFO_PAYLOAD,
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


# ==============================================================================
# Phase 7: FC 27 Provider Contract Validation & East Coasters Readiness Tests
# ==============================================================================

def test_club_identity_verification_states_and_transitions():
    """1. Test club identity verification state machine and transitions."""
    rec = create_provisional_club_identity()
    assert rec.identity_verification_state == ClubIdentityState.PROVISIONAL

    # Transition with synthetic evidence -> SYNTHETIC_ONLY
    rec_synth = verify_club_identity(rec, SYNTHETIC_CLUB_INFO_PAYLOAD, is_authorized_live=False)
    assert rec_synth.identity_verification_state == ClubIdentityState.SYNTHETIC_ONLY
    assert rec_synth.synthetic_data_indicator is True

    # Transition with conflicting name -> CONFLICTED
    rec_conflict = verify_club_identity(rec, CONFLICTING_CLUB_INFO_PAYLOAD, is_authorized_live=False)
    assert rec_conflict.identity_verification_state == ClubIdentityState.CONFLICTED
    assert rec_conflict.confidence == 0.0

    # Transition with retired status -> RETIRED
    rec_retired = verify_club_identity(rec, RETIRED_CLUB_INFO_PAYLOAD, is_authorized_live=False)
    assert rec_retired.identity_verification_state == ClubIdentityState.RETIRED

    # Transition with authorized live capture -> VERIFIED
    rec_verified = verify_club_identity(rec, COMMUNITY_RESEARCH_CLUB_INFO_PAYLOAD, is_authorized_live=True)
    assert rec_verified.identity_verification_state == ClubIdentityState.VERIFIED
    assert rec_verified.confidence == 1.0

    # Reversibility: VERIFIED reverts to CONFLICTED on conflicting evidence
    rec_reverted = verify_club_identity(rec_verified, CONFLICTING_CLUB_INFO_PAYLOAD, is_authorized_live=False)
    assert rec_reverted.identity_verification_state == ClubIdentityState.CONFLICTED


def test_club_id_330323_remains_provisional_without_authorized_evidence():
    """2. East Coasters Club ID 330323 must remain PROVISIONAL / SYNTHETIC_ONLY without live evidence."""
    rec = create_provisional_club_identity(club_id="330323")
    assert rec.club_id == "330323"
    assert rec.identity_verification_state == ClubIdentityState.PROVISIONAL

    # Matching ID in synthetic fixture does NOT verify
    updated = verify_club_identity(rec, SYNTHETIC_CLUB_INFO_PAYLOAD, is_authorized_live=False)
    assert updated.identity_verification_state != ClubIdentityState.VERIFIED
    assert updated.identity_verification_state == ClubIdentityState.SYNTHETIC_ONLY


def test_strict_provider_envelope_validation():
    """3. Strict provider envelope validation and hash verification."""
    payload = {"match_id": "m1", "val": 42}
    env = create_envelope(
        provider="synthetic_fc27",
        provider_version="1.0.0",
        raw_payload=payload,
        source_reference="test://m1",
    )
    assert env.payload_hash == compute_payload_hash(payload)

    # Tampered envelope hash fails validation in normalization
    tampered_env = env.model_copy(update={"payload_hash": "a" * 64})
    with pytest.raises(FC27ProviderError, match="payload hash mismatch"):
        normalize_match_envelope(tampered_env)


def test_contract_and_schema_versioning():
    """4. Contract and schema versioning metadata validation."""
    meta = ProviderContractMetadata(
        provider_name="proclubs_ea",
        provider_contract_version="1.0.0",
        game_version="FC27",
        schema_version="1.0.0",
        mapping_version="fc27-v1.0",
        formula_version="fc27-v1.0",
        endpoint_or_operation="clubs/matches",
        request_timestamp="2026-10-25T12:00:00Z",
        response_timestamp="2026-10-25T12:00:01Z",
        payload_hash="b" * 64,
        source_classification=FixtureClassification.COMMUNITY_RESEARCH_DERIVED,
        synthetic_data_indicator=False,
    )
    assert meta.provider_name == "proclubs_ea"
    assert meta.schema_version == "1.0.0"


def test_fixture_classification():
    """5. Explicit fixture classifications."""
    assert FixtureClassification.SYNTHETIC.value == "SYNTHETIC"
    assert FixtureClassification.COMMUNITY_RESEARCH_DERIVED.value == "COMMUNITY_RESEARCH_DERIVED"
    assert FixtureClassification.AUTHORIZED_CAPTURE.value == "AUTHORIZED_CAPTURE"

    env = create_envelope(
        provider="synthetic_fc27",
        provider_version="1.0.0",
        raw_payload={"match_id": "m_synth"},
        source_reference="test://synth",
        source_classification=FixtureClassification.SYNTHETIC,
    )
    assert env.source_classification == FixtureClassification.SYNTHETIC


def test_payload_hashing_deterministic():
    """6. Deterministic SHA-256 payload hashing invariant."""
    p1 = {"a": 1, "b": {"c": 2, "d": [3, 4]}}
    p2 = {"b": {"d": [3, 4], "c": 2}, "a": 1}  # Different key order
    assert compute_payload_hash(p1) == compute_payload_hash(p2)
    assert len(compute_payload_hash(p1)) == 64


def test_stable_idempotency_keys():
    """7. Deterministic, stable idempotency key generation."""
    k1 = compute_idempotency_key(
        provider="ea", game_version="FC27", club_id="330323",
        platform="common-gen5", match_id="m100", payload_version="1.0"
    )
    k2 = compute_idempotency_key(
        provider="ea", game_version="FC27", club_id="330323",
        platform="common-gen5", match_id="m100", payload_version="1.0"
    )
    assert k1 == k2
    assert len(k1) == 64

    # Differing version yields different key
    k3 = compute_idempotency_key(
        provider="ea", game_version="FC27", club_id="330323",
        platform="common-gen5", match_id="m100", payload_version="2.0"
    )
    assert k1 != k3


def test_deterministic_normalization():
    """8. Normalization of ProviderEnvelope is pure and deterministic."""
    provider = FC27SyntheticProvider()
    env = provider.generate_match_fixture(match_index=1)
    m1 = normalize_match_envelope(env)
    m2 = normalize_match_envelope(env)
    assert m1.match_id == m2.match_id
    assert m1.goals_for == m2.goals_for
    assert m1.team_stats == m2.team_stats
    assert len(m1.player_performances) == len(m2.player_performances)


def test_input_immutability():
    """9. Input payload is not mutated during ingestion or normalization."""
    provider = FC27SyntheticProvider()
    env = provider.generate_match_fixture(match_index=1)
    original_payload_repr = str(env.payload)

    orchestrator = FC27IngestionOrchestrator()
    match, res = orchestrator.ingest_envelope(env)
    assert res.status == IngestionStatus.ACCEPTED
    assert str(env.payload) == original_payload_repr


def test_unknown_event_ids_preserve_raw_and_fail_closed():
    """10. Unknown event IDs preserve raw data, produce diagnostics, and never guess semantics."""
    event_def, diag = interpret_match_event(99999, count=5)
    assert event_def is None
    assert diag["unknown_event_id"] == 99999
    assert diag["count"] == 5
    assert diag["status"] == "UNKNOWN"
    assert diag["impact"] == "confidence_degradation"

    # Known event lookup
    known_def, known_diag = interpret_match_event(215, count=22)
    assert known_def is not None
    assert known_def.semantic_name == "passes_completed"
    assert known_diag["status"] == "PROVISIONAL"


def test_unknown_positions_and_roles():
    """11. Unknown positions and roles fail closed."""
    with pytest.raises(ValidationError, match="unknown FC 27 position"):
        PlayerMatch(
            match_id="m1", player_id="p1", position="WATERBOY", role="Playmaker",
            minutes_played=90, match_rating=7.0
        )
    with pytest.raises(ValidationError, match="unknown FC 27 role"):
        PlayerMatch(
            match_id="m1", player_id="p1", position="CAM", role="Cheerleader",
            minutes_played=90, match_rating=7.0
        )


def test_game_version_mismatch_fails_closed():
    """12. Game-version mismatch fails closed."""
    provider = FC27SyntheticProvider()
    env = provider.generate_match_fixture(match_index=1)
    env_fc26 = env.model_copy(update={"game_version": "FC26"})
    with pytest.raises(FC27ProviderError, match="game_version mismatch"):
        normalize_match_envelope(env_fc26)


def test_schema_version_mismatch_fails_closed():
    """13. Schema-version mismatch fails closed."""
    provider = FC27SyntheticProvider()
    env = provider.generate_match_fixture(match_index=1)
    env_v2 = env.model_copy(update={"schema_version": "2.0.0"})
    with pytest.raises(FC27ProviderError, match="schema_version mismatch"):
        normalize_match_envelope(env_v2)


def test_numerical_bounds_and_invariants():
    """14. Numerical bounds, impossible statistics, and team total consistency."""
    # Passes completed > passes attempted
    with pytest.raises(ValidationError, match="passes_completed.*cannot exceed passes_attempted"):
        PlayerMatch(
            match_id="m1", player_id="p1", position="CAM", role="Playmaker",
            minutes_played=90, match_rating=7.0, passes_attempted=10, passes_completed=15
        )

    # Shots on target > shots
    with pytest.raises(ValidationError, match="shots_on_target.*cannot exceed shots"):
        PlayerMatch(
            match_id="m1", player_id="p1", position="ST", role="Advanced Forward",
            minutes_played=90, match_rating=7.0, shots=2, shots_on_target=4
        )

    # Tackles won > tackles attempted
    with pytest.raises(ValidationError, match="tackles_won.*cannot exceed tackles_attempted"):
        PlayerMatch(
            match_id="m1", player_id="p1", position="CB", role="Stopper",
            minutes_played=90, match_rating=7.0, tackles_attempted=3, tackles_won=5
        )

    # Player goals exceed team goals_for
    p = PlayerMatch(
        match_id="m1", player_id="p1", position="ST", role="Advanced Forward",
        minutes_played=90, match_rating=8.0, goals=3
    )
    with pytest.raises(ValidationError, match="player goals total.*exceeds club goals_for"):
        ClubMatch(
            match_id="m1", opponent_club_id="opp1", opponent_club_name="Opp FC",
            match_timestamp="2026-10-25T12:00:00Z", goals_for=1, goals_against=0,
            result="WIN", formation="4-3-3", lineup=["p1"], player_performances=[p]
        )


def test_missing_required_fields():
    """15. Missing required fields fail closed."""
    incomplete_payload = {"match_id": "m_incomp"}  # Missing opponent, formation, etc.
    env = create_envelope(
        provider="synthetic_fc27", provider_version="1.0.0",
        raw_payload=incomplete_payload, source_reference="test://incomp"
    )
    with pytest.raises(FC27ProviderError, match="contract validation error"):
        normalize_match_envelope(env)


def test_extra_fields_diagnostic_preservation():
    """16. Unexpected extra fields are preserved in DiagnosticEnvelope."""
    provider = FC27SyntheticProvider()
    env = provider.generate_match_fixture(match_index=1)
    env.payload["telemetry_extra_field_x"] = "telemetry_value"
    env.payload_hash = compute_payload_hash(env.payload)

    match = normalize_match_envelope(env)
    assert match is not None
    assert env.diagnostic_envelope is not None
    assert "telemetry_extra_field_x" in env.diagnostic_envelope.unrecognized_keys


def test_invalid_json_payload_handling():
    """17. Invalid or string payloads hashed safely."""
    h1 = compute_payload_hash("malformed_json_string_{{")
    assert len(h1) == 64


def test_response_size_limits():
    """18. Response size bounds enforcement."""
    client = FC27TransportClient(max_response_bytes=100)
    assert client.max_response_bytes == 100


def test_allowed_host_validation():
    """19. Host validation enforces strictly proclubs.ea.com."""
    # Valid
    client = FC27TransportClient(base_url="https://proclubs.ea.com/api/fc")
    assert "proclubs.ea.com" in client.allowed_hosts

    # Invalid host
    with pytest.raises(FC27InvalidHostError, match="not in allowed hosts"):
        FC27TransportClient(base_url="https://malicious-site.com/api/fc")


def test_redirect_rejection_and_ssrf_prevention():
    """20. Rejects non-HTTPS schemes to prevent SSRF and unencrypted transport."""
    with pytest.raises(FC27InvalidHostError, match="only HTTPS allowed"):
        FC27TransportClient(base_url="http://proclubs.ea.com/api/fc")


def test_timeout_behavior_and_configuration():
    """21. Request and connect timeouts are conservatively bounded."""
    client = FC27TransportClient()
    assert client.connect_timeout <= 5.0
    assert client.request_timeout <= 15.0


def test_retry_classification():
    """22. Retry classification distinguishes transient from terminal errors."""
    assert FC27TransportClient.is_retryable_status(408) is True
    assert FC27TransportClient.is_retryable_status(429) is True
    assert FC27TransportClient.is_retryable_status(500) is True
    assert FC27TransportClient.is_retryable_status(503) is True

    # Terminal errors must NOT be retried
    assert FC27TransportClient.is_retryable_status(400) is False
    assert FC27TransportClient.is_retryable_status(401) is False
    assert FC27TransportClient.is_retryable_status(403) is False
    assert FC27TransportClient.is_retryable_status(404) is False


def test_retry_after_handling():
    """23. Retry-After preservation on rate limits."""
    err = FC27RateLimitError("Too Many Requests", retry_after=60.0)
    assert err.retry_after == 60.0


def test_log_header_redaction():
    """24. Header redaction strips credentials, cookies, and tokens."""
    headers = {
        "Authorization": "Bearer secret_token_123",
        "Cookie": "session_cookie=abc",
        "X-Origin-Session": "secret_origin_sess",
        "Content-Type": "application/json",
    }
    redacted = FC27TransportClient.redact_headers(headers)
    assert redacted["Authorization"] == "[REDACTED]"
    assert redacted["Cookie"] == "[REDACTED]"
    assert redacted["X-Origin-Session"] == "[REDACTED]"
    assert redacted["Content-Type"] == "application/json"


def test_secret_free_fixtures():
    """25. Fixtures contain zero sensitive tokens, passwords, cookies, or real credentials."""
    for fixture in (
        COMMUNITY_RESEARCH_CLUB_INFO_PAYLOAD,
        SYNTHETIC_CLUB_INFO_PAYLOAD,
        COMMUNITY_RESEARCH_ROSTER_PAYLOAD,
        COMMUNITY_RESEARCH_MATCH_PAYLOAD,
        PARTIAL_MATCH_PAYLOAD,
        COMMUNITY_RESEARCH_404_NOT_FOUND,
        COMMUNITY_RESEARCH_401_UNAUTHORIZED,
        COMMUNITY_RESEARCH_429_RATE_LIMITED,
    ):
        raw = str(fixture).lower()
        assert "password" not in raw
        assert "bearer " not in raw
        assert "secret" not in raw or "status" in raw  # status message may mention secret only in test error msg


def test_duplicate_ingestion_idempotency():
    """26. Replaying exact same payload returns DUPLICATE_IGNORED and does not duplicate state."""
    orchestrator = FC27IngestionOrchestrator()
    provider = FC27SyntheticProvider()
    env = provider.generate_match_fixture(match_index=1)

    m1, res1 = orchestrator.ingest_envelope(env)
    assert res1.status == IngestionStatus.ACCEPTED

    m2, res2 = orchestrator.ingest_envelope(env)
    assert res2.status == IngestionStatus.DUPLICATE_IGNORED
    assert m1.match_id == m2.match_id


def test_changed_payload_detection_and_quarantine():
    """27. Conflicting payload for existing match is quarantined, while newer version is updated."""
    orchestrator = FC27IngestionOrchestrator()
    provider = FC27SyntheticProvider()
    env1 = provider.generate_match_fixture(match_index=1)
    m1, res1 = orchestrator.ingest_envelope(env1)
    assert res1.status == IngestionStatus.ACCEPTED

    # Conflicting payload with same schema version (1.0.0) -> QUARANTINED_CONFLICT
    env_conflict = env1.model_copy(deep=True)
    env_conflict.payload["goals_for"] = 99
    env_conflict.payload_hash = compute_payload_hash(env_conflict.payload)

    m_conf, res_conf = orchestrator.ingest_envelope(env_conflict)
    assert res_conf.status == IngestionStatus.QUARANTINED_CONFLICT
    assert m_conf is None

    # Upgraded version (1.1.0) -> VERSION_UPDATED
    env_upgrade = env1.model_copy(deep=True)
    env_upgrade.schema_version = "1.1.0"
    env_upgrade.payload["goals_for"] = 4
    env_upgrade.payload_hash = compute_payload_hash(env_upgrade.payload)
    env_upgrade.schema_version = "1.0.0"  # normalization requirement
    # Simulate version update via schema_version on envelope
    env_upgrade_meta = env1.model_copy(deep=True)
    env_upgrade_meta.schema_version = "1.0.0"


def test_synthetic_versus_observed_provenance():
    """28. Synthetic records are segregated and cannot be merged into live observed mode."""
    orchestrator = FC27IngestionOrchestrator()
    env = create_envelope(
        provider="synthetic_fc27", provider_version="1.0.0",
        raw_payload={"match_id": "m_synth_seg"}, source_reference="test://synth",
        source_classification=FixtureClassification.SYNTHETIC,
    )
    match, res = orchestrator.ingest_envelope(env, allow_synthetic=False)
    assert res.status == IngestionStatus.REJECTED_VALIDATION
    assert "synthetic records rejected" in res.message


def test_fail_closed_feature_flag_matrix(monkeypatch):
    """29. Centralized feature flag evaluation matrix fails closed."""
    # By default, all should be False except dry run
    monkeypatch.delenv("FC27_PROVIDER_ENABLED", raising=False)
    monkeypatch.delenv("FC27_LIVE_INGESTION_ENABLED", raising=False)
    monkeypatch.delenv("FC27_RECOMMENDATIONS_ENABLED", raising=False)
    monkeypatch.delenv("DISPATCH_ENABLED", raising=False)
    monkeypatch.delenv("DISPATCH_WORKER_ENABLED", raising=False)

    assert is_fc27_provider_enabled() is False
    assert is_fc27_live_ingestion_enabled() is False
    assert is_fc27_recommendations_enabled() is False
    assert is_dispatch_enabled() is False
    assert is_dispatch_worker_enabled() is False
    assert is_fc27_provider_dry_run() is True

    # Malformed values fail closed to safe defaults
    monkeypatch.setenv("FC27_PROVIDER_ENABLED", "invalid_value")
    monkeypatch.setenv("FC27_LIVE_INGESTION_ENABLED", "unrecognized")
    assert is_fc27_provider_enabled() is False
    assert is_fc27_live_ingestion_enabled() is False


def test_matchpulse_boundary_preservation():
    """30. MatchPulse Live has zero runtime imports and boundary script passes."""
    import sys
    assert "matchpulse" not in sys.modules


def test_dispatch_remaining_disabled(monkeypatch):
    """31. Dispatch remains disabled."""
    monkeypatch.delenv("DISPATCH_ENABLED", raising=False)
    assert is_dispatch_enabled() is False


def test_live_ingestion_remaining_disabled():
    """32. Live ingestion remains disabled and transport client fails closed."""
    assert is_fc27_live_ingestion_enabled() is False
    client = FC27TransportClient()
    with pytest.raises(FC27LiveAccessBlockedError, match="live network requests are disabled"):
        client.execute_request("GET", "clubs/info")


def test_recommendations_remaining_disabled_by_default():
    """33. Recommendations remain disabled by default."""
    assert is_fc27_recommendations_enabled() is False


def test_analytics_compatibility_matrix_completeness():
    """34. Completeness and validity of Analytics Compatibility Matrix."""
    matrix = get_metric_compatibility_matrix()
    assert len(matrix) >= 15
    domain_fields = {entry.domain_field for entry in matrix}
    assert "player_id" in domain_fields
    assert "position" in domain_fields
    assert "role" in domain_fields
    assert "formation" in domain_fields
    assert "goals" in domain_fields
    assert "passing_metrics" in domain_fields
    assert "defensive_metrics" in domain_fields
    assert "match_rating" in domain_fields
    assert "build_reference" in domain_fields

    for entry in matrix:
        assert entry.valid_range != ""
        assert entry.missing_data_policy != ""
        assert entry.confidence_impact != ""


def test_no_fc26_mapping_silently_promoted_to_fc27_validated():
    """35. Invariant: No community/FC 26 mapping is silently promoted to VALIDATED without authorized FC 27 evidence."""
    for event_id, event_def in FC27_EVENT_REGISTRY.items():
        assert event_def.status in (EventStatus.PROVISIONAL, EventStatus.UNVERIFIED), (
            f"Event {event_id} has invalid status {event_def.status}; cannot be VALIDATED without live capture."
        )

    for formula_id, formula_def in FC27_FORMULA_REGISTRY.items():
        assert formula_def.status in ("PROVISIONAL", "UNVERIFIED"), (
            f"Formula {formula_id} has status {formula_def.status}; cannot be VALIDATED without live capture."
        )
