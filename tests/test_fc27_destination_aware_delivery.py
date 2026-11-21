"""Comprehensive tests for FC27 destination-aware delivery registry and linkage."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import psycopg
import pytest

from src.db_migrate import migrate, verify_postconditions
from src.fc27_discord_delivery import (
    DELIVERY_STATE_CLAIMED,
    DELIVERY_STATE_DELIVERED,
    DELIVERY_STATE_FAILED,
    DELIVERY_STATE_MISSING,
    DELIVERY_STATE_REVOKED,
    MESSAGE_KIND_FORMAL,
    MESSAGE_KIND_TEAM_CHAT,
    FC27DeliveryValidationError,
    FC27DiscordDestination,
    build_delivery_idempotency_key,
    get_deliveries_for_refresh,
    get_destination_by_key,
    list_destinations,
    record_delivery_failure,
    record_delivery_refresh_state,
    record_delivery_success,
    reserve_discord_delivery,
    sync_destinations_from_config,
    upsert_destination,
)
from src.fc27_discord_dispatcher import (
    FC27DiscordDispatchConfig,
    FC27DiscordDispatchCycleResult,
    SyntheticDiscordNotificationTransport,
    run_discord_dispatch_cycle,
)
from src.fc27_privacy import EAST_COASTERS_CLUB_ID


def test_destination_key_and_idempotency_key_validation() -> None:
    # Valid key construction
    key = build_delivery_idempotency_key("match-123", "east-coasters-formal", MESSAGE_KIND_FORMAL)
    assert key == "post-result:match-123:east-coasters-formal:FORMAL_RESULT:v1"

    # Invalid destination key format
    with pytest.raises(FC27DeliveryValidationError, match="Invalid destination key"):
        build_delivery_idempotency_key("match-123", "INVALID KEY!", MESSAGE_KIND_FORMAL)

    # Invalid message kind
    with pytest.raises(FC27DeliveryValidationError, match="Invalid message kind"):
        build_delivery_idempotency_key("match-123", "east-coasters-formal", "UNSUPPORTED_KIND")

    # Missing match_ref
    with pytest.raises(FC27DeliveryValidationError, match="match_ref is required"):
        build_delivery_idempotency_key("", "east-coasters-formal", MESSAGE_KIND_FORMAL)


@pytest.mark.postgres
def test_destination_registry_crud_and_uniqueness(pg_db) -> None:
    migrate(pg_db.migrator)

    with psycopg.connect(pg_db.match_results_writer) as conn:
        # Invalid snowflake format raises validation error
        with pytest.raises(FC27DeliveryValidationError, match="Invalid guild_id"):
            upsert_destination(conn, "test-dest", "invalid-guild", "12345678901234567", MESSAGE_KIND_FORMAL)

        with pytest.raises(FC27DeliveryValidationError, match="Invalid channel_id"):
            upsert_destination(conn, "test-dest", "12345678901234567", "invalid-chan", MESSAGE_KIND_FORMAL)

        # Successful upsert
        dest_id = upsert_destination(
            conn,
            destination_key="test-formal",
            guild_id="11223344556677889",
            channel_id="22334455667788990",
            message_kind=MESSAGE_KIND_FORMAL,
            allow_backfill=True,
            allow_normal_delivery=True,
            allow_roster_refresh=True,
        )
        assert dest_id > 0
        conn.commit()

        # Lookup
        dest = get_destination_by_key(conn, "test-formal")
        assert dest is not None
        assert dest.destination_key == "test-formal"
        assert dest.guild_id == "11223344556677889"
        assert dest.channel_id == "22334455667788990"
        assert dest.message_kind == MESSAGE_KIND_FORMAL
        assert dest.enabled is True

        # Idempotent upsert modifies in place
        dest_id_2 = upsert_destination(
            conn,
            destination_key="test-formal",
            guild_id="11223344556677889",
            channel_id="99994455667788990",
            message_kind=MESSAGE_KIND_FORMAL,
        )
        assert dest_id_2 == dest_id
        conn.commit()

        updated = get_destination_by_key(conn, "test-formal")
        assert updated is not None
        assert updated.channel_id == "99994455667788990"


@pytest.mark.postgres
def test_disabled_destination_skipped(pg_db) -> None:
    migrate(pg_db.migrator)

    with psycopg.connect(pg_db.match_results_writer) as conn:
        dest_id = upsert_destination(
            conn,
            destination_key="disabled-dest",
            guild_id="11223344556677889",
            channel_id="22334455667788990",
            message_kind=MESSAGE_KIND_FORMAL,
        )
        # Disable it directly
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE public.fc27_discord_destinations SET enabled = FALSE WHERE destination_id = %s",
                (dest_id,),
            )
        conn.commit()

        res = reserve_discord_delivery(
            conn,
            match_ref="match-ref-001",
            destination_key="disabled-dest",
            message_kind=MESSAGE_KIND_FORMAL,
        )
        assert res.is_skipped
        assert res.status == "SKIPPED_DISABLED"


@pytest.mark.postgres
def test_delivery_reservation_success_and_idempotency(pg_db) -> None:
    migrate(pg_db.migrator)

    with psycopg.connect(pg_db.match_results_writer) as conn:
        upsert_destination(
            conn,
            destination_key="chan-formal",
            guild_id="11111111111111111",
            channel_id="22222222222222222",
            message_kind=MESSAGE_KIND_FORMAL,
        )
        conn.commit()

        # 1. First claim
        res1 = reserve_discord_delivery(
            conn,
            match_ref="m_100",
            destination_key="chan-formal",
            message_kind=MESSAGE_KIND_FORMAL,
        )
        assert res1.is_claimed
        assert res1.delivery_id is not None
        conn.commit()

        # 2. Record success
        record_delivery_success(conn, res1.delivery_id, "33333333333333333", "m_100")
        conn.commit()

        # 3. Subsequent reservation returns ALREADY_DELIVERED with existing message ID
        res2 = reserve_discord_delivery(
            conn,
            match_ref="m_100",
            destination_key="chan-formal",
            message_kind=MESSAGE_KIND_FORMAL,
        )
        assert res2.is_already_delivered
        assert res2.message_id == "33333333333333333"


@pytest.mark.postgres
def test_two_destinations_independent_failure_isolation(pg_db) -> None:
    migrate(pg_db.migrator)

    with psycopg.connect(pg_db.match_results_writer) as conn:
        upsert_destination(
            conn,
            destination_key="dest-alpha",
            guild_id="11111111111111111",
            channel_id="22222222222222222",
            message_kind=MESSAGE_KIND_FORMAL,
        )
        upsert_destination(
            conn,
            destination_key="dest-beta",
            guild_id="11111111111111111",
            channel_id="33333333333333333",
            message_kind=MESSAGE_KIND_FORMAL,
        )
        conn.commit()

        # Claim alpha and succeed
        res_a = reserve_discord_delivery(conn, "match_xyz", "dest-alpha", MESSAGE_KIND_FORMAL)
        assert res_a.is_claimed
        record_delivery_success(conn, res_a.delivery_id, "44444444444444444", "match_xyz")
        conn.commit()

        # Claim beta and fail
        res_b = reserve_discord_delivery(conn, "match_xyz", "dest-beta", MESSAGE_KIND_FORMAL)
        assert res_b.is_claimed
        record_delivery_failure(conn, res_b.delivery_id, "HTTP_TIMEOUT", "match_xyz")
        conn.commit()

        # Alpha remains delivered
        check_a = reserve_discord_delivery(conn, "match_xyz", "dest-alpha", MESSAGE_KIND_FORMAL)
        assert check_a.is_already_delivered
        assert check_a.message_id == "44444444444444444"

        # Beta can be reclaimed safely on retry
        check_b = reserve_discord_delivery(conn, "match_xyz", "dest-beta", MESSAGE_KIND_FORMAL)
        assert check_b.is_claimed


@pytest.mark.postgres
def test_legacy_linkage_import_preservation(pg_db) -> None:
    # 1. Migrate to 0019
    migrate(pg_db.migrator, target="0019")

    # 2. Insert dummy legacy audit row in fc27_manual_match_audit
    with psycopg.connect(pg_db.match_results_writer) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.fc27_manual_match_audit (
                    audit_id,
                    match_id,
                    action,
                    actor_discord_user_id,
                    discord_guild_id,
                    discord_channel_id,
                    discord_message_id,
                    audit_metadata,
                    recorded_at
                ) VALUES (
                    101,
                    NULL,
                    'RELINK_DISCORD_MESSAGE',
                    '12345678901234567',
                    '11111111111111111',
                    '22222222222222222',
                    '33333333333333333',
                    %s::jsonb,
                    '2026-11-01 12:00:00+00'::timestamptz
                )
                """,
                (
                    json.dumps({
                        "kind": "discord_message_linkage",
                        "match_ref": "mref_legacy_001",
                        "results_channel_id": "22222222222222222",
                        "results_message_id": "33333333333333333",
                        "team_chat_channel_id": "44444444444444444",
                        "team_chat_message_id": "55555555555555555",
                    }),
                ),
            )
        conn.commit()

    # 3. Apply migration 0022
    applied = migrate(pg_db.migrator)
    assert any("0022" in m for m in applied)

    # 4. Check that legacy deliveries were imported
    with psycopg.connect(pg_db.discord_reader) as conn:
        deliveries = get_deliveries_for_refresh(conn, "mref_legacy_001")
        assert len(deliveries) == 2
        kinds = {d[2]: d for d in deliveries}
        assert MESSAGE_KIND_FORMAL in kinds
        assert kinds[MESSAGE_KIND_FORMAL][5] == "33333333333333333"
        assert kinds[MESSAGE_KIND_FORMAL][1] == "legacy-formal"

        assert MESSAGE_KIND_TEAM_CHAT in kinds
        assert kinds[MESSAGE_KIND_TEAM_CHAT][5] == "55555555555555555"
        assert kinds[MESSAGE_KIND_TEAM_CHAT][1] == "legacy-team-chat"


