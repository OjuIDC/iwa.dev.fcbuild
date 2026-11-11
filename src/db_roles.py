from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from typing import Mapping

import psycopg
from psycopg import sql

from src import settings

ROLE_OWNER = "fc27_owner"
ROLE_MIGRATOR = "fc27_migrator"
ROLE_APP = "fc27_app"
ROLE_DISPATCH = "fc27_dispatch"
ROLE_RELAY = "fc27_relay"
ROLE_MATCH_RESULTS_WRITER = "fc27_match_results_writer"
ROLE_POLL_WRITER = "fc27_poll_writer"
ROLE_PLAYER_DEV_WRITER = "fc27_player_dev_writer"
ROLE_DISCORD_READER = "fc27_discord_reader"
GRANTS_POLICY_VERSION = 16


@dataclass
class BootstrapReport:
    pending_transfers: list[str]
    transferred: list[str]
    # Non-secret descriptions of DDL/DCL that a confirmed run would apply.
    # Populated only by dry runs (confirm=False); nothing in it has been executed.
    planned: list[str] = field(default_factory=list)


LOGIN_ROLES = (
    ROLE_MIGRATOR,
    ROLE_APP,
    ROLE_DISPATCH,
    ROLE_RELAY,
    ROLE_MATCH_RESULTS_WRITER,
    ROLE_POLL_WRITER,
    ROLE_PLAYER_DEV_WRITER,
    ROLE_DISCORD_READER,
)
RUNTIME_SCHEMA_USAGE_ROLES = (
    ROLE_APP,
    ROLE_DISPATCH,
    ROLE_RELAY,
    ROLE_MATCH_RESULTS_WRITER,
    ROLE_POLL_WRITER,
    ROLE_PLAYER_DEV_WRITER,
    ROLE_DISCORD_READER,
)
ALL_ROLES = (ROLE_OWNER, *LOGIN_ROLES)


def _connect_read_only(conninfo: str) -> psycopg.Connection:
    """Autocommit connection whose every statement runs in a READ ONLY transaction.

    Defense in depth for dry runs: PostgreSQL rejects CREATE ROLE, GRANT, REVOKE
    and ALTER ... OWNER inside a read-only transaction.
    """
    conn = psycopg.connect(conninfo, autocommit=True)
    conn.execute("SET default_transaction_read_only = on")
    return conn


def _existing_roles(cur) -> set[str]:
    cur.execute("SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)", (list(ALL_ROLES),))
    return {r[0] for r in cur.fetchall()}


def plan_cluster(admin_conninfo: str, passwords: Mapping[str, str | None]) -> list[str]:
    """Read-only. Describes what bootstrap_cluster() would change. Never includes passwords."""
    planned: list[str] = []
    with _connect_read_only(admin_conninfo) as conn, conn.cursor() as cur:
        existing = _existing_roles(cur)
        for role in ALL_ROLES:
            if role not in existing:
                kind = "NOLOGIN" if role == ROLE_OWNER else "LOGIN"
                planned.append(f"CREATE ROLE {role} {kind}")
        member = False
        if ROLE_OWNER in existing and ROLE_MIGRATOR in existing:
            cur.execute("SELECT pg_has_role(%s, %s, 'MEMBER')", (ROLE_MIGRATOR, ROLE_OWNER))
            member = bool(cur.fetchone()[0])
        if not member:
            planned.append(f"GRANT {ROLE_OWNER} TO {ROLE_MIGRATOR}")
        for role, pw in passwords.items():
            if pw is not None:
                planned.append(f"ALTER ROLE {role} PASSWORD <redacted>")
    return planned


def redact_conninfo(conninfo: str) -> str:
    """Returns the conninfo with any password replaced by ***."""
    if not conninfo:
        return conninfo
    try:
        info = psycopg.conninfo.conninfo_to_dict(conninfo)
        if "password" in info:
            pwd = info["password"]
            if "postgresql://" in conninfo or "postgres://" in conninfo:
                # Replace in URI
                return conninfo.replace(f":{pwd}@", ":***@")
            else:
                return conninfo.replace(f"password={pwd}", "password=***")
        return conninfo
    except psycopg.ProgrammingError:
        return conninfo

