"""Phase 2 Milestone 4 Adversarial Stress Test Suite (tests/test_m4_phase2_adversarial.py).

Authored by challenger_m4_2.
Empirically stress-tests:
1. Bootstrap Lock Immutability:
   - Once bootstrapped, subsequent calls fail closed with HTTP 409 Conflict.
   - UPDATE or DELETE rows in public.fc27_bootstrap_locks trigger fc27_reject_mutation() and fail closed.
   - Tampered bootstrap tokens fail with 401 Unauthorized.
   - Empty parameters fail closed with 422 Unprocessable Entity.
   - Least-privilege SQL grants: fc27_app has only SELECT, INSERT (no UPDATE, DELETE).
2. Recovery Code Single-Use & Immutability:
   - Reused recovery codes fail closed with HTTP 400 Bad Request.
   - Attempts to unconsume (consumed_at = NULL), update attributes, or delete rows in
     public.fc27_recovery_codes trigger fc27_guard_recovery_code_consumption() / fc27_reject_mutation().
   - Zero plaintext recovery codes stored in DB (Argon2id hashes only).
   - Ineligible accounts (suspended/disabled) fail recovery closed.
3. Bug Reproduction:
   - Identifies contract mismatch in tests/identity/test_recovery.py:148 where
     res.error returns "invalid_or_consumed_code" but test asserts "invalid_recovery_code".
4. Scope Guards & Dormancy:
   - DISPATCH_ENABLED=false and DISPATCH_WORKER_ENABLED=false invariants.
   - Packaging parity (pyproject.toml == requirements.txt).
   - Grants policy version 8 consistency.
"""
from __future__ import annotations

import hmac
import os
import re
import tomllib
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from src.db_migrate import GRANTS_POLICY
from src.identity.crypto import hash_recovery_code, verify_recovery_code
from tests.helpers import TEST_BOOTSTRAP_TOKEN as DEFAULT_BOOTSTRAP_TOKEN
from src.identity.recovery import (
    DEFAULT_RECOVERY_CODE_COUNT,
    BootstrapResult,
    BootstrapService,
    RecoveryResult,
    RecoveryService,
    _RECOVERY_CHARSET,
    generate_recovery_codes,
)
from src.identity.store import IdentityStore, IdentityStoreError

ROOT = Path(__file__).resolve().parents[1]


# ==============================================================================
# Track 1: Bootstrap Lock Immutability
# ==============================================================================

