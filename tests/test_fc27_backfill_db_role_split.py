"""Regression tests for split reader/writer database roles in secured backfill.

Incident: `/backfillresults plan` failed with `permission denied for table fc27_matches`
because a single `fc27_match_results_writer` connection performed both the read-only
dry-run inventory and the narrow request-creation procedure.

These tests use the real least-privilege runtime roles in disposable PostgreSQL; the
permission boundary is never mocked away.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from src.fc27_discord_backfill_control import (
    FC27BackfillControlError,
    FC27BackfillLockBusyError,
    FC27BackfillReaderConnectionRequiredError,
    FC27BackfillWriterConnectionRequiredError,
    compute_plan_digest,
    confirm_and_execute_backfill,
    get_backfill_status,
    hash_confirmation_token,
    plan_backfill,
    resolve_and_validate_destination,
)
from src.fc27_discord_bot import (
    DISCORD_PROVIDER_REQUESTS,
    FC27DiscordBotConfig,
    FC27DiscordBotService,
)
from src.fc27_discord_delivery import MESSAGE_KIND_FORMAL, upsert_destination
from src.fc27_match_results import (
    EAST_COASTERS_CLUB_ID,
    EAST_COASTERS_GAME_VERSION,
    EAST_COASTERS_PLATFORM,
)
from src.fc27_result_backfill import (
    _discover_latest_persisted_matches,
    run_result_backfill,
)
from tests.test_fc27_result_backfill import (
    MockLiveDiscordGatewayTransport,
    _seed_test_matches,
)

TEST_GUILD_ID = "11111111111111111"
OTHER_GUILD_ID = "11111111111111112"
TEST_FORMAL_CHANNEL_ID = "22222222222222222"
OPERATOR_USER_ID = "44444444444444444"
OTHER_USER_ID = "55555555555555555"

ADMIN_ROLE_ID = "77777777777777771"
BACKFILL_OP_ROLE_ID = "77777777777777772"
IDENTITY_OP_ROLE_ID = "77777777777777773"
MEMBER_ROLE_ID = "77777777777777774"
SVC_ACCT_ROLE_ID = "77777777777777775"
MANAGER_ROLE_ID = "77777777777777776"
CAPTAIN_ROLE_ID = "77777777777777777"

DESTINATION_KEY = "formal-backfill-dest"

CREATE_PROC = (
    "public.fc27_create_backfill_request("
    "text, text, text, text, text, integer, integer, text, integer, integer)"
)


def _make_bot_config(
    *,
    backfill_enabled: bool = True,
    backfill_op_roles: frozenset[str] = frozenset({BACKFILL_OP_ROLE_ID}),
) -> FC27DiscordBotConfig:
    return FC27DiscordBotConfig(
        enabled=True,
        application_id="99999999999999999",
        allowed_guild_ids=frozenset({TEST_GUILD_ID}),
        allowed_channel_ids=frozenset({TEST_FORMAL_CHANNEL_ID}),
        allowed_role_ids=frozenset(
            {ADMIN_ROLE_ID, BACKFILL_OP_ROLE_ID, IDENTITY_OP_ROLE_ID, MEMBER_ROLE_ID}
        ),
        admin_role_ids=frozenset({ADMIN_ROLE_ID, SVC_ACCT_ROLE_ID}),
        identity_operator_role_ids=frozenset({IDENTITY_OP_ROLE_ID}),
        backfill_enabled=backfill_enabled,
        backfill_max_matches=25,
        backfill_confirm_ttl_seconds=900,
        backfill_cooldown_seconds=0,
        backfill_operator_role_ids=backfill_op_roles,
    ).model_copy(
        update={
            "manager_role_ids": frozenset({MANAGER_ROLE_ID}),
            "captain_role_ids": frozenset({CAPTAIN_ROLE_ID}),
        }
    )


def _seed(pg_db_migrated: Any, *, match_count: int = 3) -> None:
    with psycopg.connect(pg_db_migrated.admin) as conn:
        upsert_destination(
            conn,
            destination_key=DESTINATION_KEY,
            guild_id=TEST_GUILD_ID,
            channel_id=TEST_FORMAL_CHANNEL_ID,
            message_kind=MESSAGE_KIND_FORMAL,
            allow_backfill=True,
        )
        conn.commit()
        _seed_test_matches(conn, count=match_count)


def _scalar(uri: str, sql: str, params: tuple[Any, ...] = ()) -> Any:
    with psycopg.connect(uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()[0]


def _request_rows(admin_uri: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(admin_uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT request_id, token_digest, status, planned_count, plan_digest
                  FROM public.fc27_discord_backfill_requests
                 ORDER BY created_at
                """
            )
            return cur.fetchall()


