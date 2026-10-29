"""Authorized API-First Provider Contract, Source Discovery, and Normalization Adapter for East Coasters (FC 27).

Implements:
- Part B: Candidate API source discovery and evaluation matrix (8 candidates across 7 categories)
- Part C: Refined EA approved-access and data-portability request draft (EA_ACCESS_REQUEST_SENT=NO)
- Part D: Provider-neutral contract (FC27AuthorizedProvider Protocol & FC27AuthorizedProviderDeclaration)
- Part E: Integration with bounded transport security policy (FC27AuthorizedTransportClient)
- Part F: Normalization, pseudonymization (plr_ecf_<hash12>), gamer-tag/external-ID suppression,
  deterministic payload/match hashing, and idempotent replay/conflict planning
- Part H: Provider selection gate (fails closed when no external source satisfies all legal/technical criteria)
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Mapping, Optional, Protocol, Sequence, Tuple, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.fc27_authorized_transport import (
    ALL_AUTHORIZED_OPERATIONS,
    MEMBER_DATA_OPERATIONS,
    PLAYER_DATA_OPERATIONS,
    FC27AuthorizedCredentials,
    FC27AuthorizedOperation,
    FC27AuthorizedTransportClient,
    FC27AuthorizedTransportPolicy,
    FC27MetadataTransportLog,
    FC27RetryPolicy,
)
from src.fc27_provider import (
    EAST_COASTERS_CLUB_ID,
    EAST_COASTERS_CLUB_NAME,
    FC27ProviderError,
    FC27TransportError,
    create_provisional_club_identity,
)
from src.fc27_user_export import (
    FC27ConsentAttestation,
    FC27ImportState,
    FC27SuppliedClubIdentity,
    FC27SuppliedClubStatistics,
    FC27SuppliedMatch,
    FC27SuppliedPlayerPerformance,
    FC27SupplierAttestation,
    FC27UserExportEnvelope,
    FC27UserExportImportPlan,
    compute_match_hash,
    compute_payload_sha256,
    plan_fc27_user_export_import,
)
from src.schemas import ClubIdentityRecord, ClubIdentityState, VALID_FC27_POSITIONS
from src.settings import (
    is_dispatch_enabled,
    is_dispatch_worker_enabled,
    is_fc27_analytics_enabled,
    is_fc27_live_ingestion_enabled,
    is_fc27_persistence_enabled,
    is_fc27_provider_enabled,
    is_fc27_recommendations_enabled,
)


# ==============================================================================
# Part B: Candidate API Source Discovery & Classification
# ==============================================================================

class FC27ProviderCandidateClassification(str, Enum):
    OFFICIAL_AUTHORIZED = "OFFICIAL_AUTHORIZED"
    LICENSED_THIRD_PARTY = "LICENSED_THIRD_PARTY"
    COMMUNITY_DOCUMENTED = "COMMUNITY_DOCUMENTED"
    UNOFFICIAL_CLIENT = "UNOFFICIAL_CLIENT"
    UNVERIFIED_RELAY = "UNVERIFIED_RELAY"
    NOT_ACCEPTABLE = "NOT_ACCEPTABLE"


APPROVED_PROVIDER_CLASSIFICATIONS: frozenset[str] = frozenset(
    {
        FC27ProviderCandidateClassification.OFFICIAL_AUTHORIZED.value,
        FC27ProviderCandidateClassification.LICENSED_THIRD_PARTY.value,
    }
)


class FC27ProviderCandidateRecord(BaseModel):
    """Comprehensive evaluation record for a candidate FC 27 Clubs data source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_name: str
    legal_entity: str
    category_evaluated: str
    provider_classification: FC27ProviderCandidateClassification
    api_documentation_url: str
    terms_url: str
    privacy_url: str
    licensing_url: str
    contact_url: str
    authentication_method: str
    pricing: str
    trial_availability: str
    supported_fc_versions: Tuple[str, ...]
    supported_platforms: Tuple[str, ...]
    club_search_support: bool
    club_id_lookup_support: bool
    club_statistics_support: bool
    recent_matches_support: bool
    match_history_depth: str
    pagination_support: str
    member_statistics_support: bool
    player_performance_statistics_support: bool
    career_statistics_support: bool
    playoff_data_support: bool
    rate_limits: str
    retry_guidance: str
    caching_rights: str
    normalized_storage_rights: str
    retention_rights: str
    internal_analytics_rights: str
    member_data_permissions: str
    player_data_permissions: str
    redistribution_restrictions: str
    deletion_requirements: str
    sla: str
    maintenance_status: str
    source_provenance: str
    ultimately_proxies_ea: bool
    requires_cookies_or_sessions: bool
    requires_browser_or_tls_impersonation: bool
    eligible_for_selection: bool = False
    rejection_reason: str


