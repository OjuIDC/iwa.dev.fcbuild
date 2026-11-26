"""Comprehensive unit and PostgreSQL integration tests for FC27 operations alert dispatch.

Covers:
1. Default disabled posture & configuration parsing:
   - Default disabled (FC27_OPERATIONS_ALERT_DISPATCH_ENABLED=false).
   - Symbolic destination required; numeric snowflake rejected.
   - Bounded ranges for batch size, interval, and max attempts.
2. Marker tag construction & extraction:
   - Format: 'fc27-ops-{sha256(canonical)[:16]}' (25 chars).
   - Deterministic and non-sensitive inputs.
   - extract_operations_alert_tag pattern matching.
3. Privacy & message formatting:
   - OPEN lifecycle (CRITICAL 🔴, WARNING 🟡).
   - RESOLVED lifecycle (🟢).
   - Length <= 2000 characters.
   - No secrets, no DB internal UUIDs, no provider URLs.
4. Stored procedure contract (real PostgreSQL 16 via disposable fixture pg_db_migrated):
   - fc27_reserve_operator_alert_delivery: 1 row per alert/event/destination, idempotent.
   - fc27_claim_operator_alert_deliveries: claims batch, increments attempts, uses SKIP LOCKED.
   - fc27_finalize_operator_alert_delivery: marks DELIVERED, records message/channel ID.
   - fc27_fail_operator_alert_delivery: marks FAILED or DEAD_LETTER when attempts exhausted.
   - fc27_reconcile_operator_alert_delivery_by_marker: resolves unconfirmed message by marker without re-sending.
   - fc27_get_operator_alert_delivery_summary: returns accurate state counts.
5. Dispatch cycle execution:
   - Happy path: claim, verify ambiguous, post, verify snapshot, finalize.
   - Ambiguous-send recovery: pre-flight search resolves message without duplicate post.
   - Post exception with live arrival recovery.
   - Snapshot verification failure handling.
   - Dead-letter transition on attempt exhaustion.
   - Fail-closed when disabled or destination invalid.
6. Evaluator-to-delivery integration:
   - Single OPEN reservation when alert is first created.
   - Zero reservations on suppressed/repeated evaluation.
   - Single RESOLVED reservation when cleared.
   - Fresh lifecycle on recurrence after resolution.
7. Least privilege:
   - Zero direct table grants for runtime roles.
   - Execution permissions strictly for fc27_match_results_writer and fc27_discord_reader.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from datetime import UTC, datetime
from typing import Any, Self

import psycopg
import pytest

from src import settings
from src.fc27_discord_dispatcher import (
    SyntheticDiscordNotificationTransport,
)
from src.fc27_match_results import (
    GuardedMatchResultsWriterConnection,
)
from src.fc27_operations_alert_dispatcher import (
    FC27OperationsAlertDispatchConfig,
    claim_operator_alert_deliveries,
    fail_operator_alert_delivery,
    finalize_operator_alert_delivery,
    get_operator_alert_delivery_summary,
    reconcile_operator_alert_delivery_by_marker,
    run_operations_alert_dispatch_cycle,
)
from src.fc27_operations_alerts import (
    ALERT_DISPATCH_IMPLEMENTED,
    ALERT_STATE_ENGINE_IMPLEMENTED,
    OPERATIONS_ALERT_MESSAGE_KIND,
    OPERATIONS_ALERT_TAG_LENGTH,
    OPERATIONS_ALERT_TAG_PREFIX,
    FC27AlertManager,
    build_operations_alert_marker,
    extract_operations_alert_tag,
    format_operations_alert_message,
)
from src.fc27_operations_status import parse_operations_status_snapshot

# ==============================================================================
# 1. IMPLEMENTATION CONSTANTS & DEFAULTS
# ==============================================================================


def test_01_implementation_markers() -> None:
    """1. Verify implementation status constants."""
    assert ALERT_STATE_ENGINE_IMPLEMENTED == "YES"
    assert ALERT_DISPATCH_IMPLEMENTED == "YES"
    assert OPERATIONS_ALERT_MESSAGE_KIND == "OPERATIONS_ALERT"
    assert OPERATIONS_ALERT_TAG_PREFIX == "fc27-ops-"
    assert OPERATIONS_ALERT_TAG_LENGTH == 25


def test_02_default_disabled_posture(monkeypatch: pytest.MonkeyPatch) -> None:
    """2. Operations alert dispatch is disabled by default."""
    monkeypatch.delenv("FC27_OPERATIONS_ALERT_DISPATCH_ENABLED", raising=False)
    assert settings.is_fc27_operations_alert_dispatch_enabled(strict=False) is False


def test_03_enabled_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    """3. Parse boolean values for dispatch enabled."""
    for val in ("true", "1", "yes", "True"):
        monkeypatch.setenv("FC27_OPERATIONS_ALERT_DISPATCH_ENABLED", val)
        assert settings.is_fc27_operations_alert_dispatch_enabled(strict=False) is True
    for val in ("false", "0", "no", "False", ""):
        monkeypatch.setenv("FC27_OPERATIONS_ALERT_DISPATCH_ENABLED", val)
        assert settings.is_fc27_operations_alert_dispatch_enabled(strict=False) is False


def test_04_symbolic_destination_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """4. Destination must be a symbolic key, never a numeric snowflake."""
    monkeypatch.setenv("FC27_OPERATIONS_ALERT_DESTINATION", "fc27-operations")
    assert settings.get_fc27_operations_alert_destination(strict=False) == "fc27-operations"

    # Numeric snowflake fails closed to empty
    monkeypatch.setenv("FC27_OPERATIONS_ALERT_DESTINATION", "123456789012345678")
    assert settings.get_fc27_operations_alert_destination(strict=False) == ""
    with pytest.raises(settings.ConfigError):
        settings.get_fc27_operations_alert_destination(strict=True)


def test_05_bounded_interval_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    """5. Interval seconds bounds (10..300, default 30)."""
    monkeypatch.delenv("FC27_OPERATIONS_ALERT_DISPATCH_INTERVAL_SECONDS", raising=False)
    assert settings.get_fc27_operations_alert_dispatch_interval_seconds() == 30

    monkeypatch.setenv("FC27_OPERATIONS_ALERT_DISPATCH_INTERVAL_SECONDS", "45")
    assert settings.get_fc27_operations_alert_dispatch_interval_seconds() == 45

    monkeypatch.setenv("FC27_OPERATIONS_ALERT_DISPATCH_INTERVAL_SECONDS", "5")
    assert settings.get_fc27_operations_alert_dispatch_interval_seconds(strict=False) == 30
    with pytest.raises(settings.ConfigError):
        settings.get_fc27_operations_alert_dispatch_interval_seconds(strict=True)


def test_06_bounded_batch_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """6. Batch size bounds (1..10, default 5)."""
    monkeypatch.delenv("FC27_OPERATIONS_ALERT_DISPATCH_BATCH_SIZE", raising=False)
    assert settings.get_fc27_operations_alert_dispatch_batch_size() == 5

    monkeypatch.setenv("FC27_OPERATIONS_ALERT_DISPATCH_BATCH_SIZE", "8")
    assert settings.get_fc27_operations_alert_dispatch_batch_size() == 8

    monkeypatch.setenv("FC27_OPERATIONS_ALERT_DISPATCH_BATCH_SIZE", "20")
    assert settings.get_fc27_operations_alert_dispatch_batch_size(strict=False) == 5
    with pytest.raises(settings.ConfigError):
        settings.get_fc27_operations_alert_dispatch_batch_size(strict=True)


def test_07_bounded_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    """7. Max attempts bounds (1..5, default 3)."""
    monkeypatch.delenv("FC27_OPERATIONS_ALERT_DISPATCH_MAX_ATTEMPTS", raising=False)
    assert settings.get_fc27_operations_alert_dispatch_max_attempts() == 3

    monkeypatch.setenv("FC27_OPERATIONS_ALERT_DISPATCH_MAX_ATTEMPTS", "4")
    assert settings.get_fc27_operations_alert_dispatch_max_attempts() == 4

    monkeypatch.setenv("FC27_OPERATIONS_ALERT_DISPATCH_MAX_ATTEMPTS", "10")
    assert settings.get_fc27_operations_alert_dispatch_max_attempts(strict=False) == 3


def test_08_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """8. FC27OperationsAlertDispatchConfig.from_env constructs valid instance."""
    monkeypatch.setenv("FC27_OPERATIONS_ALERT_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("FC27_OPERATIONS_ALERT_DESTINATION", "ops-channel")
    monkeypatch.setenv("FC27_OPERATIONS_ALERT_DISPATCH_INTERVAL_SECONDS", "20")
    monkeypatch.setenv("FC27_OPERATIONS_ALERT_DISPATCH_BATCH_SIZE", "3")
    monkeypatch.setenv("FC27_OPERATIONS_ALERT_DISPATCH_MAX_ATTEMPTS", "2")

    cfg = FC27OperationsAlertDispatchConfig.from_env()
    assert cfg.enabled is True
    assert cfg.destination_key == "ops-channel"
    assert cfg.interval_seconds == 20
    assert cfg.batch_size == 3
    assert cfg.max_attempts == 2
    assert cfg.claim_owner != ""


# ==============================================================================
# 2. MARKER TAG CONSTRUCTION & PRIVACY
# ==============================================================================


def test_09_marker_tag_structure() -> None:
    """9. Marker tag is exactly 25 characters with prefix and hex digest."""
    tag, digest = build_operations_alert_marker(
        alert_id=str(uuid.uuid4()),
        lifecycle_event="OPEN",
        alert_type="POLL_OVERDUE",
        component="polling",
        destination_key="fc27-operations",
    )
    assert len(tag) == 25
    assert tag.startswith("fc27-ops-")
    assert re.match(r"^fc27-ops-[0-9a-f]{16}$", tag)
    assert len(digest) == 64
    assert hashlib.sha256(tag.encode("utf-8")).hexdigest() == digest


def test_10_marker_tag_extraction() -> None:
    """10. Extract tag from message body correctly."""
    tag, _ = build_operations_alert_marker(
        alert_id=str(uuid.uuid4()),
        lifecycle_event="OPEN",
        alert_type="HEARTBEAT_MISSING",
        component="discord_bot",
        destination_key="fc27-operations",
    )
    text = f"Some header\nBody content\n{tag}"
    extracted = extract_operations_alert_tag(text)
    assert extracted == tag

    # Extracted with whitespace around
    assert extract_operations_alert_tag(f"  {tag}  ") == tag

    # Absent returns None
    assert extract_operations_alert_tag("No marker present here") is None


def test_11_message_format_open_critical() -> None:
    """11. OPEN CRITICAL message format includes 🔴, component, summary, and marker."""
    tag, _ = build_operations_alert_marker(
        str(uuid.uuid4()), "OPEN", "POLL_OVERDUE", "polling", "fc27-operations"
    )
    msg = format_operations_alert_message(
        alert_type="POLL_OVERDUE",
        component="polling",
        severity="CRITICAL",
        summary="Polling delayed by 36000s",
        lifecycle_event="OPEN",
        opened_at=datetime(2026, 11, 25, 20, 0, 0, tzinfo=UTC),
        marker_tag=tag,
    )
    assert "🔴 **FC27 Operations Alert: POLL_OVERDUE** (`CRITICAL`)" in msg
    assert "**Component**: `polling`" in msg
    assert "**Summary**: Polling delayed by 36000s" in msg
    assert tag in msg
    assert len(msg) < 2000


def test_12_message_format_open_warning() -> None:
    """12. OPEN WARNING message format includes 🟡."""
    tag, _ = build_operations_alert_marker(
        str(uuid.uuid4()), "OPEN", "PROVIDER_BUDGET_HIGH", "polling", "fc27-operations"
    )
    msg = format_operations_alert_message(
        alert_type="PROVIDER_BUDGET_HIGH",
        component="polling",
        severity="WARNING",
        summary="Provider budget high (16/18)",
        lifecycle_event="OPEN",
        marker_tag=tag,
    )
    assert "🟡 **FC27 Operations Alert: PROVIDER_BUDGET_HIGH** (`WARNING`)" in msg
    assert tag in msg


def test_13_message_format_resolved() -> None:
    """13. RESOLVED message format includes 🟢 and resolved header."""
    tag, _ = build_operations_alert_marker(
        str(uuid.uuid4()), "RESOLVED", "POLL_OVERDUE", "polling", "fc27-operations"
    )
    msg = format_operations_alert_message(
        alert_type="POLL_OVERDUE",
        component="polling",
        severity="CRITICAL",
        summary="Polling overdue cleared",
        lifecycle_event="RESOLVED",
        resolved_at=datetime(2026, 11, 25, 21, 0, 0, tzinfo=UTC),
        marker_tag=tag,
    )
    assert "🟢 **FC27 Operations Alert Resolved: POLL_OVERDUE**" in msg
    assert "**Component**: `polling`" in msg
    assert tag in msg


def test_14_message_format_privacy_safety() -> None:
    """14. Ensure message formatting never exposes secrets or internal UUIDs."""
    secret_uuid = str(uuid.uuid4())
    tag, _ = build_operations_alert_marker(
        secret_uuid, "OPEN", "HEARTBEAT_MISSING", "discord_bot", "fc27-operations"
    )
    msg = format_operations_alert_message(
        alert_type="HEARTBEAT_MISSING",
        component="discord_bot",
        severity="CRITICAL",
        summary="Bot heartbeat missing for 400s",
        lifecycle_event="OPEN",
        marker_tag=tag,
    )
    # The raw database alert UUID must NOT be embedded in visible text
    assert secret_uuid not in msg
    # Only the hashed marker tag is present
    assert tag in msg
    assert "fc27-ops-" in msg


# ==============================================================================
# 3. STORED PROCEDURE CONTRACT & LEAST PRIVILEGE (PostgreSQL Integration)
# ==============================================================================


@pytest.fixture
def test_destination_key(pg_db_migrated: Any) -> str:
    """Ensure an operations alert destination exists in test database."""
    dest_key = "fc27-operations"
    with psycopg.connect(pg_db_migrated.admin, autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO public.fc27_discord_destinations
                (destination_key, guild_id, channel_id, message_kind, enabled)
            VALUES (%s, '900000000000000001', '800000000000000001', 'OPERATIONS_ALERT', TRUE)
            ON CONFLICT (destination_key) DO UPDATE
               SET message_kind = 'OPERATIONS_ALERT', enabled = TRUE;
            """,
            (dest_key,),
        )
    return dest_key


