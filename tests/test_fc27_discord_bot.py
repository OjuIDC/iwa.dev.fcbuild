"""Comprehensive offline test suite for the read-only East Coasters FC27 Discord bot (`PHASE 3`, `PHASE 8` Tests #21–#50)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml

from scripts.fc27_discord_bot import main as discord_cli_main
from src.fc27_discord_bot import (
    DISCORD_DATABASE_WRITE_ACCESS,
    DISCORD_PROVIDER_REQUESTS,
    FC27DiscordAuthzError,
    FC27DiscordBotConfig,
    FC27DiscordBotService,
    FC27DiscordDisabledError,
    FC27DiscordPrivacyError,
    FC27DiscordWriteAccessDeniedError,
    GuardedDiscordReadOnlyConnection,
    MAX_DISCORD_CONTENT_LENGTH,
    MAX_DISCORD_EMBEDS,
    MAX_DISCORD_EXPORT_BYTES,
    NOT_AVAILABLE_NOT_IMPLEMENTED,
    PSEUDONYM_PLAYER_REF_RE,
    assert_discord_output_sanitized,
    assert_sql_is_readonly_fc27_select,
    load_discord_bot_token,
)
from src.fc27_polling_worker import FC27PollingRuntime, run_fc27_poll_cycle
from tests.test_fc27_polling_worker import (
    _build_validated_envelope,
    _enable_all_polling_gates,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
APPROVED_GUILD_ID = "123456789012345678"
APPROVED_CHANNEL_ID = "234567890123456789"
APPROVED_USER_ID = "345678901234567890"


def _seed_persisted_matches(
    monkeypatch: pytest.MonkeyPatch,
    app_db_uri: str,
    *,
    count: int = 10,
) -> FC27PollingRuntime:
    _enable_all_polling_gates(monkeypatch)
    runtime = FC27PollingRuntime()
    env, _ = _build_validated_envelope(count=count, start_id=992001, players_per_match=6)
    res = run_fc27_poll_cycle(
        runtime=runtime,
        confirm_live_polling=True,
        db_conn_or_uri=app_db_uri,
        fetch_matches_fn=lambda: env,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert res.poll_status == "NEW_MATCHES_PERSISTED"
    assert res.inserted_match_count == count
    return runtime


def _build_enabled_config(**overrides: Any) -> FC27DiscordBotConfig:
    params: dict[str, Any] = {
        "enabled": True,
        "allowed_guild_ids": frozenset({APPROVED_GUILD_ID}),
        "allowed_channel_ids": frozenset({APPROVED_CHANNEL_ID}),
    }
    params.update(overrides)
    return FC27DiscordBotConfig(**params)


def test_21_guild_allowlist_enforced() -> None:
    """21. Guild allowlist is required when enabled and accepts positive numeric snowflakes."""
    with pytest.raises(FC27DiscordDisabledError):
        FC27DiscordBotConfig(
            enabled=True,
            allowed_guild_ids=frozenset(),
            allowed_channel_ids=frozenset({APPROVED_CHANNEL_ID}),
        )
    with pytest.raises(FC27DiscordAuthzError):
        FC27DiscordBotConfig(
            enabled=True,
            allowed_guild_ids=frozenset({"not-a-snowflake"}),
            allowed_channel_ids=frozenset({APPROVED_CHANNEL_ID}),
        )
    cfg = _build_enabled_config()
    assert APPROVED_GUILD_ID in cfg.allowed_guild_ids


def test_22_channel_allowlist_enforced() -> None:
    """22. Channel allowlist is required when enabled and accepts positive numeric snowflakes."""
    with pytest.raises(FC27DiscordDisabledError):
        FC27DiscordBotConfig(
            enabled=True,
            allowed_guild_ids=frozenset({APPROVED_GUILD_ID}),
            allowed_channel_ids=frozenset(),
        )
    with pytest.raises(FC27DiscordAuthzError):
        FC27DiscordBotConfig(
            enabled=True,
            allowed_guild_ids=frozenset({APPROVED_GUILD_ID}),
            allowed_channel_ids=frozenset({"-999"}),
        )
    cfg = _build_enabled_config()
    assert APPROVED_CHANNEL_ID in cfg.allowed_channel_ids


def test_23_unapproved_guild_rejected() -> None:
    """23. Commands from an unapproved guild or direct message are rejected."""
    svc = FC27DiscordBotService(_build_enabled_config())
    resp = svc.handle_slash_command(
        "status",
        guild_id="999999999999999999",
        channel_id=APPROVED_CHANNEL_ID,
    )
    assert resp.status == "REJECTED_UNAUTHORIZED"
    assert resp.ephemeral is True

    dm_resp = svc.handle_slash_command(
        "status",
        guild_id=None,
        channel_id=APPROVED_CHANNEL_ID,
    )
    assert dm_resp.status == "REJECTED_UNAUTHORIZED"
    assert dm_resp.ephemeral is True


def test_24_unapproved_channel_rejected() -> None:
    """24. Commands from an unapproved channel in an allowlisted guild are rejected."""
    svc = FC27DiscordBotService(_build_enabled_config())
    resp = svc.handle_slash_command(
        "status",
        guild_id=APPROVED_GUILD_ID,
        channel_id="888888888888888888",
    )
    assert resp.status == "REJECTED_UNAUTHORIZED"
    assert resp.ephemeral is True


def test_25_no_admin_permission_required_and_privileged_intent_rejected() -> None:
    """25. Administrator permission and privileged message_content intent are rejected."""
    cfg = _build_enabled_config()
    assert cfg.administrator_permission_required is False
    assert cfg.message_content_intent_enabled is False
    with pytest.raises(FC27DiscordAuthzError):
        _build_enabled_config(administrator_permission_required=True)
    with pytest.raises(FC27DiscordAuthzError):
        _build_enabled_config(message_content_intent_enabled=True)


def test_26_slash_commands_only_and_arbitrary_sql_or_url_rejected() -> None:
    """26. Prefix/non-slash messages, arbitrary URLs, and arbitrary SQL are rejected."""
    svc = FC27DiscordBotService(_build_enabled_config())
    non_slash = svc.handle_slash_command(
        "status",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        is_slash_interaction=False,
    )
    assert non_slash.status == "REJECTED_UNAUTHORIZED"

    url_attempt = svc.handle_slash_command(
        "status",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        raw_argument="https://proclubs.ea.com/api/fc/clubs/matches",
    )
    assert url_attempt.status == "REJECTED_UNAUTHORIZED"

    sql_attempt = svc.handle_slash_command(
        "status",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        raw_argument="SELECT * FROM public.fc27_matches; DROP TABLE fc27_matches;",
    )
    assert sql_attempt.status == "REJECTED_UNAUTHORIZED"


def test_27_no_provider_calls_ever_executed(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """27. Discord commands never execute EA provider calls (`DISCORD_PROVIDER_REQUESTS == 0`)."""
    runtime = _seed_persisted_matches(monkeypatch, pg_db_migrated.app, count=10)
    svc = FC27DiscordBotService(
        _build_enabled_config(),
        db_conn_or_uri=pg_db_migrated.app,
        polling_runtime=runtime,
    )
    assert DISCORD_PROVIDER_REQUESTS == 0
    assert svc.provider_requests_executed == 0
    for cmd in ("status", "latest", "form", "analysis", "data-gaps", "polling", "help"):
        resp = svc.handle_slash_command(
            cmd,
            guild_id=APPROVED_GUILD_ID,
            channel_id=APPROVED_CHANNEL_ID,
            user_id=f"10000000000000001{len(cmd)}",
        )
        assert resp.status == "OK"
        assert resp.provider_requests_executed == 0
    assert svc.provider_requests_executed == 0


def test_28_readonly_db_role_rejects_superuser_or_owner(pg_db_migrated) -> None:
    """28. GuardedDiscordReadOnlyConnection accepts `fc27_app` and rejects admin/owner."""
    with psycopg.connect(pg_db_migrated.admin) as admin_conn:
        with pytest.raises(FC27DiscordWriteAccessDeniedError):
            GuardedDiscordReadOnlyConnection(admin_conn)

    with psycopg.connect(pg_db_migrated.app) as app_conn:
        guarded = GuardedDiscordReadOnlyConnection(app_conn)
        row = guarded.execute("SELECT COUNT(*) FROM public.fc27_clubs").fetchone()
        assert row is not None


def test_29_no_write_access_and_forbidden_tables_blocked(pg_db_migrated) -> None:
    """29. Any mutating SQL or access to checkpoints/approval/dispatch tables is blocked."""
    assert DISCORD_DATABASE_WRITE_ACCESS is False
    for bad_sql in (
        "INSERT INTO public.fc27_clubs (club_id) VALUES ('330323')",
        "UPDATE public.fc27_matches SET goals_for = 99",
        "DELETE FROM public.fc27_matches",
        "DROP TABLE public.fc27_matches",
        "SELECT * FROM public.checkpoints",
        "SELECT * FROM public.approval_challenges",
        "SELECT * FROM public.fc27_decision_evidence",
        "SELECT * FROM public.dispatch_outbox",
    ):
        with pytest.raises(FC27DiscordWriteAccessDeniedError):
            assert_sql_is_readonly_fc27_select(bad_sql)

    with psycopg.connect(pg_db_migrated.app) as app_conn:
        guarded = GuardedDiscordReadOnlyConnection(app_conn)
        with pytest.raises(FC27DiscordWriteAccessDeniedError):
            guarded.commit()
        with pytest.raises(FC27DiscordWriteAccessDeniedError):
            guarded.execute("DELETE FROM public.fc27_matches")


def test_30_latest_command_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """30. `/fc27 latest` returns sanitized match summaries with pseudonymous `plr_ecf_<12-hex>` refs."""
    runtime = _seed_persisted_matches(monkeypatch, pg_db_migrated.app, count=10)
    svc = FC27DiscordBotService(
        _build_enabled_config(),
        db_conn_or_uri=pg_db_migrated.app,
        polling_runtime=runtime,
    )
    resp = svc.handle_slash_command(
        "latest",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
    )
    assert resp.status == "OK"
    assert len(resp.embeds) == 1
    desc = resp.embeds[0].description
    assert "plr_ecf_" in desc
    assert_discord_output_sanitized(desc)


def test_31_form_command_includes_sample_size_and_confidence(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """31. `/fc27 form` reports team form, sample size, and confidence tier."""
    runtime = _seed_persisted_matches(monkeypatch, pg_db_migrated.app, count=10)
    svc = FC27DiscordBotService(
        _build_enabled_config(),
        db_conn_or_uri=pg_db_migrated.app,
        polling_runtime=runtime,
    )
    resp = svc.handle_slash_command(
        "form",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
    )
    assert resp.status == "OK"
    assert resp.row_count == 10
    fields = resp.embeds[0].fields
    assert fields["sample_size"] == "10"
    assert fields["confidence_tier"] == "MODERATE_CONFIDENCE"


def test_32_analysis_command_includes_counter_evidence_confounders_and_gaps(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """32. `/fc27 analysis` includes findings, counter-evidence, confounders, data gaps, and confidence."""
    runtime = _seed_persisted_matches(monkeypatch, pg_db_migrated.app, count=10)
    svc = FC27DiscordBotService(
        _build_enabled_config(),
        db_conn_or_uri=pg_db_migrated.app,
        polling_runtime=runtime,
    )
    resp = svc.handle_slash_command(
        "analysis",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
    )
    assert resp.status == "OK"
    fields = resp.embeds[0].fields
    assert "counter_evidence" in fields and fields["counter_evidence"]
    assert "confounders" in fields and fields["confounders"]
    assert "data_gaps" in fields and fields["data_gaps"]
    assert fields["confidence"] == "MODERATE_CONFIDENCE"
    assert "recommendation=false" in fields["governance"]


def test_33_data_gaps_command_and_unimplemented_advisory_commands(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """33. `/fc27 data-gaps` reports gaps; `/fc27 coach|tactics|director` return NOT_AVAILABLE_NOT_IMPLEMENTED when unwired and deterministic advisory outputs when wired."""
    runtime = _seed_persisted_matches(monkeypatch, pg_db_migrated.app, count=10)
    unwired_svc = FC27DiscordBotService(
        _build_enabled_config(),
        db_conn_or_uri=pg_db_migrated.app,
        polling_runtime=runtime,
        coach_report_provider=None,
        tactics_report_provider=None,
        director_report_provider=None,
    )
    gaps_resp = unwired_svc.handle_slash_command(
        "data-gaps",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
    )
    assert gaps_resp.status == "OK"
    assert gaps_resp.row_count > 0

    for pending_cmd in ("coach", "tactics", "director"):
        pending_resp = unwired_svc.handle_slash_command(
            pending_cmd,
            guild_id=APPROVED_GUILD_ID,
            channel_id=APPROVED_CHANNEL_ID,
            user_id=f"20000000000000000{len(pending_cmd)}",
        )
        assert pending_resp.status == NOT_AVAILABLE_NOT_IMPLEMENTED
        assert pending_resp.content == NOT_AVAILABLE_NOT_IMPLEMENTED

    wired_svc = FC27DiscordBotService(
        _build_enabled_config(),
        db_conn_or_uri=pg_db_migrated.app,
        polling_runtime=runtime,
    )
    for advisory_cmd in ("coach", "tactics", "director"):
        adv_resp = wired_svc.handle_slash_command(
            advisory_cmd,
            guild_id=APPROVED_GUILD_ID,
            channel_id=APPROVED_CHANNEL_ID,
            user_id=f"30000000000000000{len(advisory_cmd)}",
        )
        assert adv_resp.status == "OK"
        assert adv_resp.row_count > 0
        assert "recommendation=false" in adv_resp.embeds[0].fields["governance"]


def test_34_polling_command_reports_12_hour_schedule_and_gap_metadata(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """34. `/fc27 polling` reports 12-hour polling schedule (`ONCE_EVERY_12_HOURS` / `43200`s) and continuity state."""
    runtime = _seed_persisted_matches(monkeypatch, pg_db_migrated.app, count=10)
    svc = FC27DiscordBotService(
        _build_enabled_config(),
        db_conn_or_uri=pg_db_migrated.app,
        polling_runtime=runtime,
    )
    resp = svc.handle_slash_command(
        "polling",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
    )
    assert resp.status == "OK"
    assert "every 12 hours" in resp.content
    assert "ONCE_EVERY_12_HOURS" in resp.content
    assert "43200" in resp.content
    fields = resp.embeds[0].fields
    for required_key in (
        "polling_frequency",
        "last_attempt_at",
        "last_success_at",
        "last_new_match_at",
        "next_eligible_at",
        "schedule_lag_seconds",
        "possible_history_gap",
        "circuit_state",
        "database_match_count",
    ):
        assert required_key in fields
    assert fields["database_match_count"] == "10"


def test_35_export_records_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """35. `/fc27 export-records` produces a sanitized JSON attachment with pseudonymous player refs."""
    runtime = _seed_persisted_matches(monkeypatch, pg_db_migrated.app, count=10)
    svc = FC27DiscordBotService(
        _build_enabled_config(),
        db_conn_or_uri=pg_db_migrated.app,
        polling_runtime=runtime,
    )
    resp = svc.handle_slash_command(
        "export-records",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
    )
    assert resp.status == "OK"
    assert resp.attachment is not None
    assert resp.attachment.row_count == 10
    payload = json.loads(resp.attachment.data_bytes.decode("utf-8"))
    assert payload["club_id"] == "330323"
    assert len(payload["records"]) == 10
    for rec in payload["records"]:
        for p in rec["players"]:
            assert PSEUDONYM_PLAYER_REF_RE.match(p["local_player_reference"])


def test_36_export_csv_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """36. `/fc27 export-csv` produces a sanitized CSV attachment."""
    runtime = _seed_persisted_matches(monkeypatch, pg_db_migrated.app, count=10)
    svc = FC27DiscordBotService(
        _build_enabled_config(),
        db_conn_or_uri=pg_db_migrated.app,
        polling_runtime=runtime,
    )
    resp = svc.handle_slash_command(
        "export-csv",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
    )
    assert resp.status == "OK"
    assert resp.attachment is not None
    csv_text = resp.attachment.data_bytes.decode("utf-8")
    assert "match_id,played_at,club_id,club_name" in csv_text
    assert "East Coasters" in csv_text
    assert_discord_output_sanitized(csv_text)


def test_37_export_excel_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """37. `/fc27 export-excel` produces a sanitized Excel workbook attachment."""
    runtime = _seed_persisted_matches(monkeypatch, pg_db_migrated.app, count=10)
    svc = FC27DiscordBotService(
        _build_enabled_config(),
        db_conn_or_uri=pg_db_migrated.app,
        polling_runtime=runtime,
    )
    resp = svc.handle_slash_command(
        "export-excel",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
    )
    assert resp.status == "OK"
    assert resp.attachment is not None
    assert len(resp.attachment.data_bytes) > 100
    assert len(resp.attachment.data_bytes) <= MAX_DISCORD_EXPORT_BYTES


def test_38_player_names_and_best_worst_rankings_absent_and_rejected() -> None:
    """38. Player names, gamer tags, best/worst rankings, and psychological inferences are rejected."""
    for prohibited in (
        "gamer_tag: HiddenGamer99",
        "plr_ecf_001122334455 is the best player in the club",
        "plr_ecf_001122334455 is the worst player on the pitch",
        "Player showed bad attitude and low effort",
    ):
        with pytest.raises(FC27DiscordPrivacyError):
            assert_discord_output_sanitized(prohibited)


def test_39_source_ids_raw_events_and_connection_strings_absent_and_rejected() -> None:
    """39. Source player IDs, raw event bitmaps, and database connection strings are rejected."""
    for prohibited in (
        "persona_id=99887766",
        "external_player_id=12345",
        "match_event_aggregate_0",
        "215:10,216:2,220:1",
        "postgresql://fc27_app:secret@postgres:5432/fc27",
    ):
        with pytest.raises(FC27DiscordPrivacyError):
            assert_discord_output_sanitized(prohibited)


def test_40_output_bounded(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """40. Embed count, content length, and export retention are strictly bounded."""
    runtime = _seed_persisted_matches(monkeypatch, pg_db_migrated.app, count=10)
    svc = FC27DiscordBotService(
        _build_enabled_config(
            export_cooldown_seconds=1.0,
            max_user_commands_per_minute=30,
            max_guild_commands_per_minute=60,
        ),
        db_conn_or_uri=pg_db_migrated.app,
        polling_runtime=runtime,
        clock_fn=iter(float(i * 5) for i in range(1, 50)).__next__,
    )
    for idx in range(7):
        resp = svc.handle_slash_command(
            "export-records",
            guild_id=APPROVED_GUILD_ID,
            channel_id=APPROVED_CHANNEL_ID,
            user_id=f"90000000000000000{idx}",
        )
        assert len(resp.content) <= MAX_DISCORD_CONTENT_LENGTH
        assert len(resp.embeds) <= MAX_DISCORD_EMBEDS
    assert len(svc.retained_exports) == 5


def test_41_pagination_on_latest_command(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """41. `/fc27 latest` supports bounded pagination (`page`, `total_pages`, `has_next_page`)."""
    runtime = _seed_persisted_matches(monkeypatch, pg_db_migrated.app, count=10)
    svc = FC27DiscordBotService(
        _build_enabled_config(),
        db_conn_or_uri=pg_db_migrated.app,
        polling_runtime=runtime,
    )
    p1 = svc.handle_slash_command(
        "latest",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        page=1,
        page_size=4,
    )
    assert p1.page == 1
    assert p1.total_pages == 3
    assert p1.has_next_page is True
    assert p1.row_count == 4

    p3 = svc.handle_slash_command(
        "latest",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        page=3,
        page_size=4,
    )
    assert p3.page == 3
    assert p3.total_pages == 3
    assert p3.has_next_page is False
    assert p3.row_count == 2


def test_42_user_rate_limit_enforced() -> None:
    """42. Per-user command rate limit throttles excessive commands from a single user."""
    now = [100.0]
    svc = FC27DiscordBotService(
        _build_enabled_config(max_user_commands_per_minute=2, max_guild_commands_per_minute=20),
        clock_fn=lambda: now[0],
    )
    r1 = svc.handle_slash_command(
        "help", guild_id=APPROVED_GUILD_ID, channel_id=APPROVED_CHANNEL_ID, user_id=APPROVED_USER_ID
    )
    r2 = svc.handle_slash_command(
        "help", guild_id=APPROVED_GUILD_ID, channel_id=APPROVED_CHANNEL_ID, user_id=APPROVED_USER_ID
    )
    r3 = svc.handle_slash_command(
        "help", guild_id=APPROVED_GUILD_ID, channel_id=APPROVED_CHANNEL_ID, user_id=APPROVED_USER_ID
    )
    assert r1.status == "OK"
    assert r2.status == "OK"
    assert r3.status == "RATE_LIMITED"
    assert r3.ephemeral is True


def test_43_guild_rate_limit_enforced() -> None:
    """43. Per-guild command rate limit throttles excessive commands across a guild."""
    now = [200.0]
    svc = FC27DiscordBotService(
        _build_enabled_config(max_user_commands_per_minute=10, max_guild_commands_per_minute=2),
        clock_fn=lambda: now[0],
    )
    r1 = svc.handle_slash_command(
        "help", guild_id=APPROVED_GUILD_ID, channel_id=APPROVED_CHANNEL_ID, user_id="1001"
    )
    r2 = svc.handle_slash_command(
        "help", guild_id=APPROVED_GUILD_ID, channel_id=APPROVED_CHANNEL_ID, user_id="1002"
    )
    r3 = svc.handle_slash_command(
        "help", guild_id=APPROVED_GUILD_ID, channel_id=APPROVED_CHANNEL_ID, user_id="1003"
    )
    assert r1.status == "OK"
    assert r2.status == "OK"
    assert r3.status == "RATE_LIMITED"


def test_44_export_cooldown_enforced(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """44. Export commands enforce per-user cooldown."""
    runtime = _seed_persisted_matches(monkeypatch, pg_db_migrated.app, count=10)
    now = [500.0]
    svc = FC27DiscordBotService(
        _build_enabled_config(export_cooldown_seconds=30.0),
        db_conn_or_uri=pg_db_migrated.app,
        polling_runtime=runtime,
        clock_fn=lambda: now[0],
    )
    first = svc.handle_slash_command(
        "export-records",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=APPROVED_USER_ID,
    )
    assert first.status == "OK"

    now[0] = 510.0
    second = svc.handle_slash_command(
        "export-csv",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=APPROVED_USER_ID,
    )
    assert second.status == "RATE_LIMITED"

    now[0] = 535.0
    third = svc.handle_slash_command(
        "export-csv",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=APPROVED_USER_ID,
    )
    assert third.status == "OK"


def test_45_audit_records_metadata_only(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """45. Audit log records only hashed IDs, command, outcome, duration, row count, and export type."""
    runtime = _seed_persisted_matches(monkeypatch, pg_db_migrated.app, count=10)
    svc = FC27DiscordBotService(
        _build_enabled_config(),
        db_conn_or_uri=pg_db_migrated.app,
        polling_runtime=runtime,
    )
    svc.handle_slash_command(
        "export-records",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=APPROVED_USER_ID,
    )
    assert len(svc.audit_entries) == 1
    entry = svc.audit_entries[0]
    dumped = json.dumps(entry.model_dump())
    assert APPROVED_GUILD_ID not in dumped
    assert APPROVED_CHANNEL_ID not in dumped
    assert APPROVED_USER_ID not in dumped
    assert "plr_ecf_" not in dumped
    assert entry.command == "export-records"
    assert entry.export_type == "records"
    assert entry.row_count == 10


def test_46_token_secret_file_loading(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """46. `load_discord_bot_token` loads token from `DISCORD_BOT_TOKEN_FILE`."""
    secret_file = tmp_path / "discord_bot_token"
    secret_file.write_text("  discord_test_token_value_998877  \n", encoding="utf-8")
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN_FILE", str(secret_file))
    assert load_discord_bot_token(required=True) == "discord_test_token_value_998877"


def test_47_token_absent_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """47. Missing or placeholder token fails closed."""
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_BOT_TOKEN_FILE", raising=False)
    with pytest.raises( Exception ):
        load_discord_bot_token(required=True)

    monkeypatch.setenv("DISCORD_BOT_TOKEN", "replace_with_bot_token")
    with pytest.raises(FC27DiscordDisabledError):
        load_discord_bot_token(required=True)


def test_48_no_token_in_repr_or_cli_logs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """48. Bot token is never printed in `repr()` or CLI `--status` output."""
    secret_tok = "super_secret_discord_token_do_not_print_12345"
    secret_file = tmp_path / "discord_bot_token"
    secret_file.write_text(secret_tok, encoding="utf-8")
    monkeypatch.setenv("DISCORD_BOT_TOKEN_FILE", str(secret_file))
    monkeypatch.setenv("FC27_DISCORD_BOT_ENABLED", "false")

    svc = FC27DiscordBotService(
        FC27DiscordBotConfig(enabled=False),
        secret_tokens_to_scrub=(secret_tok,),
    )
    assert secret_tok not in repr(svc)
    assert "[REDACTED_DISCORD_TOKEN]" in repr(svc)

    rc = discord_cli_main(["--status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert secret_tok not in out
    assert "DISCORD_TOKEN_CONFIGURED=YES" in out
    assert "DISCORD_PROVIDER_REQUESTS=0" in out


def test_49_clean_shutdown() -> None:
    """49. `stop()` cleanly transitions `is_stopped` to True."""
    svc = FC27DiscordBotService(_build_enabled_config())
    assert svc.is_stopped is False
    svc.stop()
    assert svc.is_stopped is True


def test_50_discord_container_and_compose_isolation() -> None:
    """50. `Dockerfile.discord` and `docker-compose.discord.yml` enforce non-root, read-only, opt-in profile isolation."""
    dockerfile_text = (REPO_ROOT / "Dockerfile.discord").read_text(encoding="utf-8")
    assert dockerfile_text.startswith(
        "FROM python:3.11.18-slim@sha256:064f01707f2fc8b06bae86bb43875266c6e41bac7994e77ec5dc623c4a4d0545"
    )
    assert "USER 10001:65534" in dockerfile_text
    assert "discord.py==2.7.1" in dockerfile_text
    assert "fc27-clubs-api" not in dockerfile_text
    assert "COPY scripts /app/scripts" in dockerfile_text
    assert 'CMD ["--status"]' in dockerfile_text

    compose_data = yaml.safe_load((REPO_ROOT / "docker-compose.discord.yml").read_text(encoding="utf-8"))
    svc = compose_data["services"]["fc27-discord-bot"]
    assert svc["profiles"] == ["fc27-discord"]
    assert svc["user"] == "10001:65534"
    assert svc["read_only"] is True
    assert svc["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in svc["security_opt"]
    assert "ports" not in svc
    assert "volumes" not in svc
    assert svc["command"] == ["--run"]
    assert svc["healthcheck"]["test"] == ["CMD", "python", "scripts/fc27_discord_bot.py", "--status"]
    assert set(svc["secrets"]) == {"discord_bot_token", "fc27_app_uri"}


def test_51_identity_and_webauthn_tables_blocked_and_role_allowlist_enforced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """51. Identity/WebAuthn SQL access is blocked, and application_id + role allowlists fail closed."""
    for identity_sql in (
        "SELECT * FROM public.fc27_users",
        "SELECT * FROM public.fc27_credentials",
        "SELECT * FROM public.fc27_sessions",
        "SELECT * FROM public.fc27_invitations",
        "SELECT * FROM public.fc27_recovery_codes",
        "SELECT * FROM public.fc27_totp_credentials",
        "SELECT * FROM public.fc27_webauthn_credentials",
        "SELECT * FROM public.fc27_action_tokens",
        "SELECT * FROM public.fc27_user_roles",
        "SELECT * FROM public.fc27_delegations",
        "SELECT * FROM public.fc27_challenges",
        "SELECT * FROM public.fc27_bootstrap_locks",
    ):
        with pytest.raises(FC27DiscordWriteAccessDeniedError):
            assert_sql_is_readonly_fc27_select(identity_sql)

    with pytest.raises(FC27DiscordDisabledError):
        FC27DiscordBotConfig(
            enabled=True,
            require_application_id=True,
            application_id=None,
            allowed_guild_ids=frozenset({APPROVED_GUILD_ID}),
            allowed_channel_ids=frozenset({APPROVED_CHANNEL_ID}),
        )

    with pytest.raises(FC27DiscordAuthzError):
        FC27DiscordBotConfig(
            enabled=True,
            application_id="not_a_snowflake",
            allowed_guild_ids=frozenset({APPROVED_GUILD_ID}),
            allowed_channel_ids=frozenset({APPROVED_CHANNEL_ID}),
        )

    monkeypatch.setenv("DISCORD_ROLE_IDS", "bad_role_id")
    with pytest.raises(FC27DiscordDisabledError):
        FC27DiscordBotConfig.from_env(strict=True)
    monkeypatch.delenv("DISCORD_ROLE_IDS", raising=False)

    cfg_with_roles = FC27DiscordBotConfig(
        enabled=True,
        application_id="444444444444444444",
        require_application_id=True,
        allowed_guild_ids=frozenset({APPROVED_GUILD_ID}),
        allowed_channel_ids=frozenset({APPROVED_CHANNEL_ID}),
        allowed_role_ids=frozenset({"555555555555555555"}),
    )
    svc = FC27DiscordBotService(cfg_with_roles)
    denied = svc.handle_slash_command(
        "help",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_role_ids=("666666666666666666",),
    )
    assert denied.status == "REJECTED_UNAUTHORIZED"
    assert denied.provider_requests_executed == 0

    allowed = svc.handle_slash_command(
        "help",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_role_ids=("555555555555555555",),
    )
    assert allowed.status == "OK"
    assert allowed.provider_requests_executed == 0


def test_52_gateway_run_mode_gates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """52. `scripts/fc27_discord_bot.py --run` connects only when enabled, token, application ID, and guild/channel/role allowlists are valid."""
    secret_file = tmp_path / "discord_bot_token"
    secret_file.write_text("valid_test_discord_bot_token_1234567890", encoding="utf-8")
    monkeypatch.setenv("DISCORD_BOT_TOKEN_FILE", str(secret_file))
    monkeypatch.setenv("FC27_DISCORD_BOT_ENABLED", "true")
    monkeypatch.setenv("DISCORD_APPLICATION_ID", "1567228306099802252")
    monkeypatch.setenv("FC27_DISCORD_ALLOWED_GUILD_IDS", APPROVED_GUILD_ID)
    monkeypatch.setenv("FC27_DISCORD_ALLOWED_CHANNEL_IDS", APPROVED_CHANNEL_ID)
    monkeypatch.delenv("FC27_DISCORD_ALLOWED_ROLE_IDS", raising=False)
    monkeypatch.delenv("DISCORD_ROLE_IDS", raising=False)

    # Missing role allowlist fails closed in --run mode
    called: list[str] = []
    rc_missing_role = discord_cli_main(
        ["--run"],
        gateway_runner=lambda _svc, _tok: (called.append("ran"), 0)[1],
    )
    assert rc_missing_role == 2
    assert called == []

    # With role allowlist configured, gateway_runner is invoked cleanly
    monkeypatch.setenv("FC27_DISCORD_ALLOWED_ROLE_IDS", "1567238698402320595")
    rc_ok = discord_cli_main(
        ["--run"],
        gateway_runner=lambda _svc, _tok: (called.append("ran"), 0)[1],
    )
    assert rc_ok == 0
    assert called == ["ran"]


def test_53_status_command_registration_and_approved_guild_channel(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """53. `/fc27 status` is registered and returns complete polling and repository status in an approved guild/channel."""
    from src.fc27_discord_bot import SUPPORTED_DISCORD_SUBCOMMANDS

    assert "status" in SUPPORTED_DISCORD_SUBCOMMANDS
    runtime = _seed_persisted_matches(monkeypatch, pg_db_migrated.app, count=10)
    svc = FC27DiscordBotService(
        _build_enabled_config(),
        db_conn_or_uri=pg_db_migrated.app,
        polling_runtime=runtime,
    )
    resp = svc.handle_slash_command(
        "status",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=APPROVED_USER_ID,
    )
    assert resp.status == "OK"
    assert resp.command == "status"
    assert resp.provider_requests_executed == 0
    assert resp.row_count == 10
    assert 1 <= len(resp.embeds) <= MAX_DISCORD_EMBEDS

    combined_fields: dict[str, str] = {}
    for emb in resp.embeds:
        assert len(emb.fields) <= 25
        combined_fields.update(emb.fields)

    assert combined_fields["provider_id"] == "fc-clubs-api-community-v1"
    assert combined_fields["source_classification"] == "LIVE_EA_CLUBS_COMMUNITY_CLIENT"
    assert combined_fields["club_name"] == "East Coasters"
    assert combined_fields["platform"] == "common-gen5"
    assert combined_fields["game_version"] == "FC27"
    assert combined_fields["match_type"] == "leagueMatch"
    assert combined_fields["poll_interval_seconds"] == "43200"
    assert combined_fields["daily_request_limit"] == "2"
    assert combined_fields["maximum_requests_per_cycle"] == "1"
    assert combined_fields["maximum_retries"] == "0"
    assert combined_fields["single_flight_enabled"] == "true"
    assert combined_fields["worker_enabled"] in {"true", "false"}
    assert combined_fields["worker_running"] in {"true", "false"}
    assert combined_fields["poll_status"] == "NEW_MATCHES_PERSISTED"
    assert combined_fields["circuit_state"] == "CLOSED"
    assert combined_fields["consecutive_failures"] == "0"
    assert combined_fields["total_cycles"] == "1"
    assert combined_fields["provider_request_count"] == "1"
    assert combined_fields["matches_inserted"] == "10"
    assert combined_fields["duplicates_ignored"] == "0"
    assert combined_fields["replay_conflicts"] == "0"
    assert combined_fields["schedule_lag_seconds"] == "0"
    assert combined_fields["missed_intervals"] == "0"
    assert combined_fields["possible_history_gap"] == "false"
    assert combined_fields["continuity_last_verified_at"].endswith("Z")
    assert combined_fields["database_availability"] == "AVAILABLE"
    assert combined_fields["database_match_count"] == "10"
    assert combined_fields["latest_stored_match_time"].endswith("Z")
    assert "READ_ONLY" in combined_fields["manual_match_results_availability"]


def test_54_status_database_unavailable_safe_and_no_secret_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """54. `/fc27 status` handles database unavailability safely without leaking URIs, passwords, stack traces, or paths."""
    monkeypatch.delenv("POSTGRES_URI", raising=False)
    monkeypatch.delenv("POSTGRES_URI_FILE", raising=False)
    broken_uri = "postgresql://fc27_app:supersecret_db_pw_999@127.0.0.1:1/fc27_nonexistent"
    svc = FC27DiscordBotService(
        _build_enabled_config(),
        db_conn_or_uri=broken_uri,
        secret_tokens_to_scrub=("supersecret_db_pw_999",),
    )
    resp = svc.handle_slash_command(
        "status",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=APPROVED_USER_ID,
    )
    assert resp.status == "OK"
    assert resp.provider_requests_executed == 0
    assert resp.row_count == 0
    serialized = json.dumps(resp.model_dump(mode="json"))
    assert "supersecret_db_pw_999" not in serialized
    assert "postgresql://" not in serialized
    assert "Traceback" not in serialized

    combined_fields: dict[str, str] = {}
    for emb in resp.embeds:
        combined_fields.update(emb.fields)
    assert combined_fields["database_availability"] == "UNAVAILABLE"
    assert combined_fields["database_match_count"] == "0"
    assert combined_fields["latest_stored_match_time"] == "N/A"
    assert combined_fields["manual_match_results_availability"] == "UNAVAILABLE"


def test_55_status_empty_database_and_missing_optional_fields_render_na(
    pg_db_migrated,
) -> None:
    """55. `/fc27 status` with zero matches and missing optional polling fields renders `N/A` and never `None`/`null`/`undefined`/`NaN`."""

    class _MinimalSnapshotStub:
        worker_enabled = False
        worker_running = False
        last_status = None
        circuit_state = None
        consecutive_failures = None
        total_cycles = None
        total_provider_requests = None
        total_matches_inserted = None
        total_duplicates_ignored = None
        total_replay_conflicts = None
        schedule_lag_seconds = None
        missed_intervals = None
        possible_history_gap = None
        continuity_last_verified_at = None
        last_attempt_at = None
        last_success_at = None
        next_eligible_at = None

    class _MinimalRuntimeStub:
        advisory_cache = None

        def snapshot_health(self) -> Any:
            return _MinimalSnapshotStub()

    svc = FC27DiscordBotService(
        _build_enabled_config(),
        db_conn_or_uri=pg_db_migrated.app,
        polling_runtime=_MinimalRuntimeStub(),  # type: ignore[arg-type]
    )
    resp = svc.handle_slash_command(
        "status",
        guild_id=APPROVED_GUILD_ID,
        channel_id=APPROVED_CHANNEL_ID,
        user_id=APPROVED_USER_ID,
    )
    assert resp.status == "OK"
    assert resp.row_count == 0
    combined_fields: dict[str, str] = {}
    for emb in resp.embeds:
        combined_fields.update(emb.fields)
        for v in emb.fields.values():
            assert v.lower() not in {"none", "null", "undefined", "nan"}
    assert combined_fields["database_availability"] == "AVAILABLE"
    assert combined_fields["database_match_count"] == "0"
    assert combined_fields["latest_stored_match_time"] == "N/A"
    assert combined_fields["poll_status"] == "N/A"
    assert combined_fields["circuit_state"] == "N/A"
    assert combined_fields["continuity_last_verified_at"] == "N/A"
    assert combined_fields["last_attempt_at"] == "N/A"
    assert combined_fields["last_success_at"] == "N/A"
    assert combined_fields["next_eligible_at"] == "N/A"


def test_56_status_suppresses_mentions_and_enforces_embed_bounds() -> None:
    """56. `/fc27 status` suppresses mentions and enforces Discord embed/field bounds."""
    from src.fc27_discord_bot import FC27DiscordEmbed, FC27DiscordError, _format_safe_status_value

    sanitized_val = _format_safe_status_value("alert @everyone @here <@123456789> <#987654321>")
    assert "@everyone" not in sanitized_val
    assert "@here" not in sanitized_val
    assert "<@123456789>" not in sanitized_val
    assert "<#987654321>" not in sanitized_val
    assert_discord_output_sanitized(sanitized_val)

    with pytest.raises(FC27DiscordPrivacyError):
        assert_discord_output_sanitized("Unsuppressed @everyone ping")

    too_many_fields = {f"field_{i}": "val" for i in range(26)}
    with pytest.raises(FC27DiscordError, match="exceeds 25"):
        FC27DiscordEmbed(title="Test", description="Desc", fields=too_many_fields)
