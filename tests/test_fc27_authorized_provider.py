"""Mock-Only Test Suite for Authorized FC 27 API Provider Contract, Transport Policy, and Normalization.

Covers all 54+ required Part G test scenarios:
- Zero network requests and zero DNS resolutions (enforced by autouse socket/DNS blocker)
- Provider declaration & authorization validation (Tests 1-14)
- Bounded transport policy, HTTPS/host/path/query/redirect/timeout/size/JSON/sensitive-field/redaction/budget/breaker (Tests 15-34)
- Normalization, pseudonymization, gamer-tag/external-ID suppression, hashes, replay/conflict (Tests 35-48)
- Default runtime flags, PostgresSaver non-modification, MatchPulse boundary, and provider selection gate (Tests 49-56)
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Mapping

import pytest
from pydantic import ValidationError

from src.fc27_authorized_api import (
    ALL_AUTHORIZED_OPERATIONS,
    EA_APPROVED_ACCESS_REQUEST_DRAFT,
    REVIEWED_API_SOURCE_CANDIDATES,
    FC27AuthMethod,
    FC27AuthorizedCredentials,
    FC27AuthorizedOperation,
    FC27AuthorizedProvider,
    FC27AuthorizedProviderAdapter,
    FC27AuthorizedProviderDeclaration,
    FC27AuthorizedTransportClient,
    FC27AuthorizedTransportPolicy,
    FC27ProviderCandidateClassification,
    FC27RetryPolicy,
    evaluate_provider_selection_gate,
    pseudonymize_east_coasters_player_ref,
    redact_sensitive_auth_text,
    verify_default_runtime_safety_posture,
)
from src.fc27_provider import (
    EAST_COASTERS_CLUB_ID,
    EAST_COASTERS_CLUB_NAME,
    FC27AuthenticationError,
    FC27InvalidHostError,
    FC27PayloadTooLargeError,
    FC27ProviderError,
    FC27RateLimitError,
    FC27TransportError,
)
from src.fc27_user_export import FC27ImportState, compute_match_hash
from src.schemas import ClubIdentityState
from src.settings import (
    is_dispatch_enabled,
    is_dispatch_worker_enabled,
    is_fc27_analytics_enabled,
    is_fc27_live_ingestion_enabled,
    is_fc27_persistence_enabled,
    is_fc27_provider_enabled,
    is_fc27_recommendations_enabled,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
REF_NOW = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)
FUTURE_EXPIRY = datetime(2027, 10, 29, 12, 0, 0, tzinfo=timezone.utc)
PAST_EXPIRY = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _block_all_network_and_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guarantees zero live network requests and zero DNS resolutions during tests."""

    def _forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Live network socket or DNS resolution is prohibited in authorized API tests")

    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", _forbidden)
    monkeypatch.setattr(socket, "gethostbyname", _forbidden)


def make_transport_policy(**overrides: Any) -> FC27AuthorizedTransportPolicy:
    base: Dict[str, Any] = {
        "provider_id": "ea-authorized-clubs-partner",
        "https_only": True,
        "exact_allowed_hosts": ("api.authorized-fc27.example.com",),
        "exact_operation_paths": {
            "CLUB_IDENTITY": "/v1/fc27/clubs/identity",
            "CLUB_STATISTICS": "/v1/fc27/clubs/statistics",
            "RECENT_MATCHES": "/v1/fc27/clubs/matches",
            "MATCH_PLAYER_STATISTICS": "/v1/fc27/clubs/matches/players",
            "CURRENT_MEMBER_STATISTICS": "/v1/fc27/clubs/members/current",
            "MEMBER_CAREER_STATISTICS": "/v1/fc27/clubs/members/career",
            "PLAYOFF_ACHIEVEMENTS": "/v1/fc27/clubs/playoffs",
        },
        "allowed_query_parameters": {
            "CLUB_IDENTITY": ("clubId", "platform"),
            "CLUB_STATISTICS": ("clubId", "platform"),
            "RECENT_MATCHES": ("clubId", "platform", "matchType"),
            "MATCH_PLAYER_STATISTICS": ("clubId", "platform", "matchType"),
            "CURRENT_MEMBER_STATISTICS": ("clubId", "platform"),
            "MEMBER_CAREER_STATISTICS": ("clubId", "platform"),
            "PLAYOFF_ACHIEVEMENTS": ("clubId", "platform"),
        },
        "approved_operations": ALL_AUTHORIZED_OPERATIONS,
        "approved_club_ids": (EAST_COASTERS_CLUB_ID,),
        "approved_platforms": ("common-gen5",),
        "approved_game_versions": ("FC27",),
        "timeout_seconds": 5.0,
        "response_size_maximum_bytes": 65536,
        "redirect_policy": "REFUSE_ALL",
        "retry_policy": FC27RetryPolicy(max_retries=0, backoff_seconds=0.0, retry_on_5xx_only=True),
        "request_limit_per_minute": 5,
        "max_requests_per_session": 6,
    }
    base.update(overrides)
    return FC27AuthorizedTransportPolicy(**base)


