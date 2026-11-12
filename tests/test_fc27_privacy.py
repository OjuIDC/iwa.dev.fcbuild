"""Focused unit tests for retained FC27 privacy helpers (`src/fc27_privacy.py`).

Covers:
1. Empty input rejection
2. Whitespace normalization
3. Casefolding normalization
4. Unicode NFC normalization
5. Stable deterministic player and match pseudonym output
6. Distinct input gives distinct output
7. Same normalized input gives identical output
8. Sensitive-key detection (camelCase, snake_case, kebab-case, compound tokens)
9. Nested mapping sensitive-field detection
10. Nested list sensitive-field detection
11. Plaintext gamertag rejection
12. Raw EA ID rejection (personaId, nucleusId, blazeId, external_player_id)
13. Discord mention detection and suppression (`@everyone`, `@here`, `<@id>`, `<@&id>`)
14. Control-character rejection (NUL, CR, LF, TAB, Unicode Cc)
15. Oversized input rejection and nesting depth bounds
16. Error text and redaction contain no secret value
17. No raw identity persistence and collision detection
18. Constant-time SHA-256 digest comparison and canonical hashing
19. Single authoritative privacy implementation across retained `src/` modules (`DUPLICATE_PRIVACY_HELPERS=0`)
"""

from __future__ import annotations

import ast
import unicodedata
from pathlib import Path

import pytest

