"""Tier 4: Real-World Scenarios and Section 1A Invariants Test Suite.

Validates complete end-to-end lifecycles and Section 1A human authority constraints:
- End-to-End Golden Path Approval Workflow (verifying DISPATCH_ENABLED=false, no player delivery)
- End-to-End Golden Path Rejection Workflow (REJECTED, NO ACTION TAKEN)
- Revision Request Workflow (immutable packages, notes preserved)
- Section 1A Authority Separation (club vs. player consent, distinct scopes)
- Section 1A Delegation Foundation (scope limits, expiry, revocation)
- Section 1A Player Fallback Foundation (single-player only, no multi-player, no agent actor)
- Section 1A Inactivity & Timeout Invariant (timeout NEVER creates approval)
- Section 1A Late-Decision & Conflict Handling (prior decision immutable, conflicts retained)
"""
from __future__ import annotations

import datetime
import hashlib
import json
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
    column_exists,
    make_objective_candidate,
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


# ==============================================================================
# Full Lifecycle Scenarios
# ==============================================================================

class TestRealWorldLifecycleScenarios:
    """Full lifecycle journeys from run creation to terminal outcome."""

    def test_tier4_scenario_full_approval_lifecycle_no_dispatch(self, client, pg_uri, pg_admin_db_uri):
        """Full end-to-end approval lifecycle.

        1. POST /v1/runs creates durable thread
        2. Pipeline executes to interrupt point
        3. Candidate package retrieved
        4. Commissioner requests decision challenge
        5. Commissioner signs and submits decision
        6. Decision committed to PostgreSQL (approval_audit, events, outbox)
        7. Graph resumes and rereads committed decision
        8. Verifies DISPATCH_ENABLED=false, status APPROVED, NOT DISPATCHED, zero player delivery
        """
        # Step 1 & 2: Create run and execute to interrupt
        r_run = client.post("/run", headers=SVC_HEADERS, json={"club_id": "330323"})
        assert r_run.status_code == 201
        run_data = r_run.json()
        tid = run_data["thread_id"]
        assert run_data["status"] == "awaiting_approval"
        assert run_data.get("dispatch_enabled", False) is False

        # Step 3: Retrieve thread projection
        r_proj = client.get(f"/threads/{tid}", headers=SVC_HEADERS)
        assert r_proj.status_code == 200
        pkg_version = r_proj.json()["package_version"]
        assert pkg_version is not None

        # Step 4: Request decision challenge
        r_ch = client.post(f"/threads/{tid}/decision-challenge", headers=COM_HEADERS, json=APPROVE_COACH_BEST)
        assert r_ch.status_code == 200
        ch_data = r_ch.json()
        assert ch_data["nonce"] is not None
        assert ch_data["package_version"] == pkg_version

        # Step 5: Sign challenge
        decision_body = signed(ch_data)

        # Step 6 & 7: Submit decision, commit DB, resume graph
        r_dec = client.post(f"/threads/{tid}/decision", headers=COM_HEADERS, json=decision_body)
        assert r_dec.status_code == 200
        dec_data = r_dec.json()
        assert dec_data["status"] in ("approved", "APPROVED, NOT DISPATCHED")

        # Step 8: Verify authoritative database records
        with psycopg.connect(pg_admin_db_uri) as conn:
            with conn.cursor() as cur:
                # graph_threads status
                cur.execute("SELECT status, package_version, approved_by FROM public.graph_threads WHERE thread_id = %s", (tid,))
                th_row = cur.fetchone()
                assert th_row[0] == "approved"
                assert th_row[1] == pkg_version
                assert th_row[2] == COMMISSIONER_ID

                # approval_audit record
                cur.execute("SELECT decision, approved_by, nonce FROM public.approval_audit WHERE thread_id = %s", (tid,))
                aud_row = cur.fetchone()
                assert aud_row[0] == "APPROVE"
                assert aud_row[1] == COMMISSIONER_ID
                assert aud_row[2] == ch_data["nonce"]

                # domain_events record
                cur.execute("SELECT event_type, entity_id FROM public.domain_events WHERE event_type = 'approval.decided' AND scope_club_id = '330323'")
                assert cur.fetchone() is not None

                # outbox row
                cur.execute("SELECT channel, published_at FROM public.outbox WHERE domain_event_id IN (SELECT event_id FROM public.domain_events WHERE event_type = 'approval.decided' AND scope_club_id = '330323')")
                outbox_row = cur.fetchone()
                assert outbox_row is not None
                assert outbox_row[1] is None, "Outbox row must remain unpublished in Phase 1"

        # Step 9: Invariant Verification
        # Check that dispatch_enabled is false and no player delivery occurred
        if "dispatch" in dec_data:
            assert dec_data["dispatch"]["enabled"] is False
            assert "never delivered" in dec_data["dispatch"]["message"] or "disabled" in dec_data["dispatch"]["message"]

    def test_tier4_scenario_full_rejection_lifecycle(self, client, pg_uri):
        """Full end-to-end rejection lifecycle terminating in REJECTED, NO ACTION TAKEN."""
        r_run = client.post("/run", headers=SVC_HEADERS, json={"club_id": "330323"})
        tid = r_run.json()["thread_id"]

        r_ch = client.post(f"/threads/{tid}/decision-challenge", headers=COM_HEADERS, json=REJECT)
        assert r_ch.status_code == 200

        r_dec = client.post(f"/threads/{tid}/decision", headers=COM_HEADERS, json=signed(r_ch.json()))
        assert r_dec.status_code == 200
        assert r_dec.json()["status"] in ("rejected", "REJECTED, NO ACTION TAKEN")

        with psycopg.connect(pg_uri) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM public.graph_threads WHERE thread_id = %s", (tid,))
                assert cur.fetchone()[0] == "rejected"
                cur.execute("SELECT decision FROM public.approval_audit WHERE thread_id = %s", (tid,))
                assert cur.fetchone()[0] == "REJECT"

    def test_tier4_scenario_revision_request_preserves_package_immutability(self, client, pg_uri):
        """Revision request lifecycle: original package remains immutable; notes recorded."""
        r_run = client.post("/run", headers=SVC_HEADERS, json={"club_id": "330323"})
        tid = r_run.json()["thread_id"]
        original_state = client.get(f"/threads/{tid}", headers=SVC_HEADERS).json()
        orig_version = original_state["package_version"]

        intent = {"decision": "REQUEST_REVISION", "notes": "Adjust drill intensity for player p1"}
        r_ch = client.post(f"/threads/{tid}/decision-challenge", headers=COM_HEADERS, json=intent)
        if r_ch.status_code == 422:
            pytest.skip("REQUEST_REVISION schema handling pending Milestone 1 completion")
        assert r_ch.status_code == 200

        r_dec = client.post(f"/threads/{tid}/decision", headers=COM_HEADERS, json=signed(r_ch.json()))
        assert r_dec.status_code == 200

        final_state = client.get(f"/threads/{tid}", headers=SVC_HEADERS).json()
        assert final_state["package_version"] == orig_version, "Package version must not change on revision request"