def make_provider_declaration(**overrides: Any) -> FC27AuthorizedProviderDeclaration:
    policy = overrides.pop("transport_policy", None) or make_transport_policy(
        provider_id=overrides.get("provider_id", "ea-authorized-clubs-partner")
    )
    base: Dict[str, Any] = {
        "provider_id": "ea-authorized-clubs-partner",
        "provider_classification": FC27ProviderCandidateClassification.OFFICIAL_AUTHORIZED.value,
        "legal_entity": "Electronic Arts Inc.",
        "documentation_url": "https://developer.authorized-fc27.example.com/docs",
        "terms_url": "https://developer.authorized-fc27.example.com/terms",
        "terms_version": "2026-10-v1",
        "privacy_url": "https://developer.authorized-fc27.example.com/privacy",
        "privacy_policy_version": "2026-10-p1",
        "license_or_agreement": "EA-WRITTEN-PARTNER-AGREEMENT-2026-330323",
        "authorization_reference": "api-east-coasters-authority-2026-10",
        "authorization_expires_at": FUTURE_EXPIRY,
        "approved_club_ids": (EAST_COASTERS_CLUB_ID,),
        "approved_platforms": ("common-gen5",),
        "approved_game_versions": ("FC27",),
        "approved_operations": ALL_AUTHORIZED_OPERATIONS,
        "approved_purposes": ("INTERNAL_TEAM_ANALYTICS", "TEAM_REVIEW"),
        "request_limit_per_minute": 5,
        "max_requests_per_session": 6,
        "timeout_seconds": 5.0,
        "retry_policy": FC27RetryPolicy(max_retries=0),
        "redirect_policy": "REFUSE_ALL",
        "response_size_maximum_bytes": 65536,
        "normalized_storage_permission": True,
        "retention_permission": True,
        "max_retention_days": 730,
        "member_data_permission": True,
        "player_data_permission": True,
        "transport_policy": policy,
    }
    base.update(overrides)
    return FC27AuthorizedProviderDeclaration(**base)


def make_mock_adapter(
    response_payload: Mapping[str, Any] | bytes,
    *,
    status_code: int = 200,
    content_type: str = "application/json; charset=utf-8",
    redirect_count: int = 0,
    location_header: str = "",
    declaration_overrides: Mapping[str, Any] | None = None,
    policy_overrides: Mapping[str, Any] | None = None,
) -> FC27AuthorizedProviderAdapter:
    pol = make_transport_policy(**(dict(policy_overrides) if policy_overrides else {}))
    decl_kw = dict(declaration_overrides) if declaration_overrides else {}
    decl_kw["transport_policy"] = pol
    decl = make_provider_declaration(**decl_kw)
    creds = FC27AuthorizedCredentials(
        auth_method=FC27AuthMethod.API_KEY_ENV.value,
        env_var_name="FC27_TEST_PROVIDER_API_KEY",
    )
    raw_bytes = (
        response_payload
        if isinstance(response_payload, bytes)
        else json.dumps(response_payload).encode("utf-8")
    )

    def _mock_transport(req: Mapping[str, Any]) -> Mapping[str, Any]:
        return {
            "status_code": status_code,
            "content_type": content_type,
            "redirect_count": redirect_count,
            "location_header": location_header,
            "raw_bytes": raw_bytes,
        }

    client = FC27AuthorizedTransportClient(
        policy=pol,
        credentials=creds,
        transport_callable=_mock_transport,
    )
    return FC27AuthorizedProviderAdapter(
        declaration=decl,
        transport_client=client,
        reference_now=REF_NOW,
        environ={"FC27_TEST_PROVIDER_API_KEY": "super-secret-provider-key-9999"},
    )


# ==============================================================================
# Tests 1-14: Provider Declaration & Permission Validation
# ==============================================================================

def test_01_missing_provider_authorization_rejected() -> None:
    with pytest.raises((ValidationError, FC27ProviderError)):
        make_provider_declaration(authorization_reference="   ")


def test_02_expired_provider_authorization_rejected() -> None:
    adapter = make_mock_adapter(
        {"330323": {"clubId": "330323", "name": "East Coasters", "platform": "common-gen5"}},
        declaration_overrides={"authorization_expires_at": PAST_EXPIRY},
    )
    with pytest.raises(FC27ProviderError, match="expired"):
        adapter.fetch_club_identity()


@pytest.mark.parametrize(
    "bad_classification",
    [
        FC27ProviderCandidateClassification.UNOFFICIAL_CLIENT.value,
        FC27ProviderCandidateClassification.UNVERIFIED_RELAY.value,
        FC27ProviderCandidateClassification.NOT_ACCEPTABLE.value,
        FC27ProviderCandidateClassification.COMMUNITY_DOCUMENTED.value,
    ],
)
def test_03_unsupported_provider_classification_rejected(bad_classification: str) -> None:
    with pytest.raises(FC27ProviderError, match="Unsupported or unapproved provider_classification"):
        make_provider_declaration(provider_classification=bad_classification)


def test_04_unsupported_operation_rejected() -> None:
    with pytest.raises((ValidationError, FC27ProviderError)):
        make_provider_declaration(approved_operations=("ARBITRARY_ENDPOINT",))

    adapter = make_mock_adapter(
        {"330323": {"clubId": "330323", "name": "East Coasters", "platform": "common-gen5"}},
        declaration_overrides={"approved_operations": ("CLUB_IDENTITY",)},
    )
    with pytest.raises(FC27ProviderError, match="not approved"):
        adapter.fetch_club_statistics()


