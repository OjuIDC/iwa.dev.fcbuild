from __future__ import annotations

import os
import subprocess
import psycopg
import pytest

from src.db_roles import (
    ROLE_OWNER,
    ROLE_MIGRATOR,
    ROLE_APP,
    ROLE_DISPATCH,
    ROLE_MATCH_RESULTS_WRITER,
    ROLE_POLL_WRITER,
    ROLE_PLAYER_DEV_WRITER,
    ROLE_DISCORD_READER,
    GRANTS_POLICY_VERSION,
    redact_conninfo,
    check_runtime_posture,
    bootstrap_cluster,
    bootstrap_database,
)


def test_bootstrap_is_idempotent(pg_admin_uri):
    assert GRANTS_POLICY_VERSION == 16
    passwords = {
        ROLE_MIGRATOR: None,
        ROLE_APP: None,
        ROLE_DISPATCH: None,
        ROLE_MATCH_RESULTS_WRITER: None,
        ROLE_POLL_WRITER: None,
        ROLE_PLAYER_DEV_WRITER: None,
        ROLE_DISCORD_READER: None,
    }
    bootstrap_cluster(pg_admin_uri, passwords)
    bootstrap_cluster(pg_admin_uri, passwords)


def test_role_attributes(pg_db):
    runtime_roles = (
        ROLE_APP,
        ROLE_DISPATCH,
        ROLE_MATCH_RESULTS_WRITER,
        ROLE_POLL_WRITER,
        ROLE_PLAYER_DEV_WRITER,
        ROLE_DISCORD_READER,
    )
    with psycopg.connect(pg_db.admin) as conn:
        with conn.cursor() as cur:
            # owner: rolcanlogin false
            cur.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname = %s", (ROLE_OWNER,))
            assert not cur.fetchone()[0]

            # runtime login roles: can login, not superuser/createdb/createrole/replication/bypassrls, not member of fc27_owner
            for role in runtime_roles:
                cur.execute(
                    "SELECT rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls "
                    "FROM pg_roles WHERE rolname = %s",
                    (role,),
                )
                row = cur.fetchone()
                assert row is not None, f"missing role {role}"
                assert row[0] is True
                assert not any(row[1:])
                cur.execute("SELECT pg_has_role(%s, %s, 'MEMBER')", (role, ROLE_OWNER))
                assert not cur.fetchone()[0]

            # migrator: pg_has_role('fc27_migrator','fc27_owner','MEMBER') is true
            cur.execute("SELECT pg_has_role(%s, %s, 'MEMBER')", (ROLE_MIGRATOR, ROLE_OWNER))
            assert cur.fetchone()[0]


def test_public_cannot_create_in_public_schema(pg_db):
    with psycopg.connect(pg_db.admin) as conn:
        with conn.cursor() as cur:
            for role in (
                ROLE_APP,
                ROLE_DISPATCH,
                ROLE_MATCH_RESULTS_WRITER,
                ROLE_POLL_WRITER,
                ROLE_PLAYER_DEV_WRITER,
                ROLE_DISCORD_READER,
            ):
                cur.execute("SELECT has_schema_privilege(%s, 'public', 'CREATE')", (role,))
                assert not cur.fetchone()[0]
                cur.execute("SELECT has_schema_privilege(%s, 'public', 'USAGE')", (role,))
                assert cur.fetchone()[0]

def test_bootstrap_transfers_legacy_objects_owned_by_admin(pg_db):
    with psycopg.connect(pg_db.admin, autocommit=True) as conn:
        conn.execute("CREATE TABLE legacy_t (id int)")
        conn.execute("CREATE FUNCTION legacy_f() RETURNS void AS $$ BEGIN END; $$ LANGUAGE plpgsql")

        # bootstrap_database(confirm=False)
        report = bootstrap_database(pg_db.admin, legacy_owner=None, confirm=False)
        assert set(report.pending_transfers) == {'function public.legacy_f()', 'table public.legacy_t'}
        assert not report.transferred

        # after confirm=True: pg_class/pg_proc owner of both is fc27_owner
        report = bootstrap_database(pg_db.admin, legacy_owner=None, confirm=True)
        assert set(report.transferred) == {'function public.legacy_f()', 'table public.legacy_t'}
        assert not report.pending_transfers

        # verify owners
        assert conn.execute("SELECT relowner::regrole::text FROM pg_class WHERE relname = 'legacy_t'").fetchone()[0] == ROLE_OWNER
        assert conn.execute("SELECT proowner::regrole::text FROM pg_proc WHERE proname = 'legacy_f'").fetchone()[0] == ROLE_OWNER

def test_check_runtime_posture(pg_db):
    with psycopg.connect(pg_db.admin) as conn:
        assert check_runtime_posture(conn) == ["superuser"]

    with psycopg.connect(pg_db.app) as conn:
        assert check_runtime_posture(conn) == []
    with psycopg.connect(pg_db.poll_writer) as conn:
        assert check_runtime_posture(conn) == []
    with psycopg.connect(pg_db.player_dev_writer) as conn:
        assert check_runtime_posture(conn) == []
    with psycopg.connect(pg_db.discord_reader) as conn:
        assert check_runtime_posture(conn) == []

    with psycopg.connect(pg_db.admin, autocommit=True) as conn:
        conn.execute(f"GRANT {ROLE_OWNER} TO {ROLE_APP}")
    with psycopg.connect(pg_db.app) as conn:
        assert check_runtime_posture(conn) == ["owner_member"]

    with psycopg.connect(pg_db.admin, autocommit=True) as conn:
        conn.execute(f"REVOKE {ROLE_OWNER} FROM {ROLE_APP}")
        conn.execute(f"GRANT CREATE ON SCHEMA public TO {ROLE_APP}")
    with psycopg.connect(pg_db.app) as conn:
        assert check_runtime_posture(conn) == ["create_on_public"]

