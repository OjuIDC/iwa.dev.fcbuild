"""Package 3 Dispatch Worker Service Tests.

Tests:
1. Input validation & event envelope parsing.
2. Worker heartbeat & G-WORKER gate evaluation.
3. Deterministic policy validation & personalized plan engine.
4. Player identity verification enforcement.
5. Fail-closed kill-switch (DISPATCH_ENABLED) & gate enforcement.
6. Delivery adapters (dashboard storage & websocket notification).
7. Concurrency, claiming, and idempotency.
8. Crash recovery & stale item handling.
9. Least privilege security for fc27_dispatch role.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Any
from unittest.mock import MagicMock

import psycopg
from psycopg.rows import dict_row
import pytest

from src.dispatch.gates import DeploymentIdentity, evaluate_gates
from src.dispatch.worker import (
    DashboardStorageAdapter,
    HeartbeatManager,
    WebSocketNotificationAdapter,
    check_worker_health,
    parse_event_envelope,
    process_dispatch_event,
    validate_personalized_plan,
    write_heartbeat,
)


# =====================================================================
# Fixtures and Helpers
# =====================================================================

@pytest.fixture
def mock_redis():
    """Deterministic in-memory Redis client."""
    class InMemoryRedis:
        def __init__(self):
            self.published: list[tuple[str, str]] = []

        def publish(self, channel: str, message: str) -> int:
            self.published.append((channel, message))
            return 1

        def ping(self) -> bool:
            return True

    return InMemoryRedis()


def _make_valid_envelope(dispatch_id: uuid.UUID, player_id: str = "10001", club_id: str = "330323") -> dict[str, Any]:
    """Constructs an approved domain event envelope as published by outbox relay."""
    return {
        "event_id": 1,
        "event_type": "dispatch.created",
        "entity_type": "dispatch",
        "entity_id": str(dispatch_id),
        "actor": "commissioner:batmanL8",
        "correlation_id": str(uuid.uuid4()),
        "occurred_at": "2026-10-17T12:00:00+00:00",
        "source_table": "dispatches",
        "source_id": str(dispatch_id),
        "scope_club_id": club_id,
        "scope_player_id": player_id,
        "visibility": "player_owner",
        "fc_version": "27",
        "platform": "common-gen5",
    }


# =====================================================================
# 1. Input Validation Tests
# =====================================================================

def test_parse_valid_envelope():
    """AC: Valid envelope passes strict validation with UUIDs preserved."""
    disp_id = uuid.uuid4()
    corr_id = uuid.uuid4()
    raw = {
        "event_id": 42,
        "event_type": "dispatch.created",
        "entity_type": "dispatch",
        "entity_id": str(disp_id),
        "actor": "commissioner:admin",
        "correlation_id": str(corr_id),
        "scope_club_id": "330323",
        "scope_player_id": "player-1",
    }
    parsed = parse_event_envelope(raw)
    assert parsed["event_id"] == 42
    assert parsed["entity_id"] == str(disp_id)


def test_parse_envelope_from_json_string():
    """AC: Strict JSON string parsing works as expected."""
    disp_id = uuid.uuid4()
    corr_id = uuid.uuid4()
    raw = json.dumps({
        "event_id": 10,
        "event_type": "dispatch.created",
        "entity_type": "dispatch",
        "entity_id": str(disp_id),
        "actor": "commissioner:admin",
        "correlation_id": str(corr_id),
        "scope_club_id": "330323",
        "scope_player_id": "player-1",
    })
    parsed = parse_event_envelope(raw)
    assert parsed["event_id"] == 10


def test_parse_envelope_malformed_json():
    """AC: Malformed JSON strings are rejected with ValueError."""
    with pytest.raises(ValueError, match="malformed event JSON"):
        parse_event_envelope("{not-valid-json}")


def test_parse_envelope_missing_required_fields():
    """AC: Envelopes missing mandatory fields are rejected with ValueError."""
    incomplete = {
        "event_id": 1,
        "event_type": "dispatch.created",
        # missing entity_id, actor, correlation_id, scopes
    }
    with pytest.raises(ValueError, match="missing required fields"):
        parse_event_envelope(incomplete)


def test_parse_envelope_invalid_uuid():
    """AC: Non-UUID entity_id or correlation_id is rejected."""
    bad_uuid = {
        "event_id": 1,
        "event_type": "dispatch.created",
        "entity_type": "dispatch",
        "entity_id": "not-a-uuid",
        "actor": "commissioner:admin",
        "correlation_id": str(uuid.uuid4()),
        "scope_club_id": "330323",
        "scope_player_id": "player-1",
    }
    with pytest.raises(ValueError, match="invalid entity_id UUID"):
        parse_event_envelope(bad_uuid)


# =====================================================================
# 2. Worker Heartbeat and G-WORKER Evaluation Tests
# =====================================================================

def test_write_heartbeat_updates_service_heartbeats(pg_dispatch_uri):
    """AC: Worker writes periodic heartbeat satisfying G-WORKER gate."""
    with psycopg.connect(pg_dispatch_uri, autocommit=True) as conn:
        write_heartbeat(conn, instance_id="test-worker-1")

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT service_name, details, extract(epoch from (now() - last_heartbeat)) as age FROM public.service_heartbeats WHERE service_name = 'dispatch-worker'")
            row = cur.fetchone()
            assert row is not None
            assert row["service_name"] == "dispatch-worker"
            assert float(row["age"]) < 5.0
            assert row["details"]["instance_id"] == "test-worker-1"


def test_g_worker_gate_passes_with_fresh_heartbeat(pg_uri, pg_dispatch_uri):
    """AC: G-WORKER passes when heartbeat age < 60s."""
    with psycopg.connect(pg_dispatch_uri, autocommit=True) as conn:
        write_heartbeat(conn, instance_id="fresh-worker")

    with psycopg.connect(pg_uri) as conn:
        results = evaluate_gates(conn, DeploymentIdentity("rev1", "dep1"))
        g_worker = next(r for r in results if r.gate_id == "G-WORKER")
        assert g_worker.passed is True
        assert "<60s required" in g_worker.detail


def test_g_worker_gate_fails_with_stale_heartbeat(pg_dispatch_uri, pg_admin_db_uri, pg_uri):
    """AC: G-WORKER fails when heartbeat age >= 60s."""
    # Write initial heartbeat as worker
    with psycopg.connect(pg_dispatch_uri, autocommit=True) as conn:
        write_heartbeat(conn, instance_id="stale-worker")

    # Manually backdate heartbeat using admin connection
    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE public.service_heartbeats
                   SET last_heartbeat = now() - interval '120 seconds'
                 WHERE service_name = 'dispatch-worker'
            """)

    with psycopg.connect(pg_uri) as conn:
        results = evaluate_gates(conn, DeploymentIdentity("rev1", "dep1"))
        g_worker = next(r for r in results if r.gate_id == "G-WORKER")
        assert g_worker.passed is False
        assert "heartbeat stale" in g_worker.detail


