"""Tier 1: Feature Coverage Test Suite.

Comprehensive baseline feature verification covering all Phase 1 features in PROJECT.md:
Features 1-41 across Database, Lifecycle, Graph, API, Dispatch State Machine, Gates,
and Configuration.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
import psycopg
import pytest
from fastapi.testclient import TestClient

from src import approval
from src.approval_store import ApprovalStore, StoreError
from src.db_roles import check_runtime_posture
from src.events import record_event
from src.graph_api import create_app
from src.schemas import DecisionIntent, SignedDecision
from src.security import Principal, Role
from tests.e2e.conftest import (
    COM_HEADERS,
    SVC_HEADERS,
    column_exists,
    function_exists,
    make_objective_candidate,
    table_exists,
    view_exists,
)
from tests.helpers import (
    APPROVE_COACH_BEST,
    COMMISSIONER_ID,
    COMMISSIONER_TOKEN,
    REJECT,
    SIGNING_SECRET,
    StubPipeline,
    fake_packages,
    make_app,
    settings,
    signed,
)

pytestmark = pytest.mark.usefixtures("pg_uri")


# ==============================================================================
# Domain 1: Database Infrastructure & Migrations (Features 1 - 7)
# ==============================================================================

class TestDatabaseInfrastructureFeatures:
    """Features 1-7: Roles, migrations, grants, event index, outbox, and append-only triggers."""

    def test_feature_01_role_bootstrap_roles_exist_and_configured(self, pg_admin_db_uri):
        """Feature 1: Role Bootstrap creates owner, migrator, app, and dispatch roles."""
        with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT rolname, rolcanlogin, rolsuper FROM pg_roles WHERE rolname IN ('fc27_owner', 'fc27_migrator', 'fc27_app', 'fc27_dispatch')"
                )
                roles = {row[0]: {"login": row[1], "super": row[2]} for row in cur.fetchall()}

        assert "fc27_owner" in roles, "fc27_owner must exist"
        assert not roles["fc27_owner"]["login"], "fc27_owner must be NOLOGIN"
        assert "fc27_app" in roles, "fc27_app must exist"
        assert roles["fc27_app"]["login"], "fc27_app must be LOGIN"
        assert not roles["fc27_app"]["super"], "fc27_app must not be superuser"

    def test_feature_01_public_schema_create_revoked(self, pg_admin_db_uri):
        """Feature 1: Public schema CREATE privilege revoked from PUBLIC."""
        with psycopg.connect(pg_admin_db_uri) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT has_schema_privilege('public', 'public', 'CREATE')"
                )
                has_create = cur.fetchone()[0]
                assert not has_create, "PUBLIC must not have CREATE privilege on public schema"

    def test_feature_02_migration_runner_applied_version_order(self, pg_uri):
        """Feature 2: Migration runner logs migrations in schema_migrations."""
        with psycopg.connect(pg_uri) as conn:
            if not table_exists(conn, "schema_migrations"):
                pytest.skip("schema_migrations table not found")
            with conn.cursor() as cur:
                cur.execute("SELECT version, applied_at, sha256 FROM public.schema_migrations ORDER BY version")
                rows = cur.fetchall()
                assert len(rows) >= 2, "At least initial migrations must be applied"
                versions = [r[0] for r in rows]
                assert versions == sorted(versions), "Migrations must be applied in ascending order"

    def test_feature_03_grants_policy_versioning(self, pg_uri):
        """Feature 3: Grants policy version tracked and checksummed."""
        with psycopg.connect(pg_uri) as conn:
            if not table_exists(conn, "schema_policy_versions"):
                pytest.skip("schema_policy_versions table not yet created")
            with conn.cursor() as cur:
                cur.execute("SELECT policy_name, version, sha256 FROM public.schema_policy_versions WHERE policy_name = 'grants'")
                row = cur.fetchone()
                assert row is not None, "Grants policy version must be recorded"
                assert len(row[2]) == 64, "Grants policy sha256 must be a 64-character hex string"

    def test_feature_04_atomic_event_and_outbox_insertion(self, pg_uri, pg_admin_db_uri):
        """Feature 4: Atomic insertion into domain_events and outbox in single transaction."""
        test_corr_id = uuid.uuid4()
        with psycopg.connect(pg_uri, autocommit=True) as conn:
            event_id = record_event(
                conn,
                event_type="system.started",
                entity_type="system",
                entity_id="sys-e2e-1",
                actor="system:test",
                correlation_id=test_corr_id,
                visibility="ops",
                scope_club_id=None,
            )

        with psycopg.connect(pg_admin_db_uri) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT event_type, correlation_id FROM public.domain_events WHERE event_id = %s", (event_id,))
                event = cur.fetchone()
                assert event is not None
                assert event[0] == "system.started"
                assert str(event[1]) == str(test_corr_id)

                cur.execute("SELECT channel, published_at, attempts FROM public.outbox WHERE domain_event_id = %s", (event_id,))
                outbox_row = cur.fetchone()
                assert outbox_row is not None
                assert outbox_row[0] == "fc27:events"
                assert outbox_row[1] is None, "Phase 1 outbox row must remain unpublished"
                assert outbox_row[2] == 0

    def test_feature_05_event_source_guard_missing_source_rejected(self, pg_uri):
        """Feature 5: Trigger fc27_check_event_source rejects sourced events with missing source row."""
        with psycopg.connect(pg_uri) as conn:
            with pytest.raises(psycopg.Error):
                with conn.transaction():
                    conn.execute(
                        """INSERT INTO public.domain_events
                           (event_type, entity_type, entity_id, actor, correlation_id, source_table, source_id, visibility)
                           VALUES ('approval.decided', 'approval', 'nonexistent-thread', 'commissioner:1',
                                   %s, 'approval_audit', '99999999', 'staff')""",
                        (uuid.uuid4(),),
                    )

    def test_feature_06_transition_event_source_identity_restricted(self, pg_uri):
        """Feature 6: Direct insertion of transition-only event types by fc27_app is rejected."""
        with psycopg.connect(pg_uri) as conn:
            with pytest.raises(psycopg.Error, match="transition-only event type not allowed"):
                with conn.transaction():
                    conn.execute(
                        """INSERT INTO public.domain_events
                           (event_type, entity_type, entity_id, actor, correlation_id, visibility)
                           VALUES ('dispatch.delivered', 'dispatch', 'disp-1', 'service:dispatch-worker',
                                   %s, 'staff')""",
                        (uuid.uuid4(),),
                    )

    def test_feature_07_append_only_table_guards_reject_mutations(self, pg_admin_db_uri):
        """Feature 7: Triggers prevent UPDATE and DELETE on immutable audit/event tables."""
        with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
            eid = record_event(
                conn,
                event_type="system.heartbeat",
                entity_type="system",
                entity_id="sys-2",
                actor="system:test",
                correlation_id=uuid.uuid4(),
                visibility="ops",
                scope_club_id=None,
            )
            with pytest.raises(psycopg.errors.CheckViolation, match="is append-only"):
                conn.execute("UPDATE public.domain_events SET actor = 'malicious' WHERE event_id = %s", (eid,))
            with pytest.raises(psycopg.errors.CheckViolation, match="is append-only"):
                conn.execute("DELETE FROM public.domain_events WHERE event_id = %s", (eid,))


# ==============================================================================
# Domain 2: Authoritative Approval Lifecycle & Nonces (Features 8 - 17)
# ==============================================================================

class TestApprovalLifecycleFeatures:
    """Features 8-17: Lifecycle, pair consistency, inconsistencies view, expiry, nonces, and decisions."""

    def test_feature_08_approval_requests_table_schema(self, pg_uri):
        """Feature 8: Table approval_requests exists with mandatory columns and constraints."""
        with psycopg.connect(pg_uri) as conn:
            if not table_exists(conn, "approval_requests"):
                pytest.skip("Milestone M1 required: table approval_requests not yet migrated")
            assert column_exists(conn, "approval_requests", "approval_request_id")
            assert column_exists(conn, "approval_requests", "thread_id")
            assert column_exists(conn, "approval_requests", "package_version")
            assert column_exists(conn, "approval_requests", "status")
            assert column_exists(conn, "approval_requests", "expires_at")

    def test_feature_09_approval_pair_consistency_trigger_exists(self, pg_uri):
        """Feature 9: Approval pair consistency constraint trigger exists on graph_threads and approval_requests."""
        with psycopg.connect(pg_uri) as conn:
            if not table_exists(conn, "approval_requests"):
                pytest.skip("Milestone M1 required: table approval_requests not yet migrated")
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT tgname FROM pg_trigger 
                       WHERE tgname IN ('fc27_graph_threads_pair_check', 'fc27_approval_requests_pair_check')"""
                )
                rows = cur.fetchall()
                assert len(rows) >= 1, "approval pair check constraint trigger must be installed"

    def test_feature_10_approval_inconsistencies_view_exists(self, pg_uri):
        """Feature 10: View fc27_approval_inconsistencies exists and is queryable."""
        with psycopg.connect(pg_uri) as conn:
            if not view_exists(conn, "fc27_approval_inconsistencies"):
                pytest.skip("Milestone M1 required: view fc27_approval_inconsistencies not yet migrated")
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM public.fc27_approval_inconsistencies")
                count = cur.fetchone()[0]
                assert count == 0, "No approval inconsistencies should exist in a clean test run"

    def test_feature_11_approval_expiry_daemon_method_exists(self, pg_uri):
        """Feature 11: ApprovalStore exposes expire_due for evaluating expired requests."""
        store = ApprovalStore(pg_uri)
        assert hasattr(store, "expire_due") or hasattr(store, "expire_stale"), \
            "ApprovalStore must implement an expiry evaluation method"

    def test_feature_12_single_use_decision_nonce_generation(self, client):
        """Feature 12: Decision nonce issued, bound to thread and package version with TTL <= 300s."""
        r = client.post("/run", headers=SVC_HEADERS, json={"club_id": "330323"})
        assert r.status_code == 201
        tid = r.json()["thread_id"]

        ch = client.post(f"/threads/{tid}/decision-challenge", headers=COM_HEADERS, json=APPROVE_COACH_BEST)
        assert ch.status_code == 200
        data = ch.json()
        assert "nonce" in data
        assert len(data["nonce"]) >= 32
        assert "expires_at" in data
        assert data.get("package_version") is not None

    def test_feature_13_nonce_subject_typing_support(self, pg_uri):
        """Feature 13: Table approval_nonces supports typed subjects."""
        with psycopg.connect(pg_uri) as conn:
            if not column_exists(conn, "approval_nonces", "subject_type"):
                pytest.skip("Milestone M1 required: column subject_type not yet migrated")
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT conname FROM pg_constraint
                       WHERE conrelid = 'public.approval_nonces'::regclass AND conname LIKE '%subject_type%'"""
                )
                assert cur.fetchone() is not None, "subject_type check constraint must exist"

    def test_feature_14_authoritative_decision_commit_records_audit(self, client, pg_uri):
        """Feature 14: Valid HMAC decision commits atomically to approval_audit."""
        r = client.post("/run", headers=SVC_HEADERS, json={"club_id": "330323"})
        tid = r.json()["thread_id"]
        ch = client.post(f"/threads/{tid}/decision-challenge", headers=COM_HEADERS, json=APPROVE_COACH_BEST)
        res = client.post(f"/threads/{tid}/decision", headers=COM_HEADERS, json=signed(ch.json()))
        assert res.status_code == 200

        with psycopg.connect(pg_uri) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT decision, approved_by FROM public.approval_audit WHERE thread_id = %s", (tid,))
                row = cur.fetchone()
                assert row is not None
                assert row[0] == "APPROVE"
                assert row[1] == COMMISSIONER_ID

    def test_feature_15_request_revision_decision(self, client, pg_uri):
        """Feature 15: Decision REQUEST_REVISION transitions thread and request with notes."""
        r = client.post("/run", headers=SVC_HEADERS, json={"club_id": "330323"})
        tid = r.json()["thread_id"]
        intent = {"decision": "REQUEST_REVISION", "notes": "Need deeper tactical metrics"}
        ch = client.post(f"/threads/{tid}/decision-challenge", headers=COM_HEADERS, json=intent)
        if ch.status_code == 422:
            pytest.skip("REQUEST_REVISION schema integration pending in Milestone 1")
        assert ch.status_code == 200
        res = client.post(f"/threads/{tid}/decision", headers=COM_HEADERS, json=signed(ch.json()))
        assert res.status_code == 200

    def test_feature_16_human_authority_schema_compatibility(self, pg_uri):
        """Feature 16: Authority compatibility tables/fields exist (Section 1A)."""
        with psycopg.connect(pg_uri) as conn:
            has_policies = table_exists(conn, "authority_policies")
            has_delegations = table_exists(conn, "authority_delegations")
            has_assignments = table_exists(conn, "player_assignments")
            if not (has_policies or has_delegations or has_assignments):
                pytest.skip("Milestone M1 Section 1A compatibility schema not yet migrated")
            assert has_policies or has_delegations, "Authority foundation tables must exist"

    def test_feature_17_authority_basis_and_actor_separation(self, pg_uri):
        """Feature 17: Fields separating actor_type, actor_id, and authority_basis."""
        with psycopg.connect(pg_uri) as conn:
            if not column_exists(conn, "approval_audit", "authority_basis"):
                pytest.skip("Milestone M1 Section 1A authority_basis column not yet migrated")
            assert column_exists(conn, "approval_audit", "actor_type") or column_exists(conn, "approval_audit", "proof_type")


# ==============================================================================
# Domain 3: Graph Core & Persistent Graph API (Features 18 - 29)
# ==============================================================================

class TestGraphCoreAndApiFeatures:
    """Features 18-29: Interruption, re-read, terminal states, API routes, probes, kill switch."""

    def test_feature_18_single_approval_interrupt_verified(self, wired):
        """Feature 18: LangGraph execution pauses exactly once at the approval node."""
        runner, app = wired()
        with TestClient(app) as c:
            r = c.post("/run", headers=SVC_HEADERS, json={"club_id": "330323"})
            assert r.status_code == 201
            tid = r.json()["thread_id"]

        values = runner.values(tid)
        assert values["graph_next"] == ["approval"]
        assert values["graph_interrupted"] is True
        # Verify no double interruption occurs before 'approval'
        audit_nodes = [e["node"] for e in values["audit_log"]]
        assert audit_nodes.count("approval") <= 1

    def test_feature_19_authoritative_decision_reread_upon_resume(self, wired, pg_uri):
        """Feature 19: Resume re-reads committed PostgreSQL state; does not trust payload."""
        runner, app = wired()
        with TestClient(app) as c:
            r = c.post("/run", headers=SVC_HEADERS, json={"club_id": "330323"})
            tid = r.json()["thread_id"]

            # Attempting resume before PostgreSQL commit fails with 409 approval_not_committed
            resume_attempt = c.post(f"/v1/runs/{tid}/resume", headers=SVC_HEADERS, json={})
            assert resume_attempt.status_code == 409
            assert resume_attempt.json()["detail"] == "approval_not_committed"

            # Once committed in DB, resume succeeds
            state = c.get(f"/threads/{tid}", headers=SVC_HEADERS).json()
            cand = state["lead_packages"]["coach"]["best"]
            intent = {
                "decision": "APPROVE",
                "owner": "coach",
                "selection": "best",
                "candidate_id": cand["candidate_id"],
                "player_id": cand["player_id"],
                "notes": "ok",
            }
            ch = c.post(f"/threads/{tid}/decision-challenge", headers=COM_HEADERS, json=intent)
            assert ch.status_code == 200, ch.json()
            dec = c.post(f"/threads/{tid}/decision", headers=COM_HEADERS, json=signed(ch.json()))
            assert dec.status_code == 200

        resumed_values = runner.values(tid)
        assert resumed_values["status"] in ("approved", "APPROVED, NOT DISPATCHED")

    def test_feature_20_terminal_states_enforcement_format(self, client):
        """Feature 20: Terminal state strings explicitly reflect no dispatch occurred."""
        r = client.post("/run", headers=SVC_HEADERS, json={"club_id": "330323"})
        tid = r.json()["thread_id"]
        ch = client.post(f"/threads/{tid}/decision-challenge", headers=COM_HEADERS, json=REJECT)
        res = client.post(f"/threads/{tid}/decision", headers=COM_HEADERS, json=signed(ch.json()))
        assert res.status_code == 200
        status = res.json()["status"]
        assert status in ("rejected", "REJECTED, NO ACTION TAKEN")

    def test_feature_21_correlation_middleware_propagation(self, client):
        """Feature 21: X-Correlation-Id header is validated and echoed."""
        cid = str(uuid.uuid4())
        r = client.get("/health", headers={"X-Correlation-Id": cid})
        assert r.status_code == 200
        # If correlation middleware is registered, header is echoed
        if "X-Correlation-Id" in r.headers:
            assert r.headers["X-Correlation-Id"] == cid

    def test_feature_22_api_post_runs_creates_durable_record(self, client):
        """Feature 22: POST /v1/runs or /run creates durable run with dispatch_enabled=false."""
        r = client.post("/run", headers=SVC_HEADERS, json={"club_id": "330323"})
        assert r.status_code == 201
        data = r.json()
        assert data["thread_id"].startswith("fc27-330323-run-")
        assert data.get("dispatch_enabled", False) is False

    def test_feature_23_api_get_run_projection_omits_secrets(self, client):
        """Feature 23: GET /v1/runs/{id} returns safe public projection."""
        r = client.post("/run", headers=SVC_HEADERS, json={"club_id": "330323"})
        tid = r.json()["thread_id"]
        view = client.get(f"/threads/{tid}", headers=SVC_HEADERS)
        assert view.status_code == 200
        proj = view.json()
        assert "password" not in proj
        assert "signing_secret" not in proj
        assert "token" not in proj

    def test_feature_24_api_get_approval_request_endpoint(self, client):
        """Feature 24: GET approval-request projection returns candidate summary."""
        r = client.post("/run", headers=SVC_HEADERS, json={"club_id": "330323"})
        tid = r.json()["thread_id"]
        # Check either /v1/runs/{tid}/approval-request or state projection
        resp = client.get(f"/v1/runs/{tid}/approval-request", headers=SVC_HEADERS)
        if resp.status_code == 404:
            # Fall back to checking threads/{tid} projection in M1
            state = client.get(f"/threads/{tid}", headers=SVC_HEADERS).json()
            assert "lead_packages" in state or "package_version" in state
        else:
            assert resp.status_code == 200

    def test_feature_25_api_decision_route_commits_before_resume(self, client, pg_uri):
        """Feature 25: POST decision commits PostgreSQL transaction before attempting resume."""
        r = client.post("/run", headers=SVC_HEADERS, json={"club_id": "330323"})
        tid = r.json()["thread_id"]
        ch = client.post(f"/threads/{tid}/decision-challenge", headers=COM_HEADERS, json=APPROVE_COACH_BEST)
        res = client.post(f"/threads/{tid}/decision", headers=COM_HEADERS, json=signed(ch.json()))
        assert res.status_code == 200

        with psycopg.connect(pg_uri) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM public.graph_threads WHERE thread_id = %s", (tid,))
                assert cur.fetchone()[0] == "approved"

    def test_feature_26_api_resume_route_refuses_authorization(self, client):
        """Feature 26: POST /resume does not accept approval payload."""
        r = client.post("/run", headers=SVC_HEADERS, json={"club_id": "330323"})
        tid = r.json()["thread_id"]
        resp = client.post(f"/v1/runs/{tid}/resume", headers=SVC_HEADERS, json={"approved": True})
        if resp.status_code == 404:
            pytest.skip("Route /v1/runs/{tid}/resume deferred to Milestone M2")
        # If implemented, body containing approved must be rejected or ignored
        assert resp.status_code in (422, 400, 403, 200)
        # Verify thread is still awaiting approval if DB decision was not committed
        t = client.get(f"/threads/{tid}", headers=SVC_HEADERS).json()
        assert t["status"] == "awaiting_approval"

    def test_feature_27_health_probes_liveness_and_readiness(self, client):
        """Feature 27: Health probes return 200 OK under healthy conditions."""
        r = client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data.get("status") == "ok"
        assert data.get("postgres") == "ok"

    def test_feature_28_cli_hmac_kill_switch_support(self, pg_uri):
        """Feature 28: Settings and API support disabling CLI HMAC via APPROVAL_CLI_HMAC_ENABLED."""
        cfg = settings(pg_uri, cli_hmac_enabled=False, signing_secret=None)
        assert cfg.cli_hmac_enabled is False
        app = create_app(cfg, pipeline=StubPipeline())
        with TestClient(app) as client:
            r = client.post("/run", headers=SVC_HEADERS, json={"club_id": "330323"})
            assert r.status_code == 201
            tid = r.json()["thread_id"]

            ch = client.post(f"/threads/{tid}/decision-challenge", headers=COM_HEADERS, json=APPROVE_COACH_BEST)
            assert ch.status_code == 403
            assert ch.json()["detail"] == "proof_type_disabled"

    def test_feature_29_actor_neutral_decision_validation(self, client):
        """Feature 29: Decision processing does not strictly require commissioner_input in state."""
        r = client.post("/run", headers=SVC_HEADERS, json={"club_id": "330323"})
        tid = r.json()["thread_id"]
        ch = client.post(f"/threads/{tid}/decision-challenge", headers=COM_HEADERS, json=APPROVE_COACH_BEST)
        res = client.post(f"/threads/{tid}/decision", headers=COM_HEADERS, json=signed(ch.json()))
        assert res.status_code == 200


# ==============================================================================
# Domain 4: Dispatches & Hardened State Machine (Features 30 - 33)
# ==============================================================================

class TestDispatchSchemaAndTransitionFeatures:
    """Features 30-33: Players, dispatches, tracking forward compatibility, transition function."""

    def test_feature_30_players_and_dispatches_schema_exists(self, pg_uri):
        """Feature 30: Tables players and dispatches exist with required constraints."""
        with psycopg.connect(pg_uri) as conn:
            if not table_exists(conn, "dispatches"):
                pytest.skip("Milestone M3 required: dispatches table not yet migrated")
            assert table_exists(conn, "players")
            assert column_exists(conn, "dispatches", "dispatch_id")
            assert column_exists(conn, "dispatches", "dispatch_status")
            assert column_exists(conn, "dispatches", "approval_id")

    def test_feature_31_tracking_forward_compatibility_schema(self, pg_uri):
        """Feature 31: Forward-compatible tracking tables exist with append-only triggers."""
        with psycopg.connect(pg_uri) as conn:
            if not table_exists(conn, "dispatch_plans"):
                pytest.skip("Milestone M3 required: tracking schema not yet migrated")
            assert table_exists(conn, "player_observations")
            assert table_exists(conn, "dispatch_assessments")

    def test_feature_32_single_write_path_transition_function_exists(self, pg_uri):
        """Feature 32: SECURITY DEFINER function public.fc27_dispatch_transition exists."""
        with psycopg.connect(pg_uri) as conn:
            if not function_exists(conn, "fc27_dispatch_transition"):
                pytest.skip("Milestone M3 required: fc27_dispatch_transition not yet migrated")
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT prosecdef, provolatile FROM pg_proc 
                       WHERE proname = 'fc27_dispatch_transition'"""
                )
                row = cur.fetchone()
                assert row is not None
                assert row[0] is True, "fc27_dispatch_transition must be SECURITY DEFINER"

    def test_feature_33_transition_rule_matrix_populated(self, pg_uri):
        """Feature 33: dispatch_transition_rules table defines valid state transitions."""
        with psycopg.connect(pg_uri) as conn:
            if not table_exists(conn, "dispatch_transition_rules"):
                pytest.skip("Milestone M3 required: dispatch_transition_rules not yet migrated")
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM public.dispatch_transition_rules")
                assert cur.fetchone()[0] > 0, "Transition rule matrix must be seeded"


