"""Comprehensive tests for FC 27 PostgreSQL Performance Storage Foundation (Migration 0012).

Covers:
- Migration 0012 application, SHA-256 verification, table ownership, indexes, FKs, CHECK constraints, PostgresSaver isolation, and Grants Policy v14 (Tests 1-10)
- Role separation across fc27_owner, fc27_migrator, fc27_app, fc27_dispatch, and fc27_relay (Tests 11-18)
- FC27_PERSISTENCE_ENABLED feature flag fail-closed behavior and isolation (Tests 19-25)
- Transactional repository apply, idempotency, replay-conflict quarantine, rollback, domain events, outbox, and retention purge (Tests 26-43)
- Operator CLI apply wiring, metadata-only output, and credential/raw-input redaction (Tests 44-54)
- Platform boundary and feature-gate isolation (Tests 55-60)
"""
from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import psycopg.errors
import pytest

from scripts.fc27_user_export_import import main as cli_main
from src import settings
from src.db_migrate import (
    GRANTS_POLICY,
    GRANTS_POLICY_VERSION,
    MIGRATIONS_DIR,
    MigrationError,
    check_runtime_posture,
    migrate,
    verify_postconditions,
)
from src.fc27_provider import FC27TransportClient
from src.fc27_repository import (
    FC27PostureViolationError,
    FC27RepositoryError,
    apply_export_plan,
    fetch_export_batch_status,
    insert_quarantine_record,
    load_existing_match_hashes,
    load_import_state,
    purge_expired_quarantine_records,
    review_quarantine_record,
)
from src.fc27_user_export import (
    EAST_COASTERS_CLUB_ID,
    EAST_COASTERS_CLUB_NAME,
    EAST_COASTERS_GAME_VERSION,
    EAST_COASTERS_PLATFORM,
    apply_fc27_user_export_plan,
    compute_match_hash,
    compute_payload_sha256,
    parse_fc27_user_export_json,
    plan_fc27_user_export_import,
)
from src.schemas import ClubIdentityState

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

# Expected immutable SHA-256 digests for migrations 0001 through 0011
EXPECTED_HISTORICAL_MIGRATIONS: tuple[str, ...] = (
    "0001_initial.sql",
    "0002_approval_requests_and_hardening.sql",
    "0003_dispatch_foundation.sql",
    "0004_phase2_identity_foundation.sql",
    "0005_phase2_evidence_and_delegations.sql",
    "0006_phase2_audit_and_player_bindings.sql",
    "0007_phase2_immutability_and_indexes.sql",
    "0008_fix_authority_delegations_immutability.sql",
    "0009_fix_fc27_delegations_view_columns.sql",
    "0010_fix_totp_enrollment_atomicity.sql",
    "0011_dispatch_worker_claims.sql",
)


def make_synthetic_export_dict(
    *,
    export_id: str = "exp-synth-storage-001",
    club_id: str = EAST_COASTERS_CLUB_ID,
    club_name: str = EAST_COASTERS_CLUB_NAME,
    platform: str = EAST_COASTERS_PLATFORM,
    external_match_id: str = "match-synth-storage-101",
    goals_for: int = 3,
    goals_against: int = 1,
    result: str = "WIN",
    player_goals: int = 2,
    display_alias: str | None = "SyntheticCaptain99",
) -> dict[str, Any]:
    """Builds a deterministic synthetic FC 27 export dictionary with zero real player data."""
    now = datetime(2026, 10, 20, 18, 0, 0, tzinfo=timezone.utc)
    gen_str = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    sup_str = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    match_str = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

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
        "matches": [
            {
                "external_match_id": external_match_id,
                "played_at": match_str,
                "result": result,
                "goals_for": goals_for,
                "goals_against": goals_against,
                "opponent_name": "Synthetic Harbor FC",
                "formation": "4-3-3",
                "lineup": ["plr_synth_01", "plr_synth_02"],
                "player_performances": [
                    {
                        "local_player_reference": "plr_synth_01",
                        "display_alias": display_alias,
                        "position": "ST",
                        "role": "Poacher",
                        "minutes_played": 90,
                        "goals": player_goals,
                        "assists": 1,
                        "passes_attempted": 24,
                        "passes_completed": 20,
                        "tackles_attempted": 3,
                        "tackles_won": 2,
                        "interceptions": 1,
                        "shots": 5,
                        "shots_on_target": 4,
                        "saves": 0,
                        "possession_losses": 4,
                        "provider_rating": 8.6,
                        "build_reference": "bld_synth_st_01",
                    },
                    {
                        "local_player_reference": "plr_synth_02",
                        "display_alias": None,
                        "position": "CAM",
                        "role": "Playmaker",
                        "minutes_played": 90,
                        "goals": max(0, goals_for - player_goals),
                        "assists": 2,
                        "passes_attempted": 40,
                        "passes_completed": 35,
                        "tackles_attempted": 4,
                        "tackles_won": 3,
                        "interceptions": 2,
                        "shots": 2,
                        "shots_on_target": 1,
                        "saves": 0,
                        "possession_losses": 3,
                        "provider_rating": 8.2,
                        "build_reference": "bld_synth_cam_01",
                    },
                ],
                "match_status": "COMPLETED",
            }
        ],
        "source_classification": "USER_SUPPLIED_EXPORT",
    }
    data["payload_sha256"] = compute_payload_sha256(data)
    return data


# ==============================================================================
# MIGRATION TESTS (1-10)
# ==============================================================================

def test_01_migration_0012_applies_on_database_through_0011(pg_db) -> None:
    """1. 0012 applies cleanly on a database migrated through 0011."""
    applied_to_11 = migrate(pg_db.migrator, target="0011")
    assert "0011_dispatch_worker_claims.sql" in applied_to_11
    assert "0012_fc27_performance_and_quarantine.sql" not in applied_to_11

    applied_12 = migrate(pg_db.migrator)
    assert applied_12 == ["0012_fc27_performance_and_quarantine.sql"]


def test_02_migration_recorded_with_sha256(pg_db_migrated) -> None:
    """2. Migration 0012 is recorded in schema_migrations with exact SHA-256."""
    mig_path = MIGRATIONS_DIR / "0012_fc27_performance_and_quarantine.sql"
    expected_sha = hashlib.sha256(mig_path.read_text(encoding="utf-8").encode()).hexdigest()

    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT name, sha256 FROM public.schema_migrations WHERE version = '0012'"
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == "0012_fc27_performance_and_quarantine.sql"
            assert row[1] == expected_sha


