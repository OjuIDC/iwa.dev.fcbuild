"""Local Loopback Read-Only Demo API and Minimal HTML View (`PART K` & `PART L`).

Exposes:
- `GET /demo/status`
- `GET /demo/matches`
- `GET /demo/analysis`
- `POST /demo/ingest-fixture`
- `GET /demo` (self-contained local HTML page with zero external assets, cookies, or localStorage)

Safety & Governance Invariants:
- Binds strictly to `127.0.0.1` (`0.0.0.0` and non-loopback hosts are rejected).
- Disabled by default (`HTTP 403` unless `FC27_DEMO_MODE=true`).
- No live ingest endpoint, no provider URL parameter, no arbitrary operation parameter,
  no raw response endpoint, no player alias endpoint, and no external identifier endpoint.
- `POST /demo/ingest-fixture` accepts only UI-safe bounded scenarios:
  `valid`, `empty_history`, `duplicates`.
- Coach, Tactics, Director, recommendations, approval packages, and dispatch are absent.
"""
from __future__ import annotations

import argparse
import html
import sys
from typing import Any, Dict, Final, Literal, Optional

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from scripts.fc27_demo_fixture_server import (
    LOOPBACK_IPV4,
    UI_SAFE_FIXTURE_SCENARIOS,
)
from src import settings
from src.fc27_analyst import (
    ANALYST_POLICY_VERSION,
    ANALYST_SYNTHESIS_VERSION,
)
from src.fc27_coach import COACH_POLICY_VERSION, COACH_VERSION
from src.fc27_director import (
    DIRECTOR_POLICY_VERSION,
    DIRECTOR_REVIEW_STATUS,
    DIRECTOR_VERSION,
)
from src.fc27_tactics import (
    EXPERIMENTAL_ONLY_STATUS,
    TACTICS_POLICY_VERSION,
    TACTICS_VERSION,
)
from src.fc27_analytics import FORMULA_VERSION
from src.fc27_clubs_api_contracts import (
    TARGET_CLUB_ID,
    TARGET_CLUB_NAME,
    TARGET_GAME_VERSION,
    TARGET_PLATFORM,
)
from src.fc27_community_provider import LIVE_EA_CLUBS_COMMUNITY_CLIENT
from src.fc27_demo import (
    DEFAULT_DEMO_STATE_STORE,
    DEMO_DEFAULT_GENERATED_AT,
    DEMO_FIXTURE_LOOPBACK,
    LIVE_PROBE_EXECUTED,
    FC27DemoSessionArtifacts,
    FC27DemoStateStore,
    run_fc27_demo_ingestion_with_artifacts,
    validate_demo_host_strict,
)
from src.fc27_polling_worker import FC27PollingRuntime, get_default_polling_runtime
from src.fc27_search_scout import RANKING_POLICY_VERSION

PROHIBITED_QUERY_PARAMS: Final[frozenset[str]] = frozenset(
    {
        "url",
        "provider_url",
        "target_url",
        "endpoint",
        "host",
        "hostname",
        "port",
        "path",
        "operation",
        "source",
        "live",
        "raw",
        "raw_response",
        "retain_raw",
        "recommendations",
        "dispatch",
    }
)


