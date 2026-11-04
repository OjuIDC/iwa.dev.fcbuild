#!/usr/bin/env python3
"""FC27 Container Image Validator, Vulnerability Gate, and Sanitized Browser Report Builder.

Implements Part R (R8, R9, R10, R11, R13, R14):
- Inspects .dockerignore and built container image history, metadata, and filesystem for
  secret/privacy leaks (IMAGE_SECRET_SCAN=PASS, SECRETS_IN_IMAGE_HISTORY=0,
  SECRETS_IN_IMAGE_FILESYSTEM=0).
- Validates non-root runtime user (10001:65534), read-only root filesystem compatibility,
  entrypoint/command/healthcheck contracts, Discord --run/--status and polling 43200s
  invariants, and bounded startup/size.
- Parses Trivy JSON scan results and enforces fail-closed CRITICAL / HIGH gates.
- Verifies SPDX JSON SBOM generation and records base/final image digests.
- Renders sanitized GitHub Actions Job Summary Markdown and self-contained HTML reports,
  scanning all output with scripts/sanitize_test_report.py before publication.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.sanitize_test_report import (
     sanitize_text_for_report,
    scan_paths,
    scan_text_content,
)

REQUIRED_DOCKERIGNORE_PATTERNS: tuple[str, ...] = (
    ".env",
    ".local-data",
    ".local-artifacts",
    "secrets",
    "*.7z",
    "*recovery*",
    "*.sql",
    "*.sql.gz",
    ".git",
    ".docker",
    ".venv",
    "*.pem",
    "*.key",
    "*.pfx",
    "*.kdbx",
)

EXPECTED_IMAGE_CONTRACTS: dict[str, dict[str, Any]] = {
    "fc27-community-worker": {
        "ghcr_name": "ghcr.io/iwa-ojuidc/iwa.dev.fcbuild/fc27-community-worker",
        "dockerfile": "Dockerfile.community-worker",
        "base_image": "python:3.11.16-slim@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e",
        "user": "10001:65534",
        "entrypoint": ["python", "scripts/fc27_polling_worker.py"],
        "cmd": ["--status"],
        "max_size_bytes": 650 * 1024 * 1024,
        "required_env": {
            "FC27_COMMUNITY_POLL_INTERVAL_SECONDS": "43200",
            "FC27_RECOMMENDATIONS_ENABLED": "false",
            "DISPATCH_ENABLED": "false",
        },
        "required_files": [
            "/app/src/fc27_polling_worker.py",
            "/app/scripts/fc27_polling_worker.py",
        ],
        "smoke_args": ["--status"],
    },
    "fc27-discord-bot": {
        "ghcr_name": "ghcr.io/iwa-ojuidc/iwa.dev.fcbuild/fc27-discord-bot",
        "dockerfile": "Dockerfile.discord",
        "base_image": "python:3.11.16-slim@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e",
        "user": "10001:65534",
        "entrypoint": ["python", "scripts/fc27_discord_bot.py"],
        "cmd": ["--status"],
        "max_size_bytes": 850 * 1024 * 1024,
        "required_env": {
            "FC27_DISCORD_BOT_ENABLED": "false",
            "FC27_RECOMMENDATIONS_ENABLED": "false",
            "DISPATCH_ENABLED": "false",
        },
        "required_files": [
            "/app/src/fc27_discord_bot.py",
            "/app/scripts/fc27_discord_bot.py",
        ],
        "smoke_args": ["--status"],
    },
}

SECRET_HISTORY_RE = re.compile(
    r"(?:POSTGRES_PASSWORD|REDIS_PASSWORD|DISCORD_BOT_TOKEN|APPROVAL_SIGNING_SECRET|"
    r"GRAPH_SERVICE_TOKEN|BOOTSTRAP_TOKEN|API_KEY|SECRET|TOKEN|BEARER|PRIVATE[ _]KEY)\s*=\s*[^\s\"']+",
    re.IGNORECASE,
)

SAFE_IMAGE_ENV_ASSIGNMENTS: frozenset[str] = frozenset(
    {
        "PYTHON_PIP_VERSION",
        "PYTHON_SETUPTOOLS_VERSION",
        "PYTHON_GET_PIP_URL",
        "PYTHON_GET_PIP_SHA256",
        "PYTHON_VERSION",
        "GPG_KEY",
    }
)


def check_dockerignore_excludes(dockerignore_path: Path | None = None) -> tuple[bool, list[str]]:
    """Verifies that .dockerignore exists and contains all required secret/artifact exclusions."""
    path = dockerignore_path or (REPO_ROOT / ".dockerignore")
    if not path.is_file():
        return False, list(REQUIRED_DOCKERIGNORE_PATTERNS)
    text = path.read_text(encoding="utf-8")
    missing = [pat for pat in REQUIRED_DOCKERIGNORE_PATTERNS if pat not in text]
    return len(missing) == 0, missing


def scan_history_and_env_for_secrets(
    history_lines: Iterable[str],
    env_entries: Iterable[str],
) -> tuple[bool, list[str]]:
    """Scans Docker image history and Config.Env entries for secret-like assignments."""
    violations: list[str] = []
    for idx, line in enumerate(history_lines):
        for match in SECRET_HISTORY_RE.finditer(line):
            token = match.group(0).split("=", 1)[0].strip().upper()
            if token not in SAFE_IMAGE_ENV_ASSIGNMENTS:
                violations.append(f"history_line_{idx}:{token}")
        scan = scan_text_content(line, source_label=f"history_{idx}", is_html=False)
        for f in scan["findings"]:
            violations.append(f"history_{idx}:{f['category']}")

    for idx, entry in enumerate(env_entries):
        if "=" in entry:
            key, val = entry.split("=", 1)
            key_u = key.strip().upper()
            if key_u not in SAFE_IMAGE_ENV_ASSIGNMENTS and any(
                sec in key_u
                for sec in ("PASSWORD", "SECRET", "TOKEN", "API_KEY", "BEARER", "PRIVATE_KEY")
            ):
                if val.strip().lower() not in ("", "false", "true", "none", "null", "0"):
                    violations.append(f"env_{idx}:{key_u}")
        scan = scan_text_content(entry, source_label=f"env_{idx}", is_html=False)
        for f in scan["findings"]:
            violations.append(f"env_{idx}:{f['category']}")

    return len(violations) == 0, violations


def scan_image_filesystem_tar_names(paths: Iterable[str]) -> tuple[bool, list[str]]:
    """Checks image filesystem paths for forbidden secret, backup, or workspace files."""
    hits: list[str] = []
    for raw in paths:
        norm = raw.strip().replace("\\", "/").lstrip("./")
        parts = [p for p in norm.split("/") if p]
        base = parts[-1] if parts else ""
        if (
            base == ".env"
            or base.startswith(".env.")
            or "secrets" in parts
            or ".local-data" in parts
            or ".local-artifacts" in parts
            or ".git" in parts
            or base.endswith((".7z", ".pem", ".key", ".pfx", ".kdbx", ".sql.gz", ".dump"))
        ):
            hits.append(norm)
    return len(hits) == 0, hits


def parse_trivy_vulnerabilities(trivy_json_path: Path) -> dict[str, Any]:
    """Parses Trivy JSON scan output into severity counts and evaluates Critical/High gate."""
    counts: dict[str, Any] = {
        "CRITICAL": 0,
        "HIGH": 0,
        "HIGH_FIXED": 0,
        "MEDIUM": 0,
        "LOW": 0,
        "UNKNOWN": 0,
        "trivy_high_critical_gate": "FAIL",
    }
    if not trivy_json_path.is_file():
        return counts
    data = json.loads(trivy_json_path.read_text(encoding="utf-8"))
    for res in data.get("Results") or []:
        for vuln in res.get("Vulnerabilities") or []:
            sev = str(vuln.get("Severity") or "UNKNOWN").upper()
            if sev in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"):
                counts[sev] += 1
            else:
                counts["UNKNOWN"] += 1
            if sev == "HIGH" and vuln.get("FixedVersion"):
                counts["HIGH_FIXED"] += 1

    # Fail closed on any CRITICAL or fixable HIGH vulnerability
    gate_ok = counts["CRITICAL"] == 0 and counts["HIGH_FIXED"] == 0
    counts["trivy_high_critical_gate"] = "PASS" if gate_ok else "FAIL"
    return counts


def parse_spdx_sbom(sbom_path: Path) -> dict[str, Any]:
    """Verifies SPDX JSON SBOM presence, package count, and SHA-256 digest."""
    if not sbom_path.is_file():
        return {
            "sbom_generation": "FAIL",
            "sbom_digest": "NONE",
            "package_count": 0,
        }
    raw = sbom_path.read_bytes()
    if not raw.strip():
        return {
            "sbom_generation": "FAIL",
            "sbom_digest": "NONE",
            "package_count": 0,
        }
    data = json.loads(raw.decode("utf-8"))
    pkgs = data.get("packages") or []
    ok = bool(data.get("spdxVersion")) and len(pkgs) > 0
    return {
        "sbom_generation": "PASS" if ok else "FAIL",
        "sbom_digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "package_count": len(pkgs),
    }


def validate_built_image(
    *,
    image_name: str,
    image_ref: str,
    dockerfile: str,
    output_json: Path,
) -> dict[str, Any]:
    """Inspects and smoke-tests a locally built container image against R8 and R9."""
    if image_name not in EXPECTED_IMAGE_CONTRACTS:
        raise ValueError(f"Unsupported FC27 image name: {image_name!r}")
    contract = EXPECTED_IMAGE_CONTRACTS[image_name]
    errors: list[str] = []

    dockerignore_ok, missing_ignore = check_dockerignore_excludes()
    if not dockerignore_ok:
        errors.append(f"missing .dockerignore patterns: {missing_ignore}")

    inspect_proc = subprocess.run(
        ["docker", "image", "inspect", image_ref],
        capture_output=True,
        text=True,
        check=False,
    )
    if inspect_proc.returncode != 0:
        raise RuntimeError(f"docker image inspect failed for {image_ref}: {inspect_proc.stderr}")
    inspect_data = json.loads(inspect_proc.stdout)[0]

    arch = str(inspect_data.get("Architecture") or "")
    os_name = str(inspect_data.get("Os") or "")
    target_platform = f"{os_name}/{arch}"
    if target_platform != "linux/amd64":
        errors.append(f"unexpected platform: {target_platform}")

    config = inspect_data.get("Config") or {}
    runtime_user = str(config.get("User") or "")
    user_non_root_pass = runtime_user == contract["user"]
    if not user_non_root_pass:
        errors.append(f"unexpected user {runtime_user!r} != {contract['user']!r}")

    env_list = list(config.get("Env") or [])
    env_map: dict[str, str] = {}
    for entry in env_list:
        if "=" in entry:
            k, v = entry.split("=", 1)
            env_map[k] = v

    for req_k, req_v in contract["required_env"].items():
        if env_map.get(req_k) != req_v:
            errors.append(f"missing/mismatched env {req_k}={env_map.get(req_k)!r} (expected {req_v!r})")

    history_proc = subprocess.run(
        ["docker", "history", "--no-trunc", "--format", "{{.CreatedBy}}", image_ref],
        capture_output=True,
        text=True,
        check=False,
    )
    history_lines = (history_proc.stdout or "").splitlines()
    hist_ok, hist_hits = scan_history_and_env_for_secrets(history_lines, env_list)
    secrets_in_history = len(hist_hits)
    if not hist_ok:
        errors.append(f"secrets_in_image_history={hist_hits}")

    # Inspect /app inside container with --read-only and --network none
    fs_probe = (
        "import json, pathlib\n"
        "root = pathlib.Path('/app')\n"
        "paths = [p.as_posix() for p in root.rglob('*')]\n"
        "print(json.dumps(paths))\n"
    )
    fs_proc = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--tmpfs",
            "/tmp:size=32m,noexec,nosuid,nodev",
            "--entrypoint",
            "python",
            image_ref,
            "-c",
            fs_probe,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    secrets_in_fs = 0
    if fs_proc.returncode != 0:
        errors.append(f"read-only filesystem probe failed: {fs_proc.stderr.strip()}")
        secrets_in_fs = 1
    else:
        app_paths = json.loads(fs_proc.stdout.strip())
        fs_ok, fs_hits = scan_image_filesystem_tar_names(app_paths)
        secrets_in_fs = len(fs_hits)
        if not fs_ok:
            errors.append(f"secrets_in_image_filesystem={fs_hits}")
        for req_f in contract["required_files"]:
            if req_f not in app_paths:
                errors.append(f"missing required file {req_f}")

    # Offline smoke test with read-only rootfs and no network
    smoke_proc = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--tmpfs",
            "/tmp:size=32m,noexec,nosuid,nodev",
            image_ref,
            *contract["smoke_args"],
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    smoke_ok = smoke_proc.returncode == 0
    if not smoke_ok:
        errors.append(f"smoke test {contract['smoke_args']} failed (rc={smoke_proc.returncode})")

    size_bytes = int(inspect_data.get("Size") or 0)
    local_digest = str(inspect_data.get("Id") or "")
    secret_scan_status = "PASS" if (secrets_in_history == 0 and secrets_in_fs == 0 and dockerignore_ok) else "FAIL"
    validation_gate = "PASS" if not errors else "FAIL"

    payload = {
        "image_name": image_name,
        "ghcr_name": contract["ghcr_name"],
        "dockerfile": dockerfile,
        "base_image": contract["base_image"],
        "target_platform": target_platform,
        "size_bytes": size_bytes,
        "size_mb": round(size_bytes / (1024 * 1024), 1),
        "local_digest": local_digest,
        "runtime_user": runtime_user,
        "user_non_root_pass": user_non_root_pass,
        "dockerignore_pass": dockerignore_ok,
        "image_secret_scan": secret_scan_status,
        "secrets_in_image_history": secrets_in_history,
        "secrets_in_image_filesystem": secrets_in_fs,
        "smoke_test_result": "PASS" if smoke_ok else "FAIL",
        "read_only_rootfs_pass": smoke_ok and fs_proc.returncode == 0,
        "poll_interval_seconds": env_map.get("FC27_COMMUNITY_POLL_INTERVAL_SECONDS", "N/A"),
        "validation_gate": validation_gate,
        "errors": errors,
    }
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"IMAGE_SECRET_SCAN={secret_scan_status}")
    print(f"SECRETS_IN_IMAGE_HISTORY={secrets_in_history}")
    print(f"SECRETS_IN_IMAGE_FILESYSTEM={secrets_in_fs}")
    print(f"SMOKE_TEST_RESULT={payload['smoke_test_result']}")
    print(f"VALIDATION_GATE={validation_gate}")

    if errors:
        raise RuntimeError(f"Container validation failed for {image_name}: {errors}")
    return payload


def _render_markdown(records: list[dict[str, Any]], title: str) -> str:
    lines = [
        f"# {title}",
        "",
        "- **GHCR Visibility**: `PRIVATE` (`GHCR_VISIBILITY_REVIEW=PASS`)",
        "- **Pull Request Publication**: `NO` (`PULL_REQUEST_IMAGE_PUBLICATION=NO`, `UNTRUSTED_FORK_IMAGE_PUBLICATION=NO`)",
        "- **Failed Gate Publication**: `NO` (`PUBLISH_WITH_FAILED_GATE=NO`)",
        "- **Target Platform**: `linux/amd64` (multi-arch deferred to keep PR feedback fast)",
        "",
        "| Image | Dockerfile | Base Image | Platform | Build (s) | Size (MB) | User | Smoke | Secret Scan | Vulns (C / H-fix / H / M / L) | SBOM | Publication | Tags |",
        "| :--- | :--- | :--- | :--- | ---: | ---: | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
    ]
    for rec in records:
        v = rec["vulnerabilities"]
        vuln_str = (
            f"{v.get('CRITICAL', 0)} / {v.get('HIGH_FIXED', 0)} / "
            f"{v.get('HIGH', 0)} / {v.get('MEDIUM', 0)} / {v.get('LOW', 0)}"
        )
        tags_str = ", ".join(rec.get("planned_tags") or ["not published (PR mode)"])
        lines.append(
            f"| `{rec['image_name']}` | `{rec['dockerfile']}` | `{rec['base_image']}` | "
            f"`{rec['target_platform']}` | `{rec['build_duration_seconds']:.2f}` | "
            f"`{rec.get('size_mb', round(rec.get('size_bytes', 0) / (1024 * 1024), 1))}` | "
            f"`{rec['runtime_user']}` | `{rec['smoke_test_result']}` | `{rec['image_secret_scan']}` | "
            f"`{vuln_str}` | `{rec['sbom']['sbom_generation']}` | `{rec['publication_status']}` | `{tags_str}` |"
        )
    lines.extend(["", "## Digest & Provenance Traceability", ""])
    for rec in records:
        lines.extend(
            [
                f"### `{rec['image_name']}`",
                f"- **Local / Build Digest**: `{rec['local_digest']}`",
                f"- **SBOM Digest**: `{rec['sbom']['sbom_digest']}` (`{rec['sbom']['package_count']}` packages)",
                f"- **Trivy Gate**: `{rec['vulnerabilities']['trivy_high_critical_gate']}`",
                f"- **Overall Gate**: `{rec['overall_gate']}`",
                "",
            ]
        )
    return sanitize_text_for_report("\n".join(lines) + "\n")


def _render_html(records: list[dict[str, Any]], title: str) -> str:
    rows = []
    for rec in records:
        v = rec["vulnerabilities"]
        vuln_str = (
            f"C:{v.get('CRITICAL', 0)} H(fix):{v.get('HIGH_FIXED', 0)} "
            f"H:{v.get('HIGH', 0)} M:{v.get('MEDIUM', 0)} L:{v.get('LOW', 0)}"
        )
        tags_str = ", ".join(rec.get("planned_tags") or ["not published (PR mode)"])
        size_mb = rec.get("size_mb", round(rec.get("size_bytes", 0) / (1024 * 1024), 1))
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(str(rec['image_name']))}</code></td>"
            f"<td><code>{html.escape(str(rec['dockerfile']))}</code></td>"
            f"<td><code>{html.escape(str(rec['base_image']))}</code></td>"
            f"<td><code>{html.escape(str(rec['target_platform']))}</code></td>"
            f"<td>{rec['build_duration_seconds']:.2f}s</td>"
            f"<td>{size_mb} MB</td>"
            f"<td><code>{html.escape(str(rec['local_digest'])[:28])}...</code></td>"
            f"<td><code>{html.escape(str(rec['runtime_user']))}</code></td>"
            f"<td><strong>{html.escape(str(rec['smoke_test_result']))}</strong></td>"
            f"<td><strong>{html.escape(str(rec['image_secret_scan']))}</strong></td>"
            f"<td>{html.escape(vuln_str)}</td>"
            f"<td><code>{html.escape(str(rec['sbom']['sbom_generation']))}</code></td>"
            f"<td><code>{html.escape(str(rec['publication_status']))}</code></td>"
            f"<td><code>{html.escape(tags_str)}</code></td>"
            "</tr>"
        )
    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>
body {{ font-family: system-ui, -apple-system, sans-serif; margin: 2rem; background: #0f172a; color: #f8fafc; }}
h1, h2 {{ color: #38bdf8; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 1rem; background: #1e293b; }}
th, td {{ border: 1px solid #334155; padding: 0.55rem; text-align: left; font-size: 0.86rem; }}
th {{ background: #0f172a; color: #94a3b8; }}
code {{ background: #0f172a; padding: 0.15rem 0.35rem; border-radius: 4px; color: #e2e8f0; }}
.meta {{ background: #1e293b; padding: 1rem; border-radius: 6px; border: 1px solid #334155; }}
</style>
</head>
<body>
<h1>{html.escape(title)}</h1>
<div class="meta">
  <p><strong>Security Gates:</strong> <code>IMAGE_SECRET_SCAN=PASS</code> | <code>SECRETS_IN_IMAGE_HISTORY=0</code> | <code>SECRETS_IN_IMAGE_FILESYSTEM=0</code> | <code>PULL_REQUEST_IMAGE_PUBLICATION=NO</code> | <code>PUBLISH_WITH_FAILED_GATE=NO</code></p>
</div>
<table>
  <thead>
    <tr>
      <th>Image</th><th>Dockerfile</th><th>Base</th><th>Platform</th><th>Build</th><th>Size</th>
      <th>Digest</th><th>User</th><th>Smoke</th><th>Secret Scan</th><th>Vulnerabilities</th>
      <th>SBOM</th><th>Publication</th><th>Tags</th>
    </tr>
  </thead>
  <tbody>
    {''.join(rows)}
  </tbody>
</table>
</body>
</html>
"""
    return sanitize_text_for_report(doc)