REVIEWED_API_SOURCE_CANDIDATES: Tuple[FC27ProviderCandidateRecord, ...] = (
    FC27ProviderCandidateRecord(
        provider_name="EA_FC_PROCLUBS_PUBLIC_WEB_ENDPOINTS",
        legal_entity="Electronic Arts Inc.",
        category_evaluated="1. Official EA API or developer program",
        provider_classification=FC27ProviderCandidateClassification.NOT_ACCEPTABLE,
        api_documentation_url="NONE_PUBLISHED",
        terms_url="https://www.ea.com/legal/user-agreement",
        privacy_url="https://www.ea.com/legal/privacy-and-cookie-policy",
        licensing_url="https://www.ea.com/legal/user-agreement",
        contact_url="https://help.ea.com/",
        authentication_method="NONE_DOCUMENTED (Akamai bot filter blocks standard HTTP with 403)",
        pricing="N/A (undocumented internal web backend)",
        trial_availability="NO",
        supported_fc_versions=("FC27",),
        supported_platforms=("common-gen5", "nx"),
        club_search_support=True,
        club_id_lookup_support=True,
        club_statistics_support=True,
        recent_matches_support=True,
        match_history_depth="Last 5-25 matches per matchType",
        pagination_support="NONE (maxResultCount cap only)",
        member_statistics_support=True,
        player_performance_statistics_support=True,
        career_statistics_support=True,
        playoff_data_support=True,
        rate_limits="UNDOCUMENTED (HTTP 403 returned on first standard HTTPS request)",
        retry_guidance="NONE_DOCUMENTED",
        caching_rights="NOT_GRANTED",
        normalized_storage_rights="NOT_GRANTED",
        retention_rights="NOT_GRANTED",
        internal_analytics_rights="NOT_GRANTED_FOR_AUTOMATED_ACCESS",
        member_data_permissions="NOT_GRANTED_FOR_AUTOMATED_SCRAPING",
        player_data_permissions="NOT_GRANTED_FOR_AUTOMATED_SCRAPING",
        redistribution_restrictions="EA User Agreement Section 6 prohibits unapproved automated access",
        deletion_requirements="EA Privacy Policy",
        sla="NONE",
        maintenance_status="ACTIVE_INTERNAL_WEB_BACKEND",
        source_provenance="First-party EA web endpoints (https://proclubs.ea.com/api/fc/*)",
        ultimately_proxies_ea=False,
        requires_cookies_or_sessions=False,
        requires_browser_or_tls_impersonation=True,
        eligible_for_selection=False,
        rejection_reason="Returns HTTP 403 Forbidden to standard HTTPS clients; requires browser/TLS impersonation or written EA authorization.",
    ),
    FC27ProviderCandidateRecord(
        provider_name="EA_APPROVED_PARTNER_OR_WRITTEN_ACCESS_PROGRAM",
        legal_entity="Electronic Arts Inc.",
        category_evaluated="2. EA partner or approved-access program",
        provider_classification=FC27ProviderCandidateClassification.NOT_ACCEPTABLE,
        api_documentation_url="NONE_PUBLICLY_AVAILABLE",
        terms_url="https://www.ea.com/legal/user-agreement",
        privacy_url="https://www.ea.com/legal/privacy-and-cookie-policy",
        licensing_url="https://www.ea.com/legal",
        contact_url="https://help.ea.com/",
        authentication_method="PENDING_EA_WRITTEN_AUTHORIZATION_OR_API_KEY",
        pricing="REQUIRES_EA_AGREEMENT",
        trial_availability="NO_SELF_SERVICE_TRIAL",
        supported_fc_versions=("FC27",),
        supported_platforms=("common-gen5",),
        club_search_support=True,
        club_id_lookup_support=True,
        club_statistics_support=True,
        recent_matches_support=True,
        match_history_depth="Subject to written EA agreement",
        pagination_support="Subject to written EA agreement",
        member_statistics_support=True,
        player_performance_statistics_support=True,
        career_statistics_support=True,
        playoff_data_support=True,
        rate_limits="Subject to written EA agreement (proposed <= 5 req/min)",
        retry_guidance="Zero retries unless permitted by written EA agreement",
        caching_rights="REQUIRES_WRITTEN_EA_APPROVAL",
        normalized_storage_rights="REQUIRES_WRITTEN_EA_APPROVAL",
        retention_rights="REQUIRES_WRITTEN_EA_APPROVAL (730 days requested)",
        internal_analytics_rights="REQUIRES_WRITTEN_EA_APPROVAL",
        member_data_permissions="REQUIRES_WRITTEN_EA_APPROVAL_AND_MEMBER_CONSENT",
        player_data_permissions="REQUIRES_WRITTEN_EA_APPROVAL_AND_MEMBER_CONSENT",
        redistribution_restrictions="Internal East Coasters team review only; no publication or resale",
        deletion_requirements="Immediate purge upon member consent withdrawal or EA request",
        sla="NONE_UNTIL_EXECUTED",
        maintenance_status="PENDING_EA_RESPONSE",
        source_provenance="First-party EA (pending formal permission request in ADR Section 20.3)",
        ultimately_proxies_ea=False,
        requires_cookies_or_sessions=False,
        requires_browser_or_tls_impersonation=False,
        eligible_for_selection=False,
        rejection_reason="No public developer portal or issued credentials exist; formal EA access request is drafted (EA_ACCESS_REQUEST_SENT=NO).",
    ),
    FC27ProviderCandidateRecord(
        provider_name="EA_ACCOUNT_DATA_PORTABILITY_EXPORT_SERVICE",
        legal_entity="Electronic Arts Inc.",
        category_evaluated="3. EA data-portability or club export service",
        provider_classification=FC27ProviderCandidateClassification.NOT_ACCEPTABLE,
        api_documentation_url="https://help.ea.com/en/help/account/how-to-request-your-ea-account-data/",
        terms_url="https://www.ea.com/legal/user-agreement",
        privacy_url="https://www.ea.com/legal/privacy-and-cookie-policy",
        licensing_url="https://www.ea.com/legal/privacy-and-cookie-policy",
        contact_url="https://myaccount.ea.com/",
        authentication_method="Manual individual EA account login (not an automated API)",
        pricing="FREE (GDPR/CCPA individual account archive)",
        trial_availability="N/A",
        supported_fc_versions=("FC27",),
        supported_platforms=("common-gen5",),
        club_search_support=False,
        club_id_lookup_support=False,
        club_statistics_support=False,
        recent_matches_support=False,
        match_history_depth="Individual account telemetry only; does not provide automated Club 330323 API",
        pagination_support="NONE",
        member_statistics_support=False,
        player_performance_statistics_support=False,
        career_statistics_support=False,
        playoff_data_support=False,
        rate_limits="Manual download archive only",
        retry_guidance="N/A",
        caching_rights="Personal use only",
        normalized_storage_rights="UNCONFIRMED_FOR_MULTI_MEMBER_CLUB_ROSTERS",
        retention_rights="Personal use only",
        internal_analytics_rights="Personal use only",
        member_data_permissions="Individual account holder only",
        player_data_permissions="Individual account holder only",
        redistribution_restrictions="Personal data portability archive",
        deletion_requirements="User-managed",
        sla="Up to 30 days per manual privacy request",
        maintenance_status="ACTIVE_MANUAL_PORTAL",
        source_provenance="First-party EA account privacy portal",
        ultimately_proxies_ea=False,
        requires_cookies_or_sessions=True,
        requires_browser_or_tls_impersonation=False,
        eligible_for_selection=False,
        rejection_reason="Not an automated API and does not expose structured Club ID 330323 multi-member match/player endpoints.",
    ),
    FC27ProviderCandidateRecord(
        provider_name="PANDASCORE_AND_SPORTRADAR_ESPORTS_APIS",
        legal_entity="PandaScore SAS / Sportradar AG",
        category_evaluated="4. Licensed FC esports data providers",
        provider_classification=FC27ProviderCandidateClassification.NOT_ACCEPTABLE,
        api_documentation_url="https://developers.pandascore.co/docs",
        terms_url="https://pandascore.co/terms-of-use",
        privacy_url="https://pandascore.co/privacy-policy",
        licensing_url="https://pandascore.co/pricing",
        contact_url="https://pandascore.co/contact",
        authentication_method="Bearer API token (Authorization header)",
        pricing="Commercial B2B tiers",
        trial_availability="YES (limited tournament fixtures)",
        supported_fc_versions=("EA SPORTS FC Pro Esports Tournaments",),
        supported_platforms=("Broadcast Esports Tournaments Only",),
        club_search_support=False,
        club_id_lookup_support=False,
        club_statistics_support=False,
        recent_matches_support=False,
        match_history_depth="0 retail Pro Clubs matches (covers 1v1/2v2 pro esports leagues only)",
        pagination_support="Page/per_page for tournament matches only",
        member_statistics_support=False,
        player_performance_statistics_support=False,
        career_statistics_support=False,
        playoff_data_support=False,
        rate_limits="1,000-4,000 req/hour depending on plan",
        retry_guidance="Standard HTTP 429 Retry-After",
        caching_rights="Commercial contract dependent",
        normalized_storage_rights="Commercial contract dependent",
        retention_rights="Commercial contract dependent",
        internal_analytics_rights="Commercial contract dependent",
        member_data_permissions="Pro esports players only",
        player_data_permissions="Pro esports players only",
        redistribution_restrictions="Commercial license required",
        deletion_requirements="Contract termination purge",
        sla="Commercial SLA",
        maintenance_status="ACTIVE",
        source_provenance="Licensed tournament feeds",
        ultimately_proxies_ea=False,
        requires_cookies_or_sessions=False,
        requires_browser_or_tls_impersonation=False,
        eligible_for_selection=False,
        rejection_reason="Covers professional esports broadcast tournaments only; zero support for retail FC 27 Clubs (clubId=330323 on common-gen5).",
    ),
    FC27ProviderCandidateRecord(
        provider_name="FC27_CLUBS_API_PYTHON_CLIENT",
        legal_entity="Erkan Dogan (Open-Source GitHub Contributor)",
        category_evaluated="5. Community APIs / Open-Source Client Libraries",
        provider_classification=FC27ProviderCandidateClassification.UNOFFICIAL_CLIENT,
        api_documentation_url="https://github.com/1erkandogan/fc27-clubs-api",
        terms_url="https://github.com/1erkandogan/fc27-clubs-api/blob/main/LICENSE",
        privacy_url="NONE",
        licensing_url="https://github.com/1erkandogan/fc27-clubs-api/blob/main/LICENSE",
        contact_url="https://github.com/1erkandogan/fc27-clubs-api/issues",
        authentication_method="Spoofed Chrome 141 User-Agent, sec-ch-ua, sec-fetch-site, and Referer headers",
        pricing="MIT Open-Source Client Library (not a data provider)",
        trial_availability="N/A",
        supported_fc_versions=("FC27",),
        supported_platforms=("common-gen5", "nx"),
        club_search_support=True,
        club_id_lookup_support=True,
        club_statistics_support=True,
        recent_matches_support=True,
        match_history_depth="Calls blocked proclubs.ea.com endpoints directly",
        pagination_support="NONE",
        member_statistics_support=True,
        player_performance_statistics_support=True,
        career_statistics_support=True,
        playoff_data_support=True,
        rate_limits="NONE (unmanaged direct calls to proclubs.ea.com)",
        retry_guidance="NONE",
        caching_rights="MIT covers client code only; grants zero rights to EA data",
        normalized_storage_rights="NONE (cannot grant rights to EA infrastructure or data)",
        retention_rights="NONE",
        internal_analytics_rights="NONE",
        member_data_permissions="NONE",
        player_data_permissions="NONE",
        redistribution_restrictions="MIT applies to code only",
        deletion_requirements="NONE",
        sla="NONE",
        maintenance_status="COMMUNITY_REPO",
        source_provenance="Directly calls https://proclubs.ea.com/api/fc/*",
        ultimately_proxies_ea=True,
        requires_cookies_or_sessions=False,
        requires_browser_or_tls_impersonation=True,
        eligible_for_selection=False,
        rejection_reason="Unofficial client library that calls the blocked proclubs.ea.com endpoint using spoofed Chrome headers; MIT code license does not grant EA data or automated-access rights.",
    ),
    FC27ProviderCandidateRecord(
        provider_name="PROCLUBS_SDK_TYPESCRIPT_CLIENT",
        legal_entity="Alex Jordan (Open-Source GitHub Contributor)",
        category_evaluated="5. Community APIs / Open-Source Client Libraries",
        provider_classification=FC27ProviderCandidateClassification.UNOFFICIAL_CLIENT,
        api_documentation_url="https://github.com/alex-jordan547/proclubs-sdk",
        terms_url="https://github.com/alex-jordan547/proclubs-sdk/blob/main/LICENSE",
        privacy_url="NONE",
        licensing_url="https://github.com/alex-jordan547/proclubs-sdk/blob/main/LICENSE",
        contact_url="https://github.com/alex-jordan547/proclubs-sdk/issues",
        authentication_method="Impit Rust Chrome TLS/JA3/JA4/HTTP2 fingerprint impersonation + Chrome client hints",
        pricing="MIT Open-Source Client Library (not a data provider)",
        trial_availability="N/A",
        supported_fc_versions=("FC27",),
        supported_platforms=("common-gen5", "nx"),
        club_search_support=True,
        club_id_lookup_support=True,
        club_statistics_support=True,
        recent_matches_support=True,
        match_history_depth="Calls blocked proclubs.ea.com endpoints directly via Impit",
        pagination_support="NONE",
        member_statistics_support=True,
        player_performance_statistics_support=True,
        career_statistics_support=True,
        playoff_data_support=True,
        rate_limits="NONE",
        retry_guidance="Exponential backoff on transport errors",
        caching_rights="MIT covers client code only; grants zero rights to EA data",
        normalized_storage_rights="NONE",
        retention_rights="NONE",
        internal_analytics_rights="NONE",
        member_data_permissions="NONE",
        player_data_permissions="NONE",
        redistribution_restrictions="MIT applies to code only",
        deletion_requirements="NONE",
        sla="NONE",
        maintenance_status="COMMUNITY_REPO",
        source_provenance="Directly calls https://proclubs.ea.com/api/fc/* via Impit TLS impersonation",
        ultimately_proxies_ea=True,
        requires_cookies_or_sessions=False,
        requires_browser_or_tls_impersonation=True,
        eligible_for_selection=False,
        rejection_reason="Unofficial client library requiring Impit Chrome TLS/HTTP2 fingerprint impersonation to bypass EA's HTTP 403.",
    ),
    FC27ProviderCandidateRecord(
        provider_name="PROCLUBSHEAD_AND_TRACKER_WEB_SITES",
        legal_entity="Third-Party Gaming Stat Websites (proclubshead.com / tracker.gg)",
        category_evaluated="6. Data providers / third-party stat sites",
        provider_classification=FC27ProviderCandidateClassification.UNVERIFIED_RELAY,
        api_documentation_url="NONE_PUBLICLY_LICENSED_FOR_FC27_CLUBS",
        terms_url="https://tracker.gg/terms",
        privacy_url="https://tracker.gg/privacy",
        licensing_url="NONE",
        contact_url="https://tracker.gg/",
        authentication_method="Unverified web scraping or undocumented internal web calls",
        pricing="UNAVAILABLE_FOR_FC27_CLUBS_B2B_STORAGE",
        trial_availability="NO",
        supported_fc_versions=("Partial / unverified web views",),
        supported_platforms=("common-gen5",),
        club_search_support=True,
        club_id_lookup_support=True,
        club_statistics_support=True,
        recent_matches_support=False,
        match_history_depth="Partial web display only",
        pagination_support="NONE",
        member_statistics_support=True,
        player_performance_statistics_support=False,
        career_statistics_support=True,
        playoff_data_support=False,
        rate_limits="Automated scraping prohibited by site terms",
        retry_guidance="NONE",
        caching_rights="PROHIBITED_BY_SITE_TERMS",
        normalized_storage_rights="PROHIBITED_BY_SITE_TERMS",
        retention_rights="PROHIBITED_BY_SITE_TERMS",
        internal_analytics_rights="PROHIBITED_BY_SITE_TERMS",
        member_data_permissions="UNVERIFIED",
        player_data_permissions="UNVERIFIED",
        redistribution_restrictions="Scraping and database storage prohibited",
        deletion_requirements="N/A",
        sla="NONE",
        maintenance_status="THIRD_PARTY_WEBSITE",
        source_provenance="Relays proclubs.ea.com without downstream sublicensing rights",
        ultimately_proxies_ea=True,
        requires_cookies_or_sessions=False,
        requires_browser_or_tls_impersonation=True,
        eligible_for_selection=False,
        rejection_reason="Unverified relay / consumer website that proxies EA endpoints and prohibits automated API extraction and 730-day database retention.",
    ),
    FC27ProviderCandidateRecord(
        provider_name="COMMUNITY_HOSTED_PROCLUBS_PROXY_RELAYS",
        legal_entity="Unverified Community Proxy Hosts",
        category_evaluated="6. Community APIs / hosted proxy wrappers",
        provider_classification=FC27ProviderCandidateClassification.UNVERIFIED_RELAY,
        api_documentation_url="NONE_VERIFIED",
        terms_url="NONE",
        privacy_url="NONE",
        licensing_url="NONE",
        contact_url="NONE",
        authentication_method="None or unverified relay token",
        pricing="Unknown / unverified",
        trial_availability="NO",
        supported_fc_versions=("FC27",),
        supported_platforms=("common-gen5",),
        club_search_support=True,
        club_id_lookup_support=True,
        club_statistics_support=True,
        recent_matches_support=True,
        match_history_depth="Proxies proclubs.ea.com",
        pagination_support="NONE",
        member_statistics_support=True,
        player_performance_statistics_support=True,
        career_statistics_support=True,
        playoff_data_support=True,
        rate_limits="UNVERIFIED",
        retry_guidance="NONE",
        caching_rights="UNVERIFIED (no EA sublicense)",
        normalized_storage_rights="UNVERIFIED (no EA sublicense)",
        retention_rights="UNVERIFIED",
        internal_analytics_rights="UNVERIFIED",
        member_data_permissions="UNVERIFIED",
        player_data_permissions="UNVERIFIED",
        redistribution_restrictions="No authority to grant EA data licenses",
        deletion_requirements="NONE",
        sla="NONE",
        maintenance_status="UNVERIFIED",
        source_provenance="Proxies blocked proclubs.ea.com endpoints via server-side TLS impersonation",
        ultimately_proxies_ea=True,
        requires_cookies_or_sessions=False,
        requires_browser_or_tls_impersonation=True,
        eligible_for_selection=False,
        rejection_reason="Hosted proxy relays bypass EA's HTTP 403 on behalf of callers without EA authorization or data-licensing authority.",
    ),
    FC27ProviderCandidateRecord(
        provider_name="PUBLIC_STATIC_EAFC_RATINGS_DATASETS",
        legal_entity="Community Open-Data Contributors (Kaggle / Open Science)",
        category_evaluated="7. Public datasets with explicit reuse licenses",
        provider_classification=FC27ProviderCandidateClassification.NOT_ACCEPTABLE,
        api_documentation_url="https://www.kaggle.com/datasets",
        terms_url="https://www.kaggle.com/terms",
        privacy_url="https://www.kaggle.com/privacy",
        licensing_url="https://creativecommons.org/licenses/by/4.0/",
        contact_url="https://www.kaggle.com/",
        authentication_method="Kaggle API key",
        pricing="FREE",
        trial_availability="YES",
        supported_fc_versions=("Static Career Mode / FUT offline player attributes",),
        supported_platforms=("Offline static CSV/JSON",),
        club_search_support=False,
        club_id_lookup_support=False,
        club_statistics_support=False,
        recent_matches_support=False,
        match_history_depth="0 Pro Clubs matches",
        pagination_support="N/A",
        member_statistics_support=False,
        player_performance_statistics_support=False,
        career_statistics_support=False,
        playoff_data_support=False,
        rate_limits="Static download",
        retry_guidance="N/A",
        caching_rights="Permitted for static offline ratings",
        normalized_storage_rights="Permitted for static offline ratings",
        retention_rights="Permitted for static offline ratings",
        internal_analytics_rights="Permitted for static offline ratings",
        member_data_permissions="N/A (no Clubs member data)",
        player_data_permissions="N/A (no Clubs match data)",
        redistribution_restrictions="Attribution required",
        deletion_requirements="None",
        sla="NONE",
        maintenance_status="STATIC_ARCHIVE",
        source_provenance="Static single-player database dumps",
        ultimately_proxies_ea=False,
        requires_cookies_or_sessions=False,
        requires_browser_or_tls_impersonation=False,
        eligible_for_selection=False,
        rejection_reason="Contains static offline player ratings only; zero East Coasters (Club ID 330323) Clubs identity, match, or member data.",
    ),
)


