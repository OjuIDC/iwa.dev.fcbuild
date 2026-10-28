"""Integration and unit tests for the read-only FC 27 analytics query boundary.

Verifies all 38 Phase E7 requirements using synthetic data only against an isolated
PostgreSQL 16 database migrated through 0012 + Grants Policy Version 14.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

import psycopg
import psycopg.errors
import psycopg.pq
import pytest

from src import settings
from src.db_migrate import migrate
from src.db_roles import bootstrap_cluster, bootstrap_database
from src.fc27_analytics import FORMULA_VERSION
from src.fc27_dashboard_queries import (
    ALL_ANALYTICS_SQL_STATEMENTS,
    FC27_ANALYTICS_MAX_MATCHES,
    FC27_ANALYTICS_MAX_PLAYERS,
    FC27_ANALYTICS_WINDOW_DAYS,
    SQL_SELECT_PLAYER_STATS_NO_ALIAS,
    AnalyticsWindowSpec,
    FC27AnalyticsDisabledError,
    FC27AnalyticsError,
    FC27AnalyticsTimeoutError,
    _read_only_cursor,
    build_club_analytics_snapshot,
    fetch_club_matches_window,
    fetch_club_storage_summary,
    fetch_data_quality_summary,
    fetch_player_performances_window,
)
from src.fc27_provider import FC27TransportClient
from src.fc27_repository import (
    apply_export_plan,
    insert_quarantine_record,
    load_existing_match_hashes,
)
from src.fc27_user_export import (
    EAST_COASTERS_CLUB_ID,
    EAST_COASTERS_CLUB_NAME,
    EAST_COASTERS_GAME_VERSION,
    EAST_COASTERS_PLATFORM,
    compute_payload_sha256,
    parse_fc27_user_export_json,
    plan_fc27_user_export_import,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
REF_NOW = datetime(2026, 10, 27, 20, 0, 0, tzinfo=timezone.utc)


@dataclass
class AnalyticsDbContext:
    admin: str
    owner: str
    app: str
    reader: str
    dispatch: str
    dbname: str


def _sha256_hex(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _make_match_dict(
    external_match_id: str,
    played_at: datetime,
    *,
    result: str = "WIN",
    goals_for: int = 2,
    goals_against: int = 1,
    formation: str = "4-3-3(2)",
) -> dict[str, Any]:
    match_str = played_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    p1_goals = min(goals_for, 1)
    p2_goals = max(0, goals_for - p1_goals)
    return {
        "external_match_id": external_match_id,
        "played_at": match_str,
        "result": result,
        "goals_for": goals_for,
        "goals_against": goals_against,
        "opponent_name": "Synthetic Atlantic FC",
        "formation": formation,
        "lineup": ["player_st_01", "player_cm_02"],
        "player_performances": [
            {
                "local_player_reference": "player_st_01",
                "display_alias": "Approved_Alias_01",
                "position": "ST",
                "role": "Advanced Forward",
                "minutes_played": 90,
                "goals": p1_goals,
                "assists": 1,
                "passes_attempted": 25,
                "passes_completed": 21,
                "tackles_attempted": 4,
                "tackles_won": 3,
                "interceptions": 2,
                "shots": max(2, p1_goals),
                "shots_on_target": max(1, p1_goals),
                "saves": 0,
                "possession_losses": 2,
                "provider_rating": 8.4,
                "build_reference": "bld_synth_st_01",
            },
            {
                "local_player_reference": "player_cm_02",
                "display_alias": "Unapproved_Alias_02",
                "position": "CM",
                "role": "Playmaker",
                "minutes_played": 90,
                "goals": p2_goals,
                "assists": 1,
                "passes_attempted": 35,
                "passes_completed": 30,
                "tackles_attempted": 5,
                "tackles_won": 4,
                "interceptions": 3,
                "shots": max(2, p2_goals),
                "shots_on_target": max(1, p2_goals),
                "saves": 0,
                "possession_losses": 3,
                "provider_rating": 8.1,
                "build_reference": "bld_synth_cm_02",
            },
        ],
        "match_status": "COMPLETED",
    }


def _make_export_dict(
    export_id: str,
    matches: list[dict[str, Any]],
    *,
    club_id: str = EAST_COASTERS_CLUB_ID,
    club_name: str = EAST_COASTERS_CLUB_NAME,
    platform: str = EAST_COASTERS_PLATFORM,
) -> dict[str, Any]:
    gen_str = (REF_NOW - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    sup_str = (REF_NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    data: dict[str, Any] = {
        "export_id": export_id,
        "contract_version": "1.0.0",
        "schema_version": "1.0.0",
        "generated_at": gen_str,
        "supplied_at": sup_str,
        "provider": "EA_FC_PRO_CLUBS",
        "game_version": EAST_COASTERS_GAME_VERSION,
        "platform": platform,
        "club": {
            "club_id": club_id,
            "club_name": club_name,
            "platform": platform,
            "game_version": EAST_COASTERS_GAME_VERSION,
            "supplied_identity_state": "PROVISIONAL",
            "source_reference": "synthetic_test_export",
        },
        "supplier_attestation": {
            "supplier_role": "CLUB_MANAGER",
            "supplier_reference": "steward-synth-ref-01",
            "attested_at": sup_str,
            "authority_scope": [
                "CLUB_IDENTITY",
                "MATCH_RESULTS",
                "LINEUPS",
                "PLAYER_PERFORMANCE",
                "BUILD_REFERENCES",
            ],
            "statement_version": "1.0.0",
        },
        "consent_attestation": {
            "consent_version": "1.0.0",
            "consented_at": gen_str,
            "data_categories": [
                "CLUB_IDENTITY",
                "MATCH_RESULTS",
                "LINEUPS",
                "PLAYER_PERFORMANCE",
                "BUILD_REFERENCES",
            ],
            "retention_acknowledged": True,
            "analytics_use_acknowledged": True,
            "team_review_acknowledged": True,
            "revoked_at": None,
        },
        "matches": matches,
        "source_classification": "USER_SUPPLIED_EXPORT",
    }
    data["payload_sha256"] = compute_payload_sha256(data)
    return data


def _plan_and_apply(conn_uri: str, export_dict: dict[str, Any]) -> None:
    envelope = parse_fc27_user_export_json(json.dumps(export_dict))
    apply_export_plan(conn_uri, envelope=envelope)


@pytest.fixture(scope="module")
def pg_analytics_db(pg_admin_uri: str) -> Iterator[AnalyticsDbContext]:
    """Create a module-scoped PostgreSQL 16 database migrated through 0012 and seeded with synthetic data."""
    bootstrap_cluster(
        pg_admin_uri,
        {
            "fc27_migrator": None,
            "fc27_app": None,
            "fc27_dispatch": None,
            "fc27_relay": None,
        },
    )
    dbname = f"fc27_analytics_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(pg_admin_uri, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{dbname}"')

    info = psycopg.conninfo.conninfo_to_dict(pg_admin_uri)
    info["dbname"] = dbname
    admin_db_uri = psycopg.conninfo.make_conninfo(**info)
    bootstrap_database(admin_db_uri, legacy_owner=None, confirm=True)

    def _role_uri(user: str) -> str:
        d = dict(info)
        d["user"] = user
        return psycopg.conninfo.make_conninfo(**d)

    ctx = AnalyticsDbContext(
        admin=admin_db_uri,
        owner=_role_uri("fc27_migrator"),
        app=_role_uri("fc27_app"),
        reader=_role_uri("fc27_app"),
        dispatch=_role_uri("fc27_dispatch"),
        dbname=dbname,
    )
    migrate(ctx.owner)

    m1 = _make_match_dict("m_syn_001", REF_NOW - timedelta(days=2), result="WIN", goals_for=3, goals_against=1, formation="4-3-3(2)")
    m2 = _make_match_dict("m_syn_002", REF_NOW - timedelta(days=5), result="DRAW", goals_for=2, goals_against=2, formation="4-3-3(2)")
    m3 = _make_match_dict("m_syn_003", REF_NOW - timedelta(days=10), result="WIN", goals_for=2, goals_against=0, formation="4-2-3-1")
    m4 = _make_match_dict("m_syn_004", REF_NOW - timedelta(days=25), result="LOSS", goals_for=1, goals_against=2, formation="4-2-3-1")
    m5 = _make_match_dict("m_syn_005", REF_NOW - timedelta(days=60), result="WIN", goals_for=2, goals_against=1, formation="4-3-3(2)")
    m6_old = _make_match_dict("m_syn_006", REF_NOW - timedelta(days=120), result="WIN", goals_for=4, goals_against=0, formation="4-3-3(2)")

    with patch.dict("os.environ", {"FC27_PERSISTENCE_ENABLED": "true"}, clear=False):
        # Batch 1: m1..m5
        _plan_and_apply(ctx.app, _make_export_dict("exp-main-330323", [m1, m2, m3, m4, m5]))
        # Batch 2: m1, m2 (duplicates) + m6_old (new) -> total_matches=3, accepted_matches=1, duplicate_matches=2
        _plan_and_apply(ctx.app, _make_export_dict("exp-dup-330323", [m1, m2, m6_old]))

        insert_quarantine_record(
            ctx.app,
            club_id=EAST_COASTERS_CLUB_ID,
            platform=EAST_COASTERS_PLATFORM,
            game_version=EAST_COASTERS_GAME_VERSION,
            external_match_id="m_conflict_01",
            source_classification="SYNTHETIC",
            anomaly_category="REPLAY_CONFLICT",
            reason_code="REPLAY_HASH_MISMATCH",
            diagnostic_paths=["matches[0].goals_for"],
            payload_sha256=_sha256_hex("q-replay-1"),
            record_hash=_sha256_hex("r-replay-1"),
            quarantined_at=REF_NOW - timedelta(days=3),
            expires_at=REF_NOW + timedelta(days=27),
        )
        insert_quarantine_record(
            ctx.app,
            club_id=EAST_COASTERS_CLUB_ID,
            platform=EAST_COASTERS_PLATFORM,
            game_version=EAST_COASTERS_GAME_VERSION,
            external_match_id="m_anomaly_01",
            source_classification="SYNTHETIC",
            anomaly_category="VALIDATION_ANOMALY",
            reason_code="GOAL_INVARIANT_MISMATCH",
            diagnostic_paths=["matches[1].goals_for"],
            payload_sha256=_sha256_hex("q-anom-1"),
            quarantined_at=REF_NOW - timedelta(days=4),
            expires_at=REF_NOW + timedelta(days=26),
        )
        insert_quarantine_record(
            ctx.app,
            club_id=EAST_COASTERS_CLUB_ID,
            platform=EAST_COASTERS_PLATFORM,
            game_version=EAST_COASTERS_GAME_VERSION,
            external_match_id="m_drift_01",
            source_classification="SYNTHETIC",
            anomaly_category="SCHEMA_DRIFT",
            reason_code="UNKNOWN_FIELD_DETECTED",
            diagnostic_paths=["matches[2].unexpected_key"],
            payload_sha256=_sha256_hex("q-drift-1"),
            quarantined_at=REF_NOW - timedelta(days=4),
            expires_at=REF_NOW + timedelta(days=26),
        )
        insert_quarantine_record(
            ctx.app,
            club_id=EAST_COASTERS_CLUB_ID,
            platform=EAST_COASTERS_PLATFORM,
            game_version=EAST_COASTERS_GAME_VERSION,
            external_match_id="m_oor_01",
            source_classification="SYNTHETIC",
            anomaly_category="OUT_OF_RANGE_OBSERVATION",
            reason_code="RATING_OUT_OF_RANGE",
            diagnostic_paths=["matches[3].player_performances[0].provider_rating"],
            payload_sha256=_sha256_hex("q-oor-1"),
            quarantined_at=REF_NOW - timedelta(days=4),
            expires_at=REF_NOW + timedelta(days=26),
        )
        insert_quarantine_record(
            ctx.app,
            club_id=EAST_COASTERS_CLUB_ID,
            platform=EAST_COASTERS_PLATFORM,
            game_version=EAST_COASTERS_GAME_VERSION,
            external_match_id="m_expired_01",
            source_classification="SYNTHETIC",
            anomaly_category="VALIDATION_ANOMALY",
            reason_code="EXPIRED_ANOMALY",
            diagnostic_paths=["matches[4].formation"],
            payload_sha256=_sha256_hex("q-expired-1"),
            quarantined_at=REF_NOW - timedelta(days=45),
            expires_at=REF_NOW - timedelta(days=15),
        )

    with psycopg.connect(ctx.admin, autocommit=True) as conn:
        with conn.cursor() as cur:
            # Approve alias visibility for player_st_01 only
            cur.execute(
                """
                UPDATE public.fc27_player_match_stats
                SET alias_visibility_approved = true
                WHERE club_id = %s AND local_player_reference = 'player_st_01'
                """,
                (EAST_COASTERS_CLUB_ID,),
            )

            # Seed another club (999888) and another platform (nx) for isolation tests
            for cid, cname, plat, eid, mid in [
                ("999888", "Other Synthetic Club", EAST_COASTERS_PLATFORM, "exp-iso-club", "m_other_club_01"),
                (EAST_COASTERS_CLUB_ID, EAST_COASTERS_CLUB_NAME, "nx", "exp-iso-plat", "m_other_plat_01"),
            ]:
                cur.execute(
                    """
                    INSERT INTO public.fc27_clubs (
                        club_id, platform, game_version, club_name,
                        identity_state, manager_authority_status, source_classification
                    ) VALUES (%s, %s, %s, %s, 'SYNTHETIC_ONLY', 'UNVERIFIED', 'SYNTHETIC')
                    ON CONFLICT (club_id, platform, game_version) DO NOTHING
                    """,
                    (cid, plat, EAST_COASTERS_GAME_VERSION, cname),
                )
                cur.execute(
                    """
                    INSERT INTO public.fc27_export_batches (
                        export_id, club_id, platform, game_version,
                        contract_version, schema_version, source_classification,
                        supplier_role, supplier_reference, consent_version,
                        consented_categories, payload_sha256,
                        total_matches, accepted_matches, quarantined_matches
                    ) VALUES (
                        %s, %s, %s, %s,
                        '1.0.0', '1.0.0', 'SYNTHETIC',
                        'CLUB_MANAGER', 'steward-synth-ref-01', '1.0.0',
                        '["MATCH_RESULTS"]'::jsonb, %s,
                        1, 1, 0
                    )
                    """,
                    (eid, cid, plat, EAST_COASTERS_GAME_VERSION, _sha256_hex(f"batch-{eid}")),
                )
                cur.execute(
                    """
                    INSERT INTO public.fc27_matches (
                        club_id, platform, game_version, external_match_id,
                        export_id, played_at, result, goals_for, goals_against,
                        opponent_name, formation, match_status, match_hash,
                        source_classification, data_quality_status
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s, %s, 'LOSS', 0, 4,
                        'Synthetic Rival', '4-3-3(2)', 'COMPLETED', %s,
                        'SYNTHETIC', 'VERIFIED'
                    )
                    """,
                    (
                        cid,
                        plat,
                        EAST_COASTERS_GAME_VERSION,
                        mid,
                        eid,
                        REF_NOW - timedelta(days=1),
                        _sha256_hex(f"iso-{cid}-{plat}-{mid}"),
                    ),
                )

            non_eligible_rows = [
                ("m_abandoned_01", "ABANDONED", "VERIFIED", REF_NOW - timedelta(days=3)),
                ("m_partial_01", "PARTIAL", "VERIFIED", REF_NOW - timedelta(days=3)),
                ("m_rejected_qual_01", "COMPLETED", "REJECTED", REF_NOW - timedelta(days=3)),
            ]
            for mid, m_status, dq_status, p_at in non_eligible_rows:
                cur.execute(
                    """
                    INSERT INTO public.fc27_matches (
                        club_id, platform, game_version, external_match_id,
                        export_id, played_at, result, goals_for, goals_against,
                        opponent_name, formation, match_status, match_hash,
                        source_classification, data_quality_status
                    ) VALUES (
                        %s, %s, %s, %s,
                        'exp-main-330323', %s, 'LOSS', 0, 3,
                        'Synthetic Rival', '4-3-3(2)', %s, %s,
                        'SYNTHETIC', %s
                    )
                    """,
                    (
                        EAST_COASTERS_CLUB_ID,
                        EAST_COASTERS_PLATFORM,
                        EAST_COASTERS_GAME_VERSION,
                        mid,
                        p_at,
                        m_status,
                        _sha256_hex(f"non-eligible-{mid}"),
                        dq_status,
                    ),
                )

    yield ctx

    with psycopg.connect(pg_admin_uri, autocommit=True) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
            (dbname,),
        )
        conn.execute(f'DROP DATABASE IF EXISTS "{dbname}"')


# ==============================================================================
# E7 Required Tests (1 to 38)
# ==============================================================================


def test_01_analytics_disabled_by_default(monkeypatch: pytest.MonkeyPatch, pg_analytics_db: AnalyticsDbContext) -> None:
    """1. Analytics disabled by default."""
    monkeypatch.delenv("FC27_ANALYTICS_ENABLED", raising=False)
    assert settings.is_fc27_analytics_enabled() is False
    with pytest.raises(FC27AnalyticsDisabledError):
        fetch_club_matches_window(pg_analytics_db.reader)


def test_02_missing_flag_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """2. Missing flag false."""
    monkeypatch.delenv("FC27_ANALYTICS_ENABLED", raising=False)
    assert settings.is_fc27_analytics_enabled() is False


@pytest.mark.parametrize("bad_value", ["", "invalid", "2", "maybe", "TRUE_ISH", "null"])
def test_03_malformed_flag_false(monkeypatch: pytest.MonkeyPatch, bad_value: str) -> None:
    """3. Malformed flag false."""
    monkeypatch.setenv("FC27_ANALYTICS_ENABLED", bad_value)
    assert settings.is_fc27_analytics_enabled() is False


def test_04_read_only_transaction_enforced(monkeypatch: pytest.MonkeyPatch, pg_analytics_db: AnalyticsDbContext) -> None:
    """4. Read-only transaction enforced."""
    monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "true")
    with psycopg.connect(pg_analytics_db.admin, autocommit=True) as conn:
        with pytest.raises(FC27AnalyticsError):
            with _read_only_cursor(conn) as cur:
                cur.execute("SHOW transaction_read_only")
                assert cur.fetchone()[0] == "on"
                cur.execute("DELETE FROM public.fc27_matches")


def test_05_statement_timeout_enforced(monkeypatch: pytest.MonkeyPatch, pg_analytics_db: AnalyticsDbContext) -> None:
    """5. Statement timeout enforced."""
    monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "true")
    with psycopg.connect(pg_analytics_db.reader, autocommit=True) as conn:
        with _read_only_cursor(conn, timeout_ms=2500) as cur:
            cur.execute("SHOW statement_timeout")
            val = str(cur.fetchone()[0])
            assert val in ("2500ms", "2.5s")


def test_06_parameterized_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    """6. Parameterized SQL only and strict public.fc27_* table scope."""
    assert len(ALL_ANALYTICS_SQL_STATEMENTS) >= 10
    for sql in ALL_ANALYTICS_SQL_STATEMENTS:
        normalized = sql.strip().upper()
        assert normalized.startswith("SELECT"), f"Non-SELECT SQL found: {sql}"
        for forbidden in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER ", "TRUNCATE ", "CREATE "):
            assert forbidden not in normalized
        assert "%s" in sql
        assert "{" not in sql and "}" not in sql
        for non_fc27 in (
            "public.checkpoints",
            "public.checkpoint_blobs",
            "public.checkpoint_writes",
            "public.outbox",
            "public.domain_events",
        ):
            assert non_fc27 not in sql.lower()


def test_07_club_isolation(monkeypatch: pytest.MonkeyPatch, pg_analytics_db: AnalyticsDbContext) -> None:
    """7. Club isolation."""
    monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "true")
    spec = AnalyticsWindowSpec(club_id=EAST_COASTERS_CLUB_ID, reference_now=REF_NOW, window_days=365)
    res = fetch_club_matches_window(pg_analytics_db.reader, spec)
    assert len(res.matches) == 6
    assert all(m.club_id == EAST_COASTERS_CLUB_ID for m in res.matches)
    assert all(m.match_id != "m_other_club_01" for m in res.matches)


def test_08_platform_isolation(monkeypatch: pytest.MonkeyPatch, pg_analytics_db: AnalyticsDbContext) -> None:
    """8. Platform isolation."""
    monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "true")
    spec = AnalyticsWindowSpec(club_id=EAST_COASTERS_CLUB_ID, platform="common-gen5", reference_now=REF_NOW, window_days=365)
    res = fetch_club_matches_window(pg_analytics_db.reader, spec)
    assert all(m.platform == "common-gen5" for m in res.matches)
    assert all(m.match_id != "m_other_plat_01" for m in res.matches)


def test_09_game_version_isolation(monkeypatch: pytest.MonkeyPatch, pg_analytics_db: AnalyticsDbContext) -> None:
    """9. Game-version isolation."""
    monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "true")
    spec = AnalyticsWindowSpec(club_id=EAST_COASTERS_CLUB_ID, game_version="FC27", reference_now=REF_NOW, window_days=365)
    res = fetch_club_matches_window(pg_analytics_db.reader, spec)
    assert all(m.game_version == "FC27" for m in res.matches)
    with pytest.raises(ValueError):
        AnalyticsWindowSpec(game_version="FC26")  # type: ignore[arg-type]


def test_10_explicit_start_end_window(monkeypatch: pytest.MonkeyPatch, pg_analytics_db: AnalyticsDbContext) -> None:
    """10. Explicit start/end window."""
    monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "true")
    spec = AnalyticsWindowSpec(
        start_at=REF_NOW - timedelta(days=15),
        end_at=REF_NOW,
        reference_now=REF_NOW,
    )
    res = fetch_club_matches_window(pg_analytics_db.reader, spec)
    assert res.window.window_mode == "EXPLICIT_RANGE"
    assert [m.match_id for m in res.matches] == ["m_syn_001", "m_syn_002", "m_syn_003"]


def test_11_rolling_day_window(monkeypatch: pytest.MonkeyPatch, pg_analytics_db: AnalyticsDbContext) -> None:
    """11. Rolling day window (default 84 days excludes 120-day-old match)."""
    monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "true")
    spec = AnalyticsWindowSpec(reference_now=REF_NOW)
    res = fetch_club_matches_window(pg_analytics_db.reader, spec)
    assert res.window.window_mode == "ROLLING_DAYS"
    assert res.window.window_days == FC27_ANALYTICS_WINDOW_DAYS == 84
    assert "two seasons" not in res.window.window_description.lower()
    assert [m.match_id for m in res.matches] == ["m_syn_001", "m_syn_002", "m_syn_003", "m_syn_004", "m_syn_005"]


def test_12_last_n_match_window(monkeypatch: pytest.MonkeyPatch, pg_analytics_db: AnalyticsDbContext) -> None:
    """12. Last-N match window."""
    monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "true")
    spec = AnalyticsWindowSpec(last_n_matches=3, reference_now=REF_NOW)
    res = fetch_club_matches_window(pg_analytics_db.reader, spec)
    assert res.window.window_mode == "LAST_N_MATCHES"
    assert [m.match_id for m in res.matches] == ["m_syn_001", "m_syn_002", "m_syn_003"]


def test_13_recent_n_match_window(monkeypatch: pytest.MonkeyPatch, pg_analytics_db: AnalyticsDbContext) -> None:
    """13. Recent-N match window."""
    monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "true")
    spec = AnalyticsWindowSpec(recent_n_matches=4, reference_now=REF_NOW)
    res = fetch_club_matches_window(pg_analytics_db.reader, spec)
    assert res.window.window_mode == "RECENT_N_MATCHES"
    assert [m.match_id for m in res.matches] == ["m_syn_001", "m_syn_002", "m_syn_003", "m_syn_004"]


def test_14_completed_matches_only(monkeypatch: pytest.MonkeyPatch, pg_analytics_db: AnalyticsDbContext) -> None:
    """14. Completed matches only."""
    monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "true")
    res = fetch_club_matches_window(pg_analytics_db.reader, AnalyticsWindowSpec(reference_now=REF_NOW, window_days=365))
    assert all(m.completion_state == "COMPLETED" for m in res.matches)


def test_15_abandoned_records_excluded(monkeypatch: pytest.MonkeyPatch, pg_analytics_db: AnalyticsDbContext) -> None:
    """15. Abandoned records excluded."""
    monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "true")
    res = fetch_club_matches_window(pg_analytics_db.reader, AnalyticsWindowSpec(reference_now=REF_NOW, window_days=365))
    assert all(m.match_id != "m_abandoned_01" for m in res.matches)
    assert res.window.exclusion_reasons["ABANDONED"] == 1


def test_16_partial_records_excluded(monkeypatch: pytest.MonkeyPatch, pg_analytics_db: AnalyticsDbContext) -> None:
    """16. Partial records excluded."""
    monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "true")
    res = fetch_club_matches_window(pg_analytics_db.reader, AnalyticsWindowSpec(reference_now=REF_NOW, window_days=365))
    assert all(m.match_id != "m_partial_01" for m in res.matches)
    assert res.window.exclusion_reasons["PARTIAL"] == 1


def test_17_quarantined_records_excluded(monkeypatch: pytest.MonkeyPatch, pg_analytics_db: AnalyticsDbContext) -> None:
    """17. Quarantined records excluded."""
    monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "true")
    res = fetch_club_matches_window(pg_analytics_db.reader, AnalyticsWindowSpec(reference_now=REF_NOW, window_days=365))
    assert all(m.match_id not in ("m_anomaly_01", "m_drift_01", "m_oor_01", "m_expired_01") for m in res.matches)
    assert res.window.exclusion_reasons["QUARANTINED"] == 4


def test_18_replay_conflicts_excluded(monkeypatch: pytest.MonkeyPatch, pg_analytics_db: AnalyticsDbContext) -> None:
    """18. Replay conflicts excluded."""
    monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "true")
    res = fetch_club_matches_window(pg_analytics_db.reader, AnalyticsWindowSpec(reference_now=REF_NOW, window_days=365))
    assert all(m.match_id != "m_conflict_01" for m in res.matches)
    assert res.window.exclusion_reasons["REPLAY_CONFLICT"] == 1


def test_19_duplicate_rows_do_not_inflate_totals(monkeypatch: pytest.MonkeyPatch, pg_analytics_db: AnalyticsDbContext) -> None:
    """19. Duplicate rows do not inflate totals."""
    monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "true")
    summary = fetch_club_storage_summary(pg_analytics_db.reader)
    dq = fetch_data_quality_summary(pg_analytics_db.reader)
    assert summary.completed_matches == 6
    assert dq.accepted_matches == 6
    assert dq.duplicate_matches == 2


def test_20_aliases_hidden_by_default(monkeypatch: pytest.MonkeyPatch, pg_analytics_db: AnalyticsDbContext) -> None:
    """20. Aliases hidden by default (and SQL_SELECT_PLAYER_STATS_NO_ALIAS does not select p.display_alias)."""
    monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "true")
    assert "p.display_alias" not in SQL_SELECT_PLAYER_STATS_NO_ALIAS
    res = fetch_player_performances_window(pg_analytics_db.reader, AnalyticsWindowSpec(reference_now=REF_NOW))
    assert len(res.performances) > 0
    assert all(p.display_alias is None for p in res.performances)
    assert all(s["display_alias"] is None for s in res.player_summaries.values())


def test_21_approved_alias_shown_only_when_requested(monkeypatch: pytest.MonkeyPatch, pg_analytics_db: AnalyticsDbContext) -> None:
    """21. Approved alias shown only when requested."""
    monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "true")
    spec = AnalyticsWindowSpec(include_aliases=True, reference_now=REF_NOW)
    res = fetch_player_performances_window(pg_analytics_db.reader, spec)
    st_rows = [p for p in res.performances if p.local_player_ref == "player_st_01"]
    assert len(st_rows) > 0
    assert all(p.display_alias == "Approved_Alias_01" for p in st_rows)
    assert res.player_summaries["player_st_01"]["display_alias"] == "Approved_Alias_01"


def test_22_unapproved_alias_remains_hidden(monkeypatch: pytest.MonkeyPatch, pg_analytics_db: AnalyticsDbContext) -> None:
    """22. Unapproved alias remains hidden even when include_aliases=True."""
    monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "true")
    spec = AnalyticsWindowSpec(include_aliases=True, reference_now=REF_NOW)
    res = fetch_player_performances_window(pg_analytics_db.reader, spec)
    cm_rows = [p for p in res.performances if p.local_player_ref == "player_cm_02"]
    assert len(cm_rows) > 0
    assert all(p.display_alias is None for p in cm_rows)
    assert res.player_summaries["player_cm_02"]["display_alias"] is None


def test_23_confidence_tier_returned(monkeypatch: pytest.MonkeyPatch, pg_analytics_db: AnalyticsDbContext) -> None:
    """23. Confidence tier returned."""
    monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "true")
    snap = build_club_analytics_snapshot(pg_analytics_db.reader, AnalyticsWindowSpec(reference_now=REF_NOW))
    assert snap.window.confidence_tier == "LOW_CONFIDENCE"  # 5 matches in 84-day rolling window
    assert snap.club_summary["sample_size_tier"] == "LOW_CONFIDENCE"


def test_24_formula_version_returned(monkeypatch: pytest.MonkeyPatch, pg_analytics_db: AnalyticsDbContext) -> None:
    """24. Formula version returned and equals fc27-v1.0."""
    monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "true")
    snap = build_club_analytics_snapshot(pg_analytics_db.reader, AnalyticsWindowSpec(reference_now=REF_NOW))
    assert snap.formula_version == "fc27-v1.0"
    assert snap.window.formula_version == "fc27-v1.0"
    assert snap.club_summary["formula_version"] == "fc27-v1.0"
    assert snap.data_quality_summary.formula_version == "fc27-v1.0"


def test_25_window_metadata_returned(monkeypatch: pytest.MonkeyPatch, pg_analytics_db: AnalyticsDbContext) -> None:
    """25. Window metadata returned with all required fields."""
    monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "true")
    res = fetch_club_matches_window(pg_analytics_db.reader, AnalyticsWindowSpec(reference_now=REF_NOW))
    w = res.window
    assert w.window_mode == "ROLLING_DAYS"
    assert w.eligible_record_count == 5
    assert w.excluded_record_count == 8  # 1 abandoned + 1 partial + 1 rejected + 4 non-replay quarantine + 1 replay conflict
    assert set(w.exclusion_reasons.keys()) == {
        "ABANDONED",
        "PARTIAL",
        "QUARANTINED",
        "REPLAY_CONFLICT",
        "REJECTED_QUALITY",
    }
    assert w.generated_at


def test_26_empty_dataset_result(monkeypatch: pytest.MonkeyPatch, pg_analytics_db: AnalyticsDbContext) -> None:
    """26. Empty dataset result."""
    monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "true")
    spec = AnalyticsWindowSpec(club_id="111222", reference_now=REF_NOW)
    snap = build_club_analytics_snapshot(pg_analytics_db.reader, spec)
    assert snap.window.eligible_record_count == 0
    assert snap.window.confidence_tier == "INSUFFICIENT_DATA"
    assert snap.window.minimum_sample_met is False
    assert snap.club_summary["matches_analyzed"] == 0
    assert snap.player_form_summary == {}
    assert snap.formation_summary == {}


def test_27_insufficient_sample_result(monkeypatch: pytest.MonkeyPatch, pg_analytics_db: AnalyticsDbContext) -> None:
    """27. Insufficient sample result."""
    monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "true")
    spec = AnalyticsWindowSpec(last_n_matches=2, minimum_sample_size=5, reference_now=REF_NOW)
    snap = build_club_analytics_snapshot(pg_analytics_db.reader, spec)
    assert snap.window.eligible_record_count == 2
    assert snap.window.confidence_tier == "INSUFFICIENT_DATA"
    assert snap.window.minimum_sample_met is False
    assert snap.club_summary["minimum_sample_met"] is False


def test_28_maximum_match_limit(monkeypatch: pytest.MonkeyPatch, pg_analytics_db: AnalyticsDbContext) -> None:
    """28. Maximum match limit enforced."""
    assert FC27_ANALYTICS_MAX_MATCHES == 100
    with pytest.raises(ValueError):
        AnalyticsWindowSpec(max_matches=101)
    with pytest.raises(ValueError):
        AnalyticsWindowSpec(last_n_matches=101)
    with pytest.raises(ValueError):
        AnalyticsWindowSpec(recent_n_matches=101)


def test_29_maximum_player_limit(monkeypatch: pytest.MonkeyPatch, pg_analytics_db: AnalyticsDbContext) -> None:
    """29. Maximum player limit enforced both in schema validation and query result filtering."""
    assert FC27_ANALYTICS_MAX_PLAYERS == 100
    with pytest.raises(ValueError):
        AnalyticsWindowSpec(max_players=101)

    monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "true")
    spec = AnalyticsWindowSpec(max_players=1, reference_now=REF_NOW)
    res = fetch_player_performances_window(pg_analytics_db.reader, spec)
    assert len(res.player_summaries) == 1


def test_30_query_timeout_behavior(monkeypatch: pytest.MonkeyPatch, pg_analytics_db: AnalyticsDbContext) -> None:
    """30. Query timeout behavior raises FC27AnalyticsTimeoutError on slow query."""
    monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "true")
    with psycopg.connect(pg_analytics_db.reader, autocommit=True) as conn:
        with pytest.raises(FC27AnalyticsTimeoutError):
            with _read_only_cursor(conn, timeout_ms=50) as cur:
                cur.execute("SELECT pg_sleep(0.5)")


def test_31_transaction_rollback_or_closure_after_exception(
    monkeypatch: pytest.MonkeyPatch, pg_analytics_db: AnalyticsDbContext
) -> None:
    """31. Transaction rollback or closure after exception leaves connection IDLE."""
    monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "true")
    with psycopg.connect(pg_analytics_db.reader, autocommit=True) as conn:
        with pytest.raises(FC27AnalyticsError):
            with _read_only_cursor(conn, timeout_ms=1000) as cur:
                cur.execute("SELECT 1 / 0")
        assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE


def test_32_no_writes_attempted(monkeypatch: pytest.MonkeyPatch, pg_analytics_db: AnalyticsDbContext) -> None:
    """32. No writes attempted during full analytics snapshot and data-quality queries."""
    monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "true")
    with psycopg.connect(pg_analytics_db.reader, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(SUM(n_tup_ins + n_tup_upd + n_tup_del), 0)
                FROM pg_stat_user_tables
                WHERE schemaname = 'public' AND relname LIKE 'fc27_%'
                """
            )
            before_writes = int(cur.fetchone()[0])

        snap = build_club_analytics_snapshot(conn, AnalyticsWindowSpec(reference_now=REF_NOW))
        storage = fetch_club_storage_summary(conn)
        dq = fetch_data_quality_summary(conn)
        assert snap.window.eligible_record_count == 5
        assert storage.club_exists is True
        assert dq.accepted_matches == 6
        assert dq.quarantined_matches == 4
        assert dq.replay_conflicts == 1
        assert dq.validation_anomalies == 2
        assert dq.schema_drift == 1
        assert dq.out_of_range_observations == 1
        assert dq.pending_reviews == 4
        assert dq.expired_quarantine_metadata == 1
        assert "diagnostic_paths" not in dq.model_dump()
        assert "review_notes" not in dq.model_dump()

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(SUM(n_tup_ins + n_tup_upd + n_tup_del), 0)
                FROM pg_stat_user_tables
                WHERE schemaname = 'public' AND relname LIKE 'fc27_%'
                """
            )
            after_writes = int(cur.fetchone()[0])
        assert after_writes == before_writes


def test_33_no_events_emitted(monkeypatch: pytest.MonkeyPatch, pg_analytics_db: AnalyticsDbContext) -> None:
    """33. No events emitted during analytics queries."""
    monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "true")
    with psycopg.connect(pg_analytics_db.admin, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.domain_events")
            before_count = int(cur.fetchone()[0])

        build_club_analytics_snapshot(pg_analytics_db.reader, AnalyticsWindowSpec(reference_now=REF_NOW))

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.domain_events")
            after_count = int(cur.fetchone()[0])
    assert after_count == before_count


def test_34_no_outbox_writes(monkeypatch: pytest.MonkeyPatch, pg_analytics_db: AnalyticsDbContext) -> None:
    """34. No outbox writes during analytics queries."""
    monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "true")
    with psycopg.connect(pg_analytics_db.admin, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.outbox")
            before_count = int(cur.fetchone()[0])

        build_club_analytics_snapshot(pg_analytics_db.reader, AnalyticsWindowSpec(reference_now=REF_NOW))

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.outbox")
            after_count = int(cur.fetchone()[0])
    assert after_count == before_count


def test_35_no_provider_call(monkeypatch: pytest.MonkeyPatch, pg_analytics_db: AnalyticsDbContext) -> None:
    """35. No provider call attempted during analytics queries."""
    monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "true")
    with patch.object(
        FC27TransportClient,
        "execute_request",
        side_effect=AssertionError("Provider call must never occur"),
    ):
        snap = build_club_analytics_snapshot(pg_analytics_db.reader, AnalyticsWindowSpec(reference_now=REF_NOW))
        assert snap.window.eligible_record_count == 5


def test_36_no_recommendation_generation(monkeypatch: pytest.MonkeyPatch, pg_analytics_db: AnalyticsDbContext) -> None:
    """36. No recommendation generation attempted during analytics queries."""
    monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "true")
    assert settings.is_fc27_recommendations_enabled() is False
    snap = build_club_analytics_snapshot(pg_analytics_db.reader, AnalyticsWindowSpec(reference_now=REF_NOW))
    dump = snap.model_dump()
    assert "recommendations" not in dump


def test_37_fc27_persistence_flag_remains_unchanged(monkeypatch: pytest.MonkeyPatch, pg_analytics_db: AnalyticsDbContext) -> None:
    """37. Enabling FC27_ANALYTICS_ENABLED does not enable FC27_PERSISTENCE_ENABLED or other flags."""
    monkeypatch.delenv("FC27_PERSISTENCE_ENABLED", raising=False)
    monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "true")
    assert settings.is_fc27_analytics_enabled() is True
    assert settings.is_fc27_persistence_enabled() is False
    assert settings.is_fc27_provider_enabled() is False
    assert settings.is_fc27_live_ingestion_enabled() is False
    assert settings.is_fc27_recommendations_enabled() is False
    assert settings.is_dispatch_enabled() is False


def test_38_matchpulse_boundary_intact() -> None:
    """38. MatchPulse boundary script passes with zero violations."""
    proc = subprocess.run(
        [sys.executable, "scripts/check_matchpulse_boundary.py"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