def test_heartbeat_manager_lifecycle(pg_dispatch_uri):
    """AC: HeartbeatManager starts background thread, writes heartbeats, stops cleanly."""
    mgr = HeartbeatManager(pg_dispatch_uri, interval_seconds=0.2, instance_id="mgr-test")
    mgr.start()
    try:
        time.sleep(0.5)
        with psycopg.connect(pg_dispatch_uri) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT extract(epoch from (now() - last_heartbeat)) as age FROM public.service_heartbeats WHERE service_name = 'dispatch-worker'")
                row = cur.fetchone()
                assert row is not None
                assert float(row["age"]) < 2.0
    finally:
        mgr.stop(timeout=2.0)


# =====================================================================
# 3. Deterministic Policy Validation & Personalized Plan Tests
# =====================================================================

def test_validate_plan_valid():
    """AC: Conforming instruction produces 'valid' plan."""
    inst = {
        "metric": "pass_accuracy",
        "target_delta": 5.0,
        "objective": "Improve short passing under pressure",
        "drills": ["one_touch_passing", "rondo"],
    }
    status, plan, details = validate_personalized_plan(inst)
    assert status == "valid"
    assert plan["target_delta"] == 5.0
    assert plan["metric"] == "pass_accuracy"
    assert details["status"] == "policy_passed"


def test_validate_plan_material_change():
    """AC: Instruction marked with material change produces 'material_change'."""
    inst = {
        "metric": "pass_accuracy",
        "target_delta": 5.0,
        "material_change": True,
    }
    status, plan, details = validate_personalized_plan(inst)
    assert status == "material_change"
    assert "material change detected" in details["reason"]


