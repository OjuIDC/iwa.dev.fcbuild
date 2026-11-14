"""Comprehensive offline test suite for the FC27 Discord notification dispatcher."""

from __future__ import annotations

import asyncio
import json
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, LiteralString

import psycopg
import pytest
import yaml

from scripts.fc27_discord_bot import main as discord_cli_main
from src.fc27_discord_bot import (
    DISCORD_DATABASE_WRITE_ACCESS,
    DISCORD_PROVIDER_REQUESTS,
    FC27DiscordBotConfig,
    FC27DiscordDisabledError,
    FC27DiscordEmbed,
    FC27DiscordError,
    PSEUDONYM_PLAYER_REF_RE,
    run_discord_gateway_bot,
)
from src.fc27_discord_dispatcher import (
    DISPATCH_PROVIDER_REQUESTS,
    DiscordGatewayChannelTransport,
    FC27DiscordDispatchAccessDeniedError,
    FC27DiscordDispatchConfig,
    FC27DiscordDispatchPostureError,
    FC27DiscordDispatchTransportError,
    FC27DiscordDispatchValidationError,
    FC27PostNewResultsMessage,
    GuardedDiscordJobQueueConnection,
    SyntheticDiscordNotificationTransport,
    assert_sql_is_discord_job_queue_allowed,
    build_post_new_results_notification,
    claim_discord_jobs_batch,
    complete_discord_job,
    load_authoritative_match_for_job,
    release_dispatch_advisory_lock,
    resolve_discord_job_writer_uri,
    resolve_discord_readonly_uri,
    run_discord_dispatch_cycle,
    run_discord_dispatcher_loop,
    try_acquire_dispatch_advisory_lock,
)
from src.fc27_polling_worker import FC27PollingRuntime, run_fc27_poll_cycle
from src.fc27_privacy import (
    EAST_COASTERS_CLUB_ID,
    PSEUDONYM_MATCH_REF_RE,
    pseudonymize_match_ref,
)
from src.settings import ConfigError
from tests.test_fc27_polling_worker import (
    _build_validated_envelope,
    _enable_all_polling_gates,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_CHANNEL_ID = "456789012345678901"
ALLOWED_GUILD_ID = "123456789012345678"
ALLOWED_CHANNEL_ID = "234567890123456789"
ALLOWED_ROLE_ID = "345678901234567890"



def _fetch_one(
    conn: psycopg.Connection[Any],
    query: LiteralString,
    params: tuple[Any, ...] | None = None,
) -> tuple[Any, ...]:
    row = conn.execute(query, params).fetchone()
    assert row is not None, f"expected exactly one row for query: {query.strip()}"
    return row

def _seed_worker_matches_and_jobs(
    monkeypatch: pytest.MonkeyPatch,
    db_uris: Any,
    *,
    count: int = 5,
    start_id: int = 995101,
    players_per_match: int = 6,
    match_type: str = "leagueMatch",
) -> FC27PollingRuntime:
    """Seed realistic persisted matches, player stats, and worker-created Discord jobs."""
    _enable_all_polling_gates(monkeypatch)
    runtime = FC27PollingRuntime()
    env, _ = _build_validated_envelope(
        count=count,
        start_id=start_id,
        players_per_match=players_per_match,
        match_type=match_type,
    )
    res = run_fc27_poll_cycle(
        runtime=runtime,
        confirm_live_polling=True,
        db_conn_or_uri=db_uris.poll_writer,
        fetch_matches_fn=lambda: env,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert res.poll_status == "NEW_MATCHES_PERSISTED"
    assert res.inserted_match_count == count
    assert res.discord_jobs_inserted == count
    monkeypatch.delenv("FC27_ALIAS_HMAC_KEY", raising=False)
    monkeypatch.delenv("FC27_COMMUNITY_POLLING_ENABLED", raising=False)
    monkeypatch.delenv("FC27_COMMUNITY_PROVIDER_ENABLED", raising=False)
    monkeypatch.delenv("FC27_PERSISTENCE_ENABLED", raising=False)
    return runtime


def _enabled_config(**overrides: Any) -> FC27DiscordDispatchConfig:
    params: dict[str, Any] = {
        "enabled": True,
        "results_channel_id": RESULTS_CHANNEL_ID,
        "max_jobs_per_cycle": 5,
        "lease_minutes": 5,
        "single_flight": True,
        "poll_interval_seconds": 0.1,
    }
    params.update(overrides)
    return FC27DiscordDispatchConfig(**params)


def test_01_default_disabled_fails_closed(
    monkeypatch: pytest.MonkeyPatch, pg_db_migrated
) -> None:
    """Dispatcher defaults to disabled and executes zero DB claims or Discord sends when disabled."""
    for env_key in (
        "FC27_DISCORD_DISPATCH_ENABLED",
        "FC27_DISCORD_RESULTS_CHANNEL_ID",
        "DISCORD_RESULTS_CHANNEL_ID",
        "FC27_DISCORD_MAX_JOBS_PER_CYCLE",
        "FC27_DISCORD_LEASE_MINUTES",
        "FC27_DISCORD_SINGLE_FLIGHT",
    ):
        monkeypatch.delenv(env_key, raising=False)

    cfg = FC27DiscordDispatchConfig.from_env(strict=False)
    assert cfg.enabled is False
    assert cfg.max_jobs_per_cycle == 5
    assert cfg.lease_minutes == 5
    assert cfg.single_flight is True
    assert DISPATCH_PROVIDER_REQUESTS == 0

    _seed_worker_matches_and_jobs(monkeypatch, pg_db_migrated, count=2)
    transport = SyntheticDiscordNotificationTransport()
    res = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
    )
    assert res.enabled is False
    assert res.jobs_claimed == 0
    assert res.messages_sent == 0
    assert res.provider_requests_executed == 0
    assert transport.sent_messages == []

    with psycopg.connect(pg_db_migrated.discord_reader) as conn:
        pending_count = _fetch_one(conn, "SELECT COUNT(*) FROM public.fc27_discord_jobs WHERE state = 'pending'")[0]
    assert pending_count == 2


def test_02_missing_or_invalid_results_channel_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enabling dispatcher without a valid positive numeric FC27_DISCORD_RESULTS_CHANNEL_ID fails closed."""
    monkeypatch.setenv("FC27_DISCORD_DISPATCH_ENABLED", "true")
    monkeypatch.delenv("FC27_DISCORD_RESULTS_CHANNEL_ID", raising=False)
    monkeypatch.delenv("DISCORD_RESULTS_CHANNEL_ID", raising=False)

    non_strict = FC27DiscordDispatchConfig.from_env(strict=False)
    assert non_strict.enabled is False

    with pytest.raises(FC27DiscordDispatchValidationError):
        FC27DiscordDispatchConfig.from_env(strict=True)

    with pytest.raises(FC27DiscordDispatchValidationError):
        FC27DiscordDispatchConfig(enabled=True, results_channel_id=None)

    for bad_chan in ("not-a-snowflake", "0", "-123"):
        with pytest.raises(FC27DiscordDispatchValidationError):
            FC27DiscordDispatchConfig(enabled=True, results_channel_id=bad_chan)

    monkeypatch.setenv("FC27_DISCORD_BOT_ENABLED", "true")
    monkeypatch.setenv("FC27_DISCORD_ALLOWED_GUILD_IDS", ALLOWED_GUILD_ID)
    monkeypatch.setenv("FC27_DISCORD_ALLOWED_CHANNEL_IDS", ALLOWED_CHANNEL_ID)
    monkeypatch.setenv("FC27_DISCORD_ALLOWED_ROLE_IDS", ALLOWED_ROLE_ID)
    monkeypatch.setenv("DISCORD_APPLICATION_ID", "999888777666555444")
    with pytest.raises(FC27DiscordDisabledError):
        FC27DiscordBotConfig.from_env(strict=True, require_application_id=True)


def test_03_bounds_validation_for_cycle_and_lease_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dispatcher bounds (max_jobs 1..25, lease_minutes 1..60, single_flight bool) reject invalid strict config."""
    monkeypatch.setenv("FC27_DISCORD_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("FC27_DISCORD_RESULTS_CHANNEL_ID", RESULTS_CHANNEL_ID)
    monkeypatch.setenv("FC27_DISCORD_MAX_JOBS_PER_CYCLE", "0")
    with pytest.raises(FC27DiscordDispatchValidationError):
        FC27DiscordDispatchConfig.from_env(strict=True)

    monkeypatch.setenv("FC27_DISCORD_MAX_JOBS_PER_CYCLE", "26")
    with pytest.raises(FC27DiscordDispatchValidationError):
        FC27DiscordDispatchConfig.from_env(strict=True)

    monkeypatch.setenv("FC27_DISCORD_MAX_JOBS_PER_CYCLE", "5")
    monkeypatch.setenv("FC27_DISCORD_LEASE_MINUTES", "61")
    with pytest.raises(FC27DiscordDispatchValidationError):
        FC27DiscordDispatchConfig.from_env(strict=True)

    monkeypatch.setenv("FC27_DISCORD_LEASE_MINUTES", "5")
    monkeypatch.setenv("FC27_DISCORD_SINGLE_FLIGHT", "invalid_bool")
    with pytest.raises(FC27DiscordDispatchValidationError):
        FC27DiscordDispatchConfig.from_env(strict=True)


def test_04_least_privilege_role_posture_enforcement(pg_db_migrated) -> None:
    """Queue guard accepts only fc27_player_dev_writer and rejects admin, migrator, app, poll_writer, and discord_reader."""
    for forbidden_uri in (
        pg_db_migrated.admin,
        pg_db_migrated.migrator,
        pg_db_migrated.app,
        pg_db_migrated.poll_writer,
        pg_db_migrated.discord_reader,
    ):
        with psycopg.connect(forbidden_uri) as conn:
            with pytest.raises(FC27DiscordDispatchPostureError):
                GuardedDiscordJobQueueConnection(conn, verify_posture=True)

    with psycopg.connect(pg_db_migrated.player_dev_writer) as writer_conn:
        guarded = GuardedDiscordJobQueueConnection(writer_conn, verify_posture=True)
        row = guarded.execute(
            "SELECT COUNT(*) FROM public.fc27_discord_jobs"
        ).fetchone()
        assert row is not None

    # Read-only path must never reuse fc27_player_dev_writer
    with pytest.raises(FC27DiscordDispatchPostureError):
        run_discord_dispatch_cycle(
            config=_enabled_config(),
            transport=SyntheticDiscordNotificationTransport(),
            queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
            readonly_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        )


def test_05_sql_guard_blocks_forbidden_mutations_tables_and_columns(
    pg_db_migrated,
) -> None:
    """SQL guard and Postgres column grants block INSERT, DELETE, DDL, other tables, and immutable job columns."""
    forbidden_queries = (
        "",
        "INSERT INTO public.fc27_discord_jobs (job_type, idempotency_key, payload) VALUES ('post_new_results', 'k', '{}'::jsonb)",
        "DELETE FROM public.fc27_discord_jobs",
        "UPDATE public.fc27_discord_jobs SET payload = '{}'::jsonb WHERE job_id = 1",
        "UPDATE public.fc27_discord_jobs SET job_type = 'evaluate_suggestions' WHERE job_id = 1",
        "UPDATE public.fc27_discord_jobs SET idempotency_key = 'hack' WHERE job_id = 1",
        "UPDATE public.fc27_discord_jobs SET created_at = now() WHERE job_id = 1",
        "SELECT * FROM public.fc27_matches",
        "SELECT * FROM public.fc27_player_match_stats",
        "SELECT * FROM public.fc27_quarantine_records",
        "SELECT * FROM public.fc27_player_alias_fingerprints",
        "SELECT * FROM public.fc27_users",
        "SELECT * FROM public.dispatches",
        "SELECT * FROM public.outbox",
        "SELECT * FROM public.approval_requests",
        "SELECT * FROM public.graph_threads",
        "DROP TABLE public.fc27_discord_jobs",
        "SELECT 1; DELETE FROM public.fc27_discord_jobs",
    )
    for bad_sql in forbidden_queries:
        with pytest.raises(FC27DiscordDispatchAccessDeniedError):
            assert_sql_is_discord_job_queue_allowed(bad_sql)

    with psycopg.connect(pg_db_migrated.player_dev_writer) as writer_conn:
        guarded = GuardedDiscordJobQueueConnection(writer_conn, verify_posture=True)
        for bad_sql in forbidden_queries:
            with pytest.raises(FC27DiscordDispatchAccessDeniedError):
                guarded.execute(bad_sql)

        # Verify raw PostgreSQL grants on fc27_player_dev_writer also deny DELETE / payload UPDATE / fc27_matches writes
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            writer_conn.execute("DELETE FROM public.fc27_discord_jobs")
        writer_conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            writer_conn.execute(
                "UPDATE public.fc27_discord_jobs SET payload = '{}'::jsonb"
            )
        writer_conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            writer_conn.execute(
                "UPDATE public.fc27_matches SET opponent_name = 'Tampered'"
            )
        writer_conn.rollback()


def test_06_poll_writer_credential_and_uri_separation_rejected(
    monkeypatch: pytest.MonkeyPatch, pg_db_migrated
) -> None:
    """Dispatcher refuses to start if polling-worker secrets or shared reader/writer credentials appear."""
    monkeypatch.setenv("FC27_PLAYER_DEV_WRITER_URI", pg_db_migrated.player_dev_writer)
    monkeypatch.setenv("FC27_DISCORD_READER_URI", pg_db_migrated.discord_reader)
    assert resolve_discord_job_writer_uri(required=True) == pg_db_migrated.player_dev_writer
    assert resolve_discord_readonly_uri(required=True) == pg_db_migrated.discord_reader

    for forbidden_env in ("FC27_POLL_WRITER_URI", "FC27_ALIAS_HMAC_KEY"):
        monkeypatch.setenv(forbidden_env, "forbidden-value-12345678901234567890")
        with pytest.raises(ConfigError):
            resolve_discord_job_writer_uri(required=True)
        with pytest.raises(ConfigError):
            resolve_discord_readonly_uri(required=True)
        monkeypatch.delenv(forbidden_env, raising=False)

    # Identical reader and writer URIs fail closed
    with pytest.raises(FC27DiscordDispatchPostureError):
        run_discord_dispatch_cycle(
            config=_enabled_config(),
            transport=SyntheticDiscordNotificationTransport(),
            queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
            readonly_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        )


def test_07_end_to_end_five_pending_jobs_from_worker_dispatched_and_idempotent(
    monkeypatch: pytest.MonkeyPatch, pg_db_migrated
) -> None:
    """Five pending post_new_results jobs from polling worker are claimed, validated, published once, and completed."""
    _seed_worker_matches_and_jobs(monkeypatch, pg_db_migrated, count=5, start_id=995201)
    transport = SyntheticDiscordNotificationTransport()
    cfg = _enabled_config(max_jobs_per_cycle=5)

    res1 = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
    )
    assert res1.enabled is True
    assert res1.single_flight_acquired is True
    assert res1.jobs_claimed == 5
    assert res1.jobs_completed == 5
    assert res1.jobs_failed == 0
    assert res1.jobs_retried == 0
    assert res1.messages_sent == 5
    assert res1.provider_requests_executed == 0
    assert len(transport.sent_messages) == 5

    # Verify persisted DB rows transitioned cleanly to completed
    with psycopg.connect(pg_db_migrated.discord_reader) as conn:
        rows = conn.execute("""
            SELECT job_id, state, claim_owner, claimed_at, lease_expires_at,
                   attempt_count, last_error_category, completed_at
            FROM public.fc27_discord_jobs
            ORDER BY job_id ASC
            """
        ).fetchall()
    assert len(rows) == 5
    for _, state, owner, claimed_at, lease_exp, attempts, err_cat, completed_at in rows:
        assert state == "completed"
        assert owner is not None
        assert claimed_at is not None
        assert lease_exp is None
        assert attempts == 1
        assert err_cat is None
        assert completed_at is not None

    # Running a second cycle sends zero duplicate messages
    res2 = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
    )
    assert res2.jobs_claimed == 0
    assert res2.messages_sent == 0
    assert len(transport.sent_messages) == 5


