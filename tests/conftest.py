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
from dataclasses import dataclass

import psycopg
import psycopg.errors
import pytest

from src.db_migrate import migrate
from src.db_roles import bootstrap_cluster, bootstrap_database


class FCError(psycopg.errors.RaiseException):
    def __str__(self):
        s = super().__str__()
        diag = getattr(self, "diag", None)
        code = getattr(self, "sqlstate", None) or getattr(diag, "sqlstate", None)
        return f"{code}: {s}" if code else s


for _code in ("FC001", "FC002", "FC003", "FC004", "FC005", "FC006", "FC007"):
    psycopg.errors._sqlcodes[_code] = type(_code, (FCError,), {"sqlstate": _code})

os.environ.setdefault("PGCONNECT_TIMEOUT", "2")

ADMIN_URI = os.getenv("TEST_POSTGRES_URI")
if not ADMIN_URI:
    try:
        with psycopg.connect("postgresql://fc27test@127.0.0.1:55432/postgres", connect_timeout=1):
            ADMIN_URI = "postgresql://fc27test@127.0.0.1:55432/postgres"
            os.environ.setdefault("TEST_POSTGRES_URI", ADMIN_URI)
    except Exception:
        pass
else:
    os.environ.setdefault("TEST_POSTGRES_URI", ADMIN_URI)


def pytest_configure(config):
    from src.fc27_ci_test_groups import ALL_REGISTERED_MARKERS

    for marker_name, marker_desc in ALL_REGISTERED_MARKERS.items():
        config.addinivalue_line("markers", f"{marker_name}: {marker_desc}")


def pytest_collection_modifyitems(config, items):
    from itertools import groupby
    from pathlib import Path
    from src.fc27_ci_test_groups import (
        POSTGRES_FIXTURE_NAMES,
        classify_test_module,
        normalize_test_module_path,
    )

    repo_root = Path(__file__).resolve().parents[1]
    for item in items:
        fspath = getattr(item, "path", None) or Path(str(item.fspath))
        rel_path = normalize_test_module_path(fspath, root=repo_root)
        fixture_names = set(getattr(item, "fixturenames", ()) or ())
        uses_pg = bool(POSTGRES_FIXTURE_NAMES.intersection(fixture_names))
        info = classify_test_module(rel_path, uses_postgres_fixture=uses_pg, root=repo_root)
        for marker_name in info.markers:
            item.add_marker(getattr(pytest.mark, marker_name))
        if uses_pg and "postgres" not in info.markers:
            item.add_marker(pytest.mark.postgres)

    # Stabilize parametrized test ordering when a test module passes a set/frozenset
    # to @pytest.mark.parametrize, ensuring identical collection order across xdist workers.
    def _func_group_key(it):
        p = getattr(it, "path", None) or Path(str(it.fspath))
        return (str(p), repr(getattr(it, "cls", None)), getattr(it, "originalname", None) or it.name)

    reordered = []
    for _, grp in groupby(items, key=_func_group_key):
        chunk = list(grp)
        if len(chunk) > 1:
            chunk.sort(key=lambda x: x.nodeid)
        reordered.extend(chunk)
    items[:] = reordered


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


@dataclass
class DbUris:
    admin: str
    migrator: str
    app: str
    dispatch: str
    relay: str = ""
    match_results_writer: str = ""
    _pre_migrated: bool = False


def _xdist_worker_tag() -> str:
    raw = os.environ.get("PYTEST_XDIST_WORKER", "main")
    return "".join(ch for ch in raw if ch.isalnum() or ch == "_")[:12] or "main"


@pytest.fixture(scope="session")
def pg_roles(pg_admin_uri):
    bootstrap_cluster(
        pg_admin_uri,
        {
            "fc27_migrator": None,
            "fc27_app": None,
            "fc27_dispatch": None,
            "fc27_relay": None,
            "fc27_match_results_writer": None,
        },
    )


@pytest.fixture(scope="session")
def pg_migrated_template_db(pg_admin_uri, pg_roles) -> str:
    worker = _xdist_worker_tag()
    tmpl_name = f"fc27_tmpl_{worker}_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(pg_admin_uri, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{tmpl_name}"')

    tmpl_uri = _with_db(pg_admin_uri, tmpl_name)
    bootstrap_database(tmpl_uri, legacy_owner=None, confirm=True)
    info = psycopg.conninfo.conninfo_to_dict(tmpl_uri)
    info["user"] = "fc27_migrator"
    tmpl_migrator = psycopg.conninfo.make_conninfo(**info)
    migrate(tmpl_migrator)

    with psycopg.connect(pg_admin_uri, autocommit=True) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
            (tmpl_name,),
        )

    yield tmpl_name

    with psycopg.connect(pg_admin_uri, autocommit=True) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
            (tmpl_name,),
        )
        conn.execute(f'DROP DATABASE IF EXISTS "{tmpl_name}"')


@pytest.fixture()
def pg_db(request, pg_admin_uri, pg_roles):
    worker = _xdist_worker_tag()
    name = f"fc27_test_{worker}_{uuid.uuid4().hex[:12]}"
    use_migrated_template = "pg_db_migrated" in getattr(request, "fixturenames", ())

    if use_migrated_template:
        tmpl_name = request.getfixturevalue("pg_migrated_template_db")
        with psycopg.connect(pg_admin_uri, autocommit=True) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                (tmpl_name,),
            )
            conn.execute(f'CREATE DATABASE "{name}" TEMPLATE "{tmpl_name}"')
            conn.execute(
                f'GRANT CONNECT ON DATABASE "{name}" TO '
                f'fc27_migrator, fc27_app, fc27_dispatch, fc27_relay, fc27_match_results_writer'
            )
        uri = _with_db(pg_admin_uri, name)
        pre_migrated = True
    else:
        with psycopg.connect(pg_admin_uri, autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{name}"')
        uri = _with_db(pg_admin_uri, name)
        bootstrap_database(uri, legacy_owner=None, confirm=True)
        pre_migrated = False

    info = psycopg.conninfo.conninfo_to_dict(uri)
    info["user"] = "fc27_migrator"
    migrator = psycopg.conninfo.make_conninfo(**info)
    info["user"] = "fc27_app"
    app = psycopg.conninfo.make_conninfo(**info)
    info["user"] = "fc27_dispatch"
    dispatch = psycopg.conninfo.make_conninfo(**info)
    info["user"] = "fc27_relay"
    relay = psycopg.conninfo.make_conninfo(**info)
    info["user"] = "fc27_match_results_writer"
    match_results_writer = psycopg.conninfo.make_conninfo(**info)
    yield DbUris(uri, migrator, app, dispatch, relay, match_results_writer, _pre_migrated=pre_migrated)

    with psycopg.connect(pg_admin_uri, autocommit=True) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


@pytest.fixture()
def pg_db_migrated(pg_db):
    if not pg_db._pre_migrated:
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