from src.fc27_privacy import (
    EAST_COASTERS_CLUB_ID,
    FC27ContractError,
    FC27FingerprintCollisionError,
    FC27FormulaInjectionError,
    FC27PrivacyError,
    FC27PseudonymCollisionDetector,
    FC27PseudonymCollisionError,
    FC27SensitiveFieldDetectedError,
    FC27ValidationError,
    HEX_SHA256_RE,
    PSEUDONYM_MATCH_REF_RE,
    PSEUDONYM_PLAYER_REF_RE,
    assert_no_sensitive_fields,
    compare_digest_constant_time,
    compute_alias_hmac_fingerprint,
    compute_match_hash,
    compute_payload_sha256,
    contains_discord_mention,
    contains_prohibited_url,
    escape_csv_formula_cell,
    is_sensitive_key,
    normalize_club_name,
    normalize_display_label,
    normalize_hmac_input,
    normalize_identifier,
    normalize_whitespace_and_casefold,
    pseudonymize_east_coasters_identifier,
    pseudonymize_match_ref,
    redact_sensitive_text,
    reject_control_characters,
    reject_prohibited_url,
    scan_for_sensitive_fields,
    suppress_discord_mentions,
    validate_safe_display_alias,
    validate_safe_text,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_01_empty_input_rejected() -> None:
    """Empty or whitespace-only inputs fail closed across normalization and pseudonymization."""
    for empty_val in ("", "   ", "\t\n"):
        with pytest.raises(FC27ValidationError):
            normalize_whitespace_and_casefold(empty_val, field_name="test_field")
        with pytest.raises(FC27ValidationError):
            pseudonymize_east_coasters_identifier(empty_val)
        with pytest.raises(FC27ValidationError):
            pseudonymize_match_ref(empty_val)
        with pytest.raises(FC27ValidationError):
            validate_safe_text(empty_val, "label")


def test_02_whitespace_normalization() -> None:
    """Internal whitespace runs are collapsed and leading/trailing spaces stripped."""
    assert normalize_whitespace_and_casefold("  East    Coasters   FC  ") == "east coasters fc"
    assert normalize_club_name("  East   Coasters ") == "east coasters"
    assert normalize_display_label("  Midfield   Control  ") == "Midfield Control"


def test_03_casefolding_normalization() -> None:
    """Casefolding normalizes ASCII and Unicode uppercase forms consistently."""
    assert normalize_whitespace_and_casefold("EAST COASTERS") == "east coasters"
    assert normalize_whitespace_and_casefold("Straße FC") == "strasse fc"
    assert normalize_hmac_input("East Coasters") == normalize_hmac_input("  EAST   COASTERS ")


def test_04_unicode_nfc_normalization() -> None:
    """Decomposed (NFD) and composed (NFC) Unicode inputs normalize to identical outputs."""
    composed = "Café_Player_1"
    decomposed = unicodedata.normalize("NFD", composed)
    assert composed != decomposed
    assert normalize_whitespace_and_casefold(composed) == normalize_whitespace_and_casefold(decomposed)
    assert pseudonymize_east_coasters_identifier(composed) == pseudonymize_east_coasters_identifier(
        decomposed
    )


def test_05_stable_pseudonym_output() -> None:
    """Player and match pseudonymization produce deterministic, bounded references."""
    p_ref = pseudonymize_east_coasters_identifier("member_slot_alpha", club_id=EAST_COASTERS_CLUB_ID)
    assert PSEUDONYM_PLAYER_REF_RE.fullmatch(p_ref)
    assert pseudonymize_east_coasters_identifier(p_ref) == p_ref

    m_ref = pseudonymize_match_ref("990001", club_id=EAST_COASTERS_CLUB_ID)
    assert PSEUDONYM_MATCH_REF_RE.fullmatch(m_ref)
    assert m_ref.startswith("mref_ecf_")
    assert pseudonymize_match_ref(m_ref) == m_ref


def test_06_distinct_input_gives_distinct_output() -> None:
    """Distinct source identifiers produce distinct pseudonyms; non-East Coasters club IDs fail closed."""
    p1 = pseudonymize_east_coasters_identifier("player_token_001")
    p2 = pseudonymize_east_coasters_identifier("player_token_002")
    assert p1 != p2
    with pytest.raises(FC27ContractError):
        pseudonymize_east_coasters_identifier("player_token_001", club_id="999999")

    m1 = pseudonymize_match_ref("match_1001")
    m2 = pseudonymize_match_ref("match_1002")
    assert m1 != m2


def test_07_same_normalized_input_gives_same_output() -> None:
    """Inputs that normalize to the same casefolded/whitespace-collapsed token yield identical pseudonyms."""
    p_a = pseudonymize_east_coasters_identifier("  Player   Alpha  ")
    p_b = pseudonymize_east_coasters_identifier("player alpha")
    p_c = pseudonymize_east_coasters_identifier("PLAYER ALPHA")
    assert p_a == p_b == p_c


def test_08_sensitive_key_detection() -> None:
    """Sensitive keys are detected across camelCase, snake_case, kebab-case, and compound tokens."""
    for key in (
        "gamertag",
        "gamerTag",
        "gamer_tag",
        "gamer-tag",
        "personaId",
        "persona_id",
        "nucleusId",
        "blazeId",
        "playerName",
        "proName",
        "externalPlayerId",
        "source_player_id",
        "accessToken",
        "refresh_token",
        "session_token",
        "rawPayload",
        "raw_response",
        "authorization",
        "set-cookie",
        "privateKey",
    ):
        assert is_sensitive_key(key) is True, f"Expected sensitive key for {key!r}"

    for safe_key in (
        "match_id",
        "club_id",
        "goals_for",
        "passes_completed",
        "pseudonym_id",
        "local_player_reference",
    ):
        assert is_sensitive_key(safe_key, allow_local_player_reference=True) is False


def test_09_nested_mapping_detection() -> None:
    """Recursive scanning detects sensitive keys inside deeply nested dicts."""
    payload = {
        "club": {
            "id": "330323",
            "metadata": {
                "audit": {
                    "sessionToken": "redacted",
                }
            },
        }
    }
    violations = scan_for_sensitive_fields(payload)
    assert violations == ["club.metadata.audit.sessionToken"]
    with pytest.raises(FC27SensitiveFieldDetectedError) as exc_info:
        assert_no_sensitive_fields(payload)
    assert "club.metadata.audit.sessionToken" in str(exc_info.value)


def test_10_nested_list_detection() -> None:
    """Recursive scanning detects sensitive keys inside nested lists and tuples."""
    payload = {
        "matches": [
            {"match_id": "m1", "players": [{"pseudonym_id": "plr_ecf_001122334455"}]},
            {"match_id": "m2", "players": [{"raw_response": "{}"}]},
        ]
    }
    violations = scan_for_sensitive_fields(payload)
    assert violations == ["matches[1].players[0].raw_response"]


def test_11_plaintext_gamertag_rejection() -> None:
    """Plaintext gamertag keys (`gamertag`, `proName`, `playerName`, `personaName`) are rejected."""
    for field_name in ("gamertag", "gamer_tag", "proName", "playerName", "personaName"):
        payload = {"match_id": "1001", "player": {field_name: "PlaintextGamer99"}}
        violations = scan_for_sensitive_fields(payload)
        assert violations == [f"player.{field_name}"]


def test_12_raw_ea_id_rejection() -> None:
    """Raw EA account/player identifiers (`personaId`, `nucleusId`, `blazeId`, `eaId`) are rejected."""
    for ea_key in ("personaId", "nucleusId", "blazeId", "eaId", "external_player_id", "source_player_id"):
        payload = {"players": [{ea_key: "10099887766"}]}
        violations = scan_for_sensitive_fields(payload)
        assert violations == [f"players[0].{ea_key}"]


def test_13_discord_mention_suppression_and_url_rejection() -> None:
    """Discord mass/user/role mentions are detected and neutralized; URLs are detected and rejected."""
    for raw_mention in ("hello @everyone", "ping @here now", "user <@123456789>", "role <@&987654321>"):
        assert contains_discord_mention(raw_mention) is True
        suppressed = suppress_discord_mentions(raw_mention)
        assert contains_discord_mention(suppressed) is False
        assert "[mention-suppressed]" in suppressed
        with pytest.raises(FC27PrivacyError):
            normalize_display_label(raw_mention)

    for url_text in ("https://ea.example.com/api", "http://127.0.0.1:8000", "visit www.example.org"):
        assert contains_prohibited_url(url_text) is True
        with pytest.raises(FC27PrivacyError):
            reject_prohibited_url(url_text)
        with pytest.raises(FC27PrivacyError):
            normalize_display_label(url_text)


def test_14_control_character_and_formula_rejection() -> None:
    """Control characters and spreadsheet formula prefixes fail closed."""
    for bad_ctrl in ("bad\x00null", "line\nbreak", "carriage\rreturn", "tab\tchar", "del\x7fchar"):
        with pytest.raises(FC27ValidationError):
            reject_control_characters(bad_ctrl)
        with pytest.raises(FC27ValidationError):
            validate_safe_text(bad_ctrl, "field")

    for formula in ("=SUM(A1:A2)", "+CMD|' /C calc'!A0", "-2+3", "@HYPERLINK(\"http://bad\")"):
        with pytest.raises(FC27FormulaInjectionError):
            validate_safe_text(formula, "note", reject_formula=True)
        escaped = escape_csv_formula_cell(formula)
        assert escaped.startswith("'")


def test_15_oversized_input_and_depth_rejected() -> None:
    """Oversized identifiers, labels, and excessively deep structures fail closed."""
    with pytest.raises(FC27ValidationError, match="exceeds maximum length"):
        normalize_identifier("a" * 200, "id_field", max_len=128)
    with pytest.raises(FC27ValidationError, match="exceeds maximum length"):
        pseudonymize_east_coasters_identifier("x" * 300)
    with pytest.raises(FC27ValidationError, match="display_alias"):
        validate_safe_display_alias("x" * 60)

    deep: dict[str, object] = {"leaf": "ok"}
    for _ in range(40):
        deep = {"child": deep}
    with pytest.raises(FC27ValidationError, match="nesting depth"):
        scan_for_sensitive_fields(deep)


def test_16_error_text_and_redaction_contain_no_secret_value() -> None:
    """Privacy exceptions and redaction helpers never leak raw secrets, tokens, or connection URIs."""
    secret_value = "super_secret_gamertag_or_password_999"
    payload = {"nested": {"password": secret_value}}
    with pytest.raises(FC27SensitiveFieldDetectedError) as exc_info:
        assert_no_sensitive_fields(payload)
    assert secret_value not in str(exc_info.value)
    assert "nested.password" in str(exc_info.value)

    raw_diag = (
        "Failed connecting to postgresql://fc27_app:my_db_pw_123@127.0.0.1:5432/fc27 "
        "with Authorization: Bearer tok_abc123xyz and Bot discord_secret_tok_777"
    )
    redacted = redact_sensitive_text(raw_diag, extra_secrets=("custom_secret_val",))
    assert "my_db_pw_123" not in redacted
    assert "tok_abc123xyz" not in redacted
    assert "discord_secret_tok_777" not in redacted
    assert "<REDACTED_POSTGRES_URI>" in redacted
    assert "<REDACTED_TOKEN>" in redacted


def test_17_no_raw_identity_persistence_and_collision_detector() -> None:
    """Collision detector stores only SHA-256 digests of normalized source tokens and detects collisions."""
    detector = FC27PseudonymCollisionDetector()
    p1 = pseudonymize_east_coasters_identifier("AlphaMember")
    assert detector.register(p1, "AlphaMember") == p1
    assert detector.register(p1, "  alphamember ") == p1
    assert detector.count == 1
    assert "AlphaMember" not in str(detector._pseudonym_to_source_digest)
    assert "alphamember" not in str(detector._pseudonym_to_source_digest)

    with pytest.raises(FC27PseudonymCollisionError):
        detector.register(p1, "DifferentMemberForcingCollision")


def test_18_constant_time_digest_and_canonical_hashing() -> None:
    """Canonical hashing is key-order independent and digest comparison is constant-time."""
    h1 = compute_payload_sha256({"b": 2, "a": [1, {"d": 4.123456, "c": 3}]})
    h2 = compute_payload_sha256({"a": [1, {"c": 3, "d": 4.123456}], "b": 2})
    assert h1 == h2
    assert compare_digest_constant_time(h1, h2) is True
    assert compare_digest_constant_time(h1, "0" * 64) is False
    assert compare_digest_constant_time(h1, "not-a-sha256") is False

    m1 = compute_match_hash({"match_id": "m1", "goals_for": 2, "match_hash": "ignored_1"})
    m2 = compute_match_hash({"goals_for": 2, "match_id": "m1", "match_hash": "ignored_2"})
    assert m1 == m2


def test_19_single_authoritative_privacy_implementation_in_src() -> None:
    """Ensure retained `src/` modules do not duplicate core privacy helper definitions (`DUPLICATE_PRIVACY_HELPERS=0`)."""
    authoritative_defs = {
        "scan_for_sensitive_fields",
        "pseudonymize_east_coasters_identifier",
        "pseudonymize_match_ref",
        "escape_csv_formula_cell",
        "FC27PseudonymCollisionDetector",
        "compute_alias_hmac_fingerprint",
    }
    duplicates: list[str] = []
    for py_path in sorted((REPO_ROOT / "src").rglob("*.py")):
        if py_path.name == "fc27_privacy.py":
            continue
        tree = ast.parse(py_path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name in authoritative_defs:
                    duplicates.append(f"{py_path.relative_to(REPO_ROOT).as_posix()}:{node.name}")
    assert duplicates == []


def test_20_alias_hmac_fingerprint_determinism_and_key_validation() -> None:
    """20. C1 alias HMAC-SHA256 fingerprint computation is deterministic, normalizes whitespace/case/NFC, and rejects short/empty keys."""
    valid_key = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

    n1 = normalize_hmac_input("  East   Coaster_99  ")
    n2 = normalize_hmac_input("east coaster_99")
    assert n1 == n2 == b"east coaster_99"

    h1 = compute_alias_hmac_fingerprint("  East   Coaster_99  ", valid_key)
    h2 = compute_alias_hmac_fingerprint("EAST COASTER_99", valid_key)
    h3 = compute_alias_hmac_fingerprint("OtherPlayer_01", valid_key)
    assert h1 == h2
    assert h1 != h3
    assert HEX_SHA256_RE.fullmatch(h1)
    assert valid_key not in h1
    assert "coaster" not in h1

    for bad_key in (None, "", "   ", "short_key_under_32_bytes", b"short_bytes"):
        with pytest.raises(FC27ValidationError):
            compute_alias_hmac_fingerprint("EastCoaster", bad_key)  # type: ignore[arg-type]

    assert issubclass(FC27FingerprintCollisionError, FC27PrivacyError)
