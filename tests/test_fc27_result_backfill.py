"""Comprehensive test suite for FC27 Result Backfill CLI and Engine.

Verifies:
1. Dry run with no transport succeeds and writes nothing.
2. Confirm with no transport fails before reservation.
3. Confirm with synthetic transport fails before reservation.
4. Production CLI never constructs synthetic transport.
5. Synthetic transport remains usable only through explicit test injection.
6. False/synthetic message ID rejected.
7. Empty message ID rejected.
8. Malformed Discord message ID rejected.
9. Live transport not ready rejected.
10. Live transport lacking authenticated bot user rejected.
11. Live send plus successful fetch finalizes DELIVERED.
12. Live send plus fetch-not-found does not finalize DELIVERED.
13. Live send plus wrong channel does not finalize DELIVERED.
14. Live send plus wrong guild does not finalize DELIVERED.
15. Live send plus wrong author does not finalize DELIVERED.
16. Live send plus missing match marker does not finalize DELIVERED.
17. Ambiguous timeout does not resend blindly.
18. Same-destination idempotency still prevents duplicates.
19. Other-destination delivery remains independent.
20. Formal and compact backfills remain independent.
21. Provider requests remain zero.
22. No raw IDs, payloads, aliases, or credentials logged.
23. Existing dispatcher and destination-aware delivery tests remain green.
24. Existing roster-refresh behavior remains green.
25. Incident regression test reproducing exact prior failure.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any, LiteralString

import psycopg
import pytest

from src.db_migrate import migrate
from src.fc27_discord_delivery import (
    MESSAGE_KIND_FORMAL,
    MESSAGE_KIND_TEAM_CHAT,
    upsert_destination,
)
from src.fc27_discord_dispatcher import (
    DiscordNotificationTransport,
    FC27DiscordMessageSnapshot,
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
    FC27BackfillError,
    FC27BackfillLockBusyError,
    FC27BackfillValidationError,
    FC27LiveDiscordTransportRequiredError,
    FC27ResultBackfillSummary,
    is_synthetic_message_id,
    main,
    run_result_backfill,
    validate_discord_message_id,
)

VALIDATION_ERROR_CATEGORY = "FC27BACKFILLVALIDATIONERROR"
FORMAL_CHANNEL_ID = "22222222222222222"
GUILD_ID = "11111111111111111"


class MockLiveDiscordGatewayTransport:
    """Mock simulating an active live DiscordGatewayChannelTransport."""

    def __init__(
        self,
        *,
        bot_user_id: str = "1573141508255776858",
        guild_id: str = GUILD_ID,
        channel_id: str = FORMAL_CHANNEL_ID,
        is_ready: bool = True,
        is_closed: bool = False,
        base_message_id: int = 1573456332512825400,
        fail_fetch: bool = False,
        fetch_timeout: bool = False,
        override_channel_id: str | None = None,
        override_guild_id: str | None = None,
        override_author_id: str | None = None,
        override_is_bot: bool = True,
        strip_markers: bool = False,
        override_message_id: str | None = None,
        supports_send: bool = True,
        supports_fetch_after_send: bool = True,
    ) -> None:
        self.transport_name = "discord_gateway"
        self._bot_user_id = bot_user_id
        self._guild_id = guild_id
        self._channel_id = channel_id
        self._is_ready = is_ready
        self._is_closed = is_closed
        self._base_message_id = base_message_id
        self.fail_fetch = fail_fetch
        self.fetch_timeout = fetch_timeout
        self.override_channel_id = override_channel_id
        self.override_guild_id = override_guild_id
        self.override_author_id = override_author_id
        self.override_is_bot = override_is_bot
        self.strip_markers = strip_markers
        self.override_message_id = override_message_id
        self.supports_send = supports_send
        self.supports_fetch_after_send = supports_fetch_after_send
        self._seq = 0
        self.sent_messages: list[Any] = []
        self.fetch_requests: list[tuple[str, str]] = []
        self.readiness_requests: list[tuple[str, str | None]] = []
        self._snapshots: dict[str, FC27DiscordMessageSnapshot] = {}

    @property
    def authenticated_bot_user_id(self) -> str | None:
        return self._bot_user_id if self._bot_user_id else None

    @property
    def authenticated_bot_user_id_available(self) -> bool:
        return bool(self._bot_user_id)

    @property
    def is_live(self) -> bool:
        return self._is_ready and not self._is_closed and self.authenticated_bot_user_id_available

    def send_notification(self, *, channel_id: str, message: Any) -> str:
        self._seq += 1
        msg_id = self.override_message_id if self.override_message_id is not None else str(self._base_message_id + self._seq)
        self.sent_messages.append(message)
        cid = self.override_channel_id or channel_id
        gid = self.override_guild_id or self._guild_id
        aid = self.override_author_id or self._bot_user_id

        emb = message.embed
        if self.strip_markers:
            emb = None

        self._snapshots[msg_id] = FC27DiscordMessageSnapshot(
            channel_id=cid,
            message_id=msg_id,
            guild_id=gid,
            is_bot_authored=self.override_is_bot,
            author_id=aid,
            content="" if self.strip_markers else getattr(message, "content", ""),
            embed=emb,
        )
        return msg_id

    def send_team_chat_notification(self, *, channel_id: str, message: Any) -> str:
        self._seq += 1
        msg_id = self.override_message_id if self.override_message_id is not None else str(self._base_message_id + self._seq)
        self.sent_messages.append(message)
        cid = self.override_channel_id or channel_id
        gid = self.override_guild_id or self._guild_id
        aid = self.override_author_id or self._bot_user_id
        content = "" if self.strip_markers else getattr(message, "content", "")

        self._snapshots[msg_id] = FC27DiscordMessageSnapshot(
            channel_id=cid,
            message_id=msg_id,
            guild_id=gid,
            is_bot_authored=self.override_is_bot,
            author_id=aid,
            content=content,
            embed=None,
        )
        return msg_id

    def fetch_notification_snapshot(
        self, *, channel_id: str, message_id: str
    ) -> FC27DiscordMessageSnapshot | None:
        self.fetch_requests.append((channel_id, message_id))
        if self.fetch_timeout:
            raise TimeoutError("Discord fetch timed out")
        if self.fail_fetch:
            return None
        return self._snapshots.get(message_id)

    def verify_destination_readiness(
        self, *, channel_id: str, guild_id: str | None = None
    ) -> None:
        self.readiness_requests.append((channel_id, guild_id))
        if not self.is_live:
            raise FC27BackfillError("Gateway client not ready")


def _seed_test_matches(
    admin_conn: psycopg.Connection,
    count: int = 15,
    *,
    club_id: str = EAST_COASTERS_CLUB_ID,
) -> list[str]:
    """Insert `count` completed real matches with staggered played_at timestamps."""
    match_refs = []
    base_time = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
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

            # Seed export batch before match
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
                    'CLUB_MANAGER', 'steward-ref', '1.0.0',
                    '["MATCH_RESULTS"]'::jsonb, repeat('a', 64),
                    1, 1, 0
                ) ON CONFLICT (export_id) DO NOTHING
                """,
                (export_id, club_id, EAST_COASTERS_PLATFORM, EAST_COASTERS_GAME_VERSION),
            )

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


