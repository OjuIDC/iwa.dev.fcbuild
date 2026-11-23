from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

from src import fc27_discord_health as health


def _state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "pid": 123,
        "bot_enabled": True,
        "ready": True,
        "gateway_connected": True,
        "disconnected_since_monotonic": None,
        "dispatch_enabled": True,
        "dispatch_running": True,
        "heartbeat_monotonic": 100.0,
    }
    state.update(overrides)
    return state


def _main_env(path: Path, **overrides: str) -> dict[str, str]:
    env = {
        "FC27_DISCORD_BOT_ENABLED": "true",
        "FC27_DISCORD_DISPATCH_ENABLED": "true",
        "FC27_DISCORD_HEALTH_FILE": str(path),
        "FC27_DISCORD_READER_URI_FILE": "/run/secrets/fc27_discord_reader_uri",
    }
    env.update(overrides)
    return env


def test_healthcheck_entrypoint_is_lightweight_and_compose_wired() -> None:
    root = Path(__file__).resolve().parents[1]
    script = root / "scripts" / "fc27_discord_healthcheck.py"
    compose = (root / "docker-compose.discord.yml").read_text(encoding="utf-8")
    dockerfile = (root / "Dockerfile.discord").read_text(encoding="utf-8")
    assert script.is_file()
    assert '"scripts/fc27_discord_healthcheck.py"' in compose
    assert '"scripts/fc27_discord_healthcheck.py"' in dockerfile
    assert "--status" not in compose.split("healthcheck:", 1)[1].split("restart:", 1)[0]


def test_ready_gateway_dispatch_and_database_pass(tmp_path: Path) -> None:
    state_file = tmp_path / "health.json"
    state_file.write_text(json.dumps(_state()), encoding="utf-8")
    output: list[str] = []
    rc = health.main(
        environ=_main_env(state_file),
        output=output.append,
        process_is_alive=lambda pid: pid == 123,
        database_check=lambda path: path == "/run/secrets/fc27_discord_reader_uri",
        now=101.0,
    )
    assert rc == 0
    assert output == ["DISCORD_BOT_HEALTHCHECK=READY"]


def test_probe_fails_when_bot_has_not_reached_ready(tmp_path: Path) -> None:
    state_file = tmp_path / "health.json"
    state_file.write_text(json.dumps(_state(ready=False)), encoding="utf-8")
    output: list[str] = []
    rc = health.main(
        environ=_main_env(state_file),
        output=output.append,
        process_is_alive=lambda pid: True,
        database_check=lambda path: True,
        now=101.0,
    )
    assert rc == 1
    assert output == ["DISCORD_BOT_HEALTHCHECK=FAIL CATEGORY=bot_not_ready"]


def test_probe_fails_when_bot_process_is_missing(tmp_path: Path) -> None:
    state_file = tmp_path / "health.json"
    state_file.write_text(json.dumps(_state()), encoding="utf-8")
    output: list[str] = []
    rc = health.main(
        environ=_main_env(state_file),
        output=output.append,
        process_is_alive=lambda pid: False,
        database_check=lambda path: True,
        now=101.0,
    )
    assert rc == 1
    assert output == ["DISCORD_BOT_HEALTHCHECK=FAIL CATEGORY=process_missing"]


def test_gateway_disconnect_is_graced_then_fails() -> None:
    state = _state(
        gateway_connected=False,
        disconnected_since_monotonic=95.0,
    )
    assert (
        health.evaluate_health_state(
            state,
            now=100.0,
            expected_dispatch_enabled=True,
            process_is_alive=lambda pid: True,
        )
        == "gateway_reconnect_grace"
    )
    expired_state = {**state, "heartbeat_monotonic": 185.0}
    assert (
        health.evaluate_health_state(
            expired_state,
            now=186.0,
            expected_dispatch_enabled=True,
            process_is_alive=lambda pid: True,
        )
        == "gateway_disconnected"
    )


def test_probe_fails_when_dispatch_is_unexpectedly_disabled(tmp_path: Path) -> None:
    state_file = tmp_path / "health.json"
    state_file.write_text(
        json.dumps(_state(dispatch_enabled=False, dispatch_running=False)),
        encoding="utf-8",
    )
    output: list[str] = []
    rc = health.main(
        environ=_main_env(state_file),
        output=output.append,
        process_is_alive=lambda pid: True,
        database_check=lambda path: True,
        now=101.0,
    )
    assert rc == 1
    assert output == [
        "DISCORD_BOT_HEALTHCHECK=FAIL CATEGORY=dispatch_configuration_mismatch"
    ]


def test_probe_fails_when_dispatch_task_stops(tmp_path: Path) -> None:
    state_file = tmp_path / "health.json"
    state_file.write_text(
        json.dumps(_state(dispatch_running=False)),
        encoding="utf-8",
    )
    output: list[str] = []
    rc = health.main(
        environ=_main_env(state_file),
        output=output.append,
        process_is_alive=lambda pid: True,
        database_check=lambda path: True,
        now=101.0,
    )
    assert rc == 1
    assert output == ["DISCORD_BOT_HEALTHCHECK=FAIL CATEGORY=dispatch_not_running"]


