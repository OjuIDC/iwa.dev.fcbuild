"""Activation gates evaluation, recording, and signature verification."""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any, Final, Mapping, Sequence
from uuid import UUID

import psycopg
from psycopg.rows import dict_row


@dataclass
class GateResult:
    """Outcome of an activation gate evaluation."""
    gate_id: str
    passed: bool
    detail: str


# Phase 1 only permits manual recording of G-ID and G-ATOMIC (C-3).
# G-OUTBOX requires Phase 3 relay and reconciliation evidence.
RECORDABLE_GATES: Final[frozenset[str]] = frozenset({"G-ID", "G-ATOMIC"})

# Canonical fields covered by Commissioner HMAC signature (C-4).
# X-Correlation-Id is intentionally excluded from signature coverage (C-5).
GATE_SIGNED_FIELDS: Final[tuple[str, ...]] = (
    "gate_id",
    "status",
    "evidence_ref",
    "code_revision",
    "deployment_id",
    "fc_version",
    "platform",
    "issued_at",
    "expires_at",
    "nonce",
)


@dataclass(frozen=True)
class DeploymentIdentity:
    """Runtime deployment identity for gate evaluation and recording binding."""
    code_revision: str
    deployment_id: str
    fc_version: str = "27"
    platform: str = "common-gen5"

    def is_valid(self) -> bool:
        return bool(
            self.code_revision
            and self.code_revision.strip()
            and self.deployment_id
            and self.deployment_id.strip()
        )


def canonical_gate_bytes(payload: Mapping[str, Any]) -> bytes:
    missing = [f for f in GATE_SIGNED_FIELDS if f not in payload]
    if missing:
        raise ValueError(f"gate payload is missing required fields: {', '.join(missing)}")
    view = {f: str(payload[f]) if isinstance(payload[f], UUID) else payload[f] for f in GATE_SIGNED_FIELDS}
    return json.dumps(view, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def gate_payload_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_gate_bytes(payload)).hexdigest()


def sign_gate(payload: Mapping[str, Any], secret: str) -> str:
    if not secret or len(secret) < 32:
        raise ValueError("signing secret must be at least 32 characters")
    return hmac.new(secret.encode("utf-8"), canonical_gate_bytes(payload), hashlib.sha256).hexdigest()


def verify_gate_signature(payload: Mapping[str, Any], signature: str, secret: str) -> None:
    expected = sign_gate(payload, secret)
    if not isinstance(signature, str) or not hmac.compare_digest(signature.encode("utf-8"), expected.encode("utf-8")):
        raise PermissionError("invalid gate signature")


