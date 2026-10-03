import os
import shutil
import hashlib
import subprocess
import threading
from pathlib import Path
import pytest
import psycopg

from src.db_migrate import migrate, MigrationError, GRANTS_POLICY, verify_postconditions

def test_objects_owned_by_fc27_owner(pg_db_migrated):
    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT relname FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relkind IN ('r', 'v', 'm', 'p', 'S')
                AND c.relowner::regrole::text != 'fc27_owner'
            """)
            assert not cur.fetchall()
            cur.execute("""
                SELECT proname FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname = 'public' AND p.proowner::regrole::text != 'fc27_owner'
            """)
            assert not cur.fetchall()

def test_migrate_refuses_without_membership(pg_db):
    with pytest.raises(MigrationError, match="run python -m src.db_roles bootstrap first"):
        migrate(pg_db.app)

def test_app_cannot_run_ddl(pg_uri):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with psycopg.connect(pg_uri, autocommit=True) as conn:
            conn.execute("CREATE TABLE x()")

def test_checkpoint_setup_requires_owner(pg_uri):
    from langgraph.checkpoint.postgres import PostgresSaver
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with psycopg.connect(pg_uri, autocommit=True) as conn:
            PostgresSaver(conn).setup()

def test_app_can_use_checkpointer(pg_uri):
    from src.graph import GraphDeps, GraphRunner
    from src.approval_store import ApprovalStore
    
    store = ApprovalStore(pg_uri)
    deps = GraphDeps(mcp=lambda t, a: {}, role_runner=lambda r, d, reg: None, registry_factory=lambda: None, decision_reader=store.load)
    
    runner = GraphRunner.from_conninfo(pg_uri, deps, setup=False)
    # the thread inspector read works
    assert list(runner.history("test")) == []

def test_tamper_check_still_applies(pg_db_migrated, tmp_path):
    import src.db_migrate as db_migrate
    mig_dir = tmp_path / "migrations"
    shutil.copytree(db_migrate.MIGRATIONS_DIR, mig_dir)
    p = list(mig_dir.glob("*.sql"))[0]
    p.write_text(p.read_text() + "\n-- tampered")
    with pytest.raises(RuntimeError, match="modified after"):
        migrate(pg_db_migrated.migrator, directory=mig_dir)

def test_concurrent_migrate_is_idempotent(pg_db_migrated):
    results = []
    errors = []
    def run():
        try:
            results.append(migrate(pg_db_migrated.migrator))
        except Exception as e:
            errors.append(e)
            
    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert not errors
    assert all(r == [] for r in results)

def test_migrate_fails_on_bad_postcondition(pg_db_migrated):
    with psycopg.connect(pg_db_migrated.admin, autocommit=True) as conn:
        conn.execute("GRANT CREATE ON SCHEMA public TO fc27_app")
    with pytest.raises(MigrationError, match="create_on_public"):
        migrate(pg_db_migrated.migrator)

def test_db_migrate_cli_exit_code(pg_db_migrated):
    import sys
    with psycopg.connect(pg_db_migrated.admin, autocommit=True) as conn:
        conn.execute("GRANT CREATE ON SCHEMA public TO fc27_app")
    
    env = os.environ.copy()
    env["POSTGRES_URI"] = pg_db_migrated.migrator
    proc = subprocess.run([sys.executable, "-m", "src.db_migrate"], env=env, capture_output=True, text=True)
    assert proc.returncode != 0
    assert "create_on_public" in proc.stderr

def test_graph_api_refuses_owner_member_or_create(pg_db_migrated):
    from src.graph_api import create_app
    from src.settings import ConfigError
    from tests.helpers import settings
    from fastapi.testclient import TestClient
    
    # GRANT CREATE
    with psycopg.connect(pg_db_migrated.admin, autocommit=True) as conn:
        conn.execute("GRANT CREATE ON SCHEMA public TO fc27_app")
    
    app = create_app(settings(pg_db_migrated.app, enforce_db_posture=True))
    with pytest.raises(ConfigError, match="create_on_public"):
        with TestClient(app): pass
        
    with psycopg.connect(pg_db_migrated.admin, autocommit=True) as conn:
        conn.execute("REVOKE CREATE ON SCHEMA public FROM fc27_app")
        conn.execute("GRANT fc27_owner TO fc27_app")

    app = create_app(settings(pg_db_migrated.app, enforce_db_posture=True))
    try:
        with pytest.raises(ConfigError, match="owner_member"):
            with TestClient(app): pass
    finally:
        with psycopg.connect(pg_db_migrated.admin, autocommit=True) as conn:
            conn.execute("REVOKE fc27_owner FROM fc27_app")

def test_graph_api_refuses_privileged_role(pg_admin_db_uri):
    from src.graph_api import create_app
    from src.settings import ConfigError
    from tests.helpers import settings
    from fastapi.testclient import TestClient
    
    app = create_app(settings(pg_admin_db_uri, enforce_db_posture=True))
    with pytest.raises(ConfigError, match="superuser"):
        with TestClient(app): pass

def test_grants_policy_constant_matches_file():
    import src.db_migrate as db_migrate
    grants_path = db_migrate.MIGRATIONS_DIR / "post" / "grants.sql"
    content = grants_path.read_text(encoding="utf-8")
    assert content.startswith(f"-- grants-policy-version: {GRANTS_POLICY.version}")
    assert hashlib.sha256(content.encode()).hexdigest() == GRANTS_POLICY.sha256

def test_modified_grants_without_bump_refused(pg_db, tmp_path):
    import src.db_migrate as db_migrate
    mig_dir = tmp_path / "migrations"
    shutil.copytree(db_migrate.MIGRATIONS_DIR, mig_dir)
    grants_path = mig_dir / "post" / "grants.sql"
    grants_path.write_text(grants_path.read_text() + "\n-- tampered")
    with pytest.raises(MigrationError, match="grants.sql changed without a policy version bump"):
        migrate(pg_db.migrator, directory=mig_dir)

def test_grants_policy_recorded(pg_db_migrated):
    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version, sha256, applied_by FROM public.schema_policy_versions WHERE policy = 'grants'")
            row = cur.fetchone()
            assert row
            assert row[0] == GRANTS_POLICY.version
            assert row[1] == GRANTS_POLICY.sha256
            assert row[2] == 'fc27_migrator'

def test_same_policy_version_different_hash_refused(pg_db_migrated, tmp_path):
    import src.db_migrate as db_migrate
    from dataclasses import replace
    mig_dir = tmp_path / "migrations"
    shutil.copytree(db_migrate.MIGRATIONS_DIR, mig_dir)
    grants_path = mig_dir / "post" / "grants.sql"
    grants_path.write_text(grants_path.read_text() + "\n-- tampered")
    new_sha = hashlib.sha256(grants_path.read_text().encode()).hexdigest()
    
    # monkeypatch GRANTS_POLICY
    old_policy = db_migrate.GRANTS_POLICY
    db_migrate.GRANTS_POLICY = replace(old_policy, sha256=new_sha)
    try:
        with pytest.raises(MigrationError, match="existing row has another sha256"):
            migrate(pg_db_migrated.migrator, directory=mig_dir)
    finally:
        db_migrate.GRANTS_POLICY = old_policy

def test_migrate_errors_redact_password(pg_db):
    try:
        migrate('postgresql://fc27_migrator:s3cret@127.0.0.1:9999/nodb')
        assert False, "Should raise"
    except Exception as e:
        assert 's3cret' not in str(e)
