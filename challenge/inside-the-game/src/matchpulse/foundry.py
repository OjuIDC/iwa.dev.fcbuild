from __future__ import annotations

import abc
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .models import PersonalizedInsight

log = logging.getLogger(__name__)

DEFAULT_API_VERSION = "2024-10-21"
DEFAULT_TIMEOUT_SECONDS = 1.4
DEFAULT_SCOPE = "https://cognitiveservices.azure.com/.default"

AUTH_API_KEY = "api_key"
AUTH_MANAGED_IDENTITY = "managed_identity"
SUPPORTED_AUTH_MODES = (AUTH_API_KEY, AUTH_MANAGED_IDENTITY)


class FoundryError(RuntimeError):
    """Base error for the optional Microsoft Foundry enrichment path."""


class FoundryCredentialError(FoundryError):
    """Raised when no usable Foundry credential could be produced."""


@dataclass(frozen=True)
class FoundrySettings:
    """Non-secret configuration for the optional narrative rewrite.

    Only ``api_key`` can hold a secret, and it is supplied through the runtime
    environment. Every other field is a public identifier.
    """

    endpoint: str
    deployment: str
    api_key: str = ""
    api_version: str = DEFAULT_API_VERSION
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    auth_mode: str = AUTH_API_KEY
    scope: str = DEFAULT_SCOPE

    @property
    def configured(self) -> bool:
        if self.auth_mode == AUTH_MANAGED_IDENTITY:
            return True
        return bool(self.api_key)

    @classmethod
    def from_env(cls) -> FoundrySettings | None:
        """Build settings from the environment, or ``None`` when not configured.

        The deterministic pipeline is authoritative, so an unconfigured or
        partially configured environment simply disables the optional rewrite.
        """
        endpoint = os.getenv("FOUNDRY_ENDPOINT", "").strip().rstrip("/")
        deployment = os.getenv("FOUNDRY_DEPLOYMENT", "").strip()
        api_key = os.getenv("FOUNDRY_API_KEY", "").strip()
        if not (endpoint and deployment):
            return None

        requested = os.getenv("FOUNDRY_AUTH_MODE", "").strip().lower()
        if requested not in SUPPORTED_AUTH_MODES:
            requested = AUTH_API_KEY if api_key else ""
        if requested == AUTH_API_KEY and not api_key:
            requested = ""

        if not requested:
            return None

        try:
            timeout_seconds = float(os.getenv("FOUNDRY_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
        except ValueError:
            timeout_seconds = DEFAULT_TIMEOUT_SECONDS
        if timeout_seconds <= 0:
            timeout_seconds = DEFAULT_TIMEOUT_SECONDS

        return cls(
            endpoint=endpoint,
            deployment=deployment,
            api_key=api_key,
            api_version=os.getenv("FOUNDRY_API_VERSION", DEFAULT_API_VERSION).strip() or DEFAULT_API_VERSION,
            timeout_seconds=timeout_seconds,
            auth_mode=requested,
            scope=os.getenv("FOUNDRY_AZURE_SCOPE", DEFAULT_SCOPE).strip() or DEFAULT_SCOPE,
        )


class FoundryCredentialProvider(abc.ABC):
    """Strategy that turns configuration into outbound authentication headers."""

    name = "provider"

    @abc.abstractmethod
    def headers(self) -> dict[str, str]:
        """Return authentication headers for a single Foundry request."""


class ApiKeyCredentialProvider(FoundryCredentialProvider):
    """Runtime-supplied API key. The value is never logged or persisted."""

    name = AUTH_API_KEY

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def headers(self) -> dict[str, str]:
        if not self._api_key:
            raise FoundryCredentialError("Foundry API key is not set")
        return {"api-key": self._api_key}


class ManagedIdentityCredentialProvider(FoundryCredentialProvider):
    """Azure managed identity via ``DefaultAzureCredential``.

    This is the preferred production path: it needs no stored secret and no
    committed key. ``azure-identity`` is an optional dependency, so a machine
    without it degrades to the deterministic fallback instead of failing.
    """

    name = AUTH_MANAGED_IDENTITY

    def __init__(self, scope: str = DEFAULT_SCOPE, credential: Any | None = None, refresh_margin_seconds: float = 120.0) -> None:
        self._scope = scope
        self._credential = credential
        self._refresh_margin_seconds = refresh_margin_seconds
        self._token = ""
        self._expires_at = 0.0

    def _ensure_credential(self) -> Any:
        if self._credential is None:
            try:
                from azure.identity import DefaultAzureCredential
            except ImportError as exc:
                raise FoundryCredentialError("azure-identity is not installed") from exc
            self._credential = DefaultAzureCredential()
        return self._credential

    def headers(self) -> dict[str, str]:
        now = time.time()
        if not self._token or now >= (self._expires_at - self._refresh_margin_seconds):
            credential = self._ensure_credential()
            try:
                token = credential.get_token(self._scope)
            except Exception as exc:
                raise FoundryCredentialError(f"managed identity token request failed: {type(exc).__name__}") from exc
            self._token = token.token
            self._expires_at = float(getattr(token, "expires_on", now + 300))
        return {"Authorization": f"Bearer {self._token}"}


class FoundryNarrativeClient:
    """Optional Microsoft Foundry rewrite with deterministic, grounded fallback.

    The core pipeline never depends on model output. Any timeout, invalid JSON, new
    numeric claim, or schema violation returns the original evidence-backed text.
    """

    def __init__(self, settings: FoundrySettings, provider: FoundryCredentialProvider | None = None) -> None:
        self.settings = settings
        self._provider = provider if provider is not None else self._default_provider(settings)

    @staticmethod
    def _default_provider(settings: FoundrySettings) -> FoundryCredentialProvider:
        if settings.auth_mode == AUTH_MANAGED_IDENTITY:
            return ManagedIdentityCredentialProvider(scope=settings.scope)
        return ApiKeyCredentialProvider(settings.api_key)

    @property
    def provider_name(self) -> str:
        return self._provider.name

    @staticmethod
    def _numbers(text: str) -> set[str]:
        return set(re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)?", text))

    def rewrite(self, item: PersonalizedInsight) -> tuple[PersonalizedInsight, str | None]:
        source = item.model_dump(mode="json")
        allowed_numbers = self._numbers(json.dumps({"body": item.body, "overlay": item.overlay_text, "stats": item.stats}))
        url = (f"{self.settings.endpoint}/openai/deployments/{self.settings.deployment}"
               f"/chat/completions?api-version={self.settings.api_version}")
        prompt = {
            "role": "user",
            "content": (
                "Rewrite only the title, body, and overlay_text for clarity and the stated audience. "
                "Return strict JSON with exactly those three keys. Do not add facts, numbers, names, "
                "predictions, or causal claims. Keep overlay_text under 90 characters. Input: "
                + json.dumps(source, ensure_ascii=False)
            ),
        }
        try:
            headers = {"Content-Type": "application/json"}
            headers.update(self._provider.headers())
            response = httpx.post(url, headers=headers,
                json={"messages": [{"role": "system", "content": "You are a grounded football broadcast copy editor."}, prompt],
                      "temperature": 0.1, "max_tokens": 240, "response_format": {"type": "json_object"}},
                timeout=self.settings.timeout_seconds)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            rewrite: dict[str, Any] = json.loads(content)
            if set(rewrite) != {"title", "body", "overlay_text"}:
                raise ValueError("unexpected response keys")
            if any(not isinstance(rewrite[k], str) or not rewrite[k].strip() for k in rewrite):
                raise ValueError("empty response text")
            if len(rewrite["overlay_text"]) > 90:
                raise ValueError("overlay too long")
            introduced = self._numbers(json.dumps(rewrite, ensure_ascii=False)) - allowed_numbers
            if introduced:
                raise ValueError("model introduced unsupported numeric claims")
            return item.model_copy(update=rewrite), None
        except Exception as exc:
            reason = type(exc).__name__
            log.warning("Foundry rewrite fallback: %s", reason)
            return item, f"Foundry fallback: {reason}"
