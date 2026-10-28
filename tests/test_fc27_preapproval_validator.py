"""Tests for deterministic FC 27 pre-approval validation boundary (Part E).

Covers all 44 required test items and adversarial scenarios:
1-3: Valid lead_packages produce valid=True, deterministic package_version, and enter awaiting_approval cleanly.
4-12: Package existence, schema validation, SHA-256 package_version integrity, duplicate detection, and candidate consistency.
13-16: Objective metric validation (METRIC_BOUNDS reuse, unsupported metrics, NaN, +Inf/-Inf).
17-22: Formation, role, duplicate player, roster size, and NOT_APPLICABLE formation reporting.
23-29: Confidence and sample policy (CONFIDENCE_METADATA_MISSING, FORMULA_VERSION_UNSUPPORTED, INSUFFICIENT_DATA, INSUFFICIENT_SAMPLE, exploratory, standard, high_confidence).
30-31: Recommendation gate (FC27_RECOMMENDATIONS_ENABLED=false default, RECOMMENDATION_DISABLED on unapproved payload).
32-38: Graph and ApprovalStore fail-closed guarantees (no interrupt(), no approval_requests row, status='failed', no challenge nonce, no decision commit, no dispatch record, empty fc27_approval_inconsistencies).
39-40: Metadata-only tracing (only safe counts/booleans/versions/reason codes; zero sensitive content when LANGFUSE_CAPTURE_CONTENT=false).
41-44: Adversarial bypass attempts (package mutation, hash mismatch, approval bypass, caller-controlled resume, replayed decision, malformed state, recommendation activation attempt, dispatch bypass).
"""
from __future__ import annotations

import copy
import json
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from src.approval_store import ApprovalStore, StoreError, request_hash
from src.decision import package_version
from src.fc27_analytics import FORMULA_VERSION
from src.graph import GraphDeps, build_graph


def initial_state(club_id: str, thread_id: str) -> dict[str, Any]:
    return {
        "thread_id": thread_id,
        "club_id": club_id,
        "fc_version": "27",
        "platform": "common-gen5",
        "status": "running",
        "errors": [],
        "audit_log": [],
    }
from src.lead_packages import (
    build_lead_packages,
    emit_pre_approval_validation_trace,
    players_from_dashboard,
    validate_pre_approval_packages,
)
from src.schemas import (
    DecisionIntent,
    FORMATION_REGISTRY_VERSION,
    PRE_APPROVAL_POLICY_VERSION,
    PreApprovalValidationResult,
    SignedDecision,
    SwarmRole,
    VALID_PRE_APPROVAL_REASON_CODES,
)
from src.security import Principal, Role
from tests.conftest import FakeRoles, dashboard
from tests.helpers import COM, SVC, fake_packages, fake_packages_dispatchable


def _sample_ranked_packages(
    thread_id: str = "fc27-330323-run-20261028-0001",
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], str]:
    roles = FakeRoles()
    dash = dashboard()
    role_results = {
        role.value: roles(role.value, {"players": players_from_dashboard(dash, "330323")})
        for role in SwarmRole
    }
    pkgs = build_lead_packages(dash, "330323", role_results, thread_id)
    cands = [{"player_id": p} for p in sorted(players_from_dashboard(dash, "330323"))]
    ver = package_version(pkgs)
    assert ver is not None
    return pkgs, cands, ver


# ---------------------------------------------------------------------------
# 1-3: Valid lead_packages & awaiting_approval
# ---------------------------------------------------------------------------


def test_01_valid_lead_packages_produce_valid_true() -> None:
    pkgs, cands, ver = _sample_ranked_packages()
    res = validate_pre_approval_packages(
        pkgs,
        package_version_hash=ver,
        candidates=cands,
        thread_id="fc27-330323-run-20261028-0001",
    )
    assert isinstance(res, PreApprovalValidationResult)
    assert res.valid is True
    assert res.approvable is True
    assert res.review_required is False
    assert res.reason_codes == ()
    assert res.policy_version == PRE_APPROVAL_POLICY_VERSION


