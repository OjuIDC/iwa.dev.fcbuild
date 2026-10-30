"""Comprehensive unit tests for East Coasters Analyst Evidence Synthesis (PART Q, Items 1-110).

All tests use synthetic normalized Search and Scout fixtures only and block external network and DNS access.
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
from pydantic import ValidationError

from src.fc27_analyst import (
    ALLOWED_CLUSTER_CATEGORIES,
    ALLOWED_CONFOUNDER_NAMES,
    FORMULA_VERSION,
    MAX_ANALYST_FINDINGS,
    MAX_COMPETING_EXPLANATIONS,
    MAX_CONFOUNDERS,
    MAX_COUNTER_EVIDENCE_ITEMS,
    MAX_DATA_GAPS,
    MAX_EVIDENCE_CLUSTERS,
    MAX_UNRESOLVED_QUESTIONS,
    NOT_A_RECOMMENDATION_STATUS,
    OBSERVATIONAL_ONLY_STATUS,
    FC27AnalystContradictionError,
    FC27AnalystError,
    FC27AnalystFinding,
    FC27AnalystInput,
    FC27AnalystLanguageError,
    FC27AnalystReport,
    FC27AnalystValidationError,
    FC27CompetingExplanation,
    FC27Confounder,
    FC27CounterEvidence,
    FC27EvidenceCluster,
    FC27SupportedObservation,
    FC27UnresolvedQuestion,
    analyze_counter_evidence,
    build_evidence_clusters,
    build_fc27_analyst_report,
    detect_evidence_contradictions,
    generate_competing_explanations,
    generate_unresolved_questions,
    identify_confounders,
    identify_data_gaps,
    validate_analyst_evidence_language,
    validate_analyst_input,
)
from src.fc27_search_scout import (
    FC27EvidenceReference,
    FC27Hypothesis,
    FC27PrivacyViolationError,
    FC27ScopeViolationError,
    FC27ScoutReport,
    FC27SearchContext,
    FC27SearchInput,
    FC27SearchScoutError,
    build_fc27_scout_report,
    build_fc27_search_context,
)

REF_NOW = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)
REF_NOW_ISO = "2026-10-30T12:00:00Z"


@pytest.fixture(autouse=True)
def _block_network_and_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block all network and DNS calls in pure Analyst unit tests."""

    def _forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Network or DNS access is strictly forbidden in Analyst tests")

    monkeypatch.setattr(socket, "getaddrinfo", _forbidden)
    monkeypatch.setattr(socket, "gethostbyname", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", _forbidden)


def _sha256(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _make_matches(
    count: int = 12,
    *,
    include_formations: bool = True,
    include_roles: bool = True,
    include_builds: bool = True,
    include_partnerships: bool = True,
    include_telemetry: bool = True,
    repeated_opponent: bool = True,
    multi_match_types: bool = True,
    recent_slump: bool = False,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for i in range(1, count + 1):
        m_id = f"m_an_{i:04d}"
        dt = (REF_NOW - timedelta(hours=i * 3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        if recent_slump and i <= 5:
            res = "LOSS"
            gf, ga = 0, 2
        else:
            if i % 4 == 0:
                res, gf, ga = "LOSS", 1, 2
            elif i % 3 == 0:
                res, gf, ga = "DRAW", 1, 1
            else:
                res, gf, ga = "WIN", 2, 0

        formation = (
            ("4-3-3(2)" if i % 2 == 1 else "4-2-3-1")
            if include_formations
            else None
        )
        mtype = (
            ("LEAGUE" if i % 3 != 0 else "PLAYOFF")
            if multi_match_types
            else "LEAGUE"
        )
        opp_name = (
            ("Synthetic Rivals FC" if i % 2 == 1 else f"Opponent Club {i:02d}")
            if repeated_opponent
            else f"Unique Opponent {i:02d}"
        )

        apps: list[dict[str, Any]] = [
            {
                "player_ref": "plr_ecf_000000000001",
                "position": "ST" if i % 2 == 1 else "LW",
                "role": ("Advanced Forward" if i % 2 == 1 else "Inside Forward") if include_roles else None,
                "role_focus": "Attack" if include_roles else None,
                "build_reference": "bld_ecf_st_01" if include_builds else None,
                "started": True,
                "minutes_played": 90,
                "match_rating": 7.9 if res == "WIN" else 6.5,
                "goals": gf,
                "assists": 0,
                "passes_attempted": 20 if include_telemetry else None,
                "passes_completed": 17 if include_telemetry else None,
                "tackles_attempted": 3 if include_telemetry else None,
                "tackles_won": 2 if include_telemetry else None,
                "interceptions": 1 if include_telemetry else None,
                "shots": 4 if include_telemetry else None,
                "shots_on_target": 3 if include_telemetry else None,
                "saves": 0 if include_telemetry else None,
            }
        ]
        if include_partnerships:
            apps.append(
                {
                    "player_ref": "plr_ecf_000000000002",
                    "position": "CM",
                    "role": "Box-to-Box" if include_roles else None,
                    "role_focus": "Balanced" if include_roles else None,
                    "build_reference": "bld_ecf_cm_01" if include_builds else None,
                    "started": True,
                    "minutes_played": 90,
                    "match_rating": 7.5 if res == "WIN" else 6.4,
                    "goals": 0,
                    "assists": gf,
                    "passes_attempted": 28 if include_telemetry else None,
                    "passes_completed": 24 if include_telemetry else None,
                    "tackles_attempted": 6 if include_telemetry else None,
                    "tackles_won": 5 if include_telemetry else None,
                    "interceptions": 2 if include_telemetry else None,
                    "shots": 1 if include_telemetry else None,
                    "shots_on_target": 1 if include_telemetry else None,
                    "saves": 0 if include_telemetry else None,
                }
            )

        matches.append(
            {
                "match_id": m_id,
                "match_hash": _sha256(f"an:{m_id}:{res}:{gf}:{ga}"),
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
                "formation": formation,
                "opponent_name": opp_name,
                "match_type": mtype,
                "club_side": "HOME" if i % 2 == 1 else "AWAY",
                "possession_pct": 54.0 if include_telemetry else None,
                "team_shots": 10 if include_telemetry else None,
                "team_shots_on_target": 6 if include_telemetry else None,
                "team_passes_attempted": 120 if include_telemetry else None,
                "team_passes_completed": 102 if include_telemetry else None,
                "team_tackles_attempted": 18 if include_telemetry else None,
                "team_tackles_won": 13 if include_telemetry else None,
                "team_interceptions": 9 if include_telemetry else None,
                "team_saves": 3 if include_telemetry else None,
                "data_quality_status": "ACCEPTED",
                "player_appearances": apps,
            }
        )
    return matches


def _make_analyst_input(
    count: int = 12,
    **kwargs: Any,
) -> FC27AnalystInput:
    matches = _make_matches(count, **kwargs)
    s_in = FC27SearchInput(
        analysis_generated_at=REF_NOW,
        candidate_matches=matches,
    )
    ctx = build_fc27_search_context(s_in)
    rep = build_fc27_scout_report(ctx)
    return FC27AnalystInput(
        search_context=ctx,
        scout_report=rep,
        analysis_generated_at=REF_NOW_ISO,
        search_context_hash=ctx.context_hash,
        scout_report_hash=rep.report_hash,
    )


# ==============================================================================
# 1-17: Input and Identity Validation Tests
# ==============================================================================


def test_001_valid_search_and_scout_inputs_accepted() -> None:
    analyst_in = _make_analyst_input(12)
    validated, contradictions = validate_analyst_input(analyst_in)
    assert validated.search_context.identity.club_id == "330323"
    assert validated.scout_report.identity.club_name == "East Coasters"
    assert all(c.severity != "BLOCKING" for c in contradictions)


def test_002_wrong_club_id_rejected() -> None:
    analyst_in = _make_analyst_input(6)
    raw = analyst_in.model_dump(mode="python")
    raw["search_context"]["identity"]["club_id"] = "999999"
    with pytest.raises((FC27ScopeViolationError, ValidationError)):
        validate_analyst_input(raw)


def test_003_wrong_club_name_rejected() -> None:
    analyst_in = _make_analyst_input(6)
    raw = analyst_in.model_dump(mode="python")
    raw["search_context"]["identity"]["club_name"] = "West Coasters"
    with pytest.raises((FC27ScopeViolationError, ValidationError)):
        validate_analyst_input(raw)


def test_004_wrong_platform_rejected() -> None:
    analyst_in = _make_analyst_input(6)
    raw = analyst_in.model_dump(mode="python")
    raw["search_context"]["identity"]["platform"] = "common-gen4"
    with pytest.raises((FC27ScopeViolationError, ValidationError)):
        validate_analyst_input(raw)


def test_005_wrong_game_version_rejected() -> None:
    analyst_in = _make_analyst_input(6)
    raw = analyst_in.model_dump(mode="python")
    raw["search_context"]["identity"]["game_version"] = "FC26"
    with pytest.raises((FC27ScopeViolationError, ValidationError)):
        validate_analyst_input(raw)


def test_006_search_scout_identity_mismatch_rejected() -> None:
    analyst_in = _make_analyst_input(6)
    bad_ctx = copy.deepcopy(analyst_in.search_context)
    object.__setattr__(
        bad_ctx.identity, "club_id", "999999"
    )
    contradictions = detect_evidence_contradictions(bad_ctx, analyst_in.scout_report)
    codes = {c.reason_code for c in contradictions}
    assert "IDENTITY_MISMATCH" in codes


def test_007_formula_version_mismatch_rejected() -> None:
    analyst_in = _make_analyst_input(6)
    raw = analyst_in.model_dump(mode="python")
    raw["formula_version"] = "fc27-v9.9"
    with pytest.raises((FC27AnalystValidationError, ValidationError)):
        validate_analyst_input(raw)


def test_008_search_hash_mismatch_rejected() -> None:
    analyst_in = _make_analyst_input(6)
    tampered = analyst_in.model_copy(update={"search_context_hash": "a" * 64})
    with pytest.raises(FC27AnalystContradictionError, match="SEARCH_HASH_MISMATCH"):
        validate_analyst_input(tampered)


def test_009_scout_hash_mismatch_rejected() -> None:
    analyst_in = _make_analyst_input(6)
    tampered = analyst_in.model_copy(update={"scout_report_hash": "b" * 64})
    with pytest.raises(FC27AnalystContradictionError, match="SCOUT_HASH_MISMATCH"):
        validate_analyst_input(tampered)


def test_010_naive_timestamp_rejected() -> None:
    analyst_in = _make_analyst_input(6)
    raw = analyst_in.model_dump(mode="python")
    raw["analysis_generated_at"] = "2026-10-30T12:00:00"
    with pytest.raises((FC27SearchScoutError, ValidationError)):
        validate_analyst_input(raw)


def test_011_future_analysis_timestamp_rejected_according_to_policy() -> None:
    analyst_in = _make_analyst_input(6)
    past_ts = (REF_NOW - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    past_input = analyst_in.model_copy(update={"analysis_generated_at": past_ts})
    with pytest.raises(FC27AnalystValidationError, match="future"):
        validate_analyst_input(past_input)


def test_012_nan_rejected() -> None:
    with pytest.raises((FC27SearchScoutError, ValidationError)):
        FC27SupportedObservation(
            observation_id="obs_nan_1",
            metric="win_rate",
            observed_value=math.nan,
            comparison_value=50.0,
            sample_size=5,
            confidence_tier="LOW_CONFIDENCE",
            statement="In the available East Coasters sample, win rate was observed.",
        )


def test_013_infinity_rejected() -> None:
    with pytest.raises((FC27SearchScoutError, ValidationError)):
        FC27CounterEvidence(
            counter_evidence_id="ce_inf_1",
            statement="In the available East Coasters sample, counter-evidence was observed.",
            metric="win_rate",
            observed_value=math.inf,
            comparison_value=50.0,
            sample_size=5,
            confidence_tier="LOW_CONFIDENCE",
            impact="MINOR",
        )


def test_014_sensitive_field_rejected() -> None:
    analyst_in = _make_analyst_input(6)
    raw = analyst_in.model_dump(mode="python")
    raw["provenance"] = {"session_cookie": "secret_value"}
    with pytest.raises((FC27PrivacyViolationError, ValidationError)):
        validate_analyst_input(raw)


def test_015_raw_payload_rejected() -> None:
    analyst_in = _make_analyst_input(6)
    raw = analyst_in.model_dump(mode="python")
    raw["provenance"] = {"raw_provider_payload": {"clubs": {}}}
    with pytest.raises((FC27PrivacyViolationError, ValidationError)):
        validate_analyst_input(raw)


def test_016_alias_rejected() -> None:
    analyst_in = _make_analyst_input(6)
    raw = analyst_in.model_dump(mode="python")
    raw["provenance"] = {"display_alias": "CaptainGamer99"}
    with pytest.raises((FC27PrivacyViolationError, ValidationError)):
        validate_analyst_input(raw)


def test_017_external_player_id_rejected() -> None:
    with pytest.raises((FC27PrivacyViolationError, ValidationError)):
        FC27EvidenceCluster(
            cluster_id="cls_bad_player_1",
            category="PLAYER_CONTRIBUTION_PATTERN",
            subject_reference="player:persona_id_99887766",
            eligible_match_count=5,
            confidence_tier="LOW_CONFIDENCE",
            limitations=("Observational only.",),
        )


# ==============================================================================
# 18-30: Evidence Clustering Tests
# ==============================================================================


def test_018_team_evidence_cluster() -> None:
    clusters = build_evidence_clusters(_make_analyst_input(12))
    team_clusters = [c for c in clusters if c.category == "TEAM_OUTCOME_PATTERN"]
    assert len(team_clusters) == 1
    assert team_clusters[0].subject_reference == "club:330323:baseline_outcomes"
    assert team_clusters[0].eligible_match_count == 12


def test_019_formation_cluster() -> None:
    clusters = build_evidence_clusters(_make_analyst_input(12))
    form_clusters = [c for c in clusters if c.category == "FORMATION_ASSOCIATION"]
    assert len(form_clusters) >= 1
    assert any(c.subject_reference == "formation:4-3-3(2)" for c in form_clusters)


def test_020_role_cluster() -> None:
    clusters = build_evidence_clusters(_make_analyst_input(12))
    role_clusters = [c for c in clusters if c.category == "ROLE_ASSOCIATION"]
    assert len(role_clusters) >= 1
    assert all(c.subject_reference.startswith("role:") for c in role_clusters)


def test_021_build_cluster() -> None:
    clusters = build_evidence_clusters(_make_analyst_input(12))
    build_clusters = [c for c in clusters if c.category == "BUILD_ASSOCIATION"]
    assert len(build_clusters) >= 1
    assert all(c.subject_reference.startswith("build:bld_ecf_") for c in build_clusters)


def test_022_partnership_cluster() -> None:
    clusters = build_evidence_clusters(_make_analyst_input(12))
    part_clusters = [c for c in clusters if c.category == "PARTNERSHIP_ASSOCIATION"]
    assert len(part_clusters) >= 1
    assert part_clusters[0].subject_reference == "partnership:plr_ecf_000000000001:plr_ecf_000000000002"


def test_023_match_type_cluster() -> None:
    clusters = build_evidence_clusters(_make_analyst_input(12, multi_match_types=True))
    mt_clusters = [c for c in clusters if c.category == "MATCH_TYPE_PATTERN"]
    assert len(mt_clusters) >= 1
    assert any(c.subject_reference == "match_type:LEAGUE" for c in mt_clusters)


def test_024_opponent_context_cluster() -> None:
    clusters = build_evidence_clusters(_make_analyst_input(12))
    opp_clusters = [c for c in clusters if c.category == "OPPONENT_CONTEXT_PATTERN"]
    assert len(opp_clusters) >= 1
    assert any(c.subject_reference == "opponent:Synthetic Rivals FC" for c in opp_clusters)


def test_025_recent_form_cluster() -> None:
    clusters = build_evidence_clusters(_make_analyst_input(12))
    rf_clusters = [c for c in clusters if c.category == "RECENT_FORM_PATTERN"]
    assert len(rf_clusters) == 1
    assert rf_clusters[0].subject_reference == "club:330323:recent_5_vs_baseline_20"
    assert rf_clusters[0].eligible_match_count == 5


def test_026_stable_cluster_ordering() -> None:
    in_a = _make_analyst_input(12)
    matches_rev = list(reversed(_make_matches(12)))
    ctx_b = build_fc27_search_context(
        FC27SearchInput(analysis_generated_at=REF_NOW, candidate_matches=matches_rev)
    )
    rep_b = build_fc27_scout_report(ctx_b)
    in_b = FC27AnalystInput(
        search_context=ctx_b,
        scout_report=rep_b,
        analysis_generated_at=REF_NOW_ISO,
    )
    c_a = build_evidence_clusters(in_a)
    c_b = build_evidence_clusters(in_b)
    assert [c.cluster_id for c in c_a] == [c.cluster_id for c in c_b]


def test_027_deterministic_cluster_id() -> None:
    in_a = _make_analyst_input(8)
    c1 = build_evidence_clusters(in_a)
    c2 = build_evidence_clusters(in_a)
    assert [c.cluster_id for c in c1] == [c.cluster_id for c in c2]
    assert all(c.cluster_id.startswith("cls_") for c in c1)


def test_028_cluster_count_bounded() -> None:
    in_20 = _make_analyst_input(20, repeated_opponent=False)
    clusters = build_evidence_clusters(in_20)
    assert len(clusters) <= MAX_EVIDENCE_CLUSTERS == 20
    in_custom = in_20.model_copy(update={"maximum_clusters": 5})
    assert len(build_evidence_clusters(in_custom)) == 5


def test_029_duplicate_evidence_reference_rejected() -> None:
    ref = FC27EvidenceReference(
        evidence_id="ev_dup_00000001",
        scope_type="TEAM",
        scope_key="club:330323",
        eligible_sample_count=5,
        match_hashes=("a" * 64,),
    )
    with pytest.raises((FC27AnalystValidationError, ValidationError), match="Duplicate evidence_id"):
        FC27EvidenceCluster(
            cluster_id="cls_dup_1",
            category="TEAM_OUTCOME_PATTERN",
            subject_reference="club:330323:baseline_outcomes",
            eligible_match_count=5,
            confidence_tier="LOW_CONFIDENCE",
            evidence_references=(ref, ref),
            limitations=("Observational only.",),
        )


def test_030_unknown_evidence_reference_rejected() -> None:
    analyst_in = _make_analyst_input(6)
    bad_ref = FC27EvidenceReference(
        evidence_id="ev_unknown_9999",
        scope_type="TEAM",
        scope_key="club:330323",
        eligible_sample_count=1,
        match_hashes=("f" * 64,),
    )
    bad_rep = copy.deepcopy(analyst_in.scout_report)
    object.__setattr__(
        bad_rep, "evidence_references", (*bad_rep.evidence_references, bad_ref)
    )
    contradictions = detect_evidence_contradictions(analyst_in.search_context, bad_rep)
    assert any(c.reason_code == "UNKNOWN_EVIDENCE_REFERENCE" for c in contradictions)


# ==============================================================================
# 31-41: Counter-Evidence Tests
# ==============================================================================


def test_031_opposite_direction_detected() -> None:
    # Matches where a formation has >= baseline win rate but negative goal differential (e.g. two 1-0 wins and one 0-5 loss)
    matches = _make_matches(6)
    matches[0]["formation"] = "4-3-3(2)"
    matches[0]["result"] = "WIN"
    matches[0]["goals_for"] = 1
    matches[0]["goals_against"] = 0
    matches[2]["formation"] = "4-3-3(2)"
    matches[2]["result"] = "WIN"
    matches[2]["goals_for"] = 1
    matches[2]["goals_against"] = 0
    matches[4]["formation"] = "4-3-3(2)"
    matches[4]["result"] = "LOSS"
    matches[4]["goals_for"] = 0
    matches[4]["goals_against"] = 6
    ctx = build_fc27_search_context(
        FC27SearchInput(analysis_generated_at=REF_NOW, candidate_matches=matches)
    )
    rep = build_fc27_scout_report(ctx)
    ain = FC27AnalystInput(
        search_context=ctx, scout_report=rep, analysis_generated_at=REF_NOW_ISO
    )
    clusters = build_evidence_clusters(ain)
    ce_map = analyze_counter_evidence(ctx, rep, clusters)
    all_metrics = {ce.metric for ces in ce_map.values() for ce in ces}
    assert "opposite_metric_direction" in all_metrics


def test_032_lower_recent_form_detected() -> None:
    ain = _make_analyst_input(12, recent_slump=True)
    clusters = build_evidence_clusters(ain)
    rf_cluster = next(c for c in clusters if c.category == "RECENT_FORM_PATTERN")
    metrics = {ce.metric for ce in rf_cluster.counter_observations}
    assert "recent_win_rate_drop" in metrics


def test_033_formation_inconsistency_detected() -> None:
    ain = _make_analyst_input(12)
    clusters = build_evidence_clusters(ain)
    form_clusters = [c for c in clusters if c.category == "FORMATION_ASSOCIATION"]
    all_form_ce = {ce.metric for c in form_clusters for ce in c.counter_observations}
    assert "formation_result_inconsistency" in all_form_ce


def test_034_role_inconsistency_detected() -> None:
    ain = _make_analyst_input(12)
    clusters = build_evidence_clusters(ain)
    role_clusters = [c for c in clusters if c.category == "ROLE_ASSOCIATION"]
    all_role_ce = {ce.metric for c in role_clusters for ce in c.counter_observations}
    assert "role_result_inconsistency" in all_role_ce


def test_035_partnership_inconsistency_detected() -> None:
    ain = _make_analyst_input(12)
    clusters = build_evidence_clusters(ain)
    part_clusters = [c for c in clusters if c.category == "PARTNERSHIP_ASSOCIATION"]
    all_part_ce = {ce.metric for c in part_clusters for ce in c.counter_observations}
    assert "partnership_inconsistency" in all_part_ce


def test_036_match_type_dependence_detected() -> None:
    ain = _make_analyst_input(10, multi_match_types=False)
    clusters = build_evidence_clusters(ain)
    team_cluster = next(c for c in clusters if c.category == "TEAM_OUTCOME_PATTERN")
    metrics = {ce.metric for ce in team_cluster.counter_observations}
    assert "match_type_concentration" in metrics


def test_037_opponent_context_dependence_detected() -> None:
    ain = _make_analyst_input(10, repeated_opponent=True)
    clusters = build_evidence_clusters(ain)
    team_cluster = next(c for c in clusters if c.category == "TEAM_OUTCOME_PATTERN")
    metrics = {ce.metric for ce in team_cluster.counter_observations}
    assert "opponent_context_dependence" in metrics


def test_038_low_sample_captured() -> None:
    ain = _make_analyst_input(4)
    clusters = build_evidence_clusters(ain)
    team_cluster = next(c for c in clusters if c.category == "TEAM_OUTCOME_PATTERN")
    assert any(ce.metric == "eligible_sample_size" for ce in team_cluster.counter_observations)


def test_039_missing_telemetry_captured() -> None:
    ain = _make_analyst_input(6, include_telemetry=False)
    clusters = build_evidence_clusters(ain)
    team_cluster = next(c for c in clusters if c.category == "TEAM_OUTCOME_PATTERN")
    assert any(ce.metric == "missing_optional_telemetry" for ce in team_cluster.counter_observations)


def test_040_disqualifying_counter_evidence_blocks_support() -> None:
    ain = _make_analyst_input(2)
    report = build_fc27_analyst_report(ain)
    assert all(f.status != "SUPPORTED_OBSERVATION" for f in report.findings)
    assert any(
        any(ce.impact == "DISQUALIFYING" for ce in f.counter_evidence)
        for f in report.findings
    )


def test_041_counter_evidence_ordering_deterministic() -> None:
    ain = _make_analyst_input(12, recent_slump=True, include_telemetry=False)
    c1 = build_evidence_clusters(ain)
    c2 = build_evidence_clusters(ain)
    for cl_a, cl_b in zip(c1, c2, strict=True):
        assert [ce.counter_evidence_id for ce in cl_a.counter_observations] == [
            ce.counter_evidence_id for ce in cl_b.counter_observations
        ]
        assert len(cl_a.counter_observations) <= MAX_COUNTER_EVIDENCE_ITEMS


# ==============================================================================
# 42-52: Contradiction Detection Tests
# ==============================================================================


def test_042_aggregate_mismatch_detected() -> None:
    ain = _make_analyst_input(6)
    bad_rep = copy.deepcopy(ain.scout_report)
    object.__setattr__(bad_rep.team_evidence, "wins", 99)
    contradictions = detect_evidence_contradictions(ain.search_context, bad_rep)
    assert any(c.reason_code == "SEARCH_SCOUT_AGGREGATE_MISMATCH" for c in contradictions)


def test_043_direction_mismatch_detected() -> None:
    ain = _make_analyst_input(12)
    bad_rep = copy.deepcopy(ain.scout_report)
    bad_hyp = FC27Hypothesis(
        hypothesis_id="hyp_bad_dir_0001",
        category="FORMATION_ASSOCIATION",
        statement="In the available East Coasters sample, win rate is associated with higher outcomes in 4-3-3(2) than baseline.",
        metric_name="win_rate",
        direction="HIGHER",
        comparison_value=30.0,
        baseline_value=60.0,
        absolute_delta=-30.0,
        relative_delta=-50.0,
        eligible_match_count=6,
        supporting_match_hashes=(ain.search_context.baseline_matches[0].match_hash,),
        confidence_tier="LOW_CONFIDENCE",
        limitations=("Observational only.",),
        confounders=("opponent_strength",),
    )
    object.__setattr__(bad_rep, "hypotheses", (bad_hyp,))
    contradictions = detect_evidence_contradictions(ain.search_context, bad_rep)
    assert any(c.reason_code == "HYPOTHESIS_DIRECTION_MISMATCH" for c in contradictions)


def test_044_confidence_inflation_detected() -> None:
    ain = _make_analyst_input(4)
    bad_rep = copy.deepcopy(ain.scout_report)
    object.__setattr__(bad_rep, "confidence_tier", "HIGH_CONFIDENCE")
    contradictions = detect_evidence_contradictions(ain.search_context, bad_rep)
    assert any(c.reason_code == "CONFIDENCE_INFLATION" for c in contradictions)


def test_045_formation_total_overflow_detected() -> None:
    ain = _make_analyst_input(6)
    bad_rep = copy.deepcopy(ain.scout_report)
    object.__setattr__(bad_rep.formation_evidence[0], "appearances", 15)
    contradictions = detect_evidence_contradictions(ain.search_context, bad_rep)
    assert any(c.reason_code == "FORMATION_TOTAL_OVERFLOW" for c in contradictions)


def test_046_role_total_overflow_detected() -> None:
    ain = _make_analyst_input(6)
    bad_rep = copy.deepcopy(ain.scout_report)
    object.__setattr__(bad_rep.role_evidence[0], "distinct_matches", 19)
    contradictions = detect_evidence_contradictions(ain.search_context, bad_rep)
    assert any(c.reason_code == "ROLE_TOTAL_OVERFLOW" for c in contradictions)


def test_047_partnership_overflow_detected() -> None:
    ain = _make_analyst_input(6)
    bad_rep = copy.deepcopy(ain.scout_report)
    object.__setattr__(bad_rep.partnership_evidence[0], "shared_appearances", 18)
    contradictions = detect_evidence_contradictions(ain.search_context, bad_rep)
    assert any(c.reason_code == "PARTNERSHIP_OVERFLOW" for c in contradictions)


def test_048_formula_mismatch_blocking() -> None:
    ain = _make_analyst_input(6)
    bad_rep = copy.deepcopy(ain.scout_report)
    object.__setattr__(bad_rep, "formula_version", "fc27-v0.1")
    contradictions = detect_evidence_contradictions(ain.search_context, bad_rep)
    fm_ctrs = [c for c in contradictions if c.reason_code == "FORMULA_VERSION_MISMATCH"]
    assert len(fm_ctrs) == 1
    assert fm_ctrs[0].severity == "BLOCKING"


def test_049_identity_mismatch_blocking() -> None:
    ain = _make_analyst_input(6)
    bad_rep = copy.deepcopy(ain.scout_report)
    object.__setattr__(bad_rep.identity, "platform", "pc")
    contradictions = detect_evidence_contradictions(ain.search_context, bad_rep)
    id_ctrs = [c for c in contradictions if c.reason_code == "IDENTITY_MISMATCH"]
    assert len(id_ctrs) == 1
    assert id_ctrs[0].severity == "BLOCKING"


def test_050_supported_status_with_disqualifying_evidence_rejected() -> None:
    obs = FC27SupportedObservation(
        observation_id="obs_1",
        metric="win_rate",
        observed_value=60.0,
        comparison_value=50.0,
        sample_size=5,
        confidence_tier="LOW_CONFIDENCE",
        statement="In the available East Coasters sample, win rate was 60%.",
    )
    disq_ce = FC27CounterEvidence(
        counter_evidence_id="ce_disq_1",
        statement="In the available East Coasters sample, disqualifying counter-evidence was observed.",
        metric="excluded_quality_records",
        observed_value=10,
        comparison_value=5,
        sample_size=5,
        confidence_tier="LOW_CONFIDENCE",
        impact="DISQUALIFYING",
    )
    conf = FC27Confounder(
        confounder_id="cnf_1",
        name="opponent_strength_unavailable",
        status="UNMEASURED",
        description="Opponent strength is unmeasured.",
        mitigation_or_data_need="Collect opponent tier.",
    )
    exp = FC27CompetingExplanation(
        explanation_id="exp_1",
        category="TEAM_OUTCOME_PATTERN",
        statement="A competing explanation is that opponent mix varied.",
        plausibility_note="Opponent strength is unrecorded.",
        testable_condition="Stratify by opponent.",
    )
    with pytest.raises(FC27AnalystValidationError, match="DISQUALIFYING"):
        FC27AnalystFinding(
            finding_id="fnd_bad_1",
            cluster_id="cls_1",
            category="TEAM_OUTCOME_PATTERN",
            subject_reference="club:330323:baseline_outcomes",
            statement="In the available East Coasters sample, the evidence is associated with team outcomes.",
            status="SUPPORTED_OBSERVATION",
            confidence_tier="LOW_CONFIDENCE",
            eligible_sample_count=5,
            supporting_evidence=(obs,),
            counter_evidence=(disq_ce,),
            limitations=("Observational only.",),
            confounders=(conf,),
            competing_explanations=(exp,),
            falsification_criteria=("Reverses over 20 matches.",),
            additional_data_needed=("More matches.",),
        )


def test_051_blocking_contradiction_prevents_report() -> None:
    ain = _make_analyst_input(6)
    tampered = ain.model_copy(update={"scout_report_hash": "c" * 64})
    with pytest.raises(FC27AnalystContradictionError):
        build_fc27_analyst_report(tampered)


def test_052_warning_contradiction_retained() -> None:
    # Construct 10 matches where recent 5 has higher win rate (4 wins of 1-0, 1 loss of 0-6 -> WR=80%, GD=-0.4)
    # while older 5 had 2 wins of 3-0 and 3 draws of 0-0 (total 10 matches: 6W, 3D, 1L -> WR=60%, total GF=10, GA=6 -> GD=+0.4)
    matches = _make_matches(10)
    for idx in range(5):
        if idx < 4:
            matches[idx]["result"] = "WIN"
            matches[idx]["goals_for"] = 1
            matches[idx]["goals_against"] = 0
        else:
            matches[idx]["result"] = "LOSS"
            matches[idx]["goals_for"] = 0
            matches[idx]["goals_against"] = 6
    for idx in range(5, 10):
        if idx < 7:
            matches[idx]["result"] = "WIN"
            matches[idx]["goals_for"] = 3
            matches[idx]["goals_against"] = 0
        else:
            matches[idx]["result"] = "DRAW"
            matches[idx]["goals_for"] = 0
            matches[idx]["goals_against"] = 0
    ctx = build_fc27_search_context(
        FC27SearchInput(analysis_generated_at=REF_NOW, candidate_matches=matches)
    )
    rep = build_fc27_scout_report(ctx)
    ain = FC27AnalystInput(
        search_context=ctx, scout_report=rep, analysis_generated_at=REF_NOW_ISO
    )
    report = build_fc27_analyst_report(ain)
    warn_ctrs = [c for c in report.contradictions if c.severity == "WARNING"]
    assert len(warn_ctrs) == 1
    assert warn_ctrs[0].reason_code == "RECENT_VS_BASELINE_METRIC_DIVERGENCE"


# ==============================================================================
# 53-62: Confounders Tests
# ==============================================================================


def test_053_opponent_strength_unmeasured() -> None:
    ain = _make_analyst_input(8)
    confs = identify_confounders(ain.search_context, ain.scout_report)
    opp_str = next(c for c in confs if c.name == "opponent_strength_unavailable")
    assert opp_str.status == "UNMEASURED"


def test_054_player_availability_partially_measured() -> None:
    ain = _make_analyst_input(8)
    confs = identify_confounders(ain.search_context, ain.scout_report)
    avail = next(c for c in confs if c.name == "teammate_availability")
    assert avail.status == "PARTIALLY_MEASURED"


def test_055_match_type_measured() -> None:
    ain = _make_analyst_input(8)
    confs = identify_confounders(ain.search_context, ain.scout_report)
    mt = next(c for c in confs if c.name == "match_type")
    assert mt.status == "MEASURED"


def test_056_formation_metadata_unavailable() -> None:
    ain = _make_analyst_input(6, include_formations=False)
    confs = identify_confounders(ain.search_context, ain.scout_report)
    fm = next(c for c in confs if c.name == "missing_formation_metadata")
    assert fm.status == "UNMEASURED"


def test_057_role_metadata_unavailable() -> None:
    ain = _make_analyst_input(6, include_roles=False)
    confs = identify_confounders(ain.search_context, ain.scout_report)
    rm = next(c for c in confs if c.name == "missing_role_metadata")
    assert rm.status == "UNMEASURED"


def test_058_build_metadata_unavailable() -> None:
    ain = _make_analyst_input(6, include_builds=False)
    confs = identify_confounders(ain.search_context, ain.scout_report)
    bm = next(c for c in confs if c.name == "missing_build_metadata")
    assert bm.status == "UNMEASURED"


def test_059_unequal_sample_size_confounder() -> None:
    ain = _make_analyst_input(7)
    confs = identify_confounders(ain.search_context, ain.scout_report)
    uq = next(c for c in confs if c.name == "unequal_group_sample_size")
    assert uq.status == "MEASURED"


def test_060_repeated_opponent_concentration() -> None:
    ain = _make_analyst_input(8, repeated_opponent=True)
    confs = identify_confounders(ain.search_context, ain.scout_report)
    ro = next(c for c in confs if c.name == "repeated_opponent_concentration")
    assert ro.status == "MEASURED"


def test_061_stable_confounder_ordering() -> None:
    ain = _make_analyst_input(10)
    c1 = identify_confounders(ain.search_context, ain.scout_report)
    c2 = identify_confounders(ain.search_context, ain.scout_report)
    assert [c.confounder_id for c in c1] == [c.confounder_id for c in c2]
    assert [c.name for c in c1] == sorted(c.name for c in c1)


def test_062_confounder_count_bounded() -> None:
    report = build_fc27_analyst_report(_make_analyst_input(12))
    assert all(1 <= len(f.confounders) <= MAX_CONFOUNDERS for f in report.findings)
    assert all(c.name in ALLOWED_CONFOUNDER_NAMES for c in report.confounders)


# ==============================================================================
# 63-68: Competing Explanations Tests
# ==============================================================================


def test_063_formation_competing_explanations() -> None:
    exps = generate_competing_explanations("FORMATION_ASSOCIATION", "formation:4-3-3(2)")
    assert 1 <= len(exps) <= MAX_COMPETING_EXPLANATIONS
    texts = " ".join(e.statement.lower() for e in exps)
    assert "player availability" in texts
    assert "opponent strength" in texts


def test_064_partnership_competing_explanations() -> None:
    exps = generate_competing_explanations(
        "PARTNERSHIP_ASSOCIATION", "partnership:plr_ecf_000000000001:plr_ecf_000000000002"
    )
    assert 1 <= len(exps) <= MAX_COMPETING_EXPLANATIONS
    texts = " ".join(e.statement.lower() for e in exps)
    assert "shared minutes" in texts
    assert "position and role assignments" in texts


def test_065_recent_form_competing_explanations() -> None:
    exps = generate_competing_explanations(
        "RECENT_FORM_PATTERN", "club:330323:recent_5_vs_baseline_20"
    )
    assert 1 <= len(exps) <= MAX_COMPETING_EXPLANATIONS
    texts = " ".join(e.statement.lower() for e in exps)
    assert "opponent mix" in texts
    assert "lineup composition" in texts


def test_066_player_contribution_competing_explanations() -> None:
    exps = generate_competing_explanations(
        "PLAYER_CONTRIBUTION_PATTERN", "player:plr_ecf_000000000001"
    )
    assert 1 <= len(exps) <= MAX_COMPETING_EXPLANATIONS
    texts = " ".join(e.statement.lower() for e in exps)
    assert "position and role usage" in texts
    assert "minutes played" in texts


def test_067_deterministic_template_output() -> None:
    e1 = generate_competing_explanations("FORMATION_ASSOCIATION", "formation:4-3-3(2)")
    e2 = generate_competing_explanations("FORMATION_ASSOCIATION", "formation:4-3-3(2)")
    assert e1 == e2


def test_068_no_free_form_llm_use(monkeypatch: pytest.MonkeyPatch) -> None:
    from src import roles

    def _forbid_llm(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("LLM role invocation is strictly forbidden")

    monkeypatch.setattr(roles, "run_role", _forbid_llm)
    exps = generate_competing_explanations("FORMATION_ASSOCIATION", "formation:4-3-3(2)")
    assert len(exps) == 4


# ==============================================================================
# 69-80: Data Gaps and Unresolved Questions Tests
# ==============================================================================


def test_069_insufficient_matches_gap() -> None:
    ain = _make_analyst_input(2)
    gaps = identify_data_gaps(ain.search_context, ain.scout_report)
    g = next(x for x in gaps if x.category == "INSUFFICIENT_COMPLETED_MATCHES")
    assert g.blocking is True


def test_070_baseline_below_20_gap() -> None:
    ain = _make_analyst_input(12)
    gaps = identify_data_gaps(ain.search_context, ain.scout_report)
    assert any(x.category == "BASELINE_WINDOW_BELOW_20" for x in gaps)


def test_071_recent_window_below_5_gap() -> None:
    ain = _make_analyst_input(4)
    gaps = identify_data_gaps(ain.search_context, ain.scout_report)
    assert any(x.category == "RECENT_WINDOW_BELOW_5" for x in gaps)


def test_072_formation_metadata_gap() -> None:
    ain = _make_analyst_input(6, include_formations=False)
    gaps = identify_data_gaps(ain.search_context, ain.scout_report)
    assert any(x.category == "FORMATION_METADATA_GAP" for x in gaps)


def test_073_role_metadata_gap() -> None:
    ain = _make_analyst_input(6, include_roles=False)
    gaps = identify_data_gaps(ain.search_context, ain.scout_report)
    assert any(x.category == "ROLE_METADATA_GAP" for x in gaps)


def test_074_build_metadata_gap() -> None:
    ain = _make_analyst_input(6, include_builds=False)
    gaps = identify_data_gaps(ain.search_context, ain.scout_report)
    assert any(x.category == "BUILD_METADATA_GAP" for x in gaps)


def test_075_partnership_gap() -> None:
    ain = _make_analyst_input(6, include_partnerships=False)
    gaps = identify_data_gaps(ain.search_context, ain.scout_report)
    assert any(x.category == "PARTNERSHIP_OVERLAP_GAP" for x in gaps)


def test_076_opponent_context_gap() -> None:
    ain = _make_analyst_input(8)
    gaps = identify_data_gaps(ain.search_context, ain.scout_report)
    assert any(x.category == "OPPONENT_CONTEXT_GAP" for x in gaps)


def test_077_player_telemetry_gap() -> None:
    ain = _make_analyst_input(6, include_telemetry=False)
    gaps = identify_data_gaps(ain.search_context, ain.scout_report)
    assert any(x.category == "PLAYER_TELEMETRY_GAP" for x in gaps)


def test_078_deterministic_unresolved_question() -> None:
    ain = _make_analyst_input(6, include_formations=False, include_roles=False)
    gaps = identify_data_gaps(ain.search_context, ain.scout_report)
    q1 = generate_unresolved_questions(gaps, ())
    q2 = generate_unresolved_questions(gaps, ())
    assert q1 == q2
    assert all(q.question.endswith("?") for q in q1)
    assert all(len(q.source_gap_or_contradiction_ids) >= 1 for q in q1)


def test_079_data_gap_count_bounded() -> None:
    ain = _make_analyst_input(
        2,
        include_formations=False,
        include_roles=False,
        include_builds=False,
        include_partnerships=False,
        include_telemetry=False,
        multi_match_types=False,
    )
    gaps = identify_data_gaps(ain.search_context, ain.scout_report)
    assert 1 <= len(gaps) <= MAX_DATA_GAPS == 20


def test_080_question_count_bounded() -> None:
    ain = _make_analyst_input(
        2,
        include_formations=False,
        include_roles=False,
        include_builds=False,
        include_partnerships=False,
        include_telemetry=False,
    )
    report = build_fc27_analyst_report(ain)
    assert 1 <= len(report.unresolved_questions) <= MAX_UNRESOLVED_QUESTIONS == 20


# ==============================================================================
# 81-100: Findings & Report Determinism Tests
# ==============================================================================


def test_081_supported_observation_generated() -> None:
    # 12 clean wins with no losses -> SUPPORTED_OBSERVATION
    matches = _make_matches(12)
    for m in matches:
        m["result"] = "WIN"
        m["goals_for"] = 2
        m["goals_against"] = 0
        m["formation"] = "4-3-3(2)"
    ctx = build_fc27_search_context(
        FC27SearchInput(analysis_generated_at=REF_NOW, candidate_matches=matches)
    )
    rep = build_fc27_scout_report(ctx)
    report = build_fc27_analyst_report(
        FC27AnalystInput(
            search_context=ctx, scout_report=rep, analysis_generated_at=REF_NOW_ISO
        )
    )
    assert any(f.status == "SUPPORTED_OBSERVATION" for f in report.findings)


def test_082_mixed_evidence_finding_generated() -> None:
    report = build_fc27_analyst_report(_make_analyst_input(12))
    mixed = [f for f in report.findings if f.status == "MIXED_EVIDENCE"]
    assert len(mixed) >= 1
    assert all("the evidence is mixed" in f.statement.lower() for f in mixed)


def test_083_counter_evidence_dominant_finding_generated() -> None:
    report = build_fc27_analyst_report(_make_analyst_input(12, recent_slump=True))
    ce_dom = [f for f in report.findings if f.status == "COUNTER_EVIDENCE_DOMINANT"]
    assert len(ce_dom) >= 1


def test_084_insufficient_evidence_finding_generated() -> None:
    report = build_fc27_analyst_report(_make_analyst_input(2))
    insuff = [f for f in report.findings if f.status == "INSUFFICIENT_EVIDENCE"]
    assert len(insuff) >= 1
    assert all("insufficient" in f.statement.lower() for f in insuff)


def test_085_unresolved_finding_generated() -> None:
    # Majority of records excluded for quarantine/quality (> eligible_match_count) produces UNRESOLVED
    matches = _make_matches(4)
    q_ids = [f"m_quar_{i:03d}" for i in range(6)]
    for qid in q_ids:
        m_q = copy.deepcopy(matches[0])
        m_q["match_id"] = qid
        m_q["match_hash"] = _sha256(qid)
        m_q["data_quality_status"] = "QUARANTINED"
        matches.append(m_q)
    ctx = build_fc27_search_context(
        FC27SearchInput(analysis_generated_at=REF_NOW, candidate_matches=matches)
    )
    rep = build_fc27_scout_report(ctx)
    report = build_fc27_analyst_report(
        FC27AnalystInput(
            search_context=ctx, scout_report=rep, analysis_generated_at=REF_NOW_ISO
        )
    )
    assert any(f.status == "UNRESOLVED" for f in report.findings)


def test_086_confidence_capped_by_evidence() -> None:
    report = build_fc27_analyst_report(_make_analyst_input(6))
    assert report.overall_confidence == "LOW_CONFIDENCE"
    for f in report.findings:
        assert f.confidence_tier in {"INSUFFICIENT_DATA", "EXPLORATORY", "LOW_CONFIDENCE"}


def test_087_observational_only_status() -> None:
    report = build_fc27_analyst_report(_make_analyst_input(12))
    assert all(
        f.observation_classification == OBSERVATIONAL_ONLY_STATUS for f in report.findings
    )


def test_088_no_recommendation_status() -> None:
    report = build_fc27_analyst_report(_make_analyst_input(12))
    assert report.recommendation_status == NOT_A_RECOMMENDATION_STATUS
    assert all(f.recommendation is False for f in report.findings)
    assert all(f.causal_claim is False for f in report.findings)


def test_089_not_approvable() -> None:
    report = build_fc27_analyst_report(_make_analyst_input(12))
    assert report.approvable is False
    assert all(f.approvable is False for f in report.findings)


def test_090_not_dispatchable() -> None:
    report = build_fc27_analyst_report(_make_analyst_input(12))
    assert report.dispatchable is False
    assert all(f.dispatchable is False for f in report.findings)


def test_091_limitations_required() -> None:
    report = build_fc27_analyst_report(_make_analyst_input(12))
    assert len(report.limitations) >= 1
    assert all(len(f.limitations) >= 1 for f in report.findings)


def test_092_confounders_required() -> None:
    report = build_fc27_analyst_report(_make_analyst_input(12))
    assert len(report.confounders) >= 1
    assert all(len(f.confounders) >= 1 for f in report.findings)


def test_093_competing_explanations_required() -> None:
    report = build_fc27_analyst_report(_make_analyst_input(12))
    assert all(len(f.competing_explanations) >= 1 for f in report.findings)


def test_094_falsification_criteria_required() -> None:
    report = build_fc27_analyst_report(_make_analyst_input(12))
    assert all(len(f.falsification_criteria) >= 1 for f in report.findings)


def test_095_additional_data_needed_required() -> None:
    report = build_fc27_analyst_report(_make_analyst_input(12))
    assert all(len(f.additional_data_needed) >= 1 for f in report.findings)


def test_096_finding_count_bounded_to_20() -> None:
    ain = _make_analyst_input(20, repeated_opponent=False)
    report = build_fc27_analyst_report(ain)
    assert 1 <= len(report.findings) <= MAX_ANALYST_FINDINGS == 20
    ain_3 = ain.model_copy(update={"maximum_findings": 3})
    assert len(build_fc27_analyst_report(ain_3).findings) == 3


def test_097_stable_finding_ordering() -> None:
    r1 = build_fc27_analyst_report(_make_analyst_input(12))
    r2 = build_fc27_analyst_report(_make_analyst_input(12))
    assert [f.finding_id for f in r1.findings] == [f.finding_id for f in r2.findings]


def test_098_deterministic_finding_id() -> None:
    r1 = build_fc27_analyst_report(_make_analyst_input(8))
    assert all(f.finding_id.startswith("fnd_") for f in r1.findings)


def test_099_deterministic_analyst_report_hash() -> None:
    r1 = build_fc27_analyst_report(_make_analyst_input(12))
    r2 = build_fc27_analyst_report(_make_analyst_input(12))
    assert len(r1.report_hash) == 64
    assert r1.report_hash == r2.report_hash


def test_100_identical_input_and_timestamp_produce_identical_output() -> None:
    ain_a = _make_analyst_input(12)
    ain_b = _make_analyst_input(12)
    r_a = build_fc27_analyst_report(ain_a)
    r_b = build_fc27_analyst_report(ain_b)
    assert r_a.model_dump(mode="json") == r_b.model_dump(mode="json")


# ==============================================================================
# 101-110: Language and Safety Validation Tests
# ==============================================================================


def test_101_causal_wording_rejected() -> None:
    with pytest.raises(FC27AnalystLanguageError, match="CAUSAL_CLAIM"):
        validate_analyst_evidence_language(
            "In the available East Coasters sample, formation 4-3-3(2) causes higher win rates."
        )
    with pytest.raises(FC27AnalystLanguageError, match="CAUSAL_CLAIM"):
        validate_analyst_evidence_language(
            "In the available East Coasters sample, this metric proves the formation works."
        )


def test_102_optimal_rejected() -> None:
    with pytest.raises(FC27AnalystLanguageError, match="OPTIMAL_OR_SUPERIORITY_CLAIM"):
        validate_analyst_evidence_language(
            "In the available East Coasters sample, 4-3-3(2) is the optimal formation."
        )


def test_103_best_player_rejected() -> None:
    with pytest.raises(FC27AnalystLanguageError, match="PLAYER_QUALITY_RANKING"):
        validate_analyst_evidence_language(
            "In the available East Coasters sample, plr_ecf_000000000001 is the best player."
        )


def test_104_worst_player_rejected() -> None:
    with pytest.raises(FC27AnalystLanguageError, match="PLAYER_QUALITY_RANKING"):
        validate_analyst_evidence_language(
            "In the available East Coasters sample, plr_ecf_000000000002 is the worst player."
        )


def test_105_must_bench_rejected() -> None:
    for phrase in (
        "must bench plr_ecf_000000000002",
        "must use 4-3-3(2)",
        "should drop plr_ecf_000000000002",
        "should replace plr_ecf_000000000002",
        "switch to 4-2-3-1",
        "assign plr_ecf_000000000002 to striker",
        "promote plr_ecf_000000000002",
        "demote plr_ecf_000000000002",
    ):
        with pytest.raises(FC27AnalystLanguageError, match="PRESCRIPTIVE_RECOMMENDATION"):
            validate_analyst_evidence_language(
                f"In the available East Coasters sample, the club {phrase}."
            )


def test_106_psychological_inference_rejected() -> None:
    for phrase in (
        "player is selfish and has poor attitude",
        "psychological profile indicates tilt",
        "reliability issues in late minutes",
        "attitude problem during losses",
    ):
        with pytest.raises(FC27AnalystLanguageError, match="PSYCHOLOGICAL_OR_EFFORT_INFERENCE"):
            validate_analyst_evidence_language(
                f"In the available East Coasters sample, {phrase}."
            )


def test_107_effort_inference_rejected() -> None:
    with pytest.raises(FC27AnalystLanguageError, match="PSYCHOLOGICAL_OR_EFFORT_INFERENCE"):
        validate_analyst_evidence_language(
            "In the available East Coasters sample, player lacks effort and is lazy."
        )


def test_108_guaranteed_win_claim_rejected() -> None:
    with pytest.raises(FC27AnalystLanguageError, match="OPTIMAL_OR_SUPERIORITY_CLAIM"):
        validate_analyst_evidence_language(
            "In the available East Coasters sample, this lineup will maximize win probability."
        )
    with pytest.raises(FC27AnalystLanguageError, match="CAUSAL_CLAIM"):
        validate_analyst_evidence_language(
            "In the available East Coasters sample, this change provides guaranteed improvement."
        )


def test_109_correlational_wording_accepted() -> None:
    stmt = (
        "In the available East Coasters sample, the evidence is associated with formation 4-3-3(2); "
        "this observation warrants further testing."
    )
    assert validate_analyst_evidence_language(stmt) == stmt


def test_110_mixed_evidence_wording_accepted() -> None:
    from src.fc27_analyst import (
        ANALYST_POLICY_VERSION,
        ANALYST_SYNTHESIS_VERSION,
        build_analyst_findings,
        build_competing_explanations_for_cluster,
        build_confounders_for_cluster,
        build_counter_evidence,
        build_data_gaps,
        build_unresolved_questions,
        reconcile_cluster_confidence,
        reconcile_overall_confidence,
    )

    stmt = (
        "In the available East Coasters sample, the evidence is mixed across recent and baseline matches; "
        "the result may be affected by opponent mix."
    )
    assert validate_analyst_evidence_language(stmt) == stmt
    assert ANALYST_SYNTHESIS_VERSION == "fc27-analyst-v1.0"
    assert ANALYST_POLICY_VERSION == "fc27-analyst-policy-v1"
    a_in = _make_analyst_input(count=6)
    rep = build_fc27_analyst_report(a_in)
    assert rep.provenance["analyst_synthesis_version"] == "fc27-analyst-v1.0"
    assert rep.provenance["analyst_policy_version"] == "fc27-analyst-policy-v1"
    clusters = rep.evidence_clusters
    assert build_counter_evidence(a_in.search_context, a_in.scout_report, clusters)
    assert build_confounders_for_cluster(clusters[0], rep.confounders)
    assert build_competing_explanations_for_cluster(clusters[0])
    gaps = build_data_gaps(a_in.search_context, a_in.scout_report, clusters)
    assert gaps
    assert build_unresolved_questions(gaps, rep.contradictions, rep.findings)
    assert reconcile_cluster_confidence(clusters[0], rep.contradictions) == clusters[0].confidence_tier
    assert reconcile_overall_confidence(a_in.search_context, a_in.scout_report, rep.contradictions) == rep.overall_confidence
    assert build_analyst_findings(a_in) == rep.findings
