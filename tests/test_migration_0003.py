"""Tests for migration 0003 Part B: approval requests, pair consistency, and legacy migration."""
from __future__ import annotations

import uuid
import psycopg
import pytest

from src.db_roles import bootstrap_database
from src.db_migrate import migrate
from tests.helpers import (
    legacy_install,
    start,
    COM,
    APPROVE_COACH_BEST,
    signed,
)


def test_0003_applies_on_legacy_0002_data(pg_db):
    """AC-56: 0003 applies cleanly on legacy 0002 data, backfilling eligible awaiting threads."""
    legacy_install(pg_db.admin)
    bootstrap_database(pg_db.admin, legacy_owner=None, confirm=True)

    # Seed 5 legacy scenarios as admin
    with psycopg.connect(pg_db.admin, autocommit=True) as conn:
        with conn.cursor() as cur:
            # 1. Awaiting WITH package_version (eligible for backfill)
            cur.execute(
                """INSERT INTO public.graph_threads (thread_id, state, status, club_id, package_version)
                   VALUES ('fc27-330323-run-20261001-001', '{"status":"awaiting_approval"}', 'awaiting_approval', '330323', %s)""",
                ("a" * 64,),
            )

            # 2. Awaiting WITHOUT package_version (ineligible for backfill)
            cur.execute(
                """INSERT INTO public.graph_threads (thread_id, state, status, club_id, package_version)
                   VALUES ('fc27-330323-run-20261001-002', '{"status":"awaiting_approval"}', 'awaiting_approval', '330323', NULL)"""
            )

            # 3. Approved thread + audit + consumed nonce
            cur.execute(
                """INSERT INTO public.graph_threads (thread_id, state, status, club_id, package_version, approved_by, approved_at, decision_payload)
                   VALUES ('fc27-330323-run-20261001-003', '{"status":"approved"}', 'approved', '330323', %s, 'batmanL8', now(), '{"test":true}')""",
                ("b" * 64,),
            )
            cur.execute(
                """INSERT INTO public.approval_nonces (nonce, thread_id, payload_sha256, package_version, issued_at, expires_at, consumed_at, created_by)
                   VALUES (%s, 'fc27-330323-run-20261001-003', %s, %s, now() - interval '10m', now() + interval '10m', now(), 'batmanL8')""",
                ("nonce-003-" + "x" * 22, "1" * 64, "b" * 64),
            )
            cur.execute(
                """INSERT INTO public.approval_audit (thread_id, commissioner, decision, notes, approved_by, nonce, package_version, signature_sha256)
                   VALUES ('fc27-330323-run-20261001-003', 'batmanL8', 'APPROVE', 'ok', 'batmanL8', %s, %s, %s)""",
                ("nonce-003-" + "x" * 22, "b" * 64, "2" * 64),
            )

            # 4. Rejected thread
            cur.execute(
                """INSERT INTO public.graph_threads (thread_id, state, status, club_id, package_version, approved_by, approved_at, decision_payload)
                   VALUES ('fc27-330323-run-20261001-004', '{"status":"rejected"}', 'rejected', '330323', %s, 'batmanL8', now(), '{"test":true}')""",
                ("c" * 64,),
            )
            cur.execute(
                """INSERT INTO public.approval_nonces (nonce, thread_id, payload_sha256, package_version, issued_at, expires_at, consumed_at, created_by)
                   VALUES (%s, 'fc27-330323-run-20261001-004', %s, %s, now() - interval '10m', now() + interval '10m', now(), 'batmanL8')""",
                ("nonce-004-" + "x" * 22, "3" * 64, "c" * 64),
            )
            cur.execute(
                """INSERT INTO public.approval_audit (thread_id, commissioner, decision, notes, approved_by, nonce, package_version, signature_sha256)
                   VALUES ('fc27-330323-run-20261001-004', 'batmanL8', 'REJECT', 'no', 'batmanL8', %s, %s, %s)""",
                ("nonce-004-" + "x" * 22, "c" * 64, "4" * 64),
            )

            # 5. Unconsumed nonce for thread 1
            cur.execute(
                """INSERT INTO public.approval_nonces (nonce, thread_id, payload_sha256, package_version, issued_at, expires_at, created_by)
                   VALUES (%s, 'fc27-330323-run-20261001-001', %s, %s, now(), now() + interval '5m', 'batmanL8')""",
                ("nonce-001-" + "x" * 22, "5" * 64, "a" * 64),
            )

    # Apply migration 0003
    applied = migrate(pg_db.migrator)
    assert "0003_dispatch_foundation.sql" in applied

    with psycopg.connect(pg_db.admin) as conn:
        with conn.cursor() as cur:
            # Check exactly one approval_requests row created for thread 1
            cur.execute("SELECT approval_request_id, thread_id, package_version, status FROM public.approval_requests")
            reqs = cur.fetchall()
            assert len(reqs) == 1
            req_id, tid, pkg_v, status = reqs[0]
            assert tid == "fc27-330323-run-20261001-001"
            assert pkg_v == "a" * 64
            assert status == "awaiting_approval"

            # Thread 1 is linked
            cur.execute("SELECT approval_request_id FROM public.graph_threads WHERE thread_id = 'fc27-330323-run-20261001-001'")
            assert cur.fetchone()[0] == req_id

            # Ineligible and terminal threads have NULL link
            cur.execute(
                "SELECT thread_id, approval_request_id FROM public.graph_threads WHERE thread_id != 'fc27-330323-run-20261001-001' ORDER BY thread_id"
            )
            for other_tid, other_link in cur.fetchall():
                assert other_link is None, f"Expected None for {other_tid}"

            # Inconsistencies view contains ONLY legacy_unlinked
            cur.execute("SELECT kind, thread_id FROM public.fc27_approval_inconsistencies")
            incons = cur.fetchall()
            for kind, in_tid in incons:
                assert kind == "legacy_unlinked"
                assert in_tid in (
                    "fc27-330323-run-20261001-002",
                    "fc27-330323-run-20261001-003",
                    "fc27-330323-run-20261001-004",
                )