class FC27ProviderSelectionDecision(BaseModel):
    """Part H: Deterministic provider selection gate summary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    providers_reviewed: int
    official_authorized_providers_found: int
    licensed_third_party_providers_found: int
    community_documented_providers_found: int
    unofficial_clients_rejected: int
    unverified_relays_rejected: int
    not_acceptable_rejected: int
    selected_provider: Literal["NONE"] = "NONE"
    selected_provider_classification: Literal["NONE"] = "NONE"
    live_provider_implementation: Literal["NOT_STARTED"] = "NOT_STARTED"
    blocker: Literal["NO_AUTHORIZED_API_SOURCE_FOUND"] = "NO_AUTHORIZED_API_SOURCE_FOUND"


def evaluate_provider_selection_gate(
    candidates: Sequence[FC27ProviderCandidateRecord] = REVIEWED_API_SOURCE_CANDIDATES,
) -> FC27ProviderSelectionDecision:
    """Evaluates all candidate API sources and fails closed if none satisfy all Part H requirements."""
    official_ok = sum(
        1
        for c in candidates
        if c.provider_classification == FC27ProviderCandidateClassification.OFFICIAL_AUTHORIZED
        and c.eligible_for_selection
    )
    licensed_ok = sum(
        1
        for c in candidates
        if c.provider_classification == FC27ProviderCandidateClassification.LICENSED_THIRD_PARTY
        and c.eligible_for_selection
    )
    community_ok = sum(
        1
        for c in candidates
        if c.provider_classification == FC27ProviderCandidateClassification.COMMUNITY_DOCUMENTED
        and c.eligible_for_selection
    )
    unofficial_rej = sum(
        1
        for c in candidates
        if c.provider_classification == FC27ProviderCandidateClassification.UNOFFICIAL_CLIENT
    )
    relay_rej = sum(
        1
        for c in candidates
        if c.provider_classification == FC27ProviderCandidateClassification.UNVERIFIED_RELAY
    )
    not_acc_rej = sum(
        1
        for c in candidates
        if c.provider_classification == FC27ProviderCandidateClassification.NOT_ACCEPTABLE
    )
    return FC27ProviderSelectionDecision(
        providers_reviewed=len(candidates),
        official_authorized_providers_found=official_ok,
        licensed_third_party_providers_found=licensed_ok,
        community_documented_providers_found=community_ok,
        unofficial_clients_rejected=unofficial_rej,
        unverified_relays_rejected=relay_rej,
        not_acceptable_rejected=not_acc_rej,
    )


# ==============================================================================
# Part C: EA Permission and Data-Portability Request Draft (Not Sent)
# ==============================================================================

class FC27EAAccessRequestDraft(BaseModel):
    """Structured EA permission and data-portability request draft (never transmitted automatically)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ea_permission_request_ready: Literal[True] = True
    ea_access_request_ready: Literal[True] = True
    ea_permission_request_sent: Literal[False] = False
    ea_access_request_sent: Literal[False] = False
    club_name: Literal["East Coasters"] = EAST_COASTERS_CLUB_NAME
    club_id: Literal["330323"] = EAST_COASTERS_CLUB_ID
    platform: Literal["common-gen5"] = "common-gen5"
    game_version: Literal["FC27"] = "FC27"
    attestation_reference: Literal["api-east-coasters-authority-2026-10"] = "api-east-coasters-authority-2026-10"
    operator_club_authority: Literal["USER_ATTESTED"] = "USER_ATTESTED"
    member_consent_status: Literal["USER_ATTESTED"] = "USER_ATTESTED"
    captain_consent_status: Literal["USER_ATTESTED"] = "USER_ATTESTED"
    purpose: str = "Internal non-commercial team performance analysis for East Coasters (Club ID 330323)"
    requested_pathways: Tuple[str, ...] = (
        "1. Official club-data export",
        "2. Approved read-only API access",
        "3. Partner or developer access",
        "4. Written authorization for low-rate automated read-only access",
        "5. Data-portability archive containing structured Clubs records",
    )
    requested_operations: Tuple[str, ...] = (
        "CLUB_IDENTITY",
        "CLUB_STATISTICS",
        "RECENT_MATCHES",
        "MATCH_TEAM_STATISTICS",
        "MATCH_PLAYER_STATISTICS",
        "CURRENT_MEMBER_STATISTICS",
        "MEMBER_CAREER_STATISTICS",
        "PLAYOFF_ACHIEVEMENTS",
    )
    proposed_request_frequency: str = (
        "Read-only, manual operator-initiated post-session retrieval; initially 1 request per manually "
        "authorized operation, strictly capped at <= 5 requests/minute and <= 6 requests/session (zero continuous polling)."
    )
    security_and_privacy_controls: Tuple[str, ...] = (
        "Read-only HTTPS GET requests with standard application User-Agent or EA-issued API key",
        "No cookie or session reuse",
        "No EA credential extraction or storage",
        "No browser automation or TLS fingerprint impersonation (no Impit/Chromium)",
        "Normalized records only; zero raw HTTP response retention",
        "Deterministic pseudonymous player references (plr_ecf_<hash12>); gamer tags and EA/Persona/Nucleus IDs suppressed",
        "No public player-data publication and no commercial resale",
        "730-day maximum normalized-data retention window",
        "Immediate deletion and correction procedure upon member consent revocation or EA request",
        "Least-privilege PostgreSQL 16 storage (Grants Policy v14) with recommendations and dispatch disabled",
    )
    contact_placeholder: str = "[OPERATOR_CONTACT_PLACEHOLDER]"


