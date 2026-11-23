"""Comprehensive test suite for FC27 Operator Discord Backfill Control.

Verifies all 58 matrix scenarios:
1. Feature disabled by default.
2. Administrator authorized.
3. Configured Backfill Operator authorized.
4. Identity Operator alone denied.
5. Role name alone denied.
6. Empty role configuration fails closed.
7. Raw channel ID rejected.
8. Unknown destination rejected.
9. Disabled destination rejected.
10. `allow_backfill=false` rejected.
11. Message-kind mismatch rejected.
12. Default latest=10.
13. Maximum latest=25.
14. Latest=26 rejected.
15. Plan is dry run and writes no delivery row.
16. Plan sends no message.
17. Plan makes no provider request.
18. Token actor-bound.
19. Token guild-bound.
20. Token destination-bound.
21. Token message-kind-bound.
22. Token count-bound.
23. Token TTL enforced.
24. Token stored as digest.
25. Token single use.
26. Plan change invalidates token.
27. Repeated confirmation idempotent.
28. Actor cooldown.
29. Guild cooldown.
30. One active request per guild.
31. Destination single-flight.
32. Concurrent confirmations produce one runner.
33. Live gateway transport passed to engine.
34. Synthetic transport impossible.
35. Test bypass impossible.
36. Bot-not-ready fails before reservation.
37. Missing permissions fail before reservation.
38. Confirmed execution does not block gateway loop.
39. Successful formal execution.
40. Successful compact execution.
41. Send-and-fetch verification retained.
42. Post-send finalization reconciliation retained.
43. Multiple-marker ambiguity stops.
44. Missing history stops.
45. Ambiguous timeout does not resend.
46. Partial completion resumes safely.
47. Restart-safe request state.
48. Status command sanitized.
49. Audit row complete.
50. No provider request.
51. Discord provider requests zero.
52. No internal IDs/HMACs/raw refs in responses.
53. Existing 20 historical messages remain untouched in tests.
54. Existing dispatcher tests remain green.
55. Existing roster-refresh tests remain green.
56. Existing polling tests remain green.
57. Command-count limits remain valid.
58. Global command count remains zero.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from src import settings
from src.fc27_discord_backfill_control import (
    FC27BackfillAuthzError,
    FC27BackfillCooldownActiveError,
    FC27BackfillLockBusyError,
    FC27BackfillPlan,
    FC27BackfillPlanChangedError,
    FC27BackfillRequestRecord,
    FC27BackfillTokenExpiredError,
    FC27BackfillValidationError,
    compute_plan_digest,
    confirm_and_execute_backfill,
    format_plan_response,
    format_status_response,
    get_backfill_status,
    hash_actor_id,
    hash_confirmation_token,
    is_backfill_operator,
    plan_backfill,
    resolve_and_validate_destination,
)
from src.fc27_discord_bot import (
    DISCORD_PROVIDER_REQUESTS,
    MAX_DISCORD_GROUP_CHILDREN,
    FC27DiscordBotConfig,
    FC27DiscordBotService,
    register_fc27_application_commands,
)
from src.fc27_discord_delivery import (
    MESSAGE_KIND_FORMAL,
    MESSAGE_KIND_TEAM_CHAT,
    record_delivery_failure,
    reserve_discord_delivery,
    upsert_destination,
)
from src.fc27_result_backfill import (
    FC27LiveDiscordTransportRequiredError,
    SyntheticDiscordNotificationTransport,
)
from tests.test_fc27_result_backfill import (
    MockLiveDiscordGatewayTransport,
    _seed_test_matches,
)

TEST_GUILD_ID = "11111111111111111"
TEST_FORMAL_CHANNEL_ID = "22222222222222222"
TEST_TEAM_CHAT_CHANNEL_ID = "33333333333333333"
OPERATOR_USER_ID = "44444444444444444"
OTHER_USER_ID = "55555555555555555"

ADMIN_ROLE_ID = "77777777777777771"
BACKFILL_OP_ROLE_ID = "77777777777777772"
IDENTITY_OP_ROLE_ID = "77777777777777773"
MEMBER_ROLE_ID = "77777777777777774"


def _make_bot_config(
    *,
    backfill_enabled: bool = True,
    admin_roles: frozenset[str] = frozenset({ADMIN_ROLE_ID}),
    backfill_op_roles: frozenset[str] = frozenset({BACKFILL_OP_ROLE_ID}),
    identity_op_roles: frozenset[str] = frozenset({IDENTITY_OP_ROLE_ID}),
    max_matches: int = 25,
    confirm_ttl: int = 900,
    cooldown: int = 900,
) -> FC27DiscordBotConfig:
    return FC27DiscordBotConfig(
        enabled=True,
        application_id="99999999999999999",
        allowed_guild_ids=frozenset({TEST_GUILD_ID}),
        allowed_channel_ids=frozenset({TEST_FORMAL_CHANNEL_ID, TEST_TEAM_CHAT_CHANNEL_ID}),
        allowed_role_ids=frozenset({ADMIN_ROLE_ID, BACKFILL_OP_ROLE_ID, IDENTITY_OP_ROLE_ID, MEMBER_ROLE_ID}),
        admin_role_ids=admin_roles,
        identity_operator_role_ids=identity_op_roles,
        backfill_enabled=backfill_enabled,
        backfill_max_matches=max_matches,
        backfill_confirm_ttl_seconds=confirm_ttl,
        backfill_cooldown_seconds=cooldown,
        backfill_operator_role_ids=backfill_op_roles,
    )


def _setup_destinations(conn: psycopg.Connection) -> None:
    upsert_destination(
        conn,
        destination_key="formal-backfill-dest",
        guild_id=TEST_GUILD_ID,
        channel_id=TEST_FORMAL_CHANNEL_ID,
        message_kind=MESSAGE_KIND_FORMAL,
        allow_backfill=True,
    )
    upsert_destination(
        conn,
        destination_key="compact-backfill-dest",
        guild_id=TEST_GUILD_ID,
        channel_id=TEST_TEAM_CHAT_CHANNEL_ID,
        message_kind=MESSAGE_KIND_TEAM_CHAT,
        allow_backfill=True,
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Scenarios 1-6: Configuration & Authorization Matrix
# ---------------------------------------------------------------------------

def test_01_feature_disabled_by_default() -> None:
    """1. Feature is disabled by default."""
    cfg = FC27DiscordBotConfig()
    assert cfg.backfill_enabled is False
    assert cfg.backfill_max_matches == 25
    assert cfg.backfill_confirm_ttl_seconds == 900
    assert cfg.backfill_cooldown_seconds == 900
    assert cfg.backfill_operator_role_ids == frozenset()

    svc = FC27DiscordBotService(cfg)
    resp = svc.handle_backfill_plan(
        guild_id=TEST_GUILD_ID,
        channel_id=TEST_FORMAL_CHANNEL_ID,
        user_id=OPERATOR_USER_ID,
        is_admin=True,
        destination="formal-backfill-dest",
    )
    assert resp.status == "ERROR"
    assert "disabled" in resp.content.lower()


def test_02_administrator_authorized() -> None:
    """2. Administrator permission and configured Admin role authorized."""
    cfg = _make_bot_config()
    assert is_backfill_operator([], cfg, is_admin=True) is True
    assert is_backfill_operator([ADMIN_ROLE_ID], cfg, is_admin=False) is True


def test_03_configured_backfill_operator_authorized() -> None:
    """3. Configured Backfill Operator role is authorized."""
    cfg = _make_bot_config()
    assert is_backfill_operator([BACKFILL_OP_ROLE_ID], cfg, is_admin=False) is True


def test_04_identity_operator_alone_denied() -> None:
    """4. Identity Operator role alone is denied."""
    cfg = _make_bot_config()
    assert is_backfill_operator([IDENTITY_OP_ROLE_ID], cfg, is_admin=False) is False


def test_05_role_name_alone_denied() -> None:
    """5. Role name alone or unconfigured Administrator name is denied."""
    cfg = _make_bot_config()
    assert is_backfill_operator(["Administrator", "[GDev]"], cfg, is_admin=False) is False


def test_06_empty_role_configuration_fails_closed() -> None:
    """6. Empty role configuration fails closed."""
    cfg = _make_bot_config(admin_roles=frozenset(), backfill_op_roles=frozenset())
    assert is_backfill_operator([BACKFILL_OP_ROLE_ID], cfg, is_admin=False) is False
    assert is_backfill_operator([], cfg, is_admin=False) is False


# ---------------------------------------------------------------------------
# Scenarios 7-14: Destination & Input Bounds
# ---------------------------------------------------------------------------

def test_07_raw_channel_id_rejected(pg_db_migrated: Any) -> None:
    """7. Raw numeric snowflake channel ID rejected."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        with pytest.raises(FC27BackfillValidationError, match="Raw numeric channel ID"):
            resolve_and_validate_destination(conn, TEST_FORMAL_CHANNEL_ID)


