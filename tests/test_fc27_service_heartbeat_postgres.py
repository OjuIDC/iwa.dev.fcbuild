"""Real-PostgreSQL tests for Migration 0026 durable service heartbeats.

Uses a disposable PostgreSQL 16 instance via the standard ``pg_db_migrated``
fixture. Verifies the procedure contract, database clock, upsert semantics,
idempotency, and narrow runtime grants.
"""

from __future__ import annotations

import psycopg
import pytest

from src.fc27_service_heartbeat import (
    HeartbeatConfig,
    ServiceHeartbeat,
    read_uri_file,
)

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.domain,
    pytest.mark.security,
    pytest.mark.offline,
]

KNOWN_KEY = "fc27-community-worker"
BOT_KEY = "fc27-discord-bot"
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def _record(uri: str, key: str, digest: str, status: str) -> dict:
    with psycopg.connect(uri, autocommit=True) as conn:
        row = conn.execute(
            "SELECT public.fc27_record_service_heartbeat(%s, %s, %s)",
            (key, digest, status),
        ).fetchone()
        return row[0]


def _row(admin: str, key: str):
    with psycopg.connect(admin) as conn:
        return conn.execute(
            "SELECT service_key, instance_digest, runtime_status, started_at, "
            "last_seen_at, update_count FROM public.fc27_service_heartbeats "
            "WHERE service_key = %s",
            (key,),
        ).fetchone()


def test_30_migration_and_policy_recorded(pg_db_migrated) -> None:
    with psycopg.connect(pg_db_migrated.admin) as conn:
        versions = {
            r[0]
            for r in conn.execute("SELECT version FROM public.schema_migrations").fetchall()
        }
        assert any(v.startswith("0026") for v in versions)
        policy = conn.execute(
            "SELECT version FROM public.schema_policy_versions "
            "WHERE policy = 'grants' ORDER BY version DESC LIMIT 1"
        ).fetchone()
        assert policy[0] == 29


def test_31_migration_rerun_idempotent(pg_db_migrated) -> None:
    from src.db_migrate import migrate

    applied = migrate(pg_db_migrated.migrator)
    assert applied == []


def test_32_first_heartbeat_inserts_one_row(pg_db_migrated) -> None:
    result = _record(pg_db_migrated.poll_writer, KNOWN_KEY, DIGEST_A, "STARTING")
    assert result["instance_changed"] is True
    assert result["update_count"] == 1
    row = _row(pg_db_migrated.admin, KNOWN_KEY)
    assert row is not None
    assert row[1] == DIGEST_A
    assert row[2] == "STARTING"
    with psycopg.connect(pg_db_migrated.admin) as conn:
        count = conn.execute(
            "SELECT count(*) FROM public.fc27_service_heartbeats WHERE service_key = %s",
            (KNOWN_KEY,),
        ).fetchone()[0]
    assert count == 1


def test_33_repeated_same_instance_updates_row(pg_db_migrated) -> None:
    _record(pg_db_migrated.poll_writer, KNOWN_KEY, DIGEST_A, "STARTING")
    first = _row(pg_db_migrated.admin, KNOWN_KEY)
    r2 = _record(pg_db_migrated.poll_writer, KNOWN_KEY, DIGEST_A, "READY")
    second = _row(pg_db_migrated.admin, KNOWN_KEY)
    assert r2["instance_changed"] is False
    assert r2["update_count"] == 2
    assert second[3] == first[3]  # started_at retained
    assert second[2] == "READY"
    with psycopg.connect(pg_db_migrated.admin) as conn:
        count = conn.execute(
            "SELECT count(*) FROM public.fc27_service_heartbeats WHERE service_key = %s",
            (KNOWN_KEY,),
        ).fetchone()[0]
    assert count == 1


def test_34_new_instance_resets_started_at_and_count(pg_db_migrated) -> None:
    _record(pg_db_migrated.poll_writer, KNOWN_KEY, DIGEST_A, "READY")
    _record(pg_db_migrated.poll_writer, KNOWN_KEY, DIGEST_A, "READY")
    first = _row(pg_db_migrated.admin, KNOWN_KEY)
    r = _record(pg_db_migrated.poll_writer, KNOWN_KEY, DIGEST_B, "STARTING")
    second = _row(pg_db_migrated.admin, KNOWN_KEY)
    assert r["instance_changed"] is True
    assert r["update_count"] == 1
    assert second[1] == DIGEST_B
    assert second[3] >= first[3]
    assert second[5] == 1


def test_35_database_clock_authoritative(pg_db_migrated) -> None:
    with psycopg.connect(pg_db_migrated.admin) as conn:
        before = conn.execute("SELECT clock_timestamp()").fetchone()[0]
    _record(pg_db_migrated.poll_writer, KNOWN_KEY, DIGEST_A, "READY")
    with psycopg.connect(pg_db_migrated.admin) as conn:
        after = conn.execute("SELECT clock_timestamp()").fetchone()[0]
    row = _row(pg_db_migrated.admin, KNOWN_KEY)
    assert before <= row[4] <= after