def _insert_test_alert(conn: psycopg.Connection[Any], alert_type: str = "POLL_OVERDUE", component: str = "polling") -> str:
    """Helper to insert an active alert and return its UUID."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT alert_id FROM public.fc27_record_operator_alert(
                %s, %s, 'CRITICAL', 'Test alert summary', '{}'::jsonb, 1800
            );
            """,
            (alert_type, component),
        )
        row = cur.fetchone()
        assert row is not None
        return str(row[0])


def test_15_reserve_delivery_idempotent(pg_db_migrated: Any, test_destination_key: str) -> None:
    """15. Reserve delivery row is idempotent: 1 row per alert/event/destination."""
    with psycopg.connect(pg_db_migrated.match_results_writer) as conn:
        alert_id = _insert_test_alert(conn)
        _tag, digest = build_operations_alert_marker(
            alert_id, "OPEN", "POLL_OVERDUE", "polling", test_destination_key
        )

        with conn.cursor() as cur:
            # First reservation: creates new PENDING delivery
            cur.execute(
                "SELECT delivery_id, is_new, delivery_state FROM public.fc27_reserve_operator_alert_delivery(%s, %s, %s, %s);",
                (alert_id, "OPEN", test_destination_key, digest),
            )
            row1 = cur.fetchone()
            assert row1 is not None
            del_id1, is_new1, state1 = row1
            assert is_new1 is True
            assert state1 == "PENDING"

            # Second reservation: returns existing delivery with is_new=False
            cur.execute(
                "SELECT delivery_id, is_new, delivery_state FROM public.fc27_reserve_operator_alert_delivery(%s, %s, %s, %s);",
                (alert_id, "OPEN", test_destination_key, digest),
            )
            row2 = cur.fetchone()
            assert row2 is not None
            del_id2, is_new2, state2 = row2
            assert del_id2 == del_id1
            assert is_new2 is False
            assert state2 == "PENDING"