def test_database_probe_is_read_only_and_bounded(tmp_path: Path) -> None:
    uri_file = tmp_path / "reader_uri"
    uri_file.write_text("synthetic-reader-uri", encoding="utf-8")
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def connect(uri: str, **kwargs: Any) -> Any:
        calls.append(((uri,), kwargs))
        raise TimeoutError("synthetic timeout")

    assert not health.database_is_ready(uri_file, connect=connect)
    assert len(calls) == 1
    assert calls[0][0][0] == "synthetic-reader-uri"
    assert calls[0][1]["connect_timeout"] == health.DATABASE_CONNECT_TIMEOUT_SECONDS
    assert f"statement_timeout={health.DATABASE_STATEMENT_TIMEOUT_MS}" in calls[0][1]["options"]
    assert "default_transaction_read_only=on" in calls[0][1]["options"]


def test_database_probe_executes_only_select_one(tmp_path: Path) -> None:
    uri_file = tmp_path / "reader_uri"
    uri_file.write_text("synthetic-reader-uri", encoding="utf-8")
    statements: list[str] = []

    class Cursor:
        def __enter__(self) -> Cursor:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def execute(self, statement: str) -> None:
            statements.append(statement)

        def fetchone(self) -> tuple[int]:
            return (1,)

    class Connection:
        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def cursor(self) -> Cursor:
            return Cursor()

    assert health.database_is_ready(uri_file, connect=lambda *args, **kwargs: Connection())
    assert statements == ["SELECT 1"]


def test_probe_does_not_import_discord_provider_or_network_clients() -> None:
    root = Path(__file__).resolve().parents[1]
    paths = [
        root / "src" / "fc27_discord_health.py",
        root / "scripts" / "fc27_discord_healthcheck.py",
    ]
    forbidden = {
        "discord",
        "aiohttp",
        "http",
        "httpx",
        "requests",
        "socket",
        "urllib",
        "fc27_community_provider",
    }
    imported: set[str] = set()
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".", 1)[0])
    assert not imported & forbidden
    assert "fc27_discord_bot" not in imported
    source = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    assert "tree.sync" not in source
    assert "send_message" not in source
    assert "run_result_backfill" not in source


def test_health_output_never_includes_secret_values(tmp_path: Path) -> None:
    state_file = tmp_path / "health.json"
    state_file.write_text(json.dumps(_state()), encoding="utf-8")
    sentinel = "never-echo-this-synthetic-value"
    output: list[str] = []
    env = _main_env(state_file)
    env["DISCORD_BOT_TOKEN"] = sentinel
    env["FC27_DISCORD_READER_URI_FILE"] = sentinel
    rc = health.main(
        environ=env,
        output=output.append,
        process_is_alive=lambda pid: True,
        database_check=lambda path: True,
        now=101.0,
    )
    assert rc == 0
    assert sentinel not in "\n".join(output)


def test_health_snapshot_is_atomic_bounded_and_has_no_credentials(tmp_path: Path) -> None:
    path = tmp_path / "health.json"
    health.write_health_state(
        path,
        pid=123,
        bot_enabled=True,
        ready=True,
        gateway_connected=True,
        disconnected_since_monotonic=None,
        dispatch_enabled=True,
        dispatch_running=True,
        now=100.0,
    )
    payload = path.read_text(encoding="utf-8")
    state = health.read_health_state(path)
    assert state is not None
    assert state["pid"] == 123
    assert len(payload.encode("utf-8")) <= health.MAX_HEALTH_STATE_BYTES
    assert "token" not in payload.lower()
    assert "uri" not in payload.lower()
    assert not list(tmp_path.glob("*.tmp"))


def test_compose_healthcheck_is_bounded_and_keeps_start_period() -> None:
    import yaml

    root = Path(__file__).resolve().parents[1]
    doc = yaml.safe_load((root / "docker-compose.discord.yml").read_text(encoding="utf-8"))
    healthcheck = doc["services"]["fc27-discord-bot"]["healthcheck"]
    assert healthcheck["test"] == ["CMD", "python", "scripts/fc27_discord_healthcheck.py"]
    assert healthcheck["interval"] == "30s"
    assert healthcheck["timeout"] == "6s"
    assert healthcheck["retries"] == 3
    assert healthcheck["start_period"] == "90s"


def test_existing_status_command_and_disabled_backfill_defaults_remain() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "scripts" / "fc27_discord_bot.py").read_text(encoding="utf-8")
    compose = (root / "docker-compose.discord.yml").read_text(encoding="utf-8")
    assert '"--status"' in source
    assert 'print("DISCORD_GLOBAL_COMMAND_COUNT=0")' in source
    assert 'print("DISCORD_GUILD_TOP_LEVEL_COMMAND_COUNT=14")' in source
    assert "FC27_DISCORD_BACKFILL_ENABLED:-false" in compose
    assert "FC27_DISCORD_BACKFILL_OPERATOR_ROLE_IDS:-" in compose


def test_existing_authorization_and_destination_forwarding_remain() -> None:
    import yaml

    root = Path(__file__).resolve().parents[1]
    doc = yaml.safe_load((root / "docker-compose.discord.yml").read_text(encoding="utf-8"))
    env = doc["services"]["fc27-discord-bot"]["environment"]
    for key in (
        "FC27_DISCORD_ADMIN_ROLE_IDS",
        "FC27_DISCORD_MANAGER_ROLE_IDS",
        "FC27_DISCORD_CAPTAIN_ROLE_IDS",
        "FC27_DISCORD_IDENTITY_OPERATOR_ROLE_IDS",
        "FC27_DISCORD_BACKFILL_OPERATOR_ROLE_IDS",
        "FC27_DISCORD_RESULTS_CHANNEL_ID",
        "FC27_DISCORD_TEAM_CHAT_CHANNEL_ID",
    ):
        assert key in env
    assert env["FC27_DISCORD_BACKFILL_ENABLED"] == "${FC27_DISCORD_BACKFILL_ENABLED:-false}"
