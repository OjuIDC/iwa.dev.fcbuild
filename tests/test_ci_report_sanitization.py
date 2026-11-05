"""Unit and adversarial policy tests for FC27 CI browser-previewable reports and sanitization.

Covers all 26+ mandatory report-generation, self-contained HTML, privacy/security
sanitization, count-reconciliation, trusted-context Pages gating, and retention
pruning invariants required by the FC27 CI Browser Report Policy.
"""

from __future__ import annotations

import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from scripts.aggregate_test_reports import (
    PAGES_REPORT_RETENTION_COUNT,
    aggregate_reports,
    prune_pages_report_tree,
)
from scripts.build_test_report import (
    build_job_report,
    build_job_summary_markdown,
    extract_counts_from_html,
    extract_counts_from_summary_markdown,
    parse_junit_xml_files,
    reconcile_report_counts,
    render_self_contained_html_report,
    write_sanitized_junit_xml,
)
from scripts.classify_ci_changes import evaluate_trusted_context
from scripts.sanitize_test_report import (
    main as sanitize_cli_main,
    sanitize_text_for_report,
    scan_paths,
    scan_text_content,
)


def _write_sample_junit(
    path: Path,
    *,
    passed: int = 3,
    failed: int = 0,
    skipped: int = 1,
    failure_msg: str = "AssertionError: expected 200 got 403",
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    total = passed + failed + skipped
    suite = ET.Element(
        "testsuite",
        {
            "name": "sample",
            "tests": str(total),
            "failures": str(failed),
            "errors": "0",
            "skipped": str(skipped),
            "time": "1.250",
        },
    )
    for i in range(passed):
        ET.SubElement(
            suite,
            "testcase",
            {
                "classname": "tests.test_sample",
                "name": f"test_pass_{i}",
                "file": "tests/test_sample.py",
                "time": f"0.{i + 1}00",
            },
        )
    for i in range(failed):
        tc = ET.SubElement(
            suite,
            "testcase",
            {
                "classname": "tests.test_sample",
                "name": f"test_fail_{i}",
                "file": "tests/test_sample.py",
                "time": "0.250",
            },
        )
        fl = ET.SubElement(tc, "failure", {"message": failure_msg})
        fl.text = failure_msg
    for i in range(skipped):
        tc = ET.SubElement(
            suite,
            "testcase",
            {
                "classname": "tests.test_sample",
                "name": f"test_skip_{i}",
                "file": "tests/test_sample.py",
                "time": "0.010",
            },
        )
        ET.SubElement(tc, "skipped", {"message": "skipped in sample"})

    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)
    return path


def test_1_junit_xml_generation_and_parsing(tmp_path: Path) -> None:
    raw_xml = _write_sample_junit(tmp_path / "raw.xml", passed=4, failed=1, skipped=2)
    parsed = parse_junit_xml_files([raw_xml])
    assert parsed["collected"] == 7
    assert parsed["passed"] == 4
    assert parsed["failed"] == 1
    assert parsed["skipped"] == 2

    out_xml = tmp_path / "out" / "junit.xml"
    write_sanitized_junit_xml(parsed, out_xml, suite_name="ci:sample")
    assert out_xml.is_file()
    reparsed = parse_junit_xml_files([out_xml])
    assert reparsed["collected"] == 7
    assert reparsed["passed"] == 4
    assert reparsed["failed"] == 1
    assert reparsed["skipped"] == 2


def test_2_self_contained_html_report_generation(tmp_path: Path) -> None:
    raw_xml = _write_sample_junit(tmp_path / "raw.xml", passed=5, failed=0, skipped=1)
    meta = build_job_report(
        junit_inputs=[raw_xml],
        junit_out=tmp_path / "out" / "test-results" / "junit.xml",
        html_out=tmp_path / "out" / "test-report" / "index.html",
        summary_out=tmp_path / "out" / "job-summary.md",
        durations_out=tmp_path / "out" / "test-results" / "durations.json",
        meta_out=tmp_path / "out" / "job-meta.json",
        workflow="test-fast",
        test_group="unit-domain",
        commit_sha="abcdef1234567890",
        pr_number="42",
        python_version="3.11",
        postgres_version="N/A",
    )
    html_text = (tmp_path / "out" / "test-report" / "index.html").read_text(encoding="utf-8")
    assert "<style>" in html_text
    assert meta["html_report_self_contained"] == "YES"
    assert meta["html_external_resources"] == 0
    assert extract_counts_from_html(html_text) == {
        "collected": 6,
        "passed": 5,
        "failed": 0,
        "skipped": 1,
    }