EA_APPROVED_ACCESS_REQUEST_DRAFT = FC27EAAccessRequestDraft()


# ==============================================================================
# Part D: Provider-Neutral Declaration & Protocol
# ==============================================================================

ALLOWED_PROVIDER_PURPOSES: frozenset[str] = frozenset(
    {
        "INTERNAL_TEAM_ANALYTICS",
        "TEAM_REVIEW",
    }
)


class FC27AuthorizedProviderDeclaration(BaseModel):
    """Mandatory declaration required before any FC 27 API provider can be instantiated.

    Fails closed when any required declaration, version, scope, or permission is missing,
    expired, or outside the approved East Coasters boundary.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    provider_id: str = Field(min_length=3, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")
    provider_classification: str
    legal_entity: str = Field(min_length=2, max_length=128)
    documentation_url: str = Field(min_length=10, max_length=256)
    terms_url: str = Field(min_length=10, max_length=256)
    terms_version: str = Field(min_length=1, max_length=64)
    privacy_url: str = Field(min_length=10, max_length=256)
    privacy_policy_version: str = Field(min_length=1, max_length=64)
    license_or_agreement: str = Field(min_length=3, max_length=128)
    authorization_reference: str = Field(min_length=5, max_length=128)
    authorization_expires_at: datetime
    approved_club_ids: Tuple[str, ...]
    approved_platforms: Tuple[str, ...]
    approved_game_versions: Tuple[str, ...]
    approved_operations: Tuple[str, ...]
    approved_purposes: Tuple[str, ...]
    request_limit_per_minute: int = Field(ge=1, le=60)
    max_requests_per_session: int = Field(ge=1, le=20)
    timeout_seconds: float = Field(gt=0.0, le=10.0)
    retry_policy: FC27RetryPolicy = Field(default_factory=FC27RetryPolicy)
    redirect_policy: Literal["REFUSE_ALL"] = "REFUSE_ALL"
    response_size_maximum_bytes: int = Field(ge=1024, le=1_048_576)
    normalized_storage_permission: bool
    retention_permission: bool
    max_retention_days: int = Field(ge=1, le=3650)
    member_data_permission: bool
    player_data_permission: bool
    transport_policy: FC27AuthorizedTransportPolicy

    @field_validator(
        "terms_version",
        "privacy_policy_version",
        "license_or_agreement",
        "authorization_reference",
        "legal_entity",
    )
    @classmethod
    def _non_blank(cls, val: str) -> str:
        stripped = val.strip()
        if not stripped:
            raise ValueError("Required provider declaration string cannot be blank")
        return stripped

    @field_validator("documentation_url", "terms_url", "privacy_url")
    @classmethod
    def _https_url(cls, val: str) -> str:
        stripped = val.strip()
        if not stripped.startswith("https://"):
            raise ValueError(f"Provider legal/documentation URL must use https://: '{stripped}'")
        return stripped

    @model_validator(mode="after")
    def _validate_declaration(self) -> FC27AuthorizedProviderDeclaration:
        if self.provider_classification not in APPROVED_PROVIDER_CLASSIFICATIONS:
            raise FC27ProviderError(
                f"Unsupported or unapproved provider_classification '{self.provider_classification}'. "
                "Only OFFICIAL_AUTHORIZED or LICENSED_THIRD_PARTY providers are permitted."
            )
        if self.authorization_expires_at.tzinfo is None or self.authorization_expires_at.utcoffset() is None:
            raise FC27ProviderError("authorization_expires_at must be a timezone-aware UTC datetime")
        if self.approved_club_ids != (EAST_COASTERS_CLUB_ID,):
            raise FC27ProviderError(
                f"Unapproved club scope {self.approved_club_ids}; only ('{EAST_COASTERS_CLUB_ID}',) is permitted"
            )
        if self.approved_platforms != ("common-gen5",):
            raise FC27ProviderError(
                f"Unapproved platform scope {self.approved_platforms}; only ('common-gen5',) is permitted"
            )
        if self.approved_game_versions != ("FC27",):
            raise FC27ProviderError(
                f"Unapproved game_version scope {self.approved_game_versions}; only ('FC27',) is permitted"
            )
        if not self.approved_operations:
            raise FC27ProviderError("approved_operations cannot be empty")
        for op in self.approved_operations:
            if op not in ALL_AUTHORIZED_OPERATIONS:
                raise FC27ProviderError(f"Unsupported operation '{op}' in approved_operations")
        if not self.approved_purposes:
            raise FC27ProviderError("approved_purposes cannot be empty")
        for purpose in self.approved_purposes:
            if purpose not in ALLOWED_PROVIDER_PURPOSES:
                raise FC27ProviderError(f"Unapproved purpose '{purpose}'")
        if not self.normalized_storage_permission:
            raise FC27ProviderError("normalized_storage_permission=True is required")
        if not self.retention_permission or self.max_retention_days < 730:
            raise FC27ProviderError("retention_permission=True with max_retention_days >= 730 is required")
        if self.transport_policy.provider_id != self.provider_id:
            raise FC27ProviderError("transport_policy.provider_id must match declaration provider_id")
        return self

    def verify_active_for_operation(
        self,
        operation: FC27AuthorizedOperation | str,
        *,
        club_id: str = EAST_COASTERS_CLUB_ID,
        platform: str = "common-gen5",
        game_version: str = "FC27",
        purpose: str = "INTERNAL_TEAM_ANALYTICS",
        reference_now: Optional[datetime] = None,
    ) -> None:
        """Verifies expiration, operation allowlist, club/platform/version/purpose, and member/player permissions."""
        now_utc = (reference_now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if self.authorization_expires_at <= now_utc:
            raise FC27ProviderError("Provider authorization has expired")

        op_str = operation.value if isinstance(operation, FC27AuthorizedOperation) else str(operation)
        if op_str not in ALL_AUTHORIZED_OPERATIONS:
            raise FC27ProviderError(f"Unsupported operation '{op_str}'")
        if op_str not in self.approved_operations:
            raise FC27ProviderError(f"Operation '{op_str}' is not approved for provider '{self.provider_id}'")
        if club_id != EAST_COASTERS_CLUB_ID or club_id not in self.approved_club_ids:
            raise FC27ProviderError(f"Unapproved club_id '{club_id}'")
        if platform != "common-gen5" or platform not in self.approved_platforms:
            raise FC27ProviderError(f"Unapproved platform '{platform}'")
        if game_version != "FC27" or game_version not in self.approved_game_versions:
            raise FC27ProviderError(f"Unapproved game_version '{game_version}'")
        if purpose not in self.approved_purposes or purpose not in ALLOWED_PROVIDER_PURPOSES:
            raise FC27ProviderError(f"Unapproved purpose '{purpose}'")
        if op_str in MEMBER_DATA_OPERATIONS and not self.member_data_permission:
            raise FC27ProviderError(f"Operation '{op_str}' requires member_data_permission=True")
        if op_str in PLAYER_DATA_OPERATIONS and not self.player_data_permission:
            raise FC27ProviderError(f"Operation '{op_str}' requires player_data_permission=True")


# ==============================================================================
# Part F: Normalized Output Contracts & Deterministic Pseudonymization
# ==============================================================================

def pseudonymize_east_coasters_player_ref(raw_identifier: str, *, club_id: str = EAST_COASTERS_CLUB_ID) -> str:
    """Produces a deterministic, non-reversible pseudonymous player reference scoped to East Coasters (330323)."""
    cleaned = str(raw_identifier).strip()
    if not cleaned:
        raise FC27ProviderError("Cannot pseudonymize empty player identifier")
    if re.fullmatch(r"plr_ecf_[0-9a-f]{12}", cleaned):
        return cleaned
    domain_input = f"fc27-ecf-{club_id}-player-v1:{cleaned}".encode("utf-8")
    digest12 = hashlib.sha256(domain_input).hexdigest()[:12]
    return f"plr_ecf_{digest12}"


class FC27NormalizedMemberStatistic(BaseModel):
    """Normalized pseudonymous club member statistics record (zero gamer tags or external IDs)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    local_player_reference: str = Field(pattern=r"^plr_ecf_[0-9a-f]{12}$")
    scope: Literal["CURRENT_CLUB", "CAREER"]
    favorite_position: str
    games_played: int = Field(ge=0, le=50000)
    goals: int = Field(ge=0, le=50000)
    assists: int = Field(ge=0, le=50000)
    clean_sheets: int = Field(default=0, ge=0, le=50000)
    average_rating: Optional[float] = Field(default=None, ge=0.0, le=10.0)
    pass_success_pct: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    tackle_success_pct: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    man_of_the_match: int = Field(default=0, ge=0, le=50000)
    display_alias: None = None


