"""Durable PostgreSQL service liveness heartbeats for FC27 runtimes.

Records one bounded heartbeat row per reviewed service through the
SECURITY DEFINER function ``public.fc27_record_service_heartbeat``. The runtime
never receives direct INSERT/UPDATE privileges on the heartbeat table.

Design guarantees:
- Fixed reviewed service keys only.
- Non-reversible, process-local instance digest (SHA-256 of OS entropy).
- PostgreSQL ``clock_timestamp()`` is authoritative; the caller never supplies
  the observed time.
- One background thread per runtime; records never overlap.
- Bounded connect + statement timeouts.
- Heartbeat failure never crashes the primary service.
- Sanitized, rate-limited diagnostics; no DSN, secret path, digest, or raw
  exception payload is ever logged.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

ALLOWED_SERVICE_KEYS: frozenset[str] = frozenset(
    {"fc27-community-worker", "fc27-discord-bot"}
)
ALLOWED_RUNTIME_STATUSES: frozenset[str] = frozenset(
    {"READY", "STARTING", "DEGRADED", "STOPPING"}
)
RUNTIME_STATUS_STARTING = "STARTING"
RUNTIME_STATUS_READY = "READY"
RUNTIME_STATUS_DEGRADED = "DEGRADED"
RUNTIME_STATUS_STOPPING = "STOPPING"

DEFAULT_INTERVAL_SECONDS = 60
MIN_INTERVAL_SECONDS = 30
MAX_INTERVAL_SECONDS = 300
DEFAULT_CONNECT_TIMEOUT_SECONDS = 2
MIN_CONNECT_TIMEOUT_SECONDS = 1
MAX_CONNECT_TIMEOUT_SECONDS = 5
DEFAULT_STATEMENT_TIMEOUT_MS = 1500
MIN_STATEMENT_TIMEOUT_MS = 500
MAX_STATEMENT_TIMEOUT_MS = 5000
WARNING_INTERVAL_SECONDS = 300
MAX_URI_LENGTH = 4096

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_RECORD_SQL = "SELECT public.fc27_record_service_heartbeat(%s, %s, %s);"


def generate_instance_digest() -> str:
    """Return a non-sensitive, non-reversible per-process instance digest.

    Derived purely from OS entropy, never from container ID, hostname, machine
    name, process ID, guild/channel/user ID, database role, or credential.
    """
    return hashlib.sha256(os.urandom(32)).hexdigest()


def is_valid_instance_digest(value: object) -> bool:
    return isinstance(value, str) and bool(_DIGEST_RE.fullmatch(value))


def _clamp_int(value: Any, *, default: int, low: int, high: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    if parsed < low:
        return low
    if parsed > high:
        return high
    return parsed


def _parse_enabled(value: Any) -> bool:
    if value is None:
        return True
    return str(value).strip().lower() not in {"0", "false", "no", "off", ""}


@dataclass(frozen=True)
class HeartbeatConfig:
    """Validated, bounded heartbeat configuration."""

    enabled: bool = True
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    connect_timeout_seconds: int = DEFAULT_CONNECT_TIMEOUT_SECONDS
    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS

    def __post_init__(self) -> None:
        if not MIN_INTERVAL_SECONDS <= self.interval_seconds <= MAX_INTERVAL_SECONDS:
            raise ValueError("interval_seconds out of bounds")
        if not (
            MIN_CONNECT_TIMEOUT_SECONDS
            <= self.connect_timeout_seconds
            <= MAX_CONNECT_TIMEOUT_SECONDS
        ):
            raise ValueError("connect_timeout_seconds out of bounds")
        if not (
            MIN_STATEMENT_TIMEOUT_MS
            <= self.statement_timeout_ms
            <= MAX_STATEMENT_TIMEOUT_MS
        ):
            raise ValueError("statement_timeout_ms out of bounds")


def load_heartbeat_config(environ: Mapping[str, str] | None = None) -> HeartbeatConfig:
    """Build a bounded config from ``FC27_SERVICE_HEARTBEAT_*`` environment."""
    env = os.environ if environ is None else environ
    return HeartbeatConfig(
        enabled=_parse_enabled(env.get("FC27_SERVICE_HEARTBEAT_ENABLED", "true")),
        interval_seconds=_clamp_int(
            env.get("FC27_SERVICE_HEARTBEAT_INTERVAL_SECONDS"),
            default=DEFAULT_INTERVAL_SECONDS,
            low=MIN_INTERVAL_SECONDS,
            high=MAX_INTERVAL_SECONDS,
        ),
        connect_timeout_seconds=_clamp_int(
            env.get("FC27_SERVICE_HEARTBEAT_CONNECT_TIMEOUT_SECONDS"),
            default=DEFAULT_CONNECT_TIMEOUT_SECONDS,
            low=MIN_CONNECT_TIMEOUT_SECONDS,
            high=MAX_CONNECT_TIMEOUT_SECONDS,
        ),
        statement_timeout_ms=_clamp_int(
            env.get("FC27_SERVICE_HEARTBEAT_STATEMENT_TIMEOUT_MS"),
            default=DEFAULT_STATEMENT_TIMEOUT_MS,
            low=MIN_STATEMENT_TIMEOUT_MS,
            high=MAX_STATEMENT_TIMEOUT_MS,
        ),
    )


def read_uri_file(uri_file: str | Path | None) -> str | None:
    """Read a DSN from a secret file without ever logging its contents."""
    if not uri_file:
        return None
    try:
        raw = Path(uri_file).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw or len(raw) > MAX_URI_LENGTH:
        return None
    return raw


class ServiceHeartbeat:
    """Best-effort durable heartbeat writer for one reviewed FC27 service."""

    def __init__(
        self,
        service_key: str,
        uri: str | None,
        *,
        config: HeartbeatConfig | None = None,
        instance_digest: str | None = None,
        connect_fn: Callable[..., Any] | None = None,
        time_fn: Callable[[], float] = time.monotonic,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        if service_key not in ALLOWED_SERVICE_KEYS:
            raise ValueError(f"unsupported heartbeat service key: {service_key!r}")
        self.service_key = service_key
        self._cfg = config or HeartbeatConfig()
        self._uri = uri
        self._digest = instance_digest or generate_instance_digest()
        if not is_valid_instance_digest(self._digest):
            raise ValueError("invalid instance digest")
        self._connect_fn = connect_fn
        self._time_fn = time_fn
        self._logger = logger or print
        self._lock = threading.Lock()
        self._desired_status = RUNTIME_STATUS_STARTING
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_warning_at = float("-inf")
        self._warnings_emitted = 0
        self._discarded = 0

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.enabled and self._uri)

    @property
    def instance_digest(self) -> str:
        return self._digest

    def start(self, initial_status: str = RUNTIME_STATUS_STARTING) -> None:
        """Start the background heartbeat loop and record ``initial_status``."""
        if not self.enabled or self._thread is not None:
            return
        if initial_status not in ALLOWED_RUNTIME_STATUSES:
            raise ValueError(f"invalid heartbeat status: {initial_status!r}")
        with self._lock:
            self._desired_status = initial_status
        thread = threading.Thread(
            target=self._run_loop,
            name=f"fc27-heartbeat-{self.service_key}",
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def set_status(self, status: str) -> None:
        """Update the desired status and wake the loop for prompt persistence."""
        if status not in ALLOWED_RUNTIME_STATUSES:
            raise ValueError(f"invalid heartbeat status: {status!r}")
        with self._lock:
            self._desired_status = status
        self._wake_event.set()

    def current_status(self) -> str:
        with self._lock:
            return self._desired_status

    def stop(self, *, join_timeout_seconds: float = 5.0) -> None:
        """Best-effort STOPPING write, then terminate the loop."""
        if self._thread is None and not self.enabled:
            return
        with self._lock:
            self._desired_status = RUNTIME_STATUS_STOPPING
        # Stop the loop first so the final STOPPING write never overlaps it.
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=join_timeout_seconds)
        self._record(RUNTIME_STATUS_STOPPING)

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                status = self._desired_status
            self._record(status)
            self._wake_event.clear()
            self._wake_event.wait(timeout=float(self._cfg.interval_seconds))
        with self._lock:
            status = self._desired_status
        self._record(status)

    def _record(self, status: str) -> None:
        if not self.enabled or not self._uri:
            return
        connect_fn = self._connect_fn
        try:
            if connect_fn is None:
                import psycopg

                connect_fn = psycopg.connect
            options = (
                f"-c statement_timeout={self._cfg.statement_timeout_ms}"
            )
            with connect_fn(
                self._uri,
                connect_timeout=self._cfg.connect_timeout_seconds,
                options=options,
            ) as conn:
                with conn.cursor() as cur:
                    cur.execute(_RECORD_SQL, (self.service_key, self._digest, status))
                    cur.fetchone()
            self._discarded = 0
        except Exception as exc:  # best-effort: never propagate
            self._warn_once(type(exc).__name__)

    def _warn_once(self, category: str) -> None:
        now = self._time_fn()
        with self._lock:
            if now - self._last_warning_at < WARNING_INTERVAL_SECONDS:
                self._discarded += 1
                return
            self._last_warning_at = now
            self._warnings_emitted += 1
            discarded = self._discarded
            self._discarded = 0
        suffix = f" suppressed={discarded}" if discarded else ""
        # Category only: never log DSN, secret path, digest, or raw exception text.
        self._logger(
            f"FC27_SERVICE_HEARTBEAT_WARNING service={self.service_key} "
            f"category={category}{suffix}"
        )


def build_service_heartbeat_from_env(
    service_key: str,
    *,
    uri_file: str | Path | None,
    environ: Mapping[str, str] | None = None,
    connect_fn: Callable[..., Any] | None = None,
    instance_digest: str | None = None,
    logger: Callable[[str], None] | None = None,
) -> ServiceHeartbeat:
    """Construct a heartbeat writer for a fixed service from environment config."""
    cfg = load_heartbeat_config(environ)
    uri = read_uri_file(uri_file)
    return ServiceHeartbeat(
        service_key,
        uri,
        config=cfg,
        instance_digest=instance_digest,
        connect_fn=connect_fn,
        logger=logger,
    )