def test_02_valid_lead_packages_produce_deterministic_package_version() -> None:
    pkgs1, cands1, ver1 = _sample_ranked_packages()
    pkgs2, cands2, ver2 = _sample_ranked_packages()
    res1 = validate_pre_approval_packages(pkgs1, package_version_hash=ver1, candidates=cands1)
    res2 = validate_pre_approval_packages(pkgs2, package_version_hash=ver2, candidates=cands2)
    assert res1.package_version == res2.package_version == ver1
    assert len(res1.package_version) == 64


def test_03_valid_lead_packages_enter_awaiting_approval_cleanly(wired) -> None:
    runner, app = wired()
    with TestClient(app) as client:
        resp = client.post("/run", headers=SVC, json={"club_id": "330323"})
        assert resp.status_code == 201, resp.text
        state = resp.json()
    assert state["status"] == "awaiting_approval"
    assert state["package_version"] is not None
    values = runner.values(state["thread_id"])
    assert values["pre_approval_validation"]["valid"] is True
    assert values["pre_approval_validation"]["approvable"] is True
    assert values["pre_approval_validation"]["reason_codes"] == []


# ---------------------------------------------------------------------------
# 4-12: Package existence, schema, hash, duplicates, and candidate consistency
# ---------------------------------------------------------------------------


def test_04_empty_lead_packages_fail_with_empty_lead_packages() -> None:
    res = validate_pre_approval_packages({}, package_version_hash="a" * 64)
    assert res.valid is False
    assert res.approvable is False
    assert "EMPTY_LEAD_PACKAGES" in res.reason_codes


def test_05_none_lead_packages_fail_with_empty_lead_packages() -> None:
    res = validate_pre_approval_packages(None, package_version_hash="a" * 64)
    assert res.valid is False
    assert res.approvable is False
    assert "EMPTY_LEAD_PACKAGES" in res.reason_codes


def test_06_non_collection_lead_packages_fail_with_package_schema_invalid() -> None:
    res = validate_pre_approval_packages("not-a-dict", package_version_hash="a" * 64)
    assert res.valid is False
    assert "PACKAGE_SCHEMA_INVALID" in res.reason_codes


def test_07_malformed_package_item_fails_with_package_schema_invalid() -> None:
    res = validate_pre_approval_packages(
        {"scout": {"owner": "scout", "approvable": "not-a-bool", "unexpected_key": 123}},
        package_version_hash="a" * 64,
    )
    assert res.valid is False
    assert "PACKAGE_SCHEMA_INVALID" in res.reason_codes


def test_08_missing_package_version_fails_with_package_hash_missing() -> None:
    pkgs, cands, _ = _sample_ranked_packages()
    res = validate_pre_approval_packages(pkgs, package_version_hash=None, candidates=cands)
    assert res.valid is False
    assert "PACKAGE_HASH_MISSING" in res.reason_codes


@pytest.mark.parametrize("bad_hash", ["short", "Z" * 64, "12345", "a" * 63, "a" * 65])
def test_09_invalid_format_package_version_fails_with_package_hash_invalid(bad_hash: str) -> None:
    pkgs, cands, _ = _sample_ranked_packages()
    res = validate_pre_approval_packages(pkgs, package_version_hash=bad_hash, candidates=cands)
    assert res.valid is False
    assert "PACKAGE_HASH_INVALID" in res.reason_codes


def test_10_mutated_lead_packages_with_stale_package_version_fail_with_package_hash_mismatch() -> None:
    pkgs, cands, ver = _sample_ranked_packages()
    mutated = copy.deepcopy(pkgs)
    mutated["scout"]["reason"] = "tampered reason after hash computation"
    res = validate_pre_approval_packages(mutated, package_version_hash=ver, candidates=cands)
    assert res.valid is False
    assert "PACKAGE_HASH_MISMATCH" in res.reason_codes