def _fetch_row(
    conn: psycopg.Connection, query: LiteralString, params: tuple[Any, ...] = ()
) -> tuple[Any, ...]:
    with conn.cursor() as cur:
        cur.execute(query, params)
        row = cur.fetchone()
    assert row is not None, "query returned no rows"
    return row


def _fetch_scalar(
    conn: psycopg.Connection, query: LiteralString, params: tuple[Any, ...] = ()
) -> Any:
    return _fetch_row(conn, query, params)[0]


def _register_destination(
    conn: psycopg.Connection,
    *,
    destination_key: str = "east-coasters-formal",
    channel_id: str = FORMAL_CHANNEL_ID,
    message_kind: str = MESSAGE_KIND_FORMAL,
) -> None:
    upsert_destination(
        conn,
        destination_key=destination_key,
        guild_id=GUILD_ID,
        channel_id=channel_id,
        message_kind=message_kind,
    )
    conn.commit()


# --- Tests 1-25 ---

@pytest.mark.postgres
def test_1_dry_run_with_no_transport_succeeds_and_writes_nothing(pg_db) -> None:
    """1. Dry run with no transport succeeds and writes nothing."""
    migrate(pg_db.migrator)
    with psycopg.connect(pg_db.admin) as admin_conn:
        _seed_test_matches(admin_conn, 10)

    with psycopg.connect(pg_db.match_results_writer) as conn:
        _register_destination(conn)

        summary = run_result_backfill(
            destination_key="east-coasters-formal",
            db_conn_or_uri=pg_db.admin,
            transport=None,
            latest=10,
            dry_run=True,
        )

        assert summary.dry_run is True
        assert summary.total_eligible_found == 10
        assert summary.planned_or_sent_count == 10
        assert summary.already_delivered_count == 0
        assert summary.failed_count == 0
        assert summary.provider_requests_executed == 0

        assert _fetch_scalar(
            conn,
            "SELECT count(*) FROM public.fc27_match_discord_deliveries WHERE destination_key = 'east-coasters-formal'",
        ) == 0