def test_3_job_summary_markdown_generation(tmp_path: Path) -> None:
    raw_xml = _write_sample_junit(tmp_path / "raw.xml", passed=3, failed=1, skipped=1)
    step_summary = tmp_path / "step_summary.md"
    build_job_report(
        junit_inputs=[raw_xml],
        junit_out=tmp_path / "out" / "test-results" / "junit.xml",
        html_out=tmp_path / "out" / "test-report" / "index.html",
        summary_out=tmp_path / "out" / "job-summary.md",
        durations_out=tmp_path / "out" / "test-results" / "durations.json",
        meta_out=tmp_path / "out" / "job-meta.json",
        workflow="test-fast",
        test_group="postgres-focused",
        commit_sha="deadbeef12345678",
        pr_number="99",
        python_version="3.11",
        postgres_version="16.15",
        step_summary_path=step_summary,
    )
    md = step_summary.read_text(encoding="utf-8")
    for required_field in (
        "Workflow Name",
        "Test Group Name",
        "Commit SHA",
        "Pull Request",
        "Python Version",
        "PostgreSQL Version",
        "Total Collected",
        "Passed",
        "Failed",
        "Skipped",
        "Total Duration",
        "JUnit XML Status",
        "HTML Report Status",
        "Artifact Instructions",
        "Browser Preview",
        "Slowest 10 Tests",
        "Sanitized Failure Summary",
    ):
        assert required_field in md
    assert extract_counts_from_summary_markdown(md) == {
        "collected": 5,
        "passed": 3,
        "failed": 1,
        "skipped": 1,
    }


def test_4_aggregate_job_summary_and_report_generation(tmp_path: Path) -> None:
    shard1_xml = _write_sample_junit(tmp_path / "s1" / "raw.xml", passed=4, failed=0, skipped=1)
    shard2_xml = _write_sample_junit(tmp_path / "s2" / "raw.xml", passed=6, failed=0, skipped=2)

    s1_out = tmp_path / "downloaded" / "s1"
    s2_out = tmp_path / "downloaded" / "s2"
    build_job_report(
        junit_inputs=[shard1_xml],
        junit_out=s1_out / "test-results" / "junit.xml",
        html_out=s1_out / "test-report" / "index.html",
        summary_out=s1_out / "job-summary.md",
        durations_out=s1_out / "test-results" / "durations.json",
        meta_out=s1_out / "job-meta.json",
        workflow="ci",
        test_group="full-quality",
        commit_sha="1234567890abcdef",
    )
    build_job_report(
        junit_inputs=[shard2_xml],
        junit_out=s2_out / "test-results" / "junit.xml",
        html_out=s2_out / "test-report" / "index.html",
        summary_out=s2_out / "job-summary.md",
        durations_out=s2_out / "test-results" / "durations.json",
        meta_out=s2_out / "job-meta.json",
        workflow="ci",
        test_group="full-unit-domain",
        commit_sha="1234567890abcdef",
    )

    agg_step_summary = tmp_path / "agg_step_summary.md"
    agg = aggregate_reports(
        input_dirs=[tmp_path / "downloaded"],
        out_dir=tmp_path / "aggregate",
        workflow="ci",
        commit_sha="1234567890abcdef",
        expected_total_collected=13,
        step_summary_path=agg_step_summary,
    )
    assert agg["collected"] == 13
    assert agg["passed"] == 10
    assert agg["failed"] == 0
    assert agg["skipped"] == 3
    assert agg["report_count_reconciliation"] == "PASS"
    assert agg["actions_aggregate_summary_created"] == "YES"
    assert "Shard / Job Breakdown" in agg_step_summary.read_text(encoding="utf-8")


def test_5_html_report_rejects_external_script_urls() -> None:
    bad_html = '<html><head><script src="https://cdn.example.com/app.js"></script></head><body></body></html>'
    res = scan_text_content(bad_html, is_html=True)
    assert res["status"] == "FAIL"
    assert "HTML_EXTERNAL_SCRIPT" in res["categories"]
    assert res["html_self_contained"] is False
    assert res["html_external_resources"] == 1


def test_6_html_report_rejects_external_stylesheet_urls() -> None:
    bad_html = '<html><head><link rel="stylesheet" href="https://fonts.googleapis.com/css?family=Inter"></head></html>'
    res = scan_text_content(bad_html, is_html=True)
    assert res["status"] == "FAIL"
    assert "HTML_EXTERNAL_STYLESHEET" in res["categories"]
    assert res["html_self_contained"] is False


def test_7_html_report_rejects_remote_images_and_tracking_pixels() -> None:
    bad_img = '<html><body><img src="https://tracker.example.com/pixel.png" width="1" height="1"></body></html>'
    res = scan_text_content(bad_img, is_html=True)
    assert res["status"] == "FAIL"
    assert "HTML_REMOTE_IMAGE" in res["categories"] or "HTML_TRACKING_PIXEL" in res["categories"]
    assert res["html_self_contained"] is False


