"""Regression tests for persisted EXPIRED backfill state and status redaction.

Migration 0024 replaces update-then-raise expiration with an update-and-return
contract so the EXPIRED transition commits before the application returns
TOKEN_EXPIRED. These tests use real PostgreSQL transactions and runtime roles.
"""

from __future__ import annotations

import re
import time
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from src.db_migrate import GRANTS_POLICY_VERSION, migrate, verify_postconditions
from src.fc27_discord_backfill_control import (
    FC27BackfillRequestRecord,
    FC27BackfillTokenExpiredError,
    confirm_and_execute_backfill,
    format_status_response,
    get_backfill_status,
    hash_actor_id,
    hash_confirmation_token,
    plan_backfill,
)
from src.fc27_discord_delivery import MESSAGE_KIND_FORMAL, upsert_destination
from tests.test_fc27_result_backfill import (
    MockLiveDiscordGatewayTransport,
    _seed_test_matches,
)

TEST_GUILD_ID = "11111111111111111"
OTHER_GUILD_ID = "11111111111111112"
TEST_FORMAL_CHANNEL_ID = "22222222222222222"
OPERATOR_USER_ID = "44444444444444444"
OTHER_USER_ID = "55555555555555555"
DESTINATION_KEY = "expiry-fix-dest"


def _config(ttl_seconds: int = 1, cooldown_seconds: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        backfill_confirm_ttl_seconds=ttl_seconds,
        backfill_cooldown_seconds=cooldown_seconds,
    )


def _seed(pg_db: Any) -> None:
    with psycopg.connect(pg_db.admin) as conn:
        upsert_destination(
            conn,
            destination_key=DESTINATION_KEY,
            guild_id=TEST_GUILD_ID,
            channel_id=TEST_FORMAL_CHANNEL_ID,
            message_kind=MESSAGE_KIND_FORMAL,
            allow_backfill=True,
        )
        conn.commit()
        _seed_test_matches(conn, count=3)


def _plan(pg_db: Any, *, ttl_seconds: int = 1, latest: int = 3):
    return plan_backfill(
        reader_conn_or_uri=pg_db.discord_reader,
        writer_conn_or_uri=pg_db.match_results_writer,
        actor_user_id=OPERATOR_USER_ID,
        guild_id=TEST_GUILD_ID,
        destination_key=DESTINATION_KEY,
        latest=latest,
        config=_config(ttl_seconds=ttl_seconds),
    )