def test_redact_conninfo():
    assert redact_conninfo('postgresql://u:s3cret@h/db') == 'postgresql://u:***@h/db'
    assert redact_conninfo('host=h user=u password=s3cret') == 'host=h user=u password=***'
    assert redact_conninfo('postgresql://u@h/db') == 'postgresql://u@h/db'
    assert redact_conninfo('host=h user=u') == 'host=h user=u'
    assert 's3cret' not in redact_conninfo('postgresql://u:s3cret@h/db')
    assert 's3cret' not in redact_conninfo('host=h user=u password=s3cret')

def test_bootstrap_cli_never_prints_password(pg_db, capsys):
    # C-7: run with *_PASSWORD_FILE secrets and a failing admin URI;
    # stdout/stderr contain neither the password nor the raw URI
    env = os.environ.copy()
    env["POSTGRES_ADMIN_URI_FILE"] = "tests/test_missing_uri"
    env["FC27_MIGRATOR_PASSWORD_FILE"] = "tests/test_s3cret_migrator"
    env["FC27_APP_PASSWORD_FILE"] = "tests/test_s3cret_app"
    env["FC27_DISPATCH_PASSWORD_FILE"] = "tests/test_s3cret_dispatch"

    with open("tests/test_missing_uri", "w") as f:
        f.write("postgresql://admin:adminpw@127.0.0.1:9999/nodb")
    with open("tests/test_s3cret_migrator", "w") as f:
        f.write("s3cret_migrator")
    with open("tests/test_s3cret_app", "w") as f:
        f.write("s3cret_app")
    with open("tests/test_s3cret_dispatch", "w") as f:
        f.write("s3cret_dispatch")

    try:
        proc = subprocess.run(["python", "-m", "src.db_roles", "bootstrap", "--database", "fc27"], env=env, capture_output=True, text=True)
        assert proc.returncode != 0
        assert "s3cret_migrator" not in proc.stdout and "s3cret_migrator" not in proc.stderr
        assert "s3cret_app" not in proc.stdout and "s3cret_app" not in proc.stderr
        assert "s3cret_dispatch" not in proc.stdout and "s3cret_dispatch" not in proc.stderr
        assert "adminpw" not in proc.stdout and "adminpw" not in proc.stderr
    finally:
        os.remove("tests/test_missing_uri")
        os.remove("tests/test_s3cret_migrator")
        os.remove("tests/test_s3cret_app")
        os.remove("tests/test_s3cret_dispatch")


def test_poll_writer_c1_fingerprints_and_discord_jobs_privileges(pg_db_migrated):
    """Verify fc27_poll_writer least-privilege matrix on C1 fingerprint and Discord job tables."""
    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            # Allowed on fc27_player_alias_fingerprints: SELECT, INSERT, UPDATE(last_seen_at, updated_at)
            cur.execute(
                "SELECT has_table_privilege(%s, 'public.fc27_player_alias_fingerprints', 'SELECT'), "
                "has_table_privilege(%s, 'public.fc27_player_alias_fingerprints', 'INSERT'), "
                "has_table_privilege(%s, 'public.fc27_player_alias_fingerprints', 'DELETE'), "
                "has_column_privilege(%s, 'public.fc27_player_alias_fingerprints', 'last_seen_at', 'UPDATE'), "
                "has_column_privilege(%s, 'public.fc27_player_alias_fingerprints', 'updated_at', 'UPDATE'), "
                "has_column_privilege(%s, 'public.fc27_player_alias_fingerprints', 'gamertag_hmac', 'UPDATE')",
                (
                    ROLE_POLL_WRITER,
                    ROLE_POLL_WRITER,
                    ROLE_POLL_WRITER,
                    ROLE_POLL_WRITER,
                    ROLE_POLL_WRITER,
                    ROLE_POLL_WRITER,
                ),
            )
            assert cur.fetchone() == (True, True, False, True, True, False)

            # Allowed on fc27_discord_jobs: SELECT, INSERT only (no UPDATE, no DELETE)
            cur.execute(
                "SELECT has_table_privilege(%s, 'public.fc27_discord_jobs', 'SELECT'), "
                "has_table_privilege(%s, 'public.fc27_discord_jobs', 'INSERT'), "
                "has_table_privilege(%s, 'public.fc27_discord_jobs', 'UPDATE'), "
                "has_table_privilege(%s, 'public.fc27_discord_jobs', 'DELETE')",
                (
                    ROLE_POLL_WRITER,
                    ROLE_POLL_WRITER,
                    ROLE_POLL_WRITER,
                    ROLE_POLL_WRITER,
                ),
            )
            assert cur.fetchone() == (True, True, False, False)
