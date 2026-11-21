"""Comprehensive offline PostgreSQL-backed suite for Migration 0018 and roster-refresh queueing (Finding 8)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
import types
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from pydantic import ValidationError

from src.db_migrate import GRANTS_POLICY, migrate, verify_postconditions
from src.db_roles import GRANTS_POLICY_VERSION
from src.fc27_discord_bot import (
    FC27DiscordBotConfig,
    FC27DiscordBotService,
)
from src.fc27_discord_dispatcher import (
    DISPATCH_PROVIDER_REQUESTS,
    PERMITTED_DISPATCH_JOB_TYPES,
    DiscordGatewayChannelTransport,
    FC27DiscordDispatchConfig,
    FC27DiscordDispatchTransportError,
    FC27DiscordEmbed,
    FC27PostNewResultsMessage,
    FC27TeamChatMessage,
    SyntheticDiscordNotificationTransport,
    run_discord_dispatch_cycle,
)
from src.fc27_match_results import (
    ReconciliationTokenStore,
    build_match_post_tag,
    get_match_discord_message_links,
    list_unresolved_players,
    record_match_discord_message_links,
    record_match_result,
    rename_roster_player,
    tag_match_post_content,
    upsert_roster_player,
)
from src.fc27_privacy import (
    EAST_COASTERS_CLUB_ID,
    EAST_COASTERS_GAME_VERSION,
    EAST_COASTERS_PLATFORM,
    pseudonymize_match_ref,
)
from src.fc27_repository import (
    REFRESH_ROSTER_MESSAGES_EVENT_TYPE,
    FC27PostNewResultsPayload,
    FC27RosterRefreshPayload,
    build_roster_refresh_idempotency_key,
)

APPROVED_GUILD_ID = "123456789012345678"
RESULTS_CHANNEL_ID = "234567890123456789"
TEAM_CHAT_CHANNEL_ID = "234567890123456790"
ADMIN_ROLE_ID = "666666666666666666"
MANAGER_ROLE_ID = "444444444444444444"
MEMBER_ROLE_ID = "777777777777777777"
ADMIN_USER_ID = "345678901234567890"
MEMBER_USER_ID = "888888888888888888"

PLAYER_A = "plr_ecf_aaaa1111aaaa"
PLAYER_B = "plr_ecf_bbbb2222bbbb"
PLAYER_C = "plr_ecf_cccc3333cccc"


def _bot_config() -> FC27DiscordBotConfig:
    return FC27DiscordBotConfig(
        enabled=True,
        allowed_guild_ids=frozenset({APPROVED_GUILD_ID}),
        allowed_channel_ids=frozenset({RESULTS_CHANNEL_ID, TEAM_CHAT_CHANNEL_ID}),
        manager_role_ids=frozenset({MANAGER_ROLE_ID}),
        admin_role_ids=frozenset({ADMIN_ROLE_ID}),
        results_channel_id=RESULTS_CHANNEL_ID,
        team_chat_channel_id=TEAM_CHAT_CHANNEL_ID,
        max_user_commands_per_minute=60,
        max_guild_commands_per_minute=120,
    )


def _dispatch_config(**overrides: Any) -> FC27DiscordDispatchConfig:
    params: dict[str, Any] = {
        "enabled": True,
        "results_channel_id": RESULTS_CHANNEL_ID,
        "team_chat_channel_id": TEAM_CHAT_CHANNEL_ID,
        "allowed_channel_ids": frozenset({RESULTS_CHANNEL_ID, TEAM_CHAT_CHANNEL_ID}),
        "allowed_guild_ids": frozenset({APPROVED_GUILD_ID}),
        "max_jobs_per_cycle": 5,
        "lease_minutes": 5,
        "single_flight": True,
    }
    params.update(overrides)
    return FC27DiscordDispatchConfig(**params)


def _seed_provider_match(
    db_uris: Any,
    *,
    suffix: str,
    opponent: str = "Harbor City FC",
    scorer_ref: str = PLAYER_A,
    other_ref: str = PLAYER_B,
    played_at: datetime | None = None,
    enqueue_post_job: bool = True,
) -> str:
    now = played_at or datetime(2026, 10, 24, 1, 0, tzinfo=UTC)
    export_id = f"exp_{suffix}"
    raw_ext_id = f"ext_{suffix}"
    match_ref = pseudonymize_match_ref(raw_ext_id, club_id=EAST_COASTERS_CLUB_ID)

    with psycopg.connect(db_uris.poll_writer) as pw:
        with pw.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.fc27_clubs (
                    club_id, platform, game_version, club_name,
                    identity_state, manager_authority_status, source_classification,
                    created_at, updated_at
                ) VALUES (
                    %s, %s, %s, 'East Coasters FC',
                    'SYNTHETIC_ONLY', 'UNVERIFIED', 'SYNTHETIC', %s, %s
                )
                ON CONFLICT (club_id, platform, game_version) DO NOTHING
                """,
                (EAST_COASTERS_CLUB_ID, EAST_COASTERS_PLATFORM, EAST_COASTERS_GAME_VERSION, now, now),
            )
            cur.execute(
                """
                INSERT INTO public.fc27_export_batches (
                    export_id, club_id, platform, game_version,
                    contract_version, schema_version, source_classification,
                    supplier_role, supplier_reference, consent_version,
                    consented_categories, payload_sha256,
                    total_matches, accepted_matches, quarantined_matches,
                    applied_at, created_at
                ) VALUES (
                    %s, %s, %s, %s, '1.0.0', '1.0.0', 'SYNTHETIC',
                    'CLUB_MANAGER', 'ea-clubs-community-v1:playoffMatch', '1.0.0',
                    '["MATCH_RESULTS"]'::jsonb, repeat('a', 64),
                    1, 1, 0, %s, %s
                )
                """,
                (export_id, EAST_COASTERS_CLUB_ID, EAST_COASTERS_PLATFORM, EAST_COASTERS_GAME_VERSION, now, now),
            )
            cur.execute(
                """
                INSERT INTO public.fc27_matches (
                    club_id, platform, game_version, external_match_id,
                    export_id, played_at, result, goals_for, goals_against,
                    opponent_name, formation, match_status, match_hash,
                    source_classification, data_quality_status, created_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, 'WIN', 2, 1,
                    %s, '4-3-3', 'COMPLETED', %s,
                    'SYNTHETIC', 'VERIFIED', %s
                )
                RETURNING match_id
                """,
                (
                    EAST_COASTERS_CLUB_ID,
                    EAST_COASTERS_PLATFORM,
                    EAST_COASTERS_GAME_VERSION,
                    raw_ext_id,
                    export_id,
                    now,
                    opponent,
                    hashlib.sha256(f"match-{suffix}".encode()).hexdigest(),
                    now,
                ),
            )
            m_row = cur.fetchone()
            assert m_row is not None
            match_uuid = m_row[0]
            for pref, pos, goals, assists, rating in (
                (scorer_ref, "ST", 2, 0, Decimal("8.90")),
                (other_ref, "CM", 0, 2, Decimal("8.10")),
            ):
                cur.execute(
                    """
                    INSERT INTO public.fc27_player_match_stats (
                        match_id, club_id, platform, game_version, external_match_id,
                        local_player_reference, display_alias, position, role,
                        minutes_played, goals, assists,
                        passes_attempted, passes_completed, tackles_attempted, tackles_won,
                        interceptions, shots, shots_on_target, saves, possession_losses,
                        provider_rating, build_reference, created_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, NULL, %s, NULL,
                        90, %s, %s, 20, 17, 2, 1, 0, 3, 2, 0, 0, %s, NULL, %s
                    )
                    """,
                    (
                        match_uuid,
                        EAST_COASTERS_CLUB_ID,
                        EAST_COASTERS_PLATFORM,
                        EAST_COASTERS_GAME_VERSION,
                        raw_ext_id,
                        pref,
                        pos,
                        goals,
                        assists,
                        rating,
                        now,
                    ),
                )
            if enqueue_post_job:
                payload = FC27PostNewResultsPayload(
                    schema_version="fc27-post-new-results-v1",
                    event_type="post_new_results",
                    match_ref=match_ref,
                    club_id=EAST_COASTERS_CLUB_ID,
                    platform=EAST_COASTERS_PLATFORM,
                    game_version=EAST_COASTERS_GAME_VERSION,
                    match_type="playoffMatch",
                ).to_payload()
                cur.execute(
                    """
                    INSERT INTO public.fc27_discord_jobs (
                        job_type, idempotency_key, payload, state, attempt_count, available_at, created_at
                    ) VALUES (
                        'post_new_results', %s, %s::jsonb, 'pending', 0, %s, %s
                    )
                    """,
                    (f"fc27:post_new_results:{match_ref}", json.dumps(payload), now, now),
                )
        pw.commit()
    return match_ref


def _fetch_refresh_jobs(db_uris: Any) -> list[dict[str, Any]]:
    with psycopg.connect(db_uris.discord_reader) as conn:
        rows = conn.execute(
            """
            SELECT job_id, job_type, idempotency_key, payload, state, attempt_count, last_error_category
            FROM public.fc27_discord_jobs
            ORDER BY job_id ASC
            """
        ).fetchall()
    return [
        {
            "job_id": int(r[0]),
            "job_type": str(r[1]),
            "idempotency_key": str(r[2]),
            "payload": dict(r[3]),
            "state": str(r[4]),
            "attempt_count": int(r[5]),
            "last_error_category": r[6],
        }
        for r in rows
    ]


# ==============================================================================
# PRODUCER & MIGRATION 0018 TESTS (1-10, 31-34, 39)
# ==============================================================================