class FC27NormalizedPlayoffAchievement(BaseModel):
    """Normalized club playoff achievement record for East Coasters (330323)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    club_id: Literal["330323"] = EAST_COASTERS_CLUB_ID
    platform: Literal["common-gen5"] = "common-gen5"
    game_version: Literal["FC27"] = "FC27"
    season_id: str = Field(min_length=1, max_length=32)
    season_name: str = Field(min_length=1, max_length=64)
    best_division: int = Field(ge=1, le=10)
    playoff_wins: int = Field(ge=0, le=500)
    playoff_losses: int = Field(ge=0, le=500)
    playoff_draws: int = Field(default=0, ge=0, le=500)
    finishing_position: Optional[int] = Field(default=None, ge=1, le=100000)


class FC27NormalizedOperationEnvelope(BaseModel):
    """Normalized, privacy-checked result returned by FC27AuthorizedProviderAdapter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: str
    operation: str
    source_classification: Literal["AUTHORIZED_CAPTURE"] = "AUTHORIZED_CAPTURE"
    club_id: Literal["330323"] = EAST_COASTERS_CLUB_ID
    club_name: Literal["East Coasters"] = EAST_COASTERS_CLUB_NAME
    platform: Literal["common-gen5"] = "common-gen5"
    game_version: Literal["FC27"] = "FC27"
    payload_sha256: str
    club_identity: Optional[ClubIdentityRecord] = None
    club_statistics: Optional[FC27SuppliedClubStatistics] = None
    matches: Tuple[FC27SuppliedMatch, ...] = ()
    member_statistics: Tuple[FC27NormalizedMemberStatistic, ...] = ()
    playoff_achievements: Tuple[FC27NormalizedPlayoffAchievement, ...] = ()
    schema_drift_paths: Tuple[str, ...] = ()
    quarantine_metadata: Optional[Dict[str, Any]] = None
    transport_log: FC27MetadataTransportLog
    raw_response_retained: Literal[False] = False
    store_display_aliases: Literal[False] = False
    store_gamer_tags: Literal[False] = False
    store_external_player_identifiers: Literal[False] = False
    review_required: Literal[True] = True


@runtime_checkable
class FC27AuthorizedProvider(Protocol):
    """Part D: Provider-neutral Protocol for authorized FC 27 data providers.

    Exposes only bounded, typed operation methods; never exposes get_json(url) or arbitrary URL methods.
    """

    def fetch_club_identity(
        self,
        *,
        club_id: str = EAST_COASTERS_CLUB_ID,
        platform: str = "common-gen5",
        game_version: str = "FC27",
        purpose: str = "INTERNAL_TEAM_ANALYTICS",
    ) -> FC27NormalizedOperationEnvelope: ...

    def fetch_club_statistics(
        self,
        *,
        club_id: str = EAST_COASTERS_CLUB_ID,
        platform: str = "common-gen5",
        game_version: str = "FC27",
        purpose: str = "INTERNAL_TEAM_ANALYTICS",
    ) -> FC27NormalizedOperationEnvelope: ...

    def fetch_recent_matches(
        self,
        *,
        club_id: str = EAST_COASTERS_CLUB_ID,
        platform: str = "common-gen5",
        game_version: str = "FC27",
        match_type: str = "leagueMatch",
        purpose: str = "INTERNAL_TEAM_ANALYTICS",
    ) -> FC27NormalizedOperationEnvelope: ...

    def fetch_match_player_statistics(
        self,
        *,
        club_id: str = EAST_COASTERS_CLUB_ID,
        platform: str = "common-gen5",
        game_version: str = "FC27",
        match_type: str = "leagueMatch",
        purpose: str = "INTERNAL_TEAM_ANALYTICS",
    ) -> FC27NormalizedOperationEnvelope: ...

    def fetch_current_member_statistics(
        self,
        *,
        club_id: str = EAST_COASTERS_CLUB_ID,
        platform: str = "common-gen5",
        game_version: str = "FC27",
        purpose: str = "INTERNAL_TEAM_ANALYTICS",
    ) -> FC27NormalizedOperationEnvelope: ...

    def fetch_member_career_statistics(
        self,
        *,
        club_id: str = EAST_COASTERS_CLUB_ID,
        platform: str = "common-gen5",
        game_version: str = "FC27",
        purpose: str = "INTERNAL_TEAM_ANALYTICS",
    ) -> FC27NormalizedOperationEnvelope: ...

    def fetch_playoff_achievements(
        self,
        *,
        club_id: str = EAST_COASTERS_CLUB_ID,
        platform: str = "common-gen5",
        game_version: str = "FC27",
        purpose: str = "INTERNAL_TEAM_ANALYTICS",
    ) -> FC27NormalizedOperationEnvelope: ...


_GAMER_TAG_SOURCE_KEYS: frozenset[str] = frozenset(
    {
        "proname",
        "gamertag",
        "psnid",
        "xboxid",
        "eaid",
        "membername",
        "playername",
        "displayalias",
        "display_alias",
        "username",
        "handle",
    }
)

_EXTERNAL_ID_SOURCE_KEYS: frozenset[str] = frozenset(
    {
        "playerid",
        "player_id",
        "externalplayerid",
        "external_player_id",
        "personaid",
        "persona_id",
        "nucleusid",
        "nucleus_id",
        "accountid",
        "account_id",
        "memberid",
        "member_id",
    }
)