def test_8_html_report_strips_and_rejects_pytest_html_environment_dump(tmp_path: Path) -> None:
    raw_pytest_html = (
        '<html><body><h2>Environment</h2><table id="environment">'
        "<tr><td>GITHUB_TOKEN</td><td>ghp_1234567890abcdefghijklmnopqrstuv</td></tr>"
        "</table></body></html>"
    )
    direct_scan = scan_text_content(raw_pytest_html, is_html=True)
    assert direct_scan["status"] == "FAIL"
    assert "ENVIRONMENT_DUMP" in direct_scan["categories"]

    raw_html_path = tmp_path / "raw-pytest.html"
    raw_html_path.write_text(raw_pytest_html, encoding="utf-8")
    raw_xml = _write_sample_junit(tmp_path / "raw.xml", passed=2, failed=0, skipped=0)

    meta = build_job_report(
        junit_inputs=[raw_xml],
        raw_pytest_html_in=raw_html_path,
        junit_out=tmp_path / "out" / "test-results" / "junit.xml",
        html_out=tmp_path / "out" / "test-report" / "index.html",
        summary_out=tmp_path / "out" / "job-summary.md",
        durations_out=tmp_path / "out" / "test-results" / "durations.json",
        meta_out=tmp_path / "out" / "job-meta.json",
        workflow="ci",
        test_group="quality",
        commit_sha="abcdef",
    )
    assert meta["html_report_privacy_scan"] == "PASS"
    out_html = (tmp_path / "out" / "test-report" / "index.html").read_text(encoding="utf-8")
    assert 'id="environment"' not in out_html
    assert "ghp_1234567890abcdefghijklmnopqrstuv" not in out_html


@pytest.mark.parametrize(
    ("category", "sample_payload"),
    [
        ("POSTGRESQL_URI", "connection failed for postgresql://fc27_app:SuperSecret99@127.0.0.1:5432/fc27"),
        ("REDIS_URL", "redis error at redis://:MyRedisPass123@127.0.0.1:6379/0"),
        ("DISCORD_TOKEN", "bot token MTIzNDU2Nzg5MDEyMzQ1Njc4OQ.GhIjKl.MnOpQrStUvWxYz0123456789abcdefABCDEF leaked"),
        ("JWT_TOKEN", "token eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"),
        ("API_KEY", "langfuse key sk-lf-1234567890abcdef1234567890abcdef"),
        ("AUTHORIZATION_HEADER", "Authorization: Bearer secret_bearer_token_value_999"),
        ("COOKIE_HEADER", "Set-Cookie: fc27_session=raw_session_cookie_value_123456"),
        ("WINDOWS_USER_PATH", r"Traceback in C:\Users\iwadev\dev\iwa.dev.fcbuild\src\app.py"),
        ("UNC_PATH", r"Backup target \\oluso.ojuidc.olutech.systems\OJUIDC\FC27-Recovery"),
        ("PRIVATE_IP_ADDRESS", "Connected to internal host 192.168.10.45 on port 5432"),
        ("DISCORD_SNOWFLAKE", "Discord guild 1234567890123456789 channel 987654321098765432"),
        ("PLAYER_REFERENCE", "Player identifier plr_ecf_0123456789abcdef0123456789abcdef in failure output"),
        ("RAW_PROVIDER_FIELD", '{"personaName": "SecretGamerTag99", "blazeId": "11223344"}'),
    ],
)
def test_9_to_21_sanitizer_detects_all_forbidden_categories(
    category: str,
    sample_payload: str,
) -> None:
    res = scan_text_content(sample_payload, source_label="unit-test")
    assert res["status"] == "FAIL"
    assert category in res["categories"]

    # Also verify sanitize_text_for_report scrubs the forbidden pattern so a re-scan passes
    scrubbed = sanitize_text_for_report(sample_payload)
    res_after = scan_text_content(scrubbed, source_label="unit-test-scrubbed")
    assert res_after["status"] == "PASS", f"Failed to scrub {category}: {res_after}"


