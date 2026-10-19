"""Guarded Foundry provider tests.

These cover the optional enrichment path only. The deterministic pipeline stays
authoritative and is never allowed to depend on a model response.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from matchpulse.foundry import (
    AUTH_MANAGED_IDENTITY,
    ApiKeyCredentialProvider,
    FoundryCredentialError,
    FoundryNarrativeClient,
    FoundrySettings,
    ManagedIdentityCredentialProvider,
)
from matchpulse.models import Evidence, PersonalizedInsight

SECRET = "super-secret-foundry-key"


def item() -> PersonalizedInsight:
    return PersonalizedInsight(
        insight_id="i", match_id="m", profile_id="fan", locale="en", persona="fan",
        priority=.8, title="Shot", body="A shot reached 101 km/h.", overlay_text="101 km/h shot",
        clock_ms=1, sequence=1, severity="notable",
        evidence=Evidence(event_ids=["e1"], inputs={"speed": 101}, formula="measured speed", confidence=.9),
        stats={"speed_kmh": 101}, expires_in_ms=12000,
    )


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def _completion(content: str) -> _Response:
    return _Response({"choices": [{"message": {"content": content}}]})


def test_valid_guarded_foundry_response_is_applied(monkeypatch):
    captured: dict = {}

    def fake_post(url, headers=None, timeout=None, **kwargs):
        captured["url"] = url
        captured["headers"] = headers
        return _completion(json.dumps({
            "title": "Faster shot",
            "body": "A shot reached 101 km/h.",
            "overlay_text": "101 km/h",
        }))

    monkeypatch.setattr(httpx, "post", fake_post)
    client = FoundryNarrativeClient(FoundrySettings("https://example.test", "model", SECRET))
    output, warning = client.rewrite(item())
    assert warning is None
    assert output.title == "Faster shot"
    assert output.body == "A shot reached 101 km/h."
    assert output.overlay_text == "101 km/h"
    assert captured["headers"]["api-key"] == SECRET


def test_timeout_falls_back_to_deterministic_text(monkeypatch):
    def fake_post(*args, **kwargs):
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(httpx, "post", fake_post)
    source = item()
    client = FoundryNarrativeClient(FoundrySettings("https://example.test", "model", SECRET))
    output, warning = client.rewrite(source)
    assert output == source
    assert warning and "TimeoutException" in warning


def test_invalid_json_falls_back_to_deterministic_text(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: _completion("this is not json"))
    source = item()
    client = FoundryNarrativeClient(FoundrySettings("https://example.test", "model", SECRET))
    output, warning = client.rewrite(source)
    assert output == source
    assert warning and "JSONDecodeError" in warning


def test_schema_mismatch_falls_back_to_deterministic_text(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: _completion(json.dumps({"title": "Only a title"})))
    source = item()
    client = FoundryNarrativeClient(FoundrySettings("https://example.test", "model", SECRET))
    output, warning = client.rewrite(source)
    assert output == source
    assert warning and "ValueError" in warning


def test_service_error_falls_back_to_deterministic_text(monkeypatch):
    class Failing:
        def raise_for_status(self):
            raise httpx.HTTPStatusError("boom", request=None, response=None)

    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: Failing())
    source = item()
    client = FoundryNarrativeClient(FoundrySettings("https://example.test", "model", SECRET))
    output, warning = client.rewrite(source)
    assert output == source
    assert warning and "HTTPStatusError" in warning


def test_secret_never_appears_in_warning_or_logs(monkeypatch, caplog):
    def fake_post(*args, **kwargs):
        raise RuntimeError(f"transport rejected credential {SECRET}")

    monkeypatch.setattr(httpx, "post", fake_post)
    source = item()
    client = FoundryNarrativeClient(FoundrySettings("https://example.test", "model", SECRET))
    with caplog.at_level(logging.DEBUG):
        output, warning = client.rewrite(source)
    assert output == source
    assert warning is not None
    assert SECRET not in warning
    assert SECRET not in json.dumps(output.model_dump(mode="json"))
    for record in caplog.records:
        assert SECRET not in record.getMessage()


def test_managed_identity_configuration_needs_no_api_key(monkeypatch):
    monkeypatch.delenv("FOUNDRY_API_KEY", raising=False)
    monkeypatch.setenv("FOUNDRY_ENDPOINT", "https://example.test")
    monkeypatch.setenv("FOUNDRY_DEPLOYMENT", "model")
    monkeypatch.setenv("FOUNDRY_AUTH_MODE", AUTH_MANAGED_IDENTITY)
    settings = FoundrySettings.from_env()
    assert settings is not None
    assert settings.auth_mode == AUTH_MANAGED_IDENTITY
    assert settings.api_key == ""
    assert settings.configured is True
    client = FoundryNarrativeClient(settings)
    assert client.provider_name == AUTH_MANAGED_IDENTITY


def test_unconfigured_environment_disables_the_optional_rewrite(monkeypatch):
    for name in ("FOUNDRY_ENDPOINT", "FOUNDRY_DEPLOYMENT", "FOUNDRY_API_KEY", "FOUNDRY_AUTH_MODE"):
        monkeypatch.delenv(name, raising=False)
    assert FoundrySettings.from_env() is None


def test_api_key_mode_without_a_key_stays_unconfigured(monkeypatch):
    monkeypatch.delenv("FOUNDRY_API_KEY", raising=False)
    monkeypatch.setenv("FOUNDRY_ENDPOINT", "https://example.test")
    monkeypatch.setenv("FOUNDRY_DEPLOYMENT", "model")
    monkeypatch.setenv("FOUNDRY_AUTH_MODE", "api_key")
    assert FoundrySettings.from_env() is None


def test_api_key_provider_requires_a_non_empty_value():
    provider = ApiKeyCredentialProvider("")
    with pytest.raises(FoundryCredentialError):
        provider.headers()


def test_managed_identity_provider_uses_a_bearer_token():
    class Token:
        token = "signed-token"
        expires_on = 4_102_444_800.0

    class Credential:
        def __init__(self) -> None:
            self.calls = 0

        def get_token(self, scope, *args, **kwargs):
            self.calls += 1
            assert scope == "https://cognitiveservices.azure.com/.default"
            return Token()

    credential = Credential()
    provider = ManagedIdentityCredentialProvider(credential=credential)
    headers = provider.headers()
    assert headers == {"Authorization": "Bearer signed-token"}
    provider.headers()
    assert credential.calls == 1


def test_managed_identity_provider_falls_back_when_identity_is_unavailable():
    class Broken:
        def get_token(self, scope, *args, **kwargs):
            raise RuntimeError("no identity")

    provider = ManagedIdentityCredentialProvider(credential=Broken())
    with pytest.raises(FoundryCredentialError):
        provider.headers()
