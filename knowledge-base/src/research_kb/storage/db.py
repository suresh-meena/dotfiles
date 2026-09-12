from __future__ import annotations

import base64
import binascii
import contextlib
import json
import random
import sqlite3
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from research_kb.errors import cursor_invalid, storage_failure

WAL_FIX_VERSION = (3, 51, 3)
WAL_FIX_BACKPORTS = {(3, 50, 7), (3, 44, 6)}


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def new_id() -> str:
    return str(uuid.uuid4())


def version_tuple(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in text.split("."):
        digits = "".join(char for char in chunk if char.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def wal_reset_fix_present(version_text: str) -> bool:
    version = version_tuple(version_text)
    if version >= WAL_FIX_VERSION:
        return True
    for backport in WAL_FIX_BACKPORTS:
        if version[:2] == backport[:2] and version >= backport:
            return True
    return False


def probe_features(conn: sqlite3.Connection) -> dict[str, Any]:
    features: dict[str, Any] = {
        "sqlite_version": sqlite3.sqlite_version,
        "wal_reset_fix": wal_reset_fix_present(sqlite3.sqlite_version),
    }
    for pragma in ("foreign_keys", "journal_mode", "synchronous", "busy_timeout"):
        row = conn.execute(f"PRAGMA {pragma}").fetchone()
        features[f"pragma_{pragma}"] = row[0] if row else None
        features[pragma] = row[0] if row else None
    try:
        conn.execute("CREATE TEMP TABLE probe_strict (x INTEGER) STRICT")
        conn.execute("DROP TABLE probe_strict")
        features["strict_tables"] = True
    except sqlite3.Error:
        features["strict_tables"] = False
    try:
        row = conn.execute("SELECT json_valid('{}')").fetchone()
        features["json"] = bool(row and row[0])
    except sqlite3.Error:
        features["json"] = False
    try:
        conn.execute("CREATE VIRTUAL TABLE temp.probe_fts USING fts5(x)")
        conn.execute("DROP TABLE temp.probe_fts")
        features["fts5"] = True
    except sqlite3.Error:
        features["fts5"] = False
    features["backup_api"] = hasattr(conn, "backup")
    return features


def _allow_unpatched() -> bool:
    import os

    from research_kb.config import ENV_UNPATCHED_SQLITE

    return os.environ.get(ENV_UNPATCHED_SQLITE) in ("1", "true", "yes")


def connect(
    db_path: str | Path,
    *,
    read_only: bool = False,
    force_wal: bool | None = None,
    timeout: float = 5.0,
) -> sqlite3.Connection:
    path = Path(db_path)
    if read_only:
        if not path.exists():
            raise storage_failure(f"Cannot open missing database read-only: {path}")
        uri = f"file:{path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=timeout, isolation_level=None)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=timeout, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    check = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    if check != 1:
        conn.close()
        raise storage_failure("Could not enable foreign key enforcement on this connection.")
    conn.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
    if not read_only:
        want_wal = force_wal if force_wal is not None else (wal_reset_fix_present(sqlite3.sqlite_version) or _allow_unpatched())
        if want_wal:
            mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                conn.close()
                raise storage_failure("Could not enable WAL journal mode.", returned=mode)
        else:
            mode = conn.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
            if str(mode).lower() != "delete":
                conn.close()
                raise storage_failure("Could not enable rollback journal mode.", returned=mode)
        conn.execute("PRAGMA synchronous = FULL")
        synchronous = conn.execute("PRAGMA synchronous").fetchone()[0]
        if int(synchronous) != 2:
            conn.close()
            raise storage_failure("Could not set synchronous=FULL.", returned=synchronous)
        busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        if int(busy) != int(timeout * 1000):
            conn.close()
            raise storage_failure("Could not set busy_timeout.", returned=busy)
    return conn


def execute_script(conn: sqlite3.Connection, sql: str) -> None:
    statement = ""
    for line in sql.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            cleaned = statement.strip()
            if cleaned:
                conn.execute(cleaned)
            statement = ""
    remainder = statement.strip()
    if remainder:
        conn.execute(remainder)


@contextlib.contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    if conn.in_transaction:
        raise storage_failure("Nested write transactions are not permitted.")
    attempt = 0
    while True:
        try:
            conn.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError as exc:
            attempt += 1
            if attempt > 5 or "locked" not in str(exc).lower():
                raise storage_failure(f"Could not begin a write transaction: {exc}") from exc
            time.sleep((0.02 * attempt) + random.uniform(0, 0.02))
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise


@contextlib.contextmanager
def read_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("COMMIT")


def encode_snapshot_cursor(epoch: str, seq: int) -> str:
    payload = json.dumps({"e": epoch, "s": seq}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_snapshot_cursor(cursor: str) -> tuple[str, int]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        return str(payload["e"]), int(payload["s"])
    except (KeyError, ValueError, binascii.Error, TypeError) as exc:
        raise cursor_invalid("The snapshot cursor is not valid.") from exc


def encode_page_cursor(project_id: str, scope: str, last_key: str) -> str:
    payload = json.dumps({"p": project_id, "o": scope, "k": last_key}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_page_cursor(cursor: str, *, project_id: str, scope: str) -> str:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, binascii.Error) as exc:
        raise cursor_invalid("The page cursor is not valid.") from exc
    if payload.get("p") != project_id or payload.get("o") != scope:
        raise cursor_invalid("The page cursor does not belong to this project or query.")
    return str(payload.get("k", ""))