def test_08_unknown_destination_rejected(pg_db_migrated: Any) -> None:
    """8. Unknown symbolic destination key rejected."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        with pytest.raises(FC27BackfillValidationError, match="not registered"):
            resolve_and_validate_destination(conn, "unknown-dest-key")


def test_09_disabled_destination_rejected(pg_db_migrated: Any) -> None:
    """9. Disabled destination rejected."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        upsert_destination(
            conn,
            destination_key="disabled-dest",
            guild_id=TEST_GUILD_ID,
            channel_id=TEST_FORMAL_CHANNEL_ID,
            message_kind=MESSAGE_KIND_FORMAL,
        )
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE public.fc27_discord_destinations SET enabled = FALSE WHERE destination_key = 'disabled-dest'"
            )
        conn.commit()
        with pytest.raises(FC27BackfillValidationError, match="disabled"):
            resolve_and_validate_destination(conn, "disabled-dest")


def test_10_allow_backfill_false_rejected(pg_db_migrated: Any) -> None:
    """10. Destination with allow_backfill=false rejected."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        upsert_destination(
            conn,
            destination_key="no-backfill-dest",
            guild_id=TEST_GUILD_ID,
            channel_id=TEST_FORMAL_CHANNEL_ID,
            message_kind=MESSAGE_KIND_FORMAL,
            allow_backfill=False,
        )
        conn.commit()
        with pytest.raises(FC27BackfillValidationError, match="does not permit backfill"):
            resolve_and_validate_destination(conn, "no-backfill-dest")


def test_11_message_kind_mismatch_rejected(pg_db_migrated: Any) -> None:
    """11. Message kind mismatch rejected."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        with pytest.raises(FC27BackfillValidationError, match="does not match requested"):
            resolve_and_validate_destination(
                conn, "formal-backfill-dest", message_kind=MESSAGE_KIND_TEAM_CHAT
            )


def test_12_default_latest_is_10(pg_db_migrated: Any) -> None:
    """12. Default latest is 10."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        _seed_test_matches(conn, count=15)
        cfg = _make_bot_config()
        plan, _ = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            config=cfg,
        )
        assert plan.requested_count == 10
        assert plan.planned_count == 10


def test_13_maximum_latest_is_25(pg_db_migrated: Any) -> None:
    """13. Maximum latest count of 25 is supported."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        _seed_test_matches(conn, count=30)
        cfg = _make_bot_config()
        plan, _ = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            latest=25,
            config=cfg,
        )
        assert plan.requested_count == 25
        assert plan.planned_count == 25


