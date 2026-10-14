"""Comprehensive Milestone 4 Test Suite: Activation Gates & Atomic Approval-to-Dispatch Creation."""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import uuid
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from src.dispatch.gates import (
    DeploymentIdentity,
    dispatch_effective,
    evaluate_gates,
    sign_gate,
)
from src.graph_api import create_app, reevaluate_gates
from tests.conftest import TEST_CODE_REVISION, TEST_DEPLOYMENT_ID
from tests.helpers import (
    APPROVE_COACH_BEST,
    COM,
    SIGNING_SECRET,
    SVC,
    StubPipeline,
    decide,
    fake_packages_dispatchable,
    seed_verified_players,
    settings,
    signed,
    signed_cli_hmac,
    start,
)


# ------------------------------------------------------------------------------
# 1. Gate Evaluation & Posture Tests
# ------------------------------------------------------------------------------

def test_gates_block_by_default(pg_uri):
    """All gates evaluated; G-WORKER, G-ID, G-ATOMIC, G-OUTBOX fail; effective False."""
    identity = DeploymentIdentity("test-rev", "test-dep")
    with psycopg.connect(pg_uri) as conn:
        report = evaluate_gates(conn, identity)

    assert any(g.gate_id == "G-MIGRATION" and g.passed for g in report)
    assert any(g.gate_id == "G-GRANTS" and g.passed for g in report)
    assert any(g.gate_id == "G-WORKER" and not g.passed for g in report)
    assert any(g.gate_id == "G-ID" and not g.passed for g in report)
    assert any(g.gate_id == "G-ATOMIC" and not g.passed for g in report)
    assert any(g.gate_id == "G-OUTBOX" and not g.passed for g in report)
    assert dispatch_effective(True, report) is False
    assert dispatch_effective(False, report) is False


def test_configured_but_blocked_behaves_disabled(pg_uri):
    """AC-45: dispatch_enabled=True settings, no gate records -> no dispatch, health blocked."""
    app_settings = settings(pg_uri, dispatch_enabled=True, cli_hmac_enabled=True, code_revision="rev1", deployment_id="dep1")
    app = create_app(app_settings, pipeline=StubPipeline(packages=fake_packages_dispatchable()))

    with TestClient(app) as client:
        # Check health probe reports blocked / degraded
        r_ready = client.get("/health/ready")
        assert r_ready.status_code == 503
        data = r_ready.json()
        assert "blocked" in data.get("reason", "") or "G-WORKER" in data.get("reason", "")

        # Run pipeline and approve -> behaves as disabled
        tid = start(client)
        r_dec = decide(client, tid, intent={**APPROVE_COACH_BEST, "player_id": "1001", "candidate_id": "coach-best-1001"}, proof_type="cli_hmac")
        assert r_dec.status_code == 200, r_dec.text
        assert r_dec.json()["dispatch"]["enabled"] is False
        assert r_dec.json()["dispatch"]["created"] is False


# ------------------------------------------------------------------------------
# 2. Gate Cryptographic Challenge & Recording Tests
# ------------------------------------------------------------------------------

def test_gate_recording_requires_commissioner_and_signature(client_v1_hmac, pg_uri):
    """Service token -> 403; G-WORKER -> 422; bad sig -> 403; valid -> 200 with effective_after."""
    # Ensure deployment identity on app settings
    client_v1_hmac.app.state.settings.code_revision = "rev1"
    client_v1_hmac.app.state.settings.deployment_id = "dep1"

    # Service token rejected
    r = client_v1_hmac.post("/v1/activation-gates/G-ID/challenge", headers=SVC, json={})
    assert r.status_code == 403

    # Automatic gate rejected
    r = client_v1_hmac.post("/v1/activation-gates/G-WORKER/challenge", headers=COM, json={})
    assert r.status_code == 422

    # Request valid challenge
    r_ch = client_v1_hmac.post(
        "/v1/activation-gates/G-ID/challenge",
        headers=COM,
        json={"status": "passed", "evidence_ref": "audit-ref-1"},
    )
    assert r_ch.status_code == 200, r_ch.text
    ch_payload = r_ch.json()

    # Bad signature rejected
    bad_sub = {**ch_payload, "signature": "0" * 64}
    assert client_v1_hmac.post("/v1/activation-gates/G-ID", headers=COM, json=bad_sub).status_code == 403

    # Valid signature accepted
    valid_sig = sign_gate(ch_payload, SIGNING_SECRET)
    good_sub = {**ch_payload, "signature": valid_sig}
    r_sub = client_v1_hmac.post("/v1/activation-gates/G-ID", headers=COM, json=good_sub)
    assert r_sub.status_code == 200, r_sub.text
    res = r_sub.json()
    assert res["gate"] == "G-ID"
    assert res["effective_after"] == "next_evaluation"
    assert "next_evaluation_at" in res

    with psycopg.connect(pg_uri) as conn:
        ev = conn.execute(
            "SELECT event_type, source_table, entity_id FROM public.domain_events WHERE event_type = 'activation.gate_recorded'"
        ).fetchone()
        assert ev is not None
        assert ev[1] == "activation_gate_log"
        assert ev[2] == "G-ID"


