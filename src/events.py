import uuid
from typing import Literal, Optional, Any
from psycopg import Connection

def record_event(
    conn: Connection,
    *,
    event_type: str,
    entity_type: str,
    entity_id: str,
    actor: str,
    correlation_id: uuid.UUID,
    visibility: Literal['staff', 'player_owner', 'audit_only', 'ops'],
    scope_club_id: Optional[str],
    scope_player_id: Optional[str] = None,
    entity_version: Optional[int] = None,
    from_status: Optional[str] = None,
    to_status: Optional[str] = None,
    reason: Optional[str] = None,
    source_table: Optional[str] = None,
    source_id: Optional[str] = None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.domain_events (
                event_type, entity_type, entity_id, entity_version, actor,
                from_status, to_status, reason, correlation_id,
                source_table, source_id, scope_club_id, scope_player_id,
                visibility, fc_version, platform
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, '27', 'common-gen5'
            ) RETURNING event_id
            """,
            (
                event_type, entity_type, entity_id, entity_version, actor,
                from_status, to_status, reason, correlation_id,
                source_table, source_id, scope_club_id, scope_player_id,
                visibility
            )
        )
        row = cur.fetchone()
        assert row is not None
        event_id = row[0]

        cur.execute(
            """
            INSERT INTO public.outbox (domain_event_id, channel)
            VALUES (%s, 'fc27:events')
            """,
            (event_id,)
        )
        return event_id