def test_05_unapproved_club_rejected() -> None:
    with pytest.raises(FC27ProviderError, match="Unapproved club scope"):
        make_provider_declaration(approved_club_ids=("999999",))

    adapter = make_mock_adapter(
        {"330323": {"clubId": "330323", "name": "East Coasters", "platform": "common-gen5"}}
    )
    with pytest.raises(FC27ProviderError, match="Unapproved club_id"):
        adapter.fetch_club_identity(club_id="999999")


def test_06_unapproved_platform_rejected() -> None:
    with pytest.raises(FC27ProviderError, match="Unapproved platform scope"):
        make_provider_declaration(approved_platforms=("pc",))

    adapter = make_mock_adapter(
        {"330323": {"clubId": "330323", "name": "East Coasters", "platform": "common-gen5"}}
    )
    with pytest.raises(FC27ProviderError, match="Unapproved platform"):
        adapter.fetch_club_identity(platform="nx")


def test_07_unapproved_game_version_rejected() -> None:
    with pytest.raises(FC27ProviderError, match="Unapproved game_version scope"):
        make_provider_declaration(approved_game_versions=("FC26",))

    adapter = make_mock_adapter(
        {"330323": {"clubId": "330323", "name": "East Coasters", "platform": "common-gen5"}}
    )
    with pytest.raises(FC27ProviderError, match="Unapproved game_version"):
        adapter.fetch_club_identity(game_version="FC26")


def test_08_unapproved_purpose_rejected() -> None:
    with pytest.raises(FC27ProviderError, match="Unapproved purpose"):
        make_provider_declaration(approved_purposes=("COMMERCIAL_RESALE",))

    adapter = make_mock_adapter(
        {"330323": {"clubId": "330323", "name": "East Coasters", "platform": "common-gen5"}}
    )
    with pytest.raises(FC27ProviderError, match="Unapproved purpose"):
        adapter.fetch_club_identity(purpose="PUBLIC_LEADERBOARD")


def test_09_missing_terms_version_rejected() -> None:
    with pytest.raises(ValidationError):
        make_provider_declaration(terms_version="   ")


def test_10_missing_privacy_version_rejected() -> None:
    with pytest.raises(ValidationError):
        make_provider_declaration(privacy_policy_version="")


def test_11_missing_storage_permission_rejected() -> None:
    with pytest.raises(FC27ProviderError, match="normalized_storage_permission=True is required"):
        make_provider_declaration(normalized_storage_permission=False)


def test_12_missing_retention_permission_rejected() -> None:
    with pytest.raises(FC27ProviderError, match="retention_permission=True"):
        make_provider_declaration(retention_permission=False)
    with pytest.raises(FC27ProviderError, match="max_retention_days >= 730"):
        make_provider_declaration(max_retention_days=30)


def test_13_member_data_without_member_permission_rejected() -> None:
    adapter = make_mock_adapter(
        {"members": [{"memberKey": "m1", "favoritePosition": "CM", "gamesPlayed": 10, "goals": 2, "assists": 3}]},
        declaration_overrides={"member_data_permission": False},
    )
    with pytest.raises(FC27ProviderError, match="requires member_data_permission=True"):
        adapter.fetch_current_member_statistics()
    with pytest.raises(FC27ProviderError, match="requires member_data_permission=True"):
        adapter.fetch_member_career_statistics()


def test_14_player_data_without_player_permission_rejected() -> None:
    adapter = make_mock_adapter(
        {"matches": []},
        declaration_overrides={"player_data_permission": False},
    )
    with pytest.raises(FC27ProviderError, match="requires player_data_permission=True"):
        adapter.fetch_match_player_statistics()


# ==============================================================================
# Tests 15-34: Transport Security Contract & Redaction
# ==============================================================================

def test_15_https_enforcement_rejects_http() -> None:
    adapter = make_mock_adapter(
        {"330323": {"clubId": "330323", "name": "East Coasters", "platform": "common-gen5"}}
    )
    with pytest.raises(FC27TransportError, match="HTTPS enforcement violation"):
        adapter.transport_client.execute_operation(
            FC27AuthorizedOperation.CLUB_IDENTITY,
            override_url_for_validation="http://api.authorized-fc27.example.com/v1/fc27/clubs/identity",
            environ={"FC27_TEST_PROVIDER_API_KEY": "secret-key"},
        )


def test_16_exact_host_enforcement_rejects_unapproved_host() -> None:
    adapter = make_mock_adapter(
        {"330323": {"clubId": "330323", "name": "East Coasters", "platform": "common-gen5"}}
    )
    with pytest.raises(FC27InvalidHostError, match="not in exact_allowed_hosts"):
        adapter.transport_client.execute_operation(
            FC27AuthorizedOperation.CLUB_IDENTITY,
            target_host="evil.example.com",
            environ={"FC27_TEST_PROVIDER_API_KEY": "secret-key"},
        )