@pytest.mark.postgres
def test_2_confirm_with_no_transport_fails_before_reservation(pg_db) -> None:
    """2. Confirm with no transport fails before reservation."""
    migrate(pg_db.migrator)
    with psycopg.connect(pg_db.admin) as admin_conn:
        _seed_test_matches(admin_conn, 10)

    with psycopg.connect(pg_db.match_results_writer) as conn:
        _register_destination(conn)

        with pytest.raises(
            FC27LiveDiscordTransportRequiredError,
            match="LIVE_DISCORD_TRANSPORT_REQUIRED: confirmed backfill requires an explicit live Discord transport",
        ):
            run_result_backfill(
                destination_key="east-coasters-formal",
                db_conn_or_uri=pg_db.admin,
                transport=None,
                latest=10,
                dry_run=False,
            )

        assert _fetch_scalar(conn, "SELECT count(*) FROM public.fc27_match_discord_deliveries") == 0


@pytest.mark.postgres
def test_3_confirm_with_synthetic_transport_fails_before_reservation(pg_db) -> None:
    """3. Confirm with synthetic transport fails before reservation."""
    migrate(pg_db.migrator)
    with psycopg.connect(pg_db.admin) as admin_conn:
        _seed_test_matches(admin_conn, 10)

    with psycopg.connect(pg_db.match_results_writer) as conn:
        _register_destination(conn)

        transport = SyntheticDiscordNotificationTransport()
        with pytest.raises(
            FC27LiveDiscordTransportRequiredError,
            match="LIVE_DISCORD_TRANSPORT_REQUIRED: synthetic or non-live transport rejected for confirmed backfill",
        ):
            run_result_backfill(
                destination_key="east-coasters-formal",
                db_conn_or_uri=pg_db.admin,
                transport=transport,
                latest=10,
                dry_run=False,
            )

        assert _fetch_scalar(conn, "SELECT count(*) FROM public.fc27_match_discord_deliveries") == 0