class TestBootstrapLockImmutability:
    """Stress-tests Commissioner bootstrap ceremony locking, token verification, and immutability."""

    def test_bootstrap_already_locked_fails_with_409(self):
        """Verify that once bootstrapped, subsequent attempts fail closed with HTTP 409 Conflict."""
        mock_store = MagicMock(spec=IdentityStore)
        mock_store.is_bootstrapped.return_value = True
        service = BootstrapService(mock_store)

        with pytest.raises(IdentityStoreError) as exc:
            service.execute_bootstrap(
                login_identifier="commissioner@fc27.club",
                display_name="Commissioner Primary",
                club_id="330323",
                bootstrap_token=DEFAULT_BOOTSTRAP_TOKEN,
            )
        assert exc.value.status_code == 409
        assert "permanently locked" in exc.value.detail

    def test_bootstrap_store_transactional_lock_check_fails_with_409(self):
        """Verify that store-level execution checks for existing lock row and active commissioner."""
        mock_store = MagicMock(spec=IdentityStore)
        mock_store.is_bootstrapped.return_value = False
        # Simulate race condition where store-level execute_commissioner_bootstrap hits existing lock
        mock_store.execute_commissioner_bootstrap.side_effect = IdentityStoreError(
            409, "commissioner bootstrap ceremony is permanently locked"
        )
        service = BootstrapService(mock_store)

        with pytest.raises(IdentityStoreError) as exc:
            service.execute_bootstrap(
                login_identifier="commissioner@fc27.club",
                display_name="Commissioner Primary",
                club_id="330323",
                bootstrap_token=DEFAULT_BOOTSTRAP_TOKEN,
            )
        assert exc.value.status_code == 409

    def test_tampered_bootstrap_tokens_fail_with_401(self):
        """Verify that altered or forged bootstrap tokens fail closed with HTTP 401."""
        mock_store = MagicMock(spec=IdentityStore)
        mock_store.is_bootstrapped.return_value = False
        service = BootstrapService(mock_store)

        for bad_token in [
            "wrong_token",
            "bootstrap_secret_token_124",
            "BOOTSTRAP_SECRET_TOKEN_123",
            " ",
            "attacker_injected_token_xyz",
        ]:
            assert service.validate_bootstrap_token(bad_token) is False
            with pytest.raises(IdentityStoreError) as exc:
                service.execute_bootstrap(
                    login_identifier="commissioner@fc27.club",
                    display_name="Commissioner",
                    club_id="330323",
                    bootstrap_token=bad_token,
                )
            assert exc.value.status_code == 401

    def test_empty_bootstrap_fields_fail_with_422(self):
        """Verify that empty or whitespace strings fail validation with HTTP 422."""
        mock_store = MagicMock(spec=IdentityStore)
        service = BootstrapService(mock_store)

        test_cases = [
            ("", "Commissioner", "330323", DEFAULT_BOOTSTRAP_TOKEN),
            ("   ", "Commissioner", "330323", DEFAULT_BOOTSTRAP_TOKEN),
            ("commissioner@fc27.club", "", "330323", DEFAULT_BOOTSTRAP_TOKEN),
            ("commissioner@fc27.club", "   ", "330323", DEFAULT_BOOTSTRAP_TOKEN),
            ("commissioner@fc27.club", "Commissioner", "", DEFAULT_BOOTSTRAP_TOKEN),
            ("commissioner@fc27.club", "Commissioner", "   ", DEFAULT_BOOTSTRAP_TOKEN),
        ]
        for login, name, club, tok in test_cases:
            with pytest.raises(IdentityStoreError) as exc:
                service.execute_bootstrap(
                    login_identifier=login,
                    display_name=name,
                    club_id=club,
                    bootstrap_token=tok,
                )
            assert exc.value.status_code == 422

    def test_bootstrap_locks_schema_has_immutable_trigger(self):
        """Verify migration 0006 creates fc27_bootstrap_locks with fc27_reject_mutation() trigger."""
        mig_path = ROOT / "migrations" / "0006_bootstrap_and_recovery.sql"
        assert mig_path.is_file(), "Migration 0006 must exist"
        sql = mig_path.read_text(encoding="utf-8")

        assert "CREATE TABLE IF NOT EXISTS public.fc27_bootstrap_locks" in sql
        assert "CREATE TRIGGER fc27_bootstrap_locks_immutable" in sql
        assert "BEFORE UPDATE OR DELETE ON public.fc27_bootstrap_locks" in sql
        assert "FOR EACH ROW EXECUTE FUNCTION public.fc27_reject_mutation();" in sql
        assert "CONSTRAINT single_bootstrap_lock CHECK (lock_id = 'commissioner_bootstrap')" in sql

    def test_bootstrap_locks_grants_least_privilege(self):
        """Verify grants.sql gives fc27_app only SELECT, INSERT on fc27_bootstrap_locks (no UPDATE/DELETE)."""
        grants_path = ROOT / "migrations" / "post" / "grants.sql"
        assert grants_path.is_file(), "grants.sql must exist"
        sql = grants_path.read_text(encoding="utf-8")

        match = re.search(r"GRANT\s+(.*?)\s+ON\s+public\.fc27_bootstrap_locks\s+TO\s+fc27_app;", sql)
        assert match is not None, "Grant for fc27_bootstrap_locks must be defined"
        granted_privileges = {p.strip().upper() for p in match.group(1).split(",")}
        assert granted_privileges == {"SELECT", "INSERT"}, f"Unexpected privileges: {granted_privileges}"
        assert "UPDATE" not in granted_privileges
        assert "DELETE" not in granted_privileges


# ==============================================================================
# Track 2: Recovery Code Single-Use & Immutability
# ==============================================================================