def test_08_atomic_claim_fields_and_max_jobs_per_cycle_cap(
    monkeypatch: pytest.MonkeyPatch, pg_db_migrated
) -> None:
    """Claiming honors FIFO order and max_jobs_per_cycle bounds without dropping remaining jobs."""
    _seed_worker_matches_and_jobs(monkeypatch, pg_db_migrated, count=5, start_id=995301)

    with psycopg.connect(pg_db_migrated.player_dev_writer) as raw_q:
        qconn = GuardedDiscordJobQueueConnection(raw_q, verify_posture=True)
        claimed = claim_discord_jobs_batch(
            qconn,
            claim_owner="test-worker-cap",
            max_jobs=2,
            lease_minutes=5,
        )
        raw_q.rollback()  # rollback direct test claim before cycle test
    assert len(claimed) == 2
    assert claimed[0].attempt_count == 1
    assert claimed[0].lease_expires_at > claimed[0].claimed_at
    assert claimed[0].job_id < claimed[1].job_id

    transport = SyntheticDiscordNotificationTransport()
    cfg = _enabled_config(max_jobs_per_cycle=2)
    r1 = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
    )
    r2 = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
    )
    r3 = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
    )
    assert (r1.jobs_completed, r2.jobs_completed, r3.jobs_completed) == (2, 2, 1)
    assert len(transport.sent_messages) == 5