def evaluate_gates(conn: psycopg.Connection, identity: DeploymentIdentity) -> list[GateResult]:
    """Evaluate all activation gates in strict order: G-MIGRATION, G-GRANTS, G-WORKER, G-ID, G-ATOMIC, G-OUTBOX."""
    results: list[GateResult] = []

    with conn.cursor(row_factory=dict_row) as cur:
        # 1. G-MIGRATION
        cur.execute("SELECT 1 FROM schema_migrations WHERE version = '0003'")
        has_mig = cur.fetchone() is not None
        results.append(
            GateResult(
                gate_id="G-MIGRATION",
                passed=has_mig,
                detail="migration 0003 applied" if has_mig else "migration 0003 not applied",
            )
        )

        # 2. G-GRANTS
        grants_ok = True
        grants_details: list[str] = []

        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'fc27_dispatch'")
        if cur.fetchone() is None:
            grants_ok = False
            grants_details.append("fc27_dispatch role missing")

        for priv in ("UPDATE", "INSERT", "DELETE"):
            cur.execute("SELECT has_table_privilege('fc27_dispatch', 'public.dispatches', %s)", (priv,))
            if cur.fetchone()["has_table_privilege"]:
                grants_ok = False
                grants_details.append(f"fc27_dispatch has unauthorized {priv} on dispatches")

        cur.execute("SELECT has_function_privilege('fc27_dispatch', 'public.fc27_dispatch_transition(uuid, text, integer, text, text, uuid, jsonb)', 'EXECUTE')")
        if not cur.fetchone()["has_function_privilege"]:
            grants_ok = False
            grants_details.append("fc27_dispatch lacks EXECUTE on fc27_dispatch_transition")

        cur.execute("""
            SELECT r.rolname 
              FROM pg_proc p 
              JOIN pg_roles r ON p.proowner = r.oid 
             WHERE p.proname = 'fc27_dispatch_transition' 
               AND p.pronamespace = 'public'::regnamespace
        """)
        fn_owner = cur.fetchone()
        if fn_owner is None or fn_owner["rolname"] != "fc27_owner":
            grants_ok = False
            grants_details.append("fc27_dispatch_transition not owned by fc27_owner")

        cur.execute("SELECT pg_has_role('fc27_app', 'fc27_owner', 'MEMBER')")
        if cur.fetchone()["pg_has_role"]:
            grants_ok = False
            grants_details.append("fc27_app must not be member of fc27_owner")

        results.append(
            GateResult(
                gate_id="G-GRANTS",
                passed=grants_ok,
                detail="live privilege posture verified" if grants_ok else "; ".join(grants_details),
            )
        )

        # 3. G-WORKER
        cur.execute("SELECT to_regclass('public.service_heartbeats')")
        hb_row = cur.fetchone()
        has_hb_table = bool(hb_row and hb_row.get("to_regclass") is not None)
        if not has_hb_table:
            results.append(GateResult(gate_id="G-WORKER", passed=False, detail="service_heartbeats table missing"))
        else:
            cur.execute("""
                SELECT last_heartbeat, extract(epoch FROM (now() - last_heartbeat)) AS age_seconds
                  FROM public.service_heartbeats
                 WHERE service_name = 'dispatch-worker'
            """)
            hb = cur.fetchone()
            if hb is None:
                results.append(GateResult(gate_id="G-WORKER", passed=False, detail="no dispatch-worker heartbeat"))
            else:
                age = float(hb["age_seconds"])
                passed = age < 60.0
                results.append(
                    GateResult(
                        gate_id="G-WORKER",
                        passed=passed,
                        detail=f"heartbeat age {age:.1f}s (<60s required)" if passed else f"heartbeat stale: {age:.1f}s",
                    )
                )

        # 4, 5, 6: Manual Gates (G-ID, G-ATOMIC, G-OUTBOX)
        cur.execute("SELECT to_regclass('public.activation_gate_log')")
        log_row = cur.fetchone()
        has_log_table = bool(log_row and log_row.get("to_regclass") is not None)

        for gate_id in ("G-ID", "G-ATOMIC", "G-OUTBOX"):
            if not identity.is_valid():
                results.append(
                    GateResult(gate_id=gate_id, passed=False, detail="deployment identity missing or empty")
                )
                continue

            if not has_log_table:
                results.append(GateResult(gate_id=gate_id, passed=False, detail="activation_gate_log table missing"))
                continue

            cur.execute(
                """
                SELECT status, evidence_ref
                  FROM public.activation_gate_log
                 WHERE gate_id = %s
                   AND code_revision = %s
                   AND deployment_id = %s
                   AND fc_version = %s
                   AND platform = %s
                 ORDER BY id DESC
                 LIMIT 1
                """,
                (
                    gate_id,
                    identity.code_revision,
                    identity.deployment_id,
                    identity.fc_version,
                    identity.platform,
                ),
            )
            row = cur.fetchone()
            if row is None:
                results.append(GateResult(gate_id=gate_id, passed=False, detail="no record for deployment identity"))
            elif row["status"] == "passed":
                results.append(GateResult(gate_id=gate_id, passed=True, detail=row["evidence_ref"]))
            else:
                results.append(
                    GateResult(gate_id=gate_id, passed=False, detail=f"latest status: {row['status']} ({row['evidence_ref']})")
                )

    return results


def dispatch_effective(configured: bool, results: Sequence[GateResult]) -> bool:
    """Returns True iff configured is True AND all evaluated gates pass."""
    if not configured:
        return False
    return all(r.passed for r in results)


def evaluate_activation_gates(
    conn: psycopg.Connection,
    configured: bool = False,
    identity: DeploymentIdentity | None = None,
) -> tuple[bool, list[GateResult]]:
    """Helper returning (effective, gates) for compatibility with E2E feature tests."""
    if identity is None:
        identity = DeploymentIdentity("", "")
    results = evaluate_gates(conn, identity)
    effective = dispatch_effective(configured, results)
    return effective, results