def _wait_until_expired(pg_db: Any, request_id: str | None = None, timeout: float = 6.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with psycopg.connect(pg_db.admin) as conn:
            if request_id is None:
                expired = conn.execute(
                    "SELECT coalesce(bool_and(expires_at < clock_timestamp()), false) "
                    "FROM public.fc27_discord_backfill_requests"
                ).fetchone()[0]
            else:
                expired = conn.execute(
                    "SELECT expires_at < clock_timestamp() "
                    "FROM public.fc27_discord_backfill_requests WHERE request_id = %s",
                    (request_id,),
                ).fetchone()[0]
        if expired:
            return
        time.sleep(0.2)
    raise AssertionError("request did not expire within the test timeout")


def _request_row(pg_db: Any, request_id: str) -> dict[str, Any]:
    with psycopg.connect(pg_db.admin, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT * FROM public.fc27_discord_backfill_requests WHERE request_id = %s",
            (request_id,),
        ).fetchone()
    assert row is not None
    return dict(row)


def _count(pg_db: Any, sql: str) -> int:
    with psycopg.connect(pg_db.admin) as conn:
        return int(conn.execute(sql).fetchone()[0])


# ---------------------------------------------------------------------------
# Transaction-semantics reproduction and corrected contract
# ---------------------------------------------------------------------------


def test_update_then_raise_rolls_back_synthetic_probe(pg_db_migrated: Any) -> None:
    """Proves the legacy update-then-raise pattern cannot persist state."""
    with psycopg.connect(pg_db_migrated.admin, autocommit=True) as conn:
        conn.execute("CREATE TEMP TABLE expiry_rollback_probe (status text NOT NULL)")
        conn.execute("INSERT INTO expiry_rollback_probe (status) VALUES ('PLANNED')")
        conn.execute(
            """
            CREATE FUNCTION pg_temp.expiry_probe() RETURNS void
            LANGUAGE plpgsql AS $$
            BEGIN
                UPDATE expiry_rollback_probe SET status = 'EXPIRED';
                RAISE EXCEPTION 'TOKEN_EXPIRED';
            END;
            $$
            """
        )
        with pytest.raises(psycopg.errors.RaiseException) as exc:
            conn.execute("SELECT pg_temp.expiry_probe()")
        assert "TOKEN_EXPIRED" in str(exc.value)
        status = conn.execute("SELECT status FROM expiry_rollback_probe").fetchone()[0]
    assert status == "PLANNED"


def test_corrected_database_contract_persists_expired(pg_db_migrated: Any) -> None:
    """The corrected function returns EXPIRED and the transition commits."""
    _seed(pg_db_migrated)
    plan, _token = _plan(pg_db_migrated)
    _wait_until_expired(pg_db_migrated)

    row = _request_row(pg_db_migrated, plan.request_id)
    with psycopg.connect(pg_db_migrated.match_results_writer, autocommit=True) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM public.fc27_confirm_backfill_request(%s, %s, %s, %s)",
                (row["token_digest"], row["actor_digest"], TEST_GUILD_ID, row["plan_digest"]),
            )
            result = cur.fetchone()
    assert result is not None
    assert result["status"] == "EXPIRED"

    with psycopg.connect(pg_db_migrated.admin, autocommit=True) as conn:
        persisted = conn.execute(
            "SELECT status FROM public.fc27_discord_backfill_requests WHERE request_id = %s",
            (plan.request_id,),
        ).fetchone()[0]
    assert persisted == "EXPIRED"


def test_application_confirm_returns_token_expired_after_commit(pg_db_migrated: Any) -> None:
    """The app commits EXPIRED before raising the bounded TOKEN_EXPIRED outcome."""
    _seed(pg_db_migrated)
    plan, raw_token = _plan(pg_db_migrated)
    _wait_until_expired(pg_db_migrated)
    transport = MockLiveDiscordGatewayTransport()
    deliveries_before = _count(
        pg_db_migrated, "SELECT count(*) FROM public.fc27_match_discord_deliveries"
    )
    provider_before = _count(
        pg_db_migrated, "SELECT count(*) FROM public.fc27_provider_request_attempts"
    )

    with pytest.raises(FC27BackfillTokenExpiredError):
        confirm_and_execute_backfill(
            reader_conn_or_uri=pg_db_migrated.discord_reader,
            writer_conn_or_uri=pg_db_migrated.match_results_writer,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            confirmation_token=raw_token,
            transport=transport,
            config=_config(),
        )

    row = _request_row(pg_db_migrated, plan.request_id)
    assert row["status"] == "EXPIRED"
    assert row["confirmed_at"] is None
    assert row["completed_at"] is None
    assert row["delivered_count"] == 0
    assert transport.sent_messages == []
    assert _count(pg_db_migrated, "SELECT count(*) FROM public.fc27_match_discord_deliveries") == deliveries_before
    assert _count(pg_db_migrated, "SELECT count(*) FROM public.fc27_provider_request_attempts") == provider_before


def test_repeated_expired_confirmation_is_idempotent(pg_db_migrated: Any) -> None:
    """A second expired confirmation keeps one EXPIRED row and enters no live path."""
    _seed(pg_db_migrated)
    plan, raw_token = _plan(pg_db_migrated)
    _wait_until_expired(pg_db_migrated)
    transport = MockLiveDiscordGatewayTransport()

    for _ in range(2):
        with pytest.raises(FC27BackfillTokenExpiredError):
            confirm_and_execute_backfill(
                reader_conn_or_uri=pg_db_migrated.discord_reader,
                writer_conn_or_uri=pg_db_migrated.match_results_writer,
                actor_user_id=OPERATOR_USER_ID,
                guild_id=TEST_GUILD_ID,
                confirmation_token=raw_token,
                transport=transport,
                config=_config(),
            )

    assert _count(pg_db_migrated, "SELECT count(*) FROM public.fc27_discord_backfill_requests") == 1
    row = _request_row(pg_db_migrated, plan.request_id)
    assert row["status"] == "EXPIRED"
    assert _count(pg_db_migrated, "SELECT count(*) FROM public.fc27_discord_backfill_requests WHERE status='RUNNING'") == 0
    assert _count(pg_db_migrated, "SELECT count(*) FROM public.fc27_discord_backfill_requests WHERE status='COMPLETED'") == 0
    assert transport.sent_messages == []