def test_09_concurrent_skip_locked_and_single_flight_advisory_lock(
    monkeypatch: pytest.MonkeyPatch, pg_db_migrated
) -> None:
    """SKIP LOCKED partitions claims across workers and advisory lock enforces single-flight cycles."""
    _seed_worker_matches_and_jobs(monkeypatch, pg_db_migrated, count=5, start_id=995401)

    with (
        psycopg.connect(pg_db_migrated.player_dev_writer) as conn1,
        psycopg.connect(pg_db_migrated.player_dev_writer) as conn2,
    ):
        q1 = GuardedDiscordJobQueueConnection(conn1, verify_posture=True)
        q2 = GuardedDiscordJobQueueConnection(conn2, verify_posture=True)
        c1 = claim_discord_jobs_batch(q1, claim_owner="worker-a", max_jobs=3, lease_minutes=5)
        c2 = claim_discord_jobs_batch(q2, claim_owner="worker-b", max_jobs=3, lease_minutes=5)
        assert len(c1) == 3
        assert len(c2) == 2
        assert {j.job_id for j in c1}.isdisjoint({j.job_id for j in c2})
        conn1.rollback()
        conn2.rollback()

    # Hold single-flight advisory lock on conn_lock while running cycle
    with psycopg.connect(pg_db_migrated.player_dev_writer) as conn_lock:
        qlock = GuardedDiscordJobQueueConnection(conn_lock, verify_posture=True)
        assert try_acquire_dispatch_advisory_lock(qlock) is True
        conn_lock.commit()

        transport = SyntheticDiscordNotificationTransport()
        blocked_res = run_discord_dispatch_cycle(
            config=_enabled_config(single_flight=True),
            transport=transport,
            queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
            readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        )
        assert blocked_res.single_flight_acquired is False
        assert blocked_res.jobs_claimed == 0
        assert len(transport.sent_messages) == 0

        assert release_dispatch_advisory_lock(qlock) is True
        conn_lock.commit()

    # After lock release, cycle succeeds
    after_res = run_discord_dispatch_cycle(
        config=_enabled_config(single_flight=True),
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
    )
    assert after_res.single_flight_acquired is True
    assert after_res.jobs_completed == 5
    assert len(transport.sent_messages) == 5


def test_10_expired_lease_recovery_and_completion(
    monkeypatch: pytest.MonkeyPatch, pg_db_migrated
) -> None:
    """Expired claimed leases are recovered back to pending and completed without manual DB edits."""
    _seed_worker_matches_and_jobs(monkeypatch, pg_db_migrated, count=1, start_id=995501)

    with psycopg.connect(pg_db_migrated.player_dev_writer) as conn:
        conn.execute(
            """
            UPDATE public.fc27_discord_jobs
            SET state = 'claimed',
                claim_owner = 'crashed-bot',
                claimed_at = clock_timestamp() - interval '10 seconds',
                lease_expires_at = clock_timestamp() + interval '150 milliseconds',
                attempt_count = 1
            """
        )
        conn.commit()
    time.sleep(0.25)

    transport = SyntheticDiscordNotificationTransport()
    res = run_discord_dispatch_cycle(
        config=_enabled_config(),
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
    )
    assert res.expired_leases_recovered == 1
    assert res.jobs_claimed == 1
    assert res.jobs_completed == 1
    assert res.messages_sent == 1
    assert len(transport.sent_messages) == 1

    with psycopg.connect(pg_db_migrated.discord_reader) as conn:
        state, attempts, err_cat = _fetch_one(conn, "SELECT state, attempt_count, last_error_category FROM public.fc27_discord_jobs")
    assert state == "completed"
    assert attempts == 2
    assert err_cat is None


def test_11_restart_after_send_idempotency_prevents_duplicate_post(
    monkeypatch: pytest.MonkeyPatch, pg_db_migrated
) -> None:
    """If the bot dies after sending to Discord but before marking completed, recovery detects existing post and does not double-post."""
    _seed_worker_matches_and_jobs(monkeypatch, pg_db_migrated, count=1, start_id=995601)
    transport = SyntheticDiscordNotificationTransport()

    # Step A: Simulate worker 1 claiming, verifying, sending to Discord, then dying before complete_discord_job()
    with (
        psycopg.connect(pg_db_migrated.player_dev_writer) as qconn_raw,
        psycopg.connect(pg_db_migrated.discord_reader) as roconn_raw,
    ):
        qconn = GuardedDiscordJobQueueConnection(qconn_raw, verify_posture=True)
        claimed = claim_discord_jobs_batch(qconn, claim_owner="crashed-after-send", max_jobs=1)
        qconn_raw.commit()
        assert len(claimed) == 1
        job = claimed[0]

        from src.fc27_discord_bot import GuardedDiscordReadOnlyConnection

        ro_guarded = GuardedDiscordReadOnlyConnection(roconn_raw, verify_posture=True)
        mdata, forbidden = load_authoritative_match_for_job(
            ro_guarded,
            raw_payload=job.payload,
            idempotency_key=job.idempotency_key,
        )
        msg = build_post_new_results_notification(
            mdata,
            channel_id=RESULTS_CHANNEL_ID,
            idempotency_key=job.idempotency_key,
            extra_forbidden_tokens=forbidden,
        )
        transport.send_notification(channel_id=RESULTS_CHANNEL_ID, message=msg)
        assert len(transport.sent_messages) == 1

        # Simulate short lease expiring after crash without calling complete_discord_job
        qconn_raw.execute(
            "UPDATE public.fc27_discord_jobs SET lease_expires_at = clock_timestamp() + interval '150 milliseconds' WHERE job_id = %s",
            (job.job_id,),
        )
        qconn_raw.commit()
    time.sleep(0.25)

    # Step B: Restarted dispatcher cycle checks channel history and completes job without sending a second Discord post
    recovery_res = run_discord_dispatch_cycle(
        config=_enabled_config(),
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
    )
    assert recovery_res.expired_leases_recovered == 1
    assert recovery_res.jobs_claimed == 1
    assert recovery_res.jobs_completed == 1
    assert recovery_res.jobs_deduplicated == 1
    assert recovery_res.messages_sent == 0
    assert recovery_res.audit_entries[-1].action == "DEDUPLICATED_COMPLETED"
    assert len(transport.sent_messages) == 1

    with psycopg.connect(pg_db_migrated.discord_reader) as conn:
        state, attempts = _fetch_one(conn, "SELECT state, attempt_count FROM public.fc27_discord_jobs")
    assert state == "completed"
    assert attempts == 2


def test_12_transient_discord_failure_retries_with_backoff_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, pg_db_migrated
) -> None:
    """Transient Discord failures release jobs back to pending with backoff and complete on subsequent retry."""
    _seed_worker_matches_and_jobs(monkeypatch, pg_db_migrated, count=1, start_id=995701)
    transport = SyntheticDiscordNotificationTransport()

    def _fail_429(_msg: FC27PostNewResultsMessage) -> None:
        raise FC27DiscordDispatchTransportError(
            "rate limited",
            category="DISCORD_RATE_LIMITED",
            retryable=True,
            retry_after_seconds=30.0,
        )

    transport.on_before_send = _fail_429
    res1 = run_discord_dispatch_cycle(
        config=_enabled_config(),
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
    )
    assert res1.jobs_retried == 1
    assert res1.jobs_failed == 0
    assert res1.messages_sent == 0
    assert res1.audit_entries[-1].action == "RETRIED_PENDING"
    assert res1.audit_entries[-1].error_category == "DISCORD_RATE_LIMITED"

    with psycopg.connect(pg_db_migrated.discord_reader) as conn:
        state, err_cat, attempts, future_avail = _fetch_one(conn, """
            SELECT state, last_error_category, attempt_count, (available_at > clock_timestamp())
            FROM public.fc27_discord_jobs
            """)
    assert state == "pending"
    assert err_cat == "DISCORD_RATE_LIMITED"
    assert attempts == 1
    assert future_avail is True

    # While still in backoff window, immediate cycle does not claim the backed-off job
    res_wait = run_discord_dispatch_cycle(
        config=_enabled_config(),
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
    )
    assert res_wait.jobs_claimed == 0

    # Once backoff elapses and fault clears, retry delivers once and marks completed
    transport.on_before_send = None
    with psycopg.connect(pg_db_migrated.player_dev_writer) as conn:
        conn.execute("UPDATE public.fc27_discord_jobs SET available_at = clock_timestamp() - interval '1 second'")
        conn.commit()

    res2 = run_discord_dispatch_cycle(
        config=_enabled_config(),
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
    )
    assert res2.jobs_completed == 1
    assert res2.messages_sent == 1
    assert len(transport.sent_messages) == 1

    with psycopg.connect(pg_db_migrated.discord_reader) as conn:
        state, err_cat, attempts = _fetch_one(conn, "SELECT state, last_error_category, attempt_count FROM public.fc27_discord_jobs")
    assert state == "completed"
    assert err_cat is None
    assert attempts == 2


