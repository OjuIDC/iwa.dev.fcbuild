"""Optional Langfuse tracing (self-hosted). Disabled by default.

    LANGFUSE_ENABLED=false            (default) -> NullTracer, no network calls
    LANGFUSE_ENABLED=true             requires LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST
                                      (or *_FILE); missing values fail at startup.
    LANGFUSE_CAPTURE_CONTENT=false    (default) long free text is replaced by a length marker

Traces are keyed by thread_id (deterministic trace id), so every node, role
call, validation failure, MCP failure and the approval interrupt/resume of one
run land in one trace. Every observation carries fc_version and platform.

Redaction is applied twice: before data is handed to the SDK and again by the
SDK ``mask`` hook. Keys that look like credentials (token, secret, signature,
password, authorization, api key, nonce, cookie) are always replaced.
"""
from __future__ import annotations

import contextlib
import logging
import re
from typing import Any, Generator

from .settings import DEFAULT_PLATFORM, FC_VERSION, ConfigError, env_bool, read_secret

log = logging.getLogger("fc27.tracing")
_SECRET_KEY = re.compile(r"(token|secret|signature|password|passwd|authorization|api[_-]?key|nonce|cookie|credential)", re.I)
_MAX_TEXT = 200


def redact(data: Any, capture_content: bool = False, _depth: int = 0) -> Any:
    if _depth > 8:
        return "[TRUNCATED]"
    if isinstance(data, dict):
        return {k: ("[REDACTED]" if isinstance(k, str) and _SECRET_KEY.search(k) else redact(v, capture_content, _depth + 1))
                for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [redact(v, capture_content, _depth + 1) for v in list(data)[:100]]
    if isinstance(data, str):
        if re.fullmatch(r"(Bearer\s+)?[A-Za-z0-9_\-]{32,}", data):
            return "[REDACTED]"
        if not capture_content and len(data) > _MAX_TEXT:
            return f"[OMITTED {len(data)} chars]"
    return data


class NullTracer:
    enabled = False

    @contextlib.contextmanager
    def span(self, name: str, thread_id: str, metadata: dict | None = None, as_type: str = "span") -> Generator[None]:
        yield None

    def event(self, name: str, thread_id: str, metadata: dict | None = None, level: str = "DEFAULT") -> None:
        return None

    def flush(self) -> None:
        return None


class LangfuseTracer:
    enabled = True

    def __init__(self, client, capture_content: bool = False, platform: str = DEFAULT_PLATFORM):
        self.client, self.capture, self.platform = client, capture_content, platform

    def _meta(self, thread_id: str, metadata: dict | None) -> dict:
        return redact({"fc_version": FC_VERSION, "platform": self.platform, "thread_id": thread_id, **(metadata or {})},
                      self.capture)

    def _ctx(self, thread_id: str) -> dict:
        return {"trace_id": self.client.create_trace_id(seed=thread_id)}

    @contextlib.contextmanager
    def span(self, name: str, thread_id: str, metadata: dict | None = None, as_type: str = "span"):
        try:
            cm = self.client.start_as_current_observation(trace_context=self._ctx(thread_id), name=name, as_type=as_type,
                                                          metadata=self._meta(thread_id, metadata))
        except Exception:  # tracing must never break a run
            log.warning("langfuse span start failed")
            yield None
            return
        with cm as obs:
            yield obs

    def event(self, name: str, thread_id: str, metadata: dict | None = None, level: str = "DEFAULT") -> None:
        try:
            self.client.create_event(trace_context=self._ctx(thread_id), name=name, level=level,
                                     metadata=self._meta(thread_id, metadata))
        except Exception:
            log.warning("langfuse event failed")

    def flush(self) -> None:
        try:
            self.client.flush()
        except Exception:
            log.warning("langfuse flush failed; traces can be re-sent after Langfuse is reachable")


def tracer_from_env():
    if not env_bool("LANGFUSE_ENABLED", False):
        return NullTracer()
    public = read_secret("LANGFUSE_PUBLIC_KEY")
    secret = read_secret("LANGFUSE_SECRET_KEY")
    host = read_secret("LANGFUSE_HOST")
    capture = env_bool("LANGFUSE_CAPTURE_CONTENT", False)
    try:
        from langfuse import Langfuse  # type: ignore
    except ImportError as exc:
        raise ConfigError("LANGFUSE_ENABLED=true requires the 'langfuse' package (requirements-langfuse.txt)") from exc
    client = Langfuse(public_key=public, secret_key=secret, base_url=host, environment="fc27",
                      mask=lambda *, data, **_: redact(data, capture))
    return LangfuseTracer(client, capture)