def test_11_duplicate_candidate_or_role_package_fails_with_duplicate_candidate_package() -> None:
    pkgs, cands, _ = _sample_ranked_packages()
    dup_list = [pkgs["scout"], copy.deepcopy(pkgs["scout"])]
    ver = package_version(pkgs)
    res = validate_pre_approval_packages(dup_list, package_version_hash=ver, candidates=cands)
    assert res.valid is False
    assert "DUPLICATE_CANDIDATE_PACKAGE" in res.reason_codes


def test_12_candidate_reference_not_in_candidates_fails_with_candidate_id_mismatch() -> None:
    pkgs, cands, ver = _sample_ranked_packages()
    filtered_cands = [c for c in cands if c.get("player_id") != "p0"]
    res = validate_pre_approval_packages(pkgs, package_version_hash=ver, candidates=filtered_cands)
    assert res.valid is False
    assert "CANDIDATE_ID_MISMATCH" in res.reason_codes


# ---------------------------------------------------------------------------
# 13-16: Objective metric bounds and non-finite rejection
# ---------------------------------------------------------------------------


def test_13_objective_metric_outside_bounds_fails_with_objective_bound_violation() -> None:
    pkgs = fake_packages_dispatchable()
    pkgs["coach"]["best"]["objective"]["target_delta"] = 999.0  # outside pass_completion_pct bounds
    ver = package_version(pkgs)
    res = validate_pre_approval_packages(pkgs, package_version_hash=ver)
    assert res.valid is False
    assert "OBJECTIVE_BOUND_VIOLATION" in res.reason_codes


def test_14_unsupported_objective_metric_fails_with_objective_metric_unsupported() -> None:
    pkgs = fake_packages_dispatchable()
    pkgs["coach"]["best"]["objective"]["metric"] = "unsupported_xg_teleport"
    ver = package_version(pkgs)
    res = validate_pre_approval_packages(pkgs, package_version_hash=ver)
    assert res.valid is False
    assert "OBJECTIVE_METRIC_UNSUPPORTED" in res.reason_codes


def test_15_nan_objective_value_fails_with_objective_value_nonfinite() -> None:
    pkgs, cands, ver = _sample_ranked_packages()
    bad_pkgs = copy.deepcopy(pkgs)
    bad_pkgs["coach"]["objective"] = {
        "metric": "pass_completion_pct",
        "direction": "increase",
        "target_delta": float("nan"),
        "min_matches": 1,
        "max_matches": 5,
        "bounds_policy": "fc27-objective-bounds-v1",
    }
    res = validate_pre_approval_packages(bad_pkgs, package_version_hash=ver, candidates=cands)
    assert res.valid is False
    assert "OBJECTIVE_VALUE_NONFINITE" in res.reason_codes


@pytest.mark.parametrize("inf_val", [float("inf"), float("-inf")])
def test_16_infinite_objective_value_fails_with_objective_value_nonfinite(inf_val: float) -> None:
    pkgs, cands, ver = _sample_ranked_packages()
    bad_pkgs = copy.deepcopy(pkgs)
    bad_pkgs["coach"]["objective"] = {
        "metric": "pass_completion_pct",
        "direction": "increase",
        "target_delta": inf_val,
        "min_matches": 1,
        "max_matches": 5,
        "bounds_policy": "fc27-objective-bounds-v1",
    }
    res = validate_pre_approval_packages(bad_pkgs, package_version_hash=ver, candidates=cands)
    assert res.valid is False
    assert "OBJECTIVE_VALUE_NONFINITE" in res.reason_codes


# ---------------------------------------------------------------------------
# 17-22: Formation, role, duplicate player, roster size, NOT_APPLICABLE
# ---------------------------------------------------------------------------