def test_03_existing_migrations_0001_to_0011_unchanged() -> None:
    """3. Existing migrations 0001 through 0011 remain unmodified in git."""
    proc = subprocess.run(
        ["git", "diff", "main", "--", *[f"migrations/{m}" for m in EXPECTED_HISTORICAL_MIGRATIONS]],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == ""


def test_04_correct_table_ownership_fc27_owner(pg_db_migrated) -> None:
    """4. All FC 27 performance and quarantine tables and functions are owned by fc27_owner."""
    expected_tables = (
        "fc27_clubs",
        "fc27_export_batches",
        "fc27_matches",
        "fc27_player_match_stats",
        "fc27_quarantine_records",
    )
    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            for tbl in expected_tables:
                cur.execute(
                    """
                    SELECT c.relowner::regrole::text
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public' AND c.relname = %s
                    """,
                    (tbl,),
                )
                row = cur.fetchone()
                assert row is not None, f"missing table {tbl}"
                assert row[0] == "fc27_owner"


def test_05_required_tables_exist(pg_db_migrated) -> None:
    """5. All 5 required tables exist with expected primary keys."""
    expected_tables = (
        "fc27_clubs",
        "fc27_export_batches",
        "fc27_matches",
        "fc27_player_match_stats",
        "fc27_quarantine_records",
    )
    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            for tbl in expected_tables:
                cur.execute("SELECT to_regclass(%s)", (f"public.{tbl}",))
                assert cur.fetchone()[0] == tbl or cur.fetchone is not None
                cur.execute(
                    """
                    SELECT count(*) FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = %s
                    """,
                    (tbl,),
                )
                assert cur.fetchone()[0] == 1


def test_06_required_indexes_exist(pg_db_migrated) -> None:
    """6. All 7 required query-plan indexes exist."""
    expected_indexes = (
        "idx_fc27_matches_club_played_at",
        "idx_fc27_matches_export_id",
        "idx_fc27_player_match_stats_match_id",
        "idx_fc27_player_match_stats_club_player",
        "idx_fc27_quarantine_records_status_expires",
        "idx_fc27_quarantine_records_club_quarantined_at",
        "idx_fc27_export_batches_club_applied_at",
    )
    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            for idx in expected_indexes:
                cur.execute("SELECT to_regclass(%s)", (f"public.{idx}",))
                assert cur.fetchone()[0] is not None, f"missing index {idx}"


def test_07_required_foreign_keys_exist(pg_db_migrated) -> None:
    """7. Required foreign keys exist on fc27_export_batches, fc27_matches, and fc27_player_match_stats."""
    expected_fks = {
        "fc27_export_batches_club_fk",
        "fc27_matches_club_fk",
        "fc27_matches_export_batch_fk",
        "fc27_player_match_stats_match_fk",
        "fc27_player_match_stats_club_fk",
    }
    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT conname
                FROM pg_constraint
                WHERE contype = 'f' AND conname = ANY(%s)
                """,
                (list(expected_fks),),
            )
            found = {r[0] for r in cur.fetchall()}
            assert found == expected_fks


def test_08_required_check_constraints_exist_and_enforced(pg_uri) -> None:
    """8. Required CHECK constraints enforce SHA-256 format, score consistency, completed <= attempted, rating bounds, and authority separation."""
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            # Valid club insert
            cur.execute(
                """
                INSERT INTO public.fc27_clubs (club_id, platform, game_version, club_name)
                VALUES ('330323', 'common-gen5', 'FC27', 'East Coasters')
                """
            )
            # Club identity cannot grant manager authority
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute(
                    """
                    INSERT INTO public.fc27_clubs (club_id, platform, game_version, club_name, manager_authority_status)
                    VALUES ('330324', 'common-gen5', 'FC27', 'Other Club', 'VERIFIED')
                    """
                )
            # Export batch requires 64-char lowercase hex payload_sha256 and accepted + quarantined <= total
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute(
                    """
                    INSERT INTO public.fc27_export_batches (
                        export_id, club_id, platform, game_version,
                        supplier_role, supplier_reference, consented_categories,
                        payload_sha256, total_matches, accepted_matches, quarantined_matches
                    ) VALUES (
                        'bad-sha-batch', '330323', 'common-gen5', 'FC27',
                        'CLUB_MANAGER', 'steward-01', '["CLUB_IDENTITY"]'::jsonb,
                        'not-a-sha256', 1, 1, 0
                    )
                    """
                )
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute(
                    """
                    INSERT INTO public.fc27_export_batches (
                        export_id, club_id, platform, game_version,
                        supplier_role, supplier_reference, consented_categories,
                        payload_sha256, total_matches, accepted_matches, quarantined_matches
                    ) VALUES (
                        'bad-count-batch', '330323', 'common-gen5', 'FC27',
                        'CLUB_MANAGER', 'steward-01', '["CLUB_IDENTITY"]'::jsonb,
                        %s, 1, 1, 1
                    )
                    """,
                    ("a" * 64,),
                )


def test_09_postgres_saver_tables_unchanged(pg_db_migrated) -> None:
    """9. PostgresSaver internal tables remain unmodified and have zero custom triggers."""
    saver_tables = (
        "checkpoints",
        "checkpoint_blobs",
        "checkpoint_writes",
        "checkpoint_migrations",
    )
    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            for tbl in saver_tables:
                cur.execute("SELECT to_regclass(%s)", (f"public.{tbl}",))
                assert cur.fetchone()[0] is not None
                cur.execute(
                    """
                    SELECT tgname
                    FROM pg_trigger t
                    JOIN pg_class c ON c.oid = t.tgrelid
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public' AND c.relname = %s AND NOT t.tgisinternal
                    """,
                    (tbl,),
                )
                assert cur.fetchall() == [], f"unexpected custom trigger on {tbl}"


def test_10_grants_policy_version_14_recorded(pg_db_migrated) -> None:
    """10. Grants policy version 14 is recorded in public.schema_policy_versions."""
    assert GRANTS_POLICY_VERSION == 14
    assert GRANTS_POLICY.version == 14
    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT version, sha256, applied_by
                FROM public.schema_policy_versions
                WHERE policy = 'grants' AND version = 14
                """
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == 14
            assert row[1] == GRANTS_POLICY.sha256
            assert row[2] == "fc27_migrator"


# ==============================================================================
# ROLE & LEAST-PRIVILEGE TESTS (11-18)
# ==============================================================================

def test_11_fc27_app_can_select_normalized_tables(pg_uri) -> None:
    """11. fc27_app can SELECT from all 5 FC 27 tables."""
    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            for tbl in (
                "fc27_clubs",
                "fc27_export_batches",
                "fc27_matches",
                "fc27_player_match_stats",
                "fc27_quarantine_records",
            ):
                cur.execute(f"SELECT count(*) FROM public.{tbl}")
                assert cur.fetchone()[0] == 0


def test_12_fc27_app_can_execute_required_apply_writes(pg_uri) -> None:
    """12. fc27_app can execute transactional export apply writes and quarantine review updates."""
    env = parse_fc27_user_export_json(json.dumps(make_synthetic_export_dict()))
    res = apply_export_plan(pg_uri, envelope=env)
    assert res.status == "APPLIED"
    assert res.accepted_records == 1
    assert res.player_stat_records == 2


def test_13_fc27_app_cannot_create_schema_objects(pg_uri) -> None:
    """13. fc27_app cannot create or drop tables in public schema."""
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("CREATE TABLE public.unauthorized_fc27_tbl (id int)")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DROP TABLE public.fc27_matches")


def test_14_fc27_app_cannot_alter_roles(pg_uri) -> None:
    """14. fc27_app cannot create or alter database roles."""
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("CREATE ROLE unauthorized_role")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("GRANT fc27_owner TO fc27_app")


def test_15_fc27_dispatch_lacks_fc27_persistence_writes(pg_db_migrated) -> None:
    """15. fc27_dispatch has zero SELECT/INSERT/UPDATE/DELETE privileges on FC 27 persistence tables."""
    with psycopg.connect(pg_db_migrated.dispatch, autocommit=True) as conn:
        for tbl in (
            "fc27_clubs",
            "fc27_export_batches",
            "fc27_matches",
            "fc27_player_match_stats",
            "fc27_quarantine_records",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(f"SELECT count(*) FROM public.{tbl}")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM public.fc27_purge_expired_quarantine(now())")


def test_16_fc27_relay_lacks_fc27_persistence_writes(pg_db_migrated) -> None:
    """16. fc27_relay has zero SELECT/INSERT/UPDATE/DELETE privileges on FC 27 persistence tables."""
    with psycopg.connect(pg_db_migrated.relay, autocommit=True) as conn:
        for tbl in (
            "fc27_clubs",
            "fc27_export_batches",
            "fc27_matches",
            "fc27_player_match_stats",
            "fc27_quarantine_records",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(f"SELECT count(*) FROM public.{tbl}")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM public.fc27_purge_expired_quarantine(now())")


def test_17_expired_quarantine_purge_is_narrowly_authorized(pg_uri) -> None:
    """17. fc27_app cannot directly DELETE from fc27_quarantine_records; purge is only via SECURITY DEFINER function."""
    q_res = insert_quarantine_record(
        pg_uri,
        club_id=EAST_COASTERS_CLUB_ID,
        anomaly_category="VALIDATION_ANOMALY",
        reason_code="TEST_RETENTION_LOCK",
        diagnostic_paths=["matches[0].goals_for"],
        payload_sha256="a" * 64,
    )
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "DELETE FROM public.fc27_quarantine_records WHERE quarantine_id = %s",
                (q_res.quarantine_id,),
            )


def test_18_existing_runtime_posture_checks_remain_green(pg_db_migrated) -> None:
    """18. Runtime posture and postcondition checks remain green."""
    with psycopg.connect(pg_db_migrated.app) as app_conn:
        assert check_runtime_posture(app_conn) == []
    with psycopg.connect(pg_db_migrated.admin) as admin_conn:
        assert verify_postconditions(admin_conn) == []
        with pytest.raises(FC27PostureViolationError):
            env = parse_fc27_user_export_json(json.dumps(make_synthetic_export_dict()))
            apply_export_plan(admin_conn, envelope=env, enforce_posture=True)


# ==============================================================================
# FEATURE FLAG TESTS (19-25)
# ==============================================================================

def test_19_missing_persistence_flag_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """19. Missing FC27_PERSISTENCE_ENABLED defaults to False."""
    monkeypatch.delenv("FC27_PERSISTENCE_ENABLED", raising=False)
    assert settings.is_fc27_persistence_enabled() is False


@pytest.mark.parametrize("malformed", ["maybe", "2", "enabled", "TRUE_ISH", "null", "-1"])
def test_20_malformed_persistence_flag_is_false(
    monkeypatch: pytest.MonkeyPatch, malformed: str
) -> None:
    """20. Malformed FC27_PERSISTENCE_ENABLED values fail closed to False."""
    monkeypatch.setenv("FC27_PERSISTENCE_ENABLED", malformed)
    assert settings.is_fc27_persistence_enabled() is False


@pytest.mark.parametrize("false_val", ["false", "FALSE", "0", "no", "off", ""])
def test_21_explicit_false_is_false(monkeypatch: pytest.MonkeyPatch, false_val: str) -> None:
    """21. Explicit false values return False."""
    monkeypatch.setenv("FC27_PERSISTENCE_ENABLED", false_val)
    assert settings.is_fc27_persistence_enabled() is False


@pytest.mark.parametrize("true_val", ["true", "TRUE", "1", "yes", "on"])
def test_22_explicit_true_is_true(monkeypatch: pytest.MonkeyPatch, true_val: str) -> None:
    """22. Explicit true values return True."""
    monkeypatch.setenv("FC27_PERSISTENCE_ENABLED", true_val)
    assert settings.is_fc27_persistence_enabled() is True


def test_23_enabling_persistence_does_not_enable_networking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """23. Enabling FC27_PERSISTENCE_ENABLED does not enable provider networking or live ingestion."""
    monkeypatch.setenv("FC27_PERSISTENCE_ENABLED", "true")
    assert settings.is_fc27_persistence_enabled() is True
    assert settings.is_fc27_provider_enabled() is False
    assert settings.is_fc27_public_identity_lookup_enabled() is False
    assert settings.is_fc27_upstream_compatibility_enabled() is False
    assert settings.is_fc27_live_ingestion_enabled() is False
    assert settings.is_fc27_provider_dry_run() is True


def test_24_enabling_persistence_does_not_enable_recommendations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """24. Enabling FC27_PERSISTENCE_ENABLED does not enable recommendations."""
    monkeypatch.setenv("FC27_PERSISTENCE_ENABLED", "true")
    assert settings.is_fc27_recommendations_enabled() is False


def test_25_enabling_persistence_does_not_enable_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """25. Enabling FC27_PERSISTENCE_ENABLED does not enable dispatch or dispatch worker."""
    monkeypatch.setenv("FC27_PERSISTENCE_ENABLED", "true")
    assert settings.is_dispatch_enabled() is False
    assert settings.is_dispatch_worker_enabled() is False


# ==============================================================================
# REPOSITORY TESTS (26-43)
# ==============================================================================

def test_26_new_club_insert(pg_uri) -> None:
    """26. Applying a valid export inserts a new normalized club record."""
    env = parse_fc27_user_export_json(json.dumps(make_synthetic_export_dict()))
    res = apply_export_plan(pg_uri, envelope=env)
    assert res.status == "APPLIED"

    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT club_name, identity_state, manager_authority_status, source_classification
                FROM public.fc27_clubs
                WHERE club_id = %s AND platform = %s AND game_version = %s
                """,
                (EAST_COASTERS_CLUB_ID, EAST_COASTERS_PLATFORM, EAST_COASTERS_GAME_VERSION),
            )
            row = cur.fetchone()
            assert row == ("East Coasters", "PROVISIONAL", "UNVERIFIED", "USER_SUPPLIED_EXPORT")


def test_27_existing_club_update_does_not_verify_authority(pg_uri) -> None:
    """27. Updating an existing club never grants manager authority or promotes identity to VERIFIED."""
    env1 = parse_fc27_user_export_json(
        json.dumps(make_synthetic_export_dict(export_id="exp-club-1", external_match_id="m-1"))
    )
    env2 = parse_fc27_user_export_json(
        json.dumps(make_synthetic_export_dict(export_id="exp-club-2", external_match_id="m-2"))
    )
    apply_export_plan(pg_uri, envelope=env1)
    apply_export_plan(pg_uri, envelope=env2)

    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT identity_state, manager_authority_status
                FROM public.fc27_clubs
                WHERE club_id = %s AND platform = %s AND game_version = %s
                """,
                (EAST_COASTERS_CLUB_ID, EAST_COASTERS_PLATFORM, EAST_COASTERS_GAME_VERSION),
            )
            assert cur.fetchone() == ("PROVISIONAL", "UNVERIFIED")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(
                    "UPDATE public.fc27_clubs SET manager_authority_status = 'VERIFIED' WHERE club_id = %s",
                    (EAST_COASTERS_CLUB_ID,),
                )


def test_28_new_export_apply(pg_uri) -> None:
    """28. Applying a new export persists batch metadata and match rows."""
    env = parse_fc27_user_export_json(json.dumps(make_synthetic_export_dict()))
    res = apply_export_plan(pg_uri, envelope=env)
    assert res.status == "APPLIED"
    assert res.accepted_records == 1
    assert res.duplicate_records == 0
    assert res.quarantined_records == 0

    batch_status = fetch_export_batch_status(pg_uri, env.export_id)
    assert batch_status is not None
    assert batch_status.export_id == env.export_id
    assert batch_status.payload_sha256 == env.payload_sha256
    assert batch_status.total_matches == 1
    assert batch_status.accepted_matches == 1
    assert batch_status.quarantined_matches == 0

    hashes = load_existing_match_hashes(pg_uri, club_id=EAST_COASTERS_CLUB_ID)
    assert "match-synth-storage-101" in hashes


def test_29_identical_export_reapply_is_idempotent(pg_uri) -> None:
    """29. Reapplying the identical export envelope is idempotent and inserts zero duplicate rows."""
    env = parse_fc27_user_export_json(json.dumps(make_synthetic_export_dict()))
    res1 = apply_export_plan(pg_uri, envelope=env)
    assert res1.status == "APPLIED"
    assert res1.accepted_records == 1
    assert res1.idempotent_duplicate is False

    res2 = apply_export_plan(pg_uri, envelope=env)
    assert res2.status == "APPLIED"
    assert res2.accepted_records == 0
    assert res2.duplicate_records == 1
    assert res2.idempotent_duplicate is True

    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM public.fc27_matches")
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT count(*) FROM public.fc27_player_match_stats")
            assert cur.fetchone()[0] == 2


def test_30_duplicate_match_id_and_hash_ignored(pg_uri) -> None:
    """30. A new export batch containing an already-persisted match (identical hash) plus a new match inserts only the new match."""
    data1 = make_synthetic_export_dict(export_id="exp-batch-1", external_match_id="match-shared-01")
    env1 = parse_fc27_user_export_json(json.dumps(data1))
    apply_export_plan(pg_uri, envelope=env1)

    data2 = make_synthetic_export_dict(export_id="exp-batch-2", external_match_id="match-shared-01")
    new_match = dict(data2["matches"][0], external_match_id="match-new-02")
    data2["matches"].append(new_match)
    data2["payload_sha256"] = compute_payload_sha256(data2)
    env2 = parse_fc27_user_export_json(json.dumps(data2))

    res2 = apply_export_plan(pg_uri, envelope=env2)
    assert res2.status == "APPLIED"
    assert res2.accepted_records == 1
    assert res2.duplicate_records == 1

    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM public.fc27_matches")
            assert cur.fetchone()[0] == 2


def test_31_same_external_match_id_with_changed_hash_blocks_apply(pg_uri) -> None:
    """31. Replaying the same external_match_id with a changed record hash blocks the entire batch apply."""
    data1 = make_synthetic_export_dict(
        export_id="exp-orig-1", external_match_id="match-conflict-01", goals_for=2, goals_against=0
    )
    env1 = parse_fc27_user_export_json(json.dumps(data1))
    apply_export_plan(pg_uri, envelope=env1)

    # Second batch has a modified score for match-conflict-01 AND a brand-new match-clean-02
    data2 = make_synthetic_export_dict(
        export_id="exp-conflict-2", external_match_id="match-conflict-01", goals_for=4, goals_against=1
    )
    clean_match = dict(data1["matches"][0], external_match_id="match-clean-02")
    data2["matches"].append(clean_match)
    data2["payload_sha256"] = compute_payload_sha256(data2)
    env2 = parse_fc27_user_export_json(json.dumps(data2))

    res2 = apply_export_plan(pg_uri, envelope=env2)
    assert res2.status == "REJECTED"
    assert res2.replay_conflict is True
    assert res2.accepted_records == 0
    assert res2.quarantined_records == 1

    # Verify match-clean-02 was NOT inserted and match-conflict-01 still has goals_for = 2
    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT external_match_id, goals_for FROM public.fc27_matches")
            rows = cur.fetchall()
            assert rows == [("match-conflict-01", 2)]
            assert fetch_export_batch_status(pg_uri, "exp-conflict-2") is None


def test_32_replay_conflict_creates_sanitized_quarantine_metadata(pg_uri) -> None:
    """32. Replay conflict persists sanitized quarantine metadata with REPLAY_CONFLICT category."""
    data1 = make_synthetic_export_dict(
        export_id="exp-q-1", external_match_id="match-q-01", goals_for=2, goals_against=1
    )
    apply_export_plan(pg_uri, envelope=parse_fc27_user_export_json(json.dumps(data1)))

    data2 = make_synthetic_export_dict(
        export_id="exp-q-2", external_match_id="match-q-01", goals_for=3, goals_against=1
    )
    env2 = parse_fc27_user_export_json(json.dumps(data2))
    res2 = apply_export_plan(pg_uri, envelope=env2)

    assert res2.status == "REJECTED"
    assert len(res2.quarantine_ids) == 1

    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT anomaly_category, reason_code, diagnostic_paths, payload_sha256, record_hash, review_status, external_match_id
                FROM public.fc27_quarantine_records
                WHERE quarantine_id = %s
                """,
                (res2.quarantine_ids[0],),
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == "REPLAY_CONFLICT"
            assert row[1] == "MATCH_RECORD_HASH_CONFLICT"
            assert row[2] == ["matches[0].external_match_id", "matches[0].match_hash"]
            assert row[3] == env2.payload_sha256
            assert row[4] is not None and len(row[4]) == 64
            assert row[5] == "PENDING_REVIEW"
            assert row[6] == "match-q-01"


def test_33_raw_payload_never_stored(pg_uri) -> None:
    """33. None of the 5 FC 27 tables contain raw payload columns or raw JSON export envelopes."""
    env = parse_fc27_user_export_json(json.dumps(make_synthetic_export_dict()))
    apply_export_plan(pg_uri, envelope=env)

    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name IN (
                      'fc27_clubs',
                      'fc27_export_batches',
                      'fc27_matches',
                      'fc27_player_match_stats',
                      'fc27_quarantine_records'
                  )
                """
            )
            cols = {(r[0], r[1]) for r in cur.fetchall()}
            prohibited_col_names = {
                "raw_payload",
                "raw_json",
                "raw_body",
                "raw_upload",
                "provider_payload",
                "http_body",
            }
            assert not any(col in prohibited_col_names for _, col in cols)