def test_22_sanitizer_fails_closed_without_printing_matched_secret_value(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret_marker = "SuperSecretValueThatMustNeverBePrinted987654"
    dirty_file = tmp_path / "dirty.md"
    dirty_file.write_text(
        f"Connection failed: postgresql://fc27_app:{secret_marker}@localhost:5432/fc27\n",
        encoding="utf-8",
    )
    json_report = tmp_path / "scan.json"
    rc = sanitize_cli_main([str(dirty_file), "--json-out", str(json_report)])
    captured = capsys.readouterr()

    assert rc == 1
    assert "SANITIZATION_STATUS=FAIL" in captured.out
    assert "POSTGRESQL_URI" in captured.out
    assert secret_marker not in captured.out
    assert secret_marker not in captured.err
    assert secret_marker not in json_report.read_text(encoding="utf-8")


def test_23_and_24_report_count_reconciliation_pass_and_fail_closed() -> None:
    counts = {"collected": 10, "passed": 8, "failed": 1, "skipped": 1}
    res = reconcile_report_counts(
        junit_counts=counts,
        html_counts=dict(counts),
        summary_counts=dict(counts),
    )
    assert res["status"] == "PASS"

    # Mismatched HTML count must fail closed
    with pytest.raises(ValueError, match="Report count reconciliation failed"):
        reconcile_report_counts(
            junit_counts=counts,
            html_counts={"collected": 10, "passed": 9, "failed": 0, "skipped": 1},
            summary_counts=counts,
        )

    # Mismatched internal sum (passed + failed + skipped != collected) must fail closed
    bad_internal = {"collected": 10, "passed": 8, "failed": 0, "skipped": 1}
    with pytest.raises(ValueError, match="Report count reconciliation failed"):
        reconcile_report_counts(
            junit_counts=bad_internal,
            html_counts=bad_internal,
            summary_counts=bad_internal,
        )


def test_25_untrusted_fork_cannot_trigger_github_pages_deployment() -> None:
    fork_ctx = evaluate_trusted_context(
        event_name="pull_request",
        ref="refs/pull/10/merge",
        repository="iwa-ojuidc/iwa.dev.fcbuild",
        head_repo_full_name="attacker-fork/iwa.dev.fcbuild",
        head_repo_fork=True,
        enable_pages_var="true",
    )
    assert fork_ctx["trusted_context"] is False
    assert fork_ctx["can_publish_pages"] is False
    assert fork_ctx["untrusted_fork_pages_publication"] == "NO"
    assert fork_ctx["pages_publication_trusted_context_only"] == "YES"
    assert fork_ctx["browser_preview"] == "JOB_SUMMARY_ONLY"

    pr_target_ctx = evaluate_trusted_context(
        event_name="pull_request_target",
        ref="refs/heads/main",
        repository="iwa-ojuidc/iwa.dev.fcbuild",
        head_repo_full_name="iwa-ojuidc/iwa.dev.fcbuild",
        head_repo_fork=False,
        enable_pages_var="true",
    )
    assert pr_target_ctx["trusted_context"] is False
    assert pr_target_ctx["can_publish_pages"] is False


def test_26_pages_unavailable_or_unapproved_uses_job_summary_fallback(tmp_path: Path) -> None:
    ctx = evaluate_trusted_context(
        event_name="pull_request",
        ref="refs/pull/12/merge",
        repository="iwa-ojuidc/iwa.dev.fcbuild",
        head_repo_full_name="iwa-ojuidc/iwa.dev.fcbuild",
        head_repo_fork=False,
        enable_pages_var="false",
    )
    assert ctx["trusted_context"] is True
    assert ctx["pages_approved"] is False
    assert ctx["can_publish_pages"] is False
    assert ctx["pages_security_decision"] == "NOT_APPROVED_USE_JOB_SUMMARY_ONLY"
    assert ctx["browser_preview"] == "JOB_SUMMARY_ONLY"

    raw_xml = _write_sample_junit(tmp_path / "raw.xml", passed=2, failed=0, skipped=0)
    meta = build_job_report(
        junit_inputs=[raw_xml],
        junit_out=tmp_path / "out" / "test-results" / "junit.xml",
        html_out=tmp_path / "out" / "test-report" / "index.html",
        summary_out=tmp_path / "out" / "job-summary.md",
        durations_out=tmp_path / "out" / "test-results" / "durations.json",
        meta_out=tmp_path / "out" / "job-meta.json",
        workflow="test-fast",
        test_group="quality",
        commit_sha="112233",
        browser_preview=ctx["browser_preview"],
        pages_security_decision=ctx["pages_security_decision"],
    )
    summary_md = (tmp_path / "out" / "job-summary.md").read_text(encoding="utf-8")
    assert "BROWSER_PREVIEW=JOB_SUMMARY_ONLY" in summary_md
    assert meta["report_count_reconciliation"] == "PASS"


def test_27_pages_report_retention_pruning_caps_at_30(tmp_path: Path) -> None:
    reports_root = tmp_path / "pages-site" / "reports"
    for run_num in range(1, 36):
        attempt_dir = reports_root / "test-fast" / str(run_num) / "1"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        (attempt_dir / "index.html").write_text("<html></html>", encoding="utf-8")

    info = prune_pages_report_tree(reports_root, max_runs=PAGES_REPORT_RETENTION_COUNT)
    assert info["retention_cap"] == 30
    assert info["retained_runs"] == 30
    assert info["pruned_runs"] == 5
    # Runs 1..5 pruned, 6..35 retained
    for old_run in range(1, 6):
        assert not (reports_root / "test-fast" / str(old_run)).exists()
    for kept_run in range(6, 36):
        assert (reports_root / "test-fast" / str(kept_run) / "1" / "index.html").is_file()