def bootstrap_cluster(admin_conninfo: str, passwords: Mapping[str, str | None]) -> None:
    """Idempotent. Creates the required roles and sets passwords."""
    with psycopg.connect(admin_conninfo, autocommit=True) as conn:
        with conn.cursor() as cur:
            for role in ALL_ROLES:
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
                if not cur.fetchone():
                    if role == ROLE_OWNER:
                        cur.execute(
                            f"CREATE ROLE {role} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS"
                        )
                    else:
                        cur.execute(
                            f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS"
                        )

            # GRANT fc27_owner TO fc27_migrator
            cur.execute(f"GRANT {ROLE_OWNER} TO {ROLE_MIGRATOR}")

            # Set passwords
            for r, pw in passwords.items():
                if pw is not None:
                    cur.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(r), sql.Literal(pw)))

def check_runtime_posture(conn: psycopg.Connection) -> list[str]:
    violations = []
    with conn.cursor() as cur:
        # Check superuser
        cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        if cur.fetchone()[0]:
            return ['superuser']

        # Check owner_member
        cur.execute("SELECT pg_has_role(current_user, %s, 'MEMBER')", (ROLE_OWNER,))
        if cur.fetchone()[0]:
            violations.append("owner_member")

        # Check create_on_public
        cur.execute("SELECT has_schema_privilege(current_user, 'public', 'CREATE')")
        if cur.fetchone()[0]:
            violations.append("create_on_public")

    return violations

def _acl_has(cur, *, catalog: str, name: str, grantee: str | None, privilege: str) -> bool:
    """True if the object's ACL explicitly grants `privilege` to `grantee` (None = PUBLIC)."""
    if catalog == "database":
        acl_sql = ("SELECT a.grantee, a.privilege_type FROM pg_database o, "
                   "aclexplode(COALESCE(o.datacl, acldefault('d', o.datdba))) a WHERE o.datname = %s")
    else:
        acl_sql = ("SELECT a.grantee, a.privilege_type FROM pg_namespace o, "
                   "aclexplode(COALESCE(o.nspacl, acldefault('n', o.nspowner))) a WHERE o.nspname = %s")
    if grantee is None:
        grantee_oid = 0
    else:
        cur.execute("SELECT oid FROM pg_roles WHERE rolname = %s", (grantee,))
        row = cur.fetchone()
        if row is None:
            return False
        grantee_oid = row[0]
    cur.execute(acl_sql, (name,))
    return any(int(g) == int(grantee_oid) and p == privilege for g, p in cur.fetchall())


