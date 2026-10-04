"""Phase 2: real LangGraph StateGraph + PostgresSaver, interrupt/resume, role-driven ranking."""
from __future__ import annotations

import json

import psycopg
import pytest
from fastapi.testclient import TestClient
from langgraph.types import Command

from src.approval_store import ApprovalStore
from src.graph import ApprovalNotCommitted, GraphDeps, GraphRunner, run_config
from src.roles import RoleOutput, RoleResult
from tests.helpers import COM, SVC, make_app, settings, signed
from tests.conftest import PLAYERS, dashboard, FakeRoles
from tests.test_graph_persistence import (
    test_single_approval_interrupt_at_approval_node,
    test_resume_without_committed_postgres_decision_raises_error,
    test_resume_payload_tampering_ignored,
    test_decision_package_version_mismatch_fails_closed,
    test_terminal_graph_states_formatting,
    test_checkpoint_recovery_across_server_restart,
    test_repeated_resume_on_terminal_state_is_noop,
    test_startup_reconciliation_resumes_committed_threads,
)
from tests.test_graph_api_v1 import (
    test_malformed_correlation_id_rejected,
    test_correlation_id_generated_when_absent,
    test_correlation_id_echoed_when_present,
    test_correlation_id_propagated_to_logs,
    test_correlation_id_not_signed,
    test_v1_post_runs_creates_durable_run,
    test_v1_post_runs_validation_and_rejection,
    test_v1_get_run_safe_projection_omits_secrets,
    test_v1_get_approval_request_projection,
    test_v1_resume_refuses_authorization_payload,
    test_v1_resume_fails_closed_when_uncommitted,
    test_v1_decision_commits_db_before_resume,
    test_v1_approval_request_decisions_route,
    test_v1_kill_run_lifecycle,
    test_health_probes_liveness_and_readiness,
    test_health_ready_fails_if_dispatch_enabled_true,
    test_hmac_kill_switch_behavior,
    test_legacy_aliases_match_v1,
)



def run(client):
    r = client.post("/run", headers=SVC, json={"club_id": "330323"})
    assert r.status_code == 201, r.text
    return r.json()


def approve(client, tid, owner="coach", selection="best"):
    state = client.get(f"/threads/{tid}", headers=SVC).json()
    cand = state["lead_packages"][owner][selection]
    intent = {"decision": "APPROVE", "owner": owner, "selection": selection,
              "candidate_id": cand["candidate_id"], "player_id": cand["player_id"]}
    ch = client.post(f"/threads/{tid}/decision-challenge", headers=COM, json=intent)
    if ch.status_code != 200:
        return ch
    return client.post(f"/threads/{tid}/decision", headers=COM, json=signed(ch.json()))


def test_graph_runs_to_interrupt_with_typed_state(wired):
    runner, app = wired()
    with TestClient(app) as c:
        state = run(c)
    assert state["status"] == "awaiting_approval"
    values = runner.values(state["thread_id"])
    assert values["graph_next"] == ["approval"] and values["graph_interrupted"]
    assert [e["node"] for e in values["audit_log"]] == ["search", "scout", "analyst", "coach", "tactics", "director", "prepare_approval"]
    for key in ("fc_version", "platform", "run_seq", "prompt_version", "policy_version", "code_revision", "model_ids", "package_version"):
        assert values.get(key) is not None, key
    assert values["run_seq"] == 1 and values["fc_version"] == "27"


def test_existing_langgraph_thread_resumes(wired):
    runner, app = wired()
    with TestClient(app) as c:
        tid = run(c)["thread_id"]
        r = approve(c, tid)
        assert r.status_code == 200 and r.json()["graph_resume"] == "resumed"
    values = runner.values(tid)
    assert values["status"] in ("approved", "APPROVED, NOT DISPATCHED") and values["graph_next"] == []
    assert values["approval_decision"]["approved_by"] == "batmanL8"
    assert values["audit_log"][-1]["node"] == "finalize_decision"