class FC27DemoIngestFixtureRequest(BaseModel):
    """Strict request contract for `POST /demo/ingest-fixture` (`PART K`).

    Accepts only bounded UI-safe scenarios (`valid`, `empty_history`, `duplicates`)
    and forbids arbitrary URLs, operations, sources, or extra fields.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario: Literal["valid", "empty_history", "duplicates"] = Field(default="valid")


def _require_demo_mode_enabled(override: Optional[bool]) -> None:
    enabled = settings.is_fc27_demo_mode_enabled() if override is None else bool(override)
    if not enabled:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "FC27_DEMO_MODE_DISABLED",
                "message": "Local FC27 demo API is disabled by default; set FC27_DEMO_MODE=true.",
            },
        )


def _reject_prohibited_query_params(request: Request) -> None:
    if len(request.query_params) > 0:
        for key in request.query_params.keys():
            if key.strip().lower() in PROHIBITED_QUERY_PARAMS:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": "PROHIBITED_QUERY_PARAMETER",
                        "message": f"Query parameter '{key}' is prohibited on demo endpoints.",
                    },
                )
        raise HTTPException(
            status_code=400,
            detail={
                "error": "UNEXPECTED_QUERY_PARAMETER",
                "message": "Demo endpoints do not accept arbitrary query parameters.",
            },
        )


def _ensure_artifacts(
    store: FC27DemoStateStore,
    *,
    demo_mode_override: Optional[bool],
    fixture_port: Optional[int],
) -> FC27DemoSessionArtifacts:
    existing = store.get_artifacts()
    if existing is not None:
        return existing
    return run_fc27_demo_ingestion_with_artifacts(
        source="fixture",
        operation="full-demo",
        scenario="valid",
        fixture_port=fixture_port,
        demo_mode_enabled=True if demo_mode_override is None else demo_mode_override,
        analysis_generated_at=DEMO_DEFAULT_GENERATED_AT,
        cache=store.cache,
        rate_limiter=store.limiter,
        circuit_breaker=store.breaker,
        state_store=store,
    )


LIVE_EA_CLUBS_COMMUNITY_CLIENT_LABEL: Final[str] = (
    "Live EA Clubs Data via pinned community client"
)
SYNTHETIC_FIXTURE_LABEL: Final[str] = "Synthetic Fixture Data"


def _resolve_source_classification(source_mode: str) -> str:
    if source_mode in (
        LIVE_EA_CLUBS_COMMUNITY_CLIENT,
        "LIVE_EA_CLUBS_READ_ONLY",
        "LIVE_EA_CLUBS_BROWSER_OBSERVED",
    ):
        return source_mode
    return "SYNTHETIC_FIXTURE"


def resolve_dashboard_source_label(source_cls: str, *, include_mode_suffix: bool = False) -> str:
    """Resolves the human-readable dashboard source banner label from source classification."""
    if source_cls == LIVE_EA_CLUBS_COMMUNITY_CLIENT:
        if include_mode_suffix:
            return f"{LIVE_EA_CLUBS_COMMUNITY_CLIENT_LABEL} ({source_cls})"
        return LIVE_EA_CLUBS_COMMUNITY_CLIENT_LABEL
    return SYNTHETIC_FIXTURE_LABEL


def _build_status_dict(
    artifacts: FC27DemoSessionArtifacts,
    store: FC27DemoStateStore,
    polling_runtime: Optional[FC27PollingRuntime] = None,
) -> Dict[str, Any]:
    res = artifacts.result
    cache_metrics = store.cache.metrics()
    source_cls = _resolve_source_classification(res.source_mode)
    last_ingested_at = artifacts.search_context.analysis_generated_at
    provider_health = (
        "HEALTHY" if res.circuit_state == "CLOSED" and res.validation_status == "PASS" else "DEGRADED"
    )
    poll_rt = polling_runtime or getattr(store, "polling_runtime", None) or get_default_polling_runtime()
    poll_health = poll_rt.snapshot_health()
    last_attempt_iso = (
        poll_health.last_attempt_at.isoformat() if poll_health.last_attempt_at else None
    )
    last_success_iso = (
        poll_health.last_success_at.isoformat() if poll_health.last_success_at else None
    )
    last_new_match_iso = (
        poll_health.last_new_match_at.isoformat() if poll_health.last_new_match_at else None
    )
    next_eligible_iso = (
        poll_health.next_eligible_at.isoformat() if poll_health.next_eligible_at else None
    )
    return {
        "demo_enabled": res.demo_mode,
        "source_mode": res.source_mode,
        "source_classification": source_cls,
        "synthetic_data_classification": source_cls,
        "source_label": resolve_dashboard_source_label(source_cls),
        "club_identity": dict(res.identity),
        "last_operation": artifacts.last_operation,
        "last_ingestion_time": last_ingested_at,
        "provider_health": provider_health,
        "scenario": artifacts.scenario,
        "request_count": res.request_count,
        "cache_status": {
            "hits": res.cache_hits,
            "misses": res.cache_misses,
            "total_hits": cache_metrics.hits,
            "total_misses": cache_metrics.misses,
            "entries": cache_metrics.entry_count,
        },
        "rate_limit_status": res.rate_limit_status,
        "request_budget_status": res.request_budget_status,
        "circuit_state": res.circuit_state,
        "validation_status": res.validation_status,
        "persistence_status": res.persistence_status,
        "normalized_counts": dict(res.normalized_counts),
        "accepted_count": res.accepted_count,
        "duplicate_count": res.duplicate_count,
        "conflict_count": res.conflict_count,
        "execution_hash": res.execution_hash,
        "raw_response_retained": False,
        "polling_enabled": poll_health.worker_enabled,
        "worker_running": poll_health.worker_running,
        "poll_interval_seconds": poll_health.poll_interval_seconds,
        "polling_frequency": poll_health.polling_frequency,
        "max_provider_requests_per_24_hours": poll_health.max_provider_requests_per_24_hours,
        "catch_up_bursts_allowed": poll_health.catch_up_bursts_allowed,
        "last_attempt_at": last_attempt_iso,
        "last_success_at": last_success_iso,
        "last_new_match_at": last_new_match_iso,
        "last_status": poll_health.last_status,
        "consecutive_failures": poll_health.consecutive_failures,
        "next_eligible_at": next_eligible_iso,
        "schedule_lag_seconds": poll_health.schedule_lag_seconds,
        "missed_intervals": poll_health.missed_intervals,
        "catch_up_cycles_suppressed": poll_health.catch_up_cycles_suppressed,
        "provider_window_count": poll_health.provider_window_count,
        "elapsed_since_last_success": poll_health.elapsed_since_last_success,
        "possible_history_gap": poll_health.possible_history_gap,
        "history_gap_reason": poll_health.history_gap_reason,
        "review_required": poll_health.review_required,
        "persistence_before_analytics": poll_health.persistence_before_analytics,
        "total_matches_inserted": poll_health.total_matches_inserted,
        "total_duplicates_ignored": poll_health.total_duplicates_ignored,
        "total_replay_conflicts": poll_health.total_replay_conflicts,
        "match_count": len(artifacts.matches),
        "polling": {
            "polling_enabled": poll_health.worker_enabled,
            "worker_running": poll_health.worker_running,
            "poll_interval_seconds": poll_health.poll_interval_seconds,
            "polling_frequency": poll_health.polling_frequency,
            "max_provider_requests_per_24_hours": poll_health.max_provider_requests_per_24_hours,
            "catch_up_bursts_allowed": poll_health.catch_up_bursts_allowed,
            "possible_history_gap": poll_health.possible_history_gap,
            "history_gap_reason": poll_health.history_gap_reason,
            "circuit_state": poll_health.circuit_state,
        },
        "advisory_pipeline": {
            "pipeline_order": [
                "Search",
                "Scout",
                "Analyst",
                "Coach",
                "Tactics",
                "Director",
            ],
            "coach_active": artifacts.coach_report is not None,
            "tactics_active": artifacts.tactics_report is not None,
            "director_active": artifacts.director_report is not None,
            "coach_hypotheses_count": (
                len(artifacts.coach_report.hypotheses)
                if artifacts.coach_report is not None
                else 0
            ),
            "tactics_candidates_count": (
                len(artifacts.tactics_report.candidates)
                if artifacts.tactics_report is not None
                else 0
            ),
            "director_sequenced_count": (
                len(artifacts.director_report.experiment_sequence)
                if artifacts.director_report is not None
                else 0
            ),
            "recommendations_enabled": False,
            "approval_enabled": False,
            "dispatch_enabled": False,
        },
        "recommendations_enabled": False,
        "approval_enabled": False,
        "dispatch_enabled": False,
        "live_probe_executed": LIVE_PROBE_EXECUTED,
    }


def _build_matches_dict(artifacts: FC27DemoSessionArtifacts) -> Dict[str, Any]:
    source_cls = _resolve_source_classification(artifacts.result.source_mode)
    return {
        "source_mode": artifacts.result.source_mode,
        "source_classification": source_cls,
        "synthetic_data_classification": source_cls,
        "club_identity": {
            "club_id": TARGET_CLUB_ID,
            "club_name": TARGET_CLUB_NAME,
            "platform": TARGET_PLATFORM,
            "game_version": TARGET_GAME_VERSION,
        },
        "match_count": len(artifacts.matches),
        "matches": [
            {
                "pseudonymous_match_ref": m.pseudonymous_match_ref,
                "match_hash": m.match_hash,
                "played_timestamp": m.played_timestamp,
                "match_type": m.match_type,
                "formation": m.formation,
                "result": m.result,
                "goals_for": m.goals_for,
                "goals_against": m.goals_against,
                "safe_opponent_label": m.safe_opponent_label,
                "data_quality_status": m.data_quality_status,
                "player_count": m.player_count,
                "pseudonymous_player_refs": list(m.pseudonymous_player_refs),
            }
            for m in artifacts.matches
        ],
        "raw_provider_ids_exposed": False,
        "player_aliases_exposed": False,
        "polling_enabled": False,
        "recommendations_enabled": False,
        "dispatch_enabled": False,
    }


def _build_analysis_dict(artifacts: FC27DemoSessionArtifacts) -> Dict[str, Any]:
    s_ctx = artifacts.search_context
    scout = artifacts.scout_report
    analyst = artifacts.analyst_report
    coach = artifacts.coach_report
    tactics = artifacts.tactics_report
    director = artifacts.director_report

    counter_evidence_items = []
    for f in analyst.findings:
        for ce in f.counter_evidence:
            ce_dump = ce.model_dump(mode="json")
            ce_dump["finding_id"] = f.finding_id
            ce_dump["cluster_id"] = f.cluster_id
            counter_evidence_items.append(ce_dump)

    source_cls = _resolve_source_classification(artifacts.result.source_mode)
    return {
        "source_mode": artifacts.result.source_mode,
        "source_classification": source_cls,
        "synthetic_data_classification": source_cls,
        "formula_version": FORMULA_VERSION,
        "ranking_policy_version": RANKING_POLICY_VERSION,
        "analyst_synthesis_version": ANALYST_SYNTHESIS_VERSION,
        "analyst_policy_version": ANALYST_POLICY_VERSION,
        "coach_version": COACH_VERSION,
        "coach_policy_version": COACH_POLICY_VERSION,
        "tactics_version": TACTICS_VERSION,
        "tactics_policy_version": TACTICS_POLICY_VERSION,
        "director_version": DIRECTOR_VERSION,
        "director_policy_version": DIRECTOR_POLICY_VERSION,
        "confidence": analyst.overall_confidence,
        "search_hash": s_ctx.context_hash,
        "scout_hash": scout.report_hash,
        "analyst_hash": analyst.report_hash,
        "coach_hash": coach.report_hash if coach is not None else None,
        "tactics_hash": tactics.report_hash if tactics is not None else None,
        "director_hash": director.report_hash if director is not None else None,
        "execution_hash": artifacts.result.execution_hash,
        "search_team_summary": s_ctx.baseline_team_aggregates.model_dump(mode="json"),
        "recent_versus_baseline_summary": {
            "eligible_match_count": s_ctx.eligible_match_count,
            "excluded_match_count": s_ctx.excluded_match_count,
            "window": s_ctx.window.model_dump(mode="json"),
            "baseline": s_ctx.baseline_team_aggregates.model_dump(mode="json"),
            "recent": s_ctx.recent_team_aggregates.model_dump(mode="json"),
        },
        "scout_team_and_formation_evidence": {
            "team_evidence": scout.team_evidence.model_dump(mode="json"),
            "formation_evidence": [
                f.model_dump(mode="json") for f in scout.formation_evidence
            ],
            "role_evidence": [r.model_dump(mode="json") for r in scout.role_evidence],
        },
        "analyst_findings": [f.model_dump(mode="json") for f in analyst.findings],
        "coach_hypotheses": (
            [h.model_dump(mode="json") for h in coach.hypotheses]
            if coach is not None
            else []
        ),
        "tactics_candidates": (
            [c.model_dump(mode="json") for c in tactics.candidates]
            if tactics is not None
            else []
        ),
        "director_reconciliation": (
            {
                "review_status": director.review_status,
                "reconciliation_summary": director.reconciliation_summary,
                "experiment_sequence": [
                    s.model_dump(mode="json") for s in director.experiment_sequence
                ],
                "deferred_candidates": [
                    d.model_dump(mode="json") for d in director.deferred_candidates
                ],
                "rejected_candidates": [
                    r.model_dump(mode="json") for r in director.rejected_candidates
                ],
                "measurement_priorities": [
                    m.model_dump(mode="json") for m in director.measurement_priorities
                ],
            }
            if director is not None
            else {
                "review_status": DIRECTOR_REVIEW_STATUS,
                "reconciliation_summary": "",
                "experiment_sequence": [],
                "deferred_candidates": [],
                "rejected_candidates": [],
                "measurement_priorities": [],
            }
        ),
        "counter_evidence_summary": {
            "total_items": len(counter_evidence_items),
            "items": counter_evidence_items,
        },
        "confounders": [c.model_dump(mode="json") for c in analyst.confounders],
        "data_gaps": [g.model_dump(mode="json") for g in analyst.data_gaps],
        "unresolved_questions": [
            q.model_dump(mode="json") for q in analyst.unresolved_questions
        ],
        "limitations": list(analyst.limitations),
        "search": {
            "context_hash": s_ctx.context_hash,
            "eligible_match_count": s_ctx.eligible_match_count,
            "excluded_match_count": s_ctx.excluded_match_count,
        },
        "scout": {
            "report_hash": scout.report_hash,
            "team_evidence": scout.team_evidence.model_dump(mode="json"),
        },
        "analyst": {
            "report_hash": analyst.report_hash,
            "overall_confidence": analyst.overall_confidence,
            "findings": [f.model_dump(mode="json") for f in analyst.findings],
        },
        "coach": (
            {
                "report_hash": coach.report_hash,
                "overall_confidence": coach.overall_confidence,
                "hypotheses": [h.model_dump(mode="json") for h in coach.hypotheses],
            }
            if coach is not None
            else None
        ),
        "tactics": (
            {
                "report_hash": tactics.report_hash,
                "overall_confidence": tactics.overall_confidence,
                "candidates": [c.model_dump(mode="json") for c in tactics.candidates],
            }
            if tactics is not None
            else None
        ),
        "director": (
            {
                "report_hash": director.report_hash,
                "overall_confidence": director.overall_confidence,
                "experiment_sequence": [
                    s.model_dump(mode="json") for s in director.experiment_sequence
                ],
                "deferred_candidates": [
                    d.model_dump(mode="json") for d in director.deferred_candidates
                ],
                "rejected_candidates": [
                    r.model_dump(mode="json") for r in director.rejected_candidates
                ],
            }
            if director is not None
            else None
        ),
        "governance": {
            "recommendations_enabled": False,
            "approval_enabled": False,
            "dispatch_enabled": False,
        },
        "polling_enabled": False,
        "recommendations_enabled": False,
        "approval_enabled": False,
        "dispatch_enabled": False,
    }


def _render_demo_html(
    artifacts: FC27DemoSessionArtifacts,
    store: FC27DemoStateStore,
    polling_runtime: Optional[FC27PollingRuntime] = None,
) -> str:
    """Renders a self-contained local HTML page (`PART L`) with strict HTML escaping."""
    status = _build_status_dict(artifacts, store, polling_runtime=polling_runtime)
    esc = lambda val: html.escape(str(val), quote=True)
    polling_banner_text = (
        "Polling Enabled (Read-Only Status)"
        if status["polling_enabled"]
        else "Polling Disabled (FC27_COMMUNITY_POLLING_ENABLED=false)"
    )

    match_rows = []
    for m in artifacts.matches:
        match_rows.append(
            "<tr>"
            f"<td><code>{esc(m.pseudonymous_match_ref)}</code></td>"
            f"<td>{esc(m.played_timestamp)}</td>"
            f"<td>{esc(m.match_type)}</td>"
            f"<td>{esc(m.formation)}</td>"
            f"<td><strong>{esc(m.result)}</strong></td>"
            f"<td>{esc(m.goals_for)} - {esc(m.goals_against)}</td>"
            f"<td>{esc(m.safe_opponent_label)}</td>"
            f"<td>{esc(m.data_quality_status)}</td>"
            "</tr>"
        )
    matches_html = (
        "\n".join(match_rows)
        if match_rows
        else '<tr><td colspan="8">No eligible matches in current fixture scenario.</td></tr>'
    )

    finding_items = []
    for f in artifacts.analyst_report.findings:
        finding_items.append(
            "<li>"
            f"<strong>[{esc(f.status)} | {esc(f.confidence_tier)}]</strong> "
            f"{esc(f.statement)}<br>"
            f"<small>Category: {esc(f.category)} | "
            f"Sample: {esc(f.eligible_sample_count)} | "
            f"Counter-Evidence Items: {esc(len(f.counter_evidence))}</small>"
            "</li>"
        )
    findings_html = (
        "\n".join(finding_items)
        if finding_items
        else "<li>No findings generated (insufficient sample or empty history).</li>"
    )

    gap_items = []
    for g in artifacts.analyst_report.data_gaps:
        gap_items.append(
            f"<li><strong>[{esc(g.category)}]</strong> {esc(g.description)} "
            f"(Current: {esc(g.current_coverage)})</li>"
        )
    gaps_html = "\n".join(gap_items) if gap_items else "<li>No data gaps recorded.</li>"

    question_items = []
    for q in artifacts.analyst_report.unresolved_questions:
        question_items.append(
            f"<li><strong>[{esc(q.category)}]</strong> {esc(q.question)} "
            f"(Required: {esc(q.required_evidence_to_resolve)})</li>"
        )
    questions_html = (
        "\n".join(question_items)
        if question_items
        else "<li>No unresolved questions recorded.</li>"
    )

    coach_items = []
    if artifacts.coach_report is not None:
        for h in artifacts.coach_report.hypotheses:
            coach_items.append(
                "<li>"
                f"<strong>[{esc(h.artifact_type)} | {esc(h.confidence_tier)}]</strong> "
                f"{esc(h.statement)}<br>"
                f"<small>Falsification: {esc(h.falsification_condition)} | "
                f"Min Sample: {esc(h.minimum_sample_requirement)}</small>"
                "</li>"
            )
    coach_html = (
        "\n".join(coach_items)
        if coach_items
        else "<li>No Coach hypotheses generated.</li>"
    )

    tactics_items = []
    if artifacts.tactics_report is not None:
        for c in artifacts.tactics_report.candidates:
            tactics_items.append(
                "<li>"
                f"<strong>[{esc(c.candidate_type)} | {esc(c.experimental_status)} | {esc(c.confidence_tier)}]</strong> "
                f"{esc(c.expected_observation)}<br>"
                f"<small>Success Metric: {esc(c.success_metric)} | "
                f"Stop Condition: {esc(c.stop_condition)}</small>"
                "</li>"
            )
    tactics_html = (
        "\n".join(tactics_items)
        if tactics_items
        else "<li>No experimental Tactics candidates generated.</li>"
    )

    director_seq_items = []
    if artifacts.director_report is not None:
        for s in artifacts.director_report.experiment_sequence:
            director_seq_items.append(
                "<li>"
                f"<strong>[Slot {esc(s.sequence_order)} | {esc(s.category)} | {esc(s.confidence_tier)}]</strong> "
                f"{esc(s.rationale)} (Sample Budget: {esc(s.sample_budget_matches)} matches)"
                "</li>"
            )
    director_seq_html = (
        "\n".join(director_seq_items)
        if director_seq_items
        else "<li>No active sequenced experiments.</li>"
    )
    director_summary_text = (
        artifacts.director_report.reconciliation_summary
        if artifacts.director_report is not None
        else "Reconciliation not available."
    )
    coach_hash_str = (
        artifacts.coach_report.report_hash if artifacts.coach_report is not None else "NONE"
    )
    tactics_hash_str = (
        artifacts.tactics_report.report_hash if artifacts.tactics_report is not None else "NONE"
    )
    director_hash_str = (
        artifacts.director_report.report_hash if artifacts.director_report is not None else "NONE"
    )

    banner_label = resolve_dashboard_source_label(
        artifacts.result.source_mode,
        include_mode_suffix=True,
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>East Coasters FC 27 Local Loopback Ingestion Demo</title>
  <style>
    body {{ font-family: system-ui, -apple-system, sans-serif; margin: 1.5rem; background: #0f172a; color: #e2e8f0; }}
    .banner-bar {{ display: flex; gap: 0.75rem; flex-wrap: wrap; margin-bottom: 1rem; }}
    .banner {{ padding: 0.5rem 0.9rem; border-radius: 4px; font-weight: 700; font-size: 0.9rem; }}
    .banner-fixture {{ background: #1e3a8a; border: 1px solid #60a5fa; color: #dbeafe; }}
    .banner-rec {{ background: #7c2d12; border: 1px solid #fb923c; color: #ffedd5; }}
    .banner-mode {{ background: #064e3b; border: 1px solid #34d399; color: #d1fae5; }}
    .card {{ background: #1e293b; border: 1px solid #334155; border-radius: 6px; padding: 1rem; margin-bottom: 1rem; }}
    h1, h2 {{ margin-top: 0; color: #f8fafc; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 0.5rem; font-size: 0.9rem; }}
    th, td {{ border: 1px solid #334155; padding: 0.45rem 0.6rem; text-align: left; }}
    th {{ background: #0f172a; }}
    button, select {{ background: #2563eb; color: #ffffff; border: 1px solid #60a5fa; border-radius: 4px; padding: 0.4rem 0.8rem; cursor: pointer; font-weight: 600; }}
    select {{ background: #0f172a; }}
    code {{ color: #93c5fd; font-size: 0.85rem; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 0.75rem; }}
  </style>
</head>
<body>
  <div class="banner-bar">
    <div class="banner banner-fixture" id="banner-synthetic-fixture">{esc(banner_label)}</div>
    <div class="banner banner-rec" id="banner-polling-12h">Polling Every 12 Hours (43200s | Max 2 Requests/24h | Catch-Up Bursts Disabled)</div>
    <div class="banner banner-rec" id="banner-recommendations-disabled">Recommendations Disabled | Approval Disabled | Dispatch Disabled (RECOMMENDATIONS_DISABLED | POLLING_DISABLED | DISPATCH_DISABLED)</div>
    <div class="banner banner-rec" id="banner-polling-status">{esc(polling_banner_text)}</div>
    <div class="banner banner-mode" id="banner-source-mode">Mode: {esc(artifacts.result.source_mode)} (East Coasters 330323 / common-gen5 / FC27)</div>
  </div>

  <h1>East Coasters FC 27 Local Ingestion &amp; Analytics Demo</h1>

  <div class="card">
    <h2>Controls (Loopback Fixture Only)</h2>
    <div style="display:flex; gap:0.75rem; align-items:center; flex-wrap:wrap;">
      <button type="button" id="btn-refresh" onclick="window.location.reload()">Refresh View</button>
      <label for="scenario-select">Fixture Scenario:</label>
      <select id="scenario-select">
        <option value="valid">valid</option>
        <option value="empty_history">empty_history</option>
        <option value="duplicates">duplicates</option>
      </select>
      <button type="button" id="btn-ingest-fixture" onclick="triggerFixtureIngest()">Run Fixture Ingest</button>
      <span id="ingest-feedback"></span>
    </div>
  </div>

  <div class="card" id="card-provider-health">
    <h2>Provider Health &amp; Deterministic Hashes</h2>
    <div class="grid">
      <div><strong>Scenario:</strong> {esc(status['scenario'])}</div>
      <div><strong>Validation:</strong> {esc(status['validation_status'])}</div>
      <div><strong>Confidence:</strong> {esc(artifacts.analyst_report.overall_confidence)}</div>
      <div><strong>Circuit State:</strong> {esc(status['circuit_state'])}</div>
      <div><strong>Provider Health:</strong> {esc(status['provider_health'])}</div>
      <div><strong>Last Ingestion:</strong> {esc(status['last_ingestion_time'])}</div>
      <div><strong>Requests / Budget:</strong> {esc(status['request_count'])} ({esc(status['request_budget_status'])})</div>
      <div><strong>Cache (Hits/Misses):</strong> {esc(status['cache_status']['hits'])} / {esc(status['cache_status']['misses'])}</div>
      <div><strong>Persistence:</strong> {esc(status['persistence_status'])}</div>
      <div><strong>Accepted / Dup / Conflict:</strong> {esc(status['accepted_count'])} / {esc(status['duplicate_count'])} / {esc(status['conflict_count'])}</div>
    </div>
    <p style="margin-bottom:0;">
      <strong>Search Hash:</strong> <code>{esc(artifacts.search_context.context_hash)}</code><br>
      <strong>Scout Hash:</strong> <code>{esc(artifacts.scout_report.report_hash)}</code><br>
      <strong>Analyst Hash:</strong> <code>{esc(artifacts.analyst_report.report_hash)}</code><br>
      <strong>Coach Hash:</strong> <code>{esc(coach_hash_str)}</code><br>
      <strong>Tactics Hash:</strong> <code>{esc(tactics_hash_str)}</code><br>
      <strong>Director Hash:</strong> <code>{esc(director_hash_str)}</code><br>
      <strong>Execution Hash:</strong> <code>{esc(status['execution_hash'])}</code>
    </p>
  </div>

  <div class="card" id="card-polling-health">
    <h2>Polling Worker Health (Read-Only) — 12-Hour Schedule &amp; Possible History Gap</h2>
    <div class="grid">
      <div><strong>Polling Enabled:</strong> {esc(status['polling_enabled'])}</div>
      <div><strong>Worker Running:</strong> {esc(status['worker_running'])}</div>
      <div><strong>Poll Interval:</strong> {esc(status['poll_interval_seconds'])}s ({esc(status['polling_frequency'])})</div>
      <div><strong>Max Requests / 24h:</strong> {esc(status['max_provider_requests_per_24_hours'])}</div>
      <div><strong>Last Poll Status:</strong> {esc(status['last_status'])}</div>
      <div><strong>Consecutive Failures:</strong> {esc(status['consecutive_failures'])}</div>
      <div><strong>Last Attempt At:</strong> {esc(status['last_attempt_at'])}</div>
      <div><strong>Last Success At:</strong> {esc(status['last_success_at'])}</div>
      <div><strong>Last New Match At:</strong> {esc(status['last_new_match_at'])}</div>
      <div><strong>Next Eligible At:</strong> {esc(status['next_eligible_at'])}</div>
      <div><strong>Possible History Gap:</strong> {esc(status['possible_history_gap'])} ({esc(status['history_gap_reason'])})</div>
      <div><strong>Matches Inserted:</strong> {esc(status['total_matches_inserted'])}</div>
      <div><strong>Duplicates Ignored:</strong> {esc(status['total_duplicates_ignored'])}</div>
      <div><strong>Replay Conflicts:</strong> {esc(status['total_replay_conflicts'])}</div>
    </div>
  </div>

  <div class="card" id="card-latest-matches">
    <h2>Latest Matches (Pseudonymous References Only)</h2>
    <table>
      <thead>
        <tr>
          <th>Match Ref</th>
          <th>Played Timestamp</th>
          <th>Match Type</th>
          <th>Formation</th>
          <th>Result</th>
          <th>Score (GF-GA)</th>
          <th>Opponent</th>
          <th>Quality Status</th>
        </tr>
      </thead>
      <tbody>
        {matches_html}
      </tbody>
    </table>
  </div>

  <div class="card" id="card-search-scout">
    <h2>Search &amp; Scout Evidence Summary</h2>
    <div class="grid">
      <div><strong>Eligible Matches:</strong> {esc(artifacts.search_context.eligible_match_count)}</div>
      <div><strong>Excluded Matches:</strong> {esc(artifacts.search_context.excluded_match_count)}</div>
      <div><strong>Wins / Draws / Losses:</strong> {esc(artifacts.scout_report.team_evidence.wins)} / {esc(artifacts.scout_report.team_evidence.draws)} / {esc(artifacts.scout_report.team_evidence.losses)}</div>
      <div><strong>Goals For / Against (GD):</strong> {esc(artifacts.scout_report.team_evidence.goals_for)} / {esc(artifacts.scout_report.team_evidence.goals_against)} ({esc(artifacts.scout_report.team_evidence.goal_differential)})</div>
    </div>
  </div>

  <div class="card" id="card-analyst">
    <h2>Analyst Observational Findings ({esc(len(artifacts.analyst_report.findings))})</h2>
    <ul>
      {findings_html}
    </ul>
  </div>

  <div class="card" id="card-coach">
    <h2>Coach Hypotheses &amp; Practice Experiments</h2>
    <ul>
      {coach_html}
    </ul>
  </div>

  <div class="card" id="card-tactics">
    <h2>Tactics Experimental Candidates ({esc(EXPERIMENTAL_ONLY_STATUS)})</h2>
    <ul>
      {tactics_html}
    </ul>
  </div>

  <div class="card" id="card-director">
    <h2>Director Evidence Reconciliation ({esc(DIRECTOR_REVIEW_STATUS)})</h2>
    <p>{esc(director_summary_text)}</p>
    <ul>
      {director_seq_html}
    </ul>
  </div>

  <div class="card" id="card-data-gaps">
    <h2>Data Gaps &amp; Unmeasured Context ({esc(len(artifacts.analyst_report.data_gaps))})</h2>
    <ul>
      {gaps_html}
    </ul>
  </div>

  <div class="card" id="card-unresolved-questions">
    <h2>Unresolved Questions ({esc(len(artifacts.analyst_report.unresolved_questions))})</h2>
    <ul>
      {questions_html}
    </ul>
  </div>

  <script>
    async function triggerFixtureIngest() {{
      var sel = document.getElementById("scenario-select");
      var fb = document.getElementById("ingest-feedback");
      fb.textContent = "Ingesting fixture...";
      try {{
        var resp = await fetch("/demo/ingest-fixture", {{
          method: "POST",
          headers: {{"Content-Type": "application/json"}},
          body: JSON.stringify({{"scenario": sel.value}})
        }});
        if (resp.ok) {{
          window.location.reload();
        }} else {{
          fb.textContent = "Fixture ingest rejected (HTTP " + resp.status + ")";
        }}
      }} catch (err) {{
        fb.textContent = "Loopback request failed.";
      }}
    }}
  </script>
</body>
</html>
"""


