"""Provider-Neutral Contract Facade for Authorized FC 27 Data Providers.

Re-exports the authoritative provider declarations, operation enums, candidate records,
and protocol definitions from src.fc27_authorized_provider and src.fc27_authorized_transport.
"""
from __future__ import annotations

from src.fc27_authorized_provider import (
    ALL_AUTHORIZED_OPERATIONS,
    ALLOWED_PROVIDER_PURPOSES,
    APPROVED_PROVIDER_CLASSIFICATIONS,
    EA_APPROVED_ACCESS_REQUEST_DRAFT,
    REVIEWED_API_SOURCE_CANDIDATES,
    FC27AuthorizedOperation,
    FC27AuthorizedProvider,
    FC27AuthorizedProviderDeclaration,
    FC27EAAccessRequestDraft,
    FC27NormalizedMemberStatistic,
    FC27NormalizedOperationEnvelope,
    FC27NormalizedPlayoffAchievement,
    FC27ProviderCandidateClassification,
    FC27ProviderCandidateRecord,
    FC27ProviderSelectionDecision,
    FC27SourceProvenance,
    evaluate_provider_selection_gate,
    pseudonymize_east_coasters_player_ref,
)
from src.fc27_authorized_transport import (
    MEMBER_DATA_OPERATIONS,
    PERMITTED_AUTH_METHODS,
    PLAYER_DATA_OPERATIONS,
    PROHIBITED_HEADER_NAMES,
    STANDARD_AUTHORIZED_USER_AGENT,
    FC27AuthMethod,
    FC27AuthorizedCredentials,
    FC27AuthorizedTransportPolicy,
    FC27RetryPolicy,
)

__all__ = [
    "ALL_AUTHORIZED_OPERATIONS",
    "ALLOWED_PROVIDER_PURPOSES",
    "APPROVED_PROVIDER_CLASSIFICATIONS",
    "EA_APPROVED_ACCESS_REQUEST_DRAFT",
    "FC27AuthMethod",
    "FC27AuthorizedCredentials",
    "FC27AuthorizedOperation",
    "FC27AuthorizedProvider",
    "FC27AuthorizedProviderDeclaration",
    "FC27AuthorizedTransportPolicy",
    "FC27EAAccessRequestDraft",
    "FC27NormalizedMemberStatistic",
    "FC27NormalizedOperationEnvelope",
    "FC27NormalizedPlayoffAchievement",
    "FC27ProviderCandidateClassification",
    "FC27ProviderCandidateRecord",
    "FC27ProviderSelectionDecision",
    "FC27RetryPolicy",
    "FC27SourceProvenance",
    "MEMBER_DATA_OPERATIONS",
    "PERMITTED_AUTH_METHODS",
    "PLAYER_DATA_OPERATIONS",
    "PROHIBITED_HEADER_NAMES",
    "REVIEWED_API_SOURCE_CANDIDATES",
    "STANDARD_AUTHORIZED_USER_AGENT",
    "evaluate_provider_selection_gate",
    "pseudonymize_east_coasters_player_ref",
]