def test_pair_mismatch_fails_at_commit(pg_uri, client):
    """AC-55: Constraint trigger fc27_check_approval_pair raises at commit when statuses mismatch."""
    tid = start(client)

    # Subtest A: Mutating graph_threads.status alone fails at commit
    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE public.graph_threads
                      SET status = 'approved', approved_by = 'batmanL8', approved_at = now(), decision_payload = '{}'
                    WHERE thread_id = %s""",
                (tid,),
            )
            with pytest.raises(psycopg.errors.CheckViolation, match="approval pair: status mismatch"):
                conn.commit()

    # Subtest B: Mutating approval_requests.status alone fails at commit
    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE public.approval_requests SET status = 'approved', decided_at = now() WHERE thread_id = %s",
                (tid,),
            )
            with pytest.raises(psycopg.errors.CheckViolation, match="approval pair: status mismatch"):
                conn.commit()


def test_awaiting_thread_requires_link(pg_uri):
    """AC-55: An awaiting_approval thread with package_version must have approval_request_id."""
    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO public.graph_threads (thread_id, state, status, club_id, package_version, approval_request_id)
                   VALUES ('fc27-330323-run-20261001-999', '{"status":"awaiting_approval"}', 'awaiting_approval', '330323', %s, NULL)""",
                ("d" * 64,),
            )
            with pytest.raises(psycopg.errors.CheckViolation, match="approval pair: awaiting thread requires link"):
                conn.commit()


def test_package_version_immutable_while_awaiting(pg_uri, client):
    """Trigger fc27_guard_thread_transition blocks updating package_version while awaiting."""
    tid = start(client)
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.CheckViolation, match="package_version is immutable"):
                cur.execute(
                    "UPDATE public.graph_threads SET package_version = %s WHERE thread_id = %s",
                    ("f" * 64, tid),
                )


def test_approval_request_immutable_fields(pg_uri, client):
    """approval_requests trigger protects thread_id, package_version, created_at, expires_at."""
    tid = start(client)
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT approval_request_id FROM public.graph_threads WHERE thread_id = %s", (tid,))
            req_id = cur.fetchone()[0]

            with pytest.raises(psycopg.errors.CheckViolation, match="thread_id is immutable"):
                cur.execute("UPDATE public.approval_requests SET thread_id = 'other' WHERE approval_request_id = %s", (req_id,))

            with pytest.raises(psycopg.errors.CheckViolation, match="package_version is immutable"):
                cur.execute("UPDATE public.approval_requests SET package_version = %s WHERE approval_request_id = %s", ("e" * 64, req_id))

            with pytest.raises(psycopg.errors.CheckViolation, match="created_at is immutable"):
                cur.execute("UPDATE public.approval_requests SET created_at = now() - interval '1h' WHERE approval_request_id = %s", (req_id,))

            with pytest.raises(psycopg.errors.CheckViolation, match="expires_at is immutable"):
                cur.execute("UPDATE public.approval_requests SET expires_at = now() + interval '100h' WHERE approval_request_id = %s", (req_id,))


def test_approval_request_frozen_when_terminal(pg_uri, pg_admin_db_uri, client):
    """Terminal approval_requests rows are frozen against UPDATE and DELETE."""
    tid = start(client)
    ch = client.post(f"/threads/{tid}/decision-challenge", headers=COM, json=APPROVE_COACH_BEST).json()
    assert client.post(f"/threads/{tid}/decision", headers=COM, json=signed(ch)).status_code == 200

    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT approval_request_id FROM public.graph_threads WHERE thread_id = %s", (tid,))
            req_id = cur.fetchone()[0]

            with pytest.raises(psycopg.errors.CheckViolation, match="approval_request is terminal and frozen"):
                cur.execute("UPDATE public.approval_requests SET status = 'awaiting_approval' WHERE approval_request_id = %s", (req_id,))

    with psycopg.connect(pg_admin_db_uri, autocommit=True) as admin_conn:
        with admin_conn.cursor() as admin_cur:
            with pytest.raises(psycopg.errors.CheckViolation, match="approval_request is terminal and frozen"):
                admin_cur.execute("DELETE FROM public.approval_requests WHERE approval_request_id = %s", (req_id,))


@pytest.mark.allow_inconsistency
def test_inconsistencies_view_detects_mismatches(pg_uri, pg_admin_db_uri):
    """View fc27_approval_inconsistencies correctly classifies inconsistencies."""
    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SET session_replication_role = 'replica'")
            try:
                # Create a detached thread with package_version but no request link (missing_link)
                cur.execute(
                    """INSERT INTO public.graph_threads (thread_id, state, status, club_id, package_version)
                       VALUES ('missing-link-thread', '{"status":"awaiting_approval"}', 'awaiting_approval', '330323', %s)""",
                    ("9" * 64,),
                )
                # Create unlinked request
                req_id = str(uuid.uuid4())
                cur.execute(
                    """INSERT INTO public.approval_requests (approval_request_id, thread_id, package_version, status, expires_at)
                       VALUES (%s, 'missing-link-thread', %s, 'awaiting_approval', now() + interval '1h')""",
                    (req_id, "9" * 64),
                )
            finally:
                cur.execute("SET session_replication_role = 'origin'")

    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT kind FROM public.fc27_approval_inconsistencies WHERE approval_request_id = %s",
                (req_id,),
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == "unlinked_request"
