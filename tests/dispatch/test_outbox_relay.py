"""Package 2 transactional outbox relay tests.

Verifies claim ownership, lease expiration, owner-bound acknowledgement,
Redis publication to fc27:events, error handling, crash windows,
and least-privilege security of the fc27_relay role.
"""
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import dotenv
import psycopg
import pytest
import redis

from src.dispatch.outbox_relay import (
    BATCH_SIZE,
    CLAIM_TTL,
    MAX_ATTEMPTS,
    acknowledge,
    claim_batch,
    publish_one,
    record_failure,
    run_once,
    serialize_event_envelope,
)
from src.events import record_event


@pytest.fixture
def redis_conn():
    """Connects to real Redis if available, or provides an isolated in-memory test client."""
    dotenv_vals = dotenv.dotenv_values(".env") if os.path.exists(".env") else {}
    pw = os.getenv("REDIS_PASSWORD") or dotenv_vals.get("REDIS_PASSWORD")
    try:
        r = redis.Redis(host="127.0.0.1", port=6379, password=pw, decode_responses=True)
        r.ping()
        yield r
    except Exception:
        # Isolated test Redis
        class InMemoryRedis:
            def __init__(self):
                self.messages = []
            def publish(self, channel, message):
                self.messages.append((channel, message))
                return 1
            def ping(self):
                return True
        yield InMemoryRedis()


def _seed_outbox_event(conn, event_type="player.verified", entity_id="player-101", club_id="330323"):
    """Helper to seed domain_event + outbox record using the production record_event pipeline."""
    corr_id = uuid.uuid4()
    event_id = record_event(
        conn,
        event_type=event_type,
        entity_type="player",
        entity_id=entity_id,
        actor="commissioner-1",
        correlation_id=corr_id,
        visibility="staff",
        scope_club_id=club_id,
        scope_player_id=entity_id,
    )
    with conn.cursor() as cur:
        cur.execute("SELECT outbox_id FROM public.outbox WHERE domain_event_id = %s", (event_id,))
        outbox_id = cur.fetchone()[0]
    return outbox_id, event_id, corr_id


# =====================================================================
# 1. Schema and Envelope
# =====================================================================

def test_claim_query_uses_only_existing_columns(pg_db_migrated):
    """AC: Claim query selects only existing columns in public.domain_events and public.outbox."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        _seed_outbox_event(conn)
        # claim_batch must execute without UndefinedColumn or SQL syntax error
        claimed = claim_batch(conn, instance_id="test-runner", batch_size=10)
        assert len(claimed) >= 1
        item = claimed[0]
        assert "payload" not in item
        assert "event_id" in item
        assert "domain_event_id" in item


def test_valid_domain_event_becomes_valid_internal_envelope(pg_db_migrated):
    """AC: Approved internal event envelope contains all contract-required fields."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        outbox_id, event_id, corr_id = _seed_outbox_event(conn, event_type="player.verified")
        claimed = claim_batch(conn, instance_id="relay-inst-1", batch_size=5)
        item = next(c for c in claimed if c["outbox_id"] == outbox_id)

        # Mandatory fields
        assert item["outbox_id"] == outbox_id
        assert item["channel"] == "fc27:events"
        assert item["domain_event_id"] == event_id
        assert item["event_id"] == event_id
        assert item["event_type"] == "player.verified"
        assert item["entity_type"] == "player"
        assert item["entity_id"] == "player-101"
        assert item["actor"] == "commissioner-1"
        assert item["correlation_id"] == str(corr_id)
        assert item["scope_club_id"] == "330323"
        assert item["scope_player_id"] == "player-101"
        assert item["visibility"] == "staff"
        assert item["fc_version"] == "27"
        assert item["platform"] == "common-gen5"
        assert isinstance(item["occurred_at"], str)


