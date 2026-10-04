"""Shared fixtures.

PostgreSQL-backed tests need a real server. Provide one with either:
  * TEST_POSTGRES_URI=postgresql://user@host:port/postgres   (a superuser-capable admin DB), or
  * scripts/test_postgres.sh  (starts a disposable postgres:16 container and prints the URI).
Without a server these tests are skipped with an explicit reason; they are
never marked xfail.
"""
from __future__ import annotations

import os
import uuid

import psycopg
import psycopg.errors
import pytest

class FCError(psycopg.errors.RaiseException):
    def __str__(self):
        s = super().__str__()
        diag = getattr(self, "diag", None)
        code = getattr(self, "sqlstate", None) or getattr(diag, "sqlstate", None)
        return f"{code}: {s}" if code else s


for _code in ("FC001", "FC002", "FC003", "FC004", "FC005", "FC006", "FC007"):
    psycopg.errors._sqlcodes[_code] = type(_code, (FCError,), {"sqlstate": _code})

ADMIN_URI = os.getenv("TEST_POSTGRES_URI")


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "allow_inconsistency: allow temporary inconsistency in fc27_approval_inconsistencies view"
    )


def _with_db(uri: str, dbname: str) -> str:
    info = psycopg.conninfo.conninfo_to_dict(uri)
    info["dbname"] = dbname
    return psycopg.conninfo.make_conninfo(**info)


@pytest.fixture(scope="session")
def pg_admin_uri() -> str:
    if not ADMIN_URI:
        pytest.skip("TEST_POSTGRES_URI not set; real PostgreSQL required (see scripts/test_postgres.sh)")
    try:
        with psycopg.connect(ADMIN_URI, connect_timeout=3):
            pass
    except Exception as exc:  # pragma: no cover - environment specific
        pytest.fail(f"TEST_POSTGRES_URI is set but unreachable: {type(exc).__name__}")
    return ADMIN_URI


from fastapi.testclient import TestClient
from tests.helpers import make_app

@pytest.fixture()
def client(pg_uri):
    app, events = make_app(pg_uri)
    with TestClient(app) as c:
        c.events = events
        yield c
from src.approval_store import ApprovalStore
from src.graph import GraphDeps, GraphRunner
from src.roles import RoleOutput, RoleResult
from tests.helpers import settings

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
from dataclasses import dataclass
from src.db_roles import bootstrap_cluster, bootstrap_database

@dataclass
class DbUris:
    admin: str
    migrator: str
    app: str
    dispatch: str

@pytest.fixture(scope="session")
def pg_roles(pg_admin_uri):
    bootstrap_cluster(pg_admin_uri, {"fc27_migrator": None, "fc27_app": None, "fc27_dispatch": None})

@pytest.fixture()
def pg_db(pg_admin_uri, pg_roles):
    name = f"fc27_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_admin_uri, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    
    uri = _with_db(pg_admin_uri, name)
    bootstrap_database(uri, legacy_owner=None, confirm=True)
    
    migrator = _with_db(uri.replace(uri.split('//')[1].split(':')[0], 'fc27_migrator'), name) if '@' in uri else uri + ' user=fc27_migrator'
    app = _with_db(uri.replace(uri.split('//')[1].split(':')[0], 'fc27_app'), name) if '@' in uri else uri + ' user=fc27_app'
    dispatch = _with_db(uri.replace(uri.split('//')[1].split(':')[0], 'fc27_dispatch'), name) if '@' in uri else uri + ' user=fc27_dispatch'
    
    # Actually wait, parsing URI like that is fragile. psycopg.conninfo.make_conninfo is better.
    
    
    info = psycopg.conninfo.conninfo_to_dict(uri)
    info['user'] = 'fc27_migrator'
    migrator = psycopg.conninfo.make_conninfo(**info)
    info['user'] = 'fc27_app'
    app = psycopg.conninfo.make_conninfo(**info)
    info['user'] = 'fc27_dispatch'
    dispatch = psycopg.conninfo.make_conninfo(**info)
    yield DbUris(uri, migrator, app, dispatch)


    with psycopg.connect(pg_admin_uri, autocommit=True) as conn:
        conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
        conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


from src.db_migrate import migrate

@pytest.fixture()
def pg_db_migrated(pg_db):
    migrate(pg_db.migrator)
    yield pg_db

@pytest.fixture()
def pg_admin_db_uri(pg_db_migrated):
    yield pg_db_migrated.admin

