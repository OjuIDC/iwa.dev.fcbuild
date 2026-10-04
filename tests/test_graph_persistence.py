"""Milestone 2: Graph Core & Persistence Tests.

Verifies:
- Single interrupt mechanism at approval node (no interrupt_before)
- Decision re-read from PostgreSQL on resume (untrusted resume payloads fail closed)
- Package version and hash mismatch fails closed
- Terminal states formatted as APPROVED, NOT DISPATCHED / REJECTED, NO ACTION TAKEN
- Checkpoint persistence and process restart recovery
- Startup reconciliation of committed decisions
"""
from __future__ import annotations

import json
import psycopg
import pytest
from fastapi.testclient import TestClient
from langgraph.types import Command

from src import approval
from src.approval_store import ApprovalStore
from src.graph import ApprovalNotCommitted, GraphDeps, GraphRunner, run_config
from src.schemas import DecisionIntent, SignedDecision
from src.security import Principal, Role
from tests.conftest import FakeRoles, dashboard
from tests.helpers import COM, SIGNING_SECRET, SVC, make_app, settings, signed


def start_run(client) -> dict:
    r = client.post("/run", headers=SVC, json={"club_id": "330323"})
    assert r.status_code == 201, r.text
    return r.json()


def approve_run(client, tid: str, owner="coach", selection="best"):
    state = client.get(f"/threads/{tid}", headers=SVC).json()
    cand = state["lead_packages"][owner][selection]
    intent = {
        "decision": "APPROVE",
        "owner": owner,
        "selection": selection,
        "candidate_id": cand["candidate_id"],
        "player_id": cand["player_id"],
    }
    ch = client.post(f"/threads/{tid}/decision-challenge", headers=COM, json=intent)
    assert ch.status_code == 200, ch.text
    return client.post(f"/threads/{tid}/decision", headers=COM, json=signed(ch.json()))


def reject_run(client, tid: str):
    intent = {"decision": "REJECT", "notes": "rejected"}
    ch = client.post(f"/threads/{tid}/decision-challenge", headers=COM, json=intent)
    assert ch.status_code == 200, ch.text
    return client.post(f"/threads/{tid}/decision", headers=COM, json=signed(ch.json()))


def test_single_approval_interrupt_at_approval_node(wired):
    """AC §11.2 / F18: Execution reaches approval with exactly ONE interrupt point and no interrupt_before."""
    runner, app = wired()
    with TestClient(app) as c:
        state = start_run(c)
    tid = state["thread_id"]

    values = runner.values(tid)
    assert values["graph_interrupted"] is True
    assert values["graph_next"] == ["approval"]

    # Verify upstream progression through all nodes
    audit_nodes = [e["node"] for e in values["audit_log"]]
    expected_upstream = ["search", "scout", "analyst", "coach", "tactics", "director", "prepare_approval"]
    assert audit_nodes == expected_upstream
    assert audit_nodes.count("approval") == 0

    # Verify compiled graph has NO interrupt_before on approval node
    snap = runner.app.get_state({"configurable": {"thread_id": tid}})
    assert len(snap.interrupts) == 1
    assert len(snap.tasks) == 1
    assert snap.tasks[0].name == "approval"
    assert snap.interrupts[0].value["thread_id"] == tid
    assert snap.interrupts[0].value["package_version"] == values["package_version"]
    assert values.get("approval_request_id") is not None
    assert snap.interrupts[0].value.get("approval_request_id") == values["approval_request_id"]


def test_resume_without_committed_postgres_decision_raises_error(wired, pg_uri):
    """AC §11.2 / F19: Resuming without a committed PostgreSQL decision fails closed."""
    runner, app = wired()
    with TestClient(app) as c:
        tid = start_run(c)["thread_id"]
    cfg = run_config(tid, "330323")

    # 1. Calling runner.resume(tid) returns current values without advancing
    res = runner.resume(tid)
    assert res["graph_next"] == ["approval"]
    assert res["status"] == "awaiting_approval"

    # 2. Direct invocation with resume command raises ApprovalNotCommitted
    with pytest.raises(ApprovalNotCommitted, match="no committed Commissioner decision"):
        runner.app.invoke(Command(resume={"thread_id": tid}), cfg, durability="sync")

    # Database status remains awaiting_approval
    with psycopg.connect(pg_uri) as conn:
        row = conn.execute("SELECT status FROM graph_threads WHERE thread_id=%s", (tid,)).fetchone()
        assert row[0] == "awaiting_approval"


