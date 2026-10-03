import uuid
import pytest
import psycopg
from src.events import record_event

def test_record_event_writes_one_outbox_row(pg_uri, pg_admin_db_uri):
    # count/inspect outbox via admin: channel 'fc27:events', published_at NULL, attempts 0
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        event_id = record_event(
            conn,
            event_type='system.started',
            entity_type='system',
            entity_id='sys-1',
            actor='system',
            correlation_id=uuid.uuid4(),
            visibility='ops',
            scope_club_id=None
        )

    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT channel, published_at, attempts FROM public.outbox WHERE domain_event_id = %s", (event_id,))
            row = cur.fetchone()
            assert row is not None
            assert row[0] == 'fc27:events'
            assert row[1] is None
            assert row[2] == 0

def test_event_scope_tags_default(pg_uri):
    # fc_version '27', platform 'common-gen5'
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        event_id = record_event(
            conn,
            event_type='system.ping',
            entity_type='system',
            entity_id='sys-1',
            actor='system',
            correlation_id=uuid.uuid4(),
            visibility='ops',
            scope_club_id=None
        )
        with conn.cursor() as cur:
            cur.execute("SELECT fc_version, platform FROM public.domain_events WHERE event_id = %s", (event_id,))
            row = cur.fetchone()
            assert row is not None
            assert row[0] == '27'
            assert row[1] == 'common-gen5'

def test_domain_events_append_only(pg_uri, pg_admin_db_uri):
    # UPDATE / DELETE -> CheckViolation 'append-only'
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        event_id = record_event(
            conn,
            event_type='system.degraded',
            entity_type='system',
            entity_id='sys-1',
            actor='system',
            correlation_id=uuid.uuid4(),
            visibility='ops',
            scope_club_id=None
        )
    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.CheckViolation, match="domain_events is append-only"):
                cur.execute("UPDATE public.domain_events SET reason = 'test' WHERE event_id = %s", (event_id,))
            with pytest.raises(psycopg.errors.CheckViolation, match="domain_events is append-only"):
                cur.execute("DELETE FROM public.domain_events WHERE event_id = %s", (event_id,))

def test_approval_audit_append_only(pg_uri, pg_admin_db_uri):
    # decide once; UPDATE approval_audit -> CheckViolation
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO public.graph_threads (thread_id, state, status, club_id) VALUES ('thread-1', '{}', 'running', '123')")
            cur.execute("INSERT INTO public.approval_audit (thread_id, commissioner, decision, notes, owner, selection, candidate_id, player_id, approved_by) VALUES ('thread-1', 'admin', 'APPROVE', '', 'owner1', 'sel', 'cand', 'p1', 'sys') RETURNING id")
            audit_id = cur.fetchone()[0]

    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.CheckViolation, match="approval_audit is append-only"):
                cur.execute("UPDATE public.approval_audit SET notes = 'tampered' WHERE id = %s", (audit_id,))

def test_sourced_event_requires_existing_source(pg_uri):
    # AC-42 (data): approval.decided with source_id '999999' -> raises;
    # with source_table='dispatches' -> raises (wrong table); with no source -> raises
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="source row not found or entity mismatch"):
            record_event(
                conn,
                event_type='approval.decided',
                entity_type='thread',
                entity_id='thread-1',
                actor='comm',
                correlation_id=uuid.uuid4(),
                visibility='ops',
                scope_club_id=None,
                source_table='approval_audit',
                source_id='999999'
            )
        with pytest.raises(psycopg.errors.RaiseException, match="approval.decided requires source_table = approval_audit"):
            record_event(
                conn,
                event_type='approval.decided',
                entity_type='thread',
                entity_id='thread-1',
                actor='comm',
                correlation_id=uuid.uuid4(),
                visibility='ops',
                scope_club_id=None,
                source_table='dispatches',
                source_id='999999'
            )
        with pytest.raises(psycopg.errors.RaiseException, match="approval.decided requires a source_id"):
            record_event(
                conn,
                event_type='approval.decided',
                entity_type='thread',
                entity_id='thread-1',
                actor='comm',
                correlation_id=uuid.uuid4(),
                visibility='ops',
                scope_club_id=None,
                source_table='approval_audit'
            )

def test_unsourced_event_types_allowed(pg_uri):
    # 'system.degraded' with no source -> ok
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        event_id = record_event(
            conn,
            event_type='system.degraded',
            entity_type='system',
            entity_id='sys-1',
            actor='sys',
            correlation_id=uuid.uuid4(),
            visibility='ops',
            scope_club_id=None
        )
        assert event_id > 0

def test_app_cannot_forge_dispatch_events(pg_uri):
    # C-10: record_event('dispatch.delivered', ...) as fc27_app -> raises
    # We are already fc27_app when using pg_uri
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="transition-only event type not allowed outside transition function"):
            record_event(
                conn,
                event_type='dispatch.delivered',
                entity_type='dispatch',
                entity_id='d-1',
                actor='sys',
                correlation_id=uuid.uuid4(),
                visibility='ops',
                scope_club_id=None
            )