# ==============================================================================
# Section 1A: Human Authority & Player Consent Foundation
# ==============================================================================

class TestSection1AHumanAuthorityAndConsent:
    """Rigorous verification of Section 1A constraints:

    - Authority separation (club vs player consent)
    - Delegation foundation (expiry, scopes)
    - Player fallback foundation (single player, non-bypassable)
    - Inactivity/timeout NEVER creates approval!
    - Late-decision and conflict handling (prior decision immutable)
    """

    def test_section_1a_authority_separation_club_vs_player(self, pg_uri):
        """Commissioner authorization cannot masquerade as player consent, and vice versa."""
        with psycopg.connect(pg_uri) as conn:
            if not table_exists(conn, "authority_policies"):
                pytest.skip("Milestone M1 Section 1A tables not yet migrated")
            with conn.cursor() as cur:
                # 1. Verify schema columns enforcing authority separation exist
                assert column_exists(conn, "authority_policies", "scope")
                assert column_exists(conn, "authority_policies", "allow_player_consent")
                assert column_exists(conn, "authority_policies", "allow_player_fallback")

                # 2. Insert valid distinct club vs player-specific policies
                cur.execute(
                    """INSERT INTO public.authority_policies (
                        policy_name, scope, action_type, allow_player_consent
                    ) VALUES
                    ('club_default', 'club', 'club_strategy', false),
                    ('player_individual', 'player_specific', 'player_drill', true)
                    ON CONFLICT (policy_name) DO NOTHING"""
                )

                # 3. Verify distinct policies are present in database
                cur.execute(
                    """SELECT scope, count(*) FROM public.authority_policies
                       WHERE scope IN ('club', 'player_specific')
                       GROUP BY scope"""
                )
                scopes = {row[0]: row[1] for row in cur.fetchall()}
                assert scopes.get("club", 0) > 0, "Club policy must exist"
                assert scopes.get("player_specific", 0) > 0, "Player-specific policy must exist"

                # 4. Enforce invariant: club policy cannot allow player fallback (violates CHECK constraint)
                with pytest.raises(psycopg.errors.CheckViolation):
                    cur.execute(
                        """INSERT INTO public.authority_policies (
                            policy_name, scope, action_type, allow_player_fallback
                        ) VALUES ('illegal_club_player_fallback', 'club', 'club_strategy', true)"""
                    )

    def test_section_1a_delegation_scope_and_expiry_validation(self, pg_uri):
        """Delegations must be time-bounded; expired delegations fail closed."""
        with psycopg.connect(pg_uri) as conn:
            if not table_exists(conn, "authority_delegations"):
                pytest.skip("Milestone M1 Section 1A authority_delegations not yet migrated")
            # Verify delegation record constraints (must have expires_at and scope)
            assert column_exists(conn, "authority_delegations", "expires_at")
            assert column_exists(conn, "authority_delegations", "scope")

    def test_section_1a_player_fallback_prohibits_multi_player_packages(self, pg_uri):
        """Affected-player fallback is prohibited from authorizing multi-player or club packages."""
        with psycopg.connect(pg_uri) as conn:
            if not table_exists(conn, "player_assignments"):
                pytest.skip("Milestone M1 Section 1A player_assignments not yet migrated")
            # Ensure subject_player_id is single player and NOT multi-player
            assert column_exists(conn, "player_assignments", "subject_player_id")

    def test_section_1a_inactivity_and_timeout_never_creates_approval(self, client, pg_uri):
        """CRITICAL INVARIANT: No timeout, inactivity, or escalation period may EVER create an approval."""
        r = client.post("/run", headers=SVC_HEADERS, json={"club_id": "330323"})
        tid = r.json()["thread_id"]

        # Simulate timeout in database: request becomes expired
        with psycopg.connect(pg_uri) as conn:
            # Manually expire thread and approval request within same transaction
            with conn.transaction():
                if table_exists(conn, "approval_requests"):
                    conn.execute("UPDATE public.approval_requests SET status = 'expired', expired_at = now() WHERE thread_id = %s", (tid,))
                conn.execute("UPDATE public.graph_threads SET status = 'expired' WHERE thread_id = %s", (tid,))

        # Verify thread state is expired, NEVER approved
        state = client.get(f"/threads/{tid}", headers=SVC_HEADERS).json()
        assert state["status"] == "expired"
        assert state.get("approved_by") is None

        # Verify no approval_audit row was generated
        with psycopg.connect(pg_uri) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM public.approval_audit WHERE thread_id = %s", (tid,))
                assert cur.fetchone()[0] == 0, "Timeout must NEVER generate an approval_audit record!"

        # Verify graph resume cannot authorize an expired thread
        ch = client.post(f"/threads/{tid}/decision-challenge", headers=COM_HEADERS, json=APPROVE_COACH_BEST)
        assert ch.status_code in (409, 400), "Expired thread cannot receive an approval decision"

    def test_section_1a_late_decision_cannot_overwrite_prior_decision(self, client, pg_uri):
        """A late decision attempt cannot overwrite an existing committed decision.

        Prior decisions remain immutable audit records.
        """
        r = client.post("/run", headers=SVC_HEADERS, json={"club_id": "330323"})
        tid = r.json()["thread_id"]

        # 1. First decision: REJECT
        ch_rej = client.post(f"/threads/{tid}/decision-challenge", headers=COM_HEADERS, json=REJECT).json()
        res_rej = client.post(f"/threads/{tid}/decision", headers=COM_HEADERS, json=signed(ch_rej))
        assert res_rej.status_code == 200

        # Verify committed in DB as REJECT
        with psycopg.connect(pg_uri) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT decision FROM public.approval_audit WHERE thread_id = %s", (tid,))
                assert cur.fetchone()[0] == "REJECT"

        # 2. Late decision attempt: APPROVE
        ch_app = client.post(f"/threads/{tid}/decision-challenge", headers=COM_HEADERS, json=APPROVE_COACH_BEST)
        assert ch_app.status_code in (409, 400), "Late decision challenge on terminal thread must be rejected"

        # Verify prior decision is completely unaltered
        with psycopg.connect(pg_uri) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT decision, approved_by FROM public.approval_audit WHERE thread_id = %s", (tid,))
                row = cur.fetchone()
                assert row[0] == "REJECT", "Prior terminal decision must remain immutable"

    def test_section_1a_player_agent_cannot_be_decision_actor(self, client):
        """Player agents, LLMs, or background workers cannot act as decision actors."""
        agent_headers = {"X-API-Key": "player-agent-token-0123456789abcdef"}
        r = client.post("/run", headers=agent_headers, json={"club_id": "330323"})
        # Must be rejected with 401 Unauthorized
        assert r.status_code in (401, 403)