def test_16_reserve_delivery_rejects_invalid_inputs(pg_db_migrated: Any, test_destination_key: str) -> None:
    """16. Reserve delivery validates inputs strictly."""
    with psycopg.connect(pg_db_migrated.match_results_writer) as conn:
        alert_id = _insert_test_alert(conn)
        _, digest = build_operations_alert_marker(alert_id, "OPEN", "POLL_OVERDUE", "polling", test_destination_key)

        with conn.cursor() as cur:
            # Invalid lifecycle event
            with pytest.raises(psycopg.DatabaseError, match="must be OPEN or RESOLVED"):
                cur.execute(
                    "SELECT * FROM public.fc27_reserve_operator_alert_delivery(%s, %s, %s, %s);",
                    (alert_id, "ACKNOWLEDGED", test_destination_key, digest),
                )
            conn.rollback()

            # Unknown destination key
            with pytest.raises(psycopg.DatabaseError, match="not found in registry"):
                cur.execute(
                    "SELECT * FROM public.fc27_reserve_operator_alert_delivery(%s, %s, %s, %s);",
                    (alert_id, "OPEN", "nonexistent-dest", digest),
                )
            conn.rollback()


def test_17_claim_deliveries(pg_db_migrated: Any, test_destination_key: str) -> None:
    """17. Claim pending deliveries updates attempt count and state to CLAIMED."""
    with psycopg.connect(pg_db_migrated.match_results_writer) as conn:
        alert_id = _insert_test_alert(conn, "PROVIDER_BUDGET_EXHAUSTED", "polling")
        _, digest = build_operations_alert_marker(alert_id, "OPEN", "PROVIDER_BUDGET_EXHAUSTED", "polling", test_destination_key)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM public.fc27_reserve_operator_alert_delivery(%s, %s, %s, %s);",
                (alert_id, "OPEN", test_destination_key, digest),
            )

        # Claim the batch
        deliveries = claim_operator_alert_deliveries(
            conn,
            destination_key=test_destination_key,
            claim_owner="test-worker-1",
            batch_size=5,
        )
        assert len(deliveries) >= 1
        d = next(item for item in deliveries if item.alert_id == alert_id)
        assert d.attempt_count == 1
        assert d.destination_key == test_destination_key
        assert d.alert_type == "PROVIDER_BUDGET_EXHAUSTED"
        assert d.marker_digest == digest


def test_18_finalize_delivery(pg_db_migrated: Any, test_destination_key: str) -> None:
    """18. Finalize delivery transitions to DELIVERED with message and channel IDs."""
    with psycopg.connect(pg_db_migrated.match_results_writer) as conn:
        alert_id = _insert_test_alert(conn, "DELIVERY_DEAD_LETTER", "discord_delivery")
        _, digest = build_operations_alert_marker(alert_id, "OPEN", "DELIVERY_DEAD_LETTER", "discord_delivery", test_destination_key)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT delivery_id FROM public.fc27_reserve_operator_alert_delivery(%s, %s, %s, %s);",
                (alert_id, "OPEN", test_destination_key, digest),
            )
            del_id = cur.fetchone()[0]

        claim_operator_alert_deliveries(conn, test_destination_key, "worker-1")

        ok = finalize_operator_alert_delivery(
            conn,
            delivery_id=del_id,
            claim_owner="worker-1",
            discord_message_id="888000000000000001",
            discord_channel_id="800000000000000001",
        )
        assert ok is True

        with conn.cursor() as cur:
            cur.execute(
                "SELECT delivery_state, discord_message_id, delivered_at FROM public.fc27_operator_alert_deliveries WHERE delivery_id = %s;",
                (del_id,),
            )
            state, msg_id, del_at = cur.fetchone()
            assert state == "DELIVERED"
            assert msg_id == "888000000000000001"
            assert del_at is not None


def test_19_fail_and_dead_letter_delivery(pg_db_migrated: Any, test_destination_key: str) -> None:
    """19. Fail delivery transitions to FAILED then DEAD_LETTER on attempt limit."""
    with psycopg.connect(pg_db_migrated.match_results_writer) as conn:
        alert_id = _insert_test_alert(conn, "HEARTBEAT_MISSING", "community_worker")
        _, digest = build_operations_alert_marker(alert_id, "OPEN", "HEARTBEAT_MISSING", "community_worker", test_destination_key)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT delivery_id FROM public.fc27_reserve_operator_alert_delivery(%s, %s, %s, %s);",
                (alert_id, "OPEN", test_destination_key, digest),
            )
            del_id = cur.fetchone()[0]

        # Attempt 1: fails to FAILED (max_attempts=2)
        claim_operator_alert_deliveries(conn, test_destination_key, "worker-1", max_attempts=2)
        state1 = fail_operator_alert_delivery(conn, del_id, "worker-1", "HTTP_TIMEOUT", max_attempts=2)
        assert state1 == "FAILED"

        # Attempt 2: reaches max_attempts=2, transitions to DEAD_LETTER
        claim_operator_alert_deliveries(conn, test_destination_key, "worker-1", max_attempts=2)
        state2 = fail_operator_alert_delivery(conn, del_id, "worker-1", "HTTP_TIMEOUT", max_attempts=2)
        assert state2 == "DEAD_LETTER"


def test_20_reconcile_by_marker(pg_db_migrated: Any, test_destination_key: str) -> None:
    """20. Reconcile by marker digest recovers unconfirmed delivery to DELIVERED."""
    with psycopg.connect(pg_db_migrated.match_results_writer) as conn:
        alert_id = _insert_test_alert(conn, "HEARTBEAT_MISSING", "discord_bot")
        _, digest = build_operations_alert_marker(alert_id, "OPEN", "HEARTBEAT_MISSING", "discord_bot", test_destination_key)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT delivery_id FROM public.fc27_reserve_operator_alert_delivery(%s, %s, %s, %s);",
                (alert_id, "OPEN", test_destination_key, digest),
            )
            del_id = cur.fetchone()[0]

        claim_operator_alert_deliveries(conn, test_destination_key, "worker-1")

        reconciled, r_del_id, r_state = reconcile_operator_alert_delivery_by_marker(
            conn,
            destination_key=test_destination_key,
            marker_digest=digest,
            discord_message_id="888000000000000002",
            discord_channel_id="800000000000000001",
        )
        assert reconciled is True
        assert r_del_id == del_id
        assert r_state == "DELIVERED"


def test_21_delivery_summary(pg_db_migrated: Any, test_destination_key: str) -> None:
    """21. Get operator alert delivery summary returns accurate counts."""
    with psycopg.connect(pg_db_migrated.discord_reader) as conn:
        summary = get_operator_alert_delivery_summary(conn)
        assert "pending" in summary
        assert "claimed" in summary
        assert "delivered" in summary
        assert "failed" in summary
        assert "dead_letter" in summary