def test_01_authorized_roster_update_and_rename_enqueue_one_job_each(pg_db_migrated) -> None:
    """1. Authorized roster upsert and rename each enqueue one sanitized refresh job atomically."""
    rec = upsert_roster_player(
        pg_db_migrated.match_results_writer,
        player_id=PLAYER_A,
        display_name="Marcus",
        position="ST",
    )
    assert rec.display_name == "Marcus"

    jobs = _fetch_refresh_jobs(pg_db_migrated)
    assert len(jobs) == 1
    j1 = jobs[0]
    assert j1["job_type"] == REFRESH_ROSTER_MESSAGES_EVENT_TYPE
    assert j1["state"] == "pending"
    assert j1["payload"]["player_id"] == PLAYER_A
    assert j1["payload"]["reason_code"] == "ROSTER_UPSERT"
    assert "Marcus" not in json.dumps(j1["payload"])
    assert j1["idempotency_key"] == build_roster_refresh_idempotency_key(
        player_id=PLAYER_A,
        roster_revision=j1["payload"]["roster_revision"],
    )

    renamed = rename_roster_player(
        pg_db_migrated.match_results_writer,
        player_id_or_name="Marcus",
        new_name="Marcus Jr",
    )
    assert renamed.display_name == "Marcus Jr"

    jobs_after = _fetch_refresh_jobs(pg_db_migrated)
    assert len(jobs_after) == 2
    j2 = jobs_after[1]
    assert j2["job_type"] == REFRESH_ROSTER_MESSAGES_EVENT_TYPE
    assert j2["payload"]["reason_code"] == "ROSTER_RENAME"
    assert j2["payload"]["roster_revision"] != j1["payload"]["roster_revision"]
    assert j2["idempotency_key"] != j1["idempotency_key"]
    assert "Marcus" not in json.dumps(j2["payload"])


def test_02_noop_and_nonpublic_metadata_updates_enqueue_zero_jobs(pg_db_migrated) -> None:
    """2. Repeating identical values, identical rename, or updating only discord_user_id enqueues zero extra jobs."""
    upsert_roster_player(
        pg_db_migrated.match_results_writer,
        player_id=PLAYER_A,
        display_name="Devon",
        position="CM",
    )
    assert len(_fetch_refresh_jobs(pg_db_migrated)) == 1

    # Exact no-op upsert
    upsert_roster_player(
        pg_db_migrated.match_results_writer,
        player_id=PLAYER_A,
        display_name="Devon",
        position="CM",
    )
    assert len(_fetch_refresh_jobs(pg_db_migrated)) == 1

    # Changing only non-public discord_user_id must enqueue 0 extra jobs
    upsert_roster_player(
        pg_db_migrated.match_results_writer,
        player_id=PLAYER_A,
        display_name="Devon",
        position=None,
        discord_user_id="999888777666555444",
    )
    assert len(_fetch_refresh_jobs(pg_db_migrated)) == 1

    # Exact no-op rename
    rename_roster_player(
        pg_db_migrated.match_results_writer,
        player_id_or_name="Devon",
        new_name="Devon",
    )
    assert len(_fetch_refresh_jobs(pg_db_migrated)) == 1

    # Inserting an initially inactive player leaves public views unchanged -> 0 jobs
    upsert_roster_player(
        pg_db_migrated.match_results_writer,
        player_id=PLAYER_B,
        display_name="InactiveBench",
        position="GK",
        active=False,
    )
    assert len(_fetch_refresh_jobs(pg_db_migrated)) == 1