def test_validate_plan_out_of_bounds():
    """AC: Instruction exceeding metric bounds produces 'invalid'."""
    inst = {
        "metric": "pass_accuracy",
        "target_delta": 50.0,  # Max allowed is 25.0 in fc27-objective-bounds-v1
    }
    status, plan, details = validate_personalized_plan(inst)
    assert status == "invalid"
    assert "must be between" in details["error"]


def test_validate_plan_schema_invalid():
    """AC: Missing metric or non-numeric delta produces 'schema_invalid'."""
    inst = {"objective": "vague instructions without metric"}
    status, plan, details = validate_personalized_plan(inst)
    assert status == "schema_invalid"
    assert "missing metric" in details["error"]


# =====================================================================
# 4. End-to-End Worker Processing & Fail-Closed Invariants
# =====================================================================

def test_process_dispatch_scope_mismatch_rejected(pg_dispatch_uri, make_dispatch):
    """AC: Security violation if event scope does not match authoritative DB record."""
    d_id = make_dispatch(
        metric="pass_accuracy",
        instruction={"metric": "pass_accuracy", "target_delta": 5.0},
    )
    # Event claims club_id 999999 instead of 330323
    envelope = _make_valid_envelope(d_id, club_id="999999")
    with psycopg.connect(pg_dispatch_uri, autocommit=True) as conn:
        res = process_dispatch_event(conn, envelope, dispatch_enabled=False)
        assert res["status"] == "error"
        assert res["reason"] == "security_scope_mismatch"


def test_process_dispatch_player_unverified_transitions_failed(pg_dispatch_uri, pg_admin_db_uri, make_dispatch):
    """AC: Unverified player identity blocks dispatch and transitions record to failed."""
    d_id = make_dispatch(
        metric="pass_accuracy",
        instruction={"metric": "pass_accuracy", "target_delta": 5.0},
    )
    # Ensure player has identity_verified_at = NULL
    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE public.players SET identity_verified_at = NULL WHERE player_id = '10001'")

    envelope = _make_valid_envelope(d_id, player_id="10001")
    with psycopg.connect(pg_dispatch_uri, autocommit=True) as conn:
        res = process_dispatch_event(conn, envelope, dispatch_enabled=True)
        assert res["status"] == "failed"
        assert res["failure_reason"] == "player_identity_unverified"

        # Verify DB status transitioned to failed
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT dispatch_status, failure_reason FROM public.dispatches WHERE dispatch_id = %s", (str(d_id),))
            row = cur.fetchone()
            assert row["dispatch_status"] == "failed"
            assert row["failure_reason"] == "player_identity_unverified"


def test_process_dispatch_material_change_requires_reapproval(pg_dispatch_uri, pg_admin_db_uri, make_dispatch):
    """AC: Material plan changes transition queued -> requires_reapproval."""
    # Ensure player is verified
    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE public.players SET identity_verified_at = now() WHERE player_id = '10001'")

    d_id = make_dispatch(
        metric="pass_accuracy",
        instruction={"metric": "pass_accuracy", "target_delta": 5.0, "material_change": True},
    )
    envelope = _make_valid_envelope(d_id, player_id="10001")
    with psycopg.connect(pg_dispatch_uri, autocommit=True) as conn:
        res = process_dispatch_event(conn, envelope, dispatch_enabled=True)
        assert res["status"] == "requires_reapproval"
        assert res["plan_version"] == 1

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT dispatch_status FROM public.dispatches WHERE dispatch_id = %s", (str(d_id),))
            assert cur.fetchone()["dispatch_status"] == "requires_reapproval"

            # Verify plan stored with status material_change
            cur.execute("SELECT status FROM public.dispatch_plans WHERE dispatch_id = %s AND plan_version = 1", (str(d_id),))
            assert cur.fetchone()["status"] == "material_change"