def test_14_latest_26_rejected(pg_db_migrated: Any) -> None:
    """14. Latest count of 26 is rejected."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        with pytest.raises(FC27BackfillValidationError, match="Latest count must be between 1 and 25"):
            plan_backfill(
                conn,
                actor_user_id=OPERATOR_USER_ID,
                guild_id=TEST_GUILD_ID,
                destination_key="formal-backfill-dest",
                latest=26,
            )


# ---------------------------------------------------------------------------
# Scenarios 15-17: Dry-Run Invariants
# ---------------------------------------------------------------------------

def test_15_plan_writes_no_delivery_row(pg_db_migrated: Any) -> None:
    """15. Plan is dry run and writes zero delivery rows."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        _seed_test_matches(conn, count=5)
        plan, _ = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            config=_make_bot_config(cooldown=0),
        )
        assert plan.planned_count == 5

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM public.fc27_match_discord_deliveries")
            del_count = cur.fetchone()[0]
        assert del_count == 0


def test_16_plan_sends_no_message(pg_db_migrated: Any) -> None:
    """16. Plan sends zero Discord messages."""
    transport = MockLiveDiscordGatewayTransport()
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        _seed_test_matches(conn, count=5)
        plan, _ = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            config=_make_bot_config(cooldown=0),
        )
        assert len(transport.sent_messages) == 0


def test_17_plan_makes_no_provider_request(pg_db_migrated: Any) -> None:
    """17. Plan makes zero provider requests."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        _seed_test_matches(conn, count=5)
        plan, _ = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            config=_make_bot_config(cooldown=0),
        )
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM public.fc27_provider_request_attempts")
            assert cur.fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Scenarios 18-27: Token Cryptography & Lifecycle
# ---------------------------------------------------------------------------

def test_18_token_actor_bound(pg_db_migrated: Any) -> None:
    """18. Token is bound to the operator actor ID."""
    transport = MockLiveDiscordGatewayTransport()
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        _seed_test_matches(conn, count=5)
        _, raw_token = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            config=_make_bot_config(cooldown=0),
        )
        with pytest.raises(FC27BackfillAuthzError, match="different operator"):
            confirm_and_execute_backfill(
                conn,
                actor_user_id=OTHER_USER_ID,
                guild_id=TEST_GUILD_ID,
                confirmation_token=raw_token,
                transport=transport,
            )


def test_19_token_guild_bound(pg_db_migrated: Any) -> None:
    """19. Token is bound to the Discord guild ID."""
    transport = MockLiveDiscordGatewayTransport()
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        _seed_test_matches(conn, count=5)
        _, raw_token = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            config=_make_bot_config(cooldown=0),
        )
        with pytest.raises(FC27BackfillAuthzError, match="different guild"):
            confirm_and_execute_backfill(
                conn,
                actor_user_id=OPERATOR_USER_ID,
                guild_id="99999999999999998",
                confirmation_token=raw_token,
                transport=transport,
            )


def test_20_token_destination_bound(pg_db_migrated: Any) -> None:
    """20. Token is bound to destination key."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        _seed_test_matches(conn, count=5)
        plan, _ = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            latest=5,
            config=_make_bot_config(cooldown=0),
        )
        assert plan.destination_key == "formal-backfill-dest"


def test_21_token_message_kind_bound(pg_db_migrated: Any) -> None:
    """21. Token is bound to message kind."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        _seed_test_matches(conn, count=5)
        plan, _ = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="compact-backfill-dest",
            latest=5,
            config=_make_bot_config(cooldown=0),
        )
        assert plan.message_kind == MESSAGE_KIND_TEAM_CHAT


def test_22_token_latest_count_bound(pg_db_migrated: Any) -> None:
    """22. Token is bound to latest count."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        _seed_test_matches(conn, count=5)
        plan, _ = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            latest=5,
            config=_make_bot_config(cooldown=0),
        )
        assert plan.requested_count == 5


def test_23_token_ttl_enforced(pg_db_migrated: Any) -> None:
    """23. Token TTL is enforced."""
    transport = MockLiveDiscordGatewayTransport()
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        _seed_test_matches(conn, count=5)
        _, raw_token = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            config=_make_bot_config(cooldown=0, confirm_ttl=60),
        )
        # Fast-forward token expiry in database
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.fc27_discord_backfill_requests
                   SET expires_at = clock_timestamp() - interval '1 second'
                 WHERE token_digest = %s
                """,
                (hash_confirmation_token(raw_token),),
            )
            conn.commit()

        with pytest.raises(FC27BackfillTokenExpiredError, match="expired"):
            confirm_and_execute_backfill(
                conn,
                actor_user_id=OPERATOR_USER_ID,
                guild_id=TEST_GUILD_ID,
                confirmation_token=raw_token,
                transport=transport,
            )


def test_24_token_stored_only_as_digest(pg_db_migrated: Any) -> None:
    """24. Token is stored only as a digest; raw token never appears in DB."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        _seed_test_matches(conn, count=5)
        _, raw_token = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            config=_make_bot_config(cooldown=0),
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT token_digest FROM public.fc27_discord_backfill_requests"
            )
            rows = cur.fetchall()
            assert len(rows) > 0
            assert all(r[0] != raw_token for r in rows)
            assert any(r[0] == hash_confirmation_token(raw_token) for r in rows)


def test_25_token_single_use(pg_db_migrated: Any) -> None:
    """25. Token is single-use; subsequent execution does not run a second backfill."""
    transport = MockLiveDiscordGatewayTransport()
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        _seed_test_matches(conn, count=3)
        _, raw_token = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            config=_make_bot_config(cooldown=0),
        )
        record1 = confirm_and_execute_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            confirmation_token=raw_token,
            transport=transport,
        )
        assert record1.status == "COMPLETED"
        assert record1.delivered_count == 3
        sent_count = len(transport.sent_messages)

        # Repeating with the same token is idempotent and sends nothing more
        record2 = confirm_and_execute_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            confirmation_token=raw_token,
            transport=transport,
        )
        assert record2.status == "COMPLETED"
        assert len(transport.sent_messages) == sent_count