def test_03_failed_transaction_and_dry_run_enqueue_zero_jobs(pg_db_migrated) -> None:
    """3. Rolled-back transaction or dry_run leaves zero jobs in public.fc27_discord_jobs."""
    dry = upsert_roster_player(
        pg_db_migrated.match_results_writer,
        player_id=PLAYER_A,
        display_name="DryOnly",
        position="ST",
        dry_run=True,
    )
    assert dry.display_name == "DryOnly"
    assert _fetch_refresh_jobs(pg_db_migrated) == []

    # Rollback outer tx -> both fc27_roster insert and SECURITY DEFINER enqueue roll back atomically
    with psycopg.connect(pg_db_migrated.match_results_writer) as conn:
        try:
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO public.fc27_roster (player_id, display_name, display_name_normalized, display_name_comparison_key, position, active)
                    VALUES (%s, 'RolledBack', 'rolledback', 'rolledback', 'CB', TRUE)
                    """,
                    (PLAYER_A,),
                )
                conn.execute(
                    "SELECT public.fc27_enqueue_roster_refresh_job(%s, 'rev0001', 'ROSTER_UPSERT', now())",
                    (PLAYER_A,),
                )
                raise RuntimeError("simulated transaction failure")
        except RuntimeError:
            pass

    assert _fetch_refresh_jobs(pg_db_migrated) == []
    with psycopg.connect(pg_db_migrated.discord_reader) as rconn:
        cnt = rconn.execute("SELECT COUNT(*) FROM public.fc27_roster").fetchone()
        assert cnt is not None and cnt[0] == 0


def test_04_unauthorized_command_and_invalid_token_enqueue_zero_jobs(pg_db_migrated) -> None:
    """4. Unauthorized /setplayer /renameplayer and failed token redemption enqueue zero jobs."""
    _seed_provider_match(pg_db_migrated, suffix="authz_04")
    store = ReconciliationTokenStore()
    service = FC27DiscordBotService(
        _bot_config(),
        db_conn_or_uri=pg_db_migrated.discord_reader,
        writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
        reconciliation_token_store=store,
    )
    denied = service.handle_slash_command(
        "setplayer",
        guild_id=APPROVED_GUILD_ID,
        channel_id=RESULTS_CHANNEL_ID,
        user_id=MEMBER_USER_ID,
        user_role_ids=[MEMBER_ROLE_ID],
        player_id=PLAYER_A,
        player_name="UnauthorizedName",
    )
    assert denied.status == "REJECTED_UNAUTHORIZED"

    bad_tok = service.handle_slash_command(
        "setplayer",
        guild_id=APPROVED_GUILD_ID,
        channel_id=RESULTS_CHANNEL_ID,
        user_id=ADMIN_USER_ID,
        user_role_ids=[ADMIN_ROLE_ID],
        token="T99",
        player_name="Nobody",
    )
    assert bad_tok.status in ("ERROR", "REJECTED_VALIDATION")
    assert "Unknown or expired reconciliation token" in bad_tok.content

    # Only the initial post_new_results job exists; 0 refresh jobs created
    jobs = _fetch_refresh_jobs(pg_db_migrated)
    assert all(j["job_type"] == "post_new_results" for j in jobs)


def test_05_token_redemption_enqueues_one_refresh_job(pg_db_migrated) -> None:
    """5. Redeeming a valid persistent reconciliation token enqueues one TOKEN_REDEEM refresh job."""
    _seed_provider_match(pg_db_migrated, suffix="tok_05", enqueue_post_job=False)
    store = ReconciliationTokenStore(
        writer_conn_or_uri=pg_db_migrated.match_results_writer,
        reader_conn_or_uri=pg_db_migrated.discord_reader,
    )
    entries = list_unresolved_players(
        pg_db_migrated.discord_reader,
        token_store=store,
        actor_discord_user_id=ADMIN_USER_ID,
    )
    target_tok = next(e for e in entries if e.player_id == PLAYER_A)

    rec = upsert_roster_player(
        pg_db_migrated.match_results_writer,
        token=target_tok.token,
        actor_discord_user_id=ADMIN_USER_ID,
        display_name="Kai",
        position="ST",
        token_store=store,
    )
    assert rec.player_id == PLAYER_A
    jobs = _fetch_refresh_jobs(pg_db_migrated)
    assert len(jobs) == 1
    assert jobs[0]["job_type"] == REFRESH_ROSTER_MESSAGES_EVENT_TYPE
    assert jobs[0]["payload"]["reason_code"] == "TOKEN_REDEEM"
    assert jobs[0]["payload"]["player_id"] == PLAYER_A


def test_06_duplicate_same_revision_is_idempotent_and_new_revision_creates_new_job(
    pg_db_migrated,
) -> None:
    """6-7. Duplicate SQL enqueue with same (player_id, revision) is a no-op; a later revision inserts a new job."""
    with psycopg.connect(pg_db_migrated.match_results_writer, autocommit=True) as wconn:
        wconn.execute(
            """
            INSERT INTO public.fc27_roster (player_id, display_name, display_name_normalized, display_name_comparison_key, active)
            VALUES (%s, 'Rico', 'rico', 'rico', TRUE)
            """,
            (PLAYER_A,),
        )
        r1 = wconn.execute(
            "SELECT public.fc27_enqueue_roster_refresh_job(%s, 'rev_0001', 'ROSTER_UPSERT', now())",
            (PLAYER_A,),
        ).fetchone()
        r_dup = wconn.execute(
            "SELECT public.fc27_enqueue_roster_refresh_job(%s, 'rev_0001', 'ROSTER_UPSERT', now())",
            (PLAYER_A,),
        ).fetchone()
        r2 = wconn.execute(
            "SELECT public.fc27_enqueue_roster_refresh_job(%s, 'rev_0002', 'ROSTER_RENAME', now())",
            (PLAYER_A,),
        ).fetchone()

    assert r1 is not None and isinstance(r1[0], int)
    assert r_dup is not None and r_dup[0] is None
    assert r2 is not None and isinstance(r2[0], int) and r2[0] != r1[0]
    assert len(_fetch_refresh_jobs(pg_db_migrated)) == 2


def test_07_security_definer_producer_boundary_and_role_matrix(pg_db_migrated) -> None:
    """8-10, 31-34. Verify SECURITY DEFINER owner/search_path, PUBLIC & sibling denial, writer table isolation, and SQL injection rejection."""
    assert GRANTS_POLICY_VERSION == 23
    assert GRANTS_POLICY.version == 23

    # Function metadata checks (owner, secdef, strict search_path)
    with psycopg.connect(pg_db_migrated.admin) as aconn:
        assert verify_postconditions(aconn) == []
        meta = aconn.execute(
            """
            SELECT p.proowner::regrole::text, p.prosecdef, p.proconfig
            FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'public' AND p.proname = 'fc27_enqueue_roster_refresh_job'
            """
        ).fetchone()
        assert meta is not None
        assert meta[0] == "fc27_owner"
        assert meta[1] is True
        assert "search_path=pg_catalog, pg_temp" in " ".join(meta[2] or ())

    # PUBLIC and all sibling non-writer roles cannot execute the enqueue function
    for forbidden_uri in (
        pg_db_migrated.app,
        pg_db_migrated.dispatch,
        pg_db_migrated.relay,
        pg_db_migrated.poll_writer,
        pg_db_migrated.player_dev_writer,
        pg_db_migrated.discord_reader,
    ):
        with (
            psycopg.connect(forbidden_uri, autocommit=True) as fconn,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            fconn.execute(
                "SELECT public.fc27_enqueue_roster_refresh_job(%s, 'rev_x', 'ROSTER_UPSERT', now())",
                (PLAYER_A,),
            )

    # fc27_match_results_writer still has ZERO direct privileges on public.fc27_discord_jobs (no SELECT/INSERT/UPDATE/DELETE)
    with psycopg.connect(pg_db_migrated.match_results_writer, autocommit=True) as wconn:
        for stmt in (
            "SELECT COUNT(*) FROM public.fc27_discord_jobs",
            "INSERT INTO public.fc27_discord_jobs (job_type, idempotency_key, payload) VALUES ('refresh_roster_messages', 'k', '{}'::jsonb)",
            "UPDATE public.fc27_discord_jobs SET state = 'completed'",
            "DELETE FROM public.fc27_discord_jobs",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                wconn.execute(stmt)

        # Rejects invalid job_type-like reason_code, unknown player, bad revision, colon injection, and formula prefix
        wconn.execute(
            "INSERT INTO public.fc27_roster (player_id, display_name, display_name_normalized, display_name_comparison_key, active) VALUES (%s, 'T', 't', 't', TRUE)",
            (PLAYER_A,),
        )
        for bad_args, err_cls in (
            ((PLAYER_A, "rev_1", "post_new_results"), psycopg.errors.InvalidParameterValue),
            ((PLAYER_A, "rev_1", "CONTINUATION"), psycopg.errors.InvalidParameterValue),
            (("plr_ecf_unknown999", "rev_1", "ROSTER_UPSERT"), psycopg.errors.ForeignKeyViolation),
            ((PLAYER_A, "-rev_1", "ROSTER_UPSERT"), psycopg.errors.InvalidParameterValue),
            ((PLAYER_A, "rev:colon", "ROSTER_UPSERT"), psycopg.errors.InvalidParameterValue),
            (("bad:colon", "rev_1", "ROSTER_UPSERT"), psycopg.errors.InvalidParameterValue),
            (("=evil", "rev_1", "ROSTER_UPSERT"), psycopg.errors.InvalidParameterValue),
        ):
            with pytest.raises(err_cls):
                wconn.execute(
                    "SELECT public.fc27_enqueue_roster_refresh_job(%s, %s, %s, now())",
                    bad_args,
                )

    # Direct arbitrary job_type / arbitrary payload keys rejected by DB CHECK constraints & payload validator
    with (
        psycopg.connect(pg_db_migrated.poll_writer, autocommit=True) as pconn,
        pytest.raises(psycopg.errors.CheckViolation),
    ):
        pconn.execute(
            """
            INSERT INTO public.fc27_discord_jobs (job_type, idempotency_key, payload)
            VALUES ('arbitrary_job_type', 'bad-type-1', '{"a":1}'::jsonb)
            """
        )
    with pytest.raises(ValidationError):
        FC27RosterRefreshPayload.model_validate(
            {
                "schema_version": "fc27-roster-refresh-v1",
                "event_type": "refresh_roster_messages",
                "player_id": PLAYER_A,
                "roster_revision": "rev1",
                "reason_code": "ROSTER_UPSERT",
                "requested_at": "2026-11-16T00:00:00Z",
                "channel_id": RESULTS_CHANNEL_ID,
            }
        )


def test_08_migration_0017_to_0018_upgrade_preserves_existing_and_completed_jobs(pg_db) -> None:
    """Phase B9. Upgrading 0017->0018 and re-running idempotently preserves all existing pending/completed/eval jobs."""
    migrate(pg_db.migrator, target="0017")
    with psycopg.connect(pg_db.admin, autocommit=True) as aconn:
        aconn.execute(
            """
            INSERT INTO public.fc27_discord_jobs (job_type, idempotency_key, payload, state, attempt_count, completed_at)
            VALUES
              ('post_new_results', 'fc27:post_new_results:mref_ecf_000000000001', '{"match_ref":"mref_ecf_000000000001"}'::jsonb, 'completed', 1, now()),
              ('evaluate_suggestions', 'fc27:eval:existing', '{"schema_version":"v1"}'::jsonb, 'pending', 0, NULL)
            """
        )

    applied = migrate(pg_db.migrator, target="0018")
    assert applied == ["0018_fc27_roster_refresh_queue.sql"]
    assert migrate(pg_db.migrator, target="0018") == []

    with psycopg.connect(pg_db.admin) as aconn:
        assert verify_postconditions(aconn) == []
        rows = aconn.execute(
            "SELECT job_type, idempotency_key, state, attempt_count FROM public.fc27_discord_jobs ORDER BY job_id ASC"
        ).fetchall()
    assert rows == [
        ("post_new_results", "fc27:post_new_results:mref_ecf_000000000001", "completed", 1),
        ("evaluate_suggestions", "fc27:eval:existing", "pending", 0),
    ]


# ==============================================================================
# CONSUMER, LINKAGE, BOUNDING, RETRY, & DEAD-LETTER TESTS (11-30, 36)
# ==============================================================================


def test_09_to_15_consumer_selectivity_edits_noop_skip_and_no_repost(pg_db_migrated) -> None:
    """11-17, 26-29. Refresh claims job, edits only matching player's formal & team-chat cards in place, skips identical payloads, and never reposts."""
    assert DISPATCH_PROVIDER_REQUESTS == 0
    assert REFRESH_ROSTER_MESSAGES_EVENT_TYPE in PERMITTED_DISPATCH_JOB_TYPES
    assert "evaluate_suggestions" not in PERMITTED_DISPATCH_JOB_TYPES

    mref_ab = _seed_provider_match(
        pg_db_migrated,
        suffix="ref_ab",
        opponent="Harbor City",
        scorer_ref=PLAYER_A,
        other_ref=PLAYER_B,
    )
    mref_bc = _seed_provider_match(
        pg_db_migrated,
        suffix="ref_bc",
        opponent="Seaside United",
        scorer_ref=PLAYER_C,
        other_ref=PLAYER_B,
        played_at=datetime(2026, 10, 25, 1, 0, tzinfo=UTC),
    )

    transport = SyntheticDiscordNotificationTransport()
    cfg = _dispatch_config()

    # Initial dispatch posts 2 formal + 2 team-chat messages
    r_init = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert r_init.jobs_completed == 2
    assert r_init.messages_sent == 2
    assert r_init.team_chat_messages_sent == 2

    links_ab = get_match_discord_message_links(pg_db_migrated.discord_reader, match_ref=mref_ab)
    links_bc = get_match_discord_message_links(pg_db_migrated.discord_reader, match_ref=mref_bc)
    assert links_ab is not None and links_bc is not None
    assert links_ab.results_message_id != links_ab.team_chat_message_id

    # Update PLAYER_A -> enqueues 1 refresh job; mref_bc lacks PLAYER_A and must NOT be touched!
    upsert_roster_player(
        pg_db_migrated.match_results_writer,
        player_id=PLAYER_A,
        display_name="Marcus",
        position="ST",
    )
    r_ref1 = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert r_ref1.jobs_claimed == 1
    assert r_ref1.jobs_completed == 1
    assert r_ref1.messages_edited == 1
    assert r_ref1.team_chat_messages_edited == 1
    assert r_ref1.messages_sent == 0
    assert r_ref1.team_chat_messages_sent == 0
    assert r_ref1.provider_requests_executed == 0
    assert len(transport.sent_messages) == 2
    assert len(transport.sent_team_chat_messages) == 2
    assert len(transport.edited_messages) == 1
    assert transport.edited_messages[0][1] == links_ab.results_message_id
    assert "Marcus x2" in transport.edited_messages[0][2].embed.fields["⚽ Scorers"]
    assert len(transport.edited_team_chat_messages) == 1
    assert transport.edited_team_chat_messages[0][1] == links_ab.team_chat_message_id
    assert "Goals: Marcus x2" in transport.edited_team_chat_messages[0][2].content
    # Ensure no internal IDs leaked into Discord outputs
    for pub_msg in (
        transport.edited_messages[0][2].content,
        transport.edited_messages[0][2].embed.title,
        transport.edited_messages[0][2].embed.description,
        "".join(transport.edited_messages[0][2].embed.fields.values()),
        transport.edited_team_chat_messages[0][2].content,
    ):
        assert PLAYER_A not in pub_msg
        assert mref_ab not in pub_msg

    # Manual identical refresh job -> snapshot comparison detects identical payload -> 0 edits!
    with psycopg.connect(pg_db_migrated.match_results_writer, autocommit=True) as wconn:
        wconn.execute(
            "SELECT public.fc27_enqueue_roster_refresh_job(%s, 'rev_same_check', 'ROSTER_UPSERT', now())",
            (PLAYER_A,),
        )
    r_unchanged = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert r_unchanged.jobs_completed == 1
    assert r_unchanged.messages_edited == 0
    assert r_unchanged.team_chat_messages_edited == 0
    assert r_unchanged.messages_unchanged == 2
    assert len(transport.edited_messages) == 1

    # Missing/deleted known formal message + rename -> NEVER posts replacement, dead-letters cleanly!
    assert links_ab.results_message_id is not None
    transport.delete_message(RESULTS_CHANNEL_ID, links_ab.results_message_id)
    rename_roster_player(
        pg_db_migrated.match_results_writer,
        player_id_or_name="Marcus",
        new_name="Marcus II",
    )
    r_missing = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert r_missing.jobs_failed == 1
    assert r_missing.messages_sent == 0
    assert len(transport.sent_messages) == 2
    assert r_missing.audit_entries[0].error_category == "LINKED_MESSAGE_MISSING"


