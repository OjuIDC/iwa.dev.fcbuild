#!/usr/bin/env python3
"""Sanitized browser-previewable test report builder for FC27 CI jobs.

Merges and sanitizes JUnit XML test results, post-processes or synthesizes a
self-contained HTML report with zero external resources, generates a bounded
GitHub Actions Job Summary ($GITHUB_STEP_SUMMARY), reconciles test counts across
JUnit XML, HTML, and Job Summary (failing closed on mismatch), and runs the
fail-closed privacy/security scanner before output completion.
"""

from __future__ import annotations

import argparse
import html
import json
import os
from pathlib import Path
import platform
import re
import sys
from typing import Any, Final
import xml.etree.ElementTree as ET

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.sanitize_test_report import (  # noqa: E402
     sanitize_text_for_report,
    scan_paths,
)

MAX_SUMMARY_FAILURES: Final[int] = 25
MAX_SLOWEST_TESTS: Final[int] = 10
MAX_FAILURE_MESSAGE_CHARS: Final[int] = 400


def parse_junit_xml_files(xml_paths: list[Path]) -> dict[str, Any]:
    """Parses one or more JUnit XML files and returns sanitized aggregated metrics and testcases."""
    total_tests = 0
    total_failures = 0
    total_errors = 0
    total_skipped = 0
    total_time = 0.0
    all_cases: list[dict[str, Any]] = []
    failed_cases: list[dict[str, Any]] = []

    for xml_path in xml_paths:
        if not xml_path.is_file():
            raise FileNotFoundError(f"JUnit XML file not found: {xml_path.name}")
        tree = ET.parse(xml_path)
        root = tree.getroot()
        suites = [root] if root.tag == "testsuite" else list(root.findall(".//testsuite"))
        if not suites and root.tag == "testsuites":
            suites = [root]

        for suite in suites:
            for case in suite.findall("testcase"):
                classname = sanitize_text_for_report(case.attrib.get("classname", ""))
                name = sanitize_text_for_report(case.attrib.get("name", ""))
                file_attr = sanitize_text_for_report(case.attrib.get("file", ""))
                try:
                    duration = float(case.attrib.get("time", "0") or 0.0)
                except ValueError:
                    duration = 0.0

                total_tests += 1
                total_time += duration

                failure_el = case.find("failure")
                error_el = case.find("error")
                skipped_el = case.find("skipped")

                status = "passed"
                message = ""
                if failure_el is not None:
                    status = "failed"
                    total_failures += 1
                    raw_msg = failure_el.attrib.get("message", "") or (failure_el.text or "")
                    message = sanitize_text_for_report(raw_msg.strip())[:MAX_FAILURE_MESSAGE_CHARS]
                elif error_el is not None:
                    status = "error"
                    total_errors += 1
                    raw_msg = error_el.attrib.get("message", "") or (error_el.text or "")
                    message = sanitize_text_for_report(raw_msg.strip())[:MAX_FAILURE_MESSAGE_CHARS]
                elif skipped_el is not None:
                    status = "skipped"
                    total_skipped += 1
                    raw_msg = skipped_el.attrib.get("message", "") or (skipped_el.text or "")
                    message = sanitize_text_for_report(raw_msg.strip())[:MAX_FAILURE_MESSAGE_CHARS]

                node_id = f"{classname}::{name}" if classname else name
                record = {
                    "nodeid": node_id,
                    "classname": classname,
                    "name": name,
                    "file": file_attr,
                    "status": status,
                    "duration_seconds": round(duration, 4),
                    "message": message,
                }
                all_cases.append(record)
                if status in ("failed", "error"):
                    failed_cases.append(record)

    passed = total_tests - total_failures - total_errors - total_skipped
    failed_total = total_failures + total_errors
    slowest = sorted(
        all_cases,
        key=lambda c: (-float(c["duration_seconds"]), str(c["nodeid"])),
    )[:MAX_SLOWEST_TESTS]

    return {
        "collected": total_tests,
        "passed": passed,
        "failed": failed_total,
        "failures": total_failures,
        "errors": total_errors,
        "skipped": total_skipped,
        "duration_seconds": round(total_time, 3),
        "slowest_tests": slowest,
        "failed_tests": failed_cases[:MAX_SUMMARY_FAILURES],
        "all_cases": all_cases,
    }


