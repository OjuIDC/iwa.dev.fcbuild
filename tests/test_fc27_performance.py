"""Offline test suite for FC27 performance telemetry and optimization baseline (`tests/test_fc27_performance.py`).

Covers all 40 mandatory test requirements from PART P:
1. telemetry disabled by default;
2. disabled telemetry changes no output behavior;
3. timing uses monotonic clock;
4. timestamps are UTC;
5. operation names are bounded;
6. raw responses excluded;
7. source player names excluded;
8. external IDs excluded;
9. tokens excluded;
10. DSNs excluded;
11. local secret paths excluded;
12. output directory is ignored;
13. retention limit is bounded;
14. file size is bounded;
15. deterministic scenario generation;
16. deterministic fixture hash;
17. deterministic result hash;
18. injected clock supported;
19. failed operation recorded safely;
20. raw exception excluded;
21. provider request count is zero;
22. Discord message count is zero;
23. database benchmark uses disposable DB;
24. profiling output sanitized;
25. benchmark report parses;
26. benchmark report versioned;
27. percentiles calculated correctly;
28. empty sample rejected;
29. negative duration rejected;
30. Polars optional;
31. missing Polars handled clearly;
32. pandas comparison uses sanitized data;
33. result-equivalence check;
34. threshold evaluation deterministic;
35. decision matrix valid;
36. language decision values bounded;
37. no rewrite performed;
38. recommendations disabled;
39. approval disabled;
40. dispatch disabled.
"""

from __future__ import annotations

import builtins
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg
import pytest
from pydantic import ValidationError

from src import settings
from src.fc27_performance import (
    ALLOWED_LANGUAGE_DECISIONS,
    ALLOWED_POLARS_DECISIONS,
    ALLOWED_RECOMMENDATION_CATEGORIES,
    ALLOWED_TELEMETRY_OPERATIONS,
    ALLOWED_VALIDATION_CLASSIFICATIONS,
    BASELINE_SCHEMA_VERSION,
    DEFAULT_PERFORMANCE_DIR,
    MAX_TELEMETRY_RUN_RETENTION,
    REPO_ROOT,
    FC27BenchmarkResult,
    FC27OperationTiming,
    FC27OptimizationDecision,
    FC27PerformanceBaseline,
    FC27PerformanceRecorder,
    FC27PerformanceSafetyError,
    FC27ResourceSample,
    assert_telemetry_payload_safe,
    build_benchmark_scenario_bundle,
    build_language_decision_matrix,
    build_prioritized_recommendations,
    collect_sanitized_environment_metadata,
    compute_percentile,
    compute_sha256_hex,
    evaluate_decision_thresholds,
    redact_local_paths_and_secrets,
    render_performance_summary_markdown,
    run_analytics_layer_benchmarks,
    run_container_benchmarks,
    run_dashboard_benchmarks,
    run_discord_command_benchmarks,
    run_polars_evaluation,
    run_postgres_benchmarks,
    run_python_profile_analysis,
    write_baseline_artifacts,
)


def test_01_telemetry_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FC27_PERFORMANCE_TELEMETRY_ENABLED", raising=False)
    assert settings.is_fc27_performance_telemetry_enabled() is False
    recorder = FC27PerformanceRecorder()
    assert recorder.enabled is False


def test_02_disabled_telemetry_changes_no_output_behavior(tmp_path: Path) -> None:
    recorder = FC27PerformanceRecorder(enabled=False, output_dir=tmp_path)
    expected = {"club_id": "330323", "matches": [1, 2, 3]}
    result, timing = recorder.measure_operation(
        "validation",
        lambda: expected,
        record_count=3,
    )
    assert result == expected
    assert timing.operation == "validation"
    assert timing.success is True
    assert timing.record_count == 3
    written = recorder.persist_run_if_enabled("run-disabled")
    assert written is None
    assert list(tmp_path.glob("*.json")) == []


def test_03_timing_uses_monotonic_clock() -> None:
    ticks = iter([100.0, 100.125])
    cpus = iter([10.0, 10.050])
    recorder = FC27PerformanceRecorder(
        enabled=False,
        monotonic_fn=lambda: next(ticks),
        process_time_fn=lambda: next(cpus),
    )
    _, timing = recorder.measure_operation("search", lambda: "ok", record_count=10)
    assert timing.elapsed_ms == pytest.approx(125.0, abs=1e-4)
    assert timing.cpu_time_ms == pytest.approx(50.0, abs=1e-4)