def build_demo_router(
    *,
    state_store: Optional[FC27DemoStateStore] = None,
    demo_mode_override: Optional[bool] = None,
    fixture_port: Optional[int] = None,
    polling_runtime: Optional[FC27PollingRuntime] = None,
) -> APIRouter:
    """Builds the local read-only FC 27 demo APIRouter (`PART K` & `PART L`)."""
    router = APIRouter()
    store = state_store if state_store is not None else DEFAULT_DEMO_STATE_STORE

    @router.get("/demo/status")
    def get_demo_status(request: Request) -> JSONResponse:
        _require_demo_mode_enabled(demo_mode_override)
        _reject_prohibited_query_params(request)
        artifacts = _ensure_artifacts(
            store,
            demo_mode_override=demo_mode_override,
            fixture_port=fixture_port,
        )
        return JSONResponse(
            content=_build_status_dict(artifacts, store, polling_runtime=polling_runtime)
        )

    @router.get("/demo/matches")
    def get_demo_matches(request: Request) -> JSONResponse:
        _require_demo_mode_enabled(demo_mode_override)
        _reject_prohibited_query_params(request)
        artifacts = _ensure_artifacts(
            store,
            demo_mode_override=demo_mode_override,
            fixture_port=fixture_port,
        )
        return JSONResponse(content=_build_matches_dict(artifacts))

    @router.get("/demo/analysis")
    def get_demo_analysis(request: Request) -> JSONResponse:
        _require_demo_mode_enabled(demo_mode_override)
        _reject_prohibited_query_params(request)
        artifacts = _ensure_artifacts(
            store,
            demo_mode_override=demo_mode_override,
            fixture_port=fixture_port,
        )
        return JSONResponse(content=_build_analysis_dict(artifacts))

    @router.get("/api/fc27/demo/coach")
    def get_demo_coach(request: Request) -> JSONResponse:
        _require_demo_mode_enabled(demo_mode_override)
        _reject_prohibited_query_params(request)
        artifacts = _ensure_artifacts(
            store,
            demo_mode_override=demo_mode_override,
            fixture_port=fixture_port,
        )
        analysis = _build_analysis_dict(artifacts)
        return JSONResponse(
            content={
                "club_id": TARGET_CLUB_ID,
                "club_name": TARGET_CLUB_NAME,
                "platform": TARGET_PLATFORM,
                "game_version": TARGET_GAME_VERSION,
                "coach_version": analysis["coach_version"],
                "coach_policy_version": analysis["coach_policy_version"],
                "coach_hash": analysis["coach_hash"],
                "coach": analysis["coach"],
                "coach_hypotheses": analysis["coach_hypotheses"],
                "recommendations_enabled": False,
                "approval_enabled": False,
                "dispatch_enabled": False,
            }
        )

    @router.get("/api/fc27/demo/tactics")
    def get_demo_tactics(request: Request) -> JSONResponse:
        _require_demo_mode_enabled(demo_mode_override)
        _reject_prohibited_query_params(request)
        artifacts = _ensure_artifacts(
            store,
            demo_mode_override=demo_mode_override,
            fixture_port=fixture_port,
        )
        analysis = _build_analysis_dict(artifacts)
        return JSONResponse(
            content={
                "club_id": TARGET_CLUB_ID,
                "club_name": TARGET_CLUB_NAME,
                "platform": TARGET_PLATFORM,
                "game_version": TARGET_GAME_VERSION,
                "tactics_version": analysis["tactics_version"],
                "tactics_policy_version": analysis["tactics_policy_version"],
                "tactics_hash": analysis["tactics_hash"],
                "tactics": analysis["tactics"],
                "tactics_candidates": analysis["tactics_candidates"],
                "recommendations_enabled": False,
                "approval_enabled": False,
                "dispatch_enabled": False,
            }
        )

    @router.get("/api/fc27/demo/director")
    def get_demo_director(request: Request) -> JSONResponse:
        _require_demo_mode_enabled(demo_mode_override)
        _reject_prohibited_query_params(request)
        artifacts = _ensure_artifacts(
            store,
            demo_mode_override=demo_mode_override,
            fixture_port=fixture_port,
        )
        analysis = _build_analysis_dict(artifacts)
        return JSONResponse(
            content={
                "club_id": TARGET_CLUB_ID,
                "club_name": TARGET_CLUB_NAME,
                "platform": TARGET_PLATFORM,
                "game_version": TARGET_GAME_VERSION,
                "director_version": analysis["director_version"],
                "director_policy_version": analysis["director_policy_version"],
                "director_hash": analysis["director_hash"],
                "director": analysis["director"],
                "director_reconciliation": analysis["director_reconciliation"],
                "recommendations_enabled": False,
                "approval_enabled": False,
                "dispatch_enabled": False,
            }
        )

    @router.post("/demo/ingest-fixture")
    def post_demo_ingest_fixture(
        request: Request,
        body: Optional[FC27DemoIngestFixtureRequest] = None,
    ) -> JSONResponse:
        _require_demo_mode_enabled(demo_mode_override)
        _reject_prohibited_query_params(request)
        scenario = body.scenario if body is not None else "valid"
        if scenario not in UI_SAFE_FIXTURE_SCENARIOS:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "INVALID_DEMO_SCENARIO",
                    "message": f"Only UI-safe fixture scenarios are allowed: {UI_SAFE_FIXTURE_SCENARIOS}",
                },
            )
        artifacts = run_fc27_demo_ingestion_with_artifacts(
            source="fixture",
            operation="full-demo",
            scenario=scenario,
            fixture_port=fixture_port,
            demo_mode_enabled=True,
            analysis_generated_at=DEMO_DEFAULT_GENERATED_AT,
            cache=store.cache,
            rate_limiter=store.limiter,
            circuit_breaker=store.breaker,
            state_store=store,
        )
        return JSONResponse(
            content=_build_status_dict(artifacts, store, polling_runtime=polling_runtime)
        )

    @router.get("/demo", response_class=HTMLResponse)
    def get_demo_html_page(request: Request) -> HTMLResponse:
        _require_demo_mode_enabled(demo_mode_override)
        _reject_prohibited_query_params(request)
        artifacts = _ensure_artifacts(
            store,
            demo_mode_override=demo_mode_override,
            fixture_port=fixture_port,
        )
        return HTMLResponse(
            content=_render_demo_html(artifacts, store, polling_runtime=polling_runtime)
        )

    return router