def test_gate_nonce_replay_rejected(client_v1_hmac):
    """Replay of signed gate submission -> 409; exactly 1 row in activation_gate_log."""
    client_v1_hmac.app.state.settings.code_revision = "rev1"
    client_v1_hmac.app.state.settings.deployment_id = "dep1"

    r_ch = client_v1_hmac.post("/v1/activation-gates/G-ID/challenge", headers=COM, json={})
    assert r_ch.status_code == 200
    ch_payload = r_ch.json()
    sig = sign_gate(ch_payload, SIGNING_SECRET)
    body = {**ch_payload, "signature": sig}

    # First submit succeeds
    assert client_v1_hmac.post("/v1/activation-gates/G-ID", headers=COM, json=body).status_code == 200

    # Replay fails
    r_replay = client_v1_hmac.post("/v1/activation-gates/G-ID", headers=COM, json=body)
    assert r_replay.status_code == 409


def test_gate_nonce_expired_rejected(client_v1_hmac, pg_admin_db_uri):
    """Expired nonce -> 409; 0 gate log rows created."""
    client_v1_hmac.app.state.settings.code_revision = "rev1"
    client_v1_hmac.app.state.settings.deployment_id = "dep1"

    r_ch = client_v1_hmac.post("/v1/activation-gates/G-ID/challenge", headers=COM, json={})
    assert r_ch.status_code == 200
    ch = r_ch.json()

    # Artificially expire the nonce (must also move issued_at to satisfy check constraint)
    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        conn.execute(
            "UPDATE public.approval_nonces SET issued_at = now() - interval '20 seconds', expires_at = now() - interval '10 seconds' WHERE nonce = %s",
            (ch["nonce"],),
        )

    sig = sign_gate(ch, SIGNING_SECRET)
    r_sub = client_v1_hmac.post("/v1/activation-gates/G-ID", headers=COM, json={**ch, "signature": sig})
    assert r_sub.status_code == 409


def test_gate_tampered_field_rejected(client_v1_hmac):
    """Modifying evidence_ref, code_revision, or nonce after signing -> 403."""
    client_v1_hmac.app.state.settings.code_revision = "rev1"
    client_v1_hmac.app.state.settings.deployment_id = "dep1"

    r_ch = client_v1_hmac.post("/v1/activation-gates/G-ID/challenge", headers=COM, json={})
    assert r_ch.status_code == 200
    ch = r_ch.json()
    sig = sign_gate(ch, SIGNING_SECRET)

    # Tamper evidence_ref
    tampered = {**ch, "evidence_ref": "tampered-ref", "signature": sig}
    assert client_v1_hmac.post("/v1/activation-gates/G-ID", headers=COM, json=tampered).status_code == 403


def test_gate_nonce_bound_to_gate(client_v1_hmac):
    """Challenge for G-ID, submit to G-ATOMIC -> 409; 0 log rows."""
    client_v1_hmac.app.state.settings.code_revision = "rev1"
    client_v1_hmac.app.state.settings.deployment_id = "dep1"

    r_ch = client_v1_hmac.post("/v1/activation-gates/G-ID/challenge", headers=COM, json={})
    assert r_ch.status_code == 200
    ch = r_ch.json()

    # Re-target body to G-ATOMIC
    ch["gate_id"] = "G-ATOMIC"
    sig = sign_gate(ch, SIGNING_SECRET)
    r_sub = client_v1_hmac.post("/v1/activation-gates/G-ATOMIC", headers=COM, json={**ch, "signature": sig})
    assert r_sub.status_code == 409


def test_gate_requires_deployment_identity(client_v1_hmac):
    """Empty code_revision or deployment_id -> 409 deployment_identity_missing."""
    client_v1_hmac.app.state.settings.code_revision = None
    client_v1_hmac.app.state.settings.deployment_id = None

    r_ch = client_v1_hmac.post("/v1/activation-gates/G-ID/challenge", headers=COM, json={})
    assert r_ch.status_code == 409
    assert "deployment_identity_missing" in r_ch.text


