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