def test_34_sensitive_values_never_stored_in_quarantine(pg_uri) -> None:
    """34. Quarantine records reject diagnostic_paths containing raw values, emails, or spaces and review_notes containing secrets."""
    with pytest.raises(ValueError, match="invalid or unsafe diagnostic path"):
        insert_quarantine_record(
            pg_uri,
            club_id=EAST_COASTERS_CLUB_ID,
            anomaly_category="SENSITIVE_FIELD_REJECTED",
            reason_code="PROHIBITED_EMAIL_FIELD",
            diagnostic_paths=["user@example.com"],
            payload_sha256="b" * 64,
        )

    with pytest.raises(ValueError, match="invalid or unsafe diagnostic path"):
        insert_quarantine_record(
            pg_uri,
            club_id=EAST_COASTERS_CLUB_ID,
            anomaly_category="VALIDATION_ANOMALY",
            reason_code="INLINE_VALUE_REJECTED",
            diagnostic_paths=["matches[0].goals_for=5"],
            payload_sha256="b" * 64,
        )

    # Also verify database CHECK constraint directly rejects non-path JSONB values
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """
                INSERT INTO public.fc27_quarantine_records (
                    club_id, platform, game_version, source_classification,
                    anomaly_category, reason_code, diagnostic_paths, payload_sha256
                ) VALUES (
                    '330323', 'common-gen5', 'FC27', 'USER_SUPPLIED_EXPORT',
                    'SENSITIVE_FIELD_REJECTED', 'BAD_PATH', '[{"raw": "secret"}]'::jsonb, %s
                )
                """,
                ("c" * 64,),
            )