def test_resume_payload_tampering_ignored(wired, pg_uri):
    """AC §11.2 / §7.2: Forged or altered resume payloads cannot authorize."""
    runner, app = wired()
    with TestClient(app) as c:
        tid = start_run(c)["thread_id"]
    cfg = run_config(tid, "330323")

    # 1. Malicious resume payload attempted directly
    forged_payload = {
        "status": "approved",
        "approved": True,
        "approved_by": "attacker",
        "commissioner_input": "APPROVE",
        "decision": "APPROVE",
        "package_version": "tampered_hash",
    }
    with pytest.raises(ApprovalNotCommitted):
        runner.app.invoke(Command(resume=forged_payload), cfg, durability="sync")
    assert runner.values(tid)["graph_next"] == ["approval"]

    # 2. Checkpoint state update attempted directly
    runner.app.update_state(
        {"configurable": {"thread_id": tid}},
        {"status": "approved", "approval_decision": {"approved_by": "attacker"}},
    )
    with pytest.raises(ApprovalNotCommitted):
        runner.app.invoke(Command(resume={"thread_id": tid}), cfg, durability="sync")
    assert runner.values(tid)["graph_next"] == ["approval"]

    # 3. Legitimate signed decision committed to DB resumes cleanly using DB identity
    with TestClient(app) as c:
        r = approve_run(c, tid)
        assert r.status_code == 200
    values = runner.values(tid)
    assert values["status"] in ("approved", "APPROVED, NOT DISPATCHED")
    assert values["approval_decision"]["approved_by"] == "batmanL8"
    assert values["graph_next"] == []


def test_decision_package_version_mismatch_fails_closed(wired, pg_uri):
    """AC §11.2 / §7.2: Decision committed with wrong package version raises ApprovalNotCommitted."""
    runner, app = wired()
    with TestClient(app) as c:
        tid = start_run(c)["thread_id"]
    cfg = run_config(tid, "330323")

    # Manually update approval_requests and graph_threads to approved but with mismatched version in payload
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with conn.transaction():
            conn.execute(
                """UPDATE public.approval_requests
                      SET status = 'approved', decided_at = now()
                    WHERE thread_id = %s""",
                (tid,),
            )
            conn.execute(
                """UPDATE public.graph_threads
                      SET status = 'approved',
                          decision_payload = jsonb_build_object('package_version', 'mismatched_hash_123', 'decision', 'APPROVE'),
                          approved_by = 'batmanL8',
                          approved_at = now()
                    WHERE thread_id = %s""",
                (tid,),
            )

    with pytest.raises(ApprovalNotCommitted, match="different package version"):
        runner.app.invoke(Command(resume={"thread_id": tid}), cfg, durability="sync")


def test_terminal_graph_states_formatting(wired, pg_uri):
    """AC §11.2 / F20: Terminal outcomes format as APPROVED, NOT DISPATCHED and REJECTED, NO ACTION TAKEN."""
    runner, app = wired()

    # 1. Test Approved terminal state
    with TestClient(app) as c:
        tid_app = start_run(c)["thread_id"]
        res_app = approve_run(c, tid_app)
        assert res_app.status_code == 200

    values_app = runner.values(tid_app)
    assert values_app["status"] == "APPROVED, NOT DISPATCHED"
    assert values_app["graph_next"] == []
    final_audit_app = values_app["audit_log"][-1]
    assert final_audit_app["node"] == "finalize_decision"
    assert "APPROVED, NOT DISPATCHED" in final_audit_app["summary"]
    # Verify forbidden wording is not present
    assert "APPROVED and ACTIVE" not in str(values_app)
    assert "ACTIVE" not in final_audit_app["summary"]

    # 2. Test Rejected terminal state
    with TestClient(app) as c:
        tid_rej = start_run(c)["thread_id"]
        res_rej = reject_run(c, tid_rej)
        assert res_rej.status_code == 200

    values_rej = runner.values(tid_rej)
    assert values_rej["status"] == "REJECTED, NO ACTION TAKEN"
    assert values_rej["graph_next"] == []
    final_audit_rej = values_rej["audit_log"][-1]
    assert final_audit_rej["node"] == "finalize_decision"
    assert "REJECTED, NO ACTION TAKEN" in final_audit_rej["summary"]


