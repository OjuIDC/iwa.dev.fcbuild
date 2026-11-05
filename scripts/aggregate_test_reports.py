#!/usr/bin/env python3
"""Aggregate test report builder, shard count reconciler, and Pages tree manager.

Combines per-job JUnit XML, HTML reports, and metadata JSON files across workflow
shards; verifies that aggregate counts match the exact sum of shard counts (and
optional expected full-suite collection total); enforces the 30-report retention
cap for static report trees; writes the final aggregate GitHub Actions Job Summary;
and runs the fail-closed privacy/security scanner across all outputs.
"""

from __future__ import annotations

import argparse
import html
import json
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Any, Final

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.build_test_report import (  # noqa: E402
    build_job_summary_markdown,
    extract_counts_from_html,
    extract_counts_from_summary_markdown,
    parse_junit_xml_files,
    reconcile_report_counts,
    render_self_contained_html_report,
    write_sanitized_junit_xml,
)
from scripts.sanitize_test_report import (  # noqa: E402
    sanitize_text_for_report,
    scan_paths,
)

PAGES_REPORT_RETENTION_COUNT: Final[int] = 30


def prune_pages_report_tree(
    reports_root: Path,
    *,
    max_runs: int = PAGES_REPORT_RETENTION_COUNT,
) -> dict[str, Any]:
    """Prunes oldest report run directories under reports/<workflow>/<run-number>/<run-attempt> to keep at most max_runs."""
    if not reports_root.is_dir():
        return {"retained_runs": 0, "pruned_runs": 0, "retention_cap": max_runs}

    run_dirs: list[tuple[tuple[int, int, str], Path]] = []
    for wf_dir in sorted(reports_root.iterdir()):
        if not wf_dir.is_dir():
            continue
        for run_num_dir in sorted(wf_dir.iterdir()):
            if not run_num_dir.is_dir():
                continue
            for attempt_dir in sorted(run_num_dir.iterdir()):
                if not attempt_dir.is_dir():
                    continue
                try:
                    r_num = int(run_num_dir.name)
                except ValueError:
                    r_num = 0
                try:
                    a_num = int(attempt_dir.name)
                except ValueError:
                    a_num = 0
                run_dirs.append(((r_num, a_num, wf_dir.name), attempt_dir))

    run_dirs.sort(key=lambda item: item[0], reverse=True)
    retained = run_dirs[:max_runs]
    to_prune = run_dirs[max_runs:]

    for _, dead_dir in to_prune:
        shutil.rmtree(dead_dir, ignore_errors=True)
        parent = dead_dir.parent
        if parent.is_dir() and not any(parent.iterdir()):
            shutil.rmtree(parent, ignore_errors=True)

    return {
        "retained_runs": len(retained),
        "pruned_runs": len(to_prune),
        "retention_cap": max_runs,
    }


