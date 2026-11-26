"""Comprehensive tests for Migration 0019 provider-alias candidates, Identity Operator boundary, real CommandTree wiring, and backup/restore."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
import logging
from pathlib import Path
import types
from typing import Any

import discord
from discord import app_commands
import psycopg
from psycopg import sql
import pytest

from src import settings
from src.db_migrate import GRANTS_POLICY, migrate, verify_postconditions
from src.db_roles import GRANTS_POLICY_VERSION
from src.fc27_backup_restore import (
    EXPECTED_MIGRATION_VERSION,
    create_fc27_backup,
    verify_fc27_backup_restore,
)
from src.fc27_community_provider import (
    PublicPlayerAlias,
    PublicPlayerAliasSource,
    extract_public_player_alias,
    normalize_community_match_records,
    persist_community_provider_batch,
    resolve_public_player_alias_source,
)
from src.fc27_discord_bot import (
    ANALYTICS_GROUP_SUBCOMMANDS,
    DISCORD_PROVIDER_REQUESTS,
    MAX_DISCORD_GROUP_CHILDREN,
    P0_TOP_LEVEL_SUBCOMMANDS,
    PERMISSION_DENIED_MESSAGE,
    FC27DiscordAuthzError,
    FC27DiscordBotConfig,
    FC27DiscordBotService,
    assert_discord_output_sanitized,
    register_fc27_application_commands,
)
from src.fc27_discord_dispatcher import (
    DISPATCH_PROVIDER_REQUESTS,
    FC27DiscordDispatchConfig,
    SyntheticDiscordNotificationTransport,
    run_discord_dispatch_cycle,
)
from src.fc27_match_results import (
    DISCORD_EMBED_MAX_DESCRIPTION_CHARS,
    DISCORD_EMBED_MAX_FIELDS,
    DISCORD_EMBED_MAX_FIELD_NAME_CHARS,
    DISCORD_EMBED_MAX_FIELD_VALUE_CHARS,
    DISCORD_EMBED_MAX_FOOTER_CHARS,
    DISCORD_EMBED_MAX_TITLE_CHARS,
    DISCORD_EMBED_MAX_TOTAL_CHARS,
    EAST_COASTERS_FOOTER_TEXT,
    MatchResultsValidationError,
    ReconciliationTokenStore,
    UnresolvedPlayerSummary,
    _embed_aggregate_char_count,
    format_unresolved_players_ephemeral,
    get_match_discord_message_links,
    list_unresolved_players,
    record_match_discord_message_links,
    reject_player_alias_candidate,
    rename_roster_player,
    render_unresolved_players_embed,
    upsert_roster_player,
    verify_player_alias_candidate,
)
from src.fc27_polling_worker import (
    FC27PollingPolicy,
    FC27PollingRuntime,
    open_guarded_polling_connection,
    persist_polled_match_envelope,
    reset_default_polling_runtime,
    run_fc27_poll_cycle,
)
from src.fc27_privacy import (
    EAST_COASTERS_CLUB_ID,
    EAST_COASTERS_GAME_VERSION,
    EAST_COASTERS_PLATFORM,
    FC27ProviderError,
    PSEUDONYM_MATCH_REF_RE,
    PSEUDONYM_PLAYER_REF_RE,
    compute_alias_hmac_fingerprint,
    normalize_public_player_alias_key,
    pseudonymize_east_coasters_identifier,
    pseudonymize_match_ref,
    validate_public_player_alias_text,
)
from src.fc27_repository import (
    FC27AliasCandidateObservation,
    FC27RepositoryError,
    persist_player_alias_candidates,
    recompute_player_alias_candidate_conflicts_in_tx,
)
from src.settings import ConfigError


APPROVED_GUILD_ID = "123456789012345678"
OTHER_GUILD_ID = "923456789012345679"
RESULTS_CHANNEL_ID = "234567890123456789"
TEAM_CHAT_CHANNEL_ID = "234567890123456790"
OTHER_CHANNEL_ID = "234567890123456799"
APPLICATION_ID = "999000000000000001"

ADMIN_ROLE_ID = "666666666666666666"
IDENTITY_OPERATOR_ROLE_ID = "555555555555555555"
MANAGER_ROLE_ID = "444444444444444444"
CAPTAIN_ROLE_ID = "333333333333333333"
MEMBER_ROLE_ID = "777777777777777777"
GDEV_DECORATIVE_ROLE_ID = "777777777777777788"

ADMIN_USER_ID = "345678901234567890"
IDENTITY_OP_USER_ID = "345678901234567891"
IDENTITY_OP_USER_2_ID = "345678901234567892"
MANAGER_USER_ID = "345678901234567893"
CAPTAIN_USER_ID = "345678901234567894"
MEMBER_USER_ID = "888888888888888888"

TEST_ALIAS_HMAC_KEY = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


def _enable_all_polling_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_default_polling_runtime()
    monkeypatch.setenv("FC27_COMMUNITY_POLLING_ENABLED", "true")
    monkeypatch.setenv("FC27_COMMUNITY_PROVIDER_ENABLED", "true")
    monkeypatch.setenv("FC27_PERSISTENCE_ENABLED", "true")
    monkeypatch.setenv("FC27_ALIAS_HMAC_KEY", TEST_ALIAS_HMAC_KEY)


def _raw_community_match(
    *,
    match_id: str,
    timestamp: int,
    players: list[dict[str, Any]],
    opponent: str = "Harbor City FC",
    goals_for: int = 2,
    goals_against: int = 1,
    match_type: str = "leagueMatch",
) -> dict[str, Any]:
    res_code = "1" if goals_for > goals_against else ("2" if goals_for < goals_against else "0")
    opp_res = "2" if goals_for > goals_against else ("1" if goals_for < goals_against else "0")
    wins = 1 if goals_for > goals_against else 0
    losses = 1 if goals_for < goals_against else 0
    ties = 1 if goals_for == goals_against else 0
    return {
        "match_id": match_id,
        "match_type": match_type,
        "timestamp": timestamp,
        "clubs": [
            {
                "club_id": EAST_COASTERS_CLUB_ID,
                "club_name": "East Coasters",
                "goals": goals_for,
                "goals_against": goals_against,
                "result": res_code,
                "wins": wins,
                "ties": ties,
                "losses": losses,
                "winner_by_dnf": 0,
                "region_id": 4464981,
                "team_id": 112,
            },
            {
                "club_id": "880901",
                "club_name": opponent,
                "goals": goals_against,
                "goals_against": goals_for,
                "result": opp_res,
                "wins": losses,
                "ties": ties,
                "losses": wins,
                "winner_by_dnf": 0,
                "region_id": 4464981,
                "team_id": 115,
            },
        ],
        "players": players,
        "aggregate": {EAST_COASTERS_CLUB_ID: {"passesmade": 110}},
        "raw": {"matchId": match_id},
    }


def _player_payload(
    raw_player_id: str,
    *,
    alias_field: str | None = "player_name",
    alias_value: str | None = "Lao",
    position: str = "ST",
    goals: int = 1,
    assists: int = 0,
    mom: int = 0,
    extra_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "club_id": EAST_COASTERS_CLUB_ID,
        "player_id": raw_player_id,
        "position": position,
        "rating": 8.4,
        "goals": goals,
        "assists": assists,
        "shots": 3,
        "passes_made": 20,
        "pass_attempts": 24,
        "tackles_made": 2,
        "tackle_attempts": 3,
        "saves": 4 if position == "GK" else 0,
        "red_cards": 0,
        "mom": mom,
        "seconds_played": 5400,
        "archetype_id": 1,
        "event_mask": (1 << 1) if goals > 0 else 0,
    }
    if alias_field is not None and alias_value is not None:
        payload[alias_field] = alias_value
    if extra_fields:
        payload.update(extra_fields)
    return payload


def _bot_config(**overrides: Any) -> FC27DiscordBotConfig:
    params: dict[str, Any] = {
        "enabled": True,
        "application_id": APPLICATION_ID,
        "allowed_guild_ids": frozenset({APPROVED_GUILD_ID}),
        "allowed_channel_ids": frozenset({RESULTS_CHANNEL_ID, TEAM_CHAT_CHANNEL_ID}),
        "allowed_role_ids": frozenset({MEMBER_ROLE_ID}),
        "manager_role_ids": frozenset({MANAGER_ROLE_ID}),
        "captain_role_ids": frozenset({CAPTAIN_ROLE_ID}),
        "admin_role_ids": frozenset({ADMIN_ROLE_ID}),
        "identity_operator_role_ids": frozenset({IDENTITY_OPERATOR_ROLE_ID}),
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
        "allowed_guild_ids": frozenset({APPROVED_GUILD_ID}),
        "max_jobs_per_cycle": 5,
        "lease_minutes": 5,
        "single_flight": True,
    }
    params.update(overrides)
    return FC27DiscordDispatchConfig(**params)


class _Clock:
    def __init__(self, initial: datetime | None = None) -> None:
        self.now = initial or datetime(2026, 11, 5, 18, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class _FakeRole:
    def __init__(self, role_id: int, name: str) -> None:
        self.id = role_id
        self.name = name


class _FakeInteraction:
    def __init__(
        self,
        *,
        user_id: int,
        role_ids: Sequence[tuple[int, str]] = (),
        is_admin: bool = False,
        guild_id: int | None = int(APPROVED_GUILD_ID),
        channel_id: int = int(RESULTS_CHANNEL_ID),
        display_name: str = "Operator",
        interaction_id: int = 500001,
    ) -> None:
        self.id = interaction_id
        self.guild_id = guild_id
        self.guild = types.SimpleNamespace(id=guild_id) if guild_id is not None else None
        self.channel_id = channel_id
        self.user = types.SimpleNamespace(
            id=user_id,
            roles=[_FakeRole(rid, rname) for rid, rname in role_ids],
            guild_permissions=types.SimpleNamespace(administrator=is_admin),
            display_name=display_name,
        )
        self.sent_messages: list[dict[str, Any]] = []
        self._done = False
        self.response = types.SimpleNamespace(
            is_done=self._is_done,
            send_message=self._send_message,
            defer=self._defer,
        )
        self.followup = types.SimpleNamespace(send=self._followup_send)

    def _is_done(self) -> bool:
        return self._done

    async def _send_message(self, content: Any = None, **kwargs: Any) -> None:
        self._done = True
        self.sent_messages.append({"content": content, **kwargs})

    async def _defer(self, *, ephemeral: bool = False, thinking: bool = True) -> None:
        self._done = True
        self.sent_messages.append({"deferred": True, "ephemeral": ephemeral, "thinking": thinking})

    async def _followup_send(self, content: Any = None, **kwargs: Any) -> None:
        self.sent_messages.append({"followup": True, "content": content, **kwargs})


# ==============================================================================
# PHASE 12: Identity Operator Configuration & Authorization Matrix (31 tests)
# ==============================================================================


class TestIdentityOperatorAuthorization:
    def test_identity_operator_config_empty_dedup_and_fail_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("FC27_DISCORD_IDENTITY_OPERATOR_ROLE_IDS", raising=False)
        assert settings.get_fc27_discord_identity_operator_role_ids(strict=True) == frozenset()

        monkeypatch.setenv(
            "FC27_DISCORD_IDENTITY_OPERATOR_ROLE_IDS",
            f" {IDENTITY_OPERATOR_ROLE_ID} , {IDENTITY_OPERATOR_ROLE_ID} , 555555555555555556 ",
        )
        parsed = settings.get_fc27_discord_identity_operator_role_ids(strict=True)
        assert parsed == frozenset({IDENTITY_OPERATOR_ROLE_ID, "555555555555555556"})

        for bad_val in ("GDev", "[GDev]", "abc", "0", "-42", f"{IDENTITY_OPERATOR_ROLE_ID},GDev"):
            monkeypatch.setenv("FC27_DISCORD_IDENTITY_OPERATOR_ROLE_IDS", bad_val)
            assert settings.get_fc27_discord_identity_operator_role_ids(strict=False) == frozenset()
            with pytest.raises(ConfigError):
                settings.get_fc27_discord_identity_operator_role_ids(strict=True)

        # FC27DiscordBotConfig direct validation rejects role labels or non-positive values
        with pytest.raises((FC27DiscordAuthzError, ValueError)):
            _bot_config(identity_operator_role_ids=frozenset({"[GDev]"}))
        with pytest.raises((FC27DiscordAuthzError, ValueError)):
            _bot_config(identity_operator_role_ids=frozenset({"0"}))

    def test_identity_operator_permits_reconciliation_and_denies_admin_or_match_writes(
        self, pg_db_migrated
    ) -> None:
        clock = _Clock()
        store = ReconciliationTokenStore(clock=clock)
        player_a = pseudonymize_east_coasters_identifier("7001", club_id=EAST_COASTERS_CLUB_ID)
        player_b = pseudonymize_east_coasters_identifier("7002", club_id=EAST_COASTERS_CLUB_ID)

        persist_polled_match_envelope(
            pg_db_migrated.poll_writer,
            match_envelope=normalize_community_match_records(
                [
                    _raw_community_match(
                        match_id="930001",
                        timestamp=1762300000,
                        players=[
                            _player_payload("7001", alias_field="player_name", alias_value="Lao"),
                            _player_payload("7002", alias_field="proName", alias_value="Jay", position="CM", goals=0, assists=1),
                        ],
                    )
                ],
                alias_hmac_key=TEST_ALIAS_HMAC_KEY,
            )[0],
        )

        # Notice Identity Operator role is NOT inside allowed_role_ids (MEMBER_ROLE_ID only)
        cfg = _bot_config()
        svc = FC27DiscordBotService(
            cfg,
            db_conn_or_uri=pg_db_migrated.discord_reader,
            writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
            reconciliation_token_store=store,
            now_utc_fn=clock,
        )

        # 1. Admin allowed /unresolvedplayers
        admin_res = svc.handle_slash_command(
            "unresolvedplayers",
            guild_id=APPROVED_GUILD_ID,
            channel_id=RESULTS_CHANNEL_ID,
            user_id=ADMIN_USER_ID,
            user_role_ids=(ADMIN_ROLE_ID,),
        )
        assert admin_res.status == "OK"
        assert admin_res.ephemeral is True
        assert "Lao" in admin_res.content

        # 2. Identity Operator numeric role allowed /unresolvedplayers
        op_res = svc.handle_slash_command(
            "unresolvedplayers",
            guild_id=APPROVED_GUILD_ID,
            channel_id=RESULTS_CHANNEL_ID,
            user_id=IDENTITY_OP_USER_ID,
            user_role_ids=(IDENTITY_OPERATOR_ROLE_ID,),
        )
        assert op_res.status == "OK"
        assert op_res.ephemeral is True
        assert "T1" in op_res.content
        assert "Lao" in op_res.content
        assert player_a not in op_res.content
        assert player_b not in op_res.content

        # 3. Manager, Captain, Member denied /unresolvedplayers
        for r_id, uid in (
            (MANAGER_ROLE_ID, MANAGER_USER_ID),
            (CAPTAIN_ROLE_ID, CAPTAIN_USER_ID),
            (MEMBER_ROLE_ID, MEMBER_USER_ID),
        ):
            denied = svc.handle_slash_command(
                "unresolvedplayers",
                guild_id=APPROVED_GUILD_ID,
                channel_id=RESULTS_CHANNEL_ID,
                user_id=uid,
                user_role_ids=(r_id,),
            )
            assert denied.status != "OK"
            assert denied.ephemeral is True
            assert denied.content == PERMISSION_DENIED_MESSAGE

        # 4. [GDev] tag / role name alone grants NOTHING
        gdev_denied = svc.handle_slash_command(
            "unresolvedplayers",
            guild_id=APPROVED_GUILD_ID,
            channel_id=RESULTS_CHANNEL_ID,
            user_id=MEMBER_USER_ID,
            user_role_ids=(GDEV_DECORATIVE_ROLE_ID, "GDev", "[GDev]"),
        )
        assert gdev_denied.status != "OK"
        assert gdev_denied.ephemeral is True
        assert gdev_denied.content == PERMISSION_DENIED_MESSAGE

        # 5. Identity Operator cannot claim another actor's token or expired token or unknown token
        cross_actor_set = svc.handle_slash_command(
            "setplayer",
            guild_id=APPROVED_GUILD_ID,
            channel_id=RESULTS_CHANNEL_ID,
            user_id=IDENTITY_OP_USER_2_ID,
            user_role_ids=(IDENTITY_OPERATOR_ROLE_ID,),
            player_id="T1",
            display_name="Lao",
        )
        assert cross_actor_set.status != "OK"
        assert cross_actor_set.ephemeral is True

        unknown_token_set = svc.handle_slash_command(
            "setplayer",
            guild_id=APPROVED_GUILD_ID,
            channel_id=RESULTS_CHANNEL_ID,
            user_id=IDENTITY_OP_USER_ID,
            user_role_ids=(IDENTITY_OPERATOR_ROLE_ID,),
            player_id="T99",
            display_name="Lao",
        )
        assert unknown_token_set.status != "OK"
        assert unknown_token_set.ephemeral is True

        # 6. Identity Operator CAN confirm own unexpired token via /setplayer
        own_token_set = svc.handle_slash_command(
            "setplayer",
            guild_id=APPROVED_GUILD_ID,
            channel_id=RESULTS_CHANNEL_ID,
            user_id=IDENTITY_OP_USER_ID,
            user_role_ids=(IDENTITY_OPERATOR_ROLE_ID,),
            player_id="T1",
            display_name="Lao",
            position="ST",
        )
        assert own_token_set.status == "OK"
        assert "Lao" in own_token_set.content

        # Expired token fails
        clock.now = clock.now + timedelta(minutes=16)
        expired_set = svc.handle_slash_command(
            "setplayer",
            guild_id=APPROVED_GUILD_ID,
            channel_id=RESULTS_CHANNEL_ID,
            user_id=IDENTITY_OP_USER_ID,
            user_role_ids=(IDENTITY_OPERATOR_ROLE_ID,),
            player_id="T2",
            display_name="Jay",
        )
        assert expired_set.status != "OK"
        assert expired_set.ephemeral is True

        # 7. Identity Operator DENIED canonical plr_ecf_* /setplayer
        canonical_denied = svc.handle_slash_command(
            "setplayer",
            guild_id=APPROVED_GUILD_ID,
            channel_id=RESULTS_CHANNEL_ID,
            user_id=IDENTITY_OP_USER_ID,
            user_role_ids=(IDENTITY_OPERATOR_ROLE_ID,),
            player_id=player_b,
            display_name="Jay",
        )
        assert canonical_denied.status != "OK"
        assert canonical_denied.ephemeral is True
        assert canonical_denied.content == PERMISSION_DENIED_MESSAGE

        # 8. Identity Operator DENIED /deletematch, /matchday, /result, /editresult, /renameplayer
        denied_commands: tuple[tuple[str, dict[str, Any]], ...] = (
            ("deletematch", {"match_id": 1, "confirm_delete": True}),
            ("matchday", {"opponent": "Rival FC", "date_str": "2026-11-10"}),
            ("result", {"opponent": "Rival FC", "our_score": 2, "opponent_score": 1}),
            ("editresult", {"match_id": 1, "our_score": 3}),
            ("renameplayer", {"raw_argument": "Lao -> LaoRenamed"}),
        )
        for denied_cmd, extra_kwargs in denied_commands:
            r = svc.handle_slash_command(
                denied_cmd,
                guild_id=APPROVED_GUILD_ID,
                channel_id=RESULTS_CHANNEL_ID,
                user_id=IDENTITY_OP_USER_ID,
                user_role_ids=(IDENTITY_OPERATOR_ROLE_ID,),
                **extra_kwargs,
            )
            assert r.status != "OK", f"{denied_cmd} should fail for Identity Operator"
            assert r.ephemeral is True
            assert r.content == PERMISSION_DENIED_MESSAGE

        # 9. Preserved fallback: Manager & Captain can record match result; Admin / is_admin can /deletematch
        mgr_result = svc.handle_slash_command(
            "result",
            guild_id=APPROVED_GUILD_ID,
            channel_id=RESULTS_CHANNEL_ID,
            user_id=MANAGER_USER_ID,
            user_role_ids=(MANAGER_ROLE_ID,),
            opponent="Rival FC",
            our_score=2,
            opponent_score=0,
            interaction_id="int_mgr_01",
        )
        assert mgr_result.status == "OK"

        cap_edit = svc.handle_slash_command(
            "editresult",
            guild_id=APPROVED_GUILD_ID,
            channel_id=RESULTS_CHANNEL_ID,
            user_id=CAPTAIN_USER_ID,
            user_role_ids=(CAPTAIN_ROLE_ID,),
            match_id=1,
            our_score=3,
        )
        assert cap_edit.status == "OK"

        # Non-admin denied deletematch, guild Administrator permission (is_admin=True) allowed
        mgr_del = svc.handle_slash_command(
            "deletematch",
            guild_id=APPROVED_GUILD_ID,
            channel_id=RESULTS_CHANNEL_ID,
            user_id=MANAGER_USER_ID,
            user_role_ids=(MANAGER_ROLE_ID,),
            match_id=1,
            confirm_delete=True,
        )
        assert mgr_del.status != "OK"
        assert mgr_del.content == PERMISSION_DENIED_MESSAGE

        adm_del = svc.handle_slash_command(
            "deletematch",
            guild_id=APPROVED_GUILD_ID,
            channel_id=RESULTS_CHANNEL_ID,
            user_id=ADMIN_USER_ID,
            user_role_ids=(),
            is_admin=True,
            match_id=1,
            confirm_delete=True,
        )
        assert adm_del.status == "OK"

        # 10. DM / no-guild / cross-guild / cross-channel fail closed
        for bad_guild, bad_chan in (
            (None, RESULTS_CHANNEL_ID),
            (OTHER_GUILD_ID, RESULTS_CHANNEL_ID),
            (APPROVED_GUILD_ID, OTHER_CHANNEL_ID),
        ):
            fail_res = svc.handle_slash_command(
                "unresolvedplayers",
                guild_id=bad_guild,
                channel_id=bad_chan,
                user_id=IDENTITY_OP_USER_ID,
                user_role_ids=(IDENTITY_OPERATOR_ROLE_ID,),
            )
            assert fail_res.status != "OK"
            assert fail_res.ephemeral is True

        assert DISCORD_PROVIDER_REQUESTS == 0


# ==============================================================================
# PHASE 14: Alias Extraction, Persistence, Conflict, Promotion & Refresh (51 items)
# ==============================================================================


class TestProviderAliasCandidatePipeline:
    def test_alias_field_extraction_precedence_unicode_and_rejection_rules(self) -> None:
        # Precedence: player_name > playername > pro_name > proName > display_alias > name
        p1 = extract_public_player_alias(
            {
                "player_name": "PrimaryTag",
                "playername": "SecondTag",
                "pro_name": "ThirdTag",
                "proName": "FourthTag",
                "display_alias": "FifthTag",
                "name": "SixthTag",
            },
            alias_hmac_key=TEST_ALIAS_HMAC_KEY,
        )
        assert isinstance(p1, PublicPlayerAlias)
        assert p1.display_value == "PrimaryTag"
        assert p1.matched_field == "player_name"

        for idx, field in enumerate(
            ("playername", "pro_name", "proName", "display_alias", "name"), start=1
        ):
            res = extract_public_player_alias(
                {field: f"FieldTag_{idx}"},
                alias_hmac_key=TEST_ALIAS_HMAC_KEY,
            )
            assert res is not None
            assert res.matched_field == field
            assert res.display_value == f"FieldTag_{idx}"

        # Safe Unicode & 32-char boundary preserved with normalized lowercasing & exact HMAC parity
        uni = extract_public_player_alias(
            {"playername": "  Café_Tag-1  "},
            alias_hmac_key=TEST_ALIAS_HMAC_KEY,
        )
        assert uni is not None
        assert uni.display_value == "Café_Tag-1"
        assert uni.normalized_value == "café_tag-1"
        assert uni.hmac_fingerprint == compute_alias_hmac_fingerprint(
            "Café_Tag-1", TEST_ALIAS_HMAC_KEY, club_id=EAST_COASTERS_CLUB_ID
        )
        boundary_32 = "Tag_Boundary_32_Chars_Accepted_1"
        assert len(boundary_32) == 32
        disp_32, norm_32 = validate_public_player_alias_text(boundary_32)
        assert disp_32 == boundary_32 and norm_32 == boundary_32.lower()

        # Empty / whitespace ignored safely
        assert extract_public_player_alias({"player_name": "   "}) is None
        assert extract_public_player_alias({}) is None

        # Disallowed values fail closed
        for bad in (
            "plr_ecf_001122334455",
            "mref_ecf_001122334455",
            "@everyone",
            "<@123456789012345678>",
            "https://example.com",
            "line1\nline2",
            "bad\x00null",
            "postgresql://user:pass@localhost/db",
            "Bearer secret_tok_12345",
            "Tag_Too_Long_33_Chars_Rejected_12",
        ):
            with pytest.raises((FC27ProviderError, ValueError)):
                validate_public_player_alias_text(bad)

    def test_transactional_observations_stability_conflicts_rollback_and_zero_autopromote(
        self, monkeypatch: pytest.MonkeyPatch, pg_db_migrated, caplog: pytest.LogCaptureFixture
    ) -> None:
        _enable_all_polling_gates(monkeypatch)
        caplog.set_level(logging.DEBUG)

        # Match 1: observe HiddenTag_Lao (player 8001) & HiddenTag_Jay (player 8002)
        env1, _ = normalize_community_match_records(
            [
                _raw_community_match(
                    match_id="940001",
                    timestamp=1762400000,
                    players=[
                        _player_payload("8001", alias_field="player_name", alias_value="HiddenTag_Lao"),
                        _player_payload("8002", alias_field="pro_name", alias_value="HiddenTag_Jay", position="CM"),
                    ],
                )
            ],
            alias_hmac_key=TEST_ALIAS_HMAC_KEY,
        )
        res1 = run_fc27_poll_cycle(
            confirm_live_polling=True,
            db_conn_or_uri=pg_db_migrated.poll_writer,
            fetch_matches_fn=lambda _mt: env1,
            use_cache=False,
            use_rate_limiter=False,
        )
        assert res1.poll_status == "NEW_MATCHES_PERSISTED"

        p8001 = pseudonymize_east_coasters_identifier("8001", club_id=EAST_COASTERS_CLUB_ID)
        p8002 = pseudonymize_east_coasters_identifier("8002", club_id=EAST_COASTERS_CLUB_ID)

        with psycopg.connect(pg_db_migrated.admin) as aconn:
            row1 = aconn.execute(
                """
                SELECT public_alias, public_alias_normalized,
                       verification_status, observation_count, distinct_match_count,
                       first_seen_at = last_seen_at
                FROM public.fc27_player_alias_candidates
                WHERE player_id = %s
                """,
                (p8001,),
            ).fetchone()
            assert row1 == ("HiddenTag_Lao", "hiddentag_lao", "OBSERVED", 1, 1, True)
            assert aconn.execute("SELECT COUNT(*) FROM public.fc27_roster").fetchone() == (0,)
            assert (
                aconn.execute(
                    "SELECT COUNT(*) FROM public.fc27_discord_jobs WHERE job_type = 'refresh_roster_messages'"
                ).fetchone()
                == (0,)
            )
            # Zero raw provider payload columns exist; stored matches table has zero raw alias leakage
            assert aconn.execute(
                "SELECT opponent_name FROM public.fc27_matches"
            ).fetchall() == [("Harbor City FC",)]

        # Same-match replay (both cross-export duplicate match & idempotent batch reapply on match_id 940001)
        env1_replay, env1_replay_parts = normalize_community_match_records(
            [
                _raw_community_match(
                    match_id="940001",
                    timestamp=1762400000,
                    players=[
                        _player_payload("8001", alias_field="player_name", alias_value="HiddenTag_Lao"),
                        _player_payload("8002", alias_field="pro_name", alias_value="HiddenTag_Jay", position="CM"),
                    ],
                )
            ],
            observed_at="2026-11-01T14:00:00Z",
            alias_hmac_key=TEST_ALIAS_HMAC_KEY,
        )
        summary_replay = persist_community_provider_batch(
            env1_replay,
            match_envelopes=env1_replay_parts,
            postgres_conn_or_uri=pg_db_migrated.poll_writer,
            persist_requested=True,
            persistence_enabled=True,
            enforce_posture=True,
            emit_domain_events=False,
            export_id="exp_replay_same_match_1",
            generated_at=datetime(2026, 11, 1, 14, 0, tzinfo=UTC),
        )
        assert summary_replay.match_rows_inserted == 0
        assert summary_replay.duplicates_ignored == 1
        summary_idempotent = persist_community_provider_batch(
            env1_replay,
            match_envelopes=env1_replay_parts,
            postgres_conn_or_uri=pg_db_migrated.poll_writer,
            persist_requested=True,
            persistence_enabled=True,
            enforce_posture=True,
            emit_domain_events=False,
            export_id="exp_replay_same_match_1",
            generated_at=datetime(2026, 11, 1, 14, 0, tzinfo=UTC),
        )
        assert summary_idempotent.idempotent_duplicate is True

        with psycopg.connect(pg_db_migrated.admin) as aconn:
            row_after_same = aconn.execute(
                """
                SELECT verification_status, observation_count, distinct_match_count
                FROM public.fc27_player_alias_candidates
                WHERE player_id = %s
                """,
                (p8001,),
            ).fetchone()
            assert row_after_same == ("OBSERVED", 3, 1)

        # Match 2 (distinct match 940002): repeat observation of HiddenTag_Lao -> STABLE, first_seen_at preserved, last_seen_at updated
        env2, _ = normalize_community_match_records(
            [
                _raw_community_match(
                    match_id="940002",
                    timestamp=1762440000,
                    players=[
                        _player_payload("8001", alias_field="display_alias", alias_value="HiddenTag_Lao"),
                    ],
                )
            ],
            observed_at="2026-11-02T12:00:00Z",
            alias_hmac_key=TEST_ALIAS_HMAC_KEY,
        )
        persist_polled_match_envelope(
            pg_db_migrated.poll_writer,
            match_envelope=env2,
            generated_at=datetime(2026, 11, 2, 12, 0, tzinfo=UTC),
        )

        with psycopg.connect(pg_db_migrated.admin) as aconn:
            row2 = aconn.execute(
                """
                SELECT verification_status, observation_count, distinct_match_count,
                       last_seen_at > first_seen_at
                FROM public.fc27_player_alias_candidates
                WHERE player_id = %s
                """,
                (p8001,),
            ).fetchone()
            assert row2 == ("STABLE", 4, 2, True)

        # Match 3: Conflict cases
        # 3a) Same player (8002) appears with a SECOND distinct alias "HiddenTag_JayAlt" -> SAME_PLAYER_MULTIPLE_ALIASES
        # 3b) New player (8003) shares normalized alias "hiddentag_jayalt" -> CROSS_PLAYER_SAME_ALIAS
        p8003 = pseudonymize_east_coasters_identifier("8003", club_id=EAST_COASTERS_CLUB_ID)
        env3, _ = normalize_community_match_records(
            [
                _raw_community_match(
                    match_id="940003",
                    timestamp=1762480000,
                    players=[
                        _player_payload("8002", alias_field="name", alias_value="HiddenTag_JayAlt", position="CM"),
                        _player_payload("8003", alias_field="proName", alias_value="hiddentag_jayalt", position="GK", goals=0),
                    ],
                )
            ],
            observed_at="2026-11-03T12:00:00Z",
            alias_hmac_key=TEST_ALIAS_HMAC_KEY,
        )
        persist_polled_match_envelope(
            pg_db_migrated.poll_writer,
            match_envelope=env3,
            generated_at=datetime(2026, 11, 3, 12, 0, tzinfo=UTC),
        )

        with psycopg.connect(pg_db_migrated.admin) as aconn:
            statuses_8002 = aconn.execute(
                """
                SELECT public_alias_normalized, verification_status, conflict_status
                FROM public.fc27_player_alias_candidates
                WHERE player_id = %s
                ORDER BY public_alias_normalized ASC
                """,
                (p8002,),
            ).fetchall()
            assert all(st == "CONFLICTED" for _, st, _ in statuses_8002)
            assert any(
                cs in ("SAME_PLAYER_MULTIPLE_ALIASES", "CROSS_PLAYER_SAME_ALIAS", "BOTH", "CONFLICTED")
                for _, _, cs in statuses_8002
            )
            status_8003 = aconn.execute(
                "SELECT verification_status, conflict_status FROM public.fc27_player_alias_candidates WHERE player_id = %s",
                (p8003,),
            ).fetchone()
            assert status_8003 is not None and status_8003[0] == "CONFLICTED"
            assert status_8003[1] in ("CROSS_PLAYER_SAME_ALIAS", "BOTH", "CONFLICTED")

        # Simulated rollback removes both candidate & observation rows atomically
        with pytest.raises(FC27RepositoryError):
            with open_guarded_polling_connection(pg_db_migrated.poll_writer) as gconn:
                p_rb = pseudonymize_east_coasters_identifier("8999", club_id=EAST_COASTERS_CLUB_ID)
                mref_rb = pseudonymize_match_ref("949999", club_id=EAST_COASTERS_CLUB_ID)
                cand_row = gconn.execute(
                    """
                    INSERT INTO public.fc27_player_alias_candidates (
                        player_id, public_alias, public_alias_normalized,
                        alias_hmac_fingerprint, source_classification,
                        verification_status
                    ) VALUES (%s, 'RollbackTag', 'rollbacktag', %s, 'AUTHORIZED_MATCH_PAYLOAD', 'OBSERVED')
                    RETURNING candidate_id
                    """,
                    (p_rb, "c" * 64),
                ).fetchone()
                assert cand_row is not None
                gconn.execute(
                    """
                    INSERT INTO public.fc27_player_alias_candidate_observations (candidate_id, match_ref)
                    VALUES (%s, %s)
                    """,
                    (cand_row[0], mref_rb),
                )
                raise FC27RepositoryError("Simulated rollback")

        with psycopg.connect(pg_db_migrated.admin) as aconn:
            assert (
                aconn.execute(
                    "SELECT COUNT(*) FROM public.fc27_player_alias_candidates WHERE public_alias_normalized = 'rollbacktag'"
                ).fetchone()
                == (0,)
            )
            assert (
                aconn.execute(
                    "SELECT COUNT(*) FROM public.fc27_player_alias_candidate_observations WHERE match_ref = %s",
                    (pseudonymize_match_ref("949999", club_id=EAST_COASTERS_CLUB_ID),),
                ).fetchone()
                == (0,)
            )

        # Log leakage check: raw aliases MUST NEVER appear in logs
        all_logs = "\n".join(rec.getMessage() for rec in caplog.records)
        assert "HiddenTag_Lao" not in all_logs
        assert "HiddenTag_Jay" not in all_logs
        assert "HiddenTag_JayAlt" not in all_logs
        assert TEST_ALIAS_HMAC_KEY not in all_logs

        # Worker policy defaults remain intact
        policy = FC27PollingPolicy()
        assert policy.maximum_requests_per_cycle == 1
        assert policy.minimum_interval_seconds == 43200
        assert policy.maximum_retries == 0
        assert policy.max_provider_requests_per_24_hours == 2

    def test_reconciliation_ui_promotion_refresh_queue_and_dispatcher_in_place_edit(
        self, pg_db_migrated
    ) -> None:
        clock = _Clock()
        store = ReconciliationTokenStore(clock=clock)

        # Seed two provider matches: p_lao appears twice (STABLE), p_new appears once (OBSERVED), p_conf conflicted, p_rej rejected
        env1, _ = normalize_community_match_records(
            [
                _raw_community_match(
                    match_id="950001",
                    timestamp=1762500000,
                    players=[
                        _player_payload("9001", alias_field="player_name", alias_value="Lao", goals=2, mom=1),
                        _player_payload("9002", alias_field="proName", alias_value="JayNew", position="CM", goals=0, assists=2),
                        _player_payload("9003", alias_field="name", alias_value="ConfOne", position="CB", goals=0),
                        _player_payload("9004", alias_field="display_alias", alias_value="RejectedTag", position="GK", goals=0),
                    ],
                ),
                _raw_community_match(
                    match_id="950002",
                    timestamp=1762510000,
                    players=[
                        _player_payload("9001", alias_field="player_name", alias_value="Lao", goals=1, mom=1),
                        _player_payload("9003", alias_field="player_name", alias_value="ConfTwo", position="CB", goals=0),
                    ],
                ),
            ],
            alias_hmac_key=TEST_ALIAS_HMAC_KEY,
        )
        persist_polled_match_envelope(pg_db_migrated.poll_writer, match_envelope=env1)

        p9001 = pseudonymize_east_coasters_identifier("9001", club_id=EAST_COASTERS_CLUB_ID)
        p9004 = pseudonymize_east_coasters_identifier("9004", club_id=EAST_COASTERS_CLUB_ID)

        # Dispatch initial post_new_results cards (currently all players show as "Unnamed Player")
        transport = SyntheticDiscordNotificationTransport()
        dcfg = _dispatch_config()
        r_init = run_discord_dispatch_cycle(
            config=dcfg,
            transport=transport,
            queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
            readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
            match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
        )
        assert r_init.jobs_completed == 2
        assert r_init.messages_sent == 2
        assert r_init.team_chat_messages_sent == 2
        assert "Unnamed Player" in transport.sent_messages[0].content or "Unnamed Player" in str(
            transport.sent_messages[0].embed.fields
        )

        # Reject p9004's candidate via match_results_writer -> must be omitted from /unresolvedplayers evidence!
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            cid_9004_row = aconn.execute(
                "SELECT candidate_id FROM public.fc27_player_alias_candidates WHERE player_id = %s",
                (p9004,),
            ).fetchone()
            assert cid_9004_row is not None
            cid_9004 = int(cid_9004_row[0])
        assert (
            reject_player_alias_candidate(pg_db_migrated.match_results_writer, candidate_id=cid_9004)
            == "REJECTED"
        )

        # Check /unresolvedplayers output formatting (STABLE, OBSERVED, CONFLICTED, excluding REJECTED and plr_ecf_*)
        summaries = list_unresolved_players(
            pg_db_migrated.discord_reader,
            token_store=store,
            actor_discord_user_id=IDENTITY_OP_USER_ID,
            now=clock.now,
        )
        ephem = format_unresolved_players_ephemeral(summaries)
        embed = render_unresolved_players_embed(summaries)
        combined = ephem + "\n" + (embed.description or "") + "".join(
            f.value or "" for f in embed.fields
        )
        assert "Candidate stable — confirmation required" in combined
        assert "Candidate unverified" in combined
        assert "Conflicting candidate tags — operator review required" in combined
        assert "RejectedTag" not in combined
        assert not PSEUDONYM_PLAYER_REF_RE.search(combined)
        assert not PSEUDONYM_MATCH_REF_RE.search(combined)
        assert TEST_ALIAS_HMAC_KEY not in combined

        # Find token assigned to p9001 (the STABLE "Lao" candidate)
        lao_entry = next(e for e in summaries if "Lao" in e.candidate_aliases)
        svc = FC27DiscordBotService(
            _bot_config(),
            db_conn_or_uri=pg_db_migrated.discord_reader,
            writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
            reconciliation_token_store=store,
            now_utc_fn=clock,
        )
        promote_res = svc.handle_slash_command(
            "setplayer",
            guild_id=APPROVED_GUILD_ID,
            channel_id=RESULTS_CHANNEL_ID,
            user_id=IDENTITY_OP_USER_ID,
            user_role_ids=(IDENTITY_OPERATOR_ROLE_ID,),
            player_id=lao_entry.token,
            display_name="Lao",
            position="ST",
        )
        assert promote_res.status == "OK"
        assert "Lao" in promote_res.content

        # Verify DB state: fc27_roster created, candidate status -> PROMOTED, 1 refresh job enqueued
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            assert aconn.execute(
                "SELECT display_name, position FROM public.fc27_roster WHERE player_id = %s",
                (p9001,),
            ).fetchone() == ("Lao", "ST")
            assert aconn.execute(
                "SELECT verification_status FROM public.fc27_player_alias_candidates WHERE player_id = %s",
                (p9001,),
            ).fetchone() == ("PROMOTED",)
            refresh_jobs = aconn.execute(
                "SELECT job_id, state FROM public.fc27_discord_jobs WHERE job_type = 'refresh_roster_messages'"
            ).fetchall()
            assert len(refresh_jobs) == 1 and refresh_jobs[0][1] == "pending"

        # Same revision / idempotent second upsert does NOT duplicate active refresh job for same revision
        upsert_roster_player(
            pg_db_migrated.match_results_writer,
            player_id=p9001,
            display_name="Lao",
            position="ST",
        )
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            assert (
                aconn.execute(
                    "SELECT COUNT(*) FROM public.fc27_discord_jobs WHERE job_type = 'refresh_roster_messages' AND state = 'pending'"
                ).fetchone()
                == (1,)
            )

        # Dispatcher claims refresh job and edits BOTH linked formal & team-chat cards in place (no repost!)
        mref1 = pseudonymize_match_ref("950001", club_id=EAST_COASTERS_CLUB_ID)
        mref2 = pseudonymize_match_ref("950002", club_id=EAST_COASTERS_CLUB_ID)
        links1 = get_match_discord_message_links(pg_db_migrated.discord_reader, match_ref=mref1)
        links2 = get_match_discord_message_links(pg_db_migrated.discord_reader, match_ref=mref2)
        assert links1 is not None and links2 is not None

        r_ref = run_discord_dispatch_cycle(
            config=dcfg,
            transport=transport,
            queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
            readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
            match_results_writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
        )
        assert r_ref.jobs_completed == 1
        assert r_ref.messages_edited == 2
        assert r_ref.team_chat_messages_edited == 2
        assert r_ref.messages_sent == 0
        assert r_ref.team_chat_messages_sent == 0
        assert DISPATCH_PROVIDER_REQUESTS == 0

        # Also test verify_player_alias_candidate transition
        jay_summary = next(e for e in summaries if "JayNew" in e.candidate_aliases)
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            cid_9002_row = aconn.execute(
                "SELECT candidate_id FROM public.fc27_player_alias_candidates WHERE public_alias = 'JayNew'"
            ).fetchone()
            assert cid_9002_row is not None
            cid_9002 = int(cid_9002_row[0])
        assert verify_player_alias_candidate(pg_db_migrated.match_results_writer, candidate_id=cid_9002) == "OPERATOR_VERIFIED"
        assert jay_summary.token

    def test_migration_0019_fresh_upgrade_from_0018_and_grants_policy_20_matrix(
        self, pg_db
    ) -> None:
        # 1. Seed at 0018
        migrate(pg_db.migrator, target="0018")
        upsert_roster_player(
            pg_db.match_results_writer,
            player_id="plr_ecf_112233445566",
            display_name="ExistingPlayer",
            position="ST",
        )
        # Upgrade 0018 -> latest
        applied_0019 = migrate(pg_db.migrator)
        assert applied_0019 == [
            "0019_fc27_player_alias_candidates.sql",
            "0020_fc27_alias_auto_promotion_and_exceptions.sql",
            "0021_fc27_destination_aware_delivery_and_linkage.sql",
            "0022_fc27_provider_request_budget.sql",
            "0023_fc27_discord_backfill_control.sql",
            "0024_fc27_backfill_expiration_state_fix.sql",
            "0025_fc27_operations_status_and_alerts.sql",
            "0026_fc27_durable_service_heartbeats.sql",
            "0027_fc27_operations_alert_dispatch.sql",
            "0028_fc27_operations_alert_canary.sql",
        ]
        # Idempotent rerun
        assert migrate(pg_db.migrator) == []
        assert GRANTS_POLICY_VERSION == 29
        assert GRANTS_POLICY.version == 29

        with psycopg.connect(pg_db.admin) as aconn:
            assert verify_postconditions(aconn) == []
            max_ver_row = aconn.execute("SELECT MAX(version) FROM public.schema_migrations").fetchone()
            assert max_ver_row is not None and str(max_ver_row[0]).startswith("0028")
            assert aconn.execute(
                "SELECT version FROM public.schema_policy_versions WHERE policy = 'grants' ORDER BY version DESC LIMIT 1"
            ).fetchone() == (29,)
            # Preserved 0018 data
            assert aconn.execute(
                "SELECT display_name FROM public.fc27_roster WHERE player_id = 'plr_ecf_112233445566'"
            ).fetchone() == ("ExistingPlayer",)

            # Grants Policy 20 column & role privilege verification
            for role_name in ("fc27_discord_reader", "fc27_match_results_writer"):
                has_hmac_sel = aconn.execute(
                    "SELECT has_column_privilege(%s, 'public.fc27_player_alias_candidates', 'alias_hmac_fingerprint', 'SELECT')",
                    (role_name,),
                ).fetchone()
                assert has_hmac_sel == (False,)
                has_disp_sel = aconn.execute(
                    "SELECT has_column_privilege(%s, 'public.fc27_player_alias_candidates', 'public_alias', 'SELECT')",
                    (role_name,),
                ).fetchone()
                assert has_disp_sel == (True,)

            # Non-authorized roles have zero SELECT on any column of both 0019 tables
            for blocked_role in ("fc27_app", "fc27_dispatch", "fc27_relay", "fc27_player_dev_writer", "public"):
                for tbl in ("public.fc27_player_alias_candidates", "public.fc27_player_alias_candidate_observations"):
                    priv = aconn.execute(
                        "SELECT has_any_column_privilege(%s, %s, 'SELECT')",
                        (blocked_role, tbl),
                    ).fetchone()
                    assert priv == (False,), f"{blocked_role} must not have SELECT on {tbl}"

        # Runtime check that discord_reader and match_results_writer fail when querying alias_hmac_fingerprint
        for r_uri in (pg_db.discord_reader, pg_db.match_results_writer):
            with psycopg.connect(r_uri, autocommit=True) as rconn:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    rconn.execute("SELECT alias_hmac_fingerprint FROM public.fc27_player_alias_candidates")

        # poll_writer cannot DELETE from either 0019 table
        with open_guarded_polling_connection(pg_db.poll_writer) as pw:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                pw.execute("DELETE FROM public.fc27_player_alias_candidates")
            pw.rollback()
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                pw.execute("DELETE FROM public.fc27_player_alias_candidate_observations")
            pw.rollback()


# ==============================================================================
# PHASE 15: Real discord.py app_commands.CommandTree Wiring (15 checks)
# ==============================================================================


class TestRealDiscordCommandTreeIdentityOperator:
    def test_real_command_tree_wiring_and_identity_operator_callbacks(
        self, pg_db_migrated
    ) -> None:
        clock = _Clock()
        store = ReconciliationTokenStore(clock=clock)
        persist_polled_match_envelope(
            pg_db_migrated.poll_writer,
            match_envelope=normalize_community_match_records(
                [
                    _raw_community_match(
                        match_id="960001",
                        timestamp=1762600000,
                        players=[_player_payload("9101", alias_field="player_name", alias_value="TreeTag")],
                    )
                ],
                alias_hmac_key=TEST_ALIAS_HMAC_KEY,
            )[0],
        )

        cfg = _bot_config()
        svc = FC27DiscordBotService(
            cfg,
            db_conn_or_uri=pg_db_migrated.discord_reader,
            writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
            reconciliation_token_store=store,
            now_utc_fn=clock,
        )

        async def _run_tree_checks() -> None:
            intents = discord.Intents.none()
            intents.guilds = True
            client = discord.Client(intents=intents, application_id=int(APPLICATION_ID))
            tree = app_commands.CommandTree(client)
            summary = register_fc27_application_commands(
                tree,
                svc,
                allowed_guild_ids=cfg.allowed_guild_ids,
            )
            assert summary.global_command_names == ()
            assert summary.total_guild_top_level_commands == 14
            assert len(summary.group_subcommands) == len(ANALYTICS_GROUP_SUBCOMMANDS) == 13
            assert len(summary.group_subcommands) <= MAX_DISCORD_GROUP_CHILDREN == 25
            assert set(summary.top_level_guild_commands) == set(P0_TOP_LEVEL_SUBCOMMANDS)

            raw_guild_cmds = tree.get_commands(guild=discord.Object(id=int(APPROVED_GUILD_ID)))
            assert len(raw_guild_cmds) == 14
            guild_cmds = {
                cmd.name: cmd
                for cmd in raw_guild_cmds
                if isinstance(cmd, app_commands.Command)
            }
            assert len(guild_cmds) == len(P0_TOP_LEVEL_SUBCOMMANDS)

            # Identity Operator -> /unresolvedplayers succeeds
            i_op_unres = _FakeInteraction(
                user_id=int(IDENTITY_OP_USER_ID),
                role_ids=[(int(IDENTITY_OPERATOR_ROLE_ID), "Identity Operator")],
            )
            await guild_cmds["unresolvedplayers"].callback(i_op_unres)
            assert i_op_unres.sent_messages[-1]["ephemeral"] is True
            assert "TreeTag" in (i_op_unres.sent_messages[-1]["content"] or "")

            # [GDev] decorative role name -> /unresolvedplayers denied cleanly
            i_gdev = _FakeInteraction(
                user_id=int(MEMBER_USER_ID),
                role_ids=[(int(GDEV_DECORATIVE_ROLE_ID), "[GDev]")],
                display_name="[GDev] Decorative",
            )
            await guild_cmds["unresolvedplayers"].callback(i_gdev)
            assert i_gdev.sent_messages[-1]["ephemeral"] is True
            assert i_gdev.sent_messages[-1]["content"] == PERMISSION_DENIED_MESSAGE

            # Identity Operator -> token /setplayer succeeds
            i_op_set = _FakeInteraction(
                user_id=int(IDENTITY_OP_USER_ID),
                role_ids=[(int(IDENTITY_OPERATOR_ROLE_ID), "Identity Operator")],
            )
            await guild_cmds["setplayer"].callback(i_op_set, "T1", "TreeTag", "ST", None)
            assert "TreeTag" in (i_op_set.sent_messages[-1]["content"] or "")

            # Identity Operator -> /deletematch & /result & /matchday & /editresult & /renameplayer & canonical /setplayer denied
            p_cand = pseudonymize_east_coasters_identifier("9101", club_id=EAST_COASTERS_CLUB_ID)
            for cmd_name, args in (
                ("setplayer", (p_cand, "TreeTag", "ST", None)),
                ("deletematch", (1, True, None, None)),
                ("result", ("Opp FC", 2, 1)),
                ("matchday", ("Opp FC", "2026-11-12")),
                ("editresult", (1, "Opp FC")),
                ("renameplayer", ("TreeTag", "TreeRenamed")),
            ):
                inter = _FakeInteraction(
                    user_id=int(IDENTITY_OP_USER_ID),
                    role_ids=[(int(IDENTITY_OPERATOR_ROLE_ID), "Identity Operator")],
                )
                await guild_cmds[cmd_name].callback(inter, *args)
                assert inter.sent_messages[-1]["ephemeral"] is True
                assert inter.sent_messages[-1]["content"] == PERMISSION_DENIED_MESSAGE

            # Manager -> /result succeeds, /unresolvedplayers denied
            i_mgr_res = _FakeInteraction(
                user_id=int(MANAGER_USER_ID),
                role_ids=[(int(MANAGER_ROLE_ID), "Manager")],
            )
            await guild_cmds["result"].callback(i_mgr_res, "ClubTwo FC", 1, 0)
            assert i_mgr_res.sent_messages[-1].get("embeds")

            i_mgr_unres = _FakeInteraction(
                user_id=int(MANAGER_USER_ID),
                role_ids=[(int(MANAGER_ROLE_ID), "Manager")],
            )
            await guild_cmds["unresolvedplayers"].callback(i_mgr_unres)
            assert i_mgr_unres.sent_messages[-1]["ephemeral"] is True
            assert i_mgr_unres.sent_messages[-1]["content"] == PERMISSION_DENIED_MESSAGE

            # Admin -> /deletematch succeeds
            i_adm_del = _FakeInteraction(
                user_id=int(ADMIN_USER_ID),
                role_ids=[(int(ADMIN_ROLE_ID), "Admin")],
            )
            await guild_cmds["deletematch"].callback(i_adm_del, 1, True, None, None)
            assert i_adm_del.sent_messages[-1]["ephemeral"] is True
            assert i_adm_del.sent_messages[-1]["content"] != PERMISSION_DENIED_MESSAGE

        asyncio.run(_run_tree_checks())
        assert DISCORD_PROVIDER_REQUESTS == 0


# ==============================================================================
# PHASE 17: Backup & Restore Verification with Alias Candidates
# ==============================================================================


class TestBackupRestoreWithAliasCandidates:
    def test_backup_and_restore_preserves_candidates_observations_statuses_and_conflicts(
        self, tmp_path: Path, pg_db_migrated, pg_admin_uri: str
    ) -> None:
        env, _ = normalize_community_match_records(
            [
                _raw_community_match(
                    match_id="970001",
                    timestamp=1762700000,
                    players=[
                        _player_payload("9201", alias_field="player_name", alias_value="BakStable"),
                        _player_payload("9202", alias_field="proName", alias_value="BakObs", position="CM"),
                        _player_payload("9203", alias_field="name", alias_value="BakConfA", position="CB", goals=0),
                    ],
                ),
                _raw_community_match(
                    match_id="970002",
                    timestamp=1762710000,
                    players=[
                        _player_payload("9201", alias_field="player_name", alias_value="BakStable"),
                        _player_payload("9203", alias_field="player_name", alias_value="BakConfB", position="CB", goals=0),
                    ],
                ),
            ],
            alias_hmac_key=TEST_ALIAS_HMAC_KEY,
        )
        persist_polled_match_envelope(pg_db_migrated.poll_writer, match_envelope=env)

        p9201 = pseudonymize_east_coasters_identifier("9201", club_id=EAST_COASTERS_CLUB_ID)
        upsert_roster_player(
            pg_db_migrated.match_results_writer,
            player_id=p9201,
            display_name="BakStable",
            position="ST",
        )

        # Also add a REJECTED observation directly via repository
        p9204 = pseudonymize_east_coasters_identifier("9204", club_id=EAST_COASTERS_CLUB_ID)
        mref3 = pseudonymize_match_ref("970003", club_id=EAST_COASTERS_CLUB_ID)
        persist_player_alias_candidates(
            pg_db_migrated.poll_writer,
            [
                FC27AliasCandidateObservation(
                    player_id=p9204,
                    public_alias="BakRej",
                    public_alias_normalized="bakrej",
                    alias_hmac_fingerprint=compute_alias_hmac_fingerprint("BakRej", TEST_ALIAS_HMAC_KEY),
                    matched_field="display_alias",
                    source_classification="USER_SUPPLIED_EXPORT",
                    match_ref=mref3,
                )
            ],
            enforce_posture=False,
        )
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            cid_9204_row = aconn.execute(
                "SELECT candidate_id FROM public.fc27_player_alias_candidates WHERE player_id = %s",
                (p9204,),
            ).fetchone()
            assert cid_9204_row is not None
            cid_9204 = int(cid_9204_row[0])
        reject_player_alias_candidate(pg_db_migrated.match_results_writer, candidate_id=cid_9204)

        backup_dir = tmp_path / ".local-data" / "backups"
        archive_path, manifest_path, manifest = create_fc27_backup(
            pg_db_migrated.admin,
            backup_dir=backup_dir,
        )
        assert manifest.migration_version == EXPECTED_MIGRATION_VERSION
        assert manifest.table_counts.schema_migrations == 27
        assert manifest.table_counts.fc27_roster == 1
        assert manifest.table_counts.fc27_player_alias_candidates >= 4
        assert manifest.table_counts.fc27_player_alias_candidate_observations >= 5

        report = verify_fc27_backup_restore(
            manifest_path=manifest_path,
            archive_path=archive_path,
            admin_db_uri=pg_admin_uri,
        )
        assert report.restore_verification == "PASS"
        assert (
            report.restored_counts.fc27_player_alias_candidates
            == manifest.table_counts.fc27_player_alias_candidates
        )
        assert (
            report.restored_counts.fc27_player_alias_candidate_observations
            == manifest.table_counts.fc27_player_alias_candidate_observations
        )

        # Also test direct restore into modified DB resets noise & preserves candidate statuses/conflicts/promoted state
        with psycopg.connect(pg_db_migrated.admin, autocommit=True) as aconn:
            aconn.execute("DELETE FROM public.fc27_player_alias_candidates WHERE player_id = %s", (p9204,))
        direct_report = verify_fc27_backup_restore(
            manifest_path=manifest_path,
            archive_path=archive_path,
            disposable_target_db_uri=pg_db_migrated.admin,
        )
        assert direct_report.restore_verification == "PASS"
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            restored_statuses = aconn.execute(
                """
                SELECT public_alias, verification_status, conflict_status
                FROM public.fc27_player_alias_candidates
                ORDER BY public_alias ASC
                """
            ).fetchall()
        assert ("BakConfA", "CONFLICTED", "SAME_PLAYER_MULTIPLE_ALIASES") in restored_statuses
        assert ("BakObs", "OBSERVED", "NONE") in restored_statuses
        assert ("BakRej", "REJECTED", "NONE") in restored_statuses
        assert ("BakStable", "PROMOTED", "NONE") in restored_statuses


# ==============================================================================
# POST-MERGE PR #125 REMEDIATION REGRESSION SUITES (Phases 2-7)
# ==============================================================================


class TestSharedAliasSourcePrecedence:
    """Phase 2 (Thread 4608588139): Single authoritative alias-source precedence across candidates and HMACs."""

    def test_precedence_top_level_vs_raw_multiple_fields_and_invalid_fallthrough(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)

        # 1. Identical top-level and raw values
        identical_rec = _raw_community_match(
            match_id="980001",
            timestamp=1763000000,
            players=[
                _player_payload(
                    "9301",
                    alias_field="player_name",
                    alias_value="SameTag",
                    extra_fields={"raw": {"playername": "SameTag"}},
                )
            ],
        )
        env_ident, _ = normalize_community_match_records(
            [identical_rec], alias_hmac_key=TEST_ALIAS_HMAC_KEY
        )
        expected_hmac = compute_alias_hmac_fingerprint("SameTag", TEST_ALIAS_HMAC_KEY)
        assert env_ident.alias_candidates[0].public_alias == "SameTag"
        assert env_ident.alias_candidates[0].alias_hmac_fingerprint == expected_hmac
        assert env_ident.alias_fingerprints[0].gamertag_hmac == expected_hmac

        # 2. Differing top-level (`display_alias`) and `raw.playername`: ALL top-level fields beat `raw.*`
        diff_player = {
            "display_alias": "TopDisplayTag",
            "raw": {"playername": "RawInternalTag"},
        }
        src_diff = resolve_public_player_alias_source(diff_player)
        assert isinstance(src_diff, PublicPlayerAliasSource)
        assert src_diff.display_value == "TopDisplayTag"
        assert src_diff.matched_field == "display_alias"
        assert src_diff.source_scope == "top_level"

        diff_match = _raw_community_match(
            match_id="980002",
            timestamp=1763000300,
            players=[
                _player_payload(
                    "9302",
                    alias_field="display_alias",
                    alias_value="TopDisplayTag",
                    extra_fields={"raw": {"playername": "RawInternalTag"}},
                )
            ],
        )
        env_diff, _ = normalize_community_match_records(
            [diff_match], alias_hmac_key=TEST_ALIAS_HMAC_KEY
        )
        top_hmac = compute_alias_hmac_fingerprint("TopDisplayTag", TEST_ALIAS_HMAC_KEY)
        assert len(env_diff.alias_candidates) == 1
        assert len(env_diff.alias_fingerprints) == 1
        assert env_diff.alias_candidates[0].public_alias == "TopDisplayTag"
        assert env_diff.alias_candidates[0].matched_field == "display_alias"
        assert env_diff.alias_candidates[0].alias_hmac_fingerprint == top_hmac
        assert env_diff.alias_fingerprints[0].gamertag_hmac == top_hmac

        # 3 & 4. Multiple top-level fields & multiple raw fields precedence
        multi_top = {
            "player_name": "Top1",
            "playername": "Top2",
            "pro_name": "Top3",
            "proName": "Top4",
            "display_alias": "Top5",
            "name": "Top6",
            "raw": {"player_name": "Raw1"},
        }
        src_top = resolve_public_player_alias_source(multi_top)
        assert src_top is not None
        assert src_top.display_value == "Top1"

        multi_raw = {
            "player_name": "   ",
            "display_alias": "",
            "raw": {
                "playername": "RawSecond",
                "pro_name": "RawThird",
                "proName": "RawFourth",
                "display_alias": "RawFifth",
                "name": "RawSixth",
            },
        }
        src_multi_raw = resolve_public_player_alias_source(multi_raw)
        assert src_multi_raw is not None
        assert src_multi_raw.display_value == "RawSecond"
        assert src_multi_raw.source_scope == "raw"

        # 5. Top-level empty / None and raw populated -> both candidate & HMAC use raw
        empty_top_match = _raw_community_match(
            match_id="980003",
            timestamp=1763000600,
            players=[
                _player_payload(
                    "9303",
                    alias_field="player_name",
                    alias_value="   ",
                    extra_fields={"display_alias": None, "raw": {"proName": "RawPopulatedTag"}},
                )
            ],
        )
        env_empty_top, _ = normalize_community_match_records(
            [empty_top_match], alias_hmac_key=TEST_ALIAS_HMAC_KEY
        )
        raw_hmac = compute_alias_hmac_fingerprint("RawPopulatedTag", TEST_ALIAS_HMAC_KEY)
        assert env_empty_top.alias_candidates[0].public_alias == "RawPopulatedTag"
        assert env_empty_top.alias_candidates[0].alias_hmac_fingerprint == raw_hmac
        assert env_empty_top.alias_fingerprints[0].gamertag_hmac == raw_hmac

        # 6 & 9. Invalid higher-priority value + valid lower-priority value -> consistent fall-through for BOTH
        invalid_high_match = _raw_community_match(
            match_id="980004",
            timestamp=1763000900,
            players=[
                _player_payload(
                    "9304",
                    alias_field="player_name",
                    alias_value="https://evil.example",
                    extra_fields={"raw": {"playername": "FallbackValidTag"}},
                ),
                _player_payload(
                    "9305",
                    alias_field="player_name",
                    alias_value="@everyone",
                    extra_fields={"raw": {"playername": "plr_ecf_001122334455"}},
                ),
            ],
        )
        env_fall, _ = normalize_community_match_records(
            [invalid_high_match], alias_hmac_key=TEST_ALIAS_HMAC_KEY
        )
        # Player 9304 falls through consistently to FallbackValidTag; player 9305 has no valid source -> emits NEITHER
        fall_hmac = compute_alias_hmac_fingerprint("FallbackValidTag", TEST_ALIAS_HMAC_KEY)
        assert len(env_fall.alias_candidates) == 1
        assert len(env_fall.alias_fingerprints) == 1
        assert env_fall.alias_candidates[0].public_alias == "FallbackValidTag"
        assert env_fall.alias_candidates[0].alias_hmac_fingerprint == fall_hmac
        assert env_fall.alias_fingerprints[0].gamertag_hmac == fall_hmac

        # 7 & 10. Log leakage check across all normalized matches
        all_logs = "\n".join(rec.getMessage() for rec in caplog.records)
        for sensitive_tag in (
            "SameTag",
            "TopDisplayTag",
            "RawInternalTag",
            "RawPopulatedTag",
            "FallbackValidTag",
        ):
            assert sensitive_tag not in all_logs
        assert TEST_ALIAS_HMAC_KEY not in all_logs

    def test_differing_top_level_and_raw_replay_updates_fingerprint_safely_in_postgres(
        self, monkeypatch: pytest.MonkeyPatch, pg_db_migrated
    ) -> None:
        _enable_all_polling_gates(monkeypatch)
        # Match 1: player 9310 has display_alias="TagAlpha" and raw.playername="RawUnused"
        m1 = _raw_community_match(
            match_id="980101",
            timestamp=1763010000,
            players=[
                _player_payload(
                    "9310",
                    alias_field="display_alias",
                    alias_value="TagAlpha",
                    extra_fields={"raw": {"playername": "RawUnused"}},
                )
            ],
        )
        env1, _ = normalize_community_match_records([m1], alias_hmac_key=TEST_ALIAS_HMAC_KEY)
        res1 = persist_polled_match_envelope(pg_db_migrated.poll_writer, match_envelope=env1)
        assert res1.match_rows_inserted == 1

        # Replay exact same match 1 -> deterministic & idempotent
        env1_rep, _ = normalize_community_match_records([m1], alias_hmac_key=TEST_ALIAS_HMAC_KEY)
        assert env1_rep.alias_candidates == env1.alias_candidates
        assert env1_rep.alias_fingerprints == env1.alias_fingerprints
        res1_rep = persist_polled_match_envelope(pg_db_migrated.poll_writer, match_envelope=env1_rep)
        assert res1_rep.duplicates_ignored == 1

        # Match 2: player 9310 changes display_alias to "TagBeta" while raw.playername stays "RawUnused"
        # Because both extractors select display_alias="TagBeta", candidate_conflict_pairs includes TagBeta's HMAC!
        m2 = _raw_community_match(
            match_id="980102",
            timestamp=1763020000,
            players=[
                _player_payload(
                    "9310",
                    alias_field="display_alias",
                    alias_value="TagBeta",
                    extra_fields={"raw": {"playername": "RawUnused"}},
                )
            ],
        )
        env2, _ = normalize_community_match_records([m2], alias_hmac_key=TEST_ALIAS_HMAC_KEY)
        res2 = persist_polled_match_envelope(pg_db_migrated.poll_writer, match_envelope=env2)
        assert res2.match_rows_inserted == 1

        p9310 = pseudonymize_east_coasters_identifier("9310", club_id=EAST_COASTERS_CLUB_ID)
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            rows = aconn.execute(
                """
                SELECT public_alias, verification_status, conflict_status
                FROM public.fc27_player_alias_candidates
                WHERE player_id = %s
                ORDER BY public_alias ASC
                """,
                (p9310,),
            ).fetchall()
            assert rows == [
                ("TagAlpha", "CONFLICTED", "SAME_PLAYER_MULTIPLE_ALIASES"),
                ("TagBeta", "CONFLICTED", "SAME_PLAYER_MULTIPLE_ALIASES"),
            ]


class TestCompleteConflictComponentRecomputation:
    """Phase 3 (Thread 4608588175): Transitive connected-component conflict recomputation."""

    def test_transitive_connected_components_replay_rejection_promotion_and_rollback(
        self, pg_db_migrated, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        pA = pseudonymize_east_coasters_identifier("9401", club_id=EAST_COASTERS_CLUB_ID)
        pB = pseudonymize_east_coasters_identifier("9402", club_id=EAST_COASTERS_CLUB_ID)
        mref = lambda idx: pseudonymize_match_ref(f"98100{idx}", club_id=EAST_COASTERS_CLUB_ID)

        def _obs(pid: str, alias: str, match_idx: int) -> FC27AliasCandidateObservation:
            disp, norm = validate_public_player_alias_text(alias)
            return FC27AliasCandidateObservation(
                player_id=pid,
                public_alias=disp,
                public_alias_normalized=norm,
                alias_hmac_fingerprint=compute_alias_hmac_fingerprint(disp, TEST_ALIAS_HMAC_KEY),
                matched_field="player_name",
                source_classification="AUTHORIZED_MATCH_PAYLOAD",
                match_ref=mref(match_idx),
            )

        # Build 4-node connected component: A/X (>=2 matches), A/Y, B/Y, B/Z
        persist_player_alias_candidates(
            pg_db_migrated.poll_writer,
            [
                _obs(pA, "AliasX", 1),
                _obs(pA, "AliasX", 2),
                _obs(pA, "AliasY", 3),
                _obs(pB, "AliasY", 4),
                _obs(pB, "AliasZ", 5),
            ],
            enforce_posture=False,
        )

        with psycopg.connect(pg_db_migrated.admin) as aconn:
            cids = {
                (pid, norm): (cid, ver, conf)
                for cid, pid, norm, ver, conf in aconn.execute(
                    """
                    SELECT candidate_id, player_id, public_alias_normalized, verification_status, conflict_status
                    FROM public.fc27_player_alias_candidates
                    WHERE player_id = ANY(%s)
                    """,
                    ([pA, pB],),
                ).fetchall()
            }

        assert cids[(pA, "aliasx")][1:] == ("CONFLICTED", "SAME_PLAYER_MULTIPLE_ALIASES")
        assert cids[(pA, "aliasy")][1:] == ("CONFLICTED", "BOTH")
        assert cids[(pB, "aliasy")][1:] == ("CONFLICTED", "BOTH")
        assert cids[(pB, "aliasz")][1:] == ("CONFLICTED", "SAME_PLAYER_MULTIPLE_ALIASES")

        # Cannot verify a conflicted candidate
        with pytest.raises(MatchResultsValidationError):
            verify_player_alias_candidate(pg_db_migrated.match_results_writer, candidate_id=cids[(pA, "aliasx")][0])

        # Reject B/Z (outer edge of 4-node component): B/Y stays CROSS_PLAYER_SAME_ALIAS with A/Y,
        # A/Y stays BOTH, A/X stays SAME_PLAYER_MULTIPLE_ALIASES!
        assert reject_player_alias_candidate(
            pg_db_migrated.match_results_writer, candidate_id=cids[(pB, "aliasz")][0]
        ) == "REJECTED"

        # Now add B/X (forming A/X, A/Y, B/X, B/Y) and reject B/X -> replay B/X via poll_writer!
        persist_player_alias_candidates(
            pg_db_migrated.poll_writer,
            [_obs(pB, "AliasX", 6)],
            enforce_posture=False,
        )
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            cid_bx_row = aconn.execute(
                "SELECT candidate_id FROM public.fc27_player_alias_candidates WHERE player_id = %s AND public_alias_normalized = 'aliasx'",
                (pB,),
            ).fetchone()
            assert cid_bx_row is not None
            cid_bx = int(cid_bx_row[0])

        # Rejecting B/X still leaves A/X <-> A/Y active! A/X MUST stay CONFLICTED (never falsely STABLE)
        # and verification of A/X MUST continue to fail!
        reject_player_alias_candidate(pg_db_migrated.match_results_writer, candidate_id=cid_bx)
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            row_ax_after = aconn.execute(
                "SELECT verification_status, conflict_status FROM public.fc27_player_alias_candidates WHERE candidate_id = %s",
                (cids[(pA, "aliasx")][0],),
            ).fetchone()
            assert row_ax_after == ("CONFLICTED", "SAME_PLAYER_MULTIPLE_ALIASES")
        with pytest.raises(MatchResultsValidationError):
            verify_player_alias_candidate(pg_db_migrated.match_results_writer, candidate_id=cids[(pA, "aliasx")][0])

        # Replaying the rejected B/X observation alone (seeds only B & 'aliasx') expands transitively A -> 'aliasy'
        # and keeps A/X CONFLICTED!
        res_bx_replay = persist_player_alias_candidates(
            pg_db_migrated.poll_writer,
            [_obs(pB, "AliasX", 7)],
            enforce_posture=False,
        )
        assert res_bx_replay.updated_count == 1
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            assert aconn.execute(
                "SELECT verification_status, conflict_status FROM public.fc27_player_alias_candidates WHERE candidate_id = %s",
                (cids[(pA, "aliasx")][0],),
            ).fetchone() == ("CONFLICTED", "SAME_PLAYER_MULTIPLE_ALIASES")
            assert aconn.execute(
                "SELECT verification_status, conflict_status FROM public.fc27_player_alias_candidates WHERE candidate_id = %s",
                (cid_bx,),
            ).fetchone() == ("REJECTED", "NONE")

        # Repeated idempotent recomputation preserves identical statuses
        with psycopg.connect(pg_db_migrated.poll_writer) as pw_conn, pw_conn.cursor() as cur:
            st1 = recompute_player_alias_candidate_conflicts_in_tx(
                cur, seed_player_ids=(pA,), seed_normalized_aliases=("aliasx",)
            )
            st2 = recompute_player_alias_candidate_conflicts_in_tx(
                cur, seed_player_ids=(pB,), seed_normalized_aliases=("aliasy",)
            )
            pw_conn.commit()
        assert st1 == st2

        # Rollback when an error occurs inside a mutation transaction does not leak partial conflict updates
        with pytest.raises(RuntimeError):
            with psycopg.connect(pg_db_migrated.match_results_writer) as wconn:
                with wconn.transaction():
                    with wconn.cursor() as cur:
                        cur.execute(
                            "UPDATE public.fc27_player_alias_candidates SET verification_status = 'REJECTED', conflict_status = 'NONE' WHERE candidate_id = %s",
                            (cids[(pA, "aliasy")][0],),
                        )
                        recompute_player_alias_candidate_conflicts_in_tx(
                            cur, seed_player_ids=(pA,), seed_normalized_aliases=("aliasy",)
                        )
                        raise RuntimeError("abort_tx")

        # Rejecting A/Y clears A/X to STABLE (distinct_match_count=2) and B/Y to OBSERVED (distinct_match_count=1)
        reject_player_alias_candidate(pg_db_migrated.match_results_writer, candidate_id=cids[(pA, "aliasy")][0])
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            assert aconn.execute(
                "SELECT verification_status, conflict_status FROM public.fc27_player_alias_candidates WHERE candidate_id = %s",
                (cids[(pA, "aliasx")][0],),
            ).fetchone() == ("STABLE", "NONE")
            assert aconn.execute(
                "SELECT verification_status, conflict_status FROM public.fc27_player_alias_candidates WHERE candidate_id = %s",
                (cids[(pB, "aliasy")][0],),
            ).fetchone() == ("OBSERVED", "NONE")

        # Now A/X can be verified & promoted via upsert_roster_player, and remains PROMOTED even if sibling added later
        assert verify_player_alias_candidate(
            pg_db_migrated.match_results_writer, candidate_id=cids[(pA, "aliasx")][0]
        ) == "OPERATOR_VERIFIED"
        upsert_roster_player(
            pg_db_migrated.match_results_writer,
            player_id=pA,
            display_name="AliasX",
            position="ST",
        )
        persist_player_alias_candidates(
            pg_db_migrated.poll_writer,
            [_obs(pA, "AliasNew", 8)],
            enforce_posture=False,
        )
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            assert aconn.execute(
                "SELECT verification_status FROM public.fc27_player_alias_candidates WHERE candidate_id = %s",
                (cids[(pA, "aliasx")][0],),
            ).fetchone() == ("PROMOTED",)
            assert aconn.execute(
                "SELECT verification_status, conflict_status FROM public.fc27_player_alias_candidates WHERE player_id = %s AND public_alias_normalized = 'aliasnew'",
                (pA,),
            ).fetchone() == ("CONFLICTED", "SAME_PLAYER_MULTIPLE_ALIASES")

        # Zero raw alias leakage in logs
        all_logs = "\n".join(rec.getMessage() for rec in caplog.records)
        for alias in ("AliasX", "AliasY", "AliasZ", "AliasNew"):
            assert alias not in all_logs


class TestPreservedTokenAuthorizationContract:
    """Phase 4 (Thread 4608588212): Preserve Manager/Captain/Admin token /setplayer alongside Identity Operator."""

    def test_token_authorization_matrix_for_manager_captain_admin_identity_operator_and_member(
        self, pg_db_migrated
    ) -> None:
        clock = _Clock()
        store = ReconciliationTokenStore(clock=clock)
        env, _ = normalize_community_match_records(
            [
                _raw_community_match(
                    match_id="982001",
                    timestamp=1763030000,
                    players=[
                        _player_payload("9501", alias_field="player_name", alias_value="MgrTarget"),
                        _player_payload("9502", alias_field="player_name", alias_value="CapTarget", position="CM", goals=0),
                        _player_payload("9503", alias_field="player_name", alias_value="IdOpTarget", position="CB", goals=0),
                        _player_payload("9504", alias_field="player_name", alias_value="AdmTarget", position="GK", goals=0),
                    ],
                )
            ],
            alias_hmac_key=TEST_ALIAS_HMAC_KEY,
        )
        persist_polled_match_envelope(pg_db_migrated.poll_writer, match_envelope=env)

        svc = FC27DiscordBotService(
            _bot_config(),
            db_conn_or_uri=pg_db_migrated.discord_reader,
            writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
            reconciliation_token_store=store,
            now_utc_fn=clock,
        )

        # Issue actor-bound tokens for Manager, Captain, Identity Operator, and Admin
        mgr_entries = list_unresolved_players(
            pg_db_migrated.discord_reader,
            token_store=store,
            actor_discord_user_id=MANAGER_USER_ID,
            now=clock.now,
        )
        cap_entries = list_unresolved_players(
            pg_db_migrated.discord_reader,
            token_store=store,
            actor_discord_user_id=CAPTAIN_USER_ID,
            now=clock.now,
        )
        idop_entries = list_unresolved_players(
            pg_db_migrated.discord_reader,
            token_store=store,
            actor_discord_user_id=IDENTITY_OP_USER_ID,
            now=clock.now,
        )
        adm_entries = list_unresolved_players(
            pg_db_migrated.discord_reader,
            token_store=store,
            actor_discord_user_id=ADMIN_USER_ID,
            now=clock.now,
        )

        # Member & decorative [GDev] / "GDev" role-name denied with exact ephemeral message
        for uid, roles in (
            (MEMBER_USER_ID, (MEMBER_ROLE_ID,)),
            (MEMBER_USER_ID, (GDEV_DECORATIVE_ROLE_ID, "GDev", "[GDev]")),
        ):
            r_den = svc.handle_slash_command(
                "setplayer",
                guild_id=APPROVED_GUILD_ID,
                channel_id=RESULTS_CHANNEL_ID,
                user_id=uid,
                user_role_ids=roles,
                player_id="T1",
                display_name="MemberTry",
            )
            assert r_den.status == "REJECTED_UNAUTHORIZED"
            assert r_den.ephemeral is True
            assert r_den.content == PERMISSION_DENIED_MESSAGE

        # Cross-actor token submission rejected cleanly (actor-bound enforcement preserved for Manager/Captain/Admin/Identity Operator)
        isolated_store = ReconciliationTokenStore(clock=clock)
        isolated_svc = FC27DiscordBotService(
            _bot_config(),
            db_conn_or_uri=pg_db_migrated.discord_reader,
            writer_db_conn_or_uri=pg_db_migrated.match_results_writer,
            reconciliation_token_store=isolated_store,
            now_utc_fn=clock,
        )
        cap_tok = isolated_store.issue_tokens(
            [pseudonymize_east_coasters_identifier("9502", club_id=EAST_COASTERS_CLUB_ID)],
            actor_discord_user_id=CAPTAIN_USER_ID,
            now=clock.now,
        )[0]
        cross_res = isolated_svc.handle_slash_command(
            "setplayer",
            guild_id=APPROVED_GUILD_ID,
            channel_id=RESULTS_CHANNEL_ID,
            user_id=MANAGER_USER_ID,
            user_role_ids=(MANAGER_ROLE_ID,),
            player_id=cap_tok,
            display_name="CapTarget",
        )
        assert cross_res.status == "ERROR"
        assert cross_res.ephemeral is True

        # Cross-actor token for Identity Operator also fails even on fresh isolated store
        cross_idop = isolated_svc.handle_slash_command(
            "setplayer",
            guild_id=APPROVED_GUILD_ID,
            channel_id=RESULTS_CHANNEL_ID,
            user_id=IDENTITY_OP_USER_ID,
            user_role_ids=(IDENTITY_OPERATOR_ROLE_ID,),
            player_id=cap_tok,
            display_name="CapTarget",
        )
        assert cross_idop.status == "ERROR"
        assert cross_idop.ephemeral is True

        # Manager can confirm own unexpired actor-bound token (both player_id=tok and token=tok)
        mgr_tok_entry = next(e for e in mgr_entries if "MgrTarget" in e.candidate_aliases)
        mgr_ok = svc.handle_slash_command(
            "setplayer",
            guild_id=APPROVED_GUILD_ID,
            channel_id=RESULTS_CHANNEL_ID,
            user_id=MANAGER_USER_ID,
            user_role_ids=(MANAGER_ROLE_ID,),
            player_id=mgr_tok_entry.token,
            display_name="MgrTarget",
            position="ST",
        )
        assert mgr_ok.status == "OK"
        assert "MgrTarget" in mgr_ok.content

        # Captain can confirm own unexpired actor-bound token
        cap_tok_entry = next(e for e in cap_entries if "CapTarget" in e.candidate_aliases)
        cap_ok = svc.handle_slash_command(
            "setplayer",
            guild_id=APPROVED_GUILD_ID,
            channel_id=RESULTS_CHANNEL_ID,
            user_id=CAPTAIN_USER_ID,
            user_role_ids=(CAPTAIN_ROLE_ID,),
            token=cap_tok_entry.token,
            display_name="CapTarget",
            position="CM",
        )
        assert cap_ok.status == "OK"
        assert "CapTarget" in cap_ok.content

        # Identity Operator can confirm own unexpired token, CANNOT bypass canonical plr_ecf_* check via token kwarg
        idop_tok_entry = next(e for e in idop_entries if "IdOpTarget" in e.candidate_aliases)
        p9503 = pseudonymize_east_coasters_identifier("9503", club_id=EAST_COASTERS_CLUB_ID)
        bypass_attempt = svc.handle_slash_command(
            "setplayer",
            guild_id=APPROVED_GUILD_ID,
            channel_id=RESULTS_CHANNEL_ID,
            user_id=IDENTITY_OP_USER_ID,
            user_role_ids=(IDENTITY_OPERATOR_ROLE_ID,),
            player_id=p9503,
            token=idop_tok_entry.token,
            display_name="IdOpTarget",
        )
        assert bypass_attempt.status == "REJECTED_UNAUTHORIZED"
        assert bypass_attempt.ephemeral is True
        assert bypass_attempt.content == PERMISSION_DENIED_MESSAGE

        idop_ok = svc.handle_slash_command(
            "setplayer",
            guild_id=APPROVED_GUILD_ID,
            channel_id=RESULTS_CHANNEL_ID,
            user_id=IDENTITY_OP_USER_ID,
            user_role_ids=(IDENTITY_OPERATOR_ROLE_ID,),
            player_id=idop_tok_entry.token,
            display_name="IdOpTarget",
            position="CB",
        )
        assert idop_ok.status == "OK"

        # Unknown & expired tokens fail for Manager/Captain/Identity Operator
        unknown_mgr = svc.handle_slash_command(
            "setplayer",
            guild_id=APPROVED_GUILD_ID,
            channel_id=RESULTS_CHANNEL_ID,
            user_id=MANAGER_USER_ID,
            user_role_ids=(MANAGER_ROLE_ID,),
            player_id="T999",
            display_name="Nobody",
        )
        assert unknown_mgr.status == "ERROR"
        assert unknown_mgr.ephemeral is True

        # Admin can redeem own token
        adm_tok_entry = next(e for e in adm_entries if "AdmTarget" in e.candidate_aliases)
        adm_ok = svc.handle_slash_command(
            "setplayer",
            guild_id=APPROVED_GUILD_ID,
            channel_id=RESULTS_CHANNEL_ID,
            user_id=ADMIN_USER_ID,
            user_role_ids=(ADMIN_ROLE_ID,),
            player_id=adm_tok_entry.token,
            display_name="AdmTarget",
            position="GK",
        )
        assert adm_ok.status == "OK"

        # Expired token fails cleanly
        clock.now = clock.now + timedelta(minutes=16)
        expired_cap = svc.handle_slash_command(
            "setplayer",
            guild_id=APPROVED_GUILD_ID,
            channel_id=RESULTS_CHANNEL_ID,
            user_id=CAPTAIN_USER_ID,
            user_role_ids=(CAPTAIN_ROLE_ID,),
            player_id=cap_tok_entry.token,
            display_name="CapTarget",
        )
        assert expired_cap.status == "ERROR"
        assert expired_cap.ephemeral is True

        # Identity Operator remains isolated from /deletematch even with is_admin=False
        del_denied = svc.handle_slash_command(
            "deletematch",
            guild_id=APPROVED_GUILD_ID,
            channel_id=RESULTS_CHANNEL_ID,
            user_id=IDENTITY_OP_USER_ID,
            user_role_ids=(IDENTITY_OPERATOR_ROLE_ID,),
            match_id=1,
            confirm_delete=True,
        )
        assert del_denied.status == "REJECTED_UNAUTHORIZED"
        assert del_denied.content == PERMISSION_DENIED_MESSAGE


class TestDiscordEmbedAggregateLimit:
    """Phase 5 (Thread 4608588260): Enforce Discord 6,000-char aggregate and individual embed limits."""

    @staticmethod
    def _summary(
        idx: int,
        *,
        alias: str | None = "PlayerTag",
        ver_status: str | None = "STABLE",
        positions: tuple[str, ...] = ("ST", "CAM", "CM"),
    ) -> UnresolvedPlayerSummary:
        dt = datetime(2026, 11, 5, 18, 0, tzinfo=UTC)
        return UnresolvedPlayerSummary(
            token=f"T{idx}",
            appearance_count=idx + 1,
            observed_positions=positions,
            first_seen_at=dt,
            last_seen_at=dt,
            candidate_alias=alias,
            candidate_aliases=(alias,) if alias else (),
            candidate_verification_status=ver_status,
            candidate_conflict_status="SAME_PLAYER_MULTIPLE_ALIASES" if ver_status == "CONFLICTED" else "NONE",
            candidate_observation_count=3,
            candidate_distinct_match_count=2,
            player_id=pseudonymize_east_coasters_identifier(f"96{idx:02d}", club_id=EAST_COASTERS_CLUB_ID),
        )

    def test_embed_total_budget_exact_boundary_one_over_25_fields_and_sanitization(self) -> None:
        # 1. Small output unchanged + OBSERVED / STABLE / CONFLICTED labels preserved
        small_entries = [
            self._summary(1, alias="ObsAlpha", ver_status="OBSERVED"),
            self._summary(2, alias="StabBeta", ver_status="STABLE"),
            self._summary(3, alias=None, ver_status="CONFLICTED"),
        ]
        small_embed = render_unresolved_players_embed(small_entries)
        assert len(small_embed.fields) == 3
        assert small_embed.description == (
            "Every identity has a short-lived reconciliation token (`T1`, `T2`, ...) "
            "valid for 15 minutes. Never shares raw internal references."
        )
        assert "Candidate unverified" in (small_embed.fields[0].value or "")
        assert "Candidate stable — confirmation required" in (small_embed.fields[1].value or "")
        assert "Conflicting candidate tags — operator review required" in (small_embed.fields[2].value or "")
        assert _embed_aggregate_char_count(small_embed) <= DISCORD_EMBED_MAX_TOTAL_CHARS

        # 2 & 3 & 9. Exact 6,000-character boundary vs 6,001-character boundary (footer included in total)
        boundary_entries = [
            self._summary(
                i,
                alias=f"ÜñîcødêTag_{i:02d}_123456789012345678",
                ver_status="STABLE",
                positions=("ST", "CF", "LW", "RW", "CAM", "CM", "CDM", "LB", "RB", "CB"),
            )
            for i in range(1, 20)
        ]
        probe_embed = render_unresolved_players_embed(boundary_entries, footer_text="")
        assert len(probe_embed.fields) == 19
        used_no_footer = _embed_aggregate_char_count(probe_embed)
        pad_len = DISCORD_EMBED_MAX_TOTAL_CHARS - used_no_footer
        assert 1 <= pad_len <= DISCORD_EMBED_MAX_FOOTER_CHARS
        exact_footer = "F" * pad_len
        over_footer = "F" * (pad_len + 1)

        exact_embed = render_unresolved_players_embed(boundary_entries, footer_text=exact_footer)
        assert len(exact_embed.fields) == 19
        assert _embed_aggregate_char_count(exact_embed) == DISCORD_EMBED_MAX_TOTAL_CHARS
        assert "...and" not in (exact_embed.description or "")

        over_embed = render_unresolved_players_embed(boundary_entries, footer_text=over_footer)
        assert _embed_aggregate_char_count(over_embed) <= DISCORD_EMBED_MAX_TOTAL_CHARS
        assert len(over_embed.fields) < 19
        assert "...and " in (over_embed.description or "")
        assert [f.name for f in over_embed.fields] == [f"Token `T{i}`" for i in range(1, len(over_embed.fields) + 1)]
        for f in over_embed.fields:
            f_name = f.name or ""
            f_val = f.value or ""
            assert "Candidate stable — confirmation required" in f_val
            assert f_name.strip() and f_val.strip()

        # 4. 25-field boundary: 25 short entries fit cleanly in 25 fields; 28 entries cap at <= 25
        exact_25_entries = [self._summary(i, alias=f"Short{i}", positions=("ST",)) for i in range(1, 26)]
        embed_25 = render_unresolved_players_embed(exact_25_entries)
        assert len(embed_25.fields) == 25
        assert _embed_aggregate_char_count(embed_25) <= DISCORD_EMBED_MAX_TOTAL_CHARS
        assert "...and" not in (embed_25.description or "")

        many_entries = [self._summary(i, alias=f"Short{i}", positions=("ST",)) for i in range(1, 29)]
        many_embed = render_unresolved_players_embed(many_entries)
        assert len(many_embed.fields) == 25
        assert _embed_aggregate_char_count(many_embed) <= DISCORD_EMBED_MAX_TOTAL_CHARS
        assert "...and 3 more unresolved identities." in (many_embed.description or "")

        # 5, 10, 11, 12, 13, 14. Author included in total, long Unicode + giant positions, zero leakage, no broken separator
        huge_pos = tuple(f"POS{j:02d}" for j in range(250))
        uni_entries = [
            self._summary(i, alias="CaféStraße_" + ("Ж" * 20), ver_status="OBSERVED", positions=huge_pos)
            for i in range(1, 26)
        ]
        auth_embed = render_unresolved_players_embed(
            uni_entries,
            author_name="East Coasters Roster Review",
            footer_text=EAST_COASTERS_FOOTER_TEXT,
        )
        assert auth_embed.author.name == "East Coasters Roster Review"
        assert len(auth_embed.title or "") <= DISCORD_EMBED_MAX_TITLE_CHARS
        assert len(auth_embed.description or "") <= DISCORD_EMBED_MAX_DESCRIPTION_CHARS
        assert len(auth_embed.footer.text or "") <= DISCORD_EMBED_MAX_FOOTER_CHARS
        assert len(auth_embed.fields) <= DISCORD_EMBED_MAX_FIELDS
        assert _embed_aggregate_char_count(auth_embed) <= DISCORD_EMBED_MAX_TOTAL_CHARS
        for f in auth_embed.fields:
            f_name = f.name or ""
            f_val = f.value or ""
            assert len(f_name) <= DISCORD_EMBED_MAX_FIELD_NAME_CHARS
            assert len(f_val) <= DISCORD_EMBED_MAX_FIELD_VALUE_CHARS
            assert "Candidate unverified" in f_val
            assert not f_val.endswith((",", "•", "—"))
            assert_discord_output_sanitized(f_name)
            assert_discord_output_sanitized(f_val)
        assert_discord_output_sanitized(auth_embed.title or "")
        assert_discord_output_sanitized(auth_embed.description or "")
        assert_discord_output_sanitized(auth_embed.footer.text or "")
        joined = "\n".join(
            [auth_embed.title or "", auth_embed.description or "", auth_embed.footer.text or ""]
            + [(f.name or "") + "\n" + (f.value or "") for f in auth_embed.fields]
        )
        assert not PSEUDONYM_PLAYER_REF_RE.search(joined)
        assert not PSEUDONYM_MATCH_REF_RE.search(joined)


class TestSharedUnicodeNormalization:
    """Phase 6 (Thread 4608588301): Shared NFKC + whitespace-collapse + casefold normalizer."""

    def test_unicode_key_normalization_promotion_and_verification(self, pg_db_migrated) -> None:
        # 1. Pure normalizer equivalence checks
        assert normalize_public_player_alias_key("Straße") == "strasse"
        assert normalize_public_player_alias_key("Ｓｔｒａßｅ") == "strasse"  # NFKC + casefold
        assert normalize_public_player_alias_key("Cafe\u0301") == normalize_public_player_alias_key("Café")
        assert normalize_public_player_alias_key("  Lao   Jr \t ") == "lao jr"
        assert normalize_public_player_alias_key("\u1e97ag") == normalize_public_player_alias_key("t\u0308ag")
        assert normalize_public_player_alias_key("ＴＡＧ") == normalize_public_player_alias_key("tag")
        assert normalize_public_player_alias_key("Lao_Jr-1.0") != normalize_public_player_alias_key("Lao Jr 1 0")
        assert normalize_public_player_alias_key("Lao") == "lao"

        # 2. Candidate ingestion stores NFKC+casefold normalized key for "Straße" -> "strasse"
        env, _ = normalize_community_match_records(
            [
                _raw_community_match(
                    match_id="983001",
                    timestamp=1763040000,
                    players=[
                        _player_payload("9701", alias_field="player_name", alias_value="Straße"),
                        _player_payload("9702", alias_field="player_name", alias_value="Cafe\u0301 Tag", position="CM", goals=0),
                        _player_payload("9703", alias_field="player_name", alias_value="Rejectedß", position="CB", goals=0),
                        _player_payload("9705", alias_field="player_name", alias_value="\u1e97ag", position="LW", goals=0),
                    ],
                )
            ],
            alias_hmac_key=TEST_ALIAS_HMAC_KEY,
        )
        persist_polled_match_envelope(pg_db_migrated.poll_writer, match_envelope=env)

        p9701 = pseudonymize_east_coasters_identifier("9701", club_id=EAST_COASTERS_CLUB_ID)
        p9702 = pseudonymize_east_coasters_identifier("9702", club_id=EAST_COASTERS_CLUB_ID)
        p9703 = pseudonymize_east_coasters_identifier("9703", club_id=EAST_COASTERS_CLUB_ID)
        p9705 = pseudonymize_east_coasters_identifier("9705", club_id=EAST_COASTERS_CLUB_ID)

        # Reject p9703 before upsert: rejected candidates must NEVER be promoted
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            cid_9702_row = aconn.execute(
                "SELECT candidate_id FROM public.fc27_player_alias_candidates WHERE player_id = %s",
                (p9702,),
            ).fetchone()
            assert cid_9702_row is not None
            cid_9702 = int(cid_9702_row[0])
            cid_9703_row = aconn.execute(
                "SELECT candidate_id FROM public.fc27_player_alias_candidates WHERE player_id = %s",
                (p9703,),
            ).fetchone()
            assert cid_9703_row is not None
            cid_9703 = int(cid_9703_row[0])
        reject_player_alias_candidate(pg_db_migrated.match_results_writer, candidate_id=cid_9703)

        # Verify Café Tag using composed NFC string matches decomposed candidate
        assert (
            verify_player_alias_candidate(
                pg_db_migrated.match_results_writer,
                candidate_id=cid_9702,
                expected_alias="  Ｃａｆé   Ｔａｇ ",
            )
            == "OPERATOR_VERIFIED"
        )

        # Confirming "Straße" in upsert_roster_player marks candidate PROMOTED (even though .lower() was "straße"!)
        clock = _Clock()
        store = ReconciliationTokenStore(clock=clock)
        entries = list_unresolved_players(
            pg_db_migrated.discord_reader,
            token_store=store,
            actor_discord_user_id=IDENTITY_OP_USER_ID,
            now=clock.now,
        )
        tok_strasse = next(e.token for e in entries if e.player_id == p9701)
        upsert_roster_player(
            pg_db_migrated.match_results_writer,
            token=tok_strasse,
            token_store=store,
            actor_discord_user_id=IDENTITY_OP_USER_ID,
            display_name="Straße",
            position="ST",
        )
        # Attempting upsert matching a REJECTED candidate alias must NOT mark it PROMOTED
        upsert_roster_player(
            pg_db_migrated.match_results_writer,
            player_id=p9703,
            display_name="Rejectedß",
            position="CB",
        )
        upsert_roster_player(
            pg_db_migrated.match_results_writer,
            player_id=p9705,
            display_name="\u1e97ag",
            position="LW",
        )
        # Manual roster name with non-provider punctuation (parentheses/apostrophe) still works cleanly
        p9704 = pseudonymize_east_coasters_identifier("9704", club_id=EAST_COASTERS_CLUB_ID)
        manual_rec = upsert_roster_player(
            pg_db_migrated.match_results_writer,
            player_id=p9704,
            display_name="O'Connor (C)",
            position="CM",
        )
        assert manual_rec.display_name == "O'Connor (C)"

        # Upserting p9702 manually then renaming to "Café Tag" promotes decomposed candidate
        upsert_roster_player(
            pg_db_migrated.match_results_writer,
            player_id=p9702,
            display_name="OtherManualName",
            position="CM",
        )
        rename_roster_player(
            pg_db_migrated.match_results_writer,
            player_id_or_name=p9702,
            new_name="Café Tag",
        )

        with psycopg.connect(pg_db_migrated.admin) as aconn:
            assert aconn.execute(
                "SELECT verification_status FROM public.fc27_player_alias_candidates WHERE player_id = %s",
                (p9701,),
            ).fetchone() == ("PROMOTED",)
            assert aconn.execute(
                "SELECT verification_status FROM public.fc27_player_alias_candidates WHERE player_id = %s",
                (p9702,),
            ).fetchone() == ("PROMOTED",)
            assert aconn.execute(
                "SELECT display_name_comparison_key FROM public.fc27_roster WHERE player_id = %s",
                (p9705,),
            ).fetchone() == (normalize_public_player_alias_key("\u1e97ag"),)
            with pytest.raises(psycopg.errors.UniqueViolation):
                with aconn.transaction():
                    aconn.execute(
                        """INSERT INTO public.fc27_roster
                        (player_id, display_name, display_name_normalized,
                         display_name_comparison_key, active)
                        VALUES (%s, %s, %s, %s, TRUE)""",
                        (
                            "plr_ecf_000000009999", "t\u0308ag", "t\u0308ag",
                            normalize_public_player_alias_key("t\u0308ag"),
                        ),
                    )
            assert aconn.execute(
                "SELECT verification_status FROM public.fc27_player_alias_candidates WHERE player_id = %s",
                (p9703,),
            ).fetchone() == ("REJECTED",)


class TestDbMigratePrivilegePostconditionsRegression:
    """Phase 7 (Thread 4608588087): Column-scoped Policy 20 pass vs overbroad_table_* and forbidden DELETE failure."""

    def test_verify_postconditions_detects_overbroad_table_and_forbidden_delete_on_0019(
        self, pg_db
    ) -> None:
        migrate(pg_db.migrator)

        class _RollbackProbe(Exception):
            pass

        with psycopg.connect(pg_db.admin) as aconn:
            # 1. Intended column-scoped Policy 20 grants pass cleanly
            assert verify_postconditions(aconn) == []

            # 2. Unintended table-wide privileges report overbroad_table_* violations
            for grant_sql, expected_violation in (
                (
                    "GRANT SELECT ON public.fc27_player_alias_candidates TO fc27_discord_reader",
                    "overbroad_table_fc27_discord_reader_select_on_fc27_player_alias_candidates",
                ),
                (
                    "GRANT SELECT ON public.fc27_player_alias_candidates TO fc27_match_results_writer",
                    "overbroad_table_fc27_match_results_writer_select_on_fc27_player_alias_candidates",
                ),
                (
                    "GRANT UPDATE ON public.fc27_player_alias_candidates TO fc27_match_results_writer",
                    "overbroad_table_fc27_match_results_writer_update_on_fc27_player_alias_candidates",
                ),
                (
                    "GRANT UPDATE ON public.fc27_player_alias_candidates TO fc27_poll_writer",
                    "overbroad_table_fc27_poll_writer_update_on_fc27_player_alias_candidates",
                ),
            ):
                with pytest.raises(_RollbackProbe):
                    with aconn.transaction():
                        aconn.execute(grant_sql)
                        violations = verify_postconditions(aconn)
                        assert expected_violation in violations
                        raise _RollbackProbe()

            # 3. Forbidden DELETE grant fails with forbidden_*_delete_on_fc27_player_alias_candidates
            for role_name in ("fc27_poll_writer", "fc27_match_results_writer"):
                with pytest.raises(_RollbackProbe):
                    with aconn.transaction():
                        aconn.execute(
                            sql.SQL("GRANT DELETE ON public.fc27_player_alias_candidates TO {}").format(
                                sql.Identifier(role_name)
                            )
                        )
                        violations = verify_postconditions(aconn)
                        assert f"forbidden_{role_name}_delete_on_fc27_player_alias_candidates" in violations
                        raise _RollbackProbe()

            # 4. Granting SELECT(alias_hmac_fingerprint) fails with forbidden_*_select_alias_hmac
            for role_name in ("fc27_discord_reader", "fc27_match_results_writer"):
                with pytest.raises(_RollbackProbe):
                    with aconn.transaction():
                        aconn.execute(
                            sql.SQL(
                                "GRANT SELECT (alias_hmac_fingerprint) ON public.fc27_player_alias_candidates TO {}"
                            ).format(sql.Identifier(role_name))
                        )
                        violations = verify_postconditions(aconn)
                        assert f"forbidden_{role_name}_select_alias_hmac" in violations
                        raise _RollbackProbe()

            # Confirm rollbacks left intended least-privilege posture clean
            assert verify_postconditions(aconn) == []


def test_unchanged_roster_manual_candidate_confirmation_uses_confirmation_time(
    pg_db_migrated: Any,
) -> None:
    raw_id = "9801"
    player_id = pseudonymize_east_coasters_identifier(
        raw_id, club_id=EAST_COASTERS_CLUB_ID
    )
    alias = "ConfirmedLater"
    for match_id, timestamp in (("983101", 1763040100), ("983102", 1763040200)):
        envelope, _ = normalize_community_match_records(
            [
                _raw_community_match(
                    match_id=match_id,
                    timestamp=timestamp,
                    players=[_player_payload(raw_id, alias_field="player_name", alias_value=alias)],
                )
            ],
            alias_hmac_key=TEST_ALIAS_HMAC_KEY,
        )
        persist_polled_match_envelope(pg_db_migrated.poll_writer, match_envelope=envelope)

    historical = datetime(2020, 1, 1, tzinfo=UTC)
    comparison_key = normalize_public_player_alias_key(alias)
    with psycopg.connect(pg_db_migrated.admin) as admin:
        candidate = admin.execute(
            "SELECT candidate_id, verification_status FROM public.fc27_player_alias_candidates WHERE player_id = %s",
            (player_id,),
        ).fetchone()
        assert candidate is not None and candidate[1] == "STABLE"
        candidate_id = int(candidate[0])
        admin.execute(
            """INSERT INTO public.fc27_roster
            (player_id, display_name, display_name_normalized,
             display_name_comparison_key, position, active, created_at, updated_at)
            VALUES (%s, %s, %s, %s, 'ST', TRUE, %s, %s)""",
            (player_id, alias, alias.lower(), comparison_key, historical, historical),
        )

    upsert_roster_player(
        pg_db_migrated.match_results_writer,
        player_id=player_id,
        display_name=alias,
        position="ST",
        actor_discord_user_id=IDENTITY_OP_USER_ID,
    )
    with psycopg.connect(pg_db_migrated.admin) as admin:
        promoted_at_row = admin.execute(
            "SELECT promoted_at FROM public.fc27_player_alias_candidates WHERE candidate_id = %s",
            (candidate_id,),
        ).fetchone()
        audit_rows = admin.execute(
            "SELECT promoted_at FROM public.fc27_player_alias_promotion_audit WHERE candidate_id = %s",
            (candidate_id,),
        ).fetchall()
        roster_updated_at_row = admin.execute(
            "SELECT updated_at FROM public.fc27_roster WHERE player_id = %s",
            (player_id,),
        ).fetchone()
        refresh_count_row = admin.execute(
            "SELECT count(*) FROM public.fc27_discord_jobs WHERE job_type = 'refresh_roster_messages' AND payload ->> 'player_id' = %s",
            (player_id,),
        ).fetchone()
        assert promoted_at_row is not None
        assert roster_updated_at_row is not None
        assert refresh_count_row is not None
        promoted_at = promoted_at_row[0]
        roster_updated_at = roster_updated_at_row[0]
        refresh_count = refresh_count_row[0]
        assert promoted_at > historical
        assert audit_rows == [(promoted_at,)]
        assert roster_updated_at == historical
        assert refresh_count == 0

    upsert_roster_player(
        pg_db_migrated.match_results_writer,
        player_id=player_id,
        display_name=alias,
        position="ST",
        actor_discord_user_id=IDENTITY_OP_USER_ID,
    )
    with psycopg.connect(pg_db_migrated.admin) as admin:
        assert admin.execute(
            "SELECT count(*) FROM public.fc27_player_alias_promotion_audit WHERE candidate_id = %s",
            (candidate_id,),
        ).fetchone() == (1,)