def test_checkpoint_recovery_across_server_restart(wired_restart, pg_uri):
    """AC §11.2 / §7.1: Checkpointer preserves state and enables recovery across runner restarts."""
    make_runner = wired_restart

    # Instance 1: start run and let it pause at approval
    runner1, app1 = make_runner()
    with TestClient(app1) as c:
        state1 = start_run(c)
    tid = state1["thread_id"]
    version1 = state1["package_version"]
    runner1.pool.close()

    # Instance 2: reconstruct fresh runner on the exact same PostgreSQL database
    runner2, app2 = make_runner()
    values2 = runner2.values(tid)
    assert values2["status"] == "awaiting_approval"
    assert values2["graph_interrupted"] is True
    assert values2["graph_next"] == ["approval"]
    assert values2["package_version"] == version1
    assert values2.get("approval_request_id") == state1.get("approval_request_id")
    assert "lead_packages" in values2

    # Resume on Instance 2 after legitimate decision commit
    with TestClient(app2) as c:
        res = approve_run(c, tid)
        assert res.status_code == 200

    values_final = runner2.values(tid)
    assert values_final["status"] == "APPROVED, NOT DISPATCHED"
    assert values_final["graph_next"] == []

    # Checkpoint history check
    checkpoints = list(runner2.checkpointer.list({"configurable": {"thread_id": tid}}))
    assert len(checkpoints) >= 10
    for cp in checkpoints:
        assert cp.metadata["fc_version"] == "27"
        assert cp.metadata["platform"] == "common-gen5"
        assert cp.metadata["club_id"] == "330323"


def test_repeated_resume_on_terminal_state_is_noop(wired):
    """AC §11.2 / §7.4: Repeated resume after terminal completion is safe no-op."""
    runner, app = wired()
    with TestClient(app) as c:
        tid = start_run(c)["thread_id"]
        approve_run(c, tid)

    before = runner.values(tid)
    assert before["status"] == "APPROVED, NOT DISPATCHED"
    log_count = len(before["audit_log"])

    # Attempt resume via runner
    res = runner.resume(tid)
    assert res["status"] == "APPROVED, NOT DISPATCHED"
    assert len(res["audit_log"]) == log_count

    # Attempt direct invoke with Command
    cfg = run_config(tid, "330323")
    runner.app.invoke(Command(resume={"thread_id": tid}), cfg, durability="sync")
    after = runner.values(tid)
    assert after["status"] == "APPROVED, NOT DISPATCHED"
    assert len(after["audit_log"]) == log_count


def test_startup_reconciliation_resumes_committed_threads(wired, pg_uri):
    """AC §11.2 / §4.6: Startup reconciliation resumes committed decisions without duplicate action."""
    runner, app = wired()
    with TestClient(app) as c:
        tid = start_run(c)["thread_id"]

    # Simulate crash: commit decision directly to PostgreSQL without waking the graph runner
    store = ApprovalStore(pg_uri)
    p = Principal(Role.commissioner, "batmanL8")
    ch = store.issue_challenge(tid, DecisionIntent(decision="REJECT"), p, 60)
    signed_payload = SignedDecision.model_validate({**ch, "signature": approval.sign(ch, SIGNING_SECRET)})
    store.apply_decision(tid, signed_payload, p, SIGNING_SECRET)

    # Graph is still awaiting resume
    assert runner.values(tid)["graph_next"] == ["approval"]

    # Recreate app to trigger startup lifespan reconciliation
    from src.graph_api import create_app
    app_reconciled = create_app(settings(pg_uri), runner=runner)
    with TestClient(app_reconciled):
        pass

    # Verified resumed
    assert runner.values(tid)["status"] == "REJECTED, NO ACTION TAKEN"
    assert runner.values(tid)["graph_next"] == []


# ==============================================================================
# ADVERSARIAL CHALLENGER (challenger_m2_1) EMPIRICAL TESTS
# ==============================================================================

def request_revision_run(client, tid: str):
    intent = {"decision": "REQUEST_REVISION", "notes": "need more midfield options"}
    ch = client.post(f"/threads/{tid}/decision-challenge", headers=COM, json=intent)
    assert ch.status_code == 200, ch.text
    return client.post(f"/threads/{tid}/decision", headers=COM, json=signed(ch.json()))