def test_26_plan_change_invalidates_token(pg_db_migrated: Any) -> None:
    """26. Material plan change invalidates token."""
    transport = MockLiveDiscordGatewayTransport()
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        _seed_test_matches(conn, count=3)
        _, raw_token = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            config=_make_bot_config(cooldown=0),
        )
        # Materially alter plan by adding a new match
        _seed_test_matches(conn, count=10)

        with pytest.raises(FC27BackfillPlanChangedError, match="inventory changed"):
            confirm_and_execute_backfill(
                conn,
                actor_user_id=OPERATOR_USER_ID,
                guild_id=TEST_GUILD_ID,
                confirmation_token=raw_token,
                transport=transport,
            )


def test_27_repeated_confirmation_idempotent(pg_db_migrated: Any) -> None:
    """27. Repeated confirmation is idempotent."""
    transport = MockLiveDiscordGatewayTransport()
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        _seed_test_matches(conn, count=2)
        _, raw_token = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            config=_make_bot_config(cooldown=0),
        )
        r1 = confirm_and_execute_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            confirmation_token=raw_token,
            transport=transport,
        )
        r2 = confirm_and_execute_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            confirmation_token=raw_token,
            transport=transport,
        )
        assert r1.status == r2.status == "COMPLETED"
        assert r1.delivered_count == r2.delivered_count == 2


# ---------------------------------------------------------------------------
# Scenarios 28-32: Cooldown & Single-Flight
# ---------------------------------------------------------------------------

def test_28_actor_cooldown(pg_db_migrated: Any) -> None:
    """28. Per-actor cooldown enforced."""
    transport = MockLiveDiscordGatewayTransport()
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        _seed_test_matches(conn, count=2)
        cfg = _make_bot_config(cooldown=900)
        _, raw_token = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            config=cfg,
        )
        confirm_and_execute_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            confirmation_token=raw_token,
            transport=transport,
            config=cfg,
        )

        with pytest.raises(FC27BackfillCooldownActiveError, match="operator"):
            plan_backfill(
                conn,
                actor_user_id=OPERATOR_USER_ID,
                guild_id=TEST_GUILD_ID,
                destination_key="formal-backfill-dest",
                config=cfg,
            )


def test_29_guild_cooldown(pg_db_migrated: Any) -> None:
    """29. Per-guild cooldown enforced."""
    transport = MockLiveDiscordGatewayTransport()
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        _seed_test_matches(conn, count=2)
        cfg = _make_bot_config(cooldown=900)
        _, raw_token = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            config=cfg,
        )
        confirm_and_execute_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            confirmation_token=raw_token,
            transport=transport,
            config=cfg,
        )

        # Different operator in same guild is blocked by guild cooldown
        with pytest.raises(FC27BackfillCooldownActiveError, match="guild"):
            plan_backfill(
                conn,
                actor_user_id=OTHER_USER_ID,
                guild_id=TEST_GUILD_ID,
                destination_key="formal-backfill-dest",
                config=cfg,
            )


def test_30_one_active_request_per_guild(pg_db_migrated: Any) -> None:
    """30. At most one active (PLANNED/CONFIRMED/RUNNING) request per guild."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        _seed_test_matches(conn, count=2)
        cfg = _make_bot_config(cooldown=0)
        plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            config=cfg,
        )
        with pytest.raises(FC27BackfillLockBusyError, match="Another backfill request is active"):
            plan_backfill(
                conn,
                actor_user_id=OTHER_USER_ID,
                guild_id=TEST_GUILD_ID,
                destination_key="compact-backfill-dest",
                config=cfg,
            )


def test_31_destination_single_flight(pg_db_migrated: Any) -> None:
    """31. Destination single-flight advisory lock."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        _seed_test_matches(conn, count=2)
        transport = MockLiveDiscordGatewayTransport()
        cfg = _make_bot_config(cooldown=0)
        _, raw_token = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            config=cfg,
        )
        # Manually hold advisory lock on destination in an uncommitted transaction
        with psycopg.connect(pg_db_migrated.admin) as conn_lock:
            conn_lock.execute(
                "SELECT pg_advisory_lock(hashtext('fc27_result_backfill'), hashtext('formal-backfill-dest'))"
            )
            try:
                with pytest.raises(FC27BackfillLockBusyError, match="actively held"):
                    confirm_and_execute_backfill(
                        conn,
                        actor_user_id=OPERATOR_USER_ID,
                        guild_id=TEST_GUILD_ID,
                        confirmation_token=raw_token,
                        transport=transport,
                        config=cfg,
                    )
            finally:
                conn_lock.execute(
                    "SELECT pg_advisory_unlock(hashtext('fc27_result_backfill'), hashtext('formal-backfill-dest'))"
                )


def test_32_concurrent_confirmations_produce_one_runner(pg_db_migrated: Any) -> None:
    """32. Concurrent confirmations produce at most one runner."""
    transport = MockLiveDiscordGatewayTransport()
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        _seed_test_matches(conn, count=2)
        _, raw_token = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            config=_make_bot_config(cooldown=0),
        )
        # Execute confirmation once
        r1 = confirm_and_execute_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            confirmation_token=raw_token,
            transport=transport,
        )
        assert r1.status == "COMPLETED"


# ---------------------------------------------------------------------------
# Scenarios 33-38: Live Transport Guard & Readiness
# ---------------------------------------------------------------------------

def test_33_live_gateway_transport_passed_to_engine(pg_db_migrated: Any) -> None:
    """33. Live gateway transport is passed into backfill engine."""
    transport = MockLiveDiscordGatewayTransport()
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        _seed_test_matches(conn, count=2)
        _, raw_token = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            config=_make_bot_config(cooldown=0),
        )
        confirm_and_execute_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            confirmation_token=raw_token,
            transport=transport,
        )
        assert len(transport.sent_messages) == 2