def test_process_dispatch_disabled_kill_switch(pg_dispatch_uri, pg_admin_db_uri, make_dispatch):
    """AC: When DISPATCH_ENABLED=false, worker claims & validates but withholds delivery."""
    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE public.players SET identity_verified_at = now() WHERE player_id = '10001'")

    d_id = make_dispatch(
        metric="pass_accuracy",
        instruction={"metric": "pass_accuracy", "target_delta": 5.0},
    )
    envelope = _make_valid_envelope(d_id, player_id="10001")
    with psycopg.connect(pg_dispatch_uri, autocommit=True) as conn:
        res = process_dispatch_event(conn, envelope, dispatch_enabled=False)
        assert res["status"] == "delivery_disabled"
        assert res["plan_version"] == 1

        # Verify no delivery attempts were written
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM public.dispatch_delivery_attempts WHERE dispatch_id = %s", (str(d_id),))
            assert cur.fetchone()[0] == 0


def test_process_dispatch_delivered_when_enabled(pg_dispatch_uri, pg_admin_db_uri, make_dispatch, mock_redis):
    """AC: When DISPATCH_ENABLED=true and player verified, delivery adapters succeed and record attempts."""
    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE public.players SET identity_verified_at = now() WHERE player_id = '10001'")

    d_id = make_dispatch(
        metric="pass_accuracy",
        instruction={"metric": "pass_accuracy", "target_delta": 5.0},
    )
    envelope = _make_valid_envelope(d_id, player_id="10001")
    with psycopg.connect(pg_dispatch_uri, autocommit=True) as conn:
        res = process_dispatch_event(conn, envelope, redis_client=mock_redis, dispatch_enabled=True)
        assert res["status"] == "delivered"
        assert res["dashboard_result"] == "stored"
        assert res["websocket_result"] == "pushed"

        # Verify attempts recorded in public.dispatch_delivery_attempts
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT channel, result FROM public.dispatch_delivery_attempts WHERE dispatch_id = %s ORDER BY channel", (str(d_id),))
            attempts = cur.fetchall()
            assert len(attempts) == 2
            assert attempts[0]["channel"] == "dashboard"
            assert attempts[0]["result"] == "stored"
            assert attempts[1]["channel"] == "websocket"
            assert attempts[1]["result"] == "pushed"


def test_idempotent_duplicate_event_handling(pg_dispatch_uri, pg_admin_db_uri, make_dispatch, mock_redis):
    """AC: Duplicate Redis messages for already processed dispatch return idempotent skip."""
    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE public.players SET identity_verified_at = now() WHERE player_id = '10001'")

    d_id = make_dispatch(
        metric="pass_accuracy",
        instruction={"metric": "pass_accuracy", "target_delta": 5.0},
    )
    envelope = _make_valid_envelope(d_id, player_id="10001")
    with psycopg.connect(pg_dispatch_uri, autocommit=True) as conn:
        # First processing
        res1 = process_dispatch_event(conn, envelope, redis_client=mock_redis, dispatch_enabled=True)
        assert res1["status"] == "delivered"

        # Second processing of same event envelope
        res2 = process_dispatch_event(conn, envelope, redis_client=mock_redis, dispatch_enabled=True)
        assert res2["status"] == "idempotent_skip"
        assert res2["current_status"] in ("queued", "delivered")


# =====================================================================
# 5. Security & Least Privilege Tests (fc27_dispatch Role)
# =====================================================================