def test_langgraph_interrupt_cannot_be_bypassed(wired, pg_uri):
    runner, app = wired()
    with TestClient(app) as c:
        tid = run(c)["thread_id"]
    cfg = run_config(tid, "330323")
    # Forged resume value and forged state fields do not count as approval.
    runner.app.update_state({"configurable": {"thread_id": tid}}, {"status": "approved", "approval_decision": {"approved_by": "x"}})
    with pytest.raises(ApprovalNotCommitted):
        runner.app.invoke(Command(resume={"status": "approved", "approved_by": "attacker"}), cfg, durability="sync")
    assert runner.values(tid)["graph_next"] == ["approval"]
    with psycopg.connect(pg_uri) as conn:
        assert conn.execute("SELECT status FROM graph_threads WHERE thread_id=%s", (tid,)).fetchone()[0] == "awaiting_approval"
    # Calling runner.resume without a committed decision returns values without advancing the graph
    runner.resume(tid)
    assert runner.values(tid)["graph_next"] == ["approval"]
    # After the failed attempts a genuine committed decision still resumes the same thread.
    with TestClient(app) as c:
        r = approve(c, tid)
        assert r.status_code == 200 and r.json()["graph_resume"] == "resumed"
    assert runner.values(tid)["status"] in ("approved", "APPROVED, NOT DISPATCHED") and runner.values(tid)["graph_next"] == []


def test_terminal_graph_cannot_resume_to_pending(wired, pg_uri):
    runner, app = wired()
    with TestClient(app) as c:
        tid = run(c)["thread_id"]
        assert approve(c, tid).status_code == 200
    before = runner.values(tid)
    runner.resume(tid)
    runner.app.invoke(Command(resume={"thread_id": tid}), run_config(tid, "330323"), durability="sync")
    after = runner.values(tid)
    assert after["status"] in ("approved", "APPROVED, NOT DISPATCHED") and after["graph_next"] == [] and len(after["audit_log"]) == len(before["audit_log"])
    with psycopg.connect(pg_uri) as conn:
        assert conn.execute("SELECT status FROM graph_threads WHERE thread_id=%s", (tid,)).fetchone()[0] == "approved"


def test_checkpoint_metadata_contains_fc_version_and_platform(wired):
    runner, app = wired()
    with TestClient(app) as c:
        tid = run(c)["thread_id"]
        approve(c, tid)
    checkpoints = list(runner.checkpointer.list({"configurable": {"thread_id": tid}}))
    # input + 7 nodes before the interrupt + approval + finalize, each persisted (durability=sync)
    assert len(checkpoints) >= 10
    for cp in checkpoints:
        assert cp.metadata["fc_version"] == "27" and cp.metadata["platform"] == "common-gen5"
        assert cp.metadata["club_id"] == "330323"


def test_resume_reconciled_on_startup(wired, pg_uri):
    runner, app = wired()
    with TestClient(app) as c:
        tid = run(c)["thread_id"]
    # Commit a decision without resuming the graph (simulates a crash between commit and resume).
    from src import approval
    from src.schemas import DecisionIntent, SignedDecision
    from src.security import Principal, Role
    from tests.helpers import SIGNING_SECRET
    store = ApprovalStore(pg_uri)
    p = Principal(Role.commissioner, "batmanL8")
    ch = store.issue_challenge(tid, DecisionIntent(decision="REJECT"), p, 60)
    store.apply_decision(tid, SignedDecision.model_validate({**ch, "signature": approval.sign(ch, SIGNING_SECRET)}), p, SIGNING_SECRET)
    assert runner.values(tid)["graph_next"] == ["approval"]
    with TestClient(app):  # startup reconciliation
        pass
    assert runner.values(tid)["status"] in ("rejected", "REJECTED, NO ACTION TAKEN") and runner.values(tid)["graph_next"] == []


