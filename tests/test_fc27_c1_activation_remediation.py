"""Regression tests for the FC27 C1 activation-readiness remediation.

Covers the versioned NFKC alias-HMAC contract and complete canonical idempotency
equivalence for the minimal versioned ``post_new_results`` payload. These tests use
synthetic values only, never contact a provider or Discord, and only write to a
disposable PostgreSQL instance supplied through ``TEST_POSTGRES_URI``.
"""

from __future__ import annotations

import json
import unicodedata
from datetime import UTC, datetime
from typing import Any

import psycopg
import psycopg.errors
import pytest

from src.fc27_polling_worker import (
    FC27PollingTableAccessDeniedError,
    open_guarded_polling_connection,
)
from src.fc27_privacy import (
    ALIAS_HMAC_NORMALIZATION_VERSION,
    HEX_SHA256_RE,
    FC27ValidationError,
    compute_alias_hmac_fingerprint,
    pseudonymize_match_ref,
    scan_for_sensitive_fields,
)
from src.fc27_repository import (
    POST_NEW_RESULTS_EVENT_TYPE,
    POST_NEW_RESULTS_PAYLOAD_KEYS,
    POST_NEW_RESULTS_SCHEMA_VERSION,
    FC27AliasFingerprintObservation,
    FC27JobIdempotencyConflictError,
    FC27SuppliedMatch,
    _build_post_new_results_payload,
    _discord_job_payloads_equivalent,
    apply_export_plan,
    enqueue_post_new_results_jobs,
    fetch_discord_jobs,
    fetch_export_batch_status,
    fetch_player_alias_fingerprints,
    parse_fc27_user_export_json,
)
from tests.test_fc27_persistence_and_queries import make_synthetic_export_dict

VALID_KEY = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
CLUB_ID = "330323"
PLATFORM = "common-gen5"
GAME_VERSION = "FC27"


def _match(external_match_id: str = "930001") -> FC27SuppliedMatch:
    return FC27SuppliedMatch(
        external_match_id=external_match_id,
        played_at=datetime(2026, 11, 13, 12, 0, tzinfo=UTC),
        result="WIN",
        goals_for=2,
        goals_against=1,
        opponent_name="Synthetic Opponent",
    )


def _payload(
    external_match_id: str = "930001",
    *,
    match_type: str = "leagueMatch",
    club_id: str = CLUB_ID,
    platform: str = PLATFORM,
    game_version: str = GAME_VERSION,
) -> dict[str, Any]:
    _, payload = _build_post_new_results_payload(
        _match(external_match_id),
        club_id=club_id,
        platform=platform,
        game_version=game_version,
        match_type_override=match_type,
    )
    return payload


def _idempotency_key(external_match_id: str = "930001") -> str:
    return f"fc27:post_new_results:{pseudonymize_match_ref(external_match_id, club_id=CLUB_ID)}"


def _seed_job(admin_uri: str, key: str, payload: dict[str, Any]) -> None:
    with psycopg.connect(admin_uri) as conn:
        conn.execute(
            """
            INSERT INTO public.fc27_discord_jobs (
                job_type, idempotency_key, payload, state, attempt_count
            ) VALUES (
                'post_new_results', %s, %s::jsonb, 'pending', 0
            )
            """,
            (key, json.dumps(payload)),
        )
        conn.commit()


def _counts(admin_uri: str) -> dict[str, int]:
    with psycopg.connect(admin_uri) as conn:
        return {
            "matches": conn.execute(
                "SELECT count(*) FROM public.fc27_matches"
            ).fetchone()[0],
            "stats": conn.execute(
                "SELECT count(*) FROM public.fc27_player_match_stats"
            ).fetchone()[0],
            "fingerprints": conn.execute(
                "SELECT count(*) FROM public.fc27_player_alias_fingerprints"
            ).fetchone()[0],
            "jobs": conn.execute(
                "SELECT count(*) FROM public.fc27_discord_jobs"
            ).fetchone()[0],
            "batches": conn.execute(
                "SELECT count(*) FROM public.fc27_export_batches"
            ).fetchone()[0],
        }


