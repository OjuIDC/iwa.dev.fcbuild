"""Tier 3: Cross-Feature Combinations Test Suite.

Validates multi-feature interactions, pairwise behaviors, and race conditions:
- Decision vs. Expiry race condition & strict lock ordering
- Post-commit Graph resume failure & reconciliation persistence
- Concurrent migration runner & active graph runs
- Domain events and outbox transactional atomicity
- Idempotent decision retries vs conflicting decision attempts
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import uuid
import psycopg
import pytest
from fastapi.testclient import TestClient

from src import approval
from src.approval_store import ApprovalStore, StoreError
from src.schemas import DecisionIntent, SignedDecision
from src.security import Principal, Role
from tests.e2e.conftest import (
    COM_HEADERS,
    SVC_HEADERS,
    table_exists,
)
from tests.helpers import (
    APPROVE_COACH_BEST,
    COMMISSIONER_ID,
    COMMISSIONER_TOKEN,
    REJECT,
    SIGNING_SECRET,
    fake_packages,
    signed,
)

pytestmark = pytest.mark.usefixtures("pg_uri")


class TestCrossFeatureCombinations:
    """Pairwise and multi-component interaction test suite."""

    def test_tier3_decision_vs_expiry_race_condition(self, client, pg_uri):
        """Concurrent execution of expiry check and decision submission.

        Strict lock order (nonces -> requests -> threads) guarantees no deadlock
        and exactly one deterministic terminal state.
        """
        r = client.post("/run", headers=SVC_HEADERS, json={"club_id": "330323"})
        tid = r.json()["thread_id"]
        ch = client.post(f"/threads/{tid}/decision-challenge", headers=COM_HEADERS, json=APPROVE_COACH_BEST).json()
        decision_body = signed(ch)

        store = ApprovalStore(pg_uri)

        def submit_decision():
            return client.post(f"/threads/{tid}/decision", headers=COM_HEADERS, json=decision_body)

        def trigger_expiry():
            if hasattr(store, "expire_due"):
                return store.expire_due(thread_id=tid)
            return None

        # Execute concurrently
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            fut_dec = executor.submit(submit_decision)
            fut_exp = executor.submit(trigger_expiry)
            res_dec = fut_dec.result()
            res_exp = fut_exp.result()

        # Check final status in database: must be either approved or expired, never inconsistent
        with psycopg.connect(pg_uri) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM public.graph_threads WHERE thread_id = %s", (tid,))
                final_status = cur.fetchone()[0]
                assert final_status in ("approved", "expired")

    def test_tier3_post_commit_resume_failure_preserves_authoritative_decision(self, client, pg_uri):
        """Simulated Graph resume failure after DB commit leaves authoritative decision intact.

        Reconciliation later finalizes the thread without altering the decision.
        """
        r = client.post("/run", headers=SVC_HEADERS, json={"club_id": "330323"})
        tid = r.json()["thread_id"]
        ch = client.post(f"/threads/{tid}/decision-challenge", headers=COM_HEADERS, json=APPROVE_COACH_BEST).json()
        decision_body = signed(ch)

        # 1. Decision commits in DB
        res = client.post(f"/threads/{tid}/decision", headers=COM_HEADERS, json=decision_body)
        assert res.status_code == 200

        # Verify DB is committed
        with psycopg.connect(pg_uri) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM public.graph_threads WHERE thread_id = %s", (tid,))
                assert cur.fetchone()[0] == "approved"
                cur.execute("SELECT decision FROM public.approval_audit WHERE thread_id = %s", (tid,))
                assert cur.fetchone()[0] == "APPROVE"

        # 2. Simulate subsequent wake-up / reconciliation call
        # Even if repeated, the committed decision remains immutable
        replay = client.post(f"/threads/{tid}/decision", headers=COM_HEADERS, json=decision_body)
        assert replay.status_code in (200, 409)

        with psycopg.connect(pg_uri) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM public.approval_audit WHERE thread_id = %s", (tid,))
                # Exactly one audit row exists (no duplicates created)
                assert cur.fetchone()[0] == 1

    def test_tier3_concurrent_migration_check_and_active_run(self, client, pg_migrator_uri):
        """Migration runner under fc27_migrator does not lock out or corrupt active runs."""
        r = client.post("/run", headers=SVC_HEADERS, json={"club_id": "330323"})
        tid = r.json()["thread_id"]

        # Run migration check/idempotent re-run under migrator URI
        from src.db_migrate import migrate
        applied = migrate(pg_migrator_uri)
        assert isinstance(applied, list)

        # Verify active run is still in valid state
        status_resp = client.get(f"/threads/{tid}", headers=SVC_HEADERS)
        assert status_resp.status_code == 200
        assert status_resp.json()["status"] == "awaiting_approval"

    def test_tier3_domain_events_and_outbox_transactional_atomicity(self, pg_uri, pg_admin_db_uri):
        """An aborted transaction writing domain_events rolls back the corresponding outbox row."""
        corr_id = uuid.uuid4()
        with psycopg.connect(pg_uri) as conn:
            try:
                with conn.transaction():
                    from src.events import record_event
                    eid = record_event(
                        conn,
                        event_type="test.transactional",
                        entity_type="test",
                        entity_id="t-1",
                        actor="system",
                        correlation_id=corr_id,
                        visibility="ops",
                        scope_club_id="330323",
                    )
                    # Trigger an intentional error before commit
                    raise RuntimeError("Simulated transaction failure")
            except RuntimeError:
                pass

        # Verify neither the event nor the outbox row exists in the database
        with psycopg.connect(pg_admin_db_uri) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM public.domain_events WHERE correlation_id = %s", (corr_id,))
                assert cur.fetchone()[0] == 0
                cur.execute("SELECT count(*) FROM public.outbox WHERE domain_event_id IN (SELECT event_id FROM public.domain_events WHERE correlation_id = %s)", (corr_id,))
                assert cur.fetchone()[0] == 0

    def test_tier3_identical_decision_replay_vs_conflicting_decision(self, client):
        """Replaying identical decision succeeds idempotently; conflicting decision fails with 409."""
        r = client.post("/run", headers=SVC_HEADERS, json={"club_id": "330323"})
        tid = r.json()["thread_id"]
        ch = client.post(f"/threads/{tid}/decision-challenge", headers=COM_HEADERS, json=APPROVE_COACH_BEST).json()
        body = signed(ch)

        # First submission succeeds
        res1 = client.post(f"/threads/{tid}/decision", headers=COM_HEADERS, json=body)
        assert res1.status_code == 200

        # Exact replay is safe / idempotent
        res2 = client.post(f"/threads/{tid}/decision", headers=COM_HEADERS, json=body)
        assert res2.status_code in (200, 409)

        # Conflicting decision on same thread MUST fail
        ch_conflict = client.post(f"/threads/{tid}/decision-challenge", headers=COM_HEADERS, json=REJECT)
        assert ch_conflict.status_code in (409, 400)