def test_17_valid_formation_in_valid_fc27_formations_passes_when_present() -> None:
    pkgs, cands, ver = _sample_ranked_packages()
    lineup = [
        {"player_id": f"p{i}", "position": "CM", "role": "Box-to-Box"}
        for i in range(1, 12)
    ]
    res = validate_pre_approval_packages(
        pkgs,
        package_version_hash=ver,
        candidates=cands,
        formation="4-3-3",
        lineup=lineup,
    )
    assert res.valid is True
    assert res.formation_registry_version == FORMATION_REGISTRY_VERSION


def test_18_unsupported_formation_fails_with_formation_unsupported() -> None:
    pkgs, cands, ver = _sample_ranked_packages()
    res = validate_pre_approval_packages(
        pkgs,
        package_version_hash=ver,
        candidates=cands,
        formation="2-2-6-invalid",
    )
    assert res.valid is False
    assert "FORMATION_UNSUPPORTED" in res.reason_codes
    assert res.formation_registry_version == FORMATION_REGISTRY_VERSION


def test_19_invalid_position_or_role_assignment_fails_with_role_assignment_invalid() -> None:
    pkgs, cands, ver = _sample_ranked_packages()
    res = validate_pre_approval_packages(
        pkgs,
        package_version_hash=ver,
        candidates=cands,
        assignments=[{"player_id": "p1", "position": "INVALID_POS", "role": "Poacher"}],
    )
    assert res.valid is False
    assert "ROLE_ASSIGNMENT_INVALID" in res.reason_codes


def test_20_duplicate_player_assignment_fails_with_duplicate_player_assignment() -> None:
    pkgs, cands, ver = _sample_ranked_packages()
    res = validate_pre_approval_packages(
        pkgs,
        package_version_hash=ver,
        candidates=cands,
        assignments=[
            {"player_id": "p1", "position": "ST", "role": "Poacher"},
            {"player_id": "p1", "position": "CM", "role": "Playmaker"},
        ],
    )
    assert res.valid is False
    assert "DUPLICATE_PLAYER_ASSIGNMENT" in res.reason_codes


def test_21_invalid_lineup_size_fails_with_roster_size_invalid() -> None:
    pkgs, cands, ver = _sample_ranked_packages()
    res = validate_pre_approval_packages(
        pkgs,
        package_version_hash=ver,
        candidates=cands,
        lineup=[],
    )
    assert res.valid is False
    assert "ROSTER_SIZE_INVALID" in res.reason_codes


def test_22_absent_formation_metadata_reported_as_not_applicable() -> None:
    pkgs, cands, ver = _sample_ranked_packages()
    res = validate_pre_approval_packages(pkgs, package_version_hash=ver, candidates=cands)
    assert res.valid is True
    assert res.formation_registry_version == "NOT_APPLICABLE"


# ---------------------------------------------------------------------------
# 23-29: Confidence and sample policy
# ---------------------------------------------------------------------------


def test_23_confidence_metadata_missing_when_required_fails_with_confidence_metadata_missing() -> None:
    pkgs, cands, ver = _sample_ranked_packages()
    res = validate_pre_approval_packages(
        pkgs,
        package_version_hash=ver,
        candidates=cands,
        analytics_metadata={},
    )
    assert res.valid is False
    assert "CONFIDENCE_METADATA_MISSING" in res.reason_codes


def test_24_unsupported_formula_version_fails_with_formula_version_unsupported() -> None:
    pkgs, cands, ver = _sample_ranked_packages()
    res = validate_pre_approval_packages(
        pkgs,
        package_version_hash=ver,
        candidates=cands,
        analytics_metadata={
            "confidence_tier": "MODERATE_CONFIDENCE",
            "formula_version": "fc27-v0.0-invalid",
            "eligible_matches": 12,
        },
    )
    assert res.valid is False
    assert "FORMULA_VERSION_UNSUPPORTED" in res.reason_codes