def test_04_timestamps_are_utc() -> None:
    valid_timing = FC27OperationTiming(
        operation="search",
        started_at="2026-11-03T12:00:00Z",
        completed_at="2026-11-03T12:00:01+00:00",
        elapsed_ms=10.0,
        success=True,
        record_count=10,
        peak_rss_mb=32.0,
        cpu_time_ms=5.0,
        execution_hash="a" * 64,
    )
    assert valid_timing.started_at.endswith("Z")
    assert valid_timing.completed_at.endswith("Z")

    with pytest.raises(ValidationError, match="UTC"):
        FC27OperationTiming(
            operation="search",
            started_at="2026-11-03T12:00:00",  # naive
            completed_at="2026-11-03T12:00:01Z",
            elapsed_ms=10.0,
            success=True,
            record_count=10,
            peak_rss_mb=32.0,
            cpu_time_ms=5.0,
            execution_hash="a" * 64,
        )

    with pytest.raises(ValidationError, match="UTC"):
        FC27ResourceSample(
            target_name="fc27-community-worker",
            sampled_at="2026-11-03T12:00:00-05:00",  # non-UTC offset
        )


def test_05_operation_names_are_bounded() -> None:
    assert len(ALLOWED_TELEMETRY_OPERATIONS) == 27
    for op in (
        "poll_cycle_offline",
        "provider_fixture_load",
        "validation",
        "normalization",
        "privacy_scan",
        "persistence_plan",
        "persistence_commit",
        "persisted_readback",
        "search",
        "scout",
        "analyst",
        "coach",
        "tactics",
        "director",
        "full_advisory_pipeline",
        "dashboard_status",
        "dashboard_matches",
        "dashboard_analysis",
        "dashboard_coach",
        "dashboard_tactics",
        "dashboard_director",
        "discord_status",
        "discord_latest",
        "discord_analysis",
        "backup",
        "restore",
        "container_startup",
    ):
        assert op in ALLOWED_TELEMETRY_OPERATIONS

    recorder = FC27PerformanceRecorder(enabled=False)
    with pytest.raises(ValueError, match="Unsupported telemetry operation"):
        recorder.measure_operation("arbitrary_operation", lambda: None)


def test_06_raw_responses_excluded() -> None:
    for token in ("raw_response", "raw_payload", "raw_body"):
        with pytest.raises(FC27PerformanceSafetyError, match="Forbidden sensitive token"):
            assert_telemetry_payload_safe({"field": token})


def test_07_source_player_names_excluded() -> None:
    for token in ("gamertag", "gamer_tag", "proname", "personaname", "fixture_player_1", "src_player_9"):
        with pytest.raises(FC27PerformanceSafetyError, match="Forbidden sensitive token"):
            assert_telemetry_payload_safe({"player": token})


def test_08_external_ids_excluded() -> None:
    for token in ("persona_id", "personaid", "blaze_id", "nucleus_id", "ea_account_id"):
        with pytest.raises(FC27PerformanceSafetyError, match="Forbidden sensitive token"):
            assert_telemetry_payload_safe({"id_type": token})


def test_09_tokens_excluded() -> None:
    for token in ("discord_bot_token", "Bearer secret-jwt", "Authorization: Basic", "Set-Cookie: sid=1"):
        with pytest.raises(FC27PerformanceSafetyError, match="Forbidden sensitive token"):
            assert_telemetry_payload_safe({"auth": token})


def test_10_dsns_excluded() -> None:
    for dsn in ("postgresql://fc27_app:secret@127.0.0.1:5432/fc27", "postgres://user:pw@localhost/db"):
        with pytest.raises(FC27PerformanceSafetyError, match="Forbidden sensitive token"):
            assert_telemetry_payload_safe({"dsn": dsn})
        redacted = redact_local_paths_and_secrets(f"connected to {dsn}")
        assert "postgres" not in redacted.lower()
        assert "<REDACTED_DSN>" in redacted