def test_uuids_and_timestamps_serialize_deterministically():
    """AC: Deterministic JSON serialization with safely stringified UUIDs and timestamps."""
    raw = {
        "outbox_id": 42,
        "channel": "fc27:events",
        "event_id": 100,
        "event_type": "audit.test",
        "entity_type": "system",
        "entity_id": "sys-1",
        "entity_version": 1,
        "actor": "admin",
        "from_status": None,
        "to_status": None,
        "reason": None,
        "correlation_id": uuid.UUID("12345678-1234-5678-1234-567812345678"),
        "occurred_at": datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc),
        "source_table": None,
        "source_id": None,
        "scope_club_id": "330323",
        "scope_player_id": None,
        "visibility": "ops",
        "fc_version": "27",
        "platform": "common-gen5",
        "policy_version": None,
        "code_revision": None,
    }
    envelope = serialize_event_envelope(raw)
    json_str = json.dumps(envelope, sort_keys=True)
    loaded = json.loads(json_str)
    assert loaded["correlation_id"] == "12345678-1234-5678-1234-567812345678"
    assert "2026-10-17" in loaded["occurred_at"]


# =====================================================================
# 2. Claiming and Lease Ownership
# =====================================================================

def test_pending_row_can_be_claimed_and_persists_owner(pg_db_migrated):
    """AC: A pending row is claimed with stable instance ID and active lease expiry."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        outbox_id, _, _ = _seed_outbox_event(conn)
        claimed = claim_batch(conn, instance_id="instance-alpha", claim_ttl=60)
        assert any(c["outbox_id"] == outbox_id for c in claimed)

        with conn.cursor() as cur:
            cur.execute("SELECT locked_by, locked_until, attempts FROM public.outbox WHERE outbox_id = %s", (outbox_id,))
            locked_by, locked_until, attempts = cur.fetchone()
            assert locked_by == "instance-alpha"
            assert locked_until is not None
            assert attempts == 1


def test_published_row_is_excluded_from_claim(pg_db_migrated):
    """AC: Published outbox records (published_at IS NOT NULL) are excluded from claiming."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        outbox_id, _, _ = _seed_outbox_event(conn)
        # Mark as published
        with conn.cursor() as cur:
            cur.execute("UPDATE public.outbox SET published_at = now() WHERE outbox_id = %s", (outbox_id,))
        conn.commit()

        claimed = claim_batch(conn, instance_id="instance-alpha")
        assert not any(c["outbox_id"] == outbox_id for c in claimed)


def test_active_lease_is_excluded_from_concurrent_claim(pg_db_migrated):
    """AC: A row currently leased by instance A cannot be claimed by instance B."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        outbox_id, _, _ = _seed_outbox_event(conn)
        claimed_a = claim_batch(conn, instance_id="relay-A", claim_ttl=120)
        assert any(c["outbox_id"] == outbox_id for c in claimed_a)

        # Instance B attempts claim while lease is active
        claimed_b = claim_batch(conn, instance_id="relay-B", claim_ttl=120)
        assert not any(c["outbox_id"] == outbox_id for c in claimed_b)


def test_stale_lease_is_reclaimable(pg_db_migrated):
    """AC: When locked_until < now(), another instance can reclaim the record."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        outbox_id, _, _ = _seed_outbox_event(conn)
        # Claim with expired lease
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE public.outbox SET locked_by = 'relay-dead', locked_until = now() - interval '10 seconds' WHERE outbox_id = %s",
                (outbox_id,),
            )
        conn.commit()

        claimed = claim_batch(conn, instance_id="relay-alive", claim_ttl=60)
        assert any(c["outbox_id"] == outbox_id for c in claimed)

        with conn.cursor() as cur:
            cur.execute("SELECT locked_by FROM public.outbox WHERE outbox_id = %s", (outbox_id,))
            assert cur.fetchone()[0] == "relay-alive"