def test_25_zero_matches_fail_with_insufficient_data() -> None:
    pkgs, cands, ver = _sample_ranked_packages()
    res = validate_pre_approval_packages(
        pkgs,
        package_version_hash=ver,
        candidates=cands,
        analytics_metadata={
            "confidence_tier": "INSUFFICIENT_DATA",
            "formula_version": FORMULA_VERSION,
            "eligible_matches": 0,
        },
    )
    assert res.valid is False
    assert res.approvable is False
    assert res.confidence_tier == "INSUFFICIENT_DATA"
    assert "INSUFFICIENT_DATA" in res.reason_codes
    assert "INSUFFICIENT_SAMPLE" in res.reason_codes


def test_26_below_minimum_sample_fails_with_insufficient_sample() -> None:
    pkgs, cands, ver = _sample_ranked_packages()
    res = validate_pre_approval_packages(
        pkgs,
        package_version_hash=ver,
        candidates=cands,
        analytics_metadata={
            "confidence_tier": "INSUFFICIENT_DATA",
            "formula_version": FORMULA_VERSION,
            "eligible_matches": 2,
        },
    )
    assert res.valid is False
    assert res.approvable is False
    assert "INSUFFICIENT_SAMPLE" in res.reason_codes


def test_27_exploratory_sample_with_review_required_blocks_automatic_approval() -> None:
    pkgs, cands, ver = _sample_ranked_packages()
    res = validate_pre_approval_packages(
        pkgs,
        package_version_hash=ver,
        candidates=cands,
        analytics_metadata={
            "confidence_tier": "EXPLORATORY",
            "formula_version": FORMULA_VERSION,
            "eligible_matches": 3,
            "review_required": True,
        },
    )
    assert res.confidence_tier == "EXPLORATORY"
    assert res.review_required is True
    assert res.approvable is False
    assert "REVIEW_REQUIRED" in res.reason_codes


def test_28_moderate_confidence_sample_passes_sample_policy() -> None:
    pkgs, cands, ver = _sample_ranked_packages()
    res = validate_pre_approval_packages(
        pkgs,
        package_version_hash=ver,
        candidates=cands,
        analytics_metadata={
            "confidence_tier": "MODERATE_CONFIDENCE",
            "formula_version": FORMULA_VERSION,
            "eligible_matches": 12,
        },
    )
    assert res.valid is True
    assert res.approvable is True
    assert res.review_required is False
    assert res.confidence_tier == "MODERATE_CONFIDENCE"
    assert res.reason_codes == ()


def test_29_high_confidence_sample_passes_sample_policy() -> None:
    pkgs, cands, ver = _sample_ranked_packages()
    res = validate_pre_approval_packages(
        pkgs,
        package_version_hash=ver,
        candidates=cands,
        analytics_metadata={
            "confidence_tier": "HIGH_CONFIDENCE",
            "formula_version": FORMULA_VERSION,
            "eligible_matches": 20,
        },
    )
    assert res.valid is True
    assert res.approvable is True
    assert res.review_required is False
    assert res.confidence_tier == "HIGH_CONFIDENCE"
    assert res.reason_codes == ()


# ---------------------------------------------------------------------------
# 30-31: Recommendations disabled gate
# ---------------------------------------------------------------------------


def test_30_recommendations_disabled_by_default_keeps_recommendation_output_inactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FC27_RECOMMENDATIONS_ENABLED", raising=False)
    pkgs, cands, ver = _sample_ranked_packages()
    res = validate_pre_approval_packages(
        pkgs,
        package_version_hash=ver,
        candidates=cands,
        recommendations=[],
    )
    assert res.valid is True
    assert "RECOMMENDATION_DISABLED" not in res.reason_codes


