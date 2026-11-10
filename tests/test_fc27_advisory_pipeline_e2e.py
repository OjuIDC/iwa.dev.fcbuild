"""End-to-End Integration, Discord Readback, and Safety Tests for the FC27 Advisory Pipeline (`PHASE 7`, `PHASE 8`, Tests #84–#100).

Verifies the complete read-only advisory pipeline:
`Search -> Scout -> Analyst -> Coach -> Tactics -> Director`
plus Discord bot status and advisory parity, backup/restore hash determinism,
isolation from PostgresSaver/approval/dispatch/MatchPulse Live, and repository governance invariants.

Executes zero live EA network calls and zero live Discord calls.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import psycopg
import pytest

from src.fc27_backup_restore import create_fc27_backup, verify_fc27_backup_restore
from src.fc27_community_provider import (
    run_advisory_pipeline_from_persisted_community_records,
)
from src.fc27_discord_bot import FC27DiscordBotConfig, FC27DiscordBotService
from src.fc27_polling_worker import (
    FC27PollingRuntime,
    run_fc27_poll_cycle,
)
from tests.test_fc27_pandas_view import _make_pandas_shim
from tests.test_fc27_polling_worker import (
    _build_validated_envelope,
    _enable_all_polling_gates,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _guard_offline_network_and_pandas(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block all non-local DNS/socket connections and ensure pandas shim if needed."""
    orig_getaddrinfo = socket.getaddrinfo
    orig_create_connection = socket.create_connection

    def _guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        h_str = str(host or "").strip().lower()
        if h_str not in ("127.0.0.1", "localhost", "::1"):
            raise AssertionError(f"Blocked external DNS resolution in pytest: {host!r}")
        return orig_getaddrinfo(host, *args, **kwargs)

    def _guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        host = address[0] if isinstance(address, tuple) and address else ""
        if str(host).strip().lower() not in ("127.0.0.1", "localhost", "::1"):
            raise AssertionError(f"Blocked external socket connection in pytest: {address!r}")
        return orig_create_connection(address, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", _guarded_getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", _guarded_create_connection)
    monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "true")
    try:
        import pandas  # noqa: F401
    except ImportError:
        monkeypatch.setitem(sys.modules, "pandas", _make_pandas_shim())


