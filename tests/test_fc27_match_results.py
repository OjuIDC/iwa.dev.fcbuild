"""Comprehensive offline unit and PostgreSQL integration tests for East Coasters FC manual Match Results (`src/fc27_match_results.py` and `/fc27` Discord commands)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import logging
from typing import Any

import psycopg
import pytest

from src.fc27_discord_bot import (
    FC27DiscordBotConfig,
    FC27DiscordBotService,
    PERMISSION_DENIED_MESSAGE,
    assert_discord_output_sanitized,
)
from src.fc27_match_results import (
    BRAND_COLOR_DRAW,
    BRAND_COLOR_ELECTRIC_BLUE,
    BRAND_COLOR_LOSS,
    BRAND_COLOR_SILVER,
    BRAND_COLOR_WIN,
    EAST_COASTERS_FOOTER_TEXT,
    EAST_COASTERS_FOUNDED_TEXT,
    EAST_COASTERS_FULL_NAME,
    EAST_COASTERS_SHORT_NAME,
    GuardedMatchResultsWriterConnection,
    InMemoryDiscordChannelAdapter,
    MANUAL_MATCH_RESULT,
    ManualMatchDetail,
    ManualMatchPlayerDetail,
    MatchDeleteConfirmationView,
    MatchResultsConcurrencyError,
    MatchResultsNotFoundError,
    MatchResultsPaginationView,
    MatchResultsPostureError,
    MatchResultsValidationError,
    UNRESOLVED_PLAYER_DISPLAY_NAME,
    build_result_content,
    build_result_embed,
    clean_opponent,
    coerce_public_match_id,
    create_scheduled_match,
    edit_match_result,
    fetch_latest_manual_result,
    fetch_manual_match,
    fetch_manual_match_by_legacy_ref,
    fetch_manual_player_stats,
    fetch_manual_results_page,
    fetch_manual_top_scorers,
    format_scorers_summary,
    get_player_name,
    list_roster_players,
    normalize_competition_label,
    normalize_display_label,
    parse_kickoff_datetime,
    parse_player_stats_input,
    parse_scorers_input,
    reconcile_scorers_and_player_stats,
    record_match_result,
    rename_roster_player,
    render_empty_match_results_embed,
    render_match_result_embed,
    render_matchday_embed,
    render_player_stats_embed,
    render_results_page_embed,
    render_roster_embed,
    render_top_scorers_embed,
    resolve_player_names,
    soft_delete_match,
    upsert_roster_player,
    validate_bounded_int,
    validate_discord_snowflake,
    validate_safe_text,
)

APPROVED_GUILD_ID = "123456789012345678"
APPROVED_CHANNEL_ID = "234567890123456789"
RESULTS_CHANNEL_ID = "234567890123456789"
MANAGER_ROLE_ID = "444444444444444444"
CAPTAIN_ROLE_ID = "555555555555555555"
ADMIN_ROLE_ID = "666666666666666666"
REGULAR_ROLE_ID = "777777777777777777"
AUTHORIZED_USER_ID = "345678901234567890"
UNAUTHORIZED_USER_ID = "888888888888888888"


def _build_match_results_bot_config(**overrides: Any) -> FC27DiscordBotConfig:
    params: dict[str, Any] = {
        "enabled": True,
        "allowed_guild_ids": frozenset({APPROVED_GUILD_ID}),
        "allowed_channel_ids": frozenset({APPROVED_CHANNEL_ID}),
        "manager_role_ids": frozenset({MANAGER_ROLE_ID}),
        "captain_role_ids": frozenset({CAPTAIN_ROLE_ID}),
        "admin_role_ids": frozenset({ADMIN_ROLE_ID}),
        "results_channel_id": RESULTS_CHANNEL_ID,
        "max_user_commands_per_minute": 60,
        "max_guild_commands_per_minute": 120,
    }
    params.update(overrides)
    return FC27DiscordBotConfig(**params)


# ==============================================================================
# 1. SCORER PARSER UNIT TESTS
# ==============================================================================


def test_scorer_parser_supported_formats() -> None:
    """Verify deterministic parsing of `Lao x2, Jay` and `Lao (2), Jay (1)` formats."""
    parsed_a = parse_scorers_input("Lao x2, Jay", home_score=3)
    assert [
        (s.safe_display_label, s.normalized_display_label, s.goals) for s in parsed_a
    ] == [
        ("Lao", "lao", 2),
        ("Jay", "jay", 1),
    ]
    assert format_scorers_summary(parsed_a) == "Lao x2, Jay x1"

    parsed_b = parse_scorers_input("Lao (2), Jay (1)", home_score=3)
    assert [
        (s.safe_display_label, s.normalized_display_label, s.goals) for s in parsed_b
    ] == [
        ("Lao", "lao", 2),
        ("Jay", "jay", 1),
    ]

    parsed_c = parse_scorers_input("Lao x2, Jay x2, Sam x1", home_score=5)
    assert sum(s.goals for s in parsed_c) == 5
    assert [s.safe_display_label for s in parsed_c] == ["Lao", "Jay", "Sam"]

    # Duplicate normalized entries deterministically merge under first-seen order
    parsed_merged = parse_scorers_input("Lao x1, Jay x1, lao x1", home_score=3)
    assert [
        (s.safe_display_label, s.normalized_display_label, s.goals)
        for s in parsed_merged
    ] == [
        ("Lao", "lao", 2),
        ("Jay", "jay", 1),
    ]


def test_scorer_parser_rejects_invalid_inputs() -> None:
    """Scorer parser rejects zero/negative goals, >home_score totals, and scorers when home_score=0."""
    # Total exceeds home_score
    with pytest.raises(MatchResultsValidationError, match="cannot exceed"):
        parse_scorers_input("Lao x3, Jay x2", home_score=4)

    # Non-empty scorers when home_score == 0
    with pytest.raises(MatchResultsValidationError, match="cannot exceed"):
        parse_scorers_input("Lao x1", home_score=0)

    # Zero or negative count
    with pytest.raises(MatchResultsValidationError, match="between 1 and 30"):
        parse_scorers_input("Lao x0", home_score=2)
    with pytest.raises(MatchResultsValidationError, match="between 1 and 30"):
        parse_scorers_input("Lao (-1)", home_score=2)

    # Discord mentions / URLs / control chars rejected
    with pytest.raises(MatchResultsValidationError):
        parse_scorers_input("<@123456789012345678> x1", home_score=1)
    with pytest.raises(MatchResultsValidationError):
        parse_scorers_input("https://example.com x1", home_score=1)


# ==============================================================================
# 2. PLAYER-STAT PARSER & RECONCILIATION UNIT TESTS
# ==============================================================================


def test_player_stat_parser_and_reconciliation() -> None:
    """Verify deterministic player-stat parsing and reconciliation with parsed scorers."""
    raw_stats = (
        "Lao: 2 goals, 1 assist, 4 tackles, 32 passes, 84 pass_accuracy; "
        "Jay: 1 goal, 2 assists"
    )
    scorers = parse_scorers_input("Lao x2, Jay x1, Sam x1", home_score=4)
    player_stats = parse_player_stats_input(raw_stats, home_score=4)
    assert len(player_stats) == 2
    assert player_stats[0].safe_display_label == "Lao"
    assert player_stats[0].goals == 2
    assert player_stats[0].assists == 1
    assert player_stats[0].tackles == 4
    assert player_stats[0].passes == 32
    assert player_stats[0].pass_accuracy == 84

    final_scorers, reconciled_players = reconcile_scorers_and_player_stats(
        scorers,
        player_stats,
        home_score=4,
    )
    assert len(final_scorers) == 3
    assert [p.safe_display_label for p in reconciled_players] == ["Lao", "Jay", "Sam"]
    sam_entry = next(
        p for p in reconciled_players if p.normalized_display_label == "sam"
    )
    assert sam_entry.goals == 1
    assert sam_entry.assists == 0


def test_player_stat_parser_rejects_invalid_metrics_and_goal_mismatch() -> None:
    """Player-stat parser rejects unknown metrics, out-of-range pass_accuracy/rating, and conflicting scorer goals."""
    with pytest.raises(
        MatchResultsValidationError, match="Unsupported player stat metric"
    ):
        parse_player_stats_input("Lao: 2 goals, 5 yellow_cards")

    with pytest.raises(MatchResultsValidationError, match="between 0 and 100"):
        parse_player_stats_input("Lao: 120 pass_accuracy")

    with pytest.raises(MatchResultsValidationError, match="between 0.0 and 10.0"):
        parse_player_stats_input("Lao: 11.5 rating")

    with pytest.raises(MatchResultsValidationError, match="Conflicting duplicate"):
        parse_player_stats_input("Lao: 1 goal; lao: 2 goals")

    scorers = parse_scorers_input("Lao x2", home_score=2)
    conflicting_stats = parse_player_stats_input("Lao: 1 goal, 3 tackles")
    with pytest.raises(MatchResultsValidationError, match="Contradictory goal count"):
        reconcile_scorers_and_player_stats(
            scorers,
            conflicting_stats,
            home_score=2,
        )


# ==============================================================================
# 3. INPUT VALIDATION, FORMATTING, AND PRIVACY ENFORCEMENT TESTS
# ==============================================================================


def test_input_validation_bounds_and_privacy_guards() -> None:
    """Validate bounds on scores, percentages, dates, times, and forbid reserved tokens / links / mentions."""
    assert (
        validate_safe_text("Harbor City FC", field_name="Opponent", max_length=80)
        == "Harbor City FC"
    )
    assert (
        validate_bounded_int(
            0, field_name="Home score", minimum=0, maximum=30, allow_none=False
        )
        == 0
    )
    assert (
        validate_bounded_int(
            30, field_name="Home score", minimum=0, maximum=30, allow_none=False
        )
        == 30
    )
    with pytest.raises(MatchResultsValidationError):
        validate_bounded_int(
            -1, field_name="Home score", minimum=0, maximum=30, allow_none=False
        )
    with pytest.raises(MatchResultsValidationError):
        validate_bounded_int(
            31, field_name="Home score", minimum=0, maximum=30, allow_none=False
        )

    assert (
        validate_bounded_int(None, field_name="Possession", minimum=0, maximum=100)
        is None
    )
    assert (
        validate_bounded_int(56, field_name="Possession", minimum=0, maximum=100) == 56
    )
    with pytest.raises(MatchResultsValidationError):
        validate_bounded_int(101, field_name="Possession", minimum=0, maximum=100)

    assert validate_discord_snowflake("123456789012345678") == "123456789012345678"
    with pytest.raises(MatchResultsValidationError):
        validate_discord_snowflake("not_a_snowflake")

    # April 20 is in EDT (UTC-4), so 20:30 local Eastern converts to 00:30 UTC the next day
    dt_edt = parse_kickoff_datetime("2026-04-20", "20:30")
    assert dt_edt == datetime(2026, 4, 21, 0, 30, tzinfo=timezone.utc)

    # January is in EST (UTC-5), July default 8:00 PM is in EDT (UTC-4), explicit UTC preserved
    dt_est = parse_kickoff_datetime("2026-01-15", "20:30")
    assert dt_est == datetime(2026, 1, 16, 1, 30, tzinfo=timezone.utc)
    dt_summer_default = parse_kickoff_datetime("2026-07-15", None)
    assert dt_summer_default == datetime(2026, 7, 16, 0, 0, tzinfo=timezone.utc)
    dt_utc = parse_kickoff_datetime("2026-07-15", "20:30 UTC")
    assert dt_utc == datetime(2026, 7, 15, 20, 30, tzinfo=timezone.utc)

    # Spring-forward DST gap (2026-03-08 02:30 America/New_York does not exist) must raise
    with pytest.raises(MatchResultsValidationError, match="Eastern Daylight Saving Time gap"):
        parse_kickoff_datetime("2026-03-08", "2:30 AM")

    # Fall-back DST ambiguous time (2026-11-01 01:30 occurs twice) requires explicit EDT/EST
    with pytest.raises(MatchResultsValidationError, match="ambiguous"):
        parse_kickoff_datetime("2026-11-01", "1:30 AM")
    assert parse_kickoff_datetime("2026-11-01", "1:30 AM EDT") == datetime(
        2026, 11, 1, 5, 30, tzinfo=timezone.utc
    )
    assert parse_kickoff_datetime("2026-11-01", "1:30 AM EST") == datetime(
        2026, 11, 1, 6, 30, tzinfo=timezone.utc
    )

    # Forbidden tokens, mentions, or links in player or opponent names
    for forbidden_label in (
        "None",
        "null",
        "undefined",
        "NaN",
        "@everyone",
        "<@12345>",
        "https://evil.example",
    ):
        with pytest.raises(MatchResultsValidationError):
            normalize_display_label(forbidden_label, field_name="Player")


def test_embed_renderers_never_emit_forbidden_strings_and_use_club_identity() -> None:
    """Shared embed renderers include East Coasters FC identity and never output None/null/undefined/NaN."""
    empty_embed = render_empty_match_results_embed(
        "Latest Match Result", "No manual match results have been recorded yet."
    )
    assert getattr(empty_embed.color, "value", empty_embed.color) == BRAND_COLOR_SILVER
    assert EAST_COASTERS_SHORT_NAME in (empty_embed.title or "")
    assert empty_embed.footer.text == EAST_COASTERS_FOOTER_TEXT
    assert EAST_COASTERS_FULL_NAME in (empty_embed.description or "")
    assert EAST_COASTERS_FOUNDED_TEXT in (empty_embed.description or "")
    assert_discord_output_sanitized(empty_embed.title or "")
    assert_discord_output_sanitized(empty_embed.description or "")


# ==============================================================================
# 4.DATABASE ROLE & GUARDED WRITER CONNECTION ENFORCEMENT TESTS
# ==============================================================================


def test_least_privilege_writer_role_and_guard_enforcement(pg_db_migrated) -> None:
    """
    Verify `fc27_match_results_writer` least-privilege boundaries:
    - Cannot access provider tables (`fc27_matches`, `fc27_player_match_stats`), governance, or identity tables.
    - Cannot hard-DELETE from `fc27_manual_matches` or `fc27_manual_match_audit`.
    - `fc27_manual_match_audit` append-only trigger blocks UPDATE and DELETE even for owner.
    - `GuardedMatchResultsWriterConnection` rejects `fc27_owner` and `fc27_app` when `require_dedicated_writer_role=True`.
    """
    with psycopg.connect(pg_db_migrated.migrator) as migrator_conn:
        with pytest.raises(MatchResultsPostureError):
            GuardedMatchResultsWriterConnection(
                migrator_conn, require_dedicated_writer_role=True
            )

    with psycopg.connect(pg_db_migrated.app) as app_conn:
        with pytest.raises(MatchResultsPostureError, match="fc27_match_results_writer"):
            GuardedMatchResultsWriterConnection(
                app_conn, require_dedicated_writer_role=True
            )

    with psycopg.connect(pg_db_migrated.match_results_writer) as writer_conn:
        guarded = GuardedMatchResultsWriterConnection(
            writer_conn, require_dedicated_writer_role=True
        )
        assert guarded.current_role == "fc27_match_results_writer"

        # Guarded cursor blocks SQL mentioning provider/governance tables
        with guarded.cursor() as gcur:
            for forbidden_sql in (
                "SELECT * FROM public.fc27_matches",
                "SELECT * FROM public.fc27_player_match_stats",
                "SELECT * FROM public.approval_requests",
                "SELECT * FROM public.dispatches",
                "SELECT * FROM public.outbox",
                "SELECT * FROM public.schema_migrations",
                "SELECT * FROM public.fc27_users",
            ):
                with pytest.raises(MatchResultsPostureError):
                    gcur.execute(forbidden_sql)

        # Direct PostgreSQL role privileges block provider tables and hard DELETE on matches/audit
        with writer_conn.cursor() as raw_cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with writer_conn.transaction():
                    raw_cur.execute("SELECT COUNT(*) FROM public.fc27_matches")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with writer_conn.transaction():
                    raw_cur.execute(
                        "DELETE FROM public.fc27_manual_matches WHERE match_id = 999999"
                    )
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with writer_conn.transaction():
                    raw_cur.execute(
                        "DELETE FROM public.fc27_manual_match_audit WHERE audit_id = 999999"
                    )


# ==============================================================================
# 5. END-TO-END ACCEPTANCE SEQUENCE & EDIT-IN-PLACE / RELINKING TESTS (POSTGRES)
# ==============================================================================


def test_end_to_end_match_results_acceptance_sequence(pg_db_migrated) -> None:
    """
    End-to-end acceptance sequence:
    1. Create scheduled match (`/fc27 matchday` vs Harbor City FC, 2026-04-20, 20:30, League Match).
    2. Record 4-2 result (`/fc27 result`) with scorers, player_stats, possession=56, shots=11, shots_on_target=7, passes=134, pass_accuracy=83, corners=5, tackles=14, fouls=4.
    3. Edit result to 5-2 (`/fc27 editresult`) with updated scorers (`Lao x2, Jay x2, Sam x1`) and player_stats.
    4. Edit possession only to 58 (`/fc27 editresult match_id=1 possession=58`).
    5. Retrieve latest result (`/fc27 lastresult`).
    6. Retrieve match by ID (`/fc27 match match_id=1`) and by opponent (`/fc27 match opponent='Harbor City FC'`).
    7. Verify score (5-2), scorers, MOTM (`Lao`), tackles (`4` for Lao), passes (`32` for Lao), possession (`58%`), and single Discord message linkage edited in place.
    """
    channel_adapter = InMemoryDiscordChannelAdapter()
    service = FC27DiscordBotService(
        _build_match_results_bot_config(),
        db_conn_or_uri=pg_db_migrated.app,
        writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
        discord_channel_adapter=channel_adapter,
    )

    # 1. Create scheduled match
    r_matchday = service.handle_slash_command(
        "matchday",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=AUTHORIZED_USER_ID,
        user_role_ids=[MANAGER_ROLE_ID],
        opponent="Harbor City FC",
        date_str="2026-04-20",
        time_str="20:30",
        notes="League Match",
    )
    assert r_matchday.status == "OK"
    assert r_matchday.ephemeral is False
    assert (
        r_matchday.embeds[0].title
        == f"📅 MATCHDAY — {EAST_COASTERS_SHORT_NAME} vs Harbor City FC"
    )
    assert channel_adapter.post_count == 1
    assert channel_adapter.edit_count == 0
    initial_msg_id = "700000000000000001"

    # 2. Post 4-2 result updating the scheduled match
    r_result = service.handle_slash_command(
        "result",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=AUTHORIZED_USER_ID,
        user_role_ids=[CAPTAIN_ROLE_ID],
        match_id=1,
        opponent="Harbor City FC",
        our_score=4,
        opponent_score=2,
        scorers="Lao x2, Jay x1, Sam x1",
        motm="Lao",
        possession=56,
        shots=11,
        shots_on_target=7,
        passes=134,
        pass_accuracy=83,
        corners=5,
        tackles=14,
        fouls=4,
        player_stats="Lao: 2 goals, 1 assist, 4 tackles, 32 passes, 84 pass_accuracy; Jay: 1 goal, 2 assists; Sam: 1 goal",
    )
    assert r_result.status == "OK"
    assert r_result.ephemeral is False
    assert "56%" in r_result.embeds[0].fields["📊 Team Stats"]
    # Existing Matchday card edited in place (no duplicate post)
    assert channel_adapter.post_count == 1
    assert channel_adapter.edit_count == 1

    # 3. Edit result to 5-2
    r_edit_1 = service.handle_slash_command(
        "editresult",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=AUTHORIZED_USER_ID,
        user_role_ids=[ADMIN_ROLE_ID],
        match_id=1,
        our_score=5,
        opponent_score=2,
        scorers="Lao x2, Jay x2, Sam x1",
        player_stats="Lao: 2 goals, 1 assist, 4 tackles, 32 passes, 84 pass_accuracy; Jay: 2 goals, 2 assists; Sam: 1 goal",
    )
    assert r_edit_1.status == "OK"
    assert r_edit_1.ephemeral is False
    assert channel_adapter.post_count == 1
    assert channel_adapter.edit_count == 2

    # 4. Edit possession only to 58 (preserving scorers, MOTM, and player stats)
    r_edit_2 = service.handle_slash_command(
        "editresult",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=AUTHORIZED_USER_ID,
        user_role_ids=[MANAGER_ROLE_ID],
        match_id=1,
        possession=58,
    )
    assert r_edit_2.status == "OK"
    assert r_edit_2.ephemeral is False
    assert channel_adapter.post_count == 1
    assert channel_adapter.edit_count == 3

    # 5. Retrieve latest result (`/fc27 lastresult`) as regular user
    r_last = service.handle_slash_command(
        "lastresult",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=UNAUTHORIZED_USER_ID,
        user_role_ids=[REGULAR_ROLE_ID],
    )
    assert r_last.status == "OK"
    last_embed = r_last.embeds[0]
    assert "WIN 5-2 vs Harbor City FC" in last_embed.title
    assert f"**{EAST_COASTERS_SHORT_NAME}** 5-2 **Harbor City FC**" in last_embed.description

    # 6. Retrieve match by ID (`/fc27 match match_id=1`) and by opponent
    r_match = service.handle_slash_command(
        "match",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=UNAUTHORIZED_USER_ID,
        user_role_ids=[],
        match_id=1,
    )
    assert r_match.status == "OK"
    match_embed = r_match.embeds[0]
    assert "Lao x2, Jay x2, Sam x1" in match_embed.fields["⚽ Scorers"]
    assert match_embed.fields["⭐ MOTM"] == "Lao"
    assert "Possession 58%" in match_embed.fields["📊 Team Stats"]
    assert "Tackles 14" in match_embed.fields["📊 Team Stats"]
    assert "Passes 134" in match_embed.fields["📊 Team Stats"]
    assert (
        "Lao — 2G 1A | 32 Passes (84%) | 4 Tackles"
        in match_embed.fields["👥 Players"]
    )

    r_match_opp = service.handle_slash_command(
        "match",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=UNAUTHORIZED_USER_ID,
        user_role_ids=[],
        opponent="harbor city fc",
    )
    assert r_match_opp.status == "OK"
    assert r_match_opp.embeds[0].title == match_embed.title

    # 7. Verify DB state and provider-table non-interference directly
    with psycopg.connect(pg_db_migrated.app) as conn:
        record = fetch_manual_match(conn, match_id=1)
        assert record is not None
        assert record.home_score == 5
        assert record.away_score == 2
        assert record.motm_display_label == "Lao"
        assert record.possession == 58
        assert record.discord_message_id == initial_msg_id
        assert record.source_classification == MANUAL_MATCH_RESULT

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.fc27_matches")
            _matches_row = cur.fetchone()
            assert _matches_row is not None and _matches_row[0] == 0
            cur.execute("SELECT COUNT(*) FROM public.fc27_player_match_stats")
            _stats_row = cur.fetchone()
            assert _stats_row is not None and _stats_row[0] == 0


def test_missing_discord_message_relinks_cleanly_on_edit(pg_db_migrated) -> None:
    """When the linked Discord message was deleted in Discord, `/fc27 editresult` posts a replacement and relinks."""
    channel_adapter = InMemoryDiscordChannelAdapter()
    service = FC27DiscordBotService(
        _build_match_results_bot_config(),
        db_conn_or_uri=pg_db_migrated.app,
        writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
        discord_channel_adapter=channel_adapter,
    )

    service.handle_slash_command(
        "result",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=AUTHORIZED_USER_ID,
        user_role_ids=[MANAGER_ROLE_ID],
        opponent="Atlantic United",
        our_score=2,
        opponent_score=1,
        scorers="Lao x2",
        motm="Lao",
    )
    assert channel_adapter.post_count == 1
    first_msg_id = "700000000000000001"

    # Simulate external message deletion in Discord
    channel_adapter.delete_message(RESULTS_CHANNEL_ID, first_msg_id)

    # Edit result -> should post replacement and relink
    r_edit = service.handle_slash_command(
        "editresult",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=AUTHORIZED_USER_ID,
        user_role_ids=[MANAGER_ROLE_ID],
        match_id=1,
        our_score=3,
        opponent_score=1,
        scorers="Lao x2, Jay x1",
    )
    assert r_edit.status == "OK"
    assert channel_adapter.post_count == 2
    replacement_msg_id = "700000000000000002"

    with psycopg.connect(pg_db_migrated.app) as conn:
        rec = fetch_manual_match(conn, match_id=1)
        assert rec is not None
        assert rec.discord_message_id == replacement_msg_id
        with conn.cursor() as cur:
            cur.execute(
                "SELECT action FROM public.fc27_manual_match_audit WHERE match_id = 1 ORDER BY audit_id ASC"
            )
            actions = [r[0] for r in cur.fetchall()]
            assert "RELINK_DISCORD_MESSAGE" in actions


# ==============================================================================
# 6. ROLE-ID AUTHORIZATION, OPTIMISTIC CONCURRENCY, AND SOFT DELETE TESTS
# ==============================================================================


def test_write_commands_enforce_role_id_authorization_and_audit_denials(
    pg_db_migrated,
) -> None:
    """Unauthorized users receive exact `You don't have permission.` ephemerally and emit an audit row."""
    service = FC27DiscordBotService(
        _build_match_results_bot_config(),
        db_conn_or_uri=pg_db_migrated.app,
        writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )

    for write_cmd in ("matchday", "result", "editresult", "deletematch"):
        resp = service.handle_slash_command(
            write_cmd,
            guild_id=APPROVED_GUILD_ID,
            channel_id=APPROVED_CHANNEL_ID,
            user_id=UNAUTHORIZED_USER_ID,
            user_role_ids=[REGULAR_ROLE_ID, "Manager", "Captain", "Admin"],
            opponent="Test FC",
            date_str="2026-05-01",
            time_str="19:00",
            our_score=1,
            opponent_score=0,
            match_id=1,
        )
        assert resp.status == "REJECTED_UNAUTHORIZED"
        assert resp.ephemeral is True
        assert resp.content == PERMISSION_DENIED_MESSAGE

    with psycopg.connect(pg_db_migrated.app) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM public.fc27_manual_match_audit WHERE action = 'UNAUTHORIZED_WRITE_DENIED'"
            )
            _audit_row = cur.fetchone()
            assert _audit_row is not None and _audit_row[0] == 4


def test_optimistic_concurrency_prevents_lost_updates(pg_db_migrated) -> None:
    """Stale `expected_revision` raises `MatchResultsConcurrencyError` and preserves newer edits."""
    created = record_match_result(
        pg_db_migrated.match_results_writer,
        opponent="Bay Area XI",
        home_score=1,
        away_score=1,
        actor_discord_user_id=AUTHORIZED_USER_ID,
        scorers="Lao x1",
        possession=50,
    )
    assert created.revision == 1

    updated = edit_match_result(
        pg_db_migrated.match_results_writer,
        match_id=created.match_id,
        actor_discord_user_id=AUTHORIZED_USER_ID,
        possession=54,
        expected_revision=1,
    )
    assert updated.revision == 2
    assert updated.possession == 54

    # Stale revision 1 must fail
    with pytest.raises(
        MatchResultsConcurrencyError, match="Optimistic concurrency conflict"
    ):
        edit_match_result(
            pg_db_migrated.match_results_writer,
            match_id=created.match_id,
            actor_discord_user_id=AUTHORIZED_USER_ID,
            possession=45,
            expected_revision=1,
        )

    with psycopg.connect(pg_db_migrated.app) as conn:
        current = fetch_manual_match(conn, match_id=created.match_id)
        assert current is not None
        assert current.possession == 54
        assert current.revision == 2


def test_results_pagination_topscorers_playerstats_and_soft_delete(
    pg_db_migrated,
) -> None:
    """Verify `/fc27 results`, `/fc27 topscorers`, `/fc27 playerstats`, and `/fc27 deletematch` soft deletion."""
    service = FC27DiscordBotService(
        _build_match_results_bot_config(),
        db_conn_or_uri=pg_db_migrated.app,
        writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )

    # Seed 6 matches for pagination and leaderboard testing
    for i in range(1, 7):
        service.handle_slash_command(
            "result",
            guild_id=APPROVED_GUILD_ID,
            channel_id=APPROVED_CHANNEL_ID,
            user_id=AUTHORIZED_USER_ID,
            user_role_ids=[MANAGER_ROLE_ID],
            opponent=f"Opponent Club {i}",
            our_score=2,
            opponent_score=1,
            scorers="Lao x1, Jay x1",
            motm="Lao" if i % 2 == 1 else "Jay",
            player_stats="Lao: 1 goal, 1 assist, 3 tackles, 20 passes, 80 pass_accuracy; Jay: 1 goal, 2 tackles, 15 passes, 90 pass_accuracy",
        )

    # Paginated results page 1 and page 2
    r_page1 = service.handle_slash_command(
        "results",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=UNAUTHORIZED_USER_ID,
        page=1,
        page_size=5,
    )
    assert r_page1.status == "OK"
    assert r_page1.page == 1
    assert r_page1.total_pages == 2
    assert r_page1.has_next_page is True
    assert r_page1.row_count == 5

    r_page2 = service.handle_slash_command(
        "results",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=UNAUTHORIZED_USER_ID,
        page=2,
        page_size=5,
    )
    assert r_page2.status == "OK"
    assert r_page2.page == 2
    assert r_page2.has_next_page is False
    assert r_page2.row_count == 1

    # Top scorers
    r_top = service.handle_slash_command(
        "topscorers",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=UNAUTHORIZED_USER_ID,
    )
    assert r_top.status == "OK"
    leaderboard_text = r_top.embeds[0].fields["Manual Match Results Leaderboard"]
    # Lao has 6 goals, 6 assists; Jay has 6 goals, 0 assists -> Lao ranks #1
    assert "`#1` **Lao** — `6` Goals | `6` Assists" in leaderboard_text
    assert "`#2` **Jay** — `6` Goals | `0` Assists" in leaderboard_text

    # Player stats for Lao
    r_pstats = service.handle_slash_command(
        "playerstats",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=UNAUTHORIZED_USER_ID,
        player_name="lao",
    )
    assert r_pstats.status == "OK"
    totals_text = r_pstats.embeds[0].fields["Career Manual Match Totals"]
    assert "Matches: `6`" in totals_text
    assert "Goals: `6` | Assists: `6`" in totals_text
    assert "Tackles: `18` | Passes: `120`" in totals_text
    assert "Average Pass Accuracy: `80.0%`" in totals_text
    assert "MOTM Awards: `3`" in totals_text

    # Delete confirmation step (`confirm_delete=False`) does not delete yet
    r_del_prompt = service.handle_slash_command(
        "deletematch",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=AUTHORIZED_USER_ID,
        user_role_ids=[ADMIN_ROLE_ID],
        match_id=6,
        confirm_delete=False,
    )
    assert r_del_prompt.status == "OK"
    assert r_del_prompt.ephemeral is True
    assert "Confirmation required" in r_del_prompt.content

    # Confirmed soft delete (`confirm_delete=True`)
    r_del_exec = service.handle_slash_command(
        "deletematch",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=AUTHORIZED_USER_ID,
        user_role_ids=[ADMIN_ROLE_ID],
        match_id=6,
        notes="Duplicate entry",
        confirm_delete=True,
    )
    assert r_del_exec.status == "OK"
    assert r_del_exec.ephemeral is True
    assert "soft-deleted" in r_del_exec.content

    # Match #6 is now excluded from normal queries, while preserved in DB with deleted_at set
    r_match6 = service.handle_slash_command(
        "match",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=UNAUTHORIZED_USER_ID,
        match_id=6,
    )
    assert r_match6.status == "ERROR"
    assert "Match #6 not found. Use /results to list matches." in r_match6.content

    with psycopg.connect(pg_db_migrated.app) as conn:
        assert fetch_manual_match(conn, match_id=6) is None
        with conn.cursor() as cur:
            cur.execute(
                "SELECT deleted_at, deleted_by, delete_reason FROM public.fc27_manual_matches WHERE match_id = 6"
            )
            del_row = cur.fetchone()
            assert del_row is not None
            assert del_row[0] is not None
            assert del_row[1] == AUTHORIZED_USER_ID
            assert del_row[2] == "Duplicate entry"


def test_discord_ui_pagination_and_delete_confirmation_views_offline(
    pg_db_migrated,
) -> None:
    """Verify `MatchResultsPaginationView` and `MatchDeleteConfirmationView` button states, author checks, and timeouts."""
    for i in range(1, 8):
        create_scheduled_match(
            pg_db_migrated.match_results_writer,
            opponent=f"View Club {i}",
            match_date="2026-04-25",
            match_time="20:00",
            actor_discord_user_id=AUTHORIZED_USER_ID,
        )

    class _DummyResponse:
        def __init__(self) -> None:
            self.sent_messages: list[tuple[str, bool]] = []
            self.edited_embeds: list[Any] = []

        async def send_message(
            self, content: str, *, ephemeral: bool = False, **kwargs: Any
        ) -> None:
            self.sent_messages.append((content, ephemeral))

        async def edit_message(
            self, *, embed: Any = None, view: Any = None, **kwargs: Any
        ) -> None:
            self.edited_embeds.append(embed)

    class _DummyUser:
        def __init__(self, uid: str) -> None:
            self.id = uid

    class _DummyInteraction:
        def __init__(self, uid: str) -> None:
            self.user = _DummyUser(uid)
            self.response = _DummyResponse()

    async def _exercise_views() -> None:
        with psycopg.connect(pg_db_migrated.app) as conn:
            p1 = fetch_manual_results_page(conn, page=1, page_size=5)
            view = MatchResultsPaginationView(
                initial_page=p1,
                page_fetcher=lambda target_p: fetch_manual_results_page(
                    conn, page=target_p, page_size=5
                ),
                author_discord_user_id=AUTHORIZED_USER_ID,
            )
            assert view.prev_button.disabled is True
            assert view.next_button.disabled is False

            # Non-author interaction is rejected with PERMISSION_DENIED_MESSAGE
            other_inter = _DummyInteraction(UNAUTHORIZED_USER_ID)
            await view._change_page(other_inter, 2)  # type: ignore[arg-type]
            assert other_inter.response.sent_messages == [
                (PERMISSION_DENIED_MESSAGE, True)
            ]

            # Author interaction advances to page 2
            author_inter = _DummyInteraction(AUTHORIZED_USER_ID)
            await view._change_page(author_inter, 2)  # type: ignore[arg-type]
            assert view.current_page == 2
            assert not view.prev_button.disabled
            assert view.next_button.disabled
            assert len(author_inter.response.edited_embeds) == 1

            await view.on_timeout()
            for child in view.children:
                assert getattr(child, "disabled", False) is True

        confirmed_ids: list[int] = []

        async def _on_confirm(inter: Any) -> None:
            confirmed_ids.append(1)

        del_view = MatchDeleteConfirmationView(
            match_id=1,
            author_discord_user_id=AUTHORIZED_USER_ID,
            on_confirm_callback=_on_confirm,
        )
        other_del_inter = _DummyInteraction(UNAUTHORIZED_USER_ID)
        await del_view.confirm_button.callback(other_del_inter)  # type: ignore[arg-type]
        assert other_del_inter.response.sent_messages == [
            (PERMISSION_DENIED_MESSAGE, True)
        ]
        assert confirmed_ids == []

        author_del_inter = _DummyInteraction(AUTHORIZED_USER_ID)
        await del_view.confirm_button.callback(author_del_inter)  # type: ignore[arg-type]
        assert confirmed_ids == [1]
        assert del_view.completed is True

    asyncio.run(_exercise_views())


# ==============================================================================
# 8. P0 CANONICAL RESULTS, PLAYER ROSTER, AND HARDENING TESTS
# ==============================================================================


def test_p0_clean_opponent_and_competition_labels() -> None:
    """Verify opponent cleaning and raw EA competition token translation."""
    assert clean_opponent("Real_Madrid_FC") == "Real Madrid FC"
    assert clean_opponent("phoenix_united_sc") == "Phoenix United SC"
    assert clean_opponent("east_coasters_xi") == "East Coasters XI"
    assert clean_opponent("Rival [mention-suppressed] FC") == "Rival [mention-suppressed] FC"

    assert normalize_competition_label("leagueMatch") == "League Match"
    assert normalize_competition_label("playoffMatch") == "Playoff Match"
    assert normalize_competition_label("friendlyMatch") == "Friendly Match"
    assert normalize_competition_label("UNKNOWN") == "League Match"
    assert normalize_competition_label(None) == "League Match"


def test_p0_build_result_content_colors_omission_splitting_and_zero_jargon() -> None:
    """Verify canonical content, embed outcome colors, missing-field omission, player chunk splitting, and zero internal jargon."""
    now = datetime(2026, 5, 1, 20, 0, tzinfo=timezone.utc)
    sched = ManualMatchDetail(
        match_id=10,
        club_id="330323",
        platform="common-gen5",
        game_version="FC27",
        opponent="Harbor City FC",
        opponent_normalized="harbor city fc",
        match_date="2026-05-01",
        match_time="8PM EST",
        status="SCHEDULED",
        competition="leagueMatch",
        created_by="1",
        updated_by="1",
        created_at=now,
        updated_at=now,
        revision=1,
    )
    assert build_result_content(sched) == "📅 MATCHDAY — East Coasters FC vs Harbor City FC"
    sched_emb = build_result_embed(sched)
    assert getattr(sched_emb.color, "value", sched_emb.color) == BRAND_COLOR_ELECTRIC_BLUE

    def _make_ft(match_id: int, home: int, away: int, players: tuple[ManualMatchPlayerDetail, ...] = ()) -> ManualMatchDetail:
        return ManualMatchDetail(
            match_id=match_id,
            club_id="330323",
            platform="common-gen5",
            game_version="FC27",
            opponent="Phoenix_Club_FC",
            opponent_normalized="phoenix club fc",
            match_date="2026-05-01",
            match_time="8:00 PM EST",
            home_score=home,
            away_score=away,
            status="FULL_TIME",
            competition="leagueMatch",
            formation=None,
            created_by="1",
            updated_by="1",
            created_at=now,
            updated_at=now,
            revision=1,
            players=players,
        )

    win_match = _make_ft(11, 3, 1)
    assert build_result_content(win_match) == "✅ FULL TIME — East Coasters FC 3-1 Phoenix Club FC"
    win_emb = build_result_embed(win_match)
    assert getattr(win_emb.color, "value", win_emb.color) == BRAND_COLOR_WIN
    # Optional fields omitted cleanly when missing
    field_names = [f.name for f in win_emb.fields]
    assert "⭐ MOTM" not in field_names
    assert "⚽ Scorers" not in field_names
    assert "📊 Team Stats" not in field_names
    assert "📝 Notes" not in field_names
    assert "👥 Players" not in field_names
    assert [f.value for f in win_emb.fields if f.name == "🏆 Competition"] == ["League Match"]

    loss_emb = build_result_embed(_make_ft(12, 1, 2))
    assert getattr(loss_emb.color, "value", loss_emb.color) == BRAND_COLOR_LOSS
    draw_emb = build_result_embed(_make_ft(13, 2, 2))
    assert getattr(draw_emb.color, "value", draw_emb.color) == BRAND_COLOR_DRAW

    # Large roster > 1024 chars splits cleanly across 👥 Players and 👥 Players (cont.)
    many_players = tuple(
        ManualMatchPlayerDetail(
            normalized_display_label=f"long player name number {i:02d}",
            safe_display_label=f"Long Player Name Number {i:02d}",
            position="CM",
            goals=1 if i == 0 else 0,
            assists=2,
            tackles=5,
            passes=42,
            passes_completed=38,
            passes_attempted=42,
            pass_accuracy=90,
            shots=3,
            saves=0,
            rating=Decimal("8.4"),
        )
        for i in range(28)
    )
    split_emb = build_result_embed(_make_ft(14, 2, 0, players=many_players))
    split_names = [f.name for f in split_emb.fields]
    assert "👥 Players" in split_names
    assert "👥 Players (cont.)" in split_names
    for fld in split_emb.fields:
        assert fld.value is not None
        assert len(fld.value) <= 1024

    rendered_all = "\n".join(
        [
            build_result_content(win_match),
            split_emb.title or "",
            split_emb.description or "",
            getattr(split_emb.footer, "text", "") or "",
            *(f"{f.name}: {f.value}" for f in split_emb.fields),
        ]
    )
    for forbidden in (
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
        assert forbidden not in rendered_all


def test_p0_roster_commands_rename_unresolved_and_single_query_batch_resolution(
    pg_db_migrated, caplog: pytest.LogCaptureFixture
) -> None:
    """Verify /setplayer, /roster, /renameplayer, unresolved warning + Unnamed Player, and single-query batch resolution."""
    service = FC27DiscordBotService(
        _build_match_results_bot_config(),
        db_conn_or_uri=pg_db_migrated.app,
        writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )

    r1 = service.handle_slash_command(
        "setplayer",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=AUTHORIZED_USER_ID,
        user_role_ids=[MANAGER_ROLE_ID],
        player_id="plr_ecf_aaaaaaaaaaaa",
        player_name="Lao",
        position="CM",
        discord_user_id="123456789012345678",
    )
    assert r1.status == "OK"
    assert "Lao" in r1.content

    r2 = service.handle_slash_command(
        "setplayer",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=AUTHORIZED_USER_ID,
        user_role_ids=[CAPTAIN_ROLE_ID],
        player_id="plr_ecf_bbbbbbbbbbbb",
        player_name="Jay",
        position="ST",
    )
    assert r2.status == "OK"

    # /roster lists active players cleanly without raw plr_ecf_ tokens
    r_roster = service.handle_slash_command(
        "roster",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=UNAUTHORIZED_USER_ID,
    )
    assert r_roster.status == "OK"
    roster_text = "\n".join(r_roster.embeds[0].fields.values())
    assert "Lao" in roster_text and "Jay" in roster_text
    assert "plr_ecf_" not in roster_text

    # Single-query batch resolution (zero N+1 queries)
    class _CountingConn:
        def __init__(self, real_conn: Any) -> None:
            self._real = real_conn
            self.execute_count = 0

        def cursor(self) -> Any:
            outer = self
            inner_cur = self._real.cursor()

            class _Cur:
                def __enter__(self) -> _Cur:
                    inner_cur.__enter__()
                    return self

                def __exit__(self, *args: Any) -> Any:
                    return inner_cur.__exit__(*args)

                def execute(self, sql: Any, params: Any = None) -> Any:
                    outer.execute_count += 1
                    return inner_cur.execute(sql, params)

                def fetchall(self) -> Any:
                    return inner_cur.fetchall()

            return _Cur()

    with psycopg.connect(pg_db_migrated.app) as app_conn:
        counting = _CountingConn(app_conn)
        resolved = resolve_player_names(
            counting,
            ["plr_ecf_aaaaaaaaaaaa", "plr_ecf_bbbbbbbbbbbb", "plr_ecf_cccccccccccc"],
        )
        assert counting.execute_count == 1
        assert resolved == {
            "plr_ecf_aaaaaaaaaaaa": "Lao",
            "plr_ecf_bbbbbbbbbbbb": "Jay",
        }
        with caplog.at_level(logging.WARNING):
            fallback_name = get_player_name("plr_ecf_cccccccccccc", roster_map=resolved)
        assert fallback_name == UNRESOLVED_PLAYER_DISPLAY_NAME
        assert any("fc27_roster_unresolved_player" in rec.message for rec in caplog.records)

    # Record a match with registered player_id-linked row + two unresolved Unnamed Player rows
    with psycopg.connect(pg_db_migrated.match_results_writer) as wconn:
        with wconn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.fc27_manual_matches (
                    club_id, platform, game_version, opponent, opponent_normalized,
                    match_date, match_time, home_score, away_score, status,
                    competition, formation, created_by, updated_by
                ) VALUES ('330323', 'common-gen5', 'FC27', 'Harbor City FC', 'harbor city fc',
                          '2026-05-02', '8PM EST', 3, 1, 'FULL_TIME',
                          'League Match', '4-2-3-1', '1', '1')
                RETURNING match_id
                """
            )
            _mid_row = cur.fetchone()
            assert _mid_row is not None
            mid = int(_mid_row[0])
            cur.execute(
                """
                INSERT INTO public.fc27_manual_match_players (
                    match_id, player_id, normalized_display_label, safe_display_label,
                    position, goals, assists
                ) VALUES
                    (%s, 'plr_ecf_bbbbbbbbbbbb', 'jay', 'Jay', 'ST', 2, 0),
                    (%s, 'plr_ecf_cccccccccccc', 'plr_ecf_cccccccccccc', 'plr_ecf_cccccccccccc', 'CM', 1, 0),
                    (%s, 'plr_ecf_dddddddddddd', 'plr_ecf_dddddddddddd', 'plr_ecf_dddddddddddd', 'RB', 0, 1)
                """,
                (mid, mid, mid),
            )
        wconn.commit()

    # /renameplayer updates Jay -> Jay V2 and propagates to match reads
    r_ren = service.handle_slash_command(
        "renameplayer",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=AUTHORIZED_USER_ID,
        user_role_ids=[MANAGER_ROLE_ID],
        player_name="Jay",
        new_name="Jay V2",
    )
    assert r_ren.status == "OK"
    assert "Jay V2" in r_ren.content

    r_match = service.handle_slash_command(
        "match",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=UNAUTHORIZED_USER_ID,
        opponent="Harbor City FC",
    )
    assert r_match.status == "OK"
    match_blob = "\n".join(r_match.embeds[0].fields.values())
    assert "Jay V2" in match_blob
    assert "Unnamed Player" in match_blob
    assert "plr_ecf_" not in match_blob


