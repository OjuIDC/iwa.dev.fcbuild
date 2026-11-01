"""Unit tests for East Coasters FC 27 Coach Hypothesis Generation (Tests #51-#64)."""
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
    FC27CounterEvidence,
    build_fc27_analyst_report,
)
from src.fc27_coach import (
    ALLOWED_COACH_ARTIFACT_TYPES,
    PROHIBITED_COACH_ARTIFACT_TYPES,
    FC27CoachContradictionError,
    FC27CoachHypothesis,
    FC27CoachInput,
    FC27CoachReport,
    FC27CoachValidationError,
    build_fc27_coach_report,
    validate_coach_artifact_type,
)
from src.fc27_search_scout import (
    CONFIDENCE_TIER_RANK,
    FC27EvidenceReference,
    FC27PrivacyViolationError,
    FC27SearchInput,
    build_fc27_scout_report,
    build_fc27_search_context,
)

REF_NOW = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)
REF_NOW_ISO = "2026-10-30T12:00:00Z"


@pytest.fixture(autouse=True)
def _block_network_and_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Network or DNS access is strictly forbidden in Coach unit tests")

    monkeypatch.setattr(socket, "getaddrinfo", _forbidden)
    monkeypatch.setattr(socket, "gethostbyname", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", _forbidden)


def _sha256(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _make_coach_input(count: int = 10) -> FC27CoachInput:
    matches: list[dict[str, Any]] = []
    for i in range(1, count + 1):
        m_id = f"m_coach_{i:04d}"
        dt = (REF_NOW - timedelta(hours=i * 12)).strftime("%Y-%m-%dT%H:%M:%SZ")
        res = "WIN" if i % 2 == 1 else "DRAW"
        gf, ga = (2, 1) if res == "WIN" else (1, 1)
        matches.append(
            {
                "match_id": m_id,
                "match_hash": _sha256(f"coach:{m_id}:{res}:{gf}:{ga}"),
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
    return FC27CoachInput(
        search_context=ctx,
        scout_report=scout,
        analyst_report=analyst,
        coach_generated_at=REF_NOW_ISO,
        search_context_hash=ctx.context_hash,
        scout_report_hash=scout.report_hash,
        analyst_report_hash=analyst.report_hash,
    )


def test_51_coach_valid_input_accepted() -> None:
    """51. Coach valid input accepted."""
    coach_in = _make_coach_input(10)
    report = build_fc27_coach_report(coach_in)
    assert isinstance(report, FC27CoachReport)
    assert report.identity.club_id == "330323"
    assert report.identity.club_name == "East Coasters"
    assert len(report.hypotheses) >= 1


def test_52_coach_hash_mismatch_rejected() -> None:
    """52. Coach hash mismatch rejected."""
    coach_in = _make_coach_input(10)
    bad = coach_in.model_copy(update={"analyst_report_hash": "a" * 64})
    with pytest.raises(FC27CoachValidationError, match="analyst_report_hash"):
        build_fc27_coach_report(bad)


def test_53_coach_prohibited_artifact_type_rejected() -> None:
    """53. Coach prohibited artifact type rejected."""
    for prohibited in PROHIBITED_COACH_ARTIFACT_TYPES:
        with pytest.raises(FC27CoachValidationError, match="prohibited"):
            validate_coach_artifact_type(prohibited)
    for allowed in ALLOWED_COACH_ARTIFACT_TYPES:
        assert validate_coach_artifact_type(allowed) == allowed


def test_54_coach_hypothesis_requires_supporting_and_counter_evidence_fields() -> None:
    """54. Coach hypothesis requires supporting and counter-evidence fields."""
    report = build_fc27_coach_report(_make_coach_input(10))
    for hyp in report.hypotheses:
        dumped = hyp.model_dump(mode="json")
        assert "supporting_evidence" in dumped
        assert "counter_evidence" in dumped
        assert isinstance(hyp.supporting_evidence, tuple)
        assert isinstance(hyp.counter_evidence, tuple)


def test_55_coach_hypothesis_requires_confounders() -> None:
    """55. Coach hypothesis requires confounders."""
    report = build_fc27_coach_report(_make_coach_input(10))
    first = report.hypotheses[0]
    assert len(first.confounders) >= 1
    raw = first.model_dump(mode="python")
    raw["confounders"] = ()
    with pytest.raises((FC27CoachValidationError, ValidationError), match="confounders"):
        FC27CoachHypothesis.model_validate(raw)


def test_56_coach_hypothesis_requires_competing_explanations() -> None:
    """56. Coach hypothesis requires competing explanations."""
    report = build_fc27_coach_report(_make_coach_input(10))
    first = report.hypotheses[0]
    assert len(first.competing_explanations) >= 1
    raw = first.model_dump(mode="python")
    raw["competing_explanations"] = ()
    with pytest.raises((FC27CoachValidationError, ValidationError), match="competing_explanations"):
        FC27CoachHypothesis.model_validate(raw)


def test_57_coach_hypothesis_requires_falsification_condition() -> None:
    """57. Coach hypothesis requires falsification condition."""
    report = build_fc27_coach_report(_make_coach_input(10))
    first = report.hypotheses[0]
    assert first.falsification_condition
    raw = first.model_dump(mode="python")
    raw["falsification_condition"] = ""
    with pytest.raises(ValidationError):
        FC27CoachHypothesis.model_validate(raw)


def test_58_coach_hypothesis_requires_measurement_plan() -> None:
    """58. Coach hypothesis requires measurement plan."""
    report = build_fc27_coach_report(_make_coach_input(10))
    first = report.hypotheses[0]
    assert first.measurement_plan
    assert len(report.measurement_plans) == len(report.hypotheses)
    raw = first.model_dump(mode="python")
    raw["measurement_plan"] = ""
    with pytest.raises(ValidationError):
        FC27CoachHypothesis.model_validate(raw)


def test_59_coach_confidence_never_exceeds_analyst_confidence() -> None:
    """59. Coach confidence never exceeds Analyst confidence."""
    report = build_fc27_coach_report(_make_coach_input(10))
    for hyp in report.hypotheses:
        assert (
            CONFIDENCE_TIER_RANK[hyp.confidence_tier]
            <= CONFIDENCE_TIER_RANK[hyp.analyst_confidence_ceiling]
        )
        assert (
            CONFIDENCE_TIER_RANK[hyp.confidence_tier]
            <= CONFIDENCE_TIER_RANK[report.overall_confidence]
        )
    first = report.hypotheses[0]
    raw = first.model_dump(mode="python")
    raw["analyst_confidence_ceiling"] = "EXPLORATORY"
    raw["confidence_tier"] = "HIGH_CONFIDENCE"
    with pytest.raises((FC27CoachValidationError, ValidationError), match="exceeds"):
        FC27CoachHypothesis.model_validate(raw)


def test_60_coach_blocking_contradiction_fails_closed() -> None:
    """60. Coach blocking contradiction fails closed."""
    coach_in = _make_coach_input(10)
    blocking = FC27Contradiction(
        contradiction_id="ctr_block_01",
        reason_code="METRIC_MISMATCH",
        severity="BLOCKING",
        description="In the available East Coasters sample, match totals conflict across layers.",
        affected_references=("m_coach_0001",),
        resolution_status="UNRESOLVED",
    )
    bad_analyst = coach_in.analyst_report.model_copy(
        update={"contradictions": (blocking,)}
    )
    from src.fc27_coach import _recompute_analyst_hash

    new_hash = _recompute_analyst_hash(bad_analyst)
    bad_analyst = bad_analyst.model_copy(update={"report_hash": new_hash})
    bad_in = coach_in.model_copy(
        update={
            "analyst_report": bad_analyst,
            "analyst_report_hash": new_hash,
        }
    )
    with pytest.raises(FC27CoachContradictionError, match="BLOCKING"):
        build_fc27_coach_report(bad_in)


def test_61_coach_disqualifying_counter_evidence_blocks_hypothesis() -> None:
    """61. Coach disqualifying counter-evidence blocks hypothesis."""
    report = build_fc27_coach_report(_make_coach_input(10))
    first = report.hypotheses[0]
    raw = first.model_dump(mode="python")
    raw["counter_evidence"] = (
        FC27CounterEvidence(
            counter_evidence_id="ce_disq_01",
            statement="In the available East Coasters sample, counter-evidence invalidates the pattern.",
            metric="excluded_quality_records",
            observed_value=3,
            comparison_value=0,
            sample_size=10,
            confidence_tier="MODERATE_CONFIDENCE",
            evidence_references=first.evidence_references[:1],
            impact="DISQUALIFYING",
        ),
    )
    with pytest.raises((FC27CoachValidationError, ValidationError), match="DISQUALIFYING"):
        FC27CoachHypothesis.model_validate(raw)


def test_62_coach_language_validator_rejects_prescriptive_and_causal_claims() -> None:
    """62. Coach language validator rejects prescriptive and causal claims."""
    report = build_fc27_coach_report(_make_coach_input(10))
    first = report.hypotheses[0]
    for bad_text in (
        "In the available East Coasters sample, we recommend switching to 4-2-3-1 immediately.",
        "In the available East Coasters sample, this formation causes wins and guarantees victory.",
        "In the available East Coasters sample, you must bench player plr_ecf_000000000001.",
    ):
        raw = first.model_dump(mode="python")
        raw["statement"] = bad_text
        with pytest.raises(Exception):
            FC27CoachHypothesis.model_validate(raw)


def test_63_coach_privacy_scanner_rejects_gamer_tags() -> None:
    """63. Coach privacy scanner rejects gamer tags."""
    coach_in = _make_coach_input(10)
    raw = coach_in.model_dump(mode="python")
    raw["provenance"]["gamer_tag"] = "LeakyHandle99"
    with pytest.raises((FC27PrivacyViolationError, ValidationError)):
        FC27CoachInput.model_validate(raw)


def test_64_coach_output_hash_deterministic() -> None:
    """64. Coach output hash deterministic."""
    coach_in = _make_coach_input(10)
    r1 = build_fc27_coach_report(coach_in)
    r2 = build_fc27_coach_report(copy.deepcopy(coach_in))
    assert r1.report_hash == r2.report_hash
    assert r1.model_dump(mode="json") == r2.model_dump(mode="json")
    assert r1.recommendation is False
    assert r1.approvable is False
    assert r1.dispatchable is False
    assert r1.causal_claim is False