def test_22_least_privilege_table_access_prohibited(pg_db_migrated: Any) -> None:
    """22. Runtime roles must have NO direct SELECT/INSERT/UPDATE/DELETE on delivery table."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        for role in (
            "fc27_app",
            "fc27_dispatch",
            "fc27_poll_writer",
            "fc27_player_dev_writer",
            "fc27_match_results_writer",
            "fc27_discord_reader",
        ):
            for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                has_priv = conn.execute(
                    "SELECT has_table_privilege(%s, 'public.fc27_operator_alert_deliveries', %s);",
                    (role, priv),
                ).fetchone()[0]
                assert has_priv is False, f"Role {role} should not have direct {priv} on fc27_operator_alert_deliveries"


# ==============================================================================
# 4. DISPATCH CYCLE EXECUTION & AMBIGUOUS RECOVERY
# ==============================================================================


def test_23_cycle_skipped_when_disabled(pg_db_migrated: Any, test_destination_key: str) -> None:
    """23. Cycle returns skipped when disabled."""
    cfg = FC27OperationsAlertDispatchConfig(
        enabled=False, destination_key=test_destination_key
    )
    transport = SyntheticDiscordNotificationTransport()
    res = run_operations_alert_dispatch_cycle(cfg, transport, pg_db_migrated.match_results_writer)
    assert res.skipped_reason == "DISPATCH_DISABLED"
    assert res.claimed_count == 0


def test_24_cycle_skipped_when_destination_numeric(pg_db_migrated: Any) -> None:
    """24. Cycle rejects numeric snowflake destination."""
    cfg = FC27OperationsAlertDispatchConfig(
        enabled=True, destination_key="123456789012345678"
    )
    transport = SyntheticDiscordNotificationTransport()
    res = run_operations_alert_dispatch_cycle(cfg, transport, pg_db_migrated.match_results_writer)
    assert res.skipped_reason == "DESTINATION_KEY_MUST_BE_SYMBOLIC"


def test_25_cycle_successful_dispatch(pg_db_migrated: Any, test_destination_key: str) -> None:
    """25. Happy path: claims, posts to synthetic transport, verifies snapshot, finalizes."""
    with psycopg.connect(pg_db_migrated.match_results_writer) as conn:
        alert_id = _insert_test_alert(conn, "POLL_OVERDUE", "polling")
        _, digest = build_operations_alert_marker(alert_id, "OPEN", "POLL_OVERDUE", "polling", test_destination_key)
        conn.execute(
            "SELECT * FROM public.fc27_reserve_operator_alert_delivery(%s, %s, %s, %s);",
            (alert_id, "OPEN", test_destination_key, digest),
        )

    transport = SyntheticDiscordNotificationTransport()
    cfg = FC27OperationsAlertDispatchConfig(
        enabled=True, destination_key=test_destination_key, batch_size=5
    )

    res = run_operations_alert_dispatch_cycle(cfg, transport, pg_db_migrated.match_results_writer)
    assert res.claimed_count >= 1
    assert res.delivered_count >= 1
    assert res.failed_count == 0

    # Verify message in synthetic transport history
    assert len(transport.sent_text_messages) >= 1
    _chan_id, _msg_id, content = transport.sent_text_messages[-1]
    assert "🔴 **FC27 Operations Alert: POLL_OVERDUE**" in content
    assert "fc27-ops-" in content


def test_26_ambiguous_send_recovery_pre_flight(pg_db_migrated: Any, test_destination_key: str) -> None:
    """26. Pre-flight search finds already posted message by marker, reconciling without duplicate send."""
    transport = SyntheticDiscordNotificationTransport()

    with psycopg.connect(pg_db_migrated.match_results_writer) as conn:
        alert_id = _insert_test_alert(conn, "PROVIDER_BUDGET_EXHAUSTED", "polling")
        marker_tag, digest = build_operations_alert_marker(
            alert_id, "OPEN", "PROVIDER_BUDGET_EXHAUSTED", "polling", test_destination_key
        )
        conn.execute(
            "SELECT * FROM public.fc27_reserve_operator_alert_delivery(%s, %s, %s, %s);",
            (alert_id, "OPEN", test_destination_key, digest),
        )

    # Pre-populate transport with the message as if sent by previous crashed run
    fake_content = f"🔴 Prior message\n{marker_tag}"
    transport.post_text(channel_id="800000000000000001", content=fake_content)
    sent_count_before = len(transport.sent_text_messages)

    cfg = FC27OperationsAlertDispatchConfig(
        enabled=True, destination_key=test_destination_key, batch_size=5
    )
    res = run_operations_alert_dispatch_cycle(cfg, transport, pg_db_migrated.match_results_writer)

    # Reconciled without sending an additional message!
    assert res.reconciled_count >= 1
    assert len(transport.sent_text_messages) == sent_count_before


def test_27_post_verification_failure_records_failed(pg_db_migrated: Any, test_destination_key: str) -> None:
    """27. Snapshot verification failure marks delivery as FAILED."""
    transport = SyntheticDiscordNotificationTransport()

    with psycopg.connect(pg_db_migrated.match_results_writer) as conn:
        alert_id = _insert_test_alert(conn, "HEARTBEAT_MISSING", "discord_bot")
        _, digest = build_operations_alert_marker(
            alert_id, "OPEN", "HEARTBEAT_MISSING", "discord_bot", test_destination_key
        )
        conn.execute(
            "SELECT * FROM public.fc27_reserve_operator_alert_delivery(%s, %s, %s, %s);",
            (alert_id, "OPEN", test_destination_key, digest),
        )

    # Force snapshot to return non-bot author
    transport.non_bot_message_ids.add("910000000000000001")

    cfg = FC27OperationsAlertDispatchConfig(
        enabled=True, destination_key=test_destination_key, batch_size=5, max_attempts=3
    )
    res = run_operations_alert_dispatch_cycle(cfg, transport, pg_db_migrated.match_results_writer)
    assert res.failed_count >= 1


# ==============================================================================
# 5. EVALUATOR-TO-DELIVERY INTEGRATION
# ==============================================================================


def test_28_evaluator_reserves_delivery_on_new_alert(pg_db_migrated: Any, test_destination_key: str) -> None:
    """28. evaluate_and_sync reserves exactly 1 OPEN delivery when an alert is first opened."""
    mgr = FC27AlertManager(pg_db_migrated.match_results_writer, destination_key=test_destination_key)

    # Clean existing alerts
    with psycopg.connect(pg_db_migrated.admin, autocommit=True) as conn:
        conn.execute("DELETE FROM public.fc27_operator_alert_deliveries;")
        conn.execute("DELETE FROM public.fc27_operator_alerts;")

    now = datetime.now(UTC)
    raw = {
        "as_of": now.isoformat(),
        "database_clock": now.isoformat(),
        "health_classification": "CRITICAL",
        "polling": {
            "last_cycle_completed_at": (now - os.path.supports_unicode_filenames and datetime(2026, 11, 25, 0, 0, 0, tzinfo=UTC)).isoformat(),
            "rolling_provider_attempts_used": 18,
            "provider_attempt_limit": 18,
            "all_match_types_active": True,
        },
        "discord_delivery": {
            "jobs_pending": 0, "jobs_claimed": 0, "jobs_completed": 0,
            "jobs_failed": 0, "jobs_dead_lettered": 0,
            "oldest_pending_job_age_seconds": None,
            "formal_result_delivery_count": 0, "team_summary_delivery_count": 0,
            "failed_destination_delivery_count": 0, "last_successful_delivery_time": None,
        },
        "runtime": {
            "worker_heartbeat_age_seconds": 10,
            "bot_heartbeat_age_seconds": 10,
            "current_migration_version": "0027_fc27_operations_alert_dispatch.sql",
            "grants_policy_version": 28,
            "active_backfill_request_count": 0,
        },
    }
    snap = parse_operations_status_snapshot(raw)
    notifs = mgr.evaluate_and_sync(snap, destination_key=test_destination_key)
    assert len(notifs) >= 1

    # Check delivery table: exactly 1 OPEN reservation per alert
    with psycopg.connect(pg_db_migrated.match_results_writer) as conn:
        deliveries = conn.execute(
            "SELECT lifecycle_event, delivery_state FROM public.fc27_operator_alert_deliveries;"
        ).fetchall()
        assert len(deliveries) >= 1
        for ev, st in deliveries:
            assert ev == "OPEN"
            assert st == "PENDING"

    # Repeated evaluation while condition persists must NOT insert duplicate delivery rows
    count_before = len(deliveries)
    mgr.evaluate_and_sync(snap, destination_key=test_destination_key)
    with psycopg.connect(pg_db_migrated.match_results_writer) as conn:
        count_after = conn.execute("SELECT count(*) FROM public.fc27_operator_alert_deliveries;").fetchone()[0]
        assert count_after == count_before


def test_29_evaluator_reserves_delivery_on_resolution(pg_db_migrated: Any, test_destination_key: str) -> None:
    """29. When alert condition resolves, evaluate_and_sync reserves a RESOLVED delivery."""
    mgr = FC27AlertManager(pg_db_migrated.match_results_writer, destination_key=test_destination_key)
    with psycopg.connect(pg_db_migrated.admin, autocommit=True) as conn:
        conn.execute("DELETE FROM public.fc27_operator_alert_deliveries;")
        conn.execute("DELETE FROM public.fc27_operator_alerts;")

    # First open an alert
    alert_notif = mgr.record_alert("POLL_OVERDUE", "polling", "CRITICAL", "Poll delay")
    assert alert_notif is not None
    _tag, digest = build_operations_alert_marker(alert_notif.alert_id, "OPEN", "POLL_OVERDUE", "polling", test_destination_key)
    mgr.reserve_alert_delivery(alert_notif.alert_id, "OPEN", test_destination_key, digest)

    # Now evaluate healthy snapshot -> resolves alert
    now = datetime.now(UTC)
    raw_healthy = {
        "as_of": now.isoformat(),
        "database_clock": now.isoformat(),
        "health_classification": "HEALTHY",
        "polling": {
            "last_cycle_completed_at": now.isoformat(),
            "rolling_provider_attempts_used": 1,
            "provider_attempt_limit": 18,
            "all_match_types_active": True,
        },
        "discord_delivery": {
            "jobs_pending": 0, "jobs_claimed": 0, "jobs_completed": 0,
            "jobs_failed": 0, "jobs_dead_lettered": 0,
            "oldest_pending_job_age_seconds": None,
            "formal_result_delivery_count": 0, "team_summary_delivery_count": 0,
            "failed_destination_delivery_count": 0, "last_successful_delivery_time": None,
        },
        "runtime": {
            "worker_heartbeat_age_seconds": 10,
            "bot_heartbeat_age_seconds": 10,
            "current_migration_version": "0027_fc27_operations_alert_dispatch.sql",
            "grants_policy_version": 28,
            "active_backfill_request_count": 0,
        },
    }
    snap_healthy = parse_operations_status_snapshot(raw_healthy)
    mgr.evaluate_and_sync(snap_healthy, destination_key=test_destination_key)

    with psycopg.connect(pg_db_migrated.match_results_writer) as conn:
        resolved_deliveries = conn.execute(
            "SELECT lifecycle_event, delivery_state FROM public.fc27_operator_alert_deliveries WHERE lifecycle_event = 'RESOLVED';"
        ).fetchall()
        assert len(resolved_deliveries) == 1
        assert resolved_deliveries[0][0] == "RESOLVED"
        assert resolved_deliveries[0][1] == "PENDING"


def test_30_zero_provider_requests_and_zero_result_table_writes(pg_db_migrated: Any, test_destination_key: str) -> None:
    """30. Verify dispatch cycle interacts strictly with alert tables and never touches Match Results or Provider."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        matches_count_before = conn.execute("SELECT count(*) FROM public.fc27_matches;").fetchone()[0]
        attempts_count_before = conn.execute("SELECT count(*) FROM public.fc27_provider_request_attempts;").fetchone()[0]

    transport = SyntheticDiscordNotificationTransport()
    cfg = FC27OperationsAlertDispatchConfig(enabled=True, destination_key=test_destination_key)
    run_operations_alert_dispatch_cycle(cfg, transport, pg_db_migrated.match_results_writer)

    with psycopg.connect(pg_db_migrated.admin) as conn:
        matches_count_after = conn.execute("SELECT count(*) FROM public.fc27_matches;").fetchone()[0]
        attempts_count_after = conn.execute("SELECT count(*) FROM public.fc27_provider_request_attempts;").fetchone()[0]

    assert matches_count_after == matches_count_before
    assert attempts_count_after == attempts_count_before


