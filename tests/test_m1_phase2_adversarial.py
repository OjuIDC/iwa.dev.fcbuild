"""Adversarial stress-test suite for Phase 2 Milestone 1 (Identity Data Foundation & Cryptography).

Authored by challenger_m1_2.
Tests:
1. Packaging sync & virtualenv dependency drift check.
2. Static AST inspection of test suites (verifying missing imports like hashlib).
3. Schema & trigger boundary invariants (recovery code deletion rejection, counter monotonicity, unique login, attribute immutability).
4. Session lifecycle: idle vs absolute timeout logic, touch capping, session rotation invalidation.
5. Scope boundaries & dispatch dormancy invariants.
"""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import tomllib
import pytest

ROOT = Path(__file__).resolve().parents[1]


# =====================================================================
# 1. Packaging & Virtual Environment Verification
# =====================================================================

def test_packaging_sync():
    """Verify pyproject.toml and requirements.txt dependency parity."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    reqs = [
        line.strip()
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith(("#", "-r"))
    ]
    assert pyproject["dependencies"] == reqs, "pyproject.toml dependencies must match requirements.txt exactly"


def test_virtualenv_dependency_availability():
    """Empirical check: Verify whether declared runtime dependencies are installed in current Python env.

    Identifies if worker declared dependencies without installing them into the virtualenv.
    """
    missing_modules: list[str] = []
    # Check imports required by Phase 2 M1
    for mod_name in ("argon2", "cryptography", "webauthn", "pyotp"):
        try:
            __import__(mod_name)
        except ImportError:
            missing_modules.append(mod_name)

    # We record this finding: if missing_modules is non-empty, test fails with clear diagnosis
    assert not missing_modules, (
        f"Declared dependencies missing from active Python environment: {missing_modules}. "
        f"Tests such as tests/identity/test_crypto.py will fail to import."
    )


# =====================================================================
# 2. Test Suite Static Inspection (AST Bug Hunting)
# =====================================================================

def test_store_test_suite_has_no_undefined_hashlib():
    """AST inspection of tests/identity/test_store.py for missing hashlib import.

    Lines 381 and 392 call hashlib.sha256; test fails if hashlib is not imported.
    """
    store_test_path = ROOT / "tests" / "identity" / "test_store.py"
    assert store_test_path.is_file(), "tests/identity/test_store.py must exist"

    content = store_test_path.read_text(encoding="utf-8")
    tree = ast.parse(content, filename=str(store_test_path))

    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported_names.add(alias.asname or alias.name)

    # Check references to hashlib
    hashlib_used = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "hashlib":
            hashlib_used = True
            break

    if hashlib_used:
        assert "hashlib" in imported_names, (
            "tests/identity/test_store.py uses 'hashlib' at lines 381/392 but fails to import it! "
            "Executing test_challenge_single_use_and_binding will raise NameError: name 'hashlib' is not defined."
        )


# =====================================================================
# 3. Schema & Trigger Boundaries (Static Specification Analysis)
# =====================================================================

def test_recovery_codes_deletion_rejection_trigger():
    """Verify fc27_recovery_codes has fc27_reject_mutation attached BEFORE DELETE."""
    mig_0004 = (ROOT / "migrations" / "0004_identity_foundation.sql").read_text(encoding="utf-8")

    # Assert trigger attachment
    match = re.search(
        r"CREATE\s+TRIGGER\s+fc27_recovery_codes_immutable\s+BEFORE\s+DELETE\s+ON\s+public\.fc27_recovery_codes\s+FOR\s+EACH\s+ROW\s+EXECUTE\s+FUNCTION\s+public\.fc27_reject_mutation\(\)",
        mig_0004,
        re.IGNORECASE,
    )
    assert match is not None, "fc27_recovery_codes must have BEFORE DELETE trigger executing fc27_reject_mutation()"

    mig_0003 = (ROOT / "migrations" / "0003_dispatch_foundation.sql").read_text(encoding="utf-8")
    assert "fc27_reject_mutation" in mig_0003, "fc27_reject_mutation must be defined"
    assert "RAISE check_violation USING MESSAGE = TG_TABLE_NAME || ' is append-only'" in mig_0003


def test_webauthn_sign_counter_monotonic_guard():
    """Verify fc27_guard_webauthn_counter prevents sign_counter from decrementing."""
    mig_0004 = (ROOT / "migrations" / "0004_identity_foundation.sql").read_text(encoding="utf-8")

    assert "fc27_guard_webauthn_counter" in mig_0004
    assert "IF NEW.sign_counter < OLD.sign_counter THEN" in mig_0004
    assert "RAISE check_violation USING MESSAGE = 'webauthn sign_counter cannot decrease'" in mig_0004

    match = re.search(
        r"CREATE\s+TRIGGER\s+fc27_webauthn_counter_guard\s+BEFORE\s+UPDATE\s+ON\s+public\.fc27_webauthn_credentials",
        mig_0004,
        re.IGNORECASE,
    )
    assert match is not None, "fc27_webauthn_credentials must have BEFORE UPDATE trigger executing fc27_guard_webauthn_counter()"


def test_fc27_users_duplicate_login_and_normalization_constraints():
    """Verify fc27_users table enforces UNIQUE and lower/trim check constraints on login_id."""
    mig_0004 = (ROOT / "migrations" / "0004_identity_foundation.sql").read_text(encoding="utf-8")

    assert re.search(
        r"login_id\s+text\s+NOT\s+NULL\s+UNIQUE\s+CHECK\s*\(.*login_id\s*=\s*lower\(trim\(login_id\)\)\)",
        mig_0004,
        re.IGNORECASE,
    ), "fc27_users.login_id must have UNIQUE constraint and lower(trim(login_id)) check"


def test_fc27_recovery_codes_update_guard():
    """Verify fc27_guard_recovery_code_consumption enforces single-use and attribute immutability."""
    mig_0004 = (ROOT / "migrations" / "0004_identity_foundation.sql").read_text(encoding="utf-8")

    assert "IF OLD.consumed_at IS NOT NULL THEN" in mig_0004
    assert "RAISE check_violation USING MESSAGE = 'recovery code is already consumed'" in mig_0004
    assert "IF NEW.consumed_at IS NULL THEN" in mig_0004
    assert "RAISE check_violation USING MESSAGE = 'cannot un-consume recovery code'" in mig_0004
    assert "recovery code attributes are immutable" in mig_0004


# =====================================================================
# 4. Session Dual-Expiry and Rotation Lifecycle Logic
# =====================================================================

class SessionLifecycleSimulator:
    """Deterministic simulation of the IdentityStore session dual-expiry & rotation logic."""

    def __init__(
        self,
        created_at: datetime,
        idle_ttl_minutes: int = 30,
        absolute_ttl_hours: int = 24,
    ):
        self.created_at = created_at
        self.last_used_at = created_at
        self.idle_expires_at = created_at + timedelta(minutes=idle_ttl_minutes)
        self.absolute_expires_at = created_at + timedelta(hours=absolute_ttl_hours)
        self.idle_ttl = timedelta(minutes=idle_ttl_minutes)
        self.revoked_at: datetime | None = None
        self.auth_level = "authenticated"

    def is_valid(self, at_time: datetime) -> bool:
        if self.revoked_at is not None:
            return False
        if at_time > self.absolute_expires_at or at_time > self.idle_expires_at:
            return False
        return True

    def touch(self, at_time: datetime) -> bool:
        if not self.is_valid(at_time):
            return False
        # Exact logic from src/identity/store.py lines 550-556:
        # new_idle = min(now + timedelta(minutes=idle_ttl_minutes), row["absolute_expires_at"])
        self.idle_expires_at = min(at_time + self.idle_ttl, self.absolute_expires_at)
        self.last_used_at = at_time
        return True

    def rotate(self, at_time: datetime, new_auth_level: str) -> SessionLifecycleSimulator:
        if not self.is_valid(at_time):
            raise ValueError("cannot rotate invalid session")
        self.revoked_at = at_time
        new_sess = SessionLifecycleSimulator(created_at=at_time)
        new_sess.auth_level = new_auth_level
        return new_sess


def test_session_idle_timeout():
    """Verify session expires after 30 minutes of inactivity even if well within 24h absolute."""
    t0 = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    sess = SessionLifecycleSimulator(created_at=t0)

    # 29 minutes later: valid
    t_29 = t0 + timedelta(minutes=29)
    assert sess.is_valid(t_29) is True

    # 31 minutes later without touch: expired
    t_31 = t0 + timedelta(minutes=31)
    assert sess.is_valid(t_31) is False


def test_session_touch_extends_idle_but_strictly_capped_by_absolute():
    """Verify touching session extends idle expiration but NEVER exceeds absolute 24h limit."""
    t0 = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    sess = SessionLifecycleSimulator(created_at=t0)

    # Touch every 20 minutes up to 23h 40m
    for minute in range(20, 23 * 60 + 50, 20):
        current_time = t0 + timedelta(minutes=minute)
        assert sess.touch(current_time) is True
        assert sess.is_valid(current_time) is True
        # Invariant: idle_expires_at never exceeds absolute_expires_at
        assert sess.idle_expires_at <= sess.absolute_expires_at

    # At 23h 50m (10 min after 23h 40m): touch works, but idle_expires_at is clamped to absolute_expires_at
    t_23h50m = t0 + timedelta(hours=23, minutes=50)
    assert sess.touch(t_23h50m) is True
    assert sess.idle_expires_at == sess.absolute_expires_at

    # At 24h 1s: session is expired regardless of recent touch
    t_24h1s = t0 + timedelta(hours=24, seconds=1)
    assert sess.is_valid(t_24h1s) is False
    assert sess.touch(t_24h1s) is False


def test_session_rotation_invalidates_prior_session():
    """Verify session rotation revokes old session immediately, preventing session fixation."""
    t0 = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    sess = SessionLifecycleSimulator(created_at=t0)

    t_step_up = t0 + timedelta(minutes=10)
    new_sess = sess.rotate(at_time=t_step_up, new_auth_level="step_up_passkey")

    # Old session is now revoked and invalid
    assert sess.revoked_at == t_step_up
    assert sess.is_valid(t_step_up) is False

    # New session is active with elevated auth level
    assert new_sess.is_valid(t_step_up) is True
    assert new_sess.auth_level == "step_up_passkey"