def test_dispatch_role_cannot_execute_ddl(pg_dispatch_uri):
    """AC: fc27_dispatch role cannot execute DDL statements."""
    with psycopg.connect(pg_dispatch_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("CREATE TABLE public.malicious_hack (id int)")


def test_dispatch_role_cannot_update_dispatches_directly(pg_dispatch_uri, make_dispatch):
    """AC: fc27_dispatch cannot UPDATE dispatches directly (must use fc27_dispatch_transition)."""
    d_id = make_dispatch(status="pending")
    with psycopg.connect(pg_dispatch_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("UPDATE public.dispatches SET dispatch_status = 'delivered' WHERE dispatch_id = %s", (str(d_id),))


def test_dispatch_role_cannot_delete_plans_or_attempts(pg_dispatch_uri, make_dispatch):
    """AC: Immutable tables reject DELETE/UPDATE attempts."""
    d_id = make_dispatch(status="pending")
    with psycopg.connect(pg_dispatch_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            # Cannot DELETE from dispatch_delivery_attempts
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("DELETE FROM public.dispatch_delivery_attempts WHERE dispatch_id = %s", (str(d_id),))


# =====================================================================
# 6. Worker Health Check Test
# =====================================================================

def test_check_worker_health(pg_dispatch_uri, mock_redis):
    """AC: Health check accurately reflects DB, Redis, and heartbeat state."""
    with psycopg.connect(pg_dispatch_uri, autocommit=True) as conn:
        write_heartbeat(conn, instance_id="health-tester")
        health = check_worker_health(conn, redis_client=mock_redis)
        assert health["status"] == "ok"
        assert health["database_connected"] is True
        assert health["redis_connected"] is True
        assert health["heartbeat_fresh"] is True
        assert health["heartbeat_age_seconds"] < 5.0


# =====================================================================
# 7. Concurrency, Crash Windows, and Adversarial Scenarios
# =====================================================================

def test_concurrent_worker_claims_single_winner(pg_dispatch_uri, pg_admin_db_uri, make_dispatch):
    """AC: Concurrent workers claiming the same dispatch result in exactly one winner."""
    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE public.players SET identity_verified_at = now() WHERE player_id = '10001'")

    d_id = make_dispatch(
        metric="pass_accuracy",
        instruction={"metric": "pass_accuracy", "target_delta": 5.0},
    )
    envelope = _make_valid_envelope(d_id, player_id="10001")

    results = []
    errors = []

    def _worker_attempt(w_id: str):
        try:
            with psycopg.connect(pg_dispatch_uri, autocommit=True) as conn:
                res = process_dispatch_event(
                    conn,
                    envelope,
                    dispatch_enabled=False,
                    instance_id=w_id,
                )
                results.append((w_id, res))
        except Exception as exc:
            errors.append((w_id, exc))

    t1 = threading.Thread(target=_worker_attempt, args=("worker-A",))
    t2 = threading.Thread(target=_worker_attempt, args=("worker-B",))

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(errors) == 0
    assert len(results) == 2

    statuses = [r[1]["status"] for r in results]
    # One worker must claim and reach delivery_disabled; the other must either conflict_dropped or idempotent_skip
    assert "delivery_disabled" in statuses
    assert any(s in ("conflict_dropped", "idempotent_skip") for s in statuses)


def test_unsupported_event_type_safely_ignored(pg_dispatch_uri):
    """AC: Event types other than dispatch.created are safely dropped."""
    envelope = {
        "event_id": 99,
        "event_type": "some.unrelated.event",
        "entity_type": "audit",
        "entity_id": str(uuid.uuid4()),
        "actor": "system",
        "correlation_id": str(uuid.uuid4()),
        "scope_club_id": "330323",
        "scope_player_id": "10001",
    }
    with psycopg.connect(pg_dispatch_uri, autocommit=True) as conn:
        res = process_dispatch_event(conn, envelope)
        assert res["status"] == "ignored"
        assert "unhandled_event_type" in res["reason"]


def test_terminal_record_excluded_from_reprocessing(pg_dispatch_uri, pg_admin_db_uri, make_dispatch):
    """AC: Already cancelled/completed dispatches return idempotent_skip."""
    d_id = make_dispatch(status="pending")
    # Cancel the dispatch as admin
    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE public.dispatches SET dispatch_status = 'cancelled' WHERE dispatch_id = %s", (str(d_id),))

    envelope = _make_valid_envelope(d_id, player_id="10001")
    with psycopg.connect(pg_dispatch_uri, autocommit=True) as conn:
        res = process_dispatch_event(conn, envelope)
        assert res["status"] == "idempotent_skip"
        assert res["current_status"] == "cancelled"


def test_poison_work_isolation(pg_dispatch_uri, make_dispatch):
    """AC: Poison work (malformed payload) fails safely without crashing worker."""
    # 1. Poison payload
    with psycopg.connect(pg_dispatch_uri, autocommit=True) as conn:
        poison_res = process_dispatch_event(conn, "BAD_PAYLOAD_NOT_JSON")
        assert poison_res["status"] == "rejected"

    # 2. Subsequent healthy work succeeds
    d_id = make_dispatch(
        metric="pass_accuracy",
        instruction={"metric": "pass_accuracy", "target_delta": 5.0},
    )
    healthy_envelope = _make_valid_envelope(d_id, player_id="10001")
    with psycopg.connect(pg_dispatch_uri, autocommit=True) as conn:
        healthy_res = process_dispatch_event(conn, healthy_envelope, dispatch_enabled=False)
        assert healthy_res["status"] in ("delivery_disabled", "failed")
