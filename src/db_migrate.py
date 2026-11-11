from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path
from dataclasses import dataclass

import psycopg

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
_LOCK_KEY = 0x0FC27_0001

@dataclass(frozen=True)
class GrantsPolicy:
    version: int
    sha256: str

GRANTS_POLICY_VERSION = 16


def _compute_grants_sha256() -> str:
    p = MIGRATIONS_DIR / "post" / "grants.sql"
    return hashlib.sha256(p.read_text(encoding="utf-8").encode()).hexdigest() if p.exists() else ""


GRANTS_POLICY = GrantsPolicy(version=16, sha256=_compute_grants_sha256())

class MigrationError(RuntimeError):
    pass

def migration_files(directory: Path = MIGRATIONS_DIR) -> list[tuple[str, Path]]:
    files = []
    for path in sorted(directory.glob("*.sql")):
        m = _NAME.match(path.name)
        if not m:
            raise RuntimeError(f"invalid migration file name: {path.name}")
        files.append((m.group(1), path))
    versions = [v for v, _ in files]
    if len(versions) != len(set(versions)):
        raise RuntimeError("duplicate migration version numbers")
    return files

def verify_postconditions(conn: psycopg.Connection) -> list[str]:
    violations = []
    with conn.cursor() as cur:
        # Check fc27_app
        cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = 'fc27_app'")
        row = cur.fetchone()
        if row and row[0]:
            violations.append("superuser")
        cur.execute("SELECT pg_has_role('fc27_app', 'fc27_owner', 'MEMBER')")
        if cur.fetchone()[0]:
            violations.append("owner_member")
        cur.execute("SELECT has_schema_privilege('fc27_app', 'public', 'CREATE')")
        if cur.fetchone()[0]:
            violations.append("create_on_public")

        # check fc27_dispatch
        cur.execute("SELECT pg_has_role('fc27_dispatch', 'fc27_owner', 'MEMBER')")
        row = cur.fetchone()
        if row and row[0]:
            violations.append("dispatch_owner_member")

        # check fc27_relay
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay'")
        if cur.fetchone():
            cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = 'fc27_relay'")
            row = cur.fetchone()
            if row and row[0]:
                violations.append("relay_superuser")
            cur.execute("SELECT pg_has_role('fc27_relay', 'fc27_owner', 'MEMBER')")
            if cur.fetchone()[0]:
                violations.append("relay_owner_member")
            cur.execute("SELECT has_schema_privilege('fc27_relay', 'public', 'CREATE')")
            if cur.fetchone()[0]:
                violations.append("relay_create_on_public")

        # check fc27_match_results_writer
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer'")
        if cur.fetchone():
            cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = 'fc27_match_results_writer'")
            row = cur.fetchone()
            if row and row[0]:
                violations.append("match_results_writer_superuser")
            cur.execute("SELECT pg_has_role('fc27_match_results_writer', 'fc27_owner', 'MEMBER')")
            if cur.fetchone()[0]:
                violations.append("match_results_writer_owner_member")
            cur.execute("SELECT has_schema_privilege('fc27_match_results_writer', 'public', 'CREATE')")
            if cur.fetchone()[0]:
                violations.append("match_results_writer_create_on_public")

        # check Stage C roles (fc27_poll_writer, fc27_player_dev_writer, fc27_discord_reader)
        for stage_c_role in (
            "fc27_poll_writer",
            "fc27_player_dev_writer",
            "fc27_discord_reader",
        ):
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (stage_c_role,))
            if cur.fetchone():
                cur.execute(
                    "SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls "
                    "FROM pg_roles WHERE rolname = %s",
                    (stage_c_role,),
                )
                r_flags = cur.fetchone()
                if r_flags and any(r_flags):
                    violations.append(f"{stage_c_role}_privileged_flag")
                cur.execute("SELECT pg_has_role(%s, 'fc27_owner', 'MEMBER')", (stage_c_role,))
                if cur.fetchone()[0]:
                    violations.append(f"{stage_c_role}_owner_member")
                cur.execute("SELECT has_schema_privilege(%s, 'public', 'CREATE')", (stage_c_role,))
                if cur.fetchone()[0]:
                    violations.append(f"{stage_c_role}_create_on_public")
                cur.execute(
                    """
                    SELECT count(*) FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public' AND c.relowner::regrole::text = %s
                    """,
                    (stage_c_role,),
                )
                if cur.fetchone()[0] > 0:
                    violations.append(f"{stage_c_role}_owns_objects")

        # check fc27_dispatch_transition
        cur.execute("""
            SELECT p.oid, p.prosecdef, p.proowner::regrole::text, p.proconfig
            FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'public' AND p.proname = 'fc27_dispatch_transition'
        """)
        fn_row = cur.fetchone()
        if fn_row:
            oid, prosecdef, proowner, proconfig = fn_row
            if not prosecdef:
                violations.append("dispatch_transition_not_secdef")
            if proowner != "fc27_owner":
                violations.append("dispatch_transition_not_owner")
            cfg = " ".join(proconfig) if proconfig else ""
            if "search_path=pg_catalog, pg_temp" not in cfg:
                violations.append("dispatch_transition_search_path")
            cur.execute("SELECT has_function_privilege('public', %s, 'EXECUTE')", (oid,))
            has_pub = cur.fetchone()
            if has_pub and has_pub[0]:
                violations.append("dispatch_transition_public_exec")

        # check fc27_purge_expired_quarantine (Migration 0012)
        cur.execute("""
            SELECT p.oid, p.prosecdef, p.proowner::regrole::text, p.proconfig
            FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'public' AND p.proname = 'fc27_purge_expired_quarantine'
        """)
        purge_row = cur.fetchone()
        if purge_row:
            p_oid, p_secdef, p_owner, p_config = purge_row
            if not p_secdef:
                violations.append("purge_quarantine_not_secdef")
            if p_owner != "fc27_owner":
                violations.append("purge_quarantine_not_owner")
            p_cfg = " ".join(p_config) if p_config else ""
            if "search_path=pg_catalog, pg_temp" not in p_cfg:
                violations.append("purge_quarantine_search_path")
            cur.execute("SELECT has_function_privilege('public', %s, 'EXECUTE')", (p_oid,))
            p_has_pub = cur.fetchone()
            if p_has_pub and p_has_pub[0]:
                violations.append("purge_quarantine_public_exec")
            cur.execute("SELECT has_function_privilege('fc27_dispatch', %s, 'EXECUTE')", (p_oid,))
            p_has_disp = cur.fetchone()
            if p_has_disp and p_has_disp[0]:
                violations.append("purge_quarantine_dispatch_exec")

        # check Migration 0012 tables, indexes, ownership, and least-privilege grants
        cur.execute("SELECT to_regclass('public.schema_migrations')")
        if cur.fetchone()[0] is not None:
            cur.execute("SELECT 1 FROM public.schema_migrations WHERE version = '0012'")
            has_0012 = cur.fetchone() is not None
        else:
            has_0012 = False

        if has_0012:
            fc27_perf_tables = (
                "fc27_clubs",
                "fc27_export_batches",
                "fc27_matches",
                "fc27_player_match_stats",
                "fc27_quarantine_records",
            )
            for tbl in fc27_perf_tables:
                cur.execute(
                    """
                    SELECT c.relowner::regrole::text
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public' AND c.relname = %s
                    """,
                    (tbl,),
                )
                t_row = cur.fetchone()
                if not t_row:
                    violations.append(f"missing_table_{tbl}")
                    continue
                if t_row[0] != "fc27_owner":
                    violations.append(f"table_owner_{tbl}")
                # fc27_app must not have unrestricted DELETE on FC 27 persistence tables
                cur.execute("SELECT has_table_privilege('fc27_app', %s, 'DELETE')", (f"public.{tbl}",))
                if cur.fetchone()[0]:
                    violations.append(f"app_delete_on_{tbl}")
                # fc27_dispatch must not have access to FC 27 persistence tables
                for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    cur.execute("SELECT has_table_privilege('fc27_dispatch', %s, %s)", (f"public.{tbl}", priv))
                    if cur.fetchone()[0]:
                        violations.append(f"dispatch_{priv.lower()}_on_{tbl}")
                # fc27_relay must not have access to FC 27 persistence tables
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay'")
                if cur.fetchone():
                    for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                        cur.execute("SELECT has_table_privilege('fc27_relay', %s, %s)", (f"public.{tbl}", priv))
                        if cur.fetchone()[0]:
                            violations.append(f"relay_{priv.lower()}_on_{tbl}")

            fc27_perf_indexes = (
                "idx_fc27_matches_club_played_at",
                "idx_fc27_matches_export_id",
                "idx_fc27_player_match_stats_match_id",
                "idx_fc27_player_match_stats_club_player",
                "idx_fc27_quarantine_records_status_expires",
                "idx_fc27_quarantine_records_club_quarantined_at",
                "idx_fc27_export_batches_club_applied_at",
            )
            for idx_name in fc27_perf_indexes:
                cur.execute("SELECT to_regclass(%s)", (f"public.{idx_name}",))
                if cur.fetchone()[0] is None:
                    violations.append(f"missing_index_{idx_name}")

        # check Migration 0013 tables, indexes, ownership, and least-privilege grants
        cur.execute("SELECT to_regclass('public.schema_migrations')")
        if cur.fetchone()[0] is not None:
            cur.execute("SELECT 1 FROM public.schema_migrations WHERE version = '0013'")
            has_0013 = cur.fetchone() is not None
        else:
            has_0013 = False

        if has_0013:
            manual_tables = (
                "fc27_manual_matches",
                "fc27_manual_match_players",
                "fc27_manual_match_events",
                "fc27_manual_match_audit",
            )
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer'")
            has_writer_role = cur.fetchone() is not None
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay'")
            has_relay_role = cur.fetchone() is not None

            for tbl in manual_tables:
                cur.execute(
                    """
                    SELECT c.relowner::regrole::text
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public' AND c.relname = %s
                    """,
                    (tbl,),
                )
                t_row = cur.fetchone()
                if not t_row:
                    violations.append(f"missing_table_{tbl}")
                    continue
                if t_row[0] != "fc27_owner":
                    violations.append(f"table_owner_{tbl}")

                # fc27_app must have SELECT only (no INSERT, UPDATE, DELETE) on manual tables
                for priv in ("INSERT", "UPDATE", "DELETE"):
                    cur.execute("SELECT has_table_privilege('fc27_app', %s, %s)", (f"public.{tbl}", priv))
                    if cur.fetchone()[0]:
                        violations.append(f"app_{priv.lower()}_on_{tbl}")

                # fc27_dispatch and fc27_relay must have zero privileges on manual tables
                for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    cur.execute("SELECT has_table_privilege('fc27_dispatch', %s, %s)", (f"public.{tbl}", priv))
                    if cur.fetchone()[0]:
                        violations.append(f"dispatch_{priv.lower()}_on_{tbl}")
                    if has_relay_role:
                        cur.execute("SELECT has_table_privilege('fc27_relay', %s, %s)", (f"public.{tbl}", priv))
                        if cur.fetchone()[0]:
                            violations.append(f"relay_{priv.lower()}_on_{tbl}")

            if has_writer_role:
                # fc27_match_results_writer cannot DELETE from fc27_manual_matches or fc27_manual_match_audit
                cur.execute("SELECT has_table_privilege('fc27_match_results_writer', 'public.fc27_manual_matches', 'DELETE')")
                if cur.fetchone()[0]:
                    violations.append("writer_delete_on_fc27_manual_matches")
                for priv in ("UPDATE", "DELETE"):
                    cur.execute("SELECT has_table_privilege('fc27_match_results_writer', 'public.fc27_manual_match_audit', %s)", (priv,))
                    if cur.fetchone()[0]:
                        violations.append(f"writer_{priv.lower()}_on_fc27_manual_match_audit")

                # fc27_match_results_writer must have zero privileges on provider/governance/dispatch tables
                forbidden_writer_tables = (
                    "fc27_clubs",
                    "fc27_export_batches",
                    "fc27_matches",
                    "fc27_player_match_stats",
                    "fc27_quarantine_records",
                    "approval_requests",
                    "approval_audit",
                    "dispatches",
                    "outbox",
                    "schema_migrations",
                )
                for ftbl in forbidden_writer_tables:
                    cur.execute("SELECT to_regclass(%s)", (f"public.{ftbl}",))
                    if cur.fetchone()[0] is None:
                        continue
                    for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                        cur.execute(
                            "SELECT has_table_privilege('fc27_match_results_writer', %s, %s)",
                            (f"public.{ftbl}", priv),
                        )
                        if cur.fetchone()[0]:
                            violations.append(f"writer_{priv.lower()}_on_{ftbl}")

        # check Migration 0014 tables, indexes, triggers, ownership, and least-privilege grants
        cur.execute("SELECT to_regclass('public.schema_migrations')")
        if cur.fetchone()[0] is not None:
            cur.execute("SELECT 1 FROM public.schema_migrations WHERE version = '0014'")
            has_0014 = cur.fetchone() is not None
        else:
            has_0014 = False

        if has_0014:
            player_dev_tables = (
                "fc27_player_alias_fingerprints",
                "fc27_player_links",
                "fc27_build_catalog",
                "fc27_build_catalog_audit",
                "fc27_player_suggestions",
                "fc27_suggestion_assessments",
                "fc27_discord_jobs",
            )
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay'")
            has_relay = cur.fetchone() is not None
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer'")
            has_mr_writer = cur.fetchone() is not None

            for tbl in player_dev_tables:
                cur.execute(
                    """
                    SELECT c.relowner::regrole::text
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public' AND c.relname = %s
                    """,
                    (tbl,),
                )
                t_row = cur.fetchone()
                if not t_row:
                    violations.append(f"missing_table_{tbl}")
                    continue
                if t_row[0] != "fc27_owner":
                    violations.append(f"table_owner_{tbl}")

                # PUBLIC and unrelated roles must have zero privileges on Migration 0014 tables
                for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    cur.execute("SELECT has_table_privilege('public', %s, %s)", (f"public.{tbl}", priv))
                    if cur.fetchone()[0]:
                        violations.append(f"public_{priv.lower()}_on_{tbl}")
                    for unrelated_role in ("fc27_app", "fc27_dispatch"):
                        cur.execute("SELECT has_table_privilege(%s, %s, %s)", (unrelated_role, f"public.{tbl}", priv))
                        if cur.fetchone()[0]:
                            violations.append(f"{unrelated_role}_{priv.lower()}_on_{tbl}")
                    if has_relay:
                        cur.execute("SELECT has_table_privilege('fc27_relay', %s, %s)", (f"public.{tbl}", priv))
                        if cur.fetchone()[0]:
                            violations.append(f"fc27_relay_{priv.lower()}_on_{tbl}")
                    if has_mr_writer:
                        cur.execute(
                            "SELECT has_table_privilege('fc27_match_results_writer', %s, %s)",
                            (f"public.{tbl}", priv),
                        )
                        if cur.fetchone()[0]:
                            violations.append(f"fc27_match_results_writer_{priv.lower()}_on_{tbl}")

            player_dev_indexes = (
                "idx_fc27_player_alias_fingerprints_last_seen",
                "uq_fc27_player_links_confirmed_pseudonym",
                "uq_fc27_player_links_confirmed_discord_user",
                "idx_fc27_player_links_pseudonym_status",
                "idx_fc27_player_links_discord_user_status",
                "idx_fc27_build_catalog_status_key_version",
                "idx_fc27_build_catalog_audit_key_created",
                "idx_fc27_player_suggestions_open_pseudonym_status",
                "idx_fc27_player_suggestions_pseudonym_status",
                "idx_fc27_suggestion_assessments_suggestion_time",
                "idx_fc27_discord_jobs_pending_available",
                "idx_fc27_discord_jobs_claimed_lease",
            )
            for idx_name in player_dev_indexes:
                cur.execute("SELECT to_regclass(%s)", (f"public.{idx_name}",))
                if cur.fetchone()[0] is None:
                    violations.append(f"missing_index_{idx_name}")

            player_dev_triggers = (
                ("fc27_build_catalog_audit", "trg_fc27_build_catalog_audit_immutable"),
                ("fc27_suggestion_assessments", "trg_fc27_suggestion_assessments_immutable"),
                ("fc27_discord_jobs", "trg_fc27_discord_jobs_claim_lease"),
            )
            for tbl_name, trg_name in player_dev_triggers:
                cur.execute(
                    """
                    SELECT 1 FROM pg_trigger t
                    JOIN pg_class c ON c.oid = t.tgrelid
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public' AND c.relname = %s AND t.tgname = %s AND NOT t.tgisinternal
                    """,
                    (tbl_name, trg_name),
                )
                if cur.fetchone() is None:
                    violations.append(f"missing_trigger_{trg_name}")

            # Verify all 3 Stage C login roles exist when 0014 is applied
            for req_role in ("fc27_poll_writer", "fc27_player_dev_writer", "fc27_discord_reader"):
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (req_role,))
                if not cur.fetchone():
                    violations.append(f"missing_role_{req_role}")

            # Expected privilege matrices for Stage C roles
            expected_role_grants: dict[str, dict[str, set[str]]] = {
                "fc27_poll_writer": {
                    "fc27_clubs": {"SELECT", "INSERT", "UPDATE"},
                    "fc27_export_batches": {"SELECT", "INSERT", "UPDATE"},
                    "fc27_matches": {"SELECT", "INSERT"},
                    "fc27_player_match_stats": {"SELECT", "INSERT"},
                    "fc27_quarantine_records": {"SELECT", "INSERT"},
                    "fc27_player_alias_fingerprints": {"SELECT", "INSERT", "UPDATE"},
                    "fc27_discord_jobs": {"SELECT", "INSERT"},
                    "fc27_player_links": set(),
                    "fc27_build_catalog": set(),
                    "fc27_build_catalog_audit": set(),
                    "fc27_player_suggestions": set(),
                    "fc27_suggestion_assessments": set(),
                    "fc27_manual_matches": set(),
                    "fc27_manual_match_players": set(),
                    "fc27_manual_match_events": set(),
                    "fc27_manual_match_audit": set(),
                },
                "fc27_player_dev_writer": {
                    "fc27_player_links": {"SELECT", "INSERT", "UPDATE"},
                    "fc27_build_catalog": {"SELECT", "INSERT", "UPDATE"},
                    "fc27_build_catalog_audit": {"SELECT", "INSERT"},
                    "fc27_player_suggestions": {"SELECT", "INSERT", "UPDATE"},
                    "fc27_suggestion_assessments": {"SELECT", "INSERT"},
                    "fc27_discord_jobs": {"SELECT", "INSERT", "UPDATE"},
                    "fc27_player_alias_fingerprints": {"SELECT"},
                    "fc27_clubs": {"SELECT"},
                    "fc27_export_batches": {"SELECT"},
                    "fc27_matches": {"SELECT"},
                    "fc27_player_match_stats": {"SELECT"},
                    "fc27_quarantine_records": set(),
                    "fc27_manual_matches": set(),
                    "fc27_manual_match_players": set(),
                    "fc27_manual_match_events": set(),
                    "fc27_manual_match_audit": set(),
                },
                "fc27_discord_reader": {
                    "fc27_clubs": {"SELECT"},
                    "fc27_export_batches": {"SELECT"},
                    "fc27_matches": {"SELECT"},
                    "fc27_player_match_stats": {"SELECT"},
                    "fc27_quarantine_records": {"SELECT"},
                    "fc27_manual_matches": {"SELECT"},
                    "fc27_manual_match_players": {"SELECT"},
                    "fc27_manual_match_events": {"SELECT"},
                    "fc27_manual_match_audit": {"SELECT"},
                    "fc27_player_alias_fingerprints": {"SELECT"},
                    "fc27_player_links": {"SELECT"},
                    "fc27_build_catalog": {"SELECT"},
                    "fc27_build_catalog_audit": {"SELECT"},
                    "fc27_player_suggestions": {"SELECT"},
                    "fc27_suggestion_assessments": {"SELECT"},
                    "fc27_discord_jobs": {"SELECT"},
                },
            }
            legacy_forbidden_tables = (
                "approval_requests",
                "approval_audit",
                "dispatches",
                "outbox",
                "domain_events",
                "fc27_users",
                "fc27_sessions",
                "fc27_passkeys",
                "fc27_decision_evidence",
                "schema_migrations",
            )
            for role_name, table_map in expected_role_grants.items():
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role_name,))
                if not cur.fetchone():
                    continue
                for tbl_name, allowed_privs in table_map.items():
                    cur.execute("SELECT to_regclass(%s)", (f"public.{tbl_name}",))
                    if cur.fetchone()[0] is None:
                        continue
                    for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                        priv_fn = (
                            "has_any_column_privilege"
                            if priv == "UPDATE"
                            else "has_table_privilege"
                        )
                        cur.execute(
                            f"SELECT {priv_fn}(%s, %s, %s)",
                            (role_name, f"public.{tbl_name}", priv),
                        )
                        has_priv = bool(cur.fetchone()[0])
                        if priv in allowed_privs and not has_priv:
                            violations.append(f"missing_{role_name}_{priv.lower()}_on_{tbl_name}")
                        elif priv not in allowed_privs and has_priv:
                            violations.append(f"forbidden_{role_name}_{priv.lower()}_on_{tbl_name}")

                for leg_tbl in legacy_forbidden_tables:
                    cur.execute("SELECT to_regclass(%s)", (f"public.{leg_tbl}",))
                    if cur.fetchone()[0] is None:
                        continue
                    for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                        cur.execute(
                            "SELECT has_table_privilege(%s, %s, %s)",
                            (role_name, f"public.{leg_tbl}", priv),
                        )
                        if cur.fetchone()[0]:
                            violations.append(f"forbidden_{role_name}_{priv.lower()}_on_{leg_tbl}")
    return violations