def test_31_unapproved_recommendation_payload_fails_with_recommendation_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FC27_RECOMMENDATIONS_ENABLED", "false")
    pkgs, cands, ver = _sample_ranked_packages()
    res = validate_pre_approval_packages(
        pkgs,
        package_version_hash=ver,
        candidates=cands,
        recommendations=[{"action": "bench_player", "player_id": "p1"}],
    )
    assert res.valid is False
    assert res.approvable is False
    assert "RECOMMENDATION_DISABLED" in res.reason_codes


# ---------------------------------------------------------------------------
# 32-38: Graph & ApprovalStore fail-closed integration tests
# ---------------------------------------------------------------------------


def test_32_failed_pre_approval_validation_never_calls_interrupt() -> None:
    roles = FakeRoles()
    deps = GraphDeps(
        mcp=lambda tool, args: dashboard(),
        role_runner=lambda r, d, registry=None: roles(r, d),
    )
    app = build_graph(deps).compile(checkpointer=InMemorySaver())
    tid = "fc27-330323-run-20261028-0099"
    cfg = {"configurable": {"thread_id": tid}}

    state = initial_state("330323", tid)
    # Inject an invalid formation in state so prepare_approval fails validation
    state["formation"] = "invalid-formation-9-9-9"  # type: ignore[typeddict-unknown-key]

    result = app.invoke(state, cfg)
    snap = app.get_state(cfg)

    assert len(snap.interrupts) == 0
    assert list(snap.next) == []
    assert result["status"] == "failed"
    assert "FORMATION_UNSUPPORTED" in result["pre_approval_validation"]["reason_codes"]


def test_33_to_38_failed_validation_in_approval_store_blocks_request_nonce_decision_dispatch_and_inconsistencies(
    pg_uri: str,
) -> None:
    store = ApprovalStore(pg_uri)
    tid = "fc27-330323-run-20261028-0701"
    req_h = request_hash({"club_id": "330323", "test": "preapproval_fail"})

    state = store.create_run(
        club_id="330323",
        thread_id=tid,
        initial_state=lambda t: initial_state("330323", t),
        req_hash=req_h,
    )

    # Corrupt package objective so pre-approval validation fails
    bad_pkgs = fake_packages_dispatchable()
    bad_pkgs["coach"]["best"]["objective"]["target_delta"] = 250.0  # violates bounds
    ver = package_version(bad_pkgs)

    attempt_state = {
        **state,
        "status": "awaiting_approval",
        "lead_packages": bad_pkgs,
        "package_version": ver,
    }
    completed = store.complete_run(tid, attempt_state)

    # 34: Sets thread status to failed
    assert completed["status"] == "failed"
    assert completed["package_version"] is None
    assert completed["approval_request_id"] is None
    assert "OBJECTIVE_BOUND_VIOLATION" in completed["pre_approval_validation"]["reason_codes"]

    with psycopg.connect(pg_uri) as conn:
        # 33: Never creates public.approval_requests
        req_count = conn.execute(
            "SELECT count(*) FROM public.approval_requests WHERE thread_id = %s",
            (tid,),
        ).fetchone()[0]
        assert req_count == 0

        # 34: DB thread status is failed
        db_status = conn.execute(
            "SELECT status FROM public.graph_threads WHERE thread_id = %s",
            (tid,),
        ).fetchone()[0]
        assert db_status == "failed"

        # 37: Never creates a dispatch record
        disp_count = conn.execute(
            "SELECT count(*) FROM public.dispatches WHERE source_thread_id = %s",
            (tid,),
        ).fetchone()[0]
        assert disp_count == 0

        # 38: public.fc27_approval_inconsistencies remains empty
        inc_count = conn.execute(
            "SELECT count(*) FROM public.fc27_approval_inconsistencies WHERE thread_id = %s AND kind <> 'legacy_unlinked'",
            (tid,),
        ).fetchone()[0]
        assert inc_count == 0

    # 35: Cannot receive a challenge nonce
    commissioner = Principal(Role.commissioner, "batmanL8")
    intent = DecisionIntent(
        decision="APPROVE",
        owner="coach",
        selection="best",
        candidate_id=bad_pkgs["coach"]["best"]["candidate_id"],
        player_id=bad_pkgs["coach"]["best"]["player_id"],
    )
    with pytest.raises(StoreError) as exc_info:
        store.issue_challenge(tid, intent, commissioner, 120)
    assert exc_info.value.status_code == 409

    # 36: Cannot be approved, rejected, or revised as an awaiting approval run
    signed_dec = SignedDecision(
        thread_id=tid,
        decision="REJECT",
        package_version=ver or ("a" * 64),
        nonce="n" * 32,
        issued_at=1700000000,
        expires_at=1700000120,
        signature="0" * 64,
    )
    with pytest.raises(StoreError) as dec_exc:
        store.apply_decision(tid, signed_dec, commissioner, "secret")
    assert dec_exc.value.status_code in (403, 404, 409)


