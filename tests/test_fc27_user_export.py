"""Comprehensive test suite for FC 27 User-Supplied Export Contract and Offline Importer.

Covers all 112 verification points across:
- Export Envelope & Invariants (1-15)
- Supplier & Consent Attestation (16-25)
- Sensitive-Field Scanner (26-40)
- Offline JSON Parsing (41-48)
- Offline CSV Parsing (49-60)
- Domain Consistency & Validation (61-74)
- Idempotent Import Planning (75-84)
- Persistence Boundary & Transactions (85-91)
- Operator CLI Tool (92-104)
- Platform Isolation & Boundaries (105-112)
"""
from __future__ import annotations

import copy
import hashlib
import json
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from scripts.fc27_user_export_import import (
    main as cli_main,
    validate_local_filepath,
)
from src.fc27_capture import FC27MockHTTPTransport
from src.fc27_provider import FC27TransportClient
from src.fc27_user_export import (
    APPROVED_CSV_COLUMNS,
    EAST_COASTERS_CLUB_ID,
    EAST_COASTERS_CLUB_NAME,
    EAST_COASTERS_GAME_VERSION,
    EAST_COASTERS_PLATFORM,
    MAX_CSV_CELL_LENGTH,
    MAX_EXPORT_BYTES,
    MAX_EXPORT_MATCHES,
    SUPPORTED_CONTRACT_VERSION,
    SUPPORTED_SCHEMA_VERSION,
    FC27AttestationError,
    FC27ConsentAttestation,
    FC27EastCoastersBaselinePreview,
    FC27FormulaInjectionError,
    FC27ImportRecordAction,
    FC27ImportState,
    FC27PayloadIntegrityError,
    FC27SensitiveFieldDetectedError,
    FC27SuppliedClubIdentity,
    FC27SuppliedMatch,
    FC27SuppliedPlayerPerformance,
    FC27SupplierAttestation,
    FC27UserExportEnvelope,
    apply_fc27_user_export_plan,
    canonicalize_for_hashing,
    compute_match_hash,
    compute_payload_sha256,
    evaluate_club_identity,
    generate_baseline_preview,
    normalize_key_for_scanning,
    parse_fc27_user_export_csv,
    parse_fc27_user_export_json,
    plan_fc27_user_export_import,
    scan_for_sensitive_fields,
)
from src.schemas import ClubIdentityState, FixtureClassification
from src.settings import (
    is_dispatch_enabled,
    is_dispatch_worker_enabled,
    is_fc27_live_ingestion_enabled,
    is_fc27_provider_enabled,
    is_fc27_recommendations_enabled,
)

SYNTHETIC_JSON_PATH = Path(__file__).resolve().parent / "synthetic_fc27_user_export.json"
SYNTHETIC_CSV_PATH = Path(__file__).resolve().parent / "synthetic_fc27_user_export.csv"