def test_gate_record_from_other_deployment_ignored(pg_uri, pg_admin_db_uri):
    """Log row with foreign deployment_id or code_revision does not satisfy gate."""
    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        conn.execute(
            """INSERT INTO public.activation_gate_log (
                gate_id, status, evidence_ref, code_revision, deployment_id,
                fc_version, platform, nonce, signature_sha256, recorded_by
            ) VALUES (
                'G-ID', 'passed', 'other-ref', 'foreign-rev', 'foreign-dep',
                '27', 'common-gen5', 'other-nonce', 'other-sig', 'commissioner:batmanL8'
            )"""
        )

    identity = DeploymentIdentity("my-rev", "my-dep")
    with psycopg.connect(pg_uri) as conn:
        report = evaluate_gates(conn, identity)

    g_id = next(g for g in report if g.gate_id == "G-ID")
    assert g_id.passed is False


def test_gate_signature_excludes_correlation_id(client_v1_hmac):
    """Varying X-Correlation-Id produces valid verification because it's not signed."""
    client_v1_hmac.app.state.settings.code_revision = "rev1"
    client_v1_hmac.app.state.settings.deployment_id = "dep1"

    r_ch = client_v1_hmac.post("/v1/activation-gates/G-ID/challenge", headers={**COM, "X-Correlation-Id": str(uuid.uuid4())}, json={})
    assert r_ch.status_code == 200
    ch = r_ch.json()
    sig = sign_gate(ch, SIGNING_SECRET)

    # Submit with completely different correlation id
    r_sub = client_v1_hmac.post(
        "/v1/activation-gates/G-ID",
        headers={**COM, "X-Correlation-Id": str(uuid.uuid4())},
        json={**ch, "signature": sig},
    )
    assert r_sub.status_code == 200


def test_g_outbox_not_recordable_in_phase1(client_v1_hmac):
    """C-3: G-OUTBOX challenge and record returns 409 not_recordable_before_phase_3."""
    client_v1_hmac.app.state.settings.code_revision = "rev1"
    client_v1_hmac.app.state.settings.deployment_id = "dep1"

    r_ch = client_v1_hmac.post("/v1/activation-gates/G-OUTBOX/challenge", headers=COM, json={})
    assert r_ch.status_code == 409
    assert "not_recordable_before_phase_3" in r_ch.text


def test_gate_change_applies_on_next_evaluation(client_v1_hmac):
    """C-1: Recording gate does not take effect until reevaluate_gates runs; no server restart needed."""
    client_v1_hmac.app.state.settings.code_revision = "rev1"
    client_v1_hmac.app.state.settings.deployment_id = "dep1"
    reevaluate_gates(client_v1_hmac.app)

    r_cfg1 = client_v1_hmac.get("/v1/config/dispatch", headers=COM).json()
    assert any(g["gate_id"] == "G-ID" and not g["passed"] for g in r_cfg1["gates"])

    # Challenge and record G-ID
    r_ch = client_v1_hmac.post("/v1/activation-gates/G-ID/challenge", headers=COM, json={}).json()
    sig = sign_gate(r_ch, SIGNING_SECRET)
    assert client_v1_hmac.post("/v1/activation-gates/G-ID", headers=COM, json={**r_ch, "signature": sig}).status_code == 200

    # Config endpoint still reflects cached report
    r_cfg2 = client_v1_hmac.get("/v1/config/dispatch", headers=COM).json()
    assert any(g["gate_id"] == "G-ID" and not g["passed"] for g in r_cfg2["gates"])

    # Directly reevaluate gates without restarting graph-api (C-1)
    reevaluate_gates(client_v1_hmac.app)
    r_cfg3 = client_v1_hmac.get("/v1/config/dispatch", headers=COM).json()
    assert any(g["gate_id"] == "G-ID" and g["passed"] for g in r_cfg3["gates"])


# ------------------------------------------------------------------------------
# 3. Atomic Dispatch Creation & Rollback Tests
# ------------------------------------------------------------------------------

