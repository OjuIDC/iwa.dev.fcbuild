import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _reqs(name):
    return [l.strip() for l in (ROOT / name).read_text().splitlines() if l.strip() and not l.startswith(("#", "-r"))]


def test_pyproject_matches_runtime_requirements():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["dependencies"] == _reqs("requirements.txt")
    assert project["requires-python"] == ">=3.11"


def test_test_tools_are_not_runtime_dependencies():
    runtime = " ".join(_reqs("requirements.txt")).lower()
    assert "pytest" not in runtime and "pyyaml" not in runtime
    assert any(r.startswith("pytest") for r in _reqs("requirements-dev.txt"))


def test_all_requirements_are_upper_bounded():
    for req in _reqs("requirements.txt"):
        base = req.split(";")[0].strip()
        assert "==" in base or "<" in base, f"Unbounded dependency in requirements.txt: {req}"


def test_applied_migrations_0001_to_0012_checksums_pinned():
    import hashlib
    from src.db_migrate import migration_files
    from tests.test_db_migrate_roles import PINNED_APPLIED_MIGRATION_SHA256

    files_by_name = {path.name: path for _, path in migration_files()}
    for name, expected_sha256 in PINNED_APPLIED_MIGRATION_SHA256.items():
        assert name in files_by_name, f"missing applied migration {name}"
        path = files_by_name[name]
        text_digest = hashlib.sha256(path.read_text(encoding="utf-8").encode()).hexdigest()
        bytes_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert text_digest == expected_sha256, f"{name} text SHA-256 drifted from pinned constant"
        assert bytes_digest == expected_sha256, f"{name} raw bytes SHA-256 drifted from pinned constant"


def test_precommit_excludes_migrations_from_whitespace_fixers():
    cfg = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    assert "exclude: ^migrations/" in cfg


def test_required_repository_files_and_entrypoints_exist():
    required = (
        "Dockerfile.graph",
        "Dockerfile.community-worker",
        "Dockerfile.discord",
        "docker-compose.yml",
        "docker-compose.community-worker.yml",
        "docker-compose.discord.yml",
        "docker-compose.ghcr.yml",
        "pyproject.toml",
        "requirements.txt",
        "requirements-dev.txt",
        "README.md",
        "SECURITY.md",
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        "migrations/0001_initial.sql",
        "migrations/0012_fc27_performance_and_quarantine.sql",
        "migrations/0013_fc27_manual_match_results.sql",
        "migrations/0014_fc27_player_development.sql",
        "migrations/post/grants.sql",
        "src/db_migrate.py",
        "src/db_roles.py",
        "src/fc27_community_provider.py",
        "src/fc27_polling_worker.py",
        "src/fc27_discord_bot.py",
        "src/fc27_match_results.py",
        "src/fc27_privacy.py",
        "scripts/fc27_polling_worker.py",
        "scripts/fc27_discord_bot.py",
        "scripts/check_matchpulse_boundary.py",
    )
    for rel in required:
        assert (ROOT / rel).is_file(), f"Missing required repository file: {rel}"


def test_local_artifacts_and_data_directories_are_gitignored_and_untracked():
    import subprocess

    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".local-artifacts/" in gitignore
    assert ".local-data/" in gitignore
    proc = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    tracked = set(proc.stdout.splitlines())
    assert not any(p.startswith((".local-artifacts/", ".local-data/")) for p in tracked)


def test_single_owner_contracts_and_privacy_docstring():
    import ast
    from src.fc27_community_provider import FC27CommunityProviderDeclaration
    from src.settings import is_dispatch_enabled, is_fc27_provider_enabled

    assert is_fc27_provider_enabled() is False
    assert is_dispatch_enabled() is False
    assert FC27CommunityProviderDeclaration().raw_response_retention is False

    privacy_doc = ast.get_docstring(
        ast.parse((ROOT / "src" / "fc27_privacy.py").read_text(encoding="utf-8"))
    )
    assert privacy_doc is not None
    assert "mref_ecf_<12hex>" in privacy_doc
    assert "ec_m_<4digits>_<10hex>" not in privacy_doc