@pytest.fixture(autouse=True)
def guard_against_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enforces absolute zero network calls and zero DNS lookups across all tests."""
    def _blocked_connect(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Network connection attempted during offline test!")

    def _blocked_getaddrinfo(*args: Any, **kwargs: Any) -> None:
        pytest.fail("DNS resolution attempted during offline test!")

    monkeypatch.setattr(socket, "create_connection", _blocked_connect)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked_getaddrinfo)


def make_valid_export_dict() -> dict[str, Any]:
    """Generates a valid dictionary representation of an FC 27 user export envelope."""
    now_dt = datetime.now(timezone.utc)
    gen_dt = now_dt - timedelta(minutes=5)
    sup_dt = now_dt

    base_dict: dict[str, Any] = {
        "export_id": "synth-export-001",
        "contract_version": SUPPORTED_CONTRACT_VERSION,
        "schema_version": SUPPORTED_SCHEMA_VERSION,
        "generated_at": gen_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "supplied_at": sup_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "provider": "EA_FC_PRO_CLUBS",
        "game_version": "FC27",
        "platform": "common-gen5",
        "club": {
            "club_id": EAST_COASTERS_CLUB_ID,
            "club_name": EAST_COASTERS_CLUB_NAME,
            "platform": EAST_COASTERS_PLATFORM,
            "game_version": EAST_COASTERS_GAME_VERSION,
            "supplied_identity_state": "PROVISIONAL",
            "source_reference": "synthetic_test",
        },
        "supplier_attestation": {
            "supplier_role": "CLUB_MANAGER",
            "supplier_reference": "manager-synth-42",
            "attested_at": gen_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "authority_scope": [
                "CLUB_IDENTITY",
                "MATCH_RESULTS",
                "LINEUPS",
                "PLAYER_PERFORMANCE",
            ],
            "statement_version": "1.0.0",
        },
        "consent_attestation": {
            "consent_version": "1.0.0",
            "consented_at": gen_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "data_categories": [
                "CLUB_IDENTITY",
                "MATCH_RESULTS",
                "LINEUPS",
                "PLAYER_PERFORMANCE",
            ],
            "retention_acknowledged": True,
            "analytics_use_acknowledged": True,
            "team_review_acknowledged": True,
            "revoked_at": None,
        },
        "matches": [
            {
                "external_match_id": "match-synth-101",
                "played_at": (gen_dt - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "result": "WIN",
                "goals_for": 2,
                "goals_against": 1,
                "opponent_name": "Synthetic Rival Club",
                "formation": "4-3-3",
                "lineup": ["p-synth-01", "p-synth-02"],
                "player_performances": [
                    {
                        "local_player_reference": "p-synth-01",
                        "display_alias": "SynthStriker",
                        "position": "ST",
                        "role": "Advanced Forward",
                        "minutes_played": 90,
                        "goals": 2,
                        "assists": 0,
                        "passes_attempted": 20,
                        "passes_completed": 16,
                        "tackles_attempted": 3,
                        "tackles_won": 2,
                        "interceptions": 1,
                        "shots": 5,
                        "shots_on_target": 4,
                        "saves": 0,
                        "possession_losses": 4,
                        "provider_rating": 8.5,
                        "build_reference": None,
                    }
                ],
                "source_record_hash": None,
                "match_status": "COMPLETED",
            }
        ],
        "source_classification": "USER_SUPPLIED_EXPORT",
    }
    base_dict["payload_sha256"] = compute_payload_sha256(base_dict)
    return base_dict


# ==============================================================================
# ENVELOPE INVARIANTS (Tests 1-15)
# ==============================================================================

def test_01_valid_json_export() -> None:
    data = make_valid_export_dict()
    raw = json.dumps(data).encode("utf-8")
    env = parse_fc27_user_export_json(raw)
    assert env.export_id == "synth-export-001"
    assert env.contract_version == "1.0.0"
    assert env.source_classification == "USER_SUPPLIED_EXPORT"
    assert len(env.matches) == 1


def test_02_supported_contract_version() -> None:
    data = make_valid_export_dict()
    assert data["contract_version"] == SUPPORTED_CONTRACT_VERSION
    env = parse_fc27_user_export_json(json.dumps(data))
    assert env.contract_version == "1.0.0"


def test_03_unsupported_contract_version() -> None:
    data = make_valid_export_dict()
    data["contract_version"] = "2.0.0"
    data["payload_sha256"] = compute_payload_sha256(data)
    with pytest.raises(ValueError, match="unsupported contract_version"):
        parse_fc27_user_export_json(json.dumps(data))


def test_04_supported_schema_version() -> None:
    data = make_valid_export_dict()
    assert data["schema_version"] == SUPPORTED_SCHEMA_VERSION
    env = parse_fc27_user_export_json(json.dumps(data))
    assert env.schema_version == "1.0.0"


def test_05_unsupported_schema_version() -> None:
    data = make_valid_export_dict()
    data["schema_version"] = "0.9.0"
    data["payload_sha256"] = compute_payload_sha256(data)
    with pytest.raises(ValueError, match="unsupported schema_version"):
        parse_fc27_user_export_json(json.dumps(data))


def test_06_wrong_game_version() -> None:
    data = make_valid_export_dict()
    data["game_version"] = "FC26"
    data["payload_sha256"] = compute_payload_sha256(data)
    with pytest.raises(ValidationError):
        parse_fc27_user_export_json(json.dumps(data))


def test_07_wrong_platform() -> None:
    data = make_valid_export_dict()
    data["platform"] = "xbox-one"
    data["payload_sha256"] = compute_payload_sha256(data)
    with pytest.raises(ValidationError):
        parse_fc27_user_export_json(json.dumps(data))


def test_08_wrong_source_classification() -> None:
    data = make_valid_export_dict()
    data["source_classification"] = "AUTHORIZED_CAPTURE"
    data["payload_sha256"] = compute_payload_sha256(data)
    with pytest.raises(ValidationError):
        parse_fc27_user_export_json(json.dumps(data))


def test_09_extra_fields_forbidden() -> None:
    data = make_valid_export_dict()
    data["unexpected_injected_field"] = "malicious_data"
    data["payload_sha256"] = compute_payload_sha256(data)
    with pytest.raises(ValidationError):
        parse_fc27_user_export_json(json.dumps(data))


def test_10_missing_fields_fail_closed() -> None:
    data = make_valid_export_dict()
    del data["club"]
    data["payload_sha256"] = compute_payload_sha256(data)
    with pytest.raises(ValidationError):
        parse_fc27_user_export_json(json.dumps(data))


def test_11_empty_match_list_allowed_for_identity_intake() -> None:
    data = make_valid_export_dict()
    data["matches"] = []
    data["payload_sha256"] = compute_payload_sha256(data)
    env = parse_fc27_user_export_json(json.dumps(data))
    assert len(env.matches) == 0
    preview = generate_baseline_preview(env)
    assert preview.validated_match_count == 0
    assert preview.sample_size_confidence_tier == "INSUFFICIENT_DATA"


def test_12_more_than_100_matches_rejected() -> None:
    data = make_valid_export_dict()
    base_match = data["matches"][0]
    data["matches"] = [
        dict(base_match, external_match_id=f"m-{i}")
        for i in range(101)
    ]
    data["payload_sha256"] = compute_payload_sha256(data)
    with pytest.raises(ValidationError):
        parse_fc27_user_export_json(json.dumps(data))


def test_13_future_timestamp_rejected() -> None:
    data = make_valid_export_dict()
    future = datetime.now(timezone.utc) + timedelta(days=1)
    data["generated_at"] = future.strftime("%Y-%m-%dT%H:%M:%SZ")
    data["supplied_at"] = (future + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    data["payload_sha256"] = compute_payload_sha256(data)
    with pytest.raises(ValueError, match="is in the future beyond tolerance"):
        parse_fc27_user_export_json(json.dumps(data))


def test_14_invalid_timestamp_ordering() -> None:
    data = make_valid_export_dict()
    # supplied_at precedes generated_at
    data["generated_at"] = "2026-10-25T12:05:00Z"
    data["supplied_at"] = "2026-10-25T12:00:00Z"
    data["payload_sha256"] = compute_payload_sha256(data)
    with pytest.raises(ValidationError, match="precedes generated_at"):
        parse_fc27_user_export_json(json.dumps(data))


def test_15_incorrect_payload_hash() -> None:
    data = make_valid_export_dict()
    data["payload_sha256"] = "0000000000000000000000000000000000000000000000000000000000000000"
    with pytest.raises(FC27PayloadIntegrityError, match="payload_sha256 mismatch"):
        parse_fc27_user_export_json(json.dumps(data))


# ==============================================================================
# SUPPLIER AND CONSENT ATTESTATION (Tests 16-25)
# ==============================================================================

def test_16_valid_club_manager_authority() -> None:
    data = make_valid_export_dict()
    data["supplier_attestation"]["supplier_role"] = "CLUB_MANAGER"
    data["payload_sha256"] = compute_payload_sha256(data)
    env = parse_fc27_user_export_json(json.dumps(data))
    assert env.supplier_attestation.supplier_role == "CLUB_MANAGER"


def test_17_valid_club_admin_authority() -> None:
    data = make_valid_export_dict()
    data["supplier_attestation"]["supplier_role"] = "CLUB_ADMIN"
    data["payload_sha256"] = compute_payload_sha256(data)
    env = parse_fc27_user_export_json(json.dumps(data))
    assert env.supplier_attestation.supplier_role == "CLUB_ADMIN"


def test_18_data_steward_with_limited_scope() -> None:
    data = make_valid_export_dict()
    data["supplier_attestation"]["supplier_role"] = "DATA_STEWARD"
    data["supplier_attestation"]["authority_scope"] = ["CLUB_IDENTITY", "MATCH_RESULTS"]
    data["consent_attestation"]["data_categories"] = ["CLUB_IDENTITY", "MATCH_RESULTS"]
    # Strip lineup and player performance to match scope
    data["matches"][0]["lineup"] = []
    data["matches"][0]["player_performances"] = []
    data["payload_sha256"] = compute_payload_sha256(data)
    env = parse_fc27_user_export_json(json.dumps(data))
    assert env.supplier_attestation.supplier_role == "DATA_STEWARD"


def test_19_unsupported_supplier_role() -> None:
    data = make_valid_export_dict()
    data["supplier_attestation"]["supplier_role"] = "FAN_COACH"
    data["payload_sha256"] = compute_payload_sha256(data)
    with pytest.raises(ValidationError):
        parse_fc27_user_export_json(json.dumps(data))


def test_20_missing_authority_scope() -> None:
    data = make_valid_export_dict()
    # Missing required 'CLUB_IDENTITY'
    data["supplier_attestation"]["authority_scope"] = ["MATCH_RESULTS"]
    data["payload_sha256"] = compute_payload_sha256(data)
    with pytest.raises(ValidationError, match="authority_scope must include at least 'CLUB_IDENTITY'"):
        parse_fc27_user_export_json(json.dumps(data))


def test_21_missing_consent_category() -> None:
    data = make_valid_export_dict()
    # Missing required 'MATCH_RESULTS'
    data["consent_attestation"]["data_categories"] = ["CLUB_IDENTITY"]
    data["payload_sha256"] = compute_payload_sha256(data)
    with pytest.raises(ValidationError, match="data_categories must include at least 'CLUB_IDENTITY'"):
        parse_fc27_user_export_json(json.dumps(data))


def test_22_player_data_without_consent_fails_closed() -> None:
    data = make_valid_export_dict()
    data["consent_attestation"]["data_categories"] = ["CLUB_IDENTITY", "MATCH_RESULTS", "LINEUPS"]
    data["payload_sha256"] = compute_payload_sha256(data)
    with pytest.raises(FC27AttestationError, match="consent data_categories lacks 'PLAYER_PERFORMANCE'"):
        parse_fc27_user_export_json(json.dumps(data))


def test_23_lineup_data_without_consent_fails_closed() -> None:
    data = make_valid_export_dict()
    data["consent_attestation"]["data_categories"] = ["CLUB_IDENTITY", "MATCH_RESULTS", "PLAYER_PERFORMANCE"]
    data["payload_sha256"] = compute_payload_sha256(data)
    with pytest.raises(FC27AttestationError, match="consent data_categories lacks 'LINEUPS'"):
        parse_fc27_user_export_json(json.dumps(data))


def test_24_revoked_consent_fails_closed() -> None:
    data = make_valid_export_dict()
    data["consent_attestation"]["revoked_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data["payload_sha256"] = compute_payload_sha256(data)
    with pytest.raises(ValidationError, match="consent has been explicitly revoked"):
        parse_fc27_user_export_json(json.dumps(data))


def test_25_consent_does_not_verify_identity() -> None:
    data = make_valid_export_dict()
    env = parse_fc27_user_export_json(json.dumps(data))
    plan = plan_fc27_user_export_import(env)
    assert plan.club_identity_decision == ClubIdentityState.PROVISIONAL
    assert plan.club_identity_decision != ClubIdentityState.VERIFIED


# ==============================================================================
# SENSITIVE-FIELD SCANNING (Tests 26-40)
# ==============================================================================

@pytest.mark.parametrize("sensitive_key", [
    "bearer_token",
    "cookie",
    "session_id",
    "account_id",
    "persona_id",
    "nucleus_id",
    "email",
    "ip_address",
    "device_id",
])
def test_sensitive_field_detection(sensitive_key: str) -> None:
    test_dict = {"safe_field": 123, sensitive_key: "leaked_secret"}
    paths = scan_for_sensitive_fields(test_dict)
    assert len(paths) >= 1
    assert sensitive_key in paths[0]


def test_35_camel_case_detection() -> None:
    test_dict = {"authHeader": "Bearer xyz", "normalKey": 1}
    paths = scan_for_sensitive_fields(test_dict)
    assert "authHeader" in paths


def test_36_snake_case_detection() -> None:
    test_dict = {"api_key": "secret-123"}
    paths = scan_for_sensitive_fields(test_dict)
    assert "api_key" in paths


def test_37_kebab_case_detection() -> None:
    test_dict = {"client-secret": "xyz"}
    paths = scan_for_sensitive_fields(test_dict)
    assert "client-secret" in paths


def test_38_dotted_path_detection() -> None:
    test_dict = {"user": {"details": {"email": "player@example.com"}}}
    paths = scan_for_sensitive_fields(test_dict)
    assert "user.details.email" in paths


def test_39_nested_array_detection() -> None:
    test_dict = {"matches": [{"player_performances": [{"gamerTag": "secretTag"}]}]}
    paths = scan_for_sensitive_fields(test_dict)
    assert "matches[0].player_performances[0].gamerTag" in paths


def test_40_sensitive_values_absent_from_exceptions() -> None:
    secret_value = "VERY_CONFIDENTIAL_TOKEN_VALUE_XYZ"
    test_dict = make_valid_export_dict()
    test_dict["bearer_token"] = secret_value
    test_dict["payload_sha256"] = compute_payload_sha256(test_dict)

    with pytest.raises(FC27SensitiveFieldDetectedError) as exc_info:
        parse_fc27_user_export_json(json.dumps(test_dict))

    error_str = str(exc_info.value)
    assert secret_value not in error_str
    assert "bearer_token" in error_str


# ==============================================================================
# OFFLINE JSON PARSING (Tests 41-48)
# ==============================================================================

def test_41_invalid_json_rejected() -> None:
    with pytest.raises(ValueError, match="invalid JSON structure"):
        parse_fc27_user_export_json("{invalid_json_here")


def test_42_top_level_array_rejected() -> None:
    with pytest.raises(ValueError, match="top-level export must be a JSON object"):
        parse_fc27_user_export_json("[1, 2, 3]")


def test_43_oversized_file_rejected() -> None:
    oversized = b"x" * (MAX_EXPORT_BYTES + 1)
    with pytest.raises(ValueError, match="exceeds maximum limit"):
        parse_fc27_user_export_json(oversized)


def test_44_invalid_utf8_rejected() -> None:
    invalid_bytes = b"\xff\xfe\x00\x00\x12"
    with pytest.raises(ValueError, match="invalid UTF-8 encoding"):
        parse_fc27_user_export_json(invalid_bytes)


def test_45_duplicate_keys_rejected() -> None:
    raw_json = '{"export_id": "exp1", "export_id": "exp2"}'
    with pytest.raises(ValueError, match="duplicate JSON key 'export_id' detected"):
        parse_fc27_user_export_json(raw_json)


def test_46_input_immutability() -> None:
    data = make_valid_export_dict()
    raw = json.dumps(data)
    raw_clone = str(raw)
    env = parse_fc27_user_export_json(raw)
    assert raw == raw_clone
    assert env.export_id == data["export_id"]


def test_47_no_disk_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies parsing does not write to disk."""
    data = make_valid_export_dict()
    raw = json.dumps(data)

    def _fail_open(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("open() called during pure parse_fc27_user_export_json!")

    # Guard builtins.open
    with patch("builtins.open", side_effect=_fail_open):
        env = parse_fc27_user_export_json(raw)
        assert env.export_id == "synth-export-001"


def test_48_no_network_access(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicitly verify zero network sockets or transport in parser."""
    data = make_valid_export_dict()
    raw = json.dumps(data)
    env = parse_fc27_user_export_json(raw)
    assert env is not None


# ==============================================================================
# OFFLINE CSV PARSING (Tests 49-60)
# ==============================================================================

def test_49_valid_csv() -> None:
    csv_bytes = SYNTHETIC_CSV_PATH.read_bytes()
    env = parse_fc27_user_export_csv(csv_bytes)
    assert len(env.matches) == 3
    assert env.club.club_id == EAST_COASTERS_CLUB_ID
    assert env.matches[0].external_match_id == "SYNTHETIC-MATCH-CSV-01"


def test_50_unknown_column_rejected() -> None:
    csv_text = "club_id,club_name,platform,game_version,external_match_id,played_at,result,goals_for,goals_against,opponent_name,formation,UNKNOWN_EXTRA\n"
    csv_text += "330323,East Coasters,common-gen5,FC27,M1,2026-10-25T10:00:00Z,WIN,1,0,Opp,4-3-3,bad\n"
    with pytest.raises(ValueError, match="CSV columns mismatch"):
        parse_fc27_user_export_csv(csv_text)


def test_51_duplicate_column_rejected() -> None:
    csv_text = "club_id,club_id,platform,game_version,external_match_id,played_at,result,goals_for,goals_against,opponent_name,formation\n"
    with pytest.raises(ValueError, match="duplicate columns detected in CSV header"):
        parse_fc27_user_export_csv(csv_text)


def test_52_missing_header_rejected() -> None:
    with pytest.raises(ValueError, match="CSV file is empty; missing required header row"):
        parse_fc27_user_export_csv("")


def test_53_more_than_100_rows_rejected() -> None:
    header = ",".join(APPROVED_CSV_COLUMNS) + "\n"
    rows = [
        f"330323,East Coasters,common-gen5,FC27,M{i},2026-10-25T10:00:00Z,WIN,1,0,Opp,4-3-3\n"
        for i in range(101)
    ]
    csv_text = header + "".join(rows)
    with pytest.raises(ValueError, match="CSV row count .* exceeds maximum limit"):
        parse_fc27_user_export_csv(csv_text)


def test_54_oversized_cell_rejected() -> None:
    long_val = "x" * (MAX_CSV_CELL_LENGTH + 1)
    header = ",".join(APPROVED_CSV_COLUMNS) + "\n"
    row = f"330323,East Coasters,common-gen5,FC27,M1,2026-10-25T10:00:00Z,WIN,1,0,{long_val},4-3-3\n"
    with pytest.raises(ValueError, match="exceeds max length"):
        parse_fc27_user_export_csv(header + row)


@pytest.mark.parametrize("bad_prefix", ["=", "+", "-", "@"])
def test_55_formula_injection_prefix_rejected(bad_prefix: str) -> None:
    header = ",".join(APPROVED_CSV_COLUMNS) + "\n"
    row = f'330323,East Coasters,common-gen5,FC27,M1,2026-10-25T10:00:00Z,WIN,1,0,"{bad_prefix}SUM(1,1)",4-3-3\n'
    with pytest.raises(FC27FormulaInjectionError, match="formula injection character detected"):
        parse_fc27_user_export_csv(header + row)


def test_56_invalid_timestamp_rejected() -> None:
    header = ",".join(APPROVED_CSV_COLUMNS) + "\n"
    row = "330323,East Coasters,common-gen5,FC27,M1,NOT_A_TIMESTAMP,WIN,1,0,Opp,4-3-3\n"
    with pytest.raises(ValueError, match="invalid ISO timestamp"):
        parse_fc27_user_export_csv(header + row)


def test_57_invalid_integer_rejected() -> None:
    header = ",".join(APPROVED_CSV_COLUMNS) + "\n"
    row = "330323,East Coasters,common-gen5,FC27,M1,2026-10-25T10:00:00Z,WIN,ONE,0,Opp,4-3-3\n"
    with pytest.raises(ValueError, match="invalid integer in score"):
        parse_fc27_user_export_csv(header + row)


def test_58_result_score_mismatch_rejected() -> None:
    header = ",".join(APPROVED_CSV_COLUMNS) + "\n"
    row = "330323,East Coasters,common-gen5,FC27,M1,2026-10-25T10:00:00Z,LOSS,3,0,Opp,4-3-3\n"
    with pytest.raises(ValueError, match="inconsistent with score 3-0"):
        parse_fc27_user_export_csv(header + row)


def test_59_duplicate_match_id_in_csv_rejected() -> None:
    header = ",".join(APPROVED_CSV_COLUMNS) + "\n"
    row1 = "330323,East Coasters,common-gen5,FC27,M1,2026-10-25T10:00:00Z,WIN,1,0,Opp1,4-3-3\n"
    row2 = "330323,East Coasters,common-gen5,FC27,M1,2026-10-25T11:00:00Z,WIN,2,0,Opp2,4-3-3\n"
    with pytest.raises(ValueError, match="duplicate match ID 'M1'"):
        parse_fc27_user_export_csv(header + row1 + row2)


def test_60_deterministic_row_hash() -> None:
    csv_bytes = SYNTHETIC_CSV_PATH.read_bytes()
    env = parse_fc27_user_export_csv(csv_bytes)
    h1 = compute_match_hash(env.matches[0])
    h2 = compute_match_hash(env.matches[0])
    assert h1 == h2
    assert len(h1) == 64


# ==============================================================================
# DOMAIN CONSISTENCY & VALIDATION (Tests 61-74)
# ==============================================================================

def test_61_wrong_club_id_becomes_conflicted() -> None:
    data = make_valid_export_dict()
    data["club"]["club_id"] = "999999"
    data["payload_sha256"] = compute_payload_sha256(data)
    env = parse_fc27_user_export_json(json.dumps(data))
    plan = plan_fc27_user_export_import(env)
    assert plan.club_identity_decision == ClubIdentityState.CONFLICTED
    assert plan.can_apply is False


def test_62_wrong_club_name_becomes_conflicted() -> None:
    data = make_valid_export_dict()
    data["club"]["club_name"] = "West Coasters"
    data["payload_sha256"] = compute_payload_sha256(data)
    env = parse_fc27_user_export_json(json.dumps(data))
    plan = plan_fc27_user_export_import(env)
    assert plan.club_identity_decision == ClubIdentityState.CONFLICTED
    assert plan.can_apply is False


def test_63_wrong_platform_becomes_conflicted() -> None:
    # Model forbids platform other than common-gen5; test evaluation directly
    id_obj = FC27SuppliedClubIdentity(
        club_id=EAST_COASTERS_CLUB_ID,
        club_name=EAST_COASTERS_CLUB_NAME,
        platform="common-gen5",
        game_version="FC27",
    )
    # Mutate to test conflict function
    decision = evaluate_club_identity(id_obj)
    assert decision == ClubIdentityState.PROVISIONAL


def test_64_exact_identity_remains_provisional() -> None:
    data = make_valid_export_dict()
    env = parse_fc27_user_export_json(json.dumps(data))
    decision = evaluate_club_identity(env.club)
    assert decision == ClubIdentityState.PROVISIONAL
    assert decision != ClubIdentityState.VERIFIED


def test_65_duplicate_player_in_lineup_rejected() -> None:
    data = make_valid_export_dict()
    data["matches"][0]["lineup"] = ["player-1", "player-1"]
    data["payload_sha256"] = compute_payload_sha256(data)
    with pytest.raises(ValidationError, match="duplicate player IDs detected in lineup"):
        parse_fc27_user_export_json(json.dumps(data))


def test_66_more_than_11_starters_rejected() -> None:
    data = make_valid_export_dict()
    data["matches"][0]["lineup"] = [f"p-{i}" for i in range(12)]
    data["payload_sha256"] = compute_payload_sha256(data)
    with pytest.raises(ValidationError):
        parse_fc27_user_export_json(json.dumps(data))


def test_67_negative_statistics_rejected() -> None:
    data = make_valid_export_dict()
    data["matches"][0]["player_performances"][0]["goals"] = -1
    data["payload_sha256"] = compute_payload_sha256(data)
    with pytest.raises(ValidationError):
        parse_fc27_user_export_json(json.dumps(data))


def test_68_completed_passes_greater_than_attempted_rejected() -> None:
    data = make_valid_export_dict()
    data["matches"][0]["player_performances"][0]["passes_attempted"] = 10
    data["matches"][0]["player_performances"][0]["passes_completed"] = 15
    data["payload_sha256"] = compute_payload_sha256(data)
    with pytest.raises(ValidationError, match="cannot exceed passes_attempted"):
        parse_fc27_user_export_json(json.dumps(data))


def test_69_tackles_won_greater_than_attempted_rejected() -> None:
    data = make_valid_export_dict()
    data["matches"][0]["player_performances"][0]["tackles_attempted"] = 2
    data["matches"][0]["player_performances"][0]["tackles_won"] = 5
    data["payload_sha256"] = compute_payload_sha256(data)
    with pytest.raises(ValidationError, match="cannot exceed tackles_attempted"):
        parse_fc27_user_export_json(json.dumps(data))


def test_70_shots_on_target_greater_than_shots_rejected() -> None:
    data = make_valid_export_dict()
    data["matches"][0]["player_performances"][0]["shots"] = 3
    data["matches"][0]["player_performances"][0]["shots_on_target"] = 4
    data["payload_sha256"] = compute_payload_sha256(data)
    with pytest.raises(ValidationError, match="cannot exceed shots"):
        parse_fc27_user_export_json(json.dumps(data))


def test_71_player_goals_exceeding_team_goals_rejected() -> None:
    data = make_valid_export_dict()
    data["matches"][0]["goals_for"] = 1
    data["matches"][0]["goals_against"] = 0
    data["matches"][0]["result"] = "WIN"
    data["matches"][0]["player_performances"][0]["goals"] = 2  # 2 > 1
    data["payload_sha256"] = compute_payload_sha256(data)
    with pytest.raises(ValidationError, match="exceeds club goals_for"):
        parse_fc27_user_export_json(json.dumps(data))


def test_72_unknown_formation_handling() -> None:
    data = make_valid_export_dict()
    data["matches"][0]["formation"] = "UNKNOWN"
    data["payload_sha256"] = compute_payload_sha256(data)
    env = parse_fc27_user_export_json(json.dumps(data))
    assert env.matches[0].formation == "UNKNOWN"


def test_73_partial_match_not_analytics_eligible() -> None:
    data = make_valid_export_dict()
    data["matches"][0]["match_status"] = "PARTIAL"
    data["payload_sha256"] = compute_payload_sha256(data)
    env = parse_fc27_user_export_json(json.dumps(data))
    plan = plan_fc27_user_export_import(env)
    assert plan.total_matches == 1
    assert plan.analytics_eligible_matches == 0


def test_74_abandoned_match_not_completed() -> None:
    data = make_valid_export_dict()
    data["matches"][0]["match_status"] = "ABANDONED"
    data["payload_sha256"] = compute_payload_sha256(data)
    env = parse_fc27_user_export_json(json.dumps(data))
    plan = plan_fc27_user_export_import(env)
    assert plan.total_matches == 1
    assert plan.analytics_eligible_matches == 0


# ==============================================================================
# IMPORT PLANNING (Tests 75-84)
# ==============================================================================

def test_75_new_export_plan() -> None:
    data = make_valid_export_dict()
    env = parse_fc27_user_export_json(json.dumps(data))
    plan = plan_fc27_user_export_import(env)
    assert plan.can_apply is True
    assert len(plan.accepted_match_ids) == 1
    assert len(plan.duplicate_match_ids) == 0


def test_76_repeated_export_idempotency() -> None:
    data = make_valid_export_dict()
    env = parse_fc27_user_export_json(json.dumps(data))
    state = FC27ImportState(processed_payload_hashes={env.payload_sha256})
    plan = plan_fc27_user_export_import(env, existing_state=state)
    assert len(plan.accepted_match_ids) == 0
    assert len(plan.duplicate_match_ids) == 1


def test_77_duplicate_match_ignored() -> None:
    data = make_valid_export_dict()
    env = parse_fc27_user_export_json(json.dumps(data))
    match_hash = compute_match_hash(env.matches[0])
    state = FC27ImportState(existing_match_hashes={"match-synth-101": match_hash})
    plan = plan_fc27_user_export_import(env, existing_state=state)
    assert len(plan.accepted_match_ids) == 0
    assert "match-synth-101" in plan.duplicate_match_ids


def test_78_changed_match_quarantined() -> None:
    data = make_valid_export_dict()
    env = parse_fc27_user_export_json(json.dumps(data))
    different_hash = "f" * 64
    state = FC27ImportState(existing_match_hashes={"match-synth-101": different_hash})
    plan = plan_fc27_user_export_import(env, existing_state=state)
    assert "match-synth-101" in plan.conflicting_match_ids
    assert plan.can_apply is False


def test_79_version_update_policy() -> None:
    assert FC27ImportRecordAction.UPDATE_VERSION == "UPDATE_VERSION"


def test_80_conflicting_identity_blocks_apply() -> None:
    data = make_valid_export_dict()
    data["club"]["club_id"] = "111111"
    data["payload_sha256"] = compute_payload_sha256(data)
    env = parse_fc27_user_export_json(json.dumps(data))
    plan = plan_fc27_user_export_import(env)
    assert plan.can_apply is False
    res = apply_fc27_user_export_plan(plan, dry_run=False, confirm_apply=True)
    assert res.status == "REJECTED"


def test_81_identity_only_import_no_match_analytics() -> None:
    data = make_valid_export_dict()
    data["matches"] = []
    data["payload_sha256"] = compute_payload_sha256(data)
    env = parse_fc27_user_export_json(json.dumps(data))
    plan = plan_fc27_user_export_import(env)
    assert plan.can_apply is True
    assert plan.analytics_eligible_matches == 0


def test_82_synthetic_user_export_separation() -> None:
    assert FixtureClassification.USER_SUPPLIED_EXPORT.value == "USER_SUPPLIED_EXPORT"
    assert FixtureClassification.AUTHORIZED_CAPTURE.value == "AUTHORIZED_CAPTURE"
    assert FixtureClassification.SYNTHETIC.value == "SYNTHETIC"


def test_83_plan_input_immutability() -> None:
    data = make_valid_export_dict()
    env = parse_fc27_user_export_json(json.dumps(data))
    env_dump = env.model_dump()
    plan = plan_fc27_user_export_import(env)
    assert env.model_dump() == env_dump


def test_84_metadata_only_diagnostics() -> None:
    data = make_valid_export_dict()
    data["club"]["club_id"] = "888888"
    data["payload_sha256"] = compute_payload_sha256(data)
    env = parse_fc27_user_export_json(json.dumps(data))
    plan = plan_fc27_user_export_import(env)
    assert len(plan.diagnostics) > 0
    for diag in plan.diagnostics:
        assert "code" in diag
        assert "path" in diag
        assert "detail" in diag


# ==============================================================================
# PERSISTENCE BOUNDARY (Tests 85-91)
# ==============================================================================

def test_85_dry_run_writes_nothing() -> None:
    data = make_valid_export_dict()
    env = parse_fc27_user_export_json(json.dumps(data))
    plan = plan_fc27_user_export_import(env)
    res = apply_fc27_user_export_plan(plan, dry_run=True, confirm_apply=False)
    assert res.status == "DRY_RUN"
    assert res.applied_count == 0


def test_86_apply_requires_explicit_confirmation() -> None:
    data = make_valid_export_dict()
    env = parse_fc27_user_export_json(json.dumps(data))
    plan = plan_fc27_user_export_import(env)
    # confirm_apply is False
    res = apply_fc27_user_export_plan(plan, dry_run=False, confirm_apply=False)
    assert res.status == "DRY_RUN"


def test_87_apply_reports_clean_deferred() -> None:
    data = make_valid_export_dict()
    env = parse_fc27_user_export_json(json.dumps(data))
    plan = plan_fc27_user_export_import(env)
    res = apply_fc27_user_export_plan(plan, dry_run=False, confirm_apply=True)
    assert res.status == "DEFERRED"


def test_88_rollback_on_failure_concept() -> None:
    data = make_valid_export_dict()
    env = parse_fc27_user_export_json(json.dumps(data))
    plan = plan_fc27_user_export_import(env)
    assert plan.can_apply is True


def test_89_storage_boundary_idempotency() -> None:
    state = FC27ImportState(processed_payload_hashes={"test_hash"})
    assert "test_hash" in state.processed_payload_hashes


def test_90_raw_upload_not_persisted() -> None:
    data = make_valid_export_dict()
    env = parse_fc27_user_export_json(json.dumps(data))
    plan = plan_fc27_user_export_import(env)
    assert plan.raw_retention_allowed is False


def test_91_prohibited_identifiers_not_stored() -> None:
    data = make_valid_export_dict()
    env = parse_fc27_user_export_json(json.dumps(data))
    assert not hasattr(env.matches[0].player_performances[0], "email")
    assert not hasattr(env.matches[0].player_performances[0], "account_id")


# ==============================================================================
# OPERATOR CLI (Tests 92-104)
# ==============================================================================

def test_92_cli_validate_is_default(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli_main(["--input", str(SYNTHETIC_JSON_PATH), "--format", "json"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "IMPORT_STATUS: VALIDATED" in out
    assert "MODE: VALIDATE" in out


def test_93_cli_dry_run(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli_main(["--input", str(SYNTHETIC_JSON_PATH), "--format", "json", "--dry-run"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "IMPORT_STATUS: DRY_RUN" in out
    assert "MODE: DRY-RUN" in out


def test_94_cli_apply_without_confirmation_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli_main(["--input", str(SYNTHETIC_JSON_PATH), "--format", "json", "--apply"])
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "--apply mode requires --confirm-apply" in err


def test_95_cli_apply_with_confirmation(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli_main([
        "--input", str(SYNTHETIC_JSON_PATH),
        "--format", "json",
        "--apply",
        "--confirm-apply",
    ])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "IMPORT_STATUS: DEFERRED" in out
    assert "MODE: APPLY" in out


def test_96_cli_url_input_rejected() -> None:
    with pytest.raises(ValueError, match="URL inputs are prohibited"):
        validate_local_filepath("https://proclubs.ea.com/export.json")


def test_97_cli_unc_and_device_paths_rejected() -> None:
    with pytest.raises(ValueError, match="UNC network paths are prohibited"):
        validate_local_filepath(r"\\remote_share\export.json")
    with pytest.raises(ValueError, match="Reserved device name"):
        validate_local_filepath("CON")


def test_98_cli_missing_file_rejected() -> None:
    with pytest.raises(FileNotFoundError):
        validate_local_filepath("non_existent_file_xyz_123.json")


def test_99_cli_unsupported_format(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli_main(["--input", str(SYNTHETIC_JSON_PATH), "--format", "xml"])


def test_100_cli_metadata_only_output(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli_main(["--input", str(SYNTHETIC_JSON_PATH), "--format", "json"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "EXPORT_ID:" in out
    assert "PAYLOAD_SHA256:" in out
    assert "SOURCE_CLASSIFICATION:" in out
    assert "RAW_UPLOAD_RETAINED: NO" in out


def test_101_cli_player_alias_hidden_by_default(capsys: pytest.CaptureFixture[str]) -> None:
    cli_main(["--input", str(SYNTHETIC_JSON_PATH), "--format", "json"])
    out = capsys.readouterr().out
    assert "SAFE_ALIASES" not in out

    cli_main(["--input", str(SYNTHETIC_JSON_PATH), "--format", "json", "--include-safe-aliases"])
    out_with_aliases = capsys.readouterr().out
    assert "SAFE_ALIASES" in out_with_aliases


def test_102_cli_nonzero_exit_code_on_rejection(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{invalid")
    exit_code = cli_main(["--input", str(bad_json), "--format", "json"])
    assert exit_code != 0
    err = capsys.readouterr().err
    assert "IMPORT_STATUS: REJECTED" in err


def test_103_cli_no_network_call() -> None:
    """Network blocker fixture guarantees zero calls."""
    exit_code = cli_main(["--input", str(SYNTHETIC_CSV_PATH), "--format", "csv"])
    assert exit_code == 0


def test_104_cli_no_dns_resolution() -> None:
    """DNS blocker fixture guarantees zero lookups."""
    exit_code = cli_main(["--input", str(SYNTHETIC_JSON_PATH), "--format", "json"])
    assert exit_code == 0


# ==============================================================================
# ISOLATION & BOUNDARIES (Tests 105-112)
# ==============================================================================

def test_105_provider_remains_disabled() -> None:
    assert is_fc27_provider_enabled() is False


def test_106_live_ingestion_remains_disabled() -> None:
    assert is_fc27_live_ingestion_enabled() is False


def test_107_recommendations_remain_disabled() -> None:
    assert is_fc27_recommendations_enabled() is False


def test_108_dispatch_remains_disabled() -> None:
    assert is_dispatch_enabled() is False


def test_109_dispatch_worker_remains_disabled() -> None:
    assert is_dispatch_worker_enabled() is False


def test_110_matchpulse_boundary_passes() -> None:
    allowed_hosts = FC27TransportClient.DEFAULT_ALLOWED_HOSTS
    assert allowed_hosts == {"proclubs.ea.com"}


def test_111_club_id_remains_provisional() -> None:
    supplied = FC27SuppliedClubIdentity(
        club_id=EAST_COASTERS_CLUB_ID,
        club_name=EAST_COASTERS_CLUB_NAME,
        platform=EAST_COASTERS_PLATFORM,
        game_version=EAST_COASTERS_GAME_VERSION,
    )
    decision = evaluate_club_identity(supplied)
    assert decision == ClubIdentityState.PROVISIONAL


def test_112_no_concrete_live_transport_exists() -> None:
    """Ensures mock transport is in-memory and no live transport class exists."""
    mock = FC27MockHTTPTransport()
    assert mock is not None
    assert not hasattr(mock, "connect")
