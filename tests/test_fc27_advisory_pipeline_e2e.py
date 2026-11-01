"""End-to-End Integration, Dashboard, and Safety Tests for the FC27 Advisory Pipeline (`PHASE 7`, `PHASE 8`, Tests #84–#100).

Verifies the complete read-only advisory pipeline:
`Search -> Scout -> Analyst -> Coach -> Tactics -> Director`
plus dashboard projection (`/demo`, `/demo/status`, `/demo/analysis`), Discord bot parity,
backup/restore hash determinism, isolation from PostgresSaver/approval/dispatch/MatchPulse Live,
and repository governance invariants.

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
from fastapi.testclient import TestClient

from src.fc27_backup_restore import create_fc27_backup, verify_fc27_backup_restore
from src.fc27_community_provider import (
    build_demo_session_artifacts_from_persisted_records,
    run_advisory_pipeline_from_persisted_community_records,
)
from src.fc27_demo import FC27DemoStateStore
from src.fc27_demo_api import create_demo_app
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
    monkeypatch.setenv("FC27_DEMO_ENABLED", "true")
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
# 87. Dashboard /demo/status includes 12-hour polling and advisory pipeline health
# ---------------------------------------------------------------------------
def test_87_dashboard_demo_status_includes_12_hour_polling_and_advisory_pipeline_health(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated: Any,
) -> None:
    runtime = _seed_persisted_east_coasters(monkeypatch, pg_db_migrated.app, count=10)
    artifacts = build_demo_session_artifacts_from_persisted_records(pg_db_migrated.app)
    store = FC27DemoStateStore()
    store.set_artifacts(artifacts)
    app = create_demo_app(state_store=store, demo_mode_override=True, polling_runtime=runtime)
    client = TestClient(app)

    resp = client.get("/demo/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["polling"]["polling_frequency"] == "ONCE_EVERY_12_HOURS"
    assert body["polling"]["poll_interval_seconds"] == 43200
    assert body["polling"]["max_provider_requests_per_24_hours"] == 2
    assert body["polling"]["catch_up_bursts_allowed"] is False

    adv = body["advisory_pipeline"]
    assert adv["pipeline_order"] == [
        "Search",
        "Scout",
        "Analyst",
        "Coach",
        "Tactics",
        "Director",
    ]
    assert adv["coach_active"] is True
    assert adv["tactics_active"] is True
    assert adv["director_active"] is True
    assert adv["coach_hypotheses_count"] >= 1
    assert adv["tactics_candidates_count"] >= 1
    assert adv["recommendations_enabled"] is False
    assert adv["approval_enabled"] is False
    assert adv["dispatch_enabled"] is False


# ---------------------------------------------------------------------------
# 88. Dashboard /demo/analysis includes Search, Scout, Analyst, Coach, Tactics, Director, Data Gaps, and Unresolved Questions
# ---------------------------------------------------------------------------
def test_88_dashboard_demo_analysis_includes_all_advisory_sections(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated: Any,
) -> None:
    _seed_persisted_east_coasters(monkeypatch, pg_db_migrated.app, count=10)
    artifacts = build_demo_session_artifacts_from_persisted_records(pg_db_migrated.app)
    store = FC27DemoStateStore()
    store.set_artifacts(artifacts)
    app = create_demo_app(state_store=store, demo_mode_override=True)
    client = TestClient(app)

    resp = client.get("/demo/analysis")
    assert resp.status_code == 200
    body = resp.json()
    for required_section in (
        "search",
        "scout",
        "analyst",
        "coach",
        "tactics",
        "director",
        "data_gaps",
        "unresolved_questions",
    ):
        assert required_section in body, f"Missing {required_section} in /demo/analysis"
    assert body["coach"] is not None
    assert len(body["coach"]["hypotheses"]) >= 1
    assert body["tactics"] is not None
    assert len(body["tactics"]["candidates"]) >= 1
    assert body["director"] is not None
    assert len(body["data_gaps"]) >= 1
    assert len(body["unresolved_questions"]) >= 1
    assert body["governance"]["recommendations_enabled"] is False
    assert body["governance"]["approval_enabled"] is False
    assert body["governance"]["dispatch_enabled"] is False


# ---------------------------------------------------------------------------
# 89. Dashboard /demo HTML displays required source, 12-hour polling, and safety banners
# ---------------------------------------------------------------------------
def test_89_dashboard_demo_html_displays_required_source_polling_and_safety_banners(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated: Any,
) -> None:
    _seed_persisted_east_coasters(monkeypatch, pg_db_migrated.app, count=10)
    artifacts = build_demo_session_artifacts_from_persisted_records(pg_db_migrated.app)
    store = FC27DemoStateStore()
    store.set_artifacts(artifacts)
    app = create_demo_app(state_store=store, demo_mode_override=True)
    client = TestClient(app)

    resp = client.get("/demo")
    assert resp.status_code == 200
    html_text = resp.text
    assert "LIVE_EA_CLUBS_COMMUNITY_CLIENT" in html_text
    assert "ONCE_EVERY_12_HOURS" in html_text
    assert "43200s" in html_text
    assert "Recommendations Disabled | Approval Disabled | Dispatch Disabled" in html_text
    assert "Coach Hypotheses &amp; Practice Experiments" in html_text
    assert "Tactics Experimental Candidates" in html_text
    assert "Director Evidence Reconciliation" in html_text
    assert "Data Gaps &amp; Unmeasured Context" in html_text
    assert "Unresolved Questions" in html_text


# ---------------------------------------------------------------------------
# 90. Dashboard /demo HTML displays no gamer tags, raw player names, or raw payloads
# ---------------------------------------------------------------------------
def test_90_dashboard_demo_html_displays_no_gamer_tags_or_raw_payloads(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated: Any,
) -> None:
    _seed_persisted_east_coasters(monkeypatch, pg_db_migrated.app, count=10)
    artifacts = build_demo_session_artifacts_from_persisted_records(pg_db_migrated.app)
    store = FC27DemoStateStore()
    store.set_artifacts(artifacts)
    app = create_demo_app(state_store=store, demo_mode_override=True)
    client = TestClient(app)

    resp = client.get("/demo")
    assert resp.status_code == 200
    html_text = resp.text
    for forbidden in (
        "gamer_tag",
        "playername",
        "persona_id",
        "psn_id",
        "xbox_gamertag",
        "ea_account_id",
        "EastCoastPlayer",
    ):
        assert forbidden.lower() not in html_text.lower()


# ---------------------------------------------------------------------------
# 91. Discord bot and dashboard agree on persisted match count and confidence tier
# ---------------------------------------------------------------------------
def test_91_discord_bot_and_dashboard_agree_on_match_count_and_confidence_tier(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated: Any,
) -> None:
    runtime = _seed_persisted_east_coasters(monkeypatch, pg_db_migrated.app, count=10)
    artifacts = build_demo_session_artifacts_from_persisted_records(pg_db_migrated.app)
    store = FC27DemoStateStore()
    store.set_artifacts(artifacts)
    app = create_demo_app(state_store=store, demo_mode_override=True, polling_runtime=runtime)
    client = TestClient(app)

    dash_status = client.get("/demo/status").json()
    dash_analysis = client.get("/demo/analysis").json()

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
    assert bot_status.row_count == dash_status["match_count"] == 10
    assert (
        bot_form.embeds[0].fields["confidence_tier"]
        == dash_analysis["analyst"]["overall_confidence"]
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
# 98. Documentation consistency check passes
# ---------------------------------------------------------------------------
def test_98_documentation_consistency_check_passes() -> None:
    proc = subprocess.run(
        [sys.executable, "scripts/check_documentation_consistency.py", "--check", "--strict"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"Documentation check failed:\n{proc.stdout}\n{proc.stderr}"


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


# ---------------------------------------------------------------------------
# 100. Repository cleanup and inventory tests pass
# ---------------------------------------------------------------------------
def test_100_repository_cleanup_and_inventory_valid() -> None:
    inventory_path = REPO_ROOT / "docs" / "repository_inventory.json"
    assert inventory_path.exists()
    data = json.loads(inventory_path.read_text(encoding="utf-8"))
    assert "files" in data
    assert len(data["files"]) > 0
