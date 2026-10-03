"""Test helpers: deterministic tokens (placeholders only), app factory, pipeline stub."""
from __future__ import annotations

import hashlib

from src import approval
from src.graph_api import ApiSettings, create_app
from src.security import Authenticator

SERVICE_TOKEN = "svc-test-token-0123456789abcdef0123456789abcdef"
COMMISSIONER_TOKEN = "commissioner-test-token-0123456789abcdef01234567"
SIGNING_SECRET = "signing-test-secret-0123456789abcdef0123456789ab"
COMMISSIONER_ID = "batmanL8"
SVC = {"X-API-Key": SERVICE_TOKEN}
COM = {"X-API-Key": COMMISSIONER_TOKEN}


def settings(pg_uri: str, ttl: int = 120, **kwargs) -> ApiSettings:
    args = dict(
        postgres_uri=pg_uri,
        authenticator=Authenticator(SERVICE_TOKEN, hashlib.sha256(COMMISSIONER_TOKEN.encode()).hexdigest(), COMMISSIONER_ID),
        signing_secret=SIGNING_SECRET,
        approval_ttl_seconds=ttl,
        redis_url=None,
        run_migrations=False,
    )
    args.update(kwargs)
    return ApiSettings(**args)


def candidate(role: str, sel: str, pid: str) -> dict:
    return {"candidate_id": f"{role}-{sel}-{pid}", "player_id": pid, "owner": role, "tier": "best",
            "score": 80.0, "rationale": "r", "action": "a", "evidence_refs": ["match:1"]}


def fake_packages(players=("p1", "p2", "p3")) -> dict:
    sels = ["best", "best_runner_up", "middle", "middle_runner_up", "lowest", "ignore"]
    out = {}
    for role in ("scout", "analyst", "coach", "tactics", "director"):
        pkg = {"owner": role, "approvable": True}
        for i, sel in enumerate(sels):
            if i < len(players):
                pkg[sel] = candidate(role, sel, players[i])
        out[role] = pkg
    return out


class StubPipeline:
    """Deterministic stand-in for the model pipeline (no models, no EA)."""

    def __init__(self, packages=None):
        self.packages = packages or fake_packages()
        self.calls = 0

    def __call__(self, state):
        self.calls += 1
        return {**state, "status": "awaiting_approval", "lead_packages": self.packages, "reports": {}}


def make_app(pg_uri: str, pipeline=None, ttl: int = 120):
    events = []
    app = create_app(settings(pg_uri, ttl), pipeline=pipeline or StubPipeline(),
                     publisher=lambda ch, payload: events.append((ch, payload)))
    return app, events


def signed(challenge: dict, secret: str = SIGNING_SECRET) -> dict:
    return {**challenge, "signature": approval.sign(challenge, secret)}


APPROVE_COACH_BEST = {"decision": "APPROVE", "owner": "coach", "selection": "best",
                      "candidate_id": "coach-best-p1", "player_id": "p1", "notes": "ok"}
REJECT = {"decision": "REJECT", "notes": "no"}