def test_13_exhausted_retry_ceiling_marks_failed_permanently(
    monkeypatch: pytest.MonkeyPatch, pg_db_migrated
) -> None:
    """Reaching max_attempts (10) transitions retryable failures or expired leases to terminal failed state."""
    _seed_worker_matches_and_jobs(monkeypatch, pg_db_migrated, count=2, start_id=995801)
    with psycopg.connect(pg_db_migrated.player_dev_writer) as conn:
        jobs = conn.execute(
            "SELECT job_id FROM public.fc27_discord_jobs ORDER BY job_id ASC"
        ).fetchall()
        j1, j2 = jobs[0][0], jobs[1][0]
        # Job 1: pending at attempt_count = 9 -> 10th attempt transient error marks failed
        conn.execute(
            "UPDATE public.fc27_discord_jobs SET attempt_count = 9 WHERE job_id = %s",
            (j1,),
        )
        # Job 2: claimed at attempt_count = 10 with short lease that expires before cleanup
        conn.execute("""
            UPDATE public.fc27_discord_jobs
            SET state = 'claimed',
                claim_owner = 'old-worker',
                claimed_at = clock_timestamp() - interval '10 seconds',
                lease_expires_at = clock_timestamp() + interval '150 milliseconds',
                attempt_count = 10
            WHERE job_id = %s
            """,
            (j2,),
        )
        conn.commit()
    time.sleep(0.25)

    transport = SyntheticDiscordNotificationTransport()

    def _fail_503(_msg: FC27PostNewResultsMessage) -> None:
        raise FC27DiscordDispatchTransportError(
            "service unavailable",
            category="DISCORD_TRANSIENT_FAILURE",
            retryable=True,
        )

    transport.on_before_send = _fail_503
    res = run_discord_dispatch_cycle(
        config=_enabled_config(),
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
    )
    assert res.exhausted_jobs_failed == 1
    assert res.jobs_claimed == 1
    assert res.jobs_failed == 1
    assert res.messages_sent == 0

    with psycopg.connect(pg_db_migrated.discord_reader) as conn:
        row1 = _fetch_one(conn, "SELECT state, last_error_category, attempt_count, claim_owner FROM public.fc27_discord_jobs WHERE job_id = %s", (j1,))
        row2 = _fetch_one(conn, "SELECT state, last_error_category, attempt_count, claim_owner FROM public.fc27_discord_jobs WHERE job_id = %s", (j2,))
    assert row1 == ("failed", "DISCORD_TRANSIENT_FAILURE", 10, None)
    assert row2 == ("failed", "LEASE_EXPIRED_MAX_ATTEMPTS", 10, None)


def test_14_unknown_job_type_left_untouched_and_audited_without_crash(
    monkeypatch: pytest.MonkeyPatch, pg_db_migrated
) -> None:
    """Pending jobs with unsupported job_type (evaluate_suggestions) are left untouched, safely audited, and never block post_new_results."""
    _seed_worker_matches_and_jobs(monkeypatch, pg_db_migrated, count=1, start_id=995901)
    with psycopg.connect(pg_db_migrated.player_dev_writer) as conn:
        conn.execute(
            """
            INSERT INTO public.fc27_discord_jobs (job_type, idempotency_key, payload, state)
            VALUES ('evaluate_suggestions', 'fc27:eval:test1', '{"schema_version":"v1"}'::jsonb, 'pending')
            """
        )
        conn.commit()

    transport = SyntheticDiscordNotificationTransport()
    res = run_discord_dispatch_cycle(
        config=_enabled_config(),
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
    )
    assert res.jobs_skipped_unknown_type == 1
    assert res.jobs_claimed == 1
    assert res.jobs_completed == 1
    assert res.messages_sent == 1
    skipped = [e for e in res.audit_entries if e.action == "SKIPPED_UNKNOWN_JOB_TYPE"]
    assert len(skipped) == 1
    assert skipped[0].job_type == "evaluate_suggestions"

    with psycopg.connect(pg_db_migrated.discord_reader) as conn:
        eval_row = _fetch_one(conn, """
            SELECT state, attempt_count, claim_owner, last_error_category
            FROM public.fc27_discord_jobs
            WHERE job_type = 'evaluate_suggestions'
            """)
    assert eval_row == ("pending", 0, None, None)


def test_15_malformed_or_boundary_violating_payload_fails_closed(
    monkeypatch: pytest.MonkeyPatch, pg_db_migrated
) -> None:
    """Malformed, cross-club, sensitive-key, or mismatched-idempotency payloads fail closed without breaking sibling jobs."""
    bad_mref = pseudonymize_match_ref("996099", club_id=EAST_COASTERS_CLUB_ID)
    with psycopg.connect(pg_db_migrated.poll_writer) as conn:
        conn.execute("""
            INSERT INTO public.fc27_discord_jobs (job_type, idempotency_key, payload, state)
            VALUES
              ('post_new_results', 'bad-empty', '{"unexpected":"payload"}'::jsonb, 'pending'),
              ('post_new_results', %s, %s::jsonb, 'pending'),
              ('post_new_results', 'fc27:post_new_results:wrong_key', %s::jsonb, 'pending')
            """,
            (
                f"fc27:post_new_results:{bad_mref}",
                json.dumps(
                    {
                        "schema_version": "fc27-post-new-results-v1",
                        "event_type": "post_new_results",
                        "match_ref": bad_mref,
                        "club_id": "999999",
                        "platform": "common-gen5",
                        "game_version": "FC27",
                        "match_type": "leagueMatch",
                    }
                ),
                json.dumps(
                    {
                        "schema_version": "fc27-post-new-results-v1",
                        "event_type": "post_new_results",
                        "match_ref": bad_mref,
                        "club_id": EAST_COASTERS_CLUB_ID,
                        "platform": "common-gen5",
                        "game_version": "FC27",
                        "match_type": "leagueMatch",
                    }
                ),
            ),
        )
        conn.commit()

    # Add 1 valid match + job
    _seed_worker_matches_and_jobs(monkeypatch, pg_db_migrated, count=1, start_id=996001)

    transport = SyntheticDiscordNotificationTransport()
    res = run_discord_dispatch_cycle(
        config=_enabled_config(max_jobs_per_cycle=10),
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
    )
    assert res.jobs_claimed == 4
    assert res.jobs_failed == 3
    assert res.jobs_completed == 1
    assert res.messages_sent == 1

    with psycopg.connect(pg_db_migrated.discord_reader) as conn:
        states = conn.execute(
            "SELECT idempotency_key, state, last_error_category FROM public.fc27_discord_jobs ORDER BY job_id ASC"
        ).fetchall()
    assert states[0][1] == "failed" and states[0][2] == "INVALID_JOB_PAYLOAD"
    assert states[1][1] == "failed" and states[1][2] == "CLUB_BOUNDARY_MISMATCH"
    assert states[2][1] == "failed" and states[2][2] == "IDEMPOTENCY_KEY_MISMATCH"
    assert states[3][1] == "completed" and states[3][2] is None


def test_16_missing_authoritative_match_fails_closed(pg_db_migrated) -> None:
    """Job pointing to non-existent match_ref fails closed as MATCH_NOT_FOUND without sending."""
    missing_mref = pseudonymize_match_ref("nonexistent-match-999", club_id=EAST_COASTERS_CLUB_ID)
    with psycopg.connect(pg_db_migrated.poll_writer) as conn:
        conn.execute(
            """
            INSERT INTO public.fc27_discord_jobs (job_type, idempotency_key, payload, state)
            VALUES ('post_new_results', %s, %s::jsonb, 'pending')
            """,
            (
                f"fc27:post_new_results:{missing_mref}",
                json.dumps(
                    {
                        "schema_version": "fc27-post-new-results-v1",
                        "event_type": "post_new_results",
                        "match_ref": missing_mref,
                        "club_id": EAST_COASTERS_CLUB_ID,
                        "platform": "common-gen5",
                        "game_version": "FC27",
                        "match_type": "leagueMatch",
                    }
                ),
            ),
        )
        conn.commit()

    transport = SyntheticDiscordNotificationTransport()
    res = run_discord_dispatch_cycle(
        config=_enabled_config(),
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
    )
    assert res.jobs_claimed == 1
    assert res.jobs_failed == 1
    assert res.messages_sent == 0
    assert res.audit_entries[0].error_category == "MATCH_NOT_FOUND"