def test_17_exact_path_enforcement_rejects_unapproved_path() -> None:
    adapter = make_mock_adapter(
        {"330323": {"clubId": "330323", "name": "East Coasters", "platform": "common-gen5"}}
    )
    with pytest.raises(FC27TransportError, match="does not match exact allowlisted path"):
        adapter.transport_client.execute_operation(
            FC27AuthorizedOperation.CLUB_IDENTITY,
            override_url_for_validation="https://api.authorized-fc27.example.com/v1/fc27/clubs/other",
            environ={"FC27_TEST_PROVIDER_API_KEY": "secret-key"},
        )


def test_18_query_allowlist_rejects_unexpected_query_param() -> None:
    adapter = make_mock_adapter(
        {"330323": {"clubId": "330323", "name": "East Coasters", "platform": "common-gen5"}}
    )
    with pytest.raises(FC27TransportError, match="Unallowlisted query parameter"):
        adapter.transport_client.execute_operation(
            FC27AuthorizedOperation.CLUB_IDENTITY,
            query_params={"arbitraryParam": "1"},
            environ={"FC27_TEST_PROVIDER_API_KEY": "secret-key"},
        )


def test_19_redirect_refusal_opens_circuit_breaker() -> None:
    adapter = make_mock_adapter(
        b"{}",
        status_code=302,
        redirect_count=1,
        location_header="https://other.example.com/login",
    )
    with pytest.raises(FC27TransportError, match="Redirect response"):
        adapter.fetch_club_identity()
    assert adapter.transport_client.circuit_breaker.is_open() is True


def test_20_timeout_enforcement_opens_circuit_breaker() -> None:
    pol = make_transport_policy()
    creds = FC27AuthorizedCredentials(
        auth_method=FC27AuthMethod.API_KEY_ENV.value,
        env_var_name="FC27_TEST_PROVIDER_API_KEY",
    )

    def _timeout_transport(req: Mapping[str, Any]) -> Mapping[str, Any]:
        raise TimeoutError("Read timed out using Bearer super-secret-key-12345")

    client = FC27AuthorizedTransportClient(
        policy=pol,
        credentials=creds,
        transport_callable=_timeout_transport,
    )
    with pytest.raises(FC27TransportError, match=r"timed out.*\[REDACTED\]"):
        client.execute_operation(
            FC27AuthorizedOperation.CLUB_IDENTITY,
            environ={"FC27_TEST_PROVIDER_API_KEY": "super-secret-key-12345"},
        )
    assert client.circuit_breaker.is_open() is True


def test_21_response_size_limit_enforced() -> None:
    oversized = b'{"padding": "' + (b"x" * 70000) + b'"}'
    adapter = make_mock_adapter(oversized)
    with pytest.raises(FC27PayloadTooLargeError, match="exceeds maximum"):
        adapter.fetch_club_identity()


def test_22_json_content_type_enforced() -> None:
    adapter = make_mock_adapter(b"<html>Forbidden</html>", content_type="text/html")
    with pytest.raises(FC27TransportError, match="Invalid Content-Type"):
        adapter.fetch_club_identity()


def test_23_duplicate_json_keys_rejected() -> None:
    dup_bytes = b'{"330323": {"clubId": "330323", "clubId": "999999", "name": "East Coasters", "platform": "common-gen5"}}'
    adapter = make_mock_adapter(dup_bytes)
    with pytest.raises(FC27TransportError, match="Duplicate JSON key 'clubId' rejected"):
        adapter.fetch_club_identity()


def test_24_malformed_json_rejected() -> None:
    adapter = make_mock_adapter(b'{"330323": [unclosed')
    with pytest.raises(FC27TransportError, match="Malformed JSON"):
        adapter.fetch_club_identity()

    nan_adapter = make_mock_adapter(b'{"330323": {"clubId": "330323", "val": NaN}}')
    with pytest.raises(FC27TransportError, match="Disallowed JSON numeric constant"):
        nan_adapter.fetch_club_identity()


def test_25_schema_drift_detected_and_quarantined_without_raw_body() -> None:
    adapter = make_mock_adapter(
        {
            "330323": {
                "clubId": "330323",
                "name": "East Coasters",
                "platform": "common-gen5",
                "unexpectedNewProviderField": "extra_metadata",
            }
        }
    )
    res = adapter.fetch_club_identity()
    assert res.schema_drift_paths == ("unexpectedNewProviderField",)
    assert res.quarantine_metadata is not None
    assert res.quarantine_metadata["anomaly_category"] == "SCHEMA_DRIFT"
    assert res.quarantine_metadata["raw_body_stored"] is False


@pytest.mark.parametrize(
    "sensitive_field",
    [
        "cookie",
        "set_cookie",
        "session_id",
        "access_token",
        "account_id",
        "persona_id",
        "nucleus_id",
        "http_headers",
    ],
)
def test_26_sensitive_field_rejected(sensitive_field: str) -> None:
    adapter = make_mock_adapter(
        {
            "330323": {
                "clubId": "330323",
                "name": "East Coasters",
                "platform": "common-gen5",
                sensitive_field: "leaked_value",
            }
        }
    )
    with pytest.raises(FC27TransportError, match="prohibited sensitive field"):
        adapter.fetch_club_identity()


