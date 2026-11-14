"""Regression test matrix for FC27 live polling replay-conflict reconciliation.

Covers Phase 12 requirements (1 through 24) against disposable PostgreSQL 16 at
migration ``0015`` and Grants Policy Version ``16``:
- Versioned source-independent match equivalence (``fc27-match-equivalence-v1``)
- Backward compatibility with historical ``comm_eval_*`` stored ``match_hash`` values
- Fail-closed rejection and sanitized ``PENDING_REVIEW`` quarantine persistence
  for any material match-fact, opponent-identity, or player-statistic difference
- Exact ``replay_conflict_count`` reporting (no batch-size inflation)
- Restart-durable circuit-breaker protection while polling conflicts remain
  ``PENDING_REVIEW``, plus audited manual reset metadata
- Strict privacy, request-budget, and PostgreSQL role boundaries

All fixtures are synthetic and privacy-safe. Zero live provider, DNS, or Discord
network calls are performed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import psycopg
import psycopg.errors
import pytest

from src.db_roles import GRANTS_POLICY_VERSION
from src.fc27_api_rate_limit import FC27CircuitState
from src.fc27_community_provider import normalize_community_match_records
from src.fc27_discord_bot import FC27DiscordBotConfig, FC27DiscordBotService
from src.fc27_polling_worker import (
    FC27PollingRuntime,
    persist_polled_match_envelope,
    recover_polling_state_from_postgres,
    run_fc27_poll_cycle,
)
from src.fc27_privacy import (
    MATCH_EQUIVALENCE_CONTRACT_VERSION,
    canonicalize_match_type_for_equivalence,
    compare_match_equivalence_v1,
    compute_alias_hmac_fingerprint,
    compute_match_equivalence_hash,
    compute_match_hash,
    project_match_equivalence_v1,
    pseudonymize_east_coasters_identifier,
    pseudonymize_match_ref,
    scan_for_sensitive_fields,
)
from src.fc27_provider import FC27ProviderError
from src.fc27_repository import (
    FC27ReplayConflictError,
    FC27SuppliedMatch,
    FC27SuppliedPlayerPerformance,
    FC27UserExportEnvelope,
    _default_build_for_position,
    _default_role_for_position,
    apply_export_plan,
    compute_payload_sha256,
    fetch_quarantine_records,
    load_import_state,
    parse_fc27_user_export_json,
    plan_fc27_user_export_import,
    review_quarantine_record,
)
from tests.test_fc27_persistence_and_queries import make_synthetic_export_dict

SYNTHETIC_HMAC_KEY = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
CLUB_ID = "330323"
CLUB_NAME = "East Coasters"
PLATFORM = "common-gen5"
GAME_VERSION = "FC27"


def _enable_polling_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FC27_COMMUNITY_POLLING_ENABLED", "true")
    monkeypatch.setenv("FC27_COMMUNITY_PROVIDER_ENABLED", "true")
    monkeypatch.setenv("FC27_PERSISTENCE_ENABLED", "true")
    monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "true")
    monkeypatch.setenv("FC27_RECOMMENDATIONS_ENABLED", "false")
    monkeypatch.setenv("DISPATCH_ENABLED", "false")
    monkeypatch.setenv("FC27_ALIAS_HMAC_KEY", SYNTHETIC_HMAC_KEY)


def _raw_community_match(
    match_id: str,
    *,
    timestamp: int = 1762000800,
    match_type: str = "leagueMatch",
    goals_for: int = 2,
    goals_against: int = 1,
    opponent_club_id: str = "880101",
    opponent_name: str = "Synthetic Harbor Club",
    reverse_player_keys: bool = False,
    extra_player: bool = False,
    first_player_passes_completed: int = 19,
) -> dict[str, Any]:
    player_entries: list[dict[str, Any]] = [
        {
            "club_id": CLUB_ID,
            "player_id": "700001",
            "player_name": "SyntheticAlpha01",
            "position": "ST",
            "goals": max(0, goals_for - 1),
            "assists": 1,
            "passes_made": first_player_passes_completed,
            "pass_attempts": 24,
            "tackles_made": 2,
            "tackle_attempts": 3,
            "shots": 4,
            "saves": 0,
            "interceptions": 1,
            "rating": 8.2,
            "seconds_played": 5400,
        },
        {
            "club_id": CLUB_ID,
            "player_id": "700002",
            "player_name": "SyntheticBeta02",
            "position": "CM",
            "goals": 1 if goals_for >= 1 else 0,
            "assists": 0,
            "passes_made": 28,
            "pass_attempts": 32,
            "tackles_made": 4,
            "tackle_attempts": 5,
            "shots": 2,
            "saves": 0,
            "interceptions": 3,
            "rating": 7.6,
            "seconds_played": 5400,
        },
        {
            "club_id": CLUB_ID,
            "player_id": "700003",
            "player_name": "SyntheticGamma03",
            "position": "GK",
            "goals": 0,
            "assists": 0,
            "passes_made": 11,
            "pass_attempts": 14,
            "tackles_made": 0,
            "tackle_attempts": 0,
            "shots": 0,
            "saves": 5,
            "interceptions": 0,
            "rating": 7.4,
            "seconds_played": 5400,
        },
    ]
    if extra_player:
        player_entries.append(
            {
                "club_id": CLUB_ID,
                "player_id": "700004",
                "player_name": "SyntheticDelta04",
                "position": "CB",
                "goals": 0,
                "assists": 0,
                "passes_made": 15,
                "pass_attempts": 18,
                "tackles_made": 3,
                "tackle_attempts": 4,
                "shots": 0,
                "saves": 0,
                "interceptions": 2,
                "rating": 7.1,
                "seconds_played": 5400,
            }
        )
    if reverse_player_keys:
        player_entries = list(reversed(player_entries))

    wins = 1 if goals_for > goals_against else 0
    ties = 1 if goals_for == goals_against else 0
    losses = 1 if goals_for < goals_against else 0
    opp_wins = losses
    opp_losses = wins
    res_code = "1" if wins else ("3" if ties else "2")
    opp_res_code = "2" if wins else ("3" if ties else "1")

    return {
        "match_id": match_id,
        "match_type": match_type,
        "timestamp": timestamp,
        "clubs": [
            {
                "club_id": CLUB_ID,
                "club_name": CLUB_NAME,
                "goals": goals_for,
                "goals_against": goals_against,
                "result": res_code,
                "wins": wins,
                "ties": ties,
                "losses": losses,
            },
            {
                "club_id": opponent_club_id,
                "club_name": opponent_name,
                "goals": goals_against,
                "goals_against": goals_for,
                "result": opp_res_code,
                "wins": opp_wins,
                "ties": ties,
                "losses": opp_losses,
            },
        ],
        "players": player_entries,
    }


def _build_user_export_envelope_matching_community_record(
    match_id: str,
    *,
    export_id: str = "exp_user_synth_001",
    played_at: datetime | None = None,
    goals_for: int = 2,
    goals_against: int = 1,
    opponent_name: str = "Synthetic Harbor Club",
    match_type: str = "LEAGUE",
    formation: str = "4-3-3",
    source_classification: str = "USER_SUPPLIED_EXPORT",
    supplier_reference: str = "manual-steward-export-v1",
    shots_on_target: int = 3,
    possession_losses: int = 4,
    role_override: str | None = "Poacher",
    build_override: str | None = "bld_ecf_st_user_v1",
) -> FC27UserExportEnvelope:
    eff_played_at = (
        played_at
        if played_at is not None
        else datetime.fromtimestamp(1762000800, tz=UTC)
    )
    res = (
        "WIN"
        if goals_for > goals_against
        else ("DRAW" if goals_for == goals_against else "LOSS")
    )
    p1 = pseudonymize_east_coasters_identifier("700001")
    p2 = pseudonymize_east_coasters_identifier("700002")
    p3 = pseudonymize_east_coasters_identifier("700003")
    perfs = [
        FC27SuppliedPlayerPerformance(
            local_player_reference=p1,
            position="ST",
            role=role_override or "UNKNOWN",
            minutes_played=90,
            goals=max(0, goals_for - 1),
            assists=1,
            passes_attempted=24,
            passes_completed=19,
            tackles_attempted=3,
            tackles_won=2,
            interceptions=1,
            shots=4,
            shots_on_target=shots_on_target,
            saves=0,
            possession_losses=possession_losses,
            provider_rating=8.2,
            clean_sheet=(goals_against == 0),
            build_reference=build_override,
        ),
        FC27SuppliedPlayerPerformance(
            local_player_reference=p2,
            position="CM",
            role="Playmaker" if role_override else "UNKNOWN",
            minutes_played=90,
            goals=1 if goals_for >= 1 else 0,
            assists=0,
            passes_attempted=32,
            passes_completed=28,
            tackles_attempted=5,
            tackles_won=4,
            interceptions=3,
            shots=2,
            shots_on_target=min(2, shots_on_target),
            saves=0,
            possession_losses=possession_losses,
            provider_rating=7.6,
            clean_sheet=(goals_against == 0),
            build_reference="bld_ecf_cm_user_v1" if build_override else None,
        ),
        FC27SuppliedPlayerPerformance(
            local_player_reference=p3,
            position="GK",
            role=_default_role_for_position("GK") if role_override else "UNKNOWN",
            minutes_played=90,
            goals=0,
            assists=0,
            passes_attempted=14,
            passes_completed=11,
            tackles_attempted=0,
            tackles_won=0,
            interceptions=0,
            shots=0,
            shots_on_target=0,
            saves=5,
            possession_losses=1,
            provider_rating=7.4,
            clean_sheet=(goals_against == 0),
            build_reference="bld_ecf_gk_user_v1" if build_override else None,
        ),
    ]
    gen_dt = datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC)
    raw_dict = make_synthetic_export_dict(
        export_id=export_id,
        club_id=CLUB_ID,
        club_name=CLUB_NAME,
        platform=PLATFORM,
        external_match_id=match_id,
        goals_for=goals_for,
        goals_against=goals_against,
        result=res,
        display_alias=None,
    )
    raw_dict.pop("payload_sha256", None)
    raw_dict["generated_at"] = gen_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    raw_dict["supplied_at"] = gen_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    raw_dict["source_classification"] = source_classification
    raw_dict["supplier_attestation"]["supplier_role"] = "DATA_STEWARD"
    raw_dict["supplier_attestation"]["supplier_reference"] = supplier_reference
    raw_dict["supplier_attestation"]["attested_at"] = gen_dt.strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    raw_dict["consent_attestation"]["consented_at"] = gen_dt.strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    raw_dict["club"]["source_reference"] = supplier_reference
    raw_dict["matches"] = [
        FC27SuppliedMatch(
            external_match_id=match_id,
            played_at=eff_played_at,
            result=res,  # type: ignore[arg-type]
            goals_for=goals_for,
            goals_against=goals_against,
            opponent_name=opponent_name,
            formation=formation,
            player_performances=perfs,
            match_type=match_type,  # type: ignore[arg-type]
            match_status="COMPLETED",
            data_quality_status="PROVISIONAL",
            source_classification=source_classification,  # type: ignore[arg-type]
        ).model_dump(mode="json")
    ]
    raw_dict["payload_sha256"] = compute_payload_sha256(raw_dict)
    return parse_fc27_user_export_json(
        json.dumps(raw_dict),
        evaluation_time=gen_dt + timedelta(minutes=1),
    )


def _seed_historical_comm_eval_batch(
    app_uri: str,
    raw_records: list[dict[str, Any]],
    *,
    export_id: str,
    generated_at: datetime,
) -> tuple[str, dict[str, str]]:
    """Seed a batch reproducing the exact pre-remediation ``comm_eval_*`` persistence contract."""
    _, match_envs = normalize_community_match_records(
        raw_records,
        observed_at="2026-11-01T13:55:00Z",
        alias_hmac_key=SYNTHETIC_HMAC_KEY,
    )
    historical_matches: list[FC27SuppliedMatch] = []
    for m_env in match_envs:
        # Preserve raw stat insertion order (deliberately unsorted) + historical defaults
        perfs: list[FC27SuppliedPlayerPerformance] = []
        for p in m_env.player_statistics:
            perfs.append(
                FC27SuppliedPlayerPerformance(
                    local_player_reference=p.local_player_reference,
                    display_alias=None,
                    position=p.position,
                    role=_default_role_for_position(p.position),
                    minutes_played=p.minutes_played
                    if p.minutes_played is not None
                    else 90,
                    goals=p.goals,
                    assists=p.assists,
                    passes_attempted=p.passes_attempted
                    if p.passes_attempted is not None
                    else 0,
                    passes_completed=p.passes_completed
                    if p.passes_completed is not None
                    else 0,
                    tackles_attempted=p.tackles_attempted
                    if p.tackles_attempted is not None
                    else 0,
                    tackles_won=p.tackles_won if p.tackles_won is not None else 0,
                    interceptions=p.interceptions if p.interceptions is not None else 0,
                    shots=p.shots if p.shots is not None else 0,
                    shots_on_target=p.shots_on_target
                    if p.shots_on_target is not None
                    else 0,
                    saves=p.saves if p.saves is not None else 0,
                    possession_losses=2,
                    provider_rating=p.rating if p.rating is not None else 7.0,
                    clean_sheet=(m_env.east_coasters_side.goals_against == 0),
                    build_reference=_default_build_for_position(p.position),
                )
            )
        historical_matches.append(
            FC27SuppliedMatch(
                external_match_id=m_env.match_id,
                played_at=m_env.played_at,
                result=m_env.result,  # type: ignore[arg-type]
                goals_for=m_env.east_coasters_side.goals,
                goals_against=m_env.east_coasters_side.goals_against,
                opponent_name=m_env.opponent_side.club_name,
                formation="UNKNOWN",
                player_performances=perfs,
                match_type=m_env.normalized_match_type,  # type: ignore[arg-type]
                match_status="COMPLETED",
                data_quality_status="PROVISIONAL",
                source_classification="USER_SUPPLIED_EXPORT",
            )
        )

    env_dict = make_synthetic_export_dict(
        export_id=export_id,
        club_id=CLUB_ID,
        club_name=CLUB_NAME,
        platform=PLATFORM,
        display_alias=None,
    )
    env_dict.pop("payload_sha256", None)
    gen_iso = generated_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    env_dict["generated_at"] = gen_iso
    env_dict["supplied_at"] = gen_iso
    env_dict["source_classification"] = "USER_SUPPLIED_EXPORT"
    env_dict["supplier_attestation"]["supplier_role"] = "CLUB_MANAGER"
    env_dict["supplier_attestation"]["supplier_reference"] = "demo-fixture-loopback-v1"
    env_dict["supplier_attestation"]["attested_at"] = gen_iso
    env_dict["consent_attestation"]["consented_at"] = gen_iso
    env_dict["club"]["source_reference"] = "demo-fixture-loopback-v1"
    env_dict["matches"] = [m.model_dump(mode="json") for m in historical_matches]
    env_dict["payload_sha256"] = compute_payload_sha256(env_dict)
    hist_env = FC27UserExportEnvelope.model_validate(env_dict)
    state = load_import_state(
        app_uri,
        club_id=CLUB_ID,
        platform=PLATFORM,
        game_version=GAME_VERSION,
    )
    plan = plan_fc27_user_export_import(
        hist_env,
        existing_state=state,
        evaluation_time=generated_at,
    )
    res = apply_export_plan(
        app_uri,
        envelope=hist_env,
        plan=plan,
        actor="historical_comm_eval_seed",
        enforce_posture=False,
        quarantine_conflicts=False,
    )
    assert res.status == "APPLIED"
    updated_state = load_import_state(
        app_uri,
        club_id=CLUB_ID,
        platform=PLATFORM,
        game_version=GAME_VERSION,
    )
    return hist_env.payload_sha256, dict(updated_state.existing_match_hashes)


def _db_table_counts(admin_uri: str) -> dict[str, int]:
    with psycopg.connect(admin_uri) as conn:
        return {
            "matches": conn.execute(
                "SELECT COUNT(*) FROM public.fc27_matches"
            ).fetchone()[0],
            "stats": conn.execute(
                "SELECT COUNT(*) FROM public.fc27_player_match_stats"
            ).fetchone()[0],
            "batches": conn.execute(
                "SELECT COUNT(*) FROM public.fc27_export_batches"
            ).fetchone()[0],
            "quarantine": conn.execute(
                "SELECT COUNT(*) FROM public.fc27_quarantine_records"
            ).fetchone()[0],
            "fingerprints": conn.execute(
                "SELECT COUNT(*) FROM public.fc27_player_alias_fingerprints"
            ).fetchone()[0],
            "jobs": conn.execute(
                "SELECT COUNT(*) FROM public.fc27_discord_jobs"
            ).fetchone()[0],
        }


# ---------------------------------------------------------------------------
# Phase 12 (1 - 7): Equivalence across provenance, key order, timestamps, metadata
# ---------------------------------------------------------------------------


def test_01_exact_duplicate_across_user_export_and_live_paths_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    _enable_polling_gates(monkeypatch)
    user_env = _build_user_export_envelope_matching_community_record("950001")
    plan = plan_fc27_user_export_import(
        user_env,
        evaluation_time=datetime(2026, 11, 2, 12, 5, tzinfo=UTC),
    )
    res_user = apply_export_plan(pg_db_migrated.app, envelope=user_env, plan=plan)
    assert res_user.status == "APPLIED"
    assert res_user.accepted_records == 1

    live_env, _live_match_envs = normalize_community_match_records(
        [_raw_community_match("950001")],
        observed_at="2026-11-03T12:00:00Z",
        alias_hmac_key=SYNTHETIC_HMAC_KEY,
    )
    rt = FC27PollingRuntime()
    poll_res = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda: live_env,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert poll_res.poll_status == "NO_NEW_MATCHES"
    assert poll_res.inserted_match_count == 0
    assert poll_res.duplicate_ignored_count == 1
    assert poll_res.replay_conflict_count == 0

    # Reverse direction: persist live match first, then import matching user export
    live_env_2, live_match_envs_2 = normalize_community_match_records(
        [_raw_community_match("950002", timestamp=1762004400)],
        observed_at="2026-11-03T12:00:00Z",
        alias_hmac_key=SYNTHETIC_HMAC_KEY,
    )
    persist_polled_match_envelope(
        pg_db_migrated.poll_writer,
        match_envelope=live_env_2,
        match_envelopes=live_match_envs_2,
        generated_at=datetime(2026, 11, 3, 12, 5, tzinfo=UTC),
    )
    user_env_2 = _build_user_export_envelope_matching_community_record(
        "950002",
        export_id="exp_user_synth_002",
        played_at=datetime.fromtimestamp(1762004400, tz=UTC),
    )
    state_2 = load_import_state(
        pg_db_migrated.app,
        club_id=CLUB_ID,
        platform=PLATFORM,
        game_version=GAME_VERSION,
    )
    plan_2 = plan_fc27_user_export_import(
        user_env_2,
        existing_state=state_2,
        evaluation_time=datetime(2026, 11, 3, 12, 10, tzinfo=UTC),
    )
    assert plan_2.can_apply is True
    assert plan_2.conflicting_match_ids == []
    assert plan_2.duplicate_match_ids == ["950002"]
    apply_2 = apply_export_plan(pg_db_migrated.app, envelope=user_env_2, plan=plan_2)
    assert apply_2.status == "APPLIED"
    assert apply_2.idempotent_duplicate is True
    assert apply_2.replay_conflict is False


def test_02_key_order_differences_in_player_map_and_appearances_are_equivalent() -> (
    None
):
    raw_forward = _raw_community_match("950010", reverse_player_keys=False)
    raw_reversed = _raw_community_match("950010", reverse_player_keys=True)

    env_f, m_envs_f = normalize_community_match_records(
        [raw_forward],
        observed_at="2026-11-03T00:00:00Z",
        alias_hmac_key=SYNTHETIC_HMAC_KEY,
    )
    env_r, m_envs_r = normalize_community_match_records(
        [raw_reversed],
        observed_at="2026-11-03T00:00:00Z",
        alias_hmac_key=SYNTHETIC_HMAC_KEY,
    )
    assert [p.local_player_reference for p in m_envs_f[0].player_statistics] == [
        p.local_player_reference for p in m_envs_r[0].player_statistics
    ]
    assert env_f.record_hashes == env_r.record_hashes
    assert m_envs_f[0].player_statistics == m_envs_r[0].player_statistics

    user_env = _build_user_export_envelope_matching_community_record("950010")
    m_forward = user_env.matches[0]
    m_reversed = m_forward.model_copy(
        update={"player_performances": list(reversed(m_forward.player_performances))}
    )
    # Legacy compute_match_hash is sensitive to list order, proving why projection sorting is required
    assert compute_match_hash(m_forward, club_id=CLUB_ID) != compute_match_hash(
        m_reversed, club_id=CLUB_ID
    )
    proj_f = project_match_equivalence_v1(m_forward, club_id=CLUB_ID)
    proj_r = project_match_equivalence_v1(m_reversed, club_id=CLUB_ID)
    assert MATCH_EQUIVALENCE_CONTRACT_VERSION == "fc27-match-equivalence-v1"
    assert compute_match_equivalence_hash(proj_f) == compute_match_equivalence_hash(
        proj_r
    )
    assert compare_match_equivalence_v1(proj_f, proj_r) == []


def test_03_timestamp_precision_normalization_behaves_as_specified() -> None:
    base_match = _build_user_export_envelope_matching_community_record(
        "950020",
        played_at=datetime(2026, 11, 1, 18, 30, 45, tzinfo=UTC),
    ).matches[0]
    subsecond_match = base_match.model_copy(
        update={"played_at": datetime(2026, 11, 1, 18, 30, 45, 789123, tzinfo=UTC)}
    )
    offset_tz_match = base_match.model_copy(
        update={
            "played_at": datetime(
                2026, 11, 1, 13, 30, 45, tzinfo=timezone(timedelta(hours=-5))
            )
        }
    )
    plus_one_sec_match = base_match.model_copy(
        update={"played_at": datetime(2026, 11, 1, 18, 30, 46, tzinfo=UTC)}
    )

    p_base = project_match_equivalence_v1(base_match, club_id=CLUB_ID)
    p_sub = project_match_equivalence_v1(subsecond_match, club_id=CLUB_ID)
    p_off = project_match_equivalence_v1(offset_tz_match, club_id=CLUB_ID)
    p_diff = project_match_equivalence_v1(plus_one_sec_match, club_id=CLUB_ID)

    assert compare_match_equivalence_v1(p_base, p_sub) == []
    assert compare_match_equivalence_v1(p_base, p_off) == []
    assert compare_match_equivalence_v1(p_base, p_diff) == ["matches[0].played_at"]


def test_04_05_06_source_classification_supplier_reference_and_export_id_do_not_alter_equivalence() -> (
    None
):
    m_base = _build_user_export_envelope_matching_community_record(
        "950030",
        formation="UNKNOWN",
        shots_on_target=0,
        possession_losses=0,
        role_override=None,
        build_override=None,
    ).matches[0]

    p_user = project_match_equivalence_v1(
        m_base,
        club_id=CLUB_ID,
        source_classification="USER_SUPPLIED_EXPORT",
        supplier_reference="steward-manual-batch-01",
        export_id="exp_manual_001",
    )
    p_comm = project_match_equivalence_v1(
        m_base,
        club_id=CLUB_ID,
        source_classification="COMMUNITY_RESEARCH_DERIVED",
        supplier_reference="fc27-community-v1:leagueMatch",
        export_id="comm_poll_abcdef0123456789",
    )
    p_hist = project_match_equivalence_v1(
        m_base,
        club_id=CLUB_ID,
        source_classification="USER_SUPPLIED_EXPORT",
        supplier_reference="demo-fixture-loopback-v1",
        export_id="comm_eval_9876543210fedcba",
    )
    p_auth = project_match_equivalence_v1(
        m_base,
        club_id=CLUB_ID,
        source_classification="AUTHORIZED_CAPTURE",
        supplier_reference="operator-capture-v1",
        export_id="exp_auth_002",
    )

    assert compare_match_equivalence_v1(p_user, p_comm) == []
    assert compare_match_equivalence_v1(p_comm, p_hist) == []
    assert compare_match_equivalence_v1(p_user, p_auth) == []


def test_07_mutable_provider_metadata_and_unobserved_defaults_vs_user_export_strictness() -> (
    None
):
    # Different observed_at timestamps on the live path produce equivalent match projections
    env_a, _m_envs_a = normalize_community_match_records(
        [_raw_community_match("950040")],
        observed_at="2026-11-01T10:00:00Z",
        alias_hmac_key=SYNTHETIC_HMAC_KEY,
    )
    env_b, _m_envs_b = normalize_community_match_records(
        [_raw_community_match("950040")],
        observed_at="2026-11-10T22:30:00Z",
        alias_hmac_key=SYNTHETIC_HMAC_KEY,
    )
    assert env_a.record_hashes == env_b.record_hashes

    # Between two genuine USER_SUPPLIED_EXPORT records (no community provenance),
    # tactical/extended fields (formation, role, build_reference, shots_on_target, possession_losses)
    # remain strictly compared!
    u1 = _build_user_export_envelope_matching_community_record(
        "950040",
        formation="4-3-3",
        shots_on_target=3,
        possession_losses=4,
        role_override="Poacher",
        build_override="bld_ecf_st_user_v1",
    ).matches[0]
    u2 = _build_user_export_envelope_matching_community_record(
        "950040",
        formation="4-4-2",
        shots_on_target=1,
        possession_losses=9,
        role_override="Target Forward",
        build_override="bld_ecf_st_alt_v2",
    ).matches[0]
    pu1 = project_match_equivalence_v1(
        u1,
        club_id=CLUB_ID,
        source_classification="USER_SUPPLIED_EXPORT",
        supplier_reference="manual-user-export-a",
        export_id="exp_user_a",
    )
    pu2 = project_match_equivalence_v1(
        u2,
        club_id=CLUB_ID,
        source_classification="USER_SUPPLIED_EXPORT",
        supplier_reference="manual-user-export-b",
        export_id="exp_user_b",
    )
    diffs = compare_match_equivalence_v1(pu1, pu2)
    assert "matches[0].formation" in diffs
    assert any(d.endswith(".role") for d in diffs)
    assert any(d.endswith(".build_reference") for d in diffs)
    assert any(d.endswith(".shots_on_target") for d in diffs)
    assert any(d.endswith(".possession_losses") for d in diffs)


# ---------------------------------------------------------------------------
# Phase 12 (8 - 13): Material differences remain strictly fail-closed
# ---------------------------------------------------------------------------


def test_08_09_10_11_12_13_material_match_and_player_differences_fail_closed() -> None:
    base = _build_user_export_envelope_matching_community_record("950050").matches[0]
    p_base = project_match_equivalence_v1(
        base,
        club_id=CLUB_ID,
        source_classification="COMMUNITY_RESEARCH_DERIVED",
        supplier_reference="fc27-community-v1:leagueMatch",
        export_id="comm_poll_0001",
    )

    # 8. Goals difference
    goals_changed = base.model_copy(update={"goals_for": 4, "result": "WIN"})
    p_goals = project_match_equivalence_v1(
        goals_changed,
        club_id=CLUB_ID,
        source_classification="COMMUNITY_RESEARCH_DERIVED",
    )
    assert "matches[0].goals_for" in compare_match_equivalence_v1(p_base, p_goals)

    # 9. played_at material difference (60s)
    time_changed = base.model_copy(
        update={"played_at": base.played_at + timedelta(seconds=60)}
    )
    p_time = project_match_equivalence_v1(
        time_changed,
        club_id=CLUB_ID,
        source_classification="COMMUNITY_RESEARCH_DERIVED",
    )
    assert compare_match_equivalence_v1(p_base, p_time) == ["matches[0].played_at"]

    # 10. club_id, platform, game_version differences
    p_club = project_match_equivalence_v1(
        base,
        club_id="999999",
        source_classification="COMMUNITY_RESEARCH_DERIVED",
    )
    p_plat = project_match_equivalence_v1(
        base,
        club_id=CLUB_ID,
        platform="nx",
        source_classification="COMMUNITY_RESEARCH_DERIVED",
    )
    p_ver = project_match_equivalence_v1(
        base,
        club_id=CLUB_ID,
        game_version="FC26",
        source_classification="COMMUNITY_RESEARCH_DERIVED",
    )
    assert compare_match_equivalence_v1(p_base, p_club) == ["matches[0].club_id"]
    assert compare_match_equivalence_v1(p_base, p_plat) == ["matches[0].platform"]
    assert compare_match_equivalence_v1(p_base, p_ver) == ["matches[0].game_version"]

    # 11. match_type explicit mapping vs cross-type conflict
    assert canonicalize_match_type_for_equivalence("leagueMatch") == "LEAGUE"
    assert canonicalize_match_type_for_equivalence("playoffMatch") == "PLAYOFF"
    assert canonicalize_match_type_for_equivalence("friendlyMatch") == "FRIENDLY"
    p_league_mapped = project_match_equivalence_v1(
        base,
        club_id=CLUB_ID,
        source_classification="COMMUNITY_RESEARCH_DERIVED",
        match_type_override="leagueMatch",
    )
    p_playoff = project_match_equivalence_v1(
        base.model_copy(update={"match_type": "PLAYOFF"}),
        club_id=CLUB_ID,
        source_classification="COMMUNITY_RESEARCH_DERIVED",
        match_type_override="playoffMatch",
    )
    assert compare_match_equivalence_v1(p_base, p_league_mapped) == []
    assert compare_match_equivalence_v1(p_base, p_playoff) == ["matches[0].match_type"]

    # 12. Opponent identity difference remains fail-closed
    opp_changed = base.model_copy(update={"opponent_name": "Different Opponent Club"})
    p_opp = project_match_equivalence_v1(
        opp_changed,
        club_id=CLUB_ID,
        source_classification="COMMUNITY_RESEARCH_DERIVED",
    )
    assert compare_match_equivalence_v1(p_base, p_opp) == ["matches[0].opponent_name"]

    # 13. Player-stat material differences (passes_completed and player count) fail closed
    tweaked_perfs = list(base.player_performances)
    tweaked_perfs[0] = tweaked_perfs[0].model_copy(update={"passes_completed": 20})
    p_stat = project_match_equivalence_v1(
        base.model_copy(update={"player_performances": tweaked_perfs}),
        club_id=CLUB_ID,
        source_classification="COMMUNITY_RESEARCH_DERIVED",
    )
    stat_diffs = compare_match_equivalence_v1(p_base, p_stat)
    assert any(d.endswith(".passes_completed") for d in stat_diffs)

    fewer_perfs = list(base.player_performances[:2])
    p_count = project_match_equivalence_v1(
        base.model_copy(update={"player_performances": fewer_perfs}),
        club_id=CLUB_ID,
        source_classification="COMMUNITY_RESEARCH_DERIVED",
    )
    assert compare_match_equivalence_v1(p_base, p_count) == [
        "matches[0].player_performances.length"
    ]


# ---------------------------------------------------------------------------
# Phase 12 (14 - 18): Atomic rollback, quarantine, checkpoints, restart circuit
# ---------------------------------------------------------------------------


def test_14_15_16_17_18_unresolved_conflict_rollback_quarantine_checkpoint_and_durable_circuit(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    _enable_polling_gates(monkeypatch)

    # Seed 8 historical matches via pre-remediation comm_eval_* shape
    seed_raw = [
        _raw_community_match(f"96000{i}", timestamp=1762000000 + i * 3600)
        for i in range(1, 9)
    ]
    _, initial_hashes = _seed_historical_comm_eval_batch(
        pg_db_migrated.app,
        seed_raw,
        export_id="comm_eval_seed8_0001",
        generated_at=datetime(2026, 11, 1, 15, 0, tzinfo=UTC),
    )
    initial_club_ts = datetime(2026, 11, 1, 15, 0, tzinfo=UTC)
    with psycopg.connect(pg_db_migrated.admin) as conn:
        conn.execute(
            "UPDATE public.fc27_clubs SET updated_at = %s WHERE club_id = %s",
            (initial_club_ts, CLUB_ID),
        )
        conn.commit()
        before_match_snapshot = conn.execute(
            """
            SELECT external_match_id, match_hash, source_classification, export_id, goals_for, opponent_name
              FROM public.fc27_matches
             ORDER BY external_match_id
            """
        ).fetchall()

    counts_before = _db_table_counts(pg_db_migrated.admin)
    assert counts_before["matches"] == 8
    assert counts_before["stats"] == 24
    assert counts_before["fingerprints"] == 0
    assert counts_before["jobs"] == 0
    assert counts_before["quarantine"] == 0

    # Construct 10-match live poll window:
    # - 7 matches identical to seeded historical matches (equivalent via fc27-match-equivalence-v1)
    # - 1 match ("960003") materially altered (goals_for changed 2 -> 0, WIN -> LOSS)
    # - 2 brand-new matches ("960009", "960010") that MUST NOT partially commit
    poll_raw = [
        _raw_community_match(
            f"96000{i}",
            timestamp=1762000000 + i * 3600,
            goals_for=(0 if i == 3 else 2),
            reverse_player_keys=True,
        )
        for i in range(1, 11)
    ]
    poll_env, poll_match_envs = normalize_community_match_records(
        poll_raw,
        observed_at="2026-11-04T12:00:00Z",
        alias_hmac_key=SYNTHETIC_HMAC_KEY,
    )
    assert len(poll_env.alias_fingerprints) > 0

    class _StubAdapter:
        def __init__(self) -> None:
            self.last_match_envelopes = poll_match_envs
            self.calls = 0

        def get_east_coasters_recent_matches_once(self, **kwargs: Any) -> Any:
            self.calls += 1
            return poll_env

    stub_adapter = _StubAdapter()
    rt_run_b = FC27PollingRuntime()
    poll_now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    res_conflict = run_fc27_poll_cycle(
        runtime=rt_run_b,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        adapter=stub_adapter,  # type: ignore[arg-type]
        now_utc_fn=lambda: poll_now,
        use_cache=False,
        use_rate_limiter=False,
    )

    # Exact conflict count = 1 (NOT inflated to 10!), circuit OPEN, 1 request, 0 partial writes
    assert stub_adapter.calls == 1
    assert res_conflict.poll_status == "REPLAY_CONFLICT"
    assert res_conflict.provider_requests == 1
    assert res_conflict.source_match_count == 10
    assert res_conflict.accepted_match_count == 10
    assert res_conflict.inserted_match_count == 0
    assert res_conflict.replay_conflict_count == 1
    assert res_conflict.circuit_state == "OPEN"
    assert res_conflict.analytics_executed is False

    counts_after = _db_table_counts(pg_db_migrated.admin)
    assert counts_after["matches"] == 8
    assert counts_after["stats"] == 24
    assert counts_after["batches"] == counts_before["batches"]
    assert counts_after["fingerprints"] == 0
    assert counts_after["jobs"] == 0
    assert counts_after["quarantine"] == 1

    # Verify retained records, hashes, club updated_at checkpoint, and sanitized quarantine row
    with psycopg.connect(pg_db_migrated.admin) as conn:
        after_match_snapshot = conn.execute(
            """
            SELECT external_match_id, match_hash, source_classification, export_id, goals_for, opponent_name
              FROM public.fc27_matches
             ORDER BY external_match_id
            """
        ).fetchall()
        assert after_match_snapshot == before_match_snapshot
        club_updated = conn.execute(
            "SELECT updated_at FROM public.fc27_clubs WHERE club_id = %s",
            (CLUB_ID,),
        ).fetchone()[0]
        assert club_updated == initial_club_ts

    q_rows = fetch_quarantine_records(
        pg_db_migrated.app,
        club_id=CLUB_ID,
        review_status="PENDING_REVIEW",
    )
    assert len(q_rows) == 1
    q_rec = q_rows[0]
    expected_mref = pseudonymize_match_ref("960003", club_id=CLUB_ID)
    assert q_rec.external_match_id == expected_mref
    assert q_rec.external_match_id != "960003"
    assert q_rec.source_classification == "COMMUNITY_RESEARCH_DERIVED"
    assert q_rec.anomaly_category == "REPLAY_CONFLICT"
    assert q_rec.reason_code == "MATCH_RECORD_HASH_CONFLICT"
    assert any(p.endswith(".goals_for") for p in q_rec.diagnostic_paths)
    assert any(p.endswith(".result") for p in q_rec.diagnostic_paths)

    # 17. Simulate process restart: a brand-new FC27PollingRuntime recovers PENDING_REVIEW
    # polling conflict from PostgreSQL and blocks before any provider request!
    restarted_rt = FC27PollingRuntime()
    assert restarted_rt.circuit_breaker.state is FC27CircuitState.CLOSED

    def _must_not_call_provider(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "Provider transport must not be called while conflict is PENDING_REVIEW"
        )

    res_restarted = run_fc27_poll_cycle(
        runtime=restarted_rt,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=_must_not_call_provider,
        now_utc_fn=lambda: poll_now + timedelta(hours=24),
        use_cache=False,
        use_rate_limiter=False,
    )
    assert res_restarted.poll_status == "PROVIDER_BLOCKED"
    assert res_restarted.error_category == "CIRCUIT_BREAKER_OPEN"
    assert res_restarted.provider_requests == 0
    assert restarted_rt.circuit_breaker.state is FC27CircuitState.OPEN

    # Operator reviews quarantine record via fc27_app and performs audited manual circuit reset
    review_event_id = review_quarantine_record(
        pg_db_migrated.app,
        quarantine_id=q_rec.quarantine_id,
        review_status="REJECTED_PERMANENT",
        reviewed_by="ops_data_steward_01",
        review_notes="Rejected upstream goals discrepancy pending steward audit",
    )
    assert review_event_id > 0
    reviewed_rows = fetch_quarantine_records(
        pg_db_migrated.app,
        club_id=CLUB_ID,
        review_status="REJECTED_PERMANENT",
    )
    assert len(reviewed_rows) == 1
    assert reviewed_rows[0].quarantine_id == q_rec.quarantine_id

    with pytest.raises(FC27ProviderError):
        restarted_rt.circuit_breaker.manual_reset(authorization_reference="")
    with pytest.raises(FC27ProviderError):
        restarted_rt.circuit_breaker.manual_reset(
            authorization_reference="user@example.com"
        )

    restarted_rt.circuit_breaker.manual_reset(
        authorization_reference="OPS-CIRCUIT-RESET-2026-1113"
    )
    meta = restarted_rt.circuit_breaker.safe_metadata()
    assert meta["state"] == "CLOSED"
    assert meta["reset_count"] == 1
    assert meta["last_reset_authorization_reference"] == "OPS-CIRCUIT-RESET-2026-1113"
    assert meta["last_reset_at_monotonic"] is not None

    # After quarantine resolution, a fresh runtime no longer trips OPEN during recovery
    clean_rt = FC27PollingRuntime()
    recover_polling_state_from_postgres(
        pg_db_migrated.poll_writer,
        runtime=clean_rt,
    )
    assert clean_rt.circuit_breaker.state is FC27CircuitState.CLOSED
    assert (
        dict(
            load_import_state(
                pg_db_migrated.app,
                club_id=CLUB_ID,
                platform=PLATFORM,
                game_version=GAME_VERSION,
            ).existing_match_hashes
        )
        == initial_hashes
    )


# ---------------------------------------------------------------------------
# Phase 12 (19 - 20): Historical 3-batch B01..B03 lineage & 17-match compatibility
# ---------------------------------------------------------------------------


def test_19_20_three_batch_historical_comm_eval_lineage_reconciles_17_matches_cleanly(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    _enable_polling_gates(monkeypatch)

    # Mirror the audited retained 17-match dataset across 3 overlapping 10-match batches:
    # B01: M08..M17 (10 matches)
    # B02: M04..M13 (4 new + 6 duplicates = 14 cumulative)
    # B03: M01..M10 (3 new + 7 duplicates = 17 cumulative)
    all_17 = [
        _raw_community_match(
            f"9700{idx:02d}",
            timestamp=1762800000 - idx * 7200,
            goals_for=(idx % 4),
            goals_against=((idx + 1) % 3),
            opponent_club_id=f"8802{idx:02d}",
            opponent_name=f"Synthetic Club {idx:02d}",
            reverse_player_keys=(idx % 2 == 1),
            extra_player=(idx % 3 == 0),
        )
        for idx in range(1, 18)
    ]
    b01_raw = all_17[7:17]  # 10 oldest (M08..M17)
    b02_raw = all_17[3:13]  # 4 newer + 6 overlap (M04..M13)
    b03_raw = all_17[0:10]  # 3 newest + 7 overlap (M01..M10)

    _seed_historical_comm_eval_batch(
        pg_db_migrated.app,
        b01_raw,
        export_id="comm_eval_hist_b01",
        generated_at=datetime(2026, 11, 1, 13, 59, 39, tzinfo=UTC),
    )
    _seed_historical_comm_eval_batch(
        pg_db_migrated.app,
        b02_raw,
        export_id="comm_eval_hist_b02",
        generated_at=datetime(2026, 11, 8, 16, 7, 58, tzinfo=UTC),
    )
    _, stored_hashes_17 = _seed_historical_comm_eval_batch(
        pg_db_migrated.app,
        b03_raw,
        export_id="comm_eval_hist_b03",
        generated_at=datetime(2026, 11, 10, 14, 59, 51, tzinfo=UTC),
    )
    assert len(stored_hashes_17) == 17

    # Now poll the latest 10-match window (M01..M10) with reversed JSON player key order
    # through the remediated COMMUNITY_RESEARCH_DERIVED worker path
    live_window_10 = [
        _raw_community_match(
            f"9700{idx:02d}",
            timestamp=1762800000 - idx * 7200,
            goals_for=(idx % 4),
            goals_against=((idx + 1) % 3),
            opponent_club_id=f"8802{idx:02d}",
            opponent_name=f"Synthetic Club {idx:02d}",
            reverse_player_keys=idx % 2 != 1,
            extra_player=(idx % 3 == 0),
        )
        for idx in range(1, 11)
    ]
    live_env, live_match_envs = normalize_community_match_records(
        live_window_10,
        observed_at="2026-11-13T18:00:00Z",
        alias_hmac_key=SYNTHETIC_HMAC_KEY,
    )

    class _WindowAdapter:
        last_match_envelopes = live_match_envs

        def get_east_coasters_recent_matches_once(self, **kwargs: Any) -> Any:
            return live_env

    rt = FC27PollingRuntime()
    res = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        adapter=_WindowAdapter(),  # type: ignore[arg-type]
        match_type_override="leagueMatch",
        now_utc_fn=lambda: datetime(2026, 11, 13, 18, 0, tzinfo=UTC),
        use_cache=False,
        use_rate_limiter=False,
    )
    assert res.poll_status == "NO_NEW_MATCHES"
    assert res.source_match_count == 10
    assert res.accepted_match_count == 10
    assert res.inserted_match_count == 0
    assert res.duplicate_ignored_count == 10
    assert res.replay_conflict_count == 0
    assert res.circuit_state == "CLOSED"

    # All 17 historical stored hashes and source_classifications remain untouched
    after_hashes = dict(
        load_import_state(
            pg_db_migrated.app,
            club_id=CLUB_ID,
            platform=PLATFORM,
            game_version=GAME_VERSION,
        ).existing_match_hashes
    )
    assert after_hashes == stored_hashes_17


# ---------------------------------------------------------------------------
# Phase 12 (21 - 24): Privacy, Discord/worker boundaries, budget, role grants
# ---------------------------------------------------------------------------


def test_21_no_raw_identity_appears_in_errors_quarantine_or_audit_output(
    pg_db_migrated,
) -> None:
    raw_ext_id = "989877665544"
    raw_player_id = "700001"
    raw_gamertag = "SyntheticAlpha01"
    raw_opponent = "Secretive Opponent Club"
    hmac_digest = compute_alias_hmac_fingerprint(raw_gamertag, SYNTHETIC_HMAC_KEY)

    # Intra-batch duplicate conflict redacts raw match_id to mref_ecf_<12hex>
    m1 = _raw_community_match(raw_ext_id, goals_for=1, opponent_name=raw_opponent)
    m2 = _raw_community_match(raw_ext_id, goals_for=3, opponent_name=raw_opponent)
    with pytest.raises(FC27ReplayConflictError) as dup_exc_info:
        normalize_community_match_records(
            [m1, m2],
            observed_at="2026-11-05T00:00:00Z",
            alias_hmac_key=SYNTHETIC_HMAC_KEY,
        )
    err_text = str(dup_exc_info.value)
    assert raw_ext_id not in err_text
    assert pseudonymize_match_ref(raw_ext_id, club_id=CLUB_ID) in err_text
    assert raw_gamertag not in err_text
    assert raw_player_id not in err_text

    # Persistence conflict quarantine contains zero raw match IDs, opponent names, gamertags, or HMACs
    env_1, m_envs_1 = normalize_community_match_records(
        [m1],
        observed_at="2026-11-05T00:00:00Z",
        alias_hmac_key=SYNTHETIC_HMAC_KEY,
    )
    persist_polled_match_envelope(
        pg_db_migrated.poll_writer,
        match_envelope=env_1,
        match_envelopes=m_envs_1,
        generated_at=datetime(2026, 11, 5, 0, 5, tzinfo=UTC),
    )

    m_opp_diff = _raw_community_match(
        raw_ext_id,
        goals_for=1,
        opponent_name="Changed Secretive Opponent",
    )
    env_2, m_envs_2 = normalize_community_match_records(
        [m_opp_diff],
        observed_at="2026-11-05T01:00:00Z",
        alias_hmac_key=SYNTHETIC_HMAC_KEY,
    )
    with pytest.raises(FC27ReplayConflictError) as repo_exc_info:
        persist_polled_match_envelope(
            pg_db_migrated.poll_writer,
            match_envelope=env_2,
            match_envelopes=m_envs_2,
            generated_at=datetime(2026, 11, 5, 1, 5, tzinfo=UTC),
        )
    repo_err_text = str(repo_exc_info.value)
    q_rows = fetch_quarantine_records(pg_db_migrated.app, club_id=CLUB_ID)
    assert len(q_rows) == 1
    serialized_q = json.dumps(q_rows[0].model_dump(mode="json"))
    assert scan_for_sensitive_fields(q_rows[0].model_dump(mode="json")) == []

    for forbidden in (
        raw_ext_id,
        raw_player_id,
        raw_gamertag,
        raw_opponent,
        "Changed Secretive Opponent",
        SYNTHETIC_HMAC_KEY,
        hmac_digest,
    ):
        assert forbidden not in repo_err_text
        assert forbidden not in serialized_q


def test_22_23_24_worker_bot_provider_boundaries_request_budget_and_role_grants(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    # 24. Migration max == 0015 and Grants Policy == 16
    assert GRANTS_POLICY_VERSION == 16
    with psycopg.connect(pg_db_migrated.admin) as conn:
        max_mig = conn.execute(
            "SELECT MAX(version) FROM public.schema_migrations"
        ).fetchone()[0]
        assert str(max_mig).startswith("0015")

    # fc27_poll_writer cannot UPDATE/DELETE quarantine rows or write domain_events
    with psycopg.connect(pg_db_migrated.poll_writer) as pw_conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            pw_conn.execute("DELETE FROM public.fc27_quarantine_records")
        pw_conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            pw_conn.execute(
                "UPDATE public.fc27_quarantine_records SET review_status = 'RESOLVED'"
            )
        pw_conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            pw_conn.execute(
                """
                INSERT INTO public.domain_events (
                    event_type, entity_type, entity_id, actor, correlation_id, visibility
                ) VALUES ('x', 'y', 'z', 'a', gen_random_uuid(), 'ops')
                """
            )
        pw_conn.rollback()

    # fc27_discord_reader cannot INSERT into fc27_quarantine_records or fc27_matches
    with psycopg.connect(pg_db_migrated.discord_reader) as dr_conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            dr_conn.execute("DELETE FROM public.fc27_matches")
        dr_conn.rollback()

    # 22. Disabled-by-default worker and read-only Discord bot execute 0 provider requests
    monkeypatch.setenv("FC27_COMMUNITY_POLLING_ENABLED", "false")
    disabled_res = run_fc27_poll_cycle(
        runtime=FC27PollingRuntime(),
        confirm_live_polling=False,
        db_conn_or_uri=pg_db_migrated.poll_writer,
    )
    assert disabled_res.poll_status == "DISABLED"
    assert disabled_res.provider_requests == 0

    bot_svc = FC27DiscordBotService(
        FC27DiscordBotConfig(
            enabled=True,
            allowed_guild_ids=frozenset({"111"}),
            allowed_channel_ids=frozenset({"222"}),
        ),
        db_conn_or_uri=pg_db_migrated.discord_reader,
    )
    status_resp = bot_svc.handle_slash_command(
        "status", guild_id="111", channel_id="222", user_id="333"
    )
    assert status_resp.status == "OK"
    assert status_resp.provider_requests_executed == 0

    # 23. Discord job idempotency conflict rolls back domain writes, records 1 quarantine row,
    # and enforces 1-request / 0-retry budget
    _enable_polling_gates(monkeypatch)
    m_raw = [_raw_community_match("985501", timestamp=1762900000)]
    env_job, m_envs_job = normalize_community_match_records(
        m_raw,
        observed_at="2026-11-06T12:00:00Z",
        alias_hmac_key=SYNTHETIC_HMAC_KEY,
    )
    mref = pseudonymize_match_ref("985501", club_id=CLUB_ID)
    idem_key = f"fc27:post_new_results:{mref}"
    with psycopg.connect(pg_db_migrated.admin) as conn:
        conn.execute(
            """
            INSERT INTO public.fc27_discord_jobs (
                job_type, idempotency_key, payload, state, attempt_count
            ) VALUES (
                'post_new_results', %s, %s::jsonb, 'pending', 0
            )
            """,
            (
                idem_key,
                json.dumps(
                    {
                        "schema_version": "fc27-post-new-results-v1",
                        "event_type": "post_new_results",
                        "match_ref": mref,
                        "club_id": CLUB_ID,
                        "platform": PLATFORM,
                        "game_version": GAME_VERSION,
                        "match_type": "playoffMatch",  # Conflicts with leagueMatch
                    }
                ),
            ),
        )
        conn.commit()

    calls = 0

    class _OneShotJobConflictAdapter:
        last_match_envelopes = m_envs_job

        def get_east_coasters_recent_matches_once(self, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            return env_job

    rt_job = FC27PollingRuntime()
    job_conflict_res = run_fc27_poll_cycle(
        runtime=rt_job,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        adapter=_OneShotJobConflictAdapter(),  # type: ignore[arg-type]
        match_type_override="leagueMatch",
        now_utc_fn=lambda: datetime(2026, 11, 6, 12, 0, tzinfo=UTC),
        use_cache=False,
        use_rate_limiter=False,
    )
    assert calls == 1
    assert job_conflict_res.provider_requests == 1
    assert job_conflict_res.poll_status == "REPLAY_CONFLICT"
    assert job_conflict_res.error_category == "DISCORD_JOB_IDEMPOTENCY_CONFLICT"
    assert job_conflict_res.replay_conflict_count == 1
    assert job_conflict_res.discord_job_conflict_count == 1
    assert job_conflict_res.inserted_match_count == 0
    assert rt_job.circuit_breaker.state is FC27CircuitState.OPEN

    post_counts = _db_table_counts(pg_db_migrated.admin)
    assert post_counts["matches"] == 0
    assert post_counts["stats"] == 0
    assert post_counts["fingerprints"] == 0
    assert post_counts["jobs"] == 1
    assert post_counts["quarantine"] == 1