def create_demo_app(
    *,
    bind_host: str = LOOPBACK_IPV4,
    state_store: Optional[FC27DemoStateStore] = None,
    demo_mode_override: Optional[bool] = None,
    fixture_port: Optional[int] = None,
    polling_runtime: Optional[FC27PollingRuntime] = None,
) -> FastAPI:
    """Creates a standalone local-only FastAPI application for the FC 27 demo (`PART C` & `PART K`).

    Enforces `127.0.0.1` loopback bind validation on creation.
    """
    validate_demo_host_strict(bind_host)
    app = FastAPI(
        title="East Coasters FC 27 Local Loopback Ingestion Demo API",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.bind_host = LOOPBACK_IPV4
    app.include_router(
        build_demo_router(
            state_store=state_store,
            demo_mode_override=demo_mode_override,
            fixture_port=fixture_port,
            polling_runtime=polling_runtime,
        )
    )
    return app


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the East Coasters FC 27 Local Loopback Demo API on 127.0.0.1.",
    )
    parser.add_argument(
        "--host",
        default=LOOPBACK_IPV4,
        help="Loopback bind host (must be 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8027,
        help="Loopback TCP port (default: 8027).",
    )
    args = parser.parse_args(argv)
    validate_demo_host_strict(args.host)

    import uvicorn

    app = create_demo_app(bind_host=args.host, demo_mode_override=True)
    uvicorn.run(app, host=LOOPBACK_IPV4, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