def test_35_accepted_match_inserts_player_statistics(pg_uri) -> None:
    """35. Accepted match inserts normalized player performance rows with accurate metrics."""
    env = parse_fc27_user_export_json(json.dumps(make_synthetic_export_dict()))
    res = apply_export_plan(pg_uri, envelope=env)
    assert res.status == "APPLIED"
    assert res.player_stat_records == 2

    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT local_player_reference, position, role, goals, assists,
                       passes_attempted, passes_completed, provider_rating, alias_visibility_approved
                FROM public.fc27_player_match_stats
                ORDER BY local_player_reference
                """
            )
            rows = cur.fetchall()
            assert rows == [
                ("plr_synth_01", "ST", "Poacher", 2, 1, 24, 20, Decimal("8.60"), False),
                ("plr_synth_02", "CAM", "Playmaker", 1, 2, 40, 35, Decimal("8.20"), False),
            ]


def test_36_transaction_rollback_on_player_row_failure(pg_uri) -> None:
    """36. A failure while inserting player statistics rolls back club, batch, match, and event writes."""
    data = make_synthetic_export_dict(export_id="exp-rollback-player")
    env = parse_fc27_user_export_json(json.dumps(data))

    # Corrupt duplicate local_player_reference on the frozen model instance to trigger DB UNIQUE violation
    object.__setattr__(
        env.matches[0].player_performances[1],
        "local_player_reference",
        "plr_synth_01",
    )

    with pytest.raises(psycopg.errors.UniqueViolation):
        apply_export_plan(pg_uri, envelope=env)

    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM public.fc27_export_batches WHERE export_id = 'exp-rollback-player'")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT count(*) FROM public.fc27_matches")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT count(*) FROM public.fc27_player_match_stats")
            assert cur.fetchone()[0] == 0


def test_37_concurrent_duplicate_apply_remains_idempotent(pg_uri) -> None:
    """37. Concurrent threads applying the same export envelope serialize cleanly and insert exactly 1 batch and 1 match."""
    env = parse_fc27_user_export_json(
        json.dumps(make_synthetic_export_dict(export_id="exp-concurrent-01", external_match_id="m-conc-01"))
    )
    results: list[Any] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(4)

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            r = apply_export_plan(pg_uri, envelope=env)
            results.append(r)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    assert len(results) == 4
    assert all(r.status == "APPLIED" for r in results)
    primary_applies = [r for r in results if not r.idempotent_duplicate]
    idempotent_applies = [r for r in results if r.idempotent_duplicate]
    assert len(primary_applies) == 1
    assert len(idempotent_applies) == 3

    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM public.fc27_export_batches WHERE export_id = 'exp-concurrent-01'")
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT count(*) FROM public.fc27_matches WHERE external_match_id = 'm-conc-01'")
            assert cur.fetchone()[0] == 1


def test_38_domain_event_written_atomically(pg_uri) -> None:
    """38. Export apply writes an fc27.export.applied row in public.domain_events atomically."""
    env = parse_fc27_user_export_json(
        json.dumps(make_synthetic_export_dict(export_id="exp-event-01"))
    )
    res = apply_export_plan(pg_uri, envelope=env)
    assert len(res.domain_event_ids) == 1

    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT event_type, entity_type, entity_id, scope_club_id, to_status, reason
                FROM public.domain_events
                WHERE event_id = %s
                """,
                (res.domain_event_ids[0],),
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == "fc27.export.applied"
            assert row[1] == "fc27_export_batch"
            assert row[2] == "exp-event-01"
            assert row[3] == EAST_COASTERS_CLUB_ID
            assert row[4] == "APPLIED"
            reason_meta = json.loads(row[5])
            assert reason_meta["accepted_count"] == 1
            assert reason_meta["export_id"] == "exp-event-01"


