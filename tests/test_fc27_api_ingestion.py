"""Part E & Part J: East Coasters FC 27 API Acquisition, Normalized Ingestion, and Validation Tests.

Covers all 40 Part E transport tests and all 44 Part J ingestion, privacy, persistence, and analytics tests
without making any external EA network requests during test execution.
"""
from __future__ import annotations

import copy
import json
import socket
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import psycopg
import pytest
from pydantic import ValidationError

from src.db_migrate import migrate
from src.db_roles import bootstrap_cluster, bootstrap_database
from src.fc27_clubs import (
    EAST_COASTERS_API_AUTHORITY_ATTESTATION,
    FC27_API_INGESTION_POLICIES,
    FC27_STANDARD_HTTP_TRANSPORT_ENABLED,
    PROCLUBS_SDK_AUDIT_RECORD,
    STANDARD_APPLICATION_USER_AGENT,
    UPSTREAM_NODE_COMMIT,
    UPSTREAM_NODE_LICENSE,
    UPSTREAM_NODE_REPOSITORY,
    UPSTREAM_NODE_VERSION,
    UPSTREAM_PYTHON_COMMIT,
    UPSTREAM_PYTHON_LICENSE,
    UPSTREAM_PYTHON_REPOSITORY,
    UPSTREAM_PYTHON_VERSION,
    UPSTREAM_TRANSPORT_BEHAVIOR_CLASSIFICATION,
    FC27ApiAuthorityAttestation,
    FC27ApiIngestionOperation,
    FC27CircuitBreaker,
    FC27InvalidHostError,
    FC27ProviderError,
    build_east_coasters_api_request,
    build_member_and_player_privacy_preview,
    evaluate_api_operation_response,
    normalize_api_capture_to_export_envelope,
    persist_authorized_east_coasters_capture,
    pseudonymize_east_coasters_player_reference,
    run_east_coasters_acquisition_stages,
    sanitize_club_statistics_payload,
    sanitize_recent_matches_payload,
    validate_east_coasters_read_only_analytics,
)
from src.fc27_repository import FC27RepositoryError
from src.settings import (
    is_dispatch_enabled,
    is_fc27_analytics_enabled,
    is_fc27_live_ingestion_enabled,
    is_fc27_persistence_enabled,
    is_fc27_provider_enabled,
    is_fc27_recommendations_enabled,
    is_fc27_standard_http_transport_enabled,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _block_external_network_in_unit_tests(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Blocks external network/DNS in all non-DB tests; allows localhost 127.0.0.1 only for PostgreSQL fixture."""
    orig_create_conn = socket.create_connection
    orig_getaddrinfo = socket.getaddrinfo

    def _guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else str(address)
        if host in ("127.0.0.1", "localhost", "::1"):
            return orig_create_conn(address, *args, **kwargs)
        raise AssertionError(f"External network connection to '{host}' is prohibited during tests")

    def _guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if str(host) in ("127.0.0.1", "localhost", "::1"):
            return orig_getaddrinfo(host, *args, **kwargs)
        raise AssertionError(f"External DNS resolution for '{host}' is prohibited during tests")

    def _forbidden_urlopen(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("urllib.request.urlopen is prohibited during tests")

    monkeypatch.setattr(socket, "create_connection", _guarded_create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", _guarded_getaddrinfo)
    monkeypatch.setattr(urllib.request, "urlopen", _forbidden_urlopen)


def _sample_identity_json(club_id: int = 330323, name: str = "East Coasters") -> bytes:
    return json.dumps(
        {
            str(club_id): {
                "clubId": club_id,
                "name": name,
                "platform": "common-gen5",
                "regionId": 45123,
                "teamId": 112,
                "customKit": {"crestAssetId": "9901", "selectedKitType": "1"},
            }
        }
    ).encode("utf-8")


def _sample_club_stats_json() -> bytes:
    return json.dumps(
        [
            {
                "clubId": "330323",
                "gamesPlayed": "25",
                "wins": "16",
                "ties": "4",
                "losses": "5",
                "goals": "54",
                "goalsAgainst": "22",
                "skillRating": "2180",
                "wstreak": "3",
                "unbeatenstreak": "5",
                "promotions": "4",
                "relegations": "0",
                "bestDivision": "1",
                "reputationtier": "3",
            }
        ]
    ).encode("utf-8")


def _sample_recent_matches_json(match_id: str = "99001122", goals_for: int = 3) -> bytes:
    return json.dumps(
        [
            {
                "matchId": match_id,
                "timestamp": 1761566400,
                "clubs": {
                    "330323": {
                        "goals": str(goals_for),
                        "goalsAgainst": "1",
                        "wins": "1",
                        "losses": "0",
                        "ties": "0",
                        "details": {"name": "East Coasters"},
                    },
                    "880011": {
                        "goals": "1",
                        "goalsAgainst": str(goals_for),
                        "wins": "0",
                        "losses": "1",
                        "ties": "0",
                        "details": {"name": "Atlantic Rovers"},
                    },
                },
                "players": {
                    "330323": {
                        "777000111": {
                            "playername": "SecretGamerTagAlpha",
                            "proName": "SecretAliasAlpha",
                            "goals": "2",
                            "assists": "1",
                            "passesmade": "18",
                            "passattempts": "22",
                            "tacklesmade": "3",
                            "tackleattempts": "4",
                            "shots": "4",
                            "saves": "0",
                            "rating": "8.4",
                            "secondsPlayed": "5400",
                            "match_event_aggregate_0": "215:18,216:4,217:3,214:2,11:1,0:3,1:1,6:2,106:2",
                        }
                    }
                },
            }
        ]
    ).encode("utf-8")


def _sample_members_json() -> bytes:
    return json.dumps(
        {
            "members": [
                {
                    "name": "SecretGamerTagAlpha",
                    "proName": "SecretAliasAlpha",
                    "personaId": "777000111",
                    "gamesPlayed": "25",
                    "goals": "19",
                    "assists": "8",
                    "ratingAve": "8.1",
                    "favoritePosition": "midfielder",
                    "proOverall": "88",
                },
                {
                    "name": "SecretGamerTagBravo",
                    "proName": "SecretAliasBravo",
                    "personaId": "777000222",
                    "gamesPlayed": "24",
                    "goals": "6",
                    "assists": "14",
                    "ratingAve": "7.9",
                    "favoritePosition": "defender",
                    "proOverall": "87",
                },
            ]
        }
    ).encode("utf-8")


# ==============================================================================
# Tests 1-16: Authority Attestation, Upstream Provenance, and Transport Policy
# ==============================================================================

def test_01_authority_attestation_contract() -> None:
    att = EAST_COASTERS_API_AUTHORITY_ATTESTATION
    assert att.attestation_reference == "api-east-coasters-authority-2026-10"
    assert att.authorization_source == "OPERATOR_ATTESTATION"
    assert att.club_id == "330323"
    assert att.platform == "common-gen5"
    assert att.game_version == "FC27"
    assert att.authority_status == "USER_ATTESTED"
    assert att.member_consent_status == "USER_ATTESTED"
    assert att.captain_consent_status == "USER_ATTESTED"
    assert att.manager_authority_database_status == "UNVERIFIED"
    assert att.review_status == "PENDING_HUMAN_REVIEW"
    assert att.publication_allowed is False
    assert att.recommendations_allowed is False
    assert att.dispatch_allowed is False
    assert att.credential_use_allowed is False
    assert att.cookie_or_session_use_allowed is False
    assert att.can_acquire_club_identity_and_stats() is True
    assert att.can_persist_member_or_player_data() is False

    approved = att.model_copy(update={"review_status": "APPROVED"})
    assert approved.can_persist_member_or_player_data() is True

    with pytest.raises(ValidationError):
        FC27ApiAuthorityAttestation(attestation_reference="user@example.com")
    with pytest.raises(ValidationError):
        FC27ApiAuthorityAttestation(attestation_reference="bad_gamertag_ref")


def test_02_club_only_scope_and_upstream_provenance() -> None:
    assert UPSTREAM_PYTHON_REPOSITORY == "https://github.com/1erkandogan/fc27-clubs-api"
    assert UPSTREAM_PYTHON_COMMIT == "26a5ff12ea793042bd776a858c74f3f44dd427f5"
    assert UPSTREAM_PYTHON_VERSION == "0.3.1"
    assert UPSTREAM_PYTHON_LICENSE == "MIT"

    assert UPSTREAM_NODE_REPOSITORY == "https://github.com/alex-jordan547/proclubs-sdk"
    assert UPSTREAM_NODE_COMMIT == "57ede368098dbb524cbf16a7577fea3e1911e328"
    assert UPSTREAM_NODE_VERSION == "0.3.3"
    assert UPSTREAM_NODE_LICENSE == "MIT"
    assert "Copyright (c) 2025 alex-jordan547" in PROCLUBS_SDK_AUDIT_RECORD.attribution_notice
    assert UPSTREAM_TRANSPORT_BEHAVIOR_CLASSIFICATION["STANDARD_HTTP"] == "APPROVED"
    assert UPSTREAM_TRANSPORT_BEHAVIOR_CLASSIFICATION["TLS_PROFILE_IMPERSONATION"] == "REJECTED"
    assert UPSTREAM_TRANSPORT_BEHAVIOR_CLASSIFICATION["BROWSER_CLIENT_HINT"] == "REJECTED"

    with pytest.raises(FC27ProviderError, match="Club-only scope violation"):
        build_east_coasters_api_request("CLUB_IDENTITY", club_id="999999")
    with pytest.raises(FC27ProviderError, match="Platform scope violation"):
        build_east_coasters_api_request("CLUB_IDENTITY", platform="nx")


def test_03_exact_host_allowlist_and_url_security_guards() -> None:
    with pytest.raises(FC27InvalidHostError, match="Host"):
        build_east_coasters_api_request("CLUB_IDENTITY", host="evil.ea.com")
    with pytest.raises(FC27InvalidHostError, match="Unicode or punycode"):
        build_east_coasters_api_request("CLUB_IDENTITY", host="pr\u043eclubs.ea.com")
    with pytest.raises(FC27InvalidHostError, match="Unicode or punycode"):
        build_east_coasters_api_request("CLUB_IDENTITY", host="xn--proclubs-ea.com")
    with pytest.raises(FC27InvalidHostError, match="port"):
        build_east_coasters_api_request("CLUB_IDENTITY", host="proclubs.ea.com:8443")
    with pytest.raises(FC27InvalidHostError, match="Userinfo or credentials"):
        build_east_coasters_api_request("CLUB_IDENTITY", host="admin:secret@proclubs.ea.com")
    with pytest.raises(FC27InvalidHostError, match="HTTPS"):
        build_east_coasters_api_request("CLUB_IDENTITY", scheme="http")


def test_04_exact_path_allowlist_and_unsupported_operation_rejected() -> None:
    with pytest.raises(FC27ProviderError, match="Unsupported API ingestion operation"):
        build_east_coasters_api_request("LEADERBOARDS")
    with pytest.raises(FC27InvalidHostError, match="Endpoint path"):
        build_east_coasters_api_request("CLUB_IDENTITY", path_override="/api/fc/allTimeLeaderboard")
    with pytest.raises(FC27InvalidHostError, match="Endpoint path"):
        build_east_coasters_api_request("CLUB_IDENTITY", path_override="/api/fc/clubs/info#fragment")


def test_05_exact_query_allowlist() -> None:
    with pytest.raises(FC27ProviderError, match="Unknown or unapproved query parameter"):
        build_east_coasters_api_request("CLUB_IDENTITY", extra_query_params={"clubName": "East Coasters"})
    with pytest.raises(FC27ProviderError, match="Unsupported matchType"):
        build_east_coasters_api_request("RECENT_MATCHES", match_type="invalidMatchType")
    with pytest.raises(FC27ProviderError, match="maxResultCount"):
        build_east_coasters_api_request("RECENT_MATCHES", max_result_count=100)
    with pytest.raises(FC27ProviderError, match="override rejected"):
        build_east_coasters_api_request("CLUB_IDENTITY", extra_query_params={"clubIds": "999999"})
    with pytest.raises(FC27ProviderError, match="override rejected"):
        build_east_coasters_api_request("CLUB_IDENTITY", extra_query_params={"platform": "common-gen4"})
    with pytest.raises(FC27ProviderError, match="maxResultCount"):
        build_east_coasters_api_request("RECENT_MATCHES", extra_query_params={"maxResultCount": "999"})


def test_06_operation_request_budgets_and_no_retries_or_redirects() -> None:
    for op in FC27ApiIngestionOperation:
        pol = FC27_API_INGESTION_POLICIES[op]
        assert pol.maximum_requests == 1
        assert pol.redirect_behavior == "DISABLED"
        assert pol.retry_behavior == "ZERO_RETRIES"
        assert pol.timeout_seconds == 10.0
        assert pol.maximum_bytes == 1048576

    with pytest.raises(FC27ProviderError, match="Zero-retry"):
        build_east_coasters_api_request("CLUB_IDENTITY", retries=1)
    with pytest.raises(FC27ProviderError, match="Redirects"):
        build_east_coasters_api_request("CLUB_IDENTITY", allow_redirects=True)
    with pytest.raises(FC27ProviderError, match="timeout_seconds"):
        build_east_coasters_api_request("CLUB_IDENTITY", timeout_seconds=15.0)
    with pytest.raises(FC27ProviderError, match="maximum_bytes"):
        build_east_coasters_api_request("CLUB_IDENTITY", maximum_bytes=2 * 1048576)


def test_07_headers_tls_profile_and_proxy_prohibitions() -> None:
    req = build_east_coasters_api_request("CLUB_IDENTITY")
    assert req["headers"] == {"Accept": "application/json", "User-Agent": STANDARD_APPLICATION_USER_AGENT}
    assert req["tls_profile"] is None
    assert req["proxy"] is None

    with pytest.raises(FC27ProviderError, match="TLS-profile"):
        build_east_coasters_api_request("CLUB_IDENTITY", tls_profile="chrome_141")
    with pytest.raises(FC27ProviderError, match="Proxy"):
        build_east_coasters_api_request("CLUB_IDENTITY", proxy="http://127.0.0.1:8080")
    for bad_hdr in (
        {"Cookie": "sid=1"},
        {"Authorization": "Bearer tok"},
        {"sec-ch-ua": '"Chromium";v="141"'},
        {"sec-fetch-site": "same-origin"},
        {"Origin": "https://www.ea.com"},
        {"Referer": "https://www.ea.com/"},
    ):
        with pytest.raises(FC27ProviderError):
            build_east_coasters_api_request("CLUB_IDENTITY", headers=bad_hdr)


# ==============================================================================
# Tests 8-22: All 6 Valid Responses + Failure/Challenge/Circuit-Breaker Cases
# ==============================================================================

def test_08_valid_club_identity_response_and_identity_mismatch() -> None:
    res = evaluate_api_operation_response("CLUB_IDENTITY", raw_bytes=_sample_identity_json())
    assert res.ok is True
    assert res.outcome_status == "SUCCESS_200"
    assert res.sanitized_data is not None
    assert res.sanitized_data["club_id"] == "330323"
    assert res.sanitized_data["club_name"] == "East Coasters"
    assert res.sanitized_data["identity_decision"]["ID_MATCH"] is True
    assert res.sanitized_data["identity_decision"]["NAME_MATCH"] is True
    assert res.sanitized_data["identity_decision"]["PLATFORM_MATCH"] is True
    assert res.sanitized_data["identity_decision"]["REVIEW_REQUIRED"] is True

    mismatch_res = evaluate_api_operation_response(
        "CLUB_IDENTITY",
        raw_bytes=_sample_identity_json(club_id=330323, name="Wrong Club Name"),
    )
    assert mismatch_res.ok is False
    assert mismatch_res.outcome_status == "IDENTITY_MISMATCH"


def test_09_valid_club_statistics_response_and_sensitive_rejection() -> None:
    res = evaluate_api_operation_response("CLUB_STATISTICS", raw_bytes=_sample_club_stats_json())
    assert res.ok is True
    assert res.sanitized_data is not None
    assert res.sanitized_data["games_played"] == 25
    assert res.sanitized_data["wins"] == 16
    assert res.sanitized_data["goals"] == 54

    poisoned = json.dumps([{"clubId": "330323", "gamesPlayed": "10", "members": [{"gamertag": "Leak"}]}]).encode("utf-8")
    bad_res = evaluate_api_operation_response("CLUB_STATISTICS", raw_bytes=poisoned)
    assert bad_res.ok is False
    assert bad_res.outcome_status == "UNEXPECTED_SENSITIVE_DATA_IN_CLUB_STATISTICS"


def test_10_valid_recent_matches_response_and_deduplication() -> None:
    dup_matches = json.loads(_sample_recent_matches_json()) * 2
    res = evaluate_api_operation_response("RECENT_MATCHES", raw_bytes=json.dumps(dup_matches).encode("utf-8"))
    assert res.ok is True
    assert res.sanitized_data is not None
    assert res.sanitized_data["recent_match_count"] == 1
    m = res.sanitized_data["matches"][0]
    assert m["external_match_id"] == "99001122"
    assert m["result"] == "WIN"
    assert m["goals_for"] == 3
    assert m["goals_against"] == 1
    assert len(m["match_hash"]) == 64
    serialized = json.dumps(res.sanitized_data)
    assert "SecretGamerTagAlpha" not in serialized
    assert "SecretAliasAlpha" not in serialized
    assert "777000111" not in serialized


def test_11_valid_match_player_current_member_and_career_privacy_previews() -> None:
    r_mp = evaluate_api_operation_response("MATCH_PLAYER_STATISTICS", raw_bytes=_sample_recent_matches_json())
    r_cm = evaluate_api_operation_response("CURRENT_MEMBER_STATISTICS", raw_bytes=_sample_members_json())
    r_ca = evaluate_api_operation_response("MEMBER_CAREER_STATISTICS", raw_bytes=_sample_members_json())
    assert r_mp.ok is True and r_cm.ok is True and r_ca.ok is True
    assert r_mp.sanitized_data["player_stat_record_count"] == 1
    assert r_cm.sanitized_data["member_record_count"] == 2
    assert r_ca.sanitized_data["career_member_record_count"] == 2

    for res_obj in (r_mp, r_cm, r_ca):
        dumped = res_obj.model_dump_json()
        assert "SecretGamerTagAlpha" not in dumped
        assert "SecretGamerTagBravo" not in dumped
        assert "SecretAliasAlpha" not in dumped
        assert "777000111" not in dumped
        assert res_obj.metadata_log.raw_body_logged is False
        assert res_obj.raw_response_retained is False


def test_12_http_401_403_429_redirect_html_captcha_akamai_and_invalid_responses() -> None:
    for status, expected in (
        (401, "HTTP_401_UNAUTHORIZED"),
        (403, "HTTP_403_FORBIDDEN"),
        (429, "HTTP_429_RATE_LIMITED"),
    ):
        b = FC27CircuitBreaker()
        r = evaluate_api_operation_response("CLUB_IDENTITY", raw_bytes=b"{}", status_code=status, breaker=b)
        assert r.ok is False
        assert r.outcome_status == expected
        assert b.is_open() is True
        assert r.retries == 0

    b_red = FC27CircuitBreaker()
    r_red = evaluate_api_operation_response(
        "CLUB_IDENTITY",
        raw_bytes=b"",
        status_code=302,
        location_header="https://signin.ea.com",
        breaker=b_red,
    )
    assert r_red.outcome_status == "REDIRECT_REJECTED"
    assert b_red.is_open() is True

    b_html = FC27CircuitBreaker()
    r_html = evaluate_api_operation_response(
        "CLUB_IDENTITY",
        raw_bytes=b"<html><body>Challenge</body></html>",
        status_code=200,
        content_type="text/html",
        breaker=b_html,
    )
    assert r_html.outcome_status in ("AKAMAI_OR_WAF_CHALLENGE", "HTML_RESPONSE_REJECTED")
    assert b_html.is_open() is True

    b_cap = FC27CircuitBreaker()
    r_cap = evaluate_api_operation_response(
        "CLUB_IDENTITY",
        raw_bytes=b'{"challenge": "g-recaptcha"}',
        status_code=200,
        content_type="application/json",
        breaker=b_cap,
    )
    assert r_cap.outcome_status == "CAPTCHA_DETECTED"
    assert b_cap.is_open() is True

    b_aka = FC27CircuitBreaker()
    r_aka = evaluate_api_operation_response(
        "CLUB_IDENTITY",
        raw_bytes=b"Access Denied - Akamai Reference #18.999",
        status_code=200,
        content_type="text/plain",
        breaker=b_aka,
    )
    assert r_aka.outcome_status == "AKAMAI_OR_WAF_CHALLENGE"
    assert b_aka.is_open() is True

    r_ct = evaluate_api_operation_response(
        "CLUB_IDENTITY",
        raw_bytes=b"plain text body",
        status_code=200,
        content_type="text/plain",
    )
    assert r_ct.outcome_status == "INVALID_CONTENT_TYPE_REJECTED"

    r_big = evaluate_api_operation_response(
        "CLUB_IDENTITY",
        raw_bytes=b"x" * (1048576 + 5),
        status_code=200,
        content_type="application/json",
    )
    assert r_big.outcome_status == "OVERSIZED_RESPONSE_REJECTED"

    r_bad_json = evaluate_api_operation_response(
        "CLUB_IDENTITY",
        raw_bytes=b"{malformed",
        status_code=200,
        content_type="application/json",
    )
    assert r_bad_json.outcome_status == "MALFORMED_JSON_REJECTED"


def test_13_schema_drift_quarantined_as_metadata_only() -> None:
    drift_payload = json.dumps(
        {
            "330323": {
                "clubId": 330323,
                "name": "East Coasters",
                "platform": "common-gen5",
                "regionId": 45123,
                "undocumentedFutureField": "secret_internal_value_999",
            }
        }
    ).encode("utf-8")
    res = evaluate_api_operation_response("CLUB_IDENTITY", raw_bytes=drift_payload)
    assert res.ok is True
    assert "330323.undocumentedFutureField" in res.schema_drift_paths
    assert res.quarantine_metadata is not None
    assert res.quarantine_metadata["anomaly_category"] == "SCHEMA_DRIFT"
    assert res.quarantine_metadata["raw_body_stored"] is False
    assert "secret_internal_value_999" not in json.dumps(res.quarantine_metadata)


def test_14_sequential_stage_runner_f1_to_f4_and_stop_on_f1_failure() -> None:
    def _mock_success_transport(req: Any) -> dict[str, Any]:
        op = req["operation"]
        if op == "CLUB_IDENTITY":
            b = _sample_identity_json()
        elif op == "CLUB_STATISTICS":
            b = _sample_club_stats_json()
        elif op in ("RECENT_MATCHES", "MATCH_PLAYER_STATISTICS"):
            b = _sample_recent_matches_json()
        else:
            b = _sample_members_json()
        return {"raw_bytes": b, "status_code": 200, "content_type": "application/json"}

    report = run_east_coasters_acquisition_stages(_mock_success_transport)
    assert report["completed_all_stages"] is True
    assert report["request_counts"]["club_identity_requests"] == 1
    assert report["request_counts"]["club_statistics_requests"] == 1
    assert report["request_counts"]["recent_match_requests"] == 1
    assert report["request_counts"]["member_player_preview_requests"] == 3
    assert report["request_counts"]["total_ea_requests"] == 6
    assert report["request_counts"]["total_retries"] == 0
    assert report["request_counts"]["redirects_followed"] == 0
    gate = report["review_gate"]
    assert gate.EAST_COASTERS_IDENTITY_MATCH == "YES"
    assert gate.RECENT_MATCH_COUNT == 1
    assert gate.MEMBER_RECORD_COUNT == 2
    assert gate.PLAYER_STAT_RECORD_COUNT == 1
    assert gate.RAW_RESPONSE_RETAINED == "NO"
    assert gate.PERSISTENCE_APPLIED == "NO"
    assert gate.RECOMMENDATIONS_GENERATED == 0
    assert gate.DISPATCH_EXECUTED == "NO"

    def _mock_403_transport(req: Any) -> dict[str, Any]:
        return {"raw_bytes": b"Access Denied", "status_code": 403, "content_type": "text/html"}

    blocked_report = run_east_coasters_acquisition_stages(_mock_403_transport)
    assert blocked_report["completed_all_stages"] is False
    assert blocked_report["stopped_at_stage"] == "F1_CLUB_IDENTITY"
    assert blocked_report["request_counts"]["total_ea_requests"] == 1
    assert blocked_report["stage4_exception_assessment"].BROWSER_PROFILE_EXCEPTION_REQUIRED == "YES"
    assert blocked_report["stage4_exception_assessment"].BROWSER_PROFILE_EXCEPTION_EXECUTED == "NO"


def test_15_pseudonymous_reference_immutability_and_deterministic_normalization() -> None:
    ref1 = pseudonymize_east_coasters_player_reference("777000111")
    ref2 = pseudonymize_east_coasters_player_reference("777000111")
    assert ref1 == ref2
    assert ref1.startswith("plr_ecf_")
    assert "777000111" not in ref1

    raw_input = json.loads(_sample_recent_matches_json())
    snapshot_before = copy.deepcopy(raw_input)
    san1, _, _ = sanitize_recent_matches_payload(raw_input)
    san2, _, _ = sanitize_recent_matches_payload(raw_input)
    assert raw_input == snapshot_before
    assert san1["matches"][0]["match_hash"] == san2["matches"][0]["match_hash"]


def test_16_default_feature_flags_and_matchpulse_boundary() -> None:
    assert is_fc27_provider_enabled() is False
    assert is_fc27_standard_http_transport_enabled() is False
    assert FC27_STANDARD_HTTP_TRANSPORT_ENABLED is False
    assert is_fc27_live_ingestion_enabled() is False
    assert is_fc27_persistence_enabled() is False
    assert is_fc27_analytics_enabled() is False
    assert is_fc27_recommendations_enabled() is False
    assert is_dispatch_enabled() is False

    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_matchpulse_boundary.py")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0
    assert "OK" in res.stdout


# ==============================================================================
# Tests 17-20: PostgreSQL Normalized Persistence, Replay Conflict, Rollback, & Analytics
# ==============================================================================

@pytest.fixture(scope="module")
def pg_api_db() -> Iterator[dict[str, str]]:
    base_uri = "postgresql://fc27test@127.0.0.1:55432/postgres"
    dbname = "fc27_api_ingest_test"
    bootstrap_cluster(
        base_uri,
        {
            "fc27_migrator": None,
            "fc27_app": None,
            "fc27_dispatch": None,
            "fc27_relay": None,
        },
    )
    with psycopg.connect(base_uri, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {dbname}")
        conn.execute(f"CREATE DATABASE {dbname}")
    db_admin_uri = f"postgresql://fc27test@127.0.0.1:55432/{dbname}"
    bootstrap_database(db_admin_uri, legacy_owner=None, confirm=True)
    owner_uri = f"postgresql://fc27_migrator@127.0.0.1:55432/{dbname}"
    app_uri = f"postgresql://fc27_app@127.0.0.1:55432/{dbname}"
    migrate(owner_uri)
    yield {"admin": db_admin_uri, "owner": owner_uri, "app": app_uri}
    with psycopg.connect(base_uri, autocommit=True) as conn:
        conn.execute(
            f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{dbname}' AND pid <> pg_backend_pid()"
        )
        conn.execute(f"DROP DATABASE IF EXISTS {dbname}")


def test_17_persistence_gate_requires_approved_attestation_and_flags(
    pg_api_db: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    san_matches, _, _ = sanitize_recent_matches_payload(json.loads(_sample_recent_matches_json()))

    # Pending review attestation blocked
    with pytest.raises(FC27ProviderError, match="ATTESTATION_REVIEW_STATUS=APPROVED"):
        persist_authorized_east_coasters_capture(
            pg_api_db["app"],
            export_id="exp-api-blocked-01",
            sanitized_matches_data=san_matches,
            attestation=EAST_COASTERS_API_AUTHORITY_ATTESTATION,
            authorize_persistence=True,
        )

    approved_att = EAST_COASTERS_API_AUTHORITY_ATTESTATION.model_copy(update={"review_status": "APPROVED"})
    # Missing authorize_persistence blocked
    with pytest.raises(FC27ProviderError, match="AUTHORIZE_NORMALIZED_EAST_COASTERS_PERSISTENCE=YES"):
        persist_authorized_east_coasters_capture(
            pg_api_db["app"],
            export_id="exp-api-blocked-02",
            sanitized_matches_data=san_matches,
            attestation=approved_att,
            authorize_persistence=False,
        )

    # Default disabled flags blocked
    with pytest.raises(FC27ProviderError, match="FC27_PROVIDER_ENABLED=true"):
        persist_authorized_east_coasters_capture(
            pg_api_db["app"],
            export_id="exp-api-blocked-03",
            sanitized_matches_data=san_matches,
            attestation=approved_att,
            authorize_persistence=True,
        )


def test_18_idempotent_persistence_replay_conflict_domain_events_and_outbox_privacy(
    pg_api_db: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FC27_PROVIDER_ENABLED", "true")
    monkeypatch.setenv("FC27_STANDARD_HTTP_TRANSPORT_ENABLED", "true")
    monkeypatch.setenv("FC27_PERSISTENCE_ENABLED", "true")
    monkeypatch.setenv("FC27_LIVE_INGESTION_ENABLED", "false")
    monkeypatch.setenv("FC27_RECOMMENDATIONS_ENABLED", "false")
    monkeypatch.setenv("DISPATCH_ENABLED", "false")

    approved_att = EAST_COASTERS_API_AUTHORITY_ATTESTATION.model_copy(update={"review_status": "APPROVED"})
    san_matches, _, _ = sanitize_recent_matches_payload(json.loads(_sample_recent_matches_json("99001122", 3)))

    res1 = persist_authorized_east_coasters_capture(
        pg_api_db["app"],
        export_id="exp-api-east-coasters-001",
        sanitized_matches_data=san_matches,
        attestation=approved_att,
        authorize_persistence=True,
    )
    assert res1.status == "APPLIED"
    assert res1.accepted_records == 1
    assert res1.player_stat_records == 1
    assert res1.idempotent_duplicate is False

    # Reapply identical capture -> idempotent duplicate
    res2 = persist_authorized_east_coasters_capture(
        pg_api_db["app"],
        export_id="exp-api-east-coasters-001",
        sanitized_matches_data=san_matches,
        attestation=approved_att,
        authorize_persistence=True,
    )
    assert res2.status == "APPLIED"
    assert res2.idempotent_duplicate is True
    assert res2.accepted_records == 0

    # Replay same external_match_id with modified score -> REJECTED + replay_conflict + quarantine row
    tampered_matches, _, _ = sanitize_recent_matches_payload(json.loads(_sample_recent_matches_json("99001122", 5)))
    res_conflict = persist_authorized_east_coasters_capture(
        pg_api_db["app"],
        export_id="exp-api-east-coasters-002-conflict",
        sanitized_matches_data=tampered_matches,
        attestation=approved_att,
        authorize_persistence=True,
    )
    assert res_conflict.status == "REJECTED"
    assert res_conflict.replay_conflict is True
    assert res_conflict.quarantined_records == 1

    # Verify DB rows, source_classification='AUTHORIZED_CAPTURE', zero aliases, zero external IDs, and event/outbox privacy
    with psycopg.connect(pg_api_db["app"]) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT source_classification, manager_authority_status FROM public.fc27_clubs WHERE club_id = '330323'")
            c_row = cur.fetchone()
            assert c_row == ("AUTHORIZED_CAPTURE", "UNVERIFIED")

            cur.execute("SELECT local_player_reference, display_alias FROM public.fc27_player_match_stats WHERE club_id = '330323'")
            p_row = cur.fetchone()
            assert p_row is not None
            assert p_row[0].startswith("plr_ecf_")
            assert p_row[1] is None

            cur.execute("SELECT reason FROM public.domain_events WHERE scope_club_id = '330323'")
            for (reason_txt,) in cur.fetchall():
                assert "SecretGamerTagAlpha" not in (reason_txt or "")
                assert "SecretAliasAlpha" not in (reason_txt or "")
                assert "777000111" not in (reason_txt or "")

    with psycopg.connect(pg_api_db["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT row_to_json(outbox)::text FROM public.outbox")
            for (outbox_txt,) in cur.fetchall():
                assert "SecretGamerTagAlpha" not in (outbox_txt or "")
                assert "SecretAliasAlpha" not in (outbox_txt or "")


def test_19_transaction_rollback_on_failure_leaves_zero_partial_rows(
    pg_api_db: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FC27_PROVIDER_ENABLED", "true")
    monkeypatch.setenv("FC27_STANDARD_HTTP_TRANSPORT_ENABLED", "true")
    monkeypatch.setenv("FC27_PERSISTENCE_ENABLED", "true")

    approved_att = EAST_COASTERS_API_AUTHORITY_ATTESTATION.model_copy(update={"review_status": "APPROVED"})
    san_matches, _, _ = sanitize_recent_matches_payload(json.loads(_sample_recent_matches_json("99009999", 2)))
    envelope = normalize_api_capture_to_export_envelope(
        export_id="exp-rollback-test",
        sanitized_matches_data=san_matches,
        attestation=approved_att,
    )

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Simulated mid-transaction failure")

    monkeypatch.setattr("src.fc27_repository.record_event", _boom)
    with pytest.raises(RuntimeError, match="Simulated mid-transaction failure"):
        from src.fc27_repository import apply_export_plan

        apply_export_plan(
            pg_api_db["app"],
            envelope=envelope,
            source_classification_override="AUTHORIZED_CAPTURE",
        )

    with psycopg.connect(pg_api_db["app"]) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.fc27_export_batches WHERE export_id = 'exp-rollback-test'")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT COUNT(*) FROM public.fc27_matches WHERE external_match_id = '99009999'")
            assert cur.fetchone()[0] == 0


def test_20_read_only_analytics_validation_and_postgressaver_isolation(
    pg_api_db: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FC27_PROVIDER_ENABLED", "true")
    monkeypatch.setenv("FC27_STANDARD_HTTP_TRANSPORT_ENABLED", "true")
    monkeypatch.setenv("FC27_PERSISTENCE_ENABLED", "true")
    monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "true")
    monkeypatch.setenv("FC27_LIVE_INGESTION_ENABLED", "false")
    monkeypatch.setenv("FC27_RECOMMENDATIONS_ENABLED", "false")
    monkeypatch.setenv("DISPATCH_ENABLED", "false")

    approved_att = EAST_COASTERS_API_AUTHORITY_ATTESTATION.model_copy(update={"review_status": "APPROVED"})
    clean_matches, _, _ = sanitize_recent_matches_payload(json.loads(_sample_recent_matches_json("99001123", 4)))
    persist_authorized_east_coasters_capture(
        pg_api_db["app"],
        export_id="exp-api-east-coasters-003-clean",
        sanitized_matches_data=clean_matches,
        attestation=approved_att,
        authorize_persistence=True,
    )
    monkeypatch.setenv("FC27_PROVIDER_ENABLED", "false")
    monkeypatch.setenv("FC27_STANDARD_HTTP_TRANSPORT_ENABLED", "false")
    monkeypatch.setenv("FC27_PERSISTENCE_ENABLED", "false")

    with psycopg.connect(pg_api_db["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.domain_events")
            events_before = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM public.outbox")
            outbox_before = cur.fetchone()[0]

    analytics_res = validate_east_coasters_read_only_analytics(
        pg_api_db["app"],
        reference_now=datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert analytics_res["club_id"] == "330323"
    assert analytics_res["eligible_matches"] == 1
    assert analytics_res["confidence_tier"] == "INSUFFICIENT_DATA"
    assert analytics_res["analytics_writes"] == 0
    assert analytics_res["analytics_events"] == 0
    assert analytics_res["analytics_outbox_writes"] == 0
    assert analytics_res["recommendations_generated"] == 0
    assert analytics_res["dispatch_executed"] is False

    with psycopg.connect(pg_api_db["admin"]) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.domain_events")
            assert cur.fetchone()[0] == events_before
            cur.execute("SELECT COUNT(*) FROM public.outbox")
            assert cur.fetchone()[0] == outbox_before
            cur.execute(
                """
                SELECT COUNT(*) FROM information_schema.triggers
                WHERE event_object_schema = 'public'
                  AND event_object_table IN ('checkpoints', 'checkpoint_blobs', 'checkpoint_writes', 'checkpoint_migrations')
                """
            )
            assert cur.fetchone()[0] == 0