def test_13_ambiguous_recovery_skipped_and_unambiguous_recovered_linkage_persisted(
    pg_db_migrated,
) -> None:
    """18-19. Ambiguous channel history candidates are skipped; unambiguous missing DB linkage is recovered & persisted before completion."""
    mref_unambig = _seed_provider_match(pg_db_migrated, suffix="rec_unambig")
    mref_ambig = _seed_provider_match(
        pg_db_migrated,
        suffix="rec_ambig",
        played_at=datetime(2026, 10, 26, 1, 0, tzinfo=UTC),
    )

    transport = SyntheticDiscordNotificationTransport()
    cfg = _dispatch_config()
    # Dispatch WITHOUT writer_uri so DB linkage is initially absent (only in transport history)
    r_init = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=None,
    )
    assert r_init.jobs_completed == 2
    assert get_match_discord_message_links(pg_db_migrated.discord_reader, match_ref=mref_unambig) is None

    # Clear transport in-memory linkage map to force history recovery, and plant duplicate candidate for mref_ambig
    orig_ambig_msg = next(m for m in transport.sent_messages if m.match_ref == mref_ambig)
    transport._linkages.clear()
    transport._channel_history[RESULTS_CHANNEL_ID].append(("880000000000099999", orig_ambig_msg))

    upsert_roster_player(
        pg_db_migrated.match_results_writer,
        player_id=PLAYER_A,
        display_name="RecoveredKai",
        position="ST",
    )
    r_rec = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert r_rec.jobs_completed == 1
    assert r_rec.ambiguous_recoveries_skipped >= 1
    assert r_rec.messages_edited == 1
    assert r_rec.team_chat_messages_edited == 2

    # Unambiguous match got recovered and persisted to DB; ambiguous match formal ID stayed unpersisted
    persisted_unambig = get_match_discord_message_links(
        pg_db_migrated.discord_reader, match_ref=mref_unambig
    )
    persisted_ambig = get_match_discord_message_links(
        pg_db_migrated.discord_reader, match_ref=mref_ambig
    )
    assert persisted_unambig is not None and persisted_unambig.results_message_id is not None
    assert persisted_ambig is None or persisted_ambig.results_message_id is None