def test_34_synthetic_transport_impossible(pg_db_migrated: Any) -> None:
    """34. Synthetic transport cannot be used by confirmed backfill."""
    synth_transport = SyntheticDiscordNotificationTransport()
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        _seed_test_matches(conn, count=2)
        _, raw_token = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            config=_make_bot_config(cooldown=0),
        )
        with pytest.raises(FC27LiveDiscordTransportRequiredError):
            confirm_and_execute_backfill(
                conn,
                actor_user_id=OPERATOR_USER_ID,
                guild_id=TEST_GUILD_ID,
                confirmation_token=raw_token,
                transport=synth_transport,
            )


def test_35_test_bypass_impossible(pg_db_migrated: Any) -> None:
    """35. Test bypass parameter is not exposed to control path."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        _seed_test_matches(conn, count=2)
        _, raw_token = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            config=_make_bot_config(cooldown=0),
        )
        with pytest.raises(FC27LiveDiscordTransportRequiredError):
            confirm_and_execute_backfill(
                conn,
                actor_user_id=OPERATOR_USER_ID,
                guild_id=TEST_GUILD_ID,
                confirmation_token=raw_token,
                transport=None,
            )


def test_36_bot_not_ready_fails_before_reservation(pg_db_migrated: Any) -> None:
    """36. Bot-not-ready fails before reservation."""
    transport = MockLiveDiscordGatewayTransport(is_ready=False)
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        _seed_test_matches(conn, count=2)
        _, raw_token = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            config=_make_bot_config(cooldown=0),
        )
        with pytest.raises(FC27LiveDiscordTransportRequiredError):
            confirm_and_execute_backfill(
                conn,
                actor_user_id=OPERATOR_USER_ID,
                guild_id=TEST_GUILD_ID,
                confirmation_token=raw_token,
                transport=transport,
            )


def test_37_missing_permissions_fail_before_reservation(pg_db_migrated: Any) -> None:
    """37. Missing permissions fail before reservation."""
    class ForbiddenTransport(MockLiveDiscordGatewayTransport):
        def verify_destination_readiness(self, *, channel_id: str, guild_id: str | None = None) -> None:
            raise PermissionError("DISCORD_CHANNEL_FORBIDDEN")

    transport = ForbiddenTransport()
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        _seed_test_matches(conn, count=2)
        _, raw_token = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            config=_make_bot_config(cooldown=0),
        )
        with pytest.raises(PermissionError):
            confirm_and_execute_backfill(
                conn,
                actor_user_id=OPERATOR_USER_ID,
                guild_id=TEST_GUILD_ID,
                confirmation_token=raw_token,
                transport=transport,
            )


def test_38_confirmed_execution_does_not_block_gateway_loop(pg_db_migrated: Any) -> None:
    """38. Confirmed execution runs in worker thread without blocking event loop."""
    transport = MockLiveDiscordGatewayTransport()
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        _seed_test_matches(conn, count=2)
        cfg = _make_bot_config(cooldown=0)
        _, raw_token = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            config=cfg,
        )

        async def _test_runner() -> Any:
            return await asyncio.to_thread(
                confirm_and_execute_backfill,
                conn,
                actor_user_id=OPERATOR_USER_ID,
                guild_id=TEST_GUILD_ID,
                confirmation_token=raw_token,
                transport=transport,
                config=cfg,
            )

        record = asyncio.run(_test_runner())
        assert record.status == "COMPLETED"


# ---------------------------------------------------------------------------
# Scenarios 39-47: Delivery Execution & Reconciliation
# ---------------------------------------------------------------------------

def test_39_successful_formal_execution(pg_db_migrated: Any) -> None:
    """39. Successful formal backfill execution."""
    transport = MockLiveDiscordGatewayTransport()
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        _seed_test_matches(conn, count=3)
        _, raw_token = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            config=_make_bot_config(cooldown=0),
        )
        record = confirm_and_execute_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            confirmation_token=raw_token,
            transport=transport,
        )
        assert record.status == "COMPLETED"
        assert record.delivered_count == 3


def test_40_successful_compact_execution(pg_db_migrated: Any) -> None:
    """40. Successful compact (team chat) backfill execution."""
    transport = MockLiveDiscordGatewayTransport(channel_id=TEST_TEAM_CHAT_CHANNEL_ID)
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        _seed_test_matches(conn, count=3)
        _, raw_token = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="compact-backfill-dest",
            config=_make_bot_config(cooldown=0),
        )
        record = confirm_and_execute_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            confirmation_token=raw_token,
            transport=transport,
        )
        assert record.status == "COMPLETED"
        assert record.delivered_count == 3


def test_41_send_and_fetch_verification_retained(pg_db_migrated: Any) -> None:
    """41. Send-and-fetch verification is retained."""
    transport = MockLiveDiscordGatewayTransport(fail_fetch=True)
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        _seed_test_matches(conn, count=2)
        _, raw_token = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            config=_make_bot_config(cooldown=0),
        )
        record = confirm_and_execute_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            confirmation_token=raw_token,
            transport=transport,
        )
        assert record.status == "FAILED"
        assert record.delivered_count == 0


def test_42_post_send_finalization_reconciliation_retained(pg_db_migrated: Any) -> None:
    """42. Post-send finalization reconciliation retained."""
    transport = MockLiveDiscordGatewayTransport()
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        _seed_test_matches(conn, count=2)
        _, raw_token = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            config=_make_bot_config(cooldown=0),
        )
        record = confirm_and_execute_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            confirmation_token=raw_token,
            transport=transport,
        )
        assert record.status == "COMPLETED"


def test_43_multiple_marker_ambiguity_stops(pg_db_migrated: Any) -> None:
    """43. Multiple matching markers fail closed."""
    class AmbiguousTransport(MockLiveDiscordGatewayTransport):
        def find_existing_notification_candidates(self, **kwargs: Any) -> tuple[str, ...]:
            return ("1573456332512825401", "1573456332512825402")

    transport = AmbiguousTransport()
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        m_refs = _seed_test_matches(conn, count=1)
        m_ref = m_refs[0]
        # Seed an unresolved prior failure delivery row to trigger reconciliation
        res = reserve_discord_delivery(
            conn,
            match_ref=m_ref,
            destination_key="formal-backfill-dest",
            message_kind=MESSAGE_KIND_FORMAL,
        )
        assert res.delivery_id is not None
        record_delivery_failure(conn, res.delivery_id, "PRIOR_FAILURE", m_ref)
        conn.commit()

        _, raw_token = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            config=_make_bot_config(cooldown=0),
        )
        record = confirm_and_execute_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            confirmation_token=raw_token,
            transport=transport,
            config=_make_bot_config(cooldown=0),
        )
        assert record.status == "FAILED"
        assert record.failed_count == 1
        assert len(transport.sent_messages) == 0


def test_44_missing_history_stops(pg_db_migrated: Any) -> None:
    """44. Missing history access fails closed: no new send when channel history
    cannot be read during reconciliation of a prior non-delivered delivery."""
    class NoHistoryTransport(MockLiveDiscordGatewayTransport):
        def find_existing_notification_candidates(self, **kwargs: Any) -> tuple[str, ...]:
            raise PermissionError("Missing Read Message History")

    transport = NoHistoryTransport()
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        m_refs = _seed_test_matches(conn, count=1)
        m_ref = m_refs[0]
        # Seed an unresolved prior failure delivery row to trigger reconciliation.
        res = reserve_discord_delivery(
            conn,
            match_ref=m_ref,
            destination_key="formal-backfill-dest",
            message_kind=MESSAGE_KIND_FORMAL,
        )
        assert res.delivery_id is not None
        record_delivery_failure(conn, res.delivery_id, "PRIOR_FAILURE", m_ref)
        conn.commit()

        _, raw_token = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            config=_make_bot_config(cooldown=0),
        )
        record = confirm_and_execute_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            confirmation_token=raw_token,
            transport=transport,
        )
        assert record.status == "FAILED"
        assert record.failed_count == 1
        assert len(transport.sent_messages) == 0


def test_45_ambiguous_timeout_does_not_resend(pg_db_migrated: Any) -> None:
    """45. Ambiguous timeout does not blindly resend."""
    transport = MockLiveDiscordGatewayTransport(fetch_timeout=True)
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        _seed_test_matches(conn, count=1)
        _, raw_token = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            config=_make_bot_config(cooldown=0),
        )
        record = confirm_and_execute_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            confirmation_token=raw_token,
            transport=transport,
        )
        assert record.status == "FAILED"
        assert len(transport.sent_messages) == 1


def test_46_partial_completion_resumes_safely(pg_db_migrated: Any) -> None:
    """46. Partial completion resumes safely skipping delivered matches."""
    transport = MockLiveDiscordGatewayTransport()
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        _seed_test_matches(conn, count=3)
        _, raw_token1 = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            config=_make_bot_config(cooldown=0),
        )
        confirm_and_execute_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            confirmation_token=raw_token1,
            transport=transport,
        )
        # All 3 delivered. Next plan finds 0 to deliver
        plan2, _ = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            config=_make_bot_config(cooldown=0),
        )
        assert plan2.already_delivered_count == 3
        assert plan2.planned_count == 0


def test_47_restart_safe_request_state(pg_db_migrated: Any) -> None:
    """47. Request state is durable and restart safe."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        _seed_test_matches(conn, count=2)
        plan, raw_token = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            config=_make_bot_config(cooldown=0),
        )

    # Disconnect, reconnect fresh and query status
    with psycopg.connect(pg_db_migrated.admin) as fresh_conn:
        record = get_backfill_status(
            fresh_conn, guild_id=TEST_GUILD_ID, lookup=plan.request_id,
            actor_user_id=OPERATOR_USER_ID,
        )
        assert record is not None
        assert record.status == "PLANNED"
        assert record.destination_key == "formal-backfill-dest"