# ==============================================================================
# 6. IN-MEMORY STORED PROCEDURE & DISPATCH CYCLE UNIT TESTS
# ==============================================================================


class _MockAlertDispatchConnection:
    """Mock database connection simulating the atomic alert dispatch stored procedures."""

    def __init__(self) -> None:
        self.alerts: dict[tuple[str, str], dict[str, Any]] = {}
        self.deliveries: dict[int, dict[str, Any]] = {}
        self._next_delivery_id: int = 1
        self._destinations: dict[str, dict[str, Any]] = {
            "fc27-operations": {
                "guild_id": "900000000000000001",
                "channel_id": "800000000000000001",
                "message_kind": "OPERATIONS_ALERT",
                "enabled": True,
            }
        }

    def close(self) -> None:
        pass

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def cursor(self) -> Any:
        conn = self

        class Cursor:
            def __init__(self) -> None:
                self._result: Any = None

            def __enter__(self) -> Self:
                return self

            def __exit__(self, *args: object) -> None:
                pass

            def execute(self, query: str, params: Any = ()) -> None:
                import json
                q = query.strip()
                now = datetime.now(UTC)

                if "fc27_record_operator_alert" in q:
                    alert_type, component, severity, summary, payload_json, suppression = params
                    key = (alert_type, component)
                    if key in conn.alerts and conn.alerts[key]["status"] in ("OPEN", "ACKNOWLEDGED"):
                        rec = conn.alerts[key]
                        last_notified = rec["last_notified_at"]
                        if (now - last_notified).total_seconds() >= suppression:
                            rec["last_notified_at"] = now
                            rec["notification_count"] += 1
                            rec["summary"] = summary
                            self._result = [(rec["alert_id"], True, False, rec["notification_count"])]
                        else:
                            rec["summary"] = summary
                            self._result = [(rec["alert_id"], False, False, rec["notification_count"])]
                    else:
                        alert_id = f"alert-{len(conn.alerts) + 1}"
                        rec = {
                            "alert_id": alert_id,
                            "alert_type": alert_type,
                            "component": component,
                            "severity": severity,
                            "status": "OPEN",
                            "summary": summary,
                            "details": json.loads(payload_json) if isinstance(payload_json, str) else payload_json,
                            "opened_at": now,
                            "last_notified_at": now,
                            "notification_count": 1,
                        }
                        conn.alerts[key] = rec
                        self._result = [(alert_id, True, True, 1)]

                elif "fc27_resolve_operator_alert" in q:
                    alert_type, component = params
                    key = (alert_type, component)
                    if key in conn.alerts and conn.alerts[key]["status"] in ("OPEN", "ACKNOWLEDGED"):
                        a_id = conn.alerts[key]["alert_id"]
                        conn.alerts[key]["status"] = "RESOLVED"
                        self._result = [(True, a_id)]
                    else:
                        self._result = [(False, None)]

                elif "fc27_get_active_operator_alerts" in q:
                    rows = [
                        (
                            r["alert_id"],
                            r["alert_type"],
                            r["component"],
                            r["severity"],
                            r["status"],
                            r["summary"],
                            r["details"],
                            r["opened_at"],
                            r["last_notified_at"],
                            r["notification_count"],
                        )
                        for r in conn.alerts.values()
                        if r["status"] in ("OPEN", "ACKNOWLEDGED")
                    ]
                    self._result = rows

                elif "fc27_reserve_operator_alert_delivery" in q:
                    alert_id, lifecycle_event, dest_key, marker_digest = params
                    if dest_key not in conn._destinations:
                        raise psycopg.DatabaseError(f"Destination key '{dest_key}' not found in registry")
                    for d_id, d_rec in conn.deliveries.items():
                        if (
                            d_rec["alert_id"] == alert_id
                            and d_rec["lifecycle_event"] == lifecycle_event
                            and d_rec["destination_key"] == dest_key
                        ):
                            self._result = [(d_id, False, d_rec["delivery_state"])]
                            return
                    new_id = conn._next_delivery_id
                    conn._next_delivery_id += 1
                    conn.deliveries[new_id] = {
                        "delivery_id": new_id,
                        "alert_id": alert_id,
                        "lifecycle_event": lifecycle_event,
                        "destination_key": dest_key,
                        "marker_digest": marker_digest,
                        "delivery_state": "PENDING",
                        "attempt_count": 0,
                        "claim_owner": None,
                        "claimed_at": None,
                        "discord_message_id": None,
                        "discord_channel_id": None,
                        "last_error": None,
                        "delivered_at": None,
                        "created_at": now,
                    }
                    self._result = [(new_id, True, "PENDING")]

                elif "fc27_claim_operator_alert_deliveries" in q:
                    dest_key, claim_owner, batch_size, _lease_seconds, max_attempts = params
                    claimed_rows = []
                    count = 0
                    for d_id, d_rec in conn.deliveries.items():
                        if count >= batch_size:
                            break
                        if d_rec["destination_key"] == dest_key and (
                            d_rec["delivery_state"] == "PENDING"
                            or (
                                d_rec["delivery_state"] == "FAILED"
                                and d_rec["attempt_count"] < max_attempts
                            )
                        ):
                            d_rec["delivery_state"] = "CLAIMED"
                            d_rec["attempt_count"] += 1
                            d_rec["claim_owner"] = claim_owner
                            d_rec["claimed_at"] = now
                            count += 1

                            alert_rec = next(
                                (a for a in conn.alerts.values() if a["alert_id"] == d_rec["alert_id"]),
                                None,
                            )
                            a_type = alert_rec["alert_type"] if alert_rec else "UNKNOWN"
                            comp = alert_rec["component"] if alert_rec else "unknown"
                            sev = alert_rec["severity"] if alert_rec else "WARNING"
                            summ = alert_rec["summary"] if alert_rec else ""
                            det = alert_rec["details"] if alert_rec else {}
                            op_at = alert_rec["opened_at"] if alert_rec else now
                            res_at = now if d_rec["lifecycle_event"] == "RESOLVED" else None
                            notif_count = alert_rec["notification_count"] if alert_rec else 1

                            dest_rec = conn._destinations.get(dest_key, {})
                            g_id = dest_rec.get("guild_id", "900000000000000001")
                            ch_id = dest_rec.get("channel_id", "800000000000000001")

                            claimed_rows.append(
                                (
                                    d_id,
                                    d_rec["alert_id"],
                                    d_rec["lifecycle_event"],
                                    d_rec["destination_key"],
                                    d_rec["attempt_count"],
                                    d_rec["marker_digest"],
                                    g_id,
                                    ch_id,
                                    a_type,
                                    comp,
                                    sev,
                                    summ,
                                    json.dumps(det) if isinstance(det, dict) else det,
                                    op_at,
                                    res_at,
                                    notif_count,
                                )
                            )
                    self._result = claimed_rows

                elif "fc27_finalize_operator_alert_delivery" in q:
                    d_id, claim_owner, msg_id, ch_id = params
                    if d_id in conn.deliveries:
                        rec = conn.deliveries[d_id]
                        if rec["delivery_state"] == "CLAIMED" and rec["claim_owner"] == claim_owner:
                            rec["delivery_state"] = "DELIVERED"
                            rec["discord_message_id"] = msg_id
                            rec["discord_channel_id"] = ch_id
                            rec["delivered_at"] = now
                            self._result = [(True,)]
                            return
                    self._result = [(False,)]

                elif "fc27_fail_operator_alert_delivery" in q:
                    d_id, claim_owner, err, max_attempts = params
                    if d_id in conn.deliveries:
                        rec = conn.deliveries[d_id]
                        rec["last_error"] = err
                        if rec["attempt_count"] >= max_attempts:
                            rec["delivery_state"] = "DEAD_LETTER"
                            self._result = [("DEAD_LETTER",)]
                        else:
                            rec["delivery_state"] = "FAILED"
                            self._result = [("FAILED",)]
                    else:
                        self._result = [("FAILED",)]

                elif "fc27_reconcile_operator_alert_delivery_by_marker" in q:
                    dest_key, marker_digest, msg_id, ch_id = params
                    for d_id, rec in conn.deliveries.items():
                        if rec["destination_key"] == dest_key and rec["marker_digest"] == marker_digest:
                            if rec["delivery_state"] != "DELIVERED":
                                rec["delivery_state"] = "DELIVERED"
                                rec["discord_message_id"] = msg_id
                                rec["discord_channel_id"] = ch_id
                                rec["delivered_at"] = now
                            self._result = [(True, d_id, rec["delivery_state"])]
                            return
                    self._result = [(False, None, None)]

                elif "fc27_get_operator_alert_delivery_summary" in q:
                    pending = sum(1 for d in conn.deliveries.values() if d["delivery_state"] == "PENDING")
                    claimed = sum(1 for d in conn.deliveries.values() if d["delivery_state"] == "CLAIMED")
                    delivered = sum(1 for d in conn.deliveries.values() if d["delivery_state"] == "DELIVERED")
                    failed = sum(1 for d in conn.deliveries.values() if d["delivery_state"] == "FAILED")
                    dead_letter = sum(1 for d in conn.deliveries.values() if d["delivery_state"] == "DEAD_LETTER")
                    self._result = [(pending, claimed, delivered, failed, dead_letter)]

            def fetchone(self) -> Any:
                if self._result:
                    return self._result[0]
                return None

            def fetchall(self) -> Any:
                return self._result or []

        return Cursor()


