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
import pytest

ADMIN_URI = os.getenv("TEST_POSTGRES_URI")


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


@pytest.fixture()
def pg_uri(pg_admin_uri):
    """A fresh, fully migrated database per test."""
    from src.db_migrate import migrate

    name = f"fc27_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_admin_uri, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    uri = _with_db(pg_admin_uri, name)
    migrate(uri)
    try:
        yield uri
    finally:
        with psycopg.connect(pg_admin_uri, autocommit=True) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
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