def _stored_payload(admin_uri: str, key: str) -> dict[str, Any]:
    with psycopg.connect(admin_uri) as conn:
        row = conn.execute(
            "SELECT payload FROM public.fc27_discord_jobs WHERE idempotency_key = %s",
            (key,),
        ).fetchone()
    assert row is not None
    return dict(row[0])


def _enqueue(poll_writer_uri: str, match_type: str = "leagueMatch"):
    return enqueue_post_new_results_jobs(
        poll_writer_uri,
        accepted_matches=[(_match(), "a" * 64)],
        club_id=CLUB_ID,
        platform=PLATFORM,
        game_version=GAME_VERSION,
        match_type_override=match_type,
    )


# ---------------------------------------------------------------------------
# Versioned NFKC alias-HMAC contract
# ---------------------------------------------------------------------------


def test_alias_hmac_normalization_version_is_explicit_and_stable() -> None:
    assert ALIAS_HMAC_NORMALIZATION_VERSION == "fc27-alias-hmac-nfkc-v1"


def test_alias_hmac_nfkc_compatibility_equivalence() -> None:
    composed = "Café_Player"
    decomposed = unicodedata.normalize("NFD", composed)
    assert compute_alias_hmac_fingerprint(composed, VALID_KEY) == (
        compute_alias_hmac_fingerprint(decomposed, VALID_KEY)
    )
    # Full-width compatibility form must match ASCII.
    assert compute_alias_hmac_fingerprint("Ａ", VALID_KEY) == (
        compute_alias_hmac_fingerprint("A", VALID_KEY)
    )
    # Ligature compatibility form must match its decomposed ASCII spelling.
    assert compute_alias_hmac_fingerprint("ﬁle", VALID_KEY) == (
        compute_alias_hmac_fingerprint("file", VALID_KEY)
    )


def test_alias_hmac_case_and_whitespace_equivalence() -> None:
    canonical = compute_alias_hmac_fingerprint("East Coasters", VALID_KEY)
    assert compute_alias_hmac_fingerprint("EAST COASTERS", VALID_KEY) == canonical
    assert (
        compute_alias_hmac_fingerprint("  East    Coasters  ", VALID_KEY) == canonical
    )


def test_alias_hmac_distinguishes_aliases_and_keys() -> None:
    first = compute_alias_hmac_fingerprint("East Coasters", VALID_KEY)
    other_alias = compute_alias_hmac_fingerprint("West Coasters", VALID_KEY)
    other_key = compute_alias_hmac_fingerprint(
        "East Coasters",
        "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
    )
    assert first != other_alias
    assert first != other_key
    assert HEX_SHA256_RE.fullmatch(first)
    assert first == first.lower()


@pytest.mark.parametrize("bad_input", ["", "   "])
def test_alias_hmac_rejects_empty_and_whitespace_only(bad_input: str) -> None:
    with pytest.raises(FC27ValidationError):
        compute_alias_hmac_fingerprint(bad_input, VALID_KEY)


@pytest.mark.parametrize("bad_input", ["bad\x00null", "line\nbreak", "tab\tchar"])
def test_alias_hmac_rejects_control_characters(bad_input: str) -> None:
    with pytest.raises(FC27ValidationError):
        compute_alias_hmac_fingerprint(bad_input, VALID_KEY)


def test_alias_hmac_rejects_oversized_input() -> None:
    with pytest.raises(FC27ValidationError):
        compute_alias_hmac_fingerprint("a" * 257, VALID_KEY)


@pytest.mark.parametrize("bad_key", ["short_key_under_32_bytes", b"short", "", "   "])
def test_alias_hmac_rejects_short_keys(bad_key: object) -> None:
    with pytest.raises(FC27ValidationError):
        compute_alias_hmac_fingerprint("East Coasters", bad_key)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Minimal versioned payload
# ---------------------------------------------------------------------------


