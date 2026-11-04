#!/usr/bin/env python3
"""Fail-closed privacy, security, and self-contained HTML scanner for FC27 test reports.

Scans HTML reports, JUnit XML, Markdown Job Summaries, duration reports, failure
logs, Compose logs, and static report indexes before upload or publication.
Never prints matched secret or sensitive values; reports only the forbidden
category and line number.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any, Final

SAFE_PLACEHOLDER_VALUES: Final[frozenset[str]] = frozenset(
    {
        "redacted",
        "<redacted>",
        "***",
        "none",
        "null",
        "true",
        "false",
        "pass",
        "fail",
        "yes",
        "no",
        "disabled",
        "enabled",
        "not_run",
        "skipped",
    }
)

FORBIDDEN_CONTENT_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    (
        "POSTGRESQL_URI",
        re.compile(
            r"(?:postgres(?:ql)?://[^\s\"'<>]+|host=[^\s\"'<>]+\s+user=[^\s\"'<>]+\s+password=[^\s\"'<>]+)",
            re.IGNORECASE,
        ),
    ),
    (
        "REDIS_URL",
        re.compile(r"rediss?://[^\s\"'<>]+", re.IGNORECASE),
    ),
    (
        "DISCORD_TOKEN",
        re.compile(
            r"(?:\b[MN][A-Za-z\d]{23,28}\.[\w-]{6}\.[\w-]{27,38}\b|DISCORD_BOT_TOKEN\s*=\s*[^\s\"'<>]+)"
        ),
    ),
    (
        "JWT_TOKEN",
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
        ),
    ),
    (
        "API_KEY",
        re.compile(
            r"\b(?:sk-lf-[A-Za-z0-9_-]{8,}|pk-lf-[A-Za-z0-9_-]{8,}|sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})\b"
        ),
    ),
    (
        "AUTHORIZATION_HEADER",
        re.compile(
            r"\bAuthorization\s*:\s*(?:Bearer|Basic|Token|Bot)\s+[^\s\"'<>]+",
            re.IGNORECASE,
        ),
    ),
    (
        "COOKIE_HEADER",
        re.compile(
            r"\b(?:Set-)?Cookie\s*:\s*[A-Za-z0-9_.-]+=[^\s\"'<>]+",
            re.IGNORECASE,
        ),
    ),
    (
        "WINDOWS_USER_PATH",
        re.compile(
            r"(?:[A-Za-z]:[\\/]+Users[\\/]+[^\s\"'<>]+|/(?:Users|home/runner/work)/[^\s\"'<>]+)",
            re.IGNORECASE,
        ),
    ),
    (
        "UNC_PATH",
        re.compile(r"\\\\[A-Za-z0-9_.-]+\\[A-Za-z0-9_$.-]+"),
    ),
    (
        "PRIVATE_IP_ADDRESS",
        re.compile(
            r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3})\b"
        ),
    ),
    (
        "DISCORD_SNOWFLAKE",
        re.compile(r"\b\d{17,20}\b"),
    ),
    (
        "PLAYER_REFERENCE",
        re.compile(r"\bplr_(?:ecf_[0-9a-fA-F]{6,64}|synth_[A-Za-z0-9_]+)\b"),
    ),
    (
        "RAW_PROVIDER_FIELD",
        re.compile(
            r"\"(?:personaName|proName|blazeId|nucleusId|eaAccountId|gamerTag)\"\s*:\s*\"[^\"]+\"",
            re.IGNORECASE,
        ),
    ),
    (
        "ENVIRONMENT_DUMP",
        re.compile(
            r"(?:<h2>Environment</h2>|<table[^>]*id=[\"']environment[\"']|ACTIONS_RUNTIME_TOKEN\s*=|RUNNER_TRACKING_ID\s*=|GITHUB_TOKEN\s*=)",
            re.IGNORECASE,
        ),
    ),
    (
        "DATABASE_QUERY_PARAMETER",
        re.compile(
            r"(?:DETAIL:\s*Key\s*\([^)]+\)=\([^)]+\)|PARAMETERS:\s*\$1\s*=)",
            re.IGNORECASE,
        ),
    ),
    (
        "UNSANITIZED_STACK_TRACE",
        re.compile(
            r"File\s+\"(?:[A-Za-z]:\\[^\"]+|/(?:home|root|Users|var|tmp)/[^\"]+)\"",
            re.IGNORECASE,
        ),
    ),
)

SECRET_ASSIGNMENT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?P<key>password|passwd|secret|api_key|access_token|refresh_token|client_secret|signing_secret|encryption_key|bootstrap_token|service_token)\s*(?P<sep>[:=])\s*[\"']?(?P<val>[A-Za-z0-9+/=._-]{6,})[\"']?",
    re.IGNORECASE,
)

HTML_EXTERNAL_RESOURCE_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    (
        "HTML_EXTERNAL_SCRIPT",
        re.compile(
            r"<script\b[^>]*?\bsrc\s*=\s*[\"']?\s*(?:https?:|//)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "HTML_EXTERNAL_STYLESHEET",
        re.compile(
            r"(?:<link\b[^>]*?\bhref\s*=\s*[\"']?\s*(?:https?:|//)|@import\s+url\(\s*[\"']?\s*(?:https?:|//))",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "HTML_TRACKING_PIXEL",
        re.compile(
            r"<(?:img|iframe|object|embed)\b[^>]*?(?:width\s*=\s*[\"']?[01][\"']?|height\s*=\s*[\"']?[01][\"']?|display\s*:\s*none)[^>]*?\bsrc\s*=\s*[\"']?\s*(?:https?:|//|data:)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "HTML_REMOTE_IMAGE",
        re.compile(
            r"<img\b[^>]*?\bsrc\s*=\s*[\"']?\s*(?:https?:|//)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
)


def _line_number_for_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, max(0, offset)) + 1


def scan_text_content(
    content: str,
    *,
    source_label: str = "<memory>",
    is_html: bool = False,
) -> dict[str, Any]:
    """Scans text content and returns a value-free report of any forbidden findings."""
    findings: list[dict[str, Any]] = []
    external_resources = 0

    for category, pattern in FORBIDDEN_CONTENT_PATTERNS:
        for match in pattern.finditer(content):
            findings.append(
                {
                    "source": source_label,
                    "category": category,
                    "line": _line_number_for_offset(content, match.start()),
                }
            )

    for match in SECRET_ASSIGNMENT_PATTERN.finditer(content):
        val = (match.group("val") or "").strip().lower()
        if val in SAFE_PLACEHOLDER_VALUES:
            continue
        findings.append(
            {
                "source": source_label,
                "category": "PASSWORD_OR_SECRET_ASSIGNMENT",
                "line": _line_number_for_offset(content, match.start()),
            }
        )

    if is_html:
        for category, pattern in HTML_EXTERNAL_RESOURCE_PATTERNS:
            for match in pattern.finditer(content):
                external_resources += 1
                findings.append(
                    {
                        "source": source_label,
                        "category": category,
                        "line": _line_number_for_offset(content, match.start()),
                    }
                )

    categories_found = sorted({f["category"] for f in findings})
    passed = len(findings) == 0
    return {
        "source": source_label,
        "status": "PASS" if passed else "FAIL",
        "findings_count": len(findings),
        "categories": categories_found,
        "findings": findings,
        "is_html": is_html,
        "html_self_contained": (external_resources == 0) if is_html else None,
        "html_external_resources": external_resources if is_html else 0,
    }


def scan_file(path: Path, *, root: Path | None = None) -> dict[str, Any]:
    """Scans a single file on disk and returns a value-free sanitization result."""
    label = path.name
    if root is not None:
        try:
            label = path.resolve().relative_to(root.resolve()).as_posix()
        except Exception:
            label = path.name

    if not path.is_file():
        return {
            "source": label,
            "status": "FAIL",
            "findings_count": 1,
            "categories": ["FILE_NOT_FOUND"],
            "findings": [{"source": label, "category": "FILE_NOT_FOUND", "line": 0}],
            "is_html": path.suffix.lower() in (".html", ".htm"),
            "html_self_contained": False,
            "html_external_resources": 0,
        }

    text = path.read_text(encoding="utf-8", errors="replace")
    is_html = path.suffix.lower() in (".html", ".htm")
    return scan_text_content(text, source_label=label, is_html=is_html)


def sanitize_text_for_report(text: str) -> str:
    """Scrubs runtime paths, connection strings, tokens, IPs, and sensitive parameters from raw test output before report embedding."""
    if not text:
        return ""
    out = text
    # Normalize Windows user paths and absolute workspace paths to relative POSIX paths
    out = re.sub(
        r"[A-Za-z]:[\\/]+Users[\\/]+[^\\/\"'\s<>]+[\\/]+dev[\\/]+iwa\.dev\.fcbuild[\\/]*",
        "",
        out,
        flags=re.IGNORECASE,
    )
    out = re.sub(
        r"/home/runner/work/iwa\.dev\.fcbuild/iwa\.dev\.fcbuild/?",
        "",
        out,
    )
    out = re.sub(
        r"[A-Za-z]:[\\/]+Users[\\/]+[^\s\"'<>]+",
        "<redacted-windows-path>",
        out,
        flags=re.IGNORECASE,
    )
    out = re.sub(
        r"/(?:Users|home/runner/work|root)/[^\s\"'<>]+",
        "<redacted-host-path>",
        out,
    )
    out = re.sub(
        r"\\\\[A-Za-z0-9_.-]+\\[A-Za-z0-9_$.-]+",
        "<redacted-unc-path>",
        out,
    )
    out = re.sub(
        r"File\s+\"(?:[A-Za-z]:\\[^\"]+|/(?:home|root|Users|var|tmp)/[^\"]+)\"",
        'File "<repo-relative>"',
        out,
        flags=re.IGNORECASE,
    )
    out = re.sub(
        r"postgres(?:ql)?://[^\s\"'<>]+",
        "<redacted-postgres-uri>",
        out,
        flags=re.IGNORECASE,
    )
    out = re.sub(
        r"host=[^\s\"'<>]+\s+user=[^\s\"'<>]+\s+password=[^\s\"'<>]+",
        "<redacted-postgres-conninfo>",
        out,
        flags=re.IGNORECASE,
    )
    out = re.sub(
        r"rediss?://[^\s\"'<>]+",
        "<redacted-redis-url>",
        out,
        flags=re.IGNORECASE,
    )
    out = re.sub(
        r"\b[MN][A-Za-z\d]{23,28}\.[\w-]{6}\.[\w-]{27,38}\b",
        "<redacted-discord-token>",
        out,
    )
    out = re.sub(
        r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b",
        "<redacted-jwt>",
        out,
    )
    out = re.sub(
        r"\b(?:sk-lf-[A-Za-z0-9_-]{8,}|pk-lf-[A-Za-z0-9_-]{8,}|sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})\b",
        "<redacted-api-key>",
        out,
    )
    out = re.sub(
        r"\bAuthorization\s*:\s*(?:Bearer|Basic|Token|Bot)\s+[^\s\"'<>]+",
        "Authorization: <redacted>",
        out,
        flags=re.IGNORECASE,
    )
    out = re.sub(
        r"\b(?:Set-)?Cookie\s*:\s*[A-Za-z0-9_.-]+=[^\s\"'<>]+",
        "Cookie: <redacted>",
        out,
        flags=re.IGNORECASE,
    )
    out = re.sub(
        r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3})\b",
        "<redacted-private-ip>",
        out,
    )
    out = re.sub(r"\b\d{17,20}\b", "<redacted-snowflake>", out)
    out = re.sub(
        r"\bplr_(?:ecf_[0-9a-fA-F]{6,64}|synth_[A-Za-z0-9_]+)\b",
        "<redacted-player-ref>",
        out,
    )
    out = re.sub(
        r"\"(?:personaName|proName|blazeId|nucleusId|eaAccountId|gamerTag)\"\s*:\s*\"[^\"]+\"",
        '"<redacted-provider-field>": "<redacted>"',
        out,
        flags=re.IGNORECASE,
    )
    out = re.sub(
        r"DETAIL:\s*Key\s*\([^)]+\)=\([^)]+\)",
        "DETAIL: <redacted-db-key>",
        out,
        flags=re.IGNORECASE,
    )
    out = re.sub(
        r"PARAMETERS:\s*\$1\s*=[^\n]*",
        "PARAMETERS: <redacted-db-params>",
        out,
        flags=re.IGNORECASE,
    )
    out = SECRET_ASSIGNMENT_PATTERN.sub(
        lambda m: (
            m.group(0)
            if (m.group("val") or "").strip().lower() in SAFE_PLACEHOLDER_VALUES
            else f"{m.group('key')}{m.group('sep')}<redacted>"
        ),
        out,
    )
    return out


def scan_paths(paths: list[Path], *, root: Path | None = None) -> dict[str, Any]:
    """Scans multiple files or directories and returns an aggregate value-free report."""
    expanded: list[Path] = []
    for p in paths:
        if p.is_dir():
            for child in sorted(p.rglob("*")):
                if child.is_file() and child.suffix.lower() in (
                    ".html",
                    ".htm",
                    ".xml",
                    ".md",
                    ".json",
                    ".txt",
                    ".log",
                ):
                    expanded.append(child)
        else:
            expanded.append(p)

    file_results = [scan_file(f, root=root) for f in expanded]
    all_findings = [
        finding for res in file_results for finding in res["findings"]
    ]
    all_categories = sorted({f["category"] for f in all_findings})
    html_results = [r for r in file_results if r["is_html"]]
    xml_results = [
        r for r in file_results if str(r["source"]).lower().endswith(".xml")
    ]
    md_results = [
        r for r in file_results if str(r["source"]).lower().endswith(".md")
    ]

    html_external_resources = sum(r["html_external_resources"] for r in html_results)
    html_self_contained = all(bool(r["html_self_contained"]) for r in html_results)
    overall_pass = len(all_findings) == 0 and len(expanded) > 0

    return {
        "status": "PASS" if overall_pass else "FAIL",
        "scanned_files_count": len(expanded),
        "findings_count": len(all_findings),
        "categories": all_categories,
        "findings": all_findings,
        "html_report_self_contained": "YES" if html_self_contained else "NO",
        "html_external_resources": html_external_resources,
        "html_report_privacy_scan": (
            "PASS"
            if all(r["status"] == "PASS" for r in html_results)
            else "FAIL"
        )
        if html_results
        else "NOT_RUN",
        "junit_privacy_scan": (
            "PASS"
            if all(r["status"] == "PASS" for r in xml_results)
            else "FAIL"
        )
        if xml_results
        else "NOT_RUN",
        "summary_privacy_scan": (
            "PASS"
            if all(r["status"] == "PASS" for r in md_results)
            else "FAIL"
        )
        if md_results
        else "NOT_RUN",
        "forbidden_values_published": 0,
        "files": file_results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan FC27 CI reports (HTML, JUnit XML, Markdown, JSON, logs) for forbidden values."
    )
    parser.add_argument(
        "paths",
        nargs="+",
        help="Files or directories to scan.",
    )
    parser.add_argument(
        "--json-out",
        default="",
        help="Optional path to write value-free JSON scan report.",
    )
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[1]
    target_paths = [Path(p) for p in args.paths]
    report = scan_paths(target_paths, root=repo_root)

    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"SANITIZATION_STATUS={report['status']}")
    print(f"SCANNED_FILES={report['scanned_files_count']}")
    print(f"FINDINGS_COUNT={report['findings_count']}")
    print(f"HTML_REPORT_SELF_CONTAINED={report['html_report_self_contained']}")
    print(f"HTML_EXTERNAL_RESOURCES={report['html_external_resources']}")
    print(f"HTML_REPORT_PRIVACY_SCAN={report['html_report_privacy_scan']}")
    print(f"JUNIT_PRIVACY_SCAN={report['junit_privacy_scan']}")
    print(f"SUMMARY_PRIVACY_SCAN={report['summary_privacy_scan']}")
    print(f"FORBIDDEN_VALUES_PUBLISHED={report['forbidden_values_published']}")
    if report["categories"]:
        print(f"FORBIDDEN_CATEGORIES={','.join(report['categories'])}")

    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