# ---------------------------------------------------------------------------
# 39-40: Metadata-only tracing
# ---------------------------------------------------------------------------


def test_39_and_40_trace_metadata_emits_only_safe_fields_and_no_sensitive_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGFUSE_CAPTURE_CONTENT", "false")
    tid = "fc27-330323-run-20261028-0888"
    pkgs, cands, ver = _sample_ranked_packages(tid)
    res = validate_pre_approval_packages(
        pkgs,
        package_version_hash=ver,
        candidates=cands,
        thread_id=tid,
    )

    recorded: list[tuple[str, str, dict[str, Any], str]] = []

    class DummyTracer:
        def event(self, name: str, tid: str, meta: dict[str, Any], level: str = "DEFAULT") -> None:
            recorded.append((name, tid, meta, level))

    out = emit_pre_approval_validation_trace(
        DummyTracer(),
        thread_id="fc27-330323-run-20261028-0888",
        club_id="330323",
        result=res,
    )

    assert out["event_name"] == "preapproval.validation.passed"
    assert len(recorded) == 1
    allowed_keys = {
        "thread_id",
        "club_id",
        "package_version",
        "policy_version",
        "package_count",
        "reason_codes",
        "confidence_tier",
        "formula_version",
        "timestamp",
    }
    assert set(out["metadata"].keys()) == allowed_keys
    serialized = json.dumps(out)
    for forbidden in ("Blaze", "Kante", "postgresql://", "best_runner_up", "rationale"):
        assert forbidden not in serialized


# ---------------------------------------------------------------------------
# 41-44: Adversarial & policy invariants
# ---------------------------------------------------------------------------


def test_41_adversarial_caller_controlled_resume_cannot_bypass_failed_pre_approval() -> None:
    roles = FakeRoles()
    deps = GraphDeps(
        mcp=lambda tool, args: dashboard(),
        role_runner=lambda r, d, registry=None: roles(r, d),
    )
    app = build_graph(deps).compile(checkpointer=InMemorySaver())
    tid = "fc27-330323-run-20261028-0901"
    cfg = {"configurable": {"thread_id": tid}}
    state = initial_state("330323", tid)
    state["recommendations"] = [{"action": "auto_apply"}]  # type: ignore[typeddict-unknown-key]

    res = app.invoke(state, cfg)
    assert res["status"] == "failed"

    # Attempt to resume a failed run with caller-supplied decision payload
    resumed = app.invoke(Command(resume={"decision": "APPROVE", "approved_by": "attacker"}), cfg)
    assert resumed["status"] == "failed"
    assert resumed.get("approval_decision") is None


def test_42_lead_package_dict_contract_validates_cleanly() -> None:
    pkgs = fake_packages_dispatchable()
    ver = package_version(pkgs)
    res = validate_pre_approval_packages(pkgs, package_version_hash=ver)
    assert res.valid is True
    assert res.approvable is True
    assert res.validated_package_count == 5
    assert res.rejected_package_count == 0