def test_27_raw_response_non_retention_verified() -> None:
    adapter = make_mock_adapter(
        {"330323": {"clubId": "330323", "name": "East Coasters", "platform": "common-gen5"}}
    )
    res = adapter.fetch_club_identity()
    assert res.raw_response_retained is False
    assert res.transport_log.raw_response_retained is False
    dumped = json.dumps(res.model_dump(mode="json"))
    assert "raw_bytes" not in dumped


def test_28_api_key_redaction_from_env_and_secret_file(tmp_path: Path) -> None:
    secret_dir = tmp_path / ".local-data"
    secret_dir.mkdir(parents=True, exist_ok=True)
    secret_file = secret_dir / "provider_api_key.txt"
    secret_val = "sk_live_east_coasters_secret_token_8888"
    secret_file.write_text(secret_val, encoding="utf-8")

    creds = FC27AuthorizedCredentials(
        auth_method=FC27AuthMethod.API_KEY_SECRET_FILE.value,
        secret_file_path=str(secret_file),
        header_name="X-API-Key",
        scheme_prefix="",
    )
    h_name, h_val, raw_sec = creds.build_auth_header()
    assert h_name == "X-API-Key"
    assert h_val == secret_val
    redacted = redact_sensitive_auth_text(f"Failed with X-API-Key: {secret_val}", extra_secrets=(raw_sec,))
    assert secret_val not in redacted
    assert "[REDACTED]" in redacted


def test_29_authorization_header_redaction_and_prohibited_auth_rejected() -> None:
    raw = "Error: Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.secret and api_key=my_secret_123"
    cleaned = redact_sensitive_auth_text(raw)
    assert "eyJhbGciOiJIUzI1NiJ9" not in cleaned
    assert "my_secret_123" not in cleaned
    assert "[REDACTED]" in cleaned

    for prohibited in (
        FC27AuthMethod.EA_ACCOUNT_CREDENTIALS.value,
        FC27AuthMethod.BROWSER_COOKIE.value,
        FC27AuthMethod.BROWSER_SESSION.value,
        FC27AuthMethod.PERSONAL_TOKEN.value,
        FC27AuthMethod.BROWSER_TLS_IMPERSONATION.value,
    ):
        with pytest.raises(FC27AuthenticationError, match="Prohibited or unsupported authentication method"):
            FC27AuthorizedCredentials(auth_method=prohibited)


def test_30_error_redaction_strips_embedded_credentials() -> None:
    pol = make_transport_policy()
    creds = FC27AuthorizedCredentials(
        auth_method=FC27AuthMethod.API_KEY_ENV.value,
        env_var_name="FC27_TEST_PROVIDER_API_KEY",
    )
    secret = "tok_secret_value_77777"

    def _failing_transport(req: Mapping[str, Any]) -> Mapping[str, Any]:
        raise RuntimeError(f"Connection failed for https://user:{secret}@api.authorized-fc27.example.com with key {secret}")

    client = FC27AuthorizedTransportClient(
        policy=pol,
        credentials=creds,
        transport_callable=_failing_transport,
    )
    with pytest.raises(FC27TransportError) as exc_info:
        client.execute_operation(
            FC27AuthorizedOperation.CLUB_IDENTITY,
            environ={"FC27_TEST_PROVIDER_API_KEY": secret},
        )
    assert secret not in str(exc_info.value)
    assert "[REDACTED]" in str(exc_info.value)
    pg_conn = "Database error on postgresql://fc27_app:db_secret_pass_999@db.example.com:5432/fc27"
    redacted_conn = redact_sensitive_auth_text(pg_conn)
    assert "db_secret_pass_999" not in redacted_conn
    assert "postgresql://[REDACTED]@db.example.com:5432/fc27" in redacted_conn


def test_31_metadata_only_logging() -> None:
    adapter = make_mock_adapter(
        {"330323": {"clubId": "330323", "name": "East Coasters", "platform": "common-gen5"}}
    )
    res = adapter.fetch_club_identity()
    log_dump = res.transport_log.model_dump()
    assert log_dump["provider_id"] == "ea-authorized-clubs-partner"
    assert log_dump["operation"] == "CLUB_IDENTITY"
    assert log_dump["http_status"] == 200
    assert log_dump["raw_response_retained"] is False
    assert log_dump["authorization_redacted"] is True
    assert "super-secret-provider-key-9999" not in json.dumps(log_dump)


def test_32_rate_budget_enforcement() -> None:
    adapter = make_mock_adapter(
        {"330323": {"clubId": "330323", "name": "East Coasters", "platform": "common-gen5"}},
        policy_overrides={"max_requests_per_session": 2, "request_limit_per_minute": 2},
    )
    adapter.fetch_club_identity()
    adapter.fetch_club_identity()
    with pytest.raises(FC27RateLimitError, match="request budget exhausted"):
        adapter.fetch_club_identity()


@pytest.mark.parametrize("status_code,exc_type", [(401, FC27AuthenticationError), (403, FC27TransportError), (429, FC27RateLimitError)])
def test_33_circuit_breaker_opens_on_401_403_429(status_code: int, exc_type: type[Exception]) -> None:
    adapter = make_mock_adapter(b"{}", status_code=status_code)
    with pytest.raises(exc_type):
        adapter.fetch_club_identity()
    assert adapter.transport_client.circuit_breaker.is_open() is True
    with pytest.raises(FC27TransportError, match="Circuit breaker is OPEN"):
        adapter.fetch_club_identity()


