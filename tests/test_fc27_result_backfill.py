"""Comprehensive test suite for FC27 Result Backfill CLI and Engine."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
import hashlib
import re
from typing import Any

import psycopg
import pytest

from src.db_migrate import migrate
from src.fc27_discord_delivery import (
    MESSAGE_KIND_FORMAL,
    MESSAGE_KIND_TEAM_CHAT,
    upsert_destination,
)
from src.fc27_discord_dispatcher import (
    SyntheticDiscordNotificationTransport,
)
from src.fc27_privacy import (
    EAST_COASTERS_CLUB_ID,
    EAST_COASTERS_GAME_VERSION,
    EAST_COASTERS_PLATFORM,
    pseudonymize_match_ref,
)
from src.fc27_result_backfill import (
    BACKFILL_ADVISORY_NAMESPACE,
    DEFAULT_LATEST_COUNT,
    MAX_LATEST_COUNT,
    FC27BackfillError,
    FC27BackfillLockBusyError,
    FC27BackfillValidationError,
    main,
    run_result_backfill,
)


def _seed_test_matches(
    admin_conn: psycopg.Connection,
    count: int = 15,
    *,
    club_id: str = EAST_COASTERS_CLUB_ID,
) -> list[str]:
    """Insert `count` completed real matches with staggered played_at timestamps."""
    match_refs = []
    base_time = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    with admin_conn.cursor() as cur:
        # Seed club first
        cur.execute(
            """
            INSERT INTO public.fc27_clubs (
                club_id, platform, game_version, club_name,
                identity_state, manager_authority_status, source_classification
            ) VALUES (
                %s, %s, %s, 'East Coasters FC',
                'SYNTHETIC_ONLY', 'UNVERIFIED', 'SYNTHETIC'
            ) ON CONFLICT (club_id, platform, game_version) DO NOTHING
            """,
            (club_id, EAST_COASTERS_PLATFORM, EAST_COASTERS_GAME_VERSION),
        )

        for i in range(1, count + 1):
            ext_id = f"ext_match_{i:04d}"
            export_id = f"exp_match_{i:04d}"
            played_at = base_time + timedelta(hours=i)
            match_hash = hashlib.sha256(f"{club_id}:{ext_id}".encode()).hexdigest()
            cur.execute(
                """
                INSERT INTO public.fc27_matches (
                    club_id,
                    platform,
                    game_version,
                    external_match_id,
                    export_id,
                    played_at,
                    result,
                    goals_for,
                    goals_against,
                    opponent_name,
                    formation,
                    match_status,
                    match_hash,
                    source_classification,
                    data_quality_status
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s, 'WIN', 3, 1,
                    'Opponent FC', '4-3-3', 'COMPLETED', %s,
                    'COMMUNITY_RESEARCH_DERIVED', 'VERIFIED'
                ) ON CONFLICT (club_id, platform, game_version, external_match_id) DO NOTHING
                """,
                (
                    club_id,
                    EAST_COASTERS_PLATFORM,
                    EAST_COASTERS_GAME_VERSION,
                    ext_id,
                    export_id,
                    played_at,
                    match_hash,
                ),
            )
            m_ref = pseudonymize_match_ref(ext_id, club_id=club_id)
            match_refs.append(m_ref)
    admin_conn.commit()
    return match_refs


@pytest.mark.postgres
def test_backfill_bounds_validation() -> None:
    # Test invalid latest count
    with pytest.raises(FC27BackfillValidationError, match="Latest count must be between"):
        run_result_backfill(
            destination_key="any-dest",
            db_conn_or_uri="dummy",
            latest=0,
        )

    with pytest.raises(FC27BackfillValidationError, match="Latest count must be between"):
        run_result_backfill(
            destination_key="any-dest",
            db_conn_or_uri="dummy",
            latest=MAX_LATEST_COUNT + 1,
        )

    with pytest.raises(FC27BackfillValidationError, match="Destination key is required"):
        run_result_backfill(
            destination_key="",
            db_conn_or_uri="dummy",
        )


@pytest.mark.postgres
def test_backfill_dry_run_writes_nothing(pg_db) -> None:
    migrate(pg_db.migrator)

    with psycopg.connect(pg_db.admin) as admin_conn:
        _seed_test_matches(admin_conn, 12)

    with psycopg.connect(pg_db.match_results_writer) as conn:
        upsert_destination(
            conn,
            destination_key="east-coasters-formal",
            guild_id="11111111111111111",
            channel_id="22222222222222222",
            message_kind=MESSAGE_KIND_FORMAL,
        )
        conn.commit()

        transport = SyntheticDiscordNotificationTransport()
        summary = run_result_backfill(
            destination_key="east-coasters-formal",
            db_conn_or_uri=conn,
            transport=transport,
            latest=10,
            dry_run=True,
        )

        assert summary.dry_run is True
        assert summary.total_eligible_found == 10
        assert summary.planned_or_sent_count == 10
        assert summary.already_delivered_count == 0
        assert summary.failed_count == 0
        assert summary.provider_requests_executed == 0

        # Verify NO rows in public.fc27_match_discord_deliveries
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM public.fc27_match_discord_deliveries WHERE destination_key = 'east-coasters-formal'"
            )
            count = cur.fetchone()[0]
            assert count == 0

        # Verify transport sent zero messages
        assert len(transport.sent_notifications) == 0


@pytest.mark.postgres
def test_backfill_confirmed_execution_and_ordering(pg_db) -> None:
    migrate(pg_db.migrator)

    with psycopg.connect(pg_db.admin) as admin_conn:
        _seed_test_matches(admin_conn, 10)

    with psycopg.connect(pg_db.match_results_writer) as conn:
        upsert_destination(
            conn,
            destination_key="east-coasters-formal",
            guild_id="11111111111111111",
            channel_id="22222222222222222",
            message_kind=MESSAGE_KIND_FORMAL,
        )
        conn.commit()

        transport = SyntheticDiscordNotificationTransport()
        summary = run_result_backfill(
            destination_key="east-coasters-formal",
            db_conn_or_uri=conn,
            transport=transport,
            latest=10,
            dry_run=False,
        )

        assert summary.dry_run is False
        assert summary.total_eligible_found == 10
        assert summary.planned_or_sent_count == 10
        assert summary.already_delivered_count == 0
        assert summary.failed_count == 0

        # Transport sent 10 messages
        assert len(transport.sent_notifications) == 10

        # Deliveries recorded in DB
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM public.fc27_match_discord_deliveries WHERE destination_key = 'east-coasters-formal' AND delivery_state = 'DELIVERED'"
            )
            count = cur.fetchone()[0]
            assert count == 10

        # Second backfill run skips all 10
        summary_2 = run_result_backfill(
            destination_key="east-coasters-formal",
            db_conn_or_uri=conn,
            transport=transport,
            latest=10,
            dry_run=False,
        )
        assert summary_2.already_delivered_count == 10
        assert summary_2.planned_or_sent_count == 0
        # No extra messages sent
        assert len(transport.sent_notifications) == 10


@pytest.mark.postgres
def test_backfill_quarantined_matches_excluded(pg_db) -> None:
    migrate(pg_db.migrator)

    with psycopg.connect(pg_db.admin) as admin_conn:
        _seed_test_matches(admin_conn, 5)

        # Quarantine one match
        with admin_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.fc27_quarantine_records (
                    quarantined_record_id,
                    club_id,
                    status,
                    reason,
                    quarantined_at
                ) VALUES (
                    'ext_match_0003',
                    %s,
                    'ACTIVE',
                    'MANUAL_QUARANTINE_TEST',
                    pg_catalog.clock_timestamp()
                )
                """,
                (EAST_COASTERS_CLUB_ID,),
            )
        admin_conn.commit()

    with psycopg.connect(pg_db.match_results_writer) as conn:
        upsert_destination(
            conn,
            destination_key="east-coasters-formal",
            guild_id="11111111111111111",
            channel_id="22222222222222222",
            message_kind=MESSAGE_KIND_FORMAL,
        )
        conn.commit()

        transport = SyntheticDiscordNotificationTransport()
        summary = run_result_backfill(
            destination_key="east-coasters-formal",
            db_conn_or_uri=conn,
            transport=transport,
            latest=10,
            dry_run=True,
        )
        # 5 matches seeded, 1 quarantined -> 4 eligible matches found
        assert summary.total_eligible_found == 4