def _seed_persisted_east_coasters(
    monkeypatch: pytest.MonkeyPatch,
    app_db_conn_or_uri: Any,
    count: int = 10,
) -> FC27PollingRuntime:
    _enable_all_polling_gates(monkeypatch)
    env, _ = _build_validated_envelope(count=count, start_id=995001, players_per_match=6)
    runtime = FC27PollingRuntime()
    res = run_fc27_poll_cycle(
        runtime=runtime,
        confirm_live_polling=True,
        db_conn_or_uri=app_db_conn_or_uri,
        fetch_matches_fn=lambda: env,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert res.poll_status == "NEW_MATCHES_PERSISTED"
    assert res.inserted_match_count == count
    return runtime


# ---------------------------------------------------------------------------
# 84. Full pipeline Search -> Scout -> Analyst -> Coach -> Tactics -> Director executes from persisted PostgreSQL records
# ---------------------------------------------------------------------------
def test_84_full_pipeline_executes_from_persisted_postgres_records(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated: Any,
) -> None:
    _seed_persisted_east_coasters(monkeypatch, pg_db_migrated.app, count=10)
    (
        search_ctx,
        scout_rep,
        analyst_rep,
        coach_rep,
        tactics_rep,
        director_rep,
    ) = run_advisory_pipeline_from_persisted_community_records(pg_db_migrated.app)

    assert len(search_ctx.baseline_matches) == 10
    assert scout_rep.team_evidence.matches_played == 10
    assert analyst_rep.overall_confidence == "MODERATE_CONFIDENCE"
    assert coach_rep.overall_confidence == "MODERATE_CONFIDENCE"
    assert len(coach_rep.hypotheses) >= 1
    assert tactics_rep.overall_confidence == "MODERATE_CONFIDENCE"
    assert len(tactics_rep.candidates) >= 1
    assert director_rep.overall_confidence == "MODERATE_CONFIDENCE"
    assert (
        len(director_rep.sequenced_candidates)
        + len(director_rep.deferred_candidates)
        + len(director_rep.rejected_candidates)
    ) == len(tactics_rep.candidates) + len(coach_rep.blocked_finding_ids)


# ---------------------------------------------------------------------------
# 85. Full pipeline executes when poll worker inserts new matches
# ---------------------------------------------------------------------------
def test_85_full_pipeline_executes_when_poll_worker_inserts_new_matches(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated: Any,
) -> None:
    _enable_all_polling_gates(monkeypatch)
    env, _ = _build_validated_envelope(count=10, start_id=996001, players_per_match=6)
    runtime = FC27PollingRuntime()
    res = run_fc27_poll_cycle(
        runtime=runtime,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.app,
        fetch_matches_fn=lambda: env,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert res.poll_status == "NEW_MATCHES_PERSISTED"
    assert res.inserted_match_count == 10
    assert res.analytics_executed is True
    assert res.persistence_before_analytics is True
    assert res.confidence_tier == "MODERATE_CONFIDENCE"


# ---------------------------------------------------------------------------
# 86. Full pipeline is skipped when poll worker sees only duplicate matches
# ---------------------------------------------------------------------------
def test_86_full_pipeline_skipped_when_poll_worker_sees_only_duplicate_matches(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated: Any,
) -> None:
    _enable_all_polling_gates(monkeypatch)
    env, _ = _build_validated_envelope(count=10, start_id=997001, players_per_match=6)
    first = run_fc27_poll_cycle(
        runtime=FC27PollingRuntime(),
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.app,
        fetch_matches_fn=lambda: env,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert first.poll_status == "NEW_MATCHES_PERSISTED"
    assert first.analytics_executed is True

    second = run_fc27_poll_cycle(
        runtime=FC27PollingRuntime(),
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.app,
        fetch_matches_fn=lambda: env,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert second.poll_status == "NO_NEW_MATCHES"
    assert second.duplicate_ignored_count == 10
    assert second.analytics_executed is False


# ---------------------------------------------------------------------------
# 87. Discord /fc27 status includes 12-hour polling and repository health
# ---------------------------------------------------------------------------
def test_87_discord_status_includes_12_hour_polling_and_repository_health(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated: Any,
) -> None:
    runtime = _seed_persisted_east_coasters(monkeypatch, pg_db_migrated.app, count=10)
    bot = FC27DiscordBotService(
        FC27DiscordBotConfig(
            enabled=True,
            allowed_guild_ids=frozenset({"123456789012345678"}),
            allowed_channel_ids=frozenset({"234567890123456789"}),
        ),
        db_conn_or_uri=pg_db_migrated.app,
        polling_runtime=runtime,
    )
    resp = bot.handle_slash_command(
        "status",
        guild_id="123456789012345678",
        channel_id="234567890123456789",
    )
    assert resp.status == "OK"
    fields = {k: v for emb in resp.embeds for k, v in emb.fields.items()}
    assert fields["poll_interval"] == "43200s (ONCE_EVERY_12_HOURS)"
    assert fields["daily_request_limit"] == "2"
    assert fields["maximum_requests_per_cycle"] == "1"
    assert fields["maximum_retries"] == "0"
    assert fields["single_flight_enabled"] == "true"
    assert fields["matches_inserted"] == "10"
    assert fields["database_availability"] == "AVAILABLE"
    assert fields["database_match_count"] == "10"


# ---------------------------------------------------------------------------
# 88. Discord advisory commands expose Coach, Tactics, Director, and Data Gaps
# ---------------------------------------------------------------------------
def test_88_discord_advisory_commands_expose_all_advisory_sections(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated: Any,
) -> None:
    runtime = _seed_persisted_east_coasters(monkeypatch, pg_db_migrated.app, count=10)
    bot = FC27DiscordBotService(
        FC27DiscordBotConfig(
            enabled=True,
            allowed_guild_ids=frozenset({"123456789012345678"}),
            allowed_channel_ids=frozenset({"234567890123456789"}),
        ),
        db_conn_or_uri=pg_db_migrated.app,
        polling_runtime=runtime,
    )
    for cmd in ("analysis", "coach", "tactics", "director", "data-gaps"):
        resp = bot.handle_slash_command(
            cmd,
            guild_id="123456789012345678",
            channel_id="234567890123456789",
        )
        assert resp.status == "OK", f"Command {cmd} failed"
        assert resp.provider_requests_executed == 0
        assert bot.config.database_write_access is False
        assert len(resp.embeds) >= 1


# ---------------------------------------------------------------------------
# 89. Discord status and polling embeds display required source, 12-hour polling, and safety metadata
# ---------------------------------------------------------------------------
def test_89_discord_status_displays_required_source_polling_and_safety_metadata(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated: Any,
) -> None:
    runtime = _seed_persisted_east_coasters(monkeypatch, pg_db_migrated.app, count=10)
    bot = FC27DiscordBotService(
        FC27DiscordBotConfig(
            enabled=True,
            allowed_guild_ids=frozenset({"123456789012345678"}),
            allowed_channel_ids=frozenset({"234567890123456789"}),
        ),
        db_conn_or_uri=pg_db_migrated.app,
        polling_runtime=runtime,
    )
    resp = bot.handle_slash_command(
        "status",
        guild_id="123456789012345678",
        channel_id="234567890123456789",
    )
    fields = {k: v for emb in resp.embeds for k, v in emb.fields.items()}
    assert fields["source_classification"] == "LIVE_EA_CLUBS_COMMUNITY_CLIENT"
    assert "ONCE_EVERY_12_HOURS" in fields["poll_interval"]
    assert "43200s" in fields["poll_interval"]
    assert "RECOMMENDATIONS=DISABLED" in fields["governance"]


# ---------------------------------------------------------------------------
# 90. Discord embeds display no gamer tags, raw player names, or raw payloads
# ---------------------------------------------------------------------------
def test_90_discord_embeds_display_no_gamer_tags_or_raw_payloads(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated: Any,
) -> None:
    runtime = _seed_persisted_east_coasters(monkeypatch, pg_db_migrated.app, count=10)
    bot = FC27DiscordBotService(
        FC27DiscordBotConfig(
            enabled=True,
            allowed_guild_ids=frozenset({"123456789012345678"}),
            allowed_channel_ids=frozenset({"234567890123456789"}),
        ),
        db_conn_or_uri=pg_db_migrated.app,
        polling_runtime=runtime,
    )
    for cmd in ("status", "form", "analysis", "coach", "tactics", "director"):
        resp = bot.handle_slash_command(
            cmd,
            guild_id="123456789012345678",
            channel_id="234567890123456789",
        )
        serialized = json.dumps(resp.model_dump(mode="json"))
        for forbidden in (
            "gamer_tag",
            "playername",
            "persona_id",
            "psn_id",
            "xbox_gamertag",
            "ea_account_id",
            "EastCoastPlayer",
        ):
            assert forbidden.lower() not in serialized.lower()


# ---------------------------------------------------------------------------
# 91. Discord bot agrees with persisted advisory pipeline on match count and confidence tier
# ---------------------------------------------------------------------------
def test_91_discord_bot_agrees_with_persisted_advisory_pipeline_on_match_count_and_confidence_tier(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated: Any,
) -> None:
    runtime = _seed_persisted_east_coasters(monkeypatch, pg_db_migrated.app, count=10)
    (
        search_ctx,
        _scout_rep,
        analyst_rep,
        _coach_rep,
        _tactics_rep,
        _director_rep,
    ) = run_advisory_pipeline_from_persisted_community_records(pg_db_migrated.app)

    bot = FC27DiscordBotService(
        FC27DiscordBotConfig(
            enabled=True,
            allowed_guild_ids=frozenset({"123456789012345678"}),
            allowed_channel_ids=frozenset({"234567890123456789"}),
        ),
        db_conn_or_uri=pg_db_migrated.app,
        polling_runtime=runtime,
    )
    bot_status = bot.handle_slash_command(
        "status",
        guild_id="123456789012345678",
        channel_id="234567890123456789",
    )
    bot_form = bot.handle_slash_command(
        "form",
        guild_id="123456789012345678",
        channel_id="234567890123456789",
    )
    assert bot_status.row_count == len(search_ctx.baseline_matches) == 10
    assert (
        bot_form.embeds[0].fields["confidence_tier"]
        == analyst_rep.overall_confidence
        == "MODERATE_CONFIDENCE"
    )



# ---------------------------------------------------------------------------
# 92. Backup -> restore -> full advisory pipeline produces identical hashes
# ---------------------------------------------------------------------------
def test_92_backup_restore_full_advisory_pipeline_produces_identical_hashes(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated: Any,
    pg_admin_uri: str,
    tmp_path: Path,
) -> None:
    fixed_dt = "2026-11-01T12:00:00Z"
    _seed_persisted_east_coasters(monkeypatch, pg_db_migrated.app, count=10)
    (
        s1,
        sc1,
        a1,
        c1,
        t1,
        d1,
    ) = run_advisory_pipeline_from_persisted_community_records(
        pg_db_migrated.app,
        analysis_generated_at=fixed_dt,
    )

    backup_dir = tmp_path / ".local-data" / "backups"
    archive_path, manifest_path, _ = create_fc27_backup(
        pg_db_migrated.admin,
        backup_dir=backup_dir,
    )
    verify_res = verify_fc27_backup_restore(
        manifest_path=manifest_path,
        archive_path=archive_path,
        admin_db_uri=pg_admin_uri,
    )
    assert verify_res.restore_verification == "PASS"
    assert verify_res.search_scout_analyst_verified is True

    (
        s2,
        sc2,
        a2,
        c2,
        t2,
        d2,
    ) = run_advisory_pipeline_from_persisted_community_records(
        pg_db_migrated.app,
        analysis_generated_at=fixed_dt,
    )
    assert s1.context_hash == s2.context_hash
    assert sc1.report_hash == sc2.report_hash
    assert a1.report_hash == a2.report_hash
    assert c1.report_hash == c2.report_hash
    assert t1.report_hash == t2.report_hash
    assert d1.report_hash == d2.report_hash


# ---------------------------------------------------------------------------
# 93. PostgresSaver tables remain untouched after full advisory pipeline
# ---------------------------------------------------------------------------
def test_93_postgressaver_tables_untouched_after_full_advisory_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated: Any,
) -> None:
    _seed_persisted_east_coasters(monkeypatch, pg_db_migrated.app, count=10)
    run_advisory_pipeline_from_persisted_community_records(pg_db_migrated.app)
    with psycopg.connect(pg_db_migrated.admin) as admin_conn:
        for tbl in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            exists = admin_conn.execute(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_name = %s",
                (tbl,),
            ).fetchone()[0]
            if exists:
                cnt = admin_conn.execute(f"SELECT COUNT(*) FROM public.{tbl}").fetchone()[0]
                assert int(cnt) == 0


# ---------------------------------------------------------------------------
# 94. Approval tables remain untouched after full advisory pipeline
# ---------------------------------------------------------------------------
def test_94_approval_tables_untouched_after_full_advisory_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated: Any,
) -> None:
    with psycopg.connect(pg_db_migrated.admin) as admin_conn:
        before = admin_conn.execute(
            "SELECT COUNT(*) FROM public.approval_requests"
        ).fetchone()[0]
    _seed_persisted_east_coasters(monkeypatch, pg_db_migrated.app, count=10)
    run_advisory_pipeline_from_persisted_community_records(pg_db_migrated.app)
    with psycopg.connect(pg_db_migrated.admin) as admin_conn:
        after = admin_conn.execute(
            "SELECT COUNT(*) FROM public.approval_requests"
        ).fetchone()[0]
    assert int(before) == int(after) == 0


# ---------------------------------------------------------------------------
# 95. Dispatch tables remain untouched after full advisory pipeline
# ---------------------------------------------------------------------------
def test_95_dispatch_tables_untouched_after_full_advisory_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated: Any,
) -> None:
    with psycopg.connect(pg_db_migrated.admin) as admin_conn:
        before = admin_conn.execute(
            "SELECT COUNT(*) FROM public.dispatches"
        ).fetchone()[0]
    _seed_persisted_east_coasters(monkeypatch, pg_db_migrated.app, count=10)
    run_advisory_pipeline_from_persisted_community_records(pg_db_migrated.app)
    with psycopg.connect(pg_db_migrated.admin) as admin_conn:
        after = admin_conn.execute(
            "SELECT COUNT(*) FROM public.dispatches"
        ).fetchone()[0]
    assert int(before) == int(after) == 0


# ---------------------------------------------------------------------------
# 96. MatchPulse Live remains untouched and isolated
# ---------------------------------------------------------------------------
def test_96_matchpulse_live_remains_untouched_and_isolated() -> None:
    for mod_path in (
        REPO_ROOT / "src" / "fc27_coach.py",
        REPO_ROOT / "src" / "fc27_tactics.py",
        REPO_ROOT / "src" / "fc27_director.py",
        REPO_ROOT / "src" / "fc27_discord_bot.py",
        REPO_ROOT / "src" / "fc27_backup_restore.py",
    ):
        text = mod_path.read_text(encoding="utf-8")
        assert "matchpulse" not in text.lower(), f"{mod_path.name} references matchpulse"


# ---------------------------------------------------------------------------
# 97. Pytest executes zero live EA network calls and zero live Discord calls
# ---------------------------------------------------------------------------
def test_97_pytest_executes_zero_live_ea_and_discord_network_calls() -> None:
    with pytest.raises(AssertionError, match="Blocked external DNS resolution"):
        socket.getaddrinfo("proclubs.ea.com", 443)
    with pytest.raises(AssertionError, match="Blocked external DNS resolution"):
        socket.getaddrinfo("discord.com", 443)


# ---------------------------------------------------------------------------
# 99. MatchPulse boundary check passes
# ---------------------------------------------------------------------------
def test_99_matchpulse_boundary_check_passes() -> None:
    proc = subprocess.run(
        [sys.executable, "scripts/check_matchpulse_boundary.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"MatchPulse boundary check failed:\n{proc.stdout}\n{proc.stderr}"
