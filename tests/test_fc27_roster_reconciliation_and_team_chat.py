"""Comprehensive test suite for FC27 Roster Reconciliation and Team-Chat Enhancement (Phase 9)."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest

from src import settings
from src.db_migrate import migrate, verify_postconditions
from src.db_roles import GRANTS_POLICY_VERSION, ROLE_DISCORD_READER, ROLE_MATCH_RESULTS_WRITER
from src.fc27_discord_bot import (
    DISCORD_PROVIDER_REQUESTS,
    FC27DiscordAuthzError,
    FC27DiscordBotConfig,
    FC27DiscordBotService,
    PERMISSION_DENIED_MESSAGE,
    assert_discord_output_sanitized,
)
from src.fc27_discord_dispatcher import (
    DISPATCH_PROVIDER_REQUESTS,
    FC27AuthoritativeMatchData,
    FC27AuthoritativePlayerSummary,
    FC27DiscordDispatchConfig,
    FC27TeamChatMessage,
    SyntheticDiscordNotificationTransport,
    build_post_new_results_notification,
    build_post_new_results_team_chat_message,
    claim_discord_jobs_batch,
    load_authoritative_match_for_job,
    refresh_dispatched_match_message,
    run_discord_dispatch_cycle,
    try_acquire_dispatch_advisory_lock,
    release_dispatch_advisory_lock,
    GuardedDiscordJobQueueConnection,
)
from src.fc27_match_results import (
    DEFAULT_RECONCILIATION_TOKEN_STORE,
    UNRESOLVED_PLAYER_DISPLAY_NAME,
    InMemoryDiscordChannelAdapter,
    ManualMatchDetail,
    ManualMatchPlayerDetail,
    MatchResultsNotFoundError,
    MatchResultsPermissionError,
    MatchResultsPostureError,
    MatchResultsValidationError,
    ParsedScorerEntry,
    ReconciliationTokenStore,
    build_match_post_tag,
    build_result_content,
    build_result_embed,
    build_team_chat_result_summary,
    compute_alias_hmac_fingerprint,
    extract_match_post_tag,
    format_unresolved_players_ephemeral,
    get_match_discord_message_links,
    list_unresolved_players,
    main as match_results_cli_main,
    record_match_discord_message_links,
    record_match_result,
    redact_internal_player_refs,
    render_unresolved_players_embed,
    resolve_player_names,
    tag_match_post_content,
    upsert_roster_player,
)
from src.fc27_privacy import (
    EAST_COASTERS_CLUB_ID,
    EAST_COASTERS_GAME_VERSION,
    EAST_COASTERS_PLATFORM,
    pseudonymize_match_ref,
)
from src.fc27_repository import FC27PostNewResultsPayload

APPROVED_GUILD_ID = "123456789012345678"
APPROVED_CHANNEL_ID = "234567890123456789"
RESULTS_CHANNEL_ID = "234567890123456789"
TEAM_CHAT_CHANNEL_ID = "234567890123456790"
MANAGER_ROLE_ID = "444444444444444444"
CAPTAIN_ROLE_ID = "555555555555555555"
ADMIN_ROLE_ID = "666666666666666666"
REGULAR_ROLE_ID = "777777777777777777"
ADMIN_USER_ID = "345678901234567890"
MEMBER_USER_ID = "888888888888888888"

SAMPLE_REFS: tuple[str, ...] = (
    "plr_ecf_111111111111",
    "plr_ecf_222222222222",
    "plr_ecf_333333333333",
    "plr_ecf_444444444444",
    "plr_ecf_555555555555",
    "plr_ecf_666666666666",
)


def _bot_config(**overrides: Any) -> FC27DiscordBotConfig:
    params: dict[str, Any] = {
        "enabled": True,
        "allowed_guild_ids": frozenset({APPROVED_GUILD_ID}),
        "allowed_channel_ids": frozenset({APPROVED_CHANNEL_ID, TEAM_CHAT_CHANNEL_ID}),
        "manager_role_ids": frozenset({MANAGER_ROLE_ID}),
        "captain_role_ids": frozenset({CAPTAIN_ROLE_ID}),
        "admin_role_ids": frozenset({ADMIN_ROLE_ID}),
        "results_channel_id": RESULTS_CHANNEL_ID,
        "team_chat_channel_id": TEAM_CHAT_CHANNEL_ID,
        "max_user_commands_per_minute": 60,
        "max_guild_commands_per_minute": 120,
    }
    params.update(overrides)
    return FC27DiscordBotConfig(**params)


def _dispatch_config(**overrides: Any) -> FC27DiscordDispatchConfig:
    params: dict[str, Any] = {
        "enabled": True,
        "results_channel_id": RESULTS_CHANNEL_ID,
        "team_chat_channel_id": TEAM_CHAT_CHANNEL_ID,
        "allowed_channel_ids": frozenset({RESULTS_CHANNEL_ID, TEAM_CHAT_CHANNEL_ID}),
        "max_jobs_per_cycle": 5,
        "lease_minutes": 5,
        "single_flight": True,
    }
    params.update(overrides)
    return FC27DiscordDispatchConfig(**params)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _result_letter(goals_for: int, goals_against: int) -> str:
    if goals_for > goals_against:
        return "WIN"
    if goals_for < goals_against:
        return "LOSS"
    return "DRAW"


def _seed_provider_match_and_job(
    db_uris: Any,
    *,
    external_suffix: str,
    opponent_name: str = "Harbor City FC",
    goals_for: int = 1,
    goals_against: int = 2,
    match_type: str = "playoffMatch",
    played_at: datetime | None = None,
    formation: str = "4-3-3",
    players: list[dict[str, Any]] | None = None,
    enqueue_job: bool = True,
) -> dict[str, Any]:
    """Insert an export batch, authoritative match, player stats, and optional pending job."""
    now = played_at or datetime(2026, 10, 22, 2, 19, tzinfo=timezone.utc)
    export_id = f"exp_{external_suffix}"
    raw_ext_id = f"ext_{external_suffix}"
    match_ref = pseudonymize_match_ref(raw_ext_id, club_id=EAST_COASTERS_CLUB_ID)
    result_code = _result_letter(goals_for, goals_against)

    default_players = players or [
        {
            "ref": SAMPLE_REFS[0],
            "position": "CM",
            "goals": 0,
            "assists": 1,
            "passes_attempted": 24,
            "passes_completed": 20,
            "tackles_attempted": 4,
            "tackles_won": 3,
            "shots": 0,
            "shots_on_target": 0,
            "rating": Decimal("8.20"),
        },
        {
            "ref": SAMPLE_REFS[1],
            "position": "CM",
            "goals": 0,
            "assists": 0,
            "passes_attempted": 25,
            "passes_completed": 21,
            "tackles_attempted": 2,
            "tackles_won": 1,
            "shots": 0,
            "shots_on_target": 0,
            "rating": Decimal("7.10"),
        },
        {
            "ref": SAMPLE_REFS[2],
            "position": "ST",
            "goals": 1,
            "assists": 0,
            "passes_attempted": 10,
            "passes_completed": 7,
            "tackles_attempted": 0,
            "tackles_won": 0,
            "shots": 2,
            "shots_on_target": 1,
            "rating": Decimal("6.90"),
        },
        {
            "ref": SAMPLE_REFS[3],
            "position": "CM",
            "goals": 0,
            "assists": 0,
            "passes_attempted": 21,
            "passes_completed": 16,
            "tackles_attempted": 1,
            "tackles_won": 1,
            "shots": 0,
            "shots_on_target": 0,
            "rating": Decimal("6.50"),
        },
        {
            "ref": SAMPLE_REFS[4],
            "position": "CB",
            "goals": 0,
            "assists": 0,
            "passes_attempted": 12,
            "passes_completed": 9,
            "tackles_attempted": 1,
            "tackles_won": 0,
            "shots": 0,
            "shots_on_target": 0,
            "rating": Decimal("6.40"),
        },
        {
            "ref": SAMPLE_REFS[5],
            "position": "GK",
            "goals": 0,
            "assists": 0,
            "passes_attempted": 12,
            "passes_completed": 8,
            "tackles_attempted": 0,
            "tackles_won": 0,
            "shots": 0,
            "shots_on_target": 0,
            "rating": Decimal("6.20"),
        },
    ]

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
                    'SYNTHETIC_ONLY', 'UNVERIFIED', 'SYNTHETIC',
                    %s, %s
                )
                ON CONFLICT (club_id, platform, game_version) DO NOTHING
                """,
                (
                    EAST_COASTERS_CLUB_ID,
                    EAST_COASTERS_PLATFORM,
                    EAST_COASTERS_GAME_VERSION,
                    now,
                    now,
                ),
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
                    %s, %s, %s, %s,
                    '1.0.0', '1.0.0', 'SYNTHETIC',
                    'CLUB_MANAGER', %s, '1.0.0',
                    '["MATCH_RESULTS"]'::jsonb, %s,
                    1, 1, 0,
                    %s, %s
                )
                """,
                (
                    export_id,
                    EAST_COASTERS_CLUB_ID,
                    EAST_COASTERS_PLATFORM,
                    EAST_COASTERS_GAME_VERSION,
                    f"ea-clubs-community-v1:{match_type}",
                    _sha256_hex(f"batch-{external_suffix}"),
                    now,
                    now,
                ),
            )
            cur.execute(
                """
                INSERT INTO public.fc27_matches (
                    club_id, platform, game_version, external_match_id,
                    export_id, played_at, result, goals_for, goals_against,
                    opponent_name, formation, match_status, match_hash,
                    source_classification, data_quality_status, created_at
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, 'COMPLETED', %s,
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
                    result_code,
                    goals_for,
                    goals_against,
                    opponent_name,
                    formation,
                    _sha256_hex(f"match-{external_suffix}"),
                    now,
                ),
            )
            match_row = cur.fetchone()
            assert match_row is not None
            match_uuid = match_row[0]
            for p in default_players:
                cur.execute(
                    """
                    INSERT INTO public.fc27_player_match_stats (
                        match_id, club_id, platform, game_version, external_match_id,
                        local_player_reference, display_alias, position, role,
                        minutes_played, goals, assists,
                        passes_attempted, passes_completed,
                        tackles_attempted, tackles_won,
                        interceptions, shots, shots_on_target,
                        saves, possession_losses,
                        provider_rating, build_reference, created_at
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        %s, NULL, %s, %s,
                        90, %s, %s,
                        %s, %s,
                        %s, %s,
                        0, %s, %s,
                        %s, 0,
                        %s, NULL, %s
                    )
                    """,
                    (
                        match_uuid,
                        EAST_COASTERS_CLUB_ID,
                        EAST_COASTERS_PLATFORM,
                        EAST_COASTERS_GAME_VERSION,
                        raw_ext_id,
                        p["ref"],
                        p["position"],
                        p.get("role"),
                        p["goals"],
                        p["assists"],
                        p["passes_attempted"],
                        p["passes_completed"],
                        p["tackles_attempted"],
                        p["tackles_won"],
                        p["shots"],
                        p["shots_on_target"],
                        1 if p["position"] == "GK" else 0,
                        p["rating"],
                        now,
                    ),
                )
            job_id = None
            idem_key = f"fc27:post_new_results:{match_ref}"
            payload = FC27PostNewResultsPayload(
                schema_version="fc27-post-new-results-v1",
                event_type="post_new_results",
                match_ref=match_ref,
                club_id=EAST_COASTERS_CLUB_ID,
                platform=EAST_COASTERS_PLATFORM,
                game_version=EAST_COASTERS_GAME_VERSION,
                match_type=match_type,
            ).to_payload()
            if enqueue_job:
                cur.execute(
                    """
                    INSERT INTO public.fc27_discord_jobs (
                        job_type, idempotency_key, payload, state, attempt_count,
                        available_at, created_at
                    ) VALUES (
                        'post_new_results', %s, %s::jsonb, 'pending', 0,
                        %s, %s
                    )
                    RETURNING job_id
                    """,
                    (idem_key, json.dumps(payload), now, now),
                )
                job_row = cur.fetchone()
                assert job_row is not None
                job_id = int(job_row[0])
        pw.commit()
    return {
        "match_uuid": str(match_uuid),
        "raw_ext_id": raw_ext_id,
        "match_ref": match_ref,
        "idempotency_key": idem_key,
        "payload": payload,
        "job_id": job_id,
    }