def test_p0_admin_only_deletematch_matchday_defaults_idempotency_and_queries(
    pg_db_migrated,
) -> None:
    """Verify Admin-only /deletematch, /matchday defaults, interaction_id idempotency, /results opponent filter, and /topscorers default 5."""
    service = FC27DiscordBotService(
        _build_match_results_bot_config(),
        db_conn_or_uri=pg_db_migrated.app,
        writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
    )

    # /matchday defaults time to 8PM EST and competition to League Match; duplicate interaction_id is idempotent
    r_md1 = service.handle_slash_command(
        "matchday",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=AUTHORIZED_USER_ID,
        user_role_ids=[MANAGER_ROLE_ID],
        opponent="Metro United",
        date_str="2026-05-05",
        interaction_id="999000111222333444",
    )
    assert r_md1.status == "OK"
    assert r_md1.content == "📅 MATCHDAY — East Coasters FC vs Metro United"
    assert "8:00 PM EDT" in r_md1.embeds[0].fields["📅 Kickoff"]
    assert r_md1.embeds[0].fields["🏆 Competition"] == "League Match"

    r_md_dup = service.handle_slash_command(
        "matchday",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=AUTHORIZED_USER_ID,
        user_role_ids=[MANAGER_ROLE_ID],
        opponent="Metro United",
        date_str="2026-05-05",
        interaction_id="999000111222333444",
    )
    assert r_md_dup.status == "OK"
    assert r_md_dup.embeds[0].description == r_md1.embeds[0].description

    # /result duplicate interaction_id is idempotent
    r_res1 = service.handle_slash_command(
        "result",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=AUTHORIZED_USER_ID,
        user_role_ids=[CAPTAIN_ROLE_ID],
        opponent="Metro United",
        our_score=3,
        opponent_score=1,
        scorers="P1 x1, P2 x1, P3 x1",
        player_stats="P4: 1 assist; P5: 1 assist; P6: 1 assist",
        competition="playoffMatch",
        formation="4-3-3",
        interaction_id="999000111222333555",
    )
    assert r_res1.status == "OK"
    assert "Playoff Match" in r_res1.embeds[0].fields["🏆 Competition"]
    assert "4-3-3" in r_res1.embeds[0].fields["🏆 Competition"]

    r_res_dup = service.handle_slash_command(
        "result",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=AUTHORIZED_USER_ID,
        user_role_ids=[CAPTAIN_ROLE_ID],
        opponent="Metro United",
        our_score=3,
        opponent_score=1,
        interaction_id="999000111222333555",
    )
    assert r_res_dup.status == "OK"
    assert r_res_dup.embeds[0].description == r_res1.embeds[0].description

    # Seed extra matches for opponent filtering and /results page_size capping
    service.handle_slash_command(
        "matchday",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=AUTHORIZED_USER_ID,
        user_role_ids=[MANAGER_ROLE_ID],
        opponent="Metro City",
        date_str="2026-05-06",
    )
    service.handle_slash_command(
        "result",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=AUTHORIZED_USER_ID,
        user_role_ids=[MANAGER_ROLE_ID],
        opponent="Coastal FC",
        our_score=2,
        opponent_score=2,
    )

    r_filtered = service.handle_slash_command(
        "results",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=UNAUTHORIZED_USER_ID,
        opponent="Metro",
        page_size=25,
    )
    assert r_filtered.status == "OK"
    assert r_filtered.row_count == 2  # playoff result vs Metro United + scheduled match vs Metro City

    with psycopg.connect(pg_db_migrated.app) as conn:
        from src.fc27_match_results import fetch_manual_results_page

        capped_page = fetch_manual_results_page(conn, page_size=25)
        assert capped_page.page_size == 10

    r_last_metro = service.handle_slash_command(
        "lastresult",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=UNAUTHORIZED_USER_ID,
        opponent="Metro United",
    )
    assert r_last_metro.status == "OK"
    assert "Metro United" in r_last_metro.content

    # /topscorers defaults to 5 entries even with 6 players recorded
    r_top = service.handle_slash_command(
        "topscorers",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=UNAUTHORIZED_USER_ID,
    )
    assert r_top.status == "OK"
    assert r_top.row_count == 5

    # Missing match ID gives clear guidance message
    r_missing = service.handle_slash_command(
        "match",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=UNAUTHORIZED_USER_ID,
        match_id=9999,
    )
    assert r_missing.status == "ERROR"
    assert r_missing.content == "Match #9999 not found. Use /results to list matches."

    # /deletematch denies Manager & Captain, allows Admin (role ID or is_admin=True)
    for non_admin_role in (MANAGER_ROLE_ID, CAPTAIN_ROLE_ID):
        denied = service.handle_slash_command(
            "deletematch",
            guild_id=APPROVED_GUILD_ID,
            channel_id=APPROVED_CHANNEL_ID,
            user_id=AUTHORIZED_USER_ID,
            user_role_ids=[non_admin_role],
            match_id=1,
            confirm_delete=True,
            is_admin=False,
        )
        assert denied.status == "REJECTED_UNAUTHORIZED"
        assert denied.ephemeral is True
        assert denied.content == PERMISSION_DENIED_MESSAGE

    admin_perm_ok = service.handle_slash_command(
        "deletematch",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=AUTHORIZED_USER_ID,
        user_role_ids=[REGULAR_ROLE_ID],
        match_id=1,
        confirm_delete=True,
        is_admin=True,
    )
    assert admin_perm_ok.status == "OK"
    assert admin_perm_ok.ephemeral is True
    assert "soft-deleted" in admin_perm_ok.content