@pytest.fixture()
def pg_migrator_uri(pg_db_migrated):
    yield pg_db_migrated.migrator

@pytest.fixture()
def pg_uri(request, pg_db_migrated):
    yield pg_db_migrated.app
    if "allow_inconsistency" not in request.keywords:
        with psycopg.connect(pg_db_migrated.admin) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('public.fc27_approval_inconsistencies')")
                if cur.fetchone()[0] is not None:
                    cur.execute(
                        "SELECT count(*) FROM public.fc27_approval_inconsistencies WHERE kind <> 'legacy_unlinked'"
                    )
                    count = cur.fetchone()[0]
                    assert count == 0, (
                        f"Found {count} inconsistencies in fc27_approval_inconsistencies after test '{request.node.name}'"
                    )

@pytest.fixture()
def pg_dispatch_uri(pg_db_migrated):
    yield pg_db_migrated.dispatch




@pytest.fixture()
def client_v1(pg_uri):
    """TestClient wired with full /v1/ routes, correlation middleware, and stub pipeline."""
    from tests.helpers import make_app

    app, events = make_app(pg_uri)
    with TestClient(app) as c:
        c.events = events
        yield c


@pytest.fixture()
def api_settings_factory(pg_uri):
    """Factory creating ApiSettings with parameterized overrides."""
    from tests.helpers import settings

    def _create(**overrides):
        return settings(pg_uri, **overrides)

    return _create


@pytest.fixture()
def wired_restart(pg_uri):
    """Factory simulating process/server restart by creating fresh runners on same DB."""
    from src.approval_store import ApprovalStore
    from src.graph import GraphDeps, GraphRunner
    from src.graph_api import create_app
    from tests.conftest import FakeRoles, dashboard
    from tests.helpers import settings

    store = ApprovalStore(pg_uri)
    runners = []

    def make_runner(dash=None, roles=None):
        def mcp(tool, args):
            return dash if dash is not None else dashboard()

        deps = GraphDeps(
            mcp=mcp,
            role_runner=roles or FakeRoles(),
            registry_factory=lambda: None,
            decision_reader=store.load,
        )
        runner = GraphRunner.from_conninfo(pg_uri, deps)
        runners.append(runner)
        app = create_app(settings(pg_uri), runner=runner)
        return runner, app

    yield make_runner

    for r in runners:
        try:
            r.pool.close()
        except Exception:
            pass


# Milestone 3 Dispatch Fixtures