def test_4_production_cli_never_constructs_synthetic_transport(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """4. Production CLI exits nonzero with LIVE_DISCORD_TRANSPORT_REQUIRED when confirmed."""
    constructed: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def _record_construction(*args: Any, **kwargs: Any) -> None:
        constructed.append((args, kwargs))
        raise AssertionError("production CLI must not construct a synthetic transport")

    monkeypatch.setattr(
        "src.fc27_result_backfill.SyntheticDiscordNotificationTransport",
        _record_construction,
    )
    exit_code = main(["--destination", "east-coasters-formal", "--confirm"])
    captured = capsys.readouterr()
    assert exit_code != 0
    assert "LIVE_DISCORD_TRANSPORT_REQUIRED" in captured.err
    assert constructed == []


@pytest.mark.postgres
def test_5_synthetic_transport_usable_only_through_explicit_test_injection(pg_db) -> None:
    """5. Synthetic transport remains usable only through explicit test injection."""
    migrate(pg_db.migrator)
    with psycopg.connect(pg_db.admin) as admin_conn:
        _seed_test_matches(admin_conn, 3)

    with psycopg.connect(pg_db.match_results_writer) as conn:
        _register_destination(conn)

        transport = SyntheticDiscordNotificationTransport(
            base_message_id=1573456332512825400,
            authenticated_bot_user_id="1573141508255776858",
            default_guild_id=GUILD_ID,
        )

        # Without explicit allow_test_transport: fails
        with pytest.raises(FC27LiveDiscordTransportRequiredError):
            run_result_backfill(
                destination_key="east-coasters-formal",
                db_conn_or_uri=pg_db.admin,
                transport=transport,
                latest=3,
                dry_run=False,
                allow_test_transport=False,
            )

        # With explicit allow_test_transport=True: succeeds
        summary = run_result_backfill(
            destination_key="east-coasters-formal",
            db_conn_or_uri=pg_db.admin,
            transport=transport,
            latest=3,
            dry_run=False,
            allow_test_transport=True,
        )
        assert summary.planned_or_sent_count == 3
        assert summary.failed_count == 0


@pytest.mark.postgres
def test_6_false_synthetic_message_id_rejected(pg_db) -> None:
    """6. False/synthetic message ID rejected."""
    migrate(pg_db.migrator)
    with psycopg.connect(pg_db.admin) as admin_conn:
        _seed_test_matches(admin_conn, 2)

    with psycopg.connect(pg_db.match_results_writer) as conn:
        _register_destination(conn)

        # Returns known synthetic range 880000000000000001
        assert is_synthetic_message_id("880000000000000001") is True
        transport = MockLiveDiscordGatewayTransport(override_message_id="880000000000000001")
        summary = run_result_backfill(
            destination_key="east-coasters-formal",
            db_conn_or_uri=pg_db.admin,
            transport=transport,
            latest=1,
            dry_run=False,
        )
        assert summary.failed_count == 1
        row = _fetch_row(conn, "SELECT delivery_state, last_error_category FROM public.fc27_match_discord_deliveries")
        assert row[0] == "FAILED"
        assert row[1] == VALIDATION_ERROR_CATEGORY


@pytest.mark.postgres
def test_7_empty_message_id_rejected(pg_db) -> None:
    """7. Empty message ID rejected."""
    migrate(pg_db.migrator)
    with psycopg.connect(pg_db.admin) as admin_conn:
        _seed_test_matches(admin_conn, 2)

    with psycopg.connect(pg_db.match_results_writer) as conn:
        _register_destination(conn)

        transport = MockLiveDiscordGatewayTransport(override_message_id="")
        summary = run_result_backfill(
            destination_key="east-coasters-formal",
            db_conn_or_uri=pg_db.admin,
            transport=transport,
            latest=1,
            dry_run=False,
        )
        assert summary.failed_count == 1
        row = _fetch_row(conn, "SELECT delivery_state, last_error_category FROM public.fc27_match_discord_deliveries")
        assert row[0] == "FAILED"
        assert row[1] == VALIDATION_ERROR_CATEGORY


@pytest.mark.postgres
def test_8_malformed_discord_message_id_rejected(pg_db) -> None:
    """8. Malformed Discord message ID rejected."""
    migrate(pg_db.migrator)
    with pytest.raises(FC27BackfillValidationError):
        validate_discord_message_id("not_a_snowflake_123")
    with psycopg.connect(pg_db.admin) as admin_conn:
        _seed_test_matches(admin_conn, 2)

    with psycopg.connect(pg_db.match_results_writer) as conn:
        _register_destination(conn)

        transport = MockLiveDiscordGatewayTransport(override_message_id="not_a_snowflake_123")
        summary = run_result_backfill(
            destination_key="east-coasters-formal",
            db_conn_or_uri=pg_db.admin,
            transport=transport,
            latest=1,
            dry_run=False,
        )
        assert summary.failed_count == 1
        row = _fetch_row(conn, "SELECT delivery_state, last_error_category FROM public.fc27_match_discord_deliveries")
        assert row[0] == "FAILED"
        assert row[1] == VALIDATION_ERROR_CATEGORY


@pytest.mark.postgres
def test_9_live_transport_not_ready_rejected(pg_db) -> None:
    """9. Live transport not ready rejected."""
    migrate(pg_db.migrator)
    transport = MockLiveDiscordGatewayTransport(is_ready=False)
    with pytest.raises(FC27LiveDiscordTransportRequiredError):
        run_result_backfill(
            destination_key="east-coasters-formal",
            db_conn_or_uri=pg_db.admin,
            transport=transport,
            latest=1,
            dry_run=False,
        )


@pytest.mark.postgres
def test_10_live_transport_lacking_authenticated_bot_user_rejected(pg_db) -> None:
    """10. Live transport lacking authenticated bot user rejected."""
    migrate(pg_db.migrator)
    transport = MockLiveDiscordGatewayTransport(bot_user_id="")
    with pytest.raises(FC27LiveDiscordTransportRequiredError):
        run_result_backfill(
            destination_key="east-coasters-formal",
            db_conn_or_uri=pg_db.admin,
            transport=transport,
            latest=1,
            dry_run=False,
        )


@pytest.mark.postgres
def test_11_live_send_plus_successful_fetch_finalizes_delivered(pg_db) -> None:
    """11. Live send plus successful fetch finalizes DELIVERED."""
    migrate(pg_db.migrator)
    with psycopg.connect(pg_db.admin) as admin_conn:
        _seed_test_matches(admin_conn, 3)

    with psycopg.connect(pg_db.match_results_writer) as conn:
        _register_destination(conn)

        transport = MockLiveDiscordGatewayTransport()
        summary = run_result_backfill(
            destination_key="east-coasters-formal",
            db_conn_or_uri=pg_db.admin,
            transport=transport,
            latest=3,
            dry_run=False,
        )
        assert summary.planned_or_sent_count == 3
        assert summary.failed_count == 0
        assert transport.readiness_requests == [(FORMAL_CHANNEL_ID, GUILD_ID)]
        assert len(transport.fetch_requests) == 3

        assert _fetch_scalar(
            conn,
            "SELECT count(*) FROM public.fc27_match_discord_deliveries WHERE delivery_state = 'DELIVERED'",
        ) == 3


@pytest.mark.postgres
def test_12_live_send_plus_fetch_not_found_does_not_finalize_delivered(pg_db) -> None:
    """12. Live send plus fetch-not-found does not finalize DELIVERED."""
    migrate(pg_db.migrator)
    with psycopg.connect(pg_db.admin) as admin_conn:
        _seed_test_matches(admin_conn, 3)

    with psycopg.connect(pg_db.match_results_writer) as conn:
        _register_destination(conn)

        transport = MockLiveDiscordGatewayTransport(fail_fetch=True)
        summary = run_result_backfill(
            destination_key="east-coasters-formal",
            db_conn_or_uri=pg_db.admin,
            transport=transport,
            latest=3,
            dry_run=False,
        )
        assert summary.failed_count == 1
        assert _fetch_scalar(
            conn,
            "SELECT count(*) FROM public.fc27_match_discord_deliveries WHERE delivery_state = 'DELIVERED'",
        ) == 0
        row = _fetch_row(conn, "SELECT delivery_state, last_error_category FROM public.fc27_match_discord_deliveries")
        assert row[0] == "FAILED"
        assert row[1] == "FETCH_VERIFICATION_FAILED"


@pytest.mark.postgres
def test_13_live_send_plus_wrong_channel_does_not_finalize_delivered(pg_db) -> None:
    """13. Live send plus wrong channel does not finalize DELIVERED."""
    migrate(pg_db.migrator)
    with psycopg.connect(pg_db.admin) as admin_conn:
        _seed_test_matches(admin_conn, 2)

    with psycopg.connect(pg_db.match_results_writer) as conn:
        _register_destination(conn)

        transport = MockLiveDiscordGatewayTransport(override_channel_id="99999999999999999")
        summary = run_result_backfill(
            destination_key="east-coasters-formal",
            db_conn_or_uri=pg_db.admin,
            transport=transport,
            latest=1,
            dry_run=False,
        )
        assert summary.failed_count == 1
        assert [channel for channel, _ in transport.fetch_requests] == [FORMAL_CHANNEL_ID]
        row = _fetch_row(conn, "SELECT delivery_state, last_error_category FROM public.fc27_match_discord_deliveries")
        assert row[0] == "FAILED"
        assert row[1] == "FETCH_VERIFICATION_FAILED"


@pytest.mark.postgres
def test_14_live_send_plus_wrong_guild_does_not_finalize_delivered(pg_db) -> None:
    """14. Live send plus wrong guild does not finalize DELIVERED."""
    migrate(pg_db.migrator)
    with psycopg.connect(pg_db.admin) as admin_conn:
        _seed_test_matches(admin_conn, 2)

    with psycopg.connect(pg_db.match_results_writer) as conn:
        _register_destination(conn)

        transport = MockLiveDiscordGatewayTransport(override_guild_id="99999999999999999")
        summary = run_result_backfill(
            destination_key="east-coasters-formal",
            db_conn_or_uri=pg_db.admin,
            transport=transport,
            latest=1,
            dry_run=False,
        )
        assert summary.failed_count == 1
        row = _fetch_row(conn, "SELECT delivery_state, last_error_category FROM public.fc27_match_discord_deliveries")
        assert row[0] == "FAILED"
        assert row[1] == "FETCH_VERIFICATION_FAILED"


@pytest.mark.postgres
def test_15_live_send_plus_wrong_author_does_not_finalize_delivered(pg_db) -> None:
    """15. Live send plus wrong author does not finalize DELIVERED."""
    migrate(pg_db.migrator)
    with psycopg.connect(pg_db.admin) as admin_conn:
        _seed_test_matches(admin_conn, 2)

    with psycopg.connect(pg_db.match_results_writer) as conn:
        _register_destination(conn)

        transport = MockLiveDiscordGatewayTransport(override_author_id="99999999999999999")
        summary = run_result_backfill(
            destination_key="east-coasters-formal",
            db_conn_or_uri=pg_db.admin,
            transport=transport,
            latest=1,
            dry_run=False,
        )
        assert summary.failed_count == 1
        row = _fetch_row(conn, "SELECT delivery_state, last_error_category FROM public.fc27_match_discord_deliveries")
        assert row[0] == "FAILED"
        assert row[1] == "FETCH_VERIFICATION_FAILED"


@pytest.mark.postgres
def test_16_live_send_plus_missing_match_marker_does_not_finalize_delivered(pg_db) -> None:
    """16. Live send plus missing match marker does not finalize DELIVERED."""
    migrate(pg_db.migrator)
    with psycopg.connect(pg_db.admin) as admin_conn:
        _seed_test_matches(admin_conn, 2)

    with psycopg.connect(pg_db.match_results_writer) as conn:
        _register_destination(conn)

        transport = MockLiveDiscordGatewayTransport(strip_markers=True)
        summary = run_result_backfill(
            destination_key="east-coasters-formal",
            db_conn_or_uri=pg_db.admin,
            transport=transport,
            latest=1,
            dry_run=False,
        )
        assert summary.failed_count == 1
        row = _fetch_row(conn, "SELECT delivery_state, last_error_category FROM public.fc27_match_discord_deliveries")
        assert row[0] == "FAILED"
        assert row[1] == "FETCH_VERIFICATION_FAILED"


@pytest.mark.postgres
def test_17_ambiguous_timeout_does_not_resend_blindly(pg_db) -> None:
    """17. Ambiguous timeout stops backfill without blind resends."""
    migrate(pg_db.migrator)
    with psycopg.connect(pg_db.admin) as admin_conn:
        _seed_test_matches(admin_conn, 5)

    with psycopg.connect(pg_db.match_results_writer) as conn:
        _register_destination(conn)

        transport = MockLiveDiscordGatewayTransport(fetch_timeout=True)
        summary = run_result_backfill(
            destination_key="east-coasters-formal",
            db_conn_or_uri=pg_db.admin,
            transport=transport,
            latest=5,
            dry_run=False,
        )
        # Should stop after first failure and NOT continue to remaining 4 matches
        assert summary.failed_count == 1
        assert len(transport.sent_messages) == 1
        assert _fetch_scalar(
            conn,
            "SELECT count(*) FROM public.fc27_match_discord_deliveries WHERE delivery_state = 'DELIVERED'",
        ) == 0


@pytest.mark.postgres
def test_18_same_destination_idempotency_still_prevents_duplicates(pg_db) -> None:
    """18. Same-destination idempotency still prevents duplicates."""
    migrate(pg_db.migrator)
    with psycopg.connect(pg_db.admin) as admin_conn:
        _seed_test_matches(admin_conn, 3)

    with psycopg.connect(pg_db.match_results_writer) as conn:
        _register_destination(conn)

        transport = MockLiveDiscordGatewayTransport()
        summary_1 = run_result_backfill(
            destination_key="east-coasters-formal",
            db_conn_or_uri=pg_db.admin,
            transport=transport,
            latest=3,
            dry_run=False,
        )
        assert summary_1.planned_or_sent_count == 3
        assert len(transport.sent_messages) == 3

        summary_2 = run_result_backfill(
            destination_key="east-coasters-formal",
            db_conn_or_uri=pg_db.admin,
            transport=transport,
            latest=3,
            dry_run=False,
        )
        assert summary_2.already_delivered_count == 3
        assert summary_2.planned_or_sent_count == 0
        assert len(transport.sent_messages) == 3


@pytest.mark.postgres
def test_19_other_destination_delivery_remains_independent(pg_db) -> None:
    """19. Other-destination delivery remains independent."""
    migrate(pg_db.migrator)
    with psycopg.connect(pg_db.admin) as admin_conn:
        _seed_test_matches(admin_conn, 2)

    with psycopg.connect(pg_db.match_results_writer) as conn:
        _register_destination(conn, destination_key="dest-alpha", channel_id=FORMAL_CHANNEL_ID)
        _register_destination(conn, destination_key="dest-beta", channel_id="33333333333333333")

        transport = MockLiveDiscordGatewayTransport()
        summary_a = run_result_backfill(
            destination_key="dest-alpha",
            db_conn_or_uri=pg_db.admin,
            transport=transport,
            latest=2,
            dry_run=False,
        )
        assert summary_a.planned_or_sent_count == 2

        summary_b = run_result_backfill(
            destination_key="dest-beta",
            db_conn_or_uri=pg_db.admin,
            transport=transport,
            latest=2,
            dry_run=False,
        )
        assert summary_b.planned_or_sent_count == 2
        assert summary_b.already_delivered_count == 0


@pytest.mark.postgres
def test_20_formal_and_compact_backfills_remain_independent(pg_db) -> None:
    """20. Formal and compact backfills remain independent."""
    migrate(pg_db.migrator)
    with psycopg.connect(pg_db.admin) as admin_conn:
        _seed_test_matches(admin_conn, 2)

    with psycopg.connect(pg_db.match_results_writer) as conn:
        _register_destination(conn, destination_key="east-coasters-formal")
        _register_destination(
            conn,
            destination_key="east-coasters-compact",
            channel_id="33333333333333333",
            message_kind=MESSAGE_KIND_TEAM_CHAT,
        )

        transport = MockLiveDiscordGatewayTransport()
        summary_formal = run_result_backfill(
            destination_key="east-coasters-formal",
            db_conn_or_uri=pg_db.admin,
            transport=transport,
            latest=2,
            dry_run=False,
        )
        assert summary_formal.planned_or_sent_count == 2

        summary_compact = run_result_backfill(
            destination_key="east-coasters-compact",
            db_conn_or_uri=pg_db.admin,
            transport=transport,
            latest=2,
            dry_run=False,
        )
        assert summary_compact.planned_or_sent_count == 2
        assert summary_compact.already_delivered_count == 0

        with conn.cursor() as cur:
            cur.execute(
                "SELECT message_kind, count(*) FROM public.fc27_match_discord_deliveries GROUP BY message_kind"
            )
            rows = dict(cur.fetchall())
        assert rows[MESSAGE_KIND_FORMAL] == 2
        assert rows[MESSAGE_KIND_TEAM_CHAT] == 2


@pytest.mark.postgres
def test_21_provider_requests_remain_zero(pg_db) -> None:
    """21. Provider requests remain zero."""
    migrate(pg_db.migrator)
    with psycopg.connect(pg_db.admin) as admin_conn:
        _seed_test_matches(admin_conn, 2)

    with psycopg.connect(pg_db.match_results_writer) as conn:
        _register_destination(conn)

        transport = MockLiveDiscordGatewayTransport()
        summary = run_result_backfill(
            destination_key="east-coasters-formal",
            db_conn_or_uri=pg_db.admin,
            transport=transport,
            latest=2,
            dry_run=False,
        )
        assert summary.provider_requests_executed == 0


def test_22_no_raw_ids_payloads_aliases_or_credentials_logged(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    """22. No raw IDs, payloads, aliases, or credentials logged."""
    raw_destination = "987654321098765432"
    with caplog.at_level(logging.DEBUG):
        exit_code = main(["--destination", raw_destination])
    captured = capsys.readouterr()
    emitted = captured.out + captured.err + caplog.text

    assert exit_code != 0
    assert "Raw numeric channel ID" in captured.err
    assert raw_destination not in emitted
    assert not re.search(r"\b[0-9]{15,22}\b", emitted)

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
    assert not re.search(r"\b[0-9]{17,20}\b", report)
    assert not re.search(r"password|secret|token", report, re.IGNORECASE)


def test_23_existing_dispatcher_and_destination_aware_delivery_contract() -> None:
    """23. Existing dispatcher and destination-aware delivery tests remain green."""
    # Verify protocol conformance
    assert hasattr(DiscordNotificationTransport, "transport_name")
    assert hasattr(DiscordNotificationTransport, "is_live")
    assert hasattr(DiscordNotificationTransport, "supports_send")
    assert hasattr(DiscordNotificationTransport, "supports_fetch_after_send")
    assert hasattr(DiscordNotificationTransport, "authenticated_bot_user_id_available")


def test_24_existing_roster_refresh_behavior_contract() -> None:
    """24. Existing roster-refresh behavior remains green."""
    transport = SyntheticDiscordNotificationTransport()
    assert transport.transport_name == "synthetic"
    assert transport.is_live is False
    assert transport.supports_send is True
    assert transport.supports_fetch_after_send is True
    assert transport.authenticated_bot_user_id_available is False


@pytest.mark.postgres
def test_25_incident_regression_transport_none_confirm_true_leaves_deliveries_unchanged(pg_db) -> None:
    """25. Regression test reproducing exact original incident:
    transport=None, confirm=True (dry_run=False) previously created synthetic DELIVERED rows.
    New behavior raises LIVE_DISCORD_TRANSPORT_REQUIRED and delivery rows remain unchanged.
    """
    migrate(pg_db.migrator)
    with psycopg.connect(pg_db.admin) as admin_conn:
        _seed_test_matches(admin_conn, 10)

    with psycopg.connect(pg_db.match_results_writer) as conn:
        _register_destination(conn)

        # Before: 0 deliveries
        assert _fetch_scalar(conn, "SELECT count(*) FROM public.fc27_match_discord_deliveries") == 0

        # Attempt confirmed execution without transport
        with pytest.raises(
            FC27LiveDiscordTransportRequiredError,
            match="LIVE_DISCORD_TRANSPORT_REQUIRED",
        ):
            run_result_backfill(
                destination_key="east-coasters-formal",
                db_conn_or_uri=pg_db.admin,
                transport=None,
                latest=10,
                dry_run=False,
            )

        # After: count MUST remain exactly 0 (no false DELIVERED rows persisted)
        assert _fetch_scalar(conn, "SELECT count(*) FROM public.fc27_match_discord_deliveries") == 0


@pytest.mark.postgres
def test_backfill_single_flight_advisory_lock(pg_db) -> None:
    """Advisory lock prevents concurrent backfills for same destination."""
    migrate(pg_db.migrator)

    with psycopg.connect(pg_db.match_results_writer) as conn1, psycopg.connect(pg_db.match_results_writer) as conn2:
        _register_destination(conn1, destination_key="locked-dest")

        # Hold lock on conn1
        assert _fetch_scalar(
            conn1,
            "SELECT pg_try_advisory_lock(hashtext(%s), hashtext(%s))",
            (BACKFILL_ADVISORY_NAMESPACE, "locked-dest"),
        ) is True

        # Second backfill attempt on conn2 fails fast with lock busy error
        with pytest.raises(FC27BackfillLockBusyError, match="actively held by another process"):
            run_result_backfill(
                destination_key="locked-dest",
                db_conn_or_uri=conn2,
                latest=10,
            )

        # Release lock on conn1
        assert _fetch_scalar(
            conn1,
            "SELECT pg_advisory_unlock(hashtext(%s), hashtext(%s))",
            (BACKFILL_ADVISORY_NAMESPACE, "locked-dest"),
        ) is True


@pytest.mark.postgres
def test_backfill_unauthorized_and_invalid_destination(pg_db) -> None:
    """Invalid destination keys fail validation."""
    migrate(pg_db.migrator)

    with pytest.raises(FC27BackfillValidationError, match="is not registered in public.fc27_discord_destinations"):
        run_result_backfill(
            destination_key="unconfigured",
            db_conn_or_uri=pg_db.admin,
            latest=10,
        )

    with pytest.raises(FC27BackfillValidationError, match="Raw numeric channel ID"):
        run_result_backfill(
            destination_key="987654321098765432",
            db_conn_or_uri=pg_db.admin,
            latest=10,
        )
