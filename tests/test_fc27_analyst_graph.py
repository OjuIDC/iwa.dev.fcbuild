"""Focused unit and PostgreSQL integration tests for the LangGraph Analyst adapter (PART Q, Items 111-128).

All tests use synthetic normalized Search and Scout fixtures only and block external network and DNS access.
"""
from __future__ import annotations

import copy
import hashlib
import json
import socket
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from psycopg import conninfo, sql
import pytest

from src import approval_store, events, fc27_community_provider, graph, roles, swarms_backend
from src.db_migrate import migrate
from src.db_roles import bootstrap_cluster, bootstrap_database
from src.fc27_analyst import (
    MAX_ANALYST_FINDINGS,
    MAX_DATA_GAPS,
    MAX_UNRESOLVED_QUESTIONS,
    analyst_evidence_node,
)
from src.fc27_dashboard_queries import FC27AnalyticsDisabledError
from src.fc27_search_scout import (
    InMemoryFC27AnalyticsReader,
    PostgresFC27AnalyticsReader,
    scout_node,
    search_node,
)

REF_NOW = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)


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


def _make_synthetic_matches(count: int = 8) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for i in range(1, count + 1):
        m_id = f"m_ag_{i:04d}"
        dt = (REF_NOW - timedelta(hours=i * 4)).strftime("%Y-%m-%dT%H:%M:%SZ")
        res = "WIN" if i % 2 == 1 else "DRAW"
        gf = 2 if res == "WIN" else 1
        ga = 1
        matches.append(
            {
                "match_id": m_id,
                "match_hash": _sha256(f"ag:{m_id}:{res}:{gf}:{ga}"),
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
                "formation": "4-3-3(2)" if i % 2 == 1 else "4-2-3-1",
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


def _build_search_scout_state(count: int = 8) -> dict[str, Any]:
    reader = InMemoryFC27AnalyticsReader(matches=_make_synthetic_matches(count))
    base_state = {
        "thread_id": "fc27-330323-run-20261030-0001",
        "club_id": "330323",
        "fc_version": "27",
        "platform": "common-gen5",
        "status": "running",
        "errors": [],
        "audit_log": [],
    }
    s_update = search_node(base_state, reader=reader, analysis_generated_at=REF_NOW)
    sc_update = scout_node({**base_state, **s_update})
    return {**base_state, **sc_update}


# ==============================================================================
# 111-128: Analyst Graph Adapter Tests
# ==============================================================================


def test_111_analyst_adapter_accepts_valid_search_scout_state() -> None:
    state = _build_search_scout_state(8)
    update = analyst_evidence_node(state)
    assert "analyst_report" in update
    assert len(update["analyst_report_hash"]) == 64
    assert update["analyst_confidence"] == "LOW_CONFIDENCE"
    assert update["analyst_report"]["recommendation_status"] == "NOT_A_RECOMMENDATION"
    assert update["analyst_report"]["approvable"] is False
    assert update["analyst_report"]["dispatchable"] is False


def test_112_incoming_state_not_mutated() -> None:
    state = _build_search_scout_state(6)
    snapshot = copy.deepcopy(state)
    _ = analyst_evidence_node(state)
    assert state == snapshot


def test_113_state_update_bounded() -> None:
    state = _build_search_scout_state(20)
    update = analyst_evidence_node(state)
    assert set(update.keys()) == {
        "analyst_report",
        "analyst_report_hash",
        "analyst_confidence",
        "analyst_findings",
        "analyst_data_gaps",
        "analyst_unresolved_questions",
        "analyst_limitations",
    }
    assert len(update["analyst_findings"]) <= MAX_ANALYST_FINDINGS == 20
    assert len(update["analyst_data_gaps"]) <= MAX_DATA_GAPS == 20
    assert len(update["analyst_unresolved_questions"]) <= MAX_UNRESOLVED_QUESTIONS == 20
    assert "baseline_matches" not in update["analyst_report"]


def test_114_no_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbid_net(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Network call attempted in analyst_evidence_node")

    monkeypatch.setattr(socket, "create_connection", _forbid_net)
    state = _build_search_scout_state(6)
    update = analyst_evidence_node(state)
    assert update["analyst_report"] is not None


def test_115_no_dns_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbid_dns(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("DNS call attempted in analyst_evidence_node")

    monkeypatch.setattr(socket, "getaddrinfo", _forbid_dns)
    monkeypatch.setattr(socket, "gethostbyname", _forbid_dns)
    state = _build_search_scout_state(6)
    update = analyst_evidence_node(state)
    assert update["analyst_report"] is not None


def test_116_no_llm_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbid_llm(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("LLM call attempted in analyst_evidence_node")

    monkeypatch.setattr(roles, "run_role", _forbid_llm)
    state = _build_search_scout_state(6)
    update = analyst_evidence_node(state)
    assert update["analyst_report"] is not None


def test_117_no_ollama_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbid_ollama(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Ollama call attempted in analyst_evidence_node")

    if hasattr(roles, "_call_ollama"):
        monkeypatch.setattr(roles, "_call_ollama", _forbid_ollama)
    state = _build_search_scout_state(6)
    update = analyst_evidence_node(state)
    assert update["analyst_report"] is not None


def test_118_no_lm_studio_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbid_lm_studio(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("LM Studio call attempted in analyst_evidence_node")

    if hasattr(roles, "_call_lm_studio"):
        monkeypatch.setattr(roles, "_call_lm_studio", _forbid_lm_studio)
    state = _build_search_scout_state(6)
    update = analyst_evidence_node(state)
    assert update["analyst_report"] is not None


def test_119_no_swarms_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbid_swarms(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Swarms call attempted in analyst_evidence_node")

    monkeypatch.setattr(swarms_backend, "run_role_swarms", _forbid_swarms)
    state = _build_search_scout_state(6)
    update = analyst_evidence_node(state)
    assert update["analyst_report"] is not None


def test_120_no_provider_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbid_provider(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Provider call attempted in analyst_evidence_node")

    monkeypatch.setattr(
        fc27_community_provider.FC27CommunityProviderAdapter,
        "_require_gates",
        _forbid_provider,
    )
    state = _build_search_scout_state(6)
    update = analyst_evidence_node(state)
    assert update["analyst_report"] is not None


def test_121_no_postgresql_write(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbid_db(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("PostgreSQL connection/write attempted in analyst_evidence_node")

    monkeypatch.setattr(psycopg, "connect", _forbid_db)
    state = _build_search_scout_state(6)
    update = analyst_evidence_node(state)
    assert update["analyst_report"] is not None


def test_122_no_postgressaver_access(monkeypatch: pytest.MonkeyPatch) -> None:
    from langgraph.checkpoint.postgres import PostgresSaver

    def _forbid_saver(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("PostgresSaver access attempted in analyst_evidence_node")

    monkeypatch.setattr(PostgresSaver, "put", _forbid_saver)
    monkeypatch.setattr(PostgresSaver, "get_tuple", _forbid_saver)
    state = _build_search_scout_state(6)
    update = analyst_evidence_node(state)
    assert update["analyst_report"] is not None


def test_123_no_redis_event(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbid_event(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Event/Redis emission attempted in analyst_evidence_node")

    monkeypatch.setattr(events, "record_event", _forbid_event)
    state = _build_search_scout_state(6)
    update = analyst_evidence_node(state)
    assert update["analyst_report"] is not None


def test_124_no_outbox_write() -> None:
    state = _build_search_scout_state(6)
    update = analyst_evidence_node(state)
    assert "outbox" not in update


def test_125_no_approval_interaction(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbid_approval(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Approval store interaction attempted in analyst_evidence_node")

    if hasattr(approval_store.ApprovalStore, "create_pending"):
        monkeypatch.setattr(
            approval_store.ApprovalStore, "create_pending", _forbid_approval
        )
    state = _build_search_scout_state(6)
    update = analyst_evidence_node(state)
    assert "approval_request" not in update
    assert "approval_decision" not in update
    assert "lead_packages" not in update


def test_126_no_dispatch_interaction() -> None:
    state = _build_search_scout_state(6)
    update = analyst_evidence_node(state)
    assert "recommendations" not in update
    assert "dispatches" not in update


def test_127_graph_topology_unchanged() -> None:
    from langgraph.checkpoint.memory import InMemorySaver

    deps = graph.GraphDeps()
    compiled = graph.build_graph(deps).compile(checkpointer=InMemorySaver())
    node_names = set(compiled.get_graph().nodes.keys()) - {"__start__", "__end__"}
    assert node_names == {
        "search",
        "scout",
        "analyst",
        "coach",
        "tactics",
        "director",
        "prepare_approval",
        "approval",
        "finalize_decision",
    }


def test_128_analytics_disabled_path_fails_closed_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "false")
    state = _build_search_scout_state(6)
    with pytest.raises(FC27AnalyticsDisabledError):
        analyst_evidence_node(state)


def test_postgres_search_scout_analyst_end_to_end_read_only(
    pg_admin_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Integration test verifying PostgresFC27AnalyticsReader -> search_node -> scout_node -> analyst_evidence_node."""
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
    db_name = f"fc27_an_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(pg_admin_uri, autocommit=True) as admin_conn:
        admin_conn.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(db_name)))

    info = conninfo.conninfo_to_dict(pg_admin_uri)
    info["dbname"] = db_name
    admin_db_uri = conninfo.make_conninfo("", **{k: v for k, v in info.items() if v is not None and k != "conninfo"})

    def _role_uri(user: str) -> str:
        d = dict(info)
        d["user"] = user
        return conninfo.make_conninfo("", **{k: v for k, v in d.items() if v is not None and k != "conninfo"})

    mig_uri = _role_uri("fc27_migrator")
    app_uri = _role_uri("fc27_app")

    try:
        bootstrap_database(admin_db_uri, legacy_owner=None, confirm=True)
        migrate(mig_uri)

        matches_raw = [
            _make_match_dict(
                f"m_pg_an_{i:03d}",
                REF_NOW - timedelta(hours=i * 3),
                result="WIN" if i % 2 == 1 else "DRAW",
                goals_for=2 if i % 2 == 1 else 1,
                goals_against=0 if i % 2 == 1 else 1,
                formation="4-3-3(2)",
            )
            for i in range(1, 6)
        ]
        export_dict = _make_export_dict("exp-an-pg-0001", matches_raw)
        _plan_and_apply(app_uri, export_dict)

        monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "true")
        pg_reader = PostgresFC27AnalyticsReader(app_uri)

        base_state = {
            "thread_id": "fc27-330323-run-20261030-pg01",
            "club_id": "330323",
            "fc_version": "27",
            "platform": "common-gen5",
            "status": "running",
            "errors": [],
            "audit_log": [],
        }
        s_update = search_node(base_state, reader=pg_reader, analysis_generated_at=REF_NOW)
        sc_update = scout_node({**base_state, **s_update})
        an_update = analyst_evidence_node(
            {**base_state, **sc_update},
            analysis_generated_at=REF_NOW,
            require_analytics_gate=True,
        )

        assert an_update["analyst_confidence"] == "LOW_CONFIDENCE"
        assert an_update["analyst_report"]["eligible_match_count"] == 5
        assert an_update["analyst_report"]["recommendation_status"] == "NOT_A_RECOMMENDATION"
        serialized = json.dumps(an_update)
        assert "Approved_Alias_01" not in serialized
        assert "Unapproved_Alias_02" not in serialized
    finally:
        with psycopg.connect(pg_admin_uri, autocommit=True) as admin_conn:
            admin_conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                (db_name,),
            )
            admin_conn.execute(sql.SQL('DROP DATABASE IF EXISTS {}').format(sql.Identifier(db_name)))
