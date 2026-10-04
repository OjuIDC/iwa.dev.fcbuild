"""E2E Test Suite Shared Fixtures and Helpers.

Provides database inspection helpers, authentication contexts, deterministic candidate
generators, and test client factories for Tiers 1-4.
"""
from __future__ import annotations

import hashlib
import os
import uuid
from typing import Any, Callable

import psycopg
import pytest
from fastapi.testclient import TestClient

from src import approval
from src.approval_store import ApprovalStore
from src.graph_api import ApiSettings, create_app
from src.identity.services import reset_service_credentials_for_testing
from src.security import Authenticator
from tests.e2e.phase2_helpers import (
    BFF_SERVICE_TOKEN,
    GRAPH_SERVICE_TOKEN,
    WORKER_SERVICE_TOKEN,
)
from tests.helpers import (
    COMMISSIONER_ID,
    COMMISSIONER_TOKEN,
    SERVICE_TOKEN,
    SIGNING_SECRET,
    StubPipeline,
    fake_packages,
    settings,
    signed,
)

# Ensure default service credentials and security configuration in test environment
os.environ.setdefault("BFF_SERVICE_TOKEN", BFF_SERVICE_TOKEN)
os.environ.setdefault("GRAPH_SERVICE_TOKEN", GRAPH_SERVICE_TOKEN)
os.environ.setdefault("WORKER_SERVICE_TOKEN", WORKER_SERVICE_TOKEN)
os.environ.setdefault("APPROVAL_CLI_HMAC_ENABLED", "false")


@pytest.fixture(autouse=True, scope="session")
def configure_test_service_tokens():
    """Ensures service credential loader does not fail on missing environment variables."""
    os.environ.setdefault("BFF_SERVICE_TOKEN", BFF_SERVICE_TOKEN)
    os.environ.setdefault("GRAPH_SERVICE_TOKEN", GRAPH_SERVICE_TOKEN)
    os.environ.setdefault("WORKER_SERVICE_TOKEN", WORKER_SERVICE_TOKEN)
    os.environ.setdefault("APPROVAL_CLI_HMAC_ENABLED", "false")
    reset_service_credentials_for_testing(None)
    yield
    reset_service_credentials_for_testing(None)


E2E_SERVICE_TOKEN = SERVICE_TOKEN
E2E_COMMISSIONER_TOKEN = COMMISSIONER_TOKEN
E2E_COMMISSIONER_ID = COMMISSIONER_ID
E2E_SIGNING_SECRET = SIGNING_SECRET

SVC_HEADERS = {"X-API-Key": E2E_SERVICE_TOKEN}
COM_HEADERS = {"X-API-Key": E2E_COMMISSIONER_TOKEN}


def table_exists(conn: psycopg.Connection, table_name: str, schema: str = "public") -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT 1 FROM information_schema.tables 
               WHERE table_schema = %s AND table_name = %s""",
            (schema, table_name),
        )
        return cur.fetchone() is not None


def column_exists(conn: psycopg.Connection, table_name: str, column_name: str, schema: str = "public") -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT 1 FROM information_schema.columns 
               WHERE table_schema = %s AND table_name = %s AND column_name = %s""",
            (schema, table_name, column_name),
        )
        return cur.fetchone() is not None


def view_exists(conn: psycopg.Connection, view_name: str, schema: str = "public") -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT 1 FROM information_schema.views 
               WHERE table_schema = %s AND table_name = %s""",
            (schema, view_name),
        )
        return cur.fetchone() is not None


def function_exists(conn: psycopg.Connection, func_name: str, schema: str = "public") -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT 1 FROM pg_proc p
               JOIN pg_namespace n ON p.pronamespace = n.oid
               WHERE n.nspname = %s AND p.proname = %s""",
            (schema, func_name),
        )
        return cur.fetchone() is not None


def make_objective_candidate(role: str = "coach", selection: str = "best", player_id: str = "1001",
                            metric: str = "pass_accuracy", delta: float = 10.0) -> dict[str, Any]:
    """Generates an approvable candidate adhering to the bounded objective policy."""
    return {
        "candidate_id": f"{role}-{selection}-{player_id}",
        "player_id": player_id,
        "owner": role,
        "tier": "best",
        "score": 88.5,
        "rationale": "High passing ceiling identified by model",
        "action": "Execute tactical drill cycle",
        "evidence_refs": ["match:20261001:330323"],
        "objective": {
            "metric": metric,
            "direction": "increase",
            "target_delta": delta,
            "min_matches": 1,
            "max_matches": 5,
            "policy_version": "fc27-objective-bounds-v1",
        },
    }


@pytest.fixture()
def e2e_app_factory(pg_uri):
    """Factory creating configured FastAPI applications with captured events."""
    def _create(pipeline=None, ttl: int = 120, runner=None, **extra_settings):
        events = []
        cfg = settings(pg_uri, ttl=ttl, **extra_settings)
        app = create_app(
            cfg,
            pipeline=pipeline or StubPipeline(),
            publisher=lambda ch, payload: events.append((ch, payload)),
            runner=runner,
        )
        return app, events
    return _create


@pytest.fixture()
def e2e_client(e2e_app_factory):
    """Default TestClient for the persistent Graph API with captured events."""
    app, events = e2e_app_factory()
    with TestClient(app) as c:
        c.events = events
        yield c