def test_adversarial_resume_payload_injection_variants(wired):
    """Attack 1.1: Malicious Command(resume=...) payloads must be completely ignored and fail closed."""
    runner, app = wired()
    with TestClient(app) as c:
        tid = start_run(c)["thread_id"]
    cfg = run_config(tid, "330323")

    malicious_payloads = [
        {"decision": "approved", "authorized": True, "approved_by": "attacker"},
        {"status": "APPROVED, NOT DISPATCHED", "override": True},
        {"package_version": "0" * 64, "signature": "bogus_signature"},
        {"commissioner_input": "APPROVE", "authorized_by": "commissioner"},
        "APPROVE",
        True,
        1337,
        ["forged", "authorization"],
    ]

    for payload in malicious_payloads:
        with pytest.raises(ApprovalNotCommitted, match="no committed Commissioner decision"):
            runner.app.invoke(Command(resume=payload), cfg, durability="sync")

        val = runner.values(tid)
        assert val["status"] == "awaiting_approval"
        assert val["graph_next"] == ["approval"]


def test_adversarial_state_tampering_prior_to_resume(wired):
    """Attack 1.2: Tampering LangGraph checkpoint state before resuming cannot bypass PostgreSQL truth."""
    runner, app = wired()
    with TestClient(app) as c:
        tid = start_run(c)["thread_id"]
    cfg = run_config(tid, "330323")

    runner.app.update_state(
        {"configurable": {"thread_id": tid}},
        {
            "status": "APPROVED, NOT DISPATCHED",
            "approval_decision": {
                "status": "approved",
                "approved_by": "forged_commissioner",
                "terminal_status": "APPROVED, NOT DISPATCHED",
            },
        },
    )

    with pytest.raises(ApprovalNotCommitted):
        runner.app.invoke(Command(resume={"thread_id": tid}), cfg, durability="sync")

    assert runner.values(tid)["graph_next"] == ["approval"]


def test_adversarial_cross_thread_checkpoint_hijack(wired):
    """Attack 1.3: Injecting an approved thread's identity into an unapproved thread fails."""
    runner, app = wired()
    with TestClient(app) as c:
        tid_approved = start_run(c)["thread_id"]
        res_approved = approve_run(c, tid_approved)
        assert res_approved.status_code == 200

        tid_pending = start_run(c)["thread_id"]

    cfg_pending = run_config(tid_pending, "330323")

    runner.app.update_state(
        {"configurable": {"thread_id": tid_pending}},
        {"thread_id": tid_approved},
    )

    with pytest.raises(ApprovalNotCommitted):
        runner.app.invoke(Command(resume={"thread_id": tid_pending}), cfg_pending, durability="sync")

    assert runner.values(tid_pending)["graph_next"] == ["approval"]


def test_adversarial_api_resume_route_payload_tampering(wired):
    """Attack 1.4: Calling POST /v1/runs/{id}/resume with extra or forged fields fails at schema validation."""
    runner, app = wired()
    with TestClient(app) as c:
        tid = start_run(c)["thread_id"]

        forged_bodies = [
            {"decision": "approved", "authorized": True},
            {"status": "approved"},
            {"approved_by": "attacker"},
            {"extra_field": "disallowed"},
        ]
        for body in forged_bodies:
            r = c.post(f"/v1/runs/{tid}/resume", headers=SVC, json=body)
            assert r.status_code == 422, f"Expected 422 for {body}, got {r.status_code}: {r.text}"


def test_adversarial_legitimate_resume_discards_malicious_body(wired):
    """Attack 1.5: Even if resume command carries a malicious payload, all state comes strictly from PostgreSQL."""
    runner, app = wired()
    with TestClient(app) as c:
        tid = start_run(c)["thread_id"]
        res = approve_run(c, tid)
        assert res.status_code == 200

    cfg = run_config(tid, "330323")

    malicious_resume_payload = {
        "status": "MALICIOUS_STATUS",
        "approved_by": "EVIL_HACKER",
        "authorized": False,
        "terminal_status": "APPROVED and ACTIVE",
    }

    runner.app.invoke(Command(resume=malicious_resume_payload), cfg, durability="sync")

    values = runner.values(tid)
    assert values["status"] == "APPROVED, NOT DISPATCHED"
    assert values["approval_decision"]["approved_by"] == "batmanL8"
    assert "EVIL_HACKER" not in str(values)
    assert "MALICIOUS_STATUS" not in str(values)