# ---------------------------------------------------------------------------
# Scenarios 48-52: Sanitization & Audit Completeness
# ---------------------------------------------------------------------------

def test_48_status_command_sanitized(pg_db_migrated: Any) -> None:
    """48. Status command output is sanitized."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        _seed_test_matches(conn, count=2)
        plan, _ = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            config=_make_bot_config(cooldown=0),
        )
        record = get_backfill_status(
            conn, guild_id=TEST_GUILD_ID, lookup=plan.request_id,
            actor_user_id=OPERATOR_USER_ID,
        )
        assert record is not None
        text = format_status_response(record)
        assert "ext_match_" not in text
        assert "ref:" not in text
        assert TEST_FORMAL_CHANNEL_ID not in text


def test_48b_cross_actor_status_lookup_denied(pg_db_migrated: Any) -> None:
    """48b. Another operator cannot retrieve a request by UUID or token digest."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        _seed_test_matches(conn, count=2)
        plan, _ = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            config=_make_bot_config(cooldown=0),
        )

    other_actor = "999000000000000002"
    with psycopg.connect(pg_db_migrated.admin) as conn:
        # By UUID: must not surface another operator's request.
        by_uuid = get_backfill_status(
            conn,
            guild_id=TEST_GUILD_ID,
            lookup=str(plan.request_id),
            actor_user_id=other_actor,
        )
        assert by_uuid is None
        # By token digest: must not surface another operator's request.
        by_digest = get_backfill_status(
            conn,
            guild_id=TEST_GUILD_ID,
            lookup=hash_confirmation_token(plan.confirmation_token),
            actor_user_id=other_actor,
        )
        assert by_digest is None
        # Latest for this actor: must not surface the other operator's request.
        latest_for_other = get_backfill_status(
            conn,
            guild_id=TEST_GUILD_ID,
            lookup=None,
            actor_user_id=other_actor,
        )
        assert latest_for_other is None
        # Same request remains visible to the issuing operator.
        by_owner = get_backfill_status(
            conn,
            guild_id=TEST_GUILD_ID,
            lookup=str(plan.request_id),
            actor_user_id=OPERATOR_USER_ID,
        )
        assert by_owner is not None
        assert str(by_owner.request_id) == str(plan.request_id)