def test_14_bounded_batches_and_deterministic_continuation(pg_db_migrated) -> None:
    """20-21. Same player across 5 matches with bound=2 runs deterministic continuations until all 5 are edited once."""
    for idx in range(5):
        _seed_provider_match(
            pg_db_migrated,
            suffix=f"bound_{idx}",
            opponent=f"Club{idx}",
            played_at=datetime(2026, 10, 20 + idx, 1, 0, tzinfo=UTC),
        )
    transport = SyntheticDiscordNotificationTransport()
    cfg = _dispatch_config(refresh_max_matches_per_job=2)
    run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )

    upsert_roster_player(
        pg_db_migrated.match_results_writer,
        player_id=PLAYER_A,
        display_name="BoundedStar",
        position="ST",
    )

    c1 = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    c2 = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    c3 = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    c4 = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert (c1.messages_edited, c2.messages_edited, c3.messages_edited, c4.jobs_claimed) == (2, 2, 1, 0)
    assert (c1.continuations_enqueued, c2.continuations_enqueued, c3.continuations_enqueued) == (1, 1, 0)
    assert len({msg_id for _, msg_id, _ in transport.edited_messages}) == 5
    keys = [j["idempotency_key"] for j in _fetch_refresh_jobs(pg_db_migrated) if j["job_type"] == REFRESH_ROSTER_MESSAGES_EVENT_TYPE]
    assert len(keys) == 3
    assert ":cont:1:v1" in keys[1]
    assert ":cont:2:v1" in keys[2]

    # Continuation depth ceiling fails closed instead of looping endlessly
    rename_roster_player(
        pg_db_migrated.match_results_writer,
        player_id_or_name="BoundedStar",
        new_name="DepthBoundedStar",
    )
    shallow_cfg = _dispatch_config(
        refresh_max_matches_per_job=1,
        refresh_max_continuation_depth=1,
    )
    d1 = run_discord_dispatch_cycle(
        config=shallow_cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert d1.jobs_completed == 1
    assert d1.continuations_enqueued == 1
    d2 = run_discord_dispatch_cycle(
        config=shallow_cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert d2.jobs_failed == 1
    assert d2.continuations_enqueued == 0
    assert d2.audit_entries[0].error_category == "REFRESH_CONTINUATION_DEPTH_EXCEEDED"


def test_15_transient_retry_and_permanent_dead_letter_classifications(pg_db_migrated) -> None:
    """22-23. Transient 429/5xx/serialization failures retry and recover; permanent violations dead-letter immediately."""
    _seed_provider_match(pg_db_migrated, suffix="retry_perm_15")
    transport = SyntheticDiscordNotificationTransport()
    cfg = _dispatch_config(retry_backoff_seconds=0)
    run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )

    # 1. Transient 429 on first edit attempt -> retried -> succeeds on 2nd attempt
    upsert_roster_player(
        pg_db_migrated.match_results_writer,
        player_id=PLAYER_A,
        display_name="RetryRecovered",
        position="ST",
    )
    calls = {"n": 0}

    def _flaky_edit(*args: Any, **kwargs: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise FC27DiscordDispatchTransportError(
                "rate limit",
                category="DISCORD_RATE_LIMITED",
                retryable=True,
                retry_after_seconds=0,
            )

    transport.on_before_edit = _flaky_edit
    r_retry = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert r_retry.jobs_retried == 1
    assert r_retry.audit_entries[0].error_category == "DISCORD_RATE_LIMITED"

    with psycopg.connect(pg_db_migrated.player_dev_writer, autocommit=True) as qconn:
        qconn.execute("UPDATE public.fc27_discord_jobs SET available_at = clock_timestamp() - interval '1 second'")
    r_ok = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert r_ok.jobs_completed == 1
    assert r_ok.messages_edited == 1

    # 2. Permanent failures dead-letter immediately without retry:
    #    a) Unknown roster player
    #    b) Invalid roster revision / malformed payload
    #    c) Unauthorized guild/channel linkage
    #    d) Non-bot-authored message
    with psycopg.connect(pg_db_migrated.poll_writer, autocommit=True) as pw:
        pw.execute(
            """
            INSERT INTO public.fc27_discord_jobs (job_type, idempotency_key, payload, state)
            VALUES
              ('refresh_roster_messages', 'roster-refresh:plr_ecf_missing999:rev1:v1', %s::jsonb, 'pending'),
              ('refresh_roster_messages', 'bad-rev-key', '{"event_type":"refresh_roster_messages","player_id":"plr_ecf_aaaa1111aaaa","roster_revision":"-bad"}'::jsonb, 'pending')
            """,
            (
                json.dumps(
                    {
                        "schema_version": "fc27-roster-refresh-v1",
                        "event_type": "refresh_roster_messages",
                        "player_id": "plr_ecf_missing999",
                        "roster_revision": "rev1",
                        "reason_code": "ROSTER_UPSERT",
                        "requested_at": "2026-11-16T00:00:00Z",
                    }
                ),
            ),
        )
    r_dead = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert r_dead.jobs_failed == 2
    assert {e.error_category for e in r_dead.audit_entries} == {
        "UNKNOWN_ROSTER_PLAYER",
        "INVALID_ROSTER_REVISION",
    }

    # Non-bot authored check
    mref_nonbot = _seed_provider_match(
        pg_db_migrated,
        suffix="nonbot_15",
        played_at=datetime(2026, 10, 29, 1, 0, tzinfo=UTC),
    )
    run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    nb_links = get_match_discord_message_links(pg_db_migrated.discord_reader, match_ref=mref_nonbot)
    assert nb_links is not None and nb_links.results_message_id is not None
    transport.non_bot_message_ids.add(nb_links.results_message_id)

    rename_roster_player(
        pg_db_migrated.match_results_writer,
        player_id_or_name="RetryRecovered",
        new_name="NonBotBlocked",
    )
    r_nb = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert r_nb.jobs_failed == 1
    assert r_nb.audit_entries[0].error_category == "NON_BOT_AUTHORED_MESSAGE"

    # SerializationFailure (transient DB error) retries cleanly and succeeds on retry
    _seed_provider_match(
        pg_db_migrated,
        suffix="ser_15",
        played_at=datetime(2026, 10, 30, 1, 0, tzinfo=UTC),
    )
    run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    transport.non_bot_message_ids.clear()
    # Restore bot authorship on mref_nonbot before exercising transient SerializationFailure retry.
    rename_roster_player(
        pg_db_migrated.match_results_writer,
        player_id_or_name="NonBotBlocked",
        new_name="SerializationRetryOk",
    )
    ser_calls = {"n": 0}

    def _ser_flaky(*args: Any, **kwargs: Any) -> None:
        ser_calls["n"] += 1
        if ser_calls["n"] == 1:
            raise psycopg.errors.SerializationFailure("simulated serialization conflict")

    transport.on_before_edit = _ser_flaky
    r_ser = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    transport.on_before_edit = None
    assert r_ser.jobs_retried == 1
    assert r_ser.audit_entries[0].error_category == "DB_TRANSIENT_SERIALIZATION"
    with psycopg.connect(pg_db_migrated.player_dev_writer, autocommit=True) as qconn:
        qconn.execute("UPDATE public.fc27_discord_jobs SET available_at = clock_timestamp() - interval '1 second'")
    r_ser_ok = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert r_ser_ok.jobs_completed == 1

    # Shared formal & team-chat message ID in DB linkage -> AMBIGUOUS_MESSAGE_LINKAGE permanent failure
    with psycopg.connect(pg_db_migrated.admin, autocommit=True) as aconn:
        aconn.execute(
            """
            INSERT INTO public.fc27_manual_match_audit (
                match_id, action, actor_discord_user_id,
                discord_guild_id, discord_channel_id, discord_message_id,
                changed_fields, audit_metadata
            ) VALUES (
                NULL,
                'RELINK_DISCORD_MESSAGE',
                %s,
                %s,
                %s,
                '880000000000000099',
                '["results_message_id","team_chat_message_id"]'::jsonb,
                jsonb_build_object(
                    'kind', 'discord_message_linkage',
                    'match_ref', %s::text,
                    'results_channel_id', %s::text,
                    'results_message_id', '880000000000000099',
                    'team_chat_channel_id', %s::text,
                    'team_chat_message_id', '880000000000000099'
                )
            )
            """,
            (
                ADMIN_USER_ID,
                APPROVED_GUILD_ID,
                RESULTS_CHANNEL_ID,
                mref_nonbot,
                RESULTS_CHANNEL_ID,
                TEAM_CHAT_CHANNEL_ID,
            ),
        )
    rename_roster_player(
        pg_db_migrated.match_results_writer,
        player_id_or_name="SerializationRetryOk",
        new_name="AmbigSharedLink",
    )
    r_amb_link = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert r_amb_link.jobs_failed == 1
    assert r_amb_link.audit_entries[0].error_category == "AMBIGUOUS_MESSAGE_LINKAGE"


def test_16_lease_expiry_recovery_and_completed_job_never_reclaimed(pg_db_migrated) -> None:
    """24-25. Expired refresh lease recovers to pending and completes cleanly; completed job is terminal and never reclaimed."""
    _seed_provider_match(pg_db_migrated, suffix="lease_16")
    transport = SyntheticDiscordNotificationTransport()
    cfg = _dispatch_config()
    run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )

    upsert_roster_player(
        pg_db_migrated.match_results_writer,
        player_id=PLAYER_A,
        display_name="LeaseRecovered",
        position="ST",
    )
    with psycopg.connect(pg_db_migrated.player_dev_writer, autocommit=True) as qconn:
        qconn.execute(
            """
            UPDATE public.fc27_discord_jobs
            SET state = 'claimed',
                claim_owner = 'crashed-worker',
                claimed_at = clock_timestamp() - interval '10 seconds',
                lease_expires_at = clock_timestamp() + interval '150 milliseconds',
                attempt_count = 1
            WHERE job_type = 'refresh_roster_messages'
            """
        )
    time.sleep(0.25)

    r_rec = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert r_rec.expired_leases_recovered == 1
    assert r_rec.jobs_completed == 1
    assert r_rec.messages_edited == 1

    # Completed job is NEVER reclaimed across further cycles
    r_again = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert r_again.jobs_claimed == 0
    assert r_again.messages_edited == 0


def test_17_manual_and_provider_boundaries_and_unauthorized_destination_rejected(
    pg_db_migrated,
) -> None:
    """16, 36. Manual match linked message stays isolated from provider tables, and unauthorized channel/guild dead-letters without edit."""
    _seed_provider_match(pg_db_migrated, suffix="prov_bnd_17")
    upsert_roster_player(
        pg_db_migrated.match_results_writer,
        player_id=PLAYER_A,
        display_name="BoundaryPlayer",
        position="ST",
    )
    transport = SyntheticDiscordNotificationTransport()
    cfg = _dispatch_config()
    # Dispatch initial provider + upsert jobs cleanly BEFORE creating unauthorized manual linkage
    init_res = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert init_res.jobs_completed == 2

    manual = record_match_result(
        pg_db_migrated.match_results_writer,
        opponent="Manual FC",
        home_score=1,
        away_score=0,
        actor_discord_user_id=ADMIN_USER_ID,
        scorers="BoundaryPlayer x1",
        player_stats="BoundaryPlayer: 1 goal",
        discord_guild_id=APPROVED_GUILD_ID,
        discord_channel_id="999999999999999999",
        discord_message_id="777777777777777777",
    )
    record_match_discord_message_links(
        pg_db_migrated.match_results_writer,
        match_id=manual.match_id,
        discord_guild_id=APPROVED_GUILD_ID,
        results_channel_id="999999999999999999",
        results_message_id="777777777777777777",
    )

    rename_roster_player(
        pg_db_migrated.match_results_writer,
        player_id_or_name="BoundaryPlayer",
        new_name="BoundaryUpdated",
    )
    res = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert res.jobs_failed == 1
    assert res.audit_entries[0].error_category == "UNAUTHORIZED_DESTINATION"

    # Also verify mismatched guild linkage with an otherwise allowlisted channel fails closed
    record_match_discord_message_links(
        pg_db_migrated.match_results_writer,
        match_id=manual.match_id,
        discord_guild_id="999888777666555444",
        results_channel_id=RESULTS_CHANNEL_ID,
        results_message_id="777777777777777777",
    )
    rename_roster_player(
        pg_db_migrated.match_results_writer,
        player_id_or_name="BoundaryUpdated",
        new_name="BoundaryGuildMismatch",
    )
    res_guild = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert res_guild.jobs_failed == 1
    assert res_guild.audit_entries[0].error_category == "UNAUTHORIZED_DESTINATION"

    # Provider table untouched by manual result
    with psycopg.connect(pg_db_migrated.discord_reader) as rconn:
        cnt_row = rconn.execute("SELECT COUNT(*) FROM public.fc27_matches").fetchone()
        assert cnt_row is not None and cnt_row[0] == 1


def test_18_gateway_transport_snapshot_and_candidate_recovery() -> None:
    """Gateway transport snapshot distinguishes bot vs non-bot messages and detects ambiguous candidates."""

    @dataclass
    class _Author:
        id: int

    @dataclass
    class _EmbedField:
        name: str
        value: str

    @dataclass
    class _EmbedFooter:
        text: str

    @dataclass
    class _Embed:
        title: str
        description: str
        fields: list[_EmbedField]
        footer: _EmbedFooter
        color: int | None = None

    @dataclass
    class _Msg:
        id: int
        author: _Author
        content: str
        embeds: list[_Embed]
        guild: Any = None

    bot_user = _Author(id=111)

    class _FakeChan:
        def __init__(self, msgs: list[_Msg]) -> None:
            self._msgs = msgs
            self.guild = types.SimpleNamespace(id=int(APPROVED_GUILD_ID))

        async def send(self, *args: Any, **kwargs: Any) -> Any:
            return None

        async def fetch_message(self, msg_id: int) -> _Msg:
            for m in self._msgs:
                if m.id == msg_id:
                    return m
            raise KeyError(msg_id)

        def history(self, **kwargs: Any) -> Any:
            del kwargs
            msgs = list(self._msgs)

            class _Iter:
                def __init__(self) -> None:
                    self._idx = 0

                def __aiter__(self) -> Any:
                    return self

                async def __anext__(self) -> _Msg:
                    if self._idx >= len(msgs):
                        raise StopAsyncIteration
                    item = msgs[self._idx]
                    self._idx += 1
                    return item

            return _Iter()

    emb = _Embed(
        title="Win vs Rival",
        description="East Coasters",
        fields=[_EmbedField("📅 Played", "2026-10-24")],
        footer=_EmbedFooter("East Coasters FC • ref:mref_ecf_000011112222"),
    )
    chan = _FakeChan(
        [
            _Msg(id=501, author=bot_user, content="Update 1", embeds=[emb]),
            _Msg(id=502, author=bot_user, content="Update 2", embeds=[emb]),
            _Msg(id=503, author=_Author(id=999), content="User message", embeds=[]),
        ]
    )
    fake_client = types.SimpleNamespace(
        user=bot_user,
        get_channel=lambda cid: chan if str(cid) == RESULTS_CHANNEL_ID else None,
    )

    loop = asyncio.new_event_loop()
    try:
        async def _exercise() -> tuple[Any, Any, tuple[str, ...]]:
            transport = DiscordGatewayChannelTransport(fake_client, loop)
            cands = await transport._async_find_existing_candidates(
                channel_id=RESULTS_CHANNEL_ID,
                match_ref="mref_ecf_000011112222",
                not_before=None,
                max_candidates=2,
            )
            snap_bot = await transport._async_fetch_notification_snapshot(
                channel_id=RESULTS_CHANNEL_ID,
                message_id="501",
            )
            snap_user = await transport._async_fetch_notification_snapshot(
                channel_id=RESULTS_CHANNEL_ID,
                message_id="503",
            )
            return snap_bot, snap_user, cands

        fake_discord = types.SimpleNamespace(
            NotFound=KeyError,
            Forbidden=PermissionError,
            HTTPException=RuntimeError,
        )
        prev = sys.modules.get("discord")
        sys.modules["discord"] = fake_discord  # type: ignore[assignment]
        try:
            snap_bot, snap_user, cands = loop.run_until_complete(_exercise())
        finally:
            if prev is None:
                sys.modules.pop("discord", None)
            else:
                sys.modules["discord"] = prev
        assert snap_bot is not None and snap_bot.is_bot_authored is True
        assert snap_user is not None and snap_user.is_bot_authored is False
        assert len(cands) == 2
    finally:
        loop.close()


def test_19_manual_match_positive_in_place_refresh_formal_and_team_chat(pg_db_migrated) -> None:
    """Manual match formal embed + team-chat summary refresh in place without creating duplicates."""
    upsert_roster_player(
        pg_db_migrated.match_results_writer,
        player_id=PLAYER_A,
        display_name="ManualStar",
        position="ST",
    )
    cfg = _dispatch_config()
    transport = SyntheticDiscordNotificationTransport()
    # Drain initial upsert job cleanly
    r0 = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert r0.jobs_completed == 1

    manual = record_match_result(
        pg_db_migrated.match_results_writer,
        opponent="Manual Rivals FC",
        home_score=2,
        away_score=0,
        actor_discord_user_id=ADMIN_USER_ID,
        scorers="ManualStar x2",
        player_stats="ManualStar: 2 goals",
        discord_guild_id=APPROVED_GUILD_ID,
        discord_channel_id=RESULTS_CHANNEL_ID,
        discord_message_id="870000000000000101",
    )
    m_ref = f"manual:{manual.match_id}"
    res_msg_id = "870000000000000101"
    tc_msg_id = "890000000000000202"
    record_match_discord_message_links(
        pg_db_migrated.match_results_writer,
        match_id=manual.match_id,
        discord_guild_id=APPROVED_GUILD_ID,
        results_channel_id=RESULTS_CHANNEL_ID,
        results_message_id=res_msg_id,
        team_chat_channel_id=TEAM_CHAT_CHANNEL_ID,
        team_chat_message_id=tc_msg_id,
    )
    transport._channel_history.setdefault(RESULTS_CHANNEL_ID, []).append(
        (
            res_msg_id,
            FC27PostNewResultsMessage(
                channel_id=RESULTS_CHANNEL_ID,
                idempotency_key=f"fc27:manual:{manual.match_id}",
                match_ref=m_ref,
                content="Manual result announcement",
                embed=FC27DiscordEmbed(
                    title="Old Embed",
                    description="East Coasters FC",
                    fields={"Scorers": "ManualStar x2"},
                    footer_text="East Coasters FC",
                ),
            ),
        )
    )
    transport._team_chat_history.setdefault(TEAM_CHAT_CHANNEL_ID, []).append(
        (
            tc_msg_id,
            FC27TeamChatMessage(
                channel_id=TEAM_CHAT_CHANNEL_ID,
                idempotency_key=f"fc27:manual:{manual.match_id}",
                match_ref=m_ref,
                discriminator=build_match_post_tag(m_ref),
                content=f"ManualStar 2 goals [{build_match_post_tag(m_ref)}]",
            ),
        )
    )

    rename_roster_player(
        pg_db_migrated.match_results_writer,
        player_id_or_name="ManualStar",
        new_name="ManualLegend",
    )
    res_refresh = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert res_refresh.jobs_completed == 1
    assert res_refresh.messages_edited == 1
    assert res_refresh.team_chat_messages_edited == 1
    assert res_refresh.messages_sent == 0
    assert res_refresh.team_chat_messages_sent == 0
    edited_formal = transport.edited_messages[-1][2]
    assert edited_formal.content == "Manual result announcement"
    assert "ManualLegend" in json.dumps(edited_formal.embed.fields)
    edited_tc = transport.edited_team_chat_messages[-1][2]
    assert "ManualLegend" in edited_tc.content

    # Re-running with up-to-date snapshots skips both edits cleanly
    with psycopg.connect(pg_db_migrated.match_results_writer) as wconn:
        wconn.execute(
            "SELECT public.fc27_enqueue_roster_refresh_job(%s, %s, %s, clock_timestamp())",
            (PLAYER_A, "rev_manual_noop", "ROSTER_UPSERT"),
        )
        wconn.commit()
    res_noop = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert res_noop.jobs_completed == 1
    assert res_noop.messages_unchanged == 2
    assert res_noop.messages_edited == 0
    assert res_noop.team_chat_messages_edited == 0

    # Also verify gateway-style edit_embed / edit_text hooks are invoked for manual matches
    embed_calls: list[tuple[str, str, Any]] = []
    text_calls: list[tuple[str, str, str]] = []

    class _GatewayStyleTransport(SyntheticDiscordNotificationTransport):
        def edit_embed(self, channel_id: str, message_id: str, embed: Any) -> bool:
            embed_calls.append((channel_id, message_id, embed))
            return True

        def edit_text(self, channel_id: str, message_id: str, content: str) -> bool:
            text_calls.append((channel_id, message_id, content))
            return True

    gw_transport = _GatewayStyleTransport()
    gw_transport._channel_history[RESULTS_CHANNEL_ID] = list(transport._channel_history[RESULTS_CHANNEL_ID])
    gw_transport._team_chat_history[TEAM_CHAT_CHANNEL_ID] = list(transport._team_chat_history[TEAM_CHAT_CHANNEL_ID])
    rename_roster_player(
        pg_db_migrated.match_results_writer,
        player_id_or_name="ManualLegend",
        new_name="ManualIcon",
    )
    res_gw = run_discord_dispatch_cycle(
        config=cfg,
        transport=gw_transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert res_gw.jobs_completed == 1
    assert len(embed_calls) == 1
    assert embed_calls[0][1] == res_msg_id
    assert len(text_calls) == 1
    assert text_calls[0][1] == tc_msg_id
    assert "ManualIcon" in text_calls[0][2]


def test_20_unnamed_player_does_not_enqueue_and_rapid_upserts_immediately_claimable(
    pg_db_migrated,
) -> None:
    """Unresolved 'Unnamed Player' does not enqueue; rapid roster updates remain immediately claimable."""
    upsert_roster_player(
        pg_db_migrated.match_results_writer,
        player_id=PLAYER_A,
        display_name="Unnamed Player",
        position="ST",
    )
    assert _fetch_refresh_jobs(pg_db_migrated) == []

    # Updating position while name remains unresolved still does not enqueue
    upsert_roster_player(
        pg_db_migrated.match_results_writer,
        player_id=PLAYER_A,
        display_name="Unnamed Player",
        position="LW",
    )
    assert _fetch_refresh_jobs(pg_db_migrated) == []

    # Resolving the name mapping to a real display name enqueues immediately
    upsert_roster_player(
        pg_db_migrated.match_results_writer,
        player_id=PLAYER_A,
        display_name="ResolvedAce",
        position="LW",
    )
    for idx in range(5):
        rename_roster_player(
            pg_db_migrated.match_results_writer,
            player_id_or_name=PLAYER_A,
            new_name=f"RapidName{idx}",
        )

    jobs = _fetch_refresh_jobs(pg_db_migrated)
    assert len(jobs) == 6
    with psycopg.connect(pg_db_migrated.discord_reader) as rconn:
        future_row = rconn.execute(
            """
            SELECT COUNT(*)
            FROM public.fc27_discord_jobs
            WHERE job_type = 'refresh_roster_messages'
              AND available_at > pg_catalog.clock_timestamp()
            """
        ).fetchone()
    assert future_row is not None and future_row[0] == 0


def test_21_gateway_snapshot_fails_closed_without_bot_or_author_id() -> None:
    """_async_fetch_notification_snapshot must return is_bot_authored=False when bot_id or author_id is missing."""

    @dataclass
    class _Msg:
        id: int
        author: Any
        content: str = "hello"
        embeds: tuple[Any, ...] = ()
        guild: Any = None

    class _FakeChan:
        def __init__(self, msgs: dict[int, _Msg]) -> None:
            self._msgs = msgs
            self.guild = types.SimpleNamespace(id=int(APPROVED_GUILD_ID))

        async def send(self, *args: Any, **kwargs: Any) -> Any:
            return None

        async def fetch_message(self, msg_id: int) -> _Msg:
            return self._msgs[msg_id]

    msgs = {
        601: _Msg(id=601, author=types.SimpleNamespace(id=111)),
        602: _Msg(id=602, author=None),
    }
    chan = _FakeChan(msgs)
    fake_client = types.SimpleNamespace(
        user=None,
        get_channel=lambda cid: chan if str(cid) == RESULTS_CHANNEL_ID else None,
    )
    loop = asyncio.new_event_loop()
    try:
        fake_discord = types.SimpleNamespace(
            NotFound=KeyError,
            Forbidden=PermissionError,
            HTTPException=RuntimeError,
        )
        prev = sys.modules.get("discord")
        sys.modules["discord"] = fake_discord  # type: ignore[assignment]
        try:
            transport = DiscordGatewayChannelTransport(fake_client, loop)
            snap_no_bot = loop.run_until_complete(
                transport._async_fetch_notification_snapshot(
                    channel_id=RESULTS_CHANNEL_ID,
                    message_id="601",
                )
            )
            fake_client.user = types.SimpleNamespace(id=111)
            snap_no_author = loop.run_until_complete(
                transport._async_fetch_notification_snapshot(
                    channel_id=RESULTS_CHANNEL_ID,
                    message_id="602",
                )
            )
        finally:
            if prev is None:
                sys.modules.pop("discord", None)
            else:
                sys.modules["discord"] = prev
        assert snap_no_bot is not None and snap_no_bot.is_bot_authored is False
        assert snap_no_author is not None and snap_no_author.is_bot_authored is False
    finally:
        loop.close()


def test_22_zero_scan_cap_and_deferred_verified_linkage_persistence(pg_db_migrated) -> None:
    """refresh_max_history_scans_per_job=0 skips scans without stalling; unverified recovery does not persist; verified cache persists even on unchanged snapshot."""
    mref = _seed_provider_match(pg_db_migrated, suffix="defer_22")
    transport = SyntheticDiscordNotificationTransport()
    cfg = _dispatch_config()
    # Post without writer URI so DB links are absent initially
    r_init = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=None,
    )
    assert r_init.jobs_completed == 1
    posted_res_id = transport._channel_history[RESULTS_CHANNEL_ID][0][0]
    posted_tc_id = transport._team_chat_history[TEAM_CHAT_CHANNEL_ID][0][0]

    # 1) With scans_per_job=0 and cleared cache, completes safely without stalling
    transport._linkages.clear()
    upsert_roster_player(
        pg_db_migrated.match_results_writer,
        player_id=PLAYER_A,
        display_name="ZeroScanKai",
        position="ST",
    )
    r_zero = run_discord_dispatch_cycle(
        config=_dispatch_config(refresh_max_history_scans_per_job=0),
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert r_zero.jobs_completed == 1
    assert r_zero.jobs_failed == 0
    assert r_zero.messages_edited == 0

    # 2) Recovered candidate failing snapshot verification (wrong guild) fails BEFORE persisting linkage
    transport.message_guilds[posted_res_id] = "999888777666555444"
    transport.message_guilds[posted_tc_id] = "999888777666555444"
    rename_roster_player(
        pg_db_migrated.match_results_writer,
        player_id_or_name="ZeroScanKai",
        new_name="RejectedRecoveryKai",
    )
    r_bad_rec = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert r_bad_rec.jobs_failed == 1
    assert r_bad_rec.audit_entries[0].error_category == "UNAUTHORIZED_DESTINATION"
    db_after_rej = get_match_discord_message_links(pg_db_migrated.discord_reader, match_ref=mref)
    assert db_after_rej is None or db_after_rej.results_message_id is None

    # 3) Verified cached linkage IS persisted even when snapshot already matches (needs_*_edit=False)
    transport.message_guilds.clear()
    transport._linkages.clear()
    # First edit transport messages (without DB writer) so Discord snapshots already match RejectedRecoveryKai
    with psycopg.connect(pg_db_migrated.match_results_writer) as wconn:
        wconn.execute(
            "SELECT public.fc27_enqueue_roster_refresh_job(%s, %s, %s, clock_timestamp())",
            (PLAYER_A, "rev_sync_transport_only", "ROSTER_UPSERT"),
        )
        wconn.commit()
    r_sync = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=None,
    )
    assert r_sync.jobs_completed == 1
    assert get_match_discord_message_links(pg_db_migrated.discord_reader, match_ref=mref) is None

    # Cache already knows posted_res_id & posted_tc_id, snapshots match (unchanged), writer URI now supplied
    transport.record_match_linkage(
        match_ref=mref,
        results_channel_id=RESULTS_CHANNEL_ID,
        results_message_id=posted_res_id,
        team_chat_channel_id=TEAM_CHAT_CHANNEL_ID,
        team_chat_message_id=posted_tc_id,
    )
    with psycopg.connect(pg_db_migrated.match_results_writer) as wconn:
        wconn.execute(
            "SELECT public.fc27_enqueue_roster_refresh_job(%s, %s, %s, clock_timestamp())",
            (PLAYER_A, "rev_persist_unchanged_cache", "ROSTER_UPSERT"),
        )
        wconn.commit()
    r_persist = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert r_persist.jobs_completed == 1
    assert r_persist.messages_unchanged == 2
    persisted = get_match_discord_message_links(pg_db_migrated.discord_reader, match_ref=mref)
    assert persisted is not None
    assert persisted.results_message_id == posted_res_id
    assert persisted.team_chat_message_id == posted_tc_id


def test_23_partial_permanent_error_refreshes_healthy_sibling_matches(pg_db_migrated) -> None:
    """Permanent failure on one match's linked message still refreshes healthy sibling matches in the same job."""
    mref_bad = _seed_provider_match(
        pg_db_migrated,
        suffix="bad_23",
        opponent="Bad Sibling FC",
        played_at=datetime(2026, 10, 20, 1, 0, tzinfo=UTC),
    )
    mref_ok = _seed_provider_match(
        pg_db_migrated,
        suffix="ok_23",
        opponent="Good Sibling FC",
        played_at=datetime(2026, 10, 21, 1, 0, tzinfo=UTC),
    )
    transport = SyntheticDiscordNotificationTransport()
    cfg = _dispatch_config()
    init_res = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert init_res.jobs_completed == 2

    bad_links = get_match_discord_message_links(pg_db_migrated.discord_reader, match_ref=mref_bad)
    ok_links = get_match_discord_message_links(pg_db_migrated.discord_reader, match_ref=mref_ok)
    assert bad_links is not None and bad_links.results_message_id is not None
    assert ok_links is not None and ok_links.results_message_id is not None
    transport.deleted_message_ids.add(bad_links.results_message_id)

    upsert_roster_player(
        pg_db_migrated.match_results_writer,
        player_id=PLAYER_A,
        display_name="HealthySiblingStar",
        position="ST",
    )
    res = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert res.jobs_failed == 1
    assert res.audit_entries[0].error_category == "LINKED_MESSAGE_MISSING"
    assert res.messages_edited == 1
    assert res.team_chat_messages_edited == 2
    edited_ids = {msg_id for _, msg_id, _ in transport.edited_messages}
    assert ok_links.results_message_id in edited_ids


def test_24_lease_loss_during_completion_rolls_back_continuation(pg_db_migrated) -> None:
    """Losing the job lease right before completion rolls back continuation enqueue atomically."""
    _seed_provider_match(
        pg_db_migrated,
        suffix="ll_1",
        played_at=datetime(2026, 10, 20, 1, 0, tzinfo=UTC),
    )
    _seed_provider_match(
        pg_db_migrated,
        suffix="ll_2",
        played_at=datetime(2026, 10, 21, 1, 0, tzinfo=UTC),
    )
    transport = SyntheticDiscordNotificationTransport()
    init_res = run_discord_dispatch_cycle(
        config=_dispatch_config(),
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert init_res.jobs_completed == 2

    upsert_roster_player(
        pg_db_migrated.match_results_writer,
        player_id=PLAYER_A,
        display_name="LeaseLossCheck",
        position="ST",
    )

    def _steal_lease_on_first_edit(channel_id: str, message_id: str, msg: Any) -> None:
        del channel_id, message_id, msg
        with psycopg.connect(pg_db_migrated.player_dev_writer, autocommit=True) as thief:
            thief.execute(
                """
                UPDATE public.fc27_discord_jobs
                SET claim_owner = 'stolen-owner',
                    lease_expires_at = clock_timestamp() + interval '5 minutes'
                WHERE state = 'claimed'
                  AND job_type = 'refresh_roster_messages'
                """
            )

    transport.on_before_team_chat_edit = _steal_lease_on_first_edit
    res = run_discord_dispatch_cycle(
        config=_dispatch_config(refresh_max_matches_per_job=1),
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    transport.on_before_team_chat_edit = None
    assert res.continuations_enqueued == 0
    assert any(a.action == "LEASE_LOST" for a in res.audit_entries)

    jobs = _fetch_refresh_jobs(pg_db_migrated)
    cont_jobs = [j for j in jobs if ":cont:" in j["idempotency_key"]]
    assert cont_jobs == []


def test_25_scan_cap_one_does_not_stall_on_first_match(pg_db_migrated) -> None:
    """refresh_max_history_scans_per_job=1 completes first unlinked match formal scan without dead-lettering."""
    mref = _seed_provider_match(pg_db_migrated, suffix="scan1_25")
    transport = SyntheticDiscordNotificationTransport()
    r_init = run_discord_dispatch_cycle(
        config=_dispatch_config(),
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=None,
    )
    assert r_init.jobs_completed == 1

    transport._linkages.clear()
    upsert_roster_player(
        pg_db_migrated.match_results_writer,
        player_id=PLAYER_A,
        display_name="ScanOneStar",
        position="ST",
    )
    res = run_discord_dispatch_cycle(
        config=_dispatch_config(refresh_max_history_scans_per_job=1),
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert res.jobs_completed == 1
    assert res.jobs_failed == 0
    assert res.messages_edited == 1
    assert res.team_chat_messages_edited == 0
    links = get_match_discord_message_links(pg_db_migrated.discord_reader, match_ref=mref)
    assert links is not None and links.results_message_id is not None


def test_26_default_scan_budget_continuation_refreshes_all_formal_and_team_chat(
    pg_db_migrated,
) -> None:
    """Default refresh_max_history_scans_per_job=5 with 3 unlinked matches continues cleanly so all team-chat cards refresh too."""
    for idx in range(3):
        _seed_provider_match(
            pg_db_migrated,
            suffix=f"scan_cont_{idx}",
            opponent=f"ScanClub{idx}",
            played_at=datetime(2026, 11, 1 + idx, 1, 0, tzinfo=UTC),
        )
    transport = SyntheticDiscordNotificationTransport()
    cfg = _dispatch_config()
    assert cfg.refresh_max_history_scans_per_job == 5
    r_init = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=None,
    )
    assert r_init.jobs_completed == 3
    transport._linkages.clear()

    upsert_roster_player(
        pg_db_migrated.match_results_writer,
        player_id=PLAYER_A,
        display_name="DefaultBudgetStar",
        position="ST",
    )
    c1 = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert c1.jobs_completed == 1
    assert c1.continuations_enqueued == 1
    assert c1.messages_edited == 3
    assert c1.team_chat_messages_edited == 2

    c2 = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert c2.jobs_completed == 1
    assert c2.continuations_enqueued == 0
    assert c2.messages_edited == 0
    assert c2.team_chat_messages_edited == 1
    assert len({mid for _, mid, _ in transport.edited_messages}) == 3
    assert len({mid for _, mid, _ in transport.edited_team_chat_messages}) == 3


def test_27_manual_match_table_linkage_preserved_when_team_chat_persisted(
    pg_db_migrated,
) -> None:
    """Persisting cached team-chat linkage on a manual match must preserve formal results_message_id from fc27_manual_matches."""
    upsert_roster_player(
        pg_db_migrated.match_results_writer,
        player_id=PLAYER_A,
        display_name="ManualPreserveInit",
        position="ST",
    )
    cfg = _dispatch_config()
    transport = SyntheticDiscordNotificationTransport()
    run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )

    formal_msg_id = "870000000000000911"
    tc_msg_id = "890000000000000912"
    manual = record_match_result(
        pg_db_migrated.match_results_writer,
        opponent="Manual Table Link FC",
        home_score=3,
        away_score=1,
        actor_discord_user_id=ADMIN_USER_ID,
        scorers="ManualPreserveInit x1",
        player_stats="ManualPreserveInit: 1 goal",
        discord_guild_id=APPROVED_GUILD_ID,
        discord_channel_id=RESULTS_CHANNEL_ID,
        discord_message_id=formal_msg_id,
    )
    m_ref = f"manual:{manual.match_id}"
    transport.record_match_linkage(
        match_ref=m_ref,
        team_chat_channel_id=TEAM_CHAT_CHANNEL_ID,
        team_chat_message_id=tc_msg_id,
    )
    transport._channel_history.setdefault(RESULTS_CHANNEL_ID, []).append(
        (
            formal_msg_id,
            FC27PostNewResultsMessage(
                channel_id=RESULTS_CHANNEL_ID,
                idempotency_key=f"fc27:manual:{manual.match_id}",
                match_ref=m_ref,
                content="Manual result announcement",
                embed=FC27DiscordEmbed(
                    title="Old Manual Embed",
                    description="East Coasters FC",
                    fields={"Scorers": "ManualPreserveInit x1"},
                    footer_text="East Coasters FC",
                ),
            ),
        )
    )
    transport._team_chat_history.setdefault(TEAM_CHAT_CHANNEL_ID, []).append(
        (
            tc_msg_id,
            FC27TeamChatMessage(
                channel_id=TEAM_CHAT_CHANNEL_ID,
                idempotency_key=f"fc27:manual:{manual.match_id}",
                match_ref=m_ref,
                discriminator=build_match_post_tag(m_ref),
                content=f"ManualPreserveInit 1 goal [{build_match_post_tag(m_ref)}]",
            ),
        )
    )

    rename_roster_player(
        pg_db_migrated.match_results_writer,
        player_id_or_name="ManualPreserveInit",
        new_name="ManualPreserveAfter",
    )
    res = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert res.jobs_completed == 1
    assert res.messages_edited == 1
    assert res.team_chat_messages_edited == 1

    links = get_match_discord_message_links(
        pg_db_migrated.discord_reader, match_id=manual.match_id
    )
    assert links is not None
    assert links.discord_guild_id == APPROVED_GUILD_ID
    assert links.results_channel_id == RESULTS_CHANNEL_ID
    assert links.results_message_id == formal_msg_id
    assert links.team_chat_channel_id == TEAM_CHAT_CHANNEL_ID
    assert links.team_chat_message_id == tc_msg_id


def test_28_retry_exhaustion_dead_letters_and_shared_destination_gateway(
    pg_db_migrated,
) -> None:
    """Exhausted transient retries dead-letter to failed; shared results/team-chat channel gateway recovery separates both message types."""
    mref = _seed_provider_match(pg_db_migrated, suffix="exhaust_28")
    transport = SyntheticDiscordNotificationTransport()
    cfg_exhaust = _dispatch_config(max_attempts=2, retry_backoff_seconds=0)
    run_discord_dispatch_cycle(
        config=cfg_exhaust,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )

    upsert_roster_player(
        pg_db_migrated.match_results_writer,
        player_id=PLAYER_A,
        display_name="ExhaustStar",
        position="ST",
    )

    def _always_503(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise FC27DiscordDispatchTransportError(
            "upstream 503",
            category="DISCORD_TRANSIENT_FAILURE",
            retryable=True,
        )

    transport.on_before_edit = _always_503
    r1 = run_discord_dispatch_cycle(
        config=cfg_exhaust,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert r1.jobs_retried == 1
    assert r1.jobs_failed == 0

    with psycopg.connect(pg_db_migrated.player_dev_writer, autocommit=True) as qconn:
        qconn.execute("UPDATE public.fc27_discord_jobs SET available_at = clock_timestamp() - interval '1 second'")

    r2 = run_discord_dispatch_cycle(
        config=cfg_exhaust,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    transport.on_before_edit = None
    assert r2.jobs_failed == 1
    assert r2.messages_sent == 0
    assert r2.audit_entries[0].error_category == "DISCORD_TRANSIENT_FAILURE"

    refresh_jobs = [
        j for j in _fetch_refresh_jobs(pg_db_migrated) if j["job_type"] == REFRESH_ROSTER_MESSAGES_EVENT_TYPE
    ]
    assert len(refresh_jobs) == 1
    assert refresh_jobs[0]["state"] == "failed"
    assert refresh_jobs[0]["attempt_count"] == 2

    # Shared-destination gateway history candidate recovery distinguishes formal embed vs team-chat text in one channel
    @dataclass
    class _Author:
        id: int

    @dataclass
    class _EmbedField:
        name: str
        value: str

    @dataclass
    class _EmbedFooter:
        text: str

    @dataclass
    class _Embed:
        title: str
        description: str
        fields: list[_EmbedField]
        footer: _EmbedFooter

    @dataclass
    class _Msg:
        id: int
        author: _Author
        content: str
        embeds: list[_Embed]
        guild: Any = None

    bot_user = _Author(id=111)
    tag = build_match_post_tag(mref)
    shared_msgs = [
        _Msg(
            id=901,
            author=bot_user,
            content="✅ FULL TIME — East Coasters 2-1 Harbor City FC",
            embeds=[
                _Embed(
                    title="⚽ FULL TIME — WIN 2-1 vs Harbor City FC",
                    description="East Coasters",
                    fields=[_EmbedField("📅 Played", "2026-10-23")],
                    footer=_EmbedFooter(f"East Coasters FC • ref:{mref}"),
                )
            ],
        ),
        _Msg(
            id=902,
            author=bot_user,
            content=tag_match_post_content(
                "East Coasters FC: Win 2-1 vs Harbor City FC", seed=mref
            ),
            embeds=[],
        ),
    ]

    class _SharedChan:
        def __init__(self) -> None:
            self.guild = types.SimpleNamespace(id=int(APPROVED_GUILD_ID))

        async def send(self, *args: Any, **kwargs: Any) -> Any:
            return None

        def history(self, **kwargs: Any) -> Any:
            del kwargs

            class _Iter:
                def __init__(self) -> None:
                    self._idx = 0

                def __aiter__(self) -> Any:
                    return self

                async def __anext__(self) -> _Msg:
                    if self._idx >= len(shared_msgs):
                        raise StopAsyncIteration
                    item = shared_msgs[self._idx]
                    self._idx += 1
                    return item

            return _Iter()

    shared_chan = _SharedChan()
    fake_client = types.SimpleNamespace(
        user=bot_user,
        get_channel=lambda cid: shared_chan if str(cid) == RESULTS_CHANNEL_ID else None,
    )
    loop = asyncio.new_event_loop()
    try:
        async def _check_shared() -> tuple[tuple[str, ...], tuple[str, ...]]:
            gw = DiscordGatewayChannelTransport(fake_client, loop)
            f_cands = await gw._async_find_existing_candidates(
                channel_id=RESULTS_CHANNEL_ID,
                match_ref=mref,
                not_before=None,
                max_candidates=2,
            )
            tc_cands = await gw._async_find_existing_team_chat_candidates(
                channel_id=RESULTS_CHANNEL_ID,
                match_ref=mref,
                not_before=None,
                message=FC27TeamChatMessage(
                    channel_id=RESULTS_CHANNEL_ID,
                    idempotency_key="k",
                    match_ref=mref,
                    discriminator=tag,
                    content=tag_match_post_content("Different body", seed=mref),
                ),
                max_candidates=2,
            )
            return f_cands, tc_cands

        fake_discord = types.SimpleNamespace(
            NotFound=KeyError,
            Forbidden=PermissionError,
            HTTPException=RuntimeError,
        )
        prev = sys.modules.get("discord")
        sys.modules["discord"] = fake_discord  # type: ignore[assignment]
        try:
            f_cands, tc_cands = loop.run_until_complete(_check_shared())
        finally:
            if prev is None:
                sys.modules.pop("discord", None)
            else:
                sys.modules["discord"] = prev
        assert f_cands == ("901",)
        assert tc_cands == ("902",)
    finally:
        loop.close()