def test_17_quarantined_or_rejected_match_never_published(
    monkeypatch: pytest.MonkeyPatch, pg_db_migrated
) -> None:
    """Matches under active quarantine (including REPLAY_CONFLICT) or REJECTED quality never publish to Discord."""
    _seed_worker_matches_and_jobs(monkeypatch, pg_db_migrated, count=3, start_id=996201)

    with psycopg.connect(pg_db_migrated.admin) as conn:
        matches = conn.execute(
            "SELECT match_id, external_match_id, export_id FROM public.fc27_matches ORDER BY played_at ASC"
        ).fetchall()
        m1_id, m1_ext, _ = matches[0]
        m2_id, m2_ext, m2_exp = matches[1]
        m3_id, _, _ = matches[2]

        # Match 1: match-level REPLAY_CONFLICT quarantine
        conn.execute(
            """
            INSERT INTO public.fc27_quarantine_records (
                club_id, platform, game_version, external_match_id, export_id,
                source_classification, anomaly_category, reason_code, payload_sha256, review_status
            ) VALUES (%s, 'common-gen5', 'FC27', %s, %s, 'COMMUNITY_RESEARCH_DERIVED',
                      'REPLAY_CONFLICT', 'CANONICAL_MATCH_HASH_CONFLICT', %s, 'PENDING_REVIEW')
            """,
            (EAST_COASTERS_CLUB_ID, m1_ext, m2_exp, "a" * 64),
        )
        # Wait: Match 1 external_match_id AND export_id=m2_exp blocks both m1 (REPLAY_CONFLICT) and m2 (export batch block)!
        # Match 3: data_quality_status = 'REJECTED'
        conn.execute(
            "UPDATE public.fc27_matches SET data_quality_status = 'REJECTED' WHERE match_id = %s",
            (m3_id,),
        )
        conn.commit()

    transport = SyntheticDiscordNotificationTransport()
    res = run_discord_dispatch_cycle(
        config=_enabled_config(),
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
    )
    assert res.jobs_claimed == 3
    assert res.jobs_failed == 3
    assert res.messages_sent == 0
    assert transport.sent_messages == []
    categories = {e.error_category for e in res.audit_entries}
    assert "REPLAY_CONFLICT_QUARANTINED" in categories
    assert "MATCH_QUALITY_REJECTED" in categories


def test_18_privacy_and_output_sanitization_zero_pii_or_secrets(
    monkeypatch: pytest.MonkeyPatch, pg_db_migrated
) -> None:
    """Notifications emit only pseudonymized mref/plr tokens, scrub mentions/URLs, and never leak display_alias, raw IDs, or secrets."""
    _seed_worker_matches_and_jobs(monkeypatch, pg_db_migrated, count=1, start_id=996301)

    with psycopg.connect(pg_db_migrated.admin) as conn:
        match_id, raw_ext_id, raw_export_id = _fetch_one(conn, "SELECT match_id::text, external_match_id, export_id FROM public.fc27_matches LIMIT 1")
        conn.execute(
            "UPDATE public.fc27_matches SET opponent_name = 'Rival @everyone FC'",
        )
        conn.execute(
            "UPDATE public.fc27_player_match_stats SET display_alias = 'SecretRealGamertag99', alias_visibility_approved = true",
        )
        conn.commit()

    fake_token = "FakeBotTokenSecret987654321"
    transport = SyntheticDiscordNotificationTransport()
    res = run_discord_dispatch_cycle(
        config=_enabled_config(),
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        secret_tokens_to_scrub=(fake_token,),
    )
    assert res.jobs_completed == 1
    assert len(transport.sent_messages) == 1

    msg = transport.sent_messages[0]
    rendered = "\n".join(
        [
            msg.content,
            msg.embed.title,
            msg.embed.description,
            *msg.embed.fields.keys(),
            *msg.embed.fields.values(),
        ]
    )
    assert "SecretRealGamertag99" not in rendered
    assert raw_ext_id not in rendered
    assert match_id not in rendered
    assert raw_export_id not in rendered
    assert EAST_COASTERS_CLUB_ID not in rendered
    assert fake_token not in rendered
    assert "@everyone" not in rendered
    assert "[mention-suppressed]" in rendered
    assert PSEUDONYM_MATCH_REF_RE.fullmatch(msg.match_ref)
    assert msg.match_ref not in rendered
    assert "mref_ecf_" not in rendered
    assert "plr_ecf_" not in rendered
    assert "ref:" not in rendered
    assert "leagueMatch" not in rendered
    assert "COMMUNITY_RESEARCH_DERIVED" not in rendered
    assert "Match Reference" not in rendered
    assert "Source & Quality" not in rendered
    assert "League Match" in rendered
    assert "Unnamed Player" in rendered


def test_19_gateway_channel_transport_and_background_loop_with_mocked_discord(
    monkeypatch: pytest.MonkeyPatch, pg_db_migrated
) -> None:
    """DiscordGatewayChannelTransport and run_discord_dispatcher_loop publish safely with AllowedMentions.none() and classify HTTP errors."""
    _seed_worker_matches_and_jobs(monkeypatch, pg_db_migrated, count=2, start_id=996401)

    class _FakeAllowedMentions:
        def __init__(self, *, none_mode: bool = False) -> None:
            self.none_mode = none_mode

        @classmethod
        def none(cls) -> _FakeAllowedMentions:
            return cls(none_mode=True)

    class _FakeEmbed:
        def __init__(self, *, title: str, description: str) -> None:
            self.title = title
            self.description = description
            self.fields: list[Any] = []
            self.footer: Any = None

        def add_field(self, *, name: str, value: str, inline: bool = False) -> None:
            self.fields.append(types.SimpleNamespace(name=name, value=value, inline=inline))

        def set_footer(self, *, text: str) -> None:
            self.footer = types.SimpleNamespace(text=text)

    class _FakeHTTPException(Exception):
        def __init__(self, status: int, *, retry_after: float | None = None) -> None:
            super().__init__(f"HTTP {status}")
            self.status = status
            self.retry_after = retry_after

    class _FakeForbidden(_FakeHTTPException):
        def __init__(self) -> None:
            super().__init__(403)

    class _FakeNotFound(_FakeHTTPException):
        def __init__(self) -> None:
            super().__init__(404)

    fake_discord = types.ModuleType("discord")
    setattr(fake_discord, "Embed", _FakeEmbed)
    setattr(fake_discord, "AllowedMentions", _FakeAllowedMentions)
    setattr(fake_discord, "HTTPException", _FakeHTTPException)
    setattr(fake_discord, "Forbidden", _FakeForbidden)
    setattr(fake_discord, "NotFound", _FakeNotFound)
    monkeypatch.setitem(sys.modules, "discord", fake_discord)

    class _FakeChannel:
        def __init__(self) -> None:
            self.sent: list[Any] = []
            self._next_error: Exception | None = None

        async def send(self, *, content: str, embed: Any, allowed_mentions: Any) -> Any:
            assert isinstance(allowed_mentions, _FakeAllowedMentions)
            assert allowed_mentions.none_mode is True
            if self._next_error is not None:
                err = self._next_error
                self._next_error = None
                raise err
            msg_obj = types.SimpleNamespace(
                id=str(7000 + len(self.sent) + 1),
                author=types.SimpleNamespace(id=111222333),
                content=content,
                embeds=[embed],
            )
            self.sent.append(msg_obj)
            return msg_obj

        async def history(self, *, limit: int | None = 50, after: Any = None):
            del after
            messages = list(reversed(self.sent))
            if limit is not None:
                messages = messages[:limit]
            for m in messages:
                yield m

    class _FakeClient:
        def __init__(self, channel: _FakeChannel) -> None:
            self.user = types.SimpleNamespace(id=111222333)
            self._channel = channel

        def get_channel(self, cid: int) -> Any:
            return self._channel if cid == int(RESULTS_CHANNEL_ID) else None

        async def wait_until_ready(self) -> None:
            return None

    fake_channel = _FakeChannel()
    fake_client = _FakeClient(fake_channel)

    async def _run_loop_test() -> None:
        loop = asyncio.get_running_loop()
        transport = DiscordGatewayChannelTransport(fake_client, loop, timeout_seconds=5.0)
        stop_event = asyncio.Event()
        cycles: list[Any] = []

        def _on_cycle(res: Any) -> None:
            cycles.append(res)
            if len(cycles) >= 1:
                stop_event.set()

        await run_discord_dispatcher_loop(
            config=_enabled_config(poll_interval_seconds=0.05),
            transport=transport,
            wait_until_ready_fn=fake_client.wait_until_ready,
            stop_event=stop_event,
            queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
            readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
            on_cycle_complete=_on_cycle,
        )
        assert len(cycles) == 1
        assert cycles[0].jobs_completed == 2
        assert len(fake_channel.sent) == 2

        from src.fc27_match_results import EAST_COASTERS_FOOTER_TEXT

        assert fake_channel.sent[0].embeds[0].footer.text == EAST_COASTERS_FOOTER_TEXT
        assert "ref:" not in fake_channel.sent[0].embeds[0].footer.text

        # Verify pre-send channel history duplicate lookup finds an already posted canonical message
        first_msg = cycles[0].audit_entries[0]
        assert first_msg.match_ref is not None
        first_sent = fake_channel.sent[0]
        first_emb = first_sent.embeds[0]
        probe_message = FC27PostNewResultsMessage(
            channel_id=RESULTS_CHANNEL_ID,
            idempotency_key=first_msg.idempotency_key,
            match_ref=first_msg.match_ref,
            content=first_sent.content,
            embed=__import__("src.fc27_discord_bot", fromlist=["FC27DiscordEmbed"]).FC27DiscordEmbed(
                title=first_emb.title,
                description=first_emb.description,
                fields={f.name: f.value for f in first_emb.fields},
            ),
        )
        found_id = await asyncio.to_thread(
            transport.find_existing_notification,
            channel_id=RESULTS_CHANNEL_ID,
            match_ref=first_msg.match_ref,
            idempotency_key=first_msg.idempotency_key,
            message=probe_message,
        )
        assert found_id == first_sent.id

        # Verify HTTP error classification (429 retryable, 403 permanent)
        fake_msg = FC27PostNewResultsMessage(
            channel_id=RESULTS_CHANNEL_ID,
            idempotency_key="fc27:post_new_results:mref_ecf_000000000001",
            match_ref="mref_ecf_000000000001",
            content="Test notification",
            embed=fake_channel.sent[0].embeds[0].__dict__
            and __import__("src.fc27_discord_bot", fromlist=["FC27DiscordEmbed"]).FC27DiscordEmbed(
                title="East Coasters — WIN 2 - 0 vs Test FC",
                description="Test summary",
                fields={"Match Reference": "`mref_ecf_000000000001`"},
            ),
        )
        fake_channel._next_error = _FakeHTTPException(429, retry_after=12.0)
        with pytest.raises(FC27DiscordDispatchTransportError) as err_429:
            await asyncio.to_thread(
                transport.send_notification,
                channel_id=RESULTS_CHANNEL_ID,
                message=fake_msg,
            )
        assert err_429.value.category == "DISCORD_RATE_LIMITED"
        assert err_429.value.retryable is True

        fake_channel._next_error = _FakeForbidden()
        with pytest.raises(FC27DiscordDispatchTransportError) as err_403:
            await asyncio.to_thread(
                transport.send_notification,
                channel_id=RESULTS_CHANNEL_ID,
                message=fake_msg,
            )
        assert err_403.value.category == "DISCORD_CHANNEL_FORBIDDEN"
        assert err_403.value.retryable is False

    asyncio.run(_run_loop_test())

    # Gateway bot refuses startup if dispatch_enabled=True without valid DB URIs
    monkeypatch.delenv("FC27_PLAYER_DEV_WRITER_URI", raising=False)
    monkeypatch.delenv("FC27_PLAYER_DEV_WRITER_URI_FILE", raising=False)
    monkeypatch.delenv("FC27_DISCORD_READER_URI", raising=False)
    monkeypatch.delenv("FC27_DISCORD_READER_URI_FILE", raising=False)
    bot_cfg = FC27DiscordBotConfig(
        enabled=True,
        application_id="999888777666555444",
        require_application_id=True,
        allowed_guild_ids=frozenset({ALLOWED_GUILD_ID}),
        allowed_channel_ids=frozenset({ALLOWED_CHANNEL_ID}),
        allowed_role_ids=frozenset({ALLOWED_ROLE_ID}),
        results_channel_id=RESULTS_CHANNEL_ID,
        dispatch_enabled=True,
    )
    fake_app_commands = types.SimpleNamespace()
    setattr(fake_discord, "app_commands", fake_app_commands)
    setattr(fake_discord, "Intents", types.SimpleNamespace(none=lambda: types.SimpleNamespace(guilds=False)))
    setattr(fake_discord, "Client", object)
    setattr(fake_discord, "Object", lambda id: types.SimpleNamespace(id=id))
    setattr(fake_discord, "Interaction", object)
    with pytest.raises(FC27DiscordError):
        run_discord_gateway_bot(bot_cfg, token="fake-token-1234567890")


