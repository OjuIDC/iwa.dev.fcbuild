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

def _compute_grants_sha256() -> str:
    p = MIGRATIONS_DIR / "post" / "grants.sql"
    return hashlib.sha256(p.read_text(encoding="utf-8").encode()).hexdigest() if p.exists() else ""

GRANTS_POLICY = GrantsPolicy(version=5, sha256=_compute_grants_sha256())

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

def migrate(conninfo: str, directory: Path = MIGRATIONS_DIR, *, target: str | None = None, checkpoint_setup: bool = True) -> list[str]:
    applied_now: list[str] = []
    
    with psycopg.connect(conninfo, autocommit=True) as conn:
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
                    
                if checkpoint_setup:
                    with psycopg.connect(conninfo, autocommit=True, row_factory=psycopg.rows.dict_row) as check_conn:
                        with check_conn.cursor() as check_cur:
                            check_cur.execute("SET ROLE fc27_owner")
                        from langgraph.checkpoint.postgres import PostgresSaver
                        PostgresSaver(check_conn).setup()

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