def write_sanitized_junit_xml(parsed: dict[str, Any], out_path: Path, suite_name: str) -> None:
    """Writes a unified, sanitized JUnit XML file from parsed testcase records."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    suites_el = ET.Element("testsuites")
    suite_el = ET.SubElement(
        suites_el,
        "testsuite",
        {
            "name": sanitize_text_for_report(suite_name),
            "tests": str(parsed["collected"]),
            "failures": str(parsed["failures"]),
            "errors": str(parsed["errors"]),
            "skipped": str(parsed["skipped"]),
            "time": f"{parsed['duration_seconds']:.3f}",
        },
    )

    for case in parsed["all_cases"]:
        attrs = {
            "classname": str(case["classname"]),
            "name": str(case["name"]),
            "time": f"{case['duration_seconds']:.4f}",
        }
        if case.get("file"):
            attrs["file"] = str(case["file"])
        case_el = ET.SubElement(suite_el, "testcase", attrs)
        status = case["status"]
        msg = str(case.get("message") or "")
        if status == "failed":
            el = ET.SubElement(case_el, "failure", {"message": msg[:200]})
            el.text = msg
        elif status == "error":
            el = ET.SubElement(case_el, "error", {"message": msg[:200]})
            el.text = msg
        elif status == "skipped":
            ET.SubElement(case_el, "skipped", {"message": msg[:200]})

    tree = ET.ElementTree(suites_el)
    ET.indent(tree, space="  ")
    tree.write(out_path, encoding="utf-8", xml_declaration=True)


def _strip_pytest_html_environment(raw_html: str) -> str:
    """Removes pytest-html environment tables and data-jsonblob environment dumps."""
    cleaned = re.sub(
        r"<h2>Environment</h2>\s*<table[^>]*id=[\"']environment[\"'][^>]*>.*?</table>",
        "",
        raw_html,
        flags=re.IGNORECASE | re.DOTALL,
    )

    def _scrub_jsonblob(match: re.Match[str]) -> str:
        prefix = match.group(1)
        blob_escaped = match.group(2)
        suffix = match.group(3)
        try:
            unescaped = html.unescape(blob_escaped)
            data = json.loads(unescaped)
            if isinstance(data, dict):
                data["environment"] = {
                    "Platform": "sanitized-ci-runner",
                    "Sanitization": "PASS",
                }
                if "tests" in data and isinstance(data["tests"], dict):
                    for _, items in data["tests"].items():
                        if isinstance(items, list):
                            for item in items:
                                if isinstance(item, dict) and "log" in item:
                                    item["log"] = sanitize_text_for_report(str(item["log"]))
            reencoded = html.escape(json.dumps(data), quote=True)
            return f"{prefix}{reencoded}{suffix}"
        except Exception:
            return f'{prefix}{{}}{suffix}'

    cleaned = re.sub(
        r"(data-jsonblob=[\"'])(.*?)([\"'])",
        _scrub_jsonblob,
        cleaned,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # Remove any external links or images that pytest-html plugins might inject
    cleaned = re.sub(
        r"<script\b[^>]*?\bsrc\s*=\s*[\"']?\s*(?:https?:|//)[^>]*></script>",
        "",
        cleaned,
        flags=re.IGNORECASE | re.DOTALL,
    )
    cleaned = re.sub(
        r"<link\b[^>]*?\bhref\s*=\s*[\"']?\s*(?:https?:|//)[^>]*>",
        "",
        cleaned,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return sanitize_text_for_report(cleaned)


def render_self_contained_html_report(
    *,
    meta: dict[str, Any],
    parsed: dict[str, Any],
    existing_pytest_html: str | None = None,
) -> str:
    """Builds a 100% self-contained, sanitized HTML report with embedded count metadata."""
    counts = {
        "collected": int(parsed["collected"]),
        "passed": int(parsed["passed"]),
        "failed": int(parsed["failed"]),
        "skipped": int(parsed["skipped"]),
    }
    counts_json = html.escape(json.dumps(counts), quote=True)

    slowest_rows = []
    for idx, item in enumerate(parsed["slowest_tests"], start=1):
        slowest_rows.append(
            f"<tr><td>{idx}</td><td><code>{html.escape(str(item['nodeid']))}</code></td>"
            f"<td>{html.escape(str(item['status']))}</td>"
            f"<td>{float(item['duration_seconds']):.3f}s</td></tr>"
        )
    slowest_html = (
        "\n".join(slowest_rows)
        if slowest_rows
        else '<tr><td colspan="4">No tests recorded</td></tr>'
    )

    failure_rows = []
    for item in parsed["failed_tests"]:
        failure_rows.append(
            f"<tr><td><code>{html.escape(str(item['nodeid']))}</code></td>"
            f"<td>{html.escape(str(item['status']))}</td>"
            f"<td><pre>{html.escape(str(item.get('message') or ''))}</pre></td></tr>"
        )
    failures_section = ""
    if failure_rows:
        failures_section = (
            "<h2>Sanitized Failure Summary</h2>"
            "<table><thead><tr><th>Test</th><th>Status</th><th>Sanitized Message</th></tr></thead>"
            f"<tbody>{''.join(failure_rows)}</tbody></table>"
        )

    all_case_rows = []
    for item in parsed["all_cases"][:1000]:
        status_cls = f"status-{html.escape(str(item['status']))}"
        all_case_rows.append(
            f"<tr class=\"{status_cls}\"><td><code>{html.escape(str(item['nodeid']))}</code></td>"
            f"<td>{html.escape(str(item['status']).upper())}</td>"
            f"<td>{float(item['duration_seconds']):.4f}s</td></tr>"
        )

    sanitized_pytest_section = ""
    if existing_pytest_html:
        scrubbed = _strip_pytest_html_environment(existing_pytest_html)
        sanitized_pytest_section = (
            "<details><summary>Raw Pytest-HTML Self-Contained Payload (Sanitized)</summary>"
            f"<div class=\"pytest-html-embed\">{html.escape(scrubbed[:20000])}</div></details>"
        )

    status_badge = "PASS" if counts["failed"] == 0 else "FAIL"
    badge_color = "#15803d" if status_badge == "PASS" else "#b91c1c"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>FC27 CI Test Report - {html.escape(str(meta['workflow']))} / {html.escape(str(meta['test_group']))}</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #0f172a;
      --panel: #1e293b;
      --text: #f8fafc;
      --muted: #94a3b8;
      --border: #334155;
      --pass: #22c55e;
      --fail: #ef4444;
      --skip: #eab308;
    }}
    body {{
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      margin: 0;
      padding: 24px;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
    }}
    .container {{ max-width: 1200px; margin: 0 auto; }}
    .header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      background: var(--panel);
      padding: 20px 24px;
      border-radius: 8px;
      border: 1px solid var(--border);
      margin-bottom: 20px;
    }}
    .badge {{
      display: inline-block;
      padding: 6px 14px;
      border-radius: 999px;
      font-weight: 700;
      background: {badge_color};
      color: #ffffff;
    }}
    .cards {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
      margin-bottom: 24px;
    }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 16px;
    }}
    .card .label {{ color: var(--muted); font-size: 13px; text-transform: uppercase; }}
    .card .value {{ font-size: 26px; font-weight: 700; margin-top: 4px; }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
      margin-bottom: 24px;
    }}
    th, td {{
      text-align: left;
      padding: 10px 14px;
      border-bottom: 1px solid var(--border);
      font-size: 14px;
    }}
    th {{ background: rgba(255, 255, 255, 0.04); color: var(--muted); }}
    pre, code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 13px; }}
    pre {{ margin: 0; white-space: pre-wrap; word-break: break-word; }}
    .status-passed td:nth-child(2) {{ color: var(--pass); font-weight: 600; }}
    .status-failed td:nth-child(2), .status-error td:nth-child(2) {{ color: var(--fail); font-weight: 600; }}
    .status-skipped td:nth-child(2) {{ color: var(--skip); font-weight: 600; }}
  </style>
</head>
<body>
  <div class="container" id="fc27-report-root" data-fc27-counts="{counts_json}">
    <div class="header">
      <div>
        <h1 style="margin:0 0 6px 0;">FC27 Sanitized Browser Test Report</h1>
        <div style="color:var(--muted);font-size:14px;">
          Workflow: <code>{html.escape(str(meta['workflow']))}</code> |
          Test Group: <code>{html.escape(str(meta['test_group']))}</code> |
          Commit: <code>{html.escape(str(meta['commit_sha']))}</code> |
          PR: <code>{html.escape(str(meta['pr_number']))}</code>
        </div>
      </div>
      <span class="badge">{status_badge}</span>
    </div>

    <div class="cards">
      <div class="card"><div class="label">Collected</div><div class="value" id="count-collected">{counts['collected']}</div></div>
      <div class="card"><div class="label">Passed</div><div class="value" id="count-passed">{counts['passed']}</div></div>
      <div class="card"><div class="label">Failed</div><div class="value" id="count-failed">{counts['failed']}</div></div>
      <div class="card"><div class="label">Skipped</div><div class="value" id="count-skipped">{counts['skipped']}</div></div>
      <div class="card"><div class="label">Duration</div><div class="value">{float(parsed['duration_seconds']):.2f}s</div></div>
    </div>

    <h2>Execution Metadata &amp; Privacy Posture</h2>
    <table>
      <tbody>
        <tr><th>Workflow Name</th><td><code>{html.escape(str(meta['workflow']))}</code></td></tr>
        <tr><th>Test Group</th><td><code>{html.escape(str(meta['test_group']))}</code></td></tr>
        <tr><th>Commit SHA</th><td><code>{html.escape(str(meta['commit_sha']))}</code></td></tr>
        <tr><th>Pull Request</th><td><code>{html.escape(str(meta['pr_number']))}</code></td></tr>
        <tr><th>Python Version</th><td><code>{html.escape(str(meta['python_version']))}</code></td></tr>
        <tr><th>PostgreSQL Version</th><td><code>{html.escape(str(meta['postgres_version']))}</code></td></tr>
        <tr><th>Browser Preview Mode</th><td><code>{html.escape(str(meta['browser_preview']))}</code></td></tr>
        <tr><th>Pages Security Decision</th><td><code>{html.escape(str(meta['pages_security_decision']))}</code></td></tr>
        <tr><th>Self-Contained HTML</th><td><code>YES (HTML_EXTERNAL_RESOURCES=0)</code></td></tr>
      </tbody>
    </table>

    {failures_section}

    <h2>Slowest {MAX_SLOWEST_TESTS} Tests</h2>
    <table>
      <thead><tr><th>Rank</th><th>Test Node</th><th>Status</th><th>Duration</th></tr></thead>
      <tbody>
        {slowest_html}
      </tbody>
    </table>

    <h2>Test Execution Details (Bounded to 1,000 Entries)</h2>
    <table>
      <thead><tr><th>Test Node</th><th>Status</th><th>Duration</th></tr></thead>
      <tbody>
        {"".join(all_case_rows)}
      </tbody>
    </table>

    {sanitized_pytest_section}
  </div>
</body>
</html>
"""


