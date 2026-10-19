from __future__ import annotations

import compileall
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = [
    "README.md", "README-VISUAL-STUDIO.md", "Dockerfile", "compose.challenge.yml", "pyproject.toml",
    "MatchPulse.Live.sln", "LICENSE", "SECURITY.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "NOTICE",
    "docs/ARCHITECTURE.md", "docs/CHALLENGE_ALIGNMENT.md", "docs/AZURE_DEPLOYMENT.md",
    "docs/GITHUB_PUBLICATION.md",
    "submission/PITCH.md", "submission/PROJECT_DESCRIPTION.md", "submission/DEMO_SCRIPT_120S.md",
    "submission/TESTING_INSTRUCTIONS.md", "submission/REGISTRATION_AND_SUBMISSION_CHECKLIST.md",
    "src/matchpulse/service.py", "src/matchpulse/synthetic.py", "src/matchpulse/foundry.py",
    "infra/main.bicep", "infra/main.parameters.example.json", "infra/validate.sh", "infra/validate.ps1",
    "scripts/preflight.py", "scripts/validate_solution.py", "scripts/smoke_test.py",
    "scripts/bootstrap.sh", "scripts/bootstrap.ps1",
    ".github/workflows/challenge-ci.yml", ".github/workflows/azure-deploy.yml",
    ".env.example", ".gitignore", ".dockerignore",
]
FORBIDDEN_NAMES = {".env", "id_rsa", "id_ed25519", "secrets.json", "credentials.json", ".npmrc", ".pypirc"}
FORBIDDEN_PARTS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".vs", ".ruff_cache", "node_modules"}
SECRET_PATTERNS = {
    "GitHub token": re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    "Azure storage key": re.compile(r"AccountKey=[A-Za-z0-9+/=]{40,}"),
    "Azure storage connection string": re.compile(r"DefaultEndpointsProtocol=https;AccountName=[^;]+;AccountKey="),
    "Private key": re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"),
    "Azure client secret": re.compile(r"(?i)AZURE_CLIENT_SECRET\s*[=:]\s*['\"]?[A-Za-z0-9~._\-]{20,}"),
    "Foundry API key with a value": re.compile(r"(?i)FOUNDRY_API_KEY\s*[=:]\s*['\"]?[A-Za-z0-9._\-]{8,}"),
    "Publish profile credential": re.compile(r"(?i)<UserPwd>[^<]{8,}</UserPwd>"),
    "Generic assigned secret": re.compile(r"(?i)['\"](?:password|client_secret|api[_-]?key)['\"]\s*[:=]\s*['\"][^'\"]{12,}['\"]"),
}
PATH_PATTERNS = {
    "user profile absolute path": re.compile(r"[A-Za-z]:\\\\?Users\\\\?[A-Za-z0-9._-]+\\\\|/home/[A-Za-z0-9._-]+/"),
    "private repository reference": re.compile(
        r"(?i)\b[a-z0-9]+\.dev\.[a-z0-9]+\b"          # host-shaped internal repository names
        r"|\b[a-z0-9]{2,10}-baseline\b"                # internal baseline codenames
        r"|\b[a-z0-9]+-[a-z0-9]{4,}-full-stack\b"      # internal compose project names
    ),
    "private host reference": re.compile(r"(?i)sharepoint\.com|1drv\.ms|myvisualstudio\.com"),
}
ALLOWED_TEXT_EXCEPTIONS = {
    # The guard scripts necessarily contain the patterns they search for, so
    # they are excluded from the private-reference scan they themselves run.
    "scripts/preflight.py",
    "scripts/validate_solution.py",
    # Private-integration guidance. This file is excluded from the public cut.
    "UPSTREAM_INTEGRATION.md",
}

def fail(message: str) -> None:
    print(f"FAIL: {message}")
    raise SystemExit(1)


def iter_files():
    for path in ROOT.rglob("*"):
        if any(part in FORBIDDEN_PARTS for part in path.parts):
            continue
        if path.is_file():
            yield path


def main() -> int:
    missing = [item for item in REQUIRED if not (ROOT / item).is_file()]
    if missing:
        fail(f"missing required files: {missing}")

    for path in iter_files():
        relative = path.relative_to(ROOT).as_posix()
        if path.name in FORBIDDEN_NAMES:
            fail(f"forbidden file present: {relative}")
        if path.stat().st_size >= 2_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                fail(f"possible {label} in {relative}")
        for label, pattern in PATH_PATTERNS.items():
            if pattern.search(text) and relative not in ALLOWED_TEXT_EXCEPTIONS:
                fail(f"possible {label} in {relative}")

    if not compileall.compile_dir(ROOT / "src", quiet=1):
        fail("Python compilation failed")

    solution = (ROOT / "MatchPulse.Live.sln").read_text(encoding="utf-8")
    for configuration in ("Debug", "Release", "ChallengeDemo"):
        if f"{configuration}|Any CPU" not in solution:
            fail(f"solution configuration missing: {configuration}")

    pitch = (ROOT / "submission/PITCH.md").read_text(encoding="utf-8")
    if len(pitch) > 1_600:
        fail("elevator pitch is too long for comfortable form entry")

    video = (ROOT / "submission/DEMO_SCRIPT_120S.md").read_text(encoding="utf-8")
    for marker in ("0:00", "1:47", "1:53"):
        if marker not in video:
            fail(f"demo script missing time marker {marker}")

    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    if "CHALLENGE_SYNTHETIC_ONLY=true" not in env_example:
        fail("synthetic-only gate not enabled in .env.example")
    if not re.search(r"(?m)^FOUNDRY_API_KEY=\s*$", env_example):
        fail(".env.example must not carry a Foundry API key value")

    parameters = (ROOT / "infra/main.parameters.example.json").read_text(encoding="utf-8")
    if re.search(r"(?i)(AccountKey=|clientSecret|apiKey)\"?\s*:\s*\"[^\"]+", parameters):
        fail("infra parameter example contains a secret value")

    print("PASS: required submission files present")
    print("PASS: source compiles")
    print("PASS: no obvious secrets or forbidden files detected")
    print("PASS: synthetic-only gate documented")
    print("PASS: solution configurations present")
    print("ACTION REQUIRED: register, deploy Azure/Foundry, publish repository, record video, and submit URLs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