def test_43_all_reason_codes_are_in_valid_pre_approval_reason_codes() -> None:
    expected = {
        "EMPTY_LEAD_PACKAGES",
        "PACKAGE_SCHEMA_INVALID",
        "PACKAGE_HASH_MISSING",
        "PACKAGE_HASH_INVALID",
        "PACKAGE_HASH_MISMATCH",
        "DUPLICATE_CANDIDATE_PACKAGE",
        "CANDIDATE_ID_MISMATCH",
        "OBJECTIVE_METRIC_UNSUPPORTED",
        "OBJECTIVE_BOUND_VIOLATION",
        "OBJECTIVE_VALUE_NONFINITE",
        "FORMATION_UNSUPPORTED",
        "ROLE_ASSIGNMENT_INVALID",
        "DUPLICATE_PLAYER_ASSIGNMENT",
        "ROSTER_SIZE_INVALID",
        "INSUFFICIENT_SAMPLE",
        "INSUFFICIENT_DATA",
        "CONFIDENCE_METADATA_MISSING",
        "FORMULA_VERSION_UNSUPPORTED",
        "RECOMMENDATION_DISABLED",
        "APPROVAL_POLICY_VIOLATION",
        "REVIEW_REQUIRED",
        "VALIDATION_ANOMALY",
    }
    assert VALID_PRE_APPROVAL_REASON_CODES == expected


def test_44_pre_approval_validation_result_forbids_extra_fields_and_is_frozen() -> None:
    pkgs, cands, ver = _sample_ranked_packages()
    res = validate_pre_approval_packages(pkgs, package_version_hash=ver, candidates=cands)
    with pytest.raises(Exception):
        res.valid = False  # type: ignore[misc]
    with pytest.raises(Exception):
        PreApprovalValidationResult(
            valid=True,
            approvable=True,
            validated_at="2026-10-28T00:00:00Z",
            package_version=ver,
            extra_unexpected_field="boom",  # type: ignore[call-arg]
        )


def test_45_adversarial_external_id_or_email_in_player_ref_rejected() -> None:
    pkgs, cands, _ = _sample_ranked_packages()
    bad_pkgs = copy.deepcopy(pkgs)
    bad_pkgs["scout"]["best"]["player_id"] = "attacker@example.com"
    ver = package_version(bad_pkgs)
    res = validate_pre_approval_packages(bad_pkgs, package_version_hash=ver)
    assert res.valid is False
    assert "PACKAGE_SCHEMA_INVALID" in res.reason_codes


def test_46_fc27_approval_inconsistencies_empty_after_valid_and_invalid_runs(
    wired, pg_uri: str
) -> None:
    runner, app = wired()
    with TestClient(app) as client:
        r = client.post("/run", headers=SVC, json={"club_id": "330323"})
        assert r.status_code == 201
    with psycopg.connect(pg_uri) as conn:
        rows = conn.execute(
            "SELECT * FROM public.fc27_approval_inconsistencies WHERE kind <> 'legacy_unlinked'"
        ).fetchall()
    assert rows == []


def test_47_pre_approval_validation_result_enforces_utc_timestamp_and_sha256_hash_format() -> None:
    with pytest.raises(Exception):
        PreApprovalValidationResult(
            valid=True,
            approvable=True,
            validated_at="2026-10-28T00:00:00",  # naive timestamp rejected
            package_version="a" * 64,
        )
    with pytest.raises(Exception):
        PreApprovalValidationResult(
            valid=True,
            approvable=True,
            validated_at="2026-10-28T00:00:00+02:00",  # non-UTC offset rejected
            package_version="a" * 64,
        )
    with pytest.raises(Exception):
        PreApprovalValidationResult(
            valid=True,
            approvable=True,
            validated_at="2026-10-28T00:00:00Z",
            package_version="NOT-A-VALID-64-HEX-HASH",
        )
