"""Forward-only SQL migration runner for the FC27 PostgreSQL schema.

Usage:  python -m src.db_migrate            (reads POSTGRES_URI)

Each file in ``migrations/`` named ``NNNN_description.sql`` is applied once, in
order, inside its own transaction. A PostgreSQL advisory lock serialises
concurrent runners (for example several containers starting at once). Applied
files are recorded with their SHA-256; editing an applied file is an error.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
_LOCK_KEY = 0x0FC27_0001


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


def migrate(conninfo: str, directory: Path = MIGRATIONS_DIR) -> list[str]:
    applied_now: list[str] = []
    with psycopg.connect(conninfo, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS schema_migrations (
                       version    text PRIMARY KEY,
                       name       text NOT NULL,
                       sha256     text NOT NULL,
                       applied_at timestamptz NOT NULL DEFAULT now())"""
            )
            done = {row[0]: row[1] for row in conn.execute("SELECT version, sha256 FROM schema_migrations")}
            for version, path in migration_files(directory):
                sql = path.read_text(encoding="utf-8")
                digest = hashlib.sha256(sql.encode()).hexdigest()
                if version in done:
                    if done[version] != digest:
                        raise RuntimeError(f"applied migration {path.name} was modified after it was applied")
                    continue
                with conn.transaction():
                    conn.execute(sql)
                    conn.execute(
                        "INSERT INTO schema_migrations(version, name, sha256) VALUES (%s, %s, %s)",
                        (version, path.name, digest),
                    )
                applied_now.append(path.name)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
    return applied_now


def main() -> int:
    conninfo = os.environ.get("POSTGRES_URI")
    if not conninfo:
        print("POSTGRES_URI is not set", file=sys.stderr)
        return 2
    for name in migrate(conninfo):
        print(f"applied {name}")
    print("schema up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