def extract_counts_from_html(html_text: str) -> dict[str, int]:
    """Extracts canonical counts from the rendered HTML report for count reconciliation."""
    m = re.search(r'data-fc27-counts="([^"]+)"', html_text)
    if m:
        raw = html.unescape(m.group(1))
        data = json.loads(raw)
        return {
            "collected": int(data["collected"]),
            "passed": int(data["passed"]),
            "failed": int(data["failed"]),
            "skipped": int(data["skipped"]),
        }
    raise ValueError("Missing data-fc27-counts attribute in HTML report")


def extract_counts_from_summary_markdown(md_text: str) -> dict[str, int]:
    """Extracts canonical counts from the Job Summary Markdown table."""
    m = re.search(
        r"<!--\s*FC27_COUNTS:(\{.*?\})\s*-->",
        md_text,
    )
    if m:
        data = json.loads(m.group(1))
        return {
            "collected": int(data["collected"]),
            "passed": int(data["passed"]),
            "failed": int(data["failed"]),
            "skipped": int(data["skipped"]),
        }
    raise ValueError("Missing FC27_COUNTS metadata marker in Job Summary Markdown")


def reconcile_report_counts(
    *,
    junit_counts: dict[str, int],
    html_counts: dict[str, int],
    summary_counts: dict[str, int],
) -> dict[str, Any]:
    """Fails closed if JUnit XML, HTML report, and Job Summary counts do not match exactly."""
    keys = ("collected", "passed", "failed", "skipped")
    j_norm = {k: int(junit_counts[k]) for k in keys}
    h_norm = {k: int(html_counts[k]) for k in keys}
    s_norm = {k: int(summary_counts[k]) for k in keys}

    internal_sum_ok = (
        j_norm["passed"] + j_norm["failed"] + j_norm["skipped"] == j_norm["collected"]
    )
    matches = (j_norm == h_norm == s_norm) and internal_sum_ok
    if not matches:
        raise ValueError(
            f"Report count reconciliation failed: junit={j_norm} html={h_norm} summary={s_norm} internal_sum_ok={internal_sum_ok}"
        )
    return {
        "status": "PASS",
        "counts": j_norm,
    }


