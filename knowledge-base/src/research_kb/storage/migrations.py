from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from research_kb.domain.canonical import hash_bytes
from research_kb.errors import storage_failure, unsupported_version
from research_kb.storage.db import execute_script, utc_now, write_tx
from research_kb.version import SCHEMA_VERSION

MIGRATION_PATTERN = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path
    sql: str
    checksum: str


def default_migrations_dir() -> Path:
    here = Path(__file__).resolve().parent
    candidates = [here / "migrations", here.parents[2] / "migrations", here.parents[3] / "migrations"]
    for candidate in candidates:
        if candidate.is_dir() and any(candidate.glob("*.sql")):
            return candidate
    raise storage_failure("Could not locate the migrations directory.")


def discover(migrations_dir: Path | None = None) -> list[Migration]:
    directory = migrations_dir or default_migrations_dir()
    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = MIGRATION_PATTERN.match(path.name)
        if not match:
            continue
        payload = path.read_bytes()
        migrations.append(
            Migration(
                version=int(match.group(1)),
                name=match.group(2),
                path=path,
                sql=payload.decode("utf-8"),
                checksum=hash_bytes(payload),
            )
        )
    versions = [item.version for item in migrations]
    if len(set(versions)) != len(versions):
        raise storage_failure("Duplicate migration versions discovered.")
    return migrations


def ensure_registry(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          checksum TEXT NOT NULL,
          applied_at TEXT NOT NULL
        ) STRICT
        """
    )


def applied_versions(conn: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    ensure_registry(conn)
    rows = conn.execute("SELECT version, name, checksum, applied_at FROM schema_migrations").fetchall()
    return {int(row["version"]): row for row in rows}


def verify_checksums(conn: sqlite3.Connection, migrations_dir: Path | None = None) -> None:
    migrations = {item.version: item for item in discover(migrations_dir)}
    applied = applied_versions(conn)
    for version, row in applied.items():
        migration = migrations.get(version)
        if migration is None:
            raise storage_failure(f"Applied migration {version} is missing from the migrations directory.")
        if migration.checksum != row["checksum"]:
            raise storage_failure(
                f"Migration checksum mismatch for version {version}.",
                expected=row["checksum"],
                actual=migration.checksum,
            )
    if applied and migrations:
        newest_known = max(migrations)
        newest_applied = max(applied)
        if newest_applied > newest_known:
            raise unsupported_version(
                "The database schema is newer than this runtime supports.",
                database_schema=newest_applied,
                runtime_schema=newest_known,
            )


def pending(conn: sqlite3.Connection, migrations_dir: Path | None = None) -> list[Migration]:
    applied = applied_versions(conn)
    return [item for item in discover(migrations_dir) if item.version not in applied]


def apply_all(conn: sqlite3.Connection, migrations_dir: Path | None = None) -> list[int]:
    ensure_registry(conn)
    verify_checksums(conn, migrations_dir)
    applied: list[int] = []
    for migration in pending(conn, migrations_dir):
        with write_tx(conn):
            execute_script(conn, migration.sql)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                (migration.version, migration.name, migration.checksum, utc_now()),
            )
        applied.append(migration.version)
    set_meta(conn, "schema_version", SCHEMA_VERSION)
    return applied


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO controller_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    try:
        row = conn.execute("SELECT value FROM controller_meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return default
    return row["value"] if row else default


def schema_checksum(migrations_dir: Path | None = None) -> str:
    digest = hashlib.sha256()
    for migration in discover(migrations_dir):
        digest.update(migration.checksum.encode("ascii"))
    return digest.hexdigest()
