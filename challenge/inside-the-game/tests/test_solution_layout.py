"""Structural checks for the MatchPulse Live Visual Studio solution."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOLUTION = ROOT / "MatchPulse.Live.sln"

SOLUTION_FOLDER_TYPE = "{2150E333-8FDC-42A3-9474-1A3956D46DE8}"
PROJECT_LINE = re.compile(r'^Project\("([^"]+)"\)\s*=\s*"([^"]*)",\s*"([^"]*)",\s*"(\{[0-9A-Fa-f-]+\})"')
ITEM_LINE = re.compile(r"^\s*([^\s=]+)\s*=\s*([^\s=]+)\s*$")

REQUIRED_FOLDERS = {
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
}
REQUIRED_CONFIGURATIONS = ("Debug", "Release", "ChallengeDemo")
REQUIRED_ROOT_FILES = (
    "MatchPulse.Live.sln",
    "README.md",
    "README-VISUAL-STUDIO.md",
    "LICENSE",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "pyproject.toml",
    "Dockerfile",
    "compose.challenge.yml",
    ".gitignore",
    ".dockerignore",
    ".env.example",
)
FORBIDDEN_ITEM_SUFFIXES = (".suo", ".user", ".vs")
FORBIDDEN_ITEM_PATTERNS = (
    re.compile(r"(?i)[a-z]:[\\/]"),
    re.compile(r"(?i)/home/"),
    re.compile(r"(?i)\b[a-z0-9]+\.dev\.[a-z0-9]+\b"),
    re.compile(r"(?i)-baseline\b"),
    re.compile(r"(?i)-full-stack\b"),
)


def solution_text() -> str:
    return SOLUTION.read_text(encoding="utf-8")


def solution_items() -> list[str]:
    items: list[str] = []
    inside = False
    for line in solution_text().splitlines():
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
    return items


def declared_names() -> dict[str, str]:
    names: dict[str, str] = {}
    for line in solution_text().splitlines():
        match = PROJECT_LINE.match(line.strip())
        if match:
            names[match.group(2)] = match.group(1)
    return names


def test_solution_file_exists():
    assert SOLUTION.is_file()
    assert solution_text().startswith("Microsoft Visual Studio Solution File, Format Version 12.00")


def test_solution_declares_the_required_folders():
    names = declared_names()
    missing = sorted(REQUIRED_FOLDERS - set(names))
    assert not missing, f"missing solution folders: {missing}"
    for name, project_type in names.items():
        assert project_type == SOLUTION_FOLDER_TYPE, f"{name} is not a supported solution folder"


def test_solution_configurations_are_present():
    text = solution_text()
    for configuration in REQUIRED_CONFIGURATIONS:
        assert f"{configuration}|Any CPU" in text, f"missing configuration {configuration}"


def test_every_solution_item_resolves_to_a_real_file():
    missing = []
    for item in solution_items():
        if not (ROOT / item.replace("\\", "/")).is_file():
            missing.append(item)
    assert not missing, f"solution items that do not exist: {missing}"


def test_solution_items_use_repository_relative_paths_only():
    for item in solution_items():
        assert not item.startswith("/"), item
        assert not re.match(r"^[A-Za-z]:", item), item
        assert ".." not in item.split("\\") and ".." not in item.split("/"), item
        for pattern in FORBIDDEN_ITEM_PATTERNS:
            assert not pattern.search(item), item


def test_solution_does_not_reference_per_user_artifacts():
    for item in solution_items():
        lowered = item.lower()
        for suffix in FORBIDDEN_ITEM_SUFFIXES:
            assert not lowered.endswith(suffix), item
        assert ".vs/" not in lowered.replace("\\", "/"), item


def test_required_root_files_are_present():
    missing = [name for name in REQUIRED_ROOT_FILES if not (ROOT / name).is_file()]
    assert not missing, f"missing root files: {missing}"


def test_required_directories_are_present():
    for name in ("src", "tests", "scripts", "data", "docs", "submission", "infra", ".github/workflows"):
        assert (ROOT / name).is_dir(), name


def test_github_actions_workflow_is_present():
    workflows = list((ROOT / ".github" / "workflows").glob("*.yml"))
    assert any(path.name == "challenge-ci.yml" for path in workflows)