def _count(admin_uri: str, table: str) -> int:
    return int(_scalar(admin_uri, f"SELECT count(*) FROM public.{table}"))


@pytest.fixture()
def split_uris(pg_db_migrated: Any) -> Iterator[tuple[str, str, str]]:
    _seed(pg_db_migrated)
    yield pg_db_migrated.admin, pg_db_migrated.discord_reader, pg_db_migrated.match_results_writer


# ---------------------------------------------------------------------------
# Role matrix: catalog privileges and real permission denials
# ---------------------------------------------------------------------------


def test_reader_writer_catalog_privilege_matrix(pg_db_migrated: Any) -> None:
    """Reader can read match tables and cannot create requests; writer is the inverse."""
    admin_uri = pg_db_migrated.admin
    with psycopg.connect(admin_uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT has_table_privilege('fc27_discord_reader', 'public.fc27_matches', 'SELECT')"
            )
            assert cur.fetchone()[0] is True
            cur.execute(
                "SELECT has_table_privilege('fc27_discord_reader', 'public.fc27_match_discord_deliveries', 'SELECT')"
            )
            assert cur.fetchone()[0] is True
            cur.execute(
                "SELECT has_table_privilege('fc27_discord_reader', 'public.fc27_discord_backfill_requests', 'INSERT')"
            )
            assert cur.fetchone()[0] is False
            cur.execute("SELECT has_function_privilege('fc27_discord_reader', %s, 'EXECUTE')", (CREATE_PROC,))
            assert cur.fetchone()[0] is False

            cur.execute(
                "SELECT has_table_privilege('fc27_match_results_writer', 'public.fc27_matches', 'SELECT')"
            )
            assert cur.fetchone()[0] is False
            cur.execute(
                "SELECT has_table_privilege('fc27_match_results_writer', 'public.fc27_discord_backfill_requests', 'INSERT')"
            )
            assert cur.fetchone()[0] is False
            cur.execute(
                "SELECT has_function_privilege('fc27_match_results_writer', %s, 'EXECUTE')", (CREATE_PROC,)
            )
            assert cur.fetchone()[0] is True


def test_roles_are_denied_at_runtime_not_only_in_catalog(pg_db_migrated: Any) -> None:
    """Actual statements fail with insufficient privilege for the wrong role."""
    with psycopg.connect(pg_db_migrated.match_results_writer, autocommit=True) as writer:
        with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc:
            writer.execute("SELECT 1 FROM public.fc27_matches LIMIT 1")
        assert "fc27_matches" in str(exc.value)

    with psycopg.connect(pg_db_migrated.discord_reader, autocommit=True) as reader:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            reader.execute(
                "SELECT public.fc27_create_backfill_request(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                ("a" * 64, "b" * 64, TEST_GUILD_ID, DESTINATION_KEY, MESSAGE_KIND_FORMAL, 1, 1, "digest", 900, 0),
            )


# ---------------------------------------------------------------------------
# Defect reproduction and missing-path contract
# ---------------------------------------------------------------------------