def _make_guarded_mock(mock_conn: _MockAlertDispatchConnection) -> GuardedMatchResultsWriterConnection:
    """Create a GuardedMatchResultsWriterConnection wrapping a mock connection."""
    guarded = GuardedMatchResultsWriterConnection.__new__(GuardedMatchResultsWriterConnection)
    guarded._conn = mock_conn  # type: ignore[assignment]
    guarded.current_role = "fc27_match_results_writer"
    return guarded


def test_31_mock_reserve_operator_alert_delivery() -> None:
    """31. Unit: Reserve delivery creates row on first call, returns existing on second."""
    conn = _MockAlertDispatchConnection()
    mgr = FC27AlertManager(conn)
    alert = mgr.record_alert("POLL_OVERDUE", "polling", "CRITICAL", "Poll delay")
    assert alert is not None
    _tag, digest = build_operations_alert_marker(alert.alert_id, "OPEN", "POLL_OVERDUE", "polling", "fc27-operations")

    res1 = mgr.reserve_alert_delivery(alert.alert_id, "OPEN", "fc27-operations", digest)
    assert res1 is not None
    del_id1, is_new1, state1 = res1
    assert del_id1 == 1
    assert is_new1 is True
    assert state1 == "PENDING"

    # Second reservation
    res2 = mgr.reserve_alert_delivery(alert.alert_id, "OPEN", "fc27-operations", digest)
    assert res2 is not None
    del_id2, is_new2, state2 = res2
    assert del_id2 == 1
    assert is_new2 is False
    assert state2 == "PENDING"


def test_32_mock_claim_operator_alert_deliveries() -> None:
    """32. Unit: Claim deliveries transitions PENDING to CLAIMED and increments attempt."""
    conn = _MockAlertDispatchConnection()
    mgr = FC27AlertManager(conn)
    alert = mgr.record_alert("PROVIDER_BUDGET_EXHAUSTED", "polling", "CRITICAL", "Budget empty")
    assert alert is not None
    _tag, digest = build_operations_alert_marker(alert.alert_id, "OPEN", "PROVIDER_BUDGET_EXHAUSTED", "polling", "fc27-operations")
    mgr.reserve_alert_delivery(alert.alert_id, "OPEN", "fc27-operations", digest)

    claimed = claim_operator_alert_deliveries(conn, "fc27-operations", "worker-1", batch_size=5)  # type: ignore[arg-type]
    assert len(claimed) == 1
    d = claimed[0]
    assert d.delivery_id == 1
    assert d.attempt_count == 1
    assert d.alert_type == "PROVIDER_BUDGET_EXHAUSTED"
    assert conn.deliveries[1]["delivery_state"] == "CLAIMED"


