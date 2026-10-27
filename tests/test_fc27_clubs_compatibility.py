"""Comprehensive Test Suite for FC 27 Multi-Club Provider Integration & Audited Compatibility Layer.

Covers all 112 mandatory verification requirements across:
- Audit and provenance (1-6)
- Capabilities (7-11)
- Query validation (12-22)
- Application permissions (23-28)
- Policy & Request builder (29-41)
- Compatibility adapter (42-55)
- Sanitizer (56-62)
- Identity matching (63-70)
- Multi-club behavior (71-76)
- Cache and circuit breaker (77-87)
- Event/formula registry reconciliation (88-93)
- Feature isolation (94-103)
- CLI behavior (104-112)
"""
from __future__ import annotations

import socket
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Tuple

import pytest
from pydantic import ValidationError

from scripts.fc27_club_lookup import build_arg_parser, run_cli
from src.fc27_clubs import (
    ADOPTION_STRATEGY,
    ALL_OPERATIONS,
    AUDITED_LICENSE_SHA256,
    AUDITED_UPSTREAM_COMMIT,
    AUDITED_UPSTREAM_LICENSE,
    AUDITED_UPSTREAM_REPOSITORY,
    AUDITED_UPSTREAM_VERSION,
    EAST_COASTERS_SECONDARY_OBSERVATION_URLS,
    EAST_COASTERS_SEED_PROFILE,
    MIT_ATTRIBUTION_NOTICE,
    OFFICIAL_EA_DEVELOPER_API,
    PROVIDER_NAME,
    PROVIDER_STATUS,
    READY_FOR_ONE_PUBLIC_IDENTITY_PROBE,
    RECONCILED_EVENT_REGISTRY,
    SEARCH_LIVE_ENABLED,
    UPSTREAM_AUDIT_RECORD,
    UPSTREAM_SINGLE_EVENT_DEFS,
    USER_EXPORT_PILOT_SEQUENCE,
    ComponentAdoptionClassification,
    FC27AccessAction,
    FC27AccessRole,
    FC27ApplicationClubAccess,
    FC27Cache,
    FC27CircuitBreaker,
    FC27ClubIdentityMappedRecord,
    FC27ClubLookupResult,
    FC27ClubMatchSummaryRecord,
    FC27ClubMemberSummaryRecord,
    FC27ClubOverviewRecord,
    FC27ClubQuery,
    FC27ClubsCompatibilityAdapter,
    FC27EvidenceStatusHierarchy,
    FC27IdentityOutcome,
    FC27MemberCareerStatsRecord,
    FC27OfflineEvidenceManifest,
    FC27OfflinePublicObservation,
    FC27Operation,
    FC27Platform,
    FC27PlayerMatchStatsRecord,
    FC27ProviderCapabilities,
    FC27ProviderResponseEnvelope,
    FC27PublicProviderPolicy,
    FC27RoleAttestationTemplate,
    FC27SourceIndependenceGroup,
    UpstreamAuditRecord,
    build_default_east_coasters_offline_observations,
    build_fc27_club_request,
    canonicalize_secondary_url,
    compare_club_identity,
    compute_evidence_content_sha256,
    compute_url_sha256,
    evaluate_club_access,
    evaluate_fc27_feature_flags,
    evaluate_offline_club_evidence,
    flag_enabled,
    get_attestation_templates,
    has_permission,
    lookup_fc27_club,
    match_identity,
    parse_and_evaluate_match_events,
    reconcile_event_registry,
    sanitize_identity,
    sanitize_identity_payload,
    search_fc27_clubs_mock,
    validate_request_against_policy,
    verify_upstream_audit_gate,
)
from src.fc27_provider import (
    FC27_EVENT_REGISTRY,
    FC27_FORMULA_REGISTRY,
    EventStatus,
    FC27InvalidHostError,
    FC27LiveAccessBlockedError,
    FC27ProviderError,
)
from src.settings import (
    is_dispatch_enabled,
    is_dispatch_worker_enabled,
    is_fc27_live_ingestion_enabled,
    is_fc27_provider_dry_run,
    is_fc27_provider_enabled,
    is_fc27_public_identity_lookup_enabled,
    is_fc27_recommendations_enabled,
    is_fc27_upstream_compatibility_enabled,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def block_all_network_and_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enforces zero outbound network connections and zero DNS resolutions during tests."""

    def _forbidden_connect(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Outbound network connection attempted during test!")

    def _forbidden_dns(*args: Any, **kwargs: Any) -> None:
        pytest.fail("DNS resolution attempted during test!")

    monkeypatch.setattr(socket, "create_connection", _forbidden_connect)
    monkeypatch.setattr(socket, "getaddrinfo", _forbidden_dns)


# ==============================================================================
# 1-6: Audit and Provenance Tests
# ==============================================================================

def test_001_exact_upstream_commit_required() -> None:
    assert verify_upstream_audit_gate(UPSTREAM_AUDIT_RECORD) is True
    assert UPSTREAM_AUDIT_RECORD.upstream_commit == "26a5ff12ea793042bd776a858c74f3f44dd427f5"
    with pytest.raises(ValidationError):
        UpstreamAuditRecord(upstream_commit="main")
    with pytest.raises(ValidationError):
        UpstreamAuditRecord(upstream_commit="0" * 40)


def test_002_license_compatibility_required() -> None:
    assert UPSTREAM_AUDIT_RECORD.upstream_license == "MIT"
    assert UPSTREAM_AUDIT_RECORD.license_compatible is True
    assert UPSTREAM_AUDIT_RECORD.license_file_sha256 == AUDITED_LICENSE_SHA256
    with pytest.raises(ValidationError):
        UpstreamAuditRecord(license_compatible=False)


def test_003_attribution_retained() -> None:
    assert "Copyright (c) 2025 Erkan Dogan" in MIT_ATTRIBUTION_NOTICE
    assert "Permission is hereby granted, free of charge" in UPSTREAM_AUDIT_RECORD.attribution_requirements
    adr_text = (REPO_ROOT / "docs" / "ADR_FC27_EAST_COASTERS_PLATFORM.md").read_text(encoding="utf-8")
    assert "Copyright (c) 2025 Erkan Dogan" in adr_text
    assert AUDITED_UPSTREAM_COMMIT in adr_text
    with pytest.raises(ValidationError):
        UpstreamAuditRecord(attribution_requirements="missing notice")


def test_004_rejected_components_unavailable() -> None:
    for comp in (
        "src/fc_clubs_api/_http.py",
        "src/fc_clubs_api/client.py:get_json",
        "src/fc_clubs_api/client.py:find_club_id",
        "src/fc_clubs_api/_output.py",
        "src/fc_clubs_api/__main__.py",
    ):
        assert comp in UPSTREAM_AUDIT_RECORD.prohibited_components
        assert (
            UPSTREAM_AUDIT_RECORD.component_classifications[comp]
            == ComponentAdoptionClassification.REJECTED.value
        )


def test_005_browser_transport_rejected() -> None:
    assert UPSTREAM_AUDIT_RECORD.transport_review_pass is False
    assert UPSTREAM_AUDIT_RECORD.browser_impersonation_required is True
    assert READY_FOR_ONE_PUBLIC_IDENTITY_PROBE is False
    assert ADOPTION_STRATEGY == "INTERNAL_REIMPLEMENTATION"


def test_006_unknown_upstream_version_rejected() -> None:
    with pytest.raises(ValueError):
        FC27ClubsCompatibilityAdapter(upstream_version="9.9.9")
    with pytest.raises(ValueError):
        FC27ClubsCompatibilityAdapter(upstream_commit="f" * 40)
    with pytest.raises(ValidationError):
        FC27ProviderCapabilities(upstream_version="0.0.0")


# ==============================================================================
# 7-11: Provider Capabilities Tests
# ==============================================================================

def test_007_supported_operation_declarations() -> None:
    caps = FC27ProviderCapabilities()
    expected_ops = (
        "CLUB_SEARCH",
        "CLUB_IDENTITY",
        "CLUB_OVERVIEW",
        "CLUB_MATCHES",
        "CLUB_MEMBERS",
        "MEMBER_STATS",
        "MEMBER_CAREER_STATS",
        "MATCH_PLAYER_STATS",
        "PLAYOFF_ACHIEVEMENTS",
        "LEADERBOARDS",
    )
    assert caps.supported_operations == expected_ops
    assert caps.supported_platforms == ("common-gen5", "nx")
    assert caps.supported_game_versions == ("FC27",)
    assert caps.provider == "EA_FC_CLUBS_WEB"
    assert OFFICIAL_EA_DEVELOPER_API is False
    assert PROVIDER_STATUS == "UNOFFICIAL_PUBLIC_WEB_INTERFACE"


def test_008_live_identity_only() -> None:
    caps = FC27ProviderCapabilities()
    assert caps.design_eligible_operations == ("CLUB_IDENTITY",)
    assert caps.live_executable_operations == ()
    assert caps.live_enabled_operations == ()
    with pytest.raises(ValidationError):
        FC27ProviderCapabilities(design_eligible_operations=("CLUB_IDENTITY", "CLUB_MATCHES"))
    with pytest.raises(ValidationError):
        FC27ProviderCapabilities(live_executable_operations=("CLUB_IDENTITY",))
    with pytest.raises(ValidationError):
        FC27ProviderCapabilities(live_enabled_operations=("CLUB_IDENTITY",))


def test_009_other_live_operations_disabled() -> None:
    adapter = FC27ClubsCompatibilityAdapter()
    actor = FC27ApplicationClubAccess(actor_id="viewer-1", role=FC27AccessRole.VIEWER)
    policy = FC27PublicProviderPolicy()
    for op in ALL_OPERATIONS:
        if op == "CLUB_IDENTITY":
            continue
        with pytest.raises(FC27LiveAccessBlockedError, match="OPERATION_NOT_ENABLED"):
            adapter.map_operation_endpoint(op, live=True)
        q = FC27ClubQuery(query_id=f"q-{op}", operation=op, club_id="330323", club_name="East Coasters")
        res = lookup_fc27_club(actor, FC27AccessAction.VIEW_PUBLIC_CLUB_IDENTITY, q, adapter, policy)
        assert res.availability_status == "OPERATION_NOT_ENABLED"
        assert "OPERATION_NOT_ENABLED" in res.reason_codes


def test_010_mock_operations_available() -> None:
    caps = FC27ProviderCapabilities()
    assert set(caps.mock_enabled_operations) == set(ALL_OPERATIONS)
    adapter = FC27ClubsCompatibilityAdapter()
    for op in ALL_OPERATIONS:
        endpoint = adapter.map_operation_endpoint(op, live=False)
        assert endpoint.startswith("/api/fc/")


def test_011_personal_data_capability_explicit() -> None:
    caps = FC27ProviderCapabilities()
    assert caps.personal_data_possible is True
    assert caps.raw_response_supported is False
    assert caps.persistence_supported is False
    assert caps.cache_supported is True
    assert caps.review_required is True
    with pytest.raises(ValidationError):
        FC27ProviderCapabilities(personal_data_possible=False)


# ==============================================================================
# 12-22: Query Validation Tests
# ==============================================================================

def test_012_club_id_query() -> None:
    q = FC27ClubQuery(query_id="q-id", club_id="330323")
    assert q.club_id == "330323"
    assert q.platform == "common-gen5"
    assert q.maximum_requests == 1
    assert q.maximum_response_bytes == 1048576
    assert q.timeout_seconds == 10.0


def test_013_club_name_query() -> None:
    q = FC27ClubQuery(query_id="q-name", operation="CLUB_SEARCH", club_name="East Coasters")
    assert q.club_name == "East Coasters"
    assert q.club_id is None


def test_014_missing_name_and_id() -> None:
    with pytest.raises(ValidationError, match="At least one of club_id or club_name is required"):
        FC27ClubQuery(query_id="q-missing")


def test_015_invalid_platform() -> None:
    with pytest.raises(ValidationError, match="Invalid platform"):
        FC27ClubQuery(query_id="q-plat", club_id="330323", platform="ps4")


def test_016_nonnumeric_id() -> None:
    with pytest.raises(ValidationError, match="Nonnumeric"):
        FC27ClubQuery(query_id="q-nonnum", club_id="330323abc")


def test_017_negative_id() -> None:
    with pytest.raises(ValidationError, match="Negative"):
        FC27ClubQuery(query_id="q-neg", club_id="-330323")


def test_018_crlf_injection() -> None:
    with pytest.raises(ValidationError, match="CRLF"):
        FC27ClubQuery(query_id="q-crlf", club_id="330323\r\nX-Injected: 1")
    with pytest.raises(ValueError, match="CRLF"):
        build_fc27_club_request("330323\r\n")


def test_019_query_injection() -> None:
    with pytest.raises(ValidationError, match="injection"):
        FC27ClubQuery(query_id="q-inj", club_id="330323&matchType=leagueMatch")
    with pytest.raises(ValueError, match="injection"):
        build_fc27_club_request("330323?foo=bar")


def test_020_multiple_club_injection() -> None:
    with pytest.raises(ValidationError, match="Multiple-club injection"):
        FC27ClubQuery(query_id="q-multi", club_id="330323,999999")
    with pytest.raises(ValueError, match="injection"):
        build_fc27_club_request("330323,999999")


def test_021_expired_query() -> None:
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    q = FC27ClubQuery(
        query_id="q-exp",
        club_id="330323",
        requested_at=now - timedelta(minutes=10),
        expires_at=now - timedelta(minutes=1),
    )
    actor = FC27ApplicationClubAccess(actor_id="viewer-1", role=FC27AccessRole.VIEWER)
    res = lookup_fc27_club(
        actor,
        FC27AccessAction.VIEW_PUBLIC_CLUB_IDENTITY,
        q,
        FC27ClubsCompatibilityAdapter(),
        FC27PublicProviderPolicy(),
        clock=lambda: now,
    )
    assert res.availability_status == "QUERY_EXPIRED"
    assert "QUERY_EXPIRED" in res.reason_codes


def test_022_more_than_one_request() -> None:
    with pytest.raises(ValidationError, match="maximum_requests must be exactly 1"):
        FC27ClubQuery(query_id="q-req2", club_id="330323", maximum_requests=2)


# ==============================================================================
# 23-28: Application Permissions Tests
# ==============================================================================

def test_023_viewer_public_identity_permission() -> None:
    viewer = FC27ApplicationClubAccess(actor_id="user-viewer", role=FC27AccessRole.VIEWER)
    assert has_permission(viewer, FC27AccessAction.VIEW_PUBLIC_CLUB_IDENTITY) is True
    assert has_permission(viewer, FC27AccessAction.SUPPLY_EXPORT) is False
    assert has_permission(viewer, FC27AccessAction.APPROVE_CLUB_IDENTITY) is False


def test_024_unauthenticated_actor_denied() -> None:
    unauth = FC27ApplicationClubAccess(actor_id="anon", authenticated=False, role=FC27AccessRole.VIEWER)
    assert has_permission(unauth, FC27AccessAction.VIEW_PUBLIC_CLUB_IDENTITY) is False
    assert has_permission(None, FC27AccessAction.VIEW_PUBLIC_CLUB_IDENTITY) is False

    # Authenticated actor without role or explicit permission also fails closed
    no_role = FC27ApplicationClubAccess(actor_id="logged-in-only", authenticated=True, role=None)
    assert has_permission(no_role, FC27AccessAction.VIEW_PUBLIC_CLUB_IDENTITY) is False


def test_025_lookup_does_not_grant_membership() -> None:
    viewer = FC27ApplicationClubAccess(
        actor_id="user-1",
        role=FC27AccessRole.VIEWER,
        user_membership_status="USER_ASSERTED",
    )
    q = FC27ClubQuery(query_id="q-mem", club_id="330323", expected_name="East Coasters")

    def mock_transport(_q: FC27ClubQuery) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        return (
            {"330323": {"clubId": "330323", "name": "East Coasters", "platform": "common-gen5", "regionId": 12}},
            {"status_code": 200, "content_type": "application/json", "attempt_count": 1},
        )

    res = lookup_fc27_club(
        viewer,
        FC27AccessAction.VIEW_PUBLIC_CLUB_IDENTITY,
        q,
        FC27ClubsCompatibilityAdapter(),
        FC27PublicProviderPolicy(),
        transport=mock_transport,
    )
    assert res.identity_outcome == FC27IdentityOutcome.EXACT_MATCH.value
    assert res.field_statuses["user_membership_status"] == "USER_ASSERTED"
    assert viewer.user_membership_status == "USER_ASSERTED"


def test_026_lookup_does_not_grant_manager_authority() -> None:
    viewer = FC27ApplicationClubAccess(
        actor_id="user-1",
        role=FC27AccessRole.VIEWER,
        user_statement="I think I am a manager",
    )
    q = FC27ClubQuery(query_id="q-mgr", club_id="330323", expected_name="East Coasters")

    def mock_transport(_q: FC27ClubQuery) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        return (
            {"330323": {"clubId": "330323", "name": "East Coasters", "platform": "common-gen5"}},
            {"status_code": 200, "content_type": "application/json", "attempt_count": 1},
        )

    res = lookup_fc27_club(
        viewer,
        FC27AccessAction.VIEW_PUBLIC_CLUB_IDENTITY,
        q,
        FC27ClubsCompatibilityAdapter(),
        FC27PublicProviderPolicy(),
        transport=mock_transport,
    )
    assert res.field_statuses["manager_authority_status"] == "UNVERIFIED"
    assert viewer.has_verified_manager_authority() is False


def test_027_membership_does_not_grant_manager_authority() -> None:
    member = FC27ApplicationClubAccess(
        actor_id="member-batmanL8",
        club_id="330323",
        role=FC27AccessRole.TEAM_MEMBER,
        user_membership_status="VERIFIED",
        manager_authority_status="UNVERIFIED",
    )
    assert member.has_verified_manager_authority() is False
    assert has_permission(member, FC27AccessAction.REVIEW_CLUB_IDENTITY) is False
    assert has_permission(member, FC27AccessAction.APPROVE_CLUB_IDENTITY) is False

    # Even a CLUB_MANAGER role with UNVERIFIED manager_authority_status is denied approval
    unverified_mgr = FC27ApplicationClubAccess(
        actor_id="unverified-mgr",
        club_id="330323",
        role=FC27AccessRole.CLUB_MANAGER,
        manager_authority_status="UNVERIFIED",
    )
    assert unverified_mgr.has_verified_manager_authority() is False
    assert has_permission(unverified_mgr, FC27AccessAction.REVIEW_CLUB_IDENTITY) is False


def test_028_revoked_permission_denied() -> None:
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    revoked = FC27ApplicationClubAccess(
        actor_id="revoked-user",
        role=FC27AccessRole.CLUB_ADMIN,
        revoked=True,
        revoked_at=now,
    )
    assert has_permission(revoked, FC27AccessAction.VIEW_PUBLIC_CLUB_IDENTITY, now=now) is False

    expired = FC27ApplicationClubAccess(
        actor_id="expired-user",
        role=FC27AccessRole.VIEWER,
        expires_at=now - timedelta(seconds=1),
    )
    assert has_permission(expired, FC27AccessAction.VIEW_PUBLIC_CLUB_IDENTITY, now=now) is False


# ==============================================================================
# 29-41: Public Provider Policy & Request Builder Tests
# ==============================================================================

def test_029_https_required() -> None:
    with pytest.raises(ValidationError):
        FC27PublicProviderPolicy(https_only=False)
    with pytest.raises(ValueError, match="https"):
        build_fc27_club_request("330323", scheme="http")


def test_030_exact_host() -> None:
    with pytest.raises(ValidationError):
        FC27PublicProviderPolicy(allowed_hosts=("evil.example.com",))
    with pytest.raises(ValueError, match="Host"):
        build_fc27_club_request("330323", host="evil.ea.com")
    with pytest.raises(ValueError, match="Unicode"):
        build_fc27_club_request("330323", host="pr\u043eclubs.ea.com")
    with pytest.raises(ValueError, match="port"):
        build_fc27_club_request("330323", host="proclubs.ea.com:8443")
    with pytest.raises(ValueError, match="credentials"):
        build_fc27_club_request("330323", host="user:pass@proclubs.ea.com")


def test_031_exact_path() -> None:
    with pytest.raises(ValidationError):
        FC27PublicProviderPolicy(allowed_paths=("/api/fc/clubs/matches",))
    with pytest.raises(ValueError, match="Path"):
        build_fc27_club_request("330323", path="/api/fc/clubs/matches")
    with pytest.raises(ValueError, match="Path"):
        build_fc27_club_request("330323", path="/api/fc/clubs/info#frag")


def test_032_get_only() -> None:
    with pytest.raises(ValidationError):
        FC27PublicProviderPolicy(allowed_methods=("POST",))
    with pytest.raises(ValueError, match="GET"):
        build_fc27_club_request("330323", method="POST")


def test_033_no_request_body() -> None:
    with pytest.raises(ValidationError):
        FC27PublicProviderPolicy(allow_request_body=True)
    with pytest.raises(ValueError, match="body"):
        build_fc27_club_request("330323", body={"clubIds": "330323"})


def test_034_no_redirects() -> None:
    with pytest.raises(ValidationError):
        FC27PublicProviderPolicy(allow_redirects=True)
    req = build_fc27_club_request("330323")
    req["allow_redirects"] = True
    with pytest.raises(FC27ProviderError, match="Redirects"):
        validate_request_against_policy(req)


def test_035_no_cookies() -> None:
    with pytest.raises(ValidationError):
        FC27PublicProviderPolicy(allow_cookies=True)
    with pytest.raises(ValueError, match="headers"):
        build_fc27_club_request("330323", headers={"Cookie": "sid=123"})


def test_036_no_authorization_header() -> None:
    with pytest.raises(ValidationError):
        FC27PublicProviderPolicy(allow_authorization_headers=True)
    with pytest.raises(ValueError, match="headers"):
        build_fc27_club_request("330323", headers={"Authorization": "Bearer secret"})


def test_037_no_arbitrary_headers() -> None:
    with pytest.raises(ValidationError):
        FC27PublicProviderPolicy(allow_custom_headers=True)
    req = build_fc27_club_request("330323")
    req["headers"] = {"X-Custom-Header": "val"}
    with pytest.raises(FC27ProviderError, match="Custom header"):
        validate_request_against_policy(req)


def test_038_no_proxy() -> None:
    with pytest.raises(ValidationError):
        FC27PublicProviderPolicy(allow_proxy=True)
    with pytest.raises(ValueError, match="Proxy"):
        build_fc27_club_request("330323", proxy="http://127.0.0.1:8080")


def test_039_no_browser_fingerprint() -> None:
    with pytest.raises(ValidationError):
        FC27PublicProviderPolicy(allow_browser_fingerprint=True)
    with pytest.raises(ValueError, match="Browser fingerprint"):
        build_fc27_club_request("330323", browser_profile="chrome141")
    req = build_fc27_club_request("330323")
    req["headers"] = {"sec-fetch-site": "same-origin"}
    with pytest.raises(FC27ProviderError, match="Browser-impersonation"):
        validate_request_against_policy(req)


def test_040_response_size_limit() -> None:
    policy = FC27PublicProviderPolicy()
    assert policy.maximum_response_bytes == 1048576
    with pytest.raises(ValidationError):
        FC27PublicProviderPolicy(maximum_response_bytes=2 * 1048576)


def test_041_timeout_limit() -> None:
    policy = FC27PublicProviderPolicy()
    assert policy.connect_timeout_seconds == 5.0
    assert policy.total_timeout_seconds == 10.0
    with pytest.raises(ValidationError):
        FC27PublicProviderPolicy(connect_timeout_seconds=6.0)
    with pytest.raises(ValidationError):
        FC27PublicProviderPolicy(total_timeout_seconds=12.0)


# ==============================================================================
# 42-55: Compatibility Adapter Tests
# ==============================================================================

def test_042_club_identity_mapping() -> None:
    adapter = FC27ClubsCompatibilityAdapter()
    raw = {
        "330323": {
            "clubId": 330323,
            "name": "East Coasters",
            "platform": "common-gen5",
            "regionId": 45123,
            "teamId": 112,
            "customKit": {"crestAssetId": "9901", "selectedKitType": "1"},
        }
    }
    mapped = adapter.map_club_identity(raw, requested_club_id="330323")
    assert isinstance(mapped, FC27ClubIdentityMappedRecord)
    assert mapped.club_id == "330323"
    assert mapped.club_name == "East Coasters"
    assert mapped.platform == "common-gen5"
    assert mapped.provider_region_value == "45123"
    assert mapped.crest_kit_identifiers["crest_asset_id"] == "9901"


def test_043_club_overview_mapping() -> None:
    adapter = FC27ClubsCompatibilityAdapter()
    raw = [{
        "clubId": "330323",
        "gamesPlayed": "42",
        "wins": "28",
        "ties": "6",
        "losses": "8",
        "goals": "95",
        "goalsAgainst": "41",
        "skillRating": "2150",
        "wstreak": "4",
        "unbeatenstreak": "7",
        "promotions": "5",
        "relegations": "0",
        "bestDivision": "1",
        "reputationtier": "3",
    }]
    mapped = adapter.map_club_overview(raw)
    assert isinstance(mapped, FC27ClubOverviewRecord)
    assert mapped.club_id == "330323"
    assert mapped.games_played == 42
    assert mapped.wins == 28
    assert mapped.skill_rating == 2150
    assert mapped.reputation_tier == 3


def test_044_match_summary_mapping() -> None:
    adapter = FC27ClubsCompatibilityAdapter()
    raw = {
        "matchId": "9001002",
        "timestamp": 1761566400,
        "clubs": {
            "330323": {
                "goals": "3",
                "goalsAgainst": "1",
                "wins": "1",
                "losses": "0",
                "ties": "0",
                "winnerByDnf": "0",
                "details": {"name": "East Coasters"},
            },
            "888777": {
                "goals": "1",
                "goalsAgainst": "3",
                "wins": "0",
                "losses": "1",
                "ties": "0",
                "winnerByDnf": "0",
                "details": {"name": "Harbor City FC"},
            },
        },
    }
    mapped = adapter.map_club_match_summary(raw, perspective_club_id="330323")
    assert isinstance(mapped, FC27ClubMatchSummaryRecord)
    assert mapped.match_id == "9001002"
    assert mapped.club_id == "330323"
    assert mapped.opponent_club_id == "888777"
    assert mapped.opponent_club_name == "Harbor City FC"
    assert mapped.goals_for == 3
    assert mapped.goals_against == 1
    assert mapped.result == "WIN"
    assert mapped.dnf is False


def test_045_member_summary_mapping() -> None:
    adapter = FC27ClubsCompatibilityAdapter()
    raw = {
        "name": "PlayerOneAlias",
        "proName": "Maximus",
        "favoritePosition": "CAM",
        "proOverall": "88",
        "gamesPlayed": "30",
        "winRate": "66.7",
        "goals": "12",
        "assists": "22",
        "ratingAve": "8.1",
        "manOfTheMatch": "9",
        "passesMade": "540",
        "passSuccessRate": "84.5",
        "tacklesMade": "45",
        "tackleSuccessRate": "75.0",
        "redCards": "0",
    }
    mapped = adapter.map_club_member_summary(raw)
    assert isinstance(mapped, FC27ClubMemberSummaryRecord)
    assert mapped.member_alias == "PlayerOneAlias"
    assert mapped.pro_overall == 88
    assert mapped.average_rating == 8.1


def test_046_member_career_mapping() -> None:
    adapter = FC27ClubsCompatibilityAdapter()
    raw = {
        "name": "PlayerOneAlias",
        "favoritePosition": "CAM",
        "gamesPlayed": "120",
        "goals": "54",
        "assists": "89",
        "ratingAve": "8.3",
        "manOfTheMatch": "35",
    }
    mapped = adapter.map_member_career(raw)
    assert isinstance(mapped, FC27MemberCareerStatsRecord)
    assert mapped.games_played == 120
    assert mapped.goals == 54


def test_047_player_match_mapping() -> None:
    adapter = FC27ClubsCompatibilityAdapter()
    raw = {
        "matchId": "9001002",
        "timestamp": "1761566400",
        "clubId": "330323",
        "playerId": "p_1001",
        "playername": "PlayerOneAlias",
        "pos": "cam",
        "rating": "8.4",
        "goals": "1",
        "assists": "2",
        "shots": "3",
        "passesmade": "28",
        "passattempts": "32",
        "tacklesmade": "3",
        "tackleattempts": "4",
        "secondsPlayed": "5400",
        "mom": "1",
        "match_event_aggregate_0": "215:28,216:4,217:2,214:1,11:2,0:3,1:1,6:2",
    }
    mapped = adapter.map_player_match(raw)
    assert isinstance(mapped, FC27PlayerMatchStatsRecord)
    assert mapped.player_id == "p_1001"
    assert mapped.position == "CAM"
    assert mapped.minutes_played == 90
    assert mapped.man_of_the_match is True
    assert mapped.decoded_events["passes_completed"] == 28


def test_048_timestamp_normalization() -> None:
    adapter = FC27ClubsCompatibilityAdapter()
    mapped = adapter.map_club_identity(
        {"clubId": "330323", "name": "East Coasters", "timestamp": 1761566400}
    )
    assert mapped.timestamp == "2025-10-27T12:00:00+00:00"


def test_049_numeric_string_conversion() -> None:
    adapter = FC27ClubsCompatibilityAdapter()
    ov = adapter.map_club_overview({"clubId": "330323", "gamesPlayed": "15", "wins": "10"})
    assert ov.games_played == 15
    assert ov.wins == 10


def test_050_missing_optional_fields() -> None:
    adapter = FC27ClubsCompatibilityAdapter()
    mapped = adapter.map_club_identity({"clubId": "330323", "name": "East Coasters"})
    assert mapped.provider_region_value is None
    assert mapped.crest_kit_identifiers == {}


def test_051_unknown_fields() -> None:
    adapter = FC27ClubsCompatibilityAdapter()
    mapped = adapter.map_club_identity(
        {"clubId": "330323", "name": "East Coasters", "futureEaField1": "x", "futureEaField2": 2}
    )
    assert mapped.unknown_fields == ("futureEaField1", "futureEaField2")


def test_052_invalid_upstream_shape() -> None:
    adapter = FC27ClubsCompatibilityAdapter()
    with pytest.raises(FC27ProviderError):
        adapter.map_club_identity({"unexpected": "no_club_fields"})


def test_053_upstream_error_translation() -> None:
    adapter = FC27ClubsCompatibilityAdapter()
    err_403 = adapter.translate_error(RuntimeError("Akamai block"), status_code=403)
    assert err_403["category"] == "AUTHENTICATION_OR_BLOCK"
    assert err_403["retryable"] is False

    err_404 = adapter.translate_error(RuntimeError("ClubNotFoundError"), status_code=404)
    assert err_404["category"] == "CLUB_NOT_FOUND"


def test_054_provenance_attached() -> None:
    adapter = FC27ClubsCompatibilityAdapter()
    mapped = adapter.map_club_identity({"clubId": "330323", "name": "East Coasters"})
    assert mapped.provenance["upstream_commit"] == AUDITED_UPSTREAM_COMMIT
    assert mapped.provenance["upstream_version"] == AUDITED_UPSTREAM_VERSION
    assert mapped.provenance["provider"] == PROVIDER_NAME


def test_055_internal_model_isolation() -> None:
    analytics_src = (REPO_ROOT / "src" / "fc27_analytics.py").read_text(encoding="utf-8")
    coaching_src = (REPO_ROOT / "src" / "fc27_coaching.py").read_text(encoding="utf-8")
    assert "fc_clubs_api" not in analytics_src
    assert "fc27_clubs" not in analytics_src
    assert "fc_clubs_api" not in coaching_src


# ==============================================================================
# 56-62: Sanitizer & Response Envelope Tests
# ==============================================================================

def test_056_identity_allowlist() -> None:
    raw = {
        "330323": {
            "clubId": 330323,
            "name": "East Coasters",
            "platform": "common-gen5",
            "regionId": 99,
            "teamId": 10,
            "customKit": {"crestAssetId": "500", "selectedKitType": "1", "stadName": "DroppedStadium"},
            "unlistedField": "dropped_val",
        }
    }
    sanitized, spaths, dpaths = sanitize_identity_payload(raw, requested_club_id="330323", payload_sha256="a" * 64)
    assert spaths == []
    assert "330323.unlistedField" in dpaths
    assert "330323.customKit.stadName" in dpaths
    assert set(sanitized.keys()) == {
        "club_id",
        "club_name",
        "platform",
        "provider_region_value",
        "crest_kit_identifiers",
        "provider",
        "game_version",
        "capture_timestamp",
        "payload_hash",
        "provenance",
    }


def test_057_roster_dropped() -> None:
    raw = {
        "330323": {
            "clubId": 330323,
            "name": "East Coasters",
            "members": [{"name": "HiddenMember"}],
            "roster": ["p1", "p2"],
        }
    }
    sanitized, spaths = sanitize_identity(raw)
    assert "members" not in str(sanitized)
    assert "HiddenMember" not in str(sanitized)
    assert "330323.members" in spaths
    assert "330323.roster" in spaths


def test_058_gamer_tags_dropped() -> None:
    raw = {
        "330323": {
            "clubId": 330323,
            "name": "East Coasters",
            "gamertag": "SecretGamerTag99",
            "proName": "SecretPro",
        }
    }
    sanitized, spaths = sanitize_identity(raw)
    assert "SecretGamerTag99" not in str(sanitized)
    assert "330323.gamertag" in spaths
    assert "330323.proName" in spaths


def test_059_persona_ids_dropped() -> None:
    raw = {"330323": {"clubId": 330323, "name": "East Coasters", "personaId": "99887766"}}
    sanitized, spaths = sanitize_identity(raw)
    assert "99887766" not in str(sanitized)
    assert "330323.personaId" in spaths


def test_060_account_ids_dropped() -> None:
    raw = {
        "330323": {
            "clubId": 330323,
            "name": "East Coasters",
            "accountId": "ea_acc_111",
            "nucleusId": "nuc_222",
            "userId": "usr_333",
        }
    }
    sanitized, spaths = sanitize_identity(raw)
    assert "ea_acc_111" not in str(sanitized)
    assert "nuc_222" not in str(sanitized)
    assert "usr_333" not in str(sanitized)
    assert set(spaths) == {"330323.accountId", "330323.nucleusId", "330323.userId"}


def test_061_sensitive_paths_only() -> None:
    secret_value = "SUPER_SECRET_SESSION_TOKEN_VALUE_999"
    raw = {
        "330323": {
            "clubId": 330323,
            "name": "East Coasters",
            "sessionToken": secret_value,
        }
    }
    sanitized, spaths = sanitize_identity(raw)
    assert spaths == ["330323.sessionToken"]
    assert secret_value not in str(spaths)
    assert secret_value not in str(sanitized)


def test_062_raw_body_absent() -> None:
    adapter = FC27ClubsCompatibilityAdapter()
    env = adapter.create_response_envelope(
        operation="CLUB_IDENTITY",
        requested_club_id="330323",
        requested_platform="common-gen5",
        requested_timestamp="2026-10-27T12:00:00+00:00",
        received_timestamp="2026-10-27T12:00:01+00:00",
        http_status=200,
        content_type="application/json",
        response_size=128,
        payload_sha256="b" * 64,
    )
    assert isinstance(env, FC27ProviderResponseEnvelope)
    assert env.raw_response_retained is False
    dumped = env.model_dump()
    assert "body" not in dumped
    assert "raw_body" not in dumped
    with pytest.raises(ValidationError):
        FC27ProviderResponseEnvelope(
            requested_timestamp="2026-10-27T12:00:00+00:00",
            received_timestamp="2026-10-27T12:00:01+00:00",
            http_status=200,
            content_type="application/json",
            response_size=10,
            payload_sha256="b" * 64,
            raw_response_retained=True,  # type: ignore[arg-type]
        )


# ==============================================================================
# 63-70: Identity Matching Tests
# ==============================================================================

def test_063_exact_east_coasters_match() -> None:
    p = EAST_COASTERS_SEED_PROFILE
    assert match_identity(
        p.canonical_club_name,
        p.known_aliases[0],
        p.club_abbreviation,
        p.provisional_club_id,
        p.provisional_platform,
        {"club_id": "330323", "club_name": "East Coasters", "platform": "common-gen5"},
    ) == FC27IdentityOutcome.EXACT_MATCH
    # Case-insensitive & alias match
    assert match_identity(
        p.canonical_club_name,
        p.known_aliases[0],
        p.club_abbreviation,
        p.provisional_club_id,
        p.provisional_platform,
        {"club_id": "330323", "club_name": "eastcoasters fc", "platform": "common-gen5"},
    ) == FC27IdentityOutcome.EXACT_MATCH


def test_064_name_mismatch() -> None:
    p = EAST_COASTERS_SEED_PROFILE
    assert match_identity(
        p.canonical_club_name,
        p.known_aliases[0],
        p.club_abbreviation,
        p.provisional_club_id,
        p.provisional_platform,
        {"club_id": "330323", "club_name": "West Coasters", "platform": "common-gen5"},
    ) == FC27IdentityOutcome.NAME_MISMATCH


def test_065_id_mismatch() -> None:
    p = EAST_COASTERS_SEED_PROFILE
    assert match_identity(
        p.canonical_club_name,
        p.known_aliases[0],
        p.club_abbreviation,
        p.provisional_club_id,
        p.provisional_platform,
        {"club_id": "999999", "club_name": "East Coasters", "platform": "common-gen5"},
    ) == FC27IdentityOutcome.ID_MISMATCH


def test_066_platform_mismatch() -> None:
    p = EAST_COASTERS_SEED_PROFILE
    assert match_identity(
        p.canonical_club_name,
        p.known_aliases[0],
        p.club_abbreviation,
        p.provisional_club_id,
        p.provisional_platform,
        {"club_id": "330323", "club_name": "East Coasters", "platform": "nx"},
    ) == FC27IdentityOutcome.PLATFORM_MISMATCH


def test_067_ecf_alone_insufficient() -> None:
    p = EAST_COASTERS_SEED_PROFILE
    outcome = match_identity(
        p.canonical_club_name,
        p.known_aliases[0],
        p.club_abbreviation,
        p.provisional_club_id,
        p.provisional_platform,
        {"club_id": "330323", "club_name": "ECF", "platform": "common-gen5"},
    )
    assert outcome == FC27IdentityOutcome.REVIEW_REQUIRED


def test_068_region_remains_provisional() -> None:
    outcome, statuses = compare_club_identity(
        requested_club_id="330323",
        requested_platform="common-gen5",
        expected_name="East Coasters",
        returned={
            "club_id": "330323",
            "club_name": "East Coasters",
            "platform": "common-gen5",
            "provider_region_value": "50122",
        },
    )
    assert outcome == FC27IdentityOutcome.EXACT_MATCH
    assert statuses["matchmaking_region_status"] == "USER_ASSERTED"
    assert statuses["club_id_status"] == "PROVISIONAL"


def test_069_lookup_does_not_verify_membership() -> None:
    _, statuses = compare_club_identity(
        requested_club_id="330323",
        requested_platform="common-gen5",
        expected_name="East Coasters",
        returned={"club_id": "330323", "club_name": "East Coasters", "platform": "common-gen5"},
    )
    assert statuses["user_membership_status"] == "USER_ASSERTED"


def test_070_lookup_does_not_verify_authority() -> None:
    _, statuses = compare_club_identity(
        requested_club_id="330323",
        requested_platform="common-gen5",
        expected_name="East Coasters",
        returned={"club_id": "330323", "club_name": "East Coasters", "platform": "common-gen5"},
    )
    assert statuses["manager_authority_status"] == "UNVERIFIED"


# ==============================================================================
# 71-76: Multi-Club Behavior Tests
# ==============================================================================

def test_071_another_club_id() -> None:
    actor = FC27ApplicationClubAccess(actor_id="viewer-1", role=FC27AccessRole.VIEWER)
    q = FC27ClubQuery(query_id="q-other-1", club_id="774411", expected_name="North Star United")

    def mock_transport(_q: FC27ClubQuery) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        return (
            {"774411": {"clubId": 774411, "name": "North Star United", "platform": "common-gen5", "regionId": 9}},
            {"status_code": 200, "content_type": "application/json"},
        )

    res = lookup_fc27_club(
        actor,
        FC27AccessAction.VIEW_PUBLIC_CLUB_IDENTITY,
        q,
        FC27ClubsCompatibilityAdapter(),
        FC27PublicProviderPolicy(),
        transport=mock_transport,
    )
    assert res.requested_club_id == "774411"
    assert res.returned_club_id == "774411"
    assert res.returned_club_name == "North Star United"
    assert res.identity_outcome == FC27IdentityOutcome.EXACT_MATCH.value


def test_072_another_expected_name() -> None:
    actor = FC27ApplicationClubAccess(actor_id="viewer-1", role=FC27AccessRole.VIEWER)
    q = FC27ClubQuery(query_id="q-other-2", club_id="774411", expected_name="Wrong Name FC")

    def mock_transport(_q: FC27ClubQuery) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        return (
            {"774411": {"clubId": 774411, "name": "North Star United", "platform": "common-gen5"}},
            {"status_code": 200, "content_type": "application/json"},
        )

    res = lookup_fc27_club(
        actor,
        FC27AccessAction.VIEW_PUBLIC_CLUB_IDENTITY,
        q,
        FC27ClubsCompatibilityAdapter(),
        FC27PublicProviderPolicy(),
        transport=mock_transport,
    )
    assert res.identity_outcome == FC27IdentityOutcome.NAME_MISMATCH.value


def test_073_no_east_coasters_hardcoding() -> None:
    actor = FC27ApplicationClubAccess(actor_id="viewer-1", role=FC27AccessRole.VIEWER)
    # Lookup without expected_name on an arbitrary club returns REVIEW_REQUIRED, not a mismatch against East Coasters
    q = FC27ClubQuery(query_id="q-no-hc", club_id="555111")

    def mock_transport(_q: FC27ClubQuery) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        return (
            {"555111": {"clubId": 555111, "name": "Atlantic Rovers", "platform": "common-gen5"}},
            {"status_code": 200, "content_type": "application/json"},
        )

    res = lookup_fc27_club(
        actor,
        FC27AccessAction.VIEW_PUBLIC_CLUB_IDENTITY,
        q,
        FC27ClubsCompatibilityAdapter(),
        FC27PublicProviderPolicy(),
        transport=mock_transport,
    )
    assert res.returned_club_id == "555111"
    assert res.returned_club_name == "Atlantic Rovers"
    assert res.identity_outcome == FC27IdentityOutcome.REVIEW_REQUIRED.value


def test_074_cache_isolated_by_club() -> None:
    cache = FC27Cache()
    actor = FC27ApplicationClubAccess(actor_id="viewer-1", role=FC27AccessRole.VIEWER)
    adapter = FC27ClubsCompatibilityAdapter()
    policy = FC27PublicProviderPolicy()

    def transport(q: FC27ClubQuery) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        cname = "East Coasters" if q.club_id == "330323" else "Pacific United"
        return (
            {str(q.club_id): {"clubId": q.club_id, "name": cname, "platform": q.platform}},
            {"status_code": 200, "content_type": "application/json"},
        )

    r1 = lookup_fc27_club(
        actor,
        FC27AccessAction.VIEW_PUBLIC_CLUB_IDENTITY,
        FC27ClubQuery(query_id="q1", club_id="330323", expected_name="East Coasters"),
        adapter,
        policy,
        transport=transport,
        cache=cache,
    )
    r2 = lookup_fc27_club(
        actor,
        FC27AccessAction.VIEW_PUBLIC_CLUB_IDENTITY,
        FC27ClubQuery(query_id="q2", club_id="888999", expected_name="Pacific United"),
        adapter,
        policy,
        transport=transport,
        cache=cache,
    )
    assert r1.cache_hit is False
    assert r2.cache_hit is False
    assert r1.returned_club_name == "East Coasters"
    assert r2.returned_club_name == "Pacific United"


def test_075_cache_isolated_by_platform() -> None:
    cache = FC27Cache()
    k_gen5 = cache.key(PROVIDER_NAME, "CLUB_IDENTITY", "FC27", "common-gen5", "330323")
    k_nx = cache.key(PROVIDER_NAME, "CLUB_IDENTITY", "FC27", "nx", "330323")
    cache.set(k_gen5, {"club_id": "330323", "club_name": "East Coasters", "platform": "common-gen5"})
    assert cache.get(k_gen5)[1] is True
    assert cache.get(k_nx)[1] is False


def test_076_duplicate_club_names_require_id() -> None:
    candidates = [
        {"clubId": "330323", "clubName": "East Coasters", "currentDivision": "1"},
        {"clubId": "991122", "clubName": "East Coasters", "currentDivision": "3"},
    ]
    res = search_fc27_clubs_mock("East Coasters", candidates)
    assert len(res.candidates) == 2
    assert res.duplicate_names_detected is True
    assert res.selected_club_id is None
    assert res.club_id_confirmation_required is True
    assert res.live_search_enabled is False
    with pytest.raises(FC27LiveAccessBlockedError):
        search_fc27_clubs_mock("East Coasters", candidates, live_requested=True)


# ==============================================================================
# 77-87: Cache and Circuit Breaker Tests
# ==============================================================================

def test_077_sanitized_cache_hit() -> None:
    cache = FC27Cache()
    actor = FC27ApplicationClubAccess(actor_id="viewer-1", role=FC27AccessRole.VIEWER)
    adapter = FC27ClubsCompatibilityAdapter()
    policy = FC27PublicProviderPolicy()
    calls = {"count": 0}

    def transport(_q: FC27ClubQuery) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        calls["count"] += 1
        return (
            {"330323": {"clubId": 330323, "name": "East Coasters", "platform": "common-gen5"}},
            {"status_code": 200, "content_type": "application/json"},
        )

    q = FC27ClubQuery(query_id="q-cache", club_id="330323", expected_name="East Coasters")
    first = lookup_fc27_club(actor, FC27AccessAction.VIEW_PUBLIC_CLUB_IDENTITY, q, adapter, policy, transport=transport, cache=cache)
    second = lookup_fc27_club(actor, FC27AccessAction.VIEW_PUBLIC_CLUB_IDENTITY, q, adapter, policy, transport=transport, cache=cache)
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert calls["count"] == 1
    assert second.returned_club_name == "East Coasters"


def test_078_ttl_expiration() -> None:
    now = [1000.0]
    cache = FC27Cache(ttl=300.0, clock=lambda: now[0])
    key = cache.key(PROVIDER_NAME, "CLUB_IDENTITY", "FC27", "common-gen5", "330323")
    cache.set(key, {"club_id": "330323", "club_name": "East Coasters"})
    assert cache.get(key)[1] is True
    now[0] += 301.0
    assert cache.get(key)[1] is False
    # TTL > 600s rejected
    with pytest.raises(ValueError, match="TTL"):
        FC27Cache(ttl=601.0)


def test_079_bounded_eviction() -> None:
    cache = FC27Cache(max_size=2, ttl=600.0)
    k1 = cache.key(PROVIDER_NAME, "CLUB_IDENTITY", "FC27", "common-gen5", "1")
    k2 = cache.key(PROVIDER_NAME, "CLUB_IDENTITY", "FC27", "common-gen5", "2")
    k3 = cache.key(PROVIDER_NAME, "CLUB_IDENTITY", "FC27", "common-gen5", "3")
    cache.set(k1, {"club_id": "1", "club_name": "One"})
    cache.set(k2, {"club_id": "2", "club_name": "Two"})
    cache.set(k3, {"club_id": "3", "club_name": "Three"})
    assert cache.get(k1)[1] is False
    assert cache.get(k2)[1] is True
    assert cache.get(k3)[1] is True


def test_080_no_raw_response_cached() -> None:
    cache = FC27Cache()
    k = cache.key(PROVIDER_NAME, "CLUB_IDENTITY", "FC27", "common-gen5", "330323")
    with pytest.raises(ValueError, match="Raw provider responses"):
        cache.set(k, {"club_id": "330323", "raw_body": "{}"})
    with pytest.raises(ValueError, match="unsanitized"):
        cache.set(k, {"club_id": "330323", "personaId": "12345"})


def test_081_http_401_opens_circuit() -> None:
    breaker = FC27CircuitBreaker()
    breaker.record(status_code=401)
    assert breaker.is_open() is True
    assert breaker.open_reason == "HTTP_401"


def test_082_http_403_opens_circuit() -> None:
    breaker = FC27CircuitBreaker()
    breaker.record(status_code=403)
    assert breaker.is_open() is True
    assert breaker.open_reason == "HTTP_403"


def test_083_captcha_opens_circuit() -> None:
    breaker = FC27CircuitBreaker()
    breaker.record(status_code=200, body="<html>Please complete CAPTCHA challenge</html>", content_type="text/html")
    assert breaker.is_open() is True
    assert breaker.open_reason == "CAPTCHA_DETECTED"


def test_084_auth_redirect_opens_circuit() -> None:
    breaker = FC27CircuitBreaker()
    breaker.record(status_code=302, redirect_count=1, location_header="https://signin.ea.com/p/juno/login")
    assert breaker.is_open() is True
    assert breaker.open_reason == "AUTHENTICATION_OR_HTTP_REDIRECT"


def test_085_open_circuit_blocks_calls() -> None:
    breaker = FC27CircuitBreaker()
    breaker.open("HTTP_403")
    actor = FC27ApplicationClubAccess(actor_id="viewer-1", role=FC27AccessRole.VIEWER)
    q = FC27ClubQuery(query_id="q-open", club_id="330323")
    called = {"count": 0}

    def transport(_q: FC27ClubQuery) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        called["count"] += 1
        return {}, {}

    res = lookup_fc27_club(
        actor,
        FC27AccessAction.VIEW_PUBLIC_CLUB_IDENTITY,
        q,
        FC27ClubsCompatibilityAdapter(),
        FC27PublicProviderPolicy(),
        transport=transport,
        breaker=breaker,
    )
    assert called["count"] == 0
    assert res.circuit_breaker_state == "OPEN"
    assert res.availability_status == "PROVIDER_UNAVAILABLE"


def test_086_manual_reset_required() -> None:
    breaker = FC27CircuitBreaker()
    breaker.open("HTTP_403")
    with pytest.raises(FC27ProviderError, match="Manual review confirmation"):
        breaker.reset(manual=False)
    assert breaker.is_open() is True
    breaker.reset(manual=True, reviewer_reference="human-operator-review-1")
    assert breaker.is_open() is False


def test_087_offline_fallback_returned() -> None:
    breaker = FC27CircuitBreaker()
    actor = FC27ApplicationClubAccess(actor_id="viewer-1", role=FC27AccessRole.VIEWER)
    q = FC27ClubQuery(query_id="q-403", club_id="330323")

    def transport_403(_q: FC27ClubQuery) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        return {}, {"status_code": 403, "content_type": "text/html", "body_text": "Access Denied Akamai"}

    res = lookup_fc27_club(
        actor,
        FC27AccessAction.VIEW_PUBLIC_CLUB_IDENTITY,
        q,
        FC27ClubsCompatibilityAdapter(),
        FC27PublicProviderPolicy(),
        transport=transport_403,
        breaker=breaker,
    )
    assert res.circuit_breaker_state == "OPEN"
    assert res.availability_status == "PROVIDER_UNAVAILABLE"
    assert res.fallback_path == "USER_SUPPLIED_EXPORT"
    assert "FALLBACK_USER_SUPPLIED_EXPORT" in res.reason_codes


# ==============================================================================
# 88-93: Event and Formula Registry Reconciliation Tests
# ==============================================================================

def test_088_upstream_mappings_imported_as_provisional() -> None:
    assert len(RECONCILED_EVENT_REGISTRY) >= 70
    for eid, mapping in RECONCILED_EVENT_REGISTRY.items():
        assert mapping.status == EventStatus.PROVISIONAL
        assert mapping.status != EventStatus.VALIDATED
        assert mapping.source_commit == AUDITED_UPSTREAM_COMMIT
        assert mapping.game_version == "FC27"


def test_089_unknown_events_preserved() -> None:
    res = parse_and_evaluate_match_events(
        {"match_event_aggregate_0": "215:20,9999:7"}
    )
    assert res["decoded_metrics"]["passes_completed"] == 20
    assert res["unknown_event_counts"] == {9999: 7}


def test_090_conflicting_mapping_marked_conflicted() -> None:
    reconciled = reconcile_event_registry(
        upstream_events=[(215, "contradictory_event_name", "FC27")],
        internal_registry=FC27_EVENT_REGISTRY,
    )
    assert reconciled[215].status == EventStatus.CONFLICTED
    assert reconciled[215].conflict_status == "CONFLICTED"
    assert reconciled[215].confidence == 0.0
    # Internal registry was NOT overwritten
    assert FC27_EVENT_REGISTRY[215].semantic_name == "passes_completed"


def test_091_fc26_not_promoted() -> None:
    reconciled = reconcile_event_registry(
        upstream_events=[(501, "legacy_fc26_stat", "FC26")],
        internal_registry=FC27_EVENT_REGISTRY,
    )
    assert reconciled[501].status == EventStatus.DEPRECATED
    assert reconciled[501].conflict_status == "CONFLICTED"
    assert reconciled[501].confidence == 0.0


def test_092_formula_version_retained() -> None:
    for fid, fdef in FC27_FORMULA_REGISTRY.items():
        assert fdef.formula_version == "fc27-v1.0"
        assert fdef.game_version == "FC27"
        assert fdef.status == "PROVISIONAL"
    eval_res = parse_and_evaluate_match_events({"match_event_aggregate_0": "215:10"})
    assert eval_res["formula_version"] == "fc27-v1.0"


def test_093_confidence_reduced_for_required_unknown() -> None:
    clean_res = parse_and_evaluate_match_events({"match_event_aggregate_0": "215:15"})
    degraded_res = parse_and_evaluate_match_events(
        {"match_event_aggregate_0": "215:15,8888:4"},
        required_event_ids=(215, 8888),
    )
    assert degraded_res["analysis_confidence"] < clean_res["analysis_confidence"]


# ==============================================================================
# 94-103: Feature Isolation Tests
# ==============================================================================

def test_094_public_identity_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FC27_PUBLIC_IDENTITY_LOOKUP_ENABLED", raising=False)
    assert is_fc27_public_identity_lookup_enabled() is False


def test_095_compatibility_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FC27_UPSTREAM_COMPATIBILITY_ENABLED", raising=False)
    assert is_fc27_upstream_compatibility_enabled() is False


def test_096_malformed_flags_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FC27_PUBLIC_IDENTITY_LOOKUP_ENABLED", "maybe-enabled")
    monkeypatch.setenv("FC27_UPSTREAM_COMPATIBILITY_ENABLED", "invalid_bool")
    assert is_fc27_public_identity_lookup_enabled() is False
    assert is_fc27_upstream_compatibility_enabled() is False
    assert flag_enabled("FC27_PUBLIC_IDENTITY_LOOKUP_ENABLED") is False


def test_097_public_lookup_does_not_enable_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FC27_PUBLIC_IDENTITY_LOOKUP_ENABLED", "true")
    state = evaluate_fc27_feature_flags()
    assert state["FC27_PUBLIC_IDENTITY_LOOKUP_ENABLED"] is True
    assert state["MATCHES_ENABLED_BY_PUBLIC_IDENTITY"] is False
    assert state["FC27_LIVE_INGESTION_ENABLED"] is False


def test_098_public_lookup_does_not_enable_members(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FC27_PUBLIC_IDENTITY_LOOKUP_ENABLED", "true")
    state = evaluate_fc27_feature_flags()
    assert state["MEMBERS_ENABLED_BY_PUBLIC_IDENTITY"] is False


def test_099_public_lookup_does_not_enable_persistence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FC27_PUBLIC_IDENTITY_LOOKUP_ENABLED", "true")
    state = evaluate_fc27_feature_flags()
    assert state["PERSISTENCE_ENABLED_BY_PUBLIC_IDENTITY"] is False


def test_100_public_lookup_does_not_enable_recommendations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FC27_PUBLIC_IDENTITY_LOOKUP_ENABLED", "true")
    state = evaluate_fc27_feature_flags()
    assert state["FC27_RECOMMENDATIONS_ENABLED"] is False
    assert state["RECOMMENDATIONS_ENABLED_BY_PUBLIC_IDENTITY"] is False


def test_101_public_lookup_does_not_enable_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FC27_PUBLIC_IDENTITY_LOOKUP_ENABLED", "true")
    state = evaluate_fc27_feature_flags()
    assert state["DISPATCH_ENABLED"] is False
    assert state["DISPATCH_WORKER_ENABLED"] is False
    assert state["DISPATCH_ENABLED_BY_PUBLIC_IDENTITY"] is False


def test_102_compatibility_does_not_enable_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FC27_UPSTREAM_COMPATIBILITY_ENABLED", "true")
    state = evaluate_fc27_feature_flags()
    assert state["FC27_UPSTREAM_COMPATIBILITY_ENABLED"] is True
    assert state["NETWORK_ENABLED_BY_COMPATIBILITY"] is False
    assert state["FC27_PROVIDER_ENABLED"] is False
    assert state["MANAGER_AUTHORITY_GRANTED_BY_FLAGS"] is False


def test_103_matchpulse_boundary_intact() -> None:
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_matchpulse_boundary.py")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "boundary drift check: OK" in proc.stdout


# ==============================================================================
# 104-112: CLI Tests
# ==============================================================================

def test_104_cli_dry_run_default(capsys: pytest.CaptureFixture[str]) -> None:
    rc = run_cli(["--club-id", "330323"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "LOOKUP_MODE=DRY_RUN" in out


def test_105_cli_east_coasters_dry_run(capsys: pytest.CaptureFixture[str]) -> None:
    rc = run_cli([
        "--club-id", "330323",
        "--platform", "common-gen5",
        "--expected-name", "East Coasters",
        "--dry-run",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    for expected_line in (
        "LOOKUP_MODE=DRY_RUN",
        "PROVIDER=EA_FC_CLUBS_WEB",
        "CLUB_ID=330323",
        "PLATFORM=common-gen5",
        "EXPECTED_NAME=East Coasters",
        "REQUESTS_EXECUTED=0",
        "PERSISTENCE_USED=NO",
        "RECOMMENDATIONS_GENERATED=0",
        "DISPATCH_EXECUTED=NO",
        "MANAGER_AUTHORITY_STATUS=UNVERIFIED",
    ):
        assert expected_line in out


def test_106_cli_another_club_dry_run(capsys: pytest.CaptureFixture[str]) -> None:
    rc = run_cli([
        "--club-id", "901250",
        "--platform", "nx",
        "--expected-name", "Synthetic MultiClub FC",
        "--dry-run",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "CLUB_ID=901250" in out
    assert "PLATFORM=nx" in out
    assert "EXPECTED_NAME=Synthetic MultiClub FC" in out
    assert "REQUESTS_EXECUTED=0" in out


def test_107_cli_no_arbitrary_host_option() -> None:
    parser = build_arg_parser()
    option_strings = {opt for action in parser._actions for opt in action.option_strings}
    assert "--host" not in option_strings
    assert "--proxy" not in option_strings
    assert "--browser-profile" not in option_strings
    assert "--retries" not in option_strings
    assert "--retry-count" not in option_strings
    rc = run_cli(["--club-id", "330323", "--host", "evil.com"])
    assert rc != 0


def test_108_cli_no_url_option() -> None:
    parser = build_arg_parser()
    option_strings = {opt for action in parser._actions for opt in action.option_strings}
    assert "--url" not in option_strings
    assert "--endpoint" not in option_strings
    rc = run_cli(["--club-id", "330323", "--url", "https://evil.com"])
    assert rc != 0


def test_109_cli_no_credential_options() -> None:
    parser = build_arg_parser()
    option_strings = {opt for action in parser._actions for opt in action.option_strings}
    for forbidden in ("--cookie", "--session", "--token", "--header", "--auth", "--password", "--live"):
        assert forbidden not in option_strings
    rc = run_cli(["--club-id", "330323", "--token", "secret"])
    assert rc != 0


def test_110_cli_metadata_only_output(capsys: pytest.CaptureFixture[str]) -> None:
    rc = run_cli(["--club-id", "330323", "--expected-name", "East Coasters", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    for line in out.strip().splitlines():
        assert "=" in line
        assert "{" not in line
        assert "members" not in line.lower()


def test_111_cli_portable_execution() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "fc27_club_lookup.py"),
            "--club-id",
            "330323",
            "--platform",
            "common-gen5",
            "--expected-name",
            "East Coasters",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "LOOKUP_MODE=DRY_RUN" in proc.stdout
    assert "CLUB_ID=330323" in proc.stdout


def test_112_cli_zero_network_in_dry_run(capsys: pytest.CaptureFixture[str]) -> None:
    # Autouse fixture blocks socket.create_connection and socket.getaddrinfo
    rc = run_cli(["--club-id", "330323", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "REQUESTS_EXECUTED=0" in out


# ==============================================================================
# 113-118: Independent Review Hardening & Regression Tests
# ==============================================================================

def test_113_capability_truthfulness_four_tiers() -> None:
    caps = FC27ProviderCapabilities()
    assert caps.declared_operations == ALL_OPERATIONS
    assert caps.supported_operations == ALL_OPERATIONS
    assert caps.mock_enabled_operations == ALL_OPERATIONS
    assert caps.design_eligible_operations == ("CLUB_IDENTITY",)
    assert caps.live_executable_operations == ()
    assert caps.live_enabled_operations == ()


def test_114_sanitizer_normalized_key_variants() -> None:
    payload = {
        "clubId": "330323",
        "name": "East Coasters",
        "platform": "common-gen5",
        "ea-account-id": "secret_acct_99",
        "x_origin_id": "origin_77",
        "client-secret": "cs_123",
        "api_key": "ak_456",
        "bearerToken": "bt_789",
        "nested": {
            "ip-address": "198.51.100.7",
            "device.id": "dev_888",
            "gamer-tag": "HiddenPro99",
        },
    }
    sanitized, spaths, _ = sanitize_identity_payload(payload, requested_club_id="330323")
    assert len(spaths) == 8
    assert "ea-account-id" in spaths
    assert "x_origin_id" in spaths
    assert "client-secret" in spaths
    assert "api_key" in spaths
    assert "bearerToken" in spaths
    assert "nested.ip-address" in spaths
    assert "nested.device.id" in spaths
    assert "nested.gamer-tag" in spaths
    dumped = repr(sanitized)
    for secret_val in ("secret_acct_99", "origin_77", "cs_123", "ak_456", "bt_789", "198.51.100.7", "dev_888", "HiddenPro99"):
        assert secret_val not in dumped


def test_115_cache_and_breaker_thread_safety() -> None:
    cache = FC27Cache(max_size=32, ttl=300.0)
    breaker = FC27CircuitBreaker(malformed_threshold=50)
    errors: list[Exception] = []

    def _worker(tid: int) -> None:
        try:
            for idx in range(25):
                cid = str(330000 + (tid * 100) + idx)
                k = cache.key("EA_FC_CLUBS_WEB", "CLUB_IDENTITY", "FC27", "common-gen5", cid)
                cache.set(k, {"club_id": cid, "club_name": f"Club {cid}", "platform": "common-gen5"})
                rec, hit = cache.get(k)
                if hit:
                    assert rec is not None and rec["club_id"] == cid
                breaker.record(status_code=200, content_type="application/json")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert breaker.is_open() is False


def test_116_event_registry_exact_74_count_and_no_duplicates() -> None:
    assert len(UPSTREAM_SINGLE_EVENT_DEFS) == 74
    assert len(RECONCILED_EVENT_REGISTRY) == 74
    with pytest.raises(ValueError, match="Duplicate upstream event ID"):
        reconcile_event_registry(
            upstream_events=[
                (215, "passes_completed", "FC27"),
                (215, "duplicate_event_entry", "FC27"),
            ]
        )


# ==============================================================================
# 117-126: Offline Club Evidence Workflow & Attestation Template Tests
# ==============================================================================

def test_117_offline_observation_urls_and_sha256_provenance() -> None:
    assert len(EAST_COASTERS_SECONDARY_OBSERVATION_URLS) == 3
    obs = build_default_east_coasters_offline_observations()
    assert len(obs) == 3
    for item, expected_url in zip(obs, EAST_COASTERS_SECONDARY_OBSERVATION_URLS):
        assert item.source_url == expected_url
        assert item.url_sha256 == compute_url_sha256(expected_url)
        assert item.source_classification == "UNTRUSTED_SECONDARY_OBSERVATION"
        assert item.page_fetched is False
        assert item.scraped is False
        assert item.browser_call_replayed is False
        assert item.raw_html_stored is False
        assert item.player_data_included is False
        assert item.authority_inferred is False


def test_118_offline_observation_rejects_html_and_player_data() -> None:
    u = EAST_COASTERS_SECONDARY_OBSERVATION_URLS[0]
    h = compute_url_sha256(u)
    with pytest.raises(ValidationError, match="url_sha256 mismatch"):
        FC27OfflinePublicObservation(
            observation_id="bad-hash",
            source_url=u,
            url_sha256="0" * 64,
            source_operator="proclubstracker.com",
            observed_at="2026-10-27T12:00:00Z",
            page_title="East Coasters",
            observed_fields={"club_id": "330323"},
        )
    with pytest.raises(ValidationError, match="Raw HTML"):
        FC27OfflinePublicObservation(
            observation_id="bad-html",
            source_url=u,
            url_sha256=h,
            source_operator="proclubstracker.com",
            observed_at="2026-10-27T12:00:00Z",
            page_title="<html><body>East Coasters</body></html>",
            observed_fields={"club_id": "330323"},
        )
    with pytest.raises(ValidationError, match="Prohibited or player-linked field"):
        FC27OfflinePublicObservation(
            observation_id="bad-player",
            source_url=u,
            url_sha256=h,
            source_operator="proclubstracker.com",
            observed_at="2026-10-27T12:00:00Z",
            page_title="East Coasters",
            observed_fields={"club_id": "330323", "gamer_tag": "Player99"},
        )


def test_119_offline_evidence_corroboration_for_east_coasters() -> None:
    obs = build_default_east_coasters_offline_observations()
    manifest = evaluate_offline_club_evidence(obs)
    assert isinstance(manifest, FC27OfflineEvidenceManifest)
    assert manifest.club_name_status == "CORROBORATED"
    assert manifest.club_id_status == "CORROBORATED"
    assert manifest.platform_status == "CORROBORATED"
    assert manifest.game_version_status == "CORROBORATED"
    assert manifest.region_status == "USER_ASSERTED"
    assert manifest.membership_status == "USER_ASSERTED"
    assert manifest.manager_authority_status == "UNVERIFIED"
    assert manifest.corroborated_equals_verified is False
    assert manifest.unique_independent_sources == 3
    assert manifest.duplicate_observations_detected == 0
    assert manifest.conflicts_detected == ()


def test_120_offline_evidence_duplicate_detection() -> None:
    obs = build_default_east_coasters_offline_observations()
    # Supply the first observation twice; single unique source must NOT corroborate club_id
    dup_list = [obs[0], obs[0]]
    manifest = evaluate_offline_club_evidence(dup_list)
    assert manifest.total_observations == 2
    assert manifest.unique_independent_sources == 1
    assert manifest.duplicate_observations_detected == 1
    assert manifest.club_id_status == "PROVISIONAL"
    assert manifest.platform_status == "PROVISIONAL"


def test_121_offline_evidence_conflict_detection() -> None:
    obs = list(build_default_east_coasters_offline_observations())
    u3 = EAST_COASTERS_SECONDARY_OBSERVATION_URLS[2]
    conflicting = FC27OfflinePublicObservation(
        observation_id="obs-conflict",
        source_url=u3,
        url_sha256=compute_url_sha256(u3),
        source_operator="proclubsanalytics.com",
        observed_at="2026-10-27T12:10:00Z",
        page_title="Different Club",
        observed_fields={
            "club_name": "Wrong Club Name",
            "club_id": "999999",
            "platform": "nx",
            "game_version": "FC26",
        },
    )
    manifest = evaluate_offline_club_evidence([obs[0], obs[1], conflicting])
    assert manifest.club_name_status == "CONFLICTED"
    assert manifest.club_id_status == "CONFLICTED"
    assert manifest.platform_status == "CONFLICTED"
    assert manifest.game_version_status == "CONFLICTED"
    assert len(manifest.conflicts_detected) == 4


def test_122_offline_evidence_deterministic_manifest_hash() -> None:
    obs = build_default_east_coasters_offline_observations()
    m1 = evaluate_offline_club_evidence(obs)
    m2 = evaluate_offline_club_evidence(obs)
    assert m1.manifest_sha256 == m2.manifest_sha256
    assert len(m1.manifest_sha256) == 64


def test_123_role_attestation_templates_non_personal() -> None:
    templates = get_attestation_templates()
    assert set(templates.keys()) == {"CLUB_MANAGER", "CLUB_ADMIN", "DATA_STEWARD"}
    for role, tmpl in templates.items():
        assert tmpl.supplier_role == role
        assert "@" not in tmpl.supplier_reference
        assert tmpl.non_personal_reference_only is True
        assert tmpl.retention_acknowledged is True
        assert tmpl.analytics_use_acknowledged is True
        assert tmpl.team_review_acknowledged is True
    with pytest.raises(ValidationError, match="non-personal"):
        FC27RoleAttestationTemplate(
            supplier_role="CLUB_MANAGER",
            supplier_reference="manager@example.com",
            authorized_scopes=("CLUB_IDENTITY", "MATCH_RESULTS"),
        )


def test_124_user_export_pilot_sequence_documented() -> None:
    assert len(USER_EXPORT_PILOT_SEQUENCE) == 8
    assert "3. --validate" in USER_EXPORT_PILOT_SEQUENCE
    assert "5. --dry-run" in USER_EXPORT_PILOT_SEQUENCE
    assert "7. No --apply" in USER_EXPORT_PILOT_SEQUENCE
    assert "8. No recommendations" in USER_EXPORT_PILOT_SEQUENCE


def test_125_cli_show_claims(capsys: pytest.CaptureFixture[str]) -> None:
    rc = run_cli(["--show-claims"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "EVIDENCE_MODE=SHOW_CLAIMS" in out
    assert "CANONICAL_CLUB_NAME=East Coasters" in out
    assert "CLUB_ID=330323" in out
    assert "REGION_STATUS=USER_ASSERTED" in out
    assert "MEMBERSHIP_STATUS=USER_ASSERTED" in out
    assert "MANAGER_AUTHORITY_STATUS=UNVERIFIED" in out
    assert "ATTESTATION_ROLES=CLUB_ADMIN,CLUB_MANAGER,DATA_STEWARD" in out
    assert "REQUESTS_EXECUTED=0" in out


def test_126_cli_evaluate_offline_evidence_and_mock(capsys: pytest.CaptureFixture[str]) -> None:
    rc = run_cli(["--evaluate-offline-evidence", "--mock"])
    out = capsys.readouterr().out
    assert rc == 0
    for expected in (
        "EVIDENCE_MODE=OFFLINE_EVALUATION",
        "MOCK_MODE=YES",
        "CLUB_NAME_STATUS=CORROBORATED",
        "CLUB_ID_STATUS=CORROBORATED",
        "PLATFORM_STATUS=CORROBORATED",
        "GAME_VERSION_STATUS=CORROBORATED",
        "REGION_STATUS=USER_ASSERTED",
        "MEMBERSHIP_STATUS=USER_ASSERTED",
        "MANAGER_AUTHORITY_STATUS=UNVERIFIED",
        "CORROBORATED_EQUALS_VERIFIED=NO",
        "PAGE_FETCHING_USED=NO",
        "SCRAPING_USED=NO",
        "BROWSER_CALL_REPLAY_USED=NO",
        "REQUESTS_EXECUTED=0",
    ):
        assert expected in out


# ==============================================================================
# 127-136: Part A Remediation & Hardening Tests (Phases A3-A11)
# ==============================================================================

def test_127_secondary_url_policy_and_fragment_stripping() -> None:
    base_url = "https://proclubstracker.com/club/330323?platform=common-gen5"
    with_fragment = "https://proclubstracker.com/club/330323?platform=common-gen5#section-stats"
    assert compute_url_sha256(base_url) == compute_url_sha256(with_fragment)
    canonical, grp = canonicalize_secondary_url(with_fragment)
    assert canonical == base_url
    assert grp == FC27SourceIndependenceGroup.PROCLUBS_TRACKER.value

    for bad_url in (
        "http://proclubstracker.com/club/330323",
        "https://user:pass@proclubstracker.com/club/330323",
        "https://localhost/club/330323",
        "https://127.0.0.1/club/330323",
        "https://192.168.1.10/club/330323",
        "https://proclubstracker.com:8443/club/330323",
        "https://xn--proclubstracker-99a.com/club/330323",
        "https://pr\u043eclubstracker.com/club/330323",
        "https://untrusted-site.example.com/club/330323",
    ):
        with pytest.raises(ValueError):
            canonicalize_secondary_url(bad_url)


def test_128_independence_group_deduplication_prevents_self_corroboration() -> None:
    u1 = "https://proclubstracker.com/club/330323?platform=common-gen5"
    u2 = "https://www.proclubstracker.com/club/330323?platform=common-gen5&view=overview"
    obs1 = FC27OfflinePublicObservation(
        observation_id="obs-pct-1",
        source_url=u1,
        url_sha256=compute_url_sha256(u1),
        source_operator="proclubstracker.com",
        observed_at="2026-10-27T12:00:00Z",
        page_title="East Coasters | Pro Clubs Tracker",
        observed_fields={
            "club_name": "East Coasters",
            "club_id": "330323",
            "platform": "common-gen5",
            "game_version": "FC27",
        },
    )
    obs2 = FC27OfflinePublicObservation(
        observation_id="obs-pct-2",
        source_url=u2,
        url_sha256=compute_url_sha256(u2),
        source_operator="www.proclubstracker.com",
        observed_at="2026-10-27T12:05:00Z",
        page_title="East Coasters Overview | Pro Clubs Tracker",
        observed_fields={
            "club_name": "East Coasters",
            "club_id": "330323",
            "platform": "common-gen5",
            "game_version": "FC27",
        },
    )
    manifest = evaluate_offline_club_evidence([obs1, obs2])
    assert manifest.unique_independent_sources == 1
    assert manifest.duplicate_observations_detected == 1
    assert manifest.club_name_status == FC27EvidenceStatusHierarchy.PROVISIONAL.value
    assert manifest.club_id_status == FC27EvidenceStatusHierarchy.PROVISIONAL.value
    assert manifest.platform_status == FC27EvidenceStatusHierarchy.PROVISIONAL.value
    assert manifest.game_version_status == FC27EvidenceStatusHierarchy.PROVISIONAL.value


def test_129_same_observation_id_with_different_content_flags_conflict() -> None:
    u1 = EAST_COASTERS_SECONDARY_OBSERVATION_URLS[0]
    u2 = EAST_COASTERS_SECONDARY_OBSERVATION_URLS[1]
    obs1 = FC27OfflinePublicObservation(
        observation_id="same-obs-id",
        source_url=u1,
        url_sha256=compute_url_sha256(u1),
        source_operator="proclubstracker.com",
        observed_at="2026-10-27T12:00:00Z",
        page_title="East Coasters",
        observed_fields={"club_name": "East Coasters", "club_id": "330323"},
    )
    obs2 = FC27OfflinePublicObservation(
        observation_id="same-obs-id",
        source_url=u2,
        url_sha256=compute_url_sha256(u2),
        source_operator="myproclubs.com",
        observed_at="2026-10-27T12:05:00Z",
        page_title="East Coasters Modified",
        observed_fields={"club_name": "East Coasters", "club_id": "330323", "platform": "common-gen5"},
    )
    assert obs1.evidence_sha256 != obs2.evidence_sha256
    manifest = evaluate_offline_club_evidence([obs1, obs2])
    assert "EVIDENCE_ID_CONFLICT:same-obs-id" in manifest.conflicts_detected
    assert "same-obs-id" in manifest.conflicting_evidence_ids
    assert manifest.conflict_count >= 1


def test_130_majority_vote_never_clears_conflict() -> None:
    obs = list(build_default_east_coasters_offline_observations())
    u3 = EAST_COASTERS_SECONDARY_OBSERVATION_URLS[2]
    # 2 matching sources (obs[0], obs[1]) + 1 conflicting source -> still CONFLICTED
    conflict_obs = FC27OfflinePublicObservation(
        observation_id="obs-minority-conflict",
        source_url=u3,
        url_sha256=compute_url_sha256(u3),
        source_operator="proclubsanalytics.com",
        observed_at="2026-10-27T12:15:00Z",
        page_title="East Coasters Conflict",
        observed_fields={
            "club_name": "East Coasters",
            "club_id": "330323",
            "platform": "nx",
            "game_version": "FC27",
            "club_abbreviation": "WCF",
            "matchmaking_region": "British Isles",
        },
    )
    manifest = evaluate_offline_club_evidence([obs[0], obs[1], conflict_obs])
    assert manifest.platform_status == FC27EvidenceStatusHierarchy.CONFLICTED.value
    assert manifest.region_status == FC27EvidenceStatusHierarchy.CONFLICTED.value
    assert "platform" in manifest.unresolved_fields
    assert "club_abbreviation" in manifest.unresolved_fields
    assert "matchmaking_region" in manifest.unresolved_fields
    assert "obs-minority-conflict" in manifest.conflicting_evidence_ids


def test_131_deterministic_evidence_content_sha256_key_order_independent() -> None:
    u = EAST_COASTERS_SECONDARY_OBSERVATION_URLS[0]
    h1 = compute_evidence_content_sha256(
        url_sha256=compute_url_sha256(u),
        independence_group="PROCLUBS_TRACKER",
        observed_fields={"club_id": "330323", "club_name": "East Coasters", "platform": "common-gen5"},
    )
    h2 = compute_evidence_content_sha256(
        url_sha256=compute_url_sha256(u + "#ignored-fragment"),
        independence_group="PROCLUBS_TRACKER",
        observed_fields={"platform": "common-gen5", "club_name": "East Coasters", "club_id": "330323"},
    )
    assert h1 == h2


def test_132_attestation_template_expiry_and_authority_isolation() -> None:
    templates = get_attestation_templates()
    for role, tmpl in templates.items():
        assert tmpl.supplier_role == role
        assert tmpl.manager_authority_status == "UNVERIFIED"
        assert tmpl.review_decision == "PENDING_HUMAN_REVIEW"
        assert len(tmpl.evidence_references) >= 1
    with pytest.raises(ValidationError, match="expires_at must be strictly later"):
        FC27RoleAttestationTemplate(
            supplier_role="CLUB_MANAGER",
            supplier_reference="ecf-role-ref-club-manager-01",
            authorized_scopes=("CLUB_IDENTITY",),
            attestation_timestamp="2026-10-27T12:00:00Z",
            expires_at="2026-10-27T11:00:00Z",
        )


def test_133_cli_standalone_mock_flag(capsys: pytest.CaptureFixture[str]) -> None:
    rc = run_cli(["--mock"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "EVIDENCE_MODE=OFFLINE_EVALUATION" in out
    assert "MOCK_MODE=YES" in out
    assert "CLUB_ID_STATUS=CORROBORATED" in out
    assert "MANAGER_AUTHORITY_STATUS=UNVERIFIED" in out
    assert "REQUESTS_EXECUTED=0" in out


def test_134_cli_evaluate_offline_evidence_with_local_json_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    obs = build_default_east_coasters_offline_observations()
    payload = {"observations": [o.model_dump() for o in obs]}
    json_path = tmp_path / "offline_evidence.json"
    json_path.write_text(json.dumps(payload), encoding="utf-8")

    rc = run_cli(["--evaluate-offline-evidence", "--input", str(json_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "EVIDENCE_MODE=OFFLINE_EVALUATION" in out
    assert "CLUB_ID_STATUS=CORROBORATED" in out
    assert "CONFLICT_COUNT=0" in out
    assert "REQUESTS_EXECUTED=0" in out


def test_135_cli_offline_evidence_input_rejects_html_and_sensitive_fields(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    html_file = tmp_path / "bad_html.json"
    html_file.write_text('[{"page_title": "<html>bad</html>"}]', encoding="utf-8")
    assert run_cli(["--evaluate-offline-evidence", "--input", str(html_file)]) != 0

    sensitive_file = tmp_path / "sensitive.json"
    sensitive_file.write_text(
        json.dumps([
            {
                "observation_id": "bad-sens",
                "source_url": EAST_COASTERS_SECONDARY_OBSERVATION_URLS[0],
                "source_operator": "proclubstracker.com",
                "observed_at": "2026-10-27T12:00:00Z",
                "page_title": "East Coasters",
                "observed_fields": {"club_id": "330323", "persona_id": "99999"},
            }
        ]),
        encoding="utf-8",
    )
    assert run_cli(["--evaluate-offline-evidence", "--input", str(sensitive_file)]) != 0
    capsys.readouterr()


def test_136_manifest_records_matching_and_conflicting_ids_and_reason_codes() -> None:
    obs = build_default_east_coasters_offline_observations()
    manifest = evaluate_offline_club_evidence(obs)
    assert len(manifest.matching_evidence_ids) == 3
    assert manifest.conflicting_evidence_ids == ()
    assert set(manifest.unresolved_fields) == {
        "club_abbreviation",
        "manager_authority",
        "matchmaking_region",
        "user_membership",
    }
    assert "MULTI_GROUP_CORROBORATED_NOT_VERIFIED" in manifest.reason_codes
    assert "MANAGER_AUTHORITY_UNVERIFIED" in manifest.reason_codes


def test_137_east_coasters_export_pilot_package_deliverables() -> None:
    pkg_path = REPO_ROOT / "docs" / "FC27_EAST_COASTERS_EXPORT_PILOT_PACKAGE.md"
    assert pkg_path.is_file()
    content = pkg_path.read_text(encoding="utf-8")
    for required_token in (
        "## 1. East Coasters Pilot-Input Checklist",
        "## 2. Non-Personal Supplier-Attestation Templates",
        "## 3. Consent-Attestation Template & Scope Matrix",
        "## 4. Blank / Synthetic JSON Export Template",
        "## 5. Blank / Synthetic CSV Export Template",
        "## 6. Local File Placement Instructions",
        "## 7. Required 6-Step Operational Command Sequence",
        "## 8. Human Metadata-Review Checklist",
        "## 9. Readiness-Preview Interpretation Guide",
        "## 10. Sensitive-Data Incident Procedure",
        "## 11. Raw-File Retention and Deletion Guidance",
        "## 12. Explicit Operational Prohibitions",
        "CLUB_MANAGER",
        "CLUB_ADMIN",
        "DATA_STEWARD",
        "CLUB_IDENTITY",
        "MATCH_RESULTS",
        "LINEUPS",
        "PLAYER_PERFORMANCE",
        "BUILD_REFERENCES",
        "RETENTION_ACKNOWLEDGED=YES",
        "ANALYTICS_USE_ACKNOWLEDGED=YES",
        "TEAM_REVIEW_ACKNOWLEDGED=YES",
        "scripts/fc27_user_export_import.py",
        "--validate",
        "--dry-run",
        "INSUFFICIENT_DATA",
        "EXPLORATORY",
        "PROVISIONAL_TRENDS",
        "HIGH_SAMPLE_READINESS",
        "SAFE_TO_RETAIN_RAW_UPLOAD: NO",
        "Explicit Prohibition on `--apply`",
        "Explicit Prohibition on Recommendations",
        "Explicit Prohibition on Committing Pilot Data",
    ):
        assert required_token in content


def test_138_pilot_package_templates_synthetic_and_blank_no_personal_paths() -> None:
    pkg_path = REPO_ROOT / "docs" / "FC27_EAST_COASTERS_EXPORT_PILOT_PACKAGE.md"
    content = pkg_path.read_text(encoding="utf-8")
    assert "<ABSOLUTE_LOCAL_PATH_OUTSIDE_REPOSITORY>" in content
    assert "<CALCULATE_PAYLOAD_SHA256_BEFORE_VALIDATION>" in content
    assert "fc27_pilot_intake" not in content
    assert "@" not in content.replace("`@`", "").replace("`'@`", "")
    assert "CORROBORATED" in content
    assert "MANAGER_AUTHORITY_STATUS=UNVERIFIED" in content
    assert "MATCHMAKING_REGION_CLAIM=East Coast US 1" in content


def test_139_pilot_package_supplier_and_consent_attestation_rules() -> None:
    pkg_path = REPO_ROOT / "docs" / "FC27_EAST_COASTERS_EXPORT_PILOT_PACKAGE.md"
    content = pkg_path.read_text(encoding="utf-8")
    for required_field in (
        '"supplier_role": "CLUB_MANAGER"',
        '"supplier_role": "CLUB_ADMIN"',
        '"supplier_role": "DATA_STEWARD"',
        '"club_identity_reference"',
        '"evidence_references"',
        '"review_status": "PENDING_HUMAN_REVIEW"',
        '"reviewer_reference"',
        '"review_reason"',
        '"manager_authority_status": "UNVERIFIED"',
        '"expires_at"',
        '"consent_version": "1.0.0"',
        '"revoked_at": null',
    ):
        assert required_field in content


def test_140_pilot_package_json_and_csv_templates_parse_valid_once_hash_computed() -> None:
    import json
    import re
    from datetime import datetime, timedelta, timezone
    from src.fc27_user_export import (
        compute_payload_sha256,
        parse_fc27_user_export_csv,
        parse_fc27_user_export_json,
    )

    pkg_path = REPO_ROOT / "docs" / "FC27_EAST_COASTERS_EXPORT_PILOT_PACKAGE.md"
    content = pkg_path.read_text(encoding="utf-8")

    # Extract the full JSON export template block
    json_blocks = re.findall(r"```json\n(\{.*?\})\n```", content, flags=re.DOTALL)
    export_blocks = [b for b in json_blocks if '"export_id"' in b]
    assert len(export_blocks) == 1
    template_dict = json.loads(export_blocks[0])
    assert template_dict["payload_sha256"] == "<CALCULATE_PAYLOAD_SHA256_BEFORE_VALIDATION>"
    assert template_dict["game_version"] == "FC27"
    assert template_dict["platform"] == "common-gen5"
    assert template_dict["source_classification"] == "USER_SUPPLIED_EXPORT"

    now_utc = datetime.now(timezone.utc)
    gen_str = (now_utc - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    sup_str = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    played_str = (now_utc - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    template_dict["generated_at"] = gen_str
    template_dict["supplied_at"] = sup_str
    template_dict["supplier_attestation"]["attested_at"] = gen_str
    template_dict["consent_attestation"]["consented_at"] = gen_str
    template_dict["matches"][0]["played_at"] = played_str
    template_dict["payload_sha256"] = compute_payload_sha256(template_dict)

    parsed_json = parse_fc27_user_export_json(json.dumps(template_dict).encode("utf-8"))
    assert parsed_json.club.club_id == "330323"
    assert len(parsed_json.matches) == 1

    # Extract the CSV template block
    csv_blocks = re.findall(r"```csv\n(.*?)\n```", content, flags=re.DOTALL)
    assert len(csv_blocks) == 1
    csv_text = csv_blocks[0].replace("2026-10-27T10:00:00Z", played_str)
    parsed_csv = parse_fc27_user_export_csv(csv_text.encode("utf-8"))
    assert parsed_csv.club.club_id == "330323"
    assert len(parsed_csv.matches) == 1


def test_141_pilot_package_csv_formula_injection_whitespace_and_apostrophe_guard() -> None:
    from src.fc27_user_export import FC27FormulaInjectionError, parse_fc27_user_export_csv

    header = "club_id,club_name,platform,game_version,external_match_id,played_at,result,goals_for,goals_against,opponent_name,formation\n"
    for bad_cell in (
        "=CMD()",
        "+SUM(1,2)",
        "-1+1",
        "@SUM(A1)",
        "   =CMD()",
        "\t+CMD()",
        "\u00a0-CMD()",
        "\u2003@CMD()",
        "'=CMD()",
        " '+CMD()",
        "'\u00a0@CMD()",
    ):
        row = f'330323,East Coasters,common-gen5,FC27,M-1,2026-10-25T10:00:00Z,WIN,2,1,"{bad_cell}",4-3-3\n'
        with pytest.raises(FC27FormulaInjectionError):
            parse_fc27_user_export_csv((header + row).encode("utf-8"))


def test_142_pilot_package_command_ordering_and_human_review_gate() -> None:
    pkg_path = REPO_ROOT / "docs" / "FC27_EAST_COASTERS_EXPORT_PILOT_PACKAGE.md"
    content = pkg_path.read_text(encoding="utf-8")
    validate_pos = content.index("### 7.1 Step 1 Command: Offline Validation (`--validate`)")
    dry_run_pos = content.index("### 7.2 Step 4 Command: Offline Dry-Run (`--dry-run`)")
    assert validate_pos < dry_run_pos
    assert "HUMAN_DRY_RUN_APPROVAL=YES" in content
    for idx in range(1, 29):
        assert f"- [ ] {idx}. **" in content


def test_143_pilot_package_readiness_tiers_and_invariants() -> None:
    pkg_path = REPO_ROOT / "docs" / "FC27_EAST_COASTERS_EXPORT_PILOT_PACKAGE.md"
    content = pkg_path.read_text(encoding="utf-8")
    for required_phrase in (
        "`0` to `2`",
        "`3` to `9`",
        "`10` to `19`",
        "`20` or more (`20+`)",
        "INSUFFICIENT_DATA",
        "EXPLORATORY",
        "PROVISIONAL_TRENDS",
        "HIGH_SAMPLE_READINESS",
        "Not Team-Quality Scoring",
        "Not Coaching Confidence",
        "Does Not Enable Recommendations",
    ):
        assert required_phrase in content


def test_144_pilot_package_incident_procedure_and_retention_rules() -> None:
    pkg_path = REPO_ROOT / "docs" / "FC27_EAST_COASTERS_EXPORT_PILOT_PACKAGE.md"
    content = pkg_path.read_text(encoding="utf-8")
    for step_num in range(1, 11):
        assert f"{step_num}. **" in content
    assert "must not be deleted by the agent or tooling without explicit operator instruction" in content
    assert "AGREED_RAW_FILE_RETENTION" in content