def test_writer_only_plan_reproduces_match_permission_denied(
    pg_db_migrated: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy single writer path reproduces the incident before token generation."""
    _seed(pg_db_migrated)
    monkeypatch.setattr(
        "src.fc27_discord_backfill_control.secrets.token_urlsafe",
        lambda _n: pytest.fail("token must not be generated before the dry run succeeds"),
    )

    with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc:
        plan_backfill(
            pg_db_migrated.match_results_writer,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key=DESTINATION_KEY,
            config=_make_bot_config(),
        )
    assert "permission denied for table fc27_matches" in str(exc.value)
    assert _request_rows(pg_db_migrated.admin) == []


def test_reader_only_plan_requires_writer_path(
    pg_db_migrated: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reader-only split planning fails closed before token generation."""
    _seed(pg_db_migrated)
    monkeypatch.setattr(
        "src.fc27_discord_backfill_control.secrets.token_urlsafe",
        lambda _n: pytest.fail("token must not be generated when the writer path is absent"),
    )

    with pytest.raises(FC27BackfillWriterConnectionRequiredError) as exc:
        plan_backfill(
            reader_conn_or_uri=pg_db_migrated.discord_reader,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key=DESTINATION_KEY,
            config=_make_bot_config(),
        )
    assert str(exc.value) == "BACKFILL_WRITER_CONNECTION_REQUIRED"
    assert _request_rows(pg_db_migrated.admin) == []


def test_missing_reader_fails_before_token_generation(
    pg_db_migrated: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writer-only split planning fails closed because the reader path is absent."""
    _seed(pg_db_migrated)
    monkeypatch.setattr(
        "src.fc27_discord_backfill_control.secrets.token_urlsafe",
        lambda _n: pytest.fail("token must not be generated when the reader path is absent"),
    )

    with pytest.raises(FC27BackfillReaderConnectionRequiredError) as exc:
        plan_backfill(
            writer_conn_or_uri=pg_db_migrated.match_results_writer,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key=DESTINATION_KEY,
            config=_make_bot_config(),
        )
    assert str(exc.value) == "BACKFILL_READER_CONNECTION_REQUIRED"
    assert _request_rows(pg_db_migrated.admin) == []


def test_missing_both_paths_fails_as_reader_required(pg_db_migrated: Any) -> None:
    """No connection arguments fail closed with the bounded reader error."""
    with pytest.raises(FC27BackfillReaderConnectionRequiredError) as exc:
        plan_backfill(
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key=DESTINATION_KEY,
        )
    assert str(exc.value) == "BACKFILL_READER_CONNECTION_REQUIRED"


# ---------------------------------------------------------------------------
# Reader dry run and writer procedure against real roles
# ---------------------------------------------------------------------------


def test_reader_only_dry_run_succeeds(split_uris: tuple[str, str, str]) -> None:
    """fc27_discord_reader can execute the complete dry-run inventory."""
    _, reader_uri, _ = split_uris
    summary = run_result_backfill(
        destination_key=DESTINATION_KEY,
        db_conn_or_uri=reader_uri,
        latest=3,
        formal=True,
        dry_run=True,
    )
    assert summary.total_eligible_found == 3
    assert summary.planned_or_sent_count == 3
    assert summary.failed_count == 0
    assert summary.provider_requests_executed == 0


def test_writer_request_procedure_succeeds(split_uris: tuple[str, str, str]) -> None:
    """fc27_match_results_writer can execute the narrow request-creation procedure."""
    admin_uri, _, writer_uri = split_uris
    with psycopg.connect(writer_uri, autocommit=True) as writer:
        request_id = writer.execute(
            "SELECT public.fc27_create_backfill_request(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            ("a" * 64, "b" * 64, TEST_GUILD_ID, DESTINATION_KEY, MESSAGE_KIND_FORMAL, 1, 1, "digest", 900, 0),
        ).fetchone()[0]
    assert request_id is not None
    rows = _request_rows(admin_uri)
    assert len(rows) == 1
    assert rows[0][2] == "PLANNED"


# ---------------------------------------------------------------------------
# Successful split plan: durability, least privilege, zero side effects
# ---------------------------------------------------------------------------


def test_successful_split_plan_is_durable_and_least_privileged(
    split_uris: tuple[str, str, str],
) -> None:
    """Split planning creates exactly one PLANNED row, one digest, and zero deliveries."""
    admin_uri, reader_uri, writer_uri = split_uris
    before_deliveries = _count(admin_uri, "fc27_match_discord_deliveries")
    before_provider = _count(admin_uri, "fc27_provider_request_attempts")

    plan, raw_token = plan_backfill(
        reader_conn_or_uri=reader_uri,
        writer_conn_or_uri=writer_uri,
        actor_user_id=OPERATOR_USER_ID,
        guild_id=TEST_GUILD_ID,
        destination_key=DESTINATION_KEY,
        latest=3,
        config=_make_bot_config(),
    )

    rows = _request_rows(admin_uri)
    assert len(rows) == 1
    request_id, token_digest, status, planned_count, plan_digest = rows[0]
    assert str(request_id) == plan.request_id
    assert status == "PLANNED"
    assert planned_count == 3
    assert token_digest == hash_confirmation_token(raw_token)
    assert token_digest != raw_token
    assert len(token_digest) == 64
    assert raw_token not in "".join(str(value) for value in rows)
    assert plan.confirmation_token == raw_token

    with psycopg.connect(reader_uri, autocommit=True) as reader:
        dest = resolve_and_validate_destination(reader, DESTINATION_KEY)
        eligible = _discover_latest_persisted_matches(
            reader,
            club_id=EAST_COASTERS_CLUB_ID,
            platform=EAST_COASTERS_PLATFORM,
            game_version=EAST_COASTERS_GAME_VERSION,
            limit=3,
        )
    assert plan_digest == compute_plan_digest(
        dest.destination_key,
        dest.message_kind,
        3,
        eligible,
        destination_updated_at=dest.updated_at.isoformat() if dest.updated_at else None,
    )

    # Dry-run planning must not mutate deliveries, send messages, or hit a provider.
    assert _count(admin_uri, "fc27_match_discord_deliveries") == before_deliveries
    assert _count(admin_uri, "fc27_provider_request_attempts") == before_provider
    assert DISCORD_PROVIDER_REQUESTS == 0


def test_reader_dry_run_failure_produces_no_token_or_row(
    pg_db_migrated: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reader permission failure aborts before token generation and request creation."""
    _seed(pg_db_migrated)
    writer_uri = pg_db_migrated.match_results_writer
    monkeypatch.setattr(
        "src.fc27_discord_backfill_control.secrets.token_urlsafe",
        lambda _n: pytest.fail("token must not be generated when the reader dry run fails"),
    )

    # The writer role cannot read match tables, so it fails as the reader path.
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        plan_backfill(
            reader_conn_or_uri=writer_uri,
            writer_conn_or_uri=writer_uri,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key=DESTINATION_KEY,
            config=_make_bot_config(),
        )
    assert _request_rows(pg_db_migrated.admin) == []


def test_writer_failure_exposes_no_token_and_creates_no_row(
    pg_db_migrated: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A writer-path failure discards the minted token and leaves no request row."""
    _seed(pg_db_migrated)
    reader_uri = pg_db_migrated.discord_reader
    sentinel_token = "SENTINEL_RAW_TOKEN_VALUE"
    monkeypatch.setattr(
        "src.fc27_discord_backfill_control.secrets.token_urlsafe",
        lambda _n: sentinel_token,
    )

    # The reader role has no EXECUTE on the request-creation procedure.
    with pytest.raises(FC27BackfillControlError) as exc:
        plan_backfill(
            reader_conn_or_uri=reader_uri,
            writer_conn_or_uri=reader_uri,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key=DESTINATION_KEY,
            config=_make_bot_config(),
        )
    assert sentinel_token not in str(exc.value)
    assert _request_rows(pg_db_migrated.admin) == []


def test_procedure_failure_rolls_back_and_keeps_single_active_request(
    split_uris: tuple[str, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected procedure creates no partial row and no second token is exposed."""
    admin_uri, reader_uri, writer_uri = split_uris
    plan_backfill(
        reader_conn_or_uri=reader_uri,
        writer_conn_or_uri=writer_uri,
        actor_user_id=OPERATOR_USER_ID,
        guild_id=TEST_GUILD_ID,
        destination_key=DESTINATION_KEY,
        latest=2,
        config=_make_bot_config(),
    )
    sentinel_token = "SECOND_SENTINEL_TOKEN"
    monkeypatch.setattr(
        "src.fc27_discord_backfill_control.secrets.token_urlsafe",
        lambda _n: sentinel_token,
    )

    with pytest.raises(FC27BackfillLockBusyError) as exc:
        plan_backfill(
            reader_conn_or_uri=reader_uri,
            writer_conn_or_uri=writer_uri,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key=DESTINATION_KEY,
            latest=2,
            config=_make_bot_config(),
        )
    assert sentinel_token not in str(exc.value)
    assert len(_request_rows(admin_uri)) == 1


# ---------------------------------------------------------------------------
# Bot plan path: split resolution, response ambiguity, and authorization
# ---------------------------------------------------------------------------


def _split_service(
    reader_uri: str,
    writer_uri: str,
    *,
    config: FC27DiscordBotConfig | None = None,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> FC27DiscordBotService:
    svc = FC27DiscordBotService(
        config or _make_bot_config(),
        db_conn_or_uri=reader_uri,
        writer_db_conn_or_uri=writer_uri,
    )
    if monkeypatch is not None:
        monkeypatch.setattr(svc, "_resolve_readonly_conn_or_uri", lambda required=False: reader_uri)
        monkeypatch.setattr(svc, "_resolve_writer_conn_or_uri", lambda required=False: writer_uri)
    return svc


def test_bot_plan_path_resolves_split_roles(
    split_uris: tuple[str, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bot resolves reader and writer independently and plans successfully."""
    admin_uri, reader_uri, writer_uri = split_uris
    svc = _split_service(reader_uri, writer_uri, monkeypatch=monkeypatch)
    resp = svc.handle_backfill_plan(
        guild_id=TEST_GUILD_ID,
        channel_id=TEST_FORMAL_CHANNEL_ID,
        user_id=OPERATOR_USER_ID,
        user_role_ids=[BACKFILL_OP_ROLE_ID],
        destination=DESTINATION_KEY,
        latest=2,
    )
    assert resp.status == "OK"
    assert resp.ephemeral is True
    rows = _request_rows(admin_uri)
    assert len(rows) == 1
    assert rows[0][2] == "PLANNED"


def test_response_failure_after_commit_leaves_one_durable_plan(
    split_uris: tuple[str, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A post-commit formatting failure does not lose the row or mint a duplicate."""
    admin_uri, reader_uri, writer_uri = split_uris
    svc = _split_service(reader_uri, writer_uri, monkeypatch=monkeypatch)

    def _boom(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("response rendering failed")

    monkeypatch.setattr("src.fc27_discord_backfill_control.format_plan_response", _boom)
    resp = svc.handle_backfill_plan(
        guild_id=TEST_GUILD_ID,
        channel_id=TEST_FORMAL_CHANNEL_ID,
        user_id=OPERATOR_USER_ID,
        user_role_ids=[BACKFILL_OP_ROLE_ID],
        destination=DESTINATION_KEY,
        latest=2,
    )
    assert resp.status == "ERROR"
    assert "internal error" in resp.content.lower()
    rows = _request_rows(admin_uri)
    assert len(rows) == 1
    record = get_backfill_status(
        reader_uri,
        guild_id=TEST_GUILD_ID,
        lookup=str(rows[0][0]),
        actor_user_id=OPERATOR_USER_ID,
    )
    assert record is not None and record.status == "PLANNED"

    # A retry after ambiguity must not silently create a second active request.
    retry = svc.handle_backfill_plan(
        guild_id=TEST_GUILD_ID,
        channel_id=TEST_FORMAL_CHANNEL_ID,
        user_id=OPERATOR_USER_ID,
        user_role_ids=[BACKFILL_OP_ROLE_ID],
        destination=DESTINATION_KEY,
        latest=2,
    )
    assert retry.status == "ERROR"
    assert len(_request_rows(admin_uri)) == 1


@pytest.mark.parametrize(
    "roles",
    [
        [ADMIN_ROLE_ID],
        [SVC_ACCT_ROLE_ID],
        [MANAGER_ROLE_ID],
        [CAPTAIN_ROLE_ID],
        [IDENTITY_OP_ROLE_ID],
        [MEMBER_ROLE_ID],
        ["FC27 Backfill Operator"],
        [],
    ],
    ids=[
        "general_admin",
        "svc_acct_equivalent",
        "manager",
        "captain",
        "identity_operator",
        "allowed_role",
        "role_name_text",
        "no_roles",
    ],
)
def test_dedicated_only_authorization_holds_on_split_path(
    split_uris: tuple[str, str, str], monkeypatch: pytest.MonkeyPatch, roles: list[str]
) -> None:
    """Non-dedicated callers are denied before either split path is resolved."""
    admin_uri, reader_uri, writer_uri = split_uris
    svc = _split_service(reader_uri, writer_uri)

    def _must_not_resolve(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("database paths must not be resolved for denied callers")

    monkeypatch.setattr(svc, "_resolve_readonly_conn_or_uri", _must_not_resolve)
    monkeypatch.setattr(svc, "_resolve_writer_conn_or_uri", _must_not_resolve)
    resp = svc.handle_backfill_plan(
        guild_id=TEST_GUILD_ID,
        channel_id=TEST_FORMAL_CHANNEL_ID,
        user_id=OPERATOR_USER_ID,
        user_role_ids=roles,
        destination=DESTINATION_KEY,
        latest=2,
    )
    assert resp.status == "REJECTED_UNAUTHORIZED"
    assert resp.ephemeral is True
    assert _request_rows(admin_uri) == []


def test_feature_disabled_does_not_resolve_paths_or_register_command(
    split_uris: tuple[str, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disabled feature returns before path resolution and yields no side effects."""
    admin_uri, reader_uri, writer_uri = split_uris
    svc = _split_service(
        reader_uri,
        writer_uri,
        config=_make_bot_config(backfill_enabled=False),
        monkeypatch=monkeypatch,
    )

    def _must_not_resolve(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("database paths must not be resolved while disabled")

    monkeypatch.setattr(svc, "_resolve_readonly_conn_or_uri", _must_not_resolve)
    monkeypatch.setattr(svc, "_resolve_writer_conn_or_uri", _must_not_resolve)
    resp = svc.handle_backfill_plan(
        guild_id=TEST_GUILD_ID,
        channel_id=TEST_FORMAL_CHANNEL_ID,
        user_id=OPERATOR_USER_ID,
        user_role_ids=[BACKFILL_OP_ROLE_ID],
        destination=DESTINATION_KEY,
    )
    assert resp.status == "ERROR"
    assert "disabled" in resp.content.lower()
    assert _request_rows(admin_uri) == []


# ---------------------------------------------------------------------------
# Confirm and status paths
# ---------------------------------------------------------------------------


def test_confirm_path_uses_reader_and_writer_split(
    split_uris: tuple[str, str, str],
) -> None:
    """Confirmed execution reads inventory through the reader and mutates through the writer."""
    admin_uri, reader_uri, writer_uri = split_uris
    _, raw_token = plan_backfill(
        reader_conn_or_uri=reader_uri,
        writer_conn_or_uri=writer_uri,
        actor_user_id=OPERATOR_USER_ID,
        guild_id=TEST_GUILD_ID,
        destination_key=DESTINATION_KEY,
        latest=3,
        config=_make_bot_config(),
    )
    transport = MockLiveDiscordGatewayTransport()

    record = confirm_and_execute_backfill(
        reader_conn_or_uri=reader_uri,
        writer_conn_or_uri=writer_uri,
        actor_user_id=OPERATOR_USER_ID,
        guild_id=TEST_GUILD_ID,
        confirmation_token=raw_token,
        transport=transport,
        config=_make_bot_config(),
    )
    assert record.status == "COMPLETED"
    assert record.delivered_count == 3
    assert len(transport.sent_messages) == 3
    assert _count(admin_uri, "fc27_provider_request_attempts") == 0
    assert DISCORD_PROVIDER_REQUESTS == 0
    assert _count(admin_uri, "fc27_match_discord_deliveries") == 3


def test_confirm_with_reader_as_writer_fails_closed(
    split_uris: tuple[str, str, str],
) -> None:
    """The confirm path cannot use the reader role for writer procedures."""
    admin_uri, reader_uri, _ = split_uris
    _, raw_token = plan_backfill(
        reader_conn_or_uri=reader_uri,
        writer_conn_or_uri=split_uris[2],
        actor_user_id=OPERATOR_USER_ID,
        guild_id=TEST_GUILD_ID,
        destination_key=DESTINATION_KEY,
        latest=2,
        config=_make_bot_config(),
    )
    transport = MockLiveDiscordGatewayTransport()
    with pytest.raises((FC27BackfillControlError, psycopg.Error)):
        confirm_and_execute_backfill(
            reader_conn_or_uri=reader_uri,
            writer_conn_or_uri=reader_uri,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            confirmation_token=raw_token,
            transport=transport,
            config=_make_bot_config(),
        )
    assert transport.sent_messages == []
    assert _count(admin_uri, "fc27_match_discord_deliveries") == 0


def test_status_path_is_reader_only_and_actor_guild_scoped(
    split_uris: tuple[str, str, str],
) -> None:
    """Status uses the reader role and only exposes the issuing actor's guild-bound row."""
    admin_uri, reader_uri, writer_uri = split_uris
    plan, _ = plan_backfill(
        reader_conn_or_uri=reader_uri,
        writer_conn_or_uri=writer_uri,
        actor_user_id=OPERATOR_USER_ID,
        guild_id=TEST_GUILD_ID,
        destination_key=DESTINATION_KEY,
        latest=2,
        config=_make_bot_config(),
    )
    assert (
        get_backfill_status(
            reader_uri,
            guild_id=TEST_GUILD_ID,
            lookup=plan.request_id,
            actor_user_id=OPERATOR_USER_ID,
        )
        is not None
    )
    assert (
        get_backfill_status(
            reader_uri,
            guild_id=TEST_GUILD_ID,
            lookup=plan.request_id,
            actor_user_id=OTHER_USER_ID,
        )
        is None
    )
    assert (
        get_backfill_status(
            reader_uri,
            guild_id=OTHER_GUILD_ID,
            lookup=plan.request_id,
            actor_user_id=OPERATOR_USER_ID,
        )
        is None
    )
    assert len(_request_rows(admin_uri)) == 1