def build_job_summary_markdown(
    *,
    meta: dict[str, Any],
    parsed: dict[str, Any],
) -> str:
    """Builds the bounded GitHub Actions Job Summary Markdown with all required Part Q2 fields."""
    counts = {
        "collected": int(parsed["collected"]),
        "passed": int(parsed["passed"]),
        "failed": int(parsed["failed"]),
        "skipped": int(parsed["skipped"]),
    }
    counts_marker = f"<!-- FC27_COUNTS:{json.dumps(counts, sort_keys=True)} -->"
    status_str = "PASS" if counts["failed"] == 0 else "FAIL"

    browser_url = str(meta.get("browser_report_url") or "").strip()
    if browser_url and meta.get("browser_preview") == "PAGES_AND_JOB_SUMMARY":
        browser_line = f"[Open Sanitized HTML Report]({browser_url})"
    else:
        browser_line = (
            "Job Summary fallback active (`BROWSER_PREVIEW=JOB_SUMMARY_ONLY`, "
            f"`PAGES_SECURITY_DECISION={meta.get('pages_security_decision', 'NOT_APPROVED_USE_JOB_SUMMARY_ONLY')}`). "
            "Self-contained HTML (`test-report/index.html`) is attached as a workflow artifact."
        )

    lines = [
        counts_marker,
        f"## FC27 Test Report — `{meta['workflow']}` / `{meta['test_group']}` ({status_str})",
        "",
        "| Field | Value |",
        "| :--- | :--- |",
        f"| **Workflow Name** | `{meta['workflow']}` |",
        f"| **Test Group Name** | `{meta['test_group']}` |",
        f"| **Commit SHA** | `{meta['commit_sha']}` |",
        f"| **Pull Request** | `{meta['pr_number']}` |",
        f"| **Python Version** | `{meta['python_version']}` |",
        f"| **PostgreSQL Version** | `{meta['postgres_version']}` |",
        f"| **Total Collected** | `{counts['collected']}` |",
        f"| **Passed** | `{counts['passed']}` |",
        f"| **Failed** | `{counts['failed']}` |",
        f"| **Skipped** | `{counts['skipped']}` |",
        f"| **Total Duration** | `{float(parsed['duration_seconds']):.2f}s` |",
        f"| **JUnit XML Status** | `{meta.get('junit_status', 'GENERATED')}` |",
        f"| **HTML Report Status** | `{meta.get('html_status', 'GENERATED_SELF_CONTAINED')}` |",
        f"| **Artifact Instructions** | Download artifact `{meta.get('artifact_name', 'fc27-test-report')}` (retention: 7 days) and open `test-report/index.html` locally. |",
        f"| **Browser Preview** | {browser_line} |",
        "",
    ]

    if parsed["failed_tests"]:
        lines.extend(
            [
                "### Sanitized Failure Summary",
                "",
                "| Test Node | Status | Sanitized Message |",
                "| :--- | :--- | :--- |",
            ]
        )
        for item in parsed["failed_tests"]:
            msg = str(item.get("message") or "").replace("\n", " ").replace("|", "\\|")
            lines.append(f"| `{item['nodeid']}` | `{item['status']}` | `{msg}` |")
        lines.append("")

    lines.extend(
        [
            f"### Slowest {MAX_SLOWEST_TESTS} Tests",
            "",
            "| Rank | Test Node | Status | Duration (s) |",
            "| :--- | :--- | :--- | :--- |",
        ]
    )
    if parsed["slowest_tests"]:
        for idx, item in enumerate(parsed["slowest_tests"], start=1):
            lines.append(
                f"| {idx} | `{item['nodeid']}` | `{item['status']}` | `{float(item['duration_seconds']):.3f}s` |"
            )
    else:
        lines.append("| - | `no-tests-collected` | `none` | `0.000s` |")

    lines.append("")
    return sanitize_text_for_report("\n".join(lines))


