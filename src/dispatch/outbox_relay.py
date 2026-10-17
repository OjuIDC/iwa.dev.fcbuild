"""Transactional outbox relay.

Polls ``public.outbox``, claims rows safely with per-instance lease ownership,
publishes the full domain event envelope to the internal Redis channel (``fc27:events``),
and records owner-bound publication acknowledgement.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
import uuid

import psycopg
import redis

logger = logging.getLogger("fc27.outbox_relay")

BATCH_SIZE = int(os.getenv("OUTBOX_RELAY_BATCH_SIZE", "50"))
POLL_INTERVAL = float(os.getenv("OUTBOX_RELAY_POLL_INTERVAL", "1.0"))
CLAIM_TTL = int(os.getenv("OUTBOX_RELAY_CLAIM_TTL", "30"))
MAX_ATTEMPTS = int(os.getenv("OUTBOX_RELAY_MAX_ATTEMPTS", "10"))
DEFAULT_INSTANCE_ID = os.getenv("OUTBOX_RELAY_INSTANCE_ID") or f"relay-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _pg_uri() -> str:
    f = os.getenv("POSTGRES_URI_FILE")
    if f and os.path.exists(f):
        with open(f, encoding="utf-8") as fh:
            return fh.read().strip()
    return os.getenv("POSTGRES_URI", "")


def _redis() -> redis.Redis:
    url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    return redis.from_url(url)


def serialize_event_envelope(row: dict) -> dict:
    """Transforms a domain_events database row into the approved internal event envelope."""
    occurred_at = row.get("occurred_at")
    if hasattr(occurred_at, "isoformat"):
        occurred_at_str = occurred_at.isoformat()
    else:
        occurred_at_str = str(occurred_at) if occurred_at is not None else None

    return {
        "outbox_id": row["outbox_id"],
        "channel": row.get("channel") or "fc27:events",
        "domain_event_id": row["event_id"],
        "event_id": row["event_id"],
        "event_type": row["event_type"],
        "entity_type": row["entity_type"],
        "entity_id": str(row["entity_id"]) if row.get("entity_id") is not None else "",
        "entity_version": row.get("entity_version"),
        "actor": row["actor"],
        "from_status": row.get("from_status"),
        "to_status": row.get("to_status"),
        "reason": row.get("reason"),
        "correlation_id": str(row["correlation_id"]) if row.get("correlation_id") is not None else None,
        "occurred_at": occurred_at_str,
        "source_table": row.get("source_table"),
        "source_id": str(row["source_id"]) if row.get("source_id") is not None else None,
        "scope_club_id": str(row["scope_club_id"]) if row.get("scope_club_id") is not None else None,
        "scope_player_id": str(row["scope_player_id"]) if row.get("scope_player_id") is not None else None,
        "visibility": row["visibility"],
        "fc_version": row.get("fc_version") or "27",
        "platform": row.get("platform") or "common-gen5",
        "policy_version": row.get("policy_version"),
        "code_revision": row.get("code_revision"),
    }


def claim_batch(
    conn: psycopg.Connection,
    instance_id: str = DEFAULT_INSTANCE_ID,
    batch_size: int = BATCH_SIZE,
    claim_ttl: int = CLAIM_TTL,
    max_attempts: int = MAX_ATTEMPTS,
) -> list[dict]:
    """Claims up to batch_size pending outbox rows atomically with lease ownership."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT o.outbox_id, o.channel,
                   d.event_id, d.event_type, d.entity_type, d.entity_id, d.entity_version,
                   d.actor, d.from_status, d.to_status, d.reason, d.correlation_id,
                   d.occurred_at, d.source_table, d.source_id, d.scope_club_id,
                   d.scope_player_id, d.visibility, d.fc_version, d.platform,
                   d.policy_version, d.code_revision
            FROM public.outbox o
            JOIN public.domain_events d ON d.event_id = o.domain_event_id
            WHERE o.published_at IS NULL
              AND (o.locked_until IS NULL OR o.locked_until < now())
              AND o.attempts < %s
            ORDER BY o.outbox_id
            FOR UPDATE OF o SKIP LOCKED
            LIMIT %s
            """,
            (max_attempts, batch_size),
        )
        rows = cur.fetchall()
        if not rows:
            conn.commit()
            return []

        outbox_ids = [r[0] for r in rows]
        cur.execute(
            """
            UPDATE public.outbox
            SET locked_until = now() + (interval '1 second' * %s),
                locked_by = %s,
                attempts = attempts + 1
            WHERE outbox_id = ANY(%s)
            """,
            (claim_ttl, instance_id, outbox_ids),
        )
        conn.commit()

        claimed = []
        for r in rows:
            row_dict = {
                "outbox_id": r[0],
                "channel": r[1],
                "event_id": r[2],
                "event_type": r[3],
                "entity_type": r[4],
                "entity_id": r[5],
                "entity_version": r[6],
                "actor": r[7],
                "from_status": r[8],
                "to_status": r[9],
                "reason": r[10],
                "correlation_id": r[11],
                "occurred_at": r[12],
                "source_table": r[13],
                "source_id": r[14],
                "scope_club_id": r[15],
                "scope_player_id": r[16],
                "visibility": r[17],
                "fc_version": r[18],
                "platform": r[19],
                "policy_version": r[20],
                "code_revision": r[21],
            }
            claimed.append(serialize_event_envelope(row_dict))
        return claimed


def acknowledge(
    conn: psycopg.Connection,
    outbox_id: int,
    claim_owner: str = DEFAULT_INSTANCE_ID,
) -> bool:
    """Marks outbox row published ONLY if owned by claim_owner with an active lease."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE public.outbox
            SET published_at = now(),
                locked_until = NULL,
                locked_by = NULL
            WHERE outbox_id = %s
              AND locked_by = %s
              AND locked_until >= now()
              AND published_at IS NULL
            """,
            (outbox_id, claim_owner),
        )
        success = (cur.rowcount == 1)
    conn.commit()
    if not success:
        logger.warning(
            "outbox_relay_acknowledgement_rejected",
            extra={
                "outbox_id": outbox_id,
                "claim_owner": claim_owner,
                "reason": "ownership_lost_or_expired_or_already_published",
            },
        )
    return success


def record_failure(
    conn: psycopg.Connection,
    outbox_id: int,
    error: str,
    claim_owner: str = DEFAULT_INSTANCE_ID,
    backoff_seconds: int = 5,
) -> bool:
    """Records failure category and releases owner while setting retry backoff lease."""
    safe_error = (error[:200] if error else "unknown_error").strip()
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE public.outbox
            SET last_error = %s,
                locked_until = now() + (interval '1 second' * %s),
                locked_by = NULL
            WHERE outbox_id = %s
              AND locked_by = %s
              AND published_at IS NULL
            """,
            (safe_error, backoff_seconds, outbox_id, claim_owner),
        )
        success = (cur.rowcount == 1)
    conn.commit()
    return success