def _normalize_position_code(raw_pos: Any) -> str:
    pos_str = str(raw_pos or "CM").strip().upper()
    pos_map = {
        "GOALKEEPER": "GK",
        "DEFENDER": "CB",
        "MIDFIELDER": "CM",
        "FORWARD": "ST",
        "STRIKER": "ST",
    }
    mapped = pos_map.get(pos_str, pos_str)
    if mapped not in VALID_FC27_POSITIONS:
        raise FC27TransportError(f"Unsupported FC 27 position '{raw_pos}'")
    return mapped


class FC27AuthorizedProviderAdapter:
    """Provider-neutral adapter implementing FC27AuthorizedProvider over FC27AuthorizedTransportClient."""

    def __init__(
        self,
        *,
        declaration: FC27AuthorizedProviderDeclaration,
        transport_client: FC27AuthorizedTransportClient,
        reference_now: Optional[datetime] = None,
        environ: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._declaration = declaration
        self._transport = transport_client
        self._reference_now = reference_now
        self._environ = environ

    @property
    def declaration(self) -> FC27AuthorizedProviderDeclaration:
        return self._declaration

    @property
    def transport_client(self) -> FC27AuthorizedTransportClient:
        return self._transport

    def fetch_club_identity(
        self,
        *,
        club_id: str = EAST_COASTERS_CLUB_ID,
        platform: str = "common-gen5",
        game_version: str = "FC27",
        purpose: str = "INTERNAL_TEAM_ANALYTICS",
    ) -> FC27NormalizedOperationEnvelope:
        op = FC27AuthorizedOperation.CLUB_IDENTITY
        self._declaration.verify_active_for_operation(
            op,
            club_id=club_id,
            platform=platform,
            game_version=game_version,
            purpose=purpose,
            reference_now=self._reference_now,
        )
        resp = self._transport.execute_operation(
            op,
            club_id=club_id,
            platform=platform,
            game_version=game_version,
            environ=self._environ,
        )
        data = resp.parsed_data
        club_node = data.get(EAST_COASTERS_CLUB_ID, data)
        if not isinstance(club_node, Mapping):
            raise FC27TransportError("Invalid CLUB_IDENTITY payload schema")

        allowed_keys = {"clubId", "club_id", "name", "club_name", "platform", "regionId", "customKit"}
        drift = tuple(sorted(k for k in club_node.keys() if k not in allowed_keys))

        ret_id = str(club_node.get("clubId", club_node.get("club_id", ""))).strip()
        ret_name = str(club_node.get("name", club_node.get("club_name", ""))).strip()
        ret_plat = str(club_node.get("platform", platform)).strip()

        if ret_id != EAST_COASTERS_CLUB_ID or ret_name.lower() != EAST_COASTERS_CLUB_NAME.lower() or ret_plat != "common-gen5":
            raise FC27TransportError(
                f"Club identity mismatch: expected ({EAST_COASTERS_CLUB_ID}, {EAST_COASTERS_CLUB_NAME}, common-gen5)"
            )

        base_identity = create_provisional_club_identity(
            club_id=EAST_COASTERS_CLUB_ID,
            club_name=EAST_COASTERS_CLUB_NAME,
            platform="common-gen5",
        ).model_copy(
            update={
                "provider": self._declaration.provider_id,
                "identity_verification_state": ClubIdentityState.PROVISIONAL,
                "payload_hash": resp.payload_sha256,
                "provenance": f"{self._declaration.provider_id}:{self._declaration.authorization_reference}",
            }
        )
        return self._build_envelope(
            op=op,
            resp=resp,
            drift=drift,
            club_identity=base_identity,
        )

    def fetch_club_statistics(
        self,
        *,
        club_id: str = EAST_COASTERS_CLUB_ID,
        platform: str = "common-gen5",
        game_version: str = "FC27",
        purpose: str = "INTERNAL_TEAM_ANALYTICS",
    ) -> FC27NormalizedOperationEnvelope:
        op = FC27AuthorizedOperation.CLUB_STATISTICS
        self._declaration.verify_active_for_operation(
            op,
            club_id=club_id,
            platform=platform,
            game_version=game_version,
            purpose=purpose,
            reference_now=self._reference_now,
        )
        resp = self._transport.execute_operation(
            op,
            club_id=club_id,
            platform=platform,
            game_version=game_version,
            environ=self._environ,
        )
        data = resp.parsed_data
        stats_node = data.get(EAST_COASTERS_CLUB_ID, data)
        if not isinstance(stats_node, Mapping):
            raise FC27TransportError("Invalid CLUB_STATISTICS payload schema")

        required_keys = {"gamesPlayed", "wins", "ties", "losses", "goals", "goalsAgainst"}
        if not required_keys.issubset(stats_node.keys()):
            raise FC27TransportError("Missing required CLUB_STATISTICS fields")

        allowed_keys = required_keys | {"clubId", "cleanSheets", "bestDivision", "skillRating"}
        drift = tuple(sorted(k for k in stats_node.keys() if k not in allowed_keys))

        try:
            club_stats = FC27SuppliedClubStatistics(
                matches_played=int(stats_node["gamesPlayed"]),
                wins=int(stats_node["wins"]),
                draws=int(stats_node["ties"]),
                losses=int(stats_node["losses"]),
                goals_for=int(stats_node["goals"]),
                goals_against=int(stats_node["goalsAgainst"]),
                clean_sheets=int(stats_node["cleanSheets"]) if "cleanSheets" in stats_node and stats_node["cleanSheets"] is not None else 0,
                skill_rating=int(stats_node["skillRating"]) if "skillRating" in stats_node and stats_node["skillRating"] is not None else None,
                reputation_tier=f"DIV_{int(stats_node['bestDivision'])}" if "bestDivision" in stats_node and stats_node["bestDivision"] is not None else None,
            )
        except Exception as exc:
            raise FC27TransportError(f"CLUB_STATISTICS validation failed: {exc}") from exc

        return self._build_envelope(
            op=op,
            resp=resp,
            drift=drift,
            club_statistics=club_stats,
        )

    def fetch_recent_matches(
        self,
        *,
        club_id: str = EAST_COASTERS_CLUB_ID,
        platform: str = "common-gen5",
        game_version: str = "FC27",
        match_type: str = "leagueMatch",
        purpose: str = "INTERNAL_TEAM_ANALYTICS",
    ) -> FC27NormalizedOperationEnvelope:
        return self._fetch_matches_common(
            op=FC27AuthorizedOperation.RECENT_MATCHES,
            include_player_performances=False,
            club_id=club_id,
            platform=platform,
            game_version=game_version,
            match_type=match_type,
            purpose=purpose,
        )

    def fetch_match_player_statistics(
        self,
        *,
        club_id: str = EAST_COASTERS_CLUB_ID,
        platform: str = "common-gen5",
        game_version: str = "FC27",
        match_type: str = "leagueMatch",
        purpose: str = "INTERNAL_TEAM_ANALYTICS",
    ) -> FC27NormalizedOperationEnvelope:
        return self._fetch_matches_common(
            op=FC27AuthorizedOperation.MATCH_PLAYER_STATISTICS,
            include_player_performances=True,
            club_id=club_id,
            platform=platform,
            game_version=game_version,
            match_type=match_type,
            purpose=purpose,
        )

    def _fetch_matches_common(
        self,
        *,
        op: FC27AuthorizedOperation,
        include_player_performances: bool,
        club_id: str,
        platform: str,
        game_version: str,
        match_type: str,
        purpose: str,
    ) -> FC27NormalizedOperationEnvelope:
        self._declaration.verify_active_for_operation(
            op,
            club_id=club_id,
            platform=platform,
            game_version=game_version,
            purpose=purpose,
            reference_now=self._reference_now,
        )
        resp = self._transport.execute_operation(
            op,
            club_id=club_id,
            platform=platform,
            game_version=game_version,
            query_params={"matchType": match_type},
            environ=self._environ,
        )
        raw_matches = resp.parsed_data.get("matches")
        if not isinstance(raw_matches, list) or not raw_matches:
            raise FC27TransportError("Invalid or empty 'matches' list in provider response")

        allowed_match_keys = {
            "matchId",
            "playedAt",
            "result",
            "goalsFor",
            "goalsAgainst",
            "opponentName",
            "formation",
            "competitionType",
            "possessionPct",
            "shots",
            "shotsOnTarget",
            "passAccuracyPct",
            "tackles",
            "fouls",
            "players",
        }
        allowed_player_keys = {
            "memberKey",
            "localPlayerReference",
            "position",
            "minutesPlayed",
            "goals",
            "assists",
            "rating",
            "passesCompleted",
            "passesAttempted",
            "tacklesWon",
            "tacklesAttempted",
            "interceptions",
            "saves",
        } | _GAMER_TAG_SOURCE_KEYS | _EXTERNAL_ID_SOURCE_KEYS

        drift_set: set[str] = set()
        normalized_matches: List[FC27SuppliedMatch] = []

        for idx, m_item in enumerate(raw_matches):
            if not isinstance(m_item, Mapping):
                raise FC27TransportError(f"Match item at index {idx} is not an object")
            for k in m_item.keys():
                if k not in allowed_match_keys:
                    drift_set.add(f"matches[{idx}].{k}")

            for req_k in ("matchId", "playedAt", "result", "goalsFor", "goalsAgainst"):
                if req_k not in m_item or m_item[req_k] is None:
                    raise FC27TransportError(f"Match item at index {idx} missing required field '{req_k}'")

            perfs: List[FC27SuppliedPlayerPerformance] = []
            if include_player_performances:
                raw_players = m_item.get("players", [])
                if not isinstance(raw_players, list):
                    raise FC27TransportError("Match 'players' field must be a list")
                for p_idx, p_item in enumerate(raw_players):
                    if not isinstance(p_item, Mapping):
                        raise FC27TransportError("Player item must be an object")
                    for pk in p_item.keys():
                        if pk.lower() not in allowed_player_keys and pk not in allowed_player_keys:
                            drift_set.add(f"matches[{idx}].players[{p_idx}].{pk}")

                    raw_ref = (
                        p_item.get("localPlayerReference")
                        or p_item.get("memberKey")
                        or p_item.get("playerKey")
                    )
                    if not raw_ref:
                        raise FC27TransportError("Player performance entry missing pseudonymous source key")
                    pseudo_ref = pseudonymize_east_coasters_player_ref(str(raw_ref), club_id=EAST_COASTERS_CLUB_ID)

                    try:
                        p_att = int(p_item["passesAttempted"]) if p_item.get("passesAttempted") is not None else None
                        p_cmp = int(p_item["passesCompleted"]) if p_item.get("passesCompleted") is not None else None
                        t_att = int(p_item["tacklesAttempted"]) if p_item.get("tacklesAttempted") is not None else None
                        t_won = int(p_item["tacklesWon"]) if p_item.get("tacklesWon") is not None else None
                        s_tot = int(p_item["shots"]) if p_item.get("shots") is not None else None
                        s_on = int(p_item["shotsOnTarget"]) if p_item.get("shotsOnTarget") is not None else None
                        perf = FC27SuppliedPlayerPerformance(
                            local_player_reference=pseudo_ref,
                            display_alias=None,
                            position=_normalize_position_code(p_item.get("position", "CM")),
                            minutes_played=int(p_item.get("minutesPlayed", 90)),
                            goals=int(p_item.get("goals", 0)),
                            assists=int(p_item.get("assists", 0)),
                            provider_rating=float(p_item["rating"]) if p_item.get("rating") is not None else None,
                            passes_completed=p_cmp,
                            passes_attempted=p_att,
                            tackles_won=t_won,
                            tackles_attempted=t_att,
                            shots=s_tot,
                            shots_on_target=s_on,
                            interceptions=int(p_item["interceptions"]) if p_item.get("interceptions") is not None else None,
                            saves=int(p_item["saves"]) if p_item.get("saves") is not None else None,
                        )
                    except Exception as exc:
                        raise FC27TransportError(f"Player performance validation failed: {exc}") from exc
                    perfs.append(perf)

            raw_comp = str(m_item.get("competitionType", match_type)).upper()
            if "PLAYOFF" in raw_comp:
                mapped_type: Literal["LEAGUE", "PLAYOFF", "FRIENDLY", "CUP", "PRACTICE", "UNKNOWN"] = "PLAYOFF"
            elif "FRIENDLY" in raw_comp:
                mapped_type = "FRIENDLY"
            elif "LEAGUE" in raw_comp:
                mapped_type = "LEAGUE"
            else:
                mapped_type = "UNKNOWN"

            try:
                match_obj = FC27SuppliedMatch(
                    external_match_id=str(m_item["matchId"]),
                    played_at=m_item["playedAt"],
                    match_type=mapped_type,
                    result=str(m_item["result"]).upper(),  # type: ignore[arg-type]
                    goals_for=int(m_item["goalsFor"]),
                    goals_against=int(m_item["goalsAgainst"]),
                    opponent_name=str(m_item.get("opponentName") or "Unknown Opponent"),
                    formation=str(m_item["formation"]) if m_item.get("formation") is not None else "UNKNOWN",
                    player_performances=perfs,
                    data_quality_status="PROVISIONAL",
                )
            except Exception as exc:
                raise FC27TransportError(f"Match normalization failed: {exc}") from exc
            normalized_matches.append(match_obj)

        return self._build_envelope(
            op=op,
            resp=resp,
            drift=tuple(sorted(drift_set)),
            matches=tuple(normalized_matches),
        )

    def fetch_current_member_statistics(
        self,
        *,
        club_id: str = EAST_COASTERS_CLUB_ID,
        platform: str = "common-gen5",
        game_version: str = "FC27",
        purpose: str = "INTERNAL_TEAM_ANALYTICS",
    ) -> FC27NormalizedOperationEnvelope:
        return self._fetch_members_common(
            op=FC27AuthorizedOperation.CURRENT_MEMBER_STATISTICS,
            scope="CURRENT_CLUB",
            club_id=club_id,
            platform=platform,
            game_version=game_version,
            purpose=purpose,
        )

    def fetch_member_career_statistics(
        self,
        *,
        club_id: str = EAST_COASTERS_CLUB_ID,
        platform: str = "common-gen5",
        game_version: str = "FC27",
        purpose: str = "INTERNAL_TEAM_ANALYTICS",
    ) -> FC27NormalizedOperationEnvelope:
        return self._fetch_members_common(
            op=FC27AuthorizedOperation.MEMBER_CAREER_STATISTICS,
            scope="CAREER",
            club_id=club_id,
            platform=platform,
            game_version=game_version,
            purpose=purpose,
        )

    def _fetch_members_common(
        self,
        *,
        op: FC27AuthorizedOperation,
        scope: Literal["CURRENT_CLUB", "CAREER"],
        club_id: str,
        platform: str,
        game_version: str,
        purpose: str,
    ) -> FC27NormalizedOperationEnvelope:
        self._declaration.verify_active_for_operation(
            op,
            club_id=club_id,
            platform=platform,
            game_version=game_version,
            purpose=purpose,
            reference_now=self._reference_now,
        )
        resp = self._transport.execute_operation(
            op,
            club_id=club_id,
            platform=platform,
            game_version=game_version,
            environ=self._environ,
        )
        raw_members = resp.parsed_data.get("members")
        if not isinstance(raw_members, list) or not raw_members:
            raise FC27TransportError("Invalid or empty 'members' list in provider response")

        allowed_member_keys = {
            "memberKey",
            "localPlayerReference",
            "favoritePosition",
            "gamesPlayed",
            "goals",
            "assists",
            "cleanSheets",
            "averageRating",
            "passSuccessPct",
            "tackleSuccessPct",
            "manOfTheMatch",
        } | _GAMER_TAG_SOURCE_KEYS | _EXTERNAL_ID_SOURCE_KEYS

        drift_set: set[str] = set()
        members_out: List[FC27NormalizedMemberStatistic] = []
        for idx, item in enumerate(raw_members):
            if not isinstance(item, Mapping):
                raise FC27TransportError("Member item must be an object")
            for k in item.keys():
                if k not in allowed_member_keys and k.lower() not in allowed_member_keys:
                    drift_set.add(f"members[{idx}].{k}")

            raw_ref = item.get("localPlayerReference") or item.get("memberKey")
            if not raw_ref:
                raise FC27TransportError("Member entry missing pseudonymous source key")
            pseudo_ref = pseudonymize_east_coasters_player_ref(str(raw_ref), club_id=EAST_COASTERS_CLUB_ID)

            try:
                mem = FC27NormalizedMemberStatistic(
                    local_player_reference=pseudo_ref,
                    scope=scope,
                    favorite_position=_normalize_position_code(item.get("favoritePosition", "CM")),
                    games_played=int(item["gamesPlayed"]),
                    goals=int(item["goals"]),
                    assists=int(item["assists"]),
                    clean_sheets=int(item.get("cleanSheets", 0)),
                    average_rating=float(item["averageRating"]) if item.get("averageRating") is not None else None,
                    pass_success_pct=float(item["passSuccessPct"]) if item.get("passSuccessPct") is not None else None,
                    tackle_success_pct=float(item["tackleSuccessPct"]) if item.get("tackleSuccessPct") is not None else None,
                    man_of_the_match=int(item.get("manOfTheMatch", 0)),
                    display_alias=None,
                )
            except Exception as exc:
                raise FC27TransportError(f"Member statistics normalization failed: {exc}") from exc
            members_out.append(mem)

        return self._build_envelope(
            op=op,
            resp=resp,
            drift=tuple(sorted(drift_set)),
            member_statistics=tuple(members_out),
        )

    def fetch_playoff_achievements(
        self,
        *,
        club_id: str = EAST_COASTERS_CLUB_ID,
        platform: str = "common-gen5",
        game_version: str = "FC27",
        purpose: str = "INTERNAL_TEAM_ANALYTICS",
    ) -> FC27NormalizedOperationEnvelope:
        op = FC27AuthorizedOperation.PLAYOFF_ACHIEVEMENTS
        self._declaration.verify_active_for_operation(
            op,
            club_id=club_id,
            platform=platform,
            game_version=game_version,
            purpose=purpose,
            reference_now=self._reference_now,
        )
        resp = self._transport.execute_operation(
            op,
            club_id=club_id,
            platform=platform,
            game_version=game_version,
            environ=self._environ,
        )
        raw_achievements = resp.parsed_data.get("achievements")
        if not isinstance(raw_achievements, list):
            raise FC27TransportError("Invalid 'achievements' list in PLAYOFF_ACHIEVEMENTS response")

        allowed_keys = {
            "seasonId",
            "seasonName",
            "bestDivision",
            "playoffWins",
            "playoffLosses",
            "playoffDraws",
            "finishingPosition",
        }
        drift_set: set[str] = set()
        achievements_out: List[FC27NormalizedPlayoffAchievement] = []
        for idx, item in enumerate(raw_achievements):
            if not isinstance(item, Mapping):
                raise FC27TransportError("Playoff achievement item must be an object")
            for k in item.keys():
                if k not in allowed_keys:
                    drift_set.add(f"achievements[{idx}].{k}")
            try:
                ach = FC27NormalizedPlayoffAchievement(
                    season_id=str(item["seasonId"]),
                    season_name=str(item["seasonName"]),
                    best_division=int(item["bestDivision"]),
                    playoff_wins=int(item["playoffWins"]),
                    playoff_losses=int(item["playoffLosses"]),
                    playoff_draws=int(item.get("playoffDraws", 0)),
                    finishing_position=int(item["finishingPosition"]) if item.get("finishingPosition") is not None else None,
                )
            except Exception as exc:
                raise FC27TransportError(f"Playoff achievement normalization failed: {exc}") from exc
            achievements_out.append(ach)

        return self._build_envelope(
            op=op,
            resp=resp,
            drift=tuple(sorted(drift_set)),
            playoff_achievements=tuple(achievements_out),
        )

    def build_export_envelope_and_plan(
        self,
        *,
        export_id: str,
        matches_envelope: FC27NormalizedOperationEnvelope,
        club_statistics: Optional[FC27SuppliedClubStatistics] = None,
        existing_state: Optional[FC27ImportState] = None,
        generated_at: Optional[datetime] = None,
    ) -> Tuple[FC27UserExportEnvelope, FC27UserExportImportPlan]:
        """Converts normalized match records into an FC27UserExportEnvelope and plans idempotent import."""
        if not matches_envelope.matches:
            raise FC27ProviderError("Cannot build export envelope from empty matches tuple")
        gen_dt = (generated_at or datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)).astimezone(timezone.utc)

        clean_matches = [
            m.model_copy(
                update={
                    "player_performances": [
                        p.model_copy(update={"display_alias": None}) for p in m.player_performances
                    ]
                }
            )
            for m in matches_envelope.matches
        ]
        env_dict: Dict[str, Any] = {
            "export_id": export_id,
            "contract_version": "1.0.0",
            "schema_version": "1.0.0",
            "generated_at": gen_dt,
            "supplied_at": gen_dt,
            "provider": self._declaration.provider_id,
            "game_version": "FC27",
            "platform": "common-gen5",
            "club": FC27SuppliedClubIdentity(
                club_id=EAST_COASTERS_CLUB_ID,
                club_name=EAST_COASTERS_CLUB_NAME,
                platform="common-gen5",
                game_version="FC27",
                supplied_identity_state=ClubIdentityState.PROVISIONAL,
                source_reference=self._declaration.authorization_reference,
            ),
            "supplier_attestation": FC27SupplierAttestation(
                supplier_role="CLUB_MANAGER",
                supplier_reference=self._declaration.authorization_reference,
                attested_at=gen_dt,
                authority_scope=[
                    "CLUB_IDENTITY",
                    "MATCH_RESULTS",
                    "LINEUPS",
                    "PLAYER_PERFORMANCE",
                    "BUILD_REFERENCES",
                ],
                statement_version="1.0.0",
            ),
            "consent_attestation": FC27ConsentAttestation(
                consent_version="1.0.0",
                consented_at=gen_dt,
                data_categories=[
                    "CLUB_IDENTITY",
                    "MATCH_RESULTS",
                    "LINEUPS",
                    "PLAYER_PERFORMANCE",
                    "BUILD_REFERENCES",
                ],
                retention_acknowledged=True,
                analytics_use_acknowledged=True,
                team_review_acknowledged=True,
            ),
            "club_statistics": club_statistics,
            "matches": clean_matches,
            "source_classification": "USER_SUPPLIED_EXPORT",
        }
        temp_hash_dict = {
            **env_dict,
            "club": env_dict["club"].model_dump(),
            "supplier_attestation": env_dict["supplier_attestation"].model_dump(),
            "consent_attestation": env_dict["consent_attestation"].model_dump(),
            "club_statistics": club_statistics.model_dump() if club_statistics is not None else None,
            "matches": [m.model_dump() for m in clean_matches],
            "payload_sha256": "0" * 64,
        }
        digest = compute_payload_sha256(temp_hash_dict)
        envelope = FC27UserExportEnvelope(**env_dict, payload_sha256=digest)
        plan = plan_fc27_user_export_import(envelope, existing_state=existing_state, evaluation_time=gen_dt)
        return envelope, plan

    def _build_envelope(
        self,
        *,
        op: FC27AuthorizedOperation,
        resp: Any,
        drift: Tuple[str, ...],
        club_identity: Optional[ClubIdentityRecord] = None,
        club_statistics: Optional[FC27SuppliedClubStatistics] = None,
        matches: Tuple[FC27SuppliedMatch, ...] = (),
        member_statistics: Tuple[FC27NormalizedMemberStatistic, ...] = (),
        playoff_achievements: Tuple[FC27NormalizedPlayoffAchievement, ...] = (),
    ) -> FC27NormalizedOperationEnvelope:
        quarantine_meta: Optional[Dict[str, Any]] = None
        if drift:
            quarantine_meta = {
                "club_id": EAST_COASTERS_CLUB_ID,
                "platform": "common-gen5",
                "game_version": "FC27",
                "source_classification": "AUTHORIZED_CAPTURE",
                "anomaly_category": "SCHEMA_DRIFT",
                "reason_code": "UPSTREAM_SCHEMA_DRIFT_OBSERVED",
                "diagnostic_paths": [re.sub(r"[^a-zA-Z0-9_.\[\]-]", "_", p)[:128] for p in drift[:100]],
                "payload_sha256": resp.payload_sha256,
                "raw_body_stored": False,
            }
        return FC27NormalizedOperationEnvelope(
            provider_id=self._declaration.provider_id,
            operation=op.value,
            payload_sha256=resp.payload_sha256,
            club_identity=club_identity,
            club_statistics=club_statistics,
            matches=matches,
            member_statistics=member_statistics,
            playoff_achievements=playoff_achievements,
            schema_drift_paths=drift,
            quarantine_metadata=quarantine_meta,
            transport_log=resp.metadata_log,
            raw_response_retained=False,
            store_display_aliases=False,
            store_gamer_tags=False,
            store_external_player_identifiers=False,
            review_required=True,
        )


