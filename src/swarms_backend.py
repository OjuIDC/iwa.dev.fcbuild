"""Swarms (``swarms`` library) backend for the role-agent boundary.

Each role is a real ``swarms.Agent`` with ``max_loops=1``, autosave off and no
tools. The agent's LLM is an adapter that routes through
:func:`src.model_selection.invoke_role`, so model selection, the LM Studio
policy and timeouts stay identical to the native backend, and every reply is
still schema-validated by :mod:`src.roles` before it can influence ranking.

Local-first: Swarms enables OpenTelemetry export of agent state to a remote
endpoint by default. This module forces ``SWARMS_TELEMETRY_ON=false`` before
importing swarms and refuses to run if telemetry is reported ready.
"""
from __future__ import annotations

import os

os.environ["SWARMS_TELEMETRY_ON"] = "false"  # must precede any swarms import
os.environ.setdefault("SWARMS_VERBOSE_GLOBAL", "False")
# On import, swarms replaces the root logging handlers and writes ERROR logs to
# ./agent_workspace/error.txt. Keep that out of the working tree (private temp
# dir) and restore our logging configuration afterwards (see _swarms()).
if not os.getenv("WORKSPACE_DIR"):
    import tempfile as _tempfile

    os.environ["WORKSPACE_DIR"] = _tempfile.mkdtemp(prefix="fc27-swarms-")

from typing import Any  # noqa: E402

from .model_selection import ModelRegistry, OpenAICompatibleClient, invoke_role  # noqa: E402
from .roles import RoleResult, build_messages, parse_output  # noqa: E402

SWARMS_VERSION_REQUIRED = "15.0.3"


class SwarmsUnavailable(RuntimeError):
    pass


def _swarms():
    import logging

    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    try:
        import swarms  # type: ignore
        from swarms import Agent  # type: ignore
        from swarms.telemetry.otel import swarm_telemetry  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised when the extra is missing
        raise SwarmsUnavailable("ROLE_BACKEND=swarms requires the 'swarms' package (requirements-swarms.txt)") from exc
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
    version = getattr(swarms, "__version__", None)
    if version is None:
        from importlib.metadata import version as _v
        version = _v("swarms")
    if version != SWARMS_VERSION_REQUIRED:
        raise SwarmsUnavailable(f"swarms {SWARMS_VERSION_REQUIRED} required, found {version}")
    if getattr(swarm_telemetry(), "ready", False):
        raise SwarmsUnavailable("swarms telemetry is enabled; refusing to run (local-first policy)")
    return Agent


class _RoleLLM:
    """Swarms LLM adapter: forwards the agent's messages to invoke_role."""

    def __init__(self, role: str, registry: ModelRegistry, client: OpenAICompatibleClient | None, system: str):
        self.role, self.registry, self.client, self.system = role, registry, client, system
        self.last: dict[str, Any] | None = None

    def run(self, task: str | None = None, *args, messages: list[dict] | None = None, **kwargs) -> str:
        # The fixed role instruction is always sent first by the adapter itself, so
        # it cannot be dropped or replaced by the framework's message assembly.
        msgs = [{"role": "system", "content": self.system}]
        msgs += [{"role": "user" if m.get("role") != "assistant" else "assistant", "content": str(m.get("content", ""))}
                 for m in (messages or []) if m.get("role") != "system"]
        if len(msgs) == 1 and task:
            msgs.append({"role": "user", "content": task})
        self.last = invoke_role(self.role, msgs, self.registry, self.client)
        return self.last["content"]


def run_role_swarms(role: str, data: dict[str, Any], *, registry: ModelRegistry | None = None,
                    client: OpenAICompatibleClient | None = None) -> RoleResult:
    Agent = _swarms()
    system, user = build_messages(role, data)
    llm = _RoleLLM(role, registry or ModelRegistry(), client, system["content"])
    try:
        agent = Agent(agent_name=f"fc27-{role}", system_prompt=system["content"], llm=llm, max_loops=1,
                      autosave=False, print_on=False, verbose=False, output_type="final", retry_attempts=1)
        text = agent.run(user["content"])
    except Exception as exc:
        return RoleResult(role=role, valid=False, error=f"swarms agent failed: {type(exc).__name__}")
    if llm.last is None:
        # Swarms returns its input when every attempt fails; never parse that as model output.
        return RoleResult(role=role, valid=False, error="swarms agent produced no model response")
    route = llm.last.get("route")
    provider: str | None = getattr(route, "provider", None)
    model: str | None = getattr(route, "model", None)
    fallback_used = bool((llm.last or {}).get("fallback_used"))
    try:
        output = parse_output(text if isinstance(text, str) else str(text))
    except ValueError as exc:
        return RoleResult(
            role=role,
            valid=False,
            error=f"invalid structured output: {type(exc).__name__}",
            provider=provider,
            model=model,
            fallback_used=fallback_used,
        )
    return RoleResult(
        role=role,
        valid=True,
        output=output,
        provider=provider,
        model=model,
        fallback_used=fallback_used,
    )
