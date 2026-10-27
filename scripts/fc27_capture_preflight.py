#!/usr/bin/env python3
"""FC 27 Operator Capture Preflight Script.

Validates that feature flags, transport boundaries, and live execution gates
remain strictly fail-closed. Supports an isolated mock simulation validating
in-memory capability authorization without issuing any network or DNS requests.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Direct script execution puts scripts/ at sys.path[0]; establish repository root for local src imports.
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from src.fc27_capture import (
    FC27CaptureAuthorization,
    FC27CaptureBudget,
    FC27MockHTTPTransport,
    run_authorized_club_identity_capture,
)
from src.fc27_provider import FC27TransportClient
from src.settings import (
    is_dispatch_enabled,
    is_dispatch_worker_enabled,
    is_fc27_live_ingestion_enabled,
    is_fc27_provider_enabled,
    is_fc27_public_identity_lookup_enabled,
    is_fc27_recommendations_enabled,
    is_fc27_upstream_compatibility_enabled,
)


def run_safety_checks() -> bool:
    """Verifies that all safety boundaries and fail-closed configurations hold."""
    # 1. Feature flags must be strictly disabled
    if is_fc27_provider_enabled():
        return False
    if is_fc27_public_identity_lookup_enabled():
        return False
    if is_fc27_upstream_compatibility_enabled():
        return False
    if is_fc27_live_ingestion_enabled():
        return False
    if is_fc27_recommendations_enabled():
        return False
    if is_dispatch_enabled():
        return False
    if is_dispatch_worker_enabled():
        return False

    # 2. Host allowlisting bounds
    allowed_hosts = FC27TransportClient.DEFAULT_ALLOWED_HOSTS
    if allowed_hosts != {"proclubs.ea.com"}:
        return False

    return True


def run_default_preflight() -> int:
    """Executes the default baseline preflight check."""
    if not run_safety_checks():
        print("PREFLIGHT_STATUS=FAILED_SAFETY_GATE")
        return 1

    print("PREFLIGHT_STATUS=BLOCKED_SAFE")
    print("NETWORK_REQUESTS_EXECUTED=0")
    print("DNS_RESOLUTIONS_EXECUTED=0")
    print("LIVE_CAPTURE_AUTHORIZED=NO")
    return 0


def run_mock_simulation() -> int:
    """Executes a pure in-memory mock capture simulation."""
    if not run_safety_checks():
        print("PREFLIGHT_STATUS=FAILED_SAFETY_GATE")
        return 1

    now = datetime.now(timezone.utc)
    auth = FC27CaptureAuthorization(
        authorization_id="mock-preflight-auth-001",
        operation="club_identity_lookup",
        provider="EA_FC_PRO_CLUBS",
        scheme="https",
        hostname="proclubs.ea.com",
        method="GET",
        path="/api/fc/clubs/info",
        club_id="330323",
        platform="common-gen5",
        issued_at=now,
        expires_at=now + timedelta(minutes=15),
        maximum_attempts=1,
        maximum_redirects=0,
        maximum_response_bytes=5242880,
        connect_timeout_seconds=3.0,
        total_timeout_seconds=15.0,
        purpose="Preflight mock identity validation",
        authorized_by_reference="preflight_script",
        access_classification="AUTHORIZED_MOCK",
        raw_retention_allowed=False,
        personal_data_allowed=False,
        player_data_allowed=False,
        match_history_allowed=False,
    )

    budget = FC27CaptureBudget(auth.authorization_id)
    mock_transport = FC27MockHTTPTransport(
        response_body=b'{"330323": {"clubId": 330323, "name": "East Coasters", "platform": "common-gen5", "regionId": 12}}',
        status_code=200,
        content_type="application/json",
        elapsed_ms=32.0,
        redirect_count=0,
    )

    result = run_authorized_club_identity_capture(
        authorization=auth,
        budget=budget,
        transport=mock_transport,
        clock=lambda: now,
    )

    if result.identity_state != "PROVISIONAL" or not budget.is_consumed:
        print("PREFLIGHT_STATUS=MOCK_FAILED")
        return 1

    print("PREFLIGHT_STATUS=MOCK_VALIDATED")
    print("NETWORK_REQUESTS_EXECUTED=0")
    print("DNS_RESOLUTIONS_EXECUTED=0")
    print(f"MOCK_ATTEMPTS={mock_transport.attempt_count}")
    print(f"AUTHORIZATION_CONSUMED={'YES' if budget.is_consumed else 'NO'}")
    print(f"RAW_RESPONSE_RETAINED={'YES' if result.raw_response_retained else 'NO'}")
    print(f"IDENTITY_STATUS={result.identity_state}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="FC 27 Operator Capture Preflight",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Run an in-memory mock capture simulation with zero network calls.",
    )

    args = parser.parse_args()

    if args.mock:
        sys.exit(run_mock_simulation())
    else:
        sys.exit(run_default_preflight())


if __name__ == "__main__":
    main()