def test_adversarial_uncommitted_resume_on_non_terminal_statuses(wired, pg_uri):
    """Attack 2.2: Resuming when PostgreSQL row is in running, awaiting_approval, or expired fails closed."""
    runner, app = wired()
    with TestClient(app) as c:
        tid = start_run(c)["thread_id"]
    cfg = run_config(tid, "330323")

    # 1. Default awaiting_approval fails closed
    with pytest.raises(ApprovalNotCommitted, match="no committed Commissioner decision"):
        runner.app.invoke(Command(resume={"thread_id": tid}), cfg, durability="sync")

    val = runner.resume(tid)
    assert val["graph_next"] == ["approval"]

    # 2. Expired thread/request pair fails closed
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with conn.transaction():
            conn.execute("UPDATE public.approval_requests SET status = 'expired', expired_at = now() WHERE thread_id = %s", (tid,))
            conn.execute("UPDATE public.graph_threads SET status = 'expired' WHERE thread_id = %s", (tid,))

    with pytest.raises(ApprovalNotCommitted, match="no committed Commissioner decision"):
        runner.app.invoke(Command(resume={"thread_id": tid}), cfg, durability="sync")

    val2 = runner.resume(tid)
    assert val2["graph_next"] == ["approval"]


def test_adversarial_resume_refusal_unsupported_actor_type(wired, pg_uri):
    """Attack 2.3: Section 1A: DB decision with unsupported actor_type raises ApprovalNotCommitted."""
    runner, app = wired()

    for forbidden_actor in ["player", "delegate", "agent"]:
        with TestClient(app) as c:
            tid = start_run(c)["thread_id"]
        cfg = run_config(tid, "330323")
        pkg_version = runner.values(tid)["package_version"]

        with psycopg.connect(pg_uri, autocommit=True) as conn:
            with conn.transaction():
                conn.execute(
                    """UPDATE public.approval_requests
                          SET status = 'approved', decided_at = now()
                        WHERE thread_id = %s""",
                    (tid,),
                )
                conn.execute(
                    """UPDATE public.graph_threads
                          SET status = 'approved',
                              package_version = %s,
                              approved_by = 'test_actor',
                              approved_at = now(),
                              decision_payload = %s::jsonb
                        WHERE thread_id = %s""",
                    (pkg_version, json.dumps({
                        "package_version": pkg_version,
                        "decision": "APPROVE",
                        "actor_type": forbidden_actor,
                        "authority_basis": "primary",
                    }), tid),
                )

        with pytest.raises(ApprovalNotCommitted, match="unsupported actor type in Phase 1"):
            runner.app.invoke(Command(resume={"thread_id": tid}), cfg, durability="sync")


def test_adversarial_resume_refusal_unsupported_authority_basis(wired, pg_uri):
    """Attack 2.4: Section 1A: DB decision with unsupported authority_basis raises ApprovalNotCommitted."""
    runner, app = wired()

    for forbidden_basis in ["affected_player_fallback", "affected_player_consent", "explicit_delegate"]:
        with TestClient(app) as c:
            tid = start_run(c)["thread_id"]
        cfg = run_config(tid, "330323")
        pkg_version = runner.values(tid)["package_version"]

        with psycopg.connect(pg_uri, autocommit=True) as conn:
            with conn.transaction():
                conn.execute(
                    """UPDATE public.approval_requests
                          SET status = 'approved', decided_at = now()
                        WHERE thread_id = %s""",
                    (tid,),
                )
                conn.execute(
                    """UPDATE public.graph_threads
                          SET status = 'approved',
                              package_version = %s,
                              approved_by = 'test_actor',
                              approved_at = now(),
                              decision_payload = %s::jsonb
                        WHERE thread_id = %s""",
                    (pkg_version, json.dumps({
                        "package_version": pkg_version,
                        "decision": "APPROVE",
                        "actor_type": "commissioner",
                        "authority_basis": forbidden_basis,
                    }), tid),
                )

        with pytest.raises(ApprovalNotCommitted, match="unsupported authority basis in Phase 1"):
            runner.app.invoke(Command(resume={"thread_id": tid}), cfg, durability="sync")