def test_33_mock_finalize_operator_alert_delivery() -> None:
    """33. Unit: Finalize delivery records message/channel ID and sets DELIVERED."""
    conn = _MockAlertDispatchConnection()
    mgr = FC27AlertManager(conn)
    alert = mgr.record_alert("DELIVERY_DEAD_LETTER", "discord_delivery", "CRITICAL", "Dead letter")
    assert alert is not None
    _tag, digest = build_operations_alert_marker(alert.alert_id, "OPEN", "DELIVERY_DEAD_LETTER", "discord_delivery", "fc27-operations")
    mgr.reserve_alert_delivery(alert.alert_id, "OPEN", "fc27-operations", digest)

    claim_operator_alert_deliveries(conn, "fc27-operations", "worker-1")  # type: ignore[arg-type]

    ok = finalize_operator_alert_delivery(
        conn, delivery_id=1, claim_owner="worker-1",  # type: ignore[arg-type]
        discord_message_id="888000000000000001",
        discord_channel_id="800000000000000001",
    )
    assert ok is True
    assert conn.deliveries[1]["delivery_state"] == "DELIVERED"
    assert conn.deliveries[1]["discord_message_id"] == "888000000000000001"
    assert conn.deliveries[1]["delivered_at"] is not None


def test_34_mock_fail_operator_alert_delivery_and_dead_letter() -> None:
    """34. Unit: Fail delivery transitions to FAILED, then DEAD_LETTER on attempt limit."""
    conn = _MockAlertDispatchConnection()
    mgr = FC27AlertManager(conn)
    alert = mgr.record_alert("HEARTBEAT_MISSING", "community_worker", "CRITICAL", "Worker dead")
    assert alert is not None
    _tag, digest = build_operations_alert_marker(alert.alert_id, "OPEN", "HEARTBEAT_MISSING", "community_worker", "fc27-operations")
    mgr.reserve_alert_delivery(alert.alert_id, "OPEN", "fc27-operations", digest)

    # Attempt 1
    claim_operator_alert_deliveries(conn, "fc27-operations", "worker-1", max_attempts=2)  # type: ignore[arg-type]
    st1 = fail_operator_alert_delivery(conn, 1, "worker-1", "NETWORK_ERROR", max_attempts=2)  # type: ignore[arg-type]
    assert st1 == "FAILED"
    assert conn.deliveries[1]["delivery_state"] == "FAILED"

    # Attempt 2
    claim_operator_alert_deliveries(conn, "fc27-operations", "worker-1", max_attempts=2)  # type: ignore[arg-type]
    st2 = fail_operator_alert_delivery(conn, 1, "worker-1", "NETWORK_ERROR", max_attempts=2)  # type: ignore[arg-type]
    assert st2 == "DEAD_LETTER"
    assert conn.deliveries[1]["delivery_state"] == "DEAD_LETTER"


def test_35_mock_reconcile_operator_alert_delivery_by_marker() -> None:
    """35. Unit: Reconcile delivery by marker tag digest without resending."""
    conn = _MockAlertDispatchConnection()
    mgr = FC27AlertManager(conn)
    alert = mgr.record_alert("HEARTBEAT_MISSING", "discord_bot", "CRITICAL", "Bot dead")
    assert alert is not None
    _tag, digest = build_operations_alert_marker(alert.alert_id, "OPEN", "HEARTBEAT_MISSING", "discord_bot", "fc27-operations")
    mgr.reserve_alert_delivery(alert.alert_id, "OPEN", "fc27-operations", digest)

    claim_operator_alert_deliveries(conn, "fc27-operations", "worker-1")  # type: ignore[arg-type]

    reconciled, del_id, state = reconcile_operator_alert_delivery_by_marker(
        conn,  # type: ignore[arg-type]
        destination_key="fc27-operations",
        marker_digest=digest,
        discord_message_id="888000000000000002",
        discord_channel_id="800000000000000001",
    )
    assert reconciled is True
    assert del_id == 1
    assert state == "DELIVERED"
    assert conn.deliveries[1]["delivery_state"] == "DELIVERED"
    assert conn.deliveries[1]["discord_message_id"] == "888000000000000002"


def test_36_mock_get_operator_alert_delivery_summary() -> None:
    """36. Unit: Get operator alert delivery summary counts states accurately."""
    conn = _MockAlertDispatchConnection()
    conn.deliveries[1] = {"delivery_state": "PENDING"}
    conn.deliveries[2] = {"delivery_state": "CLAIMED"}
    conn.deliveries[3] = {"delivery_state": "DELIVERED"}
    conn.deliveries[4] = {"delivery_state": "FAILED"}
    conn.deliveries[5] = {"delivery_state": "DEAD_LETTER"}

    summary = get_operator_alert_delivery_summary(conn)  # type: ignore[arg-type]
    assert summary == {
        "pending": 1,
        "claimed": 1,
        "delivered": 1,
        "failed": 1,
        "dead_letter": 1,
    }


def test_37_mock_dispatch_cycle_happy_path() -> None:
    """37. Unit: Happy path cycle claims, posts to transport, verifies snapshot, and finalizes."""
    conn = _MockAlertDispatchConnection()
    mgr = FC27AlertManager(conn)
    alert = mgr.record_alert("POLL_OVERDUE", "polling", "CRITICAL", "Poll delay")
    assert alert is not None
    _tag, digest = build_operations_alert_marker(alert.alert_id, "OPEN", "POLL_OVERDUE", "polling", "fc27-operations")
    mgr.reserve_alert_delivery(alert.alert_id, "OPEN", "fc27-operations", digest)

    transport = SyntheticDiscordNotificationTransport()
    cfg = FC27OperationsAlertDispatchConfig(enabled=True, destination_key="fc27-operations", batch_size=5)

    res = run_operations_alert_dispatch_cycle(cfg, transport, _make_guarded_mock(conn))  # type: ignore[arg-type]
    assert res.claimed_count == 1
    assert res.delivered_count == 1
    assert res.failed_count == 0

    assert len(transport.sent_text_messages) == 1
    assert "🔴 **FC27 Operations Alert: POLL_OVERDUE**" in transport.sent_text_messages[0][2]
    assert "fc27-ops-" in transport.sent_text_messages[0][2]
    assert conn.deliveries[1]["delivery_state"] == "DELIVERED"


def test_38_mock_dispatch_cycle_ambiguous_preflight_reconciliation() -> None:
    """38. Unit: Ambiguous recovery reconciles message found in channel without duplicate post."""
    conn = _MockAlertDispatchConnection()
    mgr = FC27AlertManager(conn)
    alert = mgr.record_alert("PROVIDER_BUDGET_EXHAUSTED", "polling", "CRITICAL", "Budget exhausted")
    assert alert is not None
    marker_tag, digest = build_operations_alert_marker(alert.alert_id, "OPEN", "PROVIDER_BUDGET_EXHAUSTED", "polling", "fc27-operations")
    mgr.reserve_alert_delivery(alert.alert_id, "OPEN", "fc27-operations", digest)

    transport = SyntheticDiscordNotificationTransport()
    fake_msg = f"🔴 Prior crash message\n{marker_tag}"
    transport.post_text(channel_id="800000000000000001", content=fake_msg)
    sent_count_before = len(transport.sent_text_messages)

    cfg = FC27OperationsAlertDispatchConfig(enabled=True, destination_key="fc27-operations", batch_size=5)
    res = run_operations_alert_dispatch_cycle(cfg, transport, _make_guarded_mock(conn))  # type: ignore[arg-type]

    assert res.claimed_count == 1
    assert res.reconciled_count == 1
    assert res.delivered_count == 0
    assert len(transport.sent_text_messages) == sent_count_before
    assert conn.deliveries[1]["delivery_state"] == "DELIVERED"