def test_payload_is_minimal_versioned_and_privacy_safe() -> None:
    payload = _payload()
    assert tuple(payload) == POST_NEW_RESULTS_PAYLOAD_KEYS
    assert payload["schema_version"] == POST_NEW_RESULTS_SCHEMA_VERSION
    assert payload["event_type"] == POST_NEW_RESULTS_EVENT_TYPE
    assert payload["match_ref"] == pseudonymize_match_ref("930001", club_id=CLUB_ID)
    serialized = json.dumps(payload)
    assert "opponent_name" not in payload
    assert "Synthetic Opponent" not in serialized
    assert scan_for_sensitive_fields(payload) == []
    assert len(serialized.encode("utf-8")) <= 4096


def test_payload_rejects_unexpected_or_missing_fields() -> None:
    payload = _payload()
    from src.fc27_repository import _validate_discord_job_payload

    with pytest.raises(ValueError):
        _validate_discord_job_payload({**payload, "opponent_name": "mutable"})
    with pytest.raises(ValueError):
        _validate_discord_job_payload(
            {k: v for k, v in payload.items() if k != "platform"}
        )


# ---------------------------------------------------------------------------
# Complete canonical idempotency equivalence
# ---------------------------------------------------------------------------


def test_equivalent_payloads_ignore_key_order() -> None:
    payload = _payload()
    reordered = dict(reversed(list(payload.items())))
    assert _discord_job_payloads_equivalent(payload, reordered) is True


@pytest.mark.parametrize(
    "change",
    [
        {"match_type": "friendlyMatch"},
        {"platform": "common-gen4"},
        {"game_version": "FC26"},
        {"club_id": "999999"},
        {"schema_version": "fc27-post-new-results-v0"},
    ],
)
def test_materially_changed_payloads_are_not_equivalent(
    change: dict[str, Any],
) -> None:
    assert (
        _discord_job_payloads_equivalent(_payload(), {**_payload(), **change}) is False
    )


def test_missing_and_additional_and_retyped_payloads_are_not_equivalent() -> None:
    payload = _payload()
    missing = {k: v for k, v in payload.items() if k != "platform"}
    extra = {**payload, "export_id": "batch-1"}
    retyped = {**payload, "club_id": 330323}
    assert _discord_job_payloads_equivalent(payload, missing) is False
    assert _discord_job_payloads_equivalent(payload, extra) is False
    assert _discord_job_payloads_equivalent(payload, retyped) is False


# ---------------------------------------------------------------------------
# Transactional behavior (disposable PostgreSQL only)
# ---------------------------------------------------------------------------


def test_new_match_commits_match_stats_fingerprints_batch_and_one_job(
    pg_db_migrated,
) -> None:
    data = make_synthetic_export_dict(
        export_id="exp-c1-remediation-atomic",
        external_match_id="match-c1-remediation-atomic",
    )
    envelope = parse_fc27_user_export_json(json.dumps(data))
    result = apply_export_plan(
        pg_db_migrated.poll_writer,
        envelope=envelope,
        alias_fingerprints=(
            FC27AliasFingerprintObservation(
                pseudonym="plr_ecf_001122334455", gamertag_hmac="1" * 64
            ),
        ),
        enqueue_discord_jobs=True,
        match_type_override="leagueMatch",
        emit_domain_events=False,
    )
    assert result.status == "APPLIED"
    assert result.discord_jobs_inserted == 1
    assert result.fingerprints_inserted == 1
    jobs = fetch_discord_jobs(pg_db_migrated.poll_writer)
    assert len(jobs) == 1
    assert jobs[0]["job_type"] == "post_new_results"
    assert jobs[0]["state"] == "pending"
    assert jobs[0]["attempt_count"] == 0
    assert jobs[0]["payload"] == _payload("match-c1-remediation-atomic")