def test_batch_limit_and_deterministic_order_enforced(pg_db_migrated):
    """AC: Claiming respects batch limit and orders by outbox_id ascending."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        ids = []
        for _ in range(5):
            oid, _, _ = _seed_outbox_event(conn)
            ids.append(oid)

        claimed = claim_batch(conn, instance_id="batch-tester", batch_size=3)
        assert len(claimed) == 3
        claimed_ids = [c["outbox_id"] for c in claimed]
        assert claimed_ids == sorted(claimed_ids)


def test_concurrent_relays_claim_disjoint_rows(pg_db_migrated):
    """AC: Two concurrent relay instances running claim_batch receive mutually disjoint rows."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        for _ in range(10):
            _seed_outbox_event(conn)

    results_a = []
    results_b = []

    def _worker(inst_id, out_list):
        with psycopg.connect(pg_db_migrated.admin) as c:
            claimed = claim_batch(c, instance_id=inst_id, batch_size=5)
            out_list.extend([item["outbox_id"] for item in claimed])

    t1 = threading.Thread(target=_worker, args=("worker-A", results_a))
    t2 = threading.Thread(target=_worker, args=("worker-B", results_b))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Disjointness check
    set_a = set(results_a)
    set_b = set(results_b)
    overlap = set_a & set_b
    assert not overlap, f"Concurrency violation: relays claimed overlapping rows: {overlap}"


# =====================================================================
# 3. Acknowledgement Semantics
# =====================================================================

def test_correct_owner_acknowledges_after_publication(pg_db_migrated):
    """AC: The active owner can successfully acknowledge publication."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        outbox_id, _, _ = _seed_outbox_event(conn)
        claim_batch(conn, instance_id="owner-1", claim_ttl=60)

        ok = acknowledge(conn, outbox_id=outbox_id, claim_owner="owner-1")
        assert ok is True

        with conn.cursor() as cur:
            cur.execute("SELECT published_at, locked_by, locked_until FROM public.outbox WHERE outbox_id = %s", (outbox_id,))
            pub_at, locked_by, locked_until = cur.fetchone()
            assert pub_at is not None
            assert locked_by is None
            assert locked_until is None


def test_wrong_owner_acknowledgement_rejected(pg_db_migrated):
    """AC: A relay instance that does not own the claim cannot acknowledge the record."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        outbox_id, _, _ = _seed_outbox_event(conn)
        claim_batch(conn, instance_id="legitimate-owner", claim_ttl=60)

        # Imposter attempts acknowledgement
        ok = acknowledge(conn, outbox_id=outbox_id, claim_owner="imposter-relay")
        assert ok is False

        # Row remains unpublished
        with conn.cursor() as cur:
            cur.execute("SELECT published_at, locked_by FROM public.outbox WHERE outbox_id = %s", (outbox_id,))
            pub_at, locked_by = cur.fetchone()
            assert pub_at is None
            assert locked_by == "legitimate-owner"


def test_expired_owner_acknowledgement_rejected(pg_db_migrated):
    """AC: An expired owner whose lease lapsed and was reclaimed cannot acknowledge."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        outbox_id, _, _ = _seed_outbox_event(conn)
        claim_batch(conn, instance_id="slow-relay", claim_ttl=60)

        # Force lease expiry and reclamation by another relay
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE public.outbox SET locked_by = 'fast-relay', locked_until = now() + interval '60 seconds' WHERE outbox_id = %s",
                (outbox_id,),
            )
        conn.commit()

        # Slow relay finally wakes up and tries to acknowledge
        ok = acknowledge(conn, outbox_id=outbox_id, claim_owner="slow-relay")
        assert ok is False

        # Still owned by fast-relay and unpublished
        with conn.cursor() as cur:
            cur.execute("SELECT published_at, locked_by FROM public.outbox WHERE outbox_id = %s", (outbox_id,))
            pub_at, locked_by = cur.fetchone()
            assert pub_at is None
            assert locked_by == "fast-relay"


def test_missing_or_already_published_ack_returns_false(pg_db_migrated):
    """AC: Acknowledging a non-existent or already published record returns False."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        # Nonexistent outbox_id
        assert acknowledge(conn, outbox_id=99999999, claim_owner="any") is False

        # Already published
        outbox_id, _, _ = _seed_outbox_event(conn)
        claim_batch(conn, instance_id="owner-x", claim_ttl=60)
        assert acknowledge(conn, outbox_id=outbox_id, claim_owner="owner-x") is True
        # Second ack fails closed
        assert acknowledge(conn, outbox_id=outbox_id, claim_owner="owner-x") is False


# =====================================================================
# 4. Failure, Retry, and Observability
# =====================================================================