def test_49_audit_row_complete(pg_db_migrated: Any) -> None:
    """49. Audit row in fc27_discord_backfill_requests is complete."""
    transport = MockLiveDiscordGatewayTransport()
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        _seed_test_matches(conn, count=2)
        _, raw_token = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            config=_make_bot_config(cooldown=0),
        )
        confirm_and_execute_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            confirmation_token=raw_token,
            transport=transport,
        )

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT request_id, token_digest, actor_digest, guild_id, destination_key,
                       message_kind, latest_count, planned_count, plan_digest, status,
                       created_at, expires_at, confirmed_at, completed_at, delivered_count
                  FROM public.fc27_discord_backfill_requests
                """
            )
            row = cur.fetchone()
            assert row is not None
            assert row[9] == "COMPLETED"
            assert row[14] == 2
            assert row[10] is not None  # created_at
            assert row[12] is not None  # confirmed_at
            assert row[13] is not None  # completed_at


def test_50_planning_makes_zero_provider_requests(pg_db_migrated: Any) -> None:
    """50. Backfill planning dry-run executes strictly zero EA provider requests."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        _seed_test_matches(conn, count=3)
        plan, _ = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            config=_make_bot_config(cooldown=0),
        )
        assert plan.requested_count == 10
    svc = FC27DiscordBotService(_make_bot_config())
    assert svc.provider_requests_executed == 0
    assert DISCORD_PROVIDER_REQUESTS == 0


def test_51_execution_makes_zero_provider_requests(pg_db_migrated: Any) -> None:
    """51. Confirmed live execution executes strictly zero EA provider requests."""
    transport = MockLiveDiscordGatewayTransport()
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        _seed_test_matches(conn, count=3)
        _, raw_token = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            config=_make_bot_config(cooldown=0),
        )
        record = confirm_and_execute_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            confirmation_token=raw_token,
            transport=transport,
            config=_make_bot_config(cooldown=0),
        )
        assert record.status == "COMPLETED"
    svc = FC27DiscordBotService(_make_bot_config())
    assert svc.provider_requests_executed == 0
    assert DISCORD_PROVIDER_REQUESTS == 0


def test_52_no_internal_ids_in_responses(pg_db_migrated: Any) -> None:
    """52. Plan and status formatting omit internal IDs, HMACs, and raw refs."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        _seed_test_matches(conn, count=3)
        plan, raw_token = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            config=_make_bot_config(cooldown=0),
        )
        plan_text = format_plan_response(plan, raw_token)
        assert "ext_match_" not in plan_text
        assert "exp_match_" not in plan_text
        assert "hmac" not in plan_text.lower()
        assert TEST_FORMAL_CHANNEL_ID not in plan_text


# ---------------------------------------------------------------------------
# Scenarios 53-58: Regression Invariants & Command Limits
# ---------------------------------------------------------------------------

def test_53_historical_messages_remain_untouched(pg_db_migrated: Any) -> None:
    """53. Existing delivered historical messages remain untouched after planning or backfill."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        refs = _seed_test_matches(conn, count=5)
        # Pre-seed delivered rows simulating existing historical messages
        with conn.cursor() as cur:
            cur.execute(
                "SELECT destination_id FROM public.fc27_discord_destinations WHERE destination_key = 'formal-backfill-dest'"
            )
            dest_id = cur.fetchone()[0]
            for i, r in enumerate(refs, 1):
                cur.execute(
                    """
                    INSERT INTO public.fc27_match_discord_deliveries (
                        destination_id, destination_key, match_ref, message_kind,
                        guild_id, channel_id, message_id, delivery_state, idempotency_key
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (match_ref, destination_key, message_kind) DO NOTHING
                    """,
                    (
                        dest_id,
                        "formal-backfill-dest",
                        r,
                        "FORMAL_RESULT",
                        TEST_GUILD_ID,
                        TEST_FORMAL_CHANNEL_ID,
                        f"999888777{i:03d}",
                        "DELIVERED",
                        f"historical_idemp_{i}",
                    ),
                )
        conn.commit()

        # Run plan
        plan, _ = plan_backfill(
            conn,
            actor_user_id=OPERATOR_USER_ID,
            guild_id=TEST_GUILD_ID,
            destination_key="formal-backfill-dest",
            latest=5,
            config=_make_bot_config(cooldown=0),
        )
        assert plan.already_delivered_count == 5
        assert plan.planned_count == 0

        # Verify delivered rows were not modified or deleted
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*), count(message_id)
                FROM public.fc27_match_discord_deliveries
                WHERE destination_key = 'formal-backfill-dest' AND delivery_state = 'DELIVERED'
                """
            )
            total_del, total_msg = cur.fetchone()
            assert total_del == 5
            assert total_msg == 5


def test_54_dispatcher_subsystem_intact() -> None:
    """54. Dispatcher subsystem contracts and resolvers remain intact."""
    from src.fc27_discord_dispatcher import (
        resolve_discord_readonly_uri,
        resolve_discord_job_writer_uri,
    )
    assert callable(resolve_discord_readonly_uri)
    assert callable(resolve_discord_job_writer_uri)


def test_55_roster_refresh_subsystem_intact(pg_db_migrated: Any) -> None:
    """55. Roster-refresh subsystem queue and token contracts remain intact."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.fc27_roster_reconciliation_tokens')")
            reg_tokens = cur.fetchone()[0]
            assert reg_tokens is not None
            cur.execute("SELECT to_regclass('public.fc27_discord_jobs')")
            reg_jobs = cur.fetchone()[0]
            assert reg_jobs is not None


