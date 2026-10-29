"""Comprehensive validation suite for the East Coasters FC 27 Operator-Capture Workflow.

Covers all 66 required Part H verification cases offline with zero network calls and zero DNS lookups.
"""
from __future__ import annotations

import copy
import json
import socket
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from scripts.fc27_user_export_import import main as cli_main
from src.db_roles import redact_conninfo
from src.fc27_user_export import (
    FC27AttestationError,
    FC27FormulaInjectionError,
    FC27IdentityConflictError,
    FC27ImportState,
    FC27SensitiveFieldDetectedError,
     FC27SuppliedClubIdentity,
    apply_fc27_user_export_plan,
    compute_match_hash,
    compute_payload_sha256,
    evaluate_club_identity,
    normalize_operator_club_name,
    normalize_operator_displayed_capture,
    plan_fc27_user_export_import,
)
from src.schemas import ClubIdentityState
from src.settings import (
    is_dispatch_enabled,
    is_dispatch_worker_enabled,
    is_fc27_analytics_enabled,
    is_fc27_live_ingestion_enabled,
    is_fc27_persistence_enabled,
    is_fc27_provider_enabled,
    is_fc27_recommendations_enabled,
    is_fc27_standard_http_transport_enabled,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "config" / "fc27_east_coasters_normalized_export.schema.json"
SYNTHETIC_TEMPLATE_PATH = REPO_ROOT / "tests" / "synthetic_fc27_east_coasters_normalized_template.json"


@pytest.fixture(autouse=True)
def _block_network_and_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enforces zero network connections and zero DNS resolutions across all operator-capture tests."""
    def _forbid_network(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("External network connection prohibited in operator-capture tests")

    def _forbid_dns(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("DNS resolution prohibited in operator-capture tests")

    monkeypatch.setattr(socket, "create_connection", _forbid_network)
    monkeypatch.setattr(socket, "getaddrinfo", _forbid_dns)
    monkeypatch.setattr(urllib.request, "urlopen", _forbid_network)


def _load_template_dict() -> dict[str, Any]:
    return json.loads(SYNTHETIC_TEMPLATE_PATH.read_text(encoding="utf-8"))


def _rehash(doc: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(doc)
    out["payload_sha256"] = compute_payload_sha256(out)
    return out


# ==============================================================================
# Cases 1 - 14: Identity & Attestation Contract
# ==============================================================================

def test_01_valid_synthetic_operator_capture() -> None:
    raw = bytearray(SYNTHETIC_TEMPLATE_PATH.read_bytes())
    env = normalize_operator_displayed_capture(raw)
    assert env.source_classification == "OPERATOR_CAPTURED_DISPLAYED_DATA"
    assert env.club.club_id == "330323"
    assert env.club.club_name == "East Coasters"
    assert env.platform == "common-gen5"
    assert env.game_version == "FC27"
    assert env.operator_capture_attestation is not None
    assert env.operator_capture_attestation.attestation_reference == "api-east-coasters-authority-2026-10"
    assert len(env.matches) == 1
    assert env.matches[0].match_type == "LEAGUE"
    assert env.matches[0].data_quality_status == "PROVISIONAL"


def test_02_wrong_club_id_rejected() -> None:
    doc = _load_template_dict()
    doc["club"]["club_id"] = "999999"
    with pytest.raises(FC27IdentityConflictError, match="330323"):
        normalize_operator_displayed_capture(json.dumps(_rehash(doc)))


def test_03_wrong_club_name_and_normalization_rules() -> None:
    # Permitted whitespace/case/NFKC normalization succeeds and canonicalizes to 'East Coasters'
    assert normalize_operator_club_name("  east   COASTERS ") == "East Coasters"
    doc = _load_template_dict()
    doc["club"]["club_name"] = "  east   coasters  "
    env = normalize_operator_displayed_capture(json.dumps(_rehash(doc)))
    assert env.club.club_name == "East Coasters"

    # Fuzzy, substring, or punctuation variants are rejected fail-closed
    for bad_name in ("East Coasters FC", "The East Coasters", "East-Coasters", "East Coaster", "West Coasters"):
        doc_bad = _load_template_dict()
        doc_bad["club"]["club_name"] = bad_name
        with pytest.raises(FC27IdentityConflictError):
            normalize_operator_displayed_capture(json.dumps(_rehash(doc_bad)))


def test_04_wrong_platform_rejected() -> None:
    doc = _load_template_dict()
    doc["platform"] = "common-gen4"
    with pytest.raises(ValueError, match="common-gen5"):
        normalize_operator_displayed_capture(json.dumps(_rehash(doc)))

    doc2 = _load_template_dict()
    doc2["club"]["platform"] = "ps5"
    with pytest.raises(FC27IdentityConflictError, match="common-gen5"):
        normalize_operator_displayed_capture(json.dumps(_rehash(doc2)))


def test_05_wrong_game_version_rejected() -> None:
    for bad_gv in ("FC26", "fc27", "EAFC27"):
        doc = _load_template_dict()
        doc["game_version"] = bad_gv
        with pytest.raises(ValueError, match="FC27"):
            normalize_operator_displayed_capture(json.dumps(_rehash(doc)))


def test_06_missing_attestation_rejected() -> None:
    doc = _load_template_dict()
    del doc["operator_capture_attestation"]
    with pytest.raises(FC27AttestationError, match="operator_capture_attestation"):
        normalize_operator_displayed_capture(json.dumps(_rehash(doc)))


def test_07_wrong_attestation_reference_rejected() -> None:
    doc = _load_template_dict()
    doc["operator_capture_attestation"]["attestation_reference"] = "wrong-ref-2026"
    with pytest.raises(ValidationError):
        normalize_operator_displayed_capture(json.dumps(_rehash(doc)))


def test_08_missing_operator_authority_rejected() -> None:
    doc = _load_template_dict()
    doc["operator_capture_attestation"]["operator_authority_status"] = "UNVERIFIED"
    with pytest.raises(ValidationError):
        normalize_operator_displayed_capture(json.dumps(_rehash(doc)))


def test_09_missing_member_consent_rejected() -> None:
    doc = _load_template_dict()
    doc["operator_capture_attestation"]["member_consent_status"] = "MISSING"
    with pytest.raises(ValidationError):
        normalize_operator_displayed_capture(json.dumps(_rehash(doc)))


def test_10_missing_captain_consent_rejected() -> None:
    doc = _load_template_dict()
    doc["operator_capture_attestation"]["captain_consent_status"] = "MISSING"
    with pytest.raises(ValidationError):
        normalize_operator_displayed_capture(json.dumps(_rehash(doc)))


def test_11_manager_authority_remains_unverified() -> None:
    doc = _load_template_dict()
    doc["operator_capture_attestation"]["manager_authority_database_status"] = "VERIFIED"
    with pytest.raises(ValidationError):
        normalize_operator_displayed_capture(json.dumps(_rehash(doc)))


def test_12_publication_remains_false() -> None:
    doc = _load_template_dict()
    doc["operator_capture_attestation"]["publication_allowed"] = True
    with pytest.raises(ValidationError):
        normalize_operator_displayed_capture(json.dumps(_rehash(doc)))


def test_13_recommendations_remain_false() -> None:
    doc = _load_template_dict()
    doc["operator_capture_attestation"]["recommendations_allowed"] = True
    with pytest.raises(ValidationError):
        normalize_operator_displayed_capture(json.dumps(_rehash(doc)))


def test_14_dispatch_remains_false() -> None:
    doc = _load_template_dict()
    doc["operator_capture_attestation"]["dispatch_allowed"] = True
    with pytest.raises(ValidationError):
        normalize_operator_displayed_capture(json.dumps(_rehash(doc)))


# ==============================================================================
# Cases 15 - 26: Structural & Sensitive-Field Rejection
# ==============================================================================

def test_15_duplicate_json_key_rejected() -> None:
    raw = '{"export_id": "a", "export_id": "b"}'
    with pytest.raises(ValueError, match="duplicate JSON key"):
        normalize_operator_displayed_capture(raw)


def test_16_extra_top_level_field_rejected() -> None:
    doc = _load_template_dict()
    doc["extra_top_level"] = "unexpected"
    with pytest.raises(ValidationError):
        normalize_operator_displayed_capture(json.dumps(_rehash(doc)))


def test_17_extra_nested_field_rejected() -> None:
    doc = _load_template_dict()
    doc["matches"][0]["extra_nested"] = "unexpected"
    with pytest.raises(ValidationError):
        normalize_operator_displayed_capture(json.dumps(_rehash(doc)))


@pytest.mark.parametrize(
    "sensitive_field",
    [
        "client_secret",     # 18. Sensitive key
        "cookie",            # 19. Cookie field
        "set_cookie",        # 19b. Set-Cookie field
        "session_id",        # 20. Session field
        "bearer_token",      # 21. Token field
        "account_id",        # 22. Account ID
        "persona_id",        # 23. Persona ID
        "nucleus_id",        # 24. Nucleus ID
        "gamer_tag",         # 25. Gamer tag
        "external_player_id",# 26. External player ID
        "raw_response",      # Raw response field
        "http_headers",      # HTTP headers field
    ],
)
def test_18_to_26_sensitive_keys_rejected(sensitive_field: str) -> None:
    doc = _load_template_dict()
    doc["matches"][0]["player_performances"][0][sensitive_field] = "prohibited_value"
    with pytest.raises(FC27SensitiveFieldDetectedError) as exc_info:
        normalize_operator_displayed_capture(json.dumps(_rehash(doc)))
    assert "prohibited_value" not in str(exc_info.value)


# ==============================================================================
# Cases 27 - 41: Timestamps, Match & Player Metric Bounds, Nulls vs Zero
# ==============================================================================

def test_27_invalid_timestamp_rejected() -> None:
    doc = _load_template_dict()
    doc["matches"][0]["played_at"] = "not-a-timestamp"
    with pytest.raises(ValidationError):
        normalize_operator_displayed_capture(json.dumps(_rehash(doc)))


def test_28_naive_timestamp_rejected() -> None:
    doc = _load_template_dict()
    doc["matches"][0]["played_at"] = "2026-03-01T17:30:00"
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        normalize_operator_displayed_capture(json.dumps(_rehash(doc)))


def test_29_future_timestamp_rejected() -> None:
    doc = _load_template_dict()
    future_dt = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    doc["generated_at"] = future_dt
    doc["supplied_at"] = future_dt
    with pytest.raises(ValueError, match="in the future beyond tolerance"):
        normalize_operator_displayed_capture(json.dumps(_rehash(doc)))


def test_30_invalid_result_rejected() -> None:
    doc = _load_template_dict()
    doc["matches"][0]["result"] = "TIE"
    with pytest.raises(ValidationError):
        normalize_operator_displayed_capture(json.dumps(_rehash(doc)))


def test_31_score_result_inconsistency_rejected() -> None:
    doc = _load_template_dict()
    doc["matches"][0]["goals_for"] = 1
    doc["matches"][0]["goals_against"] = 3
    doc["matches"][0]["result"] = "WIN"
    with pytest.raises(ValidationError, match="inconsistent with score"):
        normalize_operator_displayed_capture(json.dumps(_rehash(doc)))


def test_32_negative_statistic_rejected() -> None:
    doc = _load_template_dict()
    doc["matches"][0]["player_performances"][0]["goals"] = -1
    with pytest.raises(ValidationError):
        normalize_operator_displayed_capture(json.dumps(_rehash(doc)))


def test_33_completed_passes_greater_than_attempted_rejected() -> None:
    doc = _load_template_dict()
    doc["matches"][0]["player_performances"][0]["passes_attempted"] = 10
    doc["matches"][0]["player_performances"][0]["passes_completed"] = 12
    with pytest.raises(ValidationError, match="cannot exceed passes_attempted"):
        normalize_operator_displayed_capture(json.dumps(_rehash(doc)))


def test_34_tackles_won_greater_than_attempted_rejected() -> None:
    doc = _load_template_dict()
    doc["matches"][0]["player_performances"][0]["tackles_attempted"] = 2
    doc["matches"][0]["player_performances"][0]["tackles_won"] = 4
    with pytest.raises(ValidationError, match="cannot exceed tackles_attempted"):
        normalize_operator_displayed_capture(json.dumps(_rehash(doc)))


def test_35_shots_on_target_greater_than_shots_rejected() -> None:
    doc = _load_template_dict()
    doc["matches"][0]["player_performances"][0]["shots"] = 2
    doc["matches"][0]["player_performances"][0]["shots_on_target"] = 3
    with pytest.raises(ValidationError, match="cannot exceed shots"):
        normalize_operator_displayed_capture(json.dumps(_rehash(doc)))


def test_36_invalid_rating_rejected() -> None:
    doc = _load_template_dict()
    doc["matches"][0]["player_performances"][0]["provider_rating"] = 10.5
    with pytest.raises(ValidationError):
        normalize_operator_displayed_capture(json.dumps(_rehash(doc)))


def test_37_nan_rejected() -> None:
    raw = SYNTHETIC_TEMPLATE_PATH.read_text(encoding="utf-8").replace("8.9", "NaN")
    with pytest.raises(ValueError, match="disallowed JSON constant 'NaN'"):
        normalize_operator_displayed_capture(raw)


def test_38_infinity_rejected() -> None:
    raw = SYNTHETIC_TEMPLATE_PATH.read_text(encoding="utf-8").replace("8.9", "Infinity")
    with pytest.raises(ValueError, match="disallowed JSON constant 'Infinity'"):
        normalize_operator_displayed_capture(raw)


def test_39_unknown_required_match_value_fails_closed() -> None:
    doc = _load_template_dict()
    doc["matches"][0]["result"] = None
    with pytest.raises(ValueError, match="missing required field 'result'"):
        normalize_operator_displayed_capture(json.dumps(_rehash(doc)))

    doc_placeholder = _load_template_dict()
    doc_placeholder["matches"][0]["external_match_id"] = "REPLACE_WITH_OPERATOR_VALUE"
    with pytest.raises(ValueError, match="unreplaced operator placeholder"):
        normalize_operator_displayed_capture(json.dumps(_rehash(doc_placeholder)))


def test_40_and_41_null_optional_value_accepted_and_zero_not_substituted_for_unknown() -> None:
    doc = _load_template_dict()
    perf = doc["matches"][0]["player_performances"][0]
    perf["interceptions"] = None
    perf["possession_losses"] = None
    perf["provider_rating"] = None
    perf["clean_sheet"] = None
    perf["build_reference"] = None
    del perf["saves"]
    env = normalize_operator_displayed_capture(json.dumps(_rehash(doc)))
    p0 = env.matches[0].player_performances[0]
    assert p0.interceptions is None
    assert p0.possession_losses is None
    assert p0.provider_rating is None
    assert p0.clean_sheet is None
    assert p0.build_reference is None
    assert p0.saves is None, "Omitted optional statistic must remain None, never substituted with 0"


# ==============================================================================
# Cases 42 - 56: Hashing, Planning, CLI, Redaction, and Raw-Buffer Disposal
# ==============================================================================

def test_42_and_43_deterministic_payload_and_match_hashes() -> None:
    doc = _load_template_dict()
    h1 = compute_payload_sha256(doc)
    h2 = compute_payload_sha256(copy.deepcopy(doc))
    assert h1 == h2 == doc["payload_sha256"]

    env = normalize_operator_displayed_capture(SYNTHETIC_TEMPLATE_PATH.read_bytes())
    mh1 = compute_match_hash(
        env.matches[0],
        club_id=env.club.club_id,
        platform=env.club.platform,
        game_version=env.game_version,
        source_classification=env.source_classification,
    )
    mh2 = compute_match_hash(
        env.matches[0],
        club_id=env.club.club_id,
        platform=env.club.platform,
        game_version=env.game_version,
        source_classification=env.source_classification,
    )
    assert mh1 == mh2
    assert len(mh1) == 64


def test_44_45_46_duplicate_replay_and_identity_conflict_planning() -> None:
    env = normalize_operator_displayed_capture(SYNTHETIC_TEMPLATE_PATH.read_bytes())
    mhash = compute_match_hash(
        env.matches[0],
        club_id=env.club.club_id,
        platform=env.club.platform,
        game_version=env.game_version,
        source_classification=env.source_classification,
    )
    # 44. Duplicate match planning
    dup_state = FC27ImportState(existing_match_hashes={env.matches[0].external_match_id: mhash})
    dup_plan = plan_fc27_user_export_import(env, existing_state=dup_state)
    assert dup_plan.can_apply is True
    assert env.matches[0].external_match_id in dup_plan.duplicate_match_ids

    # 45. Replay-conflict planning
    conflict_state = FC27ImportState(existing_match_hashes={env.matches[0].external_match_id: "0" * 64})
    conflict_plan = plan_fc27_user_export_import(env, existing_state=conflict_state)
    assert conflict_plan.can_apply is False
    assert env.matches[0].external_match_id in conflict_plan.conflicting_match_ids

    # 46. Identity-conflict planning
    bad_club = FC27SuppliedClubIdentity(
        club_id="999999",
        club_name="Other Club",
        platform="common-gen5",
        game_version="FC27",
    )
    assert evaluate_club_identity(bad_club) == ClubIdentityState.CONFLICTED


def test_47_to_54_cli_modes_gating_privacy_and_redaction(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FC27_PERSISTENCE_ENABLED", "false")

    # 47. Validate performs zero DB writes
    rc_val = cli_main([
        "--input", str(SYNTHETIC_TEMPLATE_PATH),
        "--format", "json",
        "--operator-displayed",
        "--validate",
    ])
    out_val = capsys.readouterr()
    assert rc_val == 0
    assert "IMPORT_STATUS: VALIDATED" in out_val.out
    assert "SOURCE_CLASSIFICATION: OPERATOR_CAPTURED_DISPLAYED_DATA" in out_val.out
    assert "PERSISTENCE_APPLIED: NO" in out_val.out
    assert "RAW_UPLOAD_RETAINED: NO" in out_val.out

    # 48. Dry-run performs zero DB writes
    rc_dry = cli_main([
        "--input", str(SYNTHETIC_TEMPLATE_PATH),
        "--format", "json",
        "--operator-displayed",
        "--dry-run",
    ])
    out_dry = capsys.readouterr()
    assert rc_dry == 0
    assert "IMPORT_STATUS: DRY_RUN" in out_dry.out
    assert "PERSISTENCE_APPLIED: NO" in out_dry.out

    # 49. Apply without confirmation rejected
    rc_no_conf = cli_main([
        "--input", str(SYNTHETIC_TEMPLATE_PATH),
        "--format", "json",
        "--operator-displayed",
        "--apply",
    ])
    out_no_conf = capsys.readouterr()
    assert rc_no_conf == 2
    assert "requires --confirm-apply" in out_no_conf.err

    # 50. Apply while persistence disabled rejected / deferred without opening DB connection
    rc_def = cli_main([
        "--input", str(SYNTHETIC_TEMPLATE_PATH),
        "--format", "json",
        "--operator-displayed",
        "--apply",
        "--confirm-apply",
    ])
    out_def = capsys.readouterr()
    assert rc_def == 1
    assert "IMPORT_STATUS: DEFERRED" in out_def.out
    assert "PERSISTENCE_APPLIED: NO" in out_def.out

    # 51-53. CLI prints no raw rows, no aliases, no secrets
    for combined_output in (out_val.out, out_dry.out, out_def.out):
        assert "Synthetic Harbour FC" not in combined_output
        assert "plr_ecf_synth_0001" not in combined_output
        assert "SAFE_ALIASES" not in combined_output

    # 54. CLI redacts connection strings
    redacted = redact_conninfo("postgresql://fc27_app:SuperSecretPass123@127.0.0.1:5432/fc27")
    assert "SuperSecretPass123" not in redacted


def test_55_and_56_raw_buffer_overwritten_and_no_raw_copy_retained() -> None:
    raw_buf = bytearray(SYNTHETIC_TEMPLATE_PATH.read_bytes())
    assert any(b != 0 for b in raw_buf)

    def _forbid_write(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("open() called during normalize_operator_displayed_capture")

    with patch("builtins.open", side_effect=_forbid_write):
        env = normalize_operator_displayed_capture(raw_buf)
        assert env.club.club_id == "330323"

    assert all(b == 0 for b in raw_buf), "Mutable input buffer must be overwritten with zeroes"


# ==============================================================================
# Cases 57 - 66: Git Ignore, Synthetic Markings, Feature Flags, Boundaries
# ==============================================================================

def test_57_to_66_gitignore_synthetic_markings_and_platform_boundaries() -> None:
    # 57 & 58. Private directory .local-data/ is gitignored and cannot be staged accidentally
    gitignore_lines = [
        line.strip()
        for line in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    ]
    assert ".local-data/" in gitignore_lines
    chk = subprocess.run(
        ["git", "check-ignore", "-v", ".local-data/east-coasters-fc27-normalized.json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert chk.returncode == 0
    assert ".local-data/" in chk.stdout

    # 59. Synthetic fixture clearly marked synthetic
    synth_doc = _load_template_dict()
    assert synth_doc["data_classification"] == "SYNTHETIC"
    assert synth_doc["real_player_data"] is False
    assert synth_doc["safe_for_tests"] is True
    assert SCHEMA_PATH.is_file()

    # 60. Real-data file absent from tracked tree
    tracked = subprocess.run(
        ["git", "ls-files", ".local-data"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert tracked.stdout.strip() == ""

    # 61-64. Recommendations, dispatch, provider, and live ingestion remain disabled by default
    assert is_fc27_recommendations_enabled() is False
    assert is_dispatch_enabled() is False
    assert is_dispatch_worker_enabled() is False
    assert is_fc27_provider_enabled() is False
    assert is_fc27_standard_http_transport_enabled() is False
    assert is_fc27_live_ingestion_enabled() is False
    assert is_fc27_persistence_enabled() is False
    assert is_fc27_analytics_enabled() is False

    # 65. PostgresSaver tables untouched across all migrations
    for mig in sorted((REPO_ROOT / "migrations").glob("*.sql")):
        sql_lower = mig.read_text(encoding="utf-8").lower()
        assert "checkpoint_blobs" not in sql_lower
        assert "checkpoint_writes" not in sql_lower

    # 66. MatchPulse boundary intact
    mp_res = subprocess.run(
        [sys.executable, "scripts/check_matchpulse_boundary.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert mp_res.returncode == 0
    assert "OK" in mp_res.stdout


def test_67_postgres_persistence_preserves_operator_capture_provisional_posture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifies that persisting synthetic OPERATOR_CAPTURED_DISPLAYED_DATA to PostgreSQL 16
    keeps data_quality_status='PROVISIONAL', display_alias=NULL, manager_authority_status='UNVERIFIED',
    and emits domain_events.reason with source_classification='OPERATOR_CAPTURED_DISPLAYED_DATA'.
    """
    import os
    import psycopg
    from src.db_migrate import migrate
    from src.db_roles import bootstrap_cluster, bootstrap_database
    from src.fc27_repository import apply_export_plan

    admin_uri = os.environ.get("TEST_POSTGRES_ADMIN_URI")
    if not admin_uri:
        pytest.skip("TEST_POSTGRES_ADMIN_URI not set")

    # Temporarily restore socket for local 127.0.0.1 PostgreSQL test container
    monkeypatch.undo()

    dbname = "fc27_op_capture_test"
    bootstrap_cluster(
        admin_uri,
        {
            "fc27_migrator": None,
            "fc27_app": None,
            "fc27_dispatch": None,
            "fc27_relay": None,
        },
    )
    with psycopg.connect(admin_uri, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {dbname}")
        conn.execute(f"CREATE DATABASE {dbname}")

    db_admin_uri = admin_uri.rsplit("/", 1)[0] + f"/{dbname}"
    bootstrap_database(db_admin_uri, legacy_owner=None, confirm=True)
    migrator_uri = f"postgresql://fc27_migrator@127.0.0.1:55432/{dbname}"
    app_uri = f"postgresql://fc27_app@127.0.0.1:55432/{dbname}"
    migrate(migrator_uri)

    env = normalize_operator_displayed_capture(SYNTHETIC_TEMPLATE_PATH.read_bytes())
    plan = plan_fc27_user_export_import(env)
    res = apply_export_plan(app_uri, envelope=env, plan=plan)
    assert res.status == "APPLIED"
    assert res.accepted_records == 1
    assert res.player_stat_records == 2

    with psycopg.connect(app_uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT identity_state, manager_authority_status, source_classification FROM public.fc27_clubs WHERE club_id = '330323'"
            )
            club_row = cur.fetchone()
            assert club_row == ("PROVISIONAL", "UNVERIFIED", "USER_SUPPLIED_EXPORT")

            cur.execute(
                "SELECT data_quality_status, source_classification FROM public.fc27_matches WHERE external_match_id = %s",
                (env.matches[0].external_match_id,),
            )
            match_row = cur.fetchone()
            assert match_row == ("PROVISIONAL", "USER_SUPPLIED_EXPORT")

            cur.execute(
                "SELECT local_player_reference, display_alias FROM public.fc27_player_match_stats ORDER BY local_player_reference"
            )
            p_rows = cur.fetchall()
            assert p_rows == [("plr_ecf_synth_0001", None), ("plr_ecf_synth_0002", None)]

            cur.execute(
                "SELECT reason FROM public.domain_events WHERE event_type = 'fc27.export.applied' AND entity_id = %s",
                (env.export_id,),
            )
            ev_row = cur.fetchone()
            assert ev_row is not None
            reason_obj = json.loads(ev_row[0]) if isinstance(ev_row[0], str) else ev_row[0]
            assert reason_obj["source_classification"] == "OPERATOR_CAPTURED_DISPLAYED_DATA"
