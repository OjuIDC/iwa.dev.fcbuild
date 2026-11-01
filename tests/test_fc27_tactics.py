"""Unit tests for East Coasters FC 27 Tactics Experimental Candidate Generation (Tests #65-#75)."""
from __future__ import annotations

import copy
import hashlib
import socket
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from src.fc27_analyst import FC27AnalystInput, build_fc27_analyst_report
from src.fc27_coach import FC27CoachInput, build_fc27_coach_report
from src.fc27_search_scout import (
    CONFIDENCE_TIER_RANK,
    FC27PrivacyViolationError,
    FC27SearchInput,
    build_fc27_scout_report,
    build_fc27_search_context,
)
from src.fc27_tactics import (
    ALLOWED_TACTICS_CANDIDATE_TYPES,
    EXPERIMENTAL_ONLY_STATUS,
    PROHIBITED_TACTICS_CANDIDATE_TYPES,
    FC27TacticalCandidate,
    FC27TacticsInput,
    FC27TacticsReport,
    FC27TacticsValidationError,
    build_fc27_tactics_report,
    validate_tactics_candidate_type,
)

REF_NOW = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)
REF_NOW_ISO = "2026-10-30T12:00:00Z"


@pytest.fixture(autouse=True)
def _block_network_and_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Network or DNS access is strictly forbidden in Tactics unit tests")

    monkeypatch.setattr(socket, "getaddrinfo", _forbidden)
    monkeypatch.setattr(socket, "gethostbyname", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", _forbidden)


def _sha256(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _make_tactics_input(count: int = 10) -> FC27TacticsInput:
    matches: list[dict[str, Any]] = []
    for i in range(1, count + 1):
        m_id = f"m_tac_{i:04d}"
        dt = (REF_NOW - timedelta(hours=i * 12)).strftime("%Y-%m-%dT%H:%M:%SZ")
        res = "WIN" if i % 2 == 1 else "DRAW"
        gf, ga = (2, 1) if res == "WIN" else (1, 1)
        matches.append(
            {
                "match_id": m_id,
                "match_hash": _sha256(f"tac:{m_id}:{res}:{gf}:{ga}"),
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
    return FC27TacticsInput(
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


def test_65_tactics_valid_input_accepted() -> None:
    """65. Tactics valid input accepted."""
    tac_in = _make_tactics_input(10)
    report = build_fc27_tactics_report(tac_in)
    assert isinstance(report, FC27TacticsReport)
    assert report.identity.club_id == "330323"
    assert report.identity.club_name == "East Coasters"
    assert len(report.candidates) >= 1


def test_66_tactics_prohibited_candidate_type_rejected() -> None:
    """66. Tactics prohibited candidate type rejected."""
    for prohibited in PROHIBITED_TACTICS_CANDIDATE_TYPES:
        with pytest.raises(FC27TacticsValidationError, match="prohibited"):
            validate_tactics_candidate_type(prohibited)
    for allowed in ALLOWED_TACTICS_CANDIDATE_TYPES:
        assert validate_tactics_candidate_type(allowed) == allowed


def test_67_tactics_candidate_requires_coach_hypothesis_reference() -> None:
    """67. Tactics candidate requires Coach hypothesis reference."""
    report = build_fc27_tactics_report(_make_tactics_input(10))
    first = report.candidates[0]
    assert len(first.coach_hypothesis_references) >= 1
    raw = first.model_dump(mode="python")
    raw["coach_hypothesis_references"] = ()
    with pytest.raises((FC27TacticsValidationError, ValidationError), match="coach_hypothesis_references"):
        FC27TacticalCandidate.model_validate(raw)


def test_68_tactics_candidate_requires_success_failure_and_stop_conditions() -> None:
    """68. Tactics candidate requires success, failure, and stop conditions."""
    report = build_fc27_tactics_report(_make_tactics_input(10))
    first = report.candidates[0]
    assert first.success_metric
    assert first.failure_metric
    assert first.stop_condition
    for field_name in ("success_metric", "failure_metric", "stop_condition"):
        raw = first.model_dump(mode="python")
        raw[field_name] = ""
        with pytest.raises(ValidationError):
            FC27TacticalCandidate.model_validate(raw)


def test_69_tactics_candidate_requires_risks_and_confounders() -> None:
    """69. Tactics candidate requires risks and confounders."""
    report = build_fc27_tactics_report(_make_tactics_input(10))
    first = report.candidates[0]
    assert len(first.risks) >= 1
    assert len(first.confounders) >= 1
    for field_name in ("risks", "confounders"):
        raw = first.model_dump(mode="python")
        raw[field_name] = ()
        with pytest.raises((FC27TacticsValidationError, ValidationError), match=field_name):
            FC27TacticalCandidate.model_validate(raw)


def test_70_tactics_confidence_never_exceeds_coach_and_analyst_confidence() -> None:
    """70. Tactics confidence never exceeds Coach and Analyst confidence."""
    tac_in = _make_tactics_input(10)
    report = build_fc27_tactics_report(tac_in)
    for cand in report.candidates:
        assert (
            CONFIDENCE_TIER_RANK[cand.confidence_tier]
            <= CONFIDENCE_TIER_RANK[cand.coach_confidence_ceiling]
        )
        assert (
            CONFIDENCE_TIER_RANK[cand.confidence_tier]
            <= CONFIDENCE_TIER_RANK[tac_in.coach_report.overall_confidence]
        )
        assert (
            CONFIDENCE_TIER_RANK[cand.confidence_tier]
            <= CONFIDENCE_TIER_RANK[tac_in.analyst_report.overall_confidence]
        )
    first = report.candidates[0]
    raw = first.model_dump(mode="python")
    raw["coach_confidence_ceiling"] = "EXPLORATORY"
    raw["confidence_tier"] = "HIGH_CONFIDENCE"
    with pytest.raises((FC27TacticsValidationError, ValidationError), match="exceeds"):
        FC27TacticalCandidate.model_validate(raw)


def test_71_tactics_experimental_status_enforced() -> None:
    """71. Tactics experimental status enforced."""
    report = build_fc27_tactics_report(_make_tactics_input(10))
    assert report.experimental_status == EXPERIMENTAL_ONLY_STATUS
    for cand in report.candidates:
        assert cand.experimental_status == EXPERIMENTAL_ONLY_STATUS
        assert cand.review_required is True
    first = report.candidates[0]
    raw = first.model_dump(mode="python")
    raw["experimental_status"] = "PRODUCTION_APPROVED"
    with pytest.raises(ValidationError):
        FC27TacticalCandidate.model_validate(raw)


def test_72_tactics_recommendation_approval_and_dispatch_flags_false() -> None:
    """72. Tactics recommendation, approval, and dispatch flags false."""
    report = build_fc27_tactics_report(_make_tactics_input(10))
    assert report.recommendation is False
    assert report.approvable is False
    assert report.dispatchable is False
    assert report.causal_claim is False
    for cand in report.candidates:
        assert cand.recommendation is False
        assert cand.approvable is False
        assert cand.dispatchable is False
        assert cand.causal_claim is False


def test_73_tactics_language_validator_rejects_mandatory_lineup_commands() -> None:
    """73. Tactics language validator rejects mandatory lineup commands."""
    report = build_fc27_tactics_report(_make_tactics_input(10))
    first = report.candidates[0]
    for bad_text in (
        "In the available East Coasters sample, you must bench player plr_ecf_000000000001.",
        "In the available East Coasters sample, we recommend switching to 4-2-3-1 immediately.",
        "In the available East Coasters sample, this formation guarantees win probability.",
    ):
        raw = first.model_dump(mode="python")
        raw["expected_observation"] = bad_text
        with pytest.raises(Exception):
            FC27TacticalCandidate.model_validate(raw)


def test_74_tactics_privacy_scanner_rejects_raw_identifiers() -> None:
    """74. Tactics privacy scanner rejects raw identifiers."""
    tac_in = _make_tactics_input(10)
    raw = tac_in.model_dump(mode="python")
    raw["provenance"]["persona_id"] = "ExposedGamerAlias"
    with pytest.raises((FC27PrivacyViolationError, ValidationError)):
        FC27TacticsInput.model_validate(raw)


def test_75_tactics_output_hash_deterministic() -> None:
    """75. Tactics output hash deterministic."""
    tac_in = _make_tactics_input(10)
    r1 = build_fc27_tactics_report(tac_in)
    r2 = build_fc27_tactics_report(copy.deepcopy(tac_in))
    assert r1.report_hash == r2.report_hash
    assert r1.model_dump(mode="json") == r2.model_dump(mode="json")