def test_34_no_arbitrary_url_support_in_adapter_or_transport() -> None:
    adapter = make_mock_adapter(
        {"330323": {"clubId": "330323", "name": "East Coasters", "platform": "common-gen5"}}
    )
    assert isinstance(adapter, FC27AuthorizedProvider)
    for forbidden_method in ("get_json", "fetch_url", "request_url", "get", "post", "urlopen"):
        assert not hasattr(adapter, forbidden_method)
        assert not hasattr(adapter.transport_client, forbidden_method)


# ==============================================================================
# Tests 35-48: Normalization, Pseudonymization, Hashes, & Replay/Conflict Planning
# ==============================================================================

def test_35_club_identity_normalization() -> None:
    adapter = make_mock_adapter(
        {"330323": {"clubId": "330323", "name": "East Coasters", "platform": "common-gen5", "regionId": 123}}
    )
    env = adapter.fetch_club_identity()
    assert env.club_identity is not None
    assert env.club_identity.club_id == "330323"
    assert env.club_identity.club_name == "East Coasters"
    assert env.club_identity.platform == "common-gen5"
    assert env.club_identity.identity_verification_state == ClubIdentityState.PROVISIONAL


def test_36_club_statistics_normalization() -> None:
    adapter = make_mock_adapter(
        {
            "330323": {
                "gamesPlayed": 40,
                "wins": 25,
                "ties": 8,
                "losses": 7,
                "goals": 82,
                "goalsAgainst": 39,
                "cleanSheets": 14,
                "bestDivision": 1,
                "skillRating": 2150,
            }
        }
    )
    env = adapter.fetch_club_statistics()
    assert env.club_statistics is not None
    assert env.club_statistics.matches_played == 40
    assert env.club_statistics.wins == 25
    assert env.club_statistics.draws == 8
    assert env.club_statistics.losses == 7
    assert env.club_statistics.goals_for == 82
    assert env.club_statistics.goals_against == 39


def test_37_match_normalization() -> None:
    adapter = make_mock_adapter(
        {
            "matches": [
                {
                    "matchId": "m-9001",
                    "playedAt": "2026-10-28T20:00:00Z",
                    "result": "WIN",
                    "goalsFor": 3,
                    "goalsAgainst": 1,
                    "opponentName": "Atlantic City FC",
                    "formation": "4-2-3-1",
                    "possessionPct": 56.5,
                    "shots": 11,
                    "shotsOnTarget": 7,
                }
            ]
        }
    )
    env = adapter.fetch_recent_matches()
    assert len(env.matches) == 1
    m = env.matches[0]
    assert m.external_match_id == "m-9001"
    assert m.result == "WIN"
    assert m.goals_for == 3
    assert m.goals_against == 1
    assert m.player_performances == []


def test_38_player_statistics_normalization() -> None:
    adapter = make_mock_adapter(
        {
            "matches": [
                {
                    "matchId": "m-9002",
                    "playedAt": "2026-10-28T21:00:00Z",
                    "result": "WIN",
                    "goalsFor": 2,
                    "goalsAgainst": 0,
                    "opponentName": "Harbor United",
                    "formation": "4-3-3",
                    "players": [
                        {
                            "memberKey": "internal-member-slot-1",
                            "proName": "RealGamerTagShouldBeDropped",
                            "playerId": "99887766",
                            "position": "ST",
                            "minutesPlayed": 90,
                            "goals": 2,
                            "assists": 0,
                            "rating": 8.9,
                            "passesCompleted": 20,
                            "passesAttempted": 24,
                        }
                    ],
                }
            ]
        }
    )
    env = adapter.fetch_match_player_statistics()
    assert len(env.matches) == 1
    assert len(env.matches[0].player_performances) == 1
    perf = env.matches[0].player_performances[0]
    assert perf.local_player_reference.startswith("plr_ecf_")
    assert perf.display_alias is None
    assert perf.goals == 2
    assert perf.provider_rating == 8.9


def test_39_member_statistics_normalization() -> None:
    adapter = make_mock_adapter(
        {
            "members": [
                {
                    "memberKey": "member-slot-alpha",
                    "proName": "HiddenHandle1",
                    "favoritePosition": "CAM",
                    "gamesPlayed": 32,
                    "goals": 14,
                    "assists": 19,
                    "cleanSheets": 8,
                    "averageRating": 8.2,
                }
            ]
        }
    )
    env = adapter.fetch_current_member_statistics()
    assert len(env.member_statistics) == 1
    mem = env.member_statistics[0]
    assert mem.scope == "CURRENT_CLUB"
    assert mem.local_player_reference.startswith("plr_ecf_")
    assert mem.display_alias is None
    assert mem.games_played == 32


def test_40_career_statistics_normalization() -> None:
    adapter = make_mock_adapter(
        {
            "members": [
                {
                    "memberKey": "member-slot-alpha",
                    "favoritePosition": "CAM",
                    "gamesPlayed": 180,
                    "goals": 75,
                    "assists": 92,
                    "averageRating": 8.4,
                }
            ]
        }
    )
    env = adapter.fetch_member_career_statistics()
    assert len(env.member_statistics) == 1
    assert env.member_statistics[0].scope == "CAREER"
    assert env.member_statistics[0].games_played == 180