def build_job_report(
    *,
    junit_inputs: list[Path],
    junit_out: Path,
    html_out: Path,
    summary_out: Path,
    durations_out: Path,
    meta_out: Path,
    workflow: str,
    test_group: str,
    commit_sha: str,
    pr_number: str = "N/A",
    python_version: str = "",
    postgres_version: str = "N/A",
    artifact_name: str = "fc27-test-report",
    browser_preview: str = "JOB_SUMMARY_ONLY",
    pages_security_decision: str = "NOT_APPROVED_USE_JOB_SUMMARY_ONLY",
    browser_report_url: str = "",
    raw_pytest_html_in: Path | None = None,
    step_summary_path: Path | None = None,
) -> dict[str, Any]:
    """Builds, reconciles, and sanitizes all report outputs for a single CI job."""
    parsed = parse_junit_xml_files(junit_inputs)
    write_sanitized_junit_xml(parsed, junit_out, suite_name=f"{workflow}:{test_group}")

    py_ver = python_version or platform.python_version()
    meta = {
        "workflow": sanitize_text_for_report(workflow),
        "test_group": sanitize_text_for_report(test_group),
        "commit_sha": sanitize_text_for_report(commit_sha or "local"),
        "pr_number": sanitize_text_for_report(pr_number or "N/A"),
        "python_version": sanitize_text_for_report(py_ver),
        "postgres_version": sanitize_text_for_report(postgres_version or "N/A"),
        "artifact_name": sanitize_text_for_report(artifact_name),
        "browser_preview": sanitize_text_for_report(browser_preview),
        "pages_security_decision": sanitize_text_for_report(pages_security_decision),
        "browser_report_url": sanitize_text_for_report(browser_report_url),
        "junit_status": "GENERATED",
        "html_status": "GENERATED_SELF_CONTAINED",
    }

    existing_html = None
    if raw_pytest_html_in and raw_pytest_html_in.is_file():
        existing_html = raw_pytest_html_in.read_text(encoding="utf-8", errors="replace")

    html_content = render_self_contained_html_report(
        meta=meta,
        parsed=parsed,
        existing_pytest_html=existing_html,
    )
    html_out.parent.mkdir(parents=True, exist_ok=True)
    html_out.write_text(html_content, encoding="utf-8")

    summary_md = build_job_summary_markdown(meta=meta, parsed=parsed)
    summary_out.parent.mkdir(parents=True, exist_ok=True)
    summary_out.write_text(summary_md, encoding="utf-8")

    if step_summary_path is not None:
        step_summary_path.parent.mkdir(parents=True, exist_ok=True)
        with step_summary_path.open("a", encoding="utf-8") as fh:
            fh.write(summary_md + "\n")

    reconcile_res = reconcile_report_counts(
        junit_counts={
            "collected": parsed["collected"],
            "passed": parsed["passed"],
            "failed": parsed["failed"],
            "skipped": parsed["skipped"],
        },
        html_counts=extract_counts_from_html(html_content),
        summary_counts=extract_counts_from_summary_markdown(summary_md),
    )

    durations_payload = {
        "workflow": meta["workflow"],
        "test_group": meta["test_group"],
        "commit_sha": meta["commit_sha"],
        "duration_seconds": parsed["duration_seconds"],
        "slowest_tests": parsed["slowest_tests"],
    }
    durations_out.parent.mkdir(parents=True, exist_ok=True)
    durations_out.write_text(json.dumps(durations_payload, indent=2), encoding="utf-8")

    scan_result = scan_paths([junit_out, html_out, summary_out, durations_out])
    if scan_result["status"] != "PASS":
        raise ValueError(
            f"Sanitization scan failed for {test_group}: categories={scan_result['categories']}"
        )

    full_meta = {
        **meta,
        "collected": parsed["collected"],
        "passed": parsed["passed"],
        "failed": parsed["failed"],
        "skipped": parsed["skipped"],
        "duration_seconds": parsed["duration_seconds"],
        "slowest_tests": parsed["slowest_tests"],
        "failed_tests": parsed["failed_tests"],
        "report_count_reconciliation": reconcile_res["status"],
        "html_report_self_contained": scan_result["html_report_self_contained"],
        "html_external_resources": scan_result["html_external_resources"],
        "html_report_privacy_scan": scan_result["html_report_privacy_scan"],
        "junit_privacy_scan": scan_result["junit_privacy_scan"],
        "summary_privacy_scan": scan_result["summary_privacy_scan"],
        "forbidden_values_published": scan_result["forbidden_values_published"],
    }
    meta_out.parent.mkdir(parents=True, exist_ok=True)
    meta_out.write_text(json.dumps(full_meta, indent=2), encoding="utf-8")

    final_scan = scan_paths([meta_out])
    if final_scan["status"] != "PASS":
        raise ValueError(
            f"Metadata sanitization scan failed: categories={final_scan['categories']}"
        )

    return full_meta


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build, reconcile, and sanitize FC27 CI job test reports."
    )
    parser.add_argument(
        "--junit-in",
        nargs="+",
        required=True,
        help="Input JUnit XML file(s) from pytest.",
    )
    parser.add_argument(
        "--junit-out",
        required=True,
        help="Output path for unified sanitized JUnit XML.",
    )
    parser.add_argument(
        "--html-out",
        required=True,
        help="Output path for self-contained sanitized HTML report.",
    )
    parser.add_argument(
        "--summary-out",
        required=True,
        help="Output path for Markdown Job Summary.",
    )
    parser.add_argument(
        "--durations-out",
        required=True,
        help="Output path for JSON durations report.",
    )
    parser.add_argument(
        "--meta-out",
        required=True,
        help="Output path for JSON job metadata.",
    )
    parser.add_argument("--raw-html-in", default="", help="Optional raw pytest-html file.")
    parser.add_argument(
        "--workflow",
        default=os.environ.get("GITHUB_WORKFLOW", "local"),
    )
    parser.add_argument("--test-group", required=True)
    parser.add_argument(
        "--commit-sha",
        default=os.environ.get("GITHUB_SHA", "local"),
    )
    parser.add_argument(
        "--pr-number",
        default=os.environ.get("FC27_PR_NUMBER", "N/A"),
    )
    parser.add_argument("--python-version", default="")
    parser.add_argument("--postgres-version", default="N/A")
    parser.add_argument("--artifact-name", default="fc27-test-report")
    parser.add_argument("--browser-preview", default="JOB_SUMMARY_ONLY")
    parser.add_argument(
        "--pages-security-decision",
        default="NOT_APPROVED_USE_JOB_SUMMARY_ONLY",
    )
    parser.add_argument("--browser-report-url", default="")
    parser.add_argument(
        "--append-github-step-summary",
        action="store_true",
        help="Append generated Markdown summary to $GITHUB_STEP_SUMMARY when set.",
    )
    args = parser.parse_args(argv)

    step_summary_env = os.environ.get("GITHUB_STEP_SUMMARY", "")
    step_summary_path = (
        Path(step_summary_env)
        if (args.append_github_step_summary and step_summary_env)
        else None
    )

    meta = build_job_report(
        junit_inputs=[Path(p) for p in args.junit_in],
        junit_out=Path(args.junit_out),
        html_out=Path(args.html_out),
        summary_out=Path(args.summary_out),
        durations_out=Path(args.durations_out),
        meta_out=Path(args.meta_out),
        workflow=args.workflow,
        test_group=args.test_group,
        commit_sha=args.commit_sha,
        pr_number=args.pr_number,
        python_version=args.python_version,
        postgres_version=args.postgres_version,
        artifact_name=args.artifact_name,
        browser_preview=args.browser_preview,
        pages_security_decision=args.pages_security_decision,
        browser_report_url=args.browser_report_url,
        raw_pytest_html_in=Path(args.raw_html_in) if args.raw_html_in else None,
        step_summary_path=step_summary_path,
    )

    print(f"REPORT_COUNT_RECONCILIATION={meta['report_count_reconciliation']}")
    print(f"HTML_REPORT_SELF_CONTAINED={meta['html_report_self_contained']}")
    print(f"HTML_EXTERNAL_RESOURCES={meta['html_external_resources']}")
    print(f"HTML_REPORT_PRIVACY_SCAN={meta['html_report_privacy_scan']}")
    print(f"JUNIT_PRIVACY_SCAN={meta['junit_privacy_scan']}")
    print(f"SUMMARY_PRIVACY_SCAN={meta['summary_privacy_scan']}")
    print(f"FORBIDDEN_VALUES_PUBLISHED={meta['forbidden_values_published']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