def test_model_output_affects_ranking_fixture(wired):
    # Base scores p0=94 ... p8=86. Without adjustments p0 is best everywhere; the
    # coach role's bounded adjustments (+10 p8, -10 p0) must reorder only its own package.
    close = {f"p{i}": {"rating": 9 - i * 0.1, "passattempts": 10, "passesmade": 8} for i in range(9)}
    runner, app = wired(dash=dashboard(close), roles=FakeRoles(adjustments={"coach": {"p8": 10.0, "p0": -10.0}}))
    with TestClient(app) as c:
        state = run(c)
    pk = state["lead_packages"]
    assert pk["analyst"]["best"]["player_id"] == "p0"
    assert pk["coach"]["best"]["player_id"] == "p8"
    assert pk["coach"]["best"]["score"] == 96.0 and pk["analyst"]["best"]["score"] == 94.0
    assert pk["coach"]["ignore"]["player_id"] == "p0"


def test_invalid_role_output_makes_package_unapprovable(wired):
    runner, app = wired(roles=FakeRoles(invalid={"scout"}))
    with TestClient(app) as c:
        state = run(c)
        assert state["lead_packages"]["scout"]["approvable"] is False
        assert "scout" not in state["approval_request"]["approvable_owners"]
        intent = {"decision": "APPROVE", "owner": "scout", "selection": "best", "candidate_id": "x" * 24, "player_id": "p0"}
        assert c.post(f"/threads/{state['thread_id']}/decision-challenge", headers=COM, json=intent).status_code == 409
        assert approve(c, state["thread_id"], owner="analyst").status_code == 200


def test_lead_packages_do_not_duplicate_players(wired):
    few = {k: PLAYERS[k] for k in ("p0", "p1", "p2", "p3")}
    runner, app = wired(dash=dashboard(few))
    with TestClient(app) as c:
        state = run(c)
    for pkg in state["lead_packages"].values():
        chosen = [pkg[s] for s in ("best", "best_runner_up", "middle", "middle_runner_up", "lowest", "ignore") if s in pkg]
        assert len({c["player_id"] for c in chosen}) == len(chosen) == pkg["availability"]["selections"]
        assert pkg["availability"]["players"] == 4 and len(chosen) < 6


def test_empty_candidates_are_not_approvable(wired):
    runner, app = wired(dash={"matches": [], "members": {}, "overall": {}})
    with TestClient(app) as c:
        state = run(c)
        assert all(p["approvable"] is False and "best" not in p for p in state["lead_packages"].values())
        assert "unassigned" not in str(state["lead_packages"])
        intent = {"decision": "APPROVE", "owner": "coach", "selection": "best", "candidate_id": "x" * 24, "player_id": "unassigned"}
        assert c.post(f"/threads/{state['thread_id']}/decision-challenge", headers=COM, json=intent).status_code == 409


def test_search_failure_ends_run_as_failed(wired):
    runner, app = wired(mcp_fails=True)
    with TestClient(app) as c:
        state = run(c)
    assert state["status"] == "failed" and runner.values(state["thread_id"])["graph_next"] == []


def test_audit_query_shows_progression_interrupt_and_decision(wired, pg_uri):
    from src.audit_query import ThreadNotFound, get_audit
    runner, app = wired()
    with TestClient(app) as c:
        tid = run(c)["thread_id"]
        pending = get_audit(pg_uri, tid, runner=runner)
        assert pending["db_status"] == "awaiting_approval" and pending["committed_decision"] is None
        assert pending["checkpoints"][-1]["interrupted"] is True
        approve(c, tid)
    report = get_audit(pg_uri, tid, runner=runner)
    assert report["node_progression"][-2:] == ["approval", "finalize_decision"]
    assert report["committed_decision"]["approved_by"] == "batmanL8"
    assert all(cp["fc_version"] == "27" and cp["platform"] == "common-gen5" for cp in report["checkpoints"])
    blob = json.dumps(report, default=str)
    assert "\"signature\"" not in blob and "\"signature_sha256\"" not in blob and "\"dashboard\":" not in blob
    with pytest.raises(ThreadNotFound):
        get_audit(pg_uri, "fc27-330323-run-20200101-999", runner=runner)