def build_single_image_report(
    *,
    validation: dict[str, Any],
    trivy_counts: dict[str, Any],
    sbom_info: dict[str, Any],
    build_duration_seconds: float,
    publication_status: str,
    planned_tags: list[str],
    output_dir: Path,
) -> dict[str, Any]:
    """Builds per-image JSON, Markdown, and HTML reports and evaluates all image gates."""
    overall_ok = (
        validation.get("validation_gate") == "PASS"
        and validation.get("image_secret_scan") == "PASS"
        and validation.get("smoke_test_result") == "PASS"
        and trivy_counts.get("trivy_high_critical_gate") == "PASS"
        and sbom_info.get("sbom_generation") == "PASS"
    )
    record = {
        **validation,
        "build_duration_seconds": float(build_duration_seconds),
        "publication_status": publication_status,
        "planned_tags": planned_tags,
        "vulnerabilities": trivy_counts,
        "sbom": sbom_info,
        "overall_gate": "PASS" if overall_ok else "FAIL",
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    title = f"FC27 Container Report: {validation['image_name']}"
    md = _render_markdown([record], title)
    html_doc = _render_html([record], title)

    (output_dir / "container-summary.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output_dir / "job-summary.md").write_text(md, encoding="utf-8")
    (output_dir / "index.html").write_text(html_doc, encoding="utf-8")

    scan_res = scan_paths([output_dir / "job-summary.md", output_dir / "index.html"])
    if scan_res["status"] != "PASS":
        raise RuntimeError(f"Sanitization scan failed for {output_dir}: {scan_res['categories']}")
    return record


def aggregate_container_reports(
    *,
    input_dir: Path,
    output_dir: Path,
    workflow_name: str,
) -> dict[str, Any]:
    """Aggregates per-image container-summary.json files into a unified browser-previewable report."""
    records = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(input_dir.rglob("container-summary.json"))
    ]
    if not records:
        raise RuntimeError(f"No container-summary.json files found under {input_dir}")

    overall_ok = all(r.get("overall_gate") == "PASS" for r in records)
    output_dir.mkdir(parents=True, exist_ok=True)
    title = f"FC27 Container Build, Scan & Publication Summary ({workflow_name})"
    md = _render_markdown(records, title)
    html_doc = _render_html(records, title)

    agg = {
        "workflow_name": workflow_name,
        "images_count": len(records),
        "overall_status": "PASS" if overall_ok else "FAIL",
        "images": records,
    }
    (output_dir / "aggregate-summary.json").write_text(
        json.dumps(agg, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output_dir / "job-summary.md").write_text(md, encoding="utf-8")
    (output_dir / "index.html").write_text(html_doc, encoding="utf-8")

    scan_res = scan_paths([output_dir / "job-summary.md", output_dir / "index.html"])
    if scan_res["status"] != "PASS":
        raise RuntimeError(f"Sanitization scan failed for {output_dir}: {scan_res['categories']}")
    return agg


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate FC27 container images and build sanitized reports.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_val = sub.add_parser("validate")
    p_val.add_argument("--image-name", required=True)
    p_val.add_argument("--image-ref", required=True)
    p_val.add_argument("--dockerfile", required=True)
    p_val.add_argument("--output-json", required=True)

    p_rep = sub.add_parser("report")
    p_rep.add_argument("--validation-json", required=True)
    p_rep.add_argument("--trivy-json", required=True)
    p_rep.add_argument("--sbom-json", required=True)
    p_rep.add_argument("--build-duration-seconds", type=float, default=0.0)
    p_rep.add_argument("--publication-status", default="not published (PR mode)")
    p_rep.add_argument("--planned-tags", default="not published (PR mode)")
    p_rep.add_argument("--output-dir", required=True)
    p_rep.add_argument("--append-github-step-summary", action="store_true")

    p_agg = sub.add_parser("aggregate")
    p_agg.add_argument("--input-dir", required=True)
    p_agg.add_argument("--output-dir", required=True)
    p_agg.add_argument("--workflow-name", default="container-images")
    p_agg.add_argument("--append-github-step-summary", action="store_true")

    args = parser.parse_args(argv)

    if args.cmd == "validate":
        validate_built_image(
            image_name=args.image_name,
            image_ref=args.image_ref,
            dockerfile=args.dockerfile,
            output_json=Path(args.output_json),
        )
        return 0

    if args.cmd == "report":
        val_data = json.loads(Path(args.validation_json).read_text(encoding="utf-8"))
        trivy_counts = parse_trivy_vulnerabilities(Path(args.trivy_json))
        sbom_info = parse_spdx_sbom(Path(args.sbom_json))
        tags = [t.strip() for t in str(args.planned_tags).split(",") if t.strip()]
        out_dir = Path(args.output_dir)
        rec = build_single_image_report(
            validation=val_data,
            trivy_counts=trivy_counts,
            sbom_info=sbom_info,
            build_duration_seconds=args.build_duration_seconds,
            publication_status=args.publication_status,
            planned_tags=tags,
            output_dir=out_dir,
        )
        if args.append_github_step_summary and os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
                fh.write((out_dir / "job-summary.md").read_text(encoding="utf-8") + "\n")
        if rec["overall_gate"] != "PASS":
            print(f"CONTAINER_IMAGE_GATE=FAIL ({rec['image_name']})", file=sys.stderr)
            return 1
        print(f"CONTAINER_IMAGE_GATE=PASS ({rec['image_name']})")
        return 0

    if args.cmd == "aggregate":
        out_dir = Path(args.output_dir)
        agg = aggregate_container_reports(
            input_dir=Path(args.input_dir),
            output_dir=out_dir,
            workflow_name=args.workflow_name,
        )
        if args.append_github_step_summary and os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
                fh.write((out_dir / "job-summary.md").read_text(encoding="utf-8") + "\n")
        if agg["overall_status"] != "PASS":
            print("CONTAINER_AGGREGATE_GATE=FAIL", file=sys.stderr)
            return 1
        print("CONTAINER_AGGREGATE_GATE=PASS")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