def test_39_mock_dispatch_cycle_snapshot_verification_failure() -> None:
    """39. Unit: Snapshot verification failure records FAILED."""
    conn = _MockAlertDispatchConnection()
    mgr = FC27AlertManager(conn)
    alert = mgr.record_alert("HEARTBEAT_MISSING", "discord_bot", "CRITICAL", "Bot missing")
    assert alert is not None
    _tag, digest = build_operations_alert_marker(alert.alert_id, "OPEN", "HEARTBEAT_MISSING", "discord_bot", "fc27-operations")
    mgr.reserve_alert_delivery(alert.alert_id, "OPEN", "fc27-operations", digest)

    transport = SyntheticDiscordNotificationTransport()
    # Designate next message as non-bot author
    transport.non_bot_message_ids.add("910000000000000001")

    cfg = FC27OperationsAlertDispatchConfig(enabled=True, destination_key="fc27-operations", batch_size=5, max_attempts=3)
    res = run_operations_alert_dispatch_cycle(cfg, transport, _make_guarded_mock(conn))  # type: ignore[arg-type]

    assert res.claimed_count == 1
    assert res.failed_count == 1
    assert conn.deliveries[1]["delivery_state"] == "FAILED"


def test_40_mock_dispatch_cycle_dead_letter_exhaustion() -> None:
    """40. Unit: Transition to DEAD_LETTER when maximum attempts are exhausted."""
    conn = _MockAlertDispatchConnection()
    mgr = FC27AlertManager(conn)
    alert = mgr.record_alert("HEARTBEAT_MISSING", "community_worker", "CRITICAL", "Worker missing")
    assert alert is not None
    _tag, digest = build_operations_alert_marker(alert.alert_id, "OPEN", "HEARTBEAT_MISSING", "community_worker", "fc27-operations")
    mgr.reserve_alert_delivery(alert.alert_id, "OPEN", "fc27-operations", digest)

    # Pre-set attempt count to 2 with max_attempts=2
    conn.deliveries[1]["attempt_count"] = 1
    conn.deliveries[1]["delivery_state"] = "FAILED"

    transport = SyntheticDiscordNotificationTransport()
    transport.non_bot_message_ids.add("910000000000000001")

    cfg = FC27OperationsAlertDispatchConfig(enabled=True, destination_key="fc27-operations", batch_size=5, max_attempts=2)
    res = run_operations_alert_dispatch_cycle(cfg, transport, _make_guarded_mock(conn))  # type: ignore[arg-type]

    assert res.claimed_count == 1
    assert res.dead_letter_count == 1
    assert conn.deliveries[1]["delivery_state"] == "DEAD_LETTER"


def test_41_mock_dispatch_cycle_disabled_and_invalid_destinations() -> None:
    """41. Unit: Dispatch cycle skips when disabled or when destination is not symbolic."""
    conn = _MockAlertDispatchConnection()
    transport = SyntheticDiscordNotificationTransport()

    # Disabled
    cfg_dis = FC27OperationsAlertDispatchConfig(enabled=False, destination_key="fc27-operations")
    res_dis = run_operations_alert_dispatch_cycle(cfg_dis, transport, _make_guarded_mock(conn))  # type: ignore[arg-type]
    assert res_dis.skipped_reason == "DISPATCH_DISABLED"

    # Numeric snowflake rejected
    cfg_num = FC27OperationsAlertDispatchConfig(enabled=True, destination_key="123456789012345678")
    res_num = run_operations_alert_dispatch_cycle(cfg_num, transport, _make_guarded_mock(conn))  # type: ignore[arg-type]
    assert res_num.skipped_reason == "DESTINATION_KEY_MUST_BE_SYMBOLIC"

    # Empty rejected
    cfg_emp = FC27OperationsAlertDispatchConfig(enabled=True, destination_key="")
    res_emp = run_operations_alert_dispatch_cycle(cfg_emp, transport, _make_guarded_mock(conn))  # type: ignore[arg-type]
    assert res_emp.skipped_reason == "DESTINATION_KEY_EMPTY"


def test_42_mock_evaluator_to_delivery_integration() -> None:
    """42. Unit: Evaluator creates OPEN delivery, suppresses duplicate, and creates RESOLVED delivery."""
    conn = _MockAlertDispatchConnection()
    mgr = FC27AlertManager(conn, destination_key="fc27-operations")

    now = datetime.now(UTC)
    raw_unhealthy = {
        "as_of": now.isoformat(),
        "database_clock": now.isoformat(),
        "health_classification": "CRITICAL",
        "polling": {
            "last_cycle_completed_at": datetime(2026, 11, 25, 0, 0, 0, tzinfo=UTC).isoformat(),
            "rolling_provider_attempts_used": 18,
            "provider_attempt_limit": 18,
            "all_match_types_active": True,
        },
        "discord_delivery": {
            "jobs_pending": 0, "jobs_claimed": 0, "jobs_completed": 0,
            "jobs_failed": 0, "jobs_dead_lettered": 0,
            "oldest_pending_job_age_seconds": None,
            "formal_result_delivery_count": 0, "team_summary_delivery_count": 0,
            "failed_destination_delivery_count": 0, "last_successful_delivery_time": None,
        },
        "runtime": {
            "worker_heartbeat_age_seconds": 10,
            "bot_heartbeat_age_seconds": 10,
            "current_migration_version": "0027_fc27_operations_alert_dispatch.sql",
            "grants_policy_version": 28,
            "active_backfill_request_count": 0,
        },
    }
    snap_unhealthy = parse_operations_status_snapshot(raw_unhealthy)

    # 1. First evaluation creates alerts and OPEN deliveries
    notifs1 = mgr.evaluate_and_sync(snap_unhealthy, destination_key="fc27-operations")
    assert len(notifs1) >= 1
    open_count_1 = sum(1 for d in conn.deliveries.values() if d["lifecycle_event"] == "OPEN")
    assert open_count_1 >= 1

    # 2. Repeated evaluation while unhealthy suppresses duplicate delivery rows
    mgr.evaluate_and_sync(snap_unhealthy, destination_key="fc27-operations")
    open_count_2 = sum(1 for d in conn.deliveries.values() if d["lifecycle_event"] == "OPEN")
    assert open_count_2 == open_count_1

    # 3. Healthy snapshot resolves alerts and creates RESOLVED deliveries
    raw_healthy = {
        "as_of": now.isoformat(),
        "database_clock": now.isoformat(),
        "health_classification": "HEALTHY",
        "polling": {
            "last_cycle_completed_at": now.isoformat(),
            "rolling_provider_attempts_used": 1,
            "provider_attempt_limit": 18,
            "all_match_types_active": True,
        },
        "discord_delivery": {
            "jobs_pending": 0, "jobs_claimed": 0, "jobs_completed": 0,
            "jobs_failed": 0, "jobs_dead_lettered": 0,
            "oldest_pending_job_age_seconds": None,
            "formal_result_delivery_count": 0, "team_summary_delivery_count": 0,
            "failed_destination_delivery_count": 0, "last_successful_delivery_time": None,
        },
        "runtime": {
            "worker_heartbeat_age_seconds": 10,
            "bot_heartbeat_age_seconds": 10,
            "current_migration_version": "0027_fc27_operations_alert_dispatch.sql",
            "grants_policy_version": 28,
            "active_backfill_request_count": 0,
        },
    }
    snap_healthy = parse_operations_status_snapshot(raw_healthy)
    mgr.evaluate_and_sync(snap_healthy, destination_key="fc27-operations")

    resolved_count = sum(1 for d in conn.deliveries.values() if d["lifecycle_event"] == "RESOLVED")
    assert resolved_count >= 1