def test_11_local_secret_paths_excluded() -> None:
    for secret_path in (
        ".local-data/secrets/discord_bot_token",
        "/run/secrets/discord_bot_token",
        r"C:\Users\alice\secrets\token.txt",
        "/home/alice/.config/secret",
    ):
        with pytest.raises(FC27PerformanceSafetyError):
            assert_telemetry_payload_safe({"path": secret_path})
        redacted = redact_local_paths_and_secrets(secret_path)
        assert "alice" not in redacted
        assert "discord_bot_token" not in redacted


def test_12_output_directory_is_ignored() -> None:
    rel_dir = DEFAULT_PERFORMANCE_DIR.relative_to(REPO_ROOT).as_posix()
    assert rel_dir == ".local-artifacts/performance"
    proc = subprocess.run(
        ["git", "check-ignore", "-v", ".local-artifacts/performance/fc27-performance-baseline.json"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert ".gitignore" in proc.stdout


def test_13_retention_limit_is_bounded(tmp_path: Path) -> None:
    assert MAX_TELEMETRY_RUN_RETENTION == 20
    base_dt = datetime(2026, 11, 3, 12, 0, 0, tzinfo=timezone.utc)
    step = [0]

    def _clock() -> datetime:
        step[0] += 1
        return base_dt + timedelta(seconds=step[0])

    recorder = FC27PerformanceRecorder(
        enabled=True,
        output_dir=tmp_path,
        retention_limit=5,
        utc_now_fn=_clock,
    )
    for idx in range(8):
        recorder.measure_operation("search", lambda i=idx: i, record_count=1)
        recorder.persist_run_if_enabled(f"run{idx:02d}")

    remaining = sorted(tmp_path.glob("telemetry-run-*.json"))
    assert len(remaining) == 5
    names = [p.name for p in remaining]
    assert any("run07" in n for n in names)
    assert not any("run00" in n for n in names)


def test_14_file_size_is_bounded(tmp_path: Path) -> None:
    recorder = FC27PerformanceRecorder(
        enabled=True,
        output_dir=tmp_path,
        max_file_bytes=1024,
    )
    for _ in range(10):
        recorder.measure_operation(
            "search",
            lambda: "ok",
            record_count=10,
            limitations=("SYNTHETIC_FIXTURE_ONLY_BOUNDED_LIMITATION_NOTE",),
        )
    with pytest.raises(FC27PerformanceSafetyError, match="exceeds limit"):
        recorder.persist_run_if_enabled("oversized")


def test_15_deterministic_scenario_generation() -> None:
    expected = {
        "SMALL": (10, 10, 60),
        "MEDIUM": (100, 100, 600),
        "LARGE": (1000, 1000, 6000),
        "STRESS": (10000, 10000, 60000),
    }
    for name, (m_cnt, t_cnt, p_cnt) in expected.items():
        meta, _, norm_matches, supplied = build_benchmark_scenario_bundle(name)  # type: ignore[arg-type]
        assert meta.scenario == name
        assert meta.match_count == m_cnt
        assert meta.team_stat_count == t_cnt
        assert meta.player_stat_count == p_cnt
        assert len(norm_matches) == m_cnt
        assert len(supplied) == m_cnt


def test_16_deterministic_fixture_hash() -> None:
    meta1, _, _, _ = build_benchmark_scenario_bundle("SMALL")
    meta2, _, _, _ = build_benchmark_scenario_bundle("SMALL")
    assert meta1.fixture_hash == meta2.fixture_hash
    assert len(meta1.fixture_hash) == 64


def test_17_deterministic_result_hash() -> None:
    out_hash = compute_sha256_hex({"out": 42})
    r1 = FC27BenchmarkResult.from_samples(
        operation="search",
        scenario="SMALL",
        warmup_iterations=3,
        samples_ms=[1.2, 1.4, 1.3],
        peak_rss_delta_mb=0.1,
        input_row_count=10,
        output_object_count=1,
        deterministic_output_hash=out_hash,
    )
    r2 = FC27BenchmarkResult.from_samples(
        operation="search",
        scenario="SMALL",
        warmup_iterations=3,
        samples_ms=[2.1, 2.0, 2.2],
        peak_rss_delta_mb=0.2,
        input_row_count=10,
        output_object_count=1,
        deterministic_output_hash=out_hash,
    )
    assert r1.result_hash == r2.result_hash


def test_18_injected_clock_supported() -> None:
    fixed_dt1 = datetime(2026, 11, 3, 15, 0, 0, tzinfo=timezone.utc)
    fixed_dt2 = datetime(2026, 11, 3, 15, 0, 2, tzinfo=timezone.utc)
    dts = iter([fixed_dt1, fixed_dt2])
    monos = iter([50.0, 52.0])
    cpus = iter([1.0, 1.25])

    recorder = FC27PerformanceRecorder(
        enabled=False,
        monotonic_fn=lambda: next(monos),
        process_time_fn=lambda: next(cpus),
        utc_now_fn=lambda: next(dts),
    )
    _, timing = recorder.measure_operation("coach", lambda: "done", record_count=4)
    assert timing.started_at == "2026-11-03T15:00:00Z"
    assert timing.completed_at == "2026-11-03T15:00:02Z"
    assert timing.elapsed_ms == pytest.approx(2000.0)
    assert timing.cpu_time_ms == pytest.approx(250.0)


def test_19_failed_operation_recorded_safely() -> None:
    recorder = FC27PerformanceRecorder(enabled=False)
    with pytest.raises(RuntimeError):
        recorder.measure_operation(
            "validation",
            lambda: (_ for _ in ()).throw(RuntimeError("postgresql://secret@host/db leaked")),
            record_count=5,
        )
    assert len(recorder.records) == 1
    rec = recorder.records[0]
    assert rec.success is False
    assert rec.error_code == "RuntimeError"


def test_20_raw_exception_excluded() -> None:
    recorder = FC27PerformanceRecorder(enabled=False)
    secret_msg = "postgresql://fc27_owner:supersecret@127.0.0.1:5432/fc27 gamertag=ProPlayer99"
    with pytest.raises(ValueError):
        recorder.measure_operation(
            "normalization",
            lambda: (_ for _ in ()).throw(ValueError(secret_msg)),
        )
    serialized = json.dumps(recorder.records[0].model_dump(mode="json"))
    assert "supersecret" not in serialized
    assert "ProPlayer99" not in serialized
    assert "postgresql://" not in serialized
    assert recorder.records[0].error_code == "ValueError"


def test_21_provider_request_count_is_zero() -> None:
    _, results = run_analytics_layer_benchmarks(
        scenarios=("SMALL",),
        warmup_iterations=1,
        measured_iterations=2,
    )
    assert len(results) > 0
    for r in results:
        assert r.provider_requests_executed == 0


def test_22_discord_message_count_is_zero(pg_admin_uri: str) -> None:
    details, op_results = run_discord_command_benchmarks(
        pg_admin_uri,
        warmup_iterations=1,
        measured_iterations=2,
    )
    assert len(details) == 8
    for cmd_info in details.values():
        assert cmd_info["provider_requests"] == 0
        assert cmd_info["database_writes"] == 0
        assert cmd_info["privacy_scan_result"] == "PASS"
    for r in op_results:
        assert r.discord_messages_sent == 0
        assert r.provider_requests_executed == 0


def test_23_database_benchmark_uses_disposable_db(pg_admin_uri: str) -> None:
    with psycopg.connect(pg_admin_uri) as conn:
        before_dbs = {
            row[0]
            for row in conn.execute(
                "SELECT datname FROM pg_database WHERE datname LIKE 'fc27_perf_%'"
            ).fetchall()
        }

    pg_summary, pg_results = run_postgres_benchmarks(
        pg_admin_uri,
        warmup_iterations=1,
        measured_iterations=2,
    )
    assert pg_summary["disposable_database_used"] is True
    assert "latest_match_read" in pg_summary["explain_analyze_buffers"]
    assert "team_form_read" in pg_summary["explain_analyze_buffers"]
    assert "player_summary_read" in pg_summary["explain_analyze_buffers"]
    assert len(pg_results) >= 5

    with psycopg.connect(pg_admin_uri) as conn:
        after_dbs = {
            row[0]
            for row in conn.execute(
                "SELECT datname FROM pg_database WHERE datname LIKE 'fc27_perf_%'"
            ).fetchall()
        }
    assert after_dbs == before_dbs


def test_24_profiling_output_sanitized_and_boundaries_classified() -> None:
    prof_report = run_python_profile_analysis("SMALL")
    assert_telemetry_payload_safe(prof_report)
    assert len(prof_report["top_cumulative_time_functions"]) > 0
    assert len(prof_report["top_self_time_functions"]) > 0
    classifications = {
        item["classification"] for item in prof_report["validation_boundary_classifications"]
    }
    assert classifications == ALLOWED_VALIDATION_CLASSIFICATIONS


def test_25_benchmark_report_parses() -> None:
    scenarios, op_results = run_analytics_layer_benchmarks(
        scenarios=("SMALL",),
        warmup_iterations=1,
        measured_iterations=2,
    )
    dash_summary, dash_results = run_dashboard_benchmarks(
        warmup_iterations=1,
        measured_iterations=2,
    )
    containers = run_container_benchmarks()
    py_prof = run_python_profile_analysis("SMALL")
    polars_eval = run_polars_evaluation(row_counts=(10, 1000))
    all_ops = (*op_results, *dash_results)
    thresholds = evaluate_decision_thresholds(
        postgres_benchmarks={"explain_analyze_buffers": {}},
        operation_results=all_ops,
        dashboard_benchmarks=dash_summary,
        discord_benchmarks={},
        container_benchmarks=containers,
        python_profile=py_prof,
    )
    baseline = FC27PerformanceBaseline(
        benchmark_timestamp="2026-11-03T12:00:00Z",
        git_commit="c55f1a18a7281dd9af6779c309d1bc9eabaadbf8",
        environment=collect_sanitized_environment_metadata(),
        scenarios=scenarios,
        operation_results=all_ops,
        postgres_benchmarks={"disposable_database_used": True, "explain_analyze_buffers": {}},
        dashboard_benchmarks=dash_summary,
        discord_benchmarks={},
        container_benchmarks=containers,
        python_profile=py_prof,
        polars_evaluation=polars_eval,
        threshold_evaluations=thresholds,
        language_decision_matrix=build_language_decision_matrix(),
        prioritized_recommendations=build_prioritized_recommendations(),
        baseline_hash="b" * 64,
    )
    dumped = json.dumps(baseline.model_dump(mode="json"))
    parsed = FC27PerformanceBaseline.model_validate_json(dumped)
    assert parsed.baseline_hash == "b" * 64
    md = render_performance_summary_markdown(parsed)
    assert "East Coasters FC27 Local Performance Baseline Summary" in md


def test_26_benchmark_report_versioned() -> None:
    assert BASELINE_SCHEMA_VERSION == "fc27-performance-baseline-v1"
    with pytest.raises(ValidationError, match="Unsupported schema_version"):
        FC27PerformanceBaseline(
            schema_version="fc27-performance-baseline-v0",
            benchmark_timestamp="2026-11-03T12:00:00Z",
            git_commit="c55f1a1",
            environment={},
            scenarios=(),
            operation_results=(),
            postgres_benchmarks={},
            dashboard_benchmarks={},
            discord_benchmarks={},
            container_benchmarks=(),
            python_profile={},
            polars_evaluation={},
            threshold_evaluations=(),
            language_decision_matrix=(),
            prioritized_recommendations=(),
            baseline_hash="c" * 64,
        )


def test_27_percentiles_calculated_correctly() -> None:
    samples = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert compute_percentile(samples, 0.0) == pytest.approx(10.0)
    assert compute_percentile(samples, 50.0) == pytest.approx(30.0)
    assert compute_percentile(samples, 95.0) == pytest.approx(48.0)
    assert compute_percentile(samples, 100.0) == pytest.approx(50.0)
    assert compute_percentile([7.5], 95.0) == pytest.approx(7.5)


def test_28_empty_sample_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        compute_percentile([], 50.0)
    with pytest.raises(ValueError, match="empty"):
        FC27BenchmarkResult.from_samples(
            operation="search",
            scenario="SMALL",
            warmup_iterations=1,
            samples_ms=[],
            peak_rss_delta_mb=0.0,
            input_row_count=10,
            output_object_count=1,
            deterministic_output_hash="a" * 64,
        )


def test_29_negative_duration_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        compute_percentile([1.0, -0.5, 2.0], 50.0)

    monos = iter([10.0, 9.5])
    recorder = FC27PerformanceRecorder(
        enabled=False,
        monotonic_fn=lambda: next(monos),
    )
    with pytest.raises(ValueError, match="negative duration"):
        recorder.measure_operation("search", lambda: "ok")


def test_30_polars_optional() -> None:
    report = run_polars_evaluation(row_counts=(10, 1000))
    assert report["adoption_status"] in ALLOWED_POLARS_DECISIONS
    assert len(report["comparisons"]) == 2


def test_31_missing_polars_handled_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    orig_import = builtins.__import__

    def _fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "polars" or name.startswith("polars."):
            raise ImportError("No module named 'polars'")
        return orig_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    report = run_polars_evaluation(row_counts=(10, 1000))
    assert report["polars_installed"] is False
    assert report["polars_version"] is None
    assert report["adoption_status"] == "NOT_JUSTIFIED"
    for comp in report["comparisons"]:
        assert comp["polars_elapsed_ms"] is None
        assert comp["result_equivalence"] is True


def test_32_pandas_comparison_uses_sanitized_data() -> None:
    report = run_polars_evaluation(row_counts=(10, 1000))
    assert_telemetry_payload_safe(report)
    for comp in report["comparisons"]:
        assert comp["sanitized_pseudonymous_only"] is True
        assert comp["utc_timestamp_handling"] == "VERIFIED_UTC"


def test_33_result_equivalence_check() -> None:
    report = run_polars_evaluation(row_counts=(10, 1000, 10000, 60000))
    assert [c["row_count"] for c in report["comparisons"]] == [10, 1000, 10000, 60000]
    for comp in report["comparisons"]:
        assert comp["result_equivalence"] is True


def test_34_threshold_evaluation_deterministic() -> None:
    sample_res = FC27BenchmarkResult.from_samples(
        operation="full_advisory_pipeline",
        scenario="LARGE",
        warmup_iterations=1,
        samples_ms=[120.0, 125.0, 130.0],
        peak_rss_delta_mb=2.0,
        input_row_count=1000,
        output_object_count=6,
        deterministic_output_hash="a" * 64,
    )
    t1 = evaluate_decision_thresholds(
        postgres_benchmarks={"explain_analyze_buffers": {"q1": {"planning_time_ms": 0.2, "execution_time_ms": 1.1}}},
        operation_results=(sample_res,),
        dashboard_benchmarks={"/demo/status": {"warm_p95_ms": 8.5}},
        discord_benchmarks={"status": {"p95_ms": 15.0}},
        container_benchmarks=(
            FC27ResourceSample(
                target_name="fc27-community-worker",
                sampled_at="2026-11-03T12:00:00Z",
                cold_start_ms=1400.0,
                idle_rss_mb=55.0,
            ),
        ),
        python_profile={"traced_peak_memory_mb": 12.5},
    )
    t2 = evaluate_decision_thresholds(
        postgres_benchmarks={"explain_analyze_buffers": {"q1": {"planning_time_ms": 0.2, "execution_time_ms": 1.1}}},
        operation_results=(sample_res,),
        dashboard_benchmarks={"/demo/status": {"warm_p95_ms": 8.5}},
        discord_benchmarks={"status": {"p95_ms": 15.0}},
        container_benchmarks=(
            FC27ResourceSample(
                target_name="fc27-community-worker",
                sampled_at="2026-11-03T12:00:00Z",
                cold_start_ms=1400.0,
                idle_rss_mb=55.0,
            ),
        ),
        python_profile={"traced_peak_memory_mb": 12.5},
    )
    assert t1 == t2
    assert len(t1) == 9
    assert all(item["exceeded"] is False for item in t1)


def test_35_decision_matrix_valid() -> None:
    matrix = build_language_decision_matrix()
    assert len(matrix) == 6
    languages = {item.candidate_language for item in matrix}
    assert any("Python 3.11+" in lang for lang in languages)
    assert any("Polars" in lang for lang in languages)
    assert "Go" in languages
    assert "Rust" in languages
    assert "C#/.NET" in languages
    assert "C/C++" in languages
    assert matrix[0].decision == "KEEP_PYTHON"

    recs = build_prioritized_recommendations()
    rec_categories = {r.recommendation_category for r in recs}
    assert rec_categories == ALLOWED_RECOMMENDATION_CATEGORIES


def test_36_language_decision_values_bounded() -> None:
    assert ALLOWED_LANGUAGE_DECISIONS == frozenset(
        {
            "KEEP_PYTHON",
            "USE_NATIVE_BACKED_PYTHON",
            "EXTRACT_WHEN_THRESHOLD_REACHED",
            "PROTOTYPE_SEPARATELY",
            "NOT_JUSTIFIED",
        }
    )
    with pytest.raises(ValidationError, match="Invalid language decision"):
        FC27OptimizationDecision(
            target_component="Test",
            candidate_language="Rust",
            recommendation_category="RUST_PIPELINE_TRIGGER",
            measured_current_bottleneck="none",
            expected_benefit="none",
            integration_cost="high",
            contract_duplication_risk="high",
            privacy_boundary_risk="high",
            deployment_complexity="high",
            testing_burden="high",
            observability_burden="high",
            rollback_complexity="high",
            decision="REWRITE_EVERYTHING_NOW",
            trigger_for_reconsideration="none",
            risk="high",
            estimated_implementation_complexity="HIGH",
            verification_test="tests/test_fc27_performance.py",
            rollback_plan="revert",
        )


def test_37_no_rewrite_performed() -> None:
    src_dir = REPO_ROOT / "src"
    non_python = [
        p
        for p in src_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in {".rs", ".go", ".cs", ".c", ".cpp", ".cc", ".cxx"}
    ]
    assert non_python == []


def test_38_recommendations_disabled() -> None:
    assert settings.is_fc27_recommendations_enabled() is False
    dash_details, _ = run_dashboard_benchmarks(warmup_iterations=1, measured_iterations=1)
    assert len(dash_details) == 6
    for ep in (
        "/demo/status",
        "/demo/matches",
        "/demo/analysis",
        "/api/fc27/demo/coach",
        "/api/fc27/demo/tactics",
        "/api/fc27/demo/director",
    ):
        assert ep in dash_details
        assert dash_details[ep]["status_code"] == 200


def test_39_approval_disabled() -> None:
    with pytest.raises(ValidationError, match="recommendations, approval, and dispatch must remain False"):
        FC27PerformanceBaseline(
            benchmark_timestamp="2026-11-03T12:00:00Z",
            git_commit="c55f1a1",
            approval_enabled=True,
            environment={},
            scenarios=(),
            operation_results=(),
            postgres_benchmarks={},
            dashboard_benchmarks={},
            discord_benchmarks={},
            container_benchmarks=(),
            python_profile={},
            polars_evaluation={},
            threshold_evaluations=(),
            language_decision_matrix=(),
            prioritized_recommendations=(),
            baseline_hash="d" * 64,
        )


def test_40_dispatch_disabled(tmp_path: Path) -> None:
    assert settings.is_dispatch_enabled() is False
    assert settings.is_dispatch_worker_enabled() is False
    baseline = FC27PerformanceBaseline(
        benchmark_timestamp="2026-11-03T12:00:00Z",
        git_commit="c55f1a18a7281dd9af6779c309d1bc9eabaadbf8",
        environment=collect_sanitized_environment_metadata(),
        scenarios=(),
        operation_results=(),
        postgres_benchmarks={},
        dashboard_benchmarks={},
        discord_benchmarks={},
        container_benchmarks=(),
        python_profile={},
        polars_evaluation={"adoption_status": "NOT_JUSTIFIED"},
        threshold_evaluations=(),
        language_decision_matrix=build_language_decision_matrix(),
        prioritized_recommendations=build_prioritized_recommendations(),
        baseline_hash="e" * 64,
    )
    assert baseline.recommendations_enabled is False
    assert baseline.approval_enabled is False
    assert baseline.dispatch_enabled is False
    j_path, m_path = write_baseline_artifacts(baseline, output_dir=tmp_path)
    assert j_path.is_file()
    assert m_path.is_file()