def publish_one(r: redis.Redis, item: dict, channel: str = "fc27:events") -> bool:
    """Publishes a single event envelope to internal Redis pub/sub."""
    try:
        payload = json.dumps(item, sort_keys=True, default=str)
        r.publish(channel, payload)
        return True
    except redis.RedisError as exc:
        logger.error(
            "outbox_relay_redis_publish_error",
            extra={
                "outbox_id": item.get("outbox_id"),
                "error": type(exc).__name__,
            },
        )
        return False


def run_once(
    pg_uri: str | None = None,
    redis_client: redis.Redis | None = None,
    instance_id: str = DEFAULT_INSTANCE_ID,
    batch_size: int = BATCH_SIZE,
) -> int:
    """Performs one poll, claim, publish, and acknowledge cycle."""
    uri = pg_uri or _pg_uri()
    if not uri:
        logger.error("outbox_relay_no_postgres_uri")
        return 0

    conn = psycopg.connect(uri)
    r = redis_client if redis_client is not None else _redis()
    published = 0
    try:
        items = claim_batch(conn, instance_id=instance_id, batch_size=batch_size)
        for item in items:
            outbox_id = item["outbox_id"]
            channel = item.get("channel") or "fc27:events"
            if publish_one(r, item, channel=channel):
                if acknowledge(conn, outbox_id, claim_owner=instance_id):
                    published += 1
                else:
                    logger.warning(
                        "outbox_relay_publish_succeeded_but_ack_rejected",
                        extra={"outbox_id": outbox_id, "instance_id": instance_id},
                    )
            else:
                record_failure(conn, outbox_id, "redis_publish_error", claim_owner=instance_id)
    finally:
        conn.close()
    return published


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("outbox_relay_starting", extra={"instance_id": DEFAULT_INSTANCE_ID})

    try:
        with psycopg.connect(_pg_uri(), connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_user")
                role = cur.fetchone()[0]
                logger.info("outbox_relay_connected_db", extra={"role": role})
    except Exception as exc:
        logger.error("outbox_relay_db_startup_check_failed", extra={"error": type(exc).__name__})
        time.sleep(2)

    stop = False

    def _sig_handler(signum, frame):
        nonlocal stop
        stop = True
        logger.info("outbox_relay_stopping")

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    while not stop:
        try:
            count = run_once(instance_id=DEFAULT_INSTANCE_ID)
            if count == 0:
                time.sleep(POLL_INTERVAL)
        except Exception as exc:
            logger.error("outbox_relay_loop_error", extra={"error": type(exc).__name__})
            time.sleep(min(POLL_INTERVAL * 2, 5.0))


if __name__ == "__main__":
    main()