def test_20_cli_status_and_compose_dispatcher_contract(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI --status, docker-compose.discord.yml, and config.example.env preserve safe defaults."""
    for env_key in (
        "FC27_DISCORD_BOT_ENABLED",
        "FC27_DISCORD_DISPATCH_ENABLED",
        "FC27_DISCORD_MAX_JOBS_PER_CYCLE",
        "FC27_DISCORD_LEASE_MINUTES",
        "FC27_DISCORD_SINGLE_FLIGHT",
        "FC27_DISCORD_RESULTS_CHANNEL_ID",
    ):
        monkeypatch.delenv(env_key, raising=False)

    assert DISCORD_PROVIDER_REQUESTS == 0
    assert DISCORD_DATABASE_WRITE_ACCESS is False
    rc = discord_cli_main(["--status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DISCORD_PROVIDER_REQUESTS=0" in out
    assert "DISCORD_DATABASE_WRITE_ACCESS=NO" in out
    assert "DISCORD_DISPATCH_ENABLED=NO" in out
    assert "DISCORD_MAX_JOBS_PER_CYCLE=5" in out
    assert "DISCORD_LEASE_MINUTES=5" in out
    assert "DISCORD_SINGLE_FLIGHT=YES" in out

    compose_doc = yaml.safe_load(
        (REPO_ROOT / "docker-compose.discord.yml").read_text(encoding="utf-8")
    )
    discord_env = compose_doc["services"]["fc27-discord-bot"]["environment"]
    assert discord_env["FC27_DISCORD_DISPATCH_ENABLED"] == "${FC27_DISCORD_DISPATCH_ENABLED:-false}"
    assert discord_env["FC27_DISCORD_MAX_JOBS_PER_CYCLE"] == "${FC27_DISCORD_MAX_JOBS_PER_CYCLE:-5}"
    assert discord_env["FC27_DISCORD_LEASE_MINUTES"] == "${FC27_DISCORD_LEASE_MINUTES:-5}"
    assert discord_env["FC27_DISCORD_SINGLE_FLIGHT"] == "${FC27_DISCORD_SINGLE_FLIGHT:-true}"
    assert "FC27_DISCORD_RESULTS_CHANNEL_ID" in discord_env

    example_env = (REPO_ROOT / "config.example.env").read_text(encoding="utf-8")
    assert "FC27_DISCORD_DISPATCH_ENABLED=false" in example_env
    assert "FC27_DISCORD_MAX_JOBS_PER_CYCLE=5" in example_env
    assert "FC27_DISCORD_LEASE_MINUTES=5" in example_env
    assert "FC27_DISCORD_SINGLE_FLIGHT=true" in example_env


def test_21_gateway_history_is_time_bounded_and_timed_out_sends_are_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery scans paginate back to job creation and transport failures/timeouts defer instead of duplicating."""

    class _FakeHTTPException(Exception):
        def __init__(self, status: int = 0, *, retry_after: float | None = None) -> None:
            super().__init__(f"HTTP {status}")
            self.status = status
            self.retry_after = retry_after

    class _FakeForbidden(_FakeHTTPException):
        def __init__(self) -> None:
            super().__init__(403)

    class _FakeNotFound(_FakeHTTPException):
        def __init__(self) -> None:
            super().__init__(404)

    class _FakeEmbed:
        def __init__(self, *, title: str, description: str) -> None:
            self.title = title
            self.description = description
            self.fields: list[Any] = []
            self.footer: Any = None

        def add_field(self, *, name: str, value: str, inline: bool = False) -> None:
            self.fields.append(types.SimpleNamespace(name=name, value=value, inline=inline))

        def set_footer(self, *, text: str) -> None:
            self.footer = types.SimpleNamespace(text=text)

    class _FakeAllowedMentions:
        @classmethod
        def none(cls) -> _FakeAllowedMentions:
            return cls()

    fake_discord = types.ModuleType("discord")
    setattr(fake_discord, "Embed", _FakeEmbed)
    setattr(fake_discord, "AllowedMentions", _FakeAllowedMentions)
    setattr(fake_discord, "HTTPException", _FakeHTTPException)
    setattr(fake_discord, "Forbidden", _FakeForbidden)
    setattr(fake_discord, "NotFound", _FakeNotFound)
    monkeypatch.setitem(sys.modules, "discord", fake_discord)

    created = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    match_ref = "mref_ecf_000000000777"
    bot_id = 111222333
    unrelated = [
        types.SimpleNamespace(
            id=str(i),
            author=types.SimpleNamespace(id=bot_id),
            content=f"other {i}",
            embeds=[],
        )
        for i in range(60)
    ]
    matching = types.SimpleNamespace(
        id="999",
        author=types.SimpleNamespace(id=bot_id),
        content="",
        embeds=[
            types.SimpleNamespace(
                footer=types.SimpleNamespace(text=f"ref:{match_ref}"),
                description="",
                fields=[],
            )
        ],
    )

    class _Channel:
        def __init__(
            self,
            *,
            error: Exception | None = None,
            cancel_flag: list[bool] | None = None,
        ) -> None:
            self.error = error
            self.cancel_flag = cancel_flag
            self.observed_limit: int | None = 50
            self.observed_after: Any = None

        async def history(self, *, limit: int | None = 50, after: Any = None):
            self.observed_limit = limit
            self.observed_after = after
            if self.error is not None:
                raise self.error
            for msg in unrelated:
                yield msg
            yield matching

        async def send(self, *, content: str, embed: Any, allowed_mentions: Any) -> Any:
            assert allowed_mentions is not None
            try:
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                if self.cancel_flag is not None:
                    self.cancel_flag.append(True)
                raise
            return types.SimpleNamespace(id="1000")

    class _FakeClient:
        def __init__(self, channel: _Channel) -> None:
            self.user = types.SimpleNamespace(id=bot_id)
            self._channel = channel

        def get_channel(self, cid: int) -> Any:
            return self._channel if cid == int(RESULTS_CHANNEL_ID) else None

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        channel = _Channel()
        transport = DiscordGatewayChannelTransport(
            _FakeClient(channel), loop, timeout_seconds=2.0
        )
        found = await asyncio.to_thread(
            transport.find_existing_notification,
            channel_id=RESULTS_CHANNEL_ID,
            match_ref=match_ref,
            idempotency_key=f"fc27:post_new_results:{match_ref}",
            not_before=created,
        )
        assert found == "999"
        assert channel.observed_limit is None
        assert channel.observed_after == created - timedelta(seconds=5)

        forbidden_channel = _Channel(error=_FakeForbidden())
        forbidden_transport = DiscordGatewayChannelTransport(
            _FakeClient(forbidden_channel), loop, timeout_seconds=2.0
        )
        with pytest.raises(FC27DiscordDispatchTransportError) as history_err:
            await asyncio.to_thread(
                forbidden_transport.find_existing_notification,
                channel_id=RESULTS_CHANNEL_ID,
                match_ref=match_ref,
                idempotency_key=f"fc27:post_new_results:{match_ref}",
                not_before=created,
            )
        assert history_err.value.category == "DISCORD_HISTORY_FORBIDDEN"
        assert history_err.value.retryable is True

        cancelled: list[bool] = []
        slow_channel = _Channel(cancel_flag=cancelled)
        slow_transport = DiscordGatewayChannelTransport(
            _FakeClient(slow_channel), loop, timeout_seconds=0.05
        )
        message = FC27PostNewResultsMessage(
            channel_id=RESULTS_CHANNEL_ID,
            idempotency_key=f"fc27:post_new_results:{match_ref}",
            match_ref=match_ref,
            content="Test notification",
            embed=__import__("src.fc27_discord_bot", fromlist=["FC27DiscordEmbed"]).FC27DiscordEmbed(
                title="East Coasters — WIN 2 - 0 vs Test FC",
                description="Test summary",
                fields={"Match Reference": f"`{match_ref}`"},
            ),
        )
        with pytest.raises(FC27DiscordDispatchTransportError) as send_err:
            await asyncio.to_thread(
                slow_transport.send_notification,
                channel_id=RESULTS_CHANNEL_ID,
                message=message,
            )
        assert send_err.value.category == "DISCORD_TIMEOUT"
        assert send_err.value.retryable is True
        await asyncio.sleep(0.1)
        assert cancelled == [True]

    asyncio.run(_run())


def test_22_history_lookup_failure_defers_delivery_instead_of_duplicating(
    monkeypatch: pytest.MonkeyPatch, pg_db_migrated
) -> None:
    """A failed recovery history lookup releases the job for retry and never publishes blindly."""
    _seed_worker_matches_and_jobs(monkeypatch, pg_db_migrated, count=1, start_id=996601)
    with psycopg.connect(pg_db_migrated.player_dev_writer) as conn:
        conn.execute(
            "UPDATE public.fc27_discord_jobs SET attempt_count = 1, state = 'pending'"
        )
        conn.commit()

    class _FailingHistoryTransport(SyntheticDiscordNotificationTransport):
        def find_existing_notification(
            self,
            *,
            channel_id: str,
            match_ref: str,
            idempotency_key: str,
            not_before: datetime | None = None,
        ) -> str | None:
            del channel_id, match_ref, idempotency_key, not_before
            raise FC27DiscordDispatchTransportError(
                "history unavailable",
                category="DISCORD_HISTORY_LOOKUP_FAILED",
                retryable=True,
            )

    transport = _FailingHistoryTransport()
    res = run_discord_dispatch_cycle(
        config=_enabled_config(),
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
    )
    assert res.jobs_claimed == 1
    assert res.jobs_retried == 1
    assert res.messages_sent == 0
    assert transport.sent_messages == []
    assert res.audit_entries[-1].action == "RETRIED_PENDING"
    assert res.audit_entries[-1].error_category == "DISCORD_HISTORY_LOOKUP_FAILED"

    with psycopg.connect(pg_db_migrated.discord_reader) as conn:
        state, err_cat, attempts = _fetch_one(conn, "SELECT state, last_error_category, attempt_count FROM public.fc27_discord_jobs")
    assert state == "pending"
    assert err_cat == "DISCORD_HISTORY_LOOKUP_FAILED"
    assert attempts == 2


def test_23_claim_lost_before_send_is_skipped_not_published(
    monkeypatch: pytest.MonkeyPatch, pg_db_migrated
) -> None:
    """A claim reassigned during recovery scanning is never used to publish a stale duplicate."""
    _seed_worker_matches_and_jobs(monkeypatch, pg_db_migrated, count=1, start_id=996701)
    with psycopg.connect(pg_db_migrated.player_dev_writer) as conn:
        conn.execute(
            "UPDATE public.fc27_discord_jobs SET attempt_count = 1, state = 'pending'"
        )
        conn.commit()
    with psycopg.connect(pg_db_migrated.discord_reader) as conn:
        job_id = _fetch_one(conn, "SELECT job_id FROM public.fc27_discord_jobs")[0]

    class _StealingClaimTransport(SyntheticDiscordNotificationTransport):
        def find_existing_notification(
            self,
            *,
            channel_id: str,
            match_ref: str,
            idempotency_key: str,
            not_before: datetime | None = None,
        ) -> str | None:
            del channel_id, match_ref, idempotency_key, not_before
            with psycopg.connect(pg_db_migrated.player_dev_writer) as conn:
                conn.execute(
                    """
                    UPDATE public.fc27_discord_jobs
                    SET claim_owner = 'thief-worker'
                    WHERE job_id = %s
                    """,
                    (job_id,),
                )
                conn.commit()
            return None

    transport = _StealingClaimTransport()
    res = run_discord_dispatch_cycle(
        config=_enabled_config(),
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
    )
    assert res.jobs_claimed == 1
    assert res.messages_sent == 0
    assert transport.sent_messages == []
    assert any(
        e.action == "LEASE_LOST" and e.error_category == "LEASE_LOST_BEFORE_SEND"
        for e in res.audit_entries
    )
    with psycopg.connect(pg_db_migrated.discord_reader) as conn:
        owner, state = _fetch_one(conn, "SELECT claim_owner, state FROM public.fc27_discord_jobs WHERE job_id = %s", (job_id,))
    assert owner == "thief-worker"
    assert state == "claimed"


def test_24_replay_quarantine_with_pseudonymized_match_ref_blocks_publish(
    monkeypatch: pytest.MonkeyPatch, pg_db_migrated
) -> None:
    """Worker replay quarantines keyed by pseudonymized match ref fail closed for already-queued jobs."""
    _seed_worker_matches_and_jobs(monkeypatch, pg_db_migrated, count=1, start_id=996801)
    with psycopg.connect(pg_db_migrated.admin) as conn:
        payload = _fetch_one(conn, "SELECT payload FROM public.fc27_discord_jobs LIMIT 1")[0]
        pseudonymized_ref = str(payload["match_ref"])
        conn.execute(
            """
            INSERT INTO public.fc27_quarantine_records (
                club_id, platform, game_version, external_match_id, export_id,
                source_classification, anomaly_category, reason_code, payload_sha256, review_status
            ) VALUES (%s, 'common-gen5', 'FC27', %s, %s, 'COMMUNITY_RESEARCH_DERIVED',
                      'REPLAY_CONFLICT', 'DISCORD_JOB_IDEMPOTENCY_CONFLICT', %s, 'PENDING_REVIEW')
            """,
            (EAST_COASTERS_CLUB_ID, pseudonymized_ref, "conflicting-export-999", "c" * 64),
        )
        conn.commit()

    transport = SyntheticDiscordNotificationTransport()
    res = run_discord_dispatch_cycle(
        config=_enabled_config(),
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
    )
    assert res.jobs_claimed == 1
    assert res.jobs_failed == 1
    assert res.messages_sent == 0
    assert transport.sent_messages == []
    assert res.audit_entries[-1].error_category == "REPLAY_CONFLICT_QUARANTINED"


def test_25_unrenderable_unknown_job_audit_key_cannot_block_cycle(
    monkeypatch: pytest.MonkeyPatch, pg_db_migrated
) -> None:
    """An unsupported job with a formula-like idempotency key is audited with a placeholder and never aborts dispatch."""
    _seed_worker_matches_and_jobs(monkeypatch, pg_db_migrated, count=1, start_id=996901)
    with psycopg.connect(pg_db_migrated.admin) as conn:
        conn.execute(
            """
            INSERT INTO public.fc27_discord_jobs (job_type, idempotency_key, payload, state)
            VALUES ('evaluate_suggestions', '=eval:test', '{"schema_version":"v1"}'::jsonb, 'pending')
            """
        )
        conn.commit()

    transport = SyntheticDiscordNotificationTransport()
    res = run_discord_dispatch_cycle(
        config=_enabled_config(),
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
    )
    assert res.jobs_skipped_unknown_type == 1
    assert res.jobs_completed == 1
    assert res.messages_sent == 1
    skipped = [e for e in res.audit_entries if e.action == "SKIPPED_UNKNOWN_JOB_TYPE"]
    assert len(skipped) == 1
    assert skipped[0].job_type == "evaluate_suggestions"
    assert skipped[0].idempotency_key == "[UNRENDERABLE]"

    with psycopg.connect(pg_db_migrated.discord_reader) as conn:
        eval_row = _fetch_one(conn, """
            SELECT state, attempt_count, claim_owner
            FROM public.fc27_discord_jobs
            WHERE job_type = 'evaluate_suggestions'
            """)
    assert eval_row == ("pending", 0, None)


def test_26_dispatcher_batch_resolves_fc27_roster_and_renders_canonical_notification(
    monkeypatch: pytest.MonkeyPatch, pg_db_migrated
) -> None:
    """Dispatcher batch-resolves registered roster names, falls back to Unnamed Player, and omits internal jargon."""
    _seed_worker_matches_and_jobs(monkeypatch, pg_db_migrated, count=1, start_id=997001)

    with psycopg.connect(pg_db_migrated.admin) as conn:
        prefs = [
            row[0]
            for row in conn.execute(
                "SELECT local_player_reference FROM public.fc27_player_match_stats ORDER BY local_player_reference ASC"
            ).fetchall()
        ]
        assert len(prefs) >= 2
        registered_pref = prefs[0]
        conn.execute("UPDATE public.fc27_matches SET formation = 'UNKNOWN'")
        conn.execute(
            """
            INSERT INTO public.fc27_roster (player_id, display_name, display_name_normalized, position, active)
            VALUES (%s, 'Lao', 'lao', 'CM', TRUE)
            ON CONFLICT (player_id) DO UPDATE SET
                display_name = EXCLUDED.display_name,
                display_name_normalized = EXCLUDED.display_name_normalized
            """,
            (registered_pref,),
        )
        conn.commit()

    transport = SyntheticDiscordNotificationTransport()
    res = run_discord_dispatch_cycle(
        config=_enabled_config(),
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
    )
    assert res.jobs_completed == 1
    assert len(transport.sent_messages) == 1
    sent = transport.sent_messages[0]
    assert sent.content.startswith("✅ FULL TIME — East Coasters FC ")
    assert sent.embed.title.startswith("⚽ FULL TIME — ")
    assert sent.embed.color is not None
    assert sent.embed.footer_text is not None and "ref:" not in sent.embed.footer_text

    full_text = "\n".join(
        [
            sent.content,
            sent.embed.title,
            sent.embed.description,
            sent.embed.footer_text or "",
            *(f"{k}: {v}" for k, v in sent.embed.fields.items()),
        ]
    )
    assert "Lao" in full_text
    assert "Unnamed Player" in full_text
    for bad in (
        "mref_ecf_",
        "plr_ecf_",
        "ref:",
        "UNKNOWN",
        "None",
        "null",
        "N/A",
        "leagueMatch",
        "COMMUNITY_RESEARCH_DERIVED",
        "VERIFIED",
        "COMPLETED",
        "Match Reference",
        "Source & Quality",
        "Provenance",
    ):
        assert bad not in full_text


def test_27_async_find_existing_requires_played_timestamp_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Canonical dedup lookup must check 📅 Played so repeat opponent+score matches are not skipped."""

    class _FakeHTTPException(Exception):
        def __init__(self, status: int = 0) -> None:
            super().__init__(f"HTTP {status}")
            self.status = status

    class _FakeForbidden(_FakeHTTPException):
        def __init__(self) -> None:
            super().__init__(403)

    class _FakeNotFound(_FakeHTTPException):
        def __init__(self) -> None:
            super().__init__(404)

    class _FakeEmbed:
        def __init__(self, *, title: str, description: str) -> None:
            self.title = title
            self.description = description
            self.fields: list[Any] = []
            self.footer: Any = None

        def add_field(self, *, name: str, value: str, inline: bool = False) -> None:
            self.fields.append(types.SimpleNamespace(name=name, value=value, inline=inline))

        def set_footer(self, *, text: str) -> None:
            self.footer = types.SimpleNamespace(text=text)

    class _FakeAllowedMentions:
        @classmethod
        def none(cls) -> _FakeAllowedMentions:
            return cls()

    fake_discord = types.ModuleType("discord")
    setattr(fake_discord, "Embed", _FakeEmbed)
    setattr(fake_discord, "AllowedMentions", _FakeAllowedMentions)
    setattr(fake_discord, "HTTPException", _FakeHTTPException)
    setattr(fake_discord, "Forbidden", _FakeForbidden)
    setattr(fake_discord, "NotFound", _FakeNotFound)
    monkeypatch.setitem(sys.modules, "discord", fake_discord)

    bot_id = 111222333
    shared_content = "✅ FULL TIME — East Coasters FC 3-1 Metro United"
    shared_title = "⚽ FULL TIME — WIN 3-1 vs Metro United"
    prior_msg = types.SimpleNamespace(
        id="8801",
        author=types.SimpleNamespace(id=bot_id),
        content=shared_content,
        embeds=[
            types.SimpleNamespace(
                title=shared_title,
                description="**East Coasters FC** 3-1 **Metro United**",
                footer=types.SimpleNamespace(text="East Coasters FC • Est. 2016"),
                fields=[types.SimpleNamespace(name="📅 Played", value="2026-05-01 • 8:00 PM EDT")],
            )
        ],
    )

    class _Channel:
        async def history(self, *, limit: int | None = 50, after: Any = None):
            yield prior_msg

        async def send(self, **kwargs: Any) -> Any:
            return types.SimpleNamespace(id="8802")

    class _Client:
        def __init__(self) -> None:
            self.user = types.SimpleNamespace(id=bot_id)
            self._channel = _Channel()

        def get_channel(self, cid: int) -> Any:
            return self._channel if cid == int(RESULTS_CHANNEL_ID) else None

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        transport = DiscordGatewayChannelTransport(_Client(), loop, timeout_seconds=2.0)
        not_before = datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)

        later_match_msg = FC27PostNewResultsMessage(
            channel_id=RESULTS_CHANNEL_ID,
            idempotency_key="fc27:post_new_results:mref_ecf_222222222222",
            match_ref="mref_ecf_222222222222",
            content=shared_content,
            embed=FC27DiscordEmbed(
                title=shared_title,
                description="**East Coasters FC** 3-1 **Metro United**",
                fields={"📅 Played": "2026-05-08 • 8:00 PM EDT"},
            ),
        )
        found_later = await asyncio.to_thread(
            transport.find_existing_notification,
            channel_id=RESULTS_CHANNEL_ID,
            match_ref=later_match_msg.match_ref,
            idempotency_key=later_match_msg.idempotency_key,
            not_before=not_before,
            message=later_match_msg,
        )
        assert found_later is None

        retry_same_msg = FC27PostNewResultsMessage(
            channel_id=RESULTS_CHANNEL_ID,
            idempotency_key="fc27:post_new_results:mref_ecf_111111111111",
            match_ref="mref_ecf_111111111111",
            content=shared_content,
            embed=FC27DiscordEmbed(
                title=shared_title,
                description="**East Coasters FC** 3-1 **Metro United**",
                fields={"📅 Played": "2026-05-01 • 8:00 PM EDT"},
            ),
        )
        found_retry = await asyncio.to_thread(
            transport.find_existing_notification,
            channel_id=RESULTS_CHANNEL_ID,
            match_ref=retry_same_msg.match_ref,
            idempotency_key=retry_same_msg.idempotency_key,
            not_before=not_before,
            message=retry_same_msg,
        )
        assert found_retry == "8801"

    asyncio.run(_run())
