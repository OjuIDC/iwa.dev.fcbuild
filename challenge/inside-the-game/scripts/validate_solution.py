"""Validate the MatchPulse Live Visual Studio solution without Visual Studio.

Checks performed:
  * the solution file parses and uses a supported Visual Studio format;
  * every declared solution folder is present;
  * every solution item resolves to a real file in the repository;
  * the Debug, Release, and ChallengeDemo configurations are declared;
  * no absolute, user-specific, or parent-repository path is referenced;
  * no per-user artifact (.vs, .suo, .user) is referenced.

Exit code 0 means the solution is structurally valid.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOLUTION = ROOT / "MatchPulse.Live.sln"

SOLUTION_FOLDER_TYPE = "{2150E333-8FDC-42A3-9474-1A3956D46DE8}"
PROJECT_LINE = re.compile(r'^Project\("([^"]+)"\)\s*=\s*"([^"]*)",\s*"([^"]*)",\s*"(\{[0-9A-Fa-f-]+\})"')
ITEM_LINE = re.compile(r"^\s*([^\s=]+)\s*=\s*([^\s=]+)\s*$")
REQUIRED_CONFIGURATIONS = ("Debug", "Release", "ChallengeDemo")
REQUIRED_FOLDERS = (
    "src",
    "MatchPulse.Live.Api",
    "tests",
    "MatchPulse.Live.Tests",
    "infrastructure",
    "MatchPulse.Live.Azure",
    "build",
    "MatchPulse.Live.Build",
    "documentation",
    "MatchPulse.Live.Docs",
)
FORBIDDEN_PATTERNS = (
    re.compile(r"(?i)[a-z]:[\\/]"),                       # absolute Windows path
    re.compile(r"(?i)/home/"),                            # absolute POSIX home path
    re.compile(r"(?i)\b[a-z0-9]+\.dev\.[a-z0-9]+\b"),     # host-shaped internal repository names
    re.compile(r"(?i)-baseline\b"),                       # internal baseline codenames
    re.compile(r"(?i)-full-stack\b"),                     # internal compose project names
)
FORBIDDEN_SUFFIXES = (".suo", ".user", ".vs")


def parse() -> dict:
    text = SOLUTION.read_text(encoding="utf-8")
    folders: dict[str, str] = {}
    items: list[str] = []
    inside = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("ProjectSection(SolutionItems)"):
            inside = True
            continue
        if inside and stripped == "EndProjectSection":
            inside = False
            continue
        if inside and stripped:
            match = ITEM_LINE.match(line)
            if match:
                items.append(match.group(1).strip())
                continue
        if not inside:
            match = PROJECT_LINE.match(stripped)
            if match:
                folders[match.group(2)] = match.group(1)
    configurations = sorted({
        line.strip().split("|", 1)[0]
        for line in text.splitlines()
        if re.match(r"^\s*(Debug|Release|ChallengeDemo)\|", line)
    })
    return {"text": text, "folders": folders, "items": items, "configurations": configurations}


def validate() -> dict:
    report: dict = {"solution": str(SOLUTION.relative_to(ROOT)), "errors": [], "warnings": []}
    if not SOLUTION.is_file():
        report["errors"].append("MatchPulse.Live.sln is missing")
        return report

    parsed = parse()
    text = parsed["text"]
    folders = parsed["folders"]
    items = parsed["items"]
    configurations = parsed["configurations"]

    if not text.startswith("Microsoft Visual Studio Solution File, Format Version 12.00"):
        report["errors"].append("solution does not declare the Visual Studio 12.00 format")

    for name, project_type in folders.items():
        if project_type != SOLUTION_FOLDER_TYPE:
            report["errors"].append(f"'{name}' is not a supported solution folder type: {project_type}")

    for name in REQUIRED_FOLDERS:
        if name not in folders:
            report["errors"].append(f"missing solution folder: {name}")

    for configuration in REQUIRED_CONFIGURATIONS:
        if configuration not in configurations:
            report["errors"].append(f"missing solution configuration: {configuration}")

    for item in items:
        candidate = ROOT / item.replace("\\", "/")
        if not candidate.is_file():
            report["errors"].append(f"solution item does not exist: {item}")
        if item.startswith("/") or re.match(r"^[A-Za-z]:", item):
            report["errors"].append(f"solution item uses an absolute path: {item}")
        if ".." in re.split(r"[\\/]", item):
            report["errors"].append(f"solution item escapes the repository: {item}")
        lowered = item.lower()
        for pattern in FORBIDDEN_PATTERNS:
            if pattern.search(item):
                report["errors"].append(f"solution item references a private path: {item}")
        for suffix in FORBIDDEN_SUFFIXES:
            if lowered.endswith(suffix):
                report["errors"].append(f"solution item references a per-user artifact: {item}")

    report["solution_folders"] = sorted(folders)
    report["solution_items"] = len(items)
    report["configurations"] = configurations
    report["status"] = "PASS" if not report["errors"] else "FAIL"
    return report


def main() -> int:
    report = validate()
    print(json.dumps(report, indent=2))
    if report["errors"]:
        print(f"FAIL: {len(report['errors'])} solution validation error(s)")
        return 1
    print("PASS: solution structure is valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