@pytest.mark.parametrize(
    "key,digest,status",
    [
        ("fc27-bogus", DIGEST_A, "READY"),
        (KNOWN_KEY, "nothex", "READY"),
        (KNOWN_KEY, "A" * 64, "READY"),
        (KNOWN_KEY, DIGEST_A, "EXPLODE"),
    ],
)
def test_36_invalid_inputs_rejected(pg_db_migrated, key, digest, status) -> None:
    with psycopg.connect(pg_db_migrated.poll_writer, autocommit=True) as conn:
        with pytest.raises(psycopg.Error):
            conn.execute(
                "SELECT public.fc27_record_service_heartbeat(%s, %s, %s)",
                (key, digest, status),
            )


def test_37_status_helper_reports_database_clock_ages(pg_db_migrated) -> None:
    _record(pg_db_migrated.poll_writer, KNOWN_KEY, DIGEST_A, "READY")
    with psycopg.connect(pg_db_migrated.discord_reader) as conn:
        info = conn.execute(
            "SELECT public.fc27_get_service_heartbeat_status()"
        ).fetchone()[0]
    assert info["worker"]["known"] is True
    assert info["worker"]["runtime_status"] == "READY"
    assert info["worker"]["heartbeat_age_seconds"] >= 0
    assert info["bot"]["known"] is False


def test_38_restored_stale_heartbeat_classifies_stale(pg_db_migrated) -> None:
    from src.fc27_operations_status import classify_heartbeat_state

    _record(pg_db_migrated.poll_writer, KNOWN_KEY, DIGEST_A, "READY")
    with psycopg.connect(pg_db_migrated.admin, autocommit=True) as conn:
        conn.execute(
            "UPDATE public.fc27_service_heartbeats SET "
            "started_at = clock_timestamp() - interval '500 seconds', "
            "last_seen_at = clock_timestamp() - interval '400 seconds' "
            "WHERE service_key = %s",
            (KNOWN_KEY,),
        )
    with psycopg.connect(pg_db_migrated.discord_reader) as conn:
        info = conn.execute(
            "SELECT public.fc27_get_service_heartbeat_status()"
        ).fetchone()[0]
    age = info["worker"]["heartbeat_age_seconds"]
    assert age >= 390
    assert classify_heartbeat_state(age, "READY", True) == "CRITICAL"


def test_39_runtime_grants_are_narrow(pg_db_migrated) -> None:
    with psycopg.connect(pg_db_migrated.admin) as conn:
        for role in ("fc27_poll_writer", "fc27_match_results_writer"):
            for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                granted = conn.execute(
                    "SELECT has_table_privilege(%s, 'public.fc27_service_heartbeats', %s)",
                    (role, priv),
                ).fetchone()[0]
                assert granted is False, f"{role} has direct {priv}"
            assert conn.execute(
                "SELECT has_function_privilege(%s, "
                "'public.fc27_record_service_heartbeat(text,text,text)', 'EXECUTE')",
                (role,),
            ).fetchone()[0] is True
        for role in ("fc27_discord_reader", "fc27_app"):
            assert conn.execute(
                "SELECT has_function_privilege(%s, "
                "'public.fc27_get_service_heartbeat_status()', 'EXECUTE')",
                (role,),
            ).fetchone()[0] is True
            assert conn.execute(
                "SELECT has_table_privilege(%s, 'public.fc27_service_heartbeats', 'SELECT')",
                (role,),
            ).fetchone()[0] is False
        assert conn.execute(
            "SELECT has_function_privilege('public', "
            "'public.fc27_record_service_heartbeat(text,text,text)', 'EXECUTE')"
        ).fetchone()[0] is False


def test_40_writer_role_cannot_select_heartbeat_table(pg_db_migrated) -> None:
    with psycopg.connect(pg_db_migrated.poll_writer) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT count(*) FROM public.fc27_service_heartbeats").fetchone()


def test_41_writer_module_records_through_procedure(pg_db_migrated, tmp_path) -> None:
    uri_file = tmp_path / "poll_writer_uri"
    uri_file.write_text(pg_db_migrated.poll_writer, encoding="utf-8")
    uri = read_uri_file(uri_file)
    assert uri
    hb = ServiceHeartbeat(
        KNOWN_KEY,
        uri,
        config=HeartbeatConfig(interval_seconds=300),
        instance_digest=DIGEST_A,
    )
    hb._record("READY")
    row = _row(pg_db_migrated.admin, KNOWN_KEY)
    assert row is not None
    assert row[2] == "READY"


def test_42_two_services_isolated(pg_db_migrated) -> None:
    _record(pg_db_migrated.poll_writer, KNOWN_KEY, DIGEST_A, "READY")
    _record(pg_db_migrated.match_results_writer, BOT_KEY, DIGEST_B, "READY")
    with psycopg.connect(pg_db_migrated.admin) as conn:
        keys = {
            r[0]
            for r in conn.execute(
                "SELECT service_key FROM public.fc27_service_heartbeats"
            ).fetchall()
        }
    assert keys == {KNOWN_KEY, BOT_KEY}