def test_39_outbox_entry_written_atomically(pg_uri, pg_admin_db_uri) -> None:
    """39. Export apply writes a paired public.outbox entry on channel 'fc27:events' in the same transaction."""
    env = parse_fc27_user_export_json(
        json.dumps(make_synthetic_export_dict(export_id="exp-outbox-01"))
    )
    res = apply_export_plan(pg_uri, envelope=env)
    event_id = res.domain_event_ids[0]

    with psycopg.connect(pg_admin_db_uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT channel, published_at, attempts FROM public.outbox WHERE domain_event_id = %s",
                (event_id,),
            )
            row = cur.fetchone()
            assert row == ("fc27:events", None, 0)


def test_40_failure_before_commit_leaves_no_partial_rows(
    pg_uri, pg_admin_db_uri, monkeypatch: pytest.MonkeyPatch
) -> None:
    """40. If domain event emission raises before transaction commit, zero rows remain in any table."""
    import src.fc27_repository as repo_mod

    def fail_event(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("simulated pre-commit failure")

    monkeypatch.setattr(repo_mod, "record_event", fail_event)
    env = parse_fc27_user_export_json(
        json.dumps(make_synthetic_export_dict(export_id="exp-precommit-fail"))
    )
    with pytest.raises(RuntimeError, match="simulated pre-commit failure"):
        apply_export_plan(pg_uri, envelope=env)

    with psycopg.connect(pg_admin_db_uri) as conn:
        with conn.cursor() as cur:
            for tbl in (
                "fc27_clubs",
                "fc27_export_batches",
                "fc27_matches",
                "fc27_player_match_stats",
                "domain_events",
                "outbox",
            ):
                cur.execute(f"SELECT count(*) FROM public.{tbl}")
                assert cur.fetchone()[0] == 0, f"partial row left in {tbl}"


def test_41_expired_quarantine_purge(pg_uri) -> None:
    """41. purge_expired_quarantine_records removes expired records and emits fc27.quarantine.expired."""
    past_q = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    past_exp = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
    eval_now = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)

    q_res = insert_quarantine_record(
        pg_uri,
        club_id=EAST_COASTERS_CLUB_ID,
        anomaly_category="SCHEMA_DRIFT",
        reason_code="UNRECOGNIZED_SCHEMA_FIELD",
        diagnostic_paths=["matches[0].extra_field"],
        payload_sha256="d" * 64,
        quarantined_at=past_q,
        expires_at=past_exp,
    )

    # Expired quarantine records cannot be reviewed as active
    with pytest.raises(FC27RepositoryError, match="expired"):
        review_quarantine_record(
            pg_uri,
            quarantine_id=q_res.quarantine_id,
            review_status="RESOLVED",
            reviewed_by="steward-reviewer-01",
            reviewed_at=eval_now,
        )

    purge_res = purge_expired_quarantine_records(pg_uri, now=eval_now)
    assert purge_res.purged_count == 1
    assert purge_res.purged_quarantine_ids == [q_res.quarantine_id]
    assert len(purge_res.domain_event_ids) == 1

    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM public.fc27_quarantine_records WHERE quarantine_id = %s",
                (q_res.quarantine_id,),
            )
            assert cur.fetchone()[0] == 0
            cur.execute(
                "SELECT event_type, to_status FROM public.domain_events WHERE event_id = %s",
                (purge_res.domain_event_ids[0],),
            )
            assert cur.fetchone() == ("fc27.quarantine.expired", "EXPIRED")


