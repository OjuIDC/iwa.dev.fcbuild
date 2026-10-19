"""Phase 1: approval security, immutable runs, atomic decisions, persisted nonces.
All tests here run against a real PostgreSQL (see tests/conftest.py)."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from fastapi.testclient import TestClient

from src import approval
from src.approval_store import ApprovalStore, StoreError
from src.schemas import DecisionIntent, SignedDecision
from src.security import Principal, Role
from tests.helpers import (APPROVE_COACH_BEST, COM, COMMISSIONER_ID, REJECT, SIGNING_SECRET, SVC,
                           StubPipeline, fake_packages, make_app, signed, signed_cli_hmac)

pytestmark = pytest.mark.usefixtures("pg_uri")
COMMISSIONER = Principal(Role.commissioner, COMMISSIONER_ID)




def start(client, **extra) -> str:
    r = client.post("/run", headers=SVC, json={"club_id": "330323", **extra})
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "awaiting_approval"
    return r.json()["thread_id"]


def decide(client, tid, intent=APPROVE_COACH_BEST, headers=COM):
    ch = client.post(f"/threads/{tid}/decision-challenge", headers=headers, json=intent)
    if ch.status_code != 200:
        return ch
    return client.post(f"/threads/{tid}/decision", headers=headers, json=signed_cli_hmac(ch.json()))


def db_status(pg_uri, tid):
    with psycopg.connect(pg_uri) as conn:
        row = conn.execute("SELECT status FROM graph_threads WHERE thread_id=%s", (tid,)).fetchone()
        audits = conn.execute("SELECT count(*) FROM approval_audit WHERE thread_id=%s", (tid,)).fetchone()[0]
    return row[0], audits


# ---------------------------------------------------------------- run creation
def test_thread_id_uses_required_convention(client):
    import re
    tid = start(client)
    assert re.fullmatch(r"fc27-330323-run-[0-9]{8}-001", tid)
    assert start(client).endswith("-002")


def test_run_rejects_existing_thread(client):
    tid = start(client)
    r = client.post("/run", headers=SVC, json={"club_id": "330323", "thread_id": tid})
    assert r.status_code == 409
    other = client.post("/run", headers=SVC, json={"club_id": "330323", "thread_id": tid, "trigger": "manual"})
    assert other.status_code == 409


def test_run_rejects_nonconforming_thread_id(client):
    for bad in ("demo-1", "fc27-330323-run-20261001-1", "fc27-999-run-20261001-001"):
        assert client.post("/run", headers=SVC, json={"club_id": "330323", "thread_id": bad}).status_code == 422


@pytest.mark.parametrize("decision_intent", [APPROVE_COACH_BEST, REJECT])
def test_terminal_thread_cannot_be_reset(client_v1_hmac, pg_uri, decision_intent):
    tid = start(client_v1_hmac)
    assert decide(client_v1_hmac, tid, decision_intent).status_code == 200
    before = db_status(pg_uri, tid)
    assert client_v1_hmac.post("/run", headers=SVC, json={"club_id": "330323", "thread_id": tid}).status_code == 409
    assert db_status(pg_uri, tid) == before
    assert decide(client_v1_hmac, tid, decision_intent).status_code == 409


def test_approved_thread_cannot_be_reset(client_v1_hmac, pg_uri):
    test_terminal_thread_cannot_be_reset(client_v1_hmac, pg_uri, APPROVE_COACH_BEST)


def test_rejected_thread_cannot_be_reset(client_v1_hmac, pg_uri):
    test_terminal_thread_cannot_be_reset(client_v1_hmac, pg_uri, REJECT)


def test_database_trigger_blocks_terminal_rewrite(client_v1_hmac, pg_uri):
    tid = start(client_v1_hmac)
    assert decide(client_v1_hmac, tid).status_code == 200
    with psycopg.connect(pg_uri) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE graph_threads SET status='awaiting_approval' WHERE thread_id=%s", (tid,))


# ------------------------------------------------------------- authentication
def test_missing_or_invalid_key_is_401(client):
    assert client.post("/run", json={}).status_code == 401
    assert client.post("/run", headers={"X-API-Key": "x" * 40}, json={}).status_code == 401
    assert client.get("/threads/fc27-1-run-20260101-001", headers={"X-API-Key": "bad key\n"}).status_code == 401


def test_service_token_cannot_approve(client_v1_hmac):
    tid = start(client_v1_hmac)
    assert client_v1_hmac.post(f"/threads/{tid}/decision-challenge", headers=SVC, json=APPROVE_COACH_BEST).status_code == 403
    com_ch = client_v1_hmac.post(f"/threads/{tid}/decision-challenge", headers=COM, json=APPROVE_COACH_BEST).json()
    assert client_v1_hmac.post(f"/threads/{tid}/decision", headers=SVC, json=signed_cli_hmac(com_ch)).status_code in (401, 403)
    assert client_v1_hmac.get(f"/threads/{tid}", headers=SVC).json()["status"] == "awaiting_approval"


def test_commissioner_token_cannot_start_runs(client):
    assert client.post("/run", headers=COM, json={"club_id": "330323"}).status_code == 403


def test_commissioner_body_field_does_not_authorize(client_v1_hmac):
    tid = start(client_v1_hmac)
    body = {**APPROVE_COACH_BEST, "commissioner": COMMISSIONER_ID}
    # Identity in the body is rejected outright, and never upgrades a service key.
    assert client_v1_hmac.post(f"/threads/{tid}/decision-challenge", headers=SVC, json=body).status_code == 403
    assert client_v1_hmac.post(f"/threads/{tid}/decision-challenge", headers=COM, json=body).status_code == 422


def test_invalid_commissioner_credential_is_rejected(client_v1_hmac):
    tid = start(client_v1_hmac)
    bad = {"X-API-Key": "commissioner-test-token-0123456789abcdef01234568"}
    assert client_v1_hmac.post(f"/threads/{tid}/decision-challenge", headers=bad, json=APPROVE_COACH_BEST).status_code == 401


def test_approver_identity_comes_from_credential(client_v1_hmac, pg_uri):
    tid = start(client_v1_hmac)
    assert decide(client_v1_hmac, tid).status_code == 200
    with psycopg.connect(pg_uri) as conn:
        assert conn.execute("SELECT approved_by FROM approval_audit WHERE thread_id=%s", (tid,)).fetchone()[0] == COMMISSIONER_ID


def test_voice_cannot_access_decision_route(client_v1_hmac):
    # The voice gateway holds only the service token; it is refused by role.
    tid = start(client_v1_hmac)
    assert decide(client_v1_hmac, tid, headers=SVC).status_code == 403
    from pathlib import Path
    source = Path("src/voice_gateway.py").read_text()
    assert "/decision" not in source and "COMMISSIONER" not in source and "APPROVAL_SIGNING_SECRET" not in source


# -------------------------------------------------------------- nonces/signing
def test_nonce_is_single_use(client_v1_hmac, pg_uri):
    tid = start(client_v1_hmac)
    ch = client_v1_hmac.post(f"/threads/{tid}/decision-challenge", headers=COM, json=REJECT).json()
    assert client_v1_hmac.post(f"/threads/{tid}/decision", headers=COM, json=signed_cli_hmac(ch)).status_code == 200
    diff_ch = {**ch, "notes": "other notes"}
    replay = client_v1_hmac.post(f"/threads/{tid}/decision", headers=COM, json=signed_cli_hmac(diff_ch))
    assert replay.status_code == 409


def test_nonce_replay_rejected_even_while_pending(client_v1_hmac, pg_uri):
    tid = start(client_v1_hmac)
    ch = client_v1_hmac.post(f"/threads/{tid}/decision-challenge", headers=COM, json=APPROVE_COACH_BEST).json()
    with psycopg.connect(pg_uri) as conn:
        conn.execute("UPDATE approval_nonces SET consumed_at=now() WHERE nonce=%s", (ch["nonce"],))
    r = client_v1_hmac.post(f"/threads/{tid}/decision", headers=COM, json=signed_cli_hmac(ch))
    assert r.status_code == 409 and "replay" in r.json()["detail"]
    assert db_status(pg_uri, tid) == ("awaiting_approval", 0)


def test_nonce_is_thread_bound(client_v1_hmac):
    t1, t2 = start(client_v1_hmac), start(client_v1_hmac)
    ch = client_v1_hmac.post(f"/threads/{t1}/decision-challenge", headers=COM, json=APPROVE_COACH_BEST).json()
    moved = {**ch, "thread_id": t2}
    r = client_v1_hmac.post(f"/threads/{t2}/decision", headers=COM, json=signed_cli_hmac(moved))
    assert r.status_code == 409 and "different thread" in r.json()["detail"]
    # Using the original payload against the other URL is rejected too.
    assert client_v1_hmac.post(f"/threads/{t2}/decision", headers=COM, json=signed_cli_hmac(ch)).status_code == 403


def test_nonce_is_candidate_bound(client_v1_hmac):
    tid = start(client_v1_hmac)
    ch = client_v1_hmac.post(f"/threads/{tid}/decision-challenge", headers=COM, json=APPROVE_COACH_BEST).json()
    swapped = {**ch, "selection": "best_runner_up", "candidate_id": "coach-best_runner_up-p2", "player_id": "p2"}
    r = client_v1_hmac.post(f"/threads/{tid}/decision", headers=COM, json=signed_cli_hmac(swapped))
    assert r.status_code == 409 and "does not match the issued challenge" in r.json()["detail"]


def test_nonce_expiration_is_enforced(client_v1_hmac, pg_uri):
    tid = start(client_v1_hmac)
    ch = client_v1_hmac.post(f"/threads/{tid}/decision-challenge", headers=COM, json=APPROVE_COACH_BEST).json()
    with psycopg.connect(pg_uri) as conn:
        conn.execute("UPDATE approval_nonces SET issued_at=now()-interval '20 minutes', expires_at=now()-interval '10 minutes' WHERE nonce=%s", (ch["nonce"],))
    r = client_v1_hmac.post(f"/threads/{tid}/decision", headers=COM, json=signed_cli_hmac(ch))
    assert r.status_code == 409 and "expired" in r.json()["detail"]


def test_modified_signed_payload_is_rejected(client_v1_hmac, pg_uri):
    tid = start(client_v1_hmac)
    ch = client_v1_hmac.post(f"/threads/{tid}/decision-challenge", headers=COM, json=APPROVE_COACH_BEST).json()
    good = signed_cli_hmac(ch)
    for field, value in (("player_id", "p2"), ("notes", "changed"), ("expires_at", ch["expires_at"] + 600),
                         ("decision", "REJECT")):
        tampered = {**good, field: value}
        try:
            r = client_v1_hmac.post(f"/threads/{tid}/decision", headers=COM, json=tampered)
            assert r.status_code in (403, 422, 409), (field, r.status_code)
        except Exception:
            assert field == "decision", f"Unexpected exception for field {field}"
    wrong_key = {**ch, "proof_type": "cli_hmac", "signature": approval.sign(ch, "another-secret-0123456789abcdef0123456789")}
    assert client_v1_hmac.post(f"/threads/{tid}/decision", headers=COM, json=wrong_key).status_code == 403
    assert db_status(pg_uri, tid) == ("awaiting_approval", 0)
    assert client_v1_hmac.post(f"/threads/{tid}/decision", headers=COM, json=good).status_code == 200


def test_package_version_immutable_once_awaiting_approval(client, pg_uri):
    tid = start(client)
    with psycopg.connect(pg_uri) as conn:
        with pytest.raises(psycopg.errors.CheckViolation, match="package_version is immutable"):
            conn.execute("UPDATE graph_threads SET package_version=%s WHERE thread_id=%s", ("f" * 64, tid))


def test_stale_candidate_package_cannot_be_approved(client_v1_hmac, pg_uri):
    tid = start(client_v1_hmac)
    ch = client_v1_hmac.post(f"/threads/{tid}/decision-challenge", headers=COM, json=APPROVE_COACH_BEST).json()
    stale_ch = {**ch, "package_version": "f" * 64}
    r = client_v1_hmac.post(f"/threads/{tid}/decision", headers=COM, json=signed_cli_hmac(stale_ch))
    assert r.status_code == 409 and ("stale" in r.json()["detail"] or "match" in r.json()["detail"])


def test_unknown_candidate_cannot_be_challenged(client_v1_hmac):
    tid = start(client_v1_hmac)
    bad = {**APPROVE_COACH_BEST, "candidate_id": "coach-best-p9", "player_id": "p9"}
    assert client_v1_hmac.post(f"/threads/{tid}/decision-challenge", headers=COM, json=bad).status_code == 409


# ------------------------------------------------------ atomicity/concurrency
def _challenge(store, tid, intent):
    payload = store.issue_challenge(tid, DecisionIntent.model_validate(intent), COMMISSIONER, 120)
    return SignedDecision.model_validate({**payload, "signature": approval.sign(payload, SIGNING_SECRET)})


def _race(pg_uri, tid, intents):
    store = ApprovalStore(pg_uri)
    decisions = [_challenge(store, tid, i) for i in intents]
    barrier = threading.Barrier(len(decisions))

    def attempt(d):
        barrier.wait()
        try:
            return store.apply_decision(tid, d, COMMISSIONER, SIGNING_SECRET)["status"]
        except StoreError as exc:
            return exc.status_code

    with ThreadPoolExecutor(len(decisions)) as pool:
        return list(pool.map(attempt, decisions))


def test_concurrent_approve_has_exactly_one_winner(client, pg_uri):
    tid = start(client)
    results = _race(pg_uri, tid, [APPROVE_COACH_BEST] * 8)
    assert results.count("approved") == 1 and results.count(409) == 7
    assert db_status(pg_uri, tid) == ("approved", 1)


def test_concurrent_approve_reject_has_one_terminal_result(client, pg_uri):
    tid = start(client)
    results = _race(pg_uri, tid, [APPROVE_COACH_BEST, REJECT] * 4)
    winners = [r for r in results if r in ("approved", "rejected")]
    assert len(winners) == 1 and results.count(409) == 7
    status, audits = db_status(pg_uri, tid)
    assert status == winners[0] and audits == 1
    with psycopg.connect(pg_uri) as conn:
        consumed = conn.execute("SELECT count(*) FROM approval_nonces WHERE thread_id=%s AND consumed_at IS NOT NULL", (tid,)).fetchone()[0]
    assert consumed == 1


def test_audit_and_decision_commit_together(client_v1_hmac, pg_uri):
    tid = start(client_v1_hmac)
    assert decide(client_v1_hmac, tid).status_code == 200
    with psycopg.connect(pg_uri) as conn:
        row = conn.execute(
            """SELECT g.status, g.approved_by, g.decision_payload->>'nonce', a.nonce, a.candidate_id, n.consumed_at IS NOT NULL
                 FROM graph_threads g JOIN approval_audit a USING (thread_id) JOIN approval_nonces n ON n.nonce = a.nonce
                WHERE g.thread_id=%s""", (tid,)).fetchone()
    assert row[0] == "approved" and row[1] == COMMISSIONER_ID and row[2] == row[3] and row[4] == "coach-best-p1" and row[5]


def test_failed_audit_rolls_back_decision(client, pg_uri, pg_admin_db_uri):
    tid = start(client)
    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        conn.execute("""CREATE FUNCTION fail_audit() RETURNS trigger LANGUAGE plpgsql AS $$
                        BEGIN RAISE EXCEPTION 'audit store unavailable'; END $$""")
        conn.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON approval_audit FOR EACH ROW EXECUTE FUNCTION fail_audit()")
    store = ApprovalStore(pg_uri)
    d = _challenge(store, tid, APPROVE_COACH_BEST)
    with pytest.raises(psycopg.Error):
        store.apply_decision(tid, d, COMMISSIONER, SIGNING_SECRET)
    assert db_status(pg_uri, tid) == ("awaiting_approval", 0)
    with psycopg.connect(pg_uri) as conn:
        assert conn.execute("SELECT consumed_at FROM approval_nonces WHERE nonce=%s", (d.nonce,)).fetchone()[0] is None


def test_thread_sequence_is_concurrency_safe(pg_uri):
    from src.thread_ids import allocate_thread_id
    barrier = threading.Barrier(16)

    def alloc(_):
        with psycopg.connect(pg_uri) as conn:
            barrier.wait()
            return allocate_thread_id(conn, "330323")

    with ThreadPoolExecutor(16) as pool:
        ids = list(pool.map(alloc, range(16)))
    assert len(set(ids)) == 16
    assert sorted(int(i.rsplit("-", 1)[1]) for i in ids) == list(range(1, 17))


def test_failed_pipeline_is_recorded_not_running(pg_uri):
    def boom(state):
        raise RuntimeError("model offline")
    app, _ = make_app(pg_uri, pipeline=boom, cli_hmac_enabled=True)
    with TestClient(app) as c:
        r = c.post("/run", headers=SVC, json={"club_id": "330323"})
    assert r.status_code == 201 and r.json()["status"] == "failed"
    tid = r.json()["thread_id"]
    with TestClient(app) as c:
        assert decide(c, tid).status_code == 409


def test_empty_package_is_not_approvable(pg_uri):
    pkgs = fake_packages()
    pkgs["coach"] = {"owner": "coach", "approvable": False}
    app, _ = make_app(pg_uri, pipeline=StubPipeline(pkgs), cli_hmac_enabled=True)
    with TestClient(app) as c:
        tid = start(c)
        assert decide(c, tid).status_code == 409