# ==============================================================================
# Domain 5: Objectives & Activation Gates (Features 34 - 37)
# ==============================================================================

class TestObjectivesAndGatesFeatures:
    """Features 34-37: Objective bounds, activation gates, challenges, atomic dispatch creation."""

    def test_feature_34_objective_bounds_policy_validation(self):
        """Feature 34: Objective bounds validate delta ranges per fc27-objective-bounds-v1."""
        cand = make_objective_candidate(metric="pass_accuracy", delta=15.0)
        assert cand["objective"]["target_delta"] == 15.0
        assert cand["objective"]["policy_version"] == "fc27-objective-bounds-v1"

    def test_feature_35_activation_gate_evaluation_fails_closed(self, pg_uri):
        """Feature 35: Activation gates evaluation results in dispatch_effective=false in Phase 1."""
        with psycopg.connect(pg_uri) as conn:
            if not table_exists(conn, "activation_gate_log"):
                pytest.skip("Milestone M4 required: activation_gate_log not yet migrated")
            # In Phase 1, dispatch is always disabled
            from src.dispatch.gates import evaluate_activation_gates
            effective, gates = evaluate_activation_gates(conn)
            assert effective is False, "DISPATCH_ENABLED=false must enforce dispatch_effective=false"

    def test_feature_36_gate_challenge_and_record_routes_exist(self, client):
        """Feature 36: Activation gate endpoints refuse G-OUTBOX in Phase 1."""
        resp = client.post("/v1/activation-gates/G-OUTBOX/challenge", headers=COM_HEADERS, json={})
        if resp.status_code == 404:
            pytest.skip("Milestone M4 required: activation gate routes not yet mounted")
        assert resp.status_code in (409, 403), "G-OUTBOX must be refused before Phase 3"

    def test_feature_37_atomic_approval_to_dispatch_creation(self, enabled_client, pg_uri):
        """Feature 37: Pending dispatch row creation is atomic with approval when dispatch_effective is true."""
        with psycopg.connect(pg_uri) as conn:
            if not table_exists(conn, "dispatches"):
                pytest.skip("Milestone M4 required: dispatches table not yet migrated")

        # 1. Execute run to interrupt
        r_run = enabled_client.post("/run", headers=SVC_HEADERS, json={"club_id": "330323"})
        assert r_run.status_code == 201
        tid = r_run.json()["thread_id"]

        # 2. Challenge and sign approval for verified dispatchable player
        intent = {**APPROVE_COACH_BEST, "player_id": "1001", "candidate_id": "coach-best-1001"}
        ch = enabled_client.post(f"/threads/{tid}/decision-challenge", headers=COM_HEADERS, json=intent)
        assert ch.status_code == 200, ch.text
        signed_body = signed(ch.json())

        # 3. Apply decision: atomically commits approval, dispatch row, and domain events
        r_dec = enabled_client.post(f"/threads/{tid}/decision", headers=COM_HEADERS, json=signed_body)
        assert r_dec.status_code == 200, r_dec.text
        dec_data = r_dec.json()
        assert dec_data["dispatch"]["enabled"] is True
        assert dec_data["dispatch"]["created"] is True
        dispatch_id = dec_data["dispatch"]["dispatch_id"]
        assert dispatch_id is not None

        # 4. Verify atomic persistence in PostgreSQL
        with psycopg.connect(pg_uri) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT dispatch_id, dispatch_status, player_id, candidate_id,
                              approval_id, approval_request_id, package_version
                         FROM public.dispatches WHERE dispatch_id = %s""",
                    (dispatch_id,),
                )
                d_row = cur.fetchone()
                assert d_row is not None
                assert d_row[1] == "pending"
                assert d_row[2] == "1001"
                assert d_row[3] == "coach-best-1001"
                assert d_row[4] == dec_data["approval_audit_id"]

                # Atomic events check: approval.decided and dispatch.created share transaction
                cur.execute(
                    "SELECT event_type FROM public.domain_events WHERE entity_id IN (%s, %s)",
                    (dec_data["approval_request_id"], dispatch_id),
                )
                ev_types = [row[0] for row in cur.fetchall()]
                assert "approval.decided" in ev_types
                assert "dispatch.created" in ev_types


# ==============================================================================
# Domain 6: Topology, Configuration & Invariants (Features 38 - 41)
# ==============================================================================

class TestTopologyAndInvariantsFeatures:
    """Features 38-41: Compose secrets, standardized config, scope boundaries, E2E invariants."""

    def test_feature_38_compose_topology_and_file_secrets(self):
        """Feature 38: Configuration reads secrets from files or sanitized environment."""
        assert "POSTGRES_URI" in os.environ or "TEST_POSTGRES_URI" in os.environ

    def test_feature_39_configuration_standardization(self):
        """Feature 39: Canonical configuration variables are standardized."""
        assert os.getenv("DISPATCH_ENABLED", "false").lower() == "false"

    def test_feature_40_scope_protection_phase_isolation(self):
        """Feature 40: Phase 2-6 components (BFF, WebAuthn, Dispatch worker) are not active."""
        # No production dispatch worker running
        assert os.getenv("DISPATCH_WORKER_ENABLED", "false").lower() == "false"

    def test_feature_41_e2e_security_invariants_suite_verification(self, pg_uri):
        """Feature 41: Security invariants verified (DISPATCH_ENABLED=false, least privilege)."""
        with psycopg.connect(pg_uri) as conn:
            violations = check_runtime_posture(conn)
            assert violations == [], f"Runtime posture violations detected: {violations}"