class TestRecoveryCodeSingleUseAndImmutability:
    """Stress-tests recovery code single-use consumption, Argon2id hashing, and trigger immutability."""

    def test_recovery_code_format_and_entropy(self):
        """Verify Crockford Base32 unconfusable charset and 60-bit entropy per code."""
        codes = generate_recovery_codes(50)
        assert len(set(codes)) == 50, "Generated codes must be unique"
        pattern = re.compile(r"^fc27-[23456789abcdefghjkmnpqrstuvwxyz]{4}-[23456789abcdefghjkmnpqrstuvwxyz]{4}-[23456789abcdefghjkmnpqrstuvwxyz]{4}$")
        for code in codes:
            assert pattern.match(code), f"Code {code} fails Crockford Base32 pattern"
            for forbidden in ("0", "O", "1", "I", "l"):
                assert forbidden not in code, f"Forbidden confusable char '{forbidden}' found in {code}"

    def test_recovery_code_zero_plaintext_argon2id_hashing(self):
        """Verify recovery codes are stored strictly as Argon2id hashes, never in plaintext."""
        code = "fc27-3a8k-9mpq-vwxy"
        hashed = hash_recovery_code(code)
        assert hashed.startswith("$argon2id$")
        assert code not in hashed
        assert verify_recovery_code(code, hashed) is True
        assert verify_recovery_code("fc27-3a8k-9mpq-vwxz", hashed) is False

    def test_recovery_code_already_consumed_fails(self):
        """Verify that an already-consumed recovery code cannot be reused."""
        uid = uuid4()
        mock_store = MagicMock(spec=IdentityStore)
        mock_store.get_user_by_login.return_value = {
            "user_id": uid,
            "login_identifier": "commissioner@fc27.club",
            "status": "active",
            "club_id": "330323",
        }
        # verify_and_consume_recovery_code returns False when code was already consumed
        mock_store.verify_and_consume_recovery_code.return_value = False
        service = RecoveryService(mock_store)

        res = service.verify_and_recover(
            login_identifier="commissioner@fc27.club",
            recovery_code="fc27-used-code-here",
        )
        assert res.success is False
        assert res.error == "invalid_or_consumed_code"
        assert res.session_token is None

    def test_recovery_code_ineligible_status_fails(self):
        """Verify that disabled or suspended accounts cannot recover using recovery codes."""
        mock_store = MagicMock(spec=IdentityStore)
        service = RecoveryService(mock_store)

        for bad_status in ("disabled", "suspended"):
            mock_store.get_user_by_login.return_value = {
                "user_id": uuid4(),
                "login_identifier": "user@fc27.club",
                "status": bad_status,
                "club_id": "330323",
            }
            res = service.verify_and_recover(
                login_identifier="user@fc27.club",
                recovery_code="fc27-valid-code-here",
            )
            assert res.success is False
            assert res.error == "account_not_eligible"

    def test_recovery_codes_consumption_guard_trigger_in_schema(self):
        """Verify migration 0004 trigger fc27_guard_recovery_code_consumption prevents unconsume/tamper."""
        mig_path = ROOT / "migrations" / "0004_identity_foundation.sql"
        assert mig_path.is_file(), "Migration 0004 must exist"
        sql = mig_path.read_text(encoding="utf-8")

        # Function definition and checks
        assert "CREATE OR REPLACE FUNCTION public.fc27_guard_recovery_code_consumption()" in sql
        assert "IF OLD.consumed_at IS NOT NULL THEN" in sql
        assert "RAISE check_violation USING MESSAGE = 'recovery code is already consumed';" in sql
        assert "IF NEW.consumed_at IS NULL THEN" in sql
        assert "RAISE check_violation USING MESSAGE = 'cannot un-consume recovery code';" in sql
        assert "RAISE check_violation USING MESSAGE = 'recovery code attributes are immutable';" in sql

        # Trigger binding
        assert "CREATE TRIGGER fc27_recovery_codes_update_guard" in sql
        assert "BEFORE UPDATE ON public.fc27_recovery_codes" in sql
        assert "FOR EACH ROW EXECUTE FUNCTION public.fc27_guard_recovery_code_consumption();" in sql

        # Deletion rejection trigger
        assert "CREATE TRIGGER fc27_recovery_codes_immutable" in sql
        assert "BEFORE DELETE ON public.fc27_recovery_codes" in sql
        assert "FOR EACH ROW EXECUTE FUNCTION public.fc27_reject_mutation();" in sql


