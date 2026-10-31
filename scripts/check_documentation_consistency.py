#!/usr/bin/env python3
"""Deterministic Documentation Consistency Validator (fc27-documentation-consistency-v1).

Performs offline, zero-network, zero-DNS, zero-database, zero-Docker static
validation of repository documentation against implemented source code,
migrations, Docker Compose files, feature flags, and archive manifests.
"""

from __future__ import annotations

import argparse
import dataclasses
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any
import yaml

POLICY_VERSION = "fc27-documentation-consistency-v1"

VALID_CATEGORIES = frozenset(
    {
        "PATH_REFERENCE",
        "MARKDOWN_LINK",
        "SCRIPT_REFERENCE",
        "TEST_REFERENCE",
        "COMPOSE_REFERENCE",
        "FEATURE_FLAG_REFERENCE",
        "ENVIRONMENT_REFERENCE",
        "MIGRATION_REFERENCE",
        "ENDPOINT_REFERENCE",
        "SECURITY_INVARIANT",
        "ARCHITECTURE_STATUS",
        "ARCHIVE_INTEGRITY",
        "DOCUMENT_AUTHORITY",
        "TACTICAL_CLAIM",
        "COMMAND_EXAMPLE",
        "SOURCE_OF_TRUTH",
        "SECRET_HYGIENE",
        "REAL_DATA_HYGIENE",
    }
)

VALID_SEVERITIES = ("ERROR", "WARNING", "INFO")
SEVERITY_ORDER = {"ERROR": 0, "WARNING": 1, "INFO": 2}


class ValidatorConfigError(Exception):
    """Raised when policy, waiver, or CLI configuration is invalid (exit code 2)."""


@dataclasses.dataclass(frozen=True)
class Finding:
    policy_version: str
    category: str
    reason_code: str
    severity: str
    document_path: str
    line_number: int | None
    referenced_value: str
    safe_message: str
    suggested_action: str
    authoritative_source: str
    archive_status: str
    waived: bool = False
    waiver_reference: str | None = None

    def sort_key(self) -> tuple[int, str, str, int, str, str]:
        return (
            SEVERITY_ORDER.get(self.severity, 99),
            self.category,
            self.document_path,
            self.line_number if self.line_number is not None else 0,
            self.reason_code,
            self.referenced_value,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "category": self.category,
            "reason_code": self.reason_code,
            "severity": self.severity,
            "document_path": self.document_path,
            "line_number": self.line_number,
            "referenced_value": self.referenced_value,
            "safe_message": self.safe_message,
            "suggested_action": self.suggested_action,
            "authoritative_source": self.authoritative_source,
            "archive_status": self.archive_status,
            "waived": self.waived,
            "waiver_reference": self.waiver_reference,
        }


@dataclasses.dataclass
class ValidationReport:
    policy_version: str
    repository_commit: str
    checked_documents: int
    references_scanned: int
    results: list[Finding]
    waivers_active: int = 0
    waivers_expired: int = 0

    @property
    def errors(self) -> int:
        return sum(1 for r in self.results if r.severity == "ERROR" and not r.waived)

    @property
    def warnings(self) -> int:
        return sum(1 for r in self.results if r.severity == "WARNING" and not r.waived)

    @property
    def info(self) -> int:
        return sum(1 for r in self.results if r.severity == "INFO" or r.waived)

    def to_json_dict(self) -> dict[str, Any]:
        sorted_results = sorted(self.results, key=lambda r: r.sort_key())
        return {
            "policy_version": self.policy_version,
            "repository_commit": self.repository_commit,
            "checked_documents": self.checked_documents,
            "errors": self.errors,
            "warnings": self.warnings,
            "info": self.info,
            "results": [r.to_dict() for r in sorted_results],
        }

    to_dict = to_json_dict


def _check_depth(obj: Any, current_depth: int, max_depth: int) -> None:
    if current_depth > max_depth:
        raise ValidatorConfigError(
            f"VALIDATOR_POLICY_DEPTH_EXCEEDED: depth {current_depth} > {max_depth}"
        )
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise ValidatorConfigError("Policy keys must be strings")
            _check_depth(v, current_depth + 1, max_depth)
    elif isinstance(obj, list):
        for item in obj:
            _check_depth(item, current_depth + 1, max_depth)


def _validate_policy_strings(obj: Any, *, allow_parent_rel: bool = False) -> None:
    if isinstance(obj, str):
        if "${" in obj or "$(" in obj:
            raise ValidatorConfigError("Environment interpolation is forbidden in policy")
        if obj.startswith(("http://", "https://", "ftp://", "file://")):
            raise ValidatorConfigError("Network URLs are forbidden in policy")
        if re.match(r"^[A-Za-z]:[/\\]", obj) or obj.startswith("/"):
            raise ValidatorConfigError("Absolute paths are forbidden in policy")
        if ".." in obj and not allow_parent_rel:
            raise ValidatorConfigError("Parent-path traversal is forbidden in policy")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _validate_policy_strings(
                v, allow_parent_rel=(k == "required_index_links")
            )
    elif isinstance(obj, list):
        for item in obj:
            _validate_policy_strings(item, allow_parent_rel=allow_parent_rel)