def check_runtime_posture(conn: psycopg.Connection) -> list[str]:
    violations = []
    with conn.cursor() as cur:
        cur.execute("SELECT current_user")
        user = cur.fetchone()[0]
        cur.execute("SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls FROM pg_roles WHERE rolname = %s", (user,))
        row = cur.fetchone()
        if row and any(row):
            violations.append("superuser")
        cur.execute("SELECT pg_has_role(%s, 'fc27_owner', 'MEMBER')", (user,))
        if cur.fetchone()[0]:
            violations.append("owner_member")
        cur.execute("SELECT has_schema_privilege(%s, 'public', 'CREATE')", (user,))
        if cur.fetchone()[0]:
            violations.append("create_on_public")
    return violations

from src.db_roles import redact_conninfo

def migrate(conninfo: str, directory: Path = MIGRATIONS_DIR, *, target: str | None = None, checkpoint_setup: bool = False) -> list[str]:
    applied_now: list[str] = []

    with psycopg.connect(conninfo, autocommit=True, connect_timeout=3) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'fc27_owner'")
            if not cur.fetchone():
                raise MigrationError("run python -m src.db_roles bootstrap first")
            cur.execute("SELECT pg_has_role(current_user, 'fc27_owner', 'MEMBER')")
            if not cur.fetchone()[0]:
                raise MigrationError("run python -m src.db_roles bootstrap first")

            # SET ROLE fc27_owner
            cur.execute("SET ROLE fc27_owner")

            cur.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
            try:
                cur.execute(
                    """CREATE TABLE IF NOT EXISTS schema_migrations (
                           version    text PRIMARY KEY,
                           name       text NOT NULL,
                           sha256     text NOT NULL,
                           applied_at timestamptz NOT NULL DEFAULT now())"""
                )
                cur.execute(
                    """CREATE TABLE IF NOT EXISTS public.schema_policy_versions (
                           policy text,
                           version int,
                           sha256 text NOT NULL,
                           applied_at timestamptz NOT NULL DEFAULT now(),
                           applied_by text NOT NULL DEFAULT session_user,
                           PRIMARY KEY (policy, version))"""
                )

                done = {row[0]: row[1] for row in cur.execute("SELECT version, sha256 FROM schema_migrations")}
                for version, path in migration_files(directory):
                    if target and version > target:
                        continue
                    sql = path.read_text(encoding="utf-8")
                    digest = hashlib.sha256(sql.encode()).hexdigest()
                    if version in done:
                        if done[version] != digest:
                            raise RuntimeError(f"applied migration {path.name} was modified after it was applied")
                        continue
                    with conn.transaction():
                        cur.execute(sql)
                        cur.execute(
                            "INSERT INTO schema_migrations(version, name, sha256) VALUES (%s, %s, %s)",
                            (version, path.name, digest),
                        )
                    applied_now.append(path.name)

                # checkpoint_setup is retained only as an optional no-op parameter
                # after Stage B3 removed the legacy LangGraph runtime.
                _ = checkpoint_setup

                # Execute grants
                grants_path = directory / "post" / "grants.sql"
                if grants_path.exists():
                    sql = grants_path.read_text(encoding="utf-8")
                    lines = sql.splitlines()
                    if not lines or not lines[0].startswith(f"-- grants-policy-version: {GRANTS_POLICY.version}"):
                        raise MigrationError("grants.sql changed without a policy version bump")

                    digest = hashlib.sha256(sql.encode()).hexdigest()
                    if digest != GRANTS_POLICY.sha256:
                        raise MigrationError("grants.sql changed without a policy version bump")

                    cur.execute(sql)

                    cur.execute("SELECT sha256 FROM public.schema_policy_versions WHERE policy = 'grants' AND version = %s", (GRANTS_POLICY.version,))
                    existing = cur.fetchone()
                    if existing:
                        if existing[0] != digest:
                            raise MigrationError("existing row has another sha256")
                    else:
                        cur.execute("INSERT INTO public.schema_policy_versions(policy, version, sha256) VALUES ('grants', %s, %s) ON CONFLICT DO NOTHING", (GRANTS_POLICY.version, digest))

                violations = verify_postconditions(conn)
                if violations:
                    raise MigrationError(f"postcondition violations: {violations}")

            finally:
                cur.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
    return applied_now

def main() -> int:
    from src import settings
    try:
        conninfo = settings.read_secret("POSTGRES_URI")
    except settings.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2
    if not conninfo:
        print("POSTGRES_URI is not set", file=sys.stderr)
        return 2
    try:
        for name in migrate(conninfo):
            print(f"applied {name}")
        print("schema up to date")
        return 0
    except MigrationError as e:
        print(f"Migration error: {redact_conninfo(str(e))}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {redact_conninfo(str(e))}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    sys.exit(main())