@pytest.mark.postgres
def test_backfill_single_flight_advisory_lock(pg_db) -> None:
    migrate(pg_db.migrator)

    with psycopg.connect(pg_db.match_results_writer) as conn1, psycopg.connect(pg_db.match_results_writer) as conn2:
        upsert_destination(
            conn1,
            destination_key="locked-dest",
            guild_id="11111111111111111",
            channel_id="22222222222222222",
            message_kind=MESSAGE_KIND_FORMAL,
        )
        conn1.commit()

        # Hold lock on conn1
        with conn1.cursor() as cur:
            cur.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s), hashtext(%s))",
                (BACKFILL_ADVISORY_NAMESPACE, "locked-dest"),
            )
            assert cur.fetchone()[0] is True

        # Second backfill attempt on conn2 fails fast with lock busy error
        with pytest.raises(FC27BackfillLockBusyError, match="actively held by another process"):
            run_result_backfill(
                destination_key="locked-dest",
                db_conn_or_uri=conn2,
                latest=10,
            )

        # Release lock on conn1
        with conn1.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_unlock(hashtext(%s), hashtext(%s))",
                (BACKFILL_ADVISORY_NAMESPACE, "locked-dest"),
            )
            assert cur.fetchone()[0] is True


@pytest.mark.postgres
def test_backfill_unauthorized_and_invalid_destination(pg_db) -> None:
    migrate(pg_db.migrator)

    with psycopg.connect(pg_db.match_results_writer) as conn:
        # Non-existent destination raises error
        with pytest.raises(FC27BackfillValidationError, match="is not registered in public.fc27_discord_destinations"):
            run_result_backfill(
                destination_key="unconfigured",
                db_conn_or_uri=conn,
                latest=10,
            )

        # Direct numeric snowflake rejected as destination_key
        with pytest.raises(FC27BackfillValidationError, match="Invalid destination key"):
            run_result_backfill(
                destination_key="987654321098765432",
                db_conn_or_uri=conn,
                latest=10,
            )


def test_sanitized_summary_report_does_not_contain_secrets_or_snowflakes() -> None:
    from src.fc27_result_backfill import FC27ResultBackfillSummary

    summary = FC27ResultBackfillSummary(
        destination_key="east-coasters-analytics",
        message_kind="FORMAL_RESULT",
        dry_run=True,
        total_eligible_found=10,
        already_delivered_count=0,
        planned_or_sent_count=10,
        failed_count=0,
        remaining_count=0,
        provider_requests_executed=0,
    )
    report = summary.format_sanitized_report()
    assert "east-coasters-analytics" in report
    assert "FORMAL_RESULT" in report
    assert "DRY-RUN" in report
    assert "STRICT ZERO BOUND" in report
    # No raw snowflake pattern in report
    assert not re.search(r"\b[0-9]{17,20}\b", report)