@pytest.mark.postgres
def test_stale_delivery_reservation_handling(pg_db) -> None:
    migrate(pg_db.migrator)

    with psycopg.connect(pg_db.match_results_writer) as conn:
        upsert_destination(
            conn,
            destination_key="stale-dest",
            guild_id="11111111111111111",
            channel_id="22222222222222222",
            message_kind=MESSAGE_KIND_FORMAL,
        )
        conn.commit()

        # Claim delivery
        res = reserve_discord_delivery(conn, "match_stale", "stale-dest", MESSAGE_KIND_FORMAL)
        assert res.is_claimed
        conn.commit()

        # Simulate timeout by leaving in CLAIMED state; second attempt re-claims
        res2 = reserve_discord_delivery(conn, "match_stale", "stale-dest", MESSAGE_KIND_FORMAL)
        assert res2.is_claimed
        assert res2.delivery_id == res.delivery_id


@pytest.mark.postgres
def test_migration_0022_fresh_and_upgrade_and_postconditions(pg_db) -> None:
    # Fresh migration of complete schema
    applied = migrate(pg_db.migrator)
    assert any("0022" in m for m in applied)

    # Verify postconditions pass cleanly
    with psycopg.connect(pg_db.admin) as conn:
        violations = verify_postconditions(conn)
        assert not violations, f"Postcondition violations: {violations}"

    # Idempotent rerun
    applied_2 = migrate(pg_db.migrator)
    assert applied_2 == []
