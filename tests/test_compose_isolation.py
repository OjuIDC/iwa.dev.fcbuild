"""Static checks on the resolved Compose model: secret isolation, network
exposure, image pinning and fail-closed configuration. Requires the docker CLI
(only `docker compose config` is used; no containers are started)."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available")

APPROVAL_SECRETS = {
    "GRAPH_COMMISSIONER_TOKEN_SHA256",
    "APPROVAL_SIGNING_SECRET",
    "GRAPH_COMMISSIONER_TOKEN",
    "GRAPH_API_TOKEN",
}

ALL_DB_SECRETS = {
    "postgres_admin_uri",
    "fc27_migrator_password",
    "fc27_app_password",
    "fc27_dispatch_password",
    "fc27_relay_password",
    "fc27_match_results_writer_password",
    "fc27_migrator_uri",
    "fc27_app_uri",
    "discord_bot_token",
    "postgres_password",
    "redis_password",
}


@pytest.fixture(autouse=True, scope="session")
def dummy_secrets():
    """Ensure declared top-level Compose secret files exist so `docker compose config` resolves."""
    secrets_dir = ROOT / "secrets"
    created = []
    created_dir = False
    if not secrets_dir.exists():
        secrets_dir.mkdir(parents=True, exist_ok=True)
        created_dir = True
    for name in ALL_DB_SECRETS:
        f = secrets_dir / name
        if not f.exists():
            f.write_text("placeholder_secret_for_validation\n", encoding="utf-8")
            created.append(f)
    yield
    for f in created:
        try:
            f.unlink()
        except OSError:
            pass
    if created_dir:
        try:
            secrets_dir.rmdir()
        except OSError:
            pass


def _env_file(tmp_path, drop=()):
    text = (ROOT / "config.example.env").read_text(encoding="utf-8")
    text = re.sub(r"=replace_with_[a-z0-9_]+", "=placeholder_validation_only_0123456789abcdef0123456789abcdef", text)
    lines = [l for l in text.splitlines() if l.split("=", 1)[0] not in drop]
    path = tmp_path / "validation.env"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _config(env, *args):
    cmd = ["docker", "compose", "--env-file", str(env), *args, "config", "--format", "json"]
    # Isolate from the caller's environment: shell variables override --env-file.
    allowed_host_vars = {
        "PATH",
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "COMMONPROGRAMFILES",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "TEMP",
        "TMP",
    }
    clean = {k: v for k, v in os.environ.items() if k.upper() in allowed_host_vars or k.upper().startswith("DOCKER_")}
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env=clean)


def _service_secrets(svc: dict) -> set[str]:
    raw = svc.get("secrets") or []
    result = set()
    for item in raw:
        if isinstance(item, str):
            result.add(item)
        elif isinstance(item, dict):
            source = item.get("source") or item.get("target")
            if source:
                result.add(source)
    return result


@pytest.fixture(scope="module")
def model(tmp_path_factory):
    env = _env_file(tmp_path_factory.mktemp("env"))
    out = _config(env, "--profile", "bootstrap")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_no_legacy_control_plane_secrets_or_services_in_base_compose(model):
    assert set(model["services"].keys()) == {"db-bootstrap", "db-migrate", "postgres", "redis"}
    for name, svc in model["services"].items():
        env = svc.get("environment") or {}
        assert "POSTGRES_URI" not in env, f"{name} has literal POSTGRES_URI in env"
        assert "APPROVAL_SIGNING_SECRET" not in env, f"{name} receives APPROVAL_SIGNING_SECRET"
        assert "GRAPH_COMMISSIONER_TOKEN_SHA256" not in env, f"{name} receives GRAPH_COMMISSIONER_TOKEN_SHA256"
        assert "GRAPH_COMMISSIONER_TOKEN" not in env
        assert "GRAPH_API_TOKEN" not in env


def test_db_secret_mounts_per_service(model):
    for name, svc in model["services"].items():
        mounted = _service_secrets(svc)
        if name == "db-bootstrap":
            assert mounted == {
                "postgres_admin_uri",
                "fc27_migrator_password",
                "fc27_app_password",
                "fc27_dispatch_password",
                "fc27_relay_password",
                "fc27_match_results_writer_password",
            }
        elif name == "db-migrate":
            assert mounted == {"fc27_migrator_uri"}
        elif name == "postgres":
            assert mounted == {"postgres_password"}
        elif name == "redis":
            assert mounted == {"redis_password"}
        else:
            assert not (mounted & ALL_DB_SECRETS), f"{name} must not mount any DB secrets, got {mounted & ALL_DB_SECRETS}"


def test_no_db_password_in_environment(model):
    uri_pattern = re.compile(r"postgres(?:ql)?://[^:]+:[^@]+@", re.IGNORECASE)
    for name, svc in model["services"].items():
        env = svc.get("environment") or {}
        for k in env.keys():
            assert k not in ("POSTGRES_URI", "POSTGRES_ADMIN_URI"), f"{name} has {k} in environment keys"
            if k.startswith("FC27_") and k.endswith("_PASSWORD"):
                pytest.fail(f"{name} has {k} password in environment keys")
            if name != "postgres":
                assert k != "POSTGRES_PASSWORD", f"{name} has POSTGRES_PASSWORD in environment keys"

        for k, v in env.items():
            if isinstance(v, str):
                assert not uri_pattern.search(v), f"{name} has credentials embedded in env var {k}: {v}"


def test_secrets_dir_is_gitignored():
    gitignore_path = ROOT / ".gitignore"
    assert gitignore_path.is_file()
    lines = [line.strip() for line in gitignore_path.read_text(encoding="utf-8").splitlines()]
    assert any(line in ("secrets/", "secrets", "/secrets/", "/secrets") for line in lines), (
        ".gitignore must ignore secrets/ directory"
    )
    if shutil.which("git"):
        res = subprocess.run(
            ["git", "check-ignore", "secrets/test_secret"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, "git check-ignore secrets/test_secret must exit with 0"


def test_db_migrate_has_no_other_secrets(model):
    env = model["services"]["db-migrate"]["environment"]
    allowed = {"POSTGRES_URI_FILE", "PYTHONUNBUFFERED"}
    assert set(env.keys()).issubset(allowed), f"db-migrate environment keys {set(env.keys())} not subset of {allowed}"
    assert env.get("POSTGRES_URI_FILE") == "/run/secrets/fc27_migrator_uri"


def test_only_db_services_can_reach_postgres_in_base_compose(model):
    on_data = sorted(n for n, s in model["services"].items() if "fc27_data" in (s.get("networks") or {}))
    assert on_data == ["db-bootstrap", "db-migrate", "postgres"]
    assert model["networks"]["fc27_data"].get("internal") is True


def test_host_ports_bind_loopback_only(model):
    for name, svc in model["services"].items():
        for port in svc.get("ports") or []:
            assert port.get("host_ip") == "127.0.0.1", f"{name} publishes {port}"


def test_images_are_pinned(model):
    for name, svc in model["services"].items():
        image = svc.get("image")
        if image and "fc27" not in image:
            assert "@sha256:" in image and not image.split("@")[0].endswith(":latest"), f"{name}: {image}"


def test_postgres_and_redis_runtime_controls(model):
    postgres = model["services"]["postgres"]
    redis = model["services"]["redis"]

    assert postgres["environment"]["POSTGRES_PASSWORD_FILE"] == "/run/secrets/postgres_password"
    assert int(postgres["mem_limit"]) == 1_073_741_824
    assert float(postgres["cpus"]) == pytest.approx(1.5)
    assert postgres["pids_limit"] == 128
    assert postgres["stop_grace_period"] == "1m30s"
    assert postgres["logging"]["driver"] == "json-file"

    assert "--requirepass \"$$(cat /run/secrets/redis_password)\"" in redis["command"][2]
    assert int(redis["mem_limit"]) == 536_870_912
    assert float(redis["cpus"]) == pytest.approx(1.0)
    assert redis["pids_limit"] == 128
    assert redis["stop_grace_period"] == "45s"
    assert redis["logging"]["driver"] == "json-file"


@pytest.mark.parametrize("missing", ["REDIS_PASSWORD"])
def test_missing_secret_fails_closed(tmp_path, missing):
    out = _config(_env_file(tmp_path, drop=(missing,)))
    assert out.returncode != 0 and missing in out.stderr


def test_settings_read_secret_fails_closed(tmp_path, monkeypatch):
    from src import settings
    monkeypatch.delenv("POSTGRES_URI", raising=False)
    monkeypatch.setenv("POSTGRES_URI_FILE", str(tmp_path / "nonexistent_secret_file"))
    with pytest.raises(settings.ConfigError):
        settings.read_secret("POSTGRES_URI")


APPROVED_PYTHON_311_BASE = (
    "python:3.11.18-slim@sha256:064f01707f2fc8b06bae86bb43875266c6e41bac7994e77ec5dc623c4a4d0545"
)
OPERATIONAL_PYTHON_DOCKERFILES = (
    "Dockerfile.graph",
    "Dockerfile.community-worker",
    "Dockerfile.discord",
    "Dockerfile.community-eval",
)


def test_dockerfiles_pin_base_images(model):
    discovered = sorted(p.name for p in ROOT.glob("Dockerfile.*"))
    assert discovered == sorted(OPERATIONAL_PYTHON_DOCKERFILES)
    for path in ROOT.glob("Dockerfile.*"):
        text = path.read_text(encoding="utf-8")
        first = next(l for l in text.splitlines() if l.startswith("FROM"))
        assert "@sha256:" in first, path.name
        assert first == f"FROM {APPROVED_PYTHON_311_BASE}", (
            f"{path.name} must pin {APPROVED_PYTHON_311_BASE}, got {first!r}"
        )
        assert not re.search(r"python:3\.(?:1[2-9]|[2-9]\d)", text), (
            f"{path.name} must not use Python 3.12 or newer"
        )
        for forbidden_compiler in ("gcc", "g++", "build-essential"):
            assert not re.search(rf"(?<![\w-]){re.escape(forbidden_compiler)}(?![\w-])", text), (
                f"{path.name} must not add compiler toolchain {forbidden_compiler!r}"
            )
        user_lines = [
            l.strip()
            for l in text.splitlines()
            if l.strip().startswith("USER ")
        ]
        assert user_lines, f"{path.name} must specify non-root USER"
        for uline in user_lines:
            user_val = uline.split(None, 1)[1].strip()
            assert user_val not in ("root", "0", "0:0"), f"{path.name} must not run as root ({uline})"

    compose_raw = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert not re.search(r"python:3\.(?:1[2-9]|[2-9]\d)", compose_raw)

    # Verify container hardening remains intact on db-bootstrap and db-migrate
    for svc_name in ("db-bootstrap", "db-migrate"):
        svc = model["services"][svc_name]
        assert svc["build"]["dockerfile"] == "Dockerfile.graph"
        assert svc["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in svc["security_opt"]
        assert svc["init"] is True


def test_match_results_writer_secret_wired_only_to_db_bootstrap(model, tmp_path, monkeypatch):
    from src import settings

    secrets_cfg = model.get("secrets") or {}
    assert "fc27_match_results_writer_password" in secrets_cfg
    writer_sec = secrets_cfg["fc27_match_results_writer_password"]
    assert str(writer_sec.get("file", "")).replace("\\", "/").endswith(
        "secrets/fc27_match_results_writer_password"
    )

    bootstrap = model["services"]["db-bootstrap"]
    assert (
        bootstrap["environment"].get("FC27_MATCH_RESULTS_WRITER_PASSWORD_FILE")
        == "/run/secrets/fc27_match_results_writer_password"
    )
    assert "FC27_MATCH_RESULTS_WRITER_PASSWORD" not in bootstrap["environment"]
    assert "fc27_match_results_writer_password" in _service_secrets(bootstrap)

    for name, svc in model["services"].items():
        if name == "db-bootstrap":
            continue
        env = svc.get("environment") or {}
        assert "FC27_MATCH_RESULTS_WRITER_PASSWORD_FILE" not in env, (
            f"{name} must not receive FC27_MATCH_RESULTS_WRITER_PASSWORD_FILE"
        )
        assert "FC27_MATCH_RESULTS_WRITER_PASSWORD" not in env, (
            f"{name} must not receive FC27_MATCH_RESULTS_WRITER_PASSWORD"
        )
        assert "fc27_match_results_writer_password" not in _service_secrets(svc), (
            f"{name} must not mount fc27_match_results_writer_password"
        )

    rendered = json.dumps(model)
    assert "placeholder_secret_for_validation" not in rendered

    monkeypatch.delenv("FC27_MATCH_RESULTS_WRITER_PASSWORD", raising=False)
    monkeypatch.setenv(
        "FC27_MATCH_RESULTS_WRITER_PASSWORD_FILE",
        str(tmp_path / "missing_writer_password"),
    )
    with pytest.raises(settings.ConfigError):
        settings.read_secret("FC27_MATCH_RESULTS_WRITER_PASSWORD")


EXPECTED_GHCR_WORKER_IMAGE = (
    "ghcr.io/iwa-ojuidc/iwa.dev.fcbuild/fc27-community-worker"
    "@sha256:8628acc26c9c73d9360a5e47d66c10c988e03d8273b5f15bb1f7cd3e1dc1fb79"
)
EXPECTED_GHCR_DISCORD_IMAGE = (
    "ghcr.io/iwa-ojuidc/iwa.dev.fcbuild/fc27-discord-bot"
    "@sha256:b8b571088b9a490fa4a1a517c60e1f43305ad863f3ecea9fc5df636541e7e084"
)


def test_ghcr_overlay_static_contract():
    overlay_path = ROOT / "docker-compose.ghcr.yml"
    assert overlay_path.is_file(), "docker-compose.ghcr.yml must exist"
    raw = overlay_path.read_text(encoding="utf-8")

    for forbidden in ("ghp_", "gho_", "github_pat_", "PASSWORD", "SECRET", "TOKEN", ":latest", ":main"):
        assert forbidden not in raw, f"Forbidden token {forbidden!r} found in docker-compose.ghcr.yml"

    import yaml

    parsed = yaml.safe_load(raw)
    assert set(parsed.keys()) == {"services"}
    services = parsed["services"]
    assert set(services.keys()) == {"fc27-community-worker", "fc27-discord-bot"}
    assert services["fc27-community-worker"] == {"image": EXPECTED_GHCR_WORKER_IMAGE}
    assert services["fc27-discord-bot"] == {"image": EXPECTED_GHCR_DISCORD_IMAGE}

    for svc_name, svc_cfg in services.items():
        img = svc_cfg["image"]
        assert img.startswith("ghcr.io/iwa-ojuidc/iwa.dev.fcbuild/"), svc_name
        repo_part, digest_part = img.split("@", 1)
        assert ":" not in repo_part, f"{svc_name} must not include a mutable tag before @sha256: {img}"
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest_part), f"{svc_name} invalid digest: {img}"


def test_ghcr_overlay_merged_compose_preserves_invariants_and_local_build_mode(tmp_path):
    env = _env_file(tmp_path)

    # 1. Local-build mode remains supported when docker-compose.ghcr.yml is omitted
    local_out = _config(
        env,
        "-f",
        "docker-compose.yml",
        "-f",
        "docker-compose.community-worker.yml",
        "-f",
        "docker-compose.discord.yml",
        "--profile",
        "fc27-polling",
        "--profile",
        "fc27-discord",
    )
    assert local_out.returncode == 0, local_out.stderr
    local_model = json.loads(local_out.stdout)
    assert local_model["services"]["fc27-community-worker"]["build"]["dockerfile"] == "Dockerfile.community-worker"
    assert local_model["services"]["fc27-discord-bot"]["build"]["dockerfile"] == "Dockerfile.discord"

    # 2. GHCR overlay pins exact digests while preserving worker interval, Discord command, healthchecks, and hardening
    ghcr_out = _config(
        env,
        "-f",
        "docker-compose.yml",
        "-f",
        "docker-compose.community-worker.yml",
        "-f",
        "docker-compose.discord.yml",
        "-f",
        "docker-compose.ghcr.yml",
        "--profile",
        "fc27-polling",
        "--profile",
        "fc27-discord",
    )
    assert ghcr_out.returncode == 0, ghcr_out.stderr
    ghcr_model = json.loads(ghcr_out.stdout)

    worker = ghcr_model["services"]["fc27-community-worker"]
    discord = ghcr_model["services"]["fc27-discord-bot"]

    assert worker["image"] == EXPECTED_GHCR_WORKER_IMAGE
    assert discord["image"] == EXPECTED_GHCR_DISCORD_IMAGE

    assert worker["environment"]["FC27_COMMUNITY_POLL_INTERVAL_SECONDS"] == "43200"
    assert worker["environment"]["FC27_COMMUNITY_POLL_MIN_SECONDS"] == "43200"
    assert worker["environment"]["FC27_COMMUNITY_POLL_MAX_SECONDS"] == "43200"
    assert worker["environment"]["FC27_COMMUNITY_POLL_MAX_REQUESTS"] == "1"
    assert worker["environment"]["FC27_COMMUNITY_POLL_MAX_RETRIES"] == "0"
    assert worker["command"] == ["--worker", "--confirm-live-polling"]
    assert worker["healthcheck"]["test"] == ["CMD", "python", "scripts/fc27_polling_worker.py", "--status"]
    assert worker["user"] == "10001:65534"
    assert worker["read_only"] is True
    assert worker["cap_drop"] == ["ALL"]

    assert discord["command"] == ["--run"]
    assert discord["healthcheck"]["test"] == ["CMD", "python", "scripts/fc27_discord_bot.py", "--status"]
    assert discord["user"] == "10001:65534"
    assert discord["read_only"] is True
    assert discord["cap_drop"] == ["ALL"]
    assert discord["environment"]["FC27_COMMUNITY_POLLING_ENABLED"] == "false"
    assert discord["environment"]["FC27_COMMUNITY_PROVIDER_ENABLED"] == "false"


def test_ghcr_runtime_promotion_documentation():
    doc_path = ROOT / "docs" / "FC27_GHCR_RUNTIME_PROMOTION.md"
    assert doc_path.is_file(), "docs/FC27_GHCR_RUNTIME_PROMOTION.md must exist"
    text = doc_path.read_text(encoding="utf-8")
    assert EXPECTED_GHCR_WORKER_IMAGE in text
    assert EXPECTED_GHCR_DISCORD_IMAGE in text
    assert "docker-compose.ghcr.yml" in text
    assert "replicas=1" in text
    assert "fc27-rollback/" in text
    assert "Local-Build Mode" in text


def test_optional_overlays_join_fc27_data_and_use_transitional_fc27_app_uri(tmp_path):
    """Verify current Discord and community-worker overlay network and DB role posture."""
    env = _env_file(tmp_path)
    out = _config(
        env,
        "-f",
        "docker-compose.yml",
        "-f",
        "docker-compose.community-worker.yml",
        "-f",
        "docker-compose.discord.yml",
        "--profile",
        "bootstrap",
        "--profile",
        "fc27-polling",
        "--profile",
        "fc27-discord",
    )
    assert out.returncode == 0, out.stderr
    merged = json.loads(out.stdout)

    on_data = sorted(
        name for name, svc in merged["services"].items() if "fc27_data" in (svc.get("networks") or {})
    )
    assert on_data == [
        "db-bootstrap",
        "db-migrate",
        "fc27-community-worker",
        "fc27-discord-bot",
        "postgres",
    ]

    discord = merged["services"]["fc27-discord-bot"]
    worker = merged["services"]["fc27-community-worker"]
    assert discord["environment"]["POSTGRES_URI_FILE"] == "/run/secrets/fc27_app_uri"
    assert worker["environment"]["POSTGRES_URI_FILE"] == "/run/secrets/fc27_app_uri"
    assert _service_secrets(discord) == {"discord_bot_token", "fc27_app_uri"}
    assert _service_secrets(worker) == {"fc27_app_uri"}

    grants_sql = (ROOT / "migrations" / "post" / "grants.sql").read_text(encoding="utf-8")
    assert "GRANT SELECT, INSERT ON public.fc27_matches TO fc27_app;" in grants_sql
    assert "fc27_discord_reader" not in grants_sql


def test_deployment_and_security_documentation_reconciliation():
    """Verify docs/DEPLOYMENT_AND_SECURITY.md and docs/FC27_BACKUP_AND_RECOVERY.md reflect live boundaries."""
    dep_doc = (ROOT / "docs" / "DEPLOYMENT_AND_SECURITY.md").read_text(encoding="utf-8")
    backup_doc = (ROOT / "docs" / "FC27_BACKUP_AND_RECOVERY.md").read_text(encoding="utf-8")

    assert "only graph-api joins" not in dep_doc.lower()
    assert "MIGRATE_SETUP_CHECKPOINTS" not in dep_doc
    for required_token in (
        "db-migrate",
        "src.db_migrate",
        "GRAPH_RUN_MIGRATIONS=false",
        "GRAPH_CHECKPOINT_SETUP=false",
        "PostgresSaver.setup()",
        "postgres_password",
        "redis_password",
        "POSTGRES_ROLE_MODEL=FAIL",
        "DISCORD_ANALYTICS_READ_ONLY=NO",
        "SEPARATE_SECURITY_PR",
        "BLOCKED_RUNTIME_CREDENTIAL_ROTATION",
        "PARTIAL_FC27_DOMAIN_TABLES_ONLY",
        "fc27_match_team_stats",
        "HISTORICAL_RECOVERY_VERIFIED=NO",
    ):
        assert required_token in dep_doc, f"Missing {required_token!r} in docs/DEPLOYMENT_AND_SECURITY.md"

    for required_backup_token in (
        "PARTIAL_FC27_DOMAIN_TABLES_ONLY",
        "FC27_DOMAIN_TABLES",
        "derived manifest row-count alias",
        "HISTORICAL_RECOVERY_VERIFIED=NO",
        "Pending feature migration",
    ):
        assert required_backup_token in backup_doc, (
            f"Missing {required_backup_token!r} in docs/FC27_BACKUP_AND_RECOVERY.md"
        )
