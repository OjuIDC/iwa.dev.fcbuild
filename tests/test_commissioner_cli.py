import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.helpers import COMMISSIONER_TOKEN, SERVICE_TOKEN, SIGNING_SECRET, SVC, make_app

spec = importlib.util.spec_from_file_location("cli", Path(__file__).resolve().parents[1] / "scripts/commissioner_decide.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


@pytest.fixture()
def wired(pg_uri, monkeypatch):
    app, _ = make_app(pg_uri)
    with TestClient(app) as client:
        monkeypatch.setattr(cli, "httpx", client)
        monkeypatch.setenv("FC27_GRAPH_URL", "http://testserver")
        monkeypatch.setenv("GRAPH_SERVICE_TOKEN", SERVICE_TOKEN)
        monkeypatch.setenv("GRAPH_COMMISSIONER_TOKEN", COMMISSIONER_TOKEN)
        monkeypatch.setenv("APPROVAL_SIGNING_SECRET", SIGNING_SECRET)
        yield client


def test_cli_approves_exact_candidate(wired, capsys):
    tid = wired.post("/run", headers=SVC, json={"club_id": "330323"}).json()["thread_id"]
    assert cli.main(["approve", tid, "coach", "best", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "approved by batmanL8" in out and SIGNING_SECRET not in out and COMMISSIONER_TOKEN not in out
    assert cli.main(["reject", tid, "--yes"]) == 1  # terminal thread


def test_cli_without_signing_secret_cannot_decide(wired, monkeypatch):
    tid = wired.post("/run", headers=SVC, json={"club_id": "330323"}).json()["thread_id"]
    monkeypatch.setenv("APPROVAL_SIGNING_SECRET", "wrong-secret-0123456789abcdef0123456789abcd")
    assert cli.main(["reject", tid, "--yes"]) == 1
    assert wired.get(f"/threads/{tid}", headers=SVC).json()["status"] == "awaiting_approval"
