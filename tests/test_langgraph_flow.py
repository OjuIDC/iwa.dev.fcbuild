"""Phase 2: real LangGraph StateGraph + PostgresSaver, interrupt/resume, role-driven ranking."""
from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient
from langgraph.types import Command

from src.approval_store import ApprovalStore
from src.graph import ApprovalNotCommitted, GraphDeps, GraphRunner, run_config
from src.roles import RoleOutput, RoleResult
from tests.helpers import COM, SVC, make_app, settings, signed

PLAYERS = {f"p{i}": {"rating": 9 - i * 0.4, "goals": 0, "assists": 0, "passattempts": 10, "passesmade": 8} for i in range(9)}


def dashboard(players=PLAYERS):
    return {"matches": [{"players": {"330323": players}}], "members": {}, "overall": {}}


class FakeRoles:
    def __init__(self, adjustments=None, invalid=()):
        self.adjustments = adjustments or {}
        self.invalid = set(invalid)
        self.calls = []

    def __call__(self, role, data, registry=None):
        self.calls.append(role)
        if role in self.invalid:
            return RoleResult(role=role, valid=False, error="invalid structured output: ValidationError", provider="ollama", model="m")
        assessments = [{"player_id": p, "adjustment": a, "reason": "fixture"} for p, a in self.adjustments.get(role, {}).items()]
        return RoleResult(role=role, valid=True, provider="ollama", model="qwen2.5:7b",
                          output=RoleOutput(summary=f"{role} summary", assessments=assessments))


@pytest.fixture()
def wired(pg_uri):
    def build(dash=None, roles=None, mcp_fails=False):
        store = ApprovalStore(pg_uri)

        def mcp(tool, args):
            if mcp_fails:
                raise ConnectionError("mcp down")
            return dash if dash is not None else dashboard()

        runner = GraphRunner.from_conninfo(pg_uri, GraphDeps(mcp=mcp, role_runner=roles or FakeRoles(),
                                                            registry_factory=lambda: None, decision_reader=store.load))
        from src.graph_api import create_app
        events = []
        app = create_app(settings(pg_uri), runner=runner, publisher=lambda c, p: events.append(c))
        return runner, app
    runners = []

    def factory(**kw):
        r, app = build(**kw)
        runners.append(r)
        return r, app
    yield factory
    for r in runners:
        r.pool.close()


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
    assert values["status"] == "approved" and values["graph_next"] == []
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
    with pytest.raises(ApprovalNotCommitted):
        runner.resume(tid)
    # After the failed attempts a genuine committed decision still resumes the same thread.
    with TestClient(app) as c:
        r = approve(c, tid)
        assert r.status_code == 200 and r.json()["graph_resume"] == "resumed"
    assert runner.values(tid)["status"] == "approved" and runner.values(tid)["graph_next"] == []


def test_terminal_graph_cannot_resume_to_pending(wired, pg_uri):
    runner, app = wired()
    with TestClient(app) as c:
        tid = run(c)["thread_id"]
        assert approve(c, tid).status_code == 200
    before = runner.values(tid)
    runner.resume(tid)
    runner.app.invoke(Command(resume={"thread_id": tid}), run_config(tid, "330323"), durability="sync")
    after = runner.values(tid)
    assert after["status"] == "approved" and after["graph_next"] == [] and len(after["audit_log"]) == len(before["audit_log"])
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
    assert runner.values(tid)["status"] == "rejected" and runner.values(tid)["graph_next"] == []


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