def _scorer(name: str, goals: int, order: int = 0) -> ParsedScorerEntry:
    return ParsedScorerEntry(
        normalized_display_label=name.strip().lower(),
        safe_display_label=name.strip(),
        goals=goals,
        display_order=order,
    )


def _manual_detail(
    *,
    opponent: str = "Test Rival FC",
    home_score: int = 1,
    away_score: int = 2,
    shots: int | None = 2,
    passes: int | None = 81,
    pass_accuracy: int | None = 78,
    tackles: int | None = 5,
    scorers: tuple[ParsedScorerEntry, ...] = (),
    players: tuple[ManualMatchPlayerDetail, ...] = (),
) -> ManualMatchDetail:
    ts = datetime(2026, 10, 22, 2, 19, tzinfo=timezone.utc)
    return ManualMatchDetail(
        match_id=42,
        club_id=EAST_COASTERS_CLUB_ID,
        platform=EAST_COASTERS_PLATFORM,
        game_version=EAST_COASTERS_GAME_VERSION,
        opponent=opponent,
        opponent_normalized=opponent.strip().lower(),
        status="FULL_TIME",
        kickoff_at=ts,
        competition="Playoff Match",
        formation="4-3-3",
        home_score=home_score,
        away_score=away_score,
        possession=None,
        shots=shots,
        shots_on_target=None,
        passes=passes,
        pass_accuracy=pass_accuracy,
        corners=None,
        tackles=tackles,
        fouls=None,
        created_by=ADMIN_USER_ID,
        updated_by=ADMIN_USER_ID,
        created_at=ts,
        updated_at=ts,
        scorers=scorers,
        players=players,
        revision=1,
    )


# ==============================================================================
# 1-8. ROSTER RECONCILIATION, SINGLE BATCH QUERY & FC GOATSSS PRESERVATION
# ==============================================================================


def test_01_verified_roster_mapping_replaces_unnamed_player(pg_db_migrated) -> None:
    """1. Verified roster mapping replaces Unnamed Player in formal notifications."""
    seeded = _seed_provider_match_and_job(pg_db_migrated, external_suffix="goat_01")
    upsert_roster_player(
        pg_db_migrated.match_results_writer,
        player_id=SAMPLE_REFS[0],
        display_name="Alex",
        position="CM",
    )
    upsert_roster_player(
        pg_db_migrated.match_results_writer,
        player_id=SAMPLE_REFS[2],
        display_name="Jordan",
        position="ST",
    )

    transport = SyntheticDiscordNotificationTransport()
    cycle = run_discord_dispatch_cycle(
        config=_dispatch_config(team_chat_channel_id=None),
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
    )
    assert cycle.messages_sent == 1
    msg = transport.sent_messages[0]
    rendered = "\n".join([msg.content, msg.embed.title, msg.embed.description, *msg.embed.fields.values()])
    assert "Alex (CM)" in rendered
    assert "Jordan (ST)" in rendered
    assert "Jordan x1" in rendered


def test_02_missing_mapping_still_produces_unnamed_player(pg_db_migrated) -> None:
    """2. Unmapped player references continue rendering 'Unnamed Player' without guessing."""
    _seed_provider_match_and_job(pg_db_migrated, external_suffix="goat_02")
    transport = SyntheticDiscordNotificationTransport()
    cycle = run_discord_dispatch_cycle(
        config=_dispatch_config(team_chat_channel_id=None),
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
    )
    assert cycle.messages_sent == 1
    fields = transport.sent_messages[0].embed.fields
    assert fields["⚽ Scorers"] == "Unnamed Player x1"
    assert fields["👥 Players"].count("Unnamed Player") == 6


def test_03_multiple_unresolved_players_stay_distinct_internally(pg_db_migrated) -> None:
    """3. Multiple unresolved players remain distinct rows and receive distinct tokens T1..Tn."""
    seeded = _seed_provider_match_and_job(pg_db_migrated, external_suffix="distinct_03")
    from src.fc27_discord_bot import GuardedDiscordReadOnlyConnection

    with psycopg.connect(pg_db_migrated.discord_reader) as rconn:
        guarded = GuardedDiscordReadOnlyConnection(rconn)
        match_data, _ = load_authoritative_match_for_job(
            guarded,
            raw_payload=seeded["payload"],
            idempotency_key=seeded["idempotency_key"],
        )
        rconn.commit()
    assert len(match_data.players) == 6
    assert len({p.local_player_reference for p in match_data.players}) == 6
    assert all(p.display_name == UNRESOLVED_PLAYER_DISPLAY_NAME for p in match_data.players)

    store = ReconciliationTokenStore()
    entries = list_unresolved_players(
        pg_db_migrated.discord_reader,
        token_store=store,
        actor_discord_user_id=ADMIN_USER_ID,
    )
    assert len(entries) == 6
    assert [e.token for e in entries] == ["T1", "T2", "T3", "T4", "T5", "T6"]
    assert len({e.player_id for e in entries}) == 6