def load_and_validate_policy(policy_path: Path) -> dict[str, Any]:
    if not policy_path.is_file():
        raise ValidatorConfigError(f"Policy file not found: {policy_path}")
    try:
        raw = json.loads(policy_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValidatorConfigError(f"Malformed JSON in policy file: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValidatorConfigError("Policy root must be a JSON object")
    bounds = raw.get("bounds")
    if not isinstance(bounds, dict):
        raise ValidatorConfigError("Policy missing 'bounds' object")
    max_depth = int(bounds.get("max_policy_depth", 32))
    if max_depth > 32 or max_depth < 1:
        raise ValidatorConfigError("Invalid max_policy_depth in policy")
    _check_depth(raw, 1, max_depth)
    if raw.get("policy_version") != POLICY_VERSION:
        raise ValidatorConfigError(
            f"Unsupported policy_version: {raw.get('policy_version')!r}"
        )
    _validate_policy_strings(raw)
    required_top_keys = {
        "policy_version",
        "bounds",
        "current_document_roots",
        "excluded_prefixes",
        "archive_prefix",
        "required_authority_metadata_docs",
        "required_index_links",
        "required_feature_flags",
        "allowed_component_statuses",
        "required_components",
        "required_security_invariants",
        "non_waivable_categories",
        "non_waivable_reason_codes",
    }
    missing = required_top_keys - set(raw.keys())
    if missing:
        raise ValidatorConfigError(f"Policy missing required keys: {sorted(missing)}")
    return raw


def load_and_validate_waivers(
    waiver_path: Path | None,
    policy: dict[str, Any],
    reference_date: date | None = None,
) -> tuple[list[dict[str, Any]], int]:
    if waiver_path is None or not waiver_path.exists():
        return [], 0
    try:
        data = json.loads(waiver_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValidatorConfigError(f"Malformed waiver JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("waivers"), list):
        raise ValidatorConfigError("Waiver file must contain a 'waivers' list")
    today = reference_date or date.today()
    required_fields = {
        "waiver_id",
        "reason_code",
        "document_path",
        "reference_pattern",
        "justification",
        "owner",
        "created_date",
        "expiration_date",
        "tracking_id",
        "approved_by",
    }
    non_waivable_codes = set(policy.get("non_waivable_reason_codes", []))
    active: list[dict[str, Any]] = []
    expired_count = 0
    for entry in data["waivers"]:
        if not isinstance(entry, dict) or not required_fields.issubset(entry.keys()):
            raise ValidatorConfigError("Waiver entry missing required fields")
        for field_name in required_fields:
            val = entry.get(field_name)
            if not isinstance(val, str) or not val.strip():
                raise ValidatorConfigError(f"Waiver field {field_name} must be non-empty")
        doc_path = entry["document_path"]
        ref_pat = entry["reference_pattern"]
        reason_code = entry["reason_code"]
        if "*" in doc_path or ".." in doc_path or doc_path.startswith("/"):
            raise ValidatorConfigError(
                f"Broad or unsafe document_path in waiver {entry['waiver_id']}"
            )
        if ref_pat == "*" or len(ref_pat.strip("*")) < 2:
            raise ValidatorConfigError(
                f"Wildcard reference_pattern too broad in waiver {entry['waiver_id']}"
            )
        if reason_code in non_waivable_codes or reason_code.startswith(
            ("SECURITY_", "ARCHIVE_", "DOCUMENT_SECRET_", "DOCUMENT_REAL_DATA_")
        ):
            raise ValidatorConfigError(
                f"Reason code {reason_code} cannot be waived ({entry['waiver_id']})"
            )
        try:
            exp = date.fromisoformat(entry["expiration_date"])
            created = date.fromisoformat(entry["created_date"])
        except ValueError as exc:
            raise ValidatorConfigError(f"Invalid date in waiver: {exc}") from exc
        if exp < created:
            raise ValidatorConfigError("Waiver expiration_date precedes created_date")
        if exp < today:
            expired_count += 1
            raise ValidatorConfigError(
                f"Expired waiver {entry['waiver_id']} (expired {entry['expiration_date']})"
            )
        active.append(entry)
    return active, expired_count


def _redact_fingerprint(raw_value: str) -> str:
    digest = hashlib.sha256(raw_value.encode("utf-8", errors="ignore")).hexdigest()[:12]
    return f"<redacted:sha256:{digest}>"


def _discover_repo_files(repo_root: Path, excluded_prefixes: tuple[str, ...]) -> list[str]:
    git_dir = repo_root / ".git"
    rel_paths: set[str] = set()
    if git_dir.exists():
        try:
            proc = subprocess.run(
                ["git", "ls-files"],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            for line in proc.stdout.splitlines():
                norm = line.strip().replace("\\", "/")
                if norm:
                    rel_paths.add(norm)
            proc_other = subprocess.run(
                ["git", "ls-files", "--others", "--exclude-standard"],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            for line in proc_other.stdout.splitlines():
                norm = line.strip().replace("\\", "/")
                if norm:
                    rel_paths.add(norm)
        except Exception:
            rel_paths.clear()

    if not rel_paths:
        for p in repo_root.rglob("*"):
            if p.is_symlink():
                try:
                    resolved = p.resolve()
                    resolved.relative_to(repo_root.resolve())
                except Exception:
                    continue
            if p.is_file():
                rel_paths.add(p.relative_to(repo_root).as_posix())

    filtered: list[str] = []
    for rel in sorted(rel_paths):
        if any(rel.startswith(pref) or rel == pref.rstrip("/") for pref in excluded_prefixes):
            continue
        candidate = repo_root / rel
        if candidate.is_symlink():
            try:
                candidate.resolve().relative_to(repo_root.resolve())
            except Exception:
                continue
        if candidate.is_file():
            filtered.append(rel)
    return filtered


def _exact_case_exists(repo_root: Path, rel_posix: str) -> tuple[bool, bool]:
    """Return (exists_any_case, exists_exact_case)."""
    parts = [p for p in rel_posix.strip("/").split("/") if p and p != "."]
    curr = repo_root
    for part in parts:
        if not curr.is_dir():
            return False, False
        entries = {child.name: child for child in curr.iterdir()}
        if part in entries:
            curr = entries[part]
            continue
        lower_map = {name.lower(): child for name, child in entries.items()}
        if part.lower() in lower_map:
            return True, False
        return False, False
    return True, True


MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[([^\]]*)\]\(([^)]*)\)")
INLINE_CODE_RE = re.compile(r"`([^`\n]{1,240})`")
FENCE_OPEN_RE = re.compile(r"^\s*(`{3,}|~{3,})\s*([A-Za-z0-9_-]*)\s*$")

PATH_PREFIXES = (
    "src/",
    "tests/",
    "scripts/",
    "docs/",
    "migrations/",
    "config/",
    "host_agent/",
)
ROOT_FILE_NAMES = frozenset(
    {
        "docker-compose.yml",
        "docker-compose.cpu.yml",
        "docker-compose.gpu.yml",
        "docker-compose.langfuse.yml",
        "docker-compose.voice-gpu.yml",
        "config.example.env",
        "README.md",
        "MIGRATION_GUIDE.md",
        "TEST_RESULTS.md",
        "THIRD_PARTY_NOTICES.md",
        "SECURITY.md",
        "pyproject.toml",
        "requirements.txt",
        "requirements-dev.txt",
        "requirements-swarms.txt",
        "requirements-langfuse.txt",
        "Dockerfile.graph",
        "Dockerfile.mcp",
        "Dockerfile.stt",
        "Dockerfile.tts",
    }
)

HISTORICAL_CONTEXT_KEYWORDS = (
    "archived",
    "historical",
    "superseded",
    "previous",
    "original path",
    "renamed",
    "moved",
    "reconciled",
    "replaced",
    "rejected",
    "legacy",
    "formerly",
    "written to the repo root",
    "not under version control",
    "preserve",
    "-->",
    "->",
    "=>",
)

DISALLOWED_ARCHIVE_AUTHORITY_PHRASES = (
    "follow this archived guide",
    "current configuration is defined in the archived document",
    "run the archived commands",
    "archived document is the source of truth",
)

ALLOWED_SECRET_PLACEHOLDERS = (
    "replace_with_",
    "placeholder",
    "<token>",
    "<secret>",
    "<password>",
    "<new_key>",
    "<old_key>",
    "change_me",
    "fc27test",
    "test_super_pw",
    "unit-test-secret",
    "example.invalid",
    "secrets.token_",
    "fernet.generate_key",
    "${",
    "/run/secrets/",
    "./secrets/",
)


def _archive_status_for_doc(doc_path: str, policy: dict[str, Any]) -> str:
    archive_prefix = policy["archive_prefix"]
    index_files = set(policy.get("archive_index_files", []))
    if doc_path in index_files:
        return "INDEX"
    if doc_path.startswith(archive_prefix):
        return "ARCHIVED"
    return "CURRENT"


def _parse_compose_inventory(repo_root: Path) -> dict[str, Any]:
    services: dict[str, dict[str, Any]] = {}
    networks: dict[str, dict[str, Any]] = {}
    volumes: set[str] = set()
    profiles: set[str] = set()
    compose_files = [
        "docker-compose.yml",
        "docker-compose.cpu.yml",
        "docker-compose.gpu.yml",
        "docker-compose.langfuse.yml",
        "docker-compose.voice-gpu.yml",
    ]
    for cf in compose_files:
        p = repo_root / cf
        if not p.is_file():
            continue
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        for net_name, net_cfg in (data.get("networks") or {}).items():
            if isinstance(net_cfg, dict):
                networks.setdefault(net_name, {}).update(net_cfg)
            else:
                networks.setdefault(net_name, {})
        for vol_name in (data.get("volumes") or {}).keys():
            volumes.add(str(vol_name))
        for svc_name, svc_cfg in (data.get("services") or {}).items():
            if not isinstance(svc_cfg, dict):
                continue
            entry = services.setdefault(
                svc_name,
                {"profiles": set(), "networks": set(), "ports": [], "files": set()},
            )
            entry["files"].add(cf)
            for prof in svc_cfg.get("profiles") or []:
                entry["profiles"].add(str(prof))
                profiles.add(str(prof))
            svc_nets = svc_cfg.get("networks")
            if isinstance(svc_nets, list):
                for n in svc_nets:
                    entry["networks"].add(str(n))
            elif isinstance(svc_nets, dict):
                for n in svc_nets.keys():
                    entry["networks"].add(str(n))
            for port in svc_cfg.get("ports") or []:
                entry["ports"].append(str(port))
    return {
        "services": services,
        "networks": networks,
        "volumes": volumes,
        "profiles": profiles,
    }


def _parse_migrations_inventory(repo_root: Path) -> dict[str, Any]:
    mig_dir = repo_root / "migrations"
    migration_files: dict[str, str] = {}
    tables: set[str] = set()
    roles: set[str] = {
        "fc27_owner",
        "fc27_migrator",
        "fc27_app",
        "fc27_dispatch",
        "fc27_relay",
    }
    grants_version: int | None = None
    if mig_dir.is_dir():
        for sql_file in sorted(mig_dir.glob("*.sql")):
            rel = sql_file.relative_to(repo_root).as_posix()
            num = sql_file.name.split("_", 1)[0]
            migration_files[num] = rel
            text = sql_file.read_text(encoding="utf-8", errors="ignore")
            for m in re.finditer(
                r"CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+(?:public\.)?([a-zA-Z0-9_]+)",
                text,
                re.IGNORECASE,
            ):
                tables.add(m.group(1).lower())
        grants_path = mig_dir / "post" / "grants.sql"
        if grants_path.is_file():
            gtext = grants_path.read_text(encoding="utf-8", errors="ignore")
            m_ver = re.search(r"grants-policy-version:\s*(\d+)", gtext)
            if m_ver:
                grants_version = int(m_ver.group(1))
    return {
        "migration_files": migration_files,
        "tables": tables,
        "roles": roles,
        "grants_version": grants_version,
    }


def _parse_endpoints_inventory(repo_root: Path) -> set[str]:
    routes: set[str] = {
        "/health",
        "/health/live",
        "/health/ready",
        "/run",
        "/threads/{id}",
        "/threads/{id}/decision-challenge",
        "/threads/{id}/decision",
        "/v1/runs",
        "/v1/runs/{id}",
        "/v1/runs/{id}/resume",
        "/v1/runs/{id}/decision-challenge",
        "/v1/runs/{id}/decision",
        "/v1/approval-requests/{id}",
        "/v1/approval-requests/{id}/decisions",
        "/mcp",
        "/tools",
        "/sse",
    }
    graph_api_path = repo_root / "src" / "graph_api.py"
    if graph_api_path.is_file():
        text = graph_api_path.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(
            r'@app\.(?:get|post|put|delete|patch)\(\s*"([^"]+)"', text
        ):
            routes.add(m.group(1))
    routers_dir = repo_root / "src" / "identity" / "routers"
    if routers_dir.is_dir():
        for rf in routers_dir.glob("*.py"):
            rtext = rf.read_text(encoding="utf-8", errors="ignore")
            pref_m = re.search(r'prefix\s*=\s*"([^"]+)"', rtext)
            prefix = pref_m.group(1) if pref_m else "/v1"
            for m in re.finditer(
                r'@router\.(?:get|post|put|delete|patch)\(\s*"([^"]*)"', rtext
            ):
                routes.add(f"{prefix}{m.group(1)}")
    return routes


def _parse_script_cli_flags(script_path: Path) -> set[str]:
    flags: set[str] = {"--help", "-h"}
    if not script_path.is_file():
        return flags
    text = script_path.read_text(encoding="utf-8", errors="ignore")
    for m in re.finditer(r'add_argument\(\s*["\'](--[A-Za-z0-9_-]+)["\']', text):
        flags.add(m.group(1))
    for m in re.finditer(r'["\'](--[A-Za-z0-9_-]+)["\']', text):
        flags.add(m.group(1))
    return flags


def validate_repository(
    repo_root: Path,
    policy_path: Path | None = None,
    waiver_path: Path | None = None,
    include_archive: bool = False,
    category_filter: set[str] | None = None,
) -> ValidationReport:
    start_time = time.monotonic()
    resolved_root = repo_root.resolve()
    eff_policy_path = policy_path or (
        resolved_root / "config" / "documentation_consistency_policy.json"
    )
    policy = load_and_validate_policy(eff_policy_path)
    bounds = policy["bounds"]
    max_file_bytes = int(bounds["max_file_bytes"])
    max_markdown_files = int(bounds["max_markdown_files"])
    max_refs_per_file = int(bounds["max_references_per_file"])
    max_results = int(bounds["max_results"])
    max_exec_sec = float(bounds["max_execution_seconds"])

    eff_waiver_path = waiver_path
    if eff_waiver_path is None:
        default_waiver = resolved_root / "config" / "documentation_consistency_waivers.json"
        if default_waiver.exists():
            eff_waiver_path = default_waiver
    active_waivers, expired_waivers = load_and_validate_waivers(eff_waiver_path, policy)

    excluded_prefixes = tuple(policy["excluded_prefixes"])
    all_repo_files = _discover_repo_files(resolved_root, excluded_prefixes)
    all_repo_files_set = set(all_repo_files)

    commit_sha = "0000000000000000000000000000000000000000"
    if (resolved_root / ".git").exists():
        try:
            commit_sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=str(resolved_root),
                text=True,
                timeout=5,
            ).strip()
        except Exception:
            pass

    findings: list[Finding] = []
    references_scanned = 0

    def add_finding(
        category: str,
        reason_code: str,
        severity: str,
        document_path: str,
        line_number: int | None,
        referenced_value: str,
        safe_message: str,
        suggested_action: str,
        authoritative_source: str,
    ) -> None:
        if len(findings) >= max_results:
            return
        arch_status = _archive_status_for_doc(document_path, policy)
        waived = False
        waiver_ref: str | None = None
        for w in active_waivers:
            if (
                w["reason_code"] == reason_code
                and w["document_path"] == document_path
                and w["reference_pattern"] in referenced_value
            ):
                waived = True
                waiver_ref = w["waiver_id"]
                break
        findings.append(
            Finding(
                policy_version=POLICY_VERSION,
                category=category,
                reason_code=reason_code,
                severity=severity,
                document_path=document_path,
                line_number=line_number,
                referenced_value=referenced_value,
                safe_message=safe_message,
                suggested_action=suggested_action,
                authoritative_source=authoritative_source,
                archive_status=arch_status,
                waived=waived,
                waiver_reference=waiver_ref,
            )
        )

    # Identify current vs archive markdown documents
    archive_prefix = policy["archive_prefix"]
    current_roots = tuple(policy["current_document_roots"])
    current_md_docs: list[str] = []
    archive_md_docs: list[str] = []

    for rel in all_repo_files:
        if not rel.endswith(".md"):
            continue
        if rel.startswith(archive_prefix):
            archive_md_docs.append(rel)
            continue
        for root_entry in current_roots:
            if rel == root_entry or rel.startswith(root_entry.rstrip("/") + "/"):
                current_md_docs.append(rel)
                break

    total_md_count = len(current_md_docs) + len(archive_md_docs)
    if total_md_count > max_markdown_files:
        add_finding(
            "DOCUMENT_AUTHORITY",
            "DOCUMENT_FILE_LIMIT_EXCEEDED",
            "ERROR",
            "docs/README.md",
            1,
            str(total_md_count),
            f"Markdown file count {total_md_count} exceeds max {max_markdown_files}",
            "Archive or consolidate redundant Markdown files",
            "config/documentation_consistency_policy.json",
        )

    compose_inv = _parse_compose_inventory(resolved_root)
    mig_inv = _parse_migrations_inventory(resolved_root)
    endpoints_inv = _parse_endpoints_inventory(resolved_root)

    # Load archived previous_path -> replacement_path map from ARCHIVE_MANIFEST.md
    manifest_rel = "docs/archive/ARCHIVE_MANIFEST.md"
    manifest_map: dict[str, dict[str, str]] = {}
    previous_to_archived: dict[str, str] = {}
    if manifest_rel in all_repo_files_set:
        mtext = (resolved_root / manifest_rel).read_text(encoding="utf-8", errors="ignore")
        for line_idx, line in enumerate(mtext.splitlines(), start=1):
            if not line.strip().startswith("|"):
                continue
            cols = [c.strip().strip("`") for c in line.strip().strip("|").split("|")]
            if len(cols) >= 9 and cols[0].startswith("docs/archive/"):
                arch_p, prev_p, title, purpose, adate, reason, repl, op_st, rev = cols[:9]
                manifest_map[arch_p] = {
                    "previous_path": prev_p,
                    "title": title,
                    "purpose": purpose,
                    "archive_date": adate,
                    "reason": reason,
                    "superseded_by": repl,
                    "operational_status": op_st,
                    "reviewer": rev,
                    "line": str(line_idx),
                }
                previous_to_archived[prev_p] = arch_p

    # DC14: Archive integrity validation
    archive_index_files = set(policy.get("archive_index_files", []))
    for arch_doc in archive_md_docs:
        if arch_doc in archive_index_files:
            continue
        full_arch = resolved_root / arch_doc
        if full_arch.stat().st_size > max_file_bytes:
            add_finding(
                "ARCHIVE_INTEGRITY",
                "DOCUMENT_FILE_TOO_LARGE",
                "ERROR",
                arch_doc,
                1,
                arch_doc,
                f"Archived file exceeds {max_file_bytes} bytes",
                "Split or truncate oversized archived file",
                manifest_rel,
            )
            continue
        atext = full_arch.read_text(encoding="utf-8", errors="ignore")
        head_lines = atext.splitlines()[:15]
        head_block = "\n".join(head_lines)
        if not atext.startswith("> Archived document"):
            add_finding(
                "ARCHIVE_INTEGRITY",
                "ARCHIVE_NOTICE_MISSING",
                "ERROR",
                arch_doc,
                1,
                arch_doc,
                "Archived document is missing '> Archived document' header notice",
                "Prepend standard non-authoritative archive notice header",
                manifest_rel,
            )
        if "Operational status: DO NOT USE" not in head_block:
            add_finding(
                "ARCHIVE_INTEGRITY",
                "ARCHIVE_OPERATIONAL_STATUS_MISSING",
                "ERROR",
                arch_doc,
                1,
                arch_doc,
                "Archived document header is missing 'Operational status: DO NOT USE'",
                "Add '> Operational status: DO NOT USE' to archive header",
                manifest_rel,
            )
        m_sup = re.search(r"^>\s*Superseded by:\s*(.+)$", head_block, re.MULTILINE)
        if not m_sup:
            add_finding(
                "ARCHIVE_INTEGRITY",
                "ARCHIVE_REPLACEMENT_MISSING",
                "ERROR",
                arch_doc,
                1,
                arch_doc,
                "Archived document header is missing 'Superseded by:' field",
                "Specify replacement path or NONE in archive header",
                manifest_rel,
            )
        else:
            repl_target = m_sup.group(1).strip().strip("`")
            if repl_target != "NONE" and not (resolved_root / repl_target).exists():
                add_finding(
                    "ARCHIVE_INTEGRITY",
                    "ARCHIVE_REPLACEMENT_MISSING",
                    "ERROR",
                    arch_doc,
                    1,
                    repl_target,
                    f"Archive replacement target '{repl_target}' does not exist",
                    "Point 'Superseded by:' to an existing current document or NONE",
                    manifest_rel,
                )
        if ("```" in atext or "docker " in atext or "python " in atext) and (
            "Warning: Do not execute commands in this archived document." not in head_block
        ):
            add_finding(
                "ARCHIVE_INTEGRITY",
                "ARCHIVE_DANGEROUS_COMMAND_WARNING_MISSING",
                "ERROR",
                arch_doc,
                1,
                arch_doc,
                "Archived document containing commands lacks non-execution warning",
                "Add '> Warning: Do not execute commands in this archived document.'",
                manifest_rel,
            )
        if arch_doc not in manifest_map:
            add_finding(
                "ARCHIVE_INTEGRITY",
                "ARCHIVE_MANIFEST_ENTRY_MISSING",
                "ERROR",
                arch_doc,
                1,
                arch_doc,
                f"Archived document '{arch_doc}' is not registered in {manifest_rel}",
                f"Add a complete row for '{arch_doc}' in {manifest_rel}",
                manifest_rel,
            )

    for listed_arch, meta in manifest_map.items():
        if not (resolved_root / listed_arch).is_file():
            add_finding(
                "ARCHIVE_INTEGRITY",
                "ARCHIVE_FILE_MISSING",
                "ERROR",
                manifest_rel,
                int(meta["line"]),
                listed_arch,
                f"Manifest entry '{listed_arch}' does not exist on disk",
                "Restore missing archived file or update ARCHIVE_MANIFEST.md",
                manifest_rel,
            )
        repl_val = meta["superseded_by"]
        if repl_val != "NONE" and not (resolved_root / repl_val).exists():
            add_finding(
                "ARCHIVE_INTEGRITY",
                "ARCHIVE_REPLACEMENT_MISSING",
                "ERROR",
                manifest_rel,
                int(meta["line"]),
                repl_val,
                f"Manifest replacement path '{repl_val}' does not exist",
                "Update Superseded By column to an existing document path",
                manifest_rel,
            )
        prev_val = meta["previous_path"]
        if prev_val in all_repo_files_set and prev_val != "TEST_RESULTS.md":
            # Check if identical duplicate
            curr_bytes = (resolved_root / prev_val).read_bytes()
            if (resolved_root / listed_arch).is_file():
                arch_bytes = (resolved_root / listed_arch).read_bytes()
                if curr_bytes == arch_bytes:
                    add_finding(
                        "ARCHIVE_INTEGRITY",
                        "ARCHIVE_DUPLICATE_CURRENT_FILE",
                        "ERROR",
                        listed_arch,
                        1,
                        prev_val,
                        f"Archived file '{listed_arch}' is an identical duplicate of current '{prev_val}'",
                        "Remove duplicate current file via git mv or reconcile content",
                        manifest_rel,
                    )

    # DC15: Authority metadata validation across required major docs
    seen_authority_scopes: dict[str, str] = {}
    for req_doc in policy["required_authority_metadata_docs"]:
        if req_doc not in all_repo_files_set:
            add_finding(
                "DOCUMENT_AUTHORITY",
                "DOCUMENT_STATUS_MISSING",
                "ERROR",
                req_doc,
                1,
                req_doc,
                f"Required current authoritative document '{req_doc}' is missing",
                f"Create '{req_doc}' with required authority metadata block",
                "config/documentation_consistency_policy.json",
            )
            continue
        dtext = (resolved_root / req_doc).read_text(encoding="utf-8", errors="ignore")
        top_section = "\n".join(dtext.splitlines()[:25])
        m_status = re.search(
            r"^\s*(?:-\s*)?\*\*?Status\*\*?:\s*([^\n]+)$",
            top_section,
            re.MULTILINE | re.IGNORECASE,
        ) or re.search(
            r"^\s*Status:\s*([^\n]+)$", top_section, re.MULTILINE | re.IGNORECASE
        )
        if not m_status or "current" not in m_status.group(1).lower():
            add_finding(
                "DOCUMENT_AUTHORITY",
                "DOCUMENT_STATUS_MISSING",
                "ERROR",
                req_doc,
                1,
                "Status: Current",
                f"Document '{req_doc}' is missing 'Status: Current' metadata",
                "Add '- **Status**: Current' to top metadata block",
                req_doc,
            )
        m_commit = re.search(
            r"Last verified commit\*\*?:\s*`?([0-9a-fA-F]{7,40})`?",
            top_section,
            re.IGNORECASE,
        )
        if not m_commit:
            add_finding(
                "DOCUMENT_AUTHORITY",
                "DOCUMENT_VERIFIED_COMMIT_INVALID",
                "ERROR",
                req_doc,
                1,
                "Last verified commit",
                f"Document '{req_doc}' is missing a valid 7..40 hex 'Last verified commit'",
                "Add '- **Last verified commit**: `<git-sha>`' to top metadata block",
                req_doc,
            )
        m_sot = re.search(
            r"Source of truth\*\*?:\s*([^\n]+)", top_section, re.IGNORECASE
        )
        if not m_sot:
            add_finding(
                "SOURCE_OF_TRUTH",
                "DOCUMENT_SOURCE_OF_TRUTH_MISSING",
                "ERROR",
                req_doc,
                1,
                "Source of truth",
                f"Document '{req_doc}' is missing 'Source of truth:' metadata",
                "Add '- **Source of truth**: `<repo-paths>`' to top metadata block",
                req_doc,
            )
        else:
            sot_raw = m_sot.group(1)
            sot_paths = [
                p.strip().strip("`")
                for p in sot_raw.split(",")
                if p.strip().strip("`")
            ]
            if not sot_paths:
                add_finding(
                    "SOURCE_OF_TRUTH",
                    "DOCUMENT_SOURCE_OF_TRUTH_MISSING",
                    "ERROR",
                    req_doc,
                    1,
                    "Source of truth",
                    f"Document '{req_doc}' has empty 'Source of truth:' metadata",
                    "Specify at least one valid repository path in Source of truth",
                    req_doc,
                )
            for sp in sot_paths:
                if not (resolved_root / sp.rstrip("/")).exists():
                    add_finding(
                        "SOURCE_OF_TRUTH",
                        "DOCUMENT_SOURCE_OF_TRUTH_INVALID",
                        "ERROR",
                        req_doc,
                        1,
                        sp,
                        f"Source-of-truth path '{sp}' in '{req_doc}' does not exist",
                        "Update Source of truth to reference existing repository paths",
                        req_doc,
                    )
        m_sup = re.search(r"Supersedes\*\*?:\s*([^\n]+)", top_section, re.IGNORECASE)
        if m_sup:
            sup_items = [
                s.strip().strip("`")
                for s in m_sup.group(1).split(",")
                if s.strip().strip("`")
            ]
            for si in sup_items:
                if si != "NONE" and not si.startswith("docs/archive/"):
                    if (resolved_root / si).exists():
                        add_finding(
                            "DOCUMENT_AUTHORITY",
                            "DOCUMENT_SUPERSEDED_STILL_CURRENT",
                            "ERROR",
                            req_doc,
                            1,
                            si,
                            f"Superseded document '{si}' is still in active current scope",
                            "Archive superseded document under docs/archive/ or remove from Supersedes",
                            req_doc,
                        )
        title_line = dtext.splitlines()[0].strip() if dtext.splitlines() else req_doc
        if title_line in seen_authority_scopes:
            add_finding(
                "DOCUMENT_AUTHORITY",
                "DOCUMENT_DUPLICATE_AUTHORITY",
                "ERROR",
                req_doc,
                1,
                title_line,
                f"Duplicate document title/authority '{title_line}' with '{seen_authority_scopes[title_line]}'",
                "Reconcile or distinguish duplicate authoritative documents",
                req_doc,
            )
        else:
            seen_authority_scopes[title_line] = req_doc

    # DC18: Documentation index validation (docs/README.md)
    index_rel = "docs/README.md"
    if index_rel in all_repo_files_set:
        idx_text = (resolved_root / index_rel).read_text(encoding="utf-8", errors="ignore")
        idx_links: list[tuple[int, str]] = []
        for lno, line in enumerate(idx_text.splitlines(), start=1):
            for m in MARKDOWN_LINK_RE.finditer(line):
                target = m.group(2).split("#", 1)[0].strip()
                if target and not target.startswith(("http://", "https://", "mailto:")):
                    idx_links.append((lno, target))
        targets_only = [t for _, t in idx_links]
        for req_link in policy["required_index_links"]:
            if req_link not in targets_only:
                add_finding(
                    "DOCUMENT_AUTHORITY",
                    "DOC_INDEX_ENTRY_MISSING",
                    "ERROR",
                    index_rel,
                    1,
                    req_link,
                    f"Required documentation index entry '{req_link}' is missing from {index_rel}",
                    f"Add a link to '{req_link}' in {index_rel}",
                    index_rel,
                )
        seen_idx_targets: set[str] = set()
        for lno, target in idx_links:
            resolved_idx = (resolved_root / "docs" / target).resolve()
            if not resolved_idx.exists():
                add_finding(
                    "DOCUMENT_AUTHORITY",
                    "DOC_INDEX_TARGET_MISSING",
                    "ERROR",
                    index_rel,
                    lno,
                    target,
                    f"Documentation index target '{target}' does not exist",
                    "Update link target in docs/README.md",
                    index_rel,
                )
            if target in seen_idx_targets:
                add_finding(
                    "DOCUMENT_AUTHORITY",
                    "DOC_INDEX_DUPLICATE",
                    "ERROR",
                    index_rel,
                    lno,
                    target,
                    f"Duplicate link target '{target}' in {index_rel}",
                    "List each authoritative document once in docs/README.md",
                    index_rel,
                )
            seen_idx_targets.add(target)
            if (
                target.startswith("archive/")
                and target not in {"archive/README.md", "archive/ARCHIVE_MANIFEST.md"}
            ):
                add_finding(
                    "DOCUMENT_AUTHORITY",
                    "DOC_INDEX_ARCHIVE_AS_CURRENT",
                    "ERROR",
                    index_rel,
                    lno,
                    target,
                    f"Archived document '{target}' listed directly in {index_rel}",
                    "Link only archive/README.md and archive/ARCHIVE_MANIFEST.md in docs/README.md",
                    index_rel,
                )
        # Check circular start-here: if Start Here links only to docs/README.md itself
        start_match = re.search(
            r"##\s+1\.\s+Start Here(.*?)(?:##\s+2\.|\Z)", idx_text, re.DOTALL
        )
        if start_match and "(README.md)" in start_match.group(1):
            add_finding(
                "DOCUMENT_AUTHORITY",
                "DOC_INDEX_CIRCULAR_START",
                "ERROR",
                index_rel,
                1,
                "README.md",
                "Circular self-link to docs/README.md in Start Here section",
                "Point Start Here to ARCHITECTURE.md and FC27_COMPONENT_STATUS.md",
                index_rel,
            )

    # DC11: Component status validation (docs/FC27_COMPONENT_STATUS.md)
    comp_rel = "docs/FC27_COMPONENT_STATUS.md"
    if comp_rel in all_repo_files_set:
        ctext = (resolved_root / comp_rel).read_text(encoding="utf-8", errors="ignore")
        allowed_statuses = set(policy["allowed_component_statuses"])
        found_components: dict[str, tuple[int, str, str, str]] = {}
        for lno, line in enumerate(ctext.splitlines(), start=1):
            if not line.strip().startswith("|"):
                continue
            cols = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cols) >= 5 and cols[0] not in {"Component", "---"}:
                comp_name = cols[0].strip("`")
                comp_status = cols[1].strip("`")
                src_ev = cols[3]
                tst_ev = cols[4]
                found_components[comp_name] = (lno, comp_status, src_ev, tst_ev)
                if comp_status not in allowed_statuses:
                    add_finding(
                        "ARCHITECTURE_STATUS",
                        "COMPONENT_STATUS_UNSUPPORTED",
                        "ERROR",
                        comp_rel,
                        lno,
                        comp_status,
                        f"Component '{comp_name}' uses unsupported status '{comp_status}'",
                        f"Use one of {sorted(allowed_statuses)}",
                        comp_rel,
                    )
                if comp_status == "IMPLEMENTED":
                    for ev_token in INLINE_CODE_RE.findall(src_ev) + INLINE_CODE_RE.findall(tst_ev):
                        if ev_token.startswith(PATH_PREFIXES) or ev_token in ROOT_FILE_NAMES:
                            if not (resolved_root / ev_token.rstrip("/")).exists():
                                add_finding(
                                    "ARCHITECTURE_STATUS",
                                    "COMPONENT_STATUS_UNSUPPORTED",
                                    "ERROR",
                                    comp_rel,
                                    lno,
                                    ev_token,
                                    f"Evidence path '{ev_token}' for IMPLEMENTED component '{comp_name}' does not exist",
                                    "Provide existing source and test evidence paths",
                                    comp_rel,
                                )
        for req_comp in policy["required_components"]:
            if req_comp not in found_components:
                add_finding(
                    "ARCHITECTURE_STATUS",
                    "COMPONENT_STATUS_UNSUPPORTED",
                    "ERROR",
                    comp_rel,
                    1,
                    req_comp,
                    f"Required component '{req_comp}' is missing from {comp_rel}",
                    f"Add a row for '{req_comp}' in {comp_rel}",
                    comp_rel,
                )

    # DC8: Feature flag inventory validation (docs/FEATURE_FLAGS.md)
    ff_rel = "docs/FEATURE_FLAGS.md"
    req_flags: dict[str, str] = policy["required_feature_flags"]
    if ff_rel in all_repo_files_set:
        ff_text = (resolved_root / ff_rel).read_text(encoding="utf-8", errors="ignore")
        documented_flags: dict[str, tuple[int, str]] = {}
        for lno, line in enumerate(ff_text.splitlines(), start=1):
            if not line.strip().startswith("|"):
                continue
            cols = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cols) >= 4 and cols[0].strip("`").isupper():
                fname = cols[0].strip("`")
                fdef = cols[1].strip("`").lower()
                facc = cols[2].lower()
                ffail = cols[3].lower()
                documented_flags[fname] = (lno, fdef)
                if fname not in req_flags:
                    add_finding(
                        "FEATURE_FLAG_REFERENCE",
                        "FEATURE_FLAG_NOT_FOUND",
                        "ERROR",
                        ff_rel,
                        lno,
                        fname,
                        f"Documented feature flag '{fname}' is not in source configuration",
                        "Remove stale feature flag or add to src/settings.py",
                        "src/settings.py",
                    )
                else:
                    expected_def = req_flags[fname].lower()
                    if fdef != expected_def:
                        add_finding(
                            "FEATURE_FLAG_REFERENCE",
                            "FEATURE_FLAG_DEFAULT_MISMATCH",
                            "ERROR",
                            ff_rel,
                            lno,
                            f"{fname}={fdef}",
                            f"Flag '{fname}' documented default '{fdef}' != source default '{expected_def}'",
                            f"Document default as '{expected_def}'",
                            "src/settings.py",
                        )
                    if "true" not in facc or "false" not in facc:
                        add_finding(
                            "FEATURE_FLAG_REFERENCE",
                            "FEATURE_FLAG_ACCEPTED_VALUES_MISMATCH",
                            "ERROR",
                            ff_rel,
                            lno,
                            fname,
                            f"Flag '{fname}' must document accepted boolean values (true, false)",
                            "Document accepted values as true, false",
                            "src/settings.py",
                        )
                    if not ffail or ("unlocks all" in ffail or "bypasses" in ffail):
                        add_finding(
                            "FEATURE_FLAG_REFERENCE",
                            "FEATURE_FLAG_FAIL_CLOSED_MISMATCH",
                            "ERROR",
                            ff_rel,
                            lno,
                            fname,
                            f"Flag '{fname}' fail-closed behavior is missing or misstated",
                            "Describe fail-closed behavior when default",
                            "src/settings.py",
                        )
        for req_f in req_flags:
            if req_f not in documented_flags:
                add_finding(
                    "FEATURE_FLAG_REFERENCE",
                    "FEATURE_FLAG_UNDOCUMENTED",
                    "ERROR",
                    ff_rel,
                    1,
                    req_f,
                    f"Active feature flag '{req_f}' is missing from {ff_rel}",
                    f"Document '{req_f}' in {ff_rel}",
                    "src/settings.py",
                )

    # DC12: Security boundaries invariant marker validation
    sec_rel = "docs/SECURITY_BOUNDARIES.md"
    if sec_rel in all_repo_files_set:
        sec_text = (resolved_root / sec_rel).read_text(encoding="utf-8", errors="ignore")
        for inv_marker in policy["required_security_invariants"]:
            if inv_marker not in sec_text:
                add_finding(
                    "SECURITY_INVARIANT",
                    "SECURITY_GATE_DEPENDENCY_MISSTATEMENT",
                    "ERROR",
                    sec_rel,
                    1,
                    inv_marker,
                    f"Security invariant marker '{inv_marker}' is missing from {sec_rel}",
                    f"Document '{inv_marker}' in {sec_rel}",
                    sec_rel,
                )

    # DC17: Test-result report validation (TEST_RESULTS.md)
    tr_rel = "TEST_RESULTS.md"
    if tr_rel in all_repo_files_set:
        tr_text = (resolved_root / tr_rel).read_text(encoding="utf-8", errors="ignore")
        if not re.search(r"\b202\d-\d{2}-\d{2}\b", tr_text):
            add_finding(
                "TEST_REFERENCE",
                "TEST_REPORT_DATE_MISSING",
                "ERROR",
                tr_rel,
                1,
                "Verification Date",
                "TEST_RESULTS.md is missing an ISO execution date (YYYY-MM-DD)",
                "Add Verification Date to TEST_RESULTS.md",
                tr_rel,
            )
        if not re.search(r"\b[0-9a-f]{40}\b", tr_text):
            add_finding(
                "TEST_REFERENCE",
                "TEST_REPORT_COMMIT_MISSING",
                "ERROR",
                tr_rel,
                1,
                "Commit SHA",
                "TEST_RESULTS.md is missing a 40-char commit SHA",
                "Record verified commit SHA in TEST_RESULTS.md",
                tr_rel,
            )
        if "python" not in tr_text.lower() or "windows" not in tr_text.lower() and "linux" not in tr_text.lower():
            add_finding(
                "TEST_REFERENCE",
                "TEST_REPORT_ENVIRONMENT_MISSING",
                "ERROR",
                tr_rel,
                1,
                "Environment",
                "TEST_RESULTS.md is missing OS / Python environment metadata",
                "Record OS and Python version in TEST_RESULTS.md",
                tr_rel,
            )
        if not re.search(r"PostgreSQL\s+`?16", tr_text, re.IGNORECASE):
            add_finding(
                "TEST_REFERENCE",
                "TEST_REPORT_POSTGRES_VERSION_MISSING",
                "ERROR",
                tr_rel,
                1,
                "PostgreSQL 16",
                "TEST_RESULTS.md is missing PostgreSQL 16 version metadata",
                "Record PostgreSQL 16 version in TEST_RESULTS.md",
                tr_rel,
            )
        if "NETWORK_REQUESTS_EXECUTED=0" not in tr_text or "EA_DNS_RESOLUTIONS_EXECUTED=0" not in tr_text:
            add_finding(
                "TEST_REFERENCE",
                "TEST_REPORT_NETWORK_COUNT_MISSING",
                "ERROR",
                tr_rel,
                1,
                "NETWORK_REQUESTS_EXECUTED=0",
                "TEST_RESULTS.md is missing network request or EA DNS resolution zero counts",
                "Include NETWORK_REQUESTS_EXECUTED=0 and EA_DNS_RESOLUTIONS_EXECUTED=0",
                tr_rel,
            )
        if "check_matchpulse_boundary.py" not in tr_text or "fc27_capture_preflight.py" not in tr_text:
            add_finding(
                "TEST_REFERENCE",
                "TEST_REPORT_BOUNDARY_RESULT_MISSING",
                "ERROR",
                tr_rel,
                1,
                "Boundary & Preflight",
                "TEST_RESULTS.md is missing MatchPulse boundary or preflight verification result",
                "Include check_matchpulse_boundary.py and fc27_capture_preflight.py results",
                tr_rel,
            )

    # Safe Adapter Bundle Manifest validation (config/fc27_safe_adapter_bundle_manifest.json)
    bundle_manifest_rel = "config/fc27_safe_adapter_bundle_manifest.json"
    if bundle_manifest_rel in all_repo_files_set:
        try:
            bm_data = json.loads(
                (resolved_root / bundle_manifest_rel).read_text(encoding="utf-8")
            )
        except Exception as exc:
            bm_data = None
            add_finding(
                "SOURCE_OF_TRUTH",
                "SAFE_BUNDLE_MANIFEST_INVALID",
                "ERROR",
                bundle_manifest_rel,
                1,
                bundle_manifest_rel,
                f"Malformed JSON in {bundle_manifest_rel}: {exc}",
                "Fix JSON syntax in config/fc27_safe_adapter_bundle_manifest.json",
                bundle_manifest_rel,
            )
        if isinstance(bm_data, dict):
            b_sha = str(bm_data.get("bundle_sha256", ""))
            if not re.fullmatch(r"[0-9a-f]{64}", b_sha):
                add_finding(
                    "SOURCE_OF_TRUTH",
                    "SAFE_BUNDLE_MANIFEST_INVALID",
                    "ERROR",
                    bundle_manifest_rel,
                    1,
                    b_sha or "bundle_sha256",
                    "bundle_sha256 in config/fc27_safe_adapter_bundle_manifest.json is not valid 64-character lowercase hex",
                    "Provide a valid 64-character lowercase hex SHA-256 digest",
                    bundle_manifest_rel,
                )
            for comp in bm_data.get("components") or []:
                if not isinstance(comp, dict):
                    continue
                dest = str(comp.get("repository_destination", ""))
                if not dest or not (resolved_root / dest).is_file():
                    add_finding(
                        "PATH_REFERENCE",
                        "SAFE_BUNDLE_DESTINATION_MISSING",
                        "ERROR",
                        bundle_manifest_rel,
                        1,
                        dest or "<empty>",
                        f"Repository-native destination '{dest}' in {bundle_manifest_rel} does not exist",
                        "Ensure all repository_destination files in config/fc27_safe_adapter_bundle_manifest.json exist",
                        bundle_manifest_rel,
                    )

    # Scan each Markdown document in scope
    docs_to_scan = list(current_md_docs)
    if include_archive:
        docs_to_scan.extend(archive_md_docs)

    exempt_archive_ref_docs = set(policy.get("authority_exempt_archive_reference_files", []))

    for doc_rel in docs_to_scan:
        if time.monotonic() - start_time > max_exec_sec:
            add_finding(
                "DOCUMENT_AUTHORITY",
                "VALIDATOR_TIMEOUT",
                "ERROR",
                doc_rel,
                1,
                doc_rel,
                f"Validator exceeded {max_exec_sec}s execution timeout",
                "Reduce documentation volume or optimize scan scope",
                "config/documentation_consistency_policy.json",
            )
            break

        doc_path = resolved_root / doc_rel
        is_archived_doc = doc_rel.startswith(archive_prefix)
        if doc_path.stat().st_size > max_file_bytes:
            add_finding(
                "DOCUMENT_AUTHORITY",
                "DOCUMENT_FILE_TOO_LARGE",
                "ERROR",
                doc_rel,
                1,
                doc_rel,
                f"Document size {doc_path.stat().st_size} exceeds limit {max_file_bytes}",
                "Split oversized documentation file",
                "config/documentation_consistency_policy.json",
            )
            continue

        text = doc_path.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
        doc_refs = 0

        in_fence = False
        fence_lang = ""
        fence_marker = ""

        for lno, line in enumerate(lines, start=1):
            lower_line = line.lower()

            # Track fenced code blocks
            m_fence = FENCE_OPEN_RE.match(line)
            if m_fence:
                marker, lang = m_fence.group(1), m_fence.group(2).lower()
                if not in_fence:
                    in_fence = True
                    fence_marker = marker[0]
                    fence_lang = lang
                elif marker[0] == fence_marker and not lang:
                    in_fence = False
                    fence_lang = ""
                    fence_marker = ""
                continue

            # DC16: Secret and real-data hygiene (applies to all scanned docs)
            if "-----begin " in lower_line and "private key-----" in lower_line:
                add_finding(
                    "SECRET_HYGIENE",
                    "DOCUMENT_PRIVATE_KEY_MATERIAL",
                    "ERROR",
                    doc_rel,
                    lno,
                    _redact_fingerprint(line),
                    "Private key PEM header detected in documentation",
                    "Remove private key material immediately",
                    "SECURITY.md",
                )
            # JWT pattern (eyJ...)
            m_jwt = re.search(
                r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b",
                line,
            )
            if m_jwt:
                add_finding(
                    "SECRET_HYGIENE",
                    "DOCUMENT_SECRET_LITERAL",
                    "ERROR",
                    doc_rel,
                    lno,
                    _redact_fingerprint(m_jwt.group(0)),
                    "Probable literal JWT token detected in documentation",
                    "Replace literal token with <token> placeholder",
                    "SECURITY.md",
                )
            # GitHub / OpenAI / AWS / Stripe / Slack style API keys
            m_apikey = re.search(
                r"\b(ghp_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{15,})\b",
                line,
            )
            if m_apikey:
                add_finding(
                    "SECRET_HYGIENE",
                    "DOCUMENT_SECRET_LITERAL",
                    "ERROR",
                    doc_rel,
                    lno,
                    _redact_fingerprint(m_apikey.group(0)),
                    "Probable literal API key detected in documentation",
                    "Replace literal secret with a <secret> placeholder",
                    "SECURITY.md",
                )
            # Credentialed database URL with non-placeholder password
            for m_dburl in re.finditer(
                r"postgresql(?:\+psycopg)?://([^:\s/]+):([^@\s]+)@([^\s/\"'`]+)",
                line,
                re.IGNORECASE,
            ):
                pw = m_dburl.group(2)
                if not any(ph in pw.lower() for ph in ALLOWED_SECRET_PLACEHOLDERS):
                    add_finding(
                        "SECRET_HYGIENE",
                        "DOCUMENT_CREDENTIAL_URL",
                        "ERROR",
                        doc_rel,
                        lno,
                        _redact_fingerprint(m_dburl.group(0)),
                        "Database URL contains a non-placeholder password literal",
                        "Use ${POSTGRES_PASSWORD}, <password>, or *_FILE secret mounts",
                        "MIGRATION_GUIDE.md",
                    )
            # Instructions to commit .env, secrets/, .local-data, or .local-artifacts
            if re.search(
                r"\bgit\s+add(?:\s+[^\n]*?\s+|\s+)(?:\.env|secrets/|\.local-data|\.local-artifacts)",
                line,
                re.IGNORECASE,
            ) and not any(neg in lower_line for neg in ("never", "do not", "don't", "prohibited", "must not", "reject")):
                reason = (
                    "DOCUMENT_INSTRUCTS_SECRET_COMMIT"
                    if (".env" in lower_line or "secrets/" in lower_line)
                    else "DOCUMENT_INSTRUCTS_PRIVATE_DATA_COMMIT"
                )
                cat = (
                    "SECRET_HYGIENE"
                    if reason == "DOCUMENT_INSTRUCTS_SECRET_COMMIT"
                    else "REAL_DATA_HYGIENE"
                )
                add_finding(
                    cat,
                    reason,
                    "ERROR",
                    doc_rel,
                    lno,
                    "git add",
                    "Documentation instructs staging or committing ignored secret or private data paths",
                    "Remove instruction to stage .env, secrets/, .local-data, or .local-artifacts",
                    ".gitignore",
                )

            # Real data hygiene: check for non-placeholder email addresses
            for m_email in re.finditer(
                r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b", line
            ):
                domain = m_email.group(1).lower()
                if domain not in {
                    "example.invalid",
                    "example.com",
                    "example.org",
                    "localhost",
                    "fc27.club",
                    "fc27.local",
                } and not domain.endswith(".example.com"):
                    add_finding(
                        "REAL_DATA_HYGIENE",
                        "DOCUMENT_REAL_DATA_SUSPECTED",
                        "ERROR",
                        doc_rel,
                        lno,
                        _redact_fingerprint(m_email.group(0)),
                        "Non-placeholder email address detected in documentation",
                        "Use commissioner@example.invalid or [OPERATOR_CONTACT_PLACEHOLDER]",
                        "docs/SECURITY_BOUNDARIES.md",
                    )

            # Skip current-only checks when inside archived documents
            if is_archived_doc:
                continue

            # DC3 & DC14: Disallowed current-authority phrasing for archived docs
            for bad_phrase in DISALLOWED_ARCHIVE_AUTHORITY_PHRASES:
                if bad_phrase in lower_line:
                    add_finding(
                        "ARCHIVE_INTEGRITY",
                        "ARCHIVE_REFERENCED_AS_CURRENT",
                        "ERROR",
                        doc_rel,
                        lno,
                        bad_phrase,
                        f"Document uses disallowed archive-authority phrasing: '{bad_phrase}'",
                        "Cite current authoritative documentation instead of archived files",
                        "docs/archive/README.md",
                    )

            # DC12: Security invariant contradiction checks across current docs
            lower_no_stars = lower_line.replace("*", "")
            if (
                "hmac" in lower_no_stars
                and "public key" in lower_no_stars
                and not any(
                    neg in lower_no_stars
                    for neg in (
                        "not a public key",
                        "never a public key",
                        "not use a public key",
                        "never be called a public key",
                    )
                )
            ):
                add_finding(
                    "SECURITY_INVARIANT",
                    "SECURITY_HMAC_PUBLIC_KEY_MISSTATEMENT",
                    "ERROR",
                    doc_rel,
                    lno,
                    "HMAC public key",
                    "HMAC authentication is misdescribed as using a public key",
                    "Describe HMAC-SHA256 as using a symmetric shared secret (APPROVAL_SIGNING_SECRET)",
                    "docs/SECURITY_BOUNDARIES.md",
                )
            if re.search(
                r"command\(resume=.*?\)\s+(?:authorizes|is\s+authoritative|approves\s+the\s+package)",
                lower_line,
            ) and "not" not in lower_line and "never" not in lower_line:
                add_finding(
                    "SECURITY_INVARIANT",
                    "SECURITY_RESUME_AUTHORITY_MISSTATEMENT",
                    "ERROR",
                    doc_rel,
                    lno,
                    "Command(resume=...)",
                    "Resume payload is misdescribed as authoritative approval",
                    "State that Command(resume=...) is non-authoritative and PostgreSQL is re-read",
                    "docs/SECURITY_BOUNDARIES.md",
                )
            if re.search(
                r"\bapproval\s+(?:automatically\s+)?(?:dispatches|triggers\s+dispatch|enables\s+dispatch)\b",
                lower_line,
            ) and "never" not in lower_line and "not" not in lower_line:
                add_finding(
                    "SECURITY_INVARIANT",
                    "SECURITY_APPROVAL_DISPATCH_CONFLATION",
                    "ERROR",
                    doc_rel,
                    lno,
                    "approval -> dispatch",
                    "Approval is conflated with outbound player dispatch",
                    "State that approval terminates at APPROVED, NOT DISPATCHED",
                    "docs/SECURITY_BOUNDARIES.md",
                )
            if re.search(
                r"\bredis\s+is\s+(?:the\s+)?(?:authoritative\s+audit|audit\s+of\s+record|source\s+of\s+truth)\b",
                lower_line,
            ) and "not" not in lower_line and "never" not in lower_line:
                add_finding(
                    "SECURITY_INVARIANT",
                    "SECURITY_REDIS_AUDIT_MISSTATEMENT",
                    "ERROR",
                    doc_rel,
                    lno,
                    "Redis audit",
                    "Redis is misdescribed as an authoritative audit store",
                    "State that PostgreSQL 16 is the sole audit of record and Redis is non-authoritative",
                    "docs/SECURITY_BOUNDARIES.md",
                )
            if re.search(
                r"postgressaver\s+(?:stores|contains)\s+normalized\s+fc\s*27\s+domain",
                lower_line,
            ) and "not" not in lower_line and "never" not in lower_line:
                add_finding(
                    "SECURITY_INVARIANT",
                    "SECURITY_POSTGRESSAVER_DOMAIN_CONFLATION",
                    "ERROR",
                    doc_rel,
                    lno,
                    "PostgresSaver domain storage",
                    "PostgresSaver checkpoint tables are conflated with normalized FC 27 storage",
                    "Keep PostgresSaver checkpoints separate from fc27_* domain tables",
                    "docs/FC27_DATA_MODEL.md",
                )
            if re.search(
                r"\b(?:enabling\s+cache\s+enables\s+provider|enabling\s+provider\s+enables\s+persistence|enabling\s+persistence\s+enables\s+analytics|enabling\s+analytics\s+enables\s+recommendations)\b",
                lower_line,
            ) and "not" not in lower_line and "never" not in lower_line:
                add_finding(
                    "SECURITY_INVARIANT",
                    "SECURITY_GATE_DEPENDENCY_MISSTATEMENT",
                    "ERROR",
                    doc_rel,
                    lno,
                    "gate dependency bypass",
                    "Documentation claims enabling one gate automatically enables a downstream gate",
                    "State that each feature gate fails closed independently",
                    "docs/FEATURE_FLAGS.md",
                )

            # DC13: Tactical claim validation
            if doc_rel != "docs/FC27_TACTICAL_HYPOTHESES.md":
                if re.search(
                    r"\b(?:optimal\s+formation|best\s+formation|maximizes\s+win\s+probability|guaranteed\s+improvement|proven\s+tactic)\b",
                    lower_line,
                ) and "hypothesis" not in lower_line and "unverified" not in lower_line and "not_approvable" not in lower_line:
                    add_finding(
                        "TACTICAL_CLAIM",
                        "TACTICAL_CLAIM_UNLABELED",
                        "ERROR",
                        doc_rel,
                        lno,
                        line.strip()[:80],
                        "Unlabeled tactical superiority claim in operational documentation",
                        "Move claim to docs/FC27_TACTICAL_HYPOTHESES.md or label EXTERNAL_TACTICAL_HYPOTHESIS / NOT_APPROVABLE",
                        "docs/FC27_TACTICAL_HYPOTHESES.md",
                    )
                if re.search(
                    r"\b(?:fc\s*25|fc\s*26)\s+(?:proves|confirms|establishes)\s+fc\s*27\b",
                    lower_line,
                ):
                    add_finding(
                        "TACTICAL_CLAIM",
                        "TACTICAL_FC_VERSION_MISMATCH",
                        "ERROR",
                        doc_rel,
                        lno,
                        line.strip()[:80],
                        "FC25/FC26 tactical evidence represented as proven FC27 fact",
                        "Classify cross-edition claims as EXTERNAL_TACTICAL_HYPOTHESIS (UNVERIFIED)",
                        "docs/FC27_TACTICAL_HYPOTHESES.md",
                    )

            # DC17: Undated permanent test count check
            if re.search(
                r"\balways\s+has\s+exactly\s+\d[\d,]*\s+tests\b", lower_line
            ):
                add_finding(
                    "TEST_REFERENCE",
                    "TEST_REPORT_UNDATED_COUNT_AS_CURRENT",
                    "ERROR",
                    doc_rel,
                    lno,
                    line.strip()[:80],
                    "Undated permanent test count assertion found in documentation",
                    "Bind test pass counts to a specific commit and date in TEST_RESULTS.md",
                    "TEST_RESULTS.md",
                )

            # DC9: SQLite compatibility misstatement check
            if (
                "sqlite" in lower_line
                and any(
                    w in lower_line
                    for w in ("supported", "compatible", "can use sqlite", "uses sqlite")
                )
                and not any(
                    neg in lower_line
                    for neg in ("rejected", "not", "never", "prohibited", "unsupported", "replaced")
                )
            ):
                add_finding(
                    "MIGRATION_REFERENCE",
                    "DATABASE_ENGINE_MISMATCH",
                    "ERROR",
                    doc_rel,
                    lno,
                    "SQLite",
                    "Documentation implies SQLite compatibility; PostgreSQL 16 is required",
                    "State that PostgreSQL 16 is the sole supported database engine",
                    "MIGRATION_GUIDE.md",
                )

            # DC10: Provider live/official/bypass misstatement checks
            if re.search(
                r"\b(?:live\s+ea\s+ingestion\s+is\s+enabled|fc27_live_ingestion_enabled=true\s+by\s+default)\b",
                lower_line,
            ):
                add_finding(
                    "ENDPOINT_REFERENCE",
                    "PROVIDER_LIVE_STATUS_OVERSTATED",
                    "ERROR",
                    doc_rel,
                    lno,
                    "FC27_LIVE_INGESTION_ENABLED",
                    "Documentation overstates live provider status as enabled",
                    "State that live ingestion is disabled by default (FC27_LIVE_INGESTION_ENABLED=false)",
                    "docs/FC27_PROVIDER_INTEGRATION.md",
                )
            if re.search(
                r"\b(?:use\s+impit|enable\s+tls\s+impersonation|pass\s+ea\s+session\s+cookies)\b",
                lower_line,
            ) and not any(
                neg in lower_line
                for neg in ("do not", "never", "prohibited", "rejected", "forbidden", "excluded")
            ):
                add_finding(
                    "ENDPOINT_REFERENCE",
                    "PROVIDER_BROWSER_BYPASS_DOCUMENTED",
                    "ERROR",
                    doc_rel,
                    lno,
                    "browser/TLS bypass",
                    "Documentation prescribes browser/TLS impersonation or session cookies",
                    "Remove browser/TLS bypass instructions",
                    "docs/SECURITY_BOUNDARIES.md",
                )

            # Safe Adapter Bundle documentation hygiene checks
            if re.search(
                r"\.local-artifacts(?:/|\b).*?\b(?:is\s+tracked|are\s+tracked|tracked\s+in\s+git|committed\s+to\s+git)\b",
                lower_line,
            ) and not any(
                neg in lower_line
                for neg in ("never", "not", "do not", "don't", "prohibited", "ignored", "untracked")
            ):
                add_finding(
                    "SECURITY_INVARIANT",
                    "SAFE_BUNDLE_LOCAL_ARTIFACTS_MISSTATED",
                    "ERROR",
                    doc_rel,
                    lno,
                    ".local-artifacts tracked",
                    "Documentation misdescribes .local-artifacts as tracked or committed to Git",
                    "State that .local-artifacts/ is git-ignored and never committed",
                    ".gitignore",
                )
            if re.search(
                r"\b(?:uses\s+zipimport|imported\s+directly\s+from\s+(?:the\s+)?zip|runtime\s+zip\s+import\s+is\s+enabled)\b",
                lower_line,
            ) and not any(
                neg in lower_line
                for neg in ("never", "no ", "not", "do not", "prohibited", "forbidden", "zero")
            ):
                add_finding(
                    "SECURITY_INVARIANT",
                    "SAFE_BUNDLE_ZIP_RUNTIME_MISSTATED",
                    "ERROR",
                    doc_rel,
                    lno,
                    "ZIP runtime import",
                    "Documentation describes importing from a ZIP archive at runtime",
                    "State that only tracked repository-native modules in src/ are imported at runtime",
                    "docs/FC27_PROVIDER_INTEGRATION.md",
                )
            if re.search(
                r"\.local-artifacts(?:/|\b).*?\b(?:is\s+required\s+(?:by|for)\s+ci|are\s+required\s+(?:by|for)\s+ci|required\s+in\s+ci)\b",
                lower_line,
            ) and not any(
                neg in lower_line
                for neg in ("never", "not", "do not", "don't", "without", "zero")
            ):
                add_finding(
                    "SECURITY_INVARIANT",
                    "SAFE_BUNDLE_CI_DEPENDENCY_MISSTATED",
                    "ERROR",
                    doc_rel,
                    lno,
                    ".local-artifacts required for CI",
                    "Documentation states that .local-artifacts is required for CI",
                    "State that .local-artifacts/ is never required by CI",
                    "docs/FC27_PROVIDER_INTEGRATION.md",
                )

            # DC5: Markdown link validation
            if not in_fence:
                for m_link in MARKDOWN_LINK_RE.finditer(line):
                    doc_refs += 1
                    references_scanned += 1
                    if doc_refs > max_refs_per_file:
                        add_finding(
                            "MARKDOWN_LINK",
                            "DOCUMENT_REFERENCE_LIMIT_EXCEEDED",
                            "ERROR",
                            doc_rel,
                            lno,
                            doc_rel,
                            f"Reference count in {doc_rel} exceeds {max_refs_per_file}",
                            "Split document or reduce inline references",
                            "config/documentation_consistency_policy.json",
                        )
                        break

                    raw_target = m_link.group(2).strip()
                    if not raw_target:
                        add_finding(
                            "MARKDOWN_LINK",
                            "MARKDOWN_LINK_MALFORMED",
                            "ERROR",
                            doc_rel,
                            lno,
                            "[]()",
                            "Empty Markdown link target",
                            "Provide a valid relative path or URL",
                            doc_rel,
                        )
                        continue
                    if raw_target.startswith(("http://", "https://")):
                        if " " in raw_target or "http://" in raw_target[7:] or "https://" in raw_target[8:]:
                            add_finding(
                                "MARKDOWN_LINK",
                                "MARKDOWN_EXTERNAL_LINK_SUSPECT",
                                "WARNING",
                                doc_rel,
                                lno,
                                raw_target,
                                f"External URL appears malformed or concatenated: '{raw_target}'",
                                "Fix external URL syntax",
                                doc_rel,
                            )
                        continue
                    if raw_target.startswith(("mailto:", "#")) or "://" in raw_target:
                        continue
                    path_part = raw_target.split("#", 1)[0].strip()
                    if not path_part:
                        continue
                    try:
                        resolved_target = (doc_path.parent / path_part).resolve()
                    except Exception:
                        add_finding(
                            "MARKDOWN_LINK",
                            "MARKDOWN_LINK_MALFORMED",
                            "ERROR",
                            doc_rel,
                            lno,
                            raw_target,
                            f"Malformed Markdown link target: '{raw_target}'",
                            "Fix relative link syntax",
                            doc_rel,
                        )
                        continue
                    try:
                        rel_target_posix = resolved_target.relative_to(resolved_root).as_posix()
                    except ValueError:
                        add_finding(
                            "MARKDOWN_LINK",
                            "MARKDOWN_LINK_OUTSIDE_REPOSITORY",
                            "ERROR",
                            doc_rel,
                            lno,
                            raw_target,
                            f"Relative link '{raw_target}' resolves outside the repository root",
                            "Keep all relative Markdown links inside the repository root",
                            doc_rel,
                        )
                        continue

                    if not resolved_target.exists():
                        add_finding(
                            "MARKDOWN_LINK",
                            "MARKDOWN_RELATIVE_LINK_BROKEN",
                            "ERROR",
                            doc_rel,
                            lno,
                            raw_target,
                            f"Broken relative Markdown link '{raw_target}' (resolved: '{rel_target_posix}')",
                            "Update link to point to an existing repository file",
                            doc_rel,
                        )
                        continue

                    if (
                        rel_target_posix.startswith(archive_prefix)
                        and doc_rel not in exempt_archive_ref_docs
                    ):
                        if not any(kw in lower_line for kw in HISTORICAL_CONTEXT_KEYWORDS):
                            add_finding(
                                "MARKDOWN_LINK",
                                "MARKDOWN_ARCHIVE_LINK_UNLABELED",
                                "ERROR",
                                doc_rel,
                                lno,
                                raw_target,
                                f"Operational document '{doc_rel}' links to archived file '{rel_target_posix}' without historical labeling",
                                "Link to the current replacement document instead of docs/archive/",
                                "docs/archive/ARCHIVE_MANIFEST.md",
                            )

            # DC6: Command block validation inside fenced blocks
            if in_fence and fence_lang in {"powershell", "pwsh", "bash", "sh", "shell", "cmd"}:
                if fence_lang in {"bash", "sh"} and line.rstrip().endswith("`") and not line.rstrip().endswith("\\`"):
                    add_finding(
                        "COMMAND_EXAMPLE",
                        "COMMAND_SHELL_MISMATCH",
                        "ERROR",
                        doc_rel,
                        lno,
                        line.strip()[:60],
                        "Bash code block uses PowerShell backtick line continuation",
                        "Use backslash (\\) for line continuation in bash blocks or label block powershell",
                        doc_rel,
                    )

                # Check python scripts/<name>.py in fenced commands
                for m_scr in re.finditer(
                    r"(?:python(?:3|\.exe)?)\s+(?:-[A-Za-z0-9]+\s+)*(?:\.[\\/])?scripts[\\/]([A-Za-z0-9_.-]+\.py)([^\n`\\]*)",
                    line,
                ):
                    scr_name = m_scr.group(1)
                    scr_args = m_scr.group(2) or ""
                    scr_rel = f"scripts/{scr_name}"
                    doc_refs += 1
                    references_scanned += 1
                    if not (resolved_root / scr_rel).is_file():
                        add_finding(
                            "SCRIPT_REFERENCE",
                            "SCRIPT_NOT_FOUND",
                            "ERROR",
                            doc_rel,
                            lno,
                            scr_rel,
                            f"Referenced script '{scr_rel}' does not exist",
                            "Update command to reference an existing script in scripts/",
                            "scripts/",
                        )
                    else:
                        known_flags = _parse_script_cli_flags(resolved_root / scr_rel)
                        for m_arg in re.finditer(r"(?:^|\s)(--[A-Za-z0-9_-]+)", scr_args):
                            arg_flag = m_arg.group(1)
                            if known_flags and arg_flag not in known_flags:
                                add_finding(
                                    "COMMAND_EXAMPLE",
                                    "CLI_ARGUMENT_NOT_FOUND",
                                    "ERROR",
                                    doc_rel,
                                    lno,
                                    f"{scr_rel} {arg_flag}",
                                    f"CLI argument '{arg_flag}' not found in '{scr_rel}' parser",
                                    f"Use a supported flag from {sorted(known_flags)}",
                                    scr_rel,
                                )

                # Check python -m src.<module>
                for m_mod in re.finditer(
                    r"python(?:3|\.exe)?\s+-m\s+(src\.[A-Za-z0-9_.]+)", line
                ):
                    mod_name = m_mod.group(1)
                    mod_path = resolved_root / (mod_name.replace(".", "/") + ".py")
                    pkg_path = resolved_root / mod_name.replace(".", "/") / "__init__.py"
                    doc_refs += 1
                    references_scanned += 1
                    if not mod_path.is_file() and not pkg_path.is_file():
                        add_finding(
                            "COMMAND_EXAMPLE",
                            "PYTHON_MODULE_NOT_FOUND",
                            "ERROR",
                            doc_rel,
                            lno,
                            mod_name,
                            f"Python module '{mod_name}' does not exist",
                            "Update command to reference an existing module in src/",
                            "src/",
                        )

                # Check uvicorn / uvicorn.exe src.<module>:<factory>
                for m_uvi in re.finditer(
                    r"uvicorn(?:\.exe)?\s+(?:--[A-Za-z0-9_-]+(?:\s+[^\s`\\]+)?\s+)*(src\.[A-Za-z0-9_.]+):([A-Za-z0-9_]+)",
                    line,
                ):
                    u_mod, u_attr = m_uvi.group(1), m_uvi.group(2)
                    u_path = resolved_root / (u_mod.replace(".", "/") + ".py")
                    doc_refs += 1
                    references_scanned += 1
                    if not u_path.is_file():
                        add_finding(
                            "COMMAND_EXAMPLE",
                            "PYTHON_MODULE_NOT_FOUND",
                            "ERROR",
                            doc_rel,
                            lno,
                            u_mod,
                            f"Uvicorn module '{u_mod}' does not exist",
                            "Update uvicorn command to an existing module",
                            "src/",
                        )
                    else:
                        utext = u_path.read_text(encoding="utf-8", errors="ignore")
                        if f"def {u_attr}" not in utext and f"{u_attr} =" not in utext and f"{u_attr}=" not in utext:
                            add_finding(
                                "COMMAND_EXAMPLE",
                                "APP_FACTORY_NOT_FOUND",
                                "ERROR",
                                doc_rel,
                                lno,
                                f"{u_mod}:{u_attr}",
                                f"App factory or ASGI object '{u_attr}' not found in '{u_mod}'",
                                "Reference an existing ASGI app or factory in the target module",
                                u_mod.replace(".", "/") + ".py",
                            )

                # Check pytest tests/<file>.py
                for m_tst in re.finditer(
                    r"\b(tests/[A-Za-z0-9_./-]+\.py)\b", line
                ):
                    t_rel = m_tst.group(1)
                    doc_refs += 1
                    references_scanned += 1
                    if "*" not in t_rel and not (resolved_root / t_rel).is_file():
                        add_finding(
                            "TEST_REFERENCE",
                            "TEST_FILE_NOT_FOUND",
                            "ERROR",
                            doc_rel,
                            lno,
                            t_rel,
                            f"Referenced test file '{t_rel}' does not exist",
                            "Update command to reference an existing test module in tests/",
                            "tests/",
                        )

                # Check docker compose -f <file> and --profile <profile>
                if "docker compose" in lower_line:
                    for m_cf in re.finditer(r"-f\s+([A-Za-z0-9_.-]+\.yml)", line):
                        cf_name = m_cf.group(1)
                        doc_refs += 1
                        references_scanned += 1
                        if not (resolved_root / cf_name).is_file():
                            add_finding(
                                "COMPOSE_REFERENCE",
                                "COMPOSE_FILE_NOT_FOUND",
                                "ERROR",
                                doc_rel,
                                lno,
                                cf_name,
                                f"Docker Compose file '{cf_name}' does not exist",
                                "Use an existing docker-compose*.yml file",
                                "docker-compose.yml",
                            )
                    for m_pf in re.finditer(r"--profile\s+([A-Za-z0-9_-]+)", line):
                        pf_name = m_pf.group(1)
                        doc_refs += 1
                        references_scanned += 1
                        if compose_inv["profiles"] and pf_name not in compose_inv["profiles"]:
                            add_finding(
                                "COMPOSE_REFERENCE",
                                "COMPOSE_PROFILE_NOT_FOUND",
                                "ERROR",
                                doc_rel,
                                lno,
                                pf_name,
                                f"Docker Compose profile '{pf_name}' is not defined in any compose file",
                                f"Use a defined profile: {sorted(compose_inv['profiles'])}",
                                "docker-compose.yml",
                            )

            # DC7: Compose service, profile, and network checks in prose/commands
            if compose_inv["services"]:
                for m_csvc in re.finditer(
                    r"\bcompose\s+service\s+`([a-zA-Z0-9_-]+)`", line, re.IGNORECASE
                ):
                    svc_cand = m_csvc.group(1)
                    doc_refs += 1
                    references_scanned += 1
                    if svc_cand not in compose_inv["services"]:
                        add_finding(
                            "COMPOSE_REFERENCE",
                            "COMPOSE_SERVICE_NOT_FOUND",
                            "ERROR",
                            doc_rel,
                            lno,
                            svc_cand,
                            f"Compose service '{svc_cand}' is not defined in docker-compose*.yml",
                            "Reference an existing service in docker-compose.yml",
                            "docker-compose.yml",
                        )
                for m_cprof in re.finditer(
                    r"\bservice\s+`([a-zA-Z0-9_-]+)`\s+uses\s+profile\s+`([a-zA-Z0-9_-]+)`",
                    line,
                    re.IGNORECASE,
                ):
                    s_name, p_name = m_cprof.group(1), m_cprof.group(2)
                    doc_refs += 1
                    references_scanned += 1
                    if s_name in compose_inv["services"] and p_name not in compose_inv["services"][s_name]["profiles"]:
                        add_finding(
                            "COMPOSE_REFERENCE",
                            "COMPOSE_PROFILE_MISMATCH",
                            "ERROR",
                            doc_rel,
                            lno,
                            f"{s_name}:{p_name}",
                            f"Service '{s_name}' does not use profile '{p_name}' in docker-compose*.yml",
                            "Match service profile to docker-compose.yml",
                            "docker-compose.yml",
                        )
                for m_cnet in re.finditer(
                    r"\bcompose\s+network\s+`([a-zA-Z0-9_-]+)`", line, re.IGNORECASE
                ):
                    net_cand = m_cnet.group(1)
                    doc_refs += 1
                    references_scanned += 1
                    if compose_inv["networks"] and net_cand not in compose_inv["networks"]:
                        add_finding(
                            "COMPOSE_REFERENCE",
                            "COMPOSE_NETWORK_MISMATCH",
                            "ERROR",
                            doc_rel,
                            lno,
                            net_cand,
                            f"Compose network '{net_cand}' is not defined in docker-compose*.yml",
                            "Use a defined network from docker-compose.yml",
                            "docker-compose.yml",
                        )

            # DC10: Local application endpoint and authentication checks
            if not any(
                neg in lower_line
                for neg in ("no route", "not implemented", "phase 3", "future", "deferred", "proposed", "rejected")
            ):
                ep_regexes = [
                    re.compile("^" + re.sub(r"\{[^}]+\}", r"[^/]+", k) + "$")
                    for k in endpoints_inv
                ]
                for m_ep in re.finditer(
                    r"`(?:GET|POST|PUT|DELETE|PATCH)\s+(/v1/[A-Za-z0-9_{}/-]+)`", line
                ):
                    ep_path = m_ep.group(1)
                    doc_refs += 1
                    references_scanned += 1
                    if not any(rx.match(ep_path) for rx in ep_regexes):
                        add_finding(
                            "ENDPOINT_REFERENCE",
                            "APPLICATION_ENDPOINT_NOT_FOUND",
                            "ERROR",
                            doc_rel,
                            lno,
                            ep_path,
                            f"Application endpoint '{ep_path}' is not defined in FastAPI routers",
                            "Update endpoint path to match src/graph_api.py or src/identity/routers/",
                            "src/graph_api.py",
                        )
            if (
                re.search(
                    r"(?:/v1/runs|/v1/approval-requests)\s+is\s+unauthenticated\b",
                    lower_line,
                )
                or re.search(
                    r"\bgraph_service_token\s+authorizes\s+/v1/runs/\{id\}/decision\b",
                    lower_line,
                )
            ) and "not" not in lower_line and "never" not in lower_line:
                add_finding(
                    "ENDPOINT_REFERENCE",
                    "APPLICATION_AUTH_MISMATCH",
                    "ERROR",
                    doc_rel,
                    lno,
                    "endpoint auth",
                    "Application endpoint authentication requirement is misstated",
                    "Document Bearer token / Commissioner authentication accurately per src/graph_api.py",
                    "src/graph_api.py",
                )

            # DC4: Inline code path references (outside fenced blocks)
            if not in_fence:
                for m_code in INLINE_CODE_RE.finditer(line):
                    token = m_code.group(1).strip()
                    if not token or " " in token or token.startswith(("-", "$", "<", "{", "http")):
                        continue
                    clean_tok = token.split("#", 1)[0].split(":", 1)[0].strip()
                    if not clean_tok:
                        continue
                    if ".." in clean_tok.split("/"):
                        # Allow parent links only if inside repo, else flag traversal
                        try:
                            res_p = (doc_path.parent / clean_tok).resolve()
                            res_p.relative_to(resolved_root)
                        except ValueError:
                            add_finding(
                                "PATH_REFERENCE",
                                "DOC_PATH_TRAVERSAL",
                                "ERROR",
                                doc_rel,
                                lno,
                                token,
                                f"Inline path '{token}' traverses outside repository root",
                                "Use repository-relative paths without parent traversal outside root",
                                doc_rel,
                            )
                            continue

                    is_repo_path = (
                        clean_tok.startswith(PATH_PREFIXES)
                        or clean_tok in ROOT_FILE_NAMES
                        or clean_tok in previous_to_archived
                    )
                    if not is_repo_path:
                        continue

                    if clean_tok.startswith(("src/fc_clubs_api/", "src/transport/")) and (
                        doc_rel == "THIRD_PARTY_NOTICES.md"
                        or any(
                            kw in lower_line
                            for kw in (
                                "upstream",
                                "clean_room_reimplemented",
                                "adapted_with_license",
                                "concept_only",
                                "rejected",
                                "proclubs-sdk",
                                "fc27-clubs-api",
                            )
                        )
                    ):
                        continue

                    doc_refs += 1
                    references_scanned += 1
                    if "*" in clean_tok or "<" in clean_tok:
                        continue

                    # Check if token is a moved/archived previous path
                    if clean_tok in previous_to_archived and clean_tok != "TEST_RESULTS.md":
                        if (
                            doc_rel not in exempt_archive_ref_docs
                            and not any(kw in lower_line for kw in HISTORICAL_CONTEXT_KEYWORDS)
                            and "supersedes" not in lower_line
                        ):
                            add_finding(
                                "PATH_REFERENCE",
                                "DOC_PATH_ARCHIVED_AS_CURRENT",
                                "ERROR",
                                doc_rel,
                                lno,
                                clean_tok,
                                f"Moved/archived path '{clean_tok}' is referenced as a current file",
                                f"Reference replacement or archived path '{previous_to_archived[clean_tok]}' with historical label",
                                "docs/archive/ARCHIVE_MANIFEST.md",
                            )
                        continue

                    if clean_tok.startswith("docs/archive/") and doc_rel not in exempt_archive_ref_docs:
                        if (
                            not any(kw in lower_line for kw in HISTORICAL_CONTEXT_KEYWORDS)
                            and "supersedes" not in lower_line
                        ):
                            add_finding(
                                "PATH_REFERENCE",
                                "DOC_PATH_ARCHIVED_AS_CURRENT",
                                "ERROR",
                                doc_rel,
                                lno,
                                clean_tok,
                                f"Archived path '{clean_tok}' referenced as current authority",
                                "Cite the current replacement document instead of docs/archive/",
                                "docs/archive/ARCHIVE_MANIFEST.md",
                            )

                    # Skip historical mentions of removed non-archive paths in notes/truth-table
                    if not clean_tok.startswith("docs/archive/") and (
                        any(kw in lower_line for kw in HISTORICAL_CONTEXT_KEYWORDS)
                        or (
                            doc_rel == "docs/FC27_DOCUMENTATION_TRUTH_TABLE.md"
                            and ("instead of" in lower_line or "referenced" in lower_line)
                        )
                    ):
                        continue

                    exists_any, exists_exact = _exact_case_exists(resolved_root, clean_tok)
                    if not exists_any:
                        reason_c = "DOC_PATH_NOT_FOUND"
                        cat_c = "PATH_REFERENCE"
                        if clean_tok.startswith("scripts/") and clean_tok.endswith(".py"):
                            cat_c = "SCRIPT_REFERENCE"
                            reason_c = "SCRIPT_NOT_FOUND"
                        elif clean_tok.startswith("tests/") and clean_tok.endswith(".py"):
                            cat_c = "TEST_REFERENCE"
                            reason_c = "TEST_FILE_NOT_FOUND"
                        elif clean_tok.startswith("migrations/") and clean_tok.endswith(".sql"):
                            cat_c = "MIGRATION_REFERENCE"
                            reason_c = "MIGRATION_NOT_FOUND"
                        add_finding(
                            cat_c,
                            reason_c,
                            "ERROR",
                            doc_rel,
                            lno,
                            clean_tok,
                            f"Referenced repository path '{clean_tok}' does not exist",
                            "Update path to match an existing file or directory in the repository",
                            doc_rel,
                        )
                    elif not exists_exact:
                        add_finding(
                            "PATH_REFERENCE",
                            "DOC_PATH_CASE_MISMATCH",
                            "ERROR",
                            doc_rel,
                            lno,
                            clean_tok,
                            f"Path '{clean_tok}' differs in case from the filesystem entry",
                            "Match exact filename casing for cross-platform Linux/macOS/Windows portability",
                            doc_rel,
                        )

    if category_filter:
        findings = [f for f in findings if f.category in category_filter]

    return ValidationReport(
        policy_version=POLICY_VERSION,
        repository_commit=commit_sha,
        checked_documents=len(docs_to_scan),
        references_scanned=references_scanned,
        results=sorted(findings, key=lambda r: r.sort_key()),
        waivers_active=len(active_waivers),
        waivers_expired=expired_waivers,
    )


def _format_human_report(report: ValidationReport, strict: bool = False) -> str:
    lines = [
        f"Documentation Consistency Report ({report.policy_version})",
        f"Repository commit: {report.repository_commit}",
        f"Checked documents: {report.checked_documents} | References scanned: {report.references_scanned}",
        f"Summary: ERRORS={report.errors} WARNINGS={report.warnings} INFO={report.info} (waivers_active={report.waivers_active})",
    ]
    by_cat: dict[str, int] = {}
    for r in report.results:
        if not r.waived:
            by_cat[r.category] = by_cat.get(r.category, 0) + 1
    if by_cat:
        lines.append("Findings by category: " + ", ".join(f"{k}={v}" for k, v in sorted(by_cat.items())))
    for r in report.results:
        loc = f"{r.document_path}:{r.line_number}" if r.line_number else r.document_path
        waiver_tag = f" [WAIVED:{r.waiver_reference}]" if r.waived else ""
        lines.append(
            f"[{r.severity}]{waiver_tag} {r.category}/{r.reason_code} at {loc} ({r.referenced_value}): "
            f"{r.safe_message} -> Fix: {r.suggested_action}"
        )
    status_ok = report.errors == 0 and (not strict or report.warnings == 0)
    lines.append(f"Result: {'PASS' if status_ok else 'FAIL'}")
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deterministic FC 27 documentation consistency validator."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        default=True,
        help="Run static consistency check (default mode).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit deterministic JSON report to stdout.",
    )
    parser.add_argument(
        "--category",
        action="append",
        dest="categories",
        help="Filter findings to one or more validation categories.",
    )
    parser.add_argument(
        "--include-archive",
        action="store_true",
        help="Scan archived documents under docs/archive/ in addition to manifest/header checks.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat WARNING findings as a non-zero exit failure.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Optional repository root override (for isolated fixture testing).",
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=None,
        help="Optional policy JSON file path override.",
    )
    parser.add_argument(
        "--waivers",
        type=Path,
        default=None,
        help="Optional waivers JSON file path override.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    repo_root = (
        args.repo_root.resolve()
        if args.repo_root is not None
        else Path(__file__).resolve().parent.parent
    )

    cat_filter: set[str] | None = None
    if args.categories:
        cat_filter = set(args.categories)
        unknown = cat_filter - VALID_CATEGORIES
        if unknown:
            sys.stderr.write(f"Invalid category filter: {sorted(unknown)}\n")
            return 2

    try:
        report = validate_repository(
            repo_root=repo_root,
            policy_path=args.policy,
            waiver_path=args.waivers,
            include_archive=args.include_archive,
            category_filter=cat_filter,
        )
    except ValidatorConfigError as exc:
        sys.stderr.write(f"Configuration error: {exc}\n")
        return 2
    except Exception as exc:
        sys.stderr.write(f"Execution error: {type(exc).__name__}: {exc}\n")
        return 2

    if args.json:
        payload = json.dumps(report.to_json_dict(), indent=2)
        sys.stdout.write(payload + "\n")
    else:
        sys.stdout.write(_format_human_report(report, strict=args.strict) + "\n")

    if report.errors > 0:
        return 1
    if args.strict and report.warnings > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
