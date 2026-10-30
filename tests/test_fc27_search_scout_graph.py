"""Focused unit and PostgreSQL integration tests for LangGraph Search/Scout adapters and reader boundary (Items 89-105).

All tests use synthetic normalized fixtures only and block external network and DNS access.
"""
from __future__ import annotations

import copy
import hashlib
import os
import socket
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
import pytest

from src import events, roles, settings
from src.db_migrate import migrate
from src.db_roles import bootstrap_cluster, bootstrap_database
from src.fc27_repository import apply_export_plan, insert_quarantine_record
from src.fc27_search_scout import (
    BASELINE_MATCH_LIMIT,
    FC27AnalyticsReader,
    FC27ScopeViolationError,
    FC27SearchScoutError,
    InMemoryFC27AnalyticsReader,
    PostgresFC27AnalyticsReader,
    scout_node,
    search_node,
)
from src.fc27_user_export import (
    compute_payload_sha256,
    parse_fc27_user_export_json,
    plan_fc27_user_export_import,
)

REF_NOW = datetime(2026, 10, 29, 18, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _block_external_network_and_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block all external network and DNS resolution unless connecting to localhost Postgres."""
    orig_getaddrinfo = socket.getaddrinfo

    def _guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if str(host) in {"127.0.0.1", "localhost", "::1"}:
            return orig_getaddrinfo(host, *args, **kwargs)
        raise AssertionError(f"External DNS resolution blocked for host: {host}")

    def _forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("External network or HTTP request is strictly forbidden")

    monkeypatch.setattr(socket, "getaddrinfo", _guarded_getaddrinfo)
    monkeypatch.setattr(socket, "gethostbyname", _forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", _forbidden)


def _sha256(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _make_synthetic_matches(count: int = 6) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for i in range(1, count + 1):
        m_id = f"m_graph_{i:04d}"
        dt = (REF_NOW - timedelta(hours=i * 4)).strftime("%Y-%m-%dT%H:%M:%SZ")
        res = "WIN" if i % 2 == 1 else "DRAW"
        gf = 2 if res == "WIN" else 1
        ga = 1
        matches.append(
            {
                "match_id": m_id,
                "match_hash": _sha256(f"graph:{m_id}:{res}:{gf}:{ga}"),
                "payload_sha256": _sha256(f"payload:{m_id}"),
                "club_id": "330323",
                "club_name": "East Coasters",
                "platform": "common-gen5",
                "game_version": "FC27",
                "played_at": dt,
                "completion_state": "COMPLETED",
                "result": res,
                "goals_for": gf,
                "goals_against": ga,
                "formation": "4-3-3(2)",
                "opponent_name": "Synthetic City FC",
                "match_type": "LEAGUE",
                "data_quality_status": "ACCEPTED",
                "player_appearances": [
                    {
                        "player_ref": "plr_ecf_000000000001",
                        "position": "ST",
                        "role": "Advanced Forward",
                        "role_focus": "Attack",
                        "build_reference": "bld_ecf_st_01",
                        "started": True,
                        "minutes_played": 90,
                        "match_rating": 7.8,
                        "goals": gf,
                        "assists": 0,
                        "passes_attempted": 18,
                        "passes_completed": 15,
                        "tackles_attempted": 3,
                        "tackles_won": 2,
                        "interceptions": 1,
                        "shots": 3,
                        "shots_on_target": 2,
                        "saves": 0,
                    },
                    {
                        "player_ref": "plr_ecf_000000000002",
                        "position": "CM",
                        "role": "Box-to-Box",
                        "role_focus": "Balanced",
                        "build_reference": "bld_ecf_cm_01",
                        "started": True,
                        "minutes_played": 90,
                        "match_rating": 7.4,
                        "goals": 0,
                        "assists": gf,
                        "passes_attempted": 28,
                        "passes_completed": 24,
                        "tackles_attempted": 5,
                        "tackles_won": 4,
                        "interceptions": 3,
                        "shots": 1,
                        "shots_on_target": 1,
                        "saves": 0,
                    },
                ],
            }
        )
    return matches


def _initial_graph_state() -> dict[str, Any]:
    return {
        "thread_id": "fc27-330323-run-20261029-0001",
        "club_id": "330323",
        "fc_version": "27",
        "platform": "common-gen5",
        "status": "running",
        "errors": [],
        "audit_log": [],
    }


# ==============================================================================
# Items 89-105: Graph Adapters and Reader Scope Tests
# ==============================================================================


def test_89_search_adapter_accepts_valid_state() -> None:
    reader = InMemoryFC27AnalyticsReader(matches=_make_synthetic_matches(6))
    state = _initial_graph_state()
    update = search_node(state, reader=reader, analysis_generated_at=REF_NOW)
    assert "search_context" in update
    assert update["search_context"]["eligible_match_count"] == 6
    assert update["analysis_confidence"] == "LOW_CONFIDENCE"
    assert len(update["analysis_report_hash"]) == 64


def test_90_scout_adapter_accepts_search_output() -> None:
    reader = InMemoryFC27AnalyticsReader(matches=_make_synthetic_matches(6))
    state = _initial_graph_state()
    s_update = search_node(state, reader=reader, analysis_generated_at=REF_NOW)
    combined_state = {**state, **s_update}
    sc_update = scout_node(combined_state)
    assert sc_update["scout_report"] is not None
    assert sc_update["scout_report"]["eligible_match_count"] == 6
    assert sc_update["analysis_report_hash"] == sc_update["scout_report"]["report_hash"]


def test_91_incoming_state_not_mutated() -> None:
    reader = InMemoryFC27AnalyticsReader(matches=_make_synthetic_matches(4))
    state = _initial_graph_state()
    state_copy = copy.deepcopy(state)
    s_update = search_node(state, reader=reader, analysis_generated_at=REF_NOW)
    assert state == state_copy

    combined = {**state, **s_update}
    combined_copy = copy.deepcopy(combined)
    _ = scout_node(combined)
    assert combined == combined_copy


def test_92_bounded_state_update() -> None:
    reader = InMemoryFC27AnalyticsReader(matches=_make_synthetic_matches(25))
    state = _initial_graph_state()
    s_update = search_node(state, reader=reader, analysis_generated_at=REF_NOW)
    sc_update = scout_node({**state, **s_update})
    assert set(sc_update.keys()) == {
        "search_context",
        "scout_report",
        "analysis_report_hash",
        "analysis_confidence",
        "analysis_limitations",
    }
    assert len(sc_update["search_context"]["baseline_matches"]) == BASELINE_MATCH_LIMIT == 20


def test_93_no_database_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbid_connect(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Direct database connection/write forbidden in node functions")

    monkeypatch.setattr(psycopg, "connect", _forbid_connect)
    reader = InMemoryFC27AnalyticsReader(matches=_make_synthetic_matches(5))
    state = _initial_graph_state()
    s_update = search_node(state, reader=reader, analysis_generated_at=REF_NOW)
    sc_update = scout_node({**state, **s_update})
    assert sc_update["scout_report"]["eligible_match_count"] == 5


def test_94_no_event_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbid_event(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("record_event writes are forbidden in Search/Scout")

    monkeypatch.setattr(events, "record_event", _forbid_event)
    reader = InMemoryFC27AnalyticsReader(matches=_make_synthetic_matches(5))
    state = _initial_graph_state()
    s_update = search_node(state, reader=reader, analysis_generated_at=REF_NOW)
    sc_update = scout_node({**state, **s_update})
    assert sc_update["scout_report"]["eligible_match_count"] == 5


def test_95_no_outbox_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = InMemoryFC27AnalyticsReader(matches=_make_synthetic_matches(5))
    state = _initial_graph_state()
    s_update = search_node(state, reader=reader, analysis_generated_at=REF_NOW)
    sc_update = scout_node({**state, **s_update})
    assert "outbox" not in sc_update


def test_96_no_network_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbid_net(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Network call attempted")

    monkeypatch.setattr(socket, "create_connection", _forbid_net)
    reader = InMemoryFC27AnalyticsReader(matches=_make_synthetic_matches(4))
    state = _initial_graph_state()
    s_update = search_node(state, reader=reader, analysis_generated_at=REF_NOW)
    sc_update = scout_node({**state, **s_update})
    assert sc_update["analysis_confidence"] == "EXPLORATORY"


def test_97_no_dns_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbid_dns(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("DNS call attempted")

    monkeypatch.setattr(socket, "getaddrinfo", _forbid_dns)
    monkeypatch.setattr(socket, "gethostbyname", _forbid_dns)
    reader = InMemoryFC27AnalyticsReader(matches=_make_synthetic_matches(4))
    state = _initial_graph_state()
    s_update = search_node(state, reader=reader, analysis_generated_at=REF_NOW)
    sc_update = scout_node({**state, **s_update})
    assert sc_update["analysis_confidence"] == "EXPLORATORY"


def test_98_no_llm_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbid_llm(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("LLM call attempted in Search or Scout")

    monkeypatch.setattr(roles, "run_role", _forbid_llm)
    reader = InMemoryFC27AnalyticsReader(matches=_make_synthetic_matches(4))
    state = _initial_graph_state()
    s_update = search_node(state, reader=reader, analysis_generated_at=REF_NOW)
    sc_update = scout_node({**state, **s_update})
    assert sc_update["scout_report"] is not None


def test_99_no_approval_interaction() -> None:
    reader = InMemoryFC27AnalyticsReader(matches=_make_synthetic_matches(4))
    state = _initial_graph_state()
    s_update = search_node(state, reader=reader, analysis_generated_at=REF_NOW)
    sc_update = scout_node({**state, **s_update})
    assert "approval_request" not in sc_update
    assert "approval_decision" not in sc_update
    assert "lead_packages" not in sc_update


def test_100_no_dispatch_interaction() -> None:
    reader = InMemoryFC27AnalyticsReader(matches=_make_synthetic_matches(4))
    state = _initial_graph_state()
    s_update = search_node(state, reader=reader, analysis_generated_at=REF_NOW)
    sc_update = scout_node({**state, **s_update})
    assert "recommendations" not in sc_update
    assert "dispatches" not in sc_update


def test_101_identical_input_and_timestamp_produce_identical_output() -> None:
    matches = _make_synthetic_matches(10)
    r1 = InMemoryFC27AnalyticsReader(matches=matches)
    r2 = InMemoryFC27AnalyticsReader(matches=list(reversed(matches)))
    state = _initial_graph_state()
    u1 = scout_node({**state, **search_node(state, reader=r1, analysis_generated_at=REF_NOW)})
    u2 = scout_node({**state, **search_node(state, reader=r2, analysis_generated_at=REF_NOW)})
    assert u1 == u2
    assert u1["analysis_report_hash"] == u2["analysis_report_hash"]


def test_102_reader_limit_enforced() -> None:
    reader = InMemoryFC27AnalyticsReader(matches=_make_synthetic_matches(5))
    with pytest.raises(FC27SearchScoutError, match="limit"):
        reader.get_recent_eligible_matches(limit=0)
    with pytest.raises(FC27SearchScoutError, match="limit"):
        reader.get_recent_eligible_matches(limit=21)


def test_103_reader_club_scope_enforced() -> None:
    reader = InMemoryFC27AnalyticsReader(matches=_make_synthetic_matches(3))
    with pytest.raises(FC27ScopeViolationError, match="club_id"):
        reader.get_club_identity(club_id="999999")
    bad_state = {**_initial_graph_state(), "club_id": "999999"}
    with pytest.raises(FC27ScopeViolationError, match="club_id"):
        search_node(bad_state, reader=reader, analysis_generated_at=REF_NOW)


def test_104_reader_platform_scope_enforced() -> None:
    reader = InMemoryFC27AnalyticsReader(matches=_make_synthetic_matches(3))
    with pytest.raises(FC27ScopeViolationError, match="platform"):
        reader.get_recent_eligible_matches(platform="common-gen4")
    bad_state = {**_initial_graph_state(), "platform": "common-gen4"}
    with pytest.raises(FC27ScopeViolationError, match="platform"):
        search_node(bad_state, reader=reader, analysis_generated_at=REF_NOW)


def test_105_reader_game_version_scope_enforced() -> None:
    reader = InMemoryFC27AnalyticsReader(matches=_make_synthetic_matches(3))
    with pytest.raises(FC27ScopeViolationError, match="game_version"):
        reader.get_club_statistics(game_version="FC26")
    bad_state = {**_initial_graph_state(), "fc_version": "26"}
    with pytest.raises(FC27ScopeViolationError, match="game_version"):
        search_node(bad_state, reader=reader, analysis_generated_at=REF_NOW)


def test_postgres_analytics_reader_integration(
    pg_admin_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Integration test for `PostgresFC27AnalyticsReader` against disposable PostgreSQL 16."""
    import json
    from tests.test_fc27_dashboard_queries import (
        _make_export_dict,
        _make_match_dict,
        _plan_and_apply,
    )

    bootstrap_cluster(
        pg_admin_uri,
        {
            "fc27_migrator": None,
            "fc27_app": None,
            "fc27_dispatch": None,
            "fc27_relay": None,
        },
    )
    db_name = f"fc27_ss_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(pg_admin_uri, autocommit=True) as admin_conn:
        admin_conn.execute(f'CREATE DATABASE "{db_name}"')

    info = psycopg.conninfo.conninfo_to_dict(pg_admin_uri)
    info["dbname"] = db_name
    admin_db_uri = psycopg.conninfo.make_conninfo(**info)

    def _role_uri(user: str) -> str:
        d = dict(info)
        d["user"] = user
        return psycopg.conninfo.make_conninfo(**d)

    mig_uri = _role_uri("fc27_migrator")
    app_uri = _role_uri("fc27_app")

    try:
        bootstrap_database(admin_db_uri, legacy_owner=None, confirm=True)
        migrate(mig_uri)

        matches_raw = [
            _make_match_dict(
                f"m_pg_{i:03d}",
                REF_NOW - timedelta(hours=i * 3),
                result="WIN",
                goals_for=2,
                goals_against=0,
                formation="4-3-3(2)",
            )
            for i in range(1, 4)
        ]
        export_dict = _make_export_dict("exp-ss-pg-0001", matches_raw)
        _plan_and_apply(app_uri, export_dict)

        # Enable read-only analytics inside monkeypatch scope only
        monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "true")
        pg_reader = PostgresFC27AnalyticsReader(app_uri)
        assert isinstance(pg_reader, FC27AnalyticsReader)

        state = _initial_graph_state()
        s_update = search_node(state, reader=pg_reader, analysis_generated_at=REF_NOW)
        sc_update = scout_node({**state, **s_update})

        assert sc_update["search_context"]["eligible_match_count"] == 3
        assert sc_update["analysis_confidence"] == "EXPLORATORY"
        serialized = json.dumps(sc_update)
        assert "Approved_Alias_01" not in serialized
        assert "Unapproved_Alias_02" not in serialized
    finally:
        with psycopg.connect(pg_admin_uri, autocommit=True) as admin_conn:
            admin_conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                (db_name,),
            )
            admin_conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