def test_41_playoff_normalization() -> None:
    adapter = make_mock_adapter(
        {
            "achievements": [
                {
                    "seasonId": "S4",
                    "seasonName": "Season 4 Playoffs",
                    "bestDivision": 1,
                    "playoffWins": 10,
                    "playoffLosses": 2,
                    "playoffDraws": 0,
                    "finishingPosition": 12,
                }
            ]
        }
    )
    env = adapter.fetch_playoff_achievements()
    assert len(env.playoff_achievements) == 1
    assert env.playoff_achievements[0].season_id == "S4"
    assert env.playoff_achievements[0].best_division == 1


def test_42_pseudonymous_player_references_deterministic_and_scoped() -> None:
    ref1 = pseudonymize_east_coasters_player_ref("member-slot-alpha", club_id="330323")
    ref2 = pseudonymize_east_coasters_player_ref("member-slot-alpha", club_id="330323")
    ref_other = pseudonymize_east_coasters_player_ref("member-slot-beta", club_id="330323")
    assert ref1 == ref2
    assert ref1 != ref_other
    assert ref1.startswith("plr_ecf_")
    assert len(ref1) == 20


def test_43_gamer_tag_suppression() -> None:
    adapter = make_mock_adapter(
        {
            "members": [
                {
                    "memberKey": "slot-1",
                    "proName": "SecretGamerTagXYZ",
                    "gamertag": "SecretGamerTagXYZ",
                    "psnId": "SecretPSN_XYZ",
                    "favoritePosition": "CM",
                    "gamesPlayed": 10,
                    "goals": 1,
                    "assists": 2,
                }
            ]
        }
    )
    env = adapter.fetch_current_member_statistics()
    serialized = json.dumps(env.model_dump(mode="json"))
    assert "SecretGamerTagXYZ" not in serialized
    assert "SecretPSN_XYZ" not in serialized
    assert env.store_gamer_tags is False
    assert env.store_display_aliases is False


def test_44_external_player_id_suppression() -> None:
    adapter = make_mock_adapter(
        {
            "members": [
                {
                    "memberKey": "slot-1",
                    "playerId": "ext_player_987654321",
                    "externalPlayerId": "ext_player_987654321",
                    "favoritePosition": "CB",
                    "gamesPlayed": 12,
                    "goals": 0,
                    "assists": 1,
                }
            ]
        }
    )
    env = adapter.fetch_current_member_statistics()
    serialized = json.dumps(env.model_dump(mode="json"))
    assert "ext_player_987654321" not in serialized
    assert env.store_external_player_identifiers is False


def test_45_deterministic_payload_hash() -> None:
    payload = {
        "matches": [
            {
                "matchId": "m-det-1",
                "playedAt": "2026-10-28T20:00:00Z",
                "result": "WIN",
                "goalsFor": 2,
                "goalsAgainst": 1,
                "opponentName": "Bay City",
                "formation": "4-3-3",
            }
        ]
    }
    a1 = make_mock_adapter(payload)
    a2 = make_mock_adapter(payload)
    e1, _ = a1.build_export_envelope_and_plan(
        export_id="exp-det-1",
        matches_envelope=a1.fetch_recent_matches(),
        generated_at=REF_NOW,
    )
    e2, _ = a2.build_export_envelope_and_plan(
        export_id="exp-det-1",
        matches_envelope=a2.fetch_recent_matches(),
        generated_at=REF_NOW,
    )
    assert e1.payload_sha256 == e2.payload_sha256
    assert len(e1.payload_sha256) == 64


def test_46_deterministic_match_hash() -> None:
    payload = {
        "matches": [
            {
                "matchId": "m-det-2",
                "playedAt": "2026-10-28T20:00:00Z",
                "result": "DRAW",
                "goalsFor": 1,
                "goalsAgainst": 1,
                "opponentName": "Coastline FC",
                "formation": "4-4-2",
            }
        ]
    }
    adapter = make_mock_adapter(payload)
    env1, _ = adapter.build_export_envelope_and_plan(
        export_id="exp-det-2",
        matches_envelope=adapter.fetch_recent_matches(),
        generated_at=REF_NOW,
    )
    env2, _ = adapter.build_export_envelope_and_plan(
        export_id="exp-det-2",
        matches_envelope=adapter.fetch_recent_matches(),
        generated_at=REF_NOW,
    )
    h1 = compute_match_hash(
        env1.matches[0],
        club_id=EAST_COASTERS_CLUB_ID,
        platform="common-gen5",
        game_version="FC27",
        source_classification="USER_SUPPLIED_EXPORT",
    )
    h2 = compute_match_hash(
        env2.matches[0],
        club_id=EAST_COASTERS_CLUB_ID,
        platform="common-gen5",
        game_version="FC27",
        source_classification="USER_SUPPLIED_EXPORT",
    )
    assert h1 == h2
    assert len(h1) == 64