def test_confirmed_request_is_not_expired_by_repeat(pg_db_migrated: Any) -> None:
    """The PLANNED-token expiry branch cannot convert an existing CONFIRMED request."""
    _seed(pg_db_migrated)
    plan, _token = _plan(pg_db_migrated)
    row = _request_row(pg_db_migrated, plan.request_id)

    with psycopg.connect(pg_db_migrated.match_results_writer, autocommit=True) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM public.fc27_confirm_backfill_request(%s, %s, %s, %s)",
                (row["token_digest"], row["actor_digest"], TEST_GUILD_ID, row["plan_digest"]),
            )
            confirmed = cur.fetchone()
    assert confirmed is not None and confirmed["status"] == "CONFIRMED"

    _wait_until_expired(pg_db_migrated)
    with psycopg.connect(pg_db_migrated.match_results_writer, autocommit=True) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM public.fc27_confirm_backfill_request(%s, %s, %s, %s)",
                (row["token_digest"], row["actor_digest"], TEST_GUILD_ID, row["plan_digest"]),
            )
            repeated = cur.fetchone()
    assert repeated is not None and repeated["status"] == "CONFIRMED"
    assert _request_row(pg_db_migrated, plan.request_id)["status"] == "CONFIRMED"


def test_terminal_state_remains_terminal(pg_db_migrated: Any) -> None:
    """FAILED/CANCELLED requests are not mutated by the expiry branch."""
    _seed(pg_db_migrated)
    plan, _token = _plan(pg_db_migrated)
    row = _request_row(pg_db_migrated, plan.request_id)
    with psycopg.connect(pg_db_migrated.admin, autocommit=True) as conn:
        conn.execute(
            "UPDATE public.fc27_discord_backfill_requests SET status='FAILED' WHERE request_id=%s",
            (plan.request_id,),
        )
    _wait_until_expired(pg_db_migrated)

    with psycopg.connect(pg_db_migrated.match_results_writer, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute(
                "SELECT * FROM public.fc27_confirm_backfill_request(%s, %s, %s, %s)",
                (row["token_digest"], row["actor_digest"], TEST_GUILD_ID, row["plan_digest"]),
            )
    assert _request_row(pg_db_migrated, plan.request_id)["status"] == "FAILED"


def test_overdue_reconciliation_expires_only_expired_rows(pg_db_migrated: Any) -> None:
    """The bounded reconciliation routine expires overdue rows and leaves valid rows alone."""
    _seed(pg_db_migrated)
    expired_plan, _ = _plan(pg_db_migrated, ttl_seconds=1)
    valid_plan, _ = plan_backfill(
        reader_conn_or_uri=pg_db_migrated.discord_reader,
        writer_conn_or_uri=pg_db_migrated.match_results_writer,
        actor_user_id=OTHER_USER_ID,
        guild_id=OTHER_GUILD_ID,
        destination_key=DESTINATION_KEY,
        latest=2,
        config=_config(ttl_seconds=900),
    )
    _wait_until_expired(pg_db_migrated, expired_plan.request_id)
    with psycopg.connect(pg_db_migrated.match_results_writer, autocommit=True) as conn:
        expired_count = conn.execute(
            "SELECT public.fc27_expire_overdue_backfill_requests()"
        ).fetchone()[0]
    assert expired_count == 1
    assert _request_row(pg_db_migrated, expired_plan.request_id)["status"] == "EXPIRED"
    assert _request_row(pg_db_migrated, valid_plan.request_id)["status"] == "PLANNED"

    with psycopg.connect(pg_db_migrated.match_results_writer, autocommit=True) as conn:
        expired_again = conn.execute(
            "SELECT public.fc27_expire_overdue_backfill_requests()"
        ).fetchone()[0]
    assert expired_again == 0


# ---------------------------------------------------------------------------
# Status redaction and cross-actor confidentiality
# ---------------------------------------------------------------------------


def test_status_response_omits_all_internal_identifiers(pg_db_migrated: Any) -> None:
    """Status rendering exposes only approved symbolic fields."""
    _seed(pg_db_migrated)
    plan, raw_token = _plan(pg_db_migrated, ttl_seconds=1)
    _wait_until_expired(pg_db_migrated)
    with psycopg.connect(pg_db_migrated.match_results_writer, autocommit=True) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM public.fc27_confirm_backfill_request(%s, %s, %s, %s)",
                (
                    hash_confirmation_token(raw_token),
                    hash_actor_id(OPERATOR_USER_ID),
                    TEST_GUILD_ID,
                    _request_row(pg_db_migrated, plan.request_id)["plan_digest"],
                ),
            )
            cur.fetchone()

    record = get_backfill_status(
        pg_db_migrated.discord_reader,
        guild_id=TEST_GUILD_ID,
        lookup=plan.request_id,
        actor_user_id=OPERATOR_USER_ID,
    )
    assert record is not None and record.status == "EXPIRED"
    rendered = format_status_response(record)

    row = _request_row(pg_db_migrated, plan.request_id)
    forbidden = [
        plan.request_id,
        row["token_digest"],
        row["actor_digest"],
        TEST_GUILD_ID,
        TEST_FORMAL_CHANNEL_ID,
        "plr_ecf_",
        "mref_ecf_",
        "postgres" + "ql://",
        "Request ID",
    ]
    for value in forbidden:
        assert value not in rendered
    assert not re.search(r"\b[a-f0-9]{64}\b", rendered)
    assert not re.search(r"\b\d{17,20}\b", rendered)

    for required in (
        "Status",
        "Destination",
        "Message Kind",
        "Latest Count",
        "Planned Count",
        "Delivered Count",
        "Skipped Count",
        "Failed Count",
        "Created At",
        "Expires At",
        "Confirmed At",
        "Completed At",
    ):
        assert required in rendered


