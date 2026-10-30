"""Focused unit tests for East Coasters FC 27 Search contracts and context assembly (Items 1-40).

All tests use synthetic normalized fixtures only and block external network and DNS access.
"""
from __future__ import annotations

import copy
import hashlib
import math
import socket
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from src.fc27_analytics import FORMULA_VERSION
from src.fc27_search_scout import (
    BASELINE_MATCH_LIMIT,
    EAST_COASTERS_NORMALIZED_EVIDENCE,
    RECENT_FORM_MATCH_LIMIT,
    FC27AnalysisIdentity,
    FC27AnalysisWindow,
    FC27NormalizedMatchEvidenceRecord,
    FC27NormalizedPlayerAppearance,
    FC27PrivacyViolationError,
    FC27ScopeViolationError,
    FC27SearchContext,
    FC27SearchInput,
    FC27SearchScoutError,
    build_fc27_search_context,
)

REF_NOW = datetime(2026, 10, 29, 18, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _block_network_and_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strictly block all external socket, HTTP, and DNS calls during Search tests."""

    def _forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("External network or DNS access is strictly forbidden")

    monkeypatch.setattr(socket, "getaddrinfo", _forbidden)
    monkeypatch.setattr(socket, "gethostbyname", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", _forbidden)


def _sha256(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _make_player_app(
    player_ref: str = "plr_ecf_000000000001",
    *,
    position: str = "ST",
    role: str | None = "Advanced Forward",
    role_focus: str | None = "Attack",
    build_reference: str | None = "bld_ecf_st_v1",
    started: bool | None = True,
    minutes_played: int = 90,
    match_rating: float | None = 7.8,
    goals: int = 1,
    assists: int = 0,
    passes_attempted: int | None = 20,
    passes_completed: int | None = 16,
    tackles_attempted: int | None = 4,
    tackles_won: int | None = 3,
    interceptions: int | None = 2,
    shots: int | None = 3,
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


def _make_match_candidate(
    idx: int = 1,
    *,
    match_id: str | None = None,
    match_hash: str | None = None,
    played_at: datetime | str | None = None,
    club_id: str = "330323",
    club_name: str = "East Coasters",
    platform: str = "common-gen5",
    game_version: str = "FC27",
    completion_state: str = "COMPLETED",
    result: str = "WIN",
    goals_for: int = 2,
    goals_against: int = 1,
    formation: str | None = "4-3-3(2)",
    opponent_name: str | None = "Synthetic Harbor FC",
    match_type: str = "LEAGUE",
    data_quality_status: str = "ACCEPTED",
    possession_pct: float | None = 54.0,
    player_appearances: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    m_id = match_id if match_id is not None else f"m_ecf_{idx:04d}"
    m_hash = match_hash if match_hash is not None else _sha256(f"hash:{m_id}:{result}:{goals_for}:{goals_against}")
    dt = played_at if played_at is not None else (REF_NOW - timedelta(hours=idx * 4))
    dt_str = dt.strftime("%Y-%m-%dT%H:%M:%SZ") if isinstance(dt, datetime) else str(dt)
    apps = (
        player_appearances
        if player_appearances is not None
        else [
            _make_player_app(
                "plr_ecf_000000000001",
                position="ST",
                role="Advanced Forward",
                goals=min(goals_for, 1),
                assists=max(0, goals_for - 1),
            ),
            _make_player_app(
                "plr_ecf_000000000002",
                position="CM",
                role="Box-to-Box",
                goals=max(0, goals_for - 1),
                assists=min(goals_for, 1),
            ),
        ]
    )
    payload: dict[str, Any] = {
        "match_id": m_id,
        "match_hash": m_hash,
        "payload_sha256": _sha256(f"payload:{m_id}"),
        "club_id": club_id,
        "club_name": club_name,
        "platform": platform,
        "game_version": game_version,
        "played_at": dt_str,
        "completion_state": completion_state,
        "result": result,
        "goals_for": goals_for,
        "goals_against": goals_against,
        "formation": formation,
        "opponent_name": opponent_name,
        "match_type": match_type,
        "data_quality_status": data_quality_status,
        "possession_pct": possession_pct,
        "player_appearances": apps,
    }
    payload.update(extra)
    return payload


# ==============================================================================
# Items 1-14: Identity and Input Validation
# ==============================================================================


def test_01_correct_east_coasters_identity_accepted() -> None:
    ident = FC27AnalysisIdentity(
        club_id="330323",
        club_name="East Coasters",
        platform="common-gen5",
        game_version="FC27",
    )
    assert ident.club_id == "330323"
    assert ident.club_name == "East Coasters"
    assert ident.platform == "common-gen5"
    assert ident.game_version == "FC27"


def test_02_wrong_club_id_rejected() -> None:
    with pytest.raises(FC27ScopeViolationError, match="club_id"):
        FC27AnalysisIdentity(club_id="999999")


def test_03_wrong_club_name_rejected() -> None:
    with pytest.raises(FC27ScopeViolationError, match="club_name"):
        FC27AnalysisIdentity(club_name="Other Club FC")


def test_04_wrong_platform_rejected() -> None:
    with pytest.raises(FC27ScopeViolationError, match="platform"):
        FC27AnalysisIdentity(platform="common-gen4")


def test_05_wrong_game_version_rejected() -> None:
    with pytest.raises(FC27ScopeViolationError, match="game_version"):
        FC27AnalysisIdentity(game_version="FC26")


def test_06_gamer_tag_field_rejected() -> None:
    bad_cand = _make_match_candidate(1, gamer_tag="SecretGamerTag123")
    with pytest.raises(FC27PrivacyViolationError):
        build_fc27_search_context(
            {"analysis_generated_at": REF_NOW, "candidate_matches": (bad_cand,)}
        )


def test_07_display_alias_field_rejected() -> None:
    bad_app = _make_player_app("plr_ecf_000000000001")
    bad_app["display_alias"] = "PlayerHandle"
    bad_cand = _make_match_candidate(1, player_appearances=[bad_app])
    with pytest.raises(FC27PrivacyViolationError):
        build_fc27_search_context(
            {"analysis_generated_at": REF_NOW, "candidate_matches": (bad_cand,)}
        )


def test_08_external_player_id_field_rejected() -> None:
    bad_app = _make_player_app("plr_ecf_000000000001")
    bad_app["persona_id"] = "100200300"
    bad_cand = _make_match_candidate(1, player_appearances=[bad_app])
    with pytest.raises(FC27PrivacyViolationError):
        build_fc27_search_context(
            {"analysis_generated_at": REF_NOW, "candidate_matches": (bad_cand,)}
        )
    with pytest.raises(FC27PrivacyViolationError):
        FC27NormalizedPlayerAppearance(
            player_ref="not_a_pseudonym_999",
            position="ST",
            minutes_played=90,
        )


def test_09_raw_payload_field_rejected() -> None:
    bad_cand = _make_match_candidate(1, raw_payload='{"raw":"body"}')
    with pytest.raises(FC27PrivacyViolationError):
        build_fc27_search_context(
            {"analysis_generated_at": REF_NOW, "candidate_matches": (bad_cand,)}
        )


def test_10_naive_timestamp_rejected() -> None:
    naive_dt = datetime(2026, 10, 29, 12, 0, 0)
    with pytest.raises(FC27SearchScoutError, match="timezone-aware UTC"):
        FC27AnalysisWindow(analysis_generated_at=naive_dt.isoformat())
    bad_cand = _make_match_candidate(1, played_at="2026-10-29T12:00:00")
    with pytest.raises(FC27SearchScoutError, match="timezone-aware UTC"):
        build_fc27_search_context(
            {"analysis_generated_at": REF_NOW, "candidate_matches": (bad_cand,)}
        )


def test_11_future_timestamp_excluded_or_rejected() -> None:
    future_dt = REF_NOW + timedelta(hours=3)
    future_cand = _make_match_candidate(1, played_at=future_dt)
    ctx = build_fc27_search_context(
        {"analysis_generated_at": REF_NOW, "candidate_matches": (future_cand,)}
    )
    assert ctx.eligible_match_count == 0
    assert ctx.exclusion_summary.excluded_future_timestamp == 1

    with pytest.raises(FC27SearchScoutError, match="Future timestamp"):
        build_fc27_search_context(
            {"analysis_generated_at": REF_NOW, "candidate_matches": (future_cand,)},
            reject_future_timestamps=True,
        )


def test_12_nan_rejected() -> None:
    with pytest.raises(FC27SearchScoutError, match="finite"):
        FC27NormalizedPlayerAppearance(
            player_ref="plr_ecf_000000000001",
            position="ST",
            minutes_played=90,
            match_rating=float("nan"),
        )


def test_13_infinity_rejected() -> None:
    with pytest.raises(FC27SearchScoutError, match="finite"):
        FC27NormalizedPlayerAppearance(
            player_ref="plr_ecf_000000000001",
            position="ST",
            minutes_played=90,
            match_rating=math.inf,
        )


def test_14_negative_impossible_metric_rejected() -> None:
    bad_cand = _make_match_candidate(1, goals_for=-1)
    with pytest.raises(FC27SearchScoutError, match="cannot be negative"):
        build_fc27_search_context(
            {"analysis_generated_at": REF_NOW, "candidate_matches": (bad_cand,)}
        )


# ==============================================================================
# Items 15-40: Search Context Assembly, Confidence Tiers, Filtering, and Hashing
# ==============================================================================


def test_15_empty_dataset() -> None:
    ctx = build_fc27_search_context(
        FC27SearchInput(analysis_generated_at=REF_NOW, candidate_matches=())
    )
    assert ctx.eligible_match_count == 0
    assert ctx.excluded_match_count == 0
    assert ctx.confidence_tier == "INSUFFICIENT_DATA"
    assert ctx.baseline_matches == ()
    assert ctx.recent_matches == ()
    assert ctx.baseline_team_aggregates.goals_per_match is None


def test_16_one_eligible_match() -> None:
    ctx = build_fc27_search_context(
        FC27SearchInput(
            analysis_generated_at=REF_NOW,
            candidate_matches=(_make_match_candidate(1),),
        )
    )
    assert ctx.eligible_match_count == 1
    assert ctx.confidence_tier == "INSUFFICIENT_DATA"


def test_17_two_eligible_matches() -> None:
    matches = tuple(_make_match_candidate(i) for i in range(1, 3))
    ctx = build_fc27_search_context(
        FC27SearchInput(analysis_generated_at=REF_NOW, candidate_matches=matches)
    )
    assert ctx.eligible_match_count == 2
    assert ctx.confidence_tier == "INSUFFICIENT_DATA"


def test_18_three_eligible_matches() -> None:
    matches = tuple(_make_match_candidate(i) for i in range(1, 4))
    ctx = build_fc27_search_context(
        FC27SearchInput(analysis_generated_at=REF_NOW, candidate_matches=matches)
    )
    assert ctx.eligible_match_count == 3
    assert ctx.confidence_tier == "EXPLORATORY"


def test_19_nine_eligible_matches() -> None:
    matches = tuple(_make_match_candidate(i) for i in range(1, 10))
    ctx = build_fc27_search_context(
        FC27SearchInput(analysis_generated_at=REF_NOW, candidate_matches=matches)
    )
    assert ctx.eligible_match_count == 9
    assert ctx.confidence_tier == "LOW_CONFIDENCE"


def test_20_ten_eligible_matches() -> None:
    matches = tuple(_make_match_candidate(i) for i in range(1, 11))
    ctx = build_fc27_search_context(
        FC27SearchInput(analysis_generated_at=REF_NOW, candidate_matches=matches)
    )
    assert ctx.eligible_match_count == 10
    assert ctx.confidence_tier == "MODERATE_CONFIDENCE"


def test_21_nineteen_eligible_matches() -> None:
    matches = tuple(_make_match_candidate(i) for i in range(1, 20))
    ctx = build_fc27_search_context(
        FC27SearchInput(analysis_generated_at=REF_NOW, candidate_matches=matches)
    )
    assert ctx.eligible_match_count == 19
    assert ctx.confidence_tier == "MODERATE_CONFIDENCE"


def test_22_twenty_eligible_matches() -> None:
    matches = tuple(_make_match_candidate(i) for i in range(1, 21))
    ctx = build_fc27_search_context(
        FC27SearchInput(analysis_generated_at=REF_NOW, candidate_matches=matches)
    )
    assert ctx.eligible_match_count == 20
    assert ctx.confidence_tier == "HIGH_CONFIDENCE"


def test_23_more_than_twenty_matches_bounded_to_latest_twenty() -> None:
    matches = tuple(_make_match_candidate(i) for i in range(1, 26))
    ctx = build_fc27_search_context(
        FC27SearchInput(analysis_generated_at=REF_NOW, candidate_matches=matches)
    )
    assert ctx.eligible_match_count == BASELINE_MATCH_LIMIT == 20
    assert len(ctx.baseline_matches) == 20
    assert ctx.exclusion_summary.excluded_window_bound == 5
    assert ctx.baseline_matches[0].match_id == "m_ecf_0001"
    assert ctx.baseline_matches[-1].match_id == "m_ecf_0020"


def test_24_recent_window_bounded_to_latest_five() -> None:
    matches = tuple(_make_match_candidate(i) for i in range(1, 15))
    ctx = build_fc27_search_context(
        FC27SearchInput(analysis_generated_at=REF_NOW, candidate_matches=matches)
    )
    assert len(ctx.recent_matches) == RECENT_FORM_MATCH_LIMIT == 5
    assert [m.match_id for m in ctx.recent_matches] == [
        f"m_ecf_{i:04d}" for i in range(1, 6)
    ]


def test_25_stable_timestamp_ordering() -> None:
    m1 = _make_match_candidate(1, match_id="m_old", played_at=REF_NOW - timedelta(days=3))
    m2 = _make_match_candidate(2, match_id="m_new", played_at=REF_NOW - timedelta(days=1))
    m3 = _make_match_candidate(3, match_id="m_mid", played_at=REF_NOW - timedelta(days=2))
    ctx = build_fc27_search_context(
        FC27SearchInput(analysis_generated_at=REF_NOW, candidate_matches=(m1, m2, m3))
    )
    assert [m.match_id for m in ctx.baseline_matches] == ["m_new", "m_mid", "m_old"]


def test_26_stable_tie_break_ordering() -> None:
    same_ts = REF_NOW - timedelta(hours=10)
    m_b = _make_match_candidate(1, match_id="m_tie_b", played_at=same_ts)
    m_a = _make_match_candidate(2, match_id="m_tie_a", played_at=same_ts)
    ctx_1 = build_fc27_search_context(
        FC27SearchInput(analysis_generated_at=REF_NOW, candidate_matches=(m_b, m_a))
    )
    ctx_2 = build_fc27_search_context(
        FC27SearchInput(analysis_generated_at=REF_NOW, candidate_matches=(m_a, m_b))
    )
    assert [m.match_id for m in ctx_1.baseline_matches] == ["m_tie_a", "m_tie_b"]
    assert ctx_1.context_hash == ctx_2.context_hash


def test_27_duplicate_match_removed() -> None:
    m1 = _make_match_candidate(1, match_id="m_dup_01")
    m1_dup = copy.deepcopy(m1)
    ctx = build_fc27_search_context(
        FC27SearchInput(analysis_generated_at=REF_NOW, candidate_matches=(m1, m1_dup))
    )
    assert ctx.eligible_match_count == 1
    assert ctx.exclusion_summary.excluded_duplicates == 1


def test_28_conflicting_match_rejected() -> None:
    m1 = _make_match_candidate(1, match_id="m_conflict_01", match_hash=_sha256("h1"))
    m2 = _make_match_candidate(2, match_id="m_conflict_01", match_hash=_sha256("h2"))
    with pytest.raises(FC27SearchScoutError, match="Conflicting match hash"):
        build_fc27_search_context(
            FC27SearchInput(analysis_generated_at=REF_NOW, candidate_matches=(m1, m2)),
            reject_on_conflict=True,
        )
    ctx = build_fc27_search_context(
        FC27SearchInput(analysis_generated_at=REF_NOW, candidate_matches=(m1, m2)),
        reject_on_conflict=False,
    )
    assert ctx.eligible_match_count == 0
    assert ctx.exclusion_summary.excluded_conflicts == 2


def test_29_quarantined_match_excluded() -> None:
    m_ok = _make_match_candidate(1, match_id="m_ok")
    m_q1 = _make_match_candidate(2, match_id="m_quarantined_id")
    m_q2 = _make_match_candidate(3, match_id="m_quarantined_status", data_quality_status="QUARANTINED")
    ctx = build_fc27_search_context(
        FC27SearchInput(
            analysis_generated_at=REF_NOW,
            candidate_matches=(m_ok, m_q1, m_q2),
            quarantined_match_ids=frozenset({"m_quarantined_id"}),
        )
    )
    assert ctx.eligible_match_count == 1
    assert ctx.exclusion_summary.excluded_quarantine == 2


def test_30_rejected_batch_excluded() -> None:
    m_ok = _make_match_candidate(1, match_id="m_ok", export_id="exp_good")
    m_rej = _make_match_candidate(2, match_id="m_rej", export_id="exp_bad")
    ctx = build_fc27_search_context(
        FC27SearchInput(
            analysis_generated_at=REF_NOW,
            candidate_matches=(m_ok, m_rej),
            rejected_batch_ids=frozenset({"exp_bad"}),
        )
    )
    assert ctx.eligible_match_count == 1
    assert ctx.exclusion_summary.excluded_rejected_batch == 1


def test_31_wrong_identity_excluded() -> None:
    m_ok = _make_match_candidate(1)
    m_wrong_club = _make_match_candidate(2, club_id="111111")
    ctx = build_fc27_search_context(
        FC27SearchInput(
            analysis_generated_at=REF_NOW,
            candidate_matches=(m_ok, m_wrong_club),
        )
    )
    assert ctx.eligible_match_count == 1
    assert ctx.exclusion_summary.excluded_wrong_identity == 1


def test_32_wrong_platform_excluded() -> None:
    m_ok = _make_match_candidate(1)
    m_wrong_plat = _make_match_candidate(2, platform="pc")
    ctx = build_fc27_search_context(
        FC27SearchInput(
            analysis_generated_at=REF_NOW,
            candidate_matches=(m_ok, m_wrong_plat),
        )
    )
    assert ctx.eligible_match_count == 1
    assert ctx.exclusion_summary.excluded_wrong_platform == 1


def test_33_wrong_version_excluded() -> None:
    m_ok = _make_match_candidate(1)
    m_wrong_ver = _make_match_candidate(2, game_version="FC26")
    ctx = build_fc27_search_context(
        FC27SearchInput(
            analysis_generated_at=REF_NOW,
            candidate_matches=(m_ok, m_wrong_ver),
        )
    )
    assert ctx.eligible_match_count == 1
    assert ctx.exclusion_summary.excluded_wrong_game_version == 1


def test_34_non_completed_match_excluded() -> None:
    m_ok = _make_match_candidate(1)
    m_aban = _make_match_candidate(2, completion_state="ABANDONED")
    m_part = _make_match_candidate(3, completion_state="PARTIAL")
    ctx = build_fc27_search_context(
        FC27SearchInput(
            analysis_generated_at=REF_NOW,
            candidate_matches=(m_ok, m_aban, m_part),
        )
    )
    assert ctx.eligible_match_count == 1
    assert ctx.exclusion_summary.excluded_status == 2


def test_35_unknown_optional_value_remains_unavailable() -> None:
    app_no_opt = _make_player_app(
        "plr_ecf_000000000001",
        passes_attempted=None,
        passes_completed=None,
        tackles_attempted=None,
        tackles_won=None,
        interceptions=None,
        shots=None,
        shots_on_target=None,
        saves=None,
    )
    m = _make_match_candidate(
        1,
        possession_pct=None,
        player_appearances=[app_no_opt],
    )
    ctx = build_fc27_search_context(
        FC27SearchInput(analysis_generated_at=REF_NOW, candidate_matches=(m,))
    )
    agg = ctx.baseline_team_aggregates
    assert agg.possession is None
    assert agg.shots is None
    assert agg.shots_on_target is None
    assert agg.passes_attempted is None
    assert agg.passes_completed is None


def test_36_unknown_optional_value_is_not_converted_to_zero() -> None:
    app_no_opt = _make_player_app(
        "plr_ecf_000000000001",
        passes_attempted=None,
        passes_completed=None,
        tackles_attempted=None,
        tackles_won=None,
        interceptions=None,
        shots=None,
        shots_on_target=None,
        saves=None,
    )
    m = _make_match_candidate(
        1,
        possession_pct=None,
        player_appearances=[app_no_opt],
    )
    ctx = build_fc27_search_context(
        FC27SearchInput(analysis_generated_at=REF_NOW, candidate_matches=(m,))
    )
    agg = ctx.baseline_team_aggregates
    assert agg.tackles_attempted is None
    assert agg.tackles_won is None
    assert agg.interceptions is None
    assert agg.saves is None


def test_37_team_aggregate_correctness() -> None:
    m1 = _make_match_candidate(1, result="WIN", goals_for=3, goals_against=0)
    m2 = _make_match_candidate(2, result="DRAW", goals_for=1, goals_against=1)
    m3 = _make_match_candidate(3, result="LOSS", goals_for=0, goals_against=2)
    ctx = build_fc27_search_context(
        FC27SearchInput(analysis_generated_at=REF_NOW, candidate_matches=(m1, m2, m3))
    )
    agg = ctx.baseline_team_aggregates
    assert agg.matches_played == 3
    assert agg.wins == 1
    assert agg.draws == 1
    assert agg.losses == 1
    assert agg.goals_for == 4
    assert agg.goals_against == 3
    assert agg.goal_differential == 1
    assert agg.goals_per_match == pytest.approx(4.0 / 3.0, abs=1e-4)
    assert agg.goals_conceded_per_match == pytest.approx(1.0, abs=1e-4)
    assert agg.clean_sheets == 1
    assert agg.clean_sheet_percentage == pytest.approx(33.3333, abs=1e-4)
    assert agg.result_distribution == {"WIN": 1, "DRAW": 1, "LOSS": 1}


def test_38_exclusion_summary_correctness() -> None:
    cands = (
        _make_match_candidate(1, match_id="m_ok"),
        _make_match_candidate(1, match_id="m_ok"),  # duplicate
        _make_match_candidate(2, match_id="m_conf", replay_conflict=True),
        _make_match_candidate(3, match_id="m_id", club_id="000001"),
        _make_match_candidate(4, match_id="m_plat", platform="ps4"),
        _make_match_candidate(5, match_id="m_ver", game_version="FC25"),
        _make_match_candidate(6, match_id="m_stat", completion_state="ABANDONED"),
        _make_match_candidate(7, match_id="m_dq", data_quality_status="REJECTED_QUALITY"),
        _make_match_candidate(8, match_id="m_fut", played_at=REF_NOW + timedelta(days=1)),
        _make_match_candidate(9, match_id="m_quar", is_quarantined=True),
        {"match_id": "", "played_at": None},  # missing required field
    )
    ctx = build_fc27_search_context(
        FC27SearchInput(analysis_generated_at=REF_NOW, candidate_matches=cands)
    )
    ex = ctx.exclusion_summary
    assert ex.total_candidates == 11
    assert ex.eligible_records == 1
    assert ex.excluded_duplicates == 1
    assert ex.excluded_conflicts == 1
    assert ex.excluded_wrong_identity == 1
    assert ex.excluded_wrong_platform == 1
    assert ex.excluded_wrong_game_version == 1
    assert ex.excluded_status == 1
    assert ex.excluded_data_quality == 1
    assert ex.excluded_future_timestamp == 1
    assert ex.excluded_quarantine == 1
    assert ex.excluded_missing_required_field == 1
    assert ctx.excluded_match_count == 10


def test_39_deterministic_context_hash() -> None:
    m1 = _make_match_candidate(1)
    m2 = _make_match_candidate(2)
    ctx_a = build_fc27_search_context(
        FC27SearchInput(analysis_generated_at=REF_NOW, candidate_matches=(m1, m2))
    )
    ctx_b = build_fc27_search_context(
        FC27SearchInput(analysis_generated_at=REF_NOW, candidate_matches=(m2, m1))
    )
    assert ctx_a.context_hash == ctx_b.context_hash
    assert ctx_a.formula_version == FORMULA_VERSION


def test_40_input_immutability() -> None:
    raw_input = {
        "analysis_generated_at": REF_NOW.isoformat(),
        "candidate_matches": [_make_match_candidate(1), _make_match_candidate(2)],
        "provenance": {"source": "synthetic"},
    }
    snapshot = copy.deepcopy(raw_input)
    ctx = build_fc27_search_context(raw_input)
    assert raw_input == snapshot
    with pytest.raises(Exception):
        ctx.eligible_match_count = 99  # type: ignore[misc]