@pytest.fixture()
def sample_player(pg_uri):
    player_id = "10001"
    club_id = "330323"
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO public.players (player_id, club_id, display_name, active)
                   VALUES (%s, %s, 'Sample Player', true)
                   ON CONFLICT (player_id) DO NOTHING""",
                (player_id, club_id),
            )
    return {"player_id": player_id, "club_id": club_id}


def _seed_approved_approval_prereqs(
    cur,
    thread_id: str,
    req_id: uuid.UUID,
    pkg_v: str,
    player_id: str,
    *,
    notes: str = "ok",
    owner: str = "coach",
    selection: str = "best",
    candidate_id: str = "cand-1",
) -> int:
    """Seed paired graph_thread, approval_request, and approval_audit following state machine lifecycle."""
    # Step 1: Insert graph_threads with status = 'running' and approval_request_id = NULL
    cur.execute(
        """INSERT INTO public.graph_threads (
            thread_id, state, status, club_id
        ) VALUES (%s, '{"status":"running"}'::jsonb, 'running', '330323')""",
        (thread_id,),
    )
    # Step 2: Insert approval_requests referencing thread_id
    cur.execute(
        """INSERT INTO public.approval_requests (
            approval_request_id, thread_id, package_version, status,
            created_at, expires_at
        ) VALUES (%s, %s, %s, 'awaiting_approval', now() - interval '10m', now() + interval '10h')""",
        (req_id, thread_id, pkg_v),
    )
    # Step 3: Update graph_threads setting status = 'awaiting_approval', package_version = pkg_v, approval_request_id = req_id
    cur.execute(
        """UPDATE public.graph_threads
              SET status = 'awaiting_approval', package_version = %s, approval_request_id = %s,
                  state = '{"status":"awaiting_approval"}'::jsonb
            WHERE thread_id = %s""",
        (pkg_v, req_id, thread_id),
    )
    # Clear intermediate awaiting_approval pair checks before transitioning to approved
    cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
    cur.execute("SET CONSTRAINTS ALL DEFERRED")

    # Step 4: Update approval_requests setting status = 'approved', decided_at = now()
    cur.execute(
        """UPDATE public.approval_requests
              SET status = 'approved', decided_at = now()
            WHERE approval_request_id = %s""",
        (req_id,),
    )
    # Step 5: Update graph_threads setting status = 'approved', approved_by = 'batmanL8', approved_at = now(), decision_payload = '{"decision":"approved"}'::jsonb
    cur.execute(
        """UPDATE public.graph_threads
              SET status = 'approved', approved_by = 'batmanL8', approved_at = now(),
                  decision_payload = '{"decision":"approved"}'::jsonb,
                  state = '{"status":"approved"}'::jsonb
            WHERE thread_id = %s""",
        (thread_id,),
    )
    # Step 6: Insert approval_audit with thread_id and req_id
    cur.execute(
        """INSERT INTO public.approval_audit (
            thread_id, commissioner, decision, notes, owner, selection,
            candidate_id, player_id, approved_by, approval_request_id, package_version
        ) VALUES (%s, 'batmanL8', 'APPROVE', %s, %s, %s, %s, %s, 'batmanL8', %s, %s)
        RETURNING id""",
        (thread_id, notes, owner, selection, candidate_id, player_id, req_id, pkg_v),
    )
    return cur.fetchone()[0]


@pytest.fixture()
def sample_approved_dispatch_prereqs(pg_uri, sample_player):
    tid = f"thread-dispatch-prereq-{uuid.uuid4().hex[:8]}"
    tid_2 = f"thread-dispatch-prereq-2-{uuid.uuid4().hex[:8]}"
    req_id = uuid.uuid4()
    req_id_2 = uuid.uuid4()
    pkg_v = "a" * 64
    payload_hash = "b" * 64

    with psycopg.connect(pg_uri) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                audit_id = _seed_approved_approval_prereqs(
                    cur,
                    tid,
                    req_id,
                    pkg_v,
                    sample_player["player_id"],
                    notes="ok",
                    owner="coach",
                    selection="best",
                    candidate_id="cand-1",
                )
                audit_id_2 = _seed_approved_approval_prereqs(
                    cur,
                    tid_2,
                    req_id_2,
                    pkg_v,
                    sample_player["player_id"],
                    notes="ok 2",
                    owner="coach",
                    selection="best_runner_up",
                    candidate_id="cand-2",
                )

    return {
        "thread_id": tid,
        "thread_id_2": tid_2,
        "approval_request_id": req_id,
        "approval_request_id_2": req_id_2,
        "approval_id": audit_id,
        "approval_id_2": audit_id_2,
        "hash": payload_hash,
        "player_id": sample_player["player_id"],
    }


@pytest.fixture()
def make_dispatch(pg_uri, pg_admin_db_uri, sample_approved_dispatch_prereqs):
    import json
    prereqs = sample_approved_dispatch_prereqs

    def _make(status="pending", version=1, owner="coach", metric=None, instruction=None):
        d_id = uuid.uuid4()
        corr_id = uuid.uuid4()
        d_tid = f"thread-dispatch-make-{uuid.uuid4().hex[:8]}"
        d_req_id = uuid.uuid4()
        pkg_v = prereqs["hash"]
        m = metric or f"metric_{uuid.uuid4().hex[:6]}"
        inst = instruction or {
            "action": "drill",
            "objective": {
                "metric": m,
                "direction": "increase",
                "target_delta": 10.0,
                "bounds_policy": "fc27-objective-bounds-v1",
            },
        }
        with psycopg.connect(pg_uri) as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    unique_audit_id = _seed_approved_approval_prereqs(
                        cur,
                        d_tid,
                        d_req_id,
                        pkg_v,
                        prereqs["player_id"],
                        notes="ok",
                        owner=owner,
                        selection="best",
                        candidate_id=f"{owner}-cand-{uuid.uuid4().hex[:6]}",
                    )

                    cur.execute(
                        """INSERT INTO public.dispatches (
                            dispatch_id, source_thread_id, approval_id, approval_request_id,
                            approval_payload_hash, package_version, player_id, candidate_id, owner, selection,
                            club_id, approved_instruction, dispatch_status, version, plan_version,
                            created_by_principal, created_correlation_id
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'best', '330323', %s, 'pending', 1, 0, 'batmanL8', %s)""",
                        (
                            d_id,
                            d_tid,
                            unique_audit_id,
                            d_req_id,
                            pkg_v,
                            pkg_v,
                            prereqs["player_id"],
                            f"{owner}-best-1",
                            owner,
                            json.dumps(inst),
                            corr_id,
                        ),
                    )

        if status != "pending" or version != 1:
            with psycopg.connect(pg_admin_db_uri, autocommit=True) as admin_conn:
                admin_conn.execute("SET session_replication_role = 'replica'")
                try:
                    admin_conn.execute(
                        "UPDATE public.dispatches SET dispatch_status = %s, version = %s WHERE dispatch_id = %s",
                        (status, version, d_id),
                    )
                finally:
                    admin_conn.execute("SET session_replication_role = 'origin'")

        return d_id

    return _make


# Milestone 4 Activation Gates & Dispatch Creation Fixtures

TEST_CODE_REVISION = "test-rev-12345"
TEST_DEPLOYMENT_ID = "test-deploy-67890"


@pytest.fixture()
def enabled_client(pg_db_migrated, pg_admin_db_uri):
    """TestClient wired with dispatch_enabled=True and synthetic passed gates for testing."""
    import psycopg
    from fastapi.testclient import TestClient
    from src.graph_api import create_app, reevaluate_gates
    from tests.helpers import (
        StubPipeline,
        fake_packages_dispatchable,
        seed_verified_players,
        settings,
    )

    pg_uri = pg_db_migrated.app

    # 1. Seed verified players
    seed_verified_players(pg_admin_db_uri, ["1001", "1002", "1003"], club_id="330323")

    # 2. Insert synthetic gate records via admin connection
    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            # G-ID manual gate passed
            cur.execute(
                """INSERT INTO public.activation_gate_log (
                    gate_id, status, evidence_ref, code_revision, deployment_id,
                    fc_version, platform, nonce, signature_sha256, recorded_by
                ) VALUES (
                    'G-ID', 'passed', 'test-evidence-g-id', %s, %s,
                    '27', 'common-gen5', 'test-nonce-gid', 'sig-sha-gid', 'commissioner:batmanL8'
                )""",
                (TEST_CODE_REVISION, TEST_DEPLOYMENT_ID),
            )
            # G-ATOMIC manual gate passed
            cur.execute(
                """INSERT INTO public.activation_gate_log (
                    gate_id, status, evidence_ref, code_revision, deployment_id,
                    fc_version, platform, nonce, signature_sha256, recorded_by
                ) VALUES (
                    'G-ATOMIC', 'passed', 'test-evidence-g-atomic', %s, %s,
                    '27', 'common-gen5', 'test-nonce-gatomic', 'sig-sha-gatomic', 'commissioner:batmanL8'
                )""",
                (TEST_CODE_REVISION, TEST_DEPLOYMENT_ID),
            )
            # Synthetic G-OUTBOX: inserted directly as synthetic test setup;
            # does not represent Phase 3 production evidence (C-3).
            cur.execute(
                """INSERT INTO public.activation_gate_log (
                    gate_id, status, evidence_ref, code_revision, deployment_id,
                    fc_version, platform, nonce, signature_sha256, recorded_by
                ) VALUES (
                    'G-OUTBOX', 'passed', 'test-synthetic-g-outbox', %s, %s,
                    '27', 'common-gen5', 'test-nonce-goutbox', 'sig-sha-goutbox', 'commissioner:batmanL8'
                )""",
                (TEST_CODE_REVISION, TEST_DEPLOYMENT_ID),
            )
            # Synthetic fresh worker heartbeat (C-2): no production worker in Phase 1
            cur.execute(
                """INSERT INTO public.service_heartbeats (service_name, last_heartbeat, details)
                   VALUES ('dispatch-worker', now(), '{"test":"synthetic"}'::jsonb)
                   ON CONFLICT (service_name) DO UPDATE SET last_heartbeat = now()"""
            )

    # 3. Create app settings with dispatch_enabled=True and deployment identity
    app_settings = settings(
        pg_uri,
        dispatch_enabled=True,
        code_revision=TEST_CODE_REVISION,
        deployment_id=TEST_DEPLOYMENT_ID,
    )
    events = []
    pipeline = StubPipeline(packages=fake_packages_dispatchable())
    app = create_app(
        app_settings,
        pipeline=pipeline,
        publisher=lambda ch, p: events.append((ch, p)),
    )

    # 4. Directly reevaluate gates on app without restarting graph-api (C-1)
    reevaluate_gates(app)

    with TestClient(app) as client:
        client.events = events
        client.app = app
        client.pipeline = pipeline
        yield client