def test_47_idempotent_replay_planning() -> None:
    payload = {
        "matches": [
            {
                "matchId": "m-replay-1",
                "playedAt": "2026-10-28T20:00:00Z",
                "result": "WIN",
                "goalsFor": 2,
                "goalsAgainst": 0,
                "opponentName": "Metro Stars",
                "formation": "4-3-3",
            }
        ]
    }
    adapter = make_mock_adapter(payload)
    matches_env = adapter.fetch_recent_matches()
    env1, initial_plan = adapter.build_export_envelope_and_plan(
        export_id="exp-replay-1",
        matches_envelope=matches_env,
        generated_at=REF_NOW,
    )
    assert initial_plan.accepted_match_ids == ["m-replay-1"]
    m_hash = compute_match_hash(
        env1.matches[0],
        club_id=EAST_COASTERS_CLUB_ID,
        platform="common-gen5",
        game_version="FC27",
        source_classification="USER_SUPPLIED_EXPORT",
    )
    existing = FC27ImportState(existing_match_hashes={"m-replay-1": m_hash})
    _, replay_plan = adapter.build_export_envelope_and_plan(
        export_id="exp-replay-2",
        matches_envelope=matches_env,
        existing_state=existing,
        generated_at=REF_NOW,
    )
    assert replay_plan.duplicate_match_ids == ["m-replay-1"]
    assert replay_plan.conflicting_match_ids == []
    assert replay_plan.can_apply is True


def test_48_changed_record_conflict_planning() -> None:
    payload = {
        "matches": [
            {
                "matchId": "m-conflict-1",
                "playedAt": "2026-10-28T20:00:00Z",
                "result": "WIN",
                "goalsFor": 3,
                "goalsAgainst": 0,
                "opponentName": "Metro Stars",
                "formation": "4-3-3",
            }
        ]
    }
    adapter = make_mock_adapter(payload)
    matches_env = adapter.fetch_recent_matches()
    existing = FC27ImportState(existing_match_hashes={"m-conflict-1": "a" * 64})
    _, conflict_plan = adapter.build_export_envelope_and_plan(
        export_id="exp-conflict-1",
        matches_envelope=matches_env,
        existing_state=existing,
        generated_at=REF_NOW,
    )
    assert conflict_plan.conflicting_match_ids == ["m-conflict-1"]
    assert conflict_plan.can_apply is False


# ==============================================================================
# Tests 49-56: Default Flags, PostgresSaver, MatchPulse Boundary, & Selection Gate
# ==============================================================================

def test_49_persistence_disabled_by_default() -> None:
    assert is_fc27_persistence_enabled() is False


def test_50_analytics_disabled_by_default() -> None:
    assert is_fc27_analytics_enabled() is False


def test_51_recommendations_disabled_by_default() -> None:
    assert is_fc27_recommendations_enabled() is False


def test_52_dispatch_disabled_by_default() -> None:
    assert is_dispatch_enabled() is False
    assert is_dispatch_worker_enabled() is False
    posture = verify_default_runtime_safety_posture()
    assert all(val is False for val in posture.values())


def test_53_postgressaver_tables_untouched() -> None:
    for mod_path in (
        REPO_ROOT / "src" / "fc27_authorized_provider.py",
        REPO_ROOT / "src" / "fc27_authorized_transport.py",
        REPO_ROOT / "src" / "fc27_provider_contract.py",
        REPO_ROOT / "src" / "fc27_authorized_api.py",
    ):
        text = mod_path.read_text(encoding="utf-8")
        for pg_saver_table in ("checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"):
            assert pg_saver_table not in text


def test_54_matchpulse_boundary_intact() -> None:
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_matchpulse_boundary.py")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0
    assert "OK" in res.stdout


def test_55_provider_selection_gate_fails_closed_when_no_authorized_source_exists() -> None:
    decision = evaluate_provider_selection_gate()
    assert decision.providers_reviewed == len(REVIEWED_API_SOURCE_CANDIDATES)
    assert decision.official_authorized_providers_found == 0
    assert decision.licensed_third_party_providers_found == 0
    assert decision.community_documented_providers_found == 0
    assert decision.unofficial_clients_rejected == 2
    assert decision.unverified_relays_rejected == 2
    assert decision.selected_provider == "NONE"
    assert decision.live_provider_implementation == "NOT_STARTED"
    assert decision.blocker == "NO_AUTHORIZED_API_SOURCE_FOUND"


def test_56_ea_access_request_draft_ready_and_not_sent() -> None:
    assert EA_APPROVED_ACCESS_REQUEST_DRAFT.ea_permission_request_ready is True
    assert EA_APPROVED_ACCESS_REQUEST_DRAFT.ea_access_request_ready is True
    assert EA_APPROVED_ACCESS_REQUEST_DRAFT.ea_permission_request_sent is False
    assert EA_APPROVED_ACCESS_REQUEST_DRAFT.ea_access_request_sent is False
    assert EA_APPROVED_ACCESS_REQUEST_DRAFT.club_id == "330323"
    assert EA_APPROVED_ACCESS_REQUEST_DRAFT.club_name == "East Coasters"
    assert EA_APPROVED_ACCESS_REQUEST_DRAFT.platform == "common-gen5"
    assert EA_APPROVED_ACCESS_REQUEST_DRAFT.contact_placeholder == "[OPERATOR_CONTACT_PLACEHOLDER]"