@pytest.mark.parametrize(
    "status",
    ["PLANNED", "EXPIRED", "CONFIRMED", "RUNNING", "COMPLETED", "FAILED"],
)
def test_status_renderer_is_sanitized_for_every_state(status: str) -> None:
    record = FC27BackfillRequestRecord(
        request_id="request-identifier-sentinel",
        status=status,
        destination_key=DESTINATION_KEY,
        message_kind=MESSAGE_KIND_FORMAL,
        latest_count=3,
        planned_count=0,
        delivered_count=0,
        skipped_count=0,
        failed_count=0,
        last_error_category=None,
        created_at="2026-11-23 22:00:00+00:00",
        expires_at="2026-11-23 22:15:00+00:00",
        confirmed_at=None,
        completed_at=None,
    )
    rendered = format_status_response(record)
    assert record.request_id not in rendered
    assert "Request ID" not in rendered


def test_cross_actor_and_cross_guild_status_lookup_denied(pg_db_migrated: Any) -> None:
    """Actor/guild-scoped status never discloses another operator's request."""
    _seed(pg_db_migrated)
    plan, raw_token = _plan(pg_db_migrated, ttl_seconds=900)
    token_digest = hash_confirmation_token(raw_token)

    own = get_backfill_status(
        pg_db_migrated.discord_reader,
        guild_id=TEST_GUILD_ID,
        lookup=plan.request_id,
        actor_user_id=OPERATOR_USER_ID,
    )
    assert own is not None

    assert (
        get_backfill_status(
            pg_db_migrated.discord_reader,
            guild_id=TEST_GUILD_ID,
            lookup=plan.request_id,
            actor_user_id=OTHER_USER_ID,
        )
        is None
    )
    assert (
        get_backfill_status(
            pg_db_migrated.discord_reader,
            guild_id=TEST_GUILD_ID,
            lookup=token_digest,
            actor_user_id=OTHER_USER_ID,
        )
        is None
    )
    assert (
        get_backfill_status(
            pg_db_migrated.discord_reader,
            guild_id=TEST_GUILD_ID,
            lookup="",
            actor_user_id=OTHER_USER_ID,
        )
        is None
    )
    assert (
        get_backfill_status(
            pg_db_migrated.discord_reader,
            guild_id=OTHER_GUILD_ID,
            lookup=plan.request_id,
            actor_user_id=OPERATOR_USER_ID,
        )
        is None
    )
    assert _request_row(pg_db_migrated, plan.request_id)["status"] == "PLANNED"