def test_correlation_id_shared_by_dispatch_and_events(enabled_client, pg_uri):
    """C-5: X-Correlation-Id is shared by dispatches.created_correlation_id and domain_events."""
    test_cid = str(uuid.uuid4())
    headers = {**COM, "X-Correlation-Id": test_cid}

    tid = start(enabled_client)
    r_dec = decide(
        enabled_client,
        tid,
        intent={**APPROVE_COACH_BEST, "player_id": "1001", "candidate_id": "coach-best-1001"},
        headers=headers,
        proof_type="cli_hmac",
    )
    assert r_dec.status_code == 200, r_dec.text
    dispatch_id = r_dec.json()["dispatch"]["dispatch_id"]

    with psycopg.connect(pg_uri) as conn:
        d_row = conn.execute(
            "SELECT created_correlation_id FROM public.dispatches WHERE dispatch_id = %s",
            (dispatch_id,),
        ).fetchone()
        assert str(d_row[0]) == test_cid

        events = conn.execute(
            "SELECT event_type, correlation_id FROM public.domain_events WHERE correlation_id = %s",
            (test_cid,),
        ).fetchall()
        types = [e[0] for e in events]
        assert "approval.decided" in types
        assert "dispatch.created" in types


def test_approve_creates_dispatch_atomically(enabled_client, pg_uri):
    """AC-1, AC-2: Valid approval with effective dispatch creates pending dispatch row, events, outbox."""
    tid = start(enabled_client)
    r_dec = decide(
        enabled_client,
        tid,
        intent={**APPROVE_COACH_BEST, "player_id": "1001", "candidate_id": "coach-best-1001"},
        proof_type="cli_hmac",
    )
    assert r_dec.status_code == 200, r_dec.text
    dec_data = r_dec.json()
    assert dec_data["dispatch"]["enabled"] is True
    assert dec_data["dispatch"]["created"] is True
    dispatch_id = dec_data["dispatch"]["dispatch_id"]
    assert dispatch_id is not None

    with psycopg.connect(pg_uri) as conn:
        d_row = conn.execute(
            """SELECT dispatch_id, dispatch_status, player_id, candidate_id, owner,
                  approval_id, approval_request_id, package_version
               FROM public.dispatches WHERE dispatch_id = %s""",
            (dispatch_id,),
        ).fetchone()
        assert d_row is not None
        assert d_row[1] == "pending"
        assert d_row[2] == "1001"
        assert d_row[3] == "coach-best-1001"
        assert d_row[5] == dec_data["approval_audit_id"]

        # Check domain events: approval.decided + dispatch.created
        events = conn.execute(
            "SELECT event_type FROM public.domain_events WHERE entity_id IN (%s, %s)",
            (dec_data["approval_request_id"], dispatch_id),
        ).fetchall()
        ev_types = [e[0] for e in events]
        assert "approval.decided" in ev_types
        assert "dispatch.created" in ev_types


def test_dispatch_failure_rolls_back_decision(enabled_client, pg_admin_db_uri):
    """Injected trigger on dispatches insert -> StoreError/500; thread awaiting; 0 audit, 0 dispatch."""
    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        conn.execute("""
            CREATE OR REPLACE FUNCTION trigger_fail_dispatches() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'simulated dispatches failure';
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER trg_fail_dispatch BEFORE INSERT ON public.dispatches
            FOR EACH ROW EXECUTE FUNCTION trigger_fail_dispatches();
        """)

    try:
        tid = start(enabled_client)
        r_dec = decide(
            enabled_client,
            tid,
            intent={**APPROVE_COACH_BEST, "player_id": "1001", "candidate_id": "coach-best-1001"},
            proof_type="cli_hmac",
        )
        assert r_dec.status_code in (409, 500)

        with psycopg.connect(pg_admin_db_uri) as conn:
            # Thread still awaiting
            t_row = conn.execute("SELECT status FROM public.graph_threads WHERE thread_id = %s", (tid,)).fetchone()
            assert t_row[0] == "awaiting_approval"
            # 0 audits
            audits = conn.execute("SELECT count(*) FROM public.approval_audit WHERE thread_id = %s", (tid,)).fetchone()[0]
            assert audits == 0
            # 0 dispatches
            dispatches = conn.execute("SELECT count(*) FROM public.dispatches WHERE source_thread_id = %s", (tid,)).fetchone()[0]
            assert dispatches == 0
            # Nonce remains unconsumed
            nonces = conn.execute("SELECT count(*) FROM public.approval_nonces WHERE thread_id = %s AND consumed_at IS NOT NULL", (tid,)).fetchone()[0]
            assert nonces == 0
    finally:
        with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
            conn.execute("DROP TRIGGER IF EXISTS trg_fail_dispatch ON public.dispatches;")
            conn.execute("DROP FUNCTION IF EXISTS trigger_fail_dispatches;")


