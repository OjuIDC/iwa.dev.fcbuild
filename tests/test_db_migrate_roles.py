import os
import shutil
import hashlib
import subprocess
import threading
from pathlib import Path
import pytest
import psycopg

from src.db_migrate import migrate, migration_files, MigrationError, GRANTS_POLICY, verify_postconditions

PINNED_APPLIED_MIGRATION_SHA256 = {
    "0001_initial.sql": "692e05d98453eb8a4c87016ad6b13cdb805bb68476b00fd8cb9df60811e88094",
    "0002_approval_security.sql": "554573926f59d5723435e476084e3ca3b437c92e7ca77a260145dafb918ad606",
    "0003_dispatch_foundation.sql": "089f5e05c12b50d323bdd085c22d656917f089a8d8fce21e0ae131fa6fccd40f",
    "0004_identity_foundation.sql": "e64075f2d6faf390b5d5046c72d07705a4f2f07b1aa9300987f8010b19256f2c",
    "0005_delegations.sql": "e5f73d7b272e4b7323b988bcb5595df32cac7c96cbaeb0615b10176be83af7c8",
    "0006_bootstrap_and_recovery.sql": "80bf2fa6570573967e77cc95aaad600d19a56543fb52de64430366f85438e252",
    "0007_decision_evidence.sql": "4641b28ffa48ea1c8c379b9a5004864b0e3558cc13597615ab50053a2063293b",
    "0008_r2_remediation.sql": "4a0bacda155fe8c6724d87df2aa2c6f95480d7c5228bb8e62e6faddc1e23e723",
    "0009_r3_evidence_binding.sql": "a173ca5679affa9fcfd9a8701bcc690e13e84cd6c99b51c8ffcd8255329f5d73",
    "0010_outbox_claim_ownership.sql": "9dc88b9f8f6c6c29f5473a2cb8d01bdbd3fe89d7128a4b2f84797bc0ef93c895",
    "0011_dispatch_worker_claims.sql": "50163beab93318e64bfd6be7645f7c096b13eaee40436e993f6299ab0ad1407e",
    "0012_fc27_performance_and_quarantine.sql": "84279fa7bddafd8bd056adb69dc5cfbf2b178475d4dbc5442fd7993df8ec1807",
}


def test_applied_migrations_0001_to_0012_sha256_pinned():
    """Pins SHA-256 of applied migrations 0001-0012 so any byte modification fails CI."""
    files_by_name = {path.name: path for _, path in migration_files()}
    for name, expected_sha256 in PINNED_APPLIED_MIGRATION_SHA256.items():
        assert name in files_by_name, f"missing applied migration {name}"
        path = files_by_name[name]
        text_digest = hashlib.sha256(path.read_text(encoding="utf-8").encode()).hexdigest()
        bytes_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert text_digest == expected_sha256, f"{name} text SHA-256 drifted from pinned constant"
        assert bytes_digest == expected_sha256, f"{name} raw bytes SHA-256 drifted from pinned constant"


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


def test_identity_store_zero_runtime_ddl():
    """Asserts that src/identity/store.py contains zero runtime DDL statements (R10)."""
    store_path = Path(__file__).resolve().parents[1] / "src" / "identity" / "store.py"
    code = store_path.read_text(encoding="utf-8")
    assert "CREATE TABLE" not in code, "store.py must not contain runtime CREATE TABLE statements"
    assert "DROP TABLE" not in code, "store.py must not contain runtime DROP TABLE statements"
    assert "ALTER TABLE" not in code, "store.py must not contain runtime ALTER TABLE statements"
    assert "_ensure_bootstrap_lock_table" not in code
    assert "_ensure_decision_evidence_table" not in code
    assert "_ensure_player_tables" not in code


def test_fc27_decision_evidence_least_privilege(pg_uri):
    """Verifies that runtime role fc27_app cannot execute DDL or DELETE on fc27_decision_evidence (R10)."""
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DROP TABLE public.fc27_decision_evidence")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("ALTER TABLE public.fc27_decision_evidence ADD COLUMN test_col text")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("TRUNCATE TABLE public.fc27_decision_evidence")
        with pytest.raises((psycopg.errors.InsufficientPrivilege, psycopg.errors.CheckViolation)):
            conn.execute("DELETE FROM public.fc27_decision_evidence")


