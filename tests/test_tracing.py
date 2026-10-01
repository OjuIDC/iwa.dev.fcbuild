"""Langfuse tracing: disabled by default, fail-closed config, thread correlation, redaction.
Uses the real Langfuse SDK with an in-memory OpenTelemetry exporter (no network)."""
from __future__ import annotations

import json

import pytest

from src.settings import ConfigError
from src.tracing import LangfuseTracer, NullTracer, redact, tracer_from_env


def test_langfuse_disabled_by_default(monkeypatch):
    monkeypatch.delenv("LANGFUSE_ENABLED", raising=False)
    assert isinstance(tracer_from_env(), NullTracer)


@pytest.mark.parametrize("missing", ["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST"])
def test_langfuse_enabled_without_secrets_fails_closed(monkeypatch, missing):
    monkeypatch.setenv("LANGFUSE_ENABLED", "true")
    for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST"):
        monkeypatch.setenv(k, "x" * 40)
    monkeypatch.delenv(missing)
    with pytest.raises(ConfigError):
        tracer_from_env()


def test_redaction_rules():
    data = {"GRAPH_SERVICE_TOKEN": "abc", "nested": {"signature": "f" * 64, "note": "ok"},
            "auth": "Bearer " + "a" * 40, "prompt": "word " * 100}
    out = redact(data)
    assert out["GRAPH_SERVICE_TOKEN"] == "[REDACTED]" and out["nested"]["signature"] == "[REDACTED]"
    assert out["auth"] == "[REDACTED]" and out["prompt"].startswith("[OMITTED") and out["nested"]["note"] == "ok"
    assert redact(data, capture_content=True)["prompt"] == "word " * 100


def test_graph_trace_is_correlated_and_redacted(pg_uri):
    langfuse = pytest.importorskip("langfuse")
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from fastapi.testclient import TestClient
    from src.approval_store import ApprovalStore
    from src.graph import GraphDeps, GraphRunner
    from src.graph_api import create_app
    from tests.helpers import SERVICE_TOKEN, SIGNING_SECRET, settings
    from tests.test_langgraph_flow import FakeRoles, approve, dashboard, run

    exporter = InMemorySpanExporter()
    client = langfuse.Langfuse(public_key="pk-lf-test", secret_key="sk-lf-test", base_url="http://127.0.0.1:9",
                               span_exporter=exporter, mask=lambda *, data, **_: redact(data))
    tracer = LangfuseTracer(client)
    store = ApprovalStore(pg_uri)
    runner = GraphRunner.from_conninfo(pg_uri, GraphDeps(mcp=lambda t, a: dashboard(), role_runner=FakeRoles(invalid={"scout"}),
                                                        registry_factory=lambda: None, decision_reader=store.load, tracer=tracer))
    try:
        app = create_app(settings(pg_uri), runner=runner, publisher=lambda c, p: None)
        with TestClient(app) as c:
            tid = run(c)["thread_id"]
            assert approve(c, tid, owner="analyst").status_code == 200
        client.flush()
    finally:
        runner.pool.close()
    spans = exporter.get_finished_spans()
    names = {s.name for s in spans}
    assert {"node:search", "node:scout", "node:coach", "node:prepare_approval", "node:approval", "node:finalize_decision",
            "approval_interrupt", "approval_resumed", "role_call", "validation_failure"} <= names
    expected_trace = int(client.create_trace_id(seed=tid), 16)
    assert all(s.context.trace_id == expected_trace for s in spans)
    blob = json.dumps([dict(s.attributes) for s in spans], default=str)
    for s in spans:
        attrs = dict(s.attributes)
        assert attrs.get("langfuse.observation.metadata.fc_version") == "27", (s.name, attrs)
        assert attrs.get("langfuse.observation.metadata.platform") == "common-gen5"
    assert SERVICE_TOKEN not in blob and SIGNING_SECRET not in blob