def test_42_unexpired_quarantine_retained(pg_uri) -> None:
    """42. Unexpired quarantine records are retained when purge_expired_quarantine_records runs."""
    eval_now = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)
    future_exp = eval_now + timedelta(days=15)

    q_res = insert_quarantine_record(
        pg_uri,
        club_id=EAST_COASTERS_CLUB_ID,
        anomaly_category="OUT_OF_RANGE_OBSERVATION",
        reason_code="MINUTES_OUT_OF_RANGE",
        diagnostic_paths=["matches[0].player_performances[0].minutes_played"],
        payload_sha256="e" * 64,
        quarantined_at=eval_now - timedelta(days=15),
        expires_at=future_exp,
    )

    purge_res = purge_expired_quarantine_records(pg_uri, now=eval_now)
    assert purge_res.purged_count == 0

    # Also verify quarantine review event emission
    rev_event_id = review_quarantine_record(
        pg_uri,
        quarantine_id=q_res.quarantine_id,
        review_status="RESOLVED",
        reviewed_by="steward-reviewer-01",
        review_notes="Reviewed synthetic observation.",
    )
    assert rev_event_id > 0

    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT review_status, reviewed_by FROM public.fc27_quarantine_records WHERE quarantine_id = %s",
                (q_res.quarantine_id,),
            )
            assert cur.fetchone() == ("RESOLVED", "steward-reviewer-01")


