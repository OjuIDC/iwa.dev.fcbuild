"""Transactional outbox relay.

Polls ``public.outbox``, claims rows safely, publishes the corresponding
domain event payload to the internal Redis channel, and records publication.
"""

import json
import os
import time
import uuid

import psycopg
import redis

BATCH_SIZE = int(os.getenv("OUTBOX_RELAY_BATCH_SIZE", "50"))
POLL_INTERVAL = float(os.getenv("OUTBOX_RELAY_POLL_INTERVAL", "1.0"))
CLAIM_TTL = int(os.getenv("OUTBOX_RELAY_CLAIM_TTL", "30"))
MAX_ATTEMPTS = int(os.getenv("OUTBOX_RELAY_MAX_ATTEMPTS", "10"))


def _pg_uri() -> str:
    f = os.getenv("POSTGRES_URI_FILE")
    if f:
        with open(f) as fh:
            return fh.read().strip()
    return os.getenv("POSTGRES_URI", "")


def _redis() -> redis.Redis:
    return redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))


def claim_batch(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT o.outbox_id, o.domain_event_id, d.event_type, d.entity_type,
                   d.entity_id, d.actor, d.correlation_id, d.scope_club_id,
                   d.scope_player_id, d.payload
            FROM public.outbox o
            JOIN public.domain_events d ON d.event_id = o.domain_event_id
            WHERE o.published_at IS NULL
              AND (o.locked_until IS NULL OR o.locked_until < now())
              AND o.attempts < %s
            ORDER BY o.outbox_id
            FOR UPDATE SKIP LOCKED
            LIMIT %s
            """,
            (MAX_ATTEMPTS, BATCH_SIZE),
        )
        rows = cur.fetchall()
        claimed = []
        for r in rows:
            cur.execute(
                "UPDATE public.outbox SET locked_until = now() + interval '%s seconds', attempts = attempts + 1 WHERE outbox_id = %s",
                (CLAIM_TTL, r[0]),
            )
            claimed.append({
                "outbox_id": r[0],
                "domain_event_id": r[1],
                "event_type": r[2],
                "entity_type": r[3],
                "entity_id": r[4],
                "actor": r[5],
                "correlation_id": str(r[6]) if r[6] else None,
                "scope_club_id": r[7],
                "scope_player_id": r[8],
            })
        conn.commit()
        return claimed


def publish_one(r: redis.Redis, item: dict) -> bool:
    try:
        r.publish("fc27:events", json.dumps(item))
        return True
    except Exception:
        return False


def acknowledge(conn: psycopg.Connection, outbox_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE public.outbox SET published_at = now(), locked_until = NULL WHERE outbox_id = %s",
            (outbox_id,),
        )
    conn.commit()


def record_failure(conn: psycopg.Connection, outbox_id: int, error: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE public.outbox SET last_error = %s, locked_until = NULL WHERE outbox_id = %s",
            (error[:500], outbox_id),
        )
    conn.commit()


def run_once() -> int:
    published = 0
    conn = psycopg.connect(_pg_uri())
    r = _redis()
    try:
        items = claim_batch(conn)
        for item in items:
            if publish_one(r, item):
                acknowledge(conn, item["outbox_id"])
                published += 1
            else:
                record_failure(conn, item["outbox_id"], "redis publish failed")
    finally:
        conn.close()
    return published


def main() -> None:
    while True:
        try:
            run_once()
        except Exception:
            pass
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