def test_fc27_decision_evidence_immutability(pg_uri):
    """Verifies single-use consumption and immutability trigger on fc27_decision_evidence (R10)."""
    import secrets
    from uuid import uuid4
    with psycopg.connect(pg_uri) as conn:
        user_id = str(uuid4())
        session_id = str(uuid4())
        thread_id = f"thread_{secrets.token_hex(4)}"
        req_id = str(uuid4())
        pkg_hash = "a" * 64
        bound_hash = "b" * 64

        conn.execute("INSERT INTO public.fc27_users (user_id, club_id, login_id, display_name) VALUES (%s, '330323', %s, 'Tester')", (user_id, f"test_{secrets.token_hex(4)}@fc27.club"))
        conn.execute("INSERT INTO public.fc27_sessions (session_id, token_hash, user_id, auth_level, auth_method, idle_expires_at, absolute_expires_at) VALUES (%s, %s, %s, 'authenticated', 'passkey', now() + interval '1 hour', now() + interval '8 hours')", (session_id, secrets.token_hex(32), user_id))
        conn.execute("INSERT INTO public.graph_threads (thread_id, state, status, package_version) VALUES (%s, '{}'::jsonb, 'running', %s)", (thread_id, pkg_hash))
        conn.execute("INSERT INTO public.approval_requests (approval_request_id, thread_id, package_version, status, expires_at) VALUES (%s, %s, %s, 'awaiting_approval', now() + interval '1 hour')", (req_id, thread_id, pkg_hash))
        conn.execute("UPDATE public.graph_threads SET status = 'awaiting_approval', approval_request_id = %s WHERE thread_id = %s", (req_id, thread_id))

        ev_id = str(uuid4())
        conn.execute("""
            INSERT INTO public.fc27_decision_evidence (
                evidence_id, user_id, session_id, thread_id, approval_request_id,
                package_id, package_version, package_hash, normalized_decision,
                canonical_bound_payload_hash, issued_at, expires_at
            ) VALUES (%s, %s, %s, %s, %s, 'pkg_1', '1', %s, 'approved', %s, now(), now() + interval '5 minutes')
        """, (ev_id, user_id, session_id, thread_id, req_id, pkg_hash, bound_hash))
        conn.commit()
        conn.autocommit = True

        # Modifying immutable attribute fails
        with pytest.raises(psycopg.errors.CheckViolation, match="decision evidence attributes are immutable"):
            conn.execute("UPDATE public.fc27_decision_evidence SET package_id = 'tampered', consumed_at = now() WHERE evidence_id = %s", (ev_id,))

        # First consumption succeeds
        conn.execute("UPDATE public.fc27_decision_evidence SET consumed_at = now() WHERE evidence_id = %s", (ev_id,))

        # Re-consumption fails
        with pytest.raises(psycopg.errors.CheckViolation, match="decision evidence is already consumed"):
            conn.execute("UPDATE public.fc27_decision_evidence SET consumed_at = now() WHERE evidence_id = %s", (ev_id,))

        # Un-consuming fails
        with pytest.raises(psycopg.errors.CheckViolation, match="decision evidence is already consumed"):
            conn.execute("UPDATE public.fc27_decision_evidence SET consumed_at = NULL WHERE evidence_id = %s", (ev_id,))


def test_fc27_performance_storage_least_privilege(pg_db_migrated):
    """Verifies Grants Policy Version 15 least-privilege separation for Migration 0012 and 0013 tables."""
    assert GRANTS_POLICY.version == 15
    with psycopg.connect(pg_db_migrated.app, autocommit=True) as app_conn:
        for tbl in (
            "fc27_clubs",
            "fc27_export_batches",
            "fc27_matches",
            "fc27_player_match_stats",
            "fc27_quarantine_records",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                app_conn.execute(f"DELETE FROM public.{tbl}")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                app_conn.execute(f"TRUNCATE TABLE public.{tbl}")

        for manual_tbl in (
            "fc27_manual_matches",
            "fc27_manual_match_players",
            "fc27_manual_match_events",
            "fc27_manual_match_audit",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                app_conn.execute(f"DELETE FROM public.{manual_tbl}")

    with psycopg.connect(pg_db_migrated.match_results_writer, autocommit=True) as writer_conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            writer_conn.execute("SELECT count(*) FROM public.fc27_matches")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            writer_conn.execute("DELETE FROM public.fc27_manual_matches")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            writer_conn.execute("DELETE FROM public.fc27_manual_match_audit")

    with psycopg.connect(pg_db_migrated.dispatch, autocommit=True) as disp_conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            disp_conn.execute("SELECT count(*) FROM public.fc27_matches")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            disp_conn.execute("SELECT count(*) FROM public.fc27_manual_matches")
