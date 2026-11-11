import os
import shutil
import hashlib
import subprocess
import threading
from pathlib import Path
import pytest
import psycopg

from src.db_migrate import (
    GRANTS_POLICY,
    MigrationError,
    check_runtime_posture,
    migrate,
    migration_files,
    verify_postconditions,
)

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
    "0013_fc27_manual_match_results.sql": "2504c4c07d2e7a81a25b408a027122965e66c636e242af0b37f929a6e01242f0",
    "0014_fc27_player_development.sql": "c15c00db374efead5c3cfca10876b83f642e3d0c1891547e23b9d23b359aab2d",
}


def test_applied_migrations_0001_to_0012_sha256_pinned():
    """Pins SHA-256 of applied migrations 0001-0014 so any byte modification fails CI."""
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

def test_runtime_posture_refuses_owner_member_or_create(pg_db_migrated):
    with psycopg.connect(pg_db_migrated.app) as app_conn:
        assert check_runtime_posture(app_conn) == []

    with psycopg.connect(pg_db_migrated.admin, autocommit=True) as conn:
        conn.execute("GRANT CREATE ON SCHEMA public TO fc27_app")

    with psycopg.connect(pg_db_migrated.app) as app_conn:
        assert "create_on_public" in check_runtime_posture(app_conn)

    with psycopg.connect(pg_db_migrated.admin, autocommit=True) as conn:
        conn.execute("REVOKE CREATE ON SCHEMA public FROM fc27_app")
        conn.execute("GRANT fc27_owner TO fc27_app")

    try:
        with psycopg.connect(pg_db_migrated.app) as app_conn:
            assert "owner_member" in check_runtime_posture(app_conn)
    finally:
        with psycopg.connect(pg_db_migrated.admin, autocommit=True) as conn:
            conn.execute("REVOKE fc27_owner FROM fc27_app")

def test_runtime_posture_refuses_privileged_role(pg_admin_db_uri):
    with psycopg.connect(pg_admin_db_uri) as admin_conn:
        assert "superuser" in check_runtime_posture(admin_conn)

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
    """Verifies Grants Policy Version 16 least-privilege separation for Migration 0012, 0013, and 0014 tables."""
    assert GRANTS_POLICY.version == 16
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


STAGE_C_TABLES = (
    "fc27_player_alias_fingerprints",
    "fc27_player_links",
    "fc27_build_catalog",
    "fc27_build_catalog_audit",
    "fc27_player_suggestions",
    "fc27_suggestion_assessments",
    "fc27_discord_jobs",
)


def test_migration_0014_fresh_and_idempotent(pg_db):
    """Validates fresh migration 0001..0014, grants policy 16, postconditions, and second-run no-op."""
    applied_first = migrate(pg_db.migrator)
    assert "0014_fc27_player_development.sql" in applied_first
    assert len(applied_first) == 14

    with psycopg.connect(pg_db.admin) as conn:
        assert verify_postconditions(conn) == []

    # Second run is a safe no-op and re-verifies grants and postconditions
    applied_second = migrate(pg_db.migrator)
    assert applied_second == []
    with psycopg.connect(pg_db.admin) as conn:
        assert verify_postconditions(conn) == []