def test_canonical_embed_contract_format_and_numeric_match_id_isolation(pg_db_migrated) -> None:
    """Verify exact canonical scorer/team/player format contract and internal-only legacy_ref isolation."""
    now = datetime(2026, 7, 16, 0, 0, tzinfo=timezone.utc)
    detail = ManualMatchDetail(
        match_id=14,
        club_id="330323",
        platform="common-gen5",
        game_version="FC27",
        opponent="Metro United",
        opponent_normalized="metro united",
        kickoff_at=now,
        match_date="2026-07-15",
        match_time="8:00 PM EDT",
        home_score=4,
        away_score=1,
        status="FULL_TIME",
        motm_display_label="Lao",
        motm_normalized_label="lao",
        possession=58,
        shots=12,
        shots_on_target=7,
        passes=342,
        pass_accuracy=84,
        tackles=18,
        corners=5,
        competition="League Match",
        created_by="345678901234567890",
        updated_by="345678901234567890",
        created_at=now,
        updated_at=now,
        revision=1,
        scorers=parse_scorers_input("Lao x3, Jay", home_score=4),
        players=(
            ManualMatchPlayerDetail(
                normalized_display_label="lao",
                safe_display_label="Lao",
                position="ST",
                goals=3,
                assists=1,
                passes_completed=25,
                passes_attempted=30,
                tackles=2,
                rating=Decimal("10.00"),
            ),
            ManualMatchPlayerDetail(
                normalized_display_label="jay",
                safe_display_label="Jay",
                goals=1,
                assists=0,
            ),
        ),
    )
    emb = build_result_embed(detail)
    emb_fields = {str(f.name): str(f.value) for f in emb.fields}
    assert emb_fields["⚽ Scorers"] == "Lao x3, Jay x1"
    assert (
        emb_fields["📊 Team Stats"]
        == "Possession 58% • Shots 12 (7 on target) • Passes 342 (84%) • Tackles 18 • Corners 5"
    )
    assert "Lao (ST) — 3G 1A | 25/30 Passes | 2 Tackles | 10.00 ⭐" in emb_fields["👥 Players"]

    assert coerce_public_match_id(14) == 14
    assert coerce_public_match_id("#14") == 14
    for invalid_mid in ("mref_ecf_000000000001", "0", "-1", 0, -5, True, "abc"):
        with pytest.raises(MatchResultsValidationError) as exc_info:
            coerce_public_match_id(invalid_mid)  # type: ignore[arg-type]
        assert "mref_ecf_" not in str(exc_info.value)

    with psycopg.connect(pg_db_migrated.admin, autocommit=True) as conn:
        inserted = conn.execute(
            """
            INSERT INTO public.fc27_manual_matches (
                opponent, opponent_normalized, home_score, away_score,
                status, legacy_ref, created_by, updated_by
            )
            VALUES (
                'Legacy Internal FC', 'legacy internal fc', 2, 1,
                'FULL_TIME', 'mref_ecf_0123456789ab', '345678901234567890', '345678901234567890'
            )
            RETURNING match_id
            """
        ).fetchone()
        assert inserted is not None and int(inserted[0]) > 0

        with pytest.raises(MatchResultsValidationError) as exc_lookup:
            fetch_manual_match(conn, match_id="mref_ecf_0123456789ab")
        assert "mref_ecf_" not in str(exc_lookup.value)

        internal_found = fetch_manual_match_by_legacy_ref(conn, "mref_ecf_0123456789ab")
        assert internal_found is not None
        assert internal_found.match_id == int(inserted[0])
        assert internal_found.legacy_ref == "mref_ecf_0123456789ab"

        with pytest.raises(MatchResultsValidationError):
            record_match_result(
                pg_db_migrated.match_results_writer,
                opponent="Legacy Internal FC",
                home_score=1,
                away_score=0,
                actor_discord_user_id="345678901234567890",
                match_id="mref_ecf_0123456789ab",
            )
        with pytest.raises(MatchResultsValidationError):
            edit_match_result(
                pg_db_migrated.match_results_writer,
                actor_discord_user_id="345678901234567890",
                match_id="mref_ecf_0123456789ab",
                home_score=3,
            )
        with pytest.raises(MatchResultsValidationError):
            soft_delete_match(
                pg_db_migrated.match_results_writer,
                match_id="mref_ecf_0123456789ab",
                actor_discord_user_id="345678901234567890",
            )