def test_redis_failure_leaves_row_unpublished_and_records_retry(pg_db_migrated):
    """AC: When Redis publication fails, row remains unpublished, attempts increment, and error is recorded."""
    failing_redis = MagicMock()
    failing_redis.publish.side_effect = redis.ConnectionError("Redis connection refused")

    with psycopg.connect(pg_db_migrated.admin) as conn:
        outbox_id, _, _ = _seed_outbox_event(conn)

    # Execute run_once with failing redis
    published = run_once(pg_uri=pg_db_migrated.admin, redis_client=failing_redis, instance_id="fail-tester")
    assert published == 0

    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT published_at, attempts, last_error, locked_by FROM public.outbox WHERE outbox_id = %s", (outbox_id,))
            pub_at, attempts, last_error, locked_by = cur.fetchone()
            assert pub_at is None
            assert attempts == 1
            assert "redis_publish_error" in (last_error or "")
            # Lock is released so it can be retried after backoff
            assert locked_by is None


def test_exhausted_record_is_excluded(pg_db_migrated):
    """AC: When attempts >= MAX_ATTEMPTS, record is excluded from claiming."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        outbox_id, _, _ = _seed_outbox_event(conn)
        with conn.cursor() as cur:
            cur.execute("UPDATE public.outbox SET attempts = %s WHERE outbox_id = %s", (MAX_ATTEMPTS, outbox_id))
        conn.commit()

        claimed = claim_batch(conn, instance_id="retry-exhaustion-check", max_attempts=MAX_ATTEMPTS)
        assert not any(c["outbox_id"] == outbox_id for c in claimed)


def test_publish_one_success_and_ack_order(pg_db_migrated, redis_conn):
    """AC: Publish to Redis succeeds first, then acknowledgement occurs."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        outbox_id, _, _ = _seed_outbox_event(conn)

    published = run_once(pg_uri=pg_db_migrated.admin, redis_client=redis_conn, instance_id="pub-success-test")
    assert published >= 1

    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT published_at FROM public.outbox WHERE outbox_id = %s", (outbox_id,))
            assert cur.fetchone()[0] is not None


# =====================================================================
# 5. Least-Privilege Role Security (fc27_relay)
# =====================================================================

def test_relay_role_cannot_execute_ddl(pg_db_migrated):
    """AC: fc27_relay cannot run DDL (CREATE TABLE, DROP TABLE, ALTER)."""
    with psycopg.connect(pg_db_migrated.relay) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.cursor() as cur:
                cur.execute("CREATE TABLE public.tamper (id int)")


def test_relay_role_cannot_modify_unrelated_tables(pg_db_migrated):
    """AC: fc27_relay has zero write access on approval, player, or identity tables."""
    with psycopg.connect(pg_db_migrated.relay, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.cursor() as cur:
                cur.execute("INSERT INTO public.players (player_id, display_name) VALUES ('p-bad', 'Bad')")

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.cursor() as cur:
                cur.execute("DELETE FROM public.approval_requests")


def test_relay_role_cannot_insert_or_delete_outbox(pg_db_migrated):
    """AC: fc27_relay has SELECT and column-level UPDATE on outbox, cannot INSERT or DELETE."""
    with psycopg.connect(pg_db_migrated.relay, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.cursor() as cur:
                cur.execute("INSERT INTO public.outbox (domain_event_id) VALUES (1)")

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.cursor() as cur:
                cur.execute("DELETE FROM public.outbox")


def test_relay_role_can_claim_and_acknowledge_outbox(pg_db_migrated):
    """AC: fc27_relay can SELECT domain_events and outbox, and UPDATE required outbox lease/publish fields."""
    with psycopg.connect(pg_db_migrated.admin) as admin_conn:
        outbox_id, _, _ = _seed_outbox_event(admin_conn)

    with psycopg.connect(pg_db_migrated.relay) as relay_conn:
        claimed = claim_batch(relay_conn, instance_id="relay-role-tester")
        assert any(c["outbox_id"] == outbox_id for c in claimed)
        ok = acknowledge(relay_conn, outbox_id=outbox_id, claim_owner="relay-role-tester")
        assert ok is True