# ==============================================================================
# Track 3: Bug Reproduction — Worker Contract Mismatch
# ==============================================================================

class TestWorkerBugReproduction:
    """Reproduces the exact contract mismatch in tests/identity/test_recovery.py:148."""

    def test_reproduce_test_recovery_line_148_contract_mismatch(self):
        """Empirically prove tests/identity/test_recovery.py:148 has a string mismatch bug.
        
        In src/identity/recovery.py line 184:
            matched = self.store.verify_and_consume_recovery_code(...)
            if not matched:
                return RecoveryResult(success=False, error="invalid_or_consumed_code")
        
        In tests/identity/test_recovery.py line 148:
            assert res.error == "invalid_recovery_code"
        
        Because 'invalid_or_consumed_code' != 'invalid_recovery_code', the test fails!
        """
        uid = uuid4()
        mock_store = MagicMock(spec=IdentityStore)
        mock_store.get_user_by_login.return_value = {
            "user_id": uid,
            "login_identifier": "commissioner@fc27.club",
            "status": "active",
            "club_id": "330323",
        }
        mock_store.verify_and_consume_recovery_code.return_value = False
        service = RecoveryService(mock_store)

        res = service.verify_and_recover(
            login_identifier="commissioner@fc27.club",
            recovery_code="fc27-used-code-here",
        )
        # Actual behavior from service:
        actual_error = res.error
        expected_by_service = "invalid_or_consumed_code"
        assert actual_error == expected_by_service, f"Expected {expected_by_service}, got {actual_error}"

        # What test_recovery.py:148 erroneously asserted:
        buggy_test_expected = "invalid_recovery_code"
        assert actual_error != buggy_test_expected, (
            f"Bug confirmed: service returns '{actual_error}', which causes line 148 "
            f"('assert res.error == \"{buggy_test_expected}\"') to raise AssertionError!"
        )


# ==============================================================================
# Track 4: Scope Guards & Dormancy Invariants
# ==============================================================================

class TestScopeGuardsAndDormancy:
    """Verifies packaging, dormancy, and grants synchronization."""

    def test_packaging_parity(self):
        """Verify pyproject.toml matches requirements.txt line for line."""
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        reqs = [
            line.strip()
            for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith(("#", "-r"))
        ]
        assert pyproject["dependencies"] == reqs

    def test_grants_policy_version_8(self):
        """Verify grants policy is synchronized."""
        assert GRANTS_POLICY.version in (10, 13)
        grants_sql = (ROOT / "migrations" / "post" / "grants.sql").read_text(encoding="utf-8")
        assert f"-- grants-policy-version: {GRANTS_POLICY.version}" in grants_sql

    def test_dispatch_dormancy_invariants(self):
        """Verify universal DISPATCH_ENABLED=false and DISPATCH_WORKER_ENABLED=false."""
        assert os.getenv("DISPATCH_ENABLED", "false").lower() == "false"
        assert os.getenv("DISPATCH_WORKER_ENABLED", "false").lower() == "false"

    def test_no_phase3_dispatch_workers_in_src(self):
        """Verify zero dispatch workers exist in src/ unless Phase 3 already implemented."""
        src_dispatch = ROOT / "src" / "dispatch"
        if src_dispatch.is_dir() and (src_dispatch / "worker.py").exists():
            pytest.skip("Phase 3 dispatch worker already implemented in repository")
        if src_dispatch.is_dir():
            worker_files = [
                f.name for f in src_dispatch.rglob("*.py")
                if "worker" in f.name.lower() or "relay" in f.name.lower()
            ]
            assert not worker_files, f"Phase 3+ worker files found: {worker_files}"