def verify_default_runtime_safety_posture() -> Dict[str, bool]:
    """Confirms all runtime flags remain disabled by default."""
    return {
        "FC27_PROVIDER_ENABLED": is_fc27_provider_enabled(),
        "FC27_LIVE_INGESTION_ENABLED": is_fc27_live_ingestion_enabled(),
        "FC27_PERSISTENCE_ENABLED": is_fc27_persistence_enabled(),
        "FC27_ANALYTICS_ENABLED": is_fc27_analytics_enabled(),
        "FC27_RECOMMENDATIONS_ENABLED": is_fc27_recommendations_enabled(),
        "DISPATCH_ENABLED": is_dispatch_enabled(),
        "DISPATCH_WORKER_ENABLED": is_dispatch_worker_enabled(),
    }


__all__ = [
    "ALL_AUTHORIZED_OPERATIONS",
    "ALLOWED_PROVIDER_PURPOSES",
    "APPROVED_PROVIDER_CLASSIFICATIONS",
    "EA_APPROVED_ACCESS_REQUEST_DRAFT",
    "FC27AuthorizedCredentials",
    "FC27AuthorizedOperation",
    "FC27AuthorizedProvider",
    "FC27AuthorizedProviderAdapter",
    "FC27AuthorizedProviderDeclaration",
    "FC27AuthorizedTransportClient",
    "FC27AuthorizedTransportPolicy",
    "FC27EAAccessRequestDraft",
    "FC27MetadataTransportLog",
    "FC27NormalizedMemberStatistic",
    "FC27NormalizedOperationEnvelope",
    "FC27NormalizedPlayoffAchievement",
    "FC27ProviderCandidateClassification",
    "FC27ProviderCandidateRecord",
    "FC27ProviderSelectionDecision",
    "FC27RetryPolicy",
    "REVIEWED_API_SOURCE_CANDIDATES",
    "compute_match_hash",
    "compute_payload_sha256",
    "evaluate_provider_selection_gate",
    "pseudonymize_east_coasters_player_ref",
    "verify_default_runtime_safety_posture",
]