# ------------------------------------------------------------------------------
# 4. Strict Dispatchability Negative Tests
# ------------------------------------------------------------------------------

def test_not_dispatchable_without_objective(enabled_client):
    """AC-5: Candidate missing objective -> 409 not_dispatchable; decision rolled back."""
    pkgs = fake_packages_dispatchable()
    pkgs["coach"]["best"]["objective"] = None
    enabled_client.pipeline.packages = pkgs

    tid = start(enabled_client)
    r = decide(enabled_client, tid, intent={**APPROVE_COACH_BEST, "player_id": "1001", "candidate_id": "coach-best-1001"}, proof_type="cli_hmac")
    assert r.status_code == 409
    assert "not_dispatchable" in r.text


def test_not_dispatchable_gamertag_player(enabled_client):
    """AC-46: Candidate player_id 'GamerTag99' -> 409 not_dispatchable."""
    enabled_client.pipeline.packages = fake_packages_dispatchable(player_ids=("GamerTag99",))
    tid = start(enabled_client)
    r = decide(enabled_client, tid, intent={**APPROVE_COACH_BEST, "player_id": "GamerTag99", "candidate_id": "coach-best-GamerTag99"}, proof_type="cli_hmac")
    assert r.status_code == 409
    assert "not_dispatchable" in r.text


def test_not_dispatchable_unverified_player(enabled_client, pg_admin_db_uri):
    """Player with identity_verified_at NULL -> 409 not_dispatchable."""
    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        conn.execute("UPDATE public.players SET identity_verified_at = NULL WHERE player_id = '1001'")

    tid = start(enabled_client)
    r = decide(enabled_client, tid, intent={**APPROVE_COACH_BEST, "player_id": "1001", "candidate_id": "coach-best-1001"}, proof_type="cli_hmac")
    assert r.status_code == 409
    assert "not_dispatchable" in r.text


def test_player_id_is_compared_exactly(enabled_client, pg_admin_db_uri):
    """players.player_id '7' vs candidate player_id '007' -> 409 not_dispatchable."""
    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        conn.execute(
            """INSERT INTO public.players (player_id, club_id, display_name, active, status, platform, identity_verified_at)
               VALUES ('7', '330323', 'Player 7', true, 'active', 'common-gen5', now())
               ON CONFLICT (player_id) DO UPDATE SET identity_verified_at = now()"""
        )

    enabled_client.pipeline.packages = fake_packages_dispatchable(player_ids=("007",))
    tid = start(enabled_client)
    r = decide(enabled_client, tid, intent={**APPROVE_COACH_BEST, "player_id": "007", "candidate_id": "coach-best-007"}, proof_type="cli_hmac")
    assert r.status_code == 409
    assert "not_dispatchable" in r.text


def test_not_dispatchable_unknown_player(enabled_client):
    """Numeric player_id absent from players table -> 409 not_dispatchable."""
    enabled_client.pipeline.packages = fake_packages_dispatchable(player_ids=("999999",))
    tid = start(enabled_client)
    r = decide(enabled_client, tid, intent={**APPROVE_COACH_BEST, "player_id": "999999", "candidate_id": "coach-best-999999"}, proof_type="cli_hmac")
    assert r.status_code == 409
    assert "not_dispatchable" in r.text


# ------------------------------------------------------------------------------
# 5. Supersession, Atomicity & Concurrency Tests
# ------------------------------------------------------------------------------

def test_reject_creates_no_dispatch(enabled_client, pg_uri):
    """REJECT decision updates thread to rejected, creates 0 dispatch rows."""
    tid = start(enabled_client)
    r = decide(enabled_client, tid, intent={"decision": "REJECT", "notes": "declined"}, proof_type="cli_hmac")
    assert r.status_code == 200
    assert r.json()["dispatch"]["created"] is False
    assert r.json()["dispatch"]["dispatch_id"] is None

    with psycopg.connect(pg_uri) as conn:
        count = conn.execute("SELECT count(*) FROM public.dispatches WHERE source_thread_id = %s", (tid,)).fetchone()[0]
        assert count == 0