# ---------------------------------------------------------------------------
# Migration, grants, and least privilege
# ---------------------------------------------------------------------------


def test_fresh_migration_through_0024(pg_db: Any) -> None:
    applied = migrate(pg_db.migrator)
    assert applied[-1] == "0024_fc27_backfill_expiration_state_fix.sql"
    assert len(applied) == 24
    assert GRANTS_POLICY_VERSION == 25
    with psycopg.connect(pg_db.admin) as conn:
        assert verify_postconditions(conn) == []
        max_version = conn.execute("SELECT max(version) FROM public.schema_migrations").fetchone()[0]
        assert str(max_version).startswith("0024")
        policy = conn.execute(
            "SELECT version FROM public.schema_policy_versions WHERE policy='grants' ORDER BY version DESC LIMIT 1"
        ).fetchone()[0]
        assert policy == 25


def test_upgrade_from_0023_applies_single_correction(pg_db: Any) -> None:
    applied_to_0023 = migrate(pg_db.migrator, target="0023")
    assert len(applied_to_0023) == 23
    assert applied_to_0023[-1] == "0023_fc27_discord_backfill_control.sql"
    with psycopg.connect(pg_db.admin) as conn:
        assert verify_postconditions(conn) == []

    applied_correction = migrate(pg_db.migrator)
    assert applied_correction == ["0024_fc27_backfill_expiration_state_fix.sql"]
    assert migrate(pg_db.migrator) == []
    with psycopg.connect(pg_db.admin) as conn:
        assert verify_postconditions(conn) == []


def test_expiry_function_grants_are_narrow(pg_db_migrated: Any) -> None:
    with psycopg.connect(pg_db_migrated.admin) as conn:
        row = conn.execute(
            """
            SELECT p.prosecdef, p.proowner::regrole::text, p.proconfig
              FROM pg_proc p
              JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE n.nspname='public' AND p.proname='fc27_expire_overdue_backfill_requests'
            """
        ).fetchone()
        assert row is not None
        secdef, owner, config = row
        assert secdef is True
        assert owner == "fc27_owner"
        assert "search_path=pg_catalog, pg_temp" in " ".join(config or [])

        checks = {
            "public": False,
            "fc27_app": False,
            "fc27_dispatch": False,
            "fc27_relay": False,
            "fc27_poll_writer": False,
            "fc27_player_dev_writer": False,
            "fc27_discord_reader": False,
            "fc27_match_results_writer": True,
        }
        for role, expected in checks.items():
            actual = conn.execute(
                "SELECT has_function_privilege(%s, 'public.fc27_expire_overdue_backfill_requests()', 'EXECUTE')",
                (role,),
            ).fetchone()[0]
            assert bool(actual) is expected


def test_confirm_function_preserves_secdef_and_ownership(pg_db_migrated: Any) -> None:
    with psycopg.connect(pg_db_migrated.admin) as conn:
        row = conn.execute(
            """
            SELECT p.prosecdef, p.proowner::regrole::text, p.proconfig
              FROM pg_proc p
              JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE n.nspname='public'
               AND p.proname='fc27_confirm_backfill_request'
            """
        ).fetchone()
        assert row is not None
        secdef, owner, config = row
        assert secdef is True
        assert owner == "fc27_owner"
        assert "search_path=pg_catalog, pg_temp" in " ".join(config or [])
        assert conn.execute(
            "SELECT has_function_privilege('fc27_discord_reader', "
            "'public.fc27_confirm_backfill_request(text,text,text,text)', 'EXECUTE')"
        ).fetchone()[0] is False
