"""Unit tests for East Coasters FC 27 Director Evidence Reconciliation (Tests #76-#83)."""
from __future__ import annotations

import copy
import hashlib
import socket
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from src.fc27_analyst import (
    FC27AnalystInput,
    FC27Contradiction,
    build_fc27_analyst_report,
)
from src.fc27_coach import (
    FC27CoachInput,
    _recompute_analyst_hash,
    build_fc27_coach_report,
)
from src.fc27_director import (
    ALLOWED_DIRECTOR_OUTPUT_TYPES,
    DIRECTOR_REVIEW_STATUS,
    PROHIBITED_DIRECTOR_OUTPUT_TYPES,
    FC27DirectorContradictionError,
    FC27DirectorInput,
    FC27DirectorReport,
    FC27DirectorValidationError,
    build_fc27_director_report,
    validate_director_output_type,
)
from src.fc27_search_scout import (
    CONFIDENCE_TIER_RANK,
    FC27SearchInput,
    build_fc27_scout_report,
    build_fc27_search_context,
)
from src.fc27_tactics import (
    FC27TacticsInput,
    build_fc27_tactics_report,
    compute_tactics_report_hash,
)

REF_NOW = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)
REF_NOW_ISO = "2026-10-30T12:00:00Z"


@pytest.fixture(autouse=True)
def _block_network_and_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Network or DNS access is strictly forbidden in Director unit tests")

    monkeypatch.setattr(socket, "getaddrinfo", _forbidden)
    monkeypatch.setattr(socket, "gethostbyname", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", _forbidden)


def _sha256(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _make_director_input(count: int = 10) -> FC27DirectorInput:
    matches: list[dict[str, Any]] = []
    for i in range(1, count + 1):
        m_id = f"m_dir_{i:04d}"
        dt = (REF_NOW - timedelta(hours=i * 12)).strftime("%Y-%m-%dT%H:%M:%SZ")
        res = "WIN" if i % 2 == 1 else "DRAW"
        gf, ga = (2, 1) if res == "WIN" else (1, 1)
        matches.append(
            {
                "match_id": m_id,
                "match_hash": _sha256(f"dir:{m_id}:{res}:{gf}:{ga}"),
                "payload_sha256": _sha256(f"payload:{m_id}"),
                "club_id": "330323",
                "club_name": "East Coasters",
                "platform": "common-gen5",
                "game_version": "FC27",
                "played_at": dt,
                "completion_state": "COMPLETED",
                "result": res,
                "goals_for": gf,
                "goals_against": ga,
                "formation": "4-3-3(2)" if i % 2 == 1 else "4-2-3-1",
                "opponent_name": f"Opponent Club {i:02d}",
                "match_type": "LEAGUE",
                "club_side": "HOME" if i % 2 == 1 else "AWAY",
                "possession_pct": 53.0,
                "team_shots": 9,
                "team_shots_on_target": 5,
                "team_passes_attempted": 115,
                "team_passes_completed": 98,
                "team_tackles_attempted": 16,
                "team_tackles_won": 12,
                "team_interceptions": 8,
                "team_saves": 3,
                "data_quality_status": "ACCEPTED",
                "player_appearances": [
                    {
                        "player_ref": "plr_ecf_000000000001",
                        "position": "ST",
                        "role": "Advanced Forward",
                        "role_focus": "Attack",
                        "build_reference": "bld_ecf_st_01",
                        "started": True,
                        "minutes_played": 90,
                        "match_rating": 7.8,
                        "goals": gf,
                        "assists": 0,
                        "passes_attempted": 20,
                        "passes_completed": 17,
                        "tackles_attempted": 3,
                        "tackles_won": 2,
                        "interceptions": 1,
                        "shots": 4,
                        "shots_on_target": 3,
                        "saves": 0,
                    },
                    {
                        "player_ref": "plr_ecf_000000000002",
                        "position": "CM",
                        "role": "Box-to-Box",
                        "role_focus": "Balanced",
                        "build_reference": "bld_ecf_cm_01",
                        "started": True,
                        "minutes_played": 90,
                        "match_rating": 7.4,
                        "goals": 0,
                        "assists": 1,
                        "passes_attempted": 28,
                        "passes_completed": 24,
                        "tackles_attempted": 5,
                        "tackles_won": 4,
                        "interceptions": 2,
                        "shots": 1,
                        "shots_on_target": 1,
                        "saves": 0,
                    },
                ],
            }
        )
    s_in = FC27SearchInput(analysis_generated_at=REF_NOW, candidate_matches=matches)
    ctx = build_fc27_search_context(s_in)
    scout = build_fc27_scout_report(ctx)
    analyst = build_fc27_analyst_report(
        FC27AnalystInput(
            search_context=ctx,
            scout_report=scout,
            analysis_generated_at=REF_NOW_ISO,
            search_context_hash=ctx.context_hash,
            scout_report_hash=scout.report_hash,
        )
    )
    coach = build_fc27_coach_report(
        FC27CoachInput(
            search_context=ctx,
            scout_report=scout,
            analyst_report=analyst,
            coach_generated_at=REF_NOW_ISO,
            search_context_hash=ctx.context_hash,
            scout_report_hash=scout.report_hash,
            analyst_report_hash=analyst.report_hash,
        )
    )
    tactics = build_fc27_tactics_report(
        FC27TacticsInput(
            search_context=ctx,
            scout_report=scout,
            analyst_report=analyst,
            coach_report=coach,
            tactics_generated_at=REF_NOW_ISO,
            search_context_hash=ctx.context_hash,
            scout_report_hash=scout.report_hash,
            analyst_report_hash=analyst.report_hash,
            coach_report_hash=coach.report_hash,
        )
    )
    return FC27DirectorInput(
        search_context=ctx,
        scout_report=scout,
        analyst_report=analyst,
        coach_report=coach,
        tactics_report=tactics,
        director_generated_at=REF_NOW_ISO,
        search_context_hash=ctx.context_hash,
        scout_report_hash=scout.report_hash,
        analyst_report_hash=analyst.report_hash,
        coach_report_hash=coach.report_hash,
        tactics_report_hash=tactics.report_hash,
    )


def test_76_director_valid_input_accepted() -> None:
    """76. Director valid input accepted."""
    dir_in = _make_director_input(10)
    report = build_fc27_director_report(dir_in)
    assert isinstance(report, FC27DirectorReport)
    assert report.identity.club_id == "330323"
    assert report.identity.club_name == "East Coasters"
    assert report.review_status == DIRECTOR_REVIEW_STATUS
    assert len(report.experiment_sequence) >= 1


def test_77_director_prohibited_output_type_rejected() -> None:
    """77. Director prohibited output type rejected."""
    for prohibited in PROHIBITED_DIRECTOR_OUTPUT_TYPES:
        with pytest.raises(FC27DirectorValidationError, match="prohibited"):
            validate_director_output_type(prohibited)
    for allowed in ALLOWED_DIRECTOR_OUTPUT_TYPES:
        assert validate_director_output_type(allowed) == allowed


def test_78_director_reconciles_conflicting_candidates_by_deferring_or_sequencing() -> None:
    """78. Director reconciles conflicting candidates by deferring or sequencing."""
    dir_in = _make_director_input(10)
    # Create an extra overlapping candidate in the same category as the first candidate
    first_cand = dir_in.tactics_report.candidates[0]
    overlapping_cand = first_cand.model_copy(
        update={"candidate_id": f"{first_cand.candidate_id}_overlap"}
    )
    updated_tactics = dir_in.tactics_report.model_copy(
        update={"candidates": (*dir_in.tactics_report.candidates, overlapping_cand)}
    )
    new_tac_hash = compute_tactics_report_hash(updated_tactics)
    updated_tactics = updated_tactics.model_copy(update={"report_hash": new_tac_hash})
    updated_in = dir_in.model_copy(
        update={
            "tactics_report": updated_tactics,
            "tactics_report_hash": new_tac_hash,
        }
    )
    report = build_fc27_director_report(updated_in)
    seq_categories = [item.category for item in report.experiment_sequence]
    assert len(seq_categories) == len(set(seq_categories))
    deferred_ids = {d.candidate_id for d in report.deferred_candidates}
    assert overlapping_cand.candidate_id in deferred_ids


def test_79_director_preserves_counter_evidence_confounders_data_gaps_and_unresolved_questions() -> None:
    """79. Director preserves counter-evidence, confounders, data gaps, and unresolved questions."""
    dir_in = _make_director_input(10)
    report = build_fc27_director_report(dir_in)
    assert len(report.confounders) == len(dir_in.analyst_report.confounders)
    assert len(report.data_gaps) == len(dir_in.analyst_report.data_gaps)
    assert len(report.unresolved_questions) == len(dir_in.analyst_report.unresolved_questions)
    assert isinstance(report.counter_evidence, tuple)


def test_80_director_confidence_never_exceeds_upstream_layers() -> None:
    """80. Director confidence never exceeds upstream layers."""
    dir_in = _make_director_input(10)
    report = build_fc27_director_report(dir_in)
    assert (
        CONFIDENCE_TIER_RANK[report.confidence_tier]
        <= CONFIDENCE_TIER_RANK[dir_in.analyst_report.overall_confidence]
    )
    assert (
        CONFIDENCE_TIER_RANK[report.confidence_tier]
        <= CONFIDENCE_TIER_RANK[dir_in.coach_report.overall_confidence]
    )
    assert (
        CONFIDENCE_TIER_RANK[report.confidence_tier]
        <= CONFIDENCE_TIER_RANK[dir_in.tactics_report.overall_confidence]
    )
    for item in report.experiment_sequence:
        assert (
            CONFIDENCE_TIER_RANK[item.confidence_tier]
            <= CONFIDENCE_TIER_RANK[report.confidence_tier]
        )


def test_81_director_blocking_contradiction_fails_closed() -> None:
    """81. Director blocking contradiction fails closed."""
    dir_in = _make_director_input(10)
    blocking = FC27Contradiction(
        contradiction_id="ctr_dir_block_01",
        reason_code="METRIC_MISMATCH",
        severity="BLOCKING",
        description="In the available East Coasters sample, match totals conflict across layers.",
        affected_references=("m_dir_0001",),
        resolution_status="UNRESOLVED",
    )
    bad_tactics = dir_in.tactics_report.model_copy(
        update={"contradictions": (blocking,)}
    )
    new_tac_hash = compute_tactics_report_hash(bad_tactics)
    bad_tactics = bad_tactics.model_copy(update={"report_hash": new_tac_hash})
    bad_in = dir_in.model_copy(
        update={
            "tactics_report": bad_tactics,
            "tactics_report_hash": new_tac_hash,
        }
    )
    with pytest.raises(FC27DirectorContradictionError, match="BLOCKING"):
        build_fc27_director_report(bad_in)


def test_82_director_recommendation_approval_and_dispatch_flags_false() -> None:
    """82. Director recommendation, approval, and dispatch flags false."""
    report = build_fc27_director_report(_make_director_input(10))
    assert report.recommendation is False
    assert report.approvable is False
    assert report.dispatchable is False
    assert report.causal_claim is False
    for item in report.experiment_sequence:
        assert item.recommendation is False
        assert item.approvable is False
        assert item.dispatchable is False
        assert item.causal_claim is False


def test_83_director_output_hash_deterministic() -> None:
    """83. Director output hash deterministic."""
    dir_in = _make_director_input(10)
    r1 = build_fc27_director_report(dir_in)
    r2 = build_fc27_director_report(copy.deepcopy(dir_in))
    assert r1.report_hash == r2.report_hash
    assert r1.model_dump(mode="json") == r2.model_dump(mode="json")
