from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path
from dataclasses import dataclass
from typing import LiteralString, cast

import psycopg
import psycopg.sql

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
_LOCK_KEY = 0x0FC27_0001

@dataclass(frozen=True)
class GrantsPolicy:
    version: int
    sha256: str

GRANTS_POLICY_VERSION = 22


def _compute_grants_sha256() -> str:
    p = MIGRATIONS_DIR / "post" / "grants.sql"
    return hashlib.sha256(p.read_text(encoding="utf-8").encode()).hexdigest() if p.exists() else ""


GRANTS_POLICY = GrantsPolicy(version=GRANTS_POLICY_VERSION, sha256=_compute_grants_sha256())

class MigrationError(RuntimeError):
    pass


def _finalize_fc27_alias_comparison_keys(cur: psycopg.Cursor) -> None:
    """Backfill Migration 0020 keys with the same Python normalizer used by writes."""
    import unicodedata

    from src.fc27_privacy import normalize_public_player_alias_key

    rows = cur.execute(
        "SELECT player_id, display_name, active FROM public.fc27_roster ORDER BY player_id"
    ).fetchall()
    keys: list[tuple[str, str, bool]] = []
    active_owners: dict[str, list[str]] = {}
    for player_id, display_name, active in rows:
        key = normalize_public_player_alias_key(str(display_name))
        if (
            not key
            or len(key) > 128
            or any(unicodedata.category(char) == "Cc" for char in key)
        ):
            raise MigrationError("Migration 0020 found an invalid roster comparison key")
        keys.append((str(player_id), key, bool(active)))
        if active:
            active_owners.setdefault(key, []).append(str(player_id))

    collisions = [owners for owners in active_owners.values() if len(owners) > 1]
    if collisions:
        raise MigrationError(
            "Migration 0020 found duplicate active roster comparison keys; resolve collisions manually"
        )

    cur.executemany(
        "UPDATE public.fc27_roster SET display_name_comparison_key = %s WHERE player_id = %s",
        [(key, player_id) for player_id, key, _active in keys],
    )
    missing_row = cur.execute(
        "SELECT count(*) FROM public.fc27_roster WHERE display_name_comparison_key IS NULL"
    ).fetchone()
    if missing_row is None:
        raise MigrationError("Migration 0020 could not verify comparison-key backfill")
    missing = missing_row[0]
    if missing:
        raise MigrationError("Migration 0020 comparison-key backfill left active roster rows missing")
    cur.execute(
        "ALTER TABLE public.fc27_roster ALTER COLUMN display_name_comparison_key SET NOT NULL"
    )
    cur.execute(
        """DO $$ BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'fc27_roster_comparison_key_chk'
              AND conrelid = 'public.fc27_roster'::regclass
        ) THEN
            ALTER TABLE public.fc27_roster ADD CONSTRAINT fc27_roster_comparison_key_chk
            CHECK (
                pg_catalog.char_length(display_name_comparison_key) BETWEEN 1 AND 128
                AND display_name_comparison_key = pg_catalog.btrim(display_name_comparison_key)
                AND display_name_comparison_key !~ '[\\x00-\\x1F\\x7F]'
            );
        END IF;
        END $$"""
    )
    cur.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_fc27_roster_active_comparison_key_unique
        ON public.fc27_roster (display_name_comparison_key) WHERE active = TRUE"""
    )

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
        row = cur.fetchone()
        if row and row[0]:
            violations.append("owner_member")
        cur.execute("SELECT has_schema_privilege('fc27_app', 'public', 'CREATE')")
        row = cur.fetchone()
        if row and row[0]:
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
            row = cur.fetchone()
            if row and row[0]:
                violations.append("relay_owner_member")
            cur.execute("SELECT has_schema_privilege('fc27_relay', 'public', 'CREATE')")
            row = cur.fetchone()
            if row and row[0]:
                violations.append("relay_create_on_public")

        # check fc27_match_results_writer
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer'")
        if cur.fetchone():
            cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = 'fc27_match_results_writer'")
            row = cur.fetchone()
            if row and row[0]:
                violations.append("match_results_writer_superuser")
            cur.execute("SELECT pg_has_role('fc27_match_results_writer', 'fc27_owner', 'MEMBER')")
            row = cur.fetchone()
            if row and row[0]:
                violations.append("match_results_writer_owner_member")
            cur.execute("SELECT has_schema_privilege('fc27_match_results_writer', 'public', 'CREATE')")
            row = cur.fetchone()
            if row and row[0]:
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
                row = cur.fetchone()
                if row and row[0]:
                    violations.append(f"{stage_c_role}_owner_member")
                cur.execute("SELECT has_schema_privilege(%s, 'public', 'CREATE')", (stage_c_role,))
                row = cur.fetchone()
                if row and row[0]:
                    violations.append(f"{stage_c_role}_create_on_public")
                cur.execute(
                    """
                    SELECT count(*) FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public' AND c.relowner::regrole::text = %s
                    """,
                    (stage_c_role,),
                )
                row = cur.fetchone()
                if row and row[0] > 0:
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
        row = cur.fetchone()
        if row and row[0] is not None:
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
                row = cur.fetchone()
                if row and row[0]:
                    violations.append(f"app_delete_on_{tbl}")
                # fc27_dispatch must not have access to FC 27 persistence tables
                for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    cur.execute("SELECT has_table_privilege('fc27_dispatch', %s, %s)", (f"public.{tbl}", priv))
                    row = cur.fetchone()
                    if row and row[0]:
                        violations.append(f"dispatch_{priv.lower()}_on_{tbl}")
                # fc27_relay must not have access to FC 27 persistence tables
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay'")
                if cur.fetchone():
                    for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                        cur.execute("SELECT has_table_privilege('fc27_relay', %s, %s)", (f"public.{tbl}", priv))
                        row = cur.fetchone()
                        if row and row[0]:
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
                row = cur.fetchone()
                if row is None or row[0] is None:
                    violations.append(f"missing_index_{idx_name}")

        # check Migration 0013 tables, indexes, ownership, and least-privilege grants
        cur.execute("SELECT to_regclass('public.schema_migrations')")
        row = cur.fetchone()
        if row and row[0] is not None:
            cur.execute("SELECT 1 FROM public.schema_migrations WHERE version = '0013'")
            has_0013 = cur.fetchone() is not None
        else:
            has_0013 = False

        if has_0013:
            cur.execute("SELECT 1 FROM schema_migrations WHERE version LIKE '0016%'")
            has_0016 = cur.fetchone() is not None
            manual_tables_list = [
                "fc27_manual_matches",
                "fc27_manual_match_players",
                "fc27_manual_match_events",
                "fc27_manual_match_audit",
            ]
            if has_0016:
                manual_tables_list.append("fc27_roster")
            manual_tables = tuple(manual_tables_list)
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
                    row = cur.fetchone()
                    if row and row[0]:
                        violations.append(f"app_{priv.lower()}_on_{tbl}")

                # fc27_dispatch and fc27_relay must have zero privileges on manual tables
                for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    cur.execute("SELECT has_table_privilege('fc27_dispatch', %s, %s)", (f"public.{tbl}", priv))
                    row = cur.fetchone()
                    if row and row[0]:
                        violations.append(f"dispatch_{priv.lower()}_on_{tbl}")
                    if has_relay_role:
                        cur.execute("SELECT has_table_privilege('fc27_relay', %s, %s)", (f"public.{tbl}", priv))
                        row = cur.fetchone()
                        if row and row[0]:
                            violations.append(f"relay_{priv.lower()}_on_{tbl}")

            if has_writer_role:
                # fc27_match_results_writer cannot DELETE from fc27_manual_matches, fc27_manual_match_audit, or fc27_roster
                cur.execute("SELECT has_table_privilege('fc27_match_results_writer', 'public.fc27_manual_matches', 'DELETE')")
                row = cur.fetchone()
                if row and row[0]:
                    violations.append("writer_delete_on_fc27_manual_matches")
                if has_0016:
                    cur.execute("SELECT has_table_privilege('fc27_match_results_writer', 'public.fc27_roster', 'DELETE')")
                    row = cur.fetchone()
                    if row and row[0]:
                        violations.append("writer_delete_on_fc27_roster")
                for priv in ("UPDATE", "DELETE"):
                    cur.execute("SELECT has_table_privilege('fc27_match_results_writer', 'public.fc27_manual_match_audit', %s)", (priv,))
                    row = cur.fetchone()
                    if row and row[0]:
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
                    row = cur.fetchone()
                    if row is None or row[0] is None:
                        continue
                    for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                        cur.execute(
                            "SELECT has_table_privilege('fc27_match_results_writer', %s, %s)",
                            (f"public.{ftbl}", priv),
                        )
                        row = cur.fetchone()
                        if row and row[0]:
                            violations.append(f"writer_{priv.lower()}_on_{ftbl}")

        # check Migration 0014 tables, indexes, triggers, ownership, and least-privilege grants
        cur.execute("SELECT to_regclass('public.schema_migrations')")
        row = cur.fetchone()
        if row and row[0] is not None:
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
                    row = cur.fetchone()
                    if row and row[0]:
                        violations.append(f"public_{priv.lower()}_on_{tbl}")
                    for unrelated_role in ("fc27_app", "fc27_dispatch"):
                        cur.execute("SELECT has_table_privilege(%s, %s, %s)", (unrelated_role, f"public.{tbl}", priv))
                        row = cur.fetchone()
                        if row and row[0]:
                            violations.append(f"{unrelated_role}_{priv.lower()}_on_{tbl}")
                    if has_relay:
                        cur.execute("SELECT has_table_privilege('fc27_relay', %s, %s)", (f"public.{tbl}", priv))
                        row = cur.fetchone()
                        if row and row[0]:
                            violations.append(f"fc27_relay_{priv.lower()}_on_{tbl}")
                    if has_mr_writer:
                        cur.execute(
                            "SELECT has_table_privilege('fc27_match_results_writer', %s, %s)",
                            (f"public.{tbl}", priv),
                        )
                        row = cur.fetchone()
                        if row and row[0]:
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
                row = cur.fetchone()
                if row is None or row[0] is None:
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
                    "fc27_roster": set(),
                },
                "fc27_player_dev_writer": {
                    "fc27_player_links": {"SELECT", "INSERT", "UPDATE"},
                    "fc27_build_catalog": {"SELECT", "INSERT", "UPDATE"},
                    "fc27_build_catalog_audit": {"SELECT", "INSERT"},
                    "fc27_player_suggestions": {"SELECT", "INSERT", "UPDATE"},
                    "fc27_suggestion_assessments": {"SELECT", "INSERT"},
                    "fc27_discord_jobs": {"SELECT", "INSERT", "UPDATE"},
                    "fc27_roster": {"SELECT"},
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
                    "fc27_roster": {"SELECT"},
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
                    row = cur.fetchone()
                    if row is None or row[0] is None:
                        continue
                    for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                        priv_fn = (
                            "has_any_column_privilege"
                            if priv == "UPDATE"
                            else "has_table_privilege"
                        )
                        cur.execute(
                            psycopg.sql.SQL("SELECT {priv_fn}(%s, %s, %s)").format(
                                priv_fn=psycopg.sql.SQL(cast(LiteralString, priv_fn))
                            ),
                            (role_name, f"public.{tbl_name}", priv),
                        )
                        row = cur.fetchone()
                        has_priv = bool(row and row[0])
                        if priv in allowed_privs and not has_priv:
                            violations.append(f"missing_{role_name}_{priv.lower()}_on_{tbl_name}")
                        elif priv not in allowed_privs and has_priv:
                            violations.append(f"forbidden_{role_name}_{priv.lower()}_on_{tbl_name}")

                for leg_tbl in legacy_forbidden_tables:
                    cur.execute("SELECT to_regclass(%s)", (f"public.{leg_tbl}",))
                    row = cur.fetchone()
                    if row is None or row[0] is None:
                        continue
                    for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                        cur.execute(
                            "SELECT has_table_privilege(%s, %s, %s)",
                            (role_name, f"public.{leg_tbl}", priv),
                        )
                        row = cur.fetchone()
                        if row and row[0]:
                            violations.append(f"forbidden_{role_name}_{priv.lower()}_on_{leg_tbl}")

        # check Migration 0017 durable reconciliation token table, index, ownership, and grants
        cur.execute("SELECT to_regclass('public.schema_migrations')")
        row = cur.fetchone()
        if row and row[0] is not None:
            cur.execute("SELECT 1 FROM public.schema_migrations WHERE version LIKE '0017%'")
            has_0017 = cur.fetchone() is not None
        else:
            has_0017 = False

        if has_0017:
            tok_tbl = "fc27_roster_reconciliation_tokens"
            cur.execute(
                """
                SELECT c.relowner::regrole::text
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relname = %s
                """,
                (tok_tbl,),
            )
            t_row = cur.fetchone()
            if not t_row:
                violations.append(f"missing_table_{tok_tbl}")
            else:
                if t_row[0] != "fc27_owner":
                    violations.append(f"table_owner_{tok_tbl}")
                cur.execute("SELECT to_regclass(%s)", ("public.idx_fc27_roster_reconciliation_tokens_expiry",))
                idx_row = cur.fetchone()
                if idx_row is None or idx_row[0] is None:
                    violations.append("missing_index_idx_fc27_roster_reconciliation_tokens_expiry")
                token_grant_matrix: dict[str, set[str]] = {
                    "public": set(),
                    "fc27_app": set(),
                    "fc27_dispatch": set(),
                    "fc27_relay": set(),
                    "fc27_poll_writer": set(),
                    "fc27_player_dev_writer": set(),
                    "fc27_discord_reader": {"SELECT"},
                    "fc27_match_results_writer": {"SELECT", "INSERT", "UPDATE", "DELETE"},
                }
                for role_name, allowed_privs in token_grant_matrix.items():
                    if role_name != "public":
                        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role_name,))
                        if not cur.fetchone():
                            continue
                    for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                        cur.execute(
                            "SELECT has_table_privilege(%s, %s, %s)",
                            (role_name, f"public.{tok_tbl}", priv),
                        )
                        p_row = cur.fetchone()
                        has_priv = bool(p_row and p_row[0])
                        if priv in allowed_privs and not has_priv:
                            violations.append(f"missing_{role_name}_{priv.lower()}_on_{tok_tbl}")
                        elif priv not in allowed_privs and has_priv:
                            violations.append(f"forbidden_{role_name}_{priv.lower()}_on_{tok_tbl}")

        # check Migration 0018 roster-refresh enqueue SECURITY DEFINER function and job_type constraint
        cur.execute("SELECT to_regclass('public.schema_migrations')")
        row = cur.fetchone()
        if row and row[0] is not None:
            cur.execute("SELECT 1 FROM public.schema_migrations WHERE version LIKE '0018%'")
            has_0018 = cur.fetchone() is not None
        else:
            has_0018 = False

        if has_0018:
            cur.execute(
                """
                SELECT pg_get_constraintdef(c.oid)
                FROM pg_constraint c
                JOIN pg_class t ON t.oid = c.conrelid
                JOIN pg_namespace n ON n.oid = t.relnamespace
                WHERE n.nspname = 'public'
                  AND t.relname = 'fc27_discord_jobs'
                  AND c.conname = 'fc27_discord_jobs_job_type_chk'
                """
            )
            chk_row = cur.fetchone()
            if not chk_row or "refresh_roster_messages" not in str(chk_row[0]):
                violations.append("missing_refresh_roster_messages_job_type_chk")

            cur.execute(
                """
                SELECT p.oid, p.prosecdef, p.proowner::regrole::text, p.proconfig
                FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname = 'public'
                  AND p.proname = 'fc27_enqueue_roster_refresh_job'
                """
            )
            enq_row = cur.fetchone()
            if not enq_row:
                violations.append("missing_fc27_enqueue_roster_refresh_job")
            else:
                e_oid, e_secdef, e_owner, e_config = enq_row
                if not e_secdef:
                    violations.append("roster_refresh_enqueue_not_secdef")
                if e_owner != "fc27_owner":
                    violations.append("roster_refresh_enqueue_not_owner")
                e_cfg = " ".join(e_config) if e_config else ""
                if "search_path=pg_catalog, pg_temp" not in e_cfg:
                    violations.append("roster_refresh_enqueue_search_path")

                for no_exec_role in (
                    "public",
                    "fc27_app",
                    "fc27_dispatch",
                    "fc27_relay",
                    "fc27_poll_writer",
                    "fc27_player_dev_writer",
                    "fc27_discord_reader",
                ):
                    if no_exec_role != "public":
                        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (no_exec_role,))
                        if not cur.fetchone():
                            continue
                    cur.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')", (no_exec_role, e_oid))
                    p_has = cur.fetchone()
                    if p_has and p_has[0]:
                        violations.append(f"roster_refresh_enqueue_{no_exec_role}_exec")

                cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer'")
                if cur.fetchone():
                    cur.execute(
                        "SELECT has_function_privilege('fc27_match_results_writer', %s, 'EXECUTE')",
                        (e_oid,),
                    )
                    w_has = cur.fetchone()
                    if not (w_has and w_has[0]):
                        violations.append("roster_refresh_enqueue_writer_missing_exec")

        # check Migration 0019 protected provider alias candidates & observations least-privilege matrix
        cur.execute("SELECT to_regclass('public.schema_migrations')")
        row = cur.fetchone()
        if row and row[0] is not None:
            cur.execute("SELECT 1 FROM public.schema_migrations WHERE version LIKE '0019%'")
            has_0019 = cur.fetchone() is not None
        else:
            has_0019 = False

        if has_0019:
            for cand_tbl in (
                "fc27_player_alias_candidates",
                "fc27_player_alias_candidate_observations",
            ):
                cur.execute(
                    "SELECT c.relowner::regrole::text FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relname = %s",
                    (cand_tbl,),
                )
                own_row = cur.fetchone()
                if not own_row:
                    violations.append(f"missing_table_{cand_tbl}")
                elif own_row[0] != "fc27_owner":
                    violations.append(f"wrong_owner_{cand_tbl}_{own_row[0]}")

            cand_matrix = {
                "public": (),
                "fc27_app": (),
                "fc27_dispatch": (),
                "fc27_relay": (),
                "fc27_player_dev_writer": (),
                "fc27_poll_writer": ("SELECT", "INSERT", "UPDATE"),
                "fc27_discord_reader": ("SELECT",),
                "fc27_match_results_writer": ("SELECT", "UPDATE"),
            }
            for role_name, allowed_privs in cand_matrix.items():
                if role_name != "public":
                    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role_name,))
                    if not cur.fetchone():
                        continue
                for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    if priv == "DELETE":
                        cur.execute(
                            "SELECT has_table_privilege(%s, 'public.fc27_player_alias_candidates', %s)",
                            (role_name, priv),
                        )
                    else:
                        cur.execute(
                            "SELECT has_any_column_privilege(%s, 'public.fc27_player_alias_candidates', %s)",
                            (role_name, priv),
                        )
                    p_row = cur.fetchone()
                    has_priv = bool(p_row and p_row[0])
                    if priv in allowed_privs and not has_priv:
                        violations.append(f"missing_{role_name}_{priv.lower()}_on_fc27_player_alias_candidates")
                    elif priv not in allowed_privs and has_priv:
                        violations.append(f"forbidden_{role_name}_{priv.lower()}_on_fc27_player_alias_candidates")

            for restricted_role, scoped_priv in (
                ("fc27_poll_writer", "UPDATE"),
                ("fc27_discord_reader", "SELECT"),
                ("fc27_match_results_writer", "SELECT"),
                ("fc27_match_results_writer", "UPDATE"),
            ):
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (restricted_role,))
                if cur.fetchone():
                    cur.execute(
                        "SELECT has_table_privilege(%s, 'public.fc27_player_alias_candidates', %s)",
                        (restricted_role, scoped_priv),
                    )
                    tbl_row = cur.fetchone()
                    if tbl_row and tbl_row[0]:
                        violations.append(
                            f"overbroad_table_{restricted_role}_{scoped_priv.lower()}_on_fc27_player_alias_candidates"
                        )

            for restricted_role in ("fc27_discord_reader", "fc27_match_results_writer"):
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (restricted_role,))
                if cur.fetchone():
                    cur.execute(
                        "SELECT has_column_privilege(%s, 'public.fc27_player_alias_candidates', 'public_alias', 'SELECT'), "
                        "has_column_privilege(%s, 'public.fc27_player_alias_candidates', 'alias_hmac_fingerprint', 'SELECT')",
                        (restricted_role, restricted_role),
                    )
                    col_row = cur.fetchone()
                    if not (col_row and col_row[0]):
                        violations.append(f"missing_{restricted_role}_select_public_alias")
                    if col_row and col_row[1]:
                        violations.append(f"forbidden_{restricted_role}_select_alias_hmac")

            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer'")
            if cur.fetchone():
                cur.execute(
                    "SELECT has_column_privilege('fc27_match_results_writer', 'public.fc27_player_alias_candidates', 'verification_status', 'UPDATE'), "
                    "has_column_privilege('fc27_match_results_writer', 'public.fc27_player_alias_candidates', 'promoted_at', 'UPDATE'), "
                    "has_column_privilege('fc27_match_results_writer', 'public.fc27_player_alias_candidates', 'public_alias', 'UPDATE')"
                )
                upd_row = cur.fetchone()
                if not (upd_row and upd_row[0] and upd_row[1]):
                    violations.append("missing_fc27_match_results_writer_update_status_cols")
                if upd_row and upd_row[2]:
                    violations.append("forbidden_fc27_match_results_writer_update_public_alias")

            obs_matrix = {
                "public": (),
                "fc27_app": (),
                "fc27_dispatch": (),
                "fc27_relay": (),
                "fc27_player_dev_writer": (),
                "fc27_discord_reader": (),
                "fc27_match_results_writer": (),
                "fc27_poll_writer": ("SELECT", "INSERT"),
            }
            for role_name, allowed_privs in obs_matrix.items():
                if role_name != "public":
                    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role_name,))
                    if not cur.fetchone():
                        continue
                for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    cur.execute(
                        "SELECT has_table_privilege(%s, 'public.fc27_player_alias_candidate_observations', %s)",
                        (role_name, priv),
                    )
                    p_row = cur.fetchone()
                    has_priv = bool(p_row and p_row[0])
                    if priv in allowed_privs and not has_priv:
                        violations.append(f"missing_{role_name}_{priv.lower()}_on_fc27_player_alias_candidate_observations")
                    elif priv not in allowed_privs and has_priv:
                        violations.append(f"forbidden_{role_name}_{priv.lower()}_on_fc27_player_alias_candidate_observations")

        # check Migration 0020 alias auto-promotion audit, review exceptions, and narrow SECURITY DEFINER boundaries
        cur.execute("SELECT to_regclass('public.schema_migrations')")
        row = cur.fetchone()
        if row and row[0] is not None:
            cur.execute("SELECT 1 FROM public.schema_migrations WHERE version LIKE '0020%'")
            has_0020 = cur.fetchone() is not None
        else:
            has_0020 = False

        if has_0020:
            for promo_tbl in (
                "fc27_player_alias_promotion_audit",
                "fc27_player_alias_review_exceptions",
            ):
                cur.execute(
                    "SELECT c.relowner::regrole::text FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relname = %s",
                    (promo_tbl,),
                )
                own_row = cur.fetchone()
                if not own_row:
                    violations.append(f"missing_table_{promo_tbl}")
                elif own_row[0] != "fc27_owner":
                    violations.append(f"wrong_owner_{promo_tbl}_{own_row[0]}")

            audit_matrix = {
                "public": (),
                "fc27_app": (),
                "fc27_dispatch": (),
                "fc27_relay": (),
                "fc27_player_dev_writer": (),
                "fc27_poll_writer": ("SELECT",),
                "fc27_discord_reader": ("SELECT",),
                "fc27_match_results_writer": ("SELECT",),
            }
            for role_name, allowed_privs in audit_matrix.items():
                if role_name != "public":
                    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role_name,))
                    if not cur.fetchone():
                        continue
                for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    cur.execute(
                        "SELECT has_table_privilege(%s, 'public.fc27_player_alias_promotion_audit', %s)",
                        (role_name, priv),
                    )
                    p_row = cur.fetchone()
                    has_priv = bool(p_row and p_row[0])
                    if priv in allowed_privs and not has_priv:
                        violations.append(f"missing_{role_name}_{priv.lower()}_on_fc27_player_alias_promotion_audit")
                    elif priv not in allowed_privs and has_priv:
                        violations.append(f"forbidden_{role_name}_{priv.lower()}_on_fc27_player_alias_promotion_audit")

            exc_matrix = {
                "public": (),
                "fc27_app": (),
                "fc27_dispatch": (),
                "fc27_relay": (),
                "fc27_player_dev_writer": (),
                "fc27_poll_writer": ("SELECT", "INSERT", "UPDATE"),
                "fc27_discord_reader": ("SELECT",),
                "fc27_match_results_writer": ("SELECT", "UPDATE"),
            }
            for role_name, allowed_privs in exc_matrix.items():
                if role_name != "public":
                    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role_name,))
                    if not cur.fetchone():
                        continue
                for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    if priv == "UPDATE":
                        cur.execute(
                            "SELECT has_any_column_privilege(%s, 'public.fc27_player_alias_review_exceptions', %s)",
                            (role_name, priv),
                        )
                    else:
                        cur.execute(
                            "SELECT has_table_privilege(%s, 'public.fc27_player_alias_review_exceptions', %s)",
                            (role_name, priv),
                        )
                    p_row = cur.fetchone()
                    has_priv = bool(p_row and p_row[0])
                    if priv in allowed_privs and not has_priv:
                        violations.append(f"missing_{role_name}_{priv.lower()}_on_fc27_player_alias_review_exceptions")
                    elif priv not in allowed_privs and has_priv:
                        violations.append(f"forbidden_{role_name}_{priv.lower()}_on_fc27_player_alias_review_exceptions")

            for restricted_role in ("fc27_poll_writer", "fc27_match_results_writer"):
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (restricted_role,))
                if cur.fetchone():
                    cur.execute(
                        "SELECT has_table_privilege(%s, 'public.fc27_player_alias_review_exceptions', 'UPDATE')",
                        (restricted_role,),
                    )
                    tbl_row = cur.fetchone()
                    if tbl_row and tbl_row[0]:
                        violations.append(
                            f"overbroad_table_{restricted_role}_update_on_fc27_player_alias_review_exceptions"
                        )

            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer'")
            if cur.fetchone():
                for wm_col in (
                    "exception_state",
                    "resolved_at",
                    "resolved_observation_count",
                    "resolved_distinct_match_count",
                    "resolved_last_seen_at",
                    "resolved_candidate_state_digest",
                    "updated_at",
                ):
                    cur.execute(
                        "SELECT has_column_privilege('fc27_match_results_writer', 'public.fc27_player_alias_review_exceptions', %s, 'UPDATE')",
                        (wm_col,),
                    )
                    col_row = cur.fetchone()
                    if not (col_row and col_row[0]):
                        violations.append(
                            f"missing_fc27_match_results_writer_update_{wm_col}_on_fc27_player_alias_review_exceptions"
                        )

            fn_specs = (
                ("fc27_auto_promote_stable_alias_candidate", ("fc27_poll_writer",)),
                ("fc27_evaluate_alias_candidates_for_promotion", ("fc27_poll_writer",)),
                ("fc27_record_manual_alias_promotion_audit", ("fc27_match_results_writer",)),
                ("fc27_purge_resolved_alias_review_exceptions", ("fc27_match_results_writer",)),
            )
            all_check_roles = (
                "public",
                "fc27_app",
                "fc27_dispatch",
                "fc27_relay",
                "fc27_poll_writer",
                "fc27_player_dev_writer",
                "fc27_discord_reader",
                "fc27_match_results_writer",
            )
            for fn_name, allowed_exec_roles in fn_specs:
                cur.execute(
                    """
                    SELECT p.oid, p.prosecdef, p.proowner::regrole::text, p.proconfig
                    FROM pg_proc p
                    JOIN pg_namespace n ON n.oid = p.pronamespace
                    WHERE n.nspname = 'public'
                      AND p.proname = %s
                    """,
                    (fn_name,),
                )
                fn_row = cur.fetchone()
                if not fn_row:
                    violations.append(f"missing_function_{fn_name}")
                    continue
                f_oid, f_secdef, f_owner, f_config = fn_row
                if not f_secdef:
                    violations.append(f"{fn_name}_not_secdef")
                if f_owner != "fc27_owner":
                    violations.append(f"{fn_name}_not_owner")
                f_cfg = " ".join(f_config) if f_config else ""
                if "search_path=pg_catalog, pg_temp" not in f_cfg:
                    violations.append(f"{fn_name}_search_path")
                for check_role in all_check_roles:
                    if check_role != "public":
                        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (check_role,))
                        if not cur.fetchone():
                            continue
                    cur.execute(
                        "SELECT has_function_privilege(%s, %s, 'EXECUTE')",
                        (check_role, f_oid),
                    )
                    e_row = cur.fetchone()
                    has_exec = bool(e_row and e_row[0])
                    if check_role in allowed_exec_roles and not has_exec:
                        violations.append(f"missing_{check_role}_exec_{fn_name}")
                    elif check_role not in allowed_exec_roles and has_exec:
                        violations.append(f"forbidden_{check_role}_exec_{fn_name}")

            # Migration 0020 deliberately has no SQL Unicode normalization helper.

            # Confirm fc27_poll_writer still has zero direct privileges on fc27_roster and fc27_discord_jobs_job_id_seq UPDATE/DELETE
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'fc27_poll_writer'")
            if cur.fetchone():
                for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    cur.execute(
                        "SELECT has_table_privilege('fc27_poll_writer', 'public.fc27_roster', %s)",
                        (priv,),
                    )
                    r_row = cur.fetchone()
                    if r_row and r_row[0]:
                        violations.append(f"forbidden_fc27_poll_writer_{priv.lower()}_on_fc27_roster")
    return violations

def check_runtime_posture(conn: psycopg.Connection) -> list[str]:
    violations = []
    with conn.cursor() as cur:
        cur.execute("SELECT current_user")
        user_row = cur.fetchone()
        if user_row is None:
            return violations
        user = user_row[0]
        cur.execute("SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls FROM pg_roles WHERE rolname = %s", (user,))
        row = cur.fetchone()
        if row and any(row):
            violations.append("superuser")
        cur.execute("SELECT pg_has_role(%s, 'fc27_owner', 'MEMBER')", (user,))
        row = cur.fetchone()
        if row and row[0]:
            violations.append("owner_member")
        cur.execute("SELECT has_schema_privilege(%s, 'public', 'CREATE')", (user,))
        row = cur.fetchone()
        if row and row[0]:
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
            _member_row = cur.fetchone()
            if not (_member_row and _member_row[0]):
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
                    migration_sql = path.read_text(encoding="utf-8")
                    digest = hashlib.sha256(migration_sql.encode()).hexdigest()
                    if version in done:
                        if done[version] != digest:
                            raise RuntimeError(f"applied migration {path.name} was modified after it was applied")
                        continue
                    with conn.transaction():
                        cur.execute(cast(LiteralString, migration_sql))
                        if version == "0020":
                            _finalize_fc27_alias_comparison_keys(cur)
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
                    grants_sql = grants_path.read_text(encoding="utf-8")
                    lines = grants_sql.splitlines()
                    if not lines or not lines[0].startswith(f"-- grants-policy-version: {GRANTS_POLICY.version}"):
                        raise MigrationError("grants.sql changed without a policy version bump")

                    digest = hashlib.sha256(grants_sql.encode()).hexdigest()
                    if digest != GRANTS_POLICY.sha256:
                        raise MigrationError("grants.sql changed without a policy version bump")

                    cur.execute(cast(LiteralString, grants_sql))

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