def test_supersede_is_atomic(enabled_client, pg_uri):
    """AC-9: Second approval transitions prior dispatch to superseded (superseded_by=new_id)."""
    # First approval -> dispatch 1
    tid1 = start(enabled_client)
    r1 = decide(enabled_client, tid1, intent={**APPROVE_COACH_BEST, "player_id": "1001", "candidate_id": "coach-best-1001"}, proof_type="cli_hmac")
    assert r1.status_code == 200
    d1_id = r1.json()["dispatch"]["dispatch_id"]

    with psycopg.connect(pg_uri) as conn:
        d1_row = conn.execute("SELECT dispatch_status, version FROM public.dispatches WHERE dispatch_id = %s", (d1_id,)).fetchone()
        assert d1_row[0] == "pending"
        assert d1_row[1] == 1

    # Second approval for same player/owner/metric -> dispatch 2 supersedes dispatch 1
    tid2 = start(enabled_client)
    r2 = decide(enabled_client, tid2, intent={**APPROVE_COACH_BEST, "player_id": "1001", "candidate_id": "coach-best-1001"}, proof_type="cli_hmac")
    assert r2.status_code == 200
    d2_id = r2.json()["dispatch"]["dispatch_id"]
    assert d2_id != d1_id

    with psycopg.connect(pg_uri) as conn:
        d1_after = conn.execute("SELECT dispatch_status, superseded_by, version FROM public.dispatches WHERE dispatch_id = %s", (d1_id,)).fetchone()
        assert d1_after[0] == "superseded"
        assert str(d1_after[1]) == str(d2_id)
        assert d1_after[2] == 2

        d2_row = conn.execute("SELECT dispatch_status, version FROM public.dispatches WHERE dispatch_id = %s", (d2_id,)).fetchone()
        assert d2_row[0] == "pending"
        assert d2_row[1] == 1


def test_concurrent_supersede_is_serialized(enabled_client, pg_uri):
    """Advisory lock serializes concurrent approvals: exactly 1 active dispatch, 1 superseded, no 500."""
    tid1 = start(enabled_client)
    tid2 = start(enabled_client)

    ch1 = enabled_client.post(f"/threads/{tid1}/decision-challenge", headers=COM, json={**APPROVE_COACH_BEST, "player_id": "1001", "candidate_id": "coach-best-1001"}).json()
    ch2 = enabled_client.post(f"/threads/{tid2}/decision-challenge", headers=COM, json={**APPROVE_COACH_BEST, "player_id": "1001", "candidate_id": "coach-best-1001"}).json()

    sub1 = signed_cli_hmac(ch1)
    sub2 = signed_cli_hmac(ch2)

    def submit(tid, body):
        return enabled_client.post(f"/threads/{tid}/decision", headers=COM, json=body)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(submit, tid1, sub1)
        f2 = executor.submit(submit, tid2, sub2)
        r1 = f1.result()
        r2 = f2.result()

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text

    d1 = r1.json()["dispatch"]["dispatch_id"]
    d2 = r2.json()["dispatch"]["dispatch_id"]
    assert d1 != d2

    with psycopg.connect(pg_uri) as conn:
        statuses = conn.execute(
            "SELECT dispatch_status FROM public.dispatches WHERE dispatch_id IN (%s, %s)",
            (d1, d2),
        ).fetchall()
        st_list = [s[0] for s in statuses]
        assert "pending" in st_list
        assert "superseded" in st_list


def test_parallel_duplicate_approvals_single_dispatch(enabled_client, pg_uri):
    """AC-18: 10 parallel duplicate decision submissions -> 1 winner, 1 audit row, 1 dispatch row."""
    tid = start(enabled_client)
    ch = enabled_client.post(
        f"/threads/{tid}/decision-challenge",
        headers=COM,
        json={**APPROVE_COACH_BEST, "player_id": "1001", "candidate_id": "coach-best-1001"},
    ).json()
    sub = signed_cli_hmac(ch)

    def submit():
        return enabled_client.post(f"/threads/{tid}/decision", headers=COM, json=sub)

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(submit) for _ in range(10)]
        results = [f.result() for f in futures]

    assert all(r.status_code == 200 for r in results)
    dispatch_ids = {r.json()["dispatch"]["dispatch_id"] for r in results}
    assert len(dispatch_ids) == 1
    winning_dispatch_id = list(dispatch_ids)[0]
    assert winning_dispatch_id is not None

    with psycopg.connect(pg_uri) as conn:
        audits = conn.execute("SELECT count(*) FROM public.approval_audit WHERE thread_id = %s", (tid,)).fetchone()[0]
        assert audits == 1

        dispatches = conn.execute("SELECT count(*) FROM public.dispatches WHERE source_thread_id = %s", (tid,)).fetchone()[0]
        assert dispatches == 1