def test_adversarial_resume_refusal_mismatched_approval_request_id(wired, pg_uri):
    """Attack 2.5: Committed decision pointing to a different approval_request_id fails closed."""
    runner, app = wired()
    with TestClient(app) as c:
        tid = start_run(c)["thread_id"]
    cfg = run_config(tid, "330323")

    # Commit decision directly to PostgreSQL without resuming the graph
    store = ApprovalStore(pg_uri)
    p = Principal(Role.commissioner, "batmanL8")
    ch = store.issue_challenge(tid, DecisionIntent(decision="REJECT"), p, 60)
    signed_payload = SignedDecision.model_validate({**ch, "signature": approval.sign(ch, SIGNING_SECRET)})
    store.apply_decision(tid, signed_payload, p, SIGNING_SECRET)

    # Graph is still waiting at approval
    assert runner.values(tid)["graph_next"] == ["approval"]

    # Bind a conflicting approval_request_id in state
    runner.app.update_state(
        {"configurable": {"thread_id": tid}},
        {"approval_request_id": "00000000-0000-0000-0000-000000000000"},
    )

    with pytest.raises(ApprovalNotCommitted, match="different approval request"):
        runner.app.invoke(Command(resume={"thread_id": tid}), cfg, durability="sync")


def test_adversarial_api_resume_endpoint_refusal_when_uncommitted(wired):
    """Attack 2.6: POST /v1/runs/{id}/resume returns HTTP 409 when thread is awaiting approval."""
    runner, app = wired()
    with TestClient(app) as c:
        tid = start_run(c)["thread_id"]
        r = c.post(f"/v1/runs/{tid}/resume", headers=SVC, json={})
        assert r.status_code == 409
        assert r.json()["detail"] == "approval_not_committed"


def test_adversarial_crash_recovery_with_rejection_decision(wired_restart, pg_uri):
    """Attack 3.2: Cold restart recovery with rejection terminal outcome."""
    make_runner = wired_restart

    runner1, app1 = make_runner()
    with TestClient(app1) as c1:
        s1 = start_run(c1)
    tid = s1["thread_id"]
    runner1.pool.close()
    del runner1
    del app1

    runner2, app2 = make_runner()
    with TestClient(app2) as c2:
        r = reject_run(c2, tid)
        assert r.status_code == 200

    final_values = runner2.values(tid)
    assert final_values["status"] == "REJECTED, NO ACTION TAKEN"
    assert final_values["graph_next"] == []


def test_adversarial_terminal_formatting_and_forbidden_strings(wired):
    """Attack 4.1: Exhaustive check of all 3 terminal statuses for strict formatting and absence of forbidden strings."""
    runner, app = wired()

    with TestClient(app) as c:
        # 1. Approved
        tid_app = start_run(c)["thread_id"]
        res_app = approve_run(c, tid_app)
        assert res_app.status_code == 200
        val_app = runner.values(tid_app)
        assert val_app["status"] == "APPROVED, NOT DISPATCHED"

        # 2. Rejected
        tid_rej = start_run(c)["thread_id"]
        res_rej = reject_run(c, tid_rej)
        assert res_rej.status_code == 200
        val_rej = runner.values(tid_rej)
        assert val_rej["status"] == "REJECTED, NO ACTION TAKEN"

        # 3. Revision Requested
        tid_rev = start_run(c)["thread_id"]
        res_rev = request_revision_run(c, tid_rev)
        assert res_rev.status_code == 200
        val_rev = runner.values(tid_rev)
        assert val_rev["status"] == "REVISION_REQUESTED, NO ACTION TAKEN"

    for val, expected_status in [
        (val_app, "APPROVED, NOT DISPATCHED"),
        (val_rej, "REJECTED, NO ACTION TAKEN"),
        (val_rev, "REVISION_REQUESTED, NO ACTION TAKEN"),
    ]:
        assert val["status"] == expected_status
        assert val["graph_next"] == []
        assert val["graph_interrupted"] is False

        assert val["approval_decision"]["terminal_status"] == expected_status

        audit_summaries = " ".join(e["summary"] for e in val["audit_log"])
        assert "APPROVED and ACTIVE" not in audit_summaries
        assert "ACTIVE" not in audit_summaries
        assert "dispatched" not in audit_summaries.lower().replace("not dispatched", "")

        final_entry = val["audit_log"][-1]
        assert final_entry["node"] == "finalize_decision"
        assert final_entry["summary"] == f"run finalized as {expected_status}"
