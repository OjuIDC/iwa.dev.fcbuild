"""Authorized FC 27 API Adapter and Bounded Transport Integration Facade.

Re-exports the provider-neutral adapter, transport client, rate-budget tracker,
and redaction utilities from src.fc27_authorized_provider and src.fc27_authorized_transport.
"""
from __future__ import annotations

from src.fc27_authorized_provider import (
    ALL_AUTHORIZED_OPERATIONS,
    EA_APPROVED_ACCESS_REQUEST_DRAFT,
    REVIEWED_API_SOURCE_CANDIDATES,
    FC27AuthorizedOperation,
    FC27AuthorizedProvider,
    FC27AuthorizedProviderAdapter,
    FC27AuthorizedProviderDeclaration,
    FC27NormalizedMemberStatistic,
    FC27NormalizedOperationEnvelope,
    FC27NormalizedPlayoffAchievement,
    FC27ProviderCandidateClassification,
    FC27ProviderCandidateRecord,
    FC27ProviderSelectionDecision,
    FC27SourceProvenance,
    evaluate_provider_selection_gate,
    pseudonymize_east_coasters_player_ref,
    verify_default_runtime_safety_posture,
)
from src.fc27_authorized_transport import (
    FC27AuthMethod,
    FC27AuthorizedCredentials,
    FC27AuthorizedTransportClient,
    FC27AuthorizedTransportPolicy,
    FC27MetadataTransportLog,
    FC27RateBudgetTracker,
    FC27RetryPolicy,
    FC27TransportParsedResponse,
    parse_json_strict_no_duplicates,
    redact_sensitive_auth_text,
)

__all__ = [
    "ALL_AUTHORIZED_OPERATIONS",
    "EA_APPROVED_ACCESS_REQUEST_DRAFT",
    "FC27AuthMethod",
    "FC27AuthorizedCredentials",
    "FC27AuthorizedOperation",
    "FC27AuthorizedProvider",
    "FC27AuthorizedProviderAdapter",
    "FC27AuthorizedProviderDeclaration",
    "FC27AuthorizedTransportClient",
    "FC27AuthorizedTransportPolicy",
    "FC27MetadataTransportLog",
    "FC27NormalizedMemberStatistic",
    "FC27NormalizedOperationEnvelope",
    "FC27NormalizedPlayoffAchievement",
    "FC27ProviderCandidateClassification",
    "FC27ProviderCandidateRecord",
    "FC27ProviderSelectionDecision",
    "FC27RateBudgetTracker",
    "FC27RetryPolicy",
    "FC27SourceProvenance",
    "FC27TransportParsedResponse",
    "REVIEWED_API_SOURCE_CANDIDATES",
    "evaluate_provider_selection_gate",
    "parse_json_strict_no_duplicates",
    "pseudonymize_east_coasters_player_ref",
    "redact_sensitive_auth_text",
    "verify_default_runtime_safety_posture",
]