def test_duplicate_replay_and_reordered_payload_are_idempotent(
    pg_db_migrated,
) -> None:
    _seed_job(pg_db_migrated.admin, _idempotency_key(), _payload())
    result = _enqueue(pg_db_migrated.poll_writer)
    assert result.inserted_count == 0
    assert result.existing_equivalent_count == 1
    assert _counts(pg_db_migrated.admin)["jobs"] == 1


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda p: {**p, "match_type": "friendlyMatch"}, id="match_type"),
        pytest.param(lambda p: {**p, "platform": "common-gen4"}, id="platform"),
        pytest.param(lambda p: {**p, "game_version": "FC26"}, id="game_version"),
        pytest.param(lambda p: {**p, "club_id": "999999"}, id="club_id"),
        pytest.param(
            lambda p: {**p, "schema_version": "fc27-post-new-results-v0"},
            id="schema_version",
        ),
        pytest.param(
            lambda p: {k: v for k, v in p.items() if k != "game_version"},
            id="missing_field",
        ),
        pytest.param(lambda p: {**p, "export_id": "batch-1"}, id="additional_field"),
        pytest.param(lambda p: {**p, "club_id": 330323}, id="changed_type"),
    ],
)
def test_conflicting_existing_job_fails_closed_and_is_unchanged(
    pg_db_migrated, mutate
) -> None:
    key = _idempotency_key()
    stored = mutate(_payload())
    _seed_job(pg_db_migrated.admin, key, stored)

    with pytest.raises(FC27JobIdempotencyConflictError) as excinfo:
        _enqueue(pg_db_migrated.poll_writer)

    message = str(excinfo.value)
    assert "{" not in message
    assert "mref_ecf_" not in message
    assert _stored_payload(pg_db_migrated.admin, key) == stored
    assert _counts(pg_db_migrated.admin)["jobs"] == 1


def test_job_conflict_rolls_back_match_stats_fingerprints_and_checkpoint(
    pg_db_migrated,
) -> None:
    export_id = "exp-c1-remediation-rollback"
    external_match_id = "match-c1-remediation-rollback"
    key = _idempotency_key(external_match_id)
    _seed_job(
        pg_db_migrated.admin,
        key,
        _payload(external_match_id, match_type="friendlyMatch"),
    )
    before = _counts(pg_db_migrated.admin)
    stored_before = _stored_payload(pg_db_migrated.admin, key)

    data = make_synthetic_export_dict(
        export_id=export_id,
        external_match_id=external_match_id,
    )
    envelope = parse_fc27_user_export_json(json.dumps(data))
    with pytest.raises(FC27JobIdempotencyConflictError):
        apply_export_plan(
            pg_db_migrated.poll_writer,
            envelope=envelope,
            alias_fingerprints=(
                FC27AliasFingerprintObservation(
                    pseudonym="plr_ecf_001122334455", gamertag_hmac="1" * 64
                ),
            ),
            enqueue_discord_jobs=True,
            match_type_override="leagueMatch",
            emit_domain_events=False,
        )

    after = _counts(pg_db_migrated.admin)
    assert after["matches"] - before["matches"] == 0
    assert after["stats"] - before["stats"] == 0
    assert after["fingerprints"] - before["fingerprints"] == 0
    assert after["jobs"] - before["jobs"] == 0
    assert after["batches"] - before["batches"] == 0
    assert fetch_export_batch_status(pg_db_migrated.poll_writer, export_id) is None
    assert _stored_payload(pg_db_migrated.admin, key) == stored_before
    assert fetch_player_alias_fingerprints(pg_db_migrated.poll_writer) == []


def test_poll_writer_cannot_claim_complete_update_or_delete_jobs(
    pg_db_migrated,
) -> None:
    _seed_job(pg_db_migrated.admin, _idempotency_key(), _payload())

    with open_guarded_polling_connection(pg_db_migrated.poll_writer) as conn:
        with pytest.raises(FC27PollingTableAccessDeniedError):
            conn.execute("UPDATE public.fc27_discord_jobs SET state = 'claimed'")
        with pytest.raises(FC27PollingTableAccessDeniedError):
            conn.execute("DELETE FROM public.fc27_discord_jobs")

    for statement in (
        "UPDATE public.fc27_discord_jobs SET state = 'claimed', claim_owner = 'x'",
        "UPDATE public.fc27_discord_jobs SET state = 'completed'",
        "UPDATE public.fc27_discord_jobs SET attempt_count = attempt_count + 1",
        "DELETE FROM public.fc27_discord_jobs",
    ):
        with psycopg.connect(pg_db_migrated.poll_writer) as raw_conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                raw_conn.execute(statement)
            raw_conn.rollback()

    assert _counts(pg_db_migrated.admin)["jobs"] == 1
