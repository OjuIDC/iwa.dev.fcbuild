"""Test helpers: deterministic tokens (placeholders only), app factory, pipeline stub."""
from __future__ import annotations

import hashlib
from typing import Any, Iterable

from src import approval
from src.graph_api import ApiSettings, create_app
from src.security import Authenticator

SERVICE_TOKEN = "svc-test-token-0123456789abcdef0123456789abcdef"
COMMISSIONER_TOKEN = "commissioner-test-token-0123456789abcdef01234567"
SIGNING_SECRET = "signing-test-secret-0123456789abcdef0123456789ab"
COMMISSIONER_ID = "batmanL8"
# Test-only bootstrap secret (never used outside the test suite). Satisfies the
# bootstrap secret quality policy in src/identity/recovery.py.
TEST_BOOTSTRAP_TOKEN = "bootstrap-testsuite-7Qm2Xv9Lk4Rt8Np3Wz6Hc1Jd5Fs0Gb"
# Test-only Phase 2 identity configuration. Never used outside the test suite.
TEST_IDENTITY_ENV = {
    "BFF_SERVICE_TOKEN": "test-bff-service-token-0123456789abcdef0123456789abcdef",
    "GRAPH_SERVICE_TOKEN": "test-graph-service-token-0123456789abcdef0123456789abcdef",
    # 44-char urlsafe base64 Fernet key (32 bytes of 0x01..0x20); test-only.
    "IDENTITY_ENCRYPTION_KEY": "AQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyA=",
}
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
        cli_hmac_enabled=True,
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


def make_app(pg_uri: str, pipeline=None, ttl: int = 120, cli_hmac_enabled: bool = True):
    events = []
    app = create_app(settings(pg_uri, ttl, cli_hmac_enabled=cli_hmac_enabled), pipeline=pipeline or StubPipeline(),
                     publisher=lambda ch, payload: events.append((ch, payload)))
    return app, events


def signed(challenge: dict, secret: str = SIGNING_SECRET) -> dict:
    return {
        **challenge,
        "proof_type": challenge.get("proof_type", "cli_hmac"),
        "signature": approval.sign(challenge, secret),
    }


def signed_cli_hmac(challenge: dict, secret: str = SIGNING_SECRET) -> dict:
    """Create a CliHmacDecisionSubmission payload with proof_type and HMAC signature."""
    # The challenge response has all the fields needed for CliHmacDecisionSubmission
    # except proof_type and signature/hmac_signature
    return {
        **challenge,
        "proof_type": "cli_hmac",
        "hmac_signature": approval.sign(challenge, secret),
    }


APPROVE_COACH_BEST = {"decision": "APPROVE", "owner": "coach", "selection": "best",
                      "candidate_id": "coach-best-p1", "player_id": "p1", "notes": "ok"}
REJECT = {"decision": "REJECT", "notes": "no"}
REQUEST_REVISION = {"decision": "REQUEST_REVISION", "notes": "Please revise the tactical formation."}


def legacy_install(admin_db_uri: str) -> None:
    """Installs 0001, 0002 and PostgresSaver checkpoints as admin (pre-bootstrap legacy state)."""
    from pathlib import Path
    import psycopg
    migrations_dir = Path(__file__).resolve().parents[1] / "migrations"
    with psycopg.connect(admin_db_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version    text PRIMARY KEY,
                    name       text NOT NULL,
                    sha256     text NOT NULL,
                    applied_at timestamptz NOT NULL DEFAULT now()
                )
            """)
            for v, filename in [("0001", "0001_initial.sql"), ("0002", "0002_approval_security.sql")]:
                sql_path = migrations_dir / filename
                content = sql_path.read_text(encoding="utf-8")
                digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
                cur.execute(content)
                cur.execute(
                    "INSERT INTO schema_migrations(version, name, sha256) VALUES (%s, %s, %s)",
                    (v, filename, digest),
                )
        from langgraph.checkpoint.postgres import PostgresSaver
        PostgresSaver(conn).setup()


def start(client, **extra) -> str:
    r = client.post("/run", headers=SVC, json={"club_id": "330323", **extra})
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "awaiting_approval"
    return r.json()["thread_id"]


def decide(client, tid, intent=APPROVE_COACH_BEST, headers=COM, proof_type=None):
    ch = client.post(f"/threads/{tid}/decision-challenge", headers=headers, json=intent)
    if ch.status_code != 200:
        return ch
    if proof_type == "cli_hmac":
        return client.post(f"/threads/{tid}/decision", headers=headers, json=signed_cli_hmac(ch.json()))
    return client.post(f"/threads/{tid}/decision", headers=headers, json=signed(ch.json()))


def db_status(pg_uri, tid):
    import psycopg
    with psycopg.connect(pg_uri) as conn:
        row = conn.execute(
            "SELECT status, (SELECT count(*) FROM approval_audit WHERE thread_id=%s) AS audits "
            "FROM graph_threads WHERE thread_id=%s",
            (tid, tid),
        ).fetchone()
        return (row[0], row[1]) if row else None


def fake_packages_dispatchable(player_ids: tuple[str, ...] = ("1001", "1002", "1003")) -> dict[str, Any]:
    """Generates lead packages with valid Objectives and numeric player IDs."""
    sels = ["best", "best_runner_up", "middle", "middle_runner_up", "lowest", "ignore"]
    out = {}
    valid_objective = {
        "metric": "pass_accuracy",
        "direction": "increase",
        "target_delta": 5.0,
        "min_matches": 3,
        "max_matches": 5,
        "bounds_policy": "fc27-objective-bounds-v1",
    }
    for role in ("scout", "analyst", "coach", "tactics", "director"):
        pkg = {"owner": role, "approvable": True}
        for i, sel in enumerate(sels):
            if i < len(player_ids):
                pid = player_ids[i]
                pkg[sel] = {
                    "candidate_id": f"{role}-{sel}-{pid}",
                    "player_id": str(pid),
                    "owner": role,
                    "tier": "best" if "best" in sel else ("middle" if "middle" in sel else "lowest"),
                    "score": 80.0,
                    "rationale": f"Valid recommendation for {pid}",
                    "action": "drill",
                    "evidence_refs": ["match:1"],
                    "objective": valid_objective,
                }
        out[role] = pkg
    return out


def seed_verified_players(admin_db_uri: str, ids: Iterable[str], club_id: str = "330323") -> None:
    """Seeds active, verified players in public.players via admin connection."""
    import psycopg
    with psycopg.connect(admin_db_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            for pid in ids:
                cur.execute(
                    """INSERT INTO public.players (
                        player_id, club_id, display_name, active, status, platform, identity_verified_at
                    ) VALUES (%s, %s, %s, true, 'active', 'common-gen5', now())
                    ON CONFLICT (player_id) DO UPDATE
                    SET identity_verified_at = EXCLUDED.identity_verified_at,
                        active = true,
                        status = 'active',
                        club_id = EXCLUDED.club_id""",
                    (str(pid), club_id, f"Player {pid}"),
                )