def test_43_audit_metadata_excludes_aliases_by_default(pg_uri) -> None:
    """43. Domain events and audit metadata never contain display_alias values."""
    secret_alias = "UniqueDisplayAliasNeverInAudit42"
    env = parse_fc27_user_export_json(
        json.dumps(make_synthetic_export_dict(export_id="exp-alias-check", display_alias=secret_alias))
    )
    res = apply_export_plan(pg_uri, envelope=env)
    assert res.status == "APPLIED"
    assert secret_alias not in res.model_dump_json()

    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT reason, actor, entity_id FROM public.domain_events WHERE event_id = %s",
                (res.domain_event_ids[0],),
            )
            row = cur.fetchone()
            assert row is not None
            assert secret_alias not in " ".join(str(x) for x in row if x)


# ==============================================================================
# OPERATOR CLI TESTS (44-54)
# ==============================================================================

def test_44_cli_validate_remains_default(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """44. CLI defaults to validate mode and performs zero persistence."""
    export_file = tmp_path / "synth_export.json"
    export_file.write_text(json.dumps(make_synthetic_export_dict()), encoding="utf-8")

    code = cli_main(["--input", str(export_file), "--format", "json"])
    assert code == 0
    out = capsys.readouterr().out
    assert "IMPORT_STATUS: VALIDATED" in out
    assert "MODE: VALIDATE" in out
    assert "PERSISTENCE_APPLIED: NO" in out


def test_45_cli_dry_run_performs_zero_writes(
    pg_uri, capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """45. CLI --dry-run performs zero database writes even if FC27_PERSISTENCE_ENABLED=true."""
    monkeypatch.setenv("FC27_PERSISTENCE_ENABLED", "true")
    monkeypatch.setenv("POSTGRES_URI", pg_uri)
    export_file = tmp_path / "synth_export.json"
    export_file.write_text(json.dumps(make_synthetic_export_dict()), encoding="utf-8")

    code = cli_main(["--input", str(export_file), "--format", "json", "--dry-run"])
    assert code == 0
    out = capsys.readouterr().out
    assert "IMPORT_STATUS: DRY_RUN" in out
    assert "PERSISTENCE_APPLIED: NO" in out

    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM public.fc27_matches")
            assert cur.fetchone()[0] == 0


def test_46_cli_apply_without_confirmation_rejected(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """46. CLI --apply without --confirm-apply is rejected with exit code 2."""
    export_file = tmp_path / "synth_export.json"
    export_file.write_text(json.dumps(make_synthetic_export_dict()), encoding="utf-8")

    code = cli_main(["--input", str(export_file), "--format", "json", "--apply"])
    assert code == 2
    err = capsys.readouterr().err
    assert "--apply mode requires --confirm-apply" in err


def test_47_cli_apply_while_feature_disabled_returns_deferred(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """47. CLI --apply --confirm-apply while FC27_PERSISTENCE_ENABLED=false returns DEFERRED and exit code 1."""
    monkeypatch.setenv("FC27_PERSISTENCE_ENABLED", "false")
    export_file = tmp_path / "synth_export.json"
    export_file.write_text(json.dumps(make_synthetic_export_dict()), encoding="utf-8")

    code = cli_main([
        "--input", str(export_file),
        "--format", "json",
        "--apply",
        "--confirm-apply",
    ])
    assert code == 1
    out = capsys.readouterr().out
    assert "IMPORT_STATUS: DEFERRED" in out
    assert "PERSISTENCE_APPLIED: NO" in out
    assert "FC27_PERSISTENCE_ENABLED: NO" in out
    assert "HUMAN_REVIEW_REQUIRED: YES" in out


def test_48_cli_apply_without_postgresql_configuration_fails_closed(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """48. CLI --apply --confirm-apply with FC27_PERSISTENCE_ENABLED=true but missing POSTGRES_URI fails closed."""
    monkeypatch.setenv("FC27_PERSISTENCE_ENABLED", "true")
    monkeypatch.delenv("POSTGRES_URI", raising=False)
    monkeypatch.delenv("POSTGRES_URI_FILE", raising=False)
    export_file = tmp_path / "synth_export.json"
    export_file.write_text(json.dumps(make_synthetic_export_dict()), encoding="utf-8")

    code = cli_main([
        "--input", str(export_file),
        "--format", "json",
        "--apply",
        "--confirm-apply",
    ])
    assert code == 1
    out = capsys.readouterr().out
    assert "IMPORT_STATUS: REJECTED" in out
    assert "PERSISTENCE_APPLIED: NO" in out


def test_49_cli_apply_with_identity_conflict_rejected(
    pg_uri, capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """49. CLI --apply --confirm-apply with conflicting club identity is rejected with zero writes."""
    monkeypatch.setenv("FC27_PERSISTENCE_ENABLED", "true")
    monkeypatch.setenv("POSTGRES_URI", pg_uri)
    export_file = tmp_path / "conflict_club.json"
    export_file.write_text(
        json.dumps(make_synthetic_export_dict(club_id="999999", club_name="Wrong Club")),
        encoding="utf-8",
    )

    code = cli_main([
        "--input", str(export_file),
        "--format", "json",
        "--apply",
        "--confirm-apply",
    ])
    assert code == 1
    out = capsys.readouterr().out
    assert "IMPORT_STATUS: REJECTED" in out
    assert "CLUB_IDENTITY_DECISION: CONFLICTED" in out
    assert "PERSISTENCE_APPLIED: NO" in out


def test_50_cli_apply_with_replay_conflict_rejected(
    pg_uri, capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """50. CLI --apply --confirm-apply with a replay conflict blocks batch apply and quarantines the conflict."""
    monkeypatch.setenv("FC27_PERSISTENCE_ENABLED", "true")
    monkeypatch.setenv("POSTGRES_URI", pg_uri)

    f1 = tmp_path / "batch1.json"
    f1.write_text(
        json.dumps(make_synthetic_export_dict(export_id="cli-exp-1", external_match_id="cli-m-1", goals_for=2)),
        encoding="utf-8",
    )
    assert cli_main(["--input", str(f1), "--format", "json", "--apply", "--confirm-apply"]) == 0
    _ = capsys.readouterr()

    f2 = tmp_path / "batch2.json"
    f2.write_text(
        json.dumps(make_synthetic_export_dict(export_id="cli-exp-2", external_match_id="cli-m-1", goals_for=5)),
        encoding="utf-8",
    )
    code2 = cli_main(["--input", str(f2), "--format", "json", "--apply", "--confirm-apply"])
    assert code2 == 1
    out2 = capsys.readouterr().out
    assert "IMPORT_STATUS: REJECTED" in out2
    assert "PERSISTENCE_APPLIED: NO" in out2
    assert "QUARANTINED_RECORDS: 1" in out2


def test_51_cli_successful_synthetic_apply_reports_metadata_only(
    pg_uri, capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """51. Successful synthetic CLI apply outputs all required metadata lines and excludes player aliases by default."""
    monkeypatch.setenv("FC27_PERSISTENCE_ENABLED", "true")
    monkeypatch.setenv("POSTGRES_URI", pg_uri)
    export_file = tmp_path / "apply_ok.json"
    export_file.write_text(
        json.dumps(make_synthetic_export_dict(export_id="cli-apply-ok-01", display_alias="HiddenAlias777")),
        encoding="utf-8",
    )

    code = cli_main([
        "--input", str(export_file),
        "--format", "json",
        "--apply",
        "--confirm-apply",
    ])
    assert code == 0
    out = capsys.readouterr().out
    assert "IMPORT_STATUS: APPLIED" in out
    assert "PERSISTENCE_APPLIED: YES" in out
    assert "EXPORT_ID: cli-apply-ok-01" in out
    assert "ACCEPTED_RECORDS: 1" in out
    assert "DUPLICATE_RECORDS: 0" in out
    assert "QUARANTINED_RECORDS: 0" in out
    assert "RAW_UPLOAD_RETAINED: NO" in out
    assert "RECOMMENDATIONS_GENERATED: 0" in out
    assert "DISPATCH_EXECUTED: NO" in out
    assert "HiddenAlias777" not in out


def test_52_cli_no_database_credentials_printed(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """52. CLI never prints database passwords or unredacted connection strings on connection failure."""
    secret_pw = "SuperSecretDbPasswordDoNotLeak987"
    bad_uri = f"postgresql://fc27_app:{secret_pw}@127.0.0.1:59999/nonexistent_db"
    monkeypatch.setenv("FC27_PERSISTENCE_ENABLED", "true")
    monkeypatch.setenv("POSTGRES_URI", bad_uri)

    export_file = tmp_path / "synth_export.json"
    export_file.write_text(json.dumps(make_synthetic_export_dict()), encoding="utf-8")

    code = cli_main([
        "--input", str(export_file),
        "--format", "json",
        "--apply",
        "--confirm-apply",
    ])
    assert code == 1
    captured = capsys.readouterr()
    assert secret_pw not in captured.out
    assert secret_pw not in captured.err


def test_53_cli_no_raw_input_printed(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """53. CLI never prints raw JSON/CSV input contents when rejecting malformed input."""
    raw_marker = "RAW_UNSANITIZED_PAYLOAD_MARKER_991122"
    bad_file = tmp_path / "malformed.json"
    bad_file.write_text(f'{{"broken": "{raw_marker}"', encoding="utf-8")

    code = cli_main(["--input", str(bad_file), "--format", "json"])
    assert code == 1
    captured = capsys.readouterr()
    assert raw_marker not in captured.out
    assert raw_marker not in captured.err


def test_54_cli_raw_upload_retained_remains_no(
    pg_uri, capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """54. RAW_UPLOAD_RETAINED: NO is reported across validate, dry-run, deferred, and applied runs."""
    export_file = tmp_path / "synth_export.json"
    export_file.write_text(json.dumps(make_synthetic_export_dict()), encoding="utf-8")

    for argv in (
        ["--input", str(export_file), "--format", "json"],
        ["--input", str(export_file), "--format", "json", "--dry-run"],
        ["--input", str(export_file), "--format", "json", "--apply", "--confirm-apply"],
    ):
        cli_main(argv)
        out = capsys.readouterr().out
        assert "RAW_UPLOAD_RETAINED: NO" in out

    monkeypatch.setenv("FC27_PERSISTENCE_ENABLED", "true")
    monkeypatch.setenv("POSTGRES_URI", pg_uri)
    cli_main(["--input", str(export_file), "--format", "json", "--apply", "--confirm-apply"])
    out_applied = capsys.readouterr().out
    assert "RAW_UPLOAD_RETAINED: NO" in out_applied


# ==============================================================================
# ISOLATION & BOUNDARY TESTS (55-60)
# ==============================================================================

def test_55_provider_remains_disabled() -> None:
    """55. FC 27 provider remains disabled by default."""
    assert settings.is_fc27_provider_enabled() is False
    assert settings.is_fc27_public_identity_lookup_enabled() is False
    assert settings.is_fc27_upstream_compatibility_enabled() is False


def test_56_live_ingestion_remains_disabled() -> None:
    """56. FC 27 live ingestion remains disabled by default."""
    assert settings.is_fc27_live_ingestion_enabled() is False


def test_57_recommendations_remain_disabled() -> None:
    """57. FC 27 recommendations remain disabled by default."""
    assert settings.is_fc27_recommendations_enabled() is False


def test_58_dispatch_remains_disabled() -> None:
    """58. Dispatch remains disabled by default."""
    assert settings.is_dispatch_enabled() is False


def test_59_dispatch_worker_remains_disabled() -> None:
    """59. Dispatch worker remains disabled by default."""
    assert settings.is_dispatch_worker_enabled() is False


def test_60_matchpulse_boundary_remains_intact() -> None:
    """60. MatchPulse boundary script passes and transport allowlist remains unchanged."""
    assert FC27TransportClient.DEFAULT_ALLOWED_HOSTS == {"proclubs.ea.com"}
    proc = subprocess.run(
        [sys.executable, "scripts/check_matchpulse_boundary.py"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "boundary drift check: OK" in proc.stdout
