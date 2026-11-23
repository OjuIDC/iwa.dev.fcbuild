from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any, Callable, Mapping

HEALTH_STATE_PATH = "/tmp/fc27-discord-bot-health.json"
HEARTBEAT_INTERVAL_SECONDS = 10.0
HEARTBEAT_MAX_AGE_SECONDS = 45.0
GATEWAY_RECONNECT_GRACE_SECONDS = 90.0
DATABASE_CONNECT_TIMEOUT_SECONDS = 2
DATABASE_STATEMENT_TIMEOUT_MS = 1500
MAX_HEALTH_STATE_BYTES = 4096


def write_health_state(
    path: str | Path,
    *,
    pid: int,
    bot_enabled: bool,
    ready: bool,
    gateway_connected: bool,
    disconnected_since_monotonic: float | None,
    dispatch_enabled: bool,
    dispatch_running: bool,
    now: float | None = None,
) -> None:
    state = {
        "pid": pid,
        "bot_enabled": bot_enabled,
        "ready": ready,
        "gateway_connected": gateway_connected,
        "disconnected_since_monotonic": disconnected_since_monotonic,
        "dispatch_enabled": dispatch_enabled,
        "dispatch_running": dispatch_running,
        "heartbeat_monotonic": time.monotonic() if now is None else now,
    }
    target = Path(path)
    temporary = target.with_name(f".{target.name}.{pid}.tmp")
    temporary.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)


def read_health_state(path: str | Path) -> dict[str, Any] | None:
    try:
        payload = Path(path).read_bytes()
    except OSError:
        return None
    if not payload or len(payload) > MAX_HEALTH_STATE_BYTES:
        return None
    try:
        state = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return state if isinstance(state, dict) else None


def bot_process_is_alive(pid: int) -> bool:
    if pid <= 1 or os.name != "posix":
        return False
    command_line = Path(f"/proc/{pid}/cmdline")
    arguments: list[bytes] = []
    try:
        arguments = command_line.read_bytes().split(b"\0")
    except OSError:
        return False
    script_present = any(
        argument.endswith(b"scripts/fc27_discord_bot.py") for argument in arguments
    )
    return script_present and b"--run" in arguments


def evaluate_health_state(
    state: Mapping[str, Any] | None,
    *,
    now: float,
    expected_dispatch_enabled: bool,
    process_is_alive: Callable[[int], bool],
) -> str | None:
    if not isinstance(state, Mapping):
        return "state_unavailable"
    pid = state.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return "process_identity_invalid"
    try:
        alive = process_is_alive(pid)
    except Exception:
        alive = False
    if not alive:
        return "process_missing"
    if state.get("bot_enabled") is not True:
        return "bot_disabled"
    heartbeat = state.get("heartbeat_monotonic")
    if isinstance(heartbeat, bool) or not isinstance(heartbeat, (int, float)):
        return "heartbeat_invalid"
    if not math.isfinite(heartbeat):
        return "heartbeat_invalid"
    heartbeat_age = now - heartbeat
    if heartbeat_age < -1.0 or heartbeat_age > HEARTBEAT_MAX_AGE_SECONDS:
        return "heartbeat_stale"
    if state.get("ready") is not True:
        return "bot_not_ready"
    if state.get("dispatch_enabled") is not expected_dispatch_enabled:
        return "dispatch_configuration_mismatch"
    if expected_dispatch_enabled and state.get("dispatch_running") is not True:
        return "dispatch_not_running"
    if state.get("gateway_connected") is not True:
        disconnected = state.get("disconnected_since_monotonic")
        if isinstance(disconnected, bool) or not isinstance(disconnected, (int, float)):
            return "gateway_disconnected"
        if not math.isfinite(disconnected) or now - disconnected > GATEWAY_RECONNECT_GRACE_SECONDS:
            return "gateway_disconnected"
        if disconnected > now + 1.0:
            return "gateway_state_invalid"
        return "gateway_reconnect_grace"
    return None


def database_is_ready(
    uri_file: str | Path,
    *,
    connect: Callable[..., Any] | None = None,
) -> bool:
    try:
        uri = Path(uri_file).read_text(encoding="utf-8").strip()
        if not uri or len(uri) > 4096:
            return False
        connect_fn = connect
        if connect_fn is None:
            import psycopg

            connect_fn = psycopg.connect
        options = (
            f"-c default_transaction_read_only=on "
            f"-c statement_timeout={DATABASE_STATEMENT_TIMEOUT_MS}"
        )
        with connect_fn(
            uri,
            connect_timeout=DATABASE_CONNECT_TIMEOUT_SECONDS,
            options=options,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                return cursor.fetchone() == (1,)
    except Exception:
        return False


def _parse_bool(value: str) -> bool | None:
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    return None


def main(
    *,
    environ: Mapping[str, str] | None = None,
    output: Callable[[str], None] = print,
    process_is_alive: Callable[[int], bool] = bot_process_is_alive,
    database_check: Callable[[str | Path], bool] = database_is_ready,
    now: float | None = None,
) -> int:
    env = os.environ if environ is None else environ
    bot_enabled = _parse_bool(env.get("FC27_DISCORD_BOT_ENABLED", "false"))
    dispatch_enabled = _parse_bool(env.get("FC27_DISCORD_DISPATCH_ENABLED", "false"))
    if bot_enabled is not True:
        output("DISCORD_BOT_HEALTHCHECK=FAIL CATEGORY=bot_disabled")
        return 1
    if dispatch_enabled is None:
        output("DISCORD_BOT_HEALTHCHECK=FAIL CATEGORY=dispatch_configuration_invalid")
        return 1
    state = read_health_state(env.get("FC27_DISCORD_HEALTH_FILE", HEALTH_STATE_PATH))
    state_failure = evaluate_health_state(
        state,
        now=time.monotonic() if now is None else now,
        expected_dispatch_enabled=dispatch_enabled,
        process_is_alive=process_is_alive,
    )
    if state_failure not in (None, "gateway_reconnect_grace"):
        output(f"DISCORD_BOT_HEALTHCHECK=FAIL CATEGORY={state_failure}")
        return 1
    uri_file = env.get("FC27_DISCORD_READER_URI_FILE", "")
    if not uri_file or not database_check(uri_file):
        output("DISCORD_BOT_HEALTHCHECK=FAIL CATEGORY=database_unavailable")
        return 1
    if state_failure == "gateway_reconnect_grace":
        output("DISCORD_BOT_HEALTHCHECK=READY CATEGORY=gateway_reconnect_grace")
    else:
        output("DISCORD_BOT_HEALTHCHECK=READY")
    return 0