def _plan_database_grants(cur, dbname: str) -> list[str]:
    """Read-only. Describes the GRANT/REVOKE statements a confirmed run would apply."""
    planned: list[str] = []
    for role in LOGIN_ROLES:
        if not _acl_has(cur, catalog="database", name=dbname, grantee=role, privilege="CONNECT"):
            planned.append(f"GRANT CONNECT ON DATABASE {dbname} TO {role}")
    if _acl_has(cur, catalog="schema", name="public", grantee=None, privilege="CREATE"):
        planned.append("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    for priv in ("USAGE", "CREATE"):
        if not _acl_has(cur, catalog="schema", name="public", grantee=ROLE_OWNER, privilege=priv):
            planned.append(f"GRANT {priv} ON SCHEMA public TO {ROLE_OWNER}")
    for role in RUNTIME_SCHEMA_USAGE_ROLES:
        if not _acl_has(cur, catalog="schema", name="public", grantee=role, privilege="USAGE"):
            planned.append(f"GRANT USAGE ON SCHEMA public TO {role}")
    return planned


def bootstrap_database(admin_db_conninfo: str, *, legacy_owner: str | None, confirm: bool) -> BootstrapReport:
    """Applies database-level grants and ownership transfers when confirm=True.

    With confirm=False this is a TRUE dry run: the session is read-only, no
    GRANT/REVOKE/ALTER is executed, and the report lists planned statements in
    `planned` and objects awaiting transfer in `pending_transfers`.
    """
    pending_transfers = []
    transferred = []
    planned: list[str] = []

    connect = (lambda: psycopg.connect(admin_db_conninfo, autocommit=True)) if confirm \
        else (lambda: _connect_read_only(admin_db_conninfo))

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database()")
            dbname = cur.fetchone()[0]
            if not confirm:
                planned.extend(_plan_database_grants(cur, dbname))
            else:
                # Grant CONNECT ON DATABASE to the login roles
                for role in LOGIN_ROLES:
                    cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(dbname), sql.Identifier(role)))

                # REVOKE CREATE ON SCHEMA public FROM PUBLIC
                cur.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")

                # GRANT USAGE, CREATE ON SCHEMA public TO fc27_owner
                cur.execute(f"GRANT USAGE, CREATE ON SCHEMA public TO {ROLE_OWNER}")

                # GRANT USAGE ON SCHEMA public TO runtime login roles
                runtime_roles_csv = ", ".join(RUNTIME_SCHEMA_USAGE_ROLES)
                cur.execute(f"GRANT USAGE ON SCHEMA public TO {runtime_roles_csv}")

            # Ownership transfer
            query = """
                SELECT 'table', c.relname
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                LEFT JOIN pg_depend d ON d.objid = c.oid AND d.deptype = 'e'
                WHERE n.nspname = 'public'
                  AND c.relkind IN ('r', 'v', 'm', 'p')
                  AND c.relowner::regrole::text != %s
                  AND d.objid IS NULL

                UNION ALL

                SELECT 'sequence', c.relname
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                LEFT JOIN pg_depend d ON d.objid = c.oid AND d.deptype IN ('e', 'a')
                WHERE n.nspname = 'public'
                  AND c.relkind = 'S'
                  AND c.relowner::regrole::text != %s
                  AND d.objid IS NULL

                UNION ALL

                SELECT 'function', p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')'
                FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                LEFT JOIN pg_depend d ON d.objid = p.oid AND d.deptype = 'e'
                WHERE n.nspname = 'public'
                  AND p.proowner::regrole::text != %s
                  AND d.objid IS NULL
            """

            if legacy_owner:
                query = f"""
                    WITH target_objs AS ({query})
                    SELECT kind, name FROM target_objs
                    -- We would need to add legacy_owner check in the subqueries if we want to filter properly.
                    -- Actually, we can just add `AND relowner::regrole::text = %s` in each subquery.
                """
                # Simplified query construction with legacy owner filter:
                query = """
                SELECT 'table', c.relname
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                LEFT JOIN pg_depend d ON d.objid = c.oid AND d.deptype = 'e'
                WHERE n.nspname = 'public'
                  AND c.relkind IN ('r', 'v', 'm', 'p')
                  AND c.relowner::regrole::text != %s
                  AND c.relowner::regrole::text = %s
                  AND d.objid IS NULL

                UNION ALL

                SELECT 'sequence', c.relname
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                LEFT JOIN pg_depend d ON d.objid = c.oid AND d.deptype IN ('e', 'a')
                WHERE n.nspname = 'public'
                  AND c.relkind = 'S'
                  AND c.relowner::regrole::text != %s
                  AND c.relowner::regrole::text = %s
                  AND d.objid IS NULL

                UNION ALL

                SELECT 'function', p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')'
                FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                LEFT JOIN pg_depend d ON d.objid = p.oid AND d.deptype = 'e'
                WHERE n.nspname = 'public'
                  AND p.proowner::regrole::text != %s
                  AND p.proowner::regrole::text = %s
                  AND d.objid IS NULL
                """
                cur.execute(query, (ROLE_OWNER, legacy_owner, ROLE_OWNER, legacy_owner, ROLE_OWNER, legacy_owner))
            else:
                cur.execute(query, (ROLE_OWNER, ROLE_OWNER, ROLE_OWNER))

            objects = cur.fetchall()

            for kind, name in objects:
                obj_desc = f"{kind} public.{name}"
                if kind == 'function':
                    # Need to construct the exact statement
                    stmt = f"ALTER FUNCTION public.{name} OWNER TO {ROLE_OWNER}"
                elif kind == 'sequence':
                    stmt = f"ALTER SEQUENCE public.{name} OWNER TO {ROLE_OWNER}"
                else:
                    # table, view, materialized view can be handled with ALTER TABLE
                    # Actually, standard ALTER TABLE works for tables, views, mat views
                    stmt = f"ALTER TABLE public.{name} OWNER TO {ROLE_OWNER}"

                if confirm:
                    cur.execute(stmt)
                    transferred.append(obj_desc)
                else:
                    pending_transfers.append(obj_desc)

    return BootstrapReport(sorted(pending_transfers), sorted(transferred), planned)

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Database role bootstrap for fc27_owner/migrator/app/dispatch. Without --confirm this is a "
            "read-only dry run: it reports planned changes and modifies nothing (exit 0 = nothing to do, "
            "3 = changes pending). With --confirm it creates roles, sets passwords, applies grants and "
            "transfers ownership."
        )
    )
    parser.add_argument("command", choices=["bootstrap"])
    parser.add_argument("--database", required=True)
    parser.add_argument("--legacy-owner", type=str)
    parser.add_argument("--confirm", action="store_true", help="apply changes (default: dry run)")
    args = parser.parse_args()

    try:
        admin_uri = settings.read_secret("POSTGRES_ADMIN_URI")
        migrator_pw = settings.read_secret("FC27_MIGRATOR_PASSWORD")
        app_pw = settings.read_secret("FC27_APP_PASSWORD")
        dispatch_pw = settings.read_secret("FC27_DISPATCH_PASSWORD")
        relay_pw = settings.read_secret("FC27_RELAY_PASSWORD", required=False)
        writer_pw = settings.read_secret("FC27_MATCH_RESULTS_WRITER_PASSWORD", required=False)
        poll_writer_pw = settings.read_secret("FC27_POLL_WRITER_PASSWORD", required=False)
        player_dev_writer_pw = settings.read_secret("FC27_PLAYER_DEV_WRITER_PASSWORD", required=False)
        discord_reader_pw = settings.read_secret("FC27_DISCORD_READER_PASSWORD", required=False)
    except settings.ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(2)

    if not admin_uri:
        print("Configuration error: POSTGRES_ADMIN_URI is not set", file=sys.stderr)
        sys.exit(2)

    passwords = {ROLE_MIGRATOR: migrator_pw, ROLE_APP: app_pw, ROLE_DISPATCH: dispatch_pw}
    if relay_pw is not None:
        passwords[ROLE_RELAY] = relay_pw
    if writer_pw is not None:
        passwords[ROLE_MATCH_RESULTS_WRITER] = writer_pw
    if poll_writer_pw is not None:
        passwords[ROLE_POLL_WRITER] = poll_writer_pw
    if player_dev_writer_pw is not None:
        passwords[ROLE_PLAYER_DEV_WRITER] = player_dev_writer_pw
    if discord_reader_pw is not None:
        passwords[ROLE_DISCORD_READER] = discord_reader_pw
    info = psycopg.conninfo.conninfo_to_dict(admin_uri)
    info["dbname"] = args.database
    db_conninfo = psycopg.conninfo.make_conninfo(**info)

    try:
        if not args.confirm:
            # TRUE dry run: read-only sessions, no CREATE ROLE / GRANT / REVOKE / ALTER.
            cluster_plan = plan_cluster(admin_uri, passwords)
            report = bootstrap_database(db_conninfo, legacy_owner=args.legacy_owner, confirm=False)
            planned = cluster_plan + report.planned
            print("DRY RUN: no changes were made.")
            if planned:
                print("Planned role/grant operations:")
                for p in planned:
                    print(f"  {p}")
            if report.pending_transfers:
                print("Pending ownership transfers:")
                for pt in report.pending_transfers:
                    print(f"  {pt}")
            if planned or report.pending_transfers:
                print("Run with --confirm to apply them.")
                sys.exit(3)
            print("Nothing to do.")
            sys.exit(0)

        bootstrap_cluster(admin_uri, passwords)
        report = bootstrap_database(db_conninfo, legacy_owner=args.legacy_owner, confirm=True)

        if report.transferred:
            print("Transferred ownership of:")
            for t in report.transferred:
                print(f"  {t}")

        print("Bootstrap complete.")
        sys.exit(0)
    except psycopg.Error as e:
        print(f"Database error: {redact_conninfo(str(e))}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
