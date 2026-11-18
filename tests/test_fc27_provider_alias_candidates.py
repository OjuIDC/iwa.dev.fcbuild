"""Comprehensive tests for Migration 0019 provider-alias candidates, Identity Operator boundary, real CommandTree wiring, and backup/restore."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import logging
from pathlib import Path
import types
from typing import Any

import discord
from discord import app_commands
import psycopg
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
    extract_public_player_alias,
    normalize_community_match_records,
    persist_community_provider_batch,
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
    register_fc27_application_commands,
)
from src.fc27_discord_dispatcher import (
    DISPATCH_PROVIDER_REQUESTS,
    FC27DiscordDispatchConfig,
    SyntheticDiscordNotificationTransport,
    run_discord_dispatch_cycle,
)
from src.fc27_match_results import (
    ReconciliationTokenStore,
    format_unresolved_players_ephemeral,
    get_match_discord_message_links,
    list_unresolved_players,
    record_match_discord_message_links,
    reject_player_alias_candidate,
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
    pseudonymize_east_coasters_identifier,
    pseudonymize_match_ref,
    validate_public_player_alias_text,
)
from src.fc27_repository import (
    FC27AliasCandidateObservation,
    FC27RepositoryError,
    persist_player_alias_candidates,
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
        role_ids: list[tuple[int, str]] = (),
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
        for denied_cmd, extra_kwargs in (
            ("deletematch", {"match_id": 1, "confirm_delete": True}),
            ("matchday", {"opponent": "Rival FC", "date_str": "2026-11-10"}),
            ("result", {"opponent": "Rival FC", "our_score": 2, "opponent_score": 1}),
            ("editresult", {"match_id": 1, "our_score": 3}),
            ("renameplayer", {"raw_argument": "Lao -> LaoRenamed"}),
        ):
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
            assert aconn.execute("SELECT COUNT(*) FROM public.fc27_roster").fetchone()[0] == 0
            assert (
                aconn.execute(
                    "SELECT COUNT(*) FROM public.fc27_discord_jobs WHERE job_type = 'refresh_roster_messages'"
                ).fetchone()[0]
                == 0
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
                ).fetchone()[0]
                == 0
            )
            assert (
                aconn.execute(
                    "SELECT COUNT(*) FROM public.fc27_player_alias_candidate_observations WHERE match_ref = %s",
                    (pseudonymize_match_ref("949999", club_id=EAST_COASTERS_CLUB_ID),),
                ).fetchone()[0]
                == 0
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
            cid_9004 = aconn.execute(
                "SELECT candidate_id FROM public.fc27_player_alias_candidates WHERE player_id = %s",
                (p9004,),
            ).fetchone()[0]
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
        combined = ephem + "\n" + (embed.description or "") + "".join(f.value for f in embed.fields)
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
                ).fetchone()[0]
                == 1
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
            cid_9002 = aconn.execute(
                "SELECT candidate_id FROM public.fc27_player_alias_candidates WHERE public_alias = 'JayNew'"
            ).fetchone()[0]
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
        # Upgrade 0018 -> 0019
        applied_0019 = migrate(pg_db.migrator)
        assert applied_0019 == ["0019_fc27_player_alias_candidates.sql"]
        # Idempotent rerun
        assert migrate(pg_db.migrator) == []
        assert GRANTS_POLICY_VERSION == 20
        assert GRANTS_POLICY.version == 20

        with psycopg.connect(pg_db.admin) as aconn:
            assert verify_postconditions(aconn) == []
            assert aconn.execute("SELECT MAX(version) FROM public.schema_migrations").fetchone()[0].startswith("0019")
            assert aconn.execute(
                "SELECT version FROM public.schema_policy_versions WHERE policy = 'grants' ORDER BY version DESC LIMIT 1"
            ).fetchone() == (20,)
            # Preserved 0018 data
            assert aconn.execute(
                "SELECT display_name FROM public.fc27_roster WHERE player_id = 'plr_ecf_112233445566'"
            ).fetchone() == ("ExistingPlayer",)

            # Grants Policy 20 column & role privilege verification
            for role_name in ("fc27_discord_reader", "fc27_match_results_writer"):
                has_hmac_sel = aconn.execute(
                    "SELECT has_column_privilege(%s, 'public.fc27_player_alias_candidates', 'alias_hmac_fingerprint', 'SELECT')",
                    (role_name,),
                ).fetchone()[0]
                assert has_hmac_sel is False
                has_disp_sel = aconn.execute(
                    "SELECT has_column_privilege(%s, 'public.fc27_player_alias_candidates', 'public_alias', 'SELECT')",
                    (role_name,),
                ).fetchone()[0]
                assert has_disp_sel is True

            # Non-authorized roles have zero SELECT on any column of both 0019 tables
            for blocked_role in ("fc27_app", "fc27_dispatch", "fc27_relay", "fc27_player_dev_writer", "public"):
                for tbl in ("public.fc27_player_alias_candidates", "public.fc27_player_alias_candidate_observations"):
                    priv = aconn.execute(
                        "SELECT has_any_column_privilege(%s, %s, 'SELECT')",
                        (blocked_role, tbl),
                    ).fetchone()[0]
                    assert priv is False, f"{blocked_role} must not have SELECT on {tbl}"

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

            guild_cmds = {
                cmd.name: cmd
                for cmd in tree.get_commands(guild=discord.Object(id=int(APPROVED_GUILD_ID)))
            }
            assert len(guild_cmds) == 14

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
            cid_9204 = aconn.execute(
                "SELECT candidate_id FROM public.fc27_player_alias_candidates WHERE player_id = %s",
                (p9204,),
            ).fetchone()[0]
        reject_player_alias_candidate(pg_db_migrated.match_results_writer, candidate_id=cid_9204)

        backup_dir = tmp_path / ".local-data" / "backups"
        archive_path, manifest_path, manifest = create_fc27_backup(
            pg_db_migrated.admin,
            backup_dir=backup_dir,
        )
        assert manifest.migration_version == EXPECTED_MIGRATION_VERSION
        assert manifest.table_counts.schema_migrations == 19
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