def render_pages_index_html(
    *,
    workflow: str,
    run_number: str,
    run_attempt: str,
    commit_sha: str,
    aggregate_counts: dict[str, int],
    shard_metas: list[dict[str, Any]],
) -> str:
    """Renders a self-contained static index.html for the bounded Pages report tree."""
    rows = []
    for sm in shard_metas:
        grp = html.escape(str(sm["test_group"]))
        status = "PASS" if int(sm["failed"]) == 0 else "FAIL"
        rows.append(
            f"<tr>"
            f"<td><a href=\"{grp}/index.html\"><code>{grp}</code></a></td>"
            f"<td>{status}</td>"
            f"<td>{int(sm['collected'])}</td>"
            f"<td>{int(sm['passed'])}</td>"
            f"<td>{int(sm['failed'])}</td>"
            f"<td>{int(sm['skipped'])}</td>"
            f"<td>{float(sm['duration_seconds']):.2f}s</td>"
            f"</tr>"
        )

    counts_json = html.escape(json.dumps(aggregate_counts), quote=True)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>FC27 Static Report Index - {html.escape(workflow)} #{html.escape(run_number)}</title>
  <style>
    body {{
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #0f172a;
      color: #f8fafc;
      margin: 0;
      padding: 24px;
    }}
    .container {{ max-width: 1100px; margin: 0 auto; }}
    table {{ width: 100%; border-collapse: collapse; background: #1e293b; border: 1px solid #334155; border-radius: 8px; overflow: hidden; }}
    th, td {{ padding: 10px 14px; text-align: left; border-bottom: 1px solid #334155; }}
    th {{ color: #94a3b8; }}
    a {{ color: #38bdf8; text-decoration: none; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
  </style>
</head>
<body>
  <div class="container" data-fc27-counts="{counts_json}">
    <h1>FC27 Browser Test Report Index</h1>
    <p>
      Workflow: <code>{html.escape(workflow)}</code> |
      Run: <code>#{html.escape(run_number)} (attempt {html.escape(run_attempt)})</code> |
      Commit: <code>{html.escape(commit_sha)}</code> |
      Retention Cap: <code>{PAGES_REPORT_RETENTION_COUNT} runs</code>
    </p>
    <p><a href="aggregate/index.html">Open Unified Aggregate HTML Report</a></p>
    <table>
      <thead>
        <tr>
          <th>Job / Test Group</th>
          <th>Status</th>
          <th>Collected</th>
          <th>Passed</th>
          <th>Failed</th>
          <th>Skipped</th>
          <th>Duration</th>
        </tr>
      </thead>
      <tbody>
        {"".join(rows)}
      </tbody>
    </table>
  </div>
</body>
</html>
"""


def aggregate_reports(
    *,
    input_dirs: list[Path],
    out_dir: Path,
    workflow: str,
    commit_sha: str,
    pr_number: str = "N/A",
    run_number: str = "1",
    run_attempt: str = "1",
    python_version: str = "3.12",
    postgres_version: str = "16",
    browser_preview: str = "JOB_SUMMARY_ONLY",
    pages_security_decision: str = "NOT_APPROVED_USE_JOB_SUMMARY_ONLY",
    browser_report_url: str = "",
    expected_total_collected: int | None = None,
    step_summary_path: Path | None = None,
) -> dict[str, Any]:
    """Aggregates shard reports, reconciles counts, builds Pages tree, and scans for privacy."""
    meta_files: list[Path] = []
    junit_files: list[Path] = []
    html_by_group: dict[str, Path] = {}

    for d in input_dirs:
        if d.is_file() and d.name == "job-meta.json":
            meta_files.append(d)
        elif d.is_dir():
            for mf in sorted(d.rglob("job-meta.json")):
                meta_files.append(mf)

    if not meta_files:
        raise ValueError("No job-meta.json files found in input directories for aggregation")

    shard_metas: list[dict[str, Any]] = []
    for mf in meta_files:
        m_data = json.loads(mf.read_text(encoding="utf-8"))
        shard_metas.append(m_data)
        base_dir = mf.parent
        j_cand = base_dir / "test-results" / "junit.xml"
        if not j_cand.is_file():
            j_cand = base_dir / "junit.xml"
        if not j_cand.is_file():
            raise FileNotFoundError(f"Missing junit.xml alongside {mf.name}")
        junit_files.append(j_cand)

        h_cand = base_dir / "test-report" / "index.html"
        if not h_cand.is_file():
            h_cand = base_dir / "index.html"
        if h_cand.is_file():
            html_by_group[str(m_data["test_group"])] = h_cand

    shard_metas.sort(key=lambda x: str(x["test_group"]))
    parsed_union = parse_junit_xml_files(junit_files)

    sum_collected = sum(int(m["collected"]) for m in shard_metas)
    sum_passed = sum(int(m["passed"]) for m in shard_metas)
    sum_failed = sum(int(m["failed"]) for m in shard_metas)
    sum_skipped = sum(int(m["skipped"]) for m in shard_metas)

    if (
        parsed_union["collected"] != sum_collected
        or parsed_union["passed"] != sum_passed
        or parsed_union["failed"] != sum_failed
        or parsed_union["skipped"] != sum_skipped
    ):
        raise ValueError(
            "Aggregate JUnit counts do not match sum of shard metadata counts: "
            f"junit=({parsed_union['collected']},{parsed_union['passed']},{parsed_union['failed']},{parsed_union['skipped']}) "
            f"shards=({sum_collected},{sum_passed},{sum_failed},{sum_skipped})"
        )

    if expected_total_collected is not None and sum_collected != int(expected_total_collected):
        raise ValueError(
            f"Shard union collection mismatch: sum_collected={sum_collected} != expected_total_collected={expected_total_collected}"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    agg_junit_out = out_dir / "test-results" / "junit.xml"
    agg_durations_out = out_dir / "test-results" / "durations.json"
    agg_html_out = out_dir / "test-report" / "index.html"
    agg_summary_out = out_dir / "job-summary.md"
    agg_meta_out = out_dir / "aggregate-meta.json"

    write_sanitized_junit_xml(parsed_union, agg_junit_out, suite_name=f"{workflow}:aggregate")

    agg_meta_header = {
        "workflow": sanitize_text_for_report(workflow),
        "test_group": "aggregate (" + ", ".join(str(m["test_group"]) for m in shard_metas) + ")",
        "commit_sha": sanitize_text_for_report(commit_sha),
        "pr_number": sanitize_text_for_report(pr_number),
        "python_version": sanitize_text_for_report(python_version),
        "postgres_version": sanitize_text_for_report(postgres_version),
        "artifact_name": f"{workflow}-browser-test-report",
        "browser_preview": sanitize_text_for_report(browser_preview),
        "pages_security_decision": sanitize_text_for_report(pages_security_decision),
        "browser_report_url": sanitize_text_for_report(browser_report_url),
        "junit_status": "GENERATED",
        "html_status": "GENERATED_SELF_CONTAINED",
    }

    agg_html_content = render_self_contained_html_report(
        meta=agg_meta_header,
        parsed=parsed_union,
    )
    agg_html_out.parent.mkdir(parents=True, exist_ok=True)
    agg_html_out.write_text(agg_html_content, encoding="utf-8")

    base_summary_md = build_job_summary_markdown(
        meta=agg_meta_header,
        parsed=parsed_union,
    )

    shard_lines = [
        "",
        "### Shard / Job Breakdown",
        "",
        "| Job / Test Group | Status | Collected | Passed | Failed | Skipped | Duration (s) |",
        "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
    ]
    for sm in shard_metas:
        st = "PASS" if int(sm["failed"]) == 0 else "FAIL"
        shard_lines.append(
            f"| `{sm['test_group']}` | `{st}` | `{sm['collected']}` | `{sm['passed']}` | "
            f"`{sm['failed']}` | `{sm['skipped']}` | `{float(sm['duration_seconds']):.2f}s` |"
        )
    shard_lines.append("")
    full_summary_md = sanitize_text_for_report(base_summary_md + "\n".join(shard_lines))
    agg_summary_out.write_text(full_summary_md, encoding="utf-8")

    if step_summary_path is not None:
        step_summary_path.parent.mkdir(parents=True, exist_ok=True)
        with step_summary_path.open("a", encoding="utf-8") as fh:
            fh.write(full_summary_md + "\n")

    reconcile_res = reconcile_report_counts(
        junit_counts={
            "collected": parsed_union["collected"],
            "passed": parsed_union["passed"],
            "failed": parsed_union["failed"],
            "skipped": parsed_union["skipped"],
        },
        html_counts=extract_counts_from_html(agg_html_content),
        summary_counts=extract_counts_from_summary_markdown(full_summary_md),
    )

    agg_durations_out.parent.mkdir(parents=True, exist_ok=True)
    agg_durations_out.write_text(
        json.dumps(
            {
                "workflow": agg_meta_header["workflow"],
                "commit_sha": agg_meta_header["commit_sha"],
                "duration_seconds": parsed_union["duration_seconds"],
                "slowest_tests": parsed_union["slowest_tests"],
                "shards": [
                    {
                        "test_group": m["test_group"],
                        "collected": m["collected"],
                        "passed": m["passed"],
                        "failed": m["failed"],
                        "skipped": m["skipped"],
                        "duration_seconds": m["duration_seconds"],
                    }
                    for m in shard_metas
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # Build bounded static report tree: pages-site/reports/<workflow>/<run-number>/<run-attempt>/<job-name>/index.html
    pages_site_dir = out_dir / "pages-site"
    reports_root = pages_site_dir / "reports"
    wf_slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", workflow.strip()) or "ci"
    run_dir = reports_root / wf_slug / str(run_number) / str(run_attempt)
    (run_dir / "aggregate").mkdir(parents=True, exist_ok=True)
    (run_dir / "aggregate" / "index.html").write_text(agg_html_content, encoding="utf-8")

    for sm in shard_metas:
        grp_slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(sm["test_group"]))
        grp_dir = run_dir / grp_slug
        grp_dir.mkdir(parents=True, exist_ok=True)
        src_html = html_by_group.get(str(sm["test_group"]))
        if src_html and src_html.is_file():
            shutil.copyfile(src_html, grp_dir / "index.html")
        else:
            (grp_dir / "index.html").write_text(agg_html_content, encoding="utf-8")

    index_html = render_pages_index_html(
        workflow=workflow,
        run_number=str(run_number),
        run_attempt=str(run_attempt),
        commit_sha=commit_sha,
        aggregate_counts=reconcile_res["counts"],
        shard_metas=shard_metas,
    )
    (run_dir / "index.html").write_text(index_html, encoding="utf-8")
    (reports_root / "index.html").write_text(index_html, encoding="utf-8")
    (pages_site_dir / "index.html").write_text(index_html, encoding="utf-8")

    retention_info = prune_pages_report_tree(
        reports_root,
        max_runs=PAGES_REPORT_RETENTION_COUNT,
    )

    scan_res = scan_paths([out_dir])
    if scan_res["status"] != "PASS":
        raise ValueError(
            f"Aggregate report privacy/self-contained scan failed: categories={scan_res['categories']}"
        )

    result_payload = {
        **agg_meta_header,
        "collected": parsed_union["collected"],
        "passed": parsed_union["passed"],
        "failed": parsed_union["failed"],
        "skipped": parsed_union["skipped"],
        "duration_seconds": parsed_union["duration_seconds"],
        "shards_count": len(shard_metas),
        "shards": shard_metas,
        "report_count_reconciliation": reconcile_res["status"],
        "html_report_self_contained": scan_res["html_report_self_contained"],
        "html_external_resources": scan_res["html_external_resources"],
        "html_report_privacy_scan": scan_res["html_report_privacy_scan"],
        "junit_privacy_scan": scan_res["junit_privacy_scan"],
        "summary_privacy_scan": scan_res["summary_privacy_scan"],
        "forbidden_values_published": scan_res["forbidden_values_published"],
        "pages_report_retention_count": retention_info["retention_cap"],
        "pages_retained_runs": retention_info["retained_runs"],
        "pages_pruned_runs": retention_info["pruned_runs"],
        "actions_job_summary_created": "YES",
        "actions_aggregate_summary_created": "YES",
    }
    agg_meta_out.write_text(json.dumps(result_payload, indent=2), encoding="utf-8")

    meta_scan = scan_paths([agg_meta_out])
    if meta_scan["status"] != "PASS":
        raise ValueError(
            f"Aggregate metadata scan failed: categories={meta_scan['categories']}"
        )

    return result_payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Aggregate FC27 shard reports, reconcile counts, and build sanitized static report tree."
    )
    parser.add_argument(
        "--input-dirs",
        nargs="+",
        required=True,
        help="Directories containing downloaded job report artifacts.",
    )
    parser.add_argument(
        "--out-dir",
        required=True,
        help="Output directory for unified aggregate report and pages-site tree.",
    )
    parser.add_argument(
        "--workflow",
        default=os.environ.get("GITHUB_WORKFLOW", "ci"),
    )
    parser.add_argument(
        "--commit-sha",
        default=os.environ.get("GITHUB_SHA", "local"),
    )
    parser.add_argument(
        "--pr-number",
        default=os.environ.get("FC27_PR_NUMBER", "N/A"),
    )
    parser.add_argument(
        "--run-number",
        default=os.environ.get("GITHUB_RUN_NUMBER", "1"),
    )
    parser.add_argument(
        "--run-attempt",
        default=os.environ.get("GITHUB_RUN_ATTEMPT", "1"),
    )
    parser.add_argument("--python-version", default="3.12")
    parser.add_argument("--postgres-version", default="16")
    parser.add_argument("--browser-preview", default="JOB_SUMMARY_ONLY")
    parser.add_argument(
        "--pages-security-decision",
        default="NOT_APPROVED_USE_JOB_SUMMARY_ONLY",
    )
    parser.add_argument("--browser-report-url", default="")
    parser.add_argument(
        "--expected-total-collected",
        type=int,
        default=None,
        help="Optional exact full-suite test collection count that the shard sum must equal.",
    )
    parser.add_argument(
        "--append-github-step-summary",
        action="store_true",
        help="Append aggregate Markdown summary to $GITHUB_STEP_SUMMARY when set.",
    )
    args = parser.parse_args(argv)

    step_summary_env = os.environ.get("GITHUB_STEP_SUMMARY", "")
    step_summary_path = (
        Path(step_summary_env)
        if (args.append_github_step_summary and step_summary_env)
        else None
    )

    res = aggregate_reports(
        input_dirs=[Path(p) for p in args.input_dirs],
        out_dir=Path(args.out_dir),
        workflow=args.workflow,
        commit_sha=args.commit_sha,
        pr_number=args.pr_number,
        run_number=args.run_number,
        run_attempt=args.run_attempt,
        python_version=args.python_version,
        postgres_version=args.postgres_version,
        browser_preview=args.browser_preview,
        pages_security_decision=args.pages_security_decision,
        browser_report_url=args.browser_report_url,
        expected_total_collected=args.expected_total_collected,
        step_summary_path=step_summary_path,
    )

    print(f"ACTIONS_JOB_SUMMARY_CREATED={res['actions_job_summary_created']}")
    print(f"ACTIONS_AGGREGATE_SUMMARY_CREATED={res['actions_aggregate_summary_created']}")
    print(f"REPORT_COUNT_RECONCILIATION={res['report_count_reconciliation']}")
    print(f"HTML_REPORT_SELF_CONTAINED={res['html_report_self_contained']}")
    print(f"HTML_EXTERNAL_RESOURCES={res['html_external_resources']}")
    print(f"HTML_REPORT_PRIVACY_SCAN={res['html_report_privacy_scan']}")
    print(f"JUNIT_PRIVACY_SCAN={res['junit_privacy_scan']}")
    print(f"SUMMARY_PRIVACY_SCAN={res['summary_privacy_scan']}")
    print(f"FORBIDDEN_VALUES_PUBLISHED={res['forbidden_values_published']}")
    print(f"PAGES_REPORT_RETENTION_COUNT={res['pages_report_retention_count']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