def test_04_scorer_and_player_list_resolve_consistently(pg_db_migrated) -> None:
    """4. Verified scorer resolves identically in Scorers, Players list, and team-chat Goals."""
    seeded = _seed_provider_match_and_job(pg_db_migrated, external_suffix="scorer_04")
    upsert_roster_player(
        pg_db_migrated.match_results_writer,
        player_id=SAMPLE_REFS[2],
        display_name="Marcus",
        position="ST",
    )
    transport = SyntheticDiscordNotificationTransport()
    run_discord_dispatch_cycle(
        config=_dispatch_config(),
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    formal = transport.sent_messages[0]
    assert formal.embed.fields["⚽ Scorers"] == "Marcus x1"
    assert "Marcus (ST) — 1G" in formal.embed.fields["👥 Players"]
    assert "Goals: Marcus x1" in transport.sent_team_chat_messages[0].content


def test_05_no_internal_player_id_appears_publicly(pg_db_migrated) -> None:
    """5. Internal plr_ecf_* and mref_ecf_* references never appear in Discord outputs or admin tokens."""
    _seed_provider_match_and_job(pg_db_migrated, external_suffix="privacy_05")
    store = ReconciliationTokenStore()
    service = FC27DiscordBotService(
        _bot_config(),
        db_conn_or_uri=pg_db_migrated.discord_reader,
        writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
        reconciliation_token_store=store,
    )
    resp_list = service.handle_slash_command(
        "unresolvedplayers",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=ADMIN_USER_ID,
        user_role_ids=[ADMIN_ROLE_ID],
    )
    assert resp_list.status == "OK"
    assert resp_list.ephemeral is True
    list_blob = resp_list.content + resp_list.embeds[0].description + "".join(resp_list.embeds[0].fields.values())
    assert "plr_ecf_" not in list_blob
    assert "mref_ecf_" not in list_blob
    assert "T1" in list_blob

    resp_set = service.handle_slash_command(
        "setplayer",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=ADMIN_USER_ID,
        user_role_ids=[ADMIN_ROLE_ID],
        player_id="T1",
        player_name="Devin",
        position="CM",
    )
    assert resp_set.status == "OK"
    set_blob = resp_set.content + "".join(resp_set.embeds[0].fields.values())
    assert "Devin" in set_blob
    assert "plr_ecf_" not in set_blob

    transport = SyntheticDiscordNotificationTransport()
    run_discord_dispatch_cycle(
        config=_dispatch_config(),
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    formal = transport.sent_messages[0]
    tc = transport.sent_team_chat_messages[0]
    all_public = formal.content + formal.embed.title + formal.embed.description + "".join(formal.embed.fields.values()) + tc.content
    assert "plr_ecf_" not in all_public
    assert "mref_ecf_" not in all_public


def test_06_roster_lookup_uses_one_batch_query(pg_db_migrated) -> None:
    """6. Resolving multiple player IDs executes exactly one SQL query against public.fc27_roster."""
    for idx, pid in enumerate(SAMPLE_REFS):
        upsert_roster_player(
            pg_db_migrated.match_results_writer,
            player_id=pid,
            display_name=f"Player{idx + 1}",
            position="CM",
        )

    class _CountingCursor:
        def __init__(self, real_cur: Any, queries: list[str]) -> None:
            self._cur = real_cur
            self._queries = queries

        def execute(self, query: Any, params: Any = None, *args: Any, **kwargs: Any) -> Any:
            q_str = query.as_string(self._cur) if hasattr(query, "as_string") else str(query)
            if "fc27_roster" in q_str.lower():
                self._queries.append(q_str)
            return self._cur.execute(query, params, *args, **kwargs)

        def __enter__(self) -> _CountingCursor:
            self._cur.__enter__()
            return self

        def __exit__(self, *args: Any) -> Any:
            return self._cur.__exit__(*args)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._cur, name)

    class _CountingConn:
        def __init__(self, real_conn: psycopg.Connection[Any]) -> None:
            self._real = real_conn
            self.roster_queries: list[str] = []

        def execute(self, query: Any, params: Any = None, *args: Any, **kwargs: Any) -> Any:
            q_str = query.as_string(self._real) if hasattr(query, "as_string") else str(query)
            if "fc27_roster" in q_str.lower():
                self.roster_queries.append(q_str)
            return self._real.execute(query, params, *args, **kwargs)

        def cursor(self, *args: Any, **kwargs: Any) -> Any:
            return _CountingCursor(self._real.cursor(*args, **kwargs), self.roster_queries)

    with psycopg.connect(pg_db_migrated.discord_reader) as rconn:
        counting = _CountingConn(rconn)
        resolved = resolve_player_names(counting, SAMPLE_REFS)
        rconn.commit()
    assert len(counting.roster_queries) == 1
    assert "= any(" in counting.roster_queries[0].lower()
    assert resolved[SAMPLE_REFS[0]] == "Player1"
    assert resolved[SAMPLE_REFS[5]] == "Player6"


def test_07_fc_goatsss_result_preserves_all_original_statistics(pg_db_migrated) -> None:
    """7. FC Goatsss match preserves kickoff, 1-2 scoreline, Playoff Match, team totals, and 6 distinct players."""
    _seed_provider_match_and_job(
        pg_db_migrated,
        external_suffix="fc_goatsss",
        opponent_name="FC Goatsss",
        goals_for=1,
        goals_against=2,
        match_type="playoffMatch",
        played_at=datetime(2026, 10, 22, 2, 19, tzinfo=timezone.utc),
    )
    transport = SyntheticDiscordNotificationTransport()
    run_discord_dispatch_cycle(
        config=_dispatch_config(team_chat_channel_id=None),
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
    )
    msg = transport.sent_messages[0]
    assert "LOSS 1-2 vs FC Goatsss" in msg.embed.title
    assert msg.embed.fields["📅 Played"] == "2026-10-21 • 10:19 PM EDT"
    assert msg.embed.fields["🏆 Competition"] == "Playoff Match • `4-3-3`"
    assert msg.embed.fields["📊 Team Stats"] == "Shots 2 • Passes 81 (78%) • Tackles 5"
    player_lines = msg.embed.fields["👥 Players"].splitlines()
    assert len(player_lines) == 6
    assert "1A | 20/24 Passes | 3 Tackles | 8.20" in player_lines[0]
    assert "21/25 Passes | 1 Tackles | 7.10" in player_lines[1]
    assert "1G | 7/10 Passes | 2 Shots | 6.90" in player_lines[2]


def test_08_existing_formal_result_is_edited_in_place(pg_db_migrated) -> None:
    """8. Resolving a player edits the already-posted formal result message in place with zero duplicate posts."""
    seeded = _seed_provider_match_and_job(pg_db_migrated, external_suffix="goat_edit_08", opponent_name="FC Goatsss")
    transport = SyntheticDiscordNotificationTransport()
    cfg = _dispatch_config(team_chat_channel_id=None)
    run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert len(transport.sent_messages) == 1
    assert len(transport.edited_messages) == 0

    upsert_roster_player(
        pg_db_migrated.match_results_writer,
        player_id=SAMPLE_REFS[0],
        display_name="Noah",
        position="CM",
    )
    formal_edited, tc_edited = refresh_dispatched_match_message(
        config=cfg,
        transport=transport,
        match_ref=seeded["match_ref"],
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
    )
    assert formal_edited is True
    assert tc_edited is False
    assert len(transport.sent_messages) == 1
    assert len(transport.edited_messages) == 1
    assert "Noah (CM)" in transport.edited_messages[0][2].embed.fields["👥 Players"]


# ==============================================================================
# 9-14. TEAM CHAT DELIVERY, LINKAGE SEPARATION & ZERO DUPLICATES
# ==============================================================================


def test_09_team_chat_output_posted_to_configured_channel(pg_db_migrated) -> None:
    """9. Team-chat coaching summary posts to FC27_DISCORD_TEAM_CHAT_CHANNEL_ID when configured."""
    _seed_provider_match_and_job(pg_db_migrated, external_suffix="tc_09", opponent_name="FC Goatsss")
    transport = SyntheticDiscordNotificationTransport()
    cycle = run_discord_dispatch_cycle(
        config=_dispatch_config(),
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert cycle.messages_sent == 1
    assert cycle.team_chat_messages_sent == 1
    assert len(transport.sent_messages) == 1
    assert len(transport.sent_team_chat_messages) == 1
    assert transport.sent_messages[0].channel_id == RESULTS_CHANNEL_ID
    assert transport.sent_team_chat_messages[0].channel_id == TEAM_CHAT_CHANNEL_ID
    assert "FC Goatsss" in transport.sent_team_chat_messages[0].content


def test_10_no_team_chat_output_when_channel_unset(pg_db_migrated) -> None:
    """10. When team_chat_channel_id is unset, both dispatcher and manual /result skip team chat silently."""
    _seed_provider_match_and_job(pg_db_migrated, external_suffix="tc_10")
    transport = SyntheticDiscordNotificationTransport()
    cycle = run_discord_dispatch_cycle(
        config=_dispatch_config(team_chat_channel_id=None),
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
    )
    assert cycle.messages_sent == 1
    assert cycle.team_chat_messages_sent == 0
    assert transport.sent_team_chat_messages == []

    adapter = InMemoryDiscordChannelAdapter()
    service = FC27DiscordBotService(
        _bot_config(team_chat_channel_id=None),
        db_conn_or_uri=pg_db_migrated.discord_reader,
        writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
        discord_channel_adapter=adapter,
    )
    res = service.handle_slash_command(
        "result",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=ADMIN_USER_ID,
        user_role_ids=[MANAGER_ROLE_ID],
        opponent="Bayfront FC",
        our_score=2,
        opponent_score=1,
    )
    assert res.status == "OK"
    assert adapter.post_count == 1
    assert adapter.text_post_count == 0


def test_11_results_channel_and_team_chat_use_separate_linkage(pg_db_migrated) -> None:
    """11. Formal results and team chat store distinct message IDs and reject shared IDs."""
    seeded = _seed_provider_match_and_job(pg_db_migrated, external_suffix="link_11")
    with pytest.raises(MatchResultsValidationError, match="cannot be reused"):
        record_match_discord_message_links(
            pg_db_migrated.match_results_writer,
            match_ref=seeded["match_ref"],
            discord_guild_id=APPROVED_GUILD_ID,
            results_channel_id=RESULTS_CHANNEL_ID,
            results_message_id="900000000000000001",
            team_chat_channel_id=TEAM_CHAT_CHANNEL_ID,
            team_chat_message_id="900000000000000001",
        )

    transport = SyntheticDiscordNotificationTransport()
    run_discord_dispatch_cycle(
        config=_dispatch_config(),
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    links = get_match_discord_message_links(pg_db_migrated.discord_reader, match_ref=seeded["match_ref"])
    assert links is not None
    assert links.results_channel_id == RESULTS_CHANNEL_ID
    assert links.team_chat_channel_id == TEAM_CHAT_CHANNEL_ID
    assert links.results_message_id is not None
    assert links.team_chat_message_id is not None
    assert links.results_message_id != links.team_chat_message_id


def test_12_retry_produces_no_duplicate_formal_or_team_chat_message(pg_db_migrated) -> None:
    """12. Retrying after formal+team-chat post before completion creates zero duplicates."""
    _seed_provider_match_and_job(pg_db_migrated, external_suffix="retry_12")
    transport = SyntheticDiscordNotificationTransport()

    def _fail_after_team_chat(_msg: Any, _mid: str) -> None:
        transport.on_after_team_chat_send = None
        raise RuntimeError("simulated crash after both messages sent before job completion")

    transport.on_after_team_chat_send = _fail_after_team_chat

    cfg = _dispatch_config()
    cycle1 = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert cycle1.jobs_retried == 1
    assert len(transport.sent_messages) == 1
    assert len(transport.sent_team_chat_messages) == 1

    # Make the retried job immediately claimable again and re-run cycle.
    with psycopg.connect(pg_db_migrated.admin, autocommit=True) as aconn:
        aconn.execute("UPDATE public.fc27_discord_jobs SET available_at = now() - interval '1 second'")

    cycle2 = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert cycle2.jobs_completed == 1
    assert len(transport.sent_messages) == 1
    assert len(transport.sent_team_chat_messages) == 1


def test_13_restart_recovery_produces_no_duplicate_team_chat_message(pg_db_migrated) -> None:
    """13. Fresh dispatcher transport & empty sent cache recover durable links without duplicate posts."""
    seeded = _seed_provider_match_and_job(pg_db_migrated, external_suffix="restart_13")
    transport1 = SyntheticDiscordNotificationTransport()
    cfg = _dispatch_config()

    # Simulate pre-existing sent messages on Discord before restart recovery.
    record_match_discord_message_links(
        pg_db_migrated.match_results_writer,
        match_ref=seeded["match_ref"],
        discord_guild_id=APPROVED_GUILD_ID,
        results_channel_id=RESULTS_CHANNEL_ID,
        results_message_id="910000000000000001",
        team_chat_channel_id=TEAM_CHAT_CHANNEL_ID,
        team_chat_message_id="910000000000000002",
    )

    transport2 = SyntheticDiscordNotificationTransport()
    # Seed Discord channel history on the restarted transport matching durable linkage.
    with psycopg.connect(pg_db_migrated.discord_reader) as rconn:
        from src.fc27_discord_bot import GuardedDiscordReadOnlyConnection

        guarded = GuardedDiscordReadOnlyConnection(rconn)
        m_data, _ = load_authoritative_match_for_job(
            guarded, raw_payload=seeded["payload"], idempotency_key=seeded["idempotency_key"]
        )
        rconn.commit()
    f_msg = build_post_new_results_notification(
        m_data, channel_id=RESULTS_CHANNEL_ID, idempotency_key=seeded["idempotency_key"]
    )
    tc_msg = build_post_new_results_team_chat_message(
        m_data, channel_id=TEAM_CHAT_CHANNEL_ID, idempotency_key=seeded["idempotency_key"]
    )
    transport2._channel_history[RESULTS_CHANNEL_ID] = [("910000000000000001", f_msg)]  # noqa: SLF001
    transport2._team_chat_history[TEAM_CHAT_CHANNEL_ID] = [("910000000000000002", tc_msg)]  # noqa: SLF001

    res = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport2,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
        sent_idempotency_cache=set(),
    )
    assert res.jobs_completed == 1
    assert len(transport2.sent_messages) == 0
    assert len(transport2.sent_team_chat_messages) == 0
    assert transport1.sent_team_chat_messages == []


def test_14_roster_reconciliation_edits_both_existing_messages_in_place(
    pg_db_migrated,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """14. /setplayer and /renameplayer edit both formal & team-chat in place; missing team-chat warns without reposting."""
    seeded = _seed_provider_match_and_job(pg_db_migrated, external_suffix="reconcile_14", opponent_name="FC Goatsss")
    transport = SyntheticDiscordNotificationTransport()
    run_discord_dispatch_cycle(
        config=_dispatch_config(),
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert "Unnamed Player" in transport.sent_messages[0].embed.fields["⚽ Scorers"]
    assert "Goals:" not in transport.sent_team_chat_messages[0].content

    store = ReconciliationTokenStore()
    service = FC27DiscordBotService(
        _bot_config(),
        db_conn_or_uri=pg_db_migrated.discord_reader,
        writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
        dispatch_transport=transport,
        reconciliation_token_store=store,
    )
    entries = list_unresolved_players(
        pg_db_migrated.discord_reader, token_store=store, actor_discord_user_id=ADMIN_USER_ID
    )
    scorer_entry = next(e for e in entries if e.player_id == SAMPLE_REFS[2])
    r_set = service.handle_slash_command(
        "setplayer",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=ADMIN_USER_ID,
        user_role_ids=[ADMIN_ROLE_ID],
        player_id=scorer_entry.token,
        player_name="Kai",
        position="ST",
    )
    assert r_set.status == "OK"
    assert len(transport.sent_messages) == 1
    assert len(transport.sent_team_chat_messages) == 1
    assert len(transport.edited_messages) == 1
    assert len(transport.edited_team_chat_messages) == 1
    assert "Kai x1" in transport.edited_messages[-1][2].embed.fields["⚽ Scorers"]
    assert "Goals: Kai x1" in transport.edited_team_chat_messages[-1][2].content

    r_ren = service.handle_slash_command(
        "renameplayer",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=ADMIN_USER_ID,
        user_role_ids=[ADMIN_ROLE_ID],
        player_name="Kai",
        new_name="Kai Jr",
    )
    assert r_ren.status == "OK"
    assert len(transport.edited_messages) == 2
    assert len(transport.edited_team_chat_messages) == 2
    assert "Goals: Kai Jr x1" in transport.edited_team_chat_messages[-1][2].content

    # Also test manual match card + manually deleted team-chat message -> bounded warning, zero silent repost.
    adapter = InMemoryDiscordChannelAdapter()
    manual_service = FC27DiscordBotService(
        _bot_config(),
        db_conn_or_uri=pg_db_migrated.discord_reader,
        writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
        discord_channel_adapter=adapter,
    )
    r_m = manual_service.handle_slash_command(
        "result",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=ADMIN_USER_ID,
        user_role_ids=[MANAGER_ROLE_ID],
        opponent="Manual Rival FC",
        our_score=1,
        opponent_score=0,
        scorers="Kai Jr",
    )
    assert r_m.status == "OK"
    assert adapter.post_count == 1
    assert adapter.text_post_count == 1
    tc_chan, tc_mid = next(iter(adapter.text_messages.keys()))
    adapter.delete_message(tc_chan, tc_mid)

    with caplog.at_level(logging.WARNING):
        r_ed = manual_service.handle_slash_command(
            "editresult",
            guild_id=APPROVED_GUILD_ID,
            channel_id=APPROVED_CHANNEL_ID,
            user_id=ADMIN_USER_ID,
            user_role_ids=[MANAGER_ROLE_ID],
            match_id=1,
            our_score=2,
        )
    assert r_ed.status == "OK"
    assert adapter.text_post_count == 1  # Zero silent repost!
    assert any("fc27_team_chat_message_missing_on_edit" in rec.message for rec in caplog.records)


# ==============================================================================
# 15-23. DETERMINISTIC CONSTRUCTIVE TEAM-CHAT COACHING SUMMARY RULES
# ==============================================================================


def test_15_low_shot_rule() -> None:
    """15. Low-shot match without high passing calls out converting possessions into attempts."""
    text = build_team_chat_result_summary(
        _manual_detail(home_score=0, away_score=1, shots=3, pass_accuracy=None, tackles=8)
    )
    assert "only 3 shots" in text
    assert "turning more possessions into attempts" in text


def test_16_low_pass_accuracy_rule() -> None:
    """16. Pass accuracy below 75% prioritizes cleaner decisions and simpler combinations."""
    text = build_team_chat_result_summary(
        _manual_detail(home_score=1, away_score=2, shots=7, pass_accuracy=68, tackles=9)
    )
    assert "Pass accuracy finished at 68%" in text
    assert "cleaner decisions and simpler combinations" in text


def test_17_adequate_passing_low_shot_rule() -> None:
    """17. High pass accuracy (>=75%) paired with low shots (<=4) praises passing base and targets progression."""
    text = build_team_chat_result_summary(
        _manual_detail(
            opponent="FC Goatsss", home_score=1, away_score=2, shots=2, pass_accuracy=78, tackles=5
        )
    )
    assert "passing base was solid at 78%" in text
    assert "only 2 shots" in text
    assert "progressing the ball into dangerous areas more consistently" in text


def test_18_one_goal_loss_positive() -> None:
    """18. One-goal loss always highlights the positive within-one-goal competitiveness."""
    text = build_team_chat_result_summary(
        _manual_detail(home_score=1, away_score=2, shots=6, pass_accuracy=80, tackles=8)
    )
    assert "The match remained within one goal." in text


def test_19_draw_copy() -> None:
    """19. Draw uses constructive draw framing."""
    text = build_team_chat_result_summary(
        _manual_detail(home_score=2, away_score=2, shots=8, pass_accuracy=82, tackles=10)
    )
    assert "2-2 draw" in text
    assert "We were close, but the next step is turning control and chances into a winner." in text


def test_20_win_copy_without_bounce_back() -> None:
    """20. Win uses constructive forward framing and never says 'bounce back'."""
    text = build_team_chat_result_summary(
        _manual_detail(home_score=3, away_score=0, shots=11, pass_accuracy=85, tackles=9)
    )
    assert "3-0 win" in text
    assert "Good result." in text
    assert "bounce back" not in text.lower()


def test_21_missing_statistics_are_omitted() -> None:
    """21. Unrecorded metrics, UNKNOWN tokens, and Unnamed Player are omitted rather than invented."""
    match = _manual_detail(
        home_score=1,
        away_score=1,
        shots=None,
        passes=None,
        pass_accuracy=None,
        tackles=None,
        scorers=(_scorer("Unnamed Player", 1, 0),),
    )
    text = build_team_chat_result_summary(match)
    assert "UNKNOWN" not in text.upper()
    assert "Unnamed Player" not in text
    assert "Goals:" not in text
    assert "possession" not in text.lower()
    assert "xG" not in text


def test_22_no_player_is_blamed() -> None:
    """22. Low player ratings or heavy defeats never blame individual players."""
    bad_match = _manual_detail(
        home_score=0,
        away_score=4,
        shots=1,
        pass_accuracy=62,
        tackles=2,
        players=(
            ManualMatchPlayerDetail(
                normalized_display_label="devon",
                safe_display_label="Devon",
                goals=0,
                assists=0,
                passes=8,
                tackles=0,
                shots=0,
                rating=Decimal("4.80"),
            ),
        ),
    )
    text = build_team_chat_result_summary(bad_match)
    assert "Devon" not in text
    for blame in ("fault", "worst", "weakest", "blame", "lazy", "poor defending"):
        if blame == "poor defending":
            # Only acceptable within the explicit caveat phrase if mentioned
            assert "does not indicate poor defending" in text or blame not in text.lower()
        else:
            assert blame not in text.lower()


def test_23_team_chat_content_stays_inside_discord_limits() -> None:
    """23. Team-chat summaries remain under 1,000 characters and safely under Discord's 2,000-char limit."""
    scorers = tuple(
        _scorer(f"Striker{i}", 1, i - 1)
        for i in range(1, 15)
    )
    match = _manual_detail(
        opponent="A Very Long Club Name United",
        home_score=14,
        away_score=3,
        shots=2,
        pass_accuracy=84,
        tackles=2,
        scorers=scorers,
    )
    text = build_team_chat_result_summary(match)
    assert len(text) <= 990
    assert len(text) <= 2000

    auth = FC27AuthoritativeMatchData(
        match_ref="mref_ecf_abcdef123456",
        club_id=EAST_COASTERS_CLUB_ID,
        platform=EAST_COASTERS_PLATFORM,
        game_version=EAST_COASTERS_GAME_VERSION,
        match_type="playoffMatch",
        played_at=datetime(2026, 10, 22, 2, 19, tzinfo=timezone.utc),
        result="W",
        goals_for=5,
        goals_against=0,
        opponent_name="Long Opponent Name FC",
        formation="4-3-3",
        match_status="COMPLETED",
        source_classification="COMMUNITY_API",
        data_quality_status="VERIFIED",
        players=tuple(
            FC27AuthoritativePlayerSummary(
                local_player_reference=f"plr_ecf_{i:012d}",
                position="ST",
                goals=1,
                assists=0,
                provider_rating=8.5,
                passes_completed=12,
                passes_attempted=14,
                tackles_won=1,
                saves=0,
                display_name=f"Player{i}",
                shots=2,
            )
            for i in range(1, 7)
        ),
    )
    tc_msg = build_post_new_results_team_chat_message(
        auth,
        channel_id=TEAM_CHAT_CHANNEL_ID,
        idempotency_key="fc27:post_new_results:mref_ecf_abcdef123456",
    )
    assert len(tc_msg.content) <= 1000


# ==============================================================================
# 24-28. SAFETY, SINGLE-FLIGHT, SCHEMA, ROLE ISOLATION & CLI/COMMAND COVERAGE
# ==============================================================================


def test_24_provider_request_count_remains_zero(pg_db_migrated) -> None:
    """24. All Discord bot and dispatcher operations maintain zero provider network requests."""
    _seed_provider_match_and_job(pg_db_migrated, external_suffix="zero_req_24")
    transport = SyntheticDiscordNotificationTransport()
    res = run_discord_dispatch_cycle(
        config=_dispatch_config(),
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert res.provider_requests_executed == 0
    assert DISPATCH_PROVIDER_REQUESTS == 0
    assert DISCORD_PROVIDER_REQUESTS == 0


def test_25_dispatcher_lease_and_single_flight_intact(pg_db_migrated) -> None:
    """25. Advisory single-flight lock and lease expiration recovery work alongside team chat."""
    with psycopg.connect(pg_db_migrated.player_dev_writer) as c1, psycopg.connect(
        pg_db_migrated.player_dev_writer
    ) as c2:
        q1 = GuardedDiscordJobQueueConnection(c1)
        q2 = GuardedDiscordJobQueueConnection(c2)
        assert try_acquire_dispatch_advisory_lock(q1) is True
        assert try_acquire_dispatch_advisory_lock(q2) is False
        assert release_dispatch_advisory_lock(q1) is True
        assert try_acquire_dispatch_advisory_lock(q2) is True
        release_dispatch_advisory_lock(q2)

    seeded = _seed_provider_match_and_job(pg_db_migrated, external_suffix="lease_25")
    with psycopg.connect(pg_db_migrated.player_dev_writer) as qconn:
        guarded_q = GuardedDiscordJobQueueConnection(qconn)
        claimed = claim_discord_jobs_batch(guarded_q, claim_owner="worker-a", max_jobs=5, lease_minutes=5)
        qconn.commit()
        assert len(claimed) == 1

    # Simulate stale expired lease before running dispatch cycle
    with psycopg.connect(pg_db_migrated.player_dev_writer) as qconn2:
        qconn2.execute(
            """
            UPDATE public.fc27_discord_jobs
            SET lease_expires_at = clock_timestamp() + interval '150 milliseconds'
            WHERE job_id = %s
            """,
            (seeded["job_id"],),
        )
        qconn2.commit()
    time.sleep(0.25)
    transport = SyntheticDiscordNotificationTransport()
    cycle = run_discord_dispatch_cycle(
        config=_dispatch_config(),
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )
    assert cycle.expired_leases_recovered == 1
    assert cycle.jobs_completed == 1
    assert cycle.team_chat_messages_sent == 1


def test_26_migration_0016_and_audit_linkage_compatibility(pg_db) -> None:
    """26. Fresh migration + idempotent re-run stay at current Grants Policy with nullable match_id RELINK linkage."""
    assert GRANTS_POLICY_VERSION == 22
    migrate(pg_db.migrator)
    migrate(pg_db.migrator)

    saved = record_match_discord_message_links(
        pg_db.match_results_writer,
        match_id=None,
        match_ref="mref_ecf_001122334455",
        discord_guild_id=APPROVED_GUILD_ID,
        results_channel_id=RESULTS_CHANNEL_ID,
        results_message_id="800000000000000001",
        team_chat_channel_id=TEAM_CHAT_CHANNEL_ID,
        team_chat_message_id="800000000000000002",
    )
    assert saved.results_message_id == "800000000000000001"
    assert saved.team_chat_message_id == "800000000000000002"
    loaded = get_match_discord_message_links(pg_db.discord_reader, match_ref="mref_ecf_001122334455")
    assert loaded == saved


def test_27_database_role_and_secret_isolation(
    pg_db_migrated,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """27. Role privileges and FC27_ALIAS_HMAC_KEY isolation hold across reader/writer/dispatcher/bot."""
    # fc27_discord_reader cannot write to fc27_roster or fc27_manual_match_audit
    with psycopg.connect(pg_db_migrated.discord_reader) as rconn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            rconn.execute(
                "INSERT INTO public.fc27_roster (player_id, display_name, display_name_normalized) VALUES ('plr_x', 'X', 'x')"
            )
        rconn.rollback()

    # fc27_match_results_writer cannot read fc27_player_alias_fingerprints or fc27_discord_jobs
    with psycopg.connect(pg_db_migrated.match_results_writer) as wconn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            wconn.execute("SELECT COUNT(*) FROM public.fc27_player_alias_fingerprints")
        wconn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            wconn.execute("SELECT COUNT(*) FROM public.fc27_discord_jobs")
        wconn.rollback()

    # FC27_ALIAS_HMAC_KEY cannot be injected into Discord bot or dispatcher runtime
    from src.fc27_discord_dispatcher import (
        FC27DiscordDispatchPostureError,
        resolve_discord_readonly_uri,
    )

    monkeypatch.setenv("FC27_DISCORD_BOT_ENABLED", "true")
    monkeypatch.setenv("FC27_DISCORD_ALLOWED_GUILD_IDS", APPROVED_GUILD_ID)
    monkeypatch.setenv("FC27_DISCORD_ALLOWED_CHANNEL_IDS", f"{APPROVED_CHANNEL_ID},{TEAM_CHAT_CHANNEL_ID}")
    monkeypatch.setenv("FC27_DISCORD_ALLOWED_ROLE_IDS", ADMIN_ROLE_ID)
    monkeypatch.setenv("FC27_DISCORD_RESULTS_CHANNEL_ID", RESULTS_CHANNEL_ID)
    monkeypatch.setenv("FC27_DISCORD_TEAM_CHAT_CHANNEL_ID", TEAM_CHAT_CHANNEL_ID)
    monkeypatch.setenv("FC27_ALIAS_HMAC_KEY", "forbidden-in-discord-minimum-32-bytes!!")
    with pytest.raises(settings.ConfigError, match="FC27_ALIAS_HMAC_KEY"):
        settings.read_fc27_discord_reader_uri(required=False)
    with pytest.raises((settings.ConfigError, FC27DiscordDispatchPostureError), match="FC27_ALIAS_HMAC_KEY"):
        resolve_discord_readonly_uri(required=False)
    monkeypatch.delenv("FC27_ALIAS_HMAC_KEY", raising=False)

    # Team chat channel must belong to allowed channels when allowlist is set
    with pytest.raises(FC27DiscordAuthzError, match="FC27_DISCORD_ALLOWED_CHANNEL_IDS"):
        FC27DiscordBotConfig(
            enabled=True,
            allowed_guild_ids=frozenset({APPROVED_GUILD_ID}),
            allowed_channel_ids=frozenset({APPROVED_CHANNEL_ID}),
            team_chat_channel_id=TEAM_CHAT_CHANNEL_ID,
        )

    # Gamertag resolution fails closed unless explicit operator key + reader connection are provided
    with pytest.raises(MatchResultsValidationError, match="HMAC key"):
        upsert_roster_player(
            pg_db_migrated.match_results_writer,
            gamertag="TestGamer",
            display_name="Alex",
        )

    # Valid operator HMAC lookup works only via explicit reader + alias_hmac_key
    hmac_key = "operator-only-secret-key-minimum-32-bytes!!"
    fp = compute_alias_hmac_fingerprint("OperatorTag99", hmac_key)
    with psycopg.connect(pg_db_migrated.poll_writer, autocommit=True) as pw:
        pw.execute(
            """
            INSERT INTO public.fc27_player_alias_fingerprints (
                pseudonym, gamertag_hmac, last_seen_at, created_at, updated_at
            ) VALUES (%s, %s, now(), now(), now())
            """,
            (SAMPLE_REFS[4], fp),
        )
    mapped = upsert_roster_player(
        pg_db_migrated.match_results_writer,
        gamertag="OperatorTag99",
        display_name="Sam",
        position="CB",
        reader_conn_or_uri=pg_db_migrated.discord_reader,
        alias_hmac_key=hmac_key,
    )
    assert mapped.player_id == SAMPLE_REFS[4]
    assert mapped.display_name == "Sam"


def test_28_existing_commands_and_cli_remain_functional(
    pg_db_migrated,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """28. Full P0 command set (/unresolvedplayers, /setplayer, /renameplayer, /roster, /matchday, /result, /editresult, /lastresult, /match, /results, /topscorers, /playerstats, /deletematch) and protected CLI remain functional."""
    _seed_provider_match_and_job(pg_db_migrated, external_suffix="cli_28", enqueue_job=False)
    adapter = InMemoryDiscordChannelAdapter()
    store = ReconciliationTokenStore()
    service = FC27DiscordBotService(
        _bot_config(),
        db_conn_or_uri=pg_db_migrated.discord_reader,
        writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
        discord_channel_adapter=adapter,
        reconciliation_token_store=store,
    )

    # Non-admin denied /unresolvedplayers
    denied = service.handle_slash_command(
        "unresolvedplayers",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=MEMBER_USER_ID,
        user_role_ids=[MANAGER_ROLE_ID],
    )
    assert denied.status == "REJECTED_UNAUTHORIZED"
    assert denied.content == PERMISSION_DENIED_MESSAGE

    # Admin allowed /unresolvedplayers -> ephemeral T1..T6, zero plr_ecf_*
    unres = service.handle_slash_command(
        "unresolvedplayers",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=ADMIN_USER_ID,
        user_role_ids=[ADMIN_ROLE_ID],
    )
    assert unres.status == "OK"
    assert unres.ephemeral is True
    assert "T1" in unres.content
    assert "plr_ecf_" not in unres.content

    # /setplayer via token + /renameplayer + /roster
    assert (
        service.handle_slash_command(
            "setplayer",
            guild_id=APPROVED_GUILD_ID,
            channel_id=APPROVED_CHANNEL_ID,
            user_id=ADMIN_USER_ID,
            user_role_ids=[ADMIN_ROLE_ID],
            token="T1",
            player_name="Lucas",
            position="CM",
        ).status
        == "OK"
    )
    assert (
        service.handle_slash_command(
            "renameplayer",
            guild_id=APPROVED_GUILD_ID,
            channel_id=APPROVED_CHANNEL_ID,
            user_id=ADMIN_USER_ID,
            user_role_ids=[MANAGER_ROLE_ID],
            player_name="Lucas",
            new_name="Lucas M",
        ).status
        == "OK"
    )
    assert (
        service.handle_slash_command(
            "roster",
            guild_id=APPROVED_GUILD_ID,
            channel_id=APPROVED_CHANNEL_ID,
            user_id=MEMBER_USER_ID,
        ).status
        == "OK"
    )

    # /matchday -> /result -> /editresult -> reads -> /deletematch
    m_day = service.handle_slash_command(
        "matchday",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=ADMIN_USER_ID,
        user_role_ids=[MANAGER_ROLE_ID],
        opponent="Seaside United",
        date_str="2026-11-20",
    )
    assert m_day.status == "OK"
    assert (
        service.handle_slash_command(
            "result",
            guild_id=APPROVED_GUILD_ID,
            channel_id=APPROVED_CHANNEL_ID,
            user_id=ADMIN_USER_ID,
            user_role_ids=[CAPTAIN_ROLE_ID],
            match_id=1,
            opponent="Seaside United",
            our_score=2,
            opponent_score=1,
            scorers="Lucas M x2",
            player_stats="Lucas M: 2 goals, 18/20 passes, 8.4 rating",
        ).status
        == "OK"
    )
    assert adapter.post_count == 1
    assert adapter.text_post_count == 1
    assert (
        service.handle_slash_command(
            "editresult",
            guild_id=APPROVED_GUILD_ID,
            channel_id=APPROVED_CHANNEL_ID,
            user_id=ADMIN_USER_ID,
            user_role_ids=[MANAGER_ROLE_ID],
            match_id=1,
            our_score=3,
            scorers="Lucas M x3",
        ).status
        == "OK"
    )
    assert adapter.edit_count >= 1
    assert adapter.text_edit_count >= 1

    read_commands: tuple[tuple[str, dict[str, Any]], ...] = (
        ("lastresult", {}),
        ("match", {"match_id": 1}),
        ("results", {}),
        ("topscorers", {}),
        ("playerstats", {"player_name": "Lucas M"}),
    )
    for read_cmd, extra in read_commands:
        assert (
            service.handle_slash_command(
                read_cmd,
                guild_id=APPROVED_GUILD_ID,
                channel_id=APPROVED_CHANNEL_ID,
                user_id=MEMBER_USER_ID,
                **extra,
            ).status
            == "OK"
        )

    assert (
        service.handle_slash_command(
            "deletematch",
            guild_id=APPROVED_GUILD_ID,
            channel_id=APPROVED_CHANNEL_ID,
            user_id=ADMIN_USER_ID,
            user_role_ids=[ADMIN_ROLE_ID],
            match_id=1,
            confirm_delete=False,
        ).status
        == "OK"
    )
    assert (
        service.handle_slash_command(
            "deletematch",
            guild_id=APPROVED_GUILD_ID,
            channel_id=APPROVED_CHANNEL_ID,
            user_id=ADMIN_USER_ID,
            user_role_ids=[ADMIN_ROLE_ID],
            match_id=1,
            confirm_delete=True,
            notes="Verified test cleanup",
        ).status
        == "OK"
    )

    # CLI unresolved-players + set-player --dry-run (writes 0 rows) + ambiguity rejection
    rc_list = match_results_cli_main(
        ["unresolved-players", "--db-uri", pg_db_migrated.discord_reader]
    )
    assert rc_list == 0
    cli_out = capsys.readouterr().out
    assert "T1" in cli_out
    assert "plr_ecf_" not in cli_out
    assert "postgresql://" not in cli_out

    rc_dry = match_results_cli_main(
        [
            "set-player",
            "--player-id",
            SAMPLE_REFS[1],
            "--display-name",
            "DryRunPlayer",
            "--position",
            "CM",
            "--dry-run",
            "--reader-uri",
            pg_db_migrated.discord_reader,
            "--writer-uri",
            pg_db_migrated.match_results_writer,
        ]
    )
    assert rc_dry == 0
    with psycopg.connect(pg_db_migrated.discord_reader) as rconn:
        row = rconn.execute(
            "SELECT display_name FROM public.fc27_roster WHERE player_id = %s",
            (SAMPLE_REFS[1],),
        ).fetchone()
    assert row is None

    with pytest.raises(MatchResultsValidationError, match="Ambiguous"):
        match_results_cli_main(
            [
                "set-player",
                "--token",
                "T1",
                "--player-id",
                SAMPLE_REFS[1],
                "--display-name",
                "Ambiguous",
                "--reader-uri",
                pg_db_migrated.discord_reader,
                "--writer-uri",
                pg_db_migrated.match_results_writer,
            ]
        )


# ==============================================================================
# 29-30. REVIEW FOLLOW-UPS: STABLE TEAM-CHAT DISCRIMINATOR & DURABLE TOKENS
# ==============================================================================

def _review_match_data(match_ref: str) -> FC27AuthoritativeMatchData:
    """Build authoritative match payloads that differ only by ``match_ref``."""
    return FC27AuthoritativeMatchData(
        match_ref=match_ref,
        club_id=EAST_COASTERS_CLUB_ID,
        platform=EAST_COASTERS_PLATFORM,
        game_version=EAST_COASTERS_GAME_VERSION,
        match_type="leagueMatch",
        played_at=datetime(2026, 10, 22, 2, 19, tzinfo=timezone.utc),
        result="L",
        goals_for=1,
        goals_against=2,
        opponent_name="Harbor City FC",
        formation="4-3-3",
        match_status="COMPLETED",
        source_classification="COMMUNITY_API",
        data_quality_status="VERIFIED",
        players=(
            FC27AuthoritativePlayerSummary(
                local_player_reference="plr_ecf_000000000001",
                position="ST",
                goals=1,
                assists=0,
                provider_rating=7.5,
                passes_completed=10,
                passes_attempted=12,
                tackles_won=1,
                saves=0,
                display_name="Kai",
                shots=2,
            ),
        ),
    )


def test_29_identical_team_chat_summaries_use_stable_distinct_discriminators() -> None:
    """Two matches with identical coaching summaries must not cross-link during history recovery."""
    match_a = _review_match_data("mref_ecf_aaaaaaaaaaaa")
    match_b = _review_match_data("mref_ecf_bbbbbbbbbbbb")
    msg_a = build_post_new_results_team_chat_message(
        match_a,
        channel_id=TEAM_CHAT_CHANNEL_ID,
        idempotency_key="fc27:post_new_results:mref_ecf_aaaaaaaaaaaa",
    )
    msg_b = build_post_new_results_team_chat_message(
        match_b,
        channel_id=TEAM_CHAT_CHANNEL_ID,
        idempotency_key="fc27:post_new_results:mref_ecf_bbbbbbbbbbbb",
    )

    # Same opponent, score and scorers produce an identical summary body.
    assert msg_a.content.split("\n\nfc27-")[0] == msg_b.content.split("\n\nfc27-")[0]
    assert msg_a.discriminator != msg_b.discriminator
    assert msg_a.content.endswith(msg_a.discriminator)
    assert msg_b.content.endswith(msg_b.discriminator)
    # Internal references stay out of the public team-chat payload.
    assert msg_a.match_ref not in msg_a.content
    assert "plr_ecf_" not in msg_a.content

    transport = SyntheticDiscordNotificationTransport()
    transport.send_team_chat_notification(channel_id=TEAM_CHAT_CHANNEL_ID, message=msg_a)

    assert (
        transport.find_existing_team_chat_notification(
            channel_id=TEAM_CHAT_CHANNEL_ID,
            match_ref=match_b.match_ref,
            idempotency_key="fc27:post_new_results:mref_ecf_bbbbbbbbbbbb",
            message=msg_b,
        )
        is None
    )
    assert (
        transport.find_existing_team_chat_notification(
            channel_id=TEAM_CHAT_CHANNEL_ID,
            match_ref=match_a.match_ref,
            idempotency_key="fc27:post_new_results:mref_ecf_aaaaaaaaaaaa",
            message=msg_a,
        )
        is not None
    )

    # Editing the coaching summary (e.g. after a roster rename) preserves the exact same tag.
    renamed_match_a = replace(
        match_a,
        players=(replace(match_a.players[0], display_name="Marcus"),),
    )
    edited_msg_a = build_post_new_results_team_chat_message(
        renamed_match_a,
        channel_id=TEAM_CHAT_CHANNEL_ID,
        idempotency_key="fc27:post_new_results:mref_ecf_aaaaaaaaaaaa",
    )
    assert edited_msg_a.discriminator == msg_a.discriminator
    assert extract_match_post_tag(edited_msg_a.content) == msg_a.discriminator
    assert edited_msg_a.content != msg_a.content
    assert (
        transport.find_existing_team_chat_notification(
            channel_id=TEAM_CHAT_CHANNEL_ID,
            match_ref=match_a.match_ref,
            idempotency_key="fc27:post_new_results:mref_ecf_aaaaaaaaaaaa",
            message=edited_msg_a,
        )
        is not None
    )

    # Legacy untagged messages still match safely by exact body equality without cross-linking.
    legacy_transport = SyntheticDiscordNotificationTransport()
    legacy_body = msg_a.content.split("\n\nfc27-")[0]
    legacy_msg = FC27TeamChatMessage(
        channel_id=TEAM_CHAT_CHANNEL_ID,
        idempotency_key="other_key",
        match_ref="mref_ecf_legacy000001",
        discriminator=build_match_post_tag("mref_ecf_legacy000001"),
        content=legacy_body,
    )
    legacy_transport._team_chat_history[TEAM_CHAT_CHANNEL_ID] = [  # noqa: SLF001
        ("910000000000000099", legacy_msg),
        ("910000000000000098", msg_b),
    ]
    assert (
        legacy_transport.find_existing_text(
            channel_id=TEAM_CHAT_CHANNEL_ID,
            content=legacy_body,
            discriminator=msg_a.discriminator,
        )
        == "910000000000000099"
    )
    assert (
        legacy_transport.find_existing_text(
            channel_id=TEAM_CHAT_CHANNEL_ID,
            content="Different body",
            discriminator=msg_a.discriminator,
        )
        is None
    )


def test_30_persisted_reconciliation_tokens_survive_a_new_process(pg_db_migrated) -> None:
    """Tokens listed by one process resolve to the same player id elsewhere until they expire."""
    _seed_provider_match_and_job(pg_db_migrated, external_suffix="tokens_30")
    writer_uri = pg_db_migrated.match_results_writer

    lister = ReconciliationTokenStore(writer_conn_or_uri=writer_uri)
    entries = list_unresolved_players(
        pg_db_migrated.discord_reader,
        token_store=lister,
        actor_discord_user_id=ADMIN_USER_ID,
    )
    assert entries
    issued = {entry.token: entry.player_id for entry in entries}
    assert issued

    # A second match changes unresolved-player ordering (e.g. boosting SAMPLE_REFS[5] appearances),
    # yet previously issued T1..Tn bindings keep pointing to the original player_ids.
    _seed_provider_match_and_job(
        pg_db_migrated,
        external_suffix="tokens_30_order_shift",
        played_at=datetime(2026, 10, 23, 3, 0, tzinfo=timezone.utc),
        players=[
            {
                "ref": SAMPLE_REFS[5],
                "position": "GK",
                "goals": 0,
                "assists": 0,
                "passes_attempted": 10,
                "passes_completed": 9,
                "tackles_attempted": 0,
                "tackles_won": 0,
                "shots": 0,
                "shots_on_target": 0,
                "rating": Decimal("9.50"),
            }
        ],
    )

    # A brand-new store (a separate process) resolves the same tokens to the same players.
    consumer = ReconciliationTokenStore(writer_conn_or_uri=writer_uri)
    for token, player_id in issued.items():
        assert consumer.resolve(token, actor_discord_user_id=ADMIN_USER_ID) == player_id

    # Cross-actor token use is isolated per Discord actor and fails closed.
    other_actor_id = "999999999999999999"
    with pytest.raises(MatchResultsValidationError, match="Unknown or expired"):
        consumer.resolve("T1", actor_discord_user_id=other_actor_id)

    with pytest.raises(MatchResultsValidationError, match="Unknown or expired"):
        consumer.resolve("T99", actor_discord_user_id=ADMIN_USER_ID)

    # Re-issuing a smaller batch replaces prior actor tokens so stale slots cannot linger.
    smaller = ReconciliationTokenStore(writer_conn_or_uri=writer_uri)
    smaller.issue_tokens([SAMPLE_REFS[0]], actor_discord_user_id=ADMIN_USER_ID)
    assert smaller.resolve("T1", actor_discord_user_id=ADMIN_USER_ID) == SAMPLE_REFS[0]
    with pytest.raises(MatchResultsValidationError, match="Unknown or expired"):
        smaller.resolve("T2", actor_discord_user_id=ADMIN_USER_ID)

    # Expired bindings fail closed instead of being re-derived from the current ordering.
    expired = ReconciliationTokenStore(writer_conn_or_uri=writer_uri)
    expired.issue_tokens(
        [SAMPLE_REFS[0]],
        actor_discord_user_id=ADMIN_USER_ID,
        ttl_seconds=60,
        now=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    with pytest.raises(MatchResultsValidationError, match="expired"):
        expired.resolve("T1", actor_discord_user_id=ADMIN_USER_ID)


def test_31_refresh_recovers_team_chat_message_from_history_without_linkage(
    pg_db_migrated,
) -> None:
    """A team-chat post with no durable linkage is recovered from history and edited, not reposted."""
    seeded = _seed_provider_match_and_job(
        pg_db_migrated, external_suffix="tc_recover_31", opponent_name="FC Goatsss"
    )
    cfg = _dispatch_config()

    # Discord already holds the team-chat post, but no durable linkage row was written.
    restarted = SyntheticDiscordNotificationTransport()
    with psycopg.connect(pg_db_migrated.discord_reader) as rconn:
        from src.fc27_discord_bot import GuardedDiscordReadOnlyConnection

        guarded = GuardedDiscordReadOnlyConnection(rconn)
        m_data, _ = load_authoritative_match_for_job(
            guarded, raw_payload=seeded["payload"], idempotency_key=seeded["idempotency_key"]
        )
        rconn.commit()
    tc_msg = build_post_new_results_team_chat_message(
        m_data, channel_id=TEAM_CHAT_CHANNEL_ID, idempotency_key=seeded["idempotency_key"]
    )
    posted_tc_id = "910000000000000002"
    restarted._team_chat_history[TEAM_CHAT_CHANNEL_ID] = [(posted_tc_id, tc_msg)]  # noqa: SLF001

    _formal_edited, tc_edited = refresh_dispatched_match_message(
        config=cfg,
        transport=restarted,
        match_ref=seeded["match_ref"],
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
    )
    assert tc_edited is True
    assert len(restarted.edited_team_chat_messages) == 1
    assert len(restarted.sent_team_chat_messages) == 0

def test_32_readonly_token_store_and_cli_dry_run_resolve_without_writer(
    pg_db_migrated, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tokens can be resolved read-only (dry-run) without a writer connection."""
    from src import fc27_match_results as match_results_module

    _seed_provider_match_and_job(pg_db_migrated, external_suffix="tokens_32")
    writer_store = ReconciliationTokenStore(
        writer_conn_or_uri=pg_db_migrated.match_results_writer
    )
    entries = list_unresolved_players(
        pg_db_migrated.discord_reader,
        token_store=writer_store,
        actor_discord_user_id=ADMIN_USER_ID,
    )
    assert entries
    token = entries[0].token

    reader_store = ReconciliationTokenStore(reader_conn_or_uri=pg_db_migrated.discord_reader)
    assert reader_store.resolve(token, actor_discord_user_id=ADMIN_USER_ID) == entries[0].player_id

    monkeypatch.setattr(
        match_results_module, "resolve_match_results_writer_uri", lambda *, strict=False: None
    )
    rc = match_results_cli_main(
        [
            "set-player",
            "--token",
            token,
            "--display-name",
            "DryRunToken",
            "--position",
            "CM",
            "--dry-run",
            "--reader-uri",
            pg_db_migrated.discord_reader,
            "--actor-discord-user-id",
            ADMIN_USER_ID,
        ]
    )
    assert rc == 0
    with psycopg.connect(pg_db_migrated.discord_reader) as rconn:
        row = rconn.execute(
            "SELECT display_name FROM public.fc27_roster WHERE player_id = %s",
            (entries[0].player_id,),
        ).fetchone()
    assert row is None


def test_33_persistent_token_store_requires_actor_and_writer(pg_db_migrated) -> None:
    """Actor-less and writer-less persistent token usage fails closed."""
    writer_store = ReconciliationTokenStore(
        writer_conn_or_uri=pg_db_migrated.match_results_writer
    )
    with pytest.raises(MatchResultsValidationError, match="actor id is required"):
        writer_store.issue_tokens([SAMPLE_REFS[0]], actor_discord_user_id=None)
    with pytest.raises(MatchResultsValidationError, match="actor id is required"):
        writer_store.resolve("T1", actor_discord_user_id=None)

    reader_store = ReconciliationTokenStore(reader_conn_or_uri=pg_db_migrated.discord_reader)
    with pytest.raises(MatchResultsValidationError, match="writer database URI"):
        reader_store.issue_tokens([SAMPLE_REFS[0]], actor_discord_user_id=ADMIN_USER_ID)


def test_34_manual_result_recovers_unlinked_team_chat_post_and_persists_id(
    pg_db_migrated,
) -> None:
    """Manual result sync recovers an existing team-chat post by tag, edits it in place, and persists linkage."""
    created = record_match_result(
        pg_db_migrated.match_results_writer,
        opponent="Bayfront Athletic",
        home_score=2,
        away_score=0,
        scorers="Lao x2",
        actor_discord_user_id=ADMIN_USER_ID,
        discord_guild_id=APPROVED_GUILD_ID,
        discord_channel_id=RESULTS_CHANNEL_ID,
        discord_message_id="920000000000000001",
    )
    adapter = InMemoryDiscordChannelAdapter(start_snowflake=920000000000000010)
    # Formal embed already exists at the recorded message ID.
    adapter.messages[(RESULTS_CHANNEL_ID, "920000000000000001")] = build_result_embed(created)
    # Simulate prior run that posted to team chat with the stable manual tag,
    # then crashed before persisting team_chat_message_id into PostgreSQL.
    stale_team_chat = tag_match_post_content(
        "Old team-chat summary before edit",
        seed=f"manual:{created.match_id}",
    )
    pre_tc_id, _ = adapter.post_text(TEAM_CHAT_CHANNEL_ID, stale_team_chat)
    assert adapter.text_post_count == 1
    pre_links = get_match_discord_message_links(
        pg_db_migrated.discord_reader, match_id=created.match_id
    )
    assert pre_links is None or pre_links.team_chat_message_id is None

    service = FC27DiscordBotService(
        _bot_config(),
        db_conn_or_uri=pg_db_migrated.discord_reader,
        writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
        discord_channel_adapter=adapter,
    )
    edited_resp = service.handle_slash_command(
        "editresult",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=ADMIN_USER_ID,
        user_role_ids=[ADMIN_ROLE_ID],
        match_id=created.match_id,
        our_score=3,
        opponent_score=1,
        scorers="Lao x3",
    )
    assert edited_resp.status == "OK"
    # No duplicate team-chat post was sent; existing post was edited in place.
    assert adapter.text_post_count == 1
    assert adapter.text_edit_count == 1
    updated_text = adapter.get_text(TEAM_CHAT_CHANNEL_ID, pre_tc_id)
    assert updated_text is not None
    assert "3-1" in updated_text
    assert extract_match_post_tag(updated_text) == build_match_post_tag(
        f"manual:{created.match_id}"
    )

    links = get_match_discord_message_links(
        pg_db_migrated.discord_reader, match_id=created.match_id
    )
    assert links is not None
    assert links.team_chat_message_id == pre_tc_id
    assert links.team_chat_channel_id == TEAM_CHAT_CHANNEL_ID


def test_35_dispatcher_persists_recovered_message_ids_when_db_linkage_missing(
    pg_db_migrated,
) -> None:
    """When formal/team-chat IDs are recovered from history without DB linkage, they are persisted to PostgreSQL."""
    seeded = _seed_provider_match_and_job(
        pg_db_migrated,
        external_suffix="dispatch_recover_35",
        opponent_name="Harbor City FC",
    )
    cfg = _dispatch_config()

    # Force a second-attempt job where Discord already received both posts,
    # while PostgreSQL linkage is completely absent.
    with psycopg.connect(pg_db_migrated.admin, autocommit=True) as aconn:
        aconn.execute(
            """
            UPDATE public.fc27_discord_jobs
            SET attempt_count = 2
            WHERE job_id = %s
            """,
            (seeded["job_id"],),
        )

    with psycopg.connect(pg_db_migrated.discord_reader) as rconn:
        from src.fc27_discord_bot import GuardedDiscordReadOnlyConnection

        guarded = GuardedDiscordReadOnlyConnection(rconn)
        m_data, _ = load_authoritative_match_for_job(
            guarded, raw_payload=seeded["payload"], idempotency_key=seeded["idempotency_key"]
        )
        rconn.commit()

    formal_msg = build_post_new_results_notification(
        m_data, channel_id=RESULTS_CHANNEL_ID, idempotency_key=seeded["idempotency_key"]
    )
    tc_msg = build_post_new_results_team_chat_message(
        m_data, channel_id=TEAM_CHAT_CHANNEL_ID, idempotency_key=seeded["idempotency_key"]
    )
    recovered_res_id = "930000000000000001"
    recovered_tc_id = "930000000000000002"

    transport = SyntheticDiscordNotificationTransport()
    transport._channel_history[RESULTS_CHANNEL_ID] = [(recovered_res_id, formal_msg)]  # noqa: SLF001
    transport._team_chat_history[TEAM_CHAT_CHANNEL_ID] = [(recovered_tc_id, tc_msg)]  # noqa: SLF001

    assert get_match_discord_message_links(
        pg_db_migrated.discord_reader, match_ref=seeded["match_ref"]
    ) is None

    summary = run_discord_dispatch_cycle(
        config=cfg,
        transport=transport,
        queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
        readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
        match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
        sent_idempotency_cache=set(),
        verify_posture=False,
    )
    assert summary.jobs_completed == 1
    assert len(transport.sent_messages) == 0
    assert len(transport.sent_team_chat_messages) == 0

    links = get_match_discord_message_links(
        pg_db_migrated.discord_reader, match_ref=seeded["match_ref"]
    )
    assert links is not None
    assert links.results_message_id == recovered_res_id
    assert links.results_channel_id == RESULTS_CHANNEL_ID
    assert links.team_chat_message_id == recovered_tc_id
    assert links.team_chat_channel_id == TEAM_CHAT_CHANNEL_ID


def test_36_compose_forwards_team_chat_channel_id_and_writer_cannot_access_discord_jobs(
    pg_db_migrated,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding 8/9: compose forwards team-chat ID and writer stays barred from fc27_discord_jobs."""
    compose_path = Path(__file__).resolve().parents[1] / "docker-compose.discord.yml"
    compose_text = compose_path.read_text(encoding="utf-8")
    assert "FC27_DISCORD_TEAM_CHAT_CHANNEL_ID: ${FC27_DISCORD_TEAM_CHAT_CHANNEL_ID:-}" in compose_text

    monkeypatch.setenv("FC27_DISCORD_ALLOWED_CHANNEL_IDS", f"{RESULTS_CHANNEL_ID},{TEAM_CHAT_CHANNEL_ID}")
    monkeypatch.setenv("FC27_DISCORD_TEAM_CHAT_CHANNEL_ID", TEAM_CHAT_CHANNEL_ID)
    assert settings.get_fc27_discord_team_chat_channel_id(strict=True) == TEAM_CHAT_CHANNEL_ID

    monkeypatch.setenv("FC27_DISCORD_TEAM_CHAT_CHANNEL_ID", "999999999999999999")
    with pytest.raises(settings.ConfigError, match="FC27_DISCORD_ALLOWED_CHANNEL_IDS"):
        settings.get_fc27_discord_team_chat_channel_id(strict=True)

    # Finding 8 boundary: writer connection wrapper & DB role must both block fc27_discord_jobs.
    from src.fc27_match_results import open_guarded_match_results_writer_connection

    with open_guarded_match_results_writer_connection(
        pg_db_migrated.match_results_writer,
        require_dedicated_writer_role=True,
    ) as wconn:
        with wconn.cursor() as wcur:
            with pytest.raises(MatchResultsPostureError, match="forbidden"):
                wcur.execute("SELECT job_id FROM public.fc27_discord_jobs LIMIT 1")

    with psycopg.connect(pg_db_migrated.admin) as aconn:
        jobs_row = aconn.execute(
            "SELECT has_table_privilege(%s, 'public.fc27_discord_jobs', 'INSERT')",
            (ROLE_MATCH_RESULTS_WRITER,),
        ).fetchone()
        assert jobs_row is not None and jobs_row[0] is False
        reader_sel_row = aconn.execute(
            "SELECT has_table_privilege(%s, 'public.fc27_roster_reconciliation_tokens', 'SELECT')",
            (ROLE_DISCORD_READER,),
        ).fetchone()
        assert reader_sel_row is not None and reader_sel_row[0] is True
        for mut_priv in ("INSERT", "UPDATE", "DELETE"):
            priv_row = aconn.execute(
                "SELECT has_table_privilege(%s, 'public.fc27_roster_reconciliation_tokens', %s)",
                (ROLE_DISCORD_READER, mut_priv),
            ).fetchone()
            assert priv_row is not None and priv_row[0] is False
        for w_priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            w_row = aconn.execute(
                "SELECT has_table_privilege(%s, 'public.fc27_roster_reconciliation_tokens', %s)",
                (ROLE_MATCH_RESULTS_WRITER, w_priv),
            ).fetchone()
            assert w_row is not None and w_row[0] is True
        assert verify_postconditions(aconn) == []


def test_37_migration_0016_to_0017_upgrade_path(pg_db) -> None:
    """Upgrading from 0016 cleanly applies 0017 and satisfies all postconditions."""
    applied_0016 = migrate(pg_db.migrator, target="0016")
    assert "0016_fc27_canonical_roster_and_legacy_matches.sql" in applied_0016
    assert "0017_fc27_roster_reconciliation_tokens.sql" not in applied_0016
    applied_0017 = migrate(pg_db.migrator, target="0017")
    assert applied_0017 == ["0017_fc27_roster_reconciliation_tokens.sql"]
    with psycopg.connect(pg_db.admin) as uconn:
        assert verify_postconditions(uconn) == []
    applied_0018_plus = migrate(pg_db.migrator)
    assert applied_0018_plus == [
        "0018_fc27_roster_refresh_queue.sql",
        "0019_fc27_player_alias_candidates.sql",
        "0020_fc27_alias_auto_promotion_and_exceptions.sql",
        "0021_fc27_destination_aware_delivery_and_linkage.sql",
    ]
    with psycopg.connect(pg_db.admin) as uconn:
        assert verify_postconditions(uconn) == []