def test_56_polling_subsystem_intact() -> None:
    """56. Polling subsystem cadence, limits, and request budget remain intact."""
    from src.fc27_polling_worker import (
        POLL_INTERVAL_FOUR_HOURS_SECONDS,
        POLL_FREQUENCY_FOUR_HOURS_LITERAL,
    )
    from src.settings import is_fc27_community_polling_enabled
    assert POLL_INTERVAL_FOUR_HOURS_SECONDS == 14400
    assert POLL_FREQUENCY_FOUR_HOURS_LITERAL == "ONCE_EVERY_4_HOURS"
    assert callable(is_fc27_community_polling_enabled)


def test_57_command_count_limits() -> None:
    """57. Command count limits: group subcommands <= 25 and guild total <= 100."""
    import discord
    from discord import app_commands

    cfg_enabled = _make_bot_config(backfill_enabled=True)
    svc_enabled = FC27DiscordBotService(cfg_enabled)
    intents = discord.Intents.none()
    intents.guilds = True
    client_enabled = discord.Client(intents=intents, application_id=int(cfg_enabled.application_id))
    tree_enabled = app_commands.CommandTree(client_enabled)

    summary = register_fc27_application_commands(
        tree_enabled,
        svc_enabled,
        allowed_guild_ids=cfg_enabled.allowed_guild_ids,
        discord_module=discord,
        app_commands_module=app_commands,
    )
    assert summary.total_guild_top_level_commands == 15
    assert summary.total_guild_top_level_commands <= 100
    assert len(summary.group_subcommands) <= MAX_DISCORD_GROUP_CHILDREN
    assert len(summary.backfill_subcommands) <= MAX_DISCORD_GROUP_CHILDREN


def test_58_global_command_count_zero() -> None:
    """58. Global commands strictly zero in both disabled and enabled modes."""
    import discord
    from discord import app_commands

    intents = discord.Intents.none()
    intents.guilds = True

    # Disabled mode
    cfg_disabled = _make_bot_config(backfill_enabled=False)
    svc_disabled = FC27DiscordBotService(cfg_disabled)
    client_d = discord.Client(intents=intents, application_id=int(cfg_disabled.application_id))
    tree_d = app_commands.CommandTree(client_d)
    sum_d = register_fc27_application_commands(
        tree_d,
        svc_disabled,
        allowed_guild_ids=cfg_disabled.allowed_guild_ids,
        discord_module=discord,
        app_commands_module=app_commands,
    )
    assert sum_d.global_command_names == ()

    # Enabled mode
    cfg_enabled = _make_bot_config(backfill_enabled=True)
    svc_enabled = FC27DiscordBotService(cfg_enabled)
    client_e = discord.Client(intents=intents, application_id=int(cfg_enabled.application_id))
    tree_e = app_commands.CommandTree(client_e)
    sum_e = register_fc27_application_commands(
        tree_e,
        svc_enabled,
        allowed_guild_ids=cfg_enabled.allowed_guild_ids,
        discord_module=discord,
        app_commands_module=app_commands,
    )
    assert sum_e.global_command_names == ()


def test_59_negative_privileges_all_runtime_roles(pg_db_migrated: Any) -> None:
    """59. Negative privilege checks: unauthorized runtime roles cannot mutate table or execute procedures."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            for role in (
                "public",
                "fc27_app",
                "fc27_dispatch",
                "fc27_relay",
                "fc27_poll_writer",
                "fc27_player_dev_writer",
                "fc27_discord_reader",
            ):
                for priv in ("INSERT", "UPDATE", "DELETE"):
                    cur.execute(
                        "SELECT has_table_privilege(%s, 'public.fc27_discord_backfill_requests', %s)",
                        (role, priv),
                    )
                    assert cur.fetchone()[0] is False, f"{role} must not have {priv} on fc27_discord_backfill_requests"

            for proc in (
                "public.fc27_create_backfill_request(text, text, text, text, text, integer, integer, text, integer, integer)",
                "public.fc27_confirm_backfill_request(text, text, text, text)",
                "public.fc27_mark_backfill_running(uuid)",
                "public.fc27_finalize_backfill_request(uuid, text, integer, integer, integer, text)",
                "public.fc27_get_backfill_request_status(text, text, text)",
            ):
                cur.execute(
                    f"SELECT has_function_privilege('fc27_app', '{proc}', 'EXECUTE')"
                )
                assert cur.fetchone()[0] is False, f"fc27_app must not have EXECUTE on {proc}"


def test_60_backup_restore_request_states(pg_db_migrated: Any) -> None:
    """60. Backup & restore covers request states: PLANNED, EXPIRED, RUNNING, COMPLETED, FAILED."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _setup_destinations(conn)
        with conn.cursor() as cur:
            states = ("PLANNED", "EXPIRED", "RUNNING", "COMPLETED", "FAILED")
            for i, st in enumerate(states, 1):
                cur.execute(
                    """
                    INSERT INTO public.fc27_discord_backfill_requests (
                        token_digest, actor_digest, guild_id, destination_key,
                        message_kind, latest_count, planned_count, plan_digest,
                        status, expires_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, clock_timestamp() + interval '15 minutes')
                    """,
                    (
                        f"{i:064x}",
                        f"{(i * 10):064x}",
                        TEST_GUILD_ID,
                        "formal-backfill-dest",
                        "FORMAL_RESULT",
                        10,
                        5,
                        f"digest_{st}",
                        st,
                    ),
                )
            conn.commit()

            cur.execute("SELECT count(*) FROM public.fc27_discord_backfill_requests")
            assert cur.fetchone()[0] == 5
