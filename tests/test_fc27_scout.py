"""Focused unit tests for East Coasters FC 27 Scout evidence metrics and hypotheses (Items 41-88).

All tests use synthetic normalized fixtures only and block external network and DNS access.
"""
from __future__ import annotations

import hashlib
import socket
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from src.fc27_analytics import FORMULA_VERSION
from src.fc27_search_scout import (
    CONFIDENCE_TIER_RANK,
    EAST_COASTERS_NORMALIZED_EVIDENCE,
    MAX_HYPOTHESES,
    OBSERVATIONAL_ONLY_STATUS,
    FC27Hypothesis,
    FC27PartnershipEvidence,
    FC27PlayerEvidence,
    FC27ScoutReport,
    FC27SearchInput,
    FC27SearchScoutError,
    build_fc27_scout_report,
    build_fc27_search_context,
)

REF_NOW = datetime(2026, 10, 29, 18, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _block_network_and_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strictly block all external socket, HTTP, and DNS calls during Scout tests."""

    def _forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("External network or DNS access is strictly forbidden")

    monkeypatch.setattr(socket, "getaddrinfo", _forbidden)
    monkeypatch.setattr(socket, "gethostbyname", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", _forbidden)


def _sha256(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _make_player(
    player_ref: str = "plr_ecf_000000000001",
    *,
    position: str = "ST",
    role: str | None = "Advanced Forward",
    role_focus: str | None = "Attack",
    build_reference: str | None = "bld_ecf_st_v1",
    started: bool | None = True,
    minutes_played: int = 90,
    match_rating: float | None = 7.6,
    goals: int = 1,
    assists: int = 1,
    passes_attempted: int | None = 20,
    passes_completed: int | None = 15,
    tackles_attempted: int | None = 5,
    tackles_won: int | None = 4,
    interceptions: int | None = 2,
    shots: int | None = 4,
    shots_on_target: int | None = 2,
    saves: int | None = 0,
) -> dict[str, Any]:
    return {
        "player_ref": player_ref,
        "position": position,
        "role": role,
        "role_focus": role_focus,
        "build_reference": build_reference,
        "started": started,
        "minutes_played": minutes_played,
        "match_rating": match_rating,
        "goals": goals,
        "assists": assists,
        "passes_attempted": passes_attempted,
        "passes_completed": passes_completed,
        "tackles_attempted": tackles_attempted,
        "tackles_won": tackles_won,
        "interceptions": interceptions,
        "shots": shots,
        "shots_on_target": shots_on_target,
        "saves": saves,
    }


def _make_match(
    idx: int = 1,
    *,
    result: str = "WIN",
    goals_for: int = 2,
    goals_against: int = 0,
    formation: str | None = "4-3-3(2)",
    opponent_name: str = "Synthetic United FC",
    player_appearances: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    m_id = f"m_scout_{idx:04d}"
    dt = REF_NOW - timedelta(hours=idx * 3)
    apps = (
        player_appearances
        if player_appearances is not None
        else [
            _make_player(
                "plr_ecf_000000000002",
                position="CM",
                role="Box-to-Box",
                role_focus="Balanced",
                build_reference="bld_ecf_cm_v1",
                goals=0,
                assists=min(goals_for, 1),
            ),
            _make_player(
                "plr_ecf_000000000001",
                position="ST",
                role="Advanced Forward",
                role_focus="Attack",
                build_reference="bld_ecf_st_v1",
                goals=min(goals_for, 1),
                assists=0,
            ),
        ]
    )
    return {
        "match_id": m_id,
        "match_hash": _sha256(f"scout:{m_id}:{result}:{goals_for}:{goals_against}"),
        "payload_sha256": _sha256(f"payload:{m_id}"),
        "club_id": "330323",
        "club_name": "East Coasters",
        "platform": "common-gen5",
        "game_version": "FC27",
        "played_at": dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "completion_state": "COMPLETED",
        "result": result,
        "goals_for": goals_for,
        "goals_against": goals_against,
        "formation": formation,
        "opponent_name": opponent_name,
        "match_type": "LEAGUE",
        "data_quality_status": "ACCEPTED",
        "player_appearances": apps,
    }


def _build_sample_report(matches: list[dict[str, Any]]) -> FC27ScoutReport:
    ctx = build_fc27_search_context(
        FC27SearchInput(analysis_generated_at=REF_NOW, candidate_matches=tuple(matches))
    )
    return build_fc27_scout_report(ctx)


def _sample_four_matches() -> list[dict[str, Any]]:
    return [
        _make_match(1, result="WIN", goals_for=2, goals_against=0),
        _make_match(2, result="WIN", goals_for=3, goals_against=1),
        _make_match(3, result="DRAW", goals_for=1, goals_against=1),
        _make_match(4, result="LOSS", goals_for=0, goals_against=2),
    ]


def _sample_ten_matches() -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for i in range(1, 7):
        matches.append(
            _make_match(i, result="WIN", goals_for=2, goals_against=0, formation="4-3-3(2)")
        )
    for i in range(7, 11):
        matches.append(
            _make_match(i, result="LOSS", goals_for=0, goals_against=2, formation="4-2-3-1(2)")
        )
    return matches


# ==============================================================================
# Items 41-49: Scout Team Metrics
# ==============================================================================


def test_41_win_rate() -> None:
    report = _build_sample_report(_sample_four_matches())
    assert report.team_evidence.win_rate == pytest.approx(50.0)


def test_42_draw_rate() -> None:
    report = _build_sample_report(_sample_four_matches())
    assert report.team_evidence.draw_rate == pytest.approx(25.0)


def test_43_loss_rate() -> None:
    report = _build_sample_report(_sample_four_matches())
    assert report.team_evidence.loss_rate == pytest.approx(25.0)


def test_44_goals_per_match() -> None:
    report = _build_sample_report(_sample_four_matches())
    assert report.team_evidence.goals_per_match == pytest.approx(1.5)


def test_45_goals_conceded_per_match() -> None:
    report = _build_sample_report(_sample_four_matches())
    assert report.team_evidence.goals_conceded_per_match == pytest.approx(1.0)


def test_46_goal_differential() -> None:
    report = _build_sample_report(_sample_four_matches())
    assert report.team_evidence.goal_differential == 2
    assert report.team_evidence.average_goal_differential == pytest.approx(0.5)


def test_47_clean_sheet_percentage() -> None:
    report = _build_sample_report(_sample_four_matches())
    assert report.team_evidence.clean_sheets == 1
    assert report.team_evidence.clean_sheet_percentage == pytest.approx(25.0)


def test_48_recent_versus_baseline_delta() -> None:
    matches = [
        _make_match(i, result="WIN", goals_for=2, goals_against=0) for i in range(1, 6)
    ] + [
        _make_match(i, result="LOSS", goals_for=0, goals_against=2) for i in range(6, 11)
    ]
    report = _build_sample_report(matches)
    assert report.team_evidence.win_rate == pytest.approx(50.0)
    assert report.team_evidence.recent_win_rate == pytest.approx(100.0)
    assert report.team_evidence.win_rate_delta == pytest.approx(50.0)


def test_49_zero_denominator_returns_unavailable() -> None:
    report = _build_sample_report([])
    te = report.team_evidence
    assert te.matches_played == 0
    assert te.win_rate is None
    assert te.draw_rate is None
    assert te.loss_rate is None
    assert te.goals_per_match is None
    assert te.goals_conceded_per_match is None
    assert te.average_goal_differential is None
    assert te.clean_sheet_percentage is None
    assert te.win_rate_delta is None


# ==============================================================================
# Items 50-62: Player Evidence
# ==============================================================================


def _two_match_player_report() -> FC27PlayerEvidence:
    p1_m1 = _make_player(
        "plr_ecf_000000000001",
        minutes_played=90,
        goals=2,
        assists=1,
        passes_attempted=20,
        passes_completed=16,
        tackles_attempted=4,
        tackles_won=3,
        shots=4,
        shots_on_target=3,
        match_rating=8.0,
    )
    p1_m2 = _make_player(
        "plr_ecf_000000000001",
        minutes_played=80,
        goals=0,
        assists=1,
        passes_attempted=20,
        passes_completed=14,
        tackles_attempted=6,
        tackles_won=5,
        shots=4,
        shots_on_target=1,
        match_rating=7.0,
    )
    matches = [
        _make_match(1, result="WIN", goals_for=2, goals_against=0, player_appearances=[p1_m1]),
        _make_match(2, result="DRAW", goals_for=1, goals_against=1, player_appearances=[p1_m2]),
    ]
    return _build_sample_report(matches).player_evidence[0]


def test_50_appearance_aggregation() -> None:
    pe = _two_match_player_report()
    assert pe.appearances == 2
    assert pe.starts == 2


def test_51_minutes_aggregation() -> None:
    pe = _two_match_player_report()
    assert pe.minutes_played == 170


def test_52_goals_and_assists() -> None:
    pe = _two_match_player_report()
    assert pe.goals == 2
    assert pe.assists == 2
    assert pe.goal_contributions == 4


def test_53_pass_completion_rate() -> None:
    pe = _two_match_player_report()
    assert pe.passes_attempted == 40
    assert pe.passes_completed == 30
    assert pe.pass_completion_rate == pytest.approx(75.0)


def test_54_tackle_success_rate() -> None:
    pe = _two_match_player_report()
    assert pe.tackles_attempted == 10
    assert pe.tackles_won == 8
    assert pe.tackle_success_rate == pytest.approx(80.0)


def test_55_shot_accuracy_rate() -> None:
    pe = _two_match_player_report()
    assert pe.shots == 8
    assert pe.shots_on_target == 4
    assert pe.shot_accuracy_rate == pytest.approx(50.0)


def test_56_conversion_rate() -> None:
    pe = _two_match_player_report()
    assert pe.conversion_rate == pytest.approx(25.0)


def test_57_average_rating() -> None:
    pe = _two_match_player_report()
    assert pe.average_rating == pytest.approx(7.5)


def test_58_missing_attempts_return_unavailable_percentage() -> None:
    p1_missing = _make_player(
        "plr_ecf_000000000001",
        passes_attempted=None,
        passes_completed=None,
        tackles_attempted=0,
        tackles_won=0,
        shots=0,
        shots_on_target=0,
        goals=0,
    )
    report = _build_sample_report([_make_match(1, player_appearances=[p1_missing])])
    pe = report.player_evidence[0]
    assert pe.pass_completion_rate is None
    assert pe.tackle_success_rate is None
    assert pe.shot_accuracy_rate is None
    assert pe.conversion_rate is None


def test_59_pseudonymous_refs_only() -> None:
    report = _build_sample_report(_sample_four_matches())
    for pe in report.player_evidence:
        assert pe.player_ref.startswith("plr_ecf_")


def test_60_stable_player_ordering() -> None:
    p_c = _make_player("plr_ecf_000000000003", goals=5, match_rating=9.5)
    p_a = _make_player("plr_ecf_000000000001", goals=0, match_rating=6.0)
    p_b = _make_player("plr_ecf_000000000002", goals=1, match_rating=7.0)
    report = _build_sample_report([_make_match(1, goals_for=6, player_appearances=[p_c, p_a, p_b])])
    assert [p.player_ref for p in report.player_evidence] == [
        "plr_ecf_000000000001",
        "plr_ecf_000000000002",
        "plr_ecf_000000000003",
    ]


def test_61_no_overall_player_quality_score() -> None:
    fields = set(FC27PlayerEvidence.model_fields.keys())
    assert "overall_score" not in fields
    assert "quality_score" not in fields
    assert "contribution_score" not in fields


def test_62_no_human_performance_ranking() -> None:
    fields = set(FC27PlayerEvidence.model_fields.keys())
    assert "rank" not in fields
    assert "player_rank" not in fields


# ==============================================================================
# Items 63-72: Formation, Role, Build, and Partnership Evidence
# ==============================================================================


def test_63_formation_aggregation() -> None:
    report = _build_sample_report(_sample_ten_matches())
    by_form = {f.formation: f for f in report.formation_evidence}
    assert by_form["4-3-3(2)"].appearances == 6
    assert by_form["4-3-3(2)"].wins == 6
    assert by_form["4-3-3(2)"].win_rate == pytest.approx(100.0)


def test_64_formation_with_insufficient_sample() -> None:
    matches = [
        _make_match(1, result="WIN", formation="4-3-3(2)"),
        _make_match(2, result="WIN", formation="4-3-3(2)"),
        _make_match(3, result="WIN", formation="4-3-3(2)"),
        _make_match(4, result="LOSS", goals_for=0, goals_against=1, formation="4-2-3-1(2)"),
    ]
    report = _build_sample_report(matches)
    by_form = {f.formation: f for f in report.formation_evidence}
    assert by_form["4-2-3-1(2)"].appearances == 1
    assert by_form["4-2-3-1(2)"].confidence_tier == "INSUFFICIENT_DATA"


def test_65_role_aggregation() -> None:
    report = _build_sample_report(_sample_four_matches())
    roles = {r.role for r in report.role_evidence}
    assert "Advanced Forward" in roles
    assert "Box-to-Box" in roles


def test_66_role_focus_separation() -> None:
    p_attack = _make_player(
        "plr_ecf_000000000001", position="ST", role="Advanced Forward", role_focus="Attack"
    )
    p_complete = _make_player(
        "plr_ecf_000000000002", position="ST", role="Advanced Forward", role_focus="Complete"
    )
    report = _build_sample_report([_make_match(1, player_appearances=[p_attack, p_complete])])
    assert [r.focus for r in report.role_evidence] == ["Attack", "Complete"]


def test_67_build_aggregation() -> None:
    p1 = _make_player(
        "plr_ecf_000000000001",
        position="ST",
        role="Advanced Forward",
        build_reference="bld_ecf_finisher_01",
        goals=2,
    )
    report = _build_sample_report(
        [
            _make_match(1, goals_for=2, player_appearances=[p1]),
            _make_match(2, goals_for=2, player_appearances=[p1]),
        ]
    )
    assert len(report.build_evidence) == 1
    assert report.build_evidence[0].build_reference == "bld_ecf_finisher_01"
    assert report.build_evidence[0].goals == 4


def test_68_partnership_overlap() -> None:
    p1 = _make_player("plr_ecf_000000000002", minutes_played=90, goals=1, assists=1)
    p2 = _make_player("plr_ecf_000000000001", minutes_played=75, goals=1, assists=0)
    report = _build_sample_report([_make_match(1, goals_for=2, player_appearances=[p1, p2])])
    part = report.partnership_evidence[0]
    assert part.shared_appearances == 1
    assert part.shared_minutes == 75
    assert part.combined_goal_contributions == 3


def test_69_partnership_requires_valid_shared_appearances() -> None:
    p1 = _make_player("plr_ecf_000000000001", minutes_played=90)
    p2_zero = _make_player("plr_ecf_000000000002", minutes_played=0)
    report = _build_sample_report([_make_match(1, player_appearances=[p1, p2_zero])])
    assert report.partnership_evidence == ()


def test_70_stable_partnership_ordering() -> None:
    p3 = _make_player("plr_ecf_000000000003", minutes_played=90)
    p1 = _make_player("plr_ecf_000000000001", minutes_played=90)
    p2 = _make_player("plr_ecf_000000000002", minutes_played=90)
    report = _build_sample_report([_make_match(1, player_appearances=[p3, p1, p2])])
    pairs = [(p.player_ref_a, p.player_ref_b) for p in report.partnership_evidence]
    assert pairs == [
        ("plr_ecf_000000000001", "plr_ecf_000000000002"),
        ("plr_ecf_000000000001", "plr_ecf_000000000003"),
        ("plr_ecf_000000000002", "plr_ecf_000000000003"),
    ]


def test_71_no_causal_wording() -> None:
    with pytest.raises(FC27SearchScoutError, match="prohibited"):
        FC27PartnershipEvidence(
            player_ref_a="plr_ecf_000000000001",
            player_ref_b="plr_ecf_000000000002",
            shared_appearances=3,
            shared_minutes=270,
            wins=3,
            draws=0,
            losses=0,
            win_rate=100.0,
            goals_for=6,
            goals_against=0,
            goal_differential=6,
            clean_sheets=3,
            combined_goals=4,
            combined_assists=2,
            combined_goal_contributions=6,
            observation_summary="This partnership caused the team to win.",
            eligible_sample_count=3,
            confidence_tier="EXPLORATORY",
        )


def test_72_no_optimal_or_best_wording() -> None:
    with pytest.raises(FC27SearchScoutError, match="prohibited"):
        FC27PartnershipEvidence(
            player_ref_a="plr_ecf_000000000001",
            player_ref_b="plr_ecf_000000000002",
            shared_appearances=3,
            shared_minutes=270,
            wins=3,
            draws=0,
            losses=0,
            win_rate=100.0,
            goals_for=6,
            goals_against=0,
            goal_differential=6,
            clean_sheets=3,
            combined_goals=4,
            combined_assists=2,
            combined_goal_contributions=6,
            observation_summary="This partnership is the optimal and best pairing.",
            eligible_sample_count=3,
            confidence_tier="EXPLORATORY",
        )


# ==============================================================================
# Items 73-88: Hypotheses
# ==============================================================================


def test_73_no_hypothesis_with_insufficient_data() -> None:
    report = _build_sample_report(_sample_four_matches()[:2])
    assert report.confidence_tier == "INSUFFICIENT_DATA"
    assert report.hypotheses == ()


def test_74_formation_association_generated() -> None:
    report = _build_sample_report(_sample_ten_matches())
    assert any(h.category == "FORMATION_ASSOCIATION" for h in report.hypotheses)


def test_75_role_association_generated() -> None:
    report = _build_sample_report(_sample_ten_matches())
    assert any(h.category == "ROLE_ASSOCIATION" for h in report.hypotheses)


def test_76_build_association_generated() -> None:
    report = _build_sample_report(_sample_ten_matches())
    assert any(h.category == "BUILD_ASSOCIATION" for h in report.hypotheses)


def test_77_partnership_association_generated() -> None:
    report = _build_sample_report(_sample_ten_matches())
    assert any(h.category == "PARTNERSHIP_ASSOCIATION" for h in report.hypotheses)


def test_78_recent_form_hypothesis_generated() -> None:
    report = _build_sample_report(_sample_ten_matches())
    assert any(h.category == "RECENT_FORM_CHANGE" for h in report.hypotheses)


def test_79_maximum_20_hypotheses() -> None:
    # Create 20 matches with many roles, builds, and players so >20 candidate hypotheses exist
    matches: list[dict[str, Any]] = []
    for i in range(1, 21):
        apps = [
            _make_player(
                f"plr_ecf_{p:012d}",
                position="CM" if p % 2 == 0 else "ST",
                role="Box-to-Box" if p % 2 == 0 else "Advanced Forward",
                role_focus=f"Focus_{p}",
                build_reference=f"bld_ecf_multi_{p:02d}",
            )
            for p in range(1, 9)
        ]
        matches.append(
            _make_match(
                i,
                result="WIN" if i <= 14 else "LOSS",
                goals_for=2 if i <= 14 else 0,
                goals_against=0 if i <= 14 else 2,
                formation="4-3-3(2)" if i % 2 == 0 else "4-2-3-1(2)",
                player_appearances=apps,
            )
        )
    report = _build_sample_report(matches)
    assert len(report.hypotheses) == MAX_HYPOTHESES == 20


def test_80_stable_hypothesis_ordering() -> None:
    report = _build_sample_report(_sample_ten_matches())
    sort_keys = [
        (
            -CONFIDENCE_TIER_RANK[h.confidence_tier],
            -h.eligible_match_count,
            -abs(h.absolute_delta),
            h.category,
            h.hypothesis_id,
        )
        for h in report.hypotheses
    ]
    assert sort_keys == sorted(sort_keys)


def test_81_deterministic_hypothesis_id() -> None:
    m = _sample_ten_matches()
    r1 = _build_sample_report(m)
    r2 = _build_sample_report(list(reversed(m)))
    assert [h.hypothesis_id for h in r1.hypotheses] == [h.hypothesis_id for h in r2.hypotheses]


def test_82_deterministic_report_hash() -> None:
    m = _sample_ten_matches()
    r1 = _build_sample_report(m)
    r2 = _build_sample_report(list(reversed(m)))
    assert r1.report_hash == r2.report_hash


def test_83_observational_only_status() -> None:
    report = _build_sample_report(_sample_ten_matches())
    assert all(h.status == OBSERVATIONAL_ONLY_STATUS for h in report.hypotheses)


def test_84_correlation_language_only() -> None:
    report = _build_sample_report(_sample_ten_matches())
    for h in report.hypotheses:
        stmt = h.statement.lower()
        assert any(
            phrase in stmt
            for phrase in ("associated with", "observed alongside", "correlated with")
        )
        assert "caused" not in stmt
        assert "optimal" not in stmt
        assert "best" not in stmt


def test_85_limitations_populated() -> None:
    report = _build_sample_report(_sample_ten_matches())
    assert all(len(h.limitations) > 0 for h in report.hypotheses)


def test_86_confounders_populated() -> None:
    report = _build_sample_report(_sample_ten_matches())
    assert all(len(h.confounders) >= 3 for h in report.hypotheses)


def test_87_confidence_bounded_by_evidence() -> None:
    report = _build_sample_report(_sample_ten_matches())
    for h in report.hypotheses:
        assert (
            CONFIDENCE_TIER_RANK[h.confidence_tier]
            <= CONFIDENCE_TIER_RANK[report.confidence_tier]
        )
    with pytest.raises(FC27SearchScoutError, match="exceeds underlying evidence tier"):
        FC27Hypothesis(
            hypothesis_id="hyp_fc27_0123456789abcdef0123",
            category="FORMATION_ASSOCIATION",
            statement="Formation 4-3-3(2) is associated with a 100.0% win rate (higher in this sample).",
            metric_name="win_rate_pct",
            direction="HIGHER",
            baseline_value=50.0,
            comparison_value=100.0,
            absolute_delta=50.0,
            relative_delta=100.0,
            eligible_match_count=3,  # EXPLORATORY max
            supporting_match_hashes=(_sha256("m1"), _sha256("m2"), _sha256("m3")),
            confidence_tier="HIGH_CONFIDENCE",
            limitations=("Exploratory sample only.",),
            confounders=("opponent_strength",),
        )


def test_88_no_external_tactical_hypothesis_promoted_automatically() -> None:
    report = _build_sample_report(_sample_ten_matches())
    for h in report.hypotheses:
        assert h.source_classification == EAST_COASTERS_NORMALIZED_EVIDENCE
        assert len(h.evidence_references) >= 1
        assert len(h.falsification_criteria) >= 1
        assert "EXTERNAL_TACTICAL_HYPOTHESIS" not in h.statement
        assert "4-2-1-3" not in h.statement
        assert "4-1-2-1-2 Narrow" not in h.statement


def test_unknown_build_and_opponent_formation_isolated() -> None:
    p_unknown = _make_player(
        "plr_ecf_000000000001",
        position="ST",
        role="Advanced Forward",
        build_reference=None,
    )
    p_known = _make_player(
        "plr_ecf_000000000002",
        position="CM",
        role="Box-to-Box",
        build_reference="bld_ecf_cm_v1",
    )
    m1 = _make_match(1, player_appearances=[p_unknown, p_known])
    m2 = _make_match(2, player_appearances=[p_unknown, p_known])
    m3 = _make_match(3, player_appearances=[p_unknown, p_known])
    m3["opponent_formation"] = "4-3-3(2)"
    report = _build_sample_report([m1, m2, m3])
    build_refs = {b.build_reference: b for b in report.build_evidence}
    assert "UNKNOWN_BUILD" in build_refs
    assert "bld_ecf_cm_v1" in build_refs
    assert build_refs["UNKNOWN_BUILD"].appearances == 3
    assert all(
        "UNKNOWN_BUILD" not in h.statement
        for h in report.hypotheses
        if h.category == "BUILD_ASSOCIATION"
    )
    opp_ev = report.opponent_evidence[0]
    assert opp_ev.opponent_formation_split == {
        "4-3-3(2)": 1,
        "UNKNOWN_OPPONENT_FORMATION": 2,
    }


@pytest.mark.parametrize(
    "forbidden_clause",
    [
        "proves team success",
        "causes higher scoring",
        "guarantees promotion",
        "guaranteed win in league play",
        "optimal tactical setup",
        "best option available",
        "worst option available",
        "must bench the midfielder",
        "drop player from starting lineup",
        "maximizes win probability",
        "reflects poor player attitude and motivation",
    ],
)
def test_hypothesis_rejects_prohibited_causal_and_prescriptive_claims(
    forbidden_clause: str,
) -> None:
    with pytest.raises(FC27SearchScoutError, match="prohibited"):
        FC27Hypothesis(
            hypothesis_id="hyp_fc27_0123456789abcdef0123",
            category="FORMATION_ASSOCIATION",
            statement=f"Formation 4-3-3(2) is associated with a 75.0% win rate and {forbidden_clause}.",
            metric_name="win_rate_pct",
            direction="HIGHER",
            baseline_value=50.0,
            comparison_value=75.0,
            absolute_delta=25.0,
            relative_delta=50.0,
            eligible_match_count=4,
            supporting_match_hashes=(_sha256("m1"), _sha256("m2"), _sha256("m3"), _sha256("m4")),
            confidence_tier="EXPLORATORY",
            limitations=("Exploratory sample only.",),
            confounders=("opponent_strength",),
        )