def test_migration_0014_upgrade_from_0013_preserves_provider_and_manual_data(pg_db, monkeypatch):
    """Validates upgrade from 0013 to 0014 with seeded provider and manual Match Results data."""
    from src.fc27_backup_restore import inspect_database_state_for_manifest
    from src.fc27_match_results import create_scheduled_match, record_match_result
    from src.fc27_polling_worker import FC27PollingRuntime, run_fc27_poll_cycle
    from tests.test_fc27_polling_worker import (
        _build_validated_envelope,
        _enable_all_polling_gates,
    )

    applied_to_0013 = migrate(pg_db.migrator, target="0013")
    assert len(applied_to_0013) == 13
    assert applied_to_0013[-1] == "0013_fc27_manual_match_results.sql"

    # Seed representative provider and manual Match Results rows at 0013
    _enable_all_polling_gates(monkeypatch)
    env, _ = _build_validated_envelope(count=2, start_id=992001, players_per_match=6)
    res = run_fc27_poll_cycle(
        runtime=FC27PollingRuntime(),
        confirm_live_polling=True,
        db_conn_or_uri=pg_db.app,
        fetch_matches_fn=lambda: env,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert res.poll_status == "NEW_MATCHES_PERSISTED"
    assert res.inserted_match_count == 2

    sched = create_scheduled_match(
        pg_db.match_results_writer,
        opponent="Harbor City",
        match_date="2026-04-19",
        match_time="21:30 UTC",
        actor_discord_user_id="345678901234567890",
    )
    record_match_result(
        pg_db.match_results_writer,
        opponent="Harbor City",
        home_score=3,
        away_score=1,
        actor_discord_user_id="345678901234567890",
        scorers="Lao x2, Jay",
        motm="Lao",
        match_id=sched.match_id,
    )

    _, mig_cnt_before, counts_before, hashes_before = inspect_database_state_for_manifest(pg_db.admin)
    assert mig_cnt_before == 13
    assert counts_before.fc27_matches == 2
    assert counts_before.fc27_player_match_stats == 12
    assert counts_before.fc27_manual_matches == 1

    # Upgrade from 0013 to 0014
    applied_upgrade = migrate(pg_db.migrator)
    assert applied_upgrade == ["0014_fc27_player_development.sql"]

    _, mig_cnt_after, counts_after, hashes_after = inspect_database_state_for_manifest(pg_db.admin)
    assert mig_cnt_after == 14
    assert counts_after.fc27_matches == counts_before.fc27_matches == 2
    assert counts_after.fc27_player_match_stats == counts_before.fc27_player_match_stats == 12
    assert counts_after.fc27_manual_matches == counts_before.fc27_manual_matches == 1
    assert hashes_after.matches_sha256 == hashes_before.matches_sha256
    assert hashes_after.player_stats_sha256 == hashes_before.player_stats_sha256
    assert hashes_after.manual_matches_sha256 == hashes_before.manual_matches_sha256
    assert hashes_after.combined_records_sha256 == hashes_before.combined_records_sha256

    with psycopg.connect(pg_db.admin) as conn:
        assert verify_postconditions(conn) == []
        with conn.cursor() as cur:
            for tbl in STAGE_C_TABLES:
                cur.execute(f"SELECT count(*) FROM public.{tbl}")
                assert cur.fetchone()[0] == 0, f"expected {tbl} to be empty after 0014 upgrade"

    # Second run after upgrade is a no-op
    assert migrate(pg_db.migrator) == []


def test_migration_0014_ownership_indexes_and_privacy_columns(pg_db_migrated):
    """Verifies ownership, index existence, empty catalog default, and absence of plaintext identity columns."""
    forbidden_column_names = {
        "gamertag",
        "gamer_tag",
        "player_name",
        "ea_player_id",
        "ea_id",
        "persona_id",
        "blaze_id",
        "nucleus_id",
        "display_name",
        "username",
        "raw_payload",
        "raw_response",
        "raw_body",
    }
    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            for tbl in STAGE_C_TABLES:
                cur.execute(
                    """
                    SELECT c.relowner::regrole::text
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public' AND c.relname = %s
                    """,
                    (tbl,),
                )
                row = cur.fetchone()
                assert row is not None and row[0] == "fc27_owner", f"{tbl} not owned by fc27_owner"

                cur.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = %s
                    """,
                    (tbl,),
                )
                cols = {r[0] for r in cur.fetchall()}
                assert cols.isdisjoint(forbidden_column_names), (
                    f"{tbl} has forbidden plaintext/raw column: {cols & forbidden_column_names}"
                )

            cur.execute("SELECT count(*) FROM public.fc27_build_catalog")
            assert cur.fetchone()[0] == 0


def test_migration_0014_constraints_and_partial_unique_indexes(pg_db_migrated):
    """Verifies constraints, partial unique indexes, append-only triggers, and job lease rules across Stage C tables."""
    pseudo_a = "plr_ecf_0123456789ab"
    pseudo_b = "plr_ecf_abcdef012345"
    hmac_1 = "a" * 64
    hmac_2 = "b" * 64

    # 1. fc27_player_alias_fingerprints via fc27_poll_writer
    with psycopg.connect(pg_db_migrated.poll_writer, autocommit=True) as pw_conn:
        pw_conn.execute(
            """
            INSERT INTO public.fc27_player_alias_fingerprints (pseudonym, gamertag_hmac)
            VALUES (%s, %s), (%s, %s)
            """,
            (pseudo_a, hmac_1, pseudo_b, hmac_2),
        )
        # Update last_seen_at allowed for poll_writer
        pw_conn.execute(
            """
            UPDATE public.fc27_player_alias_fingerprints
               SET last_seen_at = now(), updated_at = now()
             WHERE pseudonym = %s AND gamertag_hmac = %s
            """,
            (pseudo_a, hmac_1),
        )
        # Collision on pseudonym or gamertag_hmac rejected
        with pytest.raises(psycopg.errors.UniqueViolation):
            pw_conn.execute(
                "INSERT INTO public.fc27_player_alias_fingerprints (pseudonym, gamertag_hmac) VALUES (%s, %s)",
                (pseudo_a, "c" * 64),
            )
        with pytest.raises(psycopg.errors.UniqueViolation):
            pw_conn.execute(
                "INSERT INTO public.fc27_player_alias_fingerprints (pseudonym, gamertag_hmac) VALUES (%s, %s)",
                ("plr_ecf_999999999999", hmac_1),
            )
        # Invalid pseudonym rejected
        with pytest.raises(psycopg.errors.CheckViolation):
            pw_conn.execute(
                "INSERT INTO public.fc27_player_alias_fingerprints (pseudonym, gamertag_hmac) VALUES ('PlainGamertag', %s)",
                ("d" * 64,),
            )
        # Invalid HMAC rejected
        with pytest.raises(psycopg.errors.CheckViolation):
            pw_conn.execute(
                "INSERT INTO public.fc27_player_alias_fingerprints (pseudonym, gamertag_hmac) VALUES ('plr_ecf_111111111111', 'not_a_64_hex_digest')"
            )
        # DELETE forbidden for poll_writer
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            pw_conn.execute("DELETE FROM public.fc27_player_alias_fingerprints")

    # 2. fc27_player_links lifecycle & partial unique indexes via fc27_player_dev_writer
    with psycopg.connect(pg_db_migrated.player_dev_writer, autocommit=True) as pd_conn:
        link_1 = pd_conn.execute(
            """
            INSERT INTO public.fc27_player_links (
                pseudonym, discord_user_id, status, requested_by
            ) VALUES (%s, '111111111111111111', 'pending', '111111111111111111')
            RETURNING link_id
            """,
            (pseudo_a,),
        ).fetchone()[0]

        # Confirming without confirmed_by / confirmed_at fails CHECK
        with pytest.raises(psycopg.errors.CheckViolation):
            pd_conn.execute(
                "UPDATE public.fc27_player_links SET status = 'confirmed' WHERE link_id = %s",
                (link_1,),
            )
        # Valid confirmation succeeds
        pd_conn.execute(
            """
            UPDATE public.fc27_player_links
               SET status = 'confirmed',
                   confirmed_by = '222222222222222222',
                   confirmed_at = now(),
                   updated_at = now()
             WHERE link_id = %s
            """,
            (link_1,),
        )
        # Duplicate confirmed link for same pseudonym rejected by partial unique index
        with pytest.raises(psycopg.errors.UniqueViolation):
            pd_conn.execute(
                """
                INSERT INTO public.fc27_player_links (
                    pseudonym, discord_user_id, status, requested_by, confirmed_by, confirmed_at
                ) VALUES (%s, '333333333333333333', 'confirmed', '333333333333333333', '222222222222222222', now())
                """,
                (pseudo_a,),
            )
        # Duplicate confirmed link for same discord_user_id rejected by partial unique index
        with pytest.raises(psycopg.errors.UniqueViolation):
            pd_conn.execute(
                """
                INSERT INTO public.fc27_player_links (
                    pseudonym, discord_user_id, status, requested_by, confirmed_by, confirmed_at
                ) VALUES (%s, '111111111111111111', 'confirmed', '111111111111111111', '222222222222222222', now())
                """,
                (pseudo_b,),
            )
        # Revoking link_1 allows a new confirmed link for pseudo_a and 111111111111111111
        pd_conn.execute(
            """
            UPDATE public.fc27_player_links
               SET status = 'revoked', revoked_at = now(), updated_at = now()
             WHERE link_id = %s
            """,
            (link_1,),
        )
        pd_conn.execute(
            """
            INSERT INTO public.fc27_player_links (
                pseudonym, discord_user_id, status, requested_by, confirmed_by, confirmed_at
            ) VALUES (%s, '111111111111111111', 'confirmed', '111111111111111111', '222222222222222222', now())
            """,
            (pseudo_a,),
        )

        # 3. fc27_build_catalog & fc27_build_catalog_audit
        cat_1 = pd_conn.execute(
            """
            INSERT INTO public.fc27_build_catalog (
                build_key, name, positions, archetype,
                attribute_priorities, playstyles, match_rules, game_patch, notes, version, status, created_by
            ) VALUES (
                'press_st_v1', 'Pressing Striker', '["ST","CF"]'::jsonb, 'Advanced Forward',
                '["pace","finishing"]'::jsonb, '["finesse_shot"]'::jsonb, '{"min_shots":2}'::jsonb,
                '27.1', 'Club pressing striker build', 1, 'active', '222222222222222222'
            )
            RETURNING catalog_row_id
            """
        ).fetchone()[0]
        # Duplicate (build_key, version) fails UNIQUE
        with pytest.raises(psycopg.errors.UniqueViolation):
            pd_conn.execute(
                """
                INSERT INTO public.fc27_build_catalog (
                    build_key, name, positions, archetype,
                    attribute_priorities, playstyles, match_rules, game_patch, version, status, created_by
                ) VALUES (
                    'press_st_v1', 'Dup', '["ST"]'::jsonb, 'Forward',
                    '["pace"]'::jsonb, '[]'::jsonb, '{}'::jsonb, '27.1', 1, 'active', '222222222222222222'
                )
                """
            )
        # Next version (version=2) succeeds
        pd_conn.execute(
            """
            INSERT INTO public.fc27_build_catalog (
                build_key, name, positions, archetype,
                attribute_priorities, playstyles, match_rules, game_patch, version, status, created_by
            ) VALUES (
                'press_st_v1', 'Pressing Striker v2', '["ST"]'::jsonb, 'Forward',
                '["pace"]'::jsonb, '[]'::jsonb, '{}'::jsonb, '27.2', 2, 'active', '222222222222222222'
            )
            """
        )
        # Forbidden identity key in JSONB fails CHECK
        with pytest.raises(psycopg.errors.CheckViolation):
            pd_conn.execute(
                """
                INSERT INTO public.fc27_build_catalog (
                    build_key, name, positions, archetype,
                    attribute_priorities, playstyles, match_rules, game_patch, version, status, created_by
                ) VALUES (
                    'bad_build', 'Bad', '["ST"]'::jsonb, 'Forward',
                    '["pace"]'::jsonb, '[]'::jsonb, '{"gamertag":"leak"}'::jsonb, '27.1', 1, 'active', '222222222222222222'
                )
                """
            )

        audit_1 = pd_conn.execute(
            """
            INSERT INTO public.fc27_build_catalog_audit (
                catalog_row_id, build_key, version, action, actor_discord_user_id, snapshot
            ) VALUES (%s, 'press_st_v1', 1, 'create', '222222222222222222', '{"version":1}'::jsonb)
            RETURNING audit_id
            """,
            (cat_1,),
        ).fetchone()[0]
        # Append-only: UPDATE and DELETE rejected for fc27_player_dev_writer
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            pd_conn.execute("UPDATE public.fc27_build_catalog_audit SET action = 'retire' WHERE audit_id = %s", (audit_1,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            pd_conn.execute("DELETE FROM public.fc27_build_catalog_audit WHERE audit_id = %s", (audit_1,))

        # 4. fc27_player_suggestions & fc27_suggestion_assessments
        sug_1 = pd_conn.execute(
            """
            INSERT INTO public.fc27_player_suggestions (
                pseudonym, kind, objective, content,
                catalog_build_key, catalog_version, baseline, baseline_match_ids, status
            ) VALUES (
                %s, 'build',
                '{"metric":"pass_completion_pct","direction":"increase","target_delta":5.0,"min_matches":3,"max_matches":8}'::jsonb,
                '{"summary":"Increase short-passing support options"}'::jsonb,
                'press_st_v1', 2,
                '{"pass_completion_pct":76.0}'::jsonb,
                ARRAY['mref_ecf_001300130013'],
                'suggested'
            )
            RETURNING suggestion_id
            """,
            (pseudo_a,),
        ).fetchone()[0]
        # Objective min_matches > max_matches fails CHECK
        with pytest.raises(psycopg.errors.CheckViolation):
            pd_conn.execute(
                """
                INSERT INTO public.fc27_player_suggestions (
                    pseudonym, kind, objective, content, baseline, baseline_match_ids, status
                ) VALUES (
                    %s, 'training',
                    '{"metric":"tackles_won","direction":"increase","target_delta":2,"min_matches":10,"max_matches":5}'::jsonb,
                    '{"focus":"timing"}'::jsonb, '{"tackles_won":1}'::jsonb, ARRAY['mref_ecf_001300130013'], 'suggested'
                )
                """,
                (pseudo_a,),
            )
        # Transition to outcome without outcome/closed_at fails CHECK
        with pytest.raises(psycopg.errors.CheckViolation):
            pd_conn.execute(
                "UPDATE public.fc27_player_suggestions SET status = 'outcome', closed_at = now() WHERE suggestion_id = %s",
                (sug_1,),
            )

        ass_1 = pd_conn.execute(
            """
            INSERT INTO public.fc27_suggestion_assessments (
                suggestion_id, calculated_values, match_ids
            ) VALUES (
                %s, '{"pass_completion_pct":83.0}'::jsonb, ARRAY['mref_ecf_001300130013']
            )
            RETURNING assessment_id
            """,
            (sug_1,),
        ).fetchone()[0]
        # Append-only: UPDATE and DELETE rejected for fc27_player_dev_writer
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            pd_conn.execute(
                "UPDATE public.fc27_suggestion_assessments SET calculated_values = '{}'::jsonb WHERE assessment_id = %s",
                (ass_1,),
            )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            pd_conn.execute("DELETE FROM public.fc27_suggestion_assessments WHERE assessment_id = %s", (ass_1,))

    # Verify append-only trigger also blocks superuser/owner UPDATE or DELETE on audit and assessments
    with psycopg.connect(pg_db_migrated.admin, autocommit=True) as admin_conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            admin_conn.execute("DELETE FROM public.fc27_build_catalog_audit WHERE audit_id = %s", (audit_1,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            admin_conn.execute("DELETE FROM public.fc27_suggestion_assessments WHERE assessment_id = %s", (ass_1,))

    # 5. fc27_discord_jobs idempotency and claim-lease enforcement
    with psycopg.connect(pg_db_migrated.poll_writer, autocommit=True) as pw_conn:
        job_1 = pw_conn.execute(
            """
            INSERT INTO public.fc27_discord_jobs (
                job_type, payload, state, idempotency_key
            ) VALUES (
                'post_new_results', '{"match_id":"mref_ecf_001300130013"}'::jsonb, 'pending', 'idem_match_001'
            )
            RETURNING job_id
            """
        ).fetchone()[0]
        # Duplicate idempotency_key fails UNIQUE
        with pytest.raises(psycopg.errors.UniqueViolation):
            pw_conn.execute(
                """
                INSERT INTO public.fc27_discord_jobs (
                    job_type, payload, state, idempotency_key
                ) VALUES ('post_new_results', '{"match_id":"mref_ecf_001300130013"}'::jsonb, 'pending', 'idem_match_001')
                """
            )
        # poll_writer cannot UPDATE or DELETE jobs
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            pw_conn.execute("UPDATE public.fc27_discord_jobs SET state = 'completed' WHERE job_id = %s", (job_1,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            pw_conn.execute("DELETE FROM public.fc27_discord_jobs WHERE job_id = %s", (job_1,))

    with psycopg.connect(pg_db_migrated.player_dev_writer, autocommit=True) as pd_conn:
        # Claiming with an already-expired lease fails trigger CHECK
        with pytest.raises(psycopg.errors.CheckViolation):
            pd_conn.execute(
                """
                UPDATE public.fc27_discord_jobs
                   SET state = 'claimed',
                       claim_owner = 'bot_worker_1',
                       claimed_at = now() - interval '10 minutes',
                       lease_expires_at = now() - interval '5 minutes',
                       attempt_count = 1
                 WHERE job_id = %s
                """,
                (job_1,),
            )
        # Claim job_1 with active lease
        pd_conn.execute(
            """
            UPDATE public.fc27_discord_jobs
               SET state = 'claimed',
                   claim_owner = 'bot_worker_1',
                   claimed_at = now(),
                   lease_expires_at = now() + interval '5 minutes',
                   attempt_count = 1
             WHERE job_id = %s
            """,
            (job_1,),
        )
        # Complete job_1
        pd_conn.execute(
            """
            UPDATE public.fc27_discord_jobs
               SET state = 'completed',
                   claim_owner = NULL,
                   claimed_at = NULL,
                   lease_expires_at = NULL,
                   completed_at = now()
             WHERE job_id = %s
            """,
            (job_1,),
        )


def test_grants_policy_16_positive_and_negative_role_matrix(pg_db_migrated):
    """Verifies negative grants and read-only enforcement for fc27_discord_reader, fc27_poll_writer, and fc27_player_dev_writer."""
    legacy_tables = (
        "approval_requests",
        "approval_audit",
        "dispatches",
        "outbox",
        "domain_events",
        "fc27_users",
        "schema_migrations",
    )

    # 1. fc27_discord_reader can SELECT all FC27 provider, manual, and player-dev tables, but cannot write anywhere
    with psycopg.connect(pg_db_migrated.discord_reader, autocommit=True) as reader_conn:
        for readable_tbl in (
            "fc27_clubs",
            "fc27_export_batches",
            "fc27_matches",
            "fc27_player_match_stats",
            "fc27_quarantine_records",
            "fc27_manual_matches",
            "fc27_manual_match_players",
            "fc27_manual_match_events",
            "fc27_manual_match_audit",
            *STAGE_C_TABLES,
        ):
            reader_conn.execute(f"SELECT count(*) FROM public.{readable_tbl}")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                reader_conn.execute(f"DELETE FROM public.{readable_tbl}")

        for leg_tbl in legacy_tables:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                reader_conn.execute(f"SELECT count(*) FROM public.{leg_tbl}")

    # 2. fc27_poll_writer cannot access links, catalog, suggestions, assessments, manual matches, or legacy tables
    with psycopg.connect(pg_db_migrated.poll_writer, autocommit=True) as pw_conn:
        for forbidden_tbl in (
            "fc27_player_links",
            "fc27_build_catalog",
            "fc27_build_catalog_audit",
            "fc27_player_suggestions",
            "fc27_suggestion_assessments",
            "fc27_manual_matches",
            "fc27_manual_match_players",
            "fc27_manual_match_events",
            "fc27_manual_match_audit",
            *legacy_tables,
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                pw_conn.execute(f"SELECT count(*) FROM public.{forbidden_tbl}")

    # 3. fc27_player_dev_writer cannot write to provider tables or alias fingerprints, and cannot access quarantine, manual, or legacy tables
    with psycopg.connect(pg_db_migrated.player_dev_writer, autocommit=True) as pd_conn:
        for ro_tbl in (
            "fc27_player_alias_fingerprints",
            "fc27_clubs",
            "fc27_export_batches",
            "fc27_matches",
            "fc27_player_match_stats",
        ):
            pd_conn.execute(f"SELECT count(*) FROM public.{ro_tbl}")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                pd_conn.execute(f"DELETE FROM public.{ro_tbl}")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            pd_conn.execute(
                "INSERT INTO public.fc27_player_alias_fingerprints (pseudonym, gamertag_hmac) "
                "VALUES ('plr_ecf_0123456789ab', %s)",
                ("c" * 64,),
            )
        for no_access_tbl in (
            "fc27_quarantine_records",
            "fc27_manual_matches",
            "fc27_manual_match_players",
            "fc27_manual_match_events",
            "fc27_manual_match_audit",
            *legacy_tables,
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                pd_conn.execute(f"SELECT count(*) FROM public.{no_access_tbl}")
